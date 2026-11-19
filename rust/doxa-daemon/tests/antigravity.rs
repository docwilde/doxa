#![cfg(target_os = "linux")]

use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{fs::PermissionsExt, net::UnixStream},
    path::Path,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

const SID: &str = "antigravity-fixture";
const MEMORY: &str = "Antigravity memory fixture uses graphite widgets.";
const SECRET: &str = "sk-abcdefghijklmnopqrstuvwxyz123456";

struct Process(Child);

impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn private_dir(path: &Path) {
    fs::create_dir(path).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
}

fn command(root: &Path) -> Command {
    let carrier = std::env::var_os("DOXA_LORE_RS").unwrap_or_else(|| "lore-rs".into());
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"));
    cmd.env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("DOXA_LORE_RS", carrier)
        .env("HOME", root.join("home"))
        .env("DOXA_HOME", root.join("doxa"))
        .env("LORE_ROOT", root.join("lore"))
        .env("LORE_PROJECTS_DIR", root.join("projects"))
        .env("LORE_SKILLS_DIR", root.join("skills"))
        .env("LORE_DISABLE_SYNC", "1")
        .env("LORE_DISABLE_REVIEW", "1")
        .env("DOXA_WORKTREE", "0")
        .env("GEMINI_API_KEY", "synthetic-key")
        .env("AGY_ADC_AUTH", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .args([
            "--session-id",
            SID,
            "--engine",
            "antigravity",
            "--linger",
            "10",
        ])
        .arg("--cwd")
        .arg(root.join("workspace"))
        .arg("--runtime-dir")
        .arg(root)
        .arg("--antigravity-bin")
        .arg(root.join("fake-agy"));
    cmd
}

fn setup(root: &Path) {
    for child in ["home", "doxa", "lore", "workspace"] {
        private_dir(&root.join(child));
    }
    let user = root.join("lore/USER.md");
    fs::write(&user, format!("# Memory\n\n- {MEMORY}\n")).unwrap();
    fs::set_permissions(user, fs::Permissions::from_mode(0o600)).unwrap();
    let provider = root.join("fake-agy");
    fs::write(&provider, include_str!("fixtures/agy-acp.py")).unwrap();
    fs::set_permissions(provider, fs::Permissions::from_mode(0o700)).unwrap();
}

struct Client {
    writer: UnixStream,
    reader: BufReader<UnixStream>,
    next: u64,
    buffered: VecDeque<Value>,
}

impl Client {
    fn attach(root: &Path, daemon: &mut Process) -> Self {
        let registry = root.join("registry").join(format!("{SID}.json"));
        let deadline = Instant::now() + Duration::from_secs(5);
        while !registry.exists() {
            if let Some(status) = daemon.0.try_wait().unwrap() {
                let mut error = String::new();
                daemon
                    .0
                    .stderr
                    .take()
                    .unwrap()
                    .read_to_string(&mut error)
                    .unwrap();
                panic!("Antigravity startup exited {status}: {error}");
            }
            assert!(Instant::now() < deadline, "Antigravity startup timed out");
            thread::sleep(Duration::from_millis(15));
        }
        let record: Value = serde_json::from_slice(&fs::read(registry).unwrap()).unwrap();
        let writer = UnixStream::connect(record["daemon_socket"].as_str().unwrap()).unwrap();
        writer
            .set_read_timeout(Some(Duration::from_secs(4)))
            .unwrap();
        let reader = BufReader::new(writer.try_clone().unwrap());
        let mut client = Self {
            writer,
            reader,
            next: 1,
            buffered: VecDeque::new(),
        };
        let hello = client.until(|frame| frame["type"] == "hello");
        assert_eq!(hello["engine"], "antigravity");
        assert_eq!(hello["model"], "fixture-neon");
        client.send(json!({"type": "attach", "cursor": null}));
        client
    }

    fn send(&mut self, frame: Value) {
        writeln!(self.writer, "{frame}").unwrap();
    }

    #[track_caller]
    fn until(&mut self, matches: impl Fn(&Value) -> bool) -> Value {
        if let Some(index) = self.buffered.iter().position(&matches) {
            return self.buffered.remove(index).unwrap();
        }
        for _ in 0..128 {
            let mut line = String::new();
            let n = self.reader.read_line(&mut line).unwrap_or_else(|error| {
                panic!(
                    "daemon frame missing: {error}, buffered {:?}",
                    self.buffered
                )
            });
            assert!(n > 0, "unexpected daemon EOF");
            let frame: Value = serde_json::from_str(&line).unwrap();
            assert!(
                !line.contains(SECRET),
                "unscrubbed secret escaped to client"
            );
            if matches(&frame) {
                return frame;
            }
            self.buffered.push_back(frame);
        }
        panic!("expected daemon frame was absent");
    }

    #[track_caller]
    fn event(&mut self, kind: &str) -> Value {
        self.until(|frame| frame["event"]["type"] == kind)["event"]["data"].clone()
    }

    fn call(&mut self, method: &str, params: Value) -> Value {
        let id = self.next;
        self.next += 1;
        self.send(json!({"type": "call", "id": id, "method": method, "params": params}));
        self.until(|frame| frame["type"] == "reply" && frame["id"] == id)
    }

    fn prompt(&mut self, text: &str) {
        let id = self.next;
        self.next += 1;
        self.send(json!({"type": "prompt", "id": id, "text": text}));
        assert_eq!(
            self.until(|frame| frame["type"] == "reply" && frame["id"] == id)["ok"],
            true
        );
    }

    fn stop(mut self, daemon: &mut Process) {
        assert_eq!(self.call("stop", json!({}))["ok"], true);
        let deadline = Instant::now() + Duration::from_secs(8);
        loop {
            if let Some(status) = daemon.0.try_wait().unwrap() {
                assert!(status.success(), "{status}");
                break;
            }
            assert!(Instant::now() < deadline, "Antigravity shutdown timed out");
            thread::sleep(Duration::from_millis(15));
        }
    }
}

fn calls(root: &Path) -> Vec<Value> {
    fs::read_to_string(root.join("workspace/fixture-acp.jsonl"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

#[test]
fn native_acp_turns_use_neon_lore_permissions_and_exact_resume() {
    let root = tempfile::tempdir().unwrap();
    let root = root.path();
    setup(root);
    let mut daemon = Process(
        command(root)
            .args(["--model", "Gemini 4 Neon", "--effort", "high"])
            .env("DOXA_LORE", "1")
            .spawn()
            .unwrap(),
    );
    let mut client = Client::attach(root, &mut daemon);
    assert_eq!(
        client.call("list_models", json!({}))["models"],
        json!(["fixture-default", "fixture-neon"])
    );
    client.prompt(&format!("do the first turn {SECRET}"));
    let delta = client.event("text_delta");
    assert!(delta["text"]
        .as_str()
        .unwrap()
        .starts_with("Fixture answer"));
    let permission = client.event("needs_input");
    assert_eq!(permission["require_full_review"], true);
    assert!(permission["input_summary"]
        .as_str()
        .unwrap()
        .contains("printf test"));
    assert_eq!(
        client.call(
            "answer_needs_input",
            json!({"id": "expired", "answer": {"decision": "allow"}})
        )["ok"],
        false
    );
    assert_eq!(
        client.call(
            "answer_needs_input",
            json!({"id": permission["id"], "answer": {"decision": "allow"},
        "reviewed_request": permission})
        )["applied"],
        true
    );
    assert_eq!(client.event("turn_done")["stop_reason"], "end_turn");
    client.stop(&mut daemon);

    let before = calls(root);
    let prompt = before
        .iter()
        .find(|frame| frame["method"] == "session/prompt")
        .unwrap();
    assert!(prompt["params"]["prompt"][0]["text"]
        .as_str()
        .unwrap()
        .contains(MEMORY));
    assert!(!prompt.to_string().contains(SECRET));
    let log = root
        .join("projects")
        .read_dir()
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path()
        .join(format!("{SID}.jsonl"));
    let saved = fs::read_to_string(&log).unwrap();
    assert!(saved.contains("Fixture answer") && saved.contains("[REDACTED:"));
    assert!(!saved.contains(MEMORY) && !saved.contains(SECRET));
    let carrier = std::env::var_os("DOXA_LORE_RS").unwrap_or_else(|| "lore-rs".into());
    let search = Command::new(carrier)
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("HOME", root.join("home"))
        .env("LORE_ROOT", root.join("lore"))
        .env("LORE_PROJECTS_DIR", root.join("projects"))
        .env("LORE_DISABLE_INDEX", "1")
        .env("LORE_DISABLE_SYNC", "1")
        .env("LORE_DISABLE_REVIEW", "1")
        .args(["search", "Fixture answer", "--cwd"])
        .arg(root.join("workspace"))
        .output()
        .unwrap();
    assert!(
        search.status.success(),
        "{}",
        String::from_utf8_lossy(&search.stderr)
    );
    let hits: Value = serde_json::from_slice(&search.stdout).unwrap();
    assert!(
        hits.as_array()
            .unwrap()
            .iter()
            .any(|hit| hit["session_id"] == SID),
        "LORE search should find the scrubbed Antigravity transcript"
    );

    let mut daemon = Process(
        command(root)
            .args(["--resume", "true"])
            .env("DOXA_LORE", "0")
            .spawn()
            .unwrap(),
    );
    let mut client = Client::attach(root, &mut daemon);
    assert_eq!(fs::read_to_string(&log).unwrap(), saved);
    client.prompt("please cancel");
    let pending = client.event("needs_input");
    assert_ne!(pending["id"], permission["id"]);
    assert_eq!(client.call("interrupt", json!({}))["ok"], true);
    assert_eq!(client.event("turn_done")["stop_reason"], "cancelled");
    client.stop(&mut daemon);

    let observed = calls(root);
    let resume = &observed[before.len()..];
    assert_eq!(
        resume
            .iter()
            .filter(|frame| frame["method"] == "session/new")
            .count(),
        0
    );
    let resumed = resume
        .iter()
        .find(|frame| frame["method"] == "session/resume")
        .unwrap();
    assert_eq!(
        resumed["params"]["sessionId"],
        "fixture_native_acp_conversation"
    );
    let prompts: Vec<_> = resume
        .iter()
        .filter(|frame| frame["method"] == "session/prompt")
        .collect();
    assert_eq!(prompts.len(), 1);
    assert_eq!(
        prompts[0]["params"]["prompt"],
        json!([{"type": "text", "text": "please cancel"}])
    );
    assert!(!fs::read_to_string(log).unwrap().contains(SECRET));
}

#[test]
fn missing_neon_and_dollar_ceiling_refuse_before_inference() {
    let root = tempfile::tempdir().unwrap();
    let root = root.path();
    setup(root);
    let budget = command(root)
        .env("DOXA_SESSION_BUDGET_USD", "1")
        .args(["--model", "Gemini 4 Neon"])
        .output()
        .unwrap();
    assert!(!budget.status.success());
    assert!(
        String::from_utf8_lossy(&budget.stderr).contains("cannot enforce a DOXA dollar ceiling")
    );
    assert!(!root.join("workspace/fixture-acp.jsonl").exists());

    let missing = command(root)
        .args(["--model", "Gemini 4 Neon Preview"])
        .output()
        .unwrap();
    assert!(!missing.status.success());
    assert!(String::from_utf8_lossy(&missing.stderr)
        .contains("unavailable in this Antigravity account"));
    assert_eq!(
        calls(root)
            .iter()
            .map(|request| request["method"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["initialize", "session/new"]
    );
}
