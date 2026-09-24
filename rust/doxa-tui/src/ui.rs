//! Terminal shell for the Rust frontend. Daemon adapters can feed [`App::apply_update`].
use std::io::{self, IsTerminal, Stdout};
use std::sync::mpsc::{self, Receiver, Sender, TryRecvError};
use std::time::Duration;

use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEventKind};
use crossterm::terminal::{self, EnterAlternateScreen, LeaveAlternateScreen};
use crossterm::{execute, event::{DisableMouseCapture, EnableMouseCapture}};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Tabs, Wrap};
use ratatui::{Frame, Terminal};

use crate::markdown;

const MIN_PANE_WIDTH: u16 = 28;
const MIN_PANE_HEIGHT: u16 = 8;
const MAX_PENDING_PROMPTS: usize = 32;

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
    fn active_id(&self) -> Option<&str> { self.tabs.get(self.active).map(String::as_str) }
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
pub enum Focus { Prompt, Rail, Transcript }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Split { Horizontal, Vertical }

#[derive(Clone, Debug)]
pub struct App {
    pub sessions: Vec<Session>,
    pub groups: [PaneGroup; 2],
    pub active_group: usize,
    pub split: Split,
    pub split_percent: u16,
    pub rail_visible: bool,
    pub rail_selected: usize,
    pub focus: Focus,
    pub input: String,
    pub pending_prompts: Vec<(String, String)>,
    pub notice: String,
    pub should_quit: bool,
    pub size: Rect,
}

impl Default for App {
    fn default() -> Self {
        Self {
            sessions: Vec::new(),
            groups: [PaneGroup { tabs: vec![], active: 0, scroll: 0 }, PaneGroup { tabs: vec![], active: 0, scroll: 0 }],
            active_group: 0, split: Split::Vertical, split_percent: 50,
            rail_visible: true, rail_selected: 0, focus: Focus::Prompt,
            input: String::new(), pending_prompts: Vec::new(), notice: "Disconnected · waiting for daemon".into(),
            should_quit: false, size: Rect::default(),
        }
    }
}

impl App {
    pub fn apply_update(&mut self, update: DaemonUpdate) {
        match update {
            DaemonUpdate::Upsert(session) => {
                if let Some(existing) = self.sessions.iter_mut().find(|s| s.id == session.id) { *existing = session; }
                else {
                    let id = session.id.clone();
                    self.sessions.push(session);
                    if self.groups[0].tabs.is_empty() { self.groups[0].tabs.push(id); }
                }
            }
            DaemonUpdate::Transcript { id, markdown } => {
                if let Some(s) = self.sessions.iter_mut().find(|s| s.id == id) { s.transcript = markdown; }
            }
            DaemonUpdate::Status { id, text } => {
                if let Some(s) = self.sessions.iter_mut().find(|s| s.id == id) { s.status = text; }
            }
        }
        self.rail_selected = self.rail_selected.min(self.sessions.len().saturating_sub(1));
    }

