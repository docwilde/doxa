//! Linux per-send HookBroker frame reader and disabled container-origin probe.
//! The live Docker broker uses sender pidfds and credentials for every frame
//! segment. Exact container admission still needs an authenticated Engine.
#![allow(dead_code)]

use crate::error;
use std::{fs::{self, File}, io::{self, Read}, mem, os::{fd::{AsRawFd, FromRawFd},
    unix::{fs::MetadataExt, net::{UnixListener, UnixStream}}}, path::PathBuf,
    time::{Duration, Instant}};

const MAX_FRAME: usize = 65_536;
const MAX_CONTROL: usize = 256;
const MAX_FRAME_TIME: Duration = Duration::from_secs(5);
const RECV_TIMEOUT: Duration = Duration::from_millis(200);
// Linux include/linux/socket.h. SCM_PIDFD is a kernel-generated, read-only cmsg.
const SCM_PIDFD: libc::c_int = 4;
const SO_PEERPIDFD: libc::c_int = 77;

#[derive(Debug, PartialEq, Eq)]
struct Scope { pid_namespace: PathBuf, cgroup: String }
#[derive(Debug)]
struct Sender { pid: libc::pid_t, uid: libc::uid_t, scope: Scope }

/// The connector identity for a guarded stream. A Unix descriptor may be
/// inherited or passed after connect, so each read must also name this writer.
pub(crate) struct ConnectorSenderPin { pidfd: File, cred: libc::ucred }

fn enable_sender_pidfds(fd: libc::c_int) -> io::Result<()> {
    let on: libc::c_int = 1;
    for option in [libc::SO_PASSPIDFD, libc::SO_PASSCRED] {
        if unsafe { libc::setsockopt(fd, libc::SOL_SOCKET, option,
            (&on as *const libc::c_int).cast(), mem::size_of_val(&on) as libc::socklen_t) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let mut observed: libc::c_int = 0;
        let mut len = mem::size_of_val(&observed) as libc::socklen_t;
        if unsafe { libc::getsockopt(fd, libc::SOL_SOCKET, option,
            (&mut observed as *mut libc::c_int).cast(), &mut len) } != 0 {
            return Err(io::Error::last_os_error());
        }
        if observed != 1 || len as usize != mem::size_of_val(&observed) {
            return Err(error("broker sender-pidfd feature is unavailable"));
        }
    }
    Ok(())
}

/// Enable before the listener accepts or receives worker bytes. The kernel
/// propagates both options to accepted sockets; a late enable fails closed if
/// queued bytes have no sender cmsg. Unsupported kernels return an error.
pub(crate) fn prepare_listener(listener: &UnixListener) -> io::Result<()> {
    enable_sender_pidfds(listener.as_raw_fd())
}

fn pidfd_pid(fd: &File) -> io::Result<libc::pid_t> {
    let mut info = String::new();
    File::open(format!("/proc/self/fdinfo/{}", fd.as_raw_fd()))?
        .take(4097).read_to_string(&mut info)?;
    if info.len() > 4096 { return Err(error("sender pidfd record exceeds bound")); }
    let mut values = info.lines().filter_map(|line| line.strip_prefix("Pid:").map(str::trim));
    let pid = values.next().and_then(|value| value.parse::<libc::pid_t>().ok())
        .filter(|pid| *pid > 0).ok_or_else(|| error("sender pidfd PID unavailable"))?;
    if values.next().is_some() { return Err(error("sender pidfd PID is ambiguous")); }
    Ok(pid)
}

fn open_pidfd(pid: libc::pid_t) -> io::Result<File> {
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) };
    if fd < 0 { return Err(io::Error::last_os_error()); }
    Ok(unsafe { File::from_raw_fd(fd as libc::c_int) })
}

fn same_pidfd(a: &File, b: &File) -> io::Result<bool> {
    let a = a.metadata()?; let b = b.metadata()?;
    Ok((a.dev(), a.ino()) == (b.dev(), b.ino()))
}

