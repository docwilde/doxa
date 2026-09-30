//! Own diff retrieval and exact tracked-hunk rejection transitions.
use super::{App, PendingRejection, RejectDraft, MAX_PENDING_PROMPTS, MAX_QUEUED_REJECTIONS};
use crate::diff_view;
use crate::markdown;
use crossterm::event::KeyCode;
use crossterm::event::KeyEvent;
use crossterm::event::KeyModifiers;
use std::collections::HashSet;
use std::sync::mpsc;
use std::sync::mpsc::TryRecvError;

impl App {
    pub(super) fn request_auto_diff(&mut self, id: &str) {
        if self.preferences.on("auto_diff")
            && !self.auto_diff_seen.contains(id)
            && !self.auto_diff_requests.iter().any(|v| v == id)
            && self.auto_diff_requests.len() < 128
        {
            self.auto_diff_requests.push_back(id.into());
        }
    }
    pub(super) fn poll_auto_diff(&mut self) -> bool {
        if !self.preferences.on("auto_diff") {
            self.auto_diff_requests.clear();
            self.auto_diff_ready.clear();
            return false;
        }
        if let Some((id, cwd, receiver)) = self.auto_diff_pending.take() {
            match receiver.try_recv() {
                Ok((digest, changed)) if self.session_cwds.get(&id) == Some(&cwd) => {
                    if let Some(baseline) = self.auto_diff_baseline.get(&id) {
                        if baseline != &digest && changed {
                            self.auto_diff_ready.insert(id.clone());
                        }
                    } else {
                        self.auto_diff_baseline.insert(id, digest);
                    }
                }
                Err(TryRecvError::Empty) => self.auto_diff_pending = Some((id, cwd, receiver)),
                _ => {}
            }
        }
        if self.auto_diff_pending.is_none() {
            if let Some(id) = self.auto_diff_requests.pop_front() {
                if let Some(cwd) = self.session_cwds.get(&id).cloned() {
                    let (tx, rx) = mpsc::sync_channel(1);
                    self.auto_diff_pending = Some((id, cwd.clone(), rx));
                    std::thread::spawn(move || {
                        use sha2::Digest;
                        let snapshot = diff_view::read(&cwd);
                        let changed = !snapshot.files.is_empty()
                            || snapshot.text.contains("Untracked files (names only");
                        let digest =
                            format!("{:x}", sha2::Sha256::digest(snapshot.text.as_bytes()));
                        let _ = tx.send((digest, changed));
                    });
                }
            }
        }
        let Some(id) = self.groups[self.active_group]
            .active_id()
            .map(str::to_owned)
            .filter(|id| self.auto_diff_ready.contains(id))
        else {
            return false;
        };
        self.auto_diff_ready.remove(&id);
        self.auto_diff_seen.insert(id);
        if self.diff_pane || self.diff_modal {
            return false;
        }
        if self.pane_tree.is_some() {
            self.notice = "Worktree changed · /diff opens the live diff".into();
            return true;
        }
        self.diff_pane = true;
        if self.layout(self.size).panes.is_none() {
            self.diff_pane = false;
            self.notice = "Worktree changed; terminal too narrow to auto-open diff · /diff".into();
        } else {
            self.load_diff();
            self.notice = "Live diff opened after this session changed its worktree".into();
        }
        true
    }

    pub(super) fn open_diff(&mut self) {
        if self.diff_modal {
            if self.rejections_for_target() > 0 {
                self.notice = "Wait for queued hunk rejections before closing this diff".into();
            } else {
                self.diff_modal = false;
            }
            return;
        }
        self.diff_modal = true;
        self.load_diff();
    }

    pub(super) fn rejections_for_target(&self) -> usize {
        let Some(id) = self.diff_target.as_deref() else {
            return 0;
        };
        self.diff_reject_queue
            .iter()
            .filter(|item| item.session_id == id)
            .count()
            + usize::from(
                self.diff_reject_active
                    .as_ref()
                    .is_some_and(|item| item.session_id == id),
            )
    }

