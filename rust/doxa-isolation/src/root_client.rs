//! Caller-side transport through the independently authenticated ROOT broker.
//! Installation receipts pin code and scope, never admission. This module
//! executes only the root-installed isolated Python client; it has no Docker
//! or original-program fallback. The client attests the live ROOT peer again.
use crate::{error, hardened_authority::Policy, Manifest, ProviderSnapshot};
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    ffi::{CString, OsStr, OsString},
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{
            ffi::OsStrExt,
            fs::{FileTypeExt, MetadataExt, OpenOptionsExt},
        },
    },
    path::{Component, Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

const PROTOCOL: &str = "doxa-root-session-broker-v1";
const MAX_POLICY: u64 = 1024 * 1024;
const MAX_REPLY: u64 = 65536;
const MAX_SNAPSHOT: u64 = 32 * 1024 * 1024;
const MAX_HOME_BYTES: u64 = 64 * 1024 * 1024;
const MAX_HOME_FILES: usize = 20000;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    contract: String,
    session_id: String,
    caller_uid: u32,
    broker_uid: u32,
    broker_socket: PathBuf,
    policy_path: PathBuf,
    policy_sha256: String,
    broker_script: PathBuf,
    broker_sha256: String,
    broker_exe: PathBuf,
    broker_exe_sha256: String,
    boot_id: String,
}

#[derive(Debug)]
pub struct Installed {
    pub policy: Policy,
    pub policy_sha256: String,
    receipt: Receipt,
    _pins: Vec<File>,
}

fn session_id(id: &str) -> io::Result<()> {
    if id.is_empty()
        || id.len() > 64
        || !id.as_bytes()[0].is_ascii_alphanumeric()
        || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
    {
        return Err(error("invalid ROOT client session identity"));
    }
    Ok(())
}
fn digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn absolute(path: &Path) -> bool {
    let bytes = path.as_os_str().as_bytes();
    bytes.starts_with(b"/")
        && bytes.len() > 1
        && bytes.len() <= 4096
        && !bytes.contains(&0)
        && bytes[1..]
            .split(|b| *b == b'/')
            .all(|part| !part.is_empty() && part != b"." && part != b"..")
}
fn boot_id(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                b == b'-'
            } else {
                b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
            }
        })
}
fn identity(meta: &fs::Metadata) -> (u64, u64, u64, i64, i64, i64, i64) {
    (
        meta.dev(),
        meta.ino(),
        meta.len(),
        meta.ctime(),
        meta.ctime_nsec(),
        meta.mtime(),
        meta.mtime_nsec(),
    )
}

/// Walk every component with no-follow descriptors. A root ancestor writable
/// by an ordinary caller would let that caller swap the trusted executable.
pub(crate) fn root_file(
    path: &Path,
    readonly: bool,
    executable: bool,
    max: u64,
) -> io::Result<File> {
    if !absolute(path) {
        return Err(error("ROOT artifact path must be canonical and absolute"));
    }
    let parts: Vec<_> = path.components().skip(1).collect();
    let mut parent = File::open("/")?;
    for (index, part) in parts.iter().enumerate() {
        let meta = parent.metadata()?;
        if !meta.is_dir() || meta.uid() != 0 || meta.mode() & 0o022 != 0 {
            return Err(error(
                "ROOT artifact ancestor is not administrator controlled",
            ));
        }
        let Component::Normal(name) = part else {
            return Err(error("unsafe ROOT artifact component"));
        };
        let name = CString::new(name.as_bytes()).map_err(|_| error("unsafe ROOT artifact name"))?;
        let last = index + 1 == parts.len();
        let flags = libc::O_RDONLY
            | libc::O_NOFOLLOW
            | libc::O_CLOEXEC
            | libc::O_NONBLOCK
            | if last { 0 } else { libc::O_DIRECTORY };
        let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), flags) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        let file = unsafe { File::from_raw_fd(fd) };
        if last {
            let meta = file.metadata()?;
            if !meta.is_file()
                || meta.uid() != 0
                || meta.nlink() != 1
                || meta.mode() & 0o022 != 0
                || meta.mode() & 0o7000 != 0
                || meta.len() == 0
                || meta.len() > max
                || readonly && meta.mode() & 0o777 != 0o444
                || executable && meta.mode() & 0o111 == 0
            {
                return Err(error(
                    "ROOT artifact ownership, type, mode, links or size is unsafe",
                ));
            }
            return Ok(file);
        }
        parent = file;
    }
    Err(error("ROOT artifact file is missing"))
}

fn read_pin(
    file: &File,
    path: &Path,
    readonly: bool,
    executable: bool,
    max: u64,
) -> io::Result<Vec<u8>> {
    let before = file.metadata()?;
    let mut bytes = Vec::new();
    file.try_clone()?.take(max + 1).read_to_end(&mut bytes)?;
    if bytes.len() > max as usize
        || identity(&before) != identity(&file.metadata()?)
        || identity(&before) != identity(&root_file(path, readonly, executable, max)?.metadata()?)
    {
        return Err(error("ROOT artifact changed while it was pinned"));
    }
    Ok(bytes)
}

/// Read only the fixed root-installed receipt, public policy and code pins.
/// No owner environment variable or arbitrary receipt pathname can redirect it.
pub fn installed(id: &str) -> io::Result<Installed> {
    session_id(id)?;
    let directory = Path::new("/run/doxa/hardened").join(id);
    let receipt_path = directory.join("broker-receipt.json");
    let receipt_file = root_file(&receipt_path, true, false, MAX_REPLY).map_err(|e|
        if e.kind()==io::ErrorKind::NotFound {error(format!("docker-hardened requires administrator provisioning for the reserved session {id}"))}else{e})?;
    let raw = read_pin(&receipt_file, &receipt_path, true, false, MAX_REPLY)?;
    let receipt: Receipt =
        serde_json::from_slice(&raw).map_err(|_| error("ROOT client receipt is malformed"))?;
    let current_boot = fs::read_to_string("/proc/sys/kernel/random/boot_id")?;
    if receipt.contract != PROTOCOL
        || receipt.session_id != id
        || receipt.broker_uid != 0
        || receipt.caller_uid == 0
        || receipt.caller_uid != unsafe { libc::geteuid() }
        || receipt.broker_socket != directory.join("broker.sock")
        || receipt.boot_id != current_boot.trim()
        || !boot_id(&receipt.boot_id)
        || !digest(&receipt.policy_sha256)
        || !digest(&receipt.broker_sha256)
        || !digest(&receipt.broker_exe_sha256)
    {
        return Err(error(
            "ROOT client receipt scope, caller or kernel boot differs",
        ));
    }
    let policy_file = root_file(&receipt.policy_path, true, false, MAX_POLICY)?;
    let policy_bytes = read_pin(&policy_file, &receipt.policy_path, true, false, MAX_POLICY)?;
    if format!("{:x}", Sha256::digest(&policy_bytes)) != receipt.policy_sha256 {
        return Err(error(
            "ROOT public policy differs from its installed content pin",
        ));
    }
    let policy: Policy = serde_json::from_slice(&policy_bytes)
        .map_err(|_| error("ROOT public policy is malformed"))?;
    policy.validate()?;
    if policy.session_id != id
        || policy.caller_uid != receipt.caller_uid
        || policy.broker.path != receipt.broker_script
        || policy.broker.sha256 != receipt.broker_sha256
    {
        return Err(error("ROOT public policy and client code scope differ"));
    }
    let script = root_file(&receipt.broker_script, true, false, 4 * 1024 * 1024)?;
    let script_bytes = read_pin(
        &script,
        &receipt.broker_script,
        true,
        false,
        4 * 1024 * 1024,
    )?;
    let python = root_file(&receipt.broker_exe, false, true, 32 * 1024 * 1024)?;
    let python_bytes = read_pin(&python, &receipt.broker_exe, false, true, 32 * 1024 * 1024)?;
    if format!("{:x}", Sha256::digest(&script_bytes)) != receipt.broker_sha256
        || format!("{:x}", Sha256::digest(&python_bytes)) != receipt.broker_exe_sha256
    {
        return Err(error(
            "ROOT client script or interpreter content pin changed",
        ));
    }
    Ok(Installed {
        policy,
        policy_sha256: receipt.policy_sha256.clone(),
        receipt,
        _pins: vec![receipt_file, policy_file, script, python],
    })
}

