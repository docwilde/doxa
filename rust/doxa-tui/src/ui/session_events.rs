//! Session state reduction for decoded daemon frames.
//!
//! Provider JSON remains the boundary format; each branch retains its existing
//! ownership checks before reducing UI state.

use std::collections::HashSet;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use doxa_engines::EngineCapabilities;

use super::transcript_events::{
    append_transcript, append_turn_heading, set_reasoning_marker, structured_event, transcript_tail,
};
use super::{
    context_detail_lines, permission_index, safe_label, session_controls, unsafe_input_char, App,
    BranchPicker, ClearSwap, DaemonUpdate, InputRequest, QueueRow, Session, MAX_INPUT_BYTES,
    MAX_INPUT_REQUESTS, MAX_REASONING_DISPLAY_CHARS,
};
use crate::markdown;

impl App {
    // Positional folds are meaningful only while the retained transcript prefix
    // is unchanged. Provider call IDs remain stable across display-tail eviction.
    fn forget_positional_folds(&mut self, id: &str) {
        use super::transcript_tools::FoldKey;
        let stable = |key: &FoldKey| matches!(key, FoldKey::Tool(call) if !call.starts_with("legacy:Section("));
        if let Some(keys) = self.expanded_tool_sections.get_mut(id) {
            keys.retain(stable);
        }
        if self.selected_tool_sections.get(id).is_some_and(|key| !stable(key)) {
            self.selected_tool_sections.remove(id);
        }
        if self.tool_section_hover.as_ref().is_some_and(|(owner, key)| owner == id && !stable(key)) {
            self.tool_section_hover = None;
        }
    }

    fn transcript_evicted(&mut self, id: &str, clipped: bool) {
        if clipped {
            self.forget_positional_folds(id);
            self.notice = "Transcript tail limited to 512 KiB".into();
        }
    }

    pub(super) fn invalidate_repo(&mut self, id: &str) {
        self.repo_cache.remove(id);
        let epoch = self.repo_epoch.entry(id.to_owned()).or_default();
        *epoch = epoch.wrapping_add(1);
    }

    pub fn apply_update(&mut self, update: DaemonUpdate) {
        match update {
            DaemonUpdate::Upsert(mut session) => {
                session.transcript = transcript_tail(&session.transcript).to_owned();
                if self.sessions.iter().find(|old| old.id == session.id)
                    .is_some_and(|old| old.transcript != session.transcript) {
                    self.forget_positional_folds(&session.id);
                }
                self.offline_ids.remove(&session.id);
                if let Some(existing) = self.sessions.iter_mut().find(|s| s.id == session.id) {
                    *existing = session;
                } else {
                    let id = session.id.clone();
                    self.sessions.push(session);
                    if self.groups[0].tabs.is_empty() {
                        self.groups[0].tabs.push(id);
                    }
                }
            }
            DaemonUpdate::Transcript { id, markdown } => {
                let retained = transcript_tail(&markdown);
                if self.sessions.iter().find(|s| s.id == id).is_some_and(|s| s.transcript != retained) {
                    self.forget_positional_folds(&id);
                }
                if let Some(s) = self.sessions.iter_mut().find(|s| s.id == id) {
                    s.transcript = retained.to_owned();
                }
            }
            DaemonUpdate::Status { id, text } => {
                if let Some(s) = self.sessions.iter_mut().find(|s| s.id == id) {
                    s.status = text;
                }
            }
        }
        self.rail_selected = self
            .rail_selected
            .min(self.rail_order().len().saturating_sub(1));
    }

    pub(super) fn restore_pending_inputs(&mut self, session: &str, frame: &serde_json::Value) {
        if frame.get("pending_inputs_complete").is_none() {
            return;
        }
        let complete = frame["pending_inputs_complete"] == true;
        let mut restored = Vec::new();
        let mut unchanged = HashSet::new();
        let mut ids = HashSet::new();
        let valid = complete
            && frame["pending_inputs"].as_array().is_some_and(|rows| {
                if rows.len() > MAX_INPUT_REQUESTS {
                    return false;
                }
                for row in rows {
                    let Some(mut request) = InputRequest::from_event(session, row) else {
                        return false;
                    };
                    if !ids.insert(request.id.clone()) {
                        return false;
                    }
                    if let Some(old) = self.input_requests.iter().find(|old| {
                        old.session_id == session
                            && old.id == request.id
                            && old.original_payload == request.original_payload
                    }) {
                        unchanged.insert(request.id.clone());
                        request = old.clone();
                    }
                    restored.push(request);
                }
                true
            });
        self.input_requests
            .retain(|request| request.session_id != session);
        self.pending_answers
            .retain(|(owner, id, _)| owner != session || valid && unchanged.contains(id));
        if valid && self.input_requests.len() + restored.len() <= MAX_INPUT_REQUESTS {
            self.input_requests.extend(restored);
        } else {
            self.notice =
                "Pending input snapshot incomplete; reconnect or refresh session before answering"
                    .into();
        }
    }

    /// Apply one versioned daemon frame after transport decoding. Returns whether
    /// visible state changed. Unknown frames are ignored for forward compatibility.
    pub fn apply_daemon_frame(&mut self, frame: &serde_json::Value) -> bool {
        let before = self.prompt_owner();
        let changed = self.apply_daemon_frame_inner(frame);
        let owner_changed = before != self.prompt_owner();
        self.finish_prompt_owner_transition(before);
        if owner_changed {
            self.transcript_selection.borrow_mut().clear();
            self.sync_chooser_state();
        }
        changed || owner_changed
    }

    /// Apply an owned worker result without decoding synthetic JSON for controls
    /// and telemetry. Opaque daemon payloads retain the raw-frame boundary.
    pub fn apply_worker_frame(&mut self, frame: crate::worker_frames::WorkerFrame) -> bool {
        use crate::worker_frames::{CommandResult, WorkerFrame};
        let before = self.prompt_owner();
        let changed = match frame {
            WorkerFrame::Command {
                session_id,
                result:
                    result @ (CommandResult::SetModel { .. }
                    | CommandResult::SetEffort { .. }
                    | CommandResult::SetPermissionMode { .. }),
            } => session_controls::ControlReply::from_worker(&session_id, &result)
                .is_some_and(|reply| self.apply_control_result(reply)),
            WorkerFrame::Notice {
                session_id,
                message,
            } => {
                self.session_activity.remove(&session_id);
                self.apply_update(DaemonUpdate::Status {
                    id: session_id,
                    text: "Disconnected".into(),
                });
                self.notice = safe_label(&message);
                true
            }
            WorkerFrame::Telemetry { session_id, reply } => {
                if let Some(status) = reply.get("status") {
                    self.session_telemetry
                        .entry(session_id)
                        .or_default()
                        .update_status(status);
                    true
                } else {
                    false
                }
            }
            WorkerFrame::TelemetryUnavailable { session_id } => {
                self.session_telemetry.entry(session_id).or_default().lore = None;
                true
            }
            frame => self.apply_daemon_frame_inner(&frame.into_legacy_value()),
        };
        let owner_changed = before != self.prompt_owner();
        self.finish_prompt_owner_transition(before);
        if owner_changed {
            self.transcript_selection.borrow_mut().clear();
            self.sync_chooser_state();
        }
        changed || owner_changed
    }

