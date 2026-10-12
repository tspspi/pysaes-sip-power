"""Bounded synchronous Modbus transports; writes are never retried."""

import socket
import struct
import threading
import time

from labdevices.exceptions import CommunicationError


class SIPPowerCommunicationError(CommunicationError):
    pass


class SIPPowerProtocolError(SIPPowerCommunicationError):
    pass


class SIPPowerModbusError(SIPPowerCommunicationError):
    def __init__(self, function, code):
        self.function = function
        self.code = code
        super().__init__(f"Modbus function 0x{function:02x}: exception 0x{code:02x}")


class SIPPowerWriteUncertain(SIPPowerCommunicationError):
    """A write may have executed; reconcile readback before another command."""


def integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def timeout_value(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 < value <= 30:
        raise ValueError("Timeout must be greater than zero and at most 30 seconds")
    return value


def crc16(data):
    crc = 0xffff
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xa001 if crc & 1 else 0)
    return crc


class ModbusTransport:
    def __init__(self, unit = 11, timeout = 2.0):
        self.unit = integer(unit, "Unit ID", 1, 247)
        self.timeout = timeout_value(timeout)
        self.lock = threading.RLock()
        self._lastFrame = None

    def _exchange(self, pdu):
        with self.lock:
            if self._lastFrame is not None:
                time.sleep(max(0, 0.004 - (time.monotonic() - self._lastFrame)))
            try:
                response = self._frame(pdu)
                if not response:
                    raise SIPPowerProtocolError("Empty Modbus response")
                if response[0] == pdu[0] | 0x80:
                    if len(response) != 2:
                        raise SIPPowerProtocolError("Malformed exception response")
                    raise SIPPowerModbusError(pdu[0], response[1])
                if response[0] != pdu[0]:
                    raise SIPPowerProtocolError("Response function does not match request")
                return response
            except SIPPowerModbusError:
                raise
            except (OSError, SIPPowerCommunicationError) as error:
                self.disconnect()
                if pdu[0] == 0x10:
                    raise SIPPowerWriteUncertain("Write reply unavailable or invalid; execution is uncertain") from error
                if isinstance(error, SIPPowerCommunicationError):
                    raise
                raise SIPPowerCommunicationError(str(error)) from error
            finally:
                self._lastFrame = time.monotonic()

    def read(self, address, count):
        integer(address, "Address", 0, 65535)
        integer(count, "Word count", 1, 125)
        if address + count > 65536:
            raise ValueError("Read extends past address space")
        response = self._exchange(struct.pack(">BHH", 3, address, count))
        if len(response) != 2 + 2 * count or response[1] != 2 * count:
            self.disconnect()
            raise SIPPowerProtocolError("Read response has an invalid byte count")
        return list(struct.unpack(">" + "H" * count, response[2:]))

    def write(self, address, words):
        integer(address, "Address", 0, 65535)
        words = list(words)
        integer(len(words), "Word count", 1, 123)
        if address + len(words) > 65536:
            raise ValueError("Write extends past address space")
        for word in words:
            integer(word, "Word", 0, 65535)
        header = struct.pack(">BHH", 0x10, address, len(words))
        request = header + bytes([2 * len(words)]) + struct.pack(">" + "H" * len(words), *words)
        response = self._exchange(request)
        if response != header:
            self.disconnect()
            raise SIPPowerWriteUncertain("Write acknowledgement does not match request; reconcile state")
        return True


class ModbusTCP(ModbusTransport):
    def __init__(self, host, port = 502, unit = 11, timeout = 2.0):
        super().__init__(unit, timeout)
        if not isinstance(host, str) or not host:
            raise ValueError("Host must be a nonempty string")
        self.host = host
        self.port = integer(port, "TCP port", 1, 65535)
        self._socket = None
        self._transaction = 0

    def connect(self):
        with self.lock:
            if self._socket is None:
                try:
                    self._socket = socket.create_connection((self.host, self.port), self.timeout)
                except OSError as error:
                    raise SIPPowerCommunicationError(str(error)) from error
        return True

    def disconnect(self):
        with self.lock:
            if self._socket is not None:
                self._socket.close()
                self._socket = None
        return True

    def is_connected(self):
        return self._socket is not None

    def _receive(self, count, deadline):
        data = bytearray()
        while len(data) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Modbus response deadline expired")
            self._socket.settimeout(remaining)
            chunk = self._socket.recv(count - len(data))
            if not chunk:
                raise SIPPowerProtocolError("Connection closed during response")
            data.extend(chunk)
        return bytes(data)

    def _frame(self, pdu):
        if self._socket is None:
            raise SIPPowerCommunicationError("Not connected")
        self._transaction = (self._transaction + 1) & 0xffff
        self._socket.settimeout(self.timeout)
        self._socket.sendall(struct.pack(">HHHB", self._transaction, 0, len(pdu) + 1, self.unit) + pdu)
        deadline = time.monotonic() + self.timeout
        transaction, protocol, length, unit = struct.unpack(">HHHB", self._receive(7, deadline))
        if transaction != self._transaction or protocol != 0 or unit != self.unit or not 2 <= length <= 254:
            raise SIPPowerProtocolError("Invalid MBAP header")
        return self._receive(length - 1, deadline)


class ModbusRTU(ModbusTransport):
    def __init__(self, port, unit = 11, timeout = 2.0, baudrate = 38400):
        super().__init__(unit, timeout)
        if not isinstance(port, str) or not port:
            raise ValueError("Serial port must be a nonempty path")
        self.port = port
        self.baudrate = integer(baudrate, "Baud rate", 1, 1000000)
        self._serial = None

    def connect(self):
        import serial
        with self.lock:
            if self._serial is None:
                try:
                    self._serial = serial.Serial(self.port, self.baudrate, bytesize = 8,
                        parity = "N", stopbits = 2, timeout = self.timeout,
                        write_timeout = self.timeout, xonxoff = False, rtscts = False)
                except OSError as error:
                    raise SIPPowerCommunicationError(str(error)) from error
        return True

    def disconnect(self):
        with self.lock:
            if self._serial is not None:
                self._serial.close()
                self._serial = None
        return True

    def is_connected(self):
        return self._serial is not None and self._serial.is_open

    def _receive(self, count, deadline):
        data = bytearray()
        while len(data) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("RTU response deadline expired")
            self._serial.timeout = remaining
            chunk = self._serial.read(count - len(data))
            if not chunk:
                raise TimeoutError("Incomplete RTU response")
            data.extend(chunk)
        return bytes(data)

    def _frame(self, pdu):
        if not self.is_connected():
            raise SIPPowerCommunicationError("Not connected")
        self._serial.reset_input_buffer()
        request = bytes([self.unit]) + pdu
        request += struct.pack("<H", crc16(request))
        if self._serial.write(request) != len(request):
            raise SIPPowerProtocolError("Incomplete RTU write")
        deadline = time.monotonic() + self.timeout
        header = self._receive(2, deadline)
        if header[0] != self.unit:
            raise SIPPowerProtocolError("Unexpected RTU unit ID")
        if header[1] & 0x80:
            frame = header + self._receive(3, deadline)
        elif header[1] == 3:
            count = self._receive(1, deadline)
            if count[0] > 250:
                raise SIPPowerProtocolError("Invalid RTU byte count")
            frame = header + count + self._receive(count[0] + 2, deadline)
        elif header[1] == 0x10:
            frame = header + self._receive(6, deadline)
        else:
            raise SIPPowerProtocolError("Unexpected RTU function")
        if crc16(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
            raise SIPPowerProtocolError("RTU CRC mismatch")
        return frame[1:-2]
