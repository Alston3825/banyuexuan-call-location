"""Dispatch/escalation rules over Core. Time is always passed in (testable).

Escalation timings are trial values from V0.1, not approved policy.
Notifications are rows only; real delivery to watches is a later adapter.
Stages are recomputed from elapsed time and the *current* roster, so a shift
change re-routes unclaimed calls without special handling.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, time, timedelta, timezone

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai
DAY_START, NIGHT_START = time(8, 30), time(18, 0)
STATION = 'station'  # nurse-station queue; the never-silent fallback


def shift_of(at):
    """Day [08:30,18:00), night otherwise. Night before 08:30 belongs to the
    previous business date."""
    local = at.astimezone(TZ)
    t = local.time()
    if DAY_START <= t < NIGHT_START:
        return 'day', local.date().isoformat()
    day = local.date() if t >= NIGHT_START else local.date() - timedelta(days=1)
    return 'night', day.isoformat()


class Roster:
    """Minimal in-memory snapshot. Real data comes from the management system."""

    def __init__(self, groups, assignments, leaders):
        self.groups = groups            # group_id -> [staff]
        self.assignments = assignments  # room -> {'day': staff, 'night': staff}
        self.leaders = leaders          # group_id -> staff
        self.status = {}                # staff -> available|busy|offline

    def set_status(self, staff, status):
        if status not in ('available', 'busy', 'offline'):
            raise ValueError('bad status')
        self.status[staff] = status

    def state(self, staff):
        return self.status.get(staff, 'offline')  # unknown is not available

    def group_of(self, staff):
        return next((g for g, m in self.groups.items() if staff in m), None)


class Dispatcher:
    def __init__(self, core, roster, group_after=15, leader_after=30):
        self.core, self.roster = core, roster
        self.group_after, self.leader_after = group_after, leader_after
        with closing(sqlite3.connect(core.path)) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS notifications (
                call_id TEXT NOT NULL, target TEXT NOT NULL, stage INTEGER NOT NULL,
                reason TEXT NOT NULL, at TEXT NOT NULL,
                PRIMARY KEY(call_id, target))""")
            db.commit()

    def _created(self, db, call_id):
        r = db.execute('SELECT at FROM events WHERE call_id=? AND version=1',
                       (call_id,)).fetchone()
        return datetime.fromisoformat(r[0])

    def targets(self, room, priority, elapsed, now):
        """Return [(target, reason)] that should have been notified by now."""
        shift, _ = shift_of(now)
        primary = self.roster.assignments.get(room, {}).get(shift)
        group = self.roster.group_of(primary) if primary else None
        members = [m for m in self.roster.groups.get(group, []) if m != primary]
        avail = lambda s: self.roster.state(s) == 'available'
        out = []
        if priority == 'emergency':
            if primary and self.roster.state(primary) != 'offline':
                out.append((primary, 'primary'))
            out += [(m, 'group') for m in members if self.roster.state(m) != 'offline']
            out.append((STATION, 'emergency'))
            return out
        if primary and self.roster.state(primary) != 'offline':
            out.append((primary, 'primary'))
        if not primary or not avail(primary) or elapsed >= self.group_after:
            out += [(m, 'group') for m in members if avail(m)]
        if elapsed >= self.leader_after:
            lead = self.roster.leaders.get(group)
            if lead and lead != primary and self.roster.state(lead) != 'offline':
                out.append((lead, 'leader'))
            out.append((STATION, 'timeout'))
        if not out:
            out.append((STATION, 'no_available_staff'))  # never drop silently
        return out

    def tick(self, now):
        """Idempotent; returns newly created notifications."""
        sent = []
        with closing(sqlite3.connect(self.core.path, timeout=10)) as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT id,request FROM calls WHERE state='pending'").fetchall()
            for call_id, request in rows:
                req = json.loads(request)
                elapsed = (now - self._created(db, call_id)).total_seconds()
                for target, reason in self.targets(req['room'], req['priority'], elapsed, now):
                    cur = db.execute('INSERT OR IGNORE INTO notifications VALUES(?,?,?,?,?)',
                                     (call_id, target, 0, reason, now.isoformat()))
                    if cur.rowcount:
                        sent.append((call_id, target, reason))
            db.commit()
        return sent

    def notified(self, call_id):
        with closing(sqlite3.connect(self.core.path)) as db:
            return [tuple(r) for r in db.execute(
                'SELECT target,reason FROM notifications WHERE call_id=? ORDER BY rowid',
                (call_id,))]
