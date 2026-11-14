"""Pull master data from the management system and derive the live StoreConfig/Roster.

The management system is the single source of truth (spec §1). IDs are stored exactly as
given (cuid); human-readable numbers (room_no, employee_no) are only used for display.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

KINDS = {  # kind -> (endpoint, key field)
    'stores': ('stores', 'store_id'),
    'rooms': ('rooms', 'room_id'),
    'staff': ('staff', 'staff_id'),
    'stays': ('stays', 'stay_id'),
    'service_codes': ('service-codes', 'service_code'),
}
STAY_STATUS = 'CHECKED_IN,RESERVED'
# Assumption, to confirm with the management system: roles that count as 组长.
LEADER_ROLES = ('HEAD_NURSE', 'NURSE_LEADER', 'NURSE_MANAGER')


class MasterSync:
    def __init__(self, client, db_path, cfg, roster, leader_roles=LEADER_ROLES):
        self.client, self.path, self.cfg, self.roster = client, str(db_path), cfg, roster
        self.leader_roles = set(leader_roles)
        self.local_beacons = dict(cfg.beacons)      # corridor/station beacons are not in mgmt
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS master(kind TEXT, key TEXT, data TEXT,
                PRIMARY KEY(kind,key));
                CREATE TABLE IF NOT EXISTS sync_meta(k TEXT PRIMARY KEY, v TEXT);""")
            db.commit()

    def _meta(self, db, k, default=None):
        r = db.execute('SELECT v FROM sync_meta WHERE k=?', (k,)).fetchone()
        return r[0] if r else default

    def sync(self, kinds=None, full=False):
        """Incremental by default; full=True drops local rows first (catches removals)."""
        kinds = kinds or list(KINDS)
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            for kind in kinds:
                endpoint, key = KINDS[kind]
                # service codes are tiny: always pull in full (avoids same-timestamp page-boundary loss)
                since = None if (full or kind == 'service_codes') else self._meta(db, 'since:' + kind)
                extra = {'status': STAY_STATUS} if kind == 'stays' else {}
                sid = None if kind == 'service_codes' else self.cfg.store_id
                items, new = self.client.pull(endpoint, store_id=sid, updated_since=since, **extra)
                if kind == 'stores':
                    items = [i for i in items if i.get('store_id') == self.cfg.store_id]
                if full or kind == 'service_codes':
                    db.execute('DELETE FROM master WHERE kind=?', (kind,))
                for it in items:
                    db.execute('INSERT OR REPLACE INTO master VALUES(?,?,?)',
                               (kind, it[key], json.dumps(it, ensure_ascii=False)))
                if new:
                    db.execute('INSERT OR REPLACE INTO sync_meta VALUES(?,?)', ('since:' + kind, new))
                db.commit()
        return self.apply()

    def _rows(self, db, kind):
        return [json.loads(r[0]) for r in db.execute('SELECT data FROM master WHERE kind=?', (kind,))]

    def apply(self):
        """Rebuild cfg/roster in place from stored master data."""
        cfg, roster = self.cfg, self.roster
        with closing(sqlite3.connect(self.path)) as db:
            rooms, staff = self._rows(db, 'rooms'), self._rows(db, 'staff')
            codes = self._rows(db, 'service_codes')
        cfg.rooms.clear()
        cfg.beacons.clear()
        cfg.beacons.update(self.local_beacons)
        for r in rooms:
            if r.get('store_id') != cfg.store_id:
                continue
            cfg.rooms[r['room_id']] = dict(floor=r.get('floor'), room_no=r.get('room_no'),
                                           tablet=r.get('tablet_device_id'), stay_id=r.get('current_stay_id'),
                                           status=r.get('status'))
            for b in r.get('beacon_ids') or []:
                cfg.beacons[b] = dict(area=f"{r.get('room_no')} 房内", room=r['room_id'])
        cfg.staff.clear()
        groups, leaders = {}, {}
        for s in staff:
            if s.get('store_id') != cfg.store_id or not s.get('watch_id') or not s.get('employed', True):
                continue
            on_duty = s.get('duty_status') == 'ON_DUTY'
            cfg.staff[s['staff_id']] = dict(name=s.get('name'), watch=s['watch_id'], roles=s.get('roles') or [],
                                            floors=s.get('responsible_floors') or [], on_duty=on_duty,
                                            employee_no=s.get('employee_no'))
            if not on_duty:
                continue                       # only on-duty staff are ever dispatched
            for fl in cfg.staff[s['staff_id']]['floors']:
                groups.setdefault(fl, []).append(s['staff_id'])
                if self.leader_roles & set(cfg.staff[s['staff_id']]['roles']):
                    leaders.setdefault(fl, s['staff_id'])
        roster.groups.clear(); roster.groups.update(groups)
        roster.leaders.clear(); roster.leaders.update(leaders)
        roster.assignments.clear()             # mgmt has no per-room primary nurse; floor group is used
        roster.room_group.clear()
        roster.room_group.update({rid: v['floor'] for rid, v in cfg.rooms.items() if v['floor'] in groups})
        if codes:
            cfg.request_types.clear()
            cfg.request_types.update({c['service_code']: c.get('name', c['service_code']) for c in codes})
        return dict(rooms=len(cfg.rooms), staff=len(cfg.staff), groups={k: len(v) for k, v in groups.items()},
                    beacons=len(cfg.beacons), service_codes=len(cfg.request_types))
