"""同事沟通:消息存储、每人收件状态(排队/已送达)、限频。频道成员与派送由 HostService 决定。

频道 id:
  floor:<楼层>   楼层群(默认)
  dept           护理部群(全部护理岗位)
  all            全公司群发(只有有权限的岗位能发)
  dm:<a>|<b>     一对一(两个 staff_id 排序后拼接)
"""
from __future__ import annotations

import sqlite3
import uuid
from collections import defaultdict, deque
from contextlib import closing
from datetime import datetime
from typing import List, Optional


def dm_id(a, b):
    x, y = sorted([a, b])
    return f'dm:{x}|{y}'


class ChatStore:
    def __init__(self, path):
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS chat_messages(
                id TEXT PRIMARY KEY, channel TEXT NOT NULL, sender TEXT NOT NULL, kind TEXT NOT NULL,
                text TEXT, voice_url TEXT, duration_s REAL, note TEXT, at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_chat_ch ON chat_messages(channel, at);
            CREATE TABLE IF NOT EXISTS chat_inbox(
                msg_id TEXT NOT NULL, staff TEXT NOT NULL, queued_at TEXT, sent_at TEXT, delivered_at TEXT,
                PRIMARY KEY(msg_id, staff));
            CREATE INDEX IF NOT EXISTS ix_inbox_staff ON chat_inbox(staff, delivered_at);
            """)
            db.commit()
        self._recent = defaultdict(deque)          # sender -> 最近发送时间(秒),内存限频

    def _run(self, sql, args=()):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(sql, args).fetchall()
            db.commit()
            return [dict(r) for r in rows]

    def allow(self, sender, now_ts, rate_max, window_s, min_gap_s) -> Optional[str]:
        """限频检查。通过返回 None 并记一笔;不通过返回原因。"""
        q = self._recent[sender]
        while q and now_ts - q[0] > window_s:
            q.popleft()
        if q and now_ts - q[-1] < min_gap_s:
            return 'too_fast'
        if len(q) >= rate_max:
            return 'rate_limited'
        q.append(now_ts)
        return None

    def add(self, channel, sender, kind, text, voice_url, duration_s, note, at, recipients) -> dict:
        mid = uuid.uuid4().hex
        self._run('INSERT INTO chat_messages VALUES(?,?,?,?,?,?,?,?,?)',
                  (mid, channel, sender, kind, text, voice_url, duration_s, note, at))
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.executemany('INSERT OR IGNORE INTO chat_inbox(msg_id,staff) VALUES(?,?)', [(mid, r) for r in recipients])
            db.commit()
        return self.get(mid)

    def get(self, mid):
        r = self._run('SELECT * FROM chat_messages WHERE id=?', (mid,))
        return r[0] if r else None

    def mark_sent(self, mid, staff, at):
        self._run('UPDATE chat_inbox SET sent_at=?, queued_at=NULL WHERE msg_id=? AND staff=?', (at, mid, staff))

    def mark_queued(self, mid, staff, at):
        self._run('UPDATE chat_inbox SET queued_at=COALESCE(queued_at, ?) WHERE msg_id=? AND staff=?', (at, mid, staff))

    def delivered(self, ids: List[str], staff, at):
        if not ids:
            return
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.executemany('UPDATE chat_inbox SET delivered_at=COALESCE(delivered_at, ?) WHERE msg_id=? AND staff=?',
                           [(at, i, staff) for i in ids[:200]])
            db.commit()

    def queued(self):
        return self._run('SELECT i.*, m.at AS msg_at FROM chat_inbox i JOIN chat_messages m ON m.id=i.msg_id '
                         'WHERE i.queued_at IS NOT NULL AND i.sent_at IS NULL ORDER BY m.at')

    def undelivered(self, staff, since_iso, limit=50):
        """重连补发:这个人还没确认收到的消息(最近一段时间内)。"""
        return self._run('SELECT m.* FROM chat_inbox i JOIN chat_messages m ON m.id=i.msg_id WHERE i.staff=? '
                         'AND i.delivered_at IS NULL AND m.at>=? ORDER BY m.at LIMIT ?', (staff, since_iso, limit))

    def history(self, channels: List[str], limit=30, before=None):
        if not channels:
            return []
        q = ','.join('?' * len(channels))
        sql, args = f'SELECT * FROM chat_messages WHERE channel IN ({q})', list(channels)
        if before:
            sql += ' AND at<?'; args.append(before)
        rows = self._run(sql + ' ORDER BY at DESC LIMIT ?', args + [int(limit)])
        return rows[::-1]

    def recent_partners(self, staff, limit=10):
        """最近联系过的人(一对一),用于手表"找人"列表排序。"""
        rows = self._run("SELECT channel, MAX(at) at FROM chat_messages WHERE channel LIKE 'dm:%' AND "
                         "(channel LIKE ? OR channel LIKE ?) GROUP BY channel ORDER BY at DESC LIMIT ?",
                         (f'dm:{staff}|%', f'dm:%|{staff}', limit))
        out = []
        for r in rows:
            a, b = r['channel'][3:].split('|', 1)
            out.append((b if a == staff else a, r['at']))
        return out

    def stats(self, since_iso):
        return self._run('SELECT kind, COUNT(*) n FROM chat_messages WHERE at>=? GROUP BY kind', (since_iso,))


def ts(iso):
    return datetime.fromisoformat(iso).timestamp()
