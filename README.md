# SAES SIP POWER ion-pump controller (unofficial)

Python driver for the SAES SIP POWER over Modbus TCP or RS485/Modbus RTU,
implementing both `labdevices.ionpump.IonPump` and
`labdevices.pressuregauge.PressureGauge`. Includes a read-only MQTT publisher
and a FreeBSD rc.d service.

The protocol is extracted from revision 4 of the manufacturer's manual; see
[docs/protocol.md](docs/protocol.md) for the complete register map and unresolved
manual details. Default IP is `10.0.0.10`, default Modbus unit ID is `11`.
TCP port `502` is the standard-based expectation, omitted from the manual;
`2527/UDP` is a separate custom protocol. DHCP is not documented.

## Installation

The new `IonPump` base class lives in the sibling `pylabdevs` checkout, at
`src/labdevices/ionpump.py`. Use that updated checkout: an older published
`pylabdevs` may not contain the new interface.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ../pylabdevs
python -m pip install .
```

The driver requires Python 3.8+, `pylabdevs-tspspi` with the new base class,
`pyserial`, and `paho-mqtt` 2.x. Modbus TCP uses the Python socket library.
No package release, device configuration, or service deployment is performed
by installing the source.

## Reading a pump

```python
from saessippower import SIPPower
from labdevices.pressuregauge import PressureGaugeUnit

with SIPPower("10.0.0.10", unit = 11, port = 502) as pump:
    print(pump.identify())
    print(pump.get_status())
    print(pump.get_voltage())                 # V, even when output is disabled
    print(pump.get_current())                 # A
    print(pump.get_pressure())                # mbar, or None when unavailable
    print(pump.get_pressure(PressureGaugeUnit.TORR))
    print(pump.get_measurement())             # timestamp, channel, readings, status
```

An object is both an `IonPump` and a `PressureGauge`. Pressure units are converted
locally; `set_unit()` changes the gauge adapter preference without writing to the
controller. `get_pressure()` defaults to mbar, following the existing gauge API.
There is no degas function on SIP POWER.

For model 3B0509 over RS485:

```python
with SIPPower(serial_port = "/dev/ttyU0", unit = 11) as pump:
    print(pump.get_status())
