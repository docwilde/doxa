package ai.ampiric.doxa.remote

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class PendingWriteRecoveryTest {
    private val scope = PendingWriteScope("https://owner.tailnet.ts.net/", "host~session", "incarnation-1")
    private val marker = PendingWriteMarker(scope, "prompt", "write-1", 1_000)

    private class Store(var value: String? = null) : PendingWriteMarkerStore {
        var failWrite = false
        var failClear = false
        var writes = 0
        override fun read(): String? = value
        override fun write(value: String): Boolean {
            writes++
            if (failWrite) return false
            this.value = value
            return true
        }
        override fun clear(): Boolean {
            if (failClear) return false
            value = null
            return true
        }
    }

    @Test fun markerRoundTripsWithoutUserContent() {
        val encoded = marker.encode()
        assertEquals(marker, PendingWriteMarker.decode(encoded))
        assertEquals(setOf("v", "origin", "target", "incarnation", "operation", "request_id", "created_at_ms"),
            JSONObject(encoded).keys().asSequence().toSet())
        for (secret in listOf("prompt text", "approval answer", "shared key", "sealed", "payload", "body")) {
            assertFalse(encoded.contains(secret))
        }
    }

    @Test fun malformedOrOversizedMarkersFailClosed() {
        for (raw in listOf("", "{}", marker.encode().replace("write-1", "../write"),
            marker.encode().replace("host~session", "other"),
            marker.encode().replace("\"v\":1", "\"v\":2"),
            marker.encode().replace("\"operation\":\"prompt\"", "\"operation\":12"),
            marker.encode().dropLast(1) + ",\"body\":\"secret\"}",
            "x".repeat(1_025))) {
            assertThrows(Exception::class.java) { PendingWriteMarker.decode(raw) }
            assertTrue(PendingWriteGuard(Store(raw)).blocked)
        }
        assertFalse(PendingWriteMarker(scope.copy(incarnation = "x\n"), "prompt", "write-1", 1).valid())
        assertFalse(PendingWriteMarker(scope.copy(incarnation = ""), "prompt", "write-1", 1).valid())
    }

    @Test fun failedDurableSavePreventsSubmissionAndProcessRestartRetainsBlock() {
        val store = Store().also { it.failWrite = true }
        val guard = PendingWriteGuard(store)
        assertFalse(guard.begin(marker))
        assertNull(store.value)
        store.failWrite = false
        assertTrue(guard.begin(marker))
        assertEquals(2, store.writes)
        val restarted = PendingWriteGuard(store)
        assertTrue(restarted.blocked)
        assertEquals(marker, restarted.marker)
        assertFalse(restarted.begin(marker.copy(requestId = "write-2")))
        assertFalse(restarted.canAcknowledge(scope))
        restarted.observeSnapshot(scope, true)
        assertTrue(restarted.canAcknowledge(scope))
        store.failClear = true
        assertFalse(restarted.acknowledgeAfterReview(scope))
        assertTrue(restarted.blocked)
        store.failClear = false
        assertTrue(restarted.acknowledgeAfterReview(scope))
        assertFalse(restarted.blocked)
    }

    @Test fun storageExceptionFailsClosedBeforeSubmission() {
        val store = object : PendingWriteMarkerStore {
            override fun read(): String? = null
            override fun write(value: String): Boolean = error("disk unavailable")
            override fun clear(): Boolean = true
        }
        val guard = PendingWriteGuard(store)
        assertFalse(guard.begin(marker))
        assertNull(guard.marker)
    }

    @Test fun scopeChangeNeedsNewSnapshotAndExplicitReview() {
        val store = Store(marker.encode())
        val guard = PendingWriteGuard(store)
        val other = scope.copy(target = "host~other", incarnation = "incarnation-2")
        guard.observeSnapshot(scope, true)
        assertTrue(guard.canAcknowledge(scope))
        for (changed in listOf(other, scope.copy(origin = "https://other.tailnet.ts.net/"),
            scope.copy(incarnation = "incarnation-2"))) {
            guard.observeSnapshot(changed, true)
            assertFalse(guard.canAcknowledge(changed))
            assertFalse(guard.canAcknowledge(scope))
            assertFalse(guard.acknowledgeAfterReview(changed))
            assertEquals(marker, PendingWriteMarker.decode(store.value!!))
        }
        guard.forgetSnapshot()
        assertFalse(guard.canAcknowledge(scope))
        assertTrue(guard.blocked)
        assertFalse(guard.begin(marker.copy(scope = other)))
        guard.observeSnapshot(scope, true)
        assertTrue(guard.canAcknowledge(scope))
        assertTrue(guard.acknowledgeAfterReview(scope))
        assertNull(store.value)
    }

    @Test fun unreadableStoredMarkerRequiresSnapshotAndReview() {
        val store = Store("{invalid")
        val guard = PendingWriteGuard(store)
        val reviewed = scope.copy(target = "host~other", incarnation = "incarnation-2")
        assertTrue(guard.unreadableMarker)
        assertTrue(guard.blocked)
        assertFalse(guard.begin(marker))
        assertFalse(guard.acknowledgeAfterReview(reviewed))
        guard.observeSnapshot(reviewed, true)
        assertTrue(guard.acknowledgeAfterReview(reviewed))
        assertFalse(guard.blocked)
        assertNull(store.value)
    }

    @Test fun incompletePendingInputSnapshotCannotClearUncertainAnswer() {
        val answer = marker.copy(operation = "answer")
        val store = Store(answer.encode())
        val guard = PendingWriteGuard(store)
        guard.observeSnapshot(scope, false)
        assertFalse(guard.canAcknowledge(scope))
        assertFalse(guard.acknowledgeAfterReview(scope))
        assertEquals(answer, PendingWriteMarker.decode(store.value!!))
        guard.observeSnapshot(scope, true)
        assertTrue(guard.canAcknowledge(scope))
        guard.observeSnapshot(scope, false)
        assertFalse(guard.canAcknowledge(scope))
        assertTrue(guard.blocked)
    }

    @Test fun terminalResultClearsOnlyMatchingMarkerAndFreshReplacementNeedsReview() {
        val store = Store()
        val guard = PendingWriteGuard(store)
        assertTrue(guard.begin(marker))
        assertFalse(guard.finish(marker.copy(requestId = "write-2")))
        val fresh = marker.copy(requestId = "write-2", createdAt = 2_000)
        assertFalse(guard.replaceAfterReview(marker, fresh, scope))
        guard.observeSnapshot(scope, true)
        store.failWrite = true
        assertFalse(guard.replaceAfterReview(marker, fresh, scope))
        assertEquals(marker, PendingWriteMarker.decode(store.value!!))
        store.failWrite = false
        assertTrue(guard.replaceAfterReview(marker, fresh, scope))
        assertEquals(fresh, PendingWriteMarker.decode(store.value!!))
        store.failClear = true
        assertFalse(guard.finish(fresh))
        assertTrue(guard.blocked)
        store.failClear = false
        assertTrue(guard.finish(fresh))
        assertFalse(guard.blocked)
    }
}
