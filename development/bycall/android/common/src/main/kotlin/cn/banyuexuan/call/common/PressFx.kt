package cn.banyuexuan.call.common

import android.animation.ValueAnimator
import android.annotation.SuppressLint
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.animation.OvershootInterpolator
import android.widget.TextView

/** 按钮操作反馈:按下时缩小 + 轻震,松开时弹回;点击成功后闪一下并短暂显示"✓",让使用者确信操作生效。 */
object PressFx {
    /** 给按钮加按压动画。holdMs>0 用于长按按钮:按住期间按钮上渐渐泛白,表示"继续按住就会触发"。 */
    @SuppressLint("ClickableViewAccessibility")
    fun attach(v: View, holdMs: Long = 0, scale: Float = 0.94f, onHold: ((Boolean) -> Unit)? = null) {
        var fill: ValueAnimator? = null
        val overlay = GradientDrawable().apply { setColor(Color.WHITE); cornerRadius = 14f * v.resources.displayMetrics.density; alpha = 0 }
        if (holdMs > 0) v.foreground = overlay
        v.setOnTouchListener { view, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    view.performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
                    onHold?.invoke(true)
                    view.animate().cancel()
                    view.animate().scaleX(scale).scaleY(scale).setDuration(70).start()
                    if (holdMs > 0) fill = ValueAnimator.ofInt(0, 120).apply {
                        duration = holdMs; addUpdateListener { overlay.alpha = it.animatedValue as Int }; start() }
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                    fill?.cancel(); overlay.alpha = 0
                    onHold?.invoke(false)
                    view.animate().cancel()
                    view.animate().scaleX(1f).scaleY(1f).setDuration(180).setInterpolator(OvershootInterpolator(3f)).start()
                }
            }
            false      // 不拦截:点击/长按照常触发
        }
    }

    /** 点击成功:闪一下 + 确认震动 + (可选)按钮文字短暂变成 label。 */
    fun confirm(v: View, label: String? = null, restoreMs: Long = 900) {
        v.performHapticFeedback(if (Build.VERSION.SDK_INT >= 30) HapticFeedbackConstants.CONFIRM else HapticFeedbackConstants.LONG_PRESS)
        v.animate().cancel(); v.alpha = 1f
        v.animate().alpha(0.5f).setDuration(80).withEndAction { v.animate().alpha(1f).setDuration(200).start() }.start()
        if (v is TextView && label != null) {
            val old = v.text
            v.text = label
            v.postDelayed({ if (v.text == label) v.text = old }, restoreMs)
        }
    }

    val longPressMs: Long get() = ViewConfiguration.getLongPressTimeout().toLong()
}
