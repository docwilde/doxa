//! Bounded, cancellable maintenance outside the terminal event loop.
use std::{io::{self, Read}, os::unix::process::CommandExt, path::Path,
    process::{Command, Stdio}, sync::{atomic::{AtomicBool, Ordering}, mpsc},
    time::{Duration, Instant}};

pub fn run(kind: &str, engine: Option<&str>, cancel: &AtomicBool) -> io::Result<String> {
    run_at(&std::env::current_exe()?, kind, engine, cancel, Duration::from_secs(900))
}

fn run_at(exe: &Path, kind: &str, engine: Option<&str>, cancel: &AtomicBool, timeout: Duration) -> io::Result<String> {
    if !matches!(kind, "doctor" | "update") { return Err(io::Error::other("Unsupported maintenance operation")); }
    if engine.is_some_and(|engine| !matches!(engine, "claude" | "codex" | "deepseek" | "glm")) {
        return Err(io::Error::other("Unknown doctor engine"));
    }
    let mut command = Command::new(exe);
    command.arg(kind).stdin(Stdio::null()).stderr(Stdio::null()).process_group(0);
    if kind == "doctor" {
        command.stdout(Stdio::piped());
        if let Some(engine) = engine { command.args(["--engine", engine]); }
    } else {
        // Installer output can contain private repository credentials. Report
        // only completion, while retaining the installer’s atomic rollback.
        command.stdout(Stdio::null());
        if std::env::var_os("TMPDIR").is_none() {
            let scratch = crate::operations::doxa_home()?.join("build-tmp");
            std::fs::create_dir_all(&scratch)?;
            command.env("TMPDIR", scratch);
        }
    }
    let mut child = command.spawn()?;
    let (sender, receiver) = mpsc::channel();
    if let Some(output) = child.stdout.take() {
        std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let result = output.take(65537).read_to_end(&mut bytes).and_then(|_| {
                if bytes.len() > 65536 { Err(io::Error::other("Doctor report exceeded its limit")) }
                else { String::from_utf8(bytes).map_err(|_| io::Error::other("Doctor report is not UTF-8")) }
            });
            let _ = sender.send(result);
        });
    }
    let deadline = Instant::now() + timeout;
    let status = loop {
        if let Some(status) = child.try_wait()? { break status; }
        if cancel.load(Ordering::Acquire) || Instant::now() >= deadline {
            unsafe { libc::kill(-(child.id() as i32), libc::SIGTERM); }
            let grace = Instant::now() + Duration::from_secs(3);
            while child.try_wait()?.is_none() && Instant::now() < grace { std::thread::sleep(Duration::from_millis(20)); }
            unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL); }
            let _ = child.wait();
            return Err(io::Error::other("Maintenance cancelled or timed out"));
        }
        std::thread::sleep(Duration::from_millis(25));
    };
    if kind == "doctor" {
        let output = receiver.recv_timeout(Duration::from_secs(1)).map_err(|_| io::Error::other("Doctor report unavailable"))??;
        let output = crate::markdown::sanitize(&output);
        return Ok(format!("{}\n{}", if status.success() { "Health checks passed" } else { "Health checks found missing dependencies" }, output.trim()));
    }
    if !status.success() { return Err(io::Error::other("Update failed; previous installed launcher is retained when rollback succeeds")); }
    Ok("Update installed. Existing sessions retain their daemon version.".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn doctor_reports_failure_without_forwarding_arbitrary_arguments() {
        let dir = tempfile::tempdir().unwrap(); let exe = dir.path().join("fixture");
        std::fs::write(&exe, "#!/bin/sh\nprintf 'missing daemon: fixture\\n'\nexit 1\n").unwrap();
        std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o700)).unwrap();
        let cancel = AtomicBool::new(false);
        let report = run_at(&exe, "doctor", Some("codex"), &cancel, Duration::from_secs(1)).unwrap();
        assert!(report.contains("missing daemon: fixture"));
        assert!(run_at(&exe, "doctor", Some("codex --token secret"), &cancel, Duration::from_secs(1)).is_err());
        assert!(run_at(&exe, "arbitrary", None, &cancel, Duration::from_secs(1)).is_err());
    }
    #[test]
    fn cancellation_reaps_the_owned_process_group() {
        let dir = tempfile::tempdir().unwrap(); let exe = dir.path().join("fixture"); let pid = dir.path().join("pid");
        std::fs::write(&exe, format!("#!/bin/sh\nprintf '%s' $$ > '{}'\nsleep 10\n", pid.display())).unwrap();
        std::fs::set_permissions(&exe, std::fs::Permissions::from_mode(0o700)).unwrap();
        assert!(run_at(&exe, "doctor", None, &AtomicBool::new(false), Duration::from_millis(100)).is_err());
        let pid: i32 = std::fs::read_to_string(pid).unwrap().parse().unwrap();
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
    }
}
