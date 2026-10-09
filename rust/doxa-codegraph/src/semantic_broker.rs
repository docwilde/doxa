//! Disabled, identity-only handshake for a future host-managed semantic broker.
//!
//! A same-UID fake Engine cannot satisfy the root-owned path and kernel peer
//! credential requirements. This does not attest a producer or return a
//! semantic binding: the broker implementation and live containment proof do
//! not exist yet. Nothing in the CLI calls this module.

use super::semantic_producer::ProducerPlan;
use super::semantic_runtime::bounded_unix_connect;
use super::{CallCandidate, CallEdge};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::os::unix::net::UnixStream;
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, Instant};

const PROTOCOL: &str = "doxa-semantic-broker-identity-v1";
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

/// Every component of the socket path must be owned by host root. Writable
/// parents, symlinks and a rootless user's private socket are rejected.
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
            return Err("broker socket path is not host-root-owned and unsymlinked".into());
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

fn root_peer(stream: &UnixStream) -> Result<(), String> {
    let mut peer: libc::ucred = unsafe { std::mem::zeroed() };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe { libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED,
        &mut peer as *mut _ as *mut _, &mut length) } < 0
        || length as usize != std::mem::size_of::<libc::ucred>()
        || peer.pid <= 0 || peer.uid != 0 {
        return Err("broker peer is not host root".into());
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
        || reply.get("status").and_then(Value::as_str) != Some("identity_only") {
        return Err("broker reply does not match identity challenge".into());
    }
    Ok(())
}

/// Authenticate only the host-managed endpoint and echo of one exact query.
/// This private seam never starts an analyzer, accepts producer claims, or
/// changes `binding: unknown`.
#[allow(dead_code)]
pub(crate) fn identity_probe(path: &Path, plan: &ProducerPlan,
    edge: &CallEdge, candidate: &CallCandidate) -> Result<Value, String> {
    if unsafe { libc::geteuid() } == 0 {
        return Err("broker client must run unprivileged".into());
    }
    let identity = root_owned_socket(path)?;
    let deadline = Instant::now() + DEADLINE;
    let mut stream = bounded_unix_connect(path, deadline)?;
    root_peer(&stream)?;
    if root_owned_socket(path)? != identity { return Err("broker socket changed during connect".into()); }
    stream.set_nonblocking(true).map_err(|_| "cannot configure broker socket")?;
    let nonce = challenge_nonce()?;
    let digest = query_digest(plan, edge, candidate)?;
    let request = json!({"protocol":PROTOCOL,"nonce":nonce,"query_sha256":digest,
        "operation":"identity_only"});
    let body = serde_json::to_vec(&request).map_err(|_| "cannot encode broker challenge")?;
    if body.len() > MAX_WIRE_BYTES { return Err("broker challenge exceeds bound".into()); }
    let mut frame = (body.len() as u32).to_be_bytes().to_vec();
    frame.extend(body);
    io_until(&mut stream, &mut frame, true, deadline)?;
    let reply = read_reply(&mut stream, deadline)?;
    match_reply(&reply, &nonce, &digest)?;
    if root_owned_socket(path)? != identity { return Err("broker socket changed during exchange".into()); }
    Ok(json!({"status":"unknown","binding":"unknown",
        "broker_identity":"host_root_peer_observed","producer":"not_started"}))
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
        let error = identity_probe(&socket, &plan, edge, candidate).unwrap_err();
        assert!(error.contains("host-root-owned"), "{error}");
    }

    #[test]
    fn reply_requires_exact_nonce_query_and_identity_only_status() {
        let nonce = "a".repeat(64);
        let digest = "b".repeat(64);
        let valid = json!({"protocol":PROTOCOL,"nonce":nonce,
            "query_sha256":digest,"status":"identity_only"});
        assert!(match_reply(&valid, &nonce, &digest).is_ok());
        for key in ["protocol", "nonce", "query_sha256", "status"] {
            let mut invalid = valid.clone();
            invalid[key] = json!("forged");
            assert!(match_reply(&invalid, &nonce, &digest).is_err(), "{key}");
        }
        let mut extra = valid.clone();
        extra["binding"] = json!("verified");
        assert!(match_reply(&extra, &nonce, &digest).is_err());
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

    /// Run only inside a disposable guest with a root-owned broker fixture.
    /// This proves the OS identity boundary, not an analyzer or containment.
    #[test]
    #[ignore]
    fn disposable_guest_root_peer_identity_proof() {
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
        let status = identity_probe(path, &plan, &edge, &candidate).unwrap();
        assert_eq!(status["broker_identity"], "host_root_peer_observed");
        assert_eq!(status["producer"], "not_started");
        assert_eq!(status["binding"], "unknown");
        assert!(fs::remove_file(path).is_err(), "guest client replaced the root socket");
    }
}
