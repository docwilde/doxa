//! Connect one daemon socket to the terminal loop without blocking input.

use std::collections::{HashMap, HashSet};
use std::io;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::mpsc::{self, Receiver, SyncSender, TryRecvError};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::transport::{DaemonClient, TransportError};
use crate::history;
use crate::ui;
use crate::discovery::Session;

pub enum WorkerCommand {
    Prompt(String, String),
    Answer(String, String, Value),
}

fn revoke(guard: &Mutex<bool>) {
    *guard.lock().unwrap_or_else(|poison| poison.into_inner()) = false;
}

/// Each session owns its socket, replay cursor, and bounded command queue.
/// `complete` becomes false on any failed attach or disconnect and stays
/// false for this UI lifetime, so a partial roster never rewrites a tabset.
pub struct MultiBridge {
    pub frames: Receiver<Value>,
    pub commands: SyncSender<WorkerCommand>,
    pub live_ids: Vec<String>,
    pub complete: Arc<Mutex<bool>>,
    router: JoinHandle<()>,
    workers: Vec<JoinHandle<()>>,
}

impl MultiBridge {
    pub fn shutdown(self) {
        drop(self.commands);
        drop(self.frames);
        let _ = self.router.join();
        for worker in self.workers { let _ = worker.join(); }
    }
}

pub fn connect_sessions(sessions: &[Session]) -> io::Result<MultiBridge> {
    if sessions.is_empty() || sessions.len() > 64 {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "expected 1–64 live sessions"));
    }
    let mut seen = HashSet::new();
    if sessions.iter().any(|s| !crate::discovery::valid_id(&s.id) || !seen.insert(s.id.as_str())) {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid or duplicate session ID in roster"));
    }
    let (frame_tx, frame_rx) = mpsc::sync_channel(128);
    let (command_tx, command_rx) = mpsc::sync_channel(32);
    let complete = Arc::new(Mutex::new(true));
    let mut routes = HashMap::new();
    let mut workers = Vec::new();
    let mut live_ids = Vec::new();
    for session in sessions {
        let (client, snapshot) = match DaemonClient::connect_for_restore(&session.socket) {
            Ok(pair) if pair.0.hello["session_id"] == session.id => pair,
            _ => {
                revoke(&complete);
                let _ = frame_tx.send(json!({"type":"client_notice", "session_id":session.id,
                    "message":"Session unavailable; saved layout is read-only"}));
                continue;
            }
        };
        let (tx, rx) = mpsc::sync_channel(32);
        let connected = Arc::new(AtomicBool::new(true));
        routes.insert(session.id.clone(), (tx, Arc::clone(&connected)));
        live_ids.push(session.id.clone());
        let path = session.socket.clone();
        let id = session.id.clone();
        let frames = frame_tx.clone();
        let guard = Arc::clone(&complete);
        workers.push(thread::spawn(move || {
            let cursor = AtomicU64::new(client.cursor);
            let mut current = (client, snapshot);
            loop {
                worker_loop(current.0, current.1, &frames, &rx, &cursor, Some(&guard));
                connected.store(false, Ordering::Release);
                revoke(&guard);
                if frames.send(json!({"type":"client_notice", "session_id":id,
                    "message":"Daemon disconnected; reconnecting"})).is_err() { return; }
                loop {
                    match rx.recv_timeout(Duration::from_millis(250)) {
                        Err(mpsc::RecvTimeoutError::Disconnected) => return,
                        Ok(command) => { let _ = frames.send(rejected(command, "Daemon reconnecting")); }
                        Err(mpsc::RecvTimeoutError::Timeout) => {}
                    }
                    match DaemonClient::connect(&path, Some(cursor.load(Ordering::Relaxed))) {
                        Ok(client) if client.hello["session_id"] == id => {
                            connected.store(true, Ordering::Release);
                            current = (client, None);
                            break;
                        }
                        _ => {}
                    }
                }
            }
        }));
    }
    if live_ids.is_empty() {
        drop(command_rx);
        drop(frame_tx);
        for worker in workers { let _ = worker.join(); }
        return Err(io::Error::new(io::ErrorKind::NotConnected, "no daemon in roster accepted an attach"));
    }
    let router_frames = frame_tx.clone();
    let router = thread::spawn(move || {
        while let Ok(command) = command_rx.recv() {
            let id = match &command {
                WorkerCommand::Prompt(id, _) | WorkerCommand::Answer(id, _, _) => id,
            };
            let Some((route, connected)) = routes.get(id) else {
                let _ = router_frames.send(rejected(command, "Session is not attached"));
                continue;
            };
            if !connected.load(Ordering::Acquire) {
                let _ = router_frames.send(rejected(command, "Daemon reconnecting"));
                continue;
            }
            match route.try_send(command) {
                Ok(()) => {}
                Err(mpsc::TrySendError::Full(command)) => {
                    let _ = router_frames.send(rejected(command, "Daemon command queue full"));
                }
                Err(mpsc::TrySendError::Disconnected(command)) => {
                    let _ = router_frames.send(rejected(command, "Daemon worker unavailable"));
                }
            }
        }
    });
    drop(frame_tx);
    Ok(MultiBridge { frames: frame_rx, commands: command_tx, live_ids, complete, router, workers })
}

