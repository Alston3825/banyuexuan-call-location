"""主机代码更新包:打包(publish_host.py)→ 检查 → 存为待升级 → 生成构建上下文 → 构建。不依赖 fastapi。"""
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest

from bycall.updater.release import (BASE, ReleaseError, Releases, build_context, build_overlay, check_package,
                                    ensure_base)

HOST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_pkg(path, files):
    with tarfile.open(path, 'w:gz') as tf:
        for name, data in files.items():
            if data is None:
                ti = tarfile.TarInfo(name); ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            else:
                b = data.encode()
                ti = tarfile.TarInfo(name); ti.size = len(b)
                tf.addfile(ti, io.BytesIO(b))


GOOD = {'bycall/main.py': 'print(1)', 'bycall/VERSION': '20261117-1530', 'requirements.txt': 'fastapi\n',
        'tools/x.py': ''}


class FakeDocker:
    def __init__(self, has_base=False, build_out=b''):
        self.has_base, self.build_out, self.calls = has_base, build_out, []

    def json(self, method, path, **kw):
        self.calls.append((method, path))
        if path == f'/images/{BASE}/json' and not self.has_base:
            raise RuntimeError('404')
        return {}

    def call(self, method, path, body=None, headers=None, timeout=60, ok=(200,)):
        self.calls.append((method, path))
        return self.build_out


class TestRelease(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, 'p.tar.gz')

    def test_good_package(self):
        make_pkg(self.p, GOOD)
        self.assertEqual(check_package(self.p), ('20261117-1530', 4))

    def test_rejects_bad_packages(self):
        for files, why in [
            (dict(GOOD, **{'etc/passwd': 'x'}), '多余的文件'),
            (dict(GOOD, **{'bycall/../../evil': 'x'}), '不安全'),
            ({k: v for k, v in GOOD.items() if k != 'bycall/VERSION'}, '缺少 bycall/VERSION'),
        ]:
            make_pkg(self.p, files)
            with self.assertRaisesRegex(ReleaseError, why):
                check_package(self.p)
        with tarfile.open(self.p, 'w:gz') as tf:
            ti = tarfile.TarInfo('bycall/link'); ti.type = tarfile.SYMTYPE; ti.linkname = '/etc/passwd'
            tf.addfile(ti)
        with self.assertRaisesRegex(ReleaseError, '不允许的文件类型'):
            check_package(self.p)
        open(self.p, 'wb').write(b'not a tar')
        with self.assertRaisesRegex(ReleaseError, '不是有效的更新包'):
            check_package(self.p)

    def test_context_has_overlay_dockerfile(self):
        make_pkg(self.p, {k: v for k, v in GOOD.items() if not k.startswith('tools')})
        ctx = build_context(self.p, '20261117-1530')
        with tarfile.open(fileobj=io.BytesIO(ctx)) as tf:
            names = tf.getnames()
            df = tf.extractfile('Dockerfile').read().decode()
        self.assertIn(f'FROM {BASE}', df)
        self.assertIn('bycall/main.py', names)
        self.assertIn('tools', names)                     # 包里没有 tools 也要有空目录,否则 COPY tools 会失败

    def test_releases_store(self):
        r = Releases(os.path.join(self.d, 'rel'))
        self.assertIsNone(r.pending())
        make_pkg(self.p, GOOD)
        m = r.save(self.p, '修大屏')
        self.assertEqual((m['version'], m['notes']), ('20261117-1530', '修大屏'))
        self.assertEqual(r.pending()['version'], '20261117-1530')
        r.clear()
        self.assertIsNone(r.pending())

    def test_base_tag_once_and_build_errors(self):
        d = FakeDocker(has_base=False)
        self.assertTrue(ensure_base(d))
        self.assertIn(('POST', '/images/bycall-host:latest/tag?repo=bycall-host&tag=base'), d.calls)
        self.assertFalse(ensure_base(FakeDocker(has_base=True)))
        ok = FakeDocker(build_out=b'{"stream":"Step 1/9"}\n{"aux":{"ID":"sha256:1"}}\n')
        build_overlay(ok, b'ctx', 'v1')
        bad = FakeDocker(build_out=b'{"stream":"Step 4/9 : RUN pip"}\n{"error":"pip failed"}\n')
        with self.assertRaisesRegex(ReleaseError, 'pip failed'):
            build_overlay(bad, b'ctx', 'v1')

    def test_publish_host_script_builds_valid_package(self):
        out = os.path.join(self.d, 'host-update.tar.gz')
        r = subprocess.run([sys.executable, os.path.join(HOST, 'tools', 'publish_host.py'), '--host', '127.0.0.1',
                            '--key', 'k', '--out', out], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)              # 本机没有更新器 → 推送失败,但包已生成
        version, n = check_package(out)
        self.assertRegex(version, r'^\d{8}-\d{4}$')
        with tarfile.open(out) as tf:
            names = tf.getnames()
        self.assertIn('bycall/updater/release.py', names)
        self.assertFalse(any('__pycache__' in x for x in names))


if __name__ == '__main__':
    unittest.main()
