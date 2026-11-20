from fastapi.testclient import TestClient
from bycall.api.app import create_app
from bycall.core import protocol as P
from bycall.core.location_state import LocationReports
from bycall.core.screen_state import ScreenLocationState
from bycall.core.voice import VoiceStore
from tests.test_service import Base,S


class TestDualScreens(Base):
    def setUp(self):
        super().setUp()
        self.c=TestClient(create_app(self.svc,VoiceStore(self.tmp.name+'/v'),api_key='admin',twin_key='twin'))
        self.online('w-01')

    def report(self,readings=None):
        self.send(P.t_location(S,'w-01'),'w-01',P.LOCATION_REPORT,
                  dict(readings=readings if readings is not None else [dict(beacon_id='bcn-1601',rssi=-50)]))

    def text(self):return self.c.get('/station/snapshot',headers={'X-Api-Key':'admin'}).json()
    def twin(self):return self.c.get('/integration/twin/locations',headers={'X-Api-Key':'twin'}).json()

    def test_both_screens_receive_identical_authoritative_location_envelope(self):
        self.report()
        text_response=self.c.get('/station/snapshot',headers={'X-Api-Key':'admin'})
        twin_response=self.c.get('/integration/twin/locations',headers={'X-Api-Key':'twin'})
        self.assertEqual(text_response.headers['cache-control'],'no-store')
        self.assertEqual(twin_response.headers['cache-control'],'no-store')
        text=text_response.json();twin=twin_response.json()
        self.assertEqual(text['location_state'],twin)
        person=next(p for p in twin['items'] if p['watch']=='w-01')
        card=next(p for p in text['watches'] if p['sn']=='w-01')
        self.assertEqual(person['zone_id'],card['location_zone_id'])
        self.assertEqual(person['area'],card['location_area'])
        self.assertEqual(person['valid_until'],card['location_valid_until'])
        self.assertEqual(twin['delivery'],'full')
        self.assertEqual(twin['consumer_timeout_seconds'],8)

    def test_age_only_does_not_change_revision_but_new_observation_does(self):
        self.report();a=self.twin()
        self.clock.advance(1);b=self.twin()
        self.assertEqual(a['sequence'],b['sequence'])
        self.assertEqual(a['revision'],b['revision'])
        self.assertNotEqual(a['server_time'],b['server_time'])
        self.report();c=self.twin()
        self.assertGreater(c['sequence'],b['sequence'])
        self.assertNotEqual(c['revision'],b['revision'])

    def test_exact_expiry_has_the_same_result_for_both_screens(self):
        self.report();a=self.twin()
        self.clock.advance(30);self.online('w-01')
        text=self.text();twin=self.twin()
        self.assertEqual(text['location_state'],twin)
        p=next(x for x in twin['items'] if x['watch']=='w-01')
        self.assertEqual(p['location_status'],'stale')
        self.assertIsNone(p['zone_id']);self.assertIsNone(p['valid_until'])
        self.assertGreater(twin['sequence'],a['sequence'])

    def test_unknown_observation_changes_revision_and_clears_shared_zone(self):
        self.report();a=self.twin();self.report([]);b=self.twin()
        self.assertGreater(b['sequence'],a['sequence'])
        p=next(x for x in b['items'] if x['watch']=='w-01')
        self.assertEqual(p['location_status'],'unknown');self.assertIsNone(p['zone_id'])
        self.assertEqual(self.text()['location_state'],b)

    def test_boot_epoch_prevents_replaying_pre_restart_positions(self):
        self.report();a=self.twin()
        self.svc.location_reports=LocationReports(self.svc.core.path,S,'new-boot')
        self.svc.screen_locations=ScreenLocationState('new-boot')
        b=self.twin()
        self.assertNotEqual(a['source_epoch'],b['source_epoch'])
        self.assertEqual(b['sequence'],1)
        p=next(x for x in b['items'] if x['watch']=='w-01')
        self.assertEqual(p['location_reason'],'host_restarted');self.assertIsNone(p['zone_id'])

    def test_location_revision_does_not_depend_on_vitals_or_private_person_names(self):
        self.report();a=self.twin()
        self.send(P.t_vitals(S,'w-01'),'w-01',P.VITALS_REPORT,dict(heart_rate=78))
        b=self.twin()
        self.assertEqual(a['revision'],b['revision'])
        self.assertNotIn('heart_rate',b['items'][0]);self.assertNotIn('name',b['items'][0])
