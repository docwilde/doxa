//! One narrow session-bound Codex compaction endpoint, outside the worker.
use super::{active, error, nonce, Manifest};
use serde_json::{json, Value};
use std::{fs, io::{self, Read, Write}, os::unix::{fs::{MetadataExt, PermissionsExt}, net::{UnixListener, UnixStream}, io::AsRawFd},
    path::{Component, Path, PathBuf}, sync::{Arc, atomic::{AtomicBool, Ordering}}, time::Duration};

const MAX_FRAME: usize = 65536;
const BLOCKED: &str = "DOXA rejected invalid or unreviewed session hook";

// Rootless container UID 0 maps to the owner of its Engine. This check
// rejects other host users, but does not prove which same-UID process owns a
// connection. A hardened broker needs a separate container-origin attestation.
#[cfg(target_os = "linux")]
fn require_owner_peer(stream: &UnixStream) -> io::Result<()> {
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    let result = unsafe { libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED,
        (&mut cred as *mut libc::ucred).cast(), &mut len) };
    if result != 0 || len as usize != std::mem::size_of::<libc::ucred>() {
        return Err(error("session hook peer credentials unavailable"));
    }
    if cred.pid <= 0 || cred.uid != unsafe { libc::geteuid() } {
        return Err(error("session hook peer is not the rootless Engine owner"));
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
fn require_owner_peer(_: &UnixStream) -> io::Result<()> {
    Err(error("session hook peer attestation requires Linux"))
}

fn checked_event(frame: Value, capability: &str, manifest: &Manifest) -> io::Result<Value> {
    let object = frame.as_object().ok_or_else(|| error("invalid session broker frame"))?;
    if object.len() != 3 || frame["version"] != 1 || frame["capability"] != capability
        || !frame["event"].is_object() {
        return Err(error("invalid session broker capability/schema"));
    }
    let mut event = frame["event"].clone();
    if event["hook_event_name"] != "PreCompact"
        || !matches!(event["trigger"].as_str(), Some("auto" | "manual"))
        || event["session_id"].as_str().is_none_or(|id| id.is_empty() || id.len() > 256) {
        return Err(error("invalid session broker compaction event"));
    }
    let source = event["transcript_path"].as_str()
        .ok_or_else(|| error("missing worker transcript path"))?;
    let relative = Path::new(source).strip_prefix("/home/doxa/codex")
        .map_err(|_| error("worker transcript path outside private Codex home"))?;
    let mut components = relative.components();
    if !matches!(components.next(), Some(Component::Normal(name)) if name == "sessions" || name == "archived_sessions")
        || components.next().is_none()
        || relative.components().any(|part| !matches!(part, Component::Normal(_))) {
        return Err(error("invalid broker transcript path"));
    }
    event["transcript_path"] = json!(manifest.private_home.join("codex").join(relative));
    Ok(event)
}

fn process_client(mut stream: UnixStream, capability: &str, manifest: &Manifest,
    handler: &impl Fn(Value, &Manifest) -> Value) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(5)));
    let result = (|| -> io::Result<Value> {
        require_owner_peer(&stream)?;
        let mut size = [0; 4]; stream.read_exact(&mut size)?;
        let size = u32::from_be_bytes(size) as usize;
        if size > MAX_FRAME { return Err(error("broker frame exceeds bound")); }
        let mut bytes = vec![0; size]; stream.read_exact(&mut bytes)?;
        let frame: Value = serde_json::from_slice(&bytes)?;
        Ok(handler(checked_event(frame, capability, manifest)?, manifest))
    })().unwrap_or_else(|_| json!({"continue":false,"suppressOutput":true,"stopReason":BLOCKED}));
    let mut bytes = serde_json::to_vec(&result).unwrap_or_default();
    if bytes.len() > MAX_FRAME {
        bytes = serde_json::to_vec(&json!({"continue":false,"suppressOutput":true,"stopReason":BLOCKED})).unwrap();
    }
    let _ = stream.write_all(&(bytes.len() as u32).to_be_bytes())
        .and_then(|_| stream.write_all(&bytes));
}

