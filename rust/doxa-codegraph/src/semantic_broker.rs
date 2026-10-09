//! Disabled socket-observation handshake for a future semantic broker.
//!
//! Path ownership and SO_PEERCRED values are namespace-relative. Credentials
//! may describe the original listener even after it hands the FD to an
//! unprivileged worker. These observations do not authenticate a live
//! producer or return a semantic binding. Nothing in the CLI calls this.

use super::semantic_evidence::inspect_definition_reply;
use super::semantic_producer::{plan_rust_analyzer, read_lsp_frame, ProducerPlan};
use super::semantic_runtime::bounded_unix_connect;
use super::{current_scan_input_sha256, file_bytes, source_language, Answer, CallCandidate, CallEdge};
use serde::de::{MapAccess, Visitor};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{Cursor, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::os::unix::net::UnixStream;
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, Instant};

const PROTOCOL: &str = "doxa-semantic-socket-observation-v1";
const STREAM_PROTOCOL: &str = "doxa-semantic-stream-observation-v1";
const MAX_WIRE_BYTES: usize = 4096;
const MAX_STREAM_BYTES: usize = 32 * 1024;
const MAX_STREAM_CHUNKS: usize = 32;
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

// Bind this particular stream challenge to the complete Git-listed Rust
// source digest from the syntax query. The hash is a local observation, not a
// snapshot of every byte visible in the analyzer's eventual mount.
fn stream_query_digest(plan: &ProducerPlan, edge: &CallEdge,
    candidate: &CallCandidate, rust_scan: &str) -> Result<String, String> {
    let query = query_digest(plan, edge, candidate)?;
    let bytes = serde_json::to_vec(&json!({"protocol":STREAM_PROTOCOL,
        "query_sha256":query,"rust_scan_input_sha256":rust_scan}))
        .map_err(|_| "cannot encode source-bound stream query")?;
    Ok(sha256_hex(&bytes))
}

fn with_rust_scan_basis<T>(root: &Path, expected: &str,
    exchange: impl FnOnce(&str) -> Result<T, String>) -> Result<(T, String), String> {
    if expected.len() != 64 || !expected.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err("missing or invalid Rust scan input digest from syntax query".into());
    }
    let (before, count) = current_scan_input_sha256(root)?;
    if before != expected { return Err("Rust source inventory changed since syntax query".into()); }
    let result = exchange(&before)?;
    let (after, after_count) = current_scan_input_sha256(root)?;
    if after != before || after_count != count {
        return Err("Rust source inventory changed during stream observation".into());
    }
    Ok((result, before))
}

// The stream must start from a complete calls answer, not a digest recomputed
// by the caller after an unrelated file changed or failed to parse. Selecting
// by index also keeps the edge and candidate tied to displayed query rows.
fn complete_call_basis<'a>(plan: &ProducerPlan, answer: &'a Answer,
    edge_index: usize, candidate_index: usize)
    -> Result<(&'a CallEdge, &'a CallCandidate, &'a str), String> {
    let root = plan.root.to_str().ok_or("non-UTF-8 semantic worktree")?;
    if answer.scope != root || answer.query != "calls" || answer.status != "ok"
        || source_language(&answer.value) != Some("rust")
        || answer.coverage.rust_skipped_files != 0
        || answer.coverage.rust_unparseable_files != 0 {
        return Err("semantic stream requires an originating complete Rust calls answer".into());
    }
    let scan = answer.scan_input_sha256.as_deref()
        .ok_or("semantic stream requires a complete Rust scan input digest")?;
    let edge = answer.edges.get(edge_index).ok_or("call edge was not displayed in answer")?;
    let candidate = edge.candidates.get(candidate_index)
        .ok_or("call candidate was not displayed in answer")?;
    if answer.value != edge.file || answer.requested_source_sha256.as_deref() != Some(edge.sha256.as_str()) {
        return Err("call edge differs from originating answer source".into());
    }
    Ok((edge, candidate, scan))
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

/// The packet protocol has only scalar top-level fields. Reject duplicate
/// JSON names before Value parsing can silently retain the last occurrence.
fn unique_packet_object(bytes: &[u8]) -> Result<Value, String> {
    struct Unique(Value);
    impl<'de> Deserialize<'de> for Unique {
        fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
            struct UniqueVisitor;
            impl<'de> Visitor<'de> for UniqueVisitor {
                type Value = Unique;
                fn expecting(&self, formatter: &mut std::fmt::Formatter) -> std::fmt::Result {
                    formatter.write_str("a broker packet object with unique fields")
                }
                fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
                    let mut fields = serde_json::Map::new();
                    while let Some((key, value)) = map.next_entry::<String, Value>()? {
                        if fields.insert(key, value).is_some() {
                            return Err(serde::de::Error::custom("duplicate broker packet field"));
                        }
                    }
                    Ok(Unique(Value::Object(fields)))
                }
            }
            deserializer.deserialize_map(UniqueVisitor)
        }
    }
    let mut decoder = serde_json::Deserializer::from_slice(bytes);
    let parsed = Unique::deserialize(&mut decoder)
        .map_err(|_| "invalid or duplicate broker packet JSON".to_owned())?;
    decoder.end().map_err(|_| "trailing broker packet JSON".to_owned())?;
    Ok(parsed.0)
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
fn receive_root_packet_with_sender(fd: libc::c_int, deadline: Instant) -> Result<(Value, libc::ucred), String> {
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
        let value = unique_packet_object(&body[..count as usize])?;
        return Ok((value, credential));
    }
}

