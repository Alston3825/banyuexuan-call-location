#!/usr/bin/env bash
# 伴月轩呼叫主机 一键安装/更新配置(Ubuntu Server 24.04,N150 等 x86_64 主机)
#
# 用法(在 host 目录里运行,推荐放在 /opt/bycall/host):
#   sudo bash deploy/setup-host.sh --lan 192.168.1.0/24
#   sudo bash deploy/setup-host.sh --lan 192.168.1.0/24 --ip 192.168.1.108
#   sudo bash deploy/setup-host.sh --lan 192.168.1.0/24 --image ../dist/bycall-host-amd64.tar.gz   # 主机不能联网时
#
# 做的事(可重复运行,已有的 .env / 设备密码不会被覆盖):
#   1) 系统还没初始化(没有 Docker)时,先运行 deploy/install-ubuntu.sh(需要联网)
#   2) 选门店配置:有 config/store.json 用它,否则用演示配置(会提醒)
#   3) 生成每台设备的 MQTT 账号(mosquitto/auth/),已有设备的密码保持不变
#   4) 生成/补全 .env:主机 IP、随机大屏密钥、host 的 MQTT 密码、备份位置
#   5) 构建或导入主机镜像,启动全部服务,等到健康检查通过
#   6) 打印大屏/备份/更新页面地址,并把这些信息写进 安装信息.txt(仅 root 可读)
set -euo pipefail

LAN="" IP="" IMAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --lan) LAN="${2:-}"; shift 2 ;;
    --ip) IP="${2:-}"; shift 2 ;;
    --image) IMAGE="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "未知参数: $1(用 --help 查看用法)"; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
DRY="${BYCALL_SETUP_DRY_RUN:-}"     # 测试用:设为 1 时只生成配置,不动 Docker/系统

say()  { printf '\n\033[1;32m== %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*"; exit 1; }

[ -n "$DRY" ] || [ "$(id -u)" -eq 0 ] || die "请用 sudo 运行:sudo bash deploy/setup-host.sh --lan 192.168.1.0/24"
[ -f docker-compose.yml ] && [ -d bycall ] || die "请在 host 目录里运行(当前 $HERE 里没有 docker-compose.yml / bycall)"
command -v python3 >/dev/null || die "缺少 python3(Ubuntu 24.04 默认自带,如被删除请 apt install python3)"

# ---------- 1. 系统初始化 ----------
if [ -z "$DRY" ] && ! command -v docker >/dev/null; then
  [ -n "$LAN" ] || die "第一次安装需要 --lan 指定店内网段,例如 --lan 192.168.1.0/24"
  say "1/6 第一次安装:初始化系统(时区、Docker、防火墙、日志),需要联网"
  bash deploy/install-ubuntu.sh --lan "$LAN"
else
  say "1/6 系统已初始化(已安装 Docker),跳过"
fi

# ---------- 2. 门店配置 ----------
say "2/6 门店配置"
mkdir -p config data/host data/mosquitto data/updater models mosquitto/auth
if [ -f config/store.json ]; then
  CFG_LOCAL=config/store.json; CFG_IN_CONTAINER=/config/store.json
  echo "使用正式配置 config/store.json"
else
  CFG_LOCAL=bycall/custom/banyuexuan/demo_pilot.json; CFG_IN_CONTAINER=/app/bycall/custom/banyuexuan/demo_pilot.json
  warn "没有 config/store.json,先用演示配置(虚构的 2 个房间、4 块手表)。"
  warn "正式使用前把真实房间/平板/信标/护理师写进 config/store.json(格式同 $CFG_LOCAL),再运行一次本脚本。"
fi
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$CFG_LOCAL" || die "$CFG_LOCAL 不是有效的 JSON,请检查格式"

# ---------- 3. MQTT 账号 ----------
say "3/6 生成设备 MQTT 账号(已有设备的密码不变)"
python3 tools/gen_mqtt_auth.py --config "$CFG_LOCAL" --out mosquitto/auth
chmod 600 mosquitto/auth/credentials.json
chmod 644 mosquitto/auth/passwd mosquitto/auth/acl
HOST_PW="$(python3 -c "import json; print(json.load(open('mosquitto/auth/credentials.json'))['host'])")"

# ---------- 4. .env ----------
say "4/6 写配置文件 .env"
if [ -z "$IP" ]; then
  IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)"
  [ -n "$IP" ] || IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
echo "$IP" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' || die "识别不到主机 IP,请用 --ip 指定,例如 --ip 192.168.1.108"

[ -f .env ] || { cp .env.example .env; echo "已从 .env.example 新建 .env"; }
chmod 600 .env

# 只改脚本负责的几项;已经填过的值(除 host 的 MQTT 密码要和账号文件保持一致外)一律保留
python3 - "$IP" "$HOST_PW" "$CFG_IN_CONTAINER" <<'PY'
import re, secrets, sys, os
ip, host_pw, cfg = sys.argv[1:4]
lines = open('.env', encoding='utf-8').read().splitlines()

def value(line):
    v = line.split('=', 1)[1]
    return re.split(r'\s+#', v, maxsplit=1)[0].strip()

def setkey(key, new, only_if=lambda old: True):
    global lines
    for i, l in enumerate(lines):
        m = re.match(r'\s*#?\s*' + key + r'=', l)
        if m:
            commented = l.lstrip().startswith('#')
            old = '' if commented else value(l)
            if commented or only_if(old):
                lines[i] = f'{key}={new}'
                return True
            return False
    lines.append(f'{key}={new}')
    return True

