"""店内求助、同事消息及送达回执。SQLite 持久化，主机负责权限和调度。"""
import json
from datetime import datetime
from uuid import uuid4

from .watchops import WatchError


class Team:
    def __init__(self, svc):
        self.svc, self.core, self.cfg = svc, svc.core, svc.cfg
        with self.core._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS watch_voice(store TEXT, url TEXT, staff TEXT, sn TEXT, binding TEXT, seconds REAL, PRIMARY KEY(store,url));
            CREATE TABLE IF NOT EXISTS team_help(id TEXT PRIMARY KEY, store TEXT, sender TEXT,
              source_id TEXT, at TEXT, room TEXT, state TEXT, owner TEXT, stage INTEGER DEFAULT 0,
              UNIQUE(store,sender,source_id));
            CREATE TABLE IF NOT EXISTS team_message(id TEXT PRIMARY KEY, store TEXT, sender TEXT,
              source_id TEXT, at TEXT, body TEXT, UNIQUE(store,sender,source_id));
            CREATE TABLE IF NOT EXISTS watch_delivery(id TEXT PRIMARY KEY, store TEXT, staff TEXT,
              kind TEXT, ref TEXT, payload TEXT, created TEXT, sent TEXT, received TEXT,
              attempts INTEGER DEFAULT 0, UNIQUE(store,staff,kind,ref));
            CREATE TABLE IF NOT EXISTS watch_receipts(id INTEGER PRIMARY KEY AUTOINCREMENT,
              delivery_id TEXT, watch TEXT, at TEXT, latency_ms INTEGER);
            ''')
            if 'station_sent' not in [r[1] for r in db.execute('PRAGMA table_info(team_help)')]:
                db.execute('ALTER TABLE team_help ADD COLUMN station_sent INTEGER DEFAULT 0')

    def roster(self, sender):
        group = self.svc.roster.group_of(sender)
        with self.core._db() as db:
            recent = [r[0] for r in db.execute('SELECT staff FROM watch_delivery WHERE store=? AND ref IN (SELECT id FROM team_message WHERE store=? AND sender=?) ORDER BY rowid DESC', (self.cfg.store_id, self.cfg.store_id, sender))]
        rows = [dict(id=s, name=self.cfg.name(s), role=v.get('role', 'nurse'), roles=v.get('roles', []), same_floor=self.svc.roster.group_of(s) == group)
                for s, v in self.cfg.staff.items() if s != sender and v.get('on_duty', True) and self.cfg.watch_of(s) and self.svc._online(s)]
        rows.sort(key=lambda r: (not r['same_floor'], recent.index(r['id']) if r['id'] in recent else 999, r['name']))
        return rows

    def enqueue(self, staff, kind, ref, payload):
        with self.core._db() as db:
            db.execute('INSERT OR IGNORE INTO watch_delivery(id,store,staff,kind,ref,payload,created) VALUES(?,?,?,?,?,?,?)',
                       (uuid4().hex, self.cfg.store_id, staff, kind, ref, json.dumps(payload, ensure_ascii=False), self.svc._now().isoformat()))

    def receipt(self, watch, delivery):
        staff = self.cfg.staff_of_watch(watch)
        with self.core._transaction() as db:
            d = db.execute('SELECT * FROM watch_delivery WHERE id=? AND store=? AND staff=?', (delivery, self.cfg.store_id, staff)).fetchone()
            if not d or d['received']:
                return
            at = self.svc._now()
            db.execute('UPDATE watch_delivery SET received=? WHERE id=?', (at.isoformat(), delivery))
            latency = int((at - datetime.fromisoformat(d['created'])).total_seconds() * 1000)
            db.execute('INSERT INTO watch_receipts(delivery_id,watch,at,latency_ms) VALUES(?,?,?,?)', (delivery, watch, at.isoformat(), latency))

    def help(self, staff, source):
        with self.core._transaction() as db:
            old = db.execute('SELECT id FROM team_help WHERE store=? AND sender=? AND source_id=?', (self.cfg.store_id, staff, source)).fetchone()
            if old:
                return old['id']
            old = db.execute("SELECT id FROM team_help WHERE store=? AND sender=? AND state IN ('pending','claimed')", (self.cfg.store_id, staff)).fetchone()
            if old:
                return old['id']
            loc = next((l for l in self.svc.tele.latest_locations() if l['watch'] == self.cfg.watch_of(staff) and l['staff'] == staff), {})
            hid = uuid4().hex
            db.execute("INSERT INTO team_help(id,store,sender,source_id,at,room,state) VALUES(?,?,?,?,?,?,'pending')", (hid, self.cfg.store_id, staff, source, self.svc._now().isoformat(), loc.get('room')))
        self.tick_help()
        return hid

    def help_act(self, staff, hid, action):
        with self.core._transaction() as db:
            h = db.execute('SELECT * FROM team_help WHERE id=? AND store=?', (hid, self.cfg.store_id)).fetchone()
            if not h:
                raise WatchError('求助不存在')
            if action == 'claim':
                notified = db.execute("SELECT 1 FROM watch_delivery WHERE store=? AND staff=? AND ref=? AND kind='HELP_ALERT'", (self.cfg.store_id, staff, hid)).fetchone()
                if h['state'] != 'pending' or not notified or staff == h['sender']:
                    raise WatchError('求助已被响应或无接单权限')
                db.execute("UPDATE team_help SET state='claimed',owner=? WHERE id=?", (staff, hid))
            elif action in ('complete', 'cancel'):
                if (action == 'complete' and staff != h['owner']) or (action == 'cancel' and staff != h['sender']):
                    raise WatchError('无权结束求助')
                db.execute('UPDATE team_help SET state=? WHERE id=?', ('completed' if action == 'complete' else 'cancelled', hid))
            else:
                raise WatchError('未知求助操作')
            db.execute("UPDATE watch_delivery SET received=? WHERE store=? AND ref=? AND kind='HELP_ALERT' AND received IS NULL", (self.svc._now().isoformat(), self.cfg.store_id, hid))
            people = [r[0] for r in db.execute('SELECT staff FROM watch_delivery WHERE store=? AND ref=?', (self.cfg.store_id, hid))] + [h['sender'], staff]
        for s in set(people):
            self.enqueue(s, 'HELP_STATE', hid + ':' + action, dict(help_id=hid, status='claimed' if action == 'claim' else action, by=self.cfg.name(staff), owner=staff, sender=h['sender']))

    def tick_help(self):
        p = self.cfg.watch_policy
        with self.core._db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM team_help WHERE store=? AND state='pending'", (self.cfg.store_id,))]
        for h in rows:
            age = (self.svc._now() - datetime.fromisoformat(h['at'])).total_seconds()
            stage = 2 if age >= p['help_leader_seconds'] else 1 if age >= p['help_floor_seconds'] else 0
            group = self.svc.roster.group_of(h['sender'])
            floor = self.cfg.rooms.get(h['room'], {}).get('floor')
            candidates = [s for s, v in self.cfg.staff.items() if s != h['sender'] and v.get('on_duty', True) and self.svc._online(s) and self.svc.roster.state(s) == 'available' and (not self.busy(s))]
            if stage == 0:
                candidates = [s for s in candidates if self.svc.roster.group_of(s) == group]
                locs = {l['staff']: l['room'] for l in self.svc.tele.latest_locations()}
                distances = p.get('room_distances', {}).get(h['room'], {})
                candidates.sort(key=lambda s: (0 if locs.get(s) == h['room'] and h['room'] else distances.get(locs.get(s), 1000000), s))
                candidates = candidates[:1]
            elif stage == 1:
                candidates = [s for s in candidates if self.svc.roster.group_of(s) == group or (floor is not None and floor in self.cfg.staff[s].get('floors', []))]
            else:
                lead = self.svc.roster.leaders.get(group)
                candidates = [lead] if lead and self.svc._online(lead) else []
            for s in candidates:
                self.enqueue(s, 'HELP_ALERT', h['id'], dict(help_id=h['id'], sender=h['sender'], name=self.cfg.name(h['sender']), room=h['room'], stage=stage))
            if (stage >= 2 or not candidates) and (h['stage'] != stage or not h.get('station_sent')):
                from . import protocol as P
                self.svc.bus.publish(P.t_dispatch_station(self.cfg.store_id), self.svc._msg('HELP_ALERT', dict(help_id=h['id'], name=self.cfg.name(h['sender']), room=h['room'], stage=stage)))
                with self.core._db() as db:
                    db.execute('UPDATE team_help SET station_sent=1 WHERE id=?', (h['id'],))
            if stage > h['stage']:
                self.enqueue(h['sender'], 'HELP_STATE', h['id'] + ':stage:' + str(stage), dict(help_id=h['id'], status='escalated', stage=stage))
                with self.core._db() as db:
                    db.execute('UPDATE team_help SET stage=? WHERE id=?', (stage, h['id']))

    def busy(self, staff):
        with self.core._db() as db:
            customer = bool(db.execute("SELECT 1 FROM calls c LEFT JOIN notifications n ON c.id=n.call_id WHERE c.store=? AND c.state IN ('pending','claimed','arrived') AND (c.owner=? OR n.target=?) LIMIT 1", (self.cfg.store_id, staff, staff)).fetchone())
            helping = db.execute("SELECT 1 FROM team_help WHERE store=? AND owner=? AND state='claimed' LIMIT 1", (self.cfg.store_id, staff)).fetchone()
            return customer or bool(helping)

    def message(self, sender, source, body):
        p = self.cfg.watch_policy
        channel, target = body.get('channel'), body.get('target')
        if channel not in ('floor', 'department', 'company', 'direct', 'role'):
            raise WatchError('未知频道')
        text, voice, seconds = body.get('text'), body.get('voice_url'), body.get('seconds', 0)
        if voice:
            if text or not isinstance(voice, str) or not voice.startswith('/voice/') or '/' in voice[len('/voice/'):] or not isinstance(seconds, (int, float)) or not 0 < seconds <= p['voice_max_seconds']:
                raise WatchError('语音参数无效或超时')
            with self.core._db() as db:
                clip = db.execute('SELECT seconds FROM watch_voice WHERE store=? AND staff=? AND url=?', (self.cfg.store_id, sender, voice)).fetchone()
            if not clip or clip['seconds'] > p['voice_max_seconds']:
                raise WatchError('语音未通过本店上传校验或不属于本人')
        elif text not in p['quick_replies']:
            raise WatchError('请选择主机配置的快捷回复')
        roles = self.cfg.staff[sender].get('roles', []) + [self.cfg.role_of(sender)]
        if channel == 'company' and not set(roles).intersection(p['broadcast_roles']):
            raise WatchError('无全公司群发权限')
        recipients = [s for s, v in self.cfg.staff.items() if s != sender and v.get('on_duty', True) and self.cfg.watch_of(s)]
        if channel == 'floor':
            recipients = [s for s in recipients if self.svc.roster.group_of(s) == self.svc.roster.group_of(sender)]
        elif channel == 'department':
            dept = self.cfg.staff[sender].get('department')
            if not dept:
                raise WatchError('尚未配置部门资料')
            recipients = [s for s in recipients if self.cfg.staff[s].get('department') == dept]
        elif channel == 'direct':
            recipients = [s for s in recipients if s == target]
        elif channel == 'role':
            recipients = [s for s in recipients if target == self.cfg.role_of(s) or target in self.cfg.staff[s].get('roles', [])]
        if not recipients:
            raise WatchError('没有符合条件的当班收件人')
        encoded = json.dumps(body, sort_keys=True, ensure_ascii=False)
        with self.core._transaction() as db:
            old = db.execute('SELECT * FROM team_message WHERE store=? AND sender=? AND source_id=?', (self.cfg.store_id, sender, source)).fetchone()
            if old:
                if old['body'] != encoded:
                    raise WatchError('幂等键正文不一致')
                return old['id']
            last = db.execute('SELECT at FROM team_message WHERE store=? AND sender=? ORDER BY rowid DESC LIMIT 1', (self.cfg.store_id, sender)).fetchone()
            if last and (self.svc._now() - datetime.fromisoformat(last['at'])).total_seconds() < p['message_interval_seconds']:
                raise WatchError('发送过于频繁，请稍候')
            mid = uuid4().hex
            db.execute('INSERT INTO team_message VALUES(?,?,?,?,?,?)', (mid, self.cfg.store_id, sender, source, self.svc._now().isoformat(), encoded))
            for s in recipients:
                payload = dict(body, message_id=mid, sender=sender, name=self.cfg.name(sender))
                db.execute('INSERT INTO watch_delivery(id,store,staff,kind,ref,payload,created) VALUES(?,?,?,?,?,?,?)', (uuid4().hex, self.cfg.store_id, s, 'TEAM_MESSAGE', mid, json.dumps(payload, ensure_ascii=False), self.svc._now().isoformat()))
        return mid

    def flush(self):
        from . import protocol as P
        with self.core._db() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM watch_delivery WHERE store=? AND received IS NULL ORDER BY CASE kind WHEN \'CALL_DISPATCH\' THEN 0 WHEN \'HELP_ALERT\' THEN 1 ELSE 2 END,rowid LIMIT 500', (self.cfg.store_id,))]
        for d in rows:
            if not self.svc._online(d['staff']):
                continue
            if d['kind'] == 'TEAM_MESSAGE' and self.busy(d['staff']):
                continue
            if d['sent'] and (self.svc._now() - datetime.fromisoformat(d['sent'])).total_seconds() < self.cfg.watch_policy['retry_seconds']:
                continue
            if d['kind'] == 'CALL_DISPATCH':
                c = self.core.get(d['ref'], store=self.cfg.store_id)
                if c['state'] not in ('pending', 'claimed', 'arrived') or (c['owner'] and c['owner'] != d['staff'] and c['proposed_owner'] != d['staff']):
                    with self.core._db() as db:
                        db.execute('UPDATE watch_delivery SET received=? WHERE id=?', (self.svc._now().isoformat(), d['id']))
                    continue
            if d['kind'] == 'CALL_DISPATCH' and d['attempts'] == 3:
                if c['state'] == 'pending':
                    self.svc.roster.set_status(d['staff'], 'offline')
                self.svc._send_dispatch(d['ref'], 'station', 'no_device_receipt')
            payload = json.loads(d['payload'])
            if d['kind'] == 'CALL_DISPATCH':
                payload['status'] = {'claimed': 'accepted'}.get(c['state'], c['state'])
            payload['delivery_id'] = d['id']
            payload['binding_id'] = self.svc.watches.context(d['staff'])['binding']['bind_id']
            self.svc.bus.publish(P.t_dispatch_watch(self.cfg.store_id, self.cfg.watch_of(d['staff'])), self.svc._msg(d['kind'], payload, d['ref'] if d['kind'] == 'CALL_DISPATCH' else None))
            with self.core._db() as db:
                db.execute('UPDATE watch_delivery SET sent=?,attempts=attempts+1 WHERE id=?', (self.svc._now().isoformat(), d['id']))

    def tick(self):
        self.tick_help()
        self.flush()
