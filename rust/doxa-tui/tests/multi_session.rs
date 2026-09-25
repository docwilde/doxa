use doxa_tui::bridge::{connect_sessions, WorkerCommand};
use doxa_tui::discovery::Session;
use doxa_tui::ui::App;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::os::unix::fs::PermissionsExt;
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

fn accept(listener: &UnixListener, id: &str, next_seq: u64, cursor: Option<u64>) -> UnixStream {
    let (mut socket, _) = listener.accept().unwrap();
    writeln!(socket, "{}", json!({"type":"hello","proto":1,"session_id":id,
        "cwd":"/tmp","model":id,"engine":"claude","next_seq":next_seq})).unwrap();
    let mut line = String::new();
    BufReader::new(socket.try_clone().unwrap()).read_line(&mut line).unwrap();
    assert_eq!(serde_json::from_str::<Value>(&line).unwrap(), json!({"type":"attach","cursor":cursor}));
    socket
}

fn read_request(socket: &UnixStream) -> Value {
    let mut line = String::new();
    BufReader::new(socket.try_clone().unwrap()).read_line(&mut line).unwrap();
    serde_json::from_str(&line).unwrap()
}

fn until(frames: &mpsc::Receiver<Value>, pred: impl Fn(&Value) -> bool) -> Value {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        let frame = frames.recv_timeout(remaining).unwrap();
        if pred(&frame) { return frame; }
    }
}

fn session(id: &str, socket: std::path::PathBuf) -> Session {
    Session { id: id.into(), title: String::new(), socket, scope_key: "/tmp".into(), clients: None, started_at: String::new() }
}

#[test]
fn two_sockets_route_events_prompts_and_reconnect_from_each_cursor() {
    let dir = tempfile::tempdir().unwrap();
    let path_a = dir.path().join("a.sock");
    let path_b = dir.path().join("b.sock");
    let listener_a = UnixListener::bind(&path_a).unwrap();
    let listener_b = UnixListener::bind(&path_b).unwrap();
    let (done_a, wait_a) = mpsc::channel();
    let (done_b, wait_b) = mpsc::channel();
    let server_a = thread::spawn(move || {
        let mut first = accept(&listener_a, "session-a", 1, None);
        writeln!(first, "{}", json!({"type":"event","seq":1,"turn":null,
            "event":{"type":"text_delta","data":{"text":"A1"}}})).unwrap();
        let request = read_request(&first);
        assert_eq!(request["text"], "prompt a");
        writeln!(first, "{}", json!({"type":"reply","id":request["id"],"ok":true})).unwrap();
        wait_a.recv_timeout(Duration::from_secs(5)).unwrap();
        drop(first);
        let mut second = accept(&listener_a, "session-a", 2, Some(2));
        writeln!(second, "{}", json!({"type":"event","seq":2,"turn":null,
            "event":{"type":"text_delta","data":{"text":"A2"}}})).unwrap();
        thread::sleep(Duration::from_millis(200));
    });
    let server_b = thread::spawn(move || {
        let mut first = accept(&listener_b, "session-b", 1, None);
        writeln!(first, "{}", json!({"type":"event","seq":1,"turn":null,
            "event":{"type":"text_delta","data":{"text":"B1"}}})).unwrap();
        let request = read_request(&first);
        assert_eq!(request["text"], "prompt b");
        writeln!(first, "{}", json!({"type":"reply","id":request["id"],"ok":true})).unwrap();
        wait_b.recv_timeout(Duration::from_secs(5)).unwrap();
        drop(first);
        let mut second = accept(&listener_b, "session-b", 2, Some(2));
        writeln!(second, "{}", json!({"type":"event","seq":2,"turn":null,
            "event":{"type":"text_delta","data":{"text":"B2"}}})).unwrap();
        thread::sleep(Duration::from_millis(200));
    });
    let bridge = connect_sessions(&[session("session-a", path_a), session("session-b", path_b)]).unwrap();
    assert!(*bridge.complete.lock().unwrap());
    let mut app = App::default();
    for _ in 0..4 {
        let frame = bridge.frames.recv_timeout(Duration::from_secs(5)).unwrap();
        app.apply_daemon_frame(&frame);
    }
    assert_eq!(app.sessions.iter().find(|s| s.id == "session-a").unwrap().transcript, "A1");
    assert_eq!(app.sessions.iter().find(|s| s.id == "session-b").unwrap().transcript, "B1");
    bridge.commands.send(WorkerCommand::Prompt("session-a".into(), "prompt a".into())).unwrap();
    bridge.commands.send(WorkerCommand::Prompt("session-b".into(), "prompt b".into())).unwrap();
    let mut replies = std::collections::HashSet::new();
    while replies.len() < 2 {
        let reply = until(&bridge.frames, |f| f["type"] == "reply");
        replies.insert(reply["session_id"].as_str().unwrap().to_owned());
    }
    assert_eq!(replies, ["session-a".to_owned(), "session-b".to_owned()].into());
    done_a.send(()).unwrap();
    until(&bridge.frames, |f| f["type"] == "client_notice" && f["session_id"] == "session-a");
    assert!(!*bridge.complete.lock().unwrap());
    let hello = until(&bridge.frames, |f| f["type"] == "hello" && f["session_id"] == "session-a");
    app.apply_daemon_frame(&hello);
    let event = until(&bridge.frames, |f| f["type"] == "event" && f["session_id"] == "session-a" && f["seq"] == 2);
    app.apply_daemon_frame(&event);
    assert_eq!(app.sessions.iter().find(|s| s.id == "session-a").unwrap().transcript, "A1A2");
    done_b.send(()).unwrap();
    until(&bridge.frames, |f| f["type"] == "client_notice" && f["session_id"] == "session-b");
    let hello = until(&bridge.frames, |f| f["type"] == "hello" && f["session_id"] == "session-b");
    app.apply_daemon_frame(&hello);
    let event = until(&bridge.frames, |f| f["type"] == "event" && f["session_id"] == "session-b" && f["seq"] == 2);
    app.apply_daemon_frame(&event);
    assert_eq!(app.sessions.iter().find(|s| s.id == "session-b").unwrap().transcript, "B1B2");
    bridge.shutdown();
    server_a.join().unwrap();
    server_b.join().unwrap();
}

