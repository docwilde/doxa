//! Derive pane geometry, focus order and chip hit regions from frontend state.
use super::{
    chip_hint, chip_text, clipped_title, input_request_body, memory_fill_label, panes, prompt_height,
    repo_chip, safe_label, vendor_models, wrapped_rows, App, ChipHit, ChipInfo, Focus, PaneGroup,
    PaneLayout, RailGroupKey, RailRow, Split, INPUT_BLINK_INTERVAL, MIN_PANE_HEIGHT, MIN_PANE_WIDTH,
    MIN_RAIL_WIDTH, SPINNER_FRAMES, SPINNER_INTERVAL,
};
use crate::launch;
use ratatui::layout::Constraint;
use ratatui::layout::Direction;
use ratatui::layout::Layout;
use ratatui::layout::Rect;
use std::collections::{BTreeMap, HashSet};
use std::time::Duration;
use std::time::Instant;
use unicode_width::UnicodeWidthStr;

impl App {
    pub(super) fn rail_urgency(&self, index: usize) -> u8 {
        let id = &self.sessions[index].id;
        if self.waiting_for_input(id) { return 4; }
        // An unreported or invalid limit is unknown, never zero percent.
        if self.session_telemetry.get(id).and_then(|t| t.context_percent)
            .is_some_and(|percent| percent >= 50.0) { return 3; }
        if !self.remote_mode && !crate::remote_client::valid_target(id)
            && self.lore_pending_cache.get(id).is_some_and(|signal| {
                signal.pending == Some(true)
                    && self.session_cwds.get(id) == Some(&signal.cwd)
                    && Instant::now().saturating_duration_since(signal.checked) < Duration::from_secs(90)
            }) { return 2; }
        if self.unread_sessions.contains(id) { return 1; }
        0
    }

    pub(super) fn rail_groups(&self) -> Vec<(RailGroupKey, Vec<RailRow>, u8)> {
        if self.preferences.value("rail_entries") == "panes" { return self.rail_pane_groups(); }
        let mut groups = Vec::new();
        let mut seen = HashSet::new();
        for (heading, item) in self.collections.iter().enumerate() {
            let mut rows = vec![RailRow::Heading(heading)];
            let mut urgency = 0;
            for id in &item.sessions {
                if let Some(index) = self.sessions.iter().position(|session| &session.id == id) {
                    if !self.offline_ids.contains(id) && seen.insert(index)
                        && self.rail_session_visible(index) {
                        urgency = urgency.max(self.rail_urgency(index));
                        if !item.collapsed { rows.push(RailRow::Session(index)); }
                    }
                }
            }
            groups.push((RailGroupKey::Collection(item.name.clone()), rows, urgency));
        }
        let mut projects: BTreeMap<String, Vec<usize>> = BTreeMap::new();
        for index in 0..self.sessions.len() {
            if seen.insert(index) && self.rail_session_visible(index)
                && !self.offline_ids.contains(&self.sessions[index].id) {
                let label = self.rail_project_label(index);
                projects.entry(label.to_owned()).or_default().push(index);
            }
        }
        for (project, sessions) in projects {
            let urgency = sessions.iter().map(|index| self.rail_urgency(*index)).max().unwrap_or(0);
            let mut rows = vec![RailRow::ProjectHeading(project.clone())];
            rows.extend(sessions.into_iter().map(RailRow::Session));
            groups.push((RailGroupKey::Project(project), rows, urgency));
        }
        groups
    }

    /// Pane view has one navigable row per open pane. Detached live sessions
    /// remain individual rows, and hidden tabs only contribute to their pane.
    fn rail_pane_groups(&self) -> Vec<(RailGroupKey, Vec<RailRow>, u8)> {
        let mut groups = Vec::new();
        let mut placed = HashSet::new();
        for (heading, collection) in self.collections.iter().enumerate() {
            let mut rows = vec![RailRow::Heading(heading)];
            let mut urgency = 0;
            let mut members = 0;
            for id in &collection.sessions {
                let Some(index) = self.sessions.iter().position(|session| &session.id == id) else { continue };
                if !placed.insert(index) || self.offline_ids.contains(id) || !self.rail_session_visible(index) { continue; }
                if let Some(row) = self.rail_entry(index) {
                    members += 1;
                    urgency = urgency.max(self.rail_entry_urgency(&row, index));
                    if !collection.collapsed { rows.push(row); }
                }
            }
            if members > 0 || collection.sessions.is_empty() {
                groups.push((RailGroupKey::Collection(collection.name.clone()), rows, urgency));
            }
        }
        let mut projects: BTreeMap<String, Vec<(RailRow, u8)>> = BTreeMap::new();
        for index in 0..self.sessions.len() {
            let id = &self.sessions[index].id;
            if !placed.insert(index) || self.offline_ids.contains(id) || !self.rail_session_visible(index) { continue; }
            if let Some(row) = self.rail_entry(index) {
                let label = self.rail_project_label(index).to_owned();
                let urgency = self.rail_entry_urgency(&row, index);
                projects.entry(label).or_default().push((row, urgency));
            }
        }
        for (project, entries) in projects {
            let urgency = entries.iter().map(|(_, rank)| *rank).max().unwrap_or(0);
            let mut rows = vec![RailRow::ProjectHeading(project.clone())];
            rows.extend(entries.into_iter().map(|(row, _)| row));
            groups.push((RailGroupKey::Project(project), rows, urgency));
        }
        groups
    }

