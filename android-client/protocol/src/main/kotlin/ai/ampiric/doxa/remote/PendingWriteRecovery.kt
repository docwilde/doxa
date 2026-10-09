package ai.ampiric.doxa.remote

import org.json.JSONObject
import java.util.UUID

/** Boot-scoped IDs let the hub reject a delayed POST after its volatile state restarts. */
object AndroidWriteId {
    private val boot = Regex("[0-9a-f]{32}")
    private val id = Regex("[0-9a-f]{32}-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    fun validBoot(value: String): Boolean = boot.matches(value)
    fun valid(value: String): Boolean = id.matches(value)
    fun new(boot: String): String {
        require(validBoot(boot)) { "Invalid hub boot nonce" }
        return "$boot-${UUID.randomUUID()}"
    }
}

/** A transcript must identify the session actually read, including its incarnation. */
object AndroidReview {
    fun matchesIncarnation(history: JSONObject, incarnation: String): Boolean =
        incarnation.isNotBlank() && history.opt("incarnation") == incarnation

    /** A safe fence is the delivery proof; inventory boot only has to stay stable across the read. */
    fun safeSnapshot(fence: WriteFenceResult, beforeBoot: String?, afterBoot: String?,
                     history: JSONObject, incarnation: String): Boolean =
        fence.safeToClear && beforeBoot != null && AndroidWriteId.validBoot(beforeBoot) &&
            beforeBoot == afterBoot && matchesIncarnation(history, incarnation)

    fun matchesAnswer(history: JSONObject, payload: JSONObject, incarnation: String): Boolean {
        if (!matchesIncarnation(history, incarnation) || history.opt("pending_inputs_complete") != true)
            return false
        val id = payload.optString("id")
        val reviewed = payload.optJSONObject("reviewed_request") ?: return false
        val inputs = history.optJSONArray("pending_inputs") ?: return false
        if (!Wire.id(id) || reviewed.optString("id") != id || inputs.length() > 64) return false
        return (0 until inputs.length()).mapNotNull { inputs.optJSONObject(it) }
            .any { it.optString("id") == id && it.toString() == reviewed.toString() }
    }
}

/** Only the hub's documented terminal/undelivered fence states permit review. */
class WriteFenceResult private constructor(val status: String, val safeToClear: Boolean) {
    companion object {
        private val SAFE = setOf("absent_fenced", "queued_cancelled", "expired_undelivered", "terminal")
        private val UNSAFE = setOf("delivered_unsettled", "unknown_old_boot")
        fun decode(value: JSONObject): WriteFenceResult {
            val status = value.getString("status")
            val safe = value.getBoolean("safe_to_clear")
            require(status in SAFE || status in UNSAFE) { "Unknown write fence status" }
            require(safe == (status in SAFE)) { "Contradictory write fence result" }
            if (status == "terminal") require(value.optString("command_status") in setOf("accepted", "refused")) {
                "Missing terminal command status"
            }
            return WriteFenceResult(status, safe)
        }
    }
}

/** Body-free scope for a write whose outcome may be unknown after process death. */
data class PendingWriteScope(val origin: String, val target: String, val incarnation: String) {
    fun valid(): Boolean = origin.length <= 512 && runCatching { Wire.origin(origin) == origin }.getOrDefault(false) &&
        Wire.target(target) && incarnation.isNotBlank() && incarnation.length <= 64 &&
        incarnation.none { Character.isISOControl(it) }
}

/** Never contains a prompt, approval answer, shared key, or serialized command body. */
data class PendingWriteMarker(
    val scope: PendingWriteScope,
    val operation: String,
    val requestId: String,
    val createdAt: Long
) {
    fun valid(): Boolean = scope.valid() && operation in setOf("prompt", "answer") &&
        AndroidWriteId.valid(requestId) && createdAt > 0

    fun encode(): String {
        require(valid()) { "Invalid pending write marker" }
        val text = JSONObject().put("v", 1).put("origin", scope.origin)
            .put("target", scope.target).put("incarnation", scope.incarnation)
            .put("operation", operation).put("request_id", requestId)
            .put("created_at_ms", createdAt).toString()
        require(text.toByteArray(Charsets.UTF_8).size <= MAX_BYTES) { "Pending write marker exceeds bound" }
        return text
    }

    companion object {
        private const val MAX_BYTES = 1_024
        private val FIELDS = setOf("v", "origin", "target", "incarnation", "operation", "request_id", "created_at_ms")

        fun decode(raw: String): PendingWriteMarker {
            require(raw.toByteArray(Charsets.UTF_8).size <= MAX_BYTES) { "Pending write marker exceeds bound" }
            val value = JSONObject(raw)
            require(value.keys().asSequence().toSet() == FIELDS && value.opt("v") == 1 &&
                listOf("origin", "target", "incarnation", "operation", "request_id")
                    .all { value.opt(it) is String } && value.opt("created_at_ms") is Number &&
                value.getLong("created_at_ms").toDouble() == (value.opt("created_at_ms") as Number).toDouble()) {
                "Invalid pending write marker schema"
            }
            return PendingWriteMarker(PendingWriteScope(value.getString("origin"), value.getString("target"),
                value.getString("incarnation")), value.getString("operation"),
                value.getString("request_id"), value.getLong("created_at_ms"))
                .also { require(it.valid()) { "Invalid pending write marker" } }
        }
    }
}

/** The Android adapter must make writes and deletes durable before returning true. */
interface PendingWriteMarkerStore {
    fun read(): String?
    fun writeIfEmpty(value: String): Boolean
    fun replace(expected: String, value: String): Boolean
    fun clear(expected: String): Boolean
}

/** A prior write blocks every new write until a fresh snapshot and explicit review. */
class PendingWriteGuard(private val store: PendingWriteMarkerStore) {
    private var unreadable = false
    var marker: PendingWriteMarker? = null
        private set
    private var persistedRaw: String? = null
    private var observedScope: PendingWriteScope? = null
    private var fencedMarker: PendingWriteMarker? = null

