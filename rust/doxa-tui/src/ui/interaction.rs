//! Reduce keyboard, mouse and clipboard input against the current owner.
use super::{
    input_request_option_at, belief_buttons, belief_review_buttons, chooser_visible_start,
    prompt_height, raw_visual_rows, safe_label, tool_cards, transcript_tools, unsafe_input_char,
    vendor_models, App, DragTarget, Focus, RailRow, Split, COMMANDS, ENGINE_CHOICES,
    MAX_ANSWER_BYTES, MAX_INPUT_BYTES, MAX_PENDING_PROMPTS, MAX_REJECT_REASON_BYTES,
    MIN_PANE_HEIGHT, MIN_PANE_WIDTH, MIN_RAIL_WIDTH, permission_choices, REVIEW_BODY_RESERVE,
};
use crate::lore_picker;
use crossterm::event::Event;
use crossterm::event::KeyCode;
use crossterm::event::KeyEvent;
use crossterm::event::KeyEventKind;
use crossterm::event::KeyModifiers;
use crossterm::event::MouseButton;
use crossterm::event::MouseEvent;
use crossterm::event::MouseEventKind;
use ratatui::layout::Rect;
use std::time::Instant;

impl App {
    pub fn handle(&mut self, event: Event) -> bool {
        let before = (
            self.active_group,
            self.groups[self.active_group]
                .active_id()
                .unwrap_or("")
                .to_owned(),
        );
        let selection_view = (self.size, self.groups[self.active_group].scroll);
        let mut changed = match event {
            Event::Resize(w, h) => {
                if let Some(manager) = &mut self.memory_manager {
                    manager.reset_review_visibility();
                }
                for request in &mut self.input_requests {
                    request.review_seen.set(0);
                    request.review_complete.set(false);
                    request.scroll = 0;
                }
                self.size = Rect::new(0, 0, w, h);
                self.drag = None;
                self.belief_preview.clear();
                self.memory_preview.clear();
                self.belief_pointer = None;
                self.rendered_belief_rows.borrow_mut().clear();
                self.chip_hover = None;
                self.link_hover = None;
                self.visible_links.borrow_mut().clear();
                *self.rendered_chip_hits.borrow_mut() = None;
                if let Some(review) = &mut self.fleet_review {
                    review.seen.set(0);
                    review.complete.set(false);
                    review.armed = false;
                    if let Some(info) = &mut self.chip_info {
                        info.scroll = 0;
                    }
                }
                if let Some(review) = &mut self.fleet_dependency_review {
                    review.reset_visibility();
                    if let Some(info) = &mut self.chip_info { info.scroll = 0; }
                }
                if self.chip_info.is_some() && self.active_chooser_rect().is_none() {
                    self.retire_operations();
                    self.chip_info = None;
                    self.fleet_review = None;
                    self.fleet_dependency_review = None;
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
                if self.delete_confirmation.is_some() && self.active_chooser_rect().is_none() {
                    self.delete_confirmation = None;
                    self.notice = "Transcript deletion cancelled · enlarge terminal to confirm".into();
                }
                if ((self.model_picker.is_some()
                    || self.effort_picker.is_some()
                    || self.engine_picker
                    || self.new_session.is_some())
                    && self.active_chooser_rect().is_none())
                    || (self.permission_picker.is_some() && self.active_chooser_rect().is_none())
                {
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
            Event::FocusGained => {
                self.window_focused = true;
                false
            }
            Event::FocusLost => {
                self.window_focused = false;
                false
            }
            Event::Paste(text) => self.paste(&text),
            Event::Mouse(mouse) => self.mouse(mouse),
            _ => false,
        };
        let after = (
            self.active_group,
            self.groups[self.active_group]
                .active_id()
                .unwrap_or("")
                .to_owned(),
        );
        let selection_owner = crate::selection::Owner {
            pane: self.active_group,
            session: after.1.clone(),
        };
        if selection_view.0 != self.size
            || (before == after && selection_view.1 != self.groups[self.active_group].scroll)
            || (before != after
                && !self
                    .transcript_selection
                    .borrow()
                    .belongs_to(&selection_owner))
        {
            self.transcript_selection.borrow_mut().clear();
        }
        self.refresh_vendor_credentials();
        if self.operations_menu.as_ref().is_some_and(|menu| menu.editing_credential())
            && (self.should_quit || !self.chip_info.as_ref().is_some_and(|info| info.kind == "operations")
                || self.engine_picker || self.model_picker.is_some() || self.effort_picker.is_some()
                || self.settings_menu.is_some() || self.history_modal) {
            self.retire_operations();
        }
        self.finish_prompt_owner_transition(before);
        if let Some(id) = self.pending_rename.take() {
            if self.groups[self.active_group].active_id() == Some(id.as_str()) {
                if self.rename_draft_backup.as_ref().is_none_or(|(owner, _, _)| *owner != self.prompt_owner()) {
                    self.rename_draft_backup = Some((self.prompt_owner(), self.input.clone(), self.input_cursor));
                }
                if let Some(session) = self.sessions.iter().find(|session| session.id == id) {
                    self.input = format!("/rename {}", session.title);
                    self.input_cursor = self.input.len();
                    self.focus = Focus::Prompt;
                    self.notice = "Edit the session name and press Enter · Esc cancels".into();
                }
            }
        }
        if self.input.is_empty() && self.action_draft.is_some() {
            let (owner, draft, cursor) = self.action_draft.take().unwrap();
            if owner
                == (
                    self.active_group,
                    self.groups[self.active_group]
                        .active_id()
                        .unwrap_or("")
                        .to_owned(),
                )
            {
                self.input = draft;
                self.input_cursor = cursor;
            } else {
                self.input_drafts.insert(owner, (draft, cursor));
            }
        }
        self.sync_chooser_state();
        self.tick_chip_hover(Instant::now());
        changed |= self.tick_belief_preview(Instant::now());
        if matches!(self.focus, Focus::Chip(_) | Focus::Rail)
            && !self.focus_ring().contains(&self.focus)
        {
            self.focus = Focus::Prompt;
        }
        changed
    }

    pub(super) fn prompt_owner(&self) -> (usize, String) {
        (
            self.active_group,
            self.groups[self.active_group]
                .active_id()
                .unwrap_or("")
                .to_owned(),
        )
    }

    /// Every activation path uses the same draft transaction, including daemon
    /// replies arriving between keyboard events. Drafts belong to a pane and
    /// session together; moving a tab deliberately carries its active draft.
    pub(super) fn finish_prompt_owner_transition(&mut self, before: (usize, String)) {
        let after = self.prompt_owner();
        if before != after && self.rename_draft_backup.as_ref().is_some_and(|(owner, _, _)| owner == &before) {
            if let Some((_, draft, cursor)) = self.rename_draft_backup.take() {
                self.input = draft;
                self.input_cursor = cursor;
            }
        }
        if self.operations_menu.as_ref().is_some_and(|menu| menu.editing_credential())
            && before != after {
            self.retire_operations(); self.chip_info = None;
        }
        if before != after {
            self.branch_picker = None;
            let moved_active_tab = std::mem::take(&mut self.moved_active_tab)
                && !before.1.is_empty()
                && before.1 == after.1
                && !self
                    .groups
                    .get(before.0)
                    .is_some_and(|group| group.tabs.contains(&before.1))
                && self.groups[after.0].tabs.contains(&after.1);
            if moved_active_tab {
                // The draft follows its tab rather than remaining under the
                // old pane key. A command has already consumed its own input.
                self.input_drafts.remove(&before);
                self.input_drafts.remove(&after);
            } else {
                if self
                    .groups
                    .get(before.0)
                    .is_some_and(|group| before.1.is_empty() || group.tabs.contains(&before.1))
                {
                    self.input_drafts
                        .insert(before, (std::mem::take(&mut self.input), self.input_cursor));
                } else {
                    self.input.clear();
                    self.input_cursor = 0;
                }
                (self.input, self.input_cursor) =
                    self.input_drafts.remove(&after).unwrap_or_default();
            }
            self.slash_selected = 0;
            self.slash_dismissed = false;
        }
        if self.focus != Focus::Rail {
            if let Some(position) = self.rail_order().iter().position(|index| {
                self.sessions[*index].id == after.1
            }) {
                self.rail_selected = position;
            }
        }
        if !after.1.is_empty() {
            self.unread_sessions.remove(&after.1);
        }
    }

    pub(super) fn restore_rename_draft(&mut self) {
        if let Some((owner, draft, cursor)) = self.rename_draft_backup.take() {
            if owner == self.prompt_owner() {
                self.input = draft;
                self.input_cursor = cursor;
            }
        }
    }

    pub(super) fn clipboard_target(&self) -> crate::clipboard::Target {
        crate::clipboard::Target {
            pane: self.active_group,
            session: self.groups[self.active_group]
                .active_id()
                .unwrap_or("")
                .to_owned(),
            draft: self.input.clone(),
            cursor: self.input_cursor,
        }
    }
    pub(super) fn poll_clipboard(&mut self) -> bool {
        let Some(result) = self
            .clipboard_job
            .as_ref()
            .and_then(crate::clipboard::Job::poll)
        else {
            return false;
        };
        let target = self.clipboard_job.take().unwrap().target.clone();
        let text = match result {
            Ok(text) => text,
            Err(_) => {
                self.clipboard_secret_owner = None;
                self.notice = "Clipboard read failed · use terminal Ctrl+Shift+V".into();
                return true;
            }
        };
        if let Some(token) = self.clipboard_secret_owner.take() {
            let active = self.active_group == target.pane
                && self.groups[self.active_group].active_id().unwrap_or("") == target.session;
            if active && self.chip_info.as_ref().is_some_and(|info| info.kind == "operations") {
                if let Some(menu) = self.operations_menu.as_mut().filter(|menu| menu.credential_token() == Some(token)) {
                    menu.paste(&text);
                    if let Some(info) = &mut self.chip_info { info.lines = menu.lines(usize::from(self.size.width)); }
                    return true;
                }
            }
            self.notice = "Credential paste discarded; editor changed".into();
            return true;
        }
        let exists = self.groups.get(target.pane).is_some_and(|group| {
            if target.session.is_empty() {
                group.tabs.is_empty()
            } else {
                group.tabs.contains(&target.session)
            }
        });
        if !exists
            || self
                .input_requests
                .iter()
                .any(|request| request.session_id == target.session)
        {
            self.notice =
                "Clipboard paste discarded · original prompt is no longer available".into();
            return true;
        }
        let active = self.active_group == target.pane
            && self.groups[self.active_group].active_id().unwrap_or("") == target.session;
        let (draft, cursor) = if active {
            (&mut self.input, &mut self.input_cursor)
        } else {
            let entry = self
                .input_drafts
                .entry((target.pane, target.session))
                .or_default();
            (&mut entry.0, &mut entry.1)
        };
        if *draft != target.draft || *cursor != target.cursor || !draft.is_char_boundary(*cursor) {
            self.notice = "Clipboard paste discarded · prompt draft changed while reading".into();
            return true;
        }
        let available = MAX_INPUT_BYTES.saturating_sub(draft.len());
        let mut clean = String::new();
        let mut chars = text.chars().peekable();
        let mut truncated = false;
        while let Some(ch) = chars.next() {
            let ch = if ch == '\r' {
                if chars.peek() == Some(&'\n') {
                    chars.next();
                }
                '\n'
            } else if ch == '\t' {
                ' '
            } else {
                ch
            };
            if unsafe_input_char(ch) && ch != '\n' {
                continue;
            }
            if clean.len() + ch.len_utf8() > available {
                truncated = true;
                break;
            }
            clean.push(ch);
        }
        draft.insert_str(*cursor, &clean);
        *cursor += clean.len();
        if active {
            self.slash_selected = 0;
            self.slash_dismissed = false;
        }
        self.notice = if truncated {
            "Clipboard pasted into original draft · prompt limit reached"
        } else {
            "Clipboard pasted into original draft"
        }
        .into();
        true
    }

    pub(super) fn paste(&mut self, text: &str) -> bool {
        if let Some(menu) = &mut self.operations_menu {
            let handled = menu.paste(text);
            if let Some(info) = &mut self.chip_info { info.lines = menu.lines(usize::from(self.size.width)); }
            return handled;
        }
        if let Some(picker) = self.lore_picker.as_mut().filter(|picker| {
            !picker.proposal_mode
                && picker.belief_review.is_none()
                && picker.evidence.is_none()
                && !picker.resolving
        }) {
            for ch in text
                .chars()
                .filter(|ch| !unsafe_input_char(*ch) || ch.is_whitespace())
            {
                let ch = if ch.is_whitespace() { ' ' } else { ch };
                if !lore_picker::append_filter_char(&mut picker.query, ch) {
                    break;
                }
            }
            picker.offset = 0;
            picker.selected = 0;
            self.belief_filter_due = Some(Instant::now());
            return true;
        }
        if self.memory_manager.is_none()
            && self
                .chip_info
                .as_ref()
                .is_some_and(|info| info.kind == "memory")
        {
            if let Some(list) = self.memory_list.as_mut() {
                if list.owner.as_ref().is_some_and(|(id, cwd)| {
                    self.groups[self.active_group].active_id() != Some(id.as_str())
                        || self.session_cwds.get(id).and_then(|path| path.to_str())
                            != Some(cwd.as_str())
                }) {
                    return false;
                }
                for ch in text
                    .chars()
                    .filter(|ch| !unsafe_input_char(*ch) || ch.is_whitespace())
                {
                    let ch = if ch.is_whitespace() { ' ' } else { ch };
                    if !lore_picker::append_filter_char(&mut list.query, ch) {
                        break;
                    }
                }
                self.chip_info.as_mut().unwrap().scroll = 0;
                list.selected = 0;
                return true;
            }
        }
        if let Some(index) = self.active_request_index().filter(|&index| {
            self.input_requests[index].freeform() && !self.input_requests[index].sending
        }) {
            let clean: String = text.chars().filter(|c| !unsafe_input_char(*c)).collect();
            let request = &mut self.input_requests[index];
            if request.free_text.len() + clean.len() > MAX_ANSWER_BYTES / 2 {
                self.notice = "Answer paste exceeds limit".into();
                return true;
            }
            request.free_text.insert_str(request.free_cursor, &clean);
            request.free_cursor += clean.len();
            return true;
        }
        if self.diff_comment_draft.is_some() {
            for ch in text.chars() {
                self.append_diff_comment(if ch.is_whitespace() { ' ' } else { ch });
            }
            return true;
        }
        if self.diff_reject_confirm.is_some() {
            for ch in text.chars() {
                self.append_reject_reason(if ch.is_whitespace() { ' ' } else { ch });
                if self
                    .diff_reject_confirm
                    .as_ref()
                    .is_some_and(|draft| draft.reason.len() >= MAX_REJECT_REASON_BYTES)
                {
                    break;
                }
            }
            return true;
        }
        if self.focus != Focus::Prompt
            || self.active_request_index().is_some()
            || self.stop_confirmation.is_some()
            || self.delete_confirmation.is_some()
            || self.lore_picker.is_some()
            || self.settings_menu.is_some()
            || self.new_session.is_some()
            || self.repo_picker.is_some()
            || self.model_picker.is_some()
            || self.effort_picker.is_some()
            || self.permission_picker.is_some()
            || self.engine_picker
            || self.action_menu
            || self.history_modal
            || self.queue_picker.is_some()
            || self.attach_picker.is_some()
            || self.branch_picker.is_some()
            || self.diff_modal
            || self.map_modal
            || self.tool_modal
        {
            return false;
        }
        let mut clean = String::new();
        let available = MAX_INPUT_BYTES.saturating_sub(self.input.len());
        let mut chars = text.chars().peekable();
        let mut truncated = false;
        while let Some(ch) = chars.next() {
            let ch = if ch == '\r' {
                if chars.peek() == Some(&'\n') {
                    chars.next();
                }
                '\n'
            } else if ch == '\t' {
                ' '
            } else {
                ch
            };
            if unsafe_input_char(ch) && ch != '\n' {
                continue;
            }
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
        if truncated {
            self.notice = "Prompt input limit reached · paste truncated".into();
        }
        !clean.is_empty() || truncated
    }

    pub(super) fn append_reject_reason(&mut self, ch: char) {
        if ch.is_control() {
            return;
        }
        if let Some(draft) = &mut self.diff_reject_confirm {
            if draft.reason.len() + ch.len_utf8() <= MAX_REJECT_REASON_BYTES {
                draft.reason.push(ch);
            } else {
                self.notice = "Rejection reason limited to 1024 bytes".into();
            }
        }
    }

    pub(super) fn insert_input(&mut self, ch: char) -> bool {
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

    pub(super) fn move_input_vertical(&mut self, down: bool) -> bool {
        let before = &self.input[..self.input_cursor];
        let column = before.rsplit('\n').next().unwrap_or("").chars().count();
        let line_start = before.rfind('\n').map_or(0, |i| i + 1);
        let target_start = if down {
            let Some(end) = self.input[self.input_cursor..].find('\n') else {
                return false;
            };
            self.input_cursor + end + 1
        } else {
            if line_start == 0 {
                return false;
            }
            self.input[..line_start - 1]
                .rfind('\n')
                .map_or(0, |i| i + 1)
        };
        let target_end = self.input[target_start..]
            .find('\n')
            .map_or(self.input.len(), |i| target_start + i);
        self.input_cursor = target_start
            + self.input[target_start..target_end]
                .char_indices()
                .nth(column)
                .map_or(target_end - target_start, |(i, _)| i);
        true
    }

    pub(super) fn key(&mut self, key: KeyEvent) -> bool {
        use crate::keybindings::Action as KeyAction;
        if self.rename_draft_backup.is_some() && self.focus == Focus::Prompt {
            if key.code == KeyCode::Esc {
                self.restore_rename_draft();
                self.notice = "Rename cancelled".into();
                return true;
            }
            if key.code == KeyCode::Enter
                && self.input != "/rename" && !self.input.starts_with("/rename ") {
                self.notice = "Rename editor: keep /rename before the name, or press Esc".into();
                return true;
            }
        }
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        let alt = key.modifiers.contains(KeyModifiers::ALT);
        if self.restart_job.is_some() || self.restart_waiting {
            self.notice =
                "Finalizing idle sessions for restart; saved tabs will be restored".into();
            return true;
        }
        if ctrl && matches!(key.code, KeyCode::Char('c' | 'C')) && !self.link_interaction_blocked()
        {
            let owner = crate::selection::Owner {
                pane: self.active_group,
                session: self.groups[self.active_group]
                    .active_id()
                    .unwrap_or("")
                    .to_owned(),
            };
            let selected = self.transcript_selection.borrow().text(&owner);
            if let Some(text) = selected.filter(|text| !text.is_empty()) {
                self.pending_clipboard_copy = Some(crate::clipboard::osc52(&text));
                self.notice = "Clipboard copy queued for terminal · OSC52 support required".into();
                return true;
            }
            if key.modifiers.contains(KeyModifiers::SHIFT) || key.code == KeyCode::Char('C') {
                self.notice = "Drag to select transcript text before copying".into();
                return true;
            }
        }
        if key.code == KeyCode::Esc
            && !self.link_interaction_blocked()
            && self.transcript_selection.borrow_mut().clear()
        {
            return true;
        }
        if ctrl && matches!(key.code, KeyCode::Char('v' | 'V')) {
            if let Some(token) = self.operations_menu.as_ref().and_then(|menu| menu.credential_token()) {
                self.clipboard_job = None;
                self.clipboard_secret_owner = Some(token);
                let mut target = self.clipboard_target(); target.draft.clear(); target.cursor = 0;
                match crate::clipboard::Job::start(target) {
                    Ok(job) => self.clipboard_job = Some(job),
                    Err(_) => { self.clipboard_secret_owner = None; self.notice = "Clipboard reader unavailable · use terminal Ctrl+Shift+V".into(); },
                }
                return true;
            }
        }
        if ctrl
            && matches!(key.code, KeyCode::Char('v' | 'V'))
            && self.focus == Focus::Prompt
            && !self.link_interaction_blocked()
        {
            self.clipboard_job = None;
            self.clipboard_secret_owner = None;
            match crate::clipboard::Job::start(self.clipboard_target()) {
                Ok(job) => {
                    self.clipboard_job = Some(job);
                    self.notice = "Reading clipboard into this prompt draft…".into();
                }
                Err(_) => {
                    self.notice = "Clipboard reader unavailable · use terminal Ctrl+Shift+V".into()
                }
            }
            return true;
        }
        if ctrl && key.code == KeyCode::Char('c') {
            let id = self.groups[self.active_group].active_id();
            if self
                .local_shell_jobs
                .iter()
                .any(|job| Some(job.session.as_str()) == id)
            {
                for job in &self.local_shell_jobs {
                    if Some(job.session.as_str()) == id {
                        job.cancel();
                    }
                }
                self.notice = "Cancelling local shell command".into();
                return true;
            }
        }
        if ctrl && key.code == KeyCode::Char('c') && self.fleet_controller.is_some() {
            self.fleet_controller.as_mut().unwrap().cancel();
            self.notice = "Cancelling fleet controller; waiting for slot teardown and process reaping".into();
            return true;
        }
        if self.keybindings.matches(KeyAction::Quit, key) && self.fleet_controller.is_some() {
            // Controller::Drop cancels its child; detach first so quitting the
            // frontend leaves the fleet's budgeted run alive.
            self.fleet_controller.take().unwrap().detach();
            self.should_quit = true;
            return true;
        }
        if self.keybindings.matches(KeyAction::Quit, key) {
            if self.delete_after_stop.is_some() || self.session_delete_pending.is_some() {
                self.notice = "Wait for transcript deletion to finish before quitting".into();
                return true;
            }
            if !self.diff_reject_queue.is_empty()
                || self.diff_reject_active.is_some()
                || self.diff_reject_feedback.is_some()
            {
                self.notice = "Wait for queued hunk rejections before leaving".into();
                return true;
            }
            self.should_quit = true;
            return true;
        }
        if self.fleet_review.is_some() {
            return self.fleet_review_key(key);
        }
        if self.fleet_dependency_review.is_some() {
            return self.fleet_dependency_review_key(key);
        }
        if self.delete_confirmation.is_some() {
            return self.delete_confirmation_key(key);
        }
        if self.keybindings.matches(KeyAction::DeleteTranscript, key) {
            self.open_delete_confirmation();
            return true;
        }
        if self.keybindings.matches(KeyAction::Stop, key) {
            self.stop_active_session();
            return true;
        }
        if self.keybindings.matches(KeyAction::CloseTab, key)
            || (self.focus == Focus::Tabs
                && self.keybindings.matches(KeyAction::CloseTabAlternate, key)) {
            self.detach_active_tab();
            return true;
        }
        // A credential owns all typing while its editor exists. Provider input
        // requests must never capture a key intended for this transient field.
        if let Some(menu) = self.operations_menu.as_mut().filter(|menu| menu.editing_credential()) {
            menu.key(key);
            if let Some(info) = &mut self.chip_info { info.lines = menu.lines(usize::from(self.size.width)); }
            return true;
        }
        if self.active_request_index().is_some() {
            if self.navigation_key(key) { return true; }
            return self.request_key(key);
        }
        if self.stop_confirmation.is_some() {
            return self.stop_confirmation_key(key);
        }
        if self.chip_info.is_some() {
            if self.fleet_menu.is_some() {
                if key.code == KeyCode::Esc {
                    self.fleet_menu = None;
                    self.chip_info = None;
                    return true;
                }
                if matches!(key.code, KeyCode::PageUp | KeyCode::PageDown) {
                    let info = self.chip_info.as_mut().unwrap();
                    info.scroll = if key.code == KeyCode::PageUp {
                        info.scroll.saturating_sub(8)
                    } else {
                        info.scroll
                            .saturating_add(8)
                            .min(info.lines.len().saturating_sub(1))
                    };
                    return true;
                }
                let menu = self.fleet_menu.as_mut().unwrap();
                menu.key(key);
                self.chip_info.as_mut().unwrap().lines = menu.display();
                return true;
            }
            if let Some(menu) = &mut self.operations_menu {
                menu.key(key);
                if menu.closed() {
                    self.retire_operations();
                    self.chip_info = None;
                } else if let Some(info) = &mut self.chip_info {
                    info.lines = menu.lines(usize::from(self.size.width));
                }
                return true;
            }
            if self.memory_manager.is_some() {
                let current = self.groups[self.active_group].active_id().and_then(|id| {
                    self.session_cwds
                        .get(id)
                        .and_then(|p| p.to_str())
                        .map(|cwd| (id, cwd))
                });
                let manager = self.memory_manager.as_mut().unwrap();
                if current != Some((manager.owner.0.as_str(), manager.owner.1.as_str())) {
                    self.memory_manager = None;
                    self.chip_info = None;
                    self.notice = "Session changed; reopen memory".into();
                    return true;
                }
                if key.code == KeyCode::Esc && !manager.editing() {
                    self.memory_manager = None;
                    self.chip_info = None;
                    return true;
                }
                return manager.key(key);
            }
            if self.chip_info.as_ref().is_some_and(|i| i.kind == "memory")
                && key.code != KeyCode::Char('M')
                && self.edit_memory_filter(key)
            {
                return true;
            }
            if key.code == KeyCode::Char('M')
                && self.chip_info.as_ref().is_some_and(|i| i.kind == "memory")
            {
                if let Some(owner) = self.chip_info.as_ref().and_then(|i| i.owner.clone()) {
                    self.memory_menu_pending = None;
                    self.memory_manager = Some(crate::memory_menu::Manager::new(owner));
                }
                return true;
            }
            if key.code == KeyCode::Esc
                || (self
                    .chip_info
                    .as_ref()
                    .is_some_and(|info| info.kind == "about")
                    && matches!(key.code, KeyCode::Enter | KeyCode::Char('q')))
            {
                self.chip_info = None;
                self.memory_menu_pending = None;
                return true;
            }
            if self.chip_info.as_ref().is_some_and(|info| info.kind == "memory") {
                let page = self.active_chooser_rect().map_or(1, |area| usize::from(area.height.saturating_sub(3)).max(1));
                if let Some(list) = self.memory_list.as_mut() {
                    let len = list.indices().len();
                    let step = match key.code {
                        KeyCode::Up => -1,
                        KeyCode::Down => 1,
                        KeyCode::PageUp => -(page as isize),
                        KeyCode::PageDown => page as isize,
                        _ => return false,
                    };
                    list.selected = list.selected.saturating_add_signed(step).min(len.saturating_sub(1));
                    if let Some(info) = self.chip_info.as_mut() {
                        if list.selected < info.scroll { info.scroll = list.selected; }
                        if list.selected >= info.scroll + page { info.scroll = list.selected + 1 - page; }
                    }
                    return true;
                }
            }
            let codegraph_area = self.active_chooser_rect();
            if let Some(info) = self.chip_info.as_mut().filter(|info| {
                matches!(
                    info.kind,
                    "memory" | "usage" | "context" | "help" | "sessions" | "about" | "remote_history" | "native_plugin" | "native_status" | "codegraph"
                )
            }) {
                if info.kind == "codegraph" {
                    let width = codegraph_area.map_or(1, |area| usize::from(area.width.saturating_sub(3)).max(1));
                    let count = super::codegraph_viewer::wrapped_lines(&info.lines, width).len();
                    let page = codegraph_area.map_or(1, |area| usize::from(area.height.saturating_sub(2)).max(1));
                    match key.code {
                        KeyCode::Up => info.scroll = info.scroll.saturating_sub(1),
                        KeyCode::Down => info.scroll = info.scroll.saturating_add(1).min(count.saturating_sub(page)),
                        KeyCode::PageUp => info.scroll = info.scroll.saturating_sub(page),
                        KeyCode::PageDown => info.scroll = info.scroll.saturating_add(page).min(count.saturating_sub(page)),
                        _ => return false,
                    }
                    return true;
                }
                if info.kind=="remote_history" && key.code==KeyCode::PageUp && info.scroll==0 {
                    let id=info.owner.as_ref().map(|owner|owner.0.clone());
                    if let Some(id)=id {
                        if let Some(before)=self.remote_history_before.get(&id).copied() {
                            if self.remote_history_loading.insert(id.clone()) {
                                self.pending_remote_history.push((id,before));
                                self.notice="Loading older remote turns…".into();
                            }
                        }else{self.notice="Beginning of remote transcript".into();}
                    }
                    return true;
                }
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
        if self.settings_menu.is_some() {
            return self.settings_menu_key(key);
        }
        if self.repo_picker.is_some() {
            return self.repo_picker_key(key);
        }
        if self.lore_picker.is_some() {
            return self.lore_picker_key(key);
        }
        if self.new_session.is_some() {
            return self.new_session_key(key);
        }
        if self.model_picker.is_some() {
            return self.model_picker_key(key);
        }
        if self.effort_picker.is_some() {
            return self.effort_picker_key(key);
        }
        if self.permission_picker.is_some() {
            return self.permission_picker_key(key);
        }
        if self.engine_picker {
            return self.engine_picker_key(key);
        }
        if self.action_menu {
            return self.action_key(key);
        }
        if self.history_modal {
            return self.history_key(key);
        }
        if self.queue_picker.is_some() {
            return self.queue_key(key);
        }
        if self.attach_picker.is_some() {
            return self.attach_picker_key(key);
        }
        if self.branch_picker.is_some() {
            return self.branch_picker_key(key);
        }
        if self.diff_modal {
            return self.diff_key(key);
        }
        if key.code == KeyCode::Esc && self.action_draft.is_some() {
            let (owner, draft, cursor) = self.action_draft.take().unwrap();
            if owner
                == (
                    self.active_group,
                    self.groups[self.active_group]
                        .active_id()
                        .unwrap_or("")
                        .to_owned(),
                )
            {
                self.input = draft;
                self.input_cursor = cursor;
            } else {
                self.input_drafts.insert(owner, (draft, cursor));
            }
            return true;
        }
        if self.diff_pane && (self.diff_reject_confirm.is_some() || self.diff_comment_draft.is_some()) {
            return self.diff_key(key);
        }
        if self.map_modal {
            let owner = self.groups[self.active_group]
                .active_id()
                .unwrap_or("")
                .to_owned();
            return match key.code {
                _ if key.code == KeyCode::Esc || self.keybindings.matches(KeyAction::PeerMap, key) => {
                    self.map_modal = false;
                    true
                }
                KeyCode::Up => {
                    if self.peer_map.focus_messages() { self.peer_map.scroll_messages(&owner, 1); }
                    else { self.peer_map.move_selected(&owner, -1); }
                    true
                }
                KeyCode::Down => {
                    if self.peer_map.focus_messages() { self.peer_map.scroll_messages(&owner, -1); }
                    else { self.peer_map.move_selected(&owner, 1); }
                    true
                }
                KeyCode::Tab => { self.peer_map.toggle_focus(); true }
                KeyCode::PageUp => { self.peer_map.scroll_messages(&owner, 5); true }
                KeyCode::PageDown => { self.peer_map.scroll_messages(&owner, -5); true }
                KeyCode::Char('r' | 'R') => {
                    self.pending_peer_refresh = Some(owner);
                    true
                }
                _ => false,
            };
        }
        if self.keybindings.matches(KeyAction::Settings, key) {
            self.open_settings_menu();
            return true;
        }
        if self.keybindings.matches(KeyAction::PeerMap, key) {
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
        if self.keybindings.matches(KeyAction::NewTab, key) {
            if self.active_remote() { self.local_attach(""); } else { self.open_engine_picker(); }
            return true;
        }
        if self.keybindings.matches(KeyAction::Tools, key) {
            self.tool_modal = true;
            self.tool_scroll = 0;
            self.tool_selected = self.active_tool_cards().len().saturating_sub(1);
            return true;
        }
        if self.keybindings.matches(KeyAction::Palette, key) {
            self.action_menu = true;
            self.action_selected = 0;
            self.action_query.clear();
            self.drag = None;
            return true;
        }
        if self.keybindings.matches(KeyAction::Search, key) {
            self.open_history();
            return true;
        }
        if self.keybindings.matches(KeyAction::Lore, key) {
            self.open_lore_picker();
            return true;
        }
        if self.keybindings.matches(KeyAction::Model, key) {
            self.open_model_picker();
            return true;
        }
        if self.keybindings.matches(KeyAction::Effort, key) {
            self.open_effort_picker();
            return true;
        }
        if self.keybindings.matches(KeyAction::Permission, key) {
            self.open_permission_picker();
            return true;
        }
        if self.keybindings.matches(KeyAction::Engine, key) {
            self.open_engine_picker();
            return true;
        }
        if self.keybindings.matches(KeyAction::Diff, key)
            || self.keybindings.matches(KeyAction::DiffAlternate, key) {
            self.open_diff();
            return true;
        }
        if key.code == KeyCode::F(4) {
            if self.pane_tree.is_some() {
                self.open_diff();
                return true;
            }
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
                KeyCode::F(5) => {
                    self.load_diff();
                    return true;
                }
                KeyCode::Char('r' | 'R') if alt => {
                    self.begin_diff_reject();
                    return true;
                }
                KeyCode::PageUp if alt => {
                    self.diff_scroll = self.diff_scroll.saturating_sub(10);
                    return true;
                }
                KeyCode::PageDown if alt => {
                    self.diff_scroll = self.diff_scroll.saturating_add(10);
                    return true;
                }
                KeyCode::Char('n' | 'N') if alt => {
                    self.jump_diff(true, true);
                    return true;
                }
                KeyCode::Char('b' | 'B') if alt => {
                    self.jump_diff(true, false);
                    return true;
                }
                KeyCode::Char('j' | 'J') if alt => {
                    self.jump_diff(false, true);
                    return true;
                }
                KeyCode::Char('k' | 'K') if alt => {
                    self.jump_diff(false, false);
                    return true;
                }
                _ => {}
            }
        }
        if self.navigation_key(key) { return true; }
        if self.keybindings.matches(KeyAction::Sidebar, key) {
            self.rail_visible = !self.rail_visible;
            self.persist_sidebar();
            return true;
        }
        if self.keybindings.matches(KeyAction::SplitHorizontal, key) {
            self.split_active_pane(Split::Horizontal);
            return true;
        }
        if self.keybindings.matches(KeyAction::SplitVertical, key) {
            self.split_active_pane(Split::Vertical);
            return true;
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
                    KeyCode::Enter
                        if suggestions[self.slash_selected.min(suggestions.len() - 1)].0
                            != self.input =>
                    {
                        return self.complete_slash();
                    }
                    _ => {}
                }
            }
        }
        match key.code {
            KeyCode::BackTab | KeyCode::Tab => {
                self.cycle_focus(
                    key.code == KeyCode::BackTab || key.modifiers.contains(KeyModifiers::SHIFT),
                );
                true
            }
            KeyCode::Enter if matches!(self.focus, Focus::Chip(_)) => {
                if let Focus::Chip(kind) = self.focus {
                    self.activate_chip(kind, self.active_group);
                }
                true
            }
            KeyCode::Left if self.focus == Focus::Tabs => {
                self.previous_tab();
                true
            }
            KeyCode::Right if self.focus == Focus::Tabs => {
                self.next_tab();
                true
            }
            KeyCode::Enter if self.focus == Focus::Tabs => {
                self.focus = Focus::Prompt;
                true
            }
            KeyCode::Esc => {
                self.focus = Focus::Prompt;
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
            KeyCode::Char('[') if self.focus == Focus::Transcript => {
                self.select_tool_section(false)
            }
            KeyCode::Char(']') if self.focus == Focus::Transcript => self.select_tool_section(true),
            KeyCode::Enter | KeyCode::Char(' ') if self.focus == Focus::Transcript => {
                self.toggle_selected_tool_section()
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
            KeyCode::Left if self.focus == Focus::Prompt => {
                if let Some((index, _)) = self.input[..self.input_cursor].char_indices().next_back()
                {
                    self.input_cursor = index;
                    true
                } else {
                    false
                }
            }
            KeyCode::Right if self.focus == Focus::Prompt => {
                if let Some(ch) = self.input[self.input_cursor..].chars().next() {
                    self.input_cursor += ch.len_utf8();
                    true
                } else {
                    false
                }
            }
            KeyCode::Home if self.focus == Focus::Prompt => {
                self.input_cursor = self.input[..self.input_cursor]
                    .rfind('\n')
                    .map_or(0, |i| i + 1);
                true
            }
            KeyCode::End if self.focus == Focus::Prompt => {
                self.input_cursor = self.input[self.input_cursor..]
                    .find('\n')
                    .map_or(self.input.len(), |i| self.input_cursor + i);
                true
            }
            KeyCode::Up if self.focus == Focus::Prompt => self.move_input_vertical(false),
            KeyCode::Down if self.focus == Focus::Prompt => self.move_input_vertical(true),
            KeyCode::Backspace if self.focus == Focus::Prompt => {
                if let Some((index, _)) = self.input[..self.input_cursor].char_indices().next_back()
                {
                    self.input.drain(index..self.input_cursor);
                    self.input_cursor = index;
                    self.slash_selected = 0;
                    self.slash_dismissed = false;
                    true
                } else {
                    false
                }
            }
            KeyCode::Delete if self.focus == Focus::Prompt => {
                if let Some(ch) = self.input[self.input_cursor..].chars().next() {
                    self.input
                        .drain(self.input_cursor..self.input_cursor + ch.len_utf8());
                    self.slash_selected = 0;
                    self.slash_dismissed = false;
                    true
                } else {
                    false
                }
            }
            KeyCode::Enter
                if self.focus == Focus::Prompt
                    && (key.modifiers.contains(KeyModifiers::SHIFT) || alt) =>
            {
                self.insert_input('\n')
            }
            KeyCode::Enter if self.focus == Focus::Prompt && ctrl => {
                // Ctrl+Enter and a terminal-normalized control newline can
                // share this code. Neither may submit a prompt by accident.
                self.notice = "Ctrl+Enter is ambiguous here · use Alt+Enter for a newline".into();
                true
            }
            KeyCode::Char('j') if self.focus == Focus::Prompt && ctrl => self.insert_input('\n'),
            KeyCode::Char(c) if self.focus == Focus::Prompt && !ctrl && !alt => {
                if unsafe_input_char(c) {
                    false
                } else {
                    self.insert_input(c)
                }
            }
            KeyCode::Enter if self.focus == Focus::Prompt => {
                if !self.input.is_empty() {
                    if self.input.contains('\n')
                        && self
                            .input
                            .split_whitespace()
                            .next()
                            .is_some_and(|name| COMMANDS.iter().any(|row| row.name == name)
                                || self.native_plugin_commands.iter().any(|row| row.name == name))
                    {
                        self.notice = "DOXA commands must be a single line".into();
                        return true;
                    }
                    if self.input.starts_with('!') {
                        self.submit_keyboard_shell();
                        return true;
                    }
                    if self.dispatch_native_plugin_command() {
                        return true;
                    }
                    if self.dispatch_prompt_command() {
                        return true;
                    }
                    if self.submit_local_command() {
                        return true;
                    }
                    if let Some(id) = self.groups[self.active_group].active_id() {
                        if self.offline_ids.contains(id) {
                            self.notice = "Archived transcript is read-only".into();
                        } else if self.active_remote() && (self.input == "/peers" || self.input == "/mesh" || self.input == "/msg" || self.input.starts_with("/msg ")) {
                            self.notice = "Peer controls are available on the session host".into();
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
                                self.pending_peer_messages.push((
                                    id.to_owned(),
                                    target.to_owned(),
                                    body.to_owned(),
                                ));
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

    /// Window navigation remains available while a session waits for input.
    /// The request stays owned by its original tab when focus moves away.
    fn navigation_key(&mut self, key: KeyEvent) -> bool {
        use crate::keybindings::Action as KeyAction;
        if self.keybindings.matches(KeyAction::PreviousTab, key) {
            self.previous_tab(); self.focus = Focus::Prompt; true
        } else if self.keybindings.matches(KeyAction::NextTab, key) {
            self.next_tab(); self.focus = Focus::Prompt; true
        } else if self.keybindings.matches(KeyAction::PreviousPane, key) {
            self.switch_prompt_pane(false); true
        } else if self.keybindings.matches(KeyAction::NextPane, key) {
            self.switch_prompt_pane(true); true
        } else if self.keybindings.matches(KeyAction::NextPaneAlternate, key) {
            self.active_group = (self.active_group + 1)
                % if self.pane_tree.is_some() { self.pane_count() } else { 2 };
            self.split_requested = true;
            self.focus = Focus::Prompt;
            true
        } else { false }
    }

    pub(super) fn active_tool_cards(&self) -> &[tool_cards::ToolCard] {
        self.groups[self.active_group]
            .active_id()
            .map(|id| self.tool_cards.for_session(id))
            .unwrap_or(&[])
    }

    pub(super) fn tool_sections_for_active(&self) -> Option<(String, usize)> {
        let id = self.groups[self.active_group].active_id()?.to_owned();
        let transcript = &self.sessions.iter().find(|s| s.id == id)?.transcript;
        let (_, sections) = transcript_tools::render(transcript, 80, None, None);
        Some((id, sections.len()))
    }

    fn remember_tool_selection(&mut self, id: String, key: transcript_tools::FoldKey) {
        if !self.selected_tool_sections.contains_key(&id) && self.selected_tool_sections.len() >= 64 {
            if let Some(oldest) = self.selected_tool_sections.keys().next().cloned() {
                self.selected_tool_sections.remove(&oldest);
                self.expanded_tool_sections.remove(&oldest);
            }
        }
        self.selected_tool_sections.insert(id, key);
    }

    pub(super) fn select_tool_section(&mut self, forward: bool) -> bool {
        let Some((id, _)) = self.tool_sections_for_active() else {
            return false;
        };
        let visible: Vec<transcript_tools::FoldKey> = self
            .visible_tool_sections
            .borrow()
            .iter()
            .filter(|(_, group, session, _)| *group == self.active_group && *session == id)
            .map(|(_, _, _, section)| section.clone())
            .collect();
        if visible.is_empty() {
            return false;
        }
        let current = self.selected_tool_sections.get(&id).cloned();
        let next = match current.and_then(|value| visible.iter().position(|index| *index == value))
        {
            Some(position) if forward => (position + 1).min(visible.len() - 1),
            Some(position) => position.saturating_sub(1),
            None if forward => 0,
            None => visible.len() - 1,
        };
        self.remember_tool_selection(id, visible[next].clone());
        true
    }

    pub(super) fn toggle_selected_tool_section(&mut self) -> bool {
        let Some((id, count)) = self.tool_sections_for_active() else {
            return false;
        };
        if count == 0 {
            return false;
        }
        let visible: Vec<transcript_tools::FoldKey> = self
            .visible_tool_sections
            .borrow()
            .iter()
            .filter(|(_, group, session, _)| *group == self.active_group && *session == id)
            .map(|(_, _, _, section)| section.clone())
            .collect();
        let selected = self
            .selected_tool_sections
            .get(&id)
            .cloned()
            .filter(|section| visible.contains(section))
            .or_else(|| visible.last().cloned())
            .unwrap_or(transcript_tools::FoldKey::Section(count - 1));
        self.remember_tool_selection(id.clone(), selected.clone());
        self.toggle_tool_section(id, selected);
        true
    }

    pub(super) fn toggle_tool_section(&mut self, id: String, selected: transcript_tools::FoldKey) {
        if !self.expanded_tool_sections.contains_key(&id) && self.expanded_tool_sections.len() >= 64
        {
            if let Some(oldest) = self.expanded_tool_sections.keys().next().cloned() {
                self.expanded_tool_sections.remove(&oldest);
                self.selected_tool_sections.remove(&oldest);
            }
        }
        let expanded = self.expanded_tool_sections.entry(id).or_default();
        if !expanded.remove(&selected) {
            if expanded.len() >= 64 {
                if let Some(oldest) = expanded.iter().cloned().min() {
                    expanded.remove(&oldest);
                }
            }
            expanded.insert(selected);
        }
    }

    pub(super) fn tool_key(&mut self, key: KeyEvent) -> bool {
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

    pub(super) fn stop_confirmation_fits(&self) -> bool {
        self.size.width >= 40 && self.size.height >= 12
    }

    pub(super) fn open_delete_confirmation(&mut self) {
        if self.active_remote() { self.notice = "Remote transcripts must be deleted on their host".into(); return; }
        if self.active_request_index().is_some() {
            self.notice = "Resolve the session input request before deleting its transcript".into(); return;
        }
        if self.session_stop_pending.is_some() || self.delete_after_stop.is_some() || self.session_delete_pending.is_some() {
            self.notice = "Wait for the current session action to finish".into(); return;
        }
        if self.launching || !self.attaching_ids.is_empty() || self.clear_pending.is_some() {
            self.notice = "Wait for session launch/attach/clear before deleting".into(); return;
        }
        let Some(id) = self.groups[self.active_group].active_id().map(str::to_owned) else {
            self.notice = "Select a session to delete".into(); return;
        };
        let cwd = self.session_cwds.get(&id).cloned()
            .or_else(|| self.history_entries.get(&id).and_then(|entry| entry.cwd.clone()));
        let Some(cwd) = cwd else {
            self.notice = "Transcript directory is unknown; deletion refused".into(); return;
        };
        self.delete_confirmation = Some((id, cwd, 1));
        if self.active_chooser_rect().is_none() {
            self.delete_confirmation = None;
            self.notice = "Enlarge active pane to confirm deletion".into();
        }
    }

    pub(super) fn delete_confirmation_key(&mut self, key: KeyEvent) -> bool {
        match key.code {
            KeyCode::Esc => { self.delete_confirmation = None; true }
            KeyCode::Up | KeyCode::Down => {
                if let Some((_, _, selected)) = &mut self.delete_confirmation { *selected = usize::from(*selected == 0); }
                true
            }
            KeyCode::Enter | KeyCode::Char('d' | 'D') => {
                if key.code == KeyCode::Enter && self.delete_confirmation.as_ref().is_some_and(|(_, _, selected)| *selected != 0) {
                    self.delete_confirmation = None;
                    self.notice = "Transcript deletion cancelled".into();
                    return true;
                }
                let Some((id, cwd, _)) = self.delete_confirmation.take() else { return true; };
                if crate::history::saved_session(&id, &cwd).is_none() {
                    self.notice = "Saved DOXA transcript could not be verified; nothing deleted".into();
                    return true;
                }
                let live = match crate::discovery::sessions() {
                    Ok(sessions) => sessions.iter().any(|session| session.id == id),
                    Err(_) => {
                        self.notice = "Transcript preserved · cannot verify daemon state".into();
                        return true;
                    }
                };
                self.detach_active_tab();
                if !live {
                    self.start_session_delete(id, cwd);
                } else {
                    self.local_sessions_stop(crate::sessions::Action::Kill(id.clone()));
                    if self.session_stop_pending.is_some() {
                        self.delete_after_stop = Some((id, cwd));
                        self.notice = "Stopping daemon before transcript deletion…".into();
                    } else {
                        self.notice = "Transcript preserved; daemon stop could not start · use /resume".into();
                    }
                }
                true
            }
            _ => true,
        }
    }

    pub(super) fn open_stop_confirmation(&mut self) {
        if self.active_remote() { self.notice = "Remote sessions must be stopped on their host".into(); return; }
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

    pub(super) fn stop_confirmation_key(&mut self, key: KeyEvent) -> bool {
        match key.code {
            KeyCode::Esc | KeyCode::Char('n' | 'N') => self.stop_confirmation = None,
            KeyCode::Char('y' | 'Y')
                if (key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT)
                    && self.stop_confirmation_fits() =>
            {
                let id = self.stop_confirmation.take().unwrap();
                self.pending_stops.push(id.clone());
                self.notice = format!("Requesting stop · {}", safe_label(&id));
            }
            _ => return false,
        }
        true
    }

    pub(super) fn active_request_index(&self) -> Option<usize> {
        // Keep both input and rendering with the secret editor until the user
        // explicitly saves or cancels. Requests remain queued in their store.
        if self.operations_menu.as_ref().is_some_and(|menu| menu.editing_credential()) {
            return None;
        }
        let id = self.groups[self.active_group].active_id()?;
        self.input_requests.iter().position(|r| r.session_id == id)
    }

    pub(super) fn request_key(&mut self, key: KeyEvent) -> bool {
        let index = self.active_request_index().unwrap();
        if self.input_requests[index].sending {
            self.notice = "Answer already sent · awaiting resolution".into();
            return true;
        }
        if self.input_requests[index].freeform() {
            let request = &mut self.input_requests[index];
            match key.code {
                KeyCode::Char(c)
                    if !unsafe_input_char(c)
                        && !key
                            .modifiers
                            .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
                {
                    if request.free_text.len() + c.len_utf8() <= MAX_ANSWER_BYTES / 2 {
                        request.free_text.insert(request.free_cursor, c);
                        request.free_cursor += c.len_utf8();
                    }
                    return true;
                }
                KeyCode::Left => {
                    request.free_cursor = request.free_text[..request.free_cursor]
                        .char_indices()
                        .next_back()
                        .map_or(0, |(i, _)| i);
                    return true;
                }
                KeyCode::Right => {
                    if let Some(c) = request.free_text[request.free_cursor..].chars().next() {
                        request.free_cursor += c.len_utf8();
                    }
                    return true;
                }
                KeyCode::Home => {
                    request.free_cursor = 0;
                    return true;
                }
                KeyCode::End => {
                    request.free_cursor = request.free_text.len();
                    return true;
                }
                KeyCode::Backspace => {
                    if let Some((i, _)) = request.free_text[..request.free_cursor]
                        .char_indices()
                        .next_back()
                    {
                        request.free_text.drain(i..request.free_cursor);
                        request.free_cursor = i;
                    }
                    return true;
                }
                KeyCode::Delete => {
                    if let Some(c) = request.free_text[request.free_cursor..].chars().next() {
                        request
                            .free_text
                            .drain(request.free_cursor..request.free_cursor + c.len_utf8());
                    }
                    return true;
                }
                _ => {}
            }
        }
        let kind = self.input_requests[index].kind.clone();
        let can_grant_for_session = self.input_requests[index].can_grant_for_session();
        if kind != "ask_user" {
            let page = self.active_chooser_rect().map_or(1, |menu| menu.height.saturating_sub(2).max(1));
            let choices = if can_grant_for_session { 3 } else { 2 };
            match key.code {
                KeyCode::Up => {
                    let selected = self.input_requests[index].selected;
                    self.input_requests[index].selected = if selected <= 1 { choices } else { selected - 1 };
                    return true;
                }
                KeyCode::Down => {
                    let selected = self.input_requests[index].selected;
                    self.input_requests[index].selected = if selected >= choices { 1 } else { selected + 1 };
                    return true;
                }
                KeyCode::PageUp => {
                    self.input_requests[index].scroll =
                        self.input_requests[index].scroll.saturating_sub(page);
                    return true;
                }
                KeyCode::PageDown => {
                    self.input_requests[index].scroll =
                        self.input_requests[index].scroll.saturating_add(page);
                    return true;
                }
                _ => {}
            }
        }
        let answer = if kind == "ask_user" {
            let count = self.input_requests[index].option_count();
            match key.code {
                KeyCode::Esc => Some(serde_json::json!({"declined":true,"cancelled":true})),
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
                KeyCode::Enter if count > 0 || self.input_requests[index].freeform() => {
                    self.choose_question(index)
                }
                _ => None,
            }
        } else {
            let choice = match key.code {
                KeyCode::Esc => Some("deny"),
                KeyCode::Char('d' | 'D')
                    if key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT =>
                {
                    Some("deny")
                }
                KeyCode::Enter if self.input_requests[index].selected == 2 => Some("deny"),
                KeyCode::Enter if can_grant_for_session && self.input_requests[index].selected == 3 => Some("session"),
                KeyCode::Char('l' | 'L') if can_grant_for_session
                    && (key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT) => Some("session"),
                KeyCode::Char('a' | 'A') | KeyCode::Enter
                    if key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT =>
                {
                    Some("once")
                }
                _ => None,
            };
            match choice {
                Some("deny") => Some(serde_json::json!({"decision":"deny"})),
                Some(scope) => {
                    if !self.active_chooser_rect().is_some_and(|menu| menu.width >= 20 && menu.height >= 5) {
                        self.notice = "Enlarge the active pane to review and approve".into();
                        return true;
                    }
                    if self.input_requests[index].require_full_review
                        && (!self.input_requests[index].review_available
                            || !self.input_requests[index].review_complete.get())
                    {
                        self.notice = "Read the complete request before approving".into();
                        return true;
                    }
                    Some(if scope == "session" {
                        serde_json::json!({"decision":"allow","scope":"session"})
                    } else {
                        serde_json::json!({"decision":"allow"})
                    })
                }
                None => None,
            }
        };
        if let Some(answer) = answer {
            if serde_json::to_vec(&answer).map_or(true, |bytes| bytes.len() > MAX_ANSWER_BYTES) {
                self.notice = "Answer too large · Esc to decline".into();
                return true;
            }
            let request = &mut self.input_requests[index];
            request.grant_on_success = can_grant_for_session && answer["scope"] == "session";
            request.sending = true;
            self.pending_answers
                .push((request.session_id.clone(), request.id.clone(), answer));
            self.notice = "Sending answer".into();
        }
        true
    }

    pub(super) fn choose_question(&mut self, index: usize) -> Option<serde_json::Value> {
        let request = &mut self.input_requests[index];
        let question = request.questions.get(request.step)?;
        let choice = if request.freeform() {
            if request.free_text.trim().is_empty() {
                self.notice = "Type an answer before submitting".into();
                return None;
            }
            request.free_text.clone()
        } else {
            question
                .options
                .get(request.selected.checked_sub(1)?)?
                .label
                .clone()
        };
        let key = question.id.as_ref().unwrap_or(&question.question).clone();
        request
            .answers
            .insert(key, serde_json::Value::String(choice));
        if request.step + 1 == request.questions.len() {
            Some(serde_json::json!({"answers": request.answers}))
        } else {
            request.step += 1;
            request.selected = 1;
            request.free_text.clear();
            request.free_cursor = 0;
            request.scroll = 0;
            None
        }
    }

    pub(super) fn hover_chooser(&mut self, column: u16, row: u16) -> bool {
        let Some(menu) = self.active_chooser_rect() else {
            return false;
        };
        if column <= menu.x
            || column >= menu.right().saturating_sub(1)
            || row <= menu.y
            || row >= menu.bottom().saturating_sub(1)
        {
            return false;
        }
        if let Some(manager)=self.memory_manager.as_mut() {
            if row>=menu.y+2 && row<menu.bottom().saturating_sub(2) {
                return manager.hover(usize::from(row-menu.y-2),usize::from(menu.height.saturating_sub(4)));
            }
            return false;
        }
        if self.chip_info.as_ref().is_some_and(|info| info.kind == "memory") {
            if let (Some(info), Some(list)) = (self.chip_info.as_ref(), self.memory_list.as_mut()) {
                if row >= menu.y + 2 && row < menu.bottom() - 1 && column < menu.right() - 2 {
                    let index = info.scroll + usize::from(row - menu.y - 2);
                    if index < list.indices().len() && list.selected != index {
                        list.selected = index;
                        return true;
                    }
                }
            }
            return false;
        }
        if let Some(index) = self.active_request_index() {
            if self.input_requests[index].sending {
                return false;
            }
            if let Some(option) = input_request_option_at(&self.input_requests[index], menu, row) {
                if self.input_requests[index].selected != option {
                    self.input_requests[index].selected = option;
                    return true;
                }
            }
            return false;
        }
        let visible = usize::from(menu.height.saturating_sub(3)).max(1);
        let attach_len = self
            .attach_picker
            .as_ref()
            .map(|_| self.attach_matches().len());
        if let Some(settings) = self.settings_menu.as_mut() {
            if row >= menu.y + 3 {
                if let Some(index) = settings
                    .visible_indices(menu.height)
                    .get(usize::from(row - menu.y - 3))
                    .copied()
                {
                    if settings.selected != index {
                        settings.selected = index;
                        return true;
                    }
                }
            }
        } else if let Some(picker) = self.branch_picker.as_mut() {
            if row < menu.y + 2 {
                return false;
            }
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - menu.y - 2);
            if index < picker.branches.len() && picker.selected != index {
                picker.selected = index;
                return true;
            }
        } else if let Some(picker) = self.repo_picker.as_mut() {
            if row < menu.y + 2 {
                return false;
            }
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - menu.y - 2);
            if index < picker.paths.len() && picker.selected != index {
                picker.selected = index;
                return true;
            }
        } else if self.engine_picker {
            let offset = if menu.height >= 10 { 4 } else { 2 };
            if row < menu.y + offset {
                return false;
            }
            let visible = usize::from(menu.height.saturating_sub(offset + 1)).max(1);
            let start =
                chooser_visible_start(&self.chooser_view_start, self.engine_selected, visible);
            let index = start + usize::from(row - menu.y - offset);
            if index < ENGINE_CHOICES.len() && self.engine_selected != index {
                self.engine_selected = index;
                return true;
            }
        } else if let Some(form) = self.new_session.as_mut() {
            let first = menu.y + if menu.height >= 8 { 4 } else { 2 };
            let fields = if vendor_models(form.engine).is_empty() {
                3
            } else {
                4
            };
            if row >= first && usize::from(row - first) <= fields {
                let index = usize::from(row - first);
                if form.field != index {
                    form.field = index;
                    return true;
                }
            }
        } else if let Some((id, selected)) = self.permission_picker.as_mut() {
            let offset = if menu.height >= 10 { 4 } else { 2 };
            if row < menu.y + offset {
                return false;
            }
            let visible = usize::from(menu.height.saturating_sub(offset + 1)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, *selected, visible);
            let index = start + usize::from(row - menu.y - offset);
            let count = permission_choices(self.session_identity.get(id).and_then(|identity| identity.0.as_deref())).len();
            if index < count && *selected != index {
                *selected = index;
                return true;
            }
        } else if let Some(picker) = self.effort_picker.as_mut() {
            if row < menu.y + 3 {
                return false;
            }
            let visible = usize::from(menu.height.saturating_sub(4)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - menu.y - 3);
            if index < picker.levels.len() && picker.selected != index {
                picker.selected = index;
                return true;
            }
        } else if let Some(picker) = self.model_picker.as_mut() {
            let offset = picker.row_offset();
            if row < menu.y + offset {
                return false;
            }
            let visible = picker.visible_rows(menu.height);
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - menu.y - offset);
            if index < picker.models.len() && picker.selected != index {
                picker.selected = index;
                return true;
            }
        } else if let Some(picker) = self.attach_picker.as_mut() {
            if row < menu.y + 2 {
                return false;
            }
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - menu.y - 2);
            if index < attach_len.unwrap_or(0) && picker.selected != index {
                picker.selected = index;
                return true;
            }
        } else if let Some(picker) = self.queue_picker.as_mut() {
            if row < menu.y + 2 {
                return false;
            }
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - menu.y - 2);
            if index < picker.rows.len() && picker.selected != index {
                picker.selected = index;
                return true;
            }
        } else if self.history_modal {
            if row < menu.y + 2 {
                return false;
            }
            if let Some((index, header, _)) = self
                .history_rows(visible)
                .get(usize::from(row - menu.y - 2))
            {
                if *header && self.history_selected != *index {
                    self.history_selected = *index;
                    return true;
                }
            }
        } else if let Some(picker) = self.lore_picker.as_mut() {
            if picker.review.is_some()
                || picker.belief_review.is_some()
                || picker.evidence.is_some()
                || picker.pending.is_some()
                || self.belief_filter_due.is_some()
            {
                return false;
            }
            let first = if picker.proposal_mode {
                menu.y + 3
            } else {
                menu.y + 2
            };
            if row < first {
                return false;
            }
            let reserve = if picker.proposal_mode { 5 } else { 3 };
            let visible = usize::from(menu.height.saturating_sub(reserve)).max(1);
            if usize::from(row - first) >= visible {
                return false;
            }
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let index = start + usize::from(row - first);
            let count = if picker.proposal_mode {
                picker.proposals.len()
            } else {
                picker.rows.len()
            };
            if index < count && picker.selected != index {
                picker.selected = index;
                if !picker.proposal_mode {
                    picker.filter_focused = false;
                }
                return true;
            }
        } else if self.action_menu {
            if row < menu.y + 2 {
                return false;
            }
            let visible = usize::from(menu.height.saturating_sub(3)).max(1);
            let start =
                chooser_visible_start(&self.chooser_view_start, self.action_selected, visible);
            let index = start + usize::from(row - menu.y - 2);
            if index < self.action_rows().len() && self.action_selected != index {
                self.action_selected = index;
                return true;
            }
        } else if !self.slash_suggestions().is_empty() {
            let visible = usize::from(menu.height.saturating_sub(2)).max(1);
            let start =
                chooser_visible_start(&self.chooser_view_start, self.slash_selected, visible);
            let index = start + usize::from(row - menu.y - 1);
            if index < self.slash_suggestions().len() && self.slash_selected != index {
                self.slash_selected = index;
                return true;
            }
        }
        false
    }

    // Wheel targets are geometric, independent of keyboard focus. Inline
    // chooser navigation never activates a row or changes the prompt owner.
    fn wheel_chooser(&mut self, mouse: MouseEvent) -> Option<bool> {
        let code = match mouse.kind {
            MouseEventKind::ScrollUp => KeyCode::Up,
            MouseEventKind::ScrollDown => KeyCode::Down,
            _ => return None,
        };
        let point = ratatui::layout::Position::new(mouse.column, mouse.row);
        if !self.active_chooser_rect().is_some_and(|area| area.contains(point)) {
            return None;
        }
        let key = KeyEvent::new(code, KeyModifiers::NONE);
        if self.active_request_index().is_some() { return None; }
        if self.engine_picker { return Some(self.engine_picker_key(key)); }
        if self.model_picker.is_some() { return Some(self.model_picker_key(key)); }
        if self.effort_picker.is_some() { return Some(self.effort_picker_key(key)); }
        if self.permission_picker.is_some() { return Some(self.permission_picker_key(key)); }
        if self.settings_menu.is_some() {
            return Some(self.settings_menu_key(key));
        }
        if let Some(menu) = self.operations_menu.as_mut() {
            if menu.editing_credential() { return Some(false); }
            menu.key(key);
            return Some(true);
        }
        None
    }
    fn wheel_transcript(&mut self, mouse: MouseEvent) -> bool {
        let up = match mouse.kind {
            MouseEventKind::ScrollUp => true,
            MouseEventKind::ScrollDown => false,
            _ => return false,
        };
        let point = ratatui::layout::Position::new(mouse.column, mouse.row);
        let layout = self.layout(self.size);
        let panes = layout.panes.map(|panes| panes.into_iter().enumerate().collect::<Vec<_>>())
            .unwrap_or_else(|| vec![(self.active_group, layout.body)]);
        for (index, pane) in panes {
            if self.diff_pane && index != self.active_group { continue; }
            if !self.pane_regions(index, pane)[1].contains(point)
                || self.groups[index].active_id().is_none() { continue; }
            let old = self.groups[index].scroll;
            self.groups[index].scroll = if up { old.saturating_add(3) } else { old.saturating_sub(3) };
            if self.groups[index].scroll != old {
                let owner = crate::selection::Owner {pane:index,session:self.groups[index].active_id().unwrap().to_owned()};
                if self.transcript_selection.borrow().belongs_to(&owner) {
                    self.transcript_selection.borrow_mut().clear();
                }
            }
            return true;
        }
        false
    }
    pub(super) fn mouse(&mut self, mouse: MouseEvent) -> bool {
        if matches!(mouse.kind, MouseEventKind::Moved | MouseEventKind::Down(_)
            | MouseEventKind::ScrollUp | MouseEventKind::ScrollDown) {
            let inside = self.layout(self.size).rail.is_some_and(|rail|
                mouse.column > rail.x && mouse.column < rail.right().saturating_sub(1)
                && mouse.row > rail.y && mouse.row < rail.bottom().saturating_sub(1));
            if inside || self.rail_pointer_inside != inside {
                self.rail_last_interaction = Instant::now();
            }
            self.rail_pointer_inside = inside;
        }
        if mouse.kind == MouseEventKind::Down(MouseButton::Left)
            && self.rail_session_at(mouse.column, mouse.row).is_none() {
            self.last_rail_click = None;
        }
        if self.delete_confirmation.is_some() {
            if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
                let Some(area) = self.active_chooser_rect() else { self.delete_confirmation = None; return true; };
                if mouse.column >= area.x + 1 && mouse.column < area.right().saturating_sub(1) {
                    if mouse.row == area.y + 2 || mouse.row == area.y + 3 {
                        if let Some((_, _, selected)) = &mut self.delete_confirmation {
                            *selected = usize::from(mouse.row == area.y + 3);
                        }
                        return self.delete_confirmation_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
                    }
                }
                self.delete_confirmation = None;
            }
            return true;
        }
        if self.diff_modal {
            if let Some(area) = Self::diff_modal_area(self.size) {
                return self.diff_mouse(area, mouse);
            }
            return false;
        }
        if self.diff_pane && (self.diff_comment_draft.is_some() || self.diff_reject_confirm.is_some())
            && mouse.kind == MouseEventKind::Down(MouseButton::Left) {
            if let Some(panes) = self.layout(self.size).panes {
                let pane = panes[(self.active_group + 1) % panes.len()];
                if !pane.contains(ratatui::layout::Position::new(mouse.column, mouse.row)) {
                    self.notice = "Finish or cancel the diff action before switching panes".into();
                    return true;
                }
            }
        }
        if self.diff_pane && mouse.kind == MouseEventKind::Moved && !self.link_interaction_blocked() {
            if let Some(panes) = self.layout(self.size).panes {
                let pane = panes[(self.active_group + 1) % panes.len()];
                let changed = self.diff_mouse(pane, mouse);
                if changed || pane.contains(ratatui::layout::Position::new(mouse.column, mouse.row)) {
                    return changed;
                }
            }
        }
        let mut rail_hover_changed = false;
        if mouse.kind == MouseEventKind::Moved {
            let rail_hover = (!self.link_interaction_blocked())
                .then(|| self.rail_session_at(mouse.column, mouse.row).map(str::to_owned))
                .flatten();
            rail_hover_changed = self.rail_hover != rail_hover;
            self.rail_hover = rail_hover;
        }
        if self.map_modal {
            let owner = self.groups[self.active_group].active_id().unwrap_or("").to_owned();
            return self.peer_map.mouse(self.size, &owner, mouse.column, mouse.row,
                mouse.kind, Instant::now()) || rail_hover_changed;
        }
        if matches!(mouse.kind, MouseEventKind::ScrollUp | MouseEventKind::ScrollDown)
            && self.memory_preview.memory_tooltip_contains(self.size,
                ratatui::layout::Position::new(mouse.column, mouse.row)) {
            return self.memory_preview.scroll_memory(if mouse.kind == MouseEventKind::ScrollUp { -2 } else { 2 });
        }
        if let Some(handled) = self.wheel_chooser(mouse) { return handled; }
        let mut tool_hover_changed = false;
        if mouse.kind == MouseEventKind::Moved {
            let hover = self.visible_tool_sections.borrow().iter()
                .find(|(rect, _, _, _)| rect.contains(ratatui::layout::Position::new(mouse.column, mouse.row)))
                .map(|(_, _, id, key)| (id.clone(), key.clone()));
            if self.tool_section_hover != hover {
                tool_hover_changed = true;
                self.tool_section_hover = hover;
            }
            self.belief_pointer = Some((mouse.column, mouse.row));
        }
        if mouse.kind == MouseEventKind::Down(MouseButton::Left)
            && self.active_request_index().is_none()
            && (self.memory_manager.is_none()
                && self
                    .chip_info
                    .as_ref()
                    .is_some_and(|info| info.kind == "memory")
                && self.memory_list.is_some()
                || self.belief_graph_lines.is_none()
                    && self.lore_picker.as_ref().is_some_and(|picker| {
                        !picker.proposal_mode
                            && picker.belief_review.is_none()
                            && picker.evidence.is_none()
                    }))
        {
            let layout = self.layout(self.size);
            let pane = layout
                .panes
                .map_or(layout.body, |panes| panes[self.active_group]);
            if self.pane_regions(self.active_group, pane)[4]
                .contains(ratatui::layout::Position::new(mouse.column, mouse.row))
            {
                self.focus = Focus::Prompt;
                if let Some(picker) = self.lore_picker.as_mut() {
                    picker.filter_focused = true;
                }
                return true;
            }
        }
        if !self.link_interaction_blocked() && self.drag.is_none() {
            let point = ratatui::layout::Position::new(mouse.column, mouse.row);
            if matches!(
                mouse.kind,
                MouseEventKind::Drag(MouseButton::Left) | MouseEventKind::Up(MouseButton::Left)
            ) {
                let handled = self.transcript_selection.borrow_mut().drag(point);
                if handled {
                    return true;
                }
            }
            match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    let interactive = self.link_at(mouse.column, mouse.row).is_some()
                        || self
                            .visible_tool_sections
                            .borrow()
                            .iter()
                            .any(|(rect, _, _, _)| rect.contains(point));
                    self.transcript_selection.borrow_mut().clear();
                    if !interactive || mouse.modifiers.contains(KeyModifiers::SHIFT) {
                        let owner = self.transcript_selection.borrow_mut().start(point);
                        if let Some(owner) = owner {
                            self.active_group = owner.pane;
                            self.focus = Focus::Transcript;
                            self.chip_hover = None;
                            self.link_hover = None;
                            return true;
                        }
                    }
                }
                _ => {}
            }
        }

        if self.fleet_review.is_some() {
            if let Some(area) = self.active_chooser_rect() {
                if area.contains(ratatui::layout::Position::new(mouse.column, mouse.row)) {
                    match mouse.kind {
                        MouseEventKind::ScrollUp => {
                            return self
                                .fleet_review_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE))
                        }
                        MouseEventKind::ScrollDown => {
                            return self
                                .fleet_review_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE))
                        }
                        _ => return true,
                    }
                }
                if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
                    self.fleet_review = None;
                    self.chip_info = None;
                    return true;
                }
            }
            return rail_hover_changed;
        }
        if self.fleet_dependency_review.is_some() {
            if let Some(area) = self.active_chooser_rect() {
                if area.contains(ratatui::layout::Position::new(mouse.column, mouse.row)) {
                    match mouse.kind {
                        MouseEventKind::ScrollUp => return self.fleet_dependency_review_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE)),
                        MouseEventKind::ScrollDown => return self.fleet_dependency_review_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)),
                        _ => return true,
                    }
                }
                if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
                    self.fleet_dependency_review = None;
                    self.chip_info = None;
                    return true;
                }
            }
            return rail_hover_changed;
        }
        if self.drag == Some(DragTarget::Chooser) {
            match mouse.kind {
                MouseEventKind::Drag(MouseButton::Left) => {
                    let layout = self.layout(self.size);
                    let pane = layout
                        .panes
                        .map_or(layout.body, |panes| panes[self.active_group]);
                    let draft = self.groups[self.active_group]
                        .active_id()
                        .map_or("", |_| self.input.as_str());
                    let prompt = prompt_height(draft, pane.height);
                    let max = pane.height.saturating_sub(3 + prompt + 1 + 1 + 1);
                    let bottom = pane.bottom().saturating_sub(prompt + 2);
                    self.chooser_height_override
                        .set(Some(bottom.saturating_sub(mouse.row).clamp(5, max.max(5))));
                    return true;
                }
                MouseEventKind::Up(MouseButton::Left) => {
                    self.drag = None;
                    return true;
                }
                _ => {}
            }
        }
        if mouse.kind == MouseEventKind::Moved {
            let hover = self
                .lore_picker
                .as_ref()
                .filter(|picker| {
                    picker.belief_review.is_some() && picker.pending.is_none() && !picker.resolving
                })
                .and_then(|picker| {
                    self.active_chooser_rect().and_then(|area| {
                        belief_review_buttons(area, picker)
                            .into_iter()
                            .find(|(rect, _, _)| {
                                rect.contains(ratatui::layout::Position::new(
                                    mouse.column,
                                    mouse.row,
                                ))
                            })
                            .map(|(rect, _, _)| rect)
                    })
                });
            let belief_hover_changed = self.belief_button_hover != hover;
            self.belief_button_hover = hover;
            if hover.is_some() {
                let changed = rail_hover_changed
                    || belief_hover_changed
                    || self.chip_hover.is_some()
                    || self.link_hover.is_some();
                self.chip_hover = None;
                self.link_hover = None;
                return changed;
            }
            if self.hover_chooser(mouse.column, mouse.row) {
                self.chip_hover = None;
                self.link_hover = None;
                return true;
            }
            let overlay = self.link_interaction_blocked();
            let chip = (!overlay)
                .then(|| self.chip_hit_at(mouse.column, mouse.row))
                .flatten();
            let link = (!overlay)
                .then(|| self.link_at(mouse.column, mouse.row))
                .flatten();
            let changed =
                belief_hover_changed || self.chip_hover != chip || self.link_hover != link;
            self.chip_hover = chip;
            let position = link.as_ref().map(|_| (mouse.column, mouse.row));
            let moved = self.link_hover_position != position;
            self.link_hover_position = position;
            self.link_hover = link;
            return changed || moved || tool_hover_changed || rail_hover_changed;
        }
        if mouse.kind == MouseEventKind::Down(MouseButton::Left) && self.lore_picker.is_some() {
            if let Some(area) = self.active_chooser_rect() {
                if mouse.row == area.y && mouse.column > area.x {
                    let x = mouse.column - area.x - 1;
                    if x < 34 && mouse.column < area.right()-1 { return self.switch_lore_view(if x < 10 { 0 } else if x < 21 { 1 } else { 2 }); }
                }
            }
        }
        if mouse.kind == MouseEventKind::Down(MouseButton::Left)
            && self.active_chooser_rect().is_some_and(|area| {
                mouse.row == area.y && mouse.column >= area.x && mouse.column < area.right()
            })
        {
            self.drag = Some(DragTarget::Chooser);
            return true;
        }
        if let Some(index) = self.active_request_index() {
            if !self.input_requests[index].sending && self.active_chooser_rect().is_some_and(|menu| menu.contains(ratatui::layout::Position::new(mouse.column, mouse.row))) {
                match mouse.kind {
                    MouseEventKind::ScrollUp => {
                        self.input_requests[index].scroll = self.input_requests[index].scroll.saturating_sub(1);
                        return true;
                    }
                    MouseEventKind::ScrollDown => {
                        self.input_requests[index].scroll = self.input_requests[index].scroll.saturating_add(1);
                        return true;
                    }
                    _ => {}
                }
            }
            if mouse.kind == MouseEventKind::Down(MouseButton::Left)
            {
                if let Some(menu) = self.active_chooser_rect() {
                    if mouse.column > menu.x && mouse.column < menu.right().saturating_sub(1) {
                        if let Some(option) =
                            input_request_option_at(&self.input_requests[index], menu, mouse.row)
                        {
                            self.input_requests[index].selected = option;
                            return self
                                .request_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
                        }
                    }
                }
            }
        }
        if self.repo_picker.is_some() {
            let Some(menu) = self.active_chooser_rect() else {
                return false;
            };
            let inside = menu.contains(ratatui::layout::Position::new(mouse.column, mouse.row));
            return match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    if !inside {
                        self.repo_picker = None;
                        return true;
                    }
                    if mouse.row >= menu.y + 2 && mouse.row < menu.bottom().saturating_sub(1) {
                        let picker = self.repo_picker.as_mut().unwrap();
                        let visible = usize::from(menu.height.saturating_sub(3)).max(1);
                        let start = chooser_visible_start(
                            &self.chooser_view_start,
                            picker.selected,
                            visible,
                        );
                        let index = start + usize::from(mouse.row - menu.y - 2);
                        if index < picker.paths.len() {
                            picker.selected = index;
                            return self.repo_picker_key(KeyEvent::new(
                                KeyCode::Enter,
                                KeyModifiers::NONE,
                            ));
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
            };
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
                if let Some(area) = menu.filter(|area| {
                    area.contains(ratatui::layout::Position::new(mouse.column, mouse.row))
                }) {
                    let offset = usize::from(mouse.row.saturating_sub(area.y.saturating_add(3)));
                    let index = self
                        .settings_menu
                        .as_ref()
                        .unwrap()
                        .visible_indices(area.height)
                        .get(offset)
                        .copied();
                    if let Some(index) = index.filter(|_| mouse.row >= area.y + 3) {
                        let current = self.settings_menu.as_ref().unwrap().selected;
                        self.settings_menu.as_mut().unwrap().selected = index;
                        if current == index {
                            return self.settings_menu_key(KeyEvent::new(
                                KeyCode::Enter,
                                KeyModifiers::NONE,
                            ));
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
            let area = self.active_chooser_rect();
            if let Some(menu) = &mut self.operations_menu {
                if let Some(area) = area {
                    let inside = mouse.column > area.x
                        && mouse.column < area.right().saturating_sub(1)
                        && mouse.row > area.y
                        && mouse.row < area.bottom().saturating_sub(1);
                    if inside
                        && matches!(
                            mouse.kind,
                            MouseEventKind::Moved | MouseEventKind::Down(MouseButton::Left)
                        )
                    {
                        let choice_row = usize::from(mouse.row - area.y - 1);
                        let choice = menu.choice_at(choice_row);
                        menu.hover(choice_row);
                        if choice && mouse.kind == MouseEventKind::Down(MouseButton::Left) {
                            menu.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
                        }
                        if let Some(info) = &mut self.chip_info {
                            info.lines = menu.lines(usize::from(area.width));
                        }
                        return true;
                    }
                    if !area.contains(ratatui::layout::Position::new(mouse.column, mouse.row))
                        && mouse.kind == MouseEventKind::Down(MouseButton::Left)
                        && !menu.busy()
                    {
                        self.operations_menu = None;
                        self.chip_info = None;
                    }
                }
                return true;
            }
            if let Some(menu) = &mut self.fleet_menu {
                if let Some(area) = area {
                    let inside = mouse.column > area.x
                        && mouse.column < area.right().saturating_sub(1)
                        && mouse.row > area.y
                        && mouse.row < area.bottom().saturating_sub(1);
                    if inside
                        && matches!(
                            mouse.kind,
                            MouseEventKind::Moved | MouseEventKind::Down(MouseButton::Left)
                        )
                    {
                        let scroll = self.chip_info.as_ref().unwrap().scroll;
                        if menu.hover(usize::from(mouse.row - area.y - 1) + scroll)
                            && mouse.kind == MouseEventKind::Down(MouseButton::Left)
                        {
                            menu.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
                        }
                        self.chip_info.as_mut().unwrap().lines = menu.display();
                        return true;
                    }
                    if !inside && mouse.kind == MouseEventKind::Down(MouseButton::Left) {
                        self.fleet_menu = None;
                        self.chip_info = None;
                        return true;
                    }
                }
            }
            if let Some(manager) = &mut self.memory_manager {
                if let Some(area) = area {
                    let inside = mouse.column > area.x
                        && mouse.column < area.right().saturating_sub(1)
                        && mouse.row >= area.y + 2
                        && mouse.row < area.bottom().saturating_sub(2);
                    if inside {
                        match mouse.kind {
                            MouseEventKind::Moved | MouseEventKind::Down(MouseButton::Left) => {
                                return manager.hover(
                                    usize::from(mouse.row - area.y - 2),
                                    usize::from(area.height.saturating_sub(4)),
                                );
                            }
                            MouseEventKind::ScrollUp => {
                                return manager.key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE))
                            }
                            MouseEventKind::ScrollDown => {
                                return manager
                                    .key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE))
                            }
                            _ => {}
                        }
                    }
                    if !area.contains(ratatui::layout::Position::new(mouse.column, mouse.row))
                        && mouse.kind == MouseEventKind::Down(MouseButton::Left)
                        && !manager.busy()
                    {
                        self.memory_manager = None;
                        self.chip_info = None;
                    }
                }
                return true;
            }
            let inside_menu = self.active_chooser_rect().is_some_and(|area| {
                area.contains(ratatui::layout::Position::new(mouse.column, mouse.row))
            });
            let memory_rows = self.memory_list.as_ref().map(|list| list.indices().len());
            if let Some(info) = self.chip_info.as_mut().filter(|info| info.kind == "memory") {
                if inside_menu {
                    match mouse.kind {
                        MouseEventKind::ScrollUp => {
                            info.scroll = info.scroll.saturating_sub(3);
                            if let Some(list) = self.memory_list.as_mut() { list.selected = list.selected.min(info.scroll + usize::from(area.map_or(1, |area| area.height.saturating_sub(3)).max(1)) - 1); }
                            return true;
                        }
                        MouseEventKind::ScrollDown => {
                            info.scroll = info
                                .scroll
                                .saturating_add(3)
                                .min(memory_rows.unwrap_or(info.lines.len()).saturating_sub(usize::from(area.map_or(1, |area| area.height.saturating_sub(3)).max(1))));
                            if let Some(list) = self.memory_list.as_mut() { list.selected = list.selected.max(info.scroll); }
                            return true;
                        }
                        _ => {}
                    }
                }
            }
            if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
                let inside = self.active_chooser_rect().is_some_and(|area| {
                    area.contains(ratatui::layout::Position::new(mouse.column, mouse.row))
                });
                self.chip_info = None;
                self.memory_menu_pending = None;
                if inside {
                    return true;
                }
            } else {
                return false;
            }
        }
        let suggestions = self.slash_suggestions();
        if !suggestions.is_empty() {
            if let Some(menu) = self.active_chooser_rect() {
                if menu.contains(ratatui::layout::Position::new(mouse.column, mouse.row)) {
                    match mouse.kind {
                        MouseEventKind::Down(MouseButton::Left) => {
                            if mouse.column <= menu.x
                                || mouse.column >= menu.right().saturating_sub(1)
                                || mouse.row <= menu.y
                                || mouse.row >= menu.bottom().saturating_sub(1)
                            {
                                return true;
                            }
                            let visible = usize::from(menu.height.saturating_sub(2)).max(1);
                            let start = chooser_visible_start(
                                &self.chooser_view_start,
                                self.slash_selected,
                                visible,
                            );
                            let position =
                                start + usize::from(mouse.row.saturating_sub(menu.y + 1));
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
                            self.slash_selected =
                                (self.slash_selected + 1).min(suggestions.len() - 1);
                            return true;
                        }
                        _ => {}
                    }
                }
            }
        }
        if self.branch_picker.is_some() {
            let Some(menu) = self.active_chooser_rect() else {
                return false;
            };
            match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    if !menu.contains(ratatui::layout::Position::new(mouse.column, mouse.row)) {
                        self.branch_picker = None;
                    } else {
                        let first_row = menu.y.saturating_add(2);
                        if mouse.row >= first_row && mouse.row < menu.bottom().saturating_sub(1) {
                            let visible = usize::from(menu.height.saturating_sub(3)).max(1);
                            let picker = self.branch_picker.as_mut().unwrap();
                            let start = chooser_visible_start(
                                &self.chooser_view_start,
                                picker.selected,
                                visible,
                            );
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
                    picker.selected =
                        (picker.selected + 1).min(picker.branches.len().saturating_sub(1));
                    return true;
                }
                _ => return false,
            }
        }
        if self.attach_picker.is_some() {
            let Some(menu) = self.active_chooser_rect() else {
                return false;
            };
            match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    if mouse.column < menu.x
                        || mouse.column >= menu.right()
                        || mouse.row < menu.y
                        || mouse.row >= menu.bottom()
                    {
                        self.attach_picker = None;
                        return true;
                    }
                    let first_row = menu.y.saturating_add(2);
                    if mouse.row >= first_row && mouse.row < menu.bottom().saturating_sub(1) {
                        let visible = usize::from(menu.height.saturating_sub(3)).max(1);
                        let selected = self.attach_picker.as_ref().unwrap().selected;
                        let start =
                            chooser_visible_start(&self.chooser_view_start, selected, visible);
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
            let Some(menu) = self.active_chooser_rect() else {
                return false;
            };
            match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    if mouse.column < menu.x
                        || mouse.column >= menu.right()
                        || mouse.row < menu.y
                        || mouse.row >= menu.bottom()
                    {
                        self.history_modal = false;
                        self.cancel_history_query();
                        return true;
                    }
                    let first_row = menu.y.saturating_add(2);
                    if mouse.row >= first_row && mouse.row < menu.bottom().saturating_sub(1) {
                        let visible = usize::from(menu.height.saturating_sub(3)).max(1);
                        if let Some((position, header, _)) = self
                            .history_rows(visible)
                            .get(usize::from(mouse.row - first_row))
                        {
                            self.history_selected = *position;
                            if *header {
                                self.open_selected_history();
                            }
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
            let Some(menu) = self.active_chooser_rect() else {
                return false;
            };
            match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    if mouse.column < menu.x
                        || mouse.column >= menu.right()
                        || mouse.row < menu.y
                        || mouse.row >= menu.bottom()
                    {
                        self.queue_picker = None;
                    } else {
                        let first = menu.y.saturating_add(2);
                        if mouse.row >= first && mouse.row < menu.bottom().saturating_sub(1) {
                            let picker = self.queue_picker.as_mut().unwrap();
                            let visible = usize::from(menu.height.saturating_sub(3)).max(1);
                            let start = chooser_visible_start(
                                &self.chooser_view_start,
                                picker.selected,
                                visible,
                            );
                            picker.selected = (start + usize::from(mouse.row - first))
                                .min(picker.rows.len().saturating_sub(1));
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
                    picker.selected =
                        (picker.selected + 1).min(picker.rows.len().saturating_sub(1));
                    return true;
                }
                _ => return false,
            }
        }
        if self.lore_picker.is_some() {
            let Some(menu) = self.active_chooser_rect() else {
                return false;
            };
            if !menu.contains(ratatui::layout::Position::new(mouse.column, mouse.row)) {
                return true;
            }
            let picker = self.lore_picker.as_mut().unwrap();
            if picker.pending.is_some() || picker.resolving || self.belief_filter_due.is_some() {
                return true;
            }
            if picker.belief_review.is_some()
                && mouse.kind == MouseEventKind::Down(MouseButton::Left)
            {
                if let Some((_, _, key)) =
                    belief_review_buttons(menu, picker)
                        .into_iter()
                        .find(|(rect, _, _)| {
                            rect.contains(ratatui::layout::Position::new(mouse.column, mouse.row))
                        })
                {
                    return self.lore_picker_key(KeyEvent::new(key, KeyModifiers::NONE));
                }
            }
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
                if visible == 0 {
                    return true;
                }
                if visible > 0 && picker.review_scroll <= picker.review_seen {
                    picker.review_seen = picker
                        .review_seen
                        .max(picker.review_scroll.saturating_add(visible))
                        .min(total);
                }
                let max_scroll = total.saturating_sub(visible);
                match mouse.kind {
                    MouseEventKind::ScrollUp => {
                        picker.review_scroll = picker.review_scroll.saturating_sub(3)
                    }
                    MouseEventKind::ScrollDown => {
                        picker.review_scroll =
                            picker.review_scroll.saturating_add(3).min(max_scroll)
                    }
                    _ => {}
                }
                if visible > 0 && picker.review_scroll <= picker.review_seen {
                    picker.review_seen = picker
                        .review_seen
                        .max(picker.review_scroll.saturating_add(visible))
                        .min(total);
                }
                return true;
            }
            if picker.evidence.is_some() || picker.review.is_some() {
                return true;
            }
            if picker.proposal_mode {
                let visible = usize::from(menu.height.saturating_sub(5)).max(1);
                match mouse.kind {
                    MouseEventKind::Down(MouseButton::Left) => {
                        let first = menu.y + 3;
                        let start = chooser_visible_start(
                            &self.chooser_view_start,
                            picker.selected,
                            visible,
                        );
                        if mouse.row >= first && mouse.row < first.saturating_add(visible as u16) {
                            let index = start + usize::from(mouse.row - first);
                            if let Some(pid) =
                                picker.proposals.get(index).map(|row| row.pid.clone())
                            {
                                if picker.selected == index {
                                    let cwd = picker.cwd.clone();
                                    self.load_lore(lore_picker::Query::Review(cwd, pid));
                                } else {
                                    picker.selected = index;
                                }
                            }
                        }
                    }
                    MouseEventKind::ScrollUp => picker.selected = picker.selected.saturating_sub(1),
                    MouseEventKind::ScrollDown => {
                        picker.selected =
                            (picker.selected + 1).min(picker.proposals.len().saturating_sub(1))
                    }
                    _ => {}
                }
                return true;
            }
            match mouse.kind {
                MouseEventKind::Down(MouseButton::Left) => {
                    let first = menu.y.saturating_add(2);
                    let visible = usize::from(menu.height.saturating_sub(3)).max(1);
                    let start =
                        chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
                    if mouse.row >= first && mouse.row < first.saturating_add(visible as u16) {
                        let index = start + usize::from(mouse.row - first);
                        if let Some(id) = picker.rows.get(index).map(|row| row.id) {
                            picker.filter_focused = false;
                            let action_clicked = crate::lore_table::BeliefColumns::new(
                                usize::from(menu.width.saturating_sub(2)),
                            )
                            .actions
                            .then(|| {
                                belief_buttons(
                                    menu,
                                    mouse.row,
                                    &[
                                        ("[Accept]", KeyCode::Char('A')),
                                        ("[Reject]", KeyCode::Char('R')),
                                    ],
                                )
                                .into_iter()
                                .find(|(rect, _, _)| {
                                    rect.contains(ratatui::layout::Position::new(
                                        mouse.column,
                                        mouse.row,
                                    ))
                                })
                                .map(|(_, _, key)| key)
                            })
                            .flatten();
                            if picker.selected == index || action_clicked.is_some() {
                                picker.belief_intent = action_clicked.map(|key| {
                                    if key == KeyCode::Char('R') {
                                        doxa_lore::BeliefAction::Retract
                                    } else {
                                        doxa_lore::BeliefAction::Confirmed
                                    }
                                });
                                picker.selected = index;
                                let cwd = picker.cwd.clone();
                                self.load_lore(lore_picker::Query::BeliefReview(cwd, id));
                            } else {
                                picker.selected = index;
                            }
                        }
                    }
                }
                MouseEventKind::ScrollUp => picker.selected = picker.selected.saturating_sub(1),
                MouseEventKind::ScrollDown => {
                    picker.selected = (picker.selected + 1).min(picker.rows.len().saturating_sub(1))
                }
                _ => {}
            }
            return true;
        }
        if self.active_request_index().is_none()
            && matches!(mouse.kind, MouseEventKind::Down(MouseButton::Left))
            && (self.engine_picker
                || self.new_session.is_some()
                || self.model_picker.is_some()
                || self.effort_picker.is_some()
                || self.permission_picker.is_some())
        {
            let Some(menu) = self.active_chooser_rect() else {
                return false;
            };
            let (x, y, width, height) = (menu.x, menu.y, menu.width, menu.height);
            if mouse.column < x
                || mouse.column >= x + width
                || mouse.row < y
                || mouse.row >= y + height
            {
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
                if mouse.row < y + offset {
                    return true;
                }
                let visible = usize::from(height.saturating_sub(offset + 1)).max(1);
                let start =
                    chooser_visible_start(&self.chooser_view_start, self.engine_selected, visible);
                let row = start + usize::from(mouse.row.saturating_sub(y + offset));
                if row < ENGINE_CHOICES.len() {
                    self.engine_selected = row;
                    self.select_new_engine();
                }
                return true;
            }
            if let Some(form) = self.new_session.as_mut() {
                let first = y + if height >= 8 { 4 } else { 2 };
                let fields = if vendor_models(form.engine).is_empty() {
                    3
                } else {
                    4
                };
                if mouse.row >= first && usize::from(mouse.row - first) <= fields {
                    form.field = usize::from(mouse.row - first);
                    if form.field == fields {
                        self.new_session_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
                    }
                }
                return true;
            }
            if let Some(picker) = &mut self.effort_picker {
                let visible = usize::from(height.saturating_sub(4)).max(1);
                let start =
                    chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
                let row = start + usize::from(mouse.row.saturating_sub(y + 3));
                if mouse.row >= y + 3 && row < picker.levels.len() {
                    picker.selected = row;
                    self.select_effort();
                }
                return true;
            }
            if let Some((id, selected)) = &mut self.permission_picker {
                let choices = permission_choices(self.session_identity.get(id).and_then(|identity| identity.0.as_deref()));
                let offset = if height >= 10 { 4 } else { 2 };
                if mouse.row >= y + offset {
                    let visible = usize::from(height.saturating_sub(offset + 1)).max(1);
                    let start = chooser_visible_start(&self.chooser_view_start, *selected, visible);
                    let row = start + usize::from(mouse.row - (y + offset));
                    if row < choices.len() {
                        *selected = row;
                        self.permission_confirm_dont_ask = false;
                        if choices[row].0 == "dontAsk" {
                            self.notice = "dontAsk denies unapproved calls silently; press Enter twice to confirm".into();
                        } else {
                            self.select_permission_mode();
                        }
                    }
                }
                return true;
            }
            let picker = self.model_picker.as_mut().unwrap();
            let offset = picker.row_offset();
            let visible = picker.visible_rows(height);
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            let row = start + usize::from(mouse.row.saturating_sub(y + offset));
            if mouse.row >= y + offset && row < picker.models.len() && !picker.loading {
                picker.selected = row;
                self.select_model();
            }
            return true;
        }
        if self.action_menu {
            if let Some(area) = self.active_chooser_rect() {
                match mouse.kind {
                    MouseEventKind::Down(MouseButton::Left)
                        if !area
                            .contains(ratatui::layout::Position::new(mouse.column, mouse.row)) =>
                    {
                        self.action_menu = false;
                        return true;
                    }
                    MouseEventKind::Down(MouseButton::Left)
                        if mouse.column > area.x
                            && mouse.column < area.right().saturating_sub(1)
                            && mouse.row >= area.y + 2
                            && mouse.row < area.bottom().saturating_sub(1) =>
                    {
                        let visible = usize::from(area.height.saturating_sub(3)).max(1);
                        let start = chooser_visible_start(
                            &self.chooser_view_start,
                            self.action_selected,
                            visible,
                        );
                        let index = start + usize::from(mouse.row - area.y - 2);
                        if index < self.action_rows().len() {
                            self.action_selected = index;
                            return self
                                .action_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
                        }
                    }
                    MouseEventKind::ScrollUp => {
                        return self.action_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE))
                    }
                    MouseEventKind::ScrollDown => {
                        return self.action_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE))
                    }
                    _ => {}
                }
            }
            return false;
        }
        if matches!(mouse.kind, MouseEventKind::ScrollUp | MouseEventKind::ScrollDown)
            && self.active_chooser_rect().is_some()
            && self.wheel_transcript(mouse)
        { return true; }
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
            || self.delete_confirmation.is_some()
            || self.new_session.is_some()
            || self.repo_picker.is_some()
        {
            // A request belongs to its session, not the whole terminal. Let
            // the user focus the other pane and keep working while this one
            // waits for an answer. The request stays visible in its own pane.
            if self.active_request_index().is_some()
                && mouse.kind == MouseEventKind::Down(MouseButton::Left)
            {
                if let Some((other, pane)) = self.layout(self.size).panes.and_then(|regions| {
                    regions.into_iter().enumerate().find(|(index, pane)| {
                        *index != self.active_group
                            && pane
                                .contains(ratatui::layout::Position::new(mouse.column, mouse.row))
                    })
                }) {
                    let prompt = self.pane_regions(other, pane)[4];
                    self.active_group = other;
                    self.focus = if prompt
                        .contains(ratatui::layout::Position::new(mouse.column, mouse.row))
                    {
                        Focus::Prompt
                    } else {
                        Focus::Transcript
                    };
                    return true;
                }
            }
            self.drag = None;
            return false;
        }
        if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
            if let Some(hit) = self.chip_hit_at(mouse.column, mouse.row) {
                self.chip_hover = None;
                self.active_group = hit.group;
                self.focus = Focus::Chip(hit.kind);
                self.activate_chip(hit.kind, hit.group);
                return true;
            }
        }
        if self.diff_pane {
            if let Some(panes) = self.layout(self.size).panes {
                let pane = panes[(self.active_group + 1) % panes.len()];
                if pane.contains(ratatui::layout::Position::new(mouse.column, mouse.row)) {
                    return self.diff_mouse(pane, mouse)
                        || mouse.kind == MouseEventKind::Down(MouseButton::Left);
                }
            }
        }
        match mouse.kind {
            MouseEventKind::Down(MouseButton::Left) => {
                self.drag = None;
                if self.size.width < 20 || self.size.height < 5 {
                    return false;
                }
                let section_hit = self
                    .visible_tool_sections
                    .borrow()
                    .iter()
                    .find(|(rect, _, _, _)| {
                        rect.contains(ratatui::layout::Position::new(mouse.column, mouse.row))
                    })
                    .cloned();
                if let Some((_, group, id, section)) = section_hit {
                    self.active_group = group;
                    self.focus = Focus::Transcript;
                    self.remember_tool_selection(id.clone(), section.clone());
                    self.toggle_tool_section(id, section);
                    return true;
                }
                let layout = self.layout(self.size);
                if let Some(rail) = layout.rail {
                    if mouse.column > rail.x
                        && mouse.column < rail.right().saturating_sub(1)
                        && mouse.row > rail.y
                        && mouse.row < rail.bottom().saturating_sub(1)
                    {
                        let row = usize::from(mouse.row - rail.y - 1)
                            + self.rail_view_start(rail, &self.rail_rows());
                        match self.rail_rows().get(row) {
                            Some(RailRow::Heading(index)) => {
                                self.collections[*index].collapsed =
                                    !self.collections[*index].collapsed;
                                self.rail_selected = self
                                    .rail_selected
                                    .min(self.rail_order().len().saturating_sub(1));
                                self.focus = Focus::Rail;
                                return true;
                            }
                            Some(RailRow::Session(index)) => {
                                let id = self.sessions[*index].id.clone();
                                let now = Instant::now();
                                let double_click = self.last_rail_click.as_ref().is_some_and(|(previous, at)|
                                    previous == &id && now.duration_since(*at) <= std::time::Duration::from_millis(500));
                                self.last_rail_click = if double_click { None } else { Some((id.clone(), now)) };
                                self.rail_selected = self
                                    .rail_order()
                                    .iter()
                                    .position(|visible| visible == index)
                                    .unwrap_or(0);
                                self.open_selected();
                                if double_click {
                                    self.pending_rename = Some(id);
                                }
                                return true;
                            }
                            Some(RailRow::ProjectHeading(_) | RailRow::PastHeading) => {
                                self.focus = Focus::Rail;
                                return true;
                            }
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
                } else if let Some(divider) = self
                    .pane_tree
                    .as_ref()
                    .filter(|_| layout.panes.is_some())
                    .and_then(|tree| tree.divider_at(layout.body, mouse.column, mouse.row))
                {
                    self.drag = Some(DragTarget::NestedPane(divider));
                } else if let Some(panes) = layout
                    .panes
                    .as_ref()
                    .filter(|panes| panes.len() == 2 && self.pane_tree.is_none())
                {
                    let (first, second) = (panes[0], panes[1]);
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
                    let pane_hits = layout
                        .panes
                        .map(|panes| panes.into_iter().enumerate().collect::<Vec<_>>())
                        .unwrap_or_else(|| vec![(self.active_group, layout.body)]);
                    for (index, pane) in pane_hits.iter().copied() {
                        if mouse.column >= pane.x
                            && mouse.column < pane.right()
                            && mouse.row >= pane.y
                            && mouse.row < pane.bottom()
                        {
                            if mouse.row == pane.y.saturating_add(1) {
                                if let Some(tab) = self.tab_at(index, pane, mouse.column) {
                                    self.active_group = index;
                                    self.groups[index].active = tab;
                                    self.groups[index].scroll = 0;
                                    self.focus = Focus::Transcript;
                                    return true;
                                }
                            }
                            let draft = self.groups[index]
                                .active_id()
                                .map(|id| {
                                    if self.active_group == index {
                                        self.input.as_str()
                                    } else {
                                        self.input_drafts
                                            .get(&(index, id.to_owned()))
                                            .map(|(text, _)| text.as_str())
                                            .unwrap_or("")
                                    }
                                })
                                .unwrap_or("");
                            let prompt_top = pane
                                .bottom()
                                .saturating_sub(prompt_height(draft, pane.height) + 1);
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
                    DragTarget::NestedPane(divider) if layout.panes.is_some() => {
                        if let Some(tree) = self.pane_tree.as_mut() {
                            tree.resize_divider(
                                layout.body,
                                divider,
                                mouse.column,
                                mouse.row,
                                MIN_PANE_WIDTH,
                                MIN_PANE_HEIGHT,
                            );
                        }
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
            MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
                let layout = self.layout(self.size);
                if let Some(rail) = layout.rail {
                    if rail.contains(ratatui::layout::Position::new(mouse.column, mouse.row)) {
                        self.focus = Focus::Rail;
                        if mouse.kind == MouseEventKind::ScrollUp {
                            self.rail_selected = self.rail_selected.saturating_sub(1);
                        } else {
                            self.rail_selected = (self.rail_selected + 1)
                                .min(self.rail_order().len().saturating_sub(1));
                        }
                        return true;
                    }
                }
                let pane_hits = layout.panes
                    .map(|panes| panes.into_iter().enumerate().collect::<Vec<_>>())
                    .unwrap_or_else(|| vec![(self.active_group, layout.body)]);
                for (index, pane) in pane_hits {
                    if mouse.row == pane.y.saturating_add(1)
                        && mouse.column > pane.x && mouse.column < pane.right().saturating_sub(1)
                    {
                        self.active_group = index;
                        self.focus = Focus::Tabs;
                        if mouse.kind == MouseEventKind::ScrollUp {
                            self.previous_tab();
                        } else {
                            self.next_tab();
                        }
                        return true;
                    }
                }
                self.wheel_transcript(mouse)
            }
            _ => false,
        }
    }
}
