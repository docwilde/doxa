//! Local keyboard-only shell jobs. Never called by a provider or dispatcher.
use std::{io::{self, Read}, os::{fd::OwnedFd, unix::{net::UnixStream, process::CommandExt}},
    path::Path, process::{Command, Stdio}, sync::{Arc, atomic::{AtomicBool, Ordering}, mpsc},
    thread::JoinHandle, time::{Duration, Instant}};

pub const OUTPUT_CAP: usize = 64 * 1024;
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct Result {
    pub id: u64, pub command: String, pub output: String, pub status: String,
    pub running: bool, pub dropped_bytes: u64,
}
pub struct Job {
    pub session: String, pub id: u64,
    receiver: mpsc::Receiver<Result>, cancel: Arc<AtomicBool>, thread: Option<JoinHandle<()>>,
}
impl Job {
    pub fn start(session: String, id: u64, command: String, cwd: &Path) -> Self {
        let (sender, receiver) = mpsc::sync_channel(1);
        let cancel = Arc::new(AtomicBool::new(false)); let cancelled = cancel.clone();
        let cwd = cwd.to_owned();
        let thread = std::thread::spawn(move || { let _ = sender.send(run(id, command, &cwd, &cancelled, Duration::from_secs(120))); });
        Self { session, id, receiver, cancel, thread: Some(thread) }
    }
    pub fn poll(&self) -> Option<Result> { self.receiver.try_recv().ok() }
    pub fn cancel(&self) { self.cancel.store(true, Ordering::Release); }
}
impl Drop for Job {
    fn drop(&mut self) { self.cancel(); if let Some(thread) = self.thread.take() { let _ = thread.join(); } }
}

fn run(id: u64, command: String, cwd: &Path, cancel: &AtomicBool, timeout: Duration) -> Result {
    let started = Instant::now();
    let mut result = Result { id, command, output: String::new(), status: String::new(), running: false, dropped_bytes: 0 };
    let execute = || -> io::Result<(Vec<u8>, u64, String)> {
        let (mut reader, writer) = UnixStream::pair()?;
        reader.set_nonblocking(true)?;
        let mut child = Command::new("/bin/sh").args(["-c", &result.command]).current_dir(cwd)
            .stdin(Stdio::null()).stdout(Stdio::from(OwnedFd::from(writer.try_clone()?)))
            .stderr(Stdio::from(OwnedFd::from(writer))).process_group(0).spawn()?;
        let pid = child.id() as i32;
        let mut raw = Vec::new(); let mut dropped = 0u64; let mut buffer = [0u8; 8192];
        let mut exited = None; let mut eof = false; let mut exit_at = None; let mut killed = None;
        let outcome = loop {
            // Keep draining after the cap. A bounded burst also gives process
            // cancellation a turn when a command emits without stopping.
            for _ in 0..16 {
                match reader.read(&mut buffer) {
                    Ok(0) => { eof = true; break; },
                    Ok(count) => { let kept = count.min(OUTPUT_CAP.saturating_sub(raw.len())); raw.extend_from_slice(&buffer[..kept]); dropped = dropped.saturating_add((count-kept) as u64); },
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                    Err(_) => { eof = true; break; },
                }
            }
            if exited.is_none() {
                match child.try_wait() {
                    Ok(Some(status)) => { exited = Some(status); exit_at = Some(Instant::now()); },
                    Ok(None) => {},
                    Err(_) => { unsafe { libc::kill(-pid, libc::SIGKILL); } let _ = child.wait(); break "process status unavailable".into(); },
                }
            }
            if cancel.load(Ordering::Acquire) || started.elapsed() >= timeout {
                if killed.is_none() {
                    unsafe { libc::kill(-pid, libc::SIGKILL); }
                    killed = Some(if cancel.load(Ordering::Acquire) { "cancelled" } else { "timeout" });
                }
            }
            if exited.is_some() && eof { break killed.map(str::to_owned).unwrap_or_else(|| format!("exit {}", exited.unwrap().code().map_or("signal".into(), |code| code.to_string()))); }
            if exit_at.is_some_and(|at| at.elapsed() >= Duration::from_secs(2)) {
                unsafe { libc::kill(-pid, libc::SIGKILL); }
                break format!("{} · output pipe remained open", killed.unwrap_or("process exited"));
            }
            std::thread::sleep(Duration::from_millis(10));
        };
        let _ = child.wait();
        Ok((raw, dropped, outcome))
    };
    match execute() {
        Ok((raw, dropped, status)) => { result.output = crate::markdown::sanitize(&String::from_utf8_lossy(&raw)); result.dropped_bytes = dropped; result.status = status; },
        Err(_) => { result.status = "could not start shell in session directory".into(); },
    }
    result.status.push_str(&format!(" · {}ms", started.elapsed().as_millis()));
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn uses_session_directory_and_merges_streams_without_a_terminal() {
        let dir = tempfile::tempdir().unwrap();
        let result = run(1, "pwd; printf 'stdout'; printf 'stderr' >&2; exit 7".into(), dir.path(), &AtomicBool::new(false), Duration::from_secs(2));
        assert!(result.output.contains(dir.path().to_str().unwrap())); assert!(result.output.contains("stdoutstderr")); assert!(result.status.starts_with("exit 7"));
    }
    #[test]
    fn drains_past_cap_and_reaps_timeout_group() {
        let dir = tempfile::tempdir().unwrap();
        let result = run(2, "head -c 100000 /dev/zero | tr '\\000' x".into(), dir.path(), &AtomicBool::new(false), Duration::from_secs(2));
        assert_eq!(result.output.len(), OUTPUT_CAP); assert_eq!(result.dropped_bytes, 100000-OUTPUT_CAP as u64); assert!(result.status.starts_with("exit 0"));
        let pidfile = dir.path().join("pid");
        let result = run(3, format!("echo $$ > '{}'; sleep 20", pidfile.display()), dir.path(), &AtomicBool::new(false), Duration::from_millis(80));
        assert!(result.status.starts_with("timeout")); let pid: i32 = std::fs::read_to_string(pidfile).unwrap().trim().parse().unwrap(); assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
    }
}
