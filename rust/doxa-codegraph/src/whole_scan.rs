//! Disabled semantic-path measurement of every directory and regular file in
//! the worktree mount. It is a bounded, repeated observation, not an atomic
//! filesystem snapshot or evidence that an analyzer saw these bytes.

use super::{file_digest, scan_digest, worktree_root, MAX_FILES, MAX_PATH_BYTES};
use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::Path;
use std::sync::{mpsc, Mutex, OnceLock};
use std::time::{Duration, Instant};

const MAX_FILE_BYTES: u64 = 8 * 1024 * 1024;
const MAX_TREE_BYTES: u64 = 128 * 1024 * 1024;
const MAX_TREE_TIME: Duration = Duration::from_secs(20);

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct WholeScan {
    pub digest: String,
    pub entries: usize,
    pub bytes: u64,
}

fn one_pass(root: &Path, started: Instant) -> Result<WholeScan, String> {
    let root_metadata = fs::symlink_metadata(root)
        .map_err(|e| format!("whole-worktree root metadata: {e}"))?;
    let mut pending = vec![(root.to_path_buf(), String::new(),
        root_metadata.dev(), root_metadata.ino())];
    let mut entries = BTreeMap::<String, String>::new();
    entries.insert(".".into(), format!("directory:{:o}", root_metadata.mode() & 0o7777));
    let mut total = 0u64;
    while let Some((directory, prefix, dev, ino)) = pending.pop() {
        if started.elapsed() >= MAX_TREE_TIME {
            return Err("whole-worktree scan exceeded 20-second limit".into());
        }
        let before_directory = fs::symlink_metadata(&directory)
            .map_err(|e| format!("whole-worktree directory changed: {prefix}: {e}"))?;
        if !before_directory.file_type().is_dir()
            || (before_directory.dev(), before_directory.ino()) != (dev, ino) {
            return Err(format!("whole-worktree directory changed: {prefix}"));
        }
        let children = fs::read_dir(&directory)
            .map_err(|e| format!("whole-worktree directory unreadable: {prefix}: {e}"))?;
        for child in children {
            if started.elapsed() >= MAX_TREE_TIME {
                return Err("whole-worktree scan exceeded 20-second limit".into());
            }
            let child = child.map_err(|e| format!("whole-worktree directory entry unreadable: {e}"))?;
            let name = child.file_name().into_string()
                .map_err(|_| "non-UTF-8 whole-worktree path")?;
            let relative = if prefix.is_empty() { name } else { format!("{prefix}/{name}") };
            if relative.len() > MAX_PATH_BYTES {
                return Err("whole-worktree path exceeds limit".into());
            }
            if entries.len() >= MAX_FILES {
                return Err("whole-worktree entry count exceeds limit".into());
            }
            let path = root.join(&relative);
            let before = fs::symlink_metadata(&path)
                .map_err(|e| format!("whole-worktree path changed: {relative}: {e}"))?;
            let mode = before.mode() & 0o7777;
            let value = if before.file_type().is_dir() {
                pending.push((path, relative.clone(), before.dev(), before.ino()));
                format!("directory:{mode:o}")
            } else if before.file_type().is_file() {
                if before.len() > MAX_FILE_BYTES {
                    return Err(format!("whole-worktree file exceeds 8 MiB: {relative}"));
                }
                let (sha, size) = file_digest(root, &relative, MAX_FILE_BYTES)
                    .map_err(|e| format!("whole-worktree file uncheckable: {relative}: {e}"))?;
                let after = fs::symlink_metadata(&path)
                    .map_err(|e| format!("whole-worktree path changed: {relative}: {e}"))?;
                if !after.file_type().is_file() || before.dev() != after.dev()
                    || before.ino() != after.ino() || before.len() != after.len()
                    || before.modified().ok() != after.modified().ok()
                    || before.mode() != after.mode() {
                    return Err(format!("whole-worktree file changed during read: {relative}"));
                }
                total = total.saturating_add(size);
                if total > MAX_TREE_BYTES {
                    return Err("whole-worktree scan exceeded 128 MiB".into());
                }
                format!("file:{mode:o}:{size}:{sha}")
            } else {
                // A symlink, socket, FIFO, or device could change the set of
                // bytes visible to an analyzer. No complete claim is possible.
                return Err(format!("whole-worktree nonregular entry: {relative}"));
            };
            entries.insert(relative, value);
        }
        let after_directory = fs::symlink_metadata(&directory)
            .map_err(|e| format!("whole-worktree directory changed: {prefix}: {e}"))?;
        if !after_directory.file_type().is_dir()
            || (after_directory.dev(), after_directory.ino()) != (dev, ino) {
            return Err(format!("whole-worktree directory changed: {prefix}"));
        }
    }
    if started.elapsed() >= MAX_TREE_TIME {
        return Err("whole-worktree scan exceeded 20-second limit".into());
    }
    let digest = scan_digest(b"doxa-whole-worktree-input-v1\0",
        entries.iter().map(|(path, fact)| (path.as_str(), fact.as_str())));
    Ok(WholeScan { digest, entries: entries.len(), bytes: total })
}

