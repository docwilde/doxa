use doxa_runtime::{Daemon, Host, Session, MAX_FRAME_BYTES};
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::process::Command;
use std::sync::{Arc, Condvar, Mutex};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::{Duration, Instant};

struct Fixture { gate: (Mutex<bool>, Condvar), prompts: Mutex<Vec<String>> }
impl Fixture {
    fn new() -> Self { Self { gate: (Mutex::new(false), Condvar::new()), prompts: Mutex::new(vec![]) } }
    fn release(&self) { *self.gate.0.lock().unwrap() = true; self.gate.1.notify_all(); }
}
impl Host for Fixture {
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        self.prompts.lock().unwrap().push(text.to_owned());
        let mut ready = self.gate.0.lock().unwrap();
        while !*ready { ready = self.gate.1.wait(ready).unwrap(); }
        drop(ready);
        emit(json!({"type":"text_delta","data":{"text":text}}));
        emit(json!({"type":"turn_done","data":{}}));
    }
    fn call(&self, method: &str, _: &Value) -> Result<Value, String> {
        if method == "fixture" { Ok(json!({"answer":42})) }
        else if method == "stop" { Ok(json!({})) }
        else { Err("unknown method".into()) }
    }
}
fn session() -> Session { Session { session_id:"test-session".into(), cwd:"/tmp".into(), model:None,
    engine:"fixture".into(), doxa_version:"2.0.0-alpha.5".into() } }
fn connect(path: &Path) -> (BufReader<UnixStream>, UnixStream) {
    let stream = UnixStream::connect(path).unwrap();
    stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    let writer = stream.try_clone().unwrap();
    (BufReader::new(stream), writer)
}
fn recv(reader: &mut BufReader<UnixStream>) -> Value {
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    assert!(!line.is_empty(), "socket closed unexpectedly");
    serde_json::from_str(&line).unwrap()
}
fn send(writer: &mut UnixStream, frame: Value) {
    writer.write_all(serde_json::to_string(&frame).unwrap().as_bytes()).unwrap();
    writer.write_all(b"\n").unwrap();
}

struct BlockingControl {
    entered: AtomicBool,
    gate: (Mutex<bool>, Condvar),
}

impl Host for BlockingControl {
    fn prompt(&self, _: &str, emit: &mut dyn FnMut(Value)) {
        emit(json!({"type":"turn_done","data":{}}));
    }
    fn call(&self, method: &str, _: &Value) -> Result<Value, String> {
        if method != "set_model" { return Err("unknown method".into()); }
        self.entered.store(true, Ordering::Release);
        let mut ready = self.gate.0.lock().unwrap();
        while !*ready { ready = self.gate.1.wait(ready).unwrap(); }
        Ok(json!({"model":"test-model"}))
    }
}

#[test]
fn slow_model_control_does_not_block_other_clients_status() {
    let dir = tempfile::tempdir().unwrap();
    let host = Arc::new(BlockingControl { entered: AtomicBool::new(false), gate: (Mutex::new(false), Condvar::new()) });
    let handle = Daemon::bind(dir.path(), session(), host.clone()).unwrap().start();
    let (mut control_reader, mut control_writer) = connect(handle.socket_path());
    let (mut status_reader, mut status_writer) = connect(handle.socket_path());
    recv(&mut control_reader);
    recv(&mut status_reader);
    send(&mut control_writer, json!({"type":"attach","cursor":null}));
    send(&mut status_writer, json!({"type":"attach","cursor":null}));
    send(&mut control_writer, json!({"type":"call","id":1,"method":"set_model","params":{"model":"test-model"}}));
    let deadline = Instant::now() + Duration::from_secs(5);
    while !host.entered.load(Ordering::Acquire) && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(host.entered.load(Ordering::Acquire));
    send(&mut status_writer, json!({"type":"call","id":2,"method":"status","params":{}}));
    let mut line = String::new();
    let status = status_reader.read_line(&mut line);
    *host.gate.0.lock().unwrap() = true;
    host.gate.1.notify_all();
    assert!(status.is_ok() && !line.is_empty(), "status blocked behind slow control RPC: {status:?}");
    let frame: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(frame["status"]["model"], Value::Null);
    assert_eq!(recv(&mut control_reader)["model"], "test-model");
}

struct ControlReplies(Mutex<VecDeque<Value>>);