    /// Apply one versioned daemon frame after transport decoding. Returns whether
    /// visible state changed. Unknown frames are ignored for forward compatibility.
    pub fn apply_daemon_frame(&mut self, frame: &serde_json::Value) -> bool {
        let Some(kind) = frame.get("type").and_then(|v| v.as_str()) else { return false };
        match kind {
            "hello" => {
                let Some(id) = frame.get("session_id").and_then(|v| v.as_str()) else { return false };
                let model = frame.get("model").and_then(|v| v.as_str()).unwrap_or("session");
                let cwd = frame.get("cwd").and_then(|v| v.as_str()).unwrap_or("");
                self.apply_update(DaemonUpdate::Upsert(Session {
                    id: id.into(), title: model.into(), collection: cwd.into(),
                    transcript: String::new(), status: "Connected".into(),
                }));
                self.notice = format!("Connected · {model}");
                true
            }
            "event" => {
                let Some(event) = frame.get("event") else { return false };
                let Some(event_type) = event.get("type").and_then(|v| v.as_str()) else { return false };
                let data = &event["data"];
                // A transport reader may serve several sockets. The frame itself
                // lacks a session id, so a reader can add one before delivery.
                let id = frame.get("session_id").and_then(|v| v.as_str())
                    .or_else(|| self.groups[self.active_group].active_id())
                    .or_else(|| self.sessions.first().map(|s| s.id.as_str()));
                let Some(id) = id.map(str::to_owned) else { return false };
                match event_type {
                    "text_delta" => {
                        let Some(text) = data.get("text").and_then(|v| v.as_str()) else { return false };
                        if let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) {
                            session.transcript.push_str(text);
                            self.groups.iter_mut().filter(|g| g.active_id() == Some(&id)).for_each(|g| g.scroll = 0);
                            true
                        } else { false }
                    }
                    "turn_started" => { self.apply_update(DaemonUpdate::Status { id, text: "Running".into() }); true }
                    "turn_done" => { self.apply_update(DaemonUpdate::Status { id, text: "Ready".into() }); true }
                    "needs_input" => { self.apply_update(DaemonUpdate::Status { id, text: "Needs input".into() }); true }
                    _ => false,
                }
            }
            "reply" => {
                let ok = frame.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
                self.notice = if ok { "Request accepted".into() } else {
                    format!("Request failed: {}", frame.get("error").and_then(|v| v.as_str()).unwrap_or("unknown error"))
                };
                true
            }
            _ => false,
        }
    }

    pub fn handle(&mut self, event: Event) -> bool {
        match event {
            Event::Resize(w, h) => { self.size = Rect::new(0, 0, w, h); true }
            Event::Key(key) if key.kind == KeyEventKind::Press || key.kind == KeyEventKind::Repeat => self.key(key),
            Event::Mouse(mouse) => self.mouse(mouse.kind),
            _ => false,
        }
    }

    fn key(&mut self, key: KeyEvent) -> bool {
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        let alt = key.modifiers.contains(KeyModifiers::ALT);
        match key.code {
            KeyCode::Char('c') if ctrl => { self.should_quit = true; true }
            KeyCode::Char('q') if ctrl => { self.should_quit = true; true }
            KeyCode::F(3) => { self.rail_visible = !self.rail_visible; true }
            KeyCode::Tab if key.modifiers.contains(KeyModifiers::SHIFT) => { self.active_group = 1 - self.active_group; self.focus = Focus::Prompt; true }
            KeyCode::Tab => { self.focus = match self.focus { Focus::Prompt => Focus::Transcript, Focus::Transcript => Focus::Rail, Focus::Rail => Focus::Prompt }; true }
            KeyCode::Esc => { self.focus = Focus::Prompt; true }
            KeyCode::Char('h') if alt => { self.split = Split::Horizontal; true }
            KeyCode::Char('v') if alt => { self.split = Split::Vertical; true }
            KeyCode::Left if alt => self.adjust_split(-5),
            KeyCode::Right if alt => self.adjust_split(5),
            KeyCode::Up if alt => self.adjust_split(-5),
            KeyCode::Down if alt => self.adjust_split(5),
            KeyCode::Up if self.focus == Focus::Rail => { self.rail_selected = self.rail_selected.saturating_sub(1); true }
            KeyCode::Down if self.focus == Focus::Rail => { self.rail_selected = (self.rail_selected + 1).min(self.sessions.len().saturating_sub(1)); true }
            KeyCode::Enter if self.focus == Focus::Rail => { self.open_selected(); true }
            KeyCode::PageUp if self.focus == Focus::Transcript => { let p = &mut self.groups[self.active_group]; p.scroll = p.scroll.saturating_add(5); true }
            KeyCode::PageDown if self.focus == Focus::Transcript => { let p = &mut self.groups[self.active_group]; p.scroll = p.scroll.saturating_sub(5); true }
            KeyCode::Up if self.focus == Focus::Transcript => { let p = &mut self.groups[self.active_group]; p.scroll = p.scroll.saturating_add(1); true }
            KeyCode::Down if self.focus == Focus::Transcript => { let p = &mut self.groups[self.active_group]; p.scroll = p.scroll.saturating_sub(1); true }
            KeyCode::Left if self.focus == Focus::Transcript => { self.previous_tab(); true }
            KeyCode::Right if self.focus == Focus::Transcript => { self.next_tab(); true }
            KeyCode::Backspace if self.focus == Focus::Prompt => { self.input.pop().is_some() }
            KeyCode::Char(c) if self.focus == Focus::Prompt && !ctrl && !alt => { self.input.push(c); true }
            KeyCode::Enter if self.focus == Focus::Prompt => {
                if !self.input.is_empty() {
                    if let Some(id) = self.groups[self.active_group].active_id() {
                        if self.pending_prompts.len() < MAX_PENDING_PROMPTS {
                            self.pending_prompts.push((id.to_owned(), std::mem::take(&mut self.input)));
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

    fn adjust_split(&mut self, delta: i16) -> bool {
        self.split_percent = (self.split_percent as i16 + delta).clamp(20, 80) as u16;
        true
    }

    fn rail_order(&self) -> Vec<usize> {
        let mut order: Vec<usize> = (0..self.sessions.len()).collect();
        order.sort_by(|&a, &b| self.sessions[a].collection.cmp(&self.sessions[b].collection)
            .then_with(|| self.sessions[a].title.cmp(&self.sessions[b].title)));
        order
    }

    /// A transport loop drains this queue and sends each prompt to its session.
    pub fn take_prompts(&mut self) -> Vec<(String, String)> {
        std::mem::take(&mut self.pending_prompts)
    }

    fn open_selected(&mut self) {
        let selected = self.rail_order().get(self.rail_selected).copied();
        if let Some(session) = selected.and_then(|index| self.sessions.get(index)) {
            let tabs = &mut self.groups[self.active_group];
            if let Some(index) = tabs.tabs.iter().position(|id| id == &session.id) { tabs.active = index; }
            else { tabs.tabs.push(session.id.clone()); tabs.active = tabs.tabs.len() - 1; }
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
            MouseEventKind::ScrollUp => { let p = &mut self.groups[self.active_group]; p.scroll = p.scroll.saturating_add(3); true }
            MouseEventKind::ScrollDown => { let p = &mut self.groups[self.active_group]; p.scroll = p.scroll.saturating_sub(3); true }
            _ => false,
        }
    }

    pub fn draw(&self, frame: &mut Frame) {
        let area = frame.area();
        if area.width < 20 || area.height < 5 {
            frame.render_widget(Paragraph::new("DOXA · enlarge terminal"), area);
            return;
        }
        let outer = Layout::default().direction(Direction::Vertical)
            .constraints([Constraint::Min(3), Constraint::Length(3), Constraint::Length(1)])
            .split(area);
        let rail_width = if self.rail_visible && outer[0].width >= 70 { 25 } else { 0 };
        let body = if rail_width > 0 {
            let chunks = Layout::default().direction(Direction::Horizontal)
                .constraints([Constraint::Length(rail_width), Constraint::Min(1)]).split(outer[0]);
            self.draw_rail(frame, chunks[0]); chunks[1]
        } else { outer[0] };
        let vertical = self.split == Split::Vertical;
        let min_ok = if vertical { body.width >= MIN_PANE_WIDTH * 2 } else { body.height >= MIN_PANE_HEIGHT * 2 };
        if min_ok {
            let panes = Layout::default().direction(if vertical { Direction::Horizontal } else { Direction::Vertical })
                .constraints([Constraint::Percentage(self.split_percent), Constraint::Percentage(100 - self.split_percent)])
                .split(body);
            self.draw_group(frame, panes[0], 0);
            self.draw_group(frame, panes[1], 1);
        } else { self.draw_group(frame, body, self.active_group); }
        let prompt_title = if self.focus == Focus::Prompt { " Prompt ● " } else { " Prompt " };
        frame.render_widget(Paragraph::new(format!("> {}", self.input)).block(Block::default().title(prompt_title).borders(Borders::ALL)), outer[1]);
        frame.render_widget(Paragraph::new(format!("{}  |  F3 rail · Shift+Tab pane · Alt+H/V split · Alt+arrows resize · Ctrl+Q quit", self.notice)), outer[2]);
    }

    fn draw_rail(&self, frame: &mut Frame, area: Rect) {
        let mut lines = Vec::new();
        let mut last_collection = "";
        for (position, index) in self.rail_order().into_iter().enumerate() {
            let session = &self.sessions[index];
            if session.collection != last_collection {
                last_collection = &session.collection;
                lines.push(Line::styled(format!("  {}", if last_collection.is_empty() { "Sessions" } else { last_collection }), Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)));
            }
            let mark = if position == self.rail_selected { "▸" } else { " " };
            lines.push(Line::from(vec![Span::raw(format!("{mark} {}", session.title))]));
        }
        if lines.is_empty() { lines.push(Line::from("  No sessions")); }
        frame.render_widget(Paragraph::new(lines).block(Block::default().title(if self.focus == Focus::Rail { " Sessions ● " } else { " Sessions " }).borders(Borders::ALL)), area);
    }

    fn draw_group(&self, frame: &mut Frame, area: Rect, index: usize) {
        if area.width < 4 || area.height < 3 { return; }
        let group = &self.groups[index];
        let session = group.active_id().and_then(|id| self.sessions.iter().find(|s| s.id == id));
        let inner = Layout::default().direction(Direction::Vertical)
            .constraints([Constraint::Length(2), Constraint::Min(1), Constraint::Length(1)])
            .split(area);
        let titles: Vec<Line> = group.tabs.iter().map(|id| {
            let name = self.sessions.iter().find(|s| &s.id == id).map(|s| s.title.as_str()).unwrap_or(id);
            Line::from(name.to_owned())
        }).collect();
        let tabs = Tabs::new(if titles.is_empty() { vec![Line::from("Empty")] } else { titles })
            .select(group.active.min(group.tabs.len().saturating_sub(1)))
            .highlight_style(Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD))
            .block(Block::default().title(format!(" Pane {}{} ", index + 1, if self.active_group == index { " ●" } else { "" })).borders(Borders::ALL));
        frame.render_widget(tabs, inner[0]);
        let content = session.map(|s| s.transcript.as_str()).unwrap_or("No session open. Select one in the rail and press Enter.");
        let lines = markdown::render(content, inner[1].width.saturating_sub(2));
        let max_scroll = lines.len().saturating_sub(inner[1].height.saturating_sub(2) as usize) as u16;
        let scroll_from_top = max_scroll.saturating_sub(group.scroll.min(max_scroll));
        frame.render_widget(Paragraph::new(lines).scroll((scroll_from_top, 0)).wrap(Wrap { trim: false })
            .block(Block::default().borders(Borders::LEFT | Borders::RIGHT)), inner[1]);
        let status = session.map(|s| s.status.as_str()).unwrap_or("No session");
        frame.render_widget(Paragraph::new(format!(" {} ", status)).style(Style::default().fg(Color::DarkGray)), inner[2]);
    }
}

/// Owns terminal modes so every return path, including I/O errors, restores the screen.
struct TerminalGuard { out: Stdout, raw: bool, alternate: bool, mouse: bool }
impl TerminalGuard {
    fn enter() -> io::Result<Self> {
        let mut guard = Self { out: io::stdout(), raw: false, alternate: false, mouse: false };
        terminal::enable_raw_mode()?; guard.raw = true;
        execute!(guard.out, EnterAlternateScreen)?; guard.alternate = true;
        execute!(guard.out, EnableMouseCapture)?; guard.mouse = true;
        Ok(guard)
    }
}
impl Drop for TerminalGuard {
    fn drop(&mut self) {
        if self.mouse { let _ = execute!(self.out, DisableMouseCapture); }
        if self.alternate { let _ = execute!(self.out, LeaveAlternateScreen); }
        if self.raw { let _ = terminal::disable_raw_mode(); }
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
    prompts: Sender<(String, String)>,
) -> io::Result<()> {
    run_loop(frames, Some(prompts))
}

fn run_loop(
    receiver: Receiver<serde_json::Value>,
    mut prompt_sender: Option<Sender<(String, String)>>,
) -> io::Result<()> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        return Err(io::Error::new(io::ErrorKind::NotConnected, "DOXA requires an interactive terminal"));
    }
    let guard = TerminalGuard::enter()?;
    let mut terminal = Terminal::new(CrosstermBackend::new(&guard.out))?;
    let mut app = App::default();
    app.size = terminal.size().map(|s| Rect::new(0, 0, s.width, s.height))?;
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
            let mut failed = Vec::new();
            for prompt in app.take_prompts() {
                if let Err(error) = sender.send(prompt) { failed.push(error.0); }
            }
            if !failed.is_empty() {
                app.pending_prompts = failed;
                app.notice = "Daemon writer unavailable · prompt retained".into();
                prompt_sender = None;
                changed = true;
            }
        }
        if changed { terminal.draw(|frame| app.draw(frame))?; }
    }
    drop(terminal);
    drop(guard);
    Ok(())
}
