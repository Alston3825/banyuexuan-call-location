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

    watches: Dict[str, dict] = field(default_factory=dict)  # 独立设备档案，含备用表 SN/MAC
    watch_policy: dict = field(default_factory=dict)

    def __post_init__(self):
        defaults = dict(borrow_battery=80, min_spares=2, min_wifi_rssi=-80,
                        offline_seconds=90, help_floor_seconds=30, help_leader_seconds=60,
                        voice_max_seconds=30, message_interval_seconds=3, retry_seconds=3,
                        battery_seconds=900, scan_interval_seconds=10, scan_window_seconds=2,
                        quick_replies=['收到', '马上到', '需要帮忙'],
                        broadcast_roles=['HEAD_NURSE', 'NURSE_MANAGER'])
        defaults.update(self.watch_policy)
        for key in ('borrow_battery', 'min_spares', 'offline_seconds', 'help_floor_seconds',
                    'help_leader_seconds', 'voice_max_seconds', 'message_interval_seconds',
                    'retry_seconds', 'battery_seconds', 'scan_interval_seconds', 'scan_window_seconds'):
            if not isinstance(defaults[key], (int, float)) or defaults[key] <= 0:
                raise ValueError('invalid watch policy: ' + key)
        if defaults['borrow_battery'] > 100 or defaults['scan_window_seconds'] > defaults['scan_interval_seconds'] or defaults['help_leader_seconds'] < defaults['help_floor_seconds']:
            raise ValueError('invalid watch policy range')
        if not -127 <= defaults['min_wifi_rssi'] <= 0:
            raise ValueError('invalid WiFi signal threshold')
        watch_ids = list(self.watches) + [v.get('watch') for v in self.staff.values() if v.get('watch')]
        if any(not isinstance(sn, str) or any(c in sn for c in '/+#\n\r') for sn in watch_ids):
            raise ValueError('invalid watch SN for MQTT topic')
        assigned = [v['watch'] for v in self.staff.values() if v.get('watch')]
        if len(assigned) != len(set(assigned)):
            raise ValueError('watch assigned to multiple staff')
        self.watch_policy = defaults

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
