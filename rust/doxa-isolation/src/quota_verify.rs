//! Read-only, descriptor-anchored project-quota snapshot for a session tree.
//!
//! This is one necessary admission check, not a durable enforcement proof:
//! Docker bind behavior, EDQUOT, and restart/remount still need independent
//! verification before a hardened profile may be enabled.
use crate::{error, Manifest, Profile};
use std::{ffi::{CStr, CString}, fs::File, io, os::fd::{AsRawFd, FromRawFd},
    os::unix::{ffi::OsStrExt, fs::MetadataExt}, path::{Component, Path}};

const MAX_HARD_BYTES: u64 = 1024 * 1024 * 1024 * 1024;
const MAX_DESCENDANTS: usize = 4096;
const MAX_DEPTH: usize = 64;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct QuotaExpectation { pub project_id: u32, pub hard_limit_bytes: u64 }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct QuotaSnapshot { pub project_id: u32, pub hard_limit_bytes: u64,
    pub mount_id: u64, pub filesystem_device: u64, pub descendants_checked: usize,
    pub broker_entries_checked: usize }

#[derive(Clone, Copy, Debug)]
struct ProjectState { id: u32, inherits: bool, mount_id: u64 }

#[derive(Clone, Copy, Debug)]
struct LimitState { id: u32, hard_limit_bytes: u64, accounting: bool, enforcing: bool }

trait QuotaReader {
    fn project(&self, directory: &File) -> io::Result<ProjectState>;
    fn limit(&self, directory: &File, project_id: u32) -> io::Result<LimitState>;
}

/// Verify the *current* four directory inodes, bounded data-bind descendants,
/// expected broker socket entries and effective project quota on their
/// filesystem. The expectation must come
/// from an owner-controlled session policy; a fixture receipt alone is not one.
pub fn inspect_session_hard_quota(manifest: &Manifest, expected: QuotaExpectation) -> io::Result<QuotaSnapshot> {
    inspect_with(manifest, expected, &KernelQuotaReader)
}

fn inspect_with(manifest: &Manifest, expected: QuotaExpectation, reader: &impl QuotaReader) -> io::Result<QuotaSnapshot> {
    if manifest.profile != Profile::DockerOffline || manifest.state != "ready" {
        return Err(error("hard-quota inspection requires a ready network-none Docker session"));
    }
    if expected.project_id == 0 || expected.project_id > i32::MAX as u32
        || expected.hard_limit_bytes == 0
        || expected.hard_limit_bytes % 512 != 0
        || expected.hard_limit_bytes > MAX_HARD_BYTES {
        return Err(error("hard-quota expectation has no finite project ID and hard block limit"));
    }
    let root_path = manifest.checkout.parent().ok_or_else(|| error("session root missing"))?;
    if manifest.checkout != root_path.join("checkout") || manifest.private_home != root_path.join("home")
        || manifest.cache != root_path.join("cache") || manifest.broker != root_path.join("broker") {
        return Err(error("quota inspection paths escape the session root"));
    }
    let root = open_absolute_directory(root_path)?;
    let root_meta = private_metadata(&root)?;
    let root_state = reader.project(&root).map_err(|_| error("session project metadata unavailable"))?;
    if root_state.id != expected.project_id || !root_state.inherits || root_state.mount_id == 0 {
        return Err(error("session root has wrong project ID, inheritance, or mount identity"));
    }
    let mut descendants = 0;
    for (name, path) in [("checkout", &manifest.checkout), ("home", &manifest.private_home), ("cache", &manifest.cache)] {
        let directory = open_child(&root, name)?;
        let meta = private_metadata(&directory)?;
        if meta.dev() != root_meta.dev() { return Err(error("session bind source crosses a filesystem boundary")); }
        if name == "checkout" && (meta.dev(), meta.ino()) != (manifest.checkout_device, manifest.checkout_inode) {
            return Err(error("isolated checkout identity changed"));
        }
        let state = reader.project(&directory).map_err(|_| error("session bind project metadata unavailable"))?;
        if state.id != expected.project_id || !state.inherits || state.mount_id != root_state.mount_id {
            return Err(error("session bind source has wrong project ID, inheritance, or mount"));
        }
        audit_descendants(&directory, reader, expected.project_id, root_state.mount_id,
            root_meta.dev(), &mut descendants, 0)?;
        // Re-open the visible path after reading the descriptor to reject a
        // replaced bind source. This remains a snapshot, not a race-proof lease.
        let visible = open_absolute_directory(path)?;
        let visible_meta = visible.metadata()?;
        if (meta.dev(), meta.ino()) != (visible_meta.dev(), visible_meta.ino()) {
            return Err(error("session bind source changed during quota inspection"));
        }
    }
    let broker = open_child(&root, "broker")?;
    let broker_meta = private_metadata(&broker)?;
    if broker_meta.dev() != root_meta.dev() {
        return Err(error("session broker crosses a filesystem boundary"));
    }
    let broker_state = reader.project(&broker)
        .map_err(|_| error("session broker project metadata unavailable"))?;
    if broker_state.id != expected.project_id || !broker_state.inherits
        || broker_state.mount_id != root_state.mount_id {
        return Err(error("session broker has wrong project ID, inheritance, or mount"));
    }
    let broker_entries = audit_broker_entries(&broker, root_state.mount_id, root_meta.dev())?;
    let visible_broker = open_absolute_directory(&manifest.broker)?;
    let visible_meta = visible_broker.metadata()?;
    if (broker_meta.dev(), broker_meta.ino()) != (visible_meta.dev(), visible_meta.ino()) {
        return Err(error("session broker changed during quota inspection"));
    }
    let limit = reader.limit(&root, expected.project_id)
        .map_err(|_| error("effective project hard block limit unavailable"))?;
    if limit.id != expected.project_id || !limit.accounting || !limit.enforcing
        || limit.hard_limit_bytes != expected.hard_limit_bytes {
        return Err(error("effective project hard block limit or enforcement differs from policy"));
    }
    let visible = open_absolute_directory(root_path)?;
    let visible_meta = visible.metadata()?;
    if (root_meta.dev(), root_meta.ino()) != (visible_meta.dev(), visible_meta.ino()) {
        return Err(error("session root changed during quota inspection"));
    }
    Ok(QuotaSnapshot { project_id: expected.project_id, hard_limit_bytes: limit.hard_limit_bytes,
        mount_id: root_state.mount_id, filesystem_device: root_meta.dev(),
        descendants_checked: descendants, broker_entries_checked: broker_entries })
}

