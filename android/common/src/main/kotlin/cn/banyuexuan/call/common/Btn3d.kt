package cn.banyuexuan.call.common

import android.content.Context
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.LayerDrawable
import android.graphics.drawable.StateListDrawable
import android.widget.Button

/** 立体按钮:上面是带渐变和高光的按钮面,下面露出一条深色"厚度";按下时按钮面下沉,厚度变薄,像真的按键。 */
object Btn3d {
    private fun mix(c: Int, to: Int, t: Float) = Color.rgb(
        (Color.red(c) + (Color.red(to) - Color.red(c)) * t).toInt(),
        (Color.green(c) + (Color.green(to) - Color.green(c)) * t).toInt(),
        (Color.blue(c) + (Color.blue(to) - Color.blue(c)) * t).toInt())

    private fun layers(ctx: Context, base: Int, radiusDp: Int, depthDp: Int, down: Int, dim: Float): LayerDrawable {
        val d = ctx.resources.displayMetrics.density
        val r = radiusDp * d
        val edge = GradientDrawable().apply { setColor(mix(base, Color.BLACK, 0.32f + dim)); cornerRadius = r }
        val face = GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM,
            intArrayOf(mix(base, Color.WHITE, 0.28f - dim), mix(base, Color.WHITE, 0.04f), mix(base, Color.BLACK, 0.06f + dim))).apply {
            cornerRadius = r; setStroke((1.2f * d).toInt().coerceAtLeast(1), mix(base, Color.WHITE, 0.5f))
        }
        return LayerDrawable(arrayOf(edge, face)).apply {
            val dep = (depthDp * d).toInt(); val dn = (down * d).toInt()
            setLayerInset(1, 0, dn, 0, dep - dn)
        }
    }

    fun drawable(ctx: Context, base: Int, radiusDp: Int = 16, depthDp: Int = 7) = StateListDrawable().apply {
        addState(intArrayOf(android.R.attr.state_pressed), layers(ctx, base, radiusDp, depthDp, depthDp - 2, 0.05f))
        addState(intArrayOf(), layers(ctx, base, radiusDp, depthDp, 0, 0f))
    }

    /** 给 Button 套上立体外观(去掉系统默认阴影,文字落在按钮面正中)。 */
    fun apply(b: Button, base: Int, radiusDp: Int = 16, depthDp: Int = 7) {
        val d = b.resources.displayMetrics.density
        b.stateListAnimator = null
        b.isAllCaps = false
        b.minHeight = 0; b.minimumHeight = 0
        b.background = drawable(b.context, base, radiusDp, depthDp)
        b.setPadding((8 * d).toInt(), 0, (8 * d).toInt(), (depthDp * d).toInt())
        b.setShadowLayer(2f * d, 0f, 1f * d, Color.argb(90, 0, 0, 0))
    }
}
