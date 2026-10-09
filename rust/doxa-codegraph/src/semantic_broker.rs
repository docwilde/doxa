//! Disabled socket-observation handshake for a future semantic broker.
//!
//! Path ownership and SO_PEERCRED values are namespace-relative. Credentials
//! may describe the original listener even after it hands the FD to an
//! unprivileged worker. These observations do not authenticate a live
//! producer or return a semantic binding. Nothing in the CLI calls this.

use super::semantic_producer::ProducerPlan;
use super::semantic_runtime::bounded_unix_connect;
use super::{CallCandidate, CallEdge};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::os::unix::net::UnixStream;
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, Instant};

const PROTOCOL: &str = "doxa-semantic-socket-observation-v1";
const MAX_WIRE_BYTES: usize = 4096;
const DEADLINE: Duration = Duration::from_secs(2);

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn challenge_nonce() -> Result<String, String> {
    let mut nonce = [0u8; 32];
    fs::File::open("/dev/urandom").map_err(|_| "cannot open broker nonce source")?
        .read_exact(&mut nonce).map_err(|_| "cannot read broker nonce source")?;
    Ok(nonce.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn query_digest(plan: &ProducerPlan, edge: &CallEdge, candidate: &CallCandidate) -> Result<String, String> {
    let args = plan.args.iter().map(|value| value.to_str()
        .ok_or("non-UTF-8 producer plan argument")).collect::<Result<Vec<_>, _>>()?;
    let query = json!({"root":plan.root,"image":plan.image,"docker_host":plan.docker_host,
        "args":args,"initialize":plan.initialize,"attestation":plan.attestation,
        "edge":edge,"candidate":candidate});
    let bytes = serde_json::to_vec(&query).map_err(|_| "cannot encode broker query")?;
    Ok(sha256_hex(&bytes))
}

/// Require UID-zero ownership as seen in the caller's namespace. This is a
/// consistency check, not proof of host ownership or a live producer.
fn root_owned_socket(path: &Path) -> Result<(u64, u64), String> {
    if !path.is_absolute() || path.components().any(|part| matches!(part, Component::CurDir | Component::ParentDir)) {
        return Err("broker socket path must be absolute without traversal".into());
    }
    if path.canonicalize().ok().as_deref() != Some(path) {
        return Err("broker socket path must be canonical".into());
    }
    let mut current = PathBuf::from("/");
    let components = path.components().filter(|part| matches!(part, Component::Normal(_))).collect::<Vec<_>>();
    if components.is_empty() { return Err("broker socket path is empty".into()); }
    for (index, component) in components.iter().enumerate() {
        current.push(component.as_os_str());
        let metadata = fs::symlink_metadata(&current).map_err(|_| "broker socket path is missing")?;
        if metadata.uid() != 0 || metadata.file_type().is_symlink() {
            return Err("broker socket path is not UID-zero-owned and unsymlinked".into());
        }
        if index + 1 == components.len() {
            if !metadata.file_type().is_socket() {
                return Err("broker endpoint is not a Unix socket".into());
            }
            return Ok((metadata.dev(), metadata.ino()));
        }
        if !metadata.file_type().is_dir() || metadata.mode() & 0o022 != 0 {
            return Err("broker socket parent is writable or not a directory".into());
        }
    }
    Err("broker socket path is empty".into())
}

fn uid_zero_peer(stream: &UnixStream) -> Result<(), String> {
    uid_zero_peer_fd(stream.as_raw_fd())
}

fn uid_zero_peer_fd(fd: libc::c_int) -> Result<(), String> {
    let mut peer: libc::ucred = unsafe { std::mem::zeroed() };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe { libc::getsockopt(fd, libc::SOL_SOCKET, libc::SO_PEERCRED,
        &mut peer as *mut _ as *mut _, &mut length) } < 0
        || length as usize != std::mem::size_of::<libc::ucred>()
        || peer.pid <= 0 || peer.uid != 0 {
        return Err("broker peer UID is not zero in the caller namespace".into());
    }
    Ok(())
}

fn io_until(stream: &mut UnixStream, bytes: &mut [u8], write: bool, deadline: Instant) -> Result<(), String> {
    let mut position = 0;
    while position < bytes.len() {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() { return Err("broker exchange deadline exceeded".into()); }
        let mut pollfd = libc::pollfd { fd: stream.as_raw_fd(),
            events: if write { libc::POLLOUT } else { libc::POLLIN }, revents: 0 };
        let ready = unsafe { libc::poll(&mut pollfd, 1,
            remaining.as_millis().max(1).min(i32::MAX as u128) as i32) };
        if ready < 0 && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted { continue; }
        if ready <= 0 { return Err("broker exchange deadline exceeded".into()); }
        let result = if write { stream.write(&bytes[position..]) } else { stream.read(&mut bytes[position..]) };
        match result {
            Ok(0) => return Err("broker closed exchange".into()),
            Ok(count) => position += count,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {},
            Err(_) => return Err("broker exchange failed".into()),
        }
    }
    Ok(())
}

fn read_reply(stream: &mut UnixStream, deadline: Instant) -> Result<Value, String> {
    let mut size = [0u8; 4];
    io_until(stream, &mut size, false, deadline)?;
    let length = u32::from_be_bytes(size) as usize;
    if length == 0 || length > MAX_WIRE_BYTES { return Err("broker reply exceeds bound".into()); }
    let mut body = vec![0u8; length];
    io_until(stream, &mut body, false, deadline)?;
    serde_json::from_slice(&body).map_err(|_| "invalid broker reply JSON".into())
}

fn match_reply(reply: &Value, nonce: &str, digest: &str) -> Result<(), String> {
    let object = reply.as_object().ok_or("broker reply is not an object")?;
    if object.len() != 4 || reply.get("protocol").and_then(Value::as_str) != Some(PROTOCOL)
        || reply.get("nonce").and_then(Value::as_str) != Some(nonce)
        || reply.get("query_sha256").and_then(Value::as_str) != Some(digest)
        || reply.get("status").and_then(Value::as_str) != Some("observation_only") {
        return Err("broker reply does not match observation challenge".into());
    }
    Ok(())
}

fn untrusted_status() -> Value {
    json!({"status":"unknown","binding":"unknown",
        "socket_observation":"uid_zero_echo_untrusted",
        "reason":"namespace_and_fd_handoff_unproven","producer":"not_started"})
}

/// A second, disabled observation seam. Unlike SO_PEERCRED, SCM_CREDENTIALS
/// belongs to the process that sent this particular packet. The socket is
/// SOCK_SEQPACKET so the bounded challenge/reply and its credentials cannot
/// be split between a privileged header sender and an unprivileged body
/// sender. UID values are still namespace-relative; this is not a semantic
/// producer attestation or a CLI path.
#[allow(dead_code)]
fn bounded_packet_connect(path: &Path, deadline: Instant) -> Result<OwnedFd, String> {
    use std::os::unix::ffi::OsStrExt;
    let bytes = path.as_os_str().as_bytes();
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    if bytes.is_empty() || bytes.len() >= address.sun_path.len() {
        return Err("invalid broker packet socket path".into());
    }
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (index, byte) in bytes.iter().enumerate() {
        address.sun_path[index] = *byte as libc::c_char;
    }
    let fd = unsafe { libc::socket(libc::AF_UNIX,
        libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC | libc::SOCK_NONBLOCK, 0) };
    if fd < 0 { return Err("cannot create broker packet socket".into()); }
    let socket = unsafe { OwnedFd::from_raw_fd(fd) };
    let enabled: libc::c_int = 1;
    if unsafe { libc::setsockopt(fd, libc::SOL_SOCKET, libc::SO_PASSCRED,
        &enabled as *const _ as *const _, std::mem::size_of_val(&enabled) as libc::socklen_t) } < 0 {
        return Err("cannot require broker packet credentials".into());
    }
    let length = (std::mem::offset_of!(libc::sockaddr_un, sun_path) + bytes.len() + 1)
        as libc::socklen_t;
    let connected = unsafe { libc::connect(fd, &address as *const _ as *const libc::sockaddr, length) };
    if connected < 0 && std::io::Error::last_os_error().raw_os_error() != Some(libc::EINPROGRESS) {
        return Err("broker packet connect failed".into());
    }
    if connected < 0 {
        packet_ready(fd, libc::POLLOUT, deadline)?;
        let mut error: libc::c_int = 0;
        let mut size = std::mem::size_of_val(&error) as libc::socklen_t;
        if unsafe { libc::getsockopt(fd, libc::SOL_SOCKET, libc::SO_ERROR,
            &mut error as *mut _ as *mut _, &mut size) } < 0 || error != 0 {
            return Err("broker packet connect failed".into());
        }
    }
    Ok(socket)
}

#[allow(dead_code)]
fn packet_ready(fd: libc::c_int, events: libc::c_short, deadline: Instant) -> Result<(), String> {
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() { return Err("broker packet deadline exceeded".into()); }
        let mut pollfd = libc::pollfd { fd, events, revents: 0 };
        let ready = unsafe { libc::poll(&mut pollfd, 1,
            remaining.as_millis().max(1).min(i32::MAX as u128) as i32) };
        if ready < 0 && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
            continue;
        }
        if ready <= 0 { return Err("broker packet deadline exceeded".into()); }
        if pollfd.revents & events == 0 { return Err("broker packet connection closed".into()); }
        return Ok(());
    }
}

