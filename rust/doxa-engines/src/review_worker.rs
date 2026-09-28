//! Native owner for the canonical LORE reviewer. Metadata is never authority:
//! lore-rs verifies its frozen source proof before provider work and effects.
use serde_json::Value;
use std::{io::{self, Read, Write}, os::{fd::AsRawFd, unix::process::CommandExt}, path::{Path, PathBuf}, process::{Command, Stdio}, time::{Duration, Instant}};

pub const MAX_METADATA_BYTES: usize = 16 * 1024;
pub const REVIEW_TIMEOUT: Duration = Duration::from_secs(180);

/// A successful supervisor must prove which bounded job it approved. Merely
/// exiting zero (including an executable that ignores helper arguments) cannot
/// authorize destruction of context.
pub fn approval_receipt(metadata: &Value, engine: &str, timeout: Duration) -> io::Result<Value> {
    use sha2::{Digest, Sha256};
    metadata_bytes(metadata, engine, timeout)?;
    Ok(serde_json::json!({"approved":true,"job_sha256":format!("{:x}",Sha256::digest(serde_json::to_vec(&serde_json::json!({"metadata":metadata,"engine":engine,"timeout_ms":timeout.as_millis()}))?))}))
}

pub fn review_disabled() -> bool {
    std::env::var("LORE_DISABLE_REVIEW").is_ok_and(|value| !matches!(value.as_str(), "" | "0"))
        || std::env::var("LORE_SKIP").is_ok_and(|value| !value.is_empty())
}

fn metadata_bytes(metadata: &Value, engine: &str, timeout: Duration) -> io::Result<Vec<u8>> {
    if !matches!(engine, "claude" | "codex" | "deepseek" | "glm") || !metadata.is_object()
        || timeout.is_zero() || timeout > REVIEW_TIMEOUT { return Err(io::Error::other("invalid review job")); }
    let mut raw = serde_json::to_vec(metadata)?;
    raw.push(b'\n');
    if raw.len() > MAX_METADATA_BYTES { return Err(io::Error::other("review metadata too large")); }
    Ok(raw)
}

/// Spawn an independent supervisor whose stdin remains a parent-liveness pipe.
/// EOF (including parent SIGKILL) makes it stop and reap its review worker group.
/// `executable` must dispatch `__review-supervisor` to [`supervise`].
pub fn review(executable: &Path, metadata: &Value, engine: &str, timeout: Duration, cancelled: impl FnMut() -> bool) -> io::Result<bool> {
    review_at(executable, metadata, engine, timeout, cancelled)
}

/// Execute the already verified native inode, avoiding a path-replacement race.
pub(crate) fn review_pinned(executable: &std::fs::File, metadata: &Value, engine: &str, timeout: Duration, cancelled: impl FnMut() -> bool) -> io::Result<bool> {
    review_at(Path::new(&format!("/proc/self/fd/{}",executable.as_raw_fd())), metadata, engine, timeout, cancelled)
}

