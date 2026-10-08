use doxa_runtime::{Daemon, Host, Session, MAX_FRAME_BYTES};
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::{mpsc, Arc, Condvar, Mutex};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::{Duration, Instant};

struct CapabilityProbe(Mutex<Option<Arc<dyn Fn() + Send + Sync>>>);
impl CapabilityProbe {
    fn check(&self) {
        if let Some(probe) = self.0.lock().unwrap().as_ref() { probe(); }
    }
}
impl Host for CapabilityProbe {
    fn prompt(&self, _: &str, _: &mut dyn FnMut(Value)) {}
    fn call(&self, _: &str, _: &Value) -> Result<Value, String> { Ok(json!({})) }
    fn transcript_snapshot(&self) -> std::io::Result<Option<(std::path::PathBuf, u64)>> {
        self.check();
        Ok(None)
    }
    fn can_set_model(&self) -> bool { self.check(); true }
    fn initial_effort(&self) -> Option<String> { self.check(); Some("high".into()) }
    fn can_set_permission_mode(&self) -> bool { self.check(); true }
    fn lore_scrub_status(&self) -> Option<&'static str> { self.check(); Some("ready") }
}

#[test]
fn hello_and_status_capabilities_can_access_session_state() {
    let dir = tempfile::tempdir().unwrap();
    let host = Arc::new(CapabilityProbe(Mutex::new(None)));
    let handle = Arc::new(Daemon::bind(dir.path(), session(), host.clone()).unwrap().start());
    let probe_handle = handle.clone();
    *host.0.lock().unwrap() = Some(Arc::new(move || {
        let (tx, rx) = mpsc::channel();
        let handle = probe_handle.clone();
        std::thread::spawn(move || { tx.send(handle.attached_clients()).unwrap(); });
        assert!(rx.recv_timeout(Duration::from_secs(1)).is_ok(), "host capability ran under state lock");
    }));
    let (mut reader, mut writer) = connect(handle.socket_path());
    let hello = recv(&mut reader);
    assert_eq!(hello["type"], "hello");
    assert_eq!(hello["effort"], "high");
    assert_eq!(hello["lore_scrub"], "ready");
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"call","id":1,"method":"status","params":{}}));
    let status = recv(&mut reader)["status"].clone();
    assert_eq!(status["can_set_model"], true);
    assert_eq!(status["effort"], "high");
    assert_eq!(status["lore_scrub"], "ready");
    *host.0.lock().unwrap() = None;
}

struct PanicControl(AtomicUsize);
struct EffortControl(Mutex<String>);
impl Host for EffortControl {
    fn initial_effort(&self) -> Option<String> { Some(self.0.lock().unwrap().clone()) }
    fn prompt(&self, _: &str, emit: &mut dyn FnMut(Value)) {
        emit(json!({"type":"turn_done","data":{}}));
    }
    fn call(&self, method: &str, params: &Value) -> Result<Value, String> {
        if method != "set_effort" { return Err("unknown method".into()); }
        let effort = params["effort"].as_str().ok_or("missing effort")?;
        if !matches!(effort, "low" | "high") { return Err("unsupported effort".into()); }
        *self.0.lock().unwrap() = effort.into();
        Ok(json!({"effort":effort}))
    }
}

#[test]
fn effort_change_is_acknowledged_and_reflected_in_status_and_event() {
    let dir = tempfile::tempdir().unwrap();
    let handle = Daemon::bind(dir.path(), session(), Arc::new(EffortControl(Mutex::new("high".into())))).unwrap().start();
    let (mut reader, mut writer) = connect(handle.socket_path());
    assert_eq!(recv(&mut reader)["effort"], "high");
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"call","id":1,"method":"set_effort","params":{"effort":"bad"}}));
    assert_eq!(recv(&mut reader)["ok"], false);
    send(&mut writer, json!({"type":"call","id":2,"method":"set_effort","params":{"effort":"low"}}));
    assert_eq!(recv(&mut reader)["effort"], "low");
    let event = recv(&mut reader);
    assert_eq!(event["event"]["type"], "effort_changed");
    assert_eq!(event["event"]["data"]["effort"], "low");
    send(&mut writer, json!({"type":"call","id":3,"method":"status","params":{}}));
    assert_eq!(recv(&mut reader)["status"]["effort"], "low");
}