#[allow(dead_code)]
fn receive_root_packet(fd: libc::c_int, deadline: Instant) -> Result<Value, String> {
    let mut body = [0u8; MAX_WIRE_BYTES + 1];
    // Control storage is word-aligned for cmsghdr and large enough for one
    // ucred; any extra ancillary message or truncation fails closed below.
    let mut control = [0usize; 8];
    loop {
        packet_ready(fd, libc::POLLIN, deadline)?;
        let mut iov = libc::iovec { iov_base: body.as_mut_ptr().cast(), iov_len: body.len() };
        let mut message: libc::msghdr = unsafe { std::mem::zeroed() };
        message.msg_iov = &mut iov;
        message.msg_iovlen = 1;
        message.msg_control = control.as_mut_ptr().cast();
        message.msg_controllen = std::mem::size_of_val(&control);
        let count = unsafe { libc::recvmsg(fd, &mut message, libc::MSG_DONTWAIT) };
        if count < 0 && std::io::Error::last_os_error().kind() == std::io::ErrorKind::WouldBlock {
            continue;
        }
        if count <= 0 { return Err("broker packet receive failed".into()); }
        if count as usize > MAX_WIRE_BYTES || message.msg_flags & (libc::MSG_TRUNC | libc::MSG_CTRUNC) != 0 {
            return Err("broker packet reply exceeds bound".into());
        }
        let mut credential = None;
        let mut header = unsafe { libc::CMSG_FIRSTHDR(&message) };
        while !header.is_null() {
            let valid = unsafe { (*header).cmsg_level == libc::SOL_SOCKET
                && (*header).cmsg_type == libc::SCM_CREDENTIALS
                && (*header).cmsg_len as usize == libc::CMSG_LEN(std::mem::size_of::<libc::ucred>() as u32) as usize };
            if !valid || credential.is_some() {
                return Err("broker packet has unexpected credentials".into());
            }
            credential = Some(unsafe { std::ptr::read_unaligned(libc::CMSG_DATA(header).cast::<libc::ucred>()) });
            header = unsafe { libc::CMSG_NXTHDR(&message, header) };
        }
        let credential = credential.ok_or("broker packet has no sender credentials")?;
        if credential.pid <= 0 || credential.uid != 0 || credential.gid != 0 {
            return Err("broker packet sender is not UID/GID zero in caller namespace".into());
        }
        return serde_json::from_slice(&body[..count as usize])
            .map_err(|_| "invalid broker packet reply JSON".into());
    }
}

