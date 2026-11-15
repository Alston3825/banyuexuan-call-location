"""Device status, location trail and vitals. Vitals never leave the store host."""
from __future__ import annotations

import sqlite3
from contextlib import closing


class Telemetry:
    def __init__(self, path):
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS device_status(
                device TEXT PRIMARY KEY, online INTEGER NOT NULL, battery INTEGER,
                wifi_rssi INTEGER, last_seen TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS locations(
                id INTEGER PRIMARY KEY AUTOINCREMENT, watch TEXT NOT NULL,
                staff TEXT, beacon TEXT, area TEXT, room TEXT, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS vitals(
                id INTEGER PRIMARY KEY AUTOINCREMENT, staff TEXT NOT NULL, watch TEXT NOT NULL,
                at TEXT NOT NULL, heart_rate INTEGER, spo2 INTEGER, temperature REAL, steps INTEGER);
            """)
            db.execute("CREATE TABLE IF NOT EXISTS battery_samples(id INTEGER PRIMARY KEY AUTOINCREMENT, store TEXT, staff TEXT, watch TEXT, at TEXT, battery INTEGER, charging INTEGER)")
            if 'charging' not in [r[1] for r in db.execute('PRAGMA table_info(device_status)')]:
                db.execute('ALTER TABLE device_status ADD COLUMN charging INTEGER')    # 老库升级
            db.commit()

    def _run(self, sql, args=()):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(sql, args).fetchall()
            db.commit()
            return [dict(r) for r in rows]

    def heartbeat(self, device, at, battery=None, wifi_rssi=None, charging=None):
        c = None if charging is None else int(bool(charging))
        self._run('INSERT INTO device_status(device,online,battery,wifi_rssi,last_seen,charging) VALUES(?,1,?,?,?,?) '
                  'ON CONFLICT(device) DO UPDATE SET online=1,battery=excluded.battery,'
                  'wifi_rssi=excluded.wifi_rssi,last_seen=excluded.last_seen,charging=excluded.charging',
                  (device, battery, wifi_rssi, at, c))

    def set_online(self, device, online, at):
        self._run('INSERT INTO device_status(device,online,last_seen) VALUES(?,?,?) '
                  'ON CONFLICT(device) DO UPDATE SET online=excluded.online', (device, int(online), at))

    def devices(self):
        return self._run('SELECT * FROM device_status ORDER BY device')

    def add_location(self, watch, staff, fix, at):
        last = self._run('SELECT beacon FROM locations WHERE watch=? ORDER BY id DESC LIMIT 1', (watch,))
        if last and last[0]['beacon'] == (fix or {}).get('beacon'):
            return False
        self._run('INSERT INTO locations(watch,staff,beacon,area,room,at) VALUES(?,?,?,?,?,?)',
                  (watch, staff, (fix or {}).get('beacon'), (fix or {}).get('area'),
                   (fix or {}).get('room'), at))
        return True

    def latest_locations(self):
        return self._run('SELECT l.* FROM locations l JOIN (SELECT watch, MAX(id) m FROM locations '
                         'GROUP BY watch) x ON l.id=x.m ORDER BY l.watch')

    def add_vitals(self, staff, watch, at, v):
        self._run('INSERT INTO vitals(staff,watch,at,heart_rate,spo2,temperature,steps) '
                  'VALUES(?,?,?,?,?,?,?)', (staff, watch, at, v.get('heart_rate'), v.get('spo2'),
                                            v.get('temperature'), v.get('steps')))

    def vitals(self, staff, limit=100):
        return self._run('SELECT * FROM vitals WHERE staff=? ORDER BY id DESC LIMIT ?', (staff, limit))

    def stale_online(self, before_iso):
        return [r['device'] for r in self._run(
            'SELECT device FROM device_status WHERE online=1 AND last_seen<?', (before_iso,))]
