//! Model, effort, permission and new-session picker lifecycle controls.
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::mpsc::{self, TryRecvError};
use std::time::Instant;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use super::{
    effort_choices, engine_name, new_session_preferences, panes, permission_index, vendor_models,
    App, EffortPicker, ModelPicker, NewSession, ENGINE_CHOICES, MAX_INPUT_BYTES,
    PERMISSION_CHOICES,
};
use crate::launch;

impl App {
    pub(super) fn open_model_picker(&mut self) {
        if self.size.width > 0 && (self.size.width < 29 || self.size.height < 11) {
            self.notice = "Enlarge terminal to open model picker".into();
            return;
        }
        let Some(id) = self.groups[self.active_group]
            .active_id()
            .map(str::to_owned)
        else {
            self.notice = "Select a session to inspect its model".into();
            return;
        };
        if !self
            .session_capabilities
            .get(&id)
            .is_some_and(|capabilities| capabilities.live_model_switch)
        {
            self.notice = "This session cannot change models".into();
            return;
        }
        self.model_refresh_after = None;
        self.model_picker = Some(ModelPicker {
            session_id: id.clone(),
            models: Vec::new(),
            selected: 0,
            note: "Loading this engine's model catalog…".into(),
            loading: true,
            catalog_pending: false,
        });
        self.pending_model_queries.push(id);
    }

    pub(super) fn poll_model_catalog(&mut self, now: Instant) -> bool {
        let Some(picker) = self.model_picker.as_mut() else {
            self.model_refresh_after = None;
            return false;
        };
        if self.groups[self.active_group].active_id() != Some(picker.session_id.as_str()) {
            self.model_refresh_after = None;
            return false;
        }
        if picker.loading || self.model_refresh_after.is_none_or(|due| now < due) {
            return false;
        }
        self.model_refresh_after = None;
        picker.loading = true;
        self.pending_model_queries.push(picker.session_id.clone());
        true
    }

    pub(super) fn session_effort_levels(&self, id: &str, engine: &str, model: &str) -> Vec<String> {
        if let Some(levels) = self
            .session_catalogs
            .get(id)
            .and_then(|catalog| catalog.efforts(engine, model))
        {
            // An authoritative catalog that omits a model or its efforts must
            // not resurrect a measured fallback for that model.
            levels.to_vec()
        } else {
            effort_choices(engine, model)
                .iter()
                .map(|level| (*level).to_owned())
                .collect()
        }
    }

    pub(super) fn open_effort_picker(&mut self) {
        if self.size.width > 0 && (self.size.width < 29 || self.size.height < 11) {
            self.notice = "Enlarge terminal to open effort picker".into();
            return;
        }
        let Some(id) = self.groups[self.active_group]
            .active_id()
            .map(str::to_owned)
        else {
            self.notice = "Select a session to inspect its effort".into();
            return;
        };
        if let Some(effort) = self.pending_effort_verifications.get(&id) {
            self.notice = format!("Requested effort {effort} · awaiting provider verification");
            return;
        }
        let Some((Some(engine), Some(model))) = self.session_identity.get(&id) else {
            self.notice = "Effort capability is unknown for this session".into();
            return;
        };
        if matches!(engine.as_str(), "codex" | "claude")
            && !self
                .session_catalogs
                .get(&id)
                .is_some_and(|catalog| catalog.reported_for(engine))
        {
            self.effort_catalog_pending = Some((id.clone(), engine.clone(), model.clone()));
            self.model_picker = Some(ModelPicker {
                session_id: id.clone(),
                models: Vec::new(),
                selected: 0,
                note: "Loading effort capabilities for the current model…".into(),
                loading: true,
                catalog_pending: true,
            });
            self.pending_model_queries.push(id);
            self.notice = "Loading current model effort capabilities…".into();
            return;
        }
        let levels = self.session_effort_levels(&id, engine, model);
        if levels.is_empty() {
            self.notice = "Live effort change is unavailable for this session model".into();
            return;
        }
        let selected = self
            .session_efforts
            .get(&id)
            .and_then(|current| levels.iter().position(|level| level == current))
            .unwrap_or(0);
        self.effort_picker = Some(EffortPicker {
            session_id: id,
            engine: engine.clone(),
            model: model.clone(),
            levels,
            selected,
        });
    }

