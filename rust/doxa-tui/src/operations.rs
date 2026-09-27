//! Native setup and provider authentication operations. Provider CLIs own their credentials; only probe
//! exit status is observed, and their output is never captured or displayed.

use std::io;
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthState {
    Missing,
    Authenticated,
    Unauthenticated,
    TimedOut,
}

struct Provider {
    name: &'static str,
    label: &'static str,
    binary: &'static str,
    probe: &'static [&'static str],
}

const PROVIDERS: &[Provider] = &[
    Provider { name: "claude", label: "Claude (Anthropic)", binary: "claude", probe: &["auth", "status"] },
    Provider { name: "codex", label: "Codex (OpenAI)", binary: "codex", probe: &["login", "status"] },
];

fn probe(binary: &Path, args: &[&str], timeout: Duration) -> AuthState {
    let Ok(mut child) = Command::new(binary).args(args)
        .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
        .process_group(0).spawn()
    else { return AuthState::Unauthenticated };
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return if status.success() { AuthState::Authenticated } else { AuthState::Unauthenticated },
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(25)),
            Ok(None) => {
                if let Ok(pid) = i32::try_from(child.id()) {
                    // Provider CLIs can spawn helpers. Reap the isolated group
                    // so a timed-out status probe leaves none running.
                    unsafe { libc::kill(-pid, libc::SIGKILL) };
                }
                let _ = child.kill();
                let _ = child.wait();
                return AuthState::TimedOut;
            }
            Err(_) => {
                if let Ok(pid) = i32::try_from(child.id()) {
                    unsafe { libc::kill(-pid, libc::SIGKILL) };
                }
                let _ = child.kill();
                let _ = child.wait();
                return AuthState::Unauthenticated;
            }
        }
    }
}

fn locate(binary: &str) -> Option<PathBuf> {
    crate::launch::executable(Path::new(binary)).ok()
}

fn provider_state(provider: &Provider) -> AuthState {
    let Some(path) = locate(provider.binary) else { return AuthState::Missing };
    probe(&path, provider.probe, Duration::from_secs(10))
}

fn state_text(state: AuthState) -> &'static str {
    match state {
        AuthState::Missing => "CLI not installed",
        AuthState::Authenticated => "authenticated",
        AuthState::Unauthenticated => "not authenticated",
        AuthState::TimedOut => "auth status timed out",
    }
}

pub fn auth_status(name: Option<&str>) -> io::Result<String> {
    let selected: Vec<&Provider> = if let Some(name) = name {
        vec![PROVIDERS.iter().find(|provider| provider.name == name)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput,
                "usage: doxa auth status [claude|codex]"))?]
    } else { PROVIDERS.iter().collect() };
    Ok(selected.into_iter().map(|provider| format!("{}: {}", provider.label,
        state_text(provider_state(provider)))).collect::<Vec<_>>().join("\n"))
}

/// Only public browser/device authentication is supported; credentials remain
/// in the provider CLI. No raw extra argument can reach that subprocess.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AuthRequest {
    pub provider: &'static str,
    pub action: &'static str,
    pub device_auth: bool,
}

pub fn parse_auth_request(action: &str, arguments: &str) -> io::Result<Option<AuthRequest>> {
    let action = match action { "login" => "login", "logout" => "logout", _ => return Err(io::Error::other("auth action must be login or logout")) };
    let words: Vec<_> = arguments.split_whitespace().collect();
    if words.is_empty() { return Ok(None); }
    let provider = match words[0].to_ascii_lowercase().as_str() {
        "claude" => "claude", "codex" => "codex",
        _ => return Err(io::Error::new(io::ErrorKind::InvalidInput, "choose claude or codex explicitly")),
    };
    let device_auth = match words.as_slice() {
        [_] => false,
        [_, "--device-auth"] if provider == "codex" && action == "login" => true,
        _ => return Err(io::Error::new(io::ErrorKind::InvalidInput, "use login claude|codex [--device-auth (Codex only)] or logout claude|codex")),
    };
    Ok(Some(AuthRequest { provider, action, device_auth }))
}

/// Run the chosen provider's supported browser login/logout command. Only
/// allowlisted public login progress reaches callers, never arbitrary output.
pub fn auth_action(name: &str, action: &str, progress: impl FnMut(String)) -> io::Result<String> {
    auth_action_cancellable(name, action, progress, &std::sync::atomic::AtomicBool::new(false))
}

pub fn auth_action_cancellable(name: &str, action: &str, progress: impl FnMut(String), cancel: &std::sync::atomic::AtomicBool) -> io::Result<String> {
    let request = parse_auth_request(action, name)?.ok_or_else(|| io::Error::other("choose claude or codex explicitly"))?;
    auth_action_request(request, progress, cancel)
}

