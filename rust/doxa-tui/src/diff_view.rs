//! Read-only, bounded worktree diff for the native TUI.
//! Git runs on a worker thread; painting never waits for a repository.

use std::fs::OpenOptions;
use std::io::{self, Read};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const MAX_DIFF_BYTES: usize = 256 * 1024;
const MAX_UNTRACKED_BYTES: usize = 64 * 1024;
const MAX_UNTRACKED_FILES: usize = 100;
const GIT_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Clone, Debug)]
pub struct DiffSnapshot {
    pub text: String,
    /// Zero-based display rows from the same git output as `text`.
    pub files: Vec<usize>,
    pub hunks: Vec<usize>,
}

impl DiffSnapshot {
    fn message(text: String) -> Self {
        Self { text, files: Vec::new(), hunks: Vec::new() }
    }
}

fn landmarks(patch: &str, truncated: bool) -> (Vec<usize>, Vec<usize>) {
    let mut files = Vec::new();
    let mut hunks = Vec::new();
    let mut in_file = false;
    let mut has_content_header = false;
    let complete = if truncated && !patch.ends_with('\n') {
        patch.rsplit_once('\n').map_or("", |(head, _)| head)
    } else { patch };
    for (index, line) in complete.lines().enumerate() {
        if line.starts_with("diff --git ") {
            files.push(index + 1); // The rendered Base line precedes the patch.
            in_file = true;
            has_content_header = false;
        } else if in_file && line.starts_with("+++ ") {
            has_content_header = true;
        } else if has_content_header && line.starts_with("@@ ") && line[3..].contains(" @@") {
            hunks.push(index + 1);
        }
    }
    (files, hunks)
}

fn git_output(cwd: &Path, args: &[&str], limit: usize) -> Result<(Vec<u8>, bool), String> {
    let mut child = Command::new("git")
        .args(args)
        .current_dir(cwd)
        .env("GIT_OPTIONAL_LOCKS", "0")
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| "Could not start git for this worktree.".to_owned())?;
    let stdout = child.stdout.take().expect("piped stdout");
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let mut stdout = stdout;
        let mut chunk = [0u8; 8192];
        loop {
            match stdout.read(&mut chunk) {
                Ok(0) => break,
                Ok(count) => {
                    let remaining = (limit + 1).saturating_sub(bytes.len());
                    bytes.extend_from_slice(&chunk[..count.min(remaining)]);
                }
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            }
        }
        Ok(bytes)
    });
    let deadline = Instant::now() + GIT_TIMEOUT;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(20)),
            _ => { let _ = child.kill(); let _ = child.wait(); break None; }
        }
    };
    let bytes = reader.join().map_err(|_| "Git output reader stopped unexpectedly.".to_owned())?
        .map_err(|_| "Git output could not be read.".to_owned())?;
    let Some(status) = status else { return Err("Git timed out or exceeded the bounded view.".into()); };
    if !status.success() { return Err("Git could not inspect this worktree.".into()); }
    let truncated = bytes.len() > limit;
    Ok((bytes, truncated))
}

fn untracked_names(bytes: &[u8], truncated: bool) -> String {
    let mut out = String::from("\nUntracked files (names only; contents are not read):\n");
    let mut shown = 0;
    let mut omitted = truncated;
    // A bounded read can end inside a pathname. Never show a partial name.
    let complete = if truncated { &bytes[..bytes.iter().rposition(|byte| *byte == 0).unwrap_or(0)] } else { bytes };
    for raw in complete.split(|byte| *byte == 0) {
        if raw.is_empty() { continue; }
        if shown == MAX_UNTRACKED_FILES { omitted = true; break; }
        let name = String::from_utf8_lossy(raw);
        let mut escaped = String::new();
        let mut name_truncated = false;
        for character in name.chars() {
            let part = character.escape_default().to_string();
            if escaped.len() + part.len() > 300 {
                name_truncated = true;
                break;
            }
            escaped.push_str(&part);
        }
        out.push_str("  ");
        out.push_str(&escaped);
        if name_truncated { out.push_str("…"); }
        out.push('\n');
        shown += 1;
    }
    if shown == 0 { out.push_str("  None\n"); }
    if omitted {
        out.push_str("[Additional untracked names omitted from this view.]\n");
    }
    out
}