#[allow(dead_code)]
fn receive_root_packet(fd: libc::c_int, deadline: Instant) -> Result<Value, String> {
    receive_root_packet_with_sender(fd, deadline).map(|(value, _)| value)
}

#[allow(dead_code)]
struct StreamAssembler<'a> {
    nonce: &'a str,
    query: &'a str,
    source: &'a str,
    target: &'a str,
    rust_scan: &'a str,
    sender_pid: Option<libc::pid_t>,
    cid: Option<String>,
    bytes: Vec<u8>,
    chunks: usize,
    stage: u8,
}

#[allow(dead_code)]
impl<'a> StreamAssembler<'a> {
    fn new(nonce: &'a str, query: &'a str, source: &'a str, target: &'a str,
        rust_scan: &'a str) -> Self {
        Self { nonce, query, source, target, rust_scan, sender_pid: None, cid: None,
            bytes: Vec::new(), chunks: 0, stage: 0 }
    }

    fn accept(&mut self, packet: &Value, sender_pid: libc::pid_t) -> Result<bool, String> {
        if sender_pid <= 0 { return Err("invalid broker stream sender PID".into()); }
        if self.sender_pid.is_some_and(|pid| pid != sender_pid) {
            return Err("broker stream sender changed".into());
        }
        self.sender_pid.get_or_insert(sender_pid);
        let object = packet.as_object().ok_or("broker stream packet is not an object")?;
        if packet.get("protocol").and_then(Value::as_str) != Some(STREAM_PROTOCOL)
            || packet.get("nonce").and_then(Value::as_str) != Some(self.nonce)
            || packet.get("query_sha256").and_then(Value::as_str) != Some(self.query) {
            return Err("broker stream packet does not match challenge".into());
        }
        let cid = packet.get("cid").and_then(Value::as_str).ok_or("missing broker stream CID")?;
        if cid.len() != 64 || !cid.bytes().all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()) {
            return Err("invalid broker stream CID".into());
        }
        if self.cid.as_deref().is_some_and(|expected| expected != cid) {
            return Err("broker stream CID changed".into());
        }
        let phase = packet.get("phase").and_then(Value::as_str).ok_or("missing broker stream phase")?;
        match (self.stage, phase) {
            (0, "opened") => {
                if object.len() != 10 || packet.get("source_sha256").and_then(Value::as_str) != Some(self.source)
                    || packet.get("target_sha256").and_then(Value::as_str) != Some(self.target)
                    || packet.get("rust_scan_input_sha256").and_then(Value::as_str) != Some(self.rust_scan)
                    || packet.get("status").and_then(Value::as_str) != Some("observation_only")
                    || !packet.get("image_id").and_then(Value::as_str).is_some_and(valid_image_id) {
                    return Err("broker stream opening is incomplete or claims authority".into());
                }
                self.cid = Some(cid.to_owned());
                self.stage = 1;
                Ok(false)
            }
            (1, "chunk") => {
                if object.len() != 7 || packet.get("sequence").and_then(Value::as_u64) != Some(self.chunks as u64)
                    || self.chunks >= MAX_STREAM_CHUNKS {
                    return Err("broker stream sequence or chunk limit invalid".into());
                }
                let hex = packet.get("data_hex").and_then(Value::as_str).ok_or("missing broker stream bytes")?;
                if hex.is_empty() || hex.len() > 2048 || hex.len() % 2 != 0
                    || self.bytes.len() + hex.len() / 2 > MAX_STREAM_BYTES {
                    return Err("broker stream bytes exceed bound".into());
                }
                for pair in hex.as_bytes().chunks_exact(2) {
                    let pair = std::str::from_utf8(pair).map_err(|_| "invalid broker stream hex")?;
                    self.bytes.push(u8::from_str_radix(pair, 16).map_err(|_| "invalid broker stream hex")?);
                }
                self.chunks += 1;
                Ok(false)
            }
            (1, "closed") => {
                if object.len() != 9 || self.chunks == 0
                    || packet.get("chunks").and_then(Value::as_u64) != Some(self.chunks as u64)
                    || packet.get("stream_sha256").and_then(Value::as_str) != Some(sha256_hex(&self.bytes).as_str())
                    || packet.get("rust_scan_input_sha256").and_then(Value::as_str) != Some(self.rust_scan)
                    || packet.get("status").and_then(Value::as_str) != Some("observation_only") {
                    return Err("broker stream closure is incomplete or mismatched".into());
                }
                self.stage = 2;
                Ok(true)
            }
            _ => Err("broker stream phase is out of order".into()),
        }
    }

    fn lsp_reply(&self) -> Result<Value, String> {
        if self.stage != 2 { return Err("broker stream was not closed".into()); }
        let mut cursor = Cursor::new(&self.bytes);
        let reply = read_lsp_frame(&mut cursor)?;
        if cursor.position() != self.bytes.len() as u64 {
            return Err("broker stream contained trailing LSP bytes".into());
        }
        Ok(reply)
    }
}