impl Host for ControlReplies {
    fn prompt(&self, _: &str, _: &mut dyn FnMut(Value)) {}
    fn call(&self, _: &str, _: &Value) -> Result<Value, String> {
        Ok(self.0.lock().unwrap().pop_front().unwrap())
    }
}

#[test]
fn malformed_control_replies_do_not_report_success_or_change_status() {
    let dir = tempfile::tempdir().unwrap();
    let replies = vec![
        json!({}), json!({"model":null}), json!({"model":42}),
        json!({"model":""}), json!({"model":"bad\nmodel"}), json!([]),
        json!({}), json!({"mode":null}), json!({"mode":42}),
        json!({"mode":""}), json!({"mode":"unrecognized"}), json!([]),
        json!({"mode":"dontAsk"}),
        json!({"model":"test-model"}), json!({"mode":"plan"}),
    ];
    let host = Arc::new(ControlReplies(Mutex::new(replies.into())));
    let handle = Daemon::bind(dir.path(), session(), host).unwrap().start();
    let (mut reader, mut writer) = connect(handle.socket_path());
    recv(&mut reader);
    send(&mut writer, json!({"type":"attach","cursor":null}));

    for id in 1..=13 {
        let method = if id <= 6 { "set_model" } else { "set_permission_mode" };
        send(&mut writer, json!({"type":"call","id":id,"method":method,
            "params":{"mode":"plan"}}));
        let reply = recv(&mut reader);
        assert_eq!(reply["id"], id);
        assert_eq!(reply["ok"], false);
        assert!(reply["error"].as_str().unwrap().contains("invalid"));
        send(&mut writer, json!({"type":"call","id":100+id,"method":"status","params":{}}));
        let status = recv(&mut reader);
        assert_eq!(status["id"], 100+id, "unexpected event after rejected control");
        assert_eq!(status["status"]["model"], Value::Null);
        assert_eq!(status["status"]["permission_mode"], "default");
    }

    for (id, method, field, selected) in [(14, "set_model", "model", "test-model"),
        (15, "set_permission_mode", "mode", "plan")] {
        send(&mut writer, json!({"type":"call","id":id,"method":method,
            "params":{"mode":"plan"}}));
        let reply = recv(&mut reader);
        assert_eq!(reply["ok"], true);
        assert_eq!(reply[field], selected);
        let event = recv(&mut reader);
        assert_eq!(event["event"]["data"][field], selected);
    }
    send(&mut writer, json!({"type":"call","id":16,"method":"status","params":{}}));
    let status = recv(&mut reader);
    assert_eq!(status["status"]["model"], "test-model");
    assert_eq!(status["status"]["permission_mode"], "plan");
}

#[test]
fn hello_replay_live_and_call_shapes() {
    let dir = tempfile::tempdir().unwrap();
    let host = Arc::new(Fixture::new());
    let handle = Daemon::bind(dir.path(), session(), host).unwrap().start();
    handle.publish(json!({"type":"text_delta","data":{"text":"old"}}));
    handle.publish(json!({"type":"text_delta","data":{"text":"retained"}}));
    let (mut reader, mut writer) = connect(handle.socket_path());
    let hello = recv(&mut reader);
    assert_eq!(hello["type"], "hello");
    assert_eq!(hello["proto"], 1);
    assert_eq!(hello["session_id"], "test-session");
    assert_eq!(hello["next_seq"], 2);
    assert_eq!(hello["engine"], "fixture");
    send(&mut writer, json!({"type":"attach","cursor":1}));
    assert_eq!(recv(&mut reader)["seq"], 1);
    handle.publish(json!({"type":"text_delta","data":{"text":"live"}}));
    assert_eq!(recv(&mut reader)["seq"], 2);
    send(&mut writer, json!({"type":"call","id":7,"method":"fixture","params":{}}));
    let reply = recv(&mut reader);
    assert_eq!(reply, json!({"type":"reply","id":7,"ok":true,"answer":42}));
    send(&mut writer, json!({"type":"call","id":8,"method":"status","params":{}}));
    assert_eq!(recv(&mut reader)["status"]["session_id"], "test-session");
}

