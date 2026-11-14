import json, os, tempfile, unittest
import httpx
from bycall.core import protocol as P
from bycall.core.config import StoreConfig
from bycall.core.service import HostService
from bycall.gateway.bus import InMemoryBus
from bycall.integration.master_sync import MasterSync
from bycall.integration.mgmt_client import MgmtClient
from bycall.integration.writeback import CallWriteback
from tests.test_service import Clock

ST = 'cmstore0001'
T = lambda n: f'2026-11-10T08:00:{n:02d}.000Z'


class FakeMgmt:
    """Minimal stand-in for /integration/v1/* with updatedSince paging (page size 2)."""
    def __init__(self):
        self.down = False
        self.posts, self.requests = [], []
        self.data = {
            'rooms': [
                dict(room_id='cmroom01', store_id=ST, floor='2F', room_no='2801', status='OCCUPIED',
                     current_stay_id='cmstay01', tablet_device_id='PAD-0001', beacon_ids=['B-2801-1'], updated_at=T(1)),
                dict(room_id='cmroom02', store_id=ST, floor='2F', room_no='2802', status='FREE',
                     current_stay_id=None, tablet_device_id='PAD-0002', beacon_ids=['B-2802-1', 'B-2802-2'], updated_at=T(2)),
                dict(room_id='cmroom03', store_id=ST, floor='3F', room_no='3801', status='FREE',
                     current_stay_id=None, tablet_device_id='PAD-0003', beacon_ids=[], updated_at=T(3)),
            ],
            'staff': [
                dict(staff_id='cmstaffA', store_id=ST, name='王护理师', roles=['NURSE'], watch_id='W-001',
                     duty_status='ON_DUTY', responsible_floors=['2F'], employed=True, updated_at=T(1)),
                dict(staff_id='cmstaffB', store_id=ST, name='李护理师', roles=['NURSE'], watch_id='W-002',
                     duty_status='OFF_DUTY', responsible_floors=['2F'], employed=True, updated_at=T(2)),
                dict(staff_id='cmstaffC', store_id=ST, name='赵组长', roles=['HEAD_NURSE'], watch_id='W-003',
                     duty_status='ON_DUTY', responsible_floors=['2F', '3F'], employed=True, updated_at=T(3)),
            ],
            'stays': [], 'stores': [dict(store_id=ST, name='高新店', updated_at=T(0)),
                                    dict(store_id='other', name='别店', updated_at=T(0))],
            'service-codes': [dict(service_code=c, name=n, category='CALL', updated_at=T(i)) for i, (c, n) in
                              enumerate([('NEED_NURSE', '呼叫护理师'), ('MILK_STORAGE', '取储存母乳'), ('EMERGENCY', '紧急')])],
        }
        self.client = MgmtClient('http://mgmt/api/v1', 'bmx_test', transport=httpx.MockTransport(self.handle))

    def handle(self, req):
        if self.down:
            raise httpx.ConnectError('down')
        self.requests.append(req)
        assert req.headers['authorization'] == 'Bearer bmx_test'
        path = req.url.path.replace('/api/v1/integration/v1/', '')
        if req.method == 'POST' and path == 'call-events':
            self.posts.append(json.loads(req.content))
            return httpx.Response(200, json={'ok': True})
        since = req.url.params.get('updatedSince')
        items = [i for i in sorted(self.data[path], key=lambda i: i['updated_at']) if not since or i['updated_at'] > since]
        page = items[:2]
        return httpx.Response(200, json=dict(items=page, count=len(page), hasMore=len(items) > 2,
                                             nextSince=page[-1]['updated_at'] if page else None))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = FakeMgmt()
        self.clock, self.bus = Clock(), InMemoryBus()
        cfg = StoreConfig(store_id=ST, rooms={}, beacons={'B-CORR-2F': dict(area='2F 走廊', room=None)}, staff={},
                          groups={}, assignments={}, leaders={})
        self.svc = HostService(cfg, self.bus, os.path.join(self.tmp.name, 'h.db'), self.clock)
        self.sync = MasterSync(self.m.client, os.path.join(self.tmp.name, 'h.db'), cfg, self.svc.roster)
        self.wb = CallWriteback(self.m.client, self.svc.core, cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, topic, dev, type_, payload, call_id=None):
        self.bus.publish(topic, P.Envelope(store_id=ST, device_id=dev, type=type_, payload=payload, call_id=call_id).model_dump())