impl Installed {
    /// Build before configuring pipes/process groups. The isolated Python
    /// client rechecks receipt/code and authenticates the current ROOT peer.
    pub fn command(&self, operation: &str) -> io::Result<Command> {
        if !matches!(
            operation,
            "describe"
                | "prepare"
                | "resume"
                | "stop"
                | "provider_exec"
                | "workspace_exec"
                | "provider_snapshot"
                | "verify_provider_snapshot"
                | "register_hook"
                | "unregister_hook"
                | "guard"
        ) {
            return Err(error("unsupported ROOT client operation"));
        }
        let mut command = Command::new(&self.receipt.broker_exe);
        command
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .arg("-I")
            .arg(&self.receipt.broker_script)
            .args([
                "client",
                "--session-id",
                &self.policy.session_id,
                "--op",
                operation,
            ]);
        Ok(command)
    }
}

fn json_call(mut command: Command, seconds: u64) -> io::Result<Value> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn()?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| error("ROOT client stdout unavailable"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| error("ROOT client stderr unavailable"))?;
    let out = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout
            .take(MAX_REPLY + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let err = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr
            .take(MAX_REPLY + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let deadline = Instant::now() + Duration::from_secs(seconds.saturating_add(3).min(123));
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break Some(status);
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            break None;
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    let bytes = out
        .join()
        .map_err(|_| error("ROOT client output reader failed"))??;
    let errors = err
        .join()
        .map_err(|_| error("ROOT client error reader failed"))??;
    if !status.is_some_and(|status| status.success())
        || bytes.len() > MAX_REPLY as usize
        || errors.len() > MAX_REPLY as usize
    {
        // Do not include argv, environment values, credential paths or raw
        // provider/control output in errors surfaced by caller diagnostics.
        return Err(error(
            "ROOT client operation refused, exceeded its bound or timed out",
        ));
    }
    serde_json::from_slice(&bytes).map_err(|_| error("ROOT client returned malformed metadata"))
}

fn reply_scope(installed: &Installed, value: &Value) -> io::Result<()> {
    if value["protocol"] != PROTOCOL
        || value["session_id"] != installed.policy.session_id
        || value["policy_sha256"] != installed.policy_sha256
    {
        return Err(error(
            "ROOT reply differs from the installed session policy",
        ));
    }
    Ok(())
}

pub fn control(id: &str, operation: &str) -> io::Result<Value> {
    if !matches!(operation, "describe" | "prepare" | "resume" | "stop") {
        return Err(error("ROOT control requires a fixed lifecycle operation"));
    }
    let installed = installed(id)?;
    let value = json_call(
        installed.command(operation)?,
        installed.policy.limits.operation_seconds,
    )?;
    check_control_reply(&installed, &value, operation)?;
    Ok(value)
}
/// Check a current session without ending its persistent provider transport.
/// ROOT validates live quota, exact container and gateway authority afresh;
/// a cached description or client mirror disk sample never authorizes a turn.
pub fn guard(manifest:&Manifest)->io::Result<()> {
    let installed=installed(&manifest.session_id)?;manifest_scope(manifest,&installed)?;
    let value=json_call(installed.command("guard")?,installed.policy.limits.operation_seconds)?;
    reply_scope(&installed,&value)?;
    if value.as_object().is_none_or(|m|m.len()!=6)||value["status"]!="ready"||value["guard_ready"]!=true
        ||value["cid"].as_str()!=manifest.container_id.as_deref() {
        return Err(error("ROOT turn guard did not confirm the current exact worker"));
    }
    Ok(())
}

fn check_control_reply(installed: &Installed, value: &Value, operation: &str) -> io::Result<()> {
    reply_scope(installed, value)?;
    let status = value["status"]
        .as_str()
        .ok_or_else(|| error("ROOT reply status unavailable"))?;
    if !matches!(
        status,
        "unprepared" | "preparing" | "ready" | "stopped" | "cleanup-required"
    ) || matches!(operation, "prepare" | "resume") && status != "ready"
        || status == "ready" && !value["cid"].as_str().is_some_and(digest)
        || !value["cid"].is_null() && !value["cid"].as_str().is_some_and(digest)
    {
        return Err(error("ROOT lifecycle reply is incomplete or not ready"));
    }
    Ok(())
}

fn caller_directory(path: &Path, private: bool) -> io::Result<File> {
    if !absolute(path) {
        return Err(error("caller staging directory must be absolute"));
    }
    let mut directory = File::open("/")?;
    let mut private_ancestor = false;
    for part in path.components().skip(1) {
        let Component::Normal(name) = part else {
            return Err(error("caller directory escapes its scope"));
        };
        let name =
            CString::new(name.as_bytes()).map_err(|_| error("unsafe caller directory name"))?;
        let fd = unsafe {
            libc::openat(
                directory.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        directory = unsafe { File::from_raw_fd(fd) };
        let meta = directory.metadata()?;
        if (meta.uid() != 0 && meta.uid() != unsafe { libc::geteuid() })
            || !private_ancestor && meta.mode() & 0o022 != 0
        {
            return Err(error(
                "caller staging directory ancestor is writable by another identity",
            ));
        }
        // A caller0700 ancestor protects subordinate staging directories from
        // the distinct Engine UID, even on an owner-group775 SSD cache.
        if meta.uid() == unsafe { libc::geteuid() } && meta.mode() & 0o077 == 0 {
            private_ancestor = true;
        }
    }
    let meta = directory.metadata()?;
    if meta.uid() != unsafe { libc::geteuid() } || private && meta.mode() & 0o077 != 0 {
        return Err(error("caller staging directory is not owned and private"));
    }
    Ok(directory)
}

fn home_inventory(home: &Path, families: &[&str]) -> io::Result<Vec<(PathBuf, PathBuf)>> {
    caller_directory(home, true)?;
    let mut output = Vec::new();
    let mut bytes = 0;
    let mut entries_seen = 0;
    fn walk(
        home: &Path,
        relative: &Path,
        directory: bool,
        output: &mut Vec<(PathBuf, PathBuf)>,
        bytes: &mut u64,
        entries_seen: &mut usize,
        depth: usize,
    ) -> io::Result<()> {
        if depth > 24 {
            return Err(error("prepared provider snapshot exceeds directory depth"));
        }
        let source = home.join(relative);
        let meta = match fs::symlink_metadata(&source) {
            Err(cause) if cause.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(cause) => return Err(cause),
            Ok(meta) => meta,
        };
        *entries_seen += 1;
        if *entries_seen > MAX_HOME_FILES {
            return Err(error("prepared provider inventory exceeds entry bound"));
        }
        if meta.uid() != unsafe { libc::geteuid() }
            || meta.mode() & 0o077 != 0
            || meta.file_type().is_symlink()
            || !meta.is_dir() && !meta.is_file()
        {
            return Err(error(
                "prepared provider input is not owner-private or is a link/special entry",
            ));
        }
        if directory {
            if !meta.is_dir() {
                return Err(error("prepared provider snapshot is not a directory"));
            }
            caller_directory(&source, true)?;
            let mut entries = fs::read_dir(&source)?
                .take(MAX_HOME_FILES + 1)
                .collect::<io::Result<Vec<_>>>()?;
            if entries.len() > MAX_HOME_FILES {
                return Err(error("prepared provider directory exceeds entry bound"));
            }
            entries.sort_by_key(|entry| entry.file_name());
            for entry in entries {
                let name = entry.file_name();
                if matches!(
                    name.to_str(),
                    Some(".git" | "hooks" | ".mcp.json" | "mcp.json")
                ) {
                    return Err(error(
                        "prepared provider input includes excluded Git/hooks/MCP content",
                    ));
                }
                let meta = fs::symlink_metadata(entry.path())?;
                walk(
                    home,
                    &relative.join(name),
                    meta.is_dir(),
                    output,
                    bytes,
                    entries_seen,
                    depth + 1,
                )?;
            }
        } else {
            if !meta.is_file() || meta.nlink() != 1 {
                return Err(error(
                    "prepared provider input must be a single-link regular file",
                ));
            }
            let max = match relative.to_str() {
                Some("claude/.credentials.json" | "codex/auth.json") => 1024 * 1024,
                Some("claude/settings.json") => 65536,
                Some("claude/.doxa-logged-out") => 1024,
                _ => 4 * 1024 * 1024,
            };
            *bytes = bytes
                .checked_add(meta.len())
                .ok_or_else(|| error("prepared input byte count overflow"))?;
            if meta.len() > max || *bytes > MAX_HOME_BYTES || output.len() >= MAX_HOME_FILES {
                return Err(error(
                    "prepared provider input exceeds its file/inventory byte bound",
                ));
            }
            // Verify ancestors and visible inode before the pinned Python
            // client independently opens and hashes the exact source again.
            caller_directory(
                source
                    .parent()
                    .ok_or_else(|| error("prepared input parent missing"))?,
                true,
            )?;
            output.push((relative.to_owned(), source));
        }
        Ok(())
    }
    for family in families {
        match *family {
            "claude" => {
                for name in [".credentials.json", "settings.json", ".doxa-logged-out"] {
                    walk(
                        home,
                        &Path::new("claude").join(name),
                        false,
                        &mut output,
                        &mut bytes,
                        &mut entries_seen,
                        0,
                    )?;
                }
                for name in ["skills", "adopted-plugins"] {
                    walk(
                        home,
                        &Path::new("claude").join(name),
                        true,
                        &mut output,
                        &mut bytes,
                        &mut entries_seen,
                        2,
                    )?;
                }
            }
            "codex" => walk(
                home,
                Path::new("codex/auth.json"),
                false,
                &mut output,
                &mut bytes,
                &mut entries_seen,
                0,
            )?,
            _ => return Err(error("unsupported provider home inventory family")),
        }
    }
    Ok(output)
}

fn add_home(command: &mut Command, inventory: Vec<(PathBuf, PathBuf)>) {
    for (relative, source) in inventory {
        command.arg("--home-file").arg(relative).arg(source);
    }
}

pub fn prepare(id: &str, checkout: &Path, home: &Path) -> io::Result<Value> {
    let installed = installed(id)?;
    caller_directory(checkout, false)?;
    let families: Vec<_> = installed
        .policy
        .providers
        .keys()
        .map(String::as_str)
        .collect();
    let mut command = installed.command("prepare")?;
    command.arg("--checkout").arg(checkout);
    add_home(&mut command, home_inventory(home, &families)?);
    let value = json_call(command, installed.policy.limits.operation_seconds)?;
    check_control_reply(&installed, &value, "prepare")?;
    Ok(value)
}

fn manifest_scope(manifest: &Manifest, installed: &Installed) -> io::Result<()> {
    let root = manifest
        .checkout
        .parent()
        .ok_or_else(|| error("hardened caller mirror root unavailable"))?;
    if manifest.profile.key() != "docker-hardened"
        || manifest.session_id != installed.policy.session_id
        || manifest.policy_hash != installed.policy_sha256
        || manifest.state != "ready"
        || !absolute(root)
        || manifest.checkout != root.join("checkout")
        || manifest.private_home != root.join("home")
        || manifest.cache != root.join("cache")
        || manifest.broker != root.join("broker")
    {
        return Err(error(
            "saved hardened mirror differs from its installed ROOT policy",
        ));
    }
    Ok(())
}

fn relative_path(path: &Path, allow_empty: bool) -> io::Result<String> {
    if path.is_absolute()
        || path
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return Err(error("ROOT client relative path escapes its fixed scope"));
    }
    let text = path
        .to_str()
        .ok_or_else(|| error("ROOT client path is not Unicode"))?;
    if text.is_empty() && !allow_empty
        || text.len() > 4096
        || text.contains('\0')
        || !text.is_empty()
            && text
                .split('/')
                .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return Err(error("ROOT client path exceeds its bound"));
    }
    Ok(if text.is_empty() {
        ".".into()
    } else {
        text.into()
    })
}

fn cwd_relative(
    command: &Command,
    manifest: &Manifest,
    explicit: Option<&Path>,
) -> io::Result<String> {
    let cwd = explicit
        .or_else(|| command.get_current_dir())
        .unwrap_or(&manifest.checkout);
    let relative = cwd
        .strip_prefix(&manifest.checkout)
        .map_err(|_| error("hardened command cwd escapes the caller checkout"))?;
    relative_path(relative, true)
}

fn map_argument(value: &str, manifest: &Manifest) -> io::Result<String> {
    let mut mapped = value.to_owned();
    for (mirror, worker) in [
        (&manifest.checkout, "/workspace"),
        (&manifest.private_home, "/home/doxa"),
        (&manifest.cache, "/work-cache"),
        (&manifest.broker, "/run/doxa/session"),
    ] {
        let mirror = mirror
            .to_str()
            .ok_or_else(|| error("caller mirror path is not Unicode"))?;
        let mut result = String::new();
        let mut start = 0;
        while let Some(found) = mapped[start..].find(mirror) {
            let at = start + found;
            let end = at + mirror.len();
            let preceding = mapped[..at].chars().next_back();
            let following = mapped[end..].chars().next();
            let boundary = |c: char| c.is_whitespace() || "\"'=:,;()[]{}".contains(c);
            result.push_str(&mapped[start..at]);
            if preceding.is_none_or(boundary) && following.is_none_or(|c| c == '/' || boundary(c)) {
                result.push_str(worker);
            } else {
                result.push_str(mirror);
            }
            start = end;
        }
        result.push_str(&mapped[start..]);
        mapped = result;
    }
    Ok(mapped)
}

fn command_env(command: &Command, key: &str) -> Option<OsString> {
    match command
        .get_envs()
        .find(|(name, _)| *name == OsStr::new(key))
    {
        Some((_, Some(value))) => Some(value.to_owned()),
        Some((_, None)) => None,
        None => std::env::var_os(key),
    }
}

fn provider_inner(
    command: &Command,
    manifest: &Manifest,
    family: &str,
    installed: &Installed,
) -> io::Result<Command> {
    let provider = installed
        .policy
        .providers
        .get(family)
        .ok_or_else(|| error("provider family is not installed for this ROOT session"))?;
    let mut result = installed.command("provider_exec")?;
    result.args([
        "--family",
        family,
        "--cwd",
        &cwd_relative(command, manifest, None)?,
    ]);
    add_home(
        &mut result,
        home_inventory(&manifest.private_home, &[family])?,
    );
    let mut secrets = Vec::new();
    for key in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"] {
        if let Some(value) = command_env(command, key).filter(|value| !value.is_empty()) {
            secrets.push(value);
        }
    }
    for key in &provider.environment_keys {
        if let Some(value) = command_env(command, key).filter(|value| !value.is_empty()) {
            result.env(key, value).args(["--env", key]);
        }
    }
    // The broker supplies these two fixed values. Caller overrides must point
    // at the corresponding mirror/fixed worker path, never another home.
    for (key, target) in [
        ("CLAUDE_CONFIG_DIR", "/home/doxa/claude"),
        ("CODEX_HOME", "/home/doxa/codex"),
    ] {
        if let Some((_, Some(value))) = command
            .get_envs()
            .find(|(name, _)| *name == OsStr::new(key))
        {
            let value = value
                .to_str()
                .ok_or_else(|| error("provider configuration path is not Unicode"))?;
            if map_argument(value, manifest)? != target {
                return Err(error(
                    "provider configuration path escapes its fixed worker home",
                ));
            }
        }
    }
    let args: Vec<_> = command.get_args().collect();
    if args.len() > 256 {
        return Err(error("hardened provider argument count exceeds bound"));
    }
    let mut total = 0;
    result.arg("--");
    for argument in args {
        let raw = argument.as_bytes();
        if secrets.iter().any(|secret| {
            raw.windows(secret.as_bytes().len())
                .any(|part| part == secret.as_bytes())
        }) {
            return Err(error(
                "API credential cannot appear in ROOT client arguments",
            ));
        }
        let argument = argument
            .to_str()
            .ok_or_else(|| error("provider argument is not Unicode"))?;
        let mapped = map_argument(argument, manifest)?;
        total += mapped.len();
        if mapped.len() > 8192 || total > 65536 {
            return Err(error("provider argument byte bound exceeded"));
        }
        result.arg(mapped);
    }
    Ok(result)
}

