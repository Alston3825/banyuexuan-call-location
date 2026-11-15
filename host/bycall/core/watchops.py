"""本店腕表绑定事务。业务始终归员工；设备擦除回执前禁止再次借出。"""
import hashlib
import json
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from .dispatcher import shift_of


class WatchError(ValueError):
    pass


def serialized(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self.svc.lock:
            return fn(self, *args, **kwargs)
    return wrapped


class WatchOps:
    def __init__(self, svc):
        self.svc, self.cfg, self.core = svc, svc.cfg, svc.core
        with self.core._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS watch_inventory(
              store TEXT, sn TEXT, state TEXT NOT NULL, staff TEXT, battery INTEGER,
              reset_id TEXT, reset_at TEXT, note TEXT DEFAULT '', PRIMARY KEY(store,sn));
            CREATE UNIQUE INDEX IF NOT EXISTS watch_one_staff ON watch_inventory(store,staff) WHERE staff IS NOT NULL;
            CREATE TABLE IF NOT EXISTS watch_current(
              store TEXT, staff TEXT, sn TEXT, original TEXT, bind_id TEXT, version INTEGER,
              PRIMARY KEY(store,staff));
            CREATE UNIQUE INDEX IF NOT EXISTS watch_one_device ON watch_current(store,sn) WHERE sn IS NOT NULL;
            CREATE TABLE IF NOT EXISTS watch_operations(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, store TEXT, request_id TEXT, digest TEXT,
              staff TEXT, from_sn TEXT, to_sn TEXT, action TEXT, operator TEXT, source TEXT,
              at TEXT, detail TEXT, sync_status TEXT DEFAULT 'LOCAL_PENDING', UNIQUE(store,request_id));
            ''')
        self.seed()

    @serialized
    def seed(self):
        with self.core._transaction() as db:
            for sn in self.cfg.watches:
                db.execute("INSERT OR IGNORE INTO watch_inventory(store,sn,state) VALUES(?,?,'RESETTING')", (self.cfg.store_id, sn))
            for staff, info in self.cfg.staff.items():
                sn = info.get('watch')
                if not sn:
                    continue
                db.execute("INSERT OR IGNORE INTO watch_current VALUES(?,?,?,?,?,0)",
                           (self.cfg.store_id, staff, sn, None, uuid4().hex))
                current = db.execute('SELECT sn FROM watch_current WHERE store=? AND staff=?', (self.cfg.store_id, staff)).fetchone()['sn']
                if current:
                    db.execute("INSERT OR IGNORE INTO watch_inventory(store,sn,state,staff) VALUES(?,?,'BORROWED',?)", (self.cfg.store_id, current, staff))
        self.apply()

    def apply(self):
        with self.core._db() as db:
            rows = db.execute('SELECT staff,sn FROM watch_current WHERE store=?', (self.cfg.store_id,)).fetchall()
        for r in rows:
            if r['staff'] in self.cfg.staff:
                self.cfg.staff[r['staff']]['watch'] = r['sn']

    def rows(self, table, limit=300):
        assert table in ('watch_inventory', 'watch_current', 'watch_operations')
        with self.core._db() as db:
            return [dict(r) for r in db.execute(f'SELECT * FROM {table} WHERE store=? ORDER BY rowid DESC LIMIT ?', (self.cfg.store_id, limit))]

    def context(self, staff):
        if not isinstance(staff, str) or staff not in self.cfg.staff:
            raise WatchError('员工不存在')
        shift, day = shift_of(self.svc._now())
        info = self.cfg.staff[staff]
        rooms = [r for r, v in self.cfg.assignments.items() if v.get(shift) == staff]
        if not rooms:
            rooms = [r for r, v in self.cfg.rooms.items() if v.get('floor') in info.get('floors', [])]
        with self.core._db() as db:
            binding = db.execute('SELECT * FROM watch_current WHERE store=? AND staff=?', (self.cfg.store_id, staff)).fetchone()
            count = db.execute("SELECT count(DISTINCT c.id) FROM calls c LEFT JOIN notifications n ON c.id=n.call_id WHERE c.store=? AND c.state IN ('pending','claimed','arrived') AND (c.owner=? OR n.target=?)", (self.cfg.store_id, staff, staff)).fetchone()[0]
        return dict(staff_id=staff, name=self.cfg.name(staff), binding=dict(binding) if binding else None,
                    schedule_id=info.get('schedule_id'), shift=shift, business_date=day, served_rooms=rooms,
                    on_duty=info.get('on_duty', True), open_events=count)

    def inventory(self):
        devices = {r['device']: r for r in self.svc.tele.devices()}
        out = []
        for r in self.rows('watch_inventory'):
            d = devices.get(r['sn'], {})
            r.update(battery=d.get('battery'), online=bool(d.get('online')) and self.fresh(d), wifi_rssi=d.get('wifi_rssi'))
            r['eligible'] = r['state'] == 'IDLE' and not r['staff'] and r['online'] and (r['battery'] or 0) >= self.cfg.watch_policy['borrow_battery'] and self.signal_ok(d)
            out.append(r)
        return out

    def fresh(self, d):
        return bool(d.get('last_seen')) and (self.svc._now() - datetime.fromisoformat(d['last_seen'])).total_seconds() <= self.cfg.watch_policy['offline_seconds']

    def signal_ok(self, d):
        return d.get('wifi_rssi') is not None and d['wifi_rssi'] >= self.cfg.watch_policy['min_wifi_rssi']

    @serialized
    def change(self, *, staff, target=None, action='SWAP', expected_version, request_id, operator, source='NURSE_STATION', fault=False):
        if action not in ('SWAP', 'ROLLBACK', 'RELEASE', 'UNDO') or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128 or not operator or (target is not None and not isinstance(target, str)):
            raise WatchError('操作参数无效')
        digest = hashlib.sha256(json.dumps([staff, target, action, expected_version, operator, source, fault]).encode()).hexdigest()
        context = self.context(staff)
        with self.core._transaction() as db:
            old = db.execute('SELECT * FROM watch_operations WHERE store=? AND request_id=?', (self.cfg.store_id, request_id)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise WatchError('幂等键已用于不同操作')
                result = dict(old)
            else:
                b = db.execute('SELECT * FROM watch_current WHERE store=? AND staff=?', (self.cfg.store_id, staff)).fetchone()
                if not b or b['version'] != expected_version:
                    raise WatchError('绑定已变化，请刷新后重试')
                origin = b['original']
                if action in ('ROLLBACK', 'UNDO'):
                    if not origin:
                        raise WatchError('没有可回绑的原表')
                    target = origin
                    if action == 'UNDO':
                        last = db.execute('SELECT * FROM watch_operations WHERE store=? AND staff=? ORDER BY seq DESC LIMIT 1', (self.cfg.store_id, staff)).fetchone()
                        if not last or datetime.fromisoformat(last['at']).astimezone(timezone(timedelta(hours=8))).date() != self.svc._now().astimezone(timezone(timedelta(hours=8))).date():
                            raise WatchError('只能撤销当日操作')
                        if last['action'] != 'SWAP' or last['to_sn'] != b['sn']:
                            raise WatchError('只能撤销最近一次换表')
                        target = last['from_sn']
                if action != 'RELEASE':
                    if not self.cfg.staff[staff].get('on_duty', True):
                        raise WatchError('员工当前不在班')
                    if not target or target == b['sn']:
                        raise WatchError('请选择另一块手表')
                    w = db.execute('SELECT * FROM watch_inventory WHERE store=? AND sn=?', (self.cfg.store_id, target)).fetchone()
                    allowed = ('IDLE', 'CHARGING') if action in ('ROLLBACK', 'UNDO') else ('IDLE',)
                    if not w or w['staff'] or w['state'] not in allowed:
                        raise WatchError('手表非可用状态或已绑定他人')
                    d = next((r for r in self.svc.tele.devices() if r['device'] == target), {})
                    if not d.get('online') or not self.fresh(d):
                        raise WatchError('手表离线，请检查网络后重试')
                    if d.get('battery') is None or d['battery'] < self.cfg.watch_policy['borrow_battery']:
                        raise WatchError('手表电量不足')
                    if not self.signal_ok(d):
                        raise WatchError('WiFi 信号不足或未知')
                if b['sn']:
                    state = ('FAULT' if fault else 'CHARGING') if action == 'SWAP' else 'RESETTING'
                    db.execute('UPDATE watch_inventory SET staff=NULL,reset_at=NULL,state=?,reset_id=? WHERE store=? AND sn=?', (state, uuid4().hex if state == 'RESETTING' else None, self.cfg.store_id, b['sn']))
                if action == 'RELEASE':
                    target, origin = None, None
                else:
                    db.execute("UPDATE watch_inventory SET staff=?,state='BORROWED',reset_id=NULL WHERE store=? AND sn=?", (staff, self.cfg.store_id, target))
                    if action == 'SWAP':
                        origin = origin or b['sn']
                    elif action == 'UNDO':
                        origin = None if target == origin else origin
                    else:
                        origin = None
                bid = uuid4().hex
                db.execute('UPDATE watch_current SET sn=?,original=?,bind_id=?,version=version+1 WHERE store=? AND staff=?', (target, origin, bid, self.cfg.store_id, staff))
                detail = json.dumps(dict(bind_id=bid, previous_version=b['version'], context=context), ensure_ascii=False)
                db.execute('INSERT INTO watch_operations(store,request_id,digest,staff,from_sn,to_sn,action,operator,source,at,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (self.cfg.store_id, request_id, digest, staff, b['sn'], target, action, operator, source, self.svc._now().isoformat(), detail))
                result = dict(db.execute('SELECT * FROM watch_operations WHERE store=? AND request_id=?', (self.cfg.store_id, request_id)).fetchone())
        self.apply()
        for sn in (result['from_sn'], result['to_sn']):
            if sn:
                self.send_context(sn)
        if self.cfg.watch_of(staff):
            self.svc._resync(staff)
        return result

    @serialized
    def reset(self, sn, operator):
        with self.core._transaction() as db:
            w = db.execute('SELECT * FROM watch_inventory WHERE store=? AND sn=?', (self.cfg.store_id, sn)).fetchone()
            if not w or w['staff']:
                raise WatchError('未知手表或仍在使用，请先归还')
            rid = uuid4().hex
            db.execute("UPDATE watch_inventory SET state='RESETTING',reset_id=? WHERE store=? AND sn=?", (rid, self.cfg.store_id, sn))
            db.execute('INSERT INTO watch_operations(store,request_id,staff,from_sn,action,operator,source,at,detail) VALUES(?,?,NULL,?,?,?,?,?,?)', (self.cfg.store_id, rid, sn, 'RESET', operator, 'NURSE_STATION', self.svc._now().isoformat(), '{}'))
        self.send_context(sn)

    @serialized
    def reset_ack(self, sn, rid):
        with self.core._transaction() as db:
            w = db.execute('SELECT * FROM watch_inventory WHERE store=? AND sn=?', (self.cfg.store_id, sn)).fetchone()
            if not w or w['staff'] or w['state'] != 'RESETTING' or not rid or w['reset_id'] != rid:
                raise WatchError('擦除回执不匹配')
            d = next((r for r in self.svc.tele.devices() if r['device'] == sn), {})
            state = 'IDLE' if (d.get('battery') or 0) >= self.cfg.watch_policy['borrow_battery'] else 'CHARGING'
            db.execute('UPDATE watch_inventory SET state=?,reset_at=?,reset_id=NULL WHERE store=? AND sn=?', (state, self.svc._now().isoformat(), self.cfg.store_id, sn))

    @serialized
    def mark_fault(self, sn, note, operator):
        if not isinstance(note, str) or not 1 <= len(note.strip()) <= 500:
            raise WatchError('请输入故障原因（最多 500 字）')
        with self.core._transaction() as db:
            w = db.execute('SELECT * FROM watch_inventory WHERE store=? AND sn=?', (self.cfg.store_id, sn)).fetchone()
            if not w or w['staff']:
                raise WatchError('请先归还正在使用的手表')
            db.execute("UPDATE watch_inventory SET state='FAULT',note=?,reset_id=NULL,reset_at=NULL WHERE store=? AND sn=?", (note, self.cfg.store_id, sn))
            db.execute('INSERT INTO watch_operations(store,request_id,from_sn,action,operator,source,at,detail) VALUES(?,?,?,?,?,?,?,?)',
                       (self.cfg.store_id, uuid4().hex, sn, 'FAULT', operator, 'NURSE_STATION', self.svc._now().isoformat(), json.dumps(dict(note=note), ensure_ascii=False)))
        self.send_context(sn)

    def _help_state(self, staff):
        if not staff:
            return []
        with self.core._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM team_help WHERE store=? AND (sender=? OR owner=?) AND state IN ('pending','claimed')", (self.cfg.store_id, staff, staff))]

    def send_context(self, sn):
        from . import protocol as P
        staff = self.cfg.staff_of_watch(sn)
        with self.core._db() as db:
            w = db.execute('SELECT * FROM watch_inventory WHERE store=? AND sn=?', (self.cfg.store_id, sn)).fetchone()
        if not w:
            return
        recovery = next((r for r in self.rows('watch_current') if r['original'] == sn), None)
        payload = dict(rollback_version=recovery['version'] if recovery else None,
                       directory=self.svc.team.roster(staff) if staff else [],
                       roles=self.cfg.roles,
                       active_help=self._help_state(staff),
                       state=w['state'], reset_id=w['reset_id'], context=self.context(staff) if staff else None,
                       policy=self.cfg.watch_policy)
        self.svc.bus.publish(P.t_reply(self.cfg.store_id, sn), self.svc._msg('WATCH_CONTEXT', payload))
