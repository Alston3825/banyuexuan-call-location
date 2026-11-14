import gzip, os, sqlite3, tempfile, time, unittest
from datetime import datetime
from bycall.core import backup as B
from bycall.core.workflow import Core


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, 'host.db')
        self.dest = os.path.join(self.tmp.name, 'bk')
        self.core = Core(self.db)
        self.cid = self.core.create(store='gx', device='t', event_id='e1', room='1601',
                                    priority='normal', snapshot={})

    def tearDown(self):
        self.tmp.cleanup()

    def count(self, path):
        with sqlite3.connect(path) as db:
            return db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]

    def test_backup_while_open_and_restore(self):
        f = B.backup(self.db, self.dest, now=datetime(2026, 11, 10, 3))
        self.assertTrue(f.endswith('host-20261110-030000.db.gz'))
        self.assertEqual(os.listdir(self.dest), ['host-20261110-030000.db.gz'])   # no temp leftovers
        self.core.create(store='gx', device='t', event_id='e2', room='1602', priority='normal', snapshot={})
        self.assertEqual(self.count(self.db), 2)
        aside = B.restore(f, self.db)
        self.assertEqual(self.count(self.db), 1)           # rolled back to the backup
        self.assertEqual(self.count(aside), 2)             # previous state kept, not deleted
        self.assertEqual(Core(self.db).get(self.cid, store='gx')['state'], 'pending')

    def test_retention_keeps_seven_days_and_never_the_last(self):
        old = []
        for d in range(1, 11):
            f = B.backup(self.db, self.dest, keep_days=7, now=datetime(2026, 11, d, 3))
            ts = time.time() - (10 - d) * 86400
            os.utime(f, (ts, ts)); old.append(f)
        B.prune(self.dest, 7)
        left = [os.path.basename(f) for f in B.list_backups(self.dest)]
        self.assertEqual(left[-1], 'host-20261110-030000.db.gz')
        self.assertEqual(len(left), 7)                    # day 4..10 within 7 days
        # 全部过期时仍保留最新一份
        for f in B.list_backups(self.dest):
            os.utime(f, (1, 1))
        B.prune(self.dest, 7)
        self.assertEqual(len(B.list_backups(self.dest)), 1)

    def test_corrupt_backup_refused_and_db_untouched(self):
        bad = os.path.join(self.tmp.name, 'bad.db.gz')
        with gzip.open(bad, 'wb') as g:
            g.write(b'not a database' * 100)
        with self.assertRaises(B.BackupError):
            B.restore(bad, self.db)
        open(os.path.join(self.tmp.name, 'trunc.db.gz'), 'wb').write(b'\x1f\x8b junk')
        with self.assertRaises(B.BackupError):
            B.restore(os.path.join(self.tmp.name, 'trunc.db.gz'), self.db)
        self.assertEqual(self.count(self.db), 1)


if __name__ == '__main__':
    unittest.main()


class TestStoreTime(unittest.TestCase):
    def test_default_name_uses_store_timezone(self):
        import re
        from datetime import timezone, timedelta
        tmp = tempfile.TemporaryDirectory()
        db = os.path.join(tmp.name, 'h.db'); Core(db)
        f = B.backup(db, os.path.join(tmp.name, 'bk'))
        stamp = re.search(r'host-(\d{8}-\d{6})', f).group(1)
        want = datetime.now(timezone(timedelta(hours=8)))
        got = datetime.strptime(stamp, '%Y%m%d-%H%M%S')
        self.assertLess(abs((want.replace(tzinfo=None) - got).total_seconds()), 5)   # 与 UTC+8 一致,而不是本机时区
        tmp.cleanup()
