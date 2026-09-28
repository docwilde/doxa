//! One operator owner per native parent. Model arguments never set process
//! identity, engine, repository, depth, binary, or the parent control channel.
use doxa_peers::{scope_for_cwd, Registry};
use doxa_runtime::Host;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs::{File, OpenOptions},
    io::{self, BufRead, BufReader, Read},
    os::{
        fd::AsRawFd,
        unix::{
            fs::{MetadataExt, OpenOptionsExt},
            net::UnixStream,
            process::CommandExt,
        },
    },
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Sender, SyncSender},
        Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use time::{format_description::well_known::Iso8601, OffsetDateTime};
#[derive(Clone)]
pub struct SpawnConfig {
    pub executable: PathBuf,
    pub engine: String,
    pub runtime: PathBuf,
    pub cwd: PathBuf,
    pub session_id: String,
    pub depth: u32,
    pub provider_args: Vec<String>,
}
struct Pending {
    id: String,
    answer: Sender<bool>,
}
pub struct SpawnManager {
    config: SpawnConfig,
    scope: String,
    events: SyncSender<Value>,
    pending: Mutex<Option<Pending>>,
    active: AtomicBool,
    closed: AtomicBool,
    cancel: std::sync::atomic::AtomicU64,
}
pub fn enabled() -> bool {
    let home = std::env::var_os("DOXA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".doxa")
        });
    let config = doxa_state::load_config(&home.join("config.toml"));
    matches!(
        doxa_state::raw_setting(
            std::env::var("DOXA_SPAWN_SESSIONS").ok().as_deref(),
            &config,
            "spawn_sessions"
        )
        .trim()
        .to_ascii_lowercase()
        .as_str(),
        "1" | "true" | "yes" | "on"
    )
}
fn uuid() -> io::Result<String> {
    let mut bytes = [0u8; 16];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    let h = bytes.iter().map(|b| format!("{b:02x}")).collect::<String>();
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &h[..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..]
    ))
}
struct OwnedChild(Option<Child>);
impl Drop for OwnedChild {
    fn drop(&mut self) {
        if let Some(child) = self.0.as_mut() {
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}
impl SpawnManager {
    pub fn new(config: SpawnConfig, events: SyncSender<Value>) -> io::Result<Self> {
        if !config.executable.is_absolute()
            || !config.runtime.is_absolute()
            || !config.cwd.is_absolute()
            || !doxa_state::valid_session_id(&config.session_id)
            || config.depth > 2
            || !matches!(
                config.engine.as_str(),
                "fixture" | "claude" | "codex" | "deepseek" | "glm"
            )
        {
            return Err(io::Error::other("Invalid immutable spawn identity"));
        }
        // The calling main supplies validated provider binaries and sandbox only.
        if config.provider_args.iter().any(|arg| {
            matches!(
                arg.as_str(),
                "--task"
                    | "--model"
                    | "--effort"
                    | "--cwd"
                    | "--session-id"
                    | "--parent-session-id"
                    | "--spawn-depth"
                    | "--engine"
                    | "--runtime-dir"
                    | "--resume"
                    | "--base-branch"
            )
        }) {
            return Err(io::Error::other(
                "Spawn arguments cannot override host identity",
            ));
        }
        let scope = scope_for_cwd(&config.cwd)?;
        Ok(Self {
            config,
            scope,
            events,
            pending: Mutex::new(None),
            active: AtomicBool::new(false),
            closed: AtomicBool::new(false),
            cancel: std::sync::atomic::AtomicU64::new(0),
        })
    }
    pub fn is_active(&self) -> bool {
        self.active.load(Ordering::Acquire)
    }
    pub fn cancel(&self, close: bool) {
        if close {
            self.closed.store(true, Ordering::Release);
        }
        self.cancel.fetch_add(1, Ordering::AcqRel);
        if let Ok(mut pending) = self.pending.lock() {
            pending.take();
        }
    }
    pub fn answer(&self, id: &str, answer: &Value) -> Option<Result<Value, String>> {
        if !id.starts_with(&format!("spawn-{}-", self.config.session_id)) {
            return None;
        }
        let mut pending = match self.pending.lock() {
            Ok(p) => p,
            Err(_) => return Some(Err("Spawn approval unavailable".into())),
        };
        if pending.as_ref().is_none_or(|p| p.id != id) {
            return Some(Ok(json!({"applied":false})));
        }
        if !matches!(answer["decision"].as_str(), Some("allow" | "deny")) {
            return Some(Err("Invalid spawn approval answer".into()));
        }
        let pending = pending.take().unwrap();
        let applied = pending.answer.send(answer["decision"] == "allow").is_ok();
        let _ = self
            .events
            .try_send(json!({"type":"needs_input_resolved","data":{"id":id}}));
        Some(Ok(json!({"applied":applied})))
    }
    fn reservation(&self) -> Result<File, String> {
        let registry =
            Registry::open(&self.config.runtime).map_err(|_| "Spawn registry unavailable")?;
        let digest = format!("{:x}", Sha256::digest(self.scope.as_bytes()));
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
            .open(registry.directory().join(format!("spawn-{digest}.lock")))
            .map_err(|_| "Spawn reservation unavailable")?;
        let meta = file
            .metadata()
            .map_err(|_| "Spawn reservation unavailable")?;
        if !meta.is_file()
            || meta.uid() != unsafe { libc::geteuid() }
            || meta.nlink() != 1
            || meta.mode() & 0o077 != 0
        {
            return Err("Unowned spawn reservation".into());
        }
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err("Another spawn in this repository is awaiting approval".into());
        }
        Ok(file)
    }
    fn caps(&self) -> Result<usize, String> {
        let peers = Registry::open(&self.config.runtime)
            .and_then(|r| {
                r.scoped(
                    &self.scope,
                    Some(&self.config.session_id),
                    &|s: &str| s.to_owned(),
                    true,
                )
            })
            .map_err(|_| "Spawn registry unavailable")?;
        let live = peers.len() + 1;
        if live >= 3 {
            return Err("Session limit reached (3 live sessions in this repository)".into());
        }
        let now = OffsetDateTime::now_utc();
        if peers.iter().any(|peer| {
            OffsetDateTime::parse(&peer.started_at, &Iso8601::DEFAULT)
                .map_or(true, |start| (now - start).whole_seconds() <= 60)
        }) {
            return Err("Spawn rate limit reached (1 per 60 seconds in this repository)".into());
        }
        Ok(live)
    }
    pub fn spawn(&self, args: &Value, host: &dyn Host) -> Result<Value, String> {
        doxa_engines::session_tools::validate(args)?;
        if self
            .active
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return Err("Another parent spawn is active".into());
        }
        struct Active<'a>(&'a AtomicBool);
        impl Drop for Active<'_> {
            fn drop(&mut self) {
                self.0.store(false, Ordering::Release);
            }
        }
        let _active = Active(&self.active);
        if !enabled() || self.closed.load(Ordering::Acquire) {
            return Err("Session spawning is off or parent is closed".into());
        }
        if self.config.depth >= 2 {
            return Err("Spawn depth limit reached (2)".into());
        }
        let task = host
            .public_prompt(args["task"].as_str().unwrap())?
            .trim()
            .to_owned();
        if task.is_empty() || task.chars().count() > 2000 {
            return Err("Child task must contain 1 to 2000 reviewed characters".into());
        }
        let _reservation = self.reservation()?;
        let live = self.caps()?;
        let model = args["model"]
            .as_str()
            .map(str::to_owned)
            .or_else(|| host.initial_model());
        let effort = host.initial_effort();
        let base = args["base_branch"].as_str();
        let worktrees = std::env::var_os("DOXA_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".doxa")
            })
            .join("worktrees");
        let mut probe = worktrees.as_path();
        while !probe.exists() {
            probe = probe.parent().ok_or("Spawn disk probe unavailable")?;
        }
        let cpath = std::ffi::CString::new(probe.as_os_str().as_encoded_bytes())
            .map_err(|_| "Invalid disk probe")?;
        let mut stat: libc::statvfs = unsafe { std::mem::zeroed() };
        let free = if unsafe { libc::statvfs(cpath.as_ptr(), &mut stat) } == 0 {
            Some(stat.f_bavail.saturating_mul(stat.f_frsize))
        } else {
            None
        };
        if free.is_some_and(|free| free < 425 * 1024 * 1024) {
            return Err("Spawn needs 425 MiB free for its worktree and working state".into());
        }
        let generation = self.cancel.load(Ordering::Acquire);
        let id = format!(
            "spawn-{}-{}",
            self.config.session_id,
            uuid().map_err(|_| "Spawn identity unavailable")?
        );
        let (tx, rx) = mpsc::channel();
        {
            let mut pending = self.pending.try_lock().map_err(|_| "Spawn approval busy")?;
            if pending.is_some() {
                return Err("A spawn approval is already pending".into());
            }
            *pending = Some(Pending {
                id: id.clone(),
                answer: tx,
            });
        }
        let body=format!("Engine: {}\nModel: {}\nEffort: {}\nRepository: {}\nBase: {}\nLive sessions: {live}/3\nChild depth: {}/2\nThis starts a separate billed provider session.",self.config.engine,model.as_deref().unwrap_or("provider default"),effort.as_deref().unwrap_or("provider default"),self.scope,base.unwrap_or("current checkout"),self.config.depth+1);
        if self.events.try_send(json!({"type":"needs_input","data":{"id":id,"kind":"spawn","title":"Start this delegated session?","task":task,"body":body,"require_full_review":true}})).is_err(){self.cancel(false);return Err("Spawn approval channel unavailable".into());}
        let allowed = loop {
            if self.closed.load(Ordering::Acquire)
                || self.cancel.load(Ordering::Acquire) != generation
            {
                return Err("Spawn cancelled".into());
            }
            match rx.recv_timeout(Duration::from_millis(50)) {
                Ok(answer) => break answer,
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(_) => return Err("Spawn approval cancelled".into()),
            }
        };
        if !allowed {
            return Err("The user declined this spawn".into());
        }
        if !enabled()
            || self.closed.load(Ordering::Acquire)
            || self.cancel.load(Ordering::Acquire) != generation
        {
            return Err("Spawn cancelled or disabled".into());
        }
        self.caps()?;
        if args["model"].is_null() && host.initial_model() != model
            || host.initial_effort() != effort
        {
            return Err("Parent selection changed after review; request a new spawn".into());
        }
        let child_id = uuid().map_err(|_| "Child identity unavailable")?;
        let mut command = Command::new(&self.config.executable);
        command.args([
            "--runtime-dir",
            self.config.runtime.to_str().ok_or("Invalid runtime")?,
            "--cwd",
            &self.scope,
            "--session-id",
            &child_id,
            "--engine",
            &self.config.engine,
            "--spawn-depth",
            &(self.config.depth + 1).to_string(),
            "--parent-session-id",
            &self.config.session_id,
            "--task",
            &task,
            "--linger",
            "900",
        ]);
        command.args(&self.config.provider_args);
        if let Some(model) = &model {
            command.args(["--model", model]);
        }
        if let Some(effort) = &effort {
            command.args(["--effort", effort]);
        }
        if let Some(base) = base {
            command.args(["--base-branch", base]);
        }
        let mut child = OwnedChild(Some(
            command
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .process_group(0)
                .spawn()
                .map_err(|_| "Child daemon could not launch")?,
        ));
        let pid = child.0.as_ref().unwrap().id() as i32;
        let deadline = Instant::now() + Duration::from_secs(120);
        let socket = loop {
            if self.closed.load(Ordering::Acquire)
                || self.cancel.load(Ordering::Acquire) != generation
            {
                return Err("Child startup cancelled".into());
            }
            if child
                .0
                .as_mut()
                .unwrap()
                .try_wait()
                .map_err(|_| "Child status unavailable")?
                .is_some()
            {
                return Err("Child daemon exited before verified publication".into());
            }
            if Instant::now() >= deadline {
                return Err("Child daemon startup timed out".into());
            }
            let peers = Registry::open(&self.config.runtime)
                .and_then(|r| r.scoped(&self.scope, None, &|s: &str| s.to_owned(), false))
                .map_err(|_| "Child registry unavailable")?;
            if let Some(peer) = peers.iter().find(|p| {
                p.session_id == child_id
                    && p.pid == pid
                    && p.engine.as_deref() == Some(self.config.engine.as_str())
                    && p.parent_session_id.as_deref() == Some(self.config.session_id.as_str())
            }) {
                if let Some(socket) = &peer.daemon_socket {
                    if let Ok(stream) = UnixStream::connect(socket) {
                        stream
                            .set_read_timeout(Some(Duration::from_secs(1)))
                            .map_err(|_| "Child handshake unavailable")?;
                        let mut bytes = Vec::new();
                        let n = std::io::Read::take(BufReader::new(stream), 65537)
                            .read_until(b'\n', &mut bytes)
                            .map_err(|_| "Child handshake unavailable")?;
                        let hello: Value = serde_json::from_slice(&bytes)
                            .map_err(|_| "Invalid child handshake")?;
                        if n <= 65536
                            && hello["type"] == "hello"
                            && hello["session_id"] == child_id
                            && hello["engine"] == self.config.engine
                        {
                            break socket.clone();
                        }
                        return Err("Child session identity changed".into());
                    }
                }
            }
            thread::sleep(Duration::from_millis(50));
        };
        let mut published = child.0.take().unwrap();
        thread::spawn(move || {
            let _ = published.wait();
        });
        Ok(
            json!({"session_id":child_id,"daemon_socket":socket,"cwd":self.scope,"spawn_depth":self.config.depth+1,"live_sessions":live+1,"note":"The session exists and has received the approved task. Its work is not complete. Its commits are on its own DOXA branch; results are not delivered automatically."}),
        )
    }
}
impl Drop for SpawnManager {
    fn drop(&mut self) {
        self.cancel(true);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn scope_reservation_is_nonblocking_and_immutable_flags_are_refused() {
        let dir = tempfile::tempdir().unwrap();
        let (events, _) = mpsc::sync_channel(8);
        let config = SpawnConfig {
            executable: std::env::current_exe().unwrap(),
            engine: "fixture".into(),
            runtime: dir.path().join("runtime"),
            cwd: dir.path().to_owned(),
            session_id: "parent".into(),
            depth: 0,
            provider_args: vec![],
        };
        let manager = SpawnManager::new(config.clone(), events.clone()).unwrap();
        let first = manager.reservation().unwrap();
        let second = SpawnManager::new(config.clone(), events.clone()).unwrap();
        let started = Instant::now();
        assert!(second.reservation().is_err());
        assert!(started.elapsed() < Duration::from_millis(250));
        drop(first);
        assert!(second.reservation().is_ok());
        for flag in [
            "--task",
            "--engine",
            "--model",
            "--effort",
            "--spawn-depth",
            "--parent-session-id",
            "--cwd",
            "--session-id",
            "--runtime-dir",
            "--resume",
        ] {
            let mut config = config.clone();
            config.provider_args = vec![flag.into(), "forged".into()];
            assert!(SpawnManager::new(config, events.clone()).is_err());
        }
        assert!(doxa_engines::session_tools::validate(
            &json!({"task":"task","op_ctx":{"cwd":"foreign"}})
        )
        .is_err());
        assert!(
            doxa_engines::session_tools::validate(&json!({"task":"task","model":"--engine"}))
                .is_err()
        );
    }
}
