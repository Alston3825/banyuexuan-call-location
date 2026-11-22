import os

from fastapi.testclient import TestClient
from bycall.api.app import create_app
from bycall.core import protocol as P
from bycall.core.voice import VoiceStore
from tests.test_service import Base, S


class TestTwinCalls(Base):
    def setUp(self):
        super().setUp()
        self.online('w-01', 'w-02')
        self.voice = VoiceStore(os.path.join(self.tmp.name, 'voice'))
        self.c = TestClient(create_app(self.svc, self.voice, api_key='admin-test', twin_key='twin-test'))
        self.th = {'X-Api-Key': 'twin-test'}

    def get(self):
        r = self.c.get('/integration/twin/calls', headers=self.th)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers['cache-control'], 'no-store')
        return r.json()

    def test_auth_rules_match_location_projection(self):
        self.assertEqual(self.c.get('/integration/twin/calls').status_code, 401)
        self.assertEqual(self.c.get('/integration/twin/calls', headers={'X-Api-Key': 'admin-test'}).status_code, 401)
        no_key = TestClient(create_app(self.svc, self.voice, api_key='admin-test'))
        self.assertEqual(no_key.get('/integration/twin/calls', headers=self.th).status_code, 503)
        reused = TestClient(create_app(self.svc, self.voice, api_key='admin-test', twin_key='admin-test'))
        self.assertEqual(reused.get('/integration/twin/calls', headers={'X-Api-Key': 'admin-test'}).status_code, 503)

    def test_lifecycle_states_and_sequence(self):
        empty = self.get()
        self.assertEqual((empty['schema'], empty['delivery'], empty['items']), ('bycall.call.v1', 'full', []))
        cid = self.call()
        a = self.get()
        item = a['items'][0]
        self.assertEqual((item['call_id'], item['room'], item['floor'], item['state']), (cid, '1601', '16', 'pending'))
        self.assertTrue(item['label'])
        self.assertGreater(a['sequence'], empty['sequence'])
        self.assertEqual(self.get()['sequence'], a['sequence'])  # no factual change, no new sequence
        self.accept('w-01', cid)
        b = self.get()['items'][0]
        self.assertEqual((b['state'], b['owner_staff']), ('claimed', 'n01'))
        self.assertTrue(b['owner_name'] and b['claimed_at'])
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_ARRIVE, {}, call_id=cid)
        self.assertEqual(self.get()['items'][0]['state'], 'arrived')
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_COMPLETE, dict(method='onsite'), call_id=cid)
        done = self.get()['items'][0]
        self.assertEqual(done['state'], 'completed')
        self.assertTrue(done['ended_at'])
        self.clock.advance(61)
        self.assertEqual(self.get()['items'], [])  # ended calls leave the 3D scene after 60 s

    def test_cancel_and_transfer_are_visible(self):
        cid = self.call()
        self.accept('w-01', cid)
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_TRANSFER, dict(target='n02', reason='忙'), call_id=cid)
        it = self.get()['items'][0]
        self.assertTrue(it['transferred'] and it['transfer_pending'])
        self.accept('w-02', cid)
        it = self.get()['items'][0]
        self.assertEqual((it['owner_staff'], it['transfer_pending']), ('n02', False))
        cid2 = self.call('1602')
        self.send(P.t_ack(S, cid2), 'tab-1602', P.CALL_CANCEL, {}, call_id=cid2)
        states = {i['call_id']: i['state'] for i in self.get()['items']}
        self.assertEqual(states[cid2], 'cancelled')

    def test_projection_has_no_voice_text_or_customer_content(self):
        self.call()
        item = self.get()['items'][0]
        for key in ('voice_url', 'transcript', 'text', 'request', 'device', 'api_key'):
            self.assertNotIn(key, item)

    def test_location_projection_carries_display_name_and_role(self):
        loc = self.c.get('/integration/twin/locations', headers=self.th).json()
        item = next(i for i in loc['items'] if i['staff'] == 'n01')
        self.assertEqual(item['display_name'], self.svc.cfg.name('n01'))
        self.assertEqual(item['role'], 'nurse')
        self.assertTrue(item['role_name'])
        for key in ('heart_rate', 'battery', 'last_known'):
            self.assertNotIn(key, item)
