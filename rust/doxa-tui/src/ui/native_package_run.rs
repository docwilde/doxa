//! Lifecycle seam for an explicit zero-grant run or `render-local-panel-v1`
//! panel request behind an enrolled operator's short-lived, instance-bound
//! installed-host authority. Both share one cancellation and cleanup path.
use super::{App, ChipInfo};
use crate::native_plugins::{self, packages, InstalledHostAuthority, PanelText};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::{mpsc::{self, Receiver, TryRecvError}, Arc};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const RUN_KIND: &str = "native_package_run";
const CANCEL_JOIN_BUDGET: Duration = Duration::from_millis(100);
const CHECKING: u8 = 0;
const CANCELLED_BEFORE_DISPATCH: u8 = 1;
const DISPATCH_ADMITTED: u8 = 2;
const FINISHED: u8 = 3;

/// The request the owner typed. It selects the only grant set and worker
/// contract that may answer: a zero-grant run can never show a panel, and a
/// panel request never runs a package without its exactly approved grant.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum Mode { Grantless, Panel }

/// What a dispatched worker returned. `PanelText` is only constructible by
/// the panel validator.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum Payload { Value(i32), Panel(PanelText) }

#[derive(Debug, Clone, PartialEq, Eq)]
enum RunOutcome {
    Returned(i32),
    Rendered { name: String, module_sha256: String, lines: Vec<String> },
    Cancelled,
    ApprovalRequired,
    GrantsRefused,
    PanelGrantRequired,
    PackageInvalid,
    PackageChanged,
    HostUnverified,
    RunnerFailed,
    WorkerStopped,
}

impl RunOutcome {
    fn lines(&self) -> Vec<String> {
        let message = match self {
            Self::Returned(value) => return vec![format!("Isolated plugin returned {value}")],
            // DOXA owns the header and the gutter; plugin text appears only
            // after the gutter, so it cannot impersonate DOXA's own lines.
            Self::Rendered { name, module_sha256, lines } => {
                let mut rows = vec![
                    format!("Plugin panel: {name} · render-local-panel-v1 · plain text drawn by DOXA"),
                    format!("Module SHA-256: {module_sha256}"),
                    String::new(),
                ];
                rows.extend(lines.iter().map(|line| format!("│ {line}")));
                return rows;
            }
            Self::Cancelled => "Run cancelled; no result was accepted",
            Self::ApprovalRequired => "Run refused: exact owner approval is required",
            Self::GrantsRefused => "Run refused: only zero-grant packages are eligible; use /native-plugin panel for an approved render-local-panel-v1 package",
            Self::PanelGrantRequired => "Panel refused: the package must request, and the owner must approve, exactly render-local-panel-v1",
            Self::PackageInvalid => "Run refused: package preflight failed",
            Self::PackageChanged => "Run refused: approved package identity changed before admission",
            Self::HostUnverified => "TUI execution disabled: this installed host has no trusted delegation acceptance",
            Self::RunnerFailed => "Isolated worker failed; no result was accepted",
            Self::WorkerStopped => "Run task stopped; no result was accepted",
        };
        vec![message.into()]
    }
}

/// A disposable-guest receipt is evidence for that guest, not an authority for
/// the installed TUI's parent process, cgroup delegation, and worker identity.
fn installed_host_admission(home: &Path) -> Result<InstalledHostAuthority, RunOutcome> {
    native_plugins::installed_host_authority(home).map_err(|_| RunOutcome::HostUnverified)
}

fn recheck_and_admit<G>(home: &Path, review: &packages::Review, mode: Mode, cancel: &AtomicBool, gate: G) -> Result<InstalledHostAuthority, RunOutcome>
where G: FnOnce() -> Result<InstalledHostAuthority, RunOutcome>
{
    if cancel.load(Ordering::Acquire) { return Err(RunOutcome::Cancelled); }
    if !review.owner_approved { return Err(RunOutcome::ApprovalRequired); }
    match mode {
        Mode::Grantless if !review.requested_grants.is_empty() => return Err(RunOutcome::GrantsRefused),
        Mode::Panel if review.requested_grants != [packages::RENDER_PANEL_GRANT] =>
            return Err(RunOutcome::PanelGrantRequired),
        _ => {}
    }
    if packages::recheck_approved(home, review).is_err() { return Err(RunOutcome::PackageChanged); }
    if cancel.load(Ordering::Acquire) { return Err(RunOutcome::Cancelled); }
    let authority = gate()?;
    if cancel.load(Ordering::Acquire) { return Err(RunOutcome::Cancelled); }
    Ok(authority)
}