#[test]
fn effort_change_refuses_running_or_queued_turns() {
    struct GatedEffort { ready: (Mutex<bool>, Condvar), calls: AtomicUsize }
    impl Host for GatedEffort {
        fn prompt(&self, _: &str, emit: &mut dyn FnMut(Value)) {
            let mut ready = self.ready.0.lock().unwrap();
            while !*ready { ready = self.ready.1.wait(ready).unwrap(); }
            emit(json!({"type":"turn_done","data":{}}));
        }
        fn call(&self, method: &str, _: &Value) -> Result<Value, String> {
            if method != "set_effort" { return Err("unknown method".into()); }
            self.calls.fetch_add(1, Ordering::SeqCst);
            Ok(json!({"effort":"low"}))
        }
    }
    let host = Arc::new(GatedEffort { ready: (Mutex::new(false), Condvar::new()),
        calls: AtomicUsize::new(0) });
    let dir = tempfile::tempdir().unwrap();
    let handle = Daemon::bind(dir.path(), session(), host.clone()).unwrap().start();
    let (mut reader, mut writer) = connect(handle.socket_path());
    recv(&mut reader);
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"prompt","id":1,"text":"running"}));
    while recv(&mut reader)["id"] != 1 {}
    send(&mut writer, json!({"type":"prompt","id":2,"text":"queued"}));
    while recv(&mut reader)["id"] != 2 {}
    send(&mut writer, json!({"type":"call","id":3,"method":"set_effort","params":{"effort":"low"}}));
    let reply = loop { let frame = recv(&mut reader); if frame["id"] == 3 { break frame; } };
    assert_eq!(reply["ok"], false);
    assert!(reply["error"].as_str().unwrap().contains("idle"));
    assert_eq!(host.calls.load(Ordering::SeqCst), 0);
    *host.ready.0.lock().unwrap() = true;
    host.ready.1.notify_all();
}

impl Host for PanicControl {
    fn prompt(&self, _: &str, _: &mut dyn FnMut(Value)) {}
    fn call(&self, method: &str, _: &Value) -> Result<Value, String> {
        if method == "set_model" {
            if self.0.fetch_add(1, Ordering::SeqCst) == 0 { panic!("control host panicked"); }
            return Ok(json!({"model":"recovered"}));
        }
        Err("unknown method".into())
    }
}

#[test]
fn panicking_control_returns_error_and_next_control_succeeds() {
    let dir = tempfile::tempdir().unwrap();
    let handle = Daemon::bind(dir.path(), session(), Arc::new(PanicControl(AtomicUsize::new(0)))).unwrap().start();
    let (mut reader, mut writer) = connect(handle.socket_path());
    recv(&mut reader);
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"call","id":1,"method":"set_model","params":{}}));
    assert_eq!(recv(&mut reader)["error"], "host panicked");
    send(&mut writer, json!({"type":"call","id":2,"method":"set_model","params":{}}));
    assert_eq!(recv(&mut reader)["model"], "recovered");
    assert_eq!(recv(&mut reader)["event"]["type"], "model_changed");
}

struct PanicPublicPrompt(AtomicUsize);
impl Host for PanicPublicPrompt {
    fn prompt(&self, _: &str, emit: &mut dyn FnMut(Value)) { emit(json!({"type":"turn_done","data":{}})); }
    fn call(&self, _: &str, _: &Value) -> Result<Value, String> { Ok(json!({})) }
    fn public_prompt(&self, text: &str) -> Result<String, String> {
        if self.0.fetch_add(1, Ordering::SeqCst) == 0 { panic!("scrubber panicked"); }
        Ok(text.into())
    }
}

