package cn.banyuexuan.call.common

import com.hivemq.client.mqtt.MqttClient
import com.hivemq.client.mqtt.datatypes.MqttQos
import com.hivemq.client.mqtt.mqtt3.Mqtt3AsyncClient
import org.json.JSONObject
import java.util.Timer
import java.util.TimerTask
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.TimeUnit

/**
 * MQTT 3.1.1 client with auto-reconnect; resubscribes after every (re)connect. Callbacks arrive on worker threads.
 *
 * 断线恢复(2026-11-17):主机更新/重启时消息服务会停一会儿。
 *  - 自动重连间隔最长 10 秒(库默认会翻倍到 2 分钟,主机恢复后要等很久才连上);
 *  - 看门狗:断开超过 60 秒还没连上,就整个重建客户端(等同于手动“保存并重新连接”),防止库的自动重连卡住。
 */
class MqttBus(private val host: String, private val port: Int, private val clientId: String,
              private val username: String? = null, private val password: String? = null,
              private val willTopic: String? = null, private val willPayload: JSONObject? = null) {
    private val subs = CopyOnWriteArrayList<Pair<String, (String, JSONObject) -> Unit>>()
    @Volatile var connected = false; private set
    var onConnection: ((Boolean) -> Unit)? = null
    var onConnectedHook: (() -> Unit)? = null     // e.g. publish an "online" STATUS, flush queues

    @Volatile private var downSince = System.currentTimeMillis()
    @Volatile private var started = false
    @Volatile private var stopped = false
    private var watchdog: Timer? = null
    @Volatile private var client: Mqtt3AsyncClient = build()

    private fun build(): Mqtt3AsyncClient {
        lateinit var c: Mqtt3AsyncClient
        c = MqttClient.builder().useMqttVersion3()
            .identifier(clientId).serverHost(host).serverPort(port)
            .automaticReconnect().initialDelay(1, TimeUnit.SECONDS).maxDelay(10, TimeUnit.SECONDS).applyAutomaticReconnect()
            .addConnectedListener {
                if (c !== client) { try { c.disconnect() } catch (_: Throwable) { }; return@addConnectedListener }   // 已被重建替换的旧客户端
                connected = true
                // Run the hook only after every SUBACK: a reply to our "online" message must not arrive before we listen.
                val acks = subs.map { subscribeNow(it.first, it.second) }
                java.util.concurrent.CompletableFuture.allOf(*acks.toTypedArray()).whenComplete { _, _ ->
                    onConnectedHook?.invoke()
                    onConnection?.invoke(true)
                }
            }
            .addDisconnectedListener { ctx ->
                if (c !== client || stopped) { try { ctx.reconnector.reconnect(false) } catch (_: Throwable) { }; return@addDisconnectedListener }
                if (connected) downSince = System.currentTimeMillis()     // 只在“从连上变断开”时记时间
                connected = false
                onConnection?.invoke(false)
            }
            .buildAsync()
        return c
    }

    fun connect() {
        started = true; stopped = false
        downSince = System.currentTimeMillis()
        sendConnect(client)
        if (watchdog == null) watchdog = Timer(true).apply {
            schedule(object : TimerTask() { override fun run() {
                try {
                    if (started && !stopped && !connected && System.currentTimeMillis() - downSince > 60_000) rebuild()
                } catch (_: Throwable) { }
            } }, 15_000, 15_000)
        }
    }

    /** 自动重连迟迟不成功:丢掉旧客户端,新建一个重新连接。 */
    private fun rebuild() {
        val old = client
        client = build()
        downSince = System.currentTimeMillis()
        try { old.disconnect() } catch (_: Throwable) { }
        sendConnect(client)
    }

    private fun sendConnect(c: Mqtt3AsyncClient) {
        var b = c.connectWith().keepAlive(30).cleanSession(true)
        if (!username.isNullOrEmpty() && !password.isNullOrEmpty())
            b = b.simpleAuth().username(username).password(password.toByteArray()).applySimpleAuth()
        if (willTopic != null && willPayload != null) {
            b = b.willPublish().topic(willTopic).payload(willPayload.toString().toByteArray())
                .qos(MqttQos.AT_LEAST_ONCE).applyWillPublish()
        }
        b.send()
    }

    fun disconnect() {
        stopped = true
        watchdog?.cancel(); watchdog = null
        try { client.disconnect() } catch (_: Throwable) { }
    }

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
