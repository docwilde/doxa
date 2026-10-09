//! Coordinate tab, session and repository navigation with owned asynchronous jobs.
use super::{
    attach_matches, repo_directory_entries, safe_label, safe_repo_directory, unsafe_input_char,
    App, AttachPicker, ChipInfo, ClearPending, ClearSwap, DaemonUpdate, Focus, PaneGroup,
    QueuePicker, RepoPicker,
};
use crate::launch;
use crossterm::event::KeyCode;
use crossterm::event::KeyEvent;
use crossterm::event::KeyModifiers;
use std::io;
use std::collections::HashSet;
use std::path::Path;
use std::path::PathBuf;
use std::sync::mpsc;
use std::sync::mpsc::TryRecvError;
use std::time::Duration;
use std::time::Instant;

impl App {
    /// Verify a disconnected, detached row against the trusted live registry
    /// before removing it from the rail. Archived history stays searchable.
    pub(super) fn poll_stale_detached(&mut self, now: Instant) -> bool {
        if self.remote_mode { return false; }
        if let Some(receiver) = self.stale_detached_pending.as_ref() {
            match receiver.try_recv() {
                Ok(Ok(rows)) => {
                    self.stale_detached_pending = None;
                    let live: HashSet<_> = rows.into_iter().map(|row| row.id).collect();
                    return self.mark_dead_detached(&live);
                }
                Ok(Err(_)) | Err(TryRecvError::Disconnected) => {
                    self.stale_detached_pending = None;
                    return false;
                }
                Err(TryRecvError::Empty) => return false,
            }
        }
        if now < self.stale_detached_check_at { return false; }
        if !self.sessions.iter().any(|session| {
            !self.offline_ids.contains(&session.id)
                && !crate::remote_client::valid_target(&session.id)
                && !self.groups.iter().any(|group| group.tabs.contains(&session.id))
                && (session.status == "Disconnected" || self.detached_this_run.contains(&session.id))
        }) {
            self.stale_detached_check_at = now + Duration::from_secs(2);
            return false;
        }
        self.stale_detached_check_at = now + Duration::from_secs(5);
        let (tx, rx) = mpsc::sync_channel(1);
        if std::thread::Builder::new().name("stale-detached-roster".into())
            .spawn(move || { let _ = tx.send(crate::discovery::sessions()); }).is_ok() {
            self.stale_detached_pending = Some(rx);
        }
        false
    }

    pub(super) fn mark_dead_detached(&mut self, live: &HashSet<String>) -> bool {
        let selected_id = self.rail_order().get(self.rail_selected)
            .map(|index| self.sessions[*index].id.clone());
        let dead: Vec<_> = self.sessions.iter()
            .filter(|session| !self.offline_ids.contains(&session.id)
                && !crate::remote_client::valid_target(&session.id)
                && !live.contains(&session.id)
                && !self.groups.iter().any(|group| group.tabs.contains(&session.id))
                && (session.status == "Disconnected" || self.detached_this_run.contains(&session.id)))
            .map(|session| session.id.clone()).collect();
        for id in &dead { self.offline_ids.insert(id.clone()); }
        if !dead.is_empty() {
            let rows = self.rail_order();
            self.rail_selected = selected_id.as_ref()
                .and_then(|id| rows.iter().position(|index| self.sessions[*index].id == *id))
                .unwrap_or(self.rail_selected.min(rows.len().saturating_sub(1)));
        }
        !dead.is_empty()
    }

    pub(super) fn open_live_sessions(&mut self) {
        if self.session_roster_pending.is_some() {
            self.notice = "sessions: discovery pending".into();
            return;
        }
        let (tx, rx) = mpsc::sync_channel(1);
        match std::thread::Builder::new()
            .name("session-roster".into())
            .spawn(move || {
                let _ = tx.send(crate::discovery::sessions());
            }) {
            Ok(_) => {
                self.session_roster_pending = Some(rx);
                self.notice = "sessions: finding live daemons…".into();
            }
            Err(error) => self.notice = format!("sessions: {}", safe_label(&error.to_string())),
        }
    }