#[test]
fn panicking_immediate_scrubber_rejects_prompt_but_keeps_client_alive() {
    let dir = tempfile::tempdir().unwrap();
    let handle = Daemon::bind(dir.path(), session(), Arc::new(PanicPublicPrompt(AtomicUsize::new(0)))).unwrap().start();
    let (mut reader, mut writer) = connect(handle.socket_path());
    recv(&mut reader);
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"prompt","id":1,"text":"first"}));
    assert_eq!(recv(&mut reader)["error"], "prompt could not be scrubbed");
    send(&mut writer, json!({"type":"prompt","id":2,"text":"second"}));
    assert_eq!(recv(&mut reader)["ok"], true);
    assert_eq!(recv(&mut reader)["event"]["type"], "turn_done");
}

struct Fixture { gate: (Mutex<bool>, Condvar), prompts: Mutex<Vec<String>> }
impl Fixture {
    fn new() -> Self { Self { gate: (Mutex::new(false), Condvar::new()), prompts: Mutex::new(vec![]) } }
    fn release(&self) { *self.gate.0.lock().unwrap() = true; self.gate.1.notify_all(); }
}
struct BranchGate { gate: (Mutex<bool>, Condvar), calls: AtomicUsize }
impl Host for BranchGate {
    fn prompt(&self, _: &str, emit: &mut dyn FnMut(Value)) {
        let mut ready = self.gate.0.lock().unwrap();
        while !*ready { ready = self.gate.1.wait(ready).unwrap(); }
        emit(json!({"type":"turn_done","data":{}}));
    }
    fn call(&self, method: &str, _: &Value) -> Result<Value, String> {
        if method != "switch_branch" { return Err("unexpected call".into()); }
        self.calls.fetch_add(1, Ordering::SeqCst);
        Ok(json!({"base":"feature"}))
    }
}

#[test]
fn branch_switch_waits_for_idle_and_empty_prompt_queue() {
    let dir = tempfile::tempdir().unwrap();
    let host = Arc::new(BranchGate { gate: (Mutex::new(false), Condvar::new()), calls: AtomicUsize::new(0) });
    let handle = Daemon::bind(dir.path(), session(), host.clone()).unwrap().start();
    let (mut reader, mut writer) = connect(handle.socket_path());
    recv(&mut reader);
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"prompt","id":1,"text":"running"}));
    assert_eq!(recv(&mut reader)["ok"], true);
    send(&mut writer, json!({"type":"prompt","id":2,"text":"queued"}));
    // Events may precede the second prompt acknowledgement.
    while recv(&mut reader)["id"] != 2 {}
    send(&mut writer, json!({"type":"call","id":3,"method":"switch_branch","params":{"name":"feature"}}));
    let refused = loop { let frame = recv(&mut reader); if frame["id"] == 3 { break frame; } };
    assert_eq!(refused["ok"], false);
    assert!(refused["error"].as_str().unwrap().contains("idle"));
    assert_eq!(host.calls.load(Ordering::SeqCst), 0);
    *host.gate.0.lock().unwrap() = true;
    host.gate.1.notify_all();
    // Wait for both turns to drain before asking again.
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        send(&mut writer, json!({"type":"call","id":4,"method":"status","params":{}}));
        let status = loop { let frame = recv(&mut reader); if frame["id"] == 4 { break frame; } };
        if status["status"]["running"] == false && status["status"]["queued"] == 0 { break; }
        assert!(Instant::now() < deadline);
    }
    send(&mut writer, json!({"type":"call","id":5,"method":"switch_branch","params":{"name":"feature"}}));
    let accepted = loop { let frame = recv(&mut reader); if frame["id"] == 5 { break frame; } };
    assert_eq!(accepted["ok"], true);
    assert_eq!(host.calls.load(Ordering::SeqCst), 1);
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
    // Attach admission shares the control lock. Confirm both clients have
    // attached before testing status while a provider control call is blocked.
    for writer in [&mut control_writer, &mut status_writer] {
        send(writer, json!({"type":"call","id":0,"method":"get_state","params":{}}));
    }
    for reader in [&mut control_reader, &mut status_reader] {
        let attached = recv(reader);
        assert_eq!(attached["type"], "reply");
        assert_eq!(attached["id"], 0);
        assert_eq!(attached["ok"], true);
    }
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
fn prompt_reply_precedes_queue_event_for_every_client() {
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
    let local = recv(&mut a);
    assert_eq!(local["event"]["type"], "prompt_queued");
    assert_eq!(local["event"]["data"]["text"], "second");
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

struct QueueFixture { permits: (Mutex<usize>, Condvar), prompts: Mutex<Vec<String>> }
impl QueueFixture {
    fn new() -> Self { Self { permits: (Mutex::new(0), Condvar::new()), prompts: Mutex::new(Vec::new()) } }
    fn release_one(&self) { *self.permits.0.lock().unwrap() += 1; self.permits.1.notify_all(); }
}
impl Host for QueueFixture {
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        self.prompts.lock().unwrap().push(text.to_owned());
        let mut permits = self.permits.0.lock().unwrap();
        while *permits == 0 { permits = self.permits.1.wait(permits).unwrap(); }
        *permits -= 1;
        emit(json!({"type":"turn_done","data":{}}));
    }
    fn call(&self, _: &str, _: &Value) -> Result<Value, String> { Err("unknown method".into()) }
    fn public_prompt(&self, text: &str) -> Result<String, String> {
        Ok(text.replace("secret", "[redacted]"))
    }
}

