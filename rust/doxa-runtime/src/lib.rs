//! Native DOXA protocol v1 socket foundation. The engine is supplied by `Host`.
//! No Python engine, registry, persistence, or LORE integration is implied here.

use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::fs;
use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::mpsc::{self, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

pub const MAX_FRAME_BYTES: usize = 64 * 1024;
const RING_CAPACITY: usize = 512;
const CLIENT_QUEUE_CAPACITY: usize = 1024;
const PROMPT_QUEUE_CAPACITY: usize = 8;
const QUEUE_PREVIEW_CHARS: usize = 120;
const MAX_CONNECTIONS: usize = 64;

/// The engine seam. `prompt` may emit zero or more protocol event objects.
/// Each event is wrapped in a sequence-numbered `event` frame by the daemon.
/// If the host returns without `turn_done` or `turn_refused`, the daemon emits
/// one `turn_done`; it also emits an error terminal if the host panics.
/// Methods are called from worker threads and must be safe for concurrent calls.
/// Successful `set_model`, `set_effort`, and `set_permission_mode` calls must
/// return an object containing the selected `model`, `effort`, or `mode`.
pub type PeerToolHandler = Arc<dyn Fn(&str, &Value) -> Result<Value, String> + Send + Sync>;

pub trait Host: Send + Sync + 'static {
    fn isolation_status(&self) -> Option<Value> { None }
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value));
    fn call(&self, method: &str, params: &Value) -> Result<Value, String>;
    fn initial_model(&self) -> Option<String> { None }
    /// Initial effort asserted for this session; None is unknown.
    fn initial_effort(&self) -> Option<String> { None }
    fn initial_permission_mode(&self) -> String { "default".to_owned() }
    /// Nonblocking provider work ownership, including work left after an early
    /// prompt error. A true result prevents automatic idle expiration.
    fn has_active_work(&self) -> bool { false }
    fn can_set_model(&self) -> bool { false }
    fn model_change_requires_idle(&self) -> bool { false }
    fn can_set_permission_mode(&self) -> bool { false }
    fn permission_change_requires_idle(&self) -> bool { false }
    /// Called once before prompt admission. Returns true only when the host
    /// can expose these bounded, same-scope tools to its actual provider.
    fn set_peer_tool_handler(&self, _: PeerToolHandler) -> bool { false }
    /// Separate host-owned session operators; independent of peer-send opt-in.
    fn set_session_tool_handler(&self, _: PeerToolHandler) -> bool { false }
    fn peer_tools_ready(&self) -> bool { false }
    /// Provider-verified billing snapshot; None means unknown.
    fn billing_snapshot(&self) -> Option<Value> { None }
    /// Effective session memory policy, distinct from required secret scrubbing.
    fn lore_enabled(&self) -> Option<bool> { None }
    /// Display metadata asserted by this connected provider; never credentials.
    fn account_snapshot(&self) -> Option<Value> { None }
    /// Cached canonical LORE counts/containment state; this must never block on a tool.
    fn lore_status(&self) -> Option<Value> { None }
    /// Only the scrub preflight and sticky runtime scrub failure are known.
    /// This does not claim that memory indexing or snapshotting succeeded.
    fn lore_scrub_status(&self) -> Option<&'static str> { None }
    /// Text included in queue events. A real host can scrub prompts before
    /// they are sent to other attached clients; internal execution keeps the
    /// original prompt. An error rejects new prompts before queueing.
    fn public_prompt(&self, text: &str) -> Result<String, String> { Ok(text.to_owned()) }
    /// Owner-checked JSONL boundary for durable client restore.
    fn transcript_snapshot(&self) -> io::Result<Option<(PathBuf, u64)>> { Ok(None) }
}

#[derive(Clone)]
pub struct Session {
    pub session_id: String,
    pub cwd: String,
    pub model: Option<String>,
    pub engine: String,
    pub doxa_version: String,
}

struct Prompt { text: String, public_text: String, queue_id: String, peer_origin: Option<String> }

fn queue_preview(text: &str) -> String { text.chars().take(QUEUE_PREVIEW_CHARS).collect() }

#[derive(Debug, PartialEq, Eq)]
pub enum ExternalPrompt {
    Started,
    Queued,
    Full,
}
struct State {
    pending_inputs: Vec<Value>,
    pending_inputs_complete: bool,
    next_seq: u64,
    ring: VecDeque<(u64, Vec<u8>)>,
    clients: HashMap<u64, SyncSender<Vec<u8>>>,
    remote_clients: HashMap<u64, String>,
    busy: bool,
    prompts: VecDeque<Prompt>,
    next_queue_id: u64,
    next_turn_id: u64,
    model: Option<String>,
    permission_mode: String,
    effort: Option<String>,
    pending_effort: Option<String>,
}

struct Inner {
    state: Mutex<State>,
    controls: Mutex<()>,
    host: Arc<dyn Host>,
    session: Session,
    stopping: AtomicBool,
    next_client_id: AtomicU64,
    active_connections: AtomicUsize,
}

pub struct Daemon {
    inner: Arc<Inner>,
    listener: UnixListener,
    socket_path: PathBuf,
    socket_ino: u64,
}

pub struct DaemonHandle {
    inner: Arc<Inner>,
    socket_path: PathBuf,
    socket_ino: u64,
    accept_thread: Option<JoinHandle<()>>,
}

