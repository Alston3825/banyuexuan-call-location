import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from bycall.core import protocol as P
from bycall.core.config import StoreConfig
from bycall.core.service import HostService
from bycall.gateway.bus import InMemoryBus

CFG = os.path.join(os.path.dirname(__file__), '..', 'bycall', 'custom', 'banyuexuan', 'demo_pilot.json')
S = 'gx'


class Clock:
    def __init__(self):
        # 2026-11-10 10:00 +08:00 = day shift
        self.t = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += timedelta(seconds=s)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.bus = InMemoryBus()
        self.svc = HostService(StoreConfig.load(CFG), self.bus, os.path.join(self.tmp.name, 'h.db'), self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, topic, dev, type_, payload, call_id=None, msg_id=None):
        env = P.Envelope(store_id=S, device_id=dev, type=type_, payload=payload, call_id=call_id,
                         **({'msg_id': msg_id} if msg_id else {}))
        self.bus.publish(topic, env.model_dump())

    def online(self, *watches):
        for w in watches:
            self.send(P.t_heartbeat(S, w), w, P.HEARTBEAT, dict(battery=90))

    def call(self, room='1601', rt='MILK_STORAGE', msg_id=None):
        self.send(P.t_call(S, '16', room), 'tab-' + room, P.CALL_REQUEST,
                  dict(room=room, service_code=rt), msg_id=msg_id)
        return self.bus.sent(P.t_feedback(S, room), P.CALL_FEEDBACK)[-1]['call_id']

    def accept(self, w, cid):
        self.send(P.t_ack(S, cid), w, P.CALL_ACCEPT, {}, call_id=cid)

    def fb(self, room='1601'):
        return [m['payload']['status'] for m in self.bus.sent(P.t_feedback(S, room), P.CALL_FEEDBACK)]


