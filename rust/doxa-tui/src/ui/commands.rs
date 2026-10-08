//! Local command registry, prompt dispatch and keyboard-only shell execution.
use super::{
    actions, append_transcript, operations_menu, permission_index, safe_label, transcript_tail,
    transcript_tools, unsafe_input_char, App, ChipInfo, Focus, Split, ENGINE_CHOICES,
    MAX_TRANSCRIPT_BYTES, MIN_RAIL_WIDTH,
};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

/// Every reserved DOXA slash command has one identity in the registry. Unknown
/// provider and plugin commands are intentionally absent; `!` is keyboard-only.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum LocalCommand {
    Peers,
    Split,
    Vsplit,
    Diff,
    Pane,
    Movepane,
    Sidebar,
    Collection,
    Msg,
    Fleet,
    Mesh,
    RemoteConnect,
    RemoteDisconnect,
    RemoteControl,
    Local,
    Img,
    Login,
    Logout,
    Settings,
    Setup,
    Doctor,
    Plugins,
    ReloadPlugins,
    Model,
    Engine,
    Branch,
    Mode,
    Effort,
    Usage,
    Context,
    Queue,
    Clear,
    Detach,
    Attach,
    Sessions,
    Rename,
    Dir,
    Cd,
    Memory,
    Beliefs,
    Pending,
    Search,
    Resume,
    Compact,
    Update,
    Help,
    About,
}

impl LocalCommand {
    fn name(self) -> &'static str {
        COMMANDS
            .iter()
            .find(|row| row.kind == self)
            .expect("registered command")
            .name
    }
}

fn remote_local_allowed(command: LocalCommand) -> bool {
    matches!(command, LocalCommand::Help | LocalCommand::About | LocalCommand::Attach
        | LocalCommand::Sessions | LocalCommand::Split | LocalCommand::Vsplit
        | LocalCommand::Pane | LocalCommand::Sidebar | LocalCommand::Detach
        | LocalCommand::RemoteControl | LocalCommand::Local)
}

impl std::fmt::Display for LocalCommand {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.name())
    }
}

#[derive(Debug, PartialEq)]
struct ParsedCommand<'a> {
    kind: LocalCommand,
    args: &'a str,
}

impl<'a> ParsedCommand<'a> {
    fn parse(input: &'a str) -> Option<Self> {
        if input.contains('\n') {
            return None;
        }
        let line = input.trim();
        let (name, args) = line.split_once(char::is_whitespace).unwrap_or((line, ""));
        let kind = COMMANDS.iter().find(|row| row.name == name)?.kind;
        Some(Self { kind, args })
    }
}

pub(super) struct CommandHelp {
    kind: LocalCommand,
    pub(super) name: &'static str,
    pub(super) form: &'static str,
    pub(super) summary: &'static str,
    pub(super) support: &'static str,
}

