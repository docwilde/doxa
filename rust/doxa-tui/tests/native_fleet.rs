//! Native fleet integration uses only the deterministic daemon fixture.
//! Build workspace binaries first; no provider authentication or inference runs.
use doxa_tui::{fleet_control, fleet_view, transport::DaemonClient};
use serde_json::{json, Value};
use std::{fs, path::{Path, PathBuf}, process::{Child, Command, Stdio}, thread, time::{Duration, Instant}};
use std::os::unix::fs::PermissionsExt;

fn daemon() -> PathBuf {
    let path = std::env::var_os("DOXA_TEST_DAEMON_BIN").map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_BIN_EXE_doxa-rs")).with_file_name("doxa-daemon"));
    assert!(path.is_file(), "build doxa-daemon before native fleet integration tests");
    path
}
struct Fixture { dir: tempfile::TempDir, root: PathBuf, home: PathBuf }
impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let root = dir.path().join("f"); let home = dir.path().join("home");
        Self { dir, root, home }
    }
    fn command(&self) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_doxa-rs"));
        command.current_dir(self.dir.path()).env("DOXA_HOME", &self.home)
            .env("DOXA_DAEMON_BIN", daemon()).env("DOXA_WORKTREE", "0")
            .env("DOXA_RUNTIME_DIR", self.dir.path().join("unrelated-runtime"))
            .env_remove("DOXA_SESSION_BUDGET_USD").env_remove("DOXA_PEER_INBOUND_TURNS")
            .env_remove("DOXA_MODEL");
        command
    }
    fn start(&self, quiet: &str) -> Child {
        self.command().args(["fleet", "start", "--pool", "fixture", "--prompt", "same bounded fixture task",
            "-n", "2", "--allow-unbudgeted", "--run-id", "run", "--quiescence-grace", quiet,
            "--root", self.root.to_str().unwrap()]).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::inherit()).spawn().unwrap()
    }
    fn manifest(&self) -> Value { fleet_control::snapshot(&self.root, "run").unwrap() }
    fn wait_monitoring(&self) -> Value {
        wait_until(|| fleet_control::snapshot(&self.root, "run").is_ok_and(|value| value["phase"] == "monitoring"));
        self.manifest()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        // Clean only this isolated, identity-checked fixture runtime on a panic.
        if self.root.join("run/manifest.json").is_file() { let _ = fleet_view::stop(&self.root, "run"); }
    }
}
fn wait_until(mut predicate: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(15);
    while !predicate() { assert!(Instant::now() < deadline, "native fixture timed out"); thread::sleep(Duration::from_millis(20)); }
}
fn finish(child: &mut Child) {
    wait_until(|| child.try_wait().unwrap().is_some());
    assert!(child.wait().unwrap().success());
}
fn sockets_gone(value: &Value) -> bool {
    value["slots"].as_array().unwrap().iter().all(|row| !Path::new(row["socket_path"].as_str().unwrap()).exists())
}

#[test]
fn native_symmetric_fleet_dispatches_all_slots_and_tears_down_isolated_runtime() {
    let fixture = Fixture::new(); let mut child = fixture.start("0.2"); finish(&mut child);
    let manifest = fixture.manifest();
    assert_eq!(manifest["ledger_path"], fixture.root.join("run/home/peers/messages.jsonl").to_string_lossy().as_ref());
    assert_eq!(manifest["mode"], "symmetric"); assert_eq!(manifest["phase"], "finished");
    assert_eq!(manifest["live"], false); assert_eq!(manifest["quiesced"], true);
    let slots = manifest["slots"].as_array().unwrap(); assert_eq!(slots.len(), 2);
    assert!(slots.iter().all(|row| row["phase"] == "dispatched" && row["role"] == "worker"));
    assert!(sockets_gone(&manifest));
    assert!(!fixture.dir.path().join("unrelated-runtime").exists());
    assert!(!fixture.home.join("budgets").exists());
}

#[test]
fn controller_interrupt_reaches_verified_slot_teardown() {
    let fixture = Fixture::new(); let mut child = fixture.start("30"); fixture.wait_monitoring();
    unsafe { assert_eq!(libc::kill(child.id() as libc::pid_t, libc::SIGINT), 0); }
    finish(&mut child);
    let manifest = fixture.manifest(); assert_eq!(manifest["stopped"], true);
    assert_eq!(manifest["live"], false); assert_eq!(manifest["phase"], "finished");
    assert!(sockets_gone(&manifest));
}

