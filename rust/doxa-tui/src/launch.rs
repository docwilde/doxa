//! Native daemon startup and CLI settings shared with the Rust frontend.
use crate::discovery::{self, Session};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
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
}

impl Engine {
    pub fn vendor_key(self) -> Option<&'static str> {
        match self {
            Self::DeepSeek => Some("DEEPSEEK_API_KEY"),
            Self::Glm => Some("ZAI_API_KEY"),
            _ => None,
        }
    }

    fn model_key(self) -> &'static str {
        match self {
            Self::Codex => "codex",
            Self::Claude => "claude",
            Self::DeepSeek => "deepseek",
            Self::Glm => "glm",
            Self::Fixture => "fixture",
        }
    }
}

#[derive(Debug, Default, Clone)]
pub struct LaunchOptions {
    pub engine: Engine,
    /// Recorded project directory for a verified historical resume.
    pub cwd: Option<PathBuf>,
    pub branch: Option<String>,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub linger: Option<f64>,
    pub sandbox: Option<String>,
    pub codex_bin: Option<PathBuf>,
    pub lore_python: Option<PathBuf>,
    pub claude_python: Option<PathBuf>,
    pub claude_script: Option<PathBuf>,
    pub resume: Option<String>,
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

/// Resolve a program to an absolute executable path. A command name is
/// searched on PATH; a path containing a slash is never searched. Python's
/// final symlink must survive: it identifies a venv for Python's sys.prefix.
fn resolve_executable(input: &Path, preserve_python_link: bool) -> io::Result<PathBuf> {
    let candidates: Vec<PathBuf> = if input.components().count() > 1 || input.is_absolute() {
        vec![input.to_path_buf()]
    } else {
        env::split_paths(&env::var_os("PATH").unwrap_or_default())
            .filter(|dir| dir.is_absolute())
            .map(|dir| dir.join(input))
            .collect()
    };
    for candidate in candidates {
        if let Ok(meta) = fs::metadata(&candidate) {
            if meta.is_file() && meta.permissions().mode() & 0o111 != 0 {
                if preserve_python_link {
                    if let (Some(parent), Some(name)) = (candidate.parent(), candidate.file_name()) {
                        if let Ok(parent) = fs::canonicalize(parent) {
                            return Ok(parent.join(name));
                        }
                    }
                } else if let Ok(path) = fs::canonicalize(&candidate) {
                    return Ok(path);
                }
            }
        }
    }
    Err(io::Error::new(
        io::ErrorKind::NotFound,
        format!("executable not found: {}", input.display()),
    ))
}

pub fn executable(input: &Path) -> io::Result<PathBuf> {
    resolve_executable(input, false)
}

pub fn python_executable(input: &Path) -> io::Result<PathBuf> {
    resolve_executable(input, true)
}

/// The sidecar is Python source, not an executable. Require a real absolute
/// file so the daemon never receives a relative path resolved in another cwd.
pub fn claude_script(input: &Path) -> io::Result<PathBuf> {
    if !input.is_absolute() {
        return Err(invalid("Claude sidecar path must be absolute"));
    }
    let path = fs::canonicalize(input)?;
    if !fs::metadata(&path)?.is_file() {
        return Err(invalid("Claude sidecar must be a file"));
    }
    Ok(path)
}

fn claude_script_at(options: &LaunchOptions, override_path: Option<PathBuf>, executable: &Path) -> io::Result<PathBuf> {
    let candidate = options.claude_script.clone()
        .or(override_path)
        .unwrap_or_else(|| executable.with_file_name("doxa-claude-sidecar.py"));
    if options.claude_script.is_none() && !candidate.exists() {
        return Err(invalid("Claude sidecar missing; install the Rust preview or set DOXA_CLAUDE_SCRIPT"));
    }
    claude_script(&candidate)
}

pub fn resolve_claude_script(options: &LaunchOptions) -> io::Result<PathBuf> {
    claude_script_at(options, env::var_os("DOXA_CLAUDE_SCRIPT").map(PathBuf::from), &env::current_exe()?)
}

pub fn claude_dependencies(options: &LaunchOptions) -> io::Result<(PathBuf, PathBuf)> {
    let python = python_executable(
        options
            .claude_python
            .as_deref()
            .unwrap_or(Path::new("python3")),
    )?;
    Ok((python, resolve_claude_script(options)?))
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
    let source = fs::read_to_string(home.join("config.toml")).ok()?;
    source.parse().ok()
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

fn random_id() -> io::Result<String> {
    let mut bytes = [0_u8; 16];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

pub fn spawn(options: &LaunchOptions) -> io::Result<Session> {
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
    let runtime = discovery::runtime_dir()?;
    if !runtime.is_absolute() {
        return Err(invalid("runtime directory must be absolute"));
    }
    let cfg = config();
    let model = options
        .model
        .clone()
        .or_else(|| {
            if options.engine == Engine::Codex && options.resume.is_some() { return None; }
            if options.engine.vendor_key().is_none() {
                configured_string("model", "DOXA_MODEL", None)
            } else {
                None
            }
        })
        .or_else(|| {
            if options.engine == Engine::Codex && options.resume.is_some() { return None; }
            cfg.as_ref()
                .and_then(|c| c.get("models"))
                .and_then(|m| m.get(options.engine.model_key()))
                .and_then(toml::Value::as_str)
                .map(str::to_owned)
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
            Engine::Codex | Engine::Claude | Engine::DeepSeek | Engine::Glm
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
        if discovery::sessions()?.iter().any(|session| session.id == id) {
            return Err(invalid("session is already running; attach to it instead"));
        }
    }
    let mut command = Command::new(daemon);
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
    if let Some(base) = &branch { command.args(["--base-branch", base]); }
    match options.engine {
        Engine::Fixture => {
            if options.model.is_some()
                || options.effort.is_some()
                || options.sandbox.is_some()
                || options.codex_bin.is_some()
                || options.lore_python.is_some()
                || options.claude_python.is_some()
                || options.claude_script.is_some()
                || options.resume.is_some()
            {
                return Err(invalid(
                    "engine-specific options cannot be used with fixture",
                ));
            }
            command.args(["--engine", "fixture"]);
        }
        Engine::Codex => {
            if options.claude_python.is_some()
                || options.claude_script.is_some()
                || options.effort.is_some()
            {
                return Err(invalid("Claude options require --engine claude"));
            }
            let codex = executable(options.codex_bin.as_deref().unwrap_or(Path::new("codex")))?;
            let python = python_executable(
                options
                    .lore_python
                    .as_deref()
                    .unwrap_or(Path::new("python3")),
            )?;
            command.args(["--engine", "codex", "--codex-bin"]);
            command.arg(codex);
            command
                .arg("--lore-python")
                .arg(python)
                .args(["--sandbox", sandbox]);
            if options.resume.is_some() {
                command.args(["--resume", "true"]);
            }
            if let Some(model) = &model {
                command.arg("--model").arg(model);
            }
        }
        Engine::Claude => {
            if options.codex_bin.is_some()
                || options.lore_python.is_some()
                || options.sandbox.is_some()
                || options.effort.is_some()
            {
                return Err(invalid("Codex options require --engine codex"));
            }
            let (python, script) = claude_dependencies(options)?;
            command
                .args(["--engine", "claude"])
                .arg("--claude-python")
                .arg(python)
                .arg("--claude-script")
                .arg(script);
            if options.resume.is_some() {
                command.args(["--resume", "true"]);
            }
            if let Some(model) = &model {
                command.arg("--model").arg(model);
            }
        }
        Engine::DeepSeek | Engine::Glm => {
            if options.codex_bin.is_some()
                || options.sandbox.is_some()
                || options.claude_python.is_some()
                || options.claude_script.is_some()
            {
                return Err(invalid("unsupported option for vendor engine"));
            }
            let key = options.engine.vendor_key().expect("vendor engine");
            if !env::var(key).is_ok_and(|value| !value.is_empty()) {
                return Err(invalid(format!("{key} is required for native vendor chat")));
            }
            vendor_effort(options)?;
            let python = python_executable(
                options
                    .lore_python
                    .as_deref()
                    .unwrap_or(Path::new("python3")),
            )?;
            let name = options.engine.model_key();
            command
                .args(["--engine", name])
                .arg("--lore-python")
                .arg(python);
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
    }
    // Keep a private startup diagnostic so a failed daemon can tell the TUI
    // why it refused to launch (including worktree safety failures).
    let stderr_path = env::temp_dir().join(format!(".doxa-daemon-{id}-{}.stderr", random_id()?));
    let stderr_file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&stderr_path)?;
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
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let sessions = match discovery::sessions() {
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
                    Engine::Claude => "Claude SDK and sidecar",
                    Engine::Codex => "Codex authentication and LORE",
                    Engine::Fixture => "fixture",
                    Engine::DeepSeek | Engine::Glm => "vendor API key and LORE",
                }
            )));
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            let _ = fs::remove_file(&stderr_path);
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "native daemon did not register within 10 seconds",
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
        assert_eq!(python_executable(&link).unwrap(), link);
        assert!(executable(&dir.path().join("missing")).is_err());
    }