    fn apply_daemon_frame_inner(&mut self, frame: &serde_json::Value) -> bool {
        let Some(kind) = frame.get("type").and_then(|v| v.as_str()) else {
            return false;
        };
        match kind {
            "branch_reply" => {
                let Some(id) = frame["session_id"].as_str() else {
                    return false;
                };
                if frame["ok"] == true && frame["message"].as_str().is_some() {
                    self.invalidate_repo(id);
                }
                if frame["ok"] != true {
                    self.notice = format!(
                        "branch: {}",
                        safe_label(frame["error"].as_str().unwrap_or("switch refused"))
                    );
                } else if let Some(message) = frame["message"].as_str() {
                    self.notice = format!("branch: {}", safe_label(message));
                } else {
                    let Some(base) = frame["base"].as_str() else {
                        return false;
                    };
                    if base.len() > 200 || base.chars().any(unsafe_input_char) {
                        return false;
                    }
                    let Some(rows) = frame["branches"].as_array() else {
                        return false;
                    };
                    if self.groups[self.active_group].active_id() != Some(id) {
                        return false;
                    }
                    let branches: Vec<String> = rows
                        .iter()
                        .filter_map(|row| row.as_str())
                        .filter(|name| {
                            !name.is_empty()
                                && name.len() <= 200
                                && !name.chars().any(unsafe_input_char)
                        })
                        .take(100)
                        .map(str::to_owned)
                        .collect();
                    if branches.is_empty() {
                        self.notice = "branch: no local base branches available".into();
                    } else {
                        let selected = branches.iter().position(|name| name == base).unwrap_or(0);
                        self.branch_picker = Some(BranchPicker {
                            session_id: id.into(),
                            branches,
                            base: base.into(),
                            selected,
                        });
                        if self.active_chooser_rect().is_none() {
                            self.branch_picker = None;
                            self.notice = "Enlarge active pane to choose a branch".into();
                        }
                    }
                }
                true
            }
            "queue_list_reply" => {
                let Some(id) = frame["session_id"].as_str() else {
                    return false;
                };
                let Some(picker) = self
                    .queue_picker
                    .as_mut()
                    .filter(|picker| picker.session_id == id)
                else {
                    return false;
                };
                picker.loading = false;
                if frame["ok"] != true {
                    self.notice = format!(
                        "Queue unavailable · {}",
                        safe_label(frame["error"].as_str().unwrap_or("daemon refused"))
                    );
                    return true;
                }
                let rows = frame["rows"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .take(32)
                    .filter_map(|row| {
                        let id = row["id"].as_str()?;
                        if id.is_empty()
                            || id.len() > 128
                            || !id
                                .bytes()
                                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
                        {
                            return None;
                        }
                        let preview = row["preview"].as_str().filter(|text| text.len() <= 1024)?;
                        Some(QueueRow {
                            id: id.to_owned(),
                            preview: safe_label(preview),
                        })
                    })
                    .collect();
                picker.rows = rows;
                picker.selected = picker.selected.min(picker.rows.len().saturating_sub(1));
                true
            }
            "queue_cancel_reply" => {
                let Some(id) = frame["session_id"].as_str() else {
                    return false;
                };
                let Some(picker) = self
                    .queue_picker
                    .as_mut()
                    .filter(|picker| picker.session_id == id)
                else {
                    return false;
                };
                let Some(expected) = picker.cancelling.as_deref() else {
                    return false;
                };
                if Some(expected) != frame["queue_id"].as_str() {
                    return false;
                }
                picker.cancelling = None;
                self.notice = if frame["ok"] == true {
                    "Queued prompt cancelled".into()
                } else {
                    format!(
                        "Queue cancellation failed · {}",
                        safe_label(frame["error"].as_str().unwrap_or("item already started"))
                    )
                };
                picker.loading = true;
                self.pending_queue_commands
                    .push(crate::bridge::WorkerCommand::QueueList(id.to_owned()));
                true
            }
            "attach_reply" => {
                let Some(reply_id) = frame["session_id"].as_str() else {
                    return false;
                };
                if !self.attaching_ids.remove(reply_id) {
                    return false;
                }
                if frame["ok"] == true {
                    if let Some(id) = frame["session_id"]
                        .as_str()
                        .filter(|id| crate::discovery::valid_id(id))
                    {
                        let target = frame["group"]
                            .as_u64()
                            .filter(|group| *group < self.groups.len() as u64)
                            .map(|group| group as usize)
                            .unwrap_or(self.active_group);
                        if target != 0 {
                            if let Some(index) =
                                self.groups[0].tabs.iter().position(|tab| tab == id)
                            {
                                self.groups[0].tabs.remove(index);
                                self.groups[0].active = self.groups[0]
                                    .active
                                    .min(self.groups[0].tabs.len().saturating_sub(1));
                            }
                        }
                        let group = &mut self.groups[target];
                        if !group.tabs.iter().any(|tab| tab == id) {
                            group.tabs.push(id.to_owned());
                        }
                        group.active = group
                            .tabs
                            .iter()
                            .position(|tab| tab == id)
                            .unwrap_or(group.active);
                        self.active_group = target;
                        self.notice = format!("Attached · {}", safe_label(id));
                    }
                } else {
                    self.notice = format!(
                        "Attach failed · {}",
                        safe_label(frame["message"].as_str().unwrap_or("unknown error"))
                    );
                }
                true
            }
            "launch_reply" => {
                if !self.launching {
                    return false;
                }
                self.launching = false;
                if let Some(clear) = self.clear_pending.take() {
                    if frame["ok"] == true {
                        if let Some(id) = frame["session_id"]
                            .as_str()
                            .filter(|id| crate::discovery::valid_id(id))
                        {
                            if let Some(position) = self.groups[clear.group]
                                .tabs
                                .iter()
                                .position(|tab| tab == &clear.old_id)
                            {
                                // A hello can arrive before this reply and provisionally
                                // insert the new session into the first pane.
                                for group in &mut self.groups {
                                    if let Some(provisional) =
                                        group.tabs.iter().position(|tab| tab == id)
                                    {
                                        group.tabs.remove(provisional);
                                        group.active =
                                            group.active.min(group.tabs.len().saturating_sub(1));
                                    }
                                }
                                let group = &mut self.groups[clear.group];
                                let position = group
                                    .tabs
                                    .iter()
                                    .position(|tab| tab == &clear.old_id)
                                    .unwrap_or(position);
                                group.tabs[position] = id.to_owned();
                                group.active = position;
                                group.scroll = 0;
                                self.active_group = clear.group;
                                for collection in &mut self.collections {
                                    if let Some(member) = collection
                                        .sessions
                                        .iter_mut()
                                        .find(|member| member.as_str() == clear.old_id)
                                    {
                                        *member = id.to_owned();
                                    }
                                }
                                self.clear_swap = Some(ClearSwap {
                                    old_id: clear.old_id.clone(),
                                    new_id: id.to_owned(),
                                    group: clear.group,
                                    position,
                                });
                                self.clear_stop_after_save.push(clear.old_id);
                                self.notice = format!("Fresh session ready · {}", safe_label(id));
                                return true;
                            }
                        }
                    } else {
                        self.notice = format!(
                            "Clear failed; previous session preserved · {}",
                            safe_label(frame["message"].as_str().unwrap_or("unknown error"))
                        );
                        return true;
                    }
                    self.notice = "Clear target changed; new session kept as a separate tab".into();
                }
                if frame["ok"] == true {
                    if let Some(id) = frame["session_id"]
                        .as_str()
                        .filter(|id| crate::discovery::valid_id(id))
                    {
                        self.offline_ids.remove(id);
                        let target = frame["group"]
                            .as_u64()
                            .filter(|group| *group < self.groups.len() as u64)
                            .map(|group| group as usize)
                            .unwrap_or(self.active_group);
                        // A newly attached daemon may send hello before this reply.
                        // Upsert places the first observed session in group zero;
                        // move that provisional tab to the requested pane.
                        if target != 0 {
                            let first = &mut self.groups[0];
                            if let Some(index) = first.tabs.iter().position(|tab| tab == id) {
                                first.tabs.remove(index);
                                first.active = first.active.min(first.tabs.len().saturating_sub(1));
                            }
                        }
                        let group = &mut self.groups[target];
                        if !group.tabs.iter().any(|tab| tab == id) {
                            group.tabs.push(id.to_owned());
                        }
                        group.active = group
                            .tabs
                            .iter()
                            .position(|tab| tab == id)
                            .unwrap_or(group.active);
                    }
                }
                self.notice = if frame["ok"] == true {
                    format!(
                        "Session started · {}",
                        safe_label(frame["session_id"].as_str().unwrap_or(""))
                    )
                } else if frame["started"] == true {
                    let id = frame["session_id"]
                        .as_str()
                        .filter(|id| crate::discovery::valid_id(id))
                        .unwrap_or("unknown");
                    format!("Session started; UI attach failed · doxa-rs attach {id}")
                } else {
                    format!(
                        "Session launch failed · {}",
                        safe_label(frame["message"].as_str().unwrap_or("unknown error"))
                    )
                };
                if frame["ok"] == true {
                    self.new_session = None;
                } else {
                    if let Some(form) = self.new_session.as_mut() {
                        form.launch_error = Some(self.notice.clone());
                        form.retry_allowed = frame["started"] != true;
                    }
                    if self.sessions.is_empty() {
                        self.awaiting_initial_attach = false;
                        self.startup_recovery = Some(self.notice.clone());
                    }
                }
                true
            }
            "hello" => {
                let Some(id) = frame.get("session_id").and_then(|v| v.as_str()) else {
                    return false;
                };
                self.restore_pending_inputs(id, frame);
                let model = frame
                    .get("model")
                    .and_then(|v| v.as_str())
                    .map(safe_label)
                    .filter(|s| !s.is_empty());
                let engine = frame
                    .get("engine")
                    .and_then(|v| v.as_str())
                    .map(safe_label)
                    .filter(|s| !s.is_empty());
                self.session_identity
                    .insert(id.to_owned(), (engine, model.clone()));
                if let Some(effort) = frame["effort"].as_str().filter(|effort| !effort.is_empty()) {
                    self.session_efforts
                        .insert(id.to_owned(), safe_label(effort));
                } else {
                    self.session_efforts.remove(id);
                }
                self.update_pending_effort(id, frame);
                self.session_telemetry
                    .entry(id.to_owned())
                    .or_default()
                    .update_status(frame);
                self.session_capabilities.insert(
                    id.to_owned(),
                    EngineCapabilities::from_session_controls(frame),
                );
                self.session_catalogs.remove(id);
                if let Some(mode) = frame["permission_mode"]
                    .as_str()
                    .filter(|mode| permission_index(mode).is_some())
                {
                    self.permission_modes.insert(id.to_owned(), mode.to_owned());
                }
                self.session_activity.insert(
                    id.to_owned(),
                    (
                        frame["running"] == true,
                        frame["queued"].as_u64().unwrap_or(0) as usize,
                    ),
                );
                let cwd = safe_label(frame.get("cwd").and_then(|v| v.as_str()).unwrap_or(""));
                if let Some(raw) = frame.get("cwd").and_then(|v| v.as_str()) {
                    let path = PathBuf::from(raw);
                    if path.is_absolute() && raw.len() <= 4096 {
                        if self.session_cwds.get(id) != Some(&path) {
                            self.memory_cache.remove(id);
                            self.memory_repo.remove(id);
                            self.invalidate_repo(id);
                        }
                        self.session_cwds.insert(id.to_owned(), path);
                    } else {
                        self.session_cwds.remove(id);
                        self.memory_cache.remove(id);
                        self.memory_repo.remove(id);
                        self.invalidate_repo(id);
                    }
                } else {
                    self.session_cwds.remove(id);
                    self.memory_cache.remove(id);
                    self.memory_repo.remove(id);
                    self.invalidate_repo(id);
                }
                let transcript = self
                    .sessions
                    .iter()
                    .find(|s| s.id == id)
                    .map(|s| s.transcript.clone())
                    .unwrap_or_default();
                self.awaiting_initial_attach = false;
                self.startup_recovery = None;
                self.apply_update(DaemonUpdate::Upsert(Session {
                    id: id.into(),
                    title: self
                        .custom_names
                        .get(id)
                        .cloned()
                        .unwrap_or_else(|| model.clone().unwrap_or_else(|| safe_label(id))),
                    collection: cwd,
                    transcript,
                    status: "Connected".into(),
                }));
                self.request_auto_diff(id);
                self.notice = format!(
                    "Connected · {}",
                    model.as_deref().unwrap_or(&safe_label(id))
                );
                true
            }
            "event" => {
                let Some(event) = frame.get("event") else {
                    return false;
                };
                let Some(event_type) = event.get("type").and_then(|v| v.as_str()) else {
                    return false;
                };
                let data = &event["data"];
                // The transport tags every socket frame before delivery. An
                // untagged frame must not alter the currently focused session.
                let Some(id) = frame
                    .get("session_id")
                    .and_then(|v| v.as_str())
                    .map(str::to_owned)
                else {
                    return false;
                };
                let mut tool_updated = false;
                if self.sessions.iter().any(|session| session.id == id) {
                    tool_updated = self.tool_cards.record(&id, event_type, data);
                    if tool_updated {
                        let revision = self.tool_cards_revision.entry(id.clone()).or_default();
                        *revision = revision.wrapping_add(1);
                    }
                    self.peer_map.event(&id, event_type, data);
                    if self.map_modal && matches!(event_type, "peer_message" | "peer_sent")
                        && self.groups[self.active_group].active_id() == Some(id.as_str()) {
                        self.pending_peer_refresh = Some(id.clone());
                    }
                }
                if event_type == "tool_result" {
                    self.request_auto_diff(&id);
                }
                match event_type {
                    "billing" => {
                        if !self.sessions.iter().any(|session| session.id == id) {
                            return false;
                        }
                        self.session_telemetry
                            .entry(id)
                            .or_default()
                            .update_billing(data);
                        true
                    }
                    "tool_result_detail" => tool_updated,
                    "derive_done" => {
                        let count = data["staged"].as_u64().unwrap_or(0);
                        if count > 0 {
                            let body = format!(
                                "{count} proposals staged · {}",
                                data["texts"]
                                    .as_array()
                                    .and_then(|r| r.first())
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("/pending to review")
                            );
                            if self
                                .preferences
                                .should_notify("notify_staged", self.window_focused)
                            {
                                crate::preferences::notify(
                                    self.sessions
                                        .iter()
                                        .find(|s| s.id == id)
                                        .map(|s| s.title.as_str())
                                        .unwrap_or("DOXA memory review"),
                                    &body,
                                );
                            }
                            self.notice = safe_label(&body);
                            return true;
                        }
                        false
                    }
                    "branch_changed" => {
                        self.invalidate_repo(&id);
                        true
                    }
                    "model_changed" => {
                        self.pending_effort_verifications.remove(&id);
                        self.pending_effort_changes
                            .retain(|(owner, _)| owner != &id);
                        if data.get("effort").is_some() {
                            if let Some(effort) = data["effort"].as_str() {
                                self.session_efforts.insert(id.clone(), safe_label(effort));
                            } else {
                                self.session_efforts.remove(&id);
                            }
                        }
                        if self
                            .effort_picker
                            .as_ref()
                            .is_some_and(|picker| picker.session_id == id)
                        {
                            self.effort_picker = None;
                        }
                        let old_model = self
                            .session_identity
                            .get(&id)
                            .and_then(|identity| identity.1.clone());
                        let new_model = data
                            .get("model")
                            .and_then(|v| v.as_str())
                            .map(safe_label)
                            .filter(|s| !s.is_empty());
                        if let Some(identity) = self.session_identity.get_mut(&id) {
                            identity.1 = new_model.clone();
                        }
                        if let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) {
                            if !self.custom_names.contains_key(&id)
                                && old_model.as_deref() == Some(session.title.as_str())
                            {
                                if let Some(model) = new_model {
                                    session.title = model;
                                }
                            }
                        }
                        true
                    }
                    "effort_changed" | "effort_verified" => {
                        if let Some(effort) =
                            data["effort"].as_str().filter(|effort| !effort.is_empty())
                        {
                            self.session_efforts.insert(id.clone(), safe_label(effort));
                            self.pending_effort_verifications.remove(&id);
                            if self.groups[self.active_group].active_id() == Some(id.as_str()) {
                                self.notice = format!("Effort verified · {}", safe_label(effort));
                            }
                        }
                        true
                    }
                    "effort_requested" => {
                        if let Some(effort) =
                            data["effort"].as_str().filter(|value| !value.is_empty())
                        {
                            self.pending_effort_verifications
                                .insert(id.clone(), safe_label(effort));
                            if self.groups[self.active_group].active_id() == Some(id.as_str()) {
                                self.notice = format!(
                                    "Requested effort {} · awaiting provider verification",
                                    safe_label(effort)
                                );
                            }
                        }
                        true
                    }
                    "effort_verification_failed" => {
                        self.pending_effort_verifications.remove(&id);
                        self.pending_effort_changes
                            .retain(|(owner, _)| owner != &id);
                        if self.groups[self.active_group].active_id() == Some(id.as_str()) {
                            self.notice =
                                "Effort verification failed · previous verified effort retained"
                                    .into();
                        }
                        true
                    }
                    "permission_mode_changed" => {
                        if let Some(mode) = data["mode"]
                            .as_str()
                            .filter(|mode| permission_index(mode).is_some())
                        {
                            self.permission_modes.insert(id, mode.to_owned());
                        }
                        true
                    }
                    "text_delta" => {
                        let Some(text) = data.get("text").and_then(|v| v.as_str()) else {
                            return false;
                        };
                        if text.is_empty() {
                            return false;
                        }
                        if let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) {
                            let mut clipped = false;
                            if data["snapshot"] != true && self.streaming_text.insert(id.clone()) {
                                clipped |= append_turn_heading(session, "Assistant");
                            }
                            clipped |= append_transcript(session, text);
                            self.transcript_evicted(&id, clipped);
                            true
                        } else {
                            false
                        }
                    }
                    "reasoning_progress" => self.append_reasoning(&id, data, true),
                    "reasoning_delta" => self.append_reasoning(&id, data, false),
                    "turn_started" => {
                        self.streaming_text.remove(&id);
                        self.reasoning_streams.remove(&id);
                        if let Some(prompt) = data
                            .get("prompt")
                            .and_then(|v| v.as_str())
                            .filter(|v| !v.is_empty())
                        {
                            if let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) {
                                let prompt = markdown::sanitize(prompt);
                                let mut clipped = append_turn_heading(session, "You");
                                clipped |= append_transcript(session, &prompt);
                                self.transcript_evicted(&id, clipped);
                            }
                        }
                        self.session_activity.entry(id.clone()).or_default().0 = true;
                        self.apply_update(DaemonUpdate::Status {
                            id,
                            text: "Running".into(),
                        });
                        true
                    }
                    "turn_done" => {
                        self.streaming_text.remove(&id);
                        if data["is_error"] == true
                            && self.pending_effort_verifications.remove(&id).is_some()
                            && self.groups[self.active_group].active_id() == Some(id.as_str())
                        {
                            self.notice =
                                "Effort verification failed · previous verified effort retained"
                                    .into();
                        }
                        let mut clipped = false;
                        if let Some(stream) = self.reasoning_streams.get_mut(&id) {
                            stream.streaming = false;
                            if let Some(tokens) = data["reasoning_output_tokens"].as_u64() {
                                stream.tokens = tokens;
                                stream.exact = true;
                            }
                            if let Some(session) =
                                self.sessions.iter_mut().find(|session| session.id == id)
                            {
                                clipped |= set_reasoning_marker(session, stream);
                            }
                        }
                        self.transcript_evicted(&id, clipped);
                        self.session_telemetry
                            .entry(id.clone())
                            .or_default()
                            .update_turn(data);
                        self.session_activity.entry(id.clone()).or_default().0 = false;
                        let status = if data.get("is_error").and_then(|v| v.as_bool()) == Some(true)
                        {
                            "Error"
                        } else {
                            "Ready"
                        };
                        self.apply_update(DaemonUpdate::Status {
                            id: id.clone(),
                            text: status.into(),
                        });
                        self.append_event(&id, event_type, data);
                        true
                    }
                    "needs_input" => {
                        if let Some(mut request) = InputRequest::from_event(&id, data) {
                            if let Some(position) = self
                                .input_requests
                                .iter()
                                .position(|old| old.session_id == id && old.id == request.id)
                            {
                                if self.input_requests[position].original_payload
                                    != request.original_payload
                                {
                                    self.pending_answers.retain(|(owner, request_id, _)| {
                                        owner != &id || request_id != &request.id
                                    });
                                    self.input_requests[position] = request.clone();
                                }
                            }
                            if !self
                                .input_requests
                                .iter()
                                .any(|r| r.session_id == id && r.id == request.id)
                            {
                                let auto_allow = request.kind == "permission"
                                    && request.original_payload["tool_name"].as_str().is_some_and(|tool| {
                                        self.permission_grants.contains(&(id.clone(), tool.to_owned()))
                                    });
                                if auto_allow && self.input_requests.len() < MAX_INPUT_REQUESTS {
                                    request.sending = true;
                                    self.pending_answers.push((id.clone(), request.id.clone(),
                                        serde_json::json!({"decision":"allow"})));
                                    self.input_requests.push(request);
                                } else {
                                    self.drag = None;
                                    self.tool_modal = false;
                                    self.model_picker = None;
                                    self.effort_picker = None;
                                    self.permission_picker = None;
                                    self.permission_confirm_dont_ask = false;
                                    self.engine_picker = false;
                                    self.stop_confirmation = None;
                                    if self.input_requests.len() < MAX_INPUT_REQUESTS {
                                        if self.input_requests.is_empty() {
                                            self.blink_on = true;
                                            self.blink_at = Instant::now();
                                        }
                                        if self
                                            .preferences
                                            .should_notify("notify_needs_input", self.window_focused)
                                        {
                                            crate::preferences::notify(
                                                "DOXA needs input",
                                                &format!(
                                                    "{} · {}",
                                                    self.sessions
                                                        .iter()
                                                        .find(|s| s.id == id)
                                                        .map(|s| s.title.as_str())
                                                        .unwrap_or(&id),
                                                    request.kind
                                                ),
                                            );
                                        }
                                        self.input_requests.push(request);
                                    } else {
                                        self.notice =
                                            "Too many input requests · inspect the session directly"
                                                .into();
                                    }
                                }
                            }
                        } else {
                            self.notice = "Invalid input request · inspect another client".into();
                        }
                        self.apply_update(DaemonUpdate::Status {
                            id: id.clone(),
                            text: "Needs input".into(),
                        });
                        self.append_event(&id, event_type, data);
                        true
                    }
                    "needs_input_resolved" => {
                        if let Some(request_id) = data.get("id").and_then(|v| v.as_str()) {
                            self.input_requests
                                .retain(|r| !(r.session_id == id && r.id == request_id));
                            self.pending_answers.retain(|(session, request, _)| {
                                session != &id || request != request_id
                            });
                        }
                        self.apply_update(DaemonUpdate::Status {
                            id: id.clone(),
                            text: "Running".into(),
                        });
                        self.append_event(&id, event_type, data)
                    }
                    "session_done" => {
                        self.permission_grants.retain(|(session, _)| session != &id);
                        self.input_requests
                            .retain(|request| request.session_id != id);
                        self.pending_answers
                            .retain(|(session, _, _)| session != &id);
                        self.streaming_text.remove(&id);
                        self.session_activity.remove(&id);
                        let before = self.diff_reject_queue.len();
                        self.diff_reject_queue.retain(|item| item.session_id != id);
                        if self.diff_reject_queue.len() != before {
                            self.notice =
                                "Queued hunk rejections cancelled because the session ended".into();
                        }
                        self.apply_update(DaemonUpdate::Status {
                            id: id.clone(),
                            text: "Ended".into(),
                        });
                        self.append_event(&id, event_type, data)
                    }
                    "turn_refused" => {
                        self.apply_update(DaemonUpdate::Status {
                            id: id.clone(),
                            text: "Ready".into(),
                        });
                        self.append_event(&id, event_type, data)
                    }
                    "prompt_queued" => {
                        self.session_activity.entry(id.clone()).or_default().1 += 1;
                        self.append_event(&id, event_type, data)
                    }
                    "prompt_dequeued" | "prompt_cancelled" | "prompt_discarded" => {
                        let activity = self.session_activity.entry(id.clone()).or_default();
                        activity.1 = activity.1.saturating_sub(1);
                        self.append_event(&id, event_type, data)
                    }
                    _ => self.append_event(&id, event_type, data),
                }
            }
            "peer_roster" => {
                let Some(id) = frame.get("session_id").and_then(|v| v.as_str()) else {
                    return false;
                };
                self.peer_map.roster(id, frame)
            }
            "peer_history" => {
                let Some(id) = frame.get("session_id").and_then(|v| v.as_str()) else {
                    return false;
                };
                self.peer_map.history(id, frame)
            }
            "peer_message_reply" => {
                let Some(session) = frame["session_id"]
                    .as_str()
                    .filter(|id| self.sessions.iter().any(|entry| entry.id == *id))
                else {
                    return false;
                };
                let delivered = frame["delivered_to"]
                    .as_array()
                    .is_some_and(|ids| !ids.is_empty())
                    || (frame["delivered_to"].is_null() && frame["peer"].is_object());
                if !delivered {
                    if let Some(draft) = frame["draft"].as_str() {
                        self.rejected_drafts
                            .entry(session.to_owned())
                            .or_default()
                            .push(draft.to_owned());
                    }
                }
                self.notice = if frame["uncertain"] == true {
                    "Peer delivery unconfirmed · inspect peer before Alt+Up retry".into()
                } else if frame["ok"] != true {
                    format!(
                        "Peer message failed · {}",
                        safe_label(frame["error"].as_str().unwrap_or("unknown error"))
                    )
                } else if frame["ledger_error"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty())
                {
                    "Peer message delivered · delivery ledger write failed".into()
                } else if delivered {
                    let title = frame["peer"]["title"]
                        .as_str()
                        .map(safe_label)
                        .unwrap_or_else(|| "peer".into());
                    format!("Peer message sent to {title}")
                } else {
                    "Peer message was not delivered · inspect the peer before retrying".into()
                };
                if self.groups[self.active_group].active_id() != Some(session) {
                    self.notice = format!("{} · {}", safe_label(session), self.notice);
                }
                true
            }
            "context_detail" => {
                let Some(id) = frame["session_id"].as_str() else {
                    return false;
                };
                if self.groups[self.active_group].active_id() != Some(id) {
                    return false;
                }
                let Some(info) = self.chip_info.as_mut().filter(|info| {
                    info.kind == "context" && info.owner.as_ref().is_some_and(|owner| owner.0 == id)
                }) else {
                    return false;
                };
                info.lines
                    .retain(|line| line != "Loading reported context details…");
                if frame["ok"] != true {
                    info.lines
                        .push("Detailed context metadata not reported by this engine".into());
                    return true;
                }
                info.lines.extend(context_detail_lines(&frame["detail"]));
                if self.size.width >= 65 {
                    info.lines.extend(crate::preferences::context_grid(
                        &frame["detail"],
                        self.preferences.value("context_grid") == "ascii",
                    ));
                }
                true
            }
            "models_reply" => {
                let Some(id) = frame.get("session_id").and_then(|v| v.as_str()) else {
                    return false;
                };
                if !self.sessions.iter().any(|session| session.id == id) {
                    return false;
                }
                if let Some(engine) = self
                    .session_identity
                    .get(id)
                    .and_then(|identity| identity.0.as_ref())
                {
                    self.session_catalogs.insert(
                        id.to_owned(),
                        session_controls::SessionCatalog::decode(engine, frame),
                    );
                }
                if self
                    .effort_catalog_pending
                    .as_ref()
                    .is_some_and(|pending| pending.0 == id)
                    && frame["loading"] != true
                {
                    let (owner, engine, model) = self.effort_catalog_pending.take().unwrap();
                    if self.groups[self.active_group].active_id() == Some(owner.as_str())
                        && self.session_identity.get(&owner).is_some_and(|identity| {
                            identity.0.as_deref() == Some(engine.as_str())
                                && identity.1.as_deref() == Some(model.as_str())
                        })
                    {
                        self.model_picker = None;
                        if frame["ok"] == true
                            && !self
                                .session_effort_levels(&owner, &engine, &model)
                                .is_empty()
                        {
                            self.open_effort_picker();
                            self.apply_requested_argument();
                        } else {
                            self.notice =
                                "Live effort capability unavailable from this engine".into();
                        }
                        return true;
                    }
                }
                if let Some(picker) = self
                    .model_picker
                    .as_mut()
                    .filter(|picker| picker.session_id == id)
                {
                    let previous = picker.models.get(picker.selected).cloned().or_else(|| {
                        self.session_identity
                            .get(id)
                            .and_then(|identity| identity.1.clone())
                    });
                    picker.loading = false;
                    picker.catalog_pending = frame["loading"] == true;
                    self.model_refresh_after = Some(
                        Instant::now()
                            + if picker.catalog_pending {
                                Duration::from_millis(500)
                            } else if frame["ok"] != true {
                                Duration::from_secs(5)
                            } else {
                                Duration::from_secs(30)
                            },
                    );
                    picker.models = if frame["ok"] == true {
                        frame["models"]
                            .as_array()
                            .into_iter()
                            .flatten()
                            .filter_map(|value| value.as_str())
                            .filter(|model| {
                                !model.is_empty()
                                    && model.len() <= 128
                                    && !model.chars().any(char::is_control)
                            })
                            .take(100)
                            .map(safe_label)
                            .collect()
                    } else {
                        Vec::new()
                    };
                    picker.note = if frame["ok"] == true {
                        frame["note"].as_str().map(safe_label).unwrap_or_default()
                    } else {
                        format!(
                            "Catalog unavailable: {}",
                            safe_label(frame["error"].as_str().unwrap_or("unknown error"))
                        )
                    };
                    picker.selected = previous
                        .and_then(|model| picker.models.iter().position(|item| item == &model))
                        .unwrap_or(0);
                    self.apply_requested_argument();
                    return true;
                }
                false
            }
            "set_model_reply" | "set_effort_reply" | "set_permission_mode_reply" => {
                self.apply_control_reply(frame)
            }
            "stop_reply" => {
                let Some(id) = frame["session_id"]
                    .as_str()
                    .filter(|id| self.sessions.iter().any(|s| s.id == *id))
                else {
                    return false;
                };
                if frame["ok"] == true {
                    self.offline_ids.insert(id.to_owned());
                    self.input_requests
                        .retain(|request| request.session_id != id);
                    self.apply_update(DaemonUpdate::Status {
                        id: id.to_owned(),
                        text: "Stopping".into(),
                    });
                    self.notice = format!("Stop accepted · {}", safe_label(id));
                } else {
                    self.notice = format!(
                        "Stop failed · {}",
                        safe_label(frame["error"].as_str().unwrap_or("unknown error"))
                    );
                }
                true
            }
            "clear_finalize_reply" => {
                let Some(id) = frame["session_id"].as_str() else {
                    return false;
                };
                if frame["ok"] == true {
                    let selected_id = self
                        .rail_order()
                        .get(self.rail_selected)
                        .map(|index| self.sessions[*index].id.clone());
                    self.sessions.retain(|session| session.id != id);
                    self.session_activity.remove(id);
                    self.session_identity.remove(id);
                    self.session_cwds.remove(id);
                    self.session_efforts.remove(id);
                    self.pending_effort_verifications.remove(id);
                    self.pending_effort_changes.retain(|(owner, _)| owner != id);
                    self.next_efforts.remove(id);
                    self.session_telemetry.remove(id);
                    self.memory_cache.remove(id);
                    self.memory_repo.remove(id);
                    self.repo_cache.remove(id);
                    self.repo_epoch.remove(id);
                    self.session_capabilities.remove(id);
                    self.session_catalogs.remove(id);
                    self.permission_modes.remove(id);
                    self.streaming_text.remove(id);
                    self.reasoning_streams.remove(id);
                    self.custom_names.remove(id);
                    self.input_drafts.retain(|(_, session), _| session != id);
                    self.rejected_drafts.remove(id);
                    self.expanded_tool_sections.remove(id);
                    self.selected_tool_sections.remove(id);
                    if self.tool_section_hover.as_ref().is_some_and(|(session, _)| session == id) { self.tool_section_hover = None; }
                    self.tool_cards_revision.remove(id);
                    self.input_requests
                        .retain(|request| request.session_id != id);
                    self.pending_answers.retain(|(session, _, _)| session != id);
                    self.rail_selected = selected_id
                        .and_then(|selected| {
                            self.rail_order()
                                .iter()
                                .position(|index| self.sessions[*index].id == selected)
                        })
                        .unwrap_or_else(|| {
                            self.rail_selected
                                .min(self.rail_order().len().saturating_sub(1))
                        });
                    self.notice = "Previous session finalized".into();
                } else {
                    self.notice = format!(
                        "Previous session remains live · {}",
                        safe_label(frame["error"].as_str().unwrap_or("finalization refused"))
                    );
                }
                true
            }
            "telemetry_unavailable" => {
                if let Some(id) = frame["session_id"].as_str() {
                    self.session_telemetry
                        .entry(id.to_owned())
                        .or_default()
                        .lore = None;
                    return true;
                }
                false
            }
            "telemetry_status" => {
                let Some(id) = frame["session_id"].as_str() else {
                    return false;
                };
                let Some(status) = frame.get("status") else {
                    return false;
                };
                self.session_telemetry
                    .entry(id.to_owned())
                    .or_default()
                    .update_status(status);
                true
            }
            "reply" => {
                // Both native and Python daemons broadcast prompt_queued after
                // the enqueue reply. Count that event once, not this reply.
                if let Some(status) = frame.get("status") {
                    let id = status
                        .get("session_id")
                        .and_then(|v| v.as_str())
                        .or_else(|| frame.get("session_id").and_then(|v| v.as_str()));
                    if let Some(id) = id {
                        self.session_telemetry
                            .entry(id.to_owned())
                            .or_default()
                            .update_status(status);
                        let identity = self.session_identity.entry(id.to_owned()).or_default();
                        if status.get("engine").is_some() {
                            identity.0 = status
                                .get("engine")
                                .and_then(|v| v.as_str())
                                .map(safe_label)
                                .filter(|s| !s.is_empty());
                        }
                        if status.get("model").is_some() {
                            identity.1 = status
                                .get("model")
                                .and_then(|v| v.as_str())
                                .map(safe_label)
                                .filter(|s| !s.is_empty());
                        }
                        if status.get("effort").is_some() {
                            if let Some(effort) = status["effort"]
                                .as_str()
                                .filter(|effort| !effort.is_empty())
                            {
                                self.session_efforts
                                    .insert(id.to_owned(), safe_label(effort));
                            } else {
                                self.session_efforts.remove(id);
                            }
                        }
                        self.update_pending_effort(id, status);
                        self.session_capabilities
                            .entry(id.to_owned())
                            .or_default()
                            .update_session_controls(status);
                        if let Some(mode) = status["permission_mode"]
                            .as_str()
                            .filter(|mode| permission_index(mode).is_some())
                        {
                            self.permission_modes.insert(id.to_owned(), mode.to_owned());
                        }
                        if let (Some(running), Some(queued)) =
                            (status["running"].as_bool(), status["queued"].as_u64())
                        {
                            self.session_activity
                                .insert(id.to_owned(), (running, queued as usize));
                        }
                    }
                }
                let ok = frame.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
                self.notice = if ok {
                    "Request accepted".into()
                } else {
                    format!(
                        "Request failed: {}",
                        safe_label(
                            frame
                                .get("error")
                                .and_then(|v| v.as_str())
                                .unwrap_or("unknown error")
                        )
                    )
                };
                true
            }
            "client_notice" => {
                if let Some(id) = frame.get("session_id").and_then(|v| v.as_str()) {
                    self.session_activity.remove(id);
                    self.apply_update(DaemonUpdate::Status {
                        id: id.into(),
                        text: "Disconnected".into(),
                    });
                }
                self.notice = safe_label(
                    frame
                        .get("message")
                        .and_then(|v| v.as_str())
                        .unwrap_or("Daemon connection unavailable"),
                );
                true
            }
            "answer_reply" => {
                let id = frame
                    .get("request_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let session = frame
                    .get("session_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let ok = frame.get("ok").and_then(|v| v.as_bool()) == Some(true);
                let uncertain = frame.get("uncertain").and_then(|v| v.as_bool()) == Some(true);
                let mut granted_tool = None;
                if let Some(request) = self
                    .input_requests
                    .iter_mut()
                    .find(|r| r.session_id == session && r.id == id)
                {
                    if ok && request.grant_on_success && request.kind == "permission" {
                        granted_tool = request.original_payload["tool_name"].as_str().map(str::to_owned);
                        request.grant_on_success = false;
                    }
                    // An AskUser answer stays on its final question until the
                    // daemon confirms delivery. A refused or unconfirmed send
                    // must leave that selection available for a manual retry.
                    if !ok && (!uncertain || request.kind == "ask_user") {
                        request.sending = false;
                    }
                }
                if let Some(tool) = granted_tool {
                    self.permission_grants.insert((session.to_owned(), tool));
                }
                self.notice = if ok {
                    "Answer sent · awaiting resolution".into()
                } else if uncertain {
                    "Answer delivery unconfirmed · check session before retry".into()
                } else {
                    format!(
                        "Answer failed: {}",
                        safe_label(
                            frame
                                .get("message")
                                .and_then(|v| v.as_str())
                                .unwrap_or("request no longer pending")
                        )
                    )
                };
                true
            }
            "prompt_rejected" => {
                let Some(text) = frame.get("text").and_then(|v| v.as_str()) else {
                    return false;
                };
                if text.len() > MAX_INPUT_BYTES
                    || text.chars().any(|ch| unsafe_input_char(ch) && ch != '\n')
                {
                    self.notice =
                        "Rejected prompt exceeds input limits · inspect the originating client"
                            .into();
                    return true;
                }
                let Some(target) = frame.get("session_id").and_then(|v| v.as_str()) else {
                    return false;
                };
                let active = self.groups[self.active_group].active_id().unwrap_or("");
                if self.input.is_empty() && target == active {
                    self.input = text.to_owned();
                    self.input_cursor = self.input.len();
                } else {
                    self.rejected_drafts
                        .entry(target.into())
                        .or_default()
                        .push(text.to_owned());
                }
                self.notice = format!(
                    "{} · draft retained{}",
                    safe_label(
                        frame
                            .get("message")
                            .and_then(|v| v.as_str())
                            .unwrap_or("Prompt refused")
                    ),
                    if self.rejected_drafts.get(target).is_none_or(Vec::is_empty) {
                        ""
                    } else {
                        " (Alt+Up to restore)"
                    }
                );
                true
            }
            "prompt_uncertain" => {
                let Some(text) = frame.get("text").and_then(|v| v.as_str()) else {
                    return false;
                };
                if text.len() > MAX_INPUT_BYTES
                    || text.chars().any(|ch| unsafe_input_char(ch) && ch != '\n')
                {
                    self.notice =
                        "Rejected prompt exceeds input limits · inspect the originating client"
                            .into();
                    return true;
                }
                let Some(target) = frame.get("session_id").and_then(|v| v.as_str()) else {
                    return false;
                };
                self.rejected_drafts
                    .entry(target.into())
                    .or_default()
                    .push(text.to_owned());
                self.notice = format!(
                    "{} · check session before Alt+Up retry",
                    safe_label(
                        frame
                            .get("message")
                            .and_then(|v| v.as_str())
                            .unwrap_or("Prompt delivery unconfirmed")
                    )
                );
                true
            }
            _ => false,
        }
    }

