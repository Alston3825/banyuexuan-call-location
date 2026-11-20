"""Current observations are separate from movement history; stale fixes are never current positions."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta


class LocationReports:
    def __init__(self, path, store, epoch):
        self.path, self.store, self.epoch = str(path), store, epoch
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS location_current(
                store TEXT, watch TEXT, staff TEXT, binding TEXT, epoch TEXT, received_at TEXT,
                fix TEXT, last_known TEXT, diagnostics TEXT, PRIMARY KEY(store,watch))''')
            db.commit()

    def rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT * FROM location_current WHERE store=?', (self.store,)).fetchall()
        out = {}
        for r in rows:
            r = dict(r)
            for key in ('fix', 'last_known', 'diagnostics'):
                r[key] = json.loads(r[key]) if r[key] else None
            out[r['watch']] = r
        return out

    def record(self, watch, staff, binding, fix, diagnostics, at):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT staff,binding,last_known FROM location_current WHERE store=? AND watch=?',
                             (self.store, watch)).fetchone()
        last = json.loads(row[2]) if row and row[0] == staff and row[1] == binding and row[2] else None
        if fix:
            last = dict(fix, at=at, staff=staff)
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute('INSERT OR REPLACE INTO location_current VALUES(?,?,?,?,?,?,?,?,?)',
                       (self.store, watch, staff, binding, self.epoch, at, json.dumps(fix), json.dumps(last), json.dumps(diagnostics)))
            db.commit()

    def describe(self, row, watch, staff, binding, now, online, beacons):
        base = dict(watch=watch, staff=staff, online=online, location_status='unknown',
                    location_reason='not_observed', precision='area', coordinates=None,
                    beacon=None, room=None, area=None, floor=None, rssi=None, at=None,
                    observed_at=None, received_at=None, age_seconds=None, stale_after_seconds=30, valid_until=None,
                    scan_status=None, scan_error_code=None, reported_beacon_count=0, reported_sample_count=0, last_known=None)
        if not row:
            return base
        if row['staff'] != staff or row['binding'] != binding:
            return dict(base, location_reason='binding_changed')
        diag = row.get('diagnostics') or {}
        ttl = max(30, min(180, int(diag.get('report_interval_s') or 10) * 3))
        try:
            age = (now - datetime.fromisoformat(row['received_at'])).total_seconds()
            if row.get('fix'):
                age += (diag.get('fix_age_ms') or 0) / 1000
        except (ValueError, TypeError):
            age = float('inf')
        base.update(received_at=row['received_at'], age_seconds=round(max(0, age), 1) if age != float('inf') else None,
                    stale_after_seconds=ttl, scan_status=diag.get('scan_status'),
                    scan_error_code=diag.get('scan_error_code'), last_known=row.get('last_known'),
                    reported_beacon_count=len(diag.get('readings', [])),
                    reported_sample_count=sum(r.get('sample_count') or 1 for r in diag.get('readings', [])))
        reason = ('host_restarted' if row['epoch'] != self.epoch else 'offline' if not online else
                  'expired' if age >= ttl or age < -5 else None)
        if reason:
            return dict(base, location_status='stale', location_reason=reason)
        fix = row.get('fix')
        if not fix:
            return dict(base, location_reason=diag.get('reason', 'no_fix'))
        mapped = beacons.get(fix['beacon'])
        if not mapped or any(mapped.get(k) != fix.get(k) for k in ('room', 'area', 'floor')):
            return dict(base, location_reason='mapping_changed')
        base.update({k: fix.get(k) for k in ('beacon', 'room', 'area', 'floor', 'rssi')})
        base.update(location_status='fresh', location_reason=None, at=row['received_at'],
                    observed_at=diag.get('sampled_at'),
                    valid_until=(datetime.fromisoformat(row['received_at']) + timedelta(seconds=ttl - (diag.get('fix_age_ms') or 0) / 1000)).isoformat())
        return base
