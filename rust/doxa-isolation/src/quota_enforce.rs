//! Administrator-provisioned project quota capabilities for the root gateway.
//!
//! The current kernel quota is only one part of admission. The gateway must
//! independently attest the exact Engine, image and mandatory worker filter.
//! No receipt or caller-supplied verification bit is accepted by this module.
use crate::{
    error,
    quota_verify::{
        self, QuotaBinding, QuotaBindings, QuotaExpectation, QuotaSnapshot, RootControlledQuotaTree,
    },
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    ffi::CString,
    fs::File,
    io::{self, Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::MetadataExt,
    },
    path::{Component, Path, PathBuf},
};

pub const POLICY_DIRECTORY: &str = "/etc/doxa/quota-enforced";
pub const SECURITY_CONTRACT_VERSION: u32 = 1;
const MAX_POLICY_BYTES: u64 = 16 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EnforcementPolicy {
    pub version: u32,
    pub session_id: String,
    pub root: PathBuf,
    pub caller_uid: u32,
    pub engine_uid: u32,
    pub engine_gid: u32,
    pub project_id: u32,
    pub hard_limit_bytes: u64,
    pub boot_id: String,
    pub bindings: QuotaBindings,
    pub worker_image: String,
    pub worker_binary_sha256: String,
    pub security_contract_version: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct QuotaPolicyIdentity {
    pub device: u64,
    pub inode: u64,
    pub sha256: String,
}

#[derive(Debug)]
pub struct InstalledQuotaPolicy {
    pub policy: EnforcementPolicy,
    pub identity: QuotaPolicyIdentity,
}

/// Retained directory capabilities and administrator policy identity. This
/// type makes no admission claim and cannot be constructed from JSON receipts.
#[derive(Debug)]
pub struct EnforcedQuotaSnapshot {
    pub policy: EnforcementPolicy,
    pub policy_identity: QuotaPolicyIdentity,
    pub quota: QuotaSnapshot,
    pub tree: RootControlledQuotaTree,
}

impl EnforcementPolicy {
    pub fn validate(&self) -> io::Result<()> {
        validate_session_id(&self.session_id)?;
        if self.version != 2 || self.security_contract_version != SECURITY_CONTRACT_VERSION {
            return Err(error(
                "unsupported root quota policy or worker security contract",
            ));
        }
        validate_boot_id(&self.boot_id)?;
        validate_scope(
            &self.session_id,
            &self.root,
            self.caller_uid,
            self.engine_uid,
            self.engine_gid,
            self.project_id,
            self.hard_limit_bytes,
            &self.worker_image,
            &self.worker_binary_sha256,
        )?;
        validate_bindings(&self.bindings)
    }
}

impl InstalledQuotaPolicy {
    pub fn verify_for_gateway(&self) -> io::Result<EnforcedQuotaSnapshot> {
        require_root()?;
        self.policy.validate()?;
        if self.policy.boot_id != kernel_boot_id()? {
            return Err(error(
                "administrator quota policy belongs to another kernel boot",
            ));
        }
        let current = load_root_enforcement_policy(&self.policy.session_id)?;
        if current.identity != self.identity || current.policy != self.policy {
            return Err(error(
                "administrator quota policy changed after it was pinned",
            ));
        }
        let tree = quota_verify::inspect_root_controlled_session_hard_quota(
            &self.policy.root,
            QuotaExpectation {
                project_id: self.policy.project_id,
                hard_limit_bytes: self.policy.hard_limit_bytes,
            },
            self.policy.engine_gid,
            &self.policy.bindings,
        )?;
        Ok(EnforcedQuotaSnapshot {
            policy: self.policy.clone(),
            policy_identity: self.identity.clone(),
            quota: tree.snapshot,
            tree,
        })
    }
}

impl EnforcedQuotaSnapshot {
    /// Recheck before every root-controlled Engine operation. A remount needs
    /// explicit administrator reprovisioning; stale mount IDs fail closed.
    pub fn revalidate(&self) -> io::Result<()> {
        let current = load_root_enforcement_policy(&self.policy.session_id)?;
        if current.identity != self.policy_identity || current.policy != self.policy {
            return Err(error(
                "administrator quota policy changed during gateway lifetime",
            ));
        }
        current.verify_for_gateway()?;
        Ok(())
    }
}

pub fn validate_session_id(id: &str) -> io::Result<()> {
    if id.is_empty()
        || id.len() > 128
        || !id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
    {
        return Err(error("invalid root quota session identity"));
    }
    Ok(())
}

fn safe_absolute(path: &Path) -> bool {
    path.is_absolute()
        && path
            .components()
            .skip(1)
            .all(|part| matches!(part, Component::Normal(_)))
}

fn digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn validate_boot_id(id: &str) -> io::Result<()> {
    if id.len() != 36
        || !id.bytes().enumerate().all(|(i, b)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                b == b'-'
            } else {
                b.is_ascii_hexdigit() && !b.is_ascii_uppercase()
            }
        })
    {
        return Err(error(
            "root quota policy has an invalid kernel boot identity",
        ));
    }
    Ok(())
}

