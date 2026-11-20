"""派单送达回执、重发、送达耗时,以及手表电量曲线(供续航测试统计)。"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from typing import List, Optional


class Delivery:
    def __init__(self, path):
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS deliveries(
                call_id TEXT NOT NULL, target TEXT NOT NULL, watch TEXT, kind TEXT,
                first_sent TEXT NOT NULL, last_sent TEXT NOT NULL, sends INTEGER NOT NULL,
                received_at TEXT, latency_ms INTEGER, failed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(call_id, target));
            CREATE TABLE IF NOT EXISTS battery_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT NOT NULL, at TEXT NOT NULL,
                battery INTEGER, charging INTEGER);
            CREATE INDEX IF NOT EXISTS ix_battery_dev ON battery_log(device, at);
            """)
            db.commit()
        self._last_bat = {}

    def _run(self, sql, args=()):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(sql, args).fetchall()
            db.commit()
            return [dict(r) for r in rows]

    # ---- 派单回执 -------------------------------------------------------------
    def sent(self, call_id, target, watch, kind, at):
        """记一次发送。换了表(watch 变了)视为新的一轮,重新计数。"""
        old = self._run('SELECT watch, received_at FROM deliveries WHERE call_id=? AND target=?', (call_id, target))
        if old and old[0]['watch'] == watch:
            self._run('UPDATE deliveries SET last_sent=?, sends=sends+1 WHERE call_id=? AND target=?',
                      (at, call_id, target))
        else:
            self._run('INSERT OR REPLACE INTO deliveries(call_id,target,watch,kind,first_sent,last_sent,sends,'
                      'received_at,latency_ms,failed) VALUES(?,?,?,?,?,?,1,NULL,NULL,0)',
                      (call_id, target, watch, kind, at, at))

    def received(self, call_id, target, at) -> Optional[int]:
        """手表回执。返回送达耗时(毫秒);重复回执返回 None。"""
        r = self._run('SELECT first_sent, received_at FROM deliveries WHERE call_id=? AND target=?', (call_id, target))
        if not r or r[0]['received_at']:
            return None
        ms = int((datetime.fromisoformat(at) - datetime.fromisoformat(r[0]['first_sent'])).total_seconds() * 1000)
        self._run('UPDATE deliveries SET received_at=?, latency_ms=?, failed=0 WHERE call_id=? AND target=?',
                  (at, max(ms, 0), call_id, target))
        return max(ms, 0)

    def due(self, before_iso) -> List[dict]:
        """还没回执、上次发送早于 before 的派单(调用方再过滤呼叫是否仍待接单)。"""
        return self._run('SELECT * FROM deliveries WHERE received_at IS NULL AND failed=0 AND last_sent<?',
                         (before_iso,))

    def fail(self, call_id, target):
        self._run('UPDATE deliveries SET failed=1 WHERE call_id=? AND target=?', (call_id, target))

    def stats(self, since_iso):
        rows = self._run('SELECT * FROM deliveries WHERE first_sent>=?', (since_iso,))
        return rows

    # ---- 电量曲线 -------------------------------------------------------------
    def battery(self, device, at, battery, charging, every_minutes):
        """每台设备每 every_minutes 分钟最多记一条(心跳 30 秒一次,不全记)。"""
        if battery is None:
            return False
        last = self._last_bat.get(device)
        if last is None:
            r = self._run('SELECT at FROM battery_log WHERE device=? ORDER BY id DESC LIMIT 1', (device,))
            last = r[0]['at'] if r else None
        if last and (datetime.fromisoformat(at) - datetime.fromisoformat(last)).total_seconds() < every_minutes * 60:
            return False
        self._run('INSERT INTO battery_log(device,at,battery,charging) VALUES(?,?,?,?)',
                  (device, at, battery, None if charging is None else int(bool(charging))))
        self._last_bat[device] = at
        return True

    def battery_series(self, since_iso, device=None):
        sql, args = 'SELECT * FROM battery_log WHERE at>=?', [since_iso]
        if device:
            sql += ' AND device=?'; args.append(device)
        return self._run(sql + ' ORDER BY device, at', args)
