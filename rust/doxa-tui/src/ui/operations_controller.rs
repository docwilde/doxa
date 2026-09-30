//! Coordinate explicit setup, settings, fleet and maintenance operations.
use super::{
    fleet_menu, fleet_process, operations_menu, safe_label, App, ChipInfo, SettingsMenu, COMMANDS,
    MAX_PENDING_PROMPTS,
};
use crossterm::event::KeyCode;
use crossterm::event::KeyEvent;
use crossterm::event::KeyModifiers;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Instant;

impl App {
    pub(super) fn refresh_vendor_credentials(&mut self) {
        let changes = self.operations_menu.as_mut().map(|menu| menu.take_credentials_changed()).unwrap_or_default();
        for vendor in changes {
            let ids = self.session_identity.iter().filter_map(|(id,(engine,_))| {
                (engine.as_deref() == Some(vendor.engine_id()) && self.sessions.iter().any(|session| session.id == *id) && !self.offline_ids.contains(id)).then(|| id.clone())
            }).collect::<Vec<_>>();
            for id in ids {
                self.session_catalogs.remove(&id);
                if !self.pending_model_queries.contains(&id) { self.pending_model_queries.push(id); }
            }
            let engine = match vendor { doxa_vendors::Vendor::DeepSeek => crate::launch::Engine::DeepSeek, doxa_vendors::Vendor::Glm => crate::launch::Engine::Glm };
            if self.new_session.as_ref().is_some_and(|form| form.engine == engine) {
                let pending = self.request_vendor_catalog(engine);
                if let Some(form) = self.new_session.as_mut() {
                    form.catalog_pending = pending;
                    form.catalog_note = if pending { "Checking vendor model catalog…" } else { "Static fallback; vendor catalog unavailable" }.into();
                }
            }
        }
    }
    pub(super) fn offer_first_run(&mut self) -> bool {
        if self.operations_menu.is_some()
            || self.chip_info.is_some()
            || self.settings_menu.is_some()
            || self.history_modal
        {
            return false;
        }
        self.operations_menu = Some(operations_menu::Menu::new("setup"));
        self.chip_info = Some(ChipInfo {
            kind: "operations",
            label: String::new(),
            lines: self
                .operations_menu
                .as_ref()
                .unwrap()
                .lines(usize::from(self.size.width)),
            scroll: 0,
            owner: None,
        });
        if self.active_chooser_rect().is_none() {
            self.operations_menu = None;
            self.chip_info = None;
            return false;
        }
        // Mark when offered; Escape must not cause another automatic offer.
        let marked = crate::first_run::mark_seen();
        if matches!(marked, Ok(false)) {
            self.operations_menu = None;
            self.chip_info = None;
            return true;
        }
        self.operations_menu.as_mut().unwrap().start_requested();
        true
    }

    pub(super) fn open_operations(&mut self, menu: operations_menu::Menu) {
        self.clipboard_job = None;
        self.clipboard_secret_owner = None;
        self.memory_menu_pending = None;
        self.memory_manager = None;
        self.retire_operations();
        self.operations_menu = Some(menu);
        self.chip_info = Some(ChipInfo {
            kind: "operations",
            label: String::new(),
            lines: self
                .operations_menu
                .as_ref()
                .unwrap()
                .lines(usize::from(self.size.width)),
            scroll: 0,
            owner: None,
        });
        if self.active_chooser_rect().is_none() {
            self.operations_menu = None;
            self.chip_info = None;
            self.notice = "Enlarge pane to open operations".into();
        } else {
            self.input.clear();
            self.input_cursor = 0;
            self.operations_menu.as_mut().unwrap().start_requested();
        }
    }

    pub(super) fn retire_operations(&mut self) {
        if self.clipboard_secret_owner.take().is_some() { self.clipboard_job = None; }
        if let Some(mut menu) = self.operations_menu.take() {
            menu.cancel();
            if menu.busy() {
                self.retired_operations.push(menu);
            }
        }
    }

