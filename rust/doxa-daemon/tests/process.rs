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
    assert_eq!(entry["socket_path"], entry["daemon_socket"]);
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
    let mut process = Process::start(dir.path(), "0.25");
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
    thread::sleep(Duration::from_millis(350));
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
    let mut process = Process::start(dir.path(), "0.5");
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
        receive(&mut reader);
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
    assert!(fs::read_to_string(args)
        .unwrap()
        .contains("exec\nresume\nthread_1\n"));
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
            "#!/bin/sh\ncat >/dev/null\nsh -c 'sleep 1; echo leaked > {}' &\nwait\n",
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
    send(
        &mut socket,
        json!({"type":"call","id":3,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
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

    fs::remove_file(&process.registry).unwrap();
    fs::write(&process.registry, "replacement").unwrap();
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
