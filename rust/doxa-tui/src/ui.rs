//! Terminal shell for the Rust frontend. Daemon adapters can feed [`App::apply_update`].
use std::io::{self, IsTerminal, Stdout};
use std::sync::mpsc::{self, Receiver, SyncSender, TryRecvError, TrySendError};
use std::time::Duration;

use crossterm::event::{
    self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEventKind,
};
use crossterm::terminal::{self, EnterAlternateScreen, LeaveAlternateScreen};
use crossterm::{
    event::{DisableMouseCapture, EnableMouseCapture},
    execute,
};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Tabs, Wrap};
use ratatui::{Frame, Terminal};

use crate::markdown;

const MIN_PANE_WIDTH: u16 = 28;
const MIN_PANE_HEIGHT: u16 = 8;
const MAX_PENDING_PROMPTS: usize = 32;
// JSON may expand one input byte to a six-byte Unicode escape.
const MAX_INPUT_BYTES: usize = 10 * 1024;
const MAX_TRANSCRIPT_BYTES: usize = 512 * 1024;
const MAX_ANSWER_BYTES: usize = 10 * 1024;

fn safe_label(value: &str) -> String {
    markdown::sanitize(value)
        .replace('\n', " ")
        .chars()
        .take(200)
        .collect()
}

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

fn append_transcript(session: &mut Session, text: &str) -> bool {
    session.transcript.push_str(text);
    if session.transcript.len() <= MAX_TRANSCRIPT_BYTES {
        return false;
    }
    let mut start = session.transcript.len() - MAX_TRANSCRIPT_BYTES;
    while !session.transcript.is_char_boundary(start) {
        start += 1;
    }
    session.transcript.drain(..start);
    true
}

