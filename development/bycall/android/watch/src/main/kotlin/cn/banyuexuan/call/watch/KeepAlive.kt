package cn.banyuexuan.call.watch

import android.app.AlarmManager
import android.app.PendingIntent
import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.SystemClock

/**
 * 防止后台服务被杀后不再收呼叫(兼容 Android 8/9,只用标准接口):
 *  1) 每 15 分钟一次的系统任务(JobScheduler,重启后仍有效):服务没在跑就拉起来;
 *  2) 应用从“最近任务”里被划掉时(onTaskRemoved),2 秒后用闹钟重新拉起服务。
 * 被“强行停止”的应用安卓不允许自动拉起,只能等手表重启或有人打开应用——这是系统限制。
 */
object KeepAlive {
    private const val JOB_ID = 4711

    fun schedule(ctx: Context) {
        try {
            val js = ctx.getSystemService(JobScheduler::class.java) ?: return
            if (js.allPendingJobs.any { it.id == JOB_ID }) return
            js.schedule(JobInfo.Builder(JOB_ID, ComponentName(ctx, KeepAliveJob::class.java))
                .setPeriodic(15 * 60_000L).setPersisted(true).build())
        } catch (_: Throwable) { }
    }

    fun start(ctx: Context) {
        try { ctx.startForegroundService(Intent(ctx, WatchService::class.java)) } catch (_: Throwable) { }
    }

    /** 2 秒后再拉起服务(用于被划掉后自救)。 */
    fun restartSoon(ctx: Context) {
        try {
            val pi = PendingIntent.getForegroundService(ctx, 7, Intent(ctx, WatchService::class.java),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            val am = ctx.getSystemService(AlarmManager::class.java) ?: return
            val at = SystemClock.elapsedRealtime() + 2000
            // 安卓 12+ 需要“精确闹钟”权限;没有就退回普通闹钟(可能晚几秒)。手表(安卓 9)不受影响
            if (android.os.Build.VERSION.SDK_INT >= 31 && !am.canScheduleExactAlarms())
                am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pi)
            else
                am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pi)
        } catch (_: Throwable) { }
    }
}

class KeepAliveJob : JobService() {
    override fun onStartJob(p: JobParameters?): Boolean {
        if (WatchService.instance == null) KeepAlive.start(this)
        return false
    }

    override fun onStopJob(p: JobParameters?): Boolean = true
}
