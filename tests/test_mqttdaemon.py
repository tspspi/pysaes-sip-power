import json
import socketserver
import struct
import threading
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from saessippower.mqttdaemon import load_config, measurement_payload, publish_measurement, run
from saessippower import SIPPowerCommunicationError
from dummy import tcp_dummy, receive


CONFIG = {
    'mqtt_host': '127.0.0.1', 'mqtt_user': 'dummy', 'mqtt_password': 'dummy',
    'gauge': 'sippower', 'gauge_name': 'test-pump', 'device_host': '127.0.0.1',
    'base_topic': '/test', 'pressure_topic': '/test/status/pressure/pump',
    'status_topic': '/test/status/ionpump/pump', 'interval_seconds': 1
}


class PublishInfo:
    rc = 0
    def wait_for_publish(self, timeout):
        pass
    def is_published(self):
        return True


class Client:
    def __init__(self, connected):
        self.connected = connected
        self.messages = []
    def connect(self, *args, **kwargs):
        self.connected.set()
    def loop_start(self):
        pass
    def publish(self, topic, payload, qos, retain):
        self.messages.append((topic, json.loads(payload), qos, retain))
        return PublishInfo()
    def disconnect(self):
        pass
    def loop_stop(self):
        pass


def test_private_config_and_validation(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(CONFIG))
    path.chmod(0o644)
    with pytest.raises(ValueError, match = '0600'):
        load_config(path)
    path.chmod(0o600)
    assert load_config(path)['gauge_name'] == 'test-pump'
    bad = [dict(CONFIG, channel = 2), dict(CONFIG, modbus_unit = 0),
        dict(CONFIG, interval_seconds = float('nan')), dict(CONFIG, timeout_seconds = float('inf')),
        dict(CONFIG, pressure_topic = '/other/pressure'), dict(CONFIG, serial_port = '/dummy'),
        dict(CONFIG, status_topic = '/test/#'), dict(CONFIG, base_topic = '/test/+'),
        dict(CONFIG, mqtt_port = True), dict(CONFIG, status_topic = CONFIG['pressure_topic'])]
    for config in bad:
        path.write_text(json.dumps(config))
        with pytest.raises(ValueError):
            load_config(path)
    serial = dict(CONFIG, serial_port = '/dummy')
    del serial['device_host']
    path.write_text(json.dumps(serial))
    assert load_config(path)['serial_port'] == '/dummy'


@pytest.mark.parametrize('pressure,valid', [(None, False), (0, True), (-1, True),
    (float('nan'), True), (1e-7, False), (True, True)])
def test_invalid_measurement_never_published(pressure, valid):
    client = Client(threading.Event())
    with pytest.raises(ValueError):
        publish_measurement(client, CONFIG, {'pressure_mbar': pressure, 'ts': 1, 'responseValid': valid})
    assert client.messages == []


def test_payload_matches_xgs600_schema_and_puback_required():
    measurement = {'pressure_mbar': 1e-7, 'ts': 10.0, 'responseValid': True}
    assert json.loads(measurement_payload(measurement)) == {'pressure_mbar': 1e-7, 'timestamp': 10.0}
    client = Client(threading.Event())
    with patch.object(PublishInfo, 'is_published', return_value = False):
        with pytest.raises(TimeoutError, match = 'PUBACK'):
            publish_measurement(client, CONFIG, measurement)
    assert client.messages[0][2:] == (1, False)


def test_read_failure_publishes_no_stale_reading():
    with tcp_dummy() as (dummy, port):
        dummy.fault = 'drop'
        client = Client(threading.Event())
        def factory(config, connected):
            client.connected = connected
            return client
        with patch('saessippower.mqttdaemon.make_client', side_effect = factory):
            with pytest.raises(SIPPowerCommunicationError):
                run(dict(CONFIG, modbus_port = port), threading.Event(), once = True)
        assert client.messages == []
        assert dummy.writes == []