fn structured_event(event_type: &str, data: &serde_json::Value) -> Option<String> {
    let field = |key| event_string(data, key).unwrap_or_default();
    let row = match event_type {
        "reasoning_delta" => format!("Reasoning: {}", field("text")),
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
    Some(format!("\n\n{row}\n\n"))
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Session {
    pub id: String,
    pub title: String,
    pub collection: String,
    pub transcript: String,
    pub status: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PaneGroup {
    pub tabs: Vec<String>,
    pub active: usize,
    pub scroll: u16,
}

impl PaneGroup {
    fn active_id(&self) -> Option<&str> {
        self.tabs.get(self.active).map(String::as_str)
    }
}

/// Updates from a daemon reader can be delivered over a channel and applied on the UI thread.
/// The reader owns transport details; drawing and input never block on daemon I/O.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DaemonUpdate {
    Upsert(Session),
    Transcript { id: String, markdown: String },
    Status { id: String, text: String },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Focus {
    Prompt,
    Rail,
    Transcript,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Split {
    Horizontal,
    Vertical,
}

#[derive(Clone, Debug)]
pub struct QuestionOption {
    pub label: String,
    pub description: String,
}

#[derive(Clone, Debug)]
pub struct InputQuestion {
    pub question: String,
    pub header: String,
    pub options: Vec<QuestionOption>,
}

#[derive(Clone, Debug)]
pub struct InputRequest {
    pub session_id: String,
    pub id: String,
    pub kind: String,
    pub heading: String,
    pub questions: Vec<InputQuestion>,
    pub step: usize,
    pub selected: usize,
    pub answers: serde_json::Map<String, serde_json::Value>,
    pub sending: bool,
    pub allow_armed: bool,
    pub scroll: u16,
}

impl InputRequest {
    fn from_event(session_id: &str, data: &serde_json::Value) -> Option<Self> {
        let id = data.get("id")?.as_str()?.to_owned();
        if id.is_empty() || id.len() > 200 {
            return None;
        }
        let kind = data.get("kind")?.as_str()?;
        if !matches!(kind, "ask_user" | "permission" | "spawn") {
            return None;
        }
        let heading = if kind == "spawn" {
            format!(
                "{}\n{}\nTask:\n{}",
                data["title"].as_str().unwrap_or("Start another session?"),
                data["body"].as_str().unwrap_or(""),
                data["task"].as_str().unwrap_or("")
            )
        } else {
            let mut text = String::new();
            for (label, field) in [
                ("Title", "title"),
                ("Tool", "tool_name"),
                ("Display name", "display_name"),
                ("Description", "description"),
                ("Input", "input_summary"),
            ] {
                if let Some(value) = data[field].as_str().filter(|value| !value.is_empty()) {
                    text.push_str(&format!("{label}: {value}\n"));
                }
            }
            if text.is_empty() {
                "Permission request".into()
            } else {
                text
            }
        };
        let questions = if kind == "ask_user" {
            let items = data["questions"].as_array()?;
            if items.len() > 32 {
                return None;
            }
            items
                .iter()
                .map(|q| InputQuestion {
                    question: q["question"].as_str().unwrap_or("").to_owned(),
                    header: q["header"].as_str().unwrap_or("").to_owned(),
                    options: q["options"]
                        .as_array()
                        .map(|items| {
                            items
                                .iter()
                                .filter_map(|o| {
                                    Some(QuestionOption {
                                        label: o["label"].as_str()?.to_owned(),
                                        description: o["description"]
                                            .as_str()
                                            .unwrap_or("")
                                            .to_owned(),
                                    })
                                })
                                .collect()
                        })
                        .unwrap_or_default(),
                })
                .collect()
        } else {
            Vec::new()
        };
        Some(Self {
            session_id: session_id.into(),
            id,
            kind: kind.into(),
            heading,
            questions,
            step: 0,
            selected: 1,
            answers: serde_json::Map::new(),
            sending: false,
            allow_armed: false,
            scroll: 0,
        })
    }

    fn option_count(&self) -> usize {
        self.questions
            .get(self.step)
            .map(|q| q.options.len())
            .unwrap_or(0)
    }
}

#[derive(Clone, Debug)]
pub struct App {
    pub sessions: Vec<Session>,
    pub groups: [PaneGroup; 2],
    pub active_group: usize,
    pub split: Split,
    pub split_percent: u16,
    pub rail_visible: bool,
    pub rail_width: u16,
    pub rail_selected: usize,
    pub focus: Focus,
    pub input: String,
    pub pending_prompts: Vec<(String, String)>,
    pub input_requests: Vec<InputRequest>,
    pub pending_answers: Vec<(String, String, serde_json::Value)>,
    pub rejected_drafts: Vec<String>,
    pub notice: String,
    pub should_quit: bool,
    pub size: Rect,
}

impl Default for App {
    fn default() -> Self {
        Self {
            sessions: Vec::new(),
            groups: [
                PaneGroup {
                    tabs: vec![],
                    active: 0,
                    scroll: 0,
                },
                PaneGroup {
                    tabs: vec![],
                    active: 0,
                    scroll: 0,
                },
            ],
            active_group: 0,
            split: Split::Vertical,
            split_percent: 50,
            rail_visible: true,
            rail_width: 25,
            rail_selected: 0,
            focus: Focus::Prompt,
            input: String::new(),
            pending_prompts: Vec::new(),
            input_requests: Vec::new(),
            pending_answers: Vec::new(),
            rejected_drafts: Vec::new(),
            notice: "Disconnected · waiting for daemon".into(),
            should_quit: false,
            size: Rect::default(),
        }
    }
}

impl App {
    pub fn apply_update(&mut self, update: DaemonUpdate) {
        match update {
            DaemonUpdate::Upsert(session) => {
                if let Some(existing) = self.sessions.iter_mut().find(|s| s.id == session.id) {
                    *existing = session;
                } else {
                    let id = session.id.clone();
                    self.sessions.push(session);
                    if self.groups[0].tabs.is_empty() {
                        self.groups[0].tabs.push(id);
                    }
                }
            }
            DaemonUpdate::Transcript { id, markdown } => {
                if let Some(s) = self.sessions.iter_mut().find(|s| s.id == id) {
                    s.transcript = markdown;
                }
            }
            DaemonUpdate::Status { id, text } => {
                if let Some(s) = self.sessions.iter_mut().find(|s| s.id == id) {
                    s.status = text;
                }
            }
        }
        self.rail_selected = self
            .rail_selected
            .min(self.sessions.len().saturating_sub(1));
    }

    /// Apply one versioned daemon frame after transport decoding. Returns whether
    /// visible state changed. Unknown frames are ignored for forward compatibility.
    pub fn apply_daemon_frame(&mut self, frame: &serde_json::Value) -> bool {
        let Some(kind) = frame.get("type").and_then(|v| v.as_str()) else {
            return false;
        };
        match kind {
            "hello" => {
                let Some(id) = frame.get("session_id").and_then(|v| v.as_str()) else {
                    return false;
                };
                let model = safe_label(
                    frame
                        .get("model")
                        .and_then(|v| v.as_str())
                        .unwrap_or("session"),
                );
                let cwd = safe_label(frame.get("cwd").and_then(|v| v.as_str()).unwrap_or(""));
                self.apply_update(DaemonUpdate::Upsert(Session {
                    id: id.into(),
                    title: model.clone(),
                    collection: cwd,
                    transcript: String::new(),
                    status: "Connected".into(),
                }));
                self.notice = format!("Connected · {model}");
                true
            }
            "event" => {
                let Some(event) = frame.get("event") else {
                    return false;
                };
                let Some(event_type) = event.get("type").and_then(|v| v.as_str()) else {
                    return false;
                };
                let data = &event["data"];
                // A transport reader may serve several sockets. The frame itself
                // lacks a session id, so a reader can add one before delivery.
                let id = frame
                    .get("session_id")
                    .and_then(|v| v.as_str())
                    .or_else(|| self.groups[self.active_group].active_id())
                    .or_else(|| self.sessions.first().map(|s| s.id.as_str()));
                let Some(id) = id.map(str::to_owned) else {
                    return false;
                };
                match event_type {
                    "text_delta" => {
                        let Some(text) = data.get("text").and_then(|v| v.as_str()) else {
                            return false;
                        };
                        if let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) {
                            if append_transcript(session, text) {
                                self.notice = "Transcript tail limited to 512 KiB".into();
                            }
                            true
                        } else {
                            false
                        }
                    }
                    "turn_started" => {
                        self.apply_update(DaemonUpdate::Status {
                            id,
                            text: "Running".into(),
                        });
                        true
                    }
                    "turn_done" => {
                        let status = if data.get("is_error").and_then(|v| v.as_bool()) == Some(true)
                        {
                            "Error"
                        } else {
                            "Ready"
                        };
                        self.apply_update(DaemonUpdate::Status {
                            id: id.clone(),
                            text: status.into(),
                        });
                        self.append_event(&id, event_type, data);
                        true
                    }
                    "needs_input" => {
                        if let Some(request) = InputRequest::from_event(&id, data) {
                            if !self
                                .input_requests
                                .iter()
                                .any(|r| r.session_id == id && r.id == request.id)
                            {
                                self.input_requests.push(request);
                            }
                        } else {
                            self.notice = "Invalid input request · inspect another client".into();
                        }
                        self.apply_update(DaemonUpdate::Status {
                            id: id.clone(),
                            text: "Needs input".into(),
                        });
                        self.append_event(&id, event_type, data);
                        true
                    }
                    "needs_input_resolved" => {
                        if let Some(request_id) = data.get("id").and_then(|v| v.as_str()) {
                            self.input_requests
                                .retain(|r| !(r.session_id == id && r.id == request_id));
                            self.pending_answers.retain(|(session, request, _)| {
                                session != &id || request != request_id
                            });
                        }
                        self.apply_update(DaemonUpdate::Status {
                            id: id.clone(),
                            text: "Running".into(),
                        });
                        self.append_event(&id, event_type, data)
                    }
                    "session_done" => {
                        self.apply_update(DaemonUpdate::Status {
                            id: id.clone(),
                            text: "Ended".into(),
                        });
                        self.append_event(&id, event_type, data)
                    }
                    "turn_refused" => {
                        self.apply_update(DaemonUpdate::Status {
                            id: id.clone(),
                            text: "Ready".into(),
                        });
                        self.append_event(&id, event_type, data)
                    }
                    _ => self.append_event(&id, event_type, data),
                }
            }
            "reply" => {
                let ok = frame.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
                self.notice = if ok {
                    "Request accepted".into()
                } else {
                    format!(
                        "Request failed: {}",
                        safe_label(
                            frame
                                .get("error")
                                .and_then(|v| v.as_str())
                                .unwrap_or("unknown error")
                        )
                    )
                };
                true
            }
            "client_notice" => {
                self.notice = safe_label(
                    frame
                        .get("message")
                        .and_then(|v| v.as_str())
                        .unwrap_or("Daemon connection unavailable"),
                );
                true
            }
            "answer_reply" => {
                let id = frame
                    .get("request_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let session = frame
                    .get("session_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let ok = frame.get("ok").and_then(|v| v.as_bool()) == Some(true);
                let uncertain = frame.get("uncertain").and_then(|v| v.as_bool()) == Some(true);
                if let Some(request) = self
                    .input_requests
                    .iter_mut()
                    .find(|r| r.session_id == session && r.id == id)
                {
                    if !ok && !uncertain {
                        request.sending = false;
                    }
                }
                self.notice = if ok {
                    "Answer sent · awaiting resolution".into()
                } else if uncertain {
                    "Answer delivery unconfirmed · check session before retry".into()
                } else {
                    format!(
                        "Answer failed: {}",
                        safe_label(
                            frame
                                .get("message")
                                .and_then(|v| v.as_str())
                                .unwrap_or("request no longer pending")
                        )
                    )
                };
                true
            }
            "prompt_rejected" => {
                let Some(text) = frame.get("text").and_then(|v| v.as_str()) else {
                    return false;
                };
                if self.input.is_empty() {
                    self.input = text.to_owned();
                } else {
                    self.rejected_drafts.push(text.to_owned());
                }
                self.notice = format!(
                    "{} · draft retained{}",
                    safe_label(
                        frame
                            .get("message")
                            .and_then(|v| v.as_str())
                            .unwrap_or("Prompt refused")
                    ),
                    if self.rejected_drafts.is_empty() {
                        ""
                    } else {
                        " (Alt+Up to restore)"
                    }
                );
                true
            }
            "prompt_uncertain" => {
                let Some(text) = frame.get("text").and_then(|v| v.as_str()) else {
                    return false;
                };
                self.rejected_drafts.push(text.to_owned());
                self.notice = format!(
                    "{} · check session before Alt+Up retry",
                    safe_label(
                        frame
                            .get("message")
                            .and_then(|v| v.as_str())
                            .unwrap_or("Prompt delivery unconfirmed")
                    )
                );
                true
            }
            _ => false,
        }
    }

    fn append_event(&mut self, id: &str, event_type: &str, data: &serde_json::Value) -> bool {
        let Some(row) = structured_event(event_type, data) else {
            return false;
        };
        let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) else {
            return false;
        };
        if append_transcript(session, &row) {
            self.notice = "Transcript tail limited to 512 KiB".into();
        }
        true
    }

    pub fn handle(&mut self, event: Event) -> bool {
        match event {
            Event::Resize(w, h) => {
                self.size = Rect::new(0, 0, w, h);
                true
            }
            Event::Key(key)
                if key.kind == KeyEventKind::Press || key.kind == KeyEventKind::Repeat =>
            {
                self.key(key)
            }
            Event::Mouse(mouse) => self.mouse(mouse.kind),
            _ => false,
        }
    }

    fn key(&mut self, key: KeyEvent) -> bool {
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        let alt = key.modifiers.contains(KeyModifiers::ALT);
        if matches!(key.code, KeyCode::Char('c' | 'q')) && ctrl {
            self.should_quit = true;
            return true;
        }
        if self.active_request_index().is_some() {
            return self.request_key(key);
        }
        match key.code {
            KeyCode::F(3) => {
                self.rail_visible = !self.rail_visible;
                true
            }
            KeyCode::Tab if key.modifiers.contains(KeyModifiers::SHIFT) => {
                self.active_group = 1 - self.active_group;
                self.focus = Focus::Prompt;
                true
            }
            KeyCode::Tab => {
                self.focus = match self.focus {
                    Focus::Prompt => Focus::Transcript,
                    Focus::Transcript => Focus::Rail,
                    Focus::Rail => Focus::Prompt,
                };
                true
            }
            KeyCode::Esc => {
                self.focus = Focus::Prompt;
                true
            }
            KeyCode::Char('h') if alt => {
                self.split = Split::Horizontal;
                true
            }
            KeyCode::Char('v') if alt => {
                self.split = Split::Vertical;
                true
            }
            KeyCode::Up if alt && self.focus == Focus::Prompt => {
                if let Some(draft) = self.rejected_drafts.pop() {
                    let current = std::mem::replace(&mut self.input, draft);
                    if !current.is_empty() {
                        self.rejected_drafts.push(current);
                    }
                    true
                } else {
                    false
                }
            }
            KeyCode::Left if alt => self.adjust_split(-5),
            KeyCode::Right if alt => self.adjust_split(5),
            KeyCode::Up if alt => self.adjust_split(-5),
            KeyCode::Down if alt => self.adjust_split(5),
            KeyCode::Up if self.focus == Focus::Rail => {
                self.rail_selected = self.rail_selected.saturating_sub(1);
                true
            }
            KeyCode::Down if self.focus == Focus::Rail => {
                self.rail_selected =
                    (self.rail_selected + 1).min(self.sessions.len().saturating_sub(1));
                true
            }
            KeyCode::Enter if self.focus == Focus::Rail => {
                self.open_selected();
                true
            }
            KeyCode::PageUp if self.focus == Focus::Transcript => {
                let p = &mut self.groups[self.active_group];
                p.scroll = p.scroll.saturating_add(5);
                true
            }
            KeyCode::PageDown if self.focus == Focus::Transcript => {
                let p = &mut self.groups[self.active_group];
                p.scroll = p.scroll.saturating_sub(5);
                true
            }
            KeyCode::Up if self.focus == Focus::Transcript => {
                let p = &mut self.groups[self.active_group];
                p.scroll = p.scroll.saturating_add(1);
                true
            }
            KeyCode::Down if self.focus == Focus::Transcript => {
                let p = &mut self.groups[self.active_group];
                p.scroll = p.scroll.saturating_sub(1);
                true
            }
            KeyCode::Left if self.focus == Focus::Transcript => {
                self.previous_tab();
                true
            }
            KeyCode::Right if self.focus == Focus::Transcript => {
                self.next_tab();
                true
            }
            KeyCode::Backspace if self.focus == Focus::Prompt => self.input.pop().is_some(),
            KeyCode::Char(c) if self.focus == Focus::Prompt && !ctrl && !alt => {
                if self.input.len() + c.len_utf8() <= MAX_INPUT_BYTES {
                    self.input.push(c);
                    true
                } else {
                    self.notice = "Prompt input limit reached".into();
                    true
                }
            }
            KeyCode::Enter if self.focus == Focus::Prompt => {
                if !self.input.is_empty() {
                    if let Some(id) = self.groups[self.active_group].active_id() {
                        if self.pending_prompts.len() < MAX_PENDING_PROMPTS {
                            self.pending_prompts
                                .push((id.to_owned(), std::mem::take(&mut self.input)));
                            self.notice = "Prompt queued".into();
                        } else {
                            self.notice = "Prompt queue full · wait for daemon".into();
                        }
                    } else {
                        self.notice = "Select a session before sending".into();
                    }
                }
                true
            }
            _ => false,
        }
    }

    fn active_request_index(&self) -> Option<usize> {
        let id = self.groups[self.active_group].active_id()?;
        self.input_requests.iter().position(|r| r.session_id == id)
    }

    fn request_key(&mut self, key: KeyEvent) -> bool {
        let index = self.active_request_index().unwrap();
        if self.input_requests[index].sending {
            self.notice = "Answer already sent · awaiting resolution".into();
            return true;
        }
        let kind = self.input_requests[index].kind.clone();
        if kind != "ask_user" {
            if !matches!(key.code, KeyCode::Char('A' | 'Y'))
                || !(key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT)
            {
                self.input_requests[index].allow_armed = false;
            }
            match key.code {
                KeyCode::Up => {
                    self.input_requests[index].scroll =
                        self.input_requests[index].scroll.saturating_sub(1);
                    return true;
                }
                KeyCode::Down => {
                    self.input_requests[index].scroll =
                        self.input_requests[index].scroll.saturating_add(1);
                    return true;
                }
                KeyCode::PageUp => {
                    self.input_requests[index].scroll =
                        self.input_requests[index].scroll.saturating_sub(10);
                    return true;
                }
                KeyCode::PageDown => {
                    self.input_requests[index].scroll =
                        self.input_requests[index].scroll.saturating_add(10);
                    return true;
                }
                _ => {}
            }
        }
        let answer = if kind == "ask_user" {
            let count = self.input_requests[index].option_count();
            match key.code {
                KeyCode::Esc => Some(serde_json::json!({"declined":true})),
                KeyCode::Up if count > 0 => {
                    let r = &mut self.input_requests[index];
                    r.selected = if r.selected <= 1 {
                        count
                    } else {
                        r.selected - 1
                    };
                    return true;
                }
                KeyCode::Down if count > 0 => {
                    let r = &mut self.input_requests[index];
                    r.selected = if r.selected >= count {
                        1
                    } else {
                        r.selected + 1
                    };
                    return true;
                }
                KeyCode::PageUp => {
                    self.input_requests[index].scroll =
                        self.input_requests[index].scroll.saturating_sub(10);
                    return true;
                }
                KeyCode::PageDown => {
                    self.input_requests[index].scroll =
                        self.input_requests[index].scroll.saturating_add(10);
                    return true;
                }
                KeyCode::Char(c @ '1'..='9')
                    if key.modifiers.is_empty() && (c as usize - '0' as usize) <= count =>
                {
                    self.input_requests[index].selected = c as usize - '0' as usize;
                    self.choose_question(index)
                }
                KeyCode::Enter if count > 0 => self.choose_question(index),
                _ => None,
            }
        } else {
            match key.code {
                KeyCode::Esc => Some(serde_json::json!({"decision":"deny"})),
                KeyCode::Char('d' | 'D')
                    if key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT =>
                {
                    Some(serde_json::json!({"decision":"deny"}))
                }
                KeyCode::Char('A')
                    if key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT =>
                {
                    self.input_requests[index].allow_armed = true;
                    self.notice = "Approval armed · press Shift+Y to confirm".into();
                    return true;
                }
                KeyCode::Char('Y')
                    if self.input_requests[index].allow_armed
                        && (key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT) =>
                {
                    Some(serde_json::json!({"decision":"allow"}))
                }
                _ => None,
            }
        };
        if let Some(answer) = answer {
            if serde_json::to_vec(&answer).map_or(true, |bytes| bytes.len() > MAX_ANSWER_BYTES) {
                self.notice = "Answer too large · Esc to decline".into();
                return true;
            }
            let request = &mut self.input_requests[index];
            request.sending = true;
            self.pending_answers
                .push((request.session_id.clone(), request.id.clone(), answer));
            self.notice = "Sending answer".into();
        }
        true
    }

    fn choose_question(&mut self, index: usize) -> Option<serde_json::Value> {
        let request = &mut self.input_requests[index];
        let question = request.questions.get(request.step)?;
        let choice = question
            .options
            .get(request.selected.checked_sub(1)?)?
            .label
            .clone();
        request
            .answers
            .insert(question.question.clone(), serde_json::Value::String(choice));
        request.step += 1;
        request.selected = 1;
        request.scroll = 0;
        if request.step < request.questions.len() {
            None
        } else {
            Some(serde_json::json!({"answers": request.answers}))
        }
    }

    fn adjust_split(&mut self, delta: i16) -> bool {
        self.split_percent = (self.split_percent as i16 + delta).clamp(20, 80) as u16;
        true
    }

    fn rail_order(&self) -> Vec<usize> {
        let mut order: Vec<usize> = (0..self.sessions.len()).collect();
        order.sort_by(|&a, &b| {
            self.sessions[a]
                .collection
                .cmp(&self.sessions[b].collection)
                .then_with(|| self.sessions[a].title.cmp(&self.sessions[b].title))
        });
        order
    }

    /// A transport loop drains this queue and sends each prompt to its session.
    pub fn take_prompts(&mut self) -> Vec<(String, String)> {
        std::mem::take(&mut self.pending_prompts)
    }

    pub fn take_answers(&mut self) -> Vec<(String, String, serde_json::Value)> {
        std::mem::take(&mut self.pending_answers)
    }

    fn open_selected(&mut self) {
        let selected = self.rail_order().get(self.rail_selected).copied();
        if let Some(session) = selected.and_then(|index| self.sessions.get(index)) {
            let tabs = &mut self.groups[self.active_group];
            if let Some(index) = tabs.tabs.iter().position(|id| id == &session.id) {
                tabs.active = index;
            } else {
                tabs.tabs.push(session.id.clone());
                tabs.active = tabs.tabs.len() - 1;
            }
            tabs.scroll = 0;
            self.focus = Focus::Transcript;
        }
    }

    fn previous_tab(&mut self) {
        let p = &mut self.groups[self.active_group];
        p.active = p.active.saturating_sub(1);
        p.scroll = 0;
    }
    fn next_tab(&mut self) {
        let p = &mut self.groups[self.active_group];
        p.active = (p.active + 1).min(p.tabs.len().saturating_sub(1));
        p.scroll = 0;
    }

    fn mouse(&mut self, kind: MouseEventKind) -> bool {
        match kind {
            MouseEventKind::ScrollUp => {
                let p = &mut self.groups[self.active_group];
                p.scroll = p.scroll.saturating_add(3);
                true
            }
            MouseEventKind::ScrollDown => {
                let p = &mut self.groups[self.active_group];
                p.scroll = p.scroll.saturating_sub(3);
                true
            }
            _ => false,
        }
    }

    pub fn draw(&self, frame: &mut Frame) {
        let area = frame.area();
        if area.width < 20 || area.height < 5 {
            frame.render_widget(Paragraph::new("DOXA · enlarge terminal"), area);
            return;
        }
        let outer = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Min(3),
                Constraint::Length(3),
                Constraint::Length(1),
            ])
            .split(area);
        let rail_width = if self.rail_visible && outer[0].width >= 70 {
            self.rail_width
                .min(outer[0].width.saturating_sub(MIN_PANE_WIDTH))
        } else {
            0
        };
        let body = if rail_width > 0 {
            let chunks = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Length(rail_width), Constraint::Min(1)])
                .split(outer[0]);
            self.draw_rail(frame, chunks[0]);
            chunks[1]
        } else {
            outer[0]
        };
        let vertical = self.split == Split::Vertical;
        let min_ok = if vertical {
            body.width >= MIN_PANE_WIDTH * 2
        } else {
            body.height >= MIN_PANE_HEIGHT * 2
        };
        if min_ok {
            let panes = Layout::default()
                .direction(if vertical {
                    Direction::Horizontal
                } else {
                    Direction::Vertical
                })
                .constraints([
                    Constraint::Percentage(self.split_percent),
                    Constraint::Percentage(100 - self.split_percent),
                ])
                .split(body);
            self.draw_group(frame, panes[0], 0);
            self.draw_group(frame, panes[1], 1);
        } else {
            self.draw_group(frame, body, self.active_group);
        }
        let prompt_title = if self.focus == Focus::Prompt {
            " Prompt ● "
        } else {
            " Prompt "
        };
        frame.render_widget(
            Paragraph::new(format!("> {}", self.input))
                .block(Block::default().title(prompt_title).borders(Borders::ALL)),
            outer[1],
        );
        frame.render_widget(
            Paragraph::new(format!(
                "{}  |  F3 rail · Shift+Tab pane · Alt+H/V split · Alt+arrows resize · Ctrl+Q quit",
                self.notice
            )),
            outer[2],
        );
        self.draw_request(frame, area);
    }

    fn draw_request(&self, frame: &mut Frame, area: Rect) {
        let Some(index) = self.active_request_index() else {
            return;
        };
        let request = &self.input_requests[index];
        let width = area.width.saturating_sub(4).min(90);
        let height = area.height.saturating_sub(4).min(22);
        if width < 20 || height < 5 {
            return;
        }
        let modal = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        let mut body = String::new();
        if request.kind == "ask_user" {
            if let Some(question) = request.questions.get(request.step) {
                if !question.header.is_empty() {
                    body.push_str("Header: ");
                    body.push_str(&markdown::sanitize(&question.header));
                    body.push('\n');
                }
                body.push_str("Question: ");
                body.push_str(&markdown::sanitize(&question.question));
                body.push_str("\n\n");
                for (i, option) in question.options.iter().enumerate() {
                    body.push_str(&format!(
                        "{} {}. {}\n",
                        if i + 1 == request.selected {
                            "▸"
                        } else {
                            " "
                        },
                        i + 1,
                        markdown::sanitize(&option.label)
                    ));
                    if !option.description.is_empty() {
                        body.push_str("   Description: ");
                        body.push_str(&markdown::sanitize(&option.description));
                        body.push('\n');
                    }
                }
            } else {
                body.push_str("Question unavailable\n");
            }
            body.push_str("\n1–9 choose · ↑/↓ then Enter · PgUp/PgDn scroll · Esc decline");
        } else {
            body.push_str(&markdown::sanitize(&request.heading));
            body.push_str("\n\n");
            body.push_str("D deny · Esc deny · ↑/↓ scroll\nShift+A then Shift+Y to allow");
            if request.allow_armed {
                body.push_str("\nApproval armed · press Shift+Y now");
            }
        }
        if request.sending {
            body.push_str("\nSending answer…");
        }
        frame.render_widget(Clear, modal);
        frame.render_widget(
            Paragraph::new(body)
                .wrap(Wrap { trim: false })
                .scroll((request.scroll, 0))
                .block(
                    Block::default()
                        .title(format!(" Input required · {} ", request.kind))
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(Color::Yellow)),
                ),
            modal,
        );
    }

    fn draw_rail(&self, frame: &mut Frame, area: Rect) {
        let mut lines = Vec::new();
        let mut last_collection = "";
        for (position, index) in self.rail_order().into_iter().enumerate() {
            let session = &self.sessions[index];
            if session.collection != last_collection {
                last_collection = &session.collection;
                lines.push(Line::styled(
                    format!(
                        "  {}",
                        if last_collection.is_empty() {
                            "Sessions"
                        } else {
                            last_collection
                        }
                    ),
                    Style::default()
                        .fg(Color::Cyan)
                        .add_modifier(Modifier::BOLD),
                ));
            }
            let mark = if position == self.rail_selected {
                "▸"
            } else {
                " "
            };
            lines.push(Line::from(vec![Span::raw(format!(
                "{mark} {}",
                session.title
            ))]));
        }
        if lines.is_empty() {
            lines.push(Line::from("  No sessions"));
        }
        frame.render_widget(
            Paragraph::new(lines).block(
                Block::default()
                    .title(if self.focus == Focus::Rail {
                        " Sessions ● "
                    } else {
                        " Sessions "
                    })
                    .borders(Borders::ALL),
            ),
            area,
        );
    }

    fn draw_group(&self, frame: &mut Frame, area: Rect, index: usize) {
        if area.width < 4 || area.height < 3 {
            return;
        }
        let group = &self.groups[index];
        let session = group
            .active_id()
            .and_then(|id| self.sessions.iter().find(|s| s.id == id));
        let inner = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(2),
                Constraint::Min(1),
                Constraint::Length(1),
            ])
            .split(area);
        let titles: Vec<Line> = group
            .tabs
            .iter()
            .map(|id| {
                let name = self
                    .sessions
                    .iter()
                    .find(|s| &s.id == id)
                    .map(|s| s.title.as_str())
                    .unwrap_or(id);
                Line::from(name.to_owned())
            })
            .collect();
        let tabs = Tabs::new(if titles.is_empty() {
            vec![Line::from("Empty")]
        } else {
            titles
        })
        .select(group.active.min(group.tabs.len().saturating_sub(1)))
        .highlight_style(
            Style::default()
                .fg(Color::Yellow)
                .add_modifier(Modifier::BOLD),
        )
        .block(
            Block::default()
                .title(format!(
                    " Pane {}{} ",
                    index + 1,
                    if self.active_group == index {
                        " ●"
                    } else {
                        ""
                    }
                ))
                .borders(Borders::ALL),
        );
        frame.render_widget(tabs, inner[0]);
        let content = session
            .map(|s| s.transcript.as_str())
            .unwrap_or("No session open. Select one in the rail and press Enter.");
        let lines = markdown::render(content, inner[1].width.saturating_sub(2));
        let max_scroll = lines
            .len()
            .saturating_sub(inner[1].height.saturating_sub(2) as usize)
            as u16;
        let scroll_from_top = max_scroll.saturating_sub(group.scroll.min(max_scroll));
        frame.render_widget(
            Paragraph::new(lines)
                .scroll((scroll_from_top, 0))
                .wrap(Wrap { trim: false })
                .block(Block::default().borders(Borders::LEFT | Borders::RIGHT)),
            inner[1],
        );
        let status = session.map(|s| s.status.as_str()).unwrap_or("No session");
        frame.render_widget(
            Paragraph::new(format!(" {} ", status)).style(Style::default().fg(Color::DarkGray)),
            inner[2],
        );
    }
}

