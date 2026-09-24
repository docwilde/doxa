//! Native daemon startup and CLI settings shared with the Rust frontend.
use crate::discovery::{self, Session};
use std::env;
use std::fs::{self, File};
use std::io::{self, Read};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Debug, Default, Clone)]
pub struct LaunchOptions {
    pub model: Option<String>,
    pub linger: Option<f64>,
    pub sandbox: Option<String>,
    pub codex_bin: Option<PathBuf>,
    pub lore_python: Option<PathBuf>,
    pub fixture: bool,
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

/// Resolve a program to an absolute, executable, canonical path. A command
/// name is searched on PATH; a path containing a slash is never searched.
pub fn executable(input: &Path) -> io::Result<PathBuf> {
    let candidates: Vec<PathBuf> = if input.components().count() > 1 || input.is_absolute() {
        vec![input.to_path_buf()]
    } else {
        env::split_paths(&env::var_os("PATH").unwrap_or_default())
            .filter(|dir| dir.is_absolute())
            .map(|dir| dir.join(input))
            .collect()
    };
    for candidate in candidates {
        if let Ok(path) = fs::canonicalize(candidate) {
            if let Ok(meta) = fs::metadata(&path) {
                if meta.is_file() && meta.permissions().mode() & 0o111 != 0 {
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
    let cwd = fs::canonicalize(env::current_dir()?)?;
    let runtime = discovery::runtime_dir()?;
    if !runtime.is_absolute() {
        return Err(invalid("runtime directory must be absolute"));
    }
    let cfg = config();
    let model = options
        .model
        .clone()
        .or_else(|| configured_string("model", "DOXA_MODEL", None))
        .or_else(|| {
            cfg.as_ref()
                .and_then(|c| c.get("models"))
                .and_then(|m| m.get("codex"))
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
    if !linger.is_finite() || linger < 0.0 {
        return Err(invalid("linger must be a nonnegative finite number"));
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
    let id = random_id()?;
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
    if options.fixture {
        command.args(["--engine", "fixture"]);
    } else {
        let codex = executable(options.codex_bin.as_deref().unwrap_or(Path::new("codex")))?;
        let python = executable(
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
        if let Some(model) = &model {
            command.arg("--model").arg(model);
        }
    }
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let sessions = match discovery::sessions() {
            Ok(sessions) => sessions,
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        if let Some(session) = sessions.into_iter().find(|s| s.id == id) {
            return Ok(session);
        }
        if let Some(status) = child.try_wait()? {
            return Err(io::Error::other(format!("native daemon exited before startup ({status}); check Codex authentication and LORE availability")));
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
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
    client
        .call("stop", serde_json::Map::new())
        .map_err(io::Error::other)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

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
        assert_eq!(first.len(), 32);
        assert_ne!(first, second);
    }
}