placeholder = lambda old: (not old) or old in ('dev-key',) or '请改' in old
setkey('BIND_IP', ip, lambda old: (not old) or old in ('192.168.1.10', '192.168.1.108'))
setkey('BYCALL_API_KEY', secrets.token_urlsafe(24), placeholder)
setkey('BYCALL_SWAP_KEY', secrets.token_urlsafe(16), placeholder)
setkey('MQ_CONF', 'mosquitto.prod.conf')
setkey('MQTT_USER', 'host')
setkey('MQTT_PASSWORD', host_pw)                     # 始终与 credentials.json 一致
if cfg.startswith('/config/'):
    setkey('BYCALL_CONFIG', cfg)
# 备份位置:挂了第二块盘(/mnt/backup)就放那里,否则放本机 data/backups(云备份做异地副本)
bp = '/mnt/backup/bycall' if os.path.ismount('/mnt/backup') else './data/backups'
setkey('BACKUP_PATH', bp, lambda old: (not old) or (old == '/mnt/backup/bycall' and not os.path.ismount('/mnt/backup')))
open('.env', 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
PY
API_KEY="$(grep -E '^BYCALL_API_KEY=' .env | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*//; s/[[:space:]]*$//')"
SWAP_KEY="$(grep -E '^BYCALL_SWAP_KEY=' .env | tail -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*//; s/[[:space:]]*$//')"
grep -Eq '^CLOUD_ENDPOINT=[^[:space:]#]+' .env || warn "云备份还没配置(.env 里的 CLOUD_*):现在只有本机备份,防不了坏盘。正式使用前请按手册第 6 步配好。"
[ -f models/whisper-small/model.bin ] || warn "没有语音识别模型 models/whisper-small:留言转文字暂不可用,其他功能不受影响(可在 http://$IP:8200 上传模型)。"

if [ -n "$DRY" ]; then
  echo "(测试模式:到此为止,不启动 Docker)"; exit 0
fi

# ---------- 5. 镜像与启动 ----------
# 完整重建镜像时,去掉旧的“代码升级基础镜像”标记,以后的代码升级从这次的新镜像开始叠加
docker image rm bycall-host:base >/dev/null 2>&1 || true
# mosquitto 容器以 uid 1883 运行,需要能写自己的数据目录
chown -R 1883:1883 data/mosquitto 2>/dev/null || true
if [ -n "$IMAGE" ]; then
  [ -f "$IMAGE" ] || die "找不到镜像文件 $IMAGE"
  say "5/6 导入预先打包的主机镜像(约 1 分钟)"
  docker load -i "$IMAGE"
  docker image inspect eclipse-mosquitto:2 >/dev/null 2>&1 || warn "本机没有 eclipse-mosquitto:2 镜像,启动时需要联网下载一次"
  docker compose up -d
else
  say "5/6 构建主机镜像并启动(第一次需要联网,约 5~15 分钟)"
  docker compose up -d --build
fi

say "6/6 等待主机服务就绪"
ok=""
for i in $(seq 1 60); do
  st="$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q host)" 2>/dev/null || true)"
  [ "$st" = "healthy" ] && { ok=1; break; }
  sleep 5
done
docker compose ps
HEALTH="$(curl -s --max-time 5 "http://$IP:8100/health" || true)"
[ -n "$ok" ] && [ -n "$HEALTH" ] || die "主机服务 5 分钟内没有就绪。请把这条命令的输出发给开发:sudo docker compose logs --tail 200 host"
echo "健康检查:$HEALTH"

INFO="安装信息.txt"
umask 077
cat > "$INFO" <<EOF
伴月轩呼叫主机 安装信息($(date '+%F %T'))  ⚠ 内含密钥,只给护士站电脑/管理员,不要发微信
主机 IP:        $IP   (请在路由器里给这台主机做 DHCP 保留/静态绑定,IP 不能变)
大屏:           http://$IP:8100/station?key=$API_KEY
备份管理:       http://$IP:8100/backup?key=$API_KEY
换表(护士站平板):http://$IP:8100/swap?key=$SWAP_KEY
续航/送达报表:  http://$IP:8100/report?key=$API_KEY
主机更新:       http://$IP:8200
门店配置:       $CFG_LOCAL
设备密码文件:   $HERE/mosquitto/auth/credentials.json
设备设置:       主机IP=$IP  api_base=http://$IP:8100  api_key=$API_KEY  mqtt_password=该设备在 credentials.json 里的密码
EOF
cat "$INFO"
echo
echo "下一步:"
echo "  1) 护士站电脑浏览器打开上面的“大屏”地址"
echo "  2) 把 .env、mosquitto/auth/credentials.json、$INFO 拷到 U 盘另存(丢了要全部设备重配)"
echo "  3) 平板/手表“设置”里按“设备设置”一行填写;用户名=设备号(如 tab-1601、w-01)"
echo "  4) 复核 MQTT 安全(应输出 ALL OK):"
echo "     sudo docker compose run --rm -T --network host -v \"\$PWD/mosquitto/auth:/out:ro\" host python tools/mqtt_acl_check.py 127.0.0.1 /out/credentials.json"
