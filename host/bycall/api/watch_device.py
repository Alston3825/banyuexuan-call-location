"""腕表 HTTP 员工数据使用当前绑定令牌；换出立即失效，不携带护士站密钥。"""
import hmac
import io

from fastapi import Header, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from ..core.voice import MAX_BYTES


def install(app, svc, voice, staff_history, app_updates=None):
    def update_device(sn, token):
        with svc.core._db() as db:
            w = db.execute('SELECT update_token FROM watch_inventory WHERE store=? AND sn=?', (svc.cfg.store_id, sn)).fetchone()
        if not w or not token or not hmac.compare_digest(token, w['update_token'] or ''):
            raise HTTPException(401, 'invalid device update token')
        if not app_updates:
            raise HTTPException(503, 'app updates not enabled')

    @app.get('/watch-device/{sn}/update')
    def update_info(sn: str, x_watch_update: str = Header(default='')):
        update_device(sn, x_watch_update)
        info = app_updates.info('watch')
        return dict(available=bool(info), **(info or {}))

    @app.get('/watch-device/{sn}/download')
    def update_download(sn: str, x_watch_update: str = Header(default='')):
        update_device(sn, x_watch_update)
        path = app_updates.path('watch')
        if not path:
            raise HTTPException(404, 'no apk published')
        return FileResponse(path, media_type='application/vnd.android.package-archive', filename='watch.apk')

    def staff_for(sn, token):
        staff = svc.cfg.staff_of_watch(sn)
        if not staff or not svc.cfg.staff[staff].get('on_duty', True):
            raise HTTPException(401, '当前手表未绑定在班员工')
        binding = svc.watches.context(staff)['binding']
        if not token or not hmac.compare_digest(token, binding['bind_id']):
            raise HTTPException(401, '绑定会话已失效')
        return staff

    @app.get('/watch-device/{sn}/history')
    def history(sn: str, days: int = 30, limit: int = 100, x_watch_binding: str = Header(default='')):
        return staff_history(staff_for(sn, x_watch_binding), max(1, min(days, 30)), max(1, min(limit, 100)))

    def duration(data):
        try:
            import av
        except ImportError:
            raise HTTPException(503, '语音时长校验组件未安装')
        try:
            with av.open(io.BytesIO(data)) as container:
                streams = list(container.streams.audio)
                if not streams:
                    raise ValueError('no audio')
                # 实际解码累计采样，不能相信客户端或容器头声明的时长。
                seconds = 0
                for frame in container.decode(audio=0):
                    seconds += frame.samples / frame.sample_rate
                    if seconds > svc.cfg.watch_policy['voice_max_seconds'] + 0.1:
                        raise ValueError('too long')
                if seconds <= 0:
                    raise ValueError('empty')
                return seconds
        except Exception:
            raise HTTPException(422, '语音无效或超过允许时长')

    @app.post('/watch-device/{sn}/voice')
    async def upload(sn: str, request: Request, ext: str = 'm4a', x_watch_binding: str = Header(default='')):
        staff = staff_for(sn, x_watch_binding)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise HTTPException(413, '语音文件过大')
        seconds = await run_in_threadpool(duration, bytes(data))
        # 解码期间可能发生换表，写入前再次校验。
        with svc.lock:
            staff_for(sn, x_watch_binding)
            try:
                url = '/voice/' + voice.save(bytes(data), ext)
            except ValueError as e:
                raise HTTPException(422, str(e))
            with svc.core._db() as db:
                db.execute('INSERT INTO watch_voice VALUES(?,?,?,?,?,?)', (svc.cfg.store_id, url, staff, sn, x_watch_binding, seconds))
        return dict(voice_url=url, seconds=seconds)

    @app.get('/watch-device/{sn}/voice/{name}')
    def download(sn: str, name: str, x_watch_binding: str = Header(default='')):
        staff = staff_for(sn, x_watch_binding)
        url = '/voice/' + name
        with svc.core._db() as db:
            own = db.execute('SELECT 1 FROM watch_voice WHERE store=? AND staff=? AND url=?', (svc.cfg.store_id, staff, url)).fetchone()
            delivered = db.execute("SELECT 1 FROM watch_delivery WHERE store=? AND staff=? AND json_extract(payload,'$.voice_url')=?", (svc.cfg.store_id, staff, url)).fetchone()
        if not own and not delivered:
            raise HTTPException(403, '无权读取这条语音')
        path = voice.path(name)
        if not path:
            raise HTTPException(404, 'voice not found')
        return FileResponse(path)