fn rejected(command: WorkerCommand, message: &str) -> Value {
    match command {
        WorkerCommand::Prompt(id, text) => json!({"type":"prompt_rejected", "session_id":id,
            "text":text, "message":message}),
        WorkerCommand::Answer(session, request, _) => json!({"type":"answer_reply", "session_id":session,
            "request_id":request, "ok":false, "message":message}),
    }
}

pub fn run_sessions(sessions: &[Session], store: Option<crate::ui_state::UiStateStore>) -> io::Result<()> {
    let MultiBridge { frames, commands, live_ids, complete, router, workers } = connect_sessions(sessions)?;
    let result = if let Some(store) = store {
        ui::run_with_channels_state_guarded(frames, commands.clone(), store, live_ids, complete)
    } else {
        ui::run_with_channels(frames, commands.clone())
    };
    drop(commands);
    let _ = router.join();
    for worker in workers { let _ = worker.join(); }
    result
}

/// Attach to a running Python DOXA daemon. The Rust TUI currently hosts one
/// session per process; additional session discovery belongs to later 2.0 work.
pub fn run_socket(path: impl AsRef<Path>) -> io::Result<()> {
    let (client, snapshot) = DaemonClient::connect_for_restore(path).map_err(as_io_error)?;
    let (frames, prompts, worker) = spawn_worker_with_snapshot(client, snapshot);
    let result = ui::run_with_channels(frames, prompts);
    // Dropping the UI's channel sender on return tells the reader thread to
    // stop after its current bounded socket poll or prompt acknowledgement.
    let _ = worker.join();
    result
}

fn as_io_error(error: TransportError) -> io::Error {
    io::Error::other(error)
}

#[cfg(test)]
fn spawn_worker(client: DaemonClient) -> (Receiver<Value>, SyncSender<WorkerCommand>, JoinHandle<()>) {
    spawn_worker_with_snapshot(client, None)
}

fn spawn_worker_with_snapshot(client: DaemonClient, snapshot: Option<crate::transport::TranscriptSnapshot>) -> (Receiver<Value>, SyncSender<WorkerCommand>, JoinHandle<()>) {
    let (frame_tx, frame_rx) = mpsc::sync_channel(128);
    let (prompt_tx, prompt_rx) = mpsc::sync_channel(32);
    let worker = thread::spawn(move || {
        let cursor = AtomicU64::new(client.cursor);
        worker_loop(client, snapshot, &frame_tx, &prompt_rx, &cursor, None);
    });
    (frame_rx, prompt_tx, worker)
}

