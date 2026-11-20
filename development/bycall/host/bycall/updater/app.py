"""更新器网页服务。所有操作需 API 密钥(与主机相同)。"""
from __future__ import annotations

import hmac
import os
import shutil
import tarfile
import tempfile
import threading
import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from ..version import VERSION
from .dockerapi import DockerAPI, DockerError
from .release import ReleaseError, Releases, build_context, build_overlay, ensure_base
from .swap import IMAGE, host_version, snapshot_old, swap

HERE = os.path.dirname(__file__)


def safe_extract(tar_path, dest):
    """把模型包解到 dest。只允许普通文件和目录,拒绝绝对路径、.. 和链接(防止恶意包写到别处)。返回文件数。"""
    dest = os.path.realpath(dest)
    n = 0
    with tarfile.open(tar_path, 'r:*') as tf:
        for m in tf:
            if not (m.isreg() or m.isdir()):
                continue                                  # 跳过链接/设备等
            if os.path.basename(m.name).startswith('._') or '/._' in m.name:
                continue                                  # macOS 打包产生的垃圾文件
            target = os.path.realpath(os.path.join(dest, m.name))
            if not (target == dest or target.startswith(dest + os.sep)):
                raise ValueError(f'压缩包里有不安全的路径:{m.name}')
            if m.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with tf.extractfile(m) as src, open(target + '.part', 'wb') as out:
                shutil.copyfileobj(src, out, 1 << 20)
            os.replace(target + '.part', target)
            n += 1
    return n