fn kernel_boot_id() -> io::Result<String> {
    let id = std::fs::read_to_string("/proc/sys/kernel/random/boot_id")?;
    let id = id.trim().to_owned();
    validate_boot_id(&id)?;
    Ok(id)
}

#[allow(clippy::too_many_arguments)]
fn validate_scope(
    id: &str,
    root: &Path,
    caller_uid: u32,
    engine_uid: u32,
    engine_gid: u32,
    project_id: u32,
    hard_limit_bytes: u64,
    image: &str,
    worker_sha: &str,
) -> io::Result<()> {
    validate_session_id(id)?;
    if !safe_absolute(root) || root.file_name().is_none_or(|name| name != id) {
        return Err(error(
            "root quota session path must be absolute and end in its session identity",
        ));
    }
    if caller_uid == 0 || engine_uid == 0 || engine_gid == 0 || caller_uid == engine_uid {
        return Err(error(
            "root quota requires separate non-root caller and Engine identities",
        ));
    }
    if project_id == 0
        || project_id > i32::MAX as u32
        || hard_limit_bytes == 0
        || hard_limit_bytes % 512 != 0
        || hard_limit_bytes > 1024 * 1024 * 1024 * 1024
    {
        return Err(error(
            "root quota requires a finite exact project hard block limit",
        ));
    }
    let image_digest = image.strip_prefix("sha256:").or_else(|| {
        image
            .split_once("@sha256:")
            .filter(|(name, _)| {
                !name.is_empty()
                    && !name.starts_with('-')
                    && name
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"./:_-".contains(&b))
            })
            .map(|(_, hash)| hash)
    });
    if !image_digest.is_some_and(digest) || !digest(worker_sha) {
        return Err(error(
            "root quota requires a content-pinned image and reviewed worker SHA-256",
        ));
    }
    Ok(())
}

fn validate_bindings(bindings: &QuotaBindings) -> io::Result<()> {
    let entries = [
        bindings.root,
        bindings.checkout,
        bindings.home,
        bindings.cache,
        bindings.broker,
    ];
    if entries[0].device == 0
        || entries[0].mount_id == 0
        || entries.iter().any(|entry| {
            entry.device != entries[0].device
                || entry.mount_id != entries[0].mount_id
                || entry.inode == 0
        })
        || entries
            .iter()
            .enumerate()
            .any(|(i, entry)| entries[..i].iter().any(|old| old.inode == entry.inode))
    {
        return Err(error(
            "root quota policy has invalid or aliased bind identities",
        ));
    }
    Ok(())
}

fn require_root() -> io::Result<()> {
    if unsafe { libc::geteuid() } != 0 {
        return Err(error("root quota authority requires root"));
    }
    Ok(())
}

