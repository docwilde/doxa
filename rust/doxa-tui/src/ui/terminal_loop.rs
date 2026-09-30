//! Own terminal modes, scheduling, bounded command queues and layout persistence.
use super::{links, safe_label, App};
use crossterm::event::{
    self, DisableBracketedPaste, DisableFocusChange, DisableMouseCapture, EnableBracketedPaste,
    EnableFocusChange, EnableMouseCapture,
};
use crossterm::execute;
use crossterm::terminal::{self, EnterAlternateScreen, LeaveAlternateScreen};
use ratatui::{backend::CrosstermBackend, layout::Rect, Terminal};
use std::io::{self, IsTerminal, Stdout, Write};
use std::sync::mpsc::{self, Receiver, SyncSender, TryRecvError, TrySendError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Legacy wire consumers and owned worker results retain their original
/// channels, without a relay thread or a second queue.
enum FrameSource {
    Wire(Receiver<serde_json::Value>),
    Worker(Receiver<crate::worker_frames::WorkerFrame>),
}

impl FrameSource {
    fn try_reduce(&self, app: &mut App) -> Result<bool, TryRecvError> {
        match self {
            Self::Wire(receiver) => receiver
                .try_recv()
                .map(|frame| app.apply_daemon_frame(&frame)),
            Self::Worker(receiver) => receiver
                .try_recv()
                .map(|frame| app.apply_worker_frame(frame)),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum KeyboardProtocol {
    Legacy,
    Kitty,
    Unknown,
}
pub(super) fn selected_keyboard_protocol(override_value: Option<&str>) -> KeyboardProtocol {
    // Startup never asks the terminal to respond before its first visible frame.
    // Legacy is a selected compatibility mode, not measured lack of support.
    match override_value {
        Some("kitty") => KeyboardProtocol::Kitty,
        Some("unknown") => KeyboardProtocol::Unknown,
        _ => KeyboardProtocol::Legacy,
    }
}

/// Owns terminal modes so every return path, including I/O errors, restores the screen.
struct TerminalGuard {
    out: Stdout,
    raw: bool,
    alternate: bool,
    mouse: bool,
    paste: bool,
    keyboard: bool,
    keyboard_protocol: KeyboardProtocol,
}
impl TerminalGuard {
    fn enter() -> io::Result<Self> {
        let mut guard = Self {
            out: io::stdout(),
            raw: false,
            alternate: false,
            mouse: false,
            paste: false,
            keyboard: false,
            keyboard_protocol: KeyboardProtocol::Legacy,
        };
        terminal::enable_raw_mode()?;
        guard.raw = true;
        guard.keyboard_protocol =
            selected_keyboard_protocol(std::env::var("DOXA_KEYBOARD_PROTOCOL").ok().as_deref());
        if guard.keyboard_protocol == KeyboardProtocol::Kitty {
            execute!(
                guard.out,
                crossterm::event::PushKeyboardEnhancementFlags(
                    crossterm::event::KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES
                )
            )?;
            guard.keyboard = true;
        }
        execute!(guard.out, EnterAlternateScreen)?;
        guard.alternate = true;
        execute!(guard.out, EnableMouseCapture, EnableFocusChange)?;
        guard.mouse = true;
        execute!(guard.out, EnableBracketedPaste)?;
        guard.paste = true;
        Ok(guard)
    }
}
impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = self.out.write_all(pointer_shape(false));
        let _ = self.out.flush();
        if self.paste {
            let _ = execute!(self.out, DisableBracketedPaste);
        }
        if self.mouse {
            let _ = execute!(self.out, DisableMouseCapture, DisableFocusChange);
        }
        if self.alternate {
            let _ = execute!(self.out, LeaveAlternateScreen);
        }
        if self.keyboard {
            let _ = execute!(self.out, crossterm::event::PopKeyboardEnhancementFlags);
        }
        if self.raw {
            let _ = terminal::disable_raw_mode();
        }
    }
}

/// OSC 22 is a no-op in terminals without pointer-shape support.
pub(super) fn pointer_shape(link: bool) -> &'static [u8] {
    if link {
        b"\x1b]22;pointer\x1b\\"
    } else {
        b"\x1b]22;\x1b\\"
    }
}

pub(super) fn open_link(url: &str) -> io::Result<()> {
    if !links::safe_url(url) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "unsupported link",
        ));
    }
    crate::operations::open_browser(url)
}

