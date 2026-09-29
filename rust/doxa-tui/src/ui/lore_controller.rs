//! Coordinate exact LORE review menus, filtering and scoped memory workers.
use super::{
    raw_visual_rows, safe_label, unsafe_input_char, App, ChipInfo, LorePicker, REVIEW_BODY_RESERVE,
};
use crate::lore_picker;
use crossterm::event::KeyCode;
use crossterm::event::KeyEvent;
use crossterm::event::KeyModifiers;
use std::path::Path;
use std::sync::mpsc;
use std::sync::mpsc::TryRecvError;
use std::time::Duration;
use std::time::Instant;

impl App {
    pub(super) fn open_belief_graph(&mut self) {
        if self.belief_browser_fixture {
            if let Some(picker) = &mut self.lore_picker {
                picker.status = "Gallery fixture · LORE calls disabled".into();
            }
            return;
        }
        let Some(picker) = &self.lore_picker else {
            return;
        };
        if picker.proposal_mode || picker.pending.is_some() || self.belief_graph_pending.is_some() {
            return;
        }
        let Some(id) = picker.rows.get(picker.selected).map(|row| row.id) else {
            return;
        };
        if self
            .belief_graph_lines
            .as_ref()
            .is_some_and(|(selected, _)| *selected == id)
        {
            self.belief_graph_lines = None;
            return;
        }
        let cwd = picker.cwd.clone();
        let browser = self.preferences.value("graph_view") != "ascii";
        let (tx, rx) = mpsc::sync_channel(1);
        self.belief_graph_pending = Some((id, cwd.clone(), browser, rx));
        self.lore_picker.as_mut().unwrap().status = "Loading LORE belief neighbourhood…".into();
        std::thread::spawn(move || {
            let result = doxa_lore::LoreClient::open(Duration::from_secs(3))
                .and_then(|mut client| client.belief_graph(&cwd, id, browser));
            let _ = tx.send(result);
        });
    }
    pub(super) fn poll_belief_graph(&mut self) -> bool {
        let Some((id, cwd, browser, rx)) = self.belief_graph_pending.take() else {
            return false;
        };
        let result = match rx.try_recv() {
            Ok(result) => result,
            Err(TryRecvError::Empty) => {
                self.belief_graph_pending = Some((id, cwd, browser, rx));
                return false;
            }
            Err(TryRecvError::Disconnected) => {
                Err(doxa_lore::LoreError::Remote("graph_unavailable"))
            }
        };
        let Some(picker) = self.lore_picker.as_mut().filter(|p| {
            !p.proposal_mode && p.cwd == cwd && p.rows.get(p.selected).is_some_and(|r| r.id == id)
        }) else {
            return false;
        };
        match result {
            Ok(graph) if graph.id == id => {
                if browser {
                    if let Some(html) = graph.html {
                        let result = crate::operations::doxa_home()
                            .and_then(|home| crate::belief_graph::write_page(&home, id, &html))
                            .and_then(|path| {
                                let directory = path.parent().unwrap().to_path_buf();
                                if !self
                                    .belief_graph_server
                                    .as_ref()
                                    .is_some_and(|s| s.matches(&directory))
                                {
                                    self.belief_graph_server =
                                        Some(crate::belief_graph::GraphServer::start(directory)?);
                                }
                                Ok((path, self.belief_graph_server.as_ref().unwrap().url(id)))
                            });
                        match result {
                            Ok((path, url)) => {
                                picker.status = format!(
                                    "{} · {}",
                                    safe_label(&graph.note),
                                    safe_label(&path.display().to_string())
                                );
                                self.pending_open_urls.push(url);
                            }
                            Err(error) => {
                                picker.status = format!(
                                    "Graph page unavailable: {}",
                                    safe_label(&error.to_string())
                                )
                            }
                        }
                    } else {
                        picker.status = safe_label(&graph.note);
                        self.belief_graph_lines = Some((id, graph.lines));
                        self.belief_graph_scroll = 0;
                    }
                } else {
                    picker.status = safe_label(&graph.note);
                    self.belief_graph_lines = Some((id, graph.lines));
                    self.belief_graph_scroll = 0;
                }
            }
            _ => picker.status = "Belief graph unavailable from this LORE version or scope".into(),
        }
        true
    }

    pub(super) fn open_lore_picker(&mut self) {
        self.open_lore_picker_mode(false);
    }

    pub(super) fn open_pending_picker(&mut self) {
        self.open_lore_picker_mode(true);
    }

    pub(super) fn open_lore_picker_mode(&mut self, proposal_mode: bool) {
        self.belief_browser_fixture = false;
        self.belief_filter_due = None;
        self.belief_filter_request = None;
        self.belief_fixture_rows.clear();
        let cwd = self.groups[self.active_group]
            .active_id()
            .and_then(|id| self.session_cwds.get(id))
            .map(|path| path.to_string_lossy().into_owned())
            .or_else(|| {
                std::env::current_dir()
                    .ok()
                    .map(|path| path.to_string_lossy().into_owned())
            })
            .unwrap_or_default();
        self.lore_picker = Some(LorePicker {
            session_id: self.groups[self.active_group]
                .active_id()
                .map(str::to_owned),
            query: String::new(),
            rows: Vec::new(),
            selected: 0,
            offset: 0,
            proposals: Vec::new(),
            proposal_mode,
            review: None,
            review_scroll: 0,
            review_seen: 0,
            review_width: 0,
            armed_resolution: None,
            can_resolve: false,
            resolving: false,
            cwd: cwd.clone(),
            belief_review: None,
            belief_intent: None,
            can_act_on_beliefs: false,
            belief_action: None,
            belief_note: String::new(),
            retract_armed: false,
            belief_acting: false,
            result_status: None,
            evidence: None,
            status: String::new(),
            pending: None,
        });
        if proposal_mode {
            self.load_lore(lore_picker::Query::Proposals(cwd, 0));
        } else {
            self.load_lore(lore_picker::Query::Beliefs(0));
        }
    }