pub fn provider_command(
    command: Command,
    manifest: &Manifest,
    family: &str,
) -> io::Result<Command> {
    if !matches!(family, "claude" | "codex") {
        return Err(error("unsupported hardened provider family"));
    }
    let installed = installed(&manifest.session_id)?;
    manifest_scope(manifest, &installed)?;
    provider_inner(&command, manifest, family, &installed)
}

pub fn workspace_command(command: Command, manifest: &Manifest, cwd: &Path) -> io::Result<Command> {
    if command.get_program() != OsStr::new("git")
        && command.get_program() != OsStr::new("/usr/bin/git")
    {
        return Err(error("ROOT workspace transport supports Git only"));
    }
    let installed = installed(&manifest.session_id)?;
    manifest_scope(manifest, &installed)?;
    let mut result = installed.command("workspace_exec")?;
    result
        .args(["--cwd", &cwd_relative(&command, manifest, Some(cwd))?])
        .arg("--");
    for argument in workspace_arguments(&command, manifest)? {
        result.arg(argument);
    }
    Ok(result)
}

fn workspace_arguments(command: &Command, manifest: &Manifest) -> io::Result<Vec<String>> {
    let mut bytes = 0;
    if command.get_args().count() > 256 {
        return Err(error("Git argument count exceeds ROOT bound"));
    }
    let mut arguments = Vec::new();
    for argument in command.get_args() {
        let text = argument
            .to_str()
            .ok_or_else(|| error("Git argument is not Unicode"))?;
        bytes += text.len();
        if text.len() > 8192 || bytes > 65536 {
            return Err(error("Git argument exceeds ROOT byte bound"));
        }
        arguments.push(map_argument(text, manifest)?);
    }
    let mut prefix = 0;
    let mut no_pager = false;
    while let Some(argument) = arguments.get(prefix) {
        if argument == "-c" {
            let setting = arguments.get(prefix + 1).map(String::as_str);
            if !matches!(
                setting,
                Some(
                    "core.quotepath=false"
                        | "core.fsmonitor=false"
                        | "core.hooksPath=/dev/null"
                        | "core.attributesFile=/dev/null"
                )
            ) {
                return Err(error(
                    "caller Git configuration is not a trusted transport setting",
                ));
            }
            // ROOT injects these exact settings itself. Never pass caller -c
            // syntax to its builtin-only parser, even for harmless settings.
            prefix += 2;
        } else if argument == "--no-pager" && !no_pager {
            no_pager = true;
            prefix += 1;
        } else {
            break;
        }
    }
    if !arguments.get(prefix).is_some_and(|name| {
        matches!(
            name.as_str(),
            "status" | "diff" | "rev-parse" | "merge-base" | "ls-files" | "apply"
        )
    }) {
        return Err(error(
            "ROOT workspace Git builtin or global option is unsupported",
        ));
    }
    let mut output = if no_pager {
        vec!["--no-pager".into()]
    } else {
        Vec::new()
    };
    output.extend(arguments.into_iter().skip(prefix));
    Ok(output)
}

