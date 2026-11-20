"""主机“代码更新包”:Mac 上 publish.sh 只打包主机代码(几百 KB)推送到更新器,先存为“待升级”;
在大屏或更新页点“升级”后,在现有镜像的基础上只替换代码,生成新镜像再换版(不用联网、不用在 Mac 上装 Docker)。

为了镜像层数不随更新次数增长,每次都从“基础镜像” bycall-host:base 叠加(第一次升级时把当时的镜像标记为基础镜像;
setup-host.sh 完整重建时会删掉旧的基础标记)。依赖(requirements.txt)有变化时才联网用国内源安装。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import time

BASE = 'bycall-host:base'
PIP_INDEX = 'https://mirrors.aliyun.com/pypi/simple/'
ALLOWED_TOP = ('bycall/', 'tools/', 'requirements.txt')

OVERLAY = f"""FROM {BASE}
ARG PIP_INDEX_URL={PIP_INDEX}
ARG BYCALL_VERSION=dev
COPY requirements.txt /tmp/requirements.new
# 依赖没变就跳过(不联网);变了才用国内源安装
RUN python -c "import filecmp,sys; sys.exit(0 if filecmp.cmp('/tmp/requirements.new','/app/requirements.txt',shallow=False) else 1)" \\
 || pip install --no-cache-dir -i "$PIP_INDEX_URL" -r /tmp/requirements.new
RUN rm -rf /app/bycall /app/tools
COPY bycall /app/bycall
COPY tools /app/tools
COPY requirements.txt /app/requirements.txt
LABEL bycall.version=$BYCALL_VERSION
"""


class ReleaseError(ValueError):
    """更新包有问题:message 是给人看的中文原因。"""


def check_package(path):
    """检查代码更新包:只允许 bycall/、tools/、requirements.txt 下的普通文件和目录,不许绝对路径、..、链接;
    必须包含 bycall/main.py 和 bycall/VERSION。返回 (版本号, 文件数)。"""
    try:
        tf = tarfile.open(path, 'r:*')
    except (tarfile.TarError, OSError) as e:
        raise ReleaseError(f'不是有效的更新包({e})')
    names, version = set(), None
    with tf:
        for m in tf:
            n = m.name[2:] if m.name.startswith('./') else m.name
            if not n or n == '.':
                continue
            if m.issym() or m.islnk() or not (m.isreg() or m.isdir()):
                raise ReleaseError(f'更新包里有不允许的文件类型:{m.name}')
            if n.startswith('/') or '..' in n.split('/'):
                raise ReleaseError(f'更新包里有不安全的路径:{m.name}')
            if not (n == 'requirements.txt' or n in ('bycall', 'tools') or n.startswith(ALLOWED_TOP[:2])):
                raise ReleaseError(f'更新包里有多余的文件:{m.name}(只能有 bycall/、tools/、requirements.txt)')
            names.add(n)
            if n == 'bycall/VERSION' and m.isreg():
                version = tf.extractfile(m).read().decode('utf-8', 'replace').strip()
    for need in ('bycall/main.py', 'bycall/VERSION', 'requirements.txt'):
        if need not in names:
            raise ReleaseError(f'更新包里缺少 {need},请用 publish.sh 重新打包')
    if not version:
        raise ReleaseError('更新包里没有版本号')
    return version, len(names)


def build_context(path, version):
    """生成 Docker 构建上下文(tar 字节):overlay Dockerfile + 更新包里的文件。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as out, tarfile.open(path, 'r:*') as src:
        df = OVERLAY.encode()
        ti = tarfile.TarInfo('Dockerfile'); ti.size = len(df); ti.mtime = int(time.time())
        out.addfile(ti, io.BytesIO(df))
        for m in src:
            n = m.name[2:] if m.name.startswith('./') else m.name
            if not n or n == '.':
                continue
            m.name = n
            m.uid = m.gid = 0; m.uname = m.gname = 'root'
            out.addfile(m, src.extractfile(m) if m.isreg() else None)
        if not any((x.name == 'tools' or x.name.startswith('tools/')) for x in out.getmembers()):
            ti = tarfile.TarInfo('tools'); ti.type = tarfile.DIRTYPE; ti.mode = 0o755
            out.addfile(ti)                          # COPY tools 需要目录存在
    return buf.getvalue()


class Releases:
    """“待升级”的代码包,存在更新器的数据目录里(重启不丢)。"""

    def __init__(self, folder):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.pkg = os.path.join(folder, 'pending.tar.gz')
        self.meta = os.path.join(folder, 'pending.json')

    def pending(self):
        try:
            with open(self.meta, encoding='utf-8') as f:
                m = json.load(f)
            return m if os.path.isfile(self.pkg) else None
        except (OSError, ValueError):
            return None

    def save(self, tmp_path, notes=''):
        version, files = check_package(tmp_path)
        h = hashlib.sha256()
        with open(tmp_path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        shutil.move(tmp_path, self.pkg)          # 临时目录和数据目录可能不在同一个磁盘分区,不能用 os.replace
        meta = dict(version=version, notes=(notes or '')[:300], files=files, size=os.path.getsize(self.pkg),
                    sha256=h.hexdigest(), at=time.strftime('%Y-%m-%d %H:%M:%S'))
        with open(self.meta + '.part', 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(self.meta + '.part', self.meta)
        return meta

    def clear(self):
        for p in (self.pkg, self.meta):
            try:
                os.remove(p)
            except OSError:
                pass


def ensure_base(docker, image='bycall-host:latest'):
    """没有基础镜像标记时,把当前正在用的镜像标记为基础镜像(只在第一次代码升级时发生)。"""
    try:
        docker.json('GET', f'/images/{BASE}/json')
        return False
    except Exception:
        repo, tag = BASE.split(':')
        docker.call('POST', f'/images/{image}/tag?repo={repo}&tag={tag}', ok=(200, 201))
        return True


def build_overlay(docker, context_bytes, version, tag='bycall-host:latest', pip_index=PIP_INDEX):
    """调用 Docker Engine API 构建新镜像。失败抛异常(带最后几行构建输出)。"""
    from urllib.parse import quote
    args = quote(json.dumps(dict(PIP_INDEX_URL=pip_index, BYCALL_VERSION=version)))
    raw = docker.call('POST', f'/build?t={quote(tag)}&rm=1&forcerm=1&buildargs={args}', body=context_bytes,
                      headers={'Content-Type': 'application/x-tar'}, timeout=1800)
    lines, err = [], None
    for line in raw.decode(errors='replace').splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get('error'):
            err = j['error']
        if j.get('stream', '').strip():
            lines.append(j['stream'].strip())
    if err:
        tail = ' / '.join(lines[-3:])
        raise ReleaseError(f'生成新版本失败:{err}' + (f'(最后输出:{tail})' if tail else ''))
    return lines
