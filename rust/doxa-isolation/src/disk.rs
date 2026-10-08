//! Sample the session-owned tree. This gates new turns; it is not a filesystem
//! quota, so a running worker can still write between samples.
use super::{error, free_bytes, private_directory, Manifest, Policy};
use std::{
    collections::HashSet,
    ffi::{CStr, CString},
    fs::{File, OpenOptions},
    io,
    os::{fd::{AsRawFd, FromRawFd}, unix::fs::{MetadataExt, OpenOptionsExt}},
    path::Path,
    time::{Duration, Instant},
};

const GIB: u64 = 1024 * 1024 * 1024;
const MAX_ENTRIES: u64 = 1_000_000;
const MAX_DEPTH: usize = 128;
const MAX_SCAN_TIME: Duration = Duration::from_secs(10);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DiskSnapshot {
    /// Allocated bytes under the session root, including Git objects, auth
    /// home, cache and any retained migration checkout.
    pub usage_bytes: u64,
    /// Smallest available space on the session root or one of its bind mounts.
    pub free_bytes: u64,
}

struct Scan {
    seen: HashSet<(u64, u64)>,
    root_device: u64,
    usage: u64,
    entries: u64,
    started: Instant,
}
impl Scan {
    fn record(&mut self, dev: u64, ino: u64, blocks: i64) -> io::Result<bool> {
        self.entries += 1;
        if self.entries > MAX_ENTRIES || self.started.elapsed() > MAX_SCAN_TIME {
            return Err(error("session disk scan exceeded its entry/time bound; new turns refused"));
        }
        if dev != self.root_device {
            return Err(error("session tree spans multiple filesystems; monitored free-space floor cannot cover it"));
        }
        let first = self.seen.insert((dev, ino));
        if first {
            let bytes = (blocks.max(0) as u64).checked_mul(512)
                .ok_or_else(|| error("session disk usage overflow"))?;
            self.usage = self.usage.checked_add(bytes)
                .ok_or_else(|| error("session disk usage overflow"))?;
        }
        Ok(first)
    }
}

fn open_directory(path: &Path) -> io::Result<File> {
    OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC).open(path)
}

