//! Terminal shell for the Rust frontend. Daemon adapters can feed [`App::apply_update`].
mod actions;
mod commands;
mod diff_controller;
pub(crate) mod fleet_menu;
mod fleet_process;
mod history_controller;
mod interaction;
mod layout;
mod lore_controller;
mod model_controls;
mod operations_controller;
mod operations_menu;
pub(crate) mod panes;
mod render;
mod session_controls;
mod session_navigation;
mod terminal_loop;
use render::context_detail_lines;
#[cfg(test)]
use render::transcript_window;
#[cfg(test)]
use terminal_loop::*;
pub use terminal_loop::{
    run, run_with_channels, run_with_channels_state, run_with_channels_state_guarded,
    run_with_frames, run_with_worker_channels, run_with_worker_channels_state_guarded,
};
mod session_events;
mod session_telemetry;
mod transcript_events;

use session_telemetry::SessionTelemetry;
use transcript_events::{append_transcript, transcript_tail, ReasoningStream};

use std::cell::{Cell, RefCell};
use std::collections::{HashMap, HashSet, VecDeque};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicUsize;
use std::sync::mpsc::Receiver;
use std::sync::Arc;
#[cfg(test)]
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crossterm::event::KeyCode;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Line;
use ratatui::widgets::{Block, Borders, Paragraph, Widget, Wrap};
#[cfg(test)]
use ratatui::Terminal;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::theme;
use crate::{diff_view, history, launch, lore_picker, markdown, peer_map::PeerMap};
use doxa_engines::EngineCapabilities;

mod links;
mod tool_cards;
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

use commands::COMMANDS;

const ENGINE_CHOICES: [&str; 4] = ["codex", "claude", "deepseek", "glm"];
// Fallback model IDs measured from the vendors' catalogues in Python 1.19.
// Unknown models get no effort choices until a verified capability arrives.
const DEEPSEEK_MODELS: [&str; 2] = ["deepseek-flash", "deepseek-v4-pro"];
const GLM_MODELS: [&str; 10] = [
    "glm-4.5",
    "glm-4.5-air",
    "glm-4.6",
    "glm-4.7",
    "glm-5",
    "glm-5-turbo",
    "glm-5.1",
    "glm-5.2",
    "glm-5.3",
    "glm-5.3-flash",
];

fn vendor_models(engine: launch::Engine) -> &'static [&'static str] {
    match engine {
        launch::Engine::DeepSeek => &DEEPSEEK_MODELS,
        launch::Engine::Glm => &GLM_MODELS,
        _ => &[],
    }
}
fn vendor_default_model(engine: launch::Engine) -> &'static str {
    match engine {
        launch::Engine::DeepSeek => "deepseek-flash",
        launch::Engine::Glm => "glm-5.3-flash",
        _ => "",
    }
}
fn effort_choices(engine: &str, model: &str) -> &'static [&'static str] {
    match engine {
        "deepseek" => doxa_vendors::Vendor::DeepSeek.effort_choices(model),
        "glm" => doxa_vendors::Vendor::Glm.effort_choices(model),
        _ => &[],
    }
}
fn engine_name(engine: launch::Engine) -> &'static str {
    match engine {
        launch::Engine::Codex => "codex",
        launch::Engine::Claude => "claude",
        launch::Engine::DeepSeek => "deepseek",
        launch::Engine::Glm => "glm",
        launch::Engine::Fixture => "fixture",
    }
}
fn new_session_preferences(
    engine: launch::Engine,
    config: &toml::Table,
    model_override: Option<&str>,
    effort_override: Option<&str>,
) -> (String, Option<String>) {
    let configured_model = crate::settings::raw_from(
        config,
        crate::settings::find("model").unwrap(),
        model_override,
        engine_name(engine),
    );
    let model = if configured_model.is_empty() {
        vendor_default_model(engine).to_owned()
    } else {
        configured_model
    };
    let configured_effort = crate::settings::raw_from(
        config,
        crate::settings::find("effort").unwrap(),
        effort_override,
        engine_name(engine),
    );
    let vendor = !vendor_models(engine).is_empty();
    let effort = (!configured_effort.is_empty())
        .then_some(configured_effort)
        .filter(|level| {
            !vendor || effort_choices(engine_name(engine), &model).contains(&level.as_str())
        })
        .or_else(|| vendor.then(|| "high".into()));
    (model, effort)
}

