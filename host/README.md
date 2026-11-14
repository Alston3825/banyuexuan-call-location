# 主机服务(host)

依据《伴月轩智能呼叫定位系统 — 软件开发规划文档》。目录按规划分层:`bycall/core`(核心引擎,可授权)与 `bycall/custom/banyuexuan`(伴月轩定制配置)分离。

## 已实现
- `core/protocol.py`:消息信封与各类负载(规划 §四),主题构造函数。
- `core/workflow.py`:呼叫状态机,SQLite 事务,去重、抢单互斥、转交、取消;每次变更在同一事务内写事件(对接管理系统的出站事件源)。
- `core/dispatcher.py`:按班次/责任护士/同组/组长/护士站逐级分派,在线状态决定可用性。
- `core/locator.py`:信标 RSSI 滑动平均 + 滞回,房间级定位。
- `core/telemetry.py`:设备在线与电量、位置轨迹、心率等体征(只存本机)。
- `core/service.py`:协议处理。房间发呼叫 → 立即回执“已接到呼叫,请稍后” → 分派 → 接单回执 → 到场(信标自动判定或手动)→ 完成。
- `core/voice.py`:语音留言存取,超期自动清理(默认 30 天)。
- `api/app.py`:本机 HTTP 接口(呼叫、事件增量拉取、设备、护士位置、体征、语音上传下载),支持 API Key。
- `gateway/bus.py`:内存总线(测试/模拟)与 MQTT 适配器。
- `core/guest_buttons.py`:客户专属按钮(每位在住客户最多 1 个，最多 8 字,跟本次入住走)。平板的按钮列表由主机下发(`DEVICE_CONFIG`)。
- `core/backup.py` + `tools/backup.py`:SQLite 备份(在线备份+完整性校验+压缩)与命令行恢复。
- `core/backupmgr.py` + `core/cloud.py` + `api/backup.html`:**定时备份(默认每天 03/09/15/21 点)+ 加密上传 S3 兼容云存储 + 失败重试 + 管理页面 `http://主机IP:8100/backup`(设置时段/保留天数、立即备份、从本机或云端恢复)**。恢复=校验→暂存→重启主机进程时换入,旧库改名保留。云备份先加密后上传,口令不设则拒绝上传。
- `core/appupdate.py` + `tools/publish_apk.py`:平板/手表 APK 在线更新(主机保存最新 APK,设备询问并一键更新;发布用项目根目录的 `publish.sh`)。
- `core/hoststatus.py`:主机提醒——备份失败/过期、磁盘不足、MQTT 断开、管理系统同步/写回失败、后台任务异常、平板/手表失联、手表低电量。
- `integration/`:对接伴月轩管理系统(主数据同步、呼叫写回),`MGMT_URL` 等环境变量打开;目前只用模拟管理系统验证。
- 护士站大屏:浏览器打开 `http://主机IP:8100/station?key=API密钥`(页面 `api/station.html`),看呼叫、护士位置/电量、平板在线、主机提醒;可在大屏上设置客户专属按钮。
- 现场测试工具:`tools/watch_monitor.py`(实时监看手表位置/心跳/心率并标出中断,可导出 CSV 画电量曲线)、`tools/send_calls.py`(模拟平板连发呼叫)。用法见 `docs/08-样机测试清单.md`。
- MQTT 账号与权限:`tools/gen_mqtt_auth.py` 生成每设备一个账号 + 主题 ACL,`tools/mqtt_acl_check.py` 对真实 Mosquitto 复核。
- 配置驱动:`custom/banyuexuan/demo_pilot.json` 为虚构的 2 房演示数据,不是真实房间台账。

## 运行测试
```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/python -m pytest -q tests
```
真实 MQTT 冒烟(需要本机有 broker):`python tools/mqtt_smoke.py <端口>`。

> 正式部署在 N100 迷你主机(Ubuntu Server 24.04)上,**完整步骤见 `docs/09-主机部署手册.md`**(BIOS、系统安装、固定 IP、一键初始化脚本 `deploy/install-ubuntu.sh`、云备份配置、启动验证、运维、断电处理)。

## 用 Docker 启动
开发(匿名 MQTT):`docker compose up -d --build`。HTTP 对外端口 8100(容器内 8000)。

上线(必做):
1. 生成账号文件(**主机上只需装 Docker,不用装 Python**;`credentials.json` 另存好,丢了所有设备要重配密码):
   `docker compose run --rm -T -v "$PWD/mosquitto/auth:/out" host python tools/gen_mqtt_auth.py --config /app/bycall/custom/banyuexuan/<门店>.json --out /out`
2. `.env` 设:`MQ_CONF=mosquitto.prod.conf`、`MQTT_USER=host`、`MQTT_PASSWORD=<credentials.json 里 host 的密码>`、`BYCALL_API_KEY=<自定>`、云备份 `CLOUD_*` 与 `CLOUD_PASSPHRASE`(见 `.env.example`;本机备份默认与数据库同盘,防不了坏盘,所以云备份是主要的异地副本)
3. 每台平板/手表在设置里填对应密码(账号即设备号);
4. `docker compose run --rm -T --network host -v "$PWD/mosquitto/auth:/out:ro" host python tools/mqtt_acl_check.py 127.0.0.1 /out/credentials.json`(在主机上跑;Linux 主机可用 `--network host`,Docker Desktop 下改用主机 IP)
5. 备份恢复:优先用管理页 `/backup`;命令行方式:`docker compose stop host`,再 `docker compose run --rm -T host python tools/backup.py restore --db /data/host.db --dest /backups --yes`,然后 `docker compose start host`。手动备份:`docker compose exec -T host python tools/backup.py backup --db /data/host.db --dest /backups`。

可调环境变量:`BYCALL_BACKUP_KEEP_DAYS`(首次默认本机保留 7 天;备份时段在管理页设置)、`BYCALL_OFFLINE_AFTER_S`(180)。
