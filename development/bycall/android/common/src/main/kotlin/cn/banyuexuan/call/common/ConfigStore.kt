package cn.banyuexuan.call.common

/** Persistence boundary shared by production preferences and isolated JVM lifecycle tests. */
interface ConfigStore {
    operator fun get(key: String): String
    fun commit(values: Map<String, String>): Boolean
    val deviceId: String
    val apiBase: String
    val store: String
}
