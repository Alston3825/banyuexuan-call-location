package cn.banyuexuan.call.watch

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import cn.banyuexuan.call.common.DeviceConfig
import cn.banyuexuan.call.common.PressFx
import cn.banyuexuan.call.common.Proto
import org.json.JSONObject

class MainActivity : Activity() {
    private lateinit var box: LinearLayout
    private lateinit var scroll: ScrollView
    private lateinit var head: TextView
    private val listener: () -> Unit = { render() }

    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        // 上次崩溃过:先把错误原因显示出来(拍照发给开发者),点"继续"再进入正常界面
        cn.banyuexuan.call.common.CrashLog.pending(this)?.let { t ->
            val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(16), dp(24), dp(16), dp(16)); setBackgroundColor(Color.WHITE)
                addView(TextView(context).apply { text = "上次运行出错,请拍照发给开发者"; textSize = 16f; typeface = Typeface.DEFAULT_BOLD; setTextColor(Color.parseColor("#c0392b")) })
                addView(TextView(context).apply { text = t; textSize = 9f; setTextColor(Color.BLACK) })
                addView(Button(context).apply { text = "继续进入应用"; setOnClickListener { cn.banyuexuan.call.common.CrashLog.clear(context); recreate() } }) }
            actionBar?.hide()
            setContentView(ScrollView(this).apply { addView(col)
                if (android.os.Build.VERSION.SDK_INT >= 30) setOnApplyWindowInsetsListener { v, i ->
                    val b = i.getInsets(android.view.WindowInsets.Type.systemBars()); v.setPadding(0, b.top, 0, b.bottom); i } })
            try { startForegroundService(Intent(this, WatchService::class.java)) } catch (_: Exception) {}
            return
        }
        box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(12), dp(10), dp(12), dp(10)); setBackgroundColor(Color.parseColor("#f6f1ea")) }
        scroll = ScrollView(this).apply { addView(box) }
        actionBar?.hide()
        if (android.os.Build.VERSION.SDK_INT >= 30)              // Type.systemBars() 需要 API 30;更旧的系统不用处理
            scroll.setOnApplyWindowInsetsListener { v, i ->      // edge-to-edge: keep content clear of status/navigation bars
                val b = i.getInsets(android.view.WindowInsets.Type.systemBars()); v.setPadding(0, b.top, 0, b.bottom); i }
        setContentView(scroll)
        askPermissions()
    }

    private var updInfo: cn.banyuexuan.call.common.Updater.Info? = null
    private var updText: String? = null
    private val updHandler = Handler(Looper.getMainLooper())
    private val updTick = object : Runnable {
        override fun run() {
            cn.banyuexuan.call.common.Updater.checkAsync(this@MainActivity, WatchService.config(this@MainActivity), "watch") { i -> updInfo = i; render() }
            updHandler.postDelayed(this, 10 * 60_000L)
        }
    }
    override fun onStart() { super.onStart(); if (!::box.isInitialized) return; WatchState.update { WatchState.appVisible = true }; WatchState.listen(listener); render(); updHandler.post(updTick) }
    override fun onStop() { super.onStop(); if (!::box.isInitialized) return; WatchState.update { WatchState.appVisible = false }; WatchState.unlisten(listener); updHandler.removeCallbacks(updTick) }

    private fun askPermissions() {
        val need = listOf(Manifest.permission.POST_NOTIFICATIONS, Manifest.permission.BLUETOOTH_SCAN,
            Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.BODY_SENSORS)
            .filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (need.isEmpty()) afterPermissions() else requestPermissions(need.toTypedArray(), 1)
    }

    override fun onRequestPermissionsResult(c: Int, p: Array<out String>, r: IntArray) { afterPermissions() }

    private fun afterPermissions() {
        try { startForegroundService(Intent(this, WatchService::class.java)) } catch (_: Exception) {}
        if (!Settings.canDrawOverlays(this))                     // 悬浮球需要"显示在其他应用上层"权限
            try { startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName"))) } catch (_: Exception) {}
        val pm = getSystemService(PowerManager::class.java)
        if (!pm.isIgnoringBatteryOptimizations(packageName))      // keep the service alive in the background
            try { startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))) } catch (_: Exception) {}
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
    private fun bg(c: String) = GradientDrawable().apply { setColor(Color.parseColor(c)); cornerRadius = dp(12).toFloat() }
    private fun btn(t: String, c: String, f: () -> Unit) = Button(this).apply { text = t; textSize = 20f; minHeight = 0; minimumHeight = 0; isAllCaps = false; setPadding(dp(4), dp(4), dp(4), dp(4)); setTextColor(Color.WHITE); background = bg(c)
        PressFx.attach(this)
        setOnClickListener {                                     // 先让按钮闪一下、显示 ✓,再执行操作(操作会刷新界面)
            isEnabled = false; PressFx.confirm(this, "✓ $t", 600); postDelayed({ f() }, 220); postDelayed({ isEnabled = true }, 1500) } }

    private fun render() {
        box.removeAllViews()
        syncBack()
        if (hist != HistView.NONE) { renderHistory(); return }
        val cfg = WatchService.config(this)
        head = TextView(this).apply { textSize = 16f; typeface = Typeface.DEFAULT_BOLD
            text = (if (WatchState.connected) "● 已连接" else "○ 未连接") + "   ${cfg.deviceId}\n位置:${WatchState.where}"
            setTextColor(if (WatchState.connected) Color.parseColor("#1b7f3b") else Color.parseColor("#c0392b"))
            setOnLongClickListener { showSettings(); true } }
        box.addView(head)
        updInfo?.let { u -> box.addView(TextView(this).apply {          // 有新版本:蓝色方块,点一下更新
            text = updText ?: "发现新版本 ${u.name}\n点击更新"; textSize = 18f; typeface = Typeface.DEFAULT_BOLD; setTextColor(Color.WHITE)
            gravity = Gravity.CENTER; background = bg("#2f6fdb"); setPadding(dp(12), dp(14), dp(12), dp(14))
            setOnClickListener { cn.banyuexuan.call.common.Updater.update(this@MainActivity, WatchService.config(this@MainActivity), "watch", u) { t -> updText = t; render() } } },
            LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(8) }) }
        if (WatchState.lowBattery) box.addView(TextView(this).apply {
            text = "🔋 电量只剩 ${WatchState.battery}%,请尽快充电"; textSize = 18f; typeface = Typeface.DEFAULT_BOLD; setTextColor(Color.WHITE)
            background = bg("#c0392b"); setPadding(dp(12), dp(10), dp(12), dp(10)) }, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(8) })
        WatchState.notice?.let { Toast.makeText(this, it, Toast.LENGTH_LONG).show(); WatchState.notice = null }
        if (WatchState.alerts.isEmpty()) box.addView(TextView(this).apply { text = "暂无呼叫"; textSize = 22f; gravity = Gravity.CENTER; setPadding(0, dp(40), 0, 0) })
        WatchState.alerts.values.forEach { a -> box.addView(card(a), LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(10) }) }
        box.addView(btn("📋 我的记录", "#7a6a5c") { openHistory() }, LinearLayout.LayoutParams(-1, dp(64)).apply { topMargin = dp(14) })
        scroll.post { scroll.scrollTo(0, 0) }
    }


    // ---- 我的记录:最近 30 天接过的单(数据在主机),可回看时间线、听留言、看转文字 ----
    private enum class HistView { NONE, LIST, DETAIL }
    private var hist = HistView.NONE
    private var histItems: org.json.JSONArray? = null
    private var histMsg = ""
    private var histSel: JSONObject? = null

    private fun openHistory() { hist = HistView.LIST; histItems = null; histMsg = "加载中…"; render(); loadHistory() }

    private fun loadHistory() {
        val cfg = WatchService.config(this)
        Thread {
            val t = cn.banyuexuan.call.common.Http.getText(cfg.apiBase, cfg.apiKey, "/watches/${cfg.deviceId}/history?days=30&limit=100")
            runOnUiThread {
                if (hist == HistView.NONE) return@runOnUiThread
                try { histItems = t?.let { org.json.JSONArray(it) }; histMsg = if (t == null) "加载失败,请检查网络后重试" else "" }
                catch (_: Exception) { histItems = null; histMsg = "加载失败" }
                render()
            }
        }.start()
    }

    /** 主机时间是 UTC(…+00:00),换成手表本地时区再显示。 */
    private fun local(iso: String?): java.time.ZonedDateTime? = try {
        if (iso.isNullOrBlank()) null else java.time.OffsetDateTime.parse(iso).atZoneSameInstant(java.time.ZoneId.systemDefault()) } catch (_: Exception) { null }
    private fun hhmm(iso: String?): String = local(iso)?.format(java.time.format.DateTimeFormatter.ofPattern("MM/dd HH:mm")) ?: ""
    private fun stateText(s: String) = mapOf("completed" to "已完成", "claimed" to "已接单", "arrived" to "已到场", "cancelled" to "已取消")[s] ?: s

    @android.annotation.SuppressLint("GestureBackNavigation")
    @Deprecated("old systems") override fun onBackPressed() { if (hist == HistView.NONE) super.onBackPressed() else goBack() }   // 旧系统(手表<Android 13)走这里
    private val backCb: Any? by lazy { if (android.os.Build.VERSION.SDK_INT >= 33) android.window.OnBackInvokedCallback { goBack() } else null }
    private fun syncBack() {                                      // Android 13+:仅在记录页里接管返回手势
        if (android.os.Build.VERSION.SDK_INT < 33) return
        val cb = backCb as android.window.OnBackInvokedCallback
        onBackInvokedDispatcher.unregisterOnBackInvokedCallback(cb)
        if (hist != HistView.NONE) onBackInvokedDispatcher.registerOnBackInvokedCallback(android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT, cb)
    }
    private fun goBack() {
        when (hist) { HistView.DETAIL -> { hist = HistView.LIST; render() }; HistView.LIST -> { hist = HistView.NONE; render() }; else -> {} }
    }

    private fun renderHistory() {
        val back = btn(if (hist == HistView.DETAIL) "← 返回记录" else "← 返回", "#7a6a5c") { goBack() }
        box.addView(back, LinearLayout.LayoutParams(-1, dp(56)))
        if (hist == HistView.DETAIL) { histSel?.let { renderDetail(it) }; scroll.post { scroll.scrollTo(0, 0) }; return }
        box.addView(TextView(this).apply { text = "我的记录(最近 30 天)"; textSize = 18f; typeface = Typeface.DEFAULT_BOLD; setTextColor(Color.parseColor("#3a2e25")); setPadding(0, dp(10), 0, dp(4)) })
        val items = histItems
        if (items == null || items.length() == 0)
            box.addView(TextView(this).apply { text = if (items != null) "暂无记录" else histMsg; textSize = 18f; gravity = Gravity.CENTER; setPadding(0, dp(30), 0, 0) })
        if (items == null) box.addView(btn("重试", "#2f6fdb") { histMsg = "加载中…"; render(); loadHistory() }, LinearLayout.LayoutParams(-1, dp(56)).apply { topMargin = dp(10) })
        for (i in 0 until (items?.length() ?: 0)) {
            val o = items!!.getJSONObject(i)
            val tl = o.optJSONArray("timeline")
            val at = if (tl != null && tl.length() > 0) tl.getJSONObject(0).optString("at") else ""
            box.addView(LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL; background = bg(if (o.optBoolean("emergency")) "#fbe3df" else "#ffffff"); setPadding(dp(14), dp(10), dp(14), dp(10))
                addView(TextView(context).apply { textSize = 20f; typeface = Typeface.DEFAULT_BOLD; setTextColor(Color.parseColor("#3a2e25"))
                    text = (if (o.optBoolean("emergency")) "紧急 " else "") + "${o.optString("room_no")} 房  ${o.optString("label")}" })
                addView(TextView(context).apply { textSize = 14f; setTextColor(Color.parseColor("#7a6a5c"))
                    text = hhmm(at) + "  ·  " + stateText(o.optString("state")) + if (o.optString("voice_url").isNotBlank() && o.optString("voice_url") != "null") "  ·  🎤" else "" })
                setOnClickListener { histSel = o; hist = HistView.DETAIL; render() }
            }, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(8) })
        }
        scroll.post { scroll.scrollTo(0, 0) }
    }

    private fun renderDetail(o: JSONObject) {
        val names = mapOf("call.created" to "呼叫发出", "call.claim" to "接单", "call.arrive" to "到场", "call.complete" to "完成", "call.cancel" to "取消", "call.transfer" to "转交")
        box.addView(LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; background = bg("#ffffff"); setPadding(dp(14), dp(10), dp(14), dp(12))
            addView(TextView(context).apply { textSize = 24f; typeface = Typeface.DEFAULT_BOLD; setTextColor(Color.parseColor("#3a2e25"))
                text = (if (o.optBoolean("emergency")) "紧急 " else "") + "${o.optString("room_no")} 房  ${o.optString("label")}" })
            addView(TextView(context).apply { textSize = 15f; setTextColor(Color.parseColor("#7a6a5c")); text = stateText(o.optString("state")) })
            val tl = o.optJSONArray("timeline")
            for (i in 0 until (tl?.length() ?: 0)) tl!!.getJSONObject(i).let { e ->
                addView(TextView(context).apply { textSize = 17f; setPadding(0, dp(4), 0, 0); text = "${hhmm(e.optString("at"))}   ${names[e.optString("kind")] ?: e.optString("kind").removePrefix("call.")}" }) }
            o.optString("text").takeIf { it.isNotBlank() && it != "null" }?.let { addView(TextView(context).apply { textSize = 18f; setPadding(0, dp(8), 0, 0); text = "留言:$it" }) }
            o.optString("transcript").takeIf { it.isNotBlank() && it != "null" }?.let { addView(TextView(context).apply { textSize = 18f; setPadding(0, dp(8), 0, 0); text = "转文字:$it" }) }
            o.optString("voice_url").takeIf { it.isNotBlank() && it != "null" }?.let { u ->
                addView(btn("▶ 听留言", "#2f6fdb") { WatchService.instance?.playVoice(u) }, LinearLayout.LayoutParams(-1, dp(TILE_DP)).apply { topMargin = dp(10) }) }
        }, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(10) })
    }

    private fun card(a: WatchState.Alert): LinearLayout {
        val svc = WatchService.instance
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; background = bg(if (a.emergency) "#fbe3df" else if (a.status == "completed") "#e3f1e6" else "#ffffff"); setPadding(dp(14), dp(10), dp(14), dp(12))
            addView(TextView(context).apply { textSize = 26f; typeface = Typeface.DEFAULT_BOLD; setTextColor(if (a.emergency) Color.parseColor("#c0392b") else Color.parseColor("#3a2e25"))
                text = (if (a.emergency) "紧急 " else "") + "${a.roomNo} 房  ${a.label}" })
            val st = mapOf("pending" to "待接单", "accepted" to "已接单,请前往", "arrived" to "已到场", "completed" to "✓ 已完成")[a.status] ?: a.status
            addView(TextView(context).apply { textSize = 15f; text = st + if (a.reason == "transfer") " · 转交" else ""; setTextColor(Color.parseColor("#7a6a5c")) })
            a.text?.let { addView(TextView(context).apply { textSize = 18f; text = it }) }
            // 进度条:走到哪一步一眼看出来
            val step = when (a.status) { "accepted", "accepting" -> 1; "arrived" -> 2; "completed" -> 3; else -> 0 }
            addView(TextView(context).apply { textSize = 14f; setTextColor(Color.parseColor("#7a6a5c")); setPadding(0, dp(4), 0, 0)
                text = listOf("接单", "到场", "完成").mapIndexed { i, n -> (if (i < step) "● " else "○ ") + n }.joinToString("   ") })
            // 两个固定位置的方块:左边 接单 → 我已到场 → ✓已到场;右边 完成(到场前是灰色不可用,
            // 到场后才变蓝可点;到场可手动点,也可由信标自动识别)
            val complete: () -> Unit = { svc?.command(Proto.CALL_COMPLETE, a.callId, JSONObject().put("method", "onsite").put("reason", "已处理")); Unit }
            val (left, right) = when (a.status) {
                "pending" -> btn("接单", "#1b7f3b") { svc?.command(Proto.CALL_ACCEPT, a.callId); a.status = "accepting"; render() } to null
                "accepting" -> badge("接单中…", "#9a9088") to null
                "accepted" -> btn("我已到场", "#b5835a") { svc?.command(Proto.CALL_ARRIVE, a.callId) } to
                    badge("完成", "#b8b0a8").apply { setOnClickListener { Toast.makeText(context, "请先点“我已到场”", Toast.LENGTH_SHORT).show() } }
                "arrived" -> badge("✓ 已到场", "#7fae8a") to btn("完成", "#2f6fdb", complete)
                else -> null to null
            }
            val rows = mutableListOf(listOf(left, right))
            a.voiceUrl?.let { u -> rows += listOf(listOf(btn("▶ 听留言", "#2f6fdb") { svc?.playVoice(u) }, null)) }
            rows.forEach { pair ->
                if (pair.all { it == null }) return@forEach
                addView(LinearLayout(context).apply {
                    orientation = LinearLayout.HORIZONTAL
                    pair.forEach { v -> addView(v ?: View(context), LinearLayout.LayoutParams(0, dp(TILE_DP), 1f).apply { setMargins(dp(4), dp(6), dp(4), 0) }) }   // 空位也占位,方块大小、位置不变
                }, LinearLayout.LayoutParams(-1, -2))
            }
        }
    }

    /** 不能点的状态方块(如"✓ 已到场"、"接单中…"),和按钮同样大小,只显示状态。 */
    private fun badge(t: String, c: String) = TextView(this).apply { text = t; textSize = 20f; gravity = Gravity.CENTER; setTextColor(Color.WHITE); background = bg(c) }

    /** 设置页的一项:上方是"中文名(键名)",下方灰字提示,再下面是单行输入框(不能换行,防止地址被拆成两行)。 */
    private fun settingRow(parent: LinearLayout, k: String, value: String): EditText {
        parent.addView(TextView(this).apply { text = DeviceConfig.labelOf(k); textSize = 16f; typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.parseColor("#3a2e25")); setPadding(0, dp(12), 0, 0) })
        DeviceConfig.hintOf(k).takeIf { it.isNotEmpty() }?.let { h ->
            parent.addView(TextView(this).apply { text = h; textSize = 12f; setTextColor(Color.parseColor("#8a7a6c")) }) }
        return EditText(this).apply { setSingleLine(true); inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_NEXT; setText(value) }.also { parent.addView(it) }
    }

    private fun showSettings() {
        val cfg = WatchService.config(this)
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(16), dp(8), dp(16), 0) }
        val fields = cfg.keys.associateWith { k -> settingRow(col, k, cfg[k]) }
        AlertDialog.Builder(this).setTitle("设备设置  " + cn.banyuexuan.call.common.Updater.versionText(this)).setView(ScrollView(this).apply { addView(col) })
            .setPositiveButton("保存并重启服务") { _, _ ->
                fields.forEach { (k, v) -> cfg[k] = v.text.toString() }
                stopService(Intent(this, WatchService::class.java)); startForegroundService(Intent(this, WatchService::class.java))
            }.setNegativeButton("取消", null).show()
    }

    companion object { const val TILE_DP = 92 }      // 方块按钮高度,够大不易误点
}