// Only the two host-created Unix sockets may appear in the read-only worker
// broker bind. This checks visible inode identities, ownership, permissions
// and mount identity at one instant; it does not prove the peer or quota on
// future socket writes. A live transport attestation is still required.
fn audit_broker_entries(directory: &File, mount_id: u64, device: u64) -> io::Result<usize> {
    let before = directory.metadata()?;
    let duplicate = directory.try_clone()?;
    let stream = unsafe { libc::fdopendir(duplicate.as_raw_fd()) };
    if stream.is_null() { return Err(io::Error::last_os_error()); }
    std::mem::forget(duplicate);
    struct DirectoryStream(*mut libc::DIR);
    impl Drop for DirectoryStream {
        fn drop(&mut self) { unsafe { libc::closedir(self.0); } }
    }
    let stream = DirectoryStream(stream);
    let mut checked = 0;
    loop {
        errno::set_errno(errno::Errno(0));
        let entry = unsafe { libc::readdir(stream.0) };
        if entry.is_null() {
            if errno::errno().0 != 0 { return Err(io::Error::last_os_error()); }
            break;
        }
        let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) };
        if name.to_bytes() == b"." || name.to_bytes() == b".." { continue; }
        checked += 1;
        if checked > 2 || !matches!(name.to_bytes(), b"hook.sock" | b"egress.sock") {
            return Err(error("session broker contains an unexpected entry"));
        }
        let mut stat: libc::stat = unsafe { std::mem::zeroed() };
        if unsafe { libc::fstatat(directory.as_raw_fd(), name.as_ptr(), &mut stat,
            libc::AT_SYMLINK_NOFOLLOW) } < 0 { return Err(io::Error::last_os_error()); }
        #[cfg(target_os = "linux")]
        let pinned = {
            let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(),
                libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
            if fd < 0 { return Err(io::Error::last_os_error()); }
            unsafe { File::from_raw_fd(fd) }
        };
        #[cfg(not(target_os = "linux"))]
        return Err(error("broker socket inspection requires Linux"));
        #[cfg(target_os = "linux")]
        {
            use std::os::unix::fs::FileTypeExt;
            let meta = pinned.metadata()?;
            if !same_entry(&stat, &meta) || !meta.file_type().is_socket()
                || meta.uid() != unsafe { libc::geteuid() } || meta.nlink() != 1
                || meta.mode() & 0o777 != 0o600 || meta.dev() != device {
                return Err(error("session broker socket has unsafe identity or permissions"));
            }
            if entry_mount_id(&pinned)? != mount_id {
                return Err(error("session broker socket crosses a mount boundary"));
            }
            let current = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(),
                libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
            if current < 0 { return Err(io::Error::last_os_error()); }
            let visible = unsafe { File::from_raw_fd(current) };
            if !same_entry(&stat, &visible.metadata()?) {
                return Err(error("session broker socket changed during inspection"));
            }
        }
    }
    let after = directory.metadata()?;
    if (before.dev(), before.ino(), before.mtime(), before.mtime_nsec(), before.ctime(), before.ctime_nsec())
        != (after.dev(), after.ino(), after.mtime(), after.mtime_nsec(), after.ctime(), after.ctime_nsec()) {
        return Err(error("session broker changed during entry inspection"));
    }
    Ok(checked)
}