pub fn auth_action_request(request: AuthRequest, mut progress: impl FnMut(String), cancel: &std::sync::atomic::AtomicBool) -> io::Result<String> {
    let provider = PROVIDERS.iter().find(|p| p.name == request.provider).ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "choose claude or codex explicitly"))?;
    let args: &[&str] = match (request.provider, request.action, request.device_auth) {
        ("claude", "login", false) => &["auth", "login"],
        ("claude", "logout", false) => &["auth", "logout"],
        ("codex", "login", false) => &["login"],
        ("codex", "login", true) => &["login", "--device-auth"],
        ("codex", "logout", false) => &["logout"],
        _ => return Err(io::Error::new(io::ErrorKind::InvalidInput, "unsupported provider authentication option")),
    };
    let binary = locate(provider.binary).ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "provider CLI not installed"))?;
    run_auth_cancellable(&binary, args, Duration::from_secs(900), &mut progress, cancel)?;
    Ok(format!("{} {} completed; {}", provider.label, request.action, state_text(provider_state(provider))))
}

fn secret_auth_parameter(url: &str) -> bool {
    // OAuth URLs commonly percent-encode redirect_uri and scope values.
    // Decode parameter names only, preventing encoded secret keys from
    // bypassing the output filter while retaining usable public login URLs.
    for query in url.split(['?', '#']).skip(1) {
        for pair in query.split('&') {
            let key = pair.split('=').next().unwrap_or_default();
            let mut decoded = Vec::new(); let mut bytes = key.bytes();
            while let Some(byte) = bytes.next() {
                if byte == b'%' {
                    let Some(high) = bytes.next().and_then(|b| (b as char).to_digit(16)) else { return true };
                    let Some(low) = bytes.next().and_then(|b| (b as char).to_digit(16)) else { return true };
                    decoded.push((high * 16 + low) as u8);
                } else { decoded.push(byte); }
            }
            let Ok(key) = String::from_utf8(decoded) else { return true };
            let key = key.to_ascii_lowercase();
            if ["api_key", "code", "password", "passwd", "authorization", "bearer", "jwt", "key"].contains(&key.as_str())
                || ["token", "secret", "credential"].iter().any(|word| key.contains(word)) { return true; }
        }
    }
    false
}

fn strip_auth_ansi(line: &str) -> Option<String> {
    let mut output = String::new(); let mut chars = line.chars().peekable();
    while let Some(character) = chars.next() {
        if character == '\u{1b}' {
            match chars.next()? {
                '[' => {
                    let mut ended = false;
                    for value in chars.by_ref() { if ('@'..='~').contains(&value) { ended = true; break; } }
                    if !ended { return None; }
                }
                ']' => {
                    let mut ended = false;
                    while let Some(value) = chars.next() {
                        if value == '\u{7}' { ended = true; break; }
                        if value == '\u{1b}' { if chars.next()? != '\\' { return None; } ended = true; break; }
                    }
                    if !ended { return None; }
                }
                _ => return None,
            }
        } else if character.is_control() && character != '\t' { return None; }
        else { output.push(character); }
    }
    Some(output)
}

#[derive(Default)]
struct AuthProgress { next_device_code: bool, blank_lines_left: u8 }
impl AuthProgress {
    fn line(&mut self, line: &str) -> Option<String> {
        let clean = match strip_auth_ansi(line) { Some(clean) => clean, None => { self.next_device_code = false; return None; } };
        let code = clean.trim();
        // A PTY's CRLF delimiter can produce one empty line between chunks.
        // Preserve the exact next-line prompt context only across that gap.
        if code.is_empty() && self.next_device_code && self.blank_lines_left > 0 {
            self.blank_lines_left -= 1; return None;
        }
        let expected = std::mem::take(&mut self.next_device_code);
        // Official Codex0.156.1 prints the public code on the line after this
        // exact prompt. Never capture unrelated standalone CLI output.
        if expected && (4..=32).contains(&code.len()) && code.bytes().all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'-') {
            return Some(format!("Device code: {code}"));
        }
        let report = public_auth_progress(&clean);
        self.next_device_code = report.is_none() && clean.to_ascii_lowercase().contains("enter this one-time code");
        self.blank_lines_left = u8::from(self.next_device_code);
        report
    }
}

fn public_auth_progress(line: &str) -> Option<String> {
    let clean = strip_auth_ansi(line)?; let line = clean.as_str();
    for word in line.split_whitespace() {
        let url = word.trim_end_matches(['.', ',', ')']);
        let Some(rest) = url.strip_prefix("https://") else { continue };
        let host = rest.split(['/', '?', '#']).next()?;
        if !["auth.openai.com", "chatgpt.com", "claude.ai", "console.anthropic.com", "platform.openai.com"].contains(&host) { continue; }
        let lower = url.to_ascii_lowercase();
        if url.chars().any(char::is_control) || lower.contains("token") || lower.contains("api_key") || lower.contains("code=") || secret_auth_parameter(url) { continue; }
        return Some(format!("Open in your browser: {url}"));
    }
    let lower = line.to_ascii_lowercase();
    if ["device code", "verification code", "one-time code"].iter().any(|key| lower.contains(key)) {
        if let Some(code) = line.split_whitespace().last().filter(|code| code.len() >= 4 && code.len() <= 32 && code.bytes().all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'-')) {
            return Some(format!("Device code: {code}"));
        }
    }
    None
}

#[cfg(test)]
fn run_auth(binary: &Path, args: &[&str], timeout: Duration, progress: &mut impl FnMut(String)) -> io::Result<()> {
    run_auth_cancellable(binary, args, timeout, progress, &std::sync::atomic::AtomicBool::new(false))
}