#[test]
fn queue_rpc_lists_scrubbed_fifo_and_cancels_exactly_once() {
    let dir = tempfile::tempdir().unwrap();
    let host = Arc::new(QueueFixture::new());
    let handle = Daemon::bind(dir.path(), session(), host.clone()).unwrap().start();
    let (mut a, mut aw) = connect(handle.socket_path());
    let (mut b, mut bw) = connect(handle.socket_path());
    recv(&mut a); recv(&mut b);
    send(&mut aw, json!({"type":"attach","cursor":null}));
    send(&mut bw, json!({"type":"attach","cursor":null}));
    send(&mut bw, json!({"type":"call","id":1,"method":"status"}));
    assert_eq!(recv(&mut b)["id"], 1);
    send(&mut aw, json!({"type":"prompt","id":1,"text":"first secret"}));
    assert_eq!(recv(&mut a)["ok"], true);
    for (request, id, text) in [(2, "q1", "second secret"), (3, "q2", "third secret")] {
        send(&mut aw, json!({"type":"prompt","id":request,"text":text}));
        assert_eq!(recv(&mut a)["queue_id"], id);
        let local = recv(&mut a);
        assert_eq!(local["event"]["type"], "prompt_queued");
        assert!(!local.to_string().contains("secret"));
        let broadcast = recv(&mut b);
        assert_eq!(broadcast["event"]["type"], "prompt_queued");
        assert!(!broadcast.to_string().contains("secret"));
    }
    send(&mut aw, json!({"type":"call","id":4,"method":"queue"}));
    let listed = recv(&mut a);
    assert_eq!(listed["queue"], json!([
        {"id":"q1","text":"second [redacted]"},
        {"id":"q2","text":"third [redacted]"}
    ]));
    send(&mut aw, json!({"type":"call","id":5,"method":"cancel_queued","params":{"id":"q1","position":1}}));
    assert_eq!(recv(&mut a)["ok"], true);
    let cancelled = recv(&mut a);
    assert_eq!(cancelled["event"]["type"], "prompt_cancelled");
    assert_eq!(cancelled["event"]["data"]["id"], "q1");
    assert!(!cancelled.to_string().contains("secret"));
    assert_eq!(recv(&mut b)["event"]["data"]["id"], "q1");
    send(&mut aw, json!({"type":"call","id":6,"method":"queue"}));
    assert_eq!(recv(&mut a)["queue"], json!([{"id":"q2","text":"third [redacted]"}]));
    send(&mut aw, json!({"type":"call","id":7,"method":"cancel_queued","params":{"id":"q1"}}));
    assert_eq!(recv(&mut a)["ok"], false);
    send(&mut aw, json!({"type":"call","id":8,"method":"cancel_queued","params":{"id":"q"}}));
    assert_eq!(recv(&mut a)["ok"], false, "prefix must not cancel an unintended prompt");
    send(&mut aw, json!({"type":"call","id":9,"method":"cancel_queued","params":{"id":"q2","position":0}}));
    assert_eq!(recv(&mut a)["ok"], false);
    send(&mut aw, json!({"type":"call","id":12,"method":"cancel_queued","params":{"position":1}}));
    let positional = recv(&mut a);
    assert_eq!(positional["ok"], false);
    assert!(positional["error"].as_str().unwrap().contains("exact queued prompt id"));
    host.release_one();
    let mut dequeued = false;
    for _ in 0..3 {
        let frame = recv(&mut a);
        if frame["event"]["type"] == "prompt_dequeued" {
            assert_eq!(frame["event"]["data"]["id"], "q2");
            dequeued = true;
            break;
        }
    }
    assert!(dequeued);
    send(&mut aw, json!({"type":"call","id":10,"method":"queue"}));
    assert_eq!(recv(&mut a)["queue"], json!([]));
    send(&mut aw, json!({"type":"call","id":11,"method":"cancel_queued","params":{"id":"q2"}}));
    assert_eq!(recv(&mut a)["ok"], false, "already-dequeued prompt cannot be cancelled");
    host.release_one();
    for _ in 0..3 {
        if recv(&mut a)["event"]["type"] == "turn_done" { break; }
    }
    assert_eq!(*host.prompts.lock().unwrap(), vec!["first secret", "third secret"]);
}

