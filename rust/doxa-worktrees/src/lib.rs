//! Session-owned Git worktrees compatible with Python DOXA 1.19 sidecars.
//! Every uncertain cleanup decision keeps the branch and directory.
use std::collections::HashSet;
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const MAX_GIT_BYTES: usize = 64 * 1024;
const MAX_META_BYTES: u64 = 16 * 1024;

#[derive(Clone, Debug)]
pub struct Record {
    pub path: PathBuf,
    pub branch: String,
    pub session_id: String,
}

/// Read-only branch choices for the checkout containing `cwd`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BranchStatus {
    pub branches: Vec<String>,
    pub base: Option<String>,
    pub checked_out: Option<String>,
}

/// Read-only location of one session's actual checkout. A recorded base is
/// used only when its managed sidecar passes the full ownership check.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RepoStatus {
    Repository { repo: String, base: Option<String>, checked_out: Option<String>,
        sha: Option<String>, worktree: Option<String> },
    Directory { name: String },
}

pub fn repo_status(cwd: &Path) -> Option<RepoStatus> {
    let cwd = cwd.canonicalize().ok()?;
    if !cwd.is_dir() { return None; }
    let top = git_text(&cwd, &["rev-parse", "--show-toplevel"])
        .and_then(|top| PathBuf::from(top).canonicalize().ok());
    let Some(checkout) = top else {
        let name = cwd.file_name().and_then(|name| name.to_str())
            .filter(|name| !name.is_empty()).unwrap_or("/").to_owned();
        return Some(RepoStatus::Directory { name });
    };
    let main = main_root(&checkout).unwrap_or_else(|| checkout.clone());
    let repo = main.file_name()?.to_str()?.to_owned();
    let checked_out = git_text(&checkout, &["symbolic-ref", "--quiet", "--short", "HEAD"])
        .filter(|branch| safe_ref(branch));
    let sha = git_text(&checkout, &["rev-parse", "--verify", "HEAD^{commit}"])
        .filter(|oid| valid_commit_oid(oid)).map(|oid| oid[..7].to_owned());
    let managed = read_record(&checkout);
    let base = managed.as_ref().map(|(_, base, _, _)| base.clone()).or_else(|| checked_out.clone());
    let worktree = if let Some((record, _, _, _)) = managed {
        Some(record.branch)
    } else if checkout != main {
        Some("linked worktree".into())
    } else { None };
    Some(RepoStatus::Repository { repo, base, checked_out, sha, worktree })
}

#[derive(Debug)]
pub struct Managed {
    path: PathBuf,
    created: bool,
    finished: bool,
    // Held even for an already existing worktree reused by a resumed daemon.
    lock: Option<File>,
}
impl Managed {
    pub fn path(&self) -> &Path { &self.path }
    pub fn finish(&mut self) -> String {
        if self.finished || !self.created { return String::new(); }
        self.finished = true;
        let note = finalize_locked(&self.path);
        self.lock.take();
        note
    }
}
impl Drop for Managed {
    fn drop(&mut self) {
        let note = self.finish();
        if !note.is_empty() { eprintln!("doxa-daemon: {note}"); }
    }
}

fn home() -> Option<PathBuf> {
    env::var_os("DOXA_HOME").filter(|value| !value.is_empty()).map(PathBuf::from)
        .or_else(|| env::var_os("HOME").map(|value| PathBuf::from(value).join(".doxa")))
}
fn root() -> Option<PathBuf> { Some(home()?.join("worktrees")) }
fn truthy(value: &str) -> bool { !matches!(value.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off") }

/// Environment wins over Python-compatible `~/.doxa/config.toml`; default on.
pub fn enabled() -> bool {
    if let Ok(value) = env::var("DOXA_WORKTREE") {
        if !value.trim().is_empty() { return truthy(&value); }
    }
    let Some(path) = home().map(|path| path.join("config.toml")) else { return true; };
    let Ok(text) = fs::read_to_string(path) else { return true; };
    let Ok(value) = text.parse::<toml::Value>() else { return true; };
    match value.get("worktree_per_session") {
        Some(toml::Value::Boolean(false)) => false,
        Some(toml::Value::String(text)) if !text.trim().is_empty() => truthy(text),
        _ => true,
    }
}

fn git(cwd: &Path, args: &[&str], timeout: Duration) -> Option<(bool, Vec<u8>)> {
    let mut child = Command::new("git").args(args).current_dir(cwd)
        .env_remove("GIT_DIR").env_remove("GIT_WORK_TREE")
        .env_remove("GIT_COMMON_DIR").env_remove("GIT_INDEX_FILE")
        .stdout(Stdio::piped()).stderr(Stdio::null()).spawn().ok()?;
    let mut stdout = child.stdout.take()?;
    let reader = std::thread::spawn(move || -> io::Result<Vec<u8>> {
        let mut bytes = Vec::new();
        let mut chunk = [0u8; 8192];
        loop {
            let count = stdout.read(&mut chunk)?;
            if count == 0 { break; }
            let remaining = MAX_GIT_BYTES.saturating_add(1).saturating_sub(bytes.len());
            bytes.extend_from_slice(&chunk[..count.min(remaining)]);
        }
        Ok(bytes)
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(20)),
            _ => { let _ = child.kill(); let _ = child.wait(); break None; }
        }
    };
    let bytes = reader.join().ok()?.ok()?;
    if bytes.len() > MAX_GIT_BYTES { return None; }
    Some((status?.success(), bytes))
}
fn git_text(cwd: &Path, args: &[&str]) -> Option<String> {
    let (ok, bytes) = git(cwd, args, Duration::from_secs(10))?;
    if ok { String::from_utf8(bytes).ok().map(|s| s.trim().to_owned()) } else { None }
}
fn safe_ref(value: &str) -> bool {
    !value.is_empty() && value.len() <= 200 && !value.starts_with('-') && !value.contains("..")
        && value.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"_./-".contains(&byte))
}
fn valid_commit_oid(value: &str) -> bool {
    matches!(value.len(), 40 | 64) && value.bytes().all(|byte| byte.is_ascii_hexdigit())
}
fn main_root(cwd: &Path) -> Option<PathBuf> {
    if git_text(cwd, &["rev-parse", "--is-bare-repository"])?.as_str() != "false" { return None; }
    let common = PathBuf::from(git_text(cwd, &["rev-parse", "--path-format=absolute", "--git-common-dir"])?);
    if common.file_name()? != ".git" { return None; }
    common.parent()?.canonicalize().ok()
}
pub fn is_supported_checkout(cwd: &Path) -> bool { main_root(cwd).is_some() }
fn base_ref(cwd: &Path) -> Option<String> {
    git_text(cwd, &["symbolic-ref", "--quiet", "--short", "HEAD"])
        .or_else(|| git_text(cwd, &["rev-parse", "HEAD"]))
        .filter(|value| safe_ref(value))
}
/// Resolve only an existing local or remote-tracking branch. A matching local
/// branch wins over `origin/name`, matching Python 1.19 spawn semantics.
pub fn resolve_base(cwd: &Path, requested: &str) -> Option<String> {
    if !safe_ref(requested) { return None; }
    let main = main_root(cwd)?;
    let exists = |name: &str| git_text(&main, &["show-ref", "--verify", "--quiet", name]).is_some();
    if exists(&format!("refs/heads/{requested}")) { return Some(requested.into()); }
    if exists(&format!("refs/remotes/{requested}")) {
        let local = requested.split_once('/')?.1;
        if exists(&format!("refs/heads/{local}")) { return Some(local.into()); }
        return Some(requested.into());
    }
    None
}

/// List local branches without changing either the shared checkout or a
/// session worktree. A managed session's identity branch is not a base choice.
pub fn branch_status(cwd: &Path) -> Option<BranchStatus> {
    let main = main_root(cwd)?;
    let checkout = PathBuf::from(git_text(cwd, &["rev-parse", "--show-toplevel"])?);
    let checked_out = base_ref(&checkout);
    let managed = read_record(&checkout);
    let own = managed.as_ref().map(|(record, _, _, _)| record.branch.as_str());
    let text = git_text(&main, &["branch", "--format=%(refname:short)"])?;
    let branches = text.lines().map(str::trim)
        .filter(|name| safe_ref(name) && Some(*name) != own)
        .map(str::to_owned).collect();
    Some(BranchStatus {
        branches,
        base: managed.map(|(_, base, _, _)| base).or_else(|| checked_out.clone()),
        checked_out,
    })
}

