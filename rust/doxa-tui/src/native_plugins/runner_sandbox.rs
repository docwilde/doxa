//! Linux sandbox admission for the explicit grantless plugin CLI prototype.
//! A command is constructed only after a private cgroup v2 budget is installed.
//! The TUI does not call this module. Disposable delegated-host proof passed;
//! installed-host acceptance remains required before broader activation.
#![cfg(target_os = "linux")]

use std::collections::HashSet;
use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use std::sync::atomic::AtomicBool;
use std::thread;
use std::time::{Duration, Instant};
use serde_json::Value;

const BWRAP: &str = "/usr/bin/bwrap";
const CGROUP_ROOT: &str = "/sys/fs/cgroup";
const MEMORY_MAX: &str = "268435456"; // 256 MiB, including Wasmi compilation
const PIDS_MAX: &str = "16";
const CPU_MAX: &str = "100000 100000"; // at most one CPU of aggregate bandwidth
const PROCESS_AS_BYTES: libc::rlim_t = 256 * 1024 * 1024;
const PROCESS_CPU_SECONDS: libc::rlim_t = 4;
const PROCESS_FDS: libc::rlim_t = 64;
const CGROUP_EVENTS_MAX_BYTES: u64 = 4_096;

fn unavailable(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message.into())
}

/// A populated leaf cannot enable domain controllers for its own children.
/// Require the caller in a supervisor leaf immediately below an empty,
/// owner-delegated parent, and place each worker in a sibling of that leaf.
fn delegated_parent_at(root: &Path, membership: &str, uid: u32, pid: u32) -> io::Result<PathBuf> {
    let mut unified = membership.lines().filter_map(|line| line.strip_prefix("0::"));
    let relative = unified.next()
        .ok_or_else(|| unavailable("unified cgroup v2 membership unavailable"))?;
    if unified.next().is_some() {
        return Err(unavailable("ambiguous cgroup v2 membership"));
    }
    if relative.len() > 1024 || !relative.starts_with('/')
        || Path::new(relative).components().any(|component| !matches!(component, Component::RootDir | Component::Normal(_))) {
        return Err(unavailable("invalid cgroup v2 membership"));
    }
    let leaf = root.join(relative.trim_start_matches('/'));
    if leaf == root { return Err(unavailable("plugin supervisor leaf is the cgroup root")); }
    let parent = leaf.parent().filter(|parent| *parent != root && parent.starts_with(root))
        .ok_or_else(|| unavailable("plugin supervisor leaf lacks a delegated parent"))?;
    let mut group = leaf.as_path();
    while group != root {
        let metadata = fs::symlink_metadata(group)?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err(unavailable("cgroup membership traverses a non-directory or symlink"));
        }
        group = group.parent().ok_or_else(|| unavailable("cgroup membership escaped the hierarchy"))?;
    }
    let leaf_procs = fs::read_to_string(leaf.join("cgroup.procs"))?;
    if !leaf_procs.lines().any(|line| line.parse::<u32>().ok() == Some(pid)) {
        return Err(unavailable("caller is not in the supervisor leaf"));
    }
    let metadata = fs::symlink_metadata(parent)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink()
        || metadata.uid() != uid
        || metadata.permissions().mode() & 0o200 == 0 {
        return Err(unavailable("parent cgroup is not delegated to this user"));
    }
    if !fs::read_to_string(parent.join("cgroup.procs"))?.trim().is_empty() {
        return Err(unavailable("delegated parent contains processes"));
    }
    let enabled = fs::read_to_string(parent.join("cgroup.subtree_control"))?;
    if !["memory", "pids", "cpu"].iter().all(|controller| enabled.split_whitespace().any(|value| value == *controller)) {
        return Err(unavailable("memory, pids and cpu cgroup controllers are not delegated"));
    }
    Ok(parent.to_path_buf())
}

fn delegated_cgroup_parent() -> io::Result<PathBuf> {
    let mut stat: libc::statfs = unsafe { std::mem::zeroed() };
    let root_c = CString::new(CGROUP_ROOT).unwrap();
    if unsafe { libc::statfs(root_c.as_ptr(), &mut stat) } < 0 {
        return Err(io::Error::last_os_error());
    }
    if stat.f_type != libc::CGROUP2_SUPER_MAGIC as libc::c_long {
        return Err(unavailable("cgroup hierarchy is not cgroup v2"));
    }
    let membership = fs::read_to_string("/proc/self/cgroup")?;
    delegated_parent_at(Path::new(CGROUP_ROOT), &membership,
        unsafe { libc::geteuid() }, std::process::id())
}