fn check_and_dispatch<G, F>(home: &Path, name: &str, mode: Mode, cancel: &AtomicBool,
    phase: &AtomicU8, gate: G, dispatch: F) -> RunOutcome
where
    G: FnOnce() -> Result<InstalledHostAuthority, RunOutcome>,
    F: FnOnce(&InstalledHostAuthority, &Path, &packages::Review, &AtomicBool) -> io::Result<Payload>,
{
    check_and_dispatch_with(home, name, mode, cancel, phase, gate, || {}, dispatch)
}

#[allow(clippy::too_many_arguments)]
fn check_and_dispatch_with<G, H, F>(home: &Path, name: &str, mode: Mode, cancel: &AtomicBool,
    phase: &AtomicU8, gate: G, before_claim: H, dispatch: F) -> RunOutcome
where
    G: FnOnce() -> Result<InstalledHostAuthority, RunOutcome>,
    H: FnOnce(),
    F: FnOnce(&InstalledHostAuthority, &Path, &packages::Review, &AtomicBool) -> io::Result<Payload>,
{
    if cancel.load(Ordering::Acquire) { return RunOutcome::Cancelled; }
    let review = match packages::preflight(home, name) {
        Ok(review) => review,
        Err(_) => return RunOutcome::PackageInvalid,
    };
    let authority = match recheck_and_admit(home, &review, mode, cancel, gate) {
        Ok(authority) => authority,
        Err(error) => return error,
    };
    before_claim();
    // Cancellation races with the last admission check. Only this atomic
    // transition may start a sandbox worker; a closed owner wins by changing
    // CHECKING to CANCELLED_BEFORE_DISPATCH.
    if phase.compare_exchange(CHECKING, DISPATCH_ADMITTED,
        Ordering::AcqRel, Ordering::Acquire).is_err() {
        return RunOutcome::Cancelled;
    }
    if cancel.load(Ordering::Acquire) { return RunOutcome::Cancelled; }
    let result = dispatch(&authority, home, &review, cancel);
    if cancel.load(Ordering::Acquire) { return RunOutcome::Cancelled; }
    match (mode, result) {
        (Mode::Grantless, Ok(Payload::Value(value))) => RunOutcome::Returned(value),
        (Mode::Panel, Ok(Payload::Panel(panel))) => RunOutcome::Rendered {
            name: review.name.clone(), module_sha256: review.module_sha256.clone(),
            lines: panel.lines().map(str::to_owned).collect(),
        },
        // A payload of the other contract is a runner fault, never a result.
        _ => RunOutcome::RunnerFailed,
    }
}

#[derive(Debug)]
pub(super) struct PendingRun {
    owner: (usize, String),
    name: String,
    cancel: Arc<AtomicBool>,
    phase: Arc<AtomicU8>,
    receiver: Receiver<RunOutcome>,
    thread: Option<JoinHandle<()>>,
}

impl PendingRun {
    fn spawn<G, F>(owner: (usize, String), home: PathBuf, name: String, mode: Mode, gate: G, dispatch: F) -> io::Result<Self>
    where
        G: FnOnce() -> Result<InstalledHostAuthority, RunOutcome> + Send + 'static,
        F: FnOnce(&InstalledHostAuthority, &Path, &packages::Review, &AtomicBool) -> io::Result<Payload> + Send + 'static,
    {
        let cancel = Arc::new(AtomicBool::new(false));
        let phase = Arc::new(AtomicU8::new(CHECKING));
        let (sender, receiver) = mpsc::channel();
        let task_cancel = Arc::clone(&cancel);
        let task_phase = Arc::clone(&phase);
        let task_name = name.clone();
        let thread = thread::Builder::new().name("doxa-native-package-request".into()).spawn(move || {
            let outcome = check_and_dispatch(&home, &task_name, mode, &task_cancel,
                &task_phase, gate, dispatch);
            task_phase.store(FINISHED, Ordering::Release);
            let _ = sender.send(outcome);
        })?;
        Ok(Self { owner, name, cancel, phase, receiver, thread: Some(thread) })
    }
}

impl Drop for PendingRun {
    fn drop(&mut self) {
        self.cancel.store(true, Ordering::Release);
        let phase = self.phase.compare_exchange(CHECKING, CANCELLED_BEFORE_DISPATCH,
            Ordering::AcqRel, Ordering::Acquire).unwrap_or_else(|phase| phase);
        if let Some(thread) = self.thread.take() {
            if phase == DISPATCH_ADMITTED {
                // Once dispatch crossed the fence, the TUI cannot exit or
                // exec until the sandbox supervisor's cleanup attempt returns.
                // Installed-host proof must still establish cleanup success.
                let _ = thread.join();
                return;
            }
            let deadline = Instant::now() + CANCEL_JOIN_BUDGET;
            while !thread.is_finished() && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(1));
            }
            // A stuck private-file read may outlive panel close. Its failed
            // admission CAS forbids worker spawn after this controller drops.
            if thread.is_finished() { let _ = thread.join(); }
        }
    }
}

