"""Read-only call projection for the digital-twin 3D screen (schema bycall.call.v1).

Same envelope rules as bycall.location.v1: full snapshot, source_epoch/sequence/revision,
no customer text, voice, transcript or free-form content. Only the host decides call state;
the 3D screen must never infer arrival/completion from a person's location.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta

ACTIVE = ('pending', 'claimed', 'arrived')
ENDED = ('completed', 'cancelled')
RECENT_SECONDS = 60


class TwinCallState:
    def __init__(self, epoch):
        self.epoch = epoch
        self.lock = threading.RLock()
        self.sequence = 0
        self.signature = None

    @staticmethod
    def _first(events, kind):
        return next((e['at'] for e in events if e['kind'] == kind), None)

    def item(self, call, events, cfg):
        req = json.loads(call['request'])
        snap = req.get('snapshot') or {}
        room = req.get('room')
        floor = cfg.rooms.get(room or '', {}).get('floor')
        kind = snap.get('kind', 'call')
        ended = next((e['at'] for e in reversed(events) if e['kind'] in ('call.complete', 'call.cancel')), None)
        owner = call['owner']
        return dict(
            call_id=call['id'], version=call['version'], room=room,
            floor=str(floor) if floor is not None else None, kind=kind,
            service_code=snap.get('service_code'), label=snap.get('label') or snap.get('service_code'),
            priority=req.get('priority', 'normal'), state=call['state'],
            owner_staff=owner, owner_name=cfg.name(owner) if owner else None,
            requester_staff=snap.get('requester') if kind == 'help' else None,
            created_at=self._first(events, 'call.created'), claimed_at=self._first(events, 'call.claim'),
            arrived_at=self._first(events, 'call.arrive'), ended_at=ended,
            transfer_pending=bool(call['proposed_owner']),
            transferred=any(e['kind'] in ('call.propose_transfer', 'call.accept_transfer', 'call.transfer') for e in events))

    def build(self, core, cfg, now):
        with self.lock:
            calls = [c for st in ACTIVE + ENDED for c in core.list_calls(store=cfg.store_id, state=st, limit=200)]
            ids = {c['id'] for c in calls}
            by_call = {}
            with core._db() as db:
                for e in db.execute('SELECT call_id, kind, at FROM events ORDER BY seq'):
                    if e['call_id'] in ids:
                        by_call.setdefault(e['call_id'], []).append(dict(e))
            cutoff = now - timedelta(seconds=RECENT_SECONDS)
            items = []
            for c in calls:
                it = self.item(c, by_call.get(c['id'], []), cfg)
                if c['state'] in ENDED:
                    try:
                        if not it['ended_at'] or datetime.fromisoformat(it['ended_at']) < cutoff:
                            continue
                    except ValueError:
                        continue
                items.append(it)
            items.sort(key=lambda i: (i['created_at'] or '', i['call_id']))
            signature = hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False,
                                                  separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            if signature != self.signature:
                self.sequence += 1
                self.signature = signature
            return dict(schema='bycall.call.v1', store_id=cfg.store_id, source_epoch=self.epoch,
                        sequence=self.sequence, revision=signature, delivery='full',
                        consumer_timeout_seconds=8, recent_seconds=RECENT_SECONDS,
                        server_time=now.isoformat(), items=items)