def test_stopped_pump_publishes_status_only():
    with tcp_dummy() as (dummy, port):
        dummy.registers[0x3002] = 0
        clients = []
        def factory(config, connected):
            client = Client(connected)
            clients.append(client)
            return client
        with patch('saessippower.mqttdaemon.make_client', side_effect = factory):
            with pytest.raises(ValueError, match = 'No valid pressure'):
                run(dict(CONFIG, modbus_port = port), threading.Event(), once = True)
        assert [m[0] for m in clients[0].messages] == [CONFIG['status_topic']]
        assert clients[0].messages[0][1]['pressure_mbar'] is None
        assert dummy.writes == []


def test_failed_connection_reconnects_without_device_writes():
    stop = threading.Event()
    clients = []
    with tcp_dummy() as (dummy, port):
        dummy.fault = 'drop'
        def factory(config, connected):
            client = Client(connected)
            if clients:
                original = client.publish
                def publish(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if args[0] == CONFIG['pressure_topic']:
                        stop.set()
                    return result
                client.publish = publish
            clients.append(client)
            return client
        with patch('saessippower.mqttdaemon.make_client', side_effect = factory):
            run(dict(CONFIG, modbus_port = port, interval_seconds = 0.01), stop)
        assert len(clients) == 2
        assert clients[0].messages == []
        assert clients[1].messages[-1][0] == CONFIG['pressure_topic']
        assert dummy.writes == []


@contextmanager
def mqtt_dummy():
    """Minimal local MQTT 3.1.1 broker for real Paho CONNECT/PUBLISH/PUBACK."""
    messages = []
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            while True:
                first = receive(self.request, 1)
                if first is None:
                    return
                size, multiplier = 0, 1
                while True:
                    chunk = receive(self.request, 1)
                    if chunk is None:
                        return
                    size += (chunk[0] & 127) * multiplier
                    multiplier *= 128
                    if not chunk[0] & 128:
                        break
                body = receive(self.request, size)
                if body is None:
                    return
                kind = first[0] >> 4
                if kind == 1:
                    self.request.sendall(b'\x20\x02\x00\x00')
                elif kind == 3:
                    topicSize = struct.unpack('>H', body[:2])[0]
                    topic = body[2:2 + topicSize].decode()
                    offset = 2 + topicSize
                    messageID = body[offset:offset + 2]
                    messages.append((topic, json.loads(body[offset + 2:]), first[0]))
                    self.request.sendall(b'\x40\x02' + messageID)
                elif kind == 12:
                    self.request.sendall(b'\xd0\x00')
                elif kind == 14:
                    return
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True
    server = Server(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target = server.serve_forever, kwargs = {'poll_interval': 0.01}, daemon = True)
    thread.start()
    try:
        yield messages, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout = 2)


def test_end_to_end_modbus_to_real_paho_qos1_local_broker():
    with tcp_dummy() as (dummy, modbusPort), mqtt_dummy() as (messages, mqttPort):
        run(dict(CONFIG, modbus_port = modbusPort, mqtt_port = mqttPort), threading.Event(), once = True)
        assert len(messages) == 2
        statusTopic, status, statusFlags = messages[0]
        topic, pressure, flags = messages[1]
        assert statusTopic == CONFIG['status_topic']
        assert status['channel'] == 1
        assert status['status']['enabled']
        assert topic == CONFIG['pressure_topic']
        assert set(pressure) == {'pressure_mbar', 'timestamp'}
        assert pressure['pressure_mbar'] == pytest.approx(1.333223684e-6)
        assert flags == statusFlags == 0x32  # QoS 1, retain false
        assert dummy.writes == []


def test_paho_publish_runtime_error_normalized_for_reconnect():
    client = Client(threading.Event())
    with patch.object(PublishInfo, 'wait_for_publish', side_effect = RuntimeError('connection lost')):
        with pytest.raises(OSError, match = 'acknowledgement'):
            publish_measurement(client, CONFIG, {'pressure_mbar': 1e-7, 'ts': 10, 'responseValid': True})
