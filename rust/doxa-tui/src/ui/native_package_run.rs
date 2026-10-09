//! Lifecycle seam for an explicit grantless TUI request. No installed-host
//! admission authority exists yet, so production requests stop before dispatch.
use super::{App, ChipInfo};
use crate::native_plugins::packages;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc::{self, Receiver, TryRecvError}, Arc};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const RUN_KIND: &str = "native_package_run";
const CANCEL_JOIN_BUDGET: Duration = Duration::from_millis(100);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RunOutcome {
    Returned(i32),
    Cancelled,
    ApprovalRequired,
    GrantsRefused,
    PackageInvalid,
    PackageChanged,
    HostUnverified,
    RunnerFailed,
    WorkerStopped,
}

impl RunOutcome {
    fn lines(self) -> Vec<String> {
        let message = match self {
            Self::Returned(value) => return vec![format!("Isolated plugin returned {value}")],
            Self::Cancelled => "Run cancelled; no result was accepted",
            Self::ApprovalRequired => "Run refused: exact owner approval is required",
            Self::GrantsRefused => "Run refused: only zero-grant packages are eligible",
            Self::PackageInvalid => "Run refused: package preflight failed",
            Self::PackageChanged => "Run refused: approved package identity changed before admission",
            Self::HostUnverified => "TUI execution disabled: this installed host has no trusted delegation acceptance",
            Self::RunnerFailed => "Isolated worker failed; no result was accepted",
            Self::WorkerStopped => "Run task stopped; no result was accepted",
        };
        vec![message.into()]
    }
}

/// Production admission has deliberately no config or environment override.
/// A disposable-guest receipt is evidence for that guest, not an authority for
/// the installed TUI's parent process, cgroup delegation, and worker identity.
fn installed_host_admission() -> Result<(), RunOutcome> {
    Err(RunOutcome::HostUnverified)
}

fn recheck_and_admit<G>(home: &Path, review: &packages::Review, cancel: &AtomicBool, gate: G) -> Result<(), RunOutcome>
where G: FnOnce() -> Result<(), RunOutcome>
{
    if cancel.load(Ordering::Acquire) { return Err(RunOutcome::Cancelled); }
    if !review.owner_approved { return Err(RunOutcome::ApprovalRequired); }
    if !review.requested_grants.is_empty() { return Err(RunOutcome::GrantsRefused); }
    if packages::recheck_approved(home, review).is_err() { return Err(RunOutcome::PackageChanged); }
    if cancel.load(Ordering::Acquire) { return Err(RunOutcome::Cancelled); }
    gate()?;
    if cancel.load(Ordering::Acquire) { return Err(RunOutcome::Cancelled); }
    Ok(())
}

fn check_and_dispatch<G, F>(home: &Path, name: &str, cancel: &AtomicBool, gate: G, dispatch: F) -> RunOutcome
where
    G: FnOnce() -> Result<(), RunOutcome>,
    F: FnOnce(&Path, &packages::Review, &AtomicBool) -> io::Result<i32>,
{
    if cancel.load(Ordering::Acquire) { return RunOutcome::Cancelled; }
    let review = match packages::preflight(home, name) {
        Ok(review) => review,
        Err(_) => return RunOutcome::PackageInvalid,
    };
    if let Err(error) = recheck_and_admit(home, &review, cancel, gate) { return error; }
    match dispatch(home, &review, cancel) {
        Ok(value) if !cancel.load(Ordering::Acquire) => RunOutcome::Returned(value),
        Ok(_) => RunOutcome::Cancelled,
        Err(_) if cancel.load(Ordering::Acquire) => RunOutcome::Cancelled,
        Err(_) => RunOutcome::RunnerFailed,
    }
}

#[derive(Debug)]
pub(super) struct PendingRun {
    owner: (usize, String),
    name: String,
    cancel: Arc<AtomicBool>,
    receiver: Receiver<RunOutcome>,
    thread: Option<JoinHandle<()>>,
}

impl PendingRun {
    fn spawn<G, F>(owner: (usize, String), home: PathBuf, name: String, gate: G, dispatch: F) -> io::Result<Self>
    where
        G: FnOnce() -> Result<(), RunOutcome> + Send + 'static,
        F: FnOnce(&Path, &packages::Review, &AtomicBool) -> io::Result<i32> + Send + 'static,
    {
        let cancel = Arc::new(AtomicBool::new(false));
        let (sender, receiver) = mpsc::channel();
        let task_cancel = Arc::clone(&cancel);
        let task_name = name.clone();
        let thread = thread::Builder::new().name("doxa-native-package-request".into()).spawn(move || {
            let outcome = check_and_dispatch(&home, &task_name, &task_cancel, gate, dispatch);
            let _ = sender.send(outcome);
        })?;
        Ok(Self { owner, name, cancel, receiver, thread: Some(thread) })
    }
}

