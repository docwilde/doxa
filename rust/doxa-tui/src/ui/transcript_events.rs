//! Bounded transcript rows and reasoning markers from daemon events.

use super::{transcript_tools, Session, MAX_TRANSCRIPT_BYTES};
use crate::markdown;

const MAX_EVENT_FIELD_CHARS: usize = 320;

// Structured event fields are untrusted Markdown as well as terminal text.
// Keep ordinary event rows small. A queued prompt uses the input budget so
// the submitted text remains visible while the current turn is running.
fn event_field(value: &str) -> String {
    event_field_with_limit(value, MAX_EVENT_FIELD_CHARS)
}

fn event_field_with_limit(value: &str, limit: usize) -> String {
    let clean = markdown::sanitize(value).replace(['\n', '\r'], " ");
    let mut chars = clean.chars();
    let mut clipped: String = chars.by_ref().take(limit).collect();
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

// Tool names are rendered as plain TUI text in fold headers. Markdown escapes
// belong in prose fields, where a backslash would otherwise become visible.
fn event_tool_name(data: &serde_json::Value) -> String {
    let clean = markdown::sanitize(data["name"].as_str().unwrap_or("Tool"))
        .replace(['\n', '\r'], " ");
    let mut chars = clean.chars();
    let mut name: String = chars.by_ref().take(MAX_EVENT_FIELD_CHARS).collect();
    if chars.next().is_some() { name.push('…'); }
    name
}

pub(super) fn transcript_tail(text: &str) -> &str {
    if text.len() <= MAX_TRANSCRIPT_BYTES {
        return text;
    }
    let mut start = text.len() - MAX_TRANSCRIPT_BYTES;
    while !text.is_char_boundary(start) {
        start += 1;
    }
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
        let minimum = session.transcript.len() - keep_existing;
        // Evict old turns in batches so a full display tail does not invalidate
        // its render cache for every token. The durable transcript is separate.
        let current_turn = super::streamed_turn_start(&session.transcript);
        let mut start = minimum;
        if let Some(current) = current_turn.filter(|current| *current >= minimum) {
            let mut target = (minimum + MAX_TRANSCRIPT_BYTES / 8).min(current);
            while !session.transcript.is_char_boundary(target) { target -= 1; }
            start = super::streamed_turn_start(&session.transcript[..target])
                .filter(|boundary| *boundary >= minimum)
                .unwrap_or(current);
        }
        while !session.transcript.is_char_boundary(start) {
            start += 1;
        }
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

pub(super) fn set_reasoning_marker(session: &mut Session, stream: &ReasoningStream) -> bool {
    let marker = format!(
        "{}{}",
        transcript_tools::REASONING_PREFIX,
        serde_json::json!({"text":stream.text,"tokens":stream.tokens,"streaming":stream.streaming,"exact":stream.exact})
    );
    if stream.visible {
        if let Some(start) = session.transcript.rfind(transcript_tools::REASONING_PREFIX) {
            let end = session.transcript[start..]
                .find("\n\n")
                .map(|offset| start + offset)
                .unwrap_or(session.transcript.len());
            session.transcript.replace_range(start..end, &marker);
            if session.transcript.len() > MAX_TRANSCRIPT_BYTES {
                session.transcript = transcript_tail(&session.transcript).to_owned();
                return true;
            }
            return false;
        }
    }
    append_transcript(session, &format!("\n\n{marker}\n\n"))
}

pub(super) fn structured_event(event_type: &str, data: &serde_json::Value) -> Option<String> {
    let field = |key| event_string(data, key).unwrap_or_default();
    let row = match event_type {
        "tool_call" => {
            let name = event_tool_name(data);
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
            let name = event_tool_name(data);
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
        "peer_message" => {
            let marker = if data["fleet_admission"]["unreviewed"] == true {
                " [unreviewed fleet message]"
            } else { "" };
            format!("Peer {}{marker}: {}", field("from_title"), field("body"))
        }
        "fleet_guard" => {
            let outcome = if data["delivered"] == true {
                if data["unreviewed"] == true { "unreviewed" } else { "admitted" }
            } else { "quarantined" };
            format!("Fleet message {outcome}: {}", field("reason"))
        }
        "peer_sent" => "Peer message sent".into(),
        "tool_disabled" => format!("Tool disabled: {} · {}", field("name"), field("reason")),
        "needs_input" => format!("Needs input: {} · {}", field("kind"), field("tool_name")),
        "needs_input_resolved" => "Input request resolved".into(),
        "turn_refused" => format!("Turn refused: {}", field("message")),
        "session_done" => "Session ended".into(),
        "prompt_queued" => {
            let preview = data.get("text").and_then(|value| value.as_str())
                .map(|value| event_field_with_limit(value, super::MAX_INPUT_BYTES))
                .unwrap_or_default();
            if preview.is_empty() { "Prompt queued".into() }
            else { format!("Prompt queued: {preview}") }
        }
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
        data.get("id")
            .and_then(|value| value.as_str())
            .filter(|id| !id.is_empty() && id.len() <= 200 && !id.chars().any(char::is_control))
            .map(|id| {
                format!(
                    "{}{}",
                    transcript_tools::TOOL_ID_PREFIX,
                    serde_json::to_string(id).unwrap_or_default()
                )
            })
            .unwrap_or_default()
    } else {
        String::new()
    };
    Some(format!("\n\n{row}{identity}\n\n"))
}
