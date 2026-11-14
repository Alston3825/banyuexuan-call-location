#!/usr/bin/env bash
# 伴月轩呼叫主机一次性初始化脚本(Ubuntu Server 24.04 LTS,x86_64,如 N100 迷你主机)
# 用法: sudo bash deploy/install-ubuntu.sh --lan 192.168.1.0/24
#   --lan  店内局域网网段:SSH(22)等主机自身服务只对该网段开放(MQTT/大屏端口见脚本末尾注意事项)
# 可重复运行。需要联网(安装软件包);装完后主机可断外网运行。
set -euo pipefail

LAN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --lan) LAN="${2:-}"; shift 2 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "请用 sudo 运行"; exit 1; }
[ -n "$LAN" ] || { echo "请用 --lan 指定局域网网段,例如 --lan 192.168.1.0/24"; exit 2; }
echo "$LAN" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$' || { echo "网段格式不对: $LAN"; exit 2; }
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || { echo "本脚本只适用于 Ubuntu(当前: ${ID:-未知})"; exit 1; }
[ "$(uname -m)" = "x86_64" ] || echo "警告: 不是 x86_64 架构($(uname -m)),镜像需要在本机构建"

echo "== 1/6 时区(门店时间 UTC+8)"
timedatectl set-timezone Asia/Shanghai

echo "== 2/6 安装软件包"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y docker.io docker-compose-v2 chrony ufw smartmontools curl

echo "== 3/6 Docker 与时间同步开机自启"
systemctl enable --now docker
systemctl enable --now chrony

echo "== 4/6 硬盘健康监测(SMART)"
systemctl enable --now smartmontools 2>/dev/null || systemctl enable --now smartd 2>/dev/null || true

echo "== 5/6 防火墙:只放行局域网($LAN)"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow from "$LAN" to any port 22 proto tcp
ufw allow from "$LAN" to any port 1883 proto tcp
ufw allow from "$LAN" to any port 8100 proto tcp
ufw --force enable
# 注意:Docker 发布的端口(1883/8100)会绕过 ufw,所以上面这两条对它们不起作用;真正的限制靠 .env 里的 BIND_IP
# (端口只绑定在局域网网卡上)和路由器不对外网映射。ufw 在这里主要保护 SSH 和主机本身。

echo "== 6/6 日志体积限制(系统日志最多 500MB)"
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=500M\n' > /etc/systemd/journald.conf.d/bycall.conf
systemctl restart systemd-journald

echo
echo "完成。下一步(见《主机部署手册》):"
echo "  1) 把 host/ 目录放到 /opt/bycall/host,cd 进去"
echo "  2) 挂载备份盘、生成 MQTT 账号、写 .env,然后 docker compose up -d --build"