    init {
        val raw = try { store.read() } catch (_: Exception) { unreadable = true; null }
        if (raw != null) {
            marker = try { PendingWriteMarker.decode(raw).also { persistedRaw = raw } }
            catch (_: Exception) { unreadable = true; null }
        }
    }

    val blocked: Boolean get() = unreadable || marker != null
    val unreadableMarker: Boolean get() = unreadable

    fun begin(next: PendingWriteMarker): Boolean {
        if (blocked || !next.valid()) return false
        val encoded = next.encode()
        if (!runCatching { store.writeIfEmpty(encoded) }.getOrDefault(false)) return false
        marker = next
        persistedRaw = encoded
        observedScope = null
        fencedMarker = null
        return true
    }

    fun matches(current: PendingWriteMarker): Boolean = marker == current && !unreadable

    fun finish(current: PendingWriteMarker): Boolean {
        val expected = persistedRaw ?: return false
        if (!matches(current) || !runCatching { store.clear(expected) }.getOrDefault(false)) return false
        marker = null
        persistedRaw = null
        observedScope = null
        fencedMarker = null
        return true
    }

    fun recordFence(current: PendingWriteMarker, result: WriteFenceResult): Boolean {
        if (!matches(current)) return false
        fencedMarker = if (result.safeToClear) current else null
        observedScope = null
        return result.safeToClear
    }

    fun forgetSnapshot() { observedScope = null }

    fun observeSnapshot(scope: PendingWriteScope, pendingInputsComplete: Boolean) {
        observedScope = if (!unreadable && blocked && pendingInputsComplete && scope.valid() &&
            marker?.scope == scope && fencedMarker == marker) scope else null
    }

    fun canAcknowledge(scope: PendingWriteScope): Boolean = !unreadable && blocked &&
        observedScope == scope && marker?.scope == scope && fencedMarker == marker

    fun acknowledgeAfterReview(scope: PendingWriteScope): Boolean {
        val expected = persistedRaw ?: return false
        if (!canAcknowledge(scope) || !runCatching { store.clear(expected) }.getOrDefault(false)) return false
        marker = null
        persistedRaw = null
        unreadable = false
        observedScope = null
        fencedMarker = null
        return true
    }

    fun replaceAfterReview(prior: PendingWriteMarker, next: PendingWriteMarker,
                           scope: PendingWriteScope): Boolean {
        val expected = persistedRaw ?: return false
        if (!matches(prior) || !canAcknowledge(scope) || !next.valid()) return false
        val encoded = next.encode()
        if (!runCatching { store.replace(expected, encoded) }.getOrDefault(false)) return false
        marker = next
        persistedRaw = encoded
        observedScope = null
        fencedMarker = null
        return true
    }
}
