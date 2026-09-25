//! Terminal shell for the Rust frontend. Daemon adapters can feed [`App::apply_update`].
use std::collections::{HashMap, HashSet};
use std::io::{self, IsTerminal, Stdout};
use std::path::PathBuf;
use std::sync::mpsc::{self, Receiver, SyncSender, TryRecvError, TrySendError};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crossterm::event::{
    self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent,
    MouseEventKind,
};
use crossterm::terminal::{self, EnterAlternateScreen, LeaveAlternateScreen};
use crossterm::{
    event::{DisableMouseCapture, EnableMouseCapture},
    execute,
};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Tabs, Wrap};
use ratatui::{Frame, Terminal};
use unicode_width::UnicodeWidthStr;

use crate::{diff_view, history, launch, markdown, peer_map::PeerMap};
use crate::theme;

mod tool_cards;
use tool_cards::ToolCards;

const MIN_PANE_WIDTH: u16 = 28;
const MIN_PANE_HEIGHT: u16 = 8;
const MIN_RAIL_WIDTH: u16 = 12;
const MAX_PENDING_PROMPTS: usize = 32;
const MAX_INPUT_REQUESTS: usize = 32;
// JSON may expand one input byte to a six-byte Unicode escape.
const MAX_INPUT_BYTES: usize = 10 * 1024;
const MAX_TRANSCRIPT_BYTES: usize = 512 * 1024;
const MAX_ANSWER_BYTES: usize = 10 * 1024;
const ACTIONS: [(&str, &str); 12] = [
    ("Peer map", "Ctrl+M"),
    ("Tool activity", "Ctrl+T"),
    ("Open selected session", "rail selection"),
    ("Previous tab", "active pane"),
    ("Next tab", "active pane"),
    ("Switch pane", "Shift+Tab"),
    ("Session history", "Ctrl+R"),
    ("Worktree diff", "F2"),
    ("Engine for new session", "Alt+E"),
    ("Session model", "Alt+M"),
    ("Claude permissions", "Alt+P"),
    ("Stop active session", "Alt+X"),
];

const ENGINE_CHOICES: [&str; 4] = ["codex", "claude", "deepseek", "glm"];
const PERMISSION_CHOICES: [(&str, &str); 5] = [
    ("default", "Ask before dangerous calls"),
    ("acceptEdits", "Allow file edits; ask for other calls"),
    ("plan", "Planning only; no tools run"),
    ("auto", "Model classifier decides each call"),
    ("dontAsk", "Deny unapproved calls without asking"),
];

fn permission_index(mode: &str) -> Option<usize> {
    PERMISSION_CHOICES.iter().position(|(candidate, _)| *candidate == mode)
}

#[derive(Debug)]
struct ModelPicker {
    session_id: String,
    models: Vec<String>,
    selected: usize,
    note: String,
    loading: bool,
    catalog_pending: bool,
}

#[derive(Debug)]
struct NewSession {
    engine: launch::Engine,
    model: String,
    prompt: String,
    field: usize,
}

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

fn transcript_tail(text: &str) -> &str {
    if text.len() <= MAX_TRANSCRIPT_BYTES { return text; }
    let mut start = text.len() - MAX_TRANSCRIPT_BYTES;
    while !text.is_char_boundary(start) { start += 1; }
    &text[start..]
}