fn live_pidfd(fd: &File) -> io::Result<()> {
    let mut pollfd = libc::pollfd { fd: fd.as_raw_fd(), events: libc::POLLIN, revents: 0 };
    match unsafe { libc::poll(&mut pollfd, 1, 0) } {
        0 => Ok(()),
        -1 => Err(io::Error::last_os_error()),
        _ => Err(error("sender exited before origin inspection")),
    }
}

pub(crate) fn pin_connector_sender(stream: &UnixStream) -> io::Result<ConnectorSenderPin> {
    let mut cred: libc::ucred = unsafe { mem::zeroed() };
    let mut len = mem::size_of_val(&cred) as libc::socklen_t;
    if unsafe { libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED,
        (&mut cred as *mut libc::ucred).cast(), &mut len) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if len as usize != mem::size_of_val(&cred) || cred.pid <= 0
        || cred.uid != unsafe { libc::geteuid() } {
        return Err(error("guarded connector credentials are unavailable"));
    }
    let mut descriptor: libc::c_int = -1;
    len = mem::size_of_val(&descriptor) as libc::socklen_t;
    if unsafe { libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, SO_PEERPIDFD,
        (&mut descriptor as *mut libc::c_int).cast(), &mut len) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if len as usize != mem::size_of_val(&descriptor) || descriptor < 0 {
        if descriptor >= 0 { unsafe { libc::close(descriptor); } }
        return Err(error("guarded connector pidfd is unavailable"));
    }
    let pidfd = unsafe { File::from_raw_fd(descriptor) };
    live_pidfd(&pidfd)?;
    if pidfd_pid(&pidfd)? != cred.pid
        || !same_pidfd(&pidfd, &open_pidfd(cred.pid)?)? {
        return Err(error("guarded connector PID and pidfd disagree"));
    }
    Ok(ConnectorSenderPin { pidfd, cred })
}

/// Read one bounded stream chunk with kernel-generated pidfd and credentials.
/// No bytes from a transferred descriptor are returned to the caller.
pub(crate) fn read_pinned_sender(stream: &UnixStream, bytes: &mut [u8],
    connector: &ConnectorSenderPin) -> io::Result<usize> {
    let (count, sender, cred) = recv_sender_chunk(stream, bytes)?;
    if !same_pidfd(&connector.pidfd, &sender)?
        || (connector.cred.pid, connector.cred.uid, connector.cred.gid)
            != (cred.pid, cred.uid, cred.gid) {
        return Err(error("guarded stream writer differs from connector"));
    }
    live_pidfd(&connector.pidfd)?;
    Ok(count)
}

fn scope(pid: libc::pid_t) -> io::Result<Scope> {
    if pid <= 0 { return Err(error("invalid sender PID")); }
    let proc = PathBuf::from(format!("/proc/{pid}"));
    let pid_namespace = fs::read_link(proc.join("ns/pid"))?;
    let mut raw = String::new();
    File::open(proc.join("cgroup"))?.take(8193).read_to_string(&mut raw)?;
    if raw.len() > 8192 { return Err(error("sender cgroup record exceeds bound")); }
    let mut lines = raw.lines();
    let cgroup = lines.next().and_then(|row| row.strip_prefix("0::"))
        .ok_or_else(|| error("sender requires unambiguous cgroup v2"))?;
    if lines.next().is_some() || !cgroup.starts_with('/') || cgroup.contains("..") {
        return Err(error("sender cgroup record is ambiguous"));
    }
    Ok(Scope { pid_namespace, cgroup: cgroup.to_owned() })
}

fn sender_from_pidfd(fd: &File, cred: libc::ucred) -> io::Result<Sender> {
    live_pidfd(fd)?;
    let pid = pidfd_pid(fd)?;
    if cred.pid != pid || cred.uid != unsafe { libc::geteuid() } {
        return Err(error("sender pidfd and kernel credentials disagree"));
    }
    let pin = open_pidfd(pid)?;
    if !same_pidfd(fd, &pin)? { return Err(error("sender PID was recycled")); }
    let observed = scope(pid)?;
    if !same_pidfd(fd, &open_pidfd(pid)?)? { return Err(error("sender scope changed process")); }
    live_pidfd(fd)?;
    Ok(Sender { pid, uid: cred.uid, scope: observed })
}

