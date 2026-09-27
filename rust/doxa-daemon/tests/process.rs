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

struct NativeDaemonCommand(Command);
fn daemon_command() -> NativeDaemonCommand {
    NativeDaemonCommand(Command::new(env!("CARGO_BIN_EXE_doxa-daemon")))
}
impl NativeDaemonCommand {
    fn args<I,S>(&mut self,args:I)->&mut Self where I:IntoIterator<Item=S>,S:AsRef<std::ffi::OsStr> {self.0.args(args);self}
    fn env<K,V>(&mut self,key:K,value:V)->&mut Self where K:AsRef<std::ffi::OsStr>,V:AsRef<std::ffi::OsStr> {self.0.env(key,value);self}
    fn env_remove<K:AsRef<std::ffi::OsStr>>(&mut self,key:K)->&mut Self {self.0.env_remove(key);self}
    fn stdout(&mut self,value:Stdio)->&mut Self {self.0.stdout(value);self}
    fn stderr(&mut self,value:Stdio)->&mut Self {self.0.stderr(value);self}
    fn current_dir<P:AsRef<Path>>(&mut self,path:P)->&mut Self {self.0.current_dir(path);self}
    fn isolate(&mut self) {
        let args:Vec<_>=self.0.get_args().map(|s|s.to_os_string()).collect();
        let position=args.iter().position(|arg|arg=="--runtime-dir").expect("owned runtime argument");
        let runtime=PathBuf::from(&args[position+1]);
        for (key,value) in [("HOME",runtime.join("fixture-home")),("DOXA_HOME",runtime.join("home")),
            ("CLAUDE_CONFIG_DIR",runtime.join("fixture-claude")),("CODEX_HOME",runtime.join("fixture-codex")),
            ("LORE_ROOT",runtime.join("native-lore")),("LORE_PROJECTS_DIR",runtime.join("native-projects")),
            ("LORE_CODEX_SESSIONS_DIR",runtime.join("native-codex-sessions")),("LORE_SKILLS_DIR",runtime.join("native-skills"))] {
            if !self.0.get_envs().any(|(name,_)|name==key) {self.0.env(key,value);}
        }
        self.0.env("LORE_DISABLE_SYNC","1").env("LORE_DISABLE_REVIEW","1");
    }
    fn spawn(&mut self)->std::io::Result<Child> {self.isolate();self.0.spawn()}
    fn output(&mut self)->std::io::Result<std::process::Output> {self.isolate();self.0.output()}
}
fn native_transcript_dir(runtime:&Path)->PathBuf {
    // These Codex/vendor fixtures have deliberately non-Git cwd equal to their
    // owned runtime. The canonical native project identity is that literal cwd.
    let slug:String=runtime.to_string_lossy().chars().map(|c|if c.is_ascii_alphanumeric(){c}else{'-'}).collect();
    runtime.join("native-projects").join(slug)
}
fn native_transcript(runtime:&Path,name:&str)->PathBuf {native_transcript_dir(runtime).join(name)}
fn native_memory_fixture(runtime:&Path,body:&str) {
    let root=runtime.join("native-lore");fs::create_dir_all(&root).unwrap();
    fs::set_permissions(&root,fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(root.join("USER.md"),body).unwrap();
    fs::set_permissions(root.join("USER.md"),fs::Permissions::from_mode(0o600)).unwrap();
}
fn native_role_count(path:&Path,role:&str)->usize {
    fs::read_to_string(path).unwrap_or_default().lines().filter_map(|line|serde_json::from_str::<Value>(line).ok()).filter(|row|row["type"]==role).count()
}
fn native_db_rows(runtime:&Path)->usize {
    let output=Command::new("/usr/bin/python3").args(["-c",r#"import sqlite3,sys
try:
 c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True,timeout=.2)
 print(c.execute('SELECT count(*) FROM msg').fetchone()[0])
except sqlite3.Error: print(0)
"#]).arg(runtime.join("native-lore/state.db")).output().unwrap();
    assert!(output.status.success());String::from_utf8(output.stdout).unwrap().trim().parse().unwrap()
}
struct NativeWriterLock(Child);
impl NativeWriterLock {
    fn acquire(runtime:&Path)->Self {
        let root=runtime.join("native-lore");fs::create_dir_all(&root).unwrap();
        fs::set_permissions(&root,fs::Permissions::from_mode(0o700)).unwrap();
        let ready=runtime.join("native-writer-ready");
        let child=Command::new("/usr/bin/python3").args(["-c",r#"import sqlite3,sys,pathlib
c=sqlite3.connect(sys.argv[1]);c.execute('PRAGMA journal_mode=WAL');c.execute('BEGIN IMMEDIATE')
pathlib.Path(sys.argv[2]).write_text('locked')
sys.stdin.readline();c.rollback();c.close()
"#]).arg(root.join("state.db")).arg(&ready).stdin(Stdio::piped()).stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap();
        let lock=Self(child);
        wait_until(||ready.exists());lock
    }
    fn release(&mut self) {self.0.stdin.take();wait_until(||self.0.try_wait().unwrap().is_some());}
}
impl Drop for NativeWriterLock {fn drop(&mut self){let _=self.0.kill();let _=self.0.wait();}}

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
        Self::start_in(runtime, runtime, linger)
    }
    fn start_in(runtime: &Path, cwd: &Path, linger: &str) -> Self {
        let child = daemon_command()
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
        Self::start_codex_with_inbound(runtime, codex, python, false)
    }
    fn start_codex_appserver(runtime: &Path, codex: &Path, python: &Path, resume: bool) -> Self {
        let mut command = daemon_command();
        command.args([
            "--runtime-dir", runtime.to_str().unwrap(), "--cwd", runtime.to_str().unwrap(),
            "--session-id", "codex-session", "--linger", "10", "--engine", "codex",
            "--codex-bin", codex.to_str().unwrap(), "--lore-python", python.to_str().unwrap(),
            "--resume", if resume { "true" } else { "false" },
        ]).stdout(Stdio::null()).stderr(Stdio::piped())
            .env("DOXA_HOME", runtime.join("home"))
            .env_remove("DOXA_CODEX_APPSERVER");
        if resume { command.env("DOXA_CODEX_APPSERVER", "0"); }
        let child = command.spawn().unwrap();
        let registry = runtime.join("registry/codex-session.json");
        wait_until(|| registry.exists());
        let entry: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
        Self { child, registry, socket: PathBuf::from(entry["daemon_socket"].as_str().unwrap()) }
    }
    fn start_codex_with_inbound(runtime: &Path, codex: &Path, python: &Path, inbound: bool) -> Self {
        let mut command = daemon_command();
        command
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
            .env("DOXA_CODEX_APPSERVER", "0");
        if inbound { command.env("DOXA_PEER_INBOUND_TURNS", "yes"); }
        let child = command.spawn().unwrap();
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
        Self::start_claude_with_budget(runtime, script, None)
    }
    fn start_claude_with_budget(runtime: &Path, script: &Path, budget: Option<&str>) -> Self {
        let mut command = daemon_command();
        command
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
            .env("DOXA_HOME", runtime.join("home"))
            .env_remove("DOXA_SESSION_BUDGET_USD");
        if let Some(budget) = budget { command.env("DOXA_SESSION_BUDGET_USD", budget); }
        let child = command.spawn().unwrap();
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

#[test]
fn daemon_runs_in_managed_worktree_and_cleans_it_on_real_exit() {
    let dir = tempfile::tempdir().unwrap();
    let main = dir.path().join("repo");
    let runtime = dir.path().join("runtime");
    let home = dir.path().join("home");
    fs::create_dir(&main).unwrap();
    let git = |args: &[&str]| {
        let output = Command::new("git").args(args).current_dir(&main).output().unwrap();
        assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    };
    git(&["init", "-q", "-b", "main"]);
    fs::write(main.join("README"), "seed\n").unwrap();
    git(&["add", "README"]);
    git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: seed"]);
    let mut child = daemon_command()
        .args(["--runtime-dir", runtime.to_str().unwrap(), "--cwd", main.to_str().unwrap(),
            "--session-id", "session123", "--linger", "10"])
        .env("DOXA_HOME", &home).env("DOXA_WORKTREE", "1")
        .stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap();
    let registry = runtime.join("registry/session123.json");
    wait_until(|| registry.exists());
    let row: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
    let worktree = home.join("worktrees/repo-session1");
    assert_eq!(row["cwd"], worktree.to_str().unwrap());
    assert_eq!(row["repo_root"], main.to_str().unwrap());
    assert!(worktree.join("README").exists());
    unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM); }
    wait_until(|| child.try_wait().unwrap().is_some());
    assert!(!worktree.exists());
    assert!(!home.join("worktrees/.meta/repo-session1.json").exists());
    assert!(!registry.exists());

    let mut child = daemon_command()
        .args(["--runtime-dir", runtime.to_str().unwrap(), "--cwd", main.to_str().unwrap(),
            "--session-id", "session234", "--linger", "10"])
        .env("DOXA_HOME", &home).env("DOXA_WORKTREE", "1")
        .stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap();
    let registry = runtime.join("registry/session234.json");
    wait_until(|| registry.exists());
    let kept = home.join("worktrees/repo-session2");
    fs::write(kept.join("user.txt"), "work to keep\n").unwrap();
    unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM); }
    wait_until(|| child.try_wait().unwrap().is_some());
    assert_eq!(fs::read_to_string(kept.join("user.txt")).unwrap(), "work to keep\n");
    assert!(home.join("worktrees/.meta/repo-session2.json").exists());
}

#[test]
fn requested_base_branch_is_honored_and_invalid_requests_never_fall_back() {
    let dir = tempfile::tempdir().unwrap();
    let main = dir.path().join("repo");
    let runtime = dir.path().join("runtime");
    let home = dir.path().join("home");
    fs::create_dir(&main).unwrap();
    let git = |args: &[&str]| {
        let output = Command::new("git").args(args).current_dir(&main).output().unwrap();
        assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    };
    git(&["init", "-q", "-b", "main"]);
    fs::write(main.join("README"), "main\n").unwrap();
    git(&["add", "README"]);
    git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: main"]);
    git(&["checkout", "-qb", "feature"]);
    fs::write(main.join("README"), "feature\n").unwrap();
    git(&["add", "README"]);
    git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: feature"]);
    git(&["checkout", "-q", "main"]);
    let args = ["--runtime-dir", runtime.to_str().unwrap(), "--cwd", main.to_str().unwrap(),
        "--session-id", "branch123", "--linger", "10", "--base-branch", "feature"];
    let mut child = daemon_command().args(args)
        .env("DOXA_HOME", &home).env("DOXA_WORKTREE", "1")
        .stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap();
    let registry = runtime.join("registry/branch123.json");
    wait_until(|| registry.exists());
    let worktree = home.join("worktrees/repo-branch12");
    assert_eq!(fs::read_to_string(worktree.join("README")).unwrap(), "feature\n");
    let meta: Value = serde_json::from_slice(&fs::read(home.join("worktrees/.meta/repo-branch12.json")).unwrap()).unwrap();
    assert_eq!(meta["base_ref"], "feature");
    assert_eq!(fs::read_to_string(main.join("README")).unwrap(), "main\n");
    unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM); }
    wait_until(|| child.try_wait().unwrap().is_some());

    for bad in ["missing", "--output=/tmp/unsafe"] {
        let result = daemon_command()
            .args(["--runtime-dir", runtime.to_str().unwrap(), "--cwd", main.to_str().unwrap(),
                "--session-id", "badbranch", "--base-branch", bad])
            .env("DOXA_HOME", &home).env("DOXA_WORKTREE", "1").output().unwrap();
        assert!(!result.status.success());
        assert!(!runtime.join("registry/badbranch.json").exists());
    }
    let disabled = daemon_command()
        .args(["--runtime-dir", runtime.to_str().unwrap(), "--cwd", main.to_str().unwrap(),
            "--session-id", "badbranch", "--base-branch", "feature"])
        .env("DOXA_HOME", &home).env("DOXA_WORKTREE", "0").output().unwrap();
    assert!(!disabled.status.success());
    assert!(!runtime.join("registry/badbranch.json").exists());
}
#[test]
fn managed_worktree_conflict_refuses_to_start_in_original_checkout() {
    let dir = tempfile::tempdir().unwrap();
    let main = dir.path().join("repo");
    let runtime = dir.path().join("runtime");
    let home = dir.path().join("home");
    fs::create_dir(&main).unwrap();
    let git = |args: &[&str]| {
        let output = Command::new("git").args(args).current_dir(&main).output().unwrap();
        assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    };
    git(&["init", "-q", "-b", "main"]);
    fs::write(main.join("README"), "seed\n").unwrap();
    git(&["add", "README"]);
    git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: seed"]);
    git(&["branch", "doxa/conflict"]);

    let args = ["--runtime-dir", runtime.to_str().unwrap(), "--cwd", main.to_str().unwrap(),
        "--session-id", "conflict123", "--linger", "10"];
    let rejected = daemon_command().args(args)
        .env("DOXA_HOME", &home).env("DOXA_WORKTREE", "1").output().unwrap();
    assert!(!rejected.status.success());
    assert!(String::from_utf8_lossy(&rejected.stderr).contains("managed worktree unavailable"));
    assert!(!runtime.join("registry/conflict123.json").exists());
    assert_eq!(fs::read_to_string(main.join("README")).unwrap(), "seed\n");

    // Explicitly disabling managed worktrees still permits the launch directory.
    let mut allowed = daemon_command().args(args)
        .env("DOXA_HOME", &home).env("DOXA_WORKTREE", "0")
        .stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap();
    let registry = runtime.join("registry/conflict123.json");
    wait_until(|| registry.exists());
    let row: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
    assert_eq!(row["cwd"], main.to_str().unwrap());
    unsafe { libc::kill(allowed.id() as libc::pid_t, libc::SIGTERM); }
    wait_until(|| allowed.try_wait().unwrap().is_some());

    // A plain directory has no Git checkout to isolate, so it remains usable.
    let plain = dir.path().join("plain");
    fs::create_dir(&plain).unwrap();
    let mut non_git = daemon_command()
        .args(["--runtime-dir", runtime.to_str().unwrap(), "--cwd", plain.to_str().unwrap(),
            "--session-id", "nogit123", "--linger", "10"])
        .env("DOXA_HOME", &home).env("DOXA_WORKTREE", "1")
        .stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap();
    let registry = runtime.join("registry/nogit123.json");
    wait_until(|| registry.exists());
    let row: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
    assert_eq!(row["cwd"], plain.to_str().unwrap());
    unsafe { libc::kill(non_git.id() as libc::pid_t, libc::SIGTERM); }
    wait_until(|| non_git.try_wait().unwrap().is_some());
}
fn executable(path: &Path, body: &str) {
    fs::write(path, body).unwrap();
    let mut perms = fs::metadata(path).unwrap().permissions();
    perms.set_mode(0o700);
    fs::set_permissions(path, perms).unwrap();
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
fn concurrent_process_cannot_claim_same_session_and_lock_survives_restart() {
    let dir = tempfile::tempdir().unwrap();
    let mut first = Process::start(dir.path(), "10");
    let owner = first.entry();
    let second = daemon_command()
        .args(["--runtime-dir", dir.path().to_str().unwrap(), "--cwd", dir.path().to_str().unwrap(),
            "--session-id", "fixture-session", "--linger", "10"])
        .env("DOXA_HOME", dir.path().join("home"))
        .output().unwrap();
    assert!(!second.status.success());
    assert!(String::from_utf8_lossy(&second.stderr).contains("session is already active"),
        "{}", String::from_utf8_lossy(&second.stderr));
    assert!(!first.exited());
    assert_eq!(first.entry()["pid"], owner["pid"]);

    let (mut reader, mut socket) = first.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"call","id":1,"method":"stop","params":{}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| first.exited());
    wait_until(|| !first.registry.exists());
    assert!(dir.path().join("registry/fixture-session.lock").exists());
    let mut restarted = Process::start(dir.path(), "10");
    assert_ne!(restarted.entry()["pid"], owner["pid"]);
    let (mut reader, mut socket) = restarted.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"call","id":1,"method":"stop","params":{}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| restarted.exited());
}

