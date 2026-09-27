//! Bounded transcript rows and reasoning markers from daemon events.

use super::{Session, MAX_TRANSCRIPT_BYTES, transcript_tools};
use crate::markdown;

const MAX_EVENT_FIELD_CHARS: usize = 320;

// Structured event fields are untrusted Markdown as well as terminal text.
// Keep each row small even when a daemon sends a very large JSON value.
fn event_field(value: &str) -> String {
    let clean = markdown::sanitize(value).replace(['\n', '\r'], " ");
    let mut chars = clean.chars();
    let mut clipped: String = chars.by_ref().take(MAX_EVENT_FIELD_CHARS).collect();
    if chars.next().is_some() {
        clipped.push('…');
    }
    let mut escaped = String::with_capacity(clipped.len());
    for ch in clipped.chars() {
        if matches!(
            ch,
            '\\' | '`'
                | '*'
                | '_'
                | '{'
                | '}'
                | '['
                | ']'
                | '('
                | ')'
                | '#'
                | '+'
                | '-'
                | '.'
                | '!'
                | '>'
                | '|'
                | '~'
        ) {
            escaped.push('\\');
        }
        escaped.push(ch);
    }
    escaped
}

fn event_string(data: &serde_json::Value, key: &str) -> Option<String> {
    data.get(key)
        .and_then(|value| value.as_str())
        .map(event_field)
}

pub(super) fn transcript_tail(text: &str) -> &str {
    if text.len() <= MAX_TRANSCRIPT_BYTES { return text; }
    let mut start = text.len() - MAX_TRANSCRIPT_BYTES;
    while !text.is_char_boundary(start) { start += 1; }
    &text[start..]
}

pub(super) fn append_transcript(session: &mut Session, text: &str) -> bool {
    if text.len() >= MAX_TRANSCRIPT_BYTES {
        session.transcript.clear();
        session.transcript.push_str(transcript_tail(text));
        return true;
    }
    let keep_existing = MAX_TRANSCRIPT_BYTES - text.len();
    let clipped = session.transcript.len() > keep_existing;
    if clipped {
        let mut start = session.transcript.len() - keep_existing;
        while !session.transcript.is_char_boundary(start) { start += 1; }
        session.transcript.drain(..start);
    }
    session.transcript.push_str(text);
    clipped
}

pub(super) fn append_turn_heading(session: &mut Session, heading: &str) -> bool {
    let separator = if session.transcript.is_empty() || session.transcript.ends_with("\n\n") {
        ""
    } else if session.transcript.ends_with('\n') {
        "\n"
    } else {
        "\n\n"
    };
    append_transcript(session, &format!("{separator}**{heading}:**\n\n"))
}

#[derive(Default)]
pub(super) struct ReasoningStream {
    pub(super) text: String,
    pub(super) tokens: u64,
    pub(super) exact: bool,
    pub(super) visible: bool,
    pub(super) streaming: bool,
}
impl std::fmt::Debug for ReasoningStream {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ReasoningStream")
            .field("text_chars", &self.text.chars().count())
            .field("tokens", &self.tokens)
            .field("visible", &self.visible)
            .field("streaming", &self.streaming)
            .finish()
    }
}

pub(super) fn set_reasoning_marker(session: &mut Session, stream: &ReasoningStream) {
    let marker = format!("{}{}", transcript_tools::REASONING_PREFIX,
        serde_json::json!({"text":stream.text,"tokens":stream.tokens,"streaming":stream.streaming,"exact":stream.exact}));
    if stream.visible {
        if let Some(start) = session.transcript.rfind(transcript_tools::REASONING_PREFIX) {
            let end = session.transcript[start..].find("\n\n")
                .map(|offset| start + offset).unwrap_or(session.transcript.len());
            session.transcript.replace_range(start..end, &marker);
            if session.transcript.len() > MAX_TRANSCRIPT_BYTES {
                session.transcript = transcript_tail(&session.transcript).to_owned();
            }
            return;
        }
    }
    append_transcript(session, &format!("\n\n{marker}\n\n"));
}

pub(super) fn structured_event(event_type: &str, data: &serde_json::Value) -> Option<String> {
    let field = |key| event_string(data, key).unwrap_or_default();
    let row = match event_type {
        "tool_call" => {
            let name = field("name");
            let input = data
                .get("input")
                .filter(|value| !value.is_null())
                .map(|value| event_field(&value.to_string()))
                .unwrap_or_default();
            if input.is_empty() {
                format!("Tool: {name} started")
            } else {
                format!("Tool: {name} started · {input}")
            }
        }
        "tool_result" => {
            let name = field("name");
            let result = field("result_summary");
            let outcome = if data.get("is_error").and_then(|v| v.as_bool()) == Some(true) {
                "failed"
            } else {
                "finished"
            };
            let duration = data
                .get("duration_ms")
                .and_then(|v| v.as_u64())
                .map(|ms| format!(" · {ms} ms"))
                .unwrap_or_default();
            if result.is_empty() {
                format!("Tool: {name} {outcome}{duration}")
            } else {
                format!("Tool: {name} {outcome}{duration} · {result}")
            }
        }
        "peer_joined" => format!("Peer joined: {}", field("title")),
        "peer_left" => format!("Peer left: {}", field("session_id")),
        "peer_message" => format!("Peer {}: {}", field("from_title"), field("body")),
        "peer_sent" => "Peer message sent".into(),
        "tool_disabled" => format!("Tool disabled: {} · {}", field("name"), field("reason")),
        "needs_input" => format!("Needs input: {} · {}", field("kind"), field("tool_name")),
        "needs_input_resolved" => "Input request resolved".into(),
        "turn_refused" => format!("Turn refused: {}", field("message")),
        "session_done" => "Session ended".into(),
        "prompt_queued" => "Prompt queued".into(),
        "prompt_dequeued" => "Queued prompt started".into(),
        "prompt_cancelled" => "Queued prompt cancelled".into(),
        "prompt_discarded" => "Queued prompt discarded".into(),
        "replay_gap" => "Some session events were missed during reconnect".into(),
        "remote_driver_changed" => format!(
            "Remote driver: {}",
            data.get("identity")
                .and_then(|v| v.as_str())
                .map(event_field)
                .unwrap_or_else(|| "none".into())
        ),
        "turn_done" if data.get("is_error").and_then(|v| v.as_bool()) == Some(true) => {
            format!("Turn failed: {}", field("error"))
        }
        _ => return None,
    };
    let identity = if matches!(event_type, "tool_call" | "tool_result") {
        data.get("id").and_then(|value| value.as_str())
            .filter(|id| !id.is_empty() && id.len() <= 200 && !id.chars().any(char::is_control))
            .map(|id| format!("{}{}", transcript_tools::TOOL_ID_PREFIX,
                serde_json::to_string(id).unwrap_or_default()))
            .unwrap_or_default()
    } else { String::new() };
    Some(format!("\n\n{row}{identity}\n\n"))
}