fn run_auth_cancellable(binary: &Path, args: &[&str], timeout: Duration, progress: &mut impl FnMut(String), cancel: &std::sync::atomic::AtomicBool) -> io::Result<()> {
    use std::os::fd::FromRawFd;
    if cancel.load(std::sync::atomic::Ordering::Acquire) { return Err(io::Error::new(io::ErrorKind::Interrupted, "provider authentication cancelled")); }
    let (mut master, mut slave) = (-1, -1);
    if unsafe { libc::openpty(&mut master, &mut slave, std::ptr::null_mut(), std::ptr::null(), std::ptr::null()) } != 0 { return Err(io::Error::last_os_error()); }
    let mut reader = unsafe { std::fs::File::from_raw_fd(master) };
    let terminal = unsafe { std::fs::File::from_raw_fd(slave) };
    unsafe {
        libc::fcntl(master, libc::F_SETFL, libc::O_NONBLOCK);
        libc::fcntl(master, libc::F_SETFD, libc::FD_CLOEXEC);
        libc::fcntl(slave, libc::F_SETFD, libc::FD_CLOEXEC);
    }
    let mut child = Command::new(binary).args(args).stdin(terminal.try_clone()?).stdout(terminal.try_clone()?).stderr(terminal).process_group(0).spawn()?;
    let deadline = Instant::now() + timeout;
    let mut pending = String::new();
    let mut reports = AuthProgress::default();
    loop {
        let mut bytes = [0u8; 4096];
        if let Ok(count) = reader.read(&mut bytes) {
            pending.push_str(&String::from_utf8_lossy(&bytes[..count]));
            while let Some(end) = pending.find(['\n', '\r']) {
                let line = pending[..end].to_owned();
                pending.drain(..=end);
                if let Some(value) = reports.line(&line) { progress(value); }
            }
            if pending.len() > 8192 { pending.clear(); }
        }
        if let Some(status) = child.try_wait()? {
            if status.success() { return Ok(()); }
            return Err(io::Error::other("provider authentication command failed; credentials remain owned by its CLI"));
        }
        if Instant::now() >= deadline || cancel.load(std::sync::atomic::Ordering::Acquire) {
            unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL); }
            let _ = child.wait();
            return Err(if cancel.load(std::sync::atomic::Ordering::Acquire) { io::Error::new(io::ErrorKind::Interrupted, "provider authentication cancelled") }
                else { io::Error::new(io::ErrorKind::TimedOut, "provider authentication timed out") });
        }
        thread::sleep(Duration::from_millis(25));
    }
}

pub fn setup_choose_store(shared: bool) -> io::Result<String> {
    if std::env::var("LORE_ROOT").ok().is_some_and(|s| !s.trim().is_empty()) { return Err(io::Error::new(io::ErrorKind::PermissionDenied, "LORE_ROOT overrides the stored choice")); }
    let home = doxa_home()?;
    let path = if shared {
        let path = PathBuf::from(std::env::var_os("HOME").ok_or_else(|| io::Error::other("HOME unset"))?).join(".claude/lore");
        if !path.is_dir() { return Err(io::Error::new(io::ErrorKind::NotFound, "Claude LORE store not found")); }
        path
    } else { home.join("lore") };
    if std::fs::symlink_metadata(&path).is_ok_and(|m| m.file_type().is_symlink()) { return Err(io::Error::other("LORE store must not be a symlink")); }
    if !shared {
        let directory = create_private_store(&path)?;
        use std::os::unix::fs::PermissionsExt;
        directory.set_permissions(std::fs::Permissions::from_mode(0o700))?;
        let opened = directory.metadata()?;
        let current = std::fs::symlink_metadata(&path)?;
        if (opened.dev(), opened.ino()) != (current.dev(), current.ino()) {
            return Err(io::Error::other("DOXA LORE store changed during setup"));
        }
    }
    doxa_state::update_config(&home.join("config.toml"), |config| { config.insert("lore_root".into(), toml::Value::String(path.display().to_string())); Ok(()) })?;
    Ok(format!("LORE store selected: {}. New sessions use this store.", safe_report_value(&path.display().to_string())))
}

/// Anchor each component to its opened parent, creating private directories.
/// A symlink in an existing ancestor is refused before any descendant changes.
fn create_private_store(path: &Path) -> io::Result<std::fs::File> {
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    if !path.is_absolute() { return Err(io::Error::other("DOXA LORE store must be absolute")); }
    let mut directory = std::fs::OpenOptions::new().read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW).open("/")?;
    for component in path.components() {
        let name = match component {
            std::path::Component::RootDir => continue,
            std::path::Component::Normal(name) => std::ffi::CString::new(name.as_bytes())
                .map_err(|_| io::Error::other("invalid LORE directory name"))?,
            _ => return Err(io::Error::other("LORE store path must not contain relative components")),
        };
        let flags = libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC;
        let mut fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), flags) };
        if fd < 0 && io::Error::last_os_error().kind() == io::ErrorKind::NotFound {
            if unsafe { libc::mkdirat(directory.as_raw_fd(), name.as_ptr(), 0o700) } != 0
                && io::Error::last_os_error().kind() != io::ErrorKind::AlreadyExists {
                return Err(io::Error::last_os_error());
            }
            fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), flags) };
        }
        if fd < 0 { return Err(io::Error::last_os_error()); }
        directory = unsafe { std::fs::File::from_raw_fd(fd) };
    }
    if directory.metadata()?.uid() != unsafe { libc::geteuid() } {
        return Err(io::Error::other("DOXA LORE store must be owned by this user"));
    }
    Ok(directory)
}

