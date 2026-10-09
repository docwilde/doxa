package ai.ampiric.doxa.remote

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class AndroidPushWireTest {
    @Test fun genericPushRequiresCurrentOpaqueTagAndOptIn() {
        val current = "abcdef0123456789abcdef0123456789"
        assertTrue(AndroidPushWire.accepted("needs_input", current, current, true))
        assertFalse(AndroidPushWire.accepted("needs_input", current, current, false))
        assertFalse(AndroidPushWire.accepted("needs_input", current, "0".repeat(32), true))
        assertFalse(AndroidPushWire.accepted("approval", current, current, true))
        assertFalse(AndroidPushWire.accepted("turn_done", "bad", "bad", true))
    }
    @Test fun tokenIsBoundedAndRejectsUrls() {
        assertTrue(AndroidPushWire.token("a:abcdefghijklmnopqrstuvwxyz-_."))
        assertFalse(AndroidPushWire.token("a".repeat(4097)))
        assertFalse(AndroidPushWire.token("https://example.com/device"))
    }
}