```

Serial defaults are 38400 baud, 8N2, no flow control. Each request is serialized,
uses a bounded response timeout, and observes the manual's 4 ms minimum gap.

## Explicit control

Connection, context exit, and object shutdown **never stop the pump** and never
change its watchdog. Control methods are explicit:

```python
pump.connect()
pump.set_voltage(5000)           # integer V, 1000–6000
pump.set_ramp_interval(10000)    # ms, 1000–60000
pump.set_conversion_rate(65)    # A/Torr, 1–200, depends on pump and voltage
pump.start(channel = 1)
pump.stop(channel = 1)
pump.restart(channel = 1)        # only if need_restart is set
pump.clear_alarms(channel = 1)
pump.disconnect()
```

`get_settings()`, `get_lifetime()` (hours), `get_keepalive()`, and `get_network()`
provide typed readbacks. `set_switch()` programs current thresholds in nA and
modes using `SwitchMode.OFF`, `SIMPLE`, or `WINDOW`. SW1 supports only OFF/SIMPLE;
SW2 and SW3 support WINDOW with lower and upper thresholds. Programming a switch
uses multiple writes and can partially succeed; no rollback is attempted.

`set_keepalive(0)` disables the watchdog; nonzero intervals are 1000–900000 ms.
The initiating master must continue polling within the configured interval.
The monitoring service does not take responsibility for starting a pump or
maintaining another master's watchdog.

Ordinary settings writes verify register readback. Start/stop/restart verify
reported enable/restart state. Clearing alarms acknowledges the clear command;
it does not prove that the underlying alarm cause was removed. If a write reply
or its verification is lost/invalid, `SIPPowerWriteUncertain` means the command
**may already have executed**. Read back state before issuing another command;
the driver never automatically retries writes. Explicit Modbus exceptions are
reported as `SIPPowerModbusError` with a numeric exception code.

`set_network(address, prefix_length)` and `set_modbus_id(unit)` close the
connection after their write sequence. Network packing follows conventional IPv4
integer order, which the manual does not explicitly confirm; compare
`get_network()` with the known address before changing it. Reconnect explicitly
to the new address/ID and reconcile state. If an ID-change reply is lost, the
transport retains its old ID: test old/new IDs explicitly instead of replaying
the change. ID changes perform the two required bypass writes first.

## Multi-pump base interface

`IonPump` supports `nChannels`, a selected `defaultChannel`, and an optional
`channel` argument on pump-specific routines. Channels are numbered from one;
SIP POWER has exactly one. See [docs/ionpump-interface.md](docs/ionpump-interface.md)
for the interface and a Gamma QPC mapping. No Gamma driver files are changed.

## MQTT service

The publisher follows `pyAgilentXGS600`:

```json
{"pressure_mbar":1.333223684e-6,"timestamp":1791808000.0}
```

Messages use QoS 1, are not retained, and publication waits for PUBACK. An optional
separate status topic carries the full measurement, including status and a nullable
pressure. Failed reads publish no pressure; disabled output, restart-required
state, alarms, zero voltage, or zero current make pressure unavailable rather than
reporting a false zero-pressure value. Pressure is estimated from current and the
configured A/Torr constant, not an independent gauge measurement. The conversion
constant is checked before and after the sample. No pressure is cached or
substituted after an error. Voltage ramping and calibration can still affect the
estimate; a valid response is not physical pressure qualification.

The service only reads device registers: it never starts/stops/restarts, clears
alarms, changes parameters, or subscribes to remote control commands. It recreates
connections after read/broker failures. MQTT delivery can be delayed or duplicated
at QoS 1; consumers should use the measurement timestamp.

Copy [examples/sippowermqtt.example.json](examples/sippowermqtt.example.json) to
`~/.config/sippowermqtt.conf`, set the device address and MQTT credentials, and
restrict its permissions:

```sh
chmod 600 ~/.config/sippowermqtt.conf
sippowermqtt --config ~/.config/sippowermqtt.conf --once
sippowermqtt --config ~/.config/sippowermqtt.conf
```

`--once` exits unsuccessfully if a valid pressure cannot be obtained and published.
SIGINT/SIGTERM stops the monitoring loop and closes connections without stopping
output. In-flight network waits remain bounded by their timeouts.

For RTU configuration, replace `device_host` with `serial_port`; optional
`baudrate` defaults to 38400. Optional `channel` defaults to 1 and must be 1 for
SIP POWER. `interval_seconds` is 1–60 (default 10); `timeout_seconds` is >0–30
(default 2). `modbus_port` and `modbus_unit` default to 502 and 11.

[examples/sippowermqtt.jem2100.example.json](examples/sippowermqtt.jem2100.example.json)
uses the broker/topic conventions observed read-only in the current XGS-600
configuration on `10.0.3.31`. It contains placeholder credentials and a distinct
pump/topic name; the existing `sip150` XGS-600 topic is not reused. The SIP POWER
address in this example is its factory default, **not a discovered address**.

### FreeBSD rc.d

Install [service/sippowermqtt](service/sippowermqtt) as
`/usr/local/etc/rc.d/sippowermqtt` with mode 0755, install the Python package in
the selected interpreter, and configure, for example:

```sh
sippowermqtt_enable="YES"
sippowermqtt_run_user="jem2100"
sippowermqtt_config="/home/jem2100/.config/sippowermqtt.conf"
sippowermqtt_python="/path/to/venv/bin/python"
```

The service account must own/read its private config and, for RTU, have serial
port access. `sippowermqtt_pythonpath` can point to source checkouts when needed;
`sippowermqtt_pidfile` defaults to `/var/run/sippowermqtt.pid`. The rc script uses
FreeBSD `daemon` supervision and syslog like the XGS-600 service.

The rc script and example are supplied for installation; no host service has
been installed, enabled, or restarted by this implementation.

## Local verification

```sh
PYTHONPATH=src:../pylabdevs/src python3 -m pytest -q
```

Tests use a local TCP Modbus dummy, a fragmented RTU serial dummy, and a local
MQTT protocol dummy with real Paho QoS 1 exchanges. They cover channel routing,
scaling/byte order, control readback, malformed packets, CRC, lost write replies,
private config validation, pressure suppression, and reconnect behavior.
This is local protocol evidence; the physical SIP POWER and production broker
have not been used for qualification.
