# SAES SIP POWER communication protocol (extracted, unofficial)

## Connection and IP configuration

| Item | Value / behavior | Source |
| --- | --- | --- |
| Default IPv4 address | `10.0.0.10` | p. 46 |
| IP assignment | Configurable address and netmask; the documented setup is static. DHCP, automatic address assignment, and a default gateway are not described. | pp. 21, 41–42, 48 |
| Modbus TCP port | **Expected: TCP 502**, the standard Modbus TCP port. **The SIP POWER manual does not specify this port**, so it needs device confirmation. | External standard; see below |
| Custom UDP port | **UDP 2527**, fixed in firmware | p. 46 |
| Default Modbus slave/unit ID | **11** | p. 29 |
| Accepted Modbus TCP unit IDs | `1–247`; IDs outside this range receive no reply. Use the configured device ID, initially 11. | p. 30 |
| RTU broadcast ID | **255**, as stated by this manual; no response to broadcast reads | pp. 29–30 |
| RS485 defaults | **38400 baud, 8 data bits, no parity, 2 stop bits, no flow control** (`38400 8N2`) | p. 29 |
| Ethernet physical interface | 100BASE-TX | p. 30 |
| Startup delay | Ethernet communication becomes available about **30 seconds after power-on** | p. 13 |

The inference of TCP port 502 comes from the
[Modbus Organization TCP/IP implementation guide](https://www.modbus.org/docs/Modbus_Messaging_Implementation_Guide_V1_0b.pdf),
which assigns that port to Modbus TCP. UDP 2527 belongs to the manufacturer's
separate custom protocol, and is not evidence of a Modbus TCP port assignment.

The unit listens to all supported protocols concurrently; no protocol-selection
operation is required (p. 30). RS485/Modbus RTU is available on model **3B0509**.
Ethernet communication requires the Ethernet option; `CARD_TYPE` bit 1 reports
its presence. Model **3B0506** has the display, Ethernet, and USB interfaces
(pp. 30, 55).

### Changing the address

Three methods are documented:

1. **Front panel, on a display-equipped unit:** enter SETTINGS by holding the
   central and right keys together for 5 seconds. Select IP Address or IP
   Netmask, press ENTER to edit, and use the adjustment keys. ENTER advances
   through the address octets; saving occurs after completing all fields. The
   netmask is a CIDR prefix length, e.g. `24` means `255.255.255.0`.
   The MAC address can be viewed but cannot be changed (pp. 19–21).
2. **Modbus:** write `IP_ADDR` at `0x5000` (two words) and `IP_NETMASK` at
   `0x5002` (one word, CIDR prefix length in the low byte). These are adjacent
   and can be addressed as a three-word write (pp. 30, 41–42).
3. **Custom UDP:** command `0x41` sets the IP address and netmask using two
   32-bit fields (p. 48). See the UDP notes below.

The settings chapter says preset values survive power-off (p. 20). The networking
sections do not specify when a remotely changed address becomes effective,
whether a restart is needed, or how a reply behaves across the address change.
They also do not give a factory netmask. Do not assume DHCP will provide one.

For an initial connection, configure a client interface on a subnet that includes
`10.0.0.10`, avoid an address conflict, and use unit ID 11. The actual device may
already have a different saved address or ID. A successful read of `CARD_TYPE`
at `0x1000` is a useful initial protocol check.

RS485 connector: male DE-9, pin **3 = RS485+**, **4 = RS485−**,
**5 = GND**; communication is galvanically isolated from the supply frame (p. 29).

## Modbus functions, addressing, and encoding

The device implements Modbus Application Protocol V1.1b3 with only (p. 30):

| Function | Use |
| --- | --- |
| `0x03` | Read Holding Registers |
| `0x10` | Write Multiple Registers, including a write of just one word |

All addresses below are the manual's hexadecimal **protocol register addresses**.
Pass them directly to a client API that expects a zero-based PDU address, e.g.
`0x3000` is decimal `12288`. Do not add a `40001` holding-register display offset.
Counts are in **16-bit words**, not bytes or logical values. Read-only status
registers are still read with `0x03`, not Read Input Registers (`0x04`).

- **Bytes within a word:** most significant byte first.
- **Words within a multiword value:** least significant word first.
- Example: 32-bit `0x33221100` is returned as words `[0x1100, 0x3322]`,
  whose data bytes are `11 00 33 22`.
- Decode two words with `value = words[0] | (words[1] << 16)`; encode with
  `[value & 0xffff, (value >> 16) & 0xffff]`.
- A three-word MAC value follows the same general ordering:
  `value = words[0] | (words[1] << 16) | (words[2] << 32)`.
- RTU CRC bytes are least significant byte first. Standard Modbus TCP uses
  an MBAP header instead of an RTU CRC.
- The manual requires an **inter-frame interval of at least 4 ms**; it does not
  explicitly limit that requirement to RTU.
- Adjacent values may be read/written together. Keep each multiword value
  complete and aligned; avoid spanning unmapped gaps or incompatible access modes.

The manual defines `IP_ADDR` as a 32-bit value but does not explicitly map dotted
IPv4 octets to its bit positions. Under the conventional interpretation,
`10.0.0.10` is `0x0a00000a`, represented as words `[0x000a, 0x0a00]`.
Treat this octet interpretation as an assumption to verify by reading the known
address before implementing network-setting writes.

## Complete register map

`R` = readable, `W` = writable, `R/W` = both. All numerical values are unsigned.
Map source: pp. 30–32; detailed descriptions: pp. 32–45.

| Start address | Words | Access | Name | Meaning / units |
| --- | ---: | --- | --- | --- |
| `0x1000` | 1 | R | `CARD_TYPE` | Hardware capability bits |
| `0x1001` | 1 | R | `HW_CODE` | Hardware revision: high byte major, low byte minor |
| `0x1002` | 1 | R | `SW_VERSION` | Firmware version: high byte major, low byte minor |
| `0x1003` | 2 | R | `SERIAL_NUMBER` | 32-bit serial number |
| `0x2000` | 2 | R | `LIFE_TIME` | Total hours spent supplying current |
| `0x3000` | 1 | R | `TEMPERATURE` | Internal temperature, kelvin |
| `0x3001` | 1 | R | `ARCING_NUMBER` | Arcing events since the last start/restart |
| `0x3002` | 1 | R | `STATUS` | Enable, restart, current trend, and alarm bits |
| `0x3003` | 1 | R | `SW_STATUS` | SW1/SW2/SW3 output bits |
| `0x3004` | 2 | R | `UPTIME` | Seconds since last start/restart, not boot uptime |
| `0x3006` | 1 | R | `VIN` | Input voltage, decivolts (`raw / 10` V) |
| `0x3007` | 1 | R | `VOUT` | Output voltage, volts |
| `0x3008` | 2 | R | `IOUT` | Output current, nanoamperes (`raw × 1e-9` A) |
| `0x4000` | 1 | R/W | `VOUT_SETPOINT` | Output setpoint, V; `1000–6000` |
| `0x4001` | 2 | R/W | `VOUT_RAMP_INTV` | Ramp duration, ms; `1000–60000` |
| `0x4003` | 1 | R/W | `SW_MODE` | Two-bit mode fields for SW1, SW2, SW3 |
| `0x4004` | 2 | R/W | `SW1_THR` | SW1 overcurrent threshold, nA |
| `0x4006` | 2 | R/W | `SW2_THR_MIN` | SW2 simple-mode threshold / lower window threshold, nA |
| `0x4008` | 2 | R/W | `SW2_THR_MAX` | SW2 upper window threshold, nA; unused in simple mode |
| `0x400a` | 2 | R/W | `SW3_THR_MIN` | SW3 simple-mode threshold / lower window threshold, nA |
| `0x400c` | 2 | R/W | `SW3_THR_MAX` | SW3 upper window threshold, nA; unused in simple mode |
| `0x400e` | 1 | R/W | `CONV_RATE` | Current-to-pressure conversion constant, A/Torr |
| `0x5000` | 2 | R/W | `IP_ADDR` | IPv4 address; Ethernet option required |
| `0x5002` | 1 | R/W | `IP_NETMASK` | CIDR prefix length in bits 7:0; Ethernet option required |
| `0x5003` | 3 | R | `MAC_ADDR` | 48-bit Ethernet MAC address; Ethernet option required |
| `0x5006` | 2 | R/W | `KEEPALIVE` | Communication watchdog interval, ms |
| `0x6000` | 1 | W | `ENABLE_CMD` | `0` stop, `1` start, `2` restart |
| `0x6001` | 1 | W | `ALARM_CLEAR` | Any written value clears alarm latches |
| `0x7000` | 1 | W | `CRITICAL_STEP1` | First bypass value: `0x5a5a` |
| `0x7001` | 1 | W | `CRITICAL_STEP2` | Second bypass value: `0xa5a5`; corrected descriptive name |
| `0x8000` | 1 | W | `Modbus_ID` | New slave ID in bits 7:0, `1–247`; bypass required |

The network section says its registers are available only when `CARD_TYPE` bit 1
is set, and includes `KEEPALIVE` in that section. Elsewhere the watchdog is
described for both serial and Ethernet control. Availability of `0x5006` on a unit
without the Ethernet option is therefore unclear from the manual.

## Bitfields and measurements

### `CARD_TYPE` (`0x1000`, p. 32)

| Bit | Meaning |
| --- | --- |
| 0 | Display fitted |
| 1 | Ethernet fitted; networking registers available |
| 15:2 | Reserved |

### `STATUS` (`0x3002`, pp. 34–35)

| Bit(s) | Mask | Meaning |
| --- | --- | --- |
| 0 | `0x0001` | `ENABLE`: supply started |
| 1 | `0x0002` | `NEED_RESTART`: three arcing or three overcurrent events in less than 45 s |
| 3:2 | `0x000c` | `IOUT_GRADIENT`: `(status >> 2) & 3`; `0` holding, `1` rising, `2` falling; `3` not defined |
| 4 | `0x0010` | `ALARM`: at least one alarm latch set |
| 5 | `0x0020` | `ALM_SAFE`: SAFE input missing |
| 6 | `0x0040` | `ALM_INTERLOCK`: INTERLOCK input missing |
| 7 | `0x0080` | `ALM_OVERTEMP`: internal temperature above 80 °C |
| 8 | `0x0100` | `ALM_VIN`: input voltage deviates by 25% from rated voltage |
| 9 | `0x0200` | `ALM_VOUT`: output voltage more than 5% above setpoint |
| 10 | `0x0400` | `ALM_IOUT`: current exceeds SW1 threshold while SW1 protection is enabled |
| 11 | `0x0800` | `ALM_ARCING`: arcing event detected |
| 12 | `0x1000` | `ALM_COMM`: controlling master's keepalive expired |
| 15:13 | `0xe000` | Reserved |

Alarm latches record events; clearing a latch does not establish that its cause
has been removed. `ENABLE` reports the started state, so use `VOUT`/`IOUT` to
observe actual output.

### `SW_STATUS` and `SW_MODE` (pp. 35–36, 38)

`SW_STATUS` bits 0, 1, and 2 report SW1, SW2, and SW3 respectively;
bits 15:3 are reserved.

| `SW_MODE` field | Decode | Valid values |
| --- | --- | --- |
| SW1, bits 1:0 | `mode & 3` | `0` off/bypassed, `1` on/simple overcurrent protection |
| SW2, bits 3:2 | `(mode >> 2) & 3` | `0` off, `1` simple, `2` window |
| SW3, bits 5:4 | `(mode >> 4) & 3` | `0` off, `1` simple, `2` window |

Bits 15:6 are reserved. Simple mode uses the lower threshold only; window mode
uses both thresholds and is described as a hysteresis mode (pp. 14–15).
The protocol section does not fully specify switching behavior at equal
threshold values. The settings chapter gives threshold ranges of **0–99 mA**
(`0–99,000,000` nA), but the Modbus descriptions do not state validation bounds
or the required relationship between lower and upper thresholds.

### Scaling and settings (pp. 20–21, 36–41)

- Temperature in °C: `TEMPERATURE - 273.15`.
- `VIN`, `VOUT`, and `IOUT` are sampled in the same time slice.
- Estimated pressure in Torr: `(IOUT × 1e-9) / CONV_RATE`, inferred from the
  specified units. This is a current-derived estimate, not a separate pressure sensor.
- `CONV_RATE` is one 16-bit word. The settings chapter gives `1–200` A/Torr,
  default `65`; pump-dependent values are discussed there.
- Voltage ramp interval is the time to increase output from zero to the setpoint.
  The stated factory voltage setpoint is `5000` V.

## Commands and watchdog

Use function `0x10` for all writes (pp. 43–45).

| Command | Operation |
| --- | --- |
| Write `0` to `0x6000` | Stop supply |
| Write `1` to `0x6000` | Start when `NEED_RESTART` is clear |
| Write `2` to `0x6000` | Restart when `NEED_RESTART` is set |
| Write any value to `0x6001` | Clear all alarm latches; `0` is a convenient value |

Changing the Modbus ID requires writing `0x5a5a` to `0x7000`, then `0xa5a5` to
`0x7001`, then the new ID (`1–247`) to `0x8000`. The manual does not specify
bypass expiry, reset behavior, or whether the ID-change reply uses the old or
new ID. `0x8000` is write-only; it cannot be read to discover the current ID.

`KEEPALIVE` is a 32-bit millisecond interval (pp. 21, 42–43):

- `0` disables the watchdog. The Modbus description rejects nonzero intervals
  below `1000` ms.
- The front-panel chapter gives an active range of `1000–900000` ms; the
  Modbus description does not explicitly state an upper limit.
- After start/restart, the master/channel that enabled the supply must keep
  communicating within this interval.
- A successfully processed Modbus frame resets the watchdog; an exception
  response does not.
- Expiry stops output and latches the communication alarm.

## Read blocks and exception behavior

Useful complete, contiguous reads with function `0x03`:

| Start | Word count | Contents |
| --- | ---: | --- |
| `0x1000` | 5 | Capabilities, hardware/firmware versions, serial number |
| `0x2000` | 2 | Lifetime hours |
| `0x3000` | 10 | All status and measurement registers through `IOUT` |
| `0x4000` | 15 | All working settings through `CONV_RATE` |
| `0x5000` | 8 | IP, CIDR netmask, MAC, watchdog; subject to Ethernet availability |

For example, this standard Modbus TCP request reads the ten-word status block
with transaction ID 1 and unit ID 11 (standard framing; not hardware-tested):

```text
00 01 00 00 00 06 0b 03 30 00 00 0a
| transaction | protocol | length | unit | function | start | count |
```

The expected successful response PDU is `03 14` followed by 20 register-data
bytes. Decode word pairs at offsets 4/5 (`UPTIME`) and 8/9 (`IOUT`) low word first.

The implementation reports (p. 30):

| Exception | Cause |
| --- | --- |
| Illegal Function (`0x01`) | Unsupported function code |
| Illegal Data Address (`0x02`) | Unmapped address, reading write-only registers, or writing read-only registers |
| Illegal Data Value (`0x03`) | Invalid count/byte count, partial or misaligned values, or failed write validation |

Broadcast reads receive no reply. TCP requests with a unit ID outside `1–247`
receive no reply. The manual does not establish that every valid-range TCP ID
is accepted regardless of the configured slave ID; use the configured ID.
No device-specific maximum block size or response timeout is given.

## Custom UDP notes relevant to networking

This is a separate protocol (pp. 46–48), useful especially for IP configuration:

- Destination port: **2527/UDP**.
- Header: one-byte version `0x01`, one-byte command, then payload.
- Payload fields use **big-endian network byte order**, unlike Modbus multiword values.
- `0x05` reads all status/settings; the device answers with command `0x80`.
- `0x41` sets networking: payload contains 32-bit IP address followed by a
  32-bit netmask. Unlike Modbus's CIDR register, this field is described as a
  32-bit network mask; a full IPv4 mask is the natural interpretation, but
  the manual gives no byte example confirming it.
- Other commands: `0x01` start, `0x02` stop, `0x03` reset/restart,
  `0x04` clear alarm, `0x40` set working parameters.
- Mutating commands, including IP assignment, may be sent to a network broadcast
  address and affect **all SIP POWER units on that network**. Broadcast Read All
  is ignored; it is not a discovery mechanism.
- Mutating commands have **no confirmation protocol**. Verify execution by
  reading back status/settings, using the new address for an IP change.

The manual states a maximum UDP payload length of 302 bytes, but its packet
diagram labels the payload as bytes `2–302`, which is inconsistent with that
total. This does not affect the small IP-setting packet; it is an ambiguity for
an implementation of the complete custom protocol.

## Manual inconsistencies and unresolved details

- **TCP port:** omitted; `502` is a standards-based expectation, not a confirmed
  SIP POWER-specific value.
- **DHCP/default mask/gateway:** not documented. Only manual/static IP and mask
  configuration are described.
- **Display bit:** p. 32's prose calls it bit 1, but the bit diagram places
  DISPLAY at bit **0** and ETHERNET at bit **1**; this summary follows the diagram.
- **Serial number:** a prose label says bits 3:0, but the map and diagrams specify
  **32 bits / two words**.
- **Conversion constant:** prose says bits 31:0, while the map and diagram specify
  **16 bits / one word**; this summary follows the latter.
- **Enable field:** prose says bits 1:2; the diagram and commands use bits **1:0**.
- **Second critical bypass:** the manual repeats the name `CRITICAL_STEP1` for
  `0x7001`; this summary calls it `CRITICAL_STEP2` to distinguish the steps.
- **SW1 mode:** its description accidentally refers to SW2. The field is SW1,
  and only off/on values are specified.
- **Watchdog disable:** the front-panel chapter says values below one second
  disable it, whereas the Modbus section permits only **0** or **at least 1000** ms.
  Use `0` to disable it through Modbus.
- Network octet packing, address-change timing, bypass lifetime, and undocumented
  validation limits require confirmation before relying on them in a client.
