use super::BudgetHost;
use doxa_runtime::{Daemon, Host, Session};
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc::{self, Receiver, Sender},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

struct FakeClaude {
    active: AtomicBool,
    calls: AtomicUsize,
    queued_scrubs: AtomicUsize,
    first_started: Sender<()>,
    first_release: Mutex<Receiver<()>>,
    gap_ready: Sender<()>,
    gap_release: Mutex<Receiver<()>>,
    completed: Sender<String>,
}

impl Host for FakeClaude {
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        self.active.store(true, Ordering::Release);
        emit(json!({"type":"turn_started","data":{}}));
        if text == "first" {
            let _ = self.first_started.send(());
            self.first_release.lock().unwrap().recv_timeout(Duration::from_secs(5)).unwrap();
        }
        // Match ClaudeHost's provider-owned idle check: the model setting call
        // is refused while the provider is active, but no runtime queue is visible.
        self.active.store(false, Ordering::Release);
        emit(json!({"type":"turn_done","data":{"cost_usd":0.0}}));
        let _ = self.completed.send(text.to_owned());
    }

    fn call(&self, method: &str, params: &Value) -> Result<Value, String> {
        if method != "set_model" { return Err("unsupported fixture call".into()); }
        if self.active.load(Ordering::Acquire) { return Err("Claude setting changes require an idle session".into()); }
        self.calls.fetch_add(1, Ordering::SeqCst);
        Ok(json!({"model":params["model"]}))
    }

    fn can_set_model(&self) -> bool { true }
    fn model_change_requires_idle(&self) -> bool { true }
    fn has_active_work(&self) -> bool { self.active.load(Ordering::Acquire) }

    fn public_prompt(&self, text: &str) -> Result<String, String> {
        if text == "queued" && self.queued_scrubs.fetch_add(1, Ordering::SeqCst) == 1 {
            // First call is prompt admission. The second is runtime dequeue,
            // after the prior host call returned and while runtime.busy is true.
            let _ = self.gap_ready.send(());
            self.gap_release.lock().unwrap().recv_timeout(Duration::from_secs(5)).unwrap();
        }
        Ok(text.to_owned())
    }
}

fn read_json(reader: &mut BufReader<UnixStream>) -> Value {
    let mut line = String::new();
    reader.read_line(&mut line).expect("read runtime frame");
    assert!(!line.is_empty(), "runtime socket closed");
    serde_json::from_str(&line).expect("valid runtime JSON")
}

fn read_reply(reader: &mut BufReader<UnixStream>, id: u64) -> Value {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        assert!(Instant::now() < deadline, "timed out waiting for reply {id}");
        let value = read_json(reader);
        if value["type"] == "reply" && value["id"] == id { return value; }
    }
}

fn send(writer: &mut UnixStream, value: Value) {
    serde_json::to_writer(&mut *writer, &value).unwrap();
    writer.write_all(b"\n").unwrap();
    writer.flush().unwrap();
}

fn case(wrapped: bool) -> (Value, usize) {
    let dir = tempfile::tempdir().unwrap();
    let (first_started_tx, first_started_rx) = mpsc::channel();
    let (first_release_tx, first_release_rx) = mpsc::channel();
    let (gap_ready_tx, gap_ready_rx) = mpsc::channel();
    let (gap_release_tx, gap_release_rx) = mpsc::channel();
    let (completed_tx, completed_rx) = mpsc::channel();
    let fake = Arc::new(FakeClaude {
        active: AtomicBool::new(false), calls: AtomicUsize::new(0), queued_scrubs: AtomicUsize::new(0),
        first_started: first_started_tx, first_release: Mutex::new(first_release_rx),
        gap_ready: gap_ready_tx, gap_release: Mutex::new(gap_release_rx), completed: completed_tx,
    });
    let host: Arc<dyn Host> = if wrapped {
        Arc::new(BudgetHost::new(fake.clone(), 1.0))
    } else {
        fake.clone()
    };
    let daemon = Daemon::bind(dir.path(), Session {
        session_id: if wrapped { "budget-wrapped" } else { "budget-raw" }.into(),
        cwd: "/workspace".into(), model: Some("claude-sonnet".into()), engine: "claude".into(), doxa_version: "probe".into(),
    }, host).unwrap();
    let mut handle = daemon.start();
    let mut writer = UnixStream::connect(handle.socket_path()).unwrap();
    writer.set_write_timeout(Some(Duration::from_secs(5))).unwrap();
    let reader_stream = writer.try_clone().unwrap();
    reader_stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let mut reader = BufReader::new(reader_stream);
    assert_eq!(read_json(&mut reader)["type"], "hello");
    send(&mut writer, json!({"type":"attach","cursor":null}));
    let attach_deadline = Instant::now() + Duration::from_secs(5);
    while handle.attached_clients() != 1 {
        assert!(Instant::now() < attach_deadline, "attach not admitted");
        thread::sleep(Duration::from_millis(5));
    }

    send(&mut writer, json!({"type":"prompt","id":1,"text":"first"}));
    assert_eq!(read_reply(&mut reader, 1)["ok"], true);
    first_started_rx.recv_timeout(Duration::from_secs(5)).unwrap();
    send(&mut writer, json!({"type":"prompt","id":2,"text":"queued"}));
    let queued = read_reply(&mut reader, 2);
    assert_eq!(queued["queued"], true, "second prompt must be queued");

    first_release_tx.send(()).unwrap();
    gap_ready_rx.recv_timeout(Duration::from_secs(5)).expect("dequeue gap not reached");
    assert!(!fake.has_active_work(), "fixture must be provider-idle in the gap");
    send(&mut writer, json!({"type":"call","id":3,"method":"set_model","params":{"model":"claude-opus"}}));
    let result = read_reply(&mut reader, 3);

    let gap_calls = fake.calls.load(Ordering::SeqCst);
    gap_release_tx.send(()).unwrap();
    for _ in 0..2 { completed_rx.recv_timeout(Duration::from_secs(5)).unwrap(); }
    // The runtime publishes terminal after advancing its admission state.
    // Observe the queued terminal before checking an idle model transition.
    loop {
        let frame = read_json(&mut reader);
        if frame["event"]["type"] == "turn_done" { break; }
    }
    send(&mut writer, json!({"type":"call","id":4,"method":"set_model","params":{"model":"claude-opus"}}));
    let idle = read_reply(&mut reader, 4);
    assert_eq!(idle["ok"], true, "idle model change should succeed: {idle}");
    drop(reader);
    drop(writer);
    handle.shutdown();
    (result, gap_calls)
}

#[test]
fn unpriced_budget_preserves_queued_model_guard() {
    let (raw, raw_calls) = case(false);
    assert_eq!(raw["ok"], false, "unwrapped Host should keep runtime queue guard: {raw}");
    assert_eq!(raw_calls, 0, "refusal must happen before provider call");

    let (wrapped, wrapped_calls) = case(true);
    assert_eq!(wrapped["ok"], false, "BudgetHost must preserve runtime queue guard: {wrapped}");
    assert_eq!(wrapped_calls, 0, "wrapped refusal must precede the provider call");
}
