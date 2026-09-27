//! Canonical structured curated facts and exact-review memory management.
use std::path::{Path, PathBuf};
use std::time::Duration;

pub fn scope_path(cwd: &Path) -> (PathBuf, bool) {
    match crate::discovery::repo_root_for(cwd) {
        Some(root) => (root, true),
        None => (cwd.to_path_buf(), false),
    }
}

/// Display facts are scrubbed canonical read rows. They are never reused as
/// entry keys or replacement text by the separate exact-review write manager.
#[derive(Clone,Debug,PartialEq,Eq)]
pub struct Fact {pub scope:String,pub text:String,pub source:Option<String>,pub redacted:bool}

pub fn parse_facts(rows:Vec<serde_json::Value>,scope:&str)->Result<Vec<Fact>,&'static str> {
    if rows.len()>400 {return Err("Too many curated memory entries");}
    let mut bytes=0;
    rows.into_iter().map(|row|{
        let text=row["text"].as_str().filter(|text|text.len()<=64*1024).ok_or("Invalid memory fact")?.to_owned();
        let source=match row.get("source") {None|Some(serde_json::Value::Null)=>None,
            Some(value)=>Some(value.as_str().filter(|source|source.len()<=512).ok_or("Invalid memory source")?.to_owned())};
        let redacted=row["redacted"].as_bool().ok_or("Invalid memory redaction metadata")?;
        bytes+=text.len()+source.as_ref().map_or(0,String::len);
        if bytes>64*1024 {return Err("Curated memory entries too large");}
        Ok(Fact {scope:scope.into(),text,source,redacted})
    }).collect()
}

pub fn fetch_facts(python:&Path,cwd:&Path)->Result<Vec<Fact>,&'static str> {
    let (project,is_repo)=scope_path(cwd);
    let mut lore=doxa_lore::LoreClient::spawn(python,Duration::from_secs(3)).map_err(|_|"LORE unavailable")?;
    let user=lore.memory_entries(cwd.to_str().ok_or("Invalid session directory")?,"user").map_err(|_|"User facts unavailable")?;
    let project=lore.memory_entries(project.to_str().ok_or("Invalid scope directory")?,"project").map_err(|_|"Scoped facts unavailable")?;
    let mut facts=parse_facts(user,"user")?;
    facts.extend(parse_facts(project,if is_repo {"project"} else {"folder"})?);
    if facts.len()>400 || facts.iter().map(|fact|fact.text.len()+fact.source.as_ref().map_or(0,String::len)).sum::<usize>()>64*1024 {
        return Err("Curated memory entries exceed menu limit");
    }
    Ok(facts)
}