#[cfg(target_os = "linux")]
fn entry_mount_id(file: &File) -> io::Result<u64> {
    let mut statx: libc::statx = unsafe { std::mem::zeroed() };
    if unsafe { libc::statx(file.as_raw_fd(), c"".as_ptr(),
        libc::AT_EMPTY_PATH | libc::AT_STATX_DONT_SYNC, libc::STATX_MNT_ID, &mut statx) } < 0 {
        return Err(io::Error::last_os_error());
    }
    if statx.stx_mask & libc::STATX_MNT_ID == 0 {
        return Err(error("kernel did not report broker socket mount identity"));
    }
    Ok(statx.stx_mnt_id)
}

// Walk from an open directory, never through a pathname supplied by an entry.
// Reopening each entry and comparing its inode catches ordinary replacement
// during the walk. Directory timestamps catch ordinary additions/removals.
// This remains a bounded, point-in-time inspection, not an immutable lease.
fn audit_descendants(directory: &File, reader: &impl QuotaReader, project_id: u32,
    mount_id: u64, device: u64, checked: &mut usize, depth: usize) -> io::Result<()> {
    if depth > MAX_DEPTH { return Err(error("quota descendant directory depth exceeds bound")); }
    let before = directory.metadata()?;
    let duplicate = directory.try_clone()?;
    let raw = duplicate.as_raw_fd();
    let stream = unsafe { libc::fdopendir(raw) };
    if stream.is_null() { return Err(io::Error::last_os_error()); }
    std::mem::forget(duplicate); // closed by closedir, including on error
    struct DirectoryStream(*mut libc::DIR);
    impl Drop for DirectoryStream {
        fn drop(&mut self) { unsafe { libc::closedir(self.0); } }
    }
    let stream = DirectoryStream(stream);
    loop {
        errno::set_errno(errno::Errno(0));
        let entry = unsafe { libc::readdir(stream.0) };
        if entry.is_null() {
            if errno::errno().0 != 0 { return Err(io::Error::last_os_error()); }
            break;
        }
        let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) };
        if name.to_bytes() == b"." || name.to_bytes() == b".." { continue; }
        *checked += 1;
        if *checked > MAX_DESCENDANTS { return Err(error("quota descendant count exceeds bound")); }
        let child = open_entry(directory, name)?;
        let meta = child.metadata()?;
        if !meta.is_file() && !meta.is_dir() {
            return Err(error("quota data bind contains a symlink or special entry"));
        }
        if meta.dev() != device { return Err(error("quota data bind crosses a filesystem boundary")); }
        let state = reader.project(&child).map_err(|_| error("quota descendant project metadata unavailable"))?;
        if state.id != project_id || state.mount_id != mount_id || (meta.is_dir() && !state.inherits) {
            return Err(error("quota descendant has wrong project ID, inheritance, or mount"));
        }
        if meta.is_dir() {
            audit_descendants(&child, reader, project_id, mount_id, device, checked, depth + 1)?;
        }
        let visible = open_entry(directory, name)?;
        let now = visible.metadata()?;
        if (meta.dev(), meta.ino(), meta.mode(), meta.mtime(), meta.mtime_nsec(), meta.ctime(), meta.ctime_nsec())
            != (now.dev(), now.ino(), now.mode(), now.mtime(), now.mtime_nsec(), now.ctime(), now.ctime_nsec()) {
            return Err(error("quota descendant changed during inspection"));
        }
    }
    let after = directory.metadata()?;
    if (before.dev(), before.ino(), before.mtime(), before.mtime_nsec(), before.ctime(), before.ctime_nsec())
        != (after.dev(), after.ino(), after.mtime(), after.mtime_nsec(), after.ctime(), after.ctime_nsec()) {
        return Err(error("quota directory changed during descendant inspection"));
    }
    Ok(())
}

