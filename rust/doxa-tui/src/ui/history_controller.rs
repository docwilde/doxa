//! Own archived-session search, restore and resume transitions.
use super::{
    chooser_visible_start, safe_label, transcript_tail, unsafe_input_char, App, Focus, Session,
    MAX_SEARCH_WORKERS, SEARCH_DEBOUNCE,
};
use crate::history;
use crossterm::event::KeyCode;
use crossterm::event::KeyEvent;
use crossterm::event::KeyModifiers;
use std::collections::HashSet;
use std::path::Path;
use std::path::PathBuf;
use std::sync::atomic::Ordering;
use std::sync::mpsc;
use std::sync::mpsc::TryRecvError;
use std::time::Instant;

impl App {
    pub(super) fn history_matches(&self) -> Vec<usize> {
        let query = self.history_query.to_lowercase();
        self.sessions
            .iter()
            .enumerate()
            .filter_map(|(index, session)| {
                if self.history_resume {
                    return (query.is_empty() || session.id.to_lowercase().starts_with(&query))
                        .then_some(index);
                }
                let mut start = session.transcript.len().saturating_sub(16 * 1024);
                while !session.transcript.is_char_boundary(start) {
                    start += 1;
                }
                if query.is_empty()
                    || session.title.to_lowercase().contains(&query)
                    || session.id.to_lowercase().contains(&query)
                    || self
                        .history_scanned_matches
                        .get(&session.id)
                        .is_some_and(|scanned| scanned == &query)
                    || session.transcript[start..].to_lowercase().contains(&query)
                {
                    Some(index)
                } else {
                    None
                }
            })
            .take(128)
            .collect()
    }

    pub(super) fn history_snippets(&self, id: &str) -> &[String] {
        if self.history_resume
            || self.history_scanned_matches.get(id) != Some(&self.history_query.to_lowercase())
        {
            return &[];
        }
        self.history_entries
            .get(id)
            .map_or(&[], |entry| entry.search_snippets.as_slice())
    }

    /// One session header followed by at most two indexed excerpts. Row
    /// positions are shared by paint and mouse hit testing.
    pub(super) fn history_rows(&self, visible: usize) -> Vec<(usize, bool, String)> {
        let matches = self.history_matches();
        let selected_row = matches
            .iter()
            .take(self.history_selected)
            .map(|&index| 1 + self.history_snippets(&self.sessions[index].id).len().min(2))
            .sum();
        let start = chooser_visible_start(&self.chooser_view_start, selected_row, visible);
        let mut rows = Vec::new();
        let mut visual_row = 0;
        for (position, &index) in matches.iter().enumerate() {
            if rows.len() >= visible {
                break;
            }
            let session = &self.sessions[index];
            if visual_row >= start {
                let label = format!(
                    " {} {} · {}{}",
                    if position == self.history_selected {
                        '›'
                    } else {
                        ' '
                    },
                    safe_label(&session.title),
                    safe_label(&session.id),
                    if self.offline_ids.contains(&session.id) {
                        " · archived"
                    } else {
                        ""
                    }
                );
                rows.push((position, true, label));
            }
            visual_row += 1;
            for snippet in self.history_snippets(&session.id).iter().take(2) {
                if rows.len() >= visible {
                    break;
                }
                if visual_row >= start {
                    rows.push((position, false, format!("    ↳ {}", safe_label(snippet))));
                }
                visual_row += 1;
            }
        }
        rows
    }

    pub(super) fn history_fits(&self) -> bool {
        let layout = self.layout(self.size);
        let pane = layout
            .panes
            .map_or(layout.body, |panes| panes[self.active_group]);
        pane.width >= 20 && pane.height >= 11
    }

