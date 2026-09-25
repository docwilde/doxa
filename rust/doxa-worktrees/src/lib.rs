//! Session-owned Git worktrees compatible with Python DOXA 1.19 sidecars.
//! Every uncertain cleanup decision keeps the branch and directory.
use std::collections::HashSet;
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
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

pub struct Managed {
    path: PathBuf,
    created: bool,
    finished: bool,
}
impl Managed {
    pub fn path(&self) -> &Path { &self.path }
    pub fn finish(&mut self) -> String {
        if self.finished || !self.created { return String::new(); }
        self.finished = true;
        finalize(&self.path)
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
    fs::create_dir_all(path).ok()?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700)).ok()?;
    owned_dir(path)?;
    path.canonicalize().ok()
}
fn meta_path(path: &Path) -> Option<PathBuf> {
    Some(root()?.join(".meta").join(format!("{}.json", path.file_name()?.to_str()?)))
}
fn read_record(path: &Path) -> Option<(Record, String, PathBuf)> {
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
    let main = PathBuf::from(data.get("main_root")?.as_str()?);
    let root = root()?.canonicalize().ok()?;
    let canonical = path.canonicalize().ok()?;
    if canonical.parent()? != root || branch != format!("doxa/{}", short_id(&id))
        || !safe_ref(&base) || base == branch || main_root(&canonical)? != main.canonicalize().ok()?
        || path.file_name()?.to_str()? != format!("{}-{}", main.file_name()?.to_str()?, short_id(&id)) {
        return None;
    }
    Some((Record { path: canonical, branch, session_id: id }, base, main))
}
fn write_record(path: &Path, main: &Path, branch: &str, base: &str, id: &str) -> Option<()> {
    let target = meta_path(path)?;
    let parent = target.parent()?;
    ensure_owned_dir(parent)?;
    if fs::symlink_metadata(&target).is_ok() { return None; }
    let stamp = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_nanos();
    let temp = parent.join(format!(".{}.{}.tmp", id, stamp));
    let mut file = OpenOptions::new().write(true).create_new(true).mode(0o600).open(&temp).ok()?;
    let result = (|| {
        serde_json::to_writer(&mut file, &serde_json::json!({
            "main_root": main, "branch": branch, "base_ref": base, "session_id": id
        })).ok()?;
        file.flush().ok()?;
        file.sync_all().ok()?;
        fs::hard_link(&temp, &target).ok()?;
        Some(())
    })();
    let _ = fs::remove_file(temp);
    result
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

/// Create a linked checkout for a session. None means run in the original cwd.
/// Existing worktrees are reused only with an exact matching sidecar, and
/// are not automatically finalized by the new daemon instance.
pub fn create(cwd: &Path, id: &str) -> Option<Managed> {
    if !enabled() { return None; }
    let main = main_root(cwd)?;
    let base = base_ref(cwd)?;
    let short = short_id(id);
    let branch = format!("doxa/{short}");
    let repo = main.file_name()?.to_str()?;
    let worktrees = ensure_owned_dir(&root()?)?;
    let path = worktrees.join(format!("{repo}-{short}"));
    if let Some(existing) = worktree_for_branch(&main, &branch) {
        let (record, _, _) = read_record(&existing)?;
        if record.path != existing.canonicalize().ok()? || record.session_id != id || record.path != path {
            return None;
        }
        return Some(Managed { path: record.path, created: false, finished: false });
    }
    if fs::symlink_metadata(&path).is_ok() || meta_path(&path).is_some_and(|p| fs::symlink_metadata(p).is_ok()) {
        return None;
    }
    let path_str = path.to_str()?;
    if !git(&main, &["worktree", "add", "-q", "-b", &branch, path_str, &base], Duration::from_secs(30))?.0 {
        return None;
    }
    let path = path.canonicalize().ok()?;
    if write_record(&path, &main, &branch, &base, id).is_none() {
        eprintln!("doxa-daemon: worktree metadata unavailable; keeping {}", path.display());
        return Some(Managed { path, created: false, finished: false });
    }
    Some(Managed { path, created: true, finished: false })
}

/// Remove only a verified, clean managed worktree with no unique commits.
/// Any uncertainty yields a keep message and leaves user data untouched.
pub fn finalize(path: &Path) -> String {
    let Some((record, base, main)) = read_record(path) else {
        return "worktree ownership could not be verified; kept it".into();
    };
    let keep = || format!("kept {} at {} — merge when ready", record.branch, record.path.display());
    if worktree_for_branch(&main, &record.branch).as_ref() != Some(&record.path) { return keep(); }
    // A user may have checked out another branch or detached HEAD. Its
    // clean checkout is still theirs; never remove it on this sidecar's say.
    if git_text(&record.path, &["symbolic-ref", "--quiet", "--short", "HEAD"])
        .as_deref() != Some(record.branch.as_str()) { return keep(); }
    let Some(status) = git_text(&record.path, &["status", "--porcelain", "--untracked-files=all"]) else { return keep(); };
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
    if !git(&main, &["branch", "-D", &record.branch], Duration::from_secs(10)).is_some_and(|(ok, _)| ok) {
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
        let Some((record, _, _)) = read_record(&target) else { continue; };
        if !live_ids.contains(&record.session_id) { rows.push(record); }
    }
    rows.sort_by(|a, b| a.path.cmp(&b.path));
    rows
}

#[cfg(test)]
mod tests {
    use super::*;
    fn run_git(cwd: &Path, args: &[&str]) {
        let result = Command::new("git").args(args).current_dir(cwd).output().unwrap();
        assert!(result.status.success(), "git {:?}: {}", args, String::from_utf8_lossy(&result.stderr));
    }
    #[test]
    fn clean_tree_is_removed_but_dirty_and_committed_work_are_kept() {
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

        let mut clean = create(&main, "a1b2c3d4clean").unwrap();
        let clean_path = clean.path().to_path_buf();
        assert!(clean_path.join("file.txt").exists());
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
        env::remove_var("DOXA_HOME");
        env::remove_var("DOXA_WORKTREE");
    }
}