fn open_entry(parent: &File, name: &CStr) -> io::Result<File> {
    let mut before: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstatat(parent.as_raw_fd(), name.as_ptr(), &mut before, libc::AT_SYMLINK_NOFOLLOW) } < 0 {
        return Err(io::Error::last_os_error());
    }
    let kind = before.st_mode as u32 & libc::S_IFMT as u32;
    if kind != libc::S_IFREG as u32 && kind != libc::S_IFDIR as u32 {
        return Err(error("quota data bind contains a symlink or special entry"));
    }
    // O_PATH pins a Linux inode without opening a device, FIFO or socket. If
    // the name changes after fstatat, the pinned descriptor is reclassified
    // before any read-capable descriptor is obtained.
    #[cfg(target_os = "linux")]
    let pinned = {
        let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(),
            libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        unsafe { File::from_raw_fd(fd) }
    };
    #[cfg(target_os = "linux")]
    {
        let meta = pinned.metadata()?;
        if !same_entry(&before, &meta) { return Err(error("quota descendant changed before open")); }
        // The procfd names the pinned inode, not the mutable directory entry.
        // The kernel quota ioctl needs a read-capable descriptor; an absent
        // procfs or refused reopen fails this snapshot closed.
        let path = CString::new(format!("/proc/self/fd/{}", pinned.as_raw_fd())).unwrap();
        let flags = libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NONBLOCK
            | if meta.is_dir() { libc::O_DIRECTORY } else { 0 };
        let fd = unsafe { libc::open(path.as_ptr(), flags) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        let opened = unsafe { File::from_raw_fd(fd) };
        if !same_entry(&before, &opened.metadata()?) {
            return Err(error("quota pinned descendant changed before ioctl"));
        }
        return Ok(opened);
    }
    #[cfg(not(target_os = "linux"))]
    {
        // Non-Linux uses only fake quota readers in tests; the kernel reader
        // refuses inspection. O_DIRECTORY excludes device replacement for a
        // directory entry, and identity comparison rejects other changes.
        let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK
            | if kind == libc::S_IFDIR as u32 { libc::O_DIRECTORY } else { 0 };
        let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), flags) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        let opened = unsafe { File::from_raw_fd(fd) };
        if !same_entry(&before, &opened.metadata()?) {
            return Err(error("quota descendant changed before open"));
        }
        Ok(opened)
    }
}

fn same_entry(before: &libc::stat, meta: &std::fs::Metadata) -> bool {
    before.st_dev as u64 == meta.dev() && before.st_ino as u64 == meta.ino()
        && before.st_mode as u32 == meta.mode()
}

fn private_metadata(directory: &File) -> io::Result<std::fs::Metadata> {
    let meta = directory.metadata()?;
    if !meta.is_dir() || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 {
        return Err(error("quota directory must be private and owner-owned"));
    }
    Ok(meta)
}

fn open_child(parent: &File, name: &str) -> io::Result<File> {
    let name = CString::new(name).map_err(|_| error("invalid quota directory component"))?;
    let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(),
        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK) };
    if fd < 0 { return Err(io::Error::last_os_error()); }
    Ok(unsafe { File::from_raw_fd(fd) })
}

fn open_absolute_directory(path: &Path) -> io::Result<File> {
    if !path.is_absolute() { return Err(error("quota directory path must be absolute")); }
    let mut directory = File::open("/")?;
    for component in path.components().skip(1) {
        let Component::Normal(name) = component else { return Err(error("unsafe quota directory component")); };
        let name = CString::new(name.as_bytes()).map_err(|_| error("invalid quota directory component"))?;
        let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        directory = unsafe { File::from_raw_fd(fd) };
    }
    Ok(directory)
}

