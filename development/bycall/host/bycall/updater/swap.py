"""换版逻辑:把正在运行的主机容器,换成用新镜像创建的、配置完全相同的新容器;失败则换回。"""
from __future__ import annotations

import time
import urllib.request

from .dockerapi import DockerError

IMAGE = 'bycall-host:latest'


def find_host(api, service='host'):
    lst = api.json('GET', '/containers/json?all=true&filters=' + _q({'label': [f'com.docker.compose.service={service}']}))
    if not lst:
        raise DockerError(f'没找到主机容器(compose 服务 {service})。请先用 docker compose up -d 启动主机')
    lst.sort(key=lambda c: c.get('State') != 'running')          # 优先选正在运行的(避免选到上次失败遗留的容器)
    return api.json('GET', f"/containers/{lst[0]['Id']}/json")


def _q(d):
    import json
    from urllib.parse import quote
    return quote(json.dumps(d))


def create_body(insp, image=IMAGE, old_image_cfg=None):
    """用旧容器的配置生成新容器的创建参数:端口、挂载、重启策略、网络别名、compose 里设置的环境变量都照搬;
    但"镜像自带的默认值"(启动命令、环境变量、健康检查等)不照搬,让新镜像用它自己的。
    判断方法:和旧镜像的默认值一样 → 说明不是人为设置的,不传。"""
    cfg, hc = insp['Config'], insp['HostConfig']
    img = old_image_cfg or {}

    def own(key):                        # 只有和旧镜像默认值不同(即人为设置)才保留
        v = cfg.get(key)
        return None if v == img.get(key) else v

    env = [e for e in (cfg.get('Env') or []) if e not in (img.get('Env') or [])]
    labels = {k: v for k, v in (cfg.get('Labels') or {}).items() if (img.get('Labels') or {}).get(k) != v}
    nets = {}
    for name, n in (insp.get('NetworkSettings', {}).get('Networks') or {}).items():
        short = insp['Id'][:12]
        aliases = [a for a in (n.get('Aliases') or []) if a != short and not insp['Id'].startswith(a)]
        nets[name] = {'Aliases': aliases}
    body = dict(Image=image, Env=env or None, Cmd=own('Cmd'), Entrypoint=own('Entrypoint'),
                WorkingDir=own('WorkingDir'), User=own('User'), Labels=labels,
                Healthcheck=own('Healthcheck'), HostConfig=hc, NetworkingConfig={'EndpointsConfig': nets})
    return {k: v for k, v in body.items() if v is not None}


def wait_healthy(api, cid, seconds=90):
    """等新容器通过健康检查。已退出,或反复崩溃重启(重启 2 次以上)就立刻判失败,不干等 90 秒。"""
    end = time.time() + seconds
    while time.time() < end:
        d = api.json('GET', f'/containers/{cid}/json')
        st = d['State']
        if d.get('RestartCount', 0) >= 2 or st.get('Restarting') or st.get('Status') in ('exited', 'dead'):
            return False
        if (st.get('Health') or {}).get('Status') == 'healthy':
            return True
        time.sleep(2)
    return False


def host_version(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            import json
            return json.loads(r.read()).get('version')
    except Exception:
        return None


def snapshot_old(api, service='host'):
    """换版前先记下旧镜像自带的默认配置。必须在载入新镜像之前做:载入后 latest 标签挪走,旧镜像可能查不到。"""
    old = find_host(api, service)
    try:
        return api.json('GET', f"/images/{old['Image']}/json")['Config']
    except DockerError:
        return None


def swap(api, report, service='host', health_url=None, old_cfg=None):
    """report(stage, msg):阶段回调。成功返回新容器 id;失败已回滚并抛 DockerError。"""
    old = find_host(api, service)
    old_id, name = old['Id'], old['Name'].lstrip('/')
    old_image = old['Image']
    report('creating', '创建新版本容器…')
    if old_cfg is None:                  # 没能事先记下旧镜像的默认配置:保守地照搬旧容器的全部设置
        print('[swap] 没有旧镜像默认配置,照搬旧容器设置', flush=True)
    body = create_body(old, old_image_cfg=old_cfg)
    new_id = api.json('POST', f'/containers/create?name={name}-new', body=body)['Id']
    try:
        report('restarting', '停止旧版本,启动新版本…')
        api.call('POST', f'/containers/{old_id}/stop?t=15', timeout=60, ok=(204, 304))
        api.call('POST', f'/containers/{old_id}/rename?name={name}-old', ok=(204,))
        api.call('POST', f'/containers/{new_id}/rename?name={name}', ok=(204,))
        api.call('POST', f'/containers/{new_id}/start', ok=(204, 304))
        report('checking', '等待新版本启动并通过健康检查(最多 90 秒)…')
        if not wait_healthy(api, new_id):
            raise DockerError('新版本没能正常启动')
    except Exception as e:
        report('rollback', f'新版本启动失败({e}),正在换回旧版本…')
        try:
            api.call('POST', f'/containers/{new_id}/stop?t=5', ok=(204, 304))
        except DockerError:
            pass
        try:
            api.call('POST', f'/containers/{new_id}/rename?name={name}-failed', ok=(204,))
        except DockerError:
            pass
        api.call('POST', f'/containers/{old_id}/rename?name={name}', ok=(204,))
        api.call('POST', f'/containers/{old_id}/start', ok=(204, 304))
        try:
            api.call('DELETE', f'/containers/{new_id}?force=true', ok=(204,))
        except DockerError:
            pass
        raise DockerError(f'更新失败,已自动换回旧版本继续运行。原因:{e}') from e
    # 成功:清掉旧容器和旧镜像,别占硬盘
    for path in (f'/containers/{old_id}?force=true', f'/images/{old_image}?force=false'):
        try:
            api.call('DELETE', path, ok=(200, 204))
        except DockerError:
            pass
    return new_id