#[test]
fn stale_position_cannot_cancel_next_item_after_dequeue() {
    let dir = tempfile::tempdir().unwrap();
    let host = Arc::new(QueueFixture::new());
    let handle = Daemon::bind(dir.path(), session(), host.clone()).unwrap().start();
    let (mut reader, mut writer) = connect(handle.socket_path());
    recv(&mut reader);
    send(&mut writer, json!({"type":"attach","cursor":null}));
    for (request, text) in [(1, "running"), (2, "queued one"), (3, "queued two")] {
        send(&mut writer, json!({"type":"prompt","id":request,"text":text}));
        assert_eq!(recv(&mut reader)["ok"], true);
        if request > 1 {
            assert_eq!(recv(&mut reader)["event"]["type"], "prompt_queued");
        }
    }
    send(&mut writer, json!({"type":"call","id":4,"method":"queue"}));
    assert_eq!(recv(&mut reader)["queue"], json!([
        {"id":"q1","text":"queued one"}, {"id":"q2","text":"queued two"}
    ]));
    host.release_one();
    let mut dequeued = false;
    for _ in 0..3 {
        if recv(&mut reader)["event"]["type"] == "prompt_dequeued" { dequeued = true; break; }
    }
    assert!(dequeued);
    // Position 1 now names q2. The old listing's q1 must not cancel q2.
    send(&mut writer, json!({"type":"call","id":5,"method":"cancel_queued",
        "params":{"id":"q1","position":1}}));
    let stale = recv(&mut reader);
    assert_eq!(stale["ok"], false);
    assert!(stale["error"].as_str().unwrap().contains("queue changed"));
    send(&mut writer, json!({"type":"call","id":6,"method":"queue"}));
    assert_eq!(recv(&mut reader)["queue"], json!([{"id":"q2","text":"queued two"}]));
    send(&mut writer, json!({"type":"call","id":7,"method":"cancel_queued","params":{"id":"q2"}}));
    assert_eq!(recv(&mut reader)["ok"], true);
    assert_eq!(recv(&mut reader)["event"]["data"]["id"], "q2");
    host.release_one();
    assert_eq!(recv(&mut reader)["event"]["type"], "turn_done");
    assert_eq!(*host.prompts.lock().unwrap(), vec!["running", "queued one"]);
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
    let mut last = Value::Null;
    for expected in 9..=520 {
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        assert!(!line.is_empty(), "replay socket closed before sequence {expected}");
        let frame: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(frame["seq"], expected, "replay sequence changed");
        last = frame;
    }
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

#[test]
fn effort_resume_request_does_not_claim_verified_current_setting() {
    struct ResumeEffort;
    impl Host for ResumeEffort {
        fn initial_effort(&self) -> Option<String> { Some("low".into()) }
        fn call(&self, _: &str, _: &Value) -> Result<Value, String> {
            Ok(json!({"effort":"high","verification_pending":true}))
        }
        fn prompt(&self, _: &str, emit: &mut dyn FnMut(Value)) {
            emit(json!({"type":"effort_verified","data":{"effort":"high"}}));
            emit(json!({"type":"model_changed","data":{"model":"verified-model"}}));
            emit(json!({"type":"turn_done","data":{}}));
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let handle = Daemon::bind(dir.path(), session(), Arc::new(ResumeEffort)).unwrap().start();
    let (mut reader, mut writer) = connect(handle.socket_path()); recv(&mut reader);
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"call","id":1,"method":"set_effort","params":{"effort":"high"}}));
    assert_eq!(recv(&mut reader)["verification_pending"], true);
    assert_eq!(recv(&mut reader)["event"]["type"], "effort_requested");
    send(&mut writer, json!({"type":"call","id":2,"method":"status","params":{}}));
    let status = recv(&mut reader)["status"].clone();
    assert_eq!(status["effort"], "low"); assert_eq!(status["pending_effort"], "high");
    send(&mut writer, json!({"type":"prompt","id":3,"text":"verify"}));
    while recv(&mut reader)["event"]["type"] != "turn_done" {}
    send(&mut writer, json!({"type":"call","id":4,"method":"status","params":{}}));
    let status = loop { let frame = recv(&mut reader); if frame["id"] == 4 { break frame["status"].clone(); } };
    assert_eq!(status["effort"], "high"); assert!(status["pending_effort"].is_null());
    assert_eq!(status["model"], "verified-model");
}

