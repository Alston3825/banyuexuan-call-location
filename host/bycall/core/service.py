"""Host service: protocol handlers wiring workflow, dispatcher, locator, telemetry."""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from . import protocol as P
from .config import StoreConfig
from .dispatcher import STATION, Dispatcher, Roster
from .guest_buttons import GuestButtons
from .routing import CUSTOM, ButtonRouting
from .locator import Locator
from .telemetry import Telemetry
from .workflow import Conflict, Core

OFFLINE_AFTER = timedelta(seconds=90)   # 3 missed 30 s heartbeats
ACTIVE = ('claimed', 'arrived')


class HostService:
    def __init__(self, cfg: StoreConfig, bus, db_path, clock=None):
        self.cfg, self.bus = cfg, bus
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.core = Core(db_path, clock=self.clock)
        self.roster = Roster(cfg.groups, cfg.assignments, cfg.leaders)
        self.dispatcher = Dispatcher(self.core, self.roster, cfg.group_after, cfg.leader_after)
        self.tele = Telemetry(db_path)
        self.guest = GuestButtons(db_path)
        self.routing = ButtonRouting(db_path, cfg.roles)
        self.roster.roles = {s: cfg.role_of(s) for s in cfg.staff}
        self.locator = Locator(cfg.beacons)
        self.rejected_count = 0
        self._seen = OrderedDict()
        self._cursor = 0
        for pat in P.host_subscriptions(cfg.store_id):
            bus.subscribe(pat, self.on_message)

    # ---- helpers ---------------------------------------------------
    def _now(self):
        return self.clock()

    def _msg(self, type_, payload, call_id=None):
        return P.Envelope(store_id=self.cfg.store_id, device_id='host', type=type_, call_id=call_id,
                          payload=payload, timestamp=self._now().isoformat()).model_dump()

    def _reject(self, env, reason):
        self.rejected_count += 1
        self.bus.publish(P.t_reply(self.cfg.store_id, env.device_id),
                         self._msg(P.CMD_REJECTED, dict(reason=reason, ref=env.msg_id, type=env.type),
                                   env.call_id))

    def _feedback(self, room, call_id, status, nurse=None):
        text = P.FEEDBACK_TEXT[status].format(nurse=nurse or '')
        self.bus.publish(P.t_feedback(self.cfg.store_id, room),
                         self._msg(P.CALL_FEEDBACK, dict(status=status, text=text, nurse=nurse), call_id))

    def _flush(self):
        for e in self.core.events(after=self._cursor):
            self._cursor = e['seq']
            self.bus.publish(P.t_events(self.cfg.store_id), self._msg(
                'CALL_EVENT', dict(seq=e['seq'], kind=e['kind'], at=e['at'], version=e['version'],
                                   detail=json.loads(e['payload'])), e['call_id']))

    def _call(self, call_id):
        c = self.core.get(call_id, store=self.cfg.store_id)
        c['req'] = json.loads(c['request'])
        return c

    def _active_calls(self, staff):
        with self.core._db() as db:
            rows = db.execute("SELECT id,request,state FROM calls WHERE store=? AND owner=? "
                              "AND state IN ('claimed','arrived')", (self.cfg.store_id, staff)).fetchall()
        return [(r['id'], json.loads(r['request'])['room'], r['state']) for r in rows]

    def _refresh_staff(self, staff):
        if self.roster.state(staff) == 'offline' and not self._online(staff):
            return
        self.roster.set_status(staff, 'busy' if self._active_calls(staff) else 'available')

    def _online(self, staff):
        w = self.cfg.watch_of(staff)
        return any(d['device'] == w and d['online'] for d in self.tele.devices())

    def _act(self, call_id, actor, action, **kw):
        c = self.core.get(call_id, store=self.cfg.store_id)
        return self.core.act(call_id, store=self.cfg.store_id, actor=actor,
                             expected_version=c['version'], action=action, **kw)

    # ---- dispatch --------------------------------------------------
    def _dispatch_new(self):
        for call_id, target, reason in self.dispatcher.tick(self._now()):
            self._send_dispatch(call_id, target, reason)

    def _send_dispatch(self, call_id, target, reason):
        c = self._call(call_id)
        r = c['req']
        snap = r['snapshot']
        rinfo = self.cfg.rooms.get(r['room'], {})
        payload = dict(room=r['room'], room_no=rinfo.get('room_no', r['room']), floor=rinfo.get('floor'),
                       service_code=snap.get('service_code'),
                       label=snap.get('label'), voice_url=snap.get('voice_url'),
                       text=snap.get('text'), priority=r['priority'], reason=reason)
        if target == STATION:
            self.bus.publish(P.t_dispatch_station(self.cfg.store_id),
                             self._msg(P.CALL_DISPATCH, payload, call_id))
            return
        watch = self.cfg.watch_of(target)
        if watch:
            self.bus.publish(P.t_dispatch_watch(self.cfg.store_id, watch),
                             self._msg(P.CALL_DISPATCH, payload, call_id))

    def _state_to_watch(self, staff, call_id, status):
        watch = self.cfg.watch_of(staff)
        if watch:
            self.bus.publish(P.t_dispatch_watch(self.cfg.store_id, watch),
                             self._msg(P.CALL_STATE, dict(status=status), call_id))

    def _resync(self, staff):
        """A watch (re)connected: give it back every open call it was notified about or owns."""
        with self.core._db() as db:
            pend = db.execute("SELECT n.call_id, n.reason FROM notifications n JOIN calls c ON c.id=n.call_id "
                              "WHERE n.target=? AND c.store=? AND c.state='pending'",
                              (staff, self.cfg.store_id)).fetchall()
            own = db.execute("SELECT id, state FROM calls WHERE owner=? AND store=? AND state IN ('claimed','arrived')",
                             (staff, self.cfg.store_id)).fetchall()
        for r in pend:
            self._send_dispatch(r['call_id'], staff, r['reason'])
        for r in own:
            self._send_dispatch(r['id'], staff, 'resync')
            self._state_to_watch(staff, r['id'], 'accepted' if r['state'] == 'claimed' else r['state'])

    def _tell_others(self, call_id, except_staff, reason, by=None):
        for target, _ in self.dispatcher.notified(call_id):
            watch = self.cfg.watch_of(target)
            if target not in (except_staff, STATION) and watch:
                self.bus.publish(P.t_dispatch_watch(self.cfg.store_id, watch),
                                 self._msg(P.CALL_TAKEN, dict(reason=reason, by=by), call_id))

    def tick(self):
        now = self._now()
        for dev in self.tele.stale_online((now - OFFLINE_AFTER).isoformat()):
            self.tele.set_online(dev, False, now.isoformat())
            s = self.cfg.staff_of_watch(dev)
            if s:
                self.roster.set_status(s, 'offline')
        self._dispatch_new()
        self._flush()

    # ---- inbound ---------------------------------------------------
    def on_message(self, topic, raw):
        try:
            env = P.Envelope(**raw)
        except (ValidationError, TypeError):
            self.rejected_count += 1
            return
        if env.store_id != self.cfg.store_id:
            self.rejected_count += 1
            return
        parts = topic.split('/')
        # 带设备名的主题:信封里的设备必须与主题一致(配合 MQTT 按设备 ACL,防止冒用别的设备身份)
        if len(parts) == 3 and parts[1] in ('heartbeat', 'location', 'vitals', 'status') \
                and parts[2] != env.device_id:
            return self._reject(env, 'topic_device_mismatch')
        if env.msg_id in self._seen:
            return
        self._seen[env.msg_id] = 1
        if len(self._seen) > 5000:
            self._seen.popitem(last=False)
        handler = {
            P.CALL_REQUEST: self._h_request, P.CALL_ACCEPT: self._h_accept,
            P.CALL_ARRIVE: self._h_arrive, P.CALL_COMPLETE: self._h_complete,
            P.CALL_CANCEL: self._h_cancel, P.CALL_TRANSFER: self._h_transfer,
            P.HEARTBEAT: self._h_heartbeat, P.STATUS: self._h_status,
            P.LOCATION_REPORT: self._h_location, P.VITALS_REPORT: self._h_vitals,
        }.get(env.type)
        if not handler:
            return self._reject(env, 'unknown_type')
        try:
            handler(env)
        except ValidationError:
            self._reject(env, 'bad_payload')
        except KeyError:
            self._reject(env, 'unknown_call')
        self._flush()

    def _h_request(self, env):
        room = self.cfg.room_of_tablet(env.device_id)
        if not room:
            return self._reject(env, 'unknown_device')
        p = P.RequestPayload(**env.payload)
        if p.room != room:
            return self._reject(env, 'room_mismatch')
        service_code, label, text = p.service_code, self.cfg.request_types.get(p.service_code), p.text
        if service_code.startswith('GUEST_'):          # 客户专属按钮:必须是本房当前客户设置的
            g = next((x for x in self.guest_buttons(room) if x['code'] == service_code), None)
            if not g:
                return self._reject(env, 'unknown_service')
            # 对管理系统/手表统一记为 CUSTOM(自定义),具体内容放在名称里
            service_code, label, text = 'CUSTOM', g['label'], p.text or g['label']
        elif self.cfg.request_types and service_code not in self.cfg.request_types:
            return self._reject(env, 'unknown_service')
        priority = 'emergency' if 'EMERGENCY' in (service_code, p.priority.upper()) else 'normal'
        snap = dict(service_code=service_code, label=label, button_code=p.service_code,
                    voice_url=p.voice_url, text=text, stay_id=self.cfg.rooms[room].get('stay_id'),
                    role=self.routing.role_of(p.service_code if priority == 'normal' else 'EMERGENCY'))
        call_id = self.core.create(store=self.cfg.store_id, device=env.device_id,
                                   event_id=env.msg_id, room=room, priority=priority, snapshot=snap)
        self._feedback(room, call_id, 'received')
        self._dispatch_new()

    def _staff_or_reject(self, env):
        s = self.cfg.staff_of_watch(env.device_id)
        if not s:
            self._reject(env, 'unknown_device')
        return s

    def _h_accept(self, env):
        staff = self._staff_or_reject(env)
        if not staff or not env.call_id:
            return
        c = self._call(env.call_id)
        try:
            if c['state'] in ACTIVE and c['proposed_owner'] == staff:
                c = self._act(env.call_id, staff, 'accept_transfer')
            else:
                c = self._act(env.call_id, staff, 'claim')
        except Conflict:
            if self._call(env.call_id)['owner'] == staff:
                return                                   # duplicate delivery
            return self._reject(env, 'already_taken_or_closed')
        self.roster.set_status(staff, 'busy')
        self._feedback(c_room(c), env.call_id, 'accepted', self.cfg.name(staff))
        self._state_to_watch(staff, env.call_id, 'accepted')
        self._tell_others(env.call_id, staff, 'taken', self.cfg.name(staff))

    def _h_arrive(self, env):
        staff = self._staff_or_reject(env)
        if staff:
            self._arrive(env.call_id, staff, 'manual', env)

    def _arrive(self, call_id, staff, method, env=None):
        try:
            c = self._act(call_id, staff, 'arrive', method=method)
        except Conflict:
            return self._reject(env, 'arrive_not_allowed') if env else None
        self._feedback(c_room(c), call_id, 'arrived', self.cfg.name(staff))
        self._state_to_watch(staff, call_id, 'arrived')

    def _h_complete(self, env):
        staff = self._staff_or_reject(env)
        if not staff:
            return
        method = env.payload.get('method', 'onsite')
        try:
            c = self._act(env.call_id, staff, 'complete', method=method,
                          reason=env.payload.get('reason') or '已处理')
        except (Conflict, ValueError):
            return self._reject(env, 'complete_not_allowed')
        self._refresh_staff(staff)
        self._feedback(c_room(c), env.call_id, 'completed', self.cfg.name(staff))
        self._state_to_watch(staff, env.call_id, 'completed')

    def _h_cancel(self, env):
        staff = self.cfg.staff_of_watch(env.device_id)
        room = self.cfg.room_of_tablet(env.device_id)
        if not (staff or room):
            return self._reject(env, 'unknown_device')
        call = self._call(env.call_id)
        if room and call['req']['room'] != room:
            return self._reject(env, 'room_mismatch')
        try:
            c = self._act(env.call_id, staff or f'room:{room}', 'cancel')
        except Conflict:
            return self._reject(env, 'cancel_not_allowed')
        if call['owner']:
            self._refresh_staff(call['owner'])
        self._feedback(c_room(c), env.call_id, 'cancelled')
        self._tell_others(env.call_id, None, 'cancelled')

    def _h_transfer(self, env):
        staff = self._staff_or_reject(env)
        if not staff:
            return
        target = env.payload.get('target')
        if target not in self.cfg.staff:
            return self._reject(env, 'unknown_target')
        try:
            self._act(env.call_id, staff, 'propose_transfer', target=target,
                      reason=env.payload.get('reason') or '转交')
        except (Conflict, ValueError):
            return self._reject(env, 'transfer_not_allowed')
        self._send_dispatch(env.call_id, target, 'transfer')

    def stay_key(self, room):
        """专属按钮跟着"这一次入住"走;没接管理系统时退回按房间。"""
        return self.cfg.rooms.get(room, {}).get('stay_id') or f'room:{room}'

    def guest_buttons(self, room):
        return self.guest.get(self.stay_key(room))

    def tablet_config(self, dev=None):
        """按钮列表 = 门店通用按钮(服务项字典) + 本房客户专属按钮(最多 2 个) ;紧急固定最后且需长按。版本号随内容变化。"""
        codes = [c for c in self.cfg.request_types if c != 'EMERGENCY']
        buttons = [dict(code=c, label=self.cfg.request_types[c], hold=False) for c in codes]
        room = self.cfg.room_of_tablet(dev) if dev else None
        if codes or room:
            # 自定义按钮永远占第 6 格(没设置时 label 为空,平板留空位,版面不变)
            g = (self.guest_buttons(room) if room else [])[:GuestButtons.MAX]
            buttons.append(dict(code='GUEST_1', label=g[0]['label'] if g else '', hold=False, guest=True))
        if 'EMERGENCY' in self.cfg.request_types:
            buttons.append(dict(code='EMERGENCY', label=self.cfg.request_types['EMERGENCY'], hold=True))
        ver = hashlib.md5(json.dumps(buttons, ensure_ascii=False).encode()).hexdigest()[:8]
        return dict(ver=ver, buttons=buttons)

    def routing_view(self):
        """后台"按钮由谁接收"设置页用:每个按钮当前的接收岗位,以及可选岗位及在线人数。"""
        mine = self.routing.all()
        items = [dict(code=c, label=l, role=self.routing.role_of(c)) for c, l in self.cfg.request_types.items() if c != 'EMERGENCY']
        items.append(dict(code=CUSTOM, label='客户自定义按钮', role=self.routing.role_of(CUSTOM)))
        roles = [dict(id=r, name=n, people=[self.cfg.name(s) for s in self.cfg.staff if self.cfg.role_of(s) == r])
                 for r, n in self.cfg.roles.items()]
        return dict(items=items, roles=roles, emergency_role='nurse')

    def set_routing(self, mapping):
        valid = {c for c in self.cfg.request_types if c != 'EMERGENCY'} | {CUSTOM}
        self.routing.set(mapping, valid)
        return self.routing_view()

    def _send_config(self, dev):
        self.bus.publish(P.t_reply(self.cfg.store_id, dev), self._msg(P.DEVICE_CONFIG, self.tablet_config(dev)))

    def set_guest_buttons(self, room, labels):
        """员工为本房当前客户设置专属按钮,并立即推给平板。"""
        if room not in self.cfg.rooms:
            raise KeyError(room)
        out = self.guest.set(self.stay_key(room), labels)
        dev = self.cfg.rooms[room].get('tablet')
        if dev:
            self._send_config(dev)
        return out

    def _h_heartbeat(self, env):
        p = P.HeartbeatPayload(**env.payload)
        if self.cfg.room_of_tablet(env.device_id) and self.cfg.request_types \
                and p.cfg_ver != self.tablet_config(env.device_id)['ver']:
            self._send_config(env.device_id)        # 按钮列表变了或平板还没有,30 秒内自动更新
        self.tele.heartbeat(env.device_id, self._now().isoformat(), p.battery, p.wifi_rssi, p.charging)
        staff = self.cfg.staff_of_watch(env.device_id)
        if staff:
            was_off = self.roster.state(staff) == 'offline'
            self.roster.set_status(staff, 'busy' if self._active_calls(staff) else 'available')
            if was_off:
                self._resync(staff)
            self._dispatch_new()

    def _h_status(self, env):
        online = bool(env.payload.get('online'))
        self.tele.set_online(env.device_id, online, self._now().isoformat())
        if online and self.cfg.room_of_tablet(env.device_id) and self.cfg.request_types:
            self._send_config(env.device_id)
        staff = self.cfg.staff_of_watch(env.device_id)
        if staff and not online:
            self.roster.set_status(staff, 'offline')
        if staff and online:
            self.roster.set_status(staff, 'busy' if self._active_calls(staff) else 'available')
            self._resync(staff)
            self._dispatch_new()

    def _h_location(self, env):
        staff = self.cfg.staff_of_watch(env.device_id)
        if not staff:
            return self._reject(env, 'unknown_device')
        p = P.LocationPayload(**env.payload)
        fix = self.locator.update(env.device_id, [r.model_dump() for r in p.readings])
        self.tele.add_location(env.device_id, staff, fix, self._now().isoformat())
        if fix and fix['room']:
            for call_id, room, state in self._active_calls(staff):
                if state == 'claimed' and room == fix['room']:
                    self._arrive(call_id, staff, 'ble')

    def _h_vitals(self, env):
        staff = self._staff_or_reject(env)
        if staff:
            v = P.VitalsPayload(**env.payload)
            self.tele.add_vitals(staff, env.device_id, v.measured_at or self._now().isoformat(),
                                 v.model_dump())


def c_room(call):
    return call['req']['room'] if 'req' in call else json.loads(call['request'])['room']
