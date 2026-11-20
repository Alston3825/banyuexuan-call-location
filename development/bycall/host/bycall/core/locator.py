"""Room-level BLE locator: sliding mean + hysteresis (dev plan §八)."""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, List, Optional


class Locator:
    def __init__(self, beacons: Dict[str, dict], window=3, margin=4.0, min_rssi=-90):
        self.beacons = beacons            # beacon_id -> {area, room}
        self.window, self.margin, self.min_rssi = window, margin, min_rssi
        self._hist = defaultdict(lambda: deque(maxlen=window))
        self.current: Dict[str, Optional[str]] = {}

    def update(self, watch: str, readings: List[dict]) -> Optional[dict]:
        known = {r['beacon_id']: r['rssi'] for r in readings
                 if r['beacon_id'] in self.beacons and r['rssi'] >= self.min_rssi}
        if not known:
            self.clear(watch)
            return None
        self._hist[watch].append(known)
        sums, counts = defaultdict(float), defaultdict(int)
        for rep in self._hist[watch]:
            for b, v in rep.items():
                sums[b] += v
                counts[b] += 1
        avg = {b: sums[b] / counts[b] for b in known if sums[b] / counts[b] >= self.min_rssi}
        if not avg:
            self.current[watch] = None
            return None
        best = max(avg, key=avg.get)
        cur = self.current.get(watch)
        if cur and cur != best and cur in avg and avg[best] < avg[cur] + self.margin:
            best = cur                    # not clearly stronger: stay put
        self.current[watch] = best
        b = self.beacons[best]
        return dict(watch=watch, beacon=best, area=b.get('area'), room=b.get('room'), floor=b.get('floor'),
                    rssi=round(avg[best], 1))

    def clear(self, watch):
        self._hist.pop(watch, None)
        self.current.pop(watch, None)
