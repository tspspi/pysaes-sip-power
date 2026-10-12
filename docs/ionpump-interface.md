# Common ion-pump interface

Implemented in `labdevices.ionpump.IonPump`. The frontend follows existing pylabdevs conventions:
public methods validate parameters and call private methods implemented by a
hardware driver. It adds no transport or exit handler.

## Controller and channel model

One `IonPump` object represents one controller connection. Each pump output is a
one-based channel. The constructor accepts:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `nChannels` | `1` | Positive integer number of pump channels |
| `defaultChannel` | `1` | Channel selected for calls without an explicit channel |
| `voltageRange` | `None` | Optional finite `(minimum, maximum)` in volts |
| `debug` | `False` | Driver diagnostic preference |

Use `get_channel_count()`, `get_default_channel()`, and
`set_default_channel(channel)` to inspect/change selection. Every channel-specific
method accepts `channel=None`, which resolves to the selected default. Invalid
channel numbers, booleans, and noninteger channels are rejected before calling
the backend. There is deliberately no implicit all-channel start/stop operation.

```python
controller.start(channel = 1)
controller.get_voltage(channel = 4)
controller.get_pressure(channel = 3)
controller.set_default_channel(2)
controller.stop()                    # stops only pump 2
```

SIP POWER initializes one channel, voltage range 1000–6000 V. The Gamma Vacuum
QPC described by `../gammacli` has channels 1–4 and fits the same model.

## Public interface and backend hooks

| Public method | Backend hook | Scope / result |
| --- | --- | --- |
| `connect()` | `_connect()` | Controller connection; truthy success |
| `disconnect()`, `close()` | `_disconnect()` | Close transport; never stop output |
| `is_connected()` | `_is_connected()` | Local connection state, not a live health probe |
| `identify()` | `_identify()` | Controller identification; driver-defined information |
| `start(channel=None)` | `_start(channel)` | Explicit channel enable |
| `stop(channel=None)` | `_stop(channel)` | Explicit channel disable |
| `restart(channel=None)` | `_restart(channel)` | Optional explicit restart after a trip |
| `clear_alarms(channel=None)` | `_clear_alarms(channel)` | Optional alarm-latch clearing |
| `get_status(channel=None)` | `_get_status(channel)` | Channel state dictionary |
| `get_measurement(channel=None)` | `_get_measurement(channel)` | Timestamped reading dictionary |
| `get_voltage(channel=None)` | `_get_voltage(channel)` | Measured V; independent of pressure availability |
| `get_current(channel=None)` | `_get_current(channel)` | Measured A; independent of pressure availability |
| `get_pressure(unit=MBAR, channel=None)` | Uses `get_measurement()` | Current-derived pressure in requested unit, or `None` |
| `get_voltage_setpoint(channel=None)` | `_get_voltage_setpoint(channel)` | Optional configured output V |
| `set_voltage(voltage, channel=None)` | `_set_voltage(voltage, channel)` | Optional voltage setting; finite/range checked |
| `get_pump_size(channel=None)` | `_get_pump_size(channel)` | Optional configured speed, litres/second |

Backend hooks raise `NotImplementedError` unless supplied by the concrete driver.
A driver must not emulate a restart as an automatic stop/start sequence or
invent a voltage setting that its controller cannot support.

`get_measurement()` uses the common fields `ts` (Unix timestamp), `channel`,
`voltage_V`, `current_A`, and `pressure_mbar`; unavailable pressure is `None`.
Drivers can include further fields such as `status` or `responseValid`.
Communication failures raise exceptions; they must not look like a numerical
zero reading. Pressure is an estimate based on pumping current and controller
calibration. The base class converts mbar to Torr or Pascal using
`PressureGaugeUnit` from pylabdevs.

Status dictionaries should expose `enabled`, `need_restart`, and `alarms` when
those can be read. Unsupported information must remain explicitly unknown rather
than inferred from unrelated state. The SIP POWER driver provides these plus
raw bits, current trend, switch outputs, temperature, uptime, and arcing count.

## Lifecycle

Constructing the base class has no network or hardware side effects.
`connect()` / `disconnect()` and `with` provide explicit connection management;
they cannot be mixed while a context is active. `close()` is available for cleanup.
Context exit only closes the transport and does not stop a pump. There is no
`atexit` command.

Derived drivers own transport locking, timeout handling, and command verification.
An accepted command with missing readback must not be automatically replayed.
SIP POWER reports this as `SIPPowerWriteUncertain` and requires reconciliation.

## Mapping the existing Gamma QPC driver

The inspected `gammacli` frontend is `GammaIonPump` in
`../gammacli/src/gammaionctl/gammaionctl.py`:

| Common operation | Existing Gamma operation |
| --- | --- |
| Identification | `identify()` |
| Start channel | `enable(pumpIndex)` |
| Stop channel | `disable(pumpIndex)` |
| Measured voltage | `getVoltage(pumpIndex)` |
| Measured current | `getCurrent(pumpIndex)` |
| Estimated pressure | `getPressureWithUnits(pumpIndex)` or `getPressure(pumpIndex, require_units='mBar')` |
| Output enabled | `getHighVoltageStatus(pumpIndex)` |
| Human-readable status | `getSupplyStatus(pumpIndex)` |
| Configured pump speed | `getPumpSize(pumpIndex)` |
| Close connection | `close()` |

A future Gamma adapter can initialize `IonPump(nChannels=4)` and delegate these
hooks. It must normalize Gamma's `False` communication-failure returns into
exceptions and retain `None` for unavailable readings, convert reported pressure
units, and avoid treating a Boolean `False` as zero current/pressure. Gamma's
constructor connects immediately; an adapter with explicit `connect()` should
create that object in `_connect()` rather than its own constructor.

The existing Gamma API exposes no matching restart, alarm-clear, or voltage
setpoint setter, so an adapter should leave those optional hooks unsupported
until the Gamma protocol is extended. No changes to the Gamma driver were made.
A local four-channel test backend verifies routing and channel validation; it is
not a Gamma hardware test.

## PressureGauge compatibility

`SIPPower` derives from both `IonPump` and `PressureGauge` and explicitly
initializes both. The `IonPump` lifecycle and channel-aware pressure method take
precedence. The gauge hooks supply a unit/value dictionary compatible with the
existing pressure frontend, and unsupported degas raises `NotImplementedError`.

The new base does not require all future ion-pump drivers to inherit from
`PressureGauge`; a backend can implement a separate channel-bound gauge adapter
when it needs a gauge view of one pump on a multi-channel controller.
