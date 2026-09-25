use doxa_peers::{now as peer_now, PeerRecord, Registry as PeerRegistry};
use serde_json::{json, Value};
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[track_caller]
fn wait_until(mut predicate: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !predicate() {
        assert!(
            Instant::now() < deadline,
            "timed out waiting for daemon state"
        );
        thread::sleep(Duration::from_millis(10));
    }
}
struct Process {
    child: Child,
    registry: PathBuf,
    socket: PathBuf,
}
impl Process {
    fn start(runtime: &Path, linger: &str) -> Self {
        Self::start_in(runtime, Path::new("/tmp"), linger)
    }
    fn start_in(runtime: &Path, cwd: &Path, linger: &str) -> Self {
        let child = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
            .args([
                "--runtime-dir",
                runtime.to_str().unwrap(),
                "--cwd",
                cwd.to_str().unwrap(),
                "--session-id",
                "fixture-session",
                "--linger",
                linger,
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .env("DOXA_HOME", runtime.join("home"))
            .spawn()
            .unwrap();
        let registry = runtime.join("registry/fixture-session.json");
        wait_until(|| registry.exists());
        let entry: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
        let socket = PathBuf::from(entry["daemon_socket"].as_str().unwrap());
        Self {
            child,
            registry,
            socket,
        }
    }
    fn entry(&self) -> Value {
        serde_json::from_slice(&fs::read(&self.registry).unwrap()).unwrap()
    }
    fn start_codex(runtime: &Path, codex: &Path, python: &Path) -> Self {
        let child = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
            .args([
                "--runtime-dir",
                runtime.to_str().unwrap(),
                "--cwd",
                runtime.to_str().unwrap(),
                "--session-id",
                "codex-session",
                "--linger",
                "10",
                "--engine",
                "codex",
                "--codex-bin",
                codex.to_str().unwrap(),
                "--lore-python",
                python.to_str().unwrap(),
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .env("DOXA_HOME", runtime.join("home"))
            .spawn()
            .unwrap();
        let registry = runtime.join("registry/codex-session.json");
        wait_until(|| registry.exists());
        let entry: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
        let socket = PathBuf::from(entry["daemon_socket"].as_str().unwrap());
        Self {
            child,
            registry,
            socket,
        }
    }
    fn start_claude(runtime: &Path, script: &Path) -> Self {
        let child = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
            .args([
                "--runtime-dir",
                runtime.to_str().unwrap(),
                "--cwd",
                runtime.to_str().unwrap(),
                "--session-id",
                "claude-session",
                "--linger",
                "10",
                "--engine",
                "claude",
                "--claude-python",
                "/usr/bin/python3",
                "--claude-script",
                script.to_str().unwrap(),
            ])
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let registry = runtime.join("registry/claude-session.json");
        wait_until(|| registry.exists());
        let entry: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
        let socket = PathBuf::from(entry["daemon_socket"].as_str().unwrap());
        Self {
            child,
            registry,
            socket,
        }
    }
    fn connect(&self) -> (BufReader<UnixStream>, UnixStream) {
        let socket = UnixStream::connect(&self.socket).unwrap();
        socket
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        (BufReader::new(socket.try_clone().unwrap()), socket)
    }
    fn exited(&mut self) -> bool {
        self.child.try_wait().unwrap().is_some()
    }
}

#[test]
fn native_registry_uses_main_checkout_scope_from_linked_worktree() {
    let dir = tempfile::tempdir().unwrap();
    let main = dir.path().join("main");
    let worktree = dir.path().join("linked");
    let runtime = dir.path().join("runtime");
    fs::create_dir(&main).unwrap();
    let git = |cwd: &Path, args: &[&str]| {
        let output = Command::new("git")
            .args(args)
            .current_dir(cwd)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "git {:?}: {}",
            args,
            String::from_utf8_lossy(&output.stderr)
        );
    };
    git(&main, &["init", "-q"]);
    fs::write(main.join("README"), "fixture\n").unwrap();
    git(&main, &["add", "README"]);
    git(
        &main,
        &[
            "-c",
            "user.name=DOXA Test",
            "-c",
            "user.email=doxa@example.test",
            "commit",
            "-qm",
            "test: seed repository",
        ],
    );
    git(
        &main,
        &[
            "worktree",
            "add",
            "-qb",
            "linked",
            worktree.to_str().unwrap(),
        ],
    );
    let mut process = Process::start_in(&runtime, &worktree, "10");
    let entry = process.entry();
    assert_eq!(entry["cwd"], worktree.to_str().unwrap());
    assert_eq!(entry["repo_root"], main.to_str().unwrap());
    unsafe {
        libc::kill(process.child.id() as libc::pid_t, libc::SIGTERM);
    }
    wait_until(|| process.exited());
}
fn executable(path: &Path, body: &str) {
    fs::write(path, body).unwrap();
    let mut perms = fs::metadata(path).unwrap().permissions();
    perms.set_mode(0o700);
    fs::set_permissions(path, perms).unwrap();
}
fn fake_scrubber(path: &Path, fail: bool) {
    let mode = if fail { "True" } else { "False" };
    executable(
        path,
        &format!(
            r#"#!/usr/bin/env python3
import json, sys
print(json.dumps({{"type":"hello","proto":1,"capabilities":["scrub","snapshot","transcript_identity"]}}), flush=True)
for line in sys.stdin:
    frame = json.loads(line)
    if frame.get("op") == "transcript_identity":
        print(json.dumps({{"type":"reply","id":frame["id"],"ok":True,"value":{{"projects_dir":frame["cwd"],"slug":"project"}}}}), flush=True)
    elif {mode} and "fixture-secret" in frame.get("text", ""):
        print(json.dumps({{"type":"reply","id":frame["id"],"ok":False,"error":"operation_failed"}}), flush=True)
    else:
        print(json.dumps({{"type":"reply","id":frame["id"],"ok":True,"text":frame.get("text", "").replace("fixture-secret", "[redacted]")}}), flush=True)
"#
        ),
    );
}

fn fake_context_sidecar(path: &Path, snapshot_available: bool) {
    let snapshot_reply = if snapshot_available {
        r#"{"ok":True,"text":"fixture-secret durable memory"}"#
    } else {
        r#"{"ok":False,"error":"operation_failed"}"#
    };
    executable(
        path,
        &format!(
            r#"#!/usr/bin/env python3
import json, sys
print(json.dumps({{"type":"hello","proto":1,"capabilities":["scrub","snapshot","transcript_identity"]}}), flush=True)
for line in sys.stdin:
    frame = json.loads(line)
    op = frame.get("op")
    if op == "transcript_identity":
        reply = {{"ok":True,"value":{{"projects_dir":frame["cwd"],"slug":"project"}}}}
    elif op == "snapshot":
        assert frame["scope"] == "all"
        reply = {snapshot_reply}
    else:
        reply = {{"ok":True,"text":frame.get("text", "").replace("fixture-secret", "[redacted]")}}
    print(json.dumps({{"type":"reply","id":frame["id"],**reply}}), flush=True)
"#
        ),
    );
}

fn registry_peer(runtime: &Path, id: &str, scope: &str, title: &str) -> (UnixListener, PathBuf) {
    let socket = runtime.join(format!("{id}.sock"));
    let listener = UnixListener::bind(&socket).unwrap();
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
    let registry = PeerRegistry::open(runtime).unwrap();
    let record = PeerRecord {
        session_id: id.into(),
        pid: std::process::id() as i32,
        socket_path: socket.to_string_lossy().into_owned(),
        cwd: scope.into(),
        repo_root: None,
        title: title.into(),
        started_at: peer_now(),
        heartbeat_at: peer_now(),
        daemon_socket: None,
        clients: Some(0),
        usage_tokens: None,
        provider: None,
        model: None,
        engine: Some("fixture".into()),
        parent_session_id: None,
    };
    registry.write(&record).unwrap();
    (listener, registry.directory().join(format!("{id}.json")))
}
impl Drop for Process {
    fn drop(&mut self) {
        if !self.exited() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
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
    assert_ne!(entry["socket_path"], entry["daemon_socket"]);
    assert_eq!(
        entry["daemon_socket"],
        process.socket.to_string_lossy().as_ref()
    );
    assert_eq!(
        fs::metadata(entry["socket_path"].as_str().unwrap())
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    assert_eq!(entry["engine"], "fixture");
    assert_eq!(
        fs::metadata(&process.registry)
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    assert_eq!(
        fs::metadata(&process.socket).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let (mut reader, mut socket) = process.connect();
    assert_eq!(receive(&mut reader)["proto"], 1);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    wait_until(|| process.entry()["clients"] == 1);
    send(
        &mut socket,
        json!({"type":"prompt","id":1,"text":"secret prompt"}),
    );
    let reply = receive(&mut reader);
    assert_eq!(reply["ok"], true);
    assert!(reply["turn"].is_string());
    assert_eq!(receive(&mut reader)["event"]["type"], "turn_started");
    let delta = receive(&mut reader);
    assert_eq!(
        delta["event"]["data"]["text"],
        "Deterministic native fixture response."
    );
    assert!(!delta.to_string().contains("secret prompt"));
    assert_eq!(receive(&mut reader)["event"]["type"], "turn_done");
    send(
        &mut socket,
        json!({"type":"call","id":2,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
    assert!(!process.registry.exists());
    assert!(!process.socket.exists());
}

#[test]
fn linger_resets_when_a_client_reattaches() {
    let dir = tempfile::tempdir().unwrap();
    // Leave enough room for a loaded CI runner to schedule the reconnect.
    let mut process = Process::start(dir.path(), "1.0");
    let (mut first, mut socket) = process.connect();
    receive(&mut first);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    wait_until(|| process.entry()["clients"] == 1);
    drop(first);
    drop(socket);
    wait_until(|| process.entry()["clients"] == 0);
    thread::sleep(Duration::from_millis(100));
    let (mut second, mut socket) = process.connect();
    receive(&mut second);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    wait_until(|| process.entry()["clients"] == 1);
    thread::sleep(Duration::from_millis(1200));
    assert!(!process.exited(), "attached client must cancel linger");
    drop(second);
    drop(socket);
    wait_until(|| process.exited());
    assert!(!process.registry.exists());
}

#[test]
fn sigterm_removes_only_owned_resources() {
    let dir = tempfile::tempdir().unwrap();
    let mut process = Process::start(dir.path(), "10");
    unsafe {
        libc::kill(process.child.id() as libc::pid_t, libc::SIGTERM);
    }
    wait_until(|| process.exited());
    assert!(!process.registry.exists());
    assert!(!process.socket.exists());
}

#[test]
fn stop_during_rearmed_linger_exits_once() {
    let dir = tempfile::tempdir().unwrap();
    let mut process = Process::start(dir.path(), "1.0");
    let (mut first, mut socket) = process.connect();
    receive(&mut first);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    wait_until(|| process.entry()["clients"] == 1);
    drop(first);
    drop(socket);
    wait_until(|| process.entry()["clients"] == 0);
    thread::sleep(Duration::from_millis(200));
    let (mut second, mut socket) = process.connect();
    receive(&mut second);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    wait_until(|| process.entry()["clients"] == 1);
    send(
        &mut socket,
        json!({"type":"call","id":9,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut second)["id"], 9);
    wait_until(|| process.exited());
    assert!(!process.registry.exists());
    assert!(!process.socket.exists());
}

#[test]
fn rejects_traversal_and_existing_registry() {
    let dir = tempfile::tempdir().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
        .args([
            "--runtime-dir",
            dir.path().to_str().unwrap(),
            "--session-id",
            "../bad",
        ])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(!dir.path().join("bad.json").exists());
    let mut process = Process::start(dir.path(), "10");
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
        .args([
            "--runtime-dir",
            dir.path().to_str().unwrap(),
            "--session-id",
            "fixture-session",
        ])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(process.registry.exists());
    unsafe {
        libc::kill(process.child.id() as libc::pid_t, libc::SIGTERM);
    }
    wait_until(|| process.exited());
}

#[test]
fn codex_host_resumes_and_scrubs_provider_events() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    let args = dir.path().join("argv.txt");
    let prompt = dir.path().join("prompt.txt");
    fake_scrubber(&python, false);
    executable(
        &codex,
        &format!(
            r#"#!/bin/sh
printf '%s\n' "$@" >> '{}'
echo END >> '{}'
cat >> '{}'
echo '{{"type":"thread.started","thread_id":"thread_1"}}'
echo '{{"type":"item.completed","item":{{"type":"agent_message","text":"fixture-secret answer"}}}}'
"#,
            args.display(),
            args.display(),
            prompt.display()
        ),
    );
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    assert_eq!(process.entry()["engine"], "codex");
    let (mut reader, mut socket) = process.connect();
    assert_eq!(receive(&mut reader)["engine"], "codex");
    send(&mut socket, json!({"type":"attach","cursor":null}));
    for (id, text) in [(1, "fixture-secret first prompt"), (2, "second prompt")] {
        send(&mut socket, json!({"type":"prompt","id":id,"text":text}));
        assert_eq!(receive(&mut reader)["ok"], true);
        let mut kinds = Vec::new();
        loop {
            let frame = receive(&mut reader);
            assert!(!frame.to_string().contains("fixture-secret"));
            let event = &frame["event"];
            if event["type"] != "prompt_dequeued" {
                kinds.push(event["type"].as_str().unwrap().to_owned());
            }
            if event["type"] == "turn_started" {
                assert_eq!(
                    event["data"]["prompt"],
                    text.replace("fixture-secret", "[redacted]")
                );
            }
            if event["type"] == "text_delta" {
                assert_eq!(event["data"]["text"], "[redacted] answer");
            }
            if event["type"] == "turn_done" {
                assert_eq!(event["data"]["is_error"], false);
                break;
            }
        }
        assert_eq!(kinds, ["turn_started", "text_delta", "turn_done"]);
    }
    let argv = fs::read_to_string(args).unwrap();
    assert!(argv.contains("exec\nresume\nthread_1\n"));
    assert_eq!(
        fs::read_to_string(prompt).unwrap(),
        "fixture-secret first promptsecond prompt"
    );
    send(
        &mut socket,
        json!({"type":"call","id":3,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
}

#[test]
fn codex_host_indexes_completed_turn_and_finalized_transcript() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    let index_log = dir.path().join("index-requests.jsonl");
    let index_done = dir.path().join("index-done");
    executable(
        &python,
        &format!(
            r#"#!/usr/bin/env python3
import json, sys, time
print(json.dumps({{"type":"hello","proto":1,"capabilities":["scrub","snapshot","transcript_identity","index_transcript_v1"]}}), flush=True)
for line in sys.stdin:
    frame = json.loads(line)
    op = frame["op"]
    if op == "transcript_identity":
        value = {{"projects_dir":frame["cwd"],"slug":"project"}}
        reply = {{"value":value}}
    elif op == "index_transcript_v1":
        with open({:?}, "a") as out:
            out.write(json.dumps(frame) + "\n")
        time.sleep(0.8)
        open({:?}, "w").close()
        reply = {{"value":{{"indexed":2,"consumed":2}}}}
    else:
        reply = {{"text":frame.get("text", "")}}
    print(json.dumps({{"type":"reply","id":frame["id"],"ok":True,**reply}}), flush=True)
"#,
            index_log.to_str().unwrap(),
            index_done.to_str().unwrap()
        ),
    );
    executable(
        &codex,
        "#!/bin/sh\ncat >/dev/null\necho '{\"type\":\"thread.started\",\"thread_id\":\"thread_1\"}'\necho '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"answer\"}}'\n",
    );
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    loop {
        if receive(&mut reader)["event"]["type"] == "turn_done" {
            break;
        }
    }
    wait_until(|| index_log.exists());
    // The sidecar is still indexing. Completion must already be visible to
    // the runtime rather than keeping the session busy for its round trip.
    send(&mut socket, json!({"type":"call","id":3,"method":"status","params":{}}));
    assert_eq!(receive(&mut reader)["status"]["running"], false);
    assert!(!index_done.exists(), "turn completion waited for LORE indexing");
    send(&mut socket, json!({"type":"call","id":2,"method":"stop","params":{}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
    let rows: Vec<Value> = fs::read_to_string(index_log)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(rows.len(), 2);
    for row in rows {
        assert_eq!(row["op"], "index_transcript_v1");
        assert_eq!(row["cwd"], dir.path().to_str().unwrap());
        assert_eq!(row["session_id"], "codex-session");
        assert!(row.get("path").is_none());
    }
}

#[test]
fn codex_memory_reaches_only_first_provider_stdin_and_not_transcript() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    let first = dir.path().join("first-prompt.txt");
    let resumed = dir.path().join("resumed-prompts.txt");
    fake_context_sidecar(&python, true);
    executable(
        &codex,
        &format!(
            r#"#!/bin/sh
if [ "$2" = resume ]; then
  cat >> '{}'
  printf '\nEND\n' >> '{}'
else
  cat > '{}'
fi
echo '{{"type":"thread.started","thread_id":"thread_1"}}'
echo '{{"type":"item.completed","item":{{"type":"agent_message","text":"answer"}}}}'
"#,
            resumed.display(),
            resumed.display(),
            first.display()
        ),
    );
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    for (id, prompt) in [(1, "fixture-secret first"), (2, "second")] {
        send(&mut socket, json!({"type":"prompt","id":id,"text":prompt}));
        assert_eq!(receive(&mut reader)["ok"], true);
        loop {
            let frame = receive(&mut reader);
            assert!(!frame.to_string().contains("fixture-secret"));
            if frame["event"]["type"] == "turn_started" {
                assert_eq!(
                    frame["event"]["data"]["prompt"],
                    prompt.replace("fixture-secret", "[redacted]")
                );
            }
            if frame["event"]["type"] == "turn_done" {
                break;
            }
        }
    }
    send(
        &mut socket,
        json!({"type":"call","id":4,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
    // Reopen the same DOXA session: the recorded provider thread must resume.
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":3,"text":"third"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    loop {
        if receive(&mut reader)["event"]["type"] == "turn_done" {
            break;
        }
    }
    send(
        &mut socket,
        json!({"type":"call","id":4,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());

    let first_text = fs::read_to_string(first).unwrap();
    assert!(first_text.starts_with("[DOXA MEMORY -- not typed by the user]"));
    assert!(first_text
        .contains("fixture-secret durable memory\n[END OF MEMORY]\n\nfixture-secret first"));
    assert_eq!(
        fs::read_to_string(resumed).unwrap(),
        "second\nEND\nthird\nEND\n"
    );
    let transcript = fs::read_to_string(dir.path().join("project/codex-session.jsonl")).unwrap();
    assert!(!transcript.contains("DOXA MEMORY"));
    assert!(!transcript.contains("durable memory"));
    assert!(!transcript.contains("fixture-secret"));
    assert!(transcript.contains("[redacted] first"));
}

#[test]
fn unavailable_lore_snapshot_does_not_block_a_scrubbable_codex_turn() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    let captured = dir.path().join("stdin.txt");
    fake_context_sidecar(&python, false);
    executable(
        &codex,
        &format!(
            "#!/bin/sh\ncat > '{}'\necho '{{\"type\":\"thread.started\",\"thread_id\":\"thread_1\"}}'\n",
            captured.display()
        ),
    );
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(
        &mut socket,
        json!({"type":"prompt","id":1,"text":"fixture-secret prompt"}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    loop {
        let event = receive(&mut reader);
        assert!(!event.to_string().contains("fixture-secret"));
        if event["event"]["type"] == "turn_done" {
            assert_eq!(event["event"]["data"]["is_error"], false);
            break;
        }
    }
    assert_eq!(
        fs::read_to_string(captured).unwrap(),
        "fixture-secret prompt"
    );
    send(
        &mut socket,
        json!({"type":"call","id":2,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
}

#[test]
fn codex_transcript_and_thread_survive_daemon_restart() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    let args = dir.path().join("argv.txt");
    fake_scrubber(&python, false);
    executable(
        &codex,
        &format!(
            r#"#!/bin/sh
printf '%s\n' "$@" >> '{}'
cat >/dev/null
echo '{{"type":"thread.started","thread_id":"thread_1"}}'
echo '{{"type":"item.completed","item":{{"type":"agent_message","text":"fixture-secret answer"}}}}'
"#,
            args.display()
        ),
    );
    for index in 0..2 {
        let mut process = Process::start_codex(dir.path(), &codex, &python);
        let (mut reader, mut socket) = process.connect();
        let hello = receive(&mut reader);
        assert_eq!(hello["doxa"], env!("CARGO_PKG_VERSION"));
        if index == 1 {
            assert_eq!(
                hello["transcript_path"],
                dir.path()
                    .join("project/codex-session.jsonl")
                    .to_str()
                    .unwrap()
            );
            assert!(hello["transcript_bytes"].as_u64().unwrap() > 0);
        }
        send(&mut socket, json!({"type":"attach","cursor":null}));
        send(
            &mut socket,
            json!({"type":"prompt","id":1,"text":format!("fixture-secret prompt {index}")}),
        );
        assert_eq!(receive(&mut reader)["ok"], true);
        loop {
            if receive(&mut reader)["event"]["type"] == "turn_done" {
                break;
            }
        }
        send(
            &mut socket,
            json!({"type":"call","id":2,"method":"stop","params":{}}),
        );
        assert_eq!(receive(&mut reader)["ok"], true);
        wait_until(|| process.exited());
    }
    let records: Vec<Value> = fs::read_to_string(dir.path().join("project/codex-session.jsonl"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(records.len(), 4);
    assert_eq!(records[0]["type"], "user");
    assert_eq!(records[0]["message"]["content"], "[redacted] prompt 0");
    assert_eq!(records[1]["type"], "assistant");
    assert_eq!(
        records[1]["message"]["content"][0]["text"],
        "[redacted] answer"
    );
    assert!(records.iter().all(
        |record| record["engine"] == "codex" && !record.to_string().contains("fixture-secret")
    ));
    let thread: Value = serde_json::from_slice(
        &fs::read(dir.path().join("project/codex-session.codex.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(thread["thread_id"], "thread_1");
    assert_eq!(thread["session_id"], "codex-session");
    assert_eq!(thread["turn_incomplete"], false);
    assert!(fs::read_to_string(args)
        .unwrap()
        .contains("exec\nresume\nthread_1\n"));
}

#[test]
fn codex_prompt_append_failure_withholds_provider_execution() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    let invoked = dir.path().join("provider-invoked");
    fake_scrubber(&python, false);
    executable(&codex, &format!("#!/bin/sh\ntouch '{}'\n", invoked.display()));
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    let transcript = dir.path().join("project/codex-session.jsonl");
    std::os::unix::fs::symlink("/dev/full", &transcript).unwrap();
    send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    loop {
        let frame = receive(&mut reader);
        if frame["event"]["type"] == "turn_done" {
            assert_eq!(frame["event"]["data"]["is_error"], true);
            assert!(frame["event"]["data"]["error"]
                .as_str()
                .unwrap()
                .contains("prompt could not be persisted"));
            break;
        }
    }
    assert!(!invoked.exists());
    send(&mut socket, json!({"type":"prompt","id":2,"text":"again"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    loop {
        let frame = receive(&mut reader);
        if frame["event"]["type"] == "turn_done" {
            assert_eq!(frame["event"]["data"]["is_error"], true);
            break;
        }
    }
    assert!(!invoked.exists());
    send(&mut socket, json!({"type":"call","id":3,"method":"stop","params":{}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
}

#[test]
fn codex_thread_write_failure_reports_turn_error_and_stops_session() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    let invoked = dir.path().join("provider-invoked");
    fake_scrubber(&python, false);
    executable(
        &codex,
        &format!(
            "#!/bin/sh\ntouch '{}'\ncat >/dev/null\necho '{{\"type\":\"thread.started\",\"thread_id\":\"thread_1\"}}'\necho '{{\"type\":\"item.completed\",\"item\":{{\"type\":\"agent_message\",\"text\":\"answer\"}}}}'\n",
            invoked.display()
        ),
    );
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    fs::create_dir(dir.path().join("project/codex-session.codex.json")).unwrap();
    send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    loop {
        let frame = receive(&mut reader);
        if frame["event"]["type"] == "turn_done" {
            assert_eq!(frame["event"]["data"]["is_error"], true);
            assert!(frame["event"]["data"]["error"]
                .as_str()
                .unwrap()
                .contains("persistence failed"));
            break;
        }
    }
    assert!(invoked.exists());
    send(&mut socket, json!({"type":"prompt","id":2,"text":"again"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    loop {
        let frame = receive(&mut reader);
        if frame["event"]["type"] == "turn_done" {
            assert_eq!(frame["event"]["data"]["is_error"], true);
            break;
        }
    }
    send(&mut socket, json!({"type":"call","id":3,"method":"stop","params":{}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
}

#[test]
fn codex_assistant_append_failure_overrides_successful_provider_turn() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    let ready = dir.path().join("provider-ready");
    let release = dir.path().join("provider-release");
    fake_scrubber(&python, false);
    executable(
        &codex,
        &format!(
            "#!/bin/sh\ncat >/dev/null\necho '{{\"type\":\"thread.started\",\"thread_id\":\"thread_1\"}}'\ntouch '{}'\nwhile [ ! -e '{}' ]; do sleep 0.01; done\necho '{{\"type\":\"item.completed\",\"item\":{{\"type\":\"agent_message\",\"text\":\"answer\"}}}}'\n",
            ready.display(),
            release.display()
        ),
    );
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| ready.exists());
    let transcript = dir.path().join("project/codex-session.jsonl");
    let saved = dir.path().join("project/saved-user.jsonl");
    fs::rename(&transcript, &saved).unwrap();
    std::os::unix::fs::symlink("/dev/full", &transcript).unwrap();
    fs::write(&release, "").unwrap();
    loop {
        let frame = receive(&mut reader);
        if frame["event"]["type"] == "turn_done" {
            assert_eq!(frame["event"]["data"]["is_error"], true);
            assert!(frame["event"]["data"]["error"]
                .as_str()
                .unwrap()
                .contains("persistence failed"));
            break;
        }
    }
    assert!(fs::read_to_string(saved).unwrap().contains("hello"));
    let thread: Value = serde_json::from_slice(
        &fs::read(dir.path().join("project/codex-session.codex.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(thread["thread_id"], "thread_1");
    assert_eq!(thread["turn_incomplete"], true);
    send(&mut socket, json!({"type":"call","id":2,"method":"stop","params":{}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
    let restart = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
        .args([
            "--runtime-dir",
            dir.path().to_str().unwrap(),
            "--cwd",
            dir.path().to_str().unwrap(),
            "--session-id",
            "codex-session",
            "--engine",
            "codex",
            "--codex-bin",
            codex.to_str().unwrap(),
            "--lore-python",
            python.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(!restart.status.success());
}

#[test]
fn existing_transcript_without_thread_id_refuses_new_codex_thread() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    fake_scrubber(&python, false);
    executable(&codex, "#!/bin/sh\necho started > should-not-start\n");
    fs::create_dir(dir.path().join("project")).unwrap();
    fs::write(
        dir.path().join("project/codex-session.jsonl"),
        "{\"type\":\"user\",\"engine\":\"codex\"}\n",
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
        .args([
            "--runtime-dir",
            dir.path().to_str().unwrap(),
            "--cwd",
            dir.path().to_str().unwrap(),
            "--session-id",
            "codex-session",
            "--engine",
            "codex",
            "--codex-bin",
            codex.to_str().unwrap(),
            "--lore-python",
            python.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(!dir.path().join("should-not-start").exists());
    assert!(!dir.path().join("registry/codex-session.json").exists());
}

#[test]
fn thread_identity_is_durable_before_turn_completes() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    fake_scrubber(&python, false);
    executable(&codex, "#!/bin/sh\ncat >/dev/null\necho '{\"type\":\"thread.started\",\"thread_id\":\"thread_early\"}'\nsleep 10\n");
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    let path = dir.path().join("project/codex-session.codex.json");
    wait_until(|| path.exists());
    let thread: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    assert_eq!(thread["thread_id"], "thread_early");
    unsafe {
        libc::kill(process.child.id() as libc::pid_t, libc::SIGTERM);
    }
    wait_until(|| process.exited());
}

#[test]
fn claude_sidecar_answers_interrupts_and_finalizes() {
    let dir = tempfile::tempdir().unwrap();
    let script = dir.path().join("claude-fixture.py");
    let finalized = dir.path().join("finalized");
    fs::write(&script, format!(r#"import json, sys
print(json.dumps({{"type":"hello","protocol":"doxa-claude-sidecar","version":1}}),flush=True)
for line in sys.stdin:
    frame=json.loads(line)
    method=frame["method"]
    if method == "start":
        assert frame["params"]["session_id"] == "claude-session"
        print(json.dumps({{"type":"reply","id":frame["id"],"ok":True,"result":{{"event":"session_started","data":{{}}}}}}),flush=True)
    elif method == "prompt":
        print(json.dumps({{"type":"reply","id":frame["id"],"ok":True,"result":{{}}}}),flush=True)
        print(json.dumps({{"type":"event","event":"turn_started","data":{{"prompt":frame["params"]["text"]}}}}),flush=True)
        print(json.dumps({{"type":"event","event":"needs_input","data":{{"id":"question-1"}}}}),flush=True)
    elif method == "answer":
        assert frame["params"]["id"] == "question-1"
        print(json.dumps({{"type":"reply","id":frame["id"],"ok":True,"result":{{"applied":True}}}}),flush=True)
        print(json.dumps({{"type":"event","event":"text_delta","data":{{"text":"answered"}}}}),flush=True)
        print(json.dumps({{"type":"event","event":"turn_done","data":{{"is_error":False}}}}),flush=True)
    elif method == "interrupt":
        print(json.dumps({{"type":"reply","id":frame["id"],"ok":True,"result":{{}}}}),flush=True)
        print(json.dumps({{"type":"event","event":"turn_interrupted","data":{{}}}}),flush=True)
    elif method == "finalize":
        open({:?},"w").write("done")
        print(json.dumps({{"type":"reply","id":frame["id"],"ok":True,"result":{{}}}}),flush=True)
        break
"#, finalized.to_string_lossy().to_string())).unwrap();
    let mut process = Process::start_claude(dir.path(), &script);
    assert_eq!(process.entry()["engine"], "claude");
    let (mut reader, mut socket) = process.connect();
    assert_eq!(receive(&mut reader)["engine"], "claude");
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"call","id":6,"method":"set_model",
        "params":{"model":"haiku"}}));
    assert_eq!(receive(&mut reader)["ok"], false);
    send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    assert_eq!(receive(&mut reader)["event"]["type"], "turn_started");
    assert_eq!(receive(&mut reader)["event"]["type"], "needs_input");
    send(
        &mut socket,
        json!({"type":"call","id":2,"method":"answer_needs_input",
        "params":{"id":"question-1","answer":{"choice":"yes"}}}),
    );
    let frames = [
        receive(&mut reader),
        receive(&mut reader),
        receive(&mut reader),
    ];
    assert!(frames.iter().any(|frame| frame["applied"] == true));
    assert!(frames
        .iter()
        .any(|frame| frame["event"]["data"]["text"] == "answered"));
    assert!(frames
        .iter()
        .any(|frame| frame["event"]["type"] == "turn_done"));
    send(&mut socket, json!({"type":"prompt","id":3,"text":"again"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    let mut next = receive(&mut reader);
    if next["event"]["type"] == "prompt_dequeued" {
        next = receive(&mut reader);
    }
    assert_eq!(next["event"]["type"], "turn_started");
    assert_eq!(receive(&mut reader)["event"]["type"], "needs_input");
    send(
        &mut socket,
        json!({"type":"call","id":4,"method":"interrupt","params":{}}),
    );
    let first = receive(&mut reader);
    let second = receive(&mut reader);
    let (reply, interrupted) = if first["type"] == "reply" {
        (first, second)
    } else {
        (second, first)
    };
    assert_eq!(reply["ok"], true);
    assert_eq!(interrupted["event"]["type"], "turn_done");
    assert_eq!(interrupted["event"]["data"]["is_error"], true);
    send(
        &mut socket,
        json!({"type":"call","id":5,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
    assert!(finalized.exists());
}

#[test]
fn claude_controls_require_capabilities_and_broadcast_changes() {
    let dir = tempfile::tempdir().unwrap();
    let script = dir.path().join("claude-controls.py");
    fs::write(&script, r#"import json, sys
print(json.dumps({"type":"hello","protocol":"doxa-claude-sidecar","version":1,
                  "capabilities":["set_model","set_permission_mode"]}),flush=True)
for line in sys.stdin:
    frame=json.loads(line)
    method=frame["method"]
    result={}
    if method=="start":
        result={"data":{"model":"opus"},"permission_mode":"plan"}
    elif method=="set_model":
        result={"model":frame["params"]["model"] or "default"}
    elif method=="set_permission_mode":
        result={"mode":frame["params"]["mode"]}
    elif method=="prompt":
        print(json.dumps({"type":"reply","id":frame["id"],"ok":True,"result":{}}),flush=True)
        print(json.dumps({"type":"event","event":"needs_input","data":{"id":"q"}}),flush=True)
        continue
    elif method=="answer":
        print(json.dumps({"type":"reply","id":frame["id"],"ok":True,"result":{"applied":True}}),flush=True)
        print(json.dumps({"type":"event","event":"turn_done","data":{}}),flush=True)
        continue
    print(json.dumps({"type":"reply","id":frame["id"],"ok":True,"result":result}),flush=True)
    if method=="finalize": break
"#).unwrap();
    let mut process = Process::start_claude(dir.path(), &script);
    let (mut reader, mut socket) = process.connect();
    let hello = receive(&mut reader);
    assert_eq!(hello["model"], "opus");
    assert_eq!(hello["permission_mode"], "plan");
    assert_eq!(hello["bypass_armed"], false);
    assert_eq!(hello["can_set_permission_mode"], true);
    assert_eq!(hello["running"], false);
    assert_eq!(hello["queued"], 0);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"call","id":1,"method":"set_model",
        "params":{"model":"haiku"}}));
    assert_eq!(receive(&mut reader)["model"], "haiku");
    assert_eq!(receive(&mut reader)["event"]["type"], "model_changed");
    send(&mut socket, json!({"type":"call","id":2,"method":"set_permission_mode",
        "params":{"mode":"dontAsk"}}));
    assert_eq!(receive(&mut reader)["mode"], "dontAsk");
    assert_eq!(receive(&mut reader)["event"]["type"], "permission_mode_changed");
    send(&mut socket, json!({"type":"call","id":3,"method":"status","params":{}}));
    let status = receive(&mut reader);
    assert_eq!(status["status"]["model"], "haiku");
    assert_eq!(status["status"]["permission_mode"], "dontAsk");
    assert_eq!(status["status"]["can_set_permission_mode"], true);
    send(&mut socket, json!({"type":"call","id":4,"method":"set_permission_mode",
        "params":{"mode":"bypassPermissions"}}));
    assert_eq!(receive(&mut reader)["ok"], false);
    send(&mut socket, json!({"type":"call","id":5,"method":"set_permission_mode",
        "params":{"mode":"plan"}}));
    assert_eq!(receive(&mut reader)["mode"], "plan");
    assert_eq!(receive(&mut reader)["event"]["type"], "permission_mode_changed");
    send(&mut socket, json!({"type":"prompt","id":6,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    assert_eq!(receive(&mut reader)["event"]["type"], "needs_input");
    send(&mut socket, json!({"type":"call","id":7,"method":"set_permission_mode",
        "params":{"mode":"dontAsk"}}));
    assert_eq!(receive(&mut reader)["ok"], false);
    send(&mut socket, json!({"type":"call","id":8,"method":"answer_needs_input",
        "params":{"id":"q","answer":{"yes":true}}}));
    let answer1 = receive(&mut reader);
    let answer2 = receive(&mut reader);
    assert!(answer1["applied"] == true || answer2["applied"] == true);
    assert!(answer1["event"]["type"] == "turn_done" || answer2["event"]["type"] == "turn_done");
    send(&mut socket, json!({"type":"call","id":9,"method":"stop","params":{}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
}

#[test]
fn oversized_claude_event_fails_turn_without_forwarding_content() {
    let dir = tempfile::tempdir().unwrap();
    let script = dir.path().join("claude-oversize.py");
    fs::write(&script, r#"import json, sys
print(json.dumps({"type":"hello","protocol":"doxa-claude-sidecar","version":1}),flush=True)
for line in sys.stdin:
    frame=json.loads(line)
    print(json.dumps({"type":"reply","id":frame["id"],"ok":True,"result":{}}),flush=True)
    if frame["method"] == "prompt":
        print(json.dumps({"type":"event","event":"text_delta","data":{"text":"SENSITIVE"*10000}}),flush=True)
"#).unwrap();
    let mut process = Process::start_claude(dir.path(), &script);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    let frame = receive(&mut reader);
    assert_eq!(frame["event"]["type"], "turn_done");
    assert_eq!(frame["event"]["data"]["is_error"], true);
    assert!(!frame.to_string().contains("SENSITIVE"));
    unsafe {
        libc::kill(process.child.id() as libc::pid_t, libc::SIGTERM);
    }
    wait_until(|| process.exited());
}

#[test]
fn scrub_failure_withholds_provider_content_and_fails_turn() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    fake_scrubber(&python, true);
    executable(&codex, "#!/bin/sh\ncat >/dev/null\necho '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"fixture-secret answer\"}}'\n");
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    let mut frames = Vec::new();
    loop {
        let frame = receive(&mut reader);
        assert!(!frame.to_string().contains("fixture-secret"));
        let done = frame["event"]["type"] == "turn_done";
        frames.push(frame);
        if done {
            break;
        }
    }
    assert_eq!(frames.len(), 2);
    assert_eq!(frames[1]["event"]["data"]["is_error"], true);
    assert!(frames[1]["event"]["data"]["error"]
        .as_str()
        .unwrap()
        .contains("scrub failed"));
    send(
        &mut socket,
        json!({"type":"call","id":2,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
}

#[test]
fn interrupt_reaps_codex_process_group() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    let marker = dir.path().join("survived");
    fake_scrubber(&python, false);
    executable(
        &codex,
        &format!(
            "#!/bin/sh\ncat >/dev/null\necho '{{\"type\":\"thread.started\",\"thread_id\":\"thread_1\"}}'\nsh -c 'sleep 1; echo leaked > {}' &\nwait\n",
            marker.display()
        ),
    );
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    assert_eq!(receive(&mut reader)["event"]["type"], "turn_started");
    let thread_path = dir.path().join("project/codex-session.codex.json");
    wait_until(|| thread_path.exists());
    send(
        &mut socket,
        json!({"type":"call","id":2,"method":"interrupt","params":{}}),
    );
    let mut replied = false;
    let mut done = false;
    while !replied || !done {
        let frame = receive(&mut reader);
        if frame["type"] == "reply" {
            assert_eq!(frame["ok"], true);
            replied = true;
        }
        if frame["event"]["type"] == "turn_done" {
            assert_eq!(frame["event"]["data"]["is_error"], true);
            done = true;
        }
    }
    thread::sleep(Duration::from_millis(1200));
    assert!(!marker.exists(), "Codex descendant survived interruption");
    let thread: Value = serde_json::from_slice(&fs::read(&thread_path).unwrap()).unwrap();
    assert_eq!(thread["thread_id"], "thread_1");
    assert_eq!(thread["turn_incomplete"], true);
    send(
        &mut socket,
        json!({"type":"call","id":3,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
    let restart = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
        .args([
            "--runtime-dir", dir.path().to_str().unwrap(),
            "--cwd", dir.path().to_str().unwrap(),
            "--session-id", "codex-session", "--engine", "codex",
            "--codex-bin", codex.to_str().unwrap(),
            "--lore-python", python.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(!restart.status.success(), "incomplete turn must refuse restart");
}

#[test]
fn missing_lore_sidecar_rejects_session_before_socket_or_registry() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("broken-python");
    executable(&codex, "#!/bin/sh\nexit 0\n");
    executable(&python, "#!/bin/sh\nexit 1\n");
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
        .args([
            "--runtime-dir",
            dir.path().to_str().unwrap(),
            "--cwd",
            dir.path().to_str().unwrap(),
            "--session-id",
            "codex-session",
            "--engine",
            "codex",
            "--codex-bin",
            codex.to_str().unwrap(),
            "--lore-python",
            python.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(!dir.path().join("registry/codex-session.json").exists());
    assert!(!dir.path().join("daemon-codex-s").exists());
}

#[test]
fn claude_daemon_uses_venv_interpreter_from_absolute_argument() {
    let dir = tempfile::tempdir().unwrap();
    let venv = dir.path().join("venv");
    assert!(Command::new("python3")
        .args(["-m", "venv", "--without-pip"])
        .arg(&venv)
        .status()
        .unwrap()
        .success());
    let python = venv.join("bin/python3");
    let site = Command::new(&python)
        .args(["-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"])
        .output()
        .unwrap();
    assert!(site.status.success());
    let site = PathBuf::from(String::from_utf8(site.stdout).unwrap().trim());
    fs::write(site.join("doxa_venv_marker.py"), "VALUE = 'installed in venv'\n").unwrap();
    let marker = dir.path().join("prefix.txt");
    let script = dir.path().join("claude_sidecar.py");
    fs::write(&script, r#"import json, os, pathlib, sys
import doxa_venv_marker
assert doxa_venv_marker.VALUE == 'installed in venv'
pathlib.Path(os.environ['DOXA_TEST_VENV_MARKER']).write_text(sys.prefix)
print(json.dumps({'type':'hello','protocol':'doxa-claude-sidecar','version':1,'capabilities':['start']}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({'type':'reply','id':request['id'],'ok':True,
        'result':{'data':{'model':'fixture'},'permission_mode':'default'}}), flush=True)
"#).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
        .args(["--runtime-dir", dir.path().to_str().unwrap(),
            "--cwd", dir.path().to_str().unwrap(),
            "--session-id", "venv-claude", "--engine", "claude",
            "--claude-python", python.to_str().unwrap(),
            "--claude-script", script.to_str().unwrap(), "--linger", "10"])
        .env("DOXA_TEST_VENV_MARKER", &marker)
        .current_dir("/")
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while !marker.exists() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    let _ = child.kill();
    let output = child.wait_with_output().unwrap();
    assert!(marker.exists(), "daemon did not start venv sidecar: {}", String::from_utf8_lossy(&output.stderr));
    assert_eq!(fs::read_to_string(marker).unwrap(), venv.to_str().unwrap());
}

#[test]
fn queued_codex_prompt_is_scrubbed_for_other_clients() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    fake_scrubber(&python, false);
    executable(&codex, "#!/bin/sh\ncat >/dev/null\nsleep 1\necho '{\"type\":\"thread.started\",\"thread_id\":\"thread_1\"}'\n");
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut first, mut first_socket) = process.connect();
    receive(&mut first);
    send(&mut first_socket, json!({"type":"attach","cursor":null}));
    send(
        &mut first_socket,
        json!({"type":"prompt","id":1,"text":"first"}),
    );
    assert_eq!(receive(&mut first)["ok"], true);
    assert_eq!(receive(&mut first)["event"]["type"], "turn_started");
    let (mut second, mut second_socket) = process.connect();
    let hello = receive(&mut second);
    send(
        &mut second_socket,
        json!({"type":"attach","cursor":hello["next_seq"]}),
    );
    wait_until(|| process.entry()["clients"] == 2);
    send(
        &mut first_socket,
        json!({"type":"prompt","id":2,"text":"fixture-secret queued"}),
    );
    let reply = receive(&mut first);
    assert_eq!(reply["queued"], true);
    let event = receive(&mut second);
    assert_eq!(event["event"]["type"], "prompt_queued");
    assert_eq!(event["event"]["data"]["text"], "[redacted] queued");
    assert!(!event.to_string().contains("fixture-secret"));
    send(
        &mut first_socket,
        json!({"type":"call","id":3,"method":"stop","params":{}}),
    );
    while receive(&mut first)["id"] != 3 {}
    wait_until(|| process.exited());
}

#[test]
fn sigterm_reaps_active_codex_process_group() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    let ready = dir.path().join("ready");
    let marker = dir.path().join("survived");
    fake_scrubber(&python, false);
    executable(&codex, &format!("#!/bin/sh\ncat >/dev/null\necho ready > {}\nsh -c 'sleep 1; echo leaked > {}' &\nwait\n", ready.display(), marker.display()));
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| ready.exists());
    unsafe {
        libc::kill(process.child.id() as libc::pid_t, libc::SIGTERM);
    }
    wait_until(|| process.exited());
    thread::sleep(Duration::from_millis(1200));
    assert!(
        !marker.exists(),
        "Codex descendant survived daemon termination"
    );
    assert!(!process.registry.exists());
}

#[test]
fn registry_write_failure_reaps_active_codex_process_group() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    let ready = dir.path().join("ready");
    let marker = dir.path().join("survived");
    fake_scrubber(&python, false);
    executable(&codex, &format!("#!/bin/sh\ncat >/dev/null\necho ready > {}\nsh -c 'sleep 1; echo leaked > {}' &\nwait\n", ready.display(), marker.display()));
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| ready.exists() && process.entry()["clients"] == 1);

    let owned_inode = fs::metadata(&process.registry).unwrap().ino();
    fs::remove_file(&process.registry).unwrap();
    fs::write(&process.registry, "replacement").unwrap();
    assert_ne!(
        fs::metadata(&process.registry).unwrap().ino(),
        owned_inode,
        "the daemon must pin the owned registry inode against immediate reuse"
    );
    let (mut second, mut second_socket) = process.connect();
    receive(&mut second);
    send(&mut second_socket, json!({"type":"attach","cursor":null}));
    wait_until(|| process.exited());
    assert!(!process.child.try_wait().unwrap().unwrap().success());
    thread::sleep(Duration::from_millis(1200));
    assert!(
        !marker.exists(),
        "Codex descendant survived registry write failure"
    );
    assert_eq!(
        fs::read_to_string(&process.registry).unwrap(),
        "replacement"
    );
    assert!(!process.socket.exists());
}

#[cfg(feature = "local-test-server")]
mod vendor_process {
    use super::*;
    use std::net::TcpListener;

    fn fake_vendor(count: usize, answer: &'static str) -> (String, thread::JoinHandle<Vec<Value>>) {
        let body = format!("data: {{\"model\":\"resolved-model\",\"choices\":[{{\"finish_reason\":\"stop\",\"delta\":{{\"content\":\"{answer}\"}}}}],\"usage\":{{\"prompt_tokens\":3,\"completion_tokens\":4}}}}\n\ndata: [DONE]\n\n");
        fake_vendor_frames(vec![body; count])
    }

    fn fake_vendor_frames(frames: Vec<String>) -> (String, thread::JoinHandle<Vec<Value>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint = format!("http://{}/chat/completions", listener.local_addr().unwrap());
        let task = thread::spawn(move || {
            let mut requests = Vec::new();
            for body in frames {
                let (mut socket, _) = listener.accept().unwrap();
                socket
                    .set_read_timeout(Some(Duration::from_secs(3)))
                    .unwrap();
                let mut request = Vec::new();
                let mut buf = [0; 4096];
                loop {
                    let n = std::io::Read::read(&mut socket, &mut buf).unwrap();
                    assert!(
                        n > 0,
                        "provider connection closed after {} request bytes",
                        request.len()
                    );
                    request.extend_from_slice(&buf[..n]);
                    if let Some(pos) = request.windows(4).position(|part| part == b"\r\n\r\n") {
                        let header = String::from_utf8_lossy(&request[..pos]);
                        let len = header
                            .lines()
                            .find_map(|line| {
                                line.to_ascii_lowercase()
                                    .strip_prefix("content-length: ")
                                    .and_then(|n| n.trim().parse::<usize>().ok())
                            })
                            .unwrap_or(0);
                        if request.len() >= pos + 4 + len {
                            assert!(header
                                .to_ascii_lowercase()
                                .contains("authorization: bearer test-key-1234"));
                            requests.push(
                                serde_json::from_slice(&request[pos + 4..pos + 4 + len]).unwrap(),
                            );
                            break;
                        }
                    }
                }
                write!(socket, "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
            }
            requests
        });
        (endpoint, task)
    }

    fn start_vendor(runtime: &Path, vendor: &str, endpoint: &str, lore: &Path) -> Process {
        start_vendor_resume(runtime, vendor, endpoint, lore, false)
    }

    fn start_vendor_resume(
        runtime: &Path,
        vendor: &str,
        endpoint: &str,
        lore: &Path,
        resume: bool,
    ) -> Process {
        start_vendor_resume_tools(runtime, vendor, endpoint, lore, resume, false)
    }

    fn start_vendor_resume_tools(
        runtime: &Path,
        vendor: &str,
        endpoint: &str,
        lore: &Path,
        resume: bool,
        tools: bool,
    ) -> Process {
        let child = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
            .args([
                "--runtime-dir",
                runtime.to_str().unwrap(),
                "--cwd",
                runtime.to_str().unwrap(),
                "--session-id",
                "vendor-session",
                "--linger",
                "10",
                "--engine",
                vendor,
                "--lore-python",
                lore.to_str().unwrap(),
                "--vendor-endpoint",
                endpoint,
                "--resume",
                if resume { "true" } else { "false" },
            ])
            .env("DEEPSEEK_API_KEY", "test-key-1234")
            .env("ZAI_API_KEY", "test-key-1234")
            .env("DOXA_VENDOR_TOOLS", if tools { "workspace-read" } else { "" })
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let registry = runtime.join("registry/vendor-session.json");
        wait_until(|| registry.exists());
        let entry: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
        let socket = PathBuf::from(entry["daemon_socket"].as_str().unwrap());
        Process {
            child,
            registry,
            socket,
        }
    }

    #[test]
    fn native_vendor_chat_preserves_scrubbed_history_usage_and_model() {
        for vendor in ["deepseek", "glm"] {
            let dir = tempfile::tempdir().unwrap();
            let lore = dir.path().join("lore-fixture");
            fake_scrubber(&lore, false);
            let (endpoint, server) = fake_vendor(2, "fixture-secret answer");
            let mut process = start_vendor(dir.path(), vendor, &endpoint, &lore);
            assert_eq!(process.entry()["engine"], vendor);
            let (mut reader, mut socket) = process.connect();
            assert_eq!(
                receive(&mut reader)["model"],
                if vendor == "glm" {
                    "glm-5.3-flash"
                } else {
                    "deepseek-flash"
                }
            );
            send(&mut socket, json!({"type":"attach","cursor":null}));
            for id in 1..=2 {
                send(
                    &mut socket,
                    json!({"type":"prompt","id":id,"text":"fixture-secret prompt"}),
                );
                assert_eq!(receive(&mut reader)["ok"], true);
                let started = receive(&mut reader);
                assert_eq!(started["event"]["type"], "turn_started");
                assert_eq!(started["event"]["data"]["prompt"], "[redacted] prompt");
                let text = receive(&mut reader);
                assert_eq!(text["event"]["data"]["text"], "[redacted] answer", "{text}");
                let done = receive(&mut reader);
                assert_eq!(done["event"]["type"], "turn_done");
                assert_eq!(done["event"]["data"]["is_error"], false);
                assert_eq!(done["event"]["data"]["model"], "resolved-model");
                assert_eq!(done["event"]["data"]["prompt_tokens"], 3);
                assert_eq!(done["event"]["data"]["completion_tokens"], 4);
                assert!(done["event"]["data"]["cost_usd"].is_null());
            }
            send(
                &mut socket,
                json!({"type":"call","id":3,"method":"set_model","params":{}}),
            );
            let unsupported = receive(&mut reader);
            assert_eq!(unsupported["ok"], false);
            assert!(unsupported["error"]
                .as_str()
                .unwrap()
                .contains("unavailable"));
            send(
                &mut socket,
                json!({"type":"call","id":4,"method":"stop","params":{}}),
            );
            assert_eq!(receive(&mut reader)["ok"], true);
            wait_until(|| process.exited());
            let requests = server.join().unwrap();
            assert!(requests.iter().all(|body| body.get("tools").is_none()));
            assert_eq!(requests[1]["messages"][1]["content"], "[redacted] answer");
            assert!(!requests[1].to_string().contains("fixture-secret"));
            assert_eq!(
                fs::read_dir(dir.path().join("registry")).unwrap().count(),
                0
            );
        }
    }

    #[test]
    fn vendor_workspace_read_is_opt_in_scrubbed_and_turn_local() {
        let dir = tempfile::tempdir().unwrap();
        let lore = dir.path().join("lore-fixture");
        fake_scrubber(&lore, false);
        fs::write(dir.path().join("note.txt"), "fixture-secret workspace note").unwrap();
        let tool = "data: {\"choices\":[{\"finish_reason\":\"tool_calls\",\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call-1\",\"function\":{\"name\":\"workspace_read\",\"arguments\":\"{\\\"path\\\":\\\"note.txt\\\"}\"}}]}}]}\n\ndata: [DONE]\n\n";
        let answer = "data: {\"choices\":[{\"finish_reason\":\"stop\",\"delta\":{\"content\":\"Final answer\"}}]}\n\ndata: [DONE]\n\n";
        let (endpoint, server) = fake_vendor_frames(vec![tool.into(), answer.into()]);
        let mut process = start_vendor_resume_tools(dir.path(), "deepseek", &endpoint, &lore, false, true);
        let (mut reader, mut socket) = process.connect();
        receive(&mut reader);
        send(&mut socket, json!({"type":"attach","cursor":null}));
        send(&mut socket, json!({"type":"prompt","id":1,"text":"read note"}));
        assert_eq!(receive(&mut reader)["ok"], true);
        let started = receive(&mut reader);
        assert_eq!(started["event"]["data"]["vendor_tools"], "workspace-read");
        assert_eq!(receive(&mut reader)["event"]["data"]["text"], "Final answer");
        assert_eq!(receive(&mut reader)["event"]["data"]["is_error"], false);
        send(&mut socket, json!({"type":"call","id":2,"method":"stop","params":{}}));
        assert_eq!(receive(&mut reader)["ok"], true);
        wait_until(|| process.exited());
        let requests = server.join().unwrap();
        assert_eq!(requests[0]["tools"][0]["function"]["name"], "workspace_read");
        let result = requests[1]["messages"][2]["content"].as_str().unwrap();
        assert!(result.contains("[redacted] workspace note"));
        assert!(!requests[1].to_string().contains("fixture-secret"));
        let saved: Value = serde_json::from_slice(&fs::read(dir.path().join("project/vendor-session.messages.json")).unwrap()).unwrap();
        assert_eq!(saved["messages"].as_array().unwrap().len(), 2);
        assert_eq!(saved["messages"][1]["content"], "Final answer");
    }

    #[test]
    fn vendor_restart_replays_scrubbed_history_and_rejects_corrupt_state() {
        for vendor in ["deepseek", "glm"] {
            let dir = tempfile::tempdir().unwrap();
            let lore = dir.path().join("lore-fixture");
            fake_scrubber(&lore, false);
            let (endpoint, first_server) = fake_vendor(1, "fixture-secret first");
            let mut first = start_vendor(dir.path(), vendor, &endpoint, &lore);
            let (mut reader, mut socket) = first.connect();
            receive(&mut reader);
            send(&mut socket, json!({"type":"attach","cursor":null}));
            send(
                &mut socket,
                json!({"type":"prompt","id":1,"text":"fixture-secret prompt"}),
            );
            assert_eq!(receive(&mut reader)["ok"], true);
            for _ in 0..3 {
                receive(&mut reader);
            }
            send(
                &mut socket,
                json!({"type":"call","id":2,"method":"stop","params":{}}),
            );
            assert_eq!(receive(&mut reader)["ok"], true);
            wait_until(|| first.exited());
            first_server.join().unwrap();
            let state = dir.path().join("project/vendor-session.messages.json");
            let transcript = dir.path().join("project/vendor-session.jsonl");
            let records: Vec<Value> = fs::read_to_string(&transcript)
                .unwrap()
                .lines()
                .map(|line| serde_json::from_str(line).unwrap())
                .collect();
            assert_eq!(records.len(), 2);
            assert_eq!(records[0]["message"]["content"], "[redacted] prompt");
            assert_eq!(
                records[1]["message"]["content"][0]["text"],
                "[redacted] first"
            );
            assert!(records.iter().all(|record| record["engine"] == vendor));
            let saved: Value = serde_json::from_slice(&fs::read(&state).unwrap()).unwrap();
            assert_eq!(saved["engine"], vendor);
            assert_eq!(saved["session_id"], "vendor-session");
            assert_eq!(saved["messages"][0]["content"], "[redacted] prompt");
            assert_eq!(saved["messages"][1]["content"], "[redacted] first");
            assert!(!saved.to_string().contains("fixture-secret"));

            let (endpoint, second_server) = fake_vendor(1, "second");
            let mut second = start_vendor_resume(dir.path(), vendor, &endpoint, &lore, true);
            let (mut reader, mut socket) = second.connect();
            let hello = receive(&mut reader);
            assert_eq!(hello["transcript_path"], transcript.to_str().unwrap());
            assert_eq!(
                hello["transcript_bytes"],
                fs::metadata(&transcript).unwrap().len()
            );
            send(
                &mut socket,
                json!({"type":"attach","cursor":hello["next_seq"]}),
            );
            send(
                &mut socket,
                json!({"type":"prompt","id":1,"text":"continue"}),
            );
            assert_eq!(receive(&mut reader)["ok"], true);
            for _ in 0..3 {
                receive(&mut reader);
            }
            send(
                &mut socket,
                json!({"type":"call","id":2,"method":"stop","params":{}}),
            );
            assert_eq!(receive(&mut reader)["ok"], true);
            wait_until(|| second.exited());
            let requests = second_server.join().unwrap();
            assert_eq!(requests[0]["messages"][0]["content"], "[redacted] prompt");
            assert_eq!(requests[0]["messages"][1]["content"], "[redacted] first");
            assert_eq!(requests[0]["messages"][2]["content"], "continue");
            let records: Vec<Value> = fs::read_to_string(&transcript)
                .unwrap()
                .lines()
                .map(|line| serde_json::from_str(line).unwrap())
                .collect();
            assert_eq!(records.len(), 4);
            assert_eq!(records[2]["message"]["content"], "continue");
            assert_eq!(records[3]["message"]["content"][0]["text"], "second");

            let good_transcript = fs::read(&transcript).unwrap();
            fs::write(&transcript, b"{broken\n").unwrap();
            let output = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
                .args([
                    "--runtime-dir",
                    dir.path().to_str().unwrap(),
                    "--cwd",
                    dir.path().to_str().unwrap(),
                    "--session-id",
                    "vendor-session",
                    "--engine",
                    vendor,
                    "--lore-python",
                    lore.to_str().unwrap(),
                    "--resume",
                    "true",
                ])
                .env("DEEPSEEK_API_KEY", "test-key-1234")
                .env("ZAI_API_KEY", "test-key-1234")
                .output()
                .unwrap();
            assert!(!output.status.success());
            assert!(!dir.path().join("registry/vendor-session.json").exists());
            fs::write(&transcript, good_transcript).unwrap();
            fs::write(&state, b"{broken").unwrap();
            let output = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
                .args([
                    "--runtime-dir",
                    dir.path().to_str().unwrap(),
                    "--cwd",
                    dir.path().to_str().unwrap(),
                    "--session-id",
                    "vendor-session",
                    "--engine",
                    vendor,
                    "--lore-python",
                    lore.to_str().unwrap(),
                    "--resume",
                    "true",
                ])
                .env("DEEPSEEK_API_KEY", "test-key-1234")
                .env("ZAI_API_KEY", "test-key-1234")
                .output()
                .unwrap();
            assert!(!output.status.success());
            assert_eq!(fs::read(&state).unwrap(), b"{broken");
        }
    }

    #[test]
    fn vendor_scrub_failure_withholds_provider_text() {
        let dir = tempfile::tempdir().unwrap();
        let lore = dir.path().join("lore-fixture");
        fake_scrubber(&lore, true);
        let (endpoint, server) = fake_vendor(1, "fixture-secret answer");
        let mut process = start_vendor(dir.path(), "deepseek", &endpoint, &lore);
        let (mut reader, mut socket) = process.connect();
        receive(&mut reader);
        send(&mut socket, json!({"type":"attach","cursor":null}));
        send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
        assert_eq!(receive(&mut reader)["ok"], true);
        assert_eq!(receive(&mut reader)["event"]["type"], "turn_started");
        let done = receive(&mut reader);
        assert_eq!(done["event"]["type"], "turn_done");
        assert_eq!(done["event"]["data"]["is_error"], true);
        assert!(!done.to_string().contains("fixture-secret"));
        send(
            &mut socket,
            json!({"type":"call","id":2,"method":"stop","params":{}}),
        );
        assert_eq!(receive(&mut reader)["ok"], true);
        wait_until(|| process.exited());
        assert_eq!(server.join().unwrap().len(), 1);
        assert!(!dir.path().join("project/vendor-session.jsonl").exists());
        assert!(!dir
            .path()
            .join("project/vendor-session.messages.json")
            .exists());
    }

    #[test]
    fn vendor_transcript_write_failure_poisoned_session_without_history_commit() {
        let dir = tempfile::tempdir().unwrap();
        let lore = dir.path().join("lore-fixture");
        fake_scrubber(&lore, false);
        let (endpoint, server) = fake_vendor(1, "answer");
        let mut process = start_vendor(dir.path(), "deepseek", &endpoint, &lore);
        let (mut reader, mut socket) = process.connect();
        receive(&mut reader);
        send(&mut socket, json!({"type":"attach","cursor":null}));
        let target = dir.path().join("outside");
        fs::write(&target, b"untouched").unwrap();
        let transcript = dir.path().join("project/vendor-session.jsonl");
        std::os::unix::fs::symlink(&target, &transcript).unwrap();
        send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
        assert_eq!(receive(&mut reader)["ok"], true);
        assert_eq!(receive(&mut reader)["event"]["type"], "turn_started");
        let done = receive(&mut reader);
        assert_eq!(done["event"]["data"]["is_error"], true);
        assert_eq!(fs::read(&target).unwrap(), b"untouched");
        assert!(!dir
            .path()
            .join("project/vendor-session.messages.json")
            .exists());
        send(&mut socket, json!({"type":"prompt","id":2,"text":"again"}));
        assert_eq!(receive(&mut reader)["ok"], true);
        let refused = receive(&mut reader);
        assert_eq!(refused["event"]["data"]["is_error"], true);
        assert!(refused["event"]["data"]["error"]
            .as_str()
            .unwrap()
            .contains("uncertain"));
        send(
            &mut socket,
            json!({"type":"call","id":3,"method":"stop","params":{}}),
        );
        assert_eq!(receive(&mut reader)["ok"], true);
        wait_until(|| process.exited());
        assert_eq!(server.join().unwrap().len(), 1);
    }

    #[test]
    fn vendor_interrupt_cancels_active_request() {
        let dir = tempfile::tempdir().unwrap();
        let lore = dir.path().join("lore-fixture");
        fake_scrubber(&lore, false);
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let endpoint = format!("http://{}/chat/completions", listener.local_addr().unwrap());
        let mut process = start_vendor(dir.path(), "glm", &endpoint, &lore);
        let (mut reader, mut socket) = process.connect();
        receive(&mut reader);
        send(&mut socket, json!({"type":"attach","cursor":null}));
        send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
        assert_eq!(receive(&mut reader)["ok"], true);
        assert_eq!(receive(&mut reader)["event"]["type"], "turn_started");
        send(
            &mut socket,
            json!({"type":"call","id":2,"method":"interrupt","params":{}}),
        );
        let mut replied = false;
        let mut done = false;
        for _ in 0..3 {
            let frame = receive(&mut reader);
            if frame["type"] == "reply" {
                assert_eq!(frame["ok"], true);
                replied = true;
            }
            if frame["event"]["type"] == "turn_done" {
                assert_eq!(frame["event"]["data"]["is_error"], true);
                done = true;
            }
            if replied && done {
                break;
            }
        }
        assert!(replied && done);
        send(
            &mut socket,
            json!({"type":"call","id":3,"method":"stop","params":{}}),
        );
        assert_eq!(receive(&mut reader)["ok"], true);
        wait_until(|| process.exited());
    }

    #[test]
    fn vendor_missing_credential_rejects_session_before_socket() {
        let dir = tempfile::tempdir().unwrap();
        let lore = dir.path().join("lore-fixture");
        fake_scrubber(&lore, false);
        let output = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
            .args([
                "--runtime-dir",
                dir.path().to_str().unwrap(),
                "--cwd",
                dir.path().to_str().unwrap(),
                "--session-id",
                "vendor-session",
                "--engine",
                "deepseek",
                "--lore-python",
                lore.to_str().unwrap(),
            ])
            .env_remove("DEEPSEEK_API_KEY")
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(!dir.path().join("registry/vendor-session.json").exists());
        assert!(!String::from_utf8_lossy(&output.stderr).contains("test-key-1234"));
    }
}

#[test]
fn native_peers_rpc_returns_only_scrubbed_same_scope_live_peers() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    fake_scrubber(&python, false);
    executable(&codex, "#!/bin/sh\nexit 0\n");
    let (_same_listener, same_path) = registry_peer(
        dir.path(),
        "same",
        dir.path().to_str().unwrap(),
        "fixture-secret teammate",
    );
    let (_other_listener, _) = registry_peer(
        dir.path(),
        "other",
        "/other-project",
        "fixture-secret outsider",
    );
    let before = fs::read(&same_path).unwrap();
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(
        &mut socket,
        json!({"type":"call","id":1,"method":"peers","params":{}}),
    );
    let reply = receive(&mut reader);
    assert_eq!(reply["ok"], true);
    assert_eq!(reply["peers"].as_array().unwrap().len(), 1);
    assert_eq!(reply["peers"][0]["session_id"], "same");
    assert_eq!(reply["peers"][0]["title"], "[redacted] teammate");
    assert!(!reply.to_string().contains("fixture-secret"));
    assert!(!reply.to_string().contains(dir.path().to_str().unwrap()));
    assert!(reply["peers"][0].get("pid").is_none());
    assert_eq!(fs::read(&same_path).unwrap(), before);
    send(
        &mut socket,
        json!({"type":"call","id":2,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
}

#[test]
fn peers_rpc_fails_closed_without_lore_or_when_scrub_fails() {
    let dir = tempfile::tempdir().unwrap();
    let mut fixture = Process::start(dir.path(), "10");
    let (mut reader, mut socket) = fixture.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(
        &mut socket,
        json!({"type":"call","id":1,"method":"peers","params":{}}),
    );
    let reply = receive(&mut reader);
    assert_eq!(reply["ok"], false);
    assert!(reply.get("peers").is_none());
    send(
        &mut socket,
        json!({"type":"call","id":2,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| fixture.exited());

    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    fake_scrubber(&python, true);
    executable(&codex, "#!/bin/sh\nexit 0\n");
    let (listener, peer_entry) = registry_peer(
        dir.path(),
        "same",
        dir.path().to_str().unwrap(),
        "fixture-secret title",
    );
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(
        &mut socket,
        json!({"type":"call","id":1,"method":"peers","params":{}}),
    );
    let reply = receive(&mut reader);
    assert_eq!(reply["ok"], false);
    assert!(reply.get("peers").is_none());
    assert!(!reply.to_string().contains("fixture-secret"));
    let mut clean_peer: Value = serde_json::from_slice(&fs::read(&peer_entry).unwrap()).unwrap();
    clean_peer["title"] = json!("safe title");
    fs::write(&peer_entry, serde_json::to_vec(&clean_peer).unwrap()).unwrap();
    send(
        &mut socket,
        json!({"type":"call","id":3,"method":"msg",
        "params":{"target":"same","text":"fixture-secret message"}}),
    );
    let rejected = receive(&mut reader);
    assert_eq!(rejected["ok"], false);
    assert!(!dir.path().join("home/peers/messages.jsonl").exists());
    listener.set_nonblocking(true).unwrap();
    while let Ok((mut connection, _)) = listener.accept() {
        let mut bytes = Vec::new();
        connection.read_to_end(&mut bytes).unwrap();
        assert!(bytes.is_empty(), "scrub failure must not send a peer frame");
    }
    send(
        &mut socket,
        json!({"type":"call","id":2,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
}

#[test]
fn native_msg_sends_scrubbed_frame_records_ledger_and_denies_other_scope() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    fake_scrubber(&python, false);
    executable(&codex, "#!/bin/sh\nexit 0\n");
    let (same_listener, _) =
        registry_peer(dir.path(), "same", dir.path().to_str().unwrap(), "teammate");
    let (_other_listener, _) = registry_peer(dir.path(), "other", "/other-project", "outsider");
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let peer_socket = PathBuf::from(process.entry()["socket_path"].as_str().unwrap());
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(
        &mut socket,
        json!({"type":"call","id":1,"method":"msg",
        "params":{"target":"other","text":"fixture-secret"}}),
    );
    let denied = receive(&mut reader);
    assert_eq!(denied["ok"], false);
    assert!(!dir.path().join("home/peers/messages.jsonl").exists());
    let worker = thread::spawn(move || {
        same_listener.set_nonblocking(true).unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            match same_listener.accept() {
                Ok((mut stream, _)) => {
                    stream
                        .set_read_timeout(Some(Duration::from_millis(500)))
                        .unwrap();
                    let mut body = String::new();
                    stream.read_to_string(&mut body).unwrap();
                    if !body.is_empty() {
                        return body;
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    assert!(Instant::now() < deadline, "peer frame never arrived");
                    thread::sleep(Duration::from_millis(5));
                }
                Err(error) => panic!("{error}"),
            }
        }
    });
    send(
        &mut socket,
        json!({"type":"call","id":2,"method":"msg",
        "params":{"target":"same","text":"fixture-secret hello"}}),
    );
    let mut reply = Value::Null;
    let mut saw_sent = false;
    for _ in 0..3 {
        let frame = receive(&mut reader);
        if frame["event"]["type"] == "peer_sent" {
            saw_sent = true;
        }
        if frame["type"] == "reply" && frame["id"] == 2 {
            reply = frame;
            break;
        }
    }
    if !saw_sent {
        let sent = receive(&mut reader);
        assert_eq!(sent["event"]["type"], "peer_sent");
        saw_sent = true;
    }
    assert!(saw_sent);
    assert_eq!(reply["ok"], true);
    assert_eq!(reply["peer"]["session_id"], "same");
    assert!(reply["peer"]["pid"].is_number());
    assert!(reply["peer"]["socket_path"].is_string());
    assert!(!reply.to_string().contains("fixture-secret"));
    let wire = worker.join().unwrap();
    assert!(wire.contains("[redacted] hello"));
    assert!(!wire.contains("fixture-secret"));
    let ledger = fs::read_to_string(dir.path().join("home/peers/messages.jsonl")).unwrap();
    assert!(ledger.contains("[redacted] hello"));
    assert!(!ledger.contains("fixture-secret"));
    assert!(ledger.contains("637f5a69d3b12d04bc0050df9189dc19816f42fad4163fe35770a2c33559f152"));
    send(
        &mut socket,
        json!({"type":"call","id":3,"method":"stop","params":{}}),
    );
    for _ in 0..3 {
        if receive(&mut reader)["id"] == 3 {
            break;
        }
    }
    wait_until(|| process.exited());
    assert!(!peer_socket.exists());
}

#[test]
fn native_inbox_emits_scrubbed_peer_message_and_cleans_socket() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = dir.path().join("lore-fixture");
    fake_scrubber(&python, false);
    executable(&codex, "#!/bin/sh\nexit 0\n");
    let (_sender_listener, _) =
        registry_peer(dir.path(), "sender", dir.path().to_str().unwrap(), "sender");
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let peer_socket = PathBuf::from(process.entry()["socket_path"].as_str().unwrap());
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    doxa_peers::delivery::send(
        &peer_socket,
        &doxa_peers::delivery::PeerFrame {
            from_id: "sender".into(),
            from_title: "fixture-secret title".into(),
            sent_at: peer_now(),
            body: "fixture-secret body".into(),
            from_repo: Some(dir.path().display().to_string()),
            kind: None,
        },
    )
    .unwrap();
    let event = receive(&mut reader);
    assert_eq!(event["event"]["type"], "peer_message");
    assert_eq!(event["event"]["data"]["body"], "[redacted] body");
    assert!(!event.to_string().contains("fixture-secret"));
    send(
        &mut socket,
        json!({"type":"call","id":4,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
    assert!(!peer_socket.exists());
}
