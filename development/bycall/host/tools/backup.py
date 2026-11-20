"""Backup / restore CLI.
  python tools/backup.py backup  --db /data/host.db --dest /backups [--keep-days 7]
  python tools/backup.py list    --dest /backups
  python tools/backup.py restore --db /data/host.db [--dest /backups | --file X.db.gz] --yes
Stop the host before `restore`."""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from bycall.core import backup as B  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['backup', 'list', 'restore'])
    ap.add_argument('--db'); ap.add_argument('--dest'); ap.add_argument('--file')
    ap.add_argument('--keep-days', type=int, default=7); ap.add_argument('--yes', action='store_true')
    a = ap.parse_args()
    try:
        if a.cmd == 'backup':
            print('backup ok:', B.backup(a.db, a.dest, a.keep_days))
        elif a.cmd == 'list':
            for f in B.list_backups(a.dest):
                print(f'{datetime.fromtimestamp(os.path.getmtime(f)):%Y-%m-%d %H:%M:%S}  {os.path.getsize(f):>10}  {f}')
        else:
            f = a.file or (B.latest(a.dest) if a.dest else None)
            if not f:
                sys.exit('no backup found (use --file or --dest)')
            if not a.yes:
                sys.exit(f'will replace {a.db} with {f}; host must be stopped. Re-run with --yes to confirm.')
            aside = B.restore(f, a.db)
            print('restored from', f); print('previous database kept at', aside)
    except B.BackupError as e:
        sys.exit(f'FAILED: {e}')


if __name__ == '__main__':
    main()
