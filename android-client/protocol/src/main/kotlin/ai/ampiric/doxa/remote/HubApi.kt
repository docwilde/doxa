package ai.ampiric.doxa.remote

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.DisposableHandle
import kotlinx.coroutines.InternalCoroutinesApi
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL
import java.nio.charset.CodingErrorAction
import java.nio.ByteBuffer
import java.util.UUID
import java.util.concurrent.ScheduledThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.coroutines.coroutineContext

data class Session(val id: String, val title: String, val engine: String, val encrypted: Boolean, val incarnation: String = "")
class RemoteRefusal(message: String) : Exception(message)

internal fun decryptEvent(target: String, incarnation: String, key: ByteArray?, frame: JSONObject) {
    require(frame.optString("type") == "event" && frame.optString("incarnation") == incarnation) {
        "Remote event belongs to a different session incarnation"
    }
    val event = frame.getJSONObject("event")
    val kind = event.getString("type")
    val seq = frame.getLong("seq")
    require(seq >= 0 && key != null && kind != "replay_gap") { "Invalid encrypted event" }
    val sealed = event.getJSONObject("data").getJSONObject("sealed")
    event.put("data", Wire.open(key, Wire.eventContext(target, incarnation, seq, kind), sealed))
}

/** Keeps the exact serialized request while its result is uncertain. Never regenerates its ID on retry. */
data class PendingCommand(
    val target: String,
    val operation: String,
    val requestId: String,
    val createdAt: Long,
    val body: JSONObject,
    val payload: JSONObject,
    val encrypted: Boolean
)

