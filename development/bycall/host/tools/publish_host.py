"""开发者在 Mac 上用:把主机代码打包(几百 KB)推送到主机的更新器,存为“待升级”。
之后在大屏“⚙ 设置”或 http://主机IP:8200 点“立即升级主机”即可换版(失败自动换回旧版)。
  python3 tools/publish_host.py --host 192.168.1.108 --key 密钥 [--notes "更新说明"]
版本号 = 打包时间(如 20261117-1530),写进包里的 bycall/VERSION(不改动本地文件)。"""
import argparse
import io
import os
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument('--host', required=True, help='主机 IP,如 192.168.1.108')
ap.add_argument('--key', required=True)
ap.add_argument('--notes', default='')
ap.add_argument('--out', default=None, help='同时把包另存到这里(可选)')
a = ap.parse_args()

HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
version = time.strftime('%Y%m%d-%H%M')
SKIP_DIRS = {'__pycache__', '.pytest_cache'}
SKIP_FILES = {'.DS_Store', 'VERSION'}


def add_tree(tf, rel):
    base = os.path.join(HOST_DIR, rel)
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith('.'))
        for f in sorted(files):
            if f in SKIP_FILES or f.startswith('._') or f.endswith(('.pyc', '.pyo')):
                continue
            full = os.path.join(root, f)
            if os.path.islink(full):
                continue
            arc = os.path.relpath(full, HOST_DIR).replace(os.sep, '/')
            ti = tf.gettarinfo(full, arc)
            ti.uid = ti.gid = 0; ti.uname = ti.gname = 'root'
            with open(full, 'rb') as fh:
                tf.addfile(ti, fh)


buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode='w:gz') as tf:
    add_tree(tf, 'bycall')
    add_tree(tf, 'tools')
    tf.add(os.path.join(HOST_DIR, 'requirements.txt'), 'requirements.txt')
    v = version.encode()
    ti = tarfile.TarInfo('bycall/VERSION'); ti.size = len(v); ti.mtime = int(time.time())
    tf.addfile(ti, io.BytesIO(v))
data = buf.getvalue()
if a.out:
    with open(a.out, 'wb') as fh:
        fh.write(data)

url = f'http://{a.host}:8200/api/release?' + urllib.parse.urlencode(dict(notes=a.notes))
req = urllib.request.Request(url, data=data, method='POST',
                             headers={'X-Api-Key': a.key, 'Content-Type': 'application/gzip'})
try:
    r = urllib.request.urlopen(req, timeout=60)
    print(f'已推送主机程序 版本 {version}({len(data) // 1024} KB)→ 在大屏“⚙ 设置”里点“立即升级主机”')
except urllib.error.HTTPError as e:
    body = e.read().decode(errors='replace')[:300]
    if e.code == 404:
        sys.exit('主机的更新器还是旧版本,不支持推送。请按 docs/09 第 14 节把新的 host 文件夹拷到主机并运行一次 setup-host.sh(只需这一次)')
    sys.exit(f'推送主机程序失败 {e.code}: {body}')
except OSError as e:
    sys.exit(f'连不上主机更新器 http://{a.host}:8200 :{e}')