    pub(super) fn open_history(&mut self) {
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
        self.history_query_cursor = 0;
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

    pub(super) fn start_history_inventory(&mut self) {
        let (tx, rx) = mpsc::sync_channel(1);
        self.history_pending = Some(rx);
        self.history_scan_query = None;
        std::thread::spawn(move || {
            let _ = tx.send(history::discover());
        });
    }

    pub(super) fn local_search(&mut self, args: &str) {
        let query = args.trim();
        if query.len() > 200 || query.chars().any(unsafe_input_char) {
            self.notice =
                "search: query must be at most 200 bytes without control characters".into();
            return;
        }
        self.input.clear();
        self.input_cursor = 0;
        self.open_history();
        if self.history_modal {
            self.history_query = query.to_owned();
            self.history_query_cursor = self.history_query.len();
            self.schedule_history_query(Instant::now());
        }
    }

    pub(super) fn schedule_history_query(&mut self, now: Instant) {
        if self.history_resume {
            return;
        }
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
        self.history_query_due =
            (!self.history_query.trim().is_empty()).then_some(now + SEARCH_DEBOUNCE);
    }

    pub(super) fn prune_unopened_history(&mut self) {
        // Search hits are display cache, not tabs. Keep a bounded recent
        // inventory so reopening search can reuse results, while repeated
        // distinct queries cannot retain unbounded transcript tails.
        const MAX_CACHED_ARCHIVED: usize = 64;
        let selected_id = self
            .history_modal
            .then(|| {
                self.history_matches()
                    .get(self.history_selected)
                    .map(|&index| self.sessions[index].id.clone())
            })
            .flatten();
        let open: HashSet<String> = self
            .groups
            .iter()
            .flat_map(|group| group.tabs.iter().cloned())
            .collect();
        let unopened: Vec<_> = self
            .sessions
            .iter()
            .filter(|session| self.offline_ids.contains(&session.id) && !open.contains(&session.id))
            .map(|session| session.id.clone())
            .collect();
        let excess = unopened.len().saturating_sub(MAX_CACHED_ARCHIVED);
        let evict: HashSet<_> = unopened
            .into_iter()
            .filter(|id| selected_id.as_ref() != Some(id))
            .take(excess)
            .collect();
        if evict.is_empty() {
            return;
        }
        self.sessions.retain(|session| !evict.contains(&session.id));
        self.offline_ids.retain(|id| !evict.contains(id));
        self.history_entries.retain(|id, _| !evict.contains(id));
        self.history_scanned_matches
            .retain(|id, _| !evict.contains(id));
        if let Some(id) = selected_id {
            self.history_selected = self
                .history_matches()
                .iter()
                .position(|&index| self.sessions[index].id == id)
                .unwrap_or(0);
        }
    }

    pub(super) fn cancel_history_query(&mut self) {
        self.history_pending = None;
        self.history_scan_query = None;
        self.history_query_due = None;
    }

    pub(super) fn start_due_history_query(&mut self, now: Instant) -> bool {
        if !self.history_modal
            || self.history_resume
            || self.history_pending.is_some()
            || !self.history_query_due.is_some_and(|due| now >= due)
            || self.history_search_inflight.load(Ordering::Acquire) >= MAX_SEARCH_WORKERS
        {
            return false;
        }
        self.history_query_due = None;
        let query = self.history_query.clone();
        let cwd = self.groups[self.active_group]
            .active_id()
            .and_then(|id| self.session_cwds.get(id))
            .cloned()
            .or_else(|| std::env::current_dir().ok())
            .unwrap_or_default();
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

    pub(super) fn local_resume(&mut self, args: &str) {
        let query = args.trim();
        if !query.is_empty() && !crate::discovery::valid_id(query) {
            self.notice = "resume: enter a valid session ID or prefix".into();
            return;
        }
        self.input.clear();
        self.input_cursor = 0;
        // A full ID naming a live daemon is immediately attachable even if
        // that daemon has not yet been indexed into transcript history.
        if !query.is_empty()
            && crate::discovery::sessions().is_ok_and(|rows| rows.iter().any(|row| row.id == query))
        {
            self.attach_selected(query);
            return;
        }
        self.open_history();
        if !self.history_modal {
            return;
        }
        self.history_resume = true;
        self.history_explicit = !query.is_empty();
        self.history_query = query.to_owned();
        self.history_query_cursor = self.history_query.len();
        // Explicit IDs search the full bounded transcript inventory rather
        // than only the recent-history window.
        if !query.is_empty() {
            let (tx, rx) = mpsc::sync_channel(1);
            self.history_pending = Some(rx);
            let prefix = query.to_owned();
            std::thread::spawn(move || {
                let _ = tx.send(history::discover_prefix(&prefix));
            });
        }
    }

    pub(crate) fn recorded_session_cwd(&self, id: &str) -> Option<&Path> {
        self.session_cwds.get(id).map(PathBuf::as_path).or_else(|| {
            self.history_entries
                .get(id)
                .and_then(|entry| entry.cwd.as_deref())
        })
    }

    pub(crate) fn restore_archive(&mut self, entry: &history::OfflineSession, note: &str) {
        self.history_entries.insert(entry.id.clone(), entry.clone());
        self.offline_ids.insert(entry.id.clone());
        let session = Session {
            id: entry.id.clone(),
            title: entry.id.clone(),
            collection: safe_label(&entry.project),
            transcript: transcript_tail(&entry.markdown).to_owned(),
            status: if note.is_empty() {
                "Archived · read-only".into()
            } else {
                format!("Archived · read-only · {}", safe_label(note))
            },
        };
        if let Some(old) = self.sessions.iter_mut().find(|old| old.id == entry.id) {
            *old = session;
        } else {
            self.sessions.push(session);
        }
    }

    pub(super) fn poll_history(&mut self) -> bool {
        let started = self.start_due_history_query(Instant::now());
        let Some(receiver) = &self.history_pending else {
            return false;
        };
        let found = match receiver.try_recv() {
            Ok(found) => found,
            Err(TryRecvError::Empty) => return started,
            Err(TryRecvError::Disconnected) => {
                self.history_pending = None;
                return false;
            }
        };
        self.history_pending = None;
        let scan_query = self.history_scan_query.take();
        if scan_query
            .as_deref()
            .is_some_and(|query| query != self.history_query.to_lowercase())
        {
            return true;
        }
        let mut changed = false;
        for entry in found {
            if let Some(query) = &scan_query {
                self.history_scanned_matches
                    .insert(entry.id.clone(), query.clone());
            }
            self.history_entries.insert(entry.id.clone(), entry.clone());
            if self.sessions.iter().any(|session| session.id == entry.id) {
                continue;
            }
            self.offline_ids.insert(entry.id.clone());
            self.sessions.push(Session {
                id: entry.id.clone(),
                title: entry.id,
                collection: safe_label(&entry.project),
                transcript: transcript_tail(&entry.markdown).to_owned(),
                status: "Archived · read-only".into(),
            });
            changed = true;
        }
        self.prune_unopened_history();
        if self.history_modal && self.history_resume && self.history_explicit {
            match self.history_matches().len() {
                0 => {
                    self.history_modal = false;
                    self.notice = format!(
                        "Resume: no saved session matches {}",
                        safe_label(&self.history_query)
                    );
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
    /// Populate the deterministic screenshot renderer without scanning local
    /// history or contacting a provider.
    #[doc(hidden)]
    pub fn show_history_fixture(&mut self, query: &str, entries: Vec<history::OfflineSession>) {
        self.history_modal = true;
        self.history_resume = false;
        self.history_query = query.to_owned();
        self.history_query_cursor = self.history_query.len();
        self.history_selected = 0;
        self.cancel_history_query();
        for entry in entries {
            self.history_scanned_matches
                .insert(entry.id.clone(), query.to_lowercase());
            self.history_entries.insert(entry.id.clone(), entry.clone());
            if self.sessions.iter().any(|session| session.id == entry.id) {
                continue;
            }
            self.offline_ids.insert(entry.id.clone());
            self.sessions.push(Session {
                id: entry.id.clone(),
                title: entry.id,
                collection: safe_label(&entry.project),
                transcript: transcript_tail(&entry.markdown).to_owned(),
                status: "Archived · read-only".into(),
            });
        }
    }

    pub(super) fn history_key(&mut self, key: KeyEvent) -> bool {
        self.history_query_cursor = self.history_query_cursor.min(self.history_query.len());
        while !self
            .history_query
            .is_char_boundary(self.history_query_cursor)
        {
            self.history_query_cursor -= 1;
        }
        match key.code {
            KeyCode::Esc | KeyCode::Char('r')
                if key.code == KeyCode::Esc || key.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                self.history_modal = false;
                self.cancel_history_query();
                self.prune_unopened_history();
            }
            KeyCode::Up => self.history_selected = self.history_selected.saturating_sub(1),
            KeyCode::Down => {
                self.history_selected =
                    (self.history_selected + 1).min(self.history_matches().len().saturating_sub(1))
            }
            KeyCode::Left => {
                self.history_query_cursor = self.history_query[..self.history_query_cursor]
                    .char_indices()
                    .next_back()
                    .map_or(0, |(index, _)| index);
            }
            KeyCode::Right => {
                if let Some(c) = self.history_query[self.history_query_cursor..]
                    .chars()
                    .next()
                {
                    self.history_query_cursor += c.len_utf8();
                }
            }
            KeyCode::Home => self.history_query_cursor = 0,
            KeyCode::End => self.history_query_cursor = self.history_query.len(),
            KeyCode::Backspace => {
                if let Some((index, _)) = self.history_query[..self.history_query_cursor]
                    .char_indices()
                    .next_back()
                {
                    self.history_query.drain(index..self.history_query_cursor);
                    self.history_query_cursor = index;
                    self.history_selected = 0;
                    self.schedule_history_query(Instant::now());
                }
            }
            KeyCode::Delete => {
                if let Some(c) = self.history_query[self.history_query_cursor..]
                    .chars()
                    .next()
                {
                    self.history_query
                        .drain(self.history_query_cursor..self.history_query_cursor + c.len_utf8());
                    self.history_selected = 0;
                    self.schedule_history_query(Instant::now());
                }
            }
            KeyCode::Char(c)
                if !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
            {
                if !unsafe_input_char(c) && self.history_query.len() + c.len_utf8() <= 200 {
                    self.history_query.insert(self.history_query_cursor, c);
                    self.history_query_cursor += c.len_utf8();
                    self.history_selected = 0;
                    self.schedule_history_query(Instant::now());
                }
            }
            KeyCode::Enter => {
                self.open_selected_history();
            }
            _ => return false,
        }
        true
    }

    pub(super) fn open_selected_history(&mut self) {
        if let Some(&index) = self.history_matches().get(self.history_selected) {
            let id = self.sessions[index].id.clone();
            if self.history_resume {
                self.history_modal = false;
                self.cancel_history_query();
                self.history_resume = false;
                if self
                    .groups
                    .iter()
                    .any(|group| group.tabs.iter().any(|tab| tab == &id))
                {
                    for (group_index, group) in self.groups.iter_mut().enumerate() {
                        if let Some(index) = group.tabs.iter().position(|tab| tab == &id) {
                            group.active = index;
                            self.active_group = group_index;
                            self.notice = format!("Session already open · {}", safe_label(&id));
                            return;
                        }
                    }
                }
                if crate::discovery::sessions()
                    .is_ok_and(|rows| rows.iter().any(|row| row.id == id))
                {
                    self.attach_selected(&id);
                    return;
                }
                if !self.manual_tab_available_for(Some(&id)) {
                    return;
                }
                let Some(entry) = self.history_entries.get(&id).cloned() else {
                    self.notice = "Resume unavailable: no saved transcript for this session".into();
                    return;
                };
                let (tx, rx) = mpsc::sync_channel(1);
                self.resume_pending = Some((
                    self.active_group,
                    self.groups[self.active_group]
                        .active_id()
                        .map(str::to_owned),
                    rx,
                ));
                std::thread::spawn(move || {
                    let result = history::resume_plan(&entry);
                    let _ = tx.send((id, result));
                });
                self.notice = "Checking saved conversation…".into();
                return;
            }
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
            self.history_modal = false;
            self.cancel_history_query();
        }
    }

    pub(super) fn poll_resume(&mut self) -> bool {
        let Some((_, _, receiver)) = &self.resume_pending else {
            return false;
        };
        let result = match receiver.try_recv() {
            Ok(result) => result,
            Err(TryRecvError::Empty) => return false,
            Err(TryRecvError::Disconnected) => {
                self.resume_pending = None;
                return false;
            }
        };
        let (group, anchor, _) = self.resume_pending.take().unwrap();
        let (id, plan) = result;
        if !self.groups.get(group).is_some_and(|pane| {
            anchor
                .as_ref()
                .map_or(pane.tabs.is_empty(), |id| pane.tabs.contains(id))
        }) {
            self.notice = "Resume cancelled · original pane is no longer available".into();
            return true;
        }
        match plan {
            Ok(options) if !self.launching => {
                // Recheck live registry immediately before dispatch. The daemon
                // also owns the final uniqueness check on this session ID.
                if crate::discovery::sessions()
                    .is_ok_and(|rows| rows.iter().any(|row| row.id == id))
                {
                    if self.groups.iter().any(|pane| pane.tabs.contains(&id)) {
                        self.notice = format!("Session already open · {}", safe_label(&id));
                    } else if !self.attaching_ids.contains(&id)
                        && self.manual_tab_available_for(Some(&id))
                    {
                        self.attaching_ids.insert(id.clone());
                        self.pending_attaches.push((id.clone(), group));
                        self.notice = format!("Attaching · {}", safe_label(&id));
                    }
                } else {
                    if !self.manual_tab_available_for(Some(&id)) {
                        return true;
                    }
                    self.pending_launches.push((options, None, group));
                    self.launching = true;
                    self.notice = format!("Resuming · {}", safe_label(&id));
                }
            }
            Ok(_) => self.notice = "Wait for the current session launch before resuming".into(),
            Err(reason) => {
                self.notice = format!("Resume unavailable · {reason}; transcript remains readable")
            }
        }
        true
    }
}
