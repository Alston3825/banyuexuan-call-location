"""Daily SQLite backup / restore (dev plan §十一).

Uses SQLite's online backup API, so it is consistent while the host is running (WAL mode).
Each backup is verified (PRAGMA integrity_check) and gzipped. Retention is by age, and the newest
backup is never deleted, so a clock error or a failing job can not erase the last good copy.
"""
from __future__ import annotations

import glob
import gzip
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Optional

TZ = timezone(timedelta(hours=8))   # 门店时间(UTC+8);容器默认是 UTC,不能用本机时区
PREFIX = 'host-'
SUFFIX = '.db.gz'


class BackupError(Exception):
    pass


def _integrity(path: str):
    try:
        with closing(sqlite3.connect(path)) as db:
            r = db.execute('PRAGMA integrity_check').fetchone()
    except sqlite3.DatabaseError as e:
        raise BackupError(f'not a valid database: {e}')
    if not r or r[0] != 'ok':
        raise BackupError(f'integrity check failed: {r}')


def backup(db_path: str, dest_dir: str, keep_days: int = 7, now: Optional[datetime] = None) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    now = now or datetime.now(TZ)
    name = f'{PREFIX}{now:%Y%m%d-%H%M%S}{SUFFIX}'
    final = os.path.join(dest_dir, name)
    with tempfile.TemporaryDirectory(dir=dest_dir) as tmp:
        snap = os.path.join(tmp, 'snap.db')
        with closing(sqlite3.connect(db_path, timeout=30)) as src, closing(sqlite3.connect(snap)) as dst:
            src.backup(dst)
        _integrity(snap)
        part = final + '.part'
        with open(snap, 'rb') as f, gzip.open(part, 'wb', compresslevel=6) as g:
            shutil.copyfileobj(f, g)
        os.replace(part, final)           # appears only when complete
    prune(dest_dir, keep_days)
    return final


def list_backups(dest_dir: str):
    return sorted(glob.glob(os.path.join(dest_dir, f'{PREFIX}*{SUFFIX}')))


def prune(dest_dir: str, keep_days: int, now_ts: Optional[float] = None):
    files = list_backups(dest_dir)
    cutoff = (now_ts or time.time()) - keep_days * 86400
    removed = []
    for f in files[:-1]:                  # the newest is always kept
        if os.path.getmtime(f) < cutoff:
            os.remove(f)
            removed.append(f)
    return removed


def latest(dest_dir: str) -> Optional[str]:
    b = list_backups(dest_dir)
    return b[-1] if b else None


def restore(backup_file: str, db_path: str) -> Optional[str]:
    """Replace db_path with the backup. The old database is moved aside, never deleted.
    The host must be stopped first. Returns the path of the moved-aside copy (or None)."""
    with tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(db_path))) as tmp:
        restored = os.path.join(tmp, 'restored.db')
        try:
            with gzip.open(backup_file, 'rb') as g, open(restored, 'wb') as f:
                shutil.copyfileobj(g, f)
        except (OSError, EOFError) as e:
            raise BackupError(f'cannot read backup: {e}')
        _integrity(restored)
        aside = None
        if os.path.exists(db_path):
            aside = f'{db_path}.before-restore-{datetime.now():%Y%m%d-%H%M%S}'
            os.replace(db_path, aside)
        for ext in ('-wal', '-shm'):      # stale WAL files would corrupt the restored database
            if os.path.exists(db_path + ext):
                os.replace(db_path + ext, (aside or db_path) + ext)
        os.replace(restored, db_path)
    return aside


def validate(backup_file: str, tmp_dir: Optional[str] = None):
    """Check that a .db.gz decompresses to a healthy SQLite database. Raises BackupError."""
    with tempfile.TemporaryDirectory(dir=tmp_dir) as tmp:
        out = os.path.join(tmp, 'v.db')
        try:
            with gzip.open(backup_file, 'rb') as g, open(out, 'wb') as f:
                shutil.copyfileobj(g, f)
        except (OSError, EOFError) as e:
            raise BackupError(f'cannot read backup: {e}')
        _integrity(out)