fn valid_image_id(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|digest| digest.len() == 64
        && digest.bytes().all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()))
}

/// The broker must close its seqpacket connection after the terminal packet.
/// Waiting for EOF rules out a queued second transcript and keeps a half-open
/// broker from making a completed observation look successful.
#[allow(dead_code)]
fn require_stream_eof(fd: libc::c_int, deadline: Instant) -> Result<(), String> {
    packet_ready(fd, libc::POLLIN, deadline)?;
    let mut byte = [0u8; 1];
    let count = unsafe { libc::recv(fd, byte.as_mut_ptr().cast(), byte.len(), libc::MSG_DONTWAIT) };
    if count == 0 { Ok(()) } else { Err("broker stream has extra data or did not close".into()) }
}

/// This disabled exchange ties one query digest and two source hashes to a
/// bounded packet stream from the same observed UID-zero sender PID. It does
/// not prove that sender's code, the Engine, or the actual analyzer byte path.
#[allow(dead_code)]
fn observe_stream_packets(path: &Path, nonce: &str, digest: &str,
    source: &str, target: &str, rust_scan: &str) -> Result<Value, String> {
    if unsafe { libc::geteuid() } == 0 { return Err("broker client must run unprivileged".into()); }
    let inode = root_owned_socket(path)?;
    let deadline = Instant::now() + DEADLINE;
    let socket = bounded_packet_connect(path, deadline)?;
    uid_zero_peer_fd(socket.as_raw_fd())?;
    if root_owned_socket(path)? != inode { return Err("broker socket changed during connect".into()); }
    let request = json!({"protocol":STREAM_PROTOCOL,"operation":"observe_stream",
        "nonce":nonce,"query_sha256":digest,"source_sha256":source,"target_sha256":target,
        "rust_scan_input_sha256":rust_scan});
    let body = serde_json::to_vec(&request).map_err(|_| "cannot encode broker stream challenge")?;
    if body.len() > MAX_WIRE_BYTES { return Err("broker stream challenge exceeds bound".into()); }
    packet_ready(socket.as_raw_fd(), libc::POLLOUT, deadline)?;
    let sent = unsafe { libc::send(socket.as_raw_fd(), body.as_ptr().cast(), body.len(),
        libc::MSG_NOSIGNAL | libc::MSG_DONTWAIT) };
    if sent != body.len() as isize { return Err("broker stream challenge send failed".into()); }
    let mut assembler = StreamAssembler::new(nonce, digest, source, target, rust_scan);
    for _ in 0..(MAX_STREAM_CHUNKS + 2) {
        let (packet, sender) = receive_root_packet_with_sender(socket.as_raw_fd(), deadline)?;
        if assembler.accept(&packet, sender.pid)? {
            require_stream_eof(socket.as_raw_fd(), deadline)?;
            if root_owned_socket(path)? != inode { return Err("broker socket changed during stream".into()); }
            return assembler.lsp_reply();
        }
    }
    Err("broker stream packet count exceeded".into())
}

