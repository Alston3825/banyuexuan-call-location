"""新需求回归：只用临时库、内存总线与虚构员工。"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient

from bycall.api.app import create_app
from bycall.core.service import HostService
from bycall.core.voice import VoiceStore
from bycall.core.watchops import WatchError
from tests.test_service import Base, S


class TestBinding(Base):
    def setUp(self):
        super().setUp()
        self.svc.cfg.watches['spare'] = dict(mac='test-mac')
        self.svc.watches.seed()
        self.online('w-01', 'w-02', 'spare')
        self.svc.tele.heartbeat('spare', self.clock().isoformat(), 95, -50, False)
        self.svc.tele.heartbeat('w-01', self.clock().isoformat(), 90, -50, False)
        self.svc.watches.reset('spare', 'test-admin')
        self.ack_reset('spare')

    def ack_reset(self, sn):
        w = next(w for w in self.svc.watches.rows('watch_inventory') if w['sn'] == sn)
        self.svc.watches.reset_ack(sn, w['reset_id'])

    def change(self, **kw):
        args = dict(staff='n01', target='spare', expected_version=0, request_id='swap-1', operator='admin')
        args.update(kw)
        return self.svc.watches.change(**args)

    def command(self, watch, kind, payload=None, mid=None):
        staff = self.svc.cfg.staff_of_watch(watch)
        p = dict(payload or {})
        if staff:
            p['binding_id'] = self.svc.watches.context(staff)['binding']['bind_id']
        self.send(f'{S}/watch/{watch}', watch, kind, p, msg_id=mid)

    def test_swap_migrates_open_owned_call_and_rejects_old_device(self):
        cid = self.call()
        self.accept('w-01', cid)
        r = self.change()
        self.assertEqual(r['sync_status'], 'LOCAL_PENDING')
        self.assertEqual(self.svc.cfg.watch_of('n01'), 'spare')
        self.assertEqual(self.svc.core.get(cid, store=S)['owner'], 'n01')
        self.assertTrue(self.bus.sent(f'{S}/dispatch/spare', 'CALL_DISPATCH'))
        self.send(f'{S}/ack/{cid}', 'w-01', 'CALL_COMPLETE', dict(method='remote'), call_id=cid)
        self.assertEqual(self.svc.core.get(cid, store=S)['state'], 'claimed')
        self.assertEqual(self.bus.sent(f'{S}/reply/w-01', 'CMD_REJECTED')[-1]['payload']['reason'], 'unknown_device')

    def test_idempotent_swap_and_changed_body_rejected(self):
        first = self.change()
        self.assertEqual(first['seq'], self.change()['seq'])
        with self.assertRaises(WatchError):
            self.change(fault=True)
        self.assertEqual(self.svc.watches.context('n01')['binding']['version'], 1)

    def test_stale_version_does_not_steal_spare(self):
        with self.assertRaises(WatchError):
            self.change(expected_version=99)
        self.assertEqual(self.svc.cfg.watch_of('n01'), 'w-01')
        self.assertEqual(next(w for w in self.svc.watches.inventory() if w['sn'] == 'spare')['state'], 'IDLE')

    def test_low_battery_offline_and_poor_signal_rejected(self):
        for battery, signal, online in [(79, -50, True), (95, -95, True), (95, None, True), (95, -50, False)]:
            self.svc.tele.heartbeat('spare', self.clock().isoformat(), battery, signal)
            self.svc.tele.set_online('spare', online, self.clock().isoformat())
            with self.assertRaises(WatchError):
                self.change()

    def test_stale_heartbeat_rejected_even_if_online_flag_true(self):
        self.clock.advance(91)
        with self.assertRaises(WatchError):
            self.change()

    def test_rollback_requires_actual_reset_receipt_before_reuse(self):
        self.change()
        self.change(action='ROLLBACK', expected_version=1, request_id='back')
        self.assertEqual(self.svc.cfg.watch_of('n01'), 'w-01')
        w = next(w for w in self.svc.watches.inventory() if w['sn'] == 'spare')
        self.assertEqual(w['state'], 'RESETTING')
        with self.assertRaises(WatchError):
            self.svc.watches.reset_ack('spare', 'wrong-generation')
        self.assertFalse(w['eligible'])
        self.ack_reset('spare')
        self.assertTrue(next(w for w in self.svc.watches.inventory() if w['sn'] == 'spare')['eligible'])

    def test_same_spare_concurrent_requests_only_one_succeeds(self):
        def attempt(staff):
            try:
                self.change(staff=staff, request_id=staff)
                return True
            except WatchError:
                return False
        with ThreadPoolExecutor(2) as ex:
            self.assertEqual(sum(ex.map(attempt, ['n01', 'n02'])), 1)

    def test_binding_survives_restart_and_old_config(self):
        self.change()
        from bycall.core.config import StoreConfig
        from bycall.gateway.bus import InMemoryBus
        from tests.test_service import CFG
        restarted = HostService(StoreConfig.load(CFG), InMemoryBus(), self.svc.core.path, self.clock)
        self.assertEqual(restarted.cfg.watch_of('n01'), 'spare')
        self.assertEqual(restarted.watches.context('n01')['binding']['version'], 1)

    def test_admin_credential_is_separate_and_operator_not_spoofable(self):
        with patch.dict(os.environ, BYCALL_WATCH_ADMINS=json.dumps({'station-token':'护士站甲'})):
            client = TestClient(create_app(self.svc, VoiceStore(os.path.join(self.tmp.name, 'voice')), api_key='device-key'))
        self.assertEqual(client.get('/watch-admin/snapshot', headers={'x-api-key':'device-key'}).status_code, 401)
        headers = {'x-watch-admin-key':'station-token'}
        self.assertEqual(client.get('/watch-admin/snapshot', headers=headers).status_code, 200)
        response = client.post('/watch-admin/change', headers=headers, json=dict(staff='n01',target='spare',version=0,request_id='api',operator='spoof'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['operator'], '护士站甲')


class TestTeam(TestBinding):
    def test_help_first_claim_wins_and_others_receive_withdrawal(self):
        self.online('w-03')
        self.command('w-01', 'HELP_REQUEST', mid='help')
        self.clock.advance(31)
        self.online('w-01', 'w-02', 'w-03')
        self.svc.tick()
        with self.svc.core._db() as db:
            hid = db.execute('SELECT id FROM team_help').fetchone()[0]
        self.command('w-02', 'HELP_ACTION', dict(help_id=hid, action='claim'))
        self.command('w-03', 'HELP_ACTION', dict(help_id=hid, action='claim'))
        with self.svc.core._db() as db:
            h = dict(db.execute('SELECT * FROM team_help WHERE id=?', (hid,)).fetchone())
        self.assertEqual(h['owner'], 'n02')
        self.assertEqual(h['state'], 'claimed')
        self.assertTrue(self.bus.sent(f'{S}/dispatch/w-01', 'HELP_STATE'))
        self.command('w-02', 'HELP_ACTION', dict(help_id=hid, action='complete'))
        with self.svc.core._db() as db:
            self.assertEqual(db.execute('SELECT state FROM team_help WHERE id=?', (hid,)).fetchone()[0], 'completed')

    def test_help_reaches_station_when_no_colleague_and_escalates(self):
        self.svc.tele.set_online('w-02', False, self.clock().isoformat())
        self.command('w-01', 'HELP_REQUEST')
        self.clock.advance(61)
        self.online('w-01')
        self.svc.tick()
        self.assertTrue(self.bus.sent(f'{S}/station/dispatch', 'HELP_ALERT'))
        self.assertTrue(any(m['payload'].get('status') == 'escalated' for m in self.bus.sent(f'{S}/dispatch/w-01', 'HELP_STATE')))

    def test_communications_wait_for_customer_call_then_retry_until_receipt(self):
        cid = self.call()
        self.command('w-02', 'TEAM_SEND', dict(channel='direct', target='n01', text='收到'), mid='message')
        self.assertFalse(self.bus.sent(f'{S}/dispatch/w-01', 'TEAM_MESSAGE'))
        self.send(f'{S}/ack/{cid}', 'tab-1601', 'CALL_CANCEL', {}, call_id=cid)
        self.svc.tick()
        messages = self.bus.sent(f'{S}/dispatch/w-01', 'TEAM_MESSAGE')
        self.assertEqual(len(messages), 1)
        did = messages[0]['payload']['delivery_id']
        self.clock.advance(4); self.svc.tick()
        self.assertEqual(len(self.bus.sent(f'{S}/dispatch/w-01', 'TEAM_MESSAGE')), 2)
        self.command('w-01', 'WATCH_RECEIVED', dict(delivery_id=did))
        self.clock.advance(4); self.svc.tick()
        self.assertEqual(len(self.bus.sent(f'{S}/dispatch/w-01', 'TEAM_MESSAGE')), 2)

    def test_send_rate_voice_limit_and_company_permission(self):
        with self.assertRaises(WatchError):
            self.svc.team.message('n01', 'company', dict(channel='company', text='收到'))
        with self.assertRaises(WatchError):
            self.svc.team.message('n01', 'voice', dict(channel='floor', voice_url='/voice/test.m4a', seconds=31))
        body = dict(channel='direct', target='n02', text='收到')
        mid = self.svc.team.message('n01', 'first', body)
        self.assertEqual(mid, self.svc.team.message('n01', 'first', body))
        with self.assertRaises(WatchError):
            self.svc.team.message('n01', 'second', body)

    def test_dedicated_topic_and_binding_session_required(self):
        self.send(f'{S}/watch/w-02', 'w-01', 'HELP_REQUEST', {})
        self.send(f'{S}/watch/w-01', 'w-01', 'HELP_REQUEST', {'binding_id':'stale'})
        with self.svc.core._db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM team_help').fetchone()[0], 0)

    def test_call_receipt_persists_and_stops_retry(self):
        self.call()
        d = self.bus.sent(f'{S}/dispatch/w-01', 'CALL_DISPATCH')[0]
        self.command('w-02', 'WATCH_RECEIVED', dict(delivery_id=d['payload']['delivery_id']))
        self.clock.advance(4); self.svc.tick()
        self.assertEqual(len(self.bus.sent(f'{S}/dispatch/w-01', 'CALL_DISPATCH')), 2)
        self.command('w-01', 'WATCH_RECEIVED', dict(delivery_id=d['payload']['delivery_id']))
        self.clock.advance(4); self.svc.tick()
        self.assertEqual(len(self.bus.sent(f'{S}/dispatch/w-01', 'CALL_DISPATCH')), 2)

    def test_scoped_history_token_revoked_on_swap(self):
        client = TestClient(create_app(self.svc, VoiceStore(os.path.join(self.tmp.name, 'voice-scoped')), api_key='admin'))
        old = self.svc.watches.context('n01')['binding']['bind_id']
        headers = {'x-watch-binding': old}
        self.assertEqual(client.get('/watch-device/w-01/history', headers=headers).status_code, 200)
        self.change()
        self.assertEqual(client.get('/watch-device/w-01/history', headers=headers).status_code, 401)
        self.assertEqual(client.get('/watch-device/spare/history', headers=headers).status_code, 401)
        new = self.svc.watches.context('n01')['binding']['bind_id']
        self.assertEqual(client.get('/watch-device/spare/history', headers={'x-watch-binding':new}).status_code, 200)

    def test_voice_duration_verified_by_host_and_read_access_scoped(self):
        import io
        import wave
        voice = VoiceStore(os.path.join(self.tmp.name, 'voice-scoped'))
        client = TestClient(create_app(self.svc, voice, api_key='admin'))
        headers = {'x-watch-binding':self.svc.watches.context('n01')['binding']['bind_id']}
        def wav(seconds):
            out = io.BytesIO()
            with wave.open(out, 'wb') as w:
                w.setparams((1,2,8000,0,'NONE','not compressed'))
                w.writeframes(b'\x00\x00' * (8000 * seconds))
            return out.getvalue()
        response = client.post('/watch-device/w-01/voice?ext=wav', content=wav(1), headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        url = response.json()['voice_url']
        self.assertEqual(client.post('/watch-device/w-01/voice?ext=wav', content=wav(31), headers=headers).status_code, 422)
        self.assertEqual(client.get('/watch-device/w-01'+url, headers=headers).status_code, 200)
        other = {'x-watch-binding':self.svc.watches.context('n02')['binding']['bind_id']}
        self.assertEqual(client.get('/watch-device/w-02'+url, headers=other).status_code, 403)
        self.svc.team.message('n01', 'clip', dict(channel='direct', target='n02', voice_url=url, seconds=1))
        self.assertEqual(client.get('/watch-device/w-02'+url, headers=other).status_code, 200)

    def test_faulted_watch_not_borrowable_until_reset(self):
        self.svc.watches.mark_fault('spare', '模拟故障', 'admin')
        with self.assertRaises(WatchError):
            self.change()
        self.svc.watches.reset('spare', 'admin')
        self.ack_reset('spare')
        self.change()

    def test_battery_samples_and_metrics_record_staff(self):
        self.send(f'{S}/heartbeat/w-01', 'w-01', 'HEARTBEAT', dict(battery=80, wifi_rssi=-55, battery_sample=True))
        with self.svc.core._db() as db:
            row = db.execute('SELECT * FROM battery_samples').fetchone()
            self.assertEqual((row['staff'], row['watch'], row['battery']), ('n01', 'w-01', 80))
