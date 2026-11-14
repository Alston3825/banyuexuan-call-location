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

    private fun nowIso(): String =
        SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSSXXX", Locale.US).format(Date())

    fun envelope(store: String, device: String, type: String, payload: JSONObject = JSONObject(),
                 callId: String? = null, msgId: String = UUID.randomUUID().toString().replace("-", "")): JSONObject =
        JSONObject().put("msg_id", msgId).put("store_id", store).put("device_id", device)
            .put("timestamp", nowIso()).put("type", type).put("payload", payload)
            .apply { if (callId != null) put("call_id", callId) }
}
