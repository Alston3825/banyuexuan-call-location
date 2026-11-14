import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from prototype.core import Core, Conflict


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.core = Core(self.path)
        self.args = dict(store='A', device='button', event_id='1', room='room1',
                         priority='normal', snapshot={'shift': 'day', 'stay': 'fake-stay'})
        self.call = self.core.create(**self.args)

    def act(self, action, version, actor='nurse1', **extra):
        return self.core.act(self.call, store='A', actor=actor, action=action,
                             expected_version=version, **extra)

    def test_event_replay_and_changed_content(self):
        self.assertEqual(self.call, self.core.create(**self.args))
        with self.assertRaises(Conflict):
            self.core.create(**dict(self.args, room='other'))
        self.assertEqual(1, len(self.core.events()))

    def test_concurrent_claim_has_one_winner(self):
        def claim(actor):
            try:
                return self.act('claim', 1, actor=actor)['owner']
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, ['nurse' + str(i) for i in range(8)]))
        self.assertEqual(1, sum(r is not None for r in results))
        self.assertEqual(2, len(self.core.events()))

    def test_reopen_persists_and_snapshot_is_immutable(self):
        self.args['snapshot']['shift'] = 'night'
        core = Core(self.path)
        task = core.get(self.call, store='A')
        self.assertEqual('day', json.loads(task['request'])['snapshot']['shift'])
        self.assertEqual(1, len(core.events()))

    def test_onsite_requires_arrival_and_failed_change_rolls_back(self):
        self.act('claim', 1)
        with self.assertRaises(Conflict):
            self.act('complete', 2, method='onsite', reason='done')
        self.assertEqual(2, len(self.core.events()))
        with self.assertRaises(Conflict):
            self.act('arrive', 2, method='ble')
        self.act('arrive', 2, method='manual')
        task = self.act('complete', 3, method='onsite', reason='done')
        self.assertEqual('completed', task['state'])
        with self.assertRaises(Conflict):
            self.act('arrive', 4, method='manual')

    def test_remote_resolution_without_arrival(self):
        self.act('claim', 1)
        self.assertEqual('completed', self.act('complete', 2, method='remote', reason='电话解决')['state'])

    def test_transfer_requires_acceptance_and_new_arrival(self):
        self.act('claim', 1)
        self.act('arrive', 2, method='manual')
        proposed = self.act('propose_transfer', 3, target='nurse2', reason='交班')
        self.assertEqual('nurse1', proposed['owner'])
        with self.assertRaises(Conflict):
            self.act('accept_transfer', 4, actor='nurse3')
        accepted = self.act('accept_transfer', 4, actor='nurse2')
        self.assertEqual(('nurse2', 'claimed'), (accepted['owner'], accepted['state']))
        with self.assertRaises(Conflict):
            self.act('complete', 5, actor='nurse1', method='remote', reason='late')
        self.assertEqual('nurse1', json.loads(self.core.events()[-1]['payload'])['previous_owner'])

    def test_cross_store_access_and_stale_command(self):
        with self.assertRaises(KeyError):
            self.core.get(self.call, store='B')
        with self.assertRaises(KeyError):
            self.core.act(self.call, store='B', actor='nurse1', expected_version=1, action='claim')
        self.act('claim', 1)
        with self.assertRaises(Conflict):
            self.act('claim', 1)
        self.assertEqual(2, len(self.core.events()))

    def test_outbox_cursor_and_atomic_versions(self):
        first = self.core.events()[0]['seq']
        self.act('claim', 1)
        self.assertEqual([2], [e['version'] for e in self.core.events(after=first)])
        self.assertEqual([], self.core.events(after=self.core.events()[-1]['seq']))


if __name__ == '__main__':
    unittest.main()
