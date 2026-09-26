//! Selectable operations popup. Authentication runs on a worker and only
//! filtered public progress is retained in this menu, never in transcripts.
use crossterm::event::{KeyCode, KeyEvent};
use std::sync::mpsc::{self, Receiver};

#[derive(Clone)]
enum Action { Report, Store(bool), Plugins(bool), Auth(&'static str, &'static str), Skip }

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
}
impl Menu {
    pub fn new(kind: &str) -> Self {
        let mut menu = Self { kind: kind.into(), rows: Vec::new(), selected: 0, messages: Vec::new(), worker: None, closed: false, step: 0, editing: None, input: String::new() };
        menu.prepare(); menu
    }
    fn prepare(&mut self) {
        self.selected = 0;
        self.rows = match self.kind.as_str() {
            "login" | "logout" => vec![("Claude (Anthropic)".into(), Action::Auth("claude", if self.kind == "login" { "login" } else { "logout" })), ("Codex (OpenAI)".into(), Action::Auth("codex", if self.kind == "login" { "login" } else { "logout" }))],
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
                if done { self.worker = None; break; }
            }
        }
    }
    pub fn closed(&self) -> bool { self.closed }
    pub fn busy(&self) -> bool { self.worker.is_some() }
    pub fn lines(&self, _width: usize) -> Vec<String> {
        let mut lines = vec![format!("{} · ↑↓ choose · Enter apply · Esc close", self.kind)];
        if self.busy() { lines.push("Operation running…".into()); }
        if let Some(key) = self.editing { lines.push(format!("{key}: {}", self.input)); }
        else { lines.extend(self.rows.iter().enumerate().map(|(i, (label, _))| format!("{} {label}", if i == self.selected { "›" } else { " " }))); }
        lines.extend(self.messages.iter().rev().take(18).rev().flat_map(|s| s.lines().map(str::to_owned)));
        lines
    }
    pub fn choice_at(&self, row: usize) -> bool { !self.busy() && self.editing.is_none() && row > 0 && row <= self.rows.len() }
    pub fn hover(&mut self, row: usize) { if self.choice_at(row) { self.selected = row - 1; } }
    pub fn key(&mut self, key: KeyEvent) {
        if key.code == KeyCode::Esc { if !self.busy() { self.closed = true; } return; }
        if self.busy() { return; }
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
        let action = self.rows[self.selected].1.clone();
        if let Action::Auth(name, action) = action {
            let (sender, receiver) = mpsc::channel(); self.worker = Some(receiver);
            std::thread::spawn(move || {
                let result = crate::operations::auth_action(name, action, |message| { let _ = sender.send((false, message)); });
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
            Action::Auth(_, _) => unreachable!(),
        };
        let success = result.is_ok(); self.messages.push(result.unwrap_or_else(|e| e.to_string()));
        if self.kind == "setup" && success {
            if self.step >= 2 { self.closed = true; } else { self.step += 1; self.prepare(); }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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
