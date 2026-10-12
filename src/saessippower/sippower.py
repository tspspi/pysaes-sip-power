"""SAES SIP POWER Modbus driver, also usable as a labdevices PressureGauge."""

import ipaddress
import math
import time
from enum import IntEnum

from labdevices.ionpump import IonPump
from labdevices.pressuregauge import PressureGauge, PressureGaugeUnit

from .transport import (ModbusTCP, ModbusRTU, integer,
    SIPPowerCommunicationError, SIPPowerProtocolError, SIPPowerWriteUncertain)


class SwitchMode(IntEnum):
    OFF = 0
    SIMPLE = 1
    WINDOW = 2


ALARM_BITS = {
    5: "safe", 6: "interlock", 7: "overtemperature", 8: "input_voltage",
    9: "output_voltage", 10: "overcurrent", 11: "arcing", 12: "communication"
}


def uint32(words):
    return words[0] | words[1] << 16


def words32(value):
    integer(value, "32-bit value", 0, 0xffffffff)
    return [value & 0xffff, value >> 16]


class SIPPower(IonPump, PressureGauge):
    """One SIP POWER channel over TCP or RTU.

    Construction and disconnect never change output or watchdog settings.
    All writes are explicit, unicast and never automatically retried.
    Modbus TCP port 502 is assumed from the standard; override if necessary.
    """

    def __init__(self, host = None, port = 502, unit = 11, timeout = 2.0,
                 serial_port = None, baudrate = 38400, debug = False):
        IonPump.__init__(self, nChannels = 1, voltageRange = (1000, 6000), debug = debug)
        PressureGauge.__init__(self, measurementRange = (0, math.inf), hasDegas = False,
            deviceSupportedUnits = list(PressureGaugeUnit), debug = debug)
        if (host is None) == (serial_port is None):
            raise ValueError("Supply exactly one of host or serial_port")
        self._transport = (ModbusTCP(host, port, unit, timeout) if host is not None
            else ModbusRTU(serial_port, unit, timeout, baudrate))
        self._pressureUnit = PressureGaugeUnit.MBAR

    def _connect(self):
        return self._transport.connect()

    def _disconnect(self):
        return self._transport.disconnect()

    def _is_connected(self):
        return self._transport.is_connected()

    def _identify(self):
        words = self._transport.read(0x1000, 5)
        return {
            "device": "SAES SIP POWER", "has_display": bool(words[0] & 1),
            "has_ethernet": bool(words[0] & 2), "card_type": words[0],
            "hardware_version": (words[1] >> 8, words[1] & 255),
            "software_version": (words[2] >> 8, words[2] & 255),
            "serial_number": uint32(words[3:5])
        }

    def get_device_serial(self):
        return self.identify()["serial_number"]

    def get_lifetime(self):
        """Hours spent supplying current."""
        return uint32(self._transport.read(0x2000, 2))

    @staticmethod
    def _decode_status(words):
        raw = words[2]
        return {
            "raw": raw, "enabled": bool(raw & 1), "need_restart": bool(raw & 2),
            "current_gradient": {0: "holding", 1: "rising", 2: "falling"}.get((raw >> 2) & 3, "unknown"),
            "alarm": bool(raw & 0x10),
            "alarms": [name for bit, name in ALARM_BITS.items() if raw & (1 << bit)],
            "switch_outputs": [bool(words[3] & (1 << bit)) for bit in range(3)],
            "temperature_K": words[0], "temperature_C": words[0] - 273.15,
            "arcing_events": words[1], "uptime_seconds": uint32(words[4:6]),
            "input_voltage_V": words[6] / 10, "voltage_V": words[7],
            "current_A": uint32(words[8:10]) * 1e-9
        }

    def _get_status(self, channel):
        return self._decode_status(self._transport.read(0x3000, 10))

    def _get_voltage(self, channel):
        return self._transport.read(0x3007, 1)[0]

    def _get_current(self, channel):
        return uint32(self._transport.read(0x3008, 2)) * 1e-9

    def _get_measurement(self, channel):
        with self._transport.lock:
            rate = self.get_conversion_rate()
            status = self.get_status(channel)
            timestamp = time.time()
            if self.get_conversion_rate() != rate:
                raise SIPPowerProtocolError("Conversion rate changed during measurement")
        if not 1 <= rate <= 200:
            raise SIPPowerProtocolError("Invalid current-to-pressure conversion rate")
        valid = (status["enabled"] and not status["need_restart"] and not status["alarm"]
            and not status["alarms"] and status["voltage_V"] > 0 and status["current_A"] > 0)
        pressure = status["current_A"] / rate if valid else None
        return {
            "ts": timestamp, "channel": channel, "voltage_V": status["voltage_V"],
            "current_A": status["current_A"], "pressure_torr": pressure,
            "pressure_mbar": pressure * 1.333223684 if valid else None,
            "pressure_estimated": True, "responseValid": valid,
            "conversion_rate_A_per_torr": rate, "status": status
        }

    def _get_pressure(self):
        measurement = self.get_measurement()
        mbar = measurement["pressure_mbar"]
        if mbar is None:
            return None
        return {"value": {PressureGaugeUnit.MBAR: mbar,
            PressureGaugeUnit.TORR: measurement["pressure_torr"],
            PressureGaugeUnit.PASCAL: mbar * 100}, "mbar": mbar}

    def _get_unit(self):
        return self._pressureUnit

    def _set_unit(self, unit):
        # Pressure units are an adapter preference; no device write is needed.
        self._pressureUnit = unit
        return True

    def _get_device_type(self):
        return {"device": "SAES SIP POWER"}

    def _get_serials(self):
        return self.get_device_serial()

    def _get_versions(self):
        return self.identify()["software_version"]

    def _get_voltage_setpoint(self, channel):
        return self._transport.read(0x4000, 1)[0]

    def _set_voltage(self, voltage, channel):
        if int(voltage) != voltage:
            raise ValueError("Voltage must be a whole number of volts")
        return self._write_verified(0x4000, [int(voltage)])

    def _write_verified(self, address, words):
        with self._transport.lock:
            self._transport.write(address, words)
            try:
                if self._transport.read(address, len(words)) != list(words):
                    raise SIPPowerWriteUncertain("Acknowledged setting differs from readback")
            except SIPPowerCommunicationError as error:
                if isinstance(error, SIPPowerWriteUncertain):
                    raise
                raise SIPPowerWriteUncertain("Setting acknowledged but readback unavailable") from error
        return True

    def _enable(self, command, channel):
        with self._transport.lock:
            self._transport.write(0x6000, [command])
            deadline = time.monotonic() + self._transport.timeout
            try:
                while True:
                    status = self.get_status(channel)
                    if status["enabled"] == (command != 0) and (command != 2 or not status["need_restart"]):
                        return True
                    if time.monotonic() >= deadline:
                        raise SIPPowerWriteUncertain("Enable command acknowledged; requested state not observed")
                    time.sleep(0.05)
            except SIPPowerCommunicationError as error:
                if isinstance(error, SIPPowerWriteUncertain):
                    raise
                raise SIPPowerWriteUncertain("Enable command acknowledged; status readback unavailable") from error

    def _start(self, channel):
        with self._transport.lock:
            if self.get_status(channel)["need_restart"]:
                raise ValueError("Controller requires an explicit restart")
            return self._enable(1, channel)

    def _stop(self, channel):
        return self._enable(0, channel)

    def _restart(self, channel):
        with self._transport.lock:
            if not self.get_status(channel)["need_restart"]:
                raise ValueError("Restart is valid only when need_restart is set")
            return self._enable(2, channel)

    def _clear_alarms(self, channel):
        # Clearing event latches does not remove their underlying causes.
        return self._transport.write(0x6001, [0])

    def get_settings(self):
        words = self._transport.read(0x4000, 15)
        return {
            "voltage_setpoint_V": words[0], "ramp_interval_ms": uint32(words[1:3]),
            "switch_modes": [(words[3] >> shift) & 3 for shift in (0, 2, 4)],
            "sw1_threshold_nA": uint32(words[4:6]),
            "sw2_min_nA": uint32(words[6:8]), "sw2_max_nA": uint32(words[8:10]),
            "sw3_min_nA": uint32(words[10:12]), "sw3_max_nA": uint32(words[12:14]),
            "conversion_rate_A_per_torr": words[14]
        }

    def set_ramp_interval(self, milliseconds):
        integer(milliseconds, "Ramp interval (ms)", 1000, 60000)
        return self._write_verified(0x4001, words32(milliseconds))

    def get_conversion_rate(self):
        return self._transport.read(0x400e, 1)[0]

    def set_conversion_rate(self, rate):
        integer(rate, "Conversion rate (A/Torr)", 1, 200)
        return self._write_verified(0x400e, [rate])

    def set_switch(self, switch, mode, minimum_nA, maximum_nA = None):
        integer(switch, "Switch", 1, 3)
        mode = SwitchMode(integer(mode, "Switch mode", 0, 2))
        integer(minimum_nA, "Lower threshold (nA)", 0, 99000000)
        if switch == 1 and (mode == SwitchMode.WINDOW or maximum_nA is not None):
            raise ValueError("SW1 supports only off/simple with one threshold")
        if mode == SwitchMode.WINDOW and maximum_nA is None:
            raise ValueError("Window mode requires an upper threshold")
        if maximum_nA is not None:
            integer(maximum_nA, "Upper threshold (nA)", minimum_nA, 99000000)
        with self._transport.lock:
            # Program thresholds before enabling the new mode. No automatic
            # rollback is attempted if the controller accepts a partial sequence.
            address = {1: 0x4004, 2: 0x4006, 3: 0x400a}[switch]
            self._write_verified(address, words32(minimum_nA))
            if maximum_nA is not None:
                self._write_verified(address + 2, words32(maximum_nA))
            current = self._transport.read(0x4003, 1)[0]
            shift = 2 * (switch - 1)
            return self._write_verified(0x4003, [(current & ~(3 << shift)) | int(mode) << shift])

    def get_keepalive(self):
        return uint32(self._transport.read(0x5006, 2))

    def set_keepalive(self, milliseconds):
        integer(milliseconds, "Keepalive (ms)", 0, 900000)
        if 0 < milliseconds < 1000:
            raise ValueError("Keepalive is zero or at least 1000 ms")
        return self._write_verified(0x5006, words32(milliseconds))

    def get_network(self):
        if not self.identify()["has_ethernet"]:
            raise ValueError("Controller does not report an Ethernet interface")
        words = self._transport.read(0x5000, 8)
        mac = words[3] | words[4] << 16 | words[5] << 32
        return {"ip_address": str(ipaddress.IPv4Address(uint32(words[:2]))),
            "prefix_length": words[2] & 255,
            "mac_address": ":".join(f"{byte:02x}" for byte in mac.to_bytes(6, "big")),
            "keepalive_ms": uint32(words[6:8])}

    def set_network(self, address, prefix_length):
        """Explicit IP write using conventional octet packing; verify on hardware.

        The connection is closed after the write, even if its reply is lost.
        Reconnect explicitly to the new address and reconcile settings.
        """
        address = int(ipaddress.IPv4Address(address))
        integer(prefix_length, "CIDR prefix", 0, 32)
        if not self.identify()["has_ethernet"]:
            raise ValueError("Controller does not report an Ethernet interface")
        try:
            return self._transport.write(0x5000, words32(address) + [prefix_length])
        finally:
            self.close()

    def set_modbus_id(self, unit):
        """Change ID explicitly; close connection and require explicit reconnect."""
        integer(unit, "Unit ID", 1, 247)
        with self._transport.lock:
            try:
                self._transport.write(0x7000, [0x5a5a])
                self._transport.write(0x7001, [0xa5a5])
                result = self._transport.write(0x8000, [unit])
            finally:
                self.close()
            self._transport.unit = unit
            return result