    pub(super) fn poll_sessions_roster(&mut self) -> bool {
        let Some(receiver) = self.session_roster_pending.as_ref() else {
            return false;
        };
        let result = match receiver.try_recv() {
            Ok(result) => result,
            Err(TryRecvError::Empty) => return false,
            Err(TryRecvError::Disconnected) => {
                Err(io::Error::other("discovery worker disconnected"))
            }
        };
        self.session_roster_pending = None;
        let rows = match result {
            Ok(rows) => rows,
            Err(error) => {
                self.notice = format!("sessions: {}", safe_label(&error.to_string()));
                return true;
            }
        };
        let mut lines = rows
            .iter()
            .map(|row| {
                let attached = self.groups.iter().any(|group| group.tabs.contains(&row.id));
                format!(
                    "{}  {}  ·  {}",
                    safe_label(&row.id),
                    safe_label(self.custom_names.get(&row.id).unwrap_or(&row.title)),
                    if attached {
                        "attached here"
                    } else {
                        "detached"
                    }
                )
            })
            .collect::<Vec<_>>();
        if lines.is_empty() {
            lines.push("sessions: none live".into());
        }
        lines.push(String::new());
        lines.push("/sessions kill <prefix> · /sessions kill-detached".into());
        lines.push("/search · saved session history".into());
        self.chip_info = Some(ChipInfo {
            kind: "sessions",
            label: String::new(),
            lines,
            scroll: 0,
            owner: None,
        });
        self.notice.clear();
        true
    }

    pub(super) fn local_sessions_stop(&mut self, action: crate::sessions::Action) {
        if self.session_stop_pending.is_some() {
            self.notice = "sessions: stop already pending".into();
            return;
        }
        let attached = self
            .groups
            .iter()
            .flat_map(|group| group.tabs.iter().cloned())
            .collect();
        match crate::sessions::start(action, attached) {
            Ok(receiver) => {
                self.session_stop_pending = Some(receiver);
                self.notice = "sessions: checking live targets…".into();
            }
            Err(error) => self.notice = format!("sessions: {}", safe_label(&error.to_string())),
        }
    }

    pub(super) fn stop_active_session(&mut self) {
        if self.active_remote() { self.notice = "Remote sessions must be stopped on their host".into(); return; }
        let Some(id) = self.groups[self.active_group].active_id().map(str::to_owned) else {
            self.notice = "Select a session to stop".into(); return;
        };
        if self.offline_ids.contains(&id) {
            self.notice = "This session is already stopped".into(); return;
        }
        self.local_sessions_stop(crate::sessions::Action::Kill(id));
    }

    pub(super) fn start_session_delete(&mut self, id: String, cwd: std::path::PathBuf) {
        let (sender, receiver) = std::sync::mpsc::sync_channel(1);
        self.session_delete_pending = Some(receiver);
        std::thread::spawn(move || {
            let result = crate::history::delete_saved_session(&id, &cwd);
            let _ = sender.send((id, result));
        });
        self.notice = "Deleting verified DOXA transcript…".into();
    }

    pub(super) fn poll_session_delete(&mut self) -> bool {
        let Some(receiver) = self.session_delete_pending.as_ref() else { return false; };
        let result = match receiver.try_recv() {
            Ok(result) => result,
            Err(TryRecvError::Empty) => return false,
            Err(TryRecvError::Disconnected) => {
                self.session_delete_pending = None;
                self.notice = "Transcript deletion worker disconnected; saved transcript may remain".into();
                return true;
            }
        };
        self.session_delete_pending = None;
        let (id, outcome) = result;
        match outcome {
            Ok(()) => {
                self.sessions.retain(|session| session.id != id);
                self.history_entries.remove(&id);
                self.history_scanned_matches.remove(&id);
                self.offline_ids.remove(&id);
                self.detached_this_run.retain(|item| item != &id);
                self.session_cwds.remove(&id);
                self.killed_this_run.insert(id.clone());
                for collection in &mut self.collections { collection.sessions.retain(|item| item != &id); }
                self.rail_selected = self.rail_selected.min(self.rail_order().len().saturating_sub(1));
                self.notice = format!("DOXA transcript deleted · {}", safe_label(&id));
            }
            Err(reason) => self.notice = format!("Transcript preserved · {reason} · use /resume to reopen"),
        }
        true
    }

