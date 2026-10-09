package ai.ampiric.doxa.remote

import org.json.JSONObject

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
        Wire.id(requestId) && createdAt > 0

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
    fun write(value: String): Boolean
    fun clear(): Boolean
}

/** A prior write blocks every new write until a fresh snapshot and explicit review. */
class PendingWriteGuard(private val store: PendingWriteMarkerStore) {
    private var unreadable = false
    var marker: PendingWriteMarker? = null
        private set
    private var observedScope: PendingWriteScope? = null

    init {
        val raw = try { store.read() } catch (_: Exception) { unreadable = true; null }
        if (raw != null) {
            marker = try { PendingWriteMarker.decode(raw) }
            catch (_: Exception) { unreadable = true; null }
        }
    }

    val blocked: Boolean get() = unreadable || marker != null
    val unreadableMarker: Boolean get() = unreadable

    fun begin(next: PendingWriteMarker): Boolean {
        if (blocked || !next.valid() || !runCatching { store.write(next.encode()) }.getOrDefault(false)) return false
        marker = next
        observedScope = null
        return true
    }

    fun matches(current: PendingWriteMarker): Boolean = marker == current && !unreadable

    fun finish(current: PendingWriteMarker): Boolean {
        if (!matches(current) || !runCatching { store.clear() }.getOrDefault(false)) return false
        marker = null
        observedScope = null
        return true
    }

    fun forgetSnapshot() { observedScope = null }

    fun observeSnapshot(scope: PendingWriteScope, pendingInputsComplete: Boolean) {
        observedScope = if (blocked && pendingInputsComplete && scope.valid() &&
            (unreadable || marker?.scope == scope)) scope else null
    }

    fun canAcknowledge(scope: PendingWriteScope): Boolean = blocked && observedScope == scope &&
        (unreadable || marker?.scope == scope)

    fun acknowledgeAfterReview(scope: PendingWriteScope): Boolean {
        if (!canAcknowledge(scope) || !runCatching { store.clear() }.getOrDefault(false)) return false
        marker = null
        unreadable = false
        observedScope = null
        return true
    }

    fun replaceAfterReview(prior: PendingWriteMarker, next: PendingWriteMarker,
                           scope: PendingWriteScope): Boolean {
        if (!matches(prior) || !canAcknowledge(scope) || !next.valid() ||
            !runCatching { store.write(next.encode()) }.getOrDefault(false)) return false
        marker = next
        observedScope = null
        return true
    }
}
