package cn.banyuexuan.call.common

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.widget.Toast
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/** 在线更新:向主机询问新版本 → 下载并校验 → 交给系统安装器。系统会再弹一次"更新"确认(Android 不允许普通应用静默安装)。 */
object Updater {
    data class Info(val code: Long, val name: String, val notes: String, val sha256: String, val size: Long)

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var busy = false

    @Suppress("DEPRECATION")
    fun installedCode(ctx: Context): Long = ctx.packageManager.getPackageInfo(ctx.packageName, 0).let {
        if (android.os.Build.VERSION.SDK_INT >= 28) it.longVersionCode else it.versionCode.toLong() }      // longVersionCode 需要 Android 9
    fun installedName(ctx: Context): String = ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName ?: ""
    fun versionText(ctx: Context) = "v${installedName(ctx)}(${installedCode(ctx)})"

    private fun open(cfg: DeviceConfig, path: String): HttpURLConnection =
        (URL(cfg.apiBase + path).openConnection() as HttpURLConnection).apply {
            connectTimeout = 5000; readTimeout = 15000
            if (path.startsWith("/watch-device/")) setRequestProperty("X-Watch-Update", cfg["watch_update_token"]) else setRequestProperty("X-Api-Key", cfg.apiKey) }

    /** 后台询问主机;有比当前更新的版本时回调 Info,否则回调 null(含网络不通:静默)。回调在主线程。 */
    fun checkAsync(ctx: Context, cfg: DeviceConfig, app: String, cb: (Info?) -> Unit) {
        Thread {
            val info = try {
                val c = open(cfg, if (app == "watch") "/watch-device/${cfg.deviceId}/update" else "/app/update?app=$app")
                if (c.responseCode != 200) null else {
                    val j = JSONObject(c.inputStream.bufferedReader().readText())
                    if (!j.optBoolean("available") || j.optLong("versionCode") <= installedCode(ctx)) null
                    else Info(j.getLong("versionCode"), j.optString("versionName"), j.optString("notes"),
                        j.getString("sha256"), j.optLong("size"))
                }
            } catch (_: Throwable) { null }
            main.post { cb(info) }
        }.start()
    }

    /** 下载 → 校验 → 提交安装。status 在主线程回报进度/结果文字。 */
    fun update(ctx: Context, cfg: DeviceConfig, app: String, info: Info, status: (String) -> Unit) {
        if (busy) return
        if (!ctx.packageManager.canRequestPackageInstalls()) {       // 首次:引导到系统里允许本应用安装更新
            status("请先允许本应用“安装未知应用”,允许后返回再点一次")
            ctx.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${ctx.packageName}"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            return
        }
        busy = true
        Thread {
            fun say(t: String) = main.post { status(t) }
            val f = File(ctx.cacheDir, "update-$app.apk")
            try {
                say("正在下载 0%")
                val c = open(cfg, if (app == "watch") "/watch-device/${cfg.deviceId}/download" else "/app/download/$app").apply { readTimeout = 60000 }
                if (c.responseCode != 200) throw IllegalStateException("主机返回 ${c.responseCode}")
                val md = MessageDigest.getInstance("SHA-256")
                val total = c.contentLengthLong.takeIf { it > 0 } ?: info.size
                var got = 0L; var last = -1
                c.inputStream.use { ins -> f.outputStream().use { out ->
                    val buf = ByteArray(32 * 1024)
                    while (true) {
                        val n = ins.read(buf); if (n < 0) break
                        out.write(buf, 0, n); md.update(buf, 0, n); got += n
                        val pct = if (total > 0) (got * 100 / total).toInt() else 0
                        if (pct != last) { last = pct; say("正在下载 $pct%") }
                    } } }
                val sum = md.digest().joinToString("") { "%02x".format(it) }
                if (!sum.equals(info.sha256, true)) throw IllegalStateException("文件校验失败,请重试")
                say("下载完成,正在安装…")
                install(ctx, f)
            } catch (e: Exception) {
                f.delete(); say("更新失败:${e.message ?: e.javaClass.simpleName}")
            } finally { busy = false }
        }.start()
    }

    private fun install(ctx: Context, apk: File) {
        val pi = ctx.packageManager.packageInstaller
        val id = pi.createSession(PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL))
        pi.openSession(id).use { s ->
            apk.inputStream().use { ins -> s.openWrite("app.apk", 0, apk.length()).use { out -> ins.copyTo(out); s.fsync(out) } }
            val pend = PendingIntent.getBroadcast(ctx, id, Intent(ctx, InstallResultReceiver::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE)
            s.commit(pend.intentSender)
        }
    }
}

/** 系统安装器的结果:需要用户确认时拉起确认界面;失败时提示原因。 */
class InstallResultReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) {
        when (val st = i.getIntExtra(PackageInstaller.EXTRA_STATUS, -1)) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                @Suppress("DEPRECATION") val confirm = i.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
                confirm?.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)?.let { c.startActivity(it) }
            }
            PackageInstaller.STATUS_SUCCESS -> {}
            else -> Toast.makeText(c, "更新未完成(${i.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: st})", Toast.LENGTH_LONG).show()
        }
    }
}
