package cn.banyuexuan.call.watch

import android.os.Handler
import android.os.Looper
import java.util.concurrent.CopyOnWriteArrayList

/** In-process state shared by the service and the activity. */
object WatchState {
    data class Alert(val callId: String, val roomNo: String, val label: String, val voiceUrl: String?, val text: String?,
                     val emergency: Boolean, val reason: String, var status: String = "pending")

    val alerts = LinkedHashMap<String, Alert>()      // main-thread access only via update()
    @Volatile var connected = false
    @Volatile var appVisible = false                  // 主界面在前台时为 true(此时不显示悬浮球)
    @Volatile var battery = 100
    @Volatile var charging = false
    const val LOW_BATTERY = 20                        // 低于此值(且未在充电)提示充电;主机在护士站同步提醒
    val lowBattery get() = battery < LOW_BATTERY && !charging
    @Volatile var where = "—"                         // last located area
    @Volatile var notice: String? = null              // one-shot message (e.g. rejected command)
    private val listeners = CopyOnWriteArrayList<() -> Unit>()
    private val main = Handler(Looper.getMainLooper())

    fun listen(l: () -> Unit) { listeners.add(l) }
    fun unlisten(l: () -> Unit) { listeners.remove(l) }
    fun later(ms: Long, block: () -> Unit) { main.postDelayed({ block(); listeners.forEach { it() } }, ms) }
    fun update(block: () -> Unit) { main.post { block(); listeners.forEach { it() } } }
}
