#!/bin/bash
# 一键发布:编译平板+手表 APK、打包主机程序 → 推送到主机。
#   设备(平板/手表)马上弹出“发现新版本”,点一下就更新;
#   主机程序存为“待升级”,在大屏“⚙ 设置”里点“立即升级主机”(失败自动换回旧版)。
#
# 用法: ./publish.sh <主机IP> ["更新说明"] [all|apps|host]
#   例: ./publish.sh 192.168.1.108 "求助按钮变大"          # 全部发布
#       ./publish.sh 192.168.1.108 "修大屏" host            # 只发布主机程序
#       ./publish.sh 192.168.1.108 "修手表" apps            # 只发布平板和手表
# 主机密钥:第一次运行会问一次并保存在本机 ~/.bycall-publish-key(只有你能读),以后不用再输;
#          也可以临时用 KEY=xxxx ./publish.sh ...
set -e
HOST_IP="${1:?用法: ./publish.sh <主机IP> [\"更新说明\"] [all|apps|host]}"; NOTES="${2:-}"; WHAT="${3:-all}"
cd "$(dirname "$0")"
KEYFILE="$HOME/.bycall-publish-key"
if [ -z "${KEY:-}" ] && [ -f "$KEYFILE" ]; then KEY="$(tr -d '[:space:]' < "$KEYFILE")"; fi
if [ -z "${KEY:-}" ]; then
  read -r -p "请输入主机密钥(N150 上 安装信息.txt 里 key= 后面那串): " KEY
  KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
  [ -n "$KEY" ] || { echo "没有输入密钥"; exit 1; }
  umask 077; printf '%s\n' "$KEY" > "$KEYFILE"; echo "密钥已保存到 $KEYFILE,以后不用再输"
fi
# 先确认主机连得上、密钥对
code=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Api-Key: $KEY" "http://$HOST_IP:8100/system/update" || true)
case "$code" in
  200) ;;
  401) echo "主机密钥不对。请检查后删除 $KEYFILE 再运行一次"; exit 1 ;;
  000) echo "连不上主机 http://$HOST_IP:8100 ,请确认 Mac 和主机在同一个网络、主机已开机"; exit 1 ;;
  404) echo "(主机还是旧版本,没有一键升级接口;本次照常发布)" ;;
esac

if [ "$WHAT" = all ] || [ "$WHAT" = apps ]; then
  export JAVA_HOME="${JAVA_HOME:-/Applications/Android Studio.app/Contents/jbr/Contents/Home}"
  cd android
  ./gradlew --stop >/dev/null 2>&1 || true
  rm -rf tablet/build watch/build common/build        # 百度同步盘会在 build 目录里生成重复文件,先清干净
  echo "== 编译平板和手表 APK,并检查旧安卓兼容性(lint)…"
  ./gradlew :tablet:assembleDebug :watch:assembleDebug :watch:lintDebug :tablet:lintDebug -q
  cd ..
  mkdir -p dist
  for app in tablet watch; do
    apk="android/$app/build/outputs/apk/debug/$app-debug.apk"
    cp "$apk" "dist/bycall-$app.apk"
    python3 host/tools/publish_apk.py --host "http://$HOST_IP:8100" --key "$KEY" --app "$app" --apk "$apk" --notes "$NOTES"
  done
fi
if [ "$WHAT" = all ] || [ "$WHAT" = host ]; then
  python3 host/tools/publish_host.py --host "$HOST_IP" --key "$KEY" --notes "$NOTES" --out dist/host-update.tar.gz
fi
echo "完成。"