/// Owns terminal modes so every return path, including I/O errors, restores the screen.
struct TerminalGuard {
    out: Stdout,
    raw: bool,
    alternate: bool,
    mouse: bool,
}
impl TerminalGuard {
    fn enter() -> io::Result<Self> {
        let mut guard = Self {
            out: io::stdout(),
            raw: false,
            alternate: false,
            mouse: false,
        };
        terminal::enable_raw_mode()?;
        guard.raw = true;
        execute!(guard.out, EnterAlternateScreen)?;
        guard.alternate = true;
        execute!(guard.out, EnableMouseCapture)?;
        guard.mouse = true;
        Ok(guard)
    }
}
impl Drop for TerminalGuard {
    fn drop(&mut self) {
        if self.mouse {
            let _ = execute!(self.out, DisableMouseCapture);
        }
        if self.alternate {
            let _ = execute!(self.out, LeaveAlternateScreen);
        }
        if self.raw {
            let _ = terminal::disable_raw_mode();
        }
    }
}

pub fn run() -> io::Result<()> {
    let (_sender, receiver) = mpsc::channel();
    run_with_frames(receiver)
}

/// Drive the terminal with decoded daemon frames supplied by a reader thread.
/// Transport can be connected without changing terminal ownership or drawing.
pub fn run_with_frames(receiver: Receiver<serde_json::Value>) -> io::Result<()> {
    run_loop(receiver, None)
}

