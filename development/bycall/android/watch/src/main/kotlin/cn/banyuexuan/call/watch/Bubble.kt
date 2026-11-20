package cn.banyuexuan.call.watch

import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.provider.Settings
import android.view.Gravity
import android.view.MotionEvent
import android.view.WindowManager
import android.widget.TextView

/**
 * 悬浮球:应用退到桌面后,在屏幕上留一个小圆球(显示待接单数量),点一下回到应用;
 * 可以用手指拖到任何位置(不会挡住桌面上其他图标),位置会记住。
 * 需要"显示在其他应用上层"权限;没授权就不显示。
 */
class Bubble(private val ctx: Context) {
    private val wm = ctx.getSystemService(WindowManager::class.java)
    private val sp = ctx.getSharedPreferences("bubble", Context.MODE_PRIVATE)
    private var view: TextView? = null
    private lateinit var lp: WindowManager.LayoutParams
    private val size = (56 * ctx.resources.displayMetrics.density).toInt()

    /** 按当前状态显示/隐藏/刷新:应用在前台时不显示。 */
    fun refresh() {
        val want = !WatchState.appVisible && Settings.canDrawOverlays(ctx)
        if (!want) { hide(); return }
        val v = view ?: create()
        val pending = WatchState.alerts.values.count { it.status == "pending" }
        v.text = if (pending > 0) pending.toString() else "伴"
        (v.background as GradientDrawable).setColor(Color.parseColor(if (pending > 0) "#c0392b" else "#7a6a5c"))
    }

    fun hide() { view?.let { try { wm.removeView(it) } catch (_: Exception) {} }; view = null }

    private fun create(): TextView {
        val dm = ctx.resources.displayMetrics
        lp = WindowManager.LayoutParams(size, size, WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS, PixelFormat.TRANSLUCENT).apply {
            gravity = Gravity.TOP or Gravity.START
            x = sp.getInt("x", dm.widthPixels - size - 8).coerceIn(0, maxOf(0, dm.widthPixels - size))
            y = sp.getInt("y", dm.heightPixels / 2).coerceIn(0, maxOf(0, dm.heightPixels - size)) }
        val v = TextView(ctx).apply {
            gravity = Gravity.CENTER; textSize = 20f; typeface = Typeface.DEFAULT_BOLD; setTextColor(Color.WHITE); alpha = 0.92f
            background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(Color.parseColor("#7a6a5c")); setStroke(3, Color.WHITE) }
            elevation = 8f }
        var sx = 0f; var sy = 0f; var ox = 0; var oy = 0; var moved = false
        v.setOnTouchListener { _, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> { sx = e.rawX; sy = e.rawY; ox = lp.x; oy = lp.y; moved = false }
                MotionEvent.ACTION_MOVE -> {
                    val dx = e.rawX - sx; val dy = e.rawY - sy
                    if (moved || Math.abs(dx) > 12 || Math.abs(dy) > 12) {
                        moved = true
                        lp.x = (ox + dx).toInt().coerceIn(0, maxOf(0, dm.widthPixels - size))
                        lp.y = (oy + dy).toInt().coerceIn(0, maxOf(0, dm.heightPixels - size))
                        try { wm.updateViewLayout(v, lp) } catch (_: Exception) {}
                    }
                }
                MotionEvent.ACTION_UP -> {
                    if (moved) sp.edit().putInt("x", lp.x).putInt("y", lp.y).apply()
                    else ctx.startActivity(Intent(ctx, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT))
                }
            }
            true
        }
        try { wm.addView(v, lp) } catch (_: Exception) {}
        view = v
        return v
    }
}