fn write_and_check(path: &Path, value: &str) -> io::Result<()> {
    fs::write(path, value)?;
    let observed = fs::read_to_string(path)?;
    if observed.trim() != value {
        return Err(unavailable(format!("cgroup limit was not installed: {}", path.display())));
    }
    Ok(())
}

/// A successful cleanup requires one unambiguous kernel `populated` value.
/// A missing, duplicated, malformed, linked or oversized events file cannot
/// be used as evidence that all worker descendants have exited.
fn cgroup_populated(events_path: &Path) -> io::Result<bool> {
    let file = OpenOptions::new().read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
        .open(events_path)?;
    if !file.metadata()?.is_file() {
        return Err(unavailable("plugin cgroup events is not a regular control file"));
    }
    let mut bytes = Vec::new();
    file.take(CGROUP_EVENTS_MAX_BYTES + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > CGROUP_EVENTS_MAX_BYTES {
        return Err(unavailable("plugin cgroup events exceeded read limit"));
    }
    let text = std::str::from_utf8(&bytes)
        .map_err(|_| unavailable("plugin cgroup events is not UTF-8"))?;
    let mut populated = None;
    let mut seen = HashSet::new();
    for line in text.lines() {
        let mut fields = line.split_whitespace();
        let key = fields.next().ok_or_else(|| unavailable("empty plugin cgroup events row"))?;
        let value = fields.next().ok_or_else(|| unavailable("missing plugin cgroup events value"))?;
        if fields.next().is_some() || !key.bytes().all(|byte| byte.is_ascii_lowercase() || byte == b'_')
            || !seen.insert(key) || !value.bytes().all(|byte| byte.is_ascii_digit())
            || value.parse::<u64>().is_err() {
            return Err(unavailable("malformed or duplicate plugin cgroup events row"));
        }
        if key == "populated" {
            populated = Some(match value {
                "0" => false,
                "1" => true,
                _ => return Err(unavailable("invalid plugin cgroup populated value")),
            });
        }
    }
    populated.ok_or_else(|| unavailable("missing plugin cgroup populated value"))
}

/// The cgroup owns all descendants, including a compromised child that calls
/// setsid() to escape the supervisor's process group. Drop kills the entire
/// cgroup before attempting to remove it.
pub(crate) struct CgroupBudget { path: PathBuf, stopped: bool }

impl CgroupBudget {
    pub(crate) fn create() -> io::Result<Self> {
        let parent = delegated_cgroup_parent()?;
        let path = parent.join(format!("doxa-plugin-{}-{}", std::process::id(), uuid::Uuid::new_v4()));
        fs::create_dir(&path)?;
        let budget = Self { path, stopped: false };
        write_and_check(&budget.path.join("memory.max"), MEMORY_MAX)?;
        write_and_check(&budget.path.join("pids.max"), PIDS_MAX)?;
        write_and_check(&budget.path.join("cpu.max"), CPU_MAX)?;
        write_and_check(&budget.path.join("memory.swap.max"), "0")?;
        Ok(budget)
    }

    pub(crate) fn stop(&mut self) -> io::Result<()> {
        if self.stopped { return Ok(()); }
        // cgroup.kill is authoritative even if the worker changes PGID.
        fs::write(self.path.join("cgroup.kill"), "1")?;
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            if !cgroup_populated(&self.path.join("cgroup.events"))? { break; }
            if Instant::now() >= deadline {
                return Err(unavailable("plugin cgroup retained live descendants after kill"));
            }
            thread::sleep(Duration::from_millis(5));
        }
        fs::remove_dir(&self.path)?;
        self.stopped = true;
        Ok(())
    }

    fn procs_path(&self) -> io::Result<CString> {
        CString::new(self.path.join("cgroup.procs").as_os_str().as_bytes())
            .map_err(|_| unavailable("invalid cgroup path"))
    }
}

impl Drop for CgroupBudget {
    fn drop(&mut self) { let _ = self.stop(); }
}

#[cfg(test)]
#[path = "runner_sandbox_acceptance.rs"]
mod acceptance;