fn align(value: usize) -> usize {
    let word = mem::size_of::<usize>();
    (value + word - 1) & !(word - 1)
}

/// One bounded recvmsg segment and its kernel-supplied sender pidfd. Unexpected
/// SCM_RIGHTS are closed even when the control message is truncated/refused.
fn recv_sender_chunk(stream: &UnixStream, bytes: &mut [u8]) -> io::Result<(usize, File, libc::ucred)> {
    if bytes.is_empty() { return Err(error("empty broker sender read")); }
    let mut control = [0u64; MAX_CONTROL / mem::size_of::<u64>()];
    let mut iov = libc::iovec { iov_base: bytes.as_mut_ptr().cast(), iov_len: bytes.len() };
    let mut msg: libc::msghdr = unsafe { mem::zeroed() };
    msg.msg_iov = &mut iov;
    msg.msg_iovlen = 1;
    msg.msg_control = control.as_mut_ptr().cast();
    msg.msg_controllen = MAX_CONTROL;
    let count = unsafe { libc::recvmsg(stream.as_raw_fd(), &mut msg, libc::MSG_CMSG_CLOEXEC) };
    if count < 0 { return Err(io::Error::last_os_error()); }
    if count == 0 { return Err(error("incomplete broker sender frame")); }
    let mut pidfds = Vec::new();
    let mut rights = Vec::new();
    let mut credentials = Vec::new();
    let mut unexpected = false;
    let total = msg.msg_controllen as usize;
    let header = unsafe { libc::CMSG_LEN(0) as usize };
    let mut offset = 0;
    while total <= MAX_CONTROL && offset + header <= total {
        let cmsg = unsafe { &*(control.as_ptr().cast::<u8>().add(offset).cast::<libc::cmsghdr>()) };
        let size = cmsg.cmsg_len as usize;
        if size < header || size > total - offset { unexpected = true; break; }
        let payload = unsafe { std::slice::from_raw_parts(
            control.as_ptr().cast::<u8>().add(offset + header), size - header) };
        if cmsg.cmsg_level == libc::SOL_SOCKET && cmsg.cmsg_type == SCM_PIDFD {
            if payload.len() == mem::size_of::<libc::c_int>() {
                let fd = libc::c_int::from_ne_bytes(payload.try_into().unwrap());
                if fd >= 0 { pidfds.push(unsafe { File::from_raw_fd(fd) }); } else { unexpected = true; }
            } else { unexpected = true; }
        } else if cmsg.cmsg_level == libc::SOL_SOCKET && cmsg.cmsg_type == libc::SCM_RIGHTS {
            for chunk in payload.chunks_exact(mem::size_of::<libc::c_int>()) {
                let fd = libc::c_int::from_ne_bytes(chunk.try_into().unwrap());
                if fd >= 0 { rights.push(unsafe { File::from_raw_fd(fd) }); }
            }
            unexpected = true;
        } else if cmsg.cmsg_level == libc::SOL_SOCKET && cmsg.cmsg_type == libc::SCM_CREDENTIALS {
            if payload.len() == mem::size_of::<libc::ucred>() {
                let cred = unsafe { std::ptr::read_unaligned(payload.as_ptr().cast::<libc::ucred>()) };
                credentials.push(cred);
            } else { unexpected = true; }
        } else { unexpected = true; }
        offset = offset.saturating_add(align(size));
    }
    if total > MAX_CONTROL || msg.msg_flags & (libc::MSG_CTRUNC | libc::MSG_TRUNC) != 0
        || unexpected || !rights.is_empty() || pidfds.len() != 1 || credentials.len() != 1 {
        return Err(error("broker sender cmsg missing, truncated or unexpected"));
    }
    Ok((count as usize, pidfds.pop().unwrap(), credentials.pop().unwrap()))
}