class TestLoop(Base):
    def test_full_loop_with_receipt(self):
        self.online('w-01')
        cid = self.call()
        self.assertEqual(self.fb(), ['received'])                 # instant receipt
        d = self.bus.sent(P.t_dispatch_watch(S, 'w-01'), P.CALL_DISPATCH)
        self.assertEqual(d[0]['payload']['label'], '取储存母乳')
        self.accept('w-01', cid)
        self.assertEqual(self.fb(), ['received', 'accepted'])
        self.send(P.t_location(S, 'w-01'), 'w-01', P.LOCATION_REPORT,
                  dict(readings=[dict(beacon_id='bcn-1601', rssi=-55)]))
        self.assertEqual(self.fb()[-1], 'arrived')               # auto arrival by BLE
        self.assertEqual(self.bus.sent(P.t_dispatch_watch(S, 'w-01'), P.CALL_STATE)[-1]['payload']['status'], 'arrived')
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_COMPLETE, dict(method='onsite', reason='已送母乳'), call_id=cid)
        self.assertEqual(self.fb(), ['received', 'accepted', 'arrived', 'completed'])
        kinds = [m['payload']['kind'] for m in self.bus.sent(P.t_events(S))]
        self.assertEqual(kinds, ['call.created', 'call.claim', 'call.arrive', 'call.complete'])

    def test_duplicate_request_is_one_call(self):
        self.online('w-01')
        self.call(msg_id='dup1')
        self.call(msg_id='dup1')
        with self.svc.core._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM calls').fetchone()[0], 1)

    def test_claim_race_one_winner_other_told(self):
        self.online('w-01', 'w-02', 'w-03')
        self.clock.advance(16)
        cid = self.call()
        self.clock.advance(16)
        self.svc.tick()
        self.assertTrue(self.bus.sent(P.t_dispatch_watch(S, 'w-02'), P.CALL_DISPATCH))
        self.accept('w-01', cid)
        self.accept('w-02', cid)
        self.assertEqual(self.fb().count('accepted'), 1)
        rej = self.bus.sent(P.t_reply(S, 'w-02'), P.CMD_REJECTED)
        self.assertEqual(rej[0]['payload']['reason'], 'already_taken_or_closed')
        self.assertTrue(self.bus.sent(P.t_dispatch_watch(S, 'w-02'), P.CALL_TAKEN))

    def test_replayed_accept_is_harmless(self):
        self.online('w-01')
        cid = self.call()
        self.accept('w-01', cid)
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_ACCEPT, {}, call_id=cid)   # new msg_id, same actor
        self.assertEqual(self.fb().count('accepted'), 1)
        self.assertFalse(self.bus.sent(P.t_reply(S, 'w-01'), P.CMD_REJECTED))

    def test_escalation_reaches_group_after_timeout(self):
        self.online('w-01', 'w-02', 'w-03')
        self.call()
        self.assertFalse(self.bus.sent(P.t_dispatch_watch(S, 'w-02'), P.CALL_DISPATCH))
        self.clock.advance(20)
        self.svc.tick()
        self.assertTrue(self.bus.sent(P.t_dispatch_watch(S, 'w-02'), P.CALL_DISPATCH))

    def test_nobody_online_goes_to_station(self):
        self.call()
        self.assertTrue(self.bus.sent(P.t_dispatch_station(S), P.CALL_DISPATCH))

    def test_emergency_goes_to_everyone_and_station(self):
        self.online('w-01', 'w-02', 'w-03')
        self.call(rt='EMERGENCY')
        for w in ('w-01', 'w-02', 'w-03'):
            self.assertTrue(self.bus.sent(P.t_dispatch_watch(S, w), P.CALL_DISPATCH), w)
        self.assertTrue(self.bus.sent(P.t_dispatch_station(S)))

    def test_cancel_by_room(self):
        self.online('w-01')
        cid = self.call()
        self.send(P.t_ack(S, cid), 'tab-1601', P.CALL_CANCEL, {}, call_id=cid)
        self.assertEqual(self.fb()[-1], 'cancelled')
        self.assertTrue(self.bus.sent(P.t_dispatch_watch(S, 'w-01'), P.CALL_TAKEN))

    def test_transfer_then_accept(self):
        self.online('w-01', 'w-02')
        cid = self.call()
        self.accept('w-01', cid)
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_TRANSFER, dict(target='n02', reason='忙'), call_id=cid)
        self.assertTrue(self.bus.sent(P.t_dispatch_watch(S, 'w-02'), P.CALL_DISPATCH)[-1]['payload']['reason'] == 'transfer')
        self.accept('w-02', cid)
        self.assertEqual(self.svc._call(cid)['owner'], 'n02')

    def test_offline_watch_dropped_from_routing(self):
        self.online('w-01', 'w-02')
        self.clock.advance(100)                      # both miss heartbeats
        self.online('w-02')
        self.svc.tick()
        self.assertEqual(self.svc.roster.state('n01'), 'offline')
        self.assertEqual(self.svc.roster.state('n02'), 'available')

    def test_lwt_marks_offline(self):
        self.online('w-01')
        self.send(P.t_status(S, 'w-01'), 'w-01', P.STATUS, dict(online=False))
        self.assertEqual(self.svc.roster.state('n01'), 'offline')


class TestResync(Base):
    def test_reconnecting_watch_gets_open_calls_back(self):
        self.online('w-01')
        cid = self.call()
        self.send(P.t_status(S, 'w-01'), 'w-01', P.STATUS, dict(online=False))
        self.send(P.t_status(S, 'w-01'), 'w-01', P.STATUS, dict(online=True))
        self.assertEqual(len(self.bus.sent(P.t_dispatch_watch(S, 'w-01'), P.CALL_DISPATCH)), 2)
        self.accept('w-01', cid)
        self.send(P.t_status(S, 'w-01'), 'w-01', P.STATUS, dict(online=True))       # restart while holding a call
        self.assertEqual(self.bus.sent(P.t_dispatch_watch(S, 'w-01'), P.CALL_STATE)[-1]['payload']['status'], 'accepted')
        self.assertEqual(len(self.bus.sent(P.t_dispatch_watch(S, 'w-01'), P.CALL_DISPATCH)), 3)