fn append_transcript(session: &mut Session, text: &str) -> bool {
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

#[derive(Clone, Debug, Default)]
struct SessionTelemetry {
    context: Option<String>,
    usage: Option<String>,
    cost: Option<String>,
    lore: Option<String>,
}

impl SessionTelemetry {
    fn update_turn(&mut self, data: &serde_json::Value) {
        let context = data["ctx_percentage"].as_f64()
            .filter(|value| value.is_finite() && (0.0..=100.0).contains(value))
            .map(|value| format!("{value:.0}%"));
        let absolute = data["ctx_tokens"].as_u64().zip(data["ctx_max_tokens"].as_u64())
            .filter(|(used, limit)| *limit > 0 && used <= limit)
            .map(|(used, limit)| format!("{used}/{limit}"));
        if data.get("ctx_percentage").is_some() || data.get("ctx_tokens").is_some() {
            self.context = context.or(absolute);
        }
        let scope = data["usage_scope"].as_str();
        let source = data["usage_source"].as_str();
        let tokens = match (scope, source) {
            (Some("session"), Some("codex_cli_turn_completed")) =>
                data["input_tokens"].as_u64().zip(data["output_tokens"].as_u64())
                    .map(|(input, output)| (input, output, "session")),
            (Some("turn"), Some("vendor_response")) =>
                data["prompt_tokens"].as_u64().zip(data["completion_tokens"].as_u64())
                    .map(|(input, output)| (input, output, "turn")),
            _ => None,
        };
        if let Some((input, output, scope)) = tokens {
            self.usage = Some(format!("{input}/{output} {scope}"));
        } else if self.usage.as_deref().is_some_and(|usage| usage.ends_with(" turn")) {
            self.usage = None;
        }
        if let Some(cost) = data["session_cost_usd"].as_f64()
            .filter(|value| value.is_finite() && *value >= 0.0) {
            self.cost = Some(format!("${cost:.4} session"));
        } else if let Some(cost) = data["cost_usd"].as_f64()
            .filter(|value| value.is_finite() && *value >= 0.0) {
            self.cost = Some(format!("${cost:.4} turn"));
        } else if data.get("session_cost_usd").is_some() || data.get("cost_usd").is_some() {
            self.cost = None;
        }
    }

    fn update_status(&mut self, status: &serde_json::Value) {
        let context = status["ctx_percentage"].as_f64()
            .filter(|value| value.is_finite() && (0.0..=100.0).contains(value))
            .map(|value| format!("{value:.0}%"));
        let absolute = status["ctx_tokens"].as_u64().zip(status["ctx_max_tokens"].as_u64())
            .filter(|(used, limit)| *limit > 0 && used <= limit)
            .map(|(used, limit)| format!("{used}/{limit}"));
        if status.get("ctx_percentage").is_some() || status.get("ctx_tokens").is_some() {
            self.context = context.or(absolute);
        }
        if let Some(cost) = status["total_cost_usd"].as_f64()
            .filter(|value| value.is_finite() && *value >= 0.0) {
            let label = if status["usage"]["cost_basis"].as_str().is_some() {
                if status["usage"]["unpriced_models"].as_array().is_some_and(|models| !models.is_empty()) {
                    "est partial"
                } else { "est" }
            } else { "session" };
            self.cost = Some(format!("${cost:.4} {label}"));
        } else if status.get("total_cost_usd").is_some() {
            self.cost = None;
        }
        let usage = &status["usage"];
        if usage["num_turns"].as_u64().is_some_and(|turns| turns > 0) {
            if let Some((input, output)) = usage["input_tokens"].as_u64().zip(usage["output_tokens"].as_u64()) {
                self.usage = Some(format!("{input}/{output} session"));
            }
        } else if usage["num_turns"].as_u64() == Some(0) {
            self.usage = None;
        }
        if let Some(count) = status["belief_count"].as_u64() {
            self.lore = Some(format!("{count} beliefs"));
        } else if status.get("lore_scrub").is_some() {
            self.lore = match status["lore_scrub"].as_str() {
                Some("ready") => Some("scrub ready".into()),
                Some("unavailable") => Some("scrub unavailable".into()),
                _ => None,
            };
        }
    }

    fn line(&self) -> String {
        format!(" Ctx {}  Tokens {}  Cost {}  LORE {}",
            self.context.as_deref().unwrap_or("?"),
            self.usage.as_deref().unwrap_or("?"),
            self.cost.as_deref().unwrap_or("?"),
            self.lore.as_deref().unwrap_or("?"))
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PaneGroup {
    pub tabs: Vec<String>,
    pub active: usize,
    /// Lines above the viewport, measured from the transcript tail.
    pub scroll: usize,
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

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DragTarget {
    Rail,
    Pane(Split),
}

#[derive(Clone, Copy)]
struct PaneLayout {
    outer: Rect,
    rail: Option<Rect>,
    body: Rect,
    panes: Option<[Rect; 2]>,
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

#[derive(Debug)]
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
    input_drafts: HashMap<(usize, String), String>,
    session_identity: HashMap<String, (Option<String>, Option<String>)>,
    session_telemetry: HashMap<String, SessionTelemetry>,
    model_capabilities: HashMap<String, bool>,
    permission_capabilities: HashMap<String, bool>,
    permission_modes: HashMap<String, String>,
    session_activity: HashMap<String, (bool, usize)>,
    permission_picker: Option<(String, usize)>,
    permission_confirm_dont_ask: bool,
    pending_permission_changes: Vec<(String, String)>,
    stop_confirmation: Option<String>,
    pending_stops: Vec<String>,
    model_picker: Option<ModelPicker>,
    engine_picker: bool,
    engine_selected: usize,
    new_session: Option<NewSession>,
    pending_launches: Vec<(launch::LaunchOptions, Option<String>, usize)>,
    launching: bool,
    pending_model_queries: Vec<String>,
    pending_model_changes: Vec<(String, String)>,
    pub pending_prompts: Vec<(String, String)>,
    pub input_requests: Vec<InputRequest>,
    pub pending_answers: Vec<(String, String, serde_json::Value)>,
    pub rejected_drafts: HashMap<String, Vec<String>>,
    tool_cards: ToolCards,
    tool_modal: bool,
    tool_selected: usize,
    tool_scroll: u16,
    peer_map: PeerMap,
    map_modal: bool,
    action_menu: bool,
    action_selected: usize,
    history_modal: bool,
    history_query: String,
    history_selected: usize,
    history_pending: Option<Receiver<Vec<history::OfflineSession>>>,
    offline_ids: HashSet<String>,
    diff_modal: bool,
    diff_pane: bool,
    diff_target: Option<String>,
    diff_scroll: u16,
    diff_text: String,
    diff_pending: Option<Receiver<(String, diff_view::DiffSnapshot)>>,
    session_cwds: HashMap<String, PathBuf>,
    pending_peer_refresh: Option<String>,
    pub notice: String,
    pub should_quit: bool,
    pub size: Rect,
    drag: Option<DragTarget>,
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
            input_drafts: HashMap::new(),
            session_identity: HashMap::new(),
            session_telemetry: HashMap::new(),
            model_capabilities: HashMap::new(),
            permission_capabilities: HashMap::new(),
            permission_modes: HashMap::new(),
            session_activity: HashMap::new(),
            permission_picker: None,
            permission_confirm_dont_ask: false,
            pending_permission_changes: Vec::new(),
            stop_confirmation: None,
            pending_stops: Vec::new(),
            model_picker: None,
            engine_picker: false,
            engine_selected: 0,
            new_session: None,
            pending_launches: Vec::new(),
            launching: false,
            pending_model_queries: Vec::new(),
            pending_model_changes: Vec::new(),
            pending_prompts: Vec::new(),
            input_requests: Vec::new(),
            pending_answers: Vec::new(),
            rejected_drafts: HashMap::new(),
            tool_cards: ToolCards::default(),
            tool_modal: false,
            tool_selected: 0,
            tool_scroll: 0,
            peer_map: PeerMap::default(),
            map_modal: false,
            action_menu: false,
            action_selected: 0,
            history_modal: false,
            history_query: String::new(),
            history_selected: 0,
            history_pending: None,
            offline_ids: HashSet::new(),
            diff_modal: false,
            diff_pane: false,
            diff_target: None,
            diff_scroll: 0,
            diff_text: String::new(),
            diff_pending: None,
            session_cwds: HashMap::new(),
            pending_peer_refresh: None,
            notice: "Disconnected · waiting for daemon".into(),
            should_quit: false,
            size: Rect::default(),
            drag: None,
        }
    }
}

impl App {
    pub fn apply_update(&mut self, update: DaemonUpdate) {
        match update {
            DaemonUpdate::Upsert(mut session) => {
                session.transcript = transcript_tail(&session.transcript).to_owned();
                self.offline_ids.remove(&session.id);
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
                    s.transcript = transcript_tail(&markdown).to_owned();
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
            "launch_reply" => {
                if !self.launching { return false; }
                self.launching = false;
                if frame["ok"] == true {
                    if let Some(id) = frame["session_id"].as_str().filter(|id| crate::discovery::valid_id(id)) {
                        let target = frame["group"].as_u64().filter(|group| *group < 2)
                            .map(|group| group as usize).unwrap_or(self.active_group);
                        // A newly attached daemon may send hello before this reply.
                        // Upsert places the first observed session in group zero;
                        // move that provisional tab to the requested pane.
                        if target != 0 {
                            let first = &mut self.groups[0];
                            if let Some(index) = first.tabs.iter().position(|tab| tab == id) {
                                first.tabs.remove(index);
                                first.active = first.active.min(first.tabs.len().saturating_sub(1));
                            }
                        }
                        let group = &mut self.groups[target];
                        if !group.tabs.iter().any(|tab| tab == id) { group.tabs.push(id.to_owned()); }
                        group.active = group.tabs.iter().position(|tab| tab == id).unwrap_or(group.active);
                    }
                }
                self.notice = if frame["ok"] == true {
                    format!("Session started · {}", safe_label(frame["session_id"].as_str().unwrap_or("")))
                } else if frame["started"] == true {
                    let id = frame["session_id"].as_str().filter(|id| crate::discovery::valid_id(id))
                        .unwrap_or("unknown");
                    format!("Session started; UI attach failed · doxa-rs attach {id}")
                } else {
                    format!("Session launch failed · {}", safe_label(frame["message"].as_str().unwrap_or("unknown error")))
                };
                true
            }
            "hello" => {
                let Some(id) = frame.get("session_id").and_then(|v| v.as_str()) else {
                    return false;
                };
                let model = frame.get("model").and_then(|v| v.as_str()).map(safe_label).filter(|s| !s.is_empty());
                let engine = frame.get("engine").and_then(|v| v.as_str()).map(safe_label).filter(|s| !s.is_empty());
                self.session_identity.insert(id.to_owned(), (engine, model.clone()));
                self.session_telemetry.entry(id.to_owned()).or_default().update_status(frame);
                self.model_capabilities.insert(id.to_owned(), frame["can_set_model"] == true);
                self.permission_capabilities.insert(id.to_owned(), frame["can_set_permission_mode"] == true);
                if let Some(mode) = frame["permission_mode"].as_str().filter(|mode| permission_index(mode).is_some()) {
                    self.permission_modes.insert(id.to_owned(), mode.to_owned());
                }
                self.session_activity.insert(id.to_owned(), (frame["running"] == true,
                    frame["queued"].as_u64().unwrap_or(0) as usize));
                let cwd = safe_label(frame.get("cwd").and_then(|v| v.as_str()).unwrap_or(""));
                if let Some(raw) = frame.get("cwd").and_then(|v| v.as_str()) {
                    let path = PathBuf::from(raw);
                    if path.is_absolute() && raw.len() <= 4096 {
                        self.session_cwds.insert(id.to_owned(), path);
                    }
                }
                let transcript = self
                    .sessions
                    .iter()
                    .find(|s| s.id == id)
                    .map(|s| s.transcript.clone())
                    .unwrap_or_default();
                self.apply_update(DaemonUpdate::Upsert(Session {
                    id: id.into(),
                    title: model.clone().unwrap_or_else(|| safe_label(id)),
                    collection: cwd,
                    transcript,
                    status: "Connected".into(),
                }));
                self.notice = format!("Connected · {}", model.as_deref().unwrap_or(&safe_label(id)));
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
                // The transport tags every socket frame before delivery. An
                // untagged frame must not alter the currently focused session.
                let Some(id) = frame.get("session_id").and_then(|v| v.as_str()).map(str::to_owned) else {
                    return false;
                };
                if self.sessions.iter().any(|session| session.id == id) {
                    self.tool_cards.record(&id, event_type, data);
                    self.peer_map.event(&id, event_type, data);
                }
                match event_type {
                    "model_changed" => {
                        let old_model = self.session_identity.get(&id).and_then(|identity| identity.1.clone());
                        let new_model = data.get("model").and_then(|v| v.as_str()).map(safe_label).filter(|s| !s.is_empty());
                        if let Some(identity) = self.session_identity.get_mut(&id) {
                            identity.1 = new_model.clone();
                        }
                        if let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) {
                            if old_model.as_deref() == Some(session.title.as_str()) {
                                if let Some(model) = new_model { session.title = model; }
                            }
                        }
                        true
                    }
                    "permission_mode_changed" => {
                        if let Some(mode) = data["mode"].as_str().filter(|mode| permission_index(mode).is_some()) {
                            self.permission_modes.insert(id, mode.to_owned());
                        }
                        true
                    }
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
                        self.session_activity.entry(id.clone()).or_default().0 = true;
                        self.apply_update(DaemonUpdate::Status {
                            id,
                            text: "Running".into(),
                        });
                        true
                    }
                    "turn_done" => {
                        self.session_telemetry.entry(id.clone()).or_default().update_turn(data);
                        self.session_activity.entry(id.clone()).or_default().0 = false;
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
                                self.drag = None;
                                self.tool_modal = false;
                                self.model_picker = None;
                                self.permission_picker = None;
                                self.permission_confirm_dont_ask = false;
                                self.engine_picker = false;
                                self.stop_confirmation = None;
                                if self.input_requests.len() < MAX_INPUT_REQUESTS {
                                    self.input_requests.push(request);
                                } else {
                                    self.notice = "Too many input requests · inspect the session directly".into();
                                }
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
                    "prompt_queued" => {
                        self.session_activity.entry(id.clone()).or_default().1 += 1;
                        self.append_event(&id, event_type, data)
                    }
                    "prompt_dequeued" | "prompt_cancelled" | "prompt_discarded" => {
                        let activity = self.session_activity.entry(id.clone()).or_default();
                        activity.1 = activity.1.saturating_sub(1);
                        self.append_event(&id, event_type, data)
                    }
                    _ => self.append_event(&id, event_type, data),
                }
            }
            "peer_roster" => {
                let Some(id) = frame.get("session_id").and_then(|v| v.as_str()) else {
                    return false;
                };
                self.peer_map.roster(id, frame)
            }
            "models_reply" => {
                let Some(id) = frame.get("session_id").and_then(|v| v.as_str()) else { return false; };
                if let Some(picker) = self.model_picker.as_mut().filter(|picker| picker.session_id == id) {
                    picker.loading = false;
                    picker.catalog_pending = frame["loading"] == true;
                    picker.models = if frame["ok"] == true {
                        frame["models"].as_array().into_iter().flatten()
                            .filter_map(|value| value.as_str())
                            .filter(|model| !model.is_empty() && model.len() <= 128 && !model.chars().any(char::is_control))
                            .take(100).map(safe_label).collect()
                    } else { Vec::new() };
                    picker.note = if frame["ok"] == true {
                        frame["note"].as_str().map(safe_label).unwrap_or_default()
                    } else {
                        format!("Catalog unavailable: {}", safe_label(frame["error"].as_str().unwrap_or("unknown error")))
                    };
                    picker.selected = 0;
                    return true;
                }
                false
            }
            "set_model_reply" => {
                self.notice = if frame["ok"] == true {
                    format!("Model selected · {}", safe_label(frame["model"].as_str().unwrap_or("awaiting event")))
                } else {
                    format!("Model change failed · {}", safe_label(frame["error"].as_str().unwrap_or("unknown error")))
                };
                true
            }
            "set_permission_mode_reply" => {
                self.notice = if frame["ok"] == true {
                    format!("Permission mode selected · {}", safe_label(frame["mode"].as_str().unwrap_or("awaiting event")))
                } else {
                    format!("Permission change failed · {}", safe_label(frame["error"].as_str().unwrap_or("unknown error")))
                };
                true
            }
            "stop_reply" => {
                let Some(id) = frame["session_id"].as_str().filter(|id| self.sessions.iter().any(|s| s.id == *id)) else { return false; };
                if frame["ok"] == true {
                    self.offline_ids.insert(id.to_owned());
                    self.input_requests.retain(|request| request.session_id != id);
                    self.apply_update(DaemonUpdate::Status { id: id.to_owned(), text: "Stopping".into() });
                    self.notice = format!("Stop accepted · {}", safe_label(id));
                } else {
                    self.notice = format!("Stop failed · {}", safe_label(frame["error"].as_str().unwrap_or("unknown error")));
                }
                true
            }
            "telemetry_unavailable" => {
                if let Some(id) = frame["session_id"].as_str() {
                    self.session_telemetry.entry(id.to_owned()).or_default().lore = None;
                    return true;
                }
                false
            }
            "telemetry_status" => {
                let Some(id) = frame["session_id"].as_str() else { return false; };
                let Some(status) = frame.get("status") else { return false; };
                self.session_telemetry.entry(id.to_owned()).or_default().update_status(status);
                true
            }
            "reply" => {
                // Both native and Python daemons broadcast prompt_queued after
                // the enqueue reply. Count that event once, not this reply.
                if let Some(status) = frame.get("status") {
                    let id = status.get("session_id").and_then(|v| v.as_str())
                        .or_else(|| frame.get("session_id").and_then(|v| v.as_str()));
                    if let Some(id) = id {
                        self.session_telemetry.entry(id.to_owned()).or_default().update_status(status);
                        let identity = self.session_identity.entry(id.to_owned()).or_default();
                        if status.get("engine").is_some() {
                            identity.0 = status.get("engine").and_then(|v| v.as_str()).map(safe_label).filter(|s| !s.is_empty());
                        }
                        if status.get("model").is_some() {
                            identity.1 = status.get("model").and_then(|v| v.as_str()).map(safe_label).filter(|s| !s.is_empty());
                        }
                        if let Some(can_set) = status.get("can_set_model").and_then(|v| v.as_bool()) {
                            self.model_capabilities.insert(id.to_owned(), can_set);
                        }
                        if let Some(can_set) = status.get("can_set_permission_mode").and_then(|v| v.as_bool()) {
                            self.permission_capabilities.insert(id.to_owned(), can_set);
                        }
                        if let Some(mode) = status["permission_mode"].as_str().filter(|mode| permission_index(mode).is_some()) {
                            self.permission_modes.insert(id.to_owned(), mode.to_owned());
                        }
                        if let (Some(running), Some(queued)) = (status["running"].as_bool(), status["queued"].as_u64()) {
                            self.session_activity.insert(id.to_owned(), (running, queued as usize));
                        }
                    }
                }
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
                if let Some(id) = frame.get("session_id").and_then(|v| v.as_str()) {
                    self.apply_update(DaemonUpdate::Status {
                        id: id.into(),
                        text: "Disconnected".into(),
                    });
                }
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
                    // An AskUser answer stays on its final question until the
                    // daemon confirms delivery. A refused or unconfirmed send
                    // must leave that selection available for a manual retry.
                    if !ok && (!uncertain || request.kind == "ask_user") {
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
                let Some(target) = frame.get("session_id").and_then(|v| v.as_str()) else {
                    return false;
                };
                let active = self.groups[self.active_group].active_id().unwrap_or("");
                if self.input.is_empty() && target == active {
                    self.input = text.to_owned();
                } else {
                    self.rejected_drafts
                        .entry(target.into())
                        .or_default()
                        .push(text.to_owned());
                }
                self.notice = format!(
                    "{} · draft retained{}",
                    safe_label(
                        frame
                            .get("message")
                            .and_then(|v| v.as_str())
                            .unwrap_or("Prompt refused")
                    ),
                    if self.rejected_drafts.get(target).is_none_or(Vec::is_empty) {
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
                let Some(target) = frame.get("session_id").and_then(|v| v.as_str()) else {
                    return false;
                };
                self.rejected_drafts
                    .entry(target.into())
                    .or_default()
                    .push(text.to_owned());
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
        let before = (self.active_group, self.groups[self.active_group].active_id().unwrap_or("").to_owned());
        let changed = match event {
            Event::Resize(w, h) => {
                self.size = Rect::new(0, 0, w, h);
                self.drag = None;
                if self.history_modal && !self.history_fits() {
                    self.history_modal = false;
                    self.notice = "Enlarge terminal to open session history".into();
                }
                if ((self.model_picker.is_some() || self.engine_picker || self.new_session.is_some()) && (w < 29 || h < 11))
                    || (self.permission_picker.is_some() && (w < 60 || h < 15)) {
                    self.model_picker = None;
                    self.engine_picker = false;
                    self.new_session = None;
                    self.permission_picker = None;
                    self.permission_confirm_dont_ask = false;
                    self.notice = "Enlarge terminal to open chip picker".into();
                }
                true
            }
            Event::Key(key)
                if key.kind == KeyEventKind::Press || key.kind == KeyEventKind::Repeat =>
            {
                self.key(key)
            }
            Event::Mouse(mouse) => self.mouse(mouse),
            _ => false,
        };
        let after = (self.active_group, self.groups[self.active_group].active_id().unwrap_or("").to_owned());
        if before != after {
            self.input_drafts
                .insert(before, std::mem::take(&mut self.input));
            self.input = self.input_drafts.remove(&after).unwrap_or_default();
        }
        changed
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
        if self.stop_confirmation.is_some() { return self.stop_confirmation_key(key); }
        if self.new_session.is_some() { return self.new_session_key(key); }
        if self.model_picker.is_some() { return self.model_picker_key(key); }
        if self.permission_picker.is_some() { return self.permission_picker_key(key); }
        if self.engine_picker { return self.engine_picker_key(key); }
        if self.action_menu {
            return self.action_key(key);
        }
        if self.history_modal {
            return self.history_key(key);
        }
        if self.diff_modal {
            return self.diff_key(key);
        }
        if self.map_modal {
            let owner = self.groups[self.active_group]
                .active_id()
                .unwrap_or("")
                .to_owned();
            return match key.code {
                KeyCode::Esc | KeyCode::Char('m') if key.code == KeyCode::Esc || ctrl => {
                    self.map_modal = false;
                    true
                }
                KeyCode::Up => {
                    self.peer_map.move_selected(&owner, -1);
                    true
                }
                KeyCode::Down => {
                    self.peer_map.move_selected(&owner, 1);
                    true
                }
                KeyCode::Char('r' | 'R') => {
                    self.pending_peer_refresh = Some(owner);
                    true
                }
                _ => false,
            };
        }
        if key.code == KeyCode::Char('m') && ctrl {
            self.map_modal = true;
            self.peer_map.selected = 0;
            self.pending_peer_refresh = Some(
                self.groups[self.active_group]
                    .active_id()
                    .unwrap_or("")
                    .to_owned(),
            );
            return true;
        }
        if self.tool_modal {
            return self.tool_key(key);
        }
        if key.code == KeyCode::Char('t') && ctrl {
            self.tool_modal = true;
            self.tool_scroll = 0;
            self.tool_selected = self.active_tool_cards().len().saturating_sub(1);
            return true;
        }
        if key.code == KeyCode::Char('p') && ctrl {
            self.action_menu = true;
            self.action_selected = 0;
            self.drag = None;
            return true;
        }
        if key.code == KeyCode::Char('r') && ctrl {
            self.open_history();
            return true;
        }
        if key.code == KeyCode::Char('m') && alt { self.open_model_picker(); return true; }
        if key.code == KeyCode::Char('p') && alt { self.open_permission_picker(); return true; }
        if key.code == KeyCode::Char('e') && alt { self.open_engine_picker(); return true; }
        if key.code == KeyCode::Char('x') && alt { self.open_stop_confirmation(); return true; }
        if key.code == KeyCode::F(2) || (key.code == KeyCode::Char('g') && alt) {
            self.open_diff();
            return true;
        }
        if key.code == KeyCode::F(4) {
            if !self.diff_pane && self.layout(self.size).panes.is_none() {
                self.notice = "Enlarge terminal to open the diff pane".into();
            } else {
                self.diff_pane = !self.diff_pane;
                if self.diff_pane { self.load_diff(); }
            }
            return true;
        }
        if self.diff_pane {
            match key.code {
                KeyCode::F(5) => { self.load_diff(); return true; }
                KeyCode::PageUp if alt => { self.diff_scroll = self.diff_scroll.saturating_sub(10); return true; }
                KeyCode::PageDown if alt => { self.diff_scroll = self.diff_scroll.saturating_add(10); return true; }
                _ => {}
            }
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
                let target = self.groups[self.active_group]
                    .active_id()
                    .unwrap_or("")
                    .to_owned();
                if let Some(draft) = self.rejected_drafts.get_mut(&target).and_then(Vec::pop) {
                    let current = std::mem::replace(&mut self.input, draft);
                    if !current.is_empty() {
                        self.rejected_drafts
                            .entry(target)
                            .or_default()
                            .push(current);
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
                        if self.offline_ids.contains(id) {
                            self.notice = "Archived transcript is read-only".into();
                        } else if self.pending_prompts.len() < MAX_PENDING_PROMPTS {
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

    fn open_model_picker(&mut self) {
        if self.size.width > 0 && (self.size.width < 29 || self.size.height < 11) {
            self.notice = "Enlarge terminal to open model picker".into();
            return;
        }
        let Some(id) = self.groups[self.active_group].active_id().map(str::to_owned) else {
            self.notice = "Select a session to inspect its model".into();
            return;
        };
        if !self.model_capabilities.get(&id).copied().unwrap_or(false) {
            self.notice = "This session cannot change models".into();
            return;
        }
        self.model_picker = Some(ModelPicker { session_id: id.clone(), models: Vec::new(),
            selected: 0, note: "Loading this engine's model catalog…".into(),
            loading: true, catalog_pending: false });
        self.pending_model_queries.push(id);
    }

    fn open_permission_picker(&mut self) {
        if self.size.width > 0 && (self.size.width < 60 || self.size.height < 15) {
            self.notice = "Enlarge terminal to open permission picker".into();
            return;
        }
        let Some(id) = self.groups[self.active_group].active_id().map(str::to_owned) else {
            self.notice = "Select a session to inspect permissions".into();
            return;
        };
        if !self.permission_capabilities.get(&id).copied().unwrap_or(false) {
            self.notice = "This session cannot change permission modes".into();
            return;
        }
        let selected = self.permission_modes.get(&id).and_then(|mode| permission_index(mode)).unwrap_or(0);
        self.permission_picker = Some((id, selected));
        self.permission_confirm_dont_ask = false;
    }

    fn select_permission_mode(&mut self) {
        let Some((id, selected)) = self.permission_picker.as_ref() else { return; };
        let mode = PERMISSION_CHOICES[*selected].0;
        if mode == "dontAsk" && self.permission_modes.get(id).is_none_or(|current| current != mode)
            && !self.session_activity.get(id).is_some_and(|(busy, queued)| !busy && *queued == 0) {
            self.notice = "dontAsk requires an idle session with no queued prompts".into();
            return;
        }
        if mode == "dontAsk" && self.permission_modes.get(id).is_none_or(|current| current != mode)
            && !self.permission_confirm_dont_ask {
            self.permission_confirm_dont_ask = true;
            self.notice = "dontAsk denies unapproved calls silently; press Enter again to confirm".into();
            return;
        }
        self.pending_permission_changes.push((id.clone(), mode.to_owned()));
        self.notice = format!("Requesting permission mode · {mode}");
        self.permission_picker = None;
        self.permission_confirm_dont_ask = false;
    }

    fn permission_picker_key(&mut self, key: KeyEvent) -> bool {
        match key.code {
            KeyCode::Esc => { self.permission_picker = None; self.permission_confirm_dont_ask = false; }
            KeyCode::Up => { if let Some((_, selected)) = &mut self.permission_picker { *selected = selected.saturating_sub(1); self.permission_confirm_dont_ask = false; } }
            KeyCode::Down => { if let Some((_, selected)) = &mut self.permission_picker { *selected = (*selected + 1).min(PERMISSION_CHOICES.len() - 1); self.permission_confirm_dont_ask = false; } }
            KeyCode::Enter => self.select_permission_mode(),
            _ => return false,
        }
        true
    }

    fn model_picker_key(&mut self, key: KeyEvent) -> bool {
        let picker = self.model_picker.as_mut().unwrap();
        match key.code {
            KeyCode::Esc => self.model_picker = None,
            KeyCode::Up => picker.selected = picker.selected.saturating_sub(1),
            KeyCode::Down => picker.selected = (picker.selected + 1).min(picker.models.len().saturating_sub(1)),
            KeyCode::Char('r' | 'R') if !picker.loading => {
                picker.loading = true;
                picker.catalog_pending = false;
                picker.note = "Refreshing this engine's model catalog…".into();
                picker.models.clear();
                self.pending_model_queries.push(picker.session_id.clone());
            }
            KeyCode::Enter if !picker.loading => {
                if let Some(model) = picker.models.get(picker.selected) {
                    self.pending_model_changes.push((picker.session_id.clone(), model.clone()));
                    self.notice = format!("Requesting model · {}", model);
                    self.model_picker = None;
                }
            }
            _ => return false,
        }
        true
    }

    fn open_engine_picker(&mut self) {
        if self.size.width > 0 && (self.size.width < 29 || self.size.height < 11) {
            self.notice = "Enlarge terminal to open engine picker".into();
            return;
        }
        let engine = self.groups[self.active_group].active_id()
            .and_then(|id| self.session_identity.get(id)).and_then(|identity| identity.0.as_deref());
        self.engine_selected = engine.and_then(|engine| ENGINE_CHOICES.iter().position(|name| *name == engine)).unwrap_or(0);
        self.engine_picker = true;
    }

    fn engine_picker_key(&mut self, key: KeyEvent) -> bool {
        match key.code {
            KeyCode::Esc => self.engine_picker = false,
            KeyCode::Up => self.engine_selected = self.engine_selected.saturating_sub(1),
            KeyCode::Down => self.engine_selected = (self.engine_selected + 1).min(ENGINE_CHOICES.len() - 1),
            KeyCode::Enter => {
                self.select_new_engine();
            }
            _ => return false,
        }
        true
    }

    fn select_new_engine(&mut self) {
        let engine = match self.engine_selected {
            0 => launch::Engine::Codex,
            1 => launch::Engine::Claude,
            2 => launch::Engine::DeepSeek,
            _ => launch::Engine::Glm,
        };
        self.engine_picker = false;
        self.new_session = Some(NewSession { engine, model: String::new(), prompt: String::new(), field: 0 });
    }

    fn new_session_key(&mut self, key: KeyEvent) -> bool {
        let form = self.new_session.as_mut().unwrap();
        match key.code {
            KeyCode::Esc => self.new_session = None,
            KeyCode::Tab | KeyCode::Down => form.field = (form.field + 1) % 2,
            KeyCode::BackTab | KeyCode::Up => form.field = (form.field + 1) % 2,
            KeyCode::Backspace => {
                if form.field == 0 { form.model.pop(); } else { form.prompt.pop(); }
            }
            KeyCode::Char(c) if !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                && !c.is_control() => {
                let target = if form.field == 0 { &mut form.model } else { &mut form.prompt };
                let limit = if form.field == 0 { 128 } else { MAX_INPUT_BYTES };
                if target.len() + c.len_utf8() <= limit { target.push(c); }
            }
            KeyCode::Enter if form.field == 0 => form.field = 1,
            KeyCode::Enter => {
                if self.launching {
                    self.notice = "Session launch already in progress".into();
                    return true;
                }
                let form = self.new_session.take().unwrap();
                let mut options = launch::LaunchOptions { engine: form.engine, ..Default::default() };
                if !form.model.trim().is_empty() { options.model = Some(form.model.trim().to_owned()); }
                if form.engine == launch::Engine::Claude {
                    options.claude_script = std::env::var_os("DOXA_CLAUDE_SCRIPT").map(PathBuf::from);
                }
                let prompt = if form.prompt.trim().is_empty() { None } else { Some(form.prompt) };
                self.pending_launches.push((options, prompt, self.active_group));
                self.launching = true;
                self.notice = format!("Starting {} session…", ENGINE_CHOICES[self.engine_selected]);
            }
            _ => return false,
        }
        true
    }

    fn history_matches(&self) -> Vec<usize> {
        let query = self.history_query.to_lowercase();
        self.sessions.iter().enumerate().filter_map(|(index, session)| {
            let mut start = session.transcript.len().saturating_sub(16 * 1024);
            while !session.transcript.is_char_boundary(start) { start += 1; }
            if query.is_empty() || session.title.to_lowercase().contains(&query)
                || session.id.to_lowercase().contains(&query)
                || session.transcript[start..].to_lowercase().contains(&query) {
                Some(index)
            } else { None }
        }).take(128).collect()
    }

    fn history_fits(&self) -> bool {
        self.size.width >= 28 && self.size.height >= 12
    }

    fn open_history(&mut self) {
        if !self.history_fits() {
            self.notice = "Enlarge terminal to open session history".into();
            return;
        }
        self.history_modal = true;
        self.history_query.clear();
        self.history_selected = 0;
        if self.history_pending.is_none() {
            let (tx, rx) = mpsc::sync_channel(1);
            self.history_pending = Some(rx);
            std::thread::spawn(move || { let _ = tx.send(history::discover()); });
        }
    }

    fn poll_history(&mut self) -> bool {
        let Some(receiver) = &self.history_pending else { return false; };
        let found = match receiver.try_recv() {
            Ok(found) => found,
            Err(TryRecvError::Empty) => return false,
            Err(TryRecvError::Disconnected) => { self.history_pending = None; return false; }
        };
        self.history_pending = None;
        let mut changed = false;
        for entry in found {
            if self.sessions.iter().any(|session| session.id == entry.id) { continue; }
            self.offline_ids.insert(entry.id.clone());
            self.sessions.push(Session { id: entry.id.clone(), title: entry.id,
                collection: safe_label(&entry.project), transcript: transcript_tail(&entry.markdown).to_owned(),
                status: "Archived · read-only".into() });
            changed = true;
        }
        changed
    }

    fn history_key(&mut self, key: KeyEvent) -> bool {
        match key.code {
            KeyCode::Esc | KeyCode::Char('r') if key.code == KeyCode::Esc || key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.history_modal = false;
            }
            KeyCode::Up => self.history_selected = self.history_selected.saturating_sub(1),
            KeyCode::Down => self.history_selected = (self.history_selected + 1).min(self.history_matches().len().saturating_sub(1)),
            KeyCode::Backspace => { self.history_query.pop(); self.history_selected = 0; }
            KeyCode::Char(c) if !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) => {
                if self.history_query.len() + c.len_utf8() <= 200 { self.history_query.push(c); self.history_selected = 0; }
            }
            KeyCode::Enter => {
                if let Some(&index) = self.history_matches().get(self.history_selected) {
                    let id = self.sessions[index].id.clone();
                    let tabs = &mut self.groups[self.active_group];
                    if let Some(index) = tabs.tabs.iter().position(|tab| tab == &id) { tabs.active = index; }
                    else { tabs.tabs.push(id); tabs.active = tabs.tabs.len() - 1; }
                    tabs.scroll = 0;
                    self.focus = Focus::Transcript;
                    self.history_modal = false;
                }
            }
            _ => return false,
        }
        true
    }

    fn open_diff(&mut self) {
        if self.diff_modal { self.diff_modal = false; return; }
        self.diff_modal = true;
        self.load_diff();
    }

    fn load_diff(&mut self) {
        self.diff_scroll = 0;
        self.diff_pending = None;
        let Some(id) = self.groups[self.active_group].active_id().map(str::to_owned) else {
            self.diff_target = None;
            self.diff_text = "Select a session to inspect its worktree.".into();
            return;
        };
        self.diff_target = Some(id.clone());
        let Some(cwd) = self.session_cwds.get(&id).cloned() else {
            self.diff_text = "This session did not provide a worktree directory.".into();
            return;
        };
        self.diff_text = "Loading worktree diff…".into();
        let (tx, rx) = mpsc::sync_channel(1);
        self.diff_pending = Some(rx);
        std::thread::spawn(move || { let _ = tx.send((id, diff_view::read(&cwd))); });
    }

    fn poll_diff(&mut self) -> bool {
        if self.diff_pane && self.diff_target.as_deref() != self.groups[self.active_group].active_id() {
            self.load_diff();
            return true;
        }
        let Some(receiver) = &self.diff_pending else { return false; };
        match receiver.try_recv() {
            Ok((id, snapshot)) => {
                self.diff_pending = None;
                if self.groups[self.active_group].active_id() == Some(id.as_str()) {
                    self.diff_text = markdown::sanitize(&snapshot.text);
                    self.diff_scroll = 0;
                    return true;
                }
                self.diff_text = "The active session changed while the diff loaded. Press R to refresh it.".into();
                self.diff_scroll = 0;
                true
            }
            Err(TryRecvError::Disconnected) => { self.diff_pending = None; self.diff_text = "Diff worker unavailable.".into(); true }
            Err(TryRecvError::Empty) => false,
        }
    }

    fn diff_key(&mut self, key: KeyEvent) -> bool {
        match key.code {
            KeyCode::Esc | KeyCode::F(2) => self.diff_modal = false,
            KeyCode::Char('g') if key.modifiers.contains(KeyModifiers::ALT) => self.diff_modal = false,
            KeyCode::Char('r' | 'R') => { self.diff_modal = false; self.open_diff(); },
            KeyCode::Up => self.diff_scroll = self.diff_scroll.saturating_sub(1),
            KeyCode::Down => self.diff_scroll = self.diff_scroll.saturating_add(1),
            KeyCode::PageUp => self.diff_scroll = self.diff_scroll.saturating_sub(10),
            KeyCode::PageDown => self.diff_scroll = self.diff_scroll.saturating_add(10),
            _ => return false,
        }
        true
    }

    fn active_tool_cards(&self) -> &[tool_cards::ToolCard] {
        self.groups[self.active_group]
            .active_id()
            .map(|id| self.tool_cards.for_session(id))
            .unwrap_or(&[])
    }

    fn tool_key(&mut self, key: KeyEvent) -> bool {
        let count = self.active_tool_cards().len();
        match key.code {
            KeyCode::Esc | KeyCode::Char('t')
                if key.code == KeyCode::Esc || key.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                self.tool_modal = false;
            }
            KeyCode::Up => {
                self.tool_selected = self.tool_selected.saturating_sub(1);
                self.tool_scroll = 0;
            }
            KeyCode::Down => {
                self.tool_selected = (self.tool_selected + 1).min(count.saturating_sub(1));
                self.tool_scroll = 0;
            }
            KeyCode::PageUp => self.tool_scroll = self.tool_scroll.saturating_sub(10),
            KeyCode::PageDown => self.tool_scroll = self.tool_scroll.saturating_add(10),
            _ => return false,
        }
        true
    }

    fn action_key(&mut self, key: KeyEvent) -> bool {
        match key.code {
            KeyCode::Esc | KeyCode::Char('p')
                if key.code == KeyCode::Esc || key.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                self.action_menu = false;
            }
            KeyCode::Up => {
                self.action_selected = (self.action_selected + ACTIONS.len() - 1) % ACTIONS.len();
            }
            KeyCode::Down => {
                self.action_selected = (self.action_selected + 1) % ACTIONS.len();
            }
            KeyCode::Enter => {
                self.action_menu = false;
                match self.action_selected {
                    0 => {
                        self.map_modal = true;
                        self.peer_map.selected = 0;
                        self.pending_peer_refresh = Some(
                            self.groups[self.active_group]
                                .active_id()
                                .unwrap_or("")
                                .to_owned(),
                        );
                    }
                    1 => {
                        self.tool_modal = true;
                        self.tool_scroll = 0;
                        self.tool_selected = self.active_tool_cards().len().saturating_sub(1);
                    }
                    2 => self.open_selected(),
                    3 => self.previous_tab(),
                    4 => self.next_tab(),
                    5 => {
                        self.active_group = 1 - self.active_group;
                        self.focus = Focus::Prompt;
                    }
                    6 => self.open_history(),
                    7 => self.open_diff(),
                    8 => self.open_engine_picker(),
                    9 => self.open_model_picker(),
                    10 => self.open_permission_picker(),
                    11 => self.open_stop_confirmation(),
                    _ => unreachable!("fixed action list"),
                }
            }
            _ => {}
        }
        true
    }

    fn open_stop_confirmation(&mut self) {
        if self.size.width > 0 && (self.size.width < 40 || self.size.height < 12) {
            self.notice = "Enlarge terminal to confirm session stop".into();
            return;
        }
        let Some(id) = self.groups[self.active_group].active_id() else {
            self.notice = "Select a session to stop".into();
            return;
        };
        if self.offline_ids.contains(id) {
            self.notice = "This session is already stopped or archived".into();
            return;
        }
        self.stop_confirmation = Some(id.to_owned());
        self.drag = None;
    }

    fn stop_confirmation_key(&mut self, key: KeyEvent) -> bool {
        match key.code {
            KeyCode::Esc | KeyCode::Char('n' | 'N') => self.stop_confirmation = None,
            KeyCode::Char('y' | 'Y') if key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT => {
                let id = self.stop_confirmation.take().unwrap();
                self.pending_stops.push(id.clone());
                self.notice = format!("Requesting stop · {}", safe_label(&id));
            }
            _ => return false,
        }
        true
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
        if request.step + 1 == request.questions.len() {
            Some(serde_json::json!({"answers": request.answers}))
        } else {
            request.step += 1;
            request.selected = 1;
            request.scroll = 0;
            None
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

    fn layout(&self, area: Rect) -> PaneLayout {
        let outer = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Min(3), Constraint::Length(1)])
            .split(area);
        let min_body = if self.split == Split::Vertical {
            MIN_PANE_WIDTH * 2
        } else {
            MIN_PANE_WIDTH
        };
        let rail_width = if self.rail_visible && outer[0].width >= 70 {
            self.rail_width.clamp(
                MIN_RAIL_WIDTH,
                outer[0].width.saturating_sub(min_body).max(MIN_RAIL_WIDTH),
            )
        } else {
            0
        };
        let (rail, body) = if rail_width > 0 {
            let chunks = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Length(rail_width), Constraint::Min(1)])
                .split(outer[0]);
            (Some(chunks[0]), chunks[1])
        } else {
            (None, outer[0])
        };
        let min_ok = if self.split == Split::Vertical {
            body.width >= MIN_PANE_WIDTH * 2
        } else {
            body.height >= MIN_PANE_HEIGHT * 2
        };
        let panes = min_ok.then(|| {
            let desired = self.pane_rects(body, self.split_percent);
            let minimum = if self.split == Split::Vertical {
                MIN_PANE_WIDTH
            } else {
                MIN_PANE_HEIGHT
            };
            let size = |rect: Rect| {
                if self.split == Split::Vertical {
                    rect.width
                } else {
                    rect.height
                }
            };
            if size(desired[0]) >= minimum && size(desired[1]) >= minimum {
                desired
            } else {
                (0..=100)
                    .map(|percent| self.pane_rects(body, percent))
                    .filter(|pair| size(pair[0]) >= minimum && size(pair[1]) >= minimum)
                    .min_by_key(|pair| size(pair[0]).abs_diff(size(desired[0])))
                    .unwrap_or(desired)
            }
        });
        PaneLayout {
            outer: outer[0],
            rail,
            body,
            panes,
        }
    }

    fn pane_rects(&self, body: Rect, percent: u16) -> [Rect; 2] {
        let direction = if self.split == Split::Vertical {
            Direction::Horizontal
        } else {
            Direction::Vertical
        };
        let chunks = Layout::default()
            .direction(direction)
            .constraints([
                Constraint::Percentage(percent),
                Constraint::Percentage(100 - percent),
            ])
            .split(body);
        [chunks[0], chunks[1]]
    }

    fn mouse(&mut self, mouse: MouseEvent) -> bool {
        if self.active_request_index().is_none()
            && matches!(mouse.kind, MouseEventKind::Down(MouseButton::Left))
            && (self.engine_picker || self.new_session.is_some() || self.model_picker.is_some() || self.permission_picker.is_some()) {
            let width = self.size.width.saturating_sub(4).min(74);
            let height = self.size.height.saturating_sub(4).min(19);
            if width < 25 || height < 7 { return false; }
            let x = self.size.x + (self.size.width - width) / 2;
            let y = self.size.y + (self.size.height - height) / 2;
            if mouse.column < x || mouse.column >= x + width || mouse.row < y || mouse.row >= y + height {
                self.engine_picker = false;
                self.new_session = None;
                self.model_picker = None;
                self.permission_picker = None;
                self.permission_confirm_dont_ask = false;
                return true;
            }
            if self.engine_picker {
                if mouse.row < y + 4 { return true; }
                let row = usize::from(mouse.row.saturating_sub(y + 4));
                if row < ENGINE_CHOICES.len() {
                    self.engine_selected = row;
                    self.select_new_engine();
                }
                return true;
            }
            if self.new_session.is_some() { return true; }
            if let Some((_, selected)) = &mut self.permission_picker {
                if mouse.row >= y + 4 {
                    let row = usize::from(mouse.row - (y + 4));
                    if row < PERMISSION_CHOICES.len() {
                        *selected = row;
                        self.permission_confirm_dont_ask = false;
                        if PERMISSION_CHOICES[row].0 == "dontAsk" {
                            self.notice = "dontAsk denies unapproved calls silently; press Enter twice to confirm".into();
                        } else {
                            self.select_permission_mode();
                        }
                    }
                }
                return true;
            }
            let picker = self.model_picker.as_mut().unwrap();
            let visible = usize::from(height.saturating_sub(5));
            let start = picker.selected.saturating_sub(visible.saturating_sub(1));
            let row = start + usize::from(mouse.row.saturating_sub(y + 3));
            if mouse.row >= y + 3 && row < picker.models.len() && !picker.loading {
                self.pending_model_changes.push((picker.session_id.clone(), picker.models[row].clone()));
                self.notice = format!("Requesting model · {}", picker.models[row]);
                self.model_picker = None;
            }
            return true;
        }
        if self.active_request_index().is_some()
            || self.tool_modal
            || self.map_modal
            || self.action_menu
            || self.history_modal
            || self.diff_modal
            || self.model_picker.is_some()
            || self.permission_picker.is_some()
            || self.engine_picker
            || self.stop_confirmation.is_some()
            || self.new_session.is_some()
        {
            self.drag = None;
            return false;
        }
        if self.diff_pane {
            if let Some(panes) = self.layout(self.size).panes {
                let pane = panes[1 - self.active_group];
                if mouse.column > pane.x && mouse.column < pane.right().saturating_sub(1)
                    && mouse.row > pane.y && mouse.row < pane.bottom().saturating_sub(1) {
                    match mouse.kind {
                        MouseEventKind::ScrollUp => self.diff_scroll = self.diff_scroll.saturating_sub(3),
                        MouseEventKind::ScrollDown => self.diff_scroll = self.diff_scroll.saturating_add(3),
                        MouseEventKind::Down(MouseButton::Left) => {},
                        _ => return false,
                    }
                    return true;
                }
            }
        }
        match mouse.kind {
            MouseEventKind::Down(MouseButton::Left) => {
                self.drag = None;
                if self.size.width < 20 || self.size.height < 5 {
                    return false;
                }
                let layout = self.layout(self.size);
                let in_outer = mouse.row >= layout.outer.y && mouse.row < layout.outer.bottom();
                if in_outer
                    && layout.rail.is_some_and(|rail| {
                        mouse.column == rail.right().saturating_sub(1)
                            || mouse.column == layout.body.x
                    })
                {
                    self.drag = Some(DragTarget::Rail);
                } else if let Some([first, second]) = layout.panes {
                    let on_divider = if self.split == Split::Vertical {
                        mouse.row >= layout.body.y
                            && mouse.row < layout.body.bottom()
                            && (mouse.column == first.right().saturating_sub(1)
                                || mouse.column == second.x)
                    } else {
                        mouse.column >= layout.body.x
                            && mouse.column < layout.body.right()
                            && (mouse.row == first.bottom().saturating_sub(1)
                                || mouse.row == second.y)
                    };
                    if on_divider {
                        self.drag = Some(DragTarget::Pane(self.split));
                    }
                }
                if self.drag.is_none() {
                        let pane_hits = layout.panes.map(|panes| vec![(0, panes[0]), (1, panes[1])])
                            .unwrap_or_else(|| vec![(self.active_group, layout.body)]);
                        for (index, pane) in pane_hits.iter().copied() {
                            if mouse.column >= pane.x && mouse.column < pane.right()
                                && mouse.row >= pane.y && mouse.row < pane.bottom() {
                                if mouse.row == pane.bottom().saturating_sub(1) {
                                    self.active_group = index;
                                    let status_width = self.groups[index].active_id()
                                        .and_then(|id| self.sessions.iter().find(|session| session.id == id))
                                        .map(|session| session.status.width() + 3).unwrap_or(13);
                                    let engine_width = self.groups[index].active_id()
                                        .and_then(|id| self.session_identity.get(id))
                                        .and_then(|identity| identity.0.as_deref())
                                        .map(|engine| engine.width() + 3).unwrap_or(0);
                                    let model_width = self.groups[index].active_id()
                                        .and_then(|id| self.session_identity.get(id))
                                        .and_then(|identity| identity.1.as_deref())
                                        .map(|model| model.width() + 3).unwrap_or(0);
                                    let permission_width = self.groups[index].active_id()
                                        .and_then(|id| self.permission_modes.get(id))
                                        .map(|mode| mode.width() + 3).unwrap_or(0);
                                    let relative = usize::from(mouse.column.saturating_sub(pane.x));
                                    if relative >= status_width && relative < status_width + engine_width {
                                        self.open_engine_picker();
                                        return true;
                                    }
                                    if model_width > 0 && relative >= status_width + engine_width + 1
                                        && relative < status_width + engine_width + 1 + model_width {
                                        self.open_model_picker();
                                        return true;
                                    }
                                    let permission_start = status_width + engine_width + 1 + model_width + 1;
                                    if permission_width > 0 && relative >= permission_start
                                        && relative < permission_start + permission_width {
                                        self.open_permission_picker();
                                        return true;
                                    }
                                }
                                self.active_group = index;
                                self.focus = if mouse.row >= pane.bottom().saturating_sub(4) {
                                    Focus::Prompt
                                } else {
                                    Focus::Transcript
                                };
                                return true;
                            }
                        }
                }
                self.drag.is_some()
            }
            MouseEventKind::Drag(MouseButton::Left) => {
                let Some(target) = self.drag else {
                    return false;
                };
                let layout = self.layout(self.size);
                match target {
                    DragTarget::Rail if layout.rail.is_some() => {
                        let min_body = if self.split == Split::Vertical {
                            MIN_PANE_WIDTH * 2
                        } else {
                            MIN_PANE_WIDTH
                        };
                        let max = layout.outer.width.saturating_sub(min_body);
                        self.rail_width = mouse
                            .column
                            .saturating_sub(layout.outer.x)
                            .clamp(MIN_RAIL_WIDTH, max.max(MIN_RAIL_WIDTH));
                    }
                    DragTarget::Pane(split) if split == self.split && layout.panes.is_some() => {
                        let (axis, length, minimum) = if split == Split::Vertical {
                            (
                                mouse.column.saturating_sub(layout.body.x),
                                layout.body.width,
                                MIN_PANE_WIDTH,
                            )
                        } else {
                            (
                                mouse.row.saturating_sub(layout.body.y),
                                layout.body.height,
                                MIN_PANE_HEIGHT,
                            )
                        };
                        let wanted = axis.clamp(minimum, length.saturating_sub(minimum));
                        // Match ratatui's percentage rounding while keeping both panes usable.
                        self.split_percent = (0..=100)
                            .filter(|&percent| {
                                let pair = self.pane_rects(layout.body, percent);
                                let first = if split == Split::Vertical {
                                    pair[0].width
                                } else {
                                    pair[0].height
                                };
                                let second = if split == Split::Vertical {
                                    pair[1].width
                                } else {
                                    pair[1].height
                                };
                                first >= minimum && second >= minimum
                            })
                            .min_by_key(|&percent| {
                                let pair = self.pane_rects(layout.body, percent);
                                let first = if split == Split::Vertical {
                                    pair[0].width
                                } else {
                                    pair[0].height
                                };
                                first.abs_diff(wanted)
                            })
                            .unwrap_or(self.split_percent);
                    }
                    _ => self.drag = None,
                }
                true
            }
            MouseEventKind::Up(MouseButton::Left) => self.drag.take().is_some(),
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
        frame.render_widget(Block::default().style(Style::default().bg(theme::BASE).fg(theme::TEXT)), area);
        if area.width < 20 || area.height < 5 {
            frame.render_widget(Paragraph::new("DOXA · enlarge terminal"), area);
            return;
        }
        let layout = self.layout(area);
        if let Some(rail) = layout.rail {
            self.draw_rail(frame, rail);
        }
        if let Some(panes) = layout.panes {
            if self.diff_pane {
                self.draw_group(frame, panes[self.active_group], self.active_group);
                self.draw_diff_pane(frame, panes[1 - self.active_group]);
            } else {
                self.draw_group(frame, panes[0], 0);
                self.draw_group(frame, panes[1], 1);
            }
        } else {
            self.draw_group(frame, layout.body, self.active_group);
        }
        frame.render_widget(
            Paragraph::new(format!(
                "{}  |  Ctrl+P actions · Alt+X stop · Alt+E engine · Alt+M model · Alt+P permissions · Ctrl+R history · F2 diff · F4 diff pane · F3 rail · Shift+Tab pane · Ctrl+T tools · Ctrl+M peers · Alt+H/V split · Alt+arrows/drag resize · Ctrl+Q quit",
                self.notice
            )).style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            Rect::new(area.x, area.bottom().saturating_sub(1), area.width, 1),
        );
        self.draw_tool_cards(frame, area);
        if self.map_modal {
            self.peer_map.render(
                frame,
                area,
                self.groups[self.active_group].active_id().unwrap_or(""),
            );
        }
        self.draw_actions(frame, area);
        self.draw_history(frame, area);
        self.draw_diff(frame, area);
        self.draw_chip_picker(frame, area);
        self.draw_stop_confirmation(frame, area);
        self.draw_request(frame, area);
    }

    fn draw_stop_confirmation(&self, frame: &mut Frame, area: Rect) {
        let Some(id) = &self.stop_confirmation else { return; };
        let width = area.width.saturating_sub(4).min(78);
        let height = area.height.saturating_sub(4).min(12);
        if width < 36 || height < 8 { return; }
        let modal = Rect::new(area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2, width, height);
        let lines = vec![Line::from(" Stop and finalize this session?"),
            Line::from(""), Line::from(format!(" Session ID: {id}")), Line::from(""),
            Line::from(" The daemon will finish its shutdown work. This tab and draft remain visible."),
            Line::from(""), Line::from(" Press Y to stop · Esc or N to cancel")];
        frame.render_widget(Clear, modal);
        frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false })
            .block(Block::default().title(" Stop active session ").borders(Borders::ALL)
                .border_style(Style::default().fg(theme::ERROR)))
            .style(Style::default().fg(theme::TEXT).bg(theme::RAISED)), modal);
    }

    fn draw_chip_picker(&self, frame: &mut Frame, area: Rect) {
        if !self.engine_picker && self.new_session.is_none() && self.model_picker.is_none() && self.permission_picker.is_none() { return; }
        let width = area.width.saturating_sub(4).min(74);
        let height = area.height.saturating_sub(4).min(19);
        if width < 25 || height < 7 { return; }
        let modal = Rect::new(area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2, width, height);
        let mut lines = Vec::new();
        let title;
        if self.engine_picker {
            title = " New session · choose engine · Enter continue · Esc close ";
            lines.push(Line::from(" Select an engine for a new session:"));
            lines.push(Line::from(" Model and first prompt follow."));
            lines.push(Line::from(""));
            for (index, engine) in ENGINE_CHOICES.iter().enumerate() {
                lines.push(Line::styled(format!(" {} {}", if index == self.engine_selected { '›' } else { ' ' }, engine),
                    Style::default().fg(if index == self.engine_selected { theme::ACCENT } else { theme::SECONDARY })));
            }
        } else if let Some(form) = &self.new_session {
            title = " New session · Tab field · Enter continue/start · Esc close ";
            let name = match form.engine { launch::Engine::Codex => "codex", launch::Engine::Claude => "claude",
                launch::Engine::DeepSeek => "deepseek", launch::Engine::Glm => "glm", launch::Engine::Fixture => "fixture" };
            lines.push(Line::from(format!(" Engine: {name}")));
            lines.push(Line::from(" Blank model uses configured engine default."));
            lines.push(Line::from(""));
            lines.push(Line::styled(format!(" {} Model: {}", if form.field == 0 { '›' } else { ' ' }, safe_label(&form.model)),
                Style::default().fg(if form.field == 0 { theme::ACCENT } else { theme::SECONDARY })));
            lines.push(Line::styled(format!(" {} First prompt: {}", if form.field == 1 { '›' } else { ' ' }, safe_label(&form.prompt)),
                Style::default().fg(if form.field == 1 { theme::ACCENT } else { theme::SECONDARY })));
            if form.engine == launch::Engine::Claude {
                lines.push(Line::from(" Claude requires DOXA_CLAUDE_SCRIPT absolute path."));
            }
        } else if let Some((id, selected)) = &self.permission_picker {
            title = " Claude permissions · this session · Enter select · Esc close ";
            lines.push(Line::from(" Changes how Claude handles tool permission requests."));
            lines.push(Line::from(if self.permission_confirm_dont_ask {
                " dontAsk silently denies unapproved calls. Enter again to confirm."
            } else {
                " Current mode marked with ●; dontAsk requires confirmation."
            }));
            lines.push(Line::from(""));
            for (index, (mode, description)) in PERMISSION_CHOICES.iter().enumerate() {
                let current = self.permission_modes.get(id).is_some_and(|current| current == mode);
                lines.push(Line::styled(format!(" {} {} {} · {}", if index == *selected { '›' } else { ' ' },
                    if current { '●' } else { ' ' }, mode, description),
                    Style::default().fg(if index == *selected { theme::ACCENT } else { theme::SECONDARY })));
            }
        } else {
            title = " Model · this session · R retry · Enter select · Esc close ";
            let picker = self.model_picker.as_ref().unwrap();
            lines.push(Line::from(format!(" {}", picker.note)));
            lines.push(Line::from(""));
            if picker.catalog_pending {
                lines.push(Line::from(" Catalog probe in progress · press R to retry"));
            } else if !picker.loading && picker.models.is_empty() {
                lines.push(Line::from(" No verified models available for this session"));
            }
            let visible = usize::from(height.saturating_sub(5));
            let start = picker.selected.saturating_sub(visible.saturating_sub(1));
            for (index, model) in picker.models.iter().enumerate().skip(start).take(visible) {
                lines.push(Line::styled(format!(" {} {}", if index == picker.selected { '›' } else { ' ' }, model),
                    Style::default().fg(if index == picker.selected { theme::ACCENT } else { theme::SECONDARY })));
            }
        }
        frame.render_widget(Clear, modal);
        frame.render_widget(Paragraph::new(lines).block(Block::default().title(title)
            .borders(Borders::ALL).border_style(Style::default().fg(theme::BORDER)))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)), modal);
    }

    fn draw_history(&self, frame: &mut Frame, area: Rect) {
        if !self.history_modal { return; }
        let width = area.width.saturating_sub(4).min(88);
        let height = area.height.saturating_sub(4).min(24);
        if width < 24 || height < 8 { return; }
        let modal = Rect::new(area.x + (area.width - width) / 2, area.y + (area.height - height) / 2, width, height);
        let matches = self.history_matches();
        let mut lines = vec![Line::from(format!(" Search: {}", safe_label(&self.history_query))),
            Line::from(" Attached and archived sessions · read-only transcript picker"), Line::from("")];
        if matches.is_empty() { lines.push(Line::from(if self.history_pending.is_some() { " Finding saved transcripts…" } else { " No matching sessions" })); }
        let visible = usize::from(height.saturating_sub(7)).max(1);
        let start = self.history_selected.saturating_sub(visible.saturating_sub(1));
        for (position, &index) in matches.iter().enumerate().skip(start).take(visible) {
            let session = &self.sessions[index];
            let label = format!(" {} {} · {}{}", if position == self.history_selected { '›' } else { ' ' },
                safe_label(&session.title), safe_label(&session.id),
                if self.offline_ids.contains(&session.id) { " · archived" } else { "" });
            let style = if position == self.history_selected { Style::default().fg(theme::ACCENT).bg(theme::HIGHLIGHT) }
                else { Style::default().fg(theme::SECONDARY) };
            lines.push(Line::styled(label, style));
        }
        if let Some(&index) = matches.get(self.history_selected) {
            let preview: String = self.sessions[index].transcript.chars().rev().take(240).collect::<String>().chars().rev().collect();
            lines.push(Line::from(""));
            lines.push(Line::from(format!(" Preview: {}", safe_label(&preview))));
        }
        frame.render_widget(Clear, modal);
        frame.render_widget(Paragraph::new(lines).block(Block::default()
            .title(" Session history · type to filter · Enter open · Esc close ")
            .borders(Borders::ALL).border_style(Style::default().fg(theme::BORDER))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED))), modal);
    }

    fn draw_diff(&self, frame: &mut Frame, area: Rect) {
        if !self.diff_modal { return; }
        let width = area.width.saturating_sub(4).min(120);
        let height = area.height.saturating_sub(4).min(36);
        if width < 24 || height < 8 { return; }
        let modal = Rect::new(area.x + (area.width - width) / 2, area.y + (area.height - height) / 2, width, height);
        frame.render_widget(Clear, modal);
        let rows: Vec<Line> = self.diff_text.lines()
            .skip(usize::from(self.diff_scroll))
            .take(usize::from(height.saturating_sub(2)))
            .map(|line| {
            let color = if line.starts_with('+') && !line.starts_with("+++") { theme::SUCCESS }
                else if line.starts_with('-') && !line.starts_with("---") { theme::ERROR }
                else if line.starts_with("@@") { theme::ACCENT } else { theme::SECONDARY };
            Line::styled(line.to_owned(), Style::default().fg(color))
        }).collect();
        frame.render_widget(Paragraph::new(rows)
            .block(Block::default().title(" Worktree diff · ↑/↓ scroll · R refresh · F2/Esc close ")
                .borders(Borders::ALL).border_style(Style::default().fg(theme::BORDER))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED))), modal);
    }

    fn draw_diff_pane(&self, frame: &mut Frame, area: Rect) {
        let rows: Vec<Line> = self.diff_text.lines()
            .skip(usize::from(self.diff_scroll))
            .take(usize::from(area.height.saturating_sub(2)))
            .map(|line| {
                let color = if line.starts_with('+') && !line.starts_with("+++") { theme::SUCCESS }
                    else if line.starts_with('-') && !line.starts_with("---") { theme::ERROR }
                    else if line.starts_with("@@") { theme::ACCENT } else { theme::SECONDARY };
                Line::styled(line.to_owned(), Style::default().fg(color))
            }).collect();
        frame.render_widget(Paragraph::new(rows)
            .block(Block::default().title(" Worktree diff · F5 refresh · Alt+PgUp/PgDn scroll · F4 close ")
                .borders(Borders::ALL).border_style(Style::default().fg(theme::BORDER)))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)), area);
    }

    fn draw_actions(&self, frame: &mut Frame, area: Rect) {
        if !self.action_menu {
            return;
        }
        let width = area.width.saturating_sub(2).min(58);
        let height = area.height.saturating_sub(2).min(10);
        if width < 18 || height < 3 {
            return;
        }
        let modal = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        let visible = usize::from(height.saturating_sub(2));
        let start = self
            .action_selected
            .saturating_sub(visible.saturating_sub(1));
        let rows: Vec<Line> = ACTIONS
            .iter()
            .enumerate()
            .skip(start)
            .take(visible)
            .map(|(index, (label, hint))| {
                let style = if index == self.action_selected {
                    Style::default()
                        .fg(theme::ACCENT)
                        .bg(theme::HIGHLIGHT)
                        .add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(theme::SECONDARY)
                };
                Line::from(format!(
                    " {} {:<28} {}",
                    if index == self.action_selected {
                        '›'
                    } else {
                        ' '
                    },
                    label,
                    hint
                ))
                .style(style)
            })
            .collect();
        frame.render_widget(Clear, modal);
        frame.render_widget(
            Paragraph::new(rows).block(
                Block::default()
                    .title(" Actions · ↑/↓ choose · Enter open · Esc close ")
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(theme::BORDER))
                    .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            ),
            modal,
        );
    }

    fn draw_tool_cards(&self, frame: &mut Frame, area: Rect) {
        if !self.tool_modal {
            return;
        }
        let width = area.width.saturating_sub(4).min(100);
        let height = area.height.saturating_sub(4).min(28);
        if width < 24 || height < 7 {
            return;
        }
        let modal = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        let cards = self.active_tool_cards();
        let mut body = String::new();
        if cards.is_empty() {
            body.push_str("No tool activity in this session.");
        } else {
            let selected = self.tool_selected.min(cards.len() - 1);
            let start = selected
                .saturating_sub(7)
                .min(cards.len().saturating_sub(8));
            for (index, card) in cards.iter().enumerate().skip(start).take(8) {
                body.push_str(if index == selected { "▸ " } else { "  " });
                body.push_str(&format!("{} · {}\n", card.name, card.status()));
            }
            let card = &cards[selected];
            body.push_str("\nTool: ");
            body.push_str(&card.name);
            if let Some(parent) = &card.parent_id {
                body.push_str("\nParent: ");
                body.push_str(parent);
            }
            body.push_str("\nStatus: ");
            body.push_str(&card.status());
            body.push_str("\n\nInput:\n");
            body.push_str(card.input.as_deref().unwrap_or("(unavailable)"));
            body.push_str("\n\nResult:\n");
            body.push_str(card.result.as_deref().unwrap_or("(pending)"));
        }
        frame.render_widget(Clear, modal);
        frame.render_widget(
            Paragraph::new(body)
                .wrap(Wrap { trim: false })
                .scroll((self.tool_scroll, 0))
                .block(
                    Block::default()
                        .title(" Tool activity · ↑/↓ select · PgUp/PgDn scroll · Esc close ")
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::BORDER))
                    .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
                ),
            modal,
        );
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
                        .border_style(Style::default().fg(theme::ACCENT))
                        .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
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
                        .fg(theme::ACCENT)
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
            Paragraph::new(lines).style(Style::default().fg(theme::SECONDARY).bg(theme::RAIL)).block(
                Block::default()
                    .title(if self.focus == Focus::Rail {
                        " Sessions ● "
                    } else {
                        " Sessions "
                    })
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(theme::BORDER)),
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
                Constraint::Length(3),
                Constraint::Length(2),
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
                .fg(theme::ACCENT)
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
                .borders(Borders::ALL)
                .border_style(Style::default().fg(theme::BORDER)),
        );
        frame.render_widget(tabs.style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)), inner[0]);
        let content = session
            .map(|s| s.transcript.as_str())
            .unwrap_or("No session open. Select one in the rail and press Enter.");
        let lines = markdown::render(content, inner[1].width.saturating_sub(2));
        let (lines, scroll_from_top) =
            transcript_window(lines, inner[1].height, inner[1].y, group.scroll);
        frame.render_widget(
            Paragraph::new(lines)
                .style(Style::default().fg(theme::TEXT).bg(theme::BASE))
                .scroll((scroll_from_top, 0))
                .wrap(Wrap { trim: false })
                .block(Block::default().borders(Borders::LEFT | Borders::RIGHT)
                    .border_style(Style::default().fg(theme::BORDER))),
            inner[1],
        );
        let active = self.active_group == index;
        let draft = group.active_id().map(|id| {
            if active { self.input.as_str() } else { self.input_drafts.get(&(index, id.to_owned())).map(String::as_str).unwrap_or("") }
        }).unwrap_or("");
        frame.render_widget(
            Paragraph::new(format!("> {draft}"))
                .style(Style::default().fg(theme::TEXT).bg(theme::RAISED))
                .block(Block::default()
                    .title(if active && self.focus == Focus::Prompt { " Prompt ● " } else { " Prompt " })
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(if active && self.focus == Focus::Prompt { theme::ACCENT } else { theme::BORDER }))),
            inner[2],
        );
        let status = session.map(|s| s.status.as_str()).unwrap_or("No session");
        let identity = group.active_id().and_then(|id| self.session_identity.get(id));
        let engine = identity.and_then(|pair| pair.0.as_deref());
        let model = identity.and_then(|pair| pair.1.as_deref());
        let mut status_spans = vec![Span::styled(
            format!(" {}  ", status),
            Style::default().fg(theme::SECONDARY),
        )];
        if let Some(engine) = engine {
            status_spans.push(Span::styled(
                format!(" {} ▾", engine),
                Style::default().fg(theme::ACCENT).bg(theme::HIGHLIGHT),
            ));
        }
        if let Some(model) = model {
            status_spans.push(Span::raw(" "));
            status_spans.push(Span::styled(
                format!(" {} ▾", model),
                Style::default().fg(theme::TEXT).bg(theme::HIGHLIGHT),
            ));
        }
        if let Some(mode) = group.active_id().and_then(|id| self.permission_modes.get(id)) {
            status_spans.push(Span::raw(" "));
            status_spans.push(Span::styled(format!(" {} ▾", mode),
                Style::default().fg(theme::TEXT).bg(theme::HIGHLIGHT)));
        }
        frame.render_widget(
            Paragraph::new(Line::from(status_spans)).style(Style::default().bg(theme::RAISED)),
            Rect { height: 1, ..inner[3] },
        );
        let telemetry = group.active_id().and_then(|id| self.session_telemetry.get(id));
        let telemetry_line = telemetry.map(SessionTelemetry::line)
            .unwrap_or_else(|| SessionTelemetry::default().line());
        frame.render_widget(
            Paragraph::new(telemetry_line).style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            Rect { y: inner[3].y.saturating_add(1), height: 1, ..inner[3] },
        );
    }
}

