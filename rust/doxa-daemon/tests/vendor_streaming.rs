#![cfg(all(unix, feature = "local-test-server"))]
//! Delayed loopback SSE proves native streaming and canonical secrecy; no accounts.
use serde_json::{json, Value};
use std::{fs, io::{BufRead, BufReader, Read, Write}, net::TcpListener,
    os::unix::{fs::PermissionsExt, net::UnixStream}, path::Path, process::{Child, Command, Stdio},
    sync::mpsc, thread, time::{Duration, Instant}};
struct Daemon(Child);
impl Drop for Daemon { fn drop(&mut self) { let _ = self.0.kill(); let _ = self.0.wait(); } }
fn frame(reader: &mut BufReader<UnixStream>) -> Value {
    let mut line = String::new(); assert!(reader.read_line(&mut line).unwrap() > 0);
    serde_json::from_str(&line).unwrap()
}
fn send(socket: &mut UnixStream, value: Value) { writeln!(socket, "{value}").unwrap(); }
fn event(content: &str) -> String { format!("data: {}\n\n", json!({"model":"deepseek-flash","choices":[{"delta":{"content":content}}]})) }
fn transcript(root: &Path) -> std::path::PathBuf {
    let slug: String = root.to_string_lossy().chars().map(|c| if c.is_ascii_alphanumeric() {c} else {'-'}).collect();
    root.join("projects").join(slug).join("stream-session.messages.json")
}
fn run_case(case: &str) {
    let root = tempfile::tempdir().unwrap(); let root = root.path();
    let doxa = root.join("doxa"); fs::create_dir(&doxa).unwrap(); fs::set_permissions(&doxa, fs::Permissions::from_mode(0o700)).unwrap();
    // Generated synthetic values exist only in this owned fixture store.
    let active = format!("synthetic-active-{}", root.file_name().unwrap().to_string_lossy());
    let inactive = format!("synthetic-inactive-{}", root.file_name().unwrap().to_string_lossy());
    let credentials = doxa.join("credentials.json");
    fs::write(&credentials, json!({"deepseek":active,"glm":inactive}).to_string()).unwrap();
    fs::set_permissions(&credentials, fs::Permissions::from_mode(0o600)).unwrap();
    let safe = "Visible paragraph arrives while the provider connection is still waiting for the remainder of this response. ";
    let (prefix, tail) = match case {
        "split-secret" => {
            let split = inactive.len()/2;
            (format!("{safe}{}", &inactive[..split]), format!("{} password=\"synthetic phrase\" End.", &inactive[split..]))
        }
        "uncertain" => (safe.to_owned(), "password=\"unfinished synthetic span".into()),
        "overflow" => (safe.to_owned(), "x".repeat(8193)),
        _ => (safe.to_owned(), "Completed second paragraph.".into()),
    };
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let endpoint = format!("http://{}/chat", listener.local_addr().unwrap());
    let (release, wait_release) = mpsc::channel();
    let case_owned = case.to_owned();
    let server = thread::spawn(move || {
        let deadline = Instant::now()+Duration::from_secs(10);
        let mut socket = loop {
            match listener.accept() {
                Ok((socket,_)) => break socket,
                Err(error) if error.kind()==std::io::ErrorKind::WouldBlock => {
                    assert!(Instant::now()<deadline); thread::sleep(Duration::from_millis(10));
                }
                Err(error) => panic!("{error}"),
            }
        };
        socket.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        let mut request = Vec::new();
        loop {
            let mut bytes = [0u8;4096]; let count = socket.read(&mut bytes).unwrap(); assert!(count>0); request.extend_from_slice(&bytes[..count]);
            if let Some(index) = request.windows(4).position(|b| b == b"\r\n\r\n") {
                let head = String::from_utf8_lossy(&request[..index]);
                let length = head.lines().find_map(|line| line.to_ascii_lowercase().strip_prefix("content-length: ").and_then(|v| v.parse::<usize>().ok())).unwrap();
                if request.len() >= index+4+length { break; }
            }
        }
        let first = event(&prefix);
        let end = format!("{}data: {}\n\ndata: [DONE]\n\n", event(&tail), json!({"model":"deepseek-flash","choices":[{"finish_reason":"stop","delta":{}}],"usage":{"prompt_tokens":3,"completion_tokens":4}}));
        write!(socket, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{first}", first.len()+end.len()).unwrap(); socket.flush().unwrap();
        wait_release.recv_timeout(Duration::from_secs(10)).unwrap();
        if case_owned != "incomplete" { socket.write_all(end.as_bytes()).unwrap(); }
    });
    let mut daemon = Daemon(Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
        .env_clear().args(["--runtime-dir",root.to_str().unwrap(),"--cwd",root.to_str().unwrap(),"--session-id","stream-session","--engine","deepseek","--model","deepseek-flash","--effort","low","--vendor-endpoint",&endpoint,"--linger","20"])
        .env("PATH","/usr/bin:/bin").env("TMPDIR",root).env("HOME",root.join("home")).env("DOXA_HOME",&doxa)
        .env("LORE_ROOT",root.join("lore")).env("LORE_PROJECTS_DIR",root.join("projects"))
        .env("LORE_SKILLS_DIR",root.join("skills")).env("LORE_DISABLE_SYNC","1").env("LORE_DISABLE_REVIEW","1")
        .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn().unwrap());
    let registry = root.join("registry/stream-session.json"); let deadline = Instant::now()+Duration::from_secs(10);
    while !registry.exists() { assert!(daemon.0.try_wait().unwrap().is_none()); assert!(Instant::now()<deadline); thread::sleep(Duration::from_millis(10)); }
    let record: Value = serde_json::from_slice(&fs::read(registry).unwrap()).unwrap();
    let mut socket = UnixStream::connect(record["daemon_socket"].as_str().unwrap()).unwrap(); socket.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    let mut reader = BufReader::new(socket.try_clone().unwrap()); frame(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    let history_before = fs::read(transcript(root)).ok();
    send(&mut socket, json!({"type":"prompt","id":1,"text":"short synthetic fixture response"}));
    let mut visible = String::new();
    loop {
        let value = frame(&mut reader);
        assert_ne!(value["event"]["type"], "turn_done", "response completed before its delayed suffix was released");
        if value["event"]["type"] == "text_delta" {
            visible.push_str(value["event"]["data"]["text"].as_str().unwrap()); break;
        }
    }
    assert!(!visible.is_empty());
    assert!(!visible.contains(&inactive[..inactive.len()/2]));
    release.send(()).unwrap();
    let completion = loop {
        let value = frame(&mut reader);
        if value["event"]["type"] == "text_delta" { visible.push_str(value["event"]["data"]["text"].as_str().unwrap()); }
        if value["event"]["type"] == "turn_done" { break value["event"]["data"].clone(); }
    };
    assert!(!visible.contains(&inactive)); assert!(!visible.contains("synthetic phrase"));
    if matches!(case, "incomplete" | "uncertain" | "overflow") {
        assert_eq!(completion["is_error"], true);
        assert_eq!(fs::read(transcript(root)).ok(), history_before, "failed stream committed private replay");
    } else {
        assert_eq!(completion["is_error"], false);
        let paired: Value = serde_json::from_slice(&fs::read(transcript(root)).unwrap()).unwrap();
        let content = paired["messages"].as_array().unwrap().last().unwrap()["content"].as_str().unwrap();
        assert_eq!(visible, content, "final response was duplicated or omitted");
    }
    send(&mut socket, json!({"type":"call","id":2,"method":"stop","params":{}}));
    server.join().unwrap();
}
#[test] fn native_text_arrives_before_sse_completion_without_final_duplication() { run_case("plain"); }
#[test] fn split_saved_key_and_canonical_secret_never_reach_native_text() { run_case("split-secret"); }
#[test] fn incomplete_stream_does_not_commit_history() { run_case("incomplete"); }
#[test] fn uncertain_lexical_span_fails_closed_without_history_commit() { run_case("uncertain"); }
#[test] fn overflowing_lexical_span_cancels_without_history_commit() { run_case("overflow"); }