fn open_trusted_executable(path: &Path) -> io::Result<File> {
    if !path.is_absolute() { return Err(unavailable("plugin worker path must be absolute")); }
    let file = OpenOptions::new().read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK).open(path)?;
    let metadata = file.metadata()?;
    if !path.is_absolute() || !metadata.is_file()
        || ![0, unsafe { libc::geteuid() }].contains(&metadata.uid())
        || metadata.permissions().mode() & 0o022 != 0
        || metadata.permissions().mode() & 0o111 == 0
        || metadata.nlink() != 1 {
        return Err(unavailable("plugin worker executable is not private or root-owned"));
    }
    Ok(file)
}

fn status_memfd() -> io::Result<File> {
    let label = CString::new("doxa-plugin-bwrap-status").unwrap();
    let fd = unsafe { libc::memfd_create(label.as_ptr(), libc::MFD_CLOEXEC) };
    if fd < 0 { return Err(io::Error::last_os_error()); }
    Ok(unsafe { File::from_raw_fd(fd) })
}

unsafe fn set_limit(resource: libc::__rlimit_resource_t, value: libc::rlim_t) -> io::Result<()> {
    let limit = libc::rlimit { rlim_cur: value, rlim_max: value };
    if libc::setrlimit(resource, &limit) < 0 { return Err(io::Error::last_os_error()); }
    Ok(())
}

