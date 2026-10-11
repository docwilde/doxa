//! Native daemon startup and CLI settings shared with the Rust frontend.
use crate::discovery::{self, Session};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

/// A year is long enough for an intentionally retained daemon and keeps
/// configured values within Duration and platform timer limits.
pub const MAX_LINGER_SECS: f64 = 31_536_000.0;

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub enum Engine {
    #[default]
    Codex,
    Claude,
    Fixture,
    DeepSeek,
    Glm,
    Router,
}

impl Engine {
    pub fn vendor_key(self) -> Option<&'static str> {
        match self {
            Self::DeepSeek => Some("DEEPSEEK_API_KEY"),
            Self::Glm => Some("ZAI_API_KEY"),
            _ => None,
        }
    }

    pub fn vendor_credential_status(self) -> io::Result<doxa_vendors::credentials::CredentialStatus> {
        let vendor = match self {
            Self::DeepSeek => doxa_vendors::Vendor::DeepSeek,
            Self::Glm => doxa_vendors::Vendor::Glm,
            _ => return Err(invalid("vendor credential check requires a vendor engine")),
        };
        doxa_vendors::credentials::status(vendor)
            .map_err(|_| invalid("Native vendor credential store is unavailable"))
    }

    fn model_key(self) -> &'static str {
        match self {
            Self::Codex => "codex",
            Self::Claude => "claude",
            Self::DeepSeek => "deepseek",
            Self::Glm => "glm",
            Self::Router => "router",
            Self::Fixture => "fixture",
        }
    }
}

#[derive(Debug, Default, Clone)]
pub struct LaunchOptions {
    pub isolation: Option<doxa_isolation::Profile>,
    pub engine: Engine,
    /// Recorded project directory for a verified historical resume.
    pub cwd: Option<PathBuf>,
    pub branch: Option<String>,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub linger: Option<f64>,
    pub sandbox: Option<String>,
    pub codex_bin: Option<PathBuf>,
    pub claude_bin: Option<PathBuf>,
    pub resume: Option<String>,
    pub router_config: Option<PathBuf>,
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

/// One wait-only worker owns every launched daemon after admission. Closing a
/// window never kills a detached daemon; process exit reparents live children.
fn daemon_reaper() -> &'static std::sync::mpsc::Sender<Child> {
    static REAPER: std::sync::OnceLock<std::sync::mpsc::Sender<Child>> = std::sync::OnceLock::new();
    REAPER.get_or_init(|| {
        let (sender, receiver) = std::sync::mpsc::channel::<Child>();
        thread::Builder::new().name("daemon-reaper".into()).spawn(move || {
            let mut children: Vec<Child> = Vec::new();
            loop {
                if let Ok(child) = receiver.recv_timeout(Duration::from_millis(50)) { children.push(child); }
                // Bound each intake burst so exited children are reaped even
                // when a fleet continuously supplies new handles.
                for _ in 0..63 {
                    match receiver.try_recv() { Ok(child) => children.push(child), Err(_) => break }
                }
                children.retain_mut(|child| !matches!(child.try_wait(), Ok(Some(_))));
            }
        }).expect("could not start daemon reaper");
        sender
    })
}

fn installed_codex(data: &Path) -> Option<PathBuf> {
    let providers = data.join("doxa/providers");
    // An existing but broken pointer must fail executable resolution, not fall
    // back to a different provider. executable() canonicalizes a valid pointer.
    if fs::symlink_metadata(providers.join("codex-current")).is_ok() {
        return Some(providers.join("codex-current/codex"));
    }
    let legacy = providers.join("codex-0.156.1-precompact-v1/codex");
    legacy.is_file().then_some(legacy)
}

/// Resolve a command from PATH or an explicit path to its executable file.
pub fn executable(input: &Path) -> io::Result<PathBuf> {
    let candidates: Vec<PathBuf> = if input.components().count() > 1 || input.is_absolute() {
        vec![input.to_path_buf()]
    } else {
        env::split_paths(&env::var_os("PATH").unwrap_or_default()).filter(|dir| dir.is_absolute()).map(|dir| dir.join(input)).collect()
    };
    for candidate in candidates {
        if fs::metadata(&candidate).is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0) {
            if let Ok(path) = fs::canonicalize(candidate) { return Ok(path); }
        }
    }
    Err(io::Error::new(io::ErrorKind::NotFound,format!("executable not found: {}",input.display())))
}