class HubApi internal constructor(rawOrigin: String, private val key: ByteArray?,
                                  private val operationTimeoutMillis: Long,
                                  private val openConnection: (URL) -> HttpURLConnection) {
    constructor(rawOrigin: String, key: ByteArray?) : this(rawOrigin, key, OPERATION_TIMEOUT_MS,
        { url -> url.openConnection() as HttpURLConnection })

    init { require(operationTimeoutMillis in 1..OPERATION_TIMEOUT_MS) { "Invalid Hub operation deadline" } }

    private val origin = Wire.origin(rawOrigin)
    @Volatile private var hubBoot: String? = null
    private fun route(path: String): URL = java.net.URI(origin + path).toURL()

    private fun connection(path: String, method: String, stream: Boolean = false): HttpURLConnection {
        val conn = openConnection(route(path))
        conn.instanceFollowRedirects = false
        conn.requestMethod = method
        conn.connectTimeout = 10_000
        conn.readTimeout = if (stream) 20_000 else 10_000
        conn.setRequestProperty("Accept", if (stream) "text/event-stream" else "application/json")
        conn.setRequestProperty("Cache-Control", "no-store")
        conn.useCaches = false
        return conn
    }

    @OptIn(InternalCoroutinesApi::class)
    private suspend fun cancellationDisconnect(conn: HttpURLConnection): DisposableHandle? =
        coroutineContext[Job]?.invokeOnCompletion(onCancelling = true, invokeImmediately = true) { cause ->
            if (cause is CancellationException) conn.disconnect()
        }

    private suspend fun <T> finiteRequest(path: String, method: String,
                                          action: (HttpURLConnection) -> T): T = withContext(Dispatchers.IO) {
        val conn = connection(path, method)
        val deadlineNanos = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(operationTimeoutMillis)
        val timedOut = AtomicBoolean(false)
        val cancelHook = cancellationDisconnect(conn)
        val deadline = deadlineExecutor.schedule({
            timedOut.set(true)
            conn.disconnect()
        }, operationTimeoutMillis, TimeUnit.MILLISECONDS)
        try {
            val value = action(conn)
            coroutineContext.ensureActive()
            if (timedOut.get() || System.nanoTime() >= deadlineNanos)
                throw SocketTimeoutException("Hub operation deadline exceeded")
            value
        } catch (error: Exception) {
            if (timedOut.get() && error !is SocketTimeoutException) {
                throw SocketTimeoutException("Hub operation deadline exceeded").also { it.initCause(error) }
            }
            throw error
        } finally {
            deadline.cancel(false)
            cancelHook?.dispose()
            conn.disconnect()
        }
    }

    private fun readBounded(stream: InputStream, max: Int): ByteArray {
        val out = ByteArrayOutputStream()
        val buffer = ByteArray(4096)
        while (true) {
            val count = stream.read(buffer)
            if (count < 0) break
            require(out.size() + count <= max) { "Hub response exceeds bound" }
            out.write(buffer, 0, count)
        }
        return out.toByteArray()
    }

    private fun decode(bytes: ByteArray): String = Charsets.UTF_8.newDecoder()
        .onMalformedInput(CodingErrorAction.REPORT).onUnmappableCharacter(CodingErrorAction.REPORT)
        .decode(ByteBuffer.wrap(bytes)).toString()

    private fun json(conn: HttpURLConnection): JSONObject {
        val code = conn.responseCode
        require(code in 200..299) { "Hub refused request ($code)" }
        conn.inputStream.use { return JSONObject(decode(readBounded(it, 128_000))) }
    }

    private suspend fun get(path: String): JSONObject = finiteRequest(path, "GET", ::json)

    private suspend fun post(path: String, body: JSONObject): JSONObject {
        val bytes = body.toString().toByteArray(Charsets.UTF_8)
        require(bytes.size <= 128_000) { "Remote request exceeds bound" }
        return finiteRequest(path, "POST") { conn ->
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.setFixedLengthStreamingMode(bytes.size)
            conn.outputStream.use { it.write(bytes) }
            json(conn)
        }
    }

    private suspend fun delete(path: String, body: JSONObject): JSONObject {
        val bytes = body.toString().toByteArray(Charsets.UTF_8)
        require(bytes.size <= 8_192) { "Remote request exceeds bound" }
        return finiteRequest(path, "DELETE") { conn ->
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.setFixedLengthStreamingMode(bytes.size)
            conn.outputStream.use { it.write(bytes) }
            json(conn)
        }
    }

    suspend fun registerAndroidPush(session: Session, token: String, tag: String): JSONObject = withContext(Dispatchers.IO) {
        require(Wire.target(session.id) && session.incarnation.isNotBlank() && session.incarnation.length <= 64)
        require(AndroidPushWire.token(token) && AndroidPushWire.validTag(tag))
        post("api/push/android", JSONObject().put("target", session.id)
            .put("incarnation", session.incarnation).put("token", token).put("tag", tag))
    }

    suspend fun unregisterAndroidPush(token: String): JSONObject = withContext(Dispatchers.IO) {
        require(AndroidPushWire.token(token))
        delete("api/push/android", JSONObject().put("token", token))
    }

    internal fun parseInventory(inventory: JSONObject): List<Session> {
        val boot = inventory.getString("hub_boot")
        require(AndroidWriteId.validBoot(boot)) { "Invalid hub boot nonce" }
        val items = inventory.getJSONArray("sessions")
        require(items.length() <= 64) { "Invalid hub session inventory" }
        val sessions = List(items.length()) { index ->
            val row = items.getJSONObject(index)
            val id = row.getString("id")
            require(Wire.target(id)) { "Invalid hub session target" }
            Session(id, row.optString("title", id).take(160),
                row.optString("engine", "session").take(32), row.optBoolean("encrypted"),
                row.optString("incarnation").take(64))
        }
        hubBoot = boot
        return sessions
    }

    suspend fun sessions(): List<Session> = withContext(Dispatchers.IO) {
        parseInventory(get("api/sessions"))
    }

    /** Returns the current boot only when the exact session incarnation is still listed. */
    suspend fun recoveryInventoryBoot(session: Session): String? = withContext(Dispatchers.IO) {
        val inventory = get("api/sessions")
        val sessions = parseInventory(inventory)
        val exactSessionListed = sessions.any {
            it.id == session.id && it.incarnation == session.incarnation && it.encrypted == session.encrypted
        }
        if (exactSessionListed) inventory.getString("hub_boot") else null
    }

    fun prepare(target: String, operation: String, payload: JSONObject, encrypted: Boolean,
                incarnation: String? = null): PendingCommand {
        require(Wire.target(target) && operation in setOf("prompt", "answer", "transcript"))
        require(!encrypted || key != null) { "Choose the shared key to open this session" }
        val write = operation != "transcript"
        val boot = if (write) hubBoot ?: error("Refresh the hub inventory before writing") else null
        if (write) require(PendingWriteScope(origin, target, incarnation ?: "").valid()) {
            "Invalid session incarnation"
        }
        if (operation == "answer") require(payload.optJSONObject("reviewed_request")?.optString("id") ==
            payload.optString("id") && Wire.id(payload.optString("id"))) {
            "Answer is missing its reviewed pending input"
        }
        val requestId = if (boot != null) AndroidWriteId.new(boot) else UUID.randomUUID().toString()
        val plain = JSONObject(payload.toString()).put("request_id", requestId)
            .put("issued_at", System.currentTimeMillis() / 1000)
        if (write) plain.put("hub_boot", boot).put("incarnation", incarnation)
        val body = if (encrypted) JSONObject().put("request_id", requestId)
            .put("sealed", Wire.seal(key!!, "$target|command|$operation", plain)) else plain
        if (write && encrypted) body.put("hub_boot", boot).put("incarnation", incarnation)
        return PendingCommand(target, operation, requestId, System.currentTimeMillis(), body,
            JSONObject(payload.toString()), encrypted)
    }

    suspend fun fence(marker: PendingWriteMarker): WriteFenceResult = withContext(Dispatchers.IO) {
        require(marker.valid() && marker.scope.origin == origin) { "Invalid write recovery scope" }
        val result = post("api/android/requests/${marker.requestId}/fence", JSONObject()
            .put("target", marker.scope.target).put("operation", marker.operation)
            .put("incarnation", marker.scope.incarnation))
        WriteFenceResult.decode(result)
    }

    suspend fun submit(command: PendingCommand, encrypted: Boolean): JSONObject =
        withTimeout(COMMAND_TIMEOUT_MS) { withContext(Dispatchers.IO) {
        require(System.currentTimeMillis() - command.createdAt < 120_000) {
            "Request expired; inspect the session before sending a new request"
        }
        val path = if (command.operation == "transcript")
            "api/sessions/${command.target}/transcript"
        else "api/android/sessions/${command.target}/${command.operation}"
        val queued = post(path, command.body)
        val id = queued.optString("command_id")
        require(Wire.id(id)) { "Hub returned no command ID" }
        repeat(240) {
            delay(250)
            val status = get("api/commands/$id")
            when (status.optString("status")) {
                "accepted" -> {
                    val result = status.getJSONObject("result")
                    val value = if (encrypted) {
                        require(key != null) { "Shared key missing" }
                        Wire.open(key, "${command.target}|result|${command.operation}|${command.requestId}",
                            result.getJSONObject("sealed"))
                    } else result
                    if (!value.optBoolean("ok", true))
                        throw RemoteRefusal(value.optString("error", "Host refused command"))
                    return@withContext value
                }
                "refused" -> throw RemoteRefusal(
                    status.optJSONObject("result")?.optString("error") ?: "Host refused command")
                "expired" -> error("Request expired; inspect the session before sending a new request")
            }
        }
        error("Acknowledgement timed out; inspect the session before retrying")
        } }

    suspend fun transcript(target: String, encrypted: Boolean, before: Long? = null): JSONObject {
        val payload = JSONObject()
        if (before != null) payload.put("before", before)
        return submit(prepare(target, "transcript", payload, encrypted), encrypted)
    }

    private fun boundedLine(input: InputStream): String? {
        val bytes = ByteArrayOutputStream()
        while (true) {
            val byte = input.read()
            if (byte < 0) return if (bytes.size() == 0) null else decode(bytes.toByteArray())
            if (byte == 10) return decode(bytes.toByteArray()).removeSuffix("\r")
            require(bytes.size() < 128_000) { "Remote event exceeds bound" }
            bytes.write(byte)
        }
    }

    suspend fun events(target: String, encrypted: Boolean, incarnation: String, startCursor: Long,
                       onFrame: suspend (JSONObject) -> Unit) = withContext(Dispatchers.IO) {
        require(Wire.target(target) && incarnation.isNotBlank() && incarnation.length <= 128 && startCursor >= 0)
        val conn = connection("api/sessions/$target/events?cursor=$startCursor", "GET", true)
        val cancelHook = cancellationDisconnect(conn)
        try {
            require(conn.responseCode == 200) { "Remote event stream unavailable (${conn.responseCode})" }
            conn.inputStream.use { input ->
                var seenHello = false
                var frameBytes = 0
                var data: String? = null
                while (true) {
                    coroutineContext.ensureActive()
                    val line = boundedLine(input) ?: break
                    frameBytes += line.length
                    require(frameBytes <= 128_000) { "Remote event exceeds bound" }
                    if (line.isEmpty()) {
                        data?.let { raw ->
                            val frame = JSONObject(raw)
                            if (frame.optString("type") == "hello") {
                                require(frame.optString("incarnation") == incarnation) {
                                    "Remote session incarnation changed"
                                }
                                seenHello = true
                            } else if (frame.optString("type") == "event") {
                                val event = frame.getJSONObject("event")
                                val kind = event.getString("type")
                                if (encrypted && kind != "replay_gap") {
                                    require(seenHello) { "Encrypted event arrived before the session snapshot" }
                                    decryptEvent(target, incarnation, key, frame)
                                } else if (!encrypted && event.optJSONObject("data")?.has("sealed") == true) {
                                    error("Unexpected encrypted event")
                                }
                            }
                            onFrame(frame)
                        }
                        data = null
                        frameBytes = 0
                    } else if (line.startsWith("data: ")) data = line.substring(6)
                }
            }
        } finally { cancelHook?.dispose(); conn.disconnect() }
    }

    fun close() { key?.fill(0) }

    companion object {
        private const val OPERATION_TIMEOUT_MS = 30_000L
        private const val COMMAND_TIMEOUT_MS = 120_000L
        private val deadlineExecutor = ScheduledThreadPoolExecutor(1) { task ->
            Thread(task, "doxa-hub-deadline").apply { isDaemon = true }
        }.apply { removeOnCancelPolicy = true }
    }
}
