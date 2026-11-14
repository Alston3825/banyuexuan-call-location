package cn.banyuexuan.call.watch

import android.app.Application
import cn.banyuexuan.call.common.CrashLog

class WatchApp : Application() {
    override fun onCreate() { super.onCreate(); CrashLog.install(this) }
}