#[test]
fn prompt_reply_precedes_events_and_queue_notifies_only_other_client() {
    let dir = tempfile::tempdir().unwrap();
    let host = Arc::new(Fixture::new());
    let handle = Daemon::bind(dir.path(), session(), host.clone()).unwrap().start();
    let (mut a, mut aw) = connect(handle.socket_path());
    let (mut b, mut bw) = connect(handle.socket_path());
    recv(&mut a); recv(&mut b);
    send(&mut aw, json!({"type":"attach","cursor":null}));
    send(&mut bw, json!({"type":"attach","cursor":null}));
    send(&mut bw, json!({"type":"call","id":9,"method":"status","params":{}}));
    assert_eq!(recv(&mut b)["id"], 9);
    send(&mut aw, json!({"type":"prompt","id":1,"text":"first"}));
    let reply = recv(&mut a);
    assert_eq!(reply["ok"], true);
    let turn = reply["turn"].as_str().unwrap().to_owned();
    send(&mut aw, json!({"type":"prompt","id":2,"text":"second"}));
    let queued = recv(&mut a);
    assert_eq!(queued["queued"], true);
    assert_eq!(queued["queue_id"], "q1");
    let other = recv(&mut b);
    assert_eq!(other["event"]["type"], "prompt_queued");
    assert_eq!(other["event"]["data"]["text"], "second");
    host.release();
    let first = recv(&mut a);
    assert_eq!(first["turn"], turn);
    assert_eq!(first["event"]["data"]["text"], "first");
    let mut saw_second = false;
    for _ in 0..5 {
        let event = recv(&mut a);
        if event["event"]["type"] == "text_delta" && event["event"]["data"]["text"] == "second" { saw_second = true; break; }
    }
    assert!(saw_second);
    assert_eq!(*host.prompts.lock().unwrap(), vec!["first", "second"]);
}

struct PanicOnDequeue { fixture: Fixture, public_calls: AtomicUsize }
impl Host for PanicOnDequeue {
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) { self.fixture.prompt(text, emit); }
    fn call(&self, method: &str, params: &Value) -> Result<Value, String> { self.fixture.call(method, params) }
    fn public_prompt(&self, text: &str) -> Result<String, String> {
        if self.public_calls.fetch_add(1, Ordering::SeqCst) == 2 { panic!("scrubber panicked"); }
        Ok(text.to_owned())
    }
}

#[test]
fn queued_prompt_continues_after_display_scrubber_panics() {
    let dir = tempfile::tempdir().unwrap();
    let host = Arc::new(PanicOnDequeue { fixture: Fixture::new(), public_calls: AtomicUsize::new(0) });
    let handle = Daemon::bind(dir.path(), session(), host.clone()).unwrap().start();
    let (mut reader, mut writer) = connect(handle.socket_path());
    recv(&mut reader);
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"prompt","id":1,"text":"first"}));
    assert_eq!(recv(&mut reader)["ok"], true);
    send(&mut writer, json!({"type":"prompt","id":2,"text":"second"}));
    assert_eq!(recv(&mut reader)["queued"], true);
    host.fixture.release();
    let mut saw_redacted_dequeue = false;
    let mut saw_second_turn = false;
    for _ in 0..6 {
        let frame = recv(&mut reader);
        if frame["event"]["type"] == "prompt_dequeued" {
            assert_eq!(frame["event"]["data"]["text"], "[redacted: prompt unavailable]");
            saw_redacted_dequeue = true;
        }
        if frame["event"]["type"] == "text_delta" && frame["event"]["data"]["text"] == "second" {
            saw_second_turn = true;
            break;
        }
    }
    assert!(saw_redacted_dequeue && saw_second_turn);
    assert_eq!(*host.fixture.prompts.lock().unwrap(), ["first", "second"]);
}

#[test]
fn malformed_and_oversize_clients_do_not_affect_next_client() {
    let dir = tempfile::tempdir().unwrap();
    let handle = Daemon::bind(dir.path(), session(), Arc::new(Fixture::new())).unwrap().start();
    let (mut reader, mut writer) = connect(handle.socket_path());
    recv(&mut reader);
    writer.write_all(b"{not json}\n").unwrap();
    writer.write_all(b"[]\n").unwrap();
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"call","id":4,"method":"fixture","params":{}}));
    assert_eq!(recv(&mut reader)["answer"], 42);
    writer.write_all(&vec![b'x'; MAX_FRAME_BYTES + 1]).unwrap();
    let mut line = String::new();
    assert_eq!(reader.read_line(&mut line).unwrap(), 0);
    let (mut next, mut next_writer) = connect(handle.socket_path());
    assert_eq!(recv(&mut next)["type"], "hello");
    send(&mut next_writer, json!({"type":"attach","cursor":null}));
    send(&mut next_writer, json!({"type":"call","id":5,"method":"fixture","params":{}}));
    assert_eq!(recv(&mut next)["answer"], 42);
}