pub fn claude_executable(options: &LaunchOptions) -> io::Result<PathBuf> {
    executable(options.claude_bin.as_deref().unwrap_or(Path::new("claude")))
}

pub fn vendor_effort(options: &LaunchOptions) -> io::Result<()> {
    if let Some(effort) = &options.effort {
        if !(matches!(effort.as_str(), "low" | "high" | "max")
            || options.engine == Engine::DeepSeek && effort == "none")
        {
            return Err(invalid("invalid vendor effort"));
        }
    }
    Ok(())
}

pub fn daemon_binary() -> io::Result<PathBuf> {
    if let Some(path) = env::var_os("DOXA_DAEMON_BIN") {
        let path = PathBuf::from(path);
        if !path.is_absolute() {
            return Err(invalid("DOXA_DAEMON_BIN must be absolute"));
        }
        return executable(&path);
    }
    let sibling = env::current_exe()?.with_file_name("doxa-daemon-rs");
    if sibling.exists() {
        return executable(&sibling);
    }
    let sibling = env::current_exe()?.with_file_name("doxa-daemon");
    if sibling.exists() {
        return executable(&sibling);
    }
    executable(Path::new("doxa-daemon-rs"))
        .or_else(|_| executable(Path::new("doxa-daemon")))
        .map_err(|_| {
            io::Error::new(
                io::ErrorKind::NotFound,
                "native daemon missing; install or build doxa-daemon-rs, or set DOXA_DAEMON_BIN",
            )
        })
}

fn config() -> Option<toml::Value> {
    let home = env::var_os("DOXA_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|v| PathBuf::from(v).join(".doxa")))?;
    config_at(&home.join("config.toml"))
}

fn config_at(path: &Path) -> Option<toml::Value> {
    doxa_state::load_config_checked(path).ok().map(toml::Value::Table)
}

fn configured_string(key: &str, env_key: &str, config: Option<&toml::Value>) -> Option<String> {
    env::var(env_key)
        .ok()
        .filter(|v| !v.trim().is_empty())
        .or_else(|| {
            config
                .and_then(|c| c.get(key))
                .and_then(toml::Value::as_str)
                .map(str::to_owned)
        })
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty())
}

pub fn configured_engine() -> Engine {
    configured_engine_id(configured_string("engine", "DOXA_ENGINE", config().as_ref()).as_deref())
}
fn configured_engine_id(value:Option<&str>) -> Engine {
    match value.map(str::to_ascii_lowercase).as_deref() {
        Some("codex") => Engine::Codex, Some("deepseek") => Engine::DeepSeek, Some("glm") => Engine::Glm,
        Some("router") => Engine::Router,
        _ => Engine::Claude,
    }
}

/// Routing is enabled only by a named, owner-private config. No default file
/// or model shortlist is invented when a router session is requested.
pub fn router_config_path(explicit: Option<&Path>) -> io::Result<PathBuf> {
    let path=explicit.map(Path::to_path_buf).or_else(||
        configured_string("router_config","DOXA_ROUTER_CONFIG",config().as_ref()).map(PathBuf::from))
        .ok_or_else(||invalid("router needs --router-config PATH or an explicit router_config setting"))?;
    if !path.is_absolute() {return Err(invalid("router config path must be absolute"));}
    Ok(path)
}

pub fn router_config(explicit: Option<&Path>) -> io::Result<(PathBuf,doxa_router::Config)> {
    let path=router_config_path(explicit)?;
    let config=doxa_router::Config::load(&path).map_err(|_|invalid("router config is missing, unsafe, or invalid"))?;
    Ok((path,config))
}

fn configured_scalar(key: &str, env_key: &str, config: Option<&toml::Value>) -> Option<String> {
    env::var(env_key).ok().filter(|value| !value.trim().is_empty()).or_else(|| {
        let value = config?.get(key)?;
        match value {
            toml::Value::String(value) => Some(value.clone()),
            toml::Value::Boolean(value) => Some(if *value { "1" } else { "0" }.into()),
            toml::Value::Integer(value) => Some(value.to_string()),
            toml::Value::Float(value) => Some(value.to_string()), _ => None,
        }
    })
}

fn stored_model(engine: Engine, config: Option<&toml::Value>) -> Option<String> {
    let config = config?;
    let value = if engine == Engine::Claude { config.get("model") }
        else { config.get("models").and_then(|models| models.get(engine.model_key())) }?;
    value.as_str().map(str::trim).filter(|value| !value.is_empty()).map(str::to_owned)
}

