//! Own diff retrieval and exact tracked-hunk rejection transitions.
use super::{unsafe_input_char, App, DiffCommentDraft, PendingRejection, RejectDraft, MAX_PENDING_PROMPTS, MAX_QUEUED_REJECTIONS, MAX_REJECT_REASON_BYTES};
use crate::diff_view;
use crate::markdown;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::layout::Rect;
use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::mpsc;
use std::sync::mpsc::TryRecvError;
use std::time::{Duration, Instant};

const DIFF_REFRESH_INTERVAL: Duration = Duration::from_secs(1);

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
        self.diff_refresh_due = None;
        self.diff_snapshot = None;
        self.diff_hover = None;
        self.diff_selected = None;
        self.diff_reject_confirm = None;
        self.diff_comment_draft = None;
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
        self.start_diff_read(id, cwd);
    }

    fn start_diff_read(&mut self, id: String, cwd: PathBuf) {
        let (tx, rx) = mpsc::sync_channel(1);
        self.diff_pending = Some((id.clone(), cwd.clone(), rx));
        std::thread::spawn(move || {
            let _ = tx.send(diff_view::read(&cwd));
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
        if (self.diff_pane || self.diff_modal)
            && self.diff_target.as_deref() != self.groups[self.active_group].active_id()
        {
            self.load_diff();
            return true;
        }
        // A confirmation refers to one exact snapshot and hunk index. Keep
        // that snapshot in place until the decision is made.
        if self.diff_reject_confirm.is_some() || self.diff_comment_draft.is_some() {
            return false;
        }
        if self.diff_pending.is_none()
            && (self.diff_pane || self.diff_modal)
            && self.diff_refresh_due.is_some_and(|due| Instant::now() >= due)
        {
            if let Some((id, cwd)) = self.diff_target.as_ref().and_then(|id| {
                self.session_cwds.get(id).map(|cwd| (id.clone(), cwd.clone()))
            }) {
                self.start_diff_read(id, cwd);
            } else {
                self.load_diff();
                return true;
            }
            self.diff_refresh_due = None;
            return false;
        }
        let Some((id, cwd, receiver)) = &self.diff_pending else {
            return false;
        };
        match receiver.try_recv() {
            Ok(snapshot) => {
                let id = id.clone();
                let cwd = cwd.clone();
                self.diff_pending = None;
                if self.groups[self.active_group].active_id() == Some(id.as_str())
                    && self.session_cwds.get(&id) == Some(&cwd) {
                    self.diff_refresh_due = Some(Instant::now() + DIFF_REFRESH_INTERVAL);
                    if self.diff_snapshot.as_ref().is_some_and(|old| old.text == snapshot.text) {
                        return false;
                    }
                    let old_scroll = self.diff_scroll;
                    let initial = self.diff_snapshot.is_none();
                    self.diff_hover = None;
                    self.diff_selected = None;
                    self.diff_text = markdown::sanitize(&snapshot.text);
                    self.diff_files = snapshot.files.clone();
                    self.diff_hunks = snapshot.hunks.clone();
                    self.diff_snapshot = Some(snapshot);
                    self.diff_scroll = if initial {
                        0
                    } else {
                        old_scroll.min(self.diff_text.lines().count().saturating_sub(1))
                    };
                    return true;
                }
                if self.diff_pane || self.diff_modal {
                    self.load_diff();
                }
                true
            }
            Err(TryRecvError::Disconnected) => {
                self.diff_pending = None;
                self.diff_refresh_due = Some(Instant::now() + DIFF_REFRESH_INTERVAL);
                self.diff_text = "Diff worker unavailable.".into();
                true
            }
            Err(TryRecvError::Empty) => false,
        }
    }

    pub(super) fn diff_key(&mut self, key: KeyEvent) -> bool {
        if self.diff_comment_draft.is_some() {
            match key.code {
                KeyCode::Enter => self.submit_diff_comment(),
                KeyCode::Esc => {
                    self.diff_comment_draft = None;
                    self.notice = "Diff comment cancelled".into();
                }
                KeyCode::Backspace => {
                    if let Some(draft) = &mut self.diff_comment_draft { draft.text.pop(); }
                }
                KeyCode::Char(ch) if !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) => {
                    self.append_diff_comment(ch);
                }
                _ => return true,
            }
            return true;
        }
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

    pub(super) fn diff_modal_area(area: Rect) -> Option<Rect> {
        let width = area.width.saturating_sub(4).min(120);
        let height = area.height.saturating_sub(4).min(36);
        (width >= 24 && height >= 8).then(|| Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        ))
    }

    pub(super) fn diff_has_footer(&self) -> bool {
        self.diff_selected.is_some() || self.diff_reject_confirm.is_some() || self.diff_comment_draft.is_some()
    }

    pub(super) fn diff_window(&self, area: Rect) -> (usize, usize) {
        let visible = usize::from(area.height.saturating_sub(if self.diff_has_footer() { 3 } else { 2 }));
        let total = self.diff_text.lines().count();
        (visible, self.diff_scroll.min(total.saturating_sub(visible)))
    }

    pub(super) fn diff_mouse(&mut self, area: Rect, mouse: MouseEvent) -> bool {
        let inside = area.contains(ratatui::layout::Position::new(mouse.column, mouse.row));
        if !inside {
            if mouse.kind == MouseEventKind::Moved && self.diff_hover.take().is_some() { return true; }
            return false;
        }
        match mouse.kind {
            MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
                self.diff_scroll = if mouse.kind == MouseEventKind::ScrollUp {
                    self.diff_scroll.saturating_sub(3)
                } else { self.diff_scroll.saturating_add(3) };
                self.diff_hover = None;
                true
            }
            MouseEventKind::Moved | MouseEventKind::Down(MouseButton::Left) => {
                if self.diff_reject_confirm.is_some() || self.diff_comment_draft.is_some() {
                    return mouse.kind == MouseEventKind::Down(MouseButton::Left);
                }
                if mouse.kind == MouseEventKind::Down(MouseButton::Left)
                    && self.diff_selected.is_some() && self.diff_has_footer()
                    && mouse.row == area.bottom().saturating_sub(2) {
                    if mouse.column >= area.x + 2 && mouse.column < area.x + 10 {
                        self.begin_diff_reject_selected();
                        return true;
                    }
                    if mouse.column >= area.x + 12 && mouse.column < area.x + 21 {
                        self.begin_diff_comment();
                        return true;
                    }
                }
                let had_footer = self.diff_has_footer();
                let (visible, start) = self.diff_window(area);
                let row = (mouse.column > area.x && mouse.column < area.right().saturating_sub(1)
                    && mouse.row > area.y && usize::from(mouse.row - area.y - 1) < visible)
                    .then(|| start + usize::from(mouse.row - area.y - 1));
                let index = row.and_then(|row| self.diff_snapshot.as_ref()?.hunk_at_row(row));
                if mouse.kind == MouseEventKind::Moved {
                    let changed = self.diff_hover != index;
                    self.diff_hover = index;
                    changed
                } else {
                    let changed = self.diff_selected != index;
                    self.diff_selected = index;
                    if !had_footer && index.is_some() && row == Some(start + visible.saturating_sub(1)) {
                        self.diff_scroll = start + 1;
                    }
                    changed
                }
            }
            _ => false,
        }
    }

    fn begin_diff_reject_selected(&mut self) {
        let Some(row) = self.diff_selected.and_then(|index|
            self.diff_snapshot.as_ref()?.hunks.get(index).copied()) else { return; };
        self.diff_scroll = row;
        self.begin_diff_reject();
    }

    fn begin_diff_comment(&mut self) {
        let Some(index) = self.diff_selected else { return; };
        self.diff_comment_draft = Some(DiffCommentDraft { index, text: String::new() });
        self.notice = "Comment on selected diff block · Enter send · Esc cancel".into();
    }

    pub(super) fn append_diff_comment(&mut self, ch: char) {
        if unsafe_input_char(ch) { return; }
        if let Some(draft) = &mut self.diff_comment_draft {
            if draft.text.len() + ch.len_utf8() <= MAX_REJECT_REASON_BYTES {
                draft.text.push(ch);
            } else {
                self.notice = "Diff comment limited to 1024 bytes".into();
            }
        }
    }

    fn submit_diff_comment(&mut self) {
        let Some(draft) = self.diff_comment_draft.as_ref() else { return; };
        if draft.text.trim().is_empty() {
            self.notice = "Write a comment before sending".into();
            return;
        }
        let Some(id) = self.diff_target.as_ref().filter(|id|
            self.groups[self.active_group].active_id() == Some(id.as_str())) else {
            self.notice = "Active session changed; comment cancelled".into();
            self.diff_comment_draft = None;
            return;
        };
        let Some(snapshot) = self.diff_snapshot.as_ref().filter(|snapshot|
            self.session_cwds.get(id).is_some_and(|cwd| snapshot.belongs_to(cwd))) else {
            self.notice = "Worktree changed; refresh the diff before commenting".into();
            self.diff_comment_draft = None;
            return;
        };
        if self.pending_prompts.len() >= MAX_PENDING_PROMPTS {
            self.notice = "Prompt queue full · comment retained".into();
            return;
        }
        let Some(message) = snapshot.comment_message(draft.index, draft.text.trim()) else {
            self.notice = "Diff block changed; comment cancelled".into();
            self.diff_comment_draft = None;
            return;
        };
        self.pending_prompts.push((id.clone(), message));
        self.diff_comment_draft = None;
        self.notice = "Diff comment queued for this session".into();
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
        if !self.session_cwds.get(id).is_some_and(|cwd| snapshot.belongs_to(cwd)) {
            self.notice = "Worktree changed; wait for the refreshed diff before rejecting".into();
            return;
        }
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
