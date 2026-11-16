package cn.banyuexuan.call.tablet

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.media.MediaRecorder
import android.os.Bundle
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import cn.banyuexuan.call.common.DeviceConfig
import cn.banyuexuan.call.common.PressFx
import java.io.File

class MainActivity : Activity() {
    private lateinit var cfg: DeviceConfig
    private var ctl: CallController? = null
    private lateinit var title: TextView
    private lateinit var receipt: TextView
    private lateinit var steps: TextView
    private lateinit var cancelBtn: Button
    private lateinit var updBar: TextView                 // 有新版本时显示;长按才更新(防止住客误触)
    private var updInfo: cn.banyuexuan.call.common.Updater.Info? = null
    private val updTick = object : Runnable {
        override fun run() { checkUpdate(); recUi.postDelayed(this, 10 * 60_000L) }
    }
    private var recorder: MediaRecorder? = null
    private var recFile: File? = null
    private lateinit var voiceBtn: Button
    private lateinit var meter: LinearLayout              // 录音时的音量条:声音越大跳得越高,让客户看到"录进去了"
    private val bars = ArrayList<View>()
    private var recStart = 0L
    private var pulse: android.animation.ValueAnimator? = null
    private val recUi = android.os.Handler(android.os.Looper.getMainLooper())
    private val tick = object : Runnable {
        override fun run() {
            val r = recorder ?: return
            val sec = ((System.currentTimeMillis() - recStart) / 1000).toInt()
            voiceBtn.text = "● 正在录音 %d:%02d    松开发送".format(sec / 60, sec % 60)
            val lvl = try { Math.sqrt(r.maxAmplitude / 32767.0) } catch (e: Exception) { 0.0 }   // 0..1,开方让小声也看得出来
            bars.forEachIndexed { i, b ->
                val h = (0.15 + 0.85 * lvl * (0.55 + 0.45 * Math.random())) * (1.0 - 0.07 * Math.abs(i - bars.size / 2))
                b.layoutParams = (b.layoutParams as LinearLayout.LayoutParams).apply { height = dp((8 + 40 * h).toInt()) }
            }
            meter.requestLayout()
            recUi.postDelayed(this, 90)
        }
    }

    private data class Btn(val code: String, val label: String, val hold: Boolean, val guest: Boolean = false)
    // 默认按钮;主机经 reply/{device} 下发 DEVICE_CONFIG 后改用主机的列表并缓存到本机,断网重启仍可用。
    private val defaultButtons = listOf(Btn("BABY_CRY", "宝宝哭闹", false), Btn("NEED_NURSE", "呼叫护理师", false),
        Btn("MILK_STORAGE", "取储存母乳", false), Btn("CLEANING", "打扫卫生", false), Btn("TRAY", "收餐盘", false),
        Btn("GUEST_1", "", false, true), Btn("EMERGENCY", "紧急", true))
    // 每个按钮的颜色和图标(没配置的按钮用默认的暖棕色)
    private val look = mapOf("BABY_CRY" to ("#e8955a" to "👶"), "NEED_NURSE" to ("#4f8fd9" to "🔔"), "MILK_STORAGE" to ("#dfa83f" to "🍼"),
        "CLEANING" to ("#3fae98" to "🧹"), "TRAY" to ("#8c7fcb" to "🍽️"), "GUEST_1" to ("#d6709f" to "✨"))
    private var buttons = defaultButtons
    private lateinit var grid: LinearLayout
    private lateinit var emergency: LinearLayout
    private val ui by lazy { getSharedPreferences("tablet_ui", MODE_PRIVATE) }

    private fun loadButtons() {
        try {
            val a = org.json.JSONArray(ui.getString("buttons", null) ?: return)
            val l = (0 until a.length()).map { a.getJSONObject(it) }.map { Btn(it.getString("code"), it.optString("label"), it.optBoolean("hold"), it.optBoolean("guest")) }
            if (l.isNotEmpty()) buttons = l
        } catch (e: Exception) { buttons = defaultButtons }
    }

    private fun applyConfig(p: org.json.JSONObject) {
        val a = p.optJSONArray("buttons") ?: return
        if (a.length() == 0) return
        ui.edit().putString("buttons", a.toString()).putString("ver", p.optString("ver")).apply()
        runOnUiThread { loadButtons(); fillButtons() }
    }