#[derive(Debug)]
pub struct List {pub owner:Option<(String,String)>,pub facts:Vec<Fact>,pub query:String}
impl List {
    pub fn indices(&self)->Vec<usize> {
        let query=self.query.to_lowercase();
        self.facts.iter().enumerate().filter(|(_,fact)|query.is_empty()||fact.text.to_lowercase().contains(&query)
            ||fact.scope.to_lowercase().contains(&query)||fact.source.as_ref().is_some_and(|source|source.to_lowercase().contains(&query)))
            .map(|(index,_)|index).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::process::Command;

    #[test]
    fn structured_facts_keep_actual_provenance_and_filter_without_markdown() {
        let facts=parse_facts(vec![serde_json::json!({"text":"# Literal fact with \nline","source":"codex","redacted":false}),
            serde_json::json!({"text":"[redacted]","source":null,"redacted":true})],"user").unwrap();
        assert_eq!(facts[0].text,"# Literal fact with \nline");
        assert_eq!(facts[1].source,None);
        assert!(facts[1].redacted);
        let list=List {owner:None,facts,query:"codex".into()};
        assert_eq!(list.indices(),vec![0]);
        assert!(parse_facts(vec![serde_json::json!({"text":"safe","source":17,"redacted":false})],"user").is_err());
    }

    #[test]
    fn plain_directory_keeps_canonical_folder_scope() {
        let dir=tempfile::tempdir().unwrap();
        assert_eq!(scope_path(dir.path()),(dir.path().to_path_buf(),false));
    }

    #[test]
    fn managed_worktree_uses_canonical_main_repository_scope() {
        let dir = tempfile::tempdir().unwrap();
        let main = dir.path().join("repo");
        let tree = dir.path().join("tree");
        fs::create_dir(&main).unwrap();
        let git = |cwd: &Path, args: &[&str]| {
            let result = Command::new("git").args(args).current_dir(cwd).output().unwrap();
            assert!(result.status.success(), "{}", String::from_utf8_lossy(&result.stderr));
        };
        git(&main, &["init", "-q", "-b", "main"]);
        fs::write(main.join("README"), "base").unwrap();
        git(&main, &["add", "README"]);
        git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base"]);
        git(&main, &["worktree", "add", "-q", "-b", "feature", tree.to_str().unwrap()]);
        assert_eq!(scope_path(&tree), (main.canonicalize().unwrap(), true));

    }
}

/// Curated memory management stays separate from beliefs and pending reviews.
/// Each worker owns one request; switching scope drops its receiver.
pub struct Manager {
    pub owner: (String, String),
    pub scope: &'static str,
    pub selected: usize,
    pub scroll: usize,
    pub status: String,
    last_action: Option<String>,
    entries: Vec<String>,
    review: Option<serde_json::Value>,
    draft: Option<Draft>,
    pending: Option<std::sync::mpsc::Receiver<Result<Reply, String>>>,
    pub fixture: bool,
    pub refresh_scope: Option<&'static str>,
}

struct Draft {
    action: &'static str,
    entry: String,
    text: String,
    preview: bool,
    seen: std::cell::Cell<usize>,
}

enum Reply { Review(serde_json::Value), Action(String) }

impl Manager {
    pub fn new(owner: (String, String)) -> Self {
        let mut manager = Self { owner, scope: "project", selected: 0, scroll: 0,
            status: String::new(), last_action: None, entries: Vec::new(), review: None, draft: None,
            pending: None, fixture: false, refresh_scope: None };
        manager.load();
        manager
    }

    /// Render-only fixture constructor. No LORE worker or mutation is created.
    #[doc(hidden)]
    pub fn from_fixture_review(owner:(String,String),scope:&'static str,value:serde_json::Value)->Result<Self,String>{
        if !matches!(scope,"project"|"user"){return Err("Invalid fixture scope".into());}
        let mut manager=Self{owner,scope,selected:0,scroll:0,status:String::new(),last_action:None,entries:Vec::new(),review:None,draft:None,pending:None,fixture:true,refresh_scope:None};
        manager.accept_review(value)?;Ok(manager)
    }

    fn python() -> PathBuf {
        std::env::var_os("DOXA_LORE_PYTHON").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("python3"))
    }

    fn load(&mut self) {
        self.review = None;
        self.entries.clear();
        self.draft = None;
        self.selected = 0;
        self.scroll = 0;
        self.status = "Loading complete curated entries…".into();
        let cwd = scope_path(Path::new(&self.owner.1)).0;
        let scope = self.scope;
        let python = Self::python();
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        self.pending = Some(rx);
        if self.fixture { self.pending = None; return; }
        std::thread::spawn(move || {
            let result = doxa_lore::LoreClient::spawn(&python, Duration::from_secs(3))
                .and_then(|mut client| client.memory_review(&cwd.to_string_lossy(), scope))
                .map(Reply::Review).map_err(|error| format!("LORE refused memory review: {error}"));
            let _ = tx.send(result);
        });
    }

    fn accept_review(&mut self, value: serde_json::Value) -> Result<(), String> {
        let entries = value["entries"].as_array().ok_or("Incomplete memory review")?;
        let digest = value["sha256"].as_str().ok_or("Incomplete memory identity")?;
        if value["scope"] != self.scope || value["key"].as_str().is_none()
            || digest.len() != 64 || !digest.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
            || entries.len() > 400 || value["chars"].as_u64().is_none()
            || value["cap_chars"].as_u64().is_none_or(|cap| cap == 0 || cap > 1024 * 1024) {
            return Err("Invalid memory review".into());
        }
        let rows: Result<Vec<_>, _> = entries.iter().map(|row| row.as_str()
            .filter(|s| s.len() <= 16384 && !s.chars().any(char::is_control))
            .map(str::to_owned).ok_or("Incomplete memory entry")).collect();
        self.entries = rows?;
        self.review = Some(value);
        self.selected = self.selected.min(self.entries.len().saturating_sub(1));
        self.status = "↑↓ select · A add · E edit · D remove · Tab scope · R refresh".into();
        Ok(())
    }