#[test]
fn partial_roster_cannot_claim_complete_layout() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("good.sock");
    let listener = UnixListener::bind(&path).unwrap();
    let server = thread::spawn(move || {
        let socket = accept(&listener, "good", 0, None);
        thread::sleep(Duration::from_millis(200));
        drop(socket);
    });
    let bridge = connect_sessions(&[session("good", path), session("missing", dir.path().join("missing.sock"))]).unwrap();
    assert_eq!(bridge.live_ids, ["good"]);
    assert!(!*bridge.complete.lock().unwrap());
    bridge.shutdown();
    server.join().unwrap();
}

#[test]
fn stop_targets_only_selected_socket_and_other_session_keeps_prompting() {
    let dir = tempfile::tempdir().unwrap();
    let path_a = dir.path().join("a.sock");
    let path_b = dir.path().join("b.sock");
    let listener_a = UnixListener::bind(&path_a).unwrap();
    let listener_b = UnixListener::bind(&path_b).unwrap();
    let server_a = thread::spawn(move || {
        let mut socket = accept(&listener_a, "session-a", 0, None);
        let request = read_request(&socket);
        assert_eq!(request["type"], "call");
        assert_eq!(request["method"], "stop");
        assert_eq!(request["params"], json!({}));
        writeln!(socket, "{}", json!({"type":"reply","id":request["id"],"ok":true})).unwrap();
    });
    let server_b = thread::spawn(move || {
        let mut socket = accept(&listener_b, "session-b", 0, None);
        let request = read_request(&socket);
        assert_eq!(request["type"], "prompt");
        assert_eq!(request["text"], "still live");
        writeln!(socket, "{}", json!({"type":"reply","id":request["id"],"ok":true})).unwrap();
        thread::sleep(Duration::from_millis(100));
    });
    let bridge = connect_sessions(&[session("session-a", path_a), session("session-b", path_b)]).unwrap();
    for _ in 0..2 { until(&bridge.frames, |f| f["type"] == "hello"); }
    bridge.commands.send(WorkerCommand::Stop("session-a".into())).unwrap();
    let stopped = until(&bridge.frames, |f| f["type"] == "stop_reply");
    assert_eq!(stopped["session_id"], "session-a");
    assert_eq!(stopped["ok"], true);
    assert!(!*bridge.complete.lock().unwrap());
    bridge.commands.send(WorkerCommand::Prompt("session-b".into(), "still live".into())).unwrap();
    let reply = until(&bridge.frames, |f| f["type"] == "reply");
    assert_eq!(reply["session_id"], "session-b");
    bridge.shutdown();
    server_a.join().unwrap();
    server_b.join().unwrap();
}

