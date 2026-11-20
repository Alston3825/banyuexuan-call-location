"""Per-store configuration (dev plan §三 配置驱动). No store data lives in code."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class StoreConfig:
    store_id: str
    rooms: Dict[str, dict]                 # room -> {floor, tablet}
    beacons: Dict[str, dict]               # beacon_id -> {area, room}
    staff: Dict[str, dict]                 # staff_id -> {name, watch}
    groups: Dict[str, List[str]]
    assignments: Dict[str, Dict[str, str]]  # room -> {day, night}
    leaders: Dict[str, str]
    request_types: Dict[str, str] = field(default_factory=dict)  # code -> label
    roles: Dict[str, str] = field(default_factory=lambda: {'nurse': '护理师'})  # 岗位 id -> 名称;staff 的 role 默认 nurse
    group_after: int = 15                  # confirmed 2026-11-10 (review after pilot)
    leader_after: int = 30
    voice_retention_days: int = 30
    # ---- 2026-11-17 新增(都可不写,用下面的默认值) ----
    spare_watches: List[str] = field(default_factory=list)   # 备用池里的手表编号(=表身二维码里的 SN)
    watch: Dict[str, object] = field(default_factory=dict)   # 手表参数,见 WATCH_DEFAULTS
    help: Dict[str, object] = field(default_factory=dict)    # 一键求助规则,见 HELP_DEFAULTS
    chat: Dict[str, object] = field(default_factory=dict)    # 同事沟通规则,见 CHAT_DEFAULTS
    swap: Dict[str, object] = field(default_factory=dict)    # 换表规则,见 SWAP_DEFAULTS

    @classmethod
    def load(cls, path) -> 'StoreConfig':
        with open(path, encoding='utf-8') as f:
            return cls(**json.load(f))

    def watch_of(self, staff: str) -> Optional[str]:
        return self.staff.get(staff, {}).get('watch')

    def staff_of_watch(self, watch: str) -> Optional[str]:
        return next((s for s, v in self.staff.items() if v.get('watch') == watch), None)

    def room_of_tablet(self, dev: str) -> Optional[str]:
        return next((r for r, v in self.rooms.items() if v.get('tablet') == dev), None)

    def role_of(self, staff: str) -> str:
        return self.staff.get(staff, {}).get('role', 'nurse')

    def name(self, staff: str) -> str:
        return self.staff.get(staff, {}).get('name', staff)

    def opt(self, section: str, key: str):
        """读可配置项:门店配置里写了就用配置的,否则用默认值。"""
        return {**DEFAULTS[section], **(getattr(self, section) or {})}[key]


# 手表行为参数:主机随 WATCH_BIND 推给手表,改配置后手表重连即生效
WATCH_DEFAULTS = dict(
    scan_on_s=2, scan_period_s=10,       # 蓝牙信标间歇扫描:每 10 秒扫 2 秒(安卓限制 30 秒内最多启动 5 次,周期不能小于 7 秒)
    heart_rate_interval_s=60,            # 默认每分钟采样一次，可远程改30–300秒
    heartbeat_s=30,                      # 在线心跳(很小的包);电量随心跳上报,主机按 battery_log_minutes 记一次
    battery_log_minutes=15,
    help_hold_ms=1000,                   # 求助按钮需按住多久才发出
    wifi_lock=True,                      # 熄屏时保持 Wi-Fi 不休眠(否则廉价手表熄屏后可能延迟收呼叫);费电,可关
    cpu_lock=True,                       # 熄屏时保持 CPU 唤醒(否则发不出保活心跳,息屏后很快掉线);费电,可关
    receipt_resend_s=5, receipt_max_sends=4,   # 派单 5 秒没收到手表回执就重发,最多发 4 次;仍无回执视为该表收不到,转派给其他人
)
HELP_DEFAULTS = dict(
    first_count=2,                       # 第一轮通知同组里离得最近且空闲的几个人
    floor_after=30,                      # 30 秒无人接单 → 扩大到整层
    leader_after=60,                     # 再过 30 秒仍无人接单 → 通知护士长(组长)和护士站
)
CHAT_DEFAULTS = dict(
    quick_replies=['收到', '马上到', '需要帮忙', '请稍等', '已处理'],
    voice_max_s=30,
    rate_max=6, rate_window_s=60, min_gap_s=2,   # 同一人 60 秒内最多 6 条,两条之间至少隔 2 秒
    hold_max_s=90,                       # 收件人正在响客户呼叫时消息先排队,最多压 90 秒
    broadcast_roles=['leader', 'supervisor'],    # 可以"全公司群发"的岗位(leader = 各组组长/护士长)
    call_roles=[],                       # 手表上"按岗位呼叫"列出的岗位 id;空 = 列出除护理师外的全部岗位
)
SWAP_DEFAULTS = dict(
    min_battery=80,                      # 备用表借出/原表回绑的最低电量
    min_rssi=-80,                        # 手表上报的 Wi-Fi 信号低于此值不让借出
    min_available=1,                     # 可借出的备用表少于这个数时,大屏提醒
)
DEFAULTS = dict(watch=WATCH_DEFAULTS, help=HELP_DEFAULTS, chat=CHAT_DEFAULTS, swap=SWAP_DEFAULTS)