fn transcript_window(
    lines: Vec<Line<'static>>,
    viewport: u16,
    origin_y: u16,
    scroll: usize,
) -> (Vec<Line<'static>>, u16) {
    // Paragraph's internal `area.height + scroll.y` and its buffer row
    // `area.top() + y` are u16. Reserve the actual height and screen origin
    // so both calculations fit while moving a bounded window through lines.
    let viewport = usize::from(viewport);
    let max_scroll = lines.len().saturating_sub(viewport);
    let top = max_scroll.saturating_sub(scroll.min(max_scroll));
    let max_local_scroll = usize::from(u16::MAX)
        .saturating_sub(viewport)
        .saturating_sub(usize::from(origin_y));
    let start = top.saturating_sub(max_local_scroll);
    let keep = max_local_scroll.saturating_add(viewport);
    let window = lines.into_iter().skip(start).take(keep).collect();
    (window, (top - start) as u16)
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
    run_loop(receiver, None, None)
}

/// Connect the UI to a transport reader and writer without blocking input.
/// Prompt tuples contain the target session id and submitted text.
pub fn run_with_channels(
    frames: Receiver<serde_json::Value>,
    prompts: SyncSender<crate::bridge::WorkerCommand>,
) -> io::Result<()> {
    run_loop(frames, Some(prompts), None)
}

