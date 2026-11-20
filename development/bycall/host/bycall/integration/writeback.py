"""Write call records back to the management system (spec §5.1), idempotent by call_id.

Sends the full known record each time (later sends never blank earlier fields). Survives
management-system downtime: unsent versions stay queued and are retried.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from .mgmt_client import MgmtError

# Status vocabulary beyond COMPLETED is NOT confirmed by the management system yet.
STATUS = {'pending': 'PENDING', 'claimed': 'ACCEPTED', 'arrived': 'ARRIVED',
          'completed': 'COMPLETED', 'cancelled': 'CANCELLED'}


class CallWriteback:
    def __init__(self, client, core, cfg):
        self.client, self.core, self.cfg = client, core, cfg
        self.errors = []
        with closing(sqlite3.connect(core.path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS writeback(call_id TEXT PRIMARY KEY, sent TEXT NOT NULL)')
            db.commit()

    def _record(self, db, c):
        ev = db.execute('SELECT kind,at,payload FROM events WHERE call_id=? ORDER BY version', (c['id'],)).fetchall()
        first = lambda kind: next((e['at'] for e in ev if e['kind'] == kind), None)
        done = next((e for e in ev if e['kind'] == 'call.complete'), None)
        disp = db.execute('SELECT MIN(at) FROM notifications WHERE call_id=?', (c['id'],)).fetchone()[0]
        req = json.loads(c['request'])
        snap = req['snapshot']
        status = STATUS[c['state']]
        return dict(
            call_id=c['id'], store_id=c['store'], room_id=req['room'], stay_id=snap.get('stay_id'),
            request_type='CALL_REQUEST', service_code=snap.get('service_code'), status=status,
            initiated_at=first('call.created'), dispatched_at=disp, accepted_at=first('call.claim'),
            completed_at=first('call.complete'), handler_staff_id=c['owner'],
            feedback=(json.loads(done['payload']).get('reason') if done else None),
            payload=dict(priority=req['priority'], text=snap.get('text'), voice_url=snap.get('voice_url'),
                         arrived_at=first('call.arrive'), cancelled_at=first('call.cancel')))

    def flush(self, limit=50):
        """Returns number of records delivered. Stops at the first network failure."""
        sent = 0
        with closing(sqlite3.connect(self.core.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            for c in db.execute('SELECT * FROM calls WHERE store=? ORDER BY rowid', (self.cfg.store_id,)).fetchall():
                disp = db.execute('SELECT 1 FROM notifications WHERE call_id=? LIMIT 1', (c['id'],)).fetchone()
                key = f"{c['version']}:{int(bool(disp))}"
                old = db.execute('SELECT sent FROM writeback WHERE call_id=?', (c['id'],)).fetchone()
                if old and old[0] == key:
                    continue
                if json.loads(c['request'])['snapshot'].get('kind') == 'help':
                    # 同事求助只在呼叫系统内部流转,不是客户服务记录,不回写管理系统
                    db.execute('INSERT OR REPLACE INTO writeback VALUES(?,?)', (c['id'], key))
                    db.commit()
                    continue
                try:
                    self.client.post_call_event(self._record(db, c))
                except MgmtError as e:
                    self.errors.append((c['id'], str(e)))
                    if e.status is None or e.status >= 500:
                        break                      # management system unreachable: retry later
                    continue                       # 4xx: bad record, keep it visible in errors
                db.execute('INSERT OR REPLACE INTO writeback VALUES(?,?)', (c['id'], key))
                db.commit()
                sent += 1
                if sent >= limit:
                    break
        return sent