const PERMISSION_CHOICES: [(&str, &str); 5] = [
    ("default", "Ask before dangerous calls"),
    ("acceptEdits", "Allow file edits; ask for other calls"),
    ("plan", "Planning only; no tools run"),
    ("auto", "Model classifier decides each call"),
    ("dontAsk", "Deny unapproved calls without asking"),
];

fn permission_index(mode: &str) -> Option<usize> {
    PERMISSION_CHOICES
        .iter()
        .position(|(candidate, _)| *candidate == mode)
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

impl ModelPicker {
    fn row_offset(&self) -> u16 {
        3 + u16::from(self.catalog_pending)
    }
    fn visible_rows(&self, height: u16) -> usize {
        usize::from(height.saturating_sub(self.row_offset() + 1)).max(1)
    }
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
    let start = if selected < start {
        selected
    } else if selected >= start.saturating_add(visible) {
        selected.saturating_sub(visible.saturating_sub(1))
    } else {
        start
    };
    view_start.set(start);
    start
}

fn chooser_list_lines(lines: Vec<Line<'static>>, width: usize) -> Vec<Line<'static>> {
    lines
        .into_iter()
        .map(|line| {
            let text: String = line
                .spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect();
            Line::styled(clipped_title(&text, width).0, line.style)
        })
        .collect()
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
    belief_intent: Option<doxa_lore::BeliefAction>,
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

#[derive(Debug, Clone)]
struct NewSession {
    engine: launch::Engine,
    model: String,
    models: Vec<String>,
    model_efforts: HashMap<String, Vec<String>>,
    catalog_note: String,
    catalog_pending: bool,
    launch_error: Option<String>,
    retry_allowed: bool,
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
struct EffortPicker {
    session_id: String,
    engine: String,
    model: String,
    levels: Vec<String>,
    selected: usize,
}

#[derive(Debug, Clone)]
struct QueueRow {
    id: String,
    preview: String,
}

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
    rows: Vec<crate::settings::Row>,
    selected: usize,
    category: usize,
    draft: Option<(String, String)>,
    edits: HashMap<String, Option<String>>,
    engine: String,
}
impl SettingsMenu {
    fn indices(&self) -> Vec<usize> {
        self.rows
            .iter()
            .enumerate()
            .filter(|(_, r)| r.setting.category == crate::settings::CATEGORIES[self.category])
            .map(|(i, _)| i)
            .collect()
    }
    fn visible_indices(&self, height: u16) -> Vec<usize> {
        let indices = self.indices();
        let count = usize::from(height.saturating_sub(if height < 12 { 5 } else { 9 })).max(1);
        let position = indices
            .iter()
            .position(|i| *i == self.selected)
            .unwrap_or(0);
        let start = position.saturating_sub(count.saturating_sub(1));
        indices.into_iter().skip(start).take(count).collect()
    }
    fn finish_draft(&mut self) {
        if let Some((key, value)) = self.draft.take() {
            self.edits.insert(key, Some(value));
        }
    }
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
        Style::default()
            .fg(theme::ACCENT)
            .bg(theme::HIGHLIGHT)
            .add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(theme::SECONDARY)
    }
}

// Only complete buttons are rendered and clickable, including in narrow terminals.
fn belief_buttons(
    area: Rect,
    y: u16,
    buttons: &[(&'static str, KeyCode)],
) -> Vec<(Rect, &'static str, KeyCode)> {
    let mut x = area.x.saturating_add(1);
    let right = area.right().saturating_sub(1);
    if y <= area.y || y >= area.bottom().saturating_sub(1) {
        return Vec::new();
    }
    let mut result = Vec::new();
    for &(label, key) in buttons {
        let width = label.len() as u16;
        if x.saturating_add(width) > right {
            break;
        }
        result.push((Rect::new(x, y, width, 1), label, key));
        x = x.saturating_add(width + 1);
    }
    result
}

fn belief_review_buttons(area: Rect, picker: &LorePicker) -> Vec<(Rect, &'static str, KeyCode)> {
    let buttons: &[(&str, KeyCode)] = if picker.belief_action.is_some() {
        if picker.retract_armed {
            &[
                ("[Confirm reject]", KeyCode::Char('y')),
                ("[Cancel]", KeyCode::Esc),
            ]
        } else {
            &[("[Apply]", KeyCode::Enter), ("[Cancel]", KeyCode::Esc)]
        }
    } else {
        &[
            ("[Accept A]", KeyCode::Char('A')),
            ("[Reject R]", KeyCode::Char('R')),
            ("[Contradicted X]", KeyCode::Char('x')),
            ("[Stale S]", KeyCode::Char('s')),
        ]
    };
    belief_buttons(area, area.y.saturating_add(5), buttons)
}

fn repo_chip(status: &doxa_worktrees::RepoStatus) -> (&'static str, String) {
    match status {
        doxa_worktrees::RepoStatus::Directory { name } => {
            ("directory", format!("dir {}", safe_label(name)))
        }
        doxa_worktrees::RepoStatus::Repository {
            repo,
            base,
            checked_out,
            sha,
            worktree,
        } => {
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
            if let Some(sha) = sha.as_ref().filter(|sha| {
                !base
                    .as_deref()
                    .or(checked_out.as_deref())
                    .is_some_and(|branch| branch.starts_with(sha.as_str()))
            }) {
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
    query.is_empty()
        || session.id.to_lowercase().starts_with(&query)
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
                    if let Some(last) = out.pop() {
                        used -= last.width().unwrap_or(0);
                    } else {
                        break;
                    }
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
    (path.is_dir() && label.len() <= 4096 && !label.chars().any(unsafe_input_char)).then_some(path)
}

fn repo_directory_entries(current: &Path) -> Vec<PathBuf> {
    // Directory enumeration is bounded so a huge worktree never stalls the UI.
    let mut paths = vec![current.to_path_buf()];
    if let Some(parent) = current.parent().and_then(safe_repo_directory) {
        if parent != current {
            paths.push(parent);
        }
    }
    let mut children: Vec<_> = std::fs::read_dir(current)
        .into_iter()
        .flatten()
        .take(128)
        .filter_map(Result::ok)
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
            return if relative.as_os_str().is_empty() {
                "~".into()
            } else {
                format!("~/{}", relative.display())
            };
        }
    }
    path.display().to_string()
}

fn prompt_height(draft: &str, pane_height: u16) -> u16 {
    (draft.bytes().filter(|b| *b == b'\n').count().min(5) as u16 + 3)
        .clamp(3, 8)
        .min(pane_height.saturating_sub(5).max(3))
}

fn wrapped_rows(text: &str, width: usize) -> usize {
    let width = width.max(1);
    text.lines()
        .map(|line| line.width().max(1).div_ceil(width))
        .sum::<usize>()
        .max(1)
}

fn chip_text(kind: &str, label: &str) -> String {
    if kind == "more" {
        format!(" {label} › ")
    } else if kind == "effort" && label == "?" {
        format!(" {label} ")
    } else if matches!(
        kind,
        "engine" | "model" | "effort" | "permission" | "beliefs"
    ) {
        format!(" {label} ▾ ")
    } else {
        format!(" {label} ")
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Session {
    pub id: String,
    pub title: String,
    pub collection: String,
    pub transcript: String,
    pub status: String,
}

// LORE owns both curated-memory lengths and their separate scope caps.
fn memory_fill_percent(chars: u64, cap_chars: u64) -> u64 {
    if cap_chars == 0 {
        return 0;
    }
    chars.saturating_mul(100).saturating_add(cap_chars / 2) / cap_chars
}
fn memory_fill_label(chars: u64, cap_chars: u64) -> String {
    if cap_chars == 0 {
        format!("{chars}/0")
    } else {
        format!("{}%", memory_fill_percent(chars, cap_chars))
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
    Tabs,
    Chip(&'static str),
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
    NestedPane(panes::Divider),
    Chooser,
}

#[derive(Clone)]
struct PaneLayout {
    outer: Rect,
    rail: Option<Rect>,
    body: Rect,
    panes: Option<Vec<Rect>>,
}

#[derive(Clone, Debug)]
pub struct QuestionOption {
    pub label: String,
    pub description: String,
}

#[derive(Clone, Debug)]
pub struct InputQuestion {
    pub id: Option<String>,
    pub is_other: bool,
    pub question: String,
    pub header: String,
    pub options: Vec<QuestionOption>,
}

#[derive(Clone, Debug)]
pub struct InputRequest {
    pub session_id: String,
    pub id: String,
    original_payload: serde_json::Value,
    pub kind: String,
    pub heading: String,
    pub questions: Vec<InputQuestion>,
    pub step: usize,
    pub selected: usize,
    pub answers: serde_json::Map<String, serde_json::Value>,
    pub sending: bool,
    pub require_full_review: bool,
    review_available: bool,
    review_seen: Cell<usize>,
    review_complete: Cell<bool>,
    pub free_text: String,
    pub free_cursor: usize,
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
            if items.is_empty()
                || items.len() > 32
                || items.iter().any(|q| {
                    q["isSecret"] == true
                        || q["id"].as_str().is_some_and(|id| {
                            id.is_empty() || id.len() > 200 || id.chars().any(char::is_control)
                        })
                        || q["options"]
                            .as_array()
                            .is_some_and(|options| options.len() > 64)
                })
            {
                return None;
            }
            items
                .iter()
                .map(|q| InputQuestion {
                    id: q["id"].as_str().map(str::to_owned),
                    is_other: q["isOther"] == true || q["is_other"] == true,
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
            original_payload: data.clone(),
            kind: kind.into(),
            heading,
            questions,
            step: 0,
            selected: 1,
            answers: serde_json::Map::new(),
            sending: false,
            require_full_review: data["require_full_review"] == true,
            review_available: data["input_summary"].as_str().is_some()
                && data["input_summary_truncated"] != true,
            review_seen: Cell::new(0),
            review_complete: Cell::new(false),
            free_text: String::new(),
            free_cursor: 0,
            scroll: 0,
        })
    }

    fn freeform(&self) -> bool {
        self.kind == "ask_user"
            && self.questions.get(self.step).is_some_and(|question| {
                question.options.is_empty()
                    || question.is_other && self.selected > question.options.len()
            })
    }

    fn option_count(&self) -> usize {
        self.questions
            .get(self.step)
            .map(|q| q.options.len() + usize::from(q.is_other && !q.options.is_empty()))
            .unwrap_or(0)
    }
}

fn input_request_body(
    request: &InputRequest,
    title_width: usize,
) -> (String, Option<usize>, Vec<usize>) {
    let mut body = String::new();
    let mut selected_row = None;
    let mut option_rows = Vec::new();
    if request.kind == "ask_user" {
        if let Some(question) = request.questions.get(request.step) {
            if !question.header.is_empty() {
                body.push_str(&markdown::sanitize(&question.header));
                body.push('\n');
            }
            if question.question.chars().count() > title_width {
                body.push_str(&markdown::sanitize(&question.question));
                body.push_str("\n\n");
            }
            for (i, option) in question.options.iter().enumerate() {
                option_rows.push(body.lines().count());
                if i + 1 == request.selected {
                    selected_row = Some(body.lines().count());
                }
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
            if question.is_other && !question.options.is_empty() {
                option_rows.push(body.lines().count());
                let index = question.options.len() + 1;
                if request.selected == index {
                    selected_row = Some(body.lines().count());
                }
                body.push_str(&format!(
                    "{} {index}. Other · type in prompt below\n",
                    if request.selected == index {
                        "▸"
                    } else {
                        " "
                    }
                ));
            }
            if request.freeform() {
                body.push_str(
                    "Type your answer in the prompt below · Enter submit · Esc decline\n",
                );
            }
        } else {
            body.push_str("Question unavailable\n");
        }
    } else {
        for (i, label) in ["Approve · A", "Deny · D / Esc"].iter().enumerate() {
            option_rows.push(body.lines().count());
            if request.selected == i + 1 {
                selected_row = Some(body.lines().count());
            }
            body.push_str(&format!(
                "{} {}\n",
                if request.selected == i + 1 {
                    "▸"
                } else {
                    " "
                },
                label
            ));
        }
        body.push_str("Enter select · PgUp/PgDn review\n");
        body.push_str(&markdown::sanitize(&request.heading));
    }
    if request.sending {
        body.push_str("\nSending answer…");
    }
    (body, selected_row, option_rows)
}

fn input_request_option_at(request: &InputRequest, menu: Rect, row: u16) -> Option<usize> {
    if request.sending || row <= menu.y || row >= menu.bottom().saturating_sub(1) {
        return None;
    }
    let (body, _, option_rows) =
        input_request_body(request, usize::from(menu.width.saturating_sub(4)));
    // Render the same wrapping and scroll as the visible dialog into a small
    // scratch buffer. Each option uses an invisible color marker, so a click
    // on a wrapped description cannot accidentally choose the next option.
    let lines: Vec<Line> = body
        .lines()
        .enumerate()
        .flat_map(|(line_index, text)| {
            let texts = if request.require_full_review {
                crate::memory_menu::wrap_review(
                    text,
                    usize::from(menu.width.saturating_sub(2)).max(1),
                )
            } else {
                vec![text.to_owned()]
            };
            let option_rows = &option_rows;
            texts.into_iter().map(move |text| {
                if let Some(option) = option_rows.iter().rposition(|&start| {
                    start == line_index || request.kind == "ask_user" && start <= line_index
                }) {
                    Line::styled(
                        text.to_owned(),
                        Style::default().bg(Color::Rgb(0, 0, (option + 1) as u8)),
                    )
                } else {
                    Line::from(text.to_owned())
                }
            })
        })
        .collect();
    let scroll = if request.require_full_review {
        usize::from(request.scroll).min(
            lines
                .len()
                .saturating_sub(usize::from(menu.height.saturating_sub(2))),
        ) as u16
    } else {
        request.scroll
    };
    let area = Rect::new(0, 0, menu.width, menu.height);
    let mut buffer = Buffer::empty(area);
    Paragraph::new(lines)
        .wrap(Wrap { trim: false })
        .scroll((scroll, 0))
        .block(Block::default().borders(Borders::ALL))
        .render(area, &mut buffer);
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
    expanded: Option<HashSet<transcript_tools::FoldKey>>,
    selected: Option<transcript_tools::FoldKey>,
    cards_revision: u64,
    lines: Vec<Line<'static>>,
    sections: Vec<transcript_tools::Section>,
    links: Vec<markdown::LinkRegion>,
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
        if fence.is_none() && matches!(paragraph.trim_matches('\n'), "**You:**" | "**Assistant:**")
        {
            found = Some(start);
        } else {
            for line in paragraph.lines() {
                let line = line.trim_start();
                if let Some(marker @ (b'`' | b'~')) = line.as_bytes().first().copied() {
                    let count = line.bytes().take_while(|byte| *byte == marker).count();
                    if count >= 3 {
                        match fence {
                            Some((open_marker, open_count))
                                if marker == open_marker
                                    && count >= open_count
                                    && line[count..].trim().is_empty() =>
                            {
                                fence = None
                            }
                            None => fence = Some((marker, count)),
                            _ => {}
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
    fn render(
        pane: usize,
        id: &str,
        source: &str,
        width: u16,
        expanded: Option<&HashSet<transcript_tools::FoldKey>>,
        selected: Option<transcript_tools::FoldKey>,
        cards_revision: u64,
        cards: &[tool_cards::ToolCard],
    ) -> Self {
        let (lines, sections, links) =
            transcript_tools::render_with_links(source, width, expanded, selected.clone(), cards);
        let mut turn_start = None;
        let mut prefix_lines = 0;
        if let Some(start) = streamed_turn_start(source) {
            let tail = &source[start..];
            if !tail.contains("Tool: ")
                && !tail.contains(transcript_tools::REASONING_PREFIX)
                && !tail.contains(transcript_tools::SHELL_PREFIX)
            {
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
        Self {
            pane,
            id: id.to_owned(),
            source: source.to_owned(),
            width,
            expanded: expanded.cloned(),
            selected,
            cards_revision,
            lines,
            sections,
            links,
            turn_start,
            prefix_lines,
        }
    }

    fn update(
        &mut self,
        source: &str,
        width: u16,
        expanded: Option<&HashSet<transcript_tools::FoldKey>>,
        selected: Option<transcript_tools::FoldKey>,
        cards_revision: u64,
        cards: &[tool_cards::ToolCard],
    ) {
        if self.width == width
            && self.expanded.as_ref() == expanded
            && self.selected == selected
            && self.cards_revision == cards_revision
        {
            if self.source == source {
                return;
            }
            if let Some(start) = self.turn_start.filter(|_| source.starts_with(&self.source)) {
                if streamed_turn_start(source) == Some(start) {
                    let tail = &source[start..];
                    if !tail.contains("Tool: ")
                        && !tail.contains(transcript_tools::REASONING_PREFIX)
                        && !tail.contains(transcript_tools::SHELL_PREFIX)
                    {
                        let (tail_lines, tail_sections, mut tail_links) =
                            transcript_tools::render_with_links(tail, width, None, None, &[]);
                        if tail_sections.is_empty() {
                            self.links.retain(|link| link.row < self.prefix_lines);
                            for link in &mut tail_links {
                                link.row += self.prefix_lines + 1;
                            }
                            self.links.extend(tail_links);
                            self.lines.truncate(self.prefix_lines);
                            self.lines.push(Line::default());
                            self.lines.extend(tail_lines);
                            self.sections
                                .retain(|section| section.line < self.prefix_lines);
                            self.source.clear();
                            self.source.push_str(source);
                            return;
                        }
                    }
                }
            }
        }
        *self = Self::render(
            self.pane,
            &self.id,
            source,
            width,
            expanded,
            selected,
            cards_revision,
            cards,
        );
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
    preferences: crate::preferences::Preferences,
    persist_preferences: bool,
    sidebar_auto: bool,
    clock_text: String,
    clock_deadline: Option<Instant>,
    window_focused: bool,
    auto_diff_seen: HashSet<String>,
    auto_diff_baseline: HashMap<String, String>,
    auto_diff_requests: VecDeque<String>,
    auto_diff_pending: Option<(String, PathBuf, Receiver<(String, bool)>)>,
    auto_diff_ready: HashSet<String>,
    belief_graph_pending: Option<(
        u64,
        String,
        bool,
        Receiver<Result<doxa_lore::BeliefGraph, doxa_lore::LoreError>>,
    )>,
    belief_graph_lines: Option<(u64, Vec<String>)>,
    belief_graph_scroll: usize,
    belief_graph_server: Option<crate::belief_graph::GraphServer>,
    pub sessions: Vec<Session>,
    pub collections: Vec<crate::collections::Collection>,
    pub groups: Vec<PaneGroup>,
    pub(crate) pane_tree: Option<panes::Tree>,
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
    installation: crate::installation::Snapshot,
    update_notified: bool,
    session_identity: HashMap<String, (Option<String>, Option<String>)>,
    session_efforts: HashMap<String, String>,
    session_catalogs: HashMap<String, session_controls::SessionCatalog>,
    effort_catalog_pending: Option<(String, String, String)>,
    model_refresh_after: Option<Instant>,
    requested_argument: Option<(String, &'static str, String)>,
    next_efforts: HashMap<String, String>,
    pub(crate) custom_names: HashMap<String, String>,
    session_telemetry: HashMap<String, SessionTelemetry>,
    // LORE owns these counts. A bounded background query keeps store I/O off
    // the draw path; an unavailable sidecar leaves the chip unknown.
    memory_cache: HashMap<String, (Option<doxa_lore::MemoryUsage>, Instant)>,
    memory_pending: Option<(
        String,
        String,
        Receiver<Option<(doxa_lore::MemoryUsage, bool)>>,
    )>,
    memory_repo: HashMap<String, bool>,
    memory_manager: Option<crate::memory_menu::Manager>,
    memory_list: Option<crate::memory_menu::List>,
    operations_menu: Option<operations_menu::Menu>,
    window_mesh: Option<crate::mesh_control::WindowMesh>,
    restart_executable: Option<PathBuf>,
    restart_after_update: bool,
    restart_waiting: bool,
    restart_job: Option<crate::maintenance::Restart>,
    retired_operations: Vec<operations_menu::Menu>,
    local_shell_jobs: Vec<crate::shell::Job>,
    next_shell_id: u64,
    plugin_commands: Vec<crate::operations::PluginCommand>,
    plugin_refresh: Option<crate::operations::PluginRefresh>,
    plugin_refresh_dirty: bool,
    fleet_menu: Option<fleet_menu::Menu>,
    pub(crate) fleet_views: Vec<fleet_menu::SavedView>,
    fleet_review: Option<fleet_process::Prepared>,
    fleet_controller: Option<fleet_process::Controller>,
    fleet_quit_pending: bool,
    memory_menu_pending: Option<(
        String,
        String,
        Receiver<Result<Vec<crate::memory_menu::Fact>, &'static str>>,
    )>,
    repo_cache: HashMap<String, (Option<doxa_worktrees::RepoStatus>, Instant)>,
    repo_pending: Option<(
        String,
        PathBuf,
        u64,
        Receiver<Option<doxa_worktrees::RepoStatus>>,
    )>,
    repo_epoch: HashMap<String, u64>,
    chip_offsets: Vec<usize>,
    belief_browser_fixture: bool,
    belief_button_hover: Option<Rect>,
    belief_preview: crate::belief_preview::Preview,
    belief_pointer: Option<(u16, u16)>,
    rendered_belief_rows: RefCell<Vec<crate::belief_preview::Owner>>,
    chip_hover: Option<ChipHit>,
    chip_hover_started: Option<(ChipHit, Instant)>,
    chip_tooltip_visible: bool,
    link_hover: Option<String>,
    link_hover_position: Option<(u16, u16)>,
    visible_links: RefCell<Vec<(Rect, String)>>,
    pending_open_urls: Vec<String>,
    chip_info: Option<ChipInfo>,
    // Mouse coordinates must come from the last painted frame, which may
    // differ from the terminal size reported by an earlier resize event.
    rendered_chip_hits: RefCell<Option<Vec<ChipHit>>>,
    blink_on: bool,
    blink_at: Instant,
    session_capabilities: HashMap<String, EngineCapabilities>,
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
    session_roster_pending: Option<Receiver<io::Result<Vec<crate::discovery::Session>>>>,
    session_stop_pending: Option<Receiver<crate::sessions::Report>>,
    pub(crate) killed_this_run: HashSet<String>,
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
    belief_filter_due: Option<Instant>,
    belief_filter_request: Option<(String, u16)>,
    belief_fixture_rows: Vec<lore_picker::Belief>,
    engine_picker: bool,
    engine_selected: usize,
    new_session: Option<NewSession>,
    vendor_catalog_pending: Option<(
        launch::Engine,
        Receiver<Option<Vec<doxa_vendors::ModelCapability>>>,
    )>,
    pending_launches: Vec<(launch::LaunchOptions, Option<String>, usize)>,
    pending_attaches: Vec<(String, usize)>,
    attaching_ids: HashSet<String>,
    pub(crate) detached_this_run: Vec<String>,
    launching: bool,
    awaiting_initial_attach: bool,
    startup_recovery: Option<String>,
    pending_model_queries: Vec<String>,
    pending_model_changes: Vec<(String, String)>,
    pending_effort_changes: Vec<(String, String)>,
    pending_effort_verifications: HashMap<String, String>,
    pub pending_prompts: Vec<(String, String)>,
    pending_peer_messages: Vec<(String, String, String)>,
    pub input_requests: Vec<InputRequest>,
    pub pending_answers: Vec<(String, String, serde_json::Value)>,
    pub rejected_drafts: HashMap<String, Vec<String>>,
    tool_cards: ToolCards,
    tool_cards_revision: HashMap<String, u64>,
    rendered_transcripts: RefCell<Vec<RenderedTranscript>>,
    transcript_selection: RefCell<crate::selection::Selection>,
    pending_clipboard_copy: Option<Vec<u8>>,
    clipboard_job: Option<crate::clipboard::Job>,
    tool_modal: bool,
    tool_selected: usize,
    tool_scroll: u16,
    expanded_tool_sections: HashMap<String, HashSet<transcript_tools::FoldKey>>,
    tool_section_hover: Option<(String, transcript_tools::FoldKey)>,
    selected_tool_sections: HashMap<String, transcript_tools::FoldKey>,
    visible_tool_sections: RefCell<Vec<(Rect, usize, String, transcript_tools::FoldKey)>>,
    peer_map: PeerMap,
    map_modal: bool,
    action_menu: bool,
    action_selected: usize,
    action_query: String,
    action_draft: Option<((usize, String), String, usize)>,
    slash_selected: usize,
    slash_dismissed: bool,
    history_modal: bool,
    history_resume: bool,
    history_explicit: bool,
    history_query: String,
    history_query_cursor: usize,
    history_selected: usize,
    history_pending: Option<Receiver<Vec<history::OfflineSession>>>,
    history_scan_query: Option<String>,
    history_query_due: Option<Instant>,
    history_search_inflight: Arc<AtomicUsize>,
    history_scanned_matches: HashMap<String, String>,
    history_entries: HashMap<String, history::OfflineSession>,
    resume_pending: Option<(
        usize,
        Option<String>,
        Receiver<(String, Result<launch::LaunchOptions, &'static str>)>,
    )>,
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
            preferences: crate::preferences::Preferences::load(),
            persist_preferences: false,
            sidebar_auto: false,
            clock_text: String::new(),
            clock_deadline: None,
            window_focused: true,
            auto_diff_seen: HashSet::new(),
            auto_diff_baseline: HashMap::new(),
            auto_diff_requests: VecDeque::new(),
            auto_diff_pending: None,
            auto_diff_ready: HashSet::new(),
            belief_graph_pending: None,
            belief_graph_lines: None,
            belief_graph_scroll: 0,
            belief_graph_server: None,
            sessions: Vec::new(),
            collections: Vec::new(),
            groups: vec![
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
            pane_tree: None,
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
            installation: crate::installation::Snapshot::default(),
            update_notified: false,
            session_identity: HashMap::new(),
            session_efforts: HashMap::new(),
            session_catalogs: HashMap::new(),
            effort_catalog_pending: None,
            model_refresh_after: None,
            requested_argument: None,
            next_efforts: HashMap::new(),
            custom_names: HashMap::new(),
            session_telemetry: HashMap::new(),
            memory_cache: HashMap::new(),
            memory_pending: None,
            memory_repo: HashMap::new(),
            memory_menu_pending: None,
            memory_manager: None,
            memory_list: None,
            operations_menu: None,
            window_mesh: None,
            restart_executable: None,
            restart_after_update: false,
            restart_waiting: false,
            restart_job: None,
            retired_operations: Vec::new(),
            local_shell_jobs: Vec::new(),
            next_shell_id: 1,
            plugin_commands: Vec::new(),
            plugin_refresh: None,
            plugin_refresh_dirty: false,
            fleet_menu: None,
            fleet_views: Vec::new(),
            fleet_review: None,
            fleet_controller: None,
            fleet_quit_pending: false,
            repo_cache: HashMap::new(),
            repo_pending: None,
            repo_epoch: HashMap::new(),
            chip_offsets: vec![0; panes::MAX_PANES],
            belief_browser_fixture: false,
            belief_button_hover: None,
            belief_preview: crate::belief_preview::Preview::default(),
            belief_pointer: None,
            rendered_belief_rows: RefCell::new(Vec::new()),
            chip_hover: None,
            chip_hover_started: None,
            chip_tooltip_visible: false,
            link_hover: None,
            link_hover_position: None,
            visible_links: RefCell::new(Vec::new()),
            pending_open_urls: Vec::new(),
            chip_info: None,
            rendered_chip_hits: RefCell::new(None),
            blink_on: true,
            blink_at: Instant::now(),
            session_capabilities: HashMap::new(),
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
            session_roster_pending: None,
            session_stop_pending: None,
            killed_this_run: HashSet::new(),
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
            belief_filter_due: None,
            belief_filter_request: None,
            belief_fixture_rows: Vec::new(),
            engine_picker: false,
            engine_selected: 0,
            new_session: None,
            vendor_catalog_pending: None,
            pending_launches: Vec::new(),
            pending_attaches: Vec::new(),
            attaching_ids: HashSet::new(),
            detached_this_run: Vec::new(),
            launching: false,
            awaiting_initial_attach: false,
            startup_recovery: None,
            pending_model_queries: Vec::new(),
            pending_model_changes: Vec::new(),
            pending_effort_changes: Vec::new(),
            pending_effort_verifications: HashMap::new(),
            pending_prompts: Vec::new(),
            pending_peer_messages: Vec::new(),
            input_requests: Vec::new(),
            pending_answers: Vec::new(),
            rejected_drafts: HashMap::new(),
            tool_cards: ToolCards::default(),
            tool_cards_revision: HashMap::new(),
            rendered_transcripts: RefCell::new(Vec::new()),
            transcript_selection: RefCell::new(crate::selection::Selection::default()),
            pending_clipboard_copy: None,
            clipboard_job: None,
            tool_modal: false,
            tool_selected: 0,
            tool_scroll: 0,
            expanded_tool_sections: HashMap::new(),
            tool_section_hover: Option<(String, transcript_tools::FoldKey)>,
    selected_tool_sections: HashMap::new(),
            visible_tool_sections: RefCell::new(Vec::new()),
            peer_map: PeerMap::default(),
            map_modal: false,
            action_menu: false,
            action_selected: 0,
            action_query: String::new(),
            action_draft: None,
            slash_selected: 0,
            slash_dismissed: false,
            history_modal: false,
            history_resume: false,
            history_explicit: false,
            history_query: String::new(),
            history_query_cursor: 0,
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

#[cfg(test)]
mod tests;

impl std::fmt::Debug for operations_menu::Menu {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OperationsMenu")
            .field("busy", &self.busy())
            .finish()
    }
}

#[cfg(test)]
mod parity_tests;
