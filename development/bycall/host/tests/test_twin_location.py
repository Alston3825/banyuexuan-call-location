import os
from datetime import timedelta

from fastapi.testclient import TestClient
from bycall.api.app import create_app
from bycall.core import protocol as P
from bycall.core.location_state import LocationReports
from bycall.core.locator import Locator
from bycall.core.voice import VoiceStore
from tests.test_service import Base, S


class TestTwinLocation(Base):
    def setUp(self):
        super().setUp()
        self.online('w-01')
        self.voice = VoiceStore(os.path.join(self.tmp.name, 'voice'))
        self.c = TestClient(create_app(self.svc, self.voice, api_key='admin-test', twin_key='twin-test'))
        self.h = {'X-Api-Key': 'admin-test'}
        self.th = {'X-Api-Key': 'twin-test'}

    def report(self, readings=None, **meta):
        self.send(P.t_location(S, 'w-01'), 'w-01', P.LOCATION_REPORT,
                  dict(readings=readings if readings is not None else [dict(beacon_id='bcn-1601', rssi=-50)], **meta))

    def view(self):
        return next(v for v in self.svc.location_views() if v['watch'] == 'w-01')

    def test_legacy_payload_works_and_projection_has_no_precise_coordinates(self):
        self.report()
        v = self.view()
        self.assertEqual((v['location_status'], v['room'], v['floor']), ('fresh', '1601', '16'))
        result = self.c.get('/integration/twin/locations', headers=self.th).json()
        self.assertEqual(result['schema'], 'bycall.location.v1')
        v = next(i for i in result['items'] if i['watch'] == 'w-01')
        self.assertEqual(v['precision'], 'area')
        self.assertIsNone(v['coordinates'])
        for key in ('name', 'heart_rate', 'voice_url', 'api_key', 'beacon', 'last_known'):
            self.assertNotIn(key, v)

    def test_current_receipt_is_updated_while_history_only_records_transitions(self):
        self.report()
        old_at = self.svc.tele.latest_locations()[0]['at']
        self.clock.advance(29)
        self.report(sampled_at=self.clock().isoformat(), report_interval_s=10,
                    scan_status='ok', observation_source='ble')
        self.clock.advance(20)
        self.assertEqual(self.view()['location_status'], 'fresh')
        self.assertNotEqual(self.view()['at'], old_at)
        self.assertEqual(self.svc.tele.latest_locations()[0]['at'], old_at)

    def test_empty_window_clears_fix_and_cannot_implicitly_arrive(self):
        self.report()
        cid = self.call(); self.accept('w-01', cid)
        self.report([])
        v = self.view()
        self.assertEqual((v['location_status'], v['location_reason']), ('unknown', 'empty_window'))
        self.assertIsNone(v['room'])
        self.assertEqual(v['last_known']['room'], '1601')
        self.assertEqual(self.svc._call(cid)['state'], 'claimed')
        self.assertIsNone(self.svc._location('n01'))

    def test_unmapped_and_weak_current_observations_do_not_reuse_old_room(self):
        for readings, reason in [([dict(beacon_id='99-99', rssi=-50)], 'unmapped'),
                                 ([dict(beacon_id='bcn-1601', rssi=-120)], 'below_threshold')]:
            self.report()
            self.report(readings)
            self.assertEqual(self.view()['location_reason'], reason)
            self.assertIsNone(self.view()['room'])

    def test_scan_error_and_simulation_never_create_live_fix(self):
        self.report(scan_status='scan_failed', scan_error_code=2)
        self.assertEqual(self.view()['location_reason'], 'scan_error')
        self.assertEqual(self.view()['scan_error_code'], 2)
        self.report(observation_source='simulated', scan_status='ok')
        self.assertEqual(self.view()['location_reason'], 'simulated')
        self.assertIsNone(self.view()['room'])

    def test_heartbeat_does_not_refresh_location(self):
        self.report()
        self.clock.advance(31); self.online('w-01')
        v = self.view()
        self.assertTrue(v['online'])
        self.assertEqual((v['location_status'], v['location_reason']), ('stale', 'expired'))
        self.assertIsNone(v['room'])
        snap = self.c.get('/station/snapshot', headers=self.h).json()
        n = next(x for x in snap['nurses'] if x['watch'] == 'w-01')
        self.assertIsNone(n['area']); self.assertEqual(n['location_status'], 'stale')

    def test_disconnect_and_host_restart_hide_last_position(self):
        self.report()
        self.send(P.t_status(S, 'w-01'), 'w-01', P.STATUS, dict(online=False))
        self.assertEqual(self.view()['location_reason'], 'offline')
        self.online('w-01')
        self.svc.location_reports = LocationReports(self.svc.core.path, S, 'new-process')
        self.assertEqual(self.view()['location_reason'], 'host_restarted')
        self.report()
        self.assertEqual(self.view()['location_status'], 'fresh')

    def test_binding_change_does_not_expose_previous_wearer(self):
        self.report()
        reports = self.svc.location_reports
        row = reports.rows()['w-01']
        v = reports.describe(row, 'w-01', 'different-employee', 'different-binding', self.clock(), True, self.svc.cfg.beacons)
        self.assertEqual(v['location_reason'], 'binding_changed')
        self.assertIsNone(v['last_known'])
        self.assertIsNone(v['room'])

    def test_mapping_edit_invalidates_current_fix_until_new_report(self):
        self.report()
        self.svc.cfg.beacons['bcn-1601']['room'] = '1602'
        self.assertEqual(self.view()['location_reason'], 'mapping_changed')
        self.assertIsNone(self.view()['room'])
        self.report()
        self.assertEqual(self.view()['room'], '1602')

    def test_sample_count_age_and_diagnostics_preserved(self):
        self.report([dict(beacon_id='bcn-1601', rssi=-55, sample_count=4, age_ms=15000)],
                    sampled_at=self.clock().isoformat(), scan_status='ok')
        self.assertEqual(self.view()['reported_sample_count'], 4)
        self.assertEqual(self.view()['age_seconds'], 15)
        self.clock.advance(16)
        self.assertEqual(self.view()['location_status'], 'stale')
        self.report([dict(beacon_id='bcn-1601', rssi=-55, sample_count=4, age_ms=31000)])
        self.assertEqual(self.view()['location_reason'], 'samples_expired')

    def test_readonly_token_is_required_and_cannot_use_admin_api(self):
        self.assertEqual(self.c.get('/integration/twin/locations').status_code, 401)
        self.assertEqual(self.c.get('/integration/twin/locations', headers=self.h).status_code, 401)
        self.assertEqual(self.c.get('/admin/terminal-config', headers=self.th).status_code, 401)
        self.assertEqual(self.c.post('/backup/run', headers=self.th).status_code, 401)
        no_key = TestClient(create_app(self.svc, self.voice, api_key='admin-test'))
        self.assertEqual(no_key.get('/integration/twin/locations', headers=self.th).status_code, 503)
        reused = TestClient(create_app(self.svc, self.voice, api_key='admin-test', twin_key='admin-test'))
        self.assertEqual(reused.get('/integration/twin/locations', headers=self.h).status_code, 503)

    def test_location_on_ack_topic_rejected(self):
        self.send(P.t_ack(S, 'call'), 'w-01', P.LOCATION_REPORT, dict(readings=[dict(beacon_id='bcn-1601', rssi=-50)]))
        self.assertEqual(self.view()['location_reason'], 'not_observed')

    def test_bad_metadata_does_not_corrupt_existing_fix(self):
        self.report()
        for payload in [dict(sample_count=0), dict(age_ms=-1)]:
            self.report([dict(beacon_id='bcn-1601', rssi=-50, **payload)])
        self.assertEqual(self.view()['location_status'], 'fresh')
        self.assertEqual(self.svc.rejected_count, 2)

    def test_report_interval_defines_bounded_expiry(self):
        self.report(report_interval_s=60)
        self.clock.advance(120); self.online('w-01')
        self.assertEqual(self.view()['location_status'], 'fresh')
        self.assertEqual(self.view()['stale_after_seconds'], 180)
        self.clock.advance(61); self.online('w-01')
        self.assertEqual(self.view()['location_status'], 'stale')

    def test_locator_never_selects_a_beacon_missing_from_current_report(self):
        loc = Locator({'a':dict(room='1'), 'b':dict(room='2')})
        loc.update('w', [dict(beacon_id='a', rssi=-30)])
        fix = loc.update('w', [dict(beacon_id='b', rssi=-70)])
        self.assertEqual(fix['beacon'], 'b')
