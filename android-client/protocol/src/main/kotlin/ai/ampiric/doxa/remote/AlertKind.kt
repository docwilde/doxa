package ai.ampiric.doxa.remote

/** Generic notification policy. Event payloads and session IDs never enter notifications. */
enum class AlertKind(val title: String) {
    NeedsInput("DOXA needs input"),
    TurnDone("DOXA turn finished");

    companion object {
        fun fromEvent(event: String): AlertKind? = when (event) {
            "needs_input" -> NeedsInput
            "turn_done", "turn_refused" -> TurnDone
            else -> null
        }
    }
}