#[test]
fn dynamic_attach_rechecks_live_registry_before_routing() {
    let dir = tempfile::tempdir().unwrap();
    let runtime = dir.path().join("runtime");
    let registry = runtime.join("registry");
    std::fs::create_dir_all(&registry).unwrap();
    std::fs::set_permissions(&runtime, std::fs::Permissions::from_mode(0o700)).unwrap();
    std::fs::set_permissions(&registry, std::fs::Permissions::from_mode(0o700)).unwrap();
    let initial_path = dir.path().join("initial.sock");
    let initial_listener = UnixListener::bind(&initial_path).unwrap();
    let attached_path = runtime.join(format!("daemon-attached-{}.sock", std::process::id()));
    let attached_listener = UnixListener::bind(&attached_path).unwrap();
    std::fs::set_permissions(&attached_path, std::fs::Permissions::from_mode(0o600)).unwrap();
    let (release_initial, wait_initial) = mpsc::channel();
    let (release_attached, wait_attached) = mpsc::channel();
    let initial = thread::spawn(move || {
        let socket = accept(&initial_listener, "initial", 0, None);
        wait_initial.recv_timeout(Duration::from_secs(5)).unwrap();
        drop(socket);
    });
    let attached = thread::spawn(move || {
        let socket = accept(&attached_listener, "attached", 0, None);
        wait_attached.recv_timeout(Duration::from_secs(5)).unwrap();
        drop(socket);
    });
    let previous = std::env::var_os("DOXA_RUNTIME_DIR");
    std::env::set_var("DOXA_RUNTIME_DIR", &runtime);
    let bridge = connect_sessions(&[session("initial", initial_path)]).unwrap();
    until(&bridge.frames, |f| f["type"] == "hello");
    bridge.commands.send(WorkerCommand::Attach("attached".into(), 1)).unwrap();
    let refused = until(&bridge.frames, |f| f["type"] == "attach_reply");
    assert_eq!(refused["ok"], false);
    let entry = json!({"session_id":"attached", "pid":std::process::id(),
        "cwd":"/tmp", "repo_root":null, "title":"attached",
        "heartbeat_at":time::OffsetDateTime::now_utc().format(&time::format_description::well_known::Rfc3339).unwrap(),
        "started_at":"2026-01-01T00:00:00Z", "daemon_socket":attached_path});
    let entry_path = registry.join("attached.json");
    std::fs::write(&entry_path, serde_json::to_vec(&entry).unwrap()).unwrap();
    std::fs::set_permissions(&entry_path, std::fs::Permissions::from_mode(0o600)).unwrap();
    bridge.commands.send(WorkerCommand::Attach("attached".into(), 1)).unwrap();
    let reply = until(&bridge.frames, |f| f["type"] == "attach_reply");
    assert_eq!(reply["ok"], true);
    assert_eq!(reply["group"], 1);
    until(&bridge.frames, |f| f["type"] == "hello" && f["session_id"] == "attached");
    if let Some(old) = previous { std::env::set_var("DOXA_RUNTIME_DIR", old); }
    else { std::env::remove_var("DOXA_RUNTIME_DIR"); }
    release_initial.send(()).unwrap();
    release_attached.send(()).unwrap();
    bridge.shutdown();
    initial.join().unwrap();
    attached.join().unwrap();
}
