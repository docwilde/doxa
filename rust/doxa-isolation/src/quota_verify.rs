//! Read-only, descriptor-anchored project-quota snapshot for a session tree.
//!
//! This is one necessary admission check, not a durable enforcement proof:
//! descendants, Docker bind behavior, EDQUOT, and restart/remount still need
//! independent verification before a hardened profile may be enabled.
use crate::{error, Manifest, Profile};
use std::{ffi::CString, fs::File, io, os::fd::{AsRawFd, FromRawFd},
    os::unix::{ffi::OsStrExt, fs::MetadataExt}, path::{Component, Path}};

const MAX_HARD_BYTES: u64 = 1024 * 1024 * 1024 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct QuotaExpectation { pub project_id: u32, pub hard_limit_bytes: u64 }

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct QuotaSnapshot { pub project_id: u32, pub hard_limit_bytes: u64,
    pub mount_id: u64, pub filesystem_device: u64 }

#[derive(Clone, Copy, Debug)]
struct ProjectState { id: u32, inherits: bool, mount_id: u64 }

#[derive(Clone, Copy, Debug)]
struct LimitState { id: u32, hard_limit_bytes: u64, accounting: bool, enforcing: bool }

trait QuotaReader {
    fn project(&self, directory: &File) -> io::Result<ProjectState>;
    fn limit(&self, directory: &File, project_id: u32) -> io::Result<LimitState>;
}

/// Verify the *current* four directory inodes and the effective project quota
/// on their filesystem. The expectation must come from an owner-controlled
/// session policy; a fixture receipt alone is not such a policy.
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
        // Re-open the visible path after reading the descriptor to reject a
        // replaced bind source. This remains a snapshot, not a race-proof lease.
        let visible = open_absolute_directory(path)?;
        let visible_meta = visible.metadata()?;
        if (meta.dev(), meta.ino()) != (visible_meta.dev(), visible_meta.ino()) {
            return Err(error("session bind source changed during quota inspection"));
        }
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
        mount_id: root_state.mount_id, filesystem_device: root_meta.dev() })
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
            if filesystem.f_type != XFS_SUPER_MAGIC {
                return Err(error("descriptor-bound hard-limit query currently supports XFS only"));
            }
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
}

#[cfg(not(target_os = "linux"))]
impl QuotaReader for KernelQuotaReader {
    fn project(&self, _: &File) -> io::Result<ProjectState> { Err(error("Linux project quota inspection unavailable")) }
    fn limit(&self, _: &File, _: u32) -> io::Result<LimitState> { Err(error("Linux project quota inspection unavailable")) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{collections::HashMap, fs, os::unix::fs::{symlink, PermissionsExt}, path::PathBuf};

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
        let states = [root.to_path_buf(), root.join("checkout"), root.join("home"), root.join("cache")]
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