pub fn run() -> io::Result<()> {
    let (_sender, receiver) = mpsc::channel();
    run_with_frames(receiver)
}

/// Drive the terminal with decoded daemon frames supplied by a reader thread.
/// Transport can be connected without changing terminal ownership or drawing.
pub fn run_with_frames(receiver: Receiver<serde_json::Value>) -> io::Result<()> {
    run_loop(FrameSource::Wire(receiver), None, None)
}

/// Connect the UI to a transport reader and writer without blocking input.
/// Prompt tuples contain the target session id and submitted text.
pub fn run_with_channels(
    frames: Receiver<serde_json::Value>,
    prompts: SyncSender<crate::bridge::WorkerCommand>,
) -> io::Result<()> {
    run_loop(FrameSource::Wire(frames), Some(prompts), None)
}

/// Drive a multi-session transport with a complete live-ID roster and a
/// tabset store. The store writes only when layout state changes.
pub fn run_with_channels_state(
    frames: Receiver<serde_json::Value>,
    prompts: SyncSender<crate::bridge::WorkerCommand>,
    store: crate::ui_state::UiStateStore,
    live_ids: Vec<String>,
) -> io::Result<()> {
    run_with_channels_state_guarded(frames, prompts, store, live_ids, Arc::new(Mutex::new(true)))
}

pub fn run_with_channels_state_guarded(
    frames: Receiver<serde_json::Value>,
    prompts: SyncSender<crate::bridge::WorkerCommand>,
    store: crate::ui_state::UiStateStore,
    live_ids: Vec<String>,
    complete_roster: Arc<Mutex<bool>>,
) -> io::Result<()> {
    run_loop(
        FrameSource::Wire(frames),
        Some(prompts),
        Some((store, live_ids, complete_roster)),
    )
}

/// Drive the native bridge using owned, typed worker results.
pub fn run_with_worker_channels(
    frames: Receiver<crate::worker_frames::WorkerFrame>,
    prompts: SyncSender<crate::bridge::WorkerCommand>,
) -> io::Result<()> {
    run_loop(FrameSource::Worker(frames), Some(prompts), None)
}

pub fn run_with_worker_channels_state_guarded(
    frames: Receiver<crate::worker_frames::WorkerFrame>,
    prompts: SyncSender<crate::bridge::WorkerCommand>,
    store: crate::ui_state::UiStateStore,
    live_ids: Vec<String>,
    complete_roster: Arc<Mutex<bool>>,
) -> io::Result<()> {
    run_loop(
        FrameSource::Worker(frames),
        Some(prompts),
        Some((store, live_ids, complete_roster)),
    )
}

