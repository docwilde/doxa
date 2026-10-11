//! Display-only message records. Queue metadata never becomes prompt Markdown.

use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{transcript_events::{append_transcript, transcript_tail}, Session, MAX_INPUT_BYTES, MAX_TRANSCRIPT_BYTES};
use crate::markdown;

pub(super) const PREFIX: &str = "\u{001e}DOXA_MESSAGE:";

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub(super) enum QueueState { Queued, Started, Cancelled, Discarded }

#[derive(Clone, Debug, Deserialize, Serialize)]
pub(super) struct Message {
    pub queue_id: Option<String>,
    pub state: Option<QueueState>,
    pub text: String,
    pub timestamp: Option<String>,
    #[serde(default)]
    pub sender: Option<String>,
    #[serde(default)]
    pub nerd_font: bool,
    #[serde(default)]
    pub unreviewed: bool,
    #[serde(default)]
    pub starts_turn: bool,
}

fn queue_id(data: &Value) -> Option<String> {
    let id = data["id"].as_str()?;
    (!id.is_empty() && id.len() <= 128
        && id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-'))
        .then(|| id.to_owned())
}

fn timestamp(value: Option<&Value>) -> Option<String> {
    match value? {
        Value::String(text) if !text.is_empty() && text.len() <= 64
            && !text.chars().any(char::is_control) => Some(text.clone()),
        Value::Number(value) if value.as_f64().is_some_and(|v| v.is_finite() && v >= 0.0) => Some(value.to_string()),
        _ => None,
    }
}

fn body(text: &str) -> String {
    let mut text = markdown::sanitize(text);
    if text.len() > MAX_INPUT_BYTES {
        let mut end = MAX_INPUT_BYTES;
        while !text.is_char_boundary(end) { end -= 1; }
        text.truncate(end);
        text.push('…');
    }
    text
}

impl Message {
    pub(super) fn queued(data: &Value, event_timestamp: Option<&Value>, nerd_font: bool) -> Option<Self> {
        Some(Self {
            queue_id: Some(queue_id(data)?), state: Some(QueueState::Queued),
            text: body(data["text"].as_str()?),
            timestamp: timestamp(data.get("timestamp").or(event_timestamp)),
            sender: (data["peer_started"] == true).then(|| attribution(data["peer_origin"].as_str().unwrap_or("Peer"))),
            nerd_font, unreviewed: false, starts_turn: false,
        })
    }

    pub(super) fn peer(data: &Value, nerd_font: bool) -> Option<Self> {
        let title: String = attribution(data["from_title"].as_str().unwrap_or("Peer")).chars().take(128).collect();
        let id = data["from_id"].as_str().filter(|id| !id.is_empty() && id.len() <= 128
            && id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-'));
        Some(Self { queue_id: None, state: None, text: body(data["body"].as_str()?),
            timestamp: timestamp(data.get("sent_at")),
            sender: Some(id.map(|id| format!("{title} ({id})")).unwrap_or(title)),
            nerd_font, unreviewed: data["fleet_admission"]["unreviewed"] == true, starts_turn: false })
    }

    pub(super) fn peer_turn(data: &Value, nerd_font: bool) -> Option<Self> {
        (data["peer_started"] == true).then(|| Self { queue_id: None, state: None,
            text: body(data["prompt"].as_str().unwrap_or("")), timestamp: timestamp(data.get("timestamp")),
            sender: Some(attribution(data["peer_origin"].as_str().unwrap_or("Peer"))),
            nerd_font, unreviewed: false, starts_turn: true })
    }

    pub(super) fn parse(paragraph: &str) -> Option<Self> {
        let record = paragraph.strip_prefix(PREFIX)?;
        if record.len() > MAX_INPUT_BYTES * 6 + 1024 { return None; }
        let mut message: Self = serde_json::from_str(record).ok()?;
        if let Some(id) = message.queue_id.as_deref() {
            if id.is_empty() || id.len() > 128
                || !id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
                || message.state.is_none() || message.starts_turn { return None; }
        } else if message.state.is_some() || message.sender.is_none() { return None; }
        if message.text.len() > MAX_INPUT_BYTES + 3 { return None; }
        message.text = body(&message.text);
        message.timestamp = timestamp(message.timestamp.as_ref().map(|text| Value::String(text.clone())).as_ref());
        message.sender = message.sender.map(|sender| attribution(&sender));
        Some(message)
    }

    pub(super) fn label(&self) -> String {
        let state = match self.state {
            Some(QueueState::Queued) => "Queued",
            Some(QueueState::Started) => "Queued prompt started",
            Some(QueueState::Cancelled) => "Queued prompt cancelled",
            Some(QueueState::Discarded) => "Queued prompt discarded",
            None => "Message",
        };
        let queued = self.queue_id.as_ref().map(|id| format!("{state} · {id}")).unwrap_or_default();
        let peer = self.sender.as_ref().map(|sender| format!("{} Peer {sender}{}",
            if self.nerd_font { "\u{f06a9}" } else { "🤖" },
            if self.unreviewed { " [unreviewed fleet message]" } else { "" })).unwrap_or_default();
        let separator = if !queued.is_empty() && !peer.is_empty() { " · " } else { "" };
        format!("{queued}{separator}{peer}{}", self.timestamp.as_ref().map(|time| format!(" · {time}")).unwrap_or_default())
    }

    fn encoded(&self) -> String { format!("{PREFIX}{}", serde_json::to_string(self).unwrap()) }
}

fn attribution(text: &str) -> String {
    markdown::sanitize(text).replace(['\n', '\r'], " ").chars().take(512).collect()
}

pub(super) fn append(session: &mut Session, message: &Message) -> bool {
    append_transcript(session, &format!("\n\n{}\n\n", message.encoded()))
}

fn replace(session: &mut Session, range: std::ops::Range<usize>, message: &Message) -> bool {
    session.transcript.replace_range(range, &message.encoded());
    let clipped = session.transcript.len() > MAX_TRANSCRIPT_BYTES;
    if clipped { session.transcript = transcript_tail(&session.transcript).to_owned(); }
    clipped
}

fn find(source: &str, id: &str) -> Option<(std::ops::Range<usize>, Message)> {
    let mut start = 0;
    for paragraph in source.split("\n\n") {
        let leading = paragraph.len() - paragraph.trim_start_matches('\n').len();
        if let Some(message) = Message::parse(paragraph.trim_matches('\n')).filter(|message| message.queue_id.as_deref() == Some(id)) {
            return Some((start + leading..start + paragraph.trim_end_matches('\n').len(), message));
        }
        start += paragraph.len() + 2;
    }
    None
}

/// Returns (new admission, display tail evicted). Replayed queue events update
/// the existing record without incrementing the queue or duplicating its body.
pub(super) fn enqueue(session: &mut Session, message: Message) -> (bool, bool) {
    if let Some((range, previous)) = find(&session.transcript, message.queue_id.as_deref().unwrap()) {
        if previous.state == Some(QueueState::Queued) {
            let mut message = message;
            message.timestamp = previous.timestamp.or(message.timestamp);
            return (false, replace(session, range, &message));
        }
        return (false, false);
    }
    let clipped = append_transcript(session, &format!("\n\n{}\n\n", message.encoded()));
    (true, clipped)
}

/// A started preview relinquishes its body to turn_started. Its queue identity
/// and admission timestamp remain visible; cancelled/discarded bodies remain.
pub(super) fn settle(session: &mut Session, data: &Value, state: QueueState) -> (bool, bool) {
    let Some(id) = queue_id(data) else { return (false, false); };
    if let Some((range, mut message)) = find(&session.transcript, &id) {
        if message.state != Some(QueueState::Queued) { return (false, false); }
        message.state = Some(state);
        if state == QueueState::Started { message.text.clear(); }
        return (true, replace(session, range, &message));
    }
    let message = Message { queue_id: Some(id), state: Some(state), text: String::new(),
        timestamp: timestamp(data.get("timestamp")), sender: None, nerd_font: false, unreviewed: false, starts_turn: false };
    (true, append_transcript(session, &format!("\n\n{}\n\n", message.encoded())))
}
