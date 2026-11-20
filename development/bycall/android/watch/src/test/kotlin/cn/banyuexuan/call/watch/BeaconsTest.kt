package cn.banyuexuan.call.watch

import org.junit.Assert.*
import org.junit.Test

class BeaconsTest {
    private val uuid = "00112233-4455-6677-8899-aabbccddeeff"
    @Test fun nativeFilterMatchesIBeaconHeaderAndOptionalUuid() {
        assertArrayEquals(byteArrayOf(2,21), Beacons.filterPrefix(""))
        val prefix = Beacons.filterPrefix(uuid)
        assertEquals(18, prefix.size)
        assertEquals(0xFF.toByte(), prefix.last())
        assertArrayEquals(prefix, Beacons.filterPrefix(uuid.uppercase().replace("-", "")))
    }
    @Test fun invalidUuidDoesNotBecomeUnfilteredScan() {
        try { Beacons.filterPrefix("invalid"); fail("invalid UUID accepted") } catch (_: IllegalArgumentException) { }
        try { Beacons.filterPrefix("-".repeat(32)); fail("invalid UUID accepted") } catch (_: IllegalArgumentException) { }
    }
    @Test fun decodeSignedBytesAndRejectOtherUuidOrMalformedPayload() {
        val data = Beacons.filterPrefix(uuid) + byteArrayOf(0xFF.toByte(), 0xFE.toByte(), 0x80.toByte(), 1, (-59).toByte())
        val reading = Beacons.parseData(data, -55, uuid)!!
        assertEquals("65534-32769", reading.id)
        assertNull(Beacons.parseData(data, -55, "f".repeat(32)))
        assertNull(Beacons.parseData(data.copyOf(10), -55, ""))
        assertNull(Beacons.parseData(data, 127, uuid))
    }
    @Test fun averageCountAgeAndDrainAreBasedOnActualSamples() {
        var now = 1000L
        val window = Beacons.Window { now }
        window.add(Beacons.Reading("16-1", -50, 900))
        window.add(Beacons.Reading("16-1", -60, 950))
        now = 1100
        val reading = window.drain(500).single()
        assertEquals(-55, reading.rssi); assertEquals(2, reading.sampleCount); assertEquals(150L, reading.ageMs)
        assertTrue(window.drain().isEmpty())
    }
    @Test fun staleAndFutureCallbackSamplesAreExcludedFromMean() {
        val window = Beacons.Window { 10000 }
        window.add(Beacons.Reading("16-1", -20, 100))
        window.add(Beacons.Reading("16-1", -50, 9900))
        window.add(Beacons.Reading("16-1", -10, 12000))
        val reading = window.drain(1000).single()
        assertEquals(-50, reading.rssi); assertEquals(1, reading.sampleCount)
    }
    @Test fun windowBoundsDistinctBeaconCount() {
        val window = Beacons.Window { 10000 }
        for(i in 0..1000) window.add(Beacons.Reading("16-$i", -60))
        assertEquals(128, window.drain().size)
    }
}