/// Drive a multi-session transport with a complete live-ID roster and a
/// tabset store. The store writes only when layout state changes.
pub fn run_with_channels_state(
    frames: Receiver<serde_json::Value>,
    prompts: SyncSender<crate::bridge::WorkerCommand>,
    store: crate::ui_state::UiStateStore,
    live_ids: Vec<String>,
) -> io::Result<()> {
    run_with_channels_state_guarded(frames, prompts, store, live_ids, Arc::new(Mutex::new(true)))
}

pub fn run_with_channels_state_guarded(
    frames: Receiver<serde_json::Value>,
    prompts: SyncSender<crate::bridge::WorkerCommand>,
    store: crate::ui_state::UiStateStore,
    live_ids: Vec<String>,
    complete_roster: Arc<Mutex<bool>>,
) -> io::Result<()> {
    run_loop(
        frames,
        Some(prompts),
        Some((store, live_ids, complete_roster)),
    )
}

fn run_loop(
    receiver: Receiver<serde_json::Value>,
    mut prompt_sender: Option<SyncSender<crate::bridge::WorkerCommand>>,
    mut state: Option<(crate::ui_state::UiStateStore, Vec<String>, Arc<Mutex<bool>>)>,
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
    if let Some((store, live_ids, _)) = &state {
        store.restore(&mut app, live_ids);
    }
    let mut saved_layout = crate::ui_state::LayoutSignature::capture(&app);
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
        changed |= app.poll_diff();
        changed |= app.poll_history();
        if prompt_sender.is_none() {
            if let Some(id) = app.pending_peer_refresh.take() {
                changed |= app.peer_map.roster(&id, &serde_json::json!({"ok":false}));
            }
            if !app.pending_launches.is_empty() {
                app.pending_launches.clear();
                app.launching = false;
                app.notice = "Session launch unavailable · daemon connection closed".into();
                changed = true;
            }
            if !app.pending_stops.is_empty() {
                app.pending_stops.clear();
                app.notice = "Session stop unavailable · daemon connection closed".into();
                changed = true;
            }
        }
        if let Some(sender) = &prompt_sender {
            let disconnected = dispatch_launches(&mut app, sender);
            let disconnected = dispatch_prompts(&mut app, sender) || disconnected;
            let disconnected = dispatch_answers(&mut app, sender) || disconnected;
            let disconnected = dispatch_peer_refresh(&mut app, sender) || disconnected;
            let disconnected = dispatch_model_controls(&mut app, sender) || disconnected;
            let disconnected = dispatch_stops(&mut app, sender) || disconnected;
            if disconnected {
                prompt_sender = None;
                changed = true;
            }
        }
        // Retry an unsaved layout on later ticks, including ticks with no new
        // UI event (for example when the live roster becomes complete).
        if let Some((store, _, complete)) = &mut state {
            changed |= save_layout_if_changed(&mut app, store, complete, &mut saved_layout);
        } else {
            saved_layout = crate::ui_state::LayoutSignature::capture(&app);
        }
        if changed {
            terminal.draw(|frame| app.draw(frame))?;
        }
    }
    drop(terminal);
    drop(guard);
    Ok(())
}

