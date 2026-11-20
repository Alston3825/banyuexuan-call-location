import os, tempfile, unittest
from fastapi.testclient import TestClient
from bycall.api.app import create_app
from bycall.core.voice import VoiceStore
from tests.test_service import Base, S
from bycall.core import protocol as P


class TestApi(Base):
    def setUp(self):
        super().setUp()
        self.voice = VoiceStore(os.path.join(self.tmp.name, 'v'))
        self.c = TestClient(create_app(self.svc, self.voice, api_key='k'))
        self.h = {'x-api-key': 'k'}

    def test_auth_required(self):
        self.assertEqual(self.c.get('/calls').status_code, 401)
        self.assertEqual(self.c.get('/health').status_code, 200)

    def test_calls_events_voice(self):
        self.online('w-01')
        up = self.c.post('/voice?ext=wav', content=b'RIFFxxxx', headers=self.h).json()['voice_url']
        self.send(P.t_call(S, '16', '1601'), 'tab-1601', P.CALL_REQUEST,
                  dict(room='1601', service_code='NEED_NURSE', voice_url=up))
        calls = self.c.get('/calls', headers=self.h).json()
        self.assertEqual(calls[0]['request']['voice_url'], up)
        ev = self.c.get('/events?after=0', headers=self.h).json()
        self.assertEqual(ev[0]['kind'], 'call.created')
        self.assertEqual(self.c.get(up, headers=self.h).content, b'RIFFxxxx')
        self.assertEqual(self.c.get('/voice/..%2Fx', headers=self.h).status_code, 404)

    def test_watch_history(self):
        self.online('w-01')
        up = self.c.post('/voice?ext=wav', content=b'RIFFxxxx', headers=self.h).json()['voice_url']
        self.send(P.t_call(S, '16', '1601'), 'tab-1601', P.CALL_REQUEST,
                  dict(room='1601', service_code='NEED_NURSE', voice_url=up))
        cid = self.c.get('/calls', headers=self.h).json()[0]['call_id']
        self.assertEqual(self.c.get('/watches/w-01/history').status_code, 401)
        self.assertEqual(self.c.get('/watches/w-01/history', headers=self.h).json(), [])      # 没接单,没有记录
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_ACCEPT, {}, call_id=cid)
        h = self.c.get('/watches/w-01/history', headers=self.h).json()
        self.assertEqual(len(h), 1)
        self.assertEqual((h[0]['call_id'], h[0]['voice_url'], h[0]['state']), (cid, up, 'claimed'))
        self.assertEqual([e['kind'] for e in h[0]['timeline']][:1], ['call.created'])
        self.assertEqual(self.c.get('/watches/w-02/history', headers=self.h).json(), [])      # 别人看不到
        self.assertEqual(self.c.get('/watches/nope/history', headers=self.h).status_code, 404)

    def test_station_call_and_staff_history(self):
        self.online('w-01')
        self.send(P.t_call(S, '16', '1601'), 'tab-1601', P.CALL_REQUEST, dict(room='1601', service_code='NEED_NURSE'))
        cid = self.c.get('/calls', headers=self.h).json()[0]['call_id']
        self.send(P.t_ack(S, cid), 'w-01', P.CALL_ACCEPT, {}, call_id=cid)
        self.assertEqual(self.c.get(f'/station/call/{cid}').status_code, 401)
        d = self.c.get(f'/station/call/{cid}', headers=self.h).json()
        self.assertEqual((d['call_id'], d['owner']), (cid, 'n01'))
        self.assertEqual(d['timeline'][0]['kind'], 'call.created')
        self.assertTrue(d['created_at'])
        self.assertEqual(self.c.get('/station/call/nope', headers=self.h).status_code, 404)
        h = self.c.get('/station/staff/n01/history', headers=self.h).json()
        self.assertEqual((h['staff'], len(h['items'])), ('n01', 1))
        self.assertEqual(self.c.get('/station/staff/n02/history', headers=self.h).json()['items'], [])
        self.assertEqual(self.c.get('/station/staff/zz/history', headers=self.h).status_code, 404)

    def test_station_snapshot(self):
        self.online('w-01')
        self.send(P.t_call(S, '16', '1601'), 'tab-1601', P.CALL_REQUEST,
                  dict(room='1601', service_code='NEED_NURSE'))
        self.assertEqual(self.c.get('/station/snapshot').status_code, 401)
        s = self.c.get('/station/snapshot', headers=self.h).json()
        self.assertEqual(len(s['active']), 1)
        a = s['active'][0]
        self.assertEqual((a['room'], a['state']), ('1601', 'pending'))
        self.assertTrue(a['created_at'])
        self.assertNotIn('request', a)
        n = {x['staff']: x for x in s['nurses']}
        self.assertTrue(n['n01']['online'])
        self.assertFalse(n['n02']['online'])
        self.assertEqual({r['room'] for r in s['rooms']}, set(self.svc.cfg.rooms))

    def test_guest_buttons_api_limits(self):
        put = lambda labels: self.c.put('/rooms/1601/buttons', json={'labels': labels}, headers=self.h)
        self.assertEqual(self.c.put('/rooms/1601/buttons', json={'labels': []}).status_code, 401)
        r = put(['换尿布'])
        self.assertEqual([b['label'] for b in r.json()['buttons']], ['换尿布'])
        self.assertEqual(put(['a', 'b']).status_code, 400)               # 只有一个自定义按钮
        self.assertEqual(put(['这个按钮名字实在太长了啊']).status_code, 400)
        self.assertEqual(self.c.get('/rooms/1601/buttons', headers=self.h).json()['max'], 1)
        self.assertEqual(len(self.c.get('/rooms/1601/buttons', headers=self.h).json()['buttons']), 1)  # 失败不改动
        self.assertEqual(put([]).json()['buttons'], [])                  # 清除
        self.assertEqual(self.c.put('/rooms/9999/buttons', json={'labels': []}, headers=self.h).status_code, 404)

    def test_routing_api(self):
        self.assertEqual(self.c.get('/routing').status_code, 401)
        v = self.c.get('/routing', headers=self.h).json()
        self.assertEqual({i['role'] for i in v['items']}, {'nurse'})            # 默认都是护理师
        self.assertEqual(v['items'][-1]['code'], 'CUSTOM')
        r = self.c.put('/routing', json={'CUSTOM': 'cleaner'}, headers=self.h)
        self.assertEqual([i['role'] for i in r.json()['items'] if i['code'] == 'CUSTOM'], ['cleaner'])
        self.assertEqual(self.c.put('/routing', json={'CUSTOM': 'ghost'}, headers=self.h).status_code, 400)

    def test_voice_transcribe_api(self):
        from bycall.core.transcribe import Transcriber
        tr = Transcriber(os.path.join(self.tmp.name, 'h.db'), engine=lambda p: '我要红糖水')
        c = TestClient(create_app(self.svc, self.voice, api_key='k', transcriber=tr))
        h = {'X-Api-Key': 'k'}
        name = c.post('/voice?ext=m4a', content=b'x' * 3000, headers=h).json()['voice_url'].rsplit('/', 1)[-1]
        self.assertEqual(c.post(f'/voice/{name}/transcribe').status_code, 401)
        self.assertEqual(c.post('/voice/nope.m4a/transcribe', headers=h).status_code, 404)
        r = c.post(f'/voice/{name}/transcribe', headers=h).json()
        self.assertEqual((r['text'], r['cached']), ('我要红糖水', False))
        self.assertTrue(c.post(f'/voice/{name}/transcribe', headers=h).json()['cached'])
        # 没装模型:明确提示,不是 500
        c2 = TestClient(create_app(self.svc, self.voice, api_key='k', transcriber=Transcriber(os.path.join(self.tmp.name, 'h.db'), model_dir='/none')))
        n2 = c2.post('/voice?ext=m4a', content=b'y' * 3000, headers=h).json()['voice_url'].rsplit('/', 1)[-1]
        self.assertEqual(c2.post(f'/voice/{n2}/transcribe', headers=h).status_code, 503)

    def test_snapshot_host_status(self):
        from bycall.core.hoststatus import HostStatus
        hs = HostStatus(self.tmp.name, os.path.join(self.tmp.name, 'bk'))
        hs.backup_error = 'x'
        c = TestClient(create_app(self.svc, self.voice, api_key='k', host_status=hs))
        h = c.get('/station/snapshot', headers=self.h).json()['host']
        self.assertIn('备份失败', h['problems'][0])

    def test_station_page_served(self):
        r = self.c.get('/station')
        self.assertEqual(r.status_code, 200)
        self.assertIn('护士站', r.text)
        self.assertNotIn('dev-key', r.text)

    def test_voice_purge(self):
        n = self.voice.save(b'abc')
        import time
        self.assertEqual(self.voice.purge(7, now=time.time() + 8 * 86400), 1)
        self.assertIsNone(self.voice.path(n))


if __name__ == '__main__':
    unittest.main()


class TestTranscribe(unittest.TestCase):
    def test_transcribe_cached_and_unavailable(self):
        from bycall.core.transcribe import Transcriber
        tmp = tempfile.TemporaryDirectory()
        calls = []
        t = Transcriber(os.path.join(tmp.name, 't.db'), model_dir='/nonexistent')
        self.assertFalse(t.available)
        from bycall.core.transcribe import TranscribeUnavailable
        with self.assertRaises(TranscribeUnavailable):
            t.transcribe('a.m4a', '/x')
        t2 = Transcriber(os.path.join(tmp.name, 't.db'), engine=lambda p: calls.append(p) or '护理师你好')
        self.assertEqual(t2.transcribe('a.m4a', '/x'), ('护理师你好', False))
        self.assertEqual(t2.transcribe('a.m4a', '/x'), ('护理师你好', True))     # 第二次走缓存
        self.assertEqual(len(calls), 1)
        self.assertEqual(t2.cached_many(['a.m4a', 'b.m4a']), {'a.m4a': '护理师你好'})
        tmp.cleanup()