    pub(super) fn select_effort(&mut self) {
        let Some(picker) = self.effort_picker.take() else {
            return;
        };
        if let Some(effort) = self.pending_effort_verifications.get(&picker.session_id) {
            self.notice = format!("Requested effort {effort} · awaiting provider verification");
            return;
        }
        let Some((Some(engine), Some(model))) = self.session_identity.get(&picker.session_id)
        else {
            return;
        };
        if engine != &picker.engine || model != &picker.model {
            return;
        }
        let Some(chosen) = picker.levels.get(picker.selected) else {
            return;
        };
        let allowed = self.session_effort_levels(&picker.session_id, engine, model);
        if !allowed.contains(chosen) {
            return;
        }
        self.pending_effort_verifications
            .insert(picker.session_id.clone(), chosen.clone());
        self.pending_effort_changes
            .push((picker.session_id, chosen.clone()));
        self.notice = format!("Requesting {engine} effort {chosen} for this session…");
    }

    pub(super) fn effort_picker_key(&mut self, key: KeyEvent) -> bool {
        let Some(picker) = self.effort_picker.as_mut() else {
            return false;
        };
        match key.code {
            KeyCode::Esc => {
                self.effort_picker = None;
                self.requested_argument = None;
            }
            KeyCode::Up => picker.selected = picker.selected.saturating_sub(1),
            KeyCode::Down => {
                picker.selected = (picker.selected + 1).min(picker.levels.len().saturating_sub(1))
            }
            KeyCode::Enter => self.select_effort(),
            _ => return false,
        }
        true
    }

    pub(super) fn open_permission_picker(&mut self) {
        if self.size.width > 0 && (self.size.width < 60 || self.size.height < 15) {
            self.notice = "Enlarge terminal to open permission picker".into();
            return;
        }
        let Some(id) = self.groups[self.active_group]
            .active_id()
            .map(str::to_owned)
        else {
            self.notice = "Select a session to inspect permissions".into();
            return;
        };
        if !self
            .session_capabilities
            .get(&id)
            .is_some_and(|capabilities| capabilities.permission_modes)
        {
            self.notice = "This session cannot change permission modes".into();
            return;
        }
        let selected = self
            .permission_modes
            .get(&id)
            .and_then(|mode| permission_index(mode))
            .unwrap_or(0);
        self.permission_picker = Some((id, selected));
        self.permission_confirm_dont_ask = false;
    }

    pub(super) fn select_permission_mode(&mut self) {
        let Some((id, selected)) = self.permission_picker.as_ref() else {
            return;
        };
        if !self
            .session_capabilities
            .get(id)
            .is_some_and(|capabilities| capabilities.permission_modes)
        {
            self.notice = "This session cannot change permission modes".into();
            return;
        }
        let mode = PERMISSION_CHOICES[*selected].0;
        if mode == "dontAsk"
            && self
                .permission_modes
                .get(id)
                .is_none_or(|current| current != mode)
            && !self
                .session_activity
                .get(id)
                .is_some_and(|(busy, queued)| !busy && *queued == 0)
        {
            self.notice = "dontAsk requires an idle session with no queued prompts".into();
            return;
        }
        if mode == "dontAsk"
            && self
                .permission_modes
                .get(id)
                .is_none_or(|current| current != mode)
            && !self.permission_confirm_dont_ask
        {
            self.permission_confirm_dont_ask = true;
            self.notice =
                "dontAsk denies unapproved calls silently; press Enter again to confirm".into();
            return;
        }
        self.pending_permission_changes
            .push((id.clone(), mode.to_owned()));
        self.notice = format!("Requesting permission mode · {mode}");
        self.permission_picker = None;
        self.permission_confirm_dont_ask = false;
    }

