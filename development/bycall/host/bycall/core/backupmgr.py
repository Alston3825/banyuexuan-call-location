"""Backup scheduler + restore, driven from the management page.

* several runs per day at configured store-time slots (default 03:00/09:00/15:00/21:00)
* every run: local backup first (never depends on the internet), then encrypted upload to the cloud
* failed uploads are retried every few minutes; local backup is unaffected
* restore (local or cloud) is staged and applied by a RESTART of the host process, so no live SQLite
  connection ever sees the file swap. The previous database is moved aside, never deleted.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import List, Optional

from . import backup as B
from . import cloud as C

DEFAULTS = dict(times=['03:00', '09:00', '15:00', '21:00'], local_keep_days=7, cloud_keep_days=30,
                auto=True, cloud=True)
RETRY_S = 300
PENDING = 'restore-pending.db.gz'


def _valid_times(ts) -> List[str]:
    out = []
    for t in ts:
        try:
            h, m = str(t).split(':')
            assert 0 <= int(h) < 24 and 0 <= int(m) < 60
        except Exception:
            raise ValueError(f'时间格式应为 HH:MM,收到 {t!r}')
        out.append(f'{int(h):02d}:{int(m):02d}')
    if not out or len(out) > 12:
        raise ValueError('每天备份时段需要 1–12 个')
    return sorted(set(out))


def apply_pending(data_dir: str, db_path: str):
    """Called at process start, before the database is opened. Never raises: a bad staged file is
    renamed *.failed and the host starts on the current database. Returns (ok, aside_or_error)."""
    p = os.path.join(data_dir, PENDING)
    if not os.path.exists(p):
        return None
    try:
        aside = B.restore(p, db_path)
        os.replace(p, p + '.applied')
        return True, aside
    except Exception as e:
        os.replace(p, p + '.failed')
        return False, str(e)


class BackupManager:
    def __init__(self, data_dir, db_path, local_dir, store=None, passphrase=None, clock=None):
        self.data_dir, self.db_path, self.local_dir = data_dir, db_path, local_dir
        self.store, self.passphrase = store, passphrase
        self.clock = clock or (lambda: datetime.now(B.TZ))
        self.lock = threading.Lock()
        self._sp = os.path.join(data_dir, 'backup-settings.json')
        self._tp = os.path.join(data_dir, 'backup-state.json')
        self.settings = {**DEFAULTS, **self._load(self._sp)}
        self.state = {**dict(last_slot='', last_run=None, last_error=None, cloud_ok_at=None,
                             cloud_error=None, pending=[], next_retry=0), **self._load(self._tp)}

    # ---- persistence ------------------------------------------------
    @staticmethod
    def _load(p):
        try:
            with open(p) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _save(p, d):
        with open(p + '.tmp', 'w') as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(p + '.tmp', p)

    def cloud_ready(self):
        return bool(self.store and self.passphrase)

    def update_settings(self, **kw):
        s = dict(self.settings)
        if 'times' in kw:
            s['times'] = _valid_times(kw['times'])
        for k in ('local_keep_days', 'cloud_keep_days'):
            if k in kw:
                if not (1 <= int(kw[k]) <= 3650):
                    raise ValueError('保留天数需在 1–3650')
                s[k] = int(kw[k])
        for k in ('auto', 'cloud'):
            if k in kw:
                s[k] = bool(kw[k])
        self.settings = s
        self._save(self._sp, s)
        return s

    # ---- schedule ---------------------------------------------------
    def _due_slot(self, now: datetime) -> Optional[str]:
        """Latest configured slot that has passed today and has not been run yet (also catches up
        a slot missed while the host was off)."""
        today = f'{now:%Y-%m-%d}'
        passed = [t for t in self.settings['times'] if t <= f'{now:%H:%M}']
        if not passed:
            return None
        slot = f'{today} {passed[-1]}'
        return slot if slot > self.state['last_slot'] else None

    def tick(self):
        """Call about once a minute."""
        now = self.clock()
        if self.settings['auto']:
            slot = self._due_slot(now)
            if slot:
                self.run(slot=slot)
                return
        if self.state['pending'] and time.time() >= self.state['next_retry']:
            self._upload_pending()

    # ---- backup -----------------------------------------------------
    def run(self, slot: Optional[str] = None) -> dict:
        with self.lock:
            st = self.state
            if slot:
                st['last_slot'] = slot
            try:
                path = B.backup(self.db_path, self.local_dir, self.settings['local_keep_days'], self.clock())
                st['last_run'], st['last_error'] = time.time(), None
            except Exception as e:
                st['last_error'] = str(e)[:150]
                self._save(self._tp, st)
                return dict(ok=False, error=st['last_error'])
            if self.settings['cloud'] and self.store:
                name = os.path.basename(path)
                if name not in st['pending']:
                    st['pending'].append(name)
            self._save(self._tp, st)
        if self.settings['cloud'] and self.store:
            self._upload_pending()
        return dict(ok=True, file=os.path.basename(path), cloud=self.state['cloud_error'] is None)

    def _upload_pending(self):
        import tempfile
        with self.lock:
            st = self.state
            if not self.passphrase:
                st['cloud_error'] = '未设置云备份加密口令(CLOUD_PASSPHRASE),不会上传'
                self._save(self._tp, st)
                return
            st['pending'] = st['pending'][-5:]            # a long outage: only the newest few matter
            ok = []
            try:
                for name in list(st['pending']):
                    src = os.path.join(self.local_dir, name)
                    if not os.path.exists(src):
                        ok.append(name)
                        continue
                    with tempfile.TemporaryDirectory(dir=self.data_dir) as tmp:
                        enc = os.path.join(tmp, name + C.SUFFIX)
                        C.encrypt_file(src, enc, self.passphrase)
                        self.store.put(enc, name + C.SUFFIX)
                    ok.append(name)
                    st['cloud_ok_at'] = time.time()
                st['cloud_error'] = None
                self._prune_cloud()
            except Exception as e:
                st['cloud_error'] = str(e)[:150]
                st['next_retry'] = time.time() + RETRY_S
            finally:
                st['pending'] = [n for n in st['pending'] if n not in ok]
                self._save(self._tp, st)

    def _prune_cloud(self):
        try:
            objs = self.store.list()
            cutoff = time.time() - self.settings['cloud_keep_days'] * 86400
            for o in objs[:-1]:                           # newest cloud copy is always kept
                if o['mtime'] < cutoff:
                    self.store.delete(o['name'])
        except Exception:
            pass                                          # pruning must never fail a backup

    # ---- listing ----------------------------------------------------
    def listing(self) -> dict:
        local = [dict(name=os.path.basename(f), size=os.path.getsize(f), mtime=os.path.getmtime(f))
                 for f in B.list_backups(self.local_dir)]
        cloud, cerr = [], None
        if self.store:
            try:
                for o in self.store.list():
                    cloud.append({**o, 'name': o['name'][:-len(C.SUFFIX)]})
            except Exception as e:
                cerr = str(e)[:150]
        return dict(local=sorted(local, key=lambda x: x['name'], reverse=True),
                    cloud=sorted(cloud, key=lambda x: x['name'], reverse=True), cloud_list_error=cerr)

    def status(self) -> dict:
        st = self.state
        return dict(settings=self.settings, cloud_configured=bool(self.store), cloud_ready=self.cloud_ready(),
                    last_run=st['last_run'], last_error=st['last_error'], cloud_ok_at=st['cloud_ok_at'],
                    cloud_error=st['cloud_error'], pending=len(st['pending']))

    def problems(self, now=None) -> List[str]:
        now = now or time.time()
        out = []
        if self.settings['cloud'] and self.store:
            st = self.state
            if not self.passphrase:
                out.append('云备份未设置加密口令,没有上传')
            elif st['cloud_error']:
                since = st['cloud_ok_at']
                tail = f'(上次成功 {int((now - since) // 3600)} 小时前)' if since else '(还没有成功过)'
                if not since or now - since > 6 * 3600:
                    out.append('云备份上传失败' + tail + ':' + st['cloud_error'])
        elif self.settings['cloud'] and not self.store and os.environ.get('CLOUD_EXPECTED'):
            out.append('云备份已启用但未配置云存储')
        return out

    # ---- restore ----------------------------------------------------
    def stage_restore(self, source: str, name: str) -> str:
        """Validate the chosen backup and stage it; the caller then restarts the host process."""
        import tempfile
        if os.path.basename(name) != name or not name.startswith(B.PREFIX) or not name.endswith(B.SUFFIX):
            raise B.BackupError('备份文件名不合法')
        with self.lock:
            dst = os.path.join(self.data_dir, PENDING)
            if source == 'local':
                src = os.path.join(self.local_dir, name)
                if not os.path.exists(src):
                    raise B.BackupError('本地没有这份备份')
                B.validate(src, self.data_dir)
                import shutil
                shutil.copyfile(src, dst + '.tmp')
            elif source == 'cloud':
                if not self.cloud_ready():
                    raise B.BackupError('云存储或加密口令未配置')
                with tempfile.TemporaryDirectory(dir=self.data_dir) as tmp:
                    enc = os.path.join(tmp, 'x.enc')
                    try:
                        self.store.get(name + C.SUFFIX, enc)
                        C.decrypt_file(enc, dst + '.tmp', self.passphrase)
                    except C.CloudError as e:
                        raise B.BackupError(str(e))
                try:
                    B.validate(dst + '.tmp', self.data_dir)
                except B.BackupError:
                    os.remove(dst + '.tmp')
                    raise
            else:
                raise B.BackupError('来源只能是 local 或 cloud')
            os.replace(dst + '.tmp', dst)
        return dst
