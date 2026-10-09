//! Bounded child supervision for a future plugin sandbox launcher.
//! This accepts a caller-built Command and does not certify its isolation.
//! It is unwired from native-plugin commands and the TUI.
use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

const MAX_STREAM_BYTES: usize = 64 * 1024;
const POLL: Duration = Duration::from_millis(5);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Outcome {
    Exit(i32),
    Crash(i32),
    Timeout,
    Cancelled,
    OutputLimit,
}

#[derive(Debug)]
pub(crate) struct Capture {
    pub outcome: Outcome,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub group_id: u32,
}

#[derive(Clone, Copy)]
enum StopReason { Completed, Timeout, Cancelled, OutputLimit }

fn nonblocking(fd: i32) -> io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn drain(reader: &mut impl Read, bytes: &mut Vec<u8>, eof: &mut bool) -> io::Result<bool> {
    if *eof { return Ok(false); }
    let mut buffer = [0u8; 4096];
    loop {
        match reader.read(&mut buffer) {
            Ok(0) => { *eof = true; return Ok(false); }
            Ok(count) => {
                let room = MAX_STREAM_BYTES.saturating_sub(bytes.len());
                bytes.extend_from_slice(&buffer[..count.min(room)]);
                if count > room { return Ok(true); }
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => return Ok(false),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
}

fn stop_group(child: &mut Child, group_id: u32) -> io::Result<ExitStatus> {
    // process_group(0) creates a group whose ID is the initial child PID.
    // The group kill is needed even when the leader has exited but a
    // descendant still holds an inherited output descriptor.
    let killed = unsafe { libc::kill(-(group_id as i32), libc::SIGKILL) };
    let kill_error = if killed < 0 {
        let error = io::Error::last_os_error();
        (error.raw_os_error() != Some(libc::ESRCH)).then_some(error)
    } else { None };
    if kill_error.is_some() { let _ = child.kill(); }
    let status = child.wait()?;
    if let Some(error) = kill_error { return Err(error); }
    Ok(status)
}

fn peek_exited(pid: u32) -> io::Result<bool> {
    // WNOWAIT keeps the leader's PID reserved until the group is killed.
    // try_wait would reap it early and allow a dangerous PGID reuse race.
    let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
    let rc = unsafe {
        libc::waitid(libc::P_PID, pid as libc::id_t, &mut info,
            libc::WEXITED | libc::WNOHANG | libc::WNOWAIT)
    };
    if rc < 0 { return Err(io::Error::last_os_error()); }
    Ok(unsafe { info.si_pid() } != 0)
}

fn status_outcome(status: ExitStatus) -> Outcome {
    if let Some(signal) = status.signal() { Outcome::Crash(signal) }
    else { Outcome::Exit(status.code().unwrap_or(-1)) }
}

/// Runs one preconfigured child with finite output and a wall deadline.
/// On every outcome the owned process group receives SIGKILL and its leader
/// is reaped. The group leader stays unreaped until then, reserving its PGID.
///
/// This helper does not contain the child within a cgroup. A compromised
/// descendant can escape the process group, so this alone is not a sandbox.
pub(crate) fn supervise(
    command: &mut Command,
    cancel: &AtomicBool,
    deadline: Instant,
) -> io::Result<Capture> {
    if cancel.load(Ordering::Acquire) || Instant::now() >= deadline {
        return Err(io::Error::new(io::ErrorKind::Interrupted, "child cancelled before spawn"));
    }
    command.process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn()?;
    let group_id = child.id();
    let Some(mut stdout) = child.stdout.take() else {
        let _ = stop_group(&mut child, group_id);
        return Err(io::Error::other("missing child stdout"));
    };
    let Some(mut stderr) = child.stderr.take() else {
        let _ = stop_group(&mut child, group_id);
        return Err(io::Error::other("missing child stderr"));
    };
    if let Err(error) = nonblocking(stdout.as_raw_fd()).and_then(|_| nonblocking(stderr.as_raw_fd())) {
        let _ = stop_group(&mut child, group_id);
        return Err(error);
    }
    let mut out = Vec::new();
    let mut err = Vec::new();
    let mut out_eof = false;
    let mut err_eof = false;
    let mut exited = false;
    let result = loop {
        if cancel.load(Ordering::Acquire) { break Ok(StopReason::Cancelled); }
        if Instant::now() >= deadline { break Ok(StopReason::Timeout); }
        match drain(&mut stdout, &mut out, &mut out_eof) {
            Ok(true) => break Ok(StopReason::OutputLimit),
            Err(error) => break Err(error),
            Ok(false) => {}
        }
        match drain(&mut stderr, &mut err, &mut err_eof) {
            Ok(true) => break Ok(StopReason::OutputLimit),
            Err(error) => break Err(error),
            Ok(false) => {}
        }
        if !exited {
            match peek_exited(group_id) {
                Ok(value) => exited = value,
                Err(error) => break Err(error),
            }
        }
        if exited && out_eof && err_eof { break Ok(StopReason::Completed); }
        thread::sleep(POLL);
    };
    // A child may exit and close its pipes while descendants remain. Stop the
    // group before reaping its leader even on a successful return.
    let status = stop_group(&mut child, group_id)?;
    let outcome = match result {
        Ok(StopReason::Completed) => status_outcome(status),
        Ok(StopReason::Timeout) => Outcome::Timeout,
        Ok(StopReason::Cancelled) => Outcome::Cancelled,
        Ok(StopReason::OutputLimit) => Outcome::OutputLimit,
        Err(error) => return Err(error),
    };
    Ok(Capture { outcome, stdout: out, stderr: err, group_id })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::sync::atomic::AtomicBool;
    use std::sync::Arc;

    fn sandbox_available() -> bool {
        let status = Command::new("/usr/bin/bwrap")
            .args(["--unshare-all", "--die-with-parent", "--clearenv",
                "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
                "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--", "/bin/true"])
            .stdout(Stdio::null()).stderr(Stdio::null()).status();
        status.is_ok_and(|status| status.success())
    }

    fn sandbox(script: &str) -> Command {
        let mut command = Command::new("/usr/bin/bwrap");
        command.args(["--unshare-all", "--die-with-parent", "--clearenv",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--", "/bin/sh", "-c", script]);
        command
    }

    fn marker_running(marker: &str) -> bool {
        let Ok(entries) = fs::read_dir("/proc") else { return false; };
        entries.flatten().filter(|entry| entry.file_name().to_string_lossy().bytes().all(|b| b.is_ascii_digit()))
            .any(|entry| fs::read(entry.path().join("cmdline"))
                .is_ok_and(|args| args.windows(marker.len()).any(|window| window == marker.as_bytes())))
    }

    fn assert_no_marker(marker: &str) {
        let deadline = Instant::now() + Duration::from_secs(2);
        while marker_running(marker) && Instant::now() < deadline { thread::sleep(POLL); }
        assert!(!marker_running(marker), "sandbox child survived group shutdown");
    }

    fn marker() -> String {
        format!("doxa_b19_{}_{}", std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos())
    }

    #[test]
    fn sandbox_fixture_returns_bounded_stdout_and_stderr() {
        if !sandbox_available() { return; }
        let capture = supervise(&mut sandbox("printf hello; printf warning >&2"),
            &AtomicBool::new(false), Instant::now() + Duration::from_secs(2)).unwrap();
        assert_eq!(capture.outcome, Outcome::Exit(0));
        assert_eq!(capture.stdout, b"hello");
        assert_eq!(capture.stderr, b"warning");
    }

    #[test]
    fn sandbox_timeout_kills_descendant_and_reaps_leader() {
        if !sandbox_available() { return; }
        let marker = marker();
        let script = format!("/bin/bash -c 'exec -a {marker} /bin/sleep 30' & printf ready; wait");
        let capture = supervise(&mut sandbox(&script), &AtomicBool::new(false),
            Instant::now() + Duration::from_millis(120)).unwrap();
        assert_eq!(capture.outcome, Outcome::Timeout);
        assert!(capture.stdout.starts_with(b"ready"));
        assert_no_marker(&marker);
        assert_eq!(unsafe { libc::kill(capture.group_id as i32, 0) }, -1);
    }

    #[test]
    fn sandbox_cancellation_kills_descendant_and_reaps_leader() {
        if !sandbox_available() { return; }
        let marker = marker();
        let script = format!("/bin/bash -c 'exec -a {marker} /bin/sleep 30' & printf ready; wait");
        let cancel = Arc::new(AtomicBool::new(false));
        let trigger = Arc::clone(&cancel);
        let thread = thread::spawn(move || {
            thread::sleep(Duration::from_millis(80));
            trigger.store(true, Ordering::Release);
        });
        let capture = supervise(&mut sandbox(&script), &cancel,
            Instant::now() + Duration::from_secs(2)).unwrap();
        thread.join().unwrap();
        assert_eq!(capture.outcome, Outcome::Cancelled);
        assert!(capture.stdout.starts_with(b"ready"));
        assert_no_marker(&marker);
    }

    #[test]
    fn sandbox_output_flood_is_bounded_and_stopped() {
        if !sandbox_available() { return; }
        let marker = marker();
        let script = format!("/bin/bash -c 'exec -a {marker} /bin/yes x'");
        let capture = supervise(&mut sandbox(&script),
            &AtomicBool::new(false), Instant::now() + Duration::from_secs(2)).unwrap();
        assert_eq!(capture.outcome, Outcome::OutputLimit);
        assert!(capture.stdout.len() <= MAX_STREAM_BYTES);
        assert!(capture.stderr.len() <= MAX_STREAM_BYTES);
        assert_no_marker(&marker);
        let capture = supervise(&mut sandbox("yes x >&2"),
            &AtomicBool::new(false), Instant::now() + Duration::from_secs(2)).unwrap();
        assert_eq!(capture.outcome, Outcome::OutputLimit);
        assert!(capture.stdout.len() <= MAX_STREAM_BYTES);
        assert!(capture.stderr.len() <= MAX_STREAM_BYTES);
    }

    #[test]
    fn signalled_child_is_reported_as_crash() {
        let mut child = Command::new("/bin/sh");
        child.args(["-c", "kill -KILL $$"]);
        let capture = supervise(&mut child, &AtomicBool::new(false),
            Instant::now() + Duration::from_secs(2)).unwrap();
        assert_eq!(capture.outcome, Outcome::Crash(libc::SIGKILL));
    }

    #[test]
    fn sandbox_wrapper_propagates_child_crash_as_abnormal_exit() {
        if !sandbox_available() { return; }
        let capture = supervise(&mut sandbox("kill -KILL $$"),
            &AtomicBool::new(false), Instant::now() + Duration::from_secs(2)).unwrap();
        // Bubblewrap maps its child's signal to exit code 128 + signal.
        // This cannot be distinguished from a child that exits 137 itself.
        assert_eq!(capture.outcome, Outcome::Exit(128 + libc::SIGKILL));
    }
}