    pub(super) fn permission_picker_key(&mut self, key: KeyEvent) -> bool {
        match key.code {
            KeyCode::Esc => {
                self.permission_picker = None;
                self.permission_confirm_dont_ask = false;
            }
            KeyCode::Up => {
                if let Some((_, selected)) = &mut self.permission_picker {
                    *selected = selected.saturating_sub(1);
                    self.permission_confirm_dont_ask = false;
                }
            }
            KeyCode::Down => {
                if let Some((_, selected)) = &mut self.permission_picker {
                    *selected = (*selected + 1).min(PERMISSION_CHOICES.len() - 1);
                    self.permission_confirm_dont_ask = false;
                }
            }
            KeyCode::Enter => self.select_permission_mode(),
            _ => return false,
        }
        true
    }

    // Keyboard and mouse selection share the same current capability gate.
    pub(super) fn select_model(&mut self) {
        let Some(picker) = self.model_picker.as_ref().filter(|picker| !picker.loading) else {
            return;
        };
        if !self
            .session_capabilities
            .get(&picker.session_id)
            .is_some_and(|capabilities| capabilities.live_model_switch)
        {
            self.notice = "This session cannot change models".into();
            return;
        }
        let Some(model) = picker.models.get(picker.selected) else {
            return;
        };
        self.pending_model_changes
            .push((picker.session_id.clone(), model.clone()));
        self.notice = format!("Requesting model · {model}");
        self.model_picker = None;
    }

    pub(super) fn model_picker_key(&mut self, key: KeyEvent) -> bool {
        let picker = self.model_picker.as_mut().unwrap();
        match key.code {
            KeyCode::Esc => {
                self.model_picker = None;
                self.effort_catalog_pending = None;
                self.requested_argument = None;
            }
            KeyCode::Up => picker.selected = picker.selected.saturating_sub(1),
            KeyCode::Down => {
                picker.selected = (picker.selected + 1).min(picker.models.len().saturating_sub(1))
            }
            KeyCode::Char('r' | 'R') if !picker.loading => {
                picker.loading = true;
                picker.catalog_pending = false;
                picker.note = "Refreshing this engine's model catalog…".into();
                self.model_refresh_after = None;
                self.pending_model_queries.push(picker.session_id.clone());
            }
            KeyCode::Enter if !picker.loading => self.select_model(),
            _ => return false,
        }
        true
    }

    pub(super) fn manual_tab_available(&mut self) -> bool {
        self.manual_tab_available_for(None)
    }

    pub(super) fn manual_tab_available_for(&mut self, candidate: Option<&str>) -> bool {
        if self
            .groups
            .iter()
            .map(|group| group.tabs.len())
            .sum::<usize>()
            + self.attaching_ids.len()
            + usize::from(self.launching)
            >= panes::MAX_TABS
        {
            self.notice = "256 tab slots occupied · detach a tab before opening another".into();
            return false;
        }
        let retained = self
            .groups
            .iter()
            .flat_map(|group| &group.tabs)
            .chain(self.detached_this_run.iter())
            .chain(self.attaching_ids.iter())
            .filter(|id| {
                !self.killed_this_run.contains(*id) && !self.clear_stop_after_save.contains(*id)
            })
            .collect::<HashSet<_>>();
        if candidate.is_none_or(|id| !retained.iter().any(|retained| retained.as_str() == id))
            && retained.len() + usize::from(self.launching) >= panes::MAX_TABS
        {
            self.notice =
                "256 retained session records · stop an old session before starting another".into();
            return false;
        }
        true
    }