fn random_id() -> io::Result<String> {
    let mut bytes = [0_u8; 16];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    Ok(format!("{}-{}-{}-{}-{}", &hex[..8], &hex[8..12], &hex[12..16], &hex[16..20], &hex[20..]))
}

fn saved_budget(session_id: &str) -> io::Result<Option<f64>> {
    let home = env::var_os("DOXA_HOME").filter(|value| !value.is_empty()).map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|value| PathBuf::from(value).join(".doxa")))
        .ok_or_else(|| invalid("DOXA home is unset"))?;
    if !home.is_absolute() { return Err(invalid("DOXA home must be absolute")); }
    let path = home.join("budgets").join(format!("{session_id}.json"));
    let file = match OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    let meta = file.metadata()?;
    if !meta.is_file() || meta.nlink() != 1 || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 || meta.len() > 65536 {
        return Err(invalid("untrusted resume budget journal"));
    }
    let mut bytes = Vec::new(); file.take(65537).read_to_end(&mut bytes)?;
    let value: serde_json::Value = serde_json::from_slice(&bytes)?;
    if value["version"] != 1 || value["identity"]["session_id"] != session_id || value["unknown"] != false {
        return Err(invalid("resume spend accounting is unavailable or unknown"));
    }
    let ceiling = value["identity"]["ceiling_usd"].as_f64().filter(|value| value.is_finite() && *value > 0.0)
        .ok_or_else(|| invalid("invalid saved resume budget"))?;
    Ok(Some(ceiling))
}

pub fn spawn(options: &LaunchOptions) -> io::Result<Session> {
    spawn_inner(options, None, &[],None)
}

/// Fleet-scoped child environment without changing the frontend process.
pub fn spawn_fleet(options: &LaunchOptions, runtime: &Path, budget: Option<f64>, inbound: bool, lore: bool) -> io::Result<Session> {
    if options.engine==Engine::Router {return Err(invalid("router fleet config inheritance is unavailable; start an explicit router session"));}
    let mut environment = vec![("DOXA_RUNTIME_DIR", runtime.to_string_lossy().into_owned()),
        ("DOXA_AGENT_PEER_SEND", "1".into()),
        ("DOXA_PEER_INBOUND_TURNS", if inbound { "1" } else { "0" }.into())];
    environment.push(("DOXA_LORE", if lore { "1" } else { "0" }.into()));
    environment.push(("DOXA_SESSION_BUDGET_USD", budget.map(|value| value.to_string()).unwrap_or_default()));
    let run = runtime.parent().ok_or_else(|| invalid("fleet runtime has no run root"))?;
    let ledger = run.join("home/peers/messages.jsonl");
    if !runtime.is_absolute() { return Err(invalid("fleet runtime must be absolute")); }
    for directory in [run.to_owned(), run.join("home"), run.join("home/peers")] {
        let metadata = fs::symlink_metadata(directory)?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() || metadata.uid() != unsafe { libc::geteuid() } || metadata.mode() & 0o077 != 0 {
            return Err(invalid("fleet ledger scope must be private and owned"));
        }
    }
    environment.push(("DOXA_PEER_LEDGER", ledger.to_string_lossy().into_owned()));
    spawn_inner(options, Some(runtime), &environment,None)
}

/// Reattach the same stopped conversation with its original admission policy.
pub(crate) fn spawn_migrated(options:&LaunchOptions,record:&serde_json::Value)->io::Result<Session>{
    if options.engine==Engine::Router {return Err(invalid("router isolation migration is unavailable"));}
    let runtime=Path::new(record["runtime"].as_str().ok_or_else(||invalid("migration runtime missing"))?);
    let mut environment=vec![("DOXA_PEER_INBOUND_TURNS",if record["inbound"]==true{"1"}else{"0"}.into()),
        ("DOXA_LORE",if record["lore"]==true{"1"}else{"0"}.into()),
        ("DOXA_SESSION_BUDGET_USD",record["ceiling"].as_f64().map(|n|n.to_string()).unwrap_or_default())];
    for key in ["DOXA_HOME","DOXA_PEER_LEDGER","DOXA_AGENT_PEER_SEND","LORE_STORE_DIR","LORE_DATA_DIR","LORE_RUNTIME_DIR"]{
        if let Some(value)=record["environment"][key].as_str(){environment.push((key,value.to_owned()));}
    }
    spawn_inner(options,Some(runtime),&environment,Some(record))
}

