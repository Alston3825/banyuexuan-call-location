#!/usr/bin/env bash
# 在呼叫主机(N150)接的显示器上直接显示护士站大屏:开机自动登录 → 全屏打开大屏网页
# 支持 Ubuntu 桌面版(GNOME,自动识别)和 Ubuntu Server(自动装最小显示环境)。
#
# 用法(在 host 目录里,主机程序已用 setup-host.sh 装好之后):
#   sudo bash deploy/setup-kiosk.sh             # 设置开机自动显示大屏
#   sudo bash deploy/setup-kiosk.sh --remove    # 取消(已装的软件保留)
# 装完执行 sudo reboot。需要联网(装浏览器)。
#
# 退出全屏大屏:按 Alt+F4(桌面版)或 Ctrl+Alt+F2(服务器版);浏览器 3 秒后会自动重开,
# 要彻底停用请运行 --remove 再重启。
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-}"
REMOVE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --user) USER_NAME="${2:-}"; shift 2 ;;
    --remove) REMOVE=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "请用 sudo 运行"; exit 1; }
[ -n "$USER_NAME" ] && id "$USER_NAME" >/dev/null 2>&1 || { echo "找不到登录用户,请用 --user 用户名 指定"; exit 2; }
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
DESKTOP=""; dpkg -s gnome-shell >/dev/null 2>&1 && DESKTOP=1
GDM=/etc/gdm3/custom.conf
LAUNCH="$HOME_DIR/.local/bin/bycall-station.sh"
AUTOSTART="$HOME_DIR/.config/autostart/bycall-station.desktop"
OVR=/etc/systemd/system/getty@tty1.service.d/bycall-autologin.conf
MARK_BEGIN="# >>> bycall-kiosk >>>"; MARK_END="# <<< bycall-kiosk <<<"
strip_block() { [ -f "$1" ] && sed -i "/$MARK_BEGIN/,/$MARK_END/d" "$1" || true; }

if [ -n "$REMOVE" ]; then
  rm -f "$AUTOSTART" "$LAUNCH" "$OVR" "$HOME_DIR/.xinitrc.bycall"
  strip_block "$HOME_DIR/.bash_profile"
  [ -f "$GDM" ] && sed -i -E 's/^AutomaticLoginEnable *=.*/AutomaticLoginEnable=false/' "$GDM"
  systemctl daemon-reload
  echo "已取消开机自动显示大屏(防休眠设置保留,主机不应休眠)。sudo reboot 生效。"
  exit 0
fi

