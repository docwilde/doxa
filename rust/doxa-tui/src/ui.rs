//! Terminal shell for the Rust frontend. Daemon adapters can feed [`App::apply_update`].
use std::collections::{HashMap, HashSet, VecDeque};
use std::cell::{Cell, RefCell};
use std::io::{self, IsTerminal, Stdout, Write};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, SyncSender, TryRecvError, TrySendError};
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use crossterm::event::{
    self, DisableBracketedPaste, EnableBracketedPaste, Event, KeyCode, KeyEvent, KeyEventKind,
    KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use crossterm::terminal::{self, EnterAlternateScreen, LeaveAlternateScreen};
use crossterm::{
    event::{DisableMouseCapture, EnableMouseCapture},
    execute,
};
use ratatui::backend::CrosstermBackend;
use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Tabs, Widget, Wrap};
use ratatui::{Frame, Terminal};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::{diff_view, history, launch, lore_picker, markdown, peer_map::PeerMap};
use crate::theme;

mod tool_cards;
mod links;
mod transcript_roles;
pub(crate) mod transcript_tools;
use tool_cards::ToolCards;

const MIN_PANE_WIDTH: u16 = 28;
const MIN_PANE_HEIGHT: u16 = 8;
const MIN_RAIL_WIDTH: u16 = 12;
const MAX_PENDING_PROMPTS: usize = 32;
const MAX_QUEUED_REJECTIONS: usize = 8;
const MAX_REJECT_REASON_BYTES: usize = 1024;
const MAX_INPUT_REQUESTS: usize = 32;
const INPUT_BLINK_INTERVAL: Duration = Duration::from_millis(650);
const SPINNER_INTERVAL: Duration = Duration::from_millis(120);
const SPINNER_FRAMES: [&str; 4] = ["◐", "◓", "◑", "◒"];
const SEARCH_DEBOUNCE: Duration = Duration::from_millis(130);
const MAX_SEARCH_WORKERS: usize = 2;
// JSON may expand one input byte to a six-byte Unicode escape.
const MAX_INPUT_BYTES: usize = 10 * 1024;
const MAX_TRANSCRIPT_BYTES: usize = 512 * 1024;
const MAX_RENDERED_TRANSCRIPTS: usize = 2;
const MAX_REASONING_DISPLAY_CHARS: usize = 48 * 1024;
const MAX_ANSWER_BYTES: usize = 10 * 1024;
// Reserve metadata wrapping even in a narrow review modal. This is also the
// number used by the read-through gate, so it never credits hidden raw rows.
const REVIEW_BODY_RESERVE: u16 = 10;
const ACTIONS: [(&str, &str); 14] = [
    ("Peer map", "Ctrl+M"),
    ("Tool activity", "Ctrl+T"),
    ("Open selected session", "rail selection"),
    ("Previous tab", "active pane"),
    ("Next tab", "active pane"),
    ("Switch pane", "Shift+Tab"),
    ("Session history", "Ctrl+R"),
    ("LORE beliefs", "Alt+L"),
    ("Worktree diff", "F2"),
    ("Engine for new session", "Alt+E"),
    ("Session model", "Alt+M"),
    ("Claude permissions", "Alt+P"),
    ("Stop active session", "Alt+X"),
    ("Move tab to other pane", "/movepane"),
];
struct CommandHelp { name: &'static str, form: &'static str,
    summary: &'static str, support: &'static str }

// Names mirror Python 1.19's command registry. Forms and support describe
// this Rust frontend, including commands the Python frontend alone provides.
const COMMANDS: &[CommandHelp] = &[
    CommandHelp { name: "/peers", form: "/peers", summary: "Peer map", support: "local" },
    CommandHelp { name: "/split", form: "/split", summary: "Stacked pane split", support: "local" },
    CommandHelp { name: "/vsplit", form: "/vsplit", summary: "Side-by-side pane split", support: "local" },
    CommandHelp { name: "/diff", form: "/diff", summary: "Worktree diff", support: "local · active worktree" },
    CommandHelp { name: "/pane", form: "/pane [1|2]", summary: "Switch pane", support: "local · two panes" },
    CommandHelp { name: "/movepane", form: "/movepane [1|2]", summary: "Move active tab", support: "local · two panes" },
    CommandHelp { name: "/sidebar", form: "/sidebar [on|off|wider|narrower|width N]", summary: "Session rail", support: "local" },
    CommandHelp { name: "/collection", form: "/collection [action] [name]", summary: "Organize sessions", support: "local · list/new/rename/delete/add/remove" },
    CommandHelp { name: "/msg", form: "/msg <peer> <text>", summary: "Message a peer", support: "local · same project" },
    CommandHelp { name: "/fleet", form: "/fleet ...", summary: "Fleet control", support: "unavailable in Rust" },
    CommandHelp { name: "/mesh", form: "/mesh", summary: "Peer map", support: "local · arguments unavailable" },
    CommandHelp { name: "/img", form: "/img [path]", summary: "Image support", support: "unavailable in Rust" },
    CommandHelp { name: "/login", form: "/login [provider]", summary: "Provider login", support: "unavailable in Rust" },
    CommandHelp { name: "/logout", form: "/logout [provider]", summary: "Provider logout", support: "unavailable in Rust" },
    CommandHelp { name: "/settings", form: "/settings", summary: "Native settings", support: "local · linger and worktree for new sessions" },
    CommandHelp { name: "/setup", form: "/setup", summary: "Setup checks", support: "CLI only · doxa setup" },
    CommandHelp { name: "/doctor", form: "/doctor", summary: "Health checks", support: "unavailable in Rust" },
    CommandHelp { name: "/plugins", form: "/plugins", summary: "Plugin inventory", support: "unavailable in Rust" },
    CommandHelp { name: "/reload-plugins", form: "/reload-plugins", summary: "Refresh plugins", support: "unavailable in Rust" },
    CommandHelp { name: "/model", form: "/model", summary: "Select session model", support: "local · picker; name argument unavailable" },
    CommandHelp { name: "/engine", form: "/engine", summary: "Engine for new sessions", support: "local · picker; ID argument unavailable" },
    CommandHelp { name: "/branch", form: "/branch [name]", summary: "Switch base branch", support: "local · active session" },
    CommandHelp { name: "/mode", form: "/mode", summary: "Permission mode", support: "local · picker; name argument unavailable" },
    CommandHelp { name: "/effort", form: "/effort", summary: "Reasoning effort", support: "local · picker; level argument unavailable" },
    CommandHelp { name: "/usage", form: "/usage", summary: "Session usage", support: "local · reported totals only" },
    CommandHelp { name: "/context", form: "/context", summary: "Context window", support: "local · measured totals; no component breakdown" },
    CommandHelp { name: "/queue", form: "/queue", summary: "Queued prompts", support: "local · cancel selected item with X" },
    CommandHelp { name: "/clear", form: "/clear", summary: "Fresh session in this tab", support: "local · idle session and writable tabset required" },
    CommandHelp { name: "/detach", form: "/detach", summary: "Leave session running", support: "local" },
    CommandHelp { name: "/attach", form: "/attach [prefix]", summary: "Attach live session", support: "local · new tab" },
    CommandHelp { name: "/sessions", form: "/sessions", summary: "Session history", support: "local · history browser; kill unavailable" },
    CommandHelp { name: "/rename", form: "/rename [name]", summary: "Name active tab", support: "local" },
    CommandHelp { name: "/dir", form: "/dir", summary: "Session directory", support: "local" },
    CommandHelp { name: "/cd", form: "/cd <path>", summary: "Open directory", support: "local · new tab" },
    CommandHelp { name: "/beliefs", form: "/beliefs", summary: "LORE beliefs", support: "local · requires LORE" },
    CommandHelp { name: "/pending", form: "/pending", summary: "LORE proposals", support: "local · requires LORE" },
    CommandHelp { name: "/search", form: "/search [terms]", summary: "Search saved sessions", support: "local · LORE index then bounded transcript scan" },
    CommandHelp { name: "/resume", form: "/resume [session-id]", summary: "Resume conversation", support: "local · new tab" },
    CommandHelp { name: "/compact", form: "/compact", summary: "Compact transcript", support: "Claude only · completed LORE review required" },
    CommandHelp { name: "/update", form: "/update [--restart]", summary: "Update DOXA", support: "unavailable in Rust" },
    CommandHelp { name: "/help", form: "/help", summary: "Command registry", support: "local" },
    CommandHelp { name: "/about", form: "/about", summary: "Rust version", support: "local · version only" },
];

const ENGINE_CHOICES: [&str; 4] = ["codex", "claude", "deepseek", "glm"];
// Fallback model IDs measured from the vendors' catalogues in Python 1.19.
// Unknown models get no effort choices until a verified capability arrives.
const DEEPSEEK_MODELS: [&str; 2] = ["deepseek-flash", "deepseek-v4-pro"];
const GLM_MODELS: [&str; 10] = ["glm-4.5", "glm-4.5-air", "glm-4.6", "glm-4.7",
    "glm-5", "glm-5-turbo", "glm-5.1", "glm-5.2", "glm-5.3", "glm-5.3-flash"];
const DEEPSEEK_EFFORTS: [&str; 4] = ["none", "low", "high", "max"];
const GLM_EFFORTS: [&str; 3] = ["low", "high", "max"];

fn vendor_models(engine: launch::Engine) -> &'static [&'static str] {
    match engine { launch::Engine::DeepSeek => &DEEPSEEK_MODELS, launch::Engine::Glm => &GLM_MODELS, _ => &[] }
}
fn vendor_default_model(engine: launch::Engine) -> &'static str {
    match engine { launch::Engine::DeepSeek => "deepseek-flash", launch::Engine::Glm => "glm-5.3-flash", _ => "" }
}
fn effort_choices(engine: &str, model: &str) -> &'static [&'static str] {
    match engine {
        "deepseek" if DEEPSEEK_MODELS.contains(&model) => &DEEPSEEK_EFFORTS,
        "glm" if GLM_MODELS.contains(&model) => &GLM_EFFORTS,
        _ => &[],
    }
}
fn engine_name(engine: launch::Engine) -> &'static str {
    match engine { launch::Engine::Codex => "codex", launch::Engine::Claude => "claude",
        launch::Engine::DeepSeek => "deepseek", launch::Engine::Glm => "glm", launch::Engine::Fixture => "fixture" }
}
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
struct AttachPicker {
    rows: Vec<crate::discovery::Session>,
    query: String,
    selected: usize,
}

#[derive(Debug)]
struct BranchPicker {
    session_id: String,
    branches: Vec<String>,
    base: String,
    selected: usize,
}

#[derive(Debug)]
struct RepoPicker {
    current_dir: PathBuf,
    paths: Vec<PathBuf>,
    selected: usize,
}

fn chooser_visible_start(view_start: &Cell<usize>, selected: usize, visible: usize) -> usize {
    // Moving the selection within the painted viewport must not move its rows.
    let start = view_start.get();
    let start = if selected < start { selected }
        else if selected >= start.saturating_add(visible) {
            selected.saturating_sub(visible.saturating_sub(1))
        } else { start };
    view_start.set(start);
    start
}

fn chooser_list_lines(lines: Vec<Line<'static>>, width: usize) -> Vec<Line<'static>> {
    lines.into_iter().map(|line| {
        let text: String = line.spans.iter().map(|span| span.content.as_ref()).collect();
        Line::styled(clipped_title(&text, width).0, line.style)
    }).collect()
}

#[derive(Clone, Debug)]
struct RejectDraft {
    index: usize,
    reason: String,
}

#[derive(Debug)]
struct PendingRejection {
    session_id: String,
    snapshot: diff_view::DiffSnapshot,
    index: usize,
    reason: String,
}

#[derive(Debug)]
struct LorePicker {
    session_id: Option<String>,
    query: String,
    rows: Vec<lore_picker::Belief>,
    proposals: Vec<lore_picker::Proposal>,
    proposal_mode: bool,
    review: Option<doxa_lore::PendingReview>,
    review_scroll: usize,
    review_seen: usize,
    review_width: usize,
    armed_resolution: Option<doxa_lore::PendingDecision>,
    can_resolve: bool,
    resolving: bool,
    belief_review: Option<doxa_lore::BeliefReview>,
    can_act_on_beliefs: bool,
    belief_action: Option<doxa_lore::BeliefAction>,
    belief_note: String,
    retract_armed: bool,
    belief_acting: bool,
    result_status: Option<String>,
    cwd: String,
    selected: usize,
    offset: u16,
    evidence: Option<(u64, Vec<lore_picker::Evidence>)>,
    status: String,
    pending: Option<Receiver<Result<lore_picker::ResultPage, &'static str>>>,
}

#[derive(Debug)]
struct NewSession {
    engine: launch::Engine,
    model: String,
    models: Vec<String>,
    model_efforts: HashMap<String, Vec<String>>,
    catalog_note: String,
    catalog_pending: bool,
    effort: Option<String>,
    prompt: String,
    field: usize,
}

#[derive(Debug)]
struct ClearPending {
    old_id: String,
    group: usize,
}

#[derive(Debug)]
struct ClearSwap {
    old_id: String,
    new_id: String,
    group: usize,
    position: usize,
}

#[derive(Debug)]
struct EffortPicker { session_id: String, engine: String, model: String,
    levels: Vec<String>, selected: usize }

#[derive(Debug, Clone)]
struct QueueRow { id: String, preview: String }

#[derive(Debug)]
struct QueuePicker {
    session_id: String,
    rows: Vec<QueueRow>,
    selected: usize,
    loading: bool,
    cancelling: Option<String>,
}

#[derive(Debug)]
struct SettingsMenu {
    rows: [(String, bool); 2],
    selected: usize,
    linger_draft: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct ChipHit {
    group: usize,
    kind: &'static str,
    rect: Rect,
    pane: Rect,
}

#[derive(Clone, Debug)]
struct ChipInfo {
    kind: &'static str,
    label: String,
    lines: Vec<String>,
    scroll: usize,
    owner: Option<(String, String)>,
}

fn chip_hint(kind: &str) -> &'static str {
    match kind {
        "permission" => "Permission mode for this session · click to choose",
        "engine" => "Engine for new sessions · click to choose",
        "model" => "Model for this session · click to choose",
        "repo" => "Choose a known directory for a new session tab",
        "directory" => "Choose a known directory for a new session tab",
        "effort" => "Effort · current session; Alt+F selects the next turn when idle",
        "context" => "Current session context usage · click for details",
        "memory" => "User and scoped LORE memory · click to view entries",
        "beliefs" => "LORE beliefs · click to browse",
        "cost" => "Provider billing and quota information",
        "balance" => "Current DeepSeek API account balance",
        "more" => "More chips · click to reveal hidden chips",
        _ => "",
    }
}

fn chooser_row_style(selected: bool) -> Style {
    if selected {
        Style::default().fg(theme::ACCENT).bg(theme::HIGHLIGHT).add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(theme::SECONDARY)
    }
}

fn repo_chip(status: &doxa_worktrees::RepoStatus) -> (&'static str, String) {
    match status {
        doxa_worktrees::RepoStatus::Directory { name } =>
            ("directory", format!("dir {}", safe_label(name))),
        doxa_worktrees::RepoStatus::Repository { repo, base, checked_out, sha, worktree } => {
            let mut label = safe_label(repo);
            if let Some(branch) = base.as_deref().or(checked_out.as_deref()) {
                label.push_str(" ⎇ ");
                label.push_str(&safe_label(branch));
            }
            if let Some(worktree) = worktree {
                if worktree == "linked worktree" {
                    label.push_str(" [wt]");
                } else {
                    label.push_str(" [wt ");
                    label.push_str(&safe_label(checked_out.as_deref().unwrap_or("detached")));
                    label.push(']');
                }
            }
            if let Some(sha) = sha.as_ref().filter(|sha| !base.as_deref().or(checked_out.as_deref())
                .is_some_and(|branch| branch.starts_with(sha.as_str()))) {
                label.push_str(" @");
                label.push_str(sha);
            }
            ("repo", label)
        }
    }
}

fn safe_label(value: &str) -> String {
    markdown::sanitize(value)
        .replace('\n', " ")
        .chars()
        .take(200)
        .collect()
}

fn attach_matches(session: &crate::discovery::Session, query: &str) -> bool {
    let query = query.to_lowercase();
    query.is_empty() || session.id.to_lowercase().starts_with(&query)
        || session.title.to_lowercase().contains(&query)
}

fn visible_raw_line(value: &str) -> String {
    value.chars().flat_map(|ch| {
        if ch.is_control() || matches!(ch, '\u{200e}' | '\u{200f}' | '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}') {
            ch.escape_debug().collect::<String>().chars().collect::<Vec<_>>()
        } else { vec![ch] }
    }).collect()
}

fn raw_visual_rows(raw: &str, width: usize) -> Vec<String> {
    let mut rows = Vec::new();
    for line in raw.split('\n') {
        let mut row = String::new();
        let mut cells = 0;
        for ch in visible_raw_line(line).chars() {
            let next = UnicodeWidthChar::width(ch).unwrap_or(0);
            if cells + next > width.max(1) && !row.is_empty() {
                rows.push(std::mem::take(&mut row));
                cells = 0;
            }
            row.push(ch);
            cells += next;
        }
        rows.push(row);
    }
    rows
}

fn clipped_title(value: &str, width: usize) -> (String, bool) {
    let label = markdown::sanitize(value).replace('\n', " ");
    let mut out = String::new();
    let mut used = 0;
    for ch in label.chars() {
        let cells = ch.width().unwrap_or(0);
        if used + cells > width {
            if width > 0 {
                while used + 1 > width {
                    if let Some(last) = out.pop() { used -= last.width().unwrap_or(0); }
                    else { break; }
                }
                out.push('…');
            }
            return (out, false);
        }
        out.push(ch);
        used += cells;
    }
    (out, true)
}

fn unsafe_input_char(ch: char) -> bool {
    ch.is_control()
        || matches!(ch, '\u{200e}' | '\u{200f}' | '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
}

fn safe_repo_directory(path: &Path) -> Option<PathBuf> {
    let path = std::fs::canonicalize(path).ok()?;
    let label = path.to_str()?;
    (path.is_dir() && label.len() <= 4096 && !label.chars().any(unsafe_input_char))
        .then_some(path)
}

fn repo_directory_entries(current: &Path) -> Vec<PathBuf> {
    // Directory enumeration is bounded so a huge worktree never stalls the UI.
    let mut paths = vec![current.to_path_buf()];
    if let Some(parent) = current.parent().and_then(safe_repo_directory) {
        if parent != current { paths.push(parent); }
    }
    let mut children: Vec<_> = std::fs::read_dir(current).into_iter().flatten()
        .take(128).filter_map(Result::ok)
        .filter_map(|entry| safe_repo_directory(&entry.path()))
        .filter(|path| path != current && !paths.contains(path))
        .collect();
    children.sort();
    children.dedup();
    paths.extend(children);
    paths
}

fn repo_path_label(path: &Path) -> String {
    if let Some(home) = std::env::var_os("HOME").map(PathBuf::from) {
        if let Ok(relative) = path.strip_prefix(&home) {
            return if relative.as_os_str().is_empty() { "~".into() }
                else { format!("~/{}", relative.display()) };
        }
    }
    path.display().to_string()
}

fn prompt_height(draft: &str, pane_height: u16) -> u16 {
    (draft.bytes().filter(|b| *b == b'\n').count().min(5) as u16 + 3)
        .clamp(3, 8).min(pane_height.saturating_sub(5).max(3))
}

fn wrapped_rows(text: &str, width: usize) -> usize {
    let width = width.max(1);
    text.lines().map(|line| line.width().max(1).div_ceil(width)).sum::<usize>().max(1)
}

fn chip_text(kind: &str, label: &str) -> String {
    if kind == "more" {
        format!(" {label} › ")
    } else if kind == "effort" && label == "?" {
        format!(" {label} ")
    } else if matches!(kind, "engine" | "model" | "effort" | "permission" | "beliefs") {
        format!(" {label} ▾ ")
    } else {
        format!(" {label} ")
    }
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

fn append_turn_heading(session: &mut Session, heading: &str) -> bool {
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
struct ReasoningStream {
    text: String,
    tokens: u64,
    exact: bool,
    visible: bool,
    streaming: bool,
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

fn set_reasoning_marker(session: &mut Session, stream: &ReasoningStream) {
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

fn structured_event(event_type: &str, data: &serde_json::Value) -> Option<String> {
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
    context_percent: Option<f64>,
    context_tokens: Option<u64>,
    context_limit: Option<u64>,
    turns: Option<u64>,
    input_tokens: Option<u64>,
    output_tokens: Option<u64>,
    cache_read_tokens: Option<u64>,
    cache_write_tokens: Option<u64>,
    session_cost: Option<String>,
    cost: Option<String>,
    billing_mode: Option<String>,
    subscription_type: Option<String>,
    quota: Option<String>,
    balance: Option<String>,
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
            self.context_percent = data["ctx_percentage"].as_f64()
                .filter(|value| value.is_finite() && (0.0..=100.0).contains(value));
            self.context_tokens = data["ctx_tokens"].as_u64();
            self.context_limit = data["ctx_max_tokens"].as_u64().filter(|limit| *limit > 0);
        }
        if data["usage_scope"] == "session" {
            self.turns = data["num_turns"].as_u64().or(self.turns);
            self.input_tokens = data["input_tokens"].as_u64().or(self.input_tokens);
            self.output_tokens = data["output_tokens"].as_u64().or(self.output_tokens);
            self.cache_read_tokens = data["cache_read_input_tokens"].as_u64().or(self.cache_read_tokens);
        }
        if let Some(cost) = data["session_cost_usd"].as_f64()
            .filter(|value| value.is_finite() && *value >= 0.0) {
            self.cost = Some(format!("${cost:.4}"));
        } else if let Some(cost) = data["cost_usd"].as_f64()
            .filter(|value| value.is_finite() && *value >= 0.0) {
            self.cost = Some(format!("${cost:.4} turn"));
        } else if data.get("session_cost_usd").is_some() || data.get("cost_usd").is_some() {
            self.cost = None;
        }
        if data.get("session_cost_usd").is_some() {
            self.session_cost = data["session_cost_usd"].as_f64()
                .filter(|value| value.is_finite() && *value >= 0.0)
                .map(|cost| format!("${cost:.4}"));
        }
    }

    fn update_status(&mut self, status: &serde_json::Value) {
        if let Some(billing) = status.get("billing") {
            self.billing_mode = match billing["mode"].as_str() {
                Some("api") => Some("api".into()),
                Some("subscription") => Some("subscription".into()),
                _ => None,
            };
            self.subscription_type = billing["type"].as_str()
                .filter(|name| !name.is_empty() && name.len() <= 64 && !name.chars().any(char::is_control))
                .map(safe_label);
            self.quota = billing["quota"].as_str()
                .filter(|quota| !quota.is_empty() && quota.len() <= 120 && !quota.chars().any(char::is_control))
                .map(safe_label);
            self.balance = billing["balance"].as_str()
                .filter(|balance| !balance.is_empty() && balance.len() <= 80 && !balance.chars().any(char::is_control))
                .map(safe_label);
        }
        let context = status["ctx_percentage"].as_f64()
            .filter(|value| value.is_finite() && (0.0..=100.0).contains(value))
            .map(|value| format!("{value:.0}%"));
        let absolute = status["ctx_tokens"].as_u64().zip(status["ctx_max_tokens"].as_u64())
            .filter(|(used, limit)| *limit > 0 && used <= limit)
            .map(|(used, limit)| format!("{used}/{limit}"));
        if status.get("ctx_percentage").is_some() || status.get("ctx_tokens").is_some() {
            self.context = context.or(absolute);
            self.context_percent = status["ctx_percentage"].as_f64()
                .filter(|value| value.is_finite() && (0.0..=100.0).contains(value));
            self.context_tokens = status["ctx_tokens"].as_u64();
            self.context_limit = status["ctx_max_tokens"].as_u64().filter(|limit| *limit > 0);
        }
        if let Some(usage) = status.get("usage") {
            self.turns = usage["num_turns"].as_u64();
            self.input_tokens = usage["input_tokens"].as_u64();
            self.output_tokens = usage["output_tokens"].as_u64();
            self.cache_read_tokens = usage["cache_read_input_tokens"].as_u64();
            self.cache_write_tokens = usage["cache_creation_input_tokens"].as_u64();
        }
        if let Some(cost) = status["total_cost_usd"].as_f64()
            .filter(|value| value.is_finite() && *value >= 0.0) {
            let label = if status["usage"]["cost_basis"].as_str().is_some() {
                if status["usage"]["unpriced_models"].as_array().is_some_and(|models| !models.is_empty()) {
                    "est partial"
                } else { "est" }
            } else { "" };
            self.cost = Some(format!("${cost:.4} {label}").trim_end().to_owned());
            self.session_cost = self.cost.clone();
        } else if status.get("total_cost_usd").is_some() {
            self.cost = None;
            self.session_cost = None;
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

    fn billing_label(&self, engine: Option<&str>) -> Option<String> {
        match engine {
            Some("deepseek" | "glm") => Some(self.cost.clone().unwrap_or_else(|| "$?".into())),
            Some("codex" | "claude") => match self.billing_mode.as_deref() {
                Some("api") => Some(self.cost.clone().unwrap_or_else(|| "$?".into())),
                Some("subscription") => {
                    let tier = self.subscription_type.as_deref().filter(|tier| *tier != "subscription");
                    if tier.is_none() && self.quota.is_none() { return None; }
                    Some(format!("Sub {} · {}", tier.unwrap_or("?"),
                        self.quota.as_deref().unwrap_or("quota ?")))
                }
                _ => None,
            },
            _ => None,
        }
    }

}

// LORE owns both curated-memory lengths and their separate scope caps.
fn memory_fill_percent(chars: u64, cap_chars: u64) -> u64 {
    chars.saturating_mul(100).saturating_add(cap_chars / 2) / cap_chars
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
    Chooser,
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

fn input_request_body(request: &InputRequest, title_width: usize) -> (String, Option<usize>, Vec<usize>) {
    let mut body = String::new();
    let mut selected_row = None;
    let mut option_rows = Vec::new();
    if request.kind == "ask_user" {
        if let Some(question) = request.questions.get(request.step) {
            if !question.header.is_empty() {
                body.push_str(&markdown::sanitize(&question.header));
                body.push('\n');
            }
            if !clipped_title(&question.question, title_width).1 {
                body.push_str(&markdown::sanitize(&question.question));
                body.push_str("\n\n");
            }
            for (i, option) in question.options.iter().enumerate() {
                option_rows.push(body.lines().count());
                if i + 1 == request.selected { selected_row = Some(body.lines().count()); }
                body.push_str(&format!(
                    "{} {}. {}\n",
                    if i + 1 == request.selected { "▸" } else { " " },
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
    (body, selected_row, option_rows)
}

fn ask_user_option_at(request: &InputRequest, menu: Rect, row: u16) -> Option<usize> {
    if request.kind != "ask_user" || request.sending || row <= menu.y || row >= menu.bottom().saturating_sub(1) {
        return None;
    }
    let (body, _, option_rows) = input_request_body(request, usize::from(menu.width.saturating_sub(4)));
    // Render the same wrapping and scroll as the visible dialog into a small
    // scratch buffer. Each option uses an invisible color marker, so a click
    // on a wrapped description cannot accidentally choose the next option.
    let lines: Vec<Line> = body.lines().enumerate().map(|(line_index, text)| {
        if let Some(option) = option_rows.iter().rposition(|&start| start <= line_index) {
            Line::styled(text.to_owned(), Style::default().bg(Color::Rgb(0, 0, (option + 1) as u8)))
        } else { Line::from(text.to_owned()) }
    }).collect();
    let area = Rect::new(0, 0, menu.width, menu.height);
    let mut buffer = Buffer::empty(area);
    Paragraph::new(lines).wrap(Wrap { trim: false }).scroll((request.scroll, 0))
        .block(Block::default().borders(Borders::ALL)).render(area, &mut buffer);
    let local_row = row - menu.y;
    (1..menu.width.saturating_sub(1)).find_map(|x| match buffer[(x, local_row)].bg {
        Color::Rgb(0, 0, option) if option > 0 => Some(usize::from(option)),
        _ => None,
    })
}

#[derive(Debug)]
struct RenderedTranscript {
    pane: usize,
    id: String,
    source: String,
    width: u16,
    expanded: Option<HashSet<usize>>,
    selected: Option<usize>,
    cards_revision: u64,
    lines: Vec<Line<'static>>,
    sections: Vec<transcript_tools::Section>,
    turn_start: Option<usize>,
    prefix_lines: usize,
}

/// Locate the final role heading outside fenced code. Repainting a streamed
/// turn can then parse only that turn while retaining the earlier styled lines.
fn streamed_turn_start(source: &str) -> Option<usize> {
    let mut fence = None;
    let mut start = 0;
    let mut found = None;
    for paragraph in source.split("\n\n") {
        if fence.is_none() && matches!(paragraph.trim_matches('\n'), "**You:**" | "**Assistant:**") {
            found = Some(start);
        } else {
            for line in paragraph.lines() {
                let line = line.trim_start();
                if let Some(marker @ (b'`' | b'~')) = line.as_bytes().first().copied() {
                    let count = line.bytes().take_while(|byte| *byte == marker).count();
                    if count >= 3 {
                        match fence {
                            Some((open_marker, open_count)) if marker == open_marker && count >= open_count
                                && line[count..].trim().is_empty() => fence = None,
                            None => fence = Some((marker, count)),
                            _ => {},
                        }
                    }
                }
            }
        }
        start += paragraph.len() + 2;
    }
    found.filter(|start| *start > 0)
}

impl RenderedTranscript {
    fn render(pane: usize, id: &str, source: &str, width: u16, expanded: Option<&HashSet<usize>>,
              selected: Option<usize>, cards_revision: u64, cards: &[tool_cards::ToolCard]) -> Self {
        let (lines, sections) = transcript_tools::render_with_cards(source, width, expanded, selected, cards);
        let mut turn_start = None;
        let mut prefix_lines = 0;
        if let Some(start) = streamed_turn_start(source) {
            let tail = &source[start..];
            if !tail.contains("Tool: ") && !tail.contains(transcript_tools::REASONING_PREFIX) {
                let (tail_lines, tail_sections) = transcript_tools::render(tail, width, None, None);
                if tail_sections.is_empty() && lines.len() > tail_lines.len() {
                    prefix_lines = lines.len() - tail_lines.len() - 1;
                    // A deferred tool section at the end belongs to this
                    // turn, even if its source precedes the final heading.
                    if sections.iter().all(|section| section.line < prefix_lines) {
                        turn_start = Some(start);
                    }
                }
            }
        }
        Self { pane, id: id.to_owned(), source: source.to_owned(), width,
            expanded: expanded.cloned(), selected, cards_revision,
            lines, sections, turn_start, prefix_lines }
    }

    fn update(&mut self, source: &str, width: u16, expanded: Option<&HashSet<usize>>,
              selected: Option<usize>, cards_revision: u64, cards: &[tool_cards::ToolCard]) {
        if self.width == width && self.expanded.as_ref() == expanded
            && self.selected == selected && self.cards_revision == cards_revision {
            if self.source == source { return; }
            if let Some(start) = self.turn_start.filter(|_| source.starts_with(&self.source)) {
                if streamed_turn_start(source) == Some(start) {
                    let tail = &source[start..];
                    if !tail.contains("Tool: ") && !tail.contains(transcript_tools::REASONING_PREFIX) {
                        let (tail_lines, tail_sections) = transcript_tools::render(tail, width, None, None);
                        if tail_sections.is_empty() {
                            self.lines.truncate(self.prefix_lines);
                            self.lines.push(Line::default());
                            self.lines.extend(tail_lines);
                            self.sections.retain(|section| section.line < self.prefix_lines);
                            self.source.clear();
                            self.source.push_str(source);
                            return;
                        }
                    }
                }
            }
        }
        *self = Self::render(self.pane, &self.id, source, width, expanded, selected, cards_revision, cards);
    }
}

#[derive(Debug)]
enum RailRow {
    Heading(usize),
    Session(usize),
    LooseHeading,
}

#[derive(Debug)]
pub struct App {
    pub sessions: Vec<Session>,
    pub collections: Vec<crate::collections::Collection>,
    pub groups: [PaneGroup; 2],
    pub active_group: usize,
    pub split: Split,
    pub split_percent: u16,
    split_requested: bool,
    pub rail_visible: bool,
    pub rail_width: u16,
    pub rail_selected: usize,
    pub focus: Focus,
    pub input: String,
    input_cursor: usize,
    input_drafts: HashMap<(usize, String), (String, usize)>,
    moved_active_tab: bool,
    session_identity: HashMap<String, (Option<String>, Option<String>)>,
    session_efforts: HashMap<String, String>,
    catalog_efforts: HashMap<(String, String), Vec<String>>,
    next_efforts: HashMap<String, String>,
    pub(crate) custom_names: HashMap<String, String>,
    session_telemetry: HashMap<String, SessionTelemetry>,
    // LORE owns these counts. A bounded background query keeps store I/O off
    // the draw path; an unavailable sidecar leaves the chip unknown.
    memory_cache: HashMap<String, (Option<doxa_lore::MemoryUsage>, Instant)>,
    memory_pending: Option<(String, String, Receiver<Option<(doxa_lore::MemoryUsage, bool)>>)>,
    memory_repo: HashMap<String, bool>,
    memory_menu_pending: Option<(String, String, Receiver<Result<Vec<String>, &'static str>>)>,
    repo_cache: HashMap<String, (Option<doxa_worktrees::RepoStatus>, Instant)>,
    repo_pending: Option<(String, PathBuf, u64, Receiver<Option<doxa_worktrees::RepoStatus>>)>,
    repo_epoch: HashMap<String, u64>,
    chip_offsets: [usize; 2],
    chip_hover: Option<ChipHit>,
    link_hover: Option<String>,
    visible_links: RefCell<Vec<(Rect, String)>>,
    pending_open_urls: Vec<String>,
    chip_info: Option<ChipInfo>,
    // Mouse coordinates must come from the last painted frame, which may
    // differ from the terminal size reported by an earlier resize event.
    rendered_chip_hits: RefCell<Option<Vec<ChipHit>>>,
    blink_on: bool,
    blink_at: Instant,
    model_capabilities: HashMap<String, bool>,
    permission_capabilities: HashMap<String, bool>,
    permission_modes: HashMap<String, String>,
    session_activity: HashMap<String, (bool, usize)>,
    streaming_text: HashSet<String>,
    reasoning_streams: HashMap<String, ReasoningStream>,
    spinner_at: Instant,
    spinner_frame: usize,
    permission_picker: Option<(String, usize)>,
    permission_confirm_dont_ask: bool,
    pending_permission_changes: Vec<(String, String)>,
    stop_confirmation: Option<String>,
    pending_stops: Vec<String>,
    pending_clear_finalizes: Vec<String>,
    pub(crate) clear_stop_after_save: Vec<String>,
    clear_pending: Option<ClearPending>,
    clear_swap: Option<ClearSwap>,
    clear_preflight_error: Option<&'static str>,
    model_picker: Option<ModelPicker>,
    effort_picker: Option<EffortPicker>,
    attach_picker: Option<AttachPicker>,
    branch_picker: Option<BranchPicker>,
    repo_picker: Option<RepoPicker>,
    chooser_height_override: Cell<Option<u16>>,
    chooser_view_start: Cell<usize>,
    chooser_owner: RefCell<Option<String>>,
    lore_picker: Option<LorePicker>,
    engine_picker: bool,
    engine_selected: usize,
    new_session: Option<NewSession>,
    vendor_catalog_pending: Option<(launch::Engine, Receiver<Option<Vec<doxa_vendors::ModelCapability>>>)>,
    pending_launches: Vec<(launch::LaunchOptions, Option<String>, usize)>,
    pending_attaches: Vec<(String, usize)>,
    attaching_ids: HashSet<String>,
    launching: bool,
    pending_model_queries: Vec<String>,
    pending_model_changes: Vec<(String, String)>,
    pending_effort_changes: Vec<(String, String)>,
    pub pending_prompts: Vec<(String, String)>,
    pending_peer_messages: Vec<(String, String, String)>,
    pub input_requests: Vec<InputRequest>,
    pub pending_answers: Vec<(String, String, serde_json::Value)>,
    pub rejected_drafts: HashMap<String, Vec<String>>,
    tool_cards: ToolCards,
    tool_cards_revision: HashMap<String, u64>,
    rendered_transcripts: RefCell<Vec<RenderedTranscript>>,
    tool_modal: bool,
    tool_selected: usize,
    tool_scroll: u16,
    expanded_tool_sections: HashMap<String, HashSet<usize>>,
    selected_tool_sections: HashMap<String, usize>,
    visible_tool_sections: RefCell<Vec<(Rect, usize, String, usize)>>,
    peer_map: PeerMap,
    map_modal: bool,
    action_menu: bool,
    action_selected: usize,
    slash_selected: usize,
    slash_dismissed: bool,
    history_modal: bool,
    history_resume: bool,
    history_explicit: bool,
    history_query: String,
    history_selected: usize,
    history_pending: Option<Receiver<Vec<history::OfflineSession>>>,
    history_scan_query: Option<String>,
    history_query_due: Option<Instant>,
    history_search_inflight: Arc<AtomicUsize>,
    history_scanned_matches: HashMap<String, String>,
    history_entries: HashMap<String, history::OfflineSession>,
    resume_pending: Option<Receiver<(String, Result<launch::LaunchOptions, &'static str>)>>,
    offline_ids: HashSet<String>,
    queue_picker: Option<QueuePicker>,
    settings_menu: Option<SettingsMenu>,
    pending_queue_commands: Vec<crate::bridge::WorkerCommand>,
    diff_modal: bool,
    diff_pane: bool,
    diff_target: Option<String>,
    diff_scroll: usize,
    diff_text: String,
    diff_files: Vec<usize>,
    diff_hunks: Vec<usize>,
    diff_pending: Option<Receiver<(String, diff_view::DiffSnapshot)>>,
    diff_snapshot: Option<diff_view::DiffSnapshot>,
    diff_reject_confirm: Option<RejectDraft>,
    diff_reject_queue: VecDeque<PendingRejection>,
    diff_reject_active: Option<PendingRejection>,
    diff_reject_feedback: Option<(String, String)>,
    diff_reject_pending: Option<Receiver<(String, Result<String, String>, String)>>,
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
            collections: Vec::new(),
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
            split_requested: false,
            rail_visible: true,
            rail_width: 25,
            rail_selected: 0,
            focus: Focus::Prompt,
            input: String::new(),
            input_cursor: 0,
            input_drafts: HashMap::new(),
            moved_active_tab: false,
            session_identity: HashMap::new(),
            session_efforts: HashMap::new(),
            catalog_efforts: HashMap::new(),
            next_efforts: HashMap::new(),
            custom_names: HashMap::new(),
            session_telemetry: HashMap::new(),
            memory_cache: HashMap::new(),
            memory_pending: None,
            memory_repo: HashMap::new(),
            memory_menu_pending: None,
            repo_cache: HashMap::new(),
            repo_pending: None,
            repo_epoch: HashMap::new(),
            chip_offsets: [0, 0],
            chip_hover: None,
            link_hover: None,
            visible_links: RefCell::new(Vec::new()),
            pending_open_urls: Vec::new(),
            chip_info: None,
            rendered_chip_hits: RefCell::new(None),
            blink_on: true,
            blink_at: Instant::now(),
            model_capabilities: HashMap::new(),
            permission_capabilities: HashMap::new(),
            permission_modes: HashMap::new(),
            session_activity: HashMap::new(),
            streaming_text: HashSet::new(),
            reasoning_streams: HashMap::new(),
            spinner_at: Instant::now(),
            spinner_frame: 0,
            permission_picker: None,
            permission_confirm_dont_ask: false,
            pending_permission_changes: Vec::new(),
            stop_confirmation: None,
            pending_stops: Vec::new(),
            pending_clear_finalizes: Vec::new(),
            clear_stop_after_save: Vec::new(),
            clear_pending: None,
            clear_swap: None,
            clear_preflight_error: Some("persistent tabset unavailable"),
            model_picker: None,
            effort_picker: None,
            attach_picker: None,
            branch_picker: None,
            repo_picker: None,
            chooser_height_override: Cell::new(None),
            chooser_view_start: Cell::new(0),
            chooser_owner: RefCell::new(None),
            lore_picker: None,
            engine_picker: false,
            engine_selected: 0,
            new_session: None,
            vendor_catalog_pending: None,
            pending_launches: Vec::new(),
            pending_attaches: Vec::new(),
            attaching_ids: HashSet::new(),
            launching: false,
            pending_model_queries: Vec::new(),
            pending_model_changes: Vec::new(),
            pending_effort_changes: Vec::new(),
            pending_prompts: Vec::new(),
            pending_peer_messages: Vec::new(),
            input_requests: Vec::new(),
            pending_answers: Vec::new(),
            rejected_drafts: HashMap::new(),
            tool_cards: ToolCards::default(),
            tool_cards_revision: HashMap::new(),
            rendered_transcripts: RefCell::new(Vec::new()),
            tool_modal: false,
            tool_selected: 0,
            tool_scroll: 0,
            expanded_tool_sections: HashMap::new(),
            selected_tool_sections: HashMap::new(),
            visible_tool_sections: RefCell::new(Vec::new()),
            peer_map: PeerMap::default(),
            map_modal: false,
            action_menu: false,
            action_selected: 0,
            slash_selected: 0,
            slash_dismissed: false,
            history_modal: false,
            history_resume: false,
            history_explicit: false,
            history_query: String::new(),
            history_selected: 0,
            history_pending: None,
            history_scan_query: None,
            history_query_due: None,
            history_search_inflight: Arc::new(AtomicUsize::new(0)),
            history_scanned_matches: HashMap::new(),
            history_entries: HashMap::new(),
            resume_pending: None,
            offline_ids: HashSet::new(),
            queue_picker: None,
            settings_menu: None,
            pending_queue_commands: Vec::new(),
            diff_modal: false,
            diff_pane: false,
            diff_target: None,
            diff_scroll: 0,
            diff_text: String::new(),
            diff_files: Vec::new(),
            diff_hunks: Vec::new(),
            diff_pending: None,
            diff_snapshot: None,
            diff_reject_confirm: None,
            diff_reject_queue: VecDeque::new(),
            diff_reject_active: None,
            diff_reject_feedback: None,
            diff_reject_pending: None,
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
    fn chooser_identity(&self) -> Option<String> {
        let kind = if let Some(index) = self.active_request_index().filter(|&index|
            self.input_requests[index].kind == "ask_user") {
            format!("ask_user:{}:{}", self.input_requests[index].id, self.input_requests[index].step)
        } else if self.settings_menu.is_some() { "settings".into() }
        else if self.engine_picker { "engine".into() }
        else if self.new_session.is_some() { "new_session".into() }
        else if self.effort_picker.is_some() { "effort".into() }
        else if self.permission_picker.is_some() { "permission".into() }
        else if self.model_picker.is_some() { "model".into() }
        else if self.repo_picker.is_some() { "repo".into() }
        else if let Some(picker) = &self.lore_picker {
            format!("lore:{}:{}:{}:{}", picker.proposal_mode, picker.review.is_some(),
                picker.belief_review.is_some(), picker.evidence.is_some())
        }
        else if self.action_menu { "actions".into() }
        else if let Some(info) = &self.chip_info { format!("chip_info:{}", info.kind) }
        else if self.history_modal { "history".into() }
        else if self.queue_picker.is_some() { "queue".into() }
        else if self.attach_picker.is_some() { "attach".into() }
        else if self.branch_picker.is_some() { "branch".into() }
        else if !self.slash_suggestions().is_empty() { "slash".into() }
        else { return None; };
        Some(format!("{}:{}:{kind}", self.active_group,
            self.groups[self.active_group].active_id().unwrap_or("")))
    }

    fn sync_chooser_state(&self) {
        let identity = self.chooser_identity();
        let mut owner = self.chooser_owner.borrow_mut();
        if *owner != identity {
            *owner = identity;
            self.chooser_height_override.set(None);
            self.chooser_view_start.set(0);
        }
    }

    fn invalidate_repo(&mut self, id: &str) {
        self.repo_cache.remove(id);
        let epoch = self.repo_epoch.entry(id.to_owned()).or_default();
        *epoch = epoch.wrapping_add(1);
    }

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
            .min(self.rail_order().len().saturating_sub(1));
    }

    /// Apply one versioned daemon frame after transport decoding. Returns whether
    /// visible state changed. Unknown frames are ignored for forward compatibility.
    pub fn apply_daemon_frame(&mut self, frame: &serde_json::Value) -> bool {
        let Some(kind) = frame.get("type").and_then(|v| v.as_str()) else {
            return false;
        };
        match kind {
            "branch_reply" => {
                let Some(id) = frame["session_id"].as_str() else { return false; };
                if frame["ok"] == true && frame["message"].as_str().is_some() {
                    self.invalidate_repo(id);
                }
                if frame["ok"] != true {
                    self.notice = format!("branch: {}", safe_label(frame["error"].as_str().unwrap_or("switch refused")));
                } else if let Some(message) = frame["message"].as_str() {
                    self.notice = format!("branch: {}", safe_label(message));
                } else {
                    let Some(base) = frame["base"].as_str() else { return false; };
                    if base.len() > 200 || base.chars().any(unsafe_input_char) { return false; }
                    let Some(rows) = frame["branches"].as_array() else { return false; };
                    if self.groups[self.active_group].active_id() != Some(id) { return false; }
                    let branches: Vec<String> = rows.iter().filter_map(|row| row.as_str())
                        .filter(|name| !name.is_empty() && name.len() <= 200
                            && !name.chars().any(unsafe_input_char))
                        .take(100).map(str::to_owned).collect();
                    if branches.is_empty() {
                        self.notice = "branch: no local base branches available".into();
                    } else {
                        let selected = branches.iter().position(|name| name == base).unwrap_or(0);
                        self.branch_picker = Some(BranchPicker { session_id: id.into(),
                            branches, base: base.into(), selected });
                        if self.active_chooser_rect().is_none() {
                            self.branch_picker = None;
                            self.notice = "Enlarge active pane to choose a branch".into();
                        }
                    }
                }
                true
            }
            "queue_list_reply" => {
                let Some(id) = frame["session_id"].as_str() else { return false; };
                let Some(picker) = self.queue_picker.as_mut().filter(|picker| picker.session_id == id) else { return false; };
                picker.loading = false;
                if frame["ok"] != true {
                    self.notice = format!("Queue unavailable · {}", safe_label(frame["error"].as_str().unwrap_or("daemon refused")));
                    return true;
                }
                let rows = frame["rows"].as_array().into_iter().flatten().take(32).filter_map(|row| {
                    let id = row["id"].as_str()?;
                    if id.is_empty() || id.len() > 128 || !id.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-') { return None; }
                    let preview = row["preview"].as_str().filter(|text| text.len() <= 1024)?;
                    Some(QueueRow { id: id.to_owned(), preview: safe_label(preview) })
                }).collect();
                picker.rows = rows;
                picker.selected = picker.selected.min(picker.rows.len().saturating_sub(1));
                true
            }
            "queue_cancel_reply" => {
                let Some(id) = frame["session_id"].as_str() else { return false; };
                let Some(picker) = self.queue_picker.as_mut().filter(|picker| picker.session_id == id) else { return false; };
                let Some(expected) = picker.cancelling.as_deref() else { return false; };
                if Some(expected) != frame["queue_id"].as_str() { return false; }
                picker.cancelling = None;
                self.notice = if frame["ok"] == true { "Queued prompt cancelled".into() }
                    else { format!("Queue cancellation failed · {}", safe_label(frame["error"].as_str().unwrap_or("item already started"))) };
                picker.loading = true;
                self.pending_queue_commands.push(crate::bridge::WorkerCommand::QueueList(id.to_owned()));
                true
            }
            "attach_reply" => {
                let Some(reply_id) = frame["session_id"].as_str() else { return false; };
                if !self.attaching_ids.remove(reply_id) { return false; }
                if frame["ok"] == true {
                    if let Some(id) = frame["session_id"].as_str().filter(|id| crate::discovery::valid_id(id)) {
                        let target = frame["group"].as_u64().filter(|group| *group < 2)
                            .map(|group| group as usize).unwrap_or(self.active_group);
                        if target != 0 {
                            if let Some(index) = self.groups[0].tabs.iter().position(|tab| tab == id) {
                                self.groups[0].tabs.remove(index);
                                self.groups[0].active = self.groups[0].active.min(self.groups[0].tabs.len().saturating_sub(1));
                            }
                        }
                        let group = &mut self.groups[target];
                        if !group.tabs.iter().any(|tab| tab == id) { group.tabs.push(id.to_owned()); }
                        group.active = group.tabs.iter().position(|tab| tab == id).unwrap_or(group.active);
                        self.active_group = target;
                        self.notice = format!("Attached · {}", safe_label(id));
                    }
                } else {
                    self.notice = format!("Attach failed · {}", safe_label(frame["message"].as_str().unwrap_or("unknown error")));
                }
                true
            }
            "launch_reply" => {
                if !self.launching { return false; }
                self.launching = false;
                if let Some(clear) = self.clear_pending.take() {
                    if frame["ok"] == true {
                        if let Some(id) = frame["session_id"].as_str().filter(|id| crate::discovery::valid_id(id)) {
                            if let Some(position) = self.groups[clear.group].tabs.iter().position(|tab| tab == &clear.old_id) {
                                // A hello can arrive before this reply and provisionally
                                // insert the new session into the first pane.
                                for group in &mut self.groups {
                                    if let Some(provisional) = group.tabs.iter().position(|tab| tab == id) {
                                        group.tabs.remove(provisional);
                                        group.active = group.active.min(group.tabs.len().saturating_sub(1));
                                    }
                                }
                                let group = &mut self.groups[clear.group];
                                let position = group.tabs.iter().position(|tab| tab == &clear.old_id).unwrap_or(position);
                                group.tabs[position] = id.to_owned();
                                group.active = position;
                                group.scroll = 0;
                                self.active_group = clear.group;
                                for collection in &mut self.collections {
                                    if let Some(member) = collection.sessions.iter_mut().find(|member| member.as_str() == clear.old_id) {
                                        *member = id.to_owned();
                                    }
                                }
                                self.clear_swap = Some(ClearSwap { old_id: clear.old_id.clone(),
                                    new_id: id.to_owned(), group: clear.group, position });
                                self.clear_stop_after_save.push(clear.old_id);
                                self.notice = format!("Fresh session ready · {}", safe_label(id));
                                return true;
                            }
                        }
                    } else {
                        self.notice = format!("Clear failed; previous session preserved · {}",
                            safe_label(frame["message"].as_str().unwrap_or("unknown error")));
                        return true;
                    }
                    self.notice = "Clear target changed; new session kept as a separate tab".into();
                }
                if frame["ok"] == true {
                    if let Some(id) = frame["session_id"].as_str().filter(|id| crate::discovery::valid_id(id)) {
                        self.offline_ids.remove(id);
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
                if let Some(effort) = frame["effort"].as_str().filter(|effort| !effort.is_empty()) {
                    self.session_efforts.insert(id.to_owned(), safe_label(effort));
                } else { self.session_efforts.remove(id); }
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
                        if self.session_cwds.get(id) != Some(&path) {
                            self.memory_cache.remove(id);
                            self.memory_repo.remove(id);
                            self.invalidate_repo(id);
                        }
                        self.session_cwds.insert(id.to_owned(), path);
                    } else {
                        self.session_cwds.remove(id);
                        self.memory_cache.remove(id);
                        self.memory_repo.remove(id);
                        self.invalidate_repo(id);
                    }
                } else {
                    self.session_cwds.remove(id);
                    self.memory_cache.remove(id);
                    self.memory_repo.remove(id);
                    self.invalidate_repo(id);
                }
                let transcript = self
                    .sessions
                    .iter()
                    .find(|s| s.id == id)
                    .map(|s| s.transcript.clone())
                    .unwrap_or_default();
                self.apply_update(DaemonUpdate::Upsert(Session {
                    id: id.into(),
                    title: self.custom_names.get(id).cloned().unwrap_or_else(|| model.clone().unwrap_or_else(|| safe_label(id))),
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
                let mut tool_updated = false;
                if self.sessions.iter().any(|session| session.id == id) {
                    tool_updated = self.tool_cards.record(&id, event_type, data);
                    if tool_updated {
                        let revision = self.tool_cards_revision.entry(id.clone()).or_default();
                        *revision = revision.wrapping_add(1);
                    }
                    self.peer_map.event(&id, event_type, data);
                }
                match event_type {
                    "tool_result_detail" => tool_updated,
                    "branch_changed" => {
                        self.invalidate_repo(&id);
                        true
                    }
                    "model_changed" => {
                        if self.effort_picker.as_ref().is_some_and(|picker| picker.session_id == id) {
                            self.effort_picker = None;
                        }
                        let old_model = self.session_identity.get(&id).and_then(|identity| identity.1.clone());
                        let new_model = data.get("model").and_then(|v| v.as_str()).map(safe_label).filter(|s| !s.is_empty());
                        if let Some(identity) = self.session_identity.get_mut(&id) {
                            identity.1 = new_model.clone();
                        }
                        if let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) {
                            if !self.custom_names.contains_key(&id)
                                && old_model.as_deref() == Some(session.title.as_str()) {
                                if let Some(model) = new_model { session.title = model; }
                            }
                        }
                        true
                    }
                    "effort_changed" => {
                        if let Some(effort) = data["effort"].as_str().filter(|effort| !effort.is_empty()) {
                            self.session_efforts.insert(id, safe_label(effort));
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
                        if text.is_empty() { return false; }
                        if let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) {
                            if data["snapshot"] != true && self.streaming_text.insert(id.clone()) {
                                append_turn_heading(session, "Assistant");
                            }
                            if append_transcript(session, text) {
                                self.notice = "Transcript tail limited to 512 KiB".into();
                            }
                            true
                        } else {
                            false
                        }
                    }
                    "reasoning_progress" => self.append_reasoning(&id, data, true),
                    "reasoning_delta" => self.append_reasoning(&id, data, false),
                    "turn_started" => {
                        self.streaming_text.remove(&id);
                        self.reasoning_streams.remove(&id);
                        if let Some(prompt) = data.get("prompt").and_then(|v| v.as_str()).filter(|v| !v.is_empty()) {
                            if let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) {
                                let prompt = markdown::sanitize(prompt);
                                append_turn_heading(session, "You");
                                append_transcript(session, &prompt);
                            }
                        }
                        self.session_activity.entry(id.clone()).or_default().0 = true;
                        self.apply_update(DaemonUpdate::Status {
                            id,
                            text: "Running".into(),
                        });
                        true
                    }
                    "turn_done" => {
                        self.streaming_text.remove(&id);
                        if let Some(stream) = self.reasoning_streams.get_mut(&id) {
                            stream.streaming = false;
                            if let Some(tokens) = data["reasoning_output_tokens"].as_u64() {
                                stream.tokens = tokens;
                                stream.exact = true;
                            }
                            if let Some(session) = self.sessions.iter_mut().find(|session| session.id == id) {
                                set_reasoning_marker(session, stream);
                            }
                        }
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
                                self.effort_picker = None;
                                self.permission_picker = None;
                                self.permission_confirm_dont_ask = false;
                                self.engine_picker = false;
                                self.stop_confirmation = None;
                                if self.input_requests.len() < MAX_INPUT_REQUESTS {
                                    if self.input_requests.is_empty() {
                                        self.blink_on = true;
                                        self.blink_at = Instant::now();
                                    }
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
                        self.input_requests.retain(|request| request.session_id != id);
                        self.pending_answers.retain(|(session, _, _)| session != &id);
                        self.streaming_text.remove(&id);
                        self.session_activity.remove(&id);
                        let before = self.diff_reject_queue.len();
                        self.diff_reject_queue.retain(|item| item.session_id != id);
                        if self.diff_reject_queue.len() != before {
                            self.notice = "Queued hunk rejections cancelled because the session ended".into();
                        }
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
            "peer_message_reply" => {
                let Some(session) = frame["session_id"].as_str()
                    .filter(|id| self.sessions.iter().any(|entry| entry.id == *id)) else { return false; };
                let delivered = frame["delivered_to"].as_array().is_some_and(|ids| !ids.is_empty())
                    || (frame["delivered_to"].is_null() && frame["peer"].is_object());
                if !delivered {
                    if let Some(draft) = frame["draft"].as_str() {
                        self.rejected_drafts.entry(session.to_owned()).or_default().push(draft.to_owned());
                    }
                }
                self.notice = if frame["uncertain"] == true {
                    "Peer delivery unconfirmed · inspect peer before Alt+Up retry".into()
                } else if frame["ok"] != true {
                    format!("Peer message failed · {}", safe_label(frame["error"].as_str().unwrap_or("unknown error")))
                } else if frame["ledger_error"].as_str().is_some_and(|s| !s.is_empty()) {
                    "Peer message delivered · delivery ledger write failed".into()
                } else if delivered {
                    let title = frame["peer"]["title"].as_str().map(safe_label).unwrap_or_else(|| "peer".into());
                    format!("Peer message sent to {title}")
                } else {
                    "Peer message was not delivered · inspect the peer before retrying".into()
                };
                if self.groups[self.active_group].active_id() != Some(session) {
                    self.notice = format!("{} · {}", safe_label(session), self.notice);
                }
                true
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
            "set_effort_reply" => {
                self.notice = if frame["ok"] == true {
                    format!("Effort accepted · {} · awaiting session event",
                        safe_label(frame["effort"].as_str().unwrap_or("unknown")))
                } else {
                    format!("Effort change failed · {}",
                        safe_label(frame["error"].as_str().unwrap_or("unknown error")))
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
            "clear_finalize_reply" => {
                let Some(id) = frame["session_id"].as_str() else { return false; };
                if frame["ok"] == true {
                    let selected_id = self.rail_order().get(self.rail_selected)
                        .map(|index| self.sessions[*index].id.clone());
                    self.sessions.retain(|session| session.id != id);
                    self.session_activity.remove(id);
                    self.session_identity.remove(id);
                    self.session_cwds.remove(id);
                    self.session_efforts.remove(id);
                    self.next_efforts.remove(id);
                    self.session_telemetry.remove(id);
                    self.memory_cache.remove(id);
                    self.memory_repo.remove(id);
                    self.repo_cache.remove(id);
                    self.repo_epoch.remove(id);
                    self.model_capabilities.remove(id);
                    self.permission_capabilities.remove(id);
                    self.permission_modes.remove(id);
                    self.streaming_text.remove(id);
                    self.reasoning_streams.remove(id);
                    self.custom_names.remove(id);
                    self.input_drafts.retain(|(_, session), _| session != id);
                    self.rejected_drafts.remove(id);
                    self.expanded_tool_sections.remove(id);
                    self.selected_tool_sections.remove(id);
                    self.tool_cards_revision.remove(id);
                    self.input_requests.retain(|request| request.session_id != id);
                    self.pending_answers.retain(|(session, _, _)| session != id);
                    self.rail_selected = selected_id.and_then(|selected| self.rail_order().iter()
                        .position(|index| self.sessions[*index].id == selected))
                        .unwrap_or_else(|| self.rail_selected.min(self.rail_order().len().saturating_sub(1)));
                    self.notice = "Previous session finalized".into();
                } else {
                    self.notice = format!("Previous session remains live · {}",
                        safe_label(frame["error"].as_str().unwrap_or("finalization refused")));
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
                        if status.get("effort").is_some() {
                            if let Some(effort) = status["effort"].as_str().filter(|effort| !effort.is_empty()) {
                                self.session_efforts.insert(id.to_owned(), safe_label(effort));
                            } else { self.session_efforts.remove(id); }
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
                    self.session_activity.remove(id);
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
                if text.len() > MAX_INPUT_BYTES || text.chars().any(|ch| unsafe_input_char(ch) && ch != '\n') {
                    self.notice = "Rejected prompt exceeds input limits · inspect the originating client".into();
                    return true;
                }
                let Some(target) = frame.get("session_id").and_then(|v| v.as_str()) else {
                    return false;
                };
                let active = self.groups[self.active_group].active_id().unwrap_or("");
                if self.input.is_empty() && target == active {
                    self.input = text.to_owned();
                    self.input_cursor = self.input.len();
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
                if text.len() > MAX_INPUT_BYTES || text.chars().any(|ch| unsafe_input_char(ch) && ch != '\n') {
                    self.notice = "Rejected prompt exceeds input limits · inspect the originating client".into();
                    return true;
                }
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
        let heading_needed = matches!(event_type, "tool_call" | "tool_result")
            && self.streaming_text.insert(id.to_owned());
        if heading_needed { append_turn_heading(session, "Assistant"); }
        if append_transcript(session, &row) {
            self.notice = "Transcript tail limited to 512 KiB".into();
        }
        true
    }

    fn append_reasoning(&mut self, id: &str, data: &serde_json::Value, progress: bool) -> bool {
        if !self.sessions.iter().any(|session| session.id == id) { return false; }
        let stream = self.reasoning_streams.entry(id.to_owned()).or_default();
        if let Some(tokens) = data.get("approx_tokens").and_then(|value| value.as_u64()) {
            stream.tokens = stream.tokens.max(tokens);
        }
        if progress {
            stream.streaming = true;
        } else {
            let Some(text) = data.get("text").and_then(|value| value.as_str()) else { return false; };
            let clean = markdown::sanitize(text);
            let remaining = MAX_REASONING_DISPLAY_CHARS.saturating_sub(stream.text.chars().count());
            stream.text.extend(clean.chars().take(remaining));
            if clean.chars().count() > remaining && !stream.text.ends_with("[Reasoning display limit reached]") {
                stream.text.push_str("\n[Reasoning display limit reached]");
            }
            stream.tokens = stream.tokens.max(stream.text.chars().count().div_ceil(4) as u64);
            stream.streaming = data.get("final").and_then(|value| value.as_bool()) == Some(false);
        }
        let first = !stream.visible;
        let heading_needed = first && self.streaming_text.insert(id.to_owned());
        let Some(session) = self.sessions.iter_mut().find(|session| session.id == id) else { return false; };
        if heading_needed { append_turn_heading(session, "Assistant"); }
        set_reasoning_marker(session, stream);
        stream.visible = true;
        true
    }

    pub fn handle(&mut self, event: Event) -> bool {
        let before = (self.active_group, self.groups[self.active_group].active_id().unwrap_or("").to_owned());
        let changed = match event {
            Event::Resize(w, h) => {
                self.size = Rect::new(0, 0, w, h);
                self.drag = None;
                self.chip_hover = None;
                self.link_hover = None;
                self.visible_links.borrow_mut().clear();
                *self.rendered_chip_hits.borrow_mut() = None;
                if self.chip_info.is_some() && self.active_chooser_rect().is_none() {
                    self.chip_info = None;
                }
                if self.history_modal && self.active_chooser_rect().is_none() {
                    self.history_modal = false;
                    self.cancel_history_query();
                    self.notice = "Enlarge active pane to search sessions".into();
                }
                if self.attach_picker.is_some() && self.active_chooser_rect().is_none() {
                    self.attach_picker = None;
                    self.notice = "Enlarge active pane to choose a live session".into();
                }
                if self.branch_picker.is_some() && self.active_chooser_rect().is_none() {
                    self.branch_picker = None;
                    self.notice = "Enlarge active pane to choose a branch".into();
                }
                if self.repo_picker.is_some() && self.active_chooser_rect().is_none() {
                    self.repo_picker = None;
                    self.notice = "Enlarge active pane to choose a directory".into();
                }
                if self.queue_picker.is_some() && self.active_chooser_rect().is_none() {
                    self.queue_picker = None;
                    self.notice = "Enlarge active pane to inspect queued prompts".into();
                }
                if self.settings_menu.is_some() && self.active_chooser_rect().is_none() {
                    self.settings_menu = None;
                    self.notice = "Enlarge active pane to edit settings".into();
                }
                if self.lore_picker.is_some() && (w < 34 || h < 13) {
                    self.lore_picker = None;
                    self.notice = "Enlarge terminal to open LORE beliefs".into();
                }
                if self.stop_confirmation.is_some() && !self.stop_confirmation_fits() {
                    self.stop_confirmation = None;
                    self.notice = "Session stop cancelled · enlarge terminal to confirm".into();
                }
                if ((self.model_picker.is_some() || self.effort_picker.is_some() || self.engine_picker || self.new_session.is_some()) && self.active_chooser_rect().is_none())
                    || (self.permission_picker.is_some() && self.active_chooser_rect().is_none()) {
                    self.model_picker = None;
                    self.effort_picker = None;
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
            Event::Paste(text) => self.paste(&text),
            Event::Mouse(mouse) => self.mouse(mouse),
            _ => false,
        };
        let after = (self.active_group, self.groups[self.active_group].active_id().unwrap_or("").to_owned());
        if before != after {
            self.branch_picker = None;
            let moved_active_tab = std::mem::take(&mut self.moved_active_tab)
                && !before.1.is_empty() && before.1 == after.1
                && !self.groups[before.0].tabs.contains(&before.1)
                && self.groups[after.0].tabs.contains(&after.1);
            if moved_active_tab {
                // The draft follows its tab rather than remaining under the
                // old pane key. A command has already consumed its own input.
                self.input_drafts.remove(&before);
                self.input_drafts.remove(&after);
            } else {
                self.input_drafts
                    .insert(before, (std::mem::take(&mut self.input), self.input_cursor));
                (self.input, self.input_cursor) = self.input_drafts.remove(&after).unwrap_or_default();
            }
            self.slash_selected = 0;
            self.slash_dismissed = false;
        }
        self.sync_chooser_state();
        changed
    }

    fn paste(&mut self, text: &str) -> bool {
        if self.diff_reject_confirm.is_some() {
            for ch in text.chars() {
                self.append_reject_reason(if ch.is_whitespace() { ' ' } else { ch });
                if self.diff_reject_confirm.as_ref().is_some_and(|draft| draft.reason.len() >= MAX_REJECT_REASON_BYTES) {
                    break;
                }
            }
            return true;
        }
        if self.focus != Focus::Prompt || self.active_request_index().is_some()
            || self.stop_confirmation.is_some() || self.lore_picker.is_some() || self.settings_menu.is_some()
            || self.new_session.is_some() || self.repo_picker.is_some() || self.model_picker.is_some() || self.effort_picker.is_some()
            || self.permission_picker.is_some() || self.engine_picker || self.action_menu
            || self.history_modal || self.queue_picker.is_some() || self.attach_picker.is_some() || self.branch_picker.is_some() || self.diff_modal || self.map_modal || self.tool_modal {
            return false;
        }
        let mut clean = String::new();
        let available = MAX_INPUT_BYTES.saturating_sub(self.input.len());
        let mut chars = text.chars().peekable();
        let mut truncated = false;
        while let Some(ch) = chars.next() {
            let ch = if ch == '\r' {
                if chars.peek() == Some(&'\n') { chars.next(); }
                '\n'
            } else if ch == '\t' { ' ' } else { ch };
            if unsafe_input_char(ch) && ch != '\n' { continue; }
            if clean.len() + ch.len_utf8() > available {
                truncated = true;
                break;
            }
            clean.push(ch);
        }
        if !clean.is_empty() {
            self.input.insert_str(self.input_cursor, &clean);
            self.input_cursor += clean.len();
            self.slash_selected = 0;
            self.slash_dismissed = false;
        }
        if truncated { self.notice = "Prompt input limit reached · paste truncated".into(); }
        !clean.is_empty() || truncated
    }

    fn append_reject_reason(&mut self, ch: char) {
        if ch.is_control() { return; }
        if let Some(draft) = &mut self.diff_reject_confirm {
            if draft.reason.len() + ch.len_utf8() <= MAX_REJECT_REASON_BYTES {
                draft.reason.push(ch);
            } else {
                self.notice = "Rejection reason limited to 1024 bytes".into();
            }
        }
    }

    fn insert_input(&mut self, ch: char) -> bool {
        if self.input.len() + ch.len_utf8() > MAX_INPUT_BYTES {
            self.notice = "Prompt input limit reached".into();
        } else {
            self.input.insert(self.input_cursor, ch);
            self.input_cursor += ch.len_utf8();
            self.slash_selected = 0;
            self.slash_dismissed = false;
        }
        true
    }

    fn slash_suggestions(&self) -> Vec<(&'static str, &'static str)> {
        if self.focus != Focus::Prompt || self.slash_dismissed
            || self.active_request_index().is_some() || self.stop_confirmation.is_some()
            || self.chip_info.is_some() || self.lore_picker.is_some() || self.settings_menu.is_some()
            || self.new_session.is_some() || self.repo_picker.is_some() || self.model_picker.is_some()
            || self.effort_picker.is_some() || self.permission_picker.is_some()
            || self.engine_picker || self.action_menu || self.history_modal
            || self.queue_picker.is_some() || self.attach_picker.is_some() || self.branch_picker.is_some()
            || self.diff_modal || self.map_modal || self.tool_modal {
            return Vec::new();
        }
        let query = self.input.as_str();
        if !query.starts_with('/') || query.chars().any(char::is_whitespace) { return Vec::new(); }
        COMMANDS.iter().filter(|row| row.name.starts_with(query))
            .map(|row| (row.name, row.summary)).collect()
    }

    fn complete_slash(&mut self) -> bool {
        let matches = self.slash_suggestions();
        let Some((command, _)) = matches.get(self.slash_selected.min(matches.len().saturating_sub(1))) else { return false; };
        self.input = (*command).to_owned();
        self.input_cursor = self.input.len();
        self.slash_dismissed = true;
        true
    }

    fn move_input_vertical(&mut self, down: bool) -> bool {
        let before = &self.input[..self.input_cursor];
        let column = before.rsplit('\n').next().unwrap_or("").chars().count();
        let line_start = before.rfind('\n').map_or(0, |i| i + 1);
        let target_start = if down {
            let Some(end) = self.input[self.input_cursor..].find('\n') else { return false; };
            self.input_cursor + end + 1
        } else {
            if line_start == 0 { return false; }
            self.input[..line_start - 1].rfind('\n').map_or(0, |i| i + 1)
        };
        let target_end = self.input[target_start..].find('\n').map_or(self.input.len(), |i| target_start + i);
        self.input_cursor = target_start + self.input[target_start..target_end]
            .char_indices().nth(column).map_or(target_end - target_start, |(i, _)| i);
        true
    }

    /// Handle bare DOXA commands before a prompt can reach an agent. Unknown
    /// provider and plugin commands still pass through. Known unsupported
    /// forms stay in the draft.
    fn dispatch_prompt_command(&mut self) -> bool {
        let input = self.input.trim();
        if !input.starts_with('/') || input.contains('\n') {
            return false;
        }
        let mut parts = input.split_whitespace();
        let Some(name) = parts.next() else { return false; };
        let args: Vec<&str> = parts.collect();
        if !matches!(name, "/help" | "/about" | "/sessions" | "/settings" | "/model" | "/effort" | "/engine"
            | "/mode" | "/beliefs" | "/diff" | "/peers" | "/split"
            | "/vsplit" | "/pane" | "/sidebar" | "/detach" | "/dir") {
            return false;
        }
        if !args.is_empty() && !matches!(name, "/pane" | "/sidebar") {
            self.notice = format!("{name} arguments are not available in Rust yet");
            return true;
        }
        let pane_target = if name == "/pane" && !args.is_empty() {
            match args.as_slice() {
                ["1"] => Some(0),
                ["2"] => Some(1),
                _ => {
                    self.notice = "Usage: /pane [1|2]".into();
                    return true;
                }
            }
        } else { None };
        if pane_target == Some(1) && !self.pane_group_two_exists() {
            self.notice = "There is only one pane group · /split or /vsplit makes a second".into();
            return true;
        }
        let sidebar = if name == "/sidebar" && !args.is_empty() {
            match args.as_slice() {
                ["on"] => Some((true, None)),
                ["off"] => Some((false, None)),
                ["wider"] => Some((true, Some(self.rail_width.saturating_add(4).min(80)))),
                ["narrower"] => Some((true, Some(self.rail_width.saturating_sub(4).max(MIN_RAIL_WIDTH)))),
                ["width", width] => match width.parse::<u16>() {
                    Ok(width) if (MIN_RAIL_WIDTH..=80).contains(&width) => Some((true, Some(width))),
                    _ => {
                        self.notice = "Sidebar width must be 12–80 cells".into();
                        return true;
                    }
                },
                _ => {
                    self.notice = "Usage: /sidebar [on|off|wider|narrower|width N]".into();
                    return true;
                }
            }
        } else { None };
        let name = name.to_owned();
        self.input.clear();
        self.input_cursor = 0;
        match name.as_str() {
            "/help" => {
                self.open_help();
            }
            "/about" => self.notice = format!("DOXA Rust {}", env!("CARGO_PKG_VERSION")),
            "/sessions" => self.open_history(),
            "/settings" => self.open_settings_menu(),
            "/model" => self.open_model_picker(),
            "/effort" => self.open_effort_picker(),
            "/engine" => self.open_engine_picker(),
            "/mode" => self.open_permission_picker(),
            "/beliefs" => self.open_lore_picker(),
            "/diff" => self.open_diff(),
            "/peers" => {
                self.map_modal = true;
                self.peer_map.selected = 0;
                self.pending_peer_refresh = Some(
                    self.groups[self.active_group].active_id().unwrap_or("").to_owned(),
                );
            }
            "/split" => {
                self.split = Split::Horizontal;
                self.split_requested = true;
            }
            "/vsplit" => {
                self.split = Split::Vertical;
                self.split_requested = true;
            }
            "/pane" => {
                if let Some(target) = pane_target {
                    self.active_group = target;
                    self.focus = Focus::Prompt;
                } else {
                    self.notice = if self.pane_group_two_exists() {
                        "2 pane groups, numbered 1 and 2 · /pane <n> to focus one".into()
                    } else {
                        "One pane group · /split or /vsplit makes a second".into()
                    };
                }
            }
            "/sidebar" => {
                if let Some((visible, width)) = sidebar {
                    self.rail_visible = visible;
                    if let Some(width) = width { self.rail_width = width; }
                } else {
                    self.rail_visible = !self.rail_visible;
                }
            }
            "/detach" => self.detach_active_tab(),
            "/dir" => {
                self.notice = self.groups[self.active_group].active_id()
                    .and_then(|id| self.session_cwds.get(id))
                    .map(|cwd| format!("Session directory · {}", safe_label(&cwd.to_string_lossy())))
                    .unwrap_or_else(|| "Session directory unavailable".into());
            }
            _ => unreachable!("recognized bare DOXA command"),
        }
        true
    }

    fn key(&mut self, key: KeyEvent) -> bool {
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        let alt = key.modifiers.contains(KeyModifiers::ALT);
        if key.code == KeyCode::Char('q') && ctrl {
            if !self.diff_reject_queue.is_empty() || self.diff_reject_active.is_some()
                || self.diff_reject_feedback.is_some() {
                self.notice = "Wait for queued hunk rejections before leaving".into();
                return true;
            }
            self.should_quit = true;
            return true;
        }
        if key.code == KeyCode::Char('w') && ctrl {
            self.detach_active_tab();
            return true;
        }
        if self.active_request_index().is_some() {
            return self.request_key(key);
        }
        if self.stop_confirmation.is_some() { return self.stop_confirmation_key(key); }
        if self.chip_info.is_some() {
            if key.code == KeyCode::Esc {
                self.chip_info = None;
                self.memory_menu_pending = None;
                return true;
            }
            if let Some(info) = self.chip_info.as_mut().filter(|info|
                matches!(info.kind, "memory" | "usage" | "context" | "help")) {
                match key.code {
                    KeyCode::Up => info.scroll = info.scroll.saturating_sub(1),
                    KeyCode::Down => info.scroll = info.scroll.saturating_add(1).min(info.lines.len().saturating_sub(1)),
                    KeyCode::PageUp => info.scroll = info.scroll.saturating_sub(8),
                    KeyCode::PageDown => info.scroll = info.scroll.saturating_add(8).min(info.lines.len().saturating_sub(1)),
                    _ => return false,
                }
                return true;
            }
            return false;
        }
        if self.settings_menu.is_some() { return self.settings_menu_key(key); }
        if self.repo_picker.is_some() { return self.repo_picker_key(key); }
        if self.lore_picker.is_some() { return self.lore_picker_key(key); }
        if self.new_session.is_some() { return self.new_session_key(key); }
        if self.model_picker.is_some() { return self.model_picker_key(key); }
        if self.effort_picker.is_some() { return self.effort_picker_key(key); }
        if self.permission_picker.is_some() { return self.permission_picker_key(key); }
        if self.engine_picker { return self.engine_picker_key(key); }
        if self.action_menu {
            return self.action_key(key);
        }
        if self.history_modal {
            return self.history_key(key);
        }
        if self.queue_picker.is_some() { return self.queue_key(key); }
        if self.attach_picker.is_some() { return self.attach_picker_key(key); }
        if self.branch_picker.is_some() { return self.branch_picker_key(key); }
        if self.diff_modal {
            return self.diff_key(key);
        }
        if self.diff_pane && self.diff_reject_confirm.is_some() {
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
        if key.code == KeyCode::Char(',') && ctrl {
            self.open_settings_menu();
            return true;
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
        if key.code == KeyCode::Char('l') && alt { self.open_lore_picker(); return true; }
        if key.code == KeyCode::Char('m') && alt { self.open_model_picker(); return true; }
        if key.code == KeyCode::Char('f') && alt { self.open_effort_picker(); return true; }
        if key.code == KeyCode::Char('p') && alt { self.open_permission_picker(); return true; }
        if key.code == KeyCode::Char('e') && alt { self.open_engine_picker(); return true; }
        if key.code == KeyCode::Char('x') && alt { self.open_stop_confirmation(); return true; }
        if key.code == KeyCode::F(2) || (key.code == KeyCode::Char('g') && alt) {
            self.open_diff();
            return true;
        }
        if key.code == KeyCode::F(4) {
            if self.diff_pane {
                if self.rejections_for_target() > 0 {
                    self.notice = "Wait for queued hunk rejections before closing this diff".into();
                    return true;
                }
                self.diff_pane = false;
            } else {
                self.diff_pane = true;
                if self.layout(self.size).panes.is_none() {
                    self.diff_pane = false;
                    self.notice = "Enlarge terminal to open the diff pane".into();
                } else {
                    self.load_diff();
                }
            }
            return true;
        }
        if self.diff_pane {
            match key.code {
                KeyCode::F(5) => { self.load_diff(); return true; }
                KeyCode::Char('r' | 'R') if alt => { self.begin_diff_reject(); return true; }
                KeyCode::PageUp if alt => { self.diff_scroll = self.diff_scroll.saturating_sub(10); return true; }
                KeyCode::PageDown if alt => { self.diff_scroll = self.diff_scroll.saturating_add(10); return true; }
                KeyCode::Char('n' | 'N') if alt => { self.jump_diff(true, true); return true; }
                KeyCode::Char('b' | 'B') if alt => { self.jump_diff(true, false); return true; }
                KeyCode::Char('j' | 'J') if alt => { self.jump_diff(false, true); return true; }
                KeyCode::Char('k' | 'K') if alt => { self.jump_diff(false, false); return true; }
                _ => {}
            }
        }
        if !ctrl && !alt && !key.modifiers.contains(KeyModifiers::SHIFT) {
            let suggestions = self.slash_suggestions();
            if !suggestions.is_empty() {
                match key.code {
                    KeyCode::Up => {
                        self.slash_selected = self.slash_selected.saturating_sub(1);
                        return true;
                    }
                    KeyCode::Down => {
                        self.slash_selected = (self.slash_selected + 1).min(suggestions.len() - 1);
                        return true;
                    }
                    KeyCode::Tab => return self.complete_slash(),
                    KeyCode::Esc => {
                        self.slash_dismissed = true;
                        return true;
                    }
                    KeyCode::Enter if suggestions[self.slash_selected.min(suggestions.len() - 1)].0 != self.input => {
                        return self.complete_slash();
                    }
                    _ => {}
                }
            }
        }
        match key.code {
            KeyCode::F(3) => {
                self.rail_visible = !self.rail_visible;
                true
            }
            KeyCode::BackTab | KeyCode::Tab if key.code == KeyCode::BackTab
                || key.modifiers.contains(KeyModifiers::SHIFT) => {
                self.active_group = 1 - self.active_group;
                self.split_requested = true;
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
                self.split_requested = true;
                true
            }
            KeyCode::Char('v') if alt => {
                self.split = Split::Vertical;
                self.split_requested = true;
                true
            }
            KeyCode::Up if alt && self.focus == Focus::Prompt => {
                let target = self.groups[self.active_group]
                    .active_id()
                    .unwrap_or("")
                    .to_owned();
                if let Some(draft) = self.rejected_drafts.get_mut(&target).and_then(Vec::pop) {
                    let current = std::mem::replace(&mut self.input, draft);
                    self.input_cursor = self.input.len();
                    if !current.is_empty() {
                        self.rejected_drafts
                            .entry(target)
                            .or_default()
                            .push(current);
                    }
                    true
                } else {
                    self.adjust_split(-5)
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
                    (self.rail_selected + 1).min(self.rail_order().len().saturating_sub(1));
                true
            }
            KeyCode::Enter if self.focus == Focus::Rail => {
                self.open_selected();
                true
            }
            KeyCode::Char('[') if self.focus == Focus::Transcript => self.select_tool_section(false),
            KeyCode::Char(']') if self.focus == Focus::Transcript => self.select_tool_section(true),
            KeyCode::Enter if self.focus == Focus::Transcript => self.toggle_selected_tool_section(),
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
            KeyCode::Left if self.focus == Focus::Prompt => {
                if let Some((index, _)) = self.input[..self.input_cursor].char_indices().next_back() {
                    self.input_cursor = index;
                    true
                } else { false }
            }
            KeyCode::Right if self.focus == Focus::Prompt => {
                if let Some(ch) = self.input[self.input_cursor..].chars().next() {
                    self.input_cursor += ch.len_utf8();
                    true
                } else { false }
            }
            KeyCode::Home if self.focus == Focus::Prompt => {
                self.input_cursor = self.input[..self.input_cursor].rfind('\n').map_or(0, |i| i + 1);
                true
            }
            KeyCode::End if self.focus == Focus::Prompt => {
                self.input_cursor = self.input[self.input_cursor..].find('\n').map_or(self.input.len(), |i| self.input_cursor + i);
                true
            }
            KeyCode::Up if self.focus == Focus::Prompt => self.move_input_vertical(false),
            KeyCode::Down if self.focus == Focus::Prompt => self.move_input_vertical(true),
            KeyCode::Backspace if self.focus == Focus::Prompt => {
                if let Some((index, _)) = self.input[..self.input_cursor].char_indices().next_back() {
                    self.input.drain(index..self.input_cursor);
                    self.input_cursor = index;
                    self.slash_selected = 0;
                    self.slash_dismissed = false;
                    true
                } else { false }
            }
            KeyCode::Delete if self.focus == Focus::Prompt => {
                if let Some(ch) = self.input[self.input_cursor..].chars().next() {
                    self.input.drain(self.input_cursor..self.input_cursor + ch.len_utf8());
                    self.slash_selected = 0;
                    self.slash_dismissed = false;
                    true
                } else { false }
            }
            KeyCode::Enter if self.focus == Focus::Prompt && (key.modifiers.contains(KeyModifiers::SHIFT) || alt) => self.insert_input('\n'),
            KeyCode::Enter if self.focus == Focus::Prompt && ctrl => {
                // Ctrl+Enter and a terminal-normalized control newline can
                // share this code. Neither may submit a prompt by accident.
                self.notice = "Ctrl+Enter is ambiguous here · use Alt+Enter for a newline".into();
                true
            }
            KeyCode::Char('j') if self.focus == Focus::Prompt && ctrl => self.insert_input('\n'),
            KeyCode::Char(c) if self.focus == Focus::Prompt && !ctrl && !alt => {
                if unsafe_input_char(c) { false } else { self.insert_input(c) }
            }
            KeyCode::Enter if self.focus == Focus::Prompt => {
                if !self.input.is_empty() {
                    if self.input.contains('\n') && self.input.split_whitespace().next()
                        .is_some_and(|name| COMMANDS.iter().any(|row| row.name == name)) {
                        self.notice = "DOXA commands must be a single line".into();
                        return true;
                    }
                    if self.dispatch_prompt_command() { return true; }
                    if self.submit_local_command() { return true; }
                    if let Some(id) = self.groups[self.active_group].active_id() {
                        if self.offline_ids.contains(id) {
                            self.notice = "Archived transcript is read-only".into();
                        } else if self.input == "/peers" || self.input == "/mesh" {
                            self.map_modal = true;
                            self.peer_map.selected = 0;
                            self.pending_peer_refresh = Some(id.to_owned());
                            self.input.clear();
                            self.input_cursor = 0;
                        } else if self.input == "/msg" || self.input.starts_with("/msg ") {
                            let mut parts = self.input.splitn(3, ' ');
                            let _command = parts.next();
                            let target = parts.next().unwrap_or("");
                            let body = parts.next().unwrap_or("");
                            if target.is_empty() || body.trim().is_empty() {
                                self.notice = "Usage: /msg <session_prefix> <text>".into();
                            } else if self.pending_peer_messages.len() >= MAX_PENDING_PROMPTS {
                                self.notice = "Peer message queue full · wait for daemon".into();
                            } else {
                                self.pending_peer_messages.push((id.to_owned(), target.to_owned(), body.to_owned()));
                                self.input.clear();
                                self.input_cursor = 0;
                                self.notice = "Peer message queued".into();
                            }
                        } else if self.pending_prompts.len() < MAX_PENDING_PROMPTS {
                            self.pending_prompts
                                .push((id.to_owned(), std::mem::take(&mut self.input)));
                            self.input_cursor = 0;
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

    fn submit_local_command(&mut self) -> bool {
        if !self.input.trim_start().starts_with('/') || self.input.contains('\n') {
            return false;
        }
        let line = self.input.trim().to_owned();
        let (command, args) = line.split_once(char::is_whitespace).unwrap_or((line.as_str(), ""));
        match command {
            "/pending" if args.trim().is_empty() => {
                self.input.clear();
                self.input_cursor = 0;
                self.open_pending_picker();
                true
            }
            "/pending" => { self.notice = "Local command unavailable: /pending arguments".into(); true }
            "/attach" => { self.local_attach(args); true }
            "/branch" => {
                let target = args.trim();
                if target.split_whitespace().count() > 1 || target.len() > 200
                    || target.chars().any(unsafe_input_char) {
                    self.notice = "Usage: /branch [local-or-remote-name]".into();
                } else if let Some(id) = self.groups[self.active_group].active_id() {
                    self.pending_queue_commands.push(crate::bridge::WorkerCommand::Branch(
                        id.to_owned(), (!target.is_empty()).then(|| target.to_owned())));
                    self.input.clear();
                    self.input_cursor = 0;
                    self.notice = "Checking branch…".into();
                } else { self.notice = "Select a session before switching branch".into(); }
                true
            }
            "/rename" => { self.local_rename(args); true }
            "/clear" => { self.local_clear(args); true }
            "/usage" | "/context" => {
                if !args.trim().is_empty() {
                    self.notice = format!("Usage: {command}");
                } else {
                    let kind = if command == "/usage" { "usage" } else { "context" };
                    self.input.clear();
                    self.input_cursor = 0;
                    self.open_diagnostic(kind);
                }
                true
            }
            "/collection" => { self.local_collection(args); true }
            "/cd" => { self.local_cd(args); true }
            "/compact" => {
                let engine = self.groups[self.active_group].active_id()
                    .and_then(|id| self.session_identity.get(id))
                    .and_then(|identity| identity.0.as_deref());
                if args.trim().is_empty() && engine == Some("claude") {
                    return false; // The Claude sidecar reviews synchronously before forwarding.
                }
                self.notice = if !args.trim().is_empty() {
                    "Usage: /compact".into()
                } else {
                    "Reviewed compaction is available only for Claude sessions".into()
                };
                true
            }
            "/mesh" if !args.trim().is_empty() => {
                self.notice = "Local command unavailable: /mesh arguments".into(); true
            }
            "/mesh" => {
                self.map_modal = true;
                self.peer_map.selected = 0;
                self.pending_peer_refresh = Some(
                    self.groups[self.active_group].active_id().unwrap_or("").to_owned());
                self.input.clear();
                self.input_cursor = 0;
                true
            }
            "/msg" => { self.local_message(args); true }
            "/movepane" => {
                let target = match args.split_whitespace().collect::<Vec<_>>().as_slice() {
                    [] => 1 - self.active_group,
                    ["1"] => 0,
                    ["2"] => 1,
                    _ => { self.notice = "Usage: /movepane [1|2]".into(); return true; }
                };
                if self.move_active_tab(target) {
                    self.input.clear();
                    self.input_cursor = 0;
                }
                true
            }
            "/settings" if args.trim().is_empty() => {
                self.input.clear();
                self.input_cursor = 0;
                self.open_settings_menu();
                true
            }
            "/settings" => { self.notice = "Usage: /settings · edit native preferences in the menu".into(); true }
            "/setup" => { self.notice = "Run `doxa setup` in a shell for auth and store checks".into(); true }
            "/fleet" | "/img" | "/login"
            | "/logout" | "/doctor" | "/plugins"
            | "/reload-plugins" | "/effort"
            | "/update" => {
                self.notice = format!("Local command unavailable: {}", safe_label(command));
                true
            }
            "/search" => { self.local_search(args); true }
            "/resume" => { self.local_resume(args); true }
            "/queue" if args.trim().is_empty() => { self.open_queue(); true }
            "/queue" => { self.notice = "queue: open the picker and use X to cancel a selected item".into(); true }
            _ if COMMANDS.iter().any(|row| row.name == command) => {
                self.notice = format!("Local command unavailable: {}", safe_label(command));
                true
            }
            _ => false, // Unknown provider and plugin slash commands remain available.
        }
    }

    fn local_message(&mut self, args: &str) {
        let Some(id) = self.groups[self.active_group].active_id().map(str::to_owned) else {
            self.notice = "Select a session before messaging a peer".into();
            return;
        };
        let mut parts = args.trim().splitn(2, char::is_whitespace);
        let target = parts.next().unwrap_or("");
        let body = parts.next().unwrap_or("").trim();
        if target.is_empty() || body.is_empty() {
            self.notice = "Usage: /msg <session_prefix> <text>".into();
        } else if self.pending_peer_messages.len() >= MAX_PENDING_PROMPTS {
            self.notice = "Peer message queue full · wait for daemon".into();
        } else {
            self.pending_peer_messages.push((id, target.to_owned(), body.to_owned()));
            self.input.clear();
            self.input_cursor = 0;
            self.notice = "Peer message queued".into();
        }
    }

    fn local_attach(&mut self, args: &str) {
        let query = args.trim();
        if query.len() > 200 || query.chars().any(unsafe_input_char) {
            self.notice = "attach: query must be at most 200 bytes without control characters".into();
            return;
        }
        let live = match crate::discovery::sessions() {
            Ok(rows) => rows,
            Err(error) => { self.notice = format!("attach: discovery failed · {}", safe_label(&error.to_string())); return; }
        };
        let candidates: Vec<_> = if query.is_empty() {
            live.into_iter().filter(|session| !self.groups.iter().any(|group| group.tabs.contains(&session.id))).collect()
        } else {
            // ID matches win over titles, so a familiar ID prefix never
            // silently attaches a different session named after that prefix.
            let exact: Vec<_> = live.iter().filter(|session| session.id == query).cloned().collect();
            if !exact.is_empty() { exact } else {
                let prefixes: Vec<_> = live.iter().filter(|session| session.id.starts_with(query)).cloned().collect();
                if !prefixes.is_empty() { prefixes } else {
                    let query = query.to_lowercase();
                    live.into_iter().filter(|session| session.title.to_lowercase().contains(&query)).collect()
                }
            }
        };
        match candidates.as_slice() {
            [] => { self.notice = if query.is_empty() { "attach: no detached live sessions".into() }
                else { format!("attach: no live session matches {}", safe_label(query)) }; }
            [one] => self.attach_selected(&one.id),
            _ => {
                self.attach_picker = Some(AttachPicker { rows: candidates, query: String::new(), selected: 0 });
                if self.active_chooser_rect().is_none() {
                    self.attach_picker = None;
                    self.notice = "Enlarge active pane to choose a live session".into();
                } else {
                    self.input.clear();
                    self.input_cursor = 0;
                }
            }
        }
    }

    fn attach_matches(&self) -> Vec<usize> {
        let Some(picker) = &self.attach_picker else { return Vec::new(); };
        picker.rows.iter().enumerate().filter_map(|(index, session)|
            attach_matches(session, &picker.query).then_some(index)).collect()
    }

    fn attach_picker_key(&mut self, key: KeyEvent) -> bool {
        let len = self.attach_matches().len();
        let Some(picker) = self.attach_picker.as_mut() else { return false; };
        match key.code {
            KeyCode::Esc => self.attach_picker = None,
            KeyCode::Up => picker.selected = picker.selected.saturating_sub(1),
            KeyCode::Down => picker.selected = (picker.selected + 1).min(len.saturating_sub(1)),
            KeyCode::Backspace => { picker.query.pop(); picker.selected = 0; }
            KeyCode::Char(c) if !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) => {
                if !unsafe_input_char(c) && picker.query.len() + c.len_utf8() <= 200 {
                    picker.query.push(c);
                    picker.selected = 0;
                }
            }
            KeyCode::Enter => self.open_selected_attach(),
            _ => return false,
        }
        true
    }

    fn branch_picker_key(&mut self, key: KeyEvent) -> bool {
        let Some(picker) = self.branch_picker.as_mut() else { return false; };
        match key.code {
            KeyCode::Esc => self.branch_picker = None,
            KeyCode::Up => picker.selected = picker.selected.saturating_sub(1),
            KeyCode::Down => picker.selected = (picker.selected + 1).min(picker.branches.len().saturating_sub(1)),
            KeyCode::Enter => self.choose_branch(),
            _ => return false,
        }
        true
    }

    fn choose_branch(&mut self) {
        let Some(picker) = self.branch_picker.take() else { return; };
        if self.groups[self.active_group].active_id() != Some(picker.session_id.as_str()) {
            self.notice = "Branch choice cancelled: active session changed".into();
            return;
        }
        let Some(branch) = picker.branches.get(picker.selected) else { return; };
        if branch == &picker.base {
            self.notice = format!("branch: already based on {}", safe_label(branch));
            return;
        }
        self.pending_queue_commands.push(crate::bridge::WorkerCommand::Branch(
            picker.session_id, Some(branch.clone())));
        self.notice = format!("Checking branch {}…", safe_label(branch));
    }

    fn open_selected_attach(&mut self) {
        let Some(picker) = &self.attach_picker else { return; };
        let Some(&index) = self.attach_matches().get(picker.selected) else { return; };
        let id = picker.rows[index].id.clone();
        self.attach_picker = None;
        // Registry entries are hints: a row may have gone stale while the
        // picker was open. The bridge performs one more identity check.
        match crate::discovery::sessions() {
            Ok(live) if live.iter().any(|session| session.id == id) => self.attach_selected(&id),
            Ok(_) => self.notice = format!("attach: session is no longer live · {}", safe_label(&id)),
            Err(error) => self.notice = format!("attach: discovery failed · {}", safe_label(&error.to_string())),
        }
    }

    fn attach_selected(&mut self, id: &str) {
        for (group_index, group) in self.groups.iter_mut().enumerate() {
            if let Some(index) = group.tabs.iter().position(|tab| tab == id) {
                group.active = index;
                self.active_group = group_index;
                self.input.clear();
                self.input_cursor = 0;
                self.notice = format!("Already open · {}", safe_label(id));
                return;
            }
        }
        if self.attaching_ids.contains(id) {
            self.notice = format!("Already attaching · {}", safe_label(id));
            return;
        }
        self.attaching_ids.insert(id.to_owned());
        self.pending_attaches.push((id.to_owned(), self.active_group));
        self.input.clear();
        self.input_cursor = 0;
        self.notice = format!("Attaching · {}", safe_label(id));
    }

    pub(crate) fn has_offline_open_tabs(&self) -> bool {
        self.groups.iter().any(|group| group.tabs.iter().any(|id| self.offline_ids.contains(id)))
    }

    fn rollback_clear(&mut self, swap: ClearSwap) {
        for group in &mut self.groups {
            if let Some(index) = group.tabs.iter().position(|tab| tab == &swap.new_id) {
                group.tabs.remove(index);
                group.active = group.active.min(group.tabs.len().saturating_sub(1));
            }
        }
        let group = &mut self.groups[swap.group];
        if let Some(index) = group.tabs.iter().position(|tab| tab == &swap.old_id) {
            group.active = index;
        } else {
            let position = swap.position.min(group.tabs.len());
            group.tabs.insert(position, swap.old_id.clone());
            group.active = position;
        }
        group.scroll = 0;
        self.active_group = swap.group;
        for collection in &mut self.collections {
            if let Some(member) = collection.sessions.iter_mut().find(|member| member.as_str() == swap.new_id) {
                *member = swap.old_id.clone();
            }
        }
        self.clear_stop_after_save.retain(|id| id != &swap.old_id);
        self.pending_clear_finalizes.push(swap.new_id);
        self.notice = "Clear cancelled · tabset could not be saved; previous session preserved".into();
    }

    fn finish_clear_swap(&mut self, persisted: bool) -> bool {
        let Some(swap) = self.clear_swap.take() else { return false; };
        if persisted {
            self.clear_stop_after_save.retain(|id| id != &swap.old_id);
            self.pending_clear_finalizes.push(swap.old_id);
            self.notice = "Fresh session ready · finalizing previous session".into();
        } else {
            self.rollback_clear(swap);
        }
        true
    }

    fn local_clear(&mut self, args: &str) {
        if !args.trim().is_empty() {
            self.notice = "Usage: /clear".into();
            return;
        }
        if let Some(reason) = self.clear_preflight_error {
            self.notice = format!("clear unavailable · {reason}");
            return;
        }
        if self.launching {
            self.notice = "clear: wait for the current session launch".into();
            return;
        }
        let group = self.active_group;
        let Some(id) = self.groups[group].active_id().map(str::to_owned) else {
            self.notice = "clear: select a session first".into();
            return;
        };
        if self.offline_ids.contains(&id) {
            self.notice = "clear: archived sessions cannot be replaced".into();
            return;
        }
        if self.session_activity.get(&id).is_some_and(|(running, queued)| *running || *queued > 0)
            || self.input_requests.iter().any(|request| request.session_id == id)
            || self.pending_prompts.iter().any(|(session, _)| session == &id) {
            self.notice = "clear: wait for the current turn and queued prompts to finish".into();
            return;
        }
        let Some(engine) = self.session_identity.get(&id).and_then(|identity| identity.0.as_deref()) else {
            self.notice = "clear: session engine is unavailable".into();
            return;
        };
        let engine = match engine {
            "codex" => launch::Engine::Codex,
            "claude" => launch::Engine::Claude,
            "deepseek" => launch::Engine::DeepSeek,
            "glm" => launch::Engine::Glm,
            _ => { self.notice = "clear: session engine cannot be relaunched".into(); return; }
        };
        let Some(cwd) = self.session_cwds.get(&id).cloned().filter(|path| path.is_absolute()) else {
            self.notice = "clear: session directory is unavailable".into();
            return;
        };
        // A managed session's cwd is its private worktree. A fresh session
        // starts from the shared checkout, as the Python session factory
        // does, instead of branching from the old session's branch.
        let launch_cwd = crate::discovery::repo_root_for(&cwd).unwrap_or(cwd);
        let mut options = launch::LaunchOptions { engine, cwd: Some(launch_cwd), ..Default::default() };
        if engine == launch::Engine::Claude {
            options.claude_script = std::env::var_os("DOXA_CLAUDE_SCRIPT").map(PathBuf::from);
        }
        self.clear_pending = Some(ClearPending { old_id: id, group });
        self.pending_launches.push((options, None, group));
        self.launching = true;
        self.input.clear();
        self.input_cursor = 0;
        self.notice = "Starting a fresh session in this tab…".into();
    }

    fn local_cd(&mut self, args: &str) {
        let Some(id) = self.groups[self.active_group].active_id() else {
            self.notice = "cd: select a session first".into();
            return;
        };
        let Some(engine) = self.session_identity.get(id).and_then(|identity| identity.0.as_deref()) else {
            self.notice = "cd: session engine is unavailable".into();
            return;
        };
        if self.launching {
            self.notice = "cd: wait for the current session launch".into();
            return;
        }
        let requested = args.trim();
        if requested.is_empty() {
            self.notice = "Usage: /cd <path> — open a new session tab there".into();
            return;
        }
        if requested.len() > 4096 || requested.chars().any(unsafe_input_char) {
            self.notice = "cd: path is too long or contains control characters".into();
            return;
        }
        let path = if requested == "~" || requested.starts_with("~/") {
            let Some(home) = std::env::var_os("HOME") else {
                self.notice = "cd: home directory is unavailable".into();
                return;
            };
            PathBuf::from(home).join(requested.strip_prefix("~/").unwrap_or(""))
        } else {
            let requested = Path::new(requested);
            if requested.is_absolute() { requested.to_path_buf() }
            else {
                self.session_cwds.get(id).cloned()
                    .or_else(|| std::env::current_dir().ok()).unwrap_or_default().join(requested)
            }
        };
        let Ok(cwd) = std::fs::canonicalize(path) else {
            self.notice = "cd: directory does not exist or cannot be opened".into();
            return;
        };
        if !cwd.is_dir() {
            self.notice = "cd: target is not a directory".into();
            return;
        }
        let engine = match engine {
            "claude" => launch::Engine::Claude,
            "codex" => launch::Engine::Codex,
            "deepseek" => launch::Engine::DeepSeek,
            "glm" => launch::Engine::Glm,
            _ => {
                self.notice = "cd: session engine cannot be launched here".into();
                return;
            }
        };
        let mut options = launch::LaunchOptions { engine, cwd: Some(cwd.clone()), ..Default::default() };
        if engine == launch::Engine::Claude {
            options.claude_script = std::env::var_os("DOXA_CLAUDE_SCRIPT").map(PathBuf::from);
        }
        self.pending_launches.push((options, None, self.active_group));
        self.launching = true;
        self.input.clear();
        self.input_cursor = 0;
        self.notice = format!("Opening a new tab at {} · current session stays here", safe_label(&cwd.display().to_string()));
    }

    fn open_repo_picker(&mut self, group: usize) {
        self.active_group = group;
        let Some(id) = self.groups[group].active_id() else {
            self.notice = "Choose a session first".into();
            return;
        };
        let source = self.session_cwds.get(id).cloned();
        let current = source.as_deref().and_then(safe_repo_directory)
            .or_else(|| source.as_deref().and_then(Path::parent).and_then(safe_repo_directory));
        let Some(current_dir) = current else {
            self.notice = "Current session directory is unavailable".into();
            return;
        };
        self.chip_info = None;
        let paths = repo_directory_entries(&current_dir);
        self.repo_picker = Some(RepoPicker { current_dir, paths, selected: 0 });
        if self.active_chooser_rect().is_none() {
            self.repo_picker = None;
            self.notice = "Enlarge active pane to choose a directory".into();
        }
    }

    fn repo_picker_key(&mut self, key: KeyEvent) -> bool {
        let picker = self.repo_picker.as_mut().unwrap();
        match key.code {
            KeyCode::Esc => self.repo_picker = None,
            KeyCode::Up => picker.selected = picker.selected.saturating_sub(1),
            KeyCode::Down => picker.selected = (picker.selected + 1).min(picker.paths.len() - 1),
            KeyCode::Enter => {
                let launch_current = picker.selected == 0;
                let path = picker.paths[picker.selected].clone();
                if launch_current {
                    self.repo_picker = None;
                    if let Some(path) = path.to_str() { self.local_cd(path); }
                } else if let Some(current_dir) = safe_repo_directory(&path) {
                    picker.paths = repo_directory_entries(&current_dir);
                    picker.current_dir = current_dir;
                    picker.selected = 0;
                } else {
                    self.notice = "Directory no longer available".into();
                }
            }
            _ => return false,
        }
        true
    }

    fn local_collection(&mut self, args: &str) {
        let (verb, rest) = args.trim().split_once(char::is_whitespace)
            .map_or((args.trim(), ""), |(verb, rest)| (verb, rest.trim()));
        if verb.is_empty() || matches!(verb, "list" | "ls") {
            self.notice = if self.collections.is_empty() { "No collections yet · /collection add <name>".into() }
                else { self.collections.iter().map(|item| format!("{} ({} sessions)", item.name, item.sessions.len())).collect::<Vec<_>>().join(" · ") };
            self.input.clear();
            self.input_cursor = 0;
            return;
        }
        let active = self.groups[self.active_group].active_id().map(str::to_owned);
        let result = crate::collections::edit(&mut self.collections, verb, rest, active.as_deref());
        self.notice = match result { Ok(note) => { self.input.clear(); self.input_cursor = 0; note }, Err(error) => error };
    }

    fn local_rename(&mut self, args: &str) {
        let Some(id) = self.groups[self.active_group].active_id().map(str::to_owned) else {
            self.notice = "rename: select a tab".into();
            return;
        };
        if args.len() > 200 || args.chars().any(unsafe_input_char) {
            self.notice = "rename: name must be at most 200 bytes without control characters".into();
            return;
        }
        let name = args.trim();
        if name.is_empty() {
            self.custom_names.remove(&id);
            let automatic = self.session_identity.get(&id).and_then(|identity| identity.1.as_deref())
                .map(safe_label).unwrap_or_else(|| safe_label(&id));
            if let Some(session) = self.sessions.iter_mut().find(|session| session.id == id) {
                session.title = automatic;
            }
            self.notice = "Tab name cleared".into();
        } else {
            let name = name.to_owned();
            self.custom_names.insert(id.clone(), name.clone());
            if let Some(session) = self.sessions.iter_mut().find(|session| session.id == id) {
                session.title = name;
            }
            self.notice = "Tab renamed and pinned".into();
        }
        self.input.clear();
        self.input_cursor = 0;
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

    fn open_effort_picker(&mut self) {
        if self.size.width > 0 && (self.size.width < 29 || self.size.height < 11) {
            self.notice = "Enlarge terminal to open effort picker".into();
            return;
        }
        let Some(id) = self.groups[self.active_group].active_id().map(str::to_owned) else {
            self.notice = "Select a session to inspect its effort".into();
            return;
        };
        let Some((Some(engine), Some(model))) = self.session_identity.get(&id) else {
            self.notice = "Effort capability is unknown for this session".into();
            return;
        };
        let known = effort_choices(engine, model);
        let levels = self.catalog_efforts.get(&(engine.clone(), model.clone()))
            .map(|levels| levels.iter().filter(|level| known.contains(&level.as_str())).cloned().collect())
            .unwrap_or_else(|| known.iter().map(|level| (*level).to_owned()).collect::<Vec<_>>());
        if levels.is_empty() {
            self.notice = "Live effort change is unavailable for this session model".into();
            return;
        }
        let selected = self.session_efforts.get(&id)
            .and_then(|current| levels.iter().position(|level| level == current)).unwrap_or(0);
        self.effort_picker = Some(EffortPicker { session_id: id, engine: engine.clone(), model: model.clone(),
            levels, selected });
    }

    fn select_effort(&mut self) {
        let Some(picker) = self.effort_picker.take() else { return; };
        let Some((Some(engine), Some(model))) = self.session_identity.get(&picker.session_id) else { return; };
        if engine != &picker.engine || model != &picker.model { return; }
        let Some(chosen) = picker.levels.get(picker.selected) else { return; };
        let known = effort_choices(engine, model);
        let allowed = self.catalog_efforts.get(&(engine.clone(), model.clone()))
            .map(|levels| levels.iter().filter(|level| known.contains(&level.as_str())).cloned().collect())
            .unwrap_or_else(|| known.iter().map(|level| (*level).to_owned()).collect::<Vec<_>>());
        if !allowed.contains(chosen) { return; }
        self.pending_effort_changes.push((picker.session_id, chosen.clone()));
        self.notice = format!("Requesting {engine} effort {chosen} for this session…");
    }

    fn effort_picker_key(&mut self, key: KeyEvent) -> bool {
        let Some(picker) = self.effort_picker.as_mut() else { return false; };
        match key.code {
            KeyCode::Esc => self.effort_picker = None,
            KeyCode::Up => picker.selected = picker.selected.saturating_sub(1),
            KeyCode::Down => picker.selected = (picker.selected + 1).min(picker.levels.len().saturating_sub(1)),
            KeyCode::Enter => self.select_effort(),
            _ => return false,
        }
        true
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
        let models = vendor_models(engine);
        let model = vendor_default_model(engine).to_owned();
        let engine_id = engine_name(engine);
        // A previous account-scoped catalog must not survive a vendor
        // re-selection when a later lookup fails or the credential changes.
        self.catalog_efforts.retain(|(name, _), _| name != engine_id);
        let model_efforts = models.iter().map(|name| ((*name).to_owned(),
            effort_choices(engine_id, name).iter().map(|level| (*level).to_owned()).collect())).collect::<HashMap<_, _>>();
        let effort = self.next_efforts.get(engine_id)
            .filter(|level| effort_choices(engine_id, &model).contains(&level.as_str()))
            .cloned().or_else(|| (!models.is_empty()).then(|| "high".to_owned()));
        self.vendor_catalog_pending = None;
        let mut catalog_pending = false;
        if let Some(vendor) = match engine {
            launch::Engine::DeepSeek => Some(doxa_vendors::Vendor::DeepSeek),
            launch::Engine::Glm => Some(doxa_vendors::Vendor::Glm),
            _ => None,
        } {
            if !cfg!(test) && std::env::var(vendor.env_var()).is_ok_and(|key| !key.is_empty()) {
                let (tx, rx) = mpsc::sync_channel(1);
                std::thread::spawn(move || {
                    let result = tokio::runtime::Builder::new_current_thread().enable_all().build()
                        .ok().and_then(|runtime| runtime.block_on(doxa_vendors::catalog_models(vendor)));
                    let _ = tx.send(result);
                });
                self.vendor_catalog_pending = Some((engine, rx));
                catalog_pending = true;
            }
        }
        self.new_session = Some(NewSession { engine, model,
            models: models.iter().map(|name| (*name).to_owned()).collect(),
            model_efforts,
            catalog_note: if catalog_pending { "Checking vendor model catalog…".into() }
                else { "Static fallback; vendor catalog unavailable".into() },
            catalog_pending, effort, prompt: String::new(), field: 0 });
    }

    fn poll_vendor_catalog(&mut self) -> bool {
        let result = match self.vendor_catalog_pending.as_ref() {
            Some((engine, rx)) => match rx.try_recv() {
                Ok(result) => Some((*engine, result)),
                Err(TryRecvError::Disconnected) => Some((*engine, None)),
                Err(TryRecvError::Empty) => None,
            },
            None => None,
        };
        let Some((engine, result)) = result else { return false; };
        self.vendor_catalog_pending = None;
        let Some(form) = self.new_session.as_mut().filter(|form| form.engine == engine) else { return false; };
        form.catalog_pending = false;
        if let Some(live) = result {
            let vetted = vendor_models(engine);
            let mut model_efforts = HashMap::new();
            let mut defaults = HashMap::new();
            let mut metadata_count = 0;
            for row in live {
                let known = vetted.contains(&row.id.as_str());
                let mut levels = if !row.effort_metadata_present && known {
                    effort_choices(engine_name(engine), &row.id).iter().map(|level| (*level).to_owned()).collect::<Vec<_>>()
                } else { row.efforts.clone() };
                if row.effort_metadata_present && !row.efforts.is_empty() {
                    metadata_count += 1;
                    // The DeepSeek catalogue omits `none`, which disables thinking;
                    // only the measured legacy models may offer it.
                    if engine == launch::Engine::DeepSeek && known && !levels.iter().any(|level| level == "none") {
                        levels.insert(0, "none".into());
                    }
                }
                if !levels.is_empty() {
                    if let Some(default) = row.default_effort { defaults.insert(row.id.clone(), default); }
                    model_efforts.insert(row.id, levels);
                }
            }
            form.models = model_efforts.keys().cloned().collect();
            form.models.sort();
            form.model_efforts = model_efforts;
            self.catalog_efforts.retain(|(name, _), _| name != engine_name(engine));
            self.catalog_efforts.extend(form.model_efforts.iter().map(|(model, levels)|
                ((engine_name(engine).to_owned(), model.clone()), levels.clone())));
            form.catalog_note = if form.models.is_empty() {
                "Live catalog has no models with verified effort support; choose another engine or retry later".into()
            } else if metadata_count > 0 {
                "Live vendor catalog · per-model effort where available; known models use measured fallback".into()
            } else {
                "Live vendor catalog · measured effort fallback for known models".into()
            };
            if !form.models.contains(&form.model) {
                form.model = form.models.first().cloned().unwrap_or_default();
            }
            let levels = form.model_efforts.get(&form.model).cloned().unwrap_or_default();
            if form.effort.as_ref().is_none_or(|level| !levels.contains(level)) {
                form.effort = defaults.get(&form.model).cloned()
                    .or_else(|| levels.iter().find(|level| *level == "high").cloned())
                    .or_else(|| levels.first().cloned());
            }
        } else {
            form.catalog_note = "Static fallback; vendor catalog unavailable".into();
        }
        true
    }

    fn new_session_key(&mut self, key: KeyEvent) -> bool {
        let form = self.new_session.as_mut().unwrap();
        let vendor = !vendor_models(form.engine).is_empty();
        let fields = if vendor { 3 } else { 2 };
        let prompt_field = fields - 1;
        match key.code {
            KeyCode::Esc => self.new_session = None,
            KeyCode::Tab | KeyCode::Down => form.field = (form.field + 1) % fields,
            KeyCode::BackTab | KeyCode::Up => form.field = (form.field + fields - 1) % fields,
            KeyCode::Left | KeyCode::Right if vendor && form.field <= 1 => {
                if form.field == 0 {
                    let choices = &form.models;
                    if choices.is_empty() { return true; }
                    let current = choices.iter().position(|model| *model == form.model).unwrap_or(0);
                    let next = if key.code == KeyCode::Right { (current + 1) % choices.len() }
                        else { (current + choices.len() - 1) % choices.len() };
                    form.model = choices[next].clone();
                    // Discard an effort no longer supported by the new model.
                    let levels = form.model_efforts.get(&form.model).cloned().unwrap_or_default();
                    if form.effort.as_ref().is_none_or(|level| !levels.contains(level)) {
                        form.effort = levels.iter().find(|level| *level == "high").cloned()
                            .or_else(|| levels.first().cloned());
                    }
                } else {
                    let levels = form.model_efforts.get(&form.model).map(Vec::as_slice).unwrap_or(&[]);
                    if levels.is_empty() { form.effort = None; return true; }
                    let current = form.effort.as_deref().and_then(|level| levels.iter().position(|x| *x == level)).unwrap_or(0);
                    let next = if key.code == KeyCode::Right { (current + 1) % levels.len() }
                        else { (current + levels.len() - 1) % levels.len() };
                    form.effort = Some(levels[next].clone());
                }
            }
            KeyCode::Backspace => {
                if form.field == prompt_field { form.prompt.pop(); }
                else if !vendor && form.field == 0 { form.model.pop(); }
            }
            KeyCode::Char(c) if !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                && !c.is_control() => {
                if form.field == prompt_field {
                    if form.prompt.len() + c.len_utf8() <= MAX_INPUT_BYTES { form.prompt.push(c); }
                } else if !vendor && form.field == 0 && form.model.len() + c.len_utf8() <= 128 { form.model.push(c); }
            }
            KeyCode::Enter if form.field < prompt_field => form.field += 1,
            KeyCode::Enter => {
                if self.launching {
                    self.notice = "Session launch already in progress".into();
                    return true;
                }
                if vendor && form.catalog_pending {
                    self.notice = "Waiting for vendor model catalog".into();
                    return true;
                }
                if vendor && form.models.is_empty() {
                    self.notice = "No verified models with effort capability are available".into();
                    return true;
                }
                let form = self.new_session.take().unwrap();
                let mut options = launch::LaunchOptions { engine: form.engine, ..Default::default() };
                if !form.model.trim().is_empty() { options.model = Some(form.model.trim().to_owned()); }
                if vendor {
                    let allowed = form.model_efforts.get(&form.model).map(Vec::as_slice).unwrap_or(&[]);
                    if !form.models.contains(&form.model) ||
                        form.effort.as_ref().is_none_or(|level| !allowed.contains(level)) {
                        self.notice = "Model or effort capability changed; session was not started".into();
                        return true;
                    }
                    options.effort = form.effort;
                }
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
            if self.history_resume {
                return (query.is_empty() || session.id.to_lowercase().starts_with(&query)).then_some(index);
            }
            let mut start = session.transcript.len().saturating_sub(16 * 1024);
            while !session.transcript.is_char_boundary(start) { start += 1; }
            if query.is_empty() || session.title.to_lowercase().contains(&query)
                || session.id.to_lowercase().contains(&query)
                || self.history_scanned_matches.get(&session.id).is_some_and(|scanned| scanned == &query)
                || session.transcript[start..].to_lowercase().contains(&query) {
                Some(index)
            } else { None }
        }).take(128).collect()
    }

    fn history_snippets(&self, id: &str) -> &[String] {
        if self.history_resume || self.history_scanned_matches.get(id)
            != Some(&self.history_query.to_lowercase()) { return &[]; }
        self.history_entries.get(id).map_or(&[], |entry| entry.search_snippets.as_slice())
    }

    /// One session header followed by at most two indexed excerpts. Row
    /// positions are shared by paint and mouse hit testing.
    fn history_rows(&self, visible: usize) -> Vec<(usize, bool, String)> {
        let matches = self.history_matches();
        let selected_row = matches.iter().take(self.history_selected).map(|&index|
            1 + self.history_snippets(&self.sessions[index].id).len().min(2)).sum();
        let start = chooser_visible_start(&self.chooser_view_start, selected_row, visible);
        let mut rows = Vec::new();
        let mut visual_row = 0;
        for (position, &index) in matches.iter().enumerate() {
            if rows.len() >= visible { break; }
            let session = &self.sessions[index];
            if visual_row >= start {
                let label = format!(" {} {} · {}{}", if position == self.history_selected { '›' } else { ' ' },
                    safe_label(&session.title), safe_label(&session.id),
                    if self.offline_ids.contains(&session.id) { " · archived" } else { "" });
                rows.push((position, true, label));
            }
            visual_row += 1;
            for snippet in self.history_snippets(&session.id).iter().take(2) {
                if rows.len() >= visible { break; }
                if visual_row >= start {
                    rows.push((position, false, format!("    ↳ {}", safe_label(snippet))));
                }
                visual_row += 1;
            }
        }
        rows
    }

    fn history_fits(&self) -> bool {
        let layout = self.layout(self.size);
        let pane = layout.panes.map_or(layout.body, |panes| panes[self.active_group]);
        pane.width >= 20 && pane.height >= 11
    }

    fn open_history(&mut self) {
        if !self.history_fits() {
            self.notice = "Enlarge active pane to search sessions".into();
            return;
        }
        self.prune_unopened_history();
        self.history_modal = true;
        self.history_resume = false;
        self.history_explicit = false;
        self.history_scan_query = None;
        self.history_query_due = None;
        self.history_query.clear();
        self.history_selected = 0;
        if self.active_chooser_rect().is_none() {
            self.history_modal = false;
            self.notice = "Enlarge active pane to search sessions".into();
            return;
        }
        if self.history_pending.is_none() {
            self.start_history_inventory();
        }
    }

    fn start_history_inventory(&mut self) {
        let (tx, rx) = mpsc::sync_channel(1);
        self.history_pending = Some(rx);
        self.history_scan_query = None;
        std::thread::spawn(move || { let _ = tx.send(history::discover()); });
    }

    fn local_search(&mut self, args: &str) {
        let query = args.trim();
        if query.len() > 200 || query.chars().any(unsafe_input_char) {
            self.notice = "search: query must be at most 200 bytes without control characters".into();
            return;
        }
        self.input.clear();
        self.input_cursor = 0;
        self.open_history();
        if self.history_modal {
            self.history_query = query.to_owned();
            self.schedule_history_query(Instant::now());
        }
    }

    fn schedule_history_query(&mut self, now: Instant) {
        if self.history_resume { return; }
        if self.history_query.trim().is_empty() {
            // /search without a query and deleting the last search character
            // both need the recent inventory, not a cancelled query receiver.
            if self.history_pending.is_none() || self.history_scan_query.is_some() {
                self.start_history_inventory();
            }
            self.history_query_due = None;
            self.prune_unopened_history();
            return;
        }
        // Dropping the receiver cancels delivery from an older worker. Its
        // bounded file/sidecar work may finish, but can no longer paint UI.
        self.history_pending = None;
        self.history_scan_query = None;
        self.prune_unopened_history();
        self.history_query_due = (!self.history_query.trim().is_empty()).then_some(now + SEARCH_DEBOUNCE);
    }

    fn prune_unopened_history(&mut self) {
        // Search hits are display cache, not tabs. Keep a bounded recent
        // inventory so reopening search can reuse results, while repeated
        // distinct queries cannot retain unbounded transcript tails.
        const MAX_CACHED_ARCHIVED: usize = 64;
        let selected_id = self.history_modal.then(|| self.history_matches()
            .get(self.history_selected).map(|&index| self.sessions[index].id.clone())).flatten();
        let open: HashSet<String> = self.groups.iter()
            .flat_map(|group| group.tabs.iter().cloned()).collect();
        let unopened: Vec<_> = self.sessions.iter().filter(|session|
            self.offline_ids.contains(&session.id) && !open.contains(&session.id))
            .map(|session| session.id.clone()).collect();
        let excess = unopened.len().saturating_sub(MAX_CACHED_ARCHIVED);
        let evict: HashSet<_> = unopened.into_iter()
            .filter(|id| selected_id.as_ref() != Some(id)).take(excess).collect();
        if evict.is_empty() { return; }
        self.sessions.retain(|session| !evict.contains(&session.id));
        self.offline_ids.retain(|id| !evict.contains(id));
        self.history_entries.retain(|id, _| !evict.contains(id));
        self.history_scanned_matches.retain(|id, _| !evict.contains(id));
        if let Some(id) = selected_id {
            self.history_selected = self.history_matches().iter()
                .position(|&index| self.sessions[index].id == id).unwrap_or(0);
        }
    }

    fn cancel_history_query(&mut self) {
        self.history_pending = None;
        self.history_scan_query = None;
        self.history_query_due = None;
    }

    fn start_due_history_query(&mut self, now: Instant) -> bool {
        if !self.history_modal || self.history_resume || self.history_pending.is_some()
            || !self.history_query_due.is_some_and(|due| now >= due)
            || self.history_search_inflight.load(Ordering::Acquire) >= MAX_SEARCH_WORKERS {
            return false;
        }
        self.history_query_due = None;
        let query = self.history_query.clone();
        let cwd = self.groups[self.active_group].active_id()
            .and_then(|id| self.session_cwds.get(id)).cloned()
            .or_else(|| std::env::current_dir().ok()).unwrap_or_default();
        let (tx, rx) = mpsc::sync_channel(1);
        self.history_pending = Some(rx);
        self.history_scan_query = Some(query.to_lowercase());
        let in_flight = self.history_search_inflight.clone();
        in_flight.fetch_add(1, Ordering::AcqRel);
        std::thread::spawn(move || {
            let found = history::discover_query(&query, &cwd);
            in_flight.fetch_sub(1, Ordering::AcqRel);
            let _ = tx.send(found);
        });
        true
    }

    fn local_resume(&mut self, args: &str) {
        let query = args.trim();
        if !query.is_empty() && !crate::discovery::valid_id(query) {
            self.notice = "resume: enter a valid session ID or prefix".into();
            return;
        }
        self.input.clear();
        self.input_cursor = 0;
        // A full ID naming a live daemon is immediately attachable even if
        // that daemon has not yet been indexed into transcript history.
        if !query.is_empty() && crate::discovery::sessions().is_ok_and(|rows| rows.iter().any(|row| row.id == query)) {
            self.attach_selected(query);
            return;
        }
        self.open_history();
        if !self.history_modal { return; }
        self.history_resume = true;
        self.history_explicit = !query.is_empty();
        self.history_query = query.to_owned();
        // Explicit IDs search the full bounded transcript inventory rather
        // than only the recent-history window.
        if !query.is_empty() {
            let (tx, rx) = mpsc::sync_channel(1);
            self.history_pending = Some(rx);
            let prefix = query.to_owned();
            std::thread::spawn(move || { let _ = tx.send(history::discover_prefix(&prefix)); });
        }
    }

    fn open_queue(&mut self) {
        let Some(id) = self.groups[self.active_group].active_id().map(str::to_owned) else {
            self.notice = "Select a live session to inspect its queue".into();
            return;
        };
        if self.offline_ids.contains(&id) {
            self.notice = "Archived sessions have no live prompt queue".into();
            return;
        }
        self.input.clear();
        self.input_cursor = 0;
        self.queue_picker = Some(QueuePicker { session_id: id.clone(), rows: Vec::new(),
            selected: 0, loading: true, cancelling: None });
        if self.active_chooser_rect().is_none() {
            self.queue_picker = None;
            self.notice = "Enlarge active pane to inspect queued prompts".into();
            return;
        }
        self.pending_queue_commands.push(crate::bridge::WorkerCommand::QueueList(id));
    }

    fn queue_key(&mut self, key: KeyEvent) -> bool {
        let Some(picker) = self.queue_picker.as_mut() else { return false; };
        match key.code {
            KeyCode::Esc => self.queue_picker = None,
            KeyCode::Up => picker.selected = picker.selected.saturating_sub(1),
            KeyCode::Down => picker.selected = (picker.selected + 1).min(picker.rows.len().saturating_sub(1)),
            KeyCode::Char('r') | KeyCode::Char('R') => {
                picker.loading = true;
                self.pending_queue_commands.push(crate::bridge::WorkerCommand::QueueList(picker.session_id.clone()));
            }
            KeyCode::Char('x') | KeyCode::Char('X') | KeyCode::Delete => self.cancel_selected_queue(),
            _ => return false,
        }
        true
    }

    fn cancel_selected_queue(&mut self) {
        let Some(picker) = self.queue_picker.as_mut() else { return; };
        if picker.loading || picker.cancelling.is_some() { return; }
        let Some(row) = picker.rows.get(picker.selected) else { return; };
        let id = row.id.clone();
        if picker.rows.iter().filter(|row| row.id == id).count() != 1 {
            self.notice = "Duplicate queue ID; cancellation is unsafe".into();
            return;
        }
        picker.cancelling = Some(id.clone());
        self.pending_queue_commands.push(crate::bridge::WorkerCommand::QueueCancel(picker.session_id.clone(), id));
        self.notice = "Cancelling selected queued prompt…".into();
    }

    fn open_settings_menu(&mut self) {
        match crate::operations::native_settings() {
            Ok(rows) => {
                self.settings_menu = Some(SettingsMenu { rows, selected: 0, linger_draft: None });
                if self.active_chooser_rect().is_none() {
                    self.settings_menu = None;
                    self.notice = "Enlarge active pane to edit settings".into();
                }
            }
            Err(error) => self.notice = format!("Settings unavailable: {}", safe_label(&error.to_string())),
        }
    }

    fn settings_change(&mut self, key: &str, value: Option<&str>) {
        match crate::operations::settings_change(key, value) {
            Ok(message) => {
                self.notice = message;
                match crate::operations::native_settings() {
                    Ok(rows) => if let Some(menu) = &mut self.settings_menu {
                        menu.rows = rows;
                        menu.linger_draft = None;
                    },
                    Err(error) => {
                        self.settings_menu = None;
                        self.notice = format!("Setting saved; refresh failed: {}", safe_label(&error.to_string()));
                    }
                }
            }
            Err(error) => self.notice = format!("Setting unchanged: {}", safe_label(&error.to_string())),
        }
    }

    fn settings_menu_key(&mut self, key: KeyEvent) -> bool {
        let Some(menu) = self.settings_menu.as_mut() else { return false; };
        if let Some(draft) = &mut menu.linger_draft {
            match key.code {
                KeyCode::Esc => menu.linger_draft = None,
                KeyCode::Backspace => { draft.pop(); },
                KeyCode::Char(ch) if (ch.is_ascii_digit() || ch == '.') && draft.len() < 24 => draft.push(ch),
                KeyCode::Enter => {
                    let value = draft.clone();
                    self.settings_change("linger_secs", Some(&value));
                }
                _ => {}
            }
            return true;
        }
        match key.code {
            KeyCode::Esc => self.settings_menu = None,
            KeyCode::Up | KeyCode::BackTab => menu.selected = menu.selected.saturating_sub(1),
            KeyCode::Down | KeyCode::Tab => menu.selected = (menu.selected + 1).min(1),
            KeyCode::Char('u') | KeyCode::Delete => {
                let selected = menu.selected;
                if menu.rows[selected].1 {
                    self.notice = "Environment override is active; unset it before editing".into();
                } else {
                    self.settings_change(if selected == 0 { "linger_secs" } else { "worktree_per_session" }, None);
                }
            }
            KeyCode::Enter | KeyCode::Char(' ') => {
                let selected = menu.selected;
                if menu.rows[selected].1 {
                    self.notice = "Environment override is active; unset it before editing".into();
                } else if selected == 0 {
                    menu.linger_draft = Some(menu.rows[0].0.split(' ').next().unwrap_or("120").to_owned());
                } else {
                    let on = menu.rows[1].0.starts_with("on ");
                    self.settings_change("worktree_per_session", Some(if on { "off" } else { "on" }));
                }
            }
            _ => {}
        }
        true
    }

    fn open_lore_picker(&mut self) {
        self.open_lore_picker_mode(false);
    }

    fn open_pending_picker(&mut self) {
        self.open_lore_picker_mode(true);
    }

    fn open_lore_picker_mode(&mut self, proposal_mode: bool) {
        let cwd = self.groups[self.active_group].active_id()
            .and_then(|id| self.session_cwds.get(id))
            .map(|path| path.to_string_lossy().into_owned())
            .or_else(|| std::env::current_dir().ok().map(|path| path.to_string_lossy().into_owned()))
            .unwrap_or_default();
        self.lore_picker = Some(LorePicker {
            session_id: self.groups[self.active_group].active_id().map(str::to_owned),
            query: String::new(), rows: Vec::new(), selected: 0, offset: 0,
            proposals: Vec::new(), proposal_mode, review: None,
            review_scroll: 0, review_seen: 0, review_width: 0,
            armed_resolution: None, can_resolve: false, resolving: false, cwd: cwd.clone(),
            belief_review: None, can_act_on_beliefs: false, belief_action: None,
            belief_note: String::new(), retract_armed: false, belief_acting: false,
            result_status: None,
            evidence: None, status: String::new(), pending: None,
        });
        if proposal_mode { self.load_lore(lore_picker::Query::Proposals(cwd, 0)); }
        else { self.load_lore(lore_picker::Query::Beliefs(0)); }
    }

    fn load_lore(&mut self, query: lore_picker::Query) {
        let Some(picker) = &mut self.lore_picker else { return; };
        picker.resolving = matches!(&query, lore_picker::Query::Resolve(..) | lore_picker::Query::BeliefAction(..));
        picker.belief_acting = matches!(&query, lore_picker::Query::BeliefAction(..));
        picker.status = if picker.belief_acting { "Applying belief action with LORE…" }
            else if picker.resolving { "Resolving this proposal with LORE…" }
            else { "Loading from LORE…" }.into();
        picker.pending = None;
        let python = std::env::var_os("DOXA_LORE_PYTHON")
            .map(PathBuf::from).unwrap_or_else(|| PathBuf::from("python3"));
        let (tx, rx) = mpsc::sync_channel(1);
        picker.pending = Some(rx);
        std::thread::spawn(move || {
            let _ = tx.send(lore_picker::fetch(&python, query));
        });
    }

    /// The gallery uses this same state path with deterministic counts. Live
    /// values arrive only through the read-only LORE sidecar query below.
    pub fn set_lore_memory_usage(&mut self, id: &str, project_chars: u64, project_cap_chars: u64,
                                 user_chars: u64, user_cap_chars: u64) {
        let usage = doxa_lore::MemoryUsage { project_chars, project_cap_chars, user_chars, user_cap_chars };
        self.memory_cache.insert(id.to_owned(), (Some(usage), Instant::now()));
        self.memory_repo.insert(id.to_owned(), true);
    }

    /// Inject a deterministic repository snapshot for gallery fixtures. Live
    /// sessions receive this state from the background Git probe instead.
    pub fn set_repo_status(&mut self, id: &str, status: doxa_worktrees::RepoStatus) {
        self.repo_cache.insert(id.to_owned(), (Some(status), Instant::now()));
    }

    /// Deterministic gallery state for the read-only memory menu. Live menus
    /// always use the LORE sidecar through `open_memory_menu`.
    #[doc(hidden)]
    pub fn show_memory_menu_fixture(&mut self, group: usize, user: &[&str], project: &[&str], beliefs: &[&str]) {
        if group >= self.groups.len() { return; }
        self.open_chip_info("memory", group);
        let Some(info) = self.chip_info.as_mut() else { return; };
        info.owner = self.groups[group].active_id().and_then(|id|
            self.session_cwds.get(id).and_then(|cwd| cwd.to_str()).map(|cwd| (id.to_owned(), cwd.to_owned())));
        let mut lines = vec!["## User memory".to_owned()];
        lines.extend(user.iter().take(8).map(|line| clipped_title(line, 120).0));
        lines.extend([String::new(), "## Project memory".to_owned()]);
        lines.extend(project.iter().take(8).map(|line| clipped_title(line, 120).0));
        lines.extend([String::new(), "## Global active LORE beliefs · retrieved on demand".to_owned()]);
        lines.extend(beliefs.iter().take(8).map(|line| clipped_title(line, 120).0));
        info.lines = lines;
        info.scroll = 0;
        self.memory_menu_pending = None;
    }

    fn poll_memory(&mut self) -> bool {
        let mut changed = false;
        if let Some((id, cwd, receiver)) = self.memory_pending.take() {
            match receiver.try_recv() {
                Ok(result) => {
                    if self.session_cwds.get(&id).and_then(|path| path.to_str()) == Some(cwd.as_str()) {
                        let mut repo_changed = false;
                        let usage = result.map(|(usage, repo)| {
                            repo_changed = self.memory_repo.insert(id.clone(), repo) != Some(repo);
                            usage
                        });
                        changed = repo_changed || self.memory_cache.get(&id).is_none_or(|(old, _)| *old != usage);
                        self.memory_cache.insert(id, (usage, Instant::now()));
                    }
                }
                Err(TryRecvError::Disconnected) => {
                    if self.session_cwds.get(&id).and_then(|path| path.to_str()) == Some(cwd.as_str()) {
                        changed = self.memory_cache.get(&id).is_none_or(|(old, _)| old.is_some());
                        self.memory_cache.insert(id, (None, Instant::now()));
                    }
                }
                Err(TryRecvError::Empty) => self.memory_pending = Some((id, cwd, receiver)),
            }
        }
        if self.memory_pending.is_some() { return changed; }
        // Query the active pane first. The other pane is refreshed once the
        // first query completes; neither query blocks input or redraw.
        for group in [self.active_group, 1 - self.active_group] {
            let Some(id) = self.groups[group].active_id().map(str::to_owned) else { continue; };
            if self.offline_ids.contains(&id) { continue; }
            let Some(cwd) = self.session_cwds.get(&id).and_then(|path| path.to_str()).map(str::to_owned) else { continue; };
            if self.memory_cache.get(&id).is_some_and(|(_, checked)| checked.elapsed() < Duration::from_secs(60)) {
                continue;
            }
            let python = std::env::var_os("DOXA_LORE_PYTHON")
                .map(PathBuf::from).unwrap_or_else(|| PathBuf::from("python3"));
            let (tx, rx) = mpsc::sync_channel(1);
            self.memory_pending = Some((id, cwd.clone(), rx));
            std::thread::spawn(move || {
                let (scope, repo) = crate::memory_menu::scope_path(Path::new(&cwd));
                let usage = scope.to_str().and_then(|scope| doxa_lore::LoreClient::spawn(&python, Duration::from_secs(2))
                    .and_then(|mut lore| lore.memory_usage(scope)).ok())
                    .map(|usage| (usage, repo));
                let _ = tx.send(usage);
            });
            break;
        }
        changed
    }

    fn poll_repo(&mut self) -> bool {
        let mut changed = false;
        if let Some((id, cwd, epoch, receiver)) = self.repo_pending.take() {
            match receiver.try_recv() {
                Ok(status) => {
                    if self.session_cwds.get(&id) == Some(&cwd)
                        && self.repo_epoch.get(&id).copied().unwrap_or_default() == epoch {
                        changed = self.repo_cache.get(&id).is_none_or(|(old, _)| *old != status);
                        self.repo_cache.insert(id, (status, Instant::now()));
                    }
                }
                Err(TryRecvError::Disconnected) => {
                    if self.session_cwds.get(&id) == Some(&cwd)
                        && self.repo_epoch.get(&id).copied().unwrap_or_default() == epoch {
                        changed = self.repo_cache.get(&id).is_some_and(|(old, _)| old.is_some());
                        self.repo_cache.insert(id, (None, Instant::now()));
                    }
                }
                Err(TryRecvError::Empty) => self.repo_pending = Some((id, cwd, epoch, receiver)),
            }
        }
        if self.repo_pending.is_some() { return changed; }
        for group in [self.active_group, 1 - self.active_group] {
            let Some(id) = self.groups[group].active_id().map(str::to_owned) else { continue; };
            if self.offline_ids.contains(&id) { continue; }
            let Some(cwd) = self.session_cwds.get(&id).cloned() else { continue; };
            if self.repo_cache.get(&id).is_some_and(|(_, checked)| checked.elapsed() < Duration::from_secs(5)) {
                continue;
            }
            let epoch = self.repo_epoch.get(&id).copied().unwrap_or_default();
            let (tx, rx) = mpsc::sync_channel(1);
            self.repo_pending = Some((id, cwd.clone(), epoch, rx));
            std::thread::spawn(move || { let _ = tx.send(doxa_worktrees::repo_status(&cwd)); });
            break;
        }
        changed
    }

    fn poll_memory_menu(&mut self) -> bool {
        let Some((id, cwd, receiver)) = self.memory_menu_pending.take() else { return false; };
        match receiver.try_recv() {
            Ok(result) => {
                if self.groups[self.active_group].active_id() != Some(id.as_str())
                    || self.session_cwds.get(&id).and_then(|path| path.to_str()) != Some(cwd.as_str()) {
                    return false;
                }
                if let Some(info) = self.chip_info.as_mut().filter(|info| info.kind == "memory") {
                    info.lines = match result {
                        Ok(lines) => lines,
                        Err(message) => vec![message.to_owned()],
                    };
                    info.scroll = 0;
                    return true;
                }
                false
            }
            Err(TryRecvError::Empty) => {
                self.memory_menu_pending = Some((id, cwd, receiver));
                false
            }
            Err(TryRecvError::Disconnected) => {
                if self.groups[self.active_group].active_id() != Some(id.as_str())
                    || self.session_cwds.get(&id).and_then(|path| path.to_str()) != Some(cwd.as_str()) {
                    return false;
                }
                if let Some(info) = self.chip_info.as_mut().filter(|info| info.kind == "memory") {
                    info.lines = vec!["LORE unavailable".to_owned()];
                    info.scroll = 0;
                    return true;
                }
                false
            },
        }
    }

    fn poll_lore(&mut self) -> bool {
        let Some(picker) = &mut self.lore_picker else { return false; };
        let Some(receiver) = &picker.pending else { return false; };
        let result = match receiver.try_recv() {
            Ok(result) => result,
            Err(TryRecvError::Empty) => return false,
            Err(TryRecvError::Disconnected) => Err("LORE worker unavailable"),
        };
        picker.pending = None;
        let was_resolving = picker.resolving;
        let was_belief_acting = picker.belief_acting;
        picker.resolving = false;
        picker.belief_acting = false;
        let mut urgent_resolution = false;
        let mut refresh_after_action = false;
        match result {
            Ok(lore_picker::ResultPage::Beliefs(rows)) => {
                picker.rows = rows;
                picker.selected = 0;
                picker.evidence = None;
                picker.belief_review = None;
                picker.can_act_on_beliefs = false;
                picker.belief_action = None;
                picker.retract_armed = false;
                picker.status = picker.result_status.take().unwrap_or_else(||
                    if picker.rows.is_empty() { "No active beliefs on this page" } else { "Active beliefs · newest first" }.into());
            }
            Ok(lore_picker::ResultPage::Search(hit)) => {
                picker.rows = hit.map(|hit| lore_picker::Belief {
                    id: hit.id, subject: "Search match".into(), claim: hit.claim,
                    truncated: hit.claim_truncated, confidence: hit.confidence,
                    evidence_count: None,
                }).into_iter().collect();
                picker.selected = 0;
                picker.evidence = None;
                picker.belief_review = None;
                picker.can_act_on_beliefs = false;
                picker.belief_action = None;
                picker.retract_armed = false;
                picker.status = if picker.rows.is_empty() { "No active belief matched" } else { "LORE search result · cite as a claim" }.into();
            }
            Ok(lore_picker::ResultPage::Evidence(id, rows)) => {
                if picker.rows.get(picker.selected).is_some_and(|row| row.id == id) {
                    picker.evidence = Some((id, rows));
                    picker.status = "Evidence trail · read only".into();
                }
            }
            Ok(lore_picker::ResultPage::Proposals(rows)) => {
                picker.proposals = rows;
                picker.selected = 0;
                picker.review = None;
                picker.armed_resolution = None;
                picker.status = if picker.proposals.is_empty() { "No staged proposals on this page" }
                    else { "Staged proposals · select one to read its complete raw contents" }.into();
            }
            Ok(lore_picker::ResultPage::Review(review, can_resolve)) => {
                if picker.proposal_mode && picker.proposals.iter().any(|row| row.pid == review.pid()) {
                    picker.review = Some(review);
                    picker.review_scroll = 0;
                    picker.review_seen = 0;
                    picker.review_width = 0;
                    picker.armed_resolution = None;
                    picker.can_resolve = can_resolve;
                    picker.status = if can_resolve {
                        "Read the complete raw proposal; A approve or R reject after reaching the end"
                    } else { "Read only · installed LORE lacks atomic reviewed resolution" }.into();
                }
            }
            Ok(lore_picker::ResultPage::Resolved(resolution)) => {
                picker.review = None;
                picker.armed_resolution = None;
                picker.status = match resolution {
                    doxa_lore::PendingResolution::Approved => "Proposal approved and archived".into(),
                    doxa_lore::PendingResolution::Rejected => "Proposal rejected and archived".into(),
                    doxa_lore::PendingResolution::Refused { code, applied: true } =>
                        { urgent_resolution = true; format!("Applied, but archive failed ({code}); do not retry automatically") },
                    doxa_lore::PendingResolution::Refused { code, applied: false } =>
                        format!("Resolution refused: {code}"),
                };
                picker.proposals.clear();
            }
            Ok(lore_picker::ResultPage::BeliefReview(review, can_act)) => {
                if !picker.proposal_mode && picker.rows.get(picker.selected).is_some_and(|row| row.id == review.id()) {
                    picker.belief_review = Some(review);
                    picker.review_scroll = 0;
                    picker.review_seen = 0;
                    picker.review_width = 0;
                    picker.belief_action = None;
                    picker.belief_note.clear();
                    picker.retract_armed = false;
                    picker.can_act_on_beliefs = can_act;
                    picker.status = if can_act { "Read the complete belief, then choose C confirmed, X contradicted, S stale, or R retract" }
                        else { "Read only · installed LORE lacks reviewed belief actions" }.into();
                } else {
                    picker.status = "Selection changed; reopen the exact belief review".into();
                    picker.can_act_on_beliefs = false;
                }
            }
            Ok(lore_picker::ResultPage::BeliefActed(result)) => {
                use doxa_lore::BeliefStatus;
                let status = match result.status {
                    BeliefStatus::Active => "active",
                    BeliefStatus::Dormant => "dormant",
                    BeliefStatus::Retracted => "retracted",
                };
                picker.result_status = Some(format!("Belief action applied · {status} · {} confirmed, {} contradicted, {} stale",
                    result.confirmed, result.contradicted, result.stale));
                picker.belief_review = None;
                picker.belief_action = None;
                picker.belief_note.clear();
                picker.retract_armed = false;
                picker.can_act_on_beliefs = false;
                refresh_after_action = true;
            }
            Err(message) => {
                picker.status = if was_belief_acting {
                    message.into()
                } else if was_resolving {
                    urgent_resolution = true;
                    "Resolution outcome unknown; inspect LORE pending and archive before retrying".into()
                } else { message.into() };
                if picker.proposal_mode { picker.review = None; picker.armed_resolution = None; }
                else {
                    picker.can_act_on_beliefs = false;
                    picker.belief_action = None;
                    picker.retract_armed = false;
                    picker.evidence = None;
                }
            }
        }
        if urgent_resolution { self.notice = picker.status.clone(); }
        if was_belief_acting && !refresh_after_action { self.notice = picker.status.clone(); }
        if refresh_after_action {
            let offset = picker.offset;
            self.notice = picker.result_status.clone().unwrap_or_default();
            if let Some(id) = picker.session_id.as_ref().filter(|id|
                self.session_cwds.get(*id).and_then(|path| path.to_str()) == Some(picker.cwd.as_str())) {
                self.memory_cache.remove(id);
                self.session_telemetry.entry(id.clone()).or_default().lore = None;
                self.pending_queue_commands.push(crate::bridge::WorkerCommand::Status(id.clone()));
            }
            self.load_lore(lore_picker::Query::Beliefs(offset));
        }
        true
    }

    fn lore_picker_key(&mut self, key: KeyEvent) -> bool {
        let review_area = self.active_chooser_rect();
        let picker = self.lore_picker.as_mut().unwrap();
        if picker.resolving { return true; }
        if picker.pending.is_some() { return true; }
        // Dismissal must remain possible when a split or resized pane cannot
        // show the review, and when an exact-selection guard has invalidated it.
        if key.code == KeyCode::Esc {
            if picker.proposal_mode && picker.review.is_some() {
                picker.review = None;
                picker.armed_resolution = None;
                return true;
            }
            if picker.belief_review.is_some() {
                if picker.belief_action.is_some() {
                    picker.belief_action = None;
                    picker.belief_note.clear();
                    picker.retract_armed = false;
                    picker.status = "Belief action cancelled".into();
                } else {
                    picker.belief_review = None;
                    picker.can_act_on_beliefs = false;
                }
                return true;
            }
        }
        if picker.proposal_mode {
            if let Some(review) = &picker.review {
                let Some(area) = review_area else { return true; };
                let width = usize::from(area.width.saturating_sub(3)).max(1);
                let visible = usize::from(area.height.saturating_sub(REVIEW_BODY_RESERVE));
                let total = raw_visual_rows(review.raw(), width).len();
                if picker.review_width != width {
                    picker.review_width = width;
                    picker.review_scroll = 0;
                    picker.review_seen = 0;
                    picker.armed_resolution = None;
                }
                if visible == 0 {
                    picker.armed_resolution = None;
                    picker.status = "Enlarge the review to read its complete contents".into();
                    return true;
                }
                if visible > 0 && picker.review_scroll <= picker.review_seen {
                    picker.review_seen = picker.review_seen.max(picker.review_scroll.saturating_add(visible)).min(total);
                }
                let max_scroll = total.saturating_sub(visible);
                match key.code {
                    KeyCode::Esc => { picker.review = None; picker.armed_resolution = None; }
                    KeyCode::Up => { picker.review_scroll = picker.review_scroll.saturating_sub(1); picker.armed_resolution = None; }
                    KeyCode::Down => { picker.review_scroll = (picker.review_scroll + 1).min(max_scroll); picker.armed_resolution = None; }
                    KeyCode::PageUp => { picker.review_scroll = picker.review_scroll.saturating_sub(visible.saturating_sub(1).max(1)); picker.armed_resolution = None; }
                    KeyCode::PageDown => { picker.review_scroll = picker.review_scroll.saturating_add(visible.saturating_sub(1).max(1)).min(max_scroll); picker.armed_resolution = None; }
                    KeyCode::Char('a' | 'A') if !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                        && picker.can_resolve && visible > 0 && picker.review_seen == total && picker.pending.is_none() => {
                        picker.armed_resolution = Some(doxa_lore::PendingDecision::Approve);
                        picker.status = "Approve this exact proposal? Press Enter to confirm, Esc to cancel".into();
                    }
                    KeyCode::Char('r' | 'R') if !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                        && picker.can_resolve && visible > 0 && picker.review_seen == total && picker.pending.is_none() => {
                        picker.armed_resolution = Some(doxa_lore::PendingDecision::Reject);
                        picker.status = "Reject this exact proposal? Press Enter to confirm, Esc to cancel".into();
                    }
                    KeyCode::Enter if picker.armed_resolution.is_some() && picker.pending.is_none() => {
                        let decision = picker.armed_resolution.take().unwrap();
                        let cwd = picker.cwd.clone();
                        let review = review.clone();
                        self.load_lore(lore_picker::Query::Resolve(cwd, review, decision));
                    }
                    _ => {
                        if picker.review_seen < total {
                            picker.status = "Read through the end before choosing approve or reject".into();
                        }
                    }
                }
                return true;
            }
            match key.code {
                KeyCode::Esc => self.lore_picker = None,
                KeyCode::Char('b') if picker.review.is_none() => {
                    picker.proposal_mode = false;
                    picker.offset = 0;
                    picker.selected = 0;
                    self.load_lore(lore_picker::Query::Beliefs(0));
                }
                KeyCode::Up => picker.selected = picker.selected.saturating_sub(1),
                KeyCode::Down => picker.selected = (picker.selected + 1).min(picker.proposals.len().saturating_sub(1)),
                KeyCode::Enter | KeyCode::Right => {
                    if let Some(pid) = picker.proposals.get(picker.selected).map(|row| row.pid.clone()) {
                        let cwd = picker.cwd.clone();
                        self.load_lore(lore_picker::Query::Review(cwd, pid));
                    }
                }
                KeyCode::PageDown => {
                    picker.offset = picker.offset.saturating_add(lore_picker::PAGE_SIZE as u16).min(10000);
                    let (cwd, offset) = (picker.cwd.clone(), picker.offset);
                    self.load_lore(lore_picker::Query::Proposals(cwd, offset));
                }
                KeyCode::PageUp => {
                    picker.offset = picker.offset.saturating_sub(lore_picker::PAGE_SIZE as u16);
                    let (cwd, offset) = (picker.cwd.clone(), picker.offset);
                    self.load_lore(lore_picker::Query::Proposals(cwd, offset));
                }
                KeyCode::F(5) => {
                    let (cwd, offset) = (picker.cwd.clone(), picker.offset);
                    self.load_lore(lore_picker::Query::Proposals(cwd, offset));
                }
                _ => return false,
            }
            return true;
        }
        if let Some(review) = &picker.belief_review {
            if !picker.rows.get(picker.selected).is_some_and(|row| row.id == review.id()) {
                picker.can_act_on_beliefs = false;
                picker.belief_action = None;
                picker.retract_armed = false;
                picker.status = "Selection changed; reopen the exact belief review".into();
                return true;
            }
            let Some(area) = review_area else { return true; };
            let width = usize::from(area.width.saturating_sub(3)).max(1);
            let visible = usize::from(area.height.saturating_sub(REVIEW_BODY_RESERVE));
            let full = format!("Subject: {}\nClaim: {}", review.subject(), review.claim());
            let total = raw_visual_rows(&full, width).len();
            if picker.review_width != width {
                picker.review_width = width;
                picker.review_scroll = 0;
                picker.review_seen = 0;
                picker.belief_action = None;
                picker.retract_armed = false;
            }
            if visible == 0 {
                picker.belief_action = None;
                picker.retract_armed = false;
                picker.status = "Enlarge the review to read its complete contents".into();
                return true;
            }
            if visible > 0 && picker.review_scroll <= picker.review_seen {
                picker.review_seen = picker.review_seen.max(picker.review_scroll.saturating_add(visible)).min(total);
            }
            let max_scroll = total.saturating_sub(visible);
            if let Some(action) = picker.belief_action {
                match key.code {
                    KeyCode::Esc => {
                        picker.belief_action = None;
                        picker.belief_note.clear();
                        picker.retract_armed = false;
                        picker.status = "Belief action cancelled".into();
                    }
                    KeyCode::Backspace => { picker.belief_note.pop(); picker.retract_armed = false; }
                    KeyCode::Char('y' | 'Y') if !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                        && action == doxa_lore::BeliefAction::Retract && picker.retract_armed
                        && picker.can_act_on_beliefs && picker.rows.get(picker.selected).is_some_and(|row| row.id == review.id()) => {
                        let (cwd, exact, note) = (picker.cwd.clone(), review.clone(), picker.belief_note.clone());
                        picker.belief_action = None;
                        picker.retract_armed = false;
                        self.load_lore(lore_picker::Query::BeliefAction(cwd, exact, action, note));
                    }
                    KeyCode::Char(c) if !c.is_control()
                        && !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) => {
                        if picker.belief_note.len() + c.len_utf8() <= 300 {
                            picker.belief_note.push(c);
                            picker.retract_armed = false;
                        } else { picker.status = "Note is limited to 300 UTF-8 bytes".into(); }
                    }
                    KeyCode::Enter if picker.belief_note.trim().is_empty() => {
                        picker.status = "Add a note before applying this belief action".into();
                    }
                    KeyCode::Enter if action == doxa_lore::BeliefAction::Retract && !picker.retract_armed => {
                        picker.retract_armed = true;
                        picker.status = "Confirm retract of this exact belief: press Y; Esc cancels".into();
                    }
                    KeyCode::Enter if action != doxa_lore::BeliefAction::Retract && picker.can_act_on_beliefs
                        && picker.rows.get(picker.selected).is_some_and(|row| row.id == review.id()) => {
                        let (cwd, exact, note) = (picker.cwd.clone(), review.clone(), picker.belief_note.clone());
                        picker.belief_action = None;
                        picker.retract_armed = false;
                        self.load_lore(lore_picker::Query::BeliefAction(cwd, exact, action, note));
                    }
                    _ => {}
                }
                return true;
            }
            match key.code {
                KeyCode::Esc => { picker.belief_review = None; picker.can_act_on_beliefs = false; }
                KeyCode::Up => picker.review_scroll = picker.review_scroll.saturating_sub(1),
                KeyCode::Down => picker.review_scroll = (picker.review_scroll + 1).min(max_scroll),
                KeyCode::PageUp => picker.review_scroll = picker.review_scroll.saturating_sub(visible.saturating_sub(1).max(1)),
                KeyCode::PageDown => picker.review_scroll = picker.review_scroll.saturating_add(visible.saturating_sub(1).max(1)).min(max_scroll),
                KeyCode::Char(c) if !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                    && picker.can_act_on_beliefs && visible > 0 && picker.review_seen == total => {
                    picker.belief_action = match c.to_ascii_lowercase() {
                        'c' => Some(doxa_lore::BeliefAction::Confirmed),
                        'x' => Some(doxa_lore::BeliefAction::Contradicted),
                        's' => Some(doxa_lore::BeliefAction::Stale),
                        'r' => Some(doxa_lore::BeliefAction::Retract),
                        _ => None,
                    };
                    if picker.belief_action.is_some() {
                        picker.belief_note.clear();
                        picker.retract_armed = false;
                        picker.status = "Enter a note, then press Enter to apply".into();
                    }
                }
                _ => {
                    if picker.review_seen < total { picker.status = "Read the complete belief before choosing an action".into(); }
                }
            }
            return true;
        }
        match key.code {
            KeyCode::Esc => {
                if picker.evidence.is_some() { picker.evidence = None; }
                else { self.lore_picker = None; }
            }
            KeyCode::Up if picker.evidence.is_none() => picker.selected = picker.selected.saturating_sub(1),
            KeyCode::Down if picker.evidence.is_none() => picker.selected = (picker.selected + 1).min(picker.rows.len().saturating_sub(1)),
            KeyCode::Right if picker.evidence.is_none() => {
                if let Some(id) = picker.rows.get(picker.selected).map(|row| row.id) {
                    self.load_lore(lore_picker::Query::Evidence(id));
                }
            }
            KeyCode::Enter if picker.evidence.is_none() && picker.query.is_empty() => {
                if let Some(id) = picker.rows.get(picker.selected).map(|row| row.id) {
                    let cwd = picker.cwd.clone();
                    self.load_lore(lore_picker::Query::BeliefReview(cwd, id));
                }
            }
            KeyCode::Backspace if picker.evidence.is_none() => { picker.query.pop(); },
            KeyCode::Char('p' | 'P') if picker.evidence.is_none() && picker.query.is_empty() => {
                picker.proposal_mode = true;
                picker.offset = 0;
                picker.selected = 0;
                let cwd = picker.cwd.clone();
                self.load_lore(lore_picker::Query::Proposals(cwd, 0));
            }
            KeyCode::F(5) if picker.evidence.is_none() => {
                picker.query.clear();
                let offset = picker.offset;
                self.load_lore(lore_picker::Query::Beliefs(offset));
            }
            KeyCode::Char(c) if picker.evidence.is_none() && !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) => {
                if picker.query.len() + c.len_utf8() <= 512 { picker.query.push(c); }
            }
            KeyCode::Enter if picker.evidence.is_none() => {
                if !picker.query.trim().is_empty() {
                    let query = picker.query.clone();
                    self.load_lore(lore_picker::Query::Search(query));
                }
            }
            KeyCode::PageDown if picker.evidence.is_none() && picker.query.is_empty() => {
                picker.offset = picker.offset.saturating_add(lore_picker::PAGE_SIZE as u16).min(10000);
                let offset = picker.offset;
                self.load_lore(lore_picker::Query::Beliefs(offset));
            }
            KeyCode::PageUp if picker.evidence.is_none() && picker.query.is_empty() => {
                picker.offset = picker.offset.saturating_sub(lore_picker::PAGE_SIZE as u16);
                let offset = picker.offset;
                self.load_lore(lore_picker::Query::Beliefs(offset));
            }
            KeyCode::Enter if picker.evidence.is_some() => picker.evidence = None,
            _ => return false,
        }
        true
    }

    fn poll_history(&mut self) -> bool {
        let started = self.start_due_history_query(Instant::now());
        let Some(receiver) = &self.history_pending else { return false; };
        let found = match receiver.try_recv() {
            Ok(found) => found,
            Err(TryRecvError::Empty) => return started,
            Err(TryRecvError::Disconnected) => { self.history_pending = None; return false; }
        };
        self.history_pending = None;
        let scan_query = self.history_scan_query.take();
        if scan_query.as_deref().is_some_and(|query| query != self.history_query.to_lowercase()) {
            return true;
        }
        let mut changed = false;
        for entry in found {
            if let Some(query) = &scan_query {
                self.history_scanned_matches.insert(entry.id.clone(), query.clone());
            }
            self.history_entries.insert(entry.id.clone(), entry.clone());
            if self.sessions.iter().any(|session| session.id == entry.id) { continue; }
            self.offline_ids.insert(entry.id.clone());
            self.sessions.push(Session { id: entry.id.clone(), title: entry.id,
                collection: safe_label(&entry.project), transcript: transcript_tail(&entry.markdown).to_owned(),
                status: "Archived · read-only".into() });
            changed = true;
        }
        self.prune_unopened_history();
        if self.history_modal && self.history_resume && self.history_explicit {
            match self.history_matches().len() {
                0 => {
                    self.history_modal = false;
                    self.notice = format!("Resume: no saved session matches {}", safe_label(&self.history_query));
                }
                1 => self.open_selected_history(),
                _ => {}
            }
            changed = true;
        }
        changed
    }

    /// Populate the deterministic screenshot renderer without scanning local
    /// history or contacting a provider.
    #[doc(hidden)]
    pub fn show_history_fixture(&mut self, query: &str, entries: Vec<history::OfflineSession>) {
        self.history_modal = true;
        self.history_resume = false;
        self.history_query = query.to_owned();
        self.history_selected = 0;
        self.cancel_history_query();
        for entry in entries {
            self.history_scanned_matches.insert(entry.id.clone(), query.to_lowercase());
            self.history_entries.insert(entry.id.clone(), entry.clone());
            if self.sessions.iter().any(|session| session.id == entry.id) { continue; }
            self.offline_ids.insert(entry.id.clone());
            self.sessions.push(Session { id: entry.id.clone(), title: entry.id,
                collection: safe_label(&entry.project), transcript: transcript_tail(&entry.markdown).to_owned(),
                status: "Archived · read-only".into() });
        }
    }

    fn history_key(&mut self, key: KeyEvent) -> bool {
        match key.code {
            KeyCode::Esc | KeyCode::Char('r') if key.code == KeyCode::Esc || key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.history_modal = false;
                self.cancel_history_query();
                self.prune_unopened_history();
            }
            KeyCode::Up => self.history_selected = self.history_selected.saturating_sub(1),
            KeyCode::Down => self.history_selected = (self.history_selected + 1).min(self.history_matches().len().saturating_sub(1)),
            KeyCode::Backspace => { self.history_query.pop(); self.history_selected = 0;
                self.schedule_history_query(Instant::now()); }
            KeyCode::Char(c) if !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) => {
                if self.history_query.len() + c.len_utf8() <= 200 { self.history_query.push(c); self.history_selected = 0;
                    self.schedule_history_query(Instant::now()); }
            }
            KeyCode::Enter => {
                self.open_selected_history();
            }
            _ => return false,
        }
        true
    }

    fn open_selected_history(&mut self) {
        if let Some(&index) = self.history_matches().get(self.history_selected) {
            let id = self.sessions[index].id.clone();
            if self.history_resume {
                self.history_modal = false;
                self.cancel_history_query();
                self.history_resume = false;
                if self.groups.iter().any(|group| group.tabs.iter().any(|tab| tab == &id)) {
                    for (group_index, group) in self.groups.iter_mut().enumerate() {
                        if let Some(index) = group.tabs.iter().position(|tab| tab == &id) {
                            group.active = index;
                            self.active_group = group_index;
                            self.notice = format!("Session already open · {}", safe_label(&id));
                            return;
                        }
                    }
                }
                if crate::discovery::sessions().is_ok_and(|rows| rows.iter().any(|row| row.id == id)) {
                    self.attach_selected(&id);
                    return;
                }
                let Some(entry) = self.history_entries.get(&id).cloned() else {
                    self.notice = "Resume unavailable: no saved transcript for this session".into();
                    return;
                };
                let python = std::env::var_os("DOXA_LORE_PYTHON")
                    .map(PathBuf::from).unwrap_or_else(|| PathBuf::from("python3"));
                let (tx, rx) = mpsc::sync_channel(1);
                self.resume_pending = Some(rx);
                std::thread::spawn(move || { let result = history::resume_plan(&entry, &python); let _ = tx.send((id, result)); });
                self.notice = "Checking saved conversation…".into();
                return;
            }
            let tabs = &mut self.groups[self.active_group];
            if let Some(index) = tabs.tabs.iter().position(|tab| tab == &id) { tabs.active = index; }
            else { tabs.tabs.push(id); tabs.active = tabs.tabs.len() - 1; }
            tabs.scroll = 0;
            self.focus = Focus::Transcript;
            self.history_modal = false;
            self.cancel_history_query();
        }
    }

    fn poll_resume(&mut self) -> bool {
        let Some(receiver) = &self.resume_pending else { return false; };
        let result = match receiver.try_recv() {
            Ok(result) => result,
            Err(TryRecvError::Empty) => return false,
            Err(TryRecvError::Disconnected) => { self.resume_pending = None; return false; }
        };
        self.resume_pending = None;
        let (id, plan) = result;
        match plan {
            Ok(options) if !self.launching => {
                // Recheck live registry immediately before dispatch. The daemon
                // also owns the final uniqueness check on this session ID.
                if crate::discovery::sessions().is_ok_and(|rows| rows.iter().any(|row| row.id == id)) {
                    self.attach_selected(&id);
                } else {
                    self.pending_launches.push((options, None, self.active_group));
                    self.launching = true;
                    self.notice = format!("Resuming · {}", safe_label(&id));
                }
            }
            Ok(_) => self.notice = "Wait for the current session launch before resuming".into(),
            Err(reason) => self.notice = format!("Resume unavailable · {reason}; transcript remains readable"),
        }
        true
    }

    fn open_diff(&mut self) {
        if self.diff_modal {
            if self.rejections_for_target() > 0 {
                self.notice = "Wait for queued hunk rejections before closing this diff".into();
            } else { self.diff_modal = false; }
            return;
        }
        self.diff_modal = true;
        self.load_diff();
    }

    fn rejections_for_target(&self) -> usize {
        let Some(id) = self.diff_target.as_deref() else { return 0; };
        self.diff_reject_queue.iter().filter(|item| item.session_id == id).count()
            + usize::from(self.diff_reject_active.as_ref().is_some_and(|item| item.session_id == id))
    }

    fn queued_diff_rows(&self) -> HashSet<usize> {
        let mut rows = HashSet::new();
        let (Some(id), Some(current)) = (self.diff_target.as_deref(), self.diff_snapshot.as_ref()) else { return rows; };
        for (index, hunk) in current.rejectable.iter().enumerate() {
            if self.diff_reject_queue.iter().any(|item| item.session_id == id
                && current.same_hunk(index, &item.snapshot, item.index))
                || self.diff_reject_active.as_ref().is_some_and(|item| item.session_id == id
                    && current.same_hunk(index, &item.snapshot, item.index)) {
                rows.insert(hunk.row);
            }
        }
        rows
    }

    fn load_diff(&mut self) {
        self.diff_scroll = 0;
        self.diff_files.clear();
        self.diff_hunks.clear();
        self.diff_pending = None;
        self.diff_snapshot = None;
        self.diff_reject_confirm = None;
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
        if self.diff_reject_feedback.is_some() && self.pending_prompts.len() < MAX_PENDING_PROMPTS {
            self.pending_prompts.push(self.diff_reject_feedback.take().expect("retained feedback"));
            self.notice = "Notifying the session about the reverted hunk".into();
            return true;
        }
        if let Some(receiver) = &self.diff_reject_pending {
            match receiver.try_recv() {
                Ok((id, result, message)) => {
                    self.diff_reject_pending = None;
                    self.diff_reject_active = None;
                    match result {
                        Ok(note) => {
                            if self.pending_prompts.len() < MAX_PENDING_PROMPTS {
                                self.pending_prompts.push((id, message));
                                self.notice = format!("{note} · notifying the session");
                            } else {
                                self.diff_reject_feedback = Some((id, message));
                                self.notice = format!("{note} · feedback retained until the prompt queue has room");
                            }
                            self.load_diff();
                        }
                        Err(note) => self.notice = note,
                    }
                    return true;
                }
                Err(TryRecvError::Disconnected) => {
                    self.diff_reject_pending = None;
                    self.diff_reject_active = None;
                    self.notice = "Hunk rejection worker stopped unexpectedly; inspect the worktree.".into();
                    return true;
                }
                Err(TryRecvError::Empty) => {}
            }
        }
        if self.start_next_rejection() { return true; }
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
                    self.diff_files = snapshot.files.clone();
                    self.diff_hunks = snapshot.hunks.clone();
                    self.diff_snapshot = Some(snapshot);
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
        if self.diff_reject_confirm.is_some() {
            match key.code {
                KeyCode::Enter => self.confirm_diff_reject(),
                KeyCode::Esc => {
                    self.diff_reject_confirm = None;
                    self.notice = "Hunk rejection cancelled".into();
                }
                KeyCode::Backspace => {
                    if let Some(draft) = &mut self.diff_reject_confirm { draft.reason.pop(); }
                }
                KeyCode::Char(ch) if !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) => {
                    self.append_reject_reason(ch);
                }
                _ => return true,
            }
            return true;
        }
        match key.code {
            KeyCode::Esc | KeyCode::F(2) => self.open_diff(),
            KeyCode::Char('g') if key.modifiers.contains(KeyModifiers::ALT) => self.open_diff(),
            KeyCode::Char('r' | 'R') => { self.diff_modal = false; self.open_diff(); },
            KeyCode::Up => self.diff_scroll = self.diff_scroll.saturating_sub(1),
            KeyCode::Down => self.diff_scroll = self.diff_scroll.saturating_add(1),
            KeyCode::PageUp => self.diff_scroll = self.diff_scroll.saturating_sub(10),
            KeyCode::PageDown => self.diff_scroll = self.diff_scroll.saturating_add(10),
            KeyCode::Char('n' | 'N') => self.jump_diff(true, true),
            KeyCode::Char('p' | 'P') => self.jump_diff(true, false),
            KeyCode::Char('j' | 'J') => self.jump_diff(false, true),
            KeyCode::Char('k' | 'K') => self.jump_diff(false, false),
            KeyCode::Char('x' | 'X') => self.begin_diff_reject(),
            _ => return false,
        }
        true
    }

    fn begin_diff_reject(&mut self) {
        if self.diff_reject_queue.len() + usize::from(self.diff_reject_active.is_some()) >= MAX_QUEUED_REJECTIONS {
            self.notice = "Too many queued hunk rejections".into();
            return;
        }
        let Some(id) = self.diff_target.as_ref() else { self.notice = "Select a session first".into(); return; };
        if self.groups[self.active_group].active_id() != Some(id.as_str()) {
            self.notice = "Active session changed; refresh the diff before rejecting".into();
            return;
        }
        if !self.session_activity.contains_key(id) {
            self.notice = "Session activity is unknown; wait for a status update".into();
            return;
        }
        let Some(snapshot) = self.diff_snapshot.as_ref() else { self.notice = "Load the diff before rejecting an edit".into(); return; };
        let Some(row) = snapshot.hunks.iter().copied().filter(|row| *row <= self.diff_scroll).next_back()
            .or_else(|| snapshot.hunks.first().copied()) else {
            self.notice = "No tracked hunk is visible in this diff".into();
            return;
        };
        let Some(index) = snapshot.rejectable.iter().position(|hunk| hunk.row == row) else {
            self.notice = "This hunk has file-level changes or a truncated patch; inspect it with git".into();
            return;
        };
        self.diff_scroll = snapshot.rejectable[index].row;
        self.diff_reject_confirm = Some(RejectDraft { index, reason: String::new() });
        self.notice = format!("Reject {} in {}? Type optional reason · Enter confirm · Esc cancel",
            snapshot.rejectable[index].header, snapshot.rejectable[index].path);
    }

    fn confirm_diff_reject(&mut self) {
        let Some(draft) = self.diff_reject_confirm.take() else { return; };
        let Some(id) = self.diff_target.clone() else { return; };
        if self.groups[self.active_group].active_id() != Some(id.as_str()) {
            self.notice = "Active session changed; rejection cancelled".into();
            return;
        }
        if !self.session_activity.contains_key(&id) {
            self.notice = "Session activity is unknown; rejection cancelled".into();
            return;
        }
        let Some(snapshot) = self.diff_snapshot.clone() else { return; };
        if snapshot.rejectable.get(draft.index).is_none() { return; }
        if self.diff_reject_queue.iter().any(|queued| queued.session_id == id
            && snapshot.same_hunk(draft.index, &queued.snapshot, queued.index))
            || self.diff_reject_active.as_ref().is_some_and(|active| active.session_id == id
                && snapshot.same_hunk(draft.index, &active.snapshot, active.index)) {
            self.notice = "This hunk is already queued for rejection".into();
            return;
        }
        if self.diff_reject_queue.len() + usize::from(self.diff_reject_active.is_some()) >= MAX_QUEUED_REJECTIONS {
            self.notice = "Too many queued hunk rejections".into();
            return;
        }
        self.diff_reject_queue.push_back(PendingRejection {
            session_id: id, snapshot, index: draft.index, reason: draft.reason,
        });
        if self.start_next_rejection() { return; }
        self.notice = format!("Hunk rejection queued until session is idle · {} pending", self.diff_reject_queue.len());
    }

    fn start_next_rejection(&mut self) -> bool {
        if self.diff_reject_pending.is_some() || self.diff_reject_feedback.is_some()
            || self.pending_prompts.len() >= MAX_PENDING_PROMPTS { return false; }
        let Some(position) = self.diff_reject_queue.iter().position(|item| {
            self.session_activity.get(&item.session_id).copied() == Some((false, 0))
        }) else { return false; };
        let job = self.diff_reject_queue.remove(position).expect("queued rejection");
        let Some(hunk) = job.snapshot.rejectable.get(job.index) else { return false; };
        let message = hunk.message(&job.reason);
        let id = job.session_id.clone();
        let snapshot = job.snapshot.clone();
        let index = job.index;
        let (tx, rx) = mpsc::sync_channel(1);
        self.diff_reject_pending = Some(rx);
        self.diff_reject_active = Some(job);
        self.notice = "Checking and reverting the selected hunk…".into();
        std::thread::spawn(move || {
            let outcome = diff_view::reject(&snapshot, index);
            let _ = tx.send((id, outcome, message));
        });
        true
    }

    fn jump_diff(&mut self, file: bool, forward: bool) {
        let marks = if file { &self.diff_files } else { &self.diff_hunks };
        let current = self.diff_scroll;
        let target = if forward {
            marks.iter().copied().find(|&row| row > current)
        } else {
            marks.iter().copied().rev().find(|&row| row < current)
        };
        if let Some(row) = target {
            self.diff_scroll = row;
        } else {
            self.notice = format!("No {} {} in this diff", if forward { "next" } else { "previous" },
                if file { "file" } else { "hunk" });
        }
    }

    fn active_tool_cards(&self) -> &[tool_cards::ToolCard] {
        self.groups[self.active_group]
            .active_id()
            .map(|id| self.tool_cards.for_session(id))
            .unwrap_or(&[])
    }

    fn tool_sections_for_active(&self) -> Option<(String, usize)> {
        let id = self.groups[self.active_group].active_id()?.to_owned();
        let transcript = &self.sessions.iter().find(|s| s.id == id)?.transcript;
        let (_, sections) = transcript_tools::render(transcript, 80, None, None);
        Some((id, sections.len()))
    }

    fn select_tool_section(&mut self, forward: bool) -> bool {
        let Some((id, _)) = self.tool_sections_for_active() else { return false; };
        let visible: Vec<usize> = self.visible_tool_sections.borrow().iter()
            .filter(|(_, group, session, _)| *group == self.active_group && *session == id)
            .map(|(_, _, _, section)| *section).collect();
        if visible.is_empty() { return false; }
        let current = self.selected_tool_sections.get(&id).copied();
        let next = match current.and_then(|value| visible.iter().position(|index| *index == value)) {
            Some(position) if forward => (position + 1).min(visible.len() - 1),
            Some(position) => position.saturating_sub(1),
            None if forward => 0,
            None => visible.len() - 1,
        };
        self.selected_tool_sections.insert(id, visible[next]);
        true
    }

    fn toggle_selected_tool_section(&mut self) -> bool {
        let Some((id, count)) = self.tool_sections_for_active() else { return false; };
        if count == 0 { return false; }
        let visible: Vec<usize> = self.visible_tool_sections.borrow().iter()
            .filter(|(_, group, session, _)| *group == self.active_group && *session == id)
            .map(|(_, _, _, section)| *section).collect();
        let selected = self.selected_tool_sections.get(&id).copied()
            .filter(|section| visible.contains(section))
            .or_else(|| visible.last().copied())
            .unwrap_or(count - 1);
        self.selected_tool_sections.insert(id.clone(), selected);
        self.toggle_tool_section(id, selected);
        true
    }

    fn toggle_tool_section(&mut self, id: String, selected: usize) {
        if !self.expanded_tool_sections.contains_key(&id) && self.expanded_tool_sections.len() >= 64 {
            if let Some(oldest) = self.expanded_tool_sections.keys().next().cloned() {
                self.expanded_tool_sections.remove(&oldest);
            }
        }
        let expanded = self.expanded_tool_sections.entry(id).or_default();
        if !expanded.remove(&selected) {
            if expanded.len() >= 64 {
                if let Some(oldest) = expanded.iter().copied().min() { expanded.remove(&oldest); }
            }
            expanded.insert(selected);
        }
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
                        self.split_requested = true;
                        self.focus = Focus::Prompt;
                    }
                    6 => self.open_history(),
                    7 => self.open_lore_picker(),
                    8 => self.open_diff(),
                    9 => self.open_engine_picker(),
                    10 => self.open_model_picker(),
                    11 => self.open_permission_picker(),
                    12 => self.open_stop_confirmation(),
                    13 => { self.move_active_tab(1 - self.active_group); }
                    _ => unreachable!("fixed action list"),
                }
            }
            _ => {}
        }
        true
    }

    fn stop_confirmation_fits(&self) -> bool {
        self.size.width >= 40 && self.size.height >= 12
    }

    fn open_stop_confirmation(&mut self) {
        if !self.stop_confirmation_fits() {
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
            KeyCode::Char('y' | 'Y') if (key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT)
                && self.stop_confirmation_fits() => {
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

    fn rail_rows(&self) -> Vec<RailRow> {
        let mut rows = Vec::new();
        let mut seen = HashSet::new();
        for (heading, item) in self.collections.iter().enumerate() {
            rows.push(RailRow::Heading(heading));
            for id in &item.sessions {
                if let Some(index) = self.sessions.iter().position(|session| &session.id == id) {
                    if seen.insert(index) && !item.collapsed { rows.push(RailRow::Session(index)); }
                }
            }
        }
        let loose: Vec<_> = (0..self.sessions.len()).filter(|index| seen.insert(*index)).collect();
        if !loose.is_empty() {
            rows.push(RailRow::LooseHeading);
            rows.extend(loose.into_iter().map(RailRow::Session));
        }
        rows
    }

    fn rail_order(&self) -> Vec<usize> {
        self.rail_rows().into_iter().filter_map(|row| match row {
            RailRow::Session(index) => Some(index),
            _ => None,
        }).collect()
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

    /// Remove only the active tab. Its daemon and rail entry remain available
    /// for reattachment; closing the final tab exits the otherwise empty UI.
    fn detach_active_tab(&mut self) {
        let group = &mut self.groups[self.active_group];
        if group.active >= group.tabs.len() {
            self.notice = "No active tab to detach".into();
            return;
        }
        let id = group.tabs.remove(group.active);
        group.active = group.active.min(group.tabs.len().saturating_sub(1));
        group.scroll = 0;
        self.notice = format!("Tab detached · {id} remains available in sessions");

        if self.groups.iter().all(|group| group.tabs.is_empty()) {
            self.should_quit = true;
        } else if self.groups[0].tabs.is_empty() {
            self.groups.swap(0, 1);
            for id in self.groups[0].tabs.clone() {
                if let Some(draft) = self.input_drafts.remove(&(1, id.clone())) {
                    self.input_drafts.insert((0, id), draft);
                }
            }
            self.active_group = 0;
            self.split_requested = false;
        } else if self.groups[1].tabs.is_empty() {
            self.active_group = 0;
            self.split_requested = false;
        }
        self.focus = Focus::Prompt;
    }

    /// Move the active session tab to the opposite group. Keep a source tab
    /// so moving never implicitly closes a pane group.
    fn move_active_tab(&mut self, target: usize) -> bool {
        if target > 1 || target == self.active_group {
            self.notice = "Choose the other pane group (1 or 2)".into();
            return false;
        }
        let source = self.active_group;
        let Some(id) = self.groups[source].active_id().map(str::to_owned) else {
            self.notice = "No active tab to move".into();
            return false;
        };
        if self.offline_ids.contains(&id) {
            self.notice = "Archived transcript cannot be moved".into();
            return false;
        }
        if self.groups[source].tabs.len() < 2 {
            self.notice = "Cannot move the last tab out of a pane".into();
            return false;
        }
        if self.groups[target].tabs.contains(&id) {
            self.notice = "Session is already open in that pane".into();
            return false;
        }
        let current = self.groups[source].active;
        self.groups[source].tabs.remove(current);
        self.groups[source].active = current.min(self.groups[source].tabs.len() - 1);
        self.groups[source].scroll = 0;
        self.groups[target].tabs.push(id);
        self.groups[target].active = self.groups[target].tabs.len() - 1;
        self.groups[target].scroll = 0;
        self.active_group = target;
        self.moved_active_tab = true;
        self.split_requested = true;
        self.focus = Focus::Prompt;
        self.notice = format!("Tab moved to pane {}", target + 1);
        true
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

    fn pane_group_two_exists(&self) -> bool {
        self.split_requested || self.active_group == 1 || !self.groups[1].tabs.is_empty()
    }

    fn layout(&self, area: Rect) -> PaneLayout {
        let min_body = if self.split == Split::Vertical {
            MIN_PANE_WIDTH * 2
        } else {
            MIN_PANE_WIDTH
        };
        let rail_width = if self.rail_visible && area.width >= 70 {
            self.rail_width.clamp(
                MIN_RAIL_WIDTH,
                area.width.saturating_sub(min_body).max(MIN_RAIL_WIDTH),
            )
        } else {
            0
        };
        let (rail, body) = if rail_width > 0 {
            let chunks = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Length(rail_width), Constraint::Min(1)])
                .split(area);
            (Some(chunks[0]), chunks[1])
        } else {
            (None, area)
        };
        let min_ok = if self.split == Split::Vertical {
            body.width >= MIN_PANE_WIDTH * 2
        } else {
            body.height >= MIN_PANE_HEIGHT * 2
        };
        let panes = (min_ok && (self.split_requested || self.active_group == 1
            || !self.groups[1].tabs.is_empty() || self.diff_pane)).then(|| {
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
            outer: area,
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

    /// Space for a chooser inside the active pane, immediately above its
    /// prompt. Reserving this space keeps the transcript and prompt visible.
    fn chooser_rect(&self, pane: Rect) -> Option<Rect> {
        self.sync_chooser_state();
        let wanted = if let Some(index) = self.active_request_index().filter(|&index| self.input_requests[index].kind == "ask_user") {
            let (body, _, _) = input_request_body(&self.input_requests[index],
                usize::from(pane.width.saturating_sub(4)));
            wrapped_rows(&body, usize::from(pane.width.saturating_sub(2)))
                .saturating_add(2).clamp(6, 18) as u16
        } else if self.settings_menu.is_some() {
            8
        } else if self.engine_picker {
            7
        } else if let Some(form) = &self.new_session {
            if form.engine == launch::Engine::Claude || !vendor_models(form.engine).is_empty() { 8 } else { 7 }
        } else if let Some(picker) = &self.effort_picker {
            (4 + picker.levels.len()).clamp(5, 10) as u16
        } else if self.permission_picker.is_some() {
            10
        } else if let Some(picker) = &self.model_picker {
            (4 + picker.models.len() + usize::from(picker.catalog_pending || !picker.loading && picker.models.is_empty()))
                .clamp(5, 13) as u16
        } else if let Some(picker) = &self.repo_picker {
            (picker.paths.len() + 3).clamp(5, 15) as u16
        } else if let Some(picker) = &self.lore_picker {
            if picker.review.is_some() || picker.belief_review.is_some() {
                19
            } else if picker.proposal_mode {
                (7 + picker.proposals.len()).clamp(5, 19) as u16
            } else if let Some((_, evidence)) = &picker.evidence {
                let rows = evidence.len().saturating_mul(2);
                (if rows <= 4 { 4 + rows } else { 7 + rows }).clamp(5, 19) as u16
            } else {
                let rows = picker.rows.len();
                (if rows <= 2 { 4 + rows } else { 7 + rows }).clamp(5, 19) as u16
            }
        } else if self.action_menu {
            (ACTIONS.len() + 2).min(15) as u16
        } else if self.chip_info.is_some() {
            self.chip_info.as_ref().map_or(5, |info| if matches!(info.kind, "memory" | "usage" | "context" | "help") {
                (info.lines.len() + 2).clamp(7, 19) as u16
            } else { 5 })
        } else if self.history_modal {
            let rows: usize = self.history_matches().iter().map(|&index|
                1 + self.history_snippets(&self.sessions[index].id).len().min(2)).sum();
            (rows + 3).clamp(5, 15) as u16
        } else if let Some(picker) = &self.queue_picker {
            (picker.rows.len() + 3).clamp(5, 15) as u16
        } else if self.attach_picker.is_some() {
            (self.attach_matches().len() + 3).clamp(5, 15) as u16
        } else if let Some(picker) = &self.branch_picker {
            (picker.branches.len() + 3).clamp(5, 15) as u16
        } else if !self.slash_suggestions().is_empty() {
            (self.slash_suggestions().len() + 2).clamp(5, 10) as u16
        } else {
            return None;
        };
        let group = &self.groups[self.active_group];
        let draft = group.active_id().map_or("", |_| self.input.as_str());
        let prompt = prompt_height(draft, pane.height);
        let available = pane.height.saturating_sub(3 + prompt + 1 + 1 + 1);
        let height = self.chooser_height_override.get().unwrap_or(wanted).min(available);
        if height < 5 || pane.width < 18 { return None; }
        Some(Rect::new(pane.x, pane.bottom().saturating_sub(prompt + 1 + 1 + height), pane.width, height))
    }

    fn active_chooser_rect(&self) -> Option<Rect> {
        let layout = self.layout(self.size);
        let pane = layout.panes.map_or(layout.body, |panes| panes[self.active_group]);
        self.chooser_rect(pane)
    }

    fn chips(&self, index: usize) -> Vec<(&'static str, String)> {
        let id = self.groups[index].active_id();
        let identity = id.and_then(|id| self.session_identity.get(id));
        let telemetry = id.and_then(|id| self.session_telemetry.get(id));
        let mut chips = Vec::new();
        // Permission mode (including the classifier-backed `auto` mode) is
        // independent of the provider running this session.
        if let Some(mode) = id.and_then(|id| self.permission_modes.get(id)) {
            chips.push(("permission", format!("Permissions {mode}")));
        } else if id.is_some_and(|id| self.permission_capabilities.get(id).copied().unwrap_or(false)) {
            chips.push(("permission", "Permissions ?".to_owned()));
        }
        if let Some(engine) = identity.and_then(|pair| pair.0.as_deref()) {
            chips.push(("engine", engine.to_owned()));
        } else {
            chips.push(("engine", "Engine".to_owned()));
        }
        if let Some(model) = identity.and_then(|pair| pair.1.as_deref()) {
            chips.push(("model", model.to_owned()));
        } else {
            chips.push(("model", "Model".to_owned()));
        }
        let effort = id.and_then(|id| self.session_efforts.get(id)).map(String::as_str).unwrap_or("?");
        chips.push(("effort", effort.to_owned()));
        if let Some(status) = id.and_then(|id| self.repo_cache.get(id))
            .and_then(|(status, _)| status.as_ref()) {
            chips.push(repo_chip(status));
        }
        chips.push(("context", format!("Ctx {}", telemetry.and_then(|value| value.context.as_deref()).unwrap_or("?"))));
        let memory = self.memory_cache.get(id.unwrap_or("")).and_then(|(usage, _)| *usage)
            .map(|usage| format!("{} {}%/u {}%",
                if self.memory_repo.get(id.unwrap_or("")).copied().unwrap_or(false) { "p" } else { "f" },
                memory_fill_percent(usage.project_chars, usage.project_cap_chars),
                memory_fill_percent(usage.user_chars, usage.user_cap_chars)))
            .unwrap_or_else(|| "u ? · scope ?".to_owned());
        chips.push(("memory", memory));
        let beliefs = telemetry.and_then(|value| value.lore.as_deref())
            .filter(|label| label.ends_with(" beliefs"))
            .unwrap_or("Beliefs");
        chips.push(("beliefs", beliefs.to_owned()));
        let engine = identity.and_then(|pair| pair.0.as_deref());
        if let Some(label) = telemetry.and_then(|value| value.billing_label(engine))
            .or_else(|| match engine {
                Some("deepseek" | "glm") => Some("$?".into()),
                _ => None,
            }) {
            chips.push(("cost", label));
        }
        if engine == Some("deepseek") {
            if let Some(balance) = telemetry.and_then(|value| value.balance.as_deref()) {
                chips.push(("balance", format!("Balance {balance}")));
            }
        }
        chips
    }

    fn waiting_for_input(&self, id: &str) -> bool {
        self.input_requests.iter().any(|request| request.session_id == id && !request.sending)
    }

    fn activity_label(&self, id: &str) -> Option<&'static str> {
        let (running, queued) = self.session_activity.get(id).copied().unwrap_or_default();
        if running { Some("Processing") }
        else if queued > 0 { Some("Queued") }
        else { None }
    }

    fn tick_spinner(&mut self, now: Instant) -> bool {
        if !self.groups.iter().filter_map(|group| group.active_id())
            .any(|id| self.activity_label(id).is_some()) {
            self.spinner_at = now;
            return false;
        }
        if now.duration_since(self.spinner_at) < SPINNER_INTERVAL { return false; }
        self.spinner_at = now;
        self.spinner_frame = (self.spinner_frame + 1) % SPINNER_FRAMES.len();
        true
    }

    /// Called by the event loop at its normal poll cadence. Redraws only once
    /// per phase while a session actually has an unresolved request.
    fn tick_blink(&mut self, now: Instant) -> bool {
        if !self.input_requests.iter().any(|request| !request.sending) {
            self.blink_at = now;
            return std::mem::replace(&mut self.blink_on, true) == false;
        }
        if now.duration_since(self.blink_at) < INPUT_BLINK_INTERVAL { return false; }
        self.blink_at = now;
        self.blink_on = !self.blink_on;
        true
    }

    fn tab_at(&self, index: usize, pane: Rect, column: u16) -> Option<usize> {
        let mut x = pane.x.saturating_add(2); // border and left tab padding
        for (position, id) in self.groups[index].tabs.iter().enumerate() {
            let title = self.sessions.iter().find(|session| &session.id == id)
                .map(|session| session.title.as_str()).unwrap_or(id);
            let end = x.saturating_add(title.width() as u16);
            if column >= x.saturating_sub(1) && column <= end { return Some(position); }
            x = end.saturating_add(3); // right padding, divider, left padding
            if x >= pane.right() { break; }
        }
        None
    }

    fn chip_window(&self, index: usize, width: usize) -> Vec<(&'static str, String)> {
        let all = self.chips(index);
        let full_width = all.iter().map(|(kind, label)| chip_text(kind, label).width()).sum::<usize>()
            + all.len().saturating_sub(1);
        if full_width <= width { return all; }
        let budget = width.saturating_sub(chip_text("more", "+8").width() + 1);
        let mut shown = Vec::new();
        let mut used = 0;
        let start = self.chip_offsets[index] % all.len();
        for step in 0..all.len() {
            let (kind, label) = &all[(start + step) % all.len()];
            let gap = usize::from(!shown.is_empty());
            let room = budget.saturating_sub(used + gap);
            if room < 3 { break; }
            let text_width = chip_text(kind, label).width();
            if text_width > room {
                if shown.is_empty() {
                    let decoration = chip_text(kind, "").width();
                    let clipped = clipped_title(label, room.saturating_sub(decoration)).0;
                    shown.push((*kind, clipped));
                }
                break;
            }
            used += gap + text_width;
            shown.push((*kind, label.clone()));
        }
        let hidden = all.len().saturating_sub(shown.len());
        if hidden > 0 { shown.push(("more", format!("+{hidden}"))); }
        shown
    }

    fn pane_regions(&self, index: usize, area: Rect) -> [Rect; 6] {
        let group = &self.groups[index];
        let draft = group.active_id().map(|id| {
            if self.active_group == index { self.input.as_str() }
            else { self.input_drafts.get(&(index, id.to_owned()))
                .map(|(text, _)| text.as_str()).unwrap_or("") }
        }).unwrap_or("");
        let chooser_height = if self.active_group == index {
            self.chooser_rect(area).map_or(0, |rect| rect.height)
        } else { 0 };
        let regions = Layout::default().direction(Direction::Vertical).constraints([
            Constraint::Length(3), Constraint::Min(1), Constraint::Length(chooser_height),
            Constraint::Length(1), Constraint::Length(prompt_height(draft, area.height)),
            Constraint::Length(1),
        ]).split(area);
        std::array::from_fn(|index| regions[index])
    }

    fn chip_hit_at(&self, column: u16, row: u16) -> Option<ChipHit> {
        if let Some(hits) = self.rendered_chip_hits.borrow().as_ref() {
            return hits.iter().find(|hit| hit.rect.contains(
                ratatui::layout::Position::new(column, row))).cloned();
        }
        // Before the first paint, tests and synthetic input may still use
        // the current size. Interactive input always uses painted regions.
        let layout = self.layout(self.size);
        let panes = layout.panes.map(|panes| vec![(0, panes[0]), (1, panes[1])])
            .unwrap_or_else(|| vec![(self.active_group, layout.body)]);
        for (group, pane) in panes {
            if self.diff_pane && group != self.active_group { continue; }
            let strip = self.pane_regions(group, pane)[3];
            if row != strip.y || column < strip.x || column >= strip.right() { continue; }
            let mut x = strip.x;
            for (kind, label) in self.chip_window(group, usize::from(strip.width)) {
                let width = chip_text(kind, &label).width() as u16;
                let end = x.saturating_add(width).min(strip.right());
                if column >= x && column < end {
                    return Some(ChipHit { group, kind, rect: Rect::new(x, strip.y, end - x, 1), pane });
                }
                x = end.saturating_add(1);
            }
        }
        None
    }

    fn link_at(&self, column: u16, row: u16) -> Option<String> {
        self.visible_links.borrow().iter().find(|(rect, _)| rect.contains(
            ratatui::layout::Position::new(column, row)))
            .map(|(_, url)| url.clone())
    }

    fn link_interaction_blocked(&self) -> bool {
        self.active_chooser_rect().is_some() || self.active_request_index().is_some()
            || self.map_modal || self.diff_modal || self.tool_modal || self.action_menu
            || self.history_modal || self.queue_picker.is_some() || self.attach_picker.is_some()
            || self.branch_picker.is_some() || self.lore_picker.is_some()
            || self.settings_menu.is_some() || self.model_picker.is_some()
            || self.effort_picker.is_some() || self.permission_picker.is_some()
            || self.engine_picker || self.new_session.is_some() || self.repo_picker.is_some()
            || self.chip_info.is_some() || self.stop_confirmation.is_some()
    }

    fn repo_detail(&self, group: usize) -> Option<String> {
        let id = self.groups[group].active_id()?;
        let (Some(doxa_worktrees::RepoStatus::Repository { base, checked_out, worktree, .. }), _) = self.repo_cache.get(id)? else {
            return None;
        };
        let state = if let Some(worktree) = worktree {
            if worktree == "linked worktree" { "linked worktree".to_owned() }
            else { format!("managed worktree {}", safe_label(worktree)) }
        } else { "main checkout".to_owned() };
        Some(format!("base {} · HEAD {} · {state}",
            safe_label(base.as_deref().unwrap_or("?")),
            safe_label(checked_out.as_deref().unwrap_or("detached"))))
    }

    fn open_chip_info(&mut self, kind: &'static str, group: usize) {
        if matches!(kind, "context" | "cost") {
            self.active_group = group;
            self.open_diagnostic(if kind == "cost" { "usage" } else { "context" });
            return;
        }
        let mut label = self.chips(group).into_iter().find(|(candidate, _)| *candidate == kind)
            .map(|(_, label)| label).unwrap_or_default();
        if kind == "repo" {
            if let Some(detail) = self.repo_detail(group) {
                label.push_str(" · ");
                label.push_str(&detail);
            }
        }
        self.active_group = group;
        self.chip_info = Some(ChipInfo { kind, label, lines: Vec::new(), scroll: 0, owner: None });
        if self.active_chooser_rect().is_none() {
            self.chip_info = None;
            self.notice = "Enlarge active pane to inspect chip details".into();
        }
    }

    fn open_help(&mut self) {
        let mut lines = vec!["Rust DOXA commands · forms shown below".to_owned(),
            "Unavailable commands stay local; unknown provider commands pass through".to_owned(),
            String::new()];
        for row in COMMANDS {
            lines.push(format!("{} · {}", row.form, row.summary));
            lines.push(format!("  {}", row.support));
        }
        self.chip_info = Some(ChipInfo { kind: "help", label: String::new(), lines,
            scroll: 0, owner: None });
        if self.active_chooser_rect().is_none() {
            self.chip_info = None;
            self.notice = "Enlarge active pane to open help".into();
        }
    }

    fn open_diagnostic(&mut self, kind: &'static str) {
        let Some(id) = self.groups[self.active_group].active_id().map(str::to_owned) else {
            self.notice = "Select a session first".into();
            return;
        };
        let telemetry = self.session_telemetry.get(&id);
        let model = self.session_identity.get(&id).and_then(|identity| identity.1.as_deref())
            .unwrap_or("not reported");
        let mut lines = vec![format!("session  {}", safe_label(&id.chars().take(8).collect::<String>())),
            format!("model    {}", safe_label(model))];
        if kind == "usage" {
            let number = |field: Option<u64>| field.map(|value| value.to_string())
                .unwrap_or_else(|| "not reported".into());
            lines.push(format!("turns    {}", number(telemetry.and_then(|value| value.turns))));
            for (label, field) in [
                ("tokens in", telemetry.and_then(|value| value.input_tokens)),
                ("tokens out", telemetry.and_then(|value| value.output_tokens)),
                ("cache read", telemetry.and_then(|value| value.cache_read_tokens)),
                ("cache write", telemetry.and_then(|value| value.cache_write_tokens)),
            ] {
                lines.push(format!("{label:<11}{}", number(field)));
            }
            lines.push(format!("cost     {}", telemetry.and_then(|value| value.session_cost.as_deref())
                .unwrap_or("not reported by this engine")));
            if let Some(quota) = telemetry.and_then(|value| value.quota.as_deref()) {
                lines.push(format!("quota    {}", safe_label(quota)));
            }
        }
        let used = telemetry.and_then(|value| value.context_tokens);
        let limit = telemetry.and_then(|value| value.context_limit);
        let percent = telemetry.and_then(|value| value.context_percent);
        let window = match (used, limit) {
            (Some(used), Some(limit)) => format!("{used} / {limit} tokens"),
            (Some(used), None) => format!("{used} tokens · window size not reported"),
            (None, Some(limit)) => format!("? / {limit} tokens"),
            (None, None) => "not reported by this engine".into(),
        };
        lines.push(format!("context  {window}"));
        if let Some(percent) = percent {
            lines.push(format!("in use   {percent:.1}%"));
        }
        if kind == "context" {
            lines.push(String::new());
            lines.push("Component breakdown unavailable in this view".into());
            lines.push("No token counts are estimated here".into());
        }
        self.chip_info = Some(ChipInfo { kind, label: String::new(), lines, scroll: 0,
            owner: Some((id, String::new())) });
        if self.active_chooser_rect().is_none() {
            self.chip_info = None;
            self.notice = "Enlarge active pane to inspect session details".into();
        }
    }

    fn open_memory_menu(&mut self, group: usize) {
        self.open_chip_info("memory", group);
        let Some(info) = self.chip_info.as_mut() else { return; };
        info.lines = vec!["Loading LORE memory…".into()];
        let Some(id) = self.groups[group].active_id().map(str::to_owned) else {
            info.lines = vec!["No active session".into()];
            return;
        };
        let Some(cwd) = self.session_cwds.get(&id).and_then(|path| path.to_str()).map(str::to_owned) else {
            info.lines = vec!["Session directory unavailable".into()];
            return;
        };
        info.owner = Some((id.clone(), cwd.clone()));
        let python = std::env::var_os("DOXA_LORE_PYTHON")
            .map(PathBuf::from).unwrap_or_else(|| PathBuf::from("python3"));
        let (tx, rx) = mpsc::sync_channel(1);
        self.memory_menu_pending = Some((id, cwd.clone(), rx));
        std::thread::spawn(move || {
            let result = crate::memory_menu::fetch(&python, Path::new(&cwd));
            let _ = tx.send(result);
        });
    }

    fn hover_chooser(&mut self, column: u16, row: u16) -> bool {
        let Some(menu) = self.active_chooser_rect() else { return false; };
        if column <= menu.x || column >= menu.right().saturating_sub(1)
            || row <= menu.y || row >= menu.bottom().saturating_sub(1) { return false; }
        if let Some(index) = self.active_request_index() {
            if self.input_requests[index].kind != "ask_user" || self.input_requests[index].sending {
                return false;
            }
            if let Some(option) = ask_user_option_at(&self.input_requests[index], menu, row) {
                if self.input_requests[index].selected != option {
                    self.input_requests[index].selected = option;
                    return true;
                }
            }
            return false;
        }
        let visible = usize::from(menu.height.saturating_sub(3)).max(1);
        let attach_len = self.attach_picker.as_ref().map(|_| self.attach_matches().len());
        if let Some(settings) = self.settings_menu.as_mut() {
            if (menu.y + 3..menu.y + 5).contains(&row) {
                let index = usize::from(row - menu.y - 3);
                if settings.selected != index { settings.selected = index; return true; }
            }
        } else if let Some(picker) = self.branch_picker.as_mut() {
            if row < menu.y + 2 { return false; }
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - menu.y - 2);
            if index < picker.branches.len() && picker.selected != index { picker.selected = index; return true; }
        } else if let Some(picker) = self.repo_picker.as_mut() {
            if row < menu.y + 2 { return false; }
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - menu.y - 2);
            if index < picker.paths.len() && picker.selected != index { picker.selected = index; return true; }
        } else if self.engine_picker {
            let offset = if menu.height >= 10 { 4 } else { 2 };
            if row < menu.y + offset { return false; }
            let visible = usize::from(menu.height.saturating_sub(offset + 1)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, self.engine_selected, visible);
            let index = start + usize::from(row - menu.y - offset);
            if index < ENGINE_CHOICES.len() && self.engine_selected != index { self.engine_selected = index; return true; }
        } else if let Some(form) = self.new_session.as_mut() {
            let first = menu.y + if menu.height >= 8 { 4 } else { 2 };
            let fields = if vendor_models(form.engine).is_empty() { 2 } else { 3 };
            if row >= first && usize::from(row - first) < fields {
                let index = usize::from(row - first);
                if form.field != index { form.field = index; return true; }
            }
        } else if let Some((_, selected)) = self.permission_picker.as_mut() {
            let offset = if menu.height >= 10 { 4 } else { 2 };
            if row < menu.y + offset { return false; }
            let visible = usize::from(menu.height.saturating_sub(offset + 1)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, *selected, visible);
            let index = start + usize::from(row - menu.y - offset);
            if index < PERMISSION_CHOICES.len() && *selected != index { *selected = index; return true; }
        } else if let Some(picker) = self.effort_picker.as_mut() {
            if row < menu.y + 3 { return false; }
            let visible = usize::from(menu.height.saturating_sub(4)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - menu.y - 3);
            if index < picker.levels.len() && picker.selected != index { picker.selected = index; return true; }
        } else if let Some(picker) = self.model_picker.as_mut() {
            if row < menu.y + 3 { return false; }
            let visible = usize::from(menu.height.saturating_sub(4)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - menu.y - 3);
            if index < picker.models.len() && picker.selected != index { picker.selected = index; return true; }
        } else if let Some(picker) = self.attach_picker.as_mut() {
            if row < menu.y + 2 { return false; }
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - menu.y - 2);
            if index < attach_len.unwrap_or(0) && picker.selected != index { picker.selected = index; return true; }
        } else if let Some(picker) = self.queue_picker.as_mut() {
            if row < menu.y + 2 { return false; }
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - menu.y - 2);
            if index < picker.rows.len() && picker.selected != index { picker.selected = index; return true; }
        } else if self.history_modal {
            if row < menu.y + 2 { return false; }
            if let Some((index, header, _)) = self.history_rows(visible).get(usize::from(row - menu.y - 2)) {
                if *header && self.history_selected != *index { self.history_selected = *index; return true; }
            }
        } else if let Some(picker) = self.lore_picker.as_mut() {
            if picker.review.is_some() || picker.belief_review.is_some() || picker.evidence.is_some()
                || picker.pending.is_some() { return false; }
            let compact = menu.height < 10;
            let first = if picker.proposal_mode { menu.y + 4 } else { menu.y + if compact { 3 } else { 6 } };
            if row < first { return false; }
            let reserve = if picker.proposal_mode { 6 } else if compact { 4 } else { 8 };
            let visible = usize::from(menu.height.saturating_sub(reserve)).max(1);
            if usize::from(row - first) >= visible { return false; }
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - first);
            let count = if picker.proposal_mode { picker.proposals.len() } else { picker.rows.len() };
            if index < count && picker.selected != index { picker.selected = index; return true; }
        } else if self.action_menu {
            let visible = usize::from(menu.height.saturating_sub(2)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, self.action_selected, visible);
            let index = start + usize::from(row - menu.y - 1);
            if index < ACTIONS.len() && self.action_selected != index { self.action_selected = index; return true; }
        } else if !self.slash_suggestions().is_empty() {
            let visible = usize::from(menu.height.saturating_sub(2)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, self.slash_selected, visible);
            let index = start + usize::from(row - menu.y - 1);
            if index < self.slash_suggestions().len() && self.slash_selected != index { self.slash_selected = index; return true; }
        }
        false
    }

    fn mouse(&mut self, mouse: MouseEvent) -> bool {
        if self.drag == Some(DragTarget::Chooser) {
            match mouse.kind {
                MouseEventKind::Drag(MouseButton::Left) => {
                    let layout = self.layout(self.size);
                    let pane = layout.panes.map_or(layout.body, |panes| panes[self.active_group]);
                    let draft = self.groups[self.active_group].active_id().map_or("", |_| self.input.as_str());
                    let prompt = prompt_height(draft, pane.height);
                    let max = pane.height.saturating_sub(3 + prompt + 1 + 1 + 1);
                    let bottom = pane.bottom().saturating_sub(prompt + 2);
                    self.chooser_height_override.set(Some(bottom.saturating_sub(mouse.row).clamp(5, max.max(5))));
                    return true;
                }
                MouseEventKind::Up(MouseButton::Left) => { self.drag = None; return true; }
                _ => {}
            }
        }
        if mouse.kind == MouseEventKind::Moved {
            if self.hover_chooser(mouse.column, mouse.row) {
                self.chip_hover = None;
                self.link_hover = None;
                return true;
            }
            let overlay = self.link_interaction_blocked();
            let chip = (!overlay).then(|| self.chip_hit_at(mouse.column, mouse.row)).flatten();
            let link = (!overlay).then(|| self.link_at(mouse.column, mouse.row)).flatten();
            let changed = self.chip_hover != chip || self.link_hover != link;
            self.chip_hover = chip;
            self.link_hover = link;
            return changed;
        }
        if mouse.kind == MouseEventKind::Down(MouseButton::Left)
            && self.active_chooser_rect().is_some_and(|area|
                mouse.row == area.y && mouse.column >= area.x && mouse.column < area.right()) {
            self.drag = Some(DragTarget::Chooser);
            return true;
        }
        if let Some(index) = self.active_request_index() {
            if self.input_requests[index].kind == "ask_user"
                && mouse.kind == MouseEventKind::Down(MouseButton::Left) {
                if let Some(menu) = self.active_chooser_rect() {
                    if mouse.column > menu.x && mouse.column < menu.right().saturating_sub(1) {
                        if let Some(option) = ask_user_option_at(&self.input_requests[index], menu, mouse.row) {
                            self.input_requests[index].selected = option;
                            return self.request_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
                        }
                    }
                }
            }
        }
        if self.repo_picker.is_some() {
            let Some(menu) = self.active_chooser_rect() else { return false; };
            let inside = menu.contains(ratatui::layout::Position::new(mouse.column, mouse.row));
            return match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    if !inside { self.repo_picker = None; return true; }
                    if mouse.row >= menu.y + 2 && mouse.row < menu.bottom().saturating_sub(1) {
                        let picker = self.repo_picker.as_mut().unwrap();
                        let visible = usize::from(menu.height.saturating_sub(3)).max(1);
                        let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
                        let index = start + usize::from(mouse.row - menu.y - 2);
                        if index < picker.paths.len() {
                            picker.selected = index;
                            return self.repo_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
                        }
                    }
                    true
                }
                MouseEventKind::ScrollUp if inside => {
                    let picker = self.repo_picker.as_mut().unwrap();
                    picker.selected = picker.selected.saturating_sub(1);
                    true
                }
                MouseEventKind::ScrollDown if inside => {
                    let picker = self.repo_picker.as_mut().unwrap();
                    picker.selected = (picker.selected + 1).min(picker.paths.len() - 1);
                    true
                }
                _ => false,
            }
        }
        if mouse.kind == MouseEventKind::Down(MouseButton::Left)
            && mouse.modifiers.contains(KeyModifiers::CONTROL)
            && !self.link_interaction_blocked()
        {
            if let Some(url) = self.link_at(mouse.column, mouse.row) {
                self.pending_open_urls.push(url);
                return true;
            }
        }
        if self.settings_menu.is_some() {
            let menu = self.active_chooser_rect();
            if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
                if let Some(area) = menu.filter(|area| area.contains(
                    ratatui::layout::Position::new(mouse.column, mouse.row))) {
                    let index = usize::from(mouse.row.saturating_sub(area.y.saturating_add(3)));
                    if (area.y + 3..area.y + 5).contains(&mouse.row) {
                        let current = self.settings_menu.as_ref().unwrap().selected;
                        self.settings_menu.as_mut().unwrap().selected = index;
                        if current == index {
                            return self.settings_menu_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
                        }
                    }
                } else {
                    self.settings_menu = None;
                }
                return true;
            }
            return false;
        }
        if self.chip_info.is_some() {
            let inside_menu = self.active_chooser_rect().is_some_and(|area| area.contains(
                ratatui::layout::Position::new(mouse.column, mouse.row)));
            if let Some(info) = self.chip_info.as_mut().filter(|info| info.kind == "memory") {
                if inside_menu {
                    match mouse.kind {
                        MouseEventKind::ScrollUp => {
                            info.scroll = info.scroll.saturating_sub(3);
                            return true;
                        }
                        MouseEventKind::ScrollDown => {
                            info.scroll = info.scroll.saturating_add(3).min(info.lines.len().saturating_sub(1));
                            return true;
                        }
                        _ => {}
                    }
                }
            }
            if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
                let inside = self.active_chooser_rect().is_some_and(|area| area.contains(
                    ratatui::layout::Position::new(mouse.column, mouse.row)));
                self.chip_info = None;
                self.memory_menu_pending = None;
                if inside { return true; }
            } else { return false; }
        }
        let suggestions = self.slash_suggestions();
        if !suggestions.is_empty() {
            if let Some(menu) = self.active_chooser_rect() {
                if menu.contains(ratatui::layout::Position::new(mouse.column, mouse.row)) {
                    match mouse.kind {
                        MouseEventKind::Down(MouseButton::Left) => {
                            if mouse.column <= menu.x || mouse.column >= menu.right().saturating_sub(1)
                                || mouse.row <= menu.y || mouse.row >= menu.bottom().saturating_sub(1) {
                                return true;
                            }
                            let visible = usize::from(menu.height.saturating_sub(2)).max(1);
                            let start = chooser_visible_start(&self.chooser_view_start, self.slash_selected, visible);
                            let position = start + usize::from(mouse.row.saturating_sub(menu.y + 1));
                            if mouse.row > menu.y && position < suggestions.len() {
                                self.slash_selected = position;
                                self.complete_slash();
                            }
                            return true;
                        }
                        MouseEventKind::ScrollUp => {
                            self.slash_selected = self.slash_selected.saturating_sub(1);
                            return true;
                        }
                        MouseEventKind::ScrollDown => {
                            self.slash_selected = (self.slash_selected + 1).min(suggestions.len() - 1);
                            return true;
                        }
                        _ => {}
                    }
                }
            }
        }
        if self.branch_picker.is_some() {
            let Some(menu) = self.active_chooser_rect() else { return false; };
            match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    if !menu.contains(ratatui::layout::Position::new(mouse.column, mouse.row)) {
                        self.branch_picker = None;
                    } else {
                        let first_row = menu.y.saturating_add(2);
                        if mouse.row >= first_row && mouse.row < menu.bottom().saturating_sub(1) {
                            let visible = usize::from(menu.height.saturating_sub(3)).max(1);
                            let picker = self.branch_picker.as_mut().unwrap();
                            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
                            let position = start + usize::from(mouse.row - first_row);
                            if position < picker.branches.len() {
                                picker.selected = position;
                                self.choose_branch();
                            }
                        }
                    }
                    return true;
                }
                MouseEventKind::ScrollUp => {
                    let picker = self.branch_picker.as_mut().unwrap();
                    picker.selected = picker.selected.saturating_sub(1);
                    return true;
                }
                MouseEventKind::ScrollDown => {
                    let picker = self.branch_picker.as_mut().unwrap();
                    picker.selected = (picker.selected + 1).min(picker.branches.len().saturating_sub(1));
                    return true;
                }
                _ => return false,
            }
        }
        if self.attach_picker.is_some() {
            let Some(menu) = self.active_chooser_rect() else { return false; };
            match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    if mouse.column < menu.x || mouse.column >= menu.right()
                        || mouse.row < menu.y || mouse.row >= menu.bottom() {
                        self.attach_picker = None;
                        return true;
                    }
                    let first_row = menu.y.saturating_add(2);
                    if mouse.row >= first_row && mouse.row < menu.bottom().saturating_sub(1) {
                        let visible = usize::from(menu.height.saturating_sub(3)).max(1);
                        let selected = self.attach_picker.as_ref().unwrap().selected;
                        let start = chooser_visible_start(&self.chooser_view_start, selected, visible);
                        let position = start + usize::from(mouse.row - first_row);
                        if position < self.attach_matches().len() {
                            self.attach_picker.as_mut().unwrap().selected = position;
                            self.open_selected_attach();
                        }
                    }
                    return true;
                }
                MouseEventKind::ScrollUp => {
                    let picker = self.attach_picker.as_mut().unwrap();
                    picker.selected = picker.selected.saturating_sub(1);
                    return true;
                }
                MouseEventKind::ScrollDown => {
                    let max = self.attach_matches().len().saturating_sub(1);
                    let picker = self.attach_picker.as_mut().unwrap();
                    picker.selected = (picker.selected + 1).min(max);
                    return true;
                }
                _ => return false,
            }
        }
        if self.history_modal {
            let Some(menu) = self.active_chooser_rect() else { return false; };
            match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    if mouse.column < menu.x || mouse.column >= menu.right()
                        || mouse.row < menu.y || mouse.row >= menu.bottom() {
                        self.history_modal = false;
                        self.cancel_history_query();
                        return true;
                    }
                    let first_row = menu.y.saturating_add(2);
                    if mouse.row >= first_row && mouse.row < menu.bottom().saturating_sub(1) {
                        let visible = usize::from(menu.height.saturating_sub(3)).max(1);
                        if let Some((position, header, _)) = self.history_rows(visible)
                            .get(usize::from(mouse.row - first_row)) {
                            self.history_selected = *position;
                            if *header { self.open_selected_history(); }
                        }
                    }
                    return true;
                }
                MouseEventKind::ScrollUp => {
                    self.history_selected = self.history_selected.saturating_sub(1);
                    return true;
                }
                MouseEventKind::ScrollDown => {
                    self.history_selected = (self.history_selected + 1)
                        .min(self.history_matches().len().saturating_sub(1));
                    return true;
                }
                _ => return false,
            }
        }
        if self.queue_picker.is_some() {
            let Some(menu) = self.active_chooser_rect() else { return false; };
            match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    if mouse.column < menu.x || mouse.column >= menu.right()
                        || mouse.row < menu.y || mouse.row >= menu.bottom() {
                        self.queue_picker = None;
                    } else {
                        let first = menu.y.saturating_add(2);
                        if mouse.row >= first && mouse.row < menu.bottom().saturating_sub(1) {
                            let picker = self.queue_picker.as_mut().unwrap();
                            let visible = usize::from(menu.height.saturating_sub(3)).max(1);
                            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
                            picker.selected = (start + usize::from(mouse.row - first)).min(picker.rows.len().saturating_sub(1));
                        }
                    }
                    return true;
                }
                MouseEventKind::ScrollUp => {
                    let picker = self.queue_picker.as_mut().unwrap();
                    picker.selected = picker.selected.saturating_sub(1);
                    return true;
                }
                MouseEventKind::ScrollDown => {
                    let picker = self.queue_picker.as_mut().unwrap();
                    picker.selected = (picker.selected + 1).min(picker.rows.len().saturating_sub(1));
                    return true;
                }
                _ => return false,
            }
        }
        if self.lore_picker.is_some() {
            let Some(menu) = self.active_chooser_rect() else { return false; };
            if !menu.contains(ratatui::layout::Position::new(mouse.column, mouse.row)) { return true; }
            let picker = self.lore_picker.as_mut().unwrap();
            if picker.pending.is_some() || picker.resolving { return true; }
            if let Some(review) = &picker.belief_review {
                let width = usize::from(menu.width.saturating_sub(3)).max(1);
                let visible = usize::from(menu.height.saturating_sub(REVIEW_BODY_RESERVE));
                let full = format!("Subject: {}\nClaim: {}", review.subject(), review.claim());
                let total = raw_visual_rows(&full, width).len();
                if picker.review_width != width {
                    picker.review_width = width;
                    picker.review_scroll = 0;
                    picker.review_seen = 0;
                    picker.belief_action = None;
                    picker.retract_armed = false;
                }
                if visible == 0 { return true; }
                if visible > 0 && picker.review_scroll <= picker.review_seen {
                    picker.review_seen = picker.review_seen.max(picker.review_scroll.saturating_add(visible)).min(total);
                }
                let max_scroll = total.saturating_sub(visible);
                match mouse.kind {
                    MouseEventKind::ScrollUp => picker.review_scroll = picker.review_scroll.saturating_sub(3),
                    MouseEventKind::ScrollDown => picker.review_scroll = picker.review_scroll.saturating_add(3).min(max_scroll),
                    _ => {}
                }
                if visible > 0 && picker.review_scroll <= picker.review_seen {
                    picker.review_seen = picker.review_seen.max(picker.review_scroll.saturating_add(visible)).min(total);
                }
                return true;
            }
            if picker.evidence.is_some() || picker.review.is_some() { return true; }
            if picker.proposal_mode {
                let visible = usize::from(menu.height.saturating_sub(6)).max(1);
                match mouse.kind {
                    MouseEventKind::Down(MouseButton::Left) => {
                        let first = menu.y + 4;
                        let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
                        if mouse.row >= first && mouse.row < first.saturating_add(visible as u16) {
                            let index = start + usize::from(mouse.row - first);
                            if let Some(pid) = picker.proposals.get(index).map(|row| row.pid.clone()) {
                                if picker.selected == index {
                                    let cwd = picker.cwd.clone();
                                    self.load_lore(lore_picker::Query::Review(cwd, pid));
                                } else { picker.selected = index; }
                            }
                        }
                    }
                    MouseEventKind::ScrollUp => picker.selected = picker.selected.saturating_sub(1),
                    MouseEventKind::ScrollDown => picker.selected = (picker.selected + 1).min(picker.proposals.len().saturating_sub(1)),
                    _ => {}
                }
                return true;
            }
            match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    let compact = menu.height < 10;
                    let first = menu.y.saturating_add(if compact { 3 } else { 6 });
                    let visible = usize::from(menu.height.saturating_sub(if compact { 4 } else { 8 })).max(1);
                    let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
                    if mouse.row >= first && mouse.row < first.saturating_add(visible as u16) {
                        let index = start + usize::from(mouse.row - first);
                        if let Some(id) = picker.rows.get(index).map(|row| row.id) {
                            if picker.selected == index {
                                let cwd = picker.cwd.clone();
                                self.load_lore(lore_picker::Query::BeliefReview(cwd, id));
                            } else { picker.selected = index; }
                        }
                    }
                }
                MouseEventKind::ScrollUp => picker.selected = picker.selected.saturating_sub(1),
                MouseEventKind::ScrollDown => picker.selected = (picker.selected + 1).min(picker.rows.len().saturating_sub(1)),
                _ => {}
            }
            return true;
        }
        if self.active_request_index().is_none()
            && matches!(mouse.kind, MouseEventKind::Down(MouseButton::Left))
            && (self.engine_picker || self.new_session.is_some() || self.model_picker.is_some() || self.effort_picker.is_some() || self.permission_picker.is_some()) {
            let Some(menu) = self.active_chooser_rect() else { return false; };
            let (x, y, width, height) = (menu.x, menu.y, menu.width, menu.height);
            if mouse.column < x || mouse.column >= x + width || mouse.row < y || mouse.row >= y + height {
                self.engine_picker = false;
                self.new_session = None;
                self.model_picker = None;
                self.effort_picker = None;
                self.permission_picker = None;
                self.permission_confirm_dont_ask = false;
                return true;
            }
            if mouse.column == x || mouse.column == x + width - 1 || mouse.row == y + height - 1 {
                return true;
            }
            if self.engine_picker {
                let offset = if height >= 10 { 4 } else { 2 };
                if mouse.row < y + offset { return true; }
                let visible = usize::from(height.saturating_sub(offset + 1)).max(1);
                let start = chooser_visible_start(&self.chooser_view_start, self.engine_selected, visible);
                let row = start + usize::from(mouse.row.saturating_sub(y + offset));
                if row < ENGINE_CHOICES.len() {
                    self.engine_selected = row;
                    self.select_new_engine();
                }
                return true;
            }
            if self.new_session.is_some() { return true; }
            if let Some(picker) = &mut self.effort_picker {
                let visible = usize::from(height.saturating_sub(4)).max(1);
                let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
                let row = start + usize::from(mouse.row.saturating_sub(y + 3));
                if mouse.row >= y + 3 && row < picker.levels.len() {
                    picker.selected = row;
                    self.select_effort();
                }
                return true;
            }
            if let Some((_, selected)) = &mut self.permission_picker {
                let offset = if height >= 10 { 4 } else { 2 };
                if mouse.row >= y + offset {
                    let visible = usize::from(height.saturating_sub(offset + 1)).max(1);
                    let start = chooser_visible_start(&self.chooser_view_start, *selected, visible);
                    let row = start + usize::from(mouse.row - (y + offset));
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
            let visible = usize::from(height.saturating_sub(4)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
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
            || self.queue_picker.is_some()
            || self.attach_picker.is_some()
            || self.branch_picker.is_some()
            || self.lore_picker.is_some()
            || self.diff_modal
            || self.model_picker.is_some()
            || self.effort_picker.is_some()
            || self.permission_picker.is_some()
            || self.engine_picker
            || self.stop_confirmation.is_some()
            || self.new_session.is_some() || self.repo_picker.is_some()
        {
            // A request belongs to its session, not the whole terminal. Let
            // the user focus the other pane and keep working while this one
            // waits for an answer. The request stays visible in its own pane.
            if self.active_request_index().is_some()
                && mouse.kind == MouseEventKind::Down(MouseButton::Left)
                && self.layout(self.size).panes.is_some_and(|panes| {
                    panes[1 - self.active_group].contains(
                        ratatui::layout::Position::new(mouse.column, mouse.row))
                })
            {
                let other = 1 - self.active_group;
                let pane = self.layout(self.size).panes.unwrap()[other];
                let prompt = self.pane_regions(other, pane)[4];
                self.active_group = other;
                self.focus = if prompt.contains(ratatui::layout::Position::new(mouse.column, mouse.row)) {
                    Focus::Prompt
                } else {
                    Focus::Transcript
                };
                return true;
            }
            self.drag = None;
            return false;
        }
        if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
            if let Some(hit) = self.chip_hit_at(mouse.column, mouse.row) {
                self.chip_hover = None;
                self.active_group = hit.group;
                match hit.kind {
                    "permission" => self.open_permission_picker(),
                    "engine" => self.open_engine_picker(),
                    "model" => self.open_model_picker(),
                    "effort" => self.open_effort_picker(),
                    "beliefs" => self.open_lore_picker(),
                    "repo" | "directory" => self.open_repo_picker(hit.group),
                    "memory" => self.open_memory_menu(hit.group),
                    "more" => {
                        let visible = self.chip_window(hit.group, usize::from(self.pane_regions(hit.group, hit.pane)[3].width));
                        let count = visible.len().saturating_sub(1).max(1);
                        self.chip_offsets[hit.group] = (self.chip_offsets[hit.group] + count) % self.chips(hit.group).len();
                    }
                    kind => self.open_chip_info(kind, hit.group),
                }
                return true;
            }
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
                let section_hit = self.visible_tool_sections.borrow().iter()
                    .find(|(rect, _, _, _)| rect.contains(ratatui::layout::Position::new(mouse.column, mouse.row)))
                    .cloned();
                if let Some((_, group, id, section)) = section_hit {
                    self.active_group = group;
                    self.focus = Focus::Transcript;
                    self.selected_tool_sections.insert(id.clone(), section);
                    self.toggle_tool_section(id, section);
                    return true;
                }
                let layout = self.layout(self.size);
                if let Some(rail) = layout.rail {
                    if mouse.column > rail.x && mouse.column < rail.right().saturating_sub(1)
                        && mouse.row > rail.y && mouse.row < rail.bottom().saturating_sub(1) {
                        let row = usize::from(mouse.row - rail.y - 1);
                        match self.rail_rows().get(row) {
                            Some(RailRow::Heading(index)) => {
                                self.collections[*index].collapsed = !self.collections[*index].collapsed;
                                self.rail_selected = self.rail_selected.min(self.rail_order().len().saturating_sub(1));
                                self.focus = Focus::Rail;
                                return true;
                            }
                            Some(RailRow::Session(index)) => {
                                self.rail_selected = self.rail_order().iter().position(|visible| visible == index).unwrap_or(0);
                                self.open_selected();
                                return true;
                            }
                            Some(RailRow::LooseHeading) => { self.focus = Focus::Rail; return true; }
                            None => {}
                        }
                    }
                }
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
                                if mouse.row == pane.y.saturating_add(1) {
                                    if let Some(tab) = self.tab_at(index, pane, mouse.column) {
                                        self.active_group = index;
                                        self.groups[index].active = tab;
                                        self.groups[index].scroll = 0;
                                        self.focus = Focus::Transcript;
                                        return true;
                                    }
                                }
                                let draft = self.groups[index].active_id().map(|id| {
                                    if self.active_group == index { self.input.as_str() }
                                    else { self.input_drafts.get(&(index, id.to_owned()))
                                        .map(|(text, _)| text.as_str()).unwrap_or("") }
                                }).unwrap_or("");
                                let prompt_top = pane.bottom().saturating_sub(prompt_height(draft, pane.height) + 1);
                                self.active_group = index;
                                self.focus = if mouse.row >= prompt_top {
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
        self.visible_tool_sections.borrow_mut().clear();
        self.visible_links.borrow_mut().clear();
        *self.rendered_chip_hits.borrow_mut() = Some(Vec::new());
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
        if self.active_chooser_rect().is_none() && !self.active_request_index().is_some_and(|index| self.input_requests[index].kind == "ask_user") {
            let width = area.width.saturating_sub(2).min(74);
            let height = area.height.saturating_sub(2).min(19);
            if width >= 18 && height >= 3 {
                let fallback = Rect::new(area.x + (area.width - width) / 2,
                    area.y + (area.height - height) / 2, width, height);
                if self.settings_menu.is_some() || self.action_menu || self.lore_picker.is_some() || self.repo_picker.is_some() || self.engine_picker
                    || self.new_session.is_some() || self.model_picker.is_some() || self.effort_picker.is_some() || self.permission_picker.is_some() {
                    frame.render_widget(Clear, fallback);
                    if self.settings_menu.is_some() { self.draw_settings_menu(frame, fallback); }
                    else if self.action_menu { self.draw_actions(frame, fallback); }
                    else if self.repo_picker.is_some() { self.draw_repo_picker(frame, fallback); }
                    else if self.lore_picker.is_some() { self.draw_lore_picker(frame, fallback); }
                    else { self.draw_chip_picker(frame, fallback); }
                }
            }
        }
        self.draw_tool_cards(frame, area);
        if self.map_modal {
            self.peer_map.render(
                frame,
                area,
                self.groups[self.active_group].active_id().unwrap_or(""),
            );
        }
        self.draw_diff(frame, area);
        self.draw_stop_confirmation(frame, area);
        if !self.active_request_index().is_some_and(|index| self.input_requests[index].kind == "ask_user")
            || self.active_chooser_rect().is_none() {
            self.draw_request(frame, area, false);
        }
        self.draw_chip_tooltip(frame);
    }

    fn draw_chip_tooltip(&self, frame: &mut Frame) {
        let Some(hit) = &self.chip_hover else { return; };
        if self.chip_info.is_some() || self.active_chooser_rect().is_some()
            || self.active_request_index().is_some() || self.map_modal || self.diff_modal
            || self.tool_modal || self.stop_confirmation.is_some() { return; }
        let hint = if hit.kind == "repo" {
            self.repo_detail(hit.group).unwrap_or_else(|| chip_hint(hit.kind).to_owned())
        } else { chip_hint(hit.kind).to_owned() };
        if hint.is_empty() || hit.rect.y <= hit.pane.y.saturating_add(3) { return; }
        let width = (hint.width() + 2).min(usize::from(hit.pane.width)) as u16;
        let x = hit.rect.x.min(hit.pane.right().saturating_sub(width));
        let area = Rect::new(x, hit.rect.y - 1, width, 1);
        let text = clipped_title(&format!(" {hint} "), usize::from(width)).0;
        frame.render_widget(Paragraph::new(text).style(Style::default()
            .fg(theme::ACCENT).bg(theme::HIGHLIGHT)), area);
    }

    fn draw_stop_confirmation(&self, frame: &mut Frame, area: Rect) {
        let Some(id) = &self.stop_confirmation else { return; };
        let width = area.width.saturating_sub(4).min(78);
        let height = area.height.saturating_sub(4).min(12);
        if width < 36 || height < 8 { return; }
        let modal = Rect::new(area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2, width, height);
        let lines = vec![Line::from(clipped_title(&format!(" Session: {}", safe_label(id)),
                usize::from(width.saturating_sub(2))).0),
            Line::from(" Daemon shutdown runs; tab and draft stay."),
            Line::from(""), Line::from(" Y stop · Esc/N cancel")];
        frame.render_widget(Clear, modal);
        frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false })
            .block(Block::default().title(" Stop active session ").borders(Borders::ALL)
                .border_style(Style::default().fg(theme::ERROR)))
            .style(Style::default().fg(theme::TEXT).bg(theme::RAISED)), modal);
    }

    fn draw_chip_picker(&self, frame: &mut Frame, area: Rect) {
        if !self.engine_picker && self.new_session.is_none() && self.model_picker.is_none() && self.effort_picker.is_none() && self.permission_picker.is_none() { return; }
        let height = area.height;
        let modal = area;
        let mut lines = Vec::new();
        let title;
        if self.engine_picker {
            title = " New session · choose engine · Enter continue · Esc close ";
            if height >= 10 {
                lines.push(Line::from(" Select an engine for a new session:"));
                lines.push(Line::from(" Model and first prompt follow."));
                lines.push(Line::from(""));
            } else { lines.push(Line::from(" Choose engine:")); }
            let offset = if height >= 10 { 4 } else { 2 };
            let visible = usize::from(height.saturating_sub(offset + 1)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, self.engine_selected, visible);
            for (index, engine) in ENGINE_CHOICES.iter().enumerate().skip(start).take(visible) {
                lines.push(Line::styled(format!(" {} {}", if index == self.engine_selected { '›' } else { ' ' }, engine),
                    chooser_row_style(index == self.engine_selected)));
            }
        } else if let Some(form) = &self.new_session {
            title = " New session · Tab field · Enter continue/start · Esc close ";
            let name = match form.engine { launch::Engine::Codex => "codex", launch::Engine::Claude => "claude",
                launch::Engine::DeepSeek => "deepseek", launch::Engine::Glm => "glm", launch::Engine::Fixture => "fixture" };
            lines.push(Line::from(format!(" Engine: {name}")));
            if height >= 8 {
                lines.push(Line::from(if vendor_models(form.engine).is_empty() {
                    " Blank model uses configured engine default."
                } else { " Left/Right choose vendor model and effort." }));
                lines.push(Line::from(if vendor_models(form.engine).is_empty() {
                    "".to_owned()
                } else { format!(" {}", safe_label(&form.catalog_note)) }));
            }
            lines.push(Line::styled(format!(" {} Model: {}", if form.field == 0 { '›' } else { ' ' },
                safe_label(&form.model)),
                chooser_row_style(form.field == 0)));
            let prompt_field = if vendor_models(form.engine).is_empty() { 1 } else { 2 };
            if prompt_field == 2 {
                lines.push(Line::styled(format!(" {} Effort: {}", if form.field == 1 { '›' } else { ' ' },
                    form.effort.as_deref().unwrap_or("unknown")),
                    chooser_row_style(form.field == 1)));
            }
            lines.push(Line::styled(format!(" {} First prompt: {}", if form.field == prompt_field { '›' } else { ' ' }, safe_label(&form.prompt)),
                chooser_row_style(form.field == prompt_field)));
            if form.engine == launch::Engine::Claude {
                lines.push(Line::from(" Claude sidecar is bundled by the preview installer."));
            }
        } else if let Some((id, selected)) = &self.permission_picker {
            title = " Claude permissions · this session · Enter select · Esc close ";
            if height >= 10 {
                lines.push(Line::from(" Changes how Claude handles tool permission requests."));
                lines.push(Line::from(if self.permission_confirm_dont_ask {
                    " dontAsk silently denies unapproved calls. Enter again to confirm."
                } else {
                    " Current mode marked with ●; dontAsk requires confirmation."
                }));
                lines.push(Line::from(""));
            } else { lines.push(Line::from(" Permission mode:")); }
            let offset = if height >= 10 { 4 } else { 2 };
            let visible = usize::from(height.saturating_sub(offset + 1)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, *selected, visible);
            for (index, (mode, description)) in PERMISSION_CHOICES.iter().enumerate().skip(start).take(visible) {
                let current = self.permission_modes.get(id).is_some_and(|current| current == mode);
                lines.push(Line::styled(format!(" {} {} {} · {}", if index == *selected { '›' } else { ' ' },
                    if current { '●' } else { ' ' }, mode, description),
                    chooser_row_style(index == *selected)));
            }
        } else if let Some(picker) = &self.effort_picker {
            title = " Effort · this session · Enter select · Esc close ";
            let current = self.session_efforts.get(&picker.session_id).map(String::as_str).unwrap_or("unknown");
            lines.push(Line::from(format!(" Current: {current} · {}/{} · idle session required", picker.engine, picker.model)));
            lines.push(Line::from(""));
            let visible = usize::from(height.saturating_sub(4)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            for (index, level) in picker.levels.iter().enumerate().skip(start).take(visible) {
                lines.push(Line::styled(format!(" {} {}", if index == picker.selected { '›' } else { ' ' }, level),
                    chooser_row_style(index == picker.selected)));
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
            let visible = usize::from(height.saturating_sub(4)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            for (index, model) in picker.models.iter().enumerate().skip(start).take(visible) {
                lines.push(Line::styled(format!(" {} {}", if index == picker.selected { '›' } else { ' ' }, model),
                    chooser_row_style(index == picker.selected)));
            }
        }
        frame.render_widget(Paragraph::new(lines).block(Block::default().title(title)
            .borders(Borders::ALL).border_style(Style::default().fg(theme::ACCENT)))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)), modal);
    }

    fn draw_settings_menu(&self, frame: &mut Frame, area: Rect) {
        let Some(menu) = &self.settings_menu else { return; };
        let mut lines = vec![
            Line::from(" env > config.toml > default"),
            Line::from(""),
        ];
        for (index, key) in ["linger_secs", "worktree_per_session"].iter().enumerate() {
            let (value, shadowed) = &menu.rows[index];
            let shown = if index == 0 { menu.linger_draft.as_deref().unwrap_or(value) } else { value };
            let suffix = if *shadowed {
                if index == 0 { " · DOXA_LINGER_SECS overrides config" }
                else { " · DOXA_WORKTREE overrides config" }
            } else { "" };
            lines.push(Line::styled(format!(" {} {}: {}{}", if menu.selected == index { '›' } else { ' ' },
                key, safe_label(shown), suffix),
                Style::default().fg(if menu.selected == index { theme::TEXT } else { theme::SECONDARY })
                    .bg(if menu.selected == index { theme::HIGHLIGHT } else { theme::RAISED })));
        }
        lines.push(Line::from(""));
        lines.push(Line::from(if menu.linger_draft.is_some() {
            " Enter save seconds · Esc cancel edit"
        } else {
            " Enter edit/toggle · U unset · Esc close"
        }));
        lines.push(Line::from(" Changes apply to new sessions; running sessions keep launch settings."));
        frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false })
            .block(Block::default().title(" Native settings ").borders(Borders::ALL)
                .border_style(Style::default().fg(theme::ACCENT)))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)), area);
    }

    fn draw_chip_info(&self, frame: &mut Frame, area: Rect) {
        let Some(info) = &self.chip_info else { return; };
        if matches!(info.kind, "memory" | "usage" | "context" | "help") {
            let current = self.groups[self.active_group].active_id().and_then(|id|
                self.session_cwds.get(id).and_then(|cwd| cwd.to_str()).map(|cwd| (id, cwd)));
            let owner_matches = info.owner.as_ref().is_some_and(|(id, cwd)| {
                if info.kind == "memory" { current == Some((id.as_str(), cwd.as_str())) }
                else { self.groups[self.active_group].active_id() == Some(id.as_str()) }
            });
            let message;
            let source = if info.owner.is_some() && !owner_matches {
                message = vec![format!("Session changed; reopen {}", info.kind)];
                &message
            } else { &info.lines };
            let visible = usize::from(area.height.saturating_sub(2)).max(1);
            let start = info.scroll.min(source.len().saturating_sub(visible));
            let lines: Vec<String> = source.iter().skip(start).take(visible)
                .map(|line| clipped_title(line, usize::from(area.width.saturating_sub(2))).0).collect();
            frame.render_widget(Paragraph::new(lines.join("\n"))
                .block(Block::default().title(format!(" {} · ↑↓ scroll · Esc close ",
                    if info.kind == "memory" { "LORE memory" } else { info.kind })).borders(Borders::ALL)
                    .border_style(Style::default().fg(theme::ACCENT)))
                .style(Style::default().fg(theme::TEXT).bg(theme::RAISED)), area);
            return;
        }
        let title = format!(" {} · Esc close ", safe_label(info.kind));
        let body = format!(" {}\n {}", safe_label(&info.label), chip_hint(info.kind));
        frame.render_widget(Paragraph::new(body).wrap(Wrap { trim: false })
            .block(Block::default().title(title).borders(Borders::ALL)
                .border_style(Style::default().fg(theme::ACCENT)))
            .style(Style::default().fg(theme::TEXT).bg(theme::RAISED)), area);
    }

    fn draw_history(&self, frame: &mut Frame, area: Rect) {
        if !self.history_modal { return; }
        let matches = self.history_matches();
        let mut lines = vec![Line::from(format!(" Search: {}", safe_label(&self.history_query)))];
        if matches.is_empty() { lines.push(Line::from(if self.history_pending.is_some() || self.history_query_due.is_some() { " Finding saved transcripts…" } else { " No matching sessions" })); }
        let visible = usize::from(area.height.saturating_sub(3)).max(1);
        for (position, header, label) in self.history_rows(visible) {
            let style = if header && position == self.history_selected { Style::default().fg(theme::ACCENT).bg(theme::HIGHLIGHT).add_modifier(Modifier::BOLD) }
                else if header { Style::default().fg(theme::SECONDARY) }
                else { Style::default().fg(theme::MUTED) };
            let label = clipped_title(&label, usize::from(area.width.saturating_sub(2))).0;
            let padded = format!("{label}{}", " ".repeat(usize::from(area.width.saturating_sub(2)).saturating_sub(label.width())));
            lines.push(Line::styled(padded, style));
        }
        frame.render_widget(Paragraph::new(lines).block(Block::default()
            .title(if self.history_resume { " Resume session · Enter to open · Esc close " }
                else { " Session history · type to filter " })
            .borders(Borders::ALL).border_style(Style::default().fg(theme::ACCENT))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED))), area);
    }

    fn draw_attach_picker(&self, frame: &mut Frame, area: Rect) {
        let Some(picker) = &self.attach_picker else { return; };
        let matches = self.attach_matches();
        let mut lines = vec![Line::from(format!(" Search: {}", safe_label(&picker.query)))];
        if matches.is_empty() { lines.push(Line::from(" No matching live sessions")); }
        let visible = usize::from(area.height.saturating_sub(3)).max(1);
        let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
        for (position, &index) in matches.iter().enumerate().skip(start).take(visible) {
            let session = &picker.rows[index];
            let title = if session.title.trim().is_empty() { "Untitled session" } else { &session.title };
            let label = format!(" {} {} · {}", if position == picker.selected { '›' } else { ' ' },
                safe_label(title), safe_label(&session.id));
            let style = if position == picker.selected { Style::default().fg(theme::ACCENT).bg(theme::HIGHLIGHT).add_modifier(Modifier::BOLD) }
                else { Style::default().fg(theme::SECONDARY) };
            let label = clipped_title(&label, usize::from(area.width.saturating_sub(2))).0;
            let padded = format!("{label}{}", " ".repeat(usize::from(area.width.saturating_sub(2)).saturating_sub(label.width())));
            lines.push(Line::styled(padded, style));
        }
        frame.render_widget(Paragraph::new(lines).block(Block::default()
            .title(" Attach live session · type to filter ")
            .borders(Borders::ALL).border_style(Style::default().fg(theme::ACCENT))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED))), area);
    }

    fn draw_branch_picker(&self, frame: &mut Frame, area: Rect) {
        let Some(picker) = &self.branch_picker else { return; };
        let mut lines = vec![Line::from(format!(" Current base: {}", safe_label(&picker.base)))];
        let visible = usize::from(area.height.saturating_sub(3)).max(1);
        let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
        for (index, branch) in picker.branches.iter().enumerate().skip(start).take(visible) {
            let current = if branch == &picker.base { " · current" } else { "" };
            let label = format!(" {} {}{}", if index == picker.selected { '›' } else { ' ' },
                safe_label(branch), current);
            let label = clipped_title(&label, usize::from(area.width.saturating_sub(2))).0;
            let padded = format!("{label}{}", " ".repeat(usize::from(area.width.saturating_sub(2)).saturating_sub(label.width())));
            let style = if index == picker.selected {
                Style::default().fg(theme::ACCENT).bg(theme::HIGHLIGHT).add_modifier(Modifier::BOLD)
            } else { Style::default().fg(theme::SECONDARY) };
            lines.push(Line::styled(padded, style));
        }
        frame.render_widget(Paragraph::new(lines).block(Block::default()
            .title(" Base branch · Enter select · Esc close ")
            .borders(Borders::ALL).border_style(Style::default().fg(theme::ACCENT))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED))), area);
    }

    fn draw_repo_picker(&self, frame: &mut Frame, area: Rect) {
        let Some(picker) = &self.repo_picker else { return; };
        let mut lines = vec![Line::from(" Select a folder · Enter browse · current opens new tab")];
        let visible = usize::from(area.height.saturating_sub(3)).max(1);
        let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
        let width = usize::from(area.width.saturating_sub(2));
        for (index, path) in picker.paths.iter().enumerate().skip(start).take(visible) {
            let marker = if index == 0 { "current" }
                else if picker.current_dir.parent() == Some(path.as_path()) { "up" }
                else { "folder" };
            let label = format!(" {} {} · {}", if index == picker.selected { '›' } else { ' ' },
                marker, safe_label(&repo_path_label(path)));
            let label = clipped_title(&label, width).0;
            let padded = format!("{label}{}", " ".repeat(width.saturating_sub(label.width())));
            let style = if index == picker.selected {
                Style::default().fg(theme::ACCENT).bg(theme::HIGHLIGHT).add_modifier(Modifier::BOLD)
            } else { Style::default().fg(theme::SECONDARY) };
            lines.push(Line::styled(padded, style));
        }
        frame.render_widget(Paragraph::new(lines).block(Block::default()
            .title(" Directory · ↑↓ select · Enter browse/open · Esc close ")
            .borders(Borders::ALL).border_style(Style::default().fg(theme::ACCENT)))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)), area);
    }

    fn draw_slash_suggestions(&self, frame: &mut Frame, area: Rect) {
        let matches = self.slash_suggestions();
        if matches.is_empty() { return; }
        let visible = usize::from(area.height.saturating_sub(2)).max(1);
        let selected = self.slash_selected.min(matches.len() - 1);
        let start = chooser_visible_start(&self.chooser_view_start, selected, visible);
        let rows: Vec<Line> = matches.iter().enumerate().skip(start).take(visible)
            .map(|(index, (command, description))| {
                let label = format!(" {} {:<12} {}", if index == selected { '›' } else { ' ' }, command, description);
                let label = clipped_title(&label, usize::from(area.width.saturating_sub(2))).0;
                let style = if index == selected {
                    Style::default().fg(theme::ACCENT).bg(theme::HIGHLIGHT).add_modifier(Modifier::BOLD)
                } else { Style::default().fg(theme::SECONDARY) };
                Line::styled(label, style)
            }).collect();
        frame.render_widget(Paragraph::new(rows).block(Block::default()
            .title(" Commands · ↑/↓ select · Tab complete ")
            .borders(Borders::ALL).border_style(Style::default().fg(theme::ACCENT)))
            .style(Style::default().bg(theme::RAISED)), area);
    }

    fn draw_queue_picker(&self, frame: &mut Frame, area: Rect) {
        let Some(picker) = &self.queue_picker else { return; };
        let mut lines = vec![Line::from(if picker.loading { " Refreshing queue…" }
            else if picker.rows.is_empty() { " No queued prompts" }
            else if picker.cancelling.is_some() { " Cancelling selected prompt…" }
            else { " X cancel selected · R refresh · Esc close" })];
        let visible = usize::from(area.height.saturating_sub(3)).max(1);
        let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
        for (index, row) in picker.rows.iter().enumerate().skip(start).take(visible) {
            let ambiguous = picker.rows.iter().filter(|item| item.id == row.id).count() > 1;
            let label = format!(" {} {} · {}{}", if index == picker.selected { '›' } else { ' ' },
                safe_label(&row.id), if ambiguous { "[ambiguous ID] " } else { "" }, safe_label(&row.preview));
            let label = clipped_title(&label, usize::from(area.width.saturating_sub(2))).0;
            let padded = format!("{label}{}", " ".repeat(usize::from(area.width.saturating_sub(2)).saturating_sub(label.width())));
            let style = if index == picker.selected {
                Style::default().fg(theme::ACCENT).bg(theme::HIGHLIGHT).add_modifier(Modifier::BOLD)
            } else { Style::default().fg(theme::SECONDARY) };
            lines.push(Line::styled(padded, style));
        }
        frame.render_widget(Paragraph::new(lines).block(Block::default()
            .title(" Prompt queue · stable IDs ")
            .borders(Borders::ALL).border_style(Style::default().fg(theme::ACCENT))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED))), area);
    }

    fn draw_lore_picker(&self, frame: &mut Frame, area: Rect) {
        let Some(picker) = &self.lore_picker else { return; };
        if picker.proposal_mode {
            let label_width = usize::from(area.width.saturating_sub(3));
            let mut lines = vec![Line::from(format!(" {}", clipped_title(&picker.status, label_width).0))];
            if let Some(review) = &picker.review {
                lines.push(Line::from(format!(" {}", clipped_title(&format!("{} · inode {}", review.pid(), review.inode()), label_width).0)));
                lines.push(Line::from(format!(" SHA-256 {}", review.sha256())));
                lines.push(Line::from(clipped_title(if picker.can_resolve {
                    " Raw proposal · ↓/PgDn read all · A approve · R reject · Esc back"
                } else { " Raw proposal · read only with this LORE version · Esc back" }, label_width).0));
                let visible = usize::from(area.height.saturating_sub(REVIEW_BODY_RESERVE));
                let width = usize::from(area.width.saturating_sub(3)).max(1);
                // Preserve all raw content across visual rows; terminal controls
                // are shown with visible escapes, and no field is summarized.
                let visual_rows = raw_visual_rows(review.raw(), width);
                for line in visual_rows.iter().skip(picker.review_scroll).take(visible) {
                    lines.push(Line::from(line.clone()));
                }
            } else {
                lines.push(Line::from(" Select one proposal to review its complete raw contents"));
                lines.push(Line::from(format!(" Page offset {} · {} rows", picker.offset, picker.proposals.len())));
                let visible = usize::from(area.height.saturating_sub(6)).max(1);
                let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
                for (index, row) in picker.proposals.iter().enumerate().skip(start).take(visible) {
                    let label = format!(" {} {} · {}/{} · {} · {}", if index == picker.selected { '›' } else { ' ' },
                        safe_label(&row.pid), safe_label(&row.kind), safe_label(&row.action),
                        safe_label(&row.scope), safe_label(&row.summary));
                    lines.push(Line::styled(label, chooser_row_style(index == picker.selected)));
                }
            }
            if picker.review.is_none() {
                lines = chooser_list_lines(lines, usize::from(area.width.saturating_sub(2)));
            }
            frame.render_widget(Paragraph::new(lines)
                .block(Block::default().title(" LORE proposals · Enter full review · PgUp/PgDn page · B beliefs · Esc close ")
                    .borders(Borders::ALL).border_style(Style::default().fg(theme::ACCENT)))
                .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)).wrap(Wrap { trim: false }), area);
            return;
        }
        if let Some(review) = &picker.belief_review {
            let width = usize::from(area.width.saturating_sub(3)).max(1);
            let mut lines = vec![Line::from(format!(" {}", clipped_title(&picker.status, width).0))];
            lines.push(Line::from(format!(" Exact belief #{} · complete LORE review", review.id())));
            lines.push(Line::from(clipped_title(if picker.can_act_on_beliefs {
                " ↓/PgDn read all · C confirmed · X contradicted · S stale · R retract · Esc back"
            } else { " Read only with this LORE version · Esc back" }, width).0));
            if let Some(action) = picker.belief_action {
                let label = match action {
                    doxa_lore::BeliefAction::Confirmed => "confirmed",
                    doxa_lore::BeliefAction::Contradicted => "contradicted",
                    doxa_lore::BeliefAction::Stale => "stale",
                    doxa_lore::BeliefAction::Retract => "retract",
                };
                lines.push(Line::from(format!(" {label} note: {}", safe_label(&picker.belief_note))));
                lines.push(Line::from(if picker.retract_armed { " Press Y to confirm retract · Esc cancel" }
                    else if action == doxa_lore::BeliefAction::Retract { " Enter to review retract confirmation · Esc cancel" }
                    else { " Enter apply · Esc cancel" }));
            } else {
                lines.push(Line::from(" Select an outcome after reading the complete subject and claim"));
                lines.push(Line::from(""));
            }
            let full = format!("Subject: {}\nClaim: {}", review.subject(), review.claim());
            let visual_rows = raw_visual_rows(&full, width);
            let visible = usize::from(area.height.saturating_sub(REVIEW_BODY_RESERVE));
            for line in visual_rows.iter().skip(picker.review_scroll).take(visible) {
                lines.push(Line::from(line.clone()));
            }
            frame.render_widget(Paragraph::new(lines)
                .block(Block::default().title(" LORE belief review ").borders(Borders::ALL)
                    .border_style(Style::default().fg(theme::ACCENT)))
                .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED))
                .wrap(Wrap { trim: false }), area);
            return;
        }
        let height = area.height;
        let modal = area;
        let compact = height < 10;
        let mut lines = vec![Line::from(format!(" Search: {}", safe_label(&picker.query)))];
        if !compact {
            lines.push(Line::from(format!(" {}", picker.status)));
            lines.push(Line::from(" Enter exact review · → evidence · actions require a note"));
            lines.push(Line::from(""));
        }
        if let Some((id, evidence)) = &picker.evidence {
            lines.push(Line::from(format!(" Belief #{id} · {} evidence rows", evidence.len())));
            let trail_notice = evidence.last().is_some_and(|row| row.trail_truncated);
            let reserve = if compact { 4 } else { 9 } + u16::from(trail_notice);
            for row in evidence.iter().take(usize::from(height.saturating_sub(reserve) / 2)) {
                lines.push(Line::from(format!(" {} · {} · {}{}", safe_label(&row.created), safe_label(&row.project), safe_label(&row.session_id),
                    row.source_engine.as_ref().map(|engine| format!(" · {}", safe_label(engine))).unwrap_or_default())));
                lines.push(Line::from(format!("   {}{}", safe_label(&row.note), if row.truncated { "…" } else { "" })));
            }
            if trail_notice {
                lines.push(Line::from(" More evidence exists in LORE"));
            }
        } else {
            lines.push(Line::from(format!(" Page offset {} · {} rows", picker.offset, picker.rows.len())));
            let visible = usize::from(height.saturating_sub(if compact { 4 } else { 8 })).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            for (index, row) in picker.rows.iter().enumerate().skip(start).take(visible) {
                let label = format!(" {} #{} · {} · {:.0}% · {}{}{}", if index == picker.selected { '›' } else { ' ' }, row.id,
                    safe_label(&row.subject), row.confidence * 100.0, safe_label(&row.claim),
                    row.evidence_count.map(|count| format!(" · {count} evidence")).unwrap_or_default(),
                    if row.truncated { " · claim clipped" } else { "" });
                lines.push(Line::styled(label, chooser_row_style(index == picker.selected)));
            }
        }
        if picker.evidence.is_none() {
            lines = chooser_list_lines(lines, usize::from(area.width.saturating_sub(2)));
        }
        frame.render_widget(Paragraph::new(lines)
            .block(Block::default().title(" LORE beliefs · Enter review/search · → evidence · P proposals · PgUp/PgDn page · Esc close ")
            .borders(Borders::ALL).border_style(Style::default().fg(theme::ACCENT)))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)).wrap(Wrap { trim: false }), modal);
    }

    fn draw_diff(&self, frame: &mut Frame, area: Rect) {
        if !self.diff_modal { return; }
        let width = area.width.saturating_sub(4).min(120);
        let height = area.height.saturating_sub(4).min(36);
        if width < 24 || height < 8 { return; }
        let modal = Rect::new(area.x + (area.width - width) / 2, area.y + (area.height - height) / 2, width, height);
        frame.render_widget(Clear, modal);
        let queued_rows = self.queued_diff_rows();
        let mut rows: Vec<Line> = self.diff_text.lines().enumerate()
            .skip(self.diff_scroll)
            .take(usize::from(height.saturating_sub(if self.diff_reject_confirm.is_some() { 3 } else { 2 })))
            .map(|(row, line)| {
            if queued_rows.contains(&row) {
                return Line::styled(format!("⏳ {line}"), Style::default().fg(theme::ACCENT).add_modifier(Modifier::BOLD));
            }
            let color = if line.starts_with('+') && !line.starts_with("+++") { theme::SUCCESS }
                else if line.starts_with('-') && !line.starts_with("---") { theme::ERROR }
                else if line.starts_with("@@") { theme::ACCENT } else { theme::SECONDARY };
            Line::styled(line.to_owned(), Style::default().fg(color))
        }).collect();
        if let Some(draft) = &self.diff_reject_confirm {
            rows.push(Line::styled(format!(" Reason (optional): {}_ · Enter confirm · Esc cancel", draft.reason),
                Style::default().fg(theme::ACCENT)));
        }
        let pending = self.rejections_for_target();
        let title = format!(" Worktree diff{} · N/P files · J/K hunks · X reject · R refresh · F2/Esc close ",
            if pending == 0 { String::new() } else { format!(" · {pending} queued") });
        frame.render_widget(Paragraph::new(rows)
            .block(Block::default().title(title)
                .borders(Borders::ALL).border_style(Style::default().fg(theme::BORDER))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED))), modal);
    }

    fn draw_diff_pane(&self, frame: &mut Frame, area: Rect) {
        let queued_rows = self.queued_diff_rows();
        let mut rows: Vec<Line> = self.diff_text.lines().enumerate()
            .skip(self.diff_scroll)
            .take(usize::from(area.height.saturating_sub(if self.diff_reject_confirm.is_some() { 3 } else { 2 })))
            .map(|(row, line)| {
                if queued_rows.contains(&row) {
                    return Line::styled(format!("⏳ {line}"), Style::default().fg(theme::ACCENT).add_modifier(Modifier::BOLD));
                }
                let color = if line.starts_with('+') && !line.starts_with("+++") { theme::SUCCESS }
                    else if line.starts_with('-') && !line.starts_with("---") { theme::ERROR }
                    else if line.starts_with("@@") { theme::ACCENT } else { theme::SECONDARY };
                Line::styled(line.to_owned(), Style::default().fg(color))
            }).collect();
        if let Some(draft) = &self.diff_reject_confirm {
            rows.push(Line::styled(format!(" Reason (optional): {}_ · Enter confirm · Esc cancel", draft.reason),
                Style::default().fg(theme::ACCENT)));
        }
        let pending = self.rejections_for_target();
        let title = format!(" Worktree diff{} · Alt+N/B files · Alt+J/K hunks · Alt+R reject · F5 refresh · F4 close ",
            if pending == 0 { String::new() } else { format!(" · {pending} queued") });
        frame.render_widget(Paragraph::new(rows)
            .block(Block::default().title(title)
                .borders(Borders::ALL).border_style(Style::default().fg(theme::BORDER)))
            .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)), area);
    }

    fn draw_actions(&self, frame: &mut Frame, area: Rect) {
        if !self.action_menu {
            return;
        }
        let height = area.height;
        let modal = area;
        let visible = usize::from(height.saturating_sub(2));
        let start = chooser_visible_start(&self.chooser_view_start, self.action_selected, visible);
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
        frame.render_widget(
            Paragraph::new(rows).block(
                Block::default()
                    .title(" Actions · ↑/↓ choose · Enter open · Esc close ")
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(theme::ACCENT))
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

    fn draw_request(&self, frame: &mut Frame, area: Rect, inline: bool) {
        let Some(index) = self.active_request_index() else {
            return;
        };
        let request = &self.input_requests[index];
        let width = if inline { area.width } else { area.width.saturating_sub(4).min(90) };
        let height = if inline { area.height } else { area.height.saturating_sub(4).min(22) };
        if width < 20 || height < 5 {
            return;
        }
        let modal = if inline { area } else { Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        ) };
        let (body, selected_row, _) = input_request_body(request,
            usize::from(modal.width.saturating_sub(4)));
        let question = request.questions.get(request.step);
        let lines: Vec<Line> = body.lines().enumerate().map(|(index, text)| {
            if Some(index) == selected_row {
                let padding = usize::from(modal.width.saturating_sub(2)).saturating_sub(text.width());
                Line::styled(format!("{text}{}", " ".repeat(padding)), Style::default().fg(theme::ACCENT)
                    .bg(theme::HIGHLIGHT).add_modifier(Modifier::BOLD))
            } else { Line::from(text.to_owned()) }
        }).collect();
        let title = if request.kind == "ask_user" {
            question.map(|question| format!(" {} ", clipped_title(&question.question,
                usize::from(modal.width.saturating_sub(4))).0))
                .unwrap_or_else(|| " Choose an answer ".into())
        } else { format!(" Input required · {} ", request.kind) };
        if !inline { frame.render_widget(Clear, modal); }
        frame.render_widget(
            Paragraph::new(lines)
                .wrap(Wrap { trim: false })
                .scroll((request.scroll, 0))
                .block(
                    Block::default()
                        .title(title)
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::ACCENT))
                        .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
                ),
            modal,
        );
    }

    fn draw_rail(&self, frame: &mut Frame, area: Rect) {
        let mut lines = Vec::new();
        let mut position = 0;
        for row in self.rail_rows() {
            match row {
                RailRow::Heading(index) => {
                    let item = &self.collections[index];
                    let mark = if item.collapsed { "▸" } else { "▾" };
                    lines.push(Line::styled(format!(" {mark} {}", item.name),
                        Style::default().fg(theme::ACCENT).add_modifier(Modifier::BOLD)));
                }
                RailRow::LooseHeading => lines.push(Line::styled("  Sessions",
                    Style::default().fg(theme::ACCENT).add_modifier(Modifier::BOLD))),
                RailRow::Session(index) => {
                    let session = &self.sessions[index];
                    let mark = if position == self.rail_selected { "▸" } else { " " };
                    let style = if self.waiting_for_input(&session.id) && self.blink_on {
                        Style::default().fg(theme::TEXT).bg(theme::ERROR).add_modifier(Modifier::BOLD)
                    } else { Style::default() };
                    lines.push(Line::styled(format!("{mark} {}", session.title), style));
                    position += 1;
                }
            }
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
        if area.width < 4 || area.height < 6 {
            return;
        }
        let group = &self.groups[index];
        let session = group
            .active_id()
            .and_then(|id| self.sessions.iter().find(|s| s.id == id));
        let active = self.active_group == index;
        let (draft, cursor) = group.active_id().map(|id| {
            if active { (self.input.as_str(), self.input_cursor) }
            else { self.input_drafts.get(&(index, id.to_owned()))
                .map(|(text, cursor)| (text.as_str(), *cursor)).unwrap_or(("", 0)) }
        }).unwrap_or(("", 0));
        let inner = self.pane_regions(index, area);
        let chooser_height = inner[2].height;
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
                let style = if self.waiting_for_input(id) && self.blink_on {
                    Style::default().fg(theme::TEXT).bg(theme::ERROR).add_modifier(Modifier::BOLD)
                } else { Style::default() };
                Line::styled(name.to_owned(), style)
            })
            .collect();
        let tabs = Tabs::new(if titles.is_empty() {
            vec![Line::from("Empty")]
        } else {
            titles
        })
        .select(group.active.min(group.tabs.len().saturating_sub(1)))
        .highlight_style(if group.active_id().is_some_and(|id| self.waiting_for_input(id)) && self.blink_on {
            Style::default().fg(theme::TEXT).bg(theme::ERROR).add_modifier(Modifier::BOLD)
        } else { Style::default().fg(theme::ACCENT).add_modifier(Modifier::BOLD) })
        .block(
            Block::default()
                .title(format!(
                    " Pane {}{} ",
                    index + 1,
                    if self.active_group == index {
                        " ●"
                    } else {
                        ""
                    },
                ))
                .borders(Borders::ALL)
                .border_style(Style::default().fg(if group.active_id().is_some_and(|id| self.waiting_for_input(id)) && self.blink_on {
                    theme::ERROR
                } else { theme::BORDER })),
        );
        frame.render_widget(tabs.style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)), inner[0]);
        let content = session
            .map(|s| s.transcript.as_str())
            .unwrap_or("No session open. Select one in the rail and press Enter.");
        let id = group.active_id().unwrap_or("");
        let cards_revision = self.tool_cards_revision.get(id).copied().unwrap_or(0);
        let activity_line = if self.activity_label(id) == Some("Processing") {
            Some(Line::styled(
                format!(" {} Processing…", SPINNER_FRAMES[self.spinner_frame]),
                Style::default().fg(theme::ACCENT),
            ))
        } else if self.activity_label(id) == Some("Queued") {
            Some(Line::styled(" Queued", Style::default().fg(theme::SECONDARY)))
        } else { None };
        let (lines, sections, top) = {
            let mut cache = self.rendered_transcripts.borrow_mut();
            let position = cache.iter().position(|entry| entry.pane == index && entry.id == id);
            let position = if let Some(position) = position { position } else {
                if let Some(old) = cache.iter().position(|entry| entry.pane == index) { cache.remove(old); }
                if cache.len() == MAX_RENDERED_TRANSCRIPTS { cache.remove(0); }
                cache.push(RenderedTranscript::render(index, id, content,
                    inner[1].width.saturating_sub(2), self.expanded_tool_sections.get(id),
                    (active && self.focus == Focus::Transcript)
                        .then(|| self.selected_tool_sections.get(id).copied()).flatten(),
                    cards_revision, self.tool_cards.for_session(id)));
                cache.len() - 1
            };
            cache[position].update(content, inner[1].width.saturating_sub(2),
                self.expanded_tool_sections.get(id),
                (active && self.focus == Focus::Transcript)
                    .then(|| self.selected_tool_sections.get(id).copied()).flatten(),
                cards_revision, self.tool_cards.for_session(id));
            let (window, top) = transcript_window(&cache[position].lines,
                inner[1].height, group.scroll, activity_line);
            (window, cache[position].sections.clone(), top)
        };
        for section in sections {
            if section.line >= top && section.line < top + usize::from(inner[1].height) {
                self.visible_tool_sections.borrow_mut().push((
                    Rect::new(inner[1].x.saturating_add(1),
                        inner[1].y.saturating_add((section.line - top) as u16),
                        inner[1].width.saturating_sub(2), 1),
                    index, id.to_owned(), section.index,
                ));
            }
        }
        let content_width = usize::from(inner[1].width.saturating_sub(2));
        let mut visible_links = self.visible_links.borrow_mut();
        for hit in links::hits(&lines, content_width) {
            if hit.end > hit.start {
                visible_links.push((
                    Rect::new(inner[1].x.saturating_add(1).saturating_add(hit.start as u16),
                        inner[1].y.saturating_add(hit.row as u16),
                        (hit.end - hit.start) as u16, 1),
                    hit.url,
                ));
            }
        }
        frame.render_widget(
            Paragraph::new(lines)
                .style(Style::default().fg(theme::TEXT).bg(theme::BASE))
                .wrap(Wrap { trim: false })
                .block(Block::default().borders(Borders::LEFT | Borders::RIGHT)
                    .border_style(Style::default().fg(if group.active_id().is_some_and(|id| self.waiting_for_input(id)) && self.blink_on {
                        theme::ERROR
                    } else { theme::BORDER }))),
            inner[1],
        );
        if active && chooser_height > 0 {
            if self.active_request_index().is_some_and(|index| self.input_requests[index].kind == "ask_user") {
                self.draw_request(frame, inner[2], true);
            } else if self.settings_menu.is_some() {
                self.draw_settings_menu(frame, inner[2]);
            } else if self.engine_picker || self.new_session.is_some() || self.model_picker.is_some() || self.effort_picker.is_some() || self.permission_picker.is_some() {
                self.draw_chip_picker(frame, inner[2]);
            } else if self.repo_picker.is_some() {
                self.draw_repo_picker(frame, inner[2]);
            } else if self.lore_picker.is_some() {
                self.draw_lore_picker(frame, inner[2]);
            } else if self.action_menu {
                self.draw_actions(frame, inner[2]);
            } else if self.chip_info.is_some() {
                self.draw_chip_info(frame, inner[2]);
            } else if self.history_modal {
                self.draw_history(frame, inner[2]);
            } else if self.queue_picker.is_some() {
                self.draw_queue_picker(frame, inner[2]);
            } else if self.attach_picker.is_some() {
                self.draw_attach_picker(frame, inner[2]);
            } else if self.branch_picker.is_some() {
                self.draw_branch_picker(frame, inner[2]);
            } else if !self.slash_suggestions().is_empty() {
                self.draw_slash_suggestions(frame, inner[2]);
            }
        }
        let mut chip_spans = Vec::new();
        let mut chip_x = inner[3].x;
        for (kind, label) in self.chip_window(index, usize::from(inner[3].width)) {
            if !chip_spans.is_empty() { chip_spans.push(Span::raw(" ")); }
            let text = chip_text(kind, &label);
            let end = chip_x.saturating_add(text.width() as u16).min(inner[3].right());
            if end > chip_x {
                if let Some(hits) = self.rendered_chip_hits.borrow_mut().as_mut() {
                    hits.push(ChipHit { group: index, kind,
                        rect: Rect::new(chip_x, inner[3].y, end - chip_x, 1), pane: area });
                }
            }
            chip_x = end.saturating_add(1);
            chip_spans.push(Span::styled(text,
                Style::default().fg(if matches!(kind, "engine" | "more") { theme::ACCENT } else { theme::TEXT })
                    .bg(theme::HIGHLIGHT)));
        }
        frame.render_widget(Paragraph::new(Line::from(chip_spans))
            .style(Style::default().bg(theme::RAISED)), inner[3]);
        let cursor = cursor.min(draft.len());
        let cursor_line = draft[..cursor].bytes().filter(|b| *b == b'\n').count();
        let cursor_column = UnicodeWidthStr::width(draft[..cursor].rsplit('\n').next().unwrap_or("")) + 2;
        let mut rows: Vec<String> = draft.split('\n').enumerate().map(|(line, text)| {
            format!("{}{}", if line == 0 { "> " } else { "  " }, text)
        }).collect();
        if active && self.focus == Focus::Prompt {
            let offset = draft[..cursor].rsplit('\n').next().unwrap_or("").len() + 2;
            rows[cursor_line].insert(offset, '▏');
        }
        let visible = usize::from(inner[4].height.saturating_sub(2)).max(1);
        let scroll_y = cursor_line.saturating_sub(visible.saturating_sub(1));
        let width = usize::from(inner[4].width.saturating_sub(2)).max(1);
        let scroll_x = cursor_column.saturating_sub(width.saturating_sub(1));
        frame.render_widget(
            Paragraph::new(rows.join("\n"))
                .scroll((scroll_y as u16, scroll_x as u16))
                .style(Style::default().fg(theme::TEXT).bg(theme::RAISED))
                .block(Block::default()
                    .title(if active && self.focus == Focus::Prompt { " Prompt ● " } else { " Prompt " })
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(if active && self.focus == Focus::Prompt { theme::ACCENT } else { theme::BORDER }))),
            inner[4],
        );
        let status = session.map(|s| s.status.as_str()).unwrap_or("No session");
        let status_line = if active && !self.notice.is_empty() {
            format!(" {} · {status}", self.notice)
        } else { format!(" {status}") };
        frame.render_widget(
            Paragraph::new(status_line).style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            Rect { height: 1, ..inner[5] },
        );
    }
}

fn transcript_window(
    lines: &[Line<'static>],
    viewport: u16,
    scroll: usize,
    extra: Option<Line<'static>>,
) -> (Vec<Line<'static>>, usize) {
    // Markdown has already wrapped lines to the pane's content width. Give
    // Paragraph only visible rows: handing it the whole transcript makes
    // every spinner frame rewrap thousands of off-screen lines.
    let viewport = usize::from(viewport);
    let total = lines.len() + usize::from(extra.is_some());
    let max_scroll = total.saturating_sub(viewport);
    let top = max_scroll.saturating_sub(scroll.min(max_scroll));
    let end = (top + viewport).min(total);
    let mut window = lines[top.min(lines.len())..end.min(lines.len())].to_vec();
    if end > lines.len() {
        if let Some(extra) = extra { window.push(extra); }
    }
    (window, top)
}

/// Owns terminal modes so every return path, including I/O errors, restores the screen.
struct TerminalGuard {
    out: Stdout,
    raw: bool,
    alternate: bool,
    mouse: bool,
    paste: bool,
}
impl TerminalGuard {
    fn enter() -> io::Result<Self> {
        let mut guard = Self {
            out: io::stdout(),
            raw: false,
            alternate: false,
            mouse: false,
            paste: false,
        };
        terminal::enable_raw_mode()?;
        guard.raw = true;
        execute!(guard.out, EnterAlternateScreen)?;
        guard.alternate = true;
        execute!(guard.out, EnableMouseCapture)?;
        guard.mouse = true;
        execute!(guard.out, EnableBracketedPaste)?;
        guard.paste = true;
        Ok(guard)
    }
}
impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = self.out.write_all(pointer_shape(false));
        let _ = self.out.flush();
        if self.paste {
            let _ = execute!(self.out, DisableBracketedPaste);
        }
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

/// OSC 22 is a no-op in terminals without pointer-shape support.
fn pointer_shape(link: bool) -> &'static [u8] {
    if link { b"\x1b]22;pointer\x1b\\" } else { b"\x1b]22;\x1b\\" }
}

fn open_link(url: &str) -> io::Result<()> {
    if !links::safe_url(url) {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "unsupported link"));
    }
    #[cfg(target_os = "macos")]
    let opener = "open";
    #[cfg(not(target_os = "macos"))]
    let opener = "xdg-open";
    let mut child = std::process::Command::new(opener).arg(url)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()?;
    std::thread::spawn(move || { let _ = child.wait(); });
    Ok(())
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
    let mut pointer_on_link = false;
    while !app.should_quit {
        let mut changed = false;
        // Bound work per tick so a busy daemon cannot starve keyboard input.
        for _ in 0..64 {
            match receiver.try_recv() {
                Ok(frame) => changed |= app.apply_daemon_frame(&frame),
                Err(TryRecvError::Empty | TryRecvError::Disconnected) => break,
            }
        }
        // A frame is ready now. Paint before waiting for terminal input so a
        // daemon update never waits through an otherwise idle input poll.
        if changed {
            terminal.draw(|frame| app.draw(frame))?;
            changed = false;
        }
        app.clear_preflight_error = state.as_ref()
            .map_or(Some("persistent tabset unavailable"), |(store, _, complete)|
                store.clear_preflight(&app, complete).err());
        if event::poll(Duration::from_millis(10))? {
            changed |= app.handle(event::read()?);
        }
        let next_pointer = app.link_hover.is_some() && !app.link_interaction_blocked();
        if next_pointer != pointer_on_link {
            let mut out = io::stdout();
            out.write_all(pointer_shape(next_pointer))?;
            out.flush()?;
            pointer_on_link = next_pointer;
        }
        for url in std::mem::take(&mut app.pending_open_urls) {
            if open_link(&url).is_err() {
                app.notice = "Could not open link in browser".into();
                changed = true;
            }
        }
        changed |= app.poll_diff();
        changed |= app.poll_history();
        changed |= app.poll_resume();
        changed |= app.poll_lore();
        changed |= app.poll_memory();
        changed |= app.poll_repo();
        changed |= app.poll_memory_menu();
        changed |= app.poll_vendor_catalog();
        changed |= app.tick_blink(Instant::now());
        changed |= app.tick_spinner(Instant::now());
        if prompt_sender.is_none() {
            if let Some(id) = app.pending_peer_refresh.take() {
                changed |= app.peer_map.roster(&id, &serde_json::json!({"ok":false}));
            }
            if !app.pending_launches.is_empty() {
                app.pending_launches.clear();
                app.launching = false;
                app.clear_pending = None;
                app.notice = "Session launch unavailable · daemon connection closed".into();
                changed = true;
            }
            if !app.pending_attaches.is_empty() {
                app.pending_attaches.clear();
                app.attaching_ids.clear();
                app.notice = "Session attach unavailable · daemon connection closed".into();
                changed = true;
            }
            if !app.pending_stops.is_empty() {
                app.pending_stops.clear();
                app.notice = "Session stop unavailable · daemon connection closed".into();
                changed = true;
            }
            if !app.clear_stop_after_save.is_empty() {
                app.clear_stop_after_save.clear();
                app.notice = "Previous session could not be finalized · daemon connection closed".into();
                changed = true;
            }
            if !app.pending_clear_finalizes.is_empty() {
                app.pending_clear_finalizes.clear();
                app.notice = "Previous session could not be finalized · daemon connection closed".into();
                changed = true;
            }
            if !app.pending_queue_commands.is_empty() {
                app.pending_queue_commands.clear();
                app.queue_picker = None;
                app.notice = "Queue unavailable · daemon connection closed".into();
                changed = true;
            }
            if !app.pending_peer_messages.is_empty() {
                for (id, target, text) in app.pending_peer_messages.drain(..) {
                    app.rejected_drafts.entry(id).or_default().push(format!("/msg {target} {text}"));
                }
                app.notice = "Peer delivery unavailable · Alt+Up restores message".into();
                changed = true;
            }
        }
        if let Some(sender) = &prompt_sender {
            let disconnected = dispatch_launches(&mut app, sender);
            let disconnected = dispatch_attaches(&mut app, sender) || disconnected;
            let disconnected = dispatch_prompts(&mut app, sender) || disconnected;
            let disconnected = dispatch_answers(&mut app, sender) || disconnected;
            let disconnected = dispatch_peer_refresh(&mut app, sender) || disconnected;
            let disconnected = dispatch_peer_messages(&mut app, sender) || disconnected;
            let disconnected = dispatch_model_controls(&mut app, sender) || disconnected;
            let disconnected = dispatch_queue_commands(&mut app, sender) || disconnected;
            if disconnected {
                prompt_sender = None;
                app.session_activity.clear();
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
        changed |= app.finish_clear_swap(state.is_some()
            && saved_layout == crate::ui_state::LayoutSignature::capture(&app));
        if let Some(sender) = &prompt_sender {
            if dispatch_stops(&mut app, sender) || dispatch_clear_finalizes(&mut app, sender) {
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
                app.attaching_ids.clear();
                app.launching = false;
                app.clear_pending = None;
                app.notice = "Session launch unavailable".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

fn dispatch_attaches(app: &mut App, sender: &SyncSender<crate::bridge::WorkerCommand>) -> bool {
    let mut attaches = std::mem::take(&mut app.pending_attaches).into_iter();
    while let Some((id, group)) = attaches.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Attach(id, group)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Attach(id, group))) => {
                app.pending_attaches.extend(std::iter::once((id, group)).chain(attaches));
                return false;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.attaching_ids.clear();
                app.notice = "Session attach unavailable".into();
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

fn dispatch_clear_finalizes(app: &mut App, sender: &SyncSender<crate::bridge::WorkerCommand>) -> bool {
    let mut pending = std::mem::take(&mut app.pending_clear_finalizes).into_iter();
    while let Some(id) = pending.next() {
        match sender.try_send(crate::bridge::WorkerCommand::FinalizeForClear(id)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::FinalizeForClear(id))) => {
                app.pending_clear_finalizes.extend(std::iter::once(id).chain(pending));
                return false;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.notice = "Previous session could not be finalized · daemon connection closed".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

fn dispatch_queue_commands(app: &mut App, sender: &SyncSender<crate::bridge::WorkerCommand>) -> bool {
    let mut commands = std::mem::take(&mut app.pending_queue_commands).into_iter();
    while let Some(command) = commands.next() {
        match sender.try_send(command) {
            Ok(()) => {}
            Err(TrySendError::Full(command)) => {
                app.pending_queue_commands.extend(std::iter::once(command).chain(commands));
                return false;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.queue_picker = None;
                app.notice = "Queue unavailable · daemon connection closed".into();
                return true;
            }
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
        if !app.has_offline_open_tabs() && app.notice == "Layout save skipped · archived tabs are read-only" {
            app.notice.clear();
            return true;
        }
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
                if let Some(picker) = app.model_picker.as_mut() {
                    picker.loading = false;
                    picker.catalog_pending = false;
                    picker.note = "Daemon unavailable for model catalog · R retry".into();
                }
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
    let mut efforts = std::mem::take(&mut app.pending_effort_changes).into_iter();
    while let Some((id, effort)) = efforts.next() {
        match sender.try_send(crate::bridge::WorkerCommand::SetEffort(id, effort)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::SetEffort(id, effort))) => {
                app.pending_effort_changes.push((id, effort));
                app.pending_effort_changes.extend(efforts);
                break;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.notice = "Daemon unavailable for effort change".into();
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

fn dispatch_peer_messages(app: &mut App, sender: &SyncSender<crate::bridge::WorkerCommand>) -> bool {
    let mut messages = std::mem::take(&mut app.pending_peer_messages).into_iter();
    while let Some((id, target, body)) = messages.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Message(id, target, body)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Message(id, target, body))) => {
                app.pending_peer_messages.extend(std::iter::once((id, target, body)).chain(messages));
                return false;
            }
            Err(TrySendError::Disconnected(crate::bridge::WorkerCommand::Message(id, target, body))) => {
                app.rejected_drafts.entry(id).or_default().push(format!("/msg {target} {body}"));
                for (id, target, body) in messages {
                    app.rejected_drafts.entry(id).or_default().push(format!("/msg {target} {body}"));
                }
                app.notice = "Peer delivery unavailable · Alt+Up restores message".into();
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
    use ratatui::backend::TestBackend;
    use serde_json::json;

    #[test]
    fn settings_menu_protects_env_shadowed_rows_and_keeps_prompt() {
        let mut app = App::default();
        app.input = "draft prompt".into();
        app.settings_menu = Some(SettingsMenu {
            rows: [("90 (environment)".into(), true), ("on (default)".into(), false)],
            selected: 0,
            linger_draft: None,
        });
        app.settings_menu_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.settings_menu.as_ref().unwrap().linger_draft.is_none());
        assert!(app.notice.contains("Environment override"));
        app.settings_menu_key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::NONE));
        assert!(app.settings_menu.is_some());
        assert_eq!(app.input, "draft prompt");
        app.settings_menu_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.settings_menu.is_none());
    }

    #[test]
    fn settings_linger_editor_cancels_without_writing() {
        let mut app = App::default();
        app.settings_menu = Some(SettingsMenu {
            rows: [("120 (default)".into(), false), ("on (default)".into(), false)],
            selected: 0,
            linger_draft: None,
        });
        app.settings_menu_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.settings_menu.as_ref().unwrap().linger_draft.as_deref(), Some("120"));
        app.settings_menu_key(KeyEvent::new(KeyCode::Char('9'), KeyModifiers::NONE));
        app.settings_menu_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.settings_menu.as_ref().unwrap().linger_draft.is_none());
        assert_eq!(app.settings_menu.as_ref().unwrap().rows[0].0, "120 (default)");
    }

    #[cfg(unix)]
    fn belief_review_fixture() -> doxa_lore::BeliefReview {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let sidecar = dir.path().join("sidecar");
        std::fs::write(&sidecar, r##"#!/usr/bin/env python3
import hashlib, json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','belief_review_v1','belief_action_v1']}), flush=True)
for line in sys.stdin:
    req=json.loads(line)
    assert req['op']=='belief_review_v1' and req['belief_id']==7
    claim='A complete claim ' + 'x'*1200
    value={'id':7,'uid':'fixture-7','subject':'Fixture subject','claim':claim,'claim_sha256':hashlib.sha256(claim.encode()).hexdigest()}
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"##).unwrap();
        let mut permissions = std::fs::metadata(&sidecar).unwrap().permissions();
        permissions.set_mode(0o700);
        std::fs::set_permissions(&sidecar, permissions).unwrap();
        let lore_picker::ResultPage::BeliefReview(review, true) = lore_picker::fetch(&sidecar,
            lore_picker::Query::BeliefReview("/repo".into(), 7)).unwrap() else { panic!("review") };
        review
    }

    #[cfg(unix)]
    fn app_with_review() -> App {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 40);
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","cwd":"/repo"}));
        app.open_lore_picker();
        let picker = app.lore_picker.as_mut().unwrap();
        picker.pending = None;
        picker.rows = vec![lore_picker::Belief { id: 7, subject: "Fixture subject".into(),
            claim: "clipped list text".into(), truncated: true, confidence: 0.8, evidence_count: Some(1) }];
        let (tx, rx) = mpsc::sync_channel(1);
        picker.pending = Some(rx);
        tx.send(Ok(lore_picker::ResultPage::BeliefReview(belief_review_fixture(), true))).unwrap();
        assert!(app.poll_lore());
        app
    }

    #[test]
    fn ctrl_c_does_not_detach_the_terminal() {
        let mut app = App::default();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)));
        assert!(!app.should_quit);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::CONTROL)));
        assert!(app.should_quit);
    }

    #[test]
    fn peer_commands_use_active_session_without_becoming_model_prompts() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"session-1"}));
        app.input = "/msg peer-12 hello from this pane".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_peer_messages, vec![("session-1".into(), "peer-12".into(), "hello from this pane".into())]);
        assert!(app.pending_prompts.is_empty());
        assert!(app.input.is_empty());

        app.input = "/peers".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.map_modal);
        assert_eq!(app.pending_peer_refresh.as_deref(), Some("session-1"));
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn peer_message_usage_and_uncertain_delivery_are_explicit() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"session-1"}));
        app.input = "/msg peer-12".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.notice.starts_with("Usage:"));
        assert_eq!(app.input, "/msg peer-12");
        assert!(app.pending_peer_messages.is_empty());
        assert!(app.apply_daemon_frame(&json!({"type":"peer_message_reply", "session_id":"session-1",
            "ok":false, "uncertain":true, "draft":"/msg peer-12 important text"})));
        assert!(app.notice.contains("unconfirmed"));
        assert!(app.notice.contains("before Alt+Up retry"));
        assert_eq!(app.rejected_drafts["session-1"], ["/msg peer-12 important text"]);
        assert!(app.apply_daemon_frame(&json!({"type":"peer_message_reply", "session_id":"session-1",
            "ok":true, "peer":{"title":"Builder"}, "delivered_to":["peer-12"],
            "ledger_error":"disk full"})));
        assert!(app.notice.contains("ledger write failed"));
        assert!(app.apply_daemon_frame(&json!({"type":"peer_message_reply", "session_id":"session-1",
            "ok":true, "peer":{"title":"Python peer"}, "delivered_to":null})));
        assert!(app.notice.contains("sent to Python peer"));
    }

    #[test]
    fn background_peer_failure_keeps_recoverable_draft_in_its_session() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"first"}));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"second"}));
        app.groups[0].tabs = vec!["first".into(), "second".into()];
        app.groups[0].active = 0;
        assert!(app.apply_daemon_frame(&json!({"type":"peer_message_reply", "session_id":"second",
            "ok":false, "error":"peer left", "draft":"/msg peer-2 hello"})));
        assert_eq!(app.rejected_drafts["second"], ["/msg peer-2 hello"]);
        assert!(app.notice.contains("second"));
        assert!(app.notice.contains("peer left"));
    }

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
    fn shrinking_terminal_cancels_hidden_stop_confirmation() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"first"}));
        app.open_stop_confirmation();
        assert_eq!(app.stop_confirmation.as_deref(), Some("first"));
        app.handle(Event::Resize(39, 11));
        assert!(app.stop_confirmation.is_none());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE)));
        assert!(app.pending_stops.is_empty());
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
    fn chip_strip_keeps_permission_classifier_vendor_model_and_context_distinct() {
        let mut app = App::default();
        app.handle(Event::Resize(220, 30));
        app.rail_visible = false;
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"claude-1",
            "engine":"claude", "model":"sonnet", "permission_mode":"auto",
            "can_set_permission_mode":true, "can_set_model":true}));
        app.groups[0].tabs = vec!["claude-1".into()];

        let chips = app.chips(0);
        assert_eq!(chips[0], ("permission", "Permissions auto".into()));
        assert_eq!(chips[1], ("engine", "claude".into()));
        assert_eq!(chips[2], ("model", "sonnet".into()));
        assert_eq!(chips[3], ("effort", "?".into()));
        assert_eq!(chips[4].0, "context");
        assert!(chips[4].1.starts_with("Ctx "));

        let pane = app.layout(app.size).body;
        let chip_y = pane.bottom().saturating_sub(prompt_height("", pane.height) + 2);
        let click = |app: &mut App, x: u16| {
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
                column: x, row: chip_y, modifiers: KeyModifiers::NONE }));
        };
        click(&mut app, pane.x + 2);
        assert_eq!(app.permission_picker.as_ref().unwrap().1, permission_index("auto").unwrap());
        app.permission_picker = None;

        let vendor_x = pane.x + chip_text(chips[0].0, &chips[0].1).width() as u16 + 3;
        click(&mut app, vendor_x);
        assert!(app.engine_picker);
        assert_eq!(app.engine_selected, 1);
        assert!(app.new_session.is_none());
        assert_eq!(app.permission_modes["claude-1"], "auto");
        app.engine_picker = false;

        let model_x = vendor_x + chip_text(chips[1].0, &chips[1].1).width() as u16 + 1;
        click(&mut app, model_x);
        assert_eq!(app.model_picker.as_ref().unwrap().session_id, "claude-1");
        assert_eq!(app.pending_model_queries, vec!["claude-1"]);
    }

    #[test]
    fn memory_chip_uses_lore_counts_and_ignores_old_project_reply() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "cwd":"/repo/old"}));
        app.set_lore_memory_usage("s", 401, 1000, 80, 200);
        assert_eq!(app.chips(0).iter().find(|(kind, _)| *kind == "memory").unwrap().1,
            "p 40%/u 40%");
        assert_eq!(memory_fill_percent(0, 1000), 0);
        assert_eq!(memory_fill_percent(4, 1000), 0);
        assert_eq!(memory_fill_percent(5, 1000), 1);
        let (tx, rx) = mpsc::sync_channel(1);
        app.memory_pending = Some(("s".into(), "/repo/old".into(), rx));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "cwd":"/repo/new"}));
        app.offline_ids.insert("s".into());
        tx.send(Some((doxa_lore::MemoryUsage {
            project_chars: 400, project_cap_chars: 1000,
            user_chars: 200, user_cap_chars: 500,
        }, true))).unwrap();
        app.poll_memory();
        assert!(!app.memory_cache.contains_key("s"));
        assert_eq!(app.chips(0).iter().find(|(kind, _)| *kind == "memory").unwrap().1,
            "u ? · scope ?");
    }

    #[test]
    fn memory_chip_opens_inline_entries_for_active_pane_and_rejects_stale_scope() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(160, 32));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"left","cwd":"/tmp/left"}));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"right","cwd":"/tmp/right"}));
        app.groups[1].tabs.push("right".into());
        app.active_group = 1;
        app.open_memory_menu(1);
        assert_eq!(app.chip_info.as_ref().unwrap().kind, "memory");
        let menu = app.active_chooser_rect().unwrap();
        let pane = app.layout(app.size).panes.unwrap()[1];
        assert_eq!(menu.x, pane.x);
        assert!(menu.bottom() < pane.bottom());
        let (tx, rx) = mpsc::sync_channel(1);
        app.memory_menu_pending = Some(("right".into(), "/tmp/right".into(), rx));
        tx.send(Ok(vec!["## User memory".into(), "- verified user fact".into(),
            "## Folder memory".into(), "- verified folder fact".into()])).unwrap();
        assert!(app.poll_memory_menu());
        let rendered = painted_at(&app, 160, 32);
        assert!(rendered.contains("verified user fact"), "{rendered}");
        assert!(rendered.contains("verified folder fact"), "{rendered}");
        let (tx, rx) = mpsc::sync_channel(1);
        app.memory_menu_pending = Some(("right".into(), "/tmp/right".into(), rx));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"right","cwd":"/tmp/moved"}));
        tx.send(Ok(vec!["- stale secret".into()])).unwrap();
        assert!(!app.poll_memory_menu());
        assert!(!painted_at(&app, 160, 32).contains("stale secret"));
        let rendered = painted_at(&app, 160, 32);
        assert!(rendered.contains("Session changed; reopen memory"));
        assert!(!rendered.contains("verified folder fact"));
    }

    #[test]
    fn memory_gallery_fixture_renders_curated_and_global_sections_without_lore_worker() {
        let mut app = App::default();
        app.handle(Event::Resize(120, 32));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"gallery","cwd":"/demo/project"}));
        app.show_memory_menu_fixture(0, &["- User entry"], &["- Project entry"], &["- Global belief"]);
        assert!(app.memory_menu_pending.is_none());
        let rendered = painted_at(&app, 120, 32);
        assert!(rendered.contains("User entry"), "{rendered}");
        assert!(rendered.contains("Project entry"), "{rendered}");
        assert!(rendered.contains("Global active LORE beliefs"), "{rendered}");
        assert!(rendered.contains("Global belief"), "{rendered}");
    }

    #[test]
    fn single_pane_keeps_four_primary_chips_visible_without_prefixes() {
        let mut app = App::default();
        app.handle(Event::Resize(148, 31));
        app.split = Split::Horizontal;
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"claude-1",
            "engine":"claude", "model":"claude-sonnet-4", "permission_mode":"default",
            "can_set_permission_mode":true, "can_set_model":true}));
        app.groups[0].tabs = vec!["claude-1".into()];
        let layout = app.layout(app.size);
        let pane = layout.panes.map_or(layout.body, |panes| panes[0]);
        let visible = app.chip_window(0, usize::from(pane.width));
        assert_eq!(visible.iter().take(4).map(|(kind, _)| *kind).collect::<Vec<_>>(),
            vec!["permission", "engine", "model", "effort"]);
        assert_eq!(visible[0].1, "Permissions default");
        assert_eq!(visible[1].1, "claude");
        assert_eq!(visible[2].1, "claude-sonnet-4");
        assert_eq!(visible[3].1, "?");
        let occupied = visible.iter().map(|(kind, label)| chip_text(kind, label).width()).sum::<usize>()
            + visible.len().saturating_sub(1);
        assert!(occupied <= usize::from(pane.width));
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
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 6, modifiers: KeyModifiers::NONE }));
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
        assert_eq!(app.new_session.as_ref().unwrap().model, "deepseek-flash");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.new_session.as_ref().unwrap().effort.as_deref(), Some("high"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        for c in "Explain this".chars() {
            app.handle(Event::Key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)));
        }
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        let (options, prompt, group) = app.pending_launches.pop().unwrap();
        assert_eq!(options.engine, launch::Engine::DeepSeek);
        assert_eq!(options.model.as_deref(), Some("deepseek-flash"));
        assert_eq!(options.effort.as_deref(), Some("high"));
        assert_eq!(prompt.as_deref(), Some("Explain this"));
        assert_eq!(group, 0);
        assert!(app.launching);
        assert!(app.new_session.is_none());
    }

    #[test]
    fn cd_opens_new_tab_at_verified_directory_without_moving_existing_session() {
        let root = tempfile::tempdir().unwrap();
        let child = root.path().join("child");
        std::fs::create_dir(&child).unwrap();
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"existing",
            "engine":"claude", "model":"sonnet", "cwd":root.path()}));
        app.input = "/cd child".into();
        assert!(app.submit_local_command());
        let (options, prompt, group) = app.pending_launches.pop().unwrap();
        assert_eq!(options.engine, launch::Engine::Claude);
        assert_eq!(options.cwd.as_deref(), Some(child.as_path()));
        assert!(prompt.is_none());
        assert_eq!(group, 0);
        assert_eq!(app.groups[0].active_id(), Some("existing"));
        assert_eq!(app.session_cwds["existing"], root.path());
        assert!(app.input.is_empty());
    }

    #[test]
    fn cd_refuses_invalid_directory_and_keeps_draft() {
        let root = tempfile::tempdir().unwrap();
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"existing",
            "engine":"codex", "cwd":root.path()}));
        app.input = "/cd missing".into();
        assert!(app.submit_local_command());
        assert!(app.pending_launches.is_empty());
        assert_eq!(app.input, "/cd missing");
        assert!(app.notice.contains("does not exist"));
    }

    #[test]
    fn effort_chip_picker_requests_current_session_change_and_waits_for_event() {
        let mut app = App::default();
        app.handle(Event::Resize(220, 32));
        app.rail_visible = false;
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"deep-1",
            "engine":"deepseek","model":"deepseek-flash","effort":"high"}));
        app.groups[0].tabs = vec!["deep-1".into()];
        let effort_index = app.chips(0).iter().position(|(kind, _)| *kind == "effort").unwrap();
        assert_eq!(app.chips(0)[effort_index], ("effort", "high".into()));
        assert_eq!(chip_text("effort", "high"), " high ▾ ");
        assert_eq!(chip_text("effort", "?"), " ? ");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('f'), KeyModifiers::ALT)));
        assert_eq!(app.effort_picker.as_ref().unwrap().levels, ["none", "low", "high", "max"]);
        assert_eq!(app.effort_picker.as_ref().unwrap().selected, 2);
        assert!(painted(&app).contains("this session"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_effort_changes, vec![("deep-1".into(), "max".into())]);
        assert_eq!(app.session_efforts["deep-1"], "high");
        app.apply_daemon_frame(&json!({"type":"set_effort_reply","session_id":"deep-1","ok":true,"effort":"max"}));
        assert_eq!(app.session_efforts["deep-1"], "high");
        app.apply_daemon_frame(&json!({"type":"event","session_id":"deep-1",
            "event":{"type":"effort_changed","data":{"effort":"max"}}}));
        assert_eq!(app.session_efforts["deep-1"], "max");

        // The closed picker moves the chip strip back up before the next
        // pointer event; use the freshly painted hit area.
        let _ = painted_at(&app, 220, 32);
        let effort_hit = app.rendered_chip_hits.borrow().as_ref().unwrap().iter()
            .find(|hit| hit.kind == "effort").unwrap().clone();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: effort_hit.rect.x + 1, row: effort_hit.rect.y, modifiers: KeyModifiers::NONE }));
        assert!(painted_at(&app, 220, 32).contains("Effort · current session"));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: effort_hit.rect.x + 1, row: effort_hit.rect.y, modifiers: KeyModifiers::NONE }));
        assert!(app.effort_picker.is_some());
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 3, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.pending_effort_changes.last(), Some(&("deep-1".into(), "none".into())));
        assert!(app.effort_picker.is_none());
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"glm-2",
            "engine":"glm","model":"glm-5.3-flash","effort":"low"}));
        app.groups[0].tabs.push("glm-2".into());
        app.groups[0].active = 1;
        assert_eq!(app.chips(0).iter().find(|(kind, _)| *kind == "effort").unwrap().1, "low");
        app.open_effort_picker();
        assert_eq!(app.effort_picker.as_ref().unwrap().levels, ["low", "high", "max"]);
        assert!(!app.effort_picker.as_ref().unwrap().levels.contains(&"none".to_owned()));
        assert_eq!(app.pending_effort_changes.last(), Some(&("deep-1".into(), "none".into())));
        app.apply_daemon_frame(&json!({"type":"event","session_id":"glm-2",
            "event":{"type":"model_changed","data":{"model":"unknown-new-model"}}}));
        assert!(app.effort_picker.is_none());
        app.open_effort_picker();
        assert!(app.effort_picker.is_none());
    }

    #[test]
    fn effort_capability_and_vendor_model_choices_fail_closed() {
        assert_eq!(effort_choices("glm", "glm-5.3-flash"), ["low", "high", "max"]);
        assert!(effort_choices("glm", "glm-unverified").is_empty());
        assert!(effort_choices("deepseek", "glm-5.3-flash").is_empty());
        assert!(effort_choices("codex", "gpt-6").is_empty());
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"unknown",
            "engine":"codex","model":"gpt-6","effort":"high"}));
        app.groups[0].tabs = vec!["unknown".into()];
        app.open_effort_picker();
        assert!(app.effort_picker.is_none());
        assert!(app.notice.contains("Live effort change is unavailable"));

        app.engine_selected = 2;
        app.select_new_engine();
        assert_eq!(app.new_session.as_ref().unwrap().model, "deepseek-flash");
        assert_eq!(app.new_session.as_ref().unwrap().effort.as_deref(), Some("high"));
        app.new_session.as_mut().unwrap().field = 0;
        app.new_session_key(KeyEvent::new(KeyCode::Right, KeyModifiers::NONE));
        assert_eq!(app.new_session.as_ref().unwrap().model, "deepseek-v4-pro");
        app.engine_selected = 3;
        app.select_new_engine();
        assert_eq!(app.new_session.as_ref().unwrap().model, "glm-5.3-flash");
        assert_eq!(app.new_session.as_ref().unwrap().effort.as_deref(), Some("high"));
        let form = app.new_session.as_ref().unwrap();
        assert!(!vendor_models(form.engine).contains(&"deepseek-v4-pro"));
        assert!(!effort_choices(engine_name(form.engine), &form.model).contains(&"none"));
    }

    #[test]
    fn live_vendor_catalog_refresh_filters_models_and_switch_discards_stale_rows() {
        fn row(id: &str, efforts: &[&str], default: Option<&str>) -> doxa_vendors::ModelCapability {
            doxa_vendors::ModelCapability { id: id.into(), efforts: efforts.iter().map(|x| (*x).into()).collect(),
                default_effort: default.map(str::to_owned), effort_metadata_present: !efforts.is_empty() }
        }
        let mut app = App::default();
        app.engine_selected = 2;
        app.select_new_engine();
        let (tx, rx) = mpsc::sync_channel(1);
        app.vendor_catalog_pending = Some((launch::Engine::DeepSeek, rx));
        app.new_session.as_mut().unwrap().catalog_pending = true;
        tx.send(Some(vec![row("deepseek-v4-pro", &["low", "high", "max"], Some("high")),
            row("deepseek-next", &["low", "max"], Some("max"))])).unwrap();
        assert!(app.poll_vendor_catalog());
        let form = app.new_session.as_ref().unwrap();
        assert_eq!(form.models, ["deepseek-next", "deepseek-v4-pro"]);
        assert_eq!(form.model_efforts["deepseek-next"], ["low", "max"]);
        assert_eq!(form.model, "deepseek-next");
        assert_eq!(form.effort.as_deref(), Some("max"));
        assert!(form.catalog_note.contains("Live vendor"));

        // A present but unusable capability list is different from absent
        // metadata: never resurrect the static levels for that live model.
        let (tx, rx) = mpsc::sync_channel(1);
        app.vendor_catalog_pending = Some((launch::Engine::DeepSeek, rx));
        tx.send(Some(vec![doxa_vendors::ModelCapability { id: "deepseek-flash".into(),
            efforts: Vec::new(), default_effort: None, effort_metadata_present: true }])).unwrap();
        assert!(app.poll_vendor_catalog());
        assert!(app.new_session.as_ref().unwrap().models.is_empty());

        let (stale_tx, stale_rx) = mpsc::sync_channel(1);
        app.vendor_catalog_pending = Some((launch::Engine::DeepSeek, stale_rx));
        app.engine_selected = 3;
        app.select_new_engine();
        assert!(stale_tx.send(Some(vec![row("deepseek-flash", &[], None)])).is_err());
        assert_eq!(app.new_session.as_ref().unwrap().model, "glm-5.3-flash");
        let (tx, rx) = mpsc::sync_channel(1);
        app.vendor_catalog_pending = Some((launch::Engine::Glm, rx));
        app.new_session.as_mut().unwrap().catalog_pending = true;
        tx.send(Some(vec![row("unknown-glm", &[], None)])).unwrap();
        assert!(app.poll_vendor_catalog());
        assert!(app.new_session.as_ref().unwrap().models.is_empty());
        assert!(app.new_session.as_ref().unwrap().model.is_empty());
        assert!(app.new_session.as_ref().unwrap().catalog_note.contains("choose another engine"));
        app.new_session.as_mut().unwrap().field = 2;
        app.new_session_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.pending_launches.is_empty());
        assert!(app.notice.contains("No verified models"));

        app.engine_selected = 3;
        app.select_new_engine();
        let (tx, rx) = mpsc::sync_channel(1);
        app.vendor_catalog_pending = Some((launch::Engine::Glm, rx));
        tx.send(Some(vec![row("glm-5.3-flash", &[], None)])).unwrap();
        assert!(app.poll_vendor_catalog());
        assert!(app.new_session.as_ref().unwrap().catalog_note.contains("measured effort fallback"));

        app.engine_selected = 2;
        app.select_new_engine();
        let (tx, rx) = mpsc::sync_channel(1);
        app.vendor_catalog_pending = Some((launch::Engine::DeepSeek, rx));
        app.new_session.as_mut().unwrap().catalog_pending = true;
        tx.send(None).unwrap();
        assert!(app.poll_vendor_catalog());
        assert_eq!(app.new_session.as_ref().unwrap().models, ["deepseek-flash", "deepseek-v4-pro"]);
        assert!(app.new_session.as_ref().unwrap().catalog_note.contains("Static fallback"));
        app.catalog_efforts.insert(("deepseek".into(), "deepseek-flash".into()), vec!["low".into()]);
        app.engine_selected = 2;
        app.select_new_engine();
        assert!(!app.catalog_efforts.contains_key(&("deepseek".into(), "deepseek-flash".into())));
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
    fn clear_replaces_only_active_tab_after_launch_and_defers_finalization() {
        let mut app = App::default();
        app.clear_preflight_error = None;
        app.groups[0].tabs = vec!["other".into(), "old".into()];
        app.groups[0].active = 1;
        app.session_identity.insert("old".into(), (Some("codex".into()), Some("old-model".into())));
        app.session_cwds.insert("old".into(), PathBuf::from("/repo"));
        app.session_activity.insert("old".into(), (false, 0));
        app.collections.push(crate::collections::Collection { name:"Work".into(), sessions:vec!["old".into()], collapsed:false });
        app.input = "/clear".into();
        assert!(app.submit_local_command());
        assert_eq!(app.pending_launches.len(), 1);
        assert_eq!(app.pending_launches[0].0.engine, launch::Engine::Codex);
        assert_eq!(app.pending_launches[0].0.cwd.as_deref(), Some(Path::new("/repo")));
        assert!(app.pending_stops.is_empty());
        app.apply_daemon_frame(&json!({"type":"launch_reply","ok":true,"session_id":"fresh","group":0}));
        assert_eq!(app.groups[0].tabs, ["other", "fresh"]);
        assert_eq!(app.groups[0].active_id(), Some("fresh"));
        assert_eq!(app.collections[0].sessions, ["fresh"]);
        assert_eq!(app.clear_stop_after_save, ["old"]);
        assert!(app.pending_stops.is_empty());
        assert!(app.finish_clear_swap(true));
        assert_eq!(app.pending_clear_finalizes, ["old"]);
        assert!(app.clear_stop_after_save.is_empty());
    }

    #[test]
    fn clear_failure_and_unsupported_forms_preserve_the_old_session() {
        let mut app = App::default();
        app.clear_preflight_error = None;
        app.groups[0].tabs = vec!["old".into()];
        app.session_identity.insert("old".into(), (Some("codex".into()), None));
        app.session_cwds.insert("old".into(), PathBuf::from("/repo"));
        app.input = "/clear now".into();
        assert!(app.submit_local_command());
        assert_eq!(app.notice, "Usage: /clear");
        assert!(app.pending_launches.is_empty());
        app.input = "/clear".into();
        assert!(app.submit_local_command());
        app.pending_launches.clear(); // the bridge accepted the launch
        app.apply_daemon_frame(&json!({"type":"launch_reply","ok":false,"message":"spawn failed"}));
        assert_eq!(app.groups[0].tabs, ["old"]);
        assert!(app.clear_stop_after_save.is_empty());
        assert!(app.pending_stops.is_empty());
        assert!(app.pending_prompts.is_empty());
        app.session_activity.insert("old".into(), (true, 1));
        app.input = "/clear".into();
        assert!(app.submit_local_command());
        assert!(app.notice.contains("wait for the current turn"));
        assert!(app.pending_launches.is_empty());
    }

    #[test]
    fn clear_requires_persistent_state_and_rolls_back_unsaved_swap() {
        let mut app = App::default();
        app.groups[0].tabs = vec!["old".into()];
        app.session_identity.insert("old".into(), (Some("codex".into()), None));
        app.session_cwds.insert("old".into(), PathBuf::from("/repo"));
        app.input = "/clear".into();
        assert!(app.submit_local_command());
        assert!(app.notice.contains("persistent tabset unavailable"));
        assert!(app.pending_launches.is_empty());

        app.clear_preflight_error = None;
        app.input = "/clear".into();
        assert!(app.submit_local_command());
        app.apply_daemon_frame(&json!({"type":"launch_reply","ok":true,"session_id":"fresh","group":0}));
        assert!(app.finish_clear_swap(false));
        assert_eq!(app.groups[0].tabs, ["old"]);
        assert!(app.clear_stop_after_save.is_empty());
        assert_eq!(app.pending_clear_finalizes, ["fresh"]);
    }

    #[test]
    fn clear_restarts_from_shared_checkout_when_old_session_is_in_a_worktree() {
        let dir = tempfile::tempdir().unwrap();
        let main = dir.path().join("main");
        let old_tree = dir.path().join("old-tree");
        std::fs::create_dir(&main).unwrap();
        let git = |args: &[&str]| assert!(std::process::Command::new("git").args(args)
            .current_dir(&main).status().unwrap().success());
        git(&["init", "-q"]);
        std::fs::write(main.join("tracked.txt"), "base\n").unwrap();
        git(&["add", "tracked.txt"]);
        git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: base"]);
        git(&["worktree", "add", "--detach", "-q", old_tree.to_str().unwrap()]);
        let mut app = App::default();
        app.clear_preflight_error = None;
        app.groups[0].tabs.push("old".into());
        app.session_identity.insert("old".into(), (Some("codex".into()), None));
        app.session_cwds.insert("old".into(), old_tree);
        app.input = "/clear".into();
        assert!(app.submit_local_command());
        assert_eq!(app.pending_launches[0].0.cwd.as_deref(), Some(main.as_path()));
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

    #[test]
    fn deepseek_balance_chip_requires_valid_available_balance() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"deep", "engine":"deepseek"}));
        app.groups[0].tabs = vec!["deep".into()];
        assert!(!app.chips(0).iter().any(|(kind, _)| *kind == "balance"));
        app.apply_daemon_frame(&json!({"type":"telemetry_status", "session_id":"deep",
            "status":{"session_id":"deep", "billing":{"mode":"api","balance":"$3.25 · ¥25.50"}}}));
        assert!(app.chips(0).iter().any(|(kind, label)| *kind == "balance" && label == "Balance $3.25 · ¥25.50"));
        app.apply_daemon_frame(&json!({"type":"telemetry_status", "session_id":"deep",
            "status":{"session_id":"deep", "billing":null}}));
        assert!(!app.chips(0).iter().any(|(kind, _)| *kind == "balance"));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"glm", "engine":"glm",
            "billing":{"mode":"api","balance":"$100.00"}}));
        app.groups[0].tabs = vec!["glm".into()];
        assert!(!app.chips(0).iter().any(|(kind, _)| *kind == "balance"));
    }

    fn painted(app: &App) -> String {
        let mut terminal = Terminal::new(TestBackend::new(100, 28)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let buffer = terminal.backend().buffer();
        (0..28).map(|y| (0..100).map(|x| buffer[(x, y)].symbol()).collect::<String>())
            .collect::<Vec<_>>().join("\n")
    }

    #[test]
    fn repository_chip_tracks_each_panes_actual_session_and_invalidates_stale_work() {
        use doxa_worktrees::RepoStatus;
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(220, 32));
        app.groups[0].tabs = vec!["a".into(), "b".into()];
        app.groups[1].tabs = vec!["c".into()];
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"a","cwd":"/tmp/a"}));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"b","cwd":"/tmp/b"}));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"c","cwd":"/tmp/c"}));
        app.set_repo_status("a", RepoStatus::Repository {
            repo: "project".into(), base: Some("main".into()),
            checked_out: Some("doxa/a".into()), sha: Some("1234567".into()),
            worktree: Some("doxa/a".into()),
        });
        app.set_repo_status("b", RepoStatus::Directory { name: "scratch".into() });
        app.set_repo_status("c", RepoStatus::Repository {
            repo: "other".into(), base: Some("feature".into()),
            checked_out: Some("feature".into()), sha: Some("abcdef0".into()),
            worktree: None,
        });
        assert!(app.chips(0).contains(&("repo", "project ⎇ main [wt doxa/a] @1234567".into())));
        assert_eq!(app.repo_detail(0).as_deref(), Some("base main · HEAD doxa/a · managed worktree doxa/a"));
        let rendered = painted_at(&app, 220, 32);
        assert!(rendered.contains("project ⎇ main [wt doxa/a] @1234567"));
        assert!(rendered.contains("u ? · scope ?"));
        assert!(app.chips(1).contains(&("repo", "other ⎇ feature @abcdef0".into())));
        app.groups[0].active = 1;
        assert!(app.chips(0).contains(&("directory", "dir scratch".into())));
        assert!(!app.chips(0).iter().any(|(kind, _)| *kind == "repo"));
        app.handle(Event::Resize(100, 28));
        assert!(app.chips(0).contains(&("directory", "dir scratch".into())));
        app.apply_daemon_frame(&json!({"type":"event","session_id":"c",
            "event":{"type":"branch_changed","data":{"base":"main"}}}));
        assert!(!app.chips(1).iter().any(|(kind, _)| *kind == "repo"));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"b","cwd":"/tmp/new"}));
        assert!(!app.chips(0).iter().any(|(kind, _)| *kind == "directory"));
        let (tx, rx) = mpsc::sync_channel(1);
        let old_epoch = app.repo_epoch.get("b").copied().unwrap_or_default();
        app.repo_pending = Some(("b".into(), PathBuf::from("/tmp/new"), old_epoch, rx));
        app.invalidate_repo("b");
        tx.send(Some(RepoStatus::Directory { name: "stale".into() })).unwrap();
        app.poll_repo();
        assert!(!app.chips(0).iter().any(|(kind, _)| *kind == "directory"));
    }

    fn painted_at(app: &App, width: u16, height: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let buffer = terminal.backend().buffer();
        (0..height).map(|y| (0..width).map(|x| buffer[(x, y)].symbol()).collect::<String>())
            .collect::<Vec<_>>().join("\n")
    }

    fn scrolled_picker_app() -> App {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("session".into());
        app
    }

    fn hover_first_picker_row(app: &mut App, offset: u16) -> (Rect, usize) {
        painted_at(app, 100, 28);
        let start = app.chooser_view_start.get();
        assert!(start > 0, "test must start with a scrolled viewport");
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: menu.y + offset, modifiers: KeyModifiers::NONE }));
        painted_at(app, 100, 28);
        assert_eq!(app.chooser_view_start.get(), start, "hover must not shift visible entries");
        (menu, start)
    }

    fn click_picker_row(app: &mut App, menu: Rect, offset: u16) {
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + offset, modifiers: KeyModifiers::NONE }));
    }

    #[test]
    fn minimum_stop_confirmation_shows_confirmation_and_cancel_keys() {
        let mut app = App::default();
        app.stop_confirmation = Some("a-long-session-identifier-that-can-wrap".into());
        let mut terminal = Terminal::new(TestBackend::new(40, 12)).unwrap();
        terminal.draw(|frame| app.draw_stop_confirmation(frame, Rect::new(0, 0, 40, 12))).unwrap();
        let buffer = terminal.backend().buffer();
        let text = (0..12).map(|y| (0..40).map(|x| buffer[(x, y)].symbol()).collect::<String>())
            .collect::<Vec<_>>().join("\n");
        assert!(text.contains("Y stop · Esc/N cancel"));
        assert!(text.contains("draft stay."));
    }

    #[test]
    fn initial_tool_section_navigation_selects_visible_edge_without_skipping() {
        for forward in [false, true] {
            let mut app = scrolled_picker_app();
            app.sessions.push(Session { id: "session".into(), title: "Session".into(),
                collection: String::new(), transcript: "Tool: Read started".into(), status: "Ready".into() });
            *app.visible_tool_sections.borrow_mut() = (0..3).map(|index|
                (Rect::new(0, index as u16, 20, 1), 0, "session".into(), index)).collect();
            assert!(app.select_tool_section(forward));
            assert_eq!(app.selected_tool_sections["session"], if forward { 0 } else { 2 });
            assert!(app.select_tool_section(forward));
            assert_eq!(app.selected_tool_sections["session"], 1);
        }
    }

    #[test]
    fn chooser_bottom_border_never_activates_hidden_choice() {
        for kind in ["engine", "model", "effort", "permission", "slash"] {
            let mut app = scrolled_picker_app();
            match kind {
                "engine" => app.engine_picker = true,
                "model" => app.model_picker = Some(ModelPicker { session_id: "session".into(),
                    models: vec!["one".into(), "two".into(), "three".into()], selected: 0,
                    note: String::new(), loading: false, catalog_pending: false }),
                "effort" => app.effort_picker = Some(EffortPicker { session_id: "session".into(),
                    engine: "deepseek".into(), model: "deepseek-flash".into(),
                    levels: vec!["none".into(), "low".into(), "high".into()], selected: 0 }),
                "permission" => app.permission_picker = Some(("session".into(), 0)),
                _ => { app.focus = Focus::Prompt; app.input = "/".into(); }
            }
            app.active_chooser_rect();
            app.chooser_height_override.set(Some(5));
            let menu = app.active_chooser_rect().unwrap();
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
                column: menu.x + 2, row: menu.bottom() - 1, modifiers: KeyModifiers::NONE }));
            assert!(app.pending_model_changes.is_empty(), "{kind}");
            assert!(app.pending_effort_changes.is_empty(), "{kind}");
            assert!(app.pending_permission_changes.is_empty(), "{kind}");
            assert!(app.new_session.is_none(), "{kind}");
            if kind == "engine" { assert!(app.engine_picker); }
            if kind == "model" { assert!(app.model_picker.is_some()); }
            if kind == "effort" { assert_eq!(app.effort_picker.as_ref().unwrap().selected, 0); }
            if kind == "permission" { assert_eq!(app.permission_picker.as_ref().unwrap().1, 0); }
            if kind == "slash" { assert_eq!(app.input, "/"); }
        }
    }

    #[test]
    fn scrolled_repo_hover_redraw_click_opens_same_visible_directory() {
        let root = tempfile::tempdir().unwrap();
        let paths: Vec<_> = (0..30).map(|i| root.path().join(format!("child-{i:02}"))).collect();
        for path in &paths { std::fs::create_dir(path).unwrap(); }
        let mut app = scrolled_picker_app();
        app.repo_picker = Some(RepoPicker { current_dir: root.path().to_path_buf(),
            paths: paths.clone(), selected: 24 });
        app.active_chooser_rect();
        app.chooser_height_override.set(Some(8));
        let (menu, start) = hover_first_picker_row(&mut app, 2);
        assert_eq!(app.repo_picker.as_ref().unwrap().selected, start);
        click_picker_row(&mut app, menu, 2);
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, paths[start]);
        assert_eq!(app.chooser_height_override.get(), Some(8), "folder browsing keeps the chosen height");
    }

    #[test]
    fn scrolled_branch_hover_redraw_click_requests_same_visible_branch() {
        let mut app = scrolled_picker_app();
        let branches: Vec<_> = (0..30).map(|i| format!("branch-{i:02}")).collect();
        app.branch_picker = Some(BranchPicker { session_id: "session".into(), base: "main".into(),
            branches: branches.clone(), selected: 24 });
        let (menu, start) = hover_first_picker_row(&mut app, 2);
        click_picker_row(&mut app, menu, 2);
        assert!(matches!(&app.pending_queue_commands[0], crate::bridge::WorkerCommand::Branch(id, Some(branch))
            if id == "session" && branch == &branches[start]));
    }

    #[test]
    fn scrolled_model_hover_redraw_click_requests_same_visible_model() {
        let mut app = scrolled_picker_app();
        let models: Vec<_> = (0..30).map(|i| format!("model-{i:02}")).collect();
        app.model_picker = Some(ModelPicker { session_id: "session".into(), models: models.clone(),
            selected: 24, note: "Verified models".into(), loading: false, catalog_pending: false });
        let (menu, start) = hover_first_picker_row(&mut app, 3);
        click_picker_row(&mut app, menu, 3);
        assert_eq!(app.pending_model_changes, vec![("session".into(), models[start].clone())]);
    }

    #[test]
    fn long_lore_claim_and_proposal_rows_keep_hover_click_target() {
        let cwd = tempfile::tempdir().unwrap();
        for proposal_mode in [false, true] {
            let mut app = scrolled_picker_app();
            app.lore_picker = Some(LorePicker {
            session_id: None,
                rows: (1..=2).map(|id| lore_picker::Belief { id, subject: format!("belief-{id}"),
                    claim: "long claim ".repeat(80), truncated: false, confidence: 0.9,
                    evidence_count: None }).collect(),
                proposals: (1..=2).map(|id| lore_picker::Proposal { pid: format!("proposal-{id}"),
                    kind: "belief".into(), action: "add".into(), scope: "project".into(),
                    summary: "long summary ".repeat(80) }).collect(),
                selected: 0, query: String::new(), offset: 0, status: "long status ".repeat(40),
                evidence: None, pending: None, proposal_mode, review: None, review_scroll: 0,
                review_seen: 0, review_width: 0, armed_resolution: None, can_resolve: false,
                resolving: false, cwd: cwd.path().display().to_string(), belief_review: None,
                can_act_on_beliefs: false, belief_action: None, belief_note: String::new(),
                retract_armed: false, belief_acting: false, result_status: None,
            });
            let menu = app.active_chooser_rect().unwrap();
            let offset = if proposal_mode { 5 } else if menu.height < 10 { 4 } else { 7 };
            let rendered = painted_at(&app, 100, 28);
            let row = usize::from(menu.y + offset);
            assert!(rendered.lines().nth(row).unwrap().contains(if proposal_mode { "proposal-2" } else { "#2" }));
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
                column: menu.x + 2, row: menu.y + offset, modifiers: KeyModifiers::NONE }));
            assert_eq!(app.lore_picker.as_ref().unwrap().selected, 1);
            let hovered = painted_at(&app, 100, 28);
            assert!(hovered.lines().nth(row).unwrap().contains(if proposal_mode { "proposal-2" } else { "#2" }));
            click_picker_row(&mut app, menu, offset);
            let picker = app.lore_picker.as_ref().unwrap();
            assert_eq!(picker.selected, 1);
            assert!(picker.pending.is_some(), "click requests review of the highlighted entry");
        }
    }

    #[test]
    fn chooser_resize_resets_after_close_menu_change_and_pane_switch() {
        let mut app = scrolled_picker_app();
        app.engine_picker = true;
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y, modifiers: KeyModifiers::NONE }));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Drag(MouseButton::Left),
            column: menu.x + 2, row: menu.bottom() - 5, modifiers: KeyModifiers::NONE }));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Up(MouseButton::Left),
            column: menu.x + 2, row: menu.bottom() - 5, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.active_chooser_rect().unwrap().height, 5);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert_eq!(app.chooser_height_override.get(), None);
        app.engine_picker = true;
        assert_eq!(app.active_chooser_rect().unwrap().height, 7);
        app.chooser_height_override.set(Some(5));
        app.engine_picker = false;
        app.model_picker = Some(ModelPicker { session_id: "session".into(), models: vec!["one".into(), "two".into()],
            selected: 0, note: String::new(), loading: false, catalog_pending: false });
        assert_eq!(app.active_chooser_rect().unwrap().height, 6);
        app.chooser_height_override.set(Some(5));
        app.active_group = 1;
        app.groups[1].tabs.push("other".into());
        assert_eq!(app.active_chooser_rect().unwrap().height, 6);
    }

    #[test]
    fn repo_chip_picker_hovers_browses_and_opens_verified_directory_in_new_tab() {
        let root = tempfile::tempdir().unwrap();
        let child = root.path().join("child");
        std::fs::create_dir(&child).unwrap();
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("session".into());
        app.session_identity.insert("session".into(), (Some("codex".into()), None));
        app.session_cwds.insert("session".into(), child.clone());
        app.repo_cache.insert("session".into(),
            (Some(doxa_worktrees::RepoStatus::Directory { name: "child".into() }), Instant::now()));
        let mut initial = Terminal::new(TestBackend::new(100, 28)).unwrap();
        initial.draw(|frame| app.draw(frame)).unwrap();
        let hit = app.rendered_chip_hits.borrow().as_ref().unwrap().iter()
            .find(|hit| hit.kind == "directory").unwrap().clone();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: hit.rect.x, row: hit.rect.y, modifiers: KeyModifiers::NONE }));
        let menu = app.active_chooser_rect().unwrap();
        assert_eq!(app.repo_picker.as_ref().unwrap().paths, vec![child, root.path().to_path_buf()]);
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: menu.y + 3, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.repo_picker.as_ref().unwrap().selected, 1);
        let mut terminal = Terminal::new(TestBackend::new(100, 28)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert_eq!(terminal.backend().buffer()[(menu.x + 2, menu.y + 3)].bg, theme::HIGHLIGHT);
        app.repo_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, root.path());
        assert!(app.pending_launches.is_empty());
        app.repo_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.repo_picker.is_none());
        assert_eq!(app.pending_launches[0].0.cwd.as_deref(), Some(root.path()));
        assert_eq!(app.groups[0].active_id(), Some("session"));
    }

    #[test]
    fn repo_picker_mouse_scroll_click_and_unsafe_path_filter() {
        let root = tempfile::tempdir().unwrap();
        let child = root.path().join("child");
        let unsafe_child = root.path().join("unsafe\nname");
        std::fs::create_dir(&child).unwrap();
        std::fs::create_dir(&unsafe_child).unwrap();
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("session".into());
        app.session_identity.insert("session".into(), (Some("codex".into()), None));
        app.session_cwds.insert("session".into(), child);
        app.open_repo_picker(0);
        assert_eq!(app.repo_picker.as_ref().unwrap().paths.len(), 2);
        let menu = app.active_chooser_rect().unwrap();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::ScrollDown,
            column: menu.x + 2, row: menu.y + 2, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.repo_picker.as_ref().unwrap().selected, 1);
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 3, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, root.path());
        assert!(app.pending_launches.is_empty());
        app.session_cwds.insert("session".into(), unsafe_child);
        app.open_repo_picker(0);
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, root.path());
        assert!(!app.repo_picker.as_ref().unwrap().paths.iter().any(|path|
            path.to_string_lossy().contains("unsafe\nname")));
    }

    #[test]
    fn repo_picker_browses_child_directories_and_returns_to_parent() {
        let root = tempfile::tempdir().unwrap();
        let child = root.path().join("child");
        let grandchild = child.join("grandchild");
        std::fs::create_dir_all(&grandchild).unwrap();
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("session".into());
        app.session_identity.insert("session".into(), (Some("codex".into()), None));
        app.session_cwds.insert("session".into(), root.path().to_path_buf());
        app.open_repo_picker(0);
        let child_index = app.repo_picker.as_ref().unwrap().paths.iter()
            .position(|path| path == &child).unwrap();
        app.repo_picker.as_mut().unwrap().selected = child_index;
        app.repo_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, child);
        let grandchild_index = app.repo_picker.as_ref().unwrap().paths.iter()
            .position(|path| path == &grandchild).unwrap();
        app.repo_picker.as_mut().unwrap().selected = grandchild_index;
        app.repo_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, grandchild);
        app.repo_picker.as_mut().unwrap().selected = 1;
        app.repo_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, child);
        assert!(app.pending_launches.is_empty());
    }

    #[test]
    fn repo_picker_does_not_substitute_another_sessions_directory() {
        let other = tempfile::tempdir().unwrap();
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("missing".into());
        app.session_cwds.insert("other".into(), other.path().to_path_buf());
        app.open_repo_picker(0);
        assert!(app.repo_picker.is_none());
        assert_eq!(app.notice, "Current session directory is unavailable");
    }

    #[test]
    fn repo_picker_shows_home_relative_paths_without_changing_targets() {
        let Some(home) = std::env::var_os("HOME").map(PathBuf::from) else { return; };
        let target = home.join("example").join("project");
        assert_eq!(repo_path_label(&target), "~/example/project");
    }

    #[test]
    fn branch_and_settings_rows_select_on_hover() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("session".into());
        app.branch_picker = Some(BranchPicker { session_id: "session".into(),
            base: "main".into(), branches: vec!["main".into(), "feature".into()], selected: 0 });
        let menu = app.active_chooser_rect().unwrap();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: menu.y + 3, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.branch_picker.as_ref().unwrap().selected, 1);
        app.branch_picker = None;
        app.settings_menu = Some(SettingsMenu { rows: [("120".into(), false), ("false".into(), false)],
            selected: 0, linger_draft: None });
        let menu = app.active_chooser_rect().unwrap();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: menu.y + 4, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.settings_menu.as_ref().unwrap().selected, 1);
    }

    #[test]
    fn ask_user_hover_click_and_border_drag_use_inline_menu() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"session", "engine":"codex"}));
        app.groups[0].tabs.push("session".into());
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"session",
            "event":{"type":"needs_input", "data":{"id":"question", "kind":"ask_user",
                "questions":[{"question":"Choose?", "options":[
                    {"label":"First", "description":"One"}, {"label":"Second", "description":"Two"}]}]}}}));
        let before = app.active_chooser_rect().unwrap();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: before.x + 2, row: before.y, modifiers: KeyModifiers::NONE }));
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Drag(MouseButton::Left),
            column: before.x + 2, row: before.y.saturating_sub(2), modifiers: KeyModifiers::NONE }));
        let menu = app.active_chooser_rect().unwrap();
        assert!(menu.height > before.height);
        app.mouse(MouseEvent { kind: MouseEventKind::Up(MouseButton::Left),
            column: menu.x + 2, row: menu.y, modifiers: KeyModifiers::NONE });
        let request = &app.input_requests[0];
        let second = (menu.y + 1..menu.bottom() - 1)
            .find(|&row| ask_user_option_at(request, menu, row) == Some(2)).unwrap();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: second, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.input_requests[0].selected, 2);
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: second, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.pending_answers.len(), 1);
        assert_eq!(app.pending_answers[0].2["answers"]["Choose?"], "Second");
    }

    #[test]
    fn ask_user_mouse_mapping_matches_wrapped_visible_option() {
        let mut app = App::default();
        app.input_requests.push(InputRequest::from_event("session", &json!({
            "id":"question", "kind":"ask_user", "questions":[{"question":"Choose?",
                "options":[
                    {"label":"First option with several long words that wrap into more than one row",
                     "description":"A description that also wraps across the menu"},
                    {"label":"Second", "description":"Short"}]}]
        })).unwrap());
        app.groups[0].tabs.push("session".into());
        let menu = Rect::new(0, 0, 34, 16);
        let mut terminal = Terminal::new(TestBackend::new(menu.width, menu.height)).unwrap();
        terminal.draw(|frame| app.draw_request(frame, menu, true)).unwrap();
        let buffer = terminal.backend().buffer();
        let second_row = (1..menu.height - 1).find(|&row|
            (1..menu.width - 1).map(|x| buffer[(x, row)].symbol()).collect::<String>().contains("Second"))
            .unwrap();
        assert_eq!(ask_user_option_at(&app.input_requests[0], menu, second_row), Some(2));
    }

    #[test]
    fn inline_chooser_upper_border_drag_resizes_and_hover_tracks_selected_row() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 30));
        app.groups[0].tabs.push("session".into());
        app.engine_picker = true;
        let before = app.active_chooser_rect().unwrap();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: before.x + 3, row: before.y, modifiers: KeyModifiers::NONE }));
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Drag(MouseButton::Left),
            column: before.x + 3, row: before.y.saturating_sub(3), modifiers: KeyModifiers::NONE }));
        let after = app.active_chooser_rect().unwrap();
        assert!(after.height > before.height);
        assert_eq!(after.bottom(), before.bottom());
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Up(MouseButton::Left),
            column: before.x + 3, row: after.y, modifiers: KeyModifiers::NONE }));
        let row = after.y + if after.height >= 10 { 5 } else { 3 };
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: after.x + 2, row, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.engine_selected, 1);
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert_eq!(terminal.backend().buffer()[(after.x + 2, row)].bg, theme::HIGHLIGHT);
    }

    #[test]
    fn model_picker_hover_highlights_and_enter_activates_hovered_model() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("session".into());
        app.model_picker = Some(ModelPicker { session_id: "session".into(),
            models: vec!["first".into(), "second".into()], selected: 0,
            note: "Verified models".into(), loading: false, catalog_pending: false });
        let menu = app.active_chooser_rect().unwrap();
        let row = menu.y + 4;
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.model_picker.as_ref().unwrap().selected, 1);
        let mut terminal = Terminal::new(TestBackend::new(100, 28)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert_eq!(terminal.backend().buffer()[(menu.x + 2, row)].bg, theme::HIGHLIGHT);
        app.model_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.pending_model_changes, vec![("session".into(), "second".into())]);
    }

    #[test]
    fn every_chip_hover_and_click_uses_rendered_strip_geometry() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(220, 32));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"claude",
            "model":"sonnet","cwd":"/repo","permission_mode":"auto",
            "can_set_permission_mode":true,"can_set_model":true,"lore_scrub":"ready"}));
        app.groups[0].tabs = vec!["s".into()];
        app.set_lore_memory_usage("s", 200, 1000, 80, 200);
        let pane = app.layout(app.size).body;
        let strip = app.pane_regions(0, pane)[3];
        let mut x = strip.x;
        let visible = app.chip_window(0, usize::from(strip.width));
        assert_eq!(visible.len(), app.chips(0).len());
        for (kind, label) in visible {
            let inside = x + 1;
            assert!(app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
                column: inside, row: strip.y, modifiers: KeyModifiers::NONE })));
            assert_eq!(app.chip_hover.as_ref().map(|hit| hit.kind), Some(kind));
            let rendered = painted_at(&app, 220, 32);
            assert!(rendered.contains(chip_hint(kind)), "hover hint for {kind}");
            assert!(app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
                column: inside, row: strip.y, modifiers: KeyModifiers::NONE })));
            match kind {
                "permission" => assert!(app.permission_picker.is_some()),
                "engine" => assert!(app.engine_picker),
                "model" => assert!(app.model_picker.is_some()),
                "effort" => assert!(app.notice.contains("Live effort change is unavailable")),
                "beliefs" => assert!(app.lore_picker.is_some()),
                _ => {
                    assert_eq!(app.chip_info.as_ref().map(|info| info.kind), Some(kind));
                    if kind == "memory" { assert!(painted_at(&app, 220, 32).contains("Loading LORE memory")); }
                }
            }
            app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
            x += chip_text(kind, &label).width() as u16 + 1;
        }
    }

    #[test]
    fn narrow_and_split_panes_hit_visible_chips_without_disturbing_drafts() {
        for split in [Split::Vertical, Split::Horizontal] {
        for width in [76, 100, 140] {
            let mut app = App::default();
            app.rail_visible = false;
            app.handle(Event::Resize(width, 32));
            app.apply_daemon_frame(&json!({"type":"hello","session_id":"left","engine":"codex","model":"gpt-6-sol"}));
            app.apply_daemon_frame(&json!({"type":"hello","session_id":"right","engine":"claude","model":"sonnet"}));
            app.groups[0].tabs = vec!["left".into()];
            app.groups[1].tabs = vec!["right".into()];
            app.split = split;
            app.split_requested = true;
            app.input = "left draft".into();
            app.input_cursor = app.input.len();
            app.input_drafts.insert((1, "right".into()), ("right draft\ncontinued".into(), 21));
            let panes = app.layout(app.size).panes.unwrap();
            let strip = app.pane_regions(1, panes[1])[3];
            let visible = app.chip_window(1, usize::from(strip.width));
            assert!(!visible.is_empty());
            let (kind, _) = &visible[0];
            let x = strip.x + 1;
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
                column: x, row: strip.y, modifiers: KeyModifiers::NONE }));
            assert_eq!(app.chip_hover.as_ref().map(|hit| hit.kind), Some(*kind));
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
                column: x, row: strip.y, modifiers: KeyModifiers::NONE }));
            assert_eq!(app.active_group, 1);
            assert_eq!(app.input, "right draft\ncontinued");
            assert_eq!(app.input_drafts.get(&(0, "left".into())).unwrap().0, "left draft");
        }
        }
        assert!(chip_hint("memory").contains("click to view entries"));
    }

    #[test]
    fn overflow_chip_has_hover_hint_and_cycles_clickable_items() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(60, 28));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"claude",
            "model":"sonnet","permission_mode":"auto","can_set_permission_mode":true}));
        app.groups[0].tabs = vec!["s".into()];
        let pane = app.layout(app.size).body;
        let strip = app.pane_regions(0, pane)[3];
        let visible = app.chip_window(0, usize::from(strip.width));
        assert_eq!(visible.last().map(|row| row.0), Some("more"));
        let preceding = visible[..visible.len() - 1].iter()
            .map(|(kind, label)| chip_text(kind, label).width() + 1).sum::<usize>();
        let x = strip.x + preceding as u16 + 1;
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: x, row: strip.y, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.chip_hover.as_ref().map(|hit| hit.kind), Some("more"));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: x, row: strip.y, modifiers: KeyModifiers::NONE }));
        assert_ne!(app.chip_offsets[0], 0);
        assert!(app.chip_window(0, usize::from(strip.width)).iter().any(|(kind, _)| *kind == "memory"));
    }

    #[test]
    fn chip_hits_match_painted_cells_and_exclude_prompt_separator() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(140, 32));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "engine":"claude",
            "model":"sonnet", "permission_mode":"auto", "can_set_permission_mode":true}));
        app.groups[0].tabs = vec!["s".into()];

        // A redraw can observe new dimensions before its Resize event reaches
        // the input loop. Mouse hits must follow the frame the user sees.
        let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let hits = app.rendered_chip_hits.borrow().clone().unwrap();
        let permission = hits.iter().find(|hit| hit.kind == "permission").unwrap();
        let stale_pane = app.layout(app.size).body;
        assert_ne!(permission.rect.y, app.pane_regions(0, stale_pane)[3].y);
        for hit in &hits {
            for x in hit.rect.x..hit.rect.right() {
                assert_eq!(terminal.backend().buffer()[(x, hit.rect.y)].bg, theme::HIGHLIGHT);
                assert_eq!(app.chip_hit_at(x, hit.rect.y).as_ref().map(|hit| hit.kind), Some(hit.kind));
            }
            assert!(app.chip_hit_at(hit.rect.x, hit.rect.y - 1).is_none());
            assert!(app.chip_hit_at(hit.rect.x, hit.rect.y + 1).is_none());
        }
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: permission.rect.x + 1, row: permission.rect.y + 1,
            modifiers: KeyModifiers::NONE }));
        assert!(app.chip_hover.is_none());
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: permission.rect.x + 1, row: permission.rect.y,
            modifiers: KeyModifiers::NONE }));
        assert!(app.permission_picker.is_some());
    }

    #[test]
    fn transcript_links_hover_and_open_only_on_ctrl_left_click() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.apply_update(DaemonUpdate::Upsert(Session {
            id: "links".into(), title: "links".into(), collection: "repo".into(),
            transcript: "**Assistant:**\n\nRead [the guide](https://example.com/docs).".into(),
            status: "Idle".into(),
        }));
        app.groups[0].tabs = vec!["links".into()];
        let mut terminal = Terminal::new(TestBackend::new(100, 28)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let (rect, url) = app.visible_links.borrow().iter()
            .find(|(_, url)| url == "https://example.com/docs").cloned().unwrap();
        assert_eq!(url, "https://example.com/docs");
        assert!(app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: rect.x, row: rect.y, modifiers: KeyModifiers::NONE })));
        assert_eq!(app.link_hover.as_deref(), Some(url.as_str()));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: rect.x, row: rect.y, modifiers: KeyModifiers::NONE }));
        assert!(app.pending_open_urls.is_empty());
        assert!(app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: rect.x, row: rect.y, modifiers: KeyModifiers::CONTROL })));
        assert_eq!(app.pending_open_urls, vec![url]);
        assert!(app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: rect.x, row: rect.y + 1, modifiers: KeyModifiers::NONE })));
        assert!(app.link_hover.is_none());
        assert_eq!(pointer_shape(true), b"\x1b]22;pointer\x1b\\");
        assert_eq!(pointer_shape(false), b"\x1b]22;\x1b\\");
    }

    #[test]
    fn clicking_second_prompt_accepts_text_while_first_pane_waits_for_input() {
        for split in [Split::Vertical, Split::Horizontal] {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 30));
        for id in ["first", "second"] {
            app.apply_daemon_frame(&json!({"type":"hello", "session_id":id,
                "engine":"codex"}));
        }
        app.groups[0].tabs = vec!["first".into()];
        app.groups[1].tabs = vec!["second".into()];
        app.split_requested = true;
        app.split = split;
        app.input = "first draft".into();
        app.input_cursor = app.input.len();
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"first",
            "event":{"type":"needs_input", "data":{"id":"req", "kind":"ask_user",
                "questions":[{"question":"Choose", "options":[{"label":"Yes"}]}]}}}));
        assert!(app.active_request_index().is_some());
        let pane = app.layout(app.size).panes.unwrap()[1];
        let prompt = app.pane_regions(1, pane)[4];
        let point = MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: prompt.x + 2, row: prompt.y + 1, modifiers: KeyModifiers::NONE };
        assert!(app.handle(Event::Mouse(point)));
        assert_eq!(app.active_group, 1);
        assert_eq!(app.focus, Focus::Prompt);
        assert!(app.handle(Event::Key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE))));
        assert_eq!(app.input, "x");
        assert_eq!(app.input_drafts.get(&(0, "first".into())).unwrap().0, "first draft");
        assert!(app.input_requests.iter().any(|request| request.session_id == "first"));
        }
    }

    #[test]
    fn multiline_prompt_edits_at_cursor_and_enter_submits() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Resize(100, 28));
        for ch in "firstlast".chars() {
            app.handle(Event::Key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE)));
        }
        for _ in 0..4 { app.handle(Event::Key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE))); }
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('j'), KeyModifiers::CONTROL)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE)));
        assert_eq!(app.input, "first\n\nlast");
        assert!(painted(&app).contains("first"));
        assert!(painted(&app).contains("last"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_prompts, [("s".into(), "first\n\nlast".into())]);
        assert!(app.input.is_empty());
        assert_eq!(app.input_cursor, 0);
    }

    #[test]
    fn slash_autocomplete_appears_above_prompt_and_completes_without_sending() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Resize(100, 28));
        for ch in "/he".chars() {
            app.handle(Event::Key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE)));
        }
        assert_eq!(app.slash_suggestions(), vec![("/help", "Command registry")]);
        assert!(painted(&app).contains("Commands"));
        assert!(app.active_chooser_rect().is_some());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE)));
        assert_eq!(app.input, "/help");
        assert!(app.slash_suggestions().is_empty());
        assert!(app.pending_prompts.is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.chip_info.as_ref().map(|info| info.kind), Some("help"));
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn slash_autocomplete_mouse_choice_and_escape_preserve_draft() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Resize(100, 28));
        for ch in "/mo".chars() {
            app.handle(Event::Key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE)));
        }
        assert_eq!(app.slash_suggestions().len(), 3);
        painted(&app);
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 2, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.input, "/model");
        assert!(app.slash_suggestions().is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE)));
        assert_eq!(app.input, "/mode");
        assert!(!app.slash_suggestions().is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert_eq!(app.input, "/mode");
        assert!(app.slash_suggestions().is_empty());
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn bare_doxa_commands_stay_local_and_unknown_provider_commands_pass_through() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Resize(100, 28));

        app.input = "/help".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.chip_info.as_ref().map(|info| info.kind), Some("help"));
        assert!(app.input.is_empty());
        assert!(app.pending_prompts.is_empty());
        app.chip_info = None;

        app.input = "/model opus".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.input, "/model opus");
        assert!(app.notice.contains("arguments are not available"));
        assert!(app.pending_prompts.is_empty());

        app.input = "/compact".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.notice.contains("only for Claude"));
        assert!(app.pending_prompts.is_empty());

        app.session_identity.insert("s".into(), (Some("claude".into()), None));
        app.input = "/compact".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_prompts, [("s".into(), "/compact".into())]);
        app.pending_prompts.clear();

        app.input = "/compact extra".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.notice.contains("Usage: /compact"));
        assert!(app.pending_prompts.is_empty());

        app.input = "/provider-command".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_prompts, [("s".into(), "/provider-command".into())]);
    }

    #[test]
    fn help_lists_full_python_registry_with_rust_capabilities() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Resize(100, 30));
        assert_eq!(COMMANDS.len(), 42);
        let mut names = std::collections::HashSet::new();
        for row in COMMANDS { assert!(names.insert(row.name)); }
        app.open_help();
        let info = app.chip_info.as_ref().unwrap();
        assert_eq!(info.kind, "help");
        for form in ["/collection [action] [name]", "/usage", "/context", "/compact",
            "/fleet ...", "/help"] {
            assert!(info.lines.iter().any(|line| line.starts_with(form)), "missing {form}");
        }
        assert!(info.lines.iter().any(|line| line.contains("unavailable in Rust")));
        assert!(info.lines.iter().any(|line| line.contains("Claude only")));
        let menu = app.active_chooser_rect().unwrap();
        assert!(menu.bottom() < app.layout(app.size).body.bottom());
        app.handle(Event::Key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)));
        assert!(app.chip_info.as_ref().unwrap().scroll > 0);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(app.chip_info.is_none());
    }

    #[test]
    fn known_doxa_commands_never_escape_as_provider_prompts() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Resize(100, 30));
        for command in ["/fleet", "/doctor", "/clear", "/update", "/plugins",
            "/help\nignore", "/msg\t"] {
            app.input = command.into();
            app.input_cursor = app.input.len();
            app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
            assert!(app.pending_prompts.is_empty(), "forwarded {command}");
        }
        app.input = "/provider-command".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_prompts, [("s".into(), "/provider-command".into())]);
    }

    #[test]
    fn usage_and_context_open_inline_with_only_measured_session_values() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 32));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"first-session",
            "engine":"claude","model":"sonnet","ctx_percentage":25.0,
            "ctx_tokens":250,"ctx_max_tokens":1000,
            "total_cost_usd":0.25,
            "usage":{"num_turns":2,"input_tokens":100,"output_tokens":20,
                "cache_read_input_tokens":7,"cache_creation_input_tokens":3}}));
        app.groups[0].tabs = vec!["first-session".into(), "second-session".into()];
        app.groups[0].active = 0;
        app.input = "/usage".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        let info = app.chip_info.as_ref().unwrap();
        assert_eq!(info.kind, "usage");
        assert!(info.lines.iter().any(|line| line.contains("tokens in") && line.contains("100")));
        assert!(info.lines.iter().any(|line| line.contains("cost") && line.contains("$0.2500")));
        let menu = app.active_chooser_rect().unwrap();
        assert!(menu.bottom() < app.layout(app.size).body.bottom());
        assert!(app.input.is_empty());
        assert!(app.pending_prompts.is_empty());

        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        app.groups[0].active = 1;
        app.input = "/context".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        let info = app.chip_info.as_ref().unwrap();
        assert_eq!(info.kind, "context");
        assert!(info.lines.iter().any(|line| line.contains("not reported by this engine")));
        assert!(!info.lines.iter().any(|line| line.contains("250") || line.contains("25.0%")));

        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        app.groups[0].active = 0;
        app.open_chip_info("context", 0);
        let info = app.chip_info.as_ref().unwrap();
        assert!(info.lines.iter().any(|line| line.contains("250 / 1000 tokens")));
        assert!(info.lines.iter().any(|line| line.contains("25.0%")));
    }

    #[test]
    fn usage_does_not_promote_turn_cost_or_partial_tokens_to_session_totals() {
        let mut telemetry = SessionTelemetry::default();
        telemetry.update_turn(&json!({"usage_scope":"turn", "num_turns":1,
            "input_tokens":5,"output_tokens":2,"cost_usd":0.01,
            "ctx_percentage":null,"ctx_tokens":null,"ctx_max_tokens":null}));
        assert_eq!(telemetry.turns, None);
        assert_eq!(telemetry.input_tokens, None);
        assert_eq!(telemetry.session_cost, None);
        assert_eq!(telemetry.context, None);
        telemetry.update_status(&json!({"usage":{"num_turns":0,"input_tokens":0},
            "total_cost_usd":null}));
        assert_eq!(telemetry.turns, Some(0));
        assert_eq!(telemetry.input_tokens, Some(0));
        assert_eq!(telemetry.output_tokens, None);
        assert_eq!(telemetry.session_cost, None);
    }

    #[test]
    fn bare_layout_commands_change_the_real_pane_state() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("s".into());
        assert!(app.layout(app.size).panes.is_none());

        app.input = "/vsplit".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.split, Split::Vertical);
        assert!(app.layout(app.size).panes.is_some());
        assert!(app.pending_prompts.is_empty());

        app.input = "/pane".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.active_group, 0);
        assert!(app.notice.contains("2 pane groups"));
        assert!(app.layout(app.size).panes.is_some());

        app.input = "/pane 2".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.active_group, 1);
        app.input = "/pane 1".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.active_group, 0);

        app.input = "/detach".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.should_quit);
    }

    #[test]
    fn detach_keeps_other_tabs_and_reopens_from_rail() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        for id in ["first", "second"] {
            app.apply_update(DaemonUpdate::Upsert(Session {
                id: id.into(), title: id.into(), collection: String::new(),
                transcript: String::new(), status: "Ready".into(),
            }));
        }
        app.groups[0].tabs.push("second".into());
        app.groups[0].active = 1;
        app.input = "/detach".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.groups[0].tabs, ["first"]);
        assert!(!app.should_quit);
        assert!(app.sessions.iter().any(|session| session.id == "second"));
        app.rail_selected = app.rail_order().iter().position(|index| app.sessions[*index].id == "second").unwrap();
        app.open_selected();
        assert_eq!(app.groups[0].active_id(), Some("second"));
    }

    #[test]
    fn detaching_last_tab_in_first_pane_preserves_other_pane_draft() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("first".into());
        app.groups[1].tabs.push("second".into());
        app.input_drafts.insert((1, "second".into()), ("other draft".into(), 11));
        app.input = "/detach".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(), Some("second"));
        assert!(app.groups[1].tabs.is_empty());
        assert_eq!(app.input, "other draft");
        assert!(!app.should_quit);
        assert!(!app.split_requested);
    }

    #[test]
    fn pane_sidebar_and_directory_command_forms_stay_local() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("first".into());
        app.session_cwds.insert("first".into(), PathBuf::from("/repo/project"));

        app.input = "/pane".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.active_group, 0);
        assert!(app.layout(app.size).panes.is_none());
        assert!(app.notice.contains("One pane group"));
        app.input = "/pane 2".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.input, "/pane 2");
        assert_eq!(app.active_group, 0);
        assert!(app.layout(app.size).panes.is_none());
        assert!(app.notice.contains("only one pane group"));
        app.input = "/vsplit".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));

        for (command, target) in [("/pane 2", 1), ("/pane 1", 0)] {
            app.input = command.into();
            app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
            assert_eq!(app.active_group, target);
        }
        app.input = "/sidebar width 32".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.rail_width, 32);
        assert!(app.rail_visible);
        app.input = "/sidebar off".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(!app.rail_visible);
        app.input = "/dir".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.notice.contains("/repo/project"));
        assert!(app.pending_prompts.is_empty());

        app.input = "/pane 3".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.input, "/pane 3");
        assert!(app.notice.contains("Usage: /pane"));
    }

    #[test]
    fn bracketed_paste_preserves_lines_sanitizes_controls_and_caps_bytes() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Paste("one\r\ntwo\rthree\u{1b}[31m\u{7}é".into()));
        assert_eq!(app.input, "one\ntwo\nthree[31mé");
        assert!(app.pending_prompts.is_empty());
        app.handle(Event::Paste("x".repeat(MAX_INPUT_BYTES).into()));
        assert_eq!(app.input.len(), MAX_INPUT_BYTES);
        assert!(app.notice.contains("truncated"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_prompts[0].1.len(), MAX_INPUT_BYTES);
    }

    #[test]
    fn pane_drafts_keep_multiline_cursor_and_modal_ignores_paste() {
        let mut app = App::default();
        app.groups[0].tabs.push("a".into());
        app.groups[1].tabs.push("b".into());
        app.handle(Event::Paste("a\nb".into()));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT)));
        app.handle(Event::Paste("other".into()));
        app.action_menu = true;
        assert!(!app.handle(Event::Paste("ignored".into())));
        app.action_menu = false;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('!'), KeyModifiers::NONE)));
        assert_eq!(app.input, "a\n!b");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT)));
        assert_eq!(app.input, "other");
    }

    #[test]
    fn empty_second_group_uses_one_pane_until_split_is_requested() {
        let mut app = App::default();
        app.handle(Event::Resize(118, 31));
        app.groups[0].tabs.push("first".into());
        assert!(app.layout(app.size).panes.is_none());

        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('v'), KeyModifiers::ALT)));
        assert!(app.layout(app.size).panes.is_some());

        let mut app = App::default();
        app.handle(Event::Resize(118, 31));
        app.groups[0].tabs.push("first".into());
        app.groups[1].tabs.push("second".into());
        assert!(app.layout(app.size).panes.is_some());

        app.groups[1].tabs.clear();
        assert!(app.layout(app.size).panes.is_none());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT)));
        assert_eq!(app.active_group, 1);
        assert!(app.layout(app.size).panes.is_some());

        let mut app = App::default();
        app.handle(Event::Resize(118, 31));
        assert!(app.layout(app.size).panes.is_none());
        app.handle(Event::Key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE)));
        assert!(app.diff_pane);
        assert!(app.layout(app.size).panes.is_some());
        app.handle(Event::Key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE)));
        assert!(!app.diff_pane);
        assert!(app.layout(app.size).panes.is_none());
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
    fn history_search_stays_above_active_prompt_and_mouse_opens_result() {
        let mut app = App::default();
        app.rail_visible = false;
        app.apply_update(DaemonUpdate::Upsert(Session { id: "alpha".into(), title: "First".into(),
            collection: "repo".into(), transcript: "red apple".into(), status: "Ready".into() }));
        app.apply_update(DaemonUpdate::Upsert(Session { id: "beta".into(), title: "Second".into(),
            collection: "repo".into(), transcript: "green pear".into(), status: "Ready".into() }));
        app.groups[0].tabs.push("alpha".into());
        app.groups[1].tabs.push("beta".into());
        app.handle(Event::Resize(100, 28));
        let panes = app.layout(app.size).panes.unwrap();
        let mut before = Terminal::new(TestBackend::new(100, 28)).unwrap();
        before.draw(|frame| app.draw(frame)).unwrap();

        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)));
        let menu = app.active_chooser_rect().unwrap();
        assert_eq!(menu.x, panes[0].x);
        assert_eq!(menu.width, panes[0].width);
        assert!(menu.bottom() < panes[0].bottom());
        let mut after = Terminal::new(TestBackend::new(100, 28)).unwrap();
        after.draw(|frame| app.draw(frame)).unwrap();
        for y in panes[1].y..panes[1].bottom() {
            for x in panes[1].x..panes[1].right() {
                assert_eq!(before.backend().buffer()[(x, y)], after.backend().buffer()[(x, y)]);
            }
        }
        assert_eq!(after.backend().buffer()[(menu.x + 2, menu.y + 2)].bg, theme::HIGHLIGHT);

        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 3, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.groups[0].active_id(), Some("beta"));
        assert!(!app.history_modal);
    }

    #[test]
    fn history_search_closes_outside_panel_and_on_narrow_pane_resize() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.open_history();
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.right(), row: menu.y + 2, modifiers: KeyModifiers::NONE }));
        assert!(!app.history_modal);
        app.open_history();
        app.handle(Event::Resize(100, 18));
        assert!(app.history_modal);
        app.handle(Event::Resize(100, 10));
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
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 1, modifiers: KeyModifiers::NONE }));
        assert!(app.engine_picker);
        assert!(!app.notice.contains("doxa-rs new"));
    }

    #[test]
    fn choosers_reserve_space_above_active_prompt_without_covering_other_pane() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.rail_visible = false;
        app.groups[0].tabs.push("a".into());
        app.groups[1].tabs.push("b".into());
        let before = painted(&app);
        app.open_engine_picker();
        let menu = app.active_chooser_rect().unwrap();
        let pane = app.layout(app.size).panes.unwrap()[0];
        assert_eq!(menu.x, pane.x);
        assert_eq!(menu.width, pane.width);
        assert!(menu.y > pane.y + 2);
        let after = painted(&app);
        let lines: Vec<_> = after.lines().collect();
        assert!(lines[usize::from(menu.y)].contains("New session"));
        assert!(lines[usize::from(menu.bottom())].contains("Engine"));
        assert!(lines[usize::from(menu.bottom() + 1)].contains("Prompt"));
        let old_lines: Vec<_> = before.lines().collect();
        for y in pane.y..pane.bottom() {
            assert_eq!(lines[usize::from(y)].chars().skip(50).collect::<String>(),
                old_lines[usize::from(y)].chars().skip(50).collect::<String>());
        }
        app.engine_picker = false;
        app.action_menu = true;
        assert_eq!(app.active_chooser_rect().unwrap().bottom(), menu.bottom());
        app.action_menu = false;
        app.lore_picker = Some(LorePicker { session_id: None, rows: vec![], selected: 0, query: String::new(),
            offset: 0, status: "Ready".into(), evidence: None, pending: None,
            proposals: Vec::new(), proposal_mode: false, review: None, review_scroll: 0,
            review_seen: 0, review_width: 0, armed_resolution: None,
            can_resolve: false, resolving: false, cwd: String::new(),
            belief_review: None, can_act_on_beliefs: false, belief_action: None,
            belief_note: String::new(), retract_armed: false, belief_acting: false,
            result_status: None });
        let lore = app.active_chooser_rect().unwrap();
        assert_eq!(lore.bottom(), menu.bottom());
        assert!(lore.height < menu.height, "empty LORE list should stay compact");
    }

    #[test]
    fn ask_user_choices_expand_above_prompt_and_beliefs_chip_uses_clicked_pane() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.rail_visible = false;
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"a", "engine":"codex"}));
        app.groups[0].tabs = vec!["a".into()];
        app.groups[1].tabs = vec!["b".into()];
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"a",
            "event":{"type":"needs_input", "data":{"id":"req-1", "kind":"ask_user",
                "title":"Choose target", "questions":[{"header":"Environment", "question":"Where?", "options":[
                    {"label":"Staging", "description":"Validate first"},
                    {"label":"Production", "description":"Release now"}]}]}}}));
        assert!(app.active_request_index().is_some());
        let menu = app.active_chooser_rect().unwrap();
        let screen = painted(&app);
        let rows: Vec<_> = screen.lines().collect();
        assert!(rows[usize::from(menu.y)].contains("Where?"));
        assert!(rows[usize::from(menu.y + 1)].contains("Environment"));
        assert!(!screen.contains("Header:"));
        assert!(!screen.contains("Question:"));
        assert!(!screen.contains("PgUp/PgDn scroll"));
        assert!(rows[usize::from(menu.y + 2)].contains("Staging"));
        assert!(rows[usize::from(menu.bottom() + 1)].contains("Prompt"));
        assert!(menu.height <= 10, "short question should use only its content rows");
        let mut terminal = Terminal::new(TestBackend::new(100, 28)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert_eq!(terminal.backend().buffer()[(menu.x + 2, menu.y + 2)].bg, theme::HIGHLIGHT);
        assert_eq!(terminal.backend().buffer()[(menu.right() - 3, menu.y + 2)].bg, theme::HIGHLIGHT);
        app.input_requests.clear();

        let pane = app.layout(app.size).panes.unwrap()[1];
        app.chip_offsets[1] = app.chips(1).iter().position(|(kind, _)| *kind == "beliefs").unwrap();
        assert!(app.chip_window(1, usize::from(pane.width)).iter().any(|(kind, _)| *kind == "beliefs"));
        let mut belief_x = pane.x;
        for (kind, label) in app.chip_window(1, usize::from(pane.width)) {
            if kind == "beliefs" { break; }
            belief_x += chip_text(kind, &label).width() as u16 + 1;
        }
        let chip_y = pane.bottom().saturating_sub(prompt_height("", pane.height) + 2);
        painted(&app);
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: belief_x + 2, row: chip_y, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.active_group, 1);
        assert!(app.lore_picker.is_some());
        assert_eq!(app.active_chooser_rect().unwrap().x, pane.x);
    }

    #[test]
    fn input_blink_ticks_at_bounded_interval_and_resets_when_resolved() {
        let mut app = App::default();
        let start = Instant::now();
        app.blink_at = start;
        assert!(!app.tick_blink(start));
        app.input_requests.push(InputRequest::from_event("a", &json!({
            "id":"req", "kind":"permission", "title":"Approve?"
        })).unwrap());
        assert!(!app.tick_blink(start + Duration::from_millis(649)));
        assert!(app.tick_blink(start + Duration::from_millis(650)));
        assert!(!app.blink_on);
        assert!(!app.tick_blink(start + Duration::from_millis(700)));
        app.input_requests[0].sending = true;
        assert!(app.tick_blink(start + Duration::from_millis(701)));
        assert!(app.blink_on);
        app.input_requests.clear();
        assert!(!app.tick_blink(start + Duration::from_millis(702)));
    }

    #[test]
    fn grouped_rail_blinks_only_waiting_session_and_clears_on_resolution() {
        let mut app = App::default();
        for (id, title, collection) in [
            ("first", "First", "A"),
            ("second", "Second", "A"),
            ("third", "Third session with long title", "B"),
        ] {
            app.apply_update(DaemonUpdate::Upsert(Session {
                id: id.into(), title: title.into(), collection: collection.into(),
                transcript: String::new(), status: "Ready".into(),
            }));
        }
        app.collections = vec![
            crate::collections::Collection { name:"A".into(), sessions:vec!["first".into(), "second".into()], collapsed:false },
            crate::collections::Collection { name:"B".into(), sessions:vec!["third".into()], collapsed:false },
        ];
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"third",
            "event":{"type":"needs_input", "data":{"id":"req", "kind":"ask_user",
                "title":"Choose", "questions":[]}}}));
        // Two collection headings precede the third session's row. Keep the
        // rail narrow enough to clip titles while retaining the attention cue.
        let mut terminal = Terminal::new(TestBackend::new(12, 9)).unwrap();
        let mut draw = |app: &App| {
            terminal.draw(|frame| app.draw_rail(frame, Rect::new(0, 0, 12, 9))).unwrap();
            let buffer = terminal.backend().buffer();
            (buffer[(3, 3)].bg, buffer[(3, 5)].bg)
        };
        assert_eq!(draw(&app), (theme::RAIL, theme::ERROR));
        assert!(app.tick_blink(app.blink_at + INPUT_BLINK_INTERVAL));
        assert_eq!(draw(&app), (theme::RAIL, theme::RAIL));
        assert!(app.tick_blink(app.blink_at + INPUT_BLINK_INTERVAL));
        assert_eq!(draw(&app), (theme::RAIL, theme::ERROR));
        app.input_requests[0].sending = true;
        assert_eq!(draw(&app), (theme::RAIL, theme::RAIL));
        app.input_requests[0].sending = false;
        assert_eq!(draw(&app), (theme::RAIL, theme::ERROR));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"third",
            "event":{"type":"needs_input_resolved", "data":{"id":"req"}}}));
        assert_eq!(draw(&app), (theme::RAIL, theme::RAIL));
    }

    #[test]
    fn collection_commands_move_active_session_and_order_rail() {
        let mut app = App::default();
        for id in ["one", "two"] {
            app.apply_update(DaemonUpdate::Upsert(Session {
                id:id.into(), title:id.into(), collection:"repo".into(),
                transcript:String::new(), status:"Ready".into(),
            }));
        }
        app.groups[0].tabs = vec!["one".into(), "two".into()];
        app.groups[0].active = 1;
        let before = crate::ui_state::LayoutSignature::capture(&app);
        app.input = "/collection add Work".into();
        assert!(app.submit_local_command());
        assert_ne!(before, crate::ui_state::LayoutSignature::capture(&app));
        assert!(app.input.is_empty());
        assert_eq!(app.collections[0].sessions, ["two"]);
        assert_eq!(app.rail_order(), [1, 0]);
        app.input = "/collection remove".into();
        assert!(app.submit_local_command());
        assert!(app.collections[0].sessions.is_empty());
        assert!(app.take_prompts().is_empty());
    }

    #[test]
    fn folded_collection_hides_members_and_rail_clicks_follow_visible_rows() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        for id in ["held", "loose"] {
            app.apply_update(DaemonUpdate::Upsert(Session { id:id.into(), title:id.into(),
                collection:String::new(), transcript:String::new(), status:"Ready".into() }));
        }
        app.collections.push(crate::collections::Collection {
            name:"Work".into(), sessions:vec!["held".into()], collapsed:false,
        });
        assert_eq!(app.rail_order(), [0, 1]);
        let click = |row| MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column:3, row, modifiers:KeyModifiers::NONE };
        assert!(app.mouse(click(1))); // Work heading
        assert!(app.collections[0].collapsed);
        assert_eq!(app.rail_order(), [1]);
        let before = app.groups[0].tabs.clone();
        assert!(app.mouse(click(2))); // Sessions heading, no session selected
        assert_eq!(app.groups[0].tabs, before);
        assert!(app.mouse(click(3))); // loose session
        assert_eq!(app.groups[0].active_id(), Some("loose"));
        assert!(app.mouse(click(1)));
        assert!(!app.collections[0].collapsed);
        assert_eq!(app.rail_order(), [0, 1]);
    }

    #[test]
    fn long_question_title_is_clipped_and_body_keeps_scrollable_text() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.rail_visible = false;
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"a", "engine":"codex"}));
        app.groups[0].tabs = vec!["a".into()];
        let question = "Which deployment target should receive the migration? ".repeat(30);
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"a",
            "event":{"type":"needs_input", "data":{"id":"req-long", "kind":"ask_user",
                "questions":[{"question":question.clone(), "options":[{"label":"Staging"}]}]}}}));
        let menu = app.active_chooser_rect().unwrap();
        assert_eq!(menu.height, 18);
        let rows: Vec<_> = painted(&app).lines().map(str::to_owned).collect();
        assert!(rows[usize::from(menu.y)].contains('…'));
        assert!(rows[usize::from(menu.y + 1)].contains("Which deployment"));
        let (body, _, _) = input_request_body(&app.input_requests[0], usize::from(menu.width.saturating_sub(4)));
        assert!(body.contains(&question));
        assert!(!body.contains("Question:"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)));
        assert!(app.input_requests[0].scroll > 0);
        assert!(painted(&app).contains("Prompt"));
    }

    #[test]
    fn offline_history_opens_read_only_and_never_queues_prompt() {
        let mut app = App::default();
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        tx.send(vec![history::OfflineSession { id: "saved-1".into(),
            project: "project\u{1b}[31m".into(), markdown: "**You:** saved".into(), search_snippets: Vec::new(), cwd: None }]).unwrap();
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
    fn repeated_history_results_keep_a_bounded_unopened_cache() {
        let mut app = App::default();
        for number in 0..80 {
            let (tx, rx) = mpsc::sync_channel(1);
            app.history_pending = Some(rx);
            tx.send(vec![history::OfflineSession { id: format!("archive-{number}"),
                project: "project".into(), markdown: "x".repeat(4096),
                search_snippets: Vec::new(), cwd: None }]).unwrap();
            assert!(app.poll_history());
        }
        assert_eq!(app.offline_ids.len(), 64);
        assert_eq!(app.history_entries.len(), 64);
        assert!(!app.offline_ids.contains("archive-0"));
        assert!(app.offline_ids.contains("archive-79"));
        app.groups[0].tabs.push("archive-79".into());
        for number in 80..160 {
            let (tx, rx) = mpsc::sync_channel(1);
            app.history_pending = Some(rx);
            tx.send(vec![history::OfflineSession { id: format!("archive-{number}"),
                project: "project".into(), markdown: "saved".into(),
                search_snippets: Vec::new(), cwd: None }]).unwrap();
            assert!(app.poll_history());
        }
        assert!(app.offline_ids.contains("archive-79"));
        assert!(app.offline_ids.len() <= 65);
    }

    #[test]
    fn pruning_archived_inventory_preserves_highlighted_session_identity() {
        for selected in [0, 20] {
            let mut app = App::default();
            for index in 0..80 {
                let id = format!("archive-{index:02}");
                app.offline_ids.insert(id.clone());
                app.sessions.push(Session { id: id.clone(), title: id, collection: "project".into(),
                    transcript: String::new(), status: "Archived".into() });
            }
            app.history_modal = true;
            app.history_selected = selected;
            let expected = app.sessions[selected].id.clone();
            app.prune_unopened_history();
            assert_eq!(app.sessions.len(), 64);
            assert_eq!(app.sessions[app.history_matches()[app.history_selected]].id, expected);
        }
    }

    #[test]
    fn empty_search_preserves_pending_recent_history_inventory() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        app.local_search("");
        assert!(app.history_modal);
        tx.send(vec![history::OfflineSession { id: "saved-empty-search".into(),
            project: "project".into(), markdown: "saved turn".into(),
            search_snippets: Vec::new(), cwd: None }]).unwrap();
        assert!(app.poll_history());
        assert!(app.sessions.iter().any(|session| session.id == "saved-empty-search"));
        assert!(app.history_query_due.is_none());
    }

    #[test]
    fn resume_prefix_uses_nonmodal_picker_and_never_reaches_model_prompt() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.input = "/resume saved".into();
        assert!(app.submit_local_command());
        assert!(app.input.is_empty());
        assert!(app.history_modal && app.history_resume);
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        tx.send(vec!["saved-1", "saved-2"].into_iter().map(|id| history::OfflineSession {
            id: id.into(), project: "project".into(), markdown: "**You:** old turn".into(), search_snippets: Vec::new(), cwd: None,
        }).collect()).unwrap();
        assert!(app.poll_history());
        assert_eq!(app.history_matches().len(), 2);
        assert!(painted(&app).contains("Resume session"));
        assert!(app.pending_prompts.is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(!app.history_modal);
        app.input = "/resume ../unsafe".into();
        assert!(app.submit_local_command());
        assert!(app.notice.contains("valid session ID"));
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn local_search_filters_saved_transcript_content() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        tx.send(vec![history::OfflineSession { id: "archive-1".into(), project: "project".into(),
            markdown: "**You:** hidden needle".into(), search_snippets: Vec::new(), cwd: None }]).unwrap();
        app.poll_history();
        app.input = "/search needle".into();
        assert!(app.submit_local_command());
        assert!(app.history_modal && !app.history_resume);
        assert_eq!(app.history_matches().len(), 1);
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn archive_raw_search_hit_survives_truncated_render_only_for_its_query() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.local_search("needle");
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        app.history_scan_query = Some("needle".into());
        tx.send(vec![history::OfflineSession { id: "old-archive".into(), project: "project".into(),
            markdown: "**You:** recent visible turn".into(), search_snippets: Vec::new(), cwd: None }]).unwrap();
        assert!(app.poll_history());
        assert_eq!(app.history_matches().len(), 1, "raw JSONL scan found an older hidden turn");
        app.history_query = "different".into();
        assert!(app.history_matches().is_empty(), "scan hit must not satisfy a different query");
    }

    #[test]
    fn live_search_debounces_and_discards_older_query_result() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.local_search("first");
        let due = app.history_query_due.unwrap();
        assert!(!app.start_due_history_query(due - Duration::from_millis(1)));
        app.history_key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
        assert_eq!(app.history_query, "firstx");
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        app.history_scan_query = Some("first".into());
        tx.send(vec![history::OfflineSession { id: "stale-1".into(), project: "project".into(),
            markdown: "old".into(), search_snippets: vec!["first".into()], cwd: None }]).unwrap();
        assert!(app.poll_history());
        assert!(!app.history_entries.contains_key("stale-1"));
        assert!(app.history_query_due.is_some());
    }

    #[test]
    fn indexed_excerpts_group_under_session_and_strip_terminal_controls() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.local_search("needle");
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        app.history_scan_query = Some("needle".into());
        tx.send(vec![history::OfflineSession { id: "saved-1".into(), project: "project".into(),
            markdown: "recent visible turn".into(),
            search_snippets: vec!["first [needle] \u{1b}[31m".into(), "second [needle]".into()], cwd: None }]).unwrap();
        assert!(app.poll_history());
        let rows = app.history_rows(8);
        assert_eq!(rows.len(), 3);
        assert!(rows[0].1);
        assert!(!rows[1].1 && !rows[2].1);
        assert!(rows[1].2.contains("first [needle]"));
        assert!(!rows[1].2.contains('\u{1b}'));
        assert!(painted(&app).contains("second [needle]"));
    }

    #[test]
    fn queue_picker_cancels_exact_selected_id_and_refuses_duplicate_ids() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.apply_update(DaemonUpdate::Upsert(Session { id: "session-1".into(), title: "Live".into(),
            collection: "repo".into(), transcript: String::new(), status: "Ready".into() }));
        app.groups[0].tabs.push("session-1".into());
        app.input = "/queue".into();
        assert!(app.submit_local_command());
        assert!(app.queue_picker.is_some());
        assert!(app.active_chooser_rect().is_some());
        assert!(matches!(app.pending_queue_commands.pop(), Some(crate::bridge::WorkerCommand::QueueList(id)) if id == "session-1"));
        app.apply_daemon_frame(&json!({"type":"queue_list_reply","session_id":"session-1","ok":true,
            "rows":[{"id":"q3","preview":"first scrubbed"},{"id":"q9","preview":"second scrubbed"}]}));
        assert!(painted(&app).contains("Prompt queue"));
        app.queue_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        app.queue_key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
        assert!(matches!(app.pending_queue_commands.pop(), Some(crate::bridge::WorkerCommand::QueueCancel(session, id))
            if session == "session-1" && id == "q9"));
        assert!(!app.apply_daemon_frame(&json!({"type":"queue_cancel_reply","session_id":"session-1","queue_id":"q3","ok":true})));
        assert_eq!(app.queue_picker.as_ref().unwrap().cancelling.as_deref(), Some("q9"));
        app.apply_daemon_frame(&json!({"type":"queue_cancel_reply","session_id":"session-1","queue_id":"q9","ok":true}));
        assert!(matches!(app.pending_queue_commands.pop(), Some(crate::bridge::WorkerCommand::QueueList(id)) if id == "session-1"));
        app.apply_daemon_frame(&json!({"type":"queue_list_reply","session_id":"session-1","ok":true,
            "rows":[{"id":"q3","preview":"a"},{"id":"q3","preview":"b"}]}));
        app.queue_key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
        assert!(app.pending_queue_commands.is_empty());
        assert!(app.notice.contains("Duplicate queue ID"));
        assert!(app.pending_prompts.is_empty());
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
        assert!(app.notice.contains("Enlarge active pane"));
        app.handle(Event::Resize(100, 28));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)));
        assert!(app.history_modal);
        app.handle(Event::Resize(27, 11));
        assert!(!app.history_modal);
    }

    #[test]
    fn diff_view_modal_renders_patch_colors() {
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
    fn diff_reject_waits_for_idle_turn_and_sends_bounded_reason_after_reverting() {
        let dir = tempfile::tempdir().unwrap();
        let git = |args: &[&str]| assert!(std::process::Command::new("git").args(args)
            .current_dir(dir.path()).status().unwrap().success());
        git(&["init", "-q"]);
        std::fs::write(dir.path().join("tracked.txt"), "old\n").unwrap();
        git(&["add", "tracked.txt"]);
        git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: baseline"]);
        std::fs::write(dir.path().join("tracked.txt"), "new\n").unwrap();
        let snapshot = diff_view::read(dir.path());
        let mut app = App::default();
        app.groups[0].tabs.push("session".into());
        app.diff_target = Some("session".into());
        app.diff_scroll = snapshot.rejectable[0].row;
        app.diff_snapshot = Some(snapshot);
        app.session_activity.insert("session".into(), (true, 0));
        app.begin_diff_reject();
        assert_eq!(app.diff_reject_confirm.as_ref().map(|draft| draft.index), Some(0));
        app.diff_modal = true;
        app.handle(Event::Paste("Please keep the old behavior\n".into()));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.diff_reject_confirm.is_none());
        assert_eq!(app.diff_reject_queue.len(), 1);
        assert!(app.diff_reject_pending.is_none());
        assert_eq!(std::fs::read_to_string(dir.path().join("tracked.txt")).unwrap(), "new\n");
        assert!(painted(&app).contains("queued"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::F(2), KeyModifiers::NONE)));
        assert!(app.diff_modal);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::CONTROL)));
        assert!(!app.should_quit);
        app.begin_diff_reject();
        app.confirm_diff_reject();
        assert_eq!(app.diff_reject_queue.len(), 1);
        assert!(app.notice.contains("already queued"));
        app.session_activity.insert("session".into(), (false, 0));
        assert!(app.poll_diff());
        assert!(app.diff_reject_pending.is_some());
        app.pending_prompts = vec![("other".into(), "waiting".into()); MAX_PENDING_PROMPTS];
        for _ in 0..100 {
            app.poll_diff();
            if app.diff_reject_pending.is_none() && app.diff_reject_feedback.is_some() { break; }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(app.diff_reject_pending.is_none());
        assert!(app.diff_reject_feedback.is_some());
        assert_eq!(app.pending_prompts.len(), MAX_PENDING_PROMPTS);
        app.pending_prompts.clear();
        assert!(app.poll_diff());
        assert!(app.diff_reject_feedback.is_none());
        assert_eq!(std::fs::read_to_string(dir.path().join("tracked.txt")).unwrap(), "old\n");
        assert_eq!(app.pending_prompts.len(), 1);
        assert_eq!(app.pending_prompts[0].0, "session");
        assert!(app.pending_prompts[0].1.contains("Do not re-apply"));
        assert!(app.pending_prompts[0].1.contains("Why: Please keep the old behavior"));
    }

    #[test]
    fn queued_diff_rejection_refuses_stale_patch_without_sending_feedback() {
        let dir = tempfile::tempdir().unwrap();
        let git = |args: &[&str]| assert!(std::process::Command::new("git").args(args)
            .current_dir(dir.path()).status().unwrap().success());
        git(&["init", "-q"]);
        std::fs::write(dir.path().join("tracked.txt"), "old\n").unwrap();
        git(&["add", "tracked.txt"]);
        git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: baseline"]);
        std::fs::write(dir.path().join("tracked.txt"), "new\n").unwrap();
        let snapshot = diff_view::read(dir.path());
        let mut app = App::default();
        app.groups[0].tabs.push("session".into());
        app.diff_target = Some("session".into());
        app.diff_scroll = snapshot.rejectable[0].row;
        app.diff_snapshot = Some(snapshot);
        app.session_activity.insert("session".into(), (true, 0));
        app.begin_diff_reject();
        app.handle(Event::Paste(format!("{}\n\u{1b}", "x".repeat(2000))));
        assert_eq!(app.diff_reject_confirm.as_ref().unwrap().reason.len(), MAX_REJECT_REASON_BYTES);
        app.confirm_diff_reject();
        assert_eq!(app.diff_reject_queue.len(), 1);
        std::fs::write(dir.path().join("tracked.txt"), "agent changed again\n").unwrap();
        app.session_activity.insert("session".into(), (false, 0));
        for _ in 0..100 {
            app.poll_diff();
            if app.diff_reject_pending.is_none() && app.diff_reject_queue.is_empty()
                && app.notice.contains("changed") { break; }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(std::fs::read_to_string(dir.path().join("tracked.txt")).unwrap(), "agent changed again\n");
        assert!(app.pending_prompts.is_empty());
        assert!(app.notice.contains("changed"));
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
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::ALT)));
        assert!(app.stop_confirmation.is_none());
        assert!(app.notice.contains("activity is unknown") || app.notice.contains("Load the diff"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE)));
        assert!(!app.diff_pane);
        assert!(painted(&app).contains("hidden transcript"));
    }

    #[test]
    fn diff_navigation_jumps_files_and_hunks_without_editing_prompt() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.diff_pane = true;
        app.diff_text = "Base: HEAD\ndiff --git a/one b/one\n@@ first\n line\n@@ second\ndiff --git a/two b/two\n@@ third".into();
        app.diff_files = vec![1, 5];
        app.diff_hunks = vec![2, 4, 6];
        app.input = "draft".into();
        let alt = KeyModifiers::ALT;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('j'), alt)));
        assert_eq!(app.diff_scroll, 2);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('j'), alt)));
        assert_eq!(app.diff_scroll, 4);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('n'), alt)));
        assert_eq!(app.diff_scroll, 5);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('k'), alt)));
        assert_eq!(app.diff_scroll, 4);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('b'), alt)));
        assert_eq!(app.diff_scroll, 1);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('b'), alt)));
        assert_eq!(app.diff_scroll, 1);
        assert!(app.notice.contains("No previous file"));
        assert_eq!(app.input, "draft");

        app.diff_modal = true;
        app.diff_scroll = 0;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE)));
        assert_eq!(app.diff_scroll, 1);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('j'), KeyModifiers::NONE)));
        assert_eq!(app.diff_scroll, 2);
        app.diff_hunks.push(70_000);
        app.diff_scroll = 6;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('j'), KeyModifiers::NONE)));
        assert_eq!(app.diff_scroll, 70_000);
        app.load_diff();
        assert!(app.diff_files.is_empty());
        assert!(app.diff_hunks.is_empty());
    }

    #[test]
    fn long_transcript_window_reaches_both_ends() {
        let lines: Vec<Line<'static>> = (0..70_000).map(|i| Line::from(i.to_string())).collect();
        let (window, top) = transcript_window(&lines, 8, 0, None);
        assert_eq!(window.len(), 8);
        assert_eq!(top, 69_992);
        assert_eq!(window[7].to_string(), "69999");
        let (window, top) = transcript_window(&lines, 8, 69_992, None);
        assert_eq!(top, 0);
        assert_eq!(window[0].to_string(), "0");
        assert_eq!(window[7].to_string(), "7");
    }

    #[test]
    fn transcript_scroll_crosses_u16_boundary_without_losing_lines() {
        let lines: Vec<Line<'static>> = (0..70_000).map(|i| Line::from(i.to_string())).collect();
        for scroll in [0, 1, 4_457, 65_535, 65_536, 69_991, 69_992] {
            let (window, _) = transcript_window(&lines, 8, scroll, None);
            assert_eq!(window.len(), 8);
            assert_eq!(window[0].to_string(), (69_992 - scroll).to_string());
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
    fn disconnected_attach_clears_marker_and_model_catalog_can_retry() {
        let (sender, receiver) = mpsc::sync_channel(1);
        drop(receiver);
        let mut app = App::default();
        app.attach_selected("session");
        assert!(dispatch_attaches(&mut app, &sender));
        assert!(app.attaching_ids.is_empty());
        app.attach_selected("session");
        assert_eq!(app.pending_attaches.len(), 1);
        app.model_picker = Some(ModelPicker { session_id: "session".into(), models: Vec::new(),
            selected: 0, note: "Loading".into(), loading: true, catalog_pending: false });
        app.pending_model_queries.push("session".into());
        assert!(dispatch_model_controls(&mut app, &sender));
        assert!(!app.model_picker.as_ref().unwrap().loading);
        assert!(app.model_picker_key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE)));
        assert_eq!(app.pending_model_queries, ["session"]);
    }

    #[test]
    fn restoring_saved_layout_clears_obsolete_archived_tab_notice() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = crate::ui_state::UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
        let mut app = App::default();
        app.groups[0].tabs.push("live".into());
        let mut saved = crate::ui_state::LayoutSignature::capture(&app);
        let complete = Mutex::new(true);
        app.offline_ids.insert("archive".into());
        app.groups[0].tabs.push("archive".into());
        assert!(save_layout_if_changed(&mut app, &mut store, &complete, &mut saved));
        assert!(app.notice.contains("archived tabs"));
        app.groups[0].tabs.pop();
        assert!(save_layout_if_changed(&mut app, &mut store, &complete, &mut saved));
        assert!(app.notice.is_empty());
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
    fn stream_heading_detection_respects_longer_outer_fences() {
        let prefix = "**You:**\n\nhello\n\n**Assistant:**\n\n";
        let source = format!("{prefix}````markdown\n\n```\n\n**Assistant:**\n\n```\n\n````\n\nend");
        assert_eq!(streamed_turn_start(&source), prefix.find("**Assistant:**"));
    }

    #[test]
    fn sending_answer_rows_do_not_select_options() {
        let request = InputRequest::from_event("a", &json!({"id":"r", "kind":"ask_user", "questions":[{"question":"Pick", "options":[{"label":"One"}]}]})).unwrap();
        let mut sending = request.clone();
        sending.sending = true;
        let menu = Rect::new(0, 0, 80, 15);
        for row in 1..14 { assert_eq!(ask_user_option_at(&sending, menu, row), None); }
        assert!((1..14).any(|row| ask_user_option_at(&request, menu, row) == Some(1)));
    }

    #[test]
    fn ended_session_clears_pending_input_and_answers() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"a"}));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"a",
            "event":{"type":"needs_input", "data":{"id":"request", "kind":"ask_user",
                "questions":[{"question":"Choose", "options":[{"label":"One"},{"label":"Two"}]}]}}}));
        app.pending_answers.push(("a".into(), "request".into(), json!({})));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"a", "event":{"type":"session_done", "data":{}}}));
        assert!(app.input_requests.is_empty());
        assert!(app.pending_answers.is_empty());
    }

    #[test]
    fn rejected_prompts_obey_input_bounds_and_unknown_streams_are_ignored() {
        let mut app = App::default();
        for kind in ["prompt_rejected", "prompt_uncertain"] {
            for text in ["x".repeat(MAX_INPUT_BYTES + 1), "bad\u{001b}draft".into()] {
                app.apply_daemon_frame(&json!({"type":kind,"session_id":"","text":text}));
            }
        }
        assert!(app.input.is_empty());
        assert!(app.rejected_drafts.is_empty());
        assert!(!app.append_reasoning("missing", &json!({"text":"secret"}), false));
        assert!(!app.append_event("missing", "tool_call", &json!({"name":"read"})));
        assert!(app.reasoning_streams.is_empty());
        assert!(app.streaming_text.is_empty());
    }

    #[test]
    fn finalized_session_preserves_selected_rail_identity() {
        let mut app = App::default();
        for id in ["a", "b", "c"] { app.apply_daemon_frame(&json!({"type":"hello", "session_id":id})); }
        app.rail_selected = app.rail_order().iter().position(|index| app.sessions[*index].id == "b").unwrap();
        app.apply_daemon_frame(&json!({"type":"clear_finalize_reply", "session_id":"a", "ok":true}));
        assert_eq!(app.sessions[app.rail_order()[app.rail_selected]].id, "b");
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

    #[test]
    fn streamed_chunks_stay_together_and_turns_have_separate_headings() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s"}));
        let event = |kind: &str, data: serde_json::Value| json!({"type":"event", "session_id":"s",
            "event":{"type":kind, "data":data}});
        app.apply_daemon_frame(&event("turn_started", json!({"prompt":"first\nquestion"})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":"answer"})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":" one"})));
        app.apply_daemon_frame(&event("turn_done", json!({"is_error":false})));
        app.apply_daemon_frame(&event("turn_started", json!({"prompt":"second"})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":"answer two"})));
        assert_eq!(app.sessions[0].transcript,
            "**You:**\n\nfirst\nquestion\n\n**Assistant:**\n\nanswer one\n\n**You:**\n\nsecond\n\n**Assistant:**\n\nanswer two");
    }

    #[test]
    fn tool_first_turn_gets_one_assistant_heading() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s"}));
        let event = |kind: &str, data: serde_json::Value| json!({"type":"event", "session_id":"s",
            "event":{"type":kind, "data":data}});
        app.apply_daemon_frame(&event("turn_started", json!({"prompt":"inspect"})));
        app.apply_daemon_frame(&event("tool_call", json!({"name":"Read", "input":{"path":"file.rs"}})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":"Found it."})));
        let transcript = &app.sessions[0].transcript;
        assert_eq!(transcript.matches("**Assistant:**").count(), 1);
        assert!(transcript.contains("Tool: Read started"));
        assert!(transcript.ends_with("Found it."));
    }

    #[test]
    fn restored_running_turn_gets_an_answer_boundary_without_replayed_start() {
        let mut app = App::default();
        app.apply_update(DaemonUpdate::Upsert(Session { id: "s".into(), title: "s".into(),
            collection: "repo".into(), transcript: "**You:**\n\nquestion\n\n".into(), status: "Running".into() }));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"s",
            "event":{"type":"text_delta", "data":{"text":"answer"}}}));
        assert!(app.sessions[0].transcript.ends_with("\n\n**Assistant:**\n\nanswer"));
    }

    #[test]
    fn restored_snapshot_keeps_its_existing_turn_headings() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s"}));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"s",
            "event":{"type":"text_delta", "data":{"text":"**You:**\n\nprior\n\n**Assistant:**\n\nreply\n\n",
                "snapshot":true}}}));
        assert_eq!(app.sessions[0].transcript, "**You:**\n\nprior\n\n**Assistant:**\n\nreply\n\n");
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"s",
            "event":{"type":"text_delta", "data":{"text":"continued"}}}));
        assert!(app.sessions[0].transcript.ends_with("**Assistant:**\n\ncontinued"));
    }

    #[test]
    fn live_tool_activity_stays_between_latest_response_and_spinner() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s"}));
        let event = |kind: &str, data: serde_json::Value| json!({"type":"event", "session_id":"s", "event":{"type":kind,"data":data}});
        app.apply_daemon_frame(&event("turn_started", json!({"prompt":"inspect"})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":"First response"})));
        app.apply_daemon_frame(&event("tool_call", json!({"name":"Read","input":{}})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":"Latest response"})));
        for expanded in [false, true] {
            if expanded { app.expanded_tool_sections.insert("s".into(), HashSet::from([0])); }
            let frame = painted(&app);
            assert!(frame.find("Latest response").unwrap() < frame.find("1 tool call").unwrap());
            assert!(frame.find("1 tool call").unwrap() < frame.find("Processing").unwrap());
        }
        let source = "**You:**\n\nInspect\n\nTool: Read started\n\n**Assistant:**\n\nAnswer";
        let mut cached = RenderedTranscript::render(0,"s", source, 80,None,None,0,&[]);
        let appended = format!("{source} continued");
        cached.update(&appended,80,None,None,0,&[]);
        let (fresh, sections) = transcript_tools::render(&appended,80,None,None);
        assert_eq!(cached.lines,fresh);
        assert_eq!(cached.sections,sections);
    }

    #[test]
    fn processing_spinner_advances_only_for_visible_busy_sessions() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "running":true}));
        let start = app.spinner_at;
        assert_eq!(app.activity_label("s"), Some("Processing"));
        assert!(painted(&app).contains("◐ Processing"));
        assert!(!app.tick_spinner(start + Duration::from_millis(119)));
        assert!(app.tick_spinner(start + SPINNER_INTERVAL));
        assert!(painted(&app).contains("◓ Processing"));
        app.session_activity.insert("s".into(), (false, 1));
        assert_eq!(app.activity_label("s"), Some("Queued"));
        assert!(painted(&app).contains("Queued"));
        app.session_activity.insert("s".into(), (false, 0));
        assert!(!app.tick_spinner(start + SPINNER_INTERVAL * 2));
        assert!(!painted(&app).contains("Processing"));
    }

    #[test]
    fn streamed_turn_cache_matches_full_render_after_each_append() {
        let mut source = "**You:**\n\nFirst\n\n**Assistant:**\n\nEarlier answer\n\n**You:**\n\nNext\n\n**Assistant:**\n\n".to_owned();
        let mut cached = RenderedTranscript::render(0, "s", &source, 38, None, None, 0, &[]);
        assert!(cached.turn_start.is_some());
        for chunk in ["A line", " with more text", "\n\nA second paragraph", "\n\n```rust\nfn main() {}\n```", "\n\nDone"] {
            source.push_str(chunk);
            cached.update(&source, 38, None, None, 0, &[]);
            let (expected, sections) = transcript_tools::render(&source, 38, None, None);
            assert_eq!(cached.lines, expected);
            assert_eq!(cached.sections, sections);
        }
    }

    #[test]
    fn streamed_turn_cache_preserves_prior_tool_sections() {
        let mut source = "**You:**\n\nInspect\n\n**Assistant:**\n\nTool: Read started · file.rs\n\nTool: Read finished · ok\n\n**You:**\n\nSummarize\n\n**Assistant:**\n\n".to_owned();
        let expanded = HashSet::from([0]);
        let mut cached = RenderedTranscript::render(0, "s", &source, 50, Some(&expanded), Some(0), 0, &[]);
        for chunk in ["Summary", " with more detail", "\n\nFinal paragraph"] {
            source.push_str(chunk);
            cached.update(&source, 50, Some(&expanded), Some(0), 0, &[]);
            let (expected, sections) = transcript_tools::render(&source, 50, Some(&expanded), Some(0));
            assert_eq!(cached.lines, expected);
            assert_eq!(cached.sections, sections);
        }
    }

    #[test]
    fn rendered_transcript_invalidates_on_width_and_expansion() {
        let source = "**Assistant:**\n\nTool: Read started · file.rs\n\nTool: Read finished · ok";
        let mut cached = RenderedTranscript::render(0, "s", source, 60, None, None, 0, &[]);
        let expanded = HashSet::from([0]);
        cached.update(source, 24, Some(&expanded), Some(0), 0, &[]);
        let (expected, sections) = transcript_tools::render(source, 24, Some(&expanded), Some(0));
        assert_eq!(cached.lines, expected);
        assert_eq!(cached.sections, sections);
        assert_eq!(cached.width, 24);
        assert_eq!(cached.expanded.as_ref(), Some(&expanded));
        let mut cards = ToolCards::default();
        cards.record("s", "tool_call", &json!({"id":"one","name":"Read","input":"file.rs"}));
        cards.record("s", "tool_result", &json!({"id":"one","name":"Read","result_summary":"updated"}));
        let identified = "**Assistant:**\n\nTool: Read started\u{001f}DOXA_TOOL_ID:\"one\"\n\nTool: Read finished\u{001f}DOXA_TOOL_ID:\"one\"";
        cached.update(identified, 24, Some(&expanded), Some(0), 1, cards.for_session("s"));
        let (expected, sections) = transcript_tools::render_with_cards(
            identified, 24, Some(&expanded), Some(0), cards.for_session("s"));
        assert_eq!(cached.lines, expected);
        assert_eq!(cached.sections, sections);
    }

    #[test]
    fn background_pane_update_keeps_other_panes_render_cache() {
        let mut app = App::default();
        app.handle(Event::Resize(140, 32));
        for id in ["left", "right"] {
            app.apply_daemon_frame(&json!({"type":"hello","session_id":id}));
        }
        app.groups[0].tabs = vec!["left".into()];
        app.groups[1].tabs = vec!["right".into()];
        app.active_group = 1;
        painted_at(&app, 140, 32);
        let (right_source, right_lines) = {
            let cache = app.rendered_transcripts.borrow();
            assert_eq!(cache.len(), 2);
            let right = cache.iter().find(|entry| entry.pane == 1).unwrap();
            (right.source.clone(), right.lines.as_ptr())
        };
        assert!(app.apply_daemon_frame(&json!({"type":"event","session_id":"left",
            "event":{"type":"text_delta","data":{"text":"Background update"}}})));
        painted_at(&app, 140, 32);
        let cache = app.rendered_transcripts.borrow();
        assert!(cache.iter().find(|entry| entry.pane == 0).unwrap().source.contains("Background update"));
        let right = cache.iter().find(|entry| entry.pane == 1).unwrap();
        assert_eq!(right.source, right_source);
        assert_eq!(right.lines.as_ptr(), right_lines);
        drop(cache);
        assert!(app.apply_daemon_frame(&json!({"type":"event","session_id":"left",
            "event":{"type":"tool_call","data":{"id":"call-1","name":"Read","input":"file.rs"}}})));
        painted_at(&app, 140, 32);
        let cache = app.rendered_transcripts.borrow();
        let right = cache.iter().find(|entry| entry.pane == 1).unwrap();
        assert_eq!(right.lines.as_ptr(), right_lines);
    }

    #[test]
    fn streamed_reasoning_counts_live_then_reveals_only_on_expand() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s"}));
        let event = |kind: &str, data: serde_json::Value| json!({"type":"event", "session_id":"s",
            "event":{"type":kind,"data":data}});
        app.apply_daemon_frame(&event("turn_started", json!({"prompt":"check"})));
        app.apply_daemon_frame(&event("reasoning_progress", json!({"approx_tokens":37})));
        let transcript = &app.sessions[0].transcript;
        let (live, sections) = transcript_tools::render(transcript, 80, None, None);
        assert_eq!(sections.len(), 1);
        assert!(live.iter().any(|line| line.to_string().contains("~37 tokens · receiving")));
        app.apply_daemon_frame(&event("reasoning_delta", json!({"text":"scrubbed thought","approx_tokens":42,"final":true})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":"Answer"})));
        app.apply_daemon_frame(&event("turn_done", json!({"is_error":false,"reasoning_output_tokens":7})));
        let transcript = &app.sessions[0].transcript;
        let (collapsed, _) = transcript_tools::render(transcript, 80, None, None);
        assert!(!collapsed.iter().any(|line| line.to_string().contains("scrubbed thought")));
        assert!(collapsed.iter().any(|line| line.to_string().contains("Thinking · 7 tokens")));
        assert!(!collapsed.iter().any(|line| line.to_string().contains("~7 tokens")));
        let (expanded, _) = transcript_tools::render(transcript, 80, Some(&HashSet::from([0])), None);
        assert!(expanded.iter().any(|line| line.to_string().contains("scrubbed thought")));
        assert!(expanded.iter().any(|line| line.to_string().contains("Answer")));
        app.handle(Event::Resize(100, 28));
        painted(&app);
        let hit = app.visible_tool_sections.borrow().iter()
            .find(|(_, _, session, section)| session == "s" && *section == 0)
            .map(|(rect, _, _, _)| *rect).unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: hit.x + 1, row: hit.y, modifiers: KeyModifiers::NONE }));
        assert!(app.expanded_tool_sections["s"].contains(&0));
    }

    #[test]
    fn lore_picker_keeps_prompt_and_displays_only_read_results() {
        assert_eq!(visible_raw_line("x\ty\u{0000}z"), "x\\ty\\0z");
        assert_eq!(raw_visual_rows("界界", 3), vec!["界", "界"]);
        let mut app = App { input: "unsent draft".into(), ..Default::default() };
        app.lore_picker = Some(LorePicker {
            session_id: None,
            query: String::new(), rows: Vec::new(), selected: 0, offset: 0,
            evidence: None, status: String::new(), pending: None,
            proposals: Vec::new(), proposal_mode: false, review: None, review_scroll: 0,
            review_seen: 0, review_width: 0, armed_resolution: None,
            can_resolve: false, resolving: false, cwd: String::new(),
            belief_review: None, can_act_on_beliefs: false, belief_action: None,
            belief_note: String::new(), retract_armed: false, belief_acting: false,
            result_status: None,
        });
        let (tx, rx) = mpsc::sync_channel(1);
        app.lore_picker.as_mut().unwrap().pending = Some(rx);
        tx.send(Ok(lore_picker::ResultPage::Beliefs(vec![lore_picker::Belief {
            id: 7, subject: "user".into(), claim: "safe".into(), truncated: false,
            confidence: 0.8, evidence_count: Some(1),
        }]))).unwrap();
        assert!(app.poll_lore());
        assert_eq!(app.lore_picker.as_ref().unwrap().rows[0].id, 7);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE)));
        assert_eq!(app.lore_picker.as_ref().unwrap().query, "r");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(app.lore_picker.is_none());
        assert_eq!(app.input, "unsent draft");
        assert!(app.pending_prompts.is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn belief_actions_require_full_exact_review_and_note_then_refresh_success() {
        let mut app = app_with_review();
        assert!(app.lore_picker.as_ref().unwrap().belief_review.as_ref().unwrap().claim().len() > 1200);
        assert!(painted(&app).contains("Exact belief #7"));
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        for _ in 0..100 {
            app.lore_picker_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
            let picker = app.lore_picker.as_ref().unwrap();
            let full = format!("Subject: {}\nClaim: {}", picker.belief_review.as_ref().unwrap().subject(),
                picker.belief_review.as_ref().unwrap().claim());
            if picker.review_seen == raw_visual_rows(&full, picker.review_width).len() { break; }
        }
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL));
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::ALT));
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::NONE));
        assert_eq!(app.lore_picker.as_ref().unwrap().belief_action, Some(doxa_lore::BeliefAction::Confirmed));
        app.lore_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().pending.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('o'), KeyModifiers::NONE));
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('k'), KeyModifiers::NONE));
        app.lore_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().resolving);
        assert!(app.lore_picker.as_ref().unwrap().pending.is_some());
        let (tx, rx) = mpsc::sync_channel(1);
        app.lore_picker.as_mut().unwrap().pending = Some(rx);
        tx.send(Ok(lore_picker::ResultPage::BeliefActed(doxa_lore::BeliefActionResult {
            status: doxa_lore::BeliefStatus::Active, retired: false,
            confirmed: 1, contradicted: 0, stale: 0,
        }))).unwrap();
        assert!(app.poll_lore());
        assert!(app.notice.contains("Belief action applied"));
        assert!(app.pending_queue_commands.iter().any(|command|
            matches!(command, crate::bridge::WorkerCommand::Status(id) if id == "s")));
        assert!(app.lore_picker.as_ref().unwrap().belief_review.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn belief_review_zero_height_cannot_scroll_or_lose_escape() {
        let mut app = app_with_review();
        app.sync_chooser_state();
        app.chooser_height_override.set(Some(5));
        app.lore_picker_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        let menu = app.active_chooser_rect().unwrap();
        app.mouse(MouseEvent { kind: MouseEventKind::ScrollDown,
            column: menu.x + 2, row: menu.y + 2, modifiers: KeyModifiers::NONE });
        assert_eq!(app.lore_picker.as_ref().unwrap().review_scroll, 0);
        assert_eq!(app.lore_picker.as_ref().unwrap().review_seen, 0);
        app.chooser_height_override.set(Some(0));
        app.lore_picker_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().belief_review.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn belief_action_completion_refreshes_its_original_session() {
        let mut app = app_with_review();
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"other","cwd":"/other"}));
        app.groups[0].tabs = vec!["s".into(), "other".into()];
        app.groups[0].active = 1;
        app.set_lore_memory_usage("s", 10, 100, 10, 100);
        app.set_lore_memory_usage("other", 20, 100, 20, 100);
        let (tx, rx) = mpsc::sync_channel(1);
        let picker = app.lore_picker.as_mut().unwrap();
        picker.pending = Some(rx);
        picker.resolving = true;
        picker.belief_acting = true;
        tx.send(Ok(lore_picker::ResultPage::BeliefActed(doxa_lore::BeliefActionResult {
            status: doxa_lore::BeliefStatus::Active, retired: false,
            confirmed: 1, contradicted: 0, stale: 0,
        }))).unwrap();
        assert!(app.poll_lore());
        assert!(!app.memory_cache.contains_key("s"));
        assert!(app.memory_cache.contains_key("other"));
        assert!(app.pending_queue_commands.iter().any(|command|
            matches!(command, crate::bridge::WorkerCommand::Status(id) if id == "s")));
        assert!(!app.pending_queue_commands.iter().any(|command|
            matches!(command, crate::bridge::WorkerCommand::Status(id) if id == "other")));
    }

    #[test]
    fn idle_memory_poll_does_not_force_redraw() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.session_cwds.insert("s".into(), PathBuf::from("/repo"));
        app.set_lore_memory_usage("s", 10, 100, 10, 100);
        assert!(!app.poll_memory());
        assert!(app.memory_pending.is_none());
    }

    #[test]
    fn memory_scope_change_redraws_even_when_usage_counts_are_equal() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.session_cwds.insert("s".into(), PathBuf::from("/repo"));
        app.set_lore_memory_usage("s", 10, 100, 10, 100);
        let usage = app.memory_cache["s"].0.clone().unwrap();
        let (tx, rx) = mpsc::sync_channel(1);
        app.memory_pending = Some(("s".into(), "/repo".into(), rx));
        tx.send(Some((usage, false))).unwrap();
        assert!(app.poll_memory());
        assert_eq!(app.memory_repo.get("s"), Some(&false));
    }

    #[cfg(unix)]
    #[test]
    fn lore_footer_hover_never_selects_an_offscreen_row() {
        let mut app = app_with_review();
        let picker = app.lore_picker.as_mut().unwrap();
        picker.belief_review = None;
        picker.rows = (1..=30).map(|id| lore_picker::Belief { id, subject: "subject".into(),
            claim: "claim".into(), truncated: false, confidence: 0.8, evidence_count: Some(1) }).collect();
        let menu = app.active_chooser_rect().unwrap();
        let footer = menu.y + 6 + menu.height.saturating_sub(8);
        app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: footer, modifiers: KeyModifiers::NONE });
        assert_eq!(app.lore_picker.as_ref().unwrap().selected, 0);
        let picker = app.lore_picker.as_mut().unwrap();
        picker.proposal_mode = true;
        picker.proposals = (1..=30).map(|index| lore_picker::Proposal { pid: index.to_string(),
            kind: "memory".into(), action: "add".into(), scope: "user".into(), summary: "summary".into() }).collect();
        let menu = app.active_chooser_rect().unwrap();
        let footer = menu.y + 4 + menu.height.saturating_sub(6);
        app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: footer, modifiers: KeyModifiers::NONE });
        assert_eq!(app.lore_picker.as_ref().unwrap().selected, 0);
    }

    #[cfg(unix)]
    #[test]
    fn compact_evidence_menu_keeps_the_truncation_notice_visible() {
        let mut app = app_with_review();
        let picker = app.lore_picker.as_mut().unwrap();
        picker.belief_review = None;
        picker.evidence = Some((7, (0..2).map(|_| lore_picker::Evidence {
            session_id: "session".into(), project: "repo".into(), note: "note".into(),
            created: "today".into(), source_engine: None, truncated: false, trail_truncated: true,
        }).collect()));
        let mut terminal = Terminal::new(TestBackend::new(100, 8)).unwrap();
        terminal.draw(|frame| app.draw_lore_picker(frame, Rect::new(0, 0, 100, 8))).unwrap();
        let buffer = terminal.backend().buffer();
        let screen = (0..8).map(|row| (0..100).map(|x| buffer[(x, row)].symbol())
            .collect::<String>()).collect::<Vec<_>>().join("\n");
        assert!(screen.contains("More evidence exists in LORE"));
    }

    #[test]
    fn disconnected_memory_menu_does_not_replace_another_sessions_content() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 28);
        app.groups[0].tabs.push("other".into());
        app.session_cwds.insert("s".into(), PathBuf::from("/repo"));
        app.session_cwds.insert("other".into(), PathBuf::from("/other"));
        app.show_memory_menu_fixture(0, &["Keep this"], &[], &[]);
        let expected = app.chip_info.as_ref().unwrap().lines.clone();
        let (tx, rx) = mpsc::sync_channel(1);
        app.memory_menu_pending = Some(("s".into(), "/repo".into(), rx));
        drop(tx);
        assert!(!app.poll_memory_menu());
        assert_eq!(app.chip_info.as_ref().unwrap().lines, expected);
    }

    #[cfg(unix)]
    #[test]
    fn retract_needs_separate_confirmation_and_changed_selection_disables_actions() {
        let mut app = app_with_review();
        for _ in 0..100 { app.lore_picker_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)); }
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE));
        assert_eq!(app.lore_picker.as_ref().unwrap().belief_action, Some(doxa_lore::BeliefAction::Retract));
        for c in "obsolete".chars() { app.lore_picker_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)); }
        app.lore_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().retract_armed);
        assert!(app.lore_picker.as_ref().unwrap().pending.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('!'), KeyModifiers::NONE));
        assert!(!app.lore_picker.as_ref().unwrap().retract_armed);
        app.lore_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().retract_armed);
        app.lore_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().pending.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE));
        for c in "obsolete".chars() { app.lore_picker_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)); }
        app.lore_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('Y'), KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().pending.is_some());
        app.lore_picker.as_mut().unwrap().pending = None;
        app.lore_picker.as_mut().unwrap().resolving = false;
        app.lore_picker.as_mut().unwrap().belief_acting = false;
        app.lore_picker.as_mut().unwrap().rows.push(lore_picker::Belief { id: 8, subject: "other".into(),
            claim: "other".into(), truncated: false, confidence: 0.7, evidence_count: Some(0) });
        app.lore_picker.as_mut().unwrap().selected = 1;
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::NONE));
        assert!(!app.lore_picker.as_ref().unwrap().can_act_on_beliefs);
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        assert!(app.lore_picker.as_ref().unwrap().status.contains("Selection changed"));
        app.lore_picker_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().belief_review.is_none());
    }

    #[test]
    fn belief_mouse_selection_only_requests_review_of_exact_clicked_row() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 40);
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","cwd":"/repo"}));
        app.open_lore_picker();
        let picker = app.lore_picker.as_mut().unwrap();
        picker.pending = None;
        picker.rows = [7, 8].into_iter().map(|id| lore_picker::Belief { id,
            subject: "fixture".into(), claim: "list text".into(), truncated: false,
            confidence: 0.8, evidence_count: Some(0) }).collect();
        let menu = app.active_chooser_rect().unwrap();
        let first = menu.y + if menu.height < 10 { 3 } else { 6 };
        let click = |row| MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 3, row, modifiers: KeyModifiers::NONE };
        assert!(app.mouse(click(first + 1)));
        assert_eq!(app.lore_picker.as_ref().unwrap().selected, 1);
        assert!(app.lore_picker.as_ref().unwrap().pending.is_none());
        assert!(app.mouse(click(first + 1)));
        assert!(app.lore_picker.as_ref().unwrap().pending.is_some());
        assert!(app.lore_picker.as_ref().unwrap().belief_review.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn belief_error_or_missing_capability_remains_read_only() {
        let mut app = app_with_review();
        app.lore_picker.as_mut().unwrap().can_act_on_beliefs = false;
        for _ in 0..100 { app.lore_picker_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)); }
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        let (tx, rx) = mpsc::sync_channel(1);
        let picker = app.lore_picker.as_mut().unwrap();
        picker.pending = Some(rx);
        picker.resolving = true;
        picker.belief_acting = true;
        tx.send(Err("Belief changed; reopen a fresh exact review")).unwrap();
        assert!(app.poll_lore());
        let picker = app.lore_picker.as_ref().unwrap();
        assert!(!picker.can_act_on_beliefs);
        assert!(picker.belief_review.is_some());
        assert!(picker.status.contains("Belief changed"));
        assert!(app.pending_queue_commands.is_empty());
        assert!(!app.notice.contains("applied"));
    }

    #[cfg(unix)]
    #[test]
    fn wheel_scroll_can_complete_exact_belief_review() {
        let mut app = app_with_review();
        let menu = app.active_chooser_rect().unwrap();
        for _ in 0..100 {
            app.mouse(MouseEvent { kind: MouseEventKind::ScrollDown,
                column: menu.x + 3, row: menu.y + 6, modifiers: KeyModifiers::NONE });
        }
        let picker = app.lore_picker.as_ref().unwrap();
        let review = picker.belief_review.as_ref().unwrap();
        let full = format!("Subject: {}\nClaim: {}", review.subject(), review.claim());
        assert_eq!(picker.review_seen, raw_visual_rows(&full, picker.review_width).len());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::NONE));
        assert_eq!(app.lore_picker.as_ref().unwrap().belief_action, Some(doxa_lore::BeliefAction::Stale));
    }

    #[cfg(unix)]
    #[test]
    fn proposal_action_requires_reading_to_end_then_explicit_arm() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let sidecar = dir.path().join("sidecar");
        std::fs::write(&sidecar, r#"#!/usr/bin/env python3
import hashlib, json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','pending_review_v1','resolve_reviewed_v1']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    raw = json.dumps({'kind':'memory','text':'x'*4000})
    value = {'pid':req['pid'],'raw':raw,'sha256':hashlib.sha256(raw.encode()).hexdigest(),'inode':11,'complete':True}
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"#).unwrap();
        let mut permissions = std::fs::metadata(&sidecar).unwrap().permissions();
        permissions.set_mode(0o700);
        std::fs::set_permissions(&sidecar, permissions).unwrap();
        let lore_picker::ResultPage::Review(review, can_resolve) = lore_picker::fetch(&sidecar,
            lore_picker::Query::Review("/repo".into(), "one".into())).unwrap() else { panic!("review") };
        assert!(can_resolve);
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 40);
        app.lore_picker = Some(LorePicker {
            session_id: None,
            query: String::new(), rows: Vec::new(), selected: 0, offset: 0,
            proposals: vec![lore_picker::Proposal { pid: "one".into(), kind: "memory".into(),
                action: "add".into(), scope: "user".into(), summary: String::new() }],
            proposal_mode: true, review: Some(review), review_scroll: 0,
            review_seen: 0, review_width: 0, armed_resolution: None,
            can_resolve, resolving: false, cwd: "/repo".into(),
            belief_review: None, can_act_on_beliefs: false, belief_action: None,
            belief_note: String::new(), retract_armed: false, belief_acting: false,
            result_status: None,
            evidence: None, status: String::new(), pending: None,
        });
        app.sync_chooser_state();
        app.chooser_height_override.set(Some(5));
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().armed_resolution.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        assert_eq!(app.lore_picker.as_ref().unwrap().review_scroll, 0);
        assert_eq!(app.lore_picker.as_ref().unwrap().review_seen, 0);
        app.chooser_height_override.set(None);
        for _ in 0..200 {
            if app.lore_picker.as_ref().unwrap().review_seen ==
                raw_visual_rows(app.lore_picker.as_ref().unwrap().review.as_ref().unwrap().raw(),
                    app.lore_picker.as_ref().unwrap().review_width).len() { break; }
            app.lore_picker_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        }
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::CONTROL));
        assert!(app.lore_picker.as_ref().unwrap().armed_resolution.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::ALT));
        assert!(app.lore_picker.as_ref().unwrap().armed_resolution.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE));
        assert_eq!(app.lore_picker.as_ref().unwrap().armed_resolution, Some(doxa_lore::PendingDecision::Approve));
        assert!(app.lore_picker.as_ref().unwrap().pending.is_none());
        app.lore_picker.as_mut().unwrap().armed_resolution = None;
        app.lore_picker.as_mut().unwrap().can_resolve = false;
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().armed_resolution.is_none());
        app.chooser_height_override.set(Some(0));
        app.lore_picker_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().review.is_none());
    }

    #[test]
    fn pending_local_command_opens_proposals_without_sending_prompt() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 40);
        app.groups[0].tabs.push("session-1".into());
        app.input = " /pending ".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.lore_picker.as_ref().unwrap().proposal_mode);
        assert!(app.input.is_empty());
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn branch_command_targets_active_daemon_and_never_becomes_a_prompt() {
        let mut app = App::default();
        app.groups[0].tabs.push("session-1".into());
        app.input = "/branch feature".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(matches!(app.pending_queue_commands.pop(),
            Some(crate::bridge::WorkerCommand::Branch(id, Some(name)))
                if id == "session-1" && name == "feature"));
        assert!(app.pending_prompts.is_empty());
        assert!(app.input.is_empty());
        app.apply_daemon_frame(&json!({"type":"branch_reply", "session_id":"session-1",
            "ok":false,"error":"session is busy"}));
        assert!(app.notice.contains("session is busy"));
    }

    #[test]
    fn branch_listing_opens_bounded_picker_and_enter_targets_active_session() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 35);
        app.groups[0].tabs.push("session-1".into());
        app.input = "/branch".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(matches!(app.pending_queue_commands.pop(),
            Some(crate::bridge::WorkerCommand::Branch(id, None)) if id == "session-1"));
        assert!(app.apply_daemon_frame(&json!({"type":"branch_reply", "session_id":"session-1",
            "ok":true,"base":"main","branches":["feature","main","bad\nname"]})));
        assert_eq!(app.branch_picker.as_ref().unwrap().branches, ["feature", "main"]);
        assert_eq!(app.branch_picker.as_ref().unwrap().selected, 1);
        assert!(app.active_chooser_rect().is_some());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.branch_picker.is_none());
        assert!(matches!(app.pending_queue_commands.pop(),
            Some(crate::bridge::WorkerCommand::Branch(id, Some(name)))
                if id == "session-1" && name == "feature"));
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn branch_picker_cancel_and_stale_reply_do_not_switch_checkout() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 35);
        app.groups[0].tabs.push("session-1".into());
        assert!(!app.apply_daemon_frame(&json!({"type":"branch_reply", "session_id":"other",
            "ok":true,"base":"main","branches":["feature"]})));
        assert!(app.branch_picker.is_none());
        app.apply_daemon_frame(&json!({"type":"branch_reply", "session_id":"session-1",
            "ok":true,"base":"main","branches":["feature","main"]}));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(app.branch_picker.is_none());
        assert!(app.pending_queue_commands.is_empty());
    }

    #[test]
    fn branch_picker_mouse_selects_row_and_click_outside_cancels() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 35);
        app.groups[0].tabs.push("session-1".into());
        let list = json!({"type":"branch_reply", "session_id":"session-1",
            "ok":true,"base":"main","branches":["feature","main"]});
        app.apply_daemon_frame(&list);
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 2, modifiers: KeyModifiers::NONE }));
        assert!(matches!(app.pending_queue_commands.pop(),
            Some(crate::bridge::WorkerCommand::Branch(id, Some(name)))
                if id == "session-1" && name == "feature"));
        app.apply_daemon_frame(&list);
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x, row: menu.y.saturating_sub(1), modifiers: KeyModifiers::NONE }));
        assert!(app.branch_picker.is_none());
        assert!(app.pending_queue_commands.is_empty());
    }

    #[test]
    fn attach_selection_queues_new_tab_and_focuses_existing_tab() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"current", "model":"model"}));
        app.input = "/attach detached".into();
        app.attach_selected("detached");
        assert_eq!(app.pending_attaches, [("detached".into(), 0)]);
        assert_eq!(app.groups[0].active_id(), Some("current"));
        assert!(app.pending_prompts.is_empty());
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"detached", "model":"other"}));
        app.apply_daemon_frame(&json!({"type":"attach_reply", "ok":true,
            "session_id":"detached", "group":0}));
        assert_eq!(app.groups[0].active_id(), Some("detached"));
        assert_eq!(app.groups[0].tabs, ["current", "detached"]);
        app.groups[0].active = 0;
        app.attach_selected("detached");
        assert_eq!(app.groups[0].active_id(), Some("detached"));
        assert_eq!(app.groups[0].tabs.len(), 2);
    }

    #[test]
    fn attach_picker_filters_titles_and_ids_without_sending_a_prompt() {
        let row = |id: &str, title: &str| crate::discovery::Session {
            id: id.into(), title: title.into(), socket: PathBuf::new(),
            scope_key: String::new(), clients: None, started_at: String::new(),
        };
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 30);
        app.attach_picker = Some(AttachPicker { rows: vec![row("abc123", "Alpha work"),
            row("def456", "Beta work")], query: String::new(), selected: 0 });
        assert!(app.active_chooser_rect().is_some());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('b'), KeyModifiers::NONE)));
        assert_eq!(app.attach_matches(), [1]);
        assert!(app.pending_prompts.is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
        assert_eq!(app.attach_picker.as_ref().unwrap().selected, 1);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(app.attach_picker.is_none());
        assert!(app.pending_attaches.is_empty());
        assert!(attach_matches(&row("id123", "Release planning"), "plan"));
        assert!(attach_matches(&row("id123", "Release planning"), "ID1"));
        app.attach_picker = Some(AttachPicker { rows: vec![row("definitely-not-live-attach-test", "Gone")],
            query: String::new(), selected: 0 });
        app.open_selected_attach();
        assert!(app.attach_picker.is_none());
        assert!(app.pending_attaches.is_empty());
        assert!(app.notice.starts_with("attach:"));
    }

    #[test]
    fn rename_pins_label_until_cleared_and_slash_commands_never_queue_prompts() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "model":"old"}));
        app.input = "/rename A useful name".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.custom_names.get("s").map(String::as_str), Some("A useful name"));
        assert_eq!(app.sessions[0].title, "A useful name");
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"s",
            "event":{"type":"model_changed", "data":{"model":"new"}}}));
        assert_eq!(app.sessions[0].title, "A useful name");
        app.input = "/rename".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.custom_names.is_empty());
        assert_eq!(app.sessions[0].title, "new");
        for command in ["/attach bad/id", "/doctor", "/pending unsupported"] {
            app.input = command.into();
            app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
            assert!(app.notice.contains("attach:") || app.notice.contains("unavailable"));
            assert!(app.pending_prompts.is_empty());
        }
    }

    #[test]
    fn partial_lore_archive_failure_stays_visible_and_never_retries() {
        let mut app = App::default();
        app.open_pending_picker();
        let (tx, rx) = mpsc::sync_channel(1);
        let picker = app.lore_picker.as_mut().unwrap();
        picker.pending = Some(rx);
        picker.resolving = true;
        tx.send(Ok(lore_picker::ResultPage::Resolved(
            doxa_lore::PendingResolution::Refused { code: "archive_failed".into(), applied: true }))).unwrap();
        assert!(app.poll_lore());
        assert!(app.notice.contains("do not retry automatically"));
        assert!(app.lore_picker.as_ref().unwrap().pending.is_none());
        assert!(!app.lore_picker.as_ref().unwrap().resolving);
    }
}
