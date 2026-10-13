//! One elapsed deadline covers the subprocess and both inherited output pipes.
use std::{
    io::{self, Read},
    os::{fd::AsRawFd, unix::process::CommandExt},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

const OUTPUT_LIMIT: usize = 1024 * 1024;
const ERROR_LIMIT: usize = 8192;
const POLL_INTERVAL: Duration = Duration::from_millis(10);
const CLEANUP_LIMIT: Duration = Duration::from_secs(1);

fn error(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message.into())
}

pub(super) fn run(command: Command) -> io::Result<Vec<u8>> {
    run_with_timeout(command, Duration::from_secs(45))
}

fn nonblocking(pipe: &impl AsRawFd) -> io::Result<()> {
    let fd = pipe.as_raw_fd();
    // These are our own newly created pipe descriptors. Preserve other flags.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags == -1 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

// Read at most one chunk per stream per iteration: a noisy stream must not
// starve the other stream or the elapsed deadline. Keep stderr's original
// diagnostic cap while draining excess bytes until EOF or the same deadline.
enum PipeRead { Eof, Data, Pending }
fn read_chunk(pipe: &mut impl Read, bytes: &mut Vec<u8>, limit: usize,
    reject_overflow: bool) -> io::Result<PipeRead> {
    let mut buffer = [0; 8192];
    match pipe.read(&mut buffer) {
        Ok(0) => Ok(PipeRead::Eof),
        Ok(count) => {
            if reject_overflow && count > limit.saturating_sub(bytes.len()) {
                return Err(error("isolation operation output exceeds limit"));
            }
            let keep = count.min(limit.saturating_sub(bytes.len()));
            bytes.extend_from_slice(&buffer[..keep]);
            Ok(PipeRead::Data)
        },
        Err(err) if matches!(err.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted) => Ok(PipeRead::Pending),
        Err(err) => Err(err),
    }
}

fn sleep_until(deadline: Instant) {
    std::thread::sleep(POLL_INTERVAL.min(deadline.saturating_duration_since(Instant::now())));
}

fn cleanup(child: &mut Child) -> io::Result<()> {
    // The caller has not reaped this child yet, so its PID still anchors the
    // process group even if a descendant retained the pipes after its exit.
    let group = i32::try_from(child.id()).map_err(|_| error("invalid isolation process group"))?;
    let killed = unsafe { libc::kill(-group, libc::SIGKILL) };
    let kill_error = (killed == -1).then(io::Error::last_os_error)
        .filter(|err| err.raw_os_error() != Some(libc::ESRCH));
    if kill_error.is_some() { let _ = child.kill(); }
    let deadline = Instant::now() + CLEANUP_LIMIT;
    loop {
        if child.try_wait()?.is_some() {
            return kill_error.map_or(Ok(()), Err);
        }
        if Instant::now() >= deadline {
            return Err(error("isolation process cleanup unconfirmed; native fallback refused"));
        }
        sleep_until(deadline);
    }
}

pub(super) fn run_with_timeout(command: Command, timeout: Duration) -> io::Result<Vec<u8>> {
    run_with_cleanup(command, timeout, cleanup)
}

fn run_with_cleanup(mut command: Command, timeout: Duration,
    cleanup: impl FnOnce(&mut Child) -> io::Result<()>) -> io::Result<Vec<u8>> {
    let deadline = Instant::now() + timeout;
    command.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .process_group(0);
    let mut child = command.spawn()?;
    let mut stdout = child.stdout.take().expect("piped stdout");
    let mut stderr = child.stderr.take().expect("piped stderr");
    let mut reaped = false;
    let result = (|| {
        nonblocking(&stdout)?; nonblocking(&stderr)?;
        let (mut output, mut diagnostic) = (Vec::new(), Vec::new());
        let (mut out_eof, mut err_eof) = (false, false);
        loop {
            if Instant::now() >= deadline {
                return Err(error("isolation operation timed out; native fallback refused"));
            }
            let mut progress = false;
            if !out_eof {
                match read_chunk(&mut stdout, &mut output, OUTPUT_LIMIT, true)? {
                    PipeRead::Eof => out_eof = true, PipeRead::Data => progress = true, PipeRead::Pending => {},
                }
            }
            if !err_eof {
                match read_chunk(&mut stderr, &mut diagnostic, ERROR_LIMIT, false)? {
                    PipeRead::Eof => err_eof = true, PipeRead::Data => progress = true, PipeRead::Pending => {},
                }
            }
            // Delay reaping until both EOFs. A direct child can exit before its
            // descendants close these descriptors; keeping its PID reserved
            // makes deadline cleanup target our group rather than a reused PID.
            if out_eof && err_eof {
                if let Some(status) = child.try_wait()? {
                    reaped = true;
                    return if status.success() { Ok(output) } else {
                        Err(error(format!("isolation operation failed: {}",
                            String::from_utf8_lossy(&diagnostic).chars().take(1000).collect::<String>())))
                    };
                }
            }
            if !progress { sleep_until(deadline); }
        }
    })();
    // Signal while we still own the read ends: closing first can kill a writer
    // by SIGPIPE before group signaling. A signal refusal remains unconfirmed
    // even when the direct child is subsequently reaped. Never replay it.
    let cleanup_result = if reaped { Ok(()) } else { cleanup(&mut child) };
    drop(stdout); drop(stderr);
    if let Err(err) = cleanup_result {
        let cause = match result {
            Err(cause) => cause,
            Ok(_) => error("isolation operation failed"),
        };
        return Err(error(format!("{cause}; process cleanup unconfirmed: {err}; native fallback refused")));
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::DirBuilderExt, path::{Path, PathBuf}, sync::atomic::{AtomicU64, Ordering}};

    struct FixtureRoot(PathBuf);
    impl FixtureRoot {
        fn new() -> Self {
            static NEXT: AtomicU64 = AtomicU64::new(0);
            let path = std::env::temp_dir().join(format!("doxa-runner-{}-{}",
                std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
            fs::DirBuilder::new().mode(0o700).create(&path).unwrap();
            Self(path)
        }
    }
    impl Drop for FixtureRoot {
        fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); }
    }
    fn shell(script: &str) -> Command {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", script]); command
    }
    fn assert_reaped(pid: u32) {
        assert_eq!(unsafe { libc::kill(pid as i32, 0) }, -1, "direct child not reaped");
        assert_eq!(io::Error::last_os_error().raw_os_error(), Some(libc::ESRCH));
    }
    #[cfg(target_os = "linux")]
    fn assert_descendant_stopped(pid_file: &Path) {
        let pid: u32 = fs::read_to_string(pid_file).unwrap().trim().parse().unwrap();
        let deadline = Instant::now() + Duration::from_secs(1);
        loop {
            match fs::read_to_string(format!("/proc/{pid}/stat")) {
                Err(err) if err.kind() == io::ErrorKind::NotFound => return,
                Ok(stat) if stat.rsplit_once(") ").unwrap().1.starts_with('Z') => return,
                _ if Instant::now() < deadline => std::thread::sleep(POLL_INTERVAL),
                _ => panic!("descendant {pid} still executing after cleanup"),
            }
        }
    }

    #[test]
    fn normal_success_and_failure_collect_both_streams() {
        assert_eq!(run_with_timeout(shell("printf 'normal\\n'; printf 'diagnostic' >&2"),
            Duration::from_secs(2)).unwrap(), b"normal\n");
        let err = run_with_timeout(shell("printf 'failure detail' >&2; exit 7"),
            Duration::from_secs(2)).unwrap_err();
        assert!(err.to_string().contains("failure detail"));
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn exited_child_with_inherited_pipes_obeys_total_deadline() {
        // Cover either retained stream, and both: direct-child success cannot
        // make a pending inherited descriptor cease to count toward the limit.
        for redirect in ["2>/dev/null", ">/dev/null", ""] {
            let root = FixtureRoot::new();
            let mut command = shell(&format!("printf '%s' $$ > parent.pid; sleep 30 {redirect} & printf '%s' $! > descendant.pid; exit 0"));
            command.current_dir(&root.0);
            let started = Instant::now();
            let err = run_with_timeout(command, Duration::from_millis(150)).unwrap_err();
            assert!(err.to_string().contains("timed out"), "{err}");
            assert!(started.elapsed() < Duration::from_secs(2));
            let parent = fs::read_to_string(root.0.join("parent.pid")).unwrap().parse().unwrap();
            assert_reaped(parent);
            assert_descendant_stopped(&root.0.join("descendant.pid"));
        }
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn live_child_deadline_kills_its_group_without_replay() {
        for close_pipes in ["", "exec >/dev/null 2>&1;"] {
        let root = FixtureRoot::new();
        let mut command = shell(&format!("{close_pipes} printf '%s' $$ > parent.pid; printf 'attempt\\n' >> attempts; sleep 30 & printf '%s' $! > descendant.pid; wait"));
        command.current_dir(&root.0);
        let started = Instant::now();
        let err = run_with_timeout(command, Duration::from_millis(150)).unwrap_err();
        assert!(err.to_string().contains("timed out"));
        assert!(started.elapsed() < Duration::from_secs(2));
        assert_eq!(fs::read_to_string(root.0.join("attempts")).unwrap(), "attempt\n");
        assert_reaped(fs::read_to_string(root.0.join("parent.pid")).unwrap().parse().unwrap());
        assert_descendant_stopped(&root.0.join("descendant.pid"));
        }
    }

    #[test]
    fn output_limit_refuses_and_cleans_up_writer() {
        let root = FixtureRoot::new();
        let mut command = shell("printf '%s' $$ > parent.pid; printf 'attempt\\n' >> attempts; while :; do printf '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'; done");
        command.current_dir(&root.0);
        let started = Instant::now();
        let err = run_with_timeout(command, Duration::from_secs(3)).unwrap_err();
        assert!(err.to_string().contains("output exceeds limit"), "{err}");
        assert!(started.elapsed() < Duration::from_secs(3));
        assert_reaped(fs::read_to_string(root.0.join("parent.pid")).unwrap().parse().unwrap());
        assert_eq!(fs::read_to_string(root.0.join("attempts")).unwrap(), "attempt\n");
    }

    #[test]
    fn cleanup_refusal_preserves_operation_cause_and_stays_failed() {
        let root = FixtureRoot::new();
        let mut command = shell("printf '%s' $$ > parent.pid; printf 'attempt\\n' >> attempts; while :; do printf '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'; done");
        command.current_dir(&root.0);
        let mut cleanup_called = false;
        let err = run_with_cleanup(command, Duration::from_secs(3), |child| {
            cleanup_called = true;
            // Clean the actual local process, then inject an unconfirmed group
            // signal result. This must never become a successful operation or
            // hide the original output-cap error on any Unix platform.
            cleanup(child)?;
            Err(io::Error::from_raw_os_error(libc::EPERM))
        }).unwrap_err();
        assert!(cleanup_called);
        assert!(err.to_string().contains("output exceeds limit"), "{err}");
        assert!(err.to_string().contains("process cleanup unconfirmed"), "{err}");
        assert!(err.to_string().contains("native fallback refused"), "{err}");
        assert_reaped(fs::read_to_string(root.0.join("parent.pid")).unwrap().parse().unwrap());
        assert_eq!(fs::read_to_string(root.0.join("attempts")).unwrap(), "attempt\n");
    }
}