/// Guest-only check of a reply *sender* on one packet. Still returns unknown:
/// the client's namespaces, its binary/configuration, and any analyzer stream
/// are outside this observation. Nothing in the CLI calls it.
#[allow(dead_code)]
pub(crate) fn observe_separate_sender(path: &Path, plan: &ProducerPlan,
    edge: &CallEdge, candidate: &CallCandidate) -> Result<Value, String> {
    if unsafe { libc::geteuid() } == 0 { return Err("broker client must run unprivileged".into()); }
    let inode = root_owned_socket(path)?;
    let deadline = Instant::now() + DEADLINE;
    let socket = bounded_packet_connect(path, deadline)?;
    // Keep the old listener observation explicitly separate from the
    // credentials attached to the packet sent after accept/handoff.
    uid_zero_peer_fd(socket.as_raw_fd())?;
    if root_owned_socket(path)? != inode { return Err("broker socket changed during connect".into()); }
    let nonce = challenge_nonce()?;
    let digest = query_digest(plan, edge, candidate)?;
    let request = json!({"protocol":PROTOCOL,"nonce":nonce,"query_sha256":digest,
        "operation":"observe_only"});
    let body = serde_json::to_vec(&request).map_err(|_| "cannot encode broker challenge")?;
    if body.len() > MAX_WIRE_BYTES { return Err("broker challenge exceeds bound".into()); }
    packet_ready(socket.as_raw_fd(), libc::POLLOUT, deadline)?;
    let sent = unsafe { libc::send(socket.as_raw_fd(), body.as_ptr().cast(), body.len(), libc::MSG_NOSIGNAL | libc::MSG_DONTWAIT) };
    if sent != body.len() as isize { return Err("broker packet challenge send failed".into()); }
    let reply = receive_root_packet(socket.as_raw_fd(), deadline)?;
    match_reply(&reply, &nonce, &digest)?;
    if root_owned_socket(path)? != inode { return Err("broker socket changed during exchange".into()); }
    Ok(json!({"status":"unknown","binding":"unknown",
        "socket_observation":"uid_zero_reply_sender_untrusted",
        "reason":"namespace_client_and_stream_unproven","producer":"not_started"}))
}