    /// Render-only gallery fixture: all LORE reads and writes are disabled.
    pub fn show_belief_browser_fixture(&mut self, group: usize, rows: &[(u64, &str, &str)]) {
        self.active_group = group.min(self.groups.len().saturating_sub(1));
        self.belief_browser_fixture = true;
        self.lore_picker = Some(LorePicker {
            session_id: None,
            query: String::new(),
            rows: rows
                .iter()
                .map(|&(id, subject, claim)| lore_picker::Belief {
                    id,
                    subject: subject.into(),
                    claim: claim.into(),
                    truncated: false,
                    confidence: 0.8,
                    evidence_count: Some(1),
                    recency: None,
                })
                .collect(),
            selected: 0,
            offset: 0,
            proposals: Vec::new(),
            proposal_mode: false,
            review: None,
            review_scroll: 0,
            review_seen: 0,
            review_width: 0,
            armed_resolution: None,
            can_resolve: false,
            resolving: false,
            cwd: String::new(),
            belief_review: None,
            belief_intent: None,
            can_act_on_beliefs: false,
            belief_action: None,
            belief_note: String::new(),
            retract_armed: false,
            belief_acting: false,
            result_status: None,
            evidence: None,
            status: "Active beliefs · Accept/Reject review the exact claim".into(),
            pending: None,
        });
        self.belief_fixture_rows = self.lore_picker.as_ref().unwrap().rows.clone();
    }

    #[doc(hidden)]
    pub fn show_belief_browser_timed_fixture(
        &mut self,
        group: usize,
        rows: &[(u64, &str, &str, Option<&str>)],
    ) {
        let plain = rows
            .iter()
            .map(|row| (row.0, row.1, row.2))
            .collect::<Vec<_>>();
        self.show_belief_browser_fixture(group, &plain);
        if let Some(picker) = &mut self.lore_picker {
            for (belief, row) in picker.rows.iter_mut().zip(rows) {
                belief.recency = row.3.map(str::to_owned);
            }
            self.belief_fixture_rows = picker.rows.clone();
        }
    }

    /// Select a real painted row and advance only the hover clock. The fixture
    /// can never request a sidecar or grant a writable belief review.
    /// Select a real painted row and advance only the hover clock. The fixture
    /// can never request a sidecar or grant a writable belief review.
    #[doc(hidden)]
    pub fn show_belief_hover_fixture(&mut self, id: u64) {
        if !self.belief_browser_fixture {
            return;
        }
        let owner = self
            .rendered_belief_rows
            .borrow()
            .iter()
            .find(|owner| owner.id == id)
            .cloned();
        let Some(owner) = owner else {
            return;
        };
        if let Some(picker) = &mut self.lore_picker {
            picker.selected = picker
                .rows
                .iter()
                .position(|row| row.id == id)
                .unwrap_or(picker.selected);
        }
        self.belief_pointer = Some((
            owner.rect.x + owner.rect.width.saturating_sub(1).min(20),
            owner.rect.y,
        ));
        let now = Instant::now();
        self.belief_preview.set_owner(Some(owner), now);
        self.belief_preview.tick(now + Duration::from_millis(500));
    }

    pub(super) fn load_lore(&mut self, query: lore_picker::Query) {
        if self.belief_browser_fixture {
            if let Some(picker) = &mut self.lore_picker {
                picker.status = "Gallery fixture · LORE calls disabled".into();
            }
            return;
        }
        let Some(picker) = &mut self.lore_picker else {
            return;
        };
        self.belief_filter_request = match &query {
            lore_picker::Query::Beliefs(offset) => Some((String::new(), *offset)),
            lore_picker::Query::FilteredBeliefs(offset, query) => Some((query.clone(), *offset)),
            _ => None,
        };
        self.belief_filter_due = None;
        picker.resolving = matches!(
            &query,
            lore_picker::Query::Resolve(..) | lore_picker::Query::BeliefAction(..)
        );
        picker.belief_acting = matches!(&query, lore_picker::Query::BeliefAction(..));
        picker.status = if picker.belief_acting {
            "Applying belief action with LORE…"
        } else if picker.resolving {
            "Resolving this proposal with LORE…"
        } else {
            "Loading from LORE…"
        }
        .into();
        picker.pending = None;
        let (tx, rx) = mpsc::sync_channel(1);
        picker.pending = Some(rx);
        std::thread::spawn(move || {
            let _ = tx.send(lore_picker::fetch(query));
        });
    }

    /// The gallery uses this same state path with deterministic counts. Live
    /// values arrive only through the read-only canonical LORE query below.
    pub fn set_lore_memory_usage(
        &mut self,
        id: &str,
        project_chars: u64,
        project_cap_chars: u64,
        user_chars: u64,
        user_cap_chars: u64,
    ) {
        let usage = doxa_lore::MemoryUsage {
            project_chars,
            project_cap_chars,
            user_chars,
            user_cap_chars,
        };
        self.memory_cache
            .insert(id.to_owned(), (Some(usage), Instant::now()));
        self.memory_repo.insert(id.to_owned(), true);
    }
    /// Deterministic gallery state for the read-only memory menu. Live menus
    /// always use the LORE sidecar through `open_memory_menu`.
    #[doc(hidden)]
    pub fn show_memory_menu_fixture(
        &mut self,
        group: usize,
        user: &[&str],
        project: &[&str],
        beliefs: &[&str],
    ) {
        if group >= self.groups.len() {
            return;
        }
        self.open_chip_info("memory", group);
        let Some(info) = self.chip_info.as_mut() else {
            return;
        };
        info.owner = self.groups[group].active_id().and_then(|id| {
            self.session_cwds
                .get(id)
                .and_then(|cwd| cwd.to_str())
                .map(|cwd| (id.to_owned(), cwd.to_owned()))
        });
        let facts = user
            .iter()
            .take(8)
            .map(|text| crate::memory_menu::Fact {
                scope: "user".into(),
                text: (*text).into(),
                source: None,
                redacted: false,
            })
            .chain(project.iter().take(8).map(|text| crate::memory_menu::Fact {
                scope: "project".into(),
                text: (*text).into(),
                source: None,
                redacted: false,
            }))
            .collect();
        let _ = beliefs; // Beliefs have their own review menu, never curated entries.
        self.memory_list = Some(crate::memory_menu::List {
            owner: info.owner.clone(),
            facts,
            query: String::new(),
        });
        info.lines = vec!["No curated facts".into()];
        info.scroll = 0;
        self.memory_menu_pending = None;
    }

