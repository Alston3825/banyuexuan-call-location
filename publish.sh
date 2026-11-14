#!/bin/bash
# 一键:编译平板+手表 APK → 发布到主机 → 设备上出现"有新版本"提示。
# 用法: ./publish.sh <主机IP> ["更新说明"]        例: ./publish.sh 192.168.1.106 "按钮改成方块"
# 主机密钥默认 dev-key;正式环境:  KEY=xxxx ./publish.sh <主机IP>
set -e
HOST_IP="${1:?用法: ./publish.sh <主机IP> [\"更新说明\"]}"; NOTES="${2:-}"; KEY="${KEY:-dev-key}"
cd "$(dirname "$0")"
export JAVA_HOME="${JAVA_HOME:-/Applications/Android Studio.app/Contents/jbr/Contents/Home}"
cd android
./gradlew --stop >/dev/null 2>&1 || true
rm -rf tablet/build watch/build common/build        # 百度同步盘会在 build 目录里生成重复文件,先清干净
./gradlew :tablet:assembleDebug :watch:assembleDebug -q
cd ..
for app in tablet watch; do
  apk="android/$app/build/outputs/apk/debug/$app-debug.apk"
  cp "$apk" "dist/bycall-$app.apk"
  python3 host/tools/publish_apk.py --host "http://$HOST_IP:8100" --key "$KEY" --app "$app" --apk "$apk" --notes "$NOTES"
done