/// Connect the UI to a transport reader and writer without blocking input.
/// Prompt tuples contain the target session id and submitted text.
pub fn run_with_channels(
    frames: Receiver<serde_json::Value>,
    prompts: SyncSender<crate::bridge::WorkerCommand>,
) -> io::Result<()> {
    run_loop(frames, Some(prompts))
}

fn run_loop(
    receiver: Receiver<serde_json::Value>,
    mut prompt_sender: Option<SyncSender<crate::bridge::WorkerCommand>>,
) -> io::Result<()> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        return Err(io::Error::new(
            io::ErrorKind::NotConnected,
            "DOXA requires an interactive terminal",
        ));
    }
    let guard = TerminalGuard::enter()?;
    let mut terminal = Terminal::new(CrosstermBackend::new(&guard.out))?;
    let mut app = App {
        size: terminal
            .size()
            .map(|s| Rect::new(0, 0, s.width, s.height))?,
        ..Default::default()
    };
    terminal.draw(|frame| app.draw(frame))?;
    while !app.should_quit {
        let mut changed = false;
        // Bound work per tick so a busy daemon cannot starve keyboard input.
        for _ in 0..64 {
            match receiver.try_recv() {
                Ok(frame) => changed |= app.apply_daemon_frame(&frame),
                Err(TryRecvError::Empty | TryRecvError::Disconnected) => break,
            }
        }
        if event::poll(Duration::from_millis(50))? {
            changed |= app.handle(event::read()?);
        }
        if let Some(sender) = &prompt_sender {
            let disconnected = dispatch_prompts(&mut app, sender);
            let disconnected = dispatch_answers(&mut app, sender) || disconnected;
            if disconnected {
                prompt_sender = None;
                changed = true;
            }
        }
        if changed {
            terminal.draw(|frame| app.draw(frame))?;
        }
    }
    drop(terminal);
    drop(guard);
    Ok(())
}

