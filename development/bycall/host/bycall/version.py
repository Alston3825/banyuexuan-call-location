"""主机版本号:镜像打包时由 build_host.sh 写入 bycall/VERSION(日期时间),没有则显示 dev。"""
import os

_p = os.path.join(os.path.dirname(__file__), 'VERSION')
VERSION = open(_p, encoding='utf-8').read().strip() if os.path.exists(_p) else 'dev'
