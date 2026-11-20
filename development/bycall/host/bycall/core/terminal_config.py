"""Versioned, durable terminal configuration. No credentials or identity changes over this channel."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import closing
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


class ConfigConflict(ConfigError):
    pass


def field(label, type_, default=None, low=None, high=None, section='运行参数'):
    return dict(label=label, type=type_, default=default, min=low, max=high, section=section)


CONNECTION = {
    'mqtt_host': field('MQTT 主机地址', 'host', section='连接设置'),
    'mqtt_port': field('MQTT 端口', 'int', low=1, high=65535, section='连接设置'),
    'api_base': field('主机接口地址', 'url', section='连接设置'),
}
SCHEMAS = {
    'watch': {
        **CONNECTION,
        'heartbeat_s': field('心跳间隔（秒）', 'int', 30, 10, 30),
        'report_seconds': field('位置上报间隔（秒）', 'int', 10, 7, 60, '蓝牙信标'),
        'beacon_uuid': field('本店信标 UUID（空值接受所有 UUID）', 'uuid', '', section='蓝牙信标'),
        'scan_on_s': field('每次扫描时长（秒）', 'int', 2, 1, 60, '蓝牙信标'),
        'scan_period_s': field('扫描周期（秒）', 'int', 10, 7, 60, '蓝牙信标'),
        'wifi_lock': field('熄屏保持 Wi-Fi', 'bool', True),
        'cpu_lock': field('熄屏保持 CPU 唤醒', 'bool', True),
        'help_hold_ms': field('求助长按时间（毫秒）', 'int', 1000, 500, 5000),
        'voice_max_s': field('同事语音最长时长（秒）', 'int', 30, 5, 30),
        'quick_replies': field('快捷回复（每行一条）', 'lines', ['收到', '马上到', '需要帮忙']),
    },
    'tablet': {
        **CONNECTION,
        'room_label': field('房间显示名称（不改变房间绑定）', 'text'),
        'heartbeat_s': field('心跳间隔（秒）', 'int', 30, 10, 30),
        'voice_enabled': field('显示语音留言按钮', 'bool', True),
    },
    'station': {
        'title': field('大屏标题', 'text', '伴月轩智能呼叫定位系统'),
        'sound_enabled': field('启用声音提醒（首次仍需点击页面授权）', 'bool', True),
        'recent_minutes': field('显示最近已结束呼叫（分钟）', 'int', 15, 1, 120),
        'late_seconds': field('未接单标红时间（秒）', 'int', 30, 5, 300),
        'watch_slots': field('手表显示位置（至少40）', 'int', 40, 40, 120),
        'scale_percent': field('界面字号比例（%）', 'int', 100, 80, 140),
    },
}


def validate(kind, patch, current=None):
    if kind not in SCHEMAS or not isinstance(patch, dict) or not patch:
        raise ConfigError('请选择终端类型并填写参数')
    out = {}
    for key, value in patch.items():
        f = SCHEMAS[kind].get(key)
        if not f:
            raise ConfigError('不支持的参数: ' + str(key))
        t = f['type']
        if t == 'bool':
            if type(value) is not bool:
                raise ConfigError(f['label'] + '必须为开关值')
        elif t == 'int':
            if type(value) is not int or not f['min'] <= value <= f['max']:
                raise ConfigError(f"{f['label']}范围为 {f['min']}–{f['max']}")
        elif t == 'lines':
            if not isinstance(value, list) or not 1 <= len(value) <= 12 or any(not isinstance(x, str) or not x.strip() or len(x) > 30 for x in value):
                raise ConfigError('快捷回复需1–12条，每条1–30字')
            value = [x.strip() for x in value]
        else:
            if not isinstance(value, str) or len(value) > 160 or any(ord(c) < 32 for c in value):
                raise ConfigError('参数文本无效: ' + key)
            value = value.strip()
            if t == 'text' and not value:
                raise ConfigError(f['label'] + '不能为空')
            if t == 'uuid' and value:
                if not re.fullmatch(r'[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value):
                    raise ConfigError('UUID 格式无效')
                value = value.replace('-', '').lower()
            if t == 'host' and (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', value)):
                raise ConfigError('请填写主机IP或域名，不含协议或端口')
            if t == 'url':
                u = urlsplit(value)
                try:
                    port = u.port
                except ValueError:
                    raise ConfigError('接口端口无效')
                if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('', '/'):
                    raise ConfigError('接口地址需为 http(s)://主机:端口，不含密码或路径')
                if port is not None and not 1 <= port <= 65535:
                    raise ConfigError('接口端口无效')
                value = value.rstrip('/')
        out[key] = value
    merged = {k: v['default'] for k, v in SCHEMAS[kind].items()}
    merged.update(current or {})
    merged.update(out)
    if kind == 'watch' and merged['scan_on_s'] >= merged['scan_period_s']:
        raise ConfigError('扫描时长必须小于扫描周期')
    return out


class TerminalConfigs:
    def __init__(self, path, store):
        self.path, self.store = str(path), store
        self.changed = threading.Condition()
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS terminal_revisions(id INTEGER PRIMARY KEY AUTOINCREMENT, store TEXT, at REAL, kind TEXT, targets TEXT, patch TEXT);
            CREATE TABLE IF NOT EXISTS terminal_settings(store TEXT, kind TEXT, device TEXT, revision INTEGER, settings TEXT, ack_revision INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', error TEXT DEFAULT '', seen REAL, capability INTEGER DEFAULT 0, PRIMARY KEY(store,kind,device));
            CREATE TABLE IF NOT EXISTS terminal_beacons(store TEXT PRIMARY KEY, revision INTEGER, mapping TEXT);
            ''')

    def db(self):
        return closing(sqlite3.connect(self.path, timeout=10))

    def get(self, kind, device):
        with self.db() as db:
            db.row_factory = sqlite3.Row
            r = db.execute('SELECT * FROM terminal_settings WHERE store=? AND kind=? AND device=?', (self.store, kind, device)).fetchone()
        if not r:
            return dict(kind=kind, device=device, revision=0, settings={}, ack_revision=0, status='unconfigured', error='', seen=None, capability=False)
        r = dict(r)
        r['settings'] = json.loads(r['settings'])
        return r

    def seen(self, kind, device, capable=False):
        with self.db() as db:
            db.execute("INSERT INTO terminal_settings(store,kind,device,revision,settings,seen,capability,status) VALUES(?,?,?,0,'{}',?,?,'unconfigured') ON CONFLICT(store,kind,device) DO UPDATE SET seen=excluded.seen,capability=excluded.capability", (self.store, kind, device, time.time(), int(capable)))
            db.commit()

    def station_ids(self):
        with self.db() as db:
            return [r[0] for r in db.execute("SELECT device FROM terminal_settings WHERE store=? AND kind='station'", (self.store,))]

    def save(self, kind, targets, patch, expected):
        if not isinstance(targets, list) or not targets or len(targets) > 300 or len(set(targets)) != len(targets):
            raise ConfigError('请选择1–300台终端')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            updates = []
            for dev in targets:
                row = db.execute('SELECT revision,settings FROM terminal_settings WHERE store=? AND kind=? AND device=?', (self.store, kind, dev)).fetchone()
                rev, current = (row[0], json.loads(row[1])) if row else (0, {})
                if not isinstance(expected, dict) or expected.get(dev) != rev:
                    raise ConfigConflict('配置已被其他管理员修改，请刷新后重试')
                updates.append((dev, {**current, **validate(kind, patch, current)}))
            cur = db.execute('INSERT INTO terminal_revisions(store,at,kind,targets,patch) VALUES(?,?,?,?,?)', (self.store, time.time(), kind, json.dumps(targets), json.dumps(patch, ensure_ascii=False)))
            rev = cur.lastrowid
            for dev, settings in updates:
                db.execute("INSERT INTO terminal_settings(store,kind,device,revision,settings,status) VALUES(?,?,?,?,?,'pending') ON CONFLICT(store,kind,device) DO UPDATE SET revision=excluded.revision,settings=excluded.settings,status='pending',error=''", (self.store, kind, dev, rev, json.dumps(settings, ensure_ascii=False)))
            db.commit()
        with self.changed:
            self.changed.notify_all()
        return rev

    def ack(self, kind, device, revision, status, error=''):
        if type(revision) is not int or status not in ('applied', 'failed', 'applying'):
            raise ConfigError('配置回执无效')
        # Never accept a stale or future acknowledgement as the current desired state.
        with self.db() as db:
            cur = db.execute('UPDATE terminal_settings SET ack_revision=?,status=?,error=? WHERE store=? AND kind=? AND device=? AND revision=? AND revision>0', (revision, status, str(error)[:160], self.store, kind, device, revision))
            db.commit()
            return bool(cur.rowcount)

    def beacons(self):
        with self.db() as db:
            r = db.execute('SELECT revision,mapping FROM terminal_beacons WHERE store=?', (self.store,)).fetchone()
        return dict(revision=r[0], mapping=json.loads(r[1])) if r else dict(revision=0, mapping=None)

    def save_beacons(self, items, rooms, expected):
        if not isinstance(items, list) or len(items) > 500:
            raise ConfigError('信标列表无效（最多500个）')
        mapping = {}
        for x in items:
            if not isinstance(x, dict):
                raise ConfigError('信标记录格式无效')
            major, minor, room = x.get('major'), x.get('minor'), x.get('room')
            if type(major) is not int or type(minor) is not int or not 0 <= major <= 65535 or not 0 <= minor <= 65535 or (room is not None and (not isinstance(room, str) or room not in rooms)):
                raise ConfigError('Major/Minor应为0–65535，房间必须已登记')
            key = f'{major}-{minor}'
            if key in mapping:
                raise ConfigError('信标编号重复: ' + key)
            floor = rooms[room].get('floor') if room is not None else x.get('floor')
            if floor is None or not isinstance(floor, str) or not floor.strip() or len(floor) > 20:
                raise ConfigError('公共区域信标需填写楼层')
            area = x.get('area') or room or '公共区域'
            if not isinstance(area, str) or len(area) > 80:
                raise ConfigError('区域名称无效')
            mapping[key] = dict(room=room, area=area, floor=floor)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT revision FROM terminal_beacons WHERE store=?', (self.store,)).fetchone()
            revision = row[0] if row else 0
            if expected != revision:
                raise ConfigConflict('信标表已被修改，请刷新')
            db.execute('INSERT OR REPLACE INTO terminal_beacons VALUES(?,?,?)', (self.store, revision + 1, json.dumps(mapping, ensure_ascii=False)))
            db.commit()
        return dict(revision=revision + 1, mapping=mapping)
