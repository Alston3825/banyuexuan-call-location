"""Voice message files (guest recordings). Auto-deleted after the retention period."""
from __future__ import annotations

import os
import time
import uuid

MAX_BYTES = 2 * 1024 * 1024


class VoiceStore:
    def __init__(self, directory):
        self.dir = str(directory)
        os.makedirs(self.dir, exist_ok=True)

    def save(self, data: bytes, ext='wav') -> str:
        if not data or len(data) > MAX_BYTES:
            raise ValueError('empty or too large')
        if ext not in ('wav', 'mp3', 'm4a', 'amr', 'aac', 'ogg'):
            raise ValueError('unsupported format')
        vid = uuid.uuid4().hex
        with open(os.path.join(self.dir, f'{vid}.{ext}'), 'wb') as f:
            f.write(data)
        return f'{vid}.{ext}'

    def path(self, name: str):
        base = os.path.basename(name)
        p = os.path.join(self.dir, base)
        return p if base == name and os.path.isfile(p) else None

    def purge(self, days: int, now=None) -> int:
        cutoff = (now or time.time()) - days * 86400
        n = 0
        for f in os.listdir(self.dir):
            p = os.path.join(self.dir, f)
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
                n += 1
        return n