class TestSync(Base):
    def test_sync_builds_config_with_pagination(self):
        r = self.sync.sync()
        self.assertEqual(r['rooms'], 3)                       # 3 rooms over 2 pages
        self.assertEqual(r['groups'], {'2F': 2, '3F': 1})     # off-duty B excluded
        cfg = self.svc.cfg
        self.assertEqual(cfg.rooms['cmroom01']['tablet'], 'PAD-0001')
        self.assertEqual(cfg.beacons['B-2802-2'], dict(area='2802 房内', room='cmroom02'))
        self.assertIn('B-CORR-2F', cfg.beacons)               # local corridor beacon kept
        self.assertEqual(self.svc.roster.leaders, {'2F': 'cmstaffC', '3F': 'cmstaffC'})
        self.assertEqual(cfg.request_types['NEED_NURSE'], '呼叫护理师')

    def test_incremental_uses_updated_since_and_applies_changes(self):
        self.sync.sync()
        self.m.data['staff'][1]['duty_status'] = 'ON_DUTY'
        self.m.data['staff'][1]['updated_at'] = T(9)
        self.m.requests.clear()
        r = self.sync.sync(['staff'])
        self.assertEqual(self.m.requests[0].url.params['updatedSince'], T(3))
        self.assertEqual(r['groups']['2F'], 3)

    def test_full_sync_drops_removed_rows(self):
        self.sync.sync()
        del self.m.data['staff'][0]
        self.assertEqual(self.sync.sync(['staff'])['staff'], 3)       # incremental can't see removal
        self.assertEqual(self.sync.sync(['staff'], full=True)['staff'], 2)

    def test_other_store_ignored(self):
        self.m.data['rooms'].append(dict(room_id='x', store_id='other', floor='2F', room_no='9', updated_at=T(5)))
        self.sync.sync()
        self.assertNotIn('x', self.svc.cfg.rooms)