fn read_exact_from_one_sender(stream: &UnixStream, bytes: &mut [u8], first: &mut Option<File>,
    first_cred: &mut Option<libc::ucred>, deadline: Instant) -> io::Result<()> {
    let mut position = 0;
    while position < bytes.len() {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() { return Err(error("broker sender frame deadline exceeded")); }
        stream.set_read_timeout(Some(remaining.min(RECV_TIMEOUT)))?;
        let (read, pidfd, cred) = match recv_sender_chunk(stream, &mut bytes[position..]) {
            Ok(chunk) => chunk,
            Err(err) if matches!(err.kind(), io::ErrorKind::WouldBlock
                | io::ErrorKind::TimedOut | io::ErrorKind::Interrupted) => continue,
            Err(err) => return Err(err),
        };
        if Instant::now() >= deadline { return Err(error("broker sender frame deadline exceeded")); }
        if let Some(original) = first {
            if !same_pidfd(original, &pidfd)? || first_cred.as_ref().is_none_or(|old|
                (old.pid, old.uid, old.gid) != (cred.pid, cred.uid, cred.gid)) {
                return Err(error("broker frame bytes came from different senders"));
            }
        } else {
            *first = Some(pidfd);
            *first_cred = Some(cred);
        }
        position += read;
    }
    Ok(())
}

/// Experiment only: every recvmsg segment of a length-prefixed broker frame
/// must carry the same kernel sender pidfd and credentials. No fallback to
/// SO_PEERCRED is allowed. The caller must enable options before any send.
fn read_frame_sender(stream: &UnixStream) -> io::Result<(Vec<u8>, Sender)> {
    read_frame_sender_until(stream, Instant::now() + MAX_FRAME_TIME)
}

/// Read the live Docker hook frame with kernel provenance for every segment.
/// This proves the writer's identity at send time, but does not establish
/// that the writer belongs to an authenticated Docker container.
pub(crate) fn read_owner_frame(stream: &UnixStream) -> io::Result<Vec<u8>> {
    let (bytes, sender) = read_frame_sender(stream)?;
    if sender.uid != unsafe { libc::geteuid() } {
        return Err(error("broker message sender is not the rootless Engine owner"));
    }
    Ok(bytes)
}

fn read_frame_sender_until(stream: &UnixStream, deadline: Instant) -> io::Result<(Vec<u8>, Sender)> {
    let mut pidfd = None; let mut cred = None;
    let mut length = [0; 4];
    read_exact_from_one_sender(stream, &mut length, &mut pidfd, &mut cred, deadline)?;
    let size = u32::from_be_bytes(length) as usize;
    if size == 0 { return Err(error("empty broker sender frame")); }
    if size > MAX_FRAME { return Err(error("broker sender frame exceeds bound")); }
    let mut bytes = vec![0; size];
    read_exact_from_one_sender(stream, &mut bytes, &mut pidfd, &mut cred, deadline)?;
    let sender = sender_from_pidfd(&pidfd.unwrap(), cred.unwrap())?;
    Ok((bytes, sender))
}

fn candidate_matches(sender: &Scope, init: &Scope, host: &Scope) -> bool {
    let cgroup = init.cgroup.trim_end_matches('/');
    sender.pid_namespace == init.pid_namespace && sender.pid_namespace != host.pid_namespace
        && !cgroup.is_empty() && (sender.cgroup == cgroup
            || sender.cgroup.starts_with(&format!("{cgroup}/")))
}

