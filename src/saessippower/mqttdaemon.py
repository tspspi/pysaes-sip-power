"""Read-only SIP POWER MQTT publisher using the XGS-600 pressure schema."""

import argparse
import json
import logging
import math
import os
import re
import signal
import stat
import threading
import time

import paho.mqtt.client as mqtt

from .sippower import SIPPower
from .transport import SIPPowerCommunicationError, integer, timeout_value


DEFAULT_CONFIG = os.path.expanduser("~/.config/sippowermqtt.conf")
LOG = logging.getLogger("sippowermqtt")


def load_config(path):
    with open(path, "r", encoding = "utf-8") as configFile:
        fileStat = os.fstat(configFile.fileno())
        if not stat.S_ISREG(fileStat.st_mode) or stat.S_IMODE(fileStat.st_mode) & 0o077:
            raise ValueError("MQTT config must be a private regular file (mode 0600)")
        config = json.load(configFile)
    if not isinstance(config, dict):
        raise ValueError("MQTT config must be a JSON object")
    for key in ("mqtt_host", "mqtt_user", "mqtt_password", "gauge_name", "base_topic", "pressure_topic"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError(f"{key} must be a nonempty string")
    if config.get("gauge", "sippower") != "sippower":
        raise ValueError("Only gauge type sippower is supported")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", config["gauge_name"]):
        raise ValueError("gauge_name must contain only letters, digits, underscores, and hyphens")
    integer(config.get("mqtt_port", 1883), "MQTT port", 1, 65535)
    integer(config.get("modbus_port", 502), "Modbus TCP port", 1, 65535)
    integer(config.get("modbus_unit", 11), "Modbus unit", 1, 247)
    integer(config.get("channel", 1), "SIP POWER channel", 1, 1)
    timeout_value(config.get("timeout_seconds", 2.0))
    integer(config.get("baudrate", 38400), "Baud rate", 1, 1000000)
    interval = config.get("interval_seconds", 10)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 1 <= interval <= 60:
        raise ValueError("interval_seconds must be between 1 and 60")
    if (config.get("device_host") is None) == (config.get("serial_port") is None):
        raise ValueError("Supply exactly one of device_host or serial_port")
    for key in ("device_host", "serial_port"):
        if key in config and config[key] is not None and (not isinstance(config[key], str) or not config[key]):
            raise ValueError(f"{key} must be a nonempty string")
    base = config["base_topic"].rstrip("/")
    if not base or any(c in config["base_topic"] for c in ("#", "+", "\x00")):
        raise ValueError("base_topic must be nonempty and cannot contain MQTT wildcards or NUL")
    for key in ("pressure_topic", "status_topic"):
        if key == "status_topic" and key not in config:
            continue
        topic = config[key]
        if not isinstance(topic, str) or not topic.startswith(base + "/"):
            raise ValueError(f"{key} must be below base_topic")
        if any(c in topic for c in ("#", "+", "\x00")) or len(topic.encode("utf-8")) > 65535:
            raise ValueError(f"{key} must be a valid MQTT publication topic")
    if config.get("status_topic") == config["pressure_topic"]:
        raise ValueError("status_topic and pressure_topic must differ")
    return config


def measurement_payload(measurement):
    pressure = measurement["pressure_mbar"]
    timestamp = measurement["ts"]
    if (not measurement.get("responseValid", False) or pressure is None
            or isinstance(pressure, bool) or not isinstance(pressure, (int, float))
            or not math.isfinite(pressure) or pressure <= 0):
        raise ValueError("Pressure is unavailable; no pressure message will be published")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        raise ValueError("Invalid measurement timestamp")
    return json.dumps({"pressure_mbar": pressure, "timestamp": timestamp},
        separators = (",", ":"), allow_nan = False)


def make_client(config, connected):
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
        client_id = "sippowermqtt-" + config["gauge_name"], protocol = mqtt.MQTTv311)
    client.username_pw_set(config["mqtt_user"], config["mqtt_password"])
    client.reconnect_delay_set(min_delay = 1, max_delay = 30)
    # Bound Paho's QoS queue so an unavailable broker cannot accumulate readings.
    client.max_queued_messages_set(2)

    def on_connect(client, userdata, flags, reasonCode, properties):
        if reasonCode == 0:
            connected.set()
        else:
            connected.clear()
            LOG.error("MQTT connection rejected: %s", reasonCode)

    def on_disconnect(client, userdata, flags, reasonCode, properties):
        connected.clear()
        if reasonCode != 0:
            LOG.warning("MQTT connection lost: %s", reasonCode)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    return client


def _publish(client, topic, payload):
    info = client.publish(topic, payload, qos = 1, retain = False)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise OSError(f"MQTT publish failed with code {info.rc}")
    try:
        info.wait_for_publish(timeout = 10)
    except RuntimeError as error:
        raise OSError("MQTT publication failed while waiting for acknowledgement") from error
    if not info.is_published():
        raise TimeoutError("MQTT PUBACK was not received within 10 seconds")


def publish_measurement(client, config, measurement):
    payload = measurement_payload(measurement)
    _publish(client, config["pressure_topic"], payload)
    return payload


def make_pump(config):
    return SIPPower(host = config.get("device_host"), port = config.get("modbus_port", 502),
        unit = config.get("modbus_unit", 11), timeout = config.get("timeout_seconds", 2.0),
        serial_port = config.get("serial_port"), baudrate = config.get("baudrate", 38400))


def run(config, stopEvent, once = False):
    interval = config.get("interval_seconds", 10)
    while not stopEvent.is_set():
        connected = threading.Event()
        client = make_client(config, connected)
        try:
            with make_pump(config) as pump:
                client.connect(config["mqtt_host"], config.get("mqtt_port", 1883), keepalive = 120)
                client.loop_start()
                if not connected.wait(10):
                    raise TimeoutError("MQTT connection was not accepted within 10 seconds")
                while not stopEvent.is_set():
                    started = time.monotonic()
                    if not connected.is_set():
                        raise ConnectionError("MQTT broker is disconnected")
                    measurement = pump.get_measurement(channel = config.get("channel", 1))
                    if "status_topic" in config:
                        _publish(client, config["status_topic"], json.dumps(measurement,
                            separators = (",", ":"), allow_nan = False))
                    if measurement["responseValid"]:
                        payload = publish_measurement(client, config, measurement)
                        LOG.info("Published %s to %s", payload, config["pressure_topic"])
                    else:
                        LOG.warning("Pressure unavailable: enabled=%s alarms=%s",
                            measurement["status"]["enabled"], measurement["status"]["alarms"])
                        if once:
                            raise ValueError("No valid pressure reading available")
                    if once:
                        return
                    stopEvent.wait(max(0, interval - (time.monotonic() - started)))
        except (SIPPowerCommunicationError, OSError) as error:
            # Recreate both connections on failure. Never use a cached pressure.
            LOG.warning("Connection/read unavailable: %s", error)
            if once:
                raise
            stopEvent.wait(interval)
        finally:
            try:
                client.disconnect()
            finally:
                client.loop_stop()


def main(argv = None):
    parser = argparse.ArgumentParser(description = "Publish SIP POWER estimated pressure to MQTT")
    parser.add_argument("--config", default = DEFAULT_CONFIG, help = "Private JSON config path")
    parser.add_argument("--once", action = "store_true", help = "publish one reading and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level = logging.INFO, format = "%(asctime)s %(levelname)s %(message)s")
    stopEvent = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stopEvent.set())
    signal.signal(signal.SIGINT, lambda signum, frame: stopEvent.set())
    run(load_config(args.config), stopEvent, once = args.once)


if __name__ == "__main__":
    main()