/// Change only a verified session-owned worktree. The caller must serialize
/// this with prompt admission and reject active or queued turns first.
pub fn switch_base(path: &Path, requested: &str) -> Result<String, String> {
    let (record, old_base, main, base_oid) = read_record(path).ok_or_else(||
        "no verified doxa worktree here; switching the actual checkout is refused".to_owned())?;
    if base_oid.is_none() {
        return Err("legacy worktree owner cannot be verified; switch refused".into());
    }
    let keep = || format!("kept {} — merge when ready", record.branch);
    if worktree_for_branch(&main, &record.branch).as_ref() != Some(&record.path)
        || git_text(&record.path, &["symbolic-ref", "--quiet", "--short", "HEAD"])
            .as_deref() != Some(record.branch.as_str()) {
        return Err("worktree branch identity changed; switch refused".into());
    }
    let target = resolve_base(&main, requested)
        .ok_or_else(|| format!("no such local or remote-tracking branch: {requested}"))?;
    if target == record.branch {
        return Err("this session's own branch cannot be its base".into());
    }
    let status = git_text(&record.path, &["status", "--porcelain", "--untracked-files=all", "--ignored"])
        .ok_or_else(|| "could not inspect worktree status; switch refused".to_owned())?;
    if !status.is_empty() { return Err(format!("{} has uncommitted changes; {}", record.branch, keep())); }
    let spec = format!("{old_base}..{}", record.branch);
    let ahead = git_text(&record.path, &["rev-list", "--count", &spec])
        .ok_or_else(|| "could not measure commits against the current base; switch refused".to_owned())?;
    if ahead != "0" { return Err(format!("{} is {ahead} commit(s) ahead of {old_base}; {}", record.branch, keep())); }
    let before = git_text(&record.path, &["rev-parse", "--verify", "HEAD^{commit}"])
        .filter(|oid| valid_commit_oid(oid))
        .ok_or_else(|| "could not verify worktree HEAD; switch refused".to_owned())?;
    if base_oid.as_deref() != Some(before.as_str()) {
        return Err("pinned base commit differs from worktree HEAD; switch refused".into());
    }
    let target_oid = git_text(&record.path, &["rev-parse", "--verify", &format!("{target}^{{commit}}")])
        .filter(|oid| valid_commit_oid(oid))
        .ok_or_else(|| "could not verify target commit; switch refused".to_owned())?;
    // Update the checkout/index to the exact target tree without asking Git
    // to infer a replay range. A moving old base can make `git rebase target`
    // replay commits that belonged to that base, even with zero unique work.
    // read-tree refuses local file conflicts; if the ref CAS fails afterward,
    // the resulting staged changes remain visible and finalize keeps them.
    if !git(&record.path, &["read-tree", "-m", "-u", &before, &target_oid], Duration::from_secs(30))
        .is_some_and(|(ok, _)| ok) {
        return Err(format!("checkout of {target} failed; inspect {}; {}", record.path.display(), keep()));
    }
    let full_ref = format!("refs/heads/{}", record.branch);
    if !git(&main, &["update-ref", &full_ref, &target_oid, &before], Duration::from_secs(10))
        .is_some_and(|(ok, _)| ok) {
        return Err(format!("branch moved during switch; inspect {}; {}", record.path.display(), keep()));
    }
    let after = git_text(&record.path, &["rev-parse", "--verify", "HEAD^{commit}"]);
    if after.as_deref() != Some(target_oid.as_str())
        || git_text(&record.path, &["symbolic-ref", "--quiet", "--short", "HEAD"])
            .as_deref() != Some(record.branch.as_str()) {
        return Err(format!("branch changed during switch; inspect {}; {}", record.path.display(), keep()));
    }
    // A sidecar mismatch after a successful rebase must be left in place so
    // finalize cannot infer that the new branch has no unmerged work.
    if !replace_base_record(&record.path, &main, &record.branch, &record.session_id, &old_base, &before, &target, &target_oid) {
        return Err(format!("branch moved to {target}, but metadata could not be updated; inspect {}; {}", record.path.display(), keep()));
    }
    Ok(format!("{} now based on {target}", record.branch))
}
fn short_id(id: &str) -> String {
    let short: String = id.chars().filter(char::is_ascii_alphanumeric).take(8).collect();
    if short.is_empty() { "session".into() } else { short }
}
fn owned_dir(path: &Path) -> Option<()> {
    let meta = fs::symlink_metadata(path).ok()?;
    if !meta.file_type().is_dir() || meta.uid() != unsafe { libc::geteuid() }
        || meta.permissions().mode() & 0o077 != 0 { return None; }
    Some(())
}
fn ensure_owned_dir(path: &Path) -> Option<PathBuf> {
    match fs::symlink_metadata(path) {
        Ok(meta) if !meta.file_type().is_dir() || meta.uid() != unsafe { libc::geteuid() } => return None,
        Ok(_) => {},
        Err(error) if error.kind() == io::ErrorKind::NotFound => fs::create_dir_all(path).ok()?,
        Err(_) => return None,
    }
    // Open the directory itself without following a symlink; fchmod acts on
    // that inode even if another process replaces the pathname afterward.
    let dir = OpenOptions::new().read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW).open(path).ok()?;
    let meta = dir.metadata().ok()?;
    if !meta.is_dir() || meta.uid() != unsafe { libc::geteuid() } { return None; }
    if unsafe { libc::fchmod(dir.as_raw_fd(), 0o700) } != 0 { return None; }
    owned_dir(path)?;
    path.canonicalize().ok()
}
fn meta_path(path: &Path) -> Option<PathBuf> {
    Some(root()?.join(".meta").join(format!("{}.json", path.file_name()?.to_str()?)))
}
fn lock_path(path: &Path) -> Option<PathBuf> {
    Some(root()?.join(".meta").join(format!("{}.lock", path.file_name()?.to_str()?)))
}
/// Locks are intentionally retained after cleanup. Unlinking a lock allows a
/// second process to lock a new inode while the first still holds the old one.
fn lock_worktree(path: &Path) -> Option<File> {
    let target = lock_path(path)?;
    let parent = target.parent()?;
    owned_dir(parent)?;
    let file = OpenOptions::new().read(true).write(true).create(true).mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
        .open(&target).ok()?;
    let stat = file.metadata().ok()?;
    let path_stat = fs::symlink_metadata(&target).ok()?;
    if !stat.is_file() || stat.uid() != unsafe { libc::geteuid() }
        || stat.permissions().mode() & 0o077 != 0
        || stat.dev() != path_stat.dev() || stat.ino() != path_stat.ino() {
        return None;
    }
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 { return None; }
    Some(file)
}
fn read_record(path: &Path) -> Option<(Record, String, PathBuf, Option<String>)> {
    let meta_path = meta_path(path)?;
    let raw = fs::symlink_metadata(&meta_path).ok()?;
    if !raw.file_type().is_file() || raw.uid() != unsafe { libc::geteuid() }
        || raw.permissions().mode() & 0o077 != 0 || raw.len() > MAX_META_BYTES { return None; }
    let file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(&meta_path).ok()?;
    let stat = file.metadata().ok()?;
    if stat.dev() != raw.dev() || stat.ino() != raw.ino() { return None; }
    let mut bytes = Vec::new();
    file.take(MAX_META_BYTES + 1).read_to_end(&mut bytes).ok()?;
    if bytes.len() as u64 > MAX_META_BYTES { return None; }
    let data: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
    // A named foreign machine belongs to another device. Without LORE's
    // machine identity, the safe answer is never to remove or open it.
    if data.get("machine_id").and_then(|v| v.as_str()).is_some_and(|v| !v.is_empty()) { return None; }
    let id = data.get("session_id")?.as_str()?.to_owned();
    let branch = data.get("branch")?.as_str()?.to_owned();
    let base = data.get("base_ref")?.as_str()?.to_owned();
    let base_oid = match data.get("base_oid") {
        Some(value) => Some(value.as_str()?.to_owned()),
        None => None,
    };
    if base_oid.as_deref().is_some_and(|oid| !valid_commit_oid(oid)) {
        return None;
    }
    let main = PathBuf::from(data.get("main_root")?.as_str()?);
    let root = root()?.canonicalize().ok()?;
    let canonical = path.canonicalize().ok()?;
    if canonical.parent()? != root || branch != format!("doxa/{}", short_id(&id))
        || !safe_ref(&base) || base == branch || main_root(&canonical)? != main.canonicalize().ok()?
        || path.file_name()?.to_str()? != format!("{}-{}", main.file_name()?.to_str()?, short_id(&id)) {
        return None;
    }
    Some((Record { path: canonical, branch, session_id: id }, base, main, base_oid))
}
fn write_record(path: &Path, main: &Path, branch: &str, base: &str, base_oid: &str, id: &str) -> Option<()> {
    if !valid_commit_oid(base_oid) { return None; }
    let target = meta_path(path)?;
    let parent = target.parent()?;
    ensure_owned_dir(parent)?;
    if fs::symlink_metadata(&target).is_ok() { return None; }
    let stamp = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_nanos();
    let temp = parent.join(format!(".{}.{}.tmp", id, stamp));
    let mut file = OpenOptions::new().write(true).create_new(true).mode(0o600).open(&temp).ok()?;
    let result = (|| {
        serde_json::to_writer(&mut file, &serde_json::json!({
            "main_root": main, "branch": branch, "base_ref": base,
            "base_oid": base_oid, "session_id": id
        })).ok()?;
        file.flush().ok()?;
        file.sync_all().ok()?;
        fs::hard_link(&temp, &target).ok()?;
        Some(())
    })();
    let _ = fs::remove_file(temp);
    result
}
fn replace_base_record(path: &Path, main: &Path, branch: &str, session_id: &str, old_base: &str,
    old_head: &str, new_base: &str, new_oid: &str) -> bool {
    let Some(meta) = meta_path(path) else { return false; };
    let Some(parent) = meta.parent() else { return false; };
    if owned_dir(parent).is_none() { return false; }
    let Ok(raw) = fs::symlink_metadata(&meta) else { return false; };
    if !raw.file_type().is_file() || raw.uid() != unsafe { libc::geteuid() }
        || raw.permissions().mode() & 0o077 != 0 || raw.len() > MAX_META_BYTES { return false; }
    let Ok(source) = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(&meta) else { return false; };
    let Ok(stat) = source.metadata() else { return false; };
    if (raw.dev(), raw.ino()) != (stat.dev(), stat.ino()) { return false; }
    let mut bytes = Vec::new();
    if source.take(MAX_META_BYTES + 1).read_to_end(&mut bytes).is_err() || bytes.len() as u64 > MAX_META_BYTES { return false; }
    let Ok(mut data) = serde_json::from_slice::<serde_json::Value>(&bytes) else { return false; };
    if data["branch"] != branch || data["base_ref"] != old_base
        || data["main_root"] != main.to_string_lossy().as_ref()
        || data["session_id"] != session_id { return false; }
    // A legacy Python sidecar may not have base_oid. Its observed HEAD was
    // already checked before the Git operation, and the name is preserved.
    if data["base_oid"].as_str().is_some_and(|oid| !valid_commit_oid(oid)) { return false; }
    if !valid_commit_oid(old_head) || !valid_commit_oid(new_oid) { return false; }
    data["base_ref"] = serde_json::Value::String(new_base.to_owned());
    data["base_oid"] = serde_json::Value::String(new_oid.to_owned());
    let stamp = SystemTime::now().duration_since(UNIX_EPOCH).ok().map(|t| t.as_nanos());
    let Some(stamp) = stamp else { return false; };
    let temp = parent.join(format!(".base-{}-{stamp}.tmp", std::process::id()));
    let Ok(mut file) = OpenOptions::new().write(true).create_new(true).mode(0o600).open(&temp) else { return false; };
    let written = serde_json::to_writer(&mut file, &data).is_ok()
        && file.flush().is_ok() && file.sync_all().is_ok()
        && fs::symlink_metadata(&meta).is_ok_and(|now| (now.dev(), now.ino()) == (raw.dev(), raw.ino()))
        && fs::rename(&temp, &meta).is_ok();
    if !written { let _ = fs::remove_file(&temp); }
    written
}
fn worktree_for_branch(main: &Path, branch: &str) -> Option<PathBuf> {
    let text = git_text(main, &["worktree", "list", "--porcelain"])?;
    let mut path = None;
    for line in text.lines() {
        if let Some(value) = line.strip_prefix("worktree ") { path = Some(PathBuf::from(value)); }
        if line == format!("branch refs/heads/{branch}") { return path; }
    }
    None
}
fn branch_occupied_or_unknown(main: &Path, branch: &str) -> bool {
    let Some(list) = git_text(main, &["worktree", "list", "--porcelain"]) else { return true; };
    list.lines().any(|line| line == format!("branch refs/heads/{branch}"))
}
fn delete_if_unchanged(main: &Path, branch: &str, expected_oid: &str) -> bool {
    let full_ref = format!("refs/heads/{branch}");
    git(main, &["update-ref", "-d", &full_ref, expected_oid], Duration::from_secs(10))
        .is_some_and(|(ok, _)| ok)
}