impl Daemon {
    /// Bind inside an owner-private runtime directory. Existing socket paths
    /// are never removed or replaced, even if a previous process left one.
    pub fn bind(runtime_dir: impl AsRef<Path>, session: Session, host: Arc<dyn Host>) -> io::Result<Self> {
        // Keep this identical to doxa.identity._SESSION_ID_RE:
        // [0-9A-Za-z][0-9A-Za-z-]{0,127}.
        let id = session.session_id.as_bytes();
        if id.is_empty() || id.len() > 128 ||
            !id[0].is_ascii_alphanumeric() ||
            !id[1..].iter().all(|b| b.is_ascii_alphanumeric() || *b == b'-') ||
            session.cwd.is_empty() || session.engine.is_empty() {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid session metadata"));
        }
        let model = host.initial_model().or_else(|| session.model.clone());
        let permission_mode = host.initial_permission_mode();
        let effort = host.initial_effort();
        let hello = json!({"type":"hello","proto":1,"doxa":session.doxa_version,
            "session_id":session.session_id,"model":model,"engine":session.engine,
            "permission_mode":permission_mode,"bypass_armed":false,
            "cwd":session.cwd,"next_seq":0,"billing":host.billing_snapshot(),"account":host.account_snapshot()});
        if serde_json::to_vec(&hello).map_err(io::Error::other)?.len() + 1 > MAX_FRAME_BYTES {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "hello frame too large"));
        }
        let dir = runtime_dir.as_ref();
        if !dir.exists() { fs::create_dir_all(dir)?; }
        let meta = fs::symlink_metadata(dir)?;
        if !meta.is_dir() || meta.file_type().is_symlink() || meta.uid() != unsafe { libc::geteuid() } {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "runtime directory must be a real, owned directory"));
        }
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700))?;
        let name = format!("daemon-{}-{}.sock", &session.session_id[..session.session_id.len().min(8)], std::process::id());
        let socket_path = dir.join(name);
        if fs::symlink_metadata(&socket_path).is_ok() {
            return Err(io::Error::new(io::ErrorKind::AlreadyExists, "socket path already exists"));
        }
        let listener = UnixListener::bind(&socket_path)?;
        fs::set_permissions(&socket_path, fs::Permissions::from_mode(0o600))?;
        let socket_ino = fs::symlink_metadata(&socket_path)?.ino();
        listener.set_nonblocking(true)?;
        Ok(Self {
            inner: Arc::new(Inner { state: Mutex::new(State {
                pending_inputs: Vec::new(), pending_inputs_complete: true, next_seq: 0, ring: VecDeque::new(), clients: HashMap::new(), remote_clients: HashMap::new(), busy: false,
                prompts: VecDeque::new(), next_queue_id: 1, next_turn_id: 1,
                model, permission_mode, effort, pending_effort: None,
            }), controls: Mutex::new(()), host, session, stopping: AtomicBool::new(false), next_client_id: AtomicU64::new(1),
                active_connections: AtomicUsize::new(0) }),
            listener, socket_path, socket_ino,
        })
    }

    pub fn start(self) -> DaemonHandle {
        let inner = self.inner.clone();
        let socket_path = self.socket_path.clone();
        let socket_ino = self.socket_ino;
        let accept_thread = thread::spawn(move || {
            while !inner.stopping.load(Ordering::Acquire) {
                match self.listener.accept() {
                    Ok((stream, _)) => {
                        // Darwin inherits O_NONBLOCK from the listener; client
                        // read/write timeouts require a blocking stream.
                        if stream.set_nonblocking(false).is_err() { continue; }
                        if inner.active_connections.fetch_update(Ordering::AcqRel, Ordering::Acquire,
                            |count| (count < MAX_CONNECTIONS).then_some(count + 1)).is_err() {
                            continue;
                        }
                        let client_inner = inner.clone();
                        thread::spawn(move || handle_client(client_inner, stream));
                    }
                    Err(err) if err.kind() == io::ErrorKind::WouldBlock => thread::sleep(Duration::from_millis(10)),
                    Err(_) => break,
                }
            }
            drop(self.listener);
            remove_owned_socket(&socket_path, socket_ino);
        });
        DaemonHandle { inner: self.inner, socket_path: self.socket_path, socket_ino: self.socket_ino, accept_thread: Some(accept_thread) }
    }
}

impl DaemonHandle {
    pub fn socket_path(&self) -> &Path { &self.socket_path }

    /// Whether a socket `stop` call has requested shutdown.
    pub fn is_stopping(&self) -> bool { self.inner.stopping.load(Ordering::Acquire) }

    /// Number of clients that have completed the protocol attach handshake.
    pub fn attached_clients(&self) -> usize { self.inner.state.lock().unwrap().clients.len() }

    /// Both the runtime queue and provider must be idle before the linger clock
    /// can run. Hosts report conservatively if their work state is unavailable.
    pub fn has_active_work(&self) -> bool {
        let Ok(_admission) = self.inner.controls.try_lock() else { return true; };
        let provider_active = self.inner.host.has_active_work();
        let state = self.inner.state.lock().unwrap();
        provider_active || state.busy || !state.prompts.is_empty()
    }

    /// Claim automatic shutdown atomically with attach/prompt admission. The
    /// caller must have checked its linger deadline; false rearms that clock.
    pub fn expire_if_detached_idle(&self) -> bool {
        let Ok(_admission) = self.inner.controls.try_lock() else { return false; };
        let provider_active = self.inner.host.has_active_work();
        let state = self.inner.state.lock().unwrap();
        if provider_active || state.busy || !state.prompts.is_empty() || !state.clients.is_empty()
            || self.inner.stopping.load(Ordering::Acquire) { return false; }
        self.inner.stopping.store(true, Ordering::Release);
        true
    }

    /// Publish an out-of-band event (`turn: null`) to the ring and clients.
    pub fn publish(&self, event: Value) { self.inner.publish(None, event); }

    /// Admit a validated peer prompt through the same bounded FIFO as typed
    /// prompts. The caller retains the original frame on `Full` or error.
    pub fn enqueue_peer_prompt(&self, text: String, origin: &str) -> Result<ExternalPrompt, String> {
        if text.trim().is_empty() { return Err("empty peer prompt".into()); }
        let display = std::panic::catch_unwind(std::panic::AssertUnwindSafe(||
            self.inner.host.public_prompt(&text)
        )).map_err(|_| "peer prompt could not be scrubbed")?
            .map_err(|_| "peer prompt could not be scrubbed")?;
        let _control_guard = self.inner.controls.lock().unwrap();
        let mut state = self.inner.state.lock().unwrap();
        if self.inner.stopping.load(Ordering::Acquire) { return Err("daemon is stopping".into()); }
        if state.busy {
            if state.prompts.len() >= PROMPT_QUEUE_CAPACITY { return Ok(ExternalPrompt::Full); }
            let queue_id = format!("q{}", state.next_queue_id);
            state.next_queue_id += 1;
            let position = state.prompts.len() + 1;
            state.prompts.push_back(Prompt { text, public_text: queue_preview(&display), queue_id: queue_id.clone(), peer_origin: Some(origin.to_owned()) });
            drop(state);
            self.inner.publish(None, json!({"type":"prompt_queued","data":{
                "id":queue_id,"text":display,"position":position,
                "peer_started":true,"peer_origin":origin}}));
            Ok(ExternalPrompt::Queued)
        } else {
            let turn = format!("peer-r{:011}", state.next_turn_id);
            state.busy = true;
            state.next_turn_id += 1;
            drop(state);
            self.inner.start_turn(text, turn);
            Ok(ExternalPrompt::Started)
        }
    }

