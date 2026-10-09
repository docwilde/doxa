//! Own terminal modes, scheduling, bounded command queues and layout persistence.
use super::{links, safe_label, App, RemoteHandoff};
use crossterm::event::{
    self, DisableBracketedPaste, DisableFocusChange, DisableMouseCapture, EnableBracketedPaste,
    EnableFocusChange, EnableMouseCapture,
};
use crossterm::execute;
use crossterm::terminal::{self, EnterAlternateScreen, LeaveAlternateScreen};
use ratatui::{backend::CrosstermBackend, layout::Rect, Terminal};
use std::io::{self, IsTerminal, Stdout, Write};
use std::process::{Child, Command, Stdio};
use std::os::unix::process::CommandExt;
use std::sync::mpsc::{self, Receiver, SyncSender, TryRecvError, TrySendError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Legacy wire consumers and owned worker results retain their original
/// channels, without a relay thread or a second queue.
enum FrameSource {
    Wire(Receiver<serde_json::Value>),
    Worker(Receiver<crate::worker_frames::WorkerFrame>),
    Remote(Receiver<crate::worker_frames::WorkerFrame>),
}

impl FrameSource {
    fn try_reduce(&self, app: &mut App) -> Result<bool, TryRecvError> {
        match self {
            Self::Wire(receiver) => receiver
                .try_recv()
                .map(|frame| app.apply_daemon_frame(&frame)),
            Self::Worker(receiver) | Self::Remote(receiver) => receiver
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
/// The in-window connector also stops if terminal I/O exits the loop early.
struct RemoteConnector(Child);
impl Drop for RemoteConnector {
    fn drop(&mut self) { stop_remote_connector(&mut self.0); }
}
struct CommandRouter {
    sender: SyncSender<crate::bridge::WorkerCommand>,
    remote: Arc<Mutex<Option<SyncSender<crate::bridge::WorkerCommand>>>>,
    errors: Receiver<crate::worker_frames::WorkerFrame>,
    thread: std::thread::JoinHandle<()>,
}
fn command_target(command:&crate::bridge::WorkerCommand)->Option<&str>{
    use crate::bridge::WorkerCommand::*;
    match command {
        Launch(..)=>None,
        Attach(id,..)|Prompt(id,..)|Answer(id,..)|Peers(id)|Message(id,..)|Models(id)
        |SetModel(id,..)|SetEffort(id,..)|SetPermissionMode(id,..)|SetIsolation(id,..)|Branch(id,..)
        |QueueList(id)|ContextDetail(id)|RemoteHistory(id,..)|QueueCancel(id,..)|Status(id)|Stop(id)
        |FinalizeForClear(id)=>Some(id),
    }
}
impl CommandRouter {
    fn start(local:SyncSender<crate::bridge::WorkerCommand>)->Self{
        let (sender,commands)=mpsc::sync_channel(64);
        let (error_tx,errors)=mpsc::sync_channel(32);
        let remote:Arc<Mutex<Option<SyncSender<crate::bridge::WorkerCommand>>>>=Arc::new(Mutex::new(None));
        let destination=remote.clone();
        let thread=std::thread::spawn(move||{
            while let Ok(command)=commands.recv(){
                let is_remote=command_target(&command).is_some_and(crate::remote_client::valid_target);
                let target=if is_remote {destination.lock().ok().and_then(|entry|entry.clone())} else {Some(local.clone())};
                let failed=match target {
                    Some(target)=>target.send(command).err().map(|failure|failure.0),
                    None=>Some(command),
                };
                if let Some(command)=failed {
                    if error_tx.send(crate::bridge::rejection_frame(command,"Remote session is disconnected")).is_err(){break;}
                }
            }
        });
        Self{sender,remote,errors,thread}
    }
    fn shutdown(self){
        let Self{sender,remote,errors,thread}=self;
        if let Ok(mut destination)=remote.lock(){*destination=None;}
        drop(sender);drop(errors);
        let _=thread.join();
    }
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

/// Open hub sessions in the regular DOXA terminal layout with remote-only
/// commands. The hub remains the source of session identity and transcript.
pub fn run_remote_with_worker_channels(
    frames: Receiver<crate::worker_frames::WorkerFrame>,
    prompts: SyncSender<crate::bridge::WorkerCommand>,
) -> io::Result<()> {
    run_loop(FrameSource::Remote(frames), Some(prompts), None)
}

pub(crate) fn run_remote_with_worker_channels_layout(
    frames: Receiver<crate::worker_frames::WorkerFrame>,
    prompts: SyncSender<crate::bridge::WorkerCommand>,
    store: crate::remote_layout::Store,
) -> io::Result<()> {
    run_loop_with_remote(FrameSource::Remote(frames), Some(prompts), None, Some(store))
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
    prompt_sender: Option<SyncSender<crate::bridge::WorkerCommand>>,
    state: Option<(crate::ui_state::UiStateStore, Vec<String>, Arc<Mutex<bool>>)>,
) -> io::Result<()> {
    run_loop_with_remote(receiver, prompt_sender, state, None)
}

pub(super) fn remote_handoff_args(url: &str, save_layout: bool) -> Vec<&str> {
    let mut args = vec!["remote", "tui", url];
    if save_layout { args.push("--save-layout"); }
    args
}

fn open_mixed_layout(
    local: &crate::ui_state::UiStateStore,
    worker: &crate::remote_client::RemoteWorker,
    hub: &str,
) -> io::Result<crate::remote_layout::Store> {
    if local.path().as_os_str().is_empty() {
        return Err(io::Error::other("persistent local tabset unavailable"));
    }
    let inventory = crate::remote_layout::Inventory::parse(&worker.initial_inventory)?;
    let home = crate::operations::doxa_home()?;
    let scope = format!("{}\0{}", local.scope_key(), local.path().display());
    crate::remote_layout::Store::open_mixed(&home, hub, inventory, &scope)
}

fn run_loop_with_remote(
    receiver: FrameSource,
    mut prompt_sender: Option<SyncSender<crate::bridge::WorkerCommand>>,
    mut state: Option<(crate::ui_state::UiStateStore, Vec<String>, Arc<Mutex<bool>>)>,
    mut remote_layout: Option<crate::remote_layout::Store>,
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
    app.remote_mode = matches!(receiver, FrameSource::Remote(_));
    let command_router=if matches!(receiver,FrameSource::Worker(_)) {
        prompt_sender.take().map(CommandRouter::start)
    }else{None};
    if let Some(router)=command_router.as_ref(){prompt_sender=Some(router.sender.clone());}
    app.persist_preferences = true;
    app.plugin_refresh_dirty = true;
    match crate::operations::doxa_home().and_then(|home| {
        let reserved: Vec<&str> = super::COMMANDS.iter().map(|row| row.name).collect();
        crate::native_plugins::load(&home, &reserved)
    }) {
        Ok(inventory) => {
            app.native_plugin_commands = inventory.commands;
            app.native_plugin_failures = inventory.failures;
            app.native_status = crate::native_plugins::StatusRuntime::new(inventory.statuses);
            if !app.native_plugin_failures.is_empty() {
                app.notice = format!("Native plugin rejected: {}", app.native_plugin_failures.join(" · "));
            }
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => {
            app.native_plugin_failures.push(format!("loader: {error}"));
            app.notice = format!("Native plugins unavailable: {error}");
        }
    }
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
    app.configure_terminal_images(app.preferences.value("image_mode"));
    terminal.draw(|frame| app.draw(frame))?;
    let mut pointer_on_link = false;
    let mut remote_connector: Option<RemoteConnector> = None;
    let mut remote_worker:Option<crate::remote_client::RemoteWorker>=None;
    let mut remote_start:Option<(Receiver<io::Result<crate::remote_client::RemoteWorker>>, String, bool)>=None;
    let mut active_remote_hub:Option<String>=None;
    let mut auto_open_remote=false;
    let mut first_run_pending = crate::first_run::needed();
    let mut installation_worker = crate::installation::Worker::start().ok();
    if installation_worker.is_none() {
        app.installation.update = crate::installation::Update::Unknown;
    }
    while !app.should_quit {
        let mut changed = app.poll_terminal_images();
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
        if let Some(store) = remote_layout.as_mut() {
            let local_complete = app.remote_mode || state.as_ref().is_some_and(|(_, _, complete)|
                complete.lock().is_ok_and(|ready| *ready));
            changed |= store.restore_if_ready_with_local(&mut app, local_complete);
        }
        if let Some(router)=command_router.as_ref(){
            for _ in 0..32 {match router.errors.try_recv(){
                Ok(frame)=>changed|=app.apply_worker_frame(frame),
                Err(TryRecvError::Empty|TryRecvError::Disconnected)=>break,
            }}
        }
        if let Some(worker)=remote_worker.as_ref(){
            for _ in 0..64 {match worker.frames.try_recv(){
                Ok(frame)=>{
                    let opened=if auto_open_remote {
                        match &frame {crate::worker_frames::WorkerFrame::Daemon{session_id,frame}
                            if frame["type"]=="hello"=>Some(session_id.clone()),_=>None}
                    }else{None};
                    changed|=app.apply_worker_frame(frame);
                    if let Some(id)=opened {
                        let group=&mut app.groups[app.active_group];
                        if !group.tabs.contains(&id){group.tabs.push(id.clone());}
                        group.active=group.tabs.iter().position(|tab|tab==&id).unwrap();
                        group.scroll=0;
                        app.notice=format!("Remote tab opened · {id}");
                        auto_open_remote=false;
                        changed=true;
                    }
                },
                Err(TryRecvError::Empty|TryRecvError::Disconnected)=>break,
            }}
        }
        if let Some((start, hub, save_layout))=remote_start.as_ref(){
            match start.try_recv(){
                Ok(Ok(worker))=>{
                    let layout = if *save_layout {
                        state.as_ref().ok_or_else(|| io::Error::other("local tabset unavailable"))
                            .and_then(|(local, _, _)| open_mixed_layout(local, &worker, hub))
                            .map(Some)
                    } else { Ok(None) };
                    match layout {
                        Ok(store) => {
                            remote_layout = store;
                            if let Some(router)=command_router.as_ref(){
                                if let Ok(mut destination)=router.remote.lock(){*destination=Some(worker.commands.clone());}
                            }
                            active_remote_hub=crate::remote_client::hub_url(hub).ok().map(|url|url.to_string());
                            remote_worker=Some(worker);remote_start=None;auto_open_remote=true;
                            app.notice="Remote hub connected · opening first tab".into();changed=true;
                        }
                        Err(error) => {
                            worker.shutdown();remote_start=None;
                            app.notice=format!("Mixed layout unavailable: {error}");changed=true;
                        }
                    }
                },
                Ok(Err(error))=>{remote_start=None;app.notice=format!("Remote hub: {error}");changed=true;},
                Err(TryRecvError::Disconnected)=>{remote_start=None;app.notice="Remote hub startup worker stopped".into();changed=true;},
                Err(TryRecvError::Empty)=>{},
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
        if !app.remote_mode {
            if let Some(RemoteHandoff::Hub { url, save_layout })=app.remote_handoff.take(){
                app.should_quit=false;
                if command_router.is_none(){
                    app.notice="Mixed remote tabs require the native worker transport".into();
                }else if remote_worker.is_some()||remote_start.is_some(){
                    let requested_hub=crate::remote_client::hub_url(&url).map(|hub|hub.to_string()).unwrap_or_default();
                    if active_remote_hub.as_deref().is_some_and(|active| active != requested_hub) {
                        app.notice="A different remote hub is already connected in this window".into();
                    }else if save_layout && remote_layout.is_none() && remote_worker.is_some() {
                        let result=state.as_ref().ok_or_else(||io::Error::other("local tabset unavailable"))
                            .and_then(|(local,_,_)|open_mixed_layout(local,remote_worker.as_ref().unwrap(),&url));
                        match result {
                            Ok(store)=>{remote_layout=Some(store);app.notice="Mixed layout saving enabled".into();},
                            Err(error)=>{app.notice=format!("Mixed layout unavailable: {error}");}
                        }
                    }else if let Some(id)=app.sessions.iter().find(|session|crate::remote_client::valid_target(&session.id)).map(|session|session.id.clone()){
                        let group=&mut app.groups[app.active_group];
                        if !group.tabs.contains(&id){group.tabs.push(id.clone());}
                        group.active=group.tabs.iter().position(|tab|tab==&id).unwrap();
                        group.scroll=0;
                        app.notice=format!("Remote tab selected · {id}");
                    }else{app.notice="Remote hub is still connecting".into();}
                }else{
                    let (tx,rx)=mpsc::sync_channel(1);
                    remote_start=Some((rx,url.clone(),save_layout));
                    std::thread::spawn(move||{
                        let result=crate::remote_client::start(&url);
                        if let Err(error)=tx.send(result){
                            if let Ok(worker)=error.0{worker.shutdown();}
                        }
                    });
                    app.notice="Connecting to remote hub…".into();
                }
                changed=true;
            }
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
        if let Some(connector) = remote_connector.as_mut() {
            match connector.0.try_wait() {
                Ok(Some(status)) => {
                    app.notice = format!("Remote connector exited ({status}) · inspect doxa remote connect from a terminal");
                    remote_connector = None;
                    changed = true;
                }
                Err(error) => {
                    app.notice = format!("Remote connector status unavailable: {error}");
                    remote_connector = None;
                    changed = true;
                }
                Ok(None) => {}
            }
        }
        if std::mem::take(&mut app.remote_disconnect_requested) {
            app.notice = if let Some(connector) = remote_connector.take() {
                drop(connector);
                "Remote sharing stopped · hub presence expires after its lease".into()
            } else {
                "No remote connector is running in this window".into()
            };
            changed = true;
        }
        if let Some((url, host)) = app.remote_connect_request.take() {
            app.notice = if remote_connector.is_some() {
                "Remote connector already running · use /remote-disconnect first".into()
            } else {
                match start_remote_connector(&url, &host) {
                    Ok(child) => {
                        remote_connector = Some(RemoteConnector(child));
                        format!("Remote connector running as {host} · verify hub registration · /remote-disconnect stops it")
                    }
                    Err(error) => format!("Could not start remote connector: {error}"),
                }
            };
            changed = true;
        }
        changed |= app.poll_sessions_roster();
        changed |= app.poll_stale_detached(Instant::now());
        changed |= app.poll_sessions_stop();
        changed |= app.poll_session_delete();
        changed |= app.poll_fleet();
        changed |= app.poll_diff();
        changed |= app.poll_codegraph();
        changed |= app.poll_auto_diff();
        changed |= app.poll_history();
        changed |= app.poll_resume();
        changed |= app.poll_belief_filter(Instant::now());
        changed |= app.poll_lore();
        changed |= app.poll_belief_graph();
        changed |= app.poll_memory();
        changed |= app.poll_lore_pending();
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
        changed |= app.poll_native_package_review();
        changed |= app.poll_native_package_run();
        changed |= app.poll_mermaid_preflight();
        if !app.remote_mode {
            let status = app.native_status.poll(Instant::now());
            if let Some(plugin) = status.newly_disabled {
                app.notice = format!("Native plugin status {plugin} disabled after repeated refresh failures");
            }
            if status.changed && app.chip_info.as_ref().is_some_and(|info| info.kind == "native_status") {
                if let Some(info) = app.chip_info.as_mut() { info.lines = app.native_status.ledger_lines(); }
            }
            changed |= status.changed;
        }
        changed |= app.poll_vendor_catalog();
        changed |= app.poll_model_catalog(Instant::now());
        changed |= app.tick_blink(Instant::now());
        changed |= app.tick_rail_sort(Instant::now());
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
            if !app.pending_remote_history.is_empty(){
                app.pending_remote_history.clear();app.remote_history_loading.clear();
                app.notice="Remote history unavailable · hub connection closed".into();changed=true;
            }
        }
        if let Some(sender) = &prompt_sender {
            let disconnected = dispatch_launches(&mut app, sender);
            let disconnected = dispatch_attaches(&mut app, sender) || disconnected;
            let disconnected = dispatch_prompts(&mut app, sender) || disconnected;
            let disconnected = dispatch_answers(&mut app, sender) || disconnected;
            let disconnected = dispatch_remote_history(&mut app,sender) || disconnected;
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
        if app.should_quit && (app.delete_after_stop.is_some() || app.session_delete_pending.is_some()) {
            app.should_quit = false;
            app.remote_handoff = None;
            app.notice = "Wait for transcript deletion to finish before leaving".into();
            changed = true;
        }
        if changed {
            terminal.draw(|frame| app.draw(frame))?;
        }
    }
    // exec-based restart and handoff bypass Rust destructors for App.
    app.cancel_native_package_run();
    drop(remote_connector);
    if let Some(router)=command_router.as_ref(){
        if let Ok(mut destination)=router.remote.lock(){*destination=None;}
    }
    drop(prompt_sender);
    if let Some(worker)=remote_worker{worker.shutdown();}
    if let Some(router)=command_router{router.shutdown();}
    drop(terminal);
    drop(guard);
    if let Some(mut store) = remote_layout {
        let inventory = crate::remote_client::fresh_inventory(store.hub())?;
        let local_complete = app.remote_mode || state.as_ref().is_some_and(|(_, _, complete)|
            complete.lock().is_ok_and(|ready| *ready));
        store.save_checked_with_local(&app, crate::remote_layout::Inventory::parse(&inventory)?, local_complete)?;
    }
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
    if let Some(handoff) = app.remote_handoff.take() {
        use std::os::unix::process::CommandExt;
        let executable = std::env::current_exe()?;
        return Err(match handoff {
            RemoteHandoff::Hub { url, save_layout } =>
                Command::new(executable).args(remote_handoff_args(&url, save_layout)).exec(),
            RemoteHandoff::Local => Command::new(executable).exec(),
        });
    }
    Ok(())
}

fn start_remote_connector(url: &str, host: &str) -> io::Result<Child> {
    if !doxa_peers::remote_policy::remote_enabled() {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied,
            "enable remote access in /settings first"));
    }
    if doxa_peers::remote_policy::setting("remote_allowed_logins", "DOXA_REMOTE_ALLOWED_LOGINS").trim().is_empty() {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied,
            "set allowed Tailscale logins in /settings first"));
    }
    let executable = std::env::var_os("DOXA_REMOTE_BIN")
        .map(std::path::PathBuf::from)
        .unwrap_or(std::env::current_exe()?.with_file_name("doxa-remote"));
    Command::new(executable)
        .args(["connect", url, host])
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
}

fn stop_remote_connector(child: &mut Child) {
    if child.try_wait().ok().flatten().is_some() { return; }
    let group = -(child.id() as i32);
    unsafe { libc::kill(group, libc::SIGTERM); }
    let deadline = Instant::now() + Duration::from_secs(1);
    while Instant::now() < deadline {
        if child.try_wait().ok().flatten().is_some() { return; }
        std::thread::sleep(Duration::from_millis(10));
    }
    unsafe { libc::kill(group, libc::SIGKILL); }
    let _ = child.wait();
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

fn dispatch_remote_history(app:&mut App,sender:&SyncSender<crate::bridge::WorkerCommand>)->bool{
    let mut pages=std::mem::take(&mut app.pending_remote_history).into_iter();
    while let Some((id,before))=pages.next(){
        match sender.try_send(crate::bridge::WorkerCommand::RemoteHistory(id,before)){
            Ok(())=>{},
            Err(TrySendError::Full(crate::bridge::WorkerCommand::RemoteHistory(id,before)))=>{
                app.pending_remote_history.extend(std::iter::once((id,before)).chain(pages));return false;
            },
            Err(TrySendError::Disconnected(_))=>{
                app.remote_history_loading.clear();app.notice="Remote history unavailable".into();return true;
            },
            Err(_)=>unreachable!(),
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

#[cfg(test)] mod mixed_router_tests {
    use super::*;
    use crate::bridge::WorkerCommand;
    #[test] fn mixed_router_sends_each_prompt_to_its_own_transport(){
        let (local_tx,local_rx)=mpsc::sync_channel(4);
        let (remote_tx,remote_rx)=mpsc::sync_channel(4);
        let router=CommandRouter::start(local_tx);
        *router.remote.lock().unwrap()=Some(remote_tx);
        router.sender.send(WorkerCommand::Prompt("local-id".into(),"local".into())).unwrap();
        router.sender.send(WorkerCommand::Prompt("host~remote-id".into(),"remote".into())).unwrap();
        assert!(matches!(local_rx.recv_timeout(Duration::from_secs(1)).unwrap(),WorkerCommand::Prompt(id,_)
            if id=="local-id"));
        assert!(matches!(remote_rx.recv_timeout(Duration::from_secs(1)).unwrap(),WorkerCommand::Prompt(id,_)
            if id=="host~remote-id"));
        router.shutdown();
    }
}
