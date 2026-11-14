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
        /** 设置页每项的中文名和填写提示(显示在输入框上方,避免填错项)。 */
        private val LABELS = mapOf(
            "mqtt_host" to ("主机地址" to "主机的 IP,例如 192.168.1.106"),
            "mqtt_port" to ("主机端口" to "一般是 1883"),
            "store_id" to ("门店号" to "例如 gx"),
            "device_id" to ("设备号" to "平板如 tab-1601;手表如 w-01(注意短横线)"),
            "room_id" to ("房间号" to "例如 1601"),
            "room_label" to ("房间显示名" to "屏幕左上角显示的房号"),
            "floor" to ("楼层" to "例如 16"),
            "api_base" to ("接口地址" to "例如 http://192.168.1.106:8100"),
            "api_key" to ("接口密钥" to "主机的 API Key"),
            "mqtt_password" to ("MQTT 密码" to "开发测试留空;正式使用填设备密码"),
            "beacon_uuid" to ("信标 UUID" to "本店信标统一的 UUID;留空=不限制"),
            "report_seconds" to ("位置上报间隔(秒)" to "例如 10"))
        fun labelOf(key: String): String = LABELS[key]?.let { "${it.first}($key)" } ?: key
        fun hintOf(key: String): String = LABELS[key]?.second ?: ""
    }
}
