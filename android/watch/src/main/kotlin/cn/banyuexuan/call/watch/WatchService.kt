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
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Foreground service: MQTT link to the host, call alerts (vibrate + sound + notification),
 * BLE beacon scanning and periodic reports, heartbeat with battery, heart-rate upload.
 * The watch holds no business logic: the host decides who gets which call.
 */
class WatchService : Service() {
    companion object {
        @Volatile var fakeReadings: List<Beacons.Reading>? = null
        @Volatile var instance: WatchService? = null
        fun config(ctx: Context) = DeviceConfig(ctx, mapOf("mqtt_host" to "10.0.2.2", "mqtt_port" to "1883",
            "store_id" to "gx", "device_id" to "w-01", "api_base" to "http://10.0.2.2:8100", "api_key" to "dev-key", "mqtt_password" to "",
            "beacon_uuid" to "", "report_seconds" to "10"))
        private const val CH_SERVICE = "service"
        private const val CH_CALLS = "calls"
        private const val CH_BATTERY = "battery"
        private const val LOW_REMIND_MS = 30 * 60_000L
        private const val REMIND_MS = 15000L
        private const val MAX_REMINDS = 8
    }

    private lateinit var cfg: DeviceConfig
    private var bus: MqttBus? = null
    private var timer: Timer? = null
    private val window = Beacons.Window()
    private val main = Handler(Looper.getMainLooper())
    private var scanCb: ScanCallback? = null
    private var player: MediaPlayer? = null
    private var reminds = 0

    override fun onBind(i: Intent?): IBinder? = null
    override fun onStartCommand(i: Intent?, f: Int, id: Int) = START_STICKY

