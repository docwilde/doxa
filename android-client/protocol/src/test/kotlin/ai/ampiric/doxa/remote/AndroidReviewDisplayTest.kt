package ai.ampiric.doxa.remote

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class AndroidReviewDisplayTest {
    @Test fun titledApprovalStillDisplaysCompleteArgumentsAndIdentity() {
        val request = JSONObject().put("id", "review-1").put("kind", "permission")
            .put("title", "Approve tool?").put("tool_name", "Bash")
            .put("input_summary", "x".repeat(20_000) + " FINAL_REVIEWED_ARGUMENT")
        val display = AndroidReview.displayRequest(request)
        val roundtrip = JSONObject(display)
        assertEquals(request.optString("input_summary"), roundtrip.optString("input_summary"))
        assertEquals("review-1", roundtrip.optString("id"))
        assertTrue(display.contains("FINAL_REVIEWED_ARGUMENT"))
        assertEquals(request.toString(), roundtrip.toString())
    }
}
