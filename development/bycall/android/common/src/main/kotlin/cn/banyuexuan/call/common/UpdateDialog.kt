package cn.banyuexuan.call.common

import android.app.Activity
import android.app.AlertDialog
import android.graphics.Color
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/**
 * 平板、手表共用的“版本与升级”窗口:
 *   当前版本 / 主机上的最新版本 / 更新说明
 *   [立即升级](有新版本才能点)  [重新检查]  [关闭]
 * 升级进度(下载百分比、校验、安装)直接显示在窗口里。
 */
object UpdateDialog {
    fun show(a: Activity, cfg: DeviceConfig, app: String, onLatest: ((Updater.Latest) -> Unit)? = null) {
        val d = a.resources.displayMetrics.density
        fun dp(v: Int) = (v * d).toInt()
        val cur = TextView(a).apply { textSize = 17f; setTextColor(Color.parseColor("#3a2e25")) }
        val latest = TextView(a).apply { textSize = 17f; setTextColor(Color.parseColor("#3a2e25")) }
        val notes = TextView(a).apply { textSize = 14f; setTextColor(Color.parseColor("#7a6a5c")) }
        val status = TextView(a).apply { textSize = 16f; setTextColor(Color.parseColor("#2f6fdb")) }
        val col = LinearLayout(a).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(18), dp(8), dp(18), dp(4))
            addView(cur); addView(latest); addView(notes); addView(status) }
        var info: Updater.Info? = null
        val dlg = AlertDialog.Builder(a).setTitle("版本与升级").setView(col)
            .setPositiveButton("⬆ 立即升级", null).setNeutralButton("重新检查", null).setNegativeButton("关闭", null).create()
        fun up(): Button? = dlg.getButton(AlertDialog.BUTTON_POSITIVE)
        fun check() {
            cur.text = "当前版本:" + Updater.versionText(a)
            latest.text = "主机最新版本:正在查询…"; notes.text = ""; status.text = ""; up()?.isEnabled = false
            Updater.query(a, cfg, app) { r ->
                if (!dlg.isShowing) return@query
                onLatest?.invoke(r)
                info = r.newer
                latest.text = "主机最新版本:" + (if (r.error != null) "查不到" else Updater.label(r.name, r.code))
                notes.text = if (r.notes.isNotBlank()) "更新说明:" + r.notes else ""
                status.text = when {
                    r.error != null -> r.error
                    r.newer != null -> "有新版本,可以升级"
                    else -> "✓ 已是最新版本"
                }
                status.setTextColor(Color.parseColor(if (r.newer != null) "#d9483b" else if (r.error != null) "#c0392b" else "#1b7f3b"))
                up()?.isEnabled = r.newer != null
            }
        }
        dlg.setOnShowListener {
            up()?.setOnClickListener {
                val i = info ?: return@setOnClickListener
                up()?.isEnabled = false
                status.setTextColor(Color.parseColor("#2f6fdb"))
                Updater.update(a, cfg, app, i) { t -> status.text = t
                    if (t.startsWith("更新失败") || t.startsWith("请先允许")) up()?.isEnabled = true }
            }
            dlg.getButton(AlertDialog.BUTTON_NEUTRAL)?.setOnClickListener { check() }
            check()
        }
        dlg.show()
    }
}