fn worker_loop(
    mut client: DaemonClient,
    snapshot: Option<crate::transport::TranscriptSnapshot>,
    frames: &SyncSender<Value>,
    prompts: &Receiver<WorkerCommand>,
    cursor: &AtomicU64,
    roster_guard: Option<&Mutex<bool>>,
) {
    let session_id = client.hello["session_id"]
        .as_str()
        .unwrap_or_default()
        .to_owned();
    if frames.send(client.hello.clone()).is_err() {
        return;
    }
    if let Some(snapshot) = snapshot {
        let markdown = history::render(&snapshot);
        if !markdown.is_empty() && frames.send(json!({"type":"event", "session_id":session_id,
            "event":{"type":"text_delta", "data":{"text":markdown}}})).is_err() {
            return;
        }
    }
    loop {
        // Bound prompt work so a burst cannot starve incoming daemon events.
        for _ in 0..32 {
            match prompts.try_recv() {
                Ok(WorkerCommand::Answer(session, id, answer)) => {
                    if session != session_id || !answer.is_object() {
                        let _ = frames.send(json!({"type":"answer_reply", "session_id":session,
                            "request_id":id, "ok":false, "message":"Invalid answer target or payload"}));
                        continue;
                    }
                    let mut params = Map::new();
                    params.insert("id".into(), Value::String(id.clone()));
                    params.insert("answer".into(), answer);
                    let result = client.call("answer_needs_input", params);
                    cursor.store(client.cursor, Ordering::Relaxed);
                    if matches!(result, Err(TransportError::Closed)) {
                        if let Some(guard) = roster_guard { revoke(guard); }
                    }
                    let frame = match result {
                        Ok(ref reply) => json!({"type":"answer_reply", "session_id":session,
                            "request_id":id, "ok":reply["ok"] == true,
                            "message":reply["error"].as_str().unwrap_or("Request no longer pending")}),
                        Err(ref error) => json!({"type":"answer_reply", "session_id":session,
                            "request_id":id, "ok":false,
                            "uncertain":!matches!(error, TransportError::FrameTooLarge | TransportError::RequestIdsExhausted),
                            "message":error.to_string()}),
                    };
                    if frames.send(frame).is_err() {
                        return;
                    }
                    if matches!(result, Err(TransportError::Closed)) {
                        return;
                    }
                }
                Ok(WorkerCommand::Prompt(id, text)) => {
                    if id != session_id {
                        if frames
                            .send(json!({"type":"prompt_rejected", "session_id":id, "text":text,
                            "message":"Prompt target is not attached"}))
                            .is_err()
                        {
                            return;
                        }
                        continue;
                    }
                    match client.prompt(&text) {
                        Ok(reply) => {
                            let mut frame = if reply["ok"] == false {
                                json!({"type":"prompt_rejected", "session_id":id, "text":text,
                                    "message":reply["error"].as_str().unwrap_or("Prompt refused")})
                            } else {
                                reply
                            };
                            frame["session_id"] = Value::String(session_id.clone());
                            if frames.send(frame).is_err() {
                                return;
                            }
                        }
                        Err(error) => {
                            if matches!(error, TransportError::Closed) {
                                if let Some(guard) = roster_guard { revoke(guard); }
                            }
                            // Once bytes may have been written, a missing reply does not
                            // prove that the daemon rejected the prompt. Keep the draft
                            // available, but require a deliberate retry by the user.
                            let (kind, message) = match error {
                                TransportError::FrameTooLarge
                                | TransportError::RequestIdsExhausted => {
                                    ("prompt_rejected", "Prompt could not be sent")
                                }
                                TransportError::Timeout => {
                                    ("prompt_uncertain", "Prompt delivery unconfirmed")
                                }
                                TransportError::Closed => (
                                    "prompt_uncertain",
                                    "Daemon disconnected; prompt delivery unconfirmed",
                                ),
                                _ => ("prompt_uncertain", "Prompt delivery unconfirmed"),
                            };
                            let _ =
                                frames.send(json!({"type":kind, "session_id":session_id,
                                    "text":text, "message":message}));
                            if matches!(error, TransportError::Closed) {
                                return;
                            }
                        }
                    }
                    cursor.store(client.cursor, Ordering::Relaxed);
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => return,
            }
        }
        match client.poll_frame(Duration::from_millis(50)) {
            Ok(Some(mut frame)) => {
                frame["session_id"] = Value::String(session_id.clone());
                if frames.send(frame).is_err() {
                    return;
                }
            }
            Ok(None) => {}
            Err(error) => {
                if let Some(guard) = roster_guard { revoke(guard); }
                let message = match error {
                    TransportError::Closed => "Daemon disconnected",
                    TransportError::FrameTooLarge => "Daemon sent an oversized frame",
                    _ => "Daemon connection failed",
                };
                let _ = frames.send(json!({"type":"client_notice", "session_id":session_id,
                    "message":message}));
                return;
            }
        }
        cursor.store(client.cursor, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Write};
    use std::os::unix::net::UnixListener;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static NEXT_SOCKET: AtomicUsize = AtomicUsize::new(0);

    #[test]
    fn live_daemon_frames_and_prompts_cross_the_ui_bridge() {
        let path = std::env::temp_dir().join(format!(
            "doxa-rust-bridge-{}-{}.sock",
            std::process::id(),
            NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)
        ));
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            writeln!(
                socket,
                "{}",
                json!({
                    "type":"hello", "proto":1, "session_id":"session-1",
                    "cwd":"/tmp", "model":"test", "engine":"claude", "next_seq":1
                })
            )
            .unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            assert_eq!(
                serde_json::from_str::<Value>(&line).unwrap(),
                json!({"type":"attach","cursor":null})
            );
            writeln!(
                socket,
                "{}",
                json!({
                    "type":"event", "seq":1, "turn":null,
                    "event":{"type":"text_delta", "data":{"text":"hello"}}
                })
            )
            .unwrap();
            line.clear();
            reader.read_line(&mut line).unwrap();
            assert_eq!(
                serde_json::from_str::<Value>(&line).unwrap(),
                json!({"type":"prompt","id":1,"text":"next"})
            );
            writeln!(
                socket,
                "{}",
                json!({"type":"reply","id":1,"ok":true,"turn":"turn-1"})
            )
            .unwrap();
        });
        let client = DaemonClient::connect(&path, None).unwrap();
        let (frames, prompts, worker) = spawn_worker(client);
        assert_eq!(
            frames.recv_timeout(Duration::from_secs(2)).unwrap()["session_id"],
            "session-1"
        );
        let event = frames.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(event["session_id"], "session-1");
        assert_eq!(event["event"]["data"]["text"], "hello");
        prompts
            .send(WorkerCommand::Prompt("session-1".into(), "next".into()))
            .unwrap();
        assert_eq!(
            frames.recv_timeout(Duration::from_secs(2)).unwrap()["turn"],
            "turn-1"
        );
        drop(prompts);
        worker.join().unwrap();
        server.join().unwrap();
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn daemon_rejection_returns_the_original_prompt_to_ui() {
        let path = std::env::temp_dir().join(format!(
            "doxa-rust-bridge-reject-{}-{}.sock",
            std::process::id(),
            NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)
        ));
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            writeln!(
                socket,
                "{}",
                json!({
                    "type":"hello", "proto":1, "session_id":"session-1",
                    "cwd":"/tmp", "model":"test", "engine":"claude", "next_seq":0
                })
            )
            .unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap(); // attach
            line.clear();
            reader.read_line(&mut line).unwrap();
            let request: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(request["text"], "please retry");
            writeln!(
                socket,
                "{}",
                json!({"type":"reply", "id":request["id"], "ok":false, "error":"queue full"})
            )
            .unwrap();
        });
        let client = DaemonClient::connect(&path, None).unwrap();
        let (frames, prompts, worker) = spawn_worker(client);
        frames.recv_timeout(Duration::from_secs(2)).unwrap(); // hello
        prompts
            .send(WorkerCommand::Prompt(
                "session-1".into(),
                "please retry".into(),
            ))
            .unwrap();
        let rejected = frames.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(rejected["type"], "prompt_rejected");
        assert_eq!(rejected["text"], "please retry");
        assert_eq!(rejected["message"], "queue full");
        drop(prompts);
        worker.join().unwrap();
        server.join().unwrap();
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn answer_uses_call_protocol_and_reports_reply() {
        let path = std::env::temp_dir().join(format!(
            "doxa-rust-answer-{}-{}.sock",
            std::process::id(),
            NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)
        ));
        let listener = UnixListener::bind(&path).unwrap();
        let server = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            writeln!(
                socket,
                "{}",
                json!({"type":"hello", "proto":1, "session_id":"session-1",
                "cwd":"/tmp", "model":"test", "engine":"claude", "next_seq":0})
            )
            .unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap(); // attach
            line.clear();
            reader.read_line(&mut line).unwrap();
            let call: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(call["type"], "call");
            assert_eq!(call["method"], "answer_needs_input");
            assert_eq!(
                call["params"],
                json!({"id":"req-1", "answer":{"decision":"deny"}})
            );
            writeln!(
                socket,
                "{}",
                json!({"type":"reply", "id":call["id"], "ok":true})
            )
            .unwrap();
        });
        let client = DaemonClient::connect(&path, None).unwrap();
        let (frames, commands, worker) = spawn_worker(client);
        frames.recv_timeout(Duration::from_secs(2)).unwrap();
        commands
            .send(WorkerCommand::Answer(
                "session-1".into(),
                "req-1".into(),
                json!({"decision":"deny"}),
            ))
            .unwrap();
        let reply = frames.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(reply["type"], "answer_reply");
        assert_eq!(reply["ok"], true);
        drop(commands);
        worker.join().unwrap();
        server.join().unwrap();
        std::fs::remove_file(path).unwrap();
    }
}