struct DirectoryStream(*mut libc::DIR);
impl DirectoryStream {
    fn open(dir: &File) -> io::Result<Self> {
        // fdopendir owns its fd. Open "." relative to the already verified
        // directory so enumeration stays anchored and the fd cannot leak into
        // a concurrent provider process.
        let fd = unsafe { libc::openat(dir.as_raw_fd(), b".\0".as_ptr().cast(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        let stream = unsafe { libc::fdopendir(fd) };
        if stream.is_null() {
            let error = io::Error::last_os_error();
            unsafe { libc::close(fd); }
            return Err(error);
        }
        Ok(Self(stream))
    }
    fn next_name(&mut self) -> io::Result<Option<CString>> {
        errno::set_errno(errno::Errno(0));
        let entry = unsafe { libc::readdir(self.0) };
        if entry.is_null() {
            let code = errno::errno().0;
            return if code == 0 { Ok(None) } else { Err(io::Error::from_raw_os_error(code)) };
        }
        // readdir owns this buffer and may overwrite it on the next call.
        let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) };
        Ok(Some(name.to_owned()))
    }
}
impl Drop for DirectoryStream {
    fn drop(&mut self) { unsafe { libc::closedir(self.0); } }
}

fn scan_directory(dir: &File, state: &mut Scan, depth: usize) -> io::Result<()> {
    if depth >= MAX_DEPTH { return Err(error("session disk scan exceeded its directory depth bound; new turns refused")); }
    // Enumeration follows a stable directory descriptor. fstatat/openat stay
    // anchored there even if a worker renames a child during the scan.
    let mut entries = DirectoryStream::open(dir)?;
    while let Some(name) = entries.next_name()? {
        if name.as_bytes() == b"." || name.as_bytes() == b".." { continue; }
        let mut raw = std::mem::MaybeUninit::<libc::stat>::uninit();
        if unsafe { libc::fstatat(dir.as_raw_fd(), name.as_ptr(), raw.as_mut_ptr(), libc::AT_SYMLINK_NOFOLLOW) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let raw = unsafe { raw.assume_init() };
        let first = state.record(raw.st_dev as u64, raw.st_ino as u64, raw.st_blocks)?;
        if first && raw.st_mode & libc::S_IFMT == libc::S_IFDIR {
            let child_fd = unsafe { libc::openat(dir.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
            if child_fd < 0 { return Err(io::Error::last_os_error()); }
            let child = unsafe { File::from_raw_fd(child_fd) };
            let actual = child.metadata()?;
            if (actual.dev(), actual.ino()) != (raw.st_dev as u64, raw.st_ino as u64) {
                return Err(error("session directory changed during disk scan; new turns refused"));
            }
            scan_directory(&child, state, depth + 1)?;
        }
    }
    Ok(())
}

fn usage(path: &Path) -> io::Result<u64> {
    let root = open_directory(path)?;
    let meta = root.metadata()?;
    let mut state = Scan { seen: HashSet::new(), root_device:meta.dev(), usage: 0, entries: 0, started: Instant::now() };
    state.record(meta.dev(), meta.ino(), meta.blocks() as i64)?;
    scan_directory(&root, &mut state, 0)?;
    Ok(state.usage)
}

pub(crate) fn check_host_floor(path: &Path, policy: &Policy) -> io::Result<()> {
    let free = free_bytes(path)?;
    let floor = policy.disk_free_floor_bytes.unwrap_or(2 * GIB);
    if free < floor {
        return Err(error(format!("Docker session filesystem has {free} bytes free, below the monitored {floor}-byte floor; new turns refused")));
    }
    Ok(())
}

pub(crate) fn sample(manifest: &Manifest) -> io::Result<DiskSnapshot> {
    if !manifest.profile.docker() { return Err(error("native sessions have no Docker disk monitor")); }
    let root = manifest.checkout.parent().ok_or_else(|| error("session root missing"))?;
    if root != manifest.private_home.parent().unwrap_or(Path::new(""))
        || root != manifest.cache.parent().unwrap_or(Path::new(""))
        || root != manifest.broker.parent().unwrap_or(Path::new("")) {
        return Err(error("session disk paths differ from the private root"));
    }
    private_directory(root, false)?;
    let mut free = free_bytes(root)?;
    for mount in [&manifest.checkout, &manifest.private_home, &manifest.cache, &manifest.broker] {
        private_directory(mount, false)?;
        free = free.min(free_bytes(mount)?);
    }
    Ok(DiskSnapshot { usage_bytes: usage(root)?, free_bytes: free })
}

pub(crate) fn enforce(sample: DiskSnapshot, policy: &Policy) -> io::Result<()> {
    if let Some(limit) = policy.disk_soft_limit_bytes {
        if sample.usage_bytes > limit {
            return Err(error(format!("Docker session uses {} bytes, above the monitored {}-byte soft ceiling; new turns refused (not a hard quota)", sample.usage_bytes, limit)));
        }
    }
    let floor = policy.disk_free_floor_bytes.unwrap_or(2 * GIB);
    if sample.free_bytes < floor {
        return Err(error(format!("Docker session filesystem has {} bytes free, below the monitored {}-byte floor; new turns refused (not a hard quota)", sample.free_bytes, floor)));
    }
    Ok(())
}

pub fn check_disk_budget(manifest: &Manifest) -> io::Result<DiskSnapshot> {
    let policy = manifest.policy.as_ref().ok_or_else(|| error("Docker disk policy missing"))?;
    let snapshot = sample(manifest)?;
    enforce(snapshot, policy)?;
    Ok(snapshot)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::symlink};

    fn policy() -> Policy {
        Policy { image: format!("sha256:{}", "a".repeat(64)), docker_host: "unix:///run/user/1000/test.sock".into(),
            memory_bytes: GIB, cpus: 1.0, pids: 128, disk_soft_limit_bytes: Some(GIB), disk_free_floor_bytes: Some(GIB) }
    }
    #[test]
    fn ceiling_and_floor_refuse_new_turns_independently() {
        let mut p = policy();
        assert!(enforce(DiskSnapshot { usage_bytes: GIB, free_bytes: GIB }, &p).is_ok());
        assert!(enforce(DiskSnapshot { usage_bytes: GIB + 1, free_bytes: GIB }, &p).unwrap_err().to_string().contains("soft ceiling"));
        assert!(enforce(DiskSnapshot { usage_bytes: 1, free_bytes: GIB - 1 }, &p).unwrap_err().to_string().contains("floor"));
        p.disk_soft_limit_bytes = None; // older beta.10 manifest
        assert!(enforce(DiskSnapshot { usage_bytes: GIB + 1, free_bytes: GIB }, &p).is_ok());
    }
    #[test]
    fn scan_counts_allocated_blocks_once_and_never_follows_links() {
        let root = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let baseline = usage(root.path()).unwrap();
        fs::write(outside.path().join("large"), vec![b'x'; 1024 * 1024]).unwrap();
        symlink(outside.path(), root.path().join("outside")).unwrap();
        assert!(usage(root.path()).unwrap() < baseline + 1024 * 1024);
        fs::write(root.path().join("content"), vec![b'x'; 8192]).unwrap();
        let once = usage(root.path()).unwrap();
        fs::hard_link(root.path().join("content"), root.path().join("same-content")).unwrap();
        let twice = usage(root.path()).unwrap();
        assert_eq!(once, twice);
        fs::create_dir(root.path().join("nested")).unwrap();
        fs::write(root.path().join("nested/.hidden"), vec![b'y'; 8192]).unwrap();
        assert!(usage(root.path()).unwrap() > twice);
    }
}