struct KernelQuotaReader;

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    const FS_IOC_FSGETXATTR: libc::c_ulong = 0x801c581f;
    const FS_XFLAG_PROJINHERIT: u32 = 0x00000200;
    const XFS_SUPER_MAGIC: libc::c_long = 0x58465342;
    const EXT4_SUPER_MAGIC: libc::c_long = 0xEF53;
    const Q_XGETQUOTA: libc::c_int = 0x5803;
    const Q_XGETQSTATV: libc::c_int = 0x5808;
    const PRJQUOTA: libc::c_int = 2;
    const FS_PROJ_QUOTA: i8 = 2;
    const FS_QUOTA_PDQ_ACCT: u16 = 1 << 4;
    const FS_QUOTA_PDQ_ENFD: u16 = 1 << 5;
    fn qcmd(command: libc::c_int) -> libc::c_int { (command << 8) | PRJQUOTA }

    #[repr(C)]
    #[derive(Default)]
    struct Fsxattr { flags: u32, extsize: u32, nextents: u32, project_id: u32,
        cowextsize: u32, pad: [u8; 8] }

    // linux/dqblk_xfs.h. Only read commands are used.
    #[repr(C)]
    #[derive(Default)]
    struct FsDiskQuota {
        version: i8, flags: i8, fieldmask: u16, id: u32,
        blk_hardlimit: u64, blk_softlimit: u64, ino_hardlimit: u64, ino_softlimit: u64,
        bcount: u64, icount: u64, itimer: i32, btimer: i32,
        iwarns: u16, bwarns: u16, itimer_hi: i8, btimer_hi: i8,
        rtbtimer_hi: i8, padding2: i8, rtb_hardlimit: u64, rtb_softlimit: u64,
        rtbcount: u64, rtbtimer: i32, rtbwarns: u16, padding3: i16, padding4: [u8; 8],
    }
    #[repr(C)]
    #[derive(Default)]
    struct Qfilestat { ino: u64, blocks: u64, extents: u32, pad: u32 }
    #[repr(C)]
    #[derive(Default)]
    struct QuotaStatV {
        version: i8, pad1: u8, flags: u16, incore: u32,
        user: Qfilestat, group: Qfilestat, project: Qfilestat,
        btime: i32, itime: i32, rt_btime: i32, bwarn: u16, iwarn: u16,
        rt_bwarn: u16, pad3: u16, pad4: u32, pad2: [u64; 7],
    }
    fn quota_call<T>(file: &File, command: libc::c_int, id: u32, value: &mut T) -> io::Result<()> {
        let result = unsafe { libc::syscall(libc::SYS_quotactl_fd, file.as_raw_fd(), qcmd(command),
            id as libc::c_int, value as *mut T) };
        if result < 0 { Err(io::Error::last_os_error()) } else { Ok(()) }
    }
    fn supported_project_quota_filesystem(kind: libc::c_long) -> bool {
        kind == XFS_SUPER_MAGIC || kind == EXT4_SUPER_MAGIC
    }
    impl QuotaReader for KernelQuotaReader {
        fn project(&self, directory: &File) -> io::Result<ProjectState> {
            let mut fsx = Fsxattr::default();
            if unsafe { libc::ioctl(directory.as_raw_fd(), FS_IOC_FSGETXATTR, &mut fsx) } < 0 {
                return Err(io::Error::last_os_error());
            }
            let mut statx: libc::statx = unsafe { std::mem::zeroed() };
            let empty = c"";
            if unsafe { libc::statx(directory.as_raw_fd(), empty.as_ptr(),
                libc::AT_EMPTY_PATH | libc::AT_STATX_DONT_SYNC, libc::STATX_MNT_ID, &mut statx) } < 0 {
                return Err(io::Error::last_os_error());
            }
            if statx.stx_mask & libc::STATX_MNT_ID == 0 {
                return Err(error("kernel did not report mount identity"));
            }
            Ok(ProjectState { id: fsx.project_id,
                inherits: fsx.flags & FS_XFLAG_PROJINHERIT != 0, mount_id: statx.stx_mnt_id })
        }
        fn limit(&self, directory: &File, project_id: u32) -> io::Result<LimitState> {
            let mut filesystem: libc::statfs = unsafe { std::mem::zeroed() };
            if unsafe { libc::fstatfs(directory.as_raw_fd(), &mut filesystem) } < 0 {
                return Err(io::Error::last_os_error());
            }
            if !supported_project_quota_filesystem(filesystem.f_type) {
                return Err(error("descriptor-bound hard-limit query supports XFS or ext4 only"));
            }
            // Linux's generic quota dispatch implements Q_XGETQSTATV and
            // Q_XGETQUOTA for ext4 via dquot_get_state/get_dqblk. The former
            // exposes separate project accounting and enforcement bits;
            // the latter converts the hard limit to 512-byte blocks. Keep
            // both exact checks below. No quota configuration is changed.
            let mut status = QuotaStatV { version: 1, ..Default::default() };
            quota_call(directory, Q_XGETQSTATV, 0, &mut status)?;
            let mut quota = FsDiskQuota::default();
            quota_call(directory, Q_XGETQUOTA, project_id, &mut quota)?;
            if status.version != 1 || quota.version != 1 || quota.flags & FS_PROJ_QUOTA == 0 {
                return Err(error("kernel project quota response has an unexpected format"));
            }
            let hard_limit_bytes = quota.blk_hardlimit.checked_mul(512)
                .ok_or_else(|| error("project hard block limit overflows"))?;
            Ok(LimitState { id: quota.id, hard_limit_bytes,
                accounting: status.flags & FS_QUOTA_PDQ_ACCT != 0,
                enforcing: status.flags & FS_QUOTA_PDQ_ENFD != 0 })
        }
    }

    #[cfg(test)]
    #[test]
    fn kernel_quota_ffi_layout_matches_linux_uapi() {
        // linux/dqblk_xfs.h and linux/fs.h on the supported Linux ABIs.
        assert_eq!(std::mem::size_of::<Fsxattr>(), 28);
        assert_eq!(std::mem::size_of::<FsDiskQuota>(), 112);
        assert_eq!(std::mem::size_of::<QuotaStatV>(), 160);
        assert_eq!(std::mem::offset_of!(FsDiskQuota, blk_hardlimit), 8);
        assert_eq!(std::mem::offset_of!(QuotaStatV, flags), 2);
    }

    #[cfg(test)]
    #[test]
    fn descriptor_bound_quota_reader_accepts_only_reviewed_filesystems() {
        assert!(supported_project_quota_filesystem(XFS_SUPER_MAGIC));
        assert!(supported_project_quota_filesystem(EXT4_SUPER_MAGIC));
        assert!(!supported_project_quota_filesystem(0));
        assert!(!supported_project_quota_filesystem(0x01021994)); // tmpfs
    }
}