    pub fn shutdown(&mut self) {
        self.inner.stopping.store(true, Ordering::Release);
        self.inner.state.lock().unwrap().clients.clear();
        if let Some(thread) = self.accept_thread.take() { let _ = thread.join(); }
        remove_owned_socket(&self.socket_path, self.socket_ino);
    }
}

impl Drop for DaemonHandle { fn drop(&mut self) { self.shutdown(); } }

fn remove_owned_socket(path: &Path, inode: u64) {
    // Never unlink a different file substituted at this pathname.
    if let Ok(meta) = fs::symlink_metadata(path) {
        if meta.file_type().is_socket() && meta.ino() == inode {
            let _ = fs::remove_file(path);
        }
    }
}

impl Inner {
    fn publish(&self, turn: Option<&str>, event: Value) {
        let mut state = self.state.lock().unwrap();
        if event["type"] == "model_changed" {
            if let Some(model) = event["data"]["model"].as_str().filter(|s| !s.is_empty() && !s.chars().any(char::is_control)) { state.model = Some(model.to_owned()); }
        }
        if event["type"] == "effort_verified" {
            if let Some(effort) = event["data"]["effort"].as_str().filter(|s| !s.is_empty() && !s.chars().any(char::is_control)) { state.effort = Some(effort.to_owned()); state.pending_effort = None; }
        }
        if event["type"] == "effort_verification_failed" { state.pending_effort = None; }
        match event["type"].as_str() {
            Some("needs_input") => {
                let data = &event["data"];
                if let Some(id) = data["id"].as_str() {
                    if state.pending_inputs.iter().any(|item| item["id"].as_str() == Some(id) && item != data) {
                        // A provider reused an answer ID for changed content.
                        // No client may approve the earlier snapshot.
                        state.pending_inputs_complete = false;
                    }
                    state.pending_inputs.retain(|item| item["id"].as_str() != Some(id));
                    let bytes = state.pending_inputs.iter().map(|item| item.to_string().len()).sum::<usize>();
                    if state.pending_inputs.len() < 8 && bytes.saturating_add(data.to_string().len()) <= 48 * 1024 {
                        state.pending_inputs.push(data.clone());
                    } else { state.pending_inputs_complete = false; }
                } else { state.pending_inputs_complete = false; }
            }
            Some("needs_input_resolved") => {
                let id = event["data"]["id"].as_str();
                state.pending_inputs.retain(|item| item["id"].as_str() != id);
            }
            Some("turn_done" | "turn_refused") => {
                state.pending_inputs.clear(); state.pending_inputs_complete = true;
            }
            _ => {},
        }
        let seq = state.next_seq;
        let Some(next) = seq.checked_add(1) else { return; };
        state.next_seq = next;
        let frame = json!({"type":"event", "seq":seq, "turn":turn, "event":event});
        let bytes = encode_event(&frame);
        state.ring.push_back((seq, bytes.clone()));
        if state.ring.len() > RING_CAPACITY { state.ring.pop_front(); }
        state.clients.retain(|_, tx| tx.try_send(bytes.clone()).is_ok());
    }

    fn start_turn(self: &Arc<Self>, text: String, turn: String) {
        let inner = self.clone();
        thread::spawn(move || {
            let mut terminal_emitted = false;
            let mut emit = |event: Value| {
                if terminal_emitted { return; }
                terminal_emitted = matches!(event["type"].as_str(), Some("turn_done" | "turn_refused"));
                inner.publish(Some(&turn), event);
            };
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| inner.host.prompt(&text, &mut emit)));
            if result.is_err() && !terminal_emitted {
                inner.publish(Some(&turn), json!({"type":"turn_done", "data":{"is_error":true,"error":"host panicked"}}));
            } else if !terminal_emitted {
                inner.publish(Some(&turn), json!({"type":"turn_done", "data":{}}));
            }
            let next = {
                let _admission = inner.controls.lock().unwrap();
                let mut state = inner.state.lock().unwrap();
                if inner.stopping.load(Ordering::Acquire) {
                    state.prompts.clear(); state.busy = false; None
                } else if let Some(prompt) = state.prompts.pop_front() {
                    let turn = format!("{}r{:011}", if prompt.peer_origin.is_some() { "peer-" } else { "" }, state.next_turn_id);
                    state.next_turn_id += 1;
                    Some((prompt, turn))
                } else { state.busy = false; None }
            };
            if let Some((prompt, turn)) = next {
                let display = std::panic::catch_unwind(std::panic::AssertUnwindSafe(||
                    inner.host.public_prompt(&prompt.text)
                )).ok().and_then(Result::ok)
                    .unwrap_or_else(|| "[redacted: prompt unavailable]".to_owned());
                inner.publish(None, json!({"type":"prompt_dequeued","data":{"id":prompt.queue_id,"text":display,
                    "peer_started":prompt.peer_origin.is_some(),"peer_origin":prompt.peer_origin}}));
                inner.start_turn(prompt.text, turn);
            }
        });
    }
}

