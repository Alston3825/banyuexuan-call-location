package cn.banyuexuan.call.watch

import android.os.Handler
import android.os.Looper
import java.util.concurrent.CopyOnWriteArrayList

/** In-process state shared by the service and the activity. Mutate only inside update { } (main thread). */
object WatchState {
    data class Alert(val callId: String, val roomNo: String, val label: String, val voiceUrl: String?, val text: String?,
                     val emergency: Boolean, val reason: String, var status: String = "pending", val help: Boolean = false)

    /** 我发出的求助:status = sent/alone/floor/leader/accepted/arrived/completed/cancelled,text 由主机给出。 */
    data class Help(val callId: String?, var status: String, var text: String)

    /** 一条同事消息。pending=true 表示本机刚发、主机还没确认。 */
    data class Msg(val id: String, val channel: String, val channelName: String, val senderName: String, val mine: Boolean,
                   val kind: String, val text: String?, val voiceUrl: String?, val durationS: Double?, val at: String,
                   val note: String?, val clientId: String? = null, var pending: Boolean = false)

    val alerts = LinkedHashMap<String, Alert>()      // main-thread access only via update()
    @Volatile var connected = false
    @Volatile var appVisible = false                  // 主界面在前台时为 true(此时不显示悬浮球)
    @Volatile var battery = 100
    @Volatile var charging = false
    const val LOW_BATTERY = 20                        // 低于此值(且未在充电)提示充电;主机在护士站同步提醒
    val lowBattery get() = battery < LOW_BATTERY && !charging
    @Volatile var heartRateIntervalS = 60
    @Volatile var where = "—"                         // last located area
    @Volatile var notice: String? = null              // one-shot message (e.g. rejected command)
    @Volatile var appUpdateSeq = 0                    // 主机每通知一次“有新版本”就加 1,界面据此马上检查更新

    // ---- 绑定(主机下发 WATCH_BIND;主机是唯一依据) ----
    @Volatile var bindKnown = false                   // 连上主机后收到过绑定信息
    @Volatile var staffId: String? = null
    @Volatile var staffName: String? = null
    @Volatile var rooms: List<String> = emptyList()
    @Volatile var wearingSpare = false                // 正戴着备用表(可"一键归位"切回自己的表)
    @Volatile var isOwnWatchOff = false               // 这是某人自己的表,已被换下(充好电后可在这块表上"一键归位")
    @Volatile var canBroadcast = false

    // ---- 主机下发的参数(WATCH_BIND.settings),这里是默认值 ----
    @Volatile var scanOnS = 2
    @Volatile var scanPeriodS = 10
    @Volatile var helpHoldMs = 1000L
    @Volatile var wifiLock = true
    @Volatile var cpuLock = true
    @Volatile var quickReplies: List<String> = listOf("收到", "马上到", "需要帮忙", "请稍等", "已处理")
    @Volatile var voiceMaxS = 30

    // ---- 求助 / 同事消息 ----
    var help: Help? = null
    val msgs = LinkedHashMap<String, Msg>()            // id -> 消息(最近 200 条)
    var unread = 0

    fun addMsg(m: Msg) {
        if (m.clientId != null) msgs.values.firstOrNull { it.pending && it.clientId == m.clientId }?.let { msgs.remove(it.id) }
        msgs[m.id] = m
        while (msgs.size > 200) msgs.remove(msgs.keys.first())
    }

    /** 换出/归还时清空本机上与人有关的一切(呼叫、求助、消息、位置);设备设置(主机地址等)保留。 */
    fun clearPersonal() {
        alerts.clear(); help = null; msgs.clear(); unread = 0; where = "—"
        staffId = null; staffName = null; rooms = emptyList(); wearingSpare = false; canBroadcast = false
    }

    private val listeners = CopyOnWriteArrayList<() -> Unit>()
    private val main = Handler(Looper.getMainLooper())

    fun listen(l: () -> Unit) { listeners.add(l) }
    fun unlisten(l: () -> Unit) { listeners.remove(l) }
    fun later(ms: Long, block: () -> Unit) { main.postDelayed({ block(); listeners.forEach { it() } }, ms) }
    fun update(block: () -> Unit) { main.post { block(); listeners.forEach { it() } } }
}