fn dispatch_grantless(authority: &InstalledHostAuthority, home: &Path, review: &packages::Review,
    cancel: &AtomicBool) -> io::Result<Payload> {
    native_plugins::run_reviewed_grantless_package(authority, home, review, cancel).map(Payload::Value)
}

fn dispatch_panel(authority: &InstalledHostAuthority, home: &Path, review: &packages::Review,
    cancel: &AtomicBool) -> io::Result<Payload> {
    native_plugins::run_reviewed_panel_package(authority, home, review, cancel).map(Payload::Panel)
}

impl App {
    pub(super) fn open_native_package_run_at(&mut self, home: &Path, name: &str) {
        self.open_native_package_request(home, name, Mode::Grantless);
    }

    /// `/native-plugin panel NAME`: the only route to `render-local-panel-v1`.
    pub(super) fn open_native_package_panel_at(&mut self, home: &Path, name: &str) {
        self.open_native_package_request(home, name, Mode::Panel);
    }

    fn open_native_package_request(&mut self, home: &Path, name: &str, mode: Mode) {
        if self.native_package_run.is_some() {
            self.notice = "A native package request is already active".into();
            return;
        }
        let owner = self.prompt_owner();
        self.native_package_run_display_owner = None;
        self.chip_info = Some(ChipInfo {
            kind: RUN_KIND,
            label: name.into(),
            lines: vec![match mode {
                Mode::Grantless => "Checking exact zero-grant approval and installed-host admission…",
                Mode::Panel => "Checking exact render-local-panel-v1 approval and installed-host admission…",
            }.into()],
            scroll: 0,
            owner: None,
        });
        if self.active_chooser_rect().is_none() {
            self.chip_info = None;
            self.notice = "Enlarge active pane to inspect native package request".into();
            return;
        }
        let authority_home = home.to_owned();
        let gate = move || installed_host_admission(&authority_home);
        let spawned = match mode {
            Mode::Grantless => PendingRun::spawn(owner, home.to_owned(), name.to_owned(), mode, gate, dispatch_grantless),
            Mode::Panel => PendingRun::spawn(owner, home.to_owned(), name.to_owned(), mode, gate, dispatch_panel),
        };
        match spawned {
            Ok(run) => {
                self.native_package_run = Some(run);
                self.input.clear();
                self.input_cursor = 0;
            }
            Err(_) => {
                self.chip_info = None;
                self.notice = "Native package request could not start".into();
            }
        }
    }

    pub(super) fn cancel_native_package_run(&mut self) {
        self.native_package_run = None;
        self.native_package_run_display_owner = None;
        if self.chip_info.as_ref().is_some_and(|info| info.kind == RUN_KIND) {
            self.chip_info = None;
        }
    }

    /// Called after input reduction and before polling, so a closed modal,
    /// changed tab, or exiting window cannot receive a late worker result.
    pub(super) fn reconcile_native_package_run(&mut self) -> bool {
        if let Some(run) = &self.native_package_run {
            let visible = self.chip_info.as_ref().is_some_and(|info|
                info.kind == RUN_KIND && info.label == run.name);
            if self.should_quit || self.prompt_owner() != run.owner || !visible {
                self.cancel_native_package_run();
                return true;
            }
        }
        if let Some(owner) = &self.native_package_run_display_owner {
            let visible = self.chip_info.as_ref().is_some_and(|info| info.kind == RUN_KIND);
            if self.should_quit || self.prompt_owner() != *owner {
                self.cancel_native_package_run();
                return true;
            }
            if !visible { self.native_package_run_display_owner = None; }
        }
        false
    }