fn handle_client(inner: Arc<Inner>, stream: UnixStream) {
    struct ConnectionGuard(Arc<Inner>);
    impl Drop for ConnectionGuard {
        fn drop(&mut self) { self.0.active_connections.fetch_sub(1, Ordering::AcqRel); }
    }
    let _guard = ConnectionGuard(inner.clone());
    let id = inner.next_client_id.fetch_add(1, Ordering::Relaxed);
    let (tx, rx) = mpsc::sync_channel::<Vec<u8>>(CLIENT_QUEUE_CAPACITY);
    let mut writer = match stream.try_clone() { Ok(s) => s, Err(_) => return };
    // Capture the replay cursor before reading the transcript boundary.
    // A provider can publish while the file is being inspected; using the
    // later hello next_seq as the history cursor would omit those events.
    let transcript_seq = inner.state.lock().unwrap().next_seq;
    let transcript = match inner.host.transcript_snapshot() {
        Ok(value) => value,
        Err(_) => return,
    };
    let can_set_model = inner.host.can_set_model();
    let can_set_permission_mode = inner.host.can_set_permission_mode();
    let peer_tools_ready = inner.host.peer_tools_ready();
    let lore_scrub = inner.host.lore_scrub_status();
    let billing = inner.host.billing_snapshot();
    let lore_status = inner.host.lore_status();
    let hello = {
        let state = inner.state.lock().unwrap();
        json!({"type":"hello", "proto":1, "doxa":inner.session.doxa_version,
            "session_id":inner.session.session_id, "model":state.model,
            "permission_mode":state.permission_mode, "bypass_armed":false,
            "engine":inner.session.engine, "effort":state.effort,"pending_effort":state.pending_effort, "cwd":inner.session.cwd, "next_seq":state.next_seq,
            "transcript_path":transcript.as_ref().map(|(path, _)| path.to_string_lossy().into_owned()),
            "transcript_bytes":transcript.as_ref().map(|(_, size)| *size),"transcript_seq":transcript_seq,
            "running":state.busy,"queued":state.prompts.len(),"remote_driver":remote_identity(&state),
            "pending_inputs":state.pending_inputs,"pending_inputs_complete":state.pending_inputs_complete,
            "can_set_model":can_set_model,
            "can_set_permission_mode":can_set_permission_mode,"peer_tools_ready":peer_tools_ready,"isolation":inner.host.isolation_status(),
            "belief_count":lore_status.as_ref().and_then(|value|value["belief_count"].as_u64()),
            "disabled_tools":lore_status.as_ref().and_then(|value|value["disabled_tools"].as_array().cloned()),
            "lore_enabled":inner.host.lore_enabled(),"lore_scrub":lore_scrub,"billing":billing,"account":inner.host.account_snapshot()})
    };
    if writer.set_write_timeout(Some(Duration::from_secs(2))).is_err() ||
        writer.write_all(&encode_reply(&hello)).is_err() { return; }
    let writer_thread = thread::spawn(move || {
        while let Ok(bytes) = rx.recv() {
            if writer.write_all(&bytes).is_err() { break; }
        }
        let _ = writer.shutdown(std::net::Shutdown::Both);
    });
    let _ = stream.set_read_timeout(Some(Duration::from_millis(200)));
    let mut reader = BufReader::new(stream);
    let mut line = Vec::new();
    while !inner.stopping.load(Ordering::Acquire) {
        match read_bounded(&mut reader, &mut line) {
            Ok(0) => break,
            Ok(_) => {},
            Err(err) if err.kind() == io::ErrorKind::TimedOut || err.kind() == io::ErrorKind::WouldBlock => continue,
            Err(_) => break,
        }
        let parsed = serde_json::from_slice::<Value>(&line);
        line.clear();
        let Ok(frame) = parsed else { continue };
        if !frame.is_object() { continue; }
        match frame["type"].as_str() {
            Some("attach") => {
                let cursor = if frame["cursor"].is_null() { None } else { frame["cursor"].as_u64() };
                if !frame["cursor"].is_null() && cursor.is_none() { break; }
                // This advisory identity comes from the same-user bridge.
                // It never authorizes a call or replaces remote_policy's gates.
                let remote_login = frame.get("remote_login").and_then(Value::as_str)
                    .map(str::trim).filter(|login| !login.is_empty() && login.len() <= 256
                        && !login.chars().any(char::is_control));
                if frame.get("remote_login").is_some_and(|login| !login.is_null()) && remote_login.is_none() { break; }
                if !attach_client_with_identity(&inner, id, cursor, &tx, remote_login) { break; }
            }
            Some("prompt") | Some("call") if !inner.state.lock().unwrap().clients.contains_key(&id) => {
                send(&tx, json!({"type":"reply","id":frame["id"],"ok":false,"error":"attach required"}));
            }
            Some("prompt") => handle_prompt(&inner, &tx, &frame),
            Some("call") => handle_call(&inner, &tx, &frame),
            _ => {}
        }
    }
    detach_client(&inner, id);
    drop(tx);
    let _ = writer_thread.join();
}

fn remote_identity(state: &State) -> Option<String> {
    let mut identities: Vec<_> = state.remote_clients.iter().filter(|(id, _)| state.clients.contains_key(id))
        .map(|(_, login)| login.as_str()).collect();
    identities.sort(); identities.dedup();
    if identities.is_empty() { return None; }
    let label = identities.iter().take(3).copied().collect::<Vec<_>>().join(", ");
    Some(if identities.len() > 3 { format!("{label} (+{})", identities.len() - 3) } else { label })
}
fn detach_client(inner: &Inner, id: u64) {
    let mut state = inner.state.lock().unwrap();
    state.clients.remove(&id);
    let changed = state.remote_clients.remove(&id).is_some();
    let identity = remote_identity(&state);
    drop(state);
    if changed { inner.publish(None, json!({"type":"remote_driver_changed","data":{"identity":identity}})); }
}
#[cfg(test)]
fn attach_client(inner: &Inner, id: u64, cursor: Option<u64>, tx: &SyncSender<Vec<u8>>) -> bool {
    attach_client_with_identity(inner, id, cursor, tx, None)
}
fn attach_client_with_identity(inner: &Inner, id: u64, cursor: Option<u64>, tx: &SyncSender<Vec<u8>>, login: Option<&str>) -> bool {
    let _admission = inner.controls.lock().unwrap();
    let mut state = inner.state.lock().unwrap();
    if inner.stopping.load(Ordering::Acquire) { return false; }
    // Replay and registration are one atomic operation with publish.
    if let (Some(requested), Some((oldest, _))) = (cursor, state.ring.front()) {
        if requested < *oldest {
            // The gap advances the cursor before retained replay starts.
            let gap = json!({"type":"event","seq":oldest - 1,"turn":null,
                "event":{"type":"replay_gap","data":{"from_seq":requested,"to_seq":oldest - 1}}});
            if tx.try_send(encode_event(&gap)).is_err() { return false; }
        }
    }
    let replay: Vec<_> = state.ring.iter().filter(|(seq, _)| cursor.is_none_or(|c| *seq >= c))
        .map(|(_, bytes)| bytes.clone()).collect();
    if replay.len() > CLIENT_QUEUE_CAPACITY { return false; }
    if replay.into_iter().any(|bytes| tx.try_send(bytes).is_err()) { return false; }
    state.clients.insert(id, tx.clone());
    let before = remote_identity(&state);
    let previous = state.remote_clients.remove(&id);
    if let Some(login) = login { state.remote_clients.insert(id, login.to_owned()); }
    let identity = remote_identity(&state);
    let changed = before != identity || previous.as_deref() != login;
    drop(state);
    if changed { inner.publish(None, json!({"type":"remote_driver_changed","data":{"identity":identity}})); }
    true
}