    fn apply_control_reply(&mut self, frame: &serde_json::Value) -> bool {
        let Some(reply) = session_controls::ControlReply::decode(frame) else {
            return false;
        };
        self.apply_control_result(reply)
    }

    fn apply_control_result(&mut self, reply: session_controls::ControlReply<'_>) -> bool {
        use session_controls::EffortTransition;
        let id = reply.owner.id();
        if !self.sessions.iter().any(|session| session.id == id) {
            return false;
        }
        let Some(transition) = reply.reduce(
            self.pending_effort_verifications
                .get(id)
                .map(String::as_str),
        ) else {
            return false;
        };
        let state_changed = match transition.effort {
            EffortTransition::Unchanged => false,
            EffortTransition::Failed => self.pending_effort_verifications.remove(id).is_some(),
            EffortTransition::Verified(effort) => {
                self.pending_effort_verifications.remove(id);
                self.session_efforts.insert(id.to_owned(), effort);
                true
            }
        };
        if reply
            .owner
            .is_active(self.groups[self.active_group].active_id())
        {
            self.notice = transition.notice;
            true
        } else {
            state_changed
        }
    }

    fn update_pending_effort(&mut self, id: &str, status: &serde_json::Value) {
        if let Some(value) = status.get("pending_effort") {
            if let Some(effort) = value.as_str().filter(|value| !value.is_empty()) {
                self.pending_effort_verifications
                    .insert(id.to_owned(), safe_label(effort));
            } else if value.is_null() {
                self.pending_effort_verifications.remove(id);
            }
        }
    }