#[test]
fn owned_private_socket_collision_and_cleanup() {
    let dir = tempfile::tempdir().unwrap();
    let path;
    {
        let handle = Daemon::bind(dir.path(), session(), Arc::new(Fixture::new())).unwrap().start();
        path = handle.socket_path().to_path_buf();
        assert_eq!(fs::metadata(dir.path()).unwrap().permissions().mode() & 0o777, 0o700);
        assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        assert!(Daemon::bind(dir.path(), session(), Arc::new(Fixture::new())).is_err());
    }
    assert!(!path.exists());
    let link_dir = dir.path().join("link");
    std::os::unix::fs::symlink(dir.path(), &link_dir).unwrap();
    assert!(Daemon::bind(&link_dir, session(), Arc::new(Fixture::new())).is_err());
    let mut invalid = session(); invalid.session_id = "../escape".into();
    assert!(Daemon::bind(dir.path(), invalid, Arc::new(Fixture::new())).is_err());
}

#[test]
fn ring_evicts_old_events_and_large_event_keeps_sequence() {
    let dir = tempfile::tempdir().unwrap();
    let handle = Daemon::bind(dir.path(), session(), Arc::new(Fixture::new())).unwrap().start();
    for i in 0..520 { handle.publish(json!({"type":"text_delta","data":{"text":i.to_string()}})); }
    handle.publish(json!({"type":"tool_result","data":{"text":"x".repeat(MAX_FRAME_BYTES)}}));
    let (mut reader, mut writer) = connect(handle.socket_path());
    assert_eq!(recv(&mut reader)["next_seq"], 521);
    send(&mut writer, json!({"type":"attach","cursor":0}));
    let gap = recv(&mut reader);
    assert_eq!(gap["seq"], 8);
    assert_eq!(gap["event"], json!({"type":"replay_gap","data":{"from_seq":0,"to_seq":8}}));
    assert_eq!(recv(&mut reader)["seq"], 9);
    for _ in 0..510 { recv(&mut reader); }
    let last = recv(&mut reader);
    assert_eq!(last["seq"], 520);
    assert_eq!(last["event"]["type"], "tool_result");
    assert_eq!(last["event"]["data"]["truncated"], true);
}

#[test]
fn host_approved_stop_removes_socket() {
    let dir = tempfile::tempdir().unwrap();
    let handle = Daemon::bind(dir.path(), session(), Arc::new(Fixture::new())).unwrap().start();
    let path = handle.socket_path().to_path_buf();
    let (mut reader, mut writer) = connect(&path);
    recv(&mut reader);
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"call","id":3,"method":"stop","params":{}}));
    assert_eq!(recv(&mut reader)["ok"], true);
    for _ in 0..50 {
        if !path.exists() { return; }
        std::thread::sleep(Duration::from_millis(10));
    }
    panic!("stopped daemon left its socket behind");
}

#[test]
fn session_id_matches_python_identity_rule() {
    let dir = tempfile::tempdir().unwrap();
    for id in ["a", "9", "A-b-0", &format!("a{}", "-".repeat(127))] {
        let mut meta = session(); meta.session_id = id.into();
        let daemon = Daemon::bind(dir.path(), meta, Arc::new(Fixture::new())).unwrap();
        // A new ID may share an eight-character prefix; remove the socket
        // through the handle before checking the next case.
        drop(daemon.start());
    }
    for id in ["", "-bad", "_bad", "a_b", "a.b", "../bad", "a/b", "a\\b", "é", "aé",
        &format!("a{}", "-".repeat(128))] {
        let mut meta = session(); meta.session_id = id.into();
        assert!(Daemon::bind(dir.path(), meta, Arc::new(Fixture::new())).is_err(), "accepted {id:?}");
    }
}

