package ai.ampiric.doxa.remote

/** Shared validation for the FCM registration and its opaque local routing tag. */
object AndroidPushWire {
    fun token(value: String): Boolean = value.length in 20..4096 && value.all {
        it.isLetterOrDigit() && it.code < 128 || it in ":-_."
    }
    fun validTag(value: String): Boolean = value.length == 32 && value.all { it in '0'..'9' || it in 'a'..'f' }
    fun accepted(kind: String, tag: String, expectedTag: String, optedIn: Boolean): Boolean =
        optedIn && tag == expectedTag && tag.isNotEmpty() && validTag(tag) &&
            kind in setOf("needs_input", "turn_done")
}