/// Move only as many prompts as the bounded worker queue can accept, keeping
/// the rest in their original order for the next UI tick.
fn dispatch_prompts(app: &mut App, sender: &SyncSender<crate::bridge::WorkerCommand>) -> bool {
    let mut prompts = app.take_prompts().into_iter();
    while let Some(prompt) = prompts.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Prompt(prompt.0, prompt.1)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Prompt(id, text))) => {
                let prompt = (id, text);
                app.pending_prompts
                    .extend(std::iter::once(prompt).chain(prompts));
                app.notice = "Daemon writer busy · prompt retained".into();
                return false;
            }
            Err(TrySendError::Disconnected(crate::bridge::WorkerCommand::Prompt(id, text))) => {
                let prompt = (id, text);
                app.pending_prompts
                    .extend(std::iter::once(prompt).chain(prompts));
                app.notice = "Daemon writer unavailable · prompt retained".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

fn dispatch_answers(app: &mut App, sender: &SyncSender<crate::bridge::WorkerCommand>) -> bool {
    let mut answers = app.take_answers().into_iter();
    while let Some(answer) = answers.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Answer(
            answer.0, answer.1, answer.2,
        )) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Answer(session, id, payload))) => {
                app.pending_answers
                    .extend(std::iter::once((session, id, payload)).chain(answers));
                app.notice = "Daemon writer busy · answer retained".into();
                return false;
            }
            Err(TrySendError::Disconnected(crate::bridge::WorkerCommand::Answer(
                session,
                id,
                payload,
            ))) => {
                app.pending_answers
                    .extend(std::iter::once((session, id, payload)).chain(answers));
                app.notice = "Daemon writer unavailable · answer retained".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn full_worker_queue_keeps_prompts_in_order() {
        let (sender, receiver) = mpsc::sync_channel(1);
        sender
            .send(crate::bridge::WorkerCommand::Prompt(
                "s".into(),
                "first".into(),
            ))
            .unwrap();
        let mut app = App {
            pending_prompts: vec![("s".into(), "second".into()), ("s".into(), "third".into())],
            ..Default::default()
        };
        assert!(!dispatch_prompts(&mut app, &sender));
        assert_eq!(
            app.pending_prompts
                .iter()
                .map(|p| p.1.as_str())
                .collect::<Vec<_>>(),
            vec!["second", "third"]
        );
        assert!(
            matches!(receiver.recv().unwrap(), crate::bridge::WorkerCommand::Prompt(_, text) if text == "first")
        );
        assert!(!dispatch_prompts(&mut app, &sender));
        assert!(
            matches!(receiver.recv().unwrap(), crate::bridge::WorkerCommand::Prompt(_, text) if text == "second")
        );
        assert_eq!(app.pending_prompts[0].1, "third");
    }

    #[test]
    fn rejected_prompt_is_editable_and_does_not_replace_current_draft() {
        let mut app = App {
            input: "new draft".into(),
            ..Default::default()
        };
        app.apply_daemon_frame(
            &json!({"type":"prompt_rejected", "text":"old prompt", "message":"queue full"}),
        );
        assert_eq!(app.input, "new draft");
        assert_eq!(app.rejected_drafts, ["old prompt"]);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)));
        assert_eq!(app.input, "old prompt");
        assert_eq!(app.rejected_drafts, ["new draft"]);
    }

    #[test]
    fn unconfirmed_prompt_requires_deliberate_recovery_before_retry() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"prompt_uncertain", "text":"possibly sent",
            "message":"Prompt delivery unconfirmed"}));
        assert!(app.input.is_empty());
        assert_eq!(app.rejected_drafts, ["possibly sent"]);
        assert!(app.notice.contains("check session"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)));
        assert_eq!(app.input, "possibly sent");
    }
}