# 大屏地址:用主机的局域网 IP(端口只绑定在这个 IP 上,127.0.0.1 打不开)+ 大屏密钥
[ -f "$HERE/.env" ] || { echo "没有找到 $HERE/.env,请先运行 deploy/setup-host.sh 装好主机程序"; exit 1; }
val() { grep -E "^$1=" "$HERE/.env" | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*//; s/[[:space:]]*$//'; }
IP="$(val BIND_IP)"; KEY="$(val BYCALL_API_KEY)"
[ -n "$IP" ] && [ -n "$KEY" ] || { echo ".env 里缺 BIND_IP 或 BYCALL_API_KEY"; exit 1; }
URL="http://$IP:8100/station?sound=1&key=$KEY"

echo "== 1/4 主机永不休眠(否则闲置后整套呼叫系统会停)"
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null

echo "== 2/4 安装浏览器(Chromium)"
export DEBIAN_FRONTEND=noninteractive
if [ -z "$DESKTOP" ]; then
  apt-get update -y
  apt-get install -y --no-install-recommends xserver-xorg xinit x11-xserver-utils openbox fonts-noto-cjk
fi
snap list chromium >/dev/null 2>&1 || snap install chromium

echo "== 3/4 全屏大屏启动脚本"
mkdir -p "$(dirname "$LAUNCH")"
cat > "$LAUNCH" <<EOF
#!/bin/bash
# 伴月轩护士站大屏(由 setup-kiosk.sh 生成;里面有大屏密钥,不要外传)
if command -v gsettings >/dev/null; then        # 桌面版:不黑屏、不锁屏、不自动休眠
  gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null
  gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null
  gsettings set org.gnome.desktop.screensaver idle-activation-enabled false 2>/dev/null
  gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' 2>/dev/null
  gsettings set org.gnome.desktop.notifications show-banners false 2>/dev/null
fi
command -v xset >/dev/null && [ -n "\${DISPLAY:-}" ] && { xset s off; xset s noblank; xset -dpms; } 2>/dev/null
sleep 5                                          # 等桌面和网络就绪
while true; do                                   # 浏览器意外退出 3 秒后自动重开
  /snap/bin/chromium --kiosk --noerrdialogs --disable-infobars --no-first-run --password-store=basic \\
    --disable-session-crashed-bubble --overscroll-history-navigation=0 --check-for-update-interval=31536000 \\
    --autoplay-policy=no-user-gesture-required "$URL"
  sleep 3
done
EOF
chmod 700 "$LAUNCH"

echo "== 4/4 开机自动登录 $USER_NAME 并打开大屏"
if [ -n "$DESKTOP" ]; then
  [ -f "$GDM" ] || printf '[daemon]\n' > "$GDM"
  python3 - "$GDM" "$USER_NAME" <<'PY'
import re, sys
p, user = sys.argv[1:3]
s = open(p, encoding='utf-8').read()
if '[daemon]' not in s:
    s = '[daemon]\n' + s
s = re.sub(r'(?m)^\s*#?\s*AutomaticLoginEnable\s*=.*\n?', '', s)
s = re.sub(r'(?m)^\s*#?\s*AutomaticLogin\s*=.*\n?', '', s)
s = s.replace('[daemon]', f'[daemon]\nAutomaticLoginEnable=true\nAutomaticLogin={user}', 1)
open(p, 'w', encoding='utf-8').write(s)
PY
  mkdir -p "$(dirname "$AUTOSTART")"
  cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=伴月轩护士站大屏
Exec=$LAUNCH
X-GNOME-Autostart-enabled=true
EOF
else
  mkdir -p "$(dirname "$OVR")"
  printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin %s --noclear %%I $TERM\n' "$USER_NAME" > "$OVR"
  printf '#!/bin/sh\nopenbox &\nexec %s\n' "$LAUNCH" > "$HOME_DIR/.xinitrc.bycall"
  chmod 700 "$HOME_DIR/.xinitrc.bycall"
  touch "$HOME_DIR/.bash_profile"; strip_block "$HOME_DIR/.bash_profile"
  cat >> "$HOME_DIR/.bash_profile" <<EOF
$MARK_BEGIN
if [ -z "\${DISPLAY:-}" ] && [ "\$(tty)" = /dev/tty1 ]; then exec startx "\$HOME/.xinitrc.bycall"; fi
$MARK_END
EOF
  grep -q '^allowed_users=anybody' /etc/X11/Xwrapper.config 2>/dev/null || printf 'allowed_users=anybody\nneeds_root_rights=yes\n' > /etc/X11/Xwrapper.config
  systemctl daemon-reload
fi
chown -R "$USER_NAME:" "$HOME_DIR/.local" "$HOME_DIR/.config" 2>/dev/null || true
[ -f "$HOME_DIR/.bash_profile" ] && chown "$USER_NAME:" "$HOME_DIR/.bash_profile"
[ -f "$HOME_DIR/.xinitrc.bycall" ] && chown "$USER_NAME:" "$HOME_DIR/.xinitrc.bycall"

echo
echo "完成($([ -n "$DESKTOP" ] && echo 桌面版 || echo 服务器版))。执行 sudo reboot,开机后会自动登录并全屏打开大屏(声音提醒自动开启)。"
echo "取消:sudo bash deploy/setup-kiosk.sh --remove"