#[cfg(not(target_os = "linux"))]
impl QuotaReader for KernelQuotaReader {
    fn project(&self, _: &File) -> io::Result<ProjectState> { Err(error("Linux project quota inspection unavailable")) }
    fn limit(&self, _: &File, _: u32) -> io::Result<LimitState> { Err(error("Linux project quota inspection unavailable")) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::Cell, collections::HashMap, fs, os::unix::fs::{symlink, PermissionsExt}, path::PathBuf};

    struct FakeReader { states: HashMap<u64, ProjectState>, limit: io::Result<LimitState> }
    impl QuotaReader for FakeReader {
        fn project(&self, file: &File) -> io::Result<ProjectState> {
            self.states.get(&file.metadata()?.ino()).copied().ok_or_else(|| error("unknown inode"))
        }
        fn limit(&self, _: &File, _: u32) -> io::Result<LimitState> {
            self.limit.as_ref().copied().map_err(|_| error("fake quota read failure"))
        }
    }
    fn fixture() -> (tempfile::TempDir, Manifest, FakeReader, QuotaExpectation) {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        fs::set_permissions(root, fs::Permissions::from_mode(0o700)).unwrap();
        for name in ["checkout", "home", "cache", "broker"] {
            fs::create_dir(root.join(name)).unwrap();
            fs::set_permissions(root.join(name), fs::Permissions::from_mode(0o700)).unwrap();
        }
        let checkout = fs::metadata(root.join("checkout")).unwrap();
        let manifest = Manifest { version: 1, session_id: "fixture".into(), profile: Profile::DockerOffline,
            policy: None, policy_hash: String::new(), creation_policy_hash: String::new(),
            source: root.to_path_buf(), checkout: root.join("checkout"), context_cwd: None,
            provider_rollout: None, checkout_device: checkout.dev(), checkout_inode: checkout.ino(),
            base_sha: String::new(), branch: String::new(), private_home: root.join("home"),
            cache: root.join("cache"), broker: root.join("broker"), container_id: None,
            nonce: String::new(), state: "ready".into() };
        let states = [root.to_path_buf(), root.join("checkout"), root.join("home"), root.join("cache"), root.join("broker")]
            .into_iter().map(|path| (fs::metadata(path).unwrap().ino(), ProjectState { id: 41, inherits: true, mount_id: 9 })).collect();
        let reader = FakeReader { states, limit: Ok(LimitState { id: 41, hard_limit_bytes: 64 * 1024 * 1024,
            accounting: true, enforcing: true }) };
        (temp, manifest, reader, QuotaExpectation { project_id: 41, hard_limit_bytes: 64 * 1024 * 1024 })
    }
    #[test]
    fn exact_project_and_hard_limit_snapshot_is_necessary_but_not_admission() {
        let (_temp, manifest, reader, expected) = fixture();
        let snapshot = inspect_with(&manifest, expected, &reader).unwrap();
        assert_eq!(snapshot.project_id, 41);
        assert_eq!(snapshot.hard_limit_bytes, expected.hard_limit_bytes);
        assert_eq!(snapshot.descendants_checked, 0);
        assert_eq!(snapshot.broker_entries_checked, 0);
    }
    #[test]
    fn broker_root_requires_same_private_project_and_mount() {
        let (_temp, manifest, mut reader, expected) = fixture();
        let ino = fs::metadata(&manifest.broker).unwrap().ino();
        let original = reader.states[&ino];
        for altered in [ProjectState { id: 42, ..original },
            ProjectState { inherits: false, ..original },
            ProjectState { mount_id: 10, ..original }] {
            reader.states.insert(ino, altered);
            assert!(inspect_with(&manifest, expected, &reader).unwrap_err().to_string()
                .contains("session broker has wrong project"));
        }
        reader.states.insert(ino, original);
        fs::set_permissions(&manifest.broker, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(inspect_with(&manifest, expected, &reader).is_err());
    }
    #[test]
    fn broker_replacement_and_unknown_entries_refuse_snapshot() {
        let (temp, manifest, reader, expected) = fixture();
        fs::write(manifest.broker.join("unexpected"), b"x").unwrap();
        assert!(inspect_with(&manifest, expected, &reader).unwrap_err().to_string()
            .contains("unexpected entry"));
        fs::remove_file(manifest.broker.join("unexpected")).unwrap();
        fs::write(manifest.broker.join("hook.sock"), b"not a socket").unwrap();
        assert!(inspect_with(&manifest, expected, &reader).is_err());
        fs::remove_file(manifest.broker.join("hook.sock")).unwrap();
        symlink("/dev/null", manifest.broker.join("hook.sock")).unwrap();
        assert!(inspect_with(&manifest, expected, &reader).is_err());
        fs::remove_file(manifest.broker.join("hook.sock")).unwrap();
        fs::rename(&manifest.broker, temp.path().join("old-broker")).unwrap();
        symlink("old-broker", &manifest.broker).unwrap();
        assert!(inspect_with(&manifest, expected, &reader).is_err());
    }
    #[cfg(target_os = "linux")]
    #[test]
    fn only_private_owned_socket_entries_pass_broker_snapshot() {
        use std::os::unix::net::UnixListener;
        let (_temp, manifest, mut reader, expected) = fixture();
        let mount_id = entry_mount_id(&File::open(&manifest.broker).unwrap()).unwrap();
        for state in reader.states.values_mut() { state.mount_id = mount_id; }
        let hook = manifest.broker.join("hook.sock");
        let _hook = UnixListener::bind(&hook).unwrap();
        fs::set_permissions(&hook, fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(inspect_with(&manifest, expected, &reader).unwrap().broker_entries_checked, 1);
        let egress = manifest.broker.join("egress.sock");
        let _egress = UnixListener::bind(&egress).unwrap();
        fs::set_permissions(&egress, fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(inspect_with(&manifest, expected, &reader).unwrap().broker_entries_checked, 2);
        fs::set_permissions(&hook, fs::Permissions::from_mode(0o666)).unwrap();
        assert!(inspect_with(&manifest, expected, &reader).unwrap_err().to_string()
            .contains("unsafe identity or permissions"));
        fs::set_permissions(&hook, fs::Permissions::from_mode(0o600)).unwrap();
        for state in reader.states.values_mut() { state.mount_id = mount_id + 1; }
        assert!(inspect_with(&manifest, expected, &reader).unwrap_err().to_string()
            .contains("socket crosses a mount boundary"));
    }
    #[test]
    fn existing_data_bind_descendants_require_matching_project_and_inheritance() {
        let (_temp, manifest, mut reader, expected) = fixture();
        let nested = manifest.checkout.join("nested");
        fs::create_dir(&nested).unwrap();
        let file = nested.join("existing.txt");
        fs::write(&file, b"fixture").unwrap();
        let nested_ino = fs::metadata(&nested).unwrap().ino();
        let file_ino = fs::metadata(&file).unwrap().ino();
        let valid = ProjectState { id: 41, inherits: true, mount_id: 9 };
        reader.states.insert(nested_ino, valid);
        reader.states.insert(file_ino, valid);
        assert_eq!(inspect_with(&manifest, expected, &reader).unwrap().descendants_checked, 2);
        reader.states.insert(file_ino, ProjectState { id: 42, ..valid });
        assert!(inspect_with(&manifest, expected, &reader).unwrap_err().to_string()
            .contains("quota descendant has wrong project ID"));
        reader.states.insert(file_ino, ProjectState { mount_id: 10, ..valid });
        assert!(inspect_with(&manifest, expected, &reader).unwrap_err().to_string()
            .contains("quota descendant has wrong project ID"));
        reader.states.insert(file_ino, valid);
        reader.states.insert(nested_ino, ProjectState { inherits: false, ..valid });
        assert!(inspect_with(&manifest, expected, &reader).unwrap_err().to_string()
            .contains("quota descendant has wrong project ID"));
    }
    #[test]
    fn symlink_and_special_data_bind_entries_refuse_snapshot() {
        let (_temp, manifest, reader, expected) = fixture();
        let cache = File::open(&manifest.cache).unwrap();
        let shortcut = manifest.cache.join("shortcut");
        symlink(&manifest.private_home, &shortcut).unwrap();
        assert!(open_entry(&cache, c"shortcut").unwrap_err().to_string()
            .contains("symlink or special"));
        assert!(inspect_with(&manifest, expected, &reader).is_err());
        fs::remove_file(&shortcut).unwrap();
        let fifo = manifest.cache.join("pipe");
        let name = CString::new(fifo.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        assert!(open_entry(&cache, c"pipe").unwrap_err().to_string()
            .contains("symlink or special"));
        assert!(inspect_with(&manifest, expected, &reader).is_err());
        fs::remove_file(&fifo).unwrap();
        let _socket = std::os::unix::net::UnixListener::bind(manifest.cache.join("socket")).unwrap();
        assert!(open_entry(&cache, c"socket").unwrap_err().to_string()
            .contains("symlink or special"));
    }
    #[test]
    fn descendant_count_and_depth_are_bounded() {
        let (_temp, manifest, mut reader, expected) = fixture();
        let mut nested = manifest.checkout.clone();
        let valid = ProjectState { id: 41, inherits: true, mount_id: 9 };
        for _ in 0..=MAX_DEPTH {
            nested = nested.join("d");
            fs::create_dir(&nested).unwrap();
            reader.states.insert(fs::metadata(&nested).unwrap().ino(), valid);
        }
        assert!(inspect_with(&manifest, expected, &reader).unwrap_err().to_string().contains("depth exceeds bound"));
        fs::remove_dir_all(&manifest.checkout).unwrap();
        fs::create_dir(&manifest.checkout).unwrap();
        // The checkout inode changed, so exercise the same bounded walker
        // directly with one shared counter across all sibling files.
        let checkout = File::open(&manifest.checkout).unwrap();
        for index in 0..=MAX_DESCENDANTS {
            let path = manifest.checkout.join(format!("f{index}"));
            fs::write(&path, b"").unwrap();
            reader.states.insert(fs::metadata(path).unwrap().ino(), valid);
        }
        assert!(audit_descendants(&checkout, &reader, 41, 9, checkout.metadata().unwrap().dev(),
            &mut 0, 0).unwrap_err().to_string().contains("count exceeds bound"));
    }
    #[test]
    fn replacing_a_descendant_during_inspection_refuses_snapshot() {
        struct ReplacingReader { path: PathBuf, replaced: Cell<bool> }
        impl QuotaReader for ReplacingReader {
            fn project(&self, _: &File) -> io::Result<ProjectState> {
                if !self.replaced.replace(true) {
                    fs::remove_file(&self.path)?;
                    fs::write(&self.path, b"replacement")?;
                }
                Ok(ProjectState { id: 41, inherits: true, mount_id: 9 })
            }
            fn limit(&self, _: &File, _: u32) -> io::Result<LimitState> { unreachable!() }
        }
        let (_temp, manifest, _reader, _expected) = fixture();
        let file = manifest.checkout.join("file");
        fs::write(&file, b"original").unwrap();
        let checkout = File::open(&manifest.checkout).unwrap();
        let reader = ReplacingReader { path: file, replaced: Cell::new(false) };
        assert!(audit_descendants(&checkout, &reader, 41, 9, checkout.metadata().unwrap().dev(),
            &mut 0, 0).unwrap_err().to_string().contains("changed during inspection"));
        assert!(reader.replaced.get());
    }
    #[test]
    fn mismatched_project_mount_inheritance_limit_or_enforcement_refuses() {
        let (_temp, manifest, mut reader, expected) = fixture();
        for path in [&manifest.checkout, &manifest.private_home, &manifest.cache] {
            let ino = fs::metadata(path).unwrap().ino();
            let original = reader.states[&ino];
            for altered in [ProjectState { id: 42, ..original }, ProjectState { inherits: false, ..original },
                ProjectState { mount_id: 10, ..original }] {
                reader.states.insert(ino, altered);
                assert!(inspect_with(&manifest, expected, &reader).is_err());
            }
            reader.states.insert(ino, original);
        }
        let good = reader.limit.as_ref().unwrap().to_owned();
        for altered in [LimitState { id: 42, ..good }, LimitState { hard_limit_bytes: good.hard_limit_bytes + 512, ..good },
            LimitState { accounting: false, ..good }, LimitState { enforcing: false, ..good }] {
            reader.limit = Ok(altered);
            assert!(inspect_with(&manifest, expected, &reader).is_err());
        }
        reader.limit = Err(error("unavailable"));
        assert!(inspect_with(&manifest, expected, &reader).is_err());
    }
    #[test]
    fn symlink_replacement_and_checkout_inode_change_refuse() {
        let (temp, mut manifest, reader, expected) = fixture();
        manifest.checkout_inode += 1;
        assert!(inspect_with(&manifest, expected, &reader).is_err());
        manifest.checkout_inode -= 1;
        fs::rename(manifest.cache.clone(), temp.path().join("old-cache")).unwrap();
        symlink("old-cache", &manifest.cache).unwrap();
        assert!(inspect_with(&manifest, expected, &reader).is_err());
    }
    #[test]
    fn malformed_policy_and_escaped_paths_refuse() {
        let (_temp, mut manifest, reader, expected) = fixture();
        assert!(inspect_with(&manifest, QuotaExpectation { project_id: 0, ..expected }, &reader).is_err());
        assert!(inspect_with(&manifest, QuotaExpectation { project_id: u32::MAX, ..expected }, &reader).is_err());
        assert!(inspect_with(&manifest, QuotaExpectation { hard_limit_bytes: 0, ..expected }, &reader).is_err());
        assert!(inspect_with(&manifest, QuotaExpectation { hard_limit_bytes: 513, ..expected }, &reader).is_err());
        manifest.private_home = PathBuf::from("/outside/home");
        assert!(inspect_with(&manifest, expected, &reader).is_err());
    }
}