fn run_loop(
    receiver: FrameSource,
    mut prompt_sender: Option<SyncSender<crate::bridge::WorkerCommand>>,
    mut state: Option<(crate::ui_state::UiStateStore, Vec<String>, Arc<Mutex<bool>>)>,
) -> io::Result<()> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        return Err(io::Error::new(
            io::ErrorKind::NotConnected,
            "DOXA requires an interactive terminal",
        ));
    }
    let guard = TerminalGuard::enter()?;
    let mut terminal = Terminal::new(CrosstermBackend::new(&guard.out))?;
    let mut app = App {
        size: terminal
            .size()
            .map(|s| Rect::new(0, 0, s.width, s.height))?,
        ..Default::default()
    };
    app.persist_preferences = true;
    app.plugin_refresh_dirty = true;
    app.sidebar_auto = app.preferences.value("sidebar").is_empty();
    app.rail_width = app.preferences.sidebar_width();
    app.rail_visible = match app.preferences.value("sidebar") {
        "" => app.sessions.len() > 1 || !app.collections.is_empty(),
        "0" | "false" | "off" | "no" => false,
        _ => true,
    };
    app.awaiting_initial_attach = prompt_sender.is_some();
    if let Some((store, live_ids, _)) = &state {
        app.awaiting_initial_attach = !live_ids.is_empty();
        if live_ids.is_empty() && store.startup_failed {
            app.startup_recovery = store
                .startup_error
                .clone()
                .or_else(|| Some("Provider startup failed. Check setup, then retry.".into()));
        }
        store.restore(&mut app, live_ids);
    }
    match crate::keybindings::Bindings::load() {
        Ok(bindings) => app.keybindings = bindings,
        Err(error) => app.notice = format!("Invalid keybindings: {error}"),
    }
    app.refresh_clock();
    if app.preferences.on("key_notice") && guard.keyboard_protocol == KeyboardProtocol::Legacy {
        let keys = format!("Legacy key mode: {} → /settings · Shift+Enter → Ctrl+J · Ctrl+Enter → /msg",
            app.keybindings.display(crate::keybindings::Action::Settings));
        if app.notice.is_empty() {
            app.notice = keys;
        } else {
            app.notice.push_str(" · ");
            app.notice.push_str(&keys);
        }
    }
    let mut saved_layout = crate::ui_state::LayoutSignature::capture(&app);
    terminal.draw(|frame| app.draw(frame))?;
    let mut pointer_on_link = false;
    let mut first_run_pending = crate::first_run::needed();
    let mut installation_worker = crate::installation::Worker::start().ok();
    if installation_worker.is_none() {
        app.installation.update = crate::installation::Update::Unknown;
    }
    while !app.should_quit {
        let mut changed = false;
        if first_run_pending && app.offer_first_run() {
            first_run_pending = false;
            changed = true;
        }
        if let Some(snapshot) = installation_worker
            .as_ref()
            .and_then(crate::installation::Worker::poll)
        {
            app.apply_installation(snapshot);
            installation_worker = None;
            changed = true;
        }
        // Bound work per tick so a busy daemon cannot starve keyboard input.
        for _ in 0..64 {
            match receiver.try_reduce(&mut app) {
                Ok(reduced) => changed |= reduced,
                Err(TryRecvError::Empty | TryRecvError::Disconnected) => break,
            }
        }
        // A frame is ready now. Paint before waiting for terminal input so a
        // daemon update never waits through an otherwise idle input poll.
        if changed {
            terminal.draw(|frame| app.draw(frame))?;
            changed = false;
        }
        app.clear_preflight_error = state.as_ref().map_or(
            Some("persistent tabset unavailable"),
            |(store, _, complete)| store.clear_preflight(&app, complete).err(),
        );
        if event::poll(Duration::from_millis(10))? {
            changed |= app.handle(event::read()?);
        }
        let next_pointer = app.pointer_on_link();
        if next_pointer != pointer_on_link {
            let mut out = io::stdout();
            out.write_all(pointer_shape(next_pointer))?;
            out.flush()?;
            pointer_on_link = next_pointer;
        }
        for url in std::mem::take(&mut app.pending_open_urls) {
            if open_link(&url).is_err() {
                app.notice = "Could not open link in browser".into();
                changed = true;
            }
        }
        changed |= app.poll_sessions_roster();
        changed |= app.poll_stale_detached(Instant::now());
        changed |= app.poll_sessions_stop();
        changed |= app.poll_fleet();
        changed |= app.poll_diff();
        changed |= app.poll_auto_diff();
        changed |= app.poll_history();
        changed |= app.poll_resume();
        changed |= app.poll_belief_filter(Instant::now());
        changed |= app.poll_lore();
        changed |= app.poll_belief_graph();
        changed |= app.poll_memory();
        changed |= app.poll_repo();
        changed |= app.poll_memory_menu();
        changed |= app.poll_shell();
        changed |= app.poll_clipboard();
        if let Some(bytes) = app.pending_clipboard_copy.take() {
            let mut out = io::stdout();
            app.notice = if out.write_all(&bytes).and_then(|_| out.flush()).is_err() {
                "Clipboard copy failed · use terminal selection and Ctrl+Shift+C"
            } else {
                "Selection sent to terminal clipboard · OSC52 support required"
            }
            .into();
            changed = true;
        }
        changed |= app.poll_plugin_commands();
        changed |= app.poll_vendor_catalog();
        changed |= app.poll_model_catalog(Instant::now());
        changed |= app.tick_blink(Instant::now());
        changed |= app.tick_spinner(Instant::now());
        changed |= app.tick_clock(Instant::now());
        changed |= app.tick_chip_hover(Instant::now());
        changed |= app.tick_belief_preview(Instant::now());
        changed |= app.peer_map.tick(Instant::now());
        if prompt_sender.is_none() {
            if let Some(id) = app.pending_peer_refresh.take() {
                changed |= app.peer_map.roster(&id, &serde_json::json!({"ok":false}));
            }
            if !app.pending_launches.is_empty() {
                app.pending_launches.clear();
                app.launching = false;
                app.clear_pending = None;
                app.notice = "Session launch unavailable · daemon connection closed".into();
                app.retain_launch_failure();
                changed = true;
            }
            if !app.pending_attaches.is_empty() {
                app.pending_attaches.clear();
                app.attaching_ids.clear();
                app.notice = "Session attach unavailable · daemon connection closed".into();
                changed = true;
            }
            if !app.pending_stops.is_empty() {
                app.pending_stops.clear();
                app.notice = "Session stop unavailable · daemon connection closed".into();
                changed = true;
            }
            if !app.clear_stop_after_save.is_empty() {
                app.clear_stop_after_save.clear();
                app.notice =
                    "Previous session could not be finalized · daemon connection closed".into();
                changed = true;
            }
            if !app.pending_clear_finalizes.is_empty() {
                app.pending_clear_finalizes.clear();
                app.notice =
                    "Previous session could not be finalized · daemon connection closed".into();
                changed = true;
            }
            if !app.pending_queue_commands.is_empty() {
                app.pending_queue_commands.clear();
                app.queue_picker = None;
                app.notice = "Queue unavailable · daemon connection closed".into();
                changed = true;
            }
            if !app.pending_peer_messages.is_empty() {
                for (id, target, text) in app.pending_peer_messages.drain(..) {
                    app.rejected_drafts
                        .entry(id)
                        .or_default()
                        .push(format!("/msg {target} {text}"));
                }
                app.notice = "Peer delivery unavailable · Alt+Up restores message".into();
                changed = true;
            }
        }
        if let Some(sender) = &prompt_sender {
            let disconnected = dispatch_launches(&mut app, sender);
            let disconnected = dispatch_attaches(&mut app, sender) || disconnected;
            let disconnected = dispatch_prompts(&mut app, sender) || disconnected;
            let disconnected = dispatch_answers(&mut app, sender) || disconnected;
            let disconnected = dispatch_peer_refresh(&mut app, sender) || disconnected;
            let disconnected = dispatch_peer_messages(&mut app, sender) || disconnected;
            let disconnected = dispatch_model_controls(&mut app, sender) || disconnected;
            let disconnected = dispatch_queue_commands(&mut app, sender) || disconnected;
            if disconnected {
                prompt_sender = None;
                app.session_activity.clear();
                changed = true;
            }
        }
        // Retry an unsaved layout on later ticks, including ticks with no new
        // UI event (for example when the live roster becomes complete).
        if let Some((store, _, complete)) = &mut state {
            if let Err(error) = store.forget_sessions(&app.killed_this_run) {
                app.notice = format!(
                    "sessions: stopped; tabset veto save failed · {}",
                    safe_label(&error.to_string())
                );
                changed = true;
            }
            changed |= save_layout_if_changed(&mut app, store, complete, &mut saved_layout);
        } else {
            saved_layout = crate::ui_state::LayoutSignature::capture(&app);
        }
        changed |= app.finish_clear_swap(
            state.is_some() && saved_layout == crate::ui_state::LayoutSignature::capture(&app),
        );
        if let Some(sender) = &prompt_sender {
            if dispatch_stops(&mut app, sender) || dispatch_clear_finalizes(&mut app, sender) {
                prompt_sender = None;
                changed = true;
            }
        }
        changed |= app.restart_after_install(&mut state);
        if changed {
            terminal.draw(|frame| app.draw(frame))?;
        }
    }
    drop(terminal);
    drop(guard);
    if app.restart_after_update {
        if let Some(executable) = app.restart_executable.take() {
            // Capture before update replaces the binary. Owned idle daemons
            // have finalized; startup restores only verified provider state.
            app.window_mesh = None;
            app.local_shell_jobs.clear();
            use std::os::unix::process::CommandExt;
            return Err(std::process::Command::new(executable).exec());
        }
    }
    Ok(())
}