#[test]
fn pending_input_snapshots_survive_replay_overflow_and_require_exact_review() {
    struct Answers(AtomicUsize);
    impl Host for Answers {
        fn prompt(&self, _: &str, _: &mut dyn FnMut(Value)) {}
        fn call(&self, method: &str, _: &Value) -> Result<Value, String> {
            assert_eq!(method, "answer_needs_input");
            self.0.fetch_add(1, Ordering::SeqCst); Ok(json!({}))
        }
    }
    let dir = tempfile::tempdir().unwrap();
    let host = Arc::new(Answers(AtomicUsize::new(0)));
    let handle = Daemon::bind(dir.path(), session(), host.clone()).unwrap().start();
    let request = json!({"id":"opaque-1","kind":"permission","input_summary":"echo reviewed"});
    handle.publish(json!({"type":"needs_input","data":request}));
    for _ in 0..520 {handle.publish(json!({"type":"notice","data":{}}));}
    let (mut reader, mut writer) = connect(handle.socket_path());
    let hello = recv(&mut reader);
    assert_eq!(hello["pending_inputs"], json!([request]));
    send(&mut writer, json!({"type":"attach","cursor":hello["next_seq"]}));
    send(&mut writer, json!({"type":"call","id":1,"method":"get_state","params":{}}));
    let state = recv(&mut reader);
    assert_eq!(state["pending_inputs"], json!([request]));
    assert_eq!(state["pending_inputs_complete"], true);
    send(&mut writer, json!({"type":"call","id":2,"method":"answer_needs_input","params":{"id":"opaque-1","answer":{"decision":"allow"},"reviewed_request":{"id":"opaque-1","input_summary":"different"}}}));
    assert_eq!(recv(&mut reader)["ok"], false);
    assert_eq!(host.0.load(Ordering::SeqCst), 0);
    send(&mut writer, json!({"type":"call","id":3,"method":"answer_needs_input","params":{"id":"opaque-1","answer":{"decision":"deny"},"reviewed_request":request}}));
    assert_eq!(recv(&mut reader)["ok"], true);
    assert_eq!(host.0.load(Ordering::SeqCst), 1);
    handle.publish(json!({"type":"needs_input_resolved","data":{"id":"opaque-1"}}));
    assert_eq!(recv(&mut reader)["event"]["type"], "needs_input_resolved");
    send(&mut writer, json!({"type":"call","id":4,"method":"answer_needs_input","params":{"id":"opaque-1","answer":{"decision":"allow"},"reviewed_request":request}}));
    assert_eq!(recv(&mut reader)["ok"], false);
    assert_eq!(host.0.load(Ordering::SeqCst), 1);
}