/// Deliberately unavailable. The init PID would need a trusted exact-Docker
/// observation, plus protection against post-send cgroup movement and replay.
fn require_hardened_sender_origin(stream: &UnixStream, inspected_init_pid: libc::pid_t) -> io::Result<()> {
    let (_, sender) = read_frame_sender(stream)?;
    let init_pin = open_pidfd(inspected_init_pid)?;
    live_pidfd(&init_pin)?;
    let init = scope(inspected_init_pid)?;
    if !same_pidfd(&init_pin, &open_pidfd(inspected_init_pid)?)? {
        return Err(error("inspected container init PID changed"));
    }
    let host = scope(unsafe { libc::getpid() })?;
    if !candidate_matches(&sender.scope, &init, &host) {
        return Err(error("broker message sender is outside inspected container scope"));
    }
    Err(error("sender pidfd observation is not bound to an authenticated Engine/container lifecycle; hardened broker remains unavailable"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn send_frame(stream: &mut UnixStream, payload: &[u8]) {
        stream.write_all(&(payload.len() as u32).to_be_bytes()).unwrap();
        stream.write_all(payload).unwrap();
    }

    #[test]
    fn feature_probe_and_missing_cmsg_fail_closed() {
        let (server, mut client) = UnixStream::pair().unwrap();
        send_frame(&mut client, b"hello");
        assert!(read_frame_sender(&server).unwrap_err().to_string().contains("cmsg missing"));
        let (server, mut client) = UnixStream::pair().unwrap();
        enable_sender_pidfds(server.as_raw_fd()).unwrap();
        send_frame(&mut client, b"hello");
        let (body, sender) = read_frame_sender(&server).unwrap();
        assert_eq!(body, b"hello");
        assert_eq!(sender.pid, unsafe { libc::getpid() });
        assert_eq!(sender.uid, unsafe { libc::geteuid() });
        // Even a same-UID sender in this host's own namespace is not a
        // container-origin proof.
        assert!(!candidate_matches(&sender.scope, &sender.scope, &sender.scope));
    }

    #[test]
    fn stalled_sender_cannot_complete_a_partial_frame_after_deadline() {
        let (server, mut client) = UnixStream::pair().unwrap();
        enable_sender_pidfds(server.as_raw_fd()).unwrap();
        client.write_all(&[0, 0]).unwrap();
        let err = read_frame_sender_until(&server, Instant::now() + Duration::from_millis(30))
            .unwrap_err();
        assert!(err.to_string().contains("deadline exceeded"), "{err}");
    }

    #[test]
    fn zero_length_sender_frame_is_rejected() {
        let (server, mut client) = UnixStream::pair().unwrap();
        enable_sender_pidfds(server.as_raw_fd()).unwrap();
        client.write_all(&0u32.to_be_bytes()).unwrap();
        let err = read_frame_sender(&server).unwrap_err();
        assert!(err.to_string().contains("empty broker sender frame"), "{err}");
    }

    #[test]
    fn listener_feature_probe_covers_bytes_queued_before_accept() {
        let root = tempfile::tempdir().unwrap();
        let listener = UnixListener::bind(root.path().join("sender.sock")).unwrap();
        prepare_listener(&listener).unwrap();
        let mut client = UnixStream::connect(root.path().join("sender.sock")).unwrap();
        send_frame(&mut client, b"queued");
        let (server, _) = listener.accept().unwrap();
        let (body, sender) = read_frame_sender(&server).unwrap();
        assert_eq!(body, b"queued");
        assert_eq!(sender.pid, unsafe { libc::getpid() });
    }

    #[test]
    fn inherited_fd_reports_actual_child_sender_not_connector() {
        let (server, client) = UnixStream::pair().unwrap();
        enable_sender_pidfds(server.as_raw_fd()).unwrap();
        let connector = unsafe { libc::getpid() };
        let mut old_cred: libc::ucred = unsafe { mem::zeroed() };
        let mut len = mem::size_of_val(&old_cred) as libc::socklen_t;
        assert_eq!(unsafe { libc::getsockopt(server.as_raw_fd(), libc::SOL_SOCKET,
            libc::SO_PEERCRED, (&mut old_cred as *mut libc::ucred).cast(), &mut len) }, 0);
        assert_eq!(old_cred.pid, connector);
        let mut connector_pidfd: libc::c_int = -1;
        len = mem::size_of_val(&connector_pidfd) as libc::socklen_t;
        assert_eq!(unsafe { libc::getsockopt(server.as_raw_fd(), libc::SOL_SOCKET,
            libc::SO_PEERPIDFD, (&mut connector_pidfd as *mut libc::c_int).cast(), &mut len) }, 0);
        assert!(connector_pidfd >= 0);
        let connector_pidfd = unsafe { File::from_raw_fd(connector_pidfd) };
        assert_eq!(pidfd_pid(&connector_pidfd).unwrap(), connector);
        let mut release = [0; 2];
        assert_eq!(unsafe { libc::pipe2(release.as_mut_ptr(), libc::O_CLOEXEC) }, 0);
        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            unsafe { libc::close(release[1]); }
            let frame = [0, 0, 0, 1, b'X'];
            let sent = unsafe { libc::write(client.as_raw_fd(), frame.as_ptr().cast(), frame.len()) };
            let mut signal = [0];
            let ack = unsafe { libc::read(release[0], signal.as_mut_ptr().cast(), 1) };
            unsafe { libc::_exit(if sent == frame.len() as isize && ack == 1 { 0 } else { 1 }); }
        }
        unsafe { libc::close(release[0]); }
        drop(client);
        let (body, sender) = read_frame_sender(&server).unwrap();
        assert_eq!(body, b"X");
        assert_eq!(sender.pid, child);
        assert_ne!(sender.pid, old_cred.pid);
        assert_eq!(pidfd_pid(&connector_pidfd).unwrap(), connector);
        assert!(!candidate_matches(&sender.scope, &sender.scope, &sender.scope));
        assert_eq!(unsafe { libc::write(release[1], b"x".as_ptr().cast(), 1) }, 1);
        unsafe { libc::close(release[1]); }
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
        assert_eq!(status, 0);
    }

    #[test]
    fn mixed_senders_in_one_stream_frame_are_refused() {
        let (server, mut client) = UnixStream::pair().unwrap();
        enable_sender_pidfds(server.as_raw_fd()).unwrap();
        client.write_all(&1u32.to_be_bytes()).unwrap();
        let mut release = [0; 2];
        assert_eq!(unsafe { libc::pipe2(release.as_mut_ptr(), libc::O_CLOEXEC) }, 0);
        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            unsafe { libc::close(release[1]); }
            let sent = unsafe { libc::write(client.as_raw_fd(), b"X".as_ptr().cast(), 1) };
            let mut signal = [0];
            let ack = unsafe { libc::read(release[0], signal.as_mut_ptr().cast(), 1) };
            unsafe { libc::_exit(if sent == 1 && ack == 1 { 0 } else { 1 }); }
        }
        unsafe { libc::close(release[0]); }
        drop(client);
        let err = read_frame_sender(&server).unwrap_err();
        assert!(err.to_string().contains("different senders"), "{err}");
        assert_eq!(unsafe { libc::write(release[1], b"x".as_ptr().cast(), 1) }, 1);
        unsafe { libc::close(release[1]); }
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
        assert_eq!(status, 0);
    }

    #[test]
    fn host_and_sibling_scope_candidates_are_rejected() {
        let host = Scope { pid_namespace: "pid:[1]".into(), cgroup: "/owner".into() };
        let init = Scope { pid_namespace: "pid:[2]".into(), cgroup: "/owner/container-a".into() };
        let worker = Scope { pid_namespace: "pid:[2]".into(), cgroup: "/owner/container-a/worker".into() };
        let sibling = Scope { pid_namespace: "pid:[3]".into(), cgroup: "/owner/container-b".into() };
        assert!(candidate_matches(&worker, &init, &host));
        assert!(!candidate_matches(&host, &init, &host));
        assert!(!candidate_matches(&sibling, &init, &host));
        assert!(!candidate_matches(&Scope { pid_namespace: "pid:[2]".into(), cgroup: "/owner/container-ab".into() }, &init, &host));
    }
}
