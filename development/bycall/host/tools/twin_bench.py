"""双屏/3D 对接台架联调：扮演手表与客房平板，经真实 MQTT → 主机 → 数字孪生网关，逐项核对 T1–T9。

只用于台架虚构数据，不要对现场主机运行。
  python tools/twin_bench.py --broker 127.0.0.1 --host http://127.0.0.1:8100 --admin-key K \
      --gateway http://127.0.0.1:8300 [--only T1,T2] [--restart-cmd "..."]
需要台架配置：房间 2407–2429（楼层 24）、信标 24-1/24-2/24-3/24-4（有锚点）与 24-9（无锚点）、
员工 n01/n02（护理师）、m01/c01/f01（送餐/保洁/前台）、备用表 w-09。
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request
import uuid

import paho.mqtt.client as mqtt

S = 'gx'
WATCHES = ['w-01', 'w-02', 'w-03', 'w-04', 'w-05', 'w-06']
RESULTS = []


class Bench:
    def __init__(self, a):
        self.a = a
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='twin-bench-' + uuid.uuid4().hex[:6])
        self.c.connect(a.broker, 1883)
        self.c.loop_start()

    def pub(self, topic, dev, type_, payload=None, call_id=None):
        m = dict(msg_id=uuid.uuid4().hex, store_id=S, device_id=dev, type=type_, payload=payload or {}, call_id=call_id)
        self.c.publish(topic, json.dumps(m, ensure_ascii=False), qos=1).wait_for_publish()

    def heartbeat(self, *watches):
        for w in watches or WATCHES:
            self.pub(f'{S}/heartbeat/{w}', w, 'HEARTBEAT', dict(battery=88))

    def locate(self, w, beacon, times=3, gap=0.4):
        for _ in range(times):  # 主机三报告滑动窗口
            self.pub(f'{S}/location/{w}', w, 'LOCATION_REPORT',
                     dict(readings=[dict(beacon_id=beacon, rssi=-55, sample_count=4)], report_interval_s=10,
                          scan_status='ok', observation_source='ble'))
            time.sleep(gap)

    def call(self, room, service='FEED'):
        self.pub(f'{S}/call/24/{room}', f'tab-{room}', 'CALL_REQUEST', dict(room=room, service_code=service))

    def ack(self, w, cid, type_, payload=None):
        self.pub(f'{S}/ack/{cid}', w, type_, payload, call_id=cid)

    def http(self, url, key=None, body=None):
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json', **({'X-Api-Key': key} if key else {})})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def gw(self):
        return self.http(self.a.gateway + '/api/twin/state')

    def station(self):
        return self.http(self.a.host + '/station/snapshot', self.a.admin_key)

    def wait(self, pred, timeout=15, step=0.25):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                v = pred()
                if v:
                    return v, time.monotonic() - t0
            except Exception:  # noqa: BLE001
                pass
            time.sleep(step)
        return None, time.monotonic() - t0

    def gw_call(self, room, states=None):
        s = self.gw()
        for c in (s.get('calls') or {}).get('items', []):
            if c['room'] == room and (states is None or c['state'] in states):
                return c
        return None

    def gw_person(self, staff):
        s = self.gw()
        return next((i for i in (s.get('locations') or {}).get('items', []) if i['staff'] == staff), None)


def check(tid, name, ok, detail):
    RESULTS.append(dict(id=tid, name=name, ok=bool(ok), detail=detail))
    print(f"{'PASS' if ok else 'FAIL'} {tid} {name}: {detail}", flush=True)


def same_fact(b, staff):
    """文字屏与 3D 网关在同一 sequence 下应给出同一事实。"""
    for _ in range(20):
        st, g = b.station()['location_state'], b.gw()['locations']
        if st['source_epoch'] == g['source_epoch'] and st['sequence'] == g['sequence']:
            a = next(i for i in st['items'] if i['staff'] == staff)
            c = next(i for i in g['items'] if i['staff'] == staff)
            keys = ('location_status', 'zone_id', 'area', 'room', 'floor', 'valid_until')
            return st['revision'] == g['revision'] and all(a[k] == c[k] for k in keys), st['sequence']
        time.sleep(0.5)
    return False, None


def run(a):
    b = Bench(a)
    only = set(a.only.split(',')) if a.only else None
    want = lambda t: not only or t in only  # noqa: E731
    b.heartbeat()
    time.sleep(1)

    if want('T1'):
        t0 = time.monotonic()
        b.call('2414', 'FEED')
        c, dt = b.wait(lambda: b.gw_call('2414', {'pending'}))
        total = time.monotonic() - t0
        st_has = c and any(x['call_id'] == c['call_id'] for x in b.station()['active'])
        check('T1', '新呼叫两屏可见', c and st_has and c['label'] == '宝宝喂奶' and total <= 4.0,
              f"3D 网关可见耗时 {total:.1f}s；label={c and c['label']}；文字屏同一 call_id={bool(st_has)}")
        b.t1 = c

    if want('T2'):
        c = getattr(b, 't1', None) or (b.call('2414', 'FEED') or b.wait(lambda: b.gw_call('2414', {'pending'}))[0])
        cid = c['call_id']
        seen = []
        b.ack('w-01', cid, 'CALL_ACCEPT')
        x, _ = b.wait(lambda: b.gw_call('2414', {'claimed'})); seen.append(x and (x['state'], x['owner_name']))
        b.ack('w-01', cid, 'CALL_ARRIVE')
        x, _ = b.wait(lambda: b.gw_call('2414', {'arrived'})); seen.append(x and x['state'])
        b.ack('w-01', cid, 'CALL_COMPLETE', dict(method='onsite', reason='已处理'))
        x, _ = b.wait(lambda: b.gw_call('2414', {'completed'})); seen.append(x and x['state'])
        st = b.station()
        in_recent = any(r['call_id'] == cid and r['state'] == 'completed' for r in st['recent'])
        check('T2', '接单→到场→完成顺序一致', seen[0] and seen[0][0] == 'claimed' and seen[1:] == ['arrived', 'completed'] and in_recent,
              f'3D 依次看到 {seen}；文字屏最近完成={in_recent}')

    if want('T3'):
        b.call('2415', 'DIAPER')
        c, _ = b.wait(lambda: b.gw_call('2415', {'pending'}))
        b.ack('w-02', c['call_id'], 'CALL_ACCEPT')
        b.wait(lambda: b.gw_call('2415', {'claimed'}))
        b.ack('w-02', c['call_id'], 'CALL_TRANSFER', dict(target='n01', reason='忙'))
        x, _ = b.wait(lambda: (lambda y: y if y and y['transfer_pending'] else None)(b.gw_call('2415')))
        b.call('2412', 'BABY_CRY')
        y, _ = b.wait(lambda: b.gw_call('2412', {'pending'}))
        b.pub(f"{S}/ack/{y['call_id']}", 'tab-2412', 'CALL_CANCEL', call_id=y['call_id'])
        z, _ = b.wait(lambda: b.gw_call('2412', {'cancelled'}))
        b.call('2420', 'EMERGENCY')
        e, _ = b.wait(lambda: b.gw_call('2420', {'pending'}))
        check('T3', '转交/取消/紧急可见', x and x['transferred'] and z and e and e['priority'] == 'emergency',
              f"转交中={bool(x)}；取消={bool(z)}；紧急 priority={e and e['priority']}")

    if want('T4'):
        b.heartbeat()
        for w, bc in (('w-01', '24-1'), ('w-02', '24-3'), ('w-04', '24-4'), ('w-06', '24-2')):
            b.locate(w, bc)
        p, dt = b.wait(lambda: (lambda i: i if i and i['location_status'] == 'fresh' and i['zone_id'] == '24-1' else None)(b.gw_person('n01')))
        ok, seq = same_fact(b, 'n01')
        roles = {i['staff']: (i.get('display_name'), i.get('role')) for i in b.gw()['locations']['items']}
        check('T4', '区域定位两屏同一事实', p and ok and roles.get('m01', (0, 0))[1] == 'meal' and roles.get('f01', (0, 0))[1] == 'front_desk',
              f"n01 区域={p and p['area']}；两屏 sequence={seq} revision 一致={ok}；姓名/岗位={roles.get('n01')}")

    if want('T5'):
        b.locate('w-01', '24-2', times=4)
        p, dt = b.wait(lambda: (lambda i: i if i and i['zone_id'] == '24-2' else None)(b.gw_person('n01')), timeout=20)
        ok, seq = same_fact(b, 'n01')
        check('T5', '跨区域切换', p and ok, f"n01 → {p and p['area']}（{dt:.1f}s 内网关可见）；两屏一致={ok}")

    if want('T6'):
        b.heartbeat()
        b.locate('w-05', '24-3')
        b.wait(lambda: (lambda i: i and i['location_status'] == 'fresh')(b.gw_person('c01')))
        print('  T6 等待 c01 位置过期（停止上报 32 秒，保持心跳）…', flush=True)
        for _ in range(4):
            time.sleep(8); b.heartbeat()
        p = b.gw_person('c01')
        st = next(i for i in b.station()['location_state']['items'] if i['staff'] == 'c01')
        check('T6', '位置过期两屏都不再显示实时位置', p['location_status'] == 'stale' and p['zone_id'] is None and st['zone_id'] is None,
              f"3D={p['location_status']}/{p['location_reason']}；文字屏={st['location_status']}；zone_id 均为空")

    if want('T7'):
        b.locate('w-02', '24-3')
        b.wait(lambda: (lambda i: i and i['location_status'] == 'fresh')(b.gw_person('n02')))
        before = b.gw_person('n02')
        b.heartbeat('w-09'); time.sleep(1)  # 主机要求备用表在线且已报电量
        b.http(a.host + '/swap', a.admin_key, dict(staff='n02', sn='w-09', old_state='CHARGING'))
        p, _ = b.wait(lambda: (lambda i: i if i and i['watch'] == 'w-09' else None)(b.gw_person('n02')))
        check('T7', '换表后仍是同一人物、旧表位置不带过来', p and p['staff'] == 'n02' and p['location_status'] != 'fresh',
              f"换表前 {before['watch']}/{before['location_status']}，换表后 {p and p['watch']}/{p and p['location_status']}/{p and p['location_reason']}")
        b.heartbeat('w-09'); b.locate('w-09', '24-3')
        q, _ = b.wait(lambda: (lambda i: i if i and i['location_status'] == 'fresh' else None)(b.gw_person('n02')))
        check('T7b', '备用表上报后恢复位置', q and q['display_name'], f"n02={q and (q['watch'], q['area'], q['display_name'])}")

    if want('T9'):
        b.locate('w-03', '24-9')
        p, _ = b.wait(lambda: (lambda i: i if i and i['zone_id'] == '24-9' else None)(b.gw_person('n03')))
        h = None
        try:
            b.http(a.gateway + '/api/twin/health')
        except urllib.error.HTTPError as e:
            h = json.loads(e.read())
        check('T9', '映射缺失被报出、不乱放', p and h and '24-9' in h['mapping']['missing_zones'],
              f"主机仍判 fresh={p and p['location_status']}；网关 health 503，missing_zones={h and h['mapping']['missing_zones']}")
        b.locate('w-03', '24-3')

    if want('T8') and a.restart_cmd:
        before = b.gw()['locations']['source_epoch']
        subprocess.run(a.restart_cmd, shell=True, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        down, dt_down = b.wait(lambda: b.gw()['gateway']['upstream'] == 'down', timeout=10)
        back, dt_back = b.wait(lambda: (lambda g: g['gateway']['upstream'] == 'ok' and g['locations']['source_epoch'] != before)(b.gw()), timeout=40)
        after = b.gw()
        n01 = next(i for i in after['locations']['items'] if i['staff'] == 'n01')
        check('T8', '主机重启：网关报中断，恢复后新会话且不回放旧位置', down and back and n01['location_status'] != 'fresh',
              f"中断 {dt_down:.1f}s 内被网关发现；恢复用时 {dt_back:.1f}s；新 epoch≠旧；n01 重启后={n01['location_status']}/{n01['location_reason']}")
    b.c.loop_stop()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--broker', default='127.0.0.1'); ap.add_argument('--host', default='http://127.0.0.1:8100')
    ap.add_argument('--admin-key', required=True); ap.add_argument('--gateway', default='http://127.0.0.1:8300')
    ap.add_argument('--only'); ap.add_argument('--restart-cmd'); ap.add_argument('--json-out')
    a = ap.parse_args()
    run(a)
    if a.json_out:
        json.dump(RESULTS, open(a.json_out, 'w'), ensure_ascii=False, indent=1)
    sys.exit(0 if all(r['ok'] for r in RESULTS) else 1)
