package cn.banyuexuan.call.common

import com.hivemq.client.mqtt.MqttClient
import com.hivemq.client.mqtt.datatypes.MqttQos
import com.hivemq.client.mqtt.mqtt3.Mqtt3AsyncClient
import org.json.JSONObject
import java.util.concurrent.CopyOnWriteArrayList

/** MQTT 3.1.1 client with auto-reconnect; resubscribes after every (re)connect. Callbacks arrive on worker threads. */
class MqttBus(host: String, port: Int, clientId: String,
              private val username: String? = null, private val password: String? = null,
              private val willTopic: String? = null, private val willPayload: JSONObject? = null) {
    private val subs = CopyOnWriteArrayList<Pair<String, (String, JSONObject) -> Unit>>()
    @Volatile var connected = false; private set
    var onConnection: ((Boolean) -> Unit)? = null
    var onConnectedHook: (() -> Unit)? = null     // e.g. publish an "online" STATUS, flush queues

    private val client: Mqtt3AsyncClient = MqttClient.builder().useMqttVersion3()
        .identifier(clientId).serverHost(host).serverPort(port)
        .automaticReconnectWithDefaultConfig()
        .addConnectedListener {
            connected = true
            // Run the hook only after every SUBACK: a reply to our "online" message must not arrive before we listen.
            val acks = subs.map { subscribeNow(it.first, it.second) }
            java.util.concurrent.CompletableFuture.allOf(*acks.toTypedArray()).whenComplete { _, _ ->
                onConnectedHook?.invoke()
                onConnection?.invoke(true)
            }
        }
        .addDisconnectedListener { connected = false; onConnection?.invoke(false) }
        .buildAsync()

    fun connect() {
        var b = client.connectWith().keepAlive(30).cleanSession(true)
        if (!username.isNullOrEmpty() && !password.isNullOrEmpty())
            b = b.simpleAuth().username(username).password(password.toByteArray()).applySimpleAuth()
        if (willTopic != null && willPayload != null) {
            b = b.willPublish().topic(willTopic).payload(willPayload.toString().toByteArray())
                .qos(MqttQos.AT_LEAST_ONCE).applyWillPublish()
        }
        b.send()
    }

    fun disconnect() { client.disconnect() }

    fun subscribe(topic: String, handler: (String, JSONObject) -> Unit) {
        subs.add(topic to handler)
        if (connected) subscribeNow(topic, handler)
    }

    private fun subscribeNow(topic: String, handler: (String, JSONObject) -> Unit): java.util.concurrent.CompletableFuture<*> {
        return client.subscribeWith().topicFilter(topic).qos(MqttQos.AT_LEAST_ONCE).callback { pub ->
            try { handler(pub.topic.toString(), JSONObject(String(pub.payloadAsBytes))) } catch (_: Exception) {}
        }.send()
    }

    /** Returns false when not connected (caller decides whether to queue). */
    fun publish(topic: String, msg: JSONObject, qos0: Boolean = false): Boolean {
        if (!connected) return false
        client.publishWith().topic(topic).payload(msg.toString().toByteArray())
            .qos(if (qos0) MqttQos.AT_MOST_ONCE else MqttQos.AT_LEAST_ONCE).send()
        return true
    }
}
