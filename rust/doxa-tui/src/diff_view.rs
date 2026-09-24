//! Read-only, bounded worktree diff for the native TUI.
//! Git runs on a worker thread; painting never waits for a repository.

use std::fs::OpenOptions;
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const MAX_DIFF_BYTES: usize = 256 * 1024;
const GIT_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Clone, Debug)]
pub struct DiffSnapshot {
    pub text: String,
}

fn sidecar(cwd: &Path) -> Option<PathBuf> {
    let home = std::env::var_os("DOXA_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".doxa")))?;
    Some(home.join("worktrees/.meta").join(format!("{}.json", cwd.file_name()?.to_str()?)))
}

fn safe_ref(value: &str) -> bool {
    !value.is_empty() && value.len() <= 200 && !value.starts_with('-')
        && value.bytes().all(|b| b.is_ascii_alphanumeric() || b"_./-".contains(&b))
        && !value.contains("..")
}

fn base_for(cwd: &Path) -> Result<(String, &'static str), String> {
    let Some(path) = sidecar(cwd) else { return Ok(("HEAD".into(), "HEAD (uncommitted only)")); };
    let Ok(file) = OpenOptions::new().read(true).custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW).open(path)
        else { return Ok(("HEAD".into(), "HEAD (uncommitted only)")); };
    if file.metadata().map(|meta| !meta.is_file() || meta.uid() != unsafe { libc::geteuid() } || meta.len() > 16 * 1024).unwrap_or(true) {
        return Err("Worktree base metadata is too large".into());
    }
    let mut bytes = Vec::new();
    if file.take(16 * 1024 + 1).read_to_end(&mut bytes).is_err() || bytes.len() > 16 * 1024 {
        return Err("Worktree base metadata is unreadable or too large".into());
    }
    let Ok(meta) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
        return Err("Worktree base metadata is invalid".into());
    };
    let base = meta["base_ref"].as_str().unwrap_or("");
    if base.is_empty() { return Ok(("HEAD".into(), "HEAD (uncommitted only)")); }
    if !safe_ref(base) { return Err("Worktree base reference is invalid".into()); }
    if meta["branch"].as_str() == Some(base) {
        return Err("Recorded base equals this worktree's branch; committed changes cannot be shown".into());
    }
    Ok((base.into(), "recorded worktree base"))
}

/// Capture a bounded unified diff. The caller must run this off the UI thread.
pub fn read(cwd: &Path) -> DiffSnapshot {
    let Some(cwd) = cwd.canonicalize().ok().filter(|p| p.is_dir()) else {
        return DiffSnapshot { text: "Cannot read this session's worktree directory.".into() };
    };
    let (base, source) = match base_for(&cwd) {
        Ok(pair) => pair,
        Err(note) => return DiffSnapshot { text: format!("Cannot determine a safe diff base: {note}") },
    };
    let mut child = match Command::new("git")
        .args(["--no-pager", "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames", "--unified=3", &base, "--"])
        .current_dir(&cwd).env("GIT_OPTIONAL_LOCKS", "0")
        .stdout(Stdio::piped()).stderr(Stdio::null()).spawn() {
            Ok(child) => child,
            Err(_) => return DiffSnapshot { text: "Could not start git for this worktree.".into() },
        };
    // Read on this worker's own helper thread so a stalled git cannot block
    // the UI worker's timeout. The main UI still never waits for either.
    let stdout = child.stdout.take().expect("piped stdout");
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = stdout.take((MAX_DIFF_BYTES + 1) as u64).read_to_end(&mut bytes);
        bytes
    });
    let deadline = Instant::now() + GIT_TIMEOUT;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(20)),
            _ => { let _ = child.kill(); let _ = child.wait(); break None; }
        }
    };
    let bytes = reader.join().unwrap_or_default();
    let Some(status) = status else { return DiffSnapshot { text: "Git diff timed out or exceeded the bounded view.".into() }; };
    let truncated = bytes.len() > MAX_DIFF_BYTES;
    if !status.success() && !truncated { return DiffSnapshot { text: format!("Git could not compare this worktree with {base}.") }; }
    let text = String::from_utf8_lossy(&bytes[..bytes.len().min(MAX_DIFF_BYTES)]);
    let mut out = format!("Base: {base} ({source})\n");
    if text.is_empty() { out.push_str("No tracked changes in this comparison. Untracked files are not included."); }
    else { out.push_str(&text); }
    if truncated { out.push_str("\n[Diff view truncated at 256 KiB; inspect the worktree for the full patch.]\n"); }
    DiffSnapshot { text: out }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;
    #[test]
    fn rejects_self_base_and_option_like_refs() {
        assert!(!safe_ref("--output=/tmp/a"));
        assert!(!safe_ref("main..other"));
        assert!(safe_ref("refs/heads/main"));
    }


    #[test]
    fn reads_tracked_changes_without_touching_index() {
        let dir = tempfile::tempdir().unwrap();
        let git = |args: &[&str]| {
            assert!(Command::new("git").args(args).current_dir(dir.path()).status().unwrap().success());
        };
        git(&["init", "-q"]);
        std::fs::write(dir.path().join("tracked.txt"), "old\n").unwrap();
        git(&["add", "tracked.txt"]);
        git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: baseline"]);
        std::fs::write(dir.path().join("tracked.txt"), "new\n").unwrap();
        std::fs::write(dir.path().join("untracked.txt"), "untracked\n").unwrap();
        let result = read(dir.path());
        assert!(result.text.contains("-old"), "{}", result.text);
        assert!(result.text.contains("+new"), "{}", result.text);
        assert!(!result.text.contains("untracked.txt"));
        let staged = Command::new("git").args(["diff", "--cached", "--name-only"])
            .current_dir(dir.path()).output().unwrap();
        assert!(staged.stdout.is_empty());
    }
}
