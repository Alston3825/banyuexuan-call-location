import io
import os
import tarfile
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from bycall.updater import swap as S
from bycall.updater.app import create_app, safe_extract
from bycall.updater.dockerapi import DockerError

OLD = 'abcdef123456' + 'f' * 52
INSPECT = {
    'Id': OLD, 'Name': '/host-host-1', 'Image': 'sha256:old',
    'Config': {'Env': ['A=1'], 'Cmd': ['python', '-m', 'bycall.main'], 'Labels': {'com.docker.compose.service': 'host'},
               'Healthcheck': {'Test': ['CMD', 'x']}, 'ExposedPorts': {'8000/tcp': {}}, 'Hostname': 'abcdef123456'},
    'HostConfig': {'Binds': ['/d:/data'], 'RestartPolicy': {'Name': 'unless-stopped'}, 'PortBindings': {'8000/tcp': [{'HostPort': '8100'}]}},
    'NetworkSettings': {'Networks': {'host_default': {'Aliases': ['host', 'host-host-1', 'abcdef123456']}}},
    'State': {'Running': True, 'Status': 'running'},
}


IMG_CFG = {'Env': ['A=1'], 'Cmd': ['python', '-m', 'bycall.main'], 'Labels': {}, 'Healthcheck': None}


class FakeApi:
    """脚本化的 Docker:记录调用;healthy 控制新容器是否通过健康检查。"""
    def __init__(self, healthy=True):
        self.calls, self.healthy, self.loaded = [], healthy, ['bycall-host:latest']

    def json(self, method, path, **kw):
        self.calls.append((method, path))
        if path.startswith('/containers/json'):
            return [{'Id': OLD}]
        if path == 'IMG':
            raise AssertionError
        if path.startswith('/images/') and path.endswith('/json'):
            return {'Config': IMG_CFG}
        if path == f'/containers/{OLD}/json':
            return INSPECT
        if path.startswith('/containers/create'):
            return {'Id': 'new'}
        if path == '/containers/new/json':
            return {'State': {'Running': self.healthy, 'Status': 'running' if self.healthy else 'exited',
                              'Health': {'Status': 'healthy'} if self.healthy else None}}
        raise AssertionError(path)

    def call(self, method, path, **kw):
        self.calls.append((method, path))
        return b''

    def load_image(self, p):
        return self.loaded