    pub(super) fn open_engine_picker(&mut self) {
        if !self.manual_tab_available() {
            return;
        }
        if self.size.width > 0 && (self.size.width < 29 || self.size.height < 11) {
            self.notice = "Enlarge terminal to open engine picker".into();
            return;
        }
        let engine = self.groups[self.active_group]
            .active_id()
            .and_then(|id| self.session_identity.get(id))
            .and_then(|identity| identity.0.as_deref());
        let configured = engine_name(launch::configured_engine());
        self.engine_selected = engine
            .and_then(|engine| ENGINE_CHOICES.iter().position(|name| *name == engine))
            .or_else(|| ENGINE_CHOICES.iter().position(|name| *name == configured))
            .unwrap_or(1);
        self.engine_picker = true;
    }

    pub(super) fn engine_picker_key(&mut self, key: KeyEvent) -> bool {
        match key.code {
            KeyCode::Esc => self.engine_picker = false,
            KeyCode::Up => self.engine_selected = self.engine_selected.saturating_sub(1),
            KeyCode::Down => {
                self.engine_selected = (self.engine_selected + 1).min(ENGINE_CHOICES.len() - 1)
            }
            KeyCode::Enter => {
                self.select_new_engine();
            }
            _ => return false,
        }
        true
    }

    pub(super) fn select_new_engine(&mut self) {
        if !self.manual_tab_available() {
            self.engine_picker = false;
            return;
        }
        let engine = match self.engine_selected {
            0 => launch::Engine::Codex,
            1 => launch::Engine::Claude,
            2 => launch::Engine::DeepSeek,
            _ => launch::Engine::Glm,
        };
        self.engine_picker = false;
        let models = vendor_models(engine);
        let engine_id = engine_name(engine);
        let config = crate::settings::config_path()
            .map(|path| doxa_state::load_config(&path))
            .unwrap_or_default();
        let (model, configured_effort) = new_session_preferences(
            engine,
            &config,
            std::env::var("DOXA_MODEL").ok().as_deref(),
            std::env::var("DOXA_EFFORT").ok().as_deref(),
        );
        // A previous account-scoped catalog must not survive a vendor
        // re-selection when a later lookup fails or the credential changes.
        let model_efforts = models
            .iter()
            .map(|name| {
                (
                    (*name).to_owned(),
                    effort_choices(engine_id, name)
                        .iter()
                        .map(|level| (*level).to_owned())
                        .collect(),
                )
            })
            .collect::<HashMap<_, _>>();
        let effort = self
            .next_efforts
            .get(engine_id)
            .cloned()
            .or(configured_effort)
            .filter(|level| {
                models.is_empty() || effort_choices(engine_id, &model).contains(&level.as_str())
            })
            .or_else(|| (!models.is_empty()).then(|| "high".to_owned()));
        self.vendor_catalog_pending = None;
        let mut catalog_pending = false;
        if let Some(vendor) = match engine {
            launch::Engine::DeepSeek => Some(doxa_vendors::Vendor::DeepSeek),
            launch::Engine::Glm => Some(doxa_vendors::Vendor::Glm),
            _ => None,
        } {
            if !cfg!(test) && std::env::var(vendor.env_var()).is_ok_and(|key| !key.is_empty()) {
                let (tx, rx) = mpsc::sync_channel(1);
                std::thread::spawn(move || {
                    let result = tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build()
                        .ok()
                        .and_then(|runtime| runtime.block_on(doxa_vendors::catalog_models(vendor)));
                    let _ = tx.send(result);
                });
                self.vendor_catalog_pending = Some((engine, rx));
                catalog_pending = true;
            }
        }
        self.new_session = Some(NewSession {
            engine,
            model,
            models: models.iter().map(|name| (*name).to_owned()).collect(),
            model_efforts,
            catalog_note: if catalog_pending {
                "Checking vendor model catalog…".into()
            } else {
                "Static fallback; vendor catalog unavailable".into()
            },
            catalog_pending,
            launch_error: None,
            retry_allowed: true,
            effort,
            prompt: String::new(),
            field: 0,
        });
    }

