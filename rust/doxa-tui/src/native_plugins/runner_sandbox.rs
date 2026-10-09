//! Unwired Linux sandbox admission for the future grantless plugin child.
//! A command is constructed only after a private cgroup v2 budget is installed.
//! No TUI or CLI path calls this module yet: the dedicated child protocol and
//! containment acceptance suite are still required before activation.
#![cfg(target_os = "linux")]
#![allow(dead_code)] // staged admission API has no production caller

use std::ffi::CString;
use std::fs;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use std::sync::atomic::AtomicBool;
use std::thread;
use std::time::{Duration, Instant};

const BWRAP: &str = "/usr/bin/bwrap";
const CGROUP_ROOT: &str = "/sys/fs/cgroup";
const MEMORY_MAX: &str = "268435456"; // 256 MiB, including Wasmi compilation
const PIDS_MAX: &str = "16";
const CPU_MAX: &str = "100000 100000"; // at most one CPU of aggregate bandwidth
const PROCESS_AS_BYTES: libc::rlim_t = 256 * 1024 * 1024;
const PROCESS_CPU_SECONDS: libc::rlim_t = 4;
const PROCESS_FDS: libc::rlim_t = 64;

fn unavailable(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message.into())
}

fn current_cgroup() -> io::Result<PathBuf> {
    let text = fs::read_to_string("/proc/self/cgroup")?;
    let relative = text.lines().find_map(|line| line.strip_prefix("0::"))
        .ok_or_else(|| unavailable("unified cgroup v2 membership unavailable"))?;
    if relative.len() > 1024 || !relative.starts_with('/')
        || Path::new(relative).components().any(|component| !matches!(component, Component::RootDir | Component::Normal(_))) {
        return Err(unavailable("invalid cgroup v2 membership"));
    }
    let root = Path::new(CGROUP_ROOT);
    let group = root.join(relative.trim_start_matches('/'));
    let mut stat: libc::statfs = unsafe { std::mem::zeroed() };
    let root_c = CString::new(CGROUP_ROOT).unwrap();
    if unsafe { libc::statfs(root_c.as_ptr(), &mut stat) } < 0 {
        return Err(io::Error::last_os_error());
    }
    if stat.f_type != libc::CGROUP2_SUPER_MAGIC as libc::c_long {
        return Err(unavailable("cgroup hierarchy is not cgroup v2"));
    }
    let metadata = fs::symlink_metadata(&group)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o200 == 0 {
        return Err(unavailable("current cgroup is not delegated to this user"));
    }
    let enabled = fs::read_to_string(group.join("cgroup.subtree_control"))?;
    if !["memory", "pids", "cpu"].iter().all(|controller| enabled.split_whitespace().any(|value| value == *controller)) {
        return Err(unavailable("memory, pids and cpu cgroup controllers are not delegated"));
    }
    Ok(group)
}

fn write_and_check(path: &Path, value: &str) -> io::Result<()> {
    fs::write(path, value)?;
    let observed = fs::read_to_string(path)?;
    if observed.trim() != value {
        return Err(unavailable(format!("cgroup limit was not installed: {}", path.display())));
    }
    Ok(())
}

/// The cgroup owns all descendants, including a compromised child that calls
/// setsid() to escape the supervisor's process group. Drop kills the entire
/// cgroup before attempting to remove it.
pub(crate) struct CgroupBudget { path: PathBuf, stopped: bool }

