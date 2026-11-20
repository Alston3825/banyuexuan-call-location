package cn.banyuexuan.call.watch

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import cn.banyuexuan.call.common.DeviceConfig
import cn.banyuexuan.call.common.Http
import cn.banyuexuan.call.common.MqttBus
import cn.banyuexuan.call.common.Proto
import cn.banyuexuan.call.watch.WatchState.Alert
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.Timer
import java.util.TimerTask
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Foreground service: MQTT link to the host, call alerts (vibrate + sound + notification),
 * intermittent BLE beacon scanning, heartbeat with battery, heart-rate upload,
 * one-key help, colleague messages, watch binding / reset.
 * The watch holds no business logic: the host decides who gets which call.
 *
 * 兼容:minSdk 26(Android 8.0)。所有高于 26 的接口都有版本判断;后台线程一律 catch Throwable。
 */
class WatchService : Service() {
    companion object {
        @Volatile var fakeReadings: List<Beacons.Reading>? = null
        @Volatile var instance: WatchService? = null
        fun config(ctx: Context) = DeviceConfig(ctx, mapOf("mqtt_host" to "192.168.1.108", "mqtt_port" to "1883",
            "store_id" to "gx", "device_id" to "w-01", "api_base" to "http://192.168.1.108:8100", "api_key" to "", "mqtt_password" to "",
            "beacon_uuid" to "", "report_seconds" to "10"))
        private const val CH_SERVICE = "service"
        private const val CH_CALLS = "calls"
        private const val CH_BATTERY = "battery"
        private const val CH_CHAT = "chat"
        private const val LOW_REMIND_MS = 30 * 60_000L
        private const val REMIND_MS = 15000L
        private const val MAX_REMINDS = 8
        /** 本版本支持的能力(随心跳报给主机;老版本不带,主机就不等回执) */
        val CAPS = listOf("receipt", "help", "chat", "bind")
        // 震动方式:客户呼叫 / 紧急 / 同事求助 / 同事消息 各不相同,不看屏幕也能分辨
        private val VIB_CALL = longArrayOf(0, 400, 200, 400)
        private val VIB_EMERGENCY = LongArray(12) { if (it == 0) 0L else if (it % 2 == 1) 600L else 250L }
        private val VIB_HELP = longArrayOf(0, 150, 90, 150, 90, 150, 450, 150, 90, 150, 90, 150)
        private val VIB_CHAT = longArrayOf(0, 60)
        private val VIB_OK = longArrayOf(0, 300)
    }

    private lateinit var cfg: DeviceConfig
    private lateinit var remote: cn.banyuexuan.call.common.RemoteConfig
    private var lastHeartbeat = 0L
    private var bus: MqttBus? = null
    private var timer: Timer? = null
    private val window = Beacons.Window()
    private val main = Handler(Looper.getMainLooper())
    private var scanCb: ScanCallback? = null
    private var player: MediaPlayer? = null
    @Volatile private var chatPlaying = false
    private var reminds = 0
    private var wifiLock: WifiManager.WifiLock? = null
    private var cpuLock: android.os.PowerManager.WakeLock? = null

    override fun onBind(i: Intent?): IBinder? = null
    override fun onStartCommand(i: Intent?, f: Int, id: Int) = START_STICKY

