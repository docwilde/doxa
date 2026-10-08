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
        val request = api.prepare("host~session", "prompt", JSONObject().put("text", "hello"), true)
        assertTrue(Wire.id(request.requestId))
        assertEquals(request.requestId, request.body.getString("request_id"))
        val plain = Wire.open(ByteArray(32) { 7 }, "host~session|command|prompt",
            request.body.getJSONObject("sealed"))
        assertEquals(request.requestId, plain.getString("request_id"))
        assertEquals("hello", plain.getString("text"))
    }
}