#[test]
fn legacy_registry_entry_blocks_resume_before_claude_host_starts() {
    let dir = tempfile::tempdir().unwrap();
    let registry = dir.path().join("runtime/registry");
    fs::create_dir_all(&registry).unwrap();
    let entry = registry.join("legacy-session.json");
    let marker = dir.path().join("claude-host-opened");
    let sidecar = dir.path().join("claude-sidecar.py");
    fs::write(&sidecar, format!("from pathlib import Path\nPath({}).write_text('opened')\n",
        serde_json::to_string(marker.to_str().unwrap()).unwrap())).unwrap();
    let run_resume = || daemon_command()
        .args(["--runtime-dir", dir.path().join("runtime").to_str().unwrap(),
            "--cwd", dir.path().to_str().unwrap(), "--session-id", "legacy-session",
            "--engine", "claude", "--claude-python", "/usr/bin/python3",
            "--claude-script", sidecar.to_str().unwrap(), "--resume", "true"])
        .env("DOXA_HOME", dir.path().join("home"))
        .output().unwrap();

    fs::write(&entry, b"{\"session_id\":\"legacy-session\"}\n").unwrap();
    let result = run_resume();
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains("session registry entry already exists"));
    assert!(!marker.exists(), "legacy session state was opened before collision check");
    assert!(registry.join("legacy-session.lock").exists());

    fs::remove_file(&entry).unwrap();
    std::os::unix::fs::symlink(dir.path().join("missing-entry"), &entry).unwrap();
    let result = run_resume();
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains("session registry entry already exists"));
    assert!(!marker.exists(), "unsafe registry symlink was followed before host startup");
}

#[test]
fn missing_unowned_resume_directory_refuses_before_claude_host_starts() {
    let dir = tempfile::tempdir().unwrap();
    let missing = dir.path().join("missing-checkout");
    let marker = dir.path().join("claude-host-opened");
    let sidecar = dir.path().join("claude-sidecar.py");
    fs::write(&sidecar, format!("from pathlib import Path\nPath({}).write_text('opened')\n",
        serde_json::to_string(marker.to_str().unwrap()).unwrap())).unwrap();
    let result = daemon_command()
        .args(["--runtime-dir", dir.path().join("runtime").to_str().unwrap(),
            "--cwd", missing.to_str().unwrap(), "--session-id", "saved-session",
            "--engine", "claude", "--claude-python", "/usr/bin/python3",
            "--claude-script", sidecar.to_str().unwrap(), "--resume", "true"])
        .env("DOXA_HOME", dir.path().join("home"))
        .output().unwrap();
    assert!(!result.status.success());
    assert!(String::from_utf8_lossy(&result.stderr).contains("managed worktree recovery refused"));
    assert!(!missing.exists());
    assert!(!marker.exists(), "Claude host opened before checkout ownership proof");
}

#[test]
fn claude_resume_restores_verified_missing_managed_checkout() {
    let dir = tempfile::tempdir().unwrap();
    let main = dir.path().join("repo");
    let runtime = dir.path().join("runtime");
    let home = dir.path().join("home");
    fs::create_dir(&main).unwrap();
    let git = |args: &[&str]| {
        let output = Command::new("git").args(args).current_dir(&main).output().unwrap();
        assert!(output.status.success(), "git {:?}: {}", args, String::from_utf8_lossy(&output.stderr));
        String::from_utf8(output.stdout).unwrap().trim().to_owned()
    };
    git(&["init", "-q", "-b", "main"]);
    fs::write(main.join("README"), "seed\n").unwrap();
    git(&["add", "README"]);
    git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: seed"]);
    let oid = git(&["rev-parse", "HEAD"]);
    git(&["branch", "doxa/saved123", "main"]);
    let root = home.join("worktrees");
    let meta_dir = root.join(".meta");
    fs::create_dir_all(&meta_dir).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    fs::set_permissions(&meta_dir, fs::Permissions::from_mode(0o700)).unwrap();
    let checkout = root.join("repo-saved123");
    let sidecar = meta_dir.join("repo-saved123.json");
    fs::write(&sidecar, json!({"main_root":main,"branch":"doxa/saved123",
        "base_ref":"main","base_oid":oid,"session_id":"saved123-session"}).to_string()).unwrap();
    fs::set_permissions(&sidecar, fs::Permissions::from_mode(0o600)).unwrap();
    let script = dir.path().join("claude-sidecar.py");
    fs::write(&script, r#"import json, sys
print(json.dumps({'type':'hello','protocol':'doxa-claude-sidecar','version':1,'capabilities':['start']}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({'type':'reply','id':request['id'],'ok':True,
        'result':{'data':{'model':'fixture'},'permission_mode':'default'}}), flush=True)
"#).unwrap();
    let mut child = daemon_command()
        .args(["--runtime-dir", runtime.to_str().unwrap(), "--cwd", checkout.to_str().unwrap(),
            "--session-id", "saved123-session", "--engine", "claude",
            "--claude-python", "/usr/bin/python3", "--claude-script", script.to_str().unwrap(),
            "--resume", "true", "--linger", "10"])
        .env("DOXA_HOME", &home).stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap();
    let registry = runtime.join("registry/saved123-session.json");
    wait_until(|| registry.exists() || child.try_wait().unwrap().is_some());
    if !registry.exists() {
        let output = child.wait_with_output().unwrap();
        panic!("resume failed: {}", String::from_utf8_lossy(&output.stderr));
    }
    let row: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
    assert_eq!(row["cwd"], checkout.to_str().unwrap());
    assert_eq!(fs::read_to_string(checkout.join("README")).unwrap(), "seed\n");
    unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGTERM); }
    wait_until(|| child.try_wait().unwrap().is_some());
    assert!(checkout.exists(), "recovered checkout must survive daemon exit");
    assert!(sidecar.exists());
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
    let output = daemon_command()
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
    let output = daemon_command()
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
fn rejects_inbound_turn_starting_without_lore_before_binding() {
    let dir = tempfile::tempdir().unwrap();
    let output = daemon_command()
        .args(["--runtime-dir", dir.path().to_str().unwrap(), "--session-id", "fleet-slot"])
        .env("DOXA_PEER_INBOUND_TURNS", "yes")
        .output().unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("inbound peer turns require an engine with LORE scrub"));
    assert!(!dir.path().join("registry/fleet-slot.json").exists());
}

#[test]
fn rejects_invalid_ceiling_before_binding() {
    let dir = tempfile::tempdir().unwrap();
    for value in ["NaN", "-2", "not-a-number"] {
        let output = daemon_command()
            .args(["--runtime-dir", dir.path().to_str().unwrap(), "--session-id", "fleet-slot"])
            .env("DOXA_SESSION_BUDGET_USD", value)
            .output().unwrap();
        assert!(!output.status.success(), "{value}");
        assert!(!dir.path().join("registry/fleet-slot.json").exists());
    }
}

#[test]
fn rejects_budgeted_codex_without_selected_price_basis() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    executable(&codex, "#!/bin/sh\nexit 0\n");
    for (model, expected) in [(None, "budgeted Codex session requires a priced model"),
        (Some("gpt-reserve"), "no native budget price for selected Codex model")] {
        let mut command = daemon_command();
        command.args(["--runtime-dir", dir.path().to_str().unwrap(), "--session-id", "fleet-slot",
            "--engine", "codex", "--codex-bin", codex.to_str().unwrap(),
            "--lore-python", python.to_str().unwrap()])
            .env("DOXA_HOME", dir.path().join("home"))
            .env("DOXA_SESSION_BUDGET_USD", "1.0");
        if let Some(model) = model { command.args(["--model", model]); }
        let output = command.output().unwrap();
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains(expected));
        assert!(!dir.path().join("registry/fleet-slot.json").exists());
    }
}

#[test]
fn rejects_unpriced_vendor_budget_before_binding() {
    let dir = tempfile::tempdir().unwrap();
    let python = Path::new("/usr/bin/python3");
    let output = daemon_command()
        .args(["--runtime-dir", dir.path().to_str().unwrap(), "--session-id", "fleet-slot",
            "--engine", "glm", "--model", "glm-5-turbo", "--lore-python", python.to_str().unwrap()])
        .env("DOXA_SESSION_BUDGET_USD", "1.0")
        .output().unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("no native budget price for glm:glm-5-turbo"));
    assert!(!dir.path().join("registry/fleet-slot.json").exists());
}

