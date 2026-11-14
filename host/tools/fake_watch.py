"""Stand-in nurse watch for manual tests: accepts every dispatch, walks into the room, completes."""
import json, sys, threading, time
import paho.mqtt.client as mqtt
S, W = 'gx', sys.argv[1] if len(sys.argv) > 1 else 'w-01'
HOST = sys.argv[2] if len(sys.argv) > 2 else '127.0.0.1'
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='fake-' + W)
def env(t, payload=None, call_id=None):
    import uuid
    return json.dumps(dict(msg_id=uuid.uuid4().hex, store_id=S, device_id=W, type=t, payload=payload or {}, call_id=call_id))
def work(m):
    time.sleep(3); c.publish(f'{S}/ack/{m["call_id"]}', env('CALL_ACCEPT', call_id=m['call_id']), qos=1); print('accepted', m['call_id'][:8], m['payload'].get('label'), m['payload'].get('voice_url'))
    time.sleep(3); c.publish(f'{S}/location/{W}', env('LOCATION_REPORT', dict(readings=[dict(beacon_id='bcn-' + m['payload']['room'], rssi=-52)])))
    time.sleep(3); c.publish(f'{S}/ack/{m["call_id"]}', env('CALL_COMPLETE', dict(method='onsite', reason='已处理'), m['call_id']), qos=1); print('completed')
def on_msg(cl, u, msg):
    m = json.loads(msg.payload)
    if m['type'] == 'CALL_DISPATCH': threading.Thread(target=work, args=(m,), daemon=True).start()
c.on_message = on_msg
c.connect(HOST); c.subscribe(f'{S}/dispatch/{W}', qos=1); c.loop_start()
while True:
    c.publish(f'{S}/heartbeat/{W}', env('HEARTBEAT', dict(battery=90)), qos=0); time.sleep(30)
