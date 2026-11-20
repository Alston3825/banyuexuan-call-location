"""手表档案与绑定关系(换表 / 回绑 / 归还重置)。

业务归属永远以"护理师 staff_id"为主键:呼叫、派单、定位、记录都记在人身上,手表 SN 只是通道。
所以换表只需要改"谁戴哪块表",进行中的呼叫自然跟着人走,不用逐条迁移。

状态:IDLE(备用池空闲) / BORROWED(在岗佩戴) / CHARGING(换下待充电) / FAULT(故障) / RESETTING(归还清空中)
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from typing import Dict, List, Optional

STATUSES = ('IDLE', 'BORROWED', 'CHARGING', 'FAULT', 'RESETTING')
STATUS_TEXT = dict(IDLE='备用池空闲', BORROWED='在岗佩戴', CHARGING='待充电', FAULT='故障', RESETTING='清空中')
ACTIONS = ('BIND', 'SWAP_OUT', 'SWAP_IN', 'ROLLBACK', 'RELEASE', 'RESET', 'ADJUST')


class WatchError(ValueError):
    """校验不通过:message 是给护士站看的中文原因。"""


class WatchRegistry:
    def __init__(self, path):
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS watches(
                sn TEXT PRIMARY KEY, mac TEXT, status TEXT NOT NULL, bound_staff TEXT,
                home_staff TEXT, spare INTEGER NOT NULL DEFAULT 0,
                last_bind_time TEXT, last_reset_time TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS watch_binding(
                bind_id TEXT PRIMARY KEY, staff_id TEXT, watch_sn TEXT NOT NULL, schedule_id TEXT,
                action_type TEXT NOT NULL, prev_watch_sn TEXT, operator_id TEXT, operate_time TEXT NOT NULL,
                operate_source TEXT);
            CREATE TABLE IF NOT EXISTS operation_log(
                log_id TEXT PRIMARY KEY, staff_id TEXT, watch_sn_from TEXT, watch_sn_to TEXT,
                action_type TEXT NOT NULL, operator_id TEXT, operator_name TEXT, operate_time TEXT NOT NULL,
                operate_source TEXT, detail TEXT,
                sync_status TEXT NOT NULL DEFAULT 'SYNCED');   -- 单主机架构下都是本机写入,保留字段备将来多主机同步
            CREATE INDEX IF NOT EXISTS ix_oplog_time ON operation_log(operate_time);
            """)
            db.commit()
        self._load()

    # ---- 内存缓存(单进程;每次写库后同步更新) ----------------------------
    def _load(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            self.rows: Dict[str, dict] = {r['sn']: dict(r) for r in db.execute('SELECT * FROM watches')}
        self._by_staff = {r['bound_staff']: sn for sn, r in self.rows.items() if r['bound_staff']}

    def _save(self, db, sn):
        r = self.rows[sn]
        db.execute('INSERT OR REPLACE INTO watches(sn,mac,status,bound_staff,home_staff,spare,last_bind_time,'
                   'last_reset_time,note) VALUES(?,?,?,?,?,?,?,?,?)',
                   (sn, r.get('mac'), r['status'], r.get('bound_staff'), r.get('home_staff'), int(r.get('spare') or 0),
                    r.get('last_bind_time'), r.get('last_reset_time'), r.get('note')))
        self._by_staff = {x['bound_staff']: s for s, x in self.rows.items() if x['bound_staff']}

    def sync_from_config(self, staff: Dict[str, dict], spares: List[str], at: str):
        """把门店配置里的"每人自己的表"和"备用表"登记进档案。已有档案(换过表的)不覆盖。"""
        changed = False
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            for sid, info in staff.items():
                w = info.get('watch')
                if not w:
                    continue
                r = self.rows.get(w)
                if r is None:
                    taken = self._by_staff.get(sid)            # 这个人已经戴着别的表(例如换表后) → 新登记的表先放待充电
                    self.rows[w] = dict(sn=w, mac=None, status='CHARGING' if taken else 'BORROWED',
                                        bound_staff=None if taken else sid, home_staff=sid, spare=0,
                                        last_bind_time=None if taken else at, last_reset_time=None, note=None)
                    self._save(db, w)
                    if not taken:
                        self._log_binding(db, sid, w, 'BIND', None, 'system', at, 'CONFIG')
                    changed = True
                elif r.get('home_staff') != sid and not r.get('spare'):
                    r['home_staff'] = sid
                    self._save(db, w)
                    changed = True
            for w in spares:
                if w not in self.rows:
                    self.rows[w] = dict(sn=w, mac=None, status='IDLE', bound_staff=None, home_staff=None, spare=1,
                                        last_bind_time=None, last_reset_time=None, note=None)
                    self._save(db, w)
                    changed = True
                elif not self.rows[w].get('spare'):
                    self.rows[w]['spare'] = 1
                    self._save(db, w)
                    changed = True
            db.commit()
        return changed

    # ---- 查询 ---------------------------------------------------------------
    def watch_of(self, staff: str) -> Optional[str]:
        return self._by_staff.get(staff)

    def staff_of(self, sn: str) -> Optional[str]:
        r = self.rows.get(sn)
        return r['bound_staff'] if r else None

    def home_watch(self, staff: str) -> Optional[str]:
        return next((sn for sn, r in self.rows.items() if r.get('home_staff') == staff and not r.get('spare')), None)

    def get(self, sn: str) -> Optional[dict]:
        r = self.rows.get(sn)
        return dict(r) if r else None

    def all(self) -> List[dict]:
        return [dict(r) for _, r in sorted(self.rows.items())]

    def is_wearing_spare(self, staff: str) -> bool:
        w = self.watch_of(staff)
        return bool(w and self.rows[w].get('home_staff') != staff)

    # ---- 变更 ---------------------------------------------------------------
    def _log_binding(self, db, staff, sn, action, prev, operator, at, source, schedule=None):
        db.execute('INSERT INTO watch_binding VALUES(?,?,?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex, staff, sn, schedule, action, prev, operator, at, source))

    def _log_op(self, db, staff, sn_from, sn_to, action, operator, operator_name, at, source, detail):
        db.execute('INSERT INTO operation_log(log_id,staff_id,watch_sn_from,watch_sn_to,action_type,operator_id,'
                   'operator_name,operate_time,operate_source,detail) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (uuid.uuid4().hex, staff, sn_from, sn_to, action, operator, operator_name, at, source,
                    json.dumps(detail or {}, ensure_ascii=False)))

    def switch(self, staff, new_sn, old_status, operator, operator_name, at, source, action_in='SWAP_IN',
               action_out='SWAP_OUT', new_old_status=None, detail=None, schedule=None):
        """把 staff 从当前表换到 new_sn。旧表进入 old_status。一次事务完成,写两条绑定记录 + 一条操作日志。"""
        old = self.watch_of(staff)
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute('BEGIN IMMEDIATE')
            if old:
                o = self.rows[old]
                o.update(bound_staff=None, status=old_status)
                self._save(db, old)
                self._log_binding(db, staff, old, action_out, None, operator, at, source, schedule)
            n = self.rows[new_sn]
            n.update(bound_staff=staff, status='BORROWED', last_bind_time=at)
            self._save(db, new_sn)
            self._log_binding(db, staff, new_sn, action_in, old, operator, at, source, schedule)
            self._log_op(db, staff, old, new_sn, action_in, operator, operator_name, at, source, detail)
            db.commit()
        return old

    def set_status(self, sn, status, at, operator=None, operator_name=None, source='ADMIN', note=None,
                   action='ADJUST', log=True, detail=None):
        if status not in STATUSES:
            raise WatchError('未知状态')
        r = self.rows[sn]
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            staff = r.get('bound_staff')
            if status != 'BORROWED' and staff:
                r['bound_staff'] = None
                self._log_binding(db, staff, sn, 'RELEASE', None, operator, at, source)
            r['status'] = status
            if note is not None:
                r['note'] = note
            if status == 'IDLE' and action == 'RESET':
                r['last_reset_time'] = at
            self._save(db, sn)
            if log:
                self._log_op(db, staff, sn, None, action, operator, operator_name, at, source,
                             dict(detail or {}, status=status, note=note))
            db.commit()

    def set_mac(self, sn, mac):
        if sn in self.rows and mac and self.rows[sn].get('mac') != mac:
            self.rows[sn]['mac'] = mac
            with closing(sqlite3.connect(self.path, timeout=10)) as db:
                self._save(db, sn)
                db.commit()

    def log(self, staff, sn_from, sn_to, action, operator, operator_name, at, source, detail=None):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            self._log_op(db, staff, sn_from, sn_to, action, operator, operator_name, at, source, detail)
            db.commit()

    def oplog(self, staff=None, sn=None, action=None, since=None, until=None, limit=200):
        sql, args = 'SELECT * FROM operation_log WHERE 1=1', []
        if staff:
            sql += ' AND staff_id=?'; args.append(staff)
        if sn:
            sql += ' AND (watch_sn_from=? OR watch_sn_to=?)'; args += [sn, sn]
        if action:
            sql += ' AND action_type=?'; args.append(action)
        if since:
            sql += ' AND operate_time>=?'; args.append(since)
        if until:
            sql += ' AND operate_time<?'; args.append(until)
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(sql + ' ORDER BY operate_time DESC LIMIT ?', args + [int(limit)])]
        for r in rows:
            try:
                r['detail'] = json.loads(r['detail'] or '{}')
            except ValueError:
                pass
        return rows

    def bindings(self, staff=None, sn=None, limit=200):
        sql, args = 'SELECT * FROM watch_binding WHERE 1=1', []
        if staff:
            sql += ' AND staff_id=?'; args.append(staff)
        if sn:
            sql += ' AND watch_sn=?'; args.append(sn)
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(sql + ' ORDER BY operate_time DESC LIMIT ?', args + [int(limit)])]