    pub(super) fn queued_diff_rows(&self) -> HashSet<usize> {
        let mut rows = HashSet::new();
        let (Some(id), Some(current)) = (self.diff_target.as_deref(), self.diff_snapshot.as_ref())
        else {
            return rows;
        };
        for (index, hunk) in current.rejectable.iter().enumerate() {
            if self.diff_reject_queue.iter().any(|item| {
                item.session_id == id && current.same_hunk(index, &item.snapshot, item.index)
            }) || self.diff_reject_active.as_ref().is_some_and(|item| {
                item.session_id == id && current.same_hunk(index, &item.snapshot, item.index)
            }) {
                rows.insert(hunk.row);
            }
        }
        rows
    }

    pub(super) fn load_diff(&mut self) {
        self.diff_scroll = 0;
        self.diff_files.clear();
        self.diff_hunks.clear();
        self.diff_pending = None;
        self.diff_snapshot = None;
        self.diff_reject_confirm = None;
        let Some(id) = self.groups[self.active_group]
            .active_id()
            .map(str::to_owned)
        else {
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
        std::thread::spawn(move || {
            let _ = tx.send((id, diff_view::read(&cwd)));
        });
    }

    pub(super) fn poll_diff(&mut self) -> bool {
        if self.diff_reject_feedback.is_some() && self.pending_prompts.len() < MAX_PENDING_PROMPTS {
            self.pending_prompts
                .push(self.diff_reject_feedback.take().expect("retained feedback"));
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
                                self.notice = format!(
                                    "{note} · feedback retained until the prompt queue has room"
                                );
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
                    self.notice =
                        "Hunk rejection worker stopped unexpectedly; inspect the worktree.".into();
                    return true;
                }
                Err(TryRecvError::Empty) => {}
            }
        }
        if self.start_next_rejection() {
            return true;
        }
        if self.diff_pane
            && self.diff_target.as_deref() != self.groups[self.active_group].active_id()
        {
            self.load_diff();
            return true;
        }
        let Some(receiver) = &self.diff_pending else {
            return false;
        };
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
                self.diff_text =
                    "The active session changed while the diff loaded. Press R to refresh it."
                        .into();
                self.diff_scroll = 0;
                true
            }
            Err(TryRecvError::Disconnected) => {
                self.diff_pending = None;
                self.diff_text = "Diff worker unavailable.".into();
                true
            }
            Err(TryRecvError::Empty) => false,
        }
    }

    pub(super) fn diff_key(&mut self, key: KeyEvent) -> bool {
        if self.diff_reject_confirm.is_some() {
            match key.code {
                KeyCode::Enter => self.confirm_diff_reject(),
                KeyCode::Esc => {
                    self.diff_reject_confirm = None;
                    self.notice = "Hunk rejection cancelled".into();
                }
                KeyCode::Backspace => {
                    if let Some(draft) = &mut self.diff_reject_confirm {
                        draft.reason.pop();
                    }
                }
                KeyCode::Char(ch)
                    if !key
                        .modifiers
                        .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
                {
                    self.append_reject_reason(ch);
                }
                _ => return true,
            }
            return true;
        }
        if self.keybindings.matches(crate::keybindings::Action::Diff, key)
            || self.keybindings.matches(crate::keybindings::Action::DiffAlternate, key) {
            self.open_diff();
            return true;
        }
        match key.code {
            KeyCode::Esc => self.open_diff(),
            KeyCode::Char('r' | 'R') => {
                self.diff_modal = false;
                self.open_diff();
            }
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

    pub(super) fn begin_diff_reject(&mut self) {
        if self.diff_reject_queue.len() + usize::from(self.diff_reject_active.is_some())
            >= MAX_QUEUED_REJECTIONS
        {
            self.notice = "Too many queued hunk rejections".into();
            return;
        }
        let Some(id) = self.diff_target.as_ref() else {
            self.notice = "Select a session first".into();
            return;
        };
        if self.groups[self.active_group].active_id() != Some(id.as_str()) {
            self.notice = "Active session changed; refresh the diff before rejecting".into();
            return;
        }
        if !self.session_activity.contains_key(id) {
            self.notice = "Session activity is unknown; wait for a status update".into();
            return;
        }
        let Some(snapshot) = self.diff_snapshot.as_ref() else {
            self.notice = "Load the diff before rejecting an edit".into();
            return;
        };
        let Some(row) = snapshot
            .hunks
            .iter()
            .copied()
            .filter(|row| *row <= self.diff_scroll)
            .next_back()
            .or_else(|| snapshot.hunks.first().copied())
        else {
            self.notice = "No tracked hunk is visible in this diff".into();
            return;
        };
        let Some(index) = snapshot.rejectable.iter().position(|hunk| hunk.row == row) else {
            self.notice =
                "This hunk has file-level changes or a truncated patch; inspect it with git".into();
            return;
        };
        self.diff_scroll = snapshot.rejectable[index].row;
        self.diff_reject_confirm = Some(RejectDraft {
            index,
            reason: String::new(),
        });
        self.notice = format!(
            "Reject {} in {}? Type optional reason · Enter confirm · Esc cancel",
            snapshot.rejectable[index].header, snapshot.rejectable[index].path
        );
    }

    pub(super) fn confirm_diff_reject(&mut self) {
        let Some(draft) = self.diff_reject_confirm.take() else {
            return;
        };
        let Some(id) = self.diff_target.clone() else {
            return;
        };
        if self.groups[self.active_group].active_id() != Some(id.as_str()) {
            self.notice = "Active session changed; rejection cancelled".into();
            return;
        }
        if !self.session_activity.contains_key(&id) {
            self.notice = "Session activity is unknown; rejection cancelled".into();
            return;
        }
        let Some(snapshot) = self.diff_snapshot.clone() else {
            return;
        };
        if snapshot.rejectable.get(draft.index).is_none() {
            return;
        }
        if self.diff_reject_queue.iter().any(|queued| {
            queued.session_id == id
                && snapshot.same_hunk(draft.index, &queued.snapshot, queued.index)
        }) || self.diff_reject_active.as_ref().is_some_and(|active| {
            active.session_id == id
                && snapshot.same_hunk(draft.index, &active.snapshot, active.index)
        }) {
            self.notice = "This hunk is already queued for rejection".into();
            return;
        }
        if self.diff_reject_queue.len() + usize::from(self.diff_reject_active.is_some())
            >= MAX_QUEUED_REJECTIONS
        {
            self.notice = "Too many queued hunk rejections".into();
            return;
        }
        self.diff_reject_queue.push_back(PendingRejection {
            session_id: id,
            snapshot,
            index: draft.index,
            reason: draft.reason,
        });
        if self.start_next_rejection() {
            return;
        }
        self.notice = format!(
            "Hunk rejection queued until session is idle · {} pending",
            self.diff_reject_queue.len()
        );
    }

    pub(super) fn start_next_rejection(&mut self) -> bool {
        if self.diff_reject_pending.is_some()
            || self.diff_reject_feedback.is_some()
            || self.pending_prompts.len() >= MAX_PENDING_PROMPTS
        {
            return false;
        }
        let Some(position) = self.diff_reject_queue.iter().position(|item| {
            self.session_activity.get(&item.session_id).copied() == Some((false, 0))
        }) else {
            return false;
        };
        let job = self
            .diff_reject_queue
            .remove(position)
            .expect("queued rejection");
        let Some(hunk) = job.snapshot.rejectable.get(job.index) else {
            return false;
        };
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

    pub(super) fn jump_diff(&mut self, file: bool, forward: bool) {
        let marks = if file {
            &self.diff_files
        } else {
            &self.diff_hunks
        };
        let current = self.diff_scroll;
        let target = if forward {
            marks.iter().copied().find(|&row| row > current)
        } else {
            marks.iter().copied().rev().find(|&row| row < current)
        };
        if let Some(row) = target {
            self.diff_scroll = row;
        } else {
            self.notice = format!(
                "No {} {} in this diff",
                if forward { "next" } else { "previous" },
                if file { "file" } else { "hunk" }
            );
        }
    }
}
