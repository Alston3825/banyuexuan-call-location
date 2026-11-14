"""最小的 Docker Engine API 客户端(走 /var/run/docker.sock,只用标准库,不需要 docker 命令)。"""
from __future__ import annotations

import http.client
import json
import os
import socket

API = '/v1.41'


class DockerError(Exception):
    pass


class _Conn(http.client.HTTPConnection):
    def __init__(self, sock_path, timeout):
        super().__init__('localhost', timeout=timeout)
        self._sock_path = sock_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


class DockerAPI:
    def __init__(self, sock_path='/var/run/docker.sock'):
        self.sock_path = sock_path

    def call(self, method, path, body=None, headers=None, timeout=60, ok=(200, 201, 204)):
        c = _Conn(self.sock_path, timeout)
        try:
            h = dict(headers or {})
            data = None
            if isinstance(body, (dict, list)):
                data = json.dumps(body).encode()
                h['Content-Type'] = 'application/json'
            else:
                data = body
            c.request(method, API + path, body=data, headers=h)
            r = c.getresponse()
            raw = r.read()
        except OSError as e:
            raise DockerError(f'连不上 Docker({e})') from e
        finally:
            c.close()
        if r.status not in ok:
            try:
                msg = json.loads(raw).get('message', raw.decode(errors='replace'))
            except Exception:
                msg = raw.decode(errors='replace')
            raise DockerError(f'{method} {path} -> {r.status}: {msg}')
        return raw

    def json(self, method, path, **kw):
        raw = self.call(method, path, **kw)
        return json.loads(raw) if raw else None

    def load_image(self, file_path):
        """载入 docker save 导出的镜像包(.tar 或 .tar.gz),返回载入的镜像名列表。"""
        size = os.path.getsize(file_path)
        with open(file_path, 'rb') as f:
            raw = self.call('POST', '/images/load?quiet=0', body=f, timeout=900,
                            headers={'Content-Type': 'application/x-tar', 'Content-Length': str(size)})
        loaded, err = [], None
        for line in raw.decode(errors='replace').splitlines():
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if 'error' in j:
                err = j['error']
            s = j.get('stream', '')
            if s.startswith('Loaded image:'):
                loaded.append(s.split(':', 1)[1].strip())
        if err:
            raise DockerError('载入镜像失败:' + err)
        return loaded