    pub(super) fn poll_vendor_catalog(&mut self) -> bool {
        let result = match self.vendor_catalog_pending.as_ref() {
            Some((engine, rx)) => match rx.try_recv() {
                Ok(result) => Some((*engine, result)),
                Err(TryRecvError::Disconnected) => Some((*engine, None)),
                Err(TryRecvError::Empty) => None,
            },
            None => None,
        };
        let Some((engine, result)) = result else {
            return false;
        };
        self.vendor_catalog_pending = None;
        let Some(form) = self
            .new_session
            .as_mut()
            .filter(|form| form.engine == engine)
        else {
            return false;
        };
        form.catalog_pending = false;
        if let Some(live) = result {
            let vetted = vendor_models(engine);
            let mut model_efforts = HashMap::new();
            let mut defaults = HashMap::new();
            let mut metadata_count = 0;
            for row in live {
                let known = vetted.contains(&row.id.as_str());
                let mut levels = if !row.effort_metadata_present && known {
                    effort_choices(engine_name(engine), &row.id)
                        .iter()
                        .map(|level| (*level).to_owned())
                        .collect::<Vec<_>>()
                } else {
                    row.efforts.clone()
                };
                if row.effort_metadata_present && !row.efforts.is_empty() {
                    metadata_count += 1;
                    // The DeepSeek catalogue omits `none`, which disables thinking;
                    // only the measured legacy models may offer it.
                    if engine == launch::Engine::DeepSeek
                        && known
                        && !levels.iter().any(|level| level == "none")
                    {
                        levels.insert(0, "none".into());
                    }
                }
                if !levels.is_empty() {
                    if let Some(default) = row.default_effort {
                        defaults.insert(row.id.clone(), default);
                    }
                    model_efforts.insert(row.id, levels);
                }
            }
            form.models = model_efforts.keys().cloned().collect();
            form.models.sort();
            form.model_efforts = model_efforts;
            form.catalog_note = if form.models.is_empty() {
                "Live catalog has no models with verified effort support; choose another engine or retry later".into()
            } else if metadata_count > 0 {
                "Live vendor catalog · per-model effort where available; known models use measured fallback".into()
            } else {
                "Live vendor catalog · measured effort fallback for known models".into()
            };
            if !form.models.contains(&form.model) {
                form.model = form.models.first().cloned().unwrap_or_default();
            }
            let levels = form
                .model_efforts
                .get(&form.model)
                .cloned()
                .unwrap_or_default();
            if form
                .effort
                .as_ref()
                .is_none_or(|level| !levels.contains(level))
            {
                form.effort = defaults
                    .get(&form.model)
                    .cloned()
                    .or_else(|| levels.iter().find(|level| *level == "high").cloned())
                    .or_else(|| levels.first().cloned());
            }
        } else {
            form.catalog_note = "Static fallback; vendor catalog unavailable".into();
        }
        true
    }

    pub(super) fn retain_launch_failure(&mut self) {
        if let Some(form) = self.new_session.as_mut() {
            form.launch_error = Some(self.notice.clone());
        }
        if self.sessions.is_empty() {
            self.awaiting_initial_attach = false;
            self.startup_recovery = Some(self.notice.clone());
        }
    }