pub struct HookBroker { socket: PathBuf, identity: (u64,u64), stopping: Arc<AtomicBool>, worker: Option<std::thread::JoinHandle<()>>, command: String }
impl HookBroker {
    pub fn start(handler: impl Fn(Value, &Manifest) -> Value + Send + 'static) -> io::Result<Option<Self>> {
        let Some(manifest) = active()? else { return Ok(None); };
        let socket = manifest.broker.join("hook.sock");
        // A previous host may have crashed. Only a socket in our already
        // owner-checked private directory may be replaced, never a symlink.
        if let Ok(meta) = fs::symlink_metadata(&socket) {
            use std::os::unix::fs::FileTypeExt;
            if !meta.file_type().is_socket() || meta.uid() != unsafe { libc::geteuid() } { return Err(error("unsafe stale isolation broker")); }
            if std::os::unix::net::UnixStream::connect(&socket).is_ok() { return Err(error("session hook broker is already live")); }
            fs::remove_file(&socket)?;
        }
        let listener = UnixListener::bind(&socket)?; fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
        listener.set_nonblocking(true)?;
        let meta = fs::metadata(&socket)?; let identity = (meta.dev(),meta.ino());
        let capability = nonce()?;
        let command = format!("'/usr/local/bin/doxa-isolation-worker' 'hook' '/run/doxa/session/hook.sock' '{capability}'");
        let stopping = Arc::new(AtomicBool::new(false)); let stop = stopping.clone();
        let worker = std::thread::spawn(move || {
            while !stop.load(Ordering::Acquire) {
                let (stream, _) = match listener.accept() { Ok(connection) => connection, Err(e) if e.kind() == io::ErrorKind::WouldBlock => { std::thread::sleep(Duration::from_millis(20)); continue; }, Err(_) => break };
                process_client(stream, &capability, &manifest, &handler);
            }
        });
        Ok(Some(Self { socket, identity, stopping, worker:Some(worker), command }))
    }
    pub fn command(&self) -> &str { &self.command }
}
impl Drop for HookBroker {
    fn drop(&mut self) {
        self.stopping.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() { let _ = worker.join(); }
        if fs::symlink_metadata(&self.socket).is_ok_and(|m| (m.dev(),m.ino()) == self.identity) { let _ = fs::remove_file(&self.socket); }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Profile;
    use std::{os::unix::net::UnixStream, sync::atomic::AtomicUsize};

    fn manifest(root: &Path) -> Manifest {
        Manifest { version: 1, session_id: "fixture".into(), profile: Profile::DockerOffline,
            policy: None, policy_hash: String::new(), creation_policy_hash: String::new(),
            source: root.to_owned(), checkout: root.join("checkout"), context_cwd: None,
            provider_rollout: None, checkout_device: 0, checkout_inode: 0,
            base_sha: String::new(), branch: String::new(), private_home: root.join("home"),
            cache: root.join("cache"), broker: root.join("broker"), container_id: None,
            nonce: String::new(), state: "ready".into() }
    }
    fn frame() -> Value {
        json!({"version":1,"capability":"fixture-capability","event":{
            "hook_event_name":"PreCompact","trigger":"manual","session_id":"provider-thread",
            "transcript_path":"/home/doxa/codex/sessions/rollout.jsonl"}})
    }
    fn roundtrip(frame: &Value, result: Value) -> (Value, usize) {
        let root = tempfile::tempdir().unwrap();
        let listener = UnixListener::bind(root.path().join("hook.sock")).unwrap();
        let mut client = UnixStream::connect(root.path().join("hook.sock")).unwrap();
        let (server, _) = listener.accept().unwrap();
        let bytes = serde_json::to_vec(frame).unwrap();
        client.write_all(&(bytes.len() as u32).to_be_bytes()).unwrap();
        client.write_all(&bytes).unwrap();
        let calls = AtomicUsize::new(0);
        process_client(server, "fixture-capability", &manifest(root.path()), &|event, session| {
            calls.fetch_add(1, Ordering::SeqCst);
            assert_eq!(event["transcript_path"], json!(session.private_home.join("codex/sessions/rollout.jsonl")));
            result.clone()
        });
        let mut size = [0; 4]; client.read_exact(&mut size).unwrap();
        let mut answer = vec![0; u32::from_be_bytes(size) as usize];
        client.read_exact(&mut answer).unwrap();
        (serde_json::from_slice(&answer).unwrap(), calls.load(Ordering::SeqCst))
    }
    #[cfg(target_os = "linux")]
    #[test]
    fn local_socket_proves_owner_uid_and_bounded_protocol_but_not_container_origin() {
        let (answer, calls) = roundtrip(&frame(), json!({"continue":true}));
        assert_eq!(answer["continue"], true);
        assert_eq!(calls, 1);
        // This client is a same-UID host process. Rootless UID and a bearer
        // capability cannot distinguish it from the worker if that capability
        // is exposed. Hardened admission therefore remains unavailable.
    }
    #[test]
    fn malformed_capability_event_or_path_never_reaches_review_handler() {
        let mut invalid = Vec::new();
        let mut value = frame(); value["capability"] = json!("wrong"); invalid.push(value);
        let mut value = frame(); value["extra"] = json!(true); invalid.push(value);
        let mut value = frame(); value["event"]["hook_event_name"] = json!("Other"); invalid.push(value);
        let mut value = frame(); value["event"]["trigger"] = json!("other"); invalid.push(value);
        let mut value = frame(); value["event"]["transcript_path"] = json!("/home/doxa/codex/sessions/../escape"); invalid.push(value);
        let mut value = frame(); value["event"]["transcript_path"] = json!("/workspace/rollout.jsonl"); invalid.push(value);
        for value in invalid {
            let (answer, calls) = roundtrip(&value, json!({"continue":true}));
            assert_eq!(answer["continue"], false, "invalid broker frame was admitted: {value}");
            assert_eq!(calls, 0);
        }
    }
    #[cfg(target_os = "linux")]
    #[test]
    fn oversized_handler_result_is_replaced_with_bounded_refusal() {
        let (answer, calls) = roundtrip(&frame(), json!({"blob":"x".repeat(MAX_FRAME)}));
        assert_eq!(calls, 1);
        assert_eq!(answer["continue"], false);
    }
}