impl Drop for PendingRun {
    fn drop(&mut self) {
        self.cancel.store(true, Ordering::Release);
        if let Some(thread) = self.thread.take() {
            let deadline = Instant::now() + CANCEL_JOIN_BUDGET;
            while !thread.is_finished() && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(1));
            }
            // A stuck private-file read may outlive panel close. Detach only
            // after cancellation; the production gate forbids worker spawn.
            if thread.is_finished() { let _ = thread.join(); }
        }
    }
}

impl App {
    pub(super) fn open_native_package_run_at(&mut self, home: &Path, name: &str) {
        if self.native_package_run.is_some() {
            self.notice = "A native package request is already active".into();
            return;
        }
        let owner = self.prompt_owner();
        self.native_package_run_display_owner = None;
        self.chip_info = Some(ChipInfo {
            kind: RUN_KIND,
            label: name.into(),
            lines: vec!["Checking exact zero-grant approval and installed-host admission…".into()],
            scroll: 0,
            owner: None,
        });
        if self.active_chooser_rect().is_none() {
            self.chip_info = None;
            self.notice = "Enlarge active pane to inspect native package request".into();
            return;
        }
        match PendingRun::spawn(owner, home.to_owned(), name.to_owned(),
            installed_host_admission, crate::native_plugins::run_reviewed_grantless_package) {
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
    fn production_admission_never_dispatches_even_exact_approved_package() {
        let home = fixture(true, &[]);
        let cancel = AtomicBool::new(false);
        let outcome = check_and_dispatch(home.path(), "demo", &cancel,
            installed_host_admission, |_, _, _| panic!("worker dispatch must stay closed"));
        assert_eq!(outcome, RunOutcome::HostUnverified);
        assert_eq!(outcome.lines().len(), 1);
    }

    #[test]
    fn approval_grants_and_cancel_all_precede_host_gate() {
        for (approved, grants, expected) in [
            (false, &[][..], RunOutcome::ApprovalRequired),
            (true, &["render-local-panel-v1"][..], RunOutcome::GrantsRefused),
        ] {
            let home = fixture(approved, grants);
            let cancel = AtomicBool::new(false);
            let result = check_and_dispatch(home.path(), "demo", &cancel,
                || panic!("gate must not see rejected package"),
                |_, _, _| panic!("worker must not start"));
            assert_eq!(result, expected);
        }
        let home = fixture(true, &[]);
        let result = check_and_dispatch(home.path(), "demo", &AtomicBool::new(true),
            || panic!("gate must not see cancelled request"),
            |_, _, _| panic!("worker must not start"));
        assert_eq!(result, RunOutcome::Cancelled);
    }

    #[test]
    fn preflight_identity_cannot_be_used_after_bytes_change() {
        let home = fixture(true, &[]);
        let review = packages::preflight(home.path(), "demo").unwrap();
        private(&home.path().join("native-plugin-packages/demo/module.wasm"), b"\0asm\x01\0\0\0");
        assert_eq!(recheck_and_admit(home.path(), &review, &AtomicBool::new(false),
            || panic!("stale bytes must not reach installed-host gate")),
            Err(RunOutcome::PackageChanged));
    }

    fn running_app(home: &Path) -> (App, Receiver<()>) {
        let mut app = App::default();
        app.groups[0].tabs.push("session-one".into());
        app.handle(Event::Resize(100, 30));
        let (started, started_rx) = mpsc::channel();
        let (cancelled, cancelled_rx) = mpsc::channel();
        let run = PendingRun::spawn(app.prompt_owner(), home.to_owned(), "demo".into(),
            || Ok(()), move |_, _, cancel| {
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
        let home = fixture(true, &[]);
        let (mut app, cancelled) = running_app(home.path());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(app.native_package_run.is_none());
        cancelled.recv_timeout(Duration::from_secs(1)).unwrap();

        let (mut app, cancelled) = running_app(home.path());
        app.detach_active_tab();
        assert!(app.native_package_run.is_none());
        cancelled.recv_timeout(Duration::from_secs(1)).unwrap();

        let (app, cancelled) = running_app(home.path());
        drop(app);
        cancelled.recv_timeout(Duration::from_secs(1)).unwrap();

        let (mut app, cancelled) = running_app(home.path());
        app.should_quit = true;
        assert!(app.reconcile_native_package_run());
        assert!(app.native_package_run.is_none());
        cancelled.recv_timeout(Duration::from_secs(1)).unwrap();
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
        let run = PendingRun::spawn(app.prompt_owner(), home.path().to_owned(), "demo".into(),
            || Ok(()), move |_, _, _| { called.fetch_add(1, Ordering::SeqCst); Ok(17) }).unwrap();
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
        let run = PendingRun::spawn((0, "session-one".into()), home.path().to_owned(),
            "demo".into(), || Ok(()), move |_, _, _| {
                started.send(()).unwrap();
                release_rx.recv().unwrap();
                Ok(1)
            }).unwrap();
        started_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        let started = Instant::now();
        drop(run);
        assert!(started.elapsed() < Duration::from_secs(1));
        release.send(()).unwrap();
    }
}
