package cn.banyuexuan.call.common

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import com.sun.net.httpserver.HttpServer
import java.net.InetSocketAddress

class RemoteConfigTest {
    private class MemoryConfig : ConfigStore {
        val values = mutableMapOf("mqtt_host" to "old-host", "api_base" to "http://127.0.0.1:1", "beacon_uuid" to "", "scan_on_s" to "2")
        var fail = false
        override fun get(key: String) = values[key] ?: ""
        override fun commit(values: Map<String, String>): Boolean { if (fail) return false; this.values.putAll(values); return true }
        override val deviceId = "w-01"
        override val apiBase get() = get("api_base")
        override val store = "test-store"
    }
    private fun command(rev: Int, s: JSONObject) = JSONObject().put("revision", rev).put("kind", "watch").put("device", "w-01").put("settings", s)
    private fun state(r: RemoteConfig) = r.decorate(JSONObject())

    @Test fun normalSettingsPersistAndDuplicateDoesNotApplyTwice() {
        val cfg = MemoryConfig(); var changed = 0
        val r = RemoteConfig(cfg, "watch", { changed++ }, {})
        val cmd = command(1, JSONObject().put("beacon_uuid", "a".repeat(32)).put("scan_on_s", 3))
        r.receive(cmd); r.receive(cmd)
        assertEquals(1, changed); assertEquals("3", cfg["scan_on_s"])
        assertEquals("applied", state(r).getString("terminal_result"))
        val restart = RemoteConfig(cfg, "watch", { changed++ }, {})
        restart.receive(command(0, JSONObject().put("scan_on_s", 1)))
        assertEquals(1, changed); assertEquals(1L, state(restart).getLong("terminal_revision"))
    }

    @Test fun invalidSettingsAndWrongIdentityCannotPartiallyApply() {
        val cfg = MemoryConfig(); var changed = false
        val r = RemoteConfig(cfg, "watch", { changed = true }, {})
        r.receive(command(1, JSONObject().put("scan_on_s", 3)).put("device", "w-02"))
        assertEquals(0L, state(r).getLong("terminal_revision"))
        r.receive(command(2, JSONObject().put("scan_on_s", 3).put("mqtt_password", "secret")))
        assertFalse(changed); assertEquals("2", cfg["scan_on_s"])
        assertEquals("failed", state(r).getString("terminal_result"))
    }

    @Test fun networkTimeoutRollsBackAcrossProcessRestart() {
        val cfg = MemoryConfig(); var time = 1000L; var reconnect = 0
        val r = RemoteConfig(cfg, "watch", { if(it) reconnect++ }, {}, {time})
        r.receive(command(1, JSONObject().put("mqtt_host", "new-host").put("scan_on_s", 3)))
        assertEquals("applying", state(r).getString("terminal_result"))
        assertEquals("new-host", cfg["mqtt_host"])
        val restart = RemoteConfig(cfg, "watch", { if(it) reconnect++ }, {}, {time})
        time += 91_000; restart.tick()
        assertEquals("old-host", cfg["mqtt_host"]); assertEquals("2", cfg["scan_on_s"])
        assertEquals("failed", state(restart).getString("terminal_result")); assertEquals(2, reconnect)
        restart.receive(command(1, JSONObject().put("mqtt_host", "new-host")))
        assertEquals("old-host", cfg["mqtt_host"])
    }

    @Test fun staleConnectionCannotConfirmNewNetwork() {
        val cfg = MemoryConfig(); var time = 1000L
        val r = RemoteConfig(cfg, "watch", {}, {}, {time})
        r.connected(); val old = state(r).getString("terminal_session")
        r.receive(command(1, JSONObject().put("mqtt_host", "new-host")))
        r.disconnected(); r.connected()
        r.confirm(JSONObject().put("revision", 1).put("session", old))
        assertEquals("applying", state(r).getString("terminal_result"))
        time += 91_000; r.tick(); assertEquals("old-host", cfg["mqtt_host"])
    }

    @Test fun mqttConfirmationAlsoRequiresHealthyHttpWithSameStore() {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/health") { exchange ->
            val text = "{\"store_id\":\"test-store\"}".toByteArray()
            exchange.sendResponseHeaders(200, text.size.toLong()); exchange.responseBody.use { it.write(text) }
        }
        server.start()
        try {
            val cfg = MemoryConfig()
            val r = RemoteConfig(cfg, "watch", {}, {})
            r.receive(command(1, JSONObject().put("api_base", "http://127.0.0.1:" + server.address.port)))
            r.connected(); val token = state(r).getString("terminal_session")
            r.confirm(JSONObject().put("revision", 1).put("session", token))
            val end = System.currentTimeMillis() + 4000
            while (state(r).getString("terminal_result") == "applying" && System.currentTimeMillis() < end) Thread.sleep(10)
            assertEquals("applied", state(r).getString("terminal_result"))
            assertEquals("", cfg["_terminal_previous"])
        } finally { server.stop(0) }
    }

    @Test fun failedPreferenceWriteNeverReportsApplied() {
        val cfg = MemoryConfig(); cfg.fail = true; var changed = false
        val r = RemoteConfig(cfg, "watch", { changed = true }, {})
        r.receive(command(1, JSONObject().put("scan_on_s", 3)))
        assertFalse(changed); assertEquals(0L, state(r).getLong("terminal_revision"))
    }
}