#[test]
fn controller_resume_refuses_changed_identity_then_observes_without_redelivery() {
    let fixture = Fixture::new(); let mut child = fixture.start("30");
    let original = fixture.wait_monitoring();
    unsafe { assert_eq!(libc::kill(child.id() as libc::pid_t, libc::SIGKILL), 0); }
    child.wait().unwrap();
    let manifest_path = fixture.root.join("run/manifest.json");
    let mut changed = original.clone(); changed["slots"][0]["session_id"] = json!("changed-id");
    fs::write(&manifest_path, serde_json::to_vec(&changed).unwrap()).unwrap();
    let output = fixture.command().args(["fleet", "resume", "run", "--root", fixture.root.to_str().unwrap()]).output().unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("identity changed"));
    let mut restored = original.clone(); restored["spec"]["quiescence_grace_s"] = json!(0.2);
    fs::write(&manifest_path, serde_json::to_vec(&restored).unwrap()).unwrap();
    let mut observers = Vec::new();
    for row in original["slots"].as_array().unwrap() {
        let socket = row["socket_path"].as_str().unwrap();
        let client = DaemonClient::connect(socket, None).unwrap(); let head = client.hello["next_seq"].as_u64().unwrap(); drop(client);
        observers.push(DaemonClient::connect(socket, Some(head)).unwrap());
    }
    let output = fixture.command().args(["fleet", "resume", "run", "--root", fixture.root.to_str().unwrap()]).output().unwrap();
    assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    for observer in &mut observers {
        while let Ok(Some(frame)) = observer.poll_frame(Duration::from_millis(20)) {
            assert_ne!(frame["event"]["type"], "turn_started", "resume redelivered a provider prompt");
        }
    }
    let manifest = fixture.manifest(); assert_eq!(manifest["phase"], "finished"); assert!(sockets_gone(&manifest));
}

#[test]
fn ambiguous_dispatch_resume_never_creates_runtime_or_sends_a_prompt() {
    let fixture = Fixture::new(); let run = fixture.root.join("run");
    fs::create_dir_all(&run).unwrap(); fs::set_permissions(&fixture.root, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(&run, fs::Permissions::from_mode(0o700)).unwrap();
    let path = run.join("manifest.json");
    fs::write(&path, serde_json::to_vec(&json!({"native_version":1,"run_id":"run","phase":"dispatching","live":true,
        "slots":[{"phase":"dispatch_pending"}]})).unwrap()).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    let output = fixture.command().args(["fleet", "resume", "run", "--root", fixture.root.to_str().unwrap()]).output().unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("interrupted barrier"));
    assert!(!run.join("rt").exists());
}

#[test]
fn reviewed_prompt_digest_refuses_changed_input_before_launch() {
    let fixture = Fixture::new();
    let output = fixture.command().env("DOXA_FLEET_REVIEW_PROMPT_SHA256", "not-the-reviewed-digest")
        .args(["fleet", "start", "--pool", "fixture", "--prompt", "changed task", "-n", "1", "--allow-unbudgeted", "--run-id", "run", "--root", fixture.root.to_str().unwrap()]).output().unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("prompt changed after review"));
    assert!(!fixture.root.exists()); assert!(!fixture.home.exists());
}

#[test]
fn fleet_mesh_cli_stops_its_owned_loopback_renderer() {
    use std::io::{BufRead, BufReader};
    let fixture = Fixture::new(); let mut fleet = fixture.start("0.1"); finish(&mut fleet);
    let mut child = fixture.command().env("DOXA_MESH_OPEN_BROWSER", "0")
        .args(["fleet", "mesh", "run", "--root", fixture.root.to_str().unwrap()])
        .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::inherit()).spawn().unwrap();
    let mut reader = BufReader::new(child.stdout.take().unwrap()); let mut line = String::new();
    reader.read_line(&mut line).unwrap(); assert!(line.starts_with("mesh:"));
    line.clear(); reader.read_line(&mut line).unwrap();
    let address = line.trim().trim_start_matches("http://").split('/').next().unwrap().to_owned();
    assert!(std::net::TcpStream::connect(&address).is_ok());
    unsafe { assert_eq!(libc::kill(child.id() as libc::pid_t, libc::SIGINT), 0); }
    finish(&mut child);
    assert!(std::net::TcpStream::connect(address).is_err());
}