pub fn setup_default(key: &str, value: Option<&str>) -> io::Result<String> {
    let env = match key { "model" => "DOXA_MODEL", "effort" => "DOXA_EFFORT", _ => return Err(io::Error::other("default must be model or effort")) };
    if std::env::var(env).ok().is_some_and(|s| !s.trim().is_empty()) { return Err(io::Error::other(format!("{env} overrides this preference"))); }
    if value.is_some_and(|v| v.is_empty() || v.len() > 200 || v.chars().any(char::is_control)) { return Err(io::Error::other("enter a nonempty default without control characters")); }
    doxa_state::update_config(&doxa_home()?.join("config.toml"), |config| { if let Some(value) = value { config.insert(key.into(), toml::Value::String(value.into())); } else { config.remove(key); } Ok(()) })?;
    Ok(format!("{key} default updated for new sessions"))
}

pub(crate) fn doxa_home() -> io::Result<PathBuf> {
    std::env::var_os("DOXA_HOME").filter(|value| !value.is_empty()).map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|value| PathBuf::from(value).join(".doxa")))
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "DOXA_HOME and HOME are unset"))
}

fn safe_report_value(value: &str) -> String {
    value.chars().filter(|c| !c.is_control()
        && !matches!(*c, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}'))
        .take(200).collect()
}

fn preference(config: &toml::Table, key: &str, env: &str) -> String {
    if let Some(value) = std::env::var(env).ok().filter(|value| !value.trim().is_empty()) {
        return format!("{} (environment)", safe_report_value(&value));
    }
    if let Some(value) = config.get(key).and_then(toml::Value::as_str).filter(|value| !value.is_empty()) {
        return format!("{} (config.toml)", safe_report_value(value));
    }
    "(CLI default)".to_owned()
}

pub fn setup_report() -> io::Result<String> {
    let home = doxa_home()?;
    let config = doxa_state::load_config(&home.join("config.toml"));
    let lore = if let Some(root) = std::env::var("LORE_ROOT").ok().filter(|root| !root.trim().is_empty()) {
        format!("{} (environment)", safe_report_value(&root))
    } else if let Some(root) = config.get("lore_root").and_then(toml::Value::as_str).filter(|root| !root.is_empty()) {
        format!("{} (config.toml)", safe_report_value(root))
    } else {
        let plugin = std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".claude/lore"));
        if let Some(path) = plugin.filter(|path| path.is_dir()) {
            format!("unselected; existing Claude LORE store at {}", safe_report_value(&path.display().to_string()))
        } else {
            format!("unselected; suggested DOXA store at {}", safe_report_value(&home.join("lore").display().to_string()))
        }
    };
    Ok(format!(
        "auth state\n{}\n\nSign in with the provider CLI: claude auth login or codex login. DOXA never asks for credentials.\n\nLORE store\n{lore}\n\nmodel & effort defaults (stored preferences)\nmodel: {}\neffort: {}\n\nUse `doxa settings` for native linger and worktree preferences; the Python UI manages its wider settings catalog.",
        auth_status(None)?, preference(&config, "model", "DOXA_MODEL"),
        preference(&config, "effort", "DOXA_EFFORT"),
    ))
}

fn setting_env(key: &str) -> Option<&'static str> {
    match key {
        "linger_secs" => Some("DOXA_LINGER_SECS"),
        "worktree_per_session" => Some("DOXA_WORKTREE"),
        _ => None,
    }
}

