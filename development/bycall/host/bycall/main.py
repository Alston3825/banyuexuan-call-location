"""Production entry: MQTT bus + service tick loop + HTTP API."""
import os
import threading
import time

import uvicorn

from .api.app import create_app
from .core.config import StoreConfig
from .core.service import HostService
from .core.transcribe import Transcriber
from .core.voice import VoiceStore
from .gateway.bus import MqttBus


def main():
    data = os.environ.get('BYCALL_DATA', '/data')
    cfg = StoreConfig.load(os.environ.get('BYCALL_CONFIG', '/app/bycall/custom/banyuexuan/demo_pilot.json'))
    mgmt_url = os.environ.get('MGMT_URL')
    if mgmt_url:                      # 对接管理系统:门店 ID 与主数据以管理系统为准
        cfg.store_id = os.environ['MGMT_STORE_ID']
        cfg.rooms, cfg.staff, cfg.groups, cfg.assignments, cfg.leaders = {}, {}, {}, {}, {}
    bus = MqttBus(os.environ.get('MQTT_HOST', 'mosquitto'), int(os.environ.get('MQTT_PORT', '1883')),
                  os.environ.get('MQTT_USER'), os.environ.get('MQTT_PASSWORD'))
    from .core import backupmgr, cloud
    from .core.appupdate import AppUpdates
    r = backupmgr.apply_pending(data, os.path.join(data, 'host.db'))     # 管理页触发的恢复,在打开数据库前换入
    if r:
        print('RESTORE APPLIED, old db kept as:' if r[0] else 'RESTORE FAILED (kept current db):', r[1])
    svc = HostService(cfg, bus, os.path.join(data, 'host.db'))
    voice = VoiceStore(os.path.join(data, 'voice'))
    from .core.hoststatus import HostStatus
    sync = wb = None
    bk_dir = os.environ.get('BYCALL_BACKUP_DIR', os.path.join(data, 'backups'))
    hstat = HostStatus(data, bk_dir, svc, float(os.environ.get('BYCALL_OFFLINE_AFTER_S', '180')))
    bm = backupmgr.BackupManager(data, os.path.join(data, 'host.db'), bk_dir, cloud.store_from_env(),
                                 os.environ.get('CLOUD_PASSPHRASE'))
    if not os.path.exists(os.path.join(data, 'backup-settings.json')):     # 首次:沿用环境变量里的本地保留天数
        bm.update_settings(local_keep_days=int(os.environ.get('BYCALL_BACKUP_KEEP_DAYS', '7')))
    hstat.bm = bm
    if mgmt_url:
        from .integration.master_sync import MasterSync
        from .integration.mgmt_client import MgmtClient, MgmtError
        from .integration.writeback import CallWriteback
        client = MgmtClient(mgmt_url, os.environ['MGMT_API_KEY'])
        sync = MasterSync(client, os.path.join(data, 'host.db'), cfg, svc.roster)
        sync.apply()                  # 先用上次缓存的主数据,管理系统不通也能启动
        wb = CallWriteback(client, svc.core, cfg)
    bus.start()

    def loop():
        n = 0
        while True:
            time.sleep(1)
            try:                      # 后台循环绝不能因为一次异常整个停掉
                svc.tick()
                hstat.report('loop', True)
            except Exception as e:
                hstat.report('loop', False, f'主机后台任务出错:{str(e)[:80]}')
                print('tick failed:', repr(e))
            n += 1
            hstat.report('mqtt', bus.connected(), '主机与 MQTT 消息服务断开,呼叫收发中断', after_s=15)
            if sync and n % 30 == 0:  # 每 30 秒增量同步,每 6 小时全量(发现删除)
                try:
                    sync.sync(full=(n % 21600 == 0))
                    hstat.report('sync', True)
                except MgmtError as e:
                    hstat.report('sync', False, '与管理系统同步失败,房间/人员/服务项数据可能不是最新的', after_s=120)
                    print('master sync failed:', e)
            if wb and n % 5 == 0:
                before = len(wb.errors)
                try:
                    wb.flush()
                except Exception as e:
                    wb.errors.append(('-', str(e)))
                bad = len(wb.errors) > before
                hstat.report('writeback', not bad, '呼叫记录回写管理系统失败,记录暂存在主机待重发', after_s=60)
                del wb.errors[:-50]
            if n % 3600 == 0:
                voice.purge(cfg.voice_retention_days)
            if n % 60 == 0:           # 每分钟检查备份时段(默认每天 4 次),失败只记录不影响呼叫
                try:
                    from .core import backup as B
                    if bm.settings['auto'] and not B.latest(bk_dir):
                        bm.run()
                    else:
                        bm.tick()
                    hstat.backup_error = bm.state['last_error']
                except Exception as e:
                    hstat.backup_error = str(e)[:120]
                    print('BACKUP FAILED:', e)
    threading.Thread(target=loop, daemon=True).start()
    def restart():                # 恢复需要重启进程;docker 的 restart: unless-stopped 会自动拉起
        threading.Timer(1.5, lambda: os._exit(0)).start()
    uvicorn.run(create_app(svc, voice, os.environ.get('BYCALL_API_KEY'), hstat, bm, restart,
                              AppUpdates(os.path.join(data, 'apk')), Transcriber(svc.core.path),
                              swap_key=os.environ.get('BYCALL_SWAP_KEY')), host='0.0.0.0', port=int(os.environ.get('BYCALL_PORT', '8000')))


if __name__ == '__main__':
    main()
