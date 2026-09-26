//! Native DOXA protocol host. The fixture remains an explicit test mode.
mod claude_host;
mod budget_host;
mod codex_host;
mod peer_host;
mod vendor_host;
mod vendor_tools;
use claude_host::ClaudeHost;
use budget_host::BudgetHost;
use codex_host::CodexHost;
use doxa_engines::codex_driver::{DriverOptions, SandboxMode};
use doxa_peers::delivery::Inbox;
use doxa_runtime::{Daemon, ExternalPrompt, Host, Session};
use doxa_vendors::Vendor;
use peer_host::PeerHost;
use serde_json::{json, Value};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use vendor_host::VendorHost;

static TERMINATE: AtomicBool = AtomicBool::new(false);
extern "C" fn signal_handler(_: libc::c_int) {
    TERMINATE.store(true, Ordering::Release);
}

struct FixtureHost;
impl Host for FixtureHost {
    fn prompt(&self, _: &str, emit: &mut dyn FnMut(Value)) {
        emit(json!({"type":"turn_started","data":{}}));
        emit(json!({"type":"text_delta","data":{"text":"Deterministic native fixture response."}}));
        emit(json!({"type":"turn_done","data":{}}));
    }
    fn call(&self, method: &str, _: &Value) -> Result<Value, String> {
        match method {
            "stop" => Ok(json!({})),
            _ => Err(format!(
                "{method} is unavailable in the native fixture host"
            )),
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Engine {
    Fixture,
    Codex,
    Claude,
    DeepSeek,
    Glm,
}
impl Engine {
    fn name(self) -> &'static str {
        match self {
            Self::Fixture => "fixture",
            Self::Codex => "codex",
            Self::Claude => "claude",
            Self::DeepSeek => "deepseek",
            Self::Glm => "glm",
        }
    }
    fn vendor(self) -> Option<Vendor> {
        match self {
            Self::DeepSeek => Some(Vendor::DeepSeek),
            Self::Glm => Some(Vendor::Glm),
            _ => None,
        }
    }
}
struct Options {
    runtime: PathBuf,
    cwd: PathBuf,
    session_id: String,
    base_branch: Option<String>,
    linger: Duration,
    engine: Engine,
    codex_bin: Option<PathBuf>,
    lore_python: Option<PathBuf>,
    claude_python: Option<PathBuf>,
    claude_script: Option<PathBuf>,
    resume: bool,
    model: Option<String>,
    effort: Option<String>,
    #[cfg(feature = "local-test-server")]
    vendor_endpoint: Option<String>,
    sandbox: SandboxMode,
}
fn linger_duration(value: &str) -> io::Result<Duration> {
    let seconds: f64 = value.parse().map_err(|_| invalid("invalid linger"))?;
    if !seconds.is_finite() || !(0.0..=31_536_000.0).contains(&seconds) {
        return Err(invalid("linger must be between 0 and 31536000 seconds"));
    }
    Ok(Duration::from_secs_f64(seconds))
}
fn options() -> io::Result<Options> {
    let mut runtime = env::var_os("DOXA_RUNTIME_DIR")
        .map(PathBuf::from)
        .or_else(|| env::var_os("XDG_RUNTIME_DIR").map(|p| PathBuf::from(p).join("doxa")))
        .unwrap_or_else(|| {
            PathBuf::from(env::var_os("HOME").unwrap_or_default()).join(".local/share/doxa")
        });
    let mut cwd = env::current_dir()?;
    let mut session_id = random_id()?;
    let mut explicit_session_id = false;
    let mut base_branch = None;
    let mut linger = Duration::from_secs(120);
    let mut engine = Engine::Fixture;
    let mut codex_bin = None;
    let mut lore_python = None;
    let mut claude_python = None;
    let mut claude_script = None;
    let mut resume = false;
    let mut model = None;
    let mut effort = None;
    #[cfg(feature = "local-test-server")]
    let mut vendor_endpoint = None;
    let mut sandbox = SandboxMode::WorkspaceWrite;
    let mut args = env::args_os().skip(1);
    while let Some(arg) = args.next() {
        let value = args
            .next()
            .ok_or_else(|| invalid("missing argument value"))?;
        match arg.to_str() {
            Some("--runtime-dir") => runtime = PathBuf::from(value),
            Some("--cwd") => cwd = PathBuf::from(value),
            Some("--base-branch") => base_branch = Some(value.into_string().map_err(|_| invalid("invalid base branch"))?),
            Some("--session-id") => {
                session_id = value.into_string().map_err(|_| invalid("invalid session id"))?;
                explicit_session_id = true;
            },
            Some("--engine") => engine = match value.to_str() {
                Some("fixture") => Engine::Fixture, Some("codex") => Engine::Codex,
                Some("claude") => Engine::Claude,
                Some("deepseek") => Engine::DeepSeek, Some("glm") => Engine::Glm,
                _ => return Err(invalid("engine must be fixture, codex, claude, deepseek, or glm")),
            },
            Some("--codex-bin") => codex_bin = Some(PathBuf::from(value)),
            Some("--lore-python") => lore_python = Some(PathBuf::from(value)),
            Some("--claude-python") => claude_python = Some(PathBuf::from(value)),
            Some("--claude-script") => claude_script = Some(PathBuf::from(value)),
            Some("--resume") => resume = match value.to_str() {
                Some("true") => true, Some("false") => false,
                _ => return Err(invalid("resume must be true or false")),
            },
            Some("--model") => {
                let chosen = value.into_string().map_err(|_| invalid("invalid model"))?;
                if chosen.is_empty() || chosen.len() > 128 || chosen.chars().any(char::is_control) {
                    return Err(invalid("invalid model"));
                }
                model = Some(chosen);
            }
            Some("--effort") => {
                let chosen = value.into_string().map_err(|_| invalid("invalid effort"))?;
                if chosen.is_empty() || chosen.len() > 32 || !chosen.bytes().all(|b| b.is_ascii_alphanumeric()) {
                    return Err(invalid("invalid effort"));
                }
                effort = Some(chosen);
            }
            #[cfg(feature = "local-test-server")]
            Some("--vendor-endpoint") => {
                vendor_endpoint = Some(value.into_string().map_err(|_| invalid("invalid test endpoint"))?);
            }
            Some("--sandbox") => sandbox = match value.to_str() {
                Some("read-only") => SandboxMode::ReadOnly,
                Some("workspace-write") => SandboxMode::WorkspaceWrite,
                Some("danger-full-access") => SandboxMode::DangerFullAccess,
                _ => return Err(invalid("invalid sandbox")),
            },
            Some("--linger") => {
                linger = linger_duration(value.to_str().ok_or_else(|| invalid("invalid linger"))?)?;
            }
            _ => return Err(invalid("usage: doxa-daemon [--runtime-dir PATH] [--cwd PATH] [--session-id ID] [--base-branch REF] [--linger SECONDS] [--engine fixture|codex|claude|deepseek|glm] [--codex-bin PATH --lore-python PATH --claude-python PATH --claude-script PATH --model MODEL --effort EFFORT --sandbox MODE --resume true|false]")),
        }
    }
    // Do not let registry entries claim an unvalidated path or identity.
    cwd = match fs::canonicalize(&cwd) {
        Ok(path) if path.is_dir() => path,
        Ok(_) => return Err(invalid("cwd must be a directory")),
        Err(error) if resume && error.kind() == io::ErrorKind::NotFound
            && cwd.is_absolute()
            && fs::symlink_metadata(&cwd).is_err_and(|e| e.kind() == io::ErrorKind::NotFound) => cwd,
        Err(error) => return Err(error),
    };
    let id = session_id.as_bytes();
    if id.is_empty()
        || id.len() > 128
        || !id[0].is_ascii_alphanumeric()
        || !id[1..]
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || *b == b'-')
    {
        return Err(invalid("invalid session id"));
    }
    if !runtime.is_absolute() {
        return Err(invalid("runtime directory must be absolute"));
    }
    if let Some(requested) = &mut base_branch {
        if resume { return Err(invalid("--base-branch cannot change the base of a resumed session")); }
        if !doxa_worktrees::enabled() {
            return Err(invalid("--base-branch needs worktree_per_session enabled"));
        }
        if engine == Engine::Fixture && env::var("DOXA_WORKTREE").as_deref() != Ok("1") {
            return Err(invalid("fixture --base-branch needs DOXA_WORKTREE=1"));
        }
        *requested = doxa_worktrees::resolve_base(&cwd, requested)
            .ok_or_else(|| invalid("--base-branch must name an existing local or remote-tracking branch"))?;
    }
    if engine == Engine::Codex {
        codex_bin = Some(executable(
            codex_bin.ok_or_else(|| invalid("Codex needs --codex-bin"))?,
        )?);
        lore_python = Some(python_executable(
            lore_python.ok_or_else(|| invalid("Codex needs --lore-python"))?,
        )?);
        if resume && !explicit_session_id {
            return Err(invalid("Codex resume needs --session-id"));
        }
        if claude_python.is_some() || claude_script.is_some() {
            return Err(invalid("Claude options require --engine claude"));
        }
    } else if engine == Engine::Claude {
        if effort.is_some() {
            return Err(invalid("effort requires a vendor engine"));
        }
        if resume && !explicit_session_id {
            return Err(invalid("Claude resume needs --session-id"));
        }
        claude_python = Some(python_executable(
            claude_python.ok_or_else(|| invalid("Claude needs --claude-python"))?,
        )?);
        let script = claude_script.ok_or_else(|| invalid("Claude needs --claude-script"))?;
        if !script.is_absolute() {
            return Err(invalid("Claude script path must be absolute"));
        }
        let script = fs::canonicalize(script)?;
        if !fs::metadata(&script)?.is_file() {
            return Err(invalid("Claude script must be a file"));
        }
        claude_script = Some(script);
        if codex_bin.is_some() || lore_python.is_some() || sandbox != SandboxMode::WorkspaceWrite {
            return Err(invalid("Codex options require --engine codex"));
        }
    } else if let Some(vendor) = engine.vendor() {
        if codex_bin.is_some()
            || claude_python.is_some()
            || claude_script.is_some()
            || sandbox != SandboxMode::WorkspaceWrite
        {
            return Err(invalid("unsupported option for vendor engine"));
        }
        lore_python = Some(python_executable(
            lore_python.ok_or_else(|| invalid("vendor needs --lore-python"))?,
        )?);
        if resume && !explicit_session_id {
            return Err(invalid("vendor resume needs --session-id"));
        }
        let chosen_model = model.get_or_insert_with(|| vendor.default_model().to_owned());
        let chosen_effort = effort.get_or_insert_with(|| "high".to_owned());
        doxa_vendors::request_body(vendor, chosen_model, &[], chosen_effort)
            .map_err(|_| invalid("invalid vendor effort"))?;
    } else if codex_bin.is_some()
        || lore_python.is_some()
        || claude_python.is_some()
        || claude_script.is_some()
        || resume
        || model.is_some()
        || effort.is_some()
        || sandbox != SandboxMode::WorkspaceWrite
    {
        return Err(invalid("Codex options require --engine codex"));
    }
    Ok(Options {
        runtime,
        cwd,
        session_id,
        base_branch,
        linger,
        engine,
        codex_bin,
        lore_python,
        claude_python,
        claude_script,
        resume,
        model,
        effort,
        #[cfg(feature = "local-test-server")]
        vendor_endpoint,
        sandbox,
    })
}
fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn executable(path: PathBuf) -> io::Result<PathBuf> {
    if !path.is_absolute() {
        return Err(invalid("executable path must be absolute"));
    }
    let path = fs::canonicalize(path)?;
    let meta = fs::metadata(&path)?;
    if !meta.is_file() || meta.permissions().mode() & 0o111 == 0 {
        return Err(invalid("executable path must name an executable file"));
    }
    Ok(path)
}
/// Keep the final Python symlink so pyvenv.cfg can determine sys.prefix.
/// Canonicalize only its parent to retain the absolute-path boundary.
fn python_executable(path: PathBuf) -> io::Result<PathBuf> {
    if !path.is_absolute() {
        return Err(invalid("executable path must be absolute"));
    }
    let meta = fs::metadata(&path)?;
    if !meta.is_file() || meta.permissions().mode() & 0o111 == 0 {
        return Err(invalid("executable path must name an executable file"));
    }
    let parent = fs::canonicalize(path.parent().ok_or_else(|| invalid("invalid executable path"))?)?;
    let name = path.file_name().ok_or_else(|| invalid("invalid executable path"))?;
    Ok(parent.join(name))
}
fn random_id() -> io::Result<String> {
    let mut bytes = [0u8; 16];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
fn iso_now() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    let sec = now.as_secs() as libc::time_t;
    unsafe {
        libc::gmtime_r(&sec, &mut tm);
    }
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:06}Z",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec,
        now.subsec_micros()
    )
}
fn owned_directory(path: &Path) -> io::Result<()> {
    if !path.exists() {
        fs::create_dir_all(path)?;
    }
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_dir() || meta.file_type().is_symlink() || meta.uid() != unsafe { libc::geteuid() } {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "registry directory must be owned and real",
        ));
    }
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
}
/// A stable, private lock inode prevents two daemon processes from opening
/// the same conversation state. Keep the file: unlinking a held lock would let
/// another process create and lock a different inode before shutdown finishes.
struct SessionClaim { _file: File }
impl SessionClaim {
    fn acquire(runtime: &Path, session_id: &str) -> io::Result<Self> {
        owned_directory(runtime)?;
        let dir = runtime.join("registry");
        owned_directory(&dir)?;
        let path = dir.join(format!("{session_id}.lock"));
        let file = OpenOptions::new().read(true).write(true).create(true).mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK).open(path)?;
        let meta = file.metadata()?;
        if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() }
            || meta.nlink() != 1 || meta.permissions().mode() & 0o077 != 0 {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "unsafe session claim file"));
        }
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::WouldBlock {
                return Err(io::Error::new(io::ErrorKind::AlreadyExists, "session is already active"));
            }
            return Err(error);
        }
        // An older daemon may already own this ID without participating in
        // the new claim protocol. Refuse before opening its saved host state.
        // Any entry, including a dangling symlink, also preserves the old
        // fail-closed behavior for stale or unsafe registry paths.
        match fs::symlink_metadata(dir.join(format!("{session_id}.json"))) {
            Ok(_) => return Err(io::Error::new(io::ErrorKind::AlreadyExists,
                "session registry entry already exists")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {},
            Err(error) => return Err(error),
        }
        Ok(Self { _file: file })
    }
}
struct Registry {
    path: PathBuf,
    identity: Option<(u64, u64)>,
    // Keep the owned inode allocated even if another process unlinks the path.
    // Otherwise an immediate replacement can reuse its inode number and pass
    // the ownership check below (an ABA race).
    owned_file: Option<File>,
    started_at: String,
    repo_root: Option<String>,
    cwd: String,
    session_id: String,
    socket: String,
    daemon_socket: String,
    engine: Engine,
}
impl Registry {
    fn new(options: &Options, socket: &Path, daemon_socket: &Path) -> io::Result<Self> {
        let dir = options.runtime.join("registry");
        owned_directory(&dir)?;
        let path = dir.join(format!("{}.json", options.session_id));
        if fs::symlink_metadata(&path).is_ok() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "session registry entry already exists",
            ));
        }
        // A linked worktree's --show-toplevel is its own checkout path. Use
        // the shared Git directory so native sessions match Python and the
        // Rust TUI's project scope in every worktree of the same repository.
        let repo_root = Command::new("git")
            .args(["rev-parse", "--path-format=absolute", "--git-common-dir"])
            .current_dir(&options.cwd)
            .output()
            .ok()
            .filter(|o| o.status.success())
            .and_then(|o| String::from_utf8(o.stdout).ok())
            .map(|s| PathBuf::from(s.trim()))
            .filter(|path| path.is_absolute())
            .map(|common| {
                if common.file_name().is_some_and(|name| name == ".git") {
                    common
                        .parent()
                        .unwrap_or(&common)
                        .to_string_lossy()
                        .into_owned()
                } else {
                    common.to_string_lossy().into_owned()
                }
            });
        Ok(Self {
            path,
            identity: None,
            owned_file: None,
            started_at: iso_now(),
            repo_root,
            cwd: options.cwd.to_string_lossy().into_owned(),
            session_id: options.session_id.clone(),
            socket: socket.to_string_lossy().into_owned(),
            daemon_socket: daemon_socket.to_string_lossy().into_owned(),
            engine: options.engine,
        })
    }
    fn write(&mut self, clients: usize) -> io::Result<()> {
        let entry = json!({"session_id":self.session_id,"pid":std::process::id(),
            "socket_path":self.socket,"daemon_socket":self.daemon_socket,"cwd":self.cwd,
            "repo_root":self.repo_root,"title":format!("DOXA Rust {} session", self.engine.name()),
            "started_at":self.started_at,"heartbeat_at":iso_now(),"clients":clients,
            "engine":self.engine.name()});
        let tmp = self
            .path
            .with_extension(format!("json.{}.tmp", std::process::id()));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&tmp)?;
        let result = (|| -> io::Result<()> {
            serde_json::to_writer(&mut file, &entry).map_err(io::Error::other)?;
            file.flush()?;
            file.sync_all()?;
            // Refuse to overwrite another process's registry entry.
            if let Ok(meta) = fs::symlink_metadata(&self.path) {
                if Some((meta.dev(), meta.ino())) != self.identity {
                    return Err(io::Error::new(
                        io::ErrorKind::AlreadyExists,
                        "registry entry replaced",
                    ));
                }
            } else if self.identity.is_some() {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    "registry entry removed",
                ));
            }
            let meta = file.metadata()?;
            fs::rename(&tmp, &self.path)?;
            self.identity = Some((meta.dev(), meta.ino()));
            self.owned_file = Some(file);
            Ok(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(&tmp);
        }
        result
    }
}
impl Drop for Registry {
    fn drop(&mut self) {
        if let (Some(identity), Ok(meta)) = (self.identity, fs::symlink_metadata(&self.path)) {
            if (meta.dev(), meta.ino()) == identity {
                let _ = fs::remove_file(&self.path);
            }
        }
    }
}
fn run() -> io::Result<()> {
    let mut options = options()?;
    // Acquire before constructing a host: vendor and Claude resume open the
    // saved conversation state during host startup, before registry publish.
    let _claim = SessionClaim::acquire(&options.runtime, &options.session_id)?;
    // Match Python 1.19's explicit-truthy switch. Claude keeps its own Python
    // sidecar peer loop; starting a second native loop there would duplicate
    // delivery, so only native hosts with a LORE scrubber accept this switch.
    let inbound = env::var("DOXA_PEER_INBOUND_TURNS").unwrap_or_default();
    let inbound_turns = !inbound.trim().is_empty()
        && !matches!(inbound.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off");
    if inbound_turns && matches!(options.engine, Engine::Fixture | Engine::Claude) {
        return Err(invalid("native inbound peer turns require Codex or vendor engine with LORE scrub; Claude uses the Python sidecar peer loop"));
    }
    let ceiling = match env::var("DOXA_SESSION_BUDGET_USD") {
        Ok(raw) if !raw.trim().is_empty() => {
            let value: f64 = raw.trim().parse().map_err(|_| invalid("invalid session budget"))?;
            if !value.is_finite() || value <= 0.0 { return Err(invalid("invalid session budget")); }
            if options.engine == Engine::Codex {
                return Err(invalid("native Codex budget requires complete priced usage accounting; use the Python fleet harness"));
            }
            if let Some(vendor) = options.engine.vendor() {
                let model = options.model.as_deref().ok_or_else(|| invalid("budgeted vendor session requires a model"))?;
                if !budget_host::priced_vendor_model(vendor.engine_id(), model) {
                    return Err(invalid(&format!("no native budget price for {}:{model}", vendor.engine_id())));
                }
            }
            if options.resume {
                return Err(invalid("budgeted native resume requires durable spend accounting"));
            }
            Some(value)
        }
        _ => None,
    };
    // Fixture sessions normally retain the exact cwd named by tests. An
    // explicit DOXA_WORKTREE=1 opts the fixture into lifecycle testing.
    let manage_fixture = options.engine == Engine::Fixture
        && env::var("DOXA_WORKTREE").is_ok_and(|value| value == "1");
    let use_worktrees = options.engine != Engine::Fixture || manage_fixture;
    let mut managed = if use_worktrees && options.resume && !options.cwd.exists() {
        Some(doxa_worktrees::recover_missing(&options.cwd, &options.session_id)
            .map_err(|message| invalid(&format!("managed worktree recovery refused: {message}")))?)
    } else if use_worktrees {
        doxa_worktrees::create_from(&options.cwd, &options.session_id, options.base_branch.as_deref())
    } else { None };
    if options.base_branch.is_some() && managed.is_none() {
        return Err(invalid("requested branch could not be opened in a managed worktree; inspect conflicting doxa/ branches and worktree metadata; original checkout was not changed"));
    }
    if use_worktrees && managed.is_none() && doxa_worktrees::enabled()
        && doxa_worktrees::is_supported_checkout(&options.cwd) {
        return Err(invalid(&format!(
            "managed worktree unavailable for {}; inspect conflicting doxa/ branches and DOXA_HOME/worktrees, or explicitly set DOXA_WORKTREE=0 to use this checkout",
            options.cwd.display()
        )));
    }
    if options.resume {
        if let Some(tree) = &managed {
            match doxa_worktrees::repo_status(tree.path()) {
                Some(doxa_worktrees::RepoStatus::Repository {
                    checked_out: Some(ref checked_out), worktree: Some(ref branch), ..
                }) if checked_out == branch => {},
                _ => return Err(invalid("managed worktree branch changed; resume refused")),
            }
        }
    }
    if let Some(tree) = &managed { options.cwd = tree.path().to_path_buf(); }
    let mut codex_host = None;
    let mut claude_host = None;
    let mut vendor_host = None;
    let host: Arc<dyn Host> = match options.engine {
        Engine::Fixture => Arc::new(FixtureHost),
        Engine::Codex => {
            let mut driver = DriverOptions::new(options.cwd.clone());
            driver.executable = options
                .codex_bin
                .clone()
                .expect("validated Codex executable");
            driver.model = options.model.clone();
            driver.effort = options.effort.clone();
            driver.sandbox = options.sandbox;
            let host = Arc::new(
                CodexHost::new(
                    driver,
                    options
                        .lore_python
                        .as_ref()
                        .expect("validated LORE interpreter"),
                    &options.session_id,
                    options.resume,
                )
                .map_err(io::Error::other)?,
            );
            codex_host = Some(host.clone());
            host
        }
        Engine::Claude => {
            let host = Arc::new(
                ClaudeHost::new(
                    options
                        .claude_python
                        .as_ref()
                        .expect("validated Claude interpreter"),
                    options
                        .claude_script
                        .as_ref()
                        .expect("validated Claude script"),
                    &options.cwd,
                    &options.session_id,
                    options.resume,
                    options.model.as_deref(),
                )
                .map_err(io::Error::other)?,
            );
            claude_host = Some(host.clone());
            host
        }
        Engine::DeepSeek | Engine::Glm => {
            let host = Arc::new(
                VendorHost::new(
                    options.engine.vendor().expect("vendor engine"),
                    options.model.clone().expect("validated model"),
                    options.effort.clone().expect("validated effort"),
                    options
                        .lore_python
                        .as_ref()
                        .expect("validated LORE interpreter"),
                    &options.cwd,
                    &options.session_id,
                    options.resume,
                    #[cfg(feature = "local-test-server")]
                    options.vendor_endpoint.clone(),
                )
                .map_err(io::Error::other)?,
            );
            vendor_host = Some(host.clone());
            host
        }
    };
    let host: Arc<dyn Host> = match ceiling {
        Some(value) if options.engine.vendor().is_some() => Arc::new(
            BudgetHost::new_priced(host, value, options.engine.name(), options.model.as_deref().expect("validated budget model"))
                .map_err(|error| invalid(&error))?),
        Some(value) => Arc::new(BudgetHost::new(host, value)),
        None => host,
    };
    let scrub_python = match options.engine {
        Engine::Codex => options.lore_python.as_deref(),
        Engine::Claude => options.claude_python.as_deref(),
        Engine::DeepSeek | Engine::Glm => options.lore_python.as_deref(),
        Engine::Fixture => None,
    };
    let (event_tx, event_rx) = mpsc::sync_channel(256);
    let peer_host = Arc::new(PeerHost::new(
        host,
        options.runtime.clone(),
        &options.cwd,
        options.session_id.clone(),
        format!("DOXA Rust {} session", options.engine.name()),
        scrub_python,
        event_tx,
    )?);
    let host: Arc<dyn Host> = peer_host.clone();
    let session = Session {
        session_id: options.session_id.clone(),
        cwd: options.cwd.to_string_lossy().into_owned(),
        model: options.model.clone(),
        engine: options.engine.name().into(),
        doxa_version: env!("CARGO_PKG_VERSION").into(),
    };
    let mut handle = Daemon::bind(&options.runtime, session, host)?.start();
    let inbox = Inbox::bind(&options.runtime, &options.session_id)?;
    let mut registry = Registry::new(&options, inbox.path(), handle.socket_path())?;
    registry.write(0)?;
    unsafe {
        libc::signal(
            libc::SIGTERM,
            signal_handler as *const () as libc::sighandler_t,
        );
        libc::signal(
            libc::SIGINT,
            signal_handler as *const () as libc::sighandler_t,
        );
    }
    let mut had_client = false;
    let mut empty_since = Instant::now();
    let mut last_beat = Instant::now();
    let mut previous_clients = 0;
    let result = loop {
        while let Ok(event) = event_rx.try_recv() {
            handle.publish(event);
        }
        if let Ok(Some(frame)) = inbox.poll_receive(&|s: &str| s.to_owned()) {
            let broadcast = frame.kind.as_deref() == Some("broadcast");
            if let Ok(event) = peer_host.inbound_event(frame) {
                handle.publish(event.clone());
                let accepted = if inbound_turns && !broadcast {
                    match PeerHost::peer_prompt(&event) {
                        Some((prompt, origin)) => match handle.enqueue_peer_prompt(prompt, &origin) {
                            Ok(ExternalPrompt::Started | ExternalPrompt::Queued) => true,
                            Ok(ExternalPrompt::Full) => false,
                            Err(_) => false,
                        },
                        None => false,
                    }
                } else { false };
                if !accepted && !peer_host.retain_pending(event) {
                    handle.publish(json!({"type":"peer_pending_full","data":{
                        "message":"Peer message was shown but could not be retained for a later turn"}}));
                }
            }
        }
        if TERMINATE.load(Ordering::Acquire) || handle.is_stopping() {
            break Ok(());
        }
        let clients = handle.attached_clients();
        if clients > 0 {
            had_client = true;
            empty_since = Instant::now();
        } else if previous_clients > 0 {
            empty_since = Instant::now();
        }
        let delay = if had_client {
            options.linger
        } else {
            options.linger.max(Duration::from_secs(120))
        };
        if clients == 0 && empty_since.elapsed() >= delay {
            break Ok(());
        }
        if clients != previous_clients || last_beat.elapsed() >= Duration::from_secs(15) {
            if let Err(error) = registry.write(clients) {
                break Err(error);
            }
            last_beat = Instant::now();
        }
        previous_clients = clients;
        thread::sleep(Duration::from_millis(20));
    };
    if let Some(host) = &codex_host {
        if !host.shutdown() {
            eprintln!("doxa-daemon: Codex process did not finish after cancellation");
        }
    }
    if let Some(host) = &claude_host {
        if !host.shutdown() {
            eprintln!("doxa-daemon: Claude sidecar did not finalize cleanly");
        }
    }
    if let Some(host) = &vendor_host {
        host.shutdown();
    }
    handle.shutdown();
    if let Some(tree) = &mut managed {
        let note = tree.finish();
        if !note.is_empty() { eprintln!("doxa-daemon: {note}"); }
    }
    result
}
fn main() {
    if let Err(error) = run() {
        eprintln!("doxa-daemon: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn linger_rejects_values_that_would_overflow_or_stall_shutdown() {
        assert_eq!(linger_duration("31536000").unwrap(), Duration::from_secs(31_536_000));
        assert!(linger_duration("1e308").is_err());
        assert!(linger_duration("31536001").is_err());
        assert!(linger_duration("NaN").is_err());
    }

    #[test]
    fn python_validation_keeps_venv_link_and_imports_outside_checkout() {
        let dir = tempfile::tempdir().unwrap();
        let venv = dir.path().join("venv");
        assert!(Command::new("python3")
            .args(["-m", "venv", "--without-pip"])
            .arg(&venv)
            .status()
            .unwrap()
            .success());
        let python = venv.join("bin/python3");
        assert!(fs::symlink_metadata(&python).unwrap().file_type().is_symlink());
        let selected = python_executable(python.clone()).unwrap();
        assert_eq!(selected, python);
        assert_ne!(executable(python.clone()).unwrap(), python);
        let site = Command::new(&selected)
            .args(["-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"])
            .output()
            .unwrap();
        assert!(site.status.success());
        let site = PathBuf::from(String::from_utf8(site.stdout).unwrap().trim());
        fs::write(site.join("doxa_venv_marker.py"), "VALUE = 'venv only'\n").unwrap();
        let imported = Command::new(&selected)
            .args(["-c", "import doxa_venv_marker, sys; assert doxa_venv_marker.VALUE == 'venv only'; print(sys.prefix)"])
            .current_dir("/")
            .output()
            .unwrap();
        assert!(imported.status.success());
        assert_eq!(String::from_utf8(imported.stdout).unwrap().trim(), venv.to_str().unwrap());
    }
}
