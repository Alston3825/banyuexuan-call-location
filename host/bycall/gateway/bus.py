"""Transport abstraction: in-memory bus for tests/simulation, MQTT for production."""
from __future__ import annotations

import json
import re
from typing import Callable, List, Tuple


def topic_match(pattern: str, topic: str) -> bool:
    p, t = pattern.split('/'), topic.split('/')
    for i, seg in enumerate(p):
        if seg == '#':
            return True
        if i >= len(t) or (seg != '+' and seg != t[i]):
            return False
    return len(p) == len(t)


class InMemoryBus:
    """Synchronous pub/sub. Delivery order = publish order (queued, no recursion)."""

    def __init__(self):
        self.subs: List[Tuple[str, Callable]] = []
        self.log: List[Tuple[str, dict]] = []
        self._queue, self._busy = [], False

    def subscribe(self, pattern, handler):
        self.subs.append((pattern, handler))

    def publish(self, topic, message: dict):
        self.log.append((topic, message))
        self._queue.append((topic, message))
        if self._busy:
            return
        self._busy = True
        try:
            while self._queue:
                tp, msg = self._queue.pop(0)
                for pat, h in list(self.subs):
                    if topic_match(pat, tp):
                        h(tp, msg)
        finally:
            self._busy = False

    def sent(self, pattern, type_=None):
        return [m for t, m in self.log if topic_match(pattern, t)
                and (type_ is None or m.get('type') == type_)]


class MqttBus:
    """paho-mqtt adapter (QoS 1 for call traffic, QoS 0 for heartbeat/location)."""

    def __init__(self, host, port=1883, username=None, password=None, client_id='bycall-host'):
        import paho.mqtt.client as mqtt
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username:
            self.client.username_pw_set(username, password)
        self.handlers: List[Tuple[str, Callable]] = []
        self.client.on_message = self._on_message
        self.client.on_connect = self._on_connect
        self._host, self._port = host, port

    def _on_connect(self, client, userdata, flags, rc, props=None):
        for pat, _ in self.handlers:           # resubscribe after reconnect
            client.subscribe(pat, qos=1)

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode('utf-8'))
        except ValueError:
            return
        for pat, h in self.handlers:
            if topic_match(pat, msg.topic):
                h(msg.topic, data)

    def subscribe(self, pattern, handler):
        self.handlers.append((pattern, handler))
        if self.client.is_connected():
            self.client.subscribe(pattern, qos=1)

    def publish(self, topic, message: dict):
        qos = 0 if re.search(r'/(heartbeat|location)/', topic) else 1
        self.client.publish(topic, json.dumps(message, ensure_ascii=False), qos=qos)

    def connected(self):
        return self.client.is_connected()

    def start(self):
        self.client.connect(self._host, self._port, keepalive=30)
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()
