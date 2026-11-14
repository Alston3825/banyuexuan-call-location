import os, sqlite3, tempfile, time, unittest
from datetime import datetime
from fastapi.testclient import TestClient
from bycall.api.app import create_app
from bycall.core import backup as B, cloud as C, backupmgr as M
from bycall.core.hoststatus import HostStatus
from bycall.core.voice import VoiceStore
from bycall.core.workflow import Core
from tests.test_service import Base

TZ = B.TZ


class FailStore(C.DirStore):
    broken = True

    def put(self, local, name):
        if self.broken:
            raise C.CloudError('network down')
        super().put(local, name)


class TestMgr(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.data, self.db = t, os.path.join(t, 'host.db')
        self.local, self.cloud_dir = os.path.join(t, 'bk'), os.path.join(t, 'cloud')
        self.core = Core(self.db)
        self.core.create(store='gx', device='t', event_id='e1', room='1601', priority='normal', snapshot={})
        self.now = datetime(2026, 11, 10, 3, 5, tzinfo=TZ)

    def tearDown(self):
        self.tmp.cleanup()

    def mgr(self, store='dir', pw='secret'):
        st = C.DirStore(self.cloud_dir) if store == 'dir' else store
        return M.BackupManager(self.data, self.db, self.local, st, pw, clock=lambda: self.now)

    def calls(self, path):
        with sqlite3.connect(path) as d:
            return d.execute('SELECT COUNT(*) FROM calls').fetchone()[0]

    def test_encryption_roundtrip_and_wrong_key(self):
        a, b, c = [os.path.join(self.data, x) for x in ('a', 'b', 'c')]
        open(a, 'wb').write(b'hello' * 100)
        C.encrypt_file(a, b, 'pw')
        self.assertNotIn(b'hello', open(b, 'rb').read())
        C.decrypt_file(b, c, 'pw')
        self.assertEqual(open(c, 'rb').read(), b'hello' * 100)
        with self.assertRaises(C.CloudError):
            C.decrypt_file(b, c, 'wrong')
        with self.assertRaises(C.CloudError):
            C.encrypt_file(a, b, '')

    def test_slots_and_catchup(self):
        m = self.mgr()
        m.tick()                                        # 03:05 -> slot 03:00 runs
        self.assertEqual(len(m.listing()['local']), 1)
        m.tick()                                        # same slot: no second run
        self.assertEqual(len(m.listing()['local']), 1)
        self.now = datetime(2026, 11, 10, 9, 0, tzinfo=TZ)
        m.tick()
        self.assertEqual(len(m.listing()['local']), 2)
        # host was off over 15:00 and 21:00; at 22:30 only ONE catch-up run happens
        self.now = datetime(2026, 11, 10, 22, 30, tzinfo=TZ)
        m.tick(); m.tick()
        self.assertEqual(len(m.listing()['local']), 3)
        # a restarted manager remembers the slot (no duplicate)
        self.mgr().tick()
        self.assertEqual(len(m.listing()['local']), 3)

    def test_before_first_slot_does_not_run(self):
        self.now = datetime(2026, 11, 10, 1, 0, tzinfo=TZ)
        m = self.mgr(); m.tick()
        self.assertEqual(m.listing()['local'], [])

    def test_upload_encrypted_and_cloud_listing(self):
        m = self.mgr(); m.tick()
        L = m.listing()
        self.assertEqual(len(L['cloud']), 1)
        self.assertEqual(L['cloud'][0]['name'], L['local'][0]['name'])
        raw = open(os.path.join(self.cloud_dir, L['cloud'][0]['name'] + '.enc'), 'rb').read()
        self.assertTrue(raw.startswith(b'BYCB1'))
        self.assertIsNotNone(m.state['cloud_ok_at'])

    def test_cloud_failure_keeps_local_then_retries(self):
        fs = FailStore(self.cloud_dir)
        m = self.mgr(store=fs); m.tick()
        self.assertEqual(len(m.listing()['local']), 1)           # local unaffected
        self.assertIn('network down', m.state['cloud_error'])
        self.assertEqual(len(m.state['pending']), 1)
        fs.broken = False
        m.tick()                                                  # before retry time: nothing
        self.assertEqual(len(m.state['pending']), 1)
        m.state['next_retry'] = 0; m.tick()
        self.assertEqual((m.state['pending'], m.state['cloud_error']), ([], None))
        self.assertEqual(len(m.listing()['cloud']), 1)

    def test_no_passphrase_never_uploads_plaintext(self):
        m = self.mgr(pw=None); m.tick()
        self.assertEqual(os.listdir(self.cloud_dir), [])
        self.assertIn('口令', m.state['cloud_error'])
        self.assertTrue(m.problems())

    def test_problem_only_after_6h_of_failures(self):
        fs = FailStore(self.cloud_dir)
        m = self.mgr(store=fs); m.tick()
        self.assertIn('还没有成功过', m.problems()[0])
        m.state['cloud_ok_at'] = time.time() - 3600
        self.assertEqual(m.problems(), [])                        # failed recently only
        m.state['cloud_ok_at'] = time.time() - 7 * 3600
        self.assertIn('7 小时前', m.problems()[0])

    def test_cloud_retention_keeps_newest(self):
        m = self.mgr(); m.tick()
        old = os.path.join(self.cloud_dir, 'host-20200101-030000.db.gz.enc')
        open(old, 'wb').write(b'x'); os.utime(old, (1, 1))
        m.update_settings(cloud_keep_days=1)
        self.now = datetime(2026, 11, 10, 9, 0, tzinfo=TZ); m.tick()
        names = [o['name'] for o in m.listing()['cloud']]
        self.assertNotIn('host-20200101-030000.db.gz', names)
        self.assertEqual(len(names), 2)

    def test_settings_validation(self):
        m = self.mgr()
        m.update_settings(times=['6:30', '18:00', '6:30'])
        self.assertEqual(m.settings['times'], ['06:30', '18:00'])
        for bad in (['25:00'], [], ['abc']):
            with self.assertRaises(ValueError):
                m.update_settings(times=bad)
        with self.assertRaises(ValueError):
            m.update_settings(local_keep_days=0)
        self.assertEqual(self.mgr().settings['times'], ['06:30', '18:00'])     # persisted

    def test_restore_from_cloud_and_local(self):
        m = self.mgr(); m.tick()
        name = m.listing()['cloud'][0]['name']
        for i in range(3):
            self.core.create(store='gx', device='t', event_id=f'n{i}', room='1602', priority='normal', snapshot={})
        self.assertEqual(self.calls(self.db), 4)
        m.stage_restore('cloud', name)
        self.assertEqual(self.calls(self.db), 4)                  # nothing swapped until restart
        ok, aside = M.apply_pending(self.data, self.db)
        self.assertTrue(ok)
        self.assertEqual(self.calls(self.db), 1)
        self.assertEqual(self.calls(aside), 4)                    # old DB kept, not deleted
        self.assertIsNone(M.apply_pending(self.data, self.db))
        m.stage_restore('local', name)
        self.assertTrue(M.apply_pending(self.data, self.db)[0])

    def test_restore_rejects_bad_input(self):
        m = self.mgr(); m.tick()
        name = m.listing()['cloud'][0]['name']
        for src, n in (('local', '../x'), ('local', 'host-x.db.gz'), ('cloud', 'host-1.db.gz'), ('web', name)):
            with self.assertRaises(B.BackupError):
                m.stage_restore(src, n)
        m2 = self.mgr(pw='other')                                  # wrong passphrase
        with self.assertRaises(B.BackupError):
            m2.stage_restore('cloud', name)
        self.assertFalse(os.path.exists(os.path.join(self.data, M.PENDING)))
        # corrupt staged file at startup -> host keeps its DB
        open(os.path.join(self.data, M.PENDING), 'wb').write(b'garbage')
        ok, _ = M.apply_pending(self.data, self.db)
        self.assertFalse(ok)
        self.assertEqual(self.calls(self.db), 1)

    def test_hoststatus_includes_cloud_problem(self):
        m = self.mgr(pw=None); m.tick()
        hs = HostStatus(self.data, self.local); hs.bm = m
        self.assertTrue(any('云备份' in p for p in hs.snapshot()['problems']))


class TestBackupApi(Base):
    def setUp(self):
        super().setUp()
        d = self.tmp.name
        self.restarted = []
        self.mgr = M.BackupManager(d, os.path.join(d, 'x.db'), os.path.join(d, 'bk'),
                                   C.DirStore(os.path.join(d, 'cl')), 'pw')
        open(self.mgr.db_path, 'wb').close()
        Core(self.mgr.db_path)
        self.c = TestClient(create_app(self.svc, VoiceStore(os.path.join(d, 'v')), api_key='k',
                                       backup_mgr=self.mgr, restart=lambda: self.restarted.append(1)))
        self.h = {'x-api-key': 'k'}

    def test_flow(self):
        self.assertEqual(self.c.get('/backup/status').status_code, 401)
        self.assertEqual(self.c.get('/backup').status_code, 200)          # page itself, no secrets
        self.assertTrue(self.c.post('/backup/run', headers=self.h).json()['ok'])
        L = self.c.get('/backup/list', headers=self.h).json()
        self.assertEqual((len(L['local']), len(L['cloud'])), (1, 1))
        r = self.c.put('/backup/settings', headers=self.h, json=dict(times=['08:00', '20:00'], auto=False))
        self.assertEqual(r.json()['times'], ['08:00', '20:00'])
        self.assertEqual(self.c.put('/backup/settings', headers=self.h, json=dict(times=['99:00'])).status_code, 422)
        st = self.c.get('/backup/status', headers=self.h).json()
        self.assertTrue(st['cloud_ready'])
        body = dict(source='cloud', name=L['cloud'][0]['name'])
        self.assertEqual(self.c.post('/backup/restore', headers=self.h, json=body).status_code, 422)
        self.assertEqual(self.restarted, [])
        self.assertEqual(self.c.post('/backup/restore', headers=self.h, json={**body, 'confirm': '恢复'}).status_code, 200)
        self.assertEqual(self.restarted, [1])
        bad = self.c.post('/backup/restore', headers=self.h, json=dict(source='cloud', name='nope', confirm='恢复'))
        self.assertEqual(bad.status_code, 422)


if __name__ == '__main__':
    unittest.main()