fn handle_prompt(inner: &Arc<Inner>, tx: &SyncSender<Vec<u8>>, frame: &Value) {
    let Some(req_id) = frame["id"].as_u64() else { return; };
    let Some(text) = frame["text"].as_str() else {
        send(tx, json!({"type":"reply","id":req_id,"ok":false,"error":"invalid prompt"})); return;
    };
    if text.trim().is_empty() {
        send(tx, json!({"type":"reply","id":req_id,"ok":false,"error":"empty prompt"})); return;
    }
    let display = match std::panic::catch_unwind(std::panic::AssertUnwindSafe(||
        inner.host.public_prompt(text)
    )) {
        Ok(Ok(display)) => display,
        _ => {
            send(tx, json!({"type":"reply","id":req_id,"ok":false,"error":"prompt could not be scrubbed"}));
            return;
        }
    };
    // Admit prompts in the same order as permission changes. Scrubbing runs
    // before this lock so a slow sidecar cannot block unrelated controls.
    let _control_guard = inner.controls.lock().unwrap();
    let mut state = inner.state.lock().unwrap();
    if inner.stopping.load(Ordering::Acquire) {
        send(tx, json!({"type":"reply","id":req_id,"ok":false,"error":"daemon is stopping"})); return;
    }
    // The bridge checks its policy before forwarding, then the daemon checks
    // again under the same admission lock as permission changes. A local UI
    // cannot raise access between the bridge's status read and this prompt.
    if frame["remote"] == true && matches!(state.permission_mode.as_str(), "bypassPermissions" | "dontAsk" | "full-access")
        && frame["remote_allow_unrestricted"] != true {
        send(tx,json!({"type":"reply","id":req_id,"ok":false,"error":"remote prompt refused in unrestricted permission mode"}));return;
    }
    if state.busy {
        if state.prompts.len() == PROMPT_QUEUE_CAPACITY {
            send(tx, json!({"type":"reply","id":req_id,"ok":false,"error":"prompt queue full"})); return;
        }
        let queue_id = format!("q{}", state.next_queue_id);
        state.next_queue_id += 1;
        let position = state.prompts.len() + 1;
        if !send(tx, json!({"type":"reply","id":req_id,"ok":true,"queued":true,"position":position,"queue_id":queue_id})) { return; }
        state.prompts.push_back(Prompt { text: text.to_owned(), public_text: queue_preview(&display), queue_id: queue_id.clone(), peer_origin: None });
        drop(state);
        // The reply acknowledges admission; the event also lets the origin
        // display the queued prompt alongside every other attached client.
        let event = json!({"type":"prompt_queued","data":{"id":queue_id,"text":display,"position":position}});
        inner.publish(None, event);
    } else {
        let turn = format!("r{:011}", state.next_turn_id);
        if !send(tx, json!({"type":"reply","id":req_id,"ok":true,"turn":turn})) { return; }
        state.busy = true;
        state.next_turn_id += 1;
        drop(state);
        inner.start_turn(text.to_owned(), turn);
    }
}

