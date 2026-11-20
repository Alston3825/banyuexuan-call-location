package cn.banyuexuan.call.common

import android.app.Activity
import android.app.AlertDialog
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.text.InputType
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/**
 * 平板、手表共用的设置页:每一项一张卡片 ——
 *   中文名(键名)
 *   用途说明
 *   例:示例值
 *   [当前值输入框]
 * 改了“主机地址”,如果“接口地址”还是旧主机地址,保存时自动换成新地址。
 */
object SettingsDialog {
    private fun dp(a: Activity, v: Int) = (v * a.resources.displayMetrics.density).toInt()

    fun show(a: Activity, cfg: DeviceConfig, title: String, top: String? = null,
             neutral: Pair<String, () -> Unit>? = null, onSave: () -> Unit) {
        val col = LinearLayout(a).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(a, 14), dp(a, 6), dp(a, 14), dp(a, 6)) }
        top?.let { t -> col.addView(TextView(a).apply { text = t; textSize = 14f; setTextColor(Color.parseColor("#3a2e25"))
            background = GradientDrawable().apply { setColor(Color.parseColor("#efe7dc")); cornerRadius = dp(a, 8).toFloat() }
            setPadding(dp(a, 10), dp(a, 8), dp(a, 10), dp(a, 8)) }) }
        val fields = LinkedHashMap<String, EditText>()
        val keys = DeviceConfig.ITEMS.keys.filter { it in cfg.keys } + cfg.keys.filter { it !in DeviceConfig.ITEMS.keys }
        for (k in keys) {
            val item = DeviceConfig.ITEMS[k]
            val card = LinearLayout(a).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(a, 12), dp(a, 8), dp(a, 12), dp(a, 8))
                background = GradientDrawable().apply { setColor(Color.WHITE); cornerRadius = dp(a, 10).toFloat(); setStroke(dp(a, 1), Color.parseColor("#e0d6c8")) } }
            card.addView(TextView(a).apply { textSize = 17f; typeface = Typeface.DEFAULT_BOLD; setTextColor(Color.parseColor("#3a2e25"))
                text = (item?.name ?: k) + "  ($k)" })
            if (item != null) {
                card.addView(TextView(a).apply { text = item.help; textSize = 13f; setTextColor(Color.parseColor("#7a6a5c")) })
                card.addView(TextView(a).apply { text = "例:" + item.example; textSize = 13f; setTextColor(Color.parseColor("#2f6fdb")) })
            }
            val e = EditText(a).apply { setSingleLine(true); setText(cfg[k]); textSize = 18f
                inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
                imeOptions = EditorInfo.IME_ACTION_NEXT }
            card.addView(e)
            fields[k] = e
            col.addView(card, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(a, 8) })
        }
        val b = AlertDialog.Builder(a).setTitle(title).setView(ScrollView(a).apply { addView(col) })
            .setPositiveButton("保存并重新连接") { _, _ ->
                val oldHost = cfg["mqtt_host"]
                val newHost = fields["mqtt_host"]?.text?.toString()?.trim().orEmpty()
                fields.forEach { (k, v) -> cfg[k] = v.text.toString() }
                // 主机地址改了、接口地址没跟着改:自动换成新主机
                val api = cfg["api_base"]
                if (newHost.isNotEmpty() && oldHost.isNotEmpty() && newHost != oldHost && api.contains("//$oldHost"))
                    cfg["api_base"] = api.replace("//$oldHost", "//$newHost")
                onSave()
            }
            .setNegativeButton("取消", null)
        neutral?.let { (t, f) -> b.setNeutralButton(t) { _, _ -> f() } }
        b.show()
    }

    /** 有设置密码时先问密码,对了才打开。 */
    fun withPin(a: Activity, cfg: DeviceConfig, open: () -> Unit) {
        val pin = cfg["admin_pin"].trim()
        if (pin.isEmpty()) { open(); return }
        val e = EditText(a).apply { inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD; textSize = 22f; hint = "设置密码" }
        AlertDialog.Builder(a).setTitle("请输入设置密码").setMessage("仅限工作人员").setView(e)
            .setPositiveButton("确定") { _, _ ->
                if (e.text.toString().trim() == pin) open()
                else android.widget.Toast.makeText(a, "密码不对", android.widget.Toast.LENGTH_SHORT).show()
            }.setNegativeButton("取消", null).show()
    }
}