fn snapshot_local(snapshot: &ProviderSnapshot) -> bool {
    let proof = &snapshot.proof;
    let thread_matches = match &snapshot.thread {
        Some(thread) => proof["source"]["thread"].as_str() == Some(thread),
        // ROOT records the actual transcript thread even when the caller did
        // not request a particular one. Its value remains in exact metadata.
        None => proof["source"]["thread"].is_null() || proof["source"]["thread"].as_str().is_some(),
    };
    let range_matches = match (proof["offset"].as_u64(), proof["source"]["size"].as_u64()) {
        (Some(offset), Some(size)) => {
            size <= MAX_SNAPSHOT
                && offset <= size
                && offset
                    .checked_add(snapshot.bytes.len() as u64)
                    .is_some_and(|end| end <= size)
                && (offset == 0 || snapshot.bytes.len() <= 8 * 1024 * 1024)
                && (offset != 0
                    || snapshot.bytes.len() as u64 != size
                    || proof["source"]["sha256"] == proof["chunk_sha256"])
        }
        _ => false,
    };
    session_id(&snapshot.session_id).is_ok()
        && matches!(snapshot.family.as_str(), "claude" | "codex")
        && relative_path(&snapshot.relative, false).is_ok()
        && proof["protocol"] == PROTOCOL
        && proof["status"] == "snapshot"
        && proof["session_id"] == snapshot.session_id
        && proof["family"] == snapshot.family
        && proof["cid"].as_str().is_some_and(digest)
        && proof["policy_sha256"].as_str().is_some_and(digest)
        && proof["boot_id"].as_str().is_some_and(boot_id)
        && proof["source"]["relative"].as_str() == snapshot.relative.to_str()
        && proof["source"]
            .as_object()
            .is_some_and(|source| source.contains_key("thread"))
        && thread_matches
        && range_matches
        && proof["source"]["device"].as_u64().is_some_and(|id| id != 0)
        && proof["source"]["inode"].as_u64().is_some_and(|id| id != 0)
        && proof["source"]["size"].as_u64().is_some()
        && proof["source"]["sha256"].as_str().is_some_and(digest)
        && proof["size"] == proof["source"]["size"]
        && proof["sha256"] == proof["source"]["sha256"]
        && proof["source"]["ctime"].as_i64().is_some()
        && proof["source"]["mtime"].as_i64().is_some()
        && proof["source"]["ctime_nsec"]
            .as_u64()
            .is_some_and(|n| n < 1_000_000_000)
        && proof["source"]["mtime_nsec"]
            .as_u64()
            .is_some_and(|n| n < 1_000_000_000)
        && proof["bytes"].as_u64() == Some(snapshot.bytes.len() as u64)
        && snapshot.bytes.len() <= MAX_SNAPSHOT as usize
        && proof["offset"].as_u64().is_some()
        && proof["chunk_sha256"] == format!("{:x}", Sha256::digest(&snapshot.bytes))
}

fn expose_snapshot_source(mut proof: Value) -> io::Result<Value> {
    let size = proof["source"]["size"].clone();
    let hash = proof["source"]["sha256"].clone();
    let object = proof
        .as_object_mut()
        .ok_or_else(|| error("ROOT snapshot metadata is not an object"))?;
    if object.contains_key("size")
        || object.contains_key("sha256")
        || size.as_u64().is_none()
        || !hash.as_str().is_some_and(digest)
    {
        return Err(error(
            "ROOT snapshot source aliases conflict or are malformed",
        ));
    }
    object.insert("size".into(), size);
    object.insert("sha256".into(), hash);
    Ok(proof)
}

fn transport_snapshot_proof(snapshot: &ProviderSnapshot) -> io::Result<Value> {
    if !snapshot_local(snapshot) {
        return Err(error("ROOT snapshot source aliases or bytes differ"));
    }
    let mut proof = snapshot.proof.clone();
    let object = proof
        .as_object_mut()
        .ok_or_else(|| error("ROOT snapshot metadata is not an object"))?;
    // Remove exactly the two caller API aliases. All original metadata,
    // including source/thread/offset/hash and additional fields, is retained.
    object.remove("size");
    object.remove("sha256");
    if serde_json::to_vec(&proof)?.len() > MAX_REPLY as usize {
        return Err(error("ROOT snapshot transport metadata exceeds 64 KiB"));
    }
    Ok(proof)
}