/// Create a linked checkout for a session. None means no managed checkout was
/// opened; the caller must decide whether the original cwd is permissible.
/// Existing worktrees are reused only with an exact matching sidecar, and
/// are not automatically finalized by the new daemon instance.
pub fn create(cwd: &Path, id: &str) -> Option<Managed> {
    create_from(cwd, id, None)
}

/// `requested_base` must be a validated local or remote-tracking ref. An
/// explicit choice never silently falls back to the launch directory.
pub fn create_from(cwd: &Path, id: &str, requested_base: Option<&str>) -> Option<Managed> {
    if !enabled() { return None; }
    let main = main_root(cwd)?;
    let base = match requested_base {
        Some(requested) => resolve_base(cwd, requested)?,
        None => base_ref(cwd)?,
    };
    let short = short_id(id);
    let branch = format!("doxa/{short}");
    let repo = main.file_name()?.to_str()?;
    let worktrees = ensure_owned_dir(&root()?)?;
    let path = worktrees.join(format!("{repo}-{short}"));
    if let Some(existing) = worktree_for_branch(&main, &branch) {
        let (record, old_base, recorded_main, pinned) = read_record(&existing)?;
        // Python 1.19 never writes a base pin and does not participate in
        // this advisory lock. A matching session ID is not proof that its
        // owner has stopped; do not adopt that checkout as a Rust session.
        let pinned_oid = pinned.as_deref()?;
        if record.path != existing.canonicalize().ok()? || record.session_id != id || record.path != path
            || recorded_main != main
            || requested_base.is_some() && old_base != base
            || branch == base && cwd.canonicalize().ok()? != record.path {
            return None;
        }
        let lock = lock_worktree(&record.path)?;
        // The metadata may have changed while we waited for the lock.
        let (locked, locked_base, locked_main, locked_pin) = read_record(&record.path)?;
        if locked.path != record.path || locked.session_id != id || locked.branch != branch
            || locked_base != old_base || locked_main != main || locked_pin != pinned
            || worktree_for_branch(&main, &branch).as_ref() != Some(&record.path)
            || git_text(&record.path, &["symbolic-ref", "--quiet", "--short", "HEAD"]).as_deref() != Some(branch.as_str()) {
            return None;
        }
        let head = git_text(&record.path, &["rev-parse", "--verify", "HEAD^{commit}"])
            .filter(|oid| valid_commit_oid(oid))?;
        if !git(&main, &["merge-base", "--is-ancestor", pinned_oid, &head], Duration::from_secs(10))
            .is_some_and(|(ok, _)| ok) { return None; }
        return Some(Managed { path: record.path, created: false, finished: false, lock: Some(lock) });
    }
    if branch == base { return None; }
    // Hold the same lock used by orphan cleanup before the checkout or its
    // pinned sidecar exists. Otherwise cleanup could observe the sidecar in
    // the gap between write_record and the daemon's lock acquisition.
    ensure_owned_dir(&worktrees.join(".meta"))?;
    let lock = lock_worktree(&path)?;
    if fs::symlink_metadata(&path).is_ok() || meta_path(&path).is_some_and(|p| fs::symlink_metadata(p).is_ok()) {
        return None;
    }
    let path_str = path.to_str()?;
    if !git(&main, &["worktree", "add", "-q", "-b", &branch, path_str, &base], Duration::from_secs(30))?.0 {
        return None;
    }
    let path = path.canonicalize().ok()?;
    // Resolve the newly created checkout's HEAD, not the mutable base ref.
    // The base branch can advance between selection and `git worktree add`.
    let base_oid = git_text(&path, &["rev-parse", "--verify", "HEAD^{commit}"])
        .filter(|oid| valid_commit_oid(oid));
    if base_oid.as_deref().and_then(|oid| write_record(&path, &main, &branch, &base, oid, id)).is_none() {
        eprintln!("doxa-daemon: worktree metadata unavailable; keeping {}", path.display());
        // This checkout cannot be proven ours for cleanup or safe reuse.
        // Preserve it for inspection, but never run a session in it.
        return None;
    }
    Some(Managed { path, created: true, finished: false, lock: Some(lock) })
}