    fn rail_entry(&self, index: usize) -> Option<RailRow> {
        let id = &self.sessions[index].id;
        for (group, pane) in self.groups.iter().enumerate() {
            if pane.tabs.iter().any(|tab| tab == id) {
                return (pane.active_id() == Some(id.as_str())).then_some(RailRow::Pane { group, active: index });
            }
        }
        Some(RailRow::Session(index))
    }

    fn rail_entry_urgency(&self, row: &RailRow, index: usize) -> u8 {
        match row {
            RailRow::Pane { group, .. } => self.groups[*group].tabs.iter().filter_map(|id|
                self.sessions.iter().position(|session| session.id == *id)
                    .filter(|_| !self.offline_ids.contains(id)).map(|index| self.rail_urgency(index)))
                .max().unwrap_or(0),
            _ => self.rail_urgency(index),
        }
    }

    pub(super) fn chip_hint_for(&self, kind: &str, group: usize) -> String {
        if kind=="isolation" { return self.isolation_hint(group); }
        let id = self.groups.get(group).and_then(PaneGroup::active_id);
        if kind == "permission"
            && id.and_then(|id| self.session_identity.get(id))
                .and_then(|identity| identity.0.as_deref()) == Some("codex") {
            let policy = id.and_then(|id| self.permission_modes.get(id)).map(String::as_str).unwrap_or("?");
            match policy {
                "auto" => "Codex auto · no provider approval prompts; sandbox enforced. DOXA tools still use their own review.".into(),
                "full-access" => "Codex full access · no provider approval prompts or sandbox. DOXA tools still use their own review.".into(),
                _ => format!("Codex {policy} · provider approval requests appear here. Click to change for the next turn."),
            }
        } else {
            chip_hint(kind).to_owned()
        }
    }

    pub(super) fn chooser_identity(&self) -> Option<String> {
        let kind = if let Some(index) = self.active_request_index() {
            format!(
                "input_request:{}:{}:{}",
                self.input_requests[index].kind,
                self.input_requests[index].id,
                self.input_requests[index].step
            )
        } else if self.delete_confirmation.is_some() {
            "delete_transcript".into()
        } else if self.settings_menu.is_some() {
            "settings".into()
        } else if self.engine_picker {
            "engine".into()
        } else if self.new_session.is_some() {
            "new_session".into()
        } else if self.effort_picker.is_some() {
            "effort".into()
        } else if self.permission_picker.is_some() {
            "permission".into()
        } else if self.model_picker.is_some() {
            "model".into()
        } else if self.repo_picker.is_some() {
            "repo".into()
        } else if let Some(picker) = &self.lore_picker {
            format!(
                "lore:{}:{}:{}:{}",
                picker.proposal_mode,
                picker.review.is_some(),
                picker.belief_review.is_some(),
                picker.evidence.is_some()
            )
        } else if self.action_menu {
            "actions".into()
        } else if let Some(info) = &self.chip_info {
            format!("chip_info:{}", info.kind)
        } else if self.history_modal {
            "history".into()
        } else if self.queue_picker.is_some() {
            "queue".into()
        } else if self.attach_picker.is_some() {
            "attach".into()
        } else if self.branch_picker.is_some() {
            "branch".into()
        } else if !self.slash_suggestions().is_empty() {
            "slash".into()
        } else {
            return None;
        };
        Some(format!(
            "{}:{}:{kind}",
            self.active_group,
            self.groups[self.active_group].active_id().unwrap_or("")
        ))
    }

    pub(super) fn sync_chooser_state(&self) {
        let identity = self.chooser_identity();
        let mut owner = self.chooser_owner.borrow_mut();
        if *owner != identity {
            *owner = identity;
            self.chooser_height_override.set(None);
            self.chooser_view_start.set(0);
        }
    }

    pub(super) fn adjust_split(&mut self, delta: i16) -> bool {
        self.split_percent = (self.split_percent as i16 + delta).clamp(20, 80) as u16;
        true
    }

    pub(super) fn rail_rows(&self) -> Vec<RailRow> {
        let mut groups = self.rail_groups();
        if self.preferences.value("collection_sort") == "urgency" {
            groups.sort_by_key(|(key, _, _)| self.rail_sort_order.iter().position(|saved| saved == key).unwrap_or(usize::MAX));
        }
        let mut rows: Vec<_> = groups.into_iter().flat_map(|(_, rows, _)| rows).collect();
        let mut seen = HashSet::new();
        for item in &self.collections {
            for id in &item.sessions {
                if let Some(index) = self.sessions.iter().position(|session| &session.id == id) {
                    if !self.offline_ids.contains(id) { seen.insert(index); }
                }
            }
        }
        let mut past = Vec::new();
        for index in 0..self.sessions.len() {
            if seen.insert(index) && self.rail_session_visible(index)
                && self.offline_ids.contains(&self.sessions[index].id) { past.push(index); }
        }
        if !past.is_empty() {
            rows.push(RailRow::PastHeading);
            rows.extend(past.into_iter().map(RailRow::Session));
        }
        rows
    }