fn dispatch_launches(app: &mut App, sender: &SyncSender<crate::bridge::WorkerCommand>) -> bool {
    let mut launches = std::mem::take(&mut app.pending_launches).into_iter();
    while let Some((options, prompt, group)) = launches.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Launch(options, prompt, group)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Launch(options, prompt, group))) => {
                app.pending_launches.extend(std::iter::once((options, prompt, group)).chain(launches));
                return false;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.launching = false;
                app.notice = "Session launch unavailable".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

fn dispatch_stops(app: &mut App, sender: &SyncSender<crate::bridge::WorkerCommand>) -> bool {
    let mut stops = std::mem::take(&mut app.pending_stops).into_iter();
    while let Some(id) = stops.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Stop(id)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Stop(id))) => {
                app.pending_stops.extend(std::iter::once(id).chain(stops));
                app.notice = "Daemon writer busy · stop request retained".into();
                return false;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.notice = "Session stop unavailable · daemon connection closed".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

fn save_layout_if_changed(
    app: &mut App,
    store: &mut crate::ui_state::UiStateStore,
    complete: &Mutex<bool>,
    saved_layout: &mut crate::ui_state::LayoutSignature,
) -> bool {
    let layout = crate::ui_state::LayoutSignature::capture(app);
    if layout == *saved_layout {
        return false;
    }
    if app.groups.iter().any(|group| group.tabs.iter().any(|id| app.offline_ids.contains(id))) {
        if app.notice != "Layout save skipped · archived tabs are read-only" {
            app.notice = "Layout save skipped · archived tabs are read-only".into();
            return true;
        }
        return false;
    }
    let notice = match store.save_if_complete(app, complete) {
        Ok(true) => {
            *saved_layout = layout;
            if app.notice.starts_with("Layout save skipped ·") {
                app.notice.clear();
                return true;
            }
            return false;
        }
        Ok(false) => "Layout save skipped · live roster incomplete".into(),
        Err(error) => format!("Layout save skipped · {}", safe_label(&error.to_string())),
    };
    if app.notice == notice {
        false
    } else {
        app.notice = notice;
        true
    }
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

fn dispatch_model_controls(app: &mut App, sender: &SyncSender<crate::bridge::WorkerCommand>) -> bool {
    let mut queries = std::mem::take(&mut app.pending_model_queries).into_iter();
    while let Some(id) = queries.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Models(id)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Models(id))) => {
                app.pending_model_queries.push(id);
                app.pending_model_queries.extend(queries);
                break;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.notice = "Daemon unavailable for model catalog".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    let mut changes = std::mem::take(&mut app.pending_model_changes).into_iter();
    while let Some((id, model)) = changes.next() {
        match sender.try_send(crate::bridge::WorkerCommand::SetModel(id, model)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::SetModel(id, model))) => {
                app.pending_model_changes.push((id, model));
                app.pending_model_changes.extend(changes);
                break;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.notice = "Daemon unavailable for model change".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    let mut permissions = std::mem::take(&mut app.pending_permission_changes).into_iter();
    while let Some((id, mode)) = permissions.next() {
        match sender.try_send(crate::bridge::WorkerCommand::SetPermissionMode(id, mode)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::SetPermissionMode(id, mode))) => {
                app.pending_permission_changes.push((id, mode));
                app.pending_permission_changes.extend(permissions);
                break;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.notice = "Daemon unavailable for permission change".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

fn dispatch_peer_refresh(app: &mut App, sender: &SyncSender<crate::bridge::WorkerCommand>) -> bool {
    let Some(id) = app.pending_peer_refresh.take() else {
        return false;
    };
    if id.is_empty() {
        return false;
    }
    match sender.try_send(crate::bridge::WorkerCommand::Peers(id)) {
        Ok(()) => false,
        Err(TrySendError::Full(crate::bridge::WorkerCommand::Peers(id))) => {
            app.pending_peer_refresh = Some(id);
            false
        }
        Err(TrySendError::Disconnected(crate::bridge::WorkerCommand::Peers(_))) => true,
        Err(_) => unreachable!(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::backend::TestBackend;
    use serde_json::json;

    #[test]
    fn stop_requires_confirmation_and_preserves_the_target_and_draft() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 28);
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"first", "model":"one"}));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"second", "model":"two"}));
        app.groups[0].tabs = vec!["first".into(), "second".into()];
        app.groups[0].active = 0;
        app.input = "unsent draft".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::ALT)));
        assert_eq!(app.stop_confirmation.as_deref(), Some("first"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(app.pending_stops.is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::ALT)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE)));
        assert_eq!(app.pending_stops, vec!["first"]);
        assert_eq!(app.input, "unsent draft");
        app.apply_daemon_frame(&json!({"type":"stop_reply", "session_id":"first", "ok":true}));
        assert!(app.offline_ids.contains("first"));
        assert_eq!(app.groups[0].active_id(), Some("first"));
        assert_eq!(app.input, "unsent draft");
        app.open_stop_confirmation();
        assert!(app.stop_confirmation.is_none());
    }

    #[test]
    fn refused_stop_keeps_session_live_and_draft_intact() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"first"}));
        app.input = "keep this".into();
        app.apply_daemon_frame(&json!({"type":"stop_reply", "session_id":"first", "ok":false,
            "error":"busy"}));
        assert!(!app.offline_ids.contains("first"));
        assert_eq!(app.input, "keep this");
        assert!(app.notice.contains("busy"));
    }

    #[test]
    fn model_picker_is_capability_gated_and_uses_only_daemon_catalog() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"codex-1",
            "engine":"codex", "model":"current", "cwd":"/tmp", "can_set_model":false}));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('m'), KeyModifiers::ALT)));
        assert!(app.model_picker.is_none());
        assert!(app.pending_model_queries.is_empty());

        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"claude-1",
            "engine":"claude", "model":"old", "cwd":"/tmp", "can_set_model":true}));
        app.groups[0].tabs = vec!["claude-1".into()];
        app.groups[0].active = 0;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('m'), KeyModifiers::ALT)));
        assert_eq!(app.pending_model_queries, vec!["claude-1"]);
        assert!(app.model_picker.as_ref().unwrap().loading);
        app.apply_daemon_frame(&json!({"type":"models_reply", "session_id":"codex-1",
            "ok":true, "models":["wrong-engine"]}));
        assert!(app.model_picker.as_ref().unwrap().models.is_empty());
        app.apply_daemon_frame(&json!({"type":"models_reply", "session_id":"claude-1",
            "ok":true, "models":["sonnet", "opus", "bad\nmodel", ""]}));
        assert_eq!(app.model_picker.as_ref().unwrap().models, vec!["sonnet", "opus"]);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_model_changes, vec![("claude-1".into(), "opus".into())]);
    }

    #[test]
    fn permission_picker_requires_capability_and_tracks_daemon_state() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 80, 24);
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"codex-1",
            "engine":"codex", "permission_mode":"default", "can_set_permission_mode":false}));
        app.groups[0].tabs = vec!["codex-1".into()];
        app.open_permission_picker();
        assert!(app.permission_picker.is_none());

        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"claude-1",
            "engine":"claude", "permission_mode":"plan", "running":false, "queued":0,
            "can_set_permission_mode":true}));
        app.groups[0].tabs = vec!["claude-1".into()];
        app.open_permission_picker();
        assert_eq!(app.permission_picker.as_ref().unwrap().1, 2);
        app.permission_picker.as_mut().unwrap().1 = 1;
        app.permission_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.pending_permission_changes, vec![("claude-1".into(), "acceptEdits".into())]);
        assert_eq!(app.permission_modes["claude-1"], "plan");
        app.apply_daemon_frame(&json!({"type":"set_permission_mode_reply", "session_id":"claude-1",
            "ok":false, "error":"sidecar unavailable"}));
        assert!(app.notice.contains("failed"));
        assert_eq!(app.permission_modes["claude-1"], "plan");
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"claude-1",
            "event":{"type":"permission_mode_changed", "data":{"mode":"acceptEdits"}}}));
        assert_eq!(app.permission_modes["claude-1"], "acceptEdits");
    }

    #[test]
    fn dont_ask_requires_idle_and_empty_queue() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 80, 24);
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "engine":"claude",
            "permission_mode":"default", "running":true, "queued":0,
            "can_set_permission_mode":true}));
        app.groups[0].tabs = vec!["s".into()];
        app.open_permission_picker();
        app.permission_picker.as_mut().unwrap().1 = 4;
        app.select_permission_mode();
        assert!(app.pending_permission_changes.is_empty());
        app.apply_daemon_frame(&json!({"type":"reply", "session_id":"s", "ok":true,
            "status":{"session_id":"s", "running":false, "queued":1}}));
        app.select_permission_mode();
        assert!(app.pending_permission_changes.is_empty());
        app.apply_daemon_frame(&json!({"type":"reply", "session_id":"s", "ok":true,
            "status":{"session_id":"s", "running":false, "queued":0}}));
        app.select_permission_mode();
        assert!(app.pending_permission_changes.is_empty());
        assert!(app.permission_confirm_dont_ask);
        app.select_permission_mode();
        assert_eq!(app.pending_permission_changes, vec![("s".into(), "dontAsk".into())]);
        assert!(app.permission_picker.is_none());
    }

    #[test]
    fn dont_ask_confirmation_clears_on_selection_change_and_mouse_cannot_submit() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 80, 24);
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "engine":"claude",
            "permission_mode":"default", "running":false, "queued":0,
            "can_set_permission_mode":true}));
        app.groups[0].tabs = vec!["s".into()];
        app.open_permission_picker();
        app.permission_picker.as_mut().unwrap().1 = 4;
        app.permission_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.permission_confirm_dont_ask);
        app.permission_picker_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
        assert!(!app.permission_confirm_dont_ask);
        app.permission_picker_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: 8, row: 10, modifiers: KeyModifiers::NONE }));
        assert!(app.pending_permission_changes.is_empty());
        assert!(!app.permission_confirm_dont_ask);
    }

    #[test]
    fn permission_picker_mouse_selects_mode_and_outside_click_closes() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 80, 24);
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "engine":"claude",
            "permission_mode":"default", "running":false, "queued":0,
            "can_set_permission_mode":true}));
        app.groups[0].tabs = vec!["s".into()];
        app.open_permission_picker();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: 8, row: 8, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.pending_permission_changes, vec![("s".into(), "plan".into())]);
        app.open_permission_picker();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: 0, row: 0, modifiers: KeyModifiers::NONE }));
        assert!(app.permission_picker.is_none());
    }

    #[test]
    fn model_catalog_failure_never_offers_a_guessed_model() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s",
            "engine":"claude", "model":"existing", "cwd":"/tmp", "can_set_model":true}));
        app.open_model_picker();
        app.apply_daemon_frame(&json!({"type":"models_reply", "session_id":"s",
            "ok":false, "error":"offline"}));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.pending_model_changes.is_empty());
        assert!(app.model_picker.is_some());
    }

    #[test]
    fn model_picker_retries_in_progress_catalog_with_r() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s",
            "engine":"claude", "model":"existing", "cwd":"/tmp", "can_set_model":true}));
        app.open_model_picker();
        app.pending_model_queries.clear();
        app.apply_daemon_frame(&json!({"type":"models_reply", "session_id":"s",
            "ok":true, "loading":true, "models":[],
            "note":"Claude model catalog is loading; press R to retry"}));
        assert!(app.model_picker.as_ref().unwrap().catalog_pending);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE)));
        assert_eq!(app.pending_model_queries, vec!["s"]);
        app.apply_daemon_frame(&json!({"type":"models_reply", "session_id":"s",
            "ok":true, "models":["verified"], "note":"Claude CLI cache"}));
        assert!(!app.model_picker.as_ref().unwrap().catalog_pending);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_model_changes, vec![("s".into(), "verified".into())]);
    }

    #[test]
    fn engine_picker_labels_new_session_scope() {
        assert!(!ENGINE_CHOICES.contains(&"fixture"));
        let mut app = App::default();
        app.open_engine_picker();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(!app.engine_picker);
        assert_eq!(app.new_session.as_ref().unwrap().engine, launch::Engine::Claude);
    }

    #[test]
    fn new_session_form_queues_engine_model_and_first_prompt() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.open_engine_picker();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        for c in "deepseek-test".chars() {
            app.handle(Event::Key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)));
        }
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        for c in "Explain this".chars() {
            app.handle(Event::Key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)));
        }
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        let (options, prompt, group) = app.pending_launches.pop().unwrap();
        assert_eq!(options.engine, launch::Engine::DeepSeek);
        assert_eq!(options.model.as_deref(), Some("deepseek-test"));
        assert_eq!(prompt.as_deref(), Some("Explain this"));
        assert_eq!(group, 0);
        assert!(app.launching);
        assert!(app.new_session.is_none());
    }

    #[test]
    fn launched_session_opens_in_active_pane_and_failure_is_visible() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"old", "model":"old"}));
        app.launching = true;
        app.apply_daemon_frame(&json!({"type":"launch_reply", "ok":true, "session_id":"new", "group":0}));
        assert_eq!(app.groups[0].active_id(), Some("new"));
        assert!(!app.launching);
        app.launching = true;
        app.apply_daemon_frame(&json!({"type":"launch_reply", "ok":false,
            "message":"DEEPSEEK_API_KEY is required"}));
        assert!(app.notice.contains("DEEPSEEK_API_KEY"));
        assert_eq!(app.groups[0].active_id(), Some("new"));
        app.launching = true;
        app.apply_daemon_frame(&json!({"type":"launch_reply", "ok":false, "started":true,
            "session_id":"surviving", "group":0, "message":"socket refused"}));
        assert!(app.notice.contains("doxa-rs attach surviving"));
    }

    #[test]
    fn launch_reply_keeps_the_group_chosen_when_launch_started() {
        let mut app = App::default();
        app.launching = true;
        app.active_group = 1;
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"new", "engine":"codex"}));
        assert_eq!(app.groups[0].active_id(), Some("new"));
        app.active_group = 0;
        app.apply_daemon_frame(&json!({"type":"launch_reply", "ok":true,
            "session_id":"new", "group":1}));
        assert_eq!(app.groups[0].active_id(), None);
        assert_eq!(app.groups[1].active_id(), Some("new"));
        assert_eq!(app.active_group, 0);
        assert!(!app.apply_daemon_frame(&json!({"type":"launch_reply", "ok":true,
            "session_id":"unrequested", "group":1})));
    }

    #[test]
    fn internal_telemetry_refresh_cannot_restore_running_after_turn_done() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "engine":"codex",
            "running":true, "lore_scrub":"ready"}));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"s",
            "event":{"type":"turn_done", "data":{"is_error":false}}}));
        assert_eq!(app.session_activity.get("s").unwrap().0, false);
        let notice = app.notice.clone();
        app.apply_daemon_frame(&json!({"type":"telemetry_status", "session_id":"s",
            "status":{"session_id":"s", "running":true, "lore_scrub":"unavailable"}}));
        assert_eq!(app.session_activity.get("s").unwrap().0, false);
        assert_eq!(app.notice, notice);
        assert_eq!(app.session_telemetry.get("s").unwrap().lore.as_deref(), Some("scrub unavailable"));
    }

    fn painted(app: &App) -> String {
        let mut terminal = Terminal::new(TestBackend::new(100, 28)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let buffer = terminal.backend().buffer();
        (0..28).map(|y| (0..100).map(|x| buffer[(x, y)].symbol()).collect::<String>())
            .collect::<Vec<_>>().join("\n")
    }

    #[test]
    fn history_picker_filters_attached_transcripts_and_opens_selected_tab() {
        let mut app = App::default();
        app.apply_update(DaemonUpdate::Upsert(Session { id: "alpha".into(), title: "First".into(),
            collection: "repo".into(), transcript: "red apple".into(), status: "Ready".into() }));
        app.apply_update(DaemonUpdate::Upsert(Session { id: "beta".into(), title: "Second".into(),
            collection: "repo".into(), transcript: "green pear".into(), status: "Ready".into() }));
        app.handle(Event::Resize(100, 28));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)));
        for c in "pear".chars() { app.handle(Event::Key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE))); }
        let view = painted(&app);
        assert!(view.contains("Session history"));
        assert!(view.contains("Second"));
        assert!(!view.contains("First · alpha"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(), Some("beta"));
        assert!(!app.history_modal);
    }

    #[test]
    fn restored_and_appended_transcripts_keep_only_bounded_utf8_tail() {
        let mut app = App::default();
        let long = format!("{}éEND", "a".repeat(MAX_TRANSCRIPT_BYTES));
        app.apply_update(DaemonUpdate::Upsert(Session { id: "s".into(), title: "S".into(),
            collection: "repo".into(), transcript: long.clone(), status: "Ready".into() }));
        assert!(app.sessions[0].transcript.len() <= MAX_TRANSCRIPT_BYTES);
        assert!(app.sessions[0].transcript.ends_with("éEND"));
        app.apply_update(DaemonUpdate::Transcript { id: "s".into(), markdown: long.clone() });
        assert!(app.sessions[0].transcript.len() <= MAX_TRANSCRIPT_BYTES);
        let session = &mut app.sessions[0];
        assert!(append_transcript(session, &long));
        assert!(session.transcript.len() <= MAX_TRANSCRIPT_BYTES);
        assert!(session.transcript.ends_with("éEND"));
    }

    #[test]
    fn input_requests_have_a_visible_capacity_limit() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "engine":"claude"}));
        for index in 0..=MAX_INPUT_REQUESTS {
            app.apply_daemon_frame(&json!({"type":"event", "session_id":"s",
                "event":{"type":"needs_input", "data":{"id":format!("request-{index}"),
                    "kind":"permission", "title":"Approve?"}}}));
        }
        assert_eq!(app.input_requests.len(), MAX_INPUT_REQUESTS);
        assert!(app.notice.contains("Too many input requests"));
    }

    #[test]
    fn engine_picker_header_click_does_not_select_an_engine() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 80, 24);
        app.open_engine_picker();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: 10, row: 3, modifiers: KeyModifiers::NONE }));
        assert!(app.engine_picker);
        assert!(!app.notice.contains("doxa-rs new"));
    }

    #[test]
    fn offline_history_opens_read_only_and_never_queues_prompt() {
        let mut app = App::default();
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        tx.send(vec![history::OfflineSession { id: "saved-1".into(),
            project: "project\u{1b}[31m".into(), markdown: "**You:** saved".into() }]).unwrap();
        assert!(app.poll_history());
        assert!(app.offline_ids.contains("saved-1"));
        assert!(!app.sessions[0].collection.contains('\u{1b}'));
        app.handle(Event::Resize(100, 28));
        app.open_history();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(), Some("saved-1"));
        app.focus = Focus::Prompt;
        app.input = "do not send".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.pending_prompts.is_empty());
        assert_eq!(app.notice, "Archived transcript is read-only");
    }

    #[test]
    fn history_and_mouse_switches_restore_each_pane_draft() {
        let mut app = App::default();
        for id in ["alpha", "beta"] {
            app.apply_update(DaemonUpdate::Upsert(Session { id: id.into(), title: id.into(),
                collection: "repo".into(), transcript: String::new(), status: "Ready".into() }));
        }
        app.handle(Event::Resize(100, 28));
        app.input = "alpha draft".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(), Some("beta"));
        assert!(app.input.is_empty());
        app.input = "beta draft".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT)));
        assert!(app.input.is_empty());
        app.input = "second pane draft".into();
        let first = app.layout(app.size).panes.unwrap()[0];
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: first.x + 2, row: first.y + 2, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.input, "beta draft");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(), Some("alpha"));
        assert_eq!(app.input, "alpha draft");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT)));
        assert_eq!(app.input, "second pane draft");
    }

    #[test]
    fn model_change_updates_only_model_title_and_hello_sanitizes_id_notice() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"raw\u{1b}[31m", "model":"old"}));
        assert_eq!(app.sessions[0].title, "old");
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"raw\u{1b}[31m",
            "event":{"type":"model_changed", "data":{"model":"new"}}}));
        assert_eq!(app.sessions[0].title, "new");
        app.sessions[0].title = "Custom title".into();
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"raw\u{1b}[31m",
            "event":{"type":"model_changed", "data":{"model":"newer"}}}));
        assert_eq!(app.sessions[0].title, "Custom title");
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"raw\u{1b}[31m"}));
        assert!(!app.notice.contains('\u{1b}'));
    }

    #[test]
    fn history_stays_closed_when_terminal_cannot_draw_it() {
        let mut app = App::default();
        app.handle(Event::Resize(27, 11));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)));
        assert!(!app.history_modal);
        assert!(app.notice.contains("Enlarge terminal"));
        app.handle(Event::Resize(100, 28));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)));
        assert!(app.history_modal);
        app.handle(Event::Resize(27, 11));
        assert!(!app.history_modal);
    }

    #[test]
    fn diff_view_is_read_only_modal_and_renders_patch_colors() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.diff_modal = true;
        app.diff_text = "Base: HEAD\n@@ -1 +1 @@\n-old\n+new".into();
        let view = painted(&app);
        assert!(view.contains("Worktree diff"));
        assert!(view.contains("+new"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(!app.diff_modal);
    }

    #[test]
    fn diff_pane_keeps_the_active_prompt_and_restores_the_other_session() {
        let mut app = App::default();
        app.apply_update(DaemonUpdate::Upsert(Session { id: "first".into(), title: "First".into(),
            collection: "repo".into(), transcript: "active transcript".into(), status: "Ready".into() }));
        app.apply_update(DaemonUpdate::Upsert(Session { id: "second".into(), title: "Second".into(),
            collection: "repo".into(), transcript: "hidden transcript".into(), status: "Ready".into() }));
        app.groups[1].tabs.push("second".into());
        app.handle(Event::Resize(100, 28));
        app.handle(Event::Key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE)));
        assert!(app.diff_pane);
        app.diff_text = "Base: HEAD\n@@ -1 +1 @@\n-old\n+new".into();
        let view = painted(&app);
        assert!(view.contains("active transcript"));
        assert!(view.contains("Worktree diff"));
        assert!(view.contains("+new"));
        assert!(!view.contains("hidden transcript"));
        let diff_area = app.layout(app.size).panes.unwrap()[1];
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::ScrollDown,
            column: diff_area.x + 2, row: diff_area.y + 2, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.active_group, 0);
        assert_eq!(app.diff_scroll, 3);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE)));
        assert_eq!(app.input, "x");
        app.handle(Event::Key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE)));
        assert!(!app.diff_pane);
        assert!(painted(&app).contains("hidden transcript"));
    }

    #[test]
    fn long_transcript_window_reaches_both_ends() {
        let lines: Vec<Line<'static>> = (0..70_000).map(|i| Line::from(i.to_string())).collect();
        let (window, at_bottom) = transcript_window(lines.clone(), 8, 0, 0);
        assert_eq!(window.len(), u16::MAX as usize);
        assert_eq!(at_bottom, u16::MAX - 8);
        assert_eq!(window[usize::from(at_bottom) + 7].to_string(), "69999");
        let (window, at_top) = transcript_window(lines, 8, 0, 69_992);
        assert_eq!(at_top, 0);
        assert_eq!(window[0].to_string(), "0");
        assert_eq!(window[7].to_string(), "7");
    }

    #[test]
    fn transcript_scroll_crosses_u16_boundary_without_losing_lines() {
        let lines: Vec<Line<'static>> = (0..70_000).map(|i| Line::from(i.to_string())).collect();
        for scroll in [0, 1, 4_457, 65_535, 65_536, 69_991, 69_992] {
            let (window, offset) = transcript_window(lines.clone(), 8, 0, scroll);
            assert!(window.len() <= u16::MAX as usize);
            assert_eq!(
                window[usize::from(offset)].to_string(),
                (69_992 - scroll).to_string()
            );
        }
        let mut app = App {
            focus: Focus::Transcript,
            ..Default::default()
        };
        app.groups[0].scroll = usize::from(u16::MAX);
        assert!(app.handle(Event::Key(KeyEvent::new(
            KeyCode::PageUp,
            KeyModifiers::NONE
        ))));
        assert_eq!(app.groups[0].scroll, usize::from(u16::MAX) + 5);
    }

    #[test]
    fn incomplete_roster_retries_unchanged_layout_after_gate_opens() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = crate::ui_state::UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
        let mut app = App::default();
        app.groups[0].tabs.push("one".into());
        let mut saved = crate::ui_state::LayoutSignature::capture(&app);
        app.rail_width = 32;
        let complete = Mutex::new(false);
        assert!(save_layout_if_changed(
            &mut app, &mut store, &complete, &mut saved
        ));
        assert_ne!(saved, crate::ui_state::LayoutSignature::capture(&app));
        assert!(!store.path().exists());
        *complete.lock().unwrap() = true;
        assert!(
            save_layout_if_changed(&mut app, &mut store, &complete, &mut saved),
            "{}",
            app.notice
        );
        assert_eq!(saved, crate::ui_state::LayoutSignature::capture(&app));
        assert!(app.notice.is_empty());
        let written: serde_json::Value =
            serde_json::from_slice(&std::fs::read(store.path()).unwrap()).unwrap();
        assert_eq!(written["rust_ui"]["rail_width"], 32);
    }

    #[test]
    fn failed_layout_write_keeps_signature_pending() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = crate::ui_state::UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
        let mut app = App::default();
        app.groups[0].tabs.push("one".into());
        let mut saved = crate::ui_state::LayoutSignature::capture(&app);
        app.rail_width = 33;
        std::fs::create_dir_all(store.path()).unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(
            store.path().parent().unwrap(),
            std::fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        let complete = Mutex::new(true);
        assert!(save_layout_if_changed(
            &mut app, &mut store, &complete, &mut saved
        ));
        assert_ne!(saved, crate::ui_state::LayoutSignature::capture(&app));
        std::fs::remove_dir(store.path()).unwrap();
        assert!(
            save_layout_if_changed(&mut app, &mut store, &complete, &mut saved),
            "{}",
            app.notice
        );
        assert_eq!(saved, crate::ui_state::LayoutSignature::capture(&app));
        assert!(app.notice.is_empty());
        let written: serde_json::Value =
            serde_json::from_slice(&std::fs::read(store.path()).unwrap()).unwrap();
        assert_eq!(written["rust_ui"]["rail_width"], 33);
    }

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
            &json!({"type":"prompt_rejected", "session_id":"", "text":"old prompt", "message":"queue full"}),
        );
        assert_eq!(app.input, "new draft");
        assert_eq!(app.rejected_drafts[""], ["old prompt"]);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)));
        assert_eq!(app.input, "old prompt");
        assert_eq!(app.rejected_drafts[""], ["new draft"]);
    }

    #[test]
    fn unconfirmed_prompt_requires_deliberate_recovery_before_retry() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"prompt_uncertain", "session_id":"", "text":"possibly sent",
            "message":"Prompt delivery unconfirmed"}));
        assert!(app.input.is_empty());
        assert_eq!(app.rejected_drafts[""], ["possibly sent"]);
        assert!(app.notice.contains("check session"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)));
        assert_eq!(app.input, "possibly sent");
    }

    #[test]
    fn background_session_rejection_does_not_replace_active_draft() {
        let mut app = App::default();
        app.groups[0].tabs.push("a".into());
        app.groups[1].tabs.push("b".into());
        app.input = "draft for a".into();
        app.apply_daemon_frame(&json!({"type":"prompt_rejected", "session_id":"b",
            "text":"draft for b", "message":"queue full"}));
        assert_eq!(app.input, "draft for a");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)));
        assert_eq!(app.input, "draft for a");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT)));
        assert!(app.input.is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)));
        assert_eq!(app.input, "draft for b");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT)));
        assert_eq!(app.input, "draft for a");
    }

    #[test]
    fn untagged_event_or_rejection_cannot_change_the_active_session() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"a", "model":"model"}));
        let original = app.sessions[0].transcript.clone();
        assert!(!app.apply_daemon_frame(&json!({"type":"event",
            "event":{"type":"text_delta", "data":{"text":"wrong session"}}})));
        assert!(!app.apply_daemon_frame(&json!({"type":"prompt_rejected", "text":"wrong draft"})));
        assert_eq!(app.sessions[0].transcript, original);
        assert!(app.rejected_drafts.is_empty());
    }

    #[test]
    fn queued_reply_and_broadcast_count_one_prompt() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"a", "queued":0}));
        app.apply_daemon_frame(&json!({"type":"reply", "session_id":"a", "ok":true, "queued":true}));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"a",
            "event":{"type":"prompt_queued", "data":{"id":"q"}}}));
        assert_eq!(app.session_activity["a"].1, 1);
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"a",
            "event":{"type":"prompt_dequeued", "data":{"id":"q"}}}));
        assert_eq!(app.session_activity["a"].1, 0);
    }
}