pub(super) fn dispatch_launches(
    app: &mut App,
    sender: &SyncSender<crate::bridge::WorkerCommand>,
) -> bool {
    let mut launches = std::mem::take(&mut app.pending_launches).into_iter();
    while let Some((options, prompt, group)) = launches.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Launch(options, prompt, group)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Launch(
                options,
                prompt,
                group,
            ))) => {
                app.pending_launches
                    .extend(std::iter::once((options, prompt, group)).chain(launches));
                return false;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.attaching_ids.clear();
                app.launching = false;
                app.clear_pending = None;
                app.notice = "Session launch unavailable".into();
                app.retain_launch_failure();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

pub(super) fn dispatch_attaches(
    app: &mut App,
    sender: &SyncSender<crate::bridge::WorkerCommand>,
) -> bool {
    let mut attaches = std::mem::take(&mut app.pending_attaches).into_iter();
    while let Some((id, group)) = attaches.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Attach(id, group)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Attach(id, group))) => {
                app.pending_attaches
                    .extend(std::iter::once((id, group)).chain(attaches));
                return false;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.attaching_ids.clear();
                app.notice = "Session attach unavailable".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

pub(super) fn dispatch_stops(
    app: &mut App,
    sender: &SyncSender<crate::bridge::WorkerCommand>,
) -> bool {
    let mut stops = std::mem::take(&mut app.pending_stops).into_iter();
    while let Some(id) = stops.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Stop(id)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Stop(id))) => {
                app.pending_stops.extend(std::iter::once(id).chain(stops));
                app.notice = "Daemon writer busy · stop request retained".into();
                return false;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.notice = "Session stop unavailable · daemon connection closed".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

pub(super) fn dispatch_clear_finalizes(
    app: &mut App,
    sender: &SyncSender<crate::bridge::WorkerCommand>,
) -> bool {
    let mut pending = std::mem::take(&mut app.pending_clear_finalizes).into_iter();
    while let Some(id) = pending.next() {
        match sender.try_send(crate::bridge::WorkerCommand::FinalizeForClear(id)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::FinalizeForClear(id))) => {
                app.pending_clear_finalizes
                    .extend(std::iter::once(id).chain(pending));
                return false;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.notice =
                    "Previous session could not be finalized · daemon connection closed".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

pub(super) fn dispatch_queue_commands(
    app: &mut App,
    sender: &SyncSender<crate::bridge::WorkerCommand>,
) -> bool {
    let mut commands = std::mem::take(&mut app.pending_queue_commands).into_iter();
    while let Some(command) = commands.next() {
        match sender.try_send(command) {
            Ok(()) => {}
            Err(TrySendError::Full(command)) => {
                app.pending_queue_commands
                    .extend(std::iter::once(command).chain(commands));
                return false;
            }
            Err(TrySendError::Disconnected(_)) => {
                app.queue_picker = None;
                app.notice = "Queue unavailable · daemon connection closed".into();
                return true;
            }
        }
    }
    false
}

pub(super) fn save_layout_if_changed(
    app: &mut App,
    store: &mut crate::ui_state::UiStateStore,
    complete: &Mutex<bool>,
    saved_layout: &mut crate::ui_state::LayoutSignature,
) -> bool {
    let layout = crate::ui_state::LayoutSignature::capture(app);
    if layout == *saved_layout {
        if !app.has_unverified_archived_tabs() && app.notice.starts_with("Layout save skipped ·") {
            app.notice.clear();
            return true;
        }
        return false;
    }
    if app.has_unverified_archived_tabs() {
        if app.notice != "Layout save skipped · unverified archived tabs" {
            app.notice = "Layout save skipped · unverified archived tabs".into();
            return true;
        }
        return false;
    }
    let notice = match store.save_if_complete(app, complete) {
        Ok(true) => {
            *saved_layout = layout;
            if app.notice.starts_with("Layout save skipped ·") {
                app.notice.clear();
                return true;
            }
            return false;
        }
        Ok(false) => "Layout save skipped · live roster incomplete".into(),
        Err(error) => format!("Layout save skipped · {}", safe_label(&error.to_string())),
    };
    if app.notice == notice {
        false
    } else {
        app.notice = notice;
        true
    }
}

/// Move only as many prompts as the bounded worker queue can accept, keeping
/// the rest in their original order for the next UI tick.
pub(super) fn dispatch_prompts(
    app: &mut App,
    sender: &SyncSender<crate::bridge::WorkerCommand>,
) -> bool {
    let mut prompts = app.take_prompts().into_iter();
    while let Some(prompt) = prompts.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Prompt(prompt.0, prompt.1)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Prompt(id, text))) => {
                let prompt = (id, text);
                app.pending_prompts
                    .extend(std::iter::once(prompt).chain(prompts));
                app.notice = "Daemon writer busy · prompt retained".into();
                return false;
            }
            Err(TrySendError::Disconnected(crate::bridge::WorkerCommand::Prompt(id, text))) => {
                let prompt = (id, text);
                app.pending_prompts
                    .extend(std::iter::once(prompt).chain(prompts));
                app.notice = "Daemon writer unavailable · prompt retained".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

pub(super) fn dispatch_answers(
    app: &mut App,
    sender: &SyncSender<crate::bridge::WorkerCommand>,
) -> bool {
    let mut answers = app.take_answers().into_iter();
    while let Some(answer) = answers.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Answer(
            answer.0, answer.1, answer.2,
        )) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Answer(session, id, payload))) => {
                app.pending_answers
                    .extend(std::iter::once((session, id, payload)).chain(answers));
                app.notice = "Daemon writer busy · answer retained".into();
                return false;
            }
            Err(TrySendError::Disconnected(crate::bridge::WorkerCommand::Answer(
                session,
                id,
                payload,
            ))) => {
                app.pending_answers
                    .extend(std::iter::once((session, id, payload)).chain(answers));
                app.notice = "Daemon writer unavailable · answer retained".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

// A disconnected router did not admit these queued commands. Reuse the same
// typed-command failure envelopes as the router, including each original owner.
pub(super) fn reject_pending_controls(app: &mut App, failed: crate::bridge::WorkerCommand) -> bool {
    use crate::bridge::WorkerCommand;
    app.apply_worker_frame(crate::bridge::rejection_frame(failed, "Daemon unavailable"));
    for id in std::mem::take(&mut app.pending_model_queries) {
        app.apply_worker_frame(crate::bridge::rejection_frame(
            WorkerCommand::Models(id),
            "Daemon unavailable",
        ));
    }
    for (id, model) in std::mem::take(&mut app.pending_model_changes) {
        app.apply_worker_frame(crate::bridge::rejection_frame(
            WorkerCommand::SetModel(id, model),
            "Daemon unavailable",
        ));
    }
    for (id, effort) in std::mem::take(&mut app.pending_effort_changes) {
        app.apply_worker_frame(crate::bridge::rejection_frame(
            WorkerCommand::SetEffort(id, effort),
            "Daemon unavailable",
        ));
    }
    for (id, mode) in std::mem::take(&mut app.pending_permission_changes) {
        app.apply_worker_frame(crate::bridge::rejection_frame(
            WorkerCommand::SetPermissionMode(id, mode),
            "Daemon unavailable",
        ));
    }
    true
}

pub(super) fn dispatch_model_controls(
    app: &mut App,
    sender: &SyncSender<crate::bridge::WorkerCommand>,
) -> bool {
    let mut queries = std::mem::take(&mut app.pending_model_queries).into_iter();
    while let Some(id) = queries.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Models(id)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Models(id))) => {
                app.pending_model_queries.push(id);
                app.pending_model_queries.extend(queries);
                break;
            }
            Err(TrySendError::Disconnected(command)) => {
                app.pending_model_queries.extend(queries);
                return reject_pending_controls(app, command);
            }
            Err(_) => unreachable!(),
        }
    }
    let mut changes = std::mem::take(&mut app.pending_model_changes).into_iter();
    while let Some((id, model)) = changes.next() {
        match sender.try_send(crate::bridge::WorkerCommand::SetModel(id, model)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::SetModel(id, model))) => {
                app.pending_model_changes.push((id, model));
                app.pending_model_changes.extend(changes);
                break;
            }
            Err(TrySendError::Disconnected(command)) => {
                app.pending_model_changes.extend(changes);
                return reject_pending_controls(app, command);
            }
            Err(_) => unreachable!(),
        }
    }
    let mut efforts = std::mem::take(&mut app.pending_effort_changes).into_iter();
    while let Some((id, effort)) = efforts.next() {
        match sender.try_send(crate::bridge::WorkerCommand::SetEffort(id, effort)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::SetEffort(id, effort))) => {
                app.pending_effort_changes.push((id, effort));
                app.pending_effort_changes.extend(efforts);
                break;
            }
            Err(TrySendError::Disconnected(command)) => {
                app.pending_effort_changes.extend(efforts);
                return reject_pending_controls(app, command);
            }
            Err(_) => unreachable!(),
        }
    }
    let mut permissions = std::mem::take(&mut app.pending_permission_changes).into_iter();
    while let Some((id, mode)) = permissions.next() {
        match sender.try_send(crate::bridge::WorkerCommand::SetPermissionMode(id, mode)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::SetPermissionMode(id, mode))) => {
                app.pending_permission_changes.push((id, mode));
                app.pending_permission_changes.extend(permissions);
                break;
            }
            Err(TrySendError::Disconnected(command)) => {
                app.pending_permission_changes.extend(permissions);
                return reject_pending_controls(app, command);
            }
            Err(_) => unreachable!(),
        }
    }
    false
}