    pub(super) fn append_event(
        &mut self,
        id: &str,
        event_type: &str,
        data: &serde_json::Value,
    ) -> bool {
        let Some(row) = structured_event(event_type, data) else {
            return false;
        };
        let Some(session) = self.sessions.iter_mut().find(|s| s.id == id) else {
            return false;
        };
        let heading_needed = matches!(event_type, "tool_call" | "tool_result")
            && self.streaming_text.insert(id.to_owned());
        let mut clipped = false;
        if heading_needed {
            clipped |= append_turn_heading(session, "Assistant");
        }
        clipped |= append_transcript(session, &row);
        self.transcript_evicted(id, clipped);
        true
    }

    pub(super) fn append_reasoning(
        &mut self,
        id: &str,
        data: &serde_json::Value,
        progress: bool,
    ) -> bool {
        if !self.preferences.on("show_reasoning")
            || !self.sessions.iter().any(|session| session.id == id)
        {
            return false;
        }
        let stream = self.reasoning_streams.entry(id.to_owned()).or_default();
        if let Some(tokens) = data.get("approx_tokens").and_then(|value| value.as_u64()) {
            stream.tokens = stream.tokens.max(tokens);
        }
        if progress {
            stream.streaming = true;
        } else {
            let Some(text) = data.get("text").and_then(|value| value.as_str()) else {
                return false;
            };
            let clean = markdown::sanitize(text);
            let remaining = MAX_REASONING_DISPLAY_CHARS.saturating_sub(stream.text.chars().count());
            stream.text.extend(clean.chars().take(remaining));
            if clean.chars().count() > remaining
                && !stream.text.ends_with("[Reasoning display limit reached]")
            {
                stream.text.push_str("\n[Reasoning display limit reached]");
            }
            stream.tokens = stream
                .tokens
                .max(stream.text.chars().count().div_ceil(4) as u64);
            stream.streaming = data.get("final").and_then(|value| value.as_bool()) == Some(false);
        }
        let first = !stream.visible;
        let heading_needed = first && self.streaming_text.insert(id.to_owned());
        let Some(session) = self.sessions.iter_mut().find(|session| session.id == id) else {
            return false;
        };
        let mut clipped = false;
        if heading_needed {
            clipped |= append_turn_heading(session, "Assistant");
        }
        clipped |= set_reasoning_marker(session, stream);
        stream.visible = true;
        self.transcript_evicted(id, clipped);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::App;
    use crate::ui::{DaemonUpdate, Session};
    use crate::worker_frames::{CommandResult, ReplyStatus, WorkerFrame};
    use serde_json::json;

    fn control_app() -> App {
        let mut app = App::default();
        for id in ["active", "inactive"] {
            app.apply_update(DaemonUpdate::Upsert(Session {
                id: id.into(),
                title: id.into(),
                collection: String::new(),
                transcript: String::new(),
                status: "Ready".into(),
            }));
            app.pending_effort_verifications
                .insert(id.into(), "high".into());
            app.session_efforts.insert(id.into(), "low".into());
        }
        app.notice = "unchanged".into();
        app
    }

    fn effort(owner: &str, value: &str, verified: Option<bool>) -> WorkerFrame {
        WorkerFrame::Command {
            session_id: owner.into(),
            result: CommandResult::SetEffort {
                status: ReplyStatus {
                    ok: true,
                    error: None,
                },
                effort: Some(value.into()),
                verification_pending: verified,
            },
        }
    }

    #[test]
    fn typed_controls_reject_invalid_unknown_and_superseded_owners() {
        let mut app = control_app();
        for owner in ["../active", "removed"] {
            assert!(!app.apply_worker_frame(effort(owner, "high", Some(false))));
        }
        assert!(!app.apply_worker_frame(effort("active", "medium", Some(false))));
        assert_eq!(app.notice, "unchanged");
        assert_eq!(app.session_efforts["active"], "low");
        assert_eq!(app.pending_effort_verifications["active"], "high");
    }

    #[test]
    fn typed_controls_require_verification_and_keep_inactive_notice_owned() {
        let mut app = control_app();
        assert!(app.apply_worker_frame(effort("active", "high", None)));
        assert_eq!(app.session_efforts["active"], "low");
        assert_eq!(app.pending_effort_verifications["active"], "high");
        app.notice = "active notice".into();
        assert!(app.apply_worker_frame(effort("inactive", "high", Some(false))));
        assert_eq!(app.notice, "active notice");
        assert_eq!(app.session_efforts["inactive"], "high");
        assert!(!app.pending_effort_verifications.contains_key("inactive"));
    }

    #[test]
    fn typed_control_and_telemetry_reduction_match_raw_compatibility_path() {
        let mut typed = control_app();
        let mut raw = control_app();
        let outcome = typed.apply_worker_frame(effort("active", "high", Some(false)));
        assert_eq!(
            outcome,
            raw.apply_daemon_frame(&effort("active", "high", Some(false)).into_legacy_value())
        );
        let reply = json!({"status":{"ctx_tokens":42,"ctx_max_tokens":100,"belief_count":3}});
        assert_eq!(
            typed.apply_worker_frame(WorkerFrame::Telemetry {
                session_id: "active".into(),
                reply: reply.clone()
            }),
            raw.apply_daemon_frame(
                &WorkerFrame::Telemetry {
                    session_id: "active".into(),
                    reply
                }
                .into_legacy_value()
            )
        );
        assert_eq!(typed.notice, raw.notice);
        assert_eq!(typed.session_efforts, raw.session_efforts);
        assert_eq!(
            typed.pending_effort_verifications,
            raw.pending_effort_verifications
        );
        assert_eq!(
            typed.session_telemetry["active"].context,
            raw.session_telemetry["active"].context
        );
        assert_eq!(
            typed.session_telemetry["active"].lore,
            raw.session_telemetry["active"].lore
        );
        assert_eq!(typed.groups, raw.groups);
    }

    fn saturated_folds() -> App {
        use crate::ui::transcript_tools::FoldKey;
        use std::collections::HashSet;
        let mut app = App::default();
        app.sessions.push(Session { id: "s".into(), title: "S".into(), collection: "repo".into(),
            transcript: "x".repeat(crate::ui::MAX_TRANSCRIPT_BYTES), status: "Ready".into() });
        for id in ["s", "other"] {
            app.expanded_tool_sections.insert(id.into(), HashSet::from([
                FoldKey::Section(0), FoldKey::Tool("legacy:Section(0):0".into()), FoldKey::Tool("provider-call".into())]));
            app.selected_tool_sections.insert(id.into(), FoldKey::Section(0));
        }
        app.tool_section_hover = Some(("s".into(), FoldKey::Tool("legacy:Section(0):0".into())));
        app
    }

    fn assert_evicted_folds(app: &App) {
        use crate::ui::transcript_tools::FoldKey;
        assert_eq!(app.expanded_tool_sections["s"], std::collections::HashSet::from([FoldKey::Tool("provider-call".into())]));
        assert!(!app.selected_tool_sections.contains_key("s"));
        assert!(app.tool_section_hover.is_none());
        assert_eq!(app.expanded_tool_sections["other"].len(), 3);
        assert_eq!(app.selected_tool_sections["other"], FoldKey::Section(0));
    }

    #[test]
    fn transcript_eviction_clears_only_owner_positional_folds_for_all_append_routes() {
        for (kind, data) in [
            ("text_delta", json!({"text":"é"})),
            ("turn_started", json!({"prompt":"next prompt"})),
            ("tool_call", json!({"id":"new-call", "name":"Read", "input":{"file":"a.rs"}})),
        ] {
            let mut app = saturated_folds();
            assert!(app.apply_daemon_frame(&json!({"type":"event", "session_id":"s", "event":{"type":kind,"data":data}})));
            assert_evicted_folds(&app);
            assert!(app.sessions[0].transcript.len() <= crate::ui::MAX_TRANSCRIPT_BYTES);
        }
    }

    #[test]
    fn reasoning_replacement_eviction_clears_positional_folds_at_turn_done() {
        let mut app = saturated_folds();
        let prefix = crate::ui::transcript_tools::REASONING_PREFIX;
        let marker = format!("{prefix}{{}}\n\n");
        app.sessions[0].transcript = format!("{}{marker}", "x".repeat(crate::ui::MAX_TRANSCRIPT_BYTES - marker.len()));
        app.reasoning_streams.insert("s".into(), super::super::transcript_events::ReasoningStream {
            text: "expanded reasoning".repeat(20), visible: true, ..Default::default()
        });
        assert!(app.apply_daemon_frame(&json!({"type":"event", "session_id":"s", "event":{"type":"turn_done", "data":{}}})));
        assert_evicted_folds(&app);
    }

    #[test]
    fn changed_snapshot_clears_positional_folds_but_exact_snapshot_keeps_them() {
        let mut app = saturated_folds();
        let exact = app.sessions[0].transcript.clone();
        app.apply_update(DaemonUpdate::Transcript { id:"s".into(), markdown:exact });
        assert!(app.selected_tool_sections.contains_key("s"));
        app.apply_update(DaemonUpdate::Transcript { id:"s".into(), markdown:"new snapshot".into() });
        assert_evicted_folds(&app);
    }


    #[test]
    fn eviction_retains_stable_selection_and_other_owner_hover() {
        use crate::ui::transcript_tools::FoldKey;
        let mut app = saturated_folds();
        app.selected_tool_sections.insert("s".into(), FoldKey::Tool("provider-call".into()));
        app.tool_section_hover = Some(("other".into(), FoldKey::Section(0)));
        app.transcript_evicted("s", true);
        assert_eq!(app.selected_tool_sections["s"], FoldKey::Tool("provider-call".into()));
        assert_eq!(app.tool_section_hover, Some(("other".into(), FoldKey::Section(0))));
        let mut app = saturated_folds();
        app.tool_section_hover = Some(("s".into(), FoldKey::Tool("provider-call".into())));
        app.apply_update(DaemonUpdate::Upsert(Session { id:"s".into(), title:"S".into(), collection:"repo".into(), transcript:"changed".into(), status:"Ready".into() }));
        assert_eq!(app.tool_section_hover, Some(("s".into(), FoldKey::Tool("provider-call".into()))));
        assert!(!app.selected_tool_sections.contains_key("s"));
    }

}
