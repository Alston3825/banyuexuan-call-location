package cn.banyuexuan.call.common

import android.content.Context
import java.io.File

/** 崩溃记录:应用崩溃时把错误原因存到文件,下次打开时在屏幕上显示出来(拍照发给开发者),不用电脑连接。 */
object CrashLog {
    private fun file(c: Context) = File(c.filesDir, "last-crash.txt")

    fun install(c: Context) {
        val app = c.applicationContext
        val old = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { t, e ->
            try {
                val v = try { Updater.versionText(app) } catch (_: Throwable) { "?" }
                file(app).writeText("版本 $v  线程 ${t.name}\n" + android.util.Log.getStackTraceString(e).take(2500))
            } catch (_: Throwable) {}
            old?.uncaughtException(t, e)
        }
    }

    fun pending(c: Context): String? = try { file(c).takeIf { it.exists() }?.readText() } catch (_: Throwable) { null }
    fun clear(c: Context) { try { file(c).delete() } catch (_: Throwable) {} }
}