    /// Render a curated-memory fixture through the production manager and reducer.
    /// Render a curated-memory fixture through the production manager and reducer.
    #[doc(hidden)]
    pub fn show_memory_manager_fixture(
        &mut self,
        group: usize,
        scope: &'static str,
        review: serde_json::Value,
    ) -> Result<(), String> {
        if group >= self.groups.len() {
            return Err("Fixture pane missing".into());
        }
        let id = self.groups[group]
            .active_id()
            .ok_or("Fixture session missing")?
            .to_owned();
        let cwd = self
            .session_cwds
            .get(&id)
            .and_then(|path| path.to_str())
            .ok_or("Fixture directory missing")?
            .to_owned();
        self.active_group = group;
        self.memory_manager = Some(crate::memory_menu::Manager::from_fixture_review(
            (id.clone(), cwd.clone()),
            scope,
            review,
        )?);
        self.chip_info = Some(ChipInfo {
            kind: "memory",
            label: String::new(),
            lines: Vec::new(),
            scroll: 0,
            owner: Some((id, cwd)),
        });
        Ok(())
    }
    /// Render an immutable fleet plan; confirmation cannot start a controller.

    pub(super) fn poll_memory(&mut self) -> bool {
        let mut changed = false;
        if let Some((id, cwd, receiver)) = self.memory_pending.take() {
            match receiver.try_recv() {
                Ok(result) => {
                    if self.session_cwds.get(&id).and_then(|path| path.to_str())
                        == Some(cwd.as_str())
                    {
                        let mut repo_changed = false;
                        let usage = result.map(|(usage, repo)| {
                            repo_changed = self.memory_repo.insert(id.clone(), repo) != Some(repo);
                            usage
                        });
                        changed = repo_changed
                            || self
                                .memory_cache
                                .get(&id)
                                .is_none_or(|(old, _)| *old != usage);
                        self.memory_cache.insert(id, (usage, Instant::now()));
                    }
                }
                Err(TryRecvError::Disconnected) => {
                    if self.session_cwds.get(&id).and_then(|path| path.to_str())
                        == Some(cwd.as_str())
                    {
                        changed = self
                            .memory_cache
                            .get(&id)
                            .is_none_or(|(old, _)| old.is_some());
                        self.memory_cache.insert(id, (None, Instant::now()));
                    }
                }
                Err(TryRecvError::Empty) => self.memory_pending = Some((id, cwd, receiver)),
            }
        }
        if self.memory_pending.is_some() {
            return changed;
        }
        // Query the active pane first. The other pane is refreshed once the
        // first query completes; neither query blocks input or redraw.
        for group in std::iter::once(self.active_group)
            .chain((0..self.groups.len()).filter(|group| *group != self.active_group))
        {
            let Some(id) = self.groups[group].active_id().map(str::to_owned) else {
                continue;
            };
            if self.offline_ids.contains(&id) {
                continue;
            }
            let Some(cwd) = self
                .session_cwds
                .get(&id)
                .and_then(|path| path.to_str())
                .map(str::to_owned)
            else {
                continue;
            };
            if self
                .memory_cache
                .get(&id)
                .is_some_and(|(_, checked)| checked.elapsed() < Duration::from_secs(60))
            {
                continue;
            }
            let (tx, rx) = mpsc::sync_channel(1);
            self.memory_pending = Some((id, cwd.clone(), rx));
            std::thread::spawn(move || {
                let (scope, repo) = crate::memory_menu::scope_path(Path::new(&cwd));
                let usage = scope
                    .to_str()
                    .and_then(|scope| {
                        doxa_lore::LoreClient::open(Duration::from_secs(2))
                            .and_then(|mut lore| lore.memory_usage(scope))
                            .ok()
                    })
                    .map(|usage| (usage, repo));
                let _ = tx.send(usage);
            });
            break;
        }
        changed
    }

    pub(super) fn poll_memory_menu(&mut self) -> bool {
        for menu in &mut self.retired_operations {
            menu.poll();
        }
        self.retired_operations.retain(|menu| menu.busy());
        let mut changed = false;
        if let Some(manager) = &mut self.memory_manager {
            let current = self.groups[self.active_group].active_id().and_then(|id| {
                self.session_cwds
                    .get(id)
                    .and_then(|path| path.to_str())
                    .map(|cwd| (id, cwd))
            });
            if current == Some((manager.owner.0.as_str(), manager.owner.1.as_str())) {
                changed |= manager.poll();
                if manager.refresh_scope.take().is_some() {
                    self.memory_cache.clear();
                }
            } else {
                self.memory_manager = None;
                if let Some(info) = &mut self.chip_info {
                    info.lines = vec!["Session changed; reopen memory".into()];
                }
                changed = true;
            }
        }
        if let Some(menu) = &mut self.operations_menu {
            menu.poll();
            if menu.take_plugins_changed() {
                self.plugin_refresh_dirty = true;
            }
            if menu.take_restart() {
                self.restart_waiting = true;
                changed = true;
            }
            if let Some(info) = &mut self.chip_info {
                let lines = menu.lines(usize::from(self.size.width));
                if info.lines != lines {
                    info.lines = lines;
                    changed = true;
                }
            }
        }

        let Some((id, cwd, receiver)) = self.memory_menu_pending.take() else {
            return changed;
        };
        match receiver.try_recv() {
            Ok(result) => {
                if self.groups[self.active_group].active_id() != Some(id.as_str())
                    || self.session_cwds.get(&id).and_then(|path| path.to_str())
                        != Some(cwd.as_str())
                {
                    return false;
                }
                if let Some(info) = self.chip_info.as_mut().filter(|info| info.kind == "memory") {
                    info.lines = match result {
                        Ok(facts) => {
                            let query = self
                                .memory_list
                                .take()
                                .map(|list| list.query)
                                .unwrap_or_default();
                            self.memory_list = Some(crate::memory_menu::List {
                                owner: Some((id.clone(), cwd.clone())),
                                facts,
                                query,
                            });
                            vec!["No matching curated facts".into()]
                        }
                        Err(message) => vec![message.to_owned()],
                    };
                    info.scroll = 0;
                    return true;
                }
                false
            }
            Err(TryRecvError::Empty) => {
                self.memory_menu_pending = Some((id, cwd, receiver));
                false
            }
            Err(TryRecvError::Disconnected) => {
                if self.groups[self.active_group].active_id() != Some(id.as_str())
                    || self.session_cwds.get(&id).and_then(|path| path.to_str())
                        != Some(cwd.as_str())
                {
                    return false;
                }
                if let Some(info) = self.chip_info.as_mut().filter(|info| info.kind == "memory") {
                    info.lines = vec!["LORE unavailable".to_owned()];
                    info.scroll = 0;
                    return true;
                }
                false
            }
        }
    }