struct LingerWork {
    permits: Mutex<usize>,
    released: Condvar,
    entered: AtomicUsize,
    provider_work: AtomicBool,
}
impl LingerWork {
    fn new() -> Self { Self { permits: Mutex::new(0), released: Condvar::new(), entered: AtomicUsize::new(0), provider_work: AtomicBool::new(false) } }
    fn release(&self, count: usize) { *self.permits.lock().unwrap() += count; self.released.notify_all(); }
}
impl Host for LingerWork {
    fn has_active_work(&self) -> bool { self.provider_work.load(Ordering::Acquire) }
    fn prompt(&self, _: &str, emit: &mut dyn FnMut(Value)) {
        self.entered.fetch_add(1, Ordering::Release);
        let mut permits = self.permits.lock().unwrap();
        while *permits == 0 { permits = self.released.wait(permits).unwrap(); }
        *permits -= 1;
        emit(json!({"type":"turn_done","data":{}}));
    }
    fn call(&self, _: &str, _: &Value) -> Result<Value, String> { Ok(json!({})) }
}
fn linger_wait(mut condition: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(2);
    while !condition() { assert!(Instant::now() < deadline, "linger fixture state did not settle"); std::thread::sleep(Duration::from_millis(2)); }
}
fn linger_reply(reader: &mut BufReader<UnixStream>, id: u64) -> Value {
    loop { let frame = recv(reader); if frame["type"] == "reply" && frame["id"] == id { return frame; } }
}

#[test]
fn detached_linger_preserves_running_and_queued_socket_work_then_closes_admission() {
    let dir = tempfile::tempdir().unwrap();
    let work = Arc::new(LingerWork::new());
    let mut handle = Daemon::bind(dir.path(), session(), work.clone()).unwrap().start();
    let (mut reader, mut writer) = connect(handle.socket_path()); recv(&mut reader);
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"prompt","id":1,"text":"first"}));
    assert_eq!(linger_reply(&mut reader, 1)["ok"], true);
    linger_wait(|| work.entered.load(Ordering::Acquire) == 1);
    send(&mut writer, json!({"type":"prompt","id":2,"text":"queued"}));
    assert_eq!(linger_reply(&mut reader, 2)["queued"], true);
    writer.shutdown(std::net::Shutdown::Both).unwrap(); drop(reader); drop(writer);
    linger_wait(|| handle.attached_clients() == 0);
    assert!(handle.has_active_work()); assert!(!handle.expire_if_detached_idle());
    work.release(1); linger_wait(|| work.entered.load(Ordering::Acquire) == 2);
    assert!(!handle.expire_if_detached_idle(), "queued work became the next running turn");
    work.release(1); linger_wait(|| !handle.has_active_work());
    assert!(handle.expire_if_detached_idle()); assert!(handle.is_stopping());
    assert!(handle.enqueue_peer_prompt("late peer work".into(), "peer-exact").is_err());
    handle.shutdown();
}

