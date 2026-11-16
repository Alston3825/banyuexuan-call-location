"""Local HTTP API (dev plan §五 api, §15.4 read interface for the management system)."""
from __future__ import annotations

import hmac
import json
import os
from datetime import timedelta
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from ..core.guest_buttons import GuestButtonError, GuestButtons
from ..core.routing import RoutingError
from ..core.transcribe import TranscribeUnavailable
from ..version import VERSION
from ..core.voice import VoiceStore


def create_app(svc, voice: VoiceStore, api_key: Optional[str] = None, host_status=None,
               backup_mgr=None, restart=None, app_updates=None, transcriber=None) -> FastAPI:
    app = FastAPI(title='伴月轩智能呼叫定位系统 主机 API', version='0.1')
    store = svc.cfg.store_id
    from .watch_admin import install
    install(app, svc)

    def auth(x_api_key: Optional[str] = Header(default=None)):
        if api_key and not (x_api_key and hmac.compare_digest(x_api_key, api_key)):
            raise HTTPException(401, 'invalid api key')

    def view(c):
        r = json.loads(c['request'])
        return dict(call_id=c['id'], store_id=c['store'], room=r['room'], priority=r['priority'],
                    request=r['snapshot'], state=c['state'], owner=c['owner'], version=c['version'])

    @app.get('/health')
    def health():
        return dict(ok=True, store_id=store, version=VERSION)

    @app.get('/calls', dependencies=[Depends(auth)])
    def calls(state: Optional[str] = None, limit: int = 100):
        return [view(c) for c in svc.core.list_calls(store=store, state=state, limit=min(limit, 500))]

    @app.get('/calls/{call_id}', dependencies=[Depends(auth)])
    def call(call_id: str):
        try:
            return view(svc.core.get(call_id, store=store))
        except KeyError:
            raise HTTPException(404, 'call not found')

    @app.get('/events', dependencies=[Depends(auth)])
    def events(after: int = 0):
        """Incremental pull for the management system; seq is the cursor."""
        return [dict(seq=e['seq'], call_id=e['call_id'], store_id=store, kind=e['kind'], at=e['at'],
                     version=e['version'], detail=json.loads(e['payload']))
                for e in svc.core.events(after=after)]

    @app.get('/station')
    def station_page():
        """护士站大屏页面本身不含数据;数据走 /station/snapshot(需 API Key)。"""
        return FileResponse(os.path.join(os.path.dirname(__file__), 'station.html'),
                            headers={'Cache-Control': 'no-store'})

    # ---- 备份管理(云备份 + 恢复) ------------------------------------
    @app.get('/backup')
    def backup_page():
        return FileResponse(os.path.join(os.path.dirname(__file__), 'backup.html'),
                            headers={'Cache-Control': 'no-store'})

    def _bm():
        if not backup_mgr:
            raise HTTPException(503, 'backup manager not enabled')
        return backup_mgr

    @app.get('/backup/status', dependencies=[Depends(auth)])
    def backup_status():
        return _bm().status()

    @app.get('/backup/list', dependencies=[Depends(auth)])
    def backup_list():
        return _bm().listing()

    @app.post('/backup/run', dependencies=[Depends(auth)])
    def backup_run():
        r = _bm().run()
        if not r['ok']:
            raise HTTPException(500, r['error'])
        return r

    @app.put('/backup/settings', dependencies=[Depends(auth)])
    async def backup_settings(request: Request):
        body = await request.json()
        try:
            return _bm().update_settings(**{k: v for k, v in body.items()
                                            if k in ('times', 'local_keep_days', 'cloud_keep_days', 'auto', 'cloud')})
        except (ValueError, TypeError) as e:
            raise HTTPException(422, str(e))

    @app.post('/backup/restore', dependencies=[Depends(auth)])
    async def backup_restore(request: Request):
        """校验备份 → 暂存 → 重启主机进程,启动时换入(旧库改名保留)。必须带 confirm=恢复。"""
        body = await request.json()
        if body.get('confirm') != '恢复':
            raise HTTPException(422, '需要确认:confirm 必须为"恢复"')
        from ..core.backup import BackupError
        try:
            _bm().stage_restore(str(body.get('source')), str(body.get('name')))
        except BackupError as e:
            raise HTTPException(422, str(e))
        if restart:
            restart()
        return dict(ok=True, message='备份已校验,主机即将重启并换入该备份(约 20 秒内恢复)')

    # ---- 平板/手表 App 在线更新 --------------------------------------
    def _upd():
        if not app_updates:
            raise HTTPException(503, 'app updates not enabled')
        return app_updates

    @app.get('/app/update', dependencies=[Depends(auth)])
    def app_update(app: str):
        """设备询问最新版本;没发布过则返回 available=false。"""
        from ..core.appupdate import UpdateError
        try:
            m = _upd().info(app)
        except UpdateError as e:
            raise HTTPException(422, str(e))
        return dict(available=bool(m), **(m or {}))

    @app.get('/app/download/{app}', dependencies=[Depends(auth)])
    def app_download(app: str):
        from ..core.appupdate import UpdateError
        try:
            p = _upd().path(app)
        except UpdateError as e:
            raise HTTPException(422, str(e))
        if not p:
            raise HTTPException(404, 'no apk published')
        return FileResponse(p, media_type='application/vnd.android.package-archive', filename=f'{app}.apk')

    @app.post('/app/publish/{app}', dependencies=[Depends(auth)])
    async def app_publish(app: str, request: Request, version_code: int, version_name: str = '',
                          notes: str = '', force: bool = False):
        """开发者发布新版本:请求体就是 APK 文件本身。"""
        from ..core.appupdate import UpdateError
        try:
            return _upd().publish(app, await request.body(), version_code, version_name, notes, force)
        except UpdateError as e:
            raise HTTPException(422, str(e))

    @app.get('/station/snapshot', dependencies=[Depends(auth)])
    def station_snapshot(recent: int = 15):
        """大屏一次拉取:进行中的呼叫、最近已结束、护理师(位置/在线/电量/忙闲)、客房平板在线。"""
        cfg = svc.cfg
        with svc.core._db() as db:
            created = {r['call_id']: r['at'] for r in db.execute(
                'SELECT call_id, at FROM events WHERE version=1')}
            ended = {r['call_id']: r['at'] for r in db.execute(
                "SELECT call_id, MAX(at) at FROM events WHERE kind IN ('call.complete','call.cancel') "
                'GROUP BY call_id')}
        calls_all = [c for st in ('pending', 'claimed', 'arrived', 'completed', 'cancelled')
                     for c in svc.core.list_calls(store=cfg.store_id, state=st, limit=200)]
        stt = transcriber.cached_many([(json.loads(c['request'])['snapshot'].get('voice_url') or '').rsplit('/', 1)[-1]
                                       for c in calls_all]) if transcriber else {}
        def row(c):
            v = view(c)
            snap = v['request']
            v.update(label=snap.get('label') or snap.get('service_code'), text=snap.get('text'),
                     voice_url=snap.get('voice_url'), floor=cfg.rooms.get(v['room'], {}).get('floor'),
                     transcript=stt.get((snap.get('voice_url') or '').rsplit('/', 1)[-1]),
                     owner_name=cfg.name(v['owner']) if v['owner'] else None,
                     created_at=created.get(v['call_id']), ended_at=ended.get(v['call_id']))
            v.pop('request')
            return v
        active = [row(c) for st in ('pending', 'claimed', 'arrived')
                  for c in svc.core.list_calls(store=cfg.store_id, state=st, limit=200)]
        active.sort(key=lambda c: c['created_at'] or '')
        done = [row(c) for st in ('completed', 'cancelled')
                for c in svc.core.list_calls(store=cfg.store_id, state=st, limit=recent)]
        done.sort(key=lambda c: c['ended_at'] or '', reverse=True)
        dev = {d['device']: d for d in svc.tele.devices()}
        loc = {l['watch']: l for l in svc.tele.latest_locations()}
        nurses = []
        for sid, info in cfg.staff.items():
            w = info.get('watch')
            d, l = dev.get(w, {}), loc.get(w, {})
            hr = svc.tele.vitals(sid, 1)
            nurses.append(dict(staff=sid, name=info.get('name', sid), watch=w,
                               online=bool(d.get('online')), battery=d.get('battery'),
                               last_seen=d.get('last_seen'), status=svc.roster.state(sid),
                               area=l.get('area'), location_room=l.get('room'), located_at=l.get('at'),
                               heart_rate=hr[0]['heart_rate'] if hr else None,
                               active=[dict(call_id=i, room=r, state=s) for i, r, s in svc._active_calls(sid)]))
        rooms = [dict(room=r, floor=v.get('floor'), room_no=v.get('room_no'),
                      guest_buttons=[g['label'] for g in svc.guest_buttons(r)], tablet=v.get('tablet'),
                      online=bool(dev.get(v.get('tablet'), {}).get('online')),
                      battery=dev.get(v.get('tablet'), {}).get('battery'),
                      last_seen=dev.get(v.get('tablet'), {}).get('last_seen'))
                 for r, v in cfg.rooms.items()]
        with svc.core._db() as db:
            helps = [dict(r) for r in db.execute("SELECT * FROM team_help WHERE store=? AND state IN ('pending','claimed') ORDER BY at", (store,))]
        for h in helps:
            h['name'] = cfg.name(h['sender'])
            h['owner_name'] = cfg.name(h['owner']) if h['owner'] else None
        with svc.core._db() as db:
            delivery_alerts = [dict(r) for r in db.execute("SELECT staff,ref,attempts FROM watch_delivery WHERE store=? AND kind='CALL_DISPATCH' AND received IS NULL AND attempts>=3 AND ref IN (SELECT id FROM calls WHERE state IN ('pending','claimed','arrived'))", (store,))]
        return dict(watches=svc.presence.snapshot(), watch_slots=max(40, len(svc.presence.known())), delivery_alerts=delivery_alerts, helps=helps, store_id=cfg.store_id, now=svc._now().isoformat(), active=active, recent=done[:recent],
                    nurses=nurses, rooms=rooms,
                    host=host_status.snapshot() if host_status else None, version=VERSION)

    @app.get('/rooms/{room}/buttons', dependencies=[Depends(auth)])
    def room_buttons(room: str):
        if room not in svc.cfg.rooms:
            raise HTTPException(404, 'room not found')
        return dict(room=room, stay_id=svc.cfg.rooms[room].get('stay_id'), max=GuestButtons.MAX,
                    buttons=svc.guest_buttons(room))

    @app.put('/rooms/{room}/buttons', dependencies=[Depends(auth)])
    def set_room_buttons(room: str, body: dict):
        """设置本房当前客户的专属按钮(最多 2 个,每个最多 8 字);传空列表即清除。"""
        try:
            out = svc.set_guest_buttons(room, body.get('labels'))
        except KeyError:
            raise HTTPException(404, 'room not found')
        except GuestButtonError as e:
            raise HTTPException(400, str(e))
        return dict(room=room, buttons=out)

    @app.get('/routing', dependencies=[Depends(auth)])
    def routing_get():
        return svc.routing_view()

    @app.put('/routing', dependencies=[Depends(auth)])
    def routing_put(body: dict):
        try:
            return svc.set_routing(body)
        except RoutingError as e:
            raise HTTPException(400, str(e))

    @app.get('/station/watch-activity', dependencies=[Depends(auth)])
    def watch_activity(start: str, end: str, watch: Optional[str] = None, staff: Optional[str] = None, after: int = 0, limit: int = 1000):
        from datetime import date, datetime, time, timezone
        from ..core.presence import TZ
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            if last < first or (last-first).days > 30 or after < 0 or not 1 <= limit <= 2000:
                raise ValueError('日期范围 1–31 天，limit 为 1–2000，after 不得为负')
        except ValueError as e:
            raise HTTPException(422, str(e))
        lo = datetime.combine(first, time(), TZ).astimezone(timezone.utc).isoformat()
        hi = datetime.combine(last + timedelta(days=1), time(), TZ).astimezone(timezone.utc).isoformat()
        sql = 'SELECT * FROM locations WHERE store=? AND at>=? AND at<? AND id>?'
        args = [store, lo, hi, after]
        if watch:
            sql += ' AND watch=?'; args.append(watch)
        if staff:
            sql += ' AND staff=?'; args.append(staff)
        with svc.core._db() as db:
            rows = [dict(r) for r in db.execute(sql+' ORDER BY id LIMIT ?', (*args, limit+1))]
        return dict(store_id=store, timezone='Asia/Shanghai', items=rows[:limit], has_more=len(rows)>limit, next_after=rows[:limit][-1]['id'] if rows else after,
                    note='仅为 BLE 推测区域采样，不能填补无观测时段，也不等于人工确认到场。')

    @app.get('/station/watch-presence', dependencies=[Depends(auth)])
    def watch_presence(start: str, end: str, watch: Optional[str] = None, staff: Optional[str] = None):
        from datetime import date
        try:
            return svc.presence.daily(date.fromisoformat(start), date.fromisoformat(end), watch, staff)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get('/devices', dependencies=[Depends(auth)])
    def devices():
        return svc.tele.devices()

    def history_items(db, rows):
        """把若干呼叫整理成"记录":房间、事项、状态、时间线、留言、转文字。"""
        ev = {r['id']: [dict(kind=e['kind'], at=e['at']) for e in db.execute(
            'SELECT kind, at FROM events WHERE call_id=? ORDER BY version', (r['id'],))] for r in rows}
        stt = transcriber.cached_many([(json.loads(r['request'])['snapshot'].get('voice_url') or '').rsplit('/', 1)[-1]
                                       for r in rows]) if transcriber else {}
        out = []
        for r in rows:
            v = view(r)
            snap = v['request']
            tl = ev[r['id']]
            end = next((e['at'] for e in reversed(tl) if e['kind'] in ('call.complete', 'call.cancel')), None)
            out.append(dict(call_id=v['call_id'], room=v['room'],
                            room_no=svc.cfg.rooms.get(v['room'], {}).get('room_no') or v['room'],
                            label=snap.get('label') or snap.get('service_code'), text=snap.get('text'),
                            emergency=v['priority'] == 'emergency', state=v['state'],
                            owner=v['owner'], owner_name=svc.cfg.name(v['owner']) if v['owner'] else None,
                            voice_url=snap.get('voice_url'),
                            transcript=stt.get((snap.get('voice_url') or '').rsplit('/', 1)[-1]),
                            created_at=tl[0]['at'] if tl else None, ended_at=end, timeline=tl))
        return out

    def staff_history(staff, days, limit):
        cutoff = (svc._now() - timedelta(days=max(1, min(days, 3650)))).isoformat()
        with svc.core._db() as db:
            rows = [dict(r) for r in db.execute(
                'SELECT c.* FROM calls c WHERE c.store=? AND c.owner=? AND EXISTS '
                '(SELECT 1 FROM events e WHERE e.call_id=c.id AND e.version=1 AND e.at>=?) '
                'ORDER BY c.rowid DESC LIMIT ?', (store, staff, cutoff, min(limit, 500)))]
            return history_items(db, rows)

    from .watch_device import install as install_watch_device
    install_watch_device(app, svc, voice, staff_history, app_updates)

    @app.get('/watches/{watch}/history', dependencies=[Depends(auth)])
    def watch_history(watch: str, days: int = 30, limit: int = 100):
        """手表"我的记录":这块手表的护理师接过的呼叫(最近 days 天,最新在前),含时间线、留言、转文字。"""
        staff = svc.cfg.staff_of_watch(watch)
        if not staff:
            raise HTTPException(404, 'watch not bound to a staff')
        return staff_history(staff, days, limit)

    @app.get('/station/call/{call_id}', dependencies=[Depends(auth)])
    def station_call(call_id: str):
        """大屏双击一条记录:单条呼叫的完整信息(时间线、留言、转文字)。"""
        try:
            c = svc.core.get(call_id, store=store)
        except KeyError:
            raise HTTPException(404, 'call not found')
        with svc.core._db() as db:
            return history_items(db, [c])[0]

    @app.get('/station/staff/{staff}/history', dependencies=[Depends(auth)])
    def station_staff_history(staff: str, limit: int = 300):
        """大屏双击护理师:该护理师今天和之前累积的全部接单记录(最新在前)。"""
        if staff not in svc.cfg.staff:
            raise HTTPException(404, 'staff not found')
        items = staff_history(staff, 3650, limit)
        return dict(staff=staff, name=svc.cfg.name(staff), items=items)

    @app.get('/nurses/locations', dependencies=[Depends(auth)])
    def locations():
        return svc.tele.latest_locations()

    @app.get('/nurses/{staff}/vitals', dependencies=[Depends(auth)])
    def vitals(staff: str, limit: int = 100):
        return svc.tele.vitals(staff, min(limit, 1000))

    @app.post('/voice', dependencies=[Depends(auth)])
    async def upload(request: Request, ext: str = 'wav'):
        try:
            name = voice.save(await request.body(), ext)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return dict(voice_url=f'/voice/{name}')

    @app.post('/voice/{name}/transcribe', dependencies=[Depends(auth)])
    def transcribe(name: str):
        """把一条留言转成文字(本机离线识别,结果缓存,同一条只识别一次)。"""
        p = voice.path(name)
        if not p:
            raise HTTPException(404, 'voice not found')
        if not transcriber:
            raise HTTPException(503, '转文字功能未启用')
        try:
            text, cached = transcriber.transcribe(name, p)
        except TranscribeUnavailable as e:
            raise HTTPException(503, str(e))
        except Exception as e:                      # 录音损坏等:不要让界面一直转圈
            raise HTTPException(500, f'识别失败:{e}')
        return dict(name=name, text=text, cached=cached)

    @app.get('/voice/{name}', dependencies=[Depends(auth)])
    def download(name: str):
        p = voice.path(name)
        if not p:
            raise HTTPException(404, 'not found')
        return FileResponse(p)

    return app