    pub(super) fn poll_lore(&mut self) -> bool {
        let Some(picker) = &mut self.lore_picker else {
            return false;
        };
        let Some(receiver) = &picker.pending else {
            return false;
        };
        let result = match receiver.try_recv() {
            Ok(result) => result,
            Err(TryRecvError::Empty) => return false,
            Err(TryRecvError::Disconnected) => Err("LORE worker unavailable"),
        };
        picker.pending = None;
        if self
            .belief_filter_request
            .take()
            .is_some_and(|(query, offset)| picker.query != query || picker.offset != offset)
        {
            return true;
        }
        let was_resolving = picker.resolving;
        let was_belief_acting = picker.belief_acting;
        picker.resolving = false;
        picker.belief_acting = false;
        let mut urgent_resolution = false;
        let mut refresh_after_action = false;
        match result {
            Ok(lore_picker::ResultPage::Beliefs(rows)) => {
                picker.rows = rows;
                picker.selected = 0;
                picker.evidence = None;
                picker.belief_review = None;
                picker.can_act_on_beliefs = false;
                picker.belief_action = None;
                picker.retract_armed = false;
                picker.status = picker.result_status.take().unwrap_or_else(|| {
                    if picker.rows.is_empty() {
                        "No active beliefs on this page"
                    } else {
                        "Active beliefs · newest first"
                    }
                    .into()
                });
            }
            Ok(lore_picker::ResultPage::Search(hit)) => {
                picker.rows = hit
                    .map(|hit| lore_picker::Belief {
                        id: hit.id,
                        subject: "Search match".into(),
                        claim: hit.claim,
                        truncated: hit.claim_truncated,
                        confidence: hit.confidence,
                        evidence_count: None,
                        recency: None,
                    })
                    .into_iter()
                    .collect();
                picker.selected = 0;
                picker.evidence = None;
                picker.belief_review = None;
                picker.can_act_on_beliefs = false;
                picker.belief_action = None;
                picker.retract_armed = false;
                picker.status = if picker.rows.is_empty() {
                    "No active belief matched"
                } else {
                    "LORE search result · cite as a claim"
                }
                .into();
            }
            Ok(lore_picker::ResultPage::Evidence(id, rows)) => {
                if picker
                    .rows
                    .get(picker.selected)
                    .is_some_and(|row| row.id == id)
                {
                    picker.evidence = Some((id, rows));
                    picker.status = "Evidence trail · read only".into();
                }
            }
            Ok(lore_picker::ResultPage::Proposals(rows)) => {
                picker.proposals = rows;
                picker.selected = 0;
                picker.review = None;
                picker.armed_resolution = None;
                picker.status = if picker.proposals.is_empty() {
                    "No staged proposals on this page"
                } else {
                    "Staged proposals · select one to read its complete raw contents"
                }
                .into();
            }
            Ok(lore_picker::ResultPage::Review(review, can_resolve)) => {
                if picker.proposal_mode
                    && picker.proposals.iter().any(|row| row.pid == review.pid())
                {
                    picker.review = Some(review);
                    picker.review_scroll = 0;
                    picker.review_seen = 0;
                    picker.review_width = 0;
                    picker.armed_resolution = None;
                    picker.can_resolve = can_resolve;
                    picker.status = if can_resolve {
                        "Read the complete raw proposal; A approve or R reject after reaching the end"
                    } else { "Read only · installed LORE lacks atomic reviewed resolution" }.into();
                }
            }
            Ok(lore_picker::ResultPage::Resolved(resolution)) => {
                picker.review = None;
                picker.armed_resolution = None;
                picker.status = match resolution {
                    doxa_lore::PendingResolution::Approved => {
                        "Proposal approved and archived".into()
                    }
                    doxa_lore::PendingResolution::Rejected => {
                        "Proposal rejected and archived".into()
                    }
                    doxa_lore::PendingResolution::Indeterminate { code } => {
                        urgent_resolution = true;
                        format!("Proposal may have applied ({code}); recovery required, do not retry automatically")
                    }
                    doxa_lore::PendingResolution::Refused {
                        code,
                        applied: true,
                    } => {
                        urgent_resolution = true;
                        format!("Applied; finalization failed ({code}); do not retry automatically")
                    }
                    doxa_lore::PendingResolution::Refused {
                        code,
                        applied: false,
                    } => format!("Resolution refused: {code}"),
                };
                picker.proposals.clear();
            }
            Ok(lore_picker::ResultPage::BeliefReview(review, can_act)) => {
                if !picker.proposal_mode
                    && picker
                        .rows
                        .get(picker.selected)
                        .is_some_and(|row| row.id == review.id())
                {
                    picker.belief_review = Some(review);
                    picker.review_scroll = 0;
                    picker.review_seen = 0;
                    picker.review_width = 0;
                    picker.belief_action = None;
                    picker.belief_note.clear();
                    picker.retract_armed = false;
                    picker.can_act_on_beliefs = can_act;
                    picker.status = if can_act { "Read the complete belief, then choose C confirmed, X contradicted, S stale, or R retract" }
                        else { "Read only · installed LORE lacks reviewed belief actions" }.into();
                } else {
                    picker.status = "Selection changed; reopen the exact belief review".into();
                    picker.can_act_on_beliefs = false;
                }
            }
            Ok(lore_picker::ResultPage::BeliefActed(result)) => {
                use doxa_lore::BeliefStatus;
                let status = match result.status {
                    BeliefStatus::Active => "active",
                    BeliefStatus::Dormant => "dormant",
                    BeliefStatus::Retracted => "retracted",
                };
                picker.result_status = Some(format!(
                    "Belief action applied · {status} · {} confirmed, {} contradicted, {} stale",
                    result.confirmed, result.contradicted, result.stale
                ));
                picker.belief_review = None;
                picker.belief_action = None;
                picker.belief_note.clear();
                picker.retract_armed = false;
                picker.can_act_on_beliefs = false;
                refresh_after_action = true;
            }
            Err(message) => {
                picker.status = if was_belief_acting {
                    message.into()
                } else if was_resolving {
                    urgent_resolution = true;
                    "Resolution outcome unknown; inspect LORE pending and archive before retrying"
                        .into()
                } else {
                    message.into()
                };
                if picker.proposal_mode {
                    picker.review = None;
                    picker.armed_resolution = None;
                } else {
                    picker.can_act_on_beliefs = false;
                    picker.belief_action = None;
                    picker.retract_armed = false;
                    picker.evidence = None;
                }
            }
        }
        if urgent_resolution {
            self.notice = picker.status.clone();
        }
        if was_belief_acting && !refresh_after_action {
            self.notice = picker.status.clone();
        }
        if refresh_after_action {
            let (offset, query) = (picker.offset, picker.query.clone());
            self.notice = picker.result_status.clone().unwrap_or_default();
            if let Some(id) = picker.session_id.as_ref().filter(|id| {
                self.session_cwds.get(*id).and_then(|path| path.to_str())
                    == Some(picker.cwd.as_str())
            }) {
                self.memory_cache.remove(id);
                self.session_telemetry.entry(id.clone()).or_default().lore = None;
                self.pending_queue_commands
                    .push(crate::bridge::WorkerCommand::Status(id.clone()));
            }
            self.load_lore(lore_picker::Query::FilteredBeliefs(offset, query));
        }
        true
    }

