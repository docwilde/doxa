package ai.ampiric.doxa.remote

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.CodingErrorAction
import java.nio.ByteBuffer
import java.util.UUID
import kotlin.coroutines.coroutineContext

data class Session(val id: String, val title: String, val engine: String, val encrypted: Boolean)
class RemoteRefusal(message: String) : Exception(message)

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

class HubApi(rawOrigin: String, private val key: ByteArray?) {
    private val origin = Wire.origin(rawOrigin)
    private fun route(path: String): URL = java.net.URI(origin + path).toURL()

    private fun connection(path: String, method: String, stream: Boolean = false): HttpURLConnection {
        val conn = route(path).openConnection() as HttpURLConnection
        conn.instanceFollowRedirects = false
        conn.requestMethod = method
        conn.connectTimeout = 10_000
        conn.readTimeout = if (stream) 20_000 else 10_000
        conn.setRequestProperty("Accept", if (stream) "text/event-stream" else "application/json")
        conn.setRequestProperty("Cache-Control", "no-store")
        conn.useCaches = false
        return conn
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

    private fun get(path: String): JSONObject {
        val conn = connection(path, "GET")
        try { return json(conn) } finally { conn.disconnect() }
    }

    private fun post(path: String, body: JSONObject): JSONObject {
        val bytes = body.toString().toByteArray(Charsets.UTF_8)
        require(bytes.size <= 128_000) { "Remote request exceeds bound" }
        val conn = connection(path, "POST")
        try {
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.setFixedLengthStreamingMode(bytes.size)
            conn.outputStream.use { it.write(bytes) }
            return json(conn)
        } finally { conn.disconnect() }
    }

    suspend fun sessions(): List<Session> = withContext(Dispatchers.IO) {
        val items = get("api/sessions").getJSONArray("sessions")
        require(items.length() <= 64) { "Invalid hub session inventory" }
        List(items.length()) { index ->
            val row = items.getJSONObject(index)
            val id = row.getString("id")
            require(Wire.target(id)) { "Invalid hub session target" }
            Session(id, row.optString("title", id).take(160),
                row.optString("engine", "session").take(32), row.optBoolean("encrypted"))
        }
    }

    fun prepare(target: String, operation: String, payload: JSONObject, encrypted: Boolean): PendingCommand {
        require(Wire.target(target) && operation in setOf("prompt", "answer", "transcript"))
        require(!encrypted || key != null) { "Choose the shared key to open this session" }
        val requestId = UUID.randomUUID().toString()
        val plain = JSONObject(payload.toString()).put("request_id", requestId)
            .put("issued_at", System.currentTimeMillis() / 1000)
        val body = if (encrypted) JSONObject().put("request_id", requestId)
            .put("sealed", Wire.seal(key!!, "$target|command|$operation", plain)) else plain
        return PendingCommand(target, operation, requestId, System.currentTimeMillis(), body,
            JSONObject(payload.toString()), encrypted)
    }

    suspend fun submit(command: PendingCommand, encrypted: Boolean): JSONObject = withContext(Dispatchers.IO) {
        require(System.currentTimeMillis() - command.createdAt < 120_000) {
            "Request expired; inspect the session before sending a new request"
        }
        val queued = post("api/sessions/${command.target}/${command.operation}", command.body)
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
    }

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

    suspend fun events(target: String, encrypted: Boolean, startCursor: Long,
                       onFrame: suspend (JSONObject) -> Unit) = withContext(Dispatchers.IO) {
        require(Wire.target(target) && startCursor >= 0)
        val conn = connection("api/sessions/$target/events?cursor=$startCursor", "GET", true)
        try {
            require(conn.responseCode == 200) { "Remote event stream unavailable (${conn.responseCode})" }
            conn.inputStream.use { input ->
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
                            if (frame.optString("type") == "event") {
                                val event = frame.getJSONObject("event")
                                val kind = event.getString("type")
                                if (encrypted && kind != "replay_gap") {
                                    val seq = frame.getLong("seq")
                                    require(seq >= 0 && key != null) { "Invalid encrypted event" }
                                    val sealed = event.getJSONObject("data").getJSONObject("sealed")
                                    event.put("data", Wire.open(key, "$target|event|$seq|$kind", sealed))
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
        } finally { conn.disconnect() }
    }

    fun close() { key?.fill(0) }
}
