import math
import socket
import struct
import threading
from unittest.mock import patch

import pytest

from labdevices.ionpump import IonPump
from labdevices.pressuregauge import PressureGauge, PressureGaugeUnit
from saessippower import (SIPPower, SwitchMode, SIPPowerProtocolError,
    SIPPowerModbusError, SIPPowerWriteUncertain, SIPPowerCommunicationError)
from saessippower.transport import ModbusTCP, ModbusRTU, crc16
from dummy import tcp_dummy, Dummy


def pump_at(port):
    return SIPPower('127.0.0.1', port = port, timeout = 0.15)


def test_identification_status_scaling_pressure_and_disconnect_no_writes():
    with tcp_dummy() as (dummy, port):
        with pump_at(port) as pump:
            assert isinstance(pump, IonPump) and isinstance(pump, PressureGauge)
            identity = pump.identify()
            assert identity['serial_number'] == 0x33221100
            assert identity['hardware_version'] == (1, 2)
            assert identity['software_version'] == (3, 4)
            status = pump.get_status()
            assert status['uptime_seconds'] == 0x33221100
            assert status['switch_outputs'] == [True, False, True]
            assert status['input_voltage_V'] == 240
            assert status['temperature_C'] == pytest.approx(26.85)
            assert pump.get_voltage() == 5000
            assert pump.get_current() == pytest.approx(65000e-9)
            assert pump.get_pressure(PressureGaugeUnit.TORR) == pytest.approx(1e-6)
            assert PressureGauge.get_pressure(pump, PressureGaugeUnit.TORR) == pytest.approx(1e-6)
            assert pump.get_pressure(PressureGaugeUnit.PASCAL) == pytest.approx(1.333223684e-4)
            assert pump.get_network()['ip_address'] == '10.0.0.10'
            assert pump.get_network()['mac_address'] == '00:11:22:33:44:55'
            assert pump.get_lifetime() == 0
            pump.set_unit(PressureGaugeUnit.TORR)
            assert pump.get_unit() == PressureGaugeUnit.TORR
            with pytest.raises(NotImplementedError):
                pump.degas()
        assert not pump.is_connected()
        assert dummy.writes == []


@pytest.mark.parametrize('status,current,voltage', [(0, 0, 0), (0x11, 65000, 5000),
    (3, 65000, 5000), (1, 0, 5000), (1, 65000, 0)])
def test_unavailable_pressure_is_not_zero(status, current, voltage):
    with tcp_dummy() as (dummy, port):
        dummy.registers.update({0x3002: status, 0x3008: current, 0x3007: voltage})
        with pump_at(port) as pump:
            assert pump.get_measurement()['pressure_mbar'] is None
            assert pump.get_pressure() is None
            assert PressureGauge.get_pressure(pump) is None
            assert pump.get_voltage() == voltage
        assert dummy.writes == []


def test_settings_commands_and_readback():
    with tcp_dummy() as (dummy, port):
        with pump_at(port) as pump:
            assert pump.set_voltage(4500)
            assert pump.get_voltage_setpoint() == 4500
            assert pump.set_ramp_interval(60000)
            assert pump.set_conversion_rate(150)
            assert pump.set_keepalive(900000)
            assert pump.get_keepalive() == 900000
            assert pump.set_switch(2, SwitchMode.WINDOW, 123456, 234567)
            settings = pump.get_settings()
            assert settings['ramp_interval_ms'] == 60000
            assert settings['sw2_min_nA'] == 123456
            assert settings['sw2_max_nA'] == 234567
            assert settings['switch_modes'] == [0, 2, 0]
            assert pump.stop() and not pump.get_status()['enabled']
            assert pump.start() and pump.get_status()['enabled']
            with pytest.raises(ValueError, match = 'only when'):
                pump.restart()
            dummy.registers[0x3002] = 2
            with pytest.raises(ValueError, match = 'explicit restart'):
                pump.start()
            assert pump.restart()
            dummy.registers[0x3002] = 0x1011
            assert pump.clear_alarms()
            assert not pump.get_status()['alarm']
        assert (0x5006, [900000 & 65535, 900000 >> 16]) in dummy.writes


@pytest.mark.parametrize('call', [lambda p: p.start(2), lambda p: p.stop(True),
    lambda p: p.get_measurement(0), lambda p: p.set_voltage(float('nan')),
    lambda p: p.set_voltage(999), lambda p: p.set_voltage(1000.5),
    lambda p: p.set_conversion_rate(0), lambda p: p.set_keepalive(999),
    lambda p: p.set_switch(1, SwitchMode.WINDOW, 100, 200),
    lambda p: p.set_switch(2, SwitchMode.WINDOW, 200, 100)])
def test_validation_precedes_communication(call):
    with tcp_dummy() as (dummy, port):
        with pump_at(port) as pump:
            with pytest.raises(ValueError):
                call(pump)
        assert dummy.requests == []


@pytest.mark.parametrize('fault,error', [('transaction', SIPPowerProtocolError),
    ('count', SIPPowerProtocolError), ('exception', SIPPowerModbusError),
    ('drop', SIPPowerCommunicationError)])
def test_bad_read_frames_and_exceptions(fault, error):
    with tcp_dummy() as (dummy, port):
        dummy.fault = fault
        with pump_at(port) as pump:
            with pytest.raises(error):
                pump.get_status()
            assert pump.is_connected() == (fault == 'exception')