    pub fn poll(&mut self) -> bool {
        let Some(receiver) = &self.pending else { return false; };
        let reply = match receiver.try_recv() {
            Ok(reply) => reply,
            Err(std::sync::mpsc::TryRecvError::Empty) => return false,
            Err(_) => Err("LORE memory worker unavailable".into()),
        };
        self.pending = None;
        match reply {
            Ok(Reply::Review(value)) => {
                if let Err(error) = self.accept_review(value) { self.status = error; self.review = None; }
            }
            Ok(Reply::Action(status)) => {
                if status == "applied" { self.refresh_scope = Some(self.scope); }
                self.draft = None;
                self.load();
                self.last_action = Some(match status.as_str() {
                    "applied" => "Saved by LORE",
                    "staged" => "Staged by LORE write gate; review in /pending",
                    _ => "LORE refused action",
                }.into());
            }
            Err(error) => { self.status = error; self.draft = None; self.review = None; }
        }
        true
    }

    pub fn key(&mut self, key: crossterm::event::KeyEvent) -> bool {
        use crossterm::event::{KeyCode, KeyModifiers};
        if self.pending.is_some() { return true; }
        if let Some(draft) = self.draft.as_mut() {
            match key.code {
                KeyCode::Esc => { self.draft = None; self.scroll = 0; }
                KeyCode::PageDown | KeyCode::Down if draft.preview => self.scroll = self.scroll.saturating_add(1),
                KeyCode::PageUp | KeyCode::Up if draft.preview => self.scroll = self.scroll.saturating_sub(1),
                KeyCode::Enter if !draft.preview => {
                    if draft.action != "remove" && draft.text.trim().is_empty() { return true; }
                    draft.text = draft.text.split_whitespace().collect::<Vec<_>>().join(" ");
                    draft.preview = true;
                    draft.seen.set(0);
                    self.scroll = 0;
                }
                KeyCode::Char('Y') if draft.preview => {
                    // draw() tracks the last reviewed visual row. Resize and
                    // edits reset this watermark; scrolling is never approval.
                    if draft.seen.get() == usize::MAX { self.submit(); }
                    else { self.status = "Read the complete change (↓/PgDn), then press uppercase Y".into(); }
                }
                KeyCode::Backspace if !draft.preview => { draft.text.pop(); }
                KeyCode::Char(c) if !draft.preview && !c.is_control()
                    && !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) => {
                    if draft.text.len() + c.len_utf8() <= 16384 { draft.text.push(c); }
                }
                _ => {}
            }
            return true;
        }
        match key.code {
            KeyCode::Tab => { self.scope = if self.scope == "project" { "user" } else { "project" }; self.load(); }
            KeyCode::Char('r' | 'R') => self.load(),
            KeyCode::Up => self.selected = self.selected.saturating_sub(1),
            KeyCode::Down => self.selected = (self.selected + 1).min(self.entries.len().saturating_sub(1)),
            KeyCode::PageUp => self.selected = self.selected.saturating_sub(8),
            KeyCode::PageDown => self.selected = (self.selected + 8).min(self.entries.len().saturating_sub(1)),
            KeyCode::Char(c @ ('a' | 'A' | 'e' | 'E' | 'd' | 'D')) if self.review.is_some() => {
                let action = match c.to_ascii_lowercase() { 'a' => "add", 'e' => "replace", _ => "remove" };
                let entry = self.entries.get(self.selected).cloned().unwrap_or_default();
                if action != "add" && entry.is_empty() { return true; }
                self.last_action = None;
                self.draft = Some(Draft { action, text: if action == "replace" { entry.clone() } else { String::new() },
                    entry, preview: false, seen: std::cell::Cell::new(0) });
                self.scroll = 0;
            }
            _ => {}
        }
        true
    }

    fn submit(&mut self) {
        if self.fixture { self.status = "Gallery fixture: memory writes disabled".into(); return; }
        let (Some(draft), Some(review)) = (&self.draft, &self.review) else { return; };
        let request = serde_json::json!({"scope":self.scope,"action":draft.action,
            "entry":draft.entry,"text":draft.text,
            "expected":{"key":review["key"],"sha256":review["sha256"]}});
        self.status = "Applying through LORE…".into();
        let cwd = scope_path(Path::new(&self.owner.1)).0;
        let python = Self::python();
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        self.pending = Some(rx);
        std::thread::spawn(move || {
            let result = doxa_lore::LoreClient::spawn(&python, Duration::from_secs(3))
                .and_then(|mut client| client.memory_action(&cwd.to_string_lossy(), request))
                .and_then(|value| value["status"].as_str().map(str::to_owned).ok_or(doxa_lore::LoreError::InvalidFrame))
                .map(Reply::Action).map_err(|error| format!("LORE refused memory change: {error}; R refresh"));
            let _ = tx.send(result);
        });
    }

    pub fn hover(&mut self, row: usize, visible: usize) -> bool {
        if self.draft.is_some() || self.pending.is_some() || row >= visible { return false; }
        let start = self.selected.saturating_sub(visible.saturating_sub(1));
        let index = start + row;
        if index >= self.entries.len() || index == self.selected { return false; }
        self.selected = index;
        true
    }

    pub fn draw(&self, frame: &mut ratatui::Frame, area: ratatui::layout::Rect) {
        use ratatui::{widgets::{Block, Borders, Paragraph}, style::Style};
        let width = usize::from(area.width.saturating_sub(2)).max(1);
        let visible = usize::from(area.height.saturating_sub(4)).max(1);
        let usage = self.review.as_ref().map(|r| format!("{} / {} chars", r["chars"], r["cap_chars"]))
            .unwrap_or_default();
        let mut lines = vec![format!("{} · {} · {}", self.scope, usage, self.last_action.as_deref().unwrap_or(&self.status))];
        if let Some(draft) = &self.draft {
            let mut body = Vec::new();
            if draft.preview {
                body.push(format!("Review {} in {} memory", draft.action, self.scope));
                if draft.action != "add" { body.push(format!("Before: {}", draft.entry)); }
                if draft.action != "remove" { body.push(format!("After: {}", draft.text)); }
                body.push("Uppercase Y apply · Esc cancel".into());
            } else {
                body.push(format!("{} · type one fact · Enter review · Esc cancel", draft.action));
                if draft.action == "remove" { body.push(format!("Remove: {}", draft.entry)); }
                else { body.push(format!("Text: {}", draft.text)); }
            }
            let body: Vec<String> = body.iter().flat_map(|line| wrap_review(line, width)).collect();
            let start = self.scroll.min(body.len().saturating_sub(visible));
            if draft.preview {
                let seen = draft.seen.get();
                if start <= seen || seen == usize::MAX {
                    let end = start + visible;
                    draft.seen.set(if end >= body.len() { usize::MAX } else { seen.max(end) });
                }
            }
            lines.extend(body.into_iter().skip(start).take(visible));
        } else {
            let start = self.selected.saturating_sub(visible.saturating_sub(1));
            if self.entries.is_empty() { lines.push("(empty) · A add a fact".into()); }
            for (i, entry) in self.entries.iter().enumerate().skip(start).take(visible) {
                lines.push(format!("{} {}", if i == self.selected { "›" } else { " " }, crate::lore_table::cell(entry, width.saturating_sub(2)).trim_end()));
            }
            lines.push("Enter actions: A add · E edit · D remove · Tab scope".into());
        }
        let lines: Vec<_> = lines.into_iter().enumerate().map(|(index, line)|
            if self.draft.is_some() && index > 0 { line } else { crate::lore_table::cell(&line, width).trim_end().to_owned() }).collect();
        frame.render_widget(Paragraph::new(lines.join("\n"))
            .block(Block::default().title(" LORE curated memory · Esc close ").borders(Borders::ALL)
                .border_style(Style::default().fg(crate::theme::ACCENT)))
            .style(Style::default().fg(crate::theme::TEXT).bg(crate::theme::RAISED)), area);
    }

    pub fn editing(&self) -> bool { self.draft.is_some() || self.pending.is_some() }
    pub fn busy(&self) -> bool { self.pending.is_some() }

    pub fn reset_review_visibility(&mut self) {
        if let Some(draft) = &self.draft { draft.seen.set(0); }
        self.scroll = 0;
    }
}