fn spawn_inner(options: &LaunchOptions, fleet_runtime: Option<&Path>, environment: &[(&str, String)], migration:Option<&serde_json::Value>) -> io::Result<Session> {
    let startup_seconds = if options.engine == Engine::Claude {
        doxa_state::claude_startup_seconds(env::var("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT").ok().as_deref())
            .map_err(invalid)?.1
    } else { 10 };
    let cfg = config();
    let mut effective = options.clone();
    if effective.resume.is_none() && !matches!(effective.engine,Engine::Fixture|Engine::Router) && effective.effort.is_none() {
        effective.effort = configured_string("effort", "DOXA_EFFORT", cfg.as_ref());
    }
    let options = &effective;
    if options.router_config.is_some() && options.engine!=Engine::Router {return Err(invalid("--router-config requires --engine router"));}
    let requested_cwd = options.cwd.clone().unwrap_or(env::current_dir()?);
    let cwd = match fs::canonicalize(&requested_cwd) {
        Ok(cwd) if cwd.is_dir() => cwd,
        Ok(_) => return Err(invalid("session directory is not a directory")),
        Err(error) if options.resume.is_some()
            && error.kind() == io::ErrorKind::NotFound
            && requested_cwd.is_absolute()
            && fs::symlink_metadata(&requested_cwd).is_err_and(|e| e.kind() == io::ErrorKind::NotFound) => {
            // The daemon owns recovery after it claims the session ID. Keep
            // the recorded absolute path so it can prove exact ownership.
            requested_cwd
        }
        Err(error) => return Err(error),
    };
    let branch = if let Some(requested) = options.branch.as_deref() {
        if !doxa_worktrees::enabled() {
            return Err(invalid("--branch needs worktree_per_session; turn it on or change your checkout explicitly with git"));
        }
        if options.engine == Engine::Fixture && env::var("DOXA_WORKTREE").as_deref() != Ok("1") {
            return Err(invalid("fixture --branch needs DOXA_WORKTREE=1"));
        }
        if !doxa_worktrees::is_supported_checkout(&cwd) {
            return Err(invalid("--branch needs a Git checkout"));
        }
        Some(doxa_worktrees::resolve_base(&cwd, requested)
            .ok_or_else(|| invalid(format!("no such local or remote-tracking branch: {requested}")))?)
    } else { None };
    if branch.is_some() && options.resume.is_some() {
        return Err(invalid("--branch cannot change the base of a resumed session"));
    }
    let runtime = match fleet_runtime { Some(path) => path.to_path_buf(), None => discovery::runtime_dir()? };
    if !runtime.is_absolute() {
        return Err(invalid("runtime directory must be absolute"));
    }
    let model = options
        .model
        .clone()
        .or_else(|| {
            if options.resume.is_some() || matches!(options.engine,Engine::Fixture|Engine::Router) { return None; }
            configured_string("model", "DOXA_MODEL", None)
        })
        .or_else(|| {
            if options.engine == Engine::Codex && options.resume.is_some() { return None; }
            if options.resume.is_some() { None } else { stored_model(options.engine, cfg.as_ref()) }
        });
    let linger = options
        .linger
        .or_else(|| {
            configured_string("linger_secs", "DOXA_LINGER_SECS", cfg.as_ref())
                .and_then(|s| s.parse::<f64>().ok())
                .or_else(|| {
                    cfg.as_ref()
                        .and_then(|c| c.get("linger_secs"))
                        .and_then(|v| v.as_float().or_else(|| v.as_integer().map(|n| n as f64)))
                })
        })
        .unwrap_or(120.0);
    if !linger.is_finite() || !(0.0..=MAX_LINGER_SECS).contains(&linger) {
        return Err(invalid("linger must be a finite number between 0 and 31536000 seconds"));
    }
    if model
        .as_ref()
        .is_some_and(|m| m.is_empty() || m.len() > 128 || m.chars().any(char::is_control))
    {
        return Err(invalid("invalid model"));
    }
    let sandbox = options.sandbox.as_deref().unwrap_or("workspace-write");
    if !matches!(
        sandbox,
        "read-only" | "workspace-write" | "danger-full-access"
    ) {
        return Err(invalid("invalid sandbox"));
    }
    let daemon = daemon_binary()?;
    let id = if let Some(id) = &options.resume {
        if !matches!(
            options.engine,
            Engine::Codex | Engine::Claude | Engine::DeepSeek | Engine::Glm | Engine::Router
        ) || !discovery::valid_id(id)
        {
            return Err(invalid(
                "resume needs a valid full session ID and a supported engine",
            ));
        }
        id.clone()
    } else {
        random_id()?
    };
    if options.resume.is_some() {
        // A second daemon with the same conversation ID would create two
        // writers. The UI checks earlier; this is the final pre-spawn gate.
        if discovery::sessions_in(&runtime)?.iter().any(|session| session.id == id) {
            return Err(invalid("session is already running; attach to it instead"));
        }
    }
    let mut command = Command::new(daemon);
    for (key, env_key) in [("peer_inbound_turns", "DOXA_PEER_INBOUND_TURNS"), ("session_budget_usd", "DOXA_SESSION_BUDGET_USD")] {
        if !environment.iter().any(|(candidate, _)| *candidate == env_key) {
            if let Some(value) = configured_scalar(key, env_key, cfg.as_ref()) {
                let value = if key == "session_budget_usd" && value.trim().parse::<f64>().ok() == Some(0.0) { String::new() } else { value };
                command.env(env_key, value);
            }
        }
    }
    if let Some(value) = crate::preferences::lore_notify_override() { command.env("LORE_NOTIFY", value); }
    for (key, value) in environment { command.env(key, value); }
    if environment.is_empty() && options.resume.is_some() && options.engine!=Engine::Router {
        if let Some(ceiling) = saved_budget(&id)? {
            // Restore the original allowance, never a fresh allowance. The
            // daemon verifies engine/model/cwd and the durable spent total.
            command.env("DOXA_SESSION_BUDGET_USD", ceiling.to_string());
        }
    }
    command.args([
        "--runtime-dir",
        runtime
            .to_str()
            .ok_or_else(|| invalid("invalid runtime path"))?,
        "--cwd",
        cwd.to_str().ok_or_else(|| invalid("invalid cwd"))?,
        "--session-id",
        &id,
        "--linger",
        &linger.to_string(),
    ]);
    if let Some(profile) = options.isolation { command.args(["--isolation", profile.key()]); }
    if let Some(record)=migration{
        let depth=record["spawn_depth"].as_u64().ok_or_else(||invalid("migration lineage missing"))?;
        command.args(["--spawn-depth",&depth.to_string()]);
        if let Some(parent)=record["parent_session_id"].as_str(){command.args(["--parent-session-id",parent]);}
    }
    if let Some(base) = &branch { command.args(["--base-branch", base]); }
    match options.engine {
        Engine::Fixture => {
            if options.model.is_some()
                || options.effort.is_some()
                || options.sandbox.is_some()
                || options.codex_bin.is_some()
                || options.claude_bin.is_some()
                || options.resume.is_some()
            {
                return Err(invalid(
                    "engine-specific options cannot be used with fixture",
                ));
            }
            command.args(["--engine", "fixture"]);
        }
        Engine::Codex => {
            if options.claude_bin.is_some() {
                return Err(invalid("Claude options require --engine claude"));
            }
            let installed = env::var_os("XDG_DATA_HOME").map(PathBuf::from)
                .or_else(|| env::var_os("HOME").map(|home| PathBuf::from(home).join(".local/share")))
                .and_then(|data| installed_codex(&data));
            let default = installed.as_deref().unwrap_or(Path::new("codex"));
            let codex = executable(options.codex_bin.as_deref().unwrap_or(default))?;
            command.args(["--engine", "codex", "--codex-bin"]).arg(codex).args(["--sandbox", sandbox]);
            if options.resume.is_some() {
                command.args(["--resume", "true"]);
            }
            if let Some(model) = &model {
                command.arg("--model").arg(model);
            }
            if let Some(effort) = &options.effort { command.arg("--effort").arg(effort); }
        }
        Engine::Claude => {
            if options.codex_bin.is_some()
                || options.sandbox.is_some()
            {
                return Err(invalid("Codex options require --engine codex"));
            }
            command.args(["--engine", "claude", "--claude-bin"]).arg(claude_executable(options)?);
            if options.resume.is_some() {
                command.args(["--resume", "true"]);
            }
            if let Some(model) = &model {
                command.arg("--model").arg(model);
            }
            if let Some(effort) = &options.effort { command.arg("--effort").arg(effort); }
        }
        Engine::DeepSeek | Engine::Glm => {
            if options.codex_bin.is_some()
                || options.sandbox.is_some()
                || options.claude_bin.is_some()
            {
                return Err(invalid("unsupported option for vendor engine"));
            }
            let key = options.engine.vendor_key().expect("vendor engine");
            if options.engine.vendor_credential_status()? == doxa_vendors::credentials::CredentialStatus::Missing {
                return Err(invalid(format!("{key} is required for native vendor chat")));
            }
            vendor_effort(options)?;
            command.args(["--engine", options.engine.model_key()]);
            if let Some(model) = &model {
                command.arg("--model").arg(model);
            }
            if let Some(effort) = &options.effort {
                command.arg("--effort").arg(effort);
            }
            if options.resume.is_some() {
                command.args(["--resume", "true"]);
            }
        }
        Engine::Router => {
            if options.codex_bin.is_some() || options.claude_bin.is_some() || options.sandbox.is_some() || options.effort.is_some() {
                return Err(invalid("router targets define their effort; CLI executables and sandbox overrides are unsupported"));
            }
            let profile=options.isolation.unwrap_or(doxa_isolation::configured_profile(&doxa_isolation::home()?)?);
            if profile!=doxa_isolation::Profile::Native {return Err(invalid("router currently requires native isolation"));}
            let (path,config)=router_config(options.router_config.as_deref())?;
            if model.as_deref().is_some_and(|id| id!="auto" && config.candidate(id).is_none()) {
                return Err(invalid("router model must be auto or an exact configured target ID"));
            }
            command.args(["--engine","router","--router-config"]).arg(path);
            if let Some(model)=&model {command.arg("--model").arg(model);}
            if options.resume.is_some() {command.args(["--resume","true"]);}
        }
    }
    // Keep a private startup diagnostic so a failed daemon can tell the TUI
    // why it refused to launch (including worktree safety failures).
    let registry = doxa_peers::Registry::open(&runtime)?;
    let stderr_path = registry.runtime().join(format!(".doxa-daemon-{id}-{}.stderr", random_id()?));
    let stderr_file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&stderr_path)?;
    let reaper = daemon_reaper();
    let mut child = match command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::from(stderr_file))
        .spawn()
    {
        Ok(child) => child,
        Err(error) => {
            let _ = fs::remove_file(&stderr_path);
            return Err(error);
        }
    };
    let deadline = Instant::now() + Duration::from_secs(startup_seconds);
    loop {
        let sessions = match discovery::sessions_in(&runtime) {
            Ok(sessions) => sessions,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = fs::remove_file(&stderr_path);
                return Err(error);
            }
        };
        let expected_socket = format!("daemon-{}-{}.sock", &id[..id.len().min(8)], child.id());
        if let Some(session) = sessions.into_iter().find(|s| {
            s.id == id
                && s.socket
                    .file_name()
                    .is_some_and(|name| name == expected_socket.as_str())
        }) {
            let _ = fs::remove_file(&stderr_path);
            // Child has no wait-on-drop behavior. Transfer exact ownership
            // before returning the read-only session identity to the TUI.
            let _ = reaper.send(child);
            return Ok(session);
        }
        let status = match child.try_wait() {
            Ok(status) => status,
            Err(error) => {
                let _ = fs::remove_file(&stderr_path);
                return Err(error);
            }
        };
        if let Some(status) = status {
            let mut bytes = Vec::new();
            if let Ok(file) = File::open(&stderr_path) {
                let _ = file.take(4096).read_to_end(&mut bytes);
            }
            let _ = fs::remove_file(&stderr_path);
            let diagnostic = String::from_utf8_lossy(&bytes);
            let diagnostic = diagnostic.trim();
            if !diagnostic.is_empty() {
                return Err(io::Error::other(format!(
                    "native daemon exited before startup ({status}): {diagnostic}"
                )));
            }
            if branch.is_some() {
                return Err(io::Error::other(format!(
                    "native daemon exited before startup ({status}); check provider dependencies and whether the requested branch can open in a managed worktree"
                )));
            }
            return Err(io::Error::other(format!(
                "native daemon exited before startup ({status}); check {} dependencies",
                match options.engine {
                    Engine::Claude => "Claude CLI and authentication",
                    Engine::Codex => "Codex authentication and LORE",
                    Engine::Fixture => "fixture",
                    Engine::DeepSeek | Engine::Glm => "vendor API key and LORE",
                    Engine::Router => "router config, Typesafe and candidate API credentials",
                }
            )));
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            let _ = fs::remove_file(&stderr_path);
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                format!("native daemon did not register within {startup_seconds} seconds"),
            ));
        }
        thread::sleep(Duration::from_millis(25));
    }
}