    pub(super) fn poll_native_package_run(&mut self) -> bool {
        if self.reconcile_native_package_run() { return true; }
        let Some(run) = &self.native_package_run else { return false; };
        let result = match run.receiver.try_recv() {
            Ok(result) => result,
            Err(TryRecvError::Empty) => return false,
            Err(TryRecvError::Disconnected) => RunOutcome::WorkerStopped,
        };
        let owner = run.owner.clone();
        self.native_package_run = None;
        if let Some(info) = self.chip_info.as_mut().filter(|info| info.kind == RUN_KIND) {
            info.lines = result.lines();
            info.scroll = 0;
            self.native_package_run_display_owner = Some(owner);
            return true;
        }
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::{Event, KeyCode, KeyEvent, KeyModifiers};
    use sha2::{Digest, Sha256};
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::sync::atomic::AtomicUsize;
    use std::time::Duration;

    fn private(path: &Path, bytes: &[u8]) {
        fs::write(path, bytes).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    }

    fn fixture(approved: bool, grants: &[&str]) -> tempfile::TempDir {
        let home = tempfile::tempdir().unwrap();
        let packages = home.path().join("native-plugin-packages");
        let package = packages.join("demo");
        for directory in [home.path(), packages.as_path(), package.as_path()] {
            fs::create_dir_all(directory).unwrap();
            fs::set_permissions(directory, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let module = wat::parse_str("(module (func (export \"doxa_main\") (result i32) i32.const 17))").unwrap();
        let grant_list = grants.iter().map(|grant| format!("'{grant}'")).collect::<Vec<_>>().join(", ");
        let manifest = format!("package_api_version = 1\nname = 'demo'\nversion = '1.0'\nartifact_format = 'wasm-core-v1'\nrequested_grants = [{grant_list}]\n");
        private(&package.join("manifest.toml"), manifest.as_bytes());
        private(&package.join("module.wasm"), &module);
        if approved {
            let config = format!("[[native_plugin_packages]]\nname = 'demo'\nmanifest_sha256 = '{:x}'\nmodule_sha256 = '{:x}'\ngrants = [{grant_list}]\n",
                Sha256::digest(manifest.as_bytes()), Sha256::digest(&module));
            private(&home.path().join("config.toml"), config.as_bytes());
        }
        home
    }

    #[test]
    fn production_admission_refuses_approved_package_without_operator_permit() {
        let home = fixture(true, &[]);
        let cancel = AtomicBool::new(false);
        let outcome = check_and_dispatch(home.path(), "demo", Mode::Grantless, &cancel, &AtomicU8::new(CHECKING),
            || installed_host_admission(home.path()), |_, _, _, _| panic!("unverified host must stay closed"));
        assert_eq!(outcome, RunOutcome::HostUnverified);
        assert_eq!(outcome.lines().len(), 1);
    }

    #[test]
    fn package_result_and_preflight_panels_render_their_actual_lines() {
        use ratatui::{backend::TestBackend, Terminal};
        for kind in [RUN_KIND, "native_package"] {
            let mut app = App::default();
            app.handle(Event::Resize(100, 30));
            app.chip_info = Some(ChipInfo { kind, label:"demo".into(),
                lines:vec!["Isolated plugin returned 17".into()],scroll:0,owner:None });
            let mut terminal = Terminal::new(TestBackend::new(100,30)).unwrap();
            terminal.draw(|frame| app.draw(frame)).unwrap();
            let painted = terminal.backend().buffer().content.iter().map(|cell| cell.symbol()).collect::<String>();
            assert!(painted.contains("Isolated plugin returned 17"),"{kind} hid the result body");
        }
    }

    #[test]
    fn approval_grants_and_cancel_all_precede_host_gate() {
        for (approved, grants, expected) in [
            (false, &[][..], RunOutcome::ApprovalRequired),
            (true, &["render-local-panel-v1"][..], RunOutcome::GrantsRefused),
        ] {
            let home = fixture(approved, grants);
            let cancel = AtomicBool::new(false);
            let result = check_and_dispatch(home.path(), "demo", Mode::Grantless, &cancel, &AtomicU8::new(CHECKING),
                || panic!("gate must not see rejected package"),
                |_, _, _, _| panic!("worker must not start"));
            assert_eq!(result, expected);
        }
        let home = fixture(true, &[]);
        let result = check_and_dispatch(home.path(), "demo", Mode::Grantless, &AtomicBool::new(true),
            &AtomicU8::new(CHECKING),
            || panic!("gate must not see cancelled request"),
            |_, _, _, _| panic!("worker must not start"));
        assert_eq!(result, RunOutcome::Cancelled);
    }

    #[test]
    fn preflight_identity_cannot_be_used_after_bytes_change() {
        let home = fixture(true, &[]);
        let review = packages::preflight(home.path(), "demo").unwrap();
        private(&home.path().join("native-plugin-packages/demo/module.wasm"), b"\0asm\x01\0\0\0");
        assert!(matches!(recheck_and_admit(home.path(), &review, Mode::Grantless, &AtomicBool::new(false),
            || panic!("stale bytes must not reach installed-host gate")),
            Err(RunOutcome::PackageChanged)));
    }

    fn running_app(home: &Path) -> (App, Receiver<()>) { running_app_in(home, Mode::Grantless) }

    fn running_app_in(home: &Path, mode: Mode) -> (App, Receiver<()>) {
        let mut app = App::default();
        app.groups[0].tabs.push("session-one".into());
        app.handle(Event::Resize(100, 30));
        let (started, started_rx) = mpsc::channel();
        let (cancelled, cancelled_rx) = mpsc::channel();
        let run = PendingRun::spawn(app.prompt_owner(), home.to_owned(), "demo".into(), mode,
            || Ok(native_plugins::test_installed_host_authority()), move |_, _, _, cancel| {
                started.send(()).unwrap();
                while !cancel.load(Ordering::Acquire) {
                    thread::sleep(Duration::from_millis(1));
                }
                cancelled.send(()).unwrap();
                Err(io::Error::new(io::ErrorKind::Interrupted, "cancelled"))
            }).unwrap();
        app.chip_info = Some(ChipInfo { kind: RUN_KIND, label: "demo".into(),
            lines: vec!["running".into()], scroll: 0, owner: None });
        app.native_package_run = Some(run);
        started_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        (app, cancelled_rx)
    }

    #[test]
    fn panel_close_tab_detach_and_window_exit_cancel_active_task() {
        for (mode, grants) in [(Mode::Grantless, &[][..]), (Mode::Panel, &[packages::RENDER_PANEL_GRANT][..])] {
            let home = fixture(true, grants);
            let (mut app, cancelled) = running_app_in(home.path(), mode);
            app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
            assert!(app.native_package_run.is_none());
            cancelled.recv_timeout(Duration::from_secs(1)).unwrap();

            let (mut app, cancelled) = running_app_in(home.path(), mode);
            app.detach_active_tab();
            assert!(app.native_package_run.is_none());
            cancelled.recv_timeout(Duration::from_secs(1)).unwrap();

            let (app, cancelled) = running_app_in(home.path(), mode);
            drop(app);
            cancelled.recv_timeout(Duration::from_secs(1)).unwrap();

            let (mut app, cancelled) = running_app_in(home.path(), mode);
            app.should_quit = true;
            assert!(app.reconcile_native_package_run());
            assert!(app.native_package_run.is_none());
            cancelled.recv_timeout(Duration::from_secs(1)).unwrap();
        }
    }

    #[test]
    fn panel_request_needs_exact_approved_render_grant_before_host_gate() {
        for (approved, grants, expected) in [
            (false, &[packages::RENDER_PANEL_GRANT][..], RunOutcome::ApprovalRequired),
            (false, &[][..], RunOutcome::ApprovalRequired),
            (true, &[][..], RunOutcome::PanelGrantRequired),
        ] {
            let home = fixture(approved, grants);
            let result = check_and_dispatch(home.path(), "demo", Mode::Panel, &AtomicBool::new(false),
                &AtomicU8::new(CHECKING), || panic!("gate must not see a package without the exact grant"),
                |_, _, _, _| panic!("panel worker must not start"));
            assert_eq!(result, expected);
        }
        // Owner approval must name the requested grant exactly: a zero-grant
        // approval of a package that asks for the panel grant is a mismatch.
        let home = fixture(true, &[packages::RENDER_PANEL_GRANT]);
        let manifest = fs::read(home.path().join("native-plugin-packages/demo/manifest.toml")).unwrap();
        let module = fs::read(home.path().join("native-plugin-packages/demo/module.wasm")).unwrap();
        private(&home.path().join("config.toml"), format!(
            "[[native_plugin_packages]]\nname = 'demo'\nmanifest_sha256 = '{:x}'\nmodule_sha256 = '{:x}'\ngrants = []\n",
            Sha256::digest(&manifest), Sha256::digest(&module)).as_bytes());
        assert_eq!(check_and_dispatch(home.path(), "demo", Mode::Panel, &AtomicBool::new(false),
            &AtomicU8::new(CHECKING), || panic!("mismatched approval reached the gate"),
            |_, _, _, _| panic!("panel worker must not start")), RunOutcome::PackageInvalid);
    }

    #[test]
    fn panel_identity_change_after_review_never_reaches_host_gate() {
        let home = fixture(true, &[packages::RENDER_PANEL_GRANT]);
        let review = packages::preflight(home.path(), "demo").unwrap();
        let module = home.path().join("native-plugin-packages/demo/module.wasm");
        let bytes = fs::read(&module).unwrap();
        let retired = fs::File::open(&module).unwrap();
        fs::remove_file(&module).unwrap();
        private(&module, &bytes); // identical bytes, new inode
        assert_eq!(recheck_and_admit(home.path(), &review, Mode::Panel, &AtomicBool::new(false),
            || panic!("replaced inode must not reach installed-host gate")).err(), Some(RunOutcome::PackageChanged));
        drop(retired);
        let review = packages::preflight(home.path(), "demo").unwrap();
        private(&module, b"\0asm\x01\0\0\0");
        assert_eq!(recheck_and_admit(home.path(), &review, Mode::Panel, &AtomicBool::new(false),
            || panic!("changed bytes must not reach installed-host gate")).err(), Some(RunOutcome::PackageChanged));
    }

    #[test]
    fn production_panel_admission_refuses_without_operator_permit() {
        let home = fixture(true, &[packages::RENDER_PANEL_GRANT]);
        let outcome = check_and_dispatch(home.path(), "demo", Mode::Panel, &AtomicBool::new(false),
            &AtomicU8::new(CHECKING), || installed_host_admission(home.path()),
            |_, _, _, _| panic!("unverified host must stay closed"));
        assert_eq!(outcome, RunOutcome::HostUnverified);
    }

    #[test]
    fn payload_of_the_other_contract_is_never_a_result() {
        let panel = crate::native_plugins::runner::validate_panel(b"spoof").unwrap();
        for (mode, grants, payload) in [
            (Mode::Grantless, &[][..], Payload::Panel(panel.clone())),
            (Mode::Panel, &[packages::RENDER_PANEL_GRANT][..], Payload::Value(17)),
        ] {
            let home = fixture(true, grants);
            let outcome = check_and_dispatch(home.path(), "demo", mode, &AtomicBool::new(false),
                &AtomicU8::new(CHECKING), || Ok(native_plugins::test_installed_host_authority()),
                move |_, _, _, _| Ok(payload));
            assert_eq!(outcome, RunOutcome::RunnerFailed, "{mode:?}");
        }
    }

    #[test]
    fn rendered_panel_uses_doxa_header_gutter_and_widgets() {
        use ratatui::{backend::TestBackend, Terminal};
        let home = fixture(true, &[packages::RENDER_PANEL_GRANT]);
        let mut app = App::default();
        app.handle(Event::Resize(100, 30));
        let panel = crate::native_plugins::runner::validate_panel("Queue: 3 jobs\nNext: deploy ✓".as_bytes()).unwrap();
        let run = PendingRun::spawn(app.prompt_owner(), home.path().to_owned(), "demo".into(), Mode::Panel,
            || Ok(native_plugins::test_installed_host_authority()), move |_, _, _, _| Ok(Payload::Panel(panel))).unwrap();
        app.chip_info = Some(ChipInfo { kind: RUN_KIND, label: "demo".into(),
            lines: vec!["running".into()], scroll: 0, owner: None });
        app.native_package_run = Some(run);
        let deadline = Instant::now() + Duration::from_secs(2);
        while !app.poll_native_package_run() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(1));
        }
        let lines = app.chip_info.as_ref().unwrap().lines.clone();
        assert!(lines[0].starts_with("Plugin panel: demo · render-local-panel-v1"));
        assert!(lines[1].starts_with("Module SHA-256: ") && lines[1].len() == "Module SHA-256: ".len() + 64);
        assert_eq!(&lines[3..], ["│ Queue: 3 jobs", "│ Next: deploy ✓"]);
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let painted = terminal.backend().buffer().content.iter().map(|cell| cell.symbol()).collect::<String>();
        assert!(painted.contains("│ Queue: 3 jobs"), "panel body was not drawn");
        assert!(painted.contains("Plugin panel: demo"), "DOXA provenance header was not drawn");
    }

    #[test]
    fn owner_switch_and_replaced_panel_discard_late_result() {
        let home = fixture(true, &[]);
        let (mut app, cancelled) = running_app(home.path());
        app.groups[0].tabs.push("session-two".into());
        app.groups[0].active = 1;
        assert!(app.poll_native_package_run());
        assert!(app.native_package_run.is_none());
        cancelled.recv_timeout(Duration::from_secs(1)).unwrap();

        let (mut app, cancelled) = running_app(home.path());
        app.chip_info = None;
        assert!(app.poll_native_package_run());
        cancelled.recv_timeout(Duration::from_secs(1)).unwrap();
    }

    #[test]
    fn completion_is_a_single_bounded_result_and_single_request() {
        let home = fixture(true, &[]);
        let mut app = App::default();
        app.handle(Event::Resize(100, 30));
        let count = Arc::new(AtomicUsize::new(0));
        let called = Arc::clone(&count);
        let run = PendingRun::spawn(app.prompt_owner(), home.path().to_owned(), "demo".into(), Mode::Grantless,
            || Ok(native_plugins::test_installed_host_authority()), move |_, _, _, _| { called.fetch_add(1, Ordering::SeqCst); Ok(Payload::Value(17)) }).unwrap();
        app.chip_info = Some(ChipInfo { kind: RUN_KIND, label: "demo".into(),
            lines: vec!["running".into()], scroll: 0, owner: None });
        app.native_package_run = Some(run);
        app.open_native_package_run_at(home.path(), "demo");
        assert_eq!(app.notice, "A native package request is already active");
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while !app.poll_native_package_run() && std::time::Instant::now() < deadline {
            thread::sleep(Duration::from_millis(1));
        }
        assert_eq!(count.load(Ordering::SeqCst), 1);
        assert!(app.native_package_run.is_none());
        assert_eq!(app.chip_info.as_ref().unwrap().lines, vec!["Isolated plugin returned 17"]);
    }

    #[test]
    fn completed_refusal_panel_is_retired_on_owner_switch_and_detach() {
        let home = fixture(true, &[]);
        let mut app = App::default();
        app.groups[0].tabs.push("session-one".into());
        app.groups[0].tabs.push("session-two".into());
        app.handle(Event::Resize(100, 30));
        app.open_native_package_run_at(home.path(), "demo");
        let deadline = Instant::now() + Duration::from_secs(2);
        while !app.poll_native_package_run() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(1));
        }
        assert!(app.native_package_run.is_none());
        assert_eq!(app.native_package_run_display_owner, Some((0, "session-one".into())));
        assert!(app.chip_info.as_ref().unwrap().lines[0].contains("TUI execution disabled"));