/// Record only UID/inode observations and echo of one exact query. This
/// private seam never authenticates a producer, starts an analyzer, accepts
/// producer claims, or changes `binding: unknown`.
#[allow(dead_code)]
pub(crate) fn observe_socket(path: &Path, plan: &ProducerPlan,
    edge: &CallEdge, candidate: &CallCandidate) -> Result<Value, String> {
    if unsafe { libc::geteuid() } == 0 {
        return Err("broker client must run unprivileged".into());
    }
    let inode = root_owned_socket(path)?;
    let deadline = Instant::now() + DEADLINE;
    let mut stream = bounded_unix_connect(path, deadline)?;
    uid_zero_peer(&stream)?;
    if root_owned_socket(path)? != inode { return Err("broker socket changed during connect".into()); }
    stream.set_nonblocking(true).map_err(|_| "cannot configure broker socket")?;
    let nonce = challenge_nonce()?;
    let digest = query_digest(plan, edge, candidate)?;
    let request = json!({"protocol":PROTOCOL,"nonce":nonce,"query_sha256":digest,
        "operation":"observe_only"});
    let body = serde_json::to_vec(&request).map_err(|_| "cannot encode broker challenge")?;
    if body.len() > MAX_WIRE_BYTES { return Err("broker challenge exceeds bound".into()); }
    let mut frame = (body.len() as u32).to_be_bytes().to_vec();
    frame.extend(body);
    io_until(&mut stream, &mut frame, true, deadline)?;
    let reply = read_reply(&mut stream, deadline)?;
    match_reply(&reply, &nonce, &digest)?;
    if root_owned_socket(path)? != inode { return Err("broker socket changed during exchange".into()); }
    Ok(untrusted_status())
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::semantic_producer::plan_rust_analyzer;
    use super::super::{query, Query};
    use std::os::unix::net::UnixListener;
    use std::process::Command;

    const IMAGE: &str = "reviewed/rust-analyzer@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    #[test]
    fn same_uid_fake_broker_is_rejected_before_any_challenge() {
        let worktree = tempfile::tempdir().unwrap();
        assert!(Command::new("git").arg("init").arg("-q").arg(worktree.path()).status().unwrap().success());
        fs::write(worktree.path().join("a.rs"), "fn caller() { target(); }\nfn target() {}\n").unwrap();
        assert!(Command::new("git").arg("add").arg("a.rs").current_dir(worktree.path()).status().unwrap().success());
        let answer = query(worktree.path(), Query::Calls("a.rs".into())).unwrap();
        let edge = answer.edges.iter().find(|edge| edge.target == "target").unwrap();
        let candidate = edge.candidates.first().unwrap();
        let plan = plan_rust_analyzer(worktree.path(), IMAGE,
            "unix:///run/user/1000/docker.sock", true).unwrap();
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("producer.sock");
        let _server = UnixListener::bind(&socket).unwrap();
        let error = observe_socket(&socket, &plan, edge, candidate).unwrap_err();
        assert!(error.contains("UID-zero-owned"), "{error}");
    }

    #[test]
    fn same_uid_fake_packet_broker_is_rejected_before_any_challenge() {
        if unsafe { libc::geteuid() } == 0 { return; }
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("anchor.sock");
        let fd = unsafe { libc::socket(libc::AF_UNIX,
            libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC, 0) };
        assert!(fd >= 0);
        let listener = unsafe { OwnedFd::from_raw_fd(fd) };
        let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
        address.sun_family = libc::AF_UNIX as libc::sa_family_t;
        use std::os::unix::ffi::OsStrExt;
        let bytes = path.as_os_str().as_bytes();
        for (index, byte) in bytes.iter().enumerate() {
            address.sun_path[index] = *byte as libc::c_char;
        }
        let length = (std::mem::offset_of!(libc::sockaddr_un, sun_path) + bytes.len() + 1)
            as libc::socklen_t;
        assert_eq!(unsafe { libc::bind(listener.as_raw_fd(),
            &address as *const _ as *const libc::sockaddr, length) }, 0);
        assert_eq!(unsafe { libc::listen(listener.as_raw_fd(), 1) }, 0);
        let plan = ProducerPlan { root: PathBuf::from("/work"), image: IMAGE.into(),
            docker_host: "unix:///run/user/1000/docker.sock".into(), args: Vec::new(),
            initialize: json!({}), attestation: "unavailable_runtime_not_started" };
        let edge = CallEdge { file: "a.rs".into(), line: 1, column: 14,
            caller: "caller".into(), target: "target".into(), form: "function_path",
            binding: "unknown", reason: "syntax_candidate", candidates: Vec::new(),
            omitted_candidates: 0, sha256: "a".repeat(64), read_unix_ms: 0 };
        let candidate = CallCandidate { file: "a.rs".into(), line: 2,
            qualified: "target".into(), sha256: "a".repeat(64), read_unix_ms: 0 };
        let error = observe_separate_sender(&path, &plan, &edge, &candidate).unwrap_err();
        assert!(error.contains("UID-zero-owned"), "{error}");
    }

    #[test]
    fn reply_requires_exact_nonce_query_and_observation_only_status() {
        let nonce = "a".repeat(64);
        let digest = "b".repeat(64);
        let valid = json!({"protocol":PROTOCOL,"nonce":nonce,
            "query_sha256":digest,"status":"observation_only"});
        assert!(match_reply(&valid, &nonce, &digest).is_ok());
        for key in ["protocol", "nonce", "query_sha256", "status"] {
            let mut invalid = valid.clone();
            invalid[key] = json!("forged");
            assert!(match_reply(&invalid, &nonce, &digest).is_err(), "{key}");
        }
        let mut extra = valid.clone();
        extra["binding"] = json!("verified");
        assert!(match_reply(&extra, &nonce, &digest).is_err());
        let mut promoted = valid.clone();
        promoted["status"] = json!("attested");
        assert!(match_reply(&promoted, &nonce, &digest).is_err());
        let status = untrusted_status();
        assert_eq!(status["binding"], "unknown");
        assert_eq!(status["reason"], "namespace_and_fd_handoff_unproven");
    }

    #[test]
    fn wire_reply_is_bounded_and_deadline_is_absolute() {
        let (mut client, mut server) = UnixStream::pair().unwrap();
        client.set_nonblocking(true).unwrap();
        server.write_all(&((MAX_WIRE_BYTES + 1) as u32).to_be_bytes()).unwrap();
        assert!(read_reply(&mut client, Instant::now() + Duration::from_secs(1))
            .unwrap_err().contains("bound"));
        let (mut client, _silent_server) = UnixStream::pair().unwrap();
        client.set_nonblocking(true).unwrap();
        let start = Instant::now();
        assert!(read_reply(&mut client, start + Duration::from_millis(30))
            .unwrap_err().contains("deadline"));
        assert!(start.elapsed() < Duration::from_millis(250));
    }

    #[test]
    fn packet_reply_rejects_a_same_uid_sender_and_oversize_reply() {
        if unsafe { libc::geteuid() } == 0 { return; }
        let mut descriptors = [0; 2];
        assert_eq!(unsafe { libc::socketpair(libc::AF_UNIX,
            libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC, 0, descriptors.as_mut_ptr()) }, 0);
        let client = unsafe { OwnedFd::from_raw_fd(descriptors[0]) };
        let server = unsafe { OwnedFd::from_raw_fd(descriptors[1]) };
        let enabled: libc::c_int = 1;
        assert_eq!(unsafe { libc::setsockopt(client.as_raw_fd(), libc::SOL_SOCKET,
            libc::SO_PASSCRED, &enabled as *const _ as *const _,
            std::mem::size_of_val(&enabled) as libc::socklen_t) }, 0);
        let reply = br#"{"protocol":"doxa-semantic-socket-observation-v1"}"#;
        assert_eq!(unsafe { libc::send(server.as_raw_fd(), reply.as_ptr().cast(),
            reply.len(), libc::MSG_NOSIGNAL) }, reply.len() as isize);
        let error = receive_root_packet(client.as_raw_fd(), Instant::now() + Duration::from_secs(1)).unwrap_err();
        assert!(error.contains("sender is not UID/GID zero"), "{error}");
        let oversized = vec![b'x'; MAX_WIRE_BYTES + 1];
        assert_eq!(unsafe { libc::send(server.as_raw_fd(), oversized.as_ptr().cast(),
            oversized.len(), libc::MSG_NOSIGNAL) }, oversized.len() as isize);
        let error = receive_root_packet(client.as_raw_fd(), Instant::now() + Duration::from_secs(1)).unwrap_err();
        assert!(error.contains("exceeds bound"), "{error}");
    }

    #[test]
    fn packet_reply_deadline_is_absolute() {
        let mut descriptors = [0; 2];
        assert_eq!(unsafe { libc::socketpair(libc::AF_UNIX,
            libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC, 0, descriptors.as_mut_ptr()) }, 0);
        let client = unsafe { OwnedFd::from_raw_fd(descriptors[0]) };
        let _server = unsafe { OwnedFd::from_raw_fd(descriptors[1]) };
        let start = Instant::now();
        let error = receive_root_packet(client.as_raw_fd(), start + Duration::from_millis(30)).unwrap_err();
        assert!(error.contains("deadline"), "{error}");
        assert!(start.elapsed() < Duration::from_millis(250));
    }

    /// Run only in the offline guest with a root-owned, separately started
    /// broker. The unprivileged test process cannot replace that endpoint.
    #[test]
    #[ignore]
    fn disposable_guest_message_sender_boundary() {
        let mode = std::env::var("DOXA_ANCHOR_MODE").unwrap();
        assert!(matches!(mode.as_str(), "root" | "drop" | "handoff"));
        let path = Path::new("/run/doxa-semantic/anchor.sock");
        let plan = ProducerPlan { root: PathBuf::from("/work"), image: IMAGE.into(),
            docker_host: "unix:///run/user/1000/docker.sock".into(), args: Vec::new(),
            initialize: json!({}), attestation: "unavailable_runtime_not_started" };
        let edge = CallEdge { file: "a.rs".into(), line: 1, column: 14,
            caller: "caller".into(), target: "target".into(), form: "function_path",
            binding: "unknown", reason: "syntax_candidate", candidates: Vec::new(),
            omitted_candidates: 0, sha256: "a".repeat(64), read_unix_ms: 0 };
        let candidate = CallCandidate { file: "a.rs".into(), line: 2,
            qualified: "target".into(), sha256: "a".repeat(64), read_unix_ms: 0 };
        let result = observe_separate_sender(path, &plan, &edge, &candidate);
        if mode == "root" {
            let status = result.unwrap();
            assert_eq!(status["socket_observation"], "uid_zero_reply_sender_untrusted");
            assert_eq!(status["binding"], "unknown");
            assert_eq!(status["producer"], "not_started");
            println!("DOXA_ANCHOR_RECEIPT mode=root listener_uid=0 reply_sender_uid=0 binding=unknown");
        } else {
            let error = result.unwrap_err();
            assert!(error.contains("sender is not UID/GID zero"), "{mode}: {error}");
            println!("DOXA_ANCHOR_RECEIPT mode={mode} listener_uid=0 reply_sender_rejected={error}");
        }
        assert!(fs::remove_file(path).is_err(), "guest client replaced the root socket");
    }

    /// Run only inside a disposable guest. The root listener drops to UID
    /// 1000 before accept, but SO_PEERCRED still reports the listening UID.
    #[test]
    #[ignore]
    fn disposable_guest_listener_drop_stays_untrusted() {
        let path = Path::new("/run/doxa-semantic/producer.sock");
        let plan = ProducerPlan { root: PathBuf::from("/work"), image: IMAGE.into(),
            docker_host: "unix:///run/user/1000/docker.sock".into(), args: Vec::new(),
            initialize: json!({}), attestation: "unavailable_runtime_not_started" };
        let edge = CallEdge { file: "a.rs".into(), line: 1, column: 14,
            caller: "caller".into(), target: "target".into(), form: "function_path",
            binding: "unknown", reason: "syntax_candidate", candidates: Vec::new(),
            omitted_candidates: 0, sha256: "a".repeat(64), read_unix_ms: 0 };
        let candidate = CallCandidate { file: "a.rs".into(), line: 2,
            qualified: "target".into(), sha256: "a".repeat(64), read_unix_ms: 0 };
        let status = observe_socket(path, &plan, &edge, &candidate).unwrap();
        assert_eq!(status["socket_observation"], "uid_zero_echo_untrusted");
        assert_eq!(status["reason"], "namespace_and_fd_handoff_unproven");
        assert_eq!(status["producer"], "not_started");
        assert_eq!(status["binding"], "unknown");
        assert!(fs::remove_file(path).is_err(), "guest client replaced the root socket");
    }
}
