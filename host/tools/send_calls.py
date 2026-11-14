"""Field-test helper: act as a room tablet and send N calls (e.g. 100 pushes to a watch).
  python tools/send_calls.py <broker> --device tab-1601 --room 1601 --floor 16 [--count 100] [--interval 1]
                             [--service NEED_NURSE] [--user tab-1601 --password X] [--store gx]
Count the cards/vibrations on the watch afterwards (every call must arrive exactly once)."""
import argparse, json, time, uuid
import paho.mqtt.client as mqtt

ap = argparse.ArgumentParser()
ap.add_argument('broker'); ap.add_argument('--port', type=int, default=1883); ap.add_argument('--store', default='gx')
ap.add_argument('--device', required=True); ap.add_argument('--room', required=True); ap.add_argument('--floor', required=True)
ap.add_argument('--count', type=int, default=1); ap.add_argument('--interval', type=float, default=1.0)
ap.add_argument('--service', default='NEED_NURSE'); ap.add_argument('--user'); ap.add_argument('--password')
a = ap.parse_args()
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='sendcalls-' + a.device)
if a.user:
    c.username_pw_set(a.user, a.password)
c.connect(a.broker, a.port); c.loop_start()
for i in range(a.count):
    m = dict(msg_id=uuid.uuid4().hex, store_id=a.store, device_id=a.device, type='CALL_REQUEST',
             payload=dict(room=a.room, service_code=a.service, text=f'测试 {i + 1}/{a.count}'))
    c.publish(f'{a.store}/call/{a.floor}/{a.room}', json.dumps(m, ensure_ascii=False), qos=1).wait_for_publish()
    print(f'sent {i + 1}/{a.count}', flush=True)
    time.sleep(a.interval)
c.loop_stop()
