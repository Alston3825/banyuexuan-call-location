#!/bin/bash
# 在 Mac 上打包主机镜像(x86)并导出到 ../dist/,版本号 = 打包时间。用法:./build_host.sh
set -e
cd "$(dirname "$0")"
export DOCKER_CONFIG=${DOCKER_CONFIG:-/tmp/dcfg}
date '+%Y%m%d-%H%M' > bycall/VERSION
docker buildx build --platform linux/amd64 -t bycall-host:latest --load .
docker save bycall-host:latest | gzip > ../dist/bycall-host-amd64.tar.gz
echo "已导出 dist/bycall-host-amd64.tar.gz,版本 $(cat bycall/VERSION)"
