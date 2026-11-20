"""Field-test monitor: shows every heartbeat / location / vitals message and flags gaps.
  python tools/watch_monitor.py <broker> [--store gx] [--user host --password X] [--csv out.csv]
Use the `host` account on a production broker. Ctrl-C prints a summary (count, longest gap) per device.
Expected rhythm: location every ~10 s, heartbeat every 30 s. A gap > 3x the rhythm is flagged."""
import argparse, csv, json, time
import paho.mqtt.client as mqtt

EXPECT = {'location': 10, 'heartbeat': 30, 'vitals': 300}
ap = argparse.ArgumentParser()
ap.add_argument('broker'); ap.add_argument('--port', type=int, default=1883); ap.add_argument('--store', default='gx')
ap.add_argument('--user'); ap.add_argument('--password'); ap.add_argument('--csv')
a = ap.parse_args()
last, stats = {}, {}
out = open(a.csv, 'w', newline='', encoding='utf-8') if a.csv else None
w = csv.writer(out) if out else None
if w:
    w.writerow(['time', 'device', 'kind', 'battery', 'charging', 'beacons(id:rssi)', 'heart_rate'])


def on_message(c, u, msg):
    kind, dev = msg.topic.split('/')[1:3]
    try:
        p = json.loads(msg.payload).get('payload', {})
    except ValueError:
        return
    now = time.time()
    key = (dev, kind)
    gap = now - last[key] if key in last else 0
    last[key] = now
    st = stats.setdefault(key, [0, 0.0]); st[0] += 1; st[1] = max(st[1], gap)
    t = time.strftime('%H:%M:%S')
    beacons = ' '.join(f"{r['beacon_id']}:{r['rssi']}" for r in p.get('readings', []))
    flag = f'  <<< 间隔 {gap:.0f}s' if gap > 3 * EXPECT[kind] else ''
    detail = beacons or (f"电量{p.get('battery')}% {'充电中' if p.get('charging') else ''}" if kind == 'heartbeat' else f"心率{p.get('heart_rate')}")
    print(f'{t} {dev:8} {kind:9} {detail}{flag}', flush=True)
    if w:
        w.writerow([t, dev, kind, p.get('battery', ''), p.get('charging', ''), beacons, p.get('heart_rate', '')]); out.flush()


c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='watch-monitor')
if a.user:
    c.username_pw_set(a.user, a.password)
c.on_message = on_message
c.connect(a.broker, a.port)
for k in EXPECT:
    c.subscribe(f'{a.store}/{k}/+')
try:
    c.loop_forever()
except KeyboardInterrupt:
    print('\n--- 汇总(设备 类型 条数 最长间隔秒)---')
    for (dev, kind), (n, g) in sorted(stats.items()):
        print(f'{dev:8} {kind:9} {n:5} {g:6.0f}')
