#!/usr/bin/env bash
# 本脚本放在离线包根目录运行；只导入专用镜像和离线 Compose 覆盖文件，不启动服务。
set -euo pipefail
[[ "$EUID" -eq 0 ]] || { echo '请执行: sudo bash import-images.sh'; exit 2; }
[[ "$(uname -m)" == x86_64 ]] || { echo '此包只适用于 N150 等 x86_64 主机。'; exit 2; }
bundle="$(cd "$(dirname "$0")" && pwd)"
project="${1:-/opt/bycall/host}"
[[ -f "$project/docker-compose.yml" ]] || { echo "未找到 $project/docker-compose.yml，请先放置源码目录。"; exit 2; }
[[ -f "$bundle/images-linux-amd64.tar" && -f "$bundle/IMAGES.sha256" ]] || { echo '镜像文件或校验文件不完整，请完整解压离线包。'; exit 2; }
source_override="$bundle/host/docker-compose.override.yml"
[[ -f "$source_override" ]] || { echo '缺少离线 Compose 配置。'; exit 2; }
if [[ -f "$project/docker-compose.override.yml" ]] && ! cmp -s "$source_override" "$project/docker-compose.override.yml"; then
  echo '目标目录已有不同的 Compose 覆盖配置，暂停，避免覆盖既有设置。'; exit 2
fi
running="$(docker ps -q --filter "label=com.docker.compose.project.working_dir=$project")"
[[ -z "$running" ]] || { echo '该目录已有运行服务，请按升级流程处理；本脚本仅用于首次安装。'; exit 2; }
cd "$bundle"
sha256sum -c IMAGES.sha256
docker load -i images-linux-amd64.tar
for image in bycall-host:n100-20261116 bycall-mosquitto:n100-20261116; do
  platform="$(docker image inspect --platform linux/amd64 --format '{{.Os}}/{{.Architecture}}' "$image")"
  [[ "$platform" == linux/amd64 ]] || { echo "镜像架构检查失败: $image"; exit 1; }
done
if [[ ! -f "$project/docker-compose.override.yml" ]]; then
  cp "$source_override" "$project/docker-compose.override.yml"
fi
cd "$project"
docker compose config --quiet
echo '离线镜像已导入，配置校验通过。尚未启动服务，也未修改 .env、设备密码或业务数据库。'
echo "下一步请在 $project 配置 .env 和设备账号；不要再执行 build。"