struct ScanRequest {
    root: std::path::PathBuf,
    reply: mpsc::Sender<Result<WholeScan, String>>,
    #[cfg(test)]
    pause: Duration,
}

// A directory read can block in the kernel. Keep one process-local worker so
// a timed-out scan does not spawn an unbounded number of stranded threads.
struct WholeScanReader {
    requests: mpsc::SyncSender<ScanRequest>,
    disabled: bool,
}

impl WholeScanReader {
    fn new() -> Result<Self, String> {
        let (requests, incoming) = mpsc::sync_channel::<ScanRequest>(1);
        std::thread::Builder::new().name("doxa-whole-scan-reader".into())
            .spawn(move || {
                for request in incoming {
                    #[cfg(test)]
                    std::thread::sleep(request.pause);
                    let result = scan_with_between_passes(&request.root, |_| {});
                    let _ = request.reply.send(result);
                }
            }).map_err(|e| format!("whole-worktree reader start: {e}"))?;
        Ok(Self { requests, disabled: false })
    }

    fn read(&mut self, root: &Path, deadline: Duration,
        #[cfg(test)] pause: Duration) -> Result<WholeScan, String> {
        if self.disabled { return Err("whole-worktree reader disabled after timeout".into()); }
        let (reply, received) = mpsc::channel();
        let request = ScanRequest { root: root.to_path_buf(), reply,
            #[cfg(test)] pause };
        if self.requests.try_send(request).is_err() {
            self.disabled = true;
            return Err("whole-worktree reader unavailable".into());
        }
        match received.recv_timeout(deadline) {
            Ok(result) => result,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                self.disabled = true;
                Err("whole-worktree scan exceeded caller deadline; reader disabled".into())
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                self.disabled = true;
                Err("whole-worktree reader stopped".into())
            }
        }
    }
}

static WHOLE_SCAN_READER: OnceLock<Result<Mutex<WholeScanReader>, String>> = OnceLock::new();

fn scan_with_between_passes(root: &Path, between: impl FnOnce(&Path)) -> Result<WholeScan, String> {
    let root = worktree_root(root)?;
    let started = Instant::now();
    let first = one_pass(&root, started)?;
    between(&root);
    let second = one_pass(&root, started)?;
    if first != second {
        return Err("whole-worktree inputs changed between verification passes".into());
    }
    Ok(second)
}

