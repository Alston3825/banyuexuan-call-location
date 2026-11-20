"""Isolated config lifecycle and authorization regressions (no real devices or databases)."""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from bycall.api.app import create_app
from bycall.core import protocol as P
from bycall.core.locator import Locator
from bycall.core.terminal_config import TerminalConfigs, ConfigError
from bycall.core.voice import VoiceStore
from tests.test_service import Base, S


class TestTerminalConfig(Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.svc, VoiceStore(os.path.join(self.tmp.name, 'v')), api_key='test-admin', swap_key='test-swap'))
        self.h = {'X-Api-Key': 'test-admin'}

    def save(self, patch=None, targets=None, expected=None, kind='watch'):
        return self.client.put('/admin/terminal-config/' + kind, headers=self.h,
            json=dict(targets=targets or ['w-01'], settings=patch or dict(beacon_uuid='a' * 32), expected=expected or {'w-01': 0}))

    def test_admin_auth_and_no_credential_remote_write(self):
        self.assertEqual(self.client.get('/admin/terminals').status_code, 200)
        for path in ['/admin/terminal-config', '/admin/beacons', '/terminal-config/station/station-test']:
            self.assertEqual(self.client.get(path).status_code, 401)
            self.assertEqual(self.client.get(path, headers={'X-Api-Key': 'test-swap'}).status_code, 401)
        for k in ['device_id', 'store_id', 'api_key', 'mqtt_password', 'room_id']:
            self.assertEqual(self.save({k: 'x'}).status_code, 400)

    def test_save_push_ack_reject_stale_and_restart(self):
        r = self.save(dict(scan_on_s=3, scan_period_s=12))
        self.assertEqual(r.status_code, 200)
        rev = r.json()['revision']
        pushed = self.bus.sent(P.t_reply(S, 'w-01'), 'TERMINAL_CONFIG')[-1]['payload']
        self.assertEqual(pushed['revision'], rev)
        self.assertEqual(pushed['device'], 'w-01')
        self.assertEqual(self.svc.terminal_configs.get('watch', 'w-01')['status'], 'pending')
        self.send(P.t_heartbeat(S, 'w-01'), 'w-01', P.HEARTBEAT,
                  dict(terminal_config_v=1, terminal_revision=rev, terminal_result='applying'))
        self.assertTrue(self.bus.sent(P.t_reply(S, 'w-01'), 'TERMINAL_CONFIRM'))
        self.send(P.t_heartbeat(S, 'w-01'), 'w-01', P.HEARTBEAT,
                  dict(terminal_config_v=1, terminal_revision=rev, terminal_result='applied'))
        self.assertEqual(self.svc.terminal_configs.get('watch', 'w-01')['status'], 'applied')
        c = TerminalConfigs(self.svc.terminal_configs.path, S)
        self.assertEqual(c.get('watch', 'w-01')['revision'], rev)
        self.assertFalse(c.ack('watch', 'w-01', rev+1, 'applied'))
        self.assertEqual(self.save(dict(scan_on_s=1)).status_code, 409)

    def test_offline_reconnect_resends_and_failed_revision_not_reapplied(self):
        rev = self.save().json()['revision']
        before = len(self.bus.sent(P.t_reply(S, 'w-01'), 'TERMINAL_CONFIG'))
        self.send(P.t_heartbeat(S, 'w-01'), 'w-01', P.HEARTBEAT, dict(terminal_config_v=1, terminal_revision=0))
        self.assertEqual(len(self.bus.sent(P.t_reply(S, 'w-01'), 'TERMINAL_CONFIG')), before + 1)
        self.send(P.t_heartbeat(S, 'w-01'), 'w-01', P.HEARTBEAT,
                  dict(terminal_config_v=1, terminal_revision=rev, terminal_result='failed'))
        c = self.svc.terminal_configs.get('watch', 'w-01')
        self.assertEqual(c['status'], 'failed')
        self.assertEqual(len(self.bus.sent(P.t_reply(S, 'w-01'), 'TERMINAL_CONFIG')), before + 1)

    def test_old_device_not_marked_applied(self):
        self.save()
        self.online('w-01')
        c = self.svc.terminal_configs.get('watch', 'w-01')
        self.assertEqual(c['status'], 'pending')
        self.assertFalse(c['capability'])

    def test_identity_spoof_cannot_ack_another_device(self):
        rev = self.save().json()['revision']
        self.send(P.t_heartbeat(S, 'w-02'), 'w-01', P.HEARTBEAT,
                  dict(terminal_config_v=1, terminal_revision=rev, terminal_result='applied'))
        self.assertEqual(self.svc.terminal_configs.get('watch', 'w-01')['status'], 'pending')

    def test_validation_and_atomic_batch(self):
        for patch in [dict(scan_on_s=20), dict(scan_period_s=3), dict(heartbeat_s=120),
                      dict(beacon_uuid='bad'), dict(wifi_lock='true'), dict(mqtt_port=True),
                      dict(api_base='http://user:secret@server:8100'), dict(api_base='http://server:99999'),
                      dict(mqtt_host='http://server'), dict(quick_replies=[]), dict(room_label='x')]:
            self.assertEqual(self.save(patch).status_code, 400, patch)
        self.assertEqual(self.save(targets=['unknown']).status_code, 404)
        self.assertEqual(self.save(targets=['w-01', 'w-01']).status_code, 400)
        self.save(dict(scan_on_s=2, scan_period_s=9))
        r = self.save(dict(scan_on_s=10), targets=['w-02', 'w-01'], expected={'w-01': 1, 'w-02': 0})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.svc.terminal_configs.get('watch', 'w-02')['revision'], 0)

    def test_partial_merge_and_concurrent_edit(self):
        first = self.save(dict(scan_period_s=15)).json()['revision']
        def put():
            return self.save(dict(scan_on_s=4), expected={'w-01': first}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(lambda _: put(), range(2)))
        self.assertEqual(sorted(codes), [200, 409])
        c = self.svc.terminal_configs.get('watch', 'w-01')
        self.assertEqual(c['settings'], dict(scan_period_s=15, scan_on_s=4))

    def test_tablet_uses_own_reply_and_no_new_acl_topics(self):
        r = self.save(dict(room_label='试点房', voice_enabled=False), targets=['tab-1601'], expected={'tab-1601': 0}, kind='tablet')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.bus.sent(P.t_reply(S, 'tab-1601'), 'TERMINAL_CONFIG'))
        self.assertFalse(self.bus.sent(P.t_reply(S, 'tab-1602'), 'TERMINAL_CONFIG'))

    def test_station_registration_long_poll_ack_and_unique_id(self):
        a = self.client.get('/terminal-config/station/station-test', headers=self.h)
        self.assertEqual(a.status_code, 200)
        self.assertEqual(self.client.get('/terminal-config/station/bad', headers=self.h).status_code, 400)
        r = self.save(dict(watch_slots=48, title='试点大屏'), targets=['station-test'], expected={'station-test': 0}, kind='station')
        rev = r.json()['revision']
        self.assertEqual(self.client.post('/terminal-config/station/station-test/ack', headers=self.h, json=dict(revision=rev,status='applied')).status_code, 200)
        self.assertEqual(self.client.post('/terminal-config/station/station-other/ack', headers=self.h, json=dict(revision=rev,status='applied')).status_code, 409)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda:self.client.get('/terminal-config/station/station-test?after='+str(rev),headers=self.h))
            time.sleep(.1)
            self.save(dict(late_seconds=40), targets=['station-test'], expected={'station-test': rev}, kind='station')
            self.assertGreater(future.result(timeout=3).json()['revision'], rev)
        self.assertEqual(self.save(dict(watch_slots=39),targets=['station-test'],expected={'station-test':rev+1},kind='station').status_code,400)

    def test_beacon_map_persistence_validation_and_live_locator(self):
        body = dict(expected=0, items=[dict(major=16, minor=1601, room='1601', area='试点房')])
        r = self.client.put('/admin/beacons', headers=self.h, json=body)
        self.assertEqual(r.status_code, 200)
        fix = self.svc.locator.update('w-01', [dict(beacon_id='16-1601', rssi=-45)])
        self.assertEqual(fix['room'], '1601')
        self.assertEqual(TerminalConfigs(self.svc.terminal_configs.path,S).beacons()['mapping']['16-1601']['room'],'1601')
        self.assertEqual(self.client.put('/admin/beacons',headers=self.h,json=body).status_code,409)
        body['expected']=1
        body['items']*=2
        self.assertEqual(self.client.put('/admin/beacons',headers=self.h,json=body).status_code,400)
        body['items']=[dict(major=16,minor=1602,room='unknown')]
        self.assertEqual(self.client.put('/admin/beacons',headers=self.h,json=body).status_code,400)

    def test_configuration_store_isolated_by_store(self):
        self.save()
        other = TerminalConfigs(self.svc.terminal_configs.path, 'other-store')
        self.assertEqual(other.get('watch','w-01')['revision'],0)
        self.assertEqual(other.beacons()['revision'],0)