fn read_snapshot_file(path: &Path, limit: u64) -> io::Result<Vec<u8>> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
        .open(path)?;
    let before = file.metadata()?;
    if !before.is_file()
        || before.uid() != unsafe { libc::geteuid() }
        || before.nlink() != 1
        || before.mode() & 0o7777 != 0o600
        || before.len() > limit
    {
        return Err(error(
            "ROOT snapshot destination is not a private bounded single-link file",
        ));
    }
    let mut bytes = Vec::new();
    file.try_clone()?.take(limit + 1).read_to_end(&mut bytes)?;
    let visible = fs::symlink_metadata(path)?;
    if identity(&before) != identity(&file.metadata()?)
        || identity(&before) != identity(&visible)
        || visible.file_type().is_symlink()
        || bytes.len() as u64 != before.len()
    {
        return Err(error("ROOT snapshot destination changed while reading"));
    }
    Ok(bytes)
}

fn snapshot_directory(
    configured: Option<&OsStr>,
    cache: Option<&Path>,
) -> io::Result<tempfile::TempDir> {
    let base = if let Some(configured) = configured {
        fs::canonicalize(configured)?
    } else {
        let cache = cache.ok_or_else(|| error("ROOT snapshot caller cache is unavailable"))?;
        if cache.starts_with("/tmp") {
            return Err(error("ROOT snapshot caller cache must be outside /tmp"));
        }
        let pinned = caller_directory(cache, true)?;
        if unsafe { libc::mkdirat(pinned.as_raw_fd(), c"tmp".as_ptr(), 0o700) } < 0
            && io::Error::last_os_error().raw_os_error() != Some(libc::EEXIST)
        {
            return Err(io::Error::last_os_error());
        }
        let path = cache.join("tmp");
        let tmp = caller_directory(&path, true)?;
        if tmp.metadata()?.mode() & 0o7777 != 0o700 {
            return Err(error(
                "ROOT snapshot cache/tmp must be caller-owned mode0700",
            ));
        }
        path
    };
    if base.starts_with("/tmp") {
        return Err(error(
            "ROOT snapshots require a private real-disk temporary directory",
        ));
    }
    caller_directory(&base, configured.is_none())?;
    let directory=tempfile::Builder::new()
        .permissions(std::os::unix::fs::PermissionsExt::from_mode(0o700))
        .prefix("doxa-root-snapshot-")
        .tempdir_in(base)?;
    Ok(directory)
}

pub fn snapshot(
    manifest: &Manifest,
    family: &str,
    relative: &Path,
    thread: Option<&str>,
    offset: u64,
    limit: u64,
) -> io::Result<ProviderSnapshot> {
    if !matches!(family, "claude" | "codex")
        || limit == 0
        || limit > MAX_SNAPSHOT
        || offset != 0 && limit > 8 * 1024 * 1024
        || offset.checked_add(limit).is_none()
        || thread.is_some_and(|id| {
            id.is_empty()
                || id.len() > 128
                || !id.as_bytes()[0].is_ascii_alphanumeric()
                || !id
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_'))
        })
    {
        return Err(error(
            "ROOT provider snapshot scope or byte bound is invalid",
        ));
    }
    let relative_text = relative_path(relative, false)?;
    let installed = installed(&manifest.session_id)?;
    manifest_scope(manifest, &installed)?;
    if !installed.policy.providers.contains_key(family) {
        return Err(error("snapshot family is not installed"));
    }
    let configured = std::env::var_os("TMPDIR").filter(|value| !value.is_empty());
    let directory = snapshot_directory(configured.as_deref(), Some(&manifest.cache))?;
    // Python creates this nonexistent pathname with O_EXCL0600. The caller
    // owns the enclosing random0700 directory; ROOT never opens the copy.
    let destination = directory.path().join("snapshot.bin");
    let mut command = installed.command("provider_snapshot")?;
    command
        .args([
            "--family",
            family,
            "--relative",
            &relative_text,
            "--offset",
            &offset.to_string(),
            "--limit",
            &limit.to_string(),
        ])
        .arg("--snapshot-destination")
        .arg(&destination);
    if let Some(thread) = thread {
        command.args(["--thread", thread]);
    }
    let proof = expose_snapshot_source(json_call(
        command,
        installed.policy.limits.operation_seconds,
    )?)?;
    reply_scope(&installed, &proof)?;
    if proof["offset"].as_u64() != Some(offset) || proof["boot_id"] != installed.receipt.boot_id
        ||proof["cid"].as_str()!=manifest.container_id.as_deref() {
        return Err(error("ROOT snapshot destination or request scope changed"));
    }
    let bytes = read_snapshot_file(&destination, limit)?;
    let result = ProviderSnapshot {
        bytes,
        proof,
        session_id: manifest.session_id.clone(),
        family: family.into(),
        relative: relative.to_owned(),
        thread: thread.map(str::to_owned),
    };
    if !snapshot_local(&result) {
        return Err(error("ROOT snapshot bytes or original source proof differ"));
    }
    Ok(result)
}

/// Recheck the actual ROOT-owned source, not the caller's review copy. Local
/// byte/proof tampering returns false before any transport is attempted.
pub fn verify_snapshot(snapshot: &ProviderSnapshot) -> io::Result<bool> {
    if !snapshot_local(snapshot) {
        return Ok(false);
    }
    let installed = installed(&snapshot.session_id)?;
    if snapshot.proof["policy_sha256"] != installed.policy_sha256
        || snapshot.proof["boot_id"] != installed.receipt.boot_id
    {
        return Ok(false);
    }
    let configured = std::env::var_os("TMPDIR").filter(|value| !value.is_empty());
    let fallback = if configured.is_none() {
        let manifest = crate::read_manifest(&crate::manifest_path(
            &crate::home()?,
            &snapshot.session_id,
        )?)?;
        manifest_scope(&manifest, &installed)?;
        Some(manifest)
    } else {
        None
    };
    let directory = snapshot_directory(
        configured.as_deref(),
        fallback.as_ref().map(|manifest| manifest.cache.as_path()),
    )?;
    let mut expect = tempfile::NamedTempFile::new_in(directory.path())?;
    serde_json::to_writer(expect.as_file_mut(), &transport_snapshot_proof(snapshot)?)?;
    expect.as_file_mut().write_all(b"\n")?;
    expect.as_file_mut().sync_all()?;
    let mut command = installed.command("verify_provider_snapshot")?;
    command
        .args([
            "--family",
            &snapshot.family,
            "--relative",
            &relative_path(&snapshot.relative, false)?,
        ])
        .arg("--expect-file")
        .arg(expect.path());
    if let Some(thread) = &snapshot.thread {
        command.args(["--thread", thread]);
    }
    let value = json_call(command, installed.policy.limits.operation_seconds)?;
    reply_scope(&installed, &value)?;
    Ok(value["status"] == "snapshot-verified"
        && value["matches"] == true
        && value["family"] == snapshot.family
        && value["boot_id"] == installed.receipt.boot_id
        && value["cid"] == snapshot.proof["cid"]
        && value["source"] == snapshot.proof["source"])
}

/// Register a caller-owned callback through the authenticated ROOT process.
/// The one-use capability is carried in environment/socket payloads only.
pub fn register_hook(
    manifest: &Manifest,
    socket: &Path,
    capability: &str,
    pid: u32,
    register: bool,
) -> io::Result<Value> {
    let installed = installed(&manifest.session_id)?;
    manifest_scope(manifest, &installed)?;
    let command = hook_command(&installed, manifest, socket, capability, pid, register)?;
    let value = json_call(command, installed.policy.limits.operation_seconds)?;
    reply_scope(&installed, &value)?;
    if value["status"]
        != if register {
            "registered"
        } else {
            "unregistered"
        }
        || value["hook_ready"] != register
        || value["cid"].as_str()!=manifest.container_id.as_deref()
    {
        return Err(error("ROOT callback acknowledgement is incomplete"));
    }
    Ok(value)
}