impl CgroupBudget {
    pub(crate) fn create() -> io::Result<Self> {
        let parent = current_cgroup()?;
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
            let events = fs::read_to_string(self.path.join("cgroup.events"))?;
            if events.lines().any(|line| line == "populated 0") { break; }
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

fn require_trusted_executable(path: &Path) -> io::Result<PathBuf> {
    let path = fs::canonicalize(path)?;
    let metadata = fs::metadata(&path)?;
    if !path.is_absolute() || !metadata.is_file()
        || ![0, unsafe { libc::geteuid() }].contains(&metadata.uid())
        || metadata.permissions().mode() & 0o022 != 0
        || metadata.permissions().mode() & 0o111 == 0 {
        return Err(unavailable("plugin worker executable is not private or root-owned"));
    }
    Ok(path)
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

/// Build a command with a zero-grant filesystem and network view. This is not
/// an activation switch: no production caller or worker executable exists.
/// The exact module bytes must later arrive through the bounded stdin pipe.
fn command(worker: &Path, cgroup: &CgroupBudget) -> io::Result<Command> {
    let bwrap = require_trusted_executable(Path::new(BWRAP))?;
    let worker = require_trusted_executable(worker)?;
    let procs = cgroup.procs_path()?;
    let mut command = Command::new(bwrap);
    command.env_clear().current_dir("/");
    command.args(["--unshare-all", "--unshare-user", "--die-with-parent", "--disable-userns",
        "--cap-drop", "ALL", "--clearenv", "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--ro-bind", worker.to_str()
            .ok_or_else(|| unavailable("plugin worker path is not UTF-8"))?, "/worker",
        "--proc", "/proc", "--dev", "/dev", "--size", "16777216",
        "--tmpfs", "/tmp", "--chdir", "/", "--", "/worker"]);
    // SAFETY: only async-signal-safe libc calls run between fork and exec.
    // The cgroup write moves the untrusted child before bwrap can execute.
    unsafe { command.pre_exec(move || {
        let fd = libc::open(procs.as_ptr(), libc::O_WRONLY | libc::O_CLOEXEC);
        if fd < 0 { return Err(io::Error::last_os_error()); }
        let wrote = libc::write(fd, b"0".as_ptr().cast(), 1);
        let saved_error = io::Error::last_os_error();
        libc::close(fd);
        if wrote != 1 { return Err(saved_error); }
        apply_process_limits()
    }); }
    Ok(command)
}

/// Unwired containment seam. It rechecks exact owner approval before spawn,
/// sends the verified bytes through one bounded pipe, and kills the entire
/// cgroup after every process outcome. A dedicated worker executable and
/// response classification are still required before any app caller exists.
pub(crate) fn supervise_reviewed(
    home: &Path,
    review: &super::packages::Review,
    worker: &Path,
    cancel: &AtomicBool,
    deadline: Instant,
) -> io::Result<super::runner_process::Capture> {
    let package = super::packages::recheck_approved(home, review)?;
    let frame = super::runner::encode_request(&package)?;
    let mut budget = CgroupBudget::create()?;
    let mut command = command(worker, &budget)?;
    let result = super::runner_process::supervise_with_input(
        &mut command, Some(&frame), cancel, deadline);
    budget.stop()?;
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::runner_process::{supervise, Outcome};
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

    #[test]
    fn admission_fails_closed_without_delegated_cgroup() {
        if current_cgroup().is_err() {
            assert!(CgroupBudget::create().is_err());
        } else {
            let mut budget = CgroupBudget::create().expect("delegated cgroup must accept hard budgets");
            assert_eq!(fs::read_to_string(budget.path.join("memory.max")).unwrap().trim(), MEMORY_MAX);
            assert_eq!(fs::read_to_string(budget.path.join("pids.max")).unwrap().trim(), PIDS_MAX);
            assert_eq!(fs::read_to_string(budget.path.join("cpu.max")).unwrap().trim(), CPU_MAX);
            budget.stop().expect("empty budget must be removable");
        }
    }

    #[test]
    fn trusted_executable_rejects_world_writable_file() {
        let dir = tempfile::tempdir().unwrap();
        let worker = dir.path().join("worker");
        fs::write(&worker, b"worker").unwrap();
        fs::set_permissions(&worker, fs::Permissions::from_mode(0o777)).unwrap();
        assert!(require_trusted_executable(&worker).is_err());
    }

    #[test]
    fn namespace_fixture_hides_host_file_environment_and_network() {
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
