"""Guest-exclusive (自定义) call button: at most MAX (=1) per stay (客户专有按钮).

Keyed by stay, so when the management system moves the room to the next stay (check-out /
new check-in) the previous guest's buttons simply stop applying. Without a management system
the key falls back to the room, and staff clear them by hand.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing

MAX = 1
MAX_LABEL = 8


class GuestButtonError(ValueError):
    pass


class GuestButtons:
    MAX = MAX
    def __init__(self, path):
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS guest_buttons(stay_key TEXT NOT NULL, slot INTEGER NOT NULL, '
                       'label TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(stay_key, slot))')
            db.commit()

    def get(self, stay_key):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute('SELECT slot,label FROM guest_buttons WHERE stay_key=? ORDER BY slot', (stay_key,)).fetchall()
        return [dict(code=f'GUEST_{s}', label=l) for s, l in rows]

    def set(self, stay_key, labels):
        if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
            raise GuestButtonError('labels must be a list of strings')
        labels = [x.strip() for x in labels if x.strip()]
        if len(labels) > MAX:
            raise GuestButtonError(f'每位客户最多 {MAX} 个专属按钮')
        if any(len(x) > MAX_LABEL for x in labels):
            raise GuestButtonError(f'按钮文字最多 {MAX_LABEL} 个字')
        if len(set(labels)) != len(labels):
            raise GuestButtonError('两个按钮文字不能相同')
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM guest_buttons WHERE stay_key=?', (stay_key,))
            db.executemany('INSERT INTO guest_buttons VALUES(?,?,?,?)',
                           [(stay_key, i + 1, l, time.time()) for i, l in enumerate(labels)])
            db.commit()
        return self.get(stay_key)

    def purge(self, days=180):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute('DELETE FROM guest_buttons WHERE updated_at<?', (time.time() - days * 86400,))
            db.commit()