fn sidecar(cwd: &Path) -> Option<PathBuf> {
    let home = std::env::var_os("DOXA_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".doxa")))?;
    sidecar_in_home(cwd, &home)
}

fn sidecar_in_home(cwd: &Path, home: &Path) -> Option<PathBuf> {
    let root = home.join("worktrees").canonicalize().ok()?;
    if cwd.parent()? != root { return None; }
    Some(root.join(".meta").join(format!("{}.json", cwd.file_name()?.to_str()?)))
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
        return DiffSnapshot::message("Cannot read this session's worktree directory.".into());
    };
    let (base, source) = match base_for(&cwd) {
        Ok(pair) => pair,
        Err(note) => return DiffSnapshot::message(format!("Cannot determine a safe diff base: {note}")),
    };
    let (bytes, truncated) = match git_output(&cwd,
        &["--no-pager", "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames", "--unified=3", &base, "--"], MAX_DIFF_BYTES) {
        Ok(result) => result,
        Err(note) => return DiffSnapshot::message(format!("Git could not compare this worktree with {base}: {note}")),
    };
    let text = String::from_utf8_lossy(&bytes[..bytes.len().min(MAX_DIFF_BYTES)]);
    let (files, hunks) = landmarks(&text, truncated);
    let mut out = format!("Base: {base} ({source})\n");
    if text.is_empty() { out.push_str("No tracked changes in this comparison.\n"); }
    else { out.push_str(&text); }
    if truncated { out.push_str("\n[Diff view truncated at 256 KiB; inspect the worktree for the full patch.]\n"); }
    match git_output(&cwd, &["ls-files", "--others", "--exclude-standard", "-z", "--"], MAX_UNTRACKED_BYTES) {
        Ok((names, truncated)) => out.push_str(&untracked_names(&names, truncated)),
        Err(_) => out.push_str("\n[Untracked names unavailable.]\n"),
    }
    DiffSnapshot { text: out, files, hunks }
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
    fn sidecar_requires_worktree_under_doxa_root() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("home/worktrees");
        let unrelated = dir.path().join("other/same-name");
        let worktree = root.join("same-name");
        std::fs::create_dir_all(&worktree).unwrap();
        std::fs::create_dir_all(&unrelated).unwrap();
        assert_eq!(sidecar_in_home(&worktree, &dir.path().join("home")),
            Some(root.join(".meta/same-name.json")));
        assert_eq!(sidecar_in_home(&unrelated, &dir.path().join("home")), None);
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
        assert!(result.text.contains("untracked.txt"));
        assert!(!result.text.contains("untracked\n"));
        assert!(result.text.contains("names only; contents are not read"));
        assert_eq!(result.files, [1]);
        assert_eq!(result.hunks.len(), 1);
        assert!(result.text.lines().nth(result.hunks[0]).unwrap().starts_with("@@ "));
        let staged = Command::new("git").args(["diff", "--cached", "--name-only"])
            .current_dir(dir.path()).output().unwrap();
        assert!(staged.stdout.is_empty());
    }

    #[test]
    fn landmarks_ignore_patch_body_and_untracked_name_lookalikes() {
        let patch = "diff --git a/one b/one\n--- a/one\n+++ b/one\n@@ -1 +1,3 @@\n old\n+diff --git a/fake b/fake\n+@@ -1 +1 @@\ndiff --git a/two b/two\n--- a/two\n+++ b/two\n@@ -1 +1 @@\n-old\n+new\n";
        let (files, hunks) = landmarks(patch, false);
        assert_eq!(files, [1, 8]);
        assert_eq!(hunks, [4, 11]);
        assert_eq!(landmarks("diff --git a/cut b/cut", true), (vec![], vec![]));
    }

    #[test]
    fn untracked_names_escape_control_characters_and_drop_partial_tail() {
        let names = untracked_names(b"normal.txt\0line\nbreak\0partial", true);
        assert!(names.contains("normal.txt"));
        assert!(names.contains("line\\nbreak"));
        assert!(!names.contains("partial"));
        assert!(names.contains("Additional untracked names omitted"));
    }

    #[test]
    fn untracked_names_report_only_actual_omissions() {
        let exact = format!("{}\0", "x".repeat(300));
        let rendered = untracked_names(exact.as_bytes(), false);
        assert!(rendered.contains(&"x".repeat(300)));
        assert!(!rendered.contains('…'));
        let hundred = (0..MAX_UNTRACKED_FILES).map(|i| format!("{i}\0")).collect::<String>();
        assert!(!untracked_names(hundred.as_bytes(), false).contains("Additional untracked"));
        let more = format!("{hundred}extra\0");
        assert!(untracked_names(more.as_bytes(), false).contains("Additional untracked"));
    }

    #[test]
    fn bounded_git_reader_drains_output_larger_than_pipe() {
        let dir = tempfile::tempdir().unwrap();
        assert!(Command::new("git").args(["init", "-q"]).current_dir(dir.path()).status().unwrap().success());
        for index in 0..512 {
            std::fs::write(dir.path().join(format!("{index:04}-{}.txt", "long-name".repeat(12))), b"").unwrap();
        }
        let (bytes, truncated) = git_output(dir.path(), &["ls-files", "--others", "--exclude-standard", "-z", "--"], 128).unwrap();
        assert!(truncated);
        assert_eq!(bytes.len(), 129);
    }
}
