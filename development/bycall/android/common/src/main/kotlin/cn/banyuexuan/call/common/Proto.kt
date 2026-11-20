package cn.banyuexuan.call.common

import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

/** Message protocol, mirrors host/bycall/core/protocol.py. */
object Proto {
    const val CALL_REQUEST = "CALL_REQUEST"
    const val CALL_DISPATCH = "CALL_DISPATCH"
    const val CALL_TAKEN = "CALL_TAKEN"
    const val CALL_STATE = "CALL_STATE"
    const val CALL_ACCEPT = "CALL_ACCEPT"
    const val CALL_ARRIVE = "CALL_ARRIVE"
    const val CALL_COMPLETE = "CALL_COMPLETE"
    const val CALL_CANCEL = "CALL_CANCEL"
    const val CALL_TRANSFER = "CALL_TRANSFER"
    const val CALL_FEEDBACK = "CALL_FEEDBACK"
    const val CMD_REJECTED = "CMD_REJECTED"
    const val HEARTBEAT = "HEARTBEAT"
    const val LOCATION_REPORT = "LOCATION_REPORT"
    const val VITALS_REPORT = "VITALS_REPORT"
    const val STATUS = "STATUS"
    const val DEVICE_CONFIG = "DEVICE_CONFIG"
    // 2026-11-17:回执、一键求助、同事沟通、换表(与主机 protocol.py 一致)
    const val CALL_RECEIVED = "CALL_RECEIVED"
    const val HELP_REQUEST = "HELP_REQUEST"
    const val HELP_STATE = "HELP_STATE"
    const val CHAT_SEND = "CHAT_SEND"
    const val CHAT_MSG = "CHAT_MSG"
    const val CHAT_ACK = "CHAT_ACK"
    const val WATCH_BIND = "WATCH_BIND"
    const val WATCH_RESET = "WATCH_RESET"
    const val RESET_DONE = "RESET_DONE"
    const val RETURN_HOME = "RETURN_HOME"
    const val APP_UPDATE = "APP_UPDATE"            // 主机刚发布了新版本:马上检查更新

    fun tCall(store: String, floor: String, room: String) = "$store/call/$floor/$room"
    fun tDispatchWatch(store: String, watch: String) = "$store/dispatch/$watch"
    fun tAck(store: String, callId: String) = "$store/ack/$callId"
    fun tFeedback(store: String, room: String) = "$store/feedback/$room"
    fun tHeartbeat(store: String, dev: String) = "$store/heartbeat/$dev"
    fun tLocation(store: String, watch: String) = "$store/location/$watch"
    fun tVitals(store: String, watch: String) = "$store/vitals/$watch"
    fun tStatus(store: String, dev: String) = "$store/status/$dev"
    fun tReply(store: String, dev: String) = "$store/reply/$dev"
    fun tConfig(store: String, dev: String) = "$store/config/$dev"
    fun tUp(store: String, dev: String) = "$store/up/$dev"          // 手表上行:求助 / 同事消息 / 一键归位 / 清空完成

    private fun nowIso(): String =
        SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.US).format(Date())

    fun envelope(store: String, device: String, type: String, payload: JSONObject = JSONObject(),
                 callId: String? = null, msgId: String = UUID.randomUUID().toString().replace("-", "")): JSONObject =
        JSONObject().put("msg_id", msgId).put("store_id", store).put("device_id", device)
            .put("timestamp", nowIso()).put("type", type).put("payload", payload)
            .apply { if (callId != null) put("call_id", callId) }
}
