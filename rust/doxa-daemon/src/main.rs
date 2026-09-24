//! Native DOXA protocol host. The fixture remains an explicit test mode.
mod claude_host;
mod codex_host;
use claude_host::ClaudeHost;
use codex_host::CodexHost;
use doxa_engines::codex_driver::{DriverOptions, SandboxMode};
use doxa_runtime::{Daemon, Host, Session};
use serde_json::{json, Value};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

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
}
impl Engine {
    fn name(self) -> &'static str {
        match self {
            Self::Fixture => "fixture",
            Self::Codex => "codex",
            Self::Claude => "claude",
        }
    }
}
struct Options {
    runtime: PathBuf,
    cwd: PathBuf,
    session_id: String,
    linger: Duration,
    engine: Engine,
    codex_bin: Option<PathBuf>,
    lore_python: Option<PathBuf>,
    claude_python: Option<PathBuf>,
    claude_script: Option<PathBuf>,
    resume: bool,
    model: Option<String>,
    sandbox: SandboxMode,
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
    let mut linger = Duration::from_secs(120);
    let mut engine = Engine::Fixture;
    let mut codex_bin = None;
    let mut lore_python = None;
    let mut claude_python = None;
    let mut claude_script = None;
    let mut resume = false;
    let mut model = None;
    let mut sandbox = SandboxMode::WorkspaceWrite;
    let mut args = env::args_os().skip(1);
    while let Some(arg) = args.next() {
        let value = args
            .next()
            .ok_or_else(|| invalid("missing argument value"))?;
        match arg.to_str() {
            Some("--runtime-dir") => runtime = PathBuf::from(value),
            Some("--cwd") => cwd = PathBuf::from(value),
            Some("--session-id") => {
                session_id = value.into_string().map_err(|_| invalid("invalid session id"))?;
                explicit_session_id = true;
            },
            Some("--engine") => engine = match value.to_str() {
                Some("fixture") => Engine::Fixture, Some("codex") => Engine::Codex,
                Some("claude") => Engine::Claude,
                _ => return Err(invalid("engine must be fixture, codex, or claude")),
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
            Some("--sandbox") => sandbox = match value.to_str() {
                Some("read-only") => SandboxMode::ReadOnly,
                Some("workspace-write") => SandboxMode::WorkspaceWrite,
                Some("danger-full-access") => SandboxMode::DangerFullAccess,
                _ => return Err(invalid("invalid sandbox")),
            },
            Some("--linger") => {
                let seconds: f64 = value.to_str().ok_or_else(|| invalid("invalid linger"))?
                    .parse().map_err(|_| invalid("invalid linger"))?;
                if !seconds.is_finite() || seconds < 0.0 { return Err(invalid("invalid linger")); }
                linger = Duration::from_secs_f64(seconds);
            }
            _ => return Err(invalid("usage: doxa-daemon [--runtime-dir PATH] [--cwd PATH] [--session-id ID] [--linger SECONDS] [--engine fixture|codex|claude] [--codex-bin PATH --lore-python PATH --claude-python PATH --claude-script PATH --model MODEL --sandbox MODE --resume true|false]")),
        }
    }
    // Do not let registry entries claim an unvalidated path or identity.
    cwd = fs::canonicalize(cwd)?;
    if !cwd.is_dir() {
        return Err(invalid("cwd must be a directory"));
    }
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
    if engine == Engine::Codex {
        codex_bin = Some(executable(
            codex_bin.ok_or_else(|| invalid("Codex needs --codex-bin"))?,
        )?);
        lore_python = Some(executable(
            lore_python.ok_or_else(|| invalid("Codex needs --lore-python"))?,
        )?);
        if claude_python.is_some() || claude_script.is_some() || resume {
            return Err(invalid("Claude options require --engine claude"));
        }
    } else if engine == Engine::Claude {
        if resume && !explicit_session_id {
            return Err(invalid("Claude resume needs --session-id"));
        }
        claude_python = Some(executable(
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
    } else if codex_bin.is_some()
        || lore_python.is_some()
        || claude_python.is_some()
        || claude_script.is_some()
        || resume
        || model.is_some()
        || sandbox != SandboxMode::WorkspaceWrite
    {
        return Err(invalid("Codex options require --engine codex"));
    }
    Ok(Options {
        runtime,
        cwd,
        session_id,
        linger,
        engine,
        codex_bin,
        lore_python,
        claude_python,
        claude_script,
        resume,
        model,
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
struct Registry {
    path: PathBuf,
    inode: Option<u64>,
    started_at: String,
    repo_root: Option<String>,
    cwd: String,
    session_id: String,
    socket: String,
    engine: Engine,
}
impl Registry {
    fn new(options: &Options, socket: &Path) -> io::Result<Self> {
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
            inode: None,
            started_at: iso_now(),
            repo_root,
            cwd: options.cwd.to_string_lossy().into_owned(),
            session_id: options.session_id.clone(),
            socket: socket.to_string_lossy().into_owned(),
            engine: options.engine,
        })
    }
    fn write(&mut self, clients: usize) -> io::Result<()> {
        let entry = json!({"session_id":self.session_id,"pid":std::process::id(),
            "socket_path":self.socket,"daemon_socket":self.socket,"cwd":self.cwd,
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
                if Some(meta.ino()) != self.inode {
                    return Err(io::Error::new(
                        io::ErrorKind::AlreadyExists,
                        "registry entry replaced",
                    ));
                }
            } else if self.inode.is_some() {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    "registry entry removed",
                ));
            }
            fs::rename(&tmp, &self.path)?;
            self.inode = Some(fs::symlink_metadata(&self.path)?.ino());
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
        if let (Some(inode), Ok(meta)) = (self.inode, fs::symlink_metadata(&self.path)) {
            if meta.ino() == inode {
                let _ = fs::remove_file(&self.path);
            }
        }
    }
}
fn run() -> io::Result<()> {
    let options = options()?;
    let mut codex_host = None;
    let mut claude_host = None;
    let host: Arc<dyn Host> = match options.engine {
        Engine::Fixture => Arc::new(FixtureHost),
        Engine::Codex => {
            let mut driver = DriverOptions::new(options.cwd.clone());
            driver.executable = options
                .codex_bin
                .clone()
                .expect("validated Codex executable");
            driver.model = options.model.clone();
            driver.sandbox = options.sandbox;
            let host = Arc::new(
                CodexHost::new(
                    driver,
                    options
                        .lore_python
                        .as_ref()
                        .expect("validated LORE interpreter"),
                    &options.session_id,
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
    };
    let session = Session {
        session_id: options.session_id.clone(),
        cwd: options.cwd.to_string_lossy().into_owned(),
        model: options.model.clone(),
        engine: options.engine.name().into(),
        doxa_version: "2.0.0-alpha.3".into(),
    };
    let mut handle = Daemon::bind(&options.runtime, session, host)?.start();
    let mut registry = Registry::new(&options, handle.socket_path())?;
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
    handle.shutdown();
    result
}
fn main() {
    if let Err(error) = run() {
        eprintln!("doxa-daemon: {error}");
        std::process::exit(1);
    }
}
