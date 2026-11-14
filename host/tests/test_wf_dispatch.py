import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from bycall.core.workflow import Core
from bycall.core.dispatcher import Dispatcher, Roster, STATION, TZ, shift_of


class ShiftTest(unittest.TestCase):
    def test_boundaries(self):
        d = lambda h, m, day=27: datetime(2026, 10, day, h, m, tzinfo=TZ)
        self.assertEqual(('day', '2026-10-27'), shift_of(d(8, 30)))
        self.assertEqual(('night', '2026-10-26'), shift_of(d(8, 29)))
        self.assertEqual(('night', '2026-10-27'), shift_of(d(18, 0)))
        self.assertEqual(('night', '2026-10-27'), shift_of(d(23, 59)))
        self.assertEqual(('night', '2026-10-27'), shift_of(d(0, 5, 28)))


class DispatchTest(unittest.TestCase):
    def setUp(self):
        t = TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.core = Core(Path(t.name) / 'd.sqlite3')
        self.roster = Roster(
            groups={'g1': ['a', 'b', 'c']}, leaders={'g1': 'c'},
            assignments={'R1': {'day': 'a', 'night': 'b'}})
        for s in 'abc':
            self.roster.set_status(s, 'available')
        self.d = Dispatcher(self.core, self.roster)
        self.now = datetime.now(TZ).replace(hour=10, minute=0, second=0, microsecond=0)

    def call(self, priority='normal', room='R1'):
        cid = self.core.create(store='S', device='btn', event_id=str(id(object())) + priority + room,
                               room=room, priority=priority, snapshot={})
        return cid

    def tick(self, cid, sec):
        # events.at uses wall clock; shift creation time to the test clock
        import sqlite3
        with sqlite3.connect(self.core.path) as db:
            db.execute('UPDATE events SET at=? WHERE call_id=? AND version=1',
                       (self.now.isoformat(), cid))
        return self.d.tick(self.now + timedelta(seconds=sec))

    def test_normal_escalates_in_stages(self):
        c = self.call()
        self.assertEqual([(c, 'a', 'primary')], self.tick(c, 0))
        self.assertEqual({(c, 'b', 'group'), (c, 'c', 'group')}, set(self.tick(c, 15)))
        later = self.tick(c, 30)
        self.assertIn((c, STATION, 'timeout'), later)
        self.assertEqual([], self.d.tick(self.now + timedelta(seconds=30)))  # idempotent

    def test_busy_primary_goes_to_group_immediately(self):
        self.roster.set_status('a', 'busy')
        c = self.call()
        got = {t for _, t, _ in self.tick(c, 0)}
        self.assertEqual({'a', 'b', 'c'}, got)

    def test_nobody_available_reaches_station(self):
        for s in 'abc':
            self.roster.set_status(s, 'offline')
        c = self.call()
        self.assertEqual([(c, STATION, 'no_available_staff')], self.tick(c, 0))

    def test_unassigned_room_not_dropped(self):
        c = self.call(room='R9')
        self.assertEqual([(c, STATION, 'no_available_staff')], self.tick(c, 0))

    def test_emergency_notifies_everyone_at_once(self):
        c = self.call('emergency')
        got = {t for _, t, _ in self.tick(c, 0)}
        self.assertEqual({'a', 'b', 'c', STATION}, got)

    def test_claim_stops_escalation(self):
        c = self.call()
        self.tick(c, 0)
        self.core.act(c, store='S', actor='a', expected_version=1, action='claim')
        self.assertEqual([], self.d.tick(self.now + timedelta(seconds=60)))

    def test_shift_change_reroutes_unclaimed(self):
        c = self.call()
        self.tick(c, 0)  # day -> a
        self.roster.set_status('b', 'available')
        night = self.now.replace(hour=18, minute=0) + timedelta(seconds=1)
        got = self.d.tick(night)
        self.assertIn((c, 'b', 'primary'), got)  # night primary notified


if __name__ == '__main__':
    unittest.main()