#[test]
fn codex_host_resumes_and_scrubs_provider_events() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    let args = dir.path().join("argv.txt");
    let prompt = dir.path().join("prompt.txt");
    executable(
        &codex,
        &format!(
            r#"#!/bin/sh
printf '%s\n' "$@" >> '{}'
echo END >> '{}'
cat >> '{}'
echo '{{"type":"thread.started","thread_id":"thread_1"}}'
echo '{{"type":"item.completed","item":{{"type":"agent_message","text":"sk-ownedCanonicalFixtureSecret1234567890 answer"}}}}'
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
    for (id, text) in [(1, "sk-ownedCanonicalFixtureSecret1234567890 first prompt"), (2, "second prompt")] {
        send(&mut socket, json!({"type":"prompt","id":id,"text":text}));
        assert_eq!(receive(&mut reader)["ok"], true);
        let mut kinds = Vec::new();
        loop {
            let frame = receive(&mut reader);
            assert!(!frame.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
            let event = &frame["event"];
            if event["type"] != "prompt_dequeued" {
                kinds.push(event["type"].as_str().unwrap().to_owned());
            }
            if event["type"] == "turn_started" {
                assert_eq!(
                    event["data"]["prompt"],
                    text.replace("sk-ownedCanonicalFixtureSecret1234567890", "[REDACTED:api-key]")
                );
            }
            if event["type"] == "text_delta" {
                assert_eq!(event["data"]["text"], "[REDACTED:api-key] answer");
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
    let provider_stdin=fs::read_to_string(prompt).unwrap();
    assert!(provider_stdin.starts_with("[DOXA MEMORY -- not typed by the user]"));
    assert!(provider_stdin.ends_with("[END OF MEMORY]\n\nsk-ownedCanonicalFixtureSecret1234567890 first promptsecond prompt"));
    assert_eq!(provider_stdin.matches("[DOXA MEMORY -- not typed by the user]").count(),1);
    send(
        &mut socket,
        json!({"type":"call","id":3,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
}

#[test]
fn codex_host_indexes_completed_turn_and_finalized_transcript() {
    let dir=tempfile::tempdir().unwrap();let codex=dir.path().join("codex-fixture");
    let python=Path::new("/usr/bin/python3");
    executable(&codex,"#!/bin/sh\ncat >/dev/null\necho '{\"type\":\"thread.started\",\"thread_id\":\"thread_1\"}'\necho '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"answer\"}}'\n");
    native_memory_fixture(dir.path(),"- poisoned optional context\n");
    let source=dir.path().join("native-lore/USER.md");fs::remove_file(&source).unwrap();
    let target=dir.path().join("unused-context-target");
    fs::write(&target,"- owned context must never be followed\n").unwrap();
    std::os::unix::fs::symlink(&target,&source).unwrap();
    let mut lock=NativeWriterLock::acquire(dir.path());
    let mut process=Process::start_codex(dir.path(),&codex,python);
    let (mut reader,mut socket)=process.connect();receive(&mut reader);
    send(&mut socket,json!({"type":"attach","cursor":null}));
    send(&mut socket,json!({"type":"prompt","id":1,"text":"hello"}));assert_eq!(receive(&mut reader)["ok"],true);
    loop {let frame=receive(&mut reader);if frame["event"]["type"]=="turn_done" {assert_eq!(frame["event"]["data"]["is_error"],false);break;}}
    send(&mut socket,json!({"type":"call","id":3,"method":"status","params":{}}));
    assert_eq!(receive(&mut reader)["status"]["running"],false);
    assert_eq!(native_db_rows(dir.path()),0,"held native writer must block indexing, not turn completion");
    lock.release();wait_until(||native_db_rows(dir.path())==2);
    send(&mut socket,json!({"type":"call","id":2,"method":"stop","params":{}}));assert_eq!(receive(&mut reader)["ok"],true);wait_until(||process.exited());
    assert_eq!(native_db_rows(dir.path()),2,"finalization must not duplicate native indexed messages");
}

#[test]
fn blocked_codex_index_does_not_delay_scrubbing_or_disable_later_turns() {
    let dir=tempfile::tempdir().unwrap();let codex=dir.path().join("codex-fixture");
    executable(&codex,"#!/bin/sh\ncat >/dev/null\necho '{\"type\":\"thread.started\",\"thread_id\":\"thread_1\"}'\necho '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"sk-ownedCanonicalFixtureSecret1234567890 answer\"}}'\n");
    native_memory_fixture(dir.path(),"- poisoned optional context\n");
    let source=dir.path().join("native-lore/USER.md");fs::remove_file(&source).unwrap();
    let target=dir.path().join("unused-context-target");
    fs::write(&target,"- owned context must never be followed\n").unwrap();
    std::os::unix::fs::symlink(&target,&source).unwrap();
    let mut lock=NativeWriterLock::acquire(dir.path());
    let mut process=Process::start_codex(dir.path(),&codex,Path::new("/usr/bin/python3"));
    let (mut reader,mut socket)=process.connect();receive(&mut reader);send(&mut socket,json!({"type":"attach","cursor":null}));
    let mut turn=|id| {
        let start=Instant::now();send(&mut socket,json!({"type":"prompt","id":id,"text":"sk-ownedCanonicalFixtureSecret1234567890 hello"}));assert_eq!(receive(&mut reader)["ok"],true);
        loop {let frame=receive(&mut reader);assert!(!frame.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));if frame["event"]["type"]=="turn_done" {assert_eq!(frame["event"]["data"]["is_error"],false,"{frame}");break;}}
        assert!(start.elapsed()<Duration::from_secs(2),"optional native indexing delayed mandatory scrub");
    };
    turn(1);turn(2);assert_eq!(native_db_rows(dir.path()),0);
    // The native SQLite busy deadline must expire while the provider/scrub
    // paths remain available; there is no Python child to kill anymore.
    thread::sleep(Duration::from_secs(6));turn(3);drop(turn);
    lock.release();wait_until(||native_db_rows(dir.path())==6);
    let transcript=fs::read_to_string(native_transcript(dir.path(),"codex-session.jsonl")).unwrap();
    assert!(!transcript.contains("sk-ownedCanonicalFixtureSecret1234567890"));assert_eq!(native_role_count(&native_transcript(dir.path(),"codex-session.jsonl"),"user"),3);
    send(&mut socket,json!({"type":"call","id":4,"method":"stop","params":{}}));assert_eq!(receive(&mut reader)["ok"],true);wait_until(||process.exited());
}

#[test]
fn codex_memory_reaches_only_first_provider_stdin_and_not_transcript() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    let first = dir.path().join("first-prompt.txt");
    let resumed = dir.path().join("resumed-prompts.txt");
    native_memory_fixture(dir.path(), "- durable memory\n");
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
    for (id, prompt) in [(1, "sk-ownedCanonicalFixtureSecret1234567890 first"), (2, "second")] {
        send(&mut socket, json!({"type":"prompt","id":id,"text":prompt}));
        assert_eq!(receive(&mut reader)["ok"], true);
        loop {
            let frame = receive(&mut reader);
            assert!(!frame.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
            if frame["event"]["type"] == "turn_started" {
                assert_eq!(
                    frame["event"]["data"]["prompt"],
                    prompt.replace("sk-ownedCanonicalFixtureSecret1234567890", "[REDACTED:api-key]")
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
    assert!(first_text.contains("durable memory"));
    assert!(first_text
        .ends_with("[END OF MEMORY]\n\nsk-ownedCanonicalFixtureSecret1234567890 first"));
    assert_eq!(
        fs::read_to_string(resumed).unwrap(),
        "second\nEND\nthird\nEND\n"
    );
    let transcript = fs::read_to_string(native_transcript(dir.path(), "codex-session.jsonl")).unwrap();
    assert!(!transcript.contains("DOXA MEMORY"));
    assert!(!transcript.contains("durable memory"));
    assert!(!transcript.contains("sk-ownedCanonicalFixtureSecret1234567890"));
    assert!(transcript.contains("[REDACTED:api-key] first"));
}

#[test]
fn unavailable_lore_snapshot_does_not_block_a_scrubbable_codex_turn() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    let captured = dir.path().join("stdin.txt");
    native_memory_fixture(dir.path(), "- unused memory\n");
    let source=dir.path().join("native-lore/USER.md");fs::remove_file(&source).unwrap();
    let target=dir.path().join("outside-memory");
    fs::write(&target,"- outside memory must never be followed\n").unwrap();
    std::os::unix::fs::symlink(&target,&source).unwrap();
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
        json!({"type":"prompt","id":1,"text":"sk-ownedCanonicalFixtureSecret1234567890 prompt"}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    loop {
        let event = receive(&mut reader);
        assert!(!event.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
        if event["event"]["type"] == "turn_done" {
            assert_eq!(event["event"]["data"]["is_error"], false);
            break;
        }
    }
    assert_eq!(
        fs::read_to_string(captured).unwrap(),
        "sk-ownedCanonicalFixtureSecret1234567890 prompt"
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
    let python = Path::new("/usr/bin/python3");
    let args = dir.path().join("argv.txt");
    executable(
        &codex,
        &format!(
            r#"#!/bin/sh
printf '%s\n' "$@" >> '{}'
cat >/dev/null
echo '{{"type":"thread.started","thread_id":"thread_1"}}'
echo '{{"type":"item.completed","item":{{"type":"agent_message","text":"sk-ownedCanonicalFixtureSecret1234567890 answer"}}}}'
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
                native_transcript(dir.path(), "codex-session.jsonl")
                    .to_str()
                    .unwrap()
            );
            assert!(hello["transcript_bytes"].as_u64().unwrap() > 0);
        }
        send(&mut socket, json!({"type":"attach","cursor":null}));
        send(
            &mut socket,
            json!({"type":"prompt","id":1,"text":format!("sk-ownedCanonicalFixtureSecret1234567890 prompt {index}")}),
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
    let records: Vec<Value> = fs::read_to_string(native_transcript(dir.path(), "codex-session.jsonl"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(records.len(), 4);
    assert_eq!(records[0]["type"], "user");
    assert_eq!(records[0]["message"]["content"], "[REDACTED:api-key] prompt 0");
    assert_eq!(records[1]["type"], "assistant");
    assert_eq!(
        records[1]["message"]["content"][0]["text"],
        "[REDACTED:api-key] answer"
    );
    assert!(records.iter().all(
        |record| record["engine"] == "codex" && !record.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890")
    ));
    let thread: Value = serde_json::from_slice(
        &fs::read(native_transcript(dir.path(), "codex-session.codex.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(thread["thread_id"], "thread_1");
    assert_eq!(thread["session_id"], "codex-session");
    assert_eq!(thread["turn_incomplete"], false);
    assert_eq!(thread["transcript_bytes"].as_u64(), Some(
        fs::metadata(native_transcript(dir.path(), "codex-session.jsonl")).unwrap().len()));
    assert!(fs::read_to_string(args)
        .unwrap()
        .contains("exec\nresume\nthread_1\n"));
}

#[test]
fn codex_clean_checkpoint_failure_overrides_success_and_preserves_dirty_resume_guard() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    let ready=dir.path().join("checkpoint-ready");let release=dir.path().join("checkpoint-release");
    executable(&codex,&format!("#!/bin/sh\ncat >/dev/null\necho '{{\"type\":\"thread.started\",\"thread_id\":\"thread_1\"}}'\ntouch '{}'\nattempt=0\nwhile [ ! -e '{}' ]; do attempt=$((attempt+1)); [ $attempt -le 1000 ] || exit 1; sleep .01; done\n",ready.display(),release.display()));
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"owned prompt"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    let transcript=native_transcript(dir.path(),"codex-session.jsonl");
    // The host buffers assistant output until EOF. A successful empty answer
    // isolates the clean checkpoint from the separately tested append failure.
    let thread_path=native_transcript(dir.path(),"codex-session.codex.json");
    wait_until(||ready.exists()&&native_role_count(&transcript,"user")==1
        &&fs::read(&thread_path).ok().and_then(|bytes|serde_json::from_slice::<Value>(&bytes).ok())
            .is_some_and(|state|state["turn_incomplete"]==true));
    fs::rename(&transcript,dir.path().join("saved-complete.jsonl")).unwrap();
    fs::write(dir.path().join("checkpoint-fault"),"owned checkpoint fault").unwrap();fs::write(&release,"").unwrap();
    loop {
        let frame = receive(&mut reader);
        if frame["event"]["type"] == "turn_done" {
            assert_eq!(frame["event"]["data"]["is_error"], true);
            assert!(frame["event"]["data"]["error"].as_str().unwrap().contains("persistence failed"));
            break;
        }
    }
    assert!(dir.path().join("checkpoint-fault").exists());
    let thread: Value = serde_json::from_slice(&fs::read(
        native_transcript(dir.path(), "codex-session.codex.json")).unwrap()).unwrap();
    assert_eq!(thread["turn_incomplete"], true);
    assert!(thread.get("transcript_bytes").is_none());
    // Even after the complete content reappears, the failed checkpoint must
    // not become a clean/resumable turn or permit another provider execution.
    fs::rename(dir.path().join("saved-complete.jsonl"),
               native_transcript(dir.path(), "codex-session.jsonl")).unwrap();
    send(&mut socket, json!({"type":"call","id":2,"method":"stop","params":{}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
    let restart = daemon_command()
        .args(["--runtime-dir", dir.path().to_str().unwrap(), "--cwd", dir.path().to_str().unwrap(),
            "--session-id", "codex-session", "--engine", "codex", "--codex-bin", codex.to_str().unwrap(),
            "--lore-python", python.to_str().unwrap(), "--resume", "true"])
        .env("DOXA_HOME", dir.path().join("home")).output().unwrap();
    assert!(!restart.status.success());
    assert!(String::from_utf8_lossy(&restart.stderr).contains("transcript is incomplete"));
}

#[test]
fn codex_resume_refuses_changed_clean_checkpoint_before_provider_execution() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    let marker = dir.path().join("unexpected-provider-start");
    executable(&codex, &format!("#!/bin/sh\ntouch '{}'\n", marker.display()));
    let project = native_transcript_dir(dir.path());
    fs::create_dir_all(&project).unwrap();
    let transcript = project.join("codex-session.jsonl");
    let original = b"{\"type\":\"user\"}\n{\"type\":\"assistant\"}\n";
    fs::write(&transcript, original).unwrap();
    fs::write(project.join("codex-session.codex.json"), json!({
        "thread_id":"thread_1","session_id":"codex-session","cwd":dir.path(),
        "model":null,"turn_incomplete":false,"transcript_bytes":original.len()
    }).to_string()).unwrap();
    for changed in [original[..original.len()-1].to_vec(), [original.as_slice(), b"{}\n"].concat()] {
        fs::write(&transcript, changed).unwrap();
        let output = daemon_command()
            .args(["--runtime-dir", dir.path().to_str().unwrap(), "--cwd", dir.path().to_str().unwrap(),
                "--session-id", "codex-session", "--engine", "codex", "--codex-bin", codex.to_str().unwrap(),
                "--lore-python", python.to_str().unwrap(), "--resume", "true"])
            .env("DOXA_HOME", dir.path().join("home")).output().unwrap();
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("durable checkpoint"));
        assert!(!marker.exists());
        assert!(!dir.path().join("registry/codex-session.json").exists());
    }
}

#[test]
fn codex_prompt_append_failure_withholds_provider_execution() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    let invoked = dir.path().join("provider-invoked");
    executable(&codex, &format!("#!/bin/sh\ntouch '{}'\n", invoked.display()));
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    let transcript = native_transcript(dir.path(), "codex-session.jsonl");
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
    let python = Path::new("/usr/bin/python3");
    let invoked = dir.path().join("provider-invoked");
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
    fs::create_dir(native_transcript(dir.path(), "codex-session.codex.json")).unwrap();
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
    let python = Path::new("/usr/bin/python3");
    let ready = dir.path().join("provider-ready");
    let release = dir.path().join("provider-release");
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
    let transcript = native_transcript(dir.path(), "codex-session.jsonl");
    let saved = native_transcript(dir.path(), "saved-user.jsonl");
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
        &fs::read(native_transcript(dir.path(), "codex-session.codex.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(thread["thread_id"], "thread_1");
    assert_eq!(thread["turn_incomplete"], true);
    send(&mut socket, json!({"type":"call","id":2,"method":"stop","params":{}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
    let restart = daemon_command()
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
    let python = Path::new("/usr/bin/python3");
    executable(&codex, "#!/bin/sh\necho started > should-not-start\n");
    fs::create_dir_all(native_transcript_dir(dir.path())).unwrap();
    fs::write(
        native_transcript(dir.path(), "codex-session.jsonl"),
        "{\"type\":\"user\",\"engine\":\"codex\"}\n",
    )
    .unwrap();
    let output = daemon_command()
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
fn explicit_codex_resume_requires_matching_thread_metadata() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    executable(&codex, "#!/bin/sh\necho started > should-not-start\n");
    let project = native_transcript_dir(dir.path());
    fs::create_dir_all(&project).unwrap();
    let transcript = project.join("codex-session.jsonl");
    let thread = project.join("codex-session.codex.json");
    let run = || daemon_command()
        .args(["--runtime-dir", dir.path().to_str().unwrap(),
            "--cwd", dir.path().to_str().unwrap(), "--session-id", "codex-session",
            "--engine", "codex", "--codex-bin", codex.to_str().unwrap(),
            "--lore-python", python.to_str().unwrap(), "--resume", "true"])
        .output().unwrap();
    assert!(!run().status.success(), "resume without saved state must fail");
    fs::write(&transcript, b"{\"type\":\"user\"}\n").unwrap();
    for state in [
        json!({"thread_id":"thread_1","session_id":"other","cwd":dir.path()}),
        json!({"thread_id":"thread_1","session_id":"codex-session","cwd":"/wrong"}),
        json!({"thread_id":"-unsafe","session_id":"codex-session","cwd":dir.path()}),
        json!({"thread_id":"thread_1","session_id":"codex-session","cwd":dir.path()}),
        json!({"thread_id":"thread_1","session_id":"codex-session","cwd":dir.path(),"turn_incomplete":true}),
    ] {
        fs::write(&thread, state.to_string()).unwrap();
        let output = run();
        assert!(!output.status.success(), "bad state must refuse resume");
        assert!(!project.join("should-not-start").exists());
        assert!(!dir.path().join("registry/codex-session.json").exists());
    }
    fs::write(&thread, json!({"thread_id":"thread_1","session_id":"codex-session",
        "cwd":dir.path(),"model":null,"turn_incomplete":false}).to_string()).unwrap();
    let mut resumed = daemon_command()
        .args(["--runtime-dir", dir.path().to_str().unwrap(),
            "--cwd", dir.path().to_str().unwrap(), "--session-id", "codex-session",
            "--engine", "codex", "--codex-bin", codex.to_str().unwrap(),
            "--lore-python", python.to_str().unwrap(), "--resume", "true"])
        .stdout(Stdio::null()).stderr(Stdio::null()).spawn().unwrap();
    wait_until(|| dir.path().join("registry/codex-session.json").exists());
    resumed.kill().unwrap();
    resumed.wait().unwrap();
    assert!(!dir.path().join("should-not-start").exists(), "provider must wait for a prompt");
}

#[test]
fn thread_identity_is_durable_before_turn_completes() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    executable(&codex, "#!/bin/sh\ncat >/dev/null\necho '{\"type\":\"thread.started\",\"thread_id\":\"thread_early\"}'\nsleep 10\n");
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    let path = native_transcript(dir.path(), "codex-session.codex.json");
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
fn claude_reported_spend_blocks_next_prompt_over_daemon_socket() {
    let dir = tempfile::tempdir().unwrap();
    let script = dir.path().join("claude-cost.py");
    fs::write(&script, r#"import json, sys
print(json.dumps({"type":"hello","protocol":"doxa-claude-sidecar","version":1}),flush=True)
for line in sys.stdin:
    frame=json.loads(line)
    print(json.dumps({"type":"reply","id":frame["id"],"ok":True,"result":{}}),flush=True)
    if frame["method"] == "prompt":
        print(json.dumps({"type":"event","event":"turn_done","data":{"cost_usd":1.1}}),flush=True)
"#).unwrap();
    let mut process = Process::start_claude_with_budget(dir.path(), &script, Some("1.0"));
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"first"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    assert_eq!(receive(&mut reader)["event"]["type"], "turn_done");
    send(&mut socket, json!({"type":"prompt","id":2,"text":"second"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    let refusal = receive(&mut reader);
    assert_eq!(refusal["event"]["type"], "turn_refused");
    assert_eq!(refusal["event"]["data"]["reason"], "budget");
    assert_eq!(refusal["event"]["data"]["spent_usd"], 1.1);
    unsafe { libc::kill(process.child.id() as libc::pid_t, libc::SIGTERM); }
    wait_until(|| process.exited());
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
    let python = Path::new("/usr/bin/python3");
    executable(&codex,r#"#!/usr/bin/python3
import sys,json
sys.stdin.read()
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'sk-ownedCanonicalFixtureSecret1234567890 '+('x'*1100000)}}),flush=True)
"#);
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    let mut frames = Vec::new();
    loop {
        let frame = receive(&mut reader);
        assert!(!frame.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
        let done = frame["event"]["type"] == "turn_done";
        frames.push(frame);
        if done {
            break;
        }
    }
    let done=frames.last().unwrap();
    assert_eq!(done["event"]["data"]["is_error"], true);
    assert!(done["event"]["data"]["error"]
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
    let python = Path::new("/usr/bin/python3");
    let marker = dir.path().join("survived");
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
    let thread_path = native_transcript(dir.path(), "codex-session.codex.json");
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
    let restart = daemon_command()
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
fn invalid_native_lore_capacity_rejects_session_before_socket_or_registry() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    executable(&codex, "#!/bin/sh\nexit 0\n");

    let output = daemon_command()
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
        .env("LORE_USER_CAP", "invalid-native-capacity")
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
    let mut child = daemon_command()
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
    let python = Path::new("/usr/bin/python3");
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
        json!({"type":"prompt","id":2,"text":"sk-ownedCanonicalFixtureSecret1234567890 queued"}),
    );
    let reply = receive(&mut first);
    assert_eq!(reply["queued"], true);
    let event = receive(&mut second);
    assert_eq!(event["event"]["type"], "prompt_queued");
    assert_eq!(event["event"]["data"]["text"], "[REDACTED:api-key] queued");
    assert!(!event.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
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
    let python = Path::new("/usr/bin/python3");
    let ready = dir.path().join("ready");
    let marker = dir.path().join("survived");
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
    let python = Path::new("/usr/bin/python3");
    let ready = dir.path().join("ready");
    let marker = dir.path().join("survived");
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

    fn fake_vendor(count: usize, answer: &str) -> (String, thread::JoinHandle<Vec<Value>>) {
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

    #[test]
    fn priced_vendor_budget_blocks_the_next_socket_prompt() {
        let dir = tempfile::tempdir().unwrap();
        let lore = Path::new("/usr/bin/python3");
        let body = "data: {\"model\":\"deepseek-flash\",\"choices\":[{\"finish_reason\":\"stop\",\"delta\":{\"content\":\"answer\"}}],\"usage\":{\"prompt_tokens\":1000000,\"completion_tokens\":1000000}}\n\ndata: [DONE]\n\n";
        let (endpoint, server) = fake_vendor_frames(vec![body.to_owned()]);
        let child = daemon_command()
            .args(["--runtime-dir", dir.path().to_str().unwrap(), "--cwd", dir.path().to_str().unwrap(),
                "--session-id", "vendor-session", "--linger", "10", "--engine", "deepseek",
                "--model", "deepseek-flash", "--lore-python", lore.to_str().unwrap(),
                "--vendor-endpoint", &endpoint])
            .env("DEEPSEEK_API_KEY", "test-key-1234")
            .env("DOXA_SESSION_BUDGET_USD", "1.0")
            .env("DOXA_HOME", dir.path().join("home"))
            .stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap();
        let registry = dir.path().join("registry/vendor-session.json");
        wait_until(|| registry.exists());
        let entry: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
        let mut process = Process { child, registry, socket: PathBuf::from(entry["daemon_socket"].as_str().unwrap()) };
        let (mut reader, mut socket) = process.connect();
        receive(&mut reader);
        send(&mut socket, json!({"type":"attach","cursor":null}));
        send(&mut socket, json!({"type":"prompt","id":1,"text":"question"}));
        assert_eq!(receive(&mut reader)["ok"], true);
        assert_eq!(receive(&mut reader)["event"]["type"], "turn_started");
        assert_eq!(receive(&mut reader)["event"]["type"], "text_delta");
        let done = receive(&mut reader);
        assert_eq!(done["event"]["data"]["cost_usd"], 1.5);
        send(&mut socket, json!({"type":"prompt","id":2,"text":"again"}));
        assert_eq!(receive(&mut reader)["ok"], true);
        let refused = receive(&mut reader);
        assert_eq!(refused["event"]["type"], "turn_refused");
        assert_eq!(refused["event"]["data"]["spent_usd"], 1.5);
        send(&mut socket, json!({"type":"call","id":3,"method":"stop","params":{}}));
        assert_eq!(receive(&mut reader)["ok"], true);
        wait_until(|| process.exited());
        assert_eq!(server.join().unwrap().len(), 1);
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
        let mut child = daemon_command()
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
            .env_remove("DOXA_LORE")
            .env("DOXA_AGENT_PEER_SEND", "1")
            .env("DOXA_VENDOR_TOOLS", if tools { "workspace-read" } else { "" })
            .env("DOXA_HOME", runtime.join("home"))
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let registry = runtime.join("registry/vendor-session.json");
        wait_until(|| {
            if registry.exists() { return true; }
            if child.try_wait().unwrap().is_some() {
                let mut detail=String::new();
                use std::io::Read;
                if let Some(stderr)=child.stderr.take(){stderr.take(2048).read_to_string(&mut detail).unwrap();}
                panic!("vendor fixture exited before registry: {detail}");
            }
            false
        });
        let entry: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
        let socket = PathBuf::from(entry["daemon_socket"].as_str().unwrap());
        Process {
            child,
            registry,
            socket,
        }
    }

    #[test]
    fn native_vendor_refreshes_private_system_memory_and_indexes_only_on_finalization() {
        for enabled in [true, false] {
            let dir = tempfile::tempdir().unwrap();
            fs::create_dir(dir.path().join("home")).unwrap();
            fs::write(dir.path().join("home/config.toml"), format!("lore = '{}'\n", if enabled { "1" } else { "0" })).unwrap();
            let lore=Path::new("/usr/bin/python3");
            native_memory_fixture(dir.path(), "- PRIVATE-DURABLE-MEMORY-1\n");
            let (endpoint, server) = fake_vendor(2, "answer");
            let mut process = start_vendor(dir.path(), "deepseek", &endpoint, &lore);
            let (mut reader, mut socket) = process.connect();
            assert_eq!(receive(&mut reader)["lore_enabled"], enabled);
            send(&mut socket, json!({"type":"attach","cursor":null}));
            for id in 1..=2 {
                native_memory_fixture(dir.path(), &format!("- PRIVATE-DURABLE-MEMORY-{id}\n"));
                send(&mut socket, json!({"type":"prompt","id":id,"text":"sk-ownedCanonicalFixtureSecret1234567890 question"}));
                loop { let frame=receive(&mut reader); assert!(!frame.to_string().contains("PRIVATE-DURABLE-MEMORY")); if frame["event"]["type"] == "turn_done" { assert_eq!(frame["event"]["data"]["is_error"],false); break; } }
            }
            assert_eq!(native_db_rows(dir.path()),0, "vendor indexing must wait for finalization");
            send(&mut socket, json!({"type":"call","id":3,"method":"stop","params":{}}));
            assert_eq!(receive(&mut reader)["ok"], true); wait_until(|| process.exited());
            let requests = server.join().unwrap();
            for (index, body) in requests.iter().enumerate() {
                assert_eq!(body["messages"][0]["role"],"system");
                assert!(body["messages"][0]["content"].as_str().unwrap().contains("DOXA session"));
                if enabled { assert!(body["messages"][0]["content"].as_str().unwrap().contains(&format!("PRIVATE-DURABLE-MEMORY-{}",index+1))); }
                else { assert!(!body.to_string().contains("PRIVATE-DURABLE-MEMORY")); }
            }
            assert_eq!(native_db_rows(dir.path()),if enabled {4}else{0});
            for file in ["vendor-session.jsonl","vendor-session.messages.json"] {
                let stored=fs::read_to_string(native_transcript_dir(dir.path()).join(file)).unwrap();
                assert!(!stored.contains("PRIVATE-DURABLE-MEMORY")); assert!(!stored.contains("DOXA session"));
                assert!(!stored.contains("sk-ownedCanonicalFixtureSecret1234567890")); assert!(stored.contains("[REDACTED:api-key] question"));
            }
        }
    }

    #[test]
    fn native_vendor_chat_preserves_scrubbed_history_usage_and_model() {
        for vendor in ["deepseek", "glm"] {
            let dir = tempfile::tempdir().unwrap();
            let lore = Path::new("/usr/bin/python3");
            let (endpoint, server) = fake_vendor(2, "sk-ownedCanonicalFixtureSecret1234567890 answer");
            let mut process = start_vendor(dir.path(), vendor, &endpoint, &lore);
            assert_eq!(process.entry()["engine"], vendor);
            let (mut reader, mut socket) = process.connect();
            let hello = receive(&mut reader);
            assert_eq!(hello["effort"], "high");
            assert_eq!(
                hello["model"],
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
                    json!({"type":"prompt","id":id,"text":"sk-ownedCanonicalFixtureSecret1234567890 prompt"}),
                );
                assert_eq!(receive(&mut reader)["ok"], true);
                let started = receive(&mut reader);
                assert_eq!(started["event"]["type"], "turn_started");
                assert_eq!(started["event"]["data"]["prompt"], "[REDACTED:api-key] prompt");
                let text = receive(&mut reader);
                assert_eq!(text["event"]["data"]["text"], "[REDACTED:api-key] answer", "{text}");
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
                .contains("model required"));
            send(
                &mut socket,
                json!({"type":"call","id":4,"method":"stop","params":{}}),
            );
            assert_eq!(receive(&mut reader)["ok"], true);
            wait_until(|| process.exited());
            let requests = server.join().unwrap();
            for request in &requests {
                let mut names:Vec<_>=request["tools"].as_array().unwrap().iter()
                    .map(|tool|tool["function"]["name"].as_str().unwrap()).collect();
                names.sort_unstable();
                assert_eq!(names,["mcp__doxa__lore_belief_neighbours", "mcp__doxa__lore_belief_search", "mcp__doxa__lore_belief_show", "mcp__doxa__lore_memory_list", "mcp__doxa__lore_remember", "mcp__doxa__lore_session_search", "mcp__doxa__peer_history", "mcp__doxa__peer_list", "mcp__doxa__peer_send"]);
            }
            assert_eq!(requests[1]["messages"][2]["content"], "[REDACTED:api-key] answer");
            assert!(!requests[1].to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
            // The published entry is removed at shutdown. Its private claim
            // inode remains so a later daemon cannot bypass an active flock by
            // racing a lockfile unlink/recreation.
            assert!(!process.registry.exists());
            let remaining: Vec<_> = fs::read_dir(dir.path().join("registry"))
                .unwrap()
                .map(|entry| entry.unwrap().file_name().into_string().unwrap())
                .collect();
            assert_eq!(remaining, ["vendor-session.lock"]);
            assert_eq!(
                fs::metadata(dir.path().join("registry/vendor-session.lock"))
                    .unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }

    #[test]
    fn vendor_effort_control_changes_the_next_turn_request() {
        for vendor in ["deepseek", "glm"] {
            let dir = tempfile::tempdir().unwrap();
            let lore = Path::new("/usr/bin/python3");
            let (endpoint, server) = fake_vendor(2, "answer");
            let mut process = start_vendor(dir.path(), vendor, &endpoint, &lore);
            let (mut reader, mut socket) = process.connect();
            assert_eq!(receive(&mut reader)["effort"], "high");
            send(&mut socket, json!({"type":"attach","cursor":null}));
            for id in 1..=2 {
                if id == 2 {
                    send(&mut socket, json!({"type":"call","id":10,"method":"set_effort",
                        "params":{"effort":"low"}}));
                    assert_eq!(receive(&mut reader)["effort"], "low");
                    assert_eq!(receive(&mut reader)["event"]["type"], "effort_changed");
                }
                send(&mut socket, json!({"type":"prompt","id":id,"text":"hello"}));
                assert_eq!(receive(&mut reader)["ok"], true);
                for _ in 0..3 { receive(&mut reader); }
            }
            send(&mut socket, json!({"type":"call","id":11,"method":"stop","params":{}}));
            assert_eq!(receive(&mut reader)["ok"], true);
            wait_until(|| process.exited());
            let requests = server.join().unwrap();
            let effort = |body: &Value| if vendor == "deepseek" {
                body["thinking"]["reasoning_effort"].as_str().unwrap().to_owned()
            } else { body["reasoning_effort"].as_str().unwrap().to_owned() };
            assert_eq!(effort(&requests[0]), "high");
            assert_eq!(effort(&requests[1]), "low");
        }
    }

    #[test]
    fn vendor_model_control_preserves_history_and_changes_next_request() {
        let dir = tempfile::tempdir().unwrap();
        let lore = Path::new("/usr/bin/python3");
        let (endpoint, server) = fake_vendor(2, "answer");
        let mut process = start_vendor(dir.path(), "deepseek", &endpoint, &lore);
        let (mut reader, mut socket) = process.connect();
        assert_eq!(receive(&mut reader)["can_set_model"], true);
        send(&mut socket, json!({"type":"attach","cursor":null}));
        for id in 1..=2 {
            if id == 2 {
                send(&mut socket, json!({"type":"call","id":10,"method":"set_model","params":{"model":"deepseek-v4-pro"}}));
                let reply = receive(&mut reader); assert_eq!(reply["ok"], true); assert_eq!(reply["model"], "deepseek-v4-pro");
                assert_eq!(receive(&mut reader)["event"]["type"], "model_changed");
            }
            send(&mut socket, json!({"type":"prompt","id":id,"text":"hello"}));
            while receive(&mut reader)["event"]["type"] != "turn_done" {}
        }
        send(&mut socket, json!({"type":"call","id":11,"method":"set_model","params":{"model":"unverified-model"}}));
        assert_eq!(receive(&mut reader)["ok"], false);
        send(&mut socket, json!({"type":"call","id":12,"method":"stop","params":{}}));
        assert_eq!(receive(&mut reader)["ok"], true); wait_until(|| process.exited());
        let requests = server.join().unwrap();
        assert_eq!(requests[0]["model"], "deepseek-flash");
        assert_eq!(requests[1]["model"], "deepseek-v4-pro");
        assert_eq!(requests[1]["messages"].as_array().unwrap().len(), 4);
    }

    #[test]
    fn vendor_workspace_read_is_opt_in_scrubbed_and_turn_local() {
        let dir = tempfile::tempdir().unwrap();
        let lore = Path::new("/usr/bin/python3");
        fs::write(dir.path().join("note.txt"), "sk-ownedCanonicalFixtureSecret1234567890 workspace note").unwrap();
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
        assert_eq!(started["event"]["data"]["vendor_tools"], "workspace-read, peers, lore");
        assert_eq!(receive(&mut reader)["event"]["data"]["text"], "Final answer");
        assert_eq!(receive(&mut reader)["event"]["data"]["is_error"], false);
        send(&mut socket, json!({"type":"call","id":2,"method":"stop","params":{}}));
        assert_eq!(receive(&mut reader)["ok"], true);
        wait_until(|| process.exited());
        let requests = server.join().unwrap();
        assert_eq!(requests[0]["tools"][0]["function"]["name"], "workspace_read");
        let result = requests[1]["messages"][3]["content"].as_str().unwrap();
        assert!(result.contains("[REDACTED:api-key] workspace note"));
        assert!(!requests[1].to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
        let saved: Value = serde_json::from_slice(&fs::read(native_transcript(dir.path(), "vendor-session.messages.json")).unwrap()).unwrap();
        assert_eq!(saved["messages"].as_array().unwrap().len(), 2);
        assert_eq!(saved["messages"][1]["content"], "Final answer");
    }

    #[test]
    fn vendor_restart_replays_scrubbed_history_and_rejects_corrupt_state() {
        for vendor in ["deepseek", "glm"] {
            let dir = tempfile::tempdir().unwrap();
            let lore = Path::new("/usr/bin/python3");
            let (endpoint, first_server) = fake_vendor(1, "sk-ownedCanonicalFixtureSecret1234567890 first");
            let mut first = start_vendor(dir.path(), vendor, &endpoint, &lore);
            let (mut reader, mut socket) = first.connect();
            receive(&mut reader);
            send(&mut socket, json!({"type":"attach","cursor":null}));
            send(
                &mut socket,
                json!({"type":"prompt","id":1,"text":"sk-ownedCanonicalFixtureSecret1234567890 prompt"}),
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
            let state = native_transcript(dir.path(), "vendor-session.messages.json");
            let transcript = native_transcript(dir.path(), "vendor-session.jsonl");
            let records: Vec<Value> = fs::read_to_string(&transcript)
                .unwrap()
                .lines()
                .map(|line| serde_json::from_str(line).unwrap())
                .collect();
            assert_eq!(records.len(), 2);
            assert_eq!(records[0]["message"]["content"], "[REDACTED:api-key] prompt");
            assert_eq!(
                records[1]["message"]["content"][0]["text"],
                "[REDACTED:api-key] first"
            );
            assert!(records.iter().all(|record| record["engine"] == vendor));
            let saved: Value = serde_json::from_slice(&fs::read(&state).unwrap()).unwrap();
            assert_eq!(saved["engine"], vendor);
            assert_eq!(saved["session_id"], "vendor-session");
            assert_eq!(saved["messages"][0]["content"], "[REDACTED:api-key] prompt");
            assert_eq!(saved["messages"][1]["content"], "[REDACTED:api-key] first");
            assert!(!saved.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));

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
            assert_eq!(requests[0]["messages"][1]["content"], "[REDACTED:api-key] prompt");
            assert_eq!(requests[0]["messages"][2]["content"], "[REDACTED:api-key] first");
            assert_eq!(requests[0]["messages"][3]["content"], "continue");
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
            let output = daemon_command()
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
            let output = daemon_command()
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
    fn native_vendor_oversized_provider_text_is_withheld_without_history_commit() {
        let dir = tempfile::tempdir().unwrap();
        let lore = Path::new("/usr/bin/python3");
        let oversized=format!("sk-ownedCanonicalFixtureSecret1234567890 {}","x".repeat(1100000));
        let (endpoint, server) = fake_vendor(1, &oversized);
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
        assert!(!done.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
        send(
            &mut socket,
            json!({"type":"call","id":2,"method":"stop","params":{}}),
        );
        assert_eq!(receive(&mut reader)["ok"], true);
        wait_until(|| process.exited());
        assert_eq!(server.join().unwrap().len(), 1);
        assert!(!native_transcript(dir.path(),"vendor-session.jsonl").exists(),
            "failed provider streams must not commit partial turns");
        assert!(!native_transcript(dir.path(), "vendor-session.messages.json")
            .exists());
    }

    #[test]
    fn vendor_transcript_write_failure_poisoned_session_without_history_commit() {
        let dir = tempfile::tempdir().unwrap();
        let lore = Path::new("/usr/bin/python3");
        let (endpoint, server) = fake_vendor(1, "answer");
        let mut process = start_vendor(dir.path(), "deepseek", &endpoint, &lore);
        let (mut reader, mut socket) = process.connect();
        receive(&mut reader);
        send(&mut socket, json!({"type":"attach","cursor":null}));
        let target = dir.path().join("outside");
        fs::write(&target, b"untouched").unwrap();
        let transcript = native_transcript(dir.path(), "vendor-session.jsonl");
        std::os::unix::fs::symlink(&target, &transcript).unwrap();
        send(&mut socket, json!({"type":"prompt","id":1,"text":"hello"}));
        assert_eq!(receive(&mut reader)["ok"], true);
        assert_eq!(receive(&mut reader)["event"]["type"], "turn_started");
        let done = receive(&mut reader);
        assert_eq!(done["event"]["data"]["is_error"], true);
        assert_eq!(fs::read(&target).unwrap(), b"untouched");
        assert!(!native_transcript(dir.path(), "vendor-session.messages.json")
            .exists());
        send(&mut socket, json!({"type":"prompt","id":2,"text":"again"}));
        assert_eq!(receive(&mut reader)["ok"], true);
        // The prior turn_done can arrive before the runtime worker releases
        // its running slot, so the second request may briefly queue.
        let mut refused = receive(&mut reader);
        for _ in 0..2 {
            if refused["event"]["type"] == "turn_done" { break; }
            assert!(matches!(refused["event"]["type"].as_str(),
                Some("prompt_queued" | "prompt_dequeued")), "{refused}");
            refused = receive(&mut reader);
        }
        assert_eq!(refused["event"]["type"], "turn_done", "{refused}");
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
        let lore = Path::new("/usr/bin/python3");
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
        let lore = Path::new("/usr/bin/python3");
        let output = daemon_command()
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
    let python = Path::new("/usr/bin/python3");
    executable(&codex, "#!/bin/sh\nexit 0\n");
    let (_same_listener, same_path) = registry_peer(
        dir.path(),
        "same",
        dir.path().to_str().unwrap(),
        "sk-ownedCanonicalFixtureSecret1234567890 teammate",
    );
    let (_other_listener, _) = registry_peer(
        dir.path(),
        "other",
        "/other-project",
        "sk-ownedCanonicalFixtureSecret1234567890 outsider",
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
    assert_eq!(reply["peers"][0]["title"], "[REDACTED:api-key] teammate");
    assert!(!reply.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
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
fn peers_rpc_fails_closed_when_native_configuration_is_invalid() {
    let dir=tempfile::tempdir().unwrap();
    let (listener,_)=registry_peer(dir.path(),"same",dir.path().to_str().unwrap(),"sk-ownedCanonicalFixtureSecret1234567890 title");
    let child=daemon_command().args(["--runtime-dir",dir.path().to_str().unwrap(),"--cwd",dir.path().to_str().unwrap(),"--session-id","fixture-session","--linger","10"])
        .env("LORE_USER_CAP","not-a-capacity").stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap();
    let registry=dir.path().join("registry/fixture-session.json");wait_until(||registry.exists());
    let row:Value=serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();let mut process=Process{child,registry,socket:PathBuf::from(row["daemon_socket"].as_str().unwrap())};
    let (mut reader,mut socket)=process.connect();receive(&mut reader);send(&mut socket,json!({"type":"attach","cursor":null}));
    for (id,method,params) in [(1,"peers",json!({})),(3,"msg",json!({"target":"same","text":"sk-ownedCanonicalFixtureSecret1234567890 message"}))] {
        send(&mut socket,json!({"type":"call","id":id,"method":method,"params":params}));let reply=receive(&mut reader);
        assert_eq!(reply["ok"],false);assert!(reply.get("peers").is_none());assert!(!reply.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
    }
    assert!(!dir.path().join("home/peers/messages.jsonl").exists());listener.set_nonblocking(true).unwrap();
    while let Ok((mut stream,_))=listener.accept(){let mut bytes=Vec::new();stream.read_to_end(&mut bytes).unwrap();assert!(bytes.is_empty(),"native admission refusal must not deliver peer data");}
    send(&mut socket,json!({"type":"call","id":2,"method":"stop","params":{}}));assert_eq!(receive(&mut reader)["ok"],true);wait_until(||process.exited());
}

#[test]
fn native_msg_sends_scrubbed_frame_records_ledger_and_denies_other_scope() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
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
        "params":{"target":"other","text":"sk-ownedCanonicalFixtureSecret1234567890"}}),
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
        "params":{"target":"same","text":"sk-ownedCanonicalFixtureSecret1234567890 hello"}}),
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
    assert!(!reply.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
    let wire = worker.join().unwrap();
    assert!(wire.contains("[REDACTED:api-key] hello"));
    assert!(!wire.contains("sk-ownedCanonicalFixtureSecret1234567890"));
    let ledger = fs::read_to_string(dir.path().join("home/peers/messages.jsonl")).unwrap();
    assert!(ledger.contains("[REDACTED:api-key] hello"));
    assert!(!ledger.contains("sk-ownedCanonicalFixtureSecret1234567890"));
    assert!(ledger.contains("66e50b5e76495eb415cb9d9d3c5e02327f188673a0f393df258fad21d6a33470"));
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
    let python = Path::new("/usr/bin/python3");
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
            from_title: "sk-ownedCanonicalFixtureSecret1234567890 title".into(),
            sent_at: peer_now(),
            body: "sk-ownedCanonicalFixtureSecret1234567890 body".into(),
            from_repo: Some(dir.path().display().to_string()),
            kind: None,
        },
    )
    .unwrap();
    let event = receive(&mut reader);
    assert_eq!(event["event"]["type"], "peer_message");
    assert_eq!(event["event"]["data"]["body"], "[REDACTED:api-key] body");
    assert!(!event.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
    send(
        &mut socket,
        json!({"type":"call","id":4,"method":"stop","params":{}}),
    );
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
    assert!(!peer_socket.exists());
}

#[test]
fn inbound_direct_peer_starts_scrubbed_turn_but_broadcast_does_not() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    let captured = dir.path().join("captured-prompt");
    executable(&codex, &format!(r#"#!/bin/sh
cat >> '{}'
echo '{{"type":"thread.started","thread_id":"thread_1"}}'
echo '{{"type":"item.completed","item":{{"type":"agent_message","text":"done"}}}}'
"#, captured.display()));
    let (_sender_listener, _) = registry_peer(dir.path(), "sender", dir.path().to_str().unwrap(), "sender");
    let mut process = Process::start_codex_with_inbound(dir.path(), &codex, &python, true);
    let peer_socket = PathBuf::from(process.entry()["socket_path"].as_str().unwrap());
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    let frame = |kind, body: &str| doxa_peers::delivery::PeerFrame {
        from_id: "sender".into(), from_title: "sk-ownedCanonicalFixtureSecret1234567890 title".into(),
        sent_at: peer_now(), body: body.into(),
        from_repo: Some(dir.path().display().to_string()), kind,
    };
    doxa_peers::delivery::send(&peer_socket, &frame(Some("broadcast".into()), "broadcast note")).unwrap();
    assert_eq!(receive(&mut reader)["event"]["type"], "peer_message");
    send(&mut socket, json!({"type":"call","id":1,"method":"status","params":{}}));
    let status = receive(&mut reader);
    assert_eq!(status["status"]["running"], false);
    assert_eq!(status["status"]["queued"], 0);
    doxa_peers::delivery::send(&peer_socket, &frame(Some("direct".into()), "sk-ownedCanonicalFixtureSecret1234567890 body")).unwrap();
    assert_eq!(receive(&mut reader)["event"]["type"], "peer_message");
    let mut started = false;
    loop {
        let event = receive(&mut reader);
        assert!(event["turn"].as_str().unwrap_or("").starts_with("peer-"));
        if event["event"]["type"] == "turn_started" {
            started = true;
            assert_eq!(event["event"]["data"]["peer_started"], true);
            assert!(event["event"]["data"]["peer_origin"].as_str().unwrap().contains("sender"));
            let prompt = event["event"]["data"]["prompt"].as_str().unwrap();
            assert!(prompt.starts_with("[PEER-STARTED TURN]"));
            assert!(prompt.contains("[PEER MESSAGES -- UNTRUSTED]"));
            assert!(prompt.contains("[REDACTED:api-key] body"));
            assert!(!prompt.contains("sk-ownedCanonicalFixtureSecret1234567890"));
        }
        if event["event"]["type"] == "turn_done" { break; }
    }
    assert!(started);
    let provider_prompt = fs::read_to_string(captured).unwrap();
    assert!(provider_prompt.contains("[PEER-STARTED TURN]"));
    assert!(provider_prompt.contains("[REDACTED:api-key] body"));
    assert!(provider_prompt.contains("broadcast note"));
    assert!(!provider_prompt.contains("sk-ownedCanonicalFixtureSecret1234567890"));
    send(&mut socket, json!({"type":"call","id":2,"method":"stop","params":{}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
}

#[test]
fn inbound_peer_uses_typed_prompt_queue_while_turn_runs() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    let release = dir.path().join("release-first");
    executable(&codex, &format!(r#"#!/bin/sh
cat >/dev/null
if [ ! -f '{}' ]; then
  while [ ! -f '{}' ]; do sleep 0.01; done
fi
echo '{{"type":"thread.started","thread_id":"thread_1"}}'
echo '{{"type":"item.completed","item":{{"type":"agent_message","text":"done"}}}}'
"#, release.display(), release.display()));
    let (_sender_listener, _) = registry_peer(dir.path(), "sender", dir.path().to_str().unwrap(), "sender");
    let mut process = Process::start_codex_with_inbound(dir.path(), &codex, &python, true);
    let peer_socket = PathBuf::from(process.entry()["socket_path"].as_str().unwrap());
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"first"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    assert_eq!(receive(&mut reader)["event"]["type"], "turn_started");
    doxa_peers::delivery::send(&peer_socket, &doxa_peers::delivery::PeerFrame {
        from_id: "sender".into(), from_title: "sender".into(), sent_at: peer_now(),
        body: "peer task".into(), from_repo: None, kind: Some("direct".into()),
    }).unwrap();
    assert_eq!(receive(&mut reader)["event"]["type"], "peer_message");
    let queued = receive(&mut reader);
    assert_eq!(queued["event"]["type"], "prompt_queued");
    assert_eq!(queued["event"]["data"]["peer_started"], true);
    assert_eq!(queued["event"]["data"]["position"], 1);
    send(&mut socket, json!({"type":"call","id":2,"method":"status","params":{}}));
    let status = receive(&mut reader);
    assert_eq!(status["status"]["running"], true);
    assert_eq!(status["status"]["queued"], 1);
    fs::write(&release, b"go").unwrap();
    let mut saw_dequeue = false;
    let mut saw_peer = false;
    for _ in 0..8 {
        let frame = receive(&mut reader);
        if frame["event"]["type"] == "prompt_dequeued" { saw_dequeue = true; }
        if frame["event"]["type"] == "turn_started" &&
            frame["turn"].as_str().unwrap_or("").starts_with("peer-") { saw_peer = true; }
        if saw_peer && frame["event"]["type"] == "turn_done" { break; }
    }
    assert!(saw_dequeue && saw_peer);
    send(&mut socket, json!({"type":"call","id":3,"method":"stop","params":{}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
}

#[test]
fn native_daemon_queue_rpc_scrubs_and_cancels_before_turn_starts() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    let ready = dir.path().join("provider-ready");
    let release = dir.path().join("provider-release");
    executable(&codex, &format!(r#"#!/bin/sh
cat >/dev/null
touch '{}'
while [ ! -f '{}' ]; do sleep 0.01; done
echo '{{"type":"thread.started","thread_id":"thread_1"}}'
echo '{{"type":"item.completed","item":{{"type":"agent_message","text":"done"}}}}'
"#, ready.display(), release.display()));
    let mut process = Process::start_codex(dir.path(), &codex, &python);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"first"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    assert_eq!(receive(&mut reader)["event"]["type"], "turn_started");
    wait_until(|| ready.exists());
    send(&mut socket, json!({"type":"prompt","id":2,"text":"sk-ownedCanonicalFixtureSecret1234567890 queued"}));
    assert_eq!(receive(&mut reader)["queue_id"], "q1");
    send(&mut socket, json!({"type":"call","id":3,"method":"queue","params":{}}));
    let queued = receive(&mut reader);
    assert_eq!(queued["queue"], json!([{"id":"q1","text":"[REDACTED:api-key] queued"}]));
    assert!(!queued.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
    send(&mut socket, json!({"type":"call","id":4,"method":"cancel_queued","params":{"id":"q1"}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    let cancelled = receive(&mut reader);
    assert_eq!(cancelled["event"]["type"], "prompt_cancelled");
    assert_eq!(cancelled["event"]["data"]["text"], "[REDACTED:api-key] queued");
    send(&mut socket, json!({"type":"call","id":5,"method":"queue","params":{}}));
    assert_eq!(receive(&mut reader)["queue"], json!([]));
    fs::write(&release, "go").unwrap();
    loop {
        if receive(&mut reader)["event"]["type"] == "turn_done" { break; }
    }
    send(&mut socket, json!({"type":"call","id":6,"method":"stop","params":{}}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| process.exited());
}

#[test]
fn codex_appserver_default_streams_persists_and_resumes() {
    let cache = std::env::var_os("TMPDIR").map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::env::var_os("XDG_CACHE_HOME").map(std::path::PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|home| std::path::PathBuf::from(home).join(".cache")))
            .unwrap_or_else(|| std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target"))
            .join("doxa-tests"));
    std::fs::create_dir_all(&cache).unwrap();
    let dir = tempfile::tempdir_in(cache).unwrap();
    let codex = dir.path().join("codex-appserver-fixture");
    let python = Path::new("/usr/bin/python3");
    let log = dir.path().join("methods.log");
    let script = r#"#!/usr/bin/env python3
import json, sys, tomllib

def read(): return json.loads(sys.stdin.readline())
def send(v): print(json.dumps(v),flush=True)
log = open('__LOG__','a')
init=read(); assert init['method']=='initialize'
send({'id':init['id'],'result':{'userAgent':'codex_cli_rs/0.156.1'}})
assert read()['method']=='initialized'
thread=read()
if thread['method']=='hooks/list':
    overrides=[sys.argv[i+1] for i,x in enumerate(sys.argv[:-1]) if x=='-c']
    hooks=next(tomllib.loads(x)['hooks'] for x in overrides if x.startswith('hooks='))
    key=next(iter(hooks['state']))
    row={'key':key,'command':hooks['PreCompact'][0]['hooks'][0]['command'],
         'handlerType':'command','enabled':True,'trustStatus':'trusted',
         'currentHash':hooks['state'][key]['trusted_hash'],'eventName':'preCompact',
         'source':'sessionFlags','timeoutSec':240,'async':False}
    send({'id':thread['id'],'result':{'data':[{'cwd':thread['params']['cwds'][0],'hooks':[row]}],'errors':[]}})
    thread=read()
if thread['method']=='model/list':
    send({'id':thread['id'],'result':{'data':[{'model':'gpt-test','hidden':False,'isDefault':True,'supportedReasoningEfforts':[{'reasoningEffort':'low'},{'reasoningEffort':'high'}],'defaultReasoningEffort':'low'}, {'model':'no-reasoning','hidden':False,'supportedReasoningEfforts':[]}, {'model':'hidden-model','hidden':True,'supportedReasoningEfforts':[]}], 'nextCursor':None}})
    sys.exit(0)
log.write(thread['method']+'\n'); log.flush()
assert thread['method'] in ('thread/start','thread/resume')
send({'id':thread['id'],'result':{'thread':{'id':'thread-1'},'model':'gpt-test'}})
turn=read(); assert turn['method']=='turn/start'
assert 'sk-ownedCanonicalFixtureSecret1234567890' in turn['params']['input'][0]['text']
if 'second' in turn['params']['input'][0]['text']:
    assert turn['params']['model']=='gpt-test' and turn['params']['effort']=='high'
send({'id':turn['id'],'result':{'turn':{'id':'turn_1'}}})
send({'method':'item/reasoning/textDelta','params':{'threadId':'thread-1','turnId':'turn_1','itemId':'r','delta':'sk-ownedCanonicalFixtureSecret1234567890 thought'}})
send({'method':'item/started','params':{'threadId':'thread-1','turnId':'turn_1','item':{'type':'commandExecution','id':'cmd_1','command':'echo sk-ownedCanonicalFixtureSecret1234567890'}}})
send({'method':'item/completed','params':{'threadId':'thread-1','turnId':'turn_1','item':{'type':'commandExecution','id':'cmd_1','command':'echo sk-ownedCanonicalFixtureSecret1234567890','status':'completed','aggregatedOutput':'sk-ownedCanonicalFixtureSecret1234567890 tool output','exitCode':0}}})
send({'method':'item/agentMessage/delta','params':{'threadId':'thread-1','turnId':'turn_1','itemId':'a','delta':'sk-ownedCanonicalFixtureSecret1234567890 answer'}})
send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread-1','turnId':'turn_1','tokenUsage':{'total':{'inputTokens':100,'outputTokens':50,'cachedInputTokens':10},'last':{'totalTokens':20000,'reasoningOutputTokens':7},'modelContextWindow':32000}}})
send({'method':'turn/completed','params':{'threadId':'thread-1','turn':{'id':'turn_1','status':'completed','error':None}}})
for line in sys.stdin: pass
"#.replace("__LOG__", log.to_str().unwrap());
    let (setup, body) = script.split_once("turn=read();").unwrap();
    let body = format!("turn=json.loads(line);{}", body.split("for line in sys.stdin: pass").next().unwrap());
    let script = format!("{setup}for line in sys.stdin:\n{}", body.lines().map(|line| format!("    {line}\n")).collect::<String>());
    executable(&codex, &script);
    for resume in [false, true] {
        let mut process = Process::start_codex_appserver(dir.path(), &codex, &python, resume);
        let (mut reader, mut socket) = process.connect();
        receive(&mut reader);
        send(&mut socket, json!({"type":"attach","cursor":null}));
        for prompt_id in [1, 2] {
        if prompt_id == 2 {
            for (method, params, expected) in [
                ("list_models", json!({}), true),
                ("set_model", json!({"model":"hidden-model"}), false),
                ("set_model", json!({"model":"no-reasoning"}), true),
                ("set_effort", json!({"effort":"high"}), false),
                ("set_model", json!({"model":"gpt-test"}), true),
                ("set_effort", json!({"effort":"unsupported"}), false),
                ("set_effort", json!({"effort":"high"}), true),
            ] {
                send(&mut socket, json!({"type":"call","id":10,"method":method,"params":params}));
                loop {
                    let reply = receive(&mut reader);
                    if reply["type"] == "reply" && reply["id"] == 10 {
                        assert_eq!(reply["ok"], expected, "{method}: {reply}");
                        if method == "list_models" { assert_eq!(reply["models"], json!(["gpt-test", "no-reasoning"])); }
                        if method == "set_model" && params["model"] == "no-reasoning" { assert!(reply["effort"].is_null()); }
                        break;
                    }
                }
            }
        }
        send(&mut socket, json!({"type":"prompt","id":prompt_id,"text":if prompt_id == 2 { "sk-ownedCanonicalFixtureSecret1234567890 second" } else { "sk-ownedCanonicalFixtureSecret1234567890 prompt" }}));
        loop {
            let reply = receive(&mut reader);
            if reply["type"] == "reply" && reply["id"] == prompt_id { assert_eq!(reply["ok"], true); break; }
        }
        let mut kinds = Vec::new();
        loop {
            let frame = receive(&mut reader);
            assert!(!frame.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
            let event = &frame["event"];
            let kind = event["type"].as_str().unwrap_or("");
            kinds.push(kind.to_owned());
            if kind == "turn_done" {
                assert_eq!(event["data"]["is_error"], false, "{event}");
                assert_eq!(event["data"]["ctx_tokens"], 8000);
                assert_eq!(event["data"]["ctx_percentage"], 40.0);
                assert!(event["data"]["reasoning_output_tokens"].is_null());
                assert_eq!(event["data"]["reasoning_count_is_estimate"], true);
                break;
            }
        }
        assert!(kinds.contains(&"reasoning_delta".to_owned()));
        assert!(kinds.contains(&"tool_call".to_owned()));
        assert!(kinds.contains(&"tool_result_detail".to_owned()));
        assert!(kinds.contains(&"text_delta".to_owned()));
        }
        send(&mut socket, json!({"type":"call","id":2,"method":"stop","params":{}}));
        assert_eq!(receive(&mut reader)["ok"], true);
        wait_until(|| process.exited());
    }
    assert_eq!(fs::read_to_string(log).unwrap(), "thread/start\nthread/resume\n");
    let thread: Value = serde_json::from_slice(&fs::read(native_transcript(dir.path(), "codex-session.codex.json")).unwrap()).unwrap();
    assert_eq!(thread["transport"], "app-server");
    assert_eq!(thread["turn_incomplete"], false);
    assert_eq!(thread["model"], "gpt-test");
    assert_eq!(thread["effort"], "high");
    let transcript = fs::read_to_string(native_transcript(dir.path(), "codex-session.jsonl")).unwrap();
    assert!(!transcript.contains("sk-ownedCanonicalFixtureSecret1234567890"));
    assert!(transcript.contains("[REDACTED:api-key] answer"));
}

#[test]
fn stopping_codex_during_unanswered_appserver_initialization_is_prompt() {
    let cache = std::env::var_os("TMPDIR").map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::env::var_os("XDG_CACHE_HOME").map(std::path::PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|home| std::path::PathBuf::from(home).join(".cache")))
            .unwrap_or_else(|| std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target"))
            .join("doxa-tests"));
    std::fs::create_dir_all(&cache).unwrap();
    let dir = tempfile::tempdir_in(cache).unwrap();
    let codex = dir.path().join("codex-never-initializes");
    let python = Path::new("/usr/bin/python3");
    let marker = dir.path().join("initialization-received");
    executable(&codex, &format!("#!/usr/bin/env python3\nimport sys,time\nsys.stdin.readline()\nopen({:?},'w').write('ready')\ntime.sleep(30)\n", marker.to_str().unwrap()));
    let mut process = Process::start_codex_appserver(dir.path(), &codex, &python, false);
    let (mut reader, mut socket) = process.connect();
    receive(&mut reader);
    send(&mut socket, json!({"type":"attach","cursor":null}));
    send(&mut socket, json!({"type":"prompt","id":1,"text":"not submitted"}));
    assert_eq!(receive(&mut reader)["ok"], true);
    wait_until(|| marker.exists());
    let started = Instant::now();
    send(&mut socket, json!({"type":"call","id":2,"method":"stop","params":{}}));
    loop {
        let frame = receive(&mut reader);
        if frame["type"] == "reply" && frame["id"] == 2 {
            assert_eq!(frame["ok"], true);
            break;
        }
    }
    wait_until(|| process.exited());
    assert!(started.elapsed() < Duration::from_secs(2), "stop waited for app-server RPC timeout");
}

#[test]
fn saved_exec_codex_settings_preserve_transport_and_resume_thread() {
    let dir = tempfile::tempdir().unwrap();
    let codex = dir.path().join("codex-fixture");
    let python = Path::new("/usr/bin/python3");
    let args = dir.path().join("args.log");
    executable(&codex, &format!(r#"#!/usr/bin/env python3
import json, sys
if sys.argv[1]=='app-server':
    def read(): return json.loads(sys.stdin.readline())
    def send(v): print(json.dumps(v),flush=True)
    init=read(); send({{'id':init['id'],'result':{{}}}})
    assert read()['method']=='initialized'
    req=read(); assert req['method']=='model/list'
    send({{'id':req['id'],'result':{{'data':[{{'model':'account-model','isDefault':True,'supportedReasoningEfforts':[{{'reasoningEffort':'high'}}],'defaultReasoningEffort':'high'}}], 'nextCursor':None}}}})
else:
    with open({:?},'a') as log: log.write(json.dumps(sys.argv[1:])+'\n')
    sys.stdin.read()
    print(json.dumps({{'type':'thread.started','thread_id':'thread_legacy'}}))
    print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':'answer'}}}}))
    print(json.dumps({{'type':'turn.completed','usage':{{'input_tokens':1,'output_tokens':1}}}}))
"#, args.to_str().unwrap()));
    for restart in [false, true] {
        let mut process = Process::start_codex(dir.path(), &codex, &python);
        let (mut reader, mut socket) = process.connect();
        receive(&mut reader);
        send(&mut socket, json!({"type":"attach","cursor":null}));
        if !restart {
            for (method, params) in [("set_model", json!({"model":"account-model"})), ("set_effort", json!({"effort":"high"}))] {
                send(&mut socket, json!({"type":"call","id":10,"method":method,"params":params}));
                loop { let reply = receive(&mut reader); if reply["type"] == "reply" && reply["id"] == 10 { assert_eq!(reply["ok"], true, "{reply}"); break; } }
            }
        }
        send(&mut socket, json!({"type":"prompt","id":11,"text":"prompt"}));
        loop { let frame = receive(&mut reader); if frame["event"]["type"] == "turn_done" { assert_eq!(frame["event"]["data"]["is_error"], false); break; } }
        send(&mut socket, json!({"type":"call","id":12,"method":"stop","params":{}}));
        assert_eq!(receive(&mut reader)["ok"], true);
        wait_until(|| process.exited());
    }
    let calls: Vec<Value> = fs::read_to_string(args).unwrap().lines().map(|line| serde_json::from_str(line).unwrap()).collect();
    assert_eq!(calls.len(), 2);
    for call in &calls { assert!(call.as_array().unwrap().contains(&json!("account-model"))); assert!(call.as_array().unwrap().contains(&json!("model_reasoning_effort=\"high\""))); }
    assert_eq!(calls[1][1], "resume");
    assert_eq!(calls[1][2], "thread_legacy");
    let thread: Value = serde_json::from_slice(&fs::read(native_transcript(dir.path(), "codex-session.codex.json")).unwrap()).unwrap();
    assert_eq!(thread["transport"], "exec");
    assert_eq!(thread["model"], "account-model");
    assert_eq!(thread["effort"], "high");
}

#[test]
fn codex_protected_startup_preserves_authoritative_build_refusal() {
    let cache = std::env::var_os("TMPDIR").map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::PathBuf::from(std::env::var_os("HOME").unwrap()).join(".cache/doxa-tests"));
    fs::create_dir_all(&cache).unwrap();
    let dir=tempfile::tempdir_in(cache).unwrap();
    let codex=dir.path().join("unsupported-codex");
    let python = Path::new("/usr/bin/python3");
    executable(&codex,r#"#!/usr/bin/python3
import json,sys
init=json.loads(sys.stdin.readline())
assert init['method']=='initialize'
print(json.dumps({'id':init['id'],'result':{'userAgent':'codex_cli_rs/0.0.1'}}),flush=True)
assert json.loads(sys.stdin.readline())['method']=='initialized'
# A protected startup must refuse before hooks/list or thread/start.
assert not sys.stdin.readline()
"#);
    let mut process=Process::start_codex_appserver(dir.path(),&codex,&python,false);
    let (mut reader,mut socket)=process.connect();receive(&mut reader);
    send(&mut socket,json!({"type":"attach","cursor":null}));
    send(&mut socket,json!({"type":"prompt","id":1,"text":"never delivered to a provider"}));
    loop {
        let frame=receive(&mut reader);
        if frame["event"]["type"]=="turn_done" {
            assert_eq!(frame["event"]["data"]["is_error"],true);
            assert_eq!(frame["event"]["data"]["error"],"Codex build has no verified DOXA compaction hook contract");
            break;
        }
    }
    send(&mut socket,json!({"type":"call","id":2,"method":"stop","params":{}}));
    loop { let frame=receive(&mut reader);if frame["type"]=="reply"&&frame["id"]==2 {assert_eq!(frame["ok"],true);break;} }
    wait_until(||process.exited());
    assert!(!native_transcript(dir.path(), "codex-session.codex.json").exists());
}

#[test]
fn memory_off_codex_scrubs_and_records_without_snapshot_index_or_compact_review() {
    for (configured, override_env, enabled) in [("0", None, false), ("1", Some("off"), false)] {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join("home")).unwrap();
        fs::write(dir.path().join("home/config.toml"), format!("lore = '{configured}'\n")).unwrap();
        let codex = dir.path().join("codex-fixture"); let python = Path::new("/usr/bin/python3");
        let captured = dir.path().join("stdin.txt");
        native_memory_fixture(dir.path(),"- durable memory\n");
        executable(&codex, &format!("#!/bin/sh\ncat > '{}'\necho '{{\"type\":\"thread.started\",\"thread_id\":\"thread_1\"}}'\n", captured.display()));
        let mut command = daemon_command();
        command.args(["--runtime-dir", dir.path().to_str().unwrap(), "--cwd", dir.path().to_str().unwrap(),
            "--session-id", "codex-session", "--linger", "10", "--engine", "codex", "--codex-bin", codex.to_str().unwrap(), "--lore-python", python.to_str().unwrap()])
            .env("DOXA_HOME", dir.path().join("home")).env("DOXA_CODEX_APPSERVER", "0").env_remove("DOXA_LORE")
            .stdout(Stdio::null()).stderr(Stdio::piped());
        if let Some(value) = override_env { command.env("DOXA_LORE", value); }
        let child = command.spawn().unwrap(); let registry = dir.path().join("registry/codex-session.json");
        wait_until(|| registry.exists());
        let entry: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
        let mut process = Process { child, registry, socket: PathBuf::from(entry["daemon_socket"].as_str().unwrap()) };
        let (mut reader, mut socket) = process.connect(); assert_eq!(receive(&mut reader)["lore_enabled"], enabled);
        send(&mut socket, json!({"type":"attach","cursor":null}));
        send(&mut socket, json!({"type":"prompt","id":1,"text":"sk-ownedCanonicalFixtureSecret1234567890 task"}));
        assert_eq!(receive(&mut reader)["ok"], true);
        loop { let frame = receive(&mut reader); assert!(!frame.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890")); if frame["event"]["type"] == "turn_done" { break; } }
        send(&mut socket, json!({"type":"call","id":2,"method":"stop","params":{}}));
        assert_eq!(receive(&mut reader)["ok"], true); wait_until(|| process.exited());
        let provider = fs::read_to_string(&captured).unwrap();
        assert!(provider.contains("MEMORY OFF")); assert!(!provider.contains("durable memory"));
        assert!(!dir.path().join("native-lore/state.db").exists());
        assert_eq!(fs::read_to_string(dir.path().join("native-lore/USER.md")).unwrap(),"- durable memory\n");
        let transcript = fs::read_to_string(native_transcript(dir.path(), "codex-session.jsonl")).unwrap();
        assert!(transcript.contains("[REDACTED:api-key] task")); assert!(!transcript.contains("sk-ownedCanonicalFixtureSecret1234567890"));
    }
}

#[test]
fn native_child_keeps_parent_identity_and_starts_task_without_an_attachment() {
    let dir = tempfile::tempdir().unwrap();
    let runtime = dir.path();
    let child = daemon_command()
        .args(["--runtime-dir", runtime.to_str().unwrap(), "--cwd", runtime.to_str().unwrap(),
            "--session-id", "native-child", "--linger", "10", "--spawn-depth", "2",
            "--parent-session-id", "native-parent", "--task", "perform the approved task"])
        .env("DOXA_HOME", runtime.join("home")).env("DOXA_WORKTREE", "0")
        .stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap();
    let registry = runtime.join("registry/native-child.json");
    wait_until(|| registry.exists());
    let row: Value = serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
    assert_eq!(row["parent_session_id"], "native-parent");
    assert_eq!(row["pid"], child.id());
    assert!(row.get("spawn_depth").is_none());
    let socket = PathBuf::from(row["daemon_socket"].as_str().unwrap());
    assert_eq!(socket.file_name().unwrap().to_str().unwrap(), format!("daemon-native-c-{}.sock", child.id()));
    let mut process = Process { child, registry, socket };
    let mut stream = UnixStream::connect(&process.socket).unwrap();
    stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut line = String::new(); reader.read_line(&mut line).unwrap();
    writeln!(stream, "{}", json!({"type":"attach","cursor":null})).unwrap();
    loop {
        line.clear(); reader.read_line(&mut line).unwrap();
        let frame: Value = serde_json::from_str(&line).unwrap();
        if frame["event"]["type"] == "turn_done" { break; }
    }
    writeln!(stream, "{}", json!({"type":"call","id":1,"method":"stop","params":{}})).unwrap();
    wait_until(|| process.child.try_wait().unwrap().is_some());
}

#[test]
fn native_broadcast_reply_and_history_filters_follow_the_owned_delivery_path() {
    let dir=tempfile::tempdir().unwrap();let codex=dir.path().join("codex-fixture");let python = Path::new("/usr/bin/python3");executable(&codex,"#!/bin/sh\nexit 0\n");
    let (first,_)=registry_peer(dir.path(),"first",dir.path().to_str().unwrap(),"first teammate");
    let (second,_)=registry_peer(dir.path(),"second",dir.path().to_str().unwrap(),"second teammate");
    let (foreign,_)=registry_peer(dir.path(),"foreign","/other-project","outsider");
    let receive_body=|listener:UnixListener|thread::spawn(move|| {
        listener.set_nonblocking(true).unwrap();let deadline=Instant::now()+Duration::from_secs(5);
        loop {
            match listener.accept() {
                Ok((mut stream,_))=>{stream.set_read_timeout(Some(Duration::from_millis(500))).unwrap();let mut body=String::new();stream.read_to_string(&mut body).unwrap();if !body.is_empty() { return serde_json::from_str::<Value>(&body).unwrap(); }},
                Err(error) if error.kind()==std::io::ErrorKind::WouldBlock=>{assert!(Instant::now()<deadline);thread::sleep(Duration::from_millis(5));},
                Err(error)=>panic!("{error}"),
            }
        }
    });
    let first=receive_body(first);let second=receive_body(second);
    let mut process=Process::start_codex(dir.path(),&codex,&python);let (mut reader,mut socket)=process.connect();
    receive(&mut reader);send(&mut socket,json!({"type":"attach","cursor":null}));
    let reply="0123456789abcdef0123456789abcdef";
    send(&mut socket,json!({"type":"call","id":1,"method":"msg","params":{"body":"sk-ownedCanonicalFixtureSecret1234567890 broadcast","broadcast":true,"in_reply_to":reply}}));
    let mut result=receive(&mut reader);while result["id"]!=1 { result=receive(&mut reader); }
    assert_eq!(result["ok"],true);assert_eq!(result["peer_count"],2);assert_eq!(result["kind"],"broadcast");
    let mut delivered:Vec<_>=result["delivered_to"].as_array().unwrap().iter().map(|id|id.as_str().unwrap()).collect();delivered.sort();assert_eq!(delivered,vec!["first","second"]);
    for worker in [first,second] { let frame=worker.join().unwrap();assert_eq!(frame["body"],"[REDACTED:api-key] broadcast");assert_eq!(frame["kind"],"broadcast"); }
    send(&mut socket,json!({"type":"call","id":2,"method":"peer_history","params":{"direction":"sent","limit":1}}));
    let mut history=receive(&mut reader);while history["id"]!=2 { history=receive(&mut reader); }
    assert_eq!(history["ok"],true);assert_eq!(history["messages"].as_array().unwrap().len(),1);assert_eq!(history["messages"][0]["in_reply_to"],reply);
    assert_eq!(history["messages"][0]["id"],result["message_id"]);assert!(!history.to_string().contains("sk-ownedCanonicalFixtureSecret1234567890"));
    send(&mut socket,json!({"type":"call","id":3,"method":"peer_history","params":{"direction":"received","limit":100}}));
    let mut empty=receive(&mut reader);while empty["id"]!=3 { empty=receive(&mut reader); }
    assert_eq!(empty["messages"].as_array().unwrap().len(),0);
    for (id,params) in [(4,json!({"to":"first","body":"bad","broadcast":true})),(5,json!({"body":"bad","broadcast":true,"in_reply_to":"../foreign"}))] {
        send(&mut socket,json!({"type":"call","id":id,"method":"msg","params":params}));
        let mut error=receive(&mut reader);while error["id"]!=id { error=receive(&mut reader); }assert_eq!(error["ok"],false);
    }
    assert_eq!(fs::read_to_string(dir.path().join("home/peers/messages.jsonl")).unwrap().lines().count(),1);
    foreign.set_nonblocking(true).unwrap();
    while let Ok((mut stream,_))=foreign.accept() {
        stream.set_read_timeout(Some(Duration::from_millis(500))).unwrap();let mut body=String::new();stream.read_to_string(&mut body).unwrap();
        assert!(body.is_empty(),"foreign scope received a message rather than an empty discovery probe");
    }
    send(&mut socket,json!({"type":"call","id":6,"method":"stop","params":{}}));
    let mut stop=receive(&mut reader);while stop["id"]!=6 { stop=receive(&mut reader); }assert_eq!(stop["ok"],true);wait_until(||process.exited());
}

#[test]
fn detached_claude_work_survives_linger_and_gets_a_full_idle_interval() {
    let dir = tempfile::tempdir().unwrap();
    let script = dir.path().join("linger-claude.py");
    fs::write(&script, r#"import json,sys,time
from pathlib import Path
root=Path(__file__).parent
print(json.dumps({'type':'hello','protocol':'doxa-claude-sidecar','version':1,'capabilities':[]}),flush=True)
for line in sys.stdin:
 frame=json.loads(line); method=frame['method']
 print(json.dumps({'type':'reply','id':frame['id'],'ok':True,'result':{'lore_enabled':False} if method=='start' else {}}),flush=True)
 if method=='prompt':
  root.joinpath('started').write_text('owned fixture work')
  while not root.joinpath('release').exists(): time.sleep(0.005)
  print(json.dumps({'type':'event','event':'turn_done','data':{}}),flush=True)
  root.joinpath('completed').write_text('finished')
 if method=='interrupt': root.joinpath('interrupted').write_text('unexpected cancellation')
 if method=='finalize': break
"#).unwrap();
    let child = daemon_command()
        .args(["--runtime-dir", dir.path().to_str().unwrap(), "--cwd", dir.path().to_str().unwrap(),
            "--session-id", "linger-claude", "--engine", "claude", "--claude-python", "/usr/bin/python3",
            "--claude-script", script.to_str().unwrap(), "--linger", "0.7"])
        .env("DOXA_HOME", dir.path().join("home")).env("LORE_ROOT",dir.path().join("lore"))
        .env("LORE_PROJECTS_DIR",dir.path().join("projects")).env("DOXA_LORE","0")
        .env_remove("DOXA_SESSION_BUDGET_USD").env("DOXA_AGENT_PEER_SEND","0")
        .stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap();
    let registry=dir.path().join("registry/linger-claude.json");
    let mut process=Process { child, registry, socket:PathBuf::new() };
    wait_until(|| {
        if let Some(status) = process.child.try_wait().unwrap() {
            let mut stderr = String::new(); process.child.stderr.take().unwrap().read_to_string(&mut stderr).unwrap();
            panic!("owned fixture daemon exited {status}: {stderr}");
        }
        process.registry.exists()
    });
    process.socket=process.entry()["daemon_socket"].as_str().unwrap().into();
    let (mut reader,mut socket)=process.connect(); receive(&mut reader);
    send(&mut socket,json!({"type":"attach","cursor":null}));
    wait_until(||process.entry()["clients"]==1);
    send(&mut socket,json!({"type":"prompt","id":1,"text":"owned fixture turn"}));
    assert_eq!(receive(&mut reader)["ok"],true); wait_until(||dir.path().join("started").exists());
    drop(reader); drop(socket); wait_until(||process.entry()["clients"]==0);
    thread::sleep(Duration::from_millis(1000));
    assert!(!process.exited(), "detached running work was expired");
    fs::write(dir.path().join("release"),"finish").unwrap(); wait_until(||dir.path().join("completed").exists());
    thread::sleep(Duration::from_millis(200));
    assert!(!process.exited(),"work completion needs a full idle linger interval");
    assert!(!dir.path().join("interrupted").exists(),"automatic linger interrupted a running provider");
    wait_until(||process.exited()); assert!(!process.registry.exists());
}

#[test]
fn claude_initialization_past_ten_seconds_survives_real_frontend_launch() {
    use doxa_tui::launch::{self,Engine,LaunchOptions};
    struct Environment(Vec<(&'static str,Option<std::ffi::OsString>)>);
    impl Drop for Environment { fn drop(&mut self) {
        for (key,value) in self.0.drain(..) { match value { Some(value)=>std::env::set_var(key,value), None=>std::env::remove_var(key) } }
    } }
    struct OwnedSession(doxa_tui::discovery::Session);
    impl Drop for OwnedSession { fn drop(&mut self) { let _=launch::stop(&self.0); } }
    let dir=tempfile::tempdir().unwrap(); let runtime=dir.path().join("runtime");
    for path in [dir.path().to_owned(),dir.path().join("home"),dir.path().join("home/peers")] {
        fs::create_dir_all(&path).unwrap(); fs::set_permissions(&path,fs::Permissions::from_mode(0o700)).unwrap();
    }
    let mut environment=Environment(Vec::new());
    for (key,value) in [("DOXA_DAEMON_BIN",PathBuf::from(env!("CARGO_BIN_EXE_doxa-daemon"))),
        ("HOME",dir.path().join("fixture-home")),("DOXA_HOME",dir.path().join("home")),("LORE_ROOT",dir.path().join("lore")),
        ("LORE_PROJECTS_DIR",dir.path().join("projects")),("CODEX_HOME",dir.path().join("codex")),("LORE_CODEX_SESSIONS_DIR",dir.path().join("codex-sessions")),
        ("CLAUDE_CONFIG_DIR",dir.path().join("claude"))] {
        environment.0.push((key,std::env::var_os(key))); std::env::set_var(key,value);
    }
    environment.0.push(("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT",std::env::var_os("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT")));
    std::env::remove_var("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT");
    let script=dir.path().join("slow-start.py");
    fs::write(&script,r#"import json,sys,time
print(json.dumps({'type':'hello','protocol':'doxa-claude-sidecar','version':1,'capabilities':[]}),flush=True)
for line in sys.stdin:
 frame=json.loads(line); method=frame['method']
 if method=='start': time.sleep(11)
 result={'lore_enabled':False,'effort':'low','data':{'model':'fixture-claude'}} if method=='start' else {}
 print(json.dumps({'type':'reply','id':frame['id'],'ok':True,'result':result}),flush=True)
 if method=='finalize': break
"#).unwrap();
    let options=LaunchOptions { engine:Engine::Claude,cwd:Some(dir.path().to_owned()),
        model:Some("fixture-claude".into()),effort:Some("low".into()),
        claude_python:Some("/usr/bin/python3".into()),claude_script:Some(script),..LaunchOptions::default() };
    let started=Instant::now();
    let owned=OwnedSession(launch::spawn_fleet(&options,&runtime,None,false,false).unwrap());
    assert!(started.elapsed()>=Duration::from_secs(11));
    let mut client=doxa_tui::transport::DaemonClient::connect(&owned.0.socket,None).unwrap();
    assert_eq!(client.hello["session_id"],owned.0.id); assert_eq!(client.hello["engine"],"claude");
    assert_eq!(client.hello["model"],"fixture-claude");
    let status=client.call("status",serde_json::Map::new()).unwrap(); assert_eq!(status["ok"],true);
    let record=runtime.join("registry").join(format!("{}.json",owned.0.id));
    drop(client); drop(owned);
    // Claim lock files deliberately persist to retain one stable lock inode.
    wait_until(||!record.exists());
}