fn effective_setting(config: &toml::Table, key: &str) -> String {
    let env = setting_env(key).expect("validated setting");
    let override_value = std::env::var(env).ok().filter(|value| !value.trim().is_empty());
    let (source, value) = if let Some(value) = override_value {
        ("environment", value)
    } else if config.contains_key(key) {
        let value = if key == "worktree_per_session" {
            match config.get(key) {
                Some(toml::Value::Boolean(true)) => "on".into(),
                Some(toml::Value::Boolean(false)) => "off".into(),
                Some(toml::Value::String(value)) if !value.trim().is_empty() => value.clone(),
                _ => "on".into(),
            }
        } else { doxa_state::raw_setting(None, config, key) };
        ("config.toml", value)
    } else {
        ("default", if key == "linger_secs" { "120".into() } else { "1".into() })
    };
    let display = if key == "worktree_per_session" {
        if matches!(value.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off") { "off" }
        else { "on" }
    } else { value.trim() };
    format!("{} ({source})", safe_report_value(display))
}

/// Values displayed by the in-app editor. Re-read on every open and after
/// every write so the menu never presents stale config as the active value.
pub fn native_settings() -> io::Result<[(String, bool); 2]> {
    let config = doxa_state::load_config_checked(&doxa_home()?.join("config.toml"))?;
    Ok(["linger_secs", "worktree_per_session"].map(|key| {
        let env = setting_env(key).expect("native setting");
        (effective_setting(&config, key),
            std::env::var(env).ok().is_some_and(|value| !value.trim().is_empty()))
    }))
}

pub fn settings_report() -> io::Result<String> {
    let config = doxa_state::load_config_checked(&doxa_home()?.join("config.toml"))?;
    Ok(format!("native settings · environment > config.toml > default (launch flags can override)\nlinger_secs: {}\nworktree_per_session: {}\n\nChange with `doxa settings set KEY VALUE`; remove with `doxa settings unset KEY`. These affect new sessions; running sessions keep their launch settings.",
        effective_setting(&config, "linger_secs"),
        effective_setting(&config, "worktree_per_session")))
}

fn edit_setting(path: &Path, key: &str, value: Option<&str>, env_override: Option<&str>) -> io::Result<()> {
    let env = setting_env(key).ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput,
        "setting must be linger_secs or worktree_per_session"))?;
    if env_override.is_some_and(|value| !value.trim().is_empty()) {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied,
            format!("{env} overrides config.toml; unset it before changing {key}")));
    }
    let parsed = match (key, value) {
        (_, None) => None,
        ("linger_secs", Some(value)) => {
            let seconds: f64 = value.parse().map_err(|_| io::Error::new(io::ErrorKind::InvalidInput,
                "linger_secs must be a nonnegative finite number"))?;
            if !seconds.is_finite() || !(0.0..=crate::launch::MAX_LINGER_SECS).contains(&seconds) {
                return Err(io::Error::new(io::ErrorKind::InvalidInput,
                    "linger_secs must be a finite number between 0 and 31536000 seconds"));
            }
            Some(toml::Value::Float(seconds))
        }
        ("worktree_per_session", Some("on" | "true" | "1")) => Some(toml::Value::Boolean(true)),
        ("worktree_per_session", Some("off" | "false" | "0")) => Some(toml::Value::Boolean(false)),
        _ => return Err(io::Error::new(io::ErrorKind::InvalidInput,
            "worktree_per_session accepts on or off")),
    };
    doxa_state::update_config(path, |stored| {
        if let Some(value) = parsed { stored.insert(key.into(), value); }
        else { stored.remove(key); }
        Ok(())
    })
}

pub fn settings_change(key: &str, value: Option<&str>) -> io::Result<String> {
    let env = setting_env(key).ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput,
        "setting must be linger_secs or worktree_per_session"))?;
    edit_setting(&doxa_home()?.join("config.toml"), key, value,
        std::env::var(env).ok().as_deref())?;
    Ok(format!("{key}: {} for new sessions",
        if value.is_some() { "stored" } else { "removed from config.toml" }))
}

fn read_claude_json(path: &Path) -> Option<serde_json::Value> {
    // Plugin registries are external state. Do not follow links, inspect
    // special files, or read unbounded content from them.
    let file = std::fs::OpenOptions::new().read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path).ok()?;
    let metadata = file.metadata().ok()?;
    if !metadata.is_file() || metadata.nlink() != 1 || metadata.len() > 1024 * 1024 {
        return None;
    }
    let mut data = Vec::new();
    file.take(1024 * 1024 + 1).read_to_end(&mut data).ok()?;
    if data.len() > 1024 * 1024 { return None; }
    serde_json::from_slice(&data).ok()
}

fn safe_plugin_name(name: &str) -> bool {
    !name.is_empty() && name.len() <= 128 && name != ".."
        && name.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"._-@".contains(&byte))
}

#[cfg(test)]
fn plugin_rows(installed: &serde_json::Value, settings: &serde_json::Value) -> Vec<String> {
    let enabled = settings.get("enabledPlugins").and_then(serde_json::Value::as_object);
    let mut rows = installed.get("plugins").and_then(serde_json::Value::as_object)
        .into_iter().flat_map(|plugins| plugins.iter())
        .filter(|(name, entries)| safe_plugin_name(name)
            && entries.as_array().is_some_and(|entries| !entries.is_empty()))
        .map(|(name, _)| {
            let state = if enabled.and_then(|settings| settings.get(name)) == Some(&serde_json::Value::Bool(true)) {
                "enabled"
            } else { "disabled" };
            format!("{name}: {state} in Claude Code")
        }).collect::<Vec<_>>();
    rows.sort();
    rows
}

pub fn plugins_change(value: bool) -> io::Result<String> {
    if std::env::var("DOXA_ADOPT_PLUGINS").ok().is_some_and(|s| !s.trim().is_empty()) { return Err(io::Error::new(io::ErrorKind::PermissionDenied, "DOXA_ADOPT_PLUGINS overrides config.toml")); }
    doxa_state::update_config(&doxa_home()?.join("config.toml"), |config| { config.insert("adopt_plugins".into(), toml::Value::Boolean(value)); Ok(()) })?;
    Ok(format!("Claude plugin adoption {} for new sessions; hooks, MCP servers and lore@lore remain refused", if value { "enabled" } else { "disabled" }))
}