fn handle_call(inner: &Arc<Inner>, tx: &SyncSender<Vec<u8>>, frame: &Value) {
    let Some(req_id) = frame["id"].as_u64() else { return; };
    let Some(method) = frame["method"].as_str() else { return; };
    let params = frame.get("params").filter(|v| v.is_object()).cloned().unwrap_or_else(|| json!({}));
    let _control_guard = matches!(method, "set_isolation" | "isolation_migration_plan" | "set_model" | "set_effort" | "set_permission_mode" | "switch_branch" | "stop" | "stop_if_idle")
        .then(|| inner.controls.lock().unwrap());
    let (result, changed) = if method == "answer_needs_input" {
        let reviewed = params.get("reviewed_request");
        let authorized = {
            let state = inner.state.lock().unwrap();
            state.pending_inputs_complete && state.pending_inputs.iter().any(|item|
                item["id"].as_str().is_some() && item["id"].as_str() == params["id"].as_str()
                && reviewed.is_none_or(|reviewed| reviewed == item))
        };
        if authorized { (inner.host.call(method, &params), None) }
        else { (Err("Input request changed, expired, or cannot be completely reviewed".into()), None) }
    } else if method == "queue" {
        let state = inner.state.lock().unwrap();
        let queue: Vec<_> = state.prompts.iter().map(|item| json!({
            "id":item.queue_id,"text":item.public_text
        })).collect();
        (Ok(json!({"queue":queue})), None)
    } else if method == "cancel_queued" {
        let id = params.get("id").and_then(Value::as_str);
        let position = params.get("position").and_then(Value::as_u64);
        if id.is_none_or(str::is_empty) {
            (Err("cancel_queued requires an exact queued prompt id; resolve positions from a fresh queue listing".into()), None)
        } else if params.get("position").is_some_and(|v| !v.is_u64()) || position == Some(0) {
            (Err("position must be a positive 1-based number".into()), None)
        } else {
            let mut state = inner.state.lock().unwrap();
            let id = id.expect("validated exact id");
            let index = state.prompts.iter().position(|item| item.queue_id == id);
            let expected_index = position.and_then(|position| usize::try_from(position).ok())
                .and_then(|position| position.checked_sub(1));
            if position.is_some() && index != expected_index {
                (Err("queue changed; refresh the queue and retry with its exact prompt id".into()), None)
            } else { match index.and_then(|index| state.prompts.remove(index)) {
                Some(item) => (Ok(json!({})), Some(json!({"type":"prompt_cancelled","data":{
                    "id":item.queue_id,"text":item.public_text
                }}))),
                None => (Err("no such queued prompt (already started, cancelled, or discarded)".into()), None),
            } }
        }
    } else if method == "stop_if_idle" {
        // Prompt admission (including peers) holds the same control lock.
        // Check the authoritative queue before stopping, then keep the lock
        // through the host call and shutdown flag so no turn can slip in.
        let provider_active = inner.host.has_active_work();
        let state = inner.state.lock().unwrap();
        let idle = !provider_active && !state.busy && state.prompts.is_empty()
            && !inner.stopping.load(Ordering::Acquire);
        drop(state);
        if idle { (inner.host.call("stop", &params), None) }
        else { (Err("clear requires an idle session with no queued prompts".into()), None) }
    } else if method == "get_state" {
        let state = inner.state.lock().unwrap();
        (Ok(json!({"pending_inputs":state.pending_inputs,"pending_inputs_complete":state.pending_inputs_complete,
            "running":state.busy,"queued":state.prompts.len(),"model":state.model,
            "effort":state.effort,"pending_effort":state.pending_effort})), None)
    } else if method == "status" {
        let can_set_model = inner.host.can_set_model();
        let can_set_permission_mode = inner.host.can_set_permission_mode();
        let peer_tools_ready = inner.host.peer_tools_ready();
        let lore_scrub = inner.host.lore_scrub_status();
        let billing = inner.host.billing_snapshot();
        let lore_status = inner.host.lore_status();
        let state = inner.state.lock().unwrap();
        (Ok(json!({"status":{"session_id":inner.session.session_id,"cwd":inner.session.cwd,
            "model":state.model,"permission_mode":state.permission_mode,
            "engine":inner.session.engine,"effort":state.effort,"pending_effort":state.pending_effort,"running":state.busy,"queued":state.prompts.len(),"remote_driver":remote_identity(&state),
            "can_set_model":can_set_model,
            "can_set_permission_mode":can_set_permission_mode,"peer_tools_ready":peer_tools_ready,"isolation":inner.host.isolation_status(),
            "belief_count":lore_status.as_ref().and_then(|value|value["belief_count"].as_u64()),
            "disabled_tools":lore_status.as_ref().and_then(|value|value["disabled_tools"].as_array().cloned()),
            "lore_enabled":inner.host.lore_enabled(),"lore_scrub":lore_scrub,"billing":billing,"account":inner.host.account_snapshot()}})), None)
    } else if matches!(method,"set_isolation"|"isolation_migration_plan") {
        let idle = {
            let state = inner.state.lock().unwrap();
            !state.busy && state.prompts.is_empty() && state.pending_inputs.is_empty() && !inner.stopping.load(Ordering::Acquire)
        };
        if !idle { (Err("isolation changes require an idle session with no queued prompts or pending approvals".into()),None) }
        else {
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(||inner.host.call(method,&params)))
                .unwrap_or_else(|_|Err("isolation host failed".into()));
            let changed=result.as_ref().ok().filter(|_|method=="set_isolation").and_then(|reply|reply.get("isolation"))
                .map(|value|json!({"type":"isolation_changed","data":{"isolation":value}}));
            (result,changed)
        }
    } else if method == "switch_branch" {
        let idle = {
            let state = inner.state.lock().unwrap();
            !state.busy && state.prompts.is_empty() && !inner.stopping.load(Ordering::Acquire)
        };
        if !idle {
            (Err("branch switch requires an idle session with no queued prompts".into()), None)
        } else {
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(||
                inner.host.call(method, &params)
            )).unwrap_or_else(|_| Err("branch switch host panicked".into()));
            let event = result.as_ref().ok().and_then(|value| value["base"].as_str())
                .map(|base| json!({"type":"branch_changed","data":{"base":base}}));
            (result, event)
        }
    } else if method == "set_effort" {
        // Admission and control share the state mutex. A prompt cannot be
        // admitted between the idle check and the host's effort update.
        let refuse = {
            let state = inner.state.lock().unwrap();
            state.busy || !state.prompts.is_empty() || inner.stopping.load(Ordering::Acquire)
        };
        if refuse {
            (Err("effort change requires an idle session with no queued prompts".into()), None)
        } else {
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(||
                inner.host.call(method, &params)
            )).unwrap_or_else(|_| Err("effort host panicked".into())).and_then(|extra| {
                let requested = params.get("effort").and_then(Value::as_str);
                let selected = extra.get("effort").and_then(Value::as_str);
                if selected.is_some_and(|value| Some(value) == requested && !value.is_empty()
                    && !value.chars().any(char::is_control)) { Ok(extra) }
                else { Err("host returned invalid effort reply".into()) }
            });
            let changed = result.as_ref().ok().and_then(|extra| extra["effort"].as_str())
                .map(|effort| {
                    let pending = result.as_ref().ok().is_some_and(|extra| extra["verification_pending"] == true);
                    let mut state = inner.state.lock().unwrap();
                    if pending { state.pending_effort = Some(effort.to_owned()); }
                    else { state.effort = Some(effort.to_owned()); state.pending_effort = None; }
                    json!({"type":if pending { "effort_requested" } else { "effort_changed" },"data":{"effort":effort,"verification_pending":pending}})
                });
            (result, changed)
        }
    } else if matches!(method, "set_model" | "set_permission_mode") {
        // Control calls may wait on a sidecar. Hold the control lock across
        // that call, but never the global state lock: event publishing and
        // status reads must remain responsive while a sidecar answers.
        let refuse_model = method == "set_model" && inner.host.model_change_requires_idle() && {
            let state = inner.state.lock().unwrap();
            state.busy || !state.prompts.is_empty() || inner.stopping.load(Ordering::Acquire)
        };
        let refuse_permission = method == "set_permission_mode" && inner.host.permission_change_requires_idle() && {
            let state = inner.state.lock().unwrap();
            state.busy || !state.prompts.is_empty() || inner.stopping.load(Ordering::Acquire)
        };
        let refuse_dont_ask = {
            let state = inner.state.lock().unwrap();
            method == "set_permission_mode" && params["mode"] == "dontAsk"
                && state.permission_mode != "dontAsk" && (state.busy || !state.prompts.is_empty())
        };
        if refuse_model {
            (Err("Finish the current response and queued prompts, then change the model for the next turn".into()), None)
        } else if refuse_permission {
            (Err("Finish the current response and queued prompts, then change permissions for the next turn".into()), None)
        } else if refuse_dont_ask {
            (Err("dontAsk requires an idle session with no queued prompts".into()), None)
        } else {
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(||
                inner.host.call(method, &params)
            )).unwrap_or_else(|_| Err("host panicked".into())).and_then(|extra| {
                let field = if method == "set_model" { "model" } else { "mode" };
                let valid = extra.get(field).and_then(Value::as_str).is_some_and(|value| {
                    !value.trim().is_empty()
                        && !value.chars().any(char::is_control)
                        && (field != "mode" ||
                            (matches!(value, "default" | "acceptEdits" | "plan" | "auto" | "dontAsk" | "on-request" | "full-access")
                                && params["mode"] == value))
                });
                if valid { Ok(extra) } else { Err(format!("host returned invalid {field} reply")) }
            });
            let changed = match (&result, method) {
                (Ok(extra), "set_model") => extra["model"].as_str().map(|model| {
                    let mut state = inner.state.lock().unwrap();
                    state.model = if model == "default" { None } else { Some(model.to_owned()) };
                    if extra.get("effort").is_some() { state.effort = extra["effort"].as_str().map(str::to_owned); }
                    json!({"type":"model_changed","data":{"model":model,"effort":state.effort}})
                }),
                (Ok(extra), "set_permission_mode") => extra["mode"].as_str().map(|mode| {
                    let mut state = inner.state.lock().unwrap();
                    state.permission_mode = mode.to_owned();
                    json!({"type":"permission_mode_changed","data":{"mode":mode}})
                }),
                _ => None,
            };
            (result, changed)
        }
    } else { (inner.host.call(method, &params), None) };
    let stop_ok = matches!(method, "stop" | "stop_if_idle")
        && matches!(&result, Ok(value) if value.is_object());
    match result {
        Ok(extra) if extra.is_object() => {
            let mut reply = json!({"type":"reply","id":req_id,"ok":true});
            for (key, value) in extra.as_object().unwrap() {
                if key != "type" && key != "id" && key != "ok" { reply[key] = value.clone(); }
            }
            send(tx, reply);
            if let Some(event) = changed { inner.publish(None, event); }
        }
        Ok(_) => { send(tx, json!({"type":"reply","id":req_id,"ok":false,"error":"host returned invalid reply"})); }
        Err(error) => { send(tx, json!({"type":"reply","id":req_id,"ok":false,"error":error})); }
    }
    if stop_ok { inner.stopping.store(true, Ordering::Release); }
}

