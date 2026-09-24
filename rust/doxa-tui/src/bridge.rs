//! Connect one daemon socket to the terminal loop without blocking input.

use std::io;
use std::path::Path;
use std::sync::mpsc::{self, Receiver, SyncSender, TryRecvError};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::transport::{DaemonClient, TransportError};
use crate::ui;

pub enum WorkerCommand {
    Prompt(String, String),
    Answer(String, String, Value),
}

/// Attach to a running Python DOXA daemon. The Rust TUI currently hosts one
/// session per process; additional session discovery belongs to later 2.0 work.
pub fn run_socket(path: impl AsRef<Path>) -> io::Result<()> {
    let client = DaemonClient::connect(path, None).map_err(as_io_error)?;
    let (frames, prompts, worker) = spawn_worker(client);
    let result = ui::run_with_channels(frames, prompts);
    // Dropping the UI's channel sender on return tells the reader thread to
    // stop after its current bounded socket poll or prompt acknowledgement.
    let _ = worker.join();
    result
}

fn as_io_error(error: TransportError) -> io::Error {
    io::Error::other(error)
}

fn spawn_worker(
    client: DaemonClient,
) -> (Receiver<Value>, SyncSender<WorkerCommand>, JoinHandle<()>) {
    let (frame_tx, frame_rx) = mpsc::sync_channel(128);
    let (prompt_tx, prompt_rx) = mpsc::sync_channel(32);
    let worker = thread::spawn(move || worker_loop(client, frame_tx, prompt_rx));
    (frame_rx, prompt_tx, worker)
}

fn worker_loop(
    mut client: DaemonClient,
    frames: SyncSender<Value>,
    prompts: Receiver<WorkerCommand>,
) {
    let session_id = client.hello["session_id"]
        .as_str()
        .unwrap_or_default()
        .to_owned();
    if frames.send(client.hello.clone()).is_err() {
        return;
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
                            .send(json!({"type":"prompt_rejected", "text":text,
                            "message":"Prompt target is not attached"}))
                            .is_err()
                        {
                            return;
                        }
                        continue;
                    }
                    match client.prompt(&text) {
                        Ok(reply) => {
                            let frame = if reply["ok"] == false {
                                json!({"type":"prompt_rejected", "text":text,
                                    "message":reply["error"].as_str().unwrap_or("Prompt refused")})
                            } else {
                                reply
                            };
                            if frames.send(frame).is_err() {
                                return;
                            }
                        }
                        Err(error) => {
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
                                frames.send(json!({"type":kind, "text":text, "message":message}));
                            if matches!(error, TransportError::Closed) {
                                return;
                            }
                        }
                    }
                }
                Err(TryRecvError::Empty) => break,
                Err(TryRecvError::Disconnected) => return,
            }
        }
        match client.poll_frame(Duration::from_millis(50)) {
            Ok(Some(mut frame)) => {
                if frame["type"] == "event" {
                    frame["session_id"] = Value::String(session_id.clone());
                }
                if frames.send(frame).is_err() {
                    return;
                }
            }
            Ok(None) => {}
            Err(error) => {
                let message = match error {
                    TransportError::Closed => "Daemon disconnected",
                    TransportError::FrameTooLarge => "Daemon sent an oversized frame",
                    _ => "Daemon connection failed",
                };
                let _ = frames.send(json!({"type":"client_notice", "message":message}));
                return;
            }
        }
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
