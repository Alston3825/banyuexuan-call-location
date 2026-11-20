package cn.banyuexuan.call.watch

import android.content.Context
import android.media.MediaRecorder
import android.os.SystemClock
import java.io.File

/**
 * 按住说话录音(AAC/m4a,16 kHz 单声道,约 4 KB/秒)。麦克风只在按住期间打开。
 * 到达最长时长时自动停止并回调 onMax(由调用方发送)。
 */
class VoiceRec(private val ctx: Context) {
    private var rec: MediaRecorder? = null
    private var file: File? = null
    private var startedAt = 0L

    val recording get() = rec != null

    fun start(maxSeconds: Int, onMax: () -> Unit): Boolean {
        cancel()
        val f = File(ctx.cacheDir, "chat-${System.currentTimeMillis()}.m4a")
        return try {
            @Suppress("DEPRECATION")
            val r = (if (android.os.Build.VERSION.SDK_INT >= 31) MediaRecorder(ctx) else MediaRecorder()).apply {
                setAudioSource(MediaRecorder.AudioSource.MIC); setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setAudioEncoder(MediaRecorder.AudioEncoder.AAC); setAudioSamplingRate(16000); setAudioEncodingBitRate(32000)
                setAudioChannels(1); setOutputFile(f.path); setMaxDuration(maxSeconds * 1000)
                setOnInfoListener { _, what, _ -> if (what == MediaRecorder.MEDIA_RECORDER_INFO_MAX_DURATION_REACHED) onMax() }
                prepare(); start()
            }
            rec = r; file = f; startedAt = SystemClock.elapsedRealtime(); true
        } catch (_: Throwable) { cancel(); false }
    }

    /** 停止并返回 (文件, 秒数);太短(<0.8 秒)或失败返回 null。 */
    fun stop(): Pair<File, Double>? {
        val r = rec ?: return null
        val secs = (SystemClock.elapsedRealtime() - startedAt) / 1000.0
        rec = null
        val ok = try { r.stop(); true } catch (_: Throwable) { false }
        try { r.release() } catch (_: Throwable) { }
        val f = file; file = null
        if (!ok || f == null || secs < 0.8 || !f.exists() || f.length() == 0L) { f?.delete(); return null }
        return f to secs
    }

    fun cancel() {
        rec?.let { try { it.stop() } catch (_: Throwable) { }; try { it.release() } catch (_: Throwable) { } }
        rec = null; file?.delete(); file = null
    }
}