    override fun onCreate() {
        super.onCreate()
        instance = this
        cfg = config(this)
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CH_SERVICE, "运行状态", NotificationManager.IMPORTANCE_LOW))
        nm.createNotificationChannel(NotificationChannel(CH_BATTERY, "低电量提醒", NotificationManager.IMPORTANCE_DEFAULT))
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
        startBus(); startTimers(); main.post(scanCycle)
        bubble = Bubble(this); WatchState.listen(bubbleListener); bubble?.refresh()
    }

    private var bubble: Bubble? = null
    private val bubbleListener: () -> Unit = { bubble?.refresh() }

    override fun onDestroy() {
        WatchState.unlisten(bubbleListener); bubble?.hide()
        instance = null
        val old = bus; bus = null; WatchState.connected = false
        timer?.cancel(); main.removeCallbacksAndMessages(null); old?.disconnect(); stopScan(); player?.release(); player = null
        super.onDestroy()
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

    private fun onMessage(m: JSONObject) {
        if (Looper.myLooper() != Looper.getMainLooper()) { main.post { onMessage(m) }; return }
        val p = m.optJSONObject("payload") ?: JSONObject()
        val id = m.optString("call_id")
        if (m.optString("store_id") != cfg.store) return
        if (m.optString("type") == "WATCH_CONTEXT") {
            applyContext(p); return
        }
        if (p.has("binding_id") && p.optString("binding_id") != WatchState.context?.optJSONObject("binding")?.optString("bind_id")) return
        if (p.has("delivery_id")) {
            if (m.optString("type") == "TEAM_MESSAGE" && WatchState.alerts.values.any { it.status != "completed" }) return
            if (m.optString("type") != "TEAM_MESSAGE") watchCommand("WATCH_RECEIVED", JSONObject().put("delivery_id", p.optString("delivery_id")))
        }
        when (m.optString("type")) {
            Proto.CALL_DISPATCH -> {
                stopAudio()
                fun str(k: String): String? = if (p.isNull(k)) null else p.optString(k).ifBlank { null }   // JSON null must not become "null"
                val a = Alert(id, str("room_no") ?: "", str("label") ?: str("service_code") ?: "",
                    str("voice_url"), str("text"), p.optString("priority") == "emergency", str("reason") ?: "", p.optString("status", "pending"))
                WatchState.update {
                    val fresh = !WatchState.alerts.containsKey(id)
                    if (fresh) WatchState.alerts[id] = a
                    if (fresh) { alertUser(a); startReminders() }
                }
            }
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
            "HELP_ALERT" -> WatchState.update {
                val hid = p.optString("help_id")
                if (!WatchState.helps.containsKey(hid)) { WatchState.helps[hid] = p; pulse(longArrayOf(0, 250, 150, 250, 150, 250)) }
            }
            "HELP_STATE" -> WatchState.update {
                val hid = p.optString("help_id")
                val status = p.optString("status")
                if (status == "claimed" && p.optString("owner") == WatchState.context?.optString("staff_id")) {
                    WatchState.helps[hid] = p.put("status", "claimed")
                } else if (status == "claimed" || status == "complete" || status == "cancel") WatchState.helps.remove(hid)
                WatchState.helpStatus = when (status) {
                    "sent" -> "求助已发出"
                    "claimed" -> "${p.optString("by")}已响应"
                    "escalated" -> "无人响应，已升级"
                    else -> "求助已结束"
                }
            }
            "TEAM_MESSAGE" -> WatchState.update {
                val mid = p.optString("message_id")
                if (!WatchState.messages.containsKey(mid)) {
                    WatchState.messages[mid] = p
                    while (WatchState.messages.size > 100) WatchState.messages.remove(WatchState.messages.keys.first())
                    getSharedPreferences("watch_employee", MODE_PRIVATE).edit()
                        .putString("binding_id", WatchState.context?.optJSONObject("binding")?.optString("bind_id"))
                        .putString("messages", JSONArray(WatchState.messages.values.toList()).toString()).commit()
                    pulse(longArrayOf(0, 120))
                }
                watchCommand("WATCH_RECEIVED", JSONObject().put("delivery_id", p.optString("delivery_id")))
            }
            "TEAM_SENT" -> WatchState.update { WatchState.notice = "消息已存入主机，等待送达" }
            Proto.CMD_REJECTED -> WatchState.update {
                WatchState.notice = when (p.optString("reason")) {
                    "already_taken_or_closed" -> { WatchState.alerts.remove(id); "已被别人接走" }
                    "complete_not_allowed", "arrive_not_allowed" -> "请先到达房间并确认到场"
                    else -> "操作失败:" + p.optString("reason")
                }
            }
        }
    }

    private fun pulse(pattern: LongArray) {
        val vib = if (android.os.Build.VERSION.SDK_INT >= 31) getSystemService(VibratorManager::class.java).defaultVibrator else getSystemService(Vibrator::class.java)
        vib.vibrate(VibrationEffect.createWaveform(pattern, -1))
    }

    private fun stopAudio() { main.post { player?.release(); player = null } }

    fun watchCommand(type: String, payload: JSONObject = JSONObject()) {
        if (bus?.publish("${cfg.store}/watch/${cfg.deviceId}", Proto.envelope(cfg.store, cfg.deviceId, type, payload.put("binding_id", WatchState.context?.optJSONObject("binding")?.optString("bind_id")))) != true)
            WatchState.update { WatchState.notice = "网络未连接，请恢复后重试" }
    }

    fun requestHelp() {
        watchCommand("HELP_REQUEST")
        pulse(longArrayOf(0, 200))
        WatchState.update { WatchState.helpStatus = "正在发送，等待主机确认" }
    }

    private fun applyContext(p: JSONObject) {
        val next = p.optJSONObject("context")
        val previous = WatchState.context?.optJSONObject("binding")?.optString("bind_id")
        val changed = previous != next?.optJSONObject("binding")?.optString("bind_id")
        val stored = getSharedPreferences("watch_employee", MODE_PRIVATE)
        val restore = next != null && stored.getString("binding_id", null) == next.optJSONObject("binding")?.optString("bind_id")
        if (next == null || changed) {
            WatchState.alerts.keys.forEach { getSystemService(NotificationManager::class.java).cancel(it.hashCode()) }
            stopAudio(); WatchState.alerts.clear(); WatchState.messages.clear(); WatchState.helps.clear()
            WatchState.where = "—"; WatchState.helpStatus = ""; window.drain()
            cacheDir.listFiles()?.forEach { it.deleteRecursively() }
            if (!restore && !stored.edit().clear().commit()) { WatchState.update { WatchState.notice = "本地清空失败，请联系管理员" }; return }
            if (restore) try {
                val rows = JSONArray(stored.getString("messages", "[]"))
                for (i in 0 until rows.length()) rows.getJSONObject(i).let { WatchState.messages[it.optString("message_id")] = it }
            } catch (_: Exception) { stored.edit().clear().commit() }
        }
        // 员工数据只保存在内存；设备 MQTT/API 凭据属于硬件，不随员工迁移或擦除。
        WatchState.directory = p.optJSONArray("directory") ?: JSONArray()
        WatchState.roles = p.optJSONObject("roles") ?: JSONObject()
        WatchState.rollbackVersion = if (p.isNull("rollback_version")) null else p.optInt("rollback_version")
        val active = p.optJSONArray("active_help") ?: JSONArray()
        for (i in 0 until active.length()) {
            val h = active.getJSONObject(i)
            if (h.optString("owner") == next?.optString("staff_id")) WatchState.helps[h.optString("id")] = h.put("help_id", h.optString("id")).put("status", "claimed")
            if (h.optString("sender") == next?.optString("staff_id")) WatchState.helpStatus = if (h.optString("state") == "claimed") "已有同事响应" else "求助已发出"
        }
        WatchState.context = next; WatchState.bound = next != null
        WatchState.policy = p.optJSONObject("policy") ?: JSONObject()
        if (p.optString("state") == "RESETTING") {
            watchCommand("WATCH_RESET_ACK", JSONObject().put("reset_id", p.optString("reset_id")))
        }
        WatchState.update { }
    }

    // ---- commands from the UI --------------------------------------
    fun command(type: String, callId: String, payload: JSONObject = JSONObject()) {
        bus?.publish(Proto.tAck(cfg.store, callId), Proto.envelope(cfg.store, cfg.deviceId, type, payload.put("binding_id", WatchState.context?.optJSONObject("binding")?.optString("bind_id")), callId))
    }

    fun playVoice(voiceUrl: String, colleague: Boolean = false) {
        val binding = WatchState.context?.optJSONObject("binding")?.optString("bind_id")
        Thread {
            val f = File(cacheDir, "v-$binding-" + voiceUrl.substringAfterLast('/'))
            if (f.exists() || Http.download(cfg.apiBase, "", "/watch-device/${cfg.deviceId}" + voiceUrl, f, binding)) main.post {
                if (binding != WatchState.context?.optJSONObject("binding")?.optString("bind_id") ||
                    (colleague && WatchState.alerts.values.any { it.status != "completed" })) { f.delete(); return@post }
                player?.release()
                player = MediaPlayer().apply { setDataSource(f.path); setOnCompletionListener { it.release(); player = null }; prepare(); start() }
            } else WatchState.update { WatchState.notice = "留言下载失败" }
        }.start()
    }

    // ---- alerting ----------------------------------------------------
    private fun alertUser(a: Alert) {
        // 紧急:连续震动 6 次(每次 0.6 秒,间隔 0.25 秒);普通呼叫震 2 次
        val pattern = if (a.emergency) LongArray(12) { if (it == 0) 0L else if (it % 2 == 1) 600L else 250L } else longArrayOf(0, 400, 200, 400)
        val vib = if (android.os.Build.VERSION.SDK_INT >= 31)
            getSystemService(VibratorManager::class.java).defaultVibrator else @Suppress("DEPRECATION") getSystemService(Vibrator::class.java)
        vib.vibrate(VibrationEffect.createWaveform(pattern, -1))
        try { RingtoneManager.getRingtone(this, RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)).play() } catch (_: Exception) {}
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(this, CH_CALLS).setSmallIcon(android.R.drawable.stat_notify_chat)
            .setContentTitle((if (a.emergency) "【紧急】" else "") + "${a.roomNo} 房 ${a.label}")
            .setContentText(if (a.voiceUrl != null) "有语音留言" else "点击处理").setContentIntent(open)
            .setFullScreenIntent(open, true).setAutoCancel(true).build()
        getSystemService(NotificationManager::class.java).notify(a.callId.hashCode(), n)
    }

    private fun startReminders() { reminds = 0; main.removeCallbacks(remind); main.postDelayed(remind, REMIND_MS) }
    private val remind = object : Runnable {
        override fun run() {
            val pending = WatchState.alerts.values.filter { it.status == "pending" }
            if (pending.isEmpty() || ++reminds > MAX_REMINDS) return
            alertUser(pending.first()); main.postDelayed(this, REMIND_MS)
        }
    }

    // ---- BLE + timers ----------------------------------------------
    private fun has(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    private fun startScan() {
        if (android.os.Build.VERSION.SDK_INT >= 31) {
            if (!has(Manifest.permission.BLUETOOTH_SCAN)) return
        } else if (!has(Manifest.permission.ACCESS_FINE_LOCATION)) return
        val scanner = getSystemService(BluetoothManager::class.java).adapter?.bluetoothLeScanner ?: return
        val cb = object : ScanCallback() {
            override fun onScanResult(t: Int, r: ScanResult) { Beacons.parse(r, cfg["beacon_uuid"])?.let { window.add(it) } }
            override fun onBatchScanResults(l: MutableList<ScanResult>) { l.forEach { onScanResult(0, it) } }
        }
        try { scanner.startScan(null, ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_POWER).build(), cb); scanCb = cb } catch (_: Exception) {}
    }

    private fun stopScan() {
        val cb = scanCb ?: return
        scanCb = null
        try { getSystemService(BluetoothManager::class.java).adapter?.bluetoothLeScanner?.stopScan(cb) } catch (_: Exception) {}
    }

    private val scanCycle = object : Runnable {
        override fun run() {
            val interval = WatchState.policy.optLong("scan_interval_seconds", 10).coerceIn(3, 300) * 1000
            val duration = WatchState.policy.optLong("scan_window_seconds", 2).coerceIn(1, interval / 1000) * 1000
            startScan()
            main.postDelayed({ stopScan() }, duration)
            main.postDelayed(this, interval)
        }
    }

    private fun startTimers() {
        val every = (cfg["report_seconds"].toLongOrNull() ?: 10L) * 1000
        timer = Timer(true).apply {
            schedule(object : TimerTask() { override fun run() = heartbeat() }, 5000, 30000)
            schedule(object : TimerTask() { override fun run() = reportLocation() }, every, every)
            schedule(object : TimerTask() { override fun run() = reportHeartRate() }, 60000, 300000)
        }
    }

    private var lowNotified = false
    private var lowAt = 0L

    /** 电量低于 20% 且没在充电:振动 + 通知提醒护理师充电(之后每 30 分钟再提醒一次);回升到 25% 或开始充电后复位。 */
    private fun checkBattery(bat: Int, charging: Boolean) {
        val wasLow = WatchState.lowBattery
        WatchState.battery = bat; WatchState.charging = charging
        val nm = getSystemService(NotificationManager::class.java)
        if (WatchState.lowBattery) {
            val t = System.currentTimeMillis()
            if (!lowNotified || t - lowAt >= LOW_REMIND_MS) {
                lowNotified = true; lowAt = t
                val vib = if (android.os.Build.VERSION.SDK_INT >= 31)
                    getSystemService(VibratorManager::class.java).defaultVibrator else @Suppress("DEPRECATION") getSystemService(Vibrator::class.java)
                vib.vibrate(VibrationEffect.createWaveform(longArrayOf(0, 200, 150, 200, 150, 200), -1))
                val open = PendingIntent.getActivity(this, 1, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
                nm.notify(2, Notification.Builder(this, CH_BATTERY).setSmallIcon(android.R.drawable.stat_notify_error)
                    .setContentTitle("手表电量只剩 $bat%").setContentText("请到护士站换表，避免漏接呼叫").setContentIntent(open).build())
            }
        } else if (bat >= WatchState.LOW_BATTERY + 5 || charging) {
            lowNotified = false; nm.cancel(2)
        }
        if (wasLow != WatchState.lowBattery) WatchState.update { }      // 刷新界面上的提示条
    }

    private var lastBatteryAt = 0L
    private fun heartbeat() {
        // 用系统电量广播(粘性)读取:比 BATTERY_PROPERTY_CAPACITY 在各厂家系统上更可靠
        val i = registerReceiver(null, android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val scale = i?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        val bat = (i?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1).let { if (it < 0 || scale <= 0) 100 else it * 100 / scale }
        checkBattery(bat.coerceIn(0, 100), (i?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0) != 0)
        val now = System.currentTimeMillis()
        val reportBattery = now - lastBatteryAt >= WatchState.policy.optLong("battery_seconds", 900).coerceAtLeast(30) * 1000
        if (reportBattery) lastBatteryAt = now
        val wifi = try { (applicationContext.getSystemService(Context.WIFI_SERVICE) as android.net.wifi.WifiManager).connectionInfo.rssi } catch (_: Exception) { -127 }
        bus?.publish(Proto.tHeartbeat(cfg.store, cfg.deviceId), Proto.envelope(cfg.store, cfg.deviceId, Proto.HEARTBEAT,
            JSONObject().put("battery", bat.coerceIn(0, 100)).put("charging", WatchState.charging).put("wifi_rssi", wifi).put("battery_sample", reportBattery)), qos0 = true)
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