        app.groups[0].active = 1;
        assert!(app.reconcile_native_package_run());
        assert!(app.chip_info.is_none());
        assert!(app.native_package_run_display_owner.is_none());

        app.groups[0].active = 0;
        app.open_native_package_run_at(home.path(), "demo");
        let deadline = Instant::now() + Duration::from_secs(2);
        while !app.poll_native_package_run() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(1));
        }
        assert!(app.chip_info.is_some());
        app.detach_active_tab();
        assert!(app.chip_info.is_none());
        assert!(app.native_package_run_display_owner.is_none());
    }

    #[test]
    fn cancellation_never_waits_indefinitely_for_a_stalled_task() {
        let home = fixture(true, &[]);
        let (started, started_rx) = mpsc::channel();
        let (release, release_rx) = mpsc::channel();
        let dispatched = Arc::new(AtomicUsize::new(0));
        let task_dispatched = Arc::clone(&dispatched);
        let run = PendingRun::spawn((0, "session-one".into()), home.path().to_owned(),
            "demo".into(), Mode::Grantless, move || {
                started.send(()).unwrap();
                release_rx.recv().unwrap();
                Ok(native_plugins::test_installed_host_authority())
            }, move |_, _, _, _| {
                task_dispatched.fetch_add(1, Ordering::SeqCst);
                Ok(Payload::Value(17))
            }).unwrap();
        let phase = Arc::clone(&run.phase);
        started_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        let started = Instant::now();
        drop(run);
        assert!(started.elapsed() < Duration::from_secs(1));
        assert_eq!(phase.load(Ordering::Acquire), CANCELLED_BEFORE_DISPATCH);
        release.send(()).unwrap();
        let deadline = Instant::now() + Duration::from_secs(2);
        while phase.load(Ordering::Acquire) != FINISHED && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(1));
        }
        assert_eq!(phase.load(Ordering::Acquire), FINISHED);
        assert_eq!(dispatched.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn close_wins_race_after_host_gate_but_before_worker_admission() {
        let home = fixture(true, &[]);
        let home_path = home.path().to_owned();
        let cancel = Arc::new(AtomicBool::new(false));
        let phase = Arc::new(AtomicU8::new(CHECKING));
        let dispatched = Arc::new(AtomicUsize::new(0));
        let (at_claim, at_claim_rx) = mpsc::channel();
        let (release, release_rx) = mpsc::channel();
        let task_cancel = Arc::clone(&cancel);
        let task_phase = Arc::clone(&phase);
        let task_dispatched = Arc::clone(&dispatched);
        let task = thread::spawn(move || check_and_dispatch_with(&home_path, "demo", Mode::Grantless,
            &task_cancel, &task_phase,
            || Ok(native_plugins::test_installed_host_authority()),
            move || { at_claim.send(()).unwrap(); release_rx.recv().unwrap(); },
            move |_, _, _, _| { task_dispatched.fetch_add(1, Ordering::SeqCst); Ok(Payload::Value(17)) }));
        at_claim_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        cancel.store(true, Ordering::Release);
        assert_eq!(phase.compare_exchange(CHECKING, CANCELLED_BEFORE_DISPATCH,
            Ordering::AcqRel, Ordering::Acquire), Ok(CHECKING));
        release.send(()).unwrap();
        assert_eq!(task.join().unwrap(), RunOutcome::Cancelled);
        assert_eq!(dispatched.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn admitted_dispatch_blocks_owner_exit_until_supervisor_finishes() {
        let home = fixture(true, &[]);
        let (started, started_rx) = mpsc::channel();
        let (cancel_seen, cancel_seen_rx) = mpsc::channel();
        let (release, release_rx) = mpsc::channel();
        let run = PendingRun::spawn((0, "session-one".into()), home.path().to_owned(),
            "demo".into(), Mode::Grantless, || Ok(native_plugins::test_installed_host_authority()), move |_, _, _, cancel| {
                started.send(()).unwrap();
                while !cancel.load(Ordering::Acquire) {
                    thread::sleep(Duration::from_millis(1));
                }
                cancel_seen.send(()).unwrap();
                release_rx.recv().unwrap(); // model slow supervisor finalization
                Ok(Payload::Value(17))
            }).unwrap();
        started_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(run.phase.load(Ordering::Acquire), DISPATCH_ADMITTED);
        let (owner_exited, owner_exited_rx) = mpsc::channel();
        let closer = thread::spawn(move || { drop(run); owner_exited.send(()).unwrap(); });
        cancel_seen_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        assert!(owner_exited_rx.recv_timeout(Duration::from_millis(150)).is_err(),
            "owner exited before admitted supervisor finished");
        release.send(()).unwrap();
        owner_exited_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        closer.join().unwrap();
    }

    #[test]
    fn owner_switch_during_successful_dispatch_discards_late_return() {
        let home = fixture(true, &[]);
        let mut app = App::default();
        app.groups[0].tabs.push("session-one".into());
        app.groups[0].tabs.push("session-two".into());
        app.handle(Event::Resize(100, 30));
        let (started, started_rx) = mpsc::channel();
        let (cancel_seen, cancel_seen_rx) = mpsc::channel();
        let (release, release_rx) = mpsc::channel();
        let run = PendingRun::spawn(app.prompt_owner(), home.path().to_owned(),
            "demo".into(), Mode::Grantless, || Ok(native_plugins::test_installed_host_authority()),
            move |_, _, _, cancel| {
                started.send(()).unwrap();
                while !cancel.load(Ordering::Acquire) {
                    thread::sleep(Duration::from_millis(1));
                }
                cancel_seen.send(()).unwrap();
                release_rx.recv().unwrap();
                Ok(Payload::Value(17))
            }).unwrap();
        app.chip_info = Some(ChipInfo { kind: RUN_KIND, label: "demo".into(),
            lines: vec!["running".into()], scroll: 0, owner: None });
        app.native_package_run = Some(run);
        started_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        app.groups[0].active = 1;
        let releaser = thread::spawn(move || {
            cancel_seen_rx.recv_timeout(Duration::from_secs(2)).unwrap();
            release.send(()).unwrap();
        });
        assert!(app.poll_native_package_run());
        releaser.join().unwrap();
        assert!(app.native_package_run.is_none());
        assert!(app.chip_info.is_none());
        assert!(!app.poll_native_package_run());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn owner_close_waits_for_real_process_group_cancellation_and_reap() {
        for (mode, grants) in [(Mode::Grantless, &[][..]), (Mode::Panel, &[packages::RENDER_PANEL_GRANT][..])] {
            let home = fixture(true, grants);
            let marker = home.path().join("worker-started");
            let child_marker = marker.clone();
            let (cleaned, cleaned_rx) = mpsc::channel();
            let run = PendingRun::spawn((0, "session-one".into()), home.path().to_owned(),
                "demo".into(), mode, || Ok(native_plugins::test_installed_host_authority()), move |_, _, _, cancel| {
                    let mut command = std::process::Command::new("/bin/sh");
                    command.arg("-c").arg("printf ready > \"$1\"; exec /bin/sleep 30")
                        .arg("sh").arg(&child_marker);
                    let capture = crate::native_plugins::runner_process::supervise(
                        &mut command, cancel, Instant::now() + Duration::from_secs(5))?;
                    cleaned.send((capture.group_id, capture.outcome)).unwrap();
                    Ok(Payload::Value(0))
                }).unwrap();
            let deadline = Instant::now() + Duration::from_secs(2);
            while !marker.exists() && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(1));
            }
            assert!(marker.exists(), "{mode:?}: test child never started");
            drop(run);
            let (group_id, outcome) = cleaned_rx.recv_timeout(Duration::from_secs(1)).unwrap();
            assert_eq!(outcome, crate::native_plugins::runner_process::Outcome::Cancelled, "{mode:?}");
            assert_eq!(unsafe { libc::kill(group_id as i32, 0) }, -1, "{mode:?}: worker group survived");
        }
    }
}
