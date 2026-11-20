"""One authoritative location projection shared by text and 3D screens."""
from __future__ import annotations

import hashlib
import json
import threading


class ScreenLocationState:
    def __init__(self, epoch):
        self.epoch = epoch
        self.lock = threading.RLock()
        self.sequence = 0
        self.signature = None

    def build(self, views, now):
        items = []
        for v in views:
            if v['staff'] is None:
                continue
            item = {k: v[k] for k in ('watch', 'staff', 'online', 'location_status', 'location_reason',
                'room', 'area', 'floor', 'precision', 'coordinates', 'received_at', 'observed_at',
                'age_seconds', 'stale_after_seconds', 'valid_until', 'offline_since', 'device_last_seen')}
            item['zone_id'] = v['beacon'] if v['location_status'] == 'fresh' else None
            items.append(item)
        items.sort(key=lambda v: (v['staff'], v['watch']))
        # Age is a presentation clock; it must not create a new revision without a factual change.
        factual = [{k: value for k, value in v.items() if k != 'age_seconds'} for v in items]
        signature = hashlib.sha256(json.dumps(factual, sort_keys=True, ensure_ascii=False,
                                               separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if signature != self.signature:
            self.sequence += 1
            self.signature = signature
        return dict(source_epoch=self.epoch, sequence=self.sequence, revision=signature,
                    delivery='full', consumer_timeout_seconds=8,
                    server_time=now.isoformat(), items=items)