unsafe fn apply_process_limits() -> io::Result<()> {
    set_limit(libc::RLIMIT_AS, PROCESS_AS_BYTES)?;
    set_limit(libc::RLIMIT_CPU, PROCESS_CPU_SECONDS)?;
    set_limit(libc::RLIMIT_NOFILE, PROCESS_FDS)?;
    set_limit(libc::RLIMIT_CORE, 0)?;
    if libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0 {
        return Err(io::Error::last_os_error());
    }
    // Mark every ambient descriptor close-on-exec, including descriptors a
    // caller opened without O_CLOEXEC. Rust's spawn error pipe remains usable
    // until exec, so a failed setup is still reported to the parent.
    if libc::syscall(libc::SYS_close_range, 3u32, u32::MAX, libc::CLOSE_RANGE_CLOEXEC) < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

struct WorkerCommand { command: Command, status: File }

/// The worker is opened with O_NOFOLLOW and mounted from that opened inode.
/// Replacing its pathname after this call cannot change the bytes executed.
/// The separate status descriptor belongs to Bubblewrap and is not mounted
/// into the child; it distinguishes setup failure from a started child.
fn command_impl_with_bwrap(bwrap: &Path, worker: &Path, args: &[&str], cgroup: Option<&CgroupBudget>) -> io::Result<WorkerCommand> {
    let bwrap_file = open_trusted_executable(bwrap)?;
    let worker_file = open_trusted_executable(worker)?;
    let status = status_memfd()?;
    let status_child = status.try_clone()?;
    let bwrap_fd = bwrap_file.as_raw_fd();
    let bwrap_metadata = bwrap_file.metadata()?;
    let worker_fd = worker_file.as_raw_fd();
    let status_fd = status_child.as_raw_fd();
    if bwrap_fd == worker_fd || bwrap_fd == status_fd || worker_fd == status_fd {
        return Err(unavailable("plugin executable and status descriptors collided"));
    }
    let procs = cgroup.map(CgroupBudget::procs_path).transpose()?;
    // execve resolves this descriptor in the forked child. The descriptor is
    // intentionally close-on-exec: the kernel resolves the checked ELF before
    // closing it, and Bubblewrap cannot pass the descriptor to its worker.
    // A replacement of bwrap's pathname after validation cannot change the
    // wrapper that starts. The pre-exec closure owns the file until execve.
    let mut command = Command::new(format!("/proc/self/fd/{bwrap_fd}"));
    command.env_clear().current_dir("/");
    command.args(["--json-status-fd", &status_fd.to_string(),
        "--unshare-all", "--unshare-user", "--die-with-parent", "--disable-userns",
        "--cap-drop", "ALL", "--clearenv", "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--ro-bind-fd", &worker_fd.to_string(), "/worker",
        "--proc", "/proc", "--dev", "/dev", "--size", "16777216",
        "--tmpfs", "/tmp", "--chdir", "/", "--", "/worker"]);
    command.args(args);
    // SAFETY: only async-signal-safe libc calls run between fork and exec.
    // The cgroup write moves the untrusted child before bwrap can execute.
    // The two explicitly passed descriptors are reopened after close_range;
    // Rust's spawn error pipe remains close-on-exec and is never overwritten.
    unsafe { command.pre_exec(move || {
        if let Some(procs) = &procs {
            let fd = libc::open(procs.as_ptr(), libc::O_WRONLY | libc::O_CLOEXEC);
            if fd < 0 { return Err(io::Error::last_os_error()); }
            let wrote = libc::write(fd, b"0".as_ptr().cast(), 1);
            let saved_error = io::Error::last_os_error();
            libc::close(fd);
            if wrote != 1 { return Err(saved_error); }
        }
        apply_process_limits()?;
        for fd in [worker_file.as_raw_fd(), status_child.as_raw_fd()] {
            if libc::fcntl(fd, libc::F_SETFD, 0) < 0 { return Err(io::Error::last_os_error()); }
        }
        let mut observed: libc::stat = std::mem::zeroed();
        if libc::fstat(bwrap_file.as_raw_fd(), &mut observed) < 0 {
            return Err(io::Error::last_os_error());
        }
        if observed.st_dev != bwrap_metadata.dev() as libc::dev_t
            || observed.st_ino != bwrap_metadata.ino() as libc::ino_t {
            return Err(io::Error::from_raw_os_error(libc::EACCES));
        }
        Ok(())
    }); }
    Ok(WorkerCommand { command, status })
}

fn command_impl(worker: &Path, args: &[&str], cgroup: Option<&CgroupBudget>) -> io::Result<WorkerCommand> {
    command_impl_with_bwrap(Path::new(BWRAP), worker, args, cgroup)
}

fn command(worker: &Path, cgroup: &CgroupBudget) -> io::Result<WorkerCommand> {
    command_impl(worker, &[], Some(cgroup))
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
struct WrapperStatus { child_started: bool, exit_code: Option<i32> }

fn read_wrapper_status(mut file: File) -> io::Result<WrapperStatus> {
    file.seek(SeekFrom::Start(0))?;
    let mut bytes = Vec::new();
    file.take(4097).read_to_end(&mut bytes)?;
    if bytes.len() > 4096 { return Err(unavailable("Bubblewrap status exceeded limit")); }
    let mut status = WrapperStatus::default();
    let mut rows = 0;
    for line in bytes.split(|byte| *byte == b'\n').filter(|line| !line.is_empty()) {
        rows += 1;
        if rows > 4 { return Err(unavailable("too many Bubblewrap status rows")); }
        let value: Value = serde_json::from_slice(line)
            .map_err(|_| unavailable("invalid Bubblewrap status JSON"))?;
        let object = value.as_object().ok_or_else(|| unavailable("invalid Bubblewrap status object"))?;
        if let Some(pid) = object.get("child-pid") {
            if status.child_started || pid.as_u64().is_none_or(|pid| pid == 0) {
                return Err(unavailable("invalid Bubblewrap child status"));
            }
            status.child_started = true;
        }
        if let Some(code) = object.get("exit-code") {
            if status.exit_code.is_some() {
                return Err(unavailable("duplicate Bubblewrap exit status"));
            }
            status.exit_code = Some(code.as_i64().and_then(|code| i32::try_from(code).ok())
                .ok_or_else(|| unavailable("invalid Bubblewrap exit code"))?);
        }
    }
    Ok(status)
}

/// Worker results require an entry receipt and matching wrapper exit. A
/// worker that crashes after its receipt is distinct from failure before its
/// entry. Bubblewrap's child-pid is a namespace helper, not proof that the
/// worker binary reached main; pre-entry failures remain one conservative
/// class. It also cannot distinguish a signal from deliberate nonzero exit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum IsolatedOutcome {
    Return(i32),
    ModuleFailure(super::runner::WorkerFailure),
    Timeout,
    Cancelled,
    OutputLimit,
    WorkerAbnormalExit(i32),
    SandboxOrPreEntryFailure(i32),
    WrapperCrash(i32),
    ProtocolFailure,
}

fn classify(capture: &super::runner_process::Capture, status: WrapperStatus) -> IsolatedOutcome {
    use super::runner_process::Outcome;
    match capture.outcome {
        Outcome::Timeout => IsolatedOutcome::Timeout,
        Outcome::Cancelled => IsolatedOutcome::Cancelled,
        Outcome::OutputLimit => IsolatedOutcome::OutputLimit,
        Outcome::Crash(signal) => IsolatedOutcome::WrapperCrash(signal),
        Outcome::Exit(code) if !capture.stderr.starts_with(super::runner::READY_MARKER) =>
            IsolatedOutcome::SandboxOrPreEntryFailure(code),
        Outcome::Exit(code) if !status.child_started || status.exit_code != Some(code) =>
            IsolatedOutcome::ProtocolFailure,
        Outcome::Exit(0) if capture.stderr != super::runner::READY_MARKER =>
            IsolatedOutcome::ProtocolFailure,
        Outcome::Exit(0) => match super::runner::decode_response(capture.stdout.as_slice()) {
            Ok(Ok(value)) => IsolatedOutcome::Return(value),
            Ok(Err(failure)) => IsolatedOutcome::ModuleFailure(failure),
            Err(_) => IsolatedOutcome::ProtocolFailure,
        },
        Outcome::Exit(code) => IsolatedOutcome::WorkerAbnormalExit(code),
    }
}

/// CLI containment seam. It rechecks exact owner approval before spawn,
/// sends the verified bytes through one bounded pipe, and kills the entire
/// cgroup after every process outcome. A disposable delegated-host fixture
/// verified aggregate containment; each installed host still needs acceptance.
pub(crate) fn supervise_reviewed(
    home: &Path,
    review: &super::packages::Review,
    worker: &Path,
    cancel: &AtomicBool,
    deadline: Instant,
) -> io::Result<IsolatedOutcome> {
    let package = super::packages::recheck_approved(home, review)?;
    let frame = super::runner::encode_request(&package)?;
    let mut budget = CgroupBudget::create()?;
    let WorkerCommand { mut command, status } = command(worker, &budget)?;
    let result = super::runner_process::supervise_with_input(
        &mut command, Some(&frame), cancel, deadline);
    budget.stop()?;
    let capture = result?;
    if matches!(capture.outcome, super::runner_process::Outcome::Timeout
        | super::runner_process::Outcome::Cancelled
        | super::runner_process::Outcome::OutputLimit) {
        return Ok(classify(&capture, WrapperStatus::default()));
    }
    let status = read_wrapper_status(status)?;
    Ok(classify(&capture, status))
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::runner_process::{supervise, supervise_with_input, Outcome};
    use std::sync::atomic::AtomicBool;
    use std::net::TcpListener;
    use std::os::fd::AsRawFd;
    use std::process::Stdio;

    fn fixture(script: &str) -> Command {
        let mut command = Command::new(BWRAP);
        command.args(["--unshare-all", "--unshare-user", "--die-with-parent", "--disable-userns",
            "--clearenv", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--size", "16777216",
            "--tmpfs", "/tmp", "--", "/bin/sh", "-c", script]);
        // SAFETY: this uses the launcher's async-signal-safe pre-exec limits.
        unsafe { command.pre_exec(|| apply_process_limits()); }
        command
    }

    fn sandbox_available() -> bool {
        let features = Command::new(BWRAP).arg("--help").output()
            .is_ok_and(|output| {
                let help = String::from_utf8_lossy(&output.stdout);
                help.contains("--ro-bind-fd") && help.contains("--json-status-fd")
            });
        features
            && fixture("exit 0").stdout(Stdio::null()).stderr(Stdio::null())
                .status().is_ok_and(|status| status.success())
    }

    fn script(path: &Path, body: &str) {
        fs::write(path, format!("#!/bin/sh\n{body}\n")).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    }

    fn run_bwrap(launch: WorkerCommand) -> (super::super::runner_process::Capture, WrapperStatus) {
        let WorkerCommand { mut command, status } = launch;
        let capture = supervise_with_input(&mut command, None, &AtomicBool::new(false),
            Instant::now() + Duration::from_secs(3)).unwrap();
        let wrapper = read_wrapper_status(status).unwrap();
        (capture, wrapper)
    }

    fn fake_delegated_tree() -> (tempfile::TempDir, PathBuf, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("cgroup");
        let parent = root.join("delegated");
        let leaf = parent.join("supervisor");
        fs::create_dir_all(&leaf).unwrap();
        fs::write(parent.join("cgroup.procs"), "").unwrap();
        fs::write(parent.join("cgroup.subtree_control"), "cpu memory pids\n").unwrap();
        fs::write(leaf.join("cgroup.procs"), "4242\n").unwrap();
        (dir, root, parent)
    }

    #[test]
    fn cleanup_events_require_one_bounded_unambiguous_populated_value() {
        let dir = tempfile::tempdir().unwrap();
        let events = dir.path().join("cgroup.events");
        fs::write(&events, "populated 0\nfrozen 0\n").unwrap();
        assert_eq!(cgroup_populated(&events).unwrap(), false);
        fs::write(&events, "populated 1\nfrozen 0\n").unwrap();
        assert_eq!(cgroup_populated(&events).unwrap(), true);
        for invalid in [
            "", "frozen 0\n", "populated 00\n", "populated 2\n",
            "populated 0 1\n", "populated 0\npopulated 0\n",
            "populated 0\npopulated 1\n", "populated \u{0}0\n",
            "populated 0\nfrozen nope\n", "populated 0\nfrozen 0\nfrozen 0\n",
            "populated 0\n\n",
        ] {
            fs::write(&events, invalid).unwrap();
            assert!(cgroup_populated(&events).is_err(), "accepted {invalid:?}");
        }
        fs::write(&events, vec![b'x'; CGROUP_EVENTS_MAX_BYTES as usize + 1]).unwrap();
        assert!(cgroup_populated(&events).is_err());
        let link = dir.path().join("events-link");
        std::os::unix::fs::symlink(&events, &link).unwrap();
        assert!(cgroup_populated(&link).is_err());
        assert!(cgroup_populated(dir.path()).is_err());
    }

    #[test]
    fn malformed_cleanup_evidence_does_not_mark_budget_stopped() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("worker-cgroup");
        fs::create_dir(&path).unwrap();
        fs::write(path.join("cgroup.kill"), "").unwrap();
        fs::write(path.join("cgroup.events"), "populated 0\npopulated 1\n").unwrap();
        let mut budget = CgroupBudget { path: path.clone(), stopped: false };
        assert!(budget.stop().is_err());
        assert!(!budget.stopped && path.exists());
        budget.stopped = true; // The fake regular-file tree is not a cgroupfs mount.
    }

    #[test]
    fn delegated_parent_is_empty_controller_enabled_sibling_of_supervisor() {
        let (_dir, root, parent) = fake_delegated_tree();
        let uid = unsafe { libc::geteuid() };
        assert_eq!(delegated_parent_at(&root, "0::/delegated/supervisor\n", uid, 4242).unwrap(), parent);
        assert!(delegated_parent_at(&root, "0::/delegated/supervisor\n", uid, 9999).is_err());
        assert!(delegated_parent_at(&root, "0::/delegated\n", uid, 4242).is_err());
        assert!(delegated_parent_at(&root, "0::/\n", uid, 4242).is_err());
    }

    #[test]
    fn delegated_parent_rejects_populated_unowned_unwritable_or_disabled_parent() {
        let (_dir, root, parent) = fake_delegated_tree();
        let uid = unsafe { libc::geteuid() };
        let membership = "0::/delegated/supervisor\n";
        fs::write(parent.join("cgroup.procs"), "42\n").unwrap();
        assert!(delegated_parent_at(&root, membership, uid, 4242).is_err());
        fs::write(parent.join("cgroup.procs"), "").unwrap();
        fs::write(parent.join("cgroup.subtree_control"), "cpu pids\n").unwrap();
        assert!(delegated_parent_at(&root, membership, uid, 4242).is_err());
        fs::write(parent.join("cgroup.subtree_control"), "cpu memory pids\n").unwrap();
        assert!(delegated_parent_at(&root, membership, uid.wrapping_add(1), 4242).is_err());
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o500)).unwrap();
        assert!(delegated_parent_at(&root, membership, uid, 4242).is_err());
        fs::set_permissions(&parent, fs::Permissions::from_mode(0o700)).unwrap();
    }

    #[test]
    fn delegated_parent_rejects_ambiguous_paths_and_symlinked_parent() {
        let (_dir, root, parent) = fake_delegated_tree();
        let uid = unsafe { libc::geteuid() };
        for membership in ["0::/delegated/../supervisor\n", "0::/delegated/supervisor\n0::/other\n",
            "1:name=systemd:/delegated/supervisor\n"] {
            assert!(delegated_parent_at(&root, membership, uid, 4242).is_err(), "{membership}");
        }
        std::os::unix::fs::symlink(&parent, root.join("linked")).unwrap();
        assert!(delegated_parent_at(&root, "0::/linked/supervisor\n", uid, 4242).is_err());
        let nested = parent.join("supervisor/nested");
        fs::create_dir(&nested).unwrap();
        fs::write(parent.join("supervisor/cgroup.procs"), "").unwrap();
        fs::write(parent.join("supervisor/cgroup.subtree_control"), "cpu memory pids\n").unwrap();
        fs::write(nested.join("cgroup.procs"), "4242\n").unwrap();
        assert!(delegated_parent_at(&root, "0::/linked/supervisor/nested\n", uid, 4242).is_err());
    }

    #[test]
    fn trusted_executable_rejects_world_writable_file() {
        let dir = tempfile::tempdir().unwrap();
        let worker = dir.path().join("worker");
        fs::write(&worker, b"worker").unwrap();
        fs::set_permissions(&worker, fs::Permissions::from_mode(0o777)).unwrap();
        assert!(open_trusted_executable(&worker).is_err());
    }

    #[test]
    fn opened_worker_inode_survives_path_replacement() {
        if !sandbox_available() { return; }
        let dir = tempfile::tempdir().unwrap();
        let worker = dir.path().join("worker");
        script(&worker, "printf 'DOXA-WORKER-READY-v1\\n' >&2; printf 'DOXAR1\\000\\000\\000\\052\\000\\000\\000'");
        let launch = command_impl(&worker, &[], None).unwrap();
        fs::rename(&worker, dir.path().join("original")).unwrap();
        script(&worker, "printf 'replaced'");
        let (capture, wrapper) = run_bwrap(launch);
        assert!(wrapper.child_started);
        assert_eq!(classify(&capture, wrapper), IsolatedOutcome::Return(42));
    }

    #[test]
    fn opened_wrapper_inode_survives_path_replacement() {
        if !sandbox_available() { return; }
        let dir = tempfile::tempdir().unwrap();
        let bwrap = dir.path().join("bwrap");
        fs::copy(BWRAP, &bwrap).unwrap();
        fs::set_permissions(&bwrap, fs::Permissions::from_mode(0o700)).unwrap();
        let worker = dir.path().join("worker");
        script(&worker, "printf 'DOXA-WORKER-READY-v1\\n' >&2; printf 'DOXAR1\\000\\000\\000\\052\\000\\000\\000'");
        let launch = command_impl_with_bwrap(&bwrap, &worker, &[], None).unwrap();
        fs::rename(&bwrap, dir.path().join("original-wrapper")).unwrap();
        script(&bwrap, "printf 'replacement-wrapper'; exit 9");
        let (capture, wrapper) = run_bwrap(launch);
        assert!(wrapper.child_started);
        assert_eq!(classify(&capture, wrapper), IsolatedOutcome::Return(42));
    }

    #[test]
    fn wrapper_descriptors_are_not_exposed_to_worker() {
        if !sandbox_available() { return; }
        let dir = tempfile::tempdir().unwrap();
        let worker = dir.path().join("worker");
        script(&worker, "ls -l /proc/self/fd | grep -E 'doxa-plugin-bwrap-status|/usr/bin/bwrap|/home/' >/dev/null && exit 9; printf 'DOXA-WORKER-READY-v1\\n' >&2; printf 'DOXAR1\\000\\000\\000\\001\\000\\000\\000'");
        let (capture, wrapper) = run_bwrap(command_impl(&worker, &[], None).unwrap());
        assert_eq!(classify(&capture, wrapper), IsolatedOutcome::Return(1));
    }

    #[test]
    fn started_child_crash_and_protocol_failure_are_distinct() {
        if !sandbox_available() { return; }
        let dir = tempfile::tempdir().unwrap();
        let worker = dir.path().join("worker");
        script(&worker, "printf 'DOXA-WORKER-READY-v1\\n' >&2; kill -KILL $$");
        let (capture, wrapper) = run_bwrap(command_impl(&worker, &[], None).unwrap());
        assert!(wrapper.child_started);
        assert_eq!(classify(&capture, wrapper), IsolatedOutcome::WorkerAbnormalExit(137));

        script(&worker, "printf 'DOXA-WORKER-READY-v1\\n' >&2; exit 0");
        let (capture, wrapper) = run_bwrap(command_impl(&worker, &[], None).unwrap());
        assert_eq!(classify(&capture, wrapper), IsolatedOutcome::ProtocolFailure);
    }

    #[test]
    fn wrapper_setup_failure_has_no_child_start_receipt() {
        if !sandbox_available() { return; }
        let status = status_memfd().unwrap();
        let fd = status.as_raw_fd();
        let mut command = Command::new(BWRAP);
        command.args(["--json-status-fd", &fd.to_string(), "--unshare-all", "--unshare-user",
            "--ro-bind", "/does-not-exist-doxa-plugin", "/worker", "--", "/worker"]);
        // SAFETY: only the trusted wrapper gets this explicitly passed FD.
        unsafe { command.pre_exec(move || {
            apply_process_limits()?;
            if libc::fcntl(fd, libc::F_SETFD, 0) < 0 { return Err(io::Error::last_os_error()); }
            Ok(())
        }); }
        let capture = supervise(&mut command, &AtomicBool::new(false),
            Instant::now() + Duration::from_secs(3)).unwrap();
        let wrapper = read_wrapper_status(status).unwrap();
        // Bubblewrap may report its namespace helper PID even when a mount
        // fails before the worker enters. Only the worker receipt proves entry.
        assert!(!capture.stderr.starts_with(super::super::runner::READY_MARKER));
        assert!(matches!(classify(&capture, wrapper), IsolatedOutcome::SandboxOrPreEntryFailure(_)));
    }

    #[test]
    fn namespace_fixture_hides_host_file_environment_and_network() {
        if !sandbox_available() { return; }
        let marker = tempfile::tempdir().unwrap();
        let secret = marker.path().join("private-marker");
        fs::write(&secret, b"secret").unwrap();
        let script = format!("test ! -e '{}' && test -z \"$DOXA_PLUGIN_TEST_SECRET\" && ! cat /proc/net/route | grep -q '^eth' && test ! -e /home && ! touch /usr/doxa-plugin-write-probe 2>/dev/null", secret.display());
        let mut command = fixture(&script);
        command.env("DOXA_PLUGIN_TEST_SECRET", "do-not-inherit");
        let status = command.stdout(Stdio::null()).stderr(Stdio::null()).status().unwrap();
        assert!(status.success(), "namespace fixture did not hide host files, mounts, environment or network");
    }

    #[test]
    fn namespace_fixture_cannot_connect_to_host_listener() {
        if !sandbox_available() { return; }
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let port = listener.local_addr().unwrap().port();
        let script = format!("/bin/bash -c 'exec 3<>/dev/tcp/127.0.0.1/{port}'");
        let status = fixture(&script).stdout(Stdio::null()).stderr(Stdio::null()).status().unwrap();
        assert!(!status.success(), "sandbox connected to host TCP listener");
        assert_eq!(listener.accept().unwrap_err().kind(), io::ErrorKind::WouldBlock);
    }

    #[test]
    fn namespace_fixture_closes_ambient_descriptor() {
        if !sandbox_available() { return; }
        let file = tempfile::tempfile().unwrap();
        let inherited = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_DUPFD, 200) };
        assert!(inherited >= 200);
        let script = format!("test ! -e /proc/self/fd/{inherited}");
        let status = fixture(&script).stdout(Stdio::null()).stderr(Stdio::null()).status().unwrap();
        unsafe { libc::close(inherited); }
        assert!(status.success(), "ambient descriptor survived sandbox exec");
    }

    #[test]
    fn process_memory_budget_rejects_exhaustion() {
        let mut command = Command::new("/usr/bin/python3");
        command.args(["-c", "import resource\nassert resource.getrlimit(resource.RLIMIT_AS)[0] == 268435456\ntry:\n bytearray(512 * 1024 * 1024)\n raise AssertionError('allocation escaped budget')\nexcept MemoryError:\n pass"]);
        // SAFETY: the same async-signal-safe setup used by the staged launcher.
        unsafe { command.pre_exec(|| apply_process_limits()); }
        let status = command.stdout(Stdio::null()).stderr(Stdio::null()).status().unwrap();
        assert!(status.success(), "process exceeded memory budget or could not enforce it");
    }

    #[test]
    fn process_cpu_budget_stops_busy_loop() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "while :; do :; done"]);
        // SAFETY: this uses the same limit setup, then tightens CPU to one
        // second so the exhaustion fixture completes quickly.
        unsafe { command.pre_exec(|| {
            apply_process_limits()?;
            set_limit(libc::RLIMIT_CPU, 1)
        }); }
        let capture = supervise(&mut command, &AtomicBool::new(false),
            Instant::now() + Duration::from_secs(6)).unwrap();
        assert!(matches!(capture.outcome, Outcome::Crash(libc::SIGKILL | libc::SIGXCPU)),
            "CPU exhaustion was not stopped by the kernel: {:?}", capture.outcome);
    }
}