fn send(tx: &SyncSender<Vec<u8>>, frame: Value) -> bool { tx.try_send(encode_reply(&frame)).is_ok() }

fn encode_reply(frame: &Value) -> Vec<u8> {
    let mut bytes = serde_json::to_vec(frame).unwrap_or_default();
    bytes.push(b'\n');
    if bytes.len() <= MAX_FRAME_BYTES { bytes } else {
        let fallback = json!({"type":frame["type"],"id":frame["id"],"ok":false,
            "error":"reply exceeded the frame cap"});
        let mut bytes = serde_json::to_vec(&fallback).unwrap_or_default();
        bytes.push(b'\n');
        bytes
    }
}

fn encode_event(frame: &Value) -> Vec<u8> {
    let mut original = serde_json::to_vec(frame).unwrap_or_default();
    original.push(b'\n');
    if original.len() <= MAX_FRAME_BYTES { return original; }
    let kind = frame["event"]["type"].as_str().unwrap_or("unknown");
    let kind: String = kind.chars().take(128).collect();
    let slim = json!({"type":"event","seq":frame["seq"],"turn":frame["turn"],
        "event":{"type":kind,"data":{"truncated":true,
        "note":"event exceeded the frame cap; see the transcript"}}});
    encode_reply(&slim)
}

