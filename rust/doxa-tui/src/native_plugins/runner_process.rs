//! Bounded child supervision for the explicit grantless plugin CLI launcher.
//! This accepts a caller-built Command and does not certify its isolation.
//! The cgroup/Bubblewrap wrapper calls it; the TUI does not.
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

const MAX_STREAM_BYTES: usize = 64 * 1024;
// Header plus the maximum module size. The caller's frame is copied nowhere:
// the parent writes it into one nonblocking pipe while draining both outputs.
const MAX_INPUT_BYTES: usize = 8 * 1024 * 1024 + 44;
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
    supervise_with_input(command, None, cancel, deadline)
}

/// Supervise a caller-built child and deliver at most one bounded input frame.
/// Output is drained while the input pipe is written, so a child which writes
/// before reading cannot deadlock the parent. An overlong input is refused
/// before spawn. The child sees EOF after the final byte.
pub(crate) fn supervise_with_input(
    command: &mut Command,
    input: Option<&[u8]>,
    cancel: &AtomicBool,
    deadline: Instant,
) -> io::Result<Capture> {
    if input.is_some_and(|bytes| bytes.len() > MAX_INPUT_BYTES) {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "child input exceeds frame limit"));
    }
    if cancel.load(Ordering::Acquire) || Instant::now() >= deadline {
        return Err(io::Error::new(io::ErrorKind::Interrupted, "child cancelled before spawn"));
    }
    command.process_group(0)
        .stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn()?;
    let group_id = child.id();
    let mut stdin = child.stdin.take();
    let Some(mut stdout) = child.stdout.take() else {
        let _ = stop_group(&mut child, group_id);
        return Err(io::Error::other("missing child stdout"));
    };
    let Some(mut stderr) = child.stderr.take() else {
        let _ = stop_group(&mut child, group_id);
        return Err(io::Error::other("missing child stderr"));
    };
    if let Err(error) = nonblocking(stdout.as_raw_fd())
        .and_then(|_| nonblocking(stderr.as_raw_fd()))
        .and_then(|_| stdin.as_ref().map_or(Ok(()), |pipe| nonblocking(pipe.as_raw_fd()))) {
        let _ = stop_group(&mut child, group_id);
        return Err(error);
    }
    let mut out = Vec::new();
    let mut err = Vec::new();
    let mut out_eof = false;
    let mut err_eof = false;
    let mut exited = false;
    let mut input_position = 0;
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
        if let (Some(pipe), Some(bytes)) = (stdin.as_mut(), input) {
            if input_position < bytes.len() {
                let end = (input_position + 64 * 1024).min(bytes.len());
                match pipe.write(&bytes[input_position..end]) {
                    Ok(count) => input_position += count,
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => {},
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => {},
                    Err(error) if error.kind() == io::ErrorKind::BrokenPipe => { stdin = None; },
                    Err(error) => break Err(error),
                }
            }
            if input_position == bytes.len() { stdin = None; }
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

    #[test]
    fn input_pipe_delivers_exact_frame_and_eof() {
        let bytes = vec![b'x'; 256 * 1024];
        let mut command = Command::new("/usr/bin/wc");
        command.arg("-c");
        let capture = supervise_with_input(&mut command, Some(&bytes), &AtomicBool::new(false),
            Instant::now() + Duration::from_secs(3)).unwrap();
        assert_eq!(capture.outcome, Outcome::Exit(0));
        assert_eq!(String::from_utf8(capture.stdout).unwrap().trim(), bytes.len().to_string());
    }

    #[test]
    fn child_that_never_reads_input_is_stopped_at_deadline() {
        let bytes = vec![b'x'; MAX_INPUT_BYTES];
        let mut command = Command::new("/bin/sleep");
        command.arg("30");
        let capture = supervise_with_input(&mut command, Some(&bytes), &AtomicBool::new(false),
            Instant::now() + Duration::from_millis(120)).unwrap();
        assert_eq!(capture.outcome, Outcome::Timeout);
    }

    #[test]
    fn oversized_input_refuses_spawn() {
        let bytes = vec![b'x'; MAX_INPUT_BYTES + 1];
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "exit 0"]);
        let error = supervise_with_input(&mut command, Some(&bytes), &AtomicBool::new(false),
            Instant::now() + Duration::from_secs(1)).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    }
}
