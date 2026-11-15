"""专用换表管理员接口；凭据独立于普通设备 API key，未配置时禁用。"""
import hmac
import json
import os
from pathlib import Path

from fastapi import Header, HTTPException, Request
from fastapi.responses import FileResponse

from ..core.watchops import WatchError


def install(app, svc):
    # {"不透明随机令牌":"护士站操作员名称"}；部署时注入，不存入源码。
    accounts = json.loads(os.environ.get('BYCALL_WATCH_ADMINS', '{}'))

    def operator(key):
        for token, name in accounts.items():
            if key and hmac.compare_digest(key, token):
                return name
        raise HTTPException(401, '需要独立的换表管理员凭据')

    @app.get('/watch-admin')
    def page():
        return FileResponse(Path(__file__).with_name('watch_admin.html'))

    @app.get('/watch-admin/snapshot')
    def snapshot(x_watch_admin_key: str = Header(default='')):
        operator(x_watch_admin_key)
        inv = svc.watches.inventory()
        available = sum(w['eligible'] for w in inv)
        return dict(store_id=svc.cfg.store_id, staff=[svc.watches.context(s) for s in svc.cfg.staff],
                    inventory=inv, operations=svc.watches.rows('watch_operations'),
                    available=available, shortage=available < svc.cfg.watch_policy['min_spares'],
                    sync_status='LOCAL_PENDING', sync_message='本地主机可操作；跨楼层对端未配置，记录持久化待同步',
                    policy=svc.cfg.watch_policy)

    @app.get('/watch-admin/metrics')
    def metrics(x_watch_admin_key: str = Header(default='')):
        operator(x_watch_admin_key)
        with svc.core._db() as db:
            batteries = [dict(r) for r in db.execute('SELECT * FROM battery_samples WHERE store=? ORDER BY id DESC LIMIT 5000', (svc.cfg.store_id,))]
            receipts = [dict(r) for r in db.execute('SELECT r.* FROM watch_receipts r JOIN watch_delivery d ON r.delivery_id=d.id WHERE d.store=? ORDER BY r.id DESC LIMIT 5000', (svc.cfg.store_id,))]
        return dict(battery_samples=batteries, receipts=receipts)

    @app.post('/watch-admin/change')
    async def change(request: Request, x_watch_admin_key: str = Header(default='')):
        who = operator(x_watch_admin_key)
        b = await request.json()
        try:
            result = svc.watches.change(staff=b.get('staff'), target=b.get('target'), action=b.get('action', 'SWAP'),
                                        expected_version=b.get('version'), request_id=b.get('request_id'), operator=who,
                                        fault=b.get('fault') is True)
            return result
        except WatchError as e:
            raise HTTPException(409, str(e))

    @app.post('/watch-admin/fault')
    async def fault(request: Request, x_watch_admin_key: str = Header(default='')):
        who = operator(x_watch_admin_key)
        b = await request.json()
        try:
            svc.watches.mark_fault(b.get('sn'), b.get('note'), who)
            return dict(ok=True)
        except WatchError as e:
            raise HTTPException(409, str(e))

    @app.post('/watch-admin/reset')
    async def reset(request: Request, x_watch_admin_key: str = Header(default='')):
        who = operator(x_watch_admin_key)
        b = await request.json()
        try:
            svc.watches.reset(b.get('sn'), who)
            return dict(ok=True, message='已请求擦除，等待设备确认')
        except WatchError as e:
            raise HTTPException(409, str(e))