fn read_bounded(reader: &mut BufReader<UnixStream>, line: &mut Vec<u8>) -> io::Result<usize> {
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() { return if line.is_empty() { Ok(0) } else { Err(io::ErrorKind::UnexpectedEof.into()) }; }
        let n = available.iter().position(|b| *b == b'\n').map_or(available.len(), |n| n + 1);
        if line.len() + n > MAX_FRAME_BYTES { return Err(io::Error::new(io::ErrorKind::InvalidData, "frame too large")); }
        let complete = available[n - 1] == b'\n';
        line.extend_from_slice(&available[..n]);
        reader.consume(n);
        if complete { return Ok(line.len()); }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct StopHost(AtomicUsize);
    impl Host for StopHost {
        fn prompt(&self, _: &str, _: &mut dyn FnMut(Value)) {}
        fn call(&self, method: &str, _: &Value) -> Result<Value, String> {
            if method == "stop" { self.0.fetch_add(1, Ordering::Relaxed); }
            Ok(json!({}))
        }
    }

    #[test]
    fn clear_stop_is_rejected_for_busy_or_queued_session_and_closes_admission() {
        let dir = tempfile::tempdir().unwrap();
        let host = Arc::new(StopHost(AtomicUsize::new(0)));
        let daemon = Daemon::bind(dir.path(), Session {
            session_id: "clear-stop-test".into(), cwd: "/tmp".into(), model: None,
            engine: "test".into(), doxa_version: "test".into(),
        }, host.clone()).unwrap();
        let (tx, rx) = mpsc::sync_channel(CLIENT_QUEUE_CAPACITY);
        let call = json!({"id":1,"method":"stop_if_idle","params":{}});
        daemon.inner.state.lock().unwrap().busy = true;
        handle_call(&daemon.inner, &tx, &call);
        assert_eq!(serde_json::from_slice::<Value>(&rx.try_recv().unwrap()).unwrap()["ok"], false);
        assert_eq!(host.0.load(Ordering::Relaxed), 0);
        {
            let mut state = daemon.inner.state.lock().unwrap();
            state.busy = false;
            state.prompts.push_back(Prompt { text: "queued".into(), public_text: "queued".into(),
                queue_id: "q1".into(), peer_origin: None });
        }
        handle_call(&daemon.inner, &tx, &call);
        assert_eq!(serde_json::from_slice::<Value>(&rx.try_recv().unwrap()).unwrap()["ok"], false);
        assert_eq!(host.0.load(Ordering::Relaxed), 0);
        daemon.inner.state.lock().unwrap().prompts.clear();
        handle_call(&daemon.inner, &tx, &call);
        assert_eq!(serde_json::from_slice::<Value>(&rx.try_recv().unwrap()).unwrap()["ok"], true);
        assert_eq!(host.0.load(Ordering::Relaxed), 1);
        let prompt = json!({"id":2,"text":"late prompt"});
        handle_prompt(&daemon.inner, &tx, &prompt);
        assert_eq!(serde_json::from_slice::<Value>(&rx.try_recv().unwrap()).unwrap()["ok"], false);
    }

    struct NoopHost;
    impl Host for NoopHost {
        fn prompt(&self, _: &str, _: &mut dyn FnMut(Value)) {}
        fn call(&self, _: &str, _: &Value) -> Result<Value, String> { Ok(json!({})) }
    }

    #[test]
    fn failed_effort_verification_clears_pending_but_preserves_verified_effort() {
        let dir = tempfile::tempdir().unwrap();
        let daemon = Daemon::bind(dir.path(), Session {
            session_id: "effort-test".into(), cwd: "/fixture".into(), model: None,
            engine: "claude".into(), doxa_version: "test".into(),
        }, Arc::new(NoopHost)).unwrap();
        {
            let mut state = daemon.inner.state.lock().unwrap();
            state.effort = Some("low".into()); state.pending_effort = Some("high".into());
        }
        daemon.inner.publish(None, json!({"type":"effort_verification_failed","data":{"effort":"low","requested_effort":"high"}}));
        let state = daemon.inner.state.lock().unwrap();
        assert_eq!(state.effort.as_deref(), Some("low"));
        assert!(state.pending_effort.is_none());
    }

    #[test]
    fn remote_prompt_gate_is_atomic_with_permission_mode() {
        let dir=tempfile::tempdir().unwrap();
        let daemon=Daemon::bind(dir.path(),Session{session_id:"remote-gate".into(),cwd:"/fixture".into(),
            model:None,engine:"test".into(),doxa_version:"test".into()},Arc::new(NoopHost)).unwrap();
        let (tx,rx)=mpsc::sync_channel(CLIENT_QUEUE_CAPACITY);
        daemon.inner.state.lock().unwrap().permission_mode="full-access".into();
        handle_prompt(&daemon.inner,&tx,&json!({"id":1,"text":"run","remote":true,"remote_allow_unrestricted":false}));
        assert_eq!(serde_json::from_slice::<Value>(&rx.try_recv().unwrap()).unwrap()["ok"],false);
        assert!(!daemon.inner.state.lock().unwrap().busy);
    }

    #[test]
    fn failed_replay_does_not_register_client() {
        let dir = tempfile::tempdir().unwrap();
        let daemon = Daemon::bind(dir.path(), Session {
            session_id: "replay-test".into(), cwd: "/tmp".into(), model: None,
            engine: "test".into(), doxa_version: "test".into(),
        }, Arc::new(NoopHost)).unwrap();
        daemon.inner.publish(None, json!({"type":"text_delta","data":{"text":"retained"}}));
        let (tx, rx) = mpsc::sync_channel(CLIENT_QUEUE_CAPACITY);
        drop(rx);
        assert!(!attach_client(&daemon.inner, 1, None, &tx));
        assert!(daemon.inner.state.lock().unwrap().clients.is_empty());
    }

    #[test]
    fn external_peer_prompts_share_the_eight_slot_bound() {
        let dir = tempfile::tempdir().unwrap();
        let mut handle = Daemon::bind(dir.path(), Session {
            session_id: "peer-queue-test".into(), cwd: "/tmp".into(), model: None,
            engine: "test".into(), doxa_version: "test".into(),
        }, Arc::new(NoopHost)).unwrap().start();
        handle.inner.state.lock().unwrap().busy = true;
        for _ in 0..PROMPT_QUEUE_CAPACITY {
            assert_eq!(handle.enqueue_peer_prompt("peer task".into(), "sender").unwrap(), ExternalPrompt::Queued);
        }
        assert_eq!(handle.enqueue_peer_prompt("overflow".into(), "sender").unwrap(), ExternalPrompt::Full);
        assert_eq!(handle.inner.state.lock().unwrap().prompts.len(), PROMPT_QUEUE_CAPACITY);
        handle.shutdown();
    }
    #[test]
    fn remote_browser_identity_is_visible_and_removed_on_disconnect() {
        let dir = tempfile::tempdir().unwrap();
        let daemon = Daemon::bind(dir.path(), Session { session_id:"remote-test".into(),cwd:"/repo".into(),
            model:None,engine:"test".into(),doxa_version:"test".into() }, Arc::new(StopHost(AtomicUsize::new(0)))).unwrap();
        let (tx, rx) = mpsc::sync_channel(CLIENT_QUEUE_CAPACITY);
        assert!(attach_client_with_identity(&daemon.inner, 1, None, &tx, Some("owner@example.com")));
        assert_eq!(remote_identity(&daemon.inner.state.lock().unwrap()), Some("owner@example.com".into()));
        let frame: Value = serde_json::from_slice(&rx.try_recv().unwrap()).unwrap();
        assert_eq!(frame["event"]["type"], "remote_driver_changed");
        assert_eq!(frame["event"]["data"]["identity"], "owner@example.com");
        assert!(attach_client_with_identity(&daemon.inner, 2, Some(1), &tx, Some("second@example.com")));
        assert_eq!(remote_identity(&daemon.inner.state.lock().unwrap()), Some("owner@example.com, second@example.com".into()));
        detach_client(&daemon.inner, 1);
        assert_eq!(remote_identity(&daemon.inner.state.lock().unwrap()), Some("second@example.com".into()));
        detach_client(&daemon.inner, 2);
        assert_eq!(remote_identity(&daemon.inner.state.lock().unwrap()), None);
        assert!(daemon.inner.state.lock().unwrap().remote_clients.is_empty());
    }

}