#[test]
fn detached_linger_refuses_hidden_provider_work_and_reattached_clients() {
    let dir = tempfile::tempdir().unwrap(); let work = Arc::new(LingerWork::new());
    work.provider_work.store(true, Ordering::Release);
    let mut handle = Daemon::bind(dir.path(), session(), work.clone()).unwrap().start();
    assert!(handle.has_active_work()); assert!(!handle.expire_if_detached_idle());
    let (mut reader, mut writer) = connect(handle.socket_path()); recv(&mut reader);
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"call","id":1,"method":"stop_if_idle","params":{}}));
    assert_eq!(linger_reply(&mut reader, 1)["ok"], false);
    work.provider_work.store(false, Ordering::Release);
    assert!(!handle.expire_if_detached_idle(), "reattached client owns the session");
    writer.shutdown(std::net::Shutdown::Both).unwrap(); drop(reader); drop(writer);
    linger_wait(|| handle.attached_clients() == 0);
    assert!(handle.expire_if_detached_idle()); handle.shutdown();
}

#[test]
fn detached_linger_and_peer_admission_have_one_atomic_winner() {
    for _ in 0..16 {
        let dir = tempfile::tempdir().unwrap(); let work = Arc::new(LingerWork::new());
        let mut handle = Arc::new(Daemon::bind(dir.path(), session(), work.clone()).unwrap().start());
        let barrier = Arc::new(std::sync::Barrier::new(3));
        let expiring = handle.clone(); let expiry_barrier = barrier.clone();
        let expiry = std::thread::spawn(move || { expiry_barrier.wait(); expiring.expire_if_detached_idle() });
        let admitting = handle.clone(); let admission_barrier = barrier.clone();
        let admission = std::thread::spawn(move || { admission_barrier.wait(); admitting.enqueue_peer_prompt("fixture work".into(), "peer-exact") });
        barrier.wait(); let expired = expiry.join().unwrap(); let admitted = admission.join().unwrap();
        assert_eq!(admitted.is_ok(), !expired, "expiration and provider admission both won");
        if !expired { linger_wait(|| work.entered.load(Ordering::Acquire) == 1); work.release(1); linger_wait(|| !handle.has_active_work()); }
        Arc::get_mut(&mut handle).unwrap().shutdown();
    }
}

#[test]
fn detached_linger_keeps_slow_provider_controls_without_blocking_the_clock() {
    struct Maintenance(LingerWork);
    impl Host for Maintenance {
        fn prompt(&self, _: &str, _: &mut dyn FnMut(Value)) {}
        fn call(&self, method: &str, params: &Value) -> Result<Value, String> {
            assert_eq!(method, "set_effort");
            self.0.entered.fetch_add(1, Ordering::Release);
            let mut permits = self.0.permits.lock().unwrap();
            while *permits == 0 { permits = self.0.released.wait(permits).unwrap(); }
            Ok(json!({"effort":params["effort"]}))
        }
    }
    let dir = tempfile::tempdir().unwrap(); let work = Arc::new(Maintenance(LingerWork::new()));
    let mut handle = Daemon::bind(dir.path(), session(), work.clone()).unwrap().start();
    let (mut reader, mut writer) = connect(handle.socket_path()); recv(&mut reader);
    send(&mut writer, json!({"type":"attach","cursor":null}));
    send(&mut writer, json!({"type":"call","id":1,"method":"set_effort","params":{"effort":"high"}}));
    linger_wait(|| work.0.entered.load(Ordering::Acquire) == 1);
    writer.shutdown(std::net::Shutdown::Both).unwrap(); drop(reader); drop(writer);
    // Publishing removes the failed attached writer while its RPC is still
    // waiting inside the host, so attachment alone cannot protect this work.
    linger_wait(|| { handle.publish(json!({"type":"notice","data":{}})); handle.attached_clients() == 0 });
    let started = Instant::now();
    assert!(handle.has_active_work()); assert!(!handle.expire_if_detached_idle());
    assert!(started.elapsed() < Duration::from_millis(100), "provider control blocked the linger clock");
    work.0.release(1); linger_wait(|| !handle.has_active_work());
    assert!(handle.expire_if_detached_idle()); handle.shutdown();
}
