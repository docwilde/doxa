//! Stable, human-readable default names for native sessions.

use std::path::Path;
use std::process::Command;
use std::fs::{File, OpenOptions};
use std::io;
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};

/// Serialize title selection until the new registry entry is published.
pub(super) struct TitleLock(File);

impl TitleLock {
    pub(super) fn acquire(runtime: &Path) -> io::Result<Self> {
        let file = OpenOptions::new().read(true).write(true).create(true).mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(runtime.join("title.lock"))?;
        let meta = file.metadata()?;
        if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() }
            || meta.permissions().mode() & 0o077 != 0 || meta.nlink() != 1 {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "insecure title lock"));
        }
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self(file))
    }
}

impl Drop for TitleLock {
    fn drop(&mut self) { unsafe { libc::flock(self.0.as_raw_fd(), libc::LOCK_UN); } }
}

pub(super) fn repo_root(cwd: &Path) -> Option<String> {
    let output = Command::new("git")
        .args(["rev-parse", "--path-format=absolute", "--git-common-dir"])
        .env_remove("GIT_DIR").env_remove("GIT_WORK_TREE")
        .env_remove("GIT_COMMON_DIR").env_remove("GIT_INDEX_FILE")
        .current_dir(cwd).output().ok()?;
    if !output.status.success() { return None; }
    let common = std::path::PathBuf::from(std::str::from_utf8(&output.stdout).ok()?.trim());
    if !common.is_absolute() { return None; }
    let root = if common.file_name().is_some_and(|name| name == ".git") {
        common.parent()?.to_path_buf()
    } else {
        common
    };
    Some(root.to_string_lossy().into_owned())
}

fn safe_part(value: &str, limit: usize) -> String {
    value.chars().filter(|c| !c.is_control()).take(limit).collect::<String>()
}

fn model_short_name(model: Option<&str>, engine: &str) -> String {
    let model = model.filter(|value| !value.trim().is_empty()).unwrap_or(engine);
    let name = model.rsplit('/').next().unwrap_or(model);
    let name = name.strip_prefix("claude-").unwrap_or(name);
    let name = safe_part(name, 40);
    if name.is_empty() { engine.to_owned() } else { name }
}

fn short_path(cwd: &Path) -> String {
    let components: Vec<_> = cwd.components().filter_map(|component| match component {
        std::path::Component::Normal(name) => Some(name.to_string_lossy()),
        _ => None,
    }).collect();
    let tail = components.iter().rev().take(2).rev().map(|s| s.as_ref()).collect::<Vec<_>>().join("/");
    if tail.is_empty() { "/".to_owned() } else { safe_part(&tail, 80) }
}

fn branch(cwd: &Path) -> String {
    let output = Command::new("git")
        .args(["symbolic-ref", "--quiet", "--short", "HEAD"])
        .env_remove("GIT_DIR").env_remove("GIT_WORK_TREE")
        .env_remove("GIT_COMMON_DIR").env_remove("GIT_INDEX_FILE")
        .current_dir(cwd).output();
    output.ok().filter(|result| result.status.success())
        .and_then(|result| String::from_utf8(result.stdout).ok())
        .map(|name| safe_part(name.trim(), 80))
        .filter(|name| !name.is_empty())
        .unwrap_or_else(|| "detached".to_owned())
}

pub(super) fn base(model: Option<&str>, engine: &str, cwd: &Path, repo_root: Option<&Path>) -> String {
    let model = model_short_name(model, engine);
    if let Some(repo) = repo_root {
        let name = repo.file_name().map(|value| value.to_string_lossy()).unwrap_or_default();
        let name = safe_part(&name, 48);
        if !name.is_empty() {
            return format!("{model}@{}/{}", branch(cwd), name);
        }
    }
    format!("{model}@{}", short_path(cwd))
}

pub(super) fn available(base: &str, existing: impl IntoIterator<Item = String>) -> String {
    let used: std::collections::HashSet<_> = existing.into_iter().collect();
    if !used.contains(base) { return base.to_owned(); }
    for number in 2..=used.len().saturating_add(2) {
        let candidate = format!("{base}-{number}");
        if !used.contains(&candidate) { return candidate; }
    }
    unreachable!("a free suffix exists after at most used.len() + 1 attempts")
}

pub(super) fn from_registry(runtime: &Path, session_id: &str, base: &str) -> std::io::Result<String> {
    let registry = runtime.join("registry");
    let existing = doxa_state::list_daemons(&registry, None, str::to_owned)?
        .into_iter()
        .filter(|entry| entry.route.session_id != session_id)
        .map(|entry| entry.display.title);
    Ok(available(base, existing))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repo_title_uses_model_branch_and_main_repo() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("sample-repo");
        std::fs::create_dir(&repo).unwrap();
        let git = |args: &[&str]| assert!(Command::new("git").args(args).current_dir(&repo).status().unwrap().success());
        git(&["init", "-q"]);
        git(&["symbolic-ref", "HEAD", "refs/heads/feature/menu"]);
        assert_eq!(base(Some("claude-opus-5-5"), "claude", &repo, Some(&repo)), "opus-5-5@feature/menu/sample-repo");
        assert_eq!(base(Some("gpt-6-sol"), "codex", &repo, Some(&repo)), "gpt-6-sol@feature/menu/sample-repo");
    }

    #[test]
    fn outside_repo_uses_short_path_and_suffixes_do_not_renumber() {
        let dir = tempfile::tempdir().unwrap();
        let cwd = dir.path().join("work").join("notes");
        assert_eq!(base(Some("deepseek-flash"), "deepseek", &cwd, None), "deepseek-flash@work/notes");
        let existing = ["gpt-6-sol@main/doxa".to_owned(), "gpt-6-sol@main/doxa-3".to_owned()];
        assert_eq!(available("gpt-6-sol@main/doxa", existing), "gpt-6-sol@main/doxa-2");
    }

    #[test]
    fn stale_registry_entry_does_not_reserve_a_title() {
        let runtime = tempfile::tempdir().unwrap();
        let registry = runtime.path().join("registry");
        std::fs::create_dir(&registry).unwrap();
        std::fs::write(registry.join("stale.json"),
            r#"{"session_id":"stale","title":"gpt-6-sol@main/doxa","pid":999999999}"#).unwrap();
        assert_eq!(from_registry(runtime.path(), "new", "gpt-6-sol@main/doxa").unwrap(),
            "gpt-6-sol@main/doxa");
    }
}