fn adoption_enabled() -> io::Result<bool> {
    let config = doxa_state::load_config_checked(&doxa_home()?.join("config.toml"))?;
    let value = std::env::var("DOXA_ADOPT_PLUGINS").ok().filter(|v| !v.trim().is_empty());
    Ok(if let Some(value) = value { !matches!(value.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off") } else { match config.get("adopt_plugins") { Some(toml::Value::Boolean(v)) => *v, Some(toml::Value::String(v)) => !v.trim().is_empty() && !matches!(v.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off"), _ => false } })
}

fn content_count(path: &Path, skills: bool) -> usize {
    std::fs::read_dir(path).into_iter().flatten().filter_map(Result::ok).take(10000).filter(|entry| {
        entry.file_type().is_ok_and(|t| if skills { t.is_dir() && entry.path().join("SKILL.md").is_file() } else { t.is_file() && entry.path().extension().is_some_and(|s| s == "md") })
    }).count()
}

fn detailed_plugin_rows(installed: &serde_json::Value, settings: &serde_json::Value, on: bool) -> Vec<String> {
    let enabled = settings.get("enabledPlugins").and_then(serde_json::Value::as_object);
    let mut rows = Vec::new();
    for (name, entries) in installed.get("plugins").and_then(serde_json::Value::as_object).into_iter().flat_map(|m| m.iter()) {
        if !safe_plugin_name(name) || name.contains("..") { continue; }
        let Some(entry) = entries.as_array().and_then(|v| v.first()) else { continue };
        let Some(path) = entry.get("installPath").and_then(serde_json::Value::as_str).filter(|v| !v.is_empty()).map(PathBuf::from) else { continue };
        let counts = [content_count(&path.join("commands"), false), content_count(&path.join("skills"), true), content_count(&path.join("agents"), false)];
        let refusal = if name == "lore@lore" { Some("LORE already runs inside DOXA; duplicate carrier blocked") } else if enabled.and_then(|m| m.get(name)) != Some(&serde_json::Value::Bool(true)) { Some("disabled in Claude Code") } else if counts.iter().sum::<usize>() == 0 { Some("no commands, skills or agents") } else { None };
        let status = refusal.unwrap_or(if on { "adopted by new Claude sessions" } else { "would adopt when enabled" });
        rows.push(format!("{name}: {status}\n  {} commands · {} skills · {} agents; hooks and MCP always excluded", counts[0], counts[1], counts[2]));
        if refusal.is_none() {
            let plugin = name.split('@').next().unwrap_or(name);
            let mut commands = std::fs::read_dir(path.join("commands")).into_iter().flatten().filter_map(Result::ok).filter(|e| e.file_type().is_ok_and(|t| t.is_file())).filter_map(|e| { let p = e.path(); (p.extension().is_some_and(|s| s == "md")).then(|| p.file_stem().and_then(|s| s.to_str()).map(str::to_owned)).flatten() }).filter(|s| safe_plugin_name(s)).collect::<Vec<_>>();
            commands.sort();
            for command in commands { rows.push(format!("  /{plugin}:{command}")); }
        }
    }
    rows.sort();
    rows
}

/// Run the installed canonical plugin policy without creating an SDK session.
pub fn plugins_reload() -> io::Result<String> {
    plugins_bridge(true)
}

pub fn plugins_bridge(reload: bool) -> io::Result<String> {
    let (python, script) = crate::launch::claude_dependencies(&Default::default())?;
    let mut child = Command::new(python).arg("-I").arg(script)
        .arg(if reload { "--reload-plugins" } else { "--plugins-report" })
        .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null())
        .process_group(0).spawn()?;
    let stdout = child.stdout.take().unwrap();
    let reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout.take(65537).read_to_end(&mut bytes).map(|_| bytes)
    });
    let deadline = Instant::now() + Duration::from_secs(30);
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Ok(Some(status)),
            Ok(None) => {},
            Err(error) => break Err(error),
        }
        if Instant::now() >= deadline {
            unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL); }
            let _ = child.wait(); break Ok(None);
        }
        thread::sleep(Duration::from_millis(20));
    };
    // Kill descendants before joining a reader: a leftover inherited stdout
    // must not hold this operation open after its leader exits.
    unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL); }
    let _ = child.wait();
    let bytes = reader.join().map_err(|_| io::Error::other("Plugin inventory reader failed"))??;
    let status = status?;
    if status.is_none() { return Err(io::Error::new(io::ErrorKind::TimedOut, "Plugin inventory timed out")); }
    if !status.unwrap().success() || bytes.len() > 65536 { return Err(io::Error::other("Plugin inventory failed; verify installed Python/LORE dependencies")); }
    String::from_utf8(bytes).map_err(|_| io::Error::other("Invalid plugin inventory response"))
}