class TestRejections(Base):
    def test_unknown_device_and_wrong_room(self):
        self.send(P.t_call(S, '16', '1601'), 'tab-evil', P.CALL_REQUEST, dict(room='1601', service_code='MILK_STORAGE'))
        self.send(P.t_call(S, '16', '1602'), 'tab-1601', P.CALL_REQUEST, dict(room='1602', service_code='MILK_STORAGE'))
        reasons = [m['payload']['reason'] for t, m in self.bus.log if m['type'] == P.CMD_REJECTED]
        self.assertEqual(reasons, ['unknown_device', 'room_mismatch'])

    def test_tablet_gets_button_config(self):
        cfgs = lambda: self.bus.sent(P.t_reply(S, 'tab-1601'), P.DEVICE_CONFIG)
        self.send(P.t_status(S, 'tab-1601'), 'tab-1601', P.STATUS, dict(online=True))
        self.assertEqual(len(cfgs()), 1)
        c = cfgs()[0]['payload']
        codes = [b['code'] for b in c['buttons']]
        self.assertEqual(codes, ['BABY_CRY', 'NEED_NURSE', 'MILK_STORAGE', 'CLEANING', 'TRAY', 'GUEST_1', 'EMERGENCY'])
        self.assertTrue(c['buttons'][-1]['hold'])
        self.assertEqual(c['buttons'][-2]['label'], '')       # 自定义按钮没设置:空位
        # 心跳带着当前版本:不重复下发;版本不对(或没有):补发
        self.send(P.t_heartbeat(S, 'tab-1601'), 'tab-1601', P.HEARTBEAT, dict(cfg_ver=c['ver']))
        self.assertEqual(len(cfgs()), 1)
        self.send(P.t_heartbeat(S, 'tab-1601'), 'tab-1601', P.HEARTBEAT, dict(cfg_ver='old'))
        self.assertEqual(len(cfgs()), 2)
        # 服务类型变更后版本变化,下次心跳自动推送
        self.svc.cfg.request_types['LAUNDRY'] = '换洗衣物'
        self.send(P.t_heartbeat(S, 'tab-1601'), 'tab-1601', P.HEARTBEAT, dict(cfg_ver=c['ver']))
        self.assertIn('LAUNDRY', [b['code'] for b in cfgs()[-1]['payload']['buttons']])
        # 手表不收按钮配置
        self.online('w-01')
        self.assertEqual(self.bus.sent(P.t_reply(S, 'w-01'), P.DEVICE_CONFIG), [])

    def test_guest_buttons(self):
        self.svc.cfg.rooms['1601']['stay_id'] = 'stay-A'
        cfgs = lambda dev: self.bus.sent(P.t_reply(S, dev), P.DEVICE_CONFIG)
        self.assertEqual(self.svc.set_guest_buttons('1601', ['换尿布']), self.svc.guest_buttons('1601'))
        c = cfgs('tab-1601')[-1]['payload']                   # 设置后立即推送
        b = c['buttons']
        self.assertEqual([x['label'] for x in b if x.get('guest')], ['换尿布'])
        self.assertEqual(b[-1]['code'], 'EMERGENCY')          # 紧急仍在最后
        self.assertNotIn('guest', [k for x in self.bus.sent(P.t_reply(S, 'tab-1602'), P.DEVICE_CONFIG) for k in x])
        # 别的房间看不到
        self.assertEqual(self.svc.guest_buttons('1602'), [])
        self.assertEqual(self.svc.tablet_config('tab-1602')['buttons'][-2]['label'], '')
        # 按专属按钮呼叫:记为 CUSTOM,名称为客户自己的按钮文字,手表能看到
        self.online('w-01')
        self.send(P.t_call(S, '16', '1601'), 'tab-1601', P.CALL_REQUEST, dict(room='1601', service_code='GUEST_1'))
        d = self.bus.sent(P.t_dispatch_watch(S, 'w-01'), P.CALL_DISPATCH)[-1]['payload']
        self.assertEqual((d['service_code'], d['label']), ('CUSTOM', '换尿布'))
        # 没设置的槽位、别的房间的专属按钮不能呼叫
        n = len(self.bus.sent(P.t_dispatch_watch(S, 'w-01'), P.CALL_DISPATCH))
        self.send(P.t_call(S, '16', '1602'), 'tab-1602', P.CALL_REQUEST, dict(room='1602', service_code='GUEST_1'))
        self.send(P.t_call(S, '16', '1601'), 'tab-1601', P.CALL_REQUEST, dict(room='1601', service_code='GUEST_2x'))
        self.assertEqual(len(self.bus.sent(P.t_dispatch_watch(S, 'w-01'), P.CALL_DISPATCH)), n)
        # 换了新客户(stay 变了),旧客户的专属按钮不再生效
        self.svc.cfg.rooms['1601']['stay_id'] = 'stay-B'
        self.assertEqual(self.svc.guest_buttons('1601'), [])
        self.send(P.t_heartbeat(S, 'tab-1601'), 'tab-1601', P.HEARTBEAT, dict(cfg_ver=c['ver']))
        self.assertFalse([x for x in cfgs('tab-1601')[-1]['payload']['buttons'] if x.get('guest') and x['label']])

    def test_routing_to_role(self):
        # 默认:打扫卫生也发给护理师;改成保洁后,只发给保洁岗位的人
        self.svc.cfg.staff['n03']['role'] = 'cleaner'
        self.svc.roster.roles['n03'] = 'cleaner'
        self.online('w-01', 'w-03')
        self.call(rt='CLEANING')
        self.assertTrue(self.bus.sent(P.t_dispatch_watch(S, 'w-01'), P.CALL_DISPATCH))
        self.svc.set_routing({'CLEANING': 'cleaner', 'CUSTOM': 'cleaner'})
        n1 = len(self.bus.sent(P.t_dispatch_watch(S, 'w-01'), P.CALL_DISPATCH))
        self.call(rt='CLEANING')
        self.assertEqual(len(self.bus.sent(P.t_dispatch_watch(S, 'w-01'), P.CALL_DISPATCH)), n1)
        self.assertEqual(self.bus.sent(P.t_dispatch_watch(S, 'w-03'), P.CALL_DISPATCH)[-1]['payload']['reason'], 'role')
        # 自定义按钮同理;紧急不受路由影响,仍发给护理师
        self.svc.set_guest_buttons('1601', ['换尿布'])
        self.send(P.t_call(S, '16', '1601'), 'tab-1601', P.CALL_REQUEST, dict(room='1601', service_code='GUEST_1'))
        self.assertEqual(self.bus.sent(P.t_dispatch_watch(S, 'w-03'), P.CALL_DISPATCH)[-1]['payload']['label'], '换尿布')
        self.call(rt='EMERGENCY')
        self.assertEqual(self.bus.sent(P.t_dispatch_watch(S, 'w-01'), P.CALL_DISPATCH)[-1]['payload']['priority'], 'emergency')
        # 该岗位没人在线:立即进护士站,不丢
        self.svc.roster.set_status('n03', 'offline')
        self.call(rt='TRAY')
        self.svc.set_routing({'TRAY': 'cleaner'})
        n = len(self.bus.sent(P.t_dispatch_station(S), P.CALL_DISPATCH))
        self.call(rt='TRAY')
        self.assertEqual(len(self.bus.sent(P.t_dispatch_station(S), P.CALL_DISPATCH)), n + 1)
        # 非法设置被拒绝
        from bycall.core.routing import RoutingError
        with self.assertRaises(RoutingError):
            self.svc.set_routing({'CLEANING': 'ghost'})
        with self.assertRaises(RoutingError):
            self.svc.set_routing({'EMERGENCY': 'cleaner'})

    def test_topic_device_must_match_envelope(self):
        # w-01 的账号只能发自己的主题,但就算发了,信封里冒充 w-02 也会被主机拒绝
        self.send(P.t_heartbeat(S, 'w-01'), 'w-02', P.HEARTBEAT, dict(battery=50))
        self.send(P.t_location(S, 'w-01'), 'w-02', P.LOCATION_REPORT, dict(readings=[]))
        reasons = [m['payload']['reason'] for t, m in self.bus.log if m['type'] == P.CMD_REJECTED]
        self.assertEqual(reasons, ['topic_device_mismatch'] * 2)
        self.assertEqual(self.svc.tele.devices(), [])

    def test_other_store_and_garbage_ignored(self):
        self.bus.publish(P.t_call(S, '16', '1601'), dict(msg_id='x', store_id='zz', device_id='tab-1601',
                                                         type=P.CALL_REQUEST, payload={}))
        self.bus.publish(P.t_call(S, '16', '1601'), {'nonsense': 1})
        self.assertEqual(self.svc.rejected_count, 2)
        with self.svc.core._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM calls').fetchone()[0], 0)

    def test_complete_onsite_requires_arrival(self):
        # 必须先到场(手动点"我已到场"或信标自动识别)才能完成
        self.online('w-01')
        cid = self.call()
        self.accept('w-01', cid)
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_COMPLETE, dict(method='onsite'), call_id=cid)
        self.assertEqual(self.bus.sent(P.t_reply(S, 'w-01'), P.CMD_REJECTED)[-1]['payload']['reason'],
                         'complete_not_allowed')
        self.assertEqual(self.svc.core.get(cid, store=S)['state'], 'claimed')
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_ARRIVE, {}, call_id=cid)
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_COMPLETE, dict(method='onsite'), call_id=cid)
        self.assertEqual(self.svc.core.get(cid, store=S)['state'], 'completed')


