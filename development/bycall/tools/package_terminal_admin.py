"""Create review/test artifacts locally; never upload or install anything."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import tarfile
import zipfile

root = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument('--out', required=True)
ap.add_argument('--name-prefix', default='bycall-terminal-admin')
a = ap.parse_args()
if not a.name_prefix.replace('-', '').replace('_', '').isalnum():
    raise SystemExit('包名前缀无效')
out = Path(a.out).resolve()
out.mkdir(parents=True, exist_ok=True)
skip = {'build', '.gradle', '.git', '__pycache__', 'models', 'data', 'auth', 'config', 'dist', 'node_modules'}
allowed_suffixes = {'.py', '.kt', '.kts', '.xml', '.html', '.md', '.sh', '.properties', '.yml', '.bat'}
explicit = {'android/gradlew', 'host/Dockerfile', 'host/requirements.txt', 'host/.env.example',
            'host/bycall/VERSION', 'host/bycall/custom/banyuexuan/demo_pilot.json',
            'host/mosquitto/mosquitto.conf', 'host/mosquitto/mosquitto.prod.conf',
            'android/gradle/wrapper/gradle-wrapper.jar'}
files = []
for p in sorted(root.rglob('*')):
    rel = p.relative_to(root)
    if not p.is_file() or p.is_symlink() or any(x in skip for x in rel.parts):
        continue
    if rel.as_posix() in explicit or (p.suffix in allowed_suffixes and not p.name.startswith('.') and p.name != 'local.properties'):
        files.append(p)
with zipfile.ZipFile(out / (a.name_prefix + '-source.zip'), 'w', zipfile.ZIP_DEFLATED) as z:
    for p in files:
        z.write(p, 'bycall/' + str(p.relative_to(root)))
with tarfile.open(out / 'host-update.tar.gz', 'w:gz') as tf:
    for p in files:
        rel = p.relative_to(root)
        if rel.parts[:2] in [('host', 'bycall'), ('host', 'tools')] or rel.as_posix() == 'host/requirements.txt':
            tf.add(p, arcname=str(p.relative_to(root / 'host')), recursive=False)
for kind in ('watch', 'tablet'):
    apk = root / f'android/{kind}/build/outputs/apk/debug/{kind}-debug.apk'
    if not apk.is_file():
        raise SystemExit('请先编译 ' + kind)
    shutil.copy2(apk, out / f'bycall-{kind}-debug.apk')
manifest = {p.name: dict(bytes=p.stat().st_size, sha256=hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(out.iterdir()) if p.suffix in ('.apk', '.zip', '.gz')}
(out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({k: v['bytes'] for k, v in manifest.items()}, ensure_ascii=False))
