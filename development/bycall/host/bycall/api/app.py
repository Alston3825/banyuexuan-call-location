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
from ..core.watches import WatchError
from ..core.routing import RoutingError
from ..core.transcribe import TranscribeUnavailable
from ..version import VERSION
from ..core.voice import VoiceStore


def create_app(svc, voice: VoiceStore, api_key: Optional[str] = None, host_status=None,
               backup_mgr=None, restart=None, app_updates=None, transcriber=None,
               swap_key: Optional[str] = None, twin_key: Optional[str] = None) -> FastAPI:
    app = FastAPI(title='伴月轩智能呼叫定位系统 主机 API', version='0.1')
    store = svc.cfg.store_id

    def auth(x_api_key: Optional[str] = Header(default=None)):
        if api_key and not (x_api_key and hmac.compare_digest(x_api_key, api_key)):
            raise HTTPException(401, 'invalid api key')

    def swap_auth(x_api_key: Optional[str] = Header(default=None)):
        """换表接口:主密钥或"换表管理员"密钥都可以;换表密钥只能用 /swap/* 这些接口
        (看班次、看备用池、换表、回绑、收回、看日志),不能改员工资料、不能删日志。"""
        if not api_key:
            return 'admin'
        if x_api_key and hmac.compare_digest(x_api_key, api_key):
            return 'admin'
        if swap_key and x_api_key and hmac.compare_digest(x_api_key, swap_key):
            return 'swap'
        raise HTTPException(401, 'invalid api key')

    from .terminal_admin import install as install_terminal_admin
    def terminal_auth(x_api_key: Optional[str] = Header(default=None)):
        if not api_key:
            raise HTTPException(503, '请先为主机配置管理员密钥，再使用远程设置')
        return auth(x_api_key)

    install_terminal_admin(app, svc, terminal_auth)

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
            meta = _upd().publish(app, await request.body(), version_code, version_name, notes, force)
        except UpdateError as e:
            raise HTTPException(422, str(e))
        try:
            n = svc.announce_app_update(app, version_code, version_name, notes)   # 立即通知在线设备,不用等它们 10 分钟一次的检查
        except Exception:
            n = 0
        return dict(meta, notified=n) if isinstance(meta, dict) else meta

    # ---- 主机程序更新(大屏“设置”里一键升级;实际由更新器容器完成换版)----------------
    UPDATER = os.environ.get('UPDATER_URL', 'http://updater:8200')

    def _updater(method, path):
        import json as _json
        import urllib.error
        import urllib.request
        req = urllib.request.Request(UPDATER + path, method=method, data=b'' if method == 'POST' else None,
                                     headers={'X-Api-Key': api_key or ''})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return _json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            try:
                detail = _json.loads(e.read()).get('detail')
            except Exception:
                detail = None
            raise HTTPException(e.code, detail or f'更新器返回 {e.code}')
        except OSError:
            raise HTTPException(503, '连不上主机更新器(updater 容器没在运行?)')

    @app.get('/system/update', dependencies=[Depends(auth)])
    def system_update():
        from ..version import VERSION
        try:
            st = _updater('GET', '/api/status')
        except HTTPException as e:
            return dict(version=VERSION, updater=False, error=e.detail)
        return dict(version=VERSION, updater=True, pending=st.get('pending'), job=st.get('job'),
                    models_installed=st.get('models_installed'))

    @app.post('/system/update/apply', dependencies=[Depends(auth)])
    def system_update_apply():
        return _updater('POST', '/api/apply')

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
            v.update(label=snap.get('label') or snap.get('service_code'), text=snap.get('text'), kind=snap.get('kind', 'call'),
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
        loc = {l['watch']: l for l in svc.location_views()}
        nurses = []
        for sid, info in cfg.staff.items():
            w = svc.watch_of(sid)                     # 当前戴的表(换表后是备用表)
            d, l = dev.get(w, {}), loc.get(w, {})
            reg = svc.watches.get(w) if w else None
            vital = svc.watch_vitals(reg, bool(d.get('online'))) if reg else {}
            nurses.append(dict(staff=sid, name=info.get('name', sid), watch=w,
                               online=bool(d.get('online')), battery=d.get('battery'),
                               last_seen=d.get('last_seen'), status=svc.roster.state(sid),
                               area=l.get('area'), location_room=l.get('room'), located_at=l.get('at'),
                               location_status=l.get('location_status', 'unknown'), location_reason=l.get('location_reason'),
                               heart_rate=vital.get('heart_rate'), blood_pressure=vital.get('blood_pressure'),
                               active=[dict(call_id=i, room=r, state=s) for i, r, s in svc._active_calls(sid)]))
        rooms = [dict(room=r, floor=v.get('floor'), room_no=v.get('room_no'),
                      guest_buttons=[g['label'] for g in svc.guest_buttons(r)], tablet=v.get('tablet'),
                      online=bool(dev.get(v.get('tablet'), {}).get('online')),
                      battery=dev.get(v.get('tablet'), {}).get('battery'),
                      last_seen=dev.get(v.get('tablet'), {}).get('last_seen'))
                 for r, v in cfg.rooms.items()]
        return dict(store_id=cfg.store_id, now=svc._now().isoformat(), active=active, recent=done[:recent],
                    nurses=nurses, rooms=rooms, watches=svc.watch_board(),
                    latency_ms=svc.latency[-20:],
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

    @app.get('/watches/{watch}/history', dependencies=[Depends(auth)])
    def watch_history(watch: str, days: int = 30, limit: int = 100):
        """手表"我的记录":这块手表的护理师接过的呼叫(最近 days 天,最新在前),含时间线、留言、转文字。"""
        staff = svc.staff_of_watch(watch)
        if not staff:
            raise HTTPException(404, 'watch not bound to a staff')
        return staff_history(staff, days, limit)

    # ---- 手表:同事沟通(列表走 HTTP,收发消息走 MQTT) -----------------------
    def _wearer(watch):
        staff = svc.staff_of_watch(watch)
        if not staff:
            raise HTTPException(404, 'watch not bound to a staff')
        return staff

    @app.get('/watches/{watch}/chat/contacts', dependencies=[Depends(auth)])
    def chat_contacts(watch: str):
        return svc.chat_contacts(_wearer(watch))

    @app.get('/watches/{watch}/chat/inbox', dependencies=[Depends(auth)])
    def chat_inbox(watch: str, limit: int = 40):
        return svc.chat_inbox(_wearer(watch), min(limit, 100))

    @app.get('/watches/{watch}/chat/history', dependencies=[Depends(auth)])
    def chat_history(watch: str, channel: str, limit: int = 30):
        try:
            return svc.chat_history(_wearer(watch), channel, min(limit, 100))
        except ValueError:
            raise HTTPException(403, '不在这个频道里')

    # ---- 换表 / 回绑 / 归还(护士站换表页 /swap) ---------------------------
    def _swap_err(e):
        return HTTPException(422, str(e))

    @app.get('/swap')
    def swap_page():
        return FileResponse(os.path.join(os.path.dirname(__file__), 'swap.html'), headers={'Cache-Control': 'no-store'})

    @app.get('/swap/whoami')
    def swap_whoami(role: str = Depends(swap_auth)):
        return dict(role=role, store_id=store)

    @app.get('/swap/staff', dependencies=[Depends(swap_auth)])
    def swap_find(q: str = ''):
        return svc.find_staff(q)

    @app.get('/swap/staff/{staff}', dependencies=[Depends(swap_auth)])
    def swap_card(staff: str):
        try:
            return svc.staff_card(staff)
        except WatchError as e:
            raise HTTPException(404, str(e))

    @app.get('/swap/pool', dependencies=[Depends(swap_auth)])
    def swap_pool():
        return dict(pool=svc.spare_pool(), problems=svc.watch_problems(),
                    min_battery=svc.opt('swap', 'min_battery'))

    @app.get('/swap/check', dependencies=[Depends(swap_auth)])
    def swap_check(staff: str, sn: str):
        try:
            return svc.swap_check(staff, sn)
        except WatchError as e:
            raise _swap_err(e)

    @app.post('/swap')
    def swap_do(body: dict, role: str = Depends(swap_auth)):
        try:
            return svc.swap(str(body.get('staff')), str(body.get('sn') or '').strip(), body.get('old_state') or 'CHARGING',
                            operator=role, operator_name=(body.get('operator_name') or '护士站')[:20])
        except WatchError as e:
            raise _swap_err(e)

    @app.get('/swap/rollback-check', dependencies=[Depends(swap_auth)])
    def rollback_check(staff: str):
        try:
            return svc.rollback_check(staff)
        except WatchError as e:
            raise _swap_err(e)

    @app.post('/swap/rollback')
    def rollback_do(body: dict, role: str = Depends(swap_auth)):
        try:
            return svc.rollback(str(body.get('staff')), operator=role, operator_name=(body.get('operator_name') or '护士站')[:20])
        except WatchError as e:
            raise _swap_err(e)

    @app.post('/swap/release')
    def release_do(body: dict, role: str = Depends(swap_auth)):
        try:
            return svc.release(str(body.get('sn') or '').strip(), operator=role,
                               operator_name=(body.get('operator_name') or '护士站')[:20])
        except WatchError as e:
            raise _swap_err(e)

    @app.get('/swap/log', dependencies=[Depends(swap_auth)])
    def swap_log(staff: Optional[str] = None, sn: Optional[str] = None, action: Optional[str] = None,
                 since: Optional[str] = None, until: Optional[str] = None, limit: int = 200):
        rows = svc.watches.oplog(staff, sn, action, since, until, min(limit, 1000))
        for r in rows:
            r['staff_name'] = svc.cfg.name(r['staff_id']) if r['staff_id'] else None
        return rows

    # ---- 后台:手表档案(只有主密钥能改) ---------------------------------------
    @app.get('/watches', dependencies=[Depends(swap_auth)])
    def watch_list():
        return svc.watch_board()

    @app.put('/watches/{sn}/status', dependencies=[Depends(auth)])
    def watch_status(sn: str, body: dict):
        """故障标记 / 修好放回备用池(IDLE) / 手动重置(RESETTING)。"""
        try:
            return svc.set_watch_status(sn, str(body.get('status')), body.get('note'), operator='admin',
                                        operator_name=(body.get('operator_name') or '管理员')[:20])
        except WatchError as e:
            raise _swap_err(e)

    @app.get('/watches/{sn}/bindings', dependencies=[Depends(swap_auth)])
    def watch_bindings(sn: str, limit: int = 100):
        return svc.watches.bindings(sn=sn, limit=min(limit, 1000))

    # ---- 续航与送达测试报表 ---------------------------------------------------
    @app.get('/report')
    def report_page():
        return FileResponse(os.path.join(os.path.dirname(__file__), 'report.html'), headers={'Cache-Control': 'no-store'})

    @app.get('/reports/watch-test', dependencies=[Depends(auth)])
    def watch_test(hours: int = 12):
        return svc.test_report(hours)

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
        return svc.location_views()

    def twin_auth(x_api_key: Optional[str] = Header(default=None)):
        if not twin_key or twin_key in (api_key, swap_key):
            raise HTTPException(503, '数字孪生只读接口尚未启用')
        if not x_api_key or not hmac.compare_digest(x_api_key, twin_key):
            raise HTTPException(401, 'invalid read-only key')

    @app.get('/integration/twin/locations', dependencies=[Depends(twin_auth)])
    def twin_locations():
        # Dedicated read-only projection: no names, vitals, voice, call text or admin credentials.
        items = []
        for v in svc.location_views():
            if v['staff'] is None:
                continue
            items.append({k: v[k] for k in ('watch', 'staff', 'online', 'location_status', 'location_reason',
                'room', 'area', 'floor', 'precision', 'coordinates', 'received_at', 'observed_at',
                'age_seconds', 'stale_after_seconds')})
        return dict(schema='bycall.location.v1', store_id=store, server_time=svc._now().isoformat(), items=items)

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
