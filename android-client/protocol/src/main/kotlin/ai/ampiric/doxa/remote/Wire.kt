package ai.ampiric.doxa.remote

import org.json.JSONObject
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.net.URI
import java.nio.ByteBuffer
import java.nio.charset.CodingErrorAction
import java.security.SecureRandom
import java.util.Base64
import java.util.zip.Deflater
import java.util.zip.DeflaterOutputStream
import java.util.zip.Inflater
import java.util.zip.InflaterInputStream
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/** The independently installed client uses the same v1 envelope as doxa-remote-wire. */
object Wire {
    private const val MAX_PLAIN = 128_000
    private const val MAX_CIPHER = 128_000
    private const val BUCKET = 4_096
    private val id = Regex("[A-Za-z0-9][A-Za-z0-9-]{0,127}")
    private val b64 = Regex("[A-Za-z0-9+/]*")
    private val random = SecureRandom()

    fun origin(raw: String): String {
        val url = try { URI(raw.trim()) } catch (_: Exception) { error("Invalid hub URL") }
        require(url.scheme == "https" && url.host?.lowercase()?.endsWith(".ts.net") == true &&
            url.host!!.length > ".ts.net".length && url.rawUserInfo == null &&
            (url.rawPath.isNullOrEmpty() || url.rawPath == "/") && url.rawQuery == null &&
            url.rawFragment == null && (url.port == -1 || url.port in 1..65535)) {
            "Use a private https://*.ts.net hub origin"
        }
        return "https://${url.host!!.lowercase()}${if (url.port == -1) "" else ":${url.port}"}/"
    }

    fun target(value: String): Boolean {
        val parts = value.split('~')
        return parts.size == 2 && parts.all(id::matches)
    }

    fun id(value: String): Boolean = id.matches(value)

    private fun decode(value: String, max: Int): ByteArray {
        require(value.length <= max * 2 && b64.matches(value)) { "Invalid remote base64" }
        val bytes = try { Base64.getDecoder().decode(value) } catch (_: Exception) {
            error("Invalid remote base64")
        }
        require(bytes.size <= max) { "Remote field exceeds bound" }
        return bytes
    }

    fun key(text: String): ByteArray {
        val bytes = decode(text.trim(), 32)
        require(bytes.size == 32 && bytes.any { it.toInt() != 0 }) {
            "Expected a nonzero 32-byte DOXA key"
        }
        return bytes
    }

    private fun encode(bytes: ByteArray): String = Base64.getEncoder().withoutPadding().encodeToString(bytes)

    private fun bounded(input: ByteArray, compressed: Boolean): ByteArray {
        if (!compressed) return input
        InflaterInputStream(ByteArrayInputStream(input), Inflater(true)).use { stream ->
            val output = ByteArrayOutputStream()
            val chunk = ByteArray(4096)
            while (true) {
                val count = stream.read(chunk)
                if (count < 0) break
                require(output.size() + count <= MAX_PLAIN) { "Remote plaintext exceeds bound" }
                output.write(chunk, 0, count)
            }
            return output.toByteArray()
        }
    }

    fun seal(key: ByteArray, context: String, value: JSONObject): JSONObject {
        require(key.size == 32)
        val plain = value.toString().toByteArray(Charsets.UTF_8)
        require(plain.size <= MAX_PLAIN) { "Remote plaintext exceeds bound" }
        var data = plain
        var compressed = false
        if (plain.size >= 1024) {
            val zipped = ByteArrayOutputStream()
            DeflaterOutputStream(zipped, Deflater(Deflater.BEST_SPEED, true)).use { it.write(plain) }
            if (zipped.size() + 32 < plain.size) {
                data = zipped.toByteArray()
                compressed = true
            }
        }
        val size = ((data.size + 5 + BUCKET - 1) / BUCKET) * BUCKET
        require(size <= MAX_CIPHER) { "Remote envelope exceeds bound" }
        val padded = ByteArray(size)
        padded[0] = if (compressed) 1 else 0
        ByteBuffer.wrap(padded, 1, 4).putInt(data.size)
        data.copyInto(padded, 5)
        val nonce = ByteArray(12).also(random::nextBytes)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
        cipher.updateAAD("doxa-remote-v1|$context".toByteArray(Charsets.UTF_8))
        return JSONObject().put("v", 1).put("alg", "A256GCM")
            .put("nonce", encode(nonce)).put("data", encode(cipher.doFinal(padded)))
    }

    fun open(key: ByteArray, context: String, envelope: JSONObject): JSONObject {
        require(key.size == 32 && envelope.optInt("v") == 1 && envelope.optString("alg") == "A256GCM") {
            "Unknown remote envelope"
        }
        val nonce = decode(envelope.getString("nonce"), 12)
        val data = decode(envelope.getString("data"), MAX_CIPHER + 16)
        require(nonce.size == 12 && data.size in 16..(MAX_CIPHER + 16)) { "Invalid remote envelope" }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
        cipher.updateAAD("doxa-remote-v1|$context".toByteArray(Charsets.UTF_8))
        val padded = try { cipher.doFinal(data) } catch (_: Exception) {
            error("Remote authentication failed")
        }
        require(padded.size in 5..MAX_CIPHER && padded.size % BUCKET == 0) { "Invalid remote padding" }
        val flag = padded[0].toInt()
        require(flag == 0 || flag == 1) { "Invalid compression flag" }
        val length = ByteBuffer.wrap(padded, 1, 4).int
        require(length in 0..(padded.size - 5) && padded.drop(length + 5).all { it.toInt() == 0 }) {
            "Invalid remote payload length"
        }
        val plain = bounded(padded.copyOfRange(5, 5 + length), flag == 1)
        require(plain.size <= MAX_PLAIN) { "Remote plaintext exceeds bound" }
        val text = Charsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(plain)).toString()
        return JSONObject(text)
    }
}