    pub(super) fn local_mesh(&mut self, args: &str, root: Option<PathBuf>) {
        let args = args.trim();
        if !args.is_empty()
            && (args.len() > 200
                || !args
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.')))
        {
            self.notice = "Usage: /mesh [RUN|stop]".into();
            return;
        }
        let handle = self
            .window_mesh
            .get_or_insert_with(crate::mesh_control::WindowMesh::new)
            .handle();
        self.open_operations(operations_menu::Menu::mesh(handle.clone()));
        if self.operations_menu.is_some() {
            if args.eq_ignore_ascii_case("stop") {
                handle.stop();
            } else {
                handle.start((!args.is_empty()).then(|| args.to_owned()), root);
            }
        }
    }

    pub(super) fn local_fleet(&mut self, args: &str) {
        let current = self
            .fleet_controller
            .as_ref()
            .map(|controller| (controller.root.clone(), controller.id.clone()))
            .or_else(|| {
                self.fleet_menu
                    .as_ref()
                    .and_then(|menu| menu.verified_view())
                    .map(|view| (view.root, view.run_id))
            });
        match args.trim() {
            "stop" => {
                if let Some(controller) = self.fleet_controller.as_mut() {
                    controller.cancel();
                    self.input.clear();
                    self.input_cursor = 0;
                    self.notice = "Stopping owned fleet; waiting for slot teardown".into();
                } else {
                    self.notice="No controller owned by this window; use doxa fleet stop RUN for a verified saved run".into();
                }
                return;
            }
            "detach" => {
                if let Some(controller) = self.fleet_controller.take() {
                    let (root, id) = controller.detach();
                    self.open_fleet(root, Some(id));
                    self.notice="Fleet detached; it keeps running with its existing budget and approval limits".into();
                    self.input.clear();
                    self.input_cursor = 0;
                } else {
                    self.notice = "No live controller owned by this window".into();
                }
                return;
            }
            "status" | "mesh" => {
                if let Some((root, id)) = current {
                    if args.trim() == "mesh" {
                        self.local_mesh(&id, Some(root));
                    } else {
                        self.open_fleet(root, Some(id));
                        self.input.clear();
                        self.input_cursor = 0;
                    }
                } else {
                    self.notice = "Select a live fleet run first, or specify its RUN ID".into();
                }
                return;
            }
            _ => {}
        }
        let parts = match fleet_process::words(args) {
            Ok(parts) => parts,
            Err(error) => {
                self.notice = safe_label(&error.to_string());
                return;
            }
        };
        if let [verb, slot] = parts.as_slice() {
            if verb == "attach" && slot.parse::<usize>().is_ok() {
                if let Some((root, id)) = current {
                    if let Ok((_, session)) =
                        crate::fleet_view::slot_socket(&root, &id, slot.parse().unwrap())
                    {
                        self.attach_selected(&session);
                    } else {
                        self.notice =
                            "Fleet slot attachment refused; verify run and live slot".into();
                    }
                } else {
                    self.notice = "Choose a current fleet, or use /fleet attach RUN INDEX".into();
                }
                return;
            }
        }
        let start = parts.first().is_some_and(|part| part == "start");
        let mut command_words = Vec::new();
        let mut custom_root = None;
        let mut index = 0;
        while index < parts.len() {
            if !start && parts[index] == "--root" {
                index += 1;
                let Some(path) = parts.get(index) else {
                    self.notice = "Fleet --root requires an absolute path".into();
                    return;
                };
                let path = PathBuf::from(path);
                if !path.is_absolute() || custom_root.is_some() {
                    self.notice = "Fleet requires one absolute --root path".into();
                    return;
                }
                custom_root = Some(path);
            } else {
                command_words.push(parts[index].as_str());
            }
            index += 1;
        }
        let root = match custom_root
            .map(Ok)
            .unwrap_or_else(crate::fleet_view::default_root)
        {
            Ok(root) => root,
            Err(error) => {
                self.notice = safe_label(&error.to_string());
                return;
            }
        };
        let words = command_words;
        if words
            .first()
            .is_some_and(|word| matches!(*word, "start" | "resume"))
        {
            if self.fleet_controller.is_some() {
                self.notice =
                    "Wait for the current fleet controller to finish or Ctrl+C cancel it".into();
                return;
            }
            let prepared = match words.as_slice() {
                ["start", ..] => fleet_process::Prepared::start(
                    parts[1..].to_vec(),
                    self.groups[self.active_group]
                        .active_id()
                        .and_then(|id| self.session_cwds.get(id))
                        .map(PathBuf::as_path),
                ),
                ["resume", id] => fleet_process::Prepared::resume(root, id),
                _ => {
                    self.notice = "Usage: /fleet resume RUN".into();
                    return;
                }
            };
            match prepared {
                Ok(prepared) => {
                    let lines = prepared.lines.clone();
                    self.fleet_review = Some(prepared);
                    self.fleet_menu = None;
                    self.chip_info = Some(ChipInfo {
                        kind: "fleet_review",
                        label: String::new(),
                        lines,
                        scroll: 0,
                        owner: None,
                    });
                    if self.active_chooser_rect().is_none() {
                        self.fleet_review = None;
                        self.chip_info = None;
                        self.notice = "Enlarge terminal before reviewing fleet launch".into();
                    } else {
                        self.input.clear();
                        self.input_cursor = 0;
                    }
                }
                Err(error) => self.notice = format!("Fleet: {}", safe_label(&error.to_string())),
            }
            return;
        }
        match words.as_slice(){
            ["mesh",id]=>self.local_mesh(id,Some(root)),
            []|["runs"]=>self.open_fleet(root,None),
            ["status",id]if doxa_state::valid_session_id(id)=>self.open_fleet(root,Some((*id).into())),
            ["attach",id,index]=>match index.parse::<usize>().ok().and_then(|index|crate::fleet_view::slot_socket(&root,id,index).ok()){
                Some((_,session))=>self.attach_selected(&session),None=>self.notice="Fleet slot attachment refused; verify run and live slot".into()},
            _=>self.notice="Usage: /fleet [runs|status [RUN]|stop|detach|attach [RUN] INDEX|mesh [RUN]|start OPTIONS|resume RUN]".into()
        }
    }
    pub(super) fn fleet_review_key(&mut self, key: KeyEvent) -> bool {
        let review = self.fleet_review.as_mut().unwrap();
        match key.code {
            KeyCode::Esc => {
                self.fleet_review = None;
                self.chip_info = None;
            }
            KeyCode::Up | KeyCode::PageUp => {
                let info = self.chip_info.as_mut().unwrap();
                info.scroll =
                    info.scroll
                        .saturating_sub(if key.code == KeyCode::Up { 1 } else { 8 });
            }
            KeyCode::Down | KeyCode::PageDown => {
                let info = self.chip_info.as_mut().unwrap();
                info.scroll =
                    info.scroll
                        .saturating_add(if key.code == KeyCode::Down { 1 } else { 8 });
            }
            KeyCode::Char('A') if key.modifiers == KeyModifiers::SHIFT => {
                if review.complete.get() {
                    review.armed = true;
                    self.notice = "Fleet launch armed · Shift+Y confirms".into();
                } else {
                    self.notice = "Read the complete fleet plan before arming".into();
                }
            }
            KeyCode::Char('Y') if key.modifiers == KeyModifiers::SHIFT => {
                if review.armed && review.complete.get() {
                    let prepared = self.fleet_review.take().unwrap();
                    let result = std::env::current_exe().and_then(|exe| prepared.launch(&exe));
                    match result {
                        Ok(controller) => {
                            let root = controller.root.clone();
                            let id = controller.id.clone();
                            self.fleet_controller = Some(controller);
                            self.open_fleet(root, Some(id));
                            self.notice =
                                "Native fleet controller started · Ctrl+C cancels with teardown"
                                    .into();
                        }
                        Err(error) => {
                            self.chip_info = None;
                            self.notice =
                                format!("Fleet launch refused: {}", safe_label(&error.to_string()));
                        }
                    }
                }
            }
            _ => {}
        }
        true
    }
    pub(super) fn open_fleet(&mut self, root: PathBuf, run: Option<String>) {
        self.fleet_menu = Some(fleet_menu::Menu::new(root, run));
        self.chip_info = Some(ChipInfo {
            kind: "fleet",
            label: "Fleet".into(),
            lines: vec!["Loading fleet…".into()],
            scroll: 0,
            owner: None,
        });
        self.input.clear();
        self.input_cursor = 0;
    }
    pub(super) fn poll_fleet(&mut self) -> bool {
        let mut changed = false;
        if let Some(controller) = self.fleet_controller.as_mut() {
            match controller.poll() {
                Ok(Some(success)) => {
                    let root = controller.root.clone();
                    let id = controller.id.clone();
                    self.fleet_controller = None;
                    if self.fleet_menu.is_some() || self.chip_info.is_none() {
                        self.open_fleet(root, Some(id));
                    }
                    self.notice = if success {
                        "Fleet controller exited; showing actual manifest status".into()
                    } else {
                        "Fleet controller exited unsuccessfully; inspect actual manifest status"
                            .into()
                    };
                    changed = true;
                }
                Err(_) => {
                    controller.cancel();
                    self.notice="Fleet controller wait failed; cancelling and retaining ownership until reaped".into();
                }
                _ => {}
            }
        }
        let Some(menu) = self.fleet_menu.as_mut() else {
            return changed;
        };
        if self
            .chip_info
            .as_ref()
            .is_none_or(|info| info.kind != "fleet")
        {
            self.fleet_menu = None;
            return changed;
        }
        let polled = menu.poll();
        if polled {
            self.chip_info.as_mut().unwrap().lines = menu.display();
            if let Some(view) = menu.verified_view() {
                if self.fleet_views.len() < fleet_menu::MAX_SAVED_VIEWS
                    && !self.fleet_views.contains(&view)
                {
                    self.fleet_views.push(view);
                }
            }
        }
        changed || polled
    }

    pub(super) fn local_message(&mut self, args: &str) {
        let Some(id) = self.groups[self.active_group]
            .active_id()
            .map(str::to_owned)
        else {
            self.notice = "Select a session before messaging a peer".into();
            return;
        };
        let mut parts = args.trim().splitn(2, char::is_whitespace);
        let target = parts.next().unwrap_or("");
        let body = parts.next().unwrap_or("").trim();
        if target.is_empty() || body.is_empty() {
            self.notice = "Usage: /msg <session_prefix> <text>".into();
        } else if self.pending_peer_messages.len() >= MAX_PENDING_PROMPTS {
            self.notice = "Peer message queue full · wait for daemon".into();
        } else {
            self.pending_peer_messages
                .push((id, target.to_owned(), body.to_owned()));
            self.input.clear();
            self.input_cursor = 0;
            self.notice = "Peer message queued".into();
        }
    }
    pub(super) fn refresh_clock(&mut self) {
        let now = std::time::SystemTime::now();
        self.clock_text = self.preferences.clock(now);
        self.clock_deadline = self
            .preferences
            .clock_delay(now)
            .map(|delay| Instant::now() + delay);
    }
    pub(super) fn tick_clock(&mut self, now: Instant) -> bool {
        if self.clock_deadline.is_some_and(|deadline| now >= deadline) {
            let old = self.clock_text.clone();
            self.refresh_clock();
            return old != self.clock_text;
        }
        false
    }
    pub fn notify_update_available(&self, body: &str) {
        if self
            .preferences
            .should_notify("notify_update", self.window_focused)
        {
            crate::preferences::notify("DOXA update available", body);
        }
    }
    pub(super) fn persist_sidebar(&mut self) {
        self.sidebar_auto = false;
        if self.persist_preferences {
            self.rail_width = self.rail_width.clamp(22, 41);
        }
        if !self.persist_preferences {
            return;
        }
        let edits = vec![
            (
                "sidebar".into(),
                Some(if self.rail_visible { "1" } else { "0" }.into()),
            ),
            ("sidebar_width".into(), Some(self.rail_width.to_string())),
        ];
        if let Err(error) =
            crate::settings::config_path().and_then(|p| crate::settings::save(&p, &edits, "claude"))
        {
            self.notice = format!(
                "Sidebar changed for this window; preference unchanged: {}",
                safe_label(&error.to_string())
            );
        }
    }
    pub(super) fn open_settings_menu(&mut self) {
        let identity = self.groups[self.active_group]
            .active_id()
            .and_then(|id| self.session_identity.get(id));
        let engine = identity
            .and_then(|s| s.0.clone())
            .unwrap_or_else(|| "claude".into());
        let model = identity.and_then(|s| s.1.as_deref());
        match crate::settings::rows(&engine, model) {
            Ok(rows) => {
                self.settings_menu = Some(SettingsMenu {
                    rows,
                    selected: 0,
                    category: 0,
                    draft: None,
                    edits: HashMap::new(),
                    engine,
                });
                if self.active_chooser_rect().is_none() {
                    self.settings_menu = None;
                    self.notice = "Enlarge active pane to edit settings".into();
                }
            }
            Err(error) => {
                self.notice = format!("Settings unavailable: {}", safe_label(&error.to_string()))
            }
        }
    }
    pub(super) fn save_settings_menu(&mut self) {
        let Some(menu) = &mut self.settings_menu else {
            return;
        };
        menu.finish_draft();
        let edits = menu
            .edits
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect::<Vec<_>>();
        let engine = menu.engine.clone();
        let result =
            crate::settings::config_path().and_then(|p| crate::settings::save(&p, &edits, &engine));
        match result {
            Ok(()) => {
                match crate::keybindings::Bindings::load() {
                    Ok(bindings) => self.keybindings = bindings,
                    Err(error) => {
                        self.notice = format!("Settings saved, but keybindings could not reload: {error}");
                        return;
                    }
                }
                let auto_diff_was_on = self.preferences.on("auto_diff");
                self.preferences = crate::preferences::Preferences::load();
                if !auto_diff_was_on && self.preferences.on("auto_diff") {
                    for id in self
                        .sessions
                        .iter()
                        .map(|s| s.id.clone())
                        .collect::<Vec<_>>()
                    {
                        self.request_auto_diff(&id);
                    }
                }
                self.sidebar_auto = self.preferences.value("sidebar").is_empty();
                self.rail_width = self.preferences.sidebar_width();
                self.rail_visible = match self.preferences.value("sidebar") {
                    "" => self.sessions.len() > 1 || !self.collections.is_empty(),
                    "0" | "false" | "off" | "no" => false,
                    _ => true,
                };
                self.refresh_clock();
                match crate::settings::rows(&engine, None) {
                    Ok(rows) => {
                        let menu = self.settings_menu.as_mut().unwrap();
                        menu.rows = rows;
                        menu.edits.clear();
                        self.notice =
                            "Settings saved · session defaults apply to new sessions".into();
                    }
                    Err(error) => {
                        self.notice = format!(
                            "Settings saved; refresh failed: {}",
                            safe_label(&error.to_string())
                        )
                    }
                }
            }
            Err(error) => {
                self.notice = format!("Settings unchanged: {}", safe_label(&error.to_string()))
            }
        }
    }
    pub(super) fn settings_menu_key(&mut self, key: KeyEvent) -> bool {
        let Some(menu) = self.settings_menu.as_mut() else {
            return false;
        };
        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('s') {
            self.save_settings_menu();
            return true;
        }
        if key.modifiers.contains(KeyModifiers::SHIFT)
            && matches!(key.code, KeyCode::Left | KeyCode::Right)
        {
            menu.finish_draft();
            menu.category = if key.code == KeyCode::Right {
                (menu.category + 1) % crate::settings::CATEGORIES.len()
            } else {
                (menu.category + crate::settings::CATEGORIES.len() - 1)
                    % crate::settings::CATEGORIES.len()
            };
            menu.selected = menu.indices().first().copied().unwrap_or(0);
            return true;
        }
        if let Some((_, draft)) = &mut menu.draft {
            match key.code {
                KeyCode::Esc => menu.draft = None,
                KeyCode::Backspace => {
                    draft.pop();
                }
                KeyCode::Char(ch)
                    if !key
                        .modifiers
                        .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                        && !ch.is_control()
                        && draft.len() < 4096 =>
                {
                    draft.push(ch)
                }
                KeyCode::Enter => {
                    menu.finish_draft();
                    self.save_settings_menu();
                }
                _ => {}
            }
            return true;
        }
        match key.code {
            KeyCode::Esc => self.settings_menu = None,
            KeyCode::Up | KeyCode::Down | KeyCode::Tab | KeyCode::BackTab => {
                let indices = menu.indices();
                if let Some(position) = indices.iter().position(|i| *i == menu.selected) {
                    let next = if matches!(key.code, KeyCode::Up | KeyCode::BackTab) {
                        position.saturating_sub(1)
                    } else {
                        (position + 1).min(indices.len().saturating_sub(1))
                    };
                    menu.selected = indices[next];
                }
            }
            KeyCode::Char('u') | KeyCode::Delete | KeyCode::Enter | KeyCode::Char(' ') => {
                let Some(row) = menu.rows.get(menu.selected) else {
                    return true;
                };
                if row.shadowed {
                    self.notice = format!(
                        "Environment override is active; unset {} before editing",
                        row.setting.env
                    );
                } else if row.setting.read_only || row.setting.key.is_empty() {
                    self.notice =
                        format!("{} is read-only. {}", row.setting.label, row.setting.note);
                } else if matches!(key.code, KeyCode::Char('u') | KeyCode::Delete) {
                    menu.edits.insert(row.setting.key.into(), None);
                } else if matches!(
                    row.setting.kind,
                    crate::settings::Kind::Bool | crate::settings::Kind::BoolOn
                ) {
                    let current = menu
                        .edits
                        .get(row.setting.key)
                        .and_then(|v| v.as_deref())
                        .unwrap_or(&row.value);
                    let on = matches!(current, "on" | "1" | "true" | "yes");
                    menu.edits.insert(
                        row.setting.key.into(),
                        Some(if on { "off" } else { "on" }.into()),
                    );
                } else {
                    let value = menu
                        .edits
                        .get(row.setting.key)
                        .and_then(|v| v.as_deref())
                        .unwrap_or(&row.stored);
                    menu.draft = Some((
                        row.setting.key.into(),
                        if value.is_empty() {
                            row.setting.default.into()
                        } else {
                            value.into()
                        },
                    ));
                }
            }
            _ => {}
        }
        true
    }
    /// Render an immutable fleet plan; confirmation cannot start a controller.
    #[doc(hidden)]
    pub fn show_fleet_review_fixture(&mut self, review: &serde_json::Value) -> Result<(), String> {
        let prepared = fleet_process::Prepared::from_fixture_review(review)
            .map_err(|error| error.to_string())?;
        self.chip_info = Some(ChipInfo {
            kind: "fleet_review",
            label: String::new(),
            lines: prepared.lines.clone(),
            scroll: 0,
            owner: None,
        });
        self.fleet_review = Some(prepared);
        Ok(())
    }
    /// Render a manifest-status fixture without disk access or saved run state.
    /// Render a manifest-status fixture without disk access or saved run state.
    #[doc(hidden)]
    pub fn show_fleet_view_fixture(&mut self, id: &str, lines: &[&str]) {
        let lines = lines
            .iter()
            .map(|line| (*line).to_owned())
            .collect::<Vec<_>>();
        self.fleet_menu = Some(fleet_menu::Menu::from_fixture(
            PathBuf::from("/demo/fleet"),
            id,
            lines.clone(),
        ));
        self.chip_info = Some(ChipInfo {
            kind: "fleet",
            label: "Fixture fleet".into(),
            lines,
            scroll: 0,
            owner: None,
        });
    }

    pub(super) fn restart_after_install(
        &mut self,
        state: &mut Option<(crate::ui_state::UiStateStore, Vec<String>, Arc<Mutex<bool>>)>,
    ) -> bool {
        if self.restart_waiting {
            self.restart_waiting = false;
            if self.fleet_controller.is_some()
                || !self.local_shell_jobs.is_empty()
                || self.launching
                || !self.pending_prompts.is_empty()
                || !self.pending_launches.is_empty()
                || !self.attaching_ids.is_empty()
                || self
                    .groups
                    .iter()
                    .flat_map(|g| &g.tabs)
                    .filter(|id| !self.offline_ids.contains(*id))
                    .any(|id| {
                        self.session_activity
                            .get(id)
                            .is_none_or(|(running, queued)| *running || *queued > 0)
                    })
            {
                self.notice =
                    "Update installed; restart postponed because sessions became busy".into();
                return true;
            }
            let Some((store, _, complete)) = state else {
                self.notice = "Update installed; restart needs a durable saved tabset".into();
                return true;
            };
            if !matches!(store.save_if_complete(self, complete), Ok(true)) {
                self.notice =
                    "Update installed; restart postponed because the tabset could not be saved"
                        .into();
                return true;
            }
            let mut ids = self
                .groups
                .iter()
                .flat_map(|g| &g.tabs)
                .filter(|id| !self.offline_ids.contains(*id))
                .cloned()
                .collect::<Vec<_>>();
            ids.sort();
            ids.dedup();
            match crate::maintenance::Restart::start(ids) {
                Ok(worker) => {
                    self.restart_job = Some(worker);
                    self.notice =
                        "Finalizing idle sessions for restart; saved tabs will be restored".into();
                }
                Err(_) => {
                    self.notice =
                        "Update installed; restart postponed because a target is no longer live"
                            .into();
                }
            }
            return true;
        }
        if let Some(report) = self.restart_job.as_ref().and_then(|worker| worker.poll()) {
            self.restart_job = None;
            for id in report.stopped {
                self.offline_ids.insert(id.clone());
                self.session_activity.remove(&id);
                self.input_requests
                    .retain(|request| request.session_id != id);
            }
            if let Some(error) = report.error {
                self.notice = error.into();
            } else {
                self.restart_after_update = true;
                self.should_quit = true;
            }
            return true;
        }
        false
    }

    pub(super) fn poll_plugin_commands(&mut self) -> bool {
        let mut changed = false;
        if let Some(result) = self
            .plugin_refresh
            .as_ref()
            .and_then(|worker| worker.poll())
        {
            self.plugin_refresh = None;
            if !self.plugin_refresh_dirty {
                let mut rows = result.unwrap_or_default();
                rows.retain(|row| !COMMANDS.iter().any(|builtin| builtin.name == row.name));
                rows.sort_by(|left, right| left.name.cmp(&right.name));
                rows.dedup_by(|left, right| left.name == right.name);
                self.plugin_commands = rows;
                changed = true;
            }
        }
        if self.plugin_refresh_dirty && self.plugin_refresh.is_none() {
            self.plugin_refresh_dirty = false;
            self.plugin_commands.clear();
            self.plugin_refresh = Some(crate::operations::PluginRefresh::start());
            changed = true;
        }
        changed
    }

    pub(super) fn apply_installation(&mut self, snapshot: crate::installation::Snapshot) {
        let available = snapshot.update == crate::installation::Update::Available;
        self.installation = snapshot;
        if available && !self.update_notified {
            self.update_notified = true;
            self.notify_update_available(
                "Configured main differs from this installation · /update",
            );
        }
        if self
            .chip_info
            .as_ref()
            .is_some_and(|info| info.kind == "about")
        {
            self.open_about();
        }
    }

    pub(super) fn open_about(&mut self) {
        let id = self.groups[self.active_group].active_id();
        let mut lines = vec![format!("DOXA Rust {}", env!("CARGO_PKG_VERSION"))];
        lines.extend(self.installation.rows.iter().cloned());
        lines.push(format!("Update · {}", self.installation.update.label()));
        lines.push(String::new());
        if let Some(id) = id {
            lines.push(format!("Active session · {}", safe_label(id)));
            if let Some((engine, model)) = self.session_identity.get(id) {
                if let Some(engine) = engine {
                    lines.push(format!("Engine · {engine}"));
                }
                if let Some(model) = model {
                    lines.push(format!("Model · {model}"));
                }
            }
            if let Some(account) = self
                .session_telemetry
                .get(id)
                .and_then(|state| state.account.as_ref())
            {
                for (key, label) in [
                    ("email", "Account"),
                    ("organization", "Organization"),
                    ("subscriptionType", "Subscription"),
                    ("apiProvider", "API provider"),
                ] {
                    if let Some(text) = account[key].as_str() {
                        lines.push(format!("{label} · {text}"));
                    }
                }
            } else {
                lines.push("Account identity unavailable for this connected session".into());
            }
        } else {
            lines.push("No active session".into());
        }
        self.chip_info = Some(ChipInfo {
            kind: "about",
            label: String::new(),
            lines,
            scroll: 0,
            owner: id.map(|id| (id.to_owned(), String::new())),
        });
        if self.active_chooser_rect().is_none() {
            self.chip_info = None;
            self.notice = "Enlarge active pane to open about".into();
        }
    }

    pub(super) fn open_diagnostic(&mut self, kind: &'static str) {
        let Some(id) = self.groups[self.active_group]
            .active_id()
            .map(str::to_owned)
        else {
            self.notice = "Select a session first".into();
            return;
        };
        let telemetry = self.session_telemetry.get(&id);
        let model = self
            .session_identity
            .get(&id)
            .and_then(|identity| identity.1.as_deref())
            .unwrap_or("not reported");
        let mut lines = vec![
            format!(
                "session  {}",
                safe_label(&id.chars().take(8).collect::<String>())
            ),
            format!("model    {}", safe_label(model)),
        ];
        if kind == "usage" {
            let number = |field: Option<u64>| {
                field
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "not reported".into())
            };
            lines.push(format!(
                "turns    {}",
                number(telemetry.and_then(|value| value.turns))
            ));
            for (label, field) in [
                ("tokens in", telemetry.and_then(|value| value.input_tokens)),
                (
                    "tokens out",
                    telemetry.and_then(|value| value.output_tokens),
                ),
                (
                    "cache read",
                    telemetry.and_then(|value| value.cache_read_tokens),
                ),
                (
                    "cache write",
                    telemetry.and_then(|value| value.cache_write_tokens),
                ),
            ] {
                lines.push(format!("{label:<11}{}", number(field)));
            }
            lines.push(format!(
                "cost     {}",
                telemetry
                    .and_then(|value| value.session_cost.as_deref())
                    .unwrap_or("not reported by this engine")
            ));
            if let Some(quota) = telemetry.and_then(|value| value.quota.as_deref()) {
                lines.push(format!("quota    {}", safe_label(quota)));
            }
        }
        let used = telemetry.and_then(|value| value.context_tokens);
        let limit = telemetry.and_then(|value| value.context_limit);
        let percent = telemetry.and_then(|value| value.context_percent);
        let window = match (used, limit) {
            (Some(used), Some(limit)) => format!("{used} / {limit} tokens"),
            (Some(used), None) => format!("{used} tokens · window size not reported"),
            (None, Some(limit)) => format!("? / {limit} tokens"),
            (None, None) => "not reported by this engine".into(),
        };
        lines.push(format!("context  {window}"));
        if let Some(percent) = percent {
            lines.push(format!("in use   {percent:.1}%"));
        }
        if kind == "context" {
            lines.push(String::new());
            lines.push("Loading reported context details…".into());
            if let Some((Some(usage), _)) = self.memory_cache.get(&id) {
                lines.push(format!(
                    "Curated project memory: {} / {} Unicode chars (LORE cap)",
                    usage.project_chars, usage.project_cap_chars
                ));
                lines.push(format!(
                    "Curated user memory: {} / {} Unicode chars (LORE cap)",
                    usage.user_chars, usage.user_cap_chars
                ));
            }
            self.pending_queue_commands
                .push(crate::bridge::WorkerCommand::ContextDetail(id.clone()));
        }
        self.chip_info = Some(ChipInfo {
            kind,
            label: String::new(),
            lines,
            scroll: 0,
            owner: Some((id, String::new())),
        });
        if self.active_chooser_rect().is_none() {
            self.chip_info = None;
            self.notice = "Enlarge active pane to inspect session details".into();
        }
    }
}