    override fun onCreate() {
        super.onCreate()
        instance = this
        cfg = config(this)
        remote = cn.banyuexuan.call.common.RemoteConfig(cfg, "watch", { network ->
            applySettings(effectiveSettings())
            if (network) { bus?.disconnect(); startBus() }
        }, { heartbeat() })
        applySettings(effectiveSettings())
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CH_SERVICE, "运行状态", NotificationManager.IMPORTANCE_LOW))
        nm.createNotificationChannel(NotificationChannel(CH_BATTERY, "低电量提醒", NotificationManager.IMPORTANCE_DEFAULT))
        nm.createNotificationChannel(NotificationChannel(CH_CHAT, "同事消息", NotificationManager.IMPORTANCE_DEFAULT).apply {
            enableVibration(false); setSound(null, null) })
        nm.createNotificationChannel(NotificationChannel(CH_CALLS, "呼叫提醒", NotificationManager.IMPORTANCE_HIGH).apply {
            enableVibration(false); setSound(null, null) })     // we vibrate/ring ourselves so it repeats until answered
        try {
            val n = Notification.Builder(this, CH_SERVICE).setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle("伴月轩护理端运行中").setOngoing(true).build()
            try {
                if (android.os.Build.VERSION.SDK_INT >= 29) startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
                else startForeground(1, n)                          // API 26-28 没有带类型的重载
            } catch (e: Exception) { startForeground(1, n) }        // 带类型失败(缺权限等)时退回按清单声明的类型启动
        } catch (e: Exception) { stopSelf(); return }              // permissions not granted yet: the activity restarts us
        KeepAlive.schedule(this)
        applyWifiLock()
        startBus(); startScanCycle(); startTimers()
        bubble = Bubble(this); WatchState.listen(bubbleListener); bubble?.refresh()
    }

    private var bubble: Bubble? = null
    private val bubbleListener: () -> Unit = { bubble?.refresh() }

    /** 应用从“最近任务”里被划掉:2 秒后自动拉起服务,继续收呼叫。 */
    override fun onTaskRemoved(rootIntent: Intent?) {
        KeepAlive.restartSoon(this)
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        WatchState.unlisten(bubbleListener); bubble?.hide()
        instance = null
        val old = bus; bus = null; WatchState.connected = false
        timer?.cancel(); old?.disconnect(); stopScanCycle(); player?.release()
        try { wifiLock?.let { if (it.isHeld) it.release() } } catch (_: Throwable) { }
        try { cpuLock?.let { if (it.isHeld) it.release() } } catch (_: Throwable) { }
        super.onDestroy()
    }

    /** 熄屏时保持 Wi-Fi 不休眠:廉价手表熄屏后 Wi-Fi 会进入省电,呼叫可能延迟几十秒。费电,主机可配置关闭。 */
    private fun applyWifiLock() {
        try {
            val wm = applicationContext.getSystemService(WifiManager::class.java) ?: return
            if (WatchState.wifiLock) {
                if (wifiLock == null) {
                    @Suppress("DEPRECATION")
                    val mode = if (android.os.Build.VERSION.SDK_INT >= 29) WifiManager.WIFI_MODE_FULL_LOW_LATENCY else WifiManager.WIFI_MODE_FULL_HIGH_PERF
                    wifiLock = wm.createWifiLock(mode, "bycall:wifi").apply { setReferenceCounted(false) }
                }
                wifiLock?.let { if (!it.isHeld) it.acquire() }
            } else wifiLock?.let { if (it.isHeld) it.release() }
        } catch (_: Throwable) { }
        // 息屏后 CPU 休眠会发不出 MQTT 保活心跳,主机约 45 秒后判定掉线;保持 CPU 唤醒可避免(费电,主机可配置关闭)
        try {
            if (WatchState.cpuLock) {
                if (cpuLock == null) cpuLock = getSystemService(android.os.PowerManager::class.java)
                    .newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK, "bycall:cpu").apply { setReferenceCounted(false) }
                cpuLock?.let { if (!it.isHeld) it.acquire() }
            } else cpuLock?.let { if (it.isHeld) it.release() }
        } catch (_: Throwable) { }
    }

    // ---- MQTT -------------------------------------------------------
    private fun startBus() {
        val s = cfg.store; val d = cfg.deviceId
        val b = MqttBus(cfg.mqttHost, cfg.mqttPort, d, d, cfg.mqttPassword, Proto.tStatus(s, d),
            Proto.envelope(s, d, Proto.STATUS, JSONObject().put("online", false)))
        b.onConnection = { up -> if (bus === b) { WatchState.connected = up; WatchState.update { } } }   // 旧连接(服务重启前)的迟到回调不能覆盖新连接的状态
        b.onConnectedHook = {
            b.publish(Proto.tStatus(s, d), Proto.envelope(s, d, Proto.STATUS, JSONObject().put("online", true)))
            heartbeat()
        }
        b.subscribe(Proto.tDispatchWatch(s, d)) { _, m -> onMessage(m) }
        b.subscribe(Proto.tReply(s, d)) { _, m -> onMessage(m) }
        bus = b; b.connect()
    }

    private fun str(p: JSONObject, k: String): String? = if (p.isNull(k)) null else p.optString(k).ifBlank { null }   // JSON null must not become "null"

    private fun onMessage(m: JSONObject) {
        val p = m.optJSONObject("payload") ?: JSONObject()
        val id = m.optString("call_id")
        try {
            when (m.optString("type")) {
                Proto.CALL_DISPATCH -> onDispatch(id, p)
                Proto.CALL_TAKEN -> WatchState.update {
                    if (WatchState.alerts.remove(id) != null) WatchState.notice =
                        if (p.optString("reason") == "cancelled") "呼叫已取消" else "已被${p.optString("by")}接单"
                }
                Proto.CALL_STATE -> WatchState.update {
                    when (p.optString("status")) {
                        "completed" -> WatchState.alerts[id]?.let { a ->        // 完成后卡片先停 5 秒显示"✓ 已完成",再消失
                            a.status = "completed"; WatchState.later(5000) { if (WatchState.alerts[id]?.status == "completed") WatchState.alerts.remove(id) } }
                        else -> WatchState.alerts[id]?.status = p.optString("status")
                    }
                }
                Proto.CMD_REJECTED -> WatchState.update {
                    val text = str(p, "text")
                    WatchState.notice = when (p.optString("reason")) {
                        "already_taken_or_closed" -> { WatchState.alerts.remove(id); "已被别人接走" }
                        "complete_not_allowed", "arrive_not_allowed" -> "请先到达房间并确认到场"
                        "not_bound" -> text ?: "这块表还没有绑定护理师,请到护士站领用"
                        else -> text ?: ("操作失败:" + p.optString("reason"))
                    }
                }
                Proto.HELP_STATE -> onHelpState(id, p)
                Proto.CHAT_MSG -> onChat(p)
                "TERMINAL_CONFIG" -> main.post { remote.receive(p) }
                "TERMINAL_CONFIRM" -> main.post { remote.confirm(p) }
                Proto.WATCH_BIND -> onWatchBind(p)
                Proto.WATCH_RESET -> onReset()
                Proto.APP_UPDATE -> if (p.optString("app") == "watch") onAppUpdate(p)
            }
        } catch (_: Throwable) { }        // 一条坏消息不能让服务崩掉
    }

    private fun onDispatch(id: String, p: JSONObject) {
        // 先回执:主机据此停止重发并统计送达时间(重复派单也要回,主机只算第一次)
        bus?.publish(Proto.tAck(cfg.store, id), Proto.envelope(cfg.store, cfg.deviceId, Proto.CALL_RECEIVED, JSONObject(), id))
        val help = p.optString("kind") == "help"
        val a = Alert(id, str(p, "room_no") ?: "", str(p, "label") ?: str(p, "service_code") ?: "",
            str(p, "voice_url"), str(p, "text"), p.optString("priority") == "emergency", str(p, "reason") ?: "", help = help)
        WatchState.update {
            val fresh = !WatchState.alerts.containsKey(id)
            if (fresh) {
                WatchState.alerts[id] = a
                stopChatVoice()                                   // 客户呼叫/求助优先:打断正在播放的同事语音
                alertUser(a); startReminders()
            }
        }
    }

    /** 主机刚发布了新版本:通知界面马上检查;应用不在前台就发一条通知。 */
    private fun onAppUpdate(p: JSONObject) {
        WatchState.update { WatchState.appUpdateSeq++ }
        if (WatchState.appVisible) return
        try {
            val open = PendingIntent.getActivity(this, 4, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            getSystemService(NotificationManager::class.java).notify(5, Notification.Builder(this, CH_BATTERY)
                .setSmallIcon(android.R.drawable.stat_sys_download_done).setContentTitle("手表程序有新版本 ${p.optString("version_name")}")
                .setContentText(p.optString("notes").ifBlank { "打开应用,点蓝色“发现新版本”更新" }).setContentIntent(open).setAutoCancel(true).build())
        } catch (_: Throwable) { }
    }

    private fun onHelpState(id: String, p: JSONObject) {
        val st = p.optString("status")
        val text = str(p, "text") ?: st
        WatchState.update {
            val h = WatchState.help
            if (h == null || h.callId == null || h.callId == id) WatchState.help = WatchState.Help(id, st, text)
            if (st == "accepted") vibrate(VIB_OK)
            if (st == "completed" || st == "cancelled")
                WatchState.later(5000) { if (WatchState.help?.callId == id && WatchState.help?.status == st) WatchState.help = null }
        }
    }

    private fun onChat(p: JSONObject) {
        val msg = WatchState.Msg(p.optString("id"), p.optString("channel"), str(p, "channel_name") ?: "", str(p, "sender_name") ?: "",
            p.optBoolean("mine"), p.optString("kind"), str(p, "text"), str(p, "voice_url"),
            if (p.isNull("duration_s")) null else p.optDouble("duration_s"), p.optString("at"), str(p, "note"), str(p, "client_id"))
        sendUp(Proto.CHAT_ACK, JSONObject().put("ids", JSONArray().put(msg.id)))
        WatchState.update {
            val fresh = !WatchState.msgs.containsKey(msg.id)
            WatchState.addMsg(msg)
            if (fresh && !msg.mine) {
                WatchState.unread++
                // 正在响客户呼叫时不再震(主机本来也会先排队),免得盖过呼叫提醒
                if (WatchState.alerts.values.none { it.status == "pending" }) { vibrate(VIB_CHAT); chatNotice(msg) }
            }
        }
    }

    private fun onWatchBind(p: JSONObject) {
        val staff = str(p, "staff")
        p.optJSONObject("settings")?.let { original ->
            cfg["_watch_host_settings"] = original.toString()
            applySettings(effectiveSettings())
        }
        WatchState.update {
            val before = WatchState.staffId
            val wasSpare = WatchState.wearingSpare
            if (staff != before) {
                WatchState.clearPersonal()                        // 换人/换出:旧会话的呼叫、消息全部作废
                main.removeCallbacks(remind)
            }
            WatchState.bindKnown = true
            WatchState.staffId = staff
            WatchState.staffName = str(p, "name")
            WatchState.rooms = p.optJSONArray("rooms")?.let { a -> (0 until a.length()).map { a.optString(it) } } ?: emptyList()
            WatchState.wearingSpare = staff != null && !p.optBoolean("home", true)
            WatchState.canBroadcast = p.optBoolean("can_broadcast")
            WatchState.isOwnWatchOff = staff == null && !p.optBoolean("spare") && p.optString("status") in setOf("CHARGING", "IDLE")
            WatchState.notice = when {
                before != null && staff == null -> "本表已换出,请交回护士站充电"
                staff != null && staff != before -> "已绑定:${WatchState.staffName ?: staff}"
                staff != null && wasSpare && !WatchState.wearingSpare -> "已切回自己的表"
                else -> WatchState.notice
            }
        }
    }

    private fun effectiveSettings(): JSONObject {
        val s = JSONObject().put("scan_on_s", 2).put("scan_period_s", 10).put("help_hold_ms", 1000)
            .put("voice_max_s", 30).put("wifi_lock", true).put("cpu_lock", true)
            .put("quick_replies", JSONArray(listOf("收到", "马上到", "需要帮忙")))
        val policy = try { JSONObject(cfg["_watch_host_settings"].ifBlank { "{}" }) } catch (_: Exception) { JSONObject() }
        for (k in policy.keys()) s.put(k, policy.get(k))
        val overrides = remote.settings()
        for (k in overrides.keys()) s.put(k, overrides.get(k))
        return s
    }

    private fun applySettings(s: JSONObject) {
        val on = s.optInt("scan_on_s", WatchState.scanOnS); val per = s.optInt("scan_period_s", WatchState.scanPeriodS)
        val scanChanged = on != WatchState.scanOnS || per != WatchState.scanPeriodS
        WatchState.scanOnS = on; WatchState.scanPeriodS = per
        WatchState.helpHoldMs = s.optLong("help_hold_ms", WatchState.helpHoldMs)
        WatchState.voiceMaxS = s.optInt("voice_max_s", WatchState.voiceMaxS)
        s.optJSONArray("quick_replies")?.let { a -> if (a.length() > 0) WatchState.quickReplies = (0 until a.length()).map { a.optString(it) } }
        val wl = s.optBoolean("wifi_lock", WatchState.wifiLock)
        val cl = s.optBoolean("cpu_lock", WatchState.cpuLock)
        val wlChanged = wl != WatchState.wifiLock || cl != WatchState.cpuLock
        WatchState.wifiLock = wl; WatchState.cpuLock = cl
        main.post { if (scanChanged) startScanCycle(); if (wlChanged) applyWifiLock() }
    }

    /** 归还备用池:清空本机上与人有关的数据(呼叫、求助、消息、位置、缓存的语音),然后告诉主机已清空。 */
    private fun onReset() {
        main.post {
            stopChatVoice(); player?.release(); player = null
            main.removeCallbacks(remind)
            try { getSystemService(NotificationManager::class.java).cancelAll() } catch (_: Throwable) { }
            try { cacheDir.listFiles()?.forEach { f -> if (f.name.startsWith("v-") || f.name.startsWith("chat-")) f.delete() } } catch (_: Throwable) { }
            WatchState.clearPersonal()
            WatchState.isOwnWatchOff = false
            WatchState.notice = "已清空,本表回到备用池"
            WatchState.update { }
            sendUp(Proto.RESET_DONE, JSONObject())
            // 前台服务的常驻通知在 cancelAll 里被清掉了,重新挂上
            try { startForeground(1, Notification.Builder(this, CH_SERVICE).setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle("伴月轩护理端运行中").setOngoing(true).build()) } catch (_: Throwable) { }
        }
    }

    // ---- commands from the UI --------------------------------------
    fun command(type: String, callId: String, payload: JSONObject = JSONObject()) {
        bus?.publish(Proto.tAck(cfg.store, callId), Proto.envelope(cfg.store, cfg.deviceId, type, payload, callId))
    }

    fun sendUp(type: String, payload: JSONObject): Boolean =
        bus?.publish(Proto.tUp(cfg.store, cfg.deviceId), Proto.envelope(cfg.store, cfg.deviceId, type, payload)) ?: false

    /** 一键求助:手表只负责发出,找谁、何时扩大范围都由主机决定。 */
    fun sendHelp(): Boolean {
        val ok = sendUp(Proto.HELP_REQUEST, JSONObject())
        if (ok) { vibrate(VIB_OK); WatchState.update { WatchState.help = WatchState.Help(null, "sending", "正在发出求助…") } }
        return ok
    }

    fun cancelHelp() {
        val id = WatchState.help?.callId ?: return
        command(Proto.CALL_CANCEL, id)
    }

    fun returnHome(): Boolean = sendUp(Proto.RETURN_HOME, JSONObject())

    /** 发同事消息;本机先显示“发送中”,主机回显后换成正式消息。 */
    fun sendChat(channel: String, kind: String, text: String?, voiceUrl: String? = null, seconds: Double? = null): Boolean {
        val cid = UUID.randomUUID().toString().replace("-", "")
        val p = JSONObject().put("channel", channel).put("kind", kind).put("client_id", cid)
        if (text != null) p.put("text", text)
        if (voiceUrl != null) p.put("voice_url", voiceUrl)
        if (seconds != null) p.put("duration_s", seconds)
        val ok = sendUp(Proto.CHAT_SEND, p)
        if (ok) WatchState.update {
            WatchState.addMsg(WatchState.Msg("local-$cid", channel, "", "我",
                true, kind, text, voiceUrl, seconds, java.time.OffsetDateTime.now(java.time.ZoneOffset.UTC).toString(), null, cid, pending = true))
        }
        return ok
    }

    /** 上传录音再发出(后台线程)。 */
    fun sendVoice(channel: String, f: File, seconds: Double) {
        Thread {
            val url = try { Http.upload(cfg.apiBase, cfg.apiKey, f.readBytes(), "m4a") } catch (_: Throwable) { null }
            f.delete()
            if (url == null) WatchState.update { WatchState.notice = "语音上传失败,请检查网络" }
            else if (!sendChat(channel, "voice", null, url, Math.round(seconds * 10) / 10.0)) WatchState.update { WatchState.notice = "未连接主机,语音没发出" }
        }.start()
    }

    fun playVoice(voiceUrl: String, chat: Boolean = false) {
        Thread {
            val f = File(cacheDir, "v-" + voiceUrl.substringAfterLast('/'))
            if (f.exists() || Http.download(cfg.apiBase, cfg.apiKey, voiceUrl, f)) main.post {
                if (chat && WatchState.alerts.values.any { it.status == "pending" }) return@post     // 有呼叫在响:不放同事语音
                try {
                    player?.release()
                    chatPlaying = chat
                    player = MediaPlayer().apply { setDataSource(f.path); setOnCompletionListener { it.release(); if (player === it) { player = null; chatPlaying = false } }; prepare(); start() }
                } catch (_: Throwable) { player = null; chatPlaying = false; WatchState.update { WatchState.notice = "语音无法播放" } }
            } else WatchState.update { WatchState.notice = "留言下载失败" }
        }.start()
    }

    private fun stopChatVoice() {
        if (!chatPlaying) return
        try { player?.stop(); player?.release() } catch (_: Throwable) { }
        player = null; chatPlaying = false
    }

    // ---- alerting ----------------------------------------------------
    private fun vibrator(): Vibrator = if (android.os.Build.VERSION.SDK_INT >= 31)
        getSystemService(VibratorManager::class.java).defaultVibrator else @Suppress("DEPRECATION") getSystemService(Vibrator::class.java)

    private fun vibrate(pattern: LongArray) { try { vibrator().vibrate(VibrationEffect.createWaveform(pattern, -1)) } catch (_: Throwable) { } }

    private fun alertUser(a: Alert) {
        // 紧急:连续震动 6 次;同事求助:两组急促短震;普通呼叫:震 2 次
        vibrate(if (a.emergency) VIB_EMERGENCY else if (a.help) VIB_HELP else VIB_CALL)
        try { RingtoneManager.getRingtone(this, RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)).play() } catch (_: Exception) {}
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val title = if (a.help) "【求助】${a.label}" else (if (a.emergency) "【紧急】" else "") + "${a.roomNo} 房 ${a.label}"
        val n = Notification.Builder(this, CH_CALLS).setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle(title)
            .setContentText(if (a.help) (a.text ?: "同事需要帮忙") else if (a.voiceUrl != null) "有语音留言" else "点击处理").setContentIntent(open)
            .setFullScreenIntent(open, true).setAutoCancel(true).build()
        getSystemService(NotificationManager::class.java).notify(a.callId.hashCode(), n)
    }

    private fun chatNotice(m: WatchState.Msg) {
        try {
            val open = PendingIntent.getActivity(this, 3, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                .putExtra("open", "chat"), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            getSystemService(NotificationManager::class.java).notify(3, Notification.Builder(this, CH_CHAT)
                .setSmallIcon(android.R.drawable.stat_notify_chat).setContentTitle("${m.senderName}(${m.channelName})")
                .setContentText(if (m.kind == "voice") "🎤 语音 ${m.durationS?.toInt() ?: 0} 秒" else (m.text ?: ""))
                .setContentIntent(open).setAutoCancel(true).build())
        } catch (_: Throwable) { }
    }

    private fun startReminders() { reminds = 0; main.removeCallbacks(remind); main.postDelayed(remind, REMIND_MS) }
    private val remind = object : Runnable {
        override fun run() {
            val pending = WatchState.alerts.values.filter { it.status == "pending" }
            if (pending.isEmpty() || ++reminds > MAX_REMINDS) return
            alertUser(pending.first()); main.postDelayed(this, REMIND_MS)
        }
    }

    // ---- BLE:间歇扫描(默认每 10 秒扫 2 秒,主机可配置)--------------------
    private fun has(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    private fun periodMs() = maxOf(7, WatchState.scanPeriodS) * 1000L      // 安卓限制:30 秒内最多启动扫描 5 次
    private fun onMs() = WatchState.scanOnS.coerceIn(1, maxOf(7, WatchState.scanPeriodS) - 1) * 1000L

    private val scanOn: Runnable = object : Runnable {
        override fun run() {
            startScanNow()
            main.postDelayed(scanOff, onMs())
        }
    }
    private val scanOff: Runnable = object : Runnable {
        override fun run() {
            stopScanNow()
            main.postDelayed(scanOn, periodMs() - onMs())
        }
    }

    private val locationReport = object : Runnable {
        override fun run() {
            try { reportLocation() } catch (_: Throwable) { }
            main.postDelayed(this, (cfg["report_seconds"].toLongOrNull() ?: 10L).coerceIn(7L, 60L) * 1000)
        }
    }
    private fun startScanCycle() { stopScanCycle(); main.post(scanOn); main.postDelayed(locationReport, 3000) }
    private fun stopScanCycle() { main.removeCallbacks(locationReport); main.removeCallbacks(scanOn); main.removeCallbacks(scanOff); stopScanNow() }

    @android.annotation.SuppressLint("MissingPermission")   // 已按系统版本手动检查权限,且整体包在 try 里
    private fun startScanNow() {
        if (scanCb != null) return
        if (android.os.Build.VERSION.SDK_INT >= 31 && !has(Manifest.permission.BLUETOOTH_SCAN)) return
        if (android.os.Build.VERSION.SDK_INT < 31 && !has(Manifest.permission.ACCESS_FINE_LOCATION)) return
        val scanner = try { getSystemService(BluetoothManager::class.java).adapter?.bluetoothLeScanner } catch (_: Throwable) { null } ?: return
        val cb = object : ScanCallback() {
            override fun onScanResult(t: Int, r: ScanResult) { Beacons.parse(r, cfg["beacon_uuid"])?.let { window.add(it) } }
            override fun onBatchScanResults(l: MutableList<ScanResult>) { l.forEach { onScanResult(0, it) } }
        }
        try { scanner.startScan(null, ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), cb); scanCb = cb } catch (_: Throwable) {}
    }

    @android.annotation.SuppressLint("MissingPermission")
    private fun stopScanNow() {
        val cb = scanCb ?: return
        scanCb = null
        try { getSystemService(BluetoothManager::class.java).adapter?.bluetoothLeScanner?.stopScan(cb) } catch (_: Throwable) {}
    }

    private fun startTimers() {
        timer = Timer(true).apply {
            schedule(object : TimerTask() { override fun run() { try {
                main.post { remote.tick() }
                val now = System.currentTimeMillis()
                val every = (cfg["heartbeat_s"].toLongOrNull() ?: 30L).coerceIn(10L, 30L) * 1000
                if (now - lastHeartbeat >= every) { lastHeartbeat = now; heartbeat() }
            } catch (_: Throwable) { } } }, 5000, 1000)
            schedule(object : TimerTask() { override fun run() { try { reportHeartRate() } catch (_: Throwable) { } } }, 60000, 300000)
        }
    }

    private var lowNotified = false
    private var lowAt = 0L

    /** 电量低于 20% 且没在充电:振动 + 通知提醒护理师到护士站换表/充电(之后每 30 分钟再提醒一次);回升到 25% 或开始充电后复位。 */
    private fun checkBattery(bat: Int, charging: Boolean) {
        val wasLow = WatchState.lowBattery
        WatchState.battery = bat; WatchState.charging = charging
        val nm = getSystemService(NotificationManager::class.java)
        if (WatchState.lowBattery) {
            val t = System.currentTimeMillis()
            if (!lowNotified || t - lowAt >= LOW_REMIND_MS) {
                lowNotified = true; lowAt = t
                vibrate(longArrayOf(0, 200, 150, 200, 150, 200))
                val open = PendingIntent.getActivity(this, 1, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
                nm.notify(2, Notification.Builder(this, CH_BATTERY).setSmallIcon(android.R.drawable.stat_notify_error)
                    .setContentTitle("手表电量只剩 $bat%").setContentText("请到护士站换一块备用表,没电会收不到呼叫").setContentIntent(open).build())
            }
        } else if (bat >= WatchState.LOW_BATTERY + 5 || charging) {
            lowNotified = false; nm.cancel(2)
        }
        if (wasLow != WatchState.lowBattery) WatchState.update { }      // 刷新界面上的提示条
    }

    @Suppress("DEPRECATION")
    private fun wifiRssi(): Int? = try {
        applicationContext.getSystemService(WifiManager::class.java)?.connectionInfo?.rssi?.takeIf { it in -126..-1 }
    } catch (_: Throwable) { null }

    private fun heartbeat() {
        // 用系统电量广播(粘性)读取:比 BATTERY_PROPERTY_CAPACITY 在各厂家系统上更可靠
        val i = registerReceiver(null, android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val scale = i?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        val bat = (i?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1).let { if (it < 0 || scale <= 0) 100 else it * 100 / scale }
        checkBattery(bat.coerceIn(0, 100), (i?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0) != 0)
        val p = JSONObject().put("battery", bat.coerceIn(0, 100)).put("charging", WatchState.charging)
            .put("caps", JSONArray(CAPS)).put("staff", WatchState.staffId ?: JSONObject.NULL)
        wifiRssi()?.let { p.put("wifi_rssi", it) }
        bus?.publish(Proto.tHeartbeat(cfg.store, cfg.deviceId), Proto.envelope(cfg.store, cfg.deviceId, Proto.HEARTBEAT, remote.decorate(p)), qos0 = true)
    }

    private fun reportLocation() {
        val fake = fakeReadings
        val rs = if (!fake.isNullOrEmpty()) fake else window.drain()
        if (rs.isEmpty()) { WatchState.where = "未检测到信标"; return }
        WatchState.where = rs.maxByOrNull { it.rssi }!!.id
        val arr = JSONArray().apply { rs.forEach { put(JSONObject().put("beacon_id", it.id).put("rssi", it.rssi)) } }
        bus?.publish(Proto.tLocation(cfg.store, cfg.deviceId), Proto.envelope(cfg.store, cfg.deviceId, Proto.LOCATION_REPORT,
            JSONObject().put("readings", arr)), qos0 = true)
        WatchState.update { }
    }

    /** One heart-rate sample per 5 min (sensor is on only for up to 30 s). Skipped if no sensor/permission. */
    private fun reportHeartRate() {
        if (!has(Manifest.permission.BODY_SENSORS)) return
        val sm = getSystemService(SensorManager::class.java)
        val sensor = sm.getDefaultSensor(Sensor.TYPE_HEART_RATE) ?: return
        val got = CountDownLatch(1); var bpm = 0
        val l = object : SensorEventListener {
            override fun onSensorChanged(e: SensorEvent) { if (e.values[0] > 0) { bpm = e.values[0].toInt(); got.countDown() } }
            override fun onAccuracyChanged(s: Sensor?, a: Int) {}
        }
        sm.registerListener(l, sensor, SensorManager.SENSOR_DELAY_NORMAL)
        try { got.await(30, TimeUnit.SECONDS) } finally { sm.unregisterListener(l) }
        if (bpm in 20..250) bus?.publish(Proto.tVitals(cfg.store, cfg.deviceId), Proto.envelope(cfg.store, cfg.deviceId,
            Proto.VITALS_REPORT, JSONObject().put("heart_rate", bpm)))
    }
}
