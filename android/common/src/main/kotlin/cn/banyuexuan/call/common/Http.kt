package cn.banyuexuan.call.common

import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/** Blocking helpers for the host's local HTTP API (call from a worker thread). */
object Http {
    fun upload(apiBase: String, apiKey: String, bytes: ByteArray, ext: String, watch: String? = null, binding: String? = null): String? = try {
        val c = URL(if (watch == null) "$apiBase/voice?ext=$ext" else "$apiBase/watch-device/$watch/voice?ext=$ext").openConnection() as HttpURLConnection
        c.requestMethod = "POST"; c.doOutput = true; c.connectTimeout = 5000; c.readTimeout = 10000
        c.setRequestProperty("X-Api-Key", apiKey)
        if (binding != null) c.setRequestProperty("X-Watch-Binding", binding)
        c.outputStream.use { it.write(bytes) }
        if (c.responseCode == 200) JSONObject(c.inputStream.bufferedReader().readText()).getString("voice_url") else null
    } catch (_: Exception) { null }

    fun download(apiBase: String, apiKey: String, voiceUrl: String, dest: File, binding: String? = null): Boolean = try {
        val c = URL(apiBase + voiceUrl).openConnection() as HttpURLConnection
        c.connectTimeout = 5000; c.readTimeout = 10000
        c.setRequestProperty("X-Api-Key", apiKey)
        if (binding != null) c.setRequestProperty("X-Watch-Binding", binding)
        if (c.responseCode == 200) { c.inputStream.use { i -> dest.outputStream().use { o -> i.copyTo(o) } }; true } else false
    } catch (_: Exception) { false }

    /** GET 一个 JSON 接口,失败返回 null。 */
    fun getText(apiBase: String, apiKey: String, path: String, binding: String? = null): String? = try {
        val c = URL(apiBase + path).openConnection() as HttpURLConnection
        c.connectTimeout = 5000; c.readTimeout = 10000
        c.setRequestProperty("X-Api-Key", apiKey)
        if (binding != null) c.setRequestProperty("X-Watch-Binding", binding)
        if (c.responseCode == 200) c.inputStream.bufferedReader().readText() else null
    } catch (_: Exception) { null }
}