def create_app(api_key: Optional[str], docker: Optional[DockerAPI] = None, models_dir='/models',
               service='host', health_url='http://host:8000/health', tmp_dir=None, version_wait=15,
               data_dir=None) -> FastAPI:
    app = FastAPI(title='伴月轩主机更新')
    docker = docker or DockerAPI()
    data_dir = data_dir or os.environ.get('UPDATER_DATA', '/updater-data')
    try:
        releases = Releases(data_dir)
    except OSError:                       # 老的 compose 没挂数据目录:先放临时目录(更新器重启会丢,重新推送即可)
        releases = Releases(os.path.join(tempfile.gettempdir(), 'bycall-releases'))
    job = dict(kind=None, stage='idle', msg='', ok=None, version=None, at=0)
    lock = threading.Lock()

    def auth(x_api_key: Optional[str] = Header(default=None)):
        if api_key and not hmac.compare_digest(x_api_key or '', api_key):
            raise HTTPException(401, 'invalid api key')

    def report(stage, msg, **kw):
        job.update(stage=stage, msg=msg, at=time.time(), **kw)

    def run_host(path):
        try:
            old_cfg = snapshot_old(docker, service)
            report('loading', '正在载入新镜像(约 1~2 分钟)…')
            loaded = docker.load_image(path)
            if IMAGE not in loaded:
                raise DockerError(f'这个包里没有 {IMAGE}(载入了:{loaded or "无"}),请确认选的是 bycall-host-amd64.tar.gz')
            swap(docker, report, service, health_url, old_cfg)
            v = None
            for _ in range(version_wait):
                v = host_version(health_url)
                if v:
                    break
                time.sleep(1)
            report('done', f'更新成功,主机版本 {v or "未知"}', ok=True, version=v)
        except Exception as e:
            report('failed', str(e), ok=False)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
            lock.release()

    def run_release():
        """代码升级:在基础镜像上换上新代码 → 换版(失败自动换回)→ 清掉待升级标记。"""
        meta = releases.pending() or {}
        try:
            old_cfg = snapshot_old(docker, service)
            if ensure_base(docker):
                print('[release] 已把当前镜像标记为基础镜像 bycall-host:base', flush=True)
            report('building', f'正在生成新版本 {meta.get("version")}(一般几秒;依赖有变化时需联网,可能几分钟)…')
            build_overlay(docker, build_context(releases.pkg, meta.get('version', 'dev')), meta.get('version', 'dev'))
            swap(docker, report, service, health_url, old_cfg)
            v = None
            for _ in range(version_wait):
                v = host_version(health_url)
                if v:
                    break
                time.sleep(1)
            releases.clear()
            report('done', f'升级成功,主机版本 {v or meta.get("version")}', ok=True, version=v)
        except Exception as e:
            report('failed', str(e), ok=False)
        finally:
            lock.release()

    async def save_upload(request: Request, suffix):
        fd, path = tempfile.mkstemp(suffix=suffix, dir=tmp_dir)
        try:
            with os.fdopen(fd, 'wb') as f:
                async for chunk in request.stream():
                    f.write(chunk)
        except Exception:
            os.remove(path)
            raise
        if os.path.getsize(path) < 20:
            os.remove(path)
            raise HTTPException(400, '没有收到文件')
        return path

    @app.get('/')
    def page():
        return FileResponse(os.path.join(HERE, 'update.html'), headers={'Cache-Control': 'no-store'})

    @app.get('/api/status', dependencies=[Depends(auth)])
    def status():
        mp = os.path.join(models_dir, 'whisper-small', 'model.bin')
        return dict(host_version=host_version(health_url), updater_version=VERSION, pending=releases.pending(),
                    models_installed=os.path.isfile(mp),
                    models_size_mb=round(os.path.getsize(mp) / 1e6) if os.path.isfile(mp) else 0, job=job)

    @app.post('/api/upload/host', dependencies=[Depends(auth)], status_code=202)
    async def upload_host(request: Request):
        if not lock.acquire(blocking=False):
            raise HTTPException(409, '正在更新中,请稍候')
        try:
            report('uploading', '正在接收文件…', kind='host', ok=None, version=None)
            path = await save_upload(request, '.tar.gz')
        except BaseException:
            report('idle', '')
            lock.release()
            raise
        report('uploaded', '文件已收到,开始更新…')
        threading.Thread(target=run_host, args=(path,), daemon=True).start()
        return dict(started=True)

    @app.post('/api/release', dependencies=[Depends(auth)])
    async def push_release(request: Request, notes: str = ''):
        """Mac 上 publish.sh 推送主机代码包:先存为“待升级”,不立即生效;在大屏或本页点“升级”才换版。"""
        path = await save_upload(request, '.tar.gz')
        try:
            return releases.save(path, notes)
        except ReleaseError as e:
            raise HTTPException(400, str(e))
        finally:
            if os.path.exists(path):
                os.remove(path)

    @app.post('/api/apply', dependencies=[Depends(auth)], status_code=202)
    def apply_release():
        if not releases.pending():
            raise HTTPException(404, '没有待升级的新版本')
        if not lock.acquire(blocking=False):
            raise HTTPException(409, '正在更新中,请稍候')
        report('starting', '开始升级主机…', kind='release', ok=None, version=None)
        threading.Thread(target=run_release, daemon=True).start()
        return dict(started=True)

    @app.post('/api/upload/models', dependencies=[Depends(auth)])
    async def upload_models(request: Request):
        if not lock.acquire(blocking=False):
            raise HTTPException(409, '正在更新中,请稍候')
        path = None
        try:
            report('uploading', '正在接收语音识别模型…', kind='models', ok=None)
            path = await save_upload(request, '.tar.gz')
            report('extracting', '正在解压模型(约几十秒)…')
            os.makedirs(models_dir, exist_ok=True)
            stage = tempfile.mkdtemp(prefix='.incoming-', dir=models_dir)     # 先解到临时目录,检查通过再替换,坏包不会弄坏现有模型
            try:
                n = safe_extract(path, stage)
                if not os.path.isfile(os.path.join(stage, 'whisper-small', 'model.bin')):
                    raise ValueError('压缩包里没有 whisper-small/model.bin,请确认选的是 bycall-models.tar.gz')
                final = os.path.join(models_dir, 'whisper-small')
                if os.path.isdir(final):
                    shutil.rmtree(final)
                os.replace(os.path.join(stage, 'whisper-small'), final)
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            report('done', f'模型已安装({n} 个文件),现在可以在大屏上"转文字"了', ok=True)
            return dict(files=n)
        except HTTPException:
            report('idle', '')
            raise
        except Exception as e:
            report('failed', str(e), ok=False)
            raise HTTPException(400, str(e))
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            lock.release()

    return app
