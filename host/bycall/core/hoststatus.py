"""Host health shown on the nurse-station screen: backup freshness and disk space."""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from typing import List, Optional

from . import backup as B

STALE_HOURS = 36           # 每天一次备份,超过 36 小时没有新备份就告警
MIN_FREE_GB = 1.0
LOW_BATTERY = 20           # 手表电量低于 20% 提醒护士站
OFFLINE_AFTER_S = 180      # 平板/手表失联超过 3 分钟就提醒
MAX_NAMES = 5


class HostStatus:
    def __init__(self, data_dir: str, backup_dir: Optional[str], svc=None, offline_after_s: float = OFFLINE_AFTER_S):
        self.data_dir, self.backup_dir, self.svc, self.offline_after_s = data_dir, backup_dir, svc, offline_after_s
        self.bm = None                               # BackupManager(云备份状态)
        self.backup_error: Optional[str] = None      # 最近一次备份失败原因;成功后清空
        self.started = time.time()
        self._first_fail: dict = {}
        self._alerts: dict = {}

    def report(self, key: str, ok: bool, message: str = '', after_s: float = 0, now: Optional[float] = None):
        """登记某项检查的结果。连续失败超过 after_s 秒才告警(过滤偶发抖动);恢复后自动消除。"""
        now = now or time.time()
        if ok:
            self._first_fail.pop(key, None)
            self._alerts.pop(key, None)
            return
        t0 = self._first_fail.setdefault(key, now)
        if now - t0 >= after_s:
            self._alerts[key] = message

    def snapshot(self, now: Optional[float] = None) -> dict:
        now = now or time.time()
        problems: List[str] = []
        last = B.latest(self.backup_dir) if self.backup_dir else None
        last_ts = os.path.getmtime(last) if last else None
        if not self.backup_dir:
            problems.append('未配置数据库备份')
        elif self.backup_error:
            problems.append('数据库备份失败:' + self.backup_error)
        elif last_ts is None:
            if now - self.started > 600:             # 启动后 1 分钟内会自动补一份,留足余量
                problems.append('还没有数据库备份')
        elif now - last_ts > STALE_HOURS * 3600:
            problems.append(f'数据库已 {int((now - last_ts) // 3600)} 小时没有新备份')
        if self.bm:
            problems += self.bm.problems(now)
        problems += list(self._alerts.values())
        problems += self.device_problems(now)
        try:
            free = shutil.disk_usage(self.data_dir).free / 1e9
        except OSError:
            free = None
        if free is not None and free < MIN_FREE_GB:
            problems.append(f'主机磁盘剩余空间只有 {free:.1f} GB')
        return dict(backup_at=last_ts, backup_count=len(B.list_backups(self.backup_dir)) if self.backup_dir else 0,
                    backup_error=self.backup_error, disk_free_gb=None if free is None else round(free, 1),
                    uptime_s=int(now - self.started), problems=problems)

    def device_problems(self, now: float) -> List[str]:
        """房间平板 / 在岗护理师手表失联超过阈值:呼叫发不出去、或派了单没人收到。"""
        svc = self.svc
        if not svc:
            return []
        seen = {d['device']: d for d in svc.tele.devices()}

        def down_for(dev):
            """已失联多少秒;None = 在线。主机刚重启时从启动时刻算起,避免误报。"""
            d = seen.get(dev)
            if d and d['online'] and (now - datetime.fromisoformat(d['last_seen']).timestamp()) \
                    <= self.offline_after_s:
                return None
            last = datetime.fromisoformat(d['last_seen']).timestamp() if d else 0
            return now - max(last, self.started)

        def line(kind, what, names, total):
            more = f' 等{len(names)}个' if len(names) > MAX_NAMES else ''
            shown = '、'.join(names[:MAX_NAMES]) + more
            t = self.offline_after_s
            mins = f'{int(t // 60)}分钟' if t >= 60 else f'{int(t)}秒'
            if len(names) == total and total > 1:
                return f'全部{total}个{what}都已失联超过{mins}({shown}),请检查店内 Wi-Fi/路由器/主机网络'
            return f'{len(names)}个{what}失联超过{mins}:{shown}'

        out = []
        # 只看住了客人的房间(没有状态信息时视为都住人),空房间的平板不提醒
        tabs = [(r, v) for r, v in svc.cfg.rooms.items() if v.get('tablet')
                and v.get('status') in (None, 'OCCUPIED')]
        off = [v.get('room_no') or r for r, v in tabs
               if (down_for(v['tablet']) or 0) >= self.offline_after_s]
        if off:
            out.append(line('tablet', '房间平板', off, len(tabs)))
        nurses = [(s, v) for s, v in svc.cfg.staff.items() if v.get('watch') and v.get('on_duty', True)]
        offn = [v.get('name') or s for s, v in nurses if (down_for(v['watch']) or 0) >= self.offline_after_s]
        if offn:
            out.append(line('watch', '在岗护理师手表', offn, len(nurses)))
        low = [f"{v.get('name') or s}({seen[v['watch']]['battery']}%)" for s, v in nurses
               if v['watch'] in seen and down_for(v['watch']) is None
               and not seen[v['watch']].get('charging')
               and seen[v['watch']].get('battery') is not None and seen[v['watch']]['battery'] < LOW_BATTERY]
        if low:
            more = f' 等{len(low)}位' if len(low) > MAX_NAMES else ''
            out.append(f'{len(low)}位在岗护理师的手表电量低于{LOW_BATTERY}%,请让其尽快充电:' + '、'.join(low[:MAX_NAMES]) + more)
        return out
