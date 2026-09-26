//! Selectable operations popup. Authentication runs on a worker and only
//! filtered public progress is retained in this menu, never in transcripts.
use crossterm::event::{KeyCode, KeyEvent};
use std::sync::mpsc::{self, Receiver};
use std::sync::{Arc, atomic::{AtomicBool, Ordering}};

#[derive(Clone)]
enum Action { Report, Store(bool), Plugins(bool), Auth(crate::operations::AuthRequest), Skip }

pub struct Menu {
    kind: String,
    rows: Vec<(String, Action)>,
    selected: usize,
    messages: Vec<String>,
    worker: Option<Receiver<(bool, String)>>,
    closed: bool,
    step: usize,
    editing: Option<&'static str>,
    input: String,
    scroll: usize,
    cancel: Option<Arc<AtomicBool>>,
    requested: bool,
}
impl Menu {
    pub fn new(kind: &str) -> Self {
        let mut menu = Self { kind: kind.into(), rows: Vec::new(), selected: 0, messages: Vec::new(), worker: None, closed: false, step: 0, editing: None, input: String::new(), scroll: 0, cancel: None, requested: false };
        menu.prepare(); menu
    }
    /// Parsing/selection is pure. The UI calls start_requested only after the
    /// popup fits above the prompt, so a hidden popup cannot start authentication.
    pub fn with_auth_args(kind: &str, arguments: &str) -> Result<Self, String> {
        let request = crate::operations::parse_auth_request(kind, arguments).map_err(|error| error.to_string())?;
        let mut menu = Self::new(kind);
        if let Some(request) = request {
            menu.selected = menu.rows.iter().position(|(_, action)| matches!(action, Action::Auth(candidate) if *candidate == request))
                .ok_or("Unsupported authentication choice")?;
            menu.requested = true;
        }
        Ok(menu)
    }
    pub fn start_requested(&mut self) {
        if self.requested && !self.busy() && !self.closed {
            self.requested = false;
            self.apply();
        }
    }
    fn prepare(&mut self) {
        self.selected = 0;
        self.rows = match self.kind.as_str() {
            "login" | "logout" => {
                let action = if self.kind == "login" { "login" } else { "logout" };
                let mut rows = vec![("Claude (Anthropic)".into(), Action::Auth(crate::operations::AuthRequest { provider:"claude", action, device_auth:false })),
                    ((if action == "login" { "Codex (OpenAI) · browser" } else { "Codex (OpenAI)" }).into(), Action::Auth(crate::operations::AuthRequest { provider:"codex", action, device_auth:false }))];
                if action == "login" { rows.push(("Codex (OpenAI) · device code".into(), Action::Auth(crate::operations::AuthRequest { provider:"codex", action, device_auth:true }))); }
                rows
            },
            "setup" => match self.step {
                0 => vec![("Check authentication and continue".into(), Action::Report)],
                1 => vec![("Create separate DOXA LORE store".into(), Action::Store(false)), ("Share existing Claude LORE store".into(), Action::Store(true)), ("Skip store selection".into(), Action::Skip)],
                _ => vec![("Edit model default".into(), Action::Report), ("Edit effort default".into(), Action::Report), ("Finish setup".into(), Action::Skip)],
            },
            _ => vec![("Refresh discovered Claude plugins".into(), Action::Report), ("Enable adoption for new sessions".into(), Action::Plugins(true)), ("Disable adoption for new sessions".into(), Action::Plugins(false))],
        };
    }
    pub fn poll(&mut self) {
        if let Some(worker) = &self.worker {
            let results = worker.try_iter().collect::<Vec<_>>();
            for (done, message) in results {
                self.messages.push(message);
                if done { self.worker = None; self.cancel = None; break; }
            }
        }
    }
    pub fn closed(&self) -> bool { self.closed }
    pub fn busy(&self) -> bool { self.worker.is_some() }
    pub fn lines(&self, width: usize) -> Vec<String> {
        use unicode_width::UnicodeWidthChar;
        let width = width.max(1);
        let mut lines = vec![if self.busy() && matches!(self.kind.as_str(), "login" | "logout") {
            format!("{} · Esc cancel · PgUp/PgDn report", self.kind)
        } else { format!("{} · ↑↓ choose · Enter apply · Esc close · PgUp/PgDn report", self.kind) }];
        if self.busy() { lines.push("Operation running…".into()); }
        else if self.requested { lines.push("Authentication requested…".into()); }
        if let Some(key) = self.editing { lines.push(format!("{key}: {}", self.input)); }
        else { lines.extend(self.rows.iter().enumerate().map(|(i, (label, _))| format!("{} {label}", if i == self.selected { "›" } else { " " }))); }
        let mut report = Vec::new();
        for source in self.messages.iter().flat_map(|s| s.lines()) {
            let mut line = String::new(); let mut cells = 0;
            for ch in source.chars() {
                let next = ch.width().unwrap_or(0);
                if cells + next > width && !line.is_empty() { report.push(std::mem::take(&mut line)); cells = 0; }
                line.push(ch); cells += next;
            }
            report.push(line);
        }
        let room = 19usize.saturating_sub(lines.len());
        let end = report.len().saturating_sub(self.scroll.min(report.len().saturating_sub(room)));
        lines.extend(report[end.saturating_sub(room)..end].iter().cloned());
        lines
    }
    pub fn choice_at(&self, row: usize) -> bool { !self.busy() && !self.requested && self.editing.is_none() && row > 0 && row <= self.rows.len() }
    pub fn hover(&mut self, row: usize) { if self.choice_at(row) { self.selected = row - 1; } }
    pub fn key(&mut self, key: KeyEvent) {
        if key.code == KeyCode::Esc {
            if let Some(cancel) = &self.cancel { cancel.store(true, Ordering::Release); }
            self.closed = true; return;
        }
        if key.code == KeyCode::PageUp { self.scroll = self.scroll.saturating_add(8); return; }
        if key.code == KeyCode::PageDown { self.scroll = self.scroll.saturating_sub(8); return; }
        if self.busy() || self.requested { return; }
        if let Some(field) = self.editing {
            match key.code {
                KeyCode::Char(c) if !c.is_control() => self.input.push(c),
                KeyCode::Backspace => { self.input.pop(); }
                KeyCode::Enter => {
                    let result = crate::operations::setup_default(field, Some(&self.input));
                    self.messages.push(result.unwrap_or_else(|e| e.to_string())); self.editing = None; self.input.clear();
                }
                _ => {}
            }
            return;
        }
        match key.code {
            KeyCode::Up => self.selected = self.selected.saturating_sub(1),
            KeyCode::Down => self.selected = (self.selected + 1).min(self.rows.len().saturating_sub(1)),
            KeyCode::Enter => self.apply(),
            _ => {}
        }
    }
    fn apply(&mut self) {
        self.scroll = 0;
        let action = self.rows[self.selected].1.clone();
        if let Action::Auth(request) = action {
            let (sender, receiver) = mpsc::channel(); self.worker = Some(receiver);
            let cancel = Arc::new(AtomicBool::new(false)); self.cancel = Some(cancel.clone());
            std::thread::spawn(move || {
                let result = crate::operations::auth_action_request(request, |message| { let _ = sender.send((false, message)); }, &cancel);
                let _ = sender.send((true, result.unwrap_or_else(|error| error.to_string())));
            }); return;
        }
        if matches!(action, Action::Report) && !(self.kind == "setup" && self.step >= 2) {
            let setup = self.kind == "setup";
            let (sender, receiver) = mpsc::channel(); self.worker = Some(receiver);
            std::thread::spawn(move || {
                let result = if setup { crate::operations::setup_report() } else { crate::operations::plugins_report() };
                let _ = sender.send((true, result.unwrap_or_else(|e| e.to_string())));
            });
            if setup { self.step += 1; self.prepare(); }
            return;
        }
        if self.kind == "setup" && self.step >= 2 && self.selected < 2 {
            self.editing = Some(if self.selected == 0 { "model" } else { "effort" }); return;
        }
        let result = match action {
            Action::Store(shared) => crate::operations::setup_choose_store(shared),
            Action::Plugins(on) => crate::operations::plugins_change(on),
            Action::Report => if self.kind == "setup" { crate::operations::setup_report() } else { crate::operations::plugins_report() },
            Action::Skip => Ok("Skipped".into()),
            Action::Auth(_) => unreachable!(),
        };
        let success = result.is_ok(); self.messages.push(result.unwrap_or_else(|e| e.to_string()));
        if self.kind == "setup" && success {
            if self.step >= 2 { self.closed = true; } else { self.step += 1; self.prepare(); }
        }
    }
}

