package cn.banyuexuan.call.common

import android.content.Context

/** Per-device settings kept in SharedPreferences. Defaults target the Android emulator (host = 10.0.2.2). */
class DeviceConfig(ctx: Context, private val defaults: Map<String, String>) {
    private val sp = ctx.applicationContext.getSharedPreferences("device", Context.MODE_PRIVATE)
    operator fun get(key: String): String = sp.getString(key, null) ?: defaults[key] ?: ""
    operator fun set(key: String, value: String) { sp.edit().putString(key, value.trim()).apply() }
    val keys get() = defaults.keys.toList()
    val mqttHost get() = this["mqtt_host"]
    val mqttPort get() = this["mqtt_port"].toIntOrNull() ?: 1883
    val store get() = this["store_id"]
    val deviceId get() = this["device_id"]
    val apiBase get() = this["api_base"].trimEnd('/')
    /** MQTT 账号固定为 device_id;密码为空则匿名连接(仅开发用)。 */
    val mqttPassword get() = this["mqtt_password"]
    val apiKey get() = this["api_key"]

    companion object {
        /** 设置页每一项:中文名、用途说明、填写示例(设置页逐项显示,对照着填不会填错)。 */
        data class Item(val name: String, val help: String, val example: String)
        val ITEMS = linkedMapOf(
            "mqtt_host" to Item("主机地址", "呼叫主机(N150)的 IP 地址;改了它,下面的“接口地址”会自动跟着改", "192.168.1.108"),
            "mqtt_port" to Item("主机端口", "消息端口,一般不用改", "1883"),
            "store_id" to Item("门店号", "和主机门店配置里的 store_id 一致", "gx"),
            "device_id" to Item("设备号", "本设备的编号,要和主机登记的一致(注意短横线)", "平板 tab-1601;手表 w-01;备用表 w-09"),
            "room_id" to Item("房间号", "这台平板所在的房间", "1601"),
            "room_label" to Item("房间显示名", "屏幕左上角显示的房号", "1601"),
            "floor" to Item("楼层", "房间所在楼层", "16"),
            "api_base" to Item("接口地址", "下载更新、上传语音用;一般是 http://主机地址:8100", "http://192.168.1.108:8100"),
            "api_key" to Item("接口密钥", "主机 安装信息.txt 里 key= 后面那串", "一串 32 位左右的字母数字"),
            "mqtt_password" to Item("设备密码", "主机上 mosquitto/auth/credentials.json 里本设备号对应的那串", "每台设备不同"),
            "beacon_uuid" to Item("信标 UUID", "本店蓝牙信标统一的 UUID;留空 = 不限制", "留空"),
            "report_seconds" to Item("位置上报间隔(秒)", "旧参数,现在由主机统一下发,一般不用改", "10"),
            "admin_pin" to Item("设置密码", "打开本设置页要输入的密码(防止客人误改);留空 = 不需要密码", "8888"))
        fun labelOf(key: String): String = ITEMS[key]?.let { "${it.name}($key)" } ?: key
        fun hintOf(key: String): String = ITEMS[key]?.let { "${it.help}  例:${it.example}" } ?: ""
    }
}
