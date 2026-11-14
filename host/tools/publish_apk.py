"""开发者在 Mac 上用:把编译好的 APK 发布到主机,设备随后会收到"有新版本"提示。
  python3 tools/publish_apk.py --host http://192.168.1.106:8100 --key dev-key --app watch --apk path/to.apk [--notes "更新说明"]
版本号(versionCode/versionName)由 Android SDK 的 aapt2 从 APK 里读出。"""
import argparse, glob, os, re, subprocess, sys, urllib.parse, urllib.request, urllib.error

ap = argparse.ArgumentParser()
ap.add_argument('--host', required=True); ap.add_argument('--key', default='dev-key')
ap.add_argument('--app', required=True, choices=['tablet', 'watch']); ap.add_argument('--apk', required=True)
ap.add_argument('--notes', default=''); ap.add_argument('--force', action='store_true')
a = ap.parse_args()

tools = sorted(glob.glob(os.path.expanduser('~/Library/Android/sdk/build-tools/*/aapt2')))
if not tools:
    sys.exit('找不到 aapt2(需要 Android SDK build-tools)')
out = subprocess.run([tools[-1], 'dump', 'badging', a.apk], capture_output=True, text=True).stdout
m = re.search(r"package: name='([^']+)' versionCode='(\d+)' versionName='([^']*)'", out)
if not m:
    sys.exit('读不出 APK 的版本号,文件是否损坏?')
pkg, code, name = m.group(1), int(m.group(2)), m.group(3)
q = urllib.parse.urlencode(dict(version_code=code, version_name=name, notes=a.notes, force=str(a.force).lower()))
req = urllib.request.Request(f'{a.host.rstrip("/")}/app/publish/{a.app}?{q}', data=open(a.apk, 'rb').read(),
                             headers={'X-Api-Key': a.key, 'Content-Type': 'application/octet-stream'}, method='POST')
try:
    r = urllib.request.urlopen(req, timeout=60)
    print(f'已发布 {a.app}: {pkg} 版本 {name} (code {code})  → {a.host}')
except urllib.error.HTTPError as e:
    sys.exit(f'发布失败 {e.code}: {e.read().decode()[:200]}')
except OSError as e:
    sys.exit(f'连不上主机 {a.host}: {e}')
