"""Host service: protocol handlers wiring workflow, dispatcher, locator, telemetry.

2026-11-17 起新增:派单回执与重发、一键求助、同事沟通、换表/回绑/归还重置。
业务归属永远以 staff_id 为准;"谁戴哪块表"由 WatchRegistry 决定(不再只看门店配置里的 staff.watch)。
"""
from __future__ import annotations

import hashlib
import uuid
import json
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from . import protocol as P
from .chat import ChatStore, dm_id, ts
from .config import StoreConfig
from .delivery import Delivery
from .dispatcher import STATION, Dispatcher, Roster, shift_of
from .guest_buttons import GuestButtons
from .routing import CUSTOM, ButtonRouting
from .locator import Locator
from .location_state import LocationReports
from .telemetry import Telemetry
from .terminal_config import TerminalConfigs
from .watches import STATUS_TEXT, WatchError, WatchRegistry
from .workflow import Conflict, Core

OFFLINE_AFTER = timedelta(seconds=90)   # 3 missed 30 s heartbeats
ACTIVE = ('claimed', 'arrived')
HELP_TEXT = dict(
    sent='求助已发出,已通知 {n} 位同事', alone='附近没有空闲的同事,已通知护士站',
    floor='暂无人响应,已扩大到整层', leader='仍无人响应,已通知护士长和护士站',
    accepted='{by} 已响应,正在赶来', arrived='{by} 已到达', completed='求助已结束', cancelled='求助已取消')
REJECT_TEXT = dict(
    not_bound='这块表还没有绑定护理师,请到护士站领用', too_fast='发得太快了,请稍等两秒',
    rate_limited='一分钟内发得太多,请稍后再发', no_permission='没有全公司群发权限',
    no_one_on_duty='该岗位当前没有在班的人', not_member='不在这个频道里', voice_too_long='语音太长了',
    bad_message='消息内容不对', own_help='不能接自己的求助', help_open='已有一个求助在进行中')


