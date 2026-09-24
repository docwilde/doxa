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
const MAX_CONNECTIONS: usize = 64;

/// The engine seam. `prompt` may emit zero or more protocol event objects.
/// Each event is wrapped in a sequence-numbered `event` frame by the daemon.
/// If the host returns without `turn_done` or `turn_refused`, the daemon emits
/// one `turn_done`; it also emits an error terminal if the host panics.
/// Methods are called from worker threads and must be safe for concurrent calls.
pub trait Host: Send + Sync + 'static {
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value));
    fn call(&self, method: &str, params: &Value) -> Result<Value, String>;
    /// Text included in queue events. A real host can scrub prompts before
    /// they are sent to other attached clients; internal execution keeps the
    /// original prompt. An error rejects new prompts before queueing.
    fn public_prompt(&self, text: &str) -> Result<String, String> { Ok(text.to_owned()) }
}

#[derive(Clone)]
pub struct Session {
    pub session_id: String,
    pub cwd: String,
    pub model: Option<String>,
    pub engine: String,
    pub doxa_version: String,
}

struct Prompt { text: String, queue_id: String }
struct State {
    next_seq: u64,
    ring: VecDeque<(u64, Vec<u8>)>,
    clients: HashMap<u64, SyncSender<Vec<u8>>>,
    busy: bool,
    prompts: VecDeque<Prompt>,
    next_queue_id: u64,
    next_turn_id: u64,
}

struct Inner {
    state: Mutex<State>,
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
        let hello = json!({"type":"hello","proto":1,"doxa":session.doxa_version,
            "session_id":session.session_id,"model":session.model,"engine":session.engine,
            "cwd":session.cwd,"next_seq":0});
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
                next_seq: 0, ring: VecDeque::new(), clients: HashMap::new(), busy: false,
                prompts: VecDeque::new(), next_queue_id: 1, next_turn_id: 1,
            }), host, session, stopping: AtomicBool::new(false), next_client_id: AtomicU64::new(1),
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

    /// Publish an out-of-band event (`turn: null`) to the ring and clients.
    pub fn publish(&self, event: Value) { self.inner.publish(None, event); }

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
                let mut state = inner.state.lock().unwrap();
                if let Some(prompt) = state.prompts.pop_front() {
                    let turn = format!("r{:011}", state.next_turn_id);
                    state.next_turn_id += 1;
                    Some((prompt, turn))
                } else { state.busy = false; None }
            };
            if let Some((prompt, turn)) = next {
                let display = inner.host.public_prompt(&prompt.text)
                    .unwrap_or_else(|_| "[redacted: prompt unavailable]".to_owned());
                inner.publish(None, json!({"type":"prompt_dequeued","data":{"id":prompt.queue_id,"text":display}}));
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
    let hello = {
        let state = inner.state.lock().unwrap();
        json!({"type":"hello", "proto":1, "doxa":inner.session.doxa_version,
            "session_id":inner.session.session_id, "model":inner.session.model,
            "engine":inner.session.engine, "cwd":inner.session.cwd, "next_seq":state.next_seq})
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
                let mut state = inner.state.lock().unwrap();
                // Replay and registration are one atomic operation with publish.
                let replay: Vec<_> = state.ring.iter().filter(|(seq, _)| cursor.is_none_or(|c| *seq >= c))
                    .map(|(_, bytes)| bytes.clone()).collect();
                if replay.len() > CLIENT_QUEUE_CAPACITY { break; }
                for bytes in replay { if tx.try_send(bytes).is_err() { break; } }
                state.clients.insert(id, tx.clone());
            }
            Some("prompt") | Some("call") if !inner.state.lock().unwrap().clients.contains_key(&id) => {
                send(&tx, json!({"type":"reply","id":frame["id"],"ok":false,"error":"attach required"}));
            }
            Some("prompt") => handle_prompt(&inner, &tx, id, &frame),
            Some("call") => handle_call(&inner, &tx, &frame),
            _ => {}
        }
    }
    inner.state.lock().unwrap().clients.remove(&id);
    drop(tx);
    let _ = writer_thread.join();
}

