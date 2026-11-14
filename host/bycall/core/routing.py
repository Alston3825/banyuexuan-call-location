"""按钮 → 接收岗位 的对应表(主机后台可随时修改,存在 SQLite,改完立即生效)。

默认所有按钮(含客户自定义按钮)都发给护理师(nurse)。紧急呼叫固定发给护理师,不可改。
岗位来自门店配置 roles;员工的岗位在 staff[*].role(不写就是 nurse)。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

DEFAULT_ROLE = 'nurse'
CUSTOM = 'CUSTOM'     # 客户自定义按钮统一用这一项


class RoutingError(ValueError):
    pass


class ButtonRouting:
    def __init__(self, path, roles):
        self.path, self.roles = str(path), roles
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS button_routing(code TEXT PRIMARY KEY, role TEXT NOT NULL)')
            db.commit()

    def all(self):
        with closing(sqlite3.connect(self.path)) as db:
            return dict(db.execute('SELECT code, role FROM button_routing').fetchall())

    def role_of(self, code):
        """code 为按钮的服务码;GUEST_x 按 CUSTOM 处理。已被删除的岗位退回护理师,保证呼叫不丢。"""
        if code == 'EMERGENCY':
            return DEFAULT_ROLE
        if code.startswith('GUEST_'):
            code = CUSTOM
        role = self.all().get(code, DEFAULT_ROLE)
        return role if role in self.roles else DEFAULT_ROLE

    def set(self, mapping, valid_codes):
        if not isinstance(mapping, dict):
            raise RoutingError('格式不对')
        for code, role in mapping.items():
            if code not in valid_codes:
                raise RoutingError(f'未知按钮:{code}')
            if role not in self.roles:
                raise RoutingError(f'未知岗位:{role}')
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute('BEGIN IMMEDIATE')
            db.executemany('INSERT INTO button_routing VALUES(?,?) ON CONFLICT(code) DO UPDATE SET role=excluded.role',
                           list(mapping.items()))
            db.commit()
