# 用电脑 + Docker 临时当主机(办公室测试)

> **2026-11-12 起改用 Windows PC(网线 → 交换机 → TP-Link 路由器)**,见文末“Windows PC 版”。下面的步骤 1–9 以 Mac 为例,原理相同。

N100 主机到位前,用你的 Mac 跑同一套主机程序(Docker),让手表/平板在办公室真实联调。**不需要在 Docker 里装 Ubuntu。**

> 状态:镜像构建、容器启动、备份、网页都已在 Mac 上验证过;**真实手表/平板连这台 Mac 的整条链路还没在办公室走过**,第一次请边做边核对。

## 0. 先决条件
- Mac 已装并打开 **Docker Desktop**(右上角鲸鱼图标显示 running)。
- Mac、手表、平板连**同一个 Wi‑Fi**(不要用访客网络,访客网络常隔离设备)。

## 1. 固定 Mac 的 IP
设备都靠这个 IP 找主机,**不能变**。
1. 查当前 IP:终端运行 `ipconfig getifaddr en0`(用有线则查对应网口;你当前是 `192.168.1.103`,仅供参考)。
2. 在**路由器**里按 Mac 的 MAC 地址做"静态 IP / DHCP 保留",把它固定下来(最稳)。
3. 记下这个 IP,后面叫它 `Mac的IP`。

## 2. Mac 不能休眠
系统设置 → 锁定屏幕 / 电池 → 关闭"显示器关闭时自动休眠"、接电源时"防止自动休眠"。或临时在终端运行 `caffeinate -d`(窗口开着期间不休眠)。合盖会断,务必不合盖(或接外接显示器)。

## 3. 防火墙
系统设置 → 网络 → 防火墙:若已开启,要允许 Docker 接收连接(弹窗点"允许")。需要通的端口:**1883**(MQTT)和 **8100**(网页/接口)。

## 4. 启动
```sh
cd "…/伴月轩智能呼叫定位系统/host"
docker compose up -d --build        # 第一次要下载镜像,几分钟
docker compose ps                   # host 显示 healthy,mosquitto 为 Up
curl http://localhost:8100/health   # 返回 {"ok":true,...}
```
- 现在是**开发模式**:MQTT 匿名可连,API 密钥是 `dev-key`。**只在办公室内网测试用,不要把 Mac 暴露到外网。**
- 默认加载的是**虚构演示数据**(门店 `gx`、房间 1601/1602、平板 `tab-1601`/`tab-1602`、护士手表 `w-01`~`w-04`)。办公室信标测试的专用配置**尚未生成**(你说先不生成),要测真实布点时再告诉我。

## 5. 验证网页(用另一台设备)
手机/另一台电脑(同 Wi‑Fi)浏览器打开:
- 大屏:`http://Mac的IP:8100/station?key=dev-key`
- 备份管理:`http://Mac的IP:8100/backup?key=dev-key`
能打开说明网络、防火墙都通。打不开先查第 3 步和是否同一 Wi‑Fi。

## 6. 平板/手表里填什么
设置里填(开发模式不需要 MQTT 密码):

| 项 | 值 |
|---|---|
| 主机地址 | `Mac的IP`(**不是 localhost**) |
| MQTT 端口 | 1883 |
| 门店号 | `gx`(演示数据) |
| 设备号 | 平板 `tab-1601`;手表 `w-01` |
| api_base | `http://Mac的IP:8100` |
| api_key | `dev-key` |
| beacon_uuid(手表) | 你的信标统一 UUID |

测试前手表里的假信标接收器 `DebugReceiver` 要先删掉(清单 R1),否则会干扰真实信标定位。

## 7. 日常操作
| 做什么 | 命令 |
|---|---|
| 看日志 | `docker compose logs --tail 100 host` |
| 重启 | `docker compose restart` |
| 停止 | `docker compose down`(数据在 `host/data/`,不会丢) |
| 看设备在线 | 大屏上看,或 `curl -H "x-api-key: dev-key" http://localhost:8100/devices` |
| 实时看位置/心率 | `python3 tools/watch_monitor.py`(参数见 `--help`,`--csv` 可导出) |

## 8. 这台"主机"测不了的
- Ubuntu 安装、`install-ubuntu.sh`、BIOS 来电自启、真实断电恢复。
- N100 的 CPU/内存/温度(清单 N10)。
- 镜像是 arm64(Mac),N100 是 x86,**到 N100 上要重新构建**,不能直接拷镜像。
- Mac 的性能和网络不代表 N100;这里只验证功能和联调。

## 9. 出问题先查
1. 手表/平板"未连接":IP 是否填 `Mac的IP`;同一 Wi‑Fi;Mac 是否休眠;防火墙。
2. Mac IP 变了:路由器没做固定,所有设备要重填。
3. 大屏打不开:`docker compose ps`、`docker compose logs host`。


---

# Windows PC 版(网线 → 交换机 → TP-Link 路由器)

有线比 Mac 的 Wi‑Fi 更稳,且 PC 是 x86 架构,与 N100 相同,在这台 PC 上构建的镜像更接近上线环境。

> 状态:以下 Windows 步骤**尚未在你的 PC 上走过**;主机程序本身的 Docker 运行已在 Mac 上验证过。