/// Library-only semantic stream observation. Every accepted response still
/// has an unknown binding: the broker's attested launch/inspect/attach chain
/// is absent. The CLI has no call to this function.
#[allow(dead_code)]
pub(crate) fn observe_stream_definition(path: &Path, plan: &ProducerPlan,
    answer: &Answer, edge_index: usize, candidate_index: usize) -> Result<Value, String> {
    let expected = plan_rust_analyzer(&plan.root, &plan.image, &plan.docker_host, true)?;
    if plan.root != expected.root || plan.args != expected.args
        || plan.initialize != expected.initialize || plan.attestation != expected.attestation {
        return Err("semantic producer plan changed before stream challenge".into());
    }
    let (edge, candidate, rust_scan_input_sha256) = complete_call_basis(plan, answer,
        edge_index, candidate_index)?;
    let (_, source, _) = file_bytes(&plan.root, &edge.file)?;
    let (_, target, _) = file_bytes(&plan.root, &candidate.file)?;
    if source != edge.sha256 || target != candidate.sha256 {
        return Err("semantic source changed before stream challenge".into());
    }
    let (evidence, rust_scan) = with_rust_scan_basis(&plan.root, rust_scan_input_sha256, |basis| {
        let nonce = challenge_nonce()?;
        let digest = stream_query_digest(plan, edge, candidate, basis)?;
        let response = observe_stream_packets(path, &nonce, &digest, &source, &target, basis)?;
        let request = json!({"jsonrpc":"2.0","id":7,"method":"textDocument/definition",
            "params":{"textDocument":{"uri":format!("file://{}/{}",plan.root.display(),edge.file)},
                "position":{"line":edge.line.checked_sub(1).ok_or("invalid source line")?,
                    "character":edge.column}}});
        inspect_definition_reply(&plan.root, edge, candidate, &request, &response)
    })?;
    for (relative, expected) in [(&edge.file, &source), (&candidate.file, &target)] {
        if file_bytes(&plan.root, relative)?.1 != *expected {
            return Err("semantic source changed after stream observation".into());
        }
    }
    Ok(json!({"status":"protocol_match_untrusted","binding":"unknown",
        "socket_observation":"one_sender_stream_untrusted",
        "reason":"analyzer_engine_and_broker_binary_unproven",
        "rust_scan_input_sha256":rust_scan,"evidence":evidence}))
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

    fn stream_fixture() -> (String, String, String, String, String, Value, Value, Value) {
        let nonce = "a".repeat(64);
        let query = "b".repeat(64);
        let source = "c".repeat(64);
        let target = "d".repeat(64);
        let rust_scan = "a".repeat(64);
        let cid = "e".repeat(64);
        let frame = b"Content-Length: 2\r\n\r\n{}";
        let opened = json!({"protocol":STREAM_PROTOCOL,"nonce":nonce,"query_sha256":query,
            "cid":cid,"phase":"opened","source_sha256":source,
            "target_sha256":target,"rust_scan_input_sha256":rust_scan,
            "image_id":format!("sha256:{}", "f".repeat(64)),
            "status":"observation_only"});
        let chunk = json!({"protocol":STREAM_PROTOCOL,"nonce":nonce,"query_sha256":query,
            "cid":cid,"phase":"chunk","sequence":0,
            "data_hex":frame.iter().map(|byte| format!("{byte:02x}")).collect::<String>()});
        let closed = json!({"protocol":STREAM_PROTOCOL,"nonce":nonce,"query_sha256":query,
            "cid":cid,"phase":"closed","chunks":1,"stream_sha256":sha256_hex(frame),
            "rust_scan_input_sha256":rust_scan,"status":"observation_only"});
        (nonce, query, source, target, rust_scan, opened, chunk, closed)
    }

    #[test]
    fn stream_observation_requires_one_ordered_sender_and_exact_lsp_frame() {
        let (nonce, query, source, target, rust_scan, opened, chunk, closed) = stream_fixture();
        let mut stream = StreamAssembler::new(&nonce, &query, &source, &target, &rust_scan);
        assert!(!stream.accept(&opened, 101).unwrap());
        assert!(!stream.accept(&chunk, 101).unwrap());
        assert!(stream.accept(&closed, 101).unwrap());
        assert_eq!(stream.lsp_reply().unwrap(), json!({}));
        assert!(stream.accept(&closed, 101).unwrap_err().contains("out of order"));
    }

    #[test]
    fn stream_packet_json_rejects_duplicate_and_trailing_fields() {
        assert!(unique_packet_object(br#"{"phase":"opened","phase":"closed"}"#)
            .unwrap_err().contains("duplicate"));
        assert!(unique_packet_object(br#"{"phase":"opened"}{}"#)
            .unwrap_err().contains("trailing"));
        assert!(unique_packet_object(br#"["opened"]"#).is_err());
    }

    #[test]
    fn stream_observation_rejects_packet_splice_replay_and_claimed_authority() {
        let (nonce, query, source, target, rust_scan, opened, chunk, closed) = stream_fixture();
        let fresh = || StreamAssembler::new(&nonce, &query, &source, &target, &rust_scan);
        for (field, wrong) in [("nonce", json!("old")), ("query_sha256", json!("old")),
            ("source_sha256", json!("old")), ("target_sha256", json!("old")),
            ("rust_scan_input_sha256", json!("old")),
            ("status", json!("attested")), ("image_id", json!("latest"))] {
            let mut changed = opened.clone(); changed[field] = wrong;
            assert!(fresh().accept(&changed, 101).is_err(), "{field}");
        }
        let mut claimed = opened.clone(); claimed["binding"] = json!("verified");
        assert!(fresh().accept(&claimed, 101).is_err());
        let mut stream = fresh();
        assert!(stream.accept(&chunk, 101).is_err());
        stream.accept(&opened, 101).unwrap();
        assert!(stream.accept(&chunk, 102).unwrap_err().contains("sender changed"));
        for (field, wrong) in [("cid", json!("f".repeat(64))),
            ("sequence", json!(1)), ("data_hex", json!("zz")),
            ("nonce", json!("old"))] {
            let mut changed = chunk.clone(); changed[field] = wrong;
            assert!(fresh().accept(&opened, 101).is_ok());
            let mut stream = fresh(); stream.accept(&opened, 101).unwrap();
            assert!(stream.accept(&changed, 101).is_err(), "{field}");
        }
        let mut stream = fresh(); stream.accept(&opened, 101).unwrap();
        stream.accept(&chunk, 101).unwrap();
        for (field, wrong) in [("chunks", json!(2)), ("stream_sha256", json!("0".repeat(64))),
            ("rust_scan_input_sha256", json!("0".repeat(64))),
            ("cid", json!("f".repeat(64))), ("status", json!("verified"))] {
            let mut changed = closed.clone(); changed[field] = wrong;
            assert!(stream.accept(&changed, 101).is_err(), "{field}");
        }
        let mut trailing = chunk.clone(); trailing["data_hex"] = json!("00");
        let mut stream = fresh(); stream.accept(&opened, 101).unwrap();
        stream.accept(&chunk, 101).unwrap(); stream.accept(&trailing, 101).unwrap_err();
    }

    #[test]
    fn stream_definition_rederives_plan_and_rehashes_source_before_connect() {
        let root = tempfile::tempdir().unwrap();
        assert!(Command::new("git").arg("init").arg("-q").arg(root.path()).status().unwrap().success());
        fs::write(root.path().join("a.rs"), "fn caller() { target(); }\nfn target() {}\n").unwrap();
        fs::write(root.path().join("third.rs"), "fn unrelated() {}\n").unwrap();
        assert!(Command::new("git").arg("add").arg("a.rs").current_dir(root.path()).status().unwrap().success());
        let answer = query(root.path(), Query::Calls("a.rs".into())).unwrap();
        let edge_index = answer.edges.iter().position(|row| row.target == "target").unwrap();
        let mut plan = plan_rust_analyzer(root.path(), IMAGE,
            "unix:///run/user/1000/docker.sock", true).unwrap();
        let same_uid_socket = root.path().join("fake.sock");
        plan.args.push("--privileged".into());
        let error = observe_stream_definition(&same_uid_socket, &plan, &answer,
            edge_index, 0).unwrap_err();
        assert!(error.contains("plan changed"), "{error}");
        plan.args.pop();
        fs::write(root.path().join("third.rs"), "fn changed() {}\n").unwrap();
        let error = observe_stream_definition(&same_uid_socket, &plan, &answer,
            edge_index, 0).unwrap_err();
        assert!(error.contains("source inventory changed since syntax query"), "{error}");
        fs::write(root.path().join("third.rs"), "fn unrelated() {}\n").unwrap();
        fs::write(root.path().join("a.rs"), "fn caller() { target(); }\nfn target() {}\n// changed\n").unwrap();
        let error = observe_stream_definition(&same_uid_socket, &plan, &answer,
            edge_index, 0).unwrap_err();
        assert!(error.contains("source changed"), "{error}");
    }

    #[test]
    fn stream_source_basis_rejects_third_rust_file_drift_and_added_inputs() {
        let root = tempfile::tempdir().unwrap();
        assert!(Command::new("git").arg("init").arg("-q").arg(root.path()).status().unwrap().success());
        fs::write(root.path().join("a.rs"), "fn caller() { target(); }\n").unwrap();
        fs::write(root.path().join("b.rs"), "fn target() {}\n").unwrap();
        fs::write(root.path().join("third.rs"), "fn unrelated() {}\n").unwrap();
        let answer = query(root.path(), Query::Calls("a.rs".into())).unwrap();
        let expected = answer.scan_input_sha256.unwrap();
        let (seen, basis) = with_rust_scan_basis(root.path(), &expected,
            |digest| Ok(digest.to_owned())).unwrap();
        assert_eq!(seen, expected);
        assert_eq!(basis, expected);

        let error = with_rust_scan_basis(root.path(), &expected, |_| {
            fs::write(root.path().join("third.rs"), "fn changed() {}\n").unwrap();
            Ok(())
        }).unwrap_err();
        assert!(error.contains("during stream observation"), "{error}");
        let error = with_rust_scan_basis(root.path(), &expected, |_| Ok(())).unwrap_err();
        assert!(error.contains("since syntax query"), "{error}");

        fs::write(root.path().join("third.rs"), "fn unrelated() {}\n").unwrap();
        let error = with_rust_scan_basis(root.path(), &expected, |_| {
            fs::write(root.path().join("added.rs"), "fn new() {}\n").unwrap();
            Ok(())
        }).unwrap_err();
        assert!(error.contains("during stream observation"), "{error}");
    }

    #[test]
    fn stream_definition_rejects_incomplete_originating_answer_even_with_recomputed_digest() {
        let root = tempfile::tempdir().unwrap();
        assert!(Command::new("git").arg("init").arg("-q").arg(root.path()).status().unwrap().success());
        fs::write(root.path().join("a.rs"), "fn caller() { target(); }\n").unwrap();
        fs::write(root.path().join("b.rs"), "fn target() {}\n").unwrap();
        fs::write(root.path().join("third.rs"), "fn unrelated() {}\n").unwrap();
        let original = query(root.path(), Query::Calls("a.rs".into())).unwrap();
        assert!(original.scan_input_sha256.is_some());
        let plan = plan_rust_analyzer(root.path(), IMAGE,
            "unix:///run/user/1000/docker.sock", true).unwrap();
        let socket = root.path().join("fake.sock");
        assert!(complete_call_basis(&plan, &original, 100, 0).unwrap_err()
            .contains("not displayed"));
        let mut wrong_source = original.clone();
        wrong_source.value = "b.rs".into();
        assert!(complete_call_basis(&plan, &wrong_source, 0, 0).unwrap_err()
            .contains("differs from originating answer"));

        fs::write(root.path().join("third.rs"), "fn broken(\n").unwrap();
        let stale_error = observe_stream_definition(&socket, &plan, &original, 0, 0).unwrap_err();
        assert!(stale_error.contains("source inventory changed since syntax query"), "{stale_error}");

        let mut incomplete = query(root.path(), Query::Calls("a.rs".into())).unwrap();
        assert!(incomplete.scan_input_sha256.is_none());
        assert_eq!(incomplete.coverage.rust_unparseable_files, 1);
        assert!(!incomplete.edges.is_empty());
        let error = observe_stream_definition(&socket, &plan, &incomplete, 0, 0).unwrap_err();
        assert!(error.contains("originating complete Rust calls answer"), "{error}");

        // A caller cannot turn that incomplete answer into a complete one by
        // filling its public digest field from a later byte-only rehash.
        incomplete.scan_input_sha256 = Some(current_scan_input_sha256(root.path()).unwrap().0);
        let error = observe_stream_definition(&socket, &plan, &incomplete, 0, 0).unwrap_err();
        assert!(error.contains("originating complete Rust calls answer"), "{error}");

        let mut missing = original;
        missing.scan_input_sha256 = None;
        let error = observe_stream_definition(&socket, &plan, &missing, 0, 0).unwrap_err();
        assert!(error.contains("complete Rust scan input digest"), "{error}");
    }

    #[test]
    fn stream_query_digest_binds_whole_rust_scan_to_challenge() {
        let root = tempfile::tempdir().unwrap();
        assert!(Command::new("git").arg("init").arg("-q").arg(root.path()).status().unwrap().success());
        fs::write(root.path().join("a.rs"), "fn caller() { target(); }\nfn target() {}\n").unwrap();
        let answer = query(root.path(), Query::Calls("a.rs".into())).unwrap();
        let edge = answer.edges.first().unwrap();
        let candidate = edge.candidates.first().unwrap();
        let plan = plan_rust_analyzer(root.path(), IMAGE,
            "unix:///run/user/1000/docker.sock", true).unwrap();
        let basis = answer.scan_input_sha256.as_deref().unwrap();
        let digest = stream_query_digest(&plan, edge, candidate, basis).unwrap();
        assert_ne!(digest, stream_query_digest(&plan, edge, candidate, &"0".repeat(64)).unwrap());
        assert_ne!(digest, query_digest(&plan, edge, candidate).unwrap());
        assert_eq!(with_rust_scan_basis(root.path(), "", |_| Ok(())).unwrap_err(),
            "missing or invalid Rust scan input digest from syntax query");
    }

    /// Guest only: root owns this endpoint and sends a complete stream in
    /// the passing case. All cases remain transport observations, not proof
    /// that a rootless Engine or rust-analyzer produced the bytes.
    #[test]
    #[ignore]
    fn disposable_guest_stream_sender_continuity() {
        let mode = std::env::var("DOXA_STREAM_MODE").unwrap();
        let path = Path::new("/run/doxa-semantic/stream.sock");
        let result = observe_stream_packets(path, &"a".repeat(64), &"b".repeat(64),
            &"c".repeat(64), &"d".repeat(64), &"f".repeat(64));
        if mode == "root" {
            assert_eq!(result.unwrap(), json!({}));
            println!("DOXA_STREAM_RECEIPT mode=root ordered_root_sender=observed binding=unknown");
        } else {
            let error = result.unwrap_err();
            let expected = match mode.as_str() {
                "handoff" | "mid_handoff" => "sender is not UID/GID zero",
                "root_switch" => "sender changed",
                "cid_swap" => "CID changed",
                "extra" => "extra data",
                _ => panic!("unknown stream fixture mode"),
            };
            assert!(error.contains(expected), "{mode}: {error}");
            println!("DOXA_STREAM_RECEIPT mode={mode} rejected={error}");
        }
        assert!(fs::remove_file(path).is_err(), "guest client replaced the root socket");
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
