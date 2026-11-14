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
