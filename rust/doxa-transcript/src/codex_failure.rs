//! Closed diagnostic categories for a Codex continuation guard. These describe
//! why continuation was withheld; they never authorize clearing that guard.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CodexContinuationFailure {
    TranscriptAppend,
    ThreadCheckpoint,
    AssistantLimit,
    MissingThread,
    IncompleteTurn,
    CompactionReviewFailed,
    CompactionUnreviewed,
    AutomaticCompactionBlocked,
    PermissionUnacknowledged,
    TurnCancelled,
    TurnTimedOut,
    ProviderProtocol,
}

impl CodexContinuationFailure {
    pub fn key(self) -> &'static str {
        match self {
            Self::TranscriptAppend => "transcript_append_failed",
            Self::ThreadCheckpoint => "thread_checkpoint_failed",
            Self::AssistantLimit => "assistant_output_limit",
            Self::MissingThread => "thread_identity_missing",
            Self::IncompleteTurn => "terminal_boundary_missing",
            Self::CompactionReviewFailed => "compaction_review_failed",
            Self::CompactionUnreviewed => "compaction_review_unobserved",
            Self::AutomaticCompactionBlocked => "automatic_compaction_blocked_unsettled",
            Self::PermissionUnacknowledged => "permission_update_unacknowledged",
            Self::TurnCancelled => "cancelled_without_terminal_boundary",
            Self::TurnTimedOut => "timed_out_without_terminal_boundary",
            Self::ProviderProtocol => "provider_protocol_without_terminal_boundary",
        }
    }

    pub fn from_key(value: &str) -> Option<Self> {
        [Self::TranscriptAppend, Self::ThreadCheckpoint, Self::AssistantLimit,
            Self::MissingThread, Self::IncompleteTurn, Self::CompactionReviewFailed,
            Self::CompactionUnreviewed, Self::AutomaticCompactionBlocked,
            Self::PermissionUnacknowledged, Self::TurnCancelled, Self::TurnTimedOut,
            Self::ProviderProtocol].into_iter().find(|kind| kind.key() == value)
    }

    pub fn message(self) -> &'static str {
        match self {
            Self::TranscriptAppend => "Codex persistence failed: transcript append failed; session cannot safely continue",
            Self::ThreadCheckpoint => "Codex persistence failed: durable thread checkpoint failed; session cannot safely continue",
            Self::AssistantLimit => "Codex assistant output exceeded the display limit; terminal boundary unverified; session cannot safely continue",
            Self::MissingThread => "Codex thread identity is missing; session cannot safely continue",
            Self::IncompleteTurn => "Codex turn incomplete: terminal boundary unverified; session cannot safely continue",
            Self::CompactionReviewFailed => "Codex compaction review hook failed; protected session stopped; terminal boundary unverified",
            Self::CompactionUnreviewed => "Codex compaction completed without verified LORE review; protected session stopped",
            Self::AutomaticCompactionBlocked => "LORE blocked automatic Codex compaction; terminal boundary unverified; session cannot safely continue",
            Self::PermissionUnacknowledged => "Codex live permission update was not durably acknowledged; session cannot safely continue",
            Self::TurnCancelled => "Codex turn cancelled without a verified terminal boundary; session cannot safely continue",
            Self::TurnTimedOut => "Codex app-server turn timed out without a verified terminal boundary; session cannot safely continue",
            Self::ProviderProtocol => "Codex app-server protocol or process failed without a verified terminal boundary; session cannot safely continue",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn saved_categories_are_closed_static_diagnostics() {
        for kind in [CodexContinuationFailure::TranscriptAppend,
            CodexContinuationFailure::CompactionReviewFailed,
            CodexContinuationFailure::ProviderProtocol] {
            assert_eq!(CodexContinuationFailure::from_key(kind.key()), Some(kind));
        }
        for unknown in ["", "provider says retry", "compaction_review_failed\nsecret", "COMPACtion_review_failed"] {
            assert_eq!(CodexContinuationFailure::from_key(unknown), None);
        }
    }
}
