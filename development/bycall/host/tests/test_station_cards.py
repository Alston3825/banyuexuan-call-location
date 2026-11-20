from datetime import timedelta
from fastapi.testclient import TestClient
from bycall.api.app import create_app
from bycall.core import protocol as P
from bycall.core.voice import VoiceStore
from tests.test_service import Base, S


class TestStationCards(Base):
    def setUp(self):
        super().setUp()
        self.online('w-01')
        self.c = TestClient(create_app(self.svc, VoiceStore(self.tmp.name + '/v'), api_key='test'))
        self.headers = {'X-Api-Key': 'test'}

    def vitals(self, **data):
        self.send(P.t_vitals(S, 'w-01'), 'w-01', P.VITALS_REPORT, data)

    def card(self):
        snap = self.c.get('/station/snapshot', headers=self.headers).json()
        return next(w for w in snap['watches'] if w['sn'] == 'w-01')

    def test_card_shows_identity_actual_metrics_and_named_beacon_area(self):
        self.svc.cfg.beacons['bcn-1601']['area'] = '测试区域·SPA馆南侧'
        self.vitals(heart_rate=78, systolic=120, diastolic=80)
        self.send(P.t_location(S,'w-01'),'w-01',P.LOCATION_REPORT,dict(readings=[dict(beacon_id='bcn-1601',rssi=-50)]))
        v=self.card()
        self.assertEqual(v['bound_staff'],'n01')
        self.assertEqual(v['heart_rate'],78)
        self.assertEqual(v['blood_pressure'],dict(systolic=120,diastolic=80))
        self.assertEqual(v['location_area'],'测试区域·SPA馆南侧')
        self.assertEqual(v['location_status'],'fresh')

    def test_missing_pressure_never_inferred_from_heart_rate(self):
        self.vitals(heart_rate=78)
        self.assertIsNone(self.card()['blood_pressure'])
        self.assertEqual(self.card()['blood_pressure_state'],'no_data')

    def test_later_heart_rate_does_not_erase_real_pressure_sample(self):
        self.vitals(systolic=120,diastolic=80)
        self.clock.advance(10)
        self.vitals(heart_rate=81)
        v=self.card()
        self.assertEqual(v['blood_pressure'],dict(systolic=120,diastolic=80))
        self.assertEqual(v['heart_rate'],81)

    def test_old_offline_and_future_samples_are_not_current_values(self):
        self.vitals(heart_rate=78,systolic=120,diastolic=80)
        self.clock.advance(601);self.online('w-01')
        v=self.card()
        self.assertIsNone(v['heart_rate']);self.assertIsNone(v['blood_pressure'])
        self.assertEqual(v['heart_rate_state'],'stale')
        self.vitals(heart_rate=82,measured_at=(self.clock()+timedelta(hours=1)).isoformat())
        self.assertIsNone(self.card()['heart_rate'])
        self.vitals(heart_rate=84)
        self.send(P.t_status(S,'w-01'),'w-01',P.STATUS,dict(online=False))
        self.assertIsNone(self.card()['heart_rate'])
        self.assertEqual(self.card()['heart_rate_state'],'offline')

    def test_offline_duration_tracks_disconnect_and_clears_on_recovery(self):
        self.clock.advance(10)
        self.send(P.t_status(S,'w-01'),'w-01',P.STATUS,dict(online=False))
        since=self.card()['offline_since']
        self.clock.advance(20)
        self.send(P.t_status(S,'w-01'),'w-01',P.STATUS,dict(online=False))
        self.assertEqual(self.card()['offline_since'],since)
        self.online('w-01')
        self.assertIsNone(self.card()['offline_since'])

    def test_pressure_pair_and_timestamp_validation(self):
        for data in [dict(systolic=120),dict(diastolic=80),dict(systolic=80,diastolic=100),
                     dict(heart_rate=78,measured_at='2026-11-20T10:00:00')]:
            self.vitals(**data)
        self.assertEqual(self.svc.tele.vitals('n01'),[])
        self.assertEqual(self.svc.rejected_count,4)

    def test_previous_watch_measurements_not_shown_on_replacement(self):
        self.vitals(heart_rate=78,systolic=120,diastolic=80)
        # A different SN worn by the same employee must not inherit the old equipment's sample.
        reg=dict(self.svc.watches.get('w-01'),sn='replacement')
        v=self.svc.watch_vitals(reg,True)
        self.assertIsNone(v['heart_rate']);self.assertIsNone(v['blood_pressure'])
        # Same watch, new binding boundary: do not inherit earlier samples either.
        reg=dict(self.svc.watches.get('w-01'),last_bind_time=(self.clock()+timedelta(seconds=1)).isoformat())
        self.assertIsNone(self.svc.watch_vitals(reg,True)['heart_rate'])
