package ai.ampiric.doxa.remote

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

class WireTest {
    private class BlockingInputStream(private val entered: CountDownLatch) : InputStream() {
        private val released = CountDownLatch(1)
        override fun read(): Int {
            entered.countDown()
            released.await()
            return -1
        }
        fun release() { released.countDown() }
    }

    private class StubConnection(url: URL, private val body: InputStream) : HttpURLConnection(url) {
        @Volatile var wasDisconnected = false
        override fun disconnect() { wasDisconnected = true; (body as? BlockingInputStream)?.release() }
        override fun usingProxy() = false
        override fun connect() {}
        override fun getResponseCode() = 200
        override fun getInputStream() = body
    }

    @Test fun privateOriginAndTargetAreStrict() {
        assertEquals("https://owner.tailnet.ts.net/", Wire.origin("https://owner.tailnet.ts.net/"))
        assertTrue(Wire.target("host-1~session-2"))
        for (raw in listOf("http://owner.tailnet.ts.net/", "https://owner.tailnet.ts.net.evil/",
            "https://user@owner.tailnet.ts.net/", "https://owner.tailnet.ts.net/path")) {
            assertThrows(IllegalArgumentException::class.java) { Wire.origin(raw) }
        }
        assertFalse(Wire.target("../host~session"))
    }

    @Test fun browserEnvelopeOpensAndContextIsBound() {
        val fixture = JSONObject(javaClass.getResource("/js-envelope.json")!!.readText())
        val key = Wire.key(fixture.getString("key"))
        val context = fixture.getString("context")
        val value = Wire.open(key, context, fixture.getJSONObject("envelope"))
        assertEquals(fixture.getJSONObject("value").getString("text"), value.getString("text"))
        assertThrows(IllegalStateException::class.java) {
            Wire.open(key, "other~session|command|prompt", fixture.getJSONObject("envelope"))
        }
        val sealed = Wire.seal(key, context, JSONObject().put("text", "Android payload"))
        assertEquals("Android payload", Wire.open(key, context, sealed).getString("text"))
    }

    @Test fun rustEnvelopeOpens() {
        val fixture = JSONObject(javaClass.getResource("/rust-envelope.json")!!.readText())
        val actual = Wire.open(Wire.key(fixture.getString("key")), fixture.getString("context"),
            fixture.getJSONObject("envelope"))
        assertTrue(fixture.getJSONObject("value").similar(actual))
    }

    @Test fun sharedEventVectorIsIncarnationBound() {
        val fixture = JSONObject(javaClass.getResource("/remote-event-v2.json")!!.readText())
        val target = fixture.getString("target")
        val incarnation = fixture.getString("incarnation")
        val context = Wire.eventContext(target, incarnation, fixture.getLong("seq"), fixture.getString("event_type"))
        assertEquals(fixture.getString("context"), context)
        val key = Wire.key(fixture.getString("key"))
        assertTrue(fixture.getJSONObject("value").similar(
            Wire.open(key, context, fixture.getJSONObject("envelope"))))

        val replay = JSONObject().put("type", "event").put("incarnation", incarnation)
            .put("seq", fixture.getLong("seq")).put("event", JSONObject()
                .put("type", fixture.getString("event_type")).put("data", JSONObject()
                    .put("sealed", fixture.getJSONObject("envelope"))))
        assertThrows(IllegalArgumentException::class.java) {
            decryptEvent(target, "fedcba9876543210fedcba9876543210", key, replay)
        }
        replay.put("incarnation", "fedcba9876543210fedcba9876543210")
        assertThrows(IllegalStateException::class.java) {
            decryptEvent(target, replay.getString("incarnation"), key, replay)
        }
    }

    @Test fun eventContextRejectsSeparatorsAndLegacyContext() {
        assertEquals("host~session|event-v2|incarnation-1|7|text_delta",
            Wire.eventContext("host~session", "incarnation-1", 7, "text_delta"))
        assertThrows(IllegalArgumentException::class.java) {
            Wire.eventContext("host~session", "bad|incarnation", 7, "text_delta")
        }
    }