    /// Keep the displayed group order frozen while marks change, blink, or a
    /// person has the pointer/keyboard in the rail. Child session order is never
    /// sorted. The baseline order breaks equal-urgency ties deterministically.
    pub(super) fn tick_rail_sort(&mut self, now: Instant) -> bool {
        if self.preferences.value("collection_sort") != "urgency" {
            self.rail_sort_signature.clear();
            self.rail_sort_order.clear();
            return false;
        }
        if self.layout(self.size).rail.is_none() { return false; }
        let groups = self.rail_groups();
        let signature = groups.iter().map(|(key, _, urgency)| (key.clone(), *urgency)).collect::<Vec<_>>();
        if self.rail_sort_signature != signature {
            self.rail_sort_signature = signature;
            self.rail_sort_changed_at = now;
            return false;
        }
        if self.rail_pointer_inside || self.focus == super::Focus::Rail
            || now.saturating_duration_since(self.rail_sort_changed_at) < Duration::from_millis(1500)
            || now.saturating_duration_since(self.rail_last_interaction) < Duration::from_millis(1500) {
            return false;
        }
        let mut sorted = groups.iter().map(|(key, _, rank)| (key.clone(), *rank)).collect::<Vec<_>>();
        sorted.sort_by_key(|(_, rank)| std::cmp::Reverse(*rank));
        let next = sorted.into_iter().map(|(key, _)| key).collect::<Vec<_>>();
        if next == self.rail_sort_order { return false; }
        let selected = self.rail_order().get(self.rail_selected).map(|index| self.sessions[*index].id.clone());
        self.rail_sort_order = next;
        if let Some(selected) = selected {
            if let Some(index) = self.rail_order().iter().position(|index| self.sessions[*index].id == selected) {
                self.rail_selected = index;
            }
        }
        true
    }

    fn rail_session_visible(&self, index: usize) -> bool {
        let id = &self.sessions[index].id;
        if self.groups.iter().any(|group| group.tabs.contains(id)) {
            return true;
        }
        // Closed tabs stay resumable, but do not return to the active rail.
        !self.offline_ids.contains(id) && !self.detached_this_run.contains(id)
    }

    pub(super) fn rail_order(&self) -> Vec<usize> {
        self.rail_rows()
            .into_iter()
            .filter_map(|row| match row {
                RailRow::Session(index) | RailRow::Pane { active: index, .. } => Some(index),
                _ => None,
            })
            .collect()
    }

    pub(super) fn rail_session_at(&self, column: u16, row: u16) -> Option<&str> {
        let rail = self.layout(self.size).rail?;
        if column <= rail.x
            || column >= rail.right().saturating_sub(1)
            || row <= rail.y
            || row >= rail.bottom().saturating_sub(1)
        {
            return None;
        }
        let rows = self.rail_rows();
        let index = usize::from(row - rail.y - 1) + self.rail_view_start(rail, &rows);
        match rows.get(index)? {
            RailRow::Session(index) | RailRow::Pane { active: index, .. } => Some(&self.sessions[*index].id),
            _ => None,
        }
    }

    pub(super) fn rail_view_start(&self, area: Rect, rows: &[RailRow]) -> usize {
        let visible = usize::from(area.height.saturating_sub(2));
        if visible == 0 || rows.len() <= visible {
            return 0;
        }
        let selected = self.rail_order().get(self.rail_selected).copied();
        let row = rows.iter().position(|item| match item {
            RailRow::Session(index) | RailRow::Pane { active: index, .. } => Some(*index) == selected,
            _ => false,
        }).unwrap_or(0);
        row.saturating_sub(visible - 1).min(rows.len() - visible)
    }

    pub(super) fn pane_count(&self) -> usize {
        if self.pane_tree.is_some() {
            self.groups.len()
        } else if self.split_requested
            || self.active_group > 0
            || self.groups.iter().skip(1).any(|g| !g.tabs.is_empty())
        {
            2
        } else {
            1
        }
    }

    pub(super) fn split_active_pane(&mut self, orientation: Split) {
        if self.pane_tree.is_none() && self.groups[1].tabs.is_empty() {
            self.split = orientation;
            self.split_requested = true;
            return;
        }
        if self.groups.len() >= panes::MAX_PANES {
            self.notice = format!("Pane limit is {}", panes::MAX_PANES);
            return;
        }
        let layout = self.layout(self.size);
        let Some(regions) = layout.panes else {
            self.notice = "Enlarge terminal before splitting a pane".into();
            return;
        };
        let pane = regions[self.active_group];
        if (orientation == Split::Vertical && pane.width < MIN_PANE_WIDTH * 2)
            || (orientation == Split::Horizontal && pane.height < MIN_PANE_HEIGHT * 2)
        {
            self.notice = "Enlarge this pane before splitting it".into();
            return;
        }
        let mut tree = self
            .pane_tree
            .clone()
            .unwrap_or_else(|| panes::Tree::pair(self.split, self.split_percent));
        let next = self.groups.len();
        if !tree.split(self.active_group, next, orientation, 0) {
            self.notice = "Pane nesting limit reached".into();
            return;
        }
        self.groups.push(PaneGroup {
            tabs: Vec::new(),
            active: 0,
            scroll: 0,
        });
        self.pane_tree = Some(tree);
        self.split_requested = true;
        self.notice = format!("Pane {} created · /pane {} to focus it", next + 1, next + 1);
    }

    pub(super) fn pane_group_two_exists(&self) -> bool {
        self.pane_tree.is_some()
            || self.split_requested
            || self.active_group > 0
            || self
                .groups
                .iter()
                .skip(1)
                .any(|group| !group.tabs.is_empty())
    }

