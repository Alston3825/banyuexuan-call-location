"""Over-the-air updates for the tablet / watch APKs, served from the host over the store LAN.

The developer publishes a new APK to the host (tools/publish_apk.py); every device asks the host
"is there a newer version?" and, if so, offers a one-tap update. Files live in <data>/apk/.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Optional

APPS = ('tablet', 'watch')
MAX_BYTES = 60 * 1024 * 1024


class UpdateError(Exception):
    pass


class AppUpdates:
    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)

    def _apk(self, app): return os.path.join(self.dir, f'{app}.apk')
    def _meta(self, app): return os.path.join(self.dir, f'{app}.json')

    @staticmethod
    def _check_app(app: str):
        if app not in APPS:
            raise UpdateError(f'未知应用 {app!r},只能是 {APPS}')

    def info(self, app: str) -> Optional[dict]:
        self._check_app(app)
        try:
            with open(self._meta(app)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def path(self, app: str) -> Optional[str]:
        return self._apk(app) if self.info(app) and os.path.exists(self._apk(app)) else None

    def publish(self, app: str, data: bytes, version_code: int, version_name: str = '', notes: str = '',
                force: bool = False) -> dict:
        self._check_app(app)
        if not data or len(data) > MAX_BYTES:
            raise UpdateError('安装包为空或超过 60 MB')
        if data[:2] != b'PK':
            raise UpdateError('这不是 APK 文件(应为 zip 格式)')
        if version_code <= 0:
            raise UpdateError('versionCode 必须为正整数')
        cur = self.info(app)
        if cur and version_code <= cur['versionCode'] and not force:
            raise UpdateError(f'versionCode {version_code} 不高于已发布的 {cur["versionCode"]}(设备只升不降;确需覆盖请加 force)')
        meta = dict(app=app, versionCode=version_code, versionName=version_name[:40], notes=notes[:300],
                    sha256=hashlib.sha256(data).hexdigest(), size=len(data), published_at=int(time.time()))
        tmp = self._apk(app) + '.part'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, self._apk(app))               # APK first, then metadata: a device never sees metadata without its file
        with open(self._meta(app) + '.tmp', 'w') as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(self._meta(app) + '.tmp', self._meta(app))
        return meta