fn child_status(pid: libc::pid_t) -> io::Result<Option<bool>> {
    let mut status: libc::siginfo_t = unsafe { std::mem::zeroed() };
    if unsafe { libc::waitid(libc::P_PID,pid as libc::id_t,&mut status,libc::WEXITED|libc::WNOHANG|libc::WNOWAIT) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok((unsafe {status.si_pid()} != 0).then(|| status.si_code==libc::CLD_EXITED && unsafe {status.si_status()}==0))
}

fn review_at(executable: &Path, metadata: &Value, engine: &str, timeout: Duration, mut cancelled: impl FnMut() -> bool) -> io::Result<bool> {
    let expected_receipt = approval_receipt(metadata, engine, timeout)?;
    if cancelled() || review_disabled() { return Ok(false); }
    let mut command = Command::new(executable);
    command.args(["__review-supervisor", engine, &metadata.to_string(), &timeout.as_millis().to_string()])
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
    unsafe { command.pre_exec(|| { if libc::setsid() < 0 { return Err(io::Error::last_os_error()); } Ok(()) }); }
    let mut supervisor = command.spawn()?;
    let pid = supervisor.id() as libc::pid_t;
    let control = supervisor.stdin.take();
    let result = (|| {
        let mut output = supervisor.stdout.take().ok_or_else(|| io::Error::other("review receipt unavailable"))?;
        let fd = output.as_raw_fd();
        if unsafe { libc::fcntl(fd,libc::F_SETFL,libc::O_NONBLOCK) } < 0 { return Err(io::Error::last_os_error()); }
        let deadline = Instant::now() + timeout + Duration::from_secs(3);
        let mut raw = Vec::new();
        let mut eof = false;
        loop {
            if cancelled() || Instant::now() >= deadline { return Ok(false); }
            let mut chunk = [0u8;2049];
            match output.read(&mut chunk) {
                Ok(0) => eof=true,
                Ok(count) => { raw.extend_from_slice(&chunk[..count]); if raw.len()>2048 { return Ok(false); } }
                Err(error) if error.kind()==io::ErrorKind::WouldBlock => {},
                Err(error) => return Err(error),
            }
            if let Some(success) = child_status(pid)? {
                if !success { return Ok(false); }
                if eof { return Ok(serde_json::from_slice::<Value>(&raw).is_ok_and(|receipt|receipt==expected_receipt)); }
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    })();
    drop(control);
    // EOF lets the native owner stop its separate worker group. Retain the
    // leader PID with WNOWAIT until this original supervisor group is killed.
    let shutdown = Instant::now()+Duration::from_secs(3);
    while Instant::now()<shutdown && child_status(pid).ok().flatten().is_none() { std::thread::sleep(Duration::from_millis(10)); }
    unsafe { libc::kill(-pid,libc::SIGKILL); }
    let _ = supervisor.wait();
    result
}

fn binary() -> io::Result<PathBuf> {
    if let Some(path) = std::env::var_os("DOXA_LORE_RS").filter(|s| !s.is_empty()) { return Ok(path.into()); }
    std::env::var_os("PATH").into_iter().flat_map(|path| std::env::split_paths(&path).collect::<Vec<_>>())
        .map(|dir| dir.join("lore-rs")).find(|path| path.is_file())
        .ok_or_else(|| io::Error::other("native LORE reviewer unavailable"))
}

fn ready(fd: libc::c_int, events: libc::c_short) -> io::Result<bool> {
    let mut descriptor = libc::pollfd { fd, events, revents: 0 };
    let count = unsafe { libc::poll(&mut descriptor, 1, 0) };
    if count < 0 { return Err(io::Error::last_os_error()); }
    Ok(count > 0 && descriptor.revents != 0)
}

/// Supervisor process entrypoint. Stdin is ONLY the parent's control pipe;
/// worker metadata is a bounded argument and goes over a different pipe.
pub fn supervise(metadata: &Value, engine: &str, timeout: Duration) -> io::Result<bool> {
    supervise_at(&binary()?, metadata, engine, timeout, 0)
}

/// Host-resolved store paths apply only to this native worker. Secrets remain
/// in the inherited environment or the canonical private settings file.
pub fn supervise_with_env(metadata: &Value, engine: &str, timeout: Duration, environment: &[(std::ffi::OsString, std::ffi::OsString)]) -> io::Result<bool> {
    supervise_at_with_env(&binary()?, metadata, engine, timeout, 0, environment)
}

fn supervise_at(binary: &Path, metadata: &Value, engine: &str, timeout: Duration, control: libc::c_int) -> io::Result<bool> {
    supervise_at_with_env(binary, metadata, engine, timeout, control, &[])
}

fn supervise_at_with_env(binary: &Path, metadata: &Value, engine: &str, timeout: Duration, control: libc::c_int, environment: &[(std::ffi::OsString, std::ffi::OsString)]) -> io::Result<bool> {
    let raw = metadata_bytes(metadata, engine, timeout)?;
    if review_disabled() || ready(control, libc::POLLIN)? { return Ok(false); }
    let mut command = Command::new(binary);
    command.envs(environment.iter().cloned());
    command.args(["review-worker", "--engine", engine]).stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::null());
    unsafe { command.pre_exec(|| { if libc::setsid() < 0 { return Err(io::Error::last_os_error()); } Ok(()) }); }
    let mut child = command.spawn()?;
    let pid = child.id() as libc::pid_t;
    // Keep this leader unreaped until its original process group is killed.
    let result = (|| {
        let deadline = Instant::now() + timeout;
        let mut input = child.stdin.take().ok_or_else(|| io::Error::other("review stdin unavailable"))?;
        let fd = input.as_raw_fd();
        if unsafe { libc::fcntl(fd, libc::F_SETFL, libc::O_NONBLOCK) } < 0 { return Err(io::Error::last_os_error()); }
        let mut offset = 0;
        while offset < raw.len() {
            if ready(control, libc::POLLIN)? || Instant::now() >= deadline { return Ok(false); }
            match input.write(&raw[offset..]) {
                Ok(0) => return Ok(false), Ok(count) => offset += count,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => std::thread::sleep(Duration::from_millis(10)),
                Err(error) => return Err(error),
            }
        }
        drop(input);
        loop {
            if ready(control, libc::POLLIN)? || Instant::now() >= deadline { return Ok(false); }
            let mut status: libc::siginfo_t = unsafe { std::mem::zeroed() };
            if unsafe { libc::waitid(libc::P_PID, pid as libc::id_t, &mut status, libc::WEXITED | libc::WNOHANG | libc::WNOWAIT) } < 0 { return Err(io::Error::last_os_error()); }
            if unsafe { status.si_pid() } != 0 { return Ok(status.si_code == libc::CLD_EXITED && unsafe { status.si_status() } == 0); }
            std::thread::sleep(Duration::from_millis(20));
        }
    })();
    unsafe { libc::kill(-pid, libc::SIGKILL); }
    let waited = child.wait();
    if waited.is_err() { return Ok(false); }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn supervisor_deadline_and_eof_stop_worker_descendants() {
        let dir = tempfile::tempdir().unwrap();
        let worker = dir.path().join("worker"); let pidfile = dir.path().join("pid");
        std::fs::write(&worker, format!("#!/bin/sh\ncat >/dev/null\nsleep 60 &\necho $! > '{}'\nwait\n", pidfile.display())).unwrap();
        std::fs::set_permissions(&worker, std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut pipe = [0;2]; assert_eq!(unsafe { libc::pipe(pipe.as_mut_ptr()) }, 0);
        assert!(!supervise_at(&worker, &serde_json::json!({}), "codex", Duration::from_millis(150), pipe[0]).unwrap());
        let pid: i32 = std::fs::read_to_string(pidfile).unwrap().trim().parse().unwrap();
        // A killed descendant may briefly remain a zombie, but cannot execute.
        let deadline = Instant::now() + Duration::from_secs(1);
        let state = loop {
            let state = std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap_or_default();
            if state.is_empty() || state.split_whitespace().nth(2) == Some("Z") || Instant::now() >= deadline { break state; }
            std::thread::sleep(Duration::from_millis(5));
        };
        assert!(state.is_empty() || state.split_whitespace().nth(2) == Some("Z"));
        unsafe { libc::close(pipe[1]); }
        assert!(!supervise_at(&worker, &serde_json::json!({}), "codex", Duration::from_secs(1), pipe[0]).unwrap());
        unsafe { libc::close(pipe[0]); }
    }
    #[test]
    fn native_review_requires_exact_receipt_and_rejects_spawn_or_output_failures() {
        let dir = tempfile::tempdir().unwrap();
        let executable = dir.path().join("supervisor-fixture");
        let metadata = serde_json::json!({"provider_thread":"bound-thread","expected_source":{"sha256":"owned-source"}});
        let timeout = Duration::from_secs(1);
        assert!(review(&executable, &metadata, "codex", timeout, || false).is_err());
        let receipt = approval_receipt(&metadata, "codex", timeout).unwrap();
        let wrong = approval_receipt(&serde_json::json!({"provider_thread":"foreign"}), "codex", timeout).unwrap();
        for (output, approved) in [("".to_owned(), false), ("invalid output".to_owned(), false), (wrong.to_string(), false), (receipt.to_string(), true)] {
            std::fs::write(&executable, format!("#!/bin/sh\nprintf '%s' '{}'\n", output)).unwrap();
            std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o700)).unwrap();
            assert_eq!(review(&executable, &metadata, "codex", timeout, || false).unwrap(), approved);
        }
        assert!(!review(&executable, &metadata, "codex", timeout, || true).unwrap());
    }

    #[test]
    fn held_receipt_stdout_and_oversize_output_are_bounded_and_reaped() {
        let dir=tempfile::tempdir().unwrap(); let executable=dir.path().join("supervisor-fixture");
        let metadata=serde_json::json!({}); let timeout=Duration::from_millis(20);
        let receipt=approval_receipt(&metadata,"codex",timeout).unwrap();
        let pidfile=dir.path().join("child-pid");
        std::fs::write(&executable,format!("#!/bin/sh\nsleep 60 &\necho $! > '{}'\nprintf '%s' '{}'\n",pidfile.display(),receipt)).unwrap();
        std::fs::set_permissions(&executable,std::fs::Permissions::from_mode(0o700)).unwrap();
        let started=Instant::now();
        assert!(!review(&executable,&metadata,"codex",timeout,||false).unwrap());
        assert!(started.elapsed()<Duration::from_secs(5));
        let pid:i32=std::fs::read_to_string(pidfile).unwrap().trim().parse().unwrap();
        let deadline=Instant::now()+Duration::from_secs(1);
        let state=loop {
            let state=std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap_or_default();
            if state.is_empty() || state.split_whitespace().nth(2)==Some("Z") || Instant::now()>=deadline {break state;}
            std::thread::sleep(Duration::from_millis(5));
        };
        assert!(state.is_empty() || state.split_whitespace().nth(2)==Some("Z"));
        std::fs::write(&executable,"#!/bin/sh\nhead -c 2049 /dev/zero\n").unwrap();
        assert!(!review(&executable,&metadata,"codex",timeout,||false).unwrap());
        assert_ne!(receipt,approval_receipt(&metadata,"claude",timeout).unwrap());
        assert_ne!(receipt,approval_receipt(&metadata,"codex",Duration::from_secs(1)).unwrap());
    }

}
