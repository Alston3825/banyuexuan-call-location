"""Smoke test against a real broker: tablet -> host -> watch accept -> receipt on tablet."""
import json, os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import paho.mqtt.client as mqtt
from bycall.core import protocol as P
from bycall.core.config import StoreConfig
from bycall.core.service import HostService
from bycall.gateway.bus import MqttBus

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 1883
S = 'gx'
cfg = StoreConfig.load(os.path.join(os.path.dirname(__file__), '..', 'bycall', 'custom', 'banyuexuan', 'demo_pilot.json'))
bus = MqttBus('127.0.0.1', PORT, client_id='smoke-host')
svc = HostService(cfg, bus, os.path.join(tempfile.mkdtemp(), 'h.db'))
bus.start()

got = []
def client(name):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=name)
    c.connect('127.0.0.1', PORT); c.loop_start(); return c
tablet, watch = client('tab-1601'), client('w-01')
tablet.subscribe(f'{S}/feedback/1601', qos=1)
def on_watch(c, u, m):
    msg = json.loads(m.payload)
    if msg['type'] == P.CALL_DISPATCH:
        ack = P.Envelope(store_id=S, device_id='w-01', type=P.CALL_ACCEPT, call_id=msg['call_id']).model_dump()
        c.publish(P.t_ack(S, msg['call_id']), json.dumps(ack), qos=1)
watch.on_message = on_watch
watch.subscribe(f'{S}/dispatch/w-01', qos=1)
tablet.on_message = lambda c, u, m: got.append(json.loads(m.payload)['payload']['status'])
time.sleep(1)
hb = P.Envelope(store_id=S, device_id='w-01', type=P.HEARTBEAT, payload=dict(battery=88)).model_dump()
watch.publish(P.t_heartbeat(S, 'w-01'), json.dumps(hb), qos=0)
time.sleep(0.5)
t0 = time.time()
req = P.Envelope(store_id=S, device_id='tab-1601', type=P.CALL_REQUEST, payload=dict(room='1601', service_code='MILK_STORAGE')).model_dump()
tablet.publish(P.t_call(S, '16', '1601'), json.dumps(req), qos=1)
while 'accepted' not in got and time.time() - t0 < 5:
    time.sleep(0.02)
print('statuses:', got, 'round-trip %.0f ms' % ((time.time() - t0) * 1000))
assert got == ['received', 'accepted'], got
print('OK')