    pub(super) fn layout(&self, area: Rect) -> PaneLayout {
        let min_body = if self.split == Split::Vertical {
            MIN_PANE_WIDTH * 2
        } else {
            MIN_PANE_WIDTH
        };
        let visible = if self.sidebar_auto {
            self.sessions.len() > 1 || !self.collections.is_empty()
        } else {
            self.rail_visible
        };
        let rail_width = if visible && area.width >= 70 {
            self.rail_width.clamp(
                MIN_RAIL_WIDTH,
                area.width.saturating_sub(min_body).max(MIN_RAIL_WIDTH),
            )
        } else {
            0
        };
        let (rail, body) = if rail_width > 0 {
            let chunks = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Length(rail_width), Constraint::Min(1)])
                .split(area);
            (Some(chunks[0]), chunks[1])
        } else {
            (None, area)
        };
        let min_ok = if self.split == Split::Vertical {
            body.width >= MIN_PANE_WIDTH * 2
        } else {
            body.height >= MIN_PANE_HEIGHT * 2
        };
        if let Some(tree) = &self.pane_tree {
            let regions = tree.rects(body, self.groups.len());
            let panes = regions
                .iter()
                .all(|rect| rect.width >= MIN_PANE_WIDTH && rect.height >= MIN_PANE_HEIGHT)
                .then_some(regions);
            return PaneLayout {
                outer: area,
                rail,
                body,
                panes,
            };
        }
        let panes = (min_ok
            && (self.split_requested
                || self.active_group == 1
                || !self.groups[1].tabs.is_empty()
                || self.diff_pane))
            .then(|| {
                let desired = self.pane_rects(body, self.split_percent);
                let minimum = if self.split == Split::Vertical {
                    MIN_PANE_WIDTH
                } else {
                    MIN_PANE_HEIGHT
                };
                let size = |rect: Rect| {
                    if self.split == Split::Vertical {
                        rect.width
                    } else {
                        rect.height
                    }
                };
                if size(desired[0]) >= minimum && size(desired[1]) >= minimum {
                    desired.to_vec()
                } else {
                    (0..=100)
                        .map(|percent| self.pane_rects(body, percent))
                        .filter(|pair| size(pair[0]) >= minimum && size(pair[1]) >= minimum)
                        .min_by_key(|pair| size(pair[0]).abs_diff(size(desired[0])))
                        .unwrap_or(desired)
                        .to_vec()
                }
            });
        PaneLayout {
            outer: area,
            rail,
            body,
            panes,
        }
    }

    pub(super) fn pane_rects(&self, body: Rect, percent: u16) -> [Rect; 2] {
        let direction = if self.split == Split::Vertical {
            Direction::Horizontal
        } else {
            Direction::Vertical
        };
        let chunks = Layout::default()
            .direction(direction)
            .constraints([
                Constraint::Percentage(percent),
                Constraint::Percentage(100 - percent),
            ])
            .split(body);
        [chunks[0], chunks[1]]
    }

    /// Space for a chooser inside the active pane, immediately above its
    /// prompt. Reserving this space keeps the transcript and prompt visible.
    pub(super) fn chooser_rect(&self, pane: Rect) -> Option<Rect> {
        self.sync_chooser_state();
        let wanted = if let Some(index) = self.active_request_index() {
            let (body, _, _) = input_request_body(
                &self.input_requests[index],
                usize::from(pane.width.saturating_sub(4)),
            );
            wrapped_rows(&body, usize::from(pane.width.saturating_sub(2)))
                .saturating_add(2)
                .clamp(6, 18) as u16
        } else if self.delete_confirmation.is_some() {
            6
        } else if self.settings_menu.is_some() {
            8
        } else if self.engine_picker {
            7
        } else if let Some(form) = &self.new_session {
            if !vendor_models(form.engine).is_empty() {
                10
            } else if form.engine == launch::Engine::Claude {
                9
            } else {
                8
            }
        } else if let Some(picker) = &self.effort_picker {
            (4 + picker.levels.len()).clamp(5, 10) as u16
        } else if self.permission_picker.is_some() {
            10
        } else if let Some(picker) = &self.model_picker {
            (6 + picker.models.len()
                + usize::from(
                    picker.catalog_pending || !picker.loading && picker.models.is_empty(),
                ))
            .clamp(7, 15) as u16
        } else if let Some(picker) = &self.repo_picker {
            (picker.paths.len() + 3).clamp(5, 15) as u16
        } else if let Some(picker) = &self.lore_picker {
            if picker.review.is_some() || picker.belief_review.is_some() {
                19
            } else if picker.proposal_mode {
                (7 + picker.proposals.len()).clamp(5, 19) as u16
            } else if let Some((_, evidence)) = &picker.evidence {
                let rows = evidence.len().saturating_mul(2);
                (if rows <= 4 { 4 + rows } else { 7 + rows }).clamp(5, 19) as u16
            } else {
                let rows = picker.rows.len();
                (3 + rows).clamp(5, 19) as u16
            }
        } else if self.action_menu {
            (self.action_rows().len() + 3).clamp(5, 15) as u16
        } else if self.memory_manager.is_some() {
            19
        } else if let Some(menu) = &self.operations_menu {
            (menu.lines(usize::from(pane.width)).len() + 2).clamp(7, 19) as u16
        } else if self
            .chip_info
            .as_ref()
            .is_some_and(|info| info.kind == "memory")
            && self.memory_list.is_some()
        {
            (self.memory_list.as_ref().unwrap().indices().len() + 3).clamp(7, 19) as u16
        } else if self.chip_info.is_some() {
            self.chip_info.as_ref().map_or(5, |info| {
                if matches!(
                    info.kind,
                    "memory"
                        | "usage"
                        | "context"
                        | "help"
                        | "sessions"
                        | "about"
                        | "fleet"
                        | "fleet_review"
                        | "fleet_dependency_review"
                        | "isolation"
                        | "routing"
                        | "native_plugin"
                        | "native_status"
                ) {
                    (info.lines.len() + 2).clamp(7, 19) as u16
                } else {
                    5
                }
            })
        } else if self.history_modal {
            let rows: usize = self
                .history_matches()
                .iter()
                .map(|&index| 1 + self.history_snippets(&self.sessions[index].id).len().min(2))
                .sum();
            (rows + 3).clamp(5, 15) as u16
        } else if let Some(picker) = &self.queue_picker {
            (picker.rows.len() + 3).clamp(5, 15) as u16
        } else if self.attach_picker.is_some() {
            (self.attach_matches().len() + 3).clamp(5, 15) as u16
        } else if let Some(picker) = &self.branch_picker {
            (picker.branches.len() + 3).clamp(5, 15) as u16
        } else if !self.slash_suggestions().is_empty() {
            (self.slash_suggestions().len() + 2).clamp(5, 10) as u16
        } else {
            return None;
        };
        let group = &self.groups[self.active_group];
        let draft = group.active_id().map_or("", |_| self.input.as_str());
        let prompt = prompt_height(draft, pane.height);
        let available = pane.height.saturating_sub(3 + prompt + 1 + 1 + 1);
        let height = self
            .chooser_height_override
            .get()
            .unwrap_or(wanted)
            .min(available);
        if height < 5 || pane.width < 18 {
            return None;
        }
        Some(Rect::new(
            pane.x,
            pane.bottom().saturating_sub(prompt + 1 + 1 + height),
            pane.width,
            height,
        ))
    }

    pub(super) fn active_chooser_rect(&self) -> Option<Rect> {
        let layout = self.layout(self.size);
        let pane = layout
            .panes
            .map_or(layout.body, |panes| panes[self.active_group]);
        self.chooser_rect(pane)
    }

    pub(super) fn chips(&self, index: usize) -> Vec<(&'static str, String)> {
        let id = self.groups[index].active_id();
        let identity = id.and_then(|id| self.session_identity.get(id));
        if self.pane_remote(index) {
            let host = id.and_then(|id| id.split_once('~').map(|pair| pair.0)).unwrap_or("hub");
            let mut chips = vec![("remote", format!("Remote · {host}"))];
            if let Some(engine) = identity.and_then(|pair| pair.0.as_deref()) {
                chips.push(("remote_engine", engine.to_owned()));
            }
            if let Some(model) = identity.and_then(|pair| pair.1.as_deref()) {
                chips.push(("remote_model", model.to_owned()));
            }
            return chips;
        }
        let telemetry = id.and_then(|id| self.session_telemetry.get(id));
        let mut chips = Vec::new();
        // Display the provider-reported permission policy even when this
        // engine does not expose a mutable mode control.
        if let Some(mode) = id.and_then(|id| self.permission_modes.get(id)) {
            chips.push(("permission", mode.clone()));
        } else if id.is_some_and(|id| {
            self.session_capabilities
                .get(id)
                .is_some_and(|capabilities| capabilities.permission_modes)
        }) {
            chips.push(("permission", "?".to_owned()));
        }
        if let Some(label) = self.native_status.chip_label() {
            chips.push(("native_status", label));
        }
        if let Some(engine) = identity.and_then(|pair| pair.0.as_deref()) {
            chips.push(("engine", engine.to_owned()));
        } else {
            chips.push(("engine", "Engine".to_owned()));
        }
        if let Some(model) = identity.and_then(|pair| pair.1.as_deref()) {
            chips.push(("model", model.to_owned()));
        } else {
            chips.push(("model", "Model".to_owned()));
        }
        if identity.and_then(|pair| pair.0.as_deref()) == Some("router") {
            if let Some(routing) = telemetry.and_then(|value| value.routing.as_ref()) {
                chips.push(("routing", routing.label()));
            }
        }
        let effort = id
            .and_then(|id| self.session_efforts.get(id))
            .map(String::as_str)
            .unwrap_or("?");
        if identity.and_then(|pair| pair.0.as_deref()) != Some("router") {
            chips.push(("effort", effort.to_owned()));
        }
        if let Some(value)=telemetry.and_then(|t|t.isolation.as_ref()) {
            chips.push(("isolation",value["label"].as_str().unwrap_or("unavailable").into()));
        }
        if let Some(status) = id
            .and_then(|id| self.repo_cache.get(id))
            .and_then(|(status, _)| status.as_ref())
        {
            let (kind, label) = repo_chip(status);
            chips.push((
                kind,
                if self.preferences.on("nerd_font") {
                    label.replace("⎇", "\u{e0a0}")
                } else {
                    label
                },
            ));
        }
        let context = telemetry
            .and_then(|value| value.context_percent)
            .map(|v| format!("{v:.0}%"))
            .unwrap_or_else(|| "?".into());
        let absolute = if self.preferences.on("ctx_absolute") && self.size.width >= 100 {
            telemetry
                .and_then(|v| v.context_tokens)
                .map(|used| {
                    format!(
                        " {used}/{}",
                        telemetry
                            .and_then(|v| v.context_limit)
                            .map(|n| n.to_string())
                            .unwrap_or_else(|| "?".into())
                    )
                })
                .unwrap_or_default()
        } else {
            String::new()
        };
        chips.push(("context", format!("Ctx {context}{absolute}")));
        let memory = self
            .memory_cache
            .get(id.unwrap_or(""))
            .and_then(|(usage, _)| *usage)
            .map(|usage| {
                format!(
                    "{} {}/u {}",
                    if self
                        .memory_repo
                        .get(id.unwrap_or(""))
                        .copied()
                        .unwrap_or(false)
                    {
                        "p"
                    } else {
                        "f"
                    },
                    memory_fill_label(usage.project_chars, usage.project_cap_chars),
                    memory_fill_label(usage.user_chars, usage.user_cap_chars)
                )
            })
            .unwrap_or_else(|| "u ? · scope ?".to_owned());
        chips.push(("memory", memory));
        let beliefs = telemetry
            .and_then(|value| value.lore.as_deref())
            .filter(|label| label.ends_with(" beliefs"))
            .unwrap_or("Beliefs");
        chips.push(("beliefs", beliefs.to_owned()));
        let engine = identity.and_then(|pair| pair.0.as_deref());
        if let Some(label) = telemetry
            .and_then(|value| value.billing_label(engine))
            .or_else(|| match engine {
                Some("deepseek" | "glm" | "router") => Some("$?".into()),
                _ => None,
            })
        {
            chips.push(("cost", label));
        }
        if engine == Some("deepseek") {
            if let Some(balance) = telemetry.and_then(|value| value.balance.as_deref()) {
                chips.push(("balance", format!("Balance {balance}")));
            }
        }
        chips
    }

    pub(super) fn waiting_for_input(&self, id: &str) -> bool {
        self.input_requests
            .iter()
            .any(|request| request.session_id == id && !request.sending)
    }

    pub(super) fn activity_label(&self, id: &str) -> Option<&'static str> {
        let (running, queued) = self.session_activity.get(id).copied().unwrap_or_default();
        if self.local_shell_jobs.iter().any(|job| job.session == id) {
            Some("Local shell")
        } else if running {
            Some("Processing")
        } else if queued > 0 {
            Some("Queued")
        } else {
            None
        }
    }

    pub(super) fn tick_spinner(&mut self, now: Instant) -> bool {
        if !self.session_activity.values().any(|(running, queued)| *running || *queued > 0)
            && self.local_shell_jobs.is_empty() {
            self.spinner_at = now;
            return false;
        }
        if now.duration_since(self.spinner_at) < SPINNER_INTERVAL {
            return false;
        }
        self.spinner_at = now;
        self.spinner_frame = (self.spinner_frame + 1) % SPINNER_FRAMES.len();
        true
    }

    /// Called by the event loop at its normal poll cadence. Redraws only once
    /// per phase while a session actually has an unresolved request.
    pub(super) fn tick_blink(&mut self, now: Instant) -> bool {
        if !self.input_requests.iter().any(|request| !request.sending) {
            self.blink_at = now;
            return std::mem::replace(&mut self.blink_on, true) == false;
        }
        if now.duration_since(self.blink_at) < INPUT_BLINK_INTERVAL {
            return false;
        }
        self.blink_at = now;
        self.blink_on = !self.blink_on;
        true
    }

    pub(super) fn tab_title<'a>(&'a self, id: &'a str) -> std::borrow::Cow<'a,str> {
        let title=self.sessions.iter().find(|session| session.id == id)
            .map(|session| session.title.as_str()).unwrap_or(id);
        if crate::remote_client::valid_target(id){std::borrow::Cow::Owned(format!("◎ {title}"))}
        else{std::borrow::Cow::Borrowed(title)}
    }

    /// Keep the active tab visible when the header is narrower than all titles.
    /// The four-cell gutters hold the hidden-tab counts and arrow controls.
    pub(super) fn tab_window(&self, index: usize, width: u16) -> (usize, usize, bool) {
        let group = &self.groups[index];
        let count = group.tabs.len();
        if count == 0 { return (0, 0, false); }
        let inner = usize::from(width.saturating_sub(2));
        let all_width = group.tabs.iter().map(|id| self.tab_title(id).width()).sum::<usize>()
            + count.saturating_sub(1) * 3;
        if all_width <= inner { return (0, count, false); }
        let capacity = inner.saturating_sub(10).max(1);
        let active = group.active.min(count - 1);
        let mut start = active;
        let mut end = active + 1;
        let mut used = self.tab_title(&group.tabs[active]).width().min(capacity);
        while start > 0 {
            let next = self.tab_title(&group.tabs[start - 1]).width() + 3;
            if used + next > capacity { break; }
            start -= 1; used += next;
        }
        while end < count {
            let next = self.tab_title(&group.tabs[end]).width() + 3;
            if used + next > capacity { break; }
            end += 1; used += next;
        }
        (start, end, true)
    }

    pub(super) fn tab_at(&self, index: usize, pane: Rect, column: u16) -> Option<usize> {
        let header = self.pane_regions(index, pane)[0];
        let clock_width = if index == self.active_group && !self.clock_text.is_empty() {
            (self.clock_text.width() + 2).min(usize::from(header.width.saturating_sub(15))) as u16
        } else { 0 };
        let width = header.width.saturating_sub(clock_width);
        let (start, end, overflow) = self.tab_window(index, width);
        if start == end { return None; }
        let inner_left = header.x.saturating_add(1);
        let inner_right = header.x.saturating_add(width.saturating_sub(1));
        if overflow && column < inner_left.saturating_add(5) {
            return (start > 0).then_some(start - 1);
        }
        if overflow && column >= inner_right.saturating_sub(5) {
            return (end < self.groups[index].tabs.len()).then_some(end);
        }
        let mut x = inner_left.saturating_add(if overflow { 5 } else { 0 });
        let capacity = usize::from(width.saturating_sub(2 + if overflow { 10 } else { 0 }));
        let mut remaining = capacity;
        for position in start..end {
            let label = self.tab_title(&self.groups[index].tabs[position]);
            let length = label.width().min(remaining) as u16;
            if column >= x && column < x.saturating_add(length) { return Some(position); }
            x = x.saturating_add(length);
            remaining = remaining.saturating_sub(usize::from(length));
            if position + 1 < end { x = x.saturating_add(3); remaining = remaining.saturating_sub(3); }
        }
        None
    }

    pub(super) fn focus_ring(&self) -> Vec<Focus> {
        let layout = self.layout(self.size);
        let pane = layout
            .panes
            .as_ref()
            .and_then(|panes| panes.get(self.active_group))
            .copied()
            .unwrap_or(layout.body);
        let mut ring = vec![Focus::Prompt, Focus::Tabs];
        if layout.rail.is_some() {
            ring.push(Focus::Rail);
        }
        ring.push(Focus::Transcript);
        ring.extend(
            self.chip_window(
                self.active_group,
                usize::from(self.pane_regions(self.active_group, pane)[3].width),
            )
            .into_iter()
            .map(|(kind, _)| Focus::Chip(kind)),
        );
        ring
    }

    pub(super) fn cycle_focus(&mut self, reverse: bool) {
        let ring = self.focus_ring();
        let current = ring
            .iter()
            .position(|focus| *focus == self.focus)
            .unwrap_or(0);
        let next = if reverse {
            (current + ring.len() - 1) % ring.len()
        } else {
            (current + 1) % ring.len()
        };
        self.focus = ring[next];
    }

    pub(super) fn activate_chip(&mut self, kind: &'static str, group: usize) {
        match kind {
            "permission" => self.open_permission_picker(),
            "engine" => self.open_engine_picker(),
            "model" => self.open_model_picker(),
            "effort" => self.open_effort_picker(),
            "isolation" => self.open_isolation_info(group),
            "beliefs" => self.open_lore_picker(),
            "repo" | "directory" => self.open_repo_picker(group),
            "memory" => self.open_memory_menu(group),
            "more" => {
                let layout = self.layout(self.size);
                let pane = layout
                    .panes
                    .as_ref()
                    .and_then(|panes| panes.get(group))
                    .copied()
                    .unwrap_or(layout.body);
                let visible =
                    self.chip_window(group, usize::from(self.pane_regions(group, pane)[3].width));
                let count = visible.len().saturating_sub(1).max(1);
                self.chip_offsets[group] =
                    (self.chip_offsets[group] + count) % self.chips(group).len();
            }
            kind => self.open_chip_info(kind, group),
        }
    }

    pub(super) fn chip_window(&self, index: usize, width: usize) -> Vec<(&'static str, String)> {
        let all = self.chips(index);
        let full_width = all
            .iter()
            .map(|(kind, label)| chip_text(kind, label).width())
            .sum::<usize>()
            + all.len().saturating_sub(1);
        if full_width <= width {
            return all;
        }
        let budget = width.saturating_sub(chip_text("more", "+8").width() + 1);
        let mut shown = Vec::new();
        let mut used = 0;
        let start = self.chip_offsets[index] % all.len();
        for step in 0..all.len() {
            let (kind, label) = &all[(start + step) % all.len()];
            let gap = usize::from(!shown.is_empty());
            let room = budget.saturating_sub(used + gap);
            if room < 3 {
                break;
            }
            let text_width = chip_text(kind, label).width();
            if text_width > room {
                if shown.is_empty() {
                    let decoration = chip_text(kind, "").width();
                    let clipped = clipped_title(label, room.saturating_sub(decoration)).0;
                    shown.push((*kind, clipped));
                }
                break;
            }
            used += gap + text_width;
            shown.push((*kind, label.clone()));
        }
        let hidden = all.len().saturating_sub(shown.len());
        if hidden > 0 {
            shown.push(("more", format!("+{hidden}")));
        }
        shown
    }

    pub(super) fn pane_regions(&self, index: usize, area: Rect) -> [Rect; 6] {
        let group = &self.groups[index];
        let draft = group
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
        let chooser_height = if self.active_group == index {
            self.chooser_rect(area).map_or(0, |rect| rect.height)
        } else {
            0
        };
        let regions = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(3),
                Constraint::Min(1),
                Constraint::Length(chooser_height),
                Constraint::Length(1),
                Constraint::Length(prompt_height(draft, area.height)),
                Constraint::Length(1),
            ])
            .split(area);
        std::array::from_fn(|index| regions[index])
    }

    pub(super) fn chip_hit_at(&self, column: u16, row: u16) -> Option<ChipHit> {
        if let Some(hits) = self.rendered_chip_hits.borrow().as_ref() {
            return hits
                .iter()
                .find(|hit| {
                    hit.rect
                        .contains(ratatui::layout::Position::new(column, row))
                })
                .cloned();
        }
        // Before the first paint, tests and synthetic input may still use
        // the current size. Interactive input always uses painted regions.
        let layout = self.layout(self.size);
        let panes = layout
            .panes
            .map(|panes| panes.into_iter().enumerate().collect::<Vec<_>>())
            .unwrap_or_else(|| vec![(self.active_group, layout.body)]);
        for (group, pane) in panes {
            if self.diff_pane && group != self.active_group {
                continue;
            }
            let strip = self.pane_regions(group, pane)[3];
            if row != strip.y || column < strip.x || column >= strip.right() {
                continue;
            }
            let mut x = strip.x;
            for (kind, label) in self.chip_window(group, usize::from(strip.width)) {
                let width = chip_text(kind, &label).width() as u16;
                let end = x.saturating_add(width).min(strip.right());
                if column >= x && column < end {
                    return Some(ChipHit {
                        group,
                        kind,
                        rect: Rect::new(x, strip.y, end - x, 1),
                        pane,
                    });
                }
                x = end.saturating_add(1);
            }
        }
        None
    }

    pub(super) fn link_at(&self, column: u16, row: u16) -> Option<String> {
        self.visible_links
            .borrow()
            .iter()
            .find(|(rect, _)| rect.contains(ratatui::layout::Position::new(column, row)))
            .map(|(_, url)| url.clone())
    }

    pub(super) fn pointer_on_link(&self) -> bool {
        !self.link_interaction_blocked()
            && self
                .link_hover_position
                .is_some_and(|(column, row)| self.link_at(column, row).is_some())
    }

    pub(super) fn link_interaction_blocked(&self) -> bool {
        self.active_chooser_rect().is_some()
            || self.active_request_index().is_some()
            || self.map_modal
            || self.diff_modal
            || self.tool_modal
            || self.action_menu
            || self.history_modal
            || self.queue_picker.is_some()
            || self.attach_picker.is_some()
            || self.branch_picker.is_some()
            || self.lore_picker.is_some()
            || self.settings_menu.is_some()
            || self.model_picker.is_some()
            || self.effort_picker.is_some()
            || self.permission_picker.is_some()
            || self.engine_picker
            || self.new_session.is_some()
            || self.repo_picker.is_some()
            || self.chip_info.is_some()
            || self.stop_confirmation.is_some()
            || self.delete_confirmation.is_some()
    }

    pub(super) fn repo_detail(&self, group: usize) -> Option<String> {
        let id = self.groups[group].active_id()?;
        let (
            Some(doxa_worktrees::RepoStatus::Repository {
                base,
                checked_out,
                worktree,
                ..
            }),
            _,
        ) = self.repo_cache.get(id)?
        else {
            return None;
        };
        let state = if let Some(worktree) = worktree {
            if worktree == "linked worktree" {
                "linked worktree".to_owned()
            } else {
                format!("managed worktree {}", safe_label(worktree))
            }
        } else {
            "main checkout".to_owned()
        };
        Some(format!(
            "base {} · HEAD {} · {state}",
            safe_label(base.as_deref().unwrap_or("?")),
            safe_label(checked_out.as_deref().unwrap_or("detached"))
        ))
    }

    pub(super) fn open_chip_info(&mut self, kind: &'static str, group: usize) {
        self.memory_manager = None;
        self.retire_operations();
        if matches!(kind, "context" | "cost") {
            self.active_group = group;
            self.open_diagnostic(if kind == "cost" { "usage" } else { "context" });
            return;
        }
        let mut label = self
            .chips(group)
            .into_iter()
            .find(|(candidate, _)| *candidate == kind)
            .map(|(_, label)| label)
            .unwrap_or_default();
        let lines = if kind == "native_status" { self.native_status.ledger_lines() }
            else if kind == "routing" { self.groups[group].active_id()
                .and_then(|id| self.session_telemetry.get(id)).and_then(|t| t.routing.as_ref())
                .map(|routing| routing.lines()).unwrap_or_default() }
            else { Vec::new() };
        if kind == "repo" {
            if let Some(detail) = self.repo_detail(group) {
                label.push_str(" · ");
                label.push_str(&detail);
            }
        }
        self.active_group = group;
        self.chip_info = Some(ChipInfo {
            kind,
            label,
            lines,
            scroll: 0,
            owner: (kind == "routing").then(|| self.groups[group].active_id()
                .map(|id| (id.to_owned(), String::new()))).flatten(),
        });
        if self.active_chooser_rect().is_none() {
            self.chip_info = None;
            self.notice = "Enlarge active pane to inspect chip details".into();
        }
    }

    /// One dwell timer for the actual painted chip. Input focus has its own
    /// highlight and does not start or extend pointer tooltips.
    pub(super) fn tick_chip_hover(&mut self, now: Instant) -> bool {
        if self.link_interaction_blocked() || self.chip_hover.is_none() {
            let changed = self.chip_tooltip_visible
                || (self.link_interaction_blocked() && self.chip_hover.is_some());
            self.chip_hover = None;
            self.chip_hover_started = None;
            self.chip_tooltip_visible = false;
            return changed;
        }
        let hit = self.chip_hover.as_ref().unwrap();
        if self
            .chip_hover_started
            .as_ref()
            .is_none_or(|(owner, _)| owner != hit)
        {
            self.chip_hover_started = Some((hit.clone(), now));
            let changed = self.chip_tooltip_visible;
            self.chip_tooltip_visible = false;
            return changed;
        }
        if !self.chip_tooltip_visible
            && now.saturating_duration_since(self.chip_hover_started.as_ref().unwrap().1)
                >= Duration::from_millis(500)
        {
            self.chip_tooltip_visible = true;
            return true;
        }
        false
    }
}