    private fun fillButtons() {
        grid.removeAllViews(); emergency.removeAllViews()
        val gap = LinearLayout.LayoutParams(0, dp(104), 1f).apply { setMargins(dp(7), dp(5), dp(7), dp(5)) }
        buttons.filter { !it.hold }.chunked(3).forEach { rowItems ->
            val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            rowItems.forEach { b ->
                if (b.guest && b.label.isBlank()) { row.addView(View(this), LinearLayout.LayoutParams(gap)); return@forEach }   // 自定义按钮没设置:留空位,版面不变
                val (color, icon) = look[b.code] ?: ("#b5835a" to "")
                val big = if (b.guest) b.label else b.label
                row.addView(Button(this).apply {
                    text = if (icon.isEmpty()) big else android.text.SpannableString("$icon\n$big").apply {
                        setSpan(android.text.style.RelativeSizeSpan(1.5f), 0, icon.length, 0) }
                    textSize = 21f; setTextColor(Color.WHITE); gravity = Gravity.CENTER; typeface = Typeface.DEFAULT_BOLD
                    cn.banyuexuan.call.common.Btn3d.apply(this, Color.parseColor(color), 18, 8)
                    PressFx.attach(this)
                    setOnClickListener { ctl?.request(b.code); PressFx.confirm(this, "✓ 已发送") }
                }, LinearLayout.LayoutParams(gap))
            }
            repeat(3 - rowItems.size) { row.addView(View(this), LinearLayout.LayoutParams(gap)) }    // 不足 3 个补空位,宽度一致
            grid.addView(row, LinearLayout.LayoutParams(-1, -2))
        }
        buttons.firstOrNull { it.hold }?.let { b ->
            // 按住紧急键时,按钮四周泛红光并闪烁(光晕画在按钮外圈,所以外面套一层留白)
            val box = android.widget.FrameLayout(this).apply { clipChildren = false; clipToPadding = false; setPadding(dp(20), dp(20), dp(20), dp(20)) }
            val glow = View(this).apply {
                visibility = View.INVISIBLE
                background = android.graphics.drawable.LayerDrawable(Array(5) { i ->
                    GradientDrawable().apply { setColor(Color.argb(70 + i * 45, 255, 30, 20)); cornerRadius = dp(18 + (4 - i) * 4).toFloat() } }).apply {
                    for (i in 0 until 5) setLayerInset(i, dp(i * 4), dp(i * 4), dp(i * 4), dp(i * 4)) }
            }
            var flash: android.animation.ValueAnimator? = null
            val hold = { on: Boolean ->
                flash?.cancel(); flash = null
                if (on) { glow.visibility = View.VISIBLE
                    flash = android.animation.ValueAnimator.ofFloat(0.25f, 1f).apply { duration = 260; repeatMode = android.animation.ValueAnimator.REVERSE
                        repeatCount = android.animation.ValueAnimator.INFINITE; addUpdateListener { glow.alpha = it.animatedValue as Float }; start() }
                } else glow.visibility = View.INVISIBLE
            }
            box.addView(glow, android.widget.FrameLayout.LayoutParams(-1, -1))
            box.addView(Button(this).apply {
                text = "🚨  ${b.label}(长按)"; textSize = 24f; setTextColor(Color.WHITE); gravity = Gravity.CENTER; typeface = Typeface.DEFAULT_BOLD
                cn.banyuexuan.call.common.Btn3d.apply(this, Color.parseColor("#d23b2c"), 18, 8)
                PressFx.attach(this, PressFx.longPressMs, 0.97f, hold)
                setOnLongClickListener { ctl?.request(b.code); PressFx.confirm(this, "✓ 已呼叫,护理师马上赶来"); true }
            }, android.widget.FrameLayout.LayoutParams(-1, dp(78)))
            emergency.addView(box, LinearLayout.LayoutParams(-1, -2))
        }
    }

    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        cfg = DeviceConfig(this, mapOf("mqtt_host" to "10.0.2.2", "mqtt_port" to "1883", "store_id" to "gx",
            "device_id" to "tab-1601", "room_id" to "1601", "floor" to "16", "room_label" to "1601",
            "api_base" to "http://10.0.2.2:8100", "api_key" to "dev-key", "mqtt_password" to ""))
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        loadButtons()
        buildUi()
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1)
    }

    override fun onStart() { super.onStart(); startController(); recUi.post(updTick) }
    override fun onStop() { super.onStop(); ctl?.stop(); ctl = null; recUi.removeCallbacks(updTick) }
    override fun onWindowFocusChanged(f: Boolean) {
        super.onWindowFocusChanged(f)
        if (f) @Suppress("DEPRECATION") window.decorView.systemUiVisibility =
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_FULLSCREEN or
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE
    }

    private fun startController() {
        var self: CallController? = null
        self = CallController(cfg, { ui.getString("ver", "") ?: "" }, ::applyConfig) { s -> runOnUiThread { if (ctl === self) render(s) } }   // ignore a stopped controller's late events
        ctl = self
        self.start()
    }

    private fun render(s: CallController.State) {
        title.text = "${cfg["room_label"]} 房    " + if (s.connected) "● 已连接" else "○ 未连接"
        title.setTextColor(if (s.connected) Color.parseColor("#1b7f3b") else Color.parseColor("#c0392b"))
        receipt.text = s.text
        val names = listOf("已发出", "已接到呼叫", "护理师已接单", "护理师已到达", "已完成")
        steps.text = names.mapIndexed { i, n -> if (i <= s.step && s.callId != null || i == 0 && !s.closed) "● $n" else "○ $n" }.joinToString("   ")
        cancelBtn.visibility = if (s.callId != null && !s.closed) View.VISIBLE else View.GONE
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
    private fun bg(color: String) = GradientDrawable().apply { setColor(Color.parseColor(color)); cornerRadius = dp(14).toFloat() }

    @SuppressLint("ClickableViewAccessibility")
    private fun buildUi() {
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setBackgroundColor(Color.parseColor("#f6f1ea")); setPadding(dp(20), dp(12), dp(20), dp(12)) }
        title = TextView(this).apply { textSize = 22f; typeface = Typeface.DEFAULT_BOLD
            setOnLongClickListener { showSettings(); true } }       // hidden settings: long-press the title
        receipt = TextView(this).apply { textSize = 30f; gravity = Gravity.CENTER; setTextColor(Color.parseColor("#3a2e25")); text = "请选择您需要的服务"; background = bg("#ffffff"); setPadding(dp(12), dp(16), dp(12), dp(16)) }
        steps = TextView(this).apply { textSize = 15f; gravity = Gravity.CENTER; setTextColor(Color.parseColor("#7a6a5c")); setPadding(0, dp(8), 0, dp(8)) }
        grid = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        emergency = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        fillButtons()
        voiceBtn = Button(this).apply { text = "🎤  按住说话,给护理师留言"; textSize = 22f; setTextColor(Color.WHITE); typeface = Typeface.DEFAULT_BOLD
            cn.banyuexuan.call.common.Btn3d.apply(this, Color.parseColor("#2f6fdb"), 18, 8)
            setOnTouchListener { v, e ->
                when (e.action) {
                    MotionEvent.ACTION_DOWN -> { v.performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS); startRec() }
                    MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> stopRec()
                }
                true
            } }
        meter = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER; visibility = View.INVISIBLE }
        bars.clear()
        repeat(15) { meter.addView(View(this).also { v -> bars.add(v); v.background = bg("#d9483b") },
            LinearLayout.LayoutParams(dp(8), dp(8)).apply { setMargins(dp(4), 0, dp(4), 0) }) }
        cancelBtn = Button(this).apply { text = "取消本次呼叫"; textSize = 18f; visibility = View.GONE; setOnClickListener { ctl?.cancel(); PressFx.confirm(this) }; PressFx.attach(this) }
        updBar = TextView(this).apply { textSize = 16f; setTextColor(Color.WHITE); background = bg("#e08a1e"); visibility = View.GONE
            setPadding(dp(12), dp(8), dp(12), dp(8)); setOnLongClickListener { confirmUpdate(); true } }
        root.addView(LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL
            addView(title, LinearLayout.LayoutParams(0, -2, 1f))
            addView(Button(context).apply { text = "⚙ 连接设置"; textSize = 16f; setOnClickListener { showSettings() } }, LinearLayout.LayoutParams(-2, dp(52)))
        }); root.addView(updBar, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(4) }); root.addView(receipt, LinearLayout.LayoutParams(-1, dp(76)).apply { topMargin = dp(6) }); root.addView(steps)
        root.addView(grid, LinearLayout.LayoutParams(-1, -2))
        root.addView(meter, LinearLayout.LayoutParams(-1, dp(40)).apply { topMargin = dp(6) })
        root.addView(voiceBtn, LinearLayout.LayoutParams(-1, dp(72)).apply { topMargin = dp(2); leftMargin = dp(7); rightMargin = dp(7) })
        root.addView(emergency, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(0); leftMargin = dp(-13); rightMargin = dp(-13) })
        root.addView(cancelBtn, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(8) })
        setContentView(android.widget.ScrollView(this).apply { addView(root) })
    }

    private fun checkUpdate() = cn.banyuexuan.call.common.Updater.checkAsync(this, cfg, "tablet") { info ->
        updInfo = info
        updBar.visibility = if (info == null) View.GONE else View.VISIBLE
        info?.let { updBar.text = "发现新版本 ${it.name}${if (it.notes.isNotBlank()) ":" + it.notes else ""}    (工作人员:长按此处更新)" }
    }

    private fun confirmUpdate() {
        val i = updInfo ?: return
        AlertDialog.Builder(this).setTitle("更新到 ${i.name}?")
            .setMessage("${i.notes}\n\n将下载新版本,然后在系统界面点“更新”。更新期间平板会短暂不可用。")
            .setPositiveButton("更新") { _, _ -> cn.banyuexuan.call.common.Updater.update(this, cfg, "tablet", i) { updBar.text = it } }
            .setNegativeButton("取消", null).show()
    }

    /** Microphone is open only while the button is held. */
    private fun startRec() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            receipt.text = "请允许“录音”权限后再试"; requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1); return
        }
        val f = File(cacheDir, "msg-${System.currentTimeMillis()}.m4a")
        try {
            @Suppress("DEPRECATION") recorder = (if (android.os.Build.VERSION.SDK_INT >= 31) MediaRecorder(this) else MediaRecorder()).apply {
                setAudioSource(MediaRecorder.AudioSource.MIC); setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setAudioEncoder(MediaRecorder.AudioEncoder.AAC); setAudioSamplingRate(16000); setAudioEncodingBitRate(32000)
                setMaxDuration(30000); setOutputFile(f.path); prepare(); start()
            }
            recFile = f
            receipt.text = "正在录音…松开发送"
            recStart = System.currentTimeMillis()
            cn.banyuexuan.call.common.Btn3d.apply(voiceBtn, Color.parseColor("#d9483b"), 18, 8); meter.visibility = View.VISIBLE
            pulse?.cancel()
            pulse = android.animation.ValueAnimator.ofFloat(1f, 1.04f).apply {      // 按钮轻微"呼吸",表示正在录音
                duration = 450; repeatMode = android.animation.ValueAnimator.REVERSE; repeatCount = android.animation.ValueAnimator.INFINITE
                addUpdateListener { voiceBtn.scaleX = it.animatedValue as Float; voiceBtn.scaleY = it.animatedValue as Float }
                start()
            }
            recUi.post(tick)
        } catch (e: Exception) { recorder?.release(); recorder = null; receipt.text = "录音没有启动,请再试一次" }
    }

    private fun resetVoiceUi() {
        recUi.removeCallbacks(tick); pulse?.cancel(); pulse = null
        voiceBtn.scaleX = 1f; voiceBtn.scaleY = 1f; cn.banyuexuan.call.common.Btn3d.apply(voiceBtn, Color.parseColor("#2f6fdb"), 18, 8); voiceBtn.text = "🎤  按住说话,给护理师留言"
        meter.visibility = View.INVISIBLE
    }

    private fun stopRec() {
        val r = recorder ?: return
        recorder = null
        resetVoiceUi()
        val ok = try { r.stop(); true } catch (e: Exception) { false }   // stop() throws if recording was too short
        r.release()
        val f = recFile
        if (ok && f != null && f.length() > 2000) ctl?.sendVoice(f) else { f?.delete(); receipt.text = "留言太短,请按住多说几秒" }
    }


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
        val box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(20), dp(10), dp(20), 0) }
        val fields = cfg.keys.associateWith { k -> settingRow(box, k, cfg[k]) }
        AlertDialog.Builder(this).setTitle("设备设置  " + cn.banyuexuan.call.common.Updater.versionText(this)).setView(android.widget.ScrollView(this).apply { addView(box) })
            .setPositiveButton("保存并重连") { _, _ ->
                fields.forEach { (k, v) -> cfg[k] = v.text.toString() }
                ctl?.stop(); startController(); buildUi()
            }.setNegativeButton("取消", null).show()
    }
}
