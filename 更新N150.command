#!/bin/bash
cd "$(dirname "$0")" || exit 1
python3 host/deploy/deploy_n150.py menu
status=$?
echo
read -r -p "按回车关闭窗口…" _reply
exit "$status"
