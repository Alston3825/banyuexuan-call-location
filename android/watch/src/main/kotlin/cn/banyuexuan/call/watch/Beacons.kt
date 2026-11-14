package cn.banyuexuan.call.watch

import android.bluetooth.le.ScanResult

/**
 * iBeacon parsing. The reported beacon_id is "<major>-<minor>"; the management system's beacon deviceCode
 * must use the same format. Only beacons of the store's own UUID are accepted.
 */
object Beacons {
    data class Reading(val id: String, val rssi: Int)

    fun parse(r: ScanResult, uuidFilter: String): Reading? {
        val d = r.scanRecord?.getManufacturerSpecificData(0x004C) ?: return null
        if (d.size < 23 || d[0].toInt() != 0x02 || d[1].toInt() != 0x15) return null
        val uuid = d.slice(2..17).joinToString("") { "%02x".format(it) }
        if (uuidFilter.isNotBlank() && uuid != uuidFilter.replace("-", "").lowercase()) return null
        val major = ((d[18].toInt() and 0xFF) shl 8) or (d[19].toInt() and 0xFF)
        val minor = ((d[20].toInt() and 0xFF) shl 8) or (d[21].toInt() and 0xFF)
        return Reading("$major-$minor", r.rssi)
    }

    /** Collects readings over one reporting window and returns the mean RSSI per beacon. */
    class Window {
        private val sum = HashMap<String, Int>(); private val n = HashMap<String, Int>()
        @Synchronized fun add(r: Reading) { sum[r.id] = (sum[r.id] ?: 0) + r.rssi; n[r.id] = (n[r.id] ?: 0) + 1 }
        @Synchronized fun drain(): List<Reading> =
            sum.map { (k, v) -> Reading(k, v / n[k]!!) }.also { sum.clear(); n.clear() }
    }
}