fn handle_prompt(inner: &Arc<Inner>, tx: &SyncSender<Vec<u8>>, client_id: u64, frame: &Value) {
    let Some(req_id) = frame["id"].as_u64() else { return; };
    let Some(text) = frame["text"].as_str() else {
        send(tx, json!({"type":"reply","id":req_id,"ok":false,"error":"invalid prompt"})); return;
    };
    if text.trim().is_empty() {
        send(tx, json!({"type":"reply","id":req_id,"ok":false,"error":"empty prompt"})); return;
    }
    let display = match inner.host.public_prompt(text) {
        Ok(display) => display,
        Err(_) => {
            send(tx, json!({"type":"reply","id":req_id,"ok":false,"error":"prompt could not be scrubbed"}));
            return;
        }
    };
    let mut state = inner.state.lock().unwrap();
    if state.busy {
        if state.prompts.len() == PROMPT_QUEUE_CAPACITY {
            send(tx, json!({"type":"reply","id":req_id,"ok":false,"error":"prompt queue full"})); return;
        }
        let queue_id = format!("q{}", state.next_queue_id);
        state.next_queue_id += 1;
        let position = state.prompts.len() + 1;
        if !send(tx, json!({"type":"reply","id":req_id,"ok":true,"queued":true,"position":position,"queue_id":queue_id})) { return; }
        state.prompts.push_back(Prompt { text: text.to_owned(), queue_id: queue_id.clone() });
        drop(state);
        // Origin client receives its queue notification in the reply only.
        let event = json!({"type":"prompt_queued","data":{"id":queue_id,"text":display,"position":position}});
        publish_except(inner, client_id, event);
    } else {
        let turn = format!("r{:011}", state.next_turn_id);
        if !send(tx, json!({"type":"reply","id":req_id,"ok":true,"turn":turn})) { return; }
        state.busy = true;
        state.next_turn_id += 1;
        drop(state);
        inner.start_turn(text.to_owned(), turn);
    }
}

fn publish_except(inner: &Inner, excluded: u64, event: Value) {
    let mut state = inner.state.lock().unwrap();
    let seq = state.next_seq;
    let Some(next) = seq.checked_add(1) else { return; };
    state.next_seq = next;
    let bytes = encode_event(&json!({"type":"event","seq":seq,"turn":null,"event":event}));
    state.ring.push_back((seq, bytes.clone()));
    if state.ring.len() > RING_CAPACITY { state.ring.pop_front(); }
    state.clients.retain(|id, tx| *id == excluded || tx.try_send(bytes.clone()).is_ok());
}

fn handle_call(inner: &Arc<Inner>, tx: &SyncSender<Vec<u8>>, frame: &Value) {
    let Some(req_id) = frame["id"].as_u64() else { return; };
    let Some(method) = frame["method"].as_str() else { return; };
    let params = frame.get("params").filter(|v| v.is_object()).cloned().unwrap_or_else(|| json!({}));
    let result = if method == "status" {
        let state = inner.state.lock().unwrap();
        Ok(json!({"status":{"session_id":inner.session.session_id,"cwd":inner.session.cwd,
            "model":inner.session.model,"engine":inner.session.engine,"running":state.busy,"queued":state.prompts.len()}}))
    } else { inner.host.call(method, &params) };
    let stop_ok = method == "stop" && matches!(&result, Ok(value) if value.is_object());
    match result {
        Ok(extra) if extra.is_object() => {
            let mut reply = json!({"type":"reply","id":req_id,"ok":true});
            for (key, value) in extra.as_object().unwrap() {
                if key != "type" && key != "id" && key != "ok" { reply[key] = value.clone(); }
            }
            send(tx, reply);
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
