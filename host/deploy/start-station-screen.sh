#!/usr/bin/env bash
# Ubuntu 桌面登录后启动护士站；参数为主机基础地址，不传密钥。
set -euo pipefail
base="${1:-}"
case "$base" in
  http://*|https://*) ;;
  *) echo '用法: start-station-screen.sh http://主机固定IP:8100' >&2; exit 2 ;;
esac
if [[ "$base" == *'?'* || "$base" == *'#'* || "$base" == *'@'* ]]; then
  echo '地址不能含凭据、查询参数或片段；请在大屏连接设置里填写密钥。' >&2
  exit 2
fi
base="${base%/}"
ready=false
for ((n=0; n<60; n++)); do
  if curl --fail --silent --max-time 2 "$base/health" >/dev/null; then
    ready=true
    break
  fi
  sleep 2
done
if [[ "$ready" != true ]]; then
  echo "主机尚未就绪，请检查 Docker 服务及地址: $base" >&2
  exit 1
fi
if command -v firefox >/dev/null 2>&1; then
  exec firefox --kiosk "$base/station"
elif command -v chromium >/dev/null 2>&1; then
  exec chromium --kiosk --no-first-run "$base/station"
elif command -v chromium-browser >/dev/null 2>&1; then
  exec chromium-browser --kiosk --no-first-run "$base/station"
elif command -v google-chrome >/dev/null 2>&1; then
  exec google-chrome --kiosk --no-first-run "$base/station"
else
  echo '未找到支持的桌面浏览器；请先用 Ubuntu 桌面的浏览器打开主机 /station。' >&2
  exit 1
fi