/// Repeated digest of all entries under the worktree root, including ignored
/// files and empty directories. `.git` is included (and may exceed bounds).
/// Files are read through the same descriptor-anchored, no-symlink reader as
/// source queries. Each read has a two-second caller deadline. Directory
/// listing itself is checked between entries, not interruptible in-kernel.
pub(super) fn current_whole_worktree_sha256(root: &Path) -> Result<WholeScan, String> {
    let deadline = Instant::now() + MAX_TREE_TIME;
    let reader = WHOLE_SCAN_READER.get_or_init(|| WholeScanReader::new().map(Mutex::new))
        .as_ref().map_err(Clone::clone)?;
    let mut reader = loop {
        match reader.try_lock() {
            Ok(reader) => break reader,
            Err(std::sync::TryLockError::Poisoned(_)) => {
                return Err("whole-worktree reader lock poisoned".into());
            }
            Err(std::sync::TryLockError::WouldBlock) => {
                if Instant::now() >= deadline {
                    return Err("whole-worktree reader busy for 20 seconds".into());
                }
                std::thread::sleep(Duration::from_millis(1));
            }
        }
    };
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() { return Err("whole-worktree reader wait exceeded deadline".into()); }
    reader.read(root, remaining,
        #[cfg(test)] Duration::ZERO)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::fs::symlink;
    use std::process::Command;

    fn worktree() -> tempfile::TempDir {
        let root = tempfile::tempdir().unwrap();
        assert!(Command::new("git").arg("init").arg("-q").arg(root.path())
            .status().unwrap().success());
        root
    }

    #[test]
    fn covers_ignored_binary_manifests_empty_directories_and_modes() {
        let root = worktree();
        fs::write(root.path().join(".gitignore"), "ignored/\n").unwrap();
        fs::create_dir(root.path().join("ignored")).unwrap();
        fs::create_dir(root.path().join("empty")).unwrap();
        fs::write(root.path().join("ignored/data.bin"), [0, 255, 1]).unwrap();
        fs::write(root.path().join("Cargo.toml"), "[package]\nname='x'\n").unwrap();
        let first = current_whole_worktree_sha256(root.path()).unwrap();
        assert!(first.entries > 4);
        fs::write(root.path().join("ignored/data.bin"), [0, 254, 1]).unwrap();
        assert_ne!(first.digest, current_whole_worktree_sha256(root.path()).unwrap().digest);
        fs::write(root.path().join("ignored/data.bin"), [0, 255, 1]).unwrap();
        fs::write(root.path().join("Cargo.toml"), "[package]\nname='y'\n").unwrap();
        assert_ne!(first.digest, current_whole_worktree_sha256(root.path()).unwrap().digest);
        fs::write(root.path().join("Cargo.toml"), "[package]\nname='x'\n").unwrap();
        fs::remove_dir(root.path().join("empty")).unwrap();
        assert_ne!(first.digest, current_whole_worktree_sha256(root.path()).unwrap().digest);
        fs::create_dir(root.path().join("empty")).unwrap();
        let file = root.path().join("Cargo.toml");
        let original = fs::metadata(&file).unwrap().permissions();
        fs::set_permissions(&file, fs::Permissions::from_mode(0o700)).unwrap();
        assert_ne!(first.digest, current_whole_worktree_sha256(root.path()).unwrap().digest);
        fs::set_permissions(&file, original).unwrap();
    }

    #[test]
    fn rejects_add_remove_and_edits_between_passes() {
        let root = worktree();
        fs::write(root.path().join("Cargo.lock"), "a").unwrap();
        for change in ["add", "remove", "edit"] {
            let error = scan_with_between_passes(root.path(), |root| match change {
                "add" => { fs::write(root.join("late"), "x").unwrap(); },
                "remove" => { fs::remove_file(root.join("Cargo.lock")).unwrap(); },
                "edit" => { fs::write(root.join("late"), "y").unwrap(); },
                _ => unreachable!(),
            }).unwrap_err();
            assert!(error.contains("changed between verification passes"), "{change}: {error}");
        }
    }

    #[test]
    fn rejects_symlink_fifo_and_oversized_file() {
        let root = worktree();
        symlink("outside", root.path().join("link")).unwrap();
        assert!(current_whole_worktree_sha256(root.path()).unwrap_err().contains("nonregular"));
        fs::remove_file(root.path().join("link")).unwrap();
        fs::write(root.path().join("big"), vec![0; MAX_FILE_BYTES as usize + 1]).unwrap();
        assert!(current_whole_worktree_sha256(root.path()).unwrap_err().contains("exceeds 8 MiB"));
        fs::remove_file(root.path().join("big")).unwrap();
        let fifo = root.path().join("pipe");
        let c_path = std::ffi::CString::new(fifo.as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(c_path.as_ptr(), 0o600) }, 0);
        assert!(current_whole_worktree_sha256(root.path()).unwrap_err().contains("nonregular"));
    }

    #[test]
    fn blocked_whole_scan_worker_fails_closed() {
        let root = worktree();
        let mut reader = WholeScanReader::new().unwrap();
        let error = reader.read(root.path(), Duration::from_millis(25),
            Duration::from_millis(100)).unwrap_err();
        assert!(error.contains("caller deadline"), "{error}");
        assert!(reader.read(root.path(), Duration::from_secs(1), Duration::ZERO)
            .unwrap_err().contains("disabled"));
    }
}
