"""Local-only Modbus TCP device with a journal of requests and applied writes."""

import socketserver
import struct
import threading
from contextlib import contextmanager


def receive(sock, size):
    result = bytearray()
    while len(result) < size:
        data = sock.recv(size - len(result))
        if not data:
            return None
        result.extend(data)
    return bytes(result)


class Dummy:
    def __init__(self):
        self.unit = 11
        self.registers = {}
        for start, count in ((0x1000, 5), (0x2000, 2), (0x3000, 10), (0x4000, 15), (0x5000, 8)):
            self.registers.update({start + i: 0 for i in range(count)})
        self.registers.update({0x1000: 3, 0x1001: 0x0102, 0x1002: 0x0304,
            0x1003: 0x1100, 0x1004: 0x3322, 0x3000: 300, 0x3002: 1,
            0x3003: 5, 0x3004: 0x1100, 0x3005: 0x3322, 0x3006: 2400,
            0x3007: 5000, 0x3008: 65000, 0x3009: 0,
            0x4000: 5000, 0x4001: 1000, 0x400e: 65,
            0x5000: 10, 0x5001: 0x0a00, 0x5002: 24,
            0x5003: 0x4455, 0x5004: 0x2233, 0x5005: 0x0011})
        self.requests = []
        self.writes = []
        self.fault = None

    def reply(self, unit, pdu):
        self.requests.append((unit, pdu))
        function, address, count = struct.unpack('>BHH', pdu[:5])
        if unit != self.unit:
            return None
        if self.fault == 'exception':
            return bytes([function | 0x80, 3])
        if function == 3:
            if any(address + i not in self.registers for i in range(count)):
                return bytes([0x83, 2])
            return bytes([3, count * 2]) + struct.pack('>' + 'H' * count,
                *(self.registers[address + i] for i in range(count)))
        words = list(struct.unpack('>' + 'H' * count, pdu[6:]))
        self.writes.append((address, words))
        if address == 0x6000:
            self.registers[0x3002] = ((self.registers[0x3002] | 1) & ~2
                if words[0] else self.registers[0x3002] & ~1)
        elif address == 0x6001:
            self.registers[0x3002] &= ~0x1ff0
        elif address == 0x8000:
            self.unit = words[0]
        elif address not in (0x7000, 0x7001):
            self.registers.update({address + i: word for i, word in enumerate(words)})
        return pdu[:5]


@contextmanager
def tcp_dummy(dummy = None):
    dummy = dummy or Dummy()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            while True:
                header = receive(self.request, 7)
                if header is None:
                    return
                transaction, protocol, length, unit = struct.unpack('>HHHB', header)
                pdu = receive(self.request, length - 1)
                if pdu is None:
                    return
                response = dummy.reply(unit, pdu)
                fault, dummy.fault = dummy.fault, None
                if response is None or fault == 'drop':
                    return
                if fault == 'wrong_write':
                    response = response[:1] + b'\x00\x00\x00\x01'
                if fault == 'count':
                    response = b'\x03\x00'
                if fault == 'transaction':
                    transaction += 1
                frame = struct.pack('>HHHB', transaction, protocol, len(response) + 1, unit) + response
                # Deliberately fragment the frame to exercise exact receive loops.
                self.request.sendall(frame[:4])
                self.request.sendall(frame[4:])

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target = server.serve_forever, kwargs = {'poll_interval': 0.01}, daemon = True)
    thread.start()
    try:
        yield dummy, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout = 2)