    pub(super) fn poll_sessions_stop(&mut self) -> bool {
        let Some(receiver) = self.session_stop_pending.as_ref() else {
            return false;
        };
        let report = match receiver.try_recv() {
            Ok(report) => report,
            Err(TryRecvError::Empty) => return false,
            Err(TryRecvError::Disconnected) => {
                self.session_stop_pending = None;
                self.notice = if self.delete_after_stop.take().is_some() {
                    "Transcript preserved · daemon shutdown could not be verified · use /resume".into()
                } else {
                    "sessions: stop worker disconnected".into()
                };
                return true;
            }
        };
        self.session_stop_pending = None;
        let deleting = self.delete_after_stop.is_some();
        if let Some((id, cwd)) = self.delete_after_stop.take() {
            if report.stopped.contains(&id) {
                self.start_session_delete(id, cwd);
            } else {
                self.notice = format!("Transcript preserved · daemon shutdown not verified · {} · use /resume", safe_label(&id));
            }
        }
        for id in report.stopped.iter().chain(report.requested.iter()) {
            self.killed_this_run.insert(id.clone());
            self.offline_ids.insert(id.clone());
            self.input_requests
                .retain(|request| request.session_id != *id);
            self.pending_answers.retain(|(session, _, _)| session != id);
            self.apply_update(DaemonUpdate::Status {
                id: id.clone(),
                text: if report.stopped.contains(id) {
                    "Stopped"
                } else {
                    "Stopping"
                }
                .into(),
            });
        }
        if !deleting {
            self.notice = safe_label(&report.text());
        }
        true
    }

