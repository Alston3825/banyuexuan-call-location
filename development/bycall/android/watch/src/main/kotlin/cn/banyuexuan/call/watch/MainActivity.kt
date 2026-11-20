package cn.banyuexuan.call.watch

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import cn.banyuexuan.call.common.Http
import cn.banyuexuan.call.common.PressFx
import cn.banyuexuan.call.common.Proto
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder

class MainActivity : Activity() {
    private lateinit var box: LinearLayout
    private lateinit var scroll: ScrollView
    private lateinit var head: TextView
    private val listener: () -> Unit = { render() }
    private val ui = Handler(Looper.getMainLooper())

    // 页面:首页 / 我的记录(列表、详情)/ 同事消息(首页、对话)
    private enum class Page { HOME, HIST, HIST_DETAIL, CHAT_HOME, CHAT }
    private var page = Page.HOME

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
            try { startForegroundService(Intent(this, WatchService::class.java)) } catch (_: Throwable) {}
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
        handleOpen(intent)
    }

    override fun onNewIntent(i: Intent?) { super.onNewIntent(i); handleOpen(i) }
    private fun handleOpen(i: Intent?) { if (i?.getStringExtra("open") == "chat" && ::box.isInitialized) openChatHome() }

    private var updInfo: cn.banyuexuan.call.common.Updater.Info? = null
    private var seenUpdSeq = 0
    private var updText: String? = null
    private var latestText: String? = null              // 主机上最新版本(定时查询得到),显示在底部版本行
    private fun onLatest(r: cn.banyuexuan.call.common.Updater.Latest) {
        updInfo = r.newer
        latestText = if (r.error == null && r.name != null) cn.banyuexuan.call.common.Updater.label(r.name, r.code) else null
    }
    private val updTick = object : Runnable {
        override fun run() {
            cn.banyuexuan.call.common.Updater.query(this@MainActivity, WatchService.config(this@MainActivity), "watch") { r -> onLatest(r); render() }
            ui.postDelayed(this, 10 * 60_000L)
        }
    }
    override fun onStart() { super.onStart(); if (!::box.isInitialized) return; WatchState.update { WatchState.appVisible = true }; WatchState.listen(listener); render(); ui.post(updTick) }
    override fun onStop() {
        super.onStop(); if (!::box.isInitialized) return
        WatchState.update { WatchState.appVisible = false }; WatchState.unlisten(listener); ui.removeCallbacks(updTick)
        stopRecording(false)
    }

    private fun askPermissions() {
        val need = listOf(Manifest.permission.POST_NOTIFICATIONS, Manifest.permission.BLUETOOTH_SCAN,
            Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.BODY_SENSORS,
            Manifest.permission.RECORD_AUDIO)
            .filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (need.isEmpty()) afterPermissions() else requestPermissions(need.toTypedArray(), 1)
    }

    override fun onRequestPermissionsResult(c: Int, p: Array<out String>, r: IntArray) { if (c == 1) afterPermissions() }

    private fun afterPermissions() {
        try { startForegroundService(Intent(this, WatchService::class.java)) } catch (_: Throwable) {}
        KeepAlive.schedule(this)
        if (!Settings.canDrawOverlays(this))                     // 悬浮球需要"显示在其他应用上层"权限
            try { startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName"))) } catch (_: Throwable) {}
        val pm = getSystemService(PowerManager::class.java)
        if (!pm.isIgnoringBatteryOptimizations(packageName))      // keep the service alive in the background
            try { startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))) } catch (_: Throwable) {}
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
    private fun bg(c: String) = GradientDrawable().apply { setColor(Color.parseColor(c)); cornerRadius = dp(12).toFloat() }
    private fun btn(t: String, c: String, f: () -> Unit) = Button(this).apply { text = t; textSize = 20f; minHeight = 0; minimumHeight = 0; isAllCaps = false; setPadding(dp(4), dp(4), dp(4), dp(4)); setTextColor(Color.WHITE); background = bg(c)
        PressFx.attach(this)
        setOnClickListener {                                     // 先让按钮闪一下、显示 ✓,再执行操作(操作会刷新界面)
            isEnabled = false; PressFx.confirm(this, "✓ $t", 600); postDelayed({ f() }, 220); postDelayed({ isEnabled = true }, 1500) } }
    private fun label(t: String, size: Float = 16f, color: String = "#3a2e25", bold: Boolean = false) = TextView(this).apply {
        text = t; textSize = size; setTextColor(Color.parseColor(color)); if (bold) typeface = Typeface.DEFAULT_BOLD }
    private fun full(h: Int = -2, top: Int = 8) = LinearLayout.LayoutParams(-1, h).apply { topMargin = dp(top) }
    private fun row(vararg views: View?): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        views.forEach { v -> addView(v ?: View(context), LinearLayout.LayoutParams(0, dp(TILE_DP), 1f).apply { setMargins(dp(4), dp(6), dp(4), 0) }) }
    }

    private fun confirmThen(title: String, msg: String, ok: String, f: () -> Unit) {
        AlertDialog.Builder(this).setTitle(title).setMessage(msg).setPositiveButton(ok) { _, _ -> f() }.setNegativeButton("取消", null).show()
    }

    private fun render() {
        if (!::box.isInitialized) return
        box.removeAllViews()
        syncBack()
        WatchState.notice?.let { Toast.makeText(this, it, Toast.LENGTH_LONG).show(); WatchState.notice = null }
        if (WatchState.appUpdateSeq != seenUpdSeq) {      // 主机刚推送了新版本:马上检查(不用等 10 分钟)
            seenUpdSeq = WatchState.appUpdateSeq
            cn.banyuexuan.call.common.Updater.query(this, WatchService.config(this), "watch") { r -> onLatest(r); render() }
        }
        when (page) {
            Page.HIST, Page.HIST_DETAIL -> { renderHistory(); return }
            Page.CHAT_HOME -> { renderChatHome(); return }
            Page.CHAT -> { renderChat(); return }
            Page.HOME -> {}
        }
        val cfg = WatchService.config(this)
        val who = WatchState.staffName?.let { "  · $it" + if (WatchState.wearingSpare) "(备用表)" else "" } ?: ""
        head = TextView(this).apply { textSize = 16f; typeface = Typeface.DEFAULT_BOLD
            text = (if (WatchState.connected) "● 已连接" else "○ 未连接") + "   ${cfg.deviceId}$who\n位置:${WatchState.where}"
            setTextColor(if (WatchState.connected) Color.parseColor("#1b7f3b") else Color.parseColor("#c0392b"))
            setOnLongClickListener { showSettings(); true } }
        box.addView(head)
        updInfo?.let { u -> box.addView(TextView(this).apply {          // 有新版本:蓝色方块,点一下更新
            text = updText ?: "发现新版本 ${u.name}\n点击更新"; textSize = 18f; typeface = Typeface.DEFAULT_BOLD; setTextColor(Color.WHITE)
            gravity = Gravity.CENTER; background = bg("#2f6fdb"); setPadding(dp(12), dp(14), dp(12), dp(14))
            setOnClickListener { cn.banyuexuan.call.common.Updater.update(this@MainActivity, WatchService.config(this@MainActivity), "watch", u) { t -> updText = t; render() } } },
            full()) }
        if (WatchState.lowBattery) box.addView(TextView(this).apply {
            text = "🔋 电量只剩 ${WatchState.battery}%,请到护士站换表或充电"; textSize = 18f; typeface = Typeface.DEFAULT_BOLD; setTextColor(Color.WHITE)
            background = bg("#c0392b"); setPadding(dp(12), dp(10), dp(12), dp(10)) }, full())

        val svc = WatchService.instance
        if (WatchState.bindKnown && WatchState.staffId == null) {          // 未绑定:备用池里的表 / 已换下的原表
            box.addView(TextView(this).apply {
                textSize = 20f; gravity = Gravity.CENTER; setTextColor(Color.parseColor("#3a2e25")); background = bg("#ffffff"); setPadding(dp(12), dp(24), dp(12), dp(24))
                text = if (WatchState.isOwnWatchOff) "这块表已换下\n充满电后,点下面“一键归位”切回这块表" else "备用表 · 未绑定\n请在护士站领用后使用" }, full(top = 14))
            if (WatchState.isOwnWatchOff) box.addView(btn("↩ 一键归位", "#b5835a") {
                confirmThen("一键归位", "切回这块表?现在戴的备用表会被清空并交回护士站。", "切回") {
                    if (svc?.returnHome() != true) Toast.makeText(this, "未连接主机,请稍后再试", Toast.LENGTH_SHORT).show() } }, full(dp(TILE_DP), 14))
            box.addView(settingsRow(), full())
            scroll.post { scroll.scrollTo(0, 0) }
            return
        }

        WatchState.alerts.values.forEach { a -> box.addView(card(a), full(top = 10)) }
        WatchState.help?.let { h -> box.addView(helpCard(h), full(top = 10)) }
        val helpActive = (WatchState.help?.status ?: "") in setOf("sending", "sent", "alone", "floor", "leader", "accepted", "arrived")
        if (!helpActive) {
            // 没有呼叫时求助按钮占大半屏(抬腕就能按);有呼叫时缩小放在呼叫下面(客户呼叫优先)
            val big = WatchState.alerts.isEmpty()
            val h = if (big) (resources.displayMetrics.heightPixels * 0.5).toInt() else dp(TILE_DP)
            box.addView(helpButton(big), full(h, if (big) 12 else 14))
        }
        val unread = if (WatchState.unread > 0) " (${WatchState.unread})" else ""
        box.addView(row(btn("💬 消息$unread", "#2f6fdb") { openChatHome() }, btn("📋 记录", "#7a6a5c") { openHistory() }), full())
        if (WatchState.wearingSpare) box.addView(btn("↩ 一键归位(切回自己的表)", "#b5835a") {
            confirmThen("一键归位", "自己的表充好电了吗?\n切回后这块备用表会被清空,请交回护士站。", "切回") {
                if (svc?.returnHome() != true) Toast.makeText(this, "未连接主机,请稍后再试", Toast.LENGTH_SHORT).show() } }, full(dp(64), 10))
        box.addView(settingsRow(), full())
        scroll.post { scroll.scrollTo(0, 0) }
    }

    /** 底部:⚙ 设置 + ⬆ 升级 两个按钮,下面一行显示当前版本和主机最新版本(长按顶部状态栏也能打开设置)。 */
    private fun settingsRow(): LinearLayout {
        val u = updInfo
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        col.addView(row(btn("⚙ 设置", "#9a9088") { showSettings() },
            btn(if (u != null) "⬆ 升级" else "⬆ 检查更新", if (u != null) "#d9483b" else "#2f6fdb") { openUpdate() }), full())
        col.addView(TextView(this).apply { textSize = 13f; gravity = Gravity.CENTER; setTextColor(Color.parseColor("#7a6a5c"))
            text = "当前 " + cn.banyuexuan.call.common.Updater.versionText(this@MainActivity) +
                "\n最新 " + (latestText ?: "—") + if (u != null) "(可升级)" else if (latestText != null) "(已是最新)" else "" },
            LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(4) })
        return col
    }

    private fun openUpdate() = cn.banyuexuan.call.common.UpdateDialog.show(this, WatchService.config(this), "watch") { r -> onLatest(r); render() }

    // ---- 一键求助 ----------------------------------------------------
    /** 必须按住 help_hold_ms(默认 1 秒)才发出,短按只提示,防止误触。 */
    @SuppressLint("ClickableViewAccessibility")
    private fun helpButton(big: Boolean): TextView {
        val v = TextView(this).apply {
            gravity = Gravity.CENTER; setTextColor(Color.WHITE); typeface = Typeface.DEFAULT_BOLD
            textSize = if (big) 40f else 22f; background = bg("#d4572a")
            text = if (big) "求助\n按住 1 秒" else "🆘 求助(按住 1 秒)" }
        var fired = false
        val fire = Runnable {
            fired = true
            v.background = bg("#8e2f12"); v.text = "已发出"
            if (WatchService.instance?.sendHelp() != true) { Toast.makeText(this, "未连接主机,求助没发出", Toast.LENGTH_LONG).show(); render() }
        }
        v.setOnTouchListener { _, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> { fired = false; v.background = bg("#a8401c"); v.text = "继续按住…"; v.postDelayed(fire, WatchState.helpHoldMs) }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    v.removeCallbacks(fire)
                    if (!fired) { v.background = bg("#d4572a"); v.text = if (big) "求助\n按住 1 秒" else "🆘 求助(按住 1 秒)"
                        if (e.actionMasked == MotionEvent.ACTION_UP) Toast.makeText(this, "请按住 1 秒再松开", Toast.LENGTH_SHORT).show() }
                }
            }
            true
        }
        return v
    }

    private fun helpCard(h: WatchState.Help): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL; setPadding(dp(14), dp(10), dp(14), dp(12))
        val color = when (h.status) { "accepted", "arrived" -> "#e3f1e6"; "completed", "cancelled" -> "#eeeae4"; else -> "#fde7dc" }
        background = bg(color)
        addView(label("🆘 我的求助", 15f, "#7a6a5c", true))
        addView(label(h.text, 22f, "#3a2e25", true))
        if (h.status in setOf("sent", "alone", "floor", "leader") && h.callId != null)
            addView(row(btn("取消求助", "#9a9088") { WatchService.instance?.cancelHelp() }, null), LinearLayout.LayoutParams(-1, -2))
    }

    // ---- 同事消息 ----------------------------------------------------
    private var contacts: JSONObject? = null
    private var chatMsg = ""
    private var chatId = ""                // 打开时的频道(可能是 dm:<对方> 或 role:<岗位>)
    private var chatName = ""
    private var chatResolved: String? = null  // 实际频道 id(dm:a|b);岗位呼叫要等主机回显才知道
    private val sentHere = HashSet<String>()  // 本页发出的消息(client_id),岗位呼叫时用来显示
    private var recordingStarted = 0L
    private var rec: VoiceRec? = null

    private fun api(path: String, done: (String?) -> Unit) {
        val cfg = WatchService.config(this)
        Thread { val t = try { Http.getText(cfg.apiBase, cfg.apiKey, path) } catch (_: Throwable) { null }; runOnUiThread { done(t) } }.start()
    }

    private fun openChatHome() {
        page = Page.CHAT_HOME; chatMsg = "加载中…"; WatchState.unread = 0; render()
        val dev = WatchService.config(this).deviceId
        api("/watches/$dev/chat/contacts") { t ->
            contacts = try { t?.let { JSONObject(it) } } catch (_: Throwable) { null }
            chatMsg = if (contacts == null) "加载失败,请检查网络" else ""
            if (page == Page.CHAT_HOME) render()
        }
        api("/watches/$dev/chat/inbox?limit=40") { t -> mergeMsgs(t); if (page == Page.CHAT_HOME) render() }
    }

    private fun mergeMsgs(t: String?) {
        val a = try { t?.let { JSONArray(it) } } catch (_: Throwable) { null } ?: return
        for (i in 0 until a.length()) a.optJSONObject(i)?.let { p ->
            val m = WatchState.Msg(p.optString("id"), p.optString("channel"), p.optString("channel_name"), p.optString("sender_name"),
                p.optBoolean("mine"), p.optString("kind"), if (p.isNull("text")) null else p.optString("text"),
                if (p.isNull("voice_url")) null else p.optString("voice_url"), if (p.isNull("duration_s")) null else p.optDouble("duration_s"),
                p.optString("at"), if (p.isNull("note")) null else p.optString("note"))
            if (!WatchState.msgs.containsKey(m.id)) WatchState.addMsg(m)
        }
    }

    private fun dmId(a: String, b: String) = if (a < b) "dm:$a|$b" else "dm:$b|$a"

    private fun openChat(id: String, name: String) {
        chatId = id; chatName = name; sentHere.clear()
        val me = WatchState.staffId
        chatResolved = when {
            id.startsWith("dm:") && !id.contains("|") && me != null -> dmId(me, id.substring(3))
            id.startsWith("role:") -> null
            else -> id
        }
        page = Page.CHAT; render()
        val ch = chatResolved ?: return
        val dev = WatchService.config(this).deviceId
        api("/watches/$dev/chat/history?limit=30&channel=" + URLEncoder.encode(ch, "UTF-8")) { t -> mergeMsgs(t); if (page == Page.CHAT) render() }
    }

    private fun tile(t: String, sub: String?, c: String, f: () -> Unit) = btn(if (sub != null) "$t\n$sub" else t, c, f).apply { textSize = 17f }

    private fun renderChatHome() {
        box.addView(btn("← 返回", "#7a6a5c") { goBack() }, LinearLayout.LayoutParams(-1, dp(56)))
        box.addView(label("同事消息", 18f, bold = true).apply { setPadding(0, dp(10), 0, dp(4)) })
        val c = contacts
        if (c == null) { box.addView(label(chatMsg, 18f).apply { gravity = Gravity.CENTER; setPadding(0, dp(20), 0, 0) }); return }
        val chans = c.optJSONArray("channels") ?: JSONArray()
        val tiles = (0 until chans.length()).map { i -> chans.getJSONObject(i).let { o -> tile(o.optString("name"), null, "#2f6fdb") { openChat(o.optString("id"), o.optString("name")) } } }
        tiles.chunked(2).forEach { pair -> box.addView(row(pair[0], pair.getOrNull(1)), LinearLayout.LayoutParams(-1, -2)) }
        val roles = c.optJSONArray("roles") ?: JSONArray()
        if (roles.length() > 0) {
            box.addView(label("按岗位呼叫", 15f, "#7a6a5c", true).apply { setPadding(0, dp(12), 0, 0) })
            val rt = (0 until roles.length()).map { i -> roles.getJSONObject(i).let { o ->
                val n = o.optInt("online")
                tile(o.optString("name"), if (n > 0) "${n}人在班" else "无人在班", if (n > 0) "#b5835a" else "#b8b0a8") {
                    if (n > 0) openChat("role:" + o.optString("id"), "呼叫" + o.optString("name"))
                    else Toast.makeText(this, "该岗位当前没有在班的人", Toast.LENGTH_SHORT).show() } } }
            rt.chunked(2).forEach { pair -> box.addView(row(pair[0], pair.getOrNull(1)), LinearLayout.LayoutParams(-1, -2)) }
        }
        val people = c.optJSONArray("people") ?: JSONArray()
        box.addView(label("找人(在班戴表的同事)", 15f, "#7a6a5c", true).apply { setPadding(0, dp(12), 0, 0) })
        if (people.length() == 0) box.addView(label("当前没有其他在班同事", 16f))
        val pt = (0 until minOf(people.length(), 12)).map { i -> people.getJSONObject(i).let { o ->
            val sub = listOfNotNull(o.optString("floor").takeIf { it.isNotBlank() && it != "null" }?.let { "${it}层" },
                if (o.optBoolean("busy")) "忙" else null).joinToString(" · ").ifBlank { null }
            tile(o.optString("name"), sub, "#5b8a72") { openChat("dm:" + o.optString("staff"), o.optString("name")) } } }
        pt.chunked(2).forEach { pair -> box.addView(row(pair[0], pair.getOrNull(1)), LinearLayout.LayoutParams(-1, -2)) }
        val recent = WatchState.msgs.values.filter { !it.pending }.sortedByDescending { it.at }.take(10)
        if (recent.isNotEmpty()) {
            box.addView(label("最近消息", 15f, "#7a6a5c", true).apply { setPadding(0, dp(12), 0, 0) })
            recent.forEach { m -> box.addView(msgRow(m, true).apply { setOnClickListener { openChat(m.channel, m.channelName) } }, full(top = 6)) }
        }
        scroll.post { scroll.scrollTo(0, 0) }
    }

    private fun msgRow(m: WatchState.Msg, showChannel: Boolean): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL; setPadding(dp(12), dp(8), dp(12), dp(8))
        background = bg(if (m.mine) "#e3eefc" else "#ffffff")
        val who = (if (m.mine) "我" else m.senderName) + (if (showChannel) " · ${m.channelName}" else "") + "  " + hhmm(m.at) + if (m.pending) " · 发送中" else ""
        addView(label(who, 13f, "#7a6a5c"))
        m.note?.let { addView(label(it, 13f, "#b5835a")) }
        val body = if (m.kind == "voice") "▶ 语音 ${m.durationS?.let { Math.round(it) } ?: 0} 秒" else (m.text ?: "")
        addView(label(body, 19f, if (m.kind == "voice") "#2f6fdb" else "#3a2e25", m.kind == "voice"))
        if (m.kind == "voice" && m.voiceUrl != null && !showChannel)
            setOnClickListener { WatchService.instance?.playVoice(m.voiceUrl, chat = true) }
    }

    private fun renderChat() {
        box.addView(btn("← 返回", "#7a6a5c") { goBack() }, LinearLayout.LayoutParams(-1, dp(56)))
        box.addView(label(chatName, 18f, bold = true).apply { setPadding(0, dp(8), 0, dp(4)) })
        // 岗位呼叫:主机回显后才知道实际是跟谁的一对一频道
        if (chatResolved == null) WatchState.msgs.values.firstOrNull { it.clientId != null && it.clientId in sentHere && !it.pending }?.let { chatResolved = it.channel }
        val list = WatchState.msgs.values.filter { it.channel == chatResolved || (it.clientId != null && it.clientId in sentHere) }
            .sortedBy { it.at }.takeLast(15)
        if (list.isEmpty()) box.addView(label(if (chatId.startsWith("role:")) "发一条消息,主机会转给当班的人" else "还没有消息", 16f, "#7a6a5c"))
        list.forEach { m -> box.addView(msgRow(m, false), full(top = 6)) }
        // 快捷回复:一点即发
        val svc = WatchService.instance
        val qr = WatchState.quickReplies.map { q -> tile(q, null, "#5b8a72") {
            if (svc?.sendChat(chatId, "quick", q) == true) rememberLast() else Toast.makeText(this, "未连接主机,没发出", Toast.LENGTH_SHORT).show() } }
        qr.chunked(2).forEach { pair -> box.addView(row(pair[0], pair.getOrNull(1)), LinearLayout.LayoutParams(-1, -2)) }
        box.addView(talkButton(), full(dp(TILE_DP), 10))
        scroll.post { scroll.fullScroll(View.FOCUS_DOWN) }
    }

    private fun rememberLast() {
        WatchState.msgs.values.lastOrNull { it.pending && it.clientId != null }?.clientId?.let { sentHere.add(it) }
    }

    private val recTick = object : Runnable {
        override fun run() {
            val v = talkView ?: return
            if (rec?.recording != true) return
            val s = (System.currentTimeMillis() - recordingStarted) / 1000
            v.text = "● 录音中 ${s} 秒  松开发送"
            ui.postDelayed(this, 500)
        }
    }
    private var talkView: TextView? = null

    /** 按住说话:按住开始录音,松开发送;手指滑出按钮再松开 = 取消。最长 voice_max_s 秒,到时自动发送。 */
    @SuppressLint("ClickableViewAccessibility")
    private fun talkButton(): TextView {
        val v = TextView(this).apply { gravity = Gravity.CENTER; textSize = 20f; typeface = Typeface.DEFAULT_BOLD; setTextColor(Color.WHITE)
            background = bg("#2f6fdb"); text = "🎤 按住说话" }
        talkView = v
        v.setOnTouchListener { _, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                        requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 2); return@setOnTouchListener true }
                    val r = rec ?: VoiceRec(this).also { rec = it }
                    if (r.start(WatchState.voiceMaxS) { ui.post { stopRecording(true) } }) {
                        recordingStarted = System.currentTimeMillis(); v.background = bg("#c0392b"); ui.post(recTick)
                    } else Toast.makeText(this, "无法录音", Toast.LENGTH_SHORT).show()
                }
                MotionEvent.ACTION_UP -> {
                    val inside = e.x >= 0 && e.y >= 0 && e.x <= v.width && e.y <= v.height
                    stopRecording(inside)
                    if (!inside) Toast.makeText(this, "已取消", Toast.LENGTH_SHORT).show()
                }
                MotionEvent.ACTION_CANCEL -> stopRecording(false)
            }
            true
        }
        return v
    }

    private fun stopRecording(send: Boolean) {
        val r = rec ?: return
        if (!r.recording) return
        ui.removeCallbacks(recTick)
        talkView?.let { it.background = bg("#2f6fdb"); it.text = "🎤 按住说话" }
        if (!send) { r.cancel(); return }
        val got = r.stop()
        if (got == null) { Toast.makeText(this, "太短了,请按住说话", Toast.LENGTH_SHORT).show(); return }
        val svc = WatchService.instance ?: return
        svc.sendVoice(chatId, got.first, got.second)
        ui.postDelayed({ rememberLast(); if (page == Page.CHAT) render() }, 1500)
    }

    // ---- 我的记录:最近 30 天接过的单(数据在主机),可回看时间线、听留言、看转文字 ----
    private var histItems: JSONArray? = null
    private var histMsg = ""
    private var histSel: JSONObject? = null

    private fun openHistory() { page = Page.HIST; histItems = null; histMsg = "加载中…"; render(); loadHistory() }

    private fun loadHistory() {
        val dev = WatchService.config(this).deviceId
        api("/watches/$dev/history?days=30&limit=100") { t ->
            if (page != Page.HIST && page != Page.HIST_DETAIL) return@api
            try { histItems = t?.let { JSONArray(it) }; histMsg = if (t == null) "加载失败,请检查网络后重试" else "" }
            catch (_: Throwable) { histItems = null; histMsg = "加载失败" }
            render()
        }
    }

    /** 主机时间是 UTC(…+00:00),换成手表本地时区再显示。 */
    private fun local(iso: String?): java.time.ZonedDateTime? = try {
        if (iso.isNullOrBlank()) null else java.time.OffsetDateTime.parse(iso).atZoneSameInstant(java.time.ZoneId.systemDefault()) } catch (_: Throwable) { null }
    private fun hhmm(iso: String?): String = local(iso)?.format(java.time.format.DateTimeFormatter.ofPattern("MM/dd HH:mm")) ?: ""
    private fun stateText(s: String) = mapOf("completed" to "已完成", "claimed" to "已接单", "arrived" to "已到场", "cancelled" to "已取消")[s] ?: s

    @SuppressLint("GestureBackNavigation")
    @Deprecated("old systems") override fun onBackPressed() { if (page == Page.HOME) super.onBackPressed() else goBack() }   // 旧系统(手表<Android 13)走这里
    private val backCb: Any? by lazy { if (android.os.Build.VERSION.SDK_INT >= 33) android.window.OnBackInvokedCallback { goBack() } else null }
    private fun syncBack() {                                      // Android 13+:仅在子页面里接管返回手势
        if (android.os.Build.VERSION.SDK_INT < 33) return
        val cb = backCb as android.window.OnBackInvokedCallback
        onBackInvokedDispatcher.unregisterOnBackInvokedCallback(cb)
        if (page != Page.HOME) onBackInvokedDispatcher.registerOnBackInvokedCallback(android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT, cb)
    }
    private fun goBack() {
        stopRecording(false)
        page = when (page) { Page.HIST_DETAIL -> Page.HIST; Page.CHAT -> Page.CHAT_HOME; else -> Page.HOME }
        if (page == Page.CHAT_HOME) openChatHome() else render()
    }

    private fun renderHistory() {
        val back = btn(if (page == Page.HIST_DETAIL) "← 返回记录" else "← 返回", "#7a6a5c") { goBack() }
        box.addView(back, LinearLayout.LayoutParams(-1, dp(56)))
        if (page == Page.HIST_DETAIL) { histSel?.let { renderDetail(it) }; scroll.post { scroll.scrollTo(0, 0) }; return }
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
                setOnClickListener { histSel = o; page = Page.HIST_DETAIL; render() }
            }, full())
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
        }, full(top = 10))
    }

    private fun card(a: WatchState.Alert): LinearLayout {
        val svc = WatchService.instance
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = bg(if (a.emergency) "#fbe3df" else if (a.help) "#f3e6fb" else if (a.status == "completed") "#e3f1e6" else "#ffffff"); setPadding(dp(14), dp(10), dp(14), dp(12))
            addView(TextView(context).apply { textSize = 26f; typeface = Typeface.DEFAULT_BOLD
                setTextColor(if (a.emergency) Color.parseColor("#c0392b") else if (a.help) Color.parseColor("#7b3fa0") else Color.parseColor("#3a2e25"))
                text = if (a.help) "🆘 ${a.label}" else (if (a.emergency) "紧急 " else "") + "${a.roomNo} 房  ${a.label}" })
            val st = mapOf("pending" to (if (a.help) "同事需要帮忙" else "待接单"), "accepted" to "已接单,请前往", "arrived" to "已到场", "completed" to "✓ 已完成")[a.status] ?: a.status
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
                "pending" -> btn(if (a.help) "我去帮忙" else "接单", "#1b7f3b") { svc?.command(Proto.CALL_ACCEPT, a.callId); a.status = "accepting"; render() } to null
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
                addView(row(pair[0], pair[1]), LinearLayout.LayoutParams(-1, -2))   // 空位也占位,方块大小、位置不变
            }
        }
    }

    /** 不能点的状态方块(如"✓ 已到场"、"接单中…"),和按钮同样大小,只显示状态。 */
    private fun badge(t: String, c: String) = TextView(this).apply { text = t; textSize = 20f; gravity = Gravity.CENTER; setTextColor(Color.WHITE); background = bg(c) }

    /** 防杀检查:电池白名单、默认桌面、悬浮窗、待机分组(Android 9 才有)。 */
    private fun keepAliveReport(): String {
        val pm = getSystemService(PowerManager::class.java)
        val white = try { pm.isIgnoringBatteryOptimizations(packageName) } catch (_: Throwable) { false }
        val home = try { packageManager.resolveActivity(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME), PackageManager.MATCH_DEFAULT_ONLY)
            ?.activityInfo?.packageName == packageName } catch (_: Throwable) { false }
        val overlay = try { Settings.canDrawOverlays(this) } catch (_: Throwable) { false }
        val bucket = if (android.os.Build.VERSION.SDK_INT >= 28) try {
            when (getSystemService(android.app.usage.UsageStatsManager::class.java).appStandbyBucket) {
                android.app.usage.UsageStatsManager.STANDBY_BUCKET_ACTIVE -> "活跃(好)"
                android.app.usage.UsageStatsManager.STANDBY_BUCKET_WORKING_SET -> "常用(好)"
                android.app.usage.UsageStatsManager.STANDBY_BUCKET_FREQUENT -> "较常用(可能延迟)"
                android.app.usage.UsageStatsManager.STANDBY_BUCKET_RARE -> "很少用(会被限制!)"
                else -> "受限(会被限制!)"
            } } catch (_: Throwable) { "未知" } else "本系统没有"
        val ok = { b: Boolean -> if (b) "✓" else "✗" }
        return "${ok(white)} 电池优化白名单\n${ok(home)} 设为默认桌面\n${ok(overlay)} 悬浮球权限\n${ok(WatchState.wifiLock)} 息屏保持 Wi-Fi\n${ok(WatchState.cpuLock)} 息屏保持 CPU 唤醒\n待机分组:$bucket\n" +
            "务必关闭手表系统的“长续航模式/省电模式”(它会在息屏后断开 Wi-Fi)\n" +
            "另请在系统 WLAN 设置 → 高级里,把“休眠时保持 WLAN 连接”设为“始终”(有的手表叫 WLAN 休眠策略)\n安卓 ${android.os.Build.VERSION.RELEASE}(API ${android.os.Build.VERSION.SDK_INT})"
    }

    private fun showSettings() {
        val cfg = WatchService.config(this)
        cn.banyuexuan.call.common.SettingsDialog.show(this, cfg, "手表设置  " + cn.banyuexuan.call.common.Updater.versionText(this),
            top = "防杀检查(都打 ✓ 才不容易漏呼):\n" + keepAliveReport(),
            neutral = "系统设置" to { try { startActivity(Intent(Settings.ACTION_SETTINGS)) } catch (_: Throwable) {} }) {
            stopService(Intent(this, WatchService::class.java))
            try { startForegroundService(Intent(this, WatchService::class.java)) } catch (_: Throwable) {}
        }
    }

    companion object { const val TILE_DP = 92 }      // 方块按钮高度,够大不易误点
}