/// Recreate an archived session checkout whose directory has disappeared.
/// The sidecar, pinned base commit, branch and Git's old registration must all
/// agree. This cannot recover uncommitted files from a deleted directory; the
/// branch and all its commits are retained. A recovered tree is never
/// automatically finalized when this handle is dropped.
pub fn recover_missing(path: &Path, session_id: &str) -> Result<Managed, String> {
    let refuse = |reason: &str| format!("worktree recovery refused: {reason}");
    if session_id.is_empty() { return Err(refuse("session ID is empty")); }
    let worktrees = root().ok_or_else(|| refuse("DOXA home is unavailable"))?;
    let worktrees = worktrees.canonicalize().map_err(|_| refuse("DOXA worktree root is unavailable"))?;
    owned_dir(&worktrees).ok_or_else(|| refuse("DOXA worktree root is untrusted"))?;
    let meta_dir = worktrees.join(".meta");
    owned_dir(&meta_dir).ok_or_else(|| refuse("DOXA metadata directory is untrusted"))?;
    let name = path.file_name().and_then(|v| v.to_str()).ok_or_else(|| refuse("invalid worktree path"))?;
    let expected = worktrees.join(name);
    if path != expected { return Err(refuse("worktree path is outside the managed root")); }
    if fs::symlink_metadata(path).is_ok() { return Err(refuse("checkout path already exists")); }
    let sidecar = meta_dir.join(format!("{name}.json"));
    let read_sidecar = || -> Option<(Vec<u8>, u64, u64)> {
        let raw = fs::symlink_metadata(&sidecar).ok()?;
        if !raw.file_type().is_file() || raw.uid() != unsafe { libc::geteuid() }
            || raw.permissions().mode() & 0o077 != 0 || raw.len() > MAX_META_BYTES { return None; }
        let file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(&sidecar).ok()?;
        let stat = file.metadata().ok()?;
        if (stat.dev(), stat.ino()) != (raw.dev(), raw.ino()) { return None; }
        let mut bytes = Vec::new();
        file.take(MAX_META_BYTES + 1).read_to_end(&mut bytes).ok()?;
        if bytes.len() as u64 > MAX_META_BYTES { return None; }
        Some((bytes, stat.dev(), stat.ino()))
    };
    let snapshot = read_sidecar().ok_or_else(|| refuse("sidecar is missing or untrusted"))?;
    let data: serde_json::Value = serde_json::from_slice(&snapshot.0)
        .map_err(|_| refuse("sidecar is invalid"))?;
    if data.get("machine_id").and_then(|v| v.as_str()).is_some_and(|v| !v.is_empty()) {
        return Err(refuse("sidecar belongs to another machine"));
    }
    let short = short_id(session_id);
    let branch = format!("doxa/{short}");
    let main = data.get("main_root").and_then(|v| v.as_str()).map(PathBuf::from)
        .ok_or_else(|| refuse("sidecar has no repository"))?;
    if !main.is_absolute() || main.canonicalize().ok().as_ref() != Some(&main)
        || main_root(&main).as_ref() != Some(&main)
        || main.file_name().and_then(|v| v.to_str()).map(|v| format!("{v}-{short}")) != Some(name.to_owned())
        || data.get("session_id").and_then(|v| v.as_str()) != Some(session_id)
        || data.get("branch").and_then(|v| v.as_str()) != Some(branch.as_str()) {
        return Err(refuse("sidecar identity does not match this session and repository"));
    }
    let base = data.get("base_ref").and_then(|v| v.as_str())
        .filter(|v| safe_ref(v) && *v != branch)
        .ok_or_else(|| refuse("sidecar base is invalid"))?;
    let base_oid = data.get("base_oid").and_then(|v| v.as_str())
        .filter(|v| valid_commit_oid(v))
        .ok_or_else(|| refuse("sidecar lacks a pinned base commit"))?;
    let lock = lock_worktree(path).ok_or_else(|| refuse("worktree lock is busy or untrusted"))?;
    if read_sidecar().as_ref() != Some(&snapshot) {
        return Err(refuse("sidecar changed while locking"));
    }
    if fs::symlink_metadata(path).is_ok() { return Err(refuse("checkout path was occupied")); }
    let branch_oid = git_text(&main, &["rev-parse", "--verify", &format!("refs/heads/{branch}^{{commit}}")])
        .filter(|v| valid_commit_oid(v))
        .ok_or_else(|| refuse("session branch is missing"))?;
    if !git(&main, &["cat-file", "-e", &format!("{base_oid}^{{commit}}")], Duration::from_secs(10))
        .is_some_and(|(ok, _)| ok)
        || !git(&main, &["merge-base", "--is-ancestor", base_oid, &branch_oid], Duration::from_secs(10))
            .is_some_and(|(ok, _)| ok) {
        return Err(refuse("pinned base does not belong to session branch"));
    }
    // A stale registration at this exact path is expected after manual
    // deletion. A registration elsewhere means this branch belongs to a
    // different checkout and must never be seized with --force.
    let registration = worktree_for_branch(&main, &branch);
    if registration.as_ref().is_some_and(|registered| registered != path) {
        return Err(refuse("session branch is registered to another checkout"));
    }
    if read_sidecar().as_ref() != Some(&snapshot) || fs::symlink_metadata(path).is_ok() {
        return Err(refuse("path or sidecar changed before checkout"));
    }
    let path_str = path.to_str().ok_or_else(|| refuse("worktree path is not UTF-8"))?;
    let args: Vec<&str> = if registration.is_some() {
        vec!["worktree", "add", "--force", "-q", path_str, &branch]
    } else {
        vec!["worktree", "add", "-q", path_str, &branch]
    };
    if !git(&main, &args, Duration::from_secs(30)).is_some_and(|(ok, _)| ok) {
        return Err(refuse("Git could not restore the checkout; inspect its path and branch"));
    }
    if read_sidecar().as_ref() != Some(&snapshot)
        || read_record(path).is_none_or(|(record, recorded_base, recorded_main, pinned)|
            record.session_id != session_id || record.branch != branch || record.path != path
                || recorded_main != main || recorded_base != base || pinned.as_deref() != Some(base_oid))
        || worktree_for_branch(&main, &branch).as_ref() != Some(&path.to_path_buf())
        || git_text(path, &["symbolic-ref", "--quiet", "--short", "HEAD"]).as_deref() != Some(branch.as_str())
        || git_text(path, &["rev-parse", "--verify", "HEAD^{commit}"]).as_deref() != Some(branch_oid.as_str()) {
        return Err(refuse("restored checkout could not be verified; it was kept for inspection"));
    }
    Ok(Managed { path: path.to_path_buf(), created: false, finished: false, lock: Some(lock) })
}

/// Remove only a verified, clean managed worktree with no unique commits.
/// Any uncertainty yields a keep message and leaves user data untouched.
pub fn finalize(path: &Path) -> String {
    let Some(_lock) = lock_worktree(path) else {
        return "worktree cleanup lock unavailable; kept it".into();
    };
    finalize_locked(path)
}

fn finalize_locked(path: &Path) -> String {
    let Some((record, base, main, base_oid)) = read_record(path) else {
        return "worktree ownership could not be verified; kept it".into();
    };
    // The Rust lock cannot coordinate with Python 1.19: its lifecycle never
    // takes that lock. Legacy sidecars have no base_oid, so even a clean tree
    // and a matching session ID cannot authorize its removal here.
    let Some(base_oid) = base_oid else {
        return "legacy worktree owner cannot be verified; kept it".into();
    };
    let keep = || format!("kept {} at {} — merge when ready", record.branch, record.path.display());
    if worktree_for_branch(&main, &record.branch).as_ref() != Some(&record.path) { return keep(); }
    // A user may have checked out another branch or detached HEAD. Its
    // clean checkout is still theirs; never remove it on this sidecar's say.
    if git_text(&record.path, &["symbolic-ref", "--quiet", "--short", "HEAD"])
        .as_deref() != Some(record.branch.as_str()) { return keep(); }
    if !git(&main, &["merge-base", "--is-ancestor", &base_oid, &record.branch], Duration::from_secs(10))
        .is_some_and(|(ok, _)| ok) { return keep(); }
    // Git considers ignored files disposable during `worktree remove`, even
    // without --force. They may still be valuable user data.
    let Some(status) = git_text(&record.path, &["status", "--porcelain", "--ignored", "--untracked-files=all"]) else { return keep(); };
    if !status.is_empty() { return keep(); }
    let spec = format!("{base}..{}", record.branch);
    if git_text(&record.path, &["rev-list", "--count", &spec]).as_deref() != Some("0") { return keep(); }
    let Some(before) = git_text(&main, &["rev-parse", &record.branch]) else { return keep(); };
    let Some(path_str) = record.path.to_str() else { return keep(); };
    if !git(&main, &["worktree", "remove", path_str], Duration::from_secs(30)).is_some_and(|(ok, _)| ok) {
        return keep();
    }
    if git_text(&main, &["rev-parse", &record.branch]).as_deref() != Some(before.as_str())
        || git_text(&main, &["rev-list", "--count", &spec]).as_deref() != Some("0")
        || branch_occupied_or_unknown(&main, &record.branch) {
        return format!("kept branch {} after its worktree closed; branch changed during cleanup", record.branch);
    }
    if !delete_if_unchanged(&main, &record.branch, &before) {
        return format!("kept branch {} after its worktree closed; branch deletion failed", record.branch);
    }
    if let Some(meta) = meta_path(&record.path) { let _ = fs::remove_file(meta); }
    String::new()
}

