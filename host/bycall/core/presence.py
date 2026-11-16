"""腕表在线区间账本。服务端 UTC 计时，按上海自然日切分；监控中断记 UNKNOWN。"""
from datetime import datetime, time, timedelta, timezone

from .watchops import serialized

TZ = timezone(timedelta(hours=8))


class Presence:
    def __init__(self, svc):
        self.svc, self.core, self.store = svc, svc.core, svc.cfg.store_id
        with self.core._transaction() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS watch_presence(
              store TEXT, watch TEXT, state TEXT, since TEXT, last_seen TEXT,
              staff TEXT, binding TEXT, PRIMARY KEY(store,watch));
            CREATE TABLE IF NOT EXISTS watch_presence_intervals(
              id INTEGER PRIMARY KEY AUTOINCREMENT, store TEXT, watch TEXT, staff TEXT,
              binding TEXT, state TEXT, start TEXT, end TEXT, reason TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS presence_open ON watch_presence_intervals(store,watch) WHERE end IS NULL;
            CREATE INDEX IF NOT EXISTS presence_range ON watch_presence_intervals(store,start,end);
            CREATE TABLE IF NOT EXISTS presence_observer(store TEXT PRIMARY KEY, at TEXT);
            ''')
            checkpoint = db.execute('SELECT at FROM presence_observer WHERE store=?', (self.store,)).fetchone()
            if checkpoint:
                # 主机不在场期间不能认定设备仍在线或离线。心跳也可能晚于最后一次 tick。
                for row in db.execute('SELECT * FROM watch_presence WHERE store=?', (self.store,)).fetchall():
                    opened = db.execute('SELECT start FROM watch_presence_intervals WHERE store=? AND watch=? AND end IS NULL', (self.store, row['watch'])).fetchone()
                    at = max(checkpoint['at'], opened['start'] if opened else checkpoint['at'], row['last_seen'] or checkpoint['at'])
                    if at < svc._now().isoformat():
                        self._transition(db, row['watch'], 'UNKNOWN', at, row['staff'], row['binding'], 'host_restart_gap')
            db.execute('INSERT OR REPLACE INTO presence_observer VALUES(?,?)', (self.store, svc._now().isoformat()))

    def _identity(self, watch):
        staff = self.svc.cfg.staff_of_watch(watch)
        binding = self.svc.watches.context(staff)['binding'] if staff else None
        return staff, binding['bind_id'] if binding else None

    def _transition(self, db, watch, state, at, staff, binding, reason):
        old = db.execute('SELECT * FROM watch_presence WHERE store=? AND watch=?', (self.store, watch)).fetchone()
        if old and old['state'] == state and old['staff'] == staff and old['binding'] == binding:
            return
        opened = db.execute('SELECT id,start FROM watch_presence_intervals WHERE store=? AND watch=? AND end IS NULL', (self.store, watch)).fetchone()
        if opened:
            at = max(at, opened['start'])  # 防系统时钟回拨产生负区间
            db.execute('UPDATE watch_presence_intervals SET end=? WHERE id=?', (at, opened['id']))
        since = old['since'] if old and old['state'] == state else at
        db.execute('INSERT INTO watch_presence_intervals(store,watch,staff,binding,state,start,reason) VALUES(?,?,?,?,?,?,?)', (self.store, watch, staff, binding, state, at, reason))
        db.execute('INSERT INTO watch_presence VALUES(?,?,?,?,?,?,?) ON CONFLICT(store,watch) DO UPDATE SET state=excluded.state,since=excluded.since,staff=excluded.staff,binding=excluded.binding',
                   (self.store, watch, state, since, old['last_seen'] if old else None, staff, binding))

    def known(self):
        return set(self.svc.cfg.watches) | {v.get('watch') for v in self.svc.cfg.staff.values() if v.get('watch')} | {w['sn'] for w in self.svc.watches.rows('watch_inventory', 10000)}

    def _expire(self, db, watch, now):
        row = db.execute('SELECT * FROM watch_presence WHERE store=? AND watch=?', (self.store, watch)).fetchone()
        if row and row['state'] == 'ONLINE' and row['last_seen']:
            deadline = datetime.fromisoformat(row['last_seen']) + timedelta(seconds=self.svc.cfg.watch_policy['offline_seconds'])
            if deadline <= now:
                self._transition(db, watch, 'OFFLINE', deadline.isoformat(), row['staff'], row['binding'], 'heartbeat_timeout')

    @serialized
    def observe(self, watch, online=True, reason='heartbeat'):
        if watch not in self.known():
            return
        now = self.svc._now()
        staff, binding = self._identity(watch)
        with self.core._transaction() as db:
            self._expire(db, watch, now)
            self._transition(db, watch, 'ONLINE' if online else 'OFFLINE', now.isoformat(), staff, binding, reason)
            if online:
                db.execute('UPDATE watch_presence SET last_seen=? WHERE store=? AND watch=?', (now.isoformat(), self.store, watch))

    @serialized
    def tick(self):
        now = self.svc._now()
        with self.core._transaction() as db:
            for watch in self.known():
                self._expire(db, watch, now)
                staff, binding = self._identity(watch)
                row = db.execute('SELECT state FROM watch_presence WHERE store=? AND watch=?', (self.store, watch)).fetchone()
                self._transition(db, watch, row['state'] if row else 'UNKNOWN', now.isoformat(), staff, binding, 'binding_or_registration')
            db.execute('INSERT OR REPLACE INTO presence_observer VALUES(?,?)', (self.store, now.isoformat()))

    def snapshot(self):
        self.tick()
        now = self.svc._now()
        with self.core._db() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM watch_presence WHERE store=? ORDER BY watch', (self.store,))]
        devices = {d['device']: d for d in self.svc.tele.devices()}
        locations = {l['watch']: l for l in self.svc.tele.latest_locations()}
        for r in rows:
            d, loc = devices.get(r['watch'], {}), locations.get(r['watch'], {})
            r.update(name=self.svc.cfg.name(r['staff']) if r['staff'] else '备用 / 未绑定',
                     online=r['state'] == 'ONLINE', battery=d.get('battery'),
                     elapsed_seconds=max(0, int((now - datetime.fromisoformat(r['since'])).total_seconds())),
                     area=loc.get('area') if loc.get('staff') == r['staff'] and r['staff'] else None)
        return rows

    def daily(self, start, end, watch=None, staff=None):
        first = datetime.combine(start, time(), TZ).astimezone(timezone.utc)
        stop = datetime.combine(end + timedelta(days=1), time(), TZ).astimezone(timezone.utc)
        now = self.svc._now()
        if end < start or (end - start).days > 30:
            raise ValueError('日期范围必须为 1–31 天')
        self.tick()
        sql = 'SELECT * FROM watch_presence_intervals WHERE store=? AND start<? AND (end IS NULL OR end>?)'
        args = [self.store, stop.isoformat(), first.isoformat()]
        if watch:
            sql += ' AND watch=?'; args.append(watch)
        if staff:
            sql += ' AND staff=?'; args.append(staff)
        with self.core._db() as db:
            intervals = [dict(r) for r in db.execute(sql + ' ORDER BY watch,start,id', args)]
        summary, details = {}, []
        for r in intervals:
            lo = max(first, datetime.fromisoformat(r['start']))
            hi = min(stop, now, datetime.fromisoformat(r['end']) if r['end'] else now)
            while lo < hi:
                day = lo.astimezone(TZ).date()
                midnight = datetime.combine(day + timedelta(days=1), time(), TZ).astimezone(timezone.utc)
                finish = min(hi, midnight)
                seconds = (finish - lo).total_seconds()
                key = (day.isoformat(), r['watch'], r['staff'], r['binding'])
                item = summary.setdefault(key, dict(date=key[0], watch=r['watch'], staff=r['staff'], binding=r['binding'], online_seconds=0, offline_seconds=0, unknown_seconds=0, offline_intervals=0))
                item[r['state'].lower() + '_seconds'] += seconds
                if r['state'] == 'OFFLINE':
                    item['offline_intervals'] += 1
                details.append(dict(r, date=day.isoformat(), start=lo.isoformat(), end=finish.isoformat(), duration_seconds=seconds, ongoing=r['end'] is None))
                lo = finish
        for item in summary.values():
            item['name'] = self.svc.cfg.name(item['staff']) if item['staff'] else '备用 / 未绑定'
        return dict(store_id=self.store, timezone='Asia/Shanghai', generated_at=now.isoformat(), summary=list(summary.values()), intervals=details,
                    note='在线表示主机检测到设备连接，不等于员工在岗；UNKNOWN 为未观测或主机监控中断；心跳超时以最后心跳加阈值估算。')
