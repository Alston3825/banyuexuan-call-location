package cn.banyuexuan.call.common

import org.json.JSONArray
import org.json.JSONObject
import java.net.URI

/** Durable version + application receipt, with rollback when a new connection cannot reach this host. */
class RemoteConfig(private val cfg: DeviceConfig, private val kind: String,
                   private val changed: (Boolean) -> Unit, private val heartbeat: () -> Unit) {
    private val network = setOf("mqtt_host", "mqtt_port", "api_base")
    private val allowed = network + if (kind == "watch") setOf("heartbeat_s", "report_seconds", "beacon_uuid",
        "scan_on_s", "scan_period_s", "wifi_lock", "cpu_lock", "help_hold_ms", "voice_max_s", "quick_replies")
        else setOf("room_label", "heartbeat_s", "voice_enabled")
    private val ranges = mapOf("mqtt_port" to (1..65535), "heartbeat_s" to (10..30), "report_seconds" to (7..60),
        "scan_on_s" to (1..60), "scan_period_s" to (7..60), "help_hold_ms" to (500..5000), "voice_max_s" to (5..30))
    @Volatile private var verifying = false
    private val revision get() = cfg["_terminal_revision"].toLongOrNull() ?: 0L
    private val result get() = cfg["_terminal_result"].ifBlank { "applied" }

    fun decorate(p: JSONObject): JSONObject = p.put("terminal_config_v", 1)
        .put("terminal_revision", revision).put("terminal_result", result)

    fun settings(): JSONObject = try { JSONObject(cfg["_terminal_settings"].ifBlank { "{}" }) } catch (_: Exception) { JSONObject() }

    @Synchronized fun receive(p: JSONObject) {
        if (p.optString("kind") != kind || p.optString("device") != cfg.deviceId) return
        val v = p.optLong("revision", 0)
        if (v < revision) return
        if (v == revision) { heartbeat(); return }
        if (v <= 0) return
        val s = p.optJSONObject("settings") ?: return
        val old = JSONObject()
        try {
            val values = mutableMapOf<String, String>()
            for (k in s.keys()) {
                require(k in allowed)
                val value = s.get(k)
                when {
                    k in ranges -> require(value is Number && value.toDouble() == value.toInt().toDouble() && value.toInt() in ranges.getValue(k))
                    k in setOf("wifi_lock", "cpu_lock", "voice_enabled") -> require(value is Boolean)
                    k == "quick_replies" -> {
                        require(value is JSONArray && value.length() in 1..12)
                        for (i in 0 until value.length()) require(value.get(i) is String && value.getString(i).length in 1..30)
                    }
                    else -> require(value is String && value.length <= 160 && value.none { it.code < 32 })
                }
                if (k == "mqtt_host") require(value.toString().matches(Regex("[A-Za-z0-9][A-Za-z0-9.-]{0,252}")))
                if (k == "api_base") {
                    val u = URI(value.toString())
                    require(u.scheme in setOf("http", "https") && u.host != null && u.userInfo == null && u.query == null && u.fragment == null && (u.path.isNullOrEmpty() || u.path == "/"))
                }
                if (k == "beacon_uuid") require(value.toString().isEmpty() || value.toString().matches(Regex("[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}")))
                old.put(k, cfg[k])
                values[k] = value.toString()
            }
            require(s.optInt("scan_on_s", 2) <= s.optInt("scan_period_s", 10))
            val net = network.any { s.has(it) && s.get(it).toString() != cfg[it] }
            old.put("_terminal_settings", cfg["_terminal_settings"])
            values["_terminal_previous"] = old.toString()
            values["_terminal_settings"] = s.toString()
            values["_terminal_revision"] = v.toString()
            values["_terminal_result"] = if (net) "applying" else "applied"
            values["_terminal_deadline"] = if (net) (System.currentTimeMillis() + 90_000).toString() else "0"
            check(cfg.commit(values))
            try { changed(net) } catch (_: Exception) { rollback(); return }
            if (!net) cfg.commit(mapOf("_terminal_previous" to ""))
            heartbeat()
        } catch (_: Exception) {
            // The desired revision is recorded as failed, without partially writing any parameter.
            cfg.commit(mapOf("_terminal_revision" to v.toString(), "_terminal_result" to "failed"))
            heartbeat()
        }
    }

    @Synchronized fun confirm(p: JSONObject) {
        val v = revision
        if (result != "applying" || p.optLong("revision") != v || verifying) return
        verifying = true
        // MQTT confirmation proves the new broker path; also test HTTP for voice/API operations.
        Thread {
            var ok = false
            try {
                val url = java.net.URL(cfg.apiBase + "/health")
                val c = url.openConnection() as java.net.HttpURLConnection
                try {
                    c.connectTimeout = 3000; c.readTimeout = 3000; c.instanceFollowRedirects = false
                    if (c.responseCode == 200) {
                        val body = c.inputStream.bufferedReader().use { it.readText() }
                        ok = JSONObject(body).optString("store_id") == cfg.store
                    }
                } finally { c.disconnect() }
            } catch (_: Exception) { }
            synchronized(this) {
                verifying = false
                if (ok && revision == v && result == "applying") {
                    if (cfg.commit(mapOf("_terminal_result" to "applied", "_terminal_previous" to "", "_terminal_deadline" to "0"))) heartbeat()
                }
            }
        }.start()
    }

    @Synchronized fun tick() {
        if (result == "applying" && System.currentTimeMillis() >= (cfg["_terminal_deadline"].toLongOrNull() ?: 0L)) rollback()
    }

    private fun rollback() {
        val old = try { JSONObject(cfg["_terminal_previous"].ifBlank { "{}" }) } catch (_: Exception) { JSONObject() }
        val values = mutableMapOf("_terminal_result" to "failed", "_terminal_previous" to "", "_terminal_deadline" to "0")
        for (k in old.keys()) values[k] = old.getString(k)
        if (cfg.commit(values)) { changed(true); heartbeat() }
    }
}