class TestTelemetry(Base):
    def test_vitals_and_location_trail(self):
        self.online('w-01')
        self.send(P.t_vitals(S, 'w-01'), 'w-01', P.VITALS_REPORT, dict(heart_rate=78, spo2=98))
        self.send(P.t_vitals(S, 'w-01'), 'w-01', P.VITALS_REPORT, dict(heart_rate=900))   # invalid
        v = self.svc.tele.vitals('n01')
        self.assertEqual([x['heart_rate'] for x in v], [78])
        for b, r in (('bcn-c16', -60), ('bcn-c16', -61), ('bcn-1602', -50)):
            self.send(P.t_location(S, 'w-01'), 'w-01', P.LOCATION_REPORT,
                      dict(readings=[dict(beacon_id=b, rssi=r)]))
        self.assertTrue(self.svc.tele.latest_locations())


class TestLocator(unittest.TestCase):
    def test_hysteresis_prevents_flapping(self):
        from bycall.core.locator import Locator
        loc = Locator({'a': {'area': 'A', 'room': '1'}, 'b': {'area': 'B', 'room': '2'}}, window=1)
        r = lambda a, b: [dict(beacon_id='a', rssi=a), dict(beacon_id='b', rssi=b)]
        self.assertEqual(loc.update('w', r(-60, -70))['beacon'], 'a')
        self.assertEqual(loc.update('w', r(-62, -60))['beacon'], 'a')     # only 2 dB better: stay
        self.assertEqual(loc.update('w', r(-70, -55))['beacon'], 'b')     # clearly stronger: switch
        self.assertIsNone(loc.update('w', [dict(beacon_id='a', rssi=-100)]))


if __name__ == '__main__':
    unittest.main()
