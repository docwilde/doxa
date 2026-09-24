use doxa_runtime::{Daemon, Host, Session, MAX_FRAME_BYTES};
use serde_json::{json, Value};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

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
        if method == "fixture" { Ok(json!({"answer":42})) } else { Err("unknown method".into()) }
    }
}
fn session() -> Session { Session { session_id:"test-session".into(), cwd:"/tmp".into(), model:None,
    engine:"fixture".into(), doxa_version:"2.0.0-alpha.1".into() } }
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
    assert_eq!(recv(&mut reader)["seq"], 9);
    for _ in 0..510 { recv(&mut reader); }
    let last = recv(&mut reader);
    assert_eq!(last["seq"], 520);
    assert_eq!(last["event"]["type"], "tool_result");
    assert_eq!(last["event"]["data"]["truncated"], true);
}