fn root_policy_file(directory: &File, id: &str) -> io::Result<File> {
    let name = CString::new(format!("{id}.json")).unwrap();
    let fd = unsafe {
        libc::openat(
            directory.as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let file = unsafe { File::from_raw_fd(fd) };
    let meta = file.metadata()?;
    if !meta.is_file()
        || meta.uid() != 0
        || meta.nlink() != 1
        || meta.mode() & 0o7777 != 0o600
        || meta.len() == 0
        || meta.len() > MAX_POLICY_BYTES
    {
        return Err(error(
            "root quota policy must be a bounded single-link root-owned mode-0600 file",
        ));
    }
    Ok(file)
}

/// The session ID only selects the fixed administrator directory. No
/// environment variable, arbitrary pathname or helper receipt can redirect it.
pub fn load_root_enforcement_policy(id: &str) -> io::Result<InstalledQuotaPolicy> {
    require_root()?;
    validate_session_id(id)?;
    let directory = quota_verify::open_root_controlled_directory(Path::new(POLICY_DIRECTORY))?;
    let meta = directory.metadata()?;
    if meta.uid() != 0 || meta.mode() & 0o022 != 0 {
        return Err(error(
            "root quota policy directory is not administrator controlled",
        ));
    }
    let file = root_policy_file(&directory, id)?;
    let meta = file.metadata()?;
    let mut bytes = Vec::new();
    file.take(MAX_POLICY_BYTES + 1).read_to_end(&mut bytes)?;
    if bytes.len() > MAX_POLICY_BYTES as usize {
        return Err(error("root quota policy exceeds bound"));
    }
    let policy: EnforcementPolicy = serde_json::from_slice(&bytes)
        .map_err(|_| error("root quota policy is malformed or has unknown fields"))?;
    policy.validate()?;
    if policy.session_id != id {
        return Err(error(
            "root quota policy differs from its installed session identity",
        ));
    }
    let visible = root_policy_file(&directory, id)?.metadata()?;
    if (
        meta.dev(),
        meta.ino(),
        meta.len(),
        meta.ctime(),
        meta.ctime_nsec(),
    ) != (
        visible.dev(),
        visible.ino(),
        visible.len(),
        visible.ctime(),
        visible.ctime_nsec(),
    ) {
        return Err(error("root quota policy changed while loading"));
    }
    Ok(InstalledQuotaPolicy {
        policy,
        identity: QuotaPolicyIdentity {
            device: meta.dev(),
            inode: meta.ino(),
            sha256: format!("{:x}", Sha256::digest(&bytes)),
        },
    })
}

/// Root administrator inputs for creation of a new empty session. There are
/// intentionally no caller-supplied bindings or success/proof flags.
pub struct ProvisionRequest {
    pub session_id: String,
    pub root: PathBuf,
    pub caller_uid: u32,
    pub engine_uid: u32,
    pub engine_gid: u32,
    pub project_id: u32,
    pub hard_limit_bytes: u64,
    pub worker_image: String,
    pub worker_binary_sha256: String,
}

fn create_directory(parent: &File, name: &str, gid: u32, mode: u32) -> io::Result<File> {
    let name = CString::new(name).map_err(|_| error("unsafe provisioning directory name"))?;
    if unsafe { libc::mkdirat(parent.as_raw_fd(), name.as_ptr(), 0o700) } < 0 {
        return Err(io::Error::last_os_error());
    }
    let fd = unsafe {
        libc::openat(
            parent.as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let directory = unsafe { File::from_raw_fd(fd) };
    if unsafe { libc::fchown(fd, 0, gid) } < 0 || unsafe { libc::fchmod(fd, mode) } < 0 {
        return Err(io::Error::last_os_error());
    }
    directory.sync_all()?;
    parent.sync_all()?;
    Ok(directory)
}

fn ensure_policy_directory() -> io::Result<File> {
    let mut parent = quota_verify::open_root_controlled_directory(Path::new("/etc"))?;
    for name in ["doxa", "quota-enforced"] {
        let component = CString::new(name).unwrap();
        if unsafe { libc::mkdirat(parent.as_raw_fd(), component.as_ptr(), 0o755) } < 0
            && io::Error::last_os_error().kind() != io::ErrorKind::AlreadyExists
        {
            return Err(io::Error::last_os_error());
        }
        let fd = unsafe {
            libc::openat(
                parent.as_raw_fd(),
                component.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        let next = unsafe { File::from_raw_fd(fd) };
        let meta = next.metadata()?;
        if meta.uid() != 0 || meta.mode() & 0o022 != 0 {
            return Err(error(
                "quota installation directory is not administrator controlled",
            ));
        }
        parent.sync_all()?;
        parent = next;
    }
    Ok(parent)
}

fn allocation_lock(directory: &File) -> io::Result<File> {
    let fd = unsafe {
        libc::openat(
            directory.as_raw_fd(),
            c".allocation.lock".as_ptr(),
            libc::O_RDWR | libc::O_CREAT | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o600,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let file = unsafe { File::from_raw_fd(fd) };
    let meta = file.metadata()?;
    if !meta.is_file() || meta.uid() != 0 || meta.nlink() != 1 || meta.mode() & 0o7777 != 0o600 {
        return Err(error(
            "quota allocation lock is not administrator controlled",
        ));
    }
    if unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) } < 0 {
        return Err(error(
            "another root quota administrator is provisioning a session",
        ));
    }
    Ok(file)
}

/// Create an empty tree and provision its actual kernel quota. Existing trees,
/// allocated project IDs, unsafe ancestors and existing policies fail closed.
/// This never enables an Engine or asserts hardened admission. If provisioning
/// fails after a kernel change, the root-owned tree/project remains for explicit
/// administrator cleanup and no policy is installed.
#[cfg(target_os = "linux")]
pub fn provision_session(request: ProvisionRequest) -> io::Result<InstalledQuotaPolicy> {
    require_root()?;
    validate_scope(
        &request.session_id,
        &request.root,
        request.caller_uid,
        request.engine_uid,
        request.engine_gid,
        request.project_id,
        request.hard_limit_bytes,
        &request.worker_image,
        &request.worker_binary_sha256,
    )?;
    let parent_path = request
        .root
        .parent()
        .ok_or_else(|| error("session root has no parent"))?;
    let parent = quota_verify::open_root_controlled_directory(parent_path)?;
    let meta = parent.metadata()?;
    if meta.uid() != 0 || meta.mode() & 0o022 != 0 {
        return Err(error("session root parent is not administrator controlled"));
    }
    let policy_dir = ensure_policy_directory()?;
    let _allocation = allocation_lock(&policy_dir)?;
    let policy_name = CString::new(format!("{}.json", request.session_id)).unwrap();
    let mut exists: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe {
        libc::fstatat(
            policy_dir.as_raw_fd(),
            policy_name.as_ptr(),
            &mut exists,
            libc::AT_SYMLINK_NOFOLLOW,
        )
    } == 0
    {
        return Err(error("session quota policy already exists"));
    }
    if io::Error::last_os_error().kind() != io::ErrorKind::NotFound {
        return Err(io::Error::last_os_error());
    }
    // Keep the entire tree inaccessible to the Engine until every root-owned
    // bind has its project attributes and the exact hard limit is active.
    let root = create_directory(&parent, &request.session_id, request.engine_gid, 0o700)?;
    quota_verify::provision_project_quota(
        &root,
        QuotaExpectation {
            project_id: request.project_id,
            hard_limit_bytes: request.hard_limit_bytes,
        },
    )?;
    let mut children = Vec::new();
    for (name, mode) in [
        ("checkout", 0o770),
        ("home", 0o770),
        ("cache", 0o770),
        ("broker", 0o750),
    ] {
        let child = create_directory(&root, name, request.engine_gid, mode)?;
        quota_verify::set_project_inheritance(&child, request.project_id)?;
        child.sync_all()?;
        children.push(child);
    }
    if unsafe { libc::fchmod(root.as_raw_fd(), 0o750) } < 0 {
        return Err(io::Error::last_os_error());
    }
    root.sync_all()?;
    let bind = |file: &File| quota_verify::capture_quota_binding(file);
    let policy = EnforcementPolicy {
        version: 2,
        session_id: request.session_id.clone(),
        root: request.root,
        caller_uid: request.caller_uid,
        engine_uid: request.engine_uid,
        engine_gid: request.engine_gid,
        project_id: request.project_id,
        hard_limit_bytes: request.hard_limit_bytes,
        boot_id: kernel_boot_id()?,
        bindings: QuotaBindings {
            root: bind(&root)?,
            checkout: bind(&children[0])?,
            home: bind(&children[1])?,
            cache: bind(&children[2])?,
            broker: bind(&children[3])?,
        },
        worker_image: request.worker_image,
        worker_binary_sha256: request.worker_binary_sha256,
        security_contract_version: SECURITY_CONTRACT_VERSION,
    };
    policy.validate()?;
    quota_verify::inspect_root_controlled_session_hard_quota(
        &policy.root,
        QuotaExpectation {
            project_id: policy.project_id,
            hard_limit_bytes: policy.hard_limit_bytes,
        },
        policy.engine_gid,
        &policy.bindings,
    )?;
    let fd = unsafe {
        libc::openat(
            policy_dir.as_raw_fd(),
            policy_name.as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o600,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let mut file = unsafe { File::from_raw_fd(fd) };
    serde_json::to_writer_pretty(&mut file, &policy)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    policy_dir.sync_all()?;
    let installed = load_root_enforcement_policy(&policy.session_id)?;
    installed.verify_for_gateway()?;
    Ok(installed)
}

/// Explicit administrator rebinding after a source remount. Only mount IDs
/// may change: device and inode for all five root-controlled directories,
/// project attributes and effective hard limit must remain exact. Existing
/// gateway/helper leases pin the old policy inode/hash and refuse until they
/// are deliberately restarted with this replacement.
pub fn refresh_policy_after_remount(id: &str) -> io::Result<InstalledQuotaPolicy> {
    require_root()?;
    let installed = load_root_enforcement_policy(id)?;
    let directory = quota_verify::open_root_controlled_directory(Path::new(POLICY_DIRECTORY))?;
    let _allocation = allocation_lock(&directory)?;
    let mut policy = installed.policy;
    let root = quota_verify::open_root_controlled_directory(&policy.root)?;
    let capture = |name: Option<&str>, old: QuotaBinding| -> io::Result<QuotaBinding> {
        let file = match name {
            None => root.try_clone()?,
            Some(name) => {
                let name = CString::new(name).unwrap();
                let fd = unsafe {
                    libc::openat(
                        root.as_raw_fd(),
                        name.as_ptr(),
                        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                    )
                };
                if fd < 0 {
                    return Err(io::Error::last_os_error());
                }
                unsafe { File::from_raw_fd(fd) }
            }
        };
        let current = quota_verify::capture_quota_binding(&file)?;
        if (current.device, current.inode) != (old.device, old.inode) {
            return Err(error(
                "quota remount changed a pinned directory device or inode",
            ));
        }
        Ok(current)
    };
    policy.bindings = QuotaBindings {
        root: capture(None, policy.bindings.root)?,
        checkout: capture(Some("checkout"), policy.bindings.checkout)?,
        home: capture(Some("home"), policy.bindings.home)?,
        cache: capture(Some("cache"), policy.bindings.cache)?,
        broker: capture(Some("broker"), policy.bindings.broker)?,
    };
    policy.boot_id = kernel_boot_id()?;
    policy.validate()?;
    quota_verify::inspect_root_controlled_session_hard_quota(
        &policy.root,
        QuotaExpectation {
            project_id: policy.project_id,
            hard_limit_bytes: policy.hard_limit_bytes,
        },
        policy.engine_gid,
        &policy.bindings,
    )?;
    let staging = CString::new(format!(".{id}.remount-{}", std::process::id())).unwrap();
    let target = CString::new(format!("{id}.json")).unwrap();
    let fd = unsafe {
        libc::openat(
            directory.as_raw_fd(),
            staging.as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o600,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let mut file = unsafe { File::from_raw_fd(fd) };
    serde_json::to_writer_pretty(&mut file, &policy)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    if unsafe {
        libc::renameat(
            directory.as_raw_fd(),
            staging.as_ptr(),
            directory.as_raw_fd(),
            target.as_ptr(),
        )
    } < 0
    {
        return Err(io::Error::last_os_error());
    }
    directory.sync_all()?;
    let updated = load_root_enforcement_policy(id)?;
    updated.verify_for_gateway()?;
    Ok(updated)
}

#[cfg(not(target_os = "linux"))]
pub fn provision_session(_: ProvisionRequest) -> io::Result<InstalledQuotaPolicy> {
    Err(error("root project quota provisioning requires Linux"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy() -> EnforcementPolicy {
        let bind = |inode| QuotaBinding {
            device: 5,
            inode,
            mount_id: 9,
        };
        EnforcementPolicy {
            version: 2,
            session_id: "session-1".into(),
            root: "/srv/doxa/session-1".into(),
            caller_uid: 1000,
            engine_uid: 1001,
            engine_gid: 1001,
            project_id: 5001,
            hard_limit_bytes: 32 * 1024 * 1024,
            boot_id: "11111111-1111-4111-8111-111111111111".into(),
            bindings: QuotaBindings {
                root: bind(1),
                checkout: bind(2),
                home: bind(3),
                cache: bind(4),
                broker: bind(5),
            },
            worker_image: format!("sha256:{}", "a".repeat(64)),
            worker_binary_sha256: "b".repeat(64),
            security_contract_version: 1,
        }
    }

    #[test]
    fn scoped_policy_requires_separate_uids_exact_limits_and_worker_contract() {
        let good = policy();
        good.validate().unwrap();
        let mut bad = good.clone();
        bad.engine_uid = bad.caller_uid;
        assert!(bad.validate().is_err());
        let mut bad = good.clone();
        bad.engine_gid = 0;
        assert!(bad.validate().is_err());
        let mut bad = good.clone();
        bad.hard_limit_bytes = 513;
        assert!(bad.validate().is_err());
        let mut bad = good.clone();
        bad.root = "/srv/doxa/other".into();
        assert!(bad.validate().is_err());
        let mut bad = good.clone();
        bad.root = "/srv/../doxa/session-1".into();
        assert!(bad.validate().is_err());
        let mut bad = good.clone();
        bad.worker_image = "python:latest".into();
        assert!(bad.validate().is_err());
        let mut bad = good.clone();
        bad.worker_binary_sha256 = "A".repeat(64);
        assert!(bad.validate().is_err());
        let mut bad = good.clone();
        bad.security_contract_version = 0;
        assert!(bad.validate().is_err());
        let mut bad = good.clone();
        bad.version = 1;
        assert!(bad.validate().is_err());
        let mut bad = good.clone();
        bad.boot_id = "missing".into();
        assert!(bad.validate().is_err());
        let mut bad = good.clone();
        bad.bindings.cache = bad.bindings.checkout;
        assert!(bad.validate().is_err());
        let mut bad = good;
        bad.bindings.home.mount_id += 1;
        assert!(bad.validate().is_err());
    }

    #[test]
    fn receipts_and_unknown_admission_flags_cannot_become_root_policies() {
        let mut value = serde_json::to_value(policy()).unwrap();
        value["admissible_as_hard_quota"] = serde_json::json!(true);
        assert!(serde_json::from_value::<EnforcementPolicy>(value).is_err());
        assert!(
            serde_json::from_str::<EnforcementPolicy>(r#"{"verified":true,"errno":122}"#).is_err()
        );
        for id in ["", "..", "../other", "a/b", "session_1", "s.json", "é"] {
            assert!(validate_session_id(id).is_err());
        }
    }

    #[test]
    fn nonroot_cannot_load_or_provision_authority() {
        if unsafe { libc::geteuid() } == 0 {
            return;
        }
        assert!(load_root_enforcement_policy("session-1")
            .unwrap_err()
            .to_string()
            .contains("requires root"));
    }
}
