package ai.ampiric.doxa.remote

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class WireTest {
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
            .put("answer", JSONObject().put("decision", "deny")), false, "incarnation-1")
        assertEquals(boot, plainRequest.body.getString("hub_boot"))
        assertEquals("incarnation-1", plainRequest.body.getString("incarnation"))
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
