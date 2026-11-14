package cn.banyuexuan.call.tablet

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Auto-start after boot. Android 10+ may block background activity starts; must be allowed on the real device
 *  (autostart whitelist / device-owner kiosk). Verify on the sample tablet. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) {
        if (i.action == Intent.ACTION_BOOT_COMPLETED || i.action == Intent.ACTION_MY_PACKAGE_REPLACED)
            try { c.startActivity(Intent(c, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) } catch (e: Exception) { }
    }
}