1. **拷文件**:把整个 `host/` 文件夹拷到 PC,例如 `D:\bycall\host`(不要放在有中文/空格的深层路径里)。
2. **打开 Docker Desktop**,等它显示 running。
3. **固定 PC 的 IP(TP-Link 路由器)**:PowerShell 运行 `ipconfig`,记下"以太网"的 IPv4 地址和物理地址(MAC)。登录路由器管理页(通常 `tplogin.cn` 或 `192.168.0.1`)→ 高级设置 → DHCP 服务器 → **静态地址分配**,把这个 MAC 绑定到该 IP。设备经交换机上网不影响,同一网段即可。
4. **不让 PC 休眠**:设置 → 系统 → 电源 → 屏幕和睡眠,"接通电源后进入睡眠"设为**从不**。
5. **防火墙放行端口**(管理员 PowerShell):
   ```
   New-NetFirewallRule -DisplayName "bycall" -Direction Inbound -Protocol TCP -LocalPort 1883,8100 -Action Allow -Profile Private,Domain
   ```
   若网络被识别为"公用网络",在设置里改成"专用网络",或把 `Private,Domain` 改成 `Any`。
6. **启动**(PowerShell,在 `host` 目录):
   ```
   docker compose up -d --build
   docker compose ps
   curl.exe http://localhost:8100/health
   ```
7. **验证**:用手机(连 TP-Link 的 Wi‑Fi)打开 `http://PC的IP:8100/station?key=dev-key`。
8. 手表/平板里填的内容同上表,**主机地址填 PC 的 IP**。
9. Windows 上命令里的 `python3` 改为 `python`(需另装 Python 才能跑 `tools/watch_monitor.py`)。

**注意**:路由器如果开了"AP 隔离/客户端隔离",无线设备访问不到有线的 PC,要在路由器里关掉。

## PC 无法联网构建时(Docker Hub 连不上)的离线方案
现象:`docker compose up --build` 报 `failed to fetch oauth token ... auth.docker.io ... connectex`。
做法:在 Mac 上构建 x86 镜像并导出到 `dist/bycall-host-amd64.tar.gz`(已提供),拷到 PC 后:
```
docker load -i bycall-host-amd64.tar.gz
docker compose up -d          # 注意:不要加 --build
```
`docker-compose.yml` 已给 host 服务指定 `image: bycall-host:latest`。改了程序代码后需要重新导出镜像再导入。


---

# 在线更新(以后改了代码,设备上点一下就更新)

**一次性准备**:每台平板/手表手动装一次 `dist/bycall-tablet.apk`、`dist/bycall-watch.apk`(这一版开始带更新功能),第一次点更新时系统会让你允许本应用"安装未知应用",允许即可。

**以后每次改动后**(在 Mac 上,项目根目录):
```
./publish.sh 192.168.1.106 "这次改了什么"
```
脚本会编译平板和手表 APK,并上传到主机(IP 换成你的主机,默认密钥 `dev-key`,正式环境用 `KEY=xxx ./publish.sh ...`)。约 1–2 分钟。

**设备上**:
- 手表:打开应用,顶部出现蓝色"发现新版本 0.x · 点击更新",点一下 → 自动下载 → 系统弹"Update" → 点确认 → 自动重启并重新连上。
- 平板:顶部出现橙色条,**工作人员长按**该条 → 确认 → 系统弹"Update" → 点确认。(住客随手点不会触发。)
- 设备每 10 分钟检查一次,打开应用时也会检查。

**注意**
- 必须在**同一台 Mac** 上编译(签名一致),换电脑编译的包装不上。
- 版本只升不降;主机重启不丢(存在 `data/host/apk/`)。
- 更新时手表/平板会短暂不可用,避开有呼叫的时段。

## 更新主机程序(Windows PC)
1. 把 `dist/bycall-host-amd64.tar.gz` 和 `update-host.bat` 复制到 PC 上**原来放 docker-compose.yml 的 host 文件夹里**(必须同一个文件夹)。
2. 双击 `update-host.bat`(窗口会一直停着,不会自己关闭;同时在同一文件夹写出 `update-log.txt`,失败时把它发给开发者)。成功时最后会显示 `{"ok":true,...,"version":"20261113-1628"}`。版本号与 Mac 上 `build_host.sh` 结尾显示的一致,即更新成功;没有 version 字段说明仍是旧版。
3. 护士站大屏右上角也会显示"主机 v版本号"。

## 留言转文字(一次性准备)
护士站大屏的"转文字"需要语音识别模型(约 480MB),**只需放一次**:
1. 把 Mac 上的 `host/models` 整个文件夹复制到 PC 的 host 文件夹里(与 docker-compose.yml 同目录),最终路径是 `host\models\whisper-small\model.bin`。
2. 按上面的步骤更新主机程序(update-host.bat)。
3. 大屏上点某条留言的"📝 转文字",几秒后显示文字。没复制模型会提示"未安装语音识别模型"。


## 用网页更新主机(推荐,首次需手动升级一次)
1. **首次**:把 `dist/` 里的 `bycall-host-amd64.tar.gz`、`update-host.bat`、**`docker-compose.yml`(覆盖旧的)** 放进主机的 host 文件夹,双击 `update-host.bat`。完成后主机多了一个 updater 服务。
2. 以后:在任意电脑浏览器打开 `http://主机IP:8200`,输入密钥,选择新的 `bycall-host-amd64.tar.gz` 上传,等待页面显示"更新成功,主机版本 xxx"。失败会自动换回旧版本并说明。
3. 语音识别模型:在同一页面选择 `dist/bycall-models.tar.gz`(约 430MB)上传安装,一次即可;也可以手动把 `models/whisper-small/` 复制到 host 文件夹。
4. 页面里的"主机版本"应与 Mac 上 `build_host.sh` 结尾显示的版本一致。
