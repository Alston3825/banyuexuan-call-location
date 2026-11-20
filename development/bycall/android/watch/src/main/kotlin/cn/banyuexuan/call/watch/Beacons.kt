package cn.banyuexuan.call.watch

import android.bluetooth.le.ScanResult

/** Standard iBeacon manufacturer payload, shared by native filtering and callback validation. */
object Beacons {
    data class Reading(val id: String, val rssi: Int, val timestampMs: Long? = null,
                       val sampleCount: Int = 1, val ageMs: Long = 0)

    /** Non-empty 02 15 prefix even when no UUID is configured; malformed UUID fails closed. */
    fun filterPrefix(uuidFilter: String): ByteArray {
        val raw = uuidFilter.trim()
        require(raw.isEmpty() || raw.matches(Regex("[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")))
        val uuid = raw.replace("-", "")
        return byteArrayOf(0x02, 0x15) + uuid.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
    }

    fun parseData(d: ByteArray, rssi: Int, uuidFilter: String, timestampMs: Long? = null): Reading? {
        val prefix = try { filterPrefix(uuidFilter) } catch (_: IllegalArgumentException) { return null }
        if (d.size < 23 || !d.copyOfRange(0, prefix.size).contentEquals(prefix)) return null
        if (rssi !in -127..0) return null
        val major = ((d[18].toInt() and 0xFF) shl 8) or (d[19].toInt() and 0xFF)
        val minor = ((d[20].toInt() and 0xFF) shl 8) or (d[21].toInt() and 0xFF)
        return Reading("$major-$minor", rssi, timestampMs)
    }

    fun parse(r: ScanResult, uuidFilter: String): Reading? {
        val d = r.scanRecord?.getManufacturerSpecificData(0x004C) ?: return null
        return parseData(d, r.rssi, uuidFilter, r.timestampNanos / 1_000_000)
    }

    /** Only actual, recent samples contribute to RSSI; drain never invents empty observations. */
    class Window(private val clock: () -> Long = { android.os.SystemClock.elapsedRealtime() }) {
        private val samples = HashMap<String, MutableList<Reading>>()
        @Synchronized fun add(r: Reading) {
            if (r.id !in samples && samples.size >= 128) return
            val list = samples.getOrPut(r.id) { mutableListOf() }
            if (list.size < 10000 && samples.size <= 128) list.add(r.copy(timestampMs = r.timestampMs ?: clock()))
        }
        @Synchronized fun drain(maxAgeMs: Long = 60000): List<Reading> {
            val now = clock()
            val result = samples.mapNotNull { (id, all) ->
                val recent = all.filter { now - (it.timestampMs ?: now) in 0..maxAgeMs }
                if (recent.isEmpty()) null else Reading(id, recent.sumOf { it.rssi } / recent.size,
                    recent.maxOf { it.timestampMs ?: now }, recent.size,
                    now - recent.maxOf { it.timestampMs ?: now })
            }
            samples.clear()
            return result.take(128)
        }
    }
}