/// Read-only survey of local managed worktrees with no live session ID.
/// Malformed, foreign-machine and unverified records are never offered as paths.
pub fn list_orphans(live_ids: &HashSet<String>) -> Vec<Record> {
    let Some(root) = root() else { return Vec::new(); };
    let meta_dir = root.join(".meta");
    if owned_dir(&root).is_none() || owned_dir(&meta_dir).is_none() { return Vec::new(); }
    let Ok(entries) = fs::read_dir(meta_dir) else { return Vec::new(); };
    let mut rows = Vec::new();
    for entry in entries.flatten().take(1000) {
        let path = entry.path();
        if path.extension().is_none_or(|ext| ext != "json") { continue; }
        let Some(stem) = path.file_stem() else { continue; };
        let target = root.join(stem);
        let Some((record, _, _, _)) = read_record(&target) else { continue; };
        if !live_ids.contains(&record.session_id) { rows.push(record); }
    }
    rows.sort_by(|a, b| a.path.cmp(&b.path));
    rows
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OrphanState {
    Ready { head_oid: String },
    Dirty,
    UniqueCommits,
    Uncertain,
}

#[derive(Clone, Debug)]
pub struct OrphanPreview {
    pub record: Record,
    pub state: OrphanState,
}

fn orphan_state(record: &Record, base: &str, main: &Path, base_oid: Option<&str>) -> OrphanState {
    // Python 1.19 does not write base_oid or hold the Rust advisory lock.
    // Its daemon may be starting before registry publication, so those
    // sidecars are survey-only and never eligible for automated cleanup.
    let Some(base_oid) = base_oid else { return OrphanState::Uncertain; };
    if worktree_for_branch(main, &record.branch).as_ref() != Some(&record.path)
        || git_text(&record.path, &["symbolic-ref", "--quiet", "--short", "HEAD"])
            .as_deref() != Some(record.branch.as_str()) {
        return OrphanState::Uncertain;
    }
    if !git(main, &["merge-base", "--is-ancestor", base_oid, &record.branch], Duration::from_secs(10))
        .is_some_and(|(ok, _)| ok) { return OrphanState::Uncertain; }
    let Some(status) = git_text(&record.path, &["status", "--porcelain", "--ignored", "--untracked-files=all"])
        else { return OrphanState::Uncertain; };
    if !status.is_empty() { return OrphanState::Dirty; }
    let spec = format!("{base}..{}", record.branch);
    match git_text(&record.path, &["rev-list", "--count", &spec]).as_deref() {
        Some("0") => {},
        Some(value) if value.parse::<u64>().is_ok_and(|n| n > 0) => return OrphanState::UniqueCommits,
        _ => return OrphanState::Uncertain,
    }
    match git_text(main, &["rev-parse", "--verify", &record.branch]) {
        Some(head_oid) if valid_commit_oid(&head_oid) => OrphanState::Ready { head_oid },
        _ => OrphanState::Uncertain,
    }
}

/// Preview verified local orphans. Only `Ready` entries are eligible for
/// removal; dirty or uniquely committed trees remain visible for recovery.
pub fn preview_orphans(live_ids: &HashSet<String>) -> Vec<OrphanPreview> {
    list_orphans(live_ids).into_iter().map(|record| {
        let state = match read_record(&record.path) {
            Some((current, base, main, base_oid)) if current.branch == record.branch
                && current.session_id == record.session_id => orphan_state(&record, &base, &main, base_oid.as_deref()),
            _ => OrphanState::Uncertain,
        };
        OrphanPreview { record, state }
    }).collect()
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CleanupResult {
    Removed,
    Kept(String),
}

/// Explicitly remove one previously previewed clean orphan. The caller must
/// provide a fresh live-session reader: registry failures refuse cleanup.
/// Both the previewed branch OID and all ownership/status checks are repeated
/// while holding the same lock as a Rust session daemon.
pub fn cleanup_orphan(
    preview: &OrphanPreview,
    live_ids: impl Fn() -> Option<HashSet<String>>,
) -> CleanupResult {
    let OrphanState::Ready { head_oid } = &preview.state else {
        return CleanupResult::Kept("orphan was not previewed as clean".into());
    };
    let Some(live) = live_ids() else {
        return CleanupResult::Kept("live sessions could not be verified".into());
    };
    if live.contains(&preview.record.session_id) {
        return CleanupResult::Kept("session is live".into());
    }
    let Some(_lock) = lock_worktree(&preview.record.path) else {
        return CleanupResult::Kept("worktree is locked or its lock is untrusted".into());
    };
    // A process can start or replace metadata between preview and lock.
    let Some((record, base, main, base_oid)) = read_record(&preview.record.path) else {
        return CleanupResult::Kept("worktree ownership could not be verified".into());
    };
    if record.path != preview.record.path || record.branch != preview.record.branch
        || record.session_id != preview.record.session_id {
        return CleanupResult::Kept("worktree identity changed".into());
    }
    if !matches!(orphan_state(&record, &base, &main, base_oid.as_deref()), OrphanState::Ready { head_oid: current } if current == *head_oid) {
        return CleanupResult::Kept("worktree changed since preview".into());
    }
    let Some(live) = live_ids() else {
        return CleanupResult::Kept("live sessions could not be verified".into());
    };
    if live.contains(&record.session_id) {
        return CleanupResult::Kept("session became live".into());
    }
    let note = finalize_locked(&record.path);
    if note.is_empty() { CleanupResult::Removed } else { CleanupResult::Kept(note) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // These fixtures set DOXA_HOME/DOXA_WORKTREE for the whole test process.
    static TEST_ENV_LOCK: Mutex<()> = Mutex::new(());

    fn run_git(cwd: &Path, args: &[&str]) {
        let result = Command::new("git").args(args).current_dir(cwd).output().unwrap();
        assert!(result.status.success(), "git {:?}: {}", args, String::from_utf8_lossy(&result.stderr));
    }
    #[test]
    fn creation_requires_lifecycle_lock_before_creating_a_branch_or_sidecar() {
        let _serial = TEST_ENV_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        let dir = tempfile::tempdir().unwrap();
        env::set_var("DOXA_HOME", dir.path().join("home"));
        env::set_var("DOXA_WORKTREE", "1");
        let main = dir.path().join("repo");
        fs::create_dir(&main).unwrap();
        run_git(&main, &["init", "-q", "-b", "main"]);
        fs::write(main.join("file"), "base\n").unwrap();
        run_git(&main, &["add", "file"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: base"]);
        let worktrees = ensure_owned_dir(&root().unwrap()).unwrap();
        ensure_owned_dir(&worktrees.join(".meta")).unwrap();
        let path = worktrees.join("repo-lock0001");
        let held = lock_worktree(&path).unwrap();
        assert!(create(&main, "lock0001session").is_none());
        assert!(!path.exists());
        assert!(meta_path(&path).is_some_and(|meta| !meta.exists()));
        assert!(git_text(&main, &["show-ref", "--verify", "refs/heads/doxa/lock0001"]).is_none());
        drop(held);
        let mut created = create(&main, "lock0001session").unwrap();
        assert_eq!(created.path(), path);
        assert!(created.finish().is_empty());
    }

    #[test]
    fn legacy_sidecar_cannot_be_adopted_or_finalized_even_when_clean() {
        let _serial = TEST_ENV_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        let dir = tempfile::tempdir().unwrap();
        env::set_var("DOXA_HOME", dir.path().join("home"));
        env::set_var("DOXA_WORKTREE", "1");
        let main = dir.path().join("repo");
        fs::create_dir(&main).unwrap();
        run_git(&main, &["init", "-q", "-b", "main"]);
        fs::write(main.join("file"), "base\n").unwrap();
        run_git(&main, &["add", "file"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-qm", "test: base"]);

        let mut managed = create(&main, "legacy01session").unwrap();
        let path = managed.path().to_path_buf();
        let sidecar = meta_path(&path).unwrap();
        let mut data: serde_json::Value = serde_json::from_slice(&fs::read(&sidecar).unwrap()).unwrap();
        data.as_object_mut().unwrap().remove("base_oid");
        fs::write(&sidecar, serde_json::to_vec(&data).unwrap()).unwrap();

        // A Python 1.19 sidecar can be clean and its Rust lock can be free;
        // neither fact proves the uncoordinated Python owner has exited.
        assert!(finalize(&path).contains("lock unavailable"));
        assert!(managed.finish().contains("legacy worktree owner cannot be verified"));
        assert!(path.exists());
        assert!(create(&main, "legacy01session").is_none());
        assert!(switch_base(&path, "main").unwrap_err().contains("legacy worktree owner"));
        assert!(finalize(&path).contains("legacy worktree owner cannot be verified"));
        assert!(path.exists());
        assert!(sidecar.exists());
        assert!(git_text(&main, &["show-ref", "--verify", "refs/heads/doxa/legacy01"]).is_some());

        let preview = preview_orphans(&HashSet::new()).into_iter()
            .find(|row| row.record.path == path).unwrap();
        assert_eq!(preview.state, OrphanState::Uncertain);
        assert!(matches!(cleanup_orphan(&preview, || Some(HashSet::new())), CleanupResult::Kept(_)));
        env::remove_var("DOXA_HOME");
        env::remove_var("DOXA_WORKTREE");
    }
    #[test]
    fn recovers_deleted_checkout_from_pinned_sidecar_and_keeps_unique_commits() {
        let _serial = TEST_ENV_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        let dir = tempfile::tempdir().unwrap();
        env::set_var("DOXA_HOME", dir.path().join("home"));
        env::set_var("DOXA_WORKTREE", "1");
        let main = dir.path().join("repo");
        fs::create_dir(&main).unwrap();
        run_git(&main, &["init", "-q", "-b", "main"]);
        fs::write(main.join("file"), "base\n").unwrap();
        run_git(&main, &["add", "file"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: base"]);
        let mut tree = create(&main, "recover01").unwrap();
        let path = tree.path().to_path_buf();
        fs::write(path.join("file"), "unique commit\n").unwrap();
        run_git(&path, &["add", "file"]);
        run_git(&path, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: keep"]);
        let unique_oid = git_text(&path, &["rev-parse", "HEAD"]).unwrap();
        assert!(tree.finish().contains("kept"));
        fs::remove_dir_all(&path).unwrap();
        assert!(!path.exists());
        let recovered = recover_missing(&path, "recover01").unwrap();
        assert_eq!(recovered.path(), path);
        assert_eq!(git_text(&path, &["rev-parse", "HEAD"]).as_deref(), Some(unique_oid.as_str()));
        assert_eq!(fs::read_to_string(path.join("file")).unwrap(), "unique commit\n");
        drop(recovered);
        assert!(path.exists());
        assert!(recover_missing(&path, "recover01").unwrap_err().contains("already exists"));
        fs::remove_dir_all(&path).unwrap();
        assert!(recover_missing(&path, "wrong-id").unwrap_err().contains("identity"));
        let sidecar = meta_path(&path).unwrap();
        let original = fs::read(&sidecar).unwrap();
        let mut data: serde_json::Value = serde_json::from_slice(&original).unwrap();
        data.as_object_mut().unwrap().remove("base_oid");
        fs::write(&sidecar, serde_json::to_vec(&data).unwrap()).unwrap();
        assert!(recover_missing(&path, "recover01").unwrap_err().contains("pinned base"));
        fs::write(&sidecar, &original).unwrap();
        let blocker = path.clone();
        std::os::unix::fs::symlink(main.join("file"), &blocker).unwrap();
        assert!(recover_missing(&path, "recover01").unwrap_err().contains("already exists"));
        assert!(blocker.is_symlink());
        fs::remove_file(&blocker).unwrap();
        // A registered branch at a different path cannot be seized.
        run_git(&main, &["worktree", "prune", "--expire=now"]);
        let elsewhere = dir.path().join("elsewhere");
        run_git(&main, &["worktree", "add", "-q", elsewhere.to_str().unwrap(), "doxa/recover0"]);
        assert!(recover_missing(&path, "recover01").unwrap_err().contains("another checkout"));
        assert_eq!(git_text(&elsewhere, &["rev-parse", "HEAD"]).as_deref(), Some(unique_oid.as_str()));
        env::remove_var("DOXA_HOME");
        env::remove_var("DOXA_WORKTREE");
    }
    #[test]
    fn live_switch_updates_base_and_preserves_dirty_or_unique_work() {
        let _serial = TEST_ENV_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        let dir = tempfile::tempdir().unwrap();
        env::set_var("DOXA_HOME", dir.path().join("home"));
        env::set_var("DOXA_WORKTREE", "1");
        let main = dir.path().join("repo");
        fs::create_dir(&main).unwrap();
        run_git(&main, &["init", "-q", "-b", "main"]);
        fs::write(main.join("file"), "main\n").unwrap();
        run_git(&main, &["add", "file"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: base"]);
        run_git(&main, &["checkout", "-qb", "feature"]);
        fs::write(main.join("file"), "feature\n").unwrap();
        run_git(&main, &["add", "file"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: feature"]);
        let feature_oid = git_text(&main, &["rev-parse", "HEAD"]).unwrap();
        run_git(&main, &["checkout", "-q", "main"]);
        let main_oid = git_text(&main, &["rev-parse", "HEAD"]).unwrap();
        assert_eq!(repo_status(dir.path()), Some(RepoStatus::Directory { name: dir.path().file_name().unwrap().to_string_lossy().into_owned() }));
        assert_eq!(repo_status(&main), Some(RepoStatus::Repository {
            repo: "repo".into(), base: Some("main".into()), checked_out: Some("main".into()),
            sha: Some(main_oid[..7].into()), worktree: None,
        }));
        assert!(switch_base(&main, "feature").unwrap_err().contains("no verified"));
        let mut tree = create(&main, "switch001").unwrap();
        let path = tree.path().to_path_buf();
        assert_eq!(repo_status(&path), Some(RepoStatus::Repository {
            repo: "repo".into(), base: Some("main".into()), checked_out: Some("doxa/switch00".into()),
            sha: Some(main_oid[..7].into()), worktree: Some("doxa/switch00".into()),
        }));
        assert!(switch_base(&path, "doxa/switch00").unwrap_err().contains("own branch"));
        assert!(switch_base(&path, "missing").unwrap_err().contains("no such"));
        let metadata_path = meta_path(&path).unwrap();
        let mut stale: serde_json::Value = serde_json::from_slice(&fs::read(&metadata_path).unwrap()).unwrap();
        stale["base_oid"] = serde_json::json!(feature_oid);
        fs::write(&metadata_path, serde_json::to_vec(&stale).unwrap()).unwrap();
        assert!(switch_base(&path, "feature").unwrap_err().contains("pinned base"));
        assert_eq!(git_text(&path, &["rev-parse", "HEAD"]).as_deref(), Some(main_oid.as_str()));
        stale["base_oid"] = serde_json::json!(main_oid);
        fs::write(&metadata_path, serde_json::to_vec(&stale).unwrap()).unwrap();
        assert!(switch_base(&path, "feature").unwrap().contains("now based"));
        assert_eq!(repo_status(&path), Some(RepoStatus::Repository {
            repo: "repo".into(), base: Some("feature".into()), checked_out: Some("doxa/switch00".into()),
            sha: Some(feature_oid[..7].into()), worktree: Some("doxa/switch00".into()),
        }));
        assert_eq!(git_text(&path, &["rev-parse", "HEAD"]).as_deref(), Some(feature_oid.as_str()));
        assert_eq!(git_text(&main, &["rev-parse", "HEAD"]).as_deref(), Some(main_oid.as_str()));
        let data: serde_json::Value = serde_json::from_slice(&fs::read(meta_path(&path).unwrap()).unwrap()).unwrap();
        assert_eq!(data["base_ref"], "feature");
        assert_eq!(data["base_oid"], feature_oid);
        fs::write(path.join("scratch"), "dirty").unwrap();
        assert!(switch_base(&path, "main").unwrap_err().contains("uncommitted"));
        fs::remove_file(path.join("scratch")).unwrap();
        fs::write(path.join("file"), "own commit\n").unwrap();
        run_git(&path, &["add", "file"]);
        run_git(&path, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: own"]);
        let own_oid = git_text(&path, &["rev-parse", "HEAD"]).unwrap();
        assert!(switch_base(&path, "main").unwrap_err().contains("ahead"));
        assert_eq!(git_text(&path, &["rev-parse", "HEAD"]).as_deref(), Some(own_oid.as_str()));
        assert!(tree.finish().contains("kept"));
    }
    #[test]
    fn switch_does_not_replay_old_base_commits_when_base_advanced() {
        let _serial = TEST_ENV_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        let dir = tempfile::tempdir().unwrap();
        env::set_var("DOXA_HOME", dir.path().join("home"));
        env::set_var("DOXA_WORKTREE", "1");
        let main = dir.path().join("repo");
        fs::create_dir(&main).unwrap();
        run_git(&main, &["init", "-q", "-b", "main"]);
        fs::write(main.join("file"), "A\n").unwrap();
        run_git(&main, &["add", "file"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: A"]);
        let a = git_text(&main, &["rev-parse", "HEAD"]).unwrap();
        fs::write(main.join("file"), "B\n").unwrap();
        run_git(&main, &["add", "file"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: B"]);
        let mut tree = create(&main, "baseadv1").unwrap();
        let path = tree.path().to_path_buf();
        fs::write(main.join("file"), "C\n").unwrap();
        run_git(&main, &["add", "file"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: C"]);
        run_git(&main, &["branch", "feature", &a]);
        run_git(&main, &["checkout", "-q", "feature"]);
        fs::write(main.join("file"), "X\n").unwrap();
        run_git(&main, &["add", "file"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: X"]);
        let x = git_text(&main, &["rev-parse", "HEAD"]).unwrap();
        run_git(&main, &["checkout", "-q", "main"]);
        assert!(switch_base(&path, "feature").unwrap().contains("now based"));
        assert_eq!(git_text(&path, &["rev-parse", "HEAD"]).as_deref(), Some(x.as_str()));
        assert_eq!(fs::read_to_string(path.join("file")).unwrap(), "X\n");
        assert_eq!(git_text(&path, &["status", "--porcelain"]).unwrap(), "");
        assert!(tree.finish().is_empty());
    }
    #[test]
    fn switch_refuses_ignored_files_that_target_would_overwrite() {
        let _serial = TEST_ENV_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        let dir = tempfile::tempdir().unwrap();
        env::set_var("DOXA_HOME", dir.path().join("home"));
        env::set_var("DOXA_WORKTREE", "1");
        let main = dir.path().join("repo");
        fs::create_dir(&main).unwrap();
        run_git(&main, &["init", "-q", "-b", "main"]);
        fs::write(main.join(".gitignore"), "cache/\n").unwrap();
        run_git(&main, &["add", ".gitignore"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: ignore cache"]);
        let mut tree = create(&main, "ignored1").unwrap();
        let path = tree.path().to_path_buf();
        fs::create_dir(path.join("cache")).unwrap();
        fs::write(path.join("cache/user.txt"), "private work\n").unwrap();
        run_git(&main, &["checkout", "-qb", "feature"]);
        fs::create_dir(main.join("cache")).unwrap();
        fs::write(main.join("cache/user.txt"), "tracked target\n").unwrap();
        run_git(&main, &["add", "-f", "cache/user.txt"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: track cache"]);
        assert!(switch_base(&path, "feature").unwrap_err().contains("uncommitted"));
        assert_eq!(fs::read_to_string(path.join("cache/user.txt")).unwrap(), "private work\n");
        // Finalization's ignored-file rule is covered by orphan cleanup.
        let _ = tree.finish();
    }
    #[test]
    fn clean_tree_is_removed_but_dirty_and_committed_work_are_kept() {
        let _serial = TEST_ENV_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("home");
        env::set_var("DOXA_HOME", &home);
        env::set_var("DOXA_WORKTREE", "1");
        let main = dir.path().join("repo");
        fs::create_dir(&main).unwrap();
        run_git(&main, &["init", "-q", "-b", "main"]);
        fs::write(main.join("file.txt"), "base\n").unwrap();
        run_git(&main, &["add", "file.txt"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: baseline"]);
        run_git(&main, &["checkout", "-qb", "feature"]);
        fs::write(main.join("file.txt"), "feature\n").unwrap();
        run_git(&main, &["add", "file.txt"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: feature"]);
        run_git(&main, &["update-ref", "refs/remotes/origin/feature", "HEAD"]);
        run_git(&main, &["update-ref", "refs/remotes/origin/remote-only", "HEAD"]);
        run_git(&main, &["checkout", "-q", "main"]);
        let shared_head = git_text(&main, &["rev-parse", "HEAD"]).unwrap();
        let shared_status = git_text(&main, &["status", "--porcelain"]).unwrap();
        let status = branch_status(&main).unwrap();
        assert_eq!(status.base.as_deref(), Some("main"));
        assert_eq!(status.checked_out.as_deref(), Some("main"));
        assert_eq!(status.branches, vec!["feature", "main"]);
        assert_eq!(git_text(&main, &["rev-parse", "HEAD"]).unwrap(), shared_head);
        assert_eq!(git_text(&main, &["status", "--porcelain"]).unwrap(), shared_status);
        assert_eq!(resolve_base(&main, "origin/feature").as_deref(), Some("feature"));
        assert_eq!(resolve_base(&main, "origin/remote-only").as_deref(), Some("origin/remote-only"));
        assert!(resolve_base(&main, "--output=/tmp/unsafe").is_none());
        assert!(resolve_base(&main, "feature^{}").is_none());
        assert!(resolve_base(&main, "feature/unknown").is_none());
        assert!(resolve_base(&main, "refs/heads/main").is_none());
        assert!(resolve_base(&main, "missing").is_none());
        run_git(&main, &["branch", "origin/feature"]);
        assert_eq!(resolve_base(&main, "origin/feature").as_deref(), Some("origin/feature"));
        run_git(&main, &["branch", "-D", "origin/feature"]);
        let mut chosen = create_from(&main, "g1b2c3d4chosen", Some("origin/feature")).unwrap();
        assert_eq!(fs::read_to_string(chosen.path().join("file.txt")).unwrap(), "feature\n");
        assert_eq!(read_record(chosen.path()).unwrap().1, "feature");
        let status = branch_status(chosen.path()).unwrap();
        assert_eq!(status.base.as_deref(), Some("feature"));
        assert_eq!(status.checked_out.as_deref(), Some("doxa/g1b2c3d4"));
        assert!(status.branches.contains(&"feature".into()));
        assert!(!status.branches.contains(&"doxa/g1b2c3d4".into()));
        assert_eq!(git_text(&main, &["rev-parse", "HEAD"]).unwrap(), shared_head);
        assert_eq!(git_text(&main, &["status", "--porcelain"]).unwrap(), shared_status);
        assert!(chosen.finish().is_empty());

        let mut clean = create(&main, "a1b2c3d4clean").unwrap();
        let clean_path = clean.path().to_path_buf();
        assert!(clean_path.join("file.txt").exists());
        let recorded = meta_path(&clean_path).unwrap();
        let initial_oid = git_text(&clean_path, &["rev-parse", "HEAD"]).unwrap();
        let metadata: serde_json::Value = serde_json::from_slice(&fs::read(&recorded).unwrap()).unwrap();
        assert_eq!(metadata["base_ref"], "main");
        assert_eq!(metadata["base_oid"], initial_oid);
        fs::write(main.join("file.txt"), "advanced base\n").unwrap();
        run_git(&main, &["add", "file.txt"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: advance base"]);
        assert_ne!(git_text(&main, &["rev-parse", "main"]).unwrap(), initial_oid);
        let metadata_after: serde_json::Value = serde_json::from_slice(&fs::read(&recorded).unwrap()).unwrap();
        assert_eq!(metadata_after["base_oid"], initial_oid);
        assert_eq!(git_text(&clean_path, &["rev-parse", "HEAD"]).unwrap(), initial_oid);
        assert_eq!(list_orphans(&HashSet::new()).len(), 1);
        assert!(list_orphans(&HashSet::from(["a1b2c3d4clean".into()])).is_empty());
        assert!(clean.finish().is_empty());
        assert!(!clean_path.exists());
        assert!(git_text(&main, &["show-ref", "--verify", "refs/heads/doxa/a1b2c3d4"]).is_none());

        let mut dirty = create(&main, "b1b2c3d4dirty").unwrap();
        let dirty_path = dirty.path().to_path_buf();
        fs::write(dirty_path.join("untracked.txt"), "user work").unwrap();
        assert!(dirty.finish().contains("kept doxa/b1b2c3d4"));
        assert!(dirty_path.join("untracked.txt").exists());
        let reused = create(&main, "b1b2c3d4dirty").unwrap();
        assert_eq!(reused.path(), dirty_path);
        drop(reused); // a restarted daemon must not delete another instance's tree
        let resumed = create(&dirty_path, "b1b2c3d4dirty").unwrap();
        assert_eq!(resumed.path(), dirty_path);
        drop(resumed); // archived cwd is the managed tree itself
        assert!(dirty_path.join("untracked.txt").exists());
        assert!(create_from(&main, "b1b2c3d4dirty", Some("feature")).is_none());
        assert_eq!(read_record(&dirty_path).unwrap().1, "main");
        let original = git_text(&dirty_path, &["rev-parse", "HEAD"]).unwrap();
        let tree = git_text(&main, &["rev-parse", "HEAD^{tree}"]).unwrap();
        let unrelated = git_text(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit-tree", &tree, "-m", "unrelated history"]).unwrap();
        run_git(&main, &["update-ref", "refs/heads/doxa/b1b2c3d4", &unrelated]);
        assert!(create(&dirty_path, "b1b2c3d4dirty").is_none());
        run_git(&main, &["update-ref", "refs/heads/doxa/b1b2c3d4", &original]);
        assert!(dirty_path.join("untracked.txt").exists());

        let mut switched = create(&main, "e1b2c3d4switched").unwrap();
        let switched_path = switched.path().to_path_buf();
        run_git(&switched_path, &["checkout", "--detach", "main"]);
        assert!(switched.finish().contains("kept doxa/e1b2c3d4"));
        assert!(switched_path.exists());

        let mut committed = create(&main, "c1b2c3d4committed").unwrap();
        let committed_path = committed.path().to_path_buf();
        fs::write(committed_path.join("file.txt"), "commit\n").unwrap();
        run_git(&committed_path, &["add", "file.txt"]);
        run_git(&committed_path, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: work"]);
        run_git(&committed_path, &["checkout", "--detach", "main"]);
        assert!(committed.finish().contains("kept doxa/c1b2c3d4"));
        assert!(committed_path.exists());
        assert!(git_text(&main, &["show-ref", "--verify", "refs/heads/doxa/c1b2c3d4"]).is_some());

        let mut corrupt = create(&main, "d1b2c3d4corrupt").unwrap();
        let corrupt_path = corrupt.path().to_path_buf();
        fs::write(meta_path(&corrupt_path).unwrap(), "{broken").unwrap();
        assert!(corrupt.finish().contains("ownership could not be verified"));
        assert!(corrupt_path.exists());
        assert!(!list_orphans(&HashSet::new()).iter().any(|row| row.path == corrupt_path));
        env::set_var("DOXA_WORKTREE", "0");
        assert!(create(&main, "f1b2c3d4disabled").is_none());
        env::remove_var("DOXA_WORKTREE");
        fs::write(home.join("config.toml"), "worktree_per_session = false\n").unwrap();
        assert!(!enabled());
        env::set_var("DOXA_WORKTREE", "1");
        assert!(enabled());
        let victim = dir.path().join("victim");
        fs::create_dir(&victim).unwrap();
        fs::set_permissions(&victim, fs::Permissions::from_mode(0o755)).unwrap();
        let fake_home = dir.path().join("symlink-home");
        fs::create_dir(&fake_home).unwrap();
        std::os::unix::fs::symlink(&victim, fake_home.join("worktrees")).unwrap();
        env::set_var("DOXA_HOME", &fake_home);
        assert!(create(&main, "f1b2c3d4symlink").is_none());
        assert_eq!(fs::metadata(&victim).unwrap().permissions().mode() & 0o777, 0o755);
        env::set_var("DOXA_HOME", &home);

        run_git(&main, &["branch", "race"]);
        let old_oid = git_text(&main, &["rev-parse", "race"]).unwrap();
        fs::write(main.join("file.txt"), "new base\n").unwrap();
        run_git(&main, &["add", "file.txt"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: move branch"]);
        run_git(&main, &["update-ref", "refs/heads/race", "HEAD"]);
        assert!(!delete_if_unchanged(&main, "race", &old_oid));
        assert!(git_text(&main, &["rev-parse", "race"]).is_some());

        let mut orphan = create(&main, "h1b2c3d4orphan").unwrap();
        let orphan_path = orphan.path().to_path_buf();
        fs::write(orphan_path.join("scratch"), "keep").unwrap();
        assert!(orphan.finish().contains("kept"));
        fs::remove_file(orphan_path.join("scratch")).unwrap();
        let preview = preview_orphans(&HashSet::new()).into_iter()
            .find(|row| row.record.path == orphan_path).unwrap();
        assert!(matches!(preview.state, OrphanState::Ready { .. }));
        assert!(matches!(cleanup_orphan(&preview, || Some(HashSet::from(["h1b2c3d4orphan".into()]))), CleanupResult::Kept(_)));
        assert!(orphan_path.exists());
        let checks = std::cell::Cell::new(0);
        assert!(matches!(cleanup_orphan(&preview, || {
            checks.set(checks.get() + 1);
            Some(if checks.get() == 2 { HashSet::from(["h1b2c3d4orphan".into()]) } else { HashSet::new() })
        }), CleanupResult::Kept(_)));
        assert_eq!(checks.get(), 2);
        assert!(orphan_path.exists());
        assert!(matches!(cleanup_orphan(&preview, || None), CleanupResult::Kept(_)));
        let sidecar = meta_path(&orphan_path).unwrap();
        let original_sidecar = fs::read(&sidecar).unwrap();
        let mut stale: serde_json::Value = serde_json::from_slice(&original_sidecar).unwrap();
        stale["base_oid"] = serde_json::Value::String(git_text(&main, &["rev-parse", "feature"]).unwrap());
        fs::write(&sidecar, serde_json::to_vec(&stale).unwrap()).unwrap();
        let stale_preview = preview_orphans(&HashSet::new()).into_iter()
            .find(|row| row.record.path == orphan_path).unwrap();
        assert_eq!(stale_preview.state, OrphanState::Uncertain);
        assert!(matches!(cleanup_orphan(&preview, || Some(HashSet::new())), CleanupResult::Kept(_)));
        fs::write(&sidecar, &original_sidecar).unwrap();
        let mut legacy: serde_json::Value = serde_json::from_slice(&fs::read(&sidecar).unwrap()).unwrap();
        legacy.as_object_mut().unwrap().remove("base_oid");
        fs::write(&sidecar, serde_json::to_vec(&legacy).unwrap()).unwrap();
        let legacy_preview = preview_orphans(&HashSet::new()).into_iter()
            .find(|row| row.record.path == orphan_path).unwrap();
        assert_eq!(legacy_preview.state, OrphanState::Uncertain);
        assert!(matches!(cleanup_orphan(&preview, || Some(HashSet::new())), CleanupResult::Kept(_)));
        fs::write(&sidecar, original_sidecar).unwrap();
        assert_eq!(cleanup_orphan(&preview, || Some(HashSet::new())), CleanupResult::Removed);
        assert!(!orphan_path.exists());
        assert!(git_text(&main, &["show-ref", "--verify", "refs/heads/doxa/h1b2c3d4"]).is_none());

        let mut locked = create(&main, "i1b2c3d4locked").unwrap();
        let locked_path = locked.path().to_path_buf();
        let preview = preview_orphans(&HashSet::new()).into_iter()
            .find(|row| row.record.path == locked_path).unwrap();
        assert!(matches!(cleanup_orphan(&preview, || Some(HashSet::new())), CleanupResult::Kept(_)));
        assert!(locked_path.exists());
        assert!(locked.finish().is_empty());

        let mut ahead = create(&main, "j1b2c3d4ahead").unwrap();
        let ahead_path = ahead.path().to_path_buf();
        fs::write(ahead_path.join("file.txt"), "new work\n").unwrap();
        run_git(&ahead_path, &["add", "file.txt"]);
        run_git(&ahead_path, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: retained work"]);
        assert!(ahead.finish().contains("kept"));
        let preview = preview_orphans(&HashSet::new()).into_iter()
            .find(|row| row.record.path == ahead_path).unwrap();
        assert_eq!(preview.state, OrphanState::UniqueCommits);
        assert!(matches!(cleanup_orphan(&preview, || Some(HashSet::new())), CleanupResult::Kept(_)));
        assert!(ahead_path.exists());

        let mut race_tree = create(&main, "k1b2c3d4race").unwrap();
        let race_path = race_tree.path().to_path_buf();
        fs::write(race_path.join("scratch"), "keep").unwrap();
        assert!(race_tree.finish().contains("kept"));
        fs::remove_file(race_path.join("scratch")).unwrap();
        let preview = preview_orphans(&HashSet::new()).into_iter()
            .find(|row| row.record.path == race_path).unwrap();
        fs::write(race_path.join("new"), "changed since preview").unwrap();
        assert!(matches!(cleanup_orphan(&preview, || Some(HashSet::new())), CleanupResult::Kept(_)));
        assert!(race_path.exists());

        fs::write(main.join(".gitignore"), "cache/\n").unwrap();
        run_git(&main, &["add", ".gitignore"]);
        run_git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: ignore cache"]);
        let mut ignored = create(&main, "l1b2c3d4ignored").unwrap();
        let ignored_path = ignored.path().to_path_buf();
        fs::create_dir(ignored_path.join("cache")).unwrap();
        fs::write(ignored_path.join("cache/user.txt"), "valuable ignored data\n").unwrap();
        let preview = preview_orphans(&HashSet::new()).into_iter()
            .find(|row| row.record.path == ignored_path).unwrap();
        assert_eq!(preview.state, OrphanState::Dirty);
        assert!(ignored.finish().contains("kept"));
        assert_eq!(fs::read_to_string(ignored_path.join("cache/user.txt")).unwrap(), "valuable ignored data\n");

        env::remove_var("DOXA_HOME");
        env::remove_var("DOXA_WORKTREE");
    }
}