@pytest.mark.parametrize('fault', ['drop', 'wrong_write', 'transaction'])
def test_lost_or_invalid_write_ack_applies_once_and_requires_reconciliation(fault):
    with tcp_dummy() as (dummy, port):
        dummy.fault = fault
        pump = pump_at(port)
        pump.connect()
        with pytest.raises(SIPPowerWriteUncertain):
            pump.set_voltage(4500)
        assert not pump.is_connected()
        assert dummy.writes == [(0x4000, [4500])]
        pump.connect()
        assert pump.get_voltage_setpoint() == 4500
        pump.disconnect()
        assert dummy.writes == [(0x4000, [4500])]


def test_modbus_id_bypass_and_network_writes_close_connection():
    with tcp_dummy() as (dummy, port):
        pump = pump_at(port)
        pump.connect()
        assert pump.set_modbus_id(12)
        assert dummy.writes == [(0x7000, [0x5a5a]), (0x7001, [0xa5a5]), (0x8000, [12])]
        assert not pump.is_connected()
        pump.connect()
        assert pump.identify()['has_ethernet']
        assert pump.set_network('10.0.0.20', 24)
        assert dummy.writes[-1] == (0x5000, [20, 0x0a00, 24])
        assert not pump.is_connected()


def test_tcp_deadline_bounds_partial_response():
    client, server = socket.socketpair()
    transport = ModbusTCP('unused', timeout = 0.05)
    transport._socket = client
    def partial():
        server.recv(100)
        server.sendall(b'\x00')
    thread = threading.Thread(target = partial)
    thread.start()
    try:
        with pytest.raises(SIPPowerCommunicationError):
            transport.read(0x1000, 1)
        assert not transport.is_connected()
    finally:
        server.close()
        thread.join()


class SerialDummy:
    is_open = True
    def __init__(self, dummy, bad_crc = False):
        self.dummy = dummy
        self.buffer = bytearray()
        self.bad_crc = bad_crc
        self.frames = []
    def reset_input_buffer(self):
        self.buffer.clear()
    def write(self, frame):
        self.frames.append(frame)
        assert crc16(frame[:-2]) == struct.unpack('<H', frame[-2:])[0]
        response = self.dummy.reply(frame[0], frame[1:-2])
        response = bytes([frame[0]]) + response
        self.buffer.extend(response + struct.pack('<H', crc16(response) ^ int(self.bad_crc)))
        return len(frame)
    def read(self, count):
        result = self.buffer[:min(count, 2)]
        del self.buffer[:len(result)]
        return result
    def close(self):
        self.is_open = False


def test_rtu_crc_fragmented_reads_exceptions_and_serial_defaults():
    dummy = Dummy()
    serial = SerialDummy(dummy)
    with patch('serial.Serial', return_value = serial) as factory:
        with SIPPower(serial_port = '/dummy') as pump:
            assert pump.get_current() == pytest.approx(65000e-9)
            assert pump.set_voltage(4200)
            dummy.fault = 'exception'
            with pytest.raises(SIPPowerModbusError):
                pump.get_status()
        assert factory.call_args.kwargs['stopbits'] == 2
        assert factory.call_args.kwargs['parity'] == 'N'
        assert factory.call_args.args[1] == 38400
    assert not serial.is_open
    serial = SerialDummy(Dummy(), bad_crc = True)
    with patch('serial.Serial', return_value = serial):
        with SIPPower(serial_port = '/dummy') as pump:
            with pytest.raises(SIPPowerProtocolError, match = 'CRC'):
                pump.get_status()


def test_critical_settings_readback_mismatch_is_uncertain():
    with tcp_dummy() as (dummy, port):
        with pump_at(port) as pump:
            original = pump._transport.read
            with patch.object(pump._transport, 'read', side_effect = lambda address, count: [1] if address == 0x4000 else original(address, count)):
                with pytest.raises(SIPPowerWriteUncertain):
                    pump.set_voltage(4200)
        assert dummy.writes == [(0x4000, [4200])]


def test_conversion_change_and_invalid_calibration_reject_measurement():
    with tcp_dummy() as (dummy, port):
        with pump_at(port) as pump:
            with patch.object(pump, 'get_conversion_rate', side_effect = [65, 150]):
                with pytest.raises(SIPPowerProtocolError, match = 'changed'):
                    pump.get_measurement()
            dummy.registers[0x400e] = 0
            with pytest.raises(SIPPowerProtocolError, match = 'Invalid current-to-pressure'):
                pump.get_measurement()
        assert dummy.writes == []


@pytest.mark.parametrize('bit,name', [(5,'safe'), (6,'interlock'), (7,'overtemperature'),
    (8,'input_voltage'), (9,'output_voltage'), (10,'overcurrent'), (11,'arcing'), (12,'communication')])
def test_alarm_bits(bit, name):
    with tcp_dummy() as (dummy, port):
        dummy.registers[0x3002] = 1 | 0x10 | (1 << bit)
        with pump_at(port) as pump:
            assert pump.get_status()['alarms'] == [name]
            assert pump.get_pressure() is None


def test_rtu_lost_write_reply_is_uncertain_without_replay():
    dummy = Dummy()
    serial = SerialDummy(dummy)
    def drop_write(frame):
        dummy.reply(frame[0], frame[1:-2])
        return len(frame)
    with patch('serial.Serial', return_value = serial):
        with SIPPower(serial_port = '/dummy', timeout = 0.05) as pump:
            with patch.object(serial, 'write', side_effect = drop_write):
                with pytest.raises(SIPPowerWriteUncertain):
                    pump.set_voltage(4500)
        assert dummy.writes == [(0x4000, [4500])]
        assert not serial.is_open