// Names mirror Python 1.19's command registry. Forms and support describe
// this Rust frontend, including commands the Python frontend alone provides.
pub(super) const COMMANDS: &[CommandHelp] = &[
    CommandHelp { kind: LocalCommand::Peers, name: "/peers", form: "/peers", summary: "Peer map", support: "local" },
    CommandHelp { kind: LocalCommand::Split, name: "/split", form: "/split", summary: "Stacked pane split", support: "local" },
    CommandHelp { kind: LocalCommand::Vsplit, name: "/vsplit", form: "/vsplit", summary: "Side-by-side pane split", support: "local" },
    CommandHelp { kind: LocalCommand::Diff, name: "/diff", form: "/diff", summary: "Worktree diff", support: "local · active worktree" },
    CommandHelp { kind: LocalCommand::Pane, name: "/pane", form: "/pane [number]", summary: "Switch pane", support: "local · numbered pane groups" },
    CommandHelp { kind: LocalCommand::Movepane, name: "/movepane", form: "/movepane [number]", summary: "Move active tab", support: "local · source retains its final tab" },
    CommandHelp { kind: LocalCommand::Sidebar, name: "/sidebar", form: "/sidebar [on|off|wider|narrower|width N]", summary: "Session rail", support: "local" },
    CommandHelp { kind: LocalCommand::Collection, name: "/collection", form: "/collection [action] [name]", summary: "Organize sessions", support: "local · list/new/rename/delete/add/remove" },
    CommandHelp { kind: LocalCommand::Msg, name: "/msg", form: "/msg <peer> <text>", summary: "Message a peer", support: "local · same project" },
    CommandHelp { kind: LocalCommand::Fleet, name: "/fleet", form: "/fleet [runs|status [RUN]|stop|detach|attach [RUN] INDEX|mesh [RUN]|start OPTIONS|resume RUN]", summary: "Fleet manifests and slots", support: "local · verified slot attachment" },
    CommandHelp { kind: LocalCommand::Mesh, name: "/mesh", form: "/mesh [RUN|stop]", summary: "Browser peer graph", support: "local · private loopback ledger" },
    CommandHelp { kind: LocalCommand::RemoteConnect, name: "/remote-connect", form: "/remote-connect HUB_URL HOST_ID", summary: "Share local sessions with a private hub", support: "local · active while this window is open" },
    CommandHelp { kind: LocalCommand::RemoteDisconnect, name: "/remote-disconnect", form: "/remote-disconnect", summary: "Stop sharing local sessions", support: "local · hub presence expires after its lease" },
    CommandHelp { kind: LocalCommand::RemoteControl, name: "/remote-control", form: "/remote-control HUB_URL", summary: "Switch this terminal to remote tabs", support: "private hub · local sessions stay detached" },
    CommandHelp { kind: LocalCommand::Local, name: "/local", form: "/local", summary: "Return to local sessions", support: "remote view · restores saved local tabs" },
    CommandHelp { kind: LocalCommand::Img, name: "/img", form: "/img [path]", summary: "Image support", support: "unavailable in Rust" },
    CommandHelp { kind: LocalCommand::Login, name: "/login", form: "/login [claude|codex] [--device-auth]", summary: "Provider login", support: "local · selectable operations menu" },
    CommandHelp { kind: LocalCommand::Logout, name: "/logout", form: "/logout [claude|codex]", summary: "Provider logout", support: "local · selectable operations menu" },
    CommandHelp { kind: LocalCommand::Settings, name: "/settings", form: "/settings", summary: "Native settings", support: "local · preferences, provenance and defaults" },
    CommandHelp { kind: LocalCommand::Setup, name: "/setup", form: "/setup", summary: "Setup checks", support: "local · auth checks, LORE store, defaults" },
    CommandHelp { kind: LocalCommand::Doctor, name: "/doctor", form: "/doctor", summary: "Health checks", support: "local · selected provider" },
    CommandHelp { kind: LocalCommand::Plugins, name: "/plugins", form: "/plugins", summary: "Plugin inventory", support: "local · selectable operations menu" },
    CommandHelp { kind: LocalCommand::ReloadPlugins, name: "/reload-plugins", form: "/reload-plugins", summary: "Refresh plugins", support: "local · selectable operations menu" },
    CommandHelp { kind: LocalCommand::Model, name: "/model", form: "/model [name]", summary: "Select session model", support: "local · reported choices or new-session form" },
    CommandHelp { kind: LocalCommand::Engine, name: "/engine", form: "/engine [name]", summary: "Engine for new sessions", support: "local · new-session engine form" },
    CommandHelp { kind: LocalCommand::Branch, name: "/branch", form: "/branch [name]", summary: "Switch base branch", support: "local · active session" },
    CommandHelp { kind: LocalCommand::Mode, name: "/mode", form: "/mode [name]", summary: "Permission mode", support: "local · reported choices or new-session form" },
    CommandHelp { kind: LocalCommand::Effort, name: "/effort", form: "/effort [name]", summary: "Reasoning effort", support: "local · authoritative per-model effort choices" },
    CommandHelp { kind: LocalCommand::Usage, name: "/usage", form: "/usage", summary: "Session usage", support: "local · reported totals only" },
    CommandHelp { kind: LocalCommand::Context, name: "/context", form: "/context", summary: "Context window", support: "local · official telemetry and reported context details" },
    CommandHelp { kind: LocalCommand::Queue, name: "/queue", form: "/queue", summary: "Queued prompts", support: "local · cancel selected item with X" },
    CommandHelp { kind: LocalCommand::Clear, name: "/clear", form: "/clear", summary: "Fresh session in this tab", support: "local · idle session and writable tabset required" },
    CommandHelp { kind: LocalCommand::Detach, name: "/detach", form: "/detach", summary: "Leave session running", support: "local" },
    CommandHelp { kind: LocalCommand::Attach, name: "/attach", form: "/attach [prefix]", summary: "Attach live session", support: "local · new tab" },
    CommandHelp { kind: LocalCommand::Sessions, name: "/sessions", form: "/sessions [kill <prefix>|kill-detached]", summary: "Live sessions and stop", support: "local · exact live target or detached from this window" },
    CommandHelp { kind: LocalCommand::Rename, name: "/rename", form: "/rename [name]", summary: "Name active tab", support: "local" },
    CommandHelp { kind: LocalCommand::Dir, name: "/dir", form: "/dir", summary: "Session directory", support: "local" },
    CommandHelp { kind: LocalCommand::Cd, name: "/cd", form: "/cd <path>", summary: "Open directory", support: "local · new tab" },
    CommandHelp { kind: LocalCommand::Memory, name: "/memory", form: "/memory", summary: "LORE curated memory", support: "local · scoped entries; M to add/edit/remove" },
    CommandHelp { kind: LocalCommand::Beliefs, name: "/beliefs", form: "/beliefs", summary: "LORE beliefs", support: "local · requires LORE" },
    CommandHelp { kind: LocalCommand::Pending, name: "/pending", form: "/pending [--cluster]", summary: "LORE proposals: global and current project", support: "local · requires LORE" },
    CommandHelp { kind: LocalCommand::Pending, name: "/lore:pending", form: "/lore:pending [--cluster]", summary: "LORE pending alias", support: "local · same browser for every engine" },
    CommandHelp { kind: LocalCommand::Search, name: "/search", form: "/search [terms]", summary: "Search saved sessions", support: "local · LORE index then bounded transcript scan" },
    CommandHelp { kind: LocalCommand::Resume, name: "/resume", form: "/resume [session-id]", summary: "Resume conversation", support: "local · new tab" },
    CommandHelp { kind: LocalCommand::Compact, name: "/compact", form: "/compact", summary: "Compact transcript", support: "Claude only · completed LORE review required" },
    CommandHelp { kind: LocalCommand::Update, name: "/update", form: "/update [--restart]", summary: "Update DOXA", support: "local · reviewed install" },
    CommandHelp { kind: LocalCommand::Help, name: "/help", form: "/help", summary: "Command registry", support: "local" },
    CommandHelp { kind: LocalCommand::About, name: "/about", form: "/about", summary: "Version and active session identity", support: "local · measured installation and selected session details" },
];

/// Source-derived no-argument fleet verbs and argument forms shared by help
/// and the command palette. No controller action is dispatched by a model.
pub(super) const FLEET_ACTIONS: &[(&str, &str)] = &[
    ("/fleet start", "Prepare a pool, task and reviewed plan"),
    ("/fleet status", "Inspect current run"),
    ("/fleet attach", "Choose a current-run slot"),
    ("/fleet mesh", "Open current run graph"),
    ("/fleet stop", "Stop this window's controller"),
    (
        "/fleet detach",
        "Continue current controller outside this window",
    ),
    ("/fleet runs", "Choose saved run"),
];