    #[test]
    fn claude_sidecar_resolution_prefers_explicit_then_override_then_sibling() {
        let dir = tempfile::tempdir().unwrap();
        let executable = dir.path().join("doxa-rs");
        let bundled = dir.path().join("doxa-claude-sidecar.py");
        let override_path = dir.path().join("override.py");
        let explicit = dir.path().join("explicit.py");
        for path in [&bundled, &override_path, &explicit] {
            fs::write(path, "# safe fixture\n").unwrap();
        }
        let mut options = LaunchOptions::default();
        assert_eq!(claude_script_at(&options, None, &executable).unwrap(), bundled);
        assert_eq!(claude_script_at(&options, Some(override_path.clone()), &executable).unwrap(), override_path);
        options.claude_script = Some(explicit.clone());
        assert_eq!(claude_script_at(&options, Some(override_path), &executable).unwrap(), explicit);
        options.claude_script = Some(PathBuf::from("relative.py"));
        assert!(claude_script_at(&options, None, &executable).is_err());
    }

    #[test]
    fn session_ids_are_filename_safe_and_unique() {
        let first = random_id().unwrap();
        let second = random_id().unwrap();
        assert!(discovery::valid_id(&first));
        assert_eq!(first.len(), 32);
        assert_ne!(first, second);
    }
}