fn hook_command(
    installed: &Installed,
    manifest: &Manifest,
    socket: &Path,
    capability: &str,
    pid: u32,
    register: bool,
) -> io::Result<Command> {
    if pid == 0
        || pid > i32::MAX as u32
        || capability.len() != 48
        || !capability
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        || !absolute(socket)
        || socket.as_os_str().as_bytes().len() >= 108
    {
        return Err(error("ROOT callback identity or capability is invalid"));
    }
    if register {
        if !callback_path_scope(socket,manifest) {
            return Err(error("ROOT callback socket escapes the private caller callback scope"));
        }
        caller_directory(socket.parent().unwrap(), true)?;
        let meta=fs::symlink_metadata(socket)?;
        if !meta.file_type().is_socket()||meta.uid()!=unsafe{libc::geteuid()}||meta.mode()&0o777!=0o600||meta.nlink()!=1 {
            return Err(error("ROOT callback socket is not private and owned"));
        }
    }
    let operation = if register {
        "register_hook"
    } else {
        "unregister_hook"
    };
    let mut command = installed.command(operation)?;
    command
        .args([
            "--callback-pid",
            &pid.to_string(),
            "--capability-env",
            "DOXA_ROOT_HOOK_CAPABILITY",
        ])
        .env("DOXA_ROOT_HOOK_CAPABILITY", capability);
    if register {
        command.arg("--callback-socket").arg(socket);
    }
    Ok(command)
}
fn callback_path_scope(socket:&Path,manifest:&Manifest)->bool {
    if !absolute(socket){return false;}
    if socket.parent()==Some(manifest.broker.as_path()){return true;}
    let base=PathBuf::from(format!("/run/user/{}/doxa-hooks",unsafe{libc::geteuid()}));
    let Ok(relative)=socket.strip_prefix(base) else{return false;};
    let parts:Vec<_>=relative.components().collect();
    parts.len()==2&&matches!(parts[0],Component::Normal(name) if name.to_str().is_some_and(|name|
        name.strip_prefix("review-").is_some_and(|value|value.len()==6&&value.bytes().all(|b|b.is_ascii_alphanumeric()))))
        &&matches!(parts[1],Component::Normal(name) if name=="callback.sock")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Profile;
    use serde_json::json;
    use std::os::unix::fs::{symlink, PermissionsExt};

    fn temporary() -> tempfile::TempDir {
        let base =
            fs::canonicalize(std::env::var_os("TMPDIR").expect("tests require real-disk TMPDIR"))
                .unwrap();
        assert!(!base.starts_with("/tmp"));
        tempfile::Builder::new()
            .prefix("root-client-test-")
            .tempdir_in(base)
            .unwrap()
    }
    fn private_dir(path: &Path) {
        if !path.exists() {
            if let Some(parent) = path.parent() {
                if !parent.exists() {
                    private_dir(parent);
                }
            }
        }
        fs::create_dir_all(path).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    }
    fn private_file(path: &Path, bytes: &[u8]) {
        fs::write(path, bytes).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    }
    // This fixture is solely command/metadata input. It never enters installed()
    // or supplies admission or live ROOT-peer evidence.
    fn fixture() -> Installed {
        let policy: Policy = serde_json::from_value(json!({
            "version":1,"session_id":"root-client-test","caller_uid":1000,
            "engine":{"uid":1001,"gid":1001,"socket":"/run/user/1001/docker.sock",
                "exe_path":"/usr/bin/dockerd","exe_sha256":"a".repeat(64),
                "config_path":"/etc/doxa/engine.json","config_sha256":"b".repeat(64),"api_version":"1.51"},
            "root":"/srv/doxa/root-client-test","image":format!("sha256:{}","c".repeat(64)),
            "worker":{"path":"/usr/local/bin/doxa-isolation-worker","sha256":"d".repeat(64)},
            "authority":{"path":"/usr/local/libexec/doxa-hardened-gateway","sha256":"e".repeat(64)},
            "broker":{"path":"/usr/local/libexec/doxa-root-broker.py","sha256":"f".repeat(64)},
            "providers":{
                "claude":{"path":"/usr/local/bin/claude","sha256":"0".repeat(64),"artifacts":{},
                    "environment_keys":["ANTHROPIC_API_KEY","LANG"],"egress":["api.anthropic.com"],"proxy_port":3128},
                "codex":{"path":"/usr/local/bin/codex","sha256":"1".repeat(64),"artifacts":{},
                    "environment_keys":["OPENAI_API_KEY","LANG"],"egress":["api.openai.com"],"proxy_port":3129}},
            "limits":{"memory_bytes":536870912,"cpu_quota":100000,"cpu_period":100000,"pids":128,
                "disk_bytes":33554432,"output_bytes":1048576,"stdin_bytes":1048576,"deadline_seconds":600,"operation_seconds":2}
        })).unwrap();
        policy.validate().unwrap();
        Installed {
            receipt: Receipt {
                contract: PROTOCOL.into(),
                session_id: policy.session_id.clone(),
                caller_uid: 1000,
                broker_uid: 0,
                broker_socket: "/run/doxa/hardened/root-client-test/broker.sock".into(),
                policy_path: "/etc/doxa/hardened/root-client-test.json".into(),
                policy_sha256: "2".repeat(64),
                broker_script: policy.broker.path.clone(),
                broker_sha256: policy.broker.sha256.clone(),
                broker_exe: "/usr/bin/python3.13".into(),
                broker_exe_sha256: "3".repeat(64),
                boot_id: "01234567-89ab-cdef-0123-456789abcdef".into(),
            },
            policy,
            policy_sha256: "2".repeat(64),
            _pins: Vec::new(),
        }
    }
    fn mirror(root: &Path) -> Manifest {
        private_dir(&root.join("home"));
        private_dir(&root.join("broker"));
        private_dir(&root.join("checkout"));
        private_dir(&root.join("cache"));
        Manifest {
            version: 1,
            session_id: "root-client-test".into(),
            profile: Profile::DockerOpen,
            policy: None,
            policy_hash: "2".repeat(64),
            source: root.join("source"),
            checkout: root.join("checkout"),
            context_cwd: None,
            provider_rollout: None,
            creation_policy_hash: String::new(),
            checkout_device: 0,
            checkout_inode: 0,
            base_sha: String::new(),
            branch: String::new(),
            private_home: root.join("home"),
            cache: root.join("cache"),
            broker: root.join("broker"),
            container_id: None,
            nonce: String::new(),
            state: "ready".into(),
        }
    }
    fn args(command: &Command) -> Vec<String> {
        command
            .get_args()
            .map(|arg| arg.to_str().unwrap().to_owned())
            .collect()
    }
    fn snapshot_fixture() -> ProviderSnapshot {
        let bytes = b"review-source\n".to_vec();
        ProviderSnapshot {
            proof: expose_snapshot_source(json!({"protocol":PROTOCOL,"status":"snapshot","session_id":"root-client-test",
            "family":"codex","cid":"4".repeat(64),"policy_sha256":"2".repeat(64),"boot_id":"01234567-89ab-cdef-0123-456789abcdef",
            "source":{"device":42,"inode":99,"size":bytes.len(),"ctime":12,"ctime_nsec":13,"mtime":14,"mtime_nsec":15,
                "sha256":format!("{:x}",Sha256::digest(&bytes)),"relative":"sessions/review.jsonl","thread":"thread-a"},
            "offset":0,"bytes":bytes.len(),"chunk_sha256":format!("{:x}",Sha256::digest(&bytes))})).unwrap(),
            bytes,
            session_id: "root-client-test".into(),
            family: "codex".into(),
            relative: "sessions/review.jsonl".into(),
            thread: Some("thread-a".into()),
        }
    }

    #[test]
    fn scope_rejects_aliases_and_session_escapes() {
        for id in ["", "../other", "-other", "a_b", "a/b", "a\0b"] {
            assert!(session_id(id).is_err());
        }
        session_id("Root-42").unwrap();
        for path in [
            "/root//policy",
            "/root/./policy",
            "/root/../policy",
            "/root/policy/",
            "relative",
            "/",
        ] {
            assert!(!absolute(Path::new(path)), "{path}");
        }
        for path in ["../file", "/file", "a//b", "a/./b", "a/../b", "a/"] {
            assert!(relative_path(Path::new(path), false).is_err(), "{path}");
        }
        assert_eq!(relative_path(Path::new(""), true).unwrap(), ".");
    }
    #[test]
    fn owner_forged_receipt_and_symlink_cannot_supply_root_pins() {
        let directory = temporary();
        let path = directory.path().join("receipt.json");
        private_file(
            &path,
            br#"{"contract":"doxa-root-session-broker-v1","admission":true}"#,
        );
        fs::set_permissions(&path, fs::Permissions::from_mode(0o444)).unwrap();
        if unsafe { libc::geteuid() } != 0 {
            assert!(root_file(&path, true, false, MAX_REPLY).is_err());
        }
        assert!(serde_json::from_slice::<Receipt>(&fs::read(&path).unwrap()).is_err());
        let link = directory.path().join("link.json");
        symlink(&path, &link).unwrap();
        assert!(root_file(&link, true, false, MAX_REPLY).is_err());
        assert!(installed("../owner-receipt").is_err());
    }
    #[test]
    fn client_command_is_pinned_isolated_and_has_no_host_loader_environment() {
        let installed = fixture();
        let command = installed.command("describe").unwrap();
        assert_eq!(command.get_program(), OsStr::new("/usr/bin/python3.13"));
        assert_eq!(
            args(&command),
            [
                "-I",
                "/usr/local/libexec/doxa-root-broker.py",
                "client",
                "--session-id",
                "root-client-test",
                "--op",
                "describe"
            ]
        );
        assert_eq!(command.get_envs().count(), 1);
        assert!(installed.command("docker-run").is_err());
    }
    #[test]
    fn path_mapping_preserves_prefix_boundaries_and_cwd_scope() {
        let dir = temporary();
        let manifest = mirror(dir.path());
        let prompt = format!(
            "checkout={} config='{}' socket={} cache={}",
            manifest.checkout.display(),
            manifest.private_home.join("claude").display(),
            manifest.broker.join("hook.sock").display(),
            manifest.cache.display()
        );
        assert_eq!(map_argument(&prompt,&manifest).unwrap(),"checkout=/workspace config='/home/doxa/claude' socket=/run/doxa/session/hook.sock cache=/work-cache");
        let collision = format!("{}-other/a", manifest.checkout.display());
        assert_eq!(map_argument(&collision, &manifest).unwrap(), collision);
        let mut command = Command::new("claude");
        command.current_dir(manifest.checkout.join("src"));
        assert_eq!(cwd_relative(&command, &manifest, None).unwrap(), "src");
        assert!(cwd_relative(&command, &manifest, Some(&manifest.private_home)).is_err());
        assert!(cwd_relative(
            &command,
            &manifest,
            Some(&manifest.checkout.join("../home"))
        )
        .is_err());
    }
    #[test]
    fn home_inventory_selects_prepared_family_inputs_only() {
        let dir = temporary();
        let home = dir.path().join("home");
        private_dir(&home);
        private_dir(&home.join("claude"));
        private_dir(&home.join("claude/skills/tool"));
        private_dir(&home.join("codex"));
        for path in [
            "claude/.credentials.json",
            "claude/settings.json",
            "claude/skills/tool/SKILL.md",
            "codex/auth.json",
            "codex/config.toml",
            "codex/history.jsonl",
            "claude/history.jsonl",
        ] {
            private_file(&home.join(path), b"fixture");
        }
        let claude = home_inventory(&home, &["claude"]).unwrap();
        assert_eq!(
            claude
                .iter()
                .map(|(path, _)| path.to_str().unwrap())
                .collect::<Vec<_>>(),
            [
                "claude/.credentials.json",
                "claude/settings.json",
                "claude/skills/tool/SKILL.md"
            ]
        );
        let codex = home_inventory(&home, &["codex"]).unwrap();
        assert_eq!(codex.len(), 1);
        assert_eq!(codex[0].0, Path::new("codex/auth.json"));
    }
    #[test]
    fn home_inventory_refuses_links_excluded_hooks_and_public_auth() {
        let dir = temporary();
        let home = dir.path().join("home");
        private_dir(&home);
        private_dir(&home.join("claude/skills"));
        private_file(&home.join("claude/.credentials.json"), b"fixture");
        fs::set_permissions(
            home.join("claude/.credentials.json"),
            fs::Permissions::from_mode(0o644),
        )
        .unwrap();
        assert!(home_inventory(&home, &["claude"]).is_err());
        fs::set_permissions(
            home.join("claude/.credentials.json"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        symlink(
            home.join("claude/.credentials.json"),
            home.join("claude/skills/linked"),
        )
        .unwrap();
        assert!(home_inventory(&home, &["claude"]).is_err());
        fs::remove_file(home.join("claude/skills/linked")).unwrap();
        private_dir(&home.join("claude/skills/hooks"));
        assert!(home_inventory(&home, &["claude"]).is_err());
        fs::remove_dir(home.join("claude/skills/hooks")).unwrap();
        fs::hard_link(
            home.join("claude/.credentials.json"),
            home.join("claude/skills/alias"),
        )
        .unwrap();
        assert!(home_inventory(&home, &["claude"]).is_err());
    }
    #[test]
    fn provider_transport_maps_paths_and_keeps_credentials_out_of_argv() {
        let dir = temporary();
        let manifest = mirror(dir.path());
        let installed = fixture();
        let secret = "private-api-fixture-123";
        let mut command = Command::new("untrusted-host-provider");
        command
            .env("ANTHROPIC_API_KEY", secret)
            .env("OPENAI_API_KEY", "other-family-key")
            .env("LD_PRELOAD", "/owner/plugin.so")
            .env("PYTHONPATH", "/owner/python")
            .env("CLAUDE_CONFIG_DIR", manifest.private_home.join("claude"))
            .current_dir(&manifest.checkout)
            .arg("--system-prompt")
            .arg(manifest.broker.join("hook.sock"));
        let mapped = provider_inner(&command, &manifest, "claude", &installed).unwrap();
        let arguments = args(&mapped);
        assert!(!arguments.iter().any(|arg| arg.contains(secret)));
        assert!(arguments
            .windows(2)
            .any(|pair| pair == ["--env", "ANTHROPIC_API_KEY"]));
        assert!(arguments.contains(&"/run/doxa/session/hook.sock".into()));
        let environment: Vec<_> = mapped.get_envs().collect();
        assert!(environment
            .iter()
            .any(|(key, value)| *key == OsStr::new("ANTHROPIC_API_KEY")
                && *value == Some(OsStr::new(secret))));
        for key in [
            "OPENAI_API_KEY",
            "LD_PRELOAD",
            "PYTHONPATH",
            "CLAUDE_CONFIG_DIR",
            "CODEX_HOME",
        ] {
            assert!(
                !environment.iter().any(|(name, _)| *name == OsStr::new(key)),
                "{key}"
            );
        }
        command.arg(secret);
        assert!(provider_inner(&command, &manifest, "claude", &installed).is_err());
    }
    #[test]
    fn provider_transport_respects_explicit_removal_and_rejects_home_override() {
        let dir = temporary();
        let manifest = mirror(dir.path());
        let installed = fixture();
        let mut command = Command::new("claude");
        command.env_remove("ANTHROPIC_API_KEY");
        let mapped = provider_inner(&command, &manifest, "claude", &installed).unwrap();
        assert!(!args(&mapped).contains(&"ANTHROPIC_API_KEY".into()));
        command.env("CLAUDE_CONFIG_DIR", "/another/private/home");
        assert!(provider_inner(&command, &manifest, "claude", &installed).is_err());
        assert!(workspace_command(Command::new("sh"), &manifest, &manifest.checkout).is_err());
    }

    #[test]
    fn workspace_normalization_strips_only_exact_trusted_prefix_settings() {
        let dir = temporary();
        let manifest = mirror(dir.path());
        let mut command = Command::new("git");
        command.args([
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.attributesFile=/dev/null",
            "--no-pager",
            "-c",
            "core.quotepath=false",
            "diff",
            "--find-renames",
            "--unified=3",
            "HEAD",
            "--",
        ]);
        assert_eq!(
            workspace_arguments(&command, &manifest).unwrap(),
            [
                "--no-pager",
                "diff",
                "--find-renames",
                "--unified=3",
                "HEAD",
                "--"
            ]
        );
        for arguments in [
            vec!["-c", "alias.diff=!sh", "diff"],
            vec!["-c", "core.hooksPath=/owner/hooks", "diff"],
            vec!["-C", "/owner/repo", "diff"],
            vec!["--config-env=core.pager=SECRET", "diff"],
            vec!["--no-pager", "--no-pager", "diff"],
            vec!["-c"],
            vec!["fetch"],
        ] {
            let mut command = Command::new("git");
            command.args(arguments);
            assert!(workspace_arguments(&command, &manifest).is_err());
        }
    }
    #[test]
    fn control_reply_requires_installed_identity_and_ready_container() {
        let installed = fixture();
        let mut value = json!({"protocol":PROTOCOL,"session_id":"root-client-test","policy_sha256":"2".repeat(64),"status":"ready","cid":"4".repeat(64)});
        check_control_reply(&installed, &value, "prepare").unwrap();
        value["policy_sha256"] = "9".repeat(64).into();
        assert!(check_control_reply(&installed, &value, "prepare").is_err());
        value["policy_sha256"] = "2".repeat(64).into();
        value["cid"] = Value::Null;
        assert!(check_control_reply(&installed, &value, "prepare").is_err());
        value["status"] = "unprepared".into();
        assert!(check_control_reply(&installed, &value, "resume").is_err());
        check_control_reply(&installed, &value, "describe").unwrap();
    }
    #[test]
    fn snapshot_tampering_is_refused_before_root_transport() {
        let snapshot = snapshot_fixture();
        assert!(snapshot_local(&snapshot));
        let mut changed = snapshot.clone();
        changed.bytes[0] ^= 1;
        assert!(!verify_snapshot(&changed).unwrap());
        for (field, value) in [
            ("family", json!("claude")),
            ("cid", json!("owner-container")),
            ("boot_id", json!("another-boot")),
            ("bytes", json!(1)),
            ("size", json!(1)),
            ("sha256", json!("f".repeat(64))),
            ("offset", json!(u64::MAX)),
        ] {
            let mut changed = snapshot.clone();
            changed.proof[field] = value;
            assert!(!snapshot_local(&changed), "{field}");
        }
        let mut changed = snapshot.clone();
        changed.proof["source"]["ctime_nsec"] = json!(1_000_000_000);
        assert!(!snapshot_local(&changed));
        let mut changed = snapshot.clone();
        changed.proof["source"]["thread"] = Value::Null;
        assert!(!snapshot_local(&changed));
        changed.thread = None;
        assert!(snapshot_local(&changed));
        changed.proof["source"]["thread"] = json!(42);
        assert!(!snapshot_local(&changed));
    }

    #[test]
    fn snapshot_aliases_preserve_exact_transport_and_observed_thread() {
        let mut snapshot = snapshot_fixture();
        let mut original = snapshot.proof.clone();
        original.as_object_mut().unwrap().remove("size");
        original.as_object_mut().unwrap().remove("sha256");
        assert_eq!(
            expose_snapshot_source(original.clone()).unwrap(),
            snapshot.proof
        );
        assert_eq!(transport_snapshot_proof(&snapshot).unwrap(), original);
        assert!(expose_snapshot_source(snapshot.proof.clone()).is_err());
        snapshot.thread = None;
        assert!(snapshot_local(&snapshot));
        assert_eq!(transport_snapshot_proof(&snapshot).unwrap(), original);
        snapshot.thread = Some("requested-other-thread".into());
        assert!(!snapshot_local(&snapshot));
        snapshot.thread = None;
        snapshot.proof["extension"] = json!({"preserve":true});
        original["extension"] = json!({"preserve":true});
        assert_eq!(transport_snapshot_proof(&snapshot).unwrap(), original);
        snapshot.proof["source"]["sha256"] = json!("f".repeat(64));
        snapshot.proof["sha256"] = json!("f".repeat(64));
        assert!(!snapshot_local(&snapshot)); // Full local bytes must match full-source hash.
    }

    #[test]
    fn snapshot_cache_fallback_is_private_scoped_and_no_follow() {
        let root = temporary();
        let manifest = mirror(root.path());
        let directory = snapshot_directory(None, Some(&manifest.cache)).unwrap();
        let base = manifest.cache.join("tmp");
        assert!(directory.path().starts_with(&base));
        for path in [base.as_path(), directory.path()] {
            let meta = fs::symlink_metadata(path).unwrap();
            assert_eq!(meta.uid(), unsafe { libc::geteuid() });
            assert_eq!(meta.mode() & 0o7777, 0o700);
        }
        drop(directory);
        fs::set_permissions(&base, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(snapshot_directory(None, Some(&manifest.cache)).is_err());
        fs::remove_dir(&base).unwrap();
        let outside = root.path().join("other");
        private_dir(&outside);
        symlink(&outside, &base).unwrap();
        assert!(snapshot_directory(None, Some(&manifest.cache)).is_err());
        assert_eq!(fs::read_dir(&outside).unwrap().count(), 0);
        assert!(snapshot_directory(None, None).is_err());
        assert!(snapshot_directory(Some(OsStr::new("/tmp")), None).is_err());
        assert!(
            snapshot_directory(None, Some(Path::new("/tmp/root-client-unused-cache"))).is_err()
        );
    }
    #[test]
    fn snapshot_copy_is_bounded_private_regular_and_single_link() {
        let dir = temporary();
        let path = dir.path().join("snapshot.bin");
        private_file(&path, b"bytes");
        assert_eq!(read_snapshot_file(&path, 5).unwrap(), b"bytes");
        assert!(read_snapshot_file(&path, 4).is_err());
        let alias = dir.path().join("alias");
        fs::hard_link(&path, &alias).unwrap();
        assert!(read_snapshot_file(&path, 5).is_err());
        fs::remove_file(alias).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(read_snapshot_file(&path, 5).is_err());
        let link = dir.path().join("link");
        symlink(&path, &link).unwrap();
        assert!(read_snapshot_file(&link, 5).is_err());
    }
    #[test]
    fn hook_capability_is_environment_only_and_socket_scope_is_fixed() {
        // Unix sockets require short caller-private paths; this is command
        // input inspection, no callback peer or admission simulation.
        let dir = temporary();
        let manifest = mirror(dir.path());
        let installed = fixture();
        let socket = manifest.broker.join("hook.sock");
        let _listener=std::os::unix::net::UnixListener::bind(&socket).unwrap();
        fs::set_permissions(&socket,std::os::unix::fs::PermissionsExt::from_mode(0o600)).unwrap();
        let capability = "a".repeat(48);
        let command = hook_command(&installed, &manifest, &socket, &capability, 123, true).unwrap();
        assert!(!args(&command).contains(&capability));
        assert!(command.get_envs().any(|(key, value)| key
            == OsStr::new("DOXA_ROOT_HOOK_CAPABILITY")
            && value == Some(OsStr::new(&capability))));
        assert!(hook_command(
            &installed,
            &manifest,
            &manifest.private_home.join("hook.sock"),
            &capability,
            123,
            true
        )
        .is_err());
        assert!(hook_command(&installed, &manifest, &socket, "forged", 123, true).is_err());
        let runtime=PathBuf::from(format!("/run/user/{}/doxa-hooks",unsafe{libc::geteuid()}));
        assert!(callback_path_scope(&runtime.join("review-ABC123/callback.sock"),&manifest));
        for path in ["review-ABC123/other.sock","review-ABC123/sub/callback.sock","review-ABC123/../callback.sock",
            "review-bad/callback.sock","other-ABC123/callback.sock"] {
            assert!(!callback_path_scope(&runtime.join(path),&manifest),"{path}");
        }
    }
}
