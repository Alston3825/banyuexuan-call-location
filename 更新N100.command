#!/bin/bash
cd "$(dirname "$0")" || exit 1
python3 host/deploy/deploy_n100.py menu
status=$?
echo
read -r -p "按回车关闭窗口…" _reply
exit "$status"