    pub(super) fn edit_memory_filter(&mut self, key: KeyEvent) -> bool {
        let Some(list) = self.memory_list.as_mut() else {
            return false;
        };
        if list.owner.as_ref().is_some_and(|(id, cwd)| {
            self.groups[self.active_group].active_id() != Some(id.as_str())
                || self.session_cwds.get(id).and_then(|path| path.to_str()) != Some(cwd.as_str())
        }) {
            return false;
        }
        match key.code {
            KeyCode::Backspace
                if !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
            {
                list.query.pop();
            }
            KeyCode::Char('u') if key.modifiers == KeyModifiers::CONTROL => list.query.clear(),
            KeyCode::Char(c)
                if !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                    && !unsafe_input_char(c)
                    && lore_picker::append_filter_char(&mut list.query, c) => {}
            _ => return false,
        }
        if let Some(info) = &mut self.chip_info {
            info.scroll = 0;
        }
        true
    }

    pub(super) fn edit_belief_filter(&mut self, key: KeyEvent) -> bool {
        let Some(picker) = self.lore_picker.as_mut().filter(|picker| {
            !picker.proposal_mode
                && picker.belief_review.is_none()
                && picker.evidence.is_none()
                && !picker.resolving
        }) else {
            return false;
        };
        let before = picker.query.clone();
        match key.code {
            KeyCode::Backspace
                if !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
            {
                picker.query.pop();
            }
            KeyCode::Char('u') if key.modifiers == KeyModifiers::CONTROL => picker.query.clear(),
            KeyCode::Char(c)
                if !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                    && !matches!(c, 'A' | 'R' | 'P')
                    && !unsafe_input_char(c)
                    && lore_picker::append_filter_char(&mut picker.query, c) => {}
            _ => return false,
        }
        if before != picker.query {
            picker.selected = 0;
            picker.offset = 0;
            self.belief_filter_due = Some(Instant::now());
        }
        true
    }

    pub(super) fn poll_belief_filter(&mut self, now: Instant) -> bool {
        let Some(due) = self.belief_filter_due else {
            return false;
        };
        let Some(picker) = self
            .lore_picker
            .as_ref()
            .filter(|picker| !picker.proposal_mode && picker.belief_review.is_none())
        else {
            self.belief_filter_due = None;
            return false;
        };
        if now.saturating_duration_since(due) < Duration::from_millis(200) {
            return false;
        }
        self.belief_filter_due = None;
        let (offset, query) = (picker.offset, picker.query.clone());
        if self.belief_browser_fixture {
            let picker = self.lore_picker.as_mut().unwrap();
            let query = query.to_lowercase();
            picker.rows = self
                .belief_fixture_rows
                .iter()
                .filter(|row| {
                    query.is_empty()
                        || row.subject.to_lowercase().contains(&query)
                        || row.claim.to_lowercase().contains(&query)
                })
                .cloned()
                .collect();
            picker.status = if picker.rows.is_empty() {
                "No matching beliefs"
            } else {
                "Filtered beliefs"
            }
            .into();
        } else {
            self.load_lore(lore_picker::Query::FilteredBeliefs(offset, query));
        }
        true
    }

