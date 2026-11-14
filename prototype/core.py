"""Transactional workflow experiment. Callers are trusted test adapters only.

No transport, authentication, roster engine, voice, BLE or notification delivery.
Every mutation serializes through SQLite and appends an event in the same commit.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


class Conflict(ValueError):
    pass


class Core:
    def __init__(self, path):
        self.path = str(path)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, store TEXT NOT NULL,
                    device TEXT NOT NULL, source_event TEXT NOT NULL,
                    request TEXT NOT NULL, state TEXT NOT NULL,
                    owner TEXT, proposed_owner TEXT, version INTEGER NOT NULL,
                    UNIQUE(store, device, source_event)
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE, call_id TEXT NOT NULL,
                    version INTEGER NOT NULL, kind TEXT NOT NULL,
                    at TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(call_id, version)
                );
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _event(db, call_id, version, kind, payload):
        db.execute('INSERT INTO events(event_id,call_id,version,kind,at,payload) '
                   'VALUES(?,?,?,?,?,?)',
                   (str(uuid4()), call_id, version, kind,
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True)))

    def create(self, *, store, device, event_id, room, priority, snapshot):
        if not all(isinstance(x, str) and x.strip()
                   for x in (store, device, event_id, room)):
            raise ValueError('Nonempty identity fields required')
        if priority not in ('normal', 'emergency') or not isinstance(snapshot, dict):
            raise ValueError('Invalid priority or snapshot')
        request = json.dumps(dict(room=room, priority=priority, snapshot=snapshot),
                             ensure_ascii=False, sort_keys=True)
        with self._transaction() as db:
            old = db.execute('SELECT * FROM calls WHERE store=? AND device=? '
                             'AND source_event=?', (store, device, event_id)).fetchone()
            if old:
                if old['request'] != request:
                    raise Conflict('Event ID reused with different content')
                return old['id']
            call_id = str(uuid4())
            db.execute('INSERT INTO calls(id,store,device,source_event,request,state,owner,'
                       'proposed_owner,version) VALUES(?,?,?,?,?,?,?,?,?)',
                       (call_id, store, device, event_id, request, 'pending', None, None, 1))
            self._event(db, call_id, 1, 'call.created', json.loads(request))
            return call_id

    def act(self, call_id, *, store, actor, expected_version, action, target=None,
            reason=None, method=None):
        """Version check makes stale/replayed commands fail without a second event.

        Transport-level command idempotency and authenticated actor resolution
        remain future work. Device assertions must not be passed through directly.
        """
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError('Actor required')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM calls WHERE id=? AND store=?',
                             (call_id, store)).fetchone()
            if not row:
                raise KeyError('Call not found in store')
            if row['version'] != expected_version:
                raise Conflict('Stale task version')
            state, owner, proposed = row['state'], row['owner'], row['proposed_owner']
            if action == 'claim':
                if state != 'pending':
                    raise Conflict('Already claimed or closed')
                state, owner = 'claimed', actor
            elif action == 'accept_transfer':
                if state not in ('claimed', 'arrived') or proposed != actor:
                    raise Conflict('No transfer proposed to this actor')
                state, owner, proposed = 'claimed', actor, None
            else:
                if owner != actor or state not in ('claimed', 'arrived'):
                    raise Conflict('Active owner required')
                if action == 'arrive':
                    if state != 'claimed' or method not in ('manual', 'nfc'):
                        raise Conflict('Explicit arrival evidence required')
                    state = 'arrived'
                elif action == 'complete':
                    if method not in ('onsite', 'remote') or not reason:
                        raise ValueError('Resolution method and reason required')
                    if method == 'onsite' and state != 'arrived':
                        raise Conflict('Confirmed arrival required')
                    state, proposed = 'completed', None
                elif action == 'propose_transfer':
                    if not isinstance(target, str) or not target.strip() or target == actor or not reason:
                        raise ValueError('Different target and reason required')
                    if proposed:
                        raise Conflict('Transfer already pending')
                    proposed = target
                else:
                    raise ValueError('Unsupported action')
            version = row['version'] + 1
            db.execute('UPDATE calls SET state=?,owner=?,proposed_owner=?,version=? '
                       'WHERE id=?', (state, owner, proposed, version, call_id))
            self._event(db, call_id, version, 'call.' + action,
                        dict(actor=actor, previous_owner=row['owner'], target=target,
                             reason=reason, method=method, state=state, owner=owner))
        return self.get(call_id, store=store)

    def get(self, call_id, *, store):
        with self._db() as db:
            row = db.execute('SELECT * FROM calls WHERE id=? AND store=?',
                             (call_id, store)).fetchone()
            if row is None:
                raise KeyError('Call not found in store')
            return dict(row)

    def events(self, *, after=0):
        """Local test outbox read only; no external delivery/ACK worker yet."""
        with self._db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM events WHERE seq>? ORDER BY seq',
                                               (after,))]