    @Test fun finiteHttpOperationHasWallClockDeadlineAndCancellationClosesSse() {
        val deadlineEntered = CountDownLatch(1)
        lateinit var deadlineConnection: StubConnection
        val finite = HubApi("https://owner.tailnet.ts.net/", null, 100) { url ->
            StubConnection(url, BlockingInputStream(deadlineEntered)).also { deadlineConnection = it }
        }
        assertThrows(SocketTimeoutException::class.java) { runBlocking { finite.sessions() } }
        assertTrue(deadlineEntered.await(1, TimeUnit.SECONDS))
        assertTrue(deadlineConnection.wasDisconnected)

        val streamEntered = CountDownLatch(1)
        lateinit var streamConnection: StubConnection
        val streaming = HubApi("https://owner.tailnet.ts.net/", null, 5_000) { url ->
            StubConnection(url, BlockingInputStream(streamEntered)).also { streamConnection = it }
        }
        runBlocking {
            val parent = SupervisorJob()
            val scope = CoroutineScope(parent + Dispatchers.Default)
            val job = scope.launch { streaming.events("host~session", false, "incarnation-1", 0) {} }
            assertTrue(streamEntered.await(1, TimeUnit.SECONDS))
            job.cancelAndJoin()
            parent.cancel()
        }
        assertTrue("cancelled SSE socket must be closed immediately", streamConnection.wasDisconnected)
    }

    @Test fun retriesKeepTheExactRequestAndId() {
        val api = HubApi("https://owner.tailnet.ts.net/", ByteArray(32) { 7 })
        val boot = "0123456789abcdef0123456789abcdef"
        api.parseInventory(JSONObject().put("hub_boot", boot).put("sessions", org.json.JSONArray()))
        val request = api.prepare("host~session", "prompt", JSONObject().put("text", "hello"), true,
            "incarnation-1")
        assertTrue(Wire.id(request.requestId))
        assertTrue(AndroidWriteId.valid(request.requestId))
        assertTrue(request.requestId.startsWith("$boot-"))
        assertEquals(request.requestId, request.body.getString("request_id"))
        assertEquals(boot, request.body.getString("hub_boot"))
        assertEquals("incarnation-1", request.body.getString("incarnation"))
        val plain = Wire.open(ByteArray(32) { 7 }, "host~session|command|prompt",
            request.body.getJSONObject("sealed"))
        assertEquals(request.requestId, plain.getString("request_id"))
        assertEquals(boot, plain.getString("hub_boot"))
        assertEquals("incarnation-1", plain.getString("incarnation"))
        assertEquals("hello", plain.getString("text"))
        val plainRequest = api.prepare("host~session", "answer", JSONObject().put("id", "question-1")
            .put("answer", JSONObject().put("decision", "deny"))
            .put("reviewed_request", JSONObject().put("id", "question-1")
                .put("options", org.json.JSONArray().put("deny").put("allow"))), false, "incarnation-1")
        assertEquals(boot, plainRequest.body.getString("hub_boot"))
        assertEquals("incarnation-1", plainRequest.body.getString("incarnation"))
        assertEquals("question-1", plainRequest.body.getJSONObject("reviewed_request").getString("id"))
        assertThrows(Exception::class.java) {
            api.prepare("host~session", "answer", JSONObject().put("id", "question-1")
                .put("answer", JSONObject().put("decision", "deny")), false, "incarnation-1")
        }
        assertThrows(Exception::class.java) {
            HubApi("https://owner.tailnet.ts.net/", null).prepare("host~session", "prompt",
                JSONObject().put("text", "hello"), false, "incarnation-1")
        }
        assertThrows(Exception::class.java) {
            api.prepare("host~session", "prompt", JSONObject().put("text", "hello"), false,
                "different\nincarnation")
        }
        assertFalse(AndroidWriteId.valid("00000000-0000-4000-8000-000000000001"))
        assertFalse(AndroidWriteId.valid("$boot-00000000-0000-4000-8000-00000000000A"))
        assertThrows(Exception::class.java) {
            HubApi("https://owner.tailnet.ts.net/", null).parseInventory(JSONObject()
                .put("sessions", org.json.JSONArray()))
        }
    }
}