class HostService:
    def __init__(self, cfg: StoreConfig, bus, db_path, clock=None):
        self.cfg, self.bus = cfg, bus
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.core = Core(db_path, clock=self.clock)
        self.roster = Roster(cfg.groups, cfg.assignments, cfg.leaders)
        self.dispatcher = Dispatcher(self.core, self.roster, cfg.group_after, cfg.leader_after)
        self.dispatcher.help_planner = self._help_targets
        self.tele = Telemetry(db_path)
        self.terminal_configs = TerminalConfigs(db_path, cfg.store_id)
        saved_beacons = self.terminal_configs.beacons()['mapping']
        if saved_beacons is not None:
            cfg.beacons = saved_beacons
        self.guest = GuestButtons(db_path)
        self.routing = ButtonRouting(db_path, cfg.roles)
        self.watches = WatchRegistry(db_path)
        self.delivery = Delivery(db_path)
        self.chat = ChatStore(db_path)
        self.watches.sync_from_config(cfg.staff, cfg.spare_watches, self._now().isoformat())
        self.roster.roles = {s: cfg.role_of(s) for s in cfg.staff}
        self.locator = Locator(cfg.beacons)
        self.location_reports = LocationReports(db_path, cfg.store_id, uuid.uuid4().hex)
        self.rejected_count = 0
        self.caps = {}                  # watch -> set(能力),来自心跳;老 APK 没有 → 不等回执
        self.unreachable = {}           # staff -> 起始时间:连续收不到派单(无回执),暂按离线处理
        self.latency = []               # 最近的送达耗时(毫秒),大屏/报表用
        self._help_stage = {}           # call_id -> 已告诉求助人的阶段
        self._seen = OrderedDict()
        self._cursor = 0
        self._ticks = 0
        for pat in P.host_subscriptions(cfg.store_id):
            bus.subscribe(pat, self.on_message)

    # ---- helpers ---------------------------------------------------
    def _now(self):
        return self.clock()

    def opt(self, section, key):
        return self.cfg.opt(section, key)

    def watch_of(self, staff):
        return self.watches.watch_of(staff)

    def staff_of_watch(self, watch):
        return self.watches.staff_of(watch)

    def _msg(self, type_, payload, call_id=None):
        return P.Envelope(store_id=self.cfg.store_id, device_id='host', type=type_, call_id=call_id,
                          payload=payload, timestamp=self._now().isoformat()).model_dump()

    def _reject(self, env, reason):
        self.rejected_count += 1
        self.bus.publish(P.t_reply(self.cfg.store_id, env.device_id),
                         self._msg(P.CMD_REJECTED, dict(reason=reason, ref=env.msg_id, type=env.type,
                                                        text=REJECT_TEXT.get(reason)), env.call_id))

    def _to_watch(self, staff, type_, payload, call_id=None):
        w = self.watch_of(staff)
        if w:
            self.bus.publish(P.t_dispatch_watch(self.cfg.store_id, w), self._msg(type_, payload, call_id))
        return w

    def _feedback(self, room, call_id, status, nurse=None):
        text = P.FEEDBACK_TEXT[status].format(nurse=nurse or '')
        self.bus.publish(P.t_feedback(self.cfg.store_id, room),
                         self._msg(P.CALL_FEEDBACK, dict(status=status, text=text, nurse=nurse), call_id))

    def _progress(self, c, call_id, status, nurse=None):
        """进度回执:客户呼叫 → 房间平板;同事求助 → 求助人的手表(不能让客户平板看到)。"""
        snap = json.loads(c['request'])['snapshot']
        if snap.get('kind') == 'help':
            self._help_state(snap['requester'], call_id, status, by=nurse)
        else:
            self._feedback(c_room(c), call_id, status, nurse)

    def _help_state(self, requester, call_id, status, by=None, n=None):
        text = HELP_TEXT.get(status, status).format(by=by or '', n=n or 0)
        self._to_watch(requester, P.HELP_STATE, dict(status=status, by=by, text=text), call_id)

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

    def _ringing(self, staff, customer_only=True):
        """正在给这个人响的、还没人接的呼叫(同事消息要给它让路)。"""
        with self.core._db() as db:
            rows = db.execute("SELECT c.id, c.request FROM notifications n JOIN calls c ON c.id=n.call_id "
                              "WHERE n.target=? AND c.store=? AND c.state='pending'",
                              (staff, self.cfg.store_id)).fetchall()
        return [r['id'] for r in rows
                if not customer_only or json.loads(r['request'])['snapshot'].get('kind') != 'help']

    def _set_live_status(self, staff):
        if staff in self.unreachable:
            self.roster.set_status(staff, 'offline')
        else:
            self.roster.set_status(staff, 'busy' if self._active_calls(staff) else 'available')

    def _refresh_staff(self, staff):
        if self.roster.state(staff) == 'offline' and not self._online(staff):
            return
        self._set_live_status(staff)

    def _online(self, staff):
        w = self.watch_of(staff)
        return bool(w) and any(d['device'] == w and d['online'] for d in self.tele.devices())

    def _device(self, dev):
        r = self.tele._run('SELECT * FROM device_status WHERE device=?', (dev,))
        return r[0] if r else {}

    def _fresh_online(self, dev):
        d = self._device(dev)
        return bool(d.get('online')) and d.get('last_seen', '') >= (self._now() - OFFLINE_AFTER).isoformat()

    def _act(self, call_id, actor, action, **kw):
        c = self.core.get(call_id, store=self.cfg.store_id)
        return self.core.act(call_id, store=self.cfg.store_id, actor=actor,
                             expected_version=c['version'], action=action, **kw)

    # ---- 位置 / 楼层 -------------------------------------------------
    def location_views(self):
        reports = self.location_reports.rows()
        devices = {d['device']: d for d in self.tele.devices()}
        mapping = self.terminal_configs.beacons()['mapping']
        if mapping is None:
            mapping = self.cfg.beacons
        views = []
        now = self._now()
        for reg in self.watches.all():
            watch, staff = reg['sn'], reg.get('bound_staff')
            d = devices.get(watch, {})
            online = bool(d.get('online')) and d.get('last_seen', '') >= (now - OFFLINE_AFTER).isoformat()
            view = self.location_reports.describe(reports.get(watch), watch, staff, reg.get('last_bind_time'),
                                                  now, online, mapping)
            # Last-known fixes are historical evidence; never expose another wearer's data.
            if staff is None:
                view.update(location_status='unknown', location_reason='unbound',
                            beacon=None, room=None, area=None, floor=None, rssi=None, at=None, last_known=None)
            if view['location_status'] == 'fresh' and view['floor'] is None:
                view['floor'] = self._room_floor(view['room'])
            views.append(view)
        return views

    def _location(self, staff):
        return next((v for v in self.location_views()
                     if v['staff'] == staff and v['location_status'] == 'fresh'), None)

    def _room_floor(self, room):
        f = self.cfg.rooms.get(room or '', {}).get('floor')
        return str(f) if f is not None else None

    def _home_floors(self, staff):
        info = self.cfg.staff.get(staff, {})
        fl = {str(f) for f in (info.get('floors') or [])}
        if info.get('floor') is not None:
            fl.add(str(info['floor']))
        for room, a in self.roster.assignments.items():
            if staff in a.values() and self._room_floor(room):
                fl.add(self._room_floor(room))
        return fl

    def _staff_floor(self, staff):
        """当前所在楼层:优先看最近一次定位,其次看负责的楼层。"""
        loc = self._location(staff)
        if loc and loc.get('beacon'):
            f = self._room_floor(loc.get('room')) or self.cfg.beacons.get(loc['beacon'], {}).get('floor')
            if f is not None:
                return str(f)
        home = sorted(self._home_floors(staff))
        return home[0] if home else None

    def _nurse_like(self, staff):
        return self.cfg.role_of(staff) == 'nurse' or staff in self.roster.leaders.values()

    def _served_rooms(self, staff):
        shift, _ = shift_of(self._now())
        rooms = [r for r, a in self.roster.assignments.items() if a.get(shift) == staff]
        return sorted(rooms, key=lambda r: (self.cfg.rooms.get(r, {}).get('room_no') or r))

    # ---- dispatch --------------------------------------------------
    def _dispatch_new(self):
        for call_id, target, reason in self.dispatcher.tick(self._now()):
            self._send_dispatch(call_id, target, reason)
            if reason.startswith('help_'):
                self._help_escalation(call_id, reason)

    def _help_escalation(self, call_id, reason):
        stage = {'help_floor': 'floor', 'help_leader': 'leader', 'help_timeout': 'leader'}.get(reason)
        done = self._help_stage.setdefault(call_id, set())
        if stage and stage not in done and 'sent' in done:
            done.add(stage)
            c = self._call(call_id)
            if c['state'] == 'pending':
                self._help_state(c['req']['snapshot']['requester'], call_id, stage)

    def _send_dispatch(self, call_id, target, reason):
        c = self._call(call_id)
        r = c['req']
        snap = r['snapshot']
        rinfo = self.cfg.rooms.get(r['room'], {})
        payload = dict(room=r['room'], room_no=rinfo.get('room_no', r['room']), floor=rinfo.get('floor', snap.get('floor')),
                       service_code=snap.get('service_code'),
                       label=snap.get('label'), voice_url=snap.get('voice_url'),
                       text=snap.get('text'), priority=r['priority'], reason=reason,
                       kind=snap.get('kind', 'call'))
        if target == STATION:
            self.bus.publish(P.t_dispatch_station(self.cfg.store_id),
                             self._msg(P.CALL_DISPATCH, payload, call_id))
            return
        watch = self._to_watch(target, P.CALL_DISPATCH, payload, call_id)
        if watch and c['state'] == 'pending':
            self.delivery.sent(call_id, target, watch, snap.get('kind', 'call'), self._now().isoformat())

    def _state_to_watch(self, staff, call_id, status):
        self._to_watch(staff, P.CALL_STATE, dict(status=status), call_id)

    def _resync(self, staff):
        """A watch (re)connected or was just bound: give it back every open call it was notified about or owns,
        its own open help request, and colleague messages it has not confirmed."""
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
        h = self._open_help(staff)
        if h:
            st = {'pending': 'sent', 'claimed': 'accepted', 'arrived': 'arrived'}[h['state']]
            self._help_state(staff, h['id'], st, by=self.cfg.name(h['owner']) if h['owner'] else None,
                             n=len(self.dispatcher.notified(h['id'])))
        since = (self._now() - timedelta(hours=12)).isoformat()
        for m in self.chat.undelivered(staff, since):
            self._deliver_chat(m, staff)

    def _tell_others(self, call_id, except_staff, reason, by=None):
        for target, _ in self.dispatcher.notified(call_id):
            if target not in (except_staff, STATION):
                self._to_watch(target, P.CALL_TAKEN, dict(reason=reason, by=by), call_id)

    def tick(self):
        now = self._now()
        self._ticks += 1
        if self._ticks % 10 == 0:          # 管理系统同步可能改了人员/手表:补登记新表(不覆盖换表结果)
            self.watches.sync_from_config(self.cfg.staff, self.cfg.spare_watches, now.isoformat())
        for dev in self.tele.stale_online((now - OFFLINE_AFTER).isoformat()):
            self.tele.set_online(dev, False, now.isoformat())
            s = self.staff_of_watch(dev)
            if s:
                self.roster.set_status(s, 'offline')
        self._resend_unreceived(now)
        self._dispatch_new()
        self._flush_chat_queue(now)
        self._flush()

    def _resend_unreceived(self, now):
        """派单发出 N 秒没回执就重发;重发到上限仍没回执 → 这块表视为收不到,呼叫转给其他人,大屏提醒。"""
        every, most = self.opt('watch', 'receipt_resend_s'), self.opt('watch', 'receipt_max_sends')
        for d in self.delivery.due((now - timedelta(seconds=every)).isoformat()):
            if 'receipt' not in self.caps.get(d['watch'], ()) or self.watch_of(d['target']) != d['watch']:
                continue                               # 老版本 APK 不回执;或已换表(换表时会重新派)
            try:
                c = self._call(d['call_id'])
            except KeyError:
                continue
            if c['state'] != 'pending':
                continue
            if d['sends'] >= most:
                self.delivery.fail(d['call_id'], d['target'])
                if d['target'] not in self.unreachable:
                    self.unreachable[d['target']] = now.isoformat()
                    self.roster.set_status(d['target'], 'offline')
                continue
            self._send_dispatch(d['call_id'], d['target'], 'resend')

    def terminal_kind(self, device):
        if self.cfg.room_of_tablet(device):
            return 'tablet'
        if self.watches.get(device):
            return 'watch'
        return None

    def push_terminal_config(self, device):
        kind = self.terminal_kind(device)
        if not kind:
            return
        c = self.terminal_configs.get(kind, device)
        if c['revision']:
            self.bus.publish(P.t_reply(self.cfg.store_id, device), self._msg('TERMINAL_CONFIG',
                dict(kind=kind, device=device, revision=c['revision'], settings=c['settings'])))

    def _terminal_heartbeat(self, env):
        kind = self.terminal_kind(env.device_id)
        if not kind:
            return
        p = env.payload
        capable = p.get('terminal_config_v') == 1
        self.terminal_configs.seen(kind, env.device_id, capable)
        if not capable:
            return
        c = self.terminal_configs.get(kind, env.device_id)
        rev = p.get('terminal_revision', 0)
        status = p.get('terminal_result', 'applied')
        if rev == c['revision'] and rev > 0 and status in ('applied', 'failed', 'applying'):
            self.terminal_configs.ack(kind, env.device_id, rev, status,
                                      '终端应用失败或连接验证超时，已回退' if status == 'failed' else '')
            if status in ('applied', 'applying'):
                self.bus.publish(P.t_reply(self.cfg.store_id, env.device_id), self._msg('TERMINAL_CONFIRM', dict(revision=rev, session=str(p.get('terminal_session', ''))[:64])))
        elif c['revision']:
            self.push_terminal_config(env.device_id)

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
        if env.type == P.HEARTBEAT and topic != P.t_heartbeat(self.cfg.store_id, env.device_id):
            return self._reject(env, 'topic_device_mismatch')
        if env.type == P.LOCATION_REPORT and topic != P.t_location(self.cfg.store_id, env.device_id):
            return self._reject(env, 'topic_device_mismatch')
        parts = topic.split('/')
        # 带设备名的主题:信封里的设备必须与主题一致(配合 MQTT 按设备 ACL,防止冒用别的设备身份)
        if len(parts) == 3 and parts[1] in ('heartbeat', 'location', 'vitals', 'status', 'up') \
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
            P.CALL_RECEIVED: self._h_received, P.HELP_REQUEST: self._h_help,
            P.CHAT_SEND: self._h_chat_send, P.CHAT_ACK: self._h_chat_ack,
            P.RESET_DONE: self._h_reset_done, P.RETURN_HOME: self._h_return_home,
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
        s = self.staff_of_watch(env.device_id)
        if not s:
            self._reject(env, 'not_bound' if self.watches.get(env.device_id) else 'unknown_device')
        return s

    def _h_received(self, env):
        """手表回执:这条派单已经到手表上了。"""
        staff = self.staff_of_watch(env.device_id)
        if not staff or not env.call_id:
            return
        ms = self.delivery.received(env.call_id, staff, self._now().isoformat())
        if ms is not None:
            self.latency = (self.latency + [ms])[-200:]
        if self.unreachable.pop(staff, None):
            self._set_live_status(staff)

    def _h_accept(self, env):
        staff = self._staff_or_reject(env)
        if not staff or not env.call_id:
            return
        c = self._call(env.call_id)
        if c['req']['snapshot'].get('requester') == staff:
            return self._reject(env, 'own_help')
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
        self._progress(c, env.call_id, 'accepted', self.cfg.name(staff))
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
        self._progress(c, call_id, 'arrived', self.cfg.name(staff))
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
        self._progress(c, env.call_id, 'completed', self.cfg.name(staff))
        self._state_to_watch(staff, env.call_id, 'completed')

    def _h_cancel(self, env):
        staff = self.staff_of_watch(env.device_id)
        room = self.cfg.room_of_tablet(env.device_id)
        if not (staff or room):
            return self._reject(env, 'unknown_device')
        call = self._call(env.call_id)
        if room and call['req']['room'] != room:
            return self._reject(env, 'room_mismatch')
        snap = call['req']['snapshot']
        if staff and snap.get('kind') == 'help' and staff not in (snap.get('requester'), call['owner']):
            return self._reject(env, 'cancel_not_allowed')
        try:
            c = self._act(env.call_id, staff or f'room:{room}', 'cancel')
        except Conflict:
            return self._reject(env, 'cancel_not_allowed')
        if call['owner']:
            self._refresh_staff(call['owner'])
        self._progress(c, env.call_id, 'cancelled')
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

    # ---- 一键求助 ----------------------------------------------------
    def _open_help(self, staff):
        for st in ('pending', 'claimed', 'arrived'):
            for c in self.core.list_calls(store=self.cfg.store_id, state=st, limit=200):
                if json.loads(c['request'])['snapshot'].get('requester') == staff:
                    return c
        return None

    def _h_help(self, env):
        staff = self._staff_or_reject(env)
        if not staff:
            return
        old = self._open_help(staff)
        if old:                                         # 重复按:不新建,把当前进度再告诉一次
            st = {'pending': 'sent', 'claimed': 'accepted', 'arrived': 'arrived'}[old['state']]
            return self._help_state(staff, old['id'], st, by=self.cfg.name(old['owner']) if old['owner'] else None,
                                    n=len(self.dispatcher.notified(old['id'])))
        loc = self._location(staff) or {}
        room_id = loc.get('room')
        floor = self._staff_floor(staff)
        area = loc.get('area') or (self.cfg.rooms.get(room_id, {}).get('room_no') if room_id else None)
        name = self.cfg.name(staff)
        snap = dict(kind='help', requester=staff, requester_name=name, service_code='HELP', label=f'{name}求助',
                    text=(f'位置:{area}' if area else '位置未知') + (f' · {env.payload.get("text")}' if env.payload.get('text') else ''),
                    room_id=room_id, area=area, floor=floor, role='nurse')
        call_id = self.core.create(store=self.cfg.store_id, device=env.device_id, event_id=env.msg_id,
                                   room=room_id or area or '位置未知', priority='normal', snapshot=snap)
        self._help_stage[call_id] = {'sent'}
        self._dispatch_new()
        people = [t for t, _ in self.dispatcher.notified(call_id) if t != STATION]
        self._help_state(staff, call_id, 'sent' if people else 'alone', n=len(people))

    def _help_targets(self, snap, elapsed, now):
        """求助派给谁:同组、在线、离得最近且空闲的 N 人 → 超时扩大到整层 → 再超时通知护士长和护士站。"""
        req = snap['requester']
        floor, room = snap.get('floor'), snap.get('room_id')
        group = self.roster.group_of(req)
        members = [m for m in self.roster.groups.get(group, [])] if group else []
        up = lambda s: s != req and self.roster.state(s) != 'offline' and self.watch_of(s)

        def rank(s):
            loc = self._location(s) or {}
            near = 0 if room and loc.get('room') == room else 1 if floor and self._staff_floor(s) == floor else 2
            return (near, 0 if self.roster.state(s) == 'available' else 1, self.cfg.name(s))

        out = [(m, 'help_near') for m in sorted([m for m in members if up(m) and self.roster.state(m) == 'available'],
                                                key=rank)[:self.opt('help', 'first_count')]]
        if elapsed >= self.opt('help', 'floor_after') or not out:
            same_floor = [s for s in self.cfg.staff if up(s) and self._nurse_like(s)
                          and (floor is None or self._staff_floor(s) == floor or floor in self._home_floors(s))]
            out += [(s, 'help_floor') for s in sorted(set(same_floor) | {m for m in members if up(m)}, key=rank)]
        if elapsed >= self.opt('help', 'leader_after'):
            leads = {self.roster.leaders.get(group)} if group else set()
            leads |= {v for g, v in self.roster.leaders.items() if floor and floor in self._home_floors(v)}
            out += [(l, 'help_leader') for l in sorted(x for x in leads if x and x != req and self.roster.state(x) != 'offline')]
            out.append((STATION, 'help_timeout'))
        if not any(t != STATION for t, _ in out):
            out.append((STATION, 'no_available_staff'))
        return out

    # ---- 同事沟通 ----------------------------------------------------
    def _can_broadcast(self, staff):
        roles = self.opt('chat', 'broadcast_roles')
        return self.cfg.role_of(staff) in roles or ('leader' in roles and staff in self.roster.leaders.values())

    def _my_floors(self, staff):
        f = self._home_floors(staff)
        cur = self._staff_floor(staff)
        return f | ({cur} if cur else set())

    def _members(self, channel):
        if channel.startswith('dm:'):
            return channel[3:].split('|', 1)
        if channel.startswith('floor:'):
            f = channel[6:]
            return [s for s in self.cfg.staff if f in self._my_floors(s)]
        if channel == 'dept':
            return [s for s in self.cfg.staff if self._nurse_like(s)]
        if channel == 'all':
            return list(self.cfg.staff)
        return []

    def _on_duty(self, staff):
        return self.cfg.staff.get(staff, {}).get('on_duty', True) and self._online(staff) and staff not in self.unreachable

    def _channel_name(self, channel, viewer):
        if channel.startswith('floor:'):
            return f'{channel[6:]}楼群'
        if channel.startswith('dm:'):
            a, b = channel[3:].split('|', 1)
            return self.cfg.name(b if a == viewer else a)
        return {'dept': '护理部群', 'all': '全公司'}.get(channel, channel)

    def _chat_payload(self, m, viewer):
        return dict(id=m['id'], channel=m['channel'], channel_name=self._channel_name(m['channel'], viewer),
                    sender=m['sender'], sender_name=self.cfg.name(m['sender']), mine=m['sender'] == viewer,
                    kind=m['kind'], text=m['text'], voice_url=m['voice_url'], duration_s=m['duration_s'],
                    note=m['note'], at=m['at'])

    def _resolve_channel(self, staff, ch):
        """返回 (频道 id, 备注) 或抛 ValueError(拒绝原因)。"""
        if not ch:
            f = self._staff_floor(staff)
            ch = f'floor:{f}' if f else 'dept'
        if ch.startswith('floor:'):
            if ch[6:] not in self._my_floors(staff):
                raise ValueError('not_member')
            return ch, None
        if ch == 'dept':
            if not self._nurse_like(staff):
                raise ValueError('not_member')
            return ch, None
        if ch == 'all':
            if not self._can_broadcast(staff):
                raise ValueError('no_permission')
            return ch, None
        if ch.startswith('dm:'):
            other = ch[3:]
            if '|' in other:                       # 手表回复时可能直接带完整的 dm 频道 id
                a, b = other.split('|', 1)
                if staff not in (a, b):
                    raise ValueError('not_member')
                other = b if a == staff else a
            if other not in self.cfg.staff or other == staff:
                raise ValueError('bad_message')
            return dm_id(staff, other), None
        if ch.startswith('role:'):
            role = ch[5:]
            if role not in self.cfg.roles and role != 'leader':      # leader = 各组组长(护士长),由排班推出
                raise ValueError('bad_message')
            me = self._staff_floor(staff)
            cand = [s for s in self.cfg.staff if s != staff and self.cfg.role_of(s) == role and self._on_duty(s)]
            if role == 'leader':
                cand = [s for s in set(self.roster.leaders.values()) if s != staff and self._on_duty(s)]
            if not cand:
                raise ValueError('no_one_on_duty')
            pick = sorted(cand, key=lambda s: (0 if me and self._staff_floor(s) == me else 1, self.cfg.name(s)))[0]
            return dm_id(staff, pick), f'按岗位呼叫:{self.cfg.roles.get(role, "护士长" if role == "leader" else role)}'
        raise ValueError('bad_message')

    def _h_chat_send(self, env):
        staff = self._staff_or_reject(env)
        if not staff:
            return
        p = P.ChatSendPayload(**env.payload)
        if p.kind not in ('quick', 'text', 'voice') or (p.kind == 'voice' and not (p.voice_url or '').startswith('/voice/')) \
                or (p.kind != 'voice' and not (p.text or '').strip()):
            return self._reject(env, 'bad_message')
        if p.kind == 'voice' and (p.duration_s or 0) > self.opt('chat', 'voice_max_s') + 2:
            return self._reject(env, 'voice_too_long')
        try:
            ch, note = self._resolve_channel(staff, p.channel)
        except ValueError as e:
            return self._reject(env, str(e))
        why = self.chat.allow(staff, ts(self._now().isoformat()), self.opt('chat', 'rate_max'),
                              self.opt('chat', 'rate_window_s'), self.opt('chat', 'min_gap_s'))
        if why:
            return self._reject(env, why)
        rcpt = [s for s in self._members(ch) if s != staff]
        m = self.chat.add(ch, staff, p.kind, (p.text or '').strip() or None, p.voice_url, p.duration_s, note,
                          self._now().isoformat(), rcpt)
        echo = dict(self._chat_payload(m, staff), client_id=p.client_id)
        self._to_watch(staff, P.CHAT_MSG, echo)              # 回给发送人:"已发出"
        for s in rcpt:
            self._deliver_chat(m, s)

    def _deliver_chat(self, m, staff, force=False):
        if not self.watch_of(staff):
            return                                           # 没戴表:留在收件箱,戴上后补发
        if not force and self._ringing(staff):
            self.chat.mark_queued(m['id'], staff, self._now().isoformat())   # 客户呼叫优先:先排队
            return
        self._to_watch(staff, P.CHAT_MSG, self._chat_payload(m, staff))
        self.chat.mark_sent(m['id'], staff, self._now().isoformat())

    def _flush_chat_queue(self, now):
        hold = self.opt('chat', 'hold_max_s')
        for q in self.chat.queued():
            late = (now - datetime.fromisoformat(q['queued_at'])).total_seconds() >= hold
            if late or not self._ringing(q['staff']):
                m = self.chat.get(q['msg_id'])
                if m:
                    self._deliver_chat(m, q['staff'], force=True)

    def _h_chat_ack(self, env):
        staff = self.staff_of_watch(env.device_id)
        ids = env.payload.get('ids') or []
        if staff and isinstance(ids, list):
            self.chat.delivered([str(i) for i in ids], staff, self._now().isoformat())

    def chat_contacts(self, staff):
        """手表"找人"页:可用频道、在班戴表的人(同楼层优先,其次最近联系过)、可按岗位呼叫的岗位、快捷回复。"""
        floors = sorted(self._my_floors(staff))
        channels = [dict(id=f'floor:{f}', name=f'{f}楼群') for f in floors]
        if self._nurse_like(staff):
            channels.append(dict(id='dept', name='护理部群'))
        if self._can_broadcast(staff):
            channels.append(dict(id='all', name='全公司群发'))
        recent = {s: i for i, (s, _) in enumerate(self.chat.recent_partners(staff))}
        me = self._staff_floor(staff)
        people = [s for s in self.cfg.staff if s != staff and self._on_duty(s)]
        people.sort(key=lambda s: (0 if me and self._staff_floor(s) == me else 1, recent.get(s, 99), self.cfg.name(s)))
        call_roles = self.opt('chat', 'call_roles') or [r for r in self.cfg.roles if r != 'nurse']
        roles = []
        if self.roster.leaders and 'leader' not in self.cfg.roles:
            roles.append(dict(id='leader', name='护士长',
                              online=sum(1 for s in set(self.roster.leaders.values()) if self._on_duty(s))))
        roles += [dict(id=r, name=self.cfg.roles.get(r, r),
                       online=sum(1 for s in self.cfg.staff if self.cfg.role_of(s) == r and self._on_duty(s)))
                  for r in call_roles if r in self.cfg.roles]
        return dict(channels=channels,
                    people=[dict(staff=s, name=self.cfg.name(s), floor=self._staff_floor(s),
                                 role=self.cfg.roles.get(self.cfg.role_of(s), ''), busy=self.roster.state(s) == 'busy')
                            for s in people],
                    roles=roles, quick_replies=self.opt('chat', 'quick_replies'),
                    voice_max_s=self.opt('chat', 'voice_max_s'), can_broadcast=self._can_broadcast(staff))

    def chat_history(self, staff, channel, limit=30):
        """某个频道的最近消息。不是成员时抛 ValueError。全公司群发人人都能看(只是不一定能发)。"""
        ch = 'all' if channel == 'all' else self._resolve_channel(staff, channel)[0]
        if channel.startswith('role:'):
            raise ValueError('bad_message')
        return [self._chat_payload(m, staff) for m in self.chat.history([ch], limit)]

    def chat_inbox(self, staff, limit=40):
        """手表"消息"首页:与我有关的所有频道的最近消息(含全公司群发)。"""
        chans = [c['id'] for c in self.chat_contacts(staff)['channels']] + ['all']
        chans += [r[0] for r in self.chat._run("SELECT DISTINCT channel FROM chat_messages WHERE channel LIKE ? OR channel LIKE ?",
                                               (f'dm:{staff}|%', f'dm:%|{staff}'))]
        return [self._chat_payload(m, staff) for m in self.chat.history(sorted(set(chans)), limit)]

    # ---- 手表绑定 / 换表 / 回绑 / 归还 ---------------------------------
    def watch_settings(self):
        w = {k: self.opt('watch', k) for k in ('scan_on_s', 'scan_period_s', 'heartbeat_s', 'heart_rate_interval_s', 'help_hold_ms', 'wifi_lock', 'cpu_lock')}
        w.update(quick_replies=self.opt('chat', 'quick_replies'), voice_max_s=self.opt('chat', 'voice_max_s'))
        return w

    def _send_bind(self, watch, reason=None):
        staff = self.staff_of_watch(watch)
        r = self.watches.get(watch) or {}
        info = dict(staff=staff, reason=reason, status=r.get('status'), spare=bool(r.get('spare')),
                    settings=self.watch_settings())
        if staff:
            info.update(name=self.cfg.name(staff), role=self.cfg.roles.get(self.cfg.role_of(staff), ''),
                        floor=self._staff_floor(staff),
                        rooms=[self.cfg.rooms.get(x, {}).get('room_no') or x for x in self._served_rooms(staff)],
                        home=not self.watches.is_wearing_spare(staff), can_broadcast=self._can_broadcast(staff))
        self.bus.publish(P.t_reply(self.cfg.store_id, watch), self._msg(P.WATCH_BIND, info))

    def _send_reset(self, watch):
        self.bus.publish(P.t_reply(self.cfg.store_id, watch), self._msg(P.WATCH_RESET, dict(sn=watch)))

    def _watch_brief(self, sn):
        r = self.watches.get(sn) or {}
        d = self._device(sn)
        return dict(sn=sn, status=r.get('status'), status_text=STATUS_TEXT.get(r.get('status'), r.get('status')),
                    battery=d.get('battery'), charging=bool(d.get('charging')), online=bool(d.get('online')) and d.get('last_seen', '') >= (self._now() - OFFLINE_AFTER).isoformat(),
                    last_seen=d.get('last_seen'), offline_since=d.get('offline_since'), wifi_rssi=d.get('wifi_rssi'), spare=bool(r.get('spare')),
                    bound_staff=r.get('bound_staff'),
                    bound_name=self.cfg.name(r['bound_staff']) if r.get('bound_staff') else None,
                    home_staff=r.get('home_staff'),
                    home_name=self.cfg.name(r['home_staff']) if r.get('home_staff') else None,
                    last_reset_time=r.get('last_reset_time'), note=r.get('note'), mac=r.get('mac'))

    def _ready_problems(self, sn, need_status=('IDLE',)):
        """借出/回绑前检查一块表;返回不通过的原因列表(空 = 可以用)。"""
        r = self.watches.get(sn)
        if not r:
            return [f'没有编号为 {sn} 的手表档案,请核对表身二维码']
        b = self._watch_brief(sn)
        out = []
        if r['status'] not in need_status:
            out.append(f'这块表当前是“{b["status_text"]}”,不能使用')
        if r.get('bound_staff'):
            out.append(f'这块表已绑定给 {b["bound_name"]}')
        if not b['online']:
            out.append('手表离线,请确认已开机并连上 Wi-Fi 后重试')
        mb = self.opt('swap', 'min_battery')
        if b['battery'] is None:
            out.append('还没收到这块表的电量,请开机联网后稍等 30 秒')
        elif b['battery'] < mb:
            out.append(f'电量 {b["battery"]}%,低于 {mb}%,请先充电')
        if b['wifi_rssi'] is not None and b['wifi_rssi'] < self.opt('swap', 'min_rssi'):
            out.append(f'Wi-Fi 信号太弱({b["wifi_rssi"]} dBm),请靠近 AP 或检查网络')
        return out

    def staff_card(self, staff):
        if staff not in self.cfg.staff:
            raise WatchError('找不到这位员工')
        info = self.cfg.staff[staff]
        cur, home = self.watch_of(staff), self.watches.home_watch(staff)
        with self.core._db() as db:
            own = db.execute("SELECT COUNT(*) FROM calls WHERE owner=? AND store=? AND state IN ('claimed','arrived')",
                             (staff, self.cfg.store_id)).fetchone()[0]
        ringing = len(self._ringing(staff, customer_only=False))
        h = self._open_help(staff)
        rooms = self._served_rooms(staff)
        return dict(staff=staff, name=self.cfg.name(staff), employee_no=info.get('employee_no') or staff,
                    role=self.cfg.roles.get(self.cfg.role_of(staff), ''), on_duty=info.get('on_duty', True),
                    shift=shift_of(self._now())[0], status=self.roster.state(staff),
                    watch=self._watch_brief(cur) if cur else None, home_watch=self._watch_brief(home) if home else None,
                    wearing_spare=self.watches.is_wearing_spare(staff),
                    rooms=[dict(room=r, room_no=self.cfg.rooms.get(r, {}).get('room_no') or r) for r in rooms],
                    floors=sorted(self._home_floors(staff)), open_calls=own, ringing=ringing, help_open=1 if h else 0,
                    open_events=own + ringing + (1 if h else 0), unreachable=staff in self.unreachable)

    def find_staff(self, q):
        q = (q or '').strip()
        if not q:
            return []
        out = []
        for s, info in self.cfg.staff.items():
            if q in (s, str(info.get('employee_no') or '')) or q in (info.get('name') or ''):
                out.append(dict(staff=s, name=info.get('name', s), employee_no=info.get('employee_no') or s,
                                watch=self.watch_of(s)))
        return sorted(out, key=lambda x: (x['staff'] != q and x['employee_no'] != q, x['name']))[:20]

    def spare_pool(self):
        mb = self.opt('swap', 'min_battery')
        out = []
        for r in self.watches.all():
            if not r.get('spare'):
                continue
            b = self._watch_brief(r['sn'])
            why = self._ready_problems(r['sn'])
            b.update(available=not why, reason='' if not why else why[0], low=b['battery'] is not None and b['battery'] < mb)
            out.append(b)
        return out

    def swap_check(self, staff, sn):
        card = self.staff_card(staff)
        sn = (sn or '').strip()
        if card['watch'] and card['watch']['sn'] == sn:
            return dict(ok=True, already=True, problems=[], card=card, new=card['watch'])
        problems = self._ready_problems(sn)
        return dict(ok=not problems, already=False, problems=problems, card=card,
                    new=self._watch_brief(sn) if self.watches.get(sn) else None)

    def _after_switch(self, staff, old, new, old_reason):
        self.unreachable.pop(staff, None)
        if old:
            self.bus.publish(P.t_reply(self.cfg.store_id, old),
                             self._msg(P.WATCH_BIND, dict(staff=None, reason=old_reason, settings=self.watch_settings())))
        self._send_bind(new)
        if self._fresh_online(new):
            self._set_live_status(staff)
        else:
            self.roster.set_status(staff, 'offline')
        self._resync(staff)                       # 进行中的呼叫/求助/未读消息全部推到新表上
        self._dispatch_new()
        self._flush()

    def swap(self, staff, sn, old_state='CHARGING', operator='station', operator_name='护士站', source='NURSE_STATION'):
        """护士站换表:员工身份为入口,不需要扫原表。校验不通过抛 WatchError(中文原因)。重复提交幂等。"""
        if old_state not in ('CHARGING', 'FAULT'):
            raise WatchError('原表状态只能是“待充电”或“故障”')
        chk = self.swap_check(staff, sn)
        if chk['already']:
            return dict(ok=True, already=True, staff=staff, watch=sn, message='这块表已经绑定给该员工,无需重复操作')
        if not chk['ok']:
            raise WatchError(';'.join(chk['problems']))
        card = chk['card']
        detail = dict(rooms=[r['room_no'] for r in card['rooms']], open_events=card['open_events'],
                      old_battery=card['watch']['battery'] if card['watch'] else None,
                      new_battery=chk['new']['battery'], old_state=old_state)
        old = self.watches.switch(staff, sn, old_state, operator, operator_name, self._now().isoformat(), source,
                                  detail=detail)
        self._after_switch(staff, old, sn, 'swapped_out')
        return dict(ok=True, already=False, staff=staff, name=card['name'], old_watch=old, watch=sn,
                    rooms=detail['rooms'], open_events=card['open_events'],
                    message=f'换表成功:{card["name"]} 现在佩戴 {sn},可上岗')

    def rollback_check(self, staff):
        card = self.staff_card(staff)
        home = card['home_watch']
        if not home:
            return dict(ok=False, problems=['这位员工没有登记自己的手表'], card=card)
        if not card['wearing_spare']:
            return dict(ok=False, problems=['已经戴着自己的表,不需要切回'], card=card)
        problems = self._ready_problems(home['sn'], need_status=('IDLE', 'CHARGING'))
        return dict(ok=not problems, problems=problems, card=card)

    def rollback(self, staff, operator='station', operator_name='护士站', source='NURSE_STATION'):
        """切回原表:原表重新绑定该员工;备用表解绑、进入清空中,清空完成后回到备用池。"""
        chk = self.rollback_check(staff)
        if not chk['ok']:
            raise WatchError(';'.join(chk['problems']))
        card = chk['card']
        spare, home = card['watch']['sn'], card['home_watch']['sn']
        detail = dict(rooms=[r['room_no'] for r in card['rooms']], open_events=card['open_events'], spare=spare)
        self.watches.switch(staff, home, 'RESETTING', operator, operator_name, self._now().isoformat(), source,
                            action_in='ROLLBACK', action_out='RELEASE', detail=detail)
        self._after_switch(staff, spare, home, 'returned')
        self._send_reset(spare)
        return dict(ok=True, staff=staff, name=card['name'], watch=home, spare=spare, open_events=card['open_events'],
                    message=f'已切回原表 {home};备用表 {spare} 正在清空,完成后回到备用池')

    def release(self, sn, operator='station', operator_name='护士站', source='NURSE_STATION'):
        """备用表直接收回(如交班不再借出):解绑并清空。"""
        r = self.watches.get(sn)
        if not r:
            raise WatchError(f'没有编号为 {sn} 的手表档案')
        if not r.get('spare'):
            raise WatchError('这不是备用表;员工自己的表请用“切回原表”或在后台调整状态')
        staff = r.get('bound_staff')
        self.watches.set_status(sn, 'RESETTING', self._now().isoformat(), operator, operator_name, source,
                                action='RELEASE', detail=dict(staff=staff))
        if staff:
            self.roster.set_status(staff, 'offline')
        self._send_reset(sn)
        return dict(ok=True, sn=sn, staff=staff, message=f'备用表 {sn} 已收回,正在清空')

    def set_watch_status(self, sn, status, note=None, operator='admin', operator_name='管理员'):
        """后台手动调整:故障标记、修好后放回备用池等。不能用它把表绑定给人(请走换表)。"""
        r = self.watches.get(sn)
        if not r:
            raise WatchError(f'没有编号为 {sn} 的手表档案')
        if status == 'BORROWED':
            raise WatchError('绑定员工请用“换表”')
        staff = r.get('bound_staff')
        self.watches.set_status(sn, status, self._now().isoformat(), operator, operator_name, 'ADMIN', note=note)
        if staff:
            self.roster.set_status(staff, 'offline')
            self.bus.publish(P.t_reply(self.cfg.store_id, sn),
                             self._msg(P.WATCH_BIND, dict(staff=None, reason='admin', settings=self.watch_settings())))
        if status == 'RESETTING':
            self._send_reset(sn)
        return self._watch_brief(sn)

    def _h_reset_done(self, env):
        r = self.watches.get(env.device_id)
        if r and r['status'] == 'RESETTING':
            self.watches.set_status(env.device_id, 'IDLE', self._now().isoformat(), env.device_id, '手表', 'WATCH',
                                    action='RESET')

    def _h_return_home(self, env):
        """手表上的“一键归位”:当前戴的备用表、或充好电的原表,按哪块都行。"""
        dev = env.device_id
        staff = self.staff_of_watch(dev)
        r = self.watches.get(dev) or {}
        if not staff and r.get('home_staff') and self.watches.is_wearing_spare(r['home_staff']):
            staff = r['home_staff']                       # 原表上按的
        if not staff:
            return self._reject(env, 'not_bound')
        try:
            self.rollback(staff, operator=staff, operator_name=self.cfg.name(staff), source='WATCH_BUTTON')
        except WatchError as e:
            self.bus.publish(P.t_reply(self.cfg.store_id, dev),
                             self._msg(P.CMD_REJECTED, dict(reason='return_failed', ref=env.msg_id, type=env.type, text=str(e))))

    def watch_vitals(self, reg, online):
        out = dict(heart_rate=None, heart_rate_at=None, heart_rate_state='no_data',
                   blood_pressure=None, blood_pressure_at=None, blood_pressure_state='no_data', vitals_ttl_seconds=600)
        staff, watch = reg.get('bound_staff'), reg['sn']
        if not staff:
            return out
        interval = self.terminal_configs.get('watch', watch)['settings'].get('heart_rate_interval_s', self.opt('watch', 'heart_rate_interval_s'))
        heart_ttl = max(90, min(600, int(interval) * 2))
        out['heart_rate_ttl_seconds'], out['blood_pressure_ttl_seconds'] = heart_ttl, 600
        for kind in ('heart_rate', 'blood_pressure'):
            ttl = heart_ttl if kind == 'heart_rate' else 600
            sample = self.tele.latest_vital_sample(staff, watch, kind, reg.get('last_bind_time'))
            if not sample:
                continue
            observed = sample.get('measured_at') or sample['at']
            try:
                sample_age = (self._now() - datetime.fromisoformat(observed)).total_seconds()
                receipt_age = (self._now() - datetime.fromisoformat(sample['at'])).total_seconds()
                fresh = -30 <= sample_age <= ttl and -30 <= receipt_age <= ttl
            except (ValueError, TypeError):
                fresh = False
            state = 'offline' if not online else 'fresh' if fresh else 'stale'
            out[kind + '_at'], out[kind + '_state'] = observed, state
            if state == 'fresh':
                out[kind] = sample['heart_rate'] if kind == 'heart_rate' else dict(systolic=sample['systolic'], diastolic=sample['diastolic'])
        return out

    def watch_board(self):
        """Merged station cards: registered equipment, current wearer, fresh location and actual vitals."""
        locations = {v['watch']: v for v in self.location_views()}
        out = []
        for r in self.watches.all():
            b = self._watch_brief(r['sn'])
            loc = locations.get(r['sn'], {})
            b['online'] = bool(loc.get('online'))
            b['unreachable'] = bool(r.get('bound_staff') and r['bound_staff'] in self.unreachable)
            b.update({key: loc.get(key) for key in ('location_status', 'location_reason', 'scan_status')})
            b.update(location_area=loc.get('area'), location_room=loc.get('room'), location_floor=loc.get('floor'),
                     located_at=loc.get('at'), location_ttl_seconds=loc.get('stale_after_seconds', 30))
            b.update(self.watch_vitals(r, b['online']))
            b['active'] = [dict(call_id=i, room=room, state=state) for i, room, state in self._active_calls(r['bound_staff'])] if r.get('bound_staff') else []
            out.append(b)
        return out

    def watch_problems(self):
        """主机提醒:收不到派单的手表、可借出备用表不足、清空卡住的备用表。"""
        out = []
        if self.unreachable:
            names = '、'.join(self.cfg.name(s) for s in self.unreachable)
            out.append(f'{names} 的手表连续收不到派单(没有回执),呼叫已转给其他人,请检查这块表')
        pool = self.spare_pool()
        if any(p['last_seen'] for p in pool):       # 备用表从没开过机(备用池还没投入使用)时不提醒
            ok = sum(1 for p in pool if p['available'])
            need = self.opt('swap', 'min_available')
            if ok < need:
                low = sum(1 for p in pool if p['low'])
                out.append(f'可借出的备用表只剩 {ok} 块(共 {len(pool)} 块' + (f',{low} 块电量不足' if low else '') + '),请给备用表充电')
        return out

    def announce_app_update(self, app, version_code, version_name='', notes=''):
        """新 APK 发布后立即通知对应设备去检查更新(设备上弹出“发现新版本”)。返回通知的设备数。"""
        if app == 'tablet':
            devs = [r.get('tablet') for r in self.cfg.rooms.values() if r.get('tablet')]
        elif app == 'watch':
            devs = [r['sn'] for r in self.watches.all()]
        else:
            devs = []
        payload = dict(app=app, version_code=version_code, version_name=version_name, notes=notes)
        for d in dict.fromkeys(devs):
            self.bus.publish(P.t_reply(self.cfg.store_id, d), self._msg(P.APP_UPDATE, payload))
        return len(set(devs))

    # ---- 平板专属按钮 / 路由 -----------------------------------------
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

    # ---- 心跳 / 在线 / 定位 / 体征 ------------------------------------
    def _h_heartbeat(self, env):
        # Master-data sync can introduce a watch between periodic registry refreshes.
        if not self.watches.get(env.device_id) and (env.device_id in self.cfg.spare_watches or
                any(x.get('watch') == env.device_id for x in self.cfg.staff.values())):
            self.watches.sync_from_config(self.cfg.staff, self.cfg.spare_watches, self._now().isoformat())
        self._terminal_heartbeat(env)
        p = P.HeartbeatPayload(**env.payload)
        now = self._now().isoformat()
        if self.cfg.room_of_tablet(env.device_id) and self.cfg.request_types \
                and p.cfg_ver != self.tablet_config(env.device_id)['ver']:
            self._send_config(env.device_id)        # 按钮列表变了或平板还没有,30 秒内自动更新
        self.tele.heartbeat(env.device_id, now, p.battery, p.wifi_rssi, p.charging)
        self.delivery.battery(env.device_id, now, p.battery, p.charging, self.opt('watch', 'battery_log_minutes'))
        if p.caps is not None:
            self.caps[env.device_id] = set(p.caps)
        reg = self.watches.get(env.device_id)
        if reg:
            if reg['status'] == 'RESETTING':
                self._send_reset(env.device_id)        # 归还时手表关着机:开机后补发清空指令
            elif 'bind' in self.caps.get(env.device_id, ()) and (p.staff or None) != reg.get('bound_staff'):
                self._send_bind(env.device_id)         # 手表以为的绑定和主机不一致:以主机为准
        staff = self.staff_of_watch(env.device_id)
        if staff:
            was_off = self.roster.state(staff) == 'offline'
            self._set_live_status(staff)
            if was_off and staff not in self.unreachable:
                self._resync(staff)
            self._dispatch_new()

    def _h_status(self, env):
        online = bool(env.payload.get('online'))
        self.tele.set_online(env.device_id, online, self._now().isoformat())
        if online:
            self.push_terminal_config(env.device_id)
        if online and self.cfg.room_of_tablet(env.device_id) and self.cfg.request_types:
            self._send_config(env.device_id)
        reg = self.watches.get(env.device_id)
        if online and reg:
            if reg['status'] == 'RESETTING':
                self._send_reset(env.device_id)
            else:
                self._send_bind(env.device_id)
        staff = self.staff_of_watch(env.device_id)
        if staff and not online:
            self.roster.set_status(staff, 'offline')
        if staff and online:
            self.unreachable.pop(staff, None)          # 重新连上:给它一次机会,收不到会再次判定
            self._set_live_status(staff)
            self._resync(staff)
            self._dispatch_new()

    def _h_location(self, env):
        if not self.watches.get(env.device_id):
            return self._reject(env, 'unknown_device')
        staff = self.staff_of_watch(env.device_id)
        p = P.LocationPayload(**env.payload)
        if 'staff' in p.model_fields_set and p.staff != staff:
            return self._reject(env, 'binding_mismatch')
        saved = self.terminal_configs.beacons()['mapping']
        self.locator.beacons = saved if saved is not None else self.cfg.beacons
        interval = p.report_interval_s or 10
        readings = [r.model_dump() for r in p.readings if r.age_ms is None or r.age_ms <= interval * 3000]
        scan_ok = p.scan_status in (None, 'ok')
        simulated = p.observation_source == 'simulated'
        if not staff or not scan_ok or simulated:
            self.locator.clear(env.device_id)
            fix = None
        else:
            fix = self.locator.update(env.device_id, readings)
        if not staff:
            reason = 'unbound'
        elif simulated:
            reason = 'simulated'
        elif not scan_ok:
            reason = 'scan_error'
        elif not p.readings:
            reason = 'empty_window'
        elif not readings:
            reason = 'samples_expired'
        elif not any(r['beacon_id'] in self.locator.beacons for r in readings):
            reason = 'unmapped'
        else:
            reason = 'below_threshold' if not fix else None
        diag = p.model_dump(mode='json')
        diag['reason'], diag['report_interval_s'] = reason, interval
        diag['fix_age_ms'] = next((r.get('age_ms') or 0 for r in readings if fix and r['beacon_id'] == fix['beacon']), 0)
        self.location_reports.record(env.device_id, staff, self.watches.get(env.device_id).get('last_bind_time'),
                                     fix, diag, self._now().isoformat())
        if staff:
            self.tele.add_location(env.device_id, staff, fix, self._now().isoformat())
        if fix and fix['room']:
            for call_id, room, state in self._active_calls(staff):
                if state == 'claimed' and room == fix['room']:
                    self._arrive(call_id, staff, 'ble')

    def _h_vitals(self, env):
        staff = self._staff_or_reject(env)
        if staff:
            v = P.VitalsPayload(**env.payload)
            if 'staff' in v.model_fields_set and v.staff != staff:
                return self._reject(env, 'binding_mismatch')
            self.tele.add_vitals(staff, env.device_id, self._now().isoformat(), v.model_dump(mode='json'))

    # ---- 测试报表 ------------------------------------------------------
    def test_report(self, hours=12):
        """续航与送达测试用:每块手表的电量曲线与每小时耗电、派单送达耗时、求助与消息统计。"""
        now = self._now()
        since = (now - timedelta(hours=max(1, min(hours, 72)))).isoformat()
        bat = {}
        for r in self.delivery.battery_series(since):
            bat.setdefault(r['device'], []).append(r)
        watches = []
        for dev, rows in sorted(bat.items()):
            if not self.watches.get(dev):
                continue
            pts = [(datetime.fromisoformat(r['at']), r['battery'], r['charging']) for r in rows]
            drops = []                      # 每小时耗电(只算没在充电的时段)
            for (t0, b0, c0), (t1, b1, c1) in zip(pts, pts[1:]):
                h = (t1 - t0).total_seconds() / 3600
                if h > 0 and not c0 and not c1 and b1 <= b0:
                    drops.append(((b0 - b1) / h, t0, t1))
            worst = max(drops, key=lambda x: x[0]) if drops else None
            watches.append(dict(watch=dev, staff=self.staff_of_watch(dev),
                                name=self.cfg.name(self.staff_of_watch(dev)) if self.staff_of_watch(dev) else None,
                                first=dict(at=rows[0]['at'], battery=rows[0]['battery']),
                                last=dict(at=rows[-1]['at'], battery=rows[-1]['battery']),
                                per_hour=round(sum(d[0] for d in drops) / len(drops), 1) if drops else None,
                                worst=dict(per_hour=round(worst[0], 1), start=worst[1].isoformat(), end=worst[2].isoformat()) if worst else None,
                                points=[dict(at=r['at'], battery=r['battery'], charging=r['charging']) for r in rows]))
        dl = self.delivery.stats(since)
        lat = sorted(d['latency_ms'] for d in dl if d['latency_ms'] is not None)
        pct = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else None
        by_kind = {}
        for d in dl:
            k = by_kind.setdefault(d['kind'] or 'call', dict(sent=0, received=0, over_2s=0, failed=0, resent=0))
            k['sent'] += 1
            k['received'] += d['received_at'] is not None
            k['over_2s'] += (d['latency_ms'] or 0) > 2000
            k['failed'] += bool(d['failed'])
            k['resent'] += d['sends'] > 1
        with self.core._db() as db:
            fb = db.execute("SELECT c.id, MIN(e1.at) acc, MAX(e0.at) cre FROM calls c JOIN events e0 ON e0.call_id=c.id "
                            "AND e0.version=1 JOIN events e1 ON e1.call_id=c.id AND e1.kind='call.claim' "
                            "WHERE e0.at>=? GROUP BY c.id", (since,)).fetchall()
        claim = sorted(int((datetime.fromisoformat(r['acc']) - datetime.fromisoformat(r['cre'])).total_seconds())
                       for r in fb)
        return dict(since=since, now=now.isoformat(), watches=watches,
                    delivery=dict(count=len(lat), p50_ms=pct(0.5), p95_ms=pct(0.95), max_ms=lat[-1] if lat else None,
                                  by_kind=by_kind),
                    claim_seconds=dict(count=len(claim), median=claim[len(claim) // 2] if claim else None),
                    chat={r['kind']: r['n'] for r in self.chat.stats(since)})


def c_room(call):
    return call['req']['room'] if 'req' in call else json.loads(call['request'])['room']
