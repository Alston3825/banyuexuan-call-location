# 安卓端(房间平板 / 护士手表)

工具:Android Studio 2026.2、Gradle 9.9.0、AGP 9.5.1、compileSdk 37、minSdk 26。三个模块:`common`(协议、MQTT、HTTP、设备配置)、`tablet`(房间平板)、`watch`(护士手表)。

编译(命令行):
```sh
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
./gradlew :tablet:assembleDebug
```
模拟器联调:模拟器里主机地址是 `10.0.2.2`。本机 8000 端口已被别的程序占用,呼叫主机 API 用 8100(环境变量 `BYCALL_PORT`)。
平板上长按左上角标题可打开设备设置(主机地址、门店、设备号、房间、`mqtt_password` 等)。MQTT 账号固定为设备号,`mqtt_password` 留空则匿名连接(仅开发用,上线必须填,见主机 README)。

## 平板端(tablet)
- 按钮由主机下发:通用按钮 = 管理系统服务项字典;另有每位客户最多 2 个青绿色专属按钮;紧急固定最后、需长按。收到的列表缓存在本机,主机/网络不通或重启后仍用上次的,从没收到过则用内置默认 6 个。心跳带 `cfg_ver`,列表变了 30 秒内自动更新。
- 按住说话留言(麦克风只在按住时打开);未连接时呼叫排队,恢复后补发(主机按 msg_id 去重,此项未实测)。

## 手表端(watch)
- 前台服务 `WatchService`:MQTT 常连、呼叫提醒(振动 + 提示音 + 通知,未接单每 15 秒再提醒,最多 8 次)、蓝牙信标后台扫描(低功耗模式,默认每 10 秒上报一次)、每 30 秒心跳带电量、每 5 分钟采一次心率(有传感器且授权才采)。
- 界面:待接单 → 接单 → 我已到场(信标自动判定时无需点)→ 完成;可听语音留言。长按顶部状态可改设备设置。
- 信标编号约定:手表上报 `<major>-<minor>`(例如 `1601-1`),管理系统里信标的设备编号必须用同样的格式;`beacon_uuid` 设置后只认本店 UUID。
- 模拟器没有蓝牙:用 `adb shell am broadcast -a cn.banyuexuan.call.watch.FAKE_BEACON -p cn.banyuexuan.call.watch --es ids "bcn-1601:-52"` 注入假信标(仅调试用,上线前删除 DebugReceiver)。
- 主机在手表重新连上时,会把它没处理完的呼叫补发一遍,所以手表重启或断网恢复不丢呼叫。
- 低电量:电量 <20% 且未充电时,振动 + 通知 + 顶部红色提示条,每 30 分钟再提醒;主机同步在护士站大屏提醒。心跳带 `charging`。电量用系统电量广播读取。
- 目前保存设置时手表端设置项含 `mqtt_password`(同平板)。