    pub(super) fn lore_picker_key(&mut self, key: KeyEvent) -> bool {
        if self.belief_graph_lines.is_none() && self.edit_belief_filter(key) {
            return true;
        }
        if key.code == KeyCode::Char('g')
            && key.modifiers == KeyModifiers::ALT
            && self.lore_picker.as_ref().is_some_and(|p| {
                !p.proposal_mode
                    && p.belief_review.is_none()
                    && p.evidence.is_none()
                    && p.pending.is_none()
            })
            && self.belief_filter_due.is_none()
        {
            self.open_belief_graph();
            return true;
        }
        if key.code == KeyCode::Esc && self.belief_graph_lines.take().is_some() {
            return true;
        }
        if let Some((_, lines)) = &self.belief_graph_lines {
            match key.code {
                KeyCode::Up => {
                    self.belief_graph_scroll = self.belief_graph_scroll.saturating_sub(1)
                }
                KeyCode::Down => {
                    self.belief_graph_scroll =
                        (self.belief_graph_scroll + 1).min(lines.len().saturating_sub(1))
                }
                KeyCode::PageDown => {
                    self.belief_graph_scroll =
                        (self.belief_graph_scroll + 10).min(lines.len().saturating_sub(1))
                }
                KeyCode::PageUp => {
                    self.belief_graph_scroll = self.belief_graph_scroll.saturating_sub(10)
                }
                _ => {}
            }
            return true;
        }

        let review_area = self.active_chooser_rect();
        let picker = self.lore_picker.as_mut().unwrap();
        if picker.resolving {
            return true;
        }
        if picker.pending.is_some() && key.code != KeyCode::Esc {
            return true;
        }
        if self.belief_filter_due.is_some() && key.code != KeyCode::Esc {
            return true;
        }
        // Dismissal must remain possible when a split or resized pane cannot
        // show the review, and when an exact-selection guard has invalidated it.
        if key.code == KeyCode::Esc {
            if picker.proposal_mode && picker.review.is_some() {
                picker.review = None;
                picker.armed_resolution = None;
                return true;
            }
            if picker.belief_review.is_some() {
                if picker.belief_action.is_some() {
                    picker.belief_action = None;
                    picker.belief_note.clear();
                    picker.retract_armed = false;
                    picker.status = "Belief action cancelled".into();
                } else {
                    picker.belief_review = None;
                    picker.can_act_on_beliefs = false;
                }
                return true;
            }
        }
        if picker.proposal_mode {
            if let Some(review) = &picker.review {
                let Some(area) = review_area else {
                    return true;
                };
                let width = usize::from(area.width.saturating_sub(3)).max(1);
                let visible = usize::from(area.height.saturating_sub(REVIEW_BODY_RESERVE));
                let total = raw_visual_rows(review.raw(), width).len();
                if picker.review_width != width {
                    picker.review_width = width;
                    picker.review_scroll = 0;
                    picker.review_seen = 0;
                    picker.armed_resolution = None;
                }
                if visible == 0 {
                    picker.armed_resolution = None;
                    picker.status = "Enlarge the review to read its complete contents".into();
                    return true;
                }
                if visible > 0 && picker.review_scroll <= picker.review_seen {
                    picker.review_seen = picker
                        .review_seen
                        .max(picker.review_scroll.saturating_add(visible))
                        .min(total);
                }
                let max_scroll = total.saturating_sub(visible);
                match key.code {
                    KeyCode::Esc => {
                        picker.review = None;
                        picker.armed_resolution = None;
                    }
                    KeyCode::Up => {
                        picker.review_scroll = picker.review_scroll.saturating_sub(1);
                        picker.armed_resolution = None;
                    }
                    KeyCode::Down => {
                        picker.review_scroll = (picker.review_scroll + 1).min(max_scroll);
                        picker.armed_resolution = None;
                    }
                    KeyCode::PageUp => {
                        picker.review_scroll = picker
                            .review_scroll
                            .saturating_sub(visible.saturating_sub(1).max(1));
                        picker.armed_resolution = None;
                    }
                    KeyCode::PageDown => {
                        picker.review_scroll = picker
                            .review_scroll
                            .saturating_add(visible.saturating_sub(1).max(1))
                            .min(max_scroll);
                        picker.armed_resolution = None;
                    }
                    KeyCode::Char('a' | 'A')
                        if !key
                            .modifiers
                            .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                            && picker.can_resolve
                            && visible > 0
                            && picker.review_seen == total
                            && picker.pending.is_none() =>
                    {
                        picker.armed_resolution = Some(doxa_lore::PendingDecision::Approve);
                        picker.status =
                            "Approve this exact proposal? Press Enter to confirm, Esc to cancel"
                                .into();
                    }
                    KeyCode::Char('r' | 'R')
                        if !key
                            .modifiers
                            .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                            && picker.can_resolve
                            && visible > 0
                            && picker.review_seen == total
                            && picker.pending.is_none() =>
                    {
                        picker.armed_resolution = Some(doxa_lore::PendingDecision::Reject);
                        picker.status =
                            "Reject this exact proposal? Press Enter to confirm, Esc to cancel"
                                .into();
                    }
                    KeyCode::Enter
                        if picker.armed_resolution.is_some() && picker.pending.is_none() =>
                    {
                        let decision = picker.armed_resolution.take().unwrap();
                        let cwd = picker.cwd.clone();
                        let review = review.clone();
                        self.load_lore(lore_picker::Query::Resolve(cwd, review, decision));
                    }
                    _ => {
                        if picker.review_seen < total {
                            picker.status =
                                "Read through the end before choosing approve or reject".into();
                        }
                    }
                }
                return true;
            }
            match key.code {
                KeyCode::Esc => self.lore_picker = None,
                KeyCode::Char('b') if picker.review.is_none() => {
                    picker.proposal_mode = false;
                    picker.offset = 0;
                    picker.selected = 0;
                    self.load_lore(lore_picker::Query::Beliefs(0));
                }
                KeyCode::Up => picker.selected = picker.selected.saturating_sub(1),
                KeyCode::Down => {
                    picker.selected =
                        (picker.selected + 1).min(picker.proposals.len().saturating_sub(1))
                }
                KeyCode::Enter | KeyCode::Right => {
                    if let Some(pid) = picker
                        .proposals
                        .get(picker.selected)
                        .map(|row| row.pid.clone())
                    {
                        let cwd = picker.cwd.clone();
                        self.load_lore(lore_picker::Query::Review(cwd, pid));
                    }
                }
                KeyCode::PageDown => {
                    picker.offset = picker
                        .offset
                        .saturating_add(lore_picker::PAGE_SIZE as u16)
                        .min(10000);
                    let (cwd, offset) = (picker.cwd.clone(), picker.offset);
                    self.load_lore(lore_picker::Query::Proposals(cwd, offset));
                }
                KeyCode::PageUp => {
                    picker.offset = picker.offset.saturating_sub(lore_picker::PAGE_SIZE as u16);
                    let (cwd, offset) = (picker.cwd.clone(), picker.offset);
                    self.load_lore(lore_picker::Query::Proposals(cwd, offset));
                }
                KeyCode::F(5) => {
                    let (cwd, offset) = (picker.cwd.clone(), picker.offset);
                    self.load_lore(lore_picker::Query::Proposals(cwd, offset));
                }
                _ => return false,
            }
            return true;
        }
        if let Some(review) = &picker.belief_review {
            if !picker
                .rows
                .get(picker.selected)
                .is_some_and(|row| row.id == review.id())
            {
                picker.can_act_on_beliefs = false;
                picker.belief_action = None;
                picker.retract_armed = false;
                picker.status = "Selection changed; reopen the exact belief review".into();
                return true;
            }
            let Some(area) = review_area else {
                return true;
            };
            let width = usize::from(area.width.saturating_sub(3)).max(1);
            let visible = usize::from(area.height.saturating_sub(REVIEW_BODY_RESERVE));
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
                picker.belief_action = None;
                picker.retract_armed = false;
                picker.status = "Enlarge the review to read its complete contents".into();
                return true;
            }
            if visible > 0 && picker.review_scroll <= picker.review_seen {
                picker.review_seen = picker
                    .review_seen
                    .max(picker.review_scroll.saturating_add(visible))
                    .min(total);
            }
            let max_scroll = total.saturating_sub(visible);
            if picker.belief_action.is_none()
                && matches!(key.code, KeyCode::Char('A' | 'R'))
                && !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
            {
                if !picker.can_act_on_beliefs || picker.review_seen != total {
                    picker.status =
                        "Read the complete writable belief before choosing an action".into();
                    return true;
                }
                let reject = matches!(key.code, KeyCode::Char('R'));
                picker.belief_action = Some(if reject {
                    doxa_lore::BeliefAction::Retract
                } else {
                    doxa_lore::BeliefAction::Confirmed
                });
                picker.belief_note = if reject {
                    "Rejected by user in DOXA belief browser"
                } else {
                    "Accepted by user in DOXA belief browser"
                }
                .into();
                picker.retract_armed = false;
                return self.lore_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            }
            if let Some(action) = picker.belief_action {
                match key.code {
                    KeyCode::Esc => {
                        picker.belief_action = None;
                        picker.belief_note.clear();
                        picker.retract_armed = false;
                        picker.status = "Belief action cancelled".into();
                    }
                    KeyCode::Backspace => {
                        picker.belief_note.pop();
                        picker.retract_armed = false;
                    }
                    KeyCode::Char('y' | 'Y')
                        if !key
                            .modifiers
                            .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                            && action == doxa_lore::BeliefAction::Retract
                            && picker.retract_armed
                            && picker.can_act_on_beliefs
                            && picker
                                .rows
                                .get(picker.selected)
                                .is_some_and(|row| row.id == review.id()) =>
                    {
                        let (cwd, exact, note) = (
                            picker.cwd.clone(),
                            review.clone(),
                            picker.belief_note.clone(),
                        );
                        picker.belief_action = None;
                        picker.retract_armed = false;
                        self.load_lore(lore_picker::Query::BeliefAction(cwd, exact, action, note));
                    }
                    KeyCode::Char(c)
                        if !c.is_control()
                            && !key
                                .modifiers
                                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
                    {
                        if picker.belief_note.len() + c.len_utf8() <= 300 {
                            picker.belief_note.push(c);
                            picker.retract_armed = false;
                        } else {
                            picker.status = "Note is limited to 300 UTF-8 bytes".into();
                        }
                    }
                    KeyCode::Enter if picker.belief_note.trim().is_empty() => {
                        picker.status = "Add a note before applying this belief action".into();
                    }
                    KeyCode::Enter
                        if action == doxa_lore::BeliefAction::Retract && !picker.retract_armed =>
                    {
                        picker.retract_armed = true;
                        picker.status =
                            "Confirm retract of this exact belief: press Y; Esc cancels".into();
                    }
                    KeyCode::Enter
                        if action != doxa_lore::BeliefAction::Retract
                            && picker.can_act_on_beliefs
                            && picker
                                .rows
                                .get(picker.selected)
                                .is_some_and(|row| row.id == review.id()) =>
                    {
                        let (cwd, exact, note) = (
                            picker.cwd.clone(),
                            review.clone(),
                            picker.belief_note.clone(),
                        );
                        picker.belief_action = None;
                        picker.retract_armed = false;
                        self.load_lore(lore_picker::Query::BeliefAction(cwd, exact, action, note));
                    }
                    _ => {}
                }
                return true;
            }
            match key.code {
                KeyCode::Esc => {
                    picker.belief_review = None;
                    picker.can_act_on_beliefs = false;
                }
                KeyCode::Up => picker.review_scroll = picker.review_scroll.saturating_sub(1),
                KeyCode::Down => picker.review_scroll = (picker.review_scroll + 1).min(max_scroll),
                KeyCode::PageUp => {
                    picker.review_scroll = picker
                        .review_scroll
                        .saturating_sub(visible.saturating_sub(1).max(1))
                }
                KeyCode::PageDown => {
                    picker.review_scroll = picker
                        .review_scroll
                        .saturating_add(visible.saturating_sub(1).max(1))
                        .min(max_scroll)
                }
                KeyCode::Char(c)
                    if !key
                        .modifiers
                        .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                        && picker.can_act_on_beliefs
                        && visible > 0
                        && picker.review_seen == total =>
                {
                    picker.belief_action = match c.to_ascii_lowercase() {
                        'c' => Some(doxa_lore::BeliefAction::Confirmed),
                        'x' => Some(doxa_lore::BeliefAction::Contradicted),
                        's' => Some(doxa_lore::BeliefAction::Stale),
                        'r' => Some(doxa_lore::BeliefAction::Retract),
                        _ => None,
                    };
                    if picker.belief_action.is_some() {
                        picker.belief_note.clear();
                        picker.retract_armed = false;
                        picker.status = "Enter a note, then press Enter to apply".into();
                    }
                }
                _ => {
                    if picker.review_seen < total {
                        picker.status = "Read the complete belief before choosing an action".into();
                    }
                }
            }
            return true;
        }
        match key.code {
            KeyCode::Esc => {
                if picker.evidence.is_some() {
                    picker.evidence = None;
                } else {
                    self.lore_picker = None;
                }
            }
            KeyCode::Up if picker.evidence.is_none() => {
                picker.selected = picker.selected.saturating_sub(1)
            }
            KeyCode::Down if picker.evidence.is_none() => {
                picker.selected = (picker.selected + 1).min(picker.rows.len().saturating_sub(1))
            }
            KeyCode::Char(c @ ('A' | 'R'))
                if picker.evidence.is_none()
                    && !key
                        .modifiers
                        .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
            {
                picker.belief_intent = Some(if matches!(c, 'R') {
                    doxa_lore::BeliefAction::Retract
                } else {
                    doxa_lore::BeliefAction::Confirmed
                });
                if let Some(id) = picker.rows.get(picker.selected).map(|row| row.id) {
                    let cwd = picker.cwd.clone();
                    self.load_lore(lore_picker::Query::BeliefReview(cwd, id));
                }
            }
            KeyCode::Right if picker.evidence.is_none() => {
                if let Some(id) = picker.rows.get(picker.selected).map(|row| row.id) {
                    self.load_lore(lore_picker::Query::Evidence(id));
                }
            }
            KeyCode::Enter if picker.evidence.is_none() => {
                picker.belief_intent = None;
                if let Some(id) = picker.rows.get(picker.selected).map(|row| row.id) {
                    let cwd = picker.cwd.clone();
                    self.load_lore(lore_picker::Query::BeliefReview(cwd, id));
                }
            }
            KeyCode::Char('P') if picker.evidence.is_none() => {
                picker.proposal_mode = true;
                picker.offset = 0;
                picker.selected = 0;
                let cwd = picker.cwd.clone();
                self.load_lore(lore_picker::Query::Proposals(cwd, 0));
            }
            KeyCode::F(5) if picker.evidence.is_none() => {
                let (offset, query) = (picker.offset, picker.query.clone());
                self.load_lore(lore_picker::Query::FilteredBeliefs(offset, query));
            }
            KeyCode::PageDown | KeyCode::PageUp if picker.evidence.is_none() => {
                picker.offset = if key.code == KeyCode::PageDown {
                    picker
                        .offset
                        .saturating_add(lore_picker::PAGE_SIZE as u16)
                        .min(10000)
                } else {
                    picker.offset.saturating_sub(lore_picker::PAGE_SIZE as u16)
                };
                let (offset, query) = (picker.offset, picker.query.clone());
                self.load_lore(lore_picker::Query::FilteredBeliefs(offset, query));
            }
            KeyCode::Enter if picker.evidence.is_some() => picker.evidence = None,
            _ => return false,
        }
        true
    }

    pub(super) fn open_memory_menu(&mut self, group: usize) {
        self.memory_manager = None;
        self.retire_operations();
        self.open_chip_info("memory", group);
        let Some(info) = self.chip_info.as_mut() else {
            return;
        };
        info.lines = vec!["Loading curated facts…".into()];
        self.memory_list = Some(crate::memory_menu::List {
            owner: None,
            facts: Vec::new(),
            query: String::new(),
        });
        let Some(id) = self.groups[group].active_id().map(str::to_owned) else {
            info.lines = vec!["No active session".into()];
            return;
        };
        let Some(cwd) = self
            .session_cwds
            .get(&id)
            .and_then(|path| path.to_str())
            .map(str::to_owned)
        else {
            info.lines = vec!["Session directory unavailable".into()];
            return;
        };
        info.owner = Some((id.clone(), cwd.clone()));
        self.memory_list.as_mut().unwrap().owner = info.owner.clone();
        let (tx, rx) = mpsc::sync_channel(1);
        self.memory_menu_pending = Some((id, cwd.clone(), rx));
        std::thread::spawn(move || {
            let result = crate::memory_menu::fetch_facts(Path::new(&cwd));
            let _ = tx.send(result);
        });
    }

    pub(super) fn valid_belief_preview_owner(&self, owner: &crate::belief_preview::Owner) -> bool {
        if self.active_group != owner.pane
            || self.groups[self.active_group].active_id() != owner.session.as_deref()
            || self.belief_filter_due.is_some()
            || self.belief_graph_lines.is_some()
            || self.active_request_index().is_some()
            || self.chip_info.is_some()
            || self.map_modal
            || self.diff_modal
            || self.tool_modal
            || self.stop_confirmation.is_some()
        {
            return false;
        }
        let Some(picker) = self.lore_picker.as_ref().filter(|picker| {
            !picker.proposal_mode
                && picker.belief_review.is_none()
                && picker.evidence.is_none()
                && picker.pending.is_none()
                && !picker.resolving
        }) else {
            return false;
        };
        if picker.cwd != owner.cwd || picker.query != owner.query || picker.offset != owner.offset {
            return false;
        }
        if !picker.rows.get(picker.selected).is_some_and(|row| {
            row.id == owner.id
                && row.subject == owner.subject
                && row.claim == owner.claim
                && row.truncated == owner.truncated
        }) {
            return false;
        }
        self.belief_pointer
            .is_some_and(|(x, y)| owner.rect.contains(ratatui::layout::Position::new(x, y)))
            && self.rendered_belief_rows.borrow().contains(owner)
    }

    pub(super) fn valid_memory_preview_owner(&self, owner:&crate::belief_preview::Owner)->bool {
        let Some(manager)=self.memory_manager.as_ref().filter(|manager|!manager.editing()) else{return false;};
        if self.active_group!=owner.pane || manager.owner.0!=owner.session.as_deref().unwrap_or("")
            || manager.owner.1!=owner.cwd || manager.scope!=owner.query || manager.selected as u64+1!=owner.id
            || self.active_chooser_rect()!=Some(owner.menu) {return false;}
        let visible=usize::from(owner.menu.height.saturating_sub(4));
        let correct=manager.visible_entries(visible).into_iter().enumerate().any(|(offset,(index,entry))| {
            index as u64+1==owner.id && entry==owner.claim && owner.rect==ratatui::layout::Rect::new(owner.menu.x+1,owner.menu.y+2+offset as u16,owner.menu.width.saturating_sub(2),1)
        });
        correct && self.belief_pointer.is_some_and(|(x,y)|owner.rect.contains(ratatui::layout::Position::new(x,y)))
            && self.rendered_belief_rows.borrow().contains(owner)
    }
    pub(super) fn tick_belief_preview(&mut self, now: Instant) -> bool {
        let owner = self.belief_pointer.and_then(|(x, y)| {
            self.rendered_belief_rows
                .borrow()
                .iter()
                .find(|owner| {
                    owner.rect.contains(ratatui::layout::Position::new(x, y))
                        && self.valid_belief_preview_owner(owner)
                })
                .cloned()
        });
        let mut changed = self.belief_preview.set_owner(owner, now);
        changed |= self.belief_preview.tick(now);
        if self.belief_preview.needs_read() && !self.belief_browser_fixture {
            self.belief_preview.read();
        }
        changed |= self.belief_preview.poll();
        let memory_owner=self.belief_pointer.and_then(|(x,y)|self.rendered_belief_rows.borrow().iter()
            .find(|owner|owner.rect.contains(ratatui::layout::Position::new(x,y)) && self.valid_memory_preview_owner(owner)).cloned());
        changed |= self.memory_preview.set_cached_owner(memory_owner,now);
        changed |= self.memory_preview.tick(now);
        changed
    }
}
