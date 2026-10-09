//! Disabled Linux broker-origin probe. A pidfd pins the connector's process,
//! but a connected Unix descriptor can later be sent to a different writer.
//! Even a matching namespace/cgroup observation is therefore not admission.
#![allow(dead_code)]

use crate::error;
use std::{fs::{self, File}, io::{self, Read}, os::{fd::{AsRawFd, FromRawFd}, unix::fs::MetadataExt},
    os::unix::net::UnixStream, path::PathBuf};

// Linux UAPI include/uapi/asm-generic/socket.h; libc may predate this option.
const SO_PEERPIDFD: libc::c_int = 77;

#[derive(Debug, PartialEq, Eq)]
struct ProcessScope { pid_namespace: PathBuf, cgroup: String }

#[derive(Debug)]
struct ConnectorObservation { pid: libc::pid_t, uid: libc::uid_t, scope: ProcessScope }

fn peer_cred(stream: &UnixStream) -> io::Result<libc::ucred> {
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe { libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED,
        (&mut cred as *mut libc::ucred).cast(), &mut len) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if len as usize != std::mem::size_of::<libc::ucred>() || cred.pid <= 0 {
        return Err(error("broker peer credentials are incomplete"));
    }
    Ok(cred)
}

fn peer_pidfd(stream: &UnixStream) -> io::Result<File> {
    let mut fd: libc::c_int = -1;
    let mut len = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
    if unsafe { libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, SO_PEERPIDFD,
        (&mut fd as *mut libc::c_int).cast(), &mut len) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if len as usize != std::mem::size_of::<libc::c_int>() || fd < 0 {
        if fd >= 0 { unsafe { libc::close(fd); } }
        return Err(error("broker peer pidfd is incomplete"));
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

fn open_pidfd(pid: libc::pid_t) -> io::Result<File> {
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
    if fd < 0 { return Err(io::Error::last_os_error()); }
    Ok(unsafe { File::from_raw_fd(fd as libc::c_int) })
}

fn pidfd_alive(fd: &File) -> io::Result<()> {
    let mut pollfd = libc::pollfd { fd: fd.as_raw_fd(), events: libc::POLLIN, revents: 0 };
    match unsafe { libc::poll(&mut pollfd, 1, 0) } {
        0 => Ok(()),
        -1 => Err(io::Error::last_os_error()),
        _ => Err(error("broker peer pidfd no longer has a live process")),
    }
}

fn pidfd_pid(fd: &File) -> io::Result<libc::pid_t> {
    let path = format!("/proc/self/fdinfo/{}", fd.as_raw_fd());
    let mut info = String::new();
    File::open(path)?.take(4096).read_to_string(&mut info)?;
    let mut pids = info.lines().filter_map(|line| line.strip_prefix("Pid:").map(str::trim));
    let pid = pids.next().ok_or_else(|| error("broker peer pidfd has no PID"))?;
    if pids.next().is_some() { return Err(error("broker peer pidfd has duplicate PID fields")); }
    pid.parse::<libc::pid_t>().ok().filter(|pid| *pid > 0)
        .ok_or_else(|| error("broker peer pidfd PID is unavailable"))
}

fn scope(pid: libc::pid_t) -> io::Result<ProcessScope> {
    if pid <= 0 { return Err(error("broker process PID is invalid")); }
    let proc = PathBuf::from(format!("/proc/{pid}"));
    let pid_namespace = fs::read_link(proc.join("ns/pid"))?;
    let mut raw = String::new();
    File::open(proc.join("cgroup"))?.take(8193).read_to_string(&mut raw)?;
    if raw.len() > 8192 { return Err(error("broker origin cgroup record exceeds bound")); }
    let mut lines = raw.lines();
    let cgroup = lines.next().and_then(|line| line.strip_prefix("0::"))
        .ok_or_else(|| error("broker origin needs a cgroup v2 process"))?;
    if lines.next().is_some() || !cgroup.starts_with('/') || cgroup.contains("..") {
        return Err(error("broker origin cgroup path is ambiguous"));
    }
    Ok(ProcessScope { pid_namespace, cgroup: cgroup.to_owned() })
}

fn same_pidfd(left: &File, right: &File) -> io::Result<bool> {
    let left = left.metadata()?; let right = right.metadata()?;
    Ok((left.dev(), left.ino()) == (right.dev(), right.ino()))
}

/// Read-only observation of the process that originally connected the socket.
/// The second pidfd open guards /proc reads against PID reuse. Process cgroup
/// membership can still change, and neither pidfd identifies a later writer.
fn observe_connector(stream: &UnixStream) -> io::Result<ConnectorObservation> {
    let cred = peer_cred(stream)?;
    let peer = peer_pidfd(stream)?;
    pidfd_alive(&peer)?;
    if pidfd_pid(&peer)? != cred.pid { return Err(error("broker peer PID and pidfd disagree")); }
    let reopened = open_pidfd(cred.pid)?;
    if !same_pidfd(&peer, &reopened)? { return Err(error("broker peer PID was recycled")); }
    let observed = scope(cred.pid)?;
    if !same_pidfd(&peer, &open_pidfd(cred.pid)?)? {
        return Err(error("broker peer process changed during scope read"));
    }
    pidfd_alive(&peer)?;
    Ok(ConnectorObservation { pid: cred.pid, uid: cred.uid, scope: observed })
}

fn candidate_matches(peer: &ProcessScope, init: &ProcessScope, host: &ProcessScope) -> bool {
    let cgroup = init.cgroup.trim_end_matches('/');
    peer.pid_namespace == init.pid_namespace && peer.pid_namespace != host.pid_namespace
        && !cgroup.is_empty() && cgroup != "/"
        && (peer.cgroup == cgroup || peer.cgroup.starts_with(&format!("{cgroup}/")))
}

/// Disabled admission seam. `inspected_init_pid` would need to come from an
/// independently authenticated, exact-container Engine observation. Even a
/// matching connector is rejected until per-message writer provenance and
/// cgroup/namespace transition races have a proven boundary.
fn require_hardened_origin(stream: &UnixStream, inspected_init_pid: libc::pid_t) -> io::Result<()> {
    let peer = observe_connector(stream)?;
    if peer.uid != unsafe { libc::geteuid() } {
        return Err(error("broker connector is not the rootless Engine owner"));
    }
    let init_pin = open_pidfd(inspected_init_pid)?;
    pidfd_alive(&init_pin)?;
    let init = scope(inspected_init_pid)?;
    if !same_pidfd(&init_pin, &open_pidfd(inspected_init_pid)?)? {
        return Err(error("inspected container init PID changed"));
    }
    let host = scope(unsafe { libc::getpid() })?;
    if !candidate_matches(&peer.scope, &init, &host) {
        return Err(error("broker connector is outside the inspected container scope"));
    }
    Err(error("pidfd proves the socket connector, not a later descriptor holder; hardened broker origin remains unavailable"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixListener;

    #[test]
    fn scope_candidate_distinguishes_host_sibling_and_prefix() {
        let host = ProcessScope { pid_namespace: "pid:[1]".into(), cgroup: "/owner".into() };
        let init = ProcessScope { pid_namespace: "pid:[2]".into(), cgroup: "/owner/container-a".into() };
        let worker = ProcessScope { pid_namespace: "pid:[2]".into(), cgroup: "/owner/container-a/worker".into() };
        let sibling = ProcessScope { pid_namespace: "pid:[3]".into(), cgroup: "/owner/container-b".into() };
        assert!(candidate_matches(&worker, &init, &host));
        assert!(!candidate_matches(&host, &init, &host));
        assert!(!candidate_matches(&sibling, &init, &host));
        assert!(!candidate_matches(&ProcessScope { pid_namespace: "pid:[2]".into(), cgroup: "/owner/container-ab".into() }, &init, &host));
    }

    #[test]
    fn same_uid_host_connector_has_stable_pidfd_but_is_refused() {
        let root = tempfile::tempdir().unwrap();
        let listener = UnixListener::bind(root.path().join("hook.sock")).unwrap();
        let _client = UnixStream::connect(root.path().join("hook.sock")).unwrap();
        let (server, _) = listener.accept().unwrap();
        let observed = observe_connector(&server).unwrap();
        assert_eq!(observed.pid, unsafe { libc::getpid() });
        assert_eq!(observed.uid, unsafe { libc::geteuid() });
        let refusal = require_hardened_origin(&server, observed.pid).unwrap_err();
        assert!(refusal.to_string().contains("outside the inspected container scope"));
    }

    #[test]
    fn inherited_socket_writer_is_not_the_pidfd_connector() {
        let root = tempfile::tempdir().unwrap();
        let listener = UnixListener::bind(root.path().join("hook.sock")).unwrap();
        let client = UnixStream::connect(root.path().join("hook.sock")).unwrap();
        let (mut server, _) = listener.accept().unwrap();
        let connector = unsafe { libc::getpid() };
        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            // Only async-signal-safe libc calls are made after fork in the
            // multithreaded Rust test harness.
            let byte = [b'X'];
            let sent = unsafe { libc::write(client.as_raw_fd(), byte.as_ptr().cast(), 1) };
            unsafe { libc::_exit(if sent == 1 { 0 } else { 1 }); }
        }
        drop(client);
        let mut byte = [0];
        server.read_exact(&mut byte).unwrap();
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
        assert_eq!(status, 0);
        assert_eq!(byte, [b'X']);
        let observed = observe_connector(&server).unwrap();
        assert_eq!(observed.pid, connector);
        assert_ne!(observed.pid, child, "SO_PEERPIDFD identifies the connector, not the actual writer");
        assert!(require_hardened_origin(&server, connector).is_err());
    }
}