pub fn stop(session: &Session) -> io::Result<()> {
    let mut client =
        crate::transport::DaemonClient::connect(&session.socket, None).map_err(io::Error::other)?;
    if client.hello["session_id"] != session.id {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "session identity changed during stop",
        ));
    }
    let reply = client
        .call("stop", serde_json::Map::new())
        .map_err(io::Error::other)?;
    if reply["ok"] != true {
        return Err(io::Error::other("daemon refused stop request"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn router_fixture() -> serde_json::Value {
        serde_json::json!({"version":1,"jev_model":"jev-1.13.0","criteria_version":"fixture-v1",
            "fallback_id":"ds-fixture","confidence_threshold":0.5,"max_calls":2,
            "max_spend_usd_micros":1000,"max_input_bytes":2048,"deadline_ms":1000,
            "candidates":[
                {"id":"ds-fixture","provider":"deepseek","model":"deepseek-flash","effort":"high","description":"fixture only","context_tokens":65536,"max_output_tokens":1024,"supports_tools":true,"input_usd_micros_per_million":1000000,"output_usd_micros_per_million":2000000},
                {"id":"glm-fixture","provider":"glm","model":"glm-5.3-flash","effort":"high","description":"fixture only","context_tokens":65536,"max_output_tokens":1024,"supports_tools":true,"input_usd_micros_per_million":1000000,"output_usd_micros_per_million":2000000}
            ]})
    }
    #[test]
    fn router_is_explicit_and_config_errors_do_not_disclose_private_input() {
        assert_eq!(configured_engine_id(Some("router")), Engine::Router);
        assert_eq!(configured_engine_id(None), Engine::Claude);
        assert!(router_config_path(Some(Path::new("relative.json"))).is_err());
        let dir=tempfile::tempdir().unwrap();
        let path=dir.path().join("private-router.json");
        fs::write(&path,serde_json::to_vec(&router_fixture()).unwrap()).unwrap();
        fs::set_permissions(&path,fs::Permissions::from_mode(0o600)).unwrap();
        let (loaded_path,config)=router_config(Some(&path)).unwrap();
        assert_eq!(loaded_path,path);
        assert!(config.candidate("ds-fixture").is_some());
        fs::write(&path,b"private malformed input").unwrap();
        let error=router_config(Some(&path)).unwrap_err().to_string();
        assert_eq!(error,"router config is missing, unsafe, or invalid");
        assert!(!error.contains("private-router"));
    }
    #[test]
    fn router_refuses_unpropagated_fleet_and_migration_before_host_actions() {
        let options=LaunchOptions{engine:Engine::Router,..Default::default()};
        assert!(spawn_fleet(&options,Path::new("/missing"),Some(1.0),false,false).unwrap_err().to_string().contains("config inheritance"));
        assert!(spawn_migrated(&options,&serde_json::json!({})).unwrap_err().to_string().contains("migration is unavailable"));
    }
    #[test]
    fn launcher_config_rejects_fifo_and_oversized_files() {
        use std::ffi::CString;
        use std::os::unix::ffi::OsStrExt;

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.toml");
        fs::write(&path, "engine = 'codex'\n").unwrap();
        assert_eq!(config_at(&path).unwrap()["engine"].as_str(), Some("codex"));

        fs::File::create(&path).unwrap().set_len(doxa_state::MAX_CONFIG_BYTES + 1).unwrap();
        assert!(config_at(&path).is_none());

        fs::remove_file(&path).unwrap();
        let name = CString::new(path.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        assert!(config_at(&path).is_none());
    }
    #[test]
    fn installed_provider_prefers_immutable_pointer_and_refuses_broken_pointer() {
        let dir = tempfile::tempdir().unwrap();
        let providers = dir.path().join("doxa/providers");
        let legacy = providers.join("codex-0.156.1-precompact-v1");
        fs::create_dir_all(&legacy).unwrap();
        fs::write(legacy.join("codex"), "legacy").unwrap();
        assert_eq!(installed_codex(dir.path()), Some(legacy.join("codex")));
        let artifact = providers.join("codex-artifact-fixture");
        fs::create_dir(&artifact).unwrap();
        fs::write(artifact.join("codex"), "#!/bin/sh\nexit 0\n").unwrap();
        fs::set_permissions(artifact.join("codex"), fs::Permissions::from_mode(0o700)).unwrap();
        symlink("codex-artifact-fixture", providers.join("codex-current")).unwrap();
        let selected = installed_codex(dir.path()).unwrap();
        assert_eq!(executable(&selected).unwrap(), artifact.join("codex"));
        fs::remove_file(artifact.join("codex")).unwrap();
        assert_eq!(installed_codex(dir.path()), Some(selected.clone()));
        assert!(executable(&selected).is_err());
    }

    #[test]
    fn admitted_daemon_reaper_waits_without_stopping_live_children() {
        let child = Command::new("/bin/sleep").arg("0.2").spawn().unwrap();
        let pid = child.id() as i32;
        daemon_reaper().send(child).unwrap();
        assert_eq!(unsafe { libc::kill(pid, 0) }, 0);
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            if unsafe { libc::kill(pid, 0) } == -1 { break; }
            assert!(Instant::now() < deadline, "completed daemon remained an unreaped child");
            thread::sleep(Duration::from_millis(10));
        }
    }
    #[test]
    fn model_preferences_remain_engine_scoped_and_do_not_cross_claude_defaults() {
        let config: toml::Value = "model = 'sonnet'\n[models]\ncodex = 'codex-model'\ndeepseek = 'deepseek-chat'\nglm = 'glm-model'".parse().unwrap();
        assert_eq!(stored_model(Engine::Claude, Some(&config)).as_deref(), Some("sonnet"));
        assert_eq!(stored_model(Engine::Codex, Some(&config)).as_deref(), Some("codex-model"));
        assert_eq!(stored_model(Engine::DeepSeek, Some(&config)).as_deref(), Some("deepseek-chat"));
        assert_eq!(stored_model(Engine::Fixture, Some(&config)), None);
        let legacy: toml::Value = "model = 'sonnet'".parse().unwrap();
        assert_eq!(stored_model(Engine::Codex, Some(&legacy)), None);
    }
    use std::io::{BufRead, BufReader, Write};
    use std::os::unix::fs::symlink;
    use std::os::unix::net::UnixListener;

    #[test]
    fn refused_stop_reply_is_not_reported_as_success() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("daemon.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            writeln!(
                stream,
                "{}",
                serde_json::json!({
                    "type":"hello", "proto":1, "session_id":"session-1", "cwd":"/tmp",
                    "engine":"fixture", "model":null, "next_seq":0
                })
            )
            .unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            assert_eq!(
                serde_json::from_str::<serde_json::Value>(&line).unwrap(),
                serde_json::json!({"type":"attach", "cursor":null})
            );
            line.clear();
            reader.read_line(&mut line).unwrap();
            let request: serde_json::Value = serde_json::from_str(&line).unwrap();
            assert_eq!(request["method"], "stop");
            writeln!(
                stream,
                "{}",
                serde_json::json!({
                    "type":"reply", "id":request["id"], "ok":false,
                    "error":"stop refused"
                })
            )
            .unwrap();
        });
        let session = Session {
            id: "session-1".into(),
            title: String::new(),
            socket,
            scope_key: "/tmp".into(),
            clients: Some(0),
            started_at: String::new(),
        };
        assert_eq!(
            stop(&session).unwrap_err().to_string(),
            "daemon refused stop request"
        );
        server.join().unwrap();
    }

    #[test]
    fn resolves_only_real_executable_files() {
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("codex");
        fs::write(&script, "#!/bin/sh\nexit 0\n").unwrap();
        assert!(executable(&script).is_err());
        fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
        assert_eq!(executable(&script).unwrap(), script);
        let link = dir.path().join("link");
        symlink(&script, &link).unwrap();
        assert_eq!(executable(&link).unwrap(), script);
        assert!(executable(&dir.path().join("missing")).is_err());
    }

    #[test]
    fn session_ids_are_filename_safe_and_unique() {
        let first = random_id().unwrap();
        let second = random_id().unwrap();
        assert!(discovery::valid_id(&first));
        assert_eq!(first.len(), 36);
        assert_eq!(&first[14..15], "4");
        assert!(matches!(&first[19..20], "8" | "9" | "a" | "b"));
        assert_eq!(first.chars().filter(|c| *c == '-').count(), 4);
        assert_ne!(first, second);
    }
}
