package cn.banyuexuan.call.common

import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/** Blocking helpers for the host's local HTTP API (call from a worker thread). */
object Http {
    fun upload(apiBase: String, apiKey: String, bytes: ByteArray, ext: String): String? = try {
        val c = URL("$apiBase/voice?ext=$ext").openConnection() as HttpURLConnection
        c.requestMethod = "POST"; c.doOutput = true; c.connectTimeout = 5000; c.readTimeout = 10000
        c.setRequestProperty("X-Api-Key", apiKey)
        c.outputStream.use { it.write(bytes) }
        if (c.responseCode == 200) JSONObject(c.inputStream.bufferedReader().readText()).getString("voice_url") else null
    } catch (_: Exception) { null }

    fun download(apiBase: String, apiKey: String, voiceUrl: String, dest: File): Boolean = try {
        val c = URL(apiBase + voiceUrl).openConnection() as HttpURLConnection
        c.connectTimeout = 5000; c.readTimeout = 10000
        c.setRequestProperty("X-Api-Key", apiKey)
        if (c.responseCode == 200) { c.inputStream.use { i -> dest.outputStream().use { o -> i.copyTo(o) } }; true } else false
    } catch (_: Exception) { false }

    /** GET 一个 JSON 接口,失败返回 null。 */
    fun getText(apiBase: String, apiKey: String, path: String): String? = try {
        val c = URL(apiBase + path).openConnection() as HttpURLConnection
        c.connectTimeout = 5000; c.readTimeout = 10000
        c.setRequestProperty("X-Api-Key", apiKey)
        if (c.responseCode == 200) c.inputStream.bufferedReader().readText() else null
    } catch (_: Exception) { null }
}