    pub(super) fn local_attach(&mut self, args: &str) {
        let query = args.trim();
        if query.len() > 200 || query.chars().any(unsafe_input_char) {
            self.notice =
                "attach: query must be at most 200 bytes without control characters".into();
            return;
        }
        let live = if self.active_remote() {
            Ok(self.sessions.iter().map(|session| crate::discovery::Session {
                id:session.id.clone(), title:session.title.clone(), socket:PathBuf::new(),
                scope_key:session.collection.clone(), clients:None, started_at:String::new(),
            }).collect())
        } else { crate::discovery::sessions() };
        let live = match live {
            Ok(rows) => rows,
            Err(error) => {
                self.notice = format!(
                    "attach: discovery failed · {}",
                    safe_label(&error.to_string())
                );
                return;
            }
        };
        let candidates: Vec<_> = if query.is_empty() {
            live.into_iter()
                .filter(|session| {
                    !self
                        .groups
                        .iter()
                        .any(|group| group.tabs.contains(&session.id))
                })
                .collect()
        } else {
            // ID matches win over titles, so a familiar ID prefix never
            // silently attaches a different session named after that prefix.
            let exact: Vec<_> = live
                .iter()
                .filter(|session| session.id == query)
                .cloned()
                .collect();
            if !exact.is_empty() {
                exact
            } else {
                let prefixes: Vec<_> = live
                    .iter()
                    .filter(|session| session.id.starts_with(query))
                    .cloned()
                    .collect();
                if !prefixes.is_empty() {
                    prefixes
                } else {
                    let query = query.to_lowercase();
                    live.into_iter()
                        .filter(|session| session.title.to_lowercase().contains(&query))
                        .collect()
                }
            }
        };
        match candidates.as_slice() {
            [] => {
                self.notice = if query.is_empty() {
                    "attach: no detached live sessions".into()
                } else {
                    format!("attach: no live session matches {}", safe_label(query))
                };
            }
            [one] => self.attach_selected(&one.id),
            _ => {
                self.attach_picker = Some(AttachPicker {
                    rows: candidates,
                    query: String::new(),
                    selected: 0,
                });
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

    pub(super) fn attach_matches(&self) -> Vec<usize> {
        let Some(picker) = &self.attach_picker else {
            return Vec::new();
        };
        picker
            .rows
            .iter()
            .enumerate()
            .filter_map(|(index, session)| attach_matches(session, &picker.query).then_some(index))
            .collect()
    }

    pub(super) fn attach_picker_key(&mut self, key: KeyEvent) -> bool {
        let len = self.attach_matches().len();
        let Some(picker) = self.attach_picker.as_mut() else {
            return false;
        };
        match key.code {
            KeyCode::Esc => self.attach_picker = None,
            KeyCode::Up => picker.selected = picker.selected.saturating_sub(1),
            KeyCode::Down => picker.selected = (picker.selected + 1).min(len.saturating_sub(1)),
            KeyCode::Backspace => {
                picker.query.pop();
                picker.selected = 0;
            }
            KeyCode::Char(c)
                if !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
            {
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

    pub(super) fn branch_picker_key(&mut self, key: KeyEvent) -> bool {
        let Some(picker) = self.branch_picker.as_mut() else {
            return false;
        };
        match key.code {
            KeyCode::Esc => self.branch_picker = None,
            KeyCode::Up => picker.selected = picker.selected.saturating_sub(1),
            KeyCode::Down => {
                picker.selected = (picker.selected + 1).min(picker.branches.len().saturating_sub(1))
            }
            KeyCode::Enter => self.choose_branch(),
            _ => return false,
        }
        true
    }

    pub(super) fn choose_branch(&mut self) {
        let Some(picker) = self.branch_picker.take() else {
            return;
        };
        if self.groups[self.active_group].active_id() != Some(picker.session_id.as_str()) {
            self.notice = "Branch choice cancelled: active session changed".into();
            return;
        }
        let Some(branch) = picker.branches.get(picker.selected) else {
            return;
        };
        if branch == &picker.base {
            self.notice = format!("branch: already based on {}", safe_label(branch));
            return;
        }
        self.pending_queue_commands
            .push(crate::bridge::WorkerCommand::Branch(
                picker.session_id,
                Some(branch.clone()),
            ));
        self.notice = format!("Checking branch {}…", safe_label(branch));
    }

    pub(super) fn open_selected_attach(&mut self) {
        let Some(picker) = &self.attach_picker else {
            return;
        };
        let Some(&index) = self.attach_matches().get(picker.selected) else {
            return;
        };
        let id = picker.rows[index].id.clone();
        self.attach_picker = None;
        // Registry entries are hints: a row may have gone stale while the
        // picker was open. The bridge performs one more identity check.
        if self.active_remote() {
            if self.sessions.iter().any(|session| session.id == id) { self.attach_selected(&id); }
            else { self.notice = "Remote session is no longer available".into(); }
            return;
        }
        match crate::discovery::sessions() {
            Ok(live) if live.iter().any(|session| session.id == id) => self.attach_selected(&id),
            Ok(_) => {
                self.notice = format!("attach: session is no longer live · {}", safe_label(&id))
            }
            Err(error) => {
                self.notice = format!(
                    "attach: discovery failed · {}",
                    safe_label(&error.to_string())
                )
            }
        }
    }

    pub(super) fn attach_selected(&mut self, id: &str) {
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
        if !self.manual_tab_available_for(Some(id)) {
            return;
        }
        self.attaching_ids.insert(id.to_owned());
        self.pending_attaches
            .push((id.to_owned(), self.active_group));
        self.input.clear();
        self.input_cursor = 0;
        self.notice = format!("Attaching · {}", safe_label(id));
    }

    pub(crate) fn has_unverified_archived_tabs(&self) -> bool {
        self.groups.iter().any(|group| {
            group.tabs.iter().any(|id| {
                self.offline_ids.contains(id)
                    && !self.killed_this_run.contains(id)
                    && !self.history_entries.contains_key(id)
            })
        })
    }

    pub(crate) fn has_offline_open_tabs(&self) -> bool {
        self.groups
            .iter()
            .any(|group| group.tabs.iter().any(|id| self.offline_ids.contains(id)))
    }

    pub(super) fn rollback_clear(&mut self, swap: ClearSwap) {
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
            if let Some(member) = collection
                .sessions
                .iter_mut()
                .find(|member| member.as_str() == swap.new_id)
            {
                *member = swap.old_id.clone();
            }
        }
        self.clear_stop_after_save.retain(|id| id != &swap.old_id);
        self.pending_clear_finalizes.push(swap.new_id);
        self.notice =
            "Clear cancelled · tabset could not be saved; previous session preserved".into();
    }

    pub(super) fn finish_clear_swap(&mut self, persisted: bool) -> bool {
        let Some(swap) = self.clear_swap.take() else {
            return false;
        };
        if persisted {
            self.clear_stop_after_save.retain(|id| id != &swap.old_id);
            self.pending_clear_finalizes.push(swap.old_id);
            self.notice = "Fresh session ready · finalizing previous session".into();
        } else {
            self.rollback_clear(swap);
        }
        true
    }

    pub(super) fn local_clear(&mut self, args: &str) {
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
        if self
            .session_activity
            .get(&id)
            .is_some_and(|(running, queued)| *running || *queued > 0)
            || self
                .input_requests
                .iter()
                .any(|request| request.session_id == id)
            || self
                .pending_prompts
                .iter()
                .any(|(session, _)| session == &id)
        {
            self.notice = "clear: wait for the current turn and queued prompts to finish".into();
            return;
        }
        let Some(engine) = self
            .session_identity
            .get(&id)
            .and_then(|identity| identity.0.as_deref())
        else {
            self.notice = "clear: session engine is unavailable".into();
            return;
        };
        let engine = match engine {
            "codex" => launch::Engine::Codex,
            "claude" => launch::Engine::Claude,
            "deepseek" => launch::Engine::DeepSeek,
            "glm" => launch::Engine::Glm,
            _ => {
                self.notice = "clear: session engine cannot be relaunched".into();
                return;
            }
        };
        let Some(cwd) = self
            .session_cwds
            .get(&id)
            .cloned()
            .filter(|path| path.is_absolute())
        else {
            self.notice = "clear: session directory is unavailable".into();
            return;
        };
        // A managed session's cwd is its private worktree. A fresh session
        // starts from the shared checkout, as the Python session factory
        // does, instead of branching from the old session's branch.
        let launch_cwd = crate::discovery::repo_root_for(&cwd).unwrap_or(cwd);
        let mut options = launch::LaunchOptions {
            engine,
            cwd: Some(launch_cwd),
            ..Default::default()
        };
        if engine == launch::Engine::Claude {
            options.claude_bin = std::env::var_os("DOXA_CLAUDE_BIN").map(PathBuf::from);
        }
        self.clear_pending = Some(ClearPending { old_id: id, group });
        self.pending_launches.push((options, None, group));
        self.launching = true;
        self.input.clear();
        self.input_cursor = 0;
        self.notice = "Starting a fresh session in this tab…".into();
    }

    pub(super) fn local_cd(&mut self, args: &str) {
        let Some(id) = self.groups[self.active_group].active_id() else {
            self.notice = "cd: select a session first".into();
            return;
        };
        let Some(engine) = self
            .session_identity
            .get(id)
            .and_then(|identity| identity.0.as_deref())
        else {
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
            if requested.is_absolute() {
                requested.to_path_buf()
            } else {
                self.session_cwds
                    .get(id)
                    .cloned()
                    .or_else(|| std::env::current_dir().ok())
                    .unwrap_or_default()
                    .join(requested)
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
        let mut options = launch::LaunchOptions {
            engine,
            cwd: Some(cwd.clone()),
            ..Default::default()
        };
        if engine == launch::Engine::Claude {
            options.claude_bin = std::env::var_os("DOXA_CLAUDE_BIN").map(PathBuf::from);
        }
        self.pending_launches
            .push((options, None, self.active_group));
        self.launching = true;
        self.input.clear();
        self.input_cursor = 0;
        self.notice = format!(
            "Opening a new tab at {} · current session stays here",
            safe_label(&cwd.display().to_string())
        );
    }

    pub(super) fn open_repo_picker(&mut self, group: usize) {
        if self.active_remote() { self.notice = "Remote worktree is managed on the session host".into(); return; }
        self.active_group = group;
        let Some(id) = self.groups[group].active_id() else {
            self.notice = "Choose a session first".into();
            return;
        };
        let source = self.session_cwds.get(id).cloned();
        let current = source.as_deref().and_then(safe_repo_directory).or_else(|| {
            source
                .as_deref()
                .and_then(Path::parent)
                .and_then(safe_repo_directory)
        });
        let Some(current_dir) = current else {
            self.notice = "Current session directory is unavailable".into();
            return;
        };
        self.chip_info = None;
        let paths = repo_directory_entries(&current_dir);
        self.repo_picker = Some(RepoPicker {
            current_dir,
            paths,
            selected: 0,
        });
        if self.active_chooser_rect().is_none() {
            self.repo_picker = None;
            self.notice = "Enlarge active pane to choose a directory".into();
        }
    }

    pub(super) fn repo_picker_key(&mut self, key: KeyEvent) -> bool {
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
                    if let Some(path) = path.to_str() {
                        self.local_cd(path);
                    }
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

    pub(super) fn local_collection(&mut self, args: &str) {
        let (verb, rest) = args
            .trim()
            .split_once(char::is_whitespace)
            .map_or((args.trim(), ""), |(verb, rest)| (verb, rest.trim()));
        if verb.is_empty() || matches!(verb, "list" | "ls") {
            self.notice = if self.collections.is_empty() {
                "No collections yet · /collection add <name>".into()
            } else {
                self.collections
                    .iter()
                    .map(|item| format!("{} ({} sessions)", item.name, item.sessions.len()))
                    .collect::<Vec<_>>()
                    .join(" · ")
            };
            self.input.clear();
            self.input_cursor = 0;
            return;
        }
        if verb == "sort" {
            let mode = if rest.is_empty() {
                if self.preferences.value("collection_sort") == "urgency" { "manual" } else { "urgency" }
            } else { rest };
            if !["manual", "urgency"].contains(&mode) {
                self.notice = "Usage: /collection sort [manual|urgency]".into();
                return;
            }
            self.notice = match crate::settings::config_path().and_then(|path|
                crate::settings::save(&path, &[("collection_sort".into(), Some(mode.into()))], "claude")) {
                Ok(()) => {
                    self.preferences = crate::preferences::Preferences::load();
                    self.rail_sort_signature.clear();
                    self.rail_sort_order.clear();
                    self.input.clear(); self.input_cursor = 0;
                    format!("Collection order: {mode}")
                }
                Err(error) => format!("Collection order unchanged: {error}"),
            };
            return;
        }
        let active = self.groups[self.active_group]
            .active_id()
            .map(str::to_owned);
        let name = if verb == "new" && rest.is_empty() {
            let cwd = active.as_deref().and_then(|id| self.session_cwds.get(id));
            let config = crate::settings::config_path().map(|path| doxa_state::load_config(&path)).unwrap_or_default();
            let customer = cwd.and_then(|root| crate::collections::configured_customer(&config, root));
            let project = active.as_deref().and_then(|id| self.repo_cache.get(id))
                .and_then(|(status, _)| status.as_ref())
                .and_then(|status| match status {
                    doxa_worktrees::RepoStatus::Repository { repo, .. } => Some(repo.as_str()),
                    doxa_worktrees::RepoStatus::Directory { name } => Some(name.as_str()),
                })
                .or_else(|| cwd.and_then(|path| path.file_name()).and_then(|name| name.to_str()));
            let task = active.as_deref().and_then(|id| self.sessions.iter().find(|session| session.id == id))
                .map(|session| session.title.as_str())
                .filter(|title| !title.trim().is_empty() && Some(*title) != active.as_deref()
                    && *title != "New session");
            Some(crate::collections::unique_name(&self.collections,
                &crate::collections::suggested_name(customer, project, task)))
        } else { None };
        let result = crate::collections::edit(&mut self.collections, verb, name.as_deref().unwrap_or(rest), active.as_deref());
        self.notice = match result {
            Ok(note) => {
                self.input.clear();
                self.input_cursor = 0;
                note
            }
            Err(error) => error,
        };
    }

    pub(super) fn local_rename(&mut self, args: &str) {
        let Some(id) = self.groups[self.active_group]
            .active_id()
            .map(str::to_owned)
        else {
            self.notice = "rename: select a tab".into();
            return;
        };
        if args.len() > 200 || args.chars().any(unsafe_input_char) {
            self.notice =
                "rename: name must be at most 200 bytes without control characters".into();
            return;
        }
        let name = args.trim();
        if name.is_empty() {
            self.custom_names.remove(&id);
            let automatic = self.default_names.get(&id).cloned().unwrap_or_else(|| {
                self.session_identity.get(&id).and_then(|identity| identity.1.as_deref())
                    .map(safe_label).unwrap_or_else(|| safe_label(&id))
            });
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
        if self.rename_draft_backup.is_some() {
            self.restore_rename_draft();
        } else {
            self.input.clear();
            self.input_cursor = 0;
        }
    }

    pub(super) fn open_queue(&mut self) {
        let Some(id) = self.groups[self.active_group]
            .active_id()
            .map(str::to_owned)
        else {
            self.notice = "Select a live session to inspect its queue".into();
            return;
        };
        if self.offline_ids.contains(&id) {
            self.notice = "Archived sessions have no live prompt queue".into();
            return;
        }
        self.input.clear();
        self.input_cursor = 0;
        self.queue_picker = Some(QueuePicker {
            session_id: id.clone(),
            rows: Vec::new(),
            selected: 0,
            loading: true,
            cancelling: None,
        });
        if self.active_chooser_rect().is_none() {
            self.queue_picker = None;
            self.notice = "Enlarge active pane to inspect queued prompts".into();
            return;
        }
        self.pending_queue_commands
            .push(crate::bridge::WorkerCommand::QueueList(id));
    }

    pub(super) fn queue_key(&mut self, key: KeyEvent) -> bool {
        let Some(picker) = self.queue_picker.as_mut() else {
            return false;
        };
        match key.code {
            KeyCode::Esc => self.queue_picker = None,
            KeyCode::Up => picker.selected = picker.selected.saturating_sub(1),
            KeyCode::Down => {
                picker.selected = (picker.selected + 1).min(picker.rows.len().saturating_sub(1))
            }
            KeyCode::Char('r') | KeyCode::Char('R') => {
                picker.loading = true;
                self.pending_queue_commands
                    .push(crate::bridge::WorkerCommand::QueueList(
                        picker.session_id.clone(),
                    ));
            }
            KeyCode::Char('x') | KeyCode::Char('X') | KeyCode::Delete => {
                self.cancel_selected_queue()
            }
            _ => return false,
        }
        true
    }

    pub(super) fn cancel_selected_queue(&mut self) {
        let Some(picker) = self.queue_picker.as_mut() else {
            return;
        };
        if picker.loading || picker.cancelling.is_some() {
            return;
        }
        let Some(row) = picker.rows.get(picker.selected) else {
            return;
        };
        let id = row.id.clone();
        if picker.rows.iter().filter(|row| row.id == id).count() != 1 {
            self.notice = "Duplicate queue ID; cancellation is unsafe".into();
            return;
        }
        picker.cancelling = Some(id.clone());
        self.pending_queue_commands
            .push(crate::bridge::WorkerCommand::QueueCancel(
                picker.session_id.clone(),
                id,
            ));
        self.notice = "Cancelling selected queued prompt…".into();
    }

    /// Inject a deterministic repository snapshot for gallery fixtures. Live
    /// sessions receive this state from the background Git probe instead.
    pub fn set_repo_status(&mut self, id: &str, status: doxa_worktrees::RepoStatus) {
        self.repo_cache
            .insert(id.to_owned(), (Some(status), Instant::now()));
    }

    /// Inject a verified project root for deterministic gallery fixtures.
    /// Live sessions receive this only from the background repository probe.
    pub fn set_project_root_fixture(&mut self, id: &str, root: PathBuf) {
        self.project_roots.insert(id.to_owned(), root);
    }

    /// Deterministic gallery state for the read-only memory menu. Live menus
    /// always use the LORE sidecar through `open_memory_menu`.

    pub(super) fn poll_repo(&mut self) -> bool {
        let mut changed = false;
        if let Some((id, cwd, epoch, receiver)) = self.repo_pending.take() {
            match receiver.try_recv() {
                Ok((status, root)) => {
                    if self.session_cwds.get(&id) == Some(&cwd)
                        && self.repo_epoch.get(&id).copied().unwrap_or_default() == epoch
                    {
                        changed = self.project_roots.get(&id) != root.as_ref() || self
                            .repo_cache
                            .get(&id)
                            .is_none_or(|(old, _)| *old != status);
                        if let Some(root) = root { self.project_roots.insert(id.clone(), root); }
                        else { self.project_roots.remove(&id); }
                        self.repo_cache.insert(id, (status, Instant::now()));
                    }
                }
                Err(TryRecvError::Disconnected) => {
                    if self.session_cwds.get(&id) == Some(&cwd)
                        && self.repo_epoch.get(&id).copied().unwrap_or_default() == epoch
                    {
                        changed = self
                            .repo_cache
                            .get(&id)
                            .is_some_and(|(old, _)| old.is_some());
                        self.project_roots.remove(&id);
                        self.repo_cache.insert(id, (None, Instant::now()));
                    }
                }
                Err(TryRecvError::Empty) => self.repo_pending = Some((id, cwd, epoch, receiver)),
            }
        }
        if self.repo_pending.is_some() {
            return changed;
        }
        let active_ids = std::iter::once(self.active_group)
            .chain((0..self.groups.len()).filter(|group| *group != self.active_group))
            .filter_map(|group| self.groups[group].active_id().map(str::to_owned))
            .collect::<Vec<_>>();
        let mut candidates = active_ids.clone();
        candidates.extend(self.sessions.iter().map(|session| session.id.clone())
            .filter(|id| !active_ids.contains(id)));
        for id in candidates {
            if self.offline_ids.contains(&id) {
                continue;
            }
            let Some(cwd) = self.session_cwds.get(&id).cloned() else {
                continue;
            };
            // Active tabs refresh their branch status. Hidden tabs need one
            // background probe for project identity; unknown is cached too.
            if !active_ids.contains(&id) && self.repo_cache.contains_key(&id) { continue; }
            if self
                .repo_cache
                .get(&id)
                .is_some_and(|(_, checked)| checked.elapsed() < Duration::from_secs(5))
            {
                continue;
            }
            let epoch = self.repo_epoch.get(&id).copied().unwrap_or_default();
            let (tx, rx) = mpsc::sync_channel(1);
            self.repo_pending = Some((id, cwd.clone(), epoch, rx));
            std::thread::spawn(move || {
                let _ = tx.send((doxa_worktrees::repo_status(&cwd), doxa_worktrees::project_root(&cwd)));
            });
            break;
        }
        changed
    }

    /// A transport loop drains this queue and sends each prompt to its session.
    pub fn take_prompts(&mut self) -> Vec<(String, String)> {
        std::mem::take(&mut self.pending_prompts)
    }

    pub fn take_answers(&mut self) -> Vec<(String, String, serde_json::Value)> {
        std::mem::take(&mut self.pending_answers)
    }

    pub(super) fn open_selected(&mut self) {
        let selected = self.rail_order().get(self.rail_selected).copied();
        if let Some(id) = selected
            .and_then(|index| self.sessions.get(index))
            .map(|session| session.id.clone())
        {
            if !self.groups[self.active_group].tabs.contains(&id)
                && !self.manual_tab_available_for(Some(&id))
            {
                return;
            }
            let tabs = &mut self.groups[self.active_group];
            if let Some(index) = tabs.tabs.iter().position(|tab| tab == &id) {
                tabs.active = index;
            } else {
                tabs.tabs.push(id);
                tabs.active = tabs.tabs.len() - 1;
            }
            tabs.scroll = 0;
            self.focus = Focus::Transcript;
        }
    }

    /// Close the active tab and hide it from the rail. The daemon and saved
    /// history remain available through /resume; Ctrl+Q exits the window.
    pub(super) fn detach_active_tab(&mut self) {
        if self.launching || !self.attaching_ids.is_empty() || self.clear_pending.is_some() {
            self.notice = "Wait for session launch/attach/clear before closing a pane".into();
            return;
        }
        let group = &mut self.groups[self.active_group];
        if group.active >= group.tabs.len() {
            self.notice = "No active tab to detach".into();
            return;
        }
        let id = group.tabs.remove(group.active);
        let offline = self.offline_ids.contains(&id);
        if offline {
            self.detached_this_run.retain(|detached| detached != &id);
            if self.rail_hover.as_deref() == Some(id.as_str()) {
                self.rail_hover = None;
            }
        }
        if !self.remote_mode && !crate::remote_client::valid_target(&id)
            && !offline && !self.detached_this_run.contains(&id) {
            self.detached_this_run.push(id.clone());
        }
        for job in &self.local_shell_jobs {
            if job.session == id {
                job.cancel();
            }
        }
        group.active = group.active.min(group.tabs.len().saturating_sub(1));
        group.scroll = 0;
        self.notice = if offline {
            format!("Past session closed · {id}")
        } else {
            format!("Tab closed · {id} · /resume to reopen")
        };

        if self.groups.iter().all(|group| group.tabs.is_empty()) {
            self.pane_tree = None;
            self.split_requested = false;
            self.groups.truncate(2);
            while self.groups.len() < 2 {
                self.groups.push(PaneGroup { tabs: Vec::new(), active: 0, scroll: 0 });
            }
            self.active_group = 0;
        } else if self.pane_tree.is_some() && self.groups[self.active_group].tabs.is_empty() {
            let removed = self.active_group;
            self.groups.remove(removed);
            self.pane_tree = self.pane_tree.take().and_then(|tree| tree.without(removed));
            self.active_group = removed.min(self.groups.len() - 1);
            self.input_drafts = std::mem::take(&mut self.input_drafts)
                .into_iter()
                .filter_map(|((index, id), draft)| {
                    (index != removed)
                        .then_some(((index - usize::from(index > removed), id), draft))
                })
                .collect();
            if self.groups.len() == 1 {
                self.groups.push(PaneGroup {
                    tabs: Vec::new(),
                    active: 0,
                    scroll: 0,
                });
                self.pane_tree = None;
                self.split_requested = false;
            }
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
        self.rail_selected = self.rail_selected.min(self.rail_order().len().saturating_sub(1));
        self.focus = Focus::Prompt;
    }

    /// Move the active session tab to the opposite group. Keep a source tab
    /// so moving never implicitly closes a pane group.
    pub(super) fn move_active_tab(&mut self, target: usize) -> bool {
        if target >= self.groups.len() || target == self.active_group {
            self.notice = "Choose another existing pane group".into();
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

    pub(super) fn previous_tab(&mut self) {
        let p = &mut self.groups[self.active_group];
        p.active = p.active.saturating_sub(1);
        p.scroll = 0;
    }
    pub(super) fn next_tab(&mut self) {
        let p = &mut self.groups[self.active_group];
        p.active = (p.active + 1).min(p.tabs.len().saturating_sub(1));
        p.scroll = 0;
    }

    pub(super) fn switch_prompt_pane(&mut self, forward: bool) {
        let count = self.pane_count();
        if count < 2 { return; }
        self.active_group = if forward {
            (self.active_group + 1) % count
        } else {
            (self.active_group + count - 1) % count
        };
        self.focus = Focus::Prompt;
    }
}