class TestEndToEnd(Base):
    def setUp(self):
        super().setUp()
        self.sync.sync()
        for w in ('W-001', 'W-002', 'W-003'):
            self.send(P.t_heartbeat(ST, w), w, P.HEARTBEAT, dict(battery=80))

    def call(self, code='NEED_NURSE', room='cmroom01', dev='PAD-0001'):
        self.send(P.t_call(ST, '2F', room), dev, P.CALL_REQUEST, dict(room=room, service_code=code))
        return self.bus.sent(P.t_feedback(ST, room), P.CALL_FEEDBACK)[-1]['call_id']

    def test_floor_group_dispatch_skips_off_duty(self):
        self.call()
        self.assertTrue(self.bus.sent(P.t_dispatch_watch(ST, 'W-001'), P.CALL_DISPATCH))
        self.assertFalse(self.bus.sent(P.t_dispatch_watch(ST, 'W-002')))   # off duty
        d = self.bus.sent(P.t_dispatch_watch(ST, 'W-001'))[0]['payload']
        self.assertEqual((d['room_no'], d['floor'], d['label']), ('2801', '2F', '呼叫护理师'))

    def test_unknown_service_code_rejected(self):
        self.send(P.t_call(ST, '2F', 'cmroom01'), 'PAD-0001', P.CALL_REQUEST, dict(room='cmroom01', service_code='NOPE'))
        self.assertEqual(self.bus.sent(P.t_reply(ST, 'PAD-0001'))[0]['payload']['reason'], 'unknown_service')

    def test_writeback_follows_lifecycle_and_is_idempotent(self):
        cid = self.call()
        self.assertEqual(self.wb.flush(), 1)
        self.assertEqual(self.m.posts[-1]['status'], 'PENDING')
        self.assertEqual(self.wb.flush(), 0)                                # nothing new
        self.send(P.t_ack(ST, cid), 'W-001', P.CALL_ACCEPT, {}, call_id=cid)
        self.send(P.t_location(ST, 'W-001'), 'W-001', P.LOCATION_REPORT, dict(readings=[dict(beacon_id='B-2801-1', rssi=-50)]))
        self.send(P.t_ack(ST, cid), 'W-001', P.CALL_COMPLETE, dict(method='onsite', reason='已送母乳'), call_id=cid)
        self.assertEqual(self.wb.flush(), 1)
        rec = self.m.posts[-1]
        self.assertEqual((rec['call_id'], rec['store_id'], rec['room_id'], rec['stay_id']), (cid, ST, 'cmroom01', 'cmstay01'))
        self.assertEqual((rec['status'], rec['service_code'], rec['handler_staff_id'], rec['feedback']),
                         ('COMPLETED', 'NEED_NURSE', 'cmstaffA', '已送母乳'))
        self.assertTrue(rec['initiated_at'] and rec['dispatched_at'] and rec['accepted_at'] and rec['completed_at'])

    def test_writeback_retries_after_outage(self):
        self.call()
        self.m.down = True
        self.assertEqual(self.wb.flush(), 0)
        self.assertTrue(self.wb.errors)
        self.m.down = False
        self.assertEqual(self.wb.flush(), 1)

    def test_emergency_priority_from_service_code(self):
        self.call('EMERGENCY')
        for w in ('W-001', 'W-003'):
            self.assertTrue(self.bus.sent(P.t_dispatch_watch(ST, w), P.CALL_DISPATCH))



class TestPaginationGuard(unittest.TestCase):
    def client_with_pages(self, pages):
        def handle(req):
            self.assertTrue(pages, 'unexpected extra pagination request')
            return httpx.Response(200, json=pages.pop(0))
        return MgmtClient('http://m/api/v1', 'k', transport=httpx.MockTransport(handle))

    def test_list_cursor_cycle_raises(self):
        from bycall.integration.mgmt_client import MgmtError
        pages = [dict(items=[], hasMore=True, nextSince=n) for n in ('a', 'b', 'a')]
        client = self.client_with_pages(pages)
        try:
            with self.assertRaises(MgmtError):
                client.pull('rooms')
        finally:
            client.http.close()
        self.assertEqual(pages, [])

    def test_event_cursor_missing_stalled_or_cyclic_raises(self):
        from bycall.integration.mgmt_client import MgmtError
        for cursors in ([None], [4], [5, 6, 5]):
            with self.subTest(cursors=cursors):
                pages = [dict(items=[], hasMore=True, nextSeq=n) for n in cursors]
                client = self.client_with_pages(pages)
                try:
                    with self.assertRaises(MgmtError):
                        client.events(ST, after_seq=4)
                    self.assertEqual(pages, [])
                finally:
                    client.http.close()

    def test_event_pagination_preserves_items_and_cursor(self):
        pages = [dict(items=[{'seq': 5}], hasMore=True, nextSeq=5),
                 dict(items=[{'seq': 6}], hasMore=False, nextSeq=6)]
        client = self.client_with_pages(pages)
        try:
            self.assertEqual(client.events(ST, after_seq=4), ([{'seq': 5}, {'seq': 6}], 6))
        finally:
            client.http.close()

    def test_stalled_cursor_raises_instead_of_looping(self):
        from bycall.integration.mgmt_client import MgmtError
        t = httpx.MockTransport(lambda r: httpx.Response(200, json=dict(items=[{'a': 1}], hasMore=True, nextSince=None)))
        with self.assertRaises(MgmtError):
            MgmtClient('http://m/api/v1', 'k', transport=t).pull('rooms')


if __name__ == '__main__':
    unittest.main()