pub fn plugins_report() -> io::Result<String> {
    let base = std::env::var_os("CLAUDE_CONFIG_DIR").filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|value| PathBuf::from(value).join(".claude")))
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "CLAUDE_CONFIG_DIR and HOME are unset"))?;
    let installed = read_claude_json(&base.join("plugins/installed_plugins.json"))
        .unwrap_or(serde_json::Value::Null);
    let settings = read_claude_json(&base.join("settings.json"))
        .unwrap_or(serde_json::Value::Null);
    let on = adoption_enabled()?;
    let rows = detailed_plugin_rows(&installed, &settings, on);
    if rows.is_empty() { Ok(format!("Claude plugin adoption: {} · no Claude Code plugins found", if on { "ON" } else { "OFF" })) }
    else { Ok(format!("Claude plugin adoption: {} · refreshed from CLI registry\n{}\n\nChanges apply to new sessions; the sidecar rebuilds sanitized copies at launch.", if on { "ON" } else { "OFF" }, rows.join("\n"))) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn probe_uses_exit_status_and_never_exposes_output() {
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("probe");
        fs::write(&script, "#!/bin/sh\nprintf 'secret credential\\n'\nprintf 'secret error\\n' >&2\n[ \"$1\" = ok ]\n").unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
        // CI may mount its temporary directory noexec. The probe contract is
        // exit status and output isolation, not executing a temporary file.
        let script = script.to_str().unwrap();
        assert_eq!(probe(Path::new("/bin/sh"), &[script, "ok"], Duration::from_secs(5)), AuthState::Authenticated);
        assert_eq!(probe(Path::new("/bin/sh"), &[script, "fail"], Duration::from_secs(5)), AuthState::Unauthenticated);
    }

    #[test]
    fn probe_times_out_and_reaps_child() {
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("probe");
        fs::write(&script, "#!/bin/sh\nsleep 5\n").unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
        let start = Instant::now();
        assert_eq!(probe(Path::new("/bin/sh"), &[script.to_str().unwrap()], Duration::from_millis(10)), AuthState::TimedOut);
        assert!(start.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn explicit_auth_request_accepts_only_browser_and_codex_device_forms() {
        assert_eq!(parse_auth_request("login", "CODEX --device-auth").unwrap(), Some(AuthRequest { provider:"codex", action:"login", device_auth:true }));
        assert_eq!(parse_auth_request("logout", "claude").unwrap(), Some(AuthRequest { provider:"claude", action:"logout", device_auth:false }));
        assert_eq!(parse_auth_request("login", "").unwrap(), None);
        for (action, args) in [("login", "unknown"), ("login", "claude --device-auth"), ("logout", "codex --device-auth"), ("login", "codex --with-access-token"), ("login", "codex --device-auth extra")] {
            assert!(parse_auth_request(action, args).is_err());
        }
    }
    #[test]
    fn codex_official_device_prompt_reveals_only_public_url_and_labeled_next_code() {
        let mut report = AuthProgress::default();
        assert_eq!(report.line("   \u{1b}[34mhttps://auth.openai.com/codex/device\u{1b}[0m"), Some("Open in your browser: https://auth.openai.com/codex/device".into()));
        assert_eq!(report.line("2. Enter this one-time code \u{1b}[90m(expires in 15 minutes)\u{1b}[0m"), None);
        assert_eq!(report.line(""), None);
        assert_eq!(report.line("   \u{1b}[34mABCD-EFGH\u{1b}[0m"), Some("Device code: ABCD-EFGH".into()));
        assert_eq!(report.line("UNRELATED-PRIVATE"), None);
        assert_eq!(report.line("\u{1b}[34mhttps://auth.openai.com/login?client_secret=PRIVATE\u{1b}[0m"), None);
    }
    #[test]
    fn auth_progress_rejects_secrets_and_untrusted_urls() {
        assert_eq!(public_auth_progress("secret credential"), None);
        assert_eq!(public_auth_progress("https://evil.test/login"), None);
        assert_eq!(public_auth_progress("https://auth.openai.com/login?access_token=SECRET"), None);
        assert_eq!(public_auth_progress("https://auth.openai.com/login?%63ode=SECRET"), None);
        assert_eq!(public_auth_progress("https://auth.openai.com/login?client_secret=PRIVATE"), None);
        assert_eq!(public_auth_progress("https://auth.openai.com/login?%63lient_secret=PRIVATE"), None);
        assert_eq!(public_auth_progress("https://auth.openai.com/login?password=PRIVATE"), None);
        assert_eq!(public_auth_progress("https://auth.openai.com/login?state=public"), Some("Open in your browser: https://auth.openai.com/login?state=public".into()));
        assert_eq!(public_auth_progress("https://claude.ai/login?redirect_uri=http%3A%2F%2Flocalhost&scope=user%3Ainference"), Some("Open in your browser: https://claude.ai/login?redirect_uri=http%3A%2F%2Flocalhost&scope=user%3Ainference".into()));
        assert_eq!(public_auth_progress("Device code: ABCD-EFGH"), Some("Device code: ABCD-EFGH".into()));
    }

    #[test]
    fn auth_private_pty_filters_output_and_times_out() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("auth.sh");
        fs::write(&path, "printf 'secret credential\\nhttps://claude.ai/login\\n'\nsleep 0.1\n").unwrap();
        let mut messages = Vec::new();
        run_auth(Path::new("/bin/sh"), &[path.to_str().unwrap()], Duration::from_secs(2), &mut |s| messages.push(s)).unwrap();
        assert_eq!(messages, vec!["Open in your browser: https://claude.ai/login"]);
        fs::write(&path, "sleep 5\n").unwrap();
        let cancel = std::sync::atomic::AtomicBool::new(true);
        assert_eq!(run_auth_cancellable(Path::new("/bin/sh"), &[path.to_str().unwrap()], Duration::from_secs(30), &mut |_| {}, &cancel).unwrap_err().kind(), io::ErrorKind::Interrupted);
        assert_eq!(run_auth(Path::new("/bin/sh"), &[path.to_str().unwrap()], Duration::from_millis(20), &mut |_| {}).unwrap_err().kind(), io::ErrorKind::TimedOut);
    }

    #[test]
    fn plugin_preview_keeps_lore_blocked_and_disabled_plugins_refused() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir(dir.path().join("commands")).unwrap();
        fs::write(dir.path().join("commands/run.md"), "example").unwrap();
        let installed = serde_json::json!({"plugins": {
            "safe@market": [{"installPath": dir.path()}],
            "lore@lore": [{"installPath": dir.path()}],
            "disabled@market": [{"installPath": dir.path()}],
            "unsafe..@market": [{"installPath": dir.path()}]
        }});
        let settings = serde_json::json!({"enabledPlugins": {"safe@market": true, "lore@lore": true}});
        let rows = detailed_plugin_rows(&installed, &settings, true).join("\n");
        assert!(rows.contains("safe@market: adopted by new Claude sessions"));
        assert!(rows.contains("/safe:run"));
        assert!(rows.contains("duplicate carrier blocked"));
        assert!(rows.contains("disabled in Claude Code"));
        assert!(!rows.contains("/lore:run"));
        assert!(!rows.contains("unsafe.."));
    }

    #[test]
    fn preference_shows_precedence() {
        let mut config = toml::Table::new();
        config.insert("model".into(), toml::Value::String("stored".into()));
        assert_eq!(preference(&config, "model", "DOXA_TEST_MISSING_PREFERENCE"), "stored (config.toml)");
        assert_eq!(preference(&config, "missing", "DOXA_TEST_MISSING_PREFERENCE"), "(CLI default)");
    }

    #[test]
    fn native_settings_store_only_valid_active_preferences() {
        let dir = tempfile::tempdir().unwrap();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let path = dir.path().join("config.toml");
        fs::write(&path, "future = 'keep'\n").unwrap();
        edit_setting(&path, "linger_secs", Some("42.5"), None).unwrap();
        edit_setting(&path, "worktree_per_session", Some("off"), None).unwrap();
        let config = doxa_state::load_config_checked(&path).unwrap();
        assert_eq!(config["linger_secs"].as_float(), Some(42.5));
        assert_eq!(config["worktree_per_session"].as_bool(), Some(false));
        assert_eq!(config["future"].as_str(), Some("keep"));
        assert_eq!(effective_setting(&config, "worktree_per_session"), "off (config.toml)");
        let mut malformed = config.clone();
        malformed.insert("worktree_per_session".into(), toml::Value::Integer(0));
        assert_eq!(effective_setting(&malformed, "worktree_per_session"), "on (config.toml)");
        assert!(edit_setting(&path, "linger_secs", Some("NaN"), None).is_err());
        assert!(edit_setting(&path, "linger_secs", Some("1e308"), None).is_err());
        assert!(edit_setting(&path, "worktree_per_session", Some("maybe"), None).is_err());
        assert!(edit_setting(&path, "linger_secs", Some("10"), Some("7")).is_err());
        assert_eq!(doxa_state::load_config_checked(&path).unwrap(), config);
        edit_setting(&path, "linger_secs", None, None).unwrap();
        assert!(doxa_state::load_config_checked(&path).unwrap().get("linger_secs").is_none());
    }

    #[test]
    fn report_values_cannot_inject_terminal_control_sequences() {
        assert_eq!(safe_report_value("model\u{1b}[31m\nnext\u{202e}"), "model[31mnext");
    }

    #[test]
    fn plugin_inventory_uses_registry_names_without_paths_or_metadata() {
        let installed = serde_json::json!({"plugins": {
            "safe@market": [{"installPath": "/private/secret"}],
            "../unsafe": [{}],
            "empty@market": []
        }});
        let settings = serde_json::json!({"enabledPlugins": {"safe@market": true}});
        assert_eq!(plugin_rows(&installed, &settings), vec!["safe@market: enabled in Claude Code"]);
    }
}

pub fn setup_interactive() -> io::Result<()> {
    use std::io::{IsTerminal, Write};
    println!("{}", setup_report()?);
    if !io::stdin().is_terminal() { return Ok(()); }
    fn ask(question: &str) -> io::Result<String> {
        print!("{question} "); io::stdout().flush()?;
        let mut answer = String::new(); io::stdin().read_line(&mut answer)?;
        Ok(answer.trim().to_owned())
    }
    match ask("LORE store: [d] create DOXA store, [s] share existing Claude store, Enter skip:")?.as_str() {
        "d" => println!("{}", setup_choose_store(false)?),
        "s" => println!("{}", setup_choose_store(true)?),
        _ => {}
    }
    for key in ["model", "effort"] {
        let value = ask(&format!("{key} default (Enter keeps current):"))?;
        if !value.is_empty() { println!("{}", setup_default(key, Some(&value))?); }
    }
    println!("setup complete\n{}", setup_report()?);
    Ok(())
}