pub(super) fn dispatch_peer_refresh(
    app: &mut App,
    sender: &SyncSender<crate::bridge::WorkerCommand>,
) -> bool {
    let Some(id) = app.pending_peer_refresh.take() else {
        return false;
    };
    if id.is_empty() {
        return false;
    }
    match sender.try_send(crate::bridge::WorkerCommand::Peers(id)) {
        Ok(()) => false,
        Err(TrySendError::Full(crate::bridge::WorkerCommand::Peers(id))) => {
            app.pending_peer_refresh = Some(id);
            false
        }
        Err(TrySendError::Disconnected(crate::bridge::WorkerCommand::Peers(_))) => true,
        Err(_) => unreachable!(),
    }
}

pub(super) fn dispatch_peer_messages(
    app: &mut App,
    sender: &SyncSender<crate::bridge::WorkerCommand>,
) -> bool {
    let mut messages = std::mem::take(&mut app.pending_peer_messages).into_iter();
    while let Some((id, target, body)) = messages.next() {
        match sender.try_send(crate::bridge::WorkerCommand::Message(id, target, body)) {
            Ok(()) => {}
            Err(TrySendError::Full(crate::bridge::WorkerCommand::Message(id, target, body))) => {
                app.pending_peer_messages
                    .extend(std::iter::once((id, target, body)).chain(messages));
                return false;
            }
            Err(TrySendError::Disconnected(crate::bridge::WorkerCommand::Message(
                id,
                target,
                body,
            ))) => {
                app.rejected_drafts
                    .entry(id)
                    .or_default()
                    .push(format!("/msg {target} {body}"));
                for (id, target, body) in messages {
                    app.rejected_drafts
                        .entry(id)
                        .or_default()
                        .push(format!("/msg {target} {body}"));
                }
                app.notice = "Peer delivery unavailable · Alt+Up restores message".into();
                return true;
            }
            Err(_) => unreachable!(),
        }
    }
    false
}
