//! Dedicated Linux provider owner. Parent control EOF also covers parent SIGKILL.
//! The owner is the only subreaper: the daemon never adopts unrelated children.
use std::{io::{self, Read, Write, Seek, SeekFrom}, os::unix::{io::AsRawFd, net::UnixStream}, time::Duration};

pub const CONTROL_ENV: &str = "DOXA_CODEX_OWNER_FD";
pub const READY: &[u8] = b"DOXA_PROVIDER_OWNER_V1\n";

/// The native launcher must acknowledge ownership before any provider fork.
/// No blocking task owns a clone of this socket, so cancellation closes it.
pub async fn acknowledge(control: &mut UnixStream) -> io::Result<()> {
    control.set_nonblocking(true)?;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
    let mut offset = 0;
    let mut buffer = [0_u8; READY.len()];
    while offset < READY.len() {
        match control.read(&mut buffer[offset..]) {
            Ok(0) => return Err(io::Error::other("protected provider owner closed before readiness")),
            Ok(count) => offset += count,
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {},
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(io::Error::other("protected provider owner readiness timed out"));
        }
        if offset < READY.len() { tokio::time::sleep(Duration::from_millis(10)).await; }
    }
    if buffer != READY { return Err(io::Error::other("invalid protected provider owner handshake")); }
    control.write_all(b"G")
}

#[cfg(target_os = "linux")]
fn census(file: &mut std::fs::File, signal: bool) -> io::Result<bool> {
    // Read the pinned proc file in fixed space, with no lifetime child cap.
    // Every signaled entry is a DIRECT, unreaped kernel child of this owner.
    file.seek(SeekFrom::Start(0))?;
    let mut bytes = [0_u8; 4096];
    let mut token = Vec::with_capacity(12);
    let mut any = false;
    loop {
        let count = file.read(&mut bytes)?;
        for byte in bytes[..count].iter().copied().chain((count == 0).then_some(b' ')) {
            if byte.is_ascii_whitespace() {
                if token.is_empty() { continue; }
                let pid = std::str::from_utf8(&token).map_err(io::Error::other)?
                    .parse::<libc::pid_t>().map_err(io::Error::other)?;
                if pid <= 0 { return Err(io::Error::other("invalid owned child identity")); }
                any = true;
                if signal {
                    // No waitpid between reading this identity and signaling.
                    unsafe { libc::kill(pid, libc::SIGSTOP); libc::kill(pid, libc::SIGKILL); }
                }
                token.clear();
            } else {
                if !byte.is_ascii_digit() || token.len() >= 12 {
                    return Err(io::Error::other("invalid provider child census"));
                }
                token.push(byte);
            }
        }
        if count == 0 { return Ok(any); }
    }
}

#[cfg(target_os = "linux")]
fn clean_children(file: &mut std::fs::File) {
    loop {
        // A transient census error must never make this owner abandon live
        // children. The parent's wait is bounded independently; only this
        // small dedicated process persists if the kernel cannot yet reap.
        let signaled = census(file, true);
        loop {
            let result = unsafe { libc::waitpid(-1, std::ptr::null_mut(), libc::WNOHANG) };
            if result > 0 { continue; }
            if result < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) { continue; }
            if result < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::ECHILD) { return; }
            break;
        }
        // Killing direct parents adopts detached grandchildren. Repeat the
        // census after reaping; never signal old identities after a waitpid.
        std::thread::sleep(if signaled.is_ok() { Duration::from_millis(10) } else { Duration::from_millis(250) });
    }
}

/// Runs only in the single-threaded native launcher. `exec` performs only
/// async-signal-safe fexecve/_exit in the forked child, using prepared pointers.
#[cfg(target_os = "linux")]
pub fn supervise(mut control: UnixStream, exec: impl FnOnce() -> io::Error) -> io::Result<i32> {
    unsafe {
        if libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0 { return Err(io::Error::last_os_error()); }
        libc::signal(libc::SIGCHLD, libc::SIG_DFL);
    }
    // Pin and preflight the ownership census before acknowledging any start.
    let mut census_file = std::fs::File::open(format!("/proc/self/task/{}/children", std::process::id()))?;
    census(&mut census_file, false)?;
    control.set_read_timeout(Some(Duration::from_secs(30)))?;
    control.write_all(READY)?;
    let mut go = [0];
    control.read_exact(&mut go)?;
    if go != *b"G" { return Err(io::Error::other("invalid provider owner start authorization")); }
    control.set_nonblocking(true)?;
    let pid = unsafe { libc::fork() };
    if pid < 0 { return Err(io::Error::last_os_error()); }
    if pid == 0 {
        unsafe {
            libc::close(control.as_raw_fd());
            if libc::setsid() < 0 { libc::_exit(127); }
        }
        let _ = exec();
        unsafe { libc::_exit(127); }
    }
    // Only the provider owns protocol stdIO. EOF must not wait on this owner.
    unsafe { libc::close(0); libc::close(1); libc::close(2); }
    let mut status = 0;
    let result = loop {
        let waited = unsafe { libc::waitpid(pid, &mut status, libc::WNOHANG) };
        if waited == pid {
            break if libc::WIFEXITED(status) { libc::WEXITSTATUS(status) }
                else { 128 + libc::WTERMSIG(status) };
        }
        if waited < 0 && io::Error::last_os_error().raw_os_error() != Some(libc::EINTR) { break 1; }
        match control.read(&mut go) {
            Ok(_) => break 130, // EOF or unexpected traffic means shutdown.
            Err(error) if matches!(error.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted) => {},
            Err(_) => break 1,
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    clean_children(&mut census_file);
    Ok(result)
}

#[cfg(not(target_os = "linux"))]
pub fn supervise(_: UnixStream, _: impl FnOnce() -> io::Error) -> io::Result<i32> {
    Err(io::Error::other("protected provider descendant ownership requires Linux"))
}