    pub(super) fn new_session_key(&mut self, key: KeyEvent) -> bool {
        if self.launching {
            if key.code == KeyCode::Esc {
                self.new_session = None;
            }
            return true;
        }
        let form = self.new_session.as_mut().unwrap();
        let vendor = !vendor_models(form.engine).is_empty();
        let fields = if vendor { 3 } else { 2 };
        let prompt_field = fields - 1;
        match key.code {
            KeyCode::Esc => self.new_session = None,
            KeyCode::Tab | KeyCode::Down => form.field = (form.field + 1) % fields,
            KeyCode::BackTab | KeyCode::Up => form.field = (form.field + fields - 1) % fields,
            KeyCode::Left | KeyCode::Right if vendor && form.field <= 1 => {
                if form.field == 0 {
                    let choices = &form.models;
                    if choices.is_empty() {
                        return true;
                    }
                    let current = choices
                        .iter()
                        .position(|model| *model == form.model)
                        .unwrap_or(0);
                    let next = if key.code == KeyCode::Right {
                        (current + 1) % choices.len()
                    } else {
                        (current + choices.len() - 1) % choices.len()
                    };
                    form.model = choices[next].clone();
                    // Discard an effort no longer supported by the new model.
                    let levels = form
                        .model_efforts
                        .get(&form.model)
                        .cloned()
                        .unwrap_or_default();
                    if form
                        .effort
                        .as_ref()
                        .is_none_or(|level| !levels.contains(level))
                    {
                        form.effort = levels
                            .iter()
                            .find(|level| *level == "high")
                            .cloned()
                            .or_else(|| levels.first().cloned());
                    }
                } else {
                    let levels = form
                        .model_efforts
                        .get(&form.model)
                        .map(Vec::as_slice)
                        .unwrap_or(&[]);
                    if levels.is_empty() {
                        form.effort = None;
                        return true;
                    }
                    let current = form
                        .effort
                        .as_deref()
                        .and_then(|level| levels.iter().position(|x| *x == level))
                        .unwrap_or(0);
                    let next = if key.code == KeyCode::Right {
                        (current + 1) % levels.len()
                    } else {
                        (current + levels.len() - 1) % levels.len()
                    };
                    form.effort = Some(levels[next].clone());
                }
            }
            KeyCode::Backspace => {
                if form.field == prompt_field {
                    form.prompt.pop();
                } else if !vendor && form.field == 0 {
                    form.model.pop();
                }
            }
            KeyCode::Char(c)
                if !key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                    && !c.is_control() =>
            {
                if form.field == prompt_field {
                    if form.prompt.len() + c.len_utf8() <= MAX_INPUT_BYTES {
                        form.prompt.push(c);
                    }
                } else if !vendor && form.field == 0 && form.model.len() + c.len_utf8() <= 128 {
                    form.model.push(c);
                }
            }
            KeyCode::Enter if form.field < prompt_field => form.field += 1,
            KeyCode::Enter => {
                if self.launching {
                    self.notice = "Session launch already in progress".into();
                    return true;
                }
                if vendor && form.catalog_pending {
                    self.notice = "Waiting for vendor model catalog".into();
                    return true;
                }
                if vendor && form.models.is_empty() {
                    self.notice = "No verified models with effort capability are available".into();
                    return true;
                }
                if !form.retry_allowed {
                    return true;
                }
                if !self.manual_tab_available() {
                    return true;
                }
                let form = self.new_session.as_ref().unwrap().clone();
                let mut options = launch::LaunchOptions {
                    engine: form.engine,
                    ..Default::default()
                };
                if !form.model.trim().is_empty() {
                    options.model = Some(form.model.trim().to_owned());
                }
                if vendor {
                    let allowed = form
                        .model_efforts
                        .get(&form.model)
                        .map(Vec::as_slice)
                        .unwrap_or(&[]);
                    if !form.models.contains(&form.model)
                        || form
                            .effort
                            .as_ref()
                            .is_none_or(|level| !allowed.contains(level))
                    {
                        self.notice =
                            "Model or effort capability changed; session was not started".into();
                        return true;
                    }
                    options.effort = form.effort;
                }
                if form.engine == launch::Engine::Claude {
                    options.claude_bin =
                        std::env::var_os("DOXA_CLAUDE_BIN").map(PathBuf::from);
                }
                let prompt = if form.prompt.trim().is_empty() {
                    None
                } else {
                    Some(form.prompt)
                };
                self.pending_launches
                    .push((options, prompt, self.active_group));
                self.new_session.as_mut().unwrap().launch_error = None;
                self.launching = true;
                self.notice = format!("Starting {} session…", engine_name(form.engine));
            }
            _ => return false,
        }
        true
    }
}
