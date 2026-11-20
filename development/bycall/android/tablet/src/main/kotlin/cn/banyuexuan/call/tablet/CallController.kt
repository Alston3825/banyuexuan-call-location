package cn.banyuexuan.call.tablet

import cn.banyuexuan.call.common.DeviceConfig
import cn.banyuexuan.call.common.Http
import cn.banyuexuan.call.common.MqttBus
import cn.banyuexuan.call.common.Proto
import org.json.JSONObject
import java.io.File
import java.util.Timer
import java.util.TimerTask
import java.util.concurrent.CopyOnWriteArrayList

/** Room terminal logic: send requests, show receipts, queue while offline, heartbeat. UI gets updates via [onState]. */
class CallController(private val cfg: DeviceConfig, private val cfgVer: () -> String,
                     private val onConfig: (JSONObject) -> Unit, private val onState: (State) -> Unit) {
    data class State(val connected: Boolean, val text: String, val step: Int, val callId: String?, val closed: Boolean)

    private fun createBus() = MqttBus(cfg.mqttHost, cfg.mqttPort, "tab-" + cfg.deviceId, cfg.deviceId, cfg.mqttPassword,
        Proto.tStatus(cfg.store, cfg.deviceId),
        Proto.envelope(cfg.store, cfg.deviceId, Proto.STATUS, JSONObject().put("online", false)))
    private var bus = createBus()
    private val main = android.os.Handler(android.os.Looper.getMainLooper())
    private var running = false
    private var lastHeartbeat = 0L
    private val remote = cn.banyuexuan.call.common.RemoteConfig(cfg, "tablet", { network ->
        if (network) { stop(); start() }
        push(state)
    }, { heartbeat() })
    private val queue = CopyOnWriteArrayList<Pair<String, JSONObject>>()   // unsent requests while offline
    private var timer: Timer? = null
    @Volatile private var state = State(false, "正在连接…", 0, null, true)
    /** 主机通知“有新版本”时调用(界面马上检查更新)。 */
    @Volatile var onAppUpdate: (() -> Unit)? = null
    val callId get() = state.callId

    fun start() {
        timer?.cancel()
        running = true
        bus = createBus()
        val activeBus = bus
        bus.onConnection = connection@{ up ->
            if (bus !== activeBus || !running) return@connection
            if (!up) remote.disconnected()
            val idle = state.closed
            push(state.copy(connected = up, text = when {
                up && state.text in setOf("正在连接…", "网络未连接,请直接联系护士站") -> "请选择您需要的服务"
                !up && idle -> "网络未连接,请直接联系护士站"
                else -> state.text }))
        }
        bus.onConnectedHook = connected@{
            if (bus !== activeBus || !running) return@connected
            remote.connected()
            heartbeat()
            bus.publish(Proto.tStatus(cfg.store, cfg.deviceId),
                Proto.envelope(cfg.store, cfg.deviceId, Proto.STATUS, JSONObject().put("online", true)))
            queue.forEach { (t, m) -> bus.publish(t, m) }; queue.clear()    // host dedupes by msg_id
        }
        bus.subscribe(Proto.tFeedback(cfg.store, cfg["room_id"])) { _, m -> onFeedback(m) }
        bus.subscribe(Proto.tReply(cfg.store, cfg.deviceId)) { _, m ->
            when (m.optString("type")) {
                "TERMINAL_CONFIG" -> m.optJSONObject("payload")?.let { p -> main.post { if (running) remote.receive(p) } }
                "TERMINAL_CONFIRM" -> m.optJSONObject("payload")?.let { p -> main.post { if (running) remote.confirm(p) } }
                Proto.CMD_REJECTED -> push(state.copy(text = "发送失败,请直接联系护士站"))
                Proto.DEVICE_CONFIG -> m.optJSONObject("payload")?.let(onConfig)
                Proto.APP_UPDATE -> if (m.optJSONObject("payload")?.optString("app") == "tablet") onAppUpdate?.invoke()
            }
        }
        bus.connect()
        timer = Timer(true).apply {
            schedule(object : TimerTask() { override fun run() {
                main.post { if (running) remote.tick() }
                val now = System.currentTimeMillis()
                val every = (cfg["heartbeat_s"].toLongOrNull() ?: 30L).coerceIn(10L, 30L) * 1000
                if (now - lastHeartbeat >= every) { lastHeartbeat = now; heartbeat() }
            } }, 5000, 1000)
        }
    }

    fun stop() { remote.disconnected(); running = false; timer?.cancel(); bus.disconnect() }

    private fun heartbeat() {
        bus.publish(Proto.tHeartbeat(cfg.store, cfg.deviceId),
            Proto.envelope(cfg.store, cfg.deviceId, Proto.HEARTBEAT,
                remote.decorate(JSONObject().put("cfg_ver", cfgVer()))), qos0 = true)
    }

    private fun push(s: State) { state = s; onState(s) }

    private fun onFeedback(m: JSONObject) {
        if (m.optString("type") != Proto.CALL_FEEDBACK) return
        val id = m.optString("call_id")
        if (state.callId != null && id != state.callId) return          // stale receipt of an older call
        val p = m.getJSONObject("payload")
        val (step, closed) = when (p.optString("status")) {
            "received" -> 1 to false; "accepted" -> 2 to false; "arrived" -> 3 to false
            "completed" -> 4 to true; "cancelled" -> 0 to true; else -> state.step to false
        }
        push(state.copy(text = p.optString("text"), step = step, callId = id, closed = closed))
    }

    /** Send a request. The call id is assigned by the host; the first feedback (received) carries it. */
    fun request(serviceCode: String, voiceUrl: String? = null, text: String? = null) {
        val room = cfg["room_id"]
        val payload = JSONObject().put("room", room).put("service_code", serviceCode)
        if (voiceUrl != null) payload.put("voice_url", voiceUrl)
        if (text != null) payload.put("text", text)
        val msg = Proto.envelope(cfg.store, cfg.deviceId, Proto.CALL_REQUEST, payload)
        val topic = Proto.tCall(cfg.store, cfg["floor"], room)
        push(State(state.connected, "已发出…", 0, null, false))
        if (!bus.publish(topic, msg)) { queue.add(topic to msg); push(state.copy(text = "网络未连接,恢复后自动发出")) }
    }

    fun cancel() {
        val id = state.callId ?: return
        bus.publish(Proto.tAck(cfg.store, id), Proto.envelope(cfg.store, cfg.deviceId, Proto.CALL_CANCEL, callId = id))
    }

    /** Upload a recorded voice message, then send it as a nurse request. Runs on a worker thread. */
    fun sendVoice(file: File) {
        Thread {
            val (url, why) = Http.uploadVoice(cfg.apiBase, cfg.apiKey, file.readBytes(), "m4a")
            file.delete()
            if (url != null) request("NEED_NURSE", url)
            else {
                // 录音没传上去也一定要叫到护理师:照常发出呼叫,并说明情况;平板上显示失败原因,方便工作人员修设置
                request("NEED_NURSE", null, "客人录了语音留言但没能传上来,请到房间询问")
                push(state.copy(text = "已呼叫护理师(语音没传上去:$why)"))
            }
        }.start()
    }
}