def make_tar(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        for name, data in members:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


class TestSwap(unittest.TestCase):
    def test_create_body_copies_config(self):
        b = S.create_body(INSPECT, old_image_cfg=IMG_CFG)
        self.assertEqual(b['Image'], 'bycall-host:latest')
        self.assertEqual(b['HostConfig'], INSPECT['HostConfig'])             # 挂载、端口、重启策略照搬
        self.assertNotIn('Env', b)                                           # 环境变量都是镜像自带的 → 用新镜像的
        self.assertNotIn('Cmd', b)                                           # 启动命令是镜像默认的 → 用新镜像的
        self.assertEqual(b['Labels']['com.docker.compose.service'], 'host')
        self.assertEqual(b['NetworkingConfig']['EndpointsConfig']['host_default']['Aliases'], ['host', 'host-host-1'])  # 去掉旧容器 id 别名
        self.assertIn('Healthcheck', b)                                      # 旧镜像没有而容器有 → 是 compose 里设的,保留
        # compose 里自己设的环境变量/命令要保留
        insp2 = {**INSPECT, 'Config': {**INSPECT['Config'], 'Env': ['A=1', 'BYCALL_API_KEY=zzz'], 'Cmd': ['python', '-m', 'other']}}
        b2 = S.create_body(insp2, old_image_cfg=IMG_CFG)
        self.assertEqual((b2['Env'], b2['Cmd']), (['BYCALL_API_KEY=zzz'], ['python', '-m', 'other']))

    def test_swap_success_removes_old(self):
        api = FakeApi()
        stages = []
        self.assertEqual(S.swap(api, lambda s, m: stages.append(s)), 'new')
        self.assertEqual(stages, ['creating', 'restarting', 'checking'])
        paths = [p for _, p in api.calls]
        self.assertIn('/containers/new/start', paths)
        self.assertIn(f'/containers/{OLD}?force=true', paths)   # 旧容器被清理

    def test_swap_rolls_back_when_unhealthy(self):
        api = FakeApi(healthy=False)
        S_wait = S.wait_healthy
        S.wait_healthy = lambda a, c, seconds=90: False
        try:
            with self.assertRaises(DockerError) as cm:
                S.swap(api, lambda s, m: None)
        finally:
            S.wait_healthy = S_wait
        self.assertIn('已自动换回旧版本', str(cm.exception))
        paths = [p for _, p in api.calls]
        self.assertIn(f'/containers/{OLD}/start', paths)                       # 旧的重新启动了
        self.assertIn(f'/containers/{OLD}/rename?name=host-host-1', paths)     # 并换回原名


class TestExtract(unittest.TestCase):
    def test_safe_extract_blocks_traversal_and_skips_junk(self):
        with tempfile.TemporaryDirectory() as d:
            ok = os.path.join(d, 'ok.tar.gz')
            open(ok, 'wb').write(make_tar([('whisper-small/model.bin', b'x' * 10), ('whisper-small/._model.bin', b'junk')]))
            out = os.path.join(d, 'out')
            self.assertEqual(safe_extract(ok, out), 1)
            self.assertTrue(os.path.isfile(os.path.join(out, 'whisper-small', 'model.bin')))
            self.assertFalse(os.path.exists(os.path.join(out, 'whisper-small', '._model.bin')))
            bad = os.path.join(d, 'bad.tar.gz')
            open(bad, 'wb').write(make_tar([('../evil.txt', b'x')]))
            with self.assertRaises(ValueError):
                safe_extract(bad, out)
            self.assertFalse(os.path.exists(os.path.join(d, 'evil.txt')))


class TestUpdaterApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.models = os.path.join(self.tmp.name, 'models')
        self.api = FakeApi()
        self.app = create_app('k', docker=self.api, models_dir=self.models, health_url='http://127.0.0.1:1/h', tmp_dir=self.tmp.name, version_wait=1)
        self.c = TestClient(self.app)
        self.h = {'X-Api-Key': 'k'}

    def tearDown(self):
        self.tmp.cleanup()

    def wait_job(self):
        for _ in range(100):
            j = self.c.get('/api/status', headers=self.h).json()['job']
            if j['stage'] in ('done', 'failed'):
                return j
            time.sleep(0.05)
        self.fail('job did not finish')

    def test_auth_required(self):
        self.assertEqual(self.c.get('/api/status').status_code, 401)
        self.assertEqual(self.c.post('/api/upload/host', content=b'x' * 2000).status_code, 401)
        self.assertEqual(self.c.get('/').status_code, 200)                  # 页面本身不需要密钥

    def test_host_update_flow(self):
        r = self.c.post('/api/upload/host', content=b'x' * 5000, headers=self.h)
        self.assertEqual(r.status_code, 202)
        j = self.wait_job()
        self.assertEqual(j['stage'], 'done')
        self.assertTrue(j['ok'])
        self.assertEqual(os.listdir(self.tmp.name), [])                     # 临时文件已清掉

    def test_wrong_image_is_rejected_without_swapping(self):
        self.api.loaded = ['something-else:1']
        self.c.post('/api/upload/host', content=b'x' * 5000, headers=self.h)
        j = self.wait_job()
        self.assertEqual((j['stage'], j['ok']), ('failed', False))
        self.assertFalse([p for _, p in self.api.calls if 'create' in p])   # 没动现有主机

    def test_empty_upload_rejected_and_lock_released(self):
        self.assertEqual(self.c.post('/api/upload/host', content=b'', headers=self.h).status_code, 400)
        self.assertEqual(self.c.post('/api/upload/host', content=b'x' * 5000, headers=self.h).status_code, 202)
        self.wait_job()

    def test_models_upload(self):
        data = make_tar([('whisper-small/model.bin', b'm' * 3000), ('whisper-small/config.json', b'{}')])
        r = self.c.post('/api/upload/models', content=data, headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        s = self.c.get('/api/status', headers=self.h).json()
        self.assertTrue(s['models_installed'])
        bad = self.c.post('/api/upload/models', content=make_tar([('other/x.bin', b'x' * 3000)]), headers=self.h)
        self.assertEqual(bad.status_code, 400)


if __name__ == '__main__':
    unittest.main()
