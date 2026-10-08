package ai.ampiric.doxa.remote

import org.junit.Assert.*
import org.junit.Test

class AlertKindTest {
    @Test fun onlyGenericActionableEventsCanCreateAlerts() {
        assertEquals(AlertKind.NeedsInput, AlertKind.fromEvent("needs_input"))
        assertEquals(AlertKind.TurnDone, AlertKind.fromEvent("turn_done"))
        assertEquals(AlertKind.TurnDone, AlertKind.fromEvent("turn_refused"))
        for (event in listOf("text_delta", "tool_call", "prompt_queued", "replay_gap")) {
            assertNull(AlertKind.fromEvent(event))
        }
        assertFalse(AlertKind.NeedsInput.title.contains("session"))
        assertFalse(AlertKind.TurnDone.title.contains("session"))
    }
}