impl Drop for Menu {
    fn drop(&mut self) { if let Some(cancel) = &self.cancel { cancel.store(true, Ordering::Release); } }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn explicit_auth_argument_selection_is_pure_until_popup_is_visible() {
        let browser = Menu::with_auth_args("login", "claude").unwrap();
        assert_eq!(browser.selected, 0); assert!(browser.requested); assert!(!browser.busy());
        let device = Menu::with_auth_args("login", "codex --device-auth").unwrap();
        assert_eq!(device.selected, 2); assert!(device.requested); assert!(!device.busy());
        assert!(device.lines(80).iter().any(|line| line.contains("device code")));
        let chooser = Menu::with_auth_args("logout", "").unwrap();
        assert!(!chooser.requested); assert!(!chooser.busy());
        assert!(Menu::with_auth_args("login", "claude --device-auth").is_err());
        assert!(Menu::with_auth_args("logout", "codex --device-auth").is_err());
        assert!(Menu::with_auth_args("login", "codex --with-api-key").is_err());
    }
    #[test]
    fn public_progress_wraps_without_truncation_and_reports_scroll() {
        let mut menu = Menu::new("plugins");
        menu.messages.push("abcdefghijklmnopqrstuvwxyz".into());
        assert_eq!(menu.lines(8).iter().skip(4).cloned().collect::<String>(), "abcdefghijklmnopqrstuvwxyz");
        menu.messages.push((0..30).map(|n| format!("line {n}")).collect::<Vec<_>>().join("\n"));
        let latest = menu.lines(80);
        menu.key(KeyEvent::new(KeyCode::PageUp, crossterm::event::KeyModifiers::NONE));
        assert_ne!(menu.lines(80), latest);
    }
    #[test]
    fn closing_menu_cancels_private_auth_worker() {
        let cancel = Arc::new(AtomicBool::new(false));
        let mut menu = Menu::new("login"); menu.cancel = Some(cancel.clone());
        menu.key(KeyEvent::new(KeyCode::Esc, crossterm::event::KeyModifiers::NONE));
        assert!(cancel.load(Ordering::Acquire)); assert!(menu.closed());
    }
    #[test]
    fn login_menu_requires_explicit_provider_and_never_runs_on_open() {
        let mut menu = Menu::new("login");
        menu.poll(); menu.hover(1);
        assert!(!menu.busy());
        assert!(!menu.choice_at(0)); assert!(menu.choice_at(1)); assert!(!menu.choice_at(99));
        assert!(menu.lines(80).iter().any(|l| l.contains("Claude")));
        menu.key(KeyEvent::new(KeyCode::Down, crossterm::event::KeyModifiers::NONE));
        assert_eq!(menu.selected, 1);
        menu.key(KeyEvent::new(KeyCode::Esc, crossterm::event::KeyModifiers::NONE));
        assert!(menu.closed());
    }
}