pub(crate) fn wrap_review(line: &str, width: usize) -> Vec<String> {
    use unicode_width::UnicodeWidthChar;
    let mut rows = Vec::new();
    let mut row = String::new();
    let mut used = 0;
    for c in line.chars() {
        let cells = c.width().unwrap_or(0);
        if used + cells > width && !row.is_empty() { rows.push(std::mem::take(&mut row)); used = 0; }
        row.push(c); used += cells;
    }
    rows.push(row); rows
}

#[cfg(test)]
mod manager_tests {
    use super::*;
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    fn key(code: KeyCode) -> KeyEvent { KeyEvent::new(code, KeyModifiers::NONE) }
    fn fixture() -> Manager {
        let mut manager = Manager { owner: ("session".into(), "/fixture".into()), scope: "user",
            selected: 0, scroll: 0, status: String::new(), last_action: None, entries: Vec::new(), review: None,
            draft: None, pending: None, fixture: true, refresh_scope: None };
        manager.accept_review(serde_json::json!({"scope":"user","key":"user", "sha256":"a".repeat(64),
            "entries":["first fact", "long fact".repeat(80)],"chars":100,"cap_chars":9000})).unwrap();
        manager
    }
    #[test]
    fn exact_change_requires_complete_preview_and_explicit_uppercase_confirmation() {
        let mut manager = fixture();
        manager.selected = 1;
        manager.key(key(KeyCode::Char('d')));
        manager.key(key(KeyCode::Enter));
        manager.key(key(KeyCode::Char('Y')));
        assert!(manager.pending.is_none());
        let mut terminal = ratatui::Terminal::new(ratatui::backend::TestBackend::new(40, 8)).unwrap();
        terminal.draw(|frame| manager.draw(frame, frame.area())).unwrap();
        manager.key(key(KeyCode::Char('Y')));
        assert!(manager.pending.is_none(), "long exact entry was not fully displayed");
        assert_ne!(manager.status, "Gallery fixture: memory writes disabled", "incomplete review must not reach submit");
        for _ in 0..40 {
            manager.key(key(KeyCode::PageDown));
            terminal.draw(|frame| manager.draw(frame, frame.area())).unwrap();
        }
        manager.key(key(KeyCode::Char('y')));
        assert!(manager.pending.is_none());
        assert_ne!(manager.status, "Gallery fixture: memory writes disabled", "lowercase confirmation must not reach submit");
        manager.key(key(KeyCode::Char('Y')));
        assert!(manager.pending.is_none(), "render fixtures must never start a LORE worker");
        assert_eq!(manager.status, "Gallery fixture: memory writes disabled", "complete review and uppercase confirmation reached submit");
    }
    #[test]
    fn cancelling_edit_and_scope_switch_discard_draft_and_old_receiver() {
        let mut manager = fixture();
        manager.key(key(KeyCode::Char('e')));
        manager.key(key(KeyCode::Backspace));
        manager.key(key(KeyCode::Esc));
        assert!(manager.draft.is_none());
        assert_eq!(manager.entries[0], "first fact");
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        manager.pending = Some(rx);
        manager.pending = None; // completed worker before choosing another scope
        manager.key(key(KeyCode::Tab));
        assert_eq!(manager.scope, "project");
        assert!(tx.send(Ok(Reply::Action("applied".into()))).is_err());
        assert!(manager.review.is_none());
    }
    #[test]
    fn hover_does_not_select_footer_or_mutate_during_preview() {
        let mut manager = fixture();
        assert!(!manager.hover(4, 4));
        assert!(manager.hover(1, 4));
        assert_eq!(manager.selected, 1);
        manager.key(key(KeyCode::Char('d')));
        assert!(!manager.hover(0, 4));
        assert_eq!(manager.selected, 1);
    }
    #[test]
    fn review_wrap_respects_wide_characters_and_resize_revokes_confirmation() {
        assert_eq!(wrap_review("界界界", 4), vec!["界界", "界"]);
        let mut manager = fixture();
        manager.key(key(KeyCode::Char('d')));
        manager.key(key(KeyCode::Enter));
        manager.draft.as_ref().unwrap().seen.set(usize::MAX);
        manager.reset_review_visibility();
        manager.key(key(KeyCode::Char('Y')));
        assert!(manager.pending.is_none());
    }
}

impl std::fmt::Debug for Manager {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MemoryManager").field("scope", &self.scope).field("busy", &self.busy()).finish()
    }
}