#[test]
fn python_engine_client_attaches_replays_and_sends_prompt() {
    let dir = tempfile::tempdir().unwrap();
    let host = Arc::new(Fixture::new());
    host.release();
    let handle = Daemon::bind(dir.path(), session(), host).unwrap().start();
    handle.publish(json!({"type":"text_delta","data":{"text":"replayed"}}));
    let repo_root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let script = r#"
import asyncio, json, sys
from doxa.client import EngineClient

async def run():
    client = EngineClient(sys.argv[1])
    started = await client.start()
    replay = await asyncio.wait_for(anext(client.peer_events()), 5)
    events = []
    async for event in client.send('hello from Python'):
        events.append([event.type, event.data])
    status = await client.refresh_status()
    result = {'started': started.type, 'session_id': client.session_id,
              'replay': replay.data['text'], 'events': events,
              'status_session_id': status['session_id'], 'cursor': client.cursor}
    await client.finalize()
    return result

print(json.dumps(asyncio.run(asyncio.wait_for(run(), 8))))
"#;
    let python = std::env::var("DOXA_TEST_PYTHON").unwrap_or_else(|_| "python3".into());
    let output = Command::new(python).arg("-c").arg(script).arg(handle.socket_path())
        .env("PYTHONPATH", repo_root).output().unwrap();
    assert!(output.status.success(), "Python EngineClient failed: {}",
        String::from_utf8_lossy(&output.stderr));
    let result: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(result["started"], "session_started");
    assert_eq!(result["session_id"], "test-session");
    assert_eq!(result["replay"], "replayed");
    assert_eq!(result["events"][0], json!(["text_delta", {"text":"hello from Python"}]));
    assert_eq!(result["events"][1][0], "turn_done");
    assert_eq!(result["status_session_id"], "test-session");
    assert_eq!(result["cursor"], 3);
}

struct TerminalHost { mode: &'static str }
impl Host for TerminalHost {
    fn prompt(&self, _: &str, emit: &mut dyn FnMut(Value)) {
        emit(json!({"type":"text_delta","data":{"text":"body"}}));
        if self.mode == "explicit" {
            emit(json!({"type":"turn_done","data":{"explicit":true}}));
        } else if self.mode == "refused" {
            emit(json!({"type":"turn_refused","data":{"reason":"fixture"}}));
        } else if self.mode == "panic" {
            panic!("fixture failure");
        }
    }
    fn call(&self, method: &str, _: &Value) -> Result<Value, String> {
        if method == "stop" { Ok(json!(42)) } else { Err("unknown method".into()) }
    }
}

#[test]
fn every_turn_has_exactly_one_terminal_event() {
    for mode in ["implicit", "explicit", "refused", "panic"] {
        let dir = tempfile::tempdir().unwrap();
        let handle = Daemon::bind(dir.path(), session(), Arc::new(TerminalHost { mode })).unwrap().start();
        let (mut reader, mut writer) = connect(handle.socket_path());
        recv(&mut reader);
        send(&mut writer, json!({"type":"attach","cursor":null}));
        send(&mut writer, json!({"type":"prompt","id":1,"text":"hello"}));
        assert_eq!(recv(&mut reader)["ok"], true);
        assert_eq!(recv(&mut reader)["event"]["type"], "text_delta");
        let terminal = recv(&mut reader);
        assert_eq!(terminal["event"]["type"], if mode == "refused" { "turn_refused" } else { "turn_done" });
        assert_eq!(terminal["event"]["data"]["is_error"] == true, mode == "panic");
        let mut settled = false;
        for id in 2..22 {
            send(&mut writer, json!({"type":"call","id":id,"method":"status","params":{}}));
            let status = recv(&mut reader);
            assert_eq!(status["type"], "reply", "extra event after terminal in {mode}");
            if status["status"]["running"] == false { settled = true; break; }
            std::thread::sleep(Duration::from_millis(1));
        }
        assert!(settled, "turn did not settle in {mode}");
    }
}

#[test]
fn invalid_stop_reply_does_not_stop_listener() {
    let dir = tempfile::tempdir().unwrap();
    let handle = Daemon::bind(dir.path(), session(), Arc::new(TerminalHost { mode:"implicit" })).unwrap().start();
    let (mut reader, mut writer) = connect(handle.socket_path());
    recv(&mut reader);
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"call","id":1,"method":"stop","params":{}}));
    let reply = recv(&mut reader);
    assert_eq!(reply["ok"], false);
    assert!(handle.socket_path().exists());
    let (mut next, _) = connect(handle.socket_path());
    assert_eq!(recv(&mut next)["type"], "hello");
}
