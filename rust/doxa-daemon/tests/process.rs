use serde_json::{json, Value};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

fn wait_until(mut predicate: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while !predicate() {
        assert!(Instant::now() < deadline, "timed out waiting for daemon state");
        thread::sleep(Duration::from_millis(10));
    }
}
struct Process { child: Child, registry: PathBuf, socket: PathBuf }
impl Process {
    fn start(runtime: &Path, linger: &str) -> Self {
        let child = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
            .args(["--runtime-dir", runtime.to_str().unwrap(), "--cwd", "/tmp",
                "--session-id", "fixture-session", "--linger", linger])
            .stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap();
        let registry = runtime.join("registry/fixture-session.json");
        wait_until(|| registry.exists());
        let entry: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
        let socket = PathBuf::from(entry["daemon_socket"].as_str().unwrap());
        Self { child, registry, socket }
    }
    fn entry(&self) -> Value { serde_json::from_slice(&fs::read(&self.registry).unwrap()).unwrap() }
    fn connect(&self) -> (BufReader<UnixStream>, UnixStream) {
        let socket = UnixStream::connect(&self.socket).unwrap();
        socket.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        (BufReader::new(socket.try_clone().unwrap()), socket)
    }
    fn exited(&mut self) -> bool { self.child.try_wait().unwrap().is_some() }
}
impl Drop for Process {
    fn drop(&mut self) {
        if !self.exited() { let _ = self.child.kill(); let _ = self.child.wait(); }
    }
}
fn receive(reader: &mut BufReader<UnixStream>) -> Value {
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    assert!(!line.is_empty(), "socket closed");
    serde_json::from_str(&line).unwrap()
}
fn send(socket: &mut UnixStream, frame: Value) {
    writeln!(socket, "{frame}").unwrap();
}

#[test]
fn registry_wire_prompt_and_stop() {
    let dir = tempfile::tempdir().unwrap();
    let mut process = Process::start(dir.path(), "1");
    let entry = process.entry();
    assert_eq!(entry["session_id"], "fixture-session");
    assert_eq!(entry["socket_path"], entry["daemon_socket"]);
    assert_eq!(entry["engine"], "fixture");
    assert_eq!(fs::metadata(&process.registry).unwrap().permissions().mode() & 0o777, 0o600);
    assert_eq!(fs::metadata(&process.socket).unwrap().permissions().mode() & 0o777, 0o600);
    let (mut reader, mut socket) = process.connect();
    assert_eq!(receive(&mut reader)["proto"], 1);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    wait_until(|| process.entry()["clients"] == 1);
    send(&mut socket, json!({"type":"prompt","id":1,"text":"secret prompt"}));
    let reply = receive(&mut reader);
    assert_eq!(reply["ok"], true);
    assert!(reply["turn"].is_string());
    assert_eq!(receive(&mut reader)["event"]["type"], "turn_started");
    let delta = receive(&mut reader);
    assert_eq!(delta["event"]["data"]["text"], "Deterministic native fixture response.");
    assert!(!delta.to_string().contains("secret prompt"));
    assert_eq!(receive(&mut reader)["event"]["type"], "turn_done");
    send(&mut socket, json!({"type":"call","id":2,"method":"stop","params":{}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
    assert!(!process.registry.exists());
    assert!(!process.socket.exists());
}

#[test]
fn linger_resets_when_a_client_reattaches() {
    let dir = tempfile::tempdir().unwrap();
    let mut process = Process::start(dir.path(), "0.25");
    let (mut first, mut socket) = process.connect();
    receive(&mut first);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    wait_until(|| process.entry()["clients"] == 1);
    drop(first); drop(socket);
    wait_until(|| process.entry()["clients"] == 0);
    thread::sleep(Duration::from_millis(100));
    let (mut second, mut socket) = process.connect();
    receive(&mut second);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    wait_until(|| process.entry()["clients"] == 1);
    thread::sleep(Duration::from_millis(350));
    assert!(!process.exited(), "attached client must cancel linger");
    drop(second); drop(socket);
    wait_until(|| process.exited());
    assert!(!process.registry.exists());
}

#[test]
fn sigterm_removes_only_owned_resources() {
    let dir = tempfile::tempdir().unwrap();
    let mut process = Process::start(dir.path(), "10");
    unsafe { libc::kill(process.child.id() as libc::pid_t, libc::SIGTERM); }
    wait_until(|| process.exited());
    assert!(!process.registry.exists());
    assert!(!process.socket.exists());
}

#[test]
fn stop_during_rearmed_linger_exits_once() {
    let dir = tempfile::tempdir().unwrap();
    let mut process = Process::start(dir.path(), "0.5");
    let (mut first, mut socket) = process.connect();
    receive(&mut first);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    wait_until(|| process.entry()["clients"] == 1);
    drop(first); drop(socket);
    wait_until(|| process.entry()["clients"] == 0);
    thread::sleep(Duration::from_millis(200));
    let (mut second, mut socket) = process.connect();
    receive(&mut second);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    wait_until(|| process.entry()["clients"] == 1);
    send(&mut socket, json!({"type":"call","id":9,"method":"stop","params":{}}));
    assert_eq!(receive(&mut second)["id"], 9);
    wait_until(|| process.exited());
    assert!(!process.registry.exists());
    assert!(!process.socket.exists());
}

#[test]
fn rejects_traversal_and_existing_registry() {
    let dir = tempfile::tempdir().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
        .args(["--runtime-dir", dir.path().to_str().unwrap(), "--session-id", "../bad"])
        .output().unwrap();
    assert!(!output.status.success());
    assert!(!dir.path().join("bad.json").exists());
    let mut process = Process::start(dir.path(), "10");
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
        .args(["--runtime-dir", dir.path().to_str().unwrap(), "--session-id", "fixture-session"])
        .output().unwrap();
    assert!(!output.status.success());
    assert!(process.registry.exists());
    unsafe { libc::kill(process.child.id() as libc::pid_t, libc::SIGTERM); }
    wait_until(|| process.exited());
}
