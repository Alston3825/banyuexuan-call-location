package cn.banyuexuan.call.watch

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) {
        if (i.action == Intent.ACTION_BOOT_COMPLETED || i.action == Intent.ACTION_MY_PACKAGE_REPLACED)
            try { c.startForegroundService(Intent(c, WatchService::class.java)) }
            catch (e: Exception) { /* 新版安卓禁止后台启动前台服务:不能闪退,等用户打开应用时由界面启动服务 */ }
    }
}

/** adb shell am broadcast -a cn.banyuexuan.call.watch.FAKE_BEACON -p cn.banyuexuan.call.watch --es ids "1601-1:-52" */
class DebugReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) {
        val ids = i.getStringExtra("ids") ?: return
        WatchService.fakeReadings = ids.split(",").mapNotNull {
            val p = it.split(":"); if (p.size == 2) Beacons.Reading(p[0], p[1].toIntOrNull() ?: return@mapNotNull null) else null
        }
    }
}