impl App {
    pub(super) fn slash_suggestions(&self) -> Vec<(&str, &str)> {
        if self.focus != Focus::Prompt
            || self.slash_dismissed
            || self.active_request_index().is_some()
            || self.stop_confirmation.is_some()
            || self.chip_info.is_some()
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
            return Vec::new();
        }
        let query = self.input.as_str();
        if !query.starts_with('/') || query.chars().any(char::is_whitespace) {
            return Vec::new();
        }
        COMMANDS
            .iter()
            .filter(|row| row.name.starts_with(query))
            .map(|row| (row.name, row.summary))
            .chain(
                self.plugin_commands
                    .iter()
                    .filter(|row| row.name.starts_with(query))
                    .map(|row| (row.name.as_str(), row.summary.as_str())),
            )
            .collect()
    }

    pub(super) fn complete_slash(&mut self) -> bool {
        let matches = self.slash_suggestions();
        let Some((command, _)) =
            matches.get(self.slash_selected.min(matches.len().saturating_sub(1)))
        else {
            return false;
        };
        let command = (*command).to_owned();
        self.input = command;
        self.input_cursor = self.input.len();
        self.slash_dismissed = true;
        true
    }

    /// Handle bare DOXA commands before a prompt can reach an agent. Unknown
    /// provider and plugin commands still pass through. Known unsupported
    /// forms stay in the draft.
    pub(super) fn dispatch_prompt_command(&mut self) -> bool {
        if self.dispatch_isolation_command() { return true; }
        let Some(parsed) = ParsedCommand::parse(self.input.trim()) else {
            return false;
        };
        let command = parsed.kind;
        if self.active_remote() && !remote_local_allowed(command) {
            self.notice = "This command is available on the session host, not through the remote hub".into();
            return true;
        }
        let name = command.name();
        let args: Vec<&str> = parsed.args.split_whitespace().collect();
        if !matches!(
            command,
            LocalCommand::Help
                | LocalCommand::About
                | LocalCommand::Sessions
                | LocalCommand::Settings
                | LocalCommand::Model
                | LocalCommand::Effort
                | LocalCommand::Engine
                | LocalCommand::Mode
                | LocalCommand::Beliefs
                | LocalCommand::Diff
                | LocalCommand::Peers
                | LocalCommand::Split
                | LocalCommand::Vsplit
                | LocalCommand::Pane
                | LocalCommand::Sidebar
                | LocalCommand::Detach
                | LocalCommand::Dir
        ) {
            return false;
        }
        if !args.is_empty()
            && matches!(
                command,
                LocalCommand::Model
                    | LocalCommand::Effort
                    | LocalCommand::Mode
                    | LocalCommand::Engine
            )
        {
            return false;
        }
        if command == LocalCommand::Sessions && !args.is_empty() {
            let action = crate::sessions::Action::parse(&args);
            match action {
                Ok(action) => self.local_sessions_stop(action),
                Err(error) => self.notice = error.to_string(),
            }
            return true;
        }
        if !args.is_empty() && !matches!(command, LocalCommand::Pane | LocalCommand::Sidebar) {
            self.notice = format!("{name} arguments are not available in Rust yet");
            return true;
        }
        let pane_target = if command == LocalCommand::Pane && !args.is_empty() {
            match args.as_slice() {
                [number] => match number.parse::<usize>() {
                    Ok(index) if index > 0 && index <= self.pane_count() => Some(index - 1),
                    _ => {
                        self.notice = format!("Choose pane 1–{}", self.pane_count());
                        return true;
                    }
                },
                _ => {
                    self.notice = "Usage: /pane [number]".into();
                    return true;
                }
            }
        } else {
            None
        };
        let sidebar = if command == LocalCommand::Sidebar && !args.is_empty() {
            match args.as_slice() {
                ["on"] => Some((true, None)),
                ["off"] => Some((false, None)),
                ["wider"] => Some((true, Some(self.rail_width.saturating_add(4).min(80)))),
                ["narrower"] => Some((
                    true,
                    Some(self.rail_width.saturating_sub(4).max(MIN_RAIL_WIDTH)),
                )),
                ["width", width] => match width.parse::<u16>() {
                    Ok(width) if (MIN_RAIL_WIDTH..=80).contains(&width) => {
                        Some((true, Some(width)))
                    }
                    _ => {
                        self.notice = "Sidebar width must be 12–80 cells".into();
                        return true;
                    }
                },
                _ => {
                    self.notice = "Usage: /sidebar [on|off|wider|narrower|width N]".into();
                    return true;
                }
            }
        } else {
            None
        };
        self.input.clear();
        self.input_cursor = 0;
        match command {
            LocalCommand::Help => {
                self.open_help();
            }
            LocalCommand::About => self.open_about(),
            LocalCommand::Sessions => {
                if self.active_remote() { self.local_attach(""); } else { self.open_live_sessions(); }
            },
            LocalCommand::Settings => self.open_settings_menu(),
            LocalCommand::Model => self.open_model_picker(),
            LocalCommand::Effort => self.open_effort_picker(),
            LocalCommand::Engine => self.open_engine_picker(),
            LocalCommand::Mode => self.open_permission_picker(),
            LocalCommand::Beliefs => self.open_lore_picker(),
            LocalCommand::Diff => self.open_diff(),
            LocalCommand::Peers => {
                self.map_modal = true;
                self.peer_map.selected = 0;
                self.pending_peer_refresh = Some(
                    self.groups[self.active_group]
                        .active_id()
                        .unwrap_or("")
                        .to_owned(),
                );
            }
            LocalCommand::Split => {
                self.split_active_pane(Split::Horizontal);
            }
            LocalCommand::Vsplit => {
                self.split_active_pane(Split::Vertical);
            }
            LocalCommand::Pane => {
                if let Some(target) = pane_target {
                    self.active_group = target;
                    self.focus = Focus::Prompt;
                } else {
                    self.notice = if self.pane_group_two_exists() {
                        format!("{} pane groups · /pane <n> to focus one", self.pane_count())
                    } else {
                        "One pane group · /split or /vsplit makes a second".into()
                    };
                }
            }
            LocalCommand::Sidebar => {
                if let Some((visible, width)) = sidebar {
                    self.rail_visible = visible;
                    if let Some(width) = width {
                        self.rail_width = width;
                    }
                } else {
                    self.rail_visible = !self.rail_visible;
                }
                self.persist_sidebar();
            }
            LocalCommand::Detach => self.detach_active_tab(),
            LocalCommand::Dir => {
                self.notice = self.groups[self.active_group]
                    .active_id()
                    .and_then(|id| self.session_cwds.get(id))
                    .map(|cwd| {
                        format!("Session directory · {}", safe_label(&cwd.to_string_lossy()))
                    })
                    .unwrap_or_else(|| "Session directory unavailable".into());
            }
            _ => unreachable!("recognized bare DOXA command"),
        }
        true
    }

    // This single call site is the prompt Enter handler, never the command
    // registry, daemon frames, remote messages or model tool callbacks.
    pub(super) fn submit_keyboard_shell(&mut self) {
        if self.active_remote() { self.notice = "Local shell is unavailable in remote session mode".into(); return; }
        let Some(id) = self.groups[self.active_group]
            .active_id()
            .map(str::to_owned)
        else {
            self.notice = "Select a session before running a local shell".into();
            return;
        };
        if self.offline_ids.contains(&id) {
            self.notice = "Archived transcript is read-only".into();
            return;
        }
        let Some(cwd) = self.session_cwds.get(&id).cloned() else {
            self.notice = "Session directory unavailable".into();
            return;
        };
        let command = self.input[1..].trim().to_owned();
        if command.is_empty() {
            self.notice = "!<command> runs locally in the session directory; output is not sent to the model or saved".into();
            return;
        }
        if self.local_shell_jobs.len() >= 4 {
            self.notice = "Wait for a local shell command to finish".into();
            return;
        }
        let shell_id = self.next_shell_id;
        self.next_shell_id = self.next_shell_id.saturating_add(1);
        let initial = crate::shell::Result {
            id: shell_id,
            command: command.clone(),
            output: String::new(),
            status: "running · Ctrl+C cancel".into(),
            running: true,
            dropped_bytes: 0,
        };
        if let Some(session) = self.sessions.iter_mut().find(|session| session.id == id) {
            append_transcript(
                session,
                &format!(
                    "\n\n{}{}\n\n",
                    transcript_tools::SHELL_PREFIX,
                    serde_json::to_string(&initial).unwrap()
                ),
            );
        }
        self.local_shell_jobs
            .push(crate::shell::Job::start(id, shell_id, command, &cwd));
        self.input.clear();
        self.input_cursor = 0;
        self.notice = "Local shell running · output stays in this window".into();
    }

    pub(super) fn poll_shell(&mut self) -> bool {
        let mut changed = false;
        let mut index = 0;
        while index < self.local_shell_jobs.len() {
            if let Some(result) = self.local_shell_jobs[index].poll() {
                let job = self.local_shell_jobs.remove(index);
                if let Some(session) = self
                    .sessions
                    .iter_mut()
                    .find(|session| session.id == job.session)
                {
                    let prefix = format!("{}{{\"id\":{},", transcript_tools::SHELL_PREFIX, job.id);
                    if let Some(start) = session.transcript.find(&prefix) {
                        let end = session.transcript[start..]
                            .find("\n\n")
                            .map_or(session.transcript.len(), |end| start + end);
                        session.transcript.replace_range(
                            start..end,
                            &format!(
                                "{}{}",
                                transcript_tools::SHELL_PREFIX,
                                serde_json::to_string(&result).unwrap()
                            ),
                        );
                        if session.transcript.len() > MAX_TRANSCRIPT_BYTES {
                            session.transcript = transcript_tail(&session.transcript).to_owned();
                        }
                        changed = true;
                    }
                }
            } else {
                index += 1;
            }
        }
        changed
    }

    pub(super) fn submit_local_command(&mut self) -> bool {
        // Own the draft while dispatch mutates App; malformed known forms remain
        // in self.input until the action has actually been accepted.
        let line = self.input.clone();
        let Some(parsed) = ParsedCommand::parse(&line) else {
            return false;
        };
        let command = parsed.kind;
        if self.active_remote() && !remote_local_allowed(command) {
            self.notice = "This command is available on the session host, not through the remote hub".into();
            return true;
        }
        let args = parsed.args;
        match command {
            LocalCommand::RemoteConnect => {
                let words = args.split_whitespace().collect::<Vec<_>>();
                let [url, host] = words.as_slice() else {
                    self.notice = "Usage: /remote-connect HUB_URL HOST_ID".into();
                    return true;
                };
                if let Err(error) = crate::remote_client::hub_url(url) {
                    self.notice = format!("Remote connect: {error}");
                    return true;
                }
                if !crate::remote_client::valid_id(host) {
                    self.notice = "Remote host ID must contain only letters, digits and hyphens".into();
                    return true;
                }
                self.remote_connect_request = Some(((*url).into(), (*host).into()));
                self.input.clear();
                self.input_cursor = 0;
                self.notice = "Connecting local sessions to the private hub…".into();
                true
            }
            LocalCommand::RemoteDisconnect => {
                if !args.trim().is_empty() {
                    self.notice = "Usage: /remote-disconnect".into();
                } else {
                    self.remote_disconnect_requested = true;
                    self.input.clear();
                    self.input_cursor = 0;
                }
                true
            }
            LocalCommand::RemoteControl => {
                let words = args.split_whitespace().collect::<Vec<_>>();
                let [url] = words.as_slice() else {
                    self.notice = "Usage: /remote-control HUB_URL".into();
                    return true;
                };
                if let Err(error) = crate::remote_client::hub_url(url) {
                    self.notice = format!("Remote control: {error}");
                    return true;
                }
                self.remote_handoff = Some(super::RemoteHandoff::Hub((*url).into()));
                self.input.clear();
                self.input_cursor = 0;
                self.should_quit = true;
                true
            }
            LocalCommand::Local => {
                if !args.trim().is_empty() {
                    self.notice = "Usage: /local".into();
                } else if self.remote_mode {
                    self.remote_handoff = Some(super::RemoteHandoff::Local);
                    self.input.clear();
                    self.input_cursor = 0;
                    self.should_quit = true;
                } else if self.active_remote() {
                    let local=self.groups.iter().enumerate().find_map(|(pane,group)|group.tabs.iter()
                        .position(|id|!crate::remote_client::valid_target(id)).map(|tab|(pane,tab)));
                    if let Some((pane,tab))=local {
                        self.active_group=pane;
                        self.groups[pane].active=tab;
                        self.input.clear();self.input_cursor=0;
                        self.notice="Local tab selected".into();
                    }else{self.notice="No local tab is open".into();}
                } else {
                    self.notice = "Already viewing local sessions".into();
                }
                true
            }
            LocalCommand::Doctor if args.trim().is_empty() => {
                let engine = self.groups[self.active_group]
                    .active_id()
                    .and_then(|id| self.session_identity.get(id))
                    .and_then(|id| id.0.clone());
                self.open_operations(operations_menu::Menu::maintenance("doctor", engine, false));
                true
            }
            LocalCommand::Update if matches!(args.trim(), "" | "--restart") => {
                if self.fleet_controller.is_some()
                    || self.groups.iter().flat_map(|g| &g.tabs).any(|id| {
                        self.session_activity
                            .get(id)
                            .is_none_or(|(running, queued)| *running || *queued > 0)
                    })
                {
                    self.notice =
                        "Wait for idle sessions and an idle fleet controller before updating"
                            .into();
                    return true;
                }
                self.restart_executable = if args.trim() == "--restart" {
                    std::env::current_exe().ok()
                } else {
                    None
                };
                self.open_operations(operations_menu::Menu::maintenance(
                    "update",
                    None,
                    args.trim() == "--restart",
                ));
                true
            }
            LocalCommand::Model
            | LocalCommand::Effort
            | LocalCommand::Mode
            | LocalCommand::Engine
                if !args.trim().is_empty() =>
            {
                let target = args.trim();
                if target.split_whitespace().count() != 1
                    || target.len() > 128
                    || target.chars().any(unsafe_input_char)
                {
                    self.notice = format!("Usage: {command} <name>");
                    return true;
                }
                if command == LocalCommand::Engine {
                    if let Some(index) = ENGINE_CHOICES
                        .iter()
                        .position(|engine| engine.eq_ignore_ascii_case(target))
                    {
                        self.engine_selected = index;
                        self.select_new_engine();
                        self.input.clear();
                        self.input_cursor = 0;
                    } else {
                        self.notice =
                            "Unknown engine · choose claude, codex, deepseek or glm".into();
                    }
                    return true;
                }
                if command == LocalCommand::Mode {
                    let engine = self.groups[self.active_group].active_id()
                        .and_then(|id| self.session_identity.get(id))
                        .and_then(|identity| identity.0.as_deref());
                    if let Some(index) = permission_index(target, engine) {
                        self.open_permission_picker();
                        if let Some(picker) = &mut self.permission_picker {
                            picker.1 = index;
                            self.select_permission_mode();
                            self.input.clear();
                            self.input_cursor = 0;
                        }
                    } else {
                        self.notice =
                            "Unknown permission mode · /mode opens supported choices".into();
                    }
                    return true;
                }
                let Some(id) = self.groups[self.active_group]
                    .active_id()
                    .map(str::to_owned)
                else {
                    self.notice = "Select a session first".into();
                    return true;
                };
                self.requested_argument = Some((
                    id,
                    if command == LocalCommand::Model {
                        "model"
                    } else {
                        "effort"
                    },
                    target.into(),
                ));
                if command == LocalCommand::Model {
                    self.open_model_picker();
                } else {
                    self.open_effort_picker();
                    self.apply_requested_argument();
                }
                true
            }
            LocalCommand::Setup
            | LocalCommand::Login
            | LocalCommand::Logout
            | LocalCommand::Plugins
            | LocalCommand::ReloadPlugins
                if args.trim().is_empty()
                    || matches!(command, LocalCommand::Login | LocalCommand::Logout) =>
            {
                let kind = &command.name()[1..];
                let menu = if matches!(command, LocalCommand::Login | LocalCommand::Logout) {
                    operations_menu::Menu::with_auth_args(kind, args)
                } else if matches!(command, LocalCommand::Plugins | LocalCommand::ReloadPlugins) {
                    Ok(operations_menu::Menu::with_plugin_report(
                        command == LocalCommand::ReloadPlugins,
                    ))
                } else {
                    Ok(operations_menu::Menu::new(kind))
                };
                let menu = match menu {
                    Ok(menu) => menu,
                    Err(error) => {
                        self.notice = format!("{command}: {error}");
                        return true;
                    }
                };
                self.input.clear();
                self.input_cursor = 0;
                self.memory_menu_pending = None;
                self.memory_manager = None;
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
                } else if let Some(menu) = &mut self.operations_menu {
                    menu.start_requested();
                    if let Some(info) = &mut self.chip_info {
                        info.lines = menu.lines(usize::from(self.size.width));
                    }
                }
                true
            }
            LocalCommand::Memory if args.trim().is_empty() => {
                self.input.clear();
                self.input_cursor = 0;
                self.open_memory_menu(self.active_group);
                true
            }
            LocalCommand::Pending if args.trim().is_empty() => {
                self.input.clear();
                self.input_cursor = 0;
                self.open_pending_picker();
                true
            }
            LocalCommand::Pending if args.trim() == "--cluster" => {
                self.input.clear();
                self.input_cursor = 0;
                self.open_pending_picker();
                self.switch_lore_view(2);
                true
            }
            LocalCommand::Pending => {
                self.notice = "Local command unavailable: /pending arguments".into();
                true
            }
            LocalCommand::Attach => {
                self.local_attach(args);
                true
            }
            LocalCommand::Branch => {
                let target = args.trim();
                if target.split_whitespace().count() > 1
                    || target.len() > 200
                    || target.chars().any(unsafe_input_char)
                {
                    self.notice = "Usage: /branch [local-or-remote-name]".into();
                } else if let Some(id) = self.groups[self.active_group].active_id() {
                    self.pending_queue_commands
                        .push(crate::bridge::WorkerCommand::Branch(
                            id.to_owned(),
                            (!target.is_empty()).then(|| target.to_owned()),
                        ));
                    self.input.clear();
                    self.input_cursor = 0;
                    self.notice = "Checking branch…".into();
                } else {
                    self.notice = "Select a session before switching branch".into();
                }
                true
            }
            LocalCommand::Rename => {
                self.local_rename(args);
                true
            }
            LocalCommand::Clear => {
                self.local_clear(args);
                true
            }
            LocalCommand::Usage | LocalCommand::Context => {
                if !args.trim().is_empty() {
                    self.notice = format!("Usage: {command}");
                } else {
                    let kind = if command == LocalCommand::Usage {
                        "usage"
                    } else {
                        "context"
                    };
                    self.input.clear();
                    self.input_cursor = 0;
                    self.open_diagnostic(kind);
                }
                true
            }
            LocalCommand::Collection => {
                self.local_collection(args);
                true
            }
            LocalCommand::Cd => {
                self.local_cd(args);
                true
            }
            LocalCommand::Compact => {
                let engine = self.groups[self.active_group]
                    .active_id()
                    .and_then(|id| self.session_identity.get(id))
                    .and_then(|identity| identity.0.as_deref());
                if args.trim().is_empty() && engine == Some("claude") {
                    return false; // The Claude sidecar reviews synchronously before forwarding.
                }
                self.notice = if !args.trim().is_empty() {
                    "Usage: /compact".into()
                } else {
                    "Reviewed compaction is available only for Claude sessions".into()
                };
                true
            }
            LocalCommand::Mesh => {
                self.local_mesh(args, None);
                true
            }
            LocalCommand::Msg => {
                self.local_message(args);
                true
            }
            LocalCommand::Movepane => {
                let target = match args.split_whitespace().collect::<Vec<_>>().as_slice() {
                    [] => {
                        (self.active_group + 1)
                            % if self.pane_tree.is_some() {
                                self.pane_count()
                            } else {
                                2
                            }
                    }
                    [number]
                        if number
                            .parse::<usize>()
                            .is_ok_and(|n| n > 0 && n <= self.groups.len()) =>
                    {
                        number.parse::<usize>().unwrap() - 1
                    }
                    _ => {
                        self.notice = "Usage: /movepane [number]".into();
                        return true;
                    }
                };
                if self.move_active_tab(target) {
                    self.input.clear();
                    self.input_cursor = 0;
                }
                true
            }
            LocalCommand::Settings if args.trim().is_empty() => {
                self.input.clear();
                self.input_cursor = 0;
                self.open_settings_menu();
                true
            }
            LocalCommand::Settings => {
                self.notice = "Usage: /settings · edit native preferences in the menu".into();
                true
            }
            LocalCommand::Setup => {
                self.notice = "Usage: /setup · use the selectable setup menu".into();
                true
            }
            LocalCommand::Fleet => {
                self.local_fleet(args);
                true
            }
            LocalCommand::Img
            | LocalCommand::Login
            | LocalCommand::Logout
            | LocalCommand::Doctor
            | LocalCommand::Plugins
            | LocalCommand::ReloadPlugins
            | LocalCommand::Effort
            | LocalCommand::Update => {
                self.notice = format!("Local command unavailable: {}", safe_label(command.name()));
                true
            }
            LocalCommand::Search => {
                self.local_search(args);
                true
            }
            LocalCommand::Resume => {
                self.local_resume(args);
                true
            }
            LocalCommand::Queue if args.trim().is_empty() => {
                self.open_queue();
                true
            }
            LocalCommand::Queue => {
                self.notice = "queue: open the picker and use X to cancel a selected item".into();
                true
            }
            _ => {
                self.notice = format!("Local command unavailable: {}", safe_label(command.name()));
                true
            }
        }
    }

    pub(super) fn apply_requested_argument(&mut self) {
        let Some((owner, kind, target)) = self.requested_argument.clone() else {
            return;
        };
        if self.groups[self.active_group].active_id() != Some(owner.as_str()) {
            self.requested_argument = None;
            return;
        }
        let chosen = if kind == "model" {
            self.model_picker
                .as_ref()
                .filter(|picker| picker.session_id == owner && !picker.loading)
                .map(|picker| picker.models.iter().position(|model| model == &target))
        } else {
            self.effort_picker
                .as_ref()
                .filter(|picker| picker.session_id == owner)
                .map(|picker| picker.levels.iter().position(|level| level == &target))
        };
        let Some(chosen) = chosen else {
            return;
        };
        self.requested_argument = None;
        if let Some(index) = chosen {
            if kind == "model" {
                self.model_picker.as_mut().unwrap().selected = index;
                self.model_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            } else {
                self.effort_picker.as_mut().unwrap().selected = index;
                self.select_effort();
            }
            self.input.clear();
            self.input_cursor = 0;
        } else {
            self.notice = format!("{target} is not in this session's reported {kind} choices");
        }
    }

    pub(super) fn action_rows(&self) -> Vec<actions::Entry> {
        actions::entries(self, &self.action_query)
    }

    pub(super) fn action_key(&mut self, key: KeyEvent) -> bool {
        let rows = self.action_rows();
        match key.code {
            KeyCode::Esc | KeyCode::Char('p')
                if key.code == KeyCode::Esc || key.modifiers.contains(KeyModifiers::CONTROL) =>
            {
                self.action_menu = false
            }
            KeyCode::Up => self.action_selected = self.action_selected.saturating_sub(1),
            KeyCode::Down => {
                self.action_selected = (self.action_selected + 1).min(rows.len().saturating_sub(1))
            }
            KeyCode::PageUp => self.action_selected = self.action_selected.saturating_sub(8),
            KeyCode::PageDown => {
                self.action_selected = (self.action_selected + 8).min(rows.len().saturating_sub(1))
            }
            KeyCode::Backspace => {
                self.action_query.pop();
                self.action_selected = 0;
            }
            KeyCode::Char(c)
                if !unsafe_input_char(c)
                    && !key
                        .modifiers
                        .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
            {
                if self.action_query.len() + c.len_utf8() <= 200 {
                    self.action_query.push(c);
                    self.action_selected = 0;
                }
            }
            KeyCode::Enter => {
                let Some(entry) = rows.get(self.action_selected) else {
                    return true;
                };
                let action = entry.action.clone();
                self.action_menu = false;
                match action {
                    actions::Action::Plugin(command) => {
                        self.action_draft = Some((
                            (
                                self.active_group,
                                self.groups[self.active_group]
                                    .active_id()
                                    .unwrap_or("")
                                    .to_owned(),
                            ),
                            self.input.clone(),
                            self.input_cursor,
                        ));
                        self.input = format!("{command} ");
                        self.input_cursor = self.input.len();
                        self.focus = Focus::Prompt;
                    }
                    actions::Action::New => {
                        if self.active_remote() { self.local_attach(""); } else { self.open_engine_picker(); }
                    }
                    actions::Action::Fleet(view) => {
                        if self.active_remote() { self.notice = "Fleet controls are available on the session host".into(); }
                        else { self.open_fleet(view.root, Some(view.run_id)); }
                    }
                    actions::Action::Tab(pane, tab) => {
                        if self.groups.get(pane).is_some_and(|g| tab < g.tabs.len()) {
                            self.active_group = pane;
                            self.groups[pane].active = tab;
                            self.focus = Focus::Prompt;
                        }
                    }
                    actions::Action::Stop => self.open_stop_confirmation(),
                    actions::Action::Tools => {
                        self.tool_modal = true;
                        self.tool_scroll = 0;
                        self.tool_selected = self.active_tool_cards().len().saturating_sub(1);
                    }
                    actions::Action::Close => self.detach_active_tab(),
                    actions::Action::NextPane => {
                        self.active_group = (self.active_group + 1)
                            % if self.pane_tree.is_some() {
                                self.pane_count()
                            } else {
                                2
                            };
                        self.focus = Focus::Prompt;
                    }
                    actions::Action::Command(command) => {
                        // Argument-bearing operations prepare the user's prompt
                        // for editing; bare forms use the same local dispatcher.
                        if matches!(
                            command,
                            "/msg"
                                | "/collection"
                                | "/cd"
                                | "/fleet"
                                | "/fleet start"
                                | "/fleet attach"
                                | "/remote-connect"
                                | "/remote-control"
                                | "/img"
                        ) {
                            self.action_draft = Some((
                                (
                                    self.active_group,
                                    self.groups[self.active_group]
                                        .active_id()
                                        .unwrap_or("")
                                        .to_owned(),
                                ),
                                self.input.clone(),
                                self.input_cursor,
                            ));
                            self.input = format!("{command} ");
                            self.input_cursor = self.input.len();
                            self.focus = Focus::Prompt;
                        } else {
                            let saved = (self.input.clone(), self.input_cursor);
                            self.input = command.into();
                            self.input_cursor = self.input.len();
                            if !self.dispatch_prompt_command() {
                                self.submit_local_command();
                            }
                            // Palette commands do not consume a conversation draft.
                            self.input = saved.0;
                            self.input_cursor = saved.1;
                        }
                    }
                }
            }
            _ => return false,
        }
        true
    }

    pub(super) fn open_help(&mut self) {
        use crate::keybindings::Action as KeyAction;
        let mut lines = vec![
            "Rust DOXA commands · forms shown below".to_owned(),
            "Unavailable commands stay local; unknown provider commands pass through".to_owned(),
            String::new(),
        ];
        lines.push(
            "Tab / Shift+Tab: focus prompt, tab headers, transcript, visible chips, sidebar".into(),
        );
        lines.push(format!(
            "Focused chip: Enter opens · tab headers: ←/→ select, Enter prompt · {} next pane",
            self.keybindings.display(KeyAction::NextPaneAlternate)
        ));
        lines.push(format!(
            "{} new tab · {} close tab · {} quit · /settings → Keys to remap",
            self.keybindings.display(KeyAction::NewTab),
            self.keybindings.display(KeyAction::CloseTab),
            self.keybindings.display(KeyAction::Quit)
        ));
        lines.push(format!(
            "{} stop daemon · {} confirm DOXA transcript deletion · Delete closes a focused tab",
            self.keybindings.display(KeyAction::Stop),
            self.keybindings.display(KeyAction::DeleteTranscript),
        ));
        lines.push(format!("{} or /mode: permission picker", self.keybindings.display(KeyAction::Permission)));
        lines.push("Drag transcript text to select · Ctrl+C / Ctrl+Shift+C copy · Esc clears · Ctrl+V paste into prompt".into());
        lines.push("Terminal fallback: Shift+drag, Ctrl+Shift+C / Ctrl+Shift+V; OSC52 support required for native copy".into());
        for row in COMMANDS {
            lines.push(format!("{} · {}", row.form, row.summary));
            lines.push(format!("  {}", row.support));
        }
        for command in &self.plugin_commands {
            lines.push(format!(
                "{} · {} · {} plugin passthrough",
                if command.usage.is_empty() {
                    &command.name
                } else {
                    &command.usage
                },
                command.summary,
                command.plugin
            ));
        }
        lines.push("Local keyboard shell: !<command> · current session directory; output is neither sent nor saved".into());
        for (command, description) in FLEET_ACTIONS {
            lines.push(format!("{command} · {description}"));
        }
        self.chip_info = Some(ChipInfo {
            kind: "help",
            label: String::new(),
            lines,
            scroll: 0,
            owner: None,
        });
        if self.active_chooser_rect().is_none() {
            self.chip_info = None;
            self.notice = "Enlarge active pane to open help".into();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn enter(app: &mut App, draft: &str) {
        app.input = draft.into();
        app.input_cursor = app.input.len();
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    }

    #[test]
    fn malformed_reserved_forms_keep_the_draft_and_never_queue_provider_work() {
        let mut app = App::default();
        app.groups[0].tabs.push("session".into());
        for draft in [
            "/help extra",
            "/pane 0",
            "/pane 1 2",
            "/sidebar width 11",
            "/sidebar wider extra",
            "/model one two",
            "/effort one two",
            "/engine unknown",
            "/mode unknown",
            "/branch one two",
            "/compact extra",
            "/update --force",
            "/login codex --unexpected",
            "/pending extra",
            "/lore:pending extra",
            "/img reference.png",
            "/sessions kill one two",
            "/help\nextra",
            "/help\n",
        ] {
            enter(&mut app, draft);
            assert_eq!(app.input, draft, "consumed {draft:?}");
            assert_eq!(app.input_cursor, draft.len());
            assert!(app.pending_prompts.is_empty(), "forwarded {draft:?}");
            assert!(app.pending_queue_commands.is_empty());
            assert!(app.local_shell_jobs.is_empty());
            assert!(app.operations_menu.is_none());
        }
    }

    #[test]
    fn lore_pending_alias_is_local_for_every_engine_and_cluster_mode() {
        for engine in ["claude", "codex"] {
            let mut app = App::default();
            app.groups[0].tabs.push("session".into());
            app.session_identity.insert("session".into(), (Some(engine.into()), None));
            enter(&mut app, "/lore:pending");
            assert!(app.lore_picker.as_ref().is_some_and(|picker| picker.proposal_mode && !picker.cluster_mode));
            assert!(app.pending_prompts.is_empty());
            app.lore_picker = None;
            enter(&mut app, "/lore:pending --cluster");
            assert!(app.lore_picker.as_ref().is_some_and(|picker| picker.proposal_mode && picker.cluster_mode));
            assert!(app.pending_prompts.is_empty());
        }
    }

    #[test]
    fn unknown_provider_and_plugin_forms_preserve_exact_prompt_bytes() {
        let mut app = App::default();
        app.groups[0].tabs.push("session".into());
        for draft in [
            "/provider-command --flag value",
            " /plugin-command\tvalue  ",
            "/helpful",
            "/modelx opus",
            "/shell echo text",
            "/HELP",
            "/provider\ntext",
        ] {
            enter(&mut app, draft);
            assert_eq!(
                app.pending_prompts.pop(),
                Some(("session".into(), draft.into()))
            );
            assert!(app.input.is_empty());
            assert!(app.local_shell_jobs.is_empty());
        }
    }

    #[test]
    fn compaction_passthrough_requires_exact_claude_identity_and_bare_form() {
        for engine in [None, Some("codex"), Some("Claude"), Some("claude")] {
            let mut app = App::default();
            app.groups[0].tabs.push("session".into());
            if let Some(engine) = engine {
                app.session_identity
                    .insert("session".into(), (Some(engine.into()), None));
            }
            enter(&mut app, "/compact");
            if engine == Some("claude") {
                assert_eq!(app.pending_prompts, [("session".into(), "/compact".into())]);
            } else {
                assert!(app.pending_prompts.is_empty());
                assert_eq!(app.input, "/compact");
            }
            app.pending_prompts.clear();
            enter(&mut app, "/compact extra");
            assert!(app.pending_prompts.is_empty());
            assert_eq!(app.input, "/compact extra");
        }
    }

    #[test]
    fn local_dispatch_cannot_run_shell_syntax_or_unknown_shell_command() {
        let mut app = App::default();
        for draft in ["!echo harmless", "/shell echo harmless"] {
            app.input = draft.into();
            assert!(!app.dispatch_prompt_command());
            assert!(!app.submit_local_command());
            assert_eq!(app.input, draft);
            assert!(app.local_shell_jobs.is_empty());
        }
    }
}
