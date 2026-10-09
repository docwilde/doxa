//! Opt-in, requestless privileged project-quota inspector.
//!
//! An administrator installs one root-owned policy per session and a Unix
//! socket-activated service. A peer can only ask for the pinned tree's current
//! read-only snapshot by connecting; it cannot supply paths, IDs or limits.
//! This is evidence, never hardened admission or quota configuration.
use crate::{error, quota_verify::{inspect_pinned_session_hard_quota, QuotaBindings,
    QuotaExpectation, QuotaSnapshot}, Manifest, Profile};
use serde::{Deserialize, Serialize};
use std::{ffi::CString, fs::File, io::{self, Read, Write},
    os::{fd::{AsRawFd, FromRawFd}, unix::{ffi::OsStrExt, fs::{FileTypeExt, MetadataExt}, net::{UnixListener, UnixStream}}},
    path::{Component, Path, PathBuf}, time::{Duration, Instant}};

const MAX_POLICY: u64 = 8192;
const MAX_REPLY: u64 = 4096;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct QuotaHelperPolicy {
    pub version: u32,
    pub session_id: String,
    pub root: PathBuf,
    pub socket_path: PathBuf,
    pub caller_uid: u32,
    pub owner_uid: u32,
    pub project_id: u32,
    pub hard_limit_bytes: u64,
    pub bindings: QuotaBindings,
}

#[derive(Debug, Serialize)]
pub struct QuotaHelperResult<'a> {
    pub version: u32,
    pub session_id: &'a str,
    pub root: &'a Path,
    pub owner_uid: u32,
    pub bindings: &'a QuotaBindings,
    pub verified: bool,
    pub admissible_as_hard_quota: bool,
    pub project_id: Option<u32>,
    pub hard_limit_bytes: Option<u64>,
    pub mount_id: Option<u64>,
    pub descendants_checked: Option<usize>,
    pub broker_entries_checked: Option<usize>,
}

impl QuotaHelperPolicy {
    pub fn validate(&self) -> io::Result<()> {
        if self.version != 1 || self.session_id.is_empty() || self.session_id.len() > 128
            || !self.session_id.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-') {
            return Err(error("invalid quota helper policy or session identity"));
        }
        if !safe_absolute(&self.root) || self.root.file_name().is_none_or(|name| name != self.session_id.as_str())
            || !safe_absolute(&self.socket_path) {
            return Err(error("quota helper policy has an unsafe root or socket path"));
        }
        // Rootless workers and the trusted controller must have separate host
        // UIDs. A same-UID process could otherwise request another snapshot.
        if self.caller_uid == 0 || self.owner_uid == 0 || self.caller_uid == self.owner_uid {
            return Err(error("quota helper requires distinct non-root caller and tree owner UIDs"));
        }
        if self.project_id == 0 || self.project_id > i32::MAX as u32
            || self.hard_limit_bytes == 0 || self.hard_limit_bytes % 512 != 0
            || self.hard_limit_bytes > 1024 * 1024 * 1024 * 1024 {
            return Err(error("quota helper policy has an invalid finite project limit"));
        }
        let identities = [self.bindings.root, self.bindings.checkout, self.bindings.home,
            self.bindings.cache, self.bindings.broker];
        let first = identities[0];
        if first.device == 0 || first.inode == 0 || first.mount_id == 0
            || identities.iter().any(|entry| entry.device != first.device
                || entry.mount_id != first.mount_id || entry.inode == 0)
            || identities.iter().enumerate().any(|(i, entry)|
                identities[..i].iter().any(|prior| prior.inode == entry.inode)) {
            return Err(error("quota helper policy has invalid or aliased bind identities"));
        }
        Ok(())
    }

    fn manifest(&self) -> Manifest {
        Manifest { version: 1, session_id: self.session_id.clone(), profile: Profile::DockerOffline,
            policy: None, policy_hash: String::new(), creation_policy_hash: String::new(),
            source: self.root.clone(), checkout: self.root.join("checkout"), context_cwd: None,
            provider_rollout: None, checkout_device: self.bindings.checkout.device,
            checkout_inode: self.bindings.checkout.inode, base_sha: String::new(),
            branch: String::new(), private_home: self.root.join("home"),
            cache: self.root.join("cache"), broker: self.root.join("broker"),
            container_id: None, nonce: String::new(), state: "ready".into() }
    }

    pub fn inspect(&self) -> io::Result<QuotaSnapshot> {
        self.validate()?;
        inspect_pinned_session_hard_quota(&self.manifest(), QuotaExpectation {
            project_id: self.project_id, hard_limit_bytes: self.hard_limit_bytes,
        }, self.owner_uid, &self.bindings)
    }

    pub fn result(&self) -> QuotaHelperResult<'_> {
        let snapshot = self.inspect().ok();
        QuotaHelperResult { version: 2, session_id: &self.session_id,
            root: &self.root, owner_uid: self.owner_uid, bindings: &self.bindings,
            verified: snapshot.is_some(), admissible_as_hard_quota: false,
            project_id: snapshot.as_ref().map(|value| value.project_id),
            hard_limit_bytes: snapshot.as_ref().map(|value| value.hard_limit_bytes),
            mount_id: snapshot.as_ref().map(|value| value.mount_id),
            descendants_checked: snapshot.as_ref().map(|value| value.descendants_checked),
            broker_entries_checked: snapshot.as_ref().map(|value| value.broker_entries_checked) }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct QuotaHelperReply {
    version: u32,
    session_id: String,
    root: PathBuf,
    owner_uid: u32,
    bindings: QuotaBindings,
    verified: bool,
    admissible_as_hard_quota: bool,
    project_id: Option<u32>,
    hard_limit_bytes: Option<u64>,
    mount_id: Option<u64>,
    descendants_checked: Option<usize>,
    broker_entries_checked: Option<usize>,
}

/// A current read-only report from the root-owned helper. It is deliberately
/// not an admission capability or a durable EDQUOT/restart proof.
#[derive(Debug, PartialEq, Eq)]
pub struct AdvisoryQuotaEvidence {
    pub project_id: u32,
    pub hard_limit_bytes: u64,
    pub mount_id: u64,
    pub descendants_checked: usize,
    pub broker_entries_checked: usize,
}

fn validate_reply(manifest: &Manifest, reply: QuotaHelperReply) -> io::Result<AdvisoryQuotaEvidence> {
    let root = manifest.checkout.parent().ok_or_else(|| error("quota client session root missing"))?;
    if manifest.profile != Profile::DockerOffline || manifest.state != "ready"
        || manifest.session_id.is_empty() || manifest.session_id.len() > 128
        || !manifest.session_id.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        || manifest.checkout != root.join("checkout") || manifest.private_home != root.join("home")
        || manifest.cache != root.join("cache") || manifest.broker != root.join("broker") {
        return Err(error("quota client requires an exact ready offline session"));
    }
    if reply.version != 2 || !reply.verified || reply.admissible_as_hard_quota
        || reply.session_id != manifest.session_id || reply.root != root
        || reply.owner_uid == 0 || reply.owner_uid == unsafe { libc::geteuid() }
        || (reply.bindings.checkout.device, reply.bindings.checkout.inode)
            != (manifest.checkout_device, manifest.checkout_inode) {
        return Err(error("quota helper report does not match the saved session"));
    }
    let bindings = [reply.bindings.root, reply.bindings.checkout, reply.bindings.home,
        reply.bindings.cache, reply.bindings.broker];
    if bindings[0].device == 0 || bindings[0].inode == 0 || bindings[0].mount_id == 0
        || bindings.iter().any(|entry| entry.device != bindings[0].device
            || entry.mount_id != bindings[0].mount_id || entry.inode == 0)
        || bindings.iter().enumerate().any(|(index, entry)|
            bindings[..index].iter().any(|prior| prior.inode == entry.inode)) {
        return Err(error("quota helper report has invalid bind identities"));
    }
    let (Some(project_id), Some(hard_limit_bytes), Some(mount_id),
        Some(descendants_checked), Some(broker_entries_checked)) = (
        reply.project_id, reply.hard_limit_bytes, reply.mount_id,
        reply.descendants_checked, reply.broker_entries_checked) else {
        return Err(error("quota helper report is incomplete"));
    };
    if project_id == 0 || project_id > i32::MAX as u32 || hard_limit_bytes == 0
        || hard_limit_bytes % 512 != 0 || hard_limit_bytes > 1024 * 1024 * 1024 * 1024
        || mount_id == 0 || mount_id != reply.bindings.root.mount_id
        || descendants_checked > 4096 || broker_entries_checked > 2 {
        return Err(error("quota helper report has invalid limits or counts"));
    }
    Ok(AdvisoryQuotaEvidence { project_id, hard_limit_bytes, mount_id,
        descendants_checked, broker_entries_checked })
}

/// Connect only to the fixed administrator-owned endpoint for this session.
/// No caller-supplied socket path or request body can select a different tree.
#[cfg(target_os = "linux")]
pub fn query_advisory_quota(manifest: &Manifest) -> io::Result<AdvisoryQuotaEvidence> {
    let id = &manifest.session_id;
    if id.is_empty() || id.len() > 128 || !id.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-') {
        return Err(error("invalid quota client session identity"));
    }
    let socket_path = Path::new("/run/doxa/quota").join(format!("{id}.sock"));
    let pinned = verify_socket_path(&socket_path)?;
    let stream = connect_challenge_path(&socket_path)?;
    let visible = verify_socket_path(&socket_path)?;
    let before = pinned.metadata()?; let after = visible.metadata()?;
    if (before.dev(), before.ino()) != (after.dev(), after.ino()) {
        return Err(error("quota helper socket changed during connection"));
    }
    read_root_reply(manifest, stream)
}

#[cfg(target_os = "linux")]
fn read_root_reply(manifest: &Manifest, stream: UnixStream) -> io::Result<AdvisoryQuotaEvidence> {
    if peer_uid(&stream)? != 0 { return Err(error("quota helper peer is not root")); }
    stream.set_read_timeout(Some(Duration::from_secs(3)))?;
    let mut bytes = Vec::new();
    stream.take(MAX_REPLY + 1).read_to_end(&mut bytes)?;
    if bytes.len() > MAX_REPLY as usize || !bytes.ends_with(b"\n") {
        return Err(error("quota helper report is missing or exceeds its bound"));
    }
    let reply: QuotaHelperReply = serde_json::from_slice(&bytes)
        .map_err(|_| error("quota helper report is malformed"))?;
    validate_reply(manifest, reply)
}

#[cfg(not(target_os = "linux"))]
pub fn query_advisory_quota(_: &Manifest) -> io::Result<AdvisoryQuotaEvidence> {
    Err(error("quota helper client requires Linux"))
}

fn safe_absolute(path: &Path) -> bool {
    path.is_absolute() && path.components().skip(1).all(|part| matches!(part, Component::Normal(_)))
}

/// Open an administrator-installed file through root-owned, non-writable
/// ancestors. No DOXA owner environment variable can redirect this path.
pub fn load_root_policy(path: &Path) -> io::Result<QuotaHelperPolicy> {
    if unsafe { libc::geteuid() } != 0 || !safe_absolute(path) {
        return Err(error("quota helper requires root and an absolute policy path"));
    }
    let mut parent = File::open("/")?;
    let root_meta = parent.metadata()?;
    if root_meta.uid() != 0 || root_meta.mode() & 0o022 != 0 {
        return Err(error("quota helper policy root is not administrator controlled"));
    }
    let components: Vec<_> = path.components().skip(1).collect();
    for (index, component) in components.iter().enumerate() {
        let Component::Normal(name) = component else { return Err(error("unsafe quota helper policy path")); };
        let name = CString::new(name.as_bytes()).map_err(|_| error("unsafe quota helper policy name"))?;
        let is_last = index + 1 == components.len();
        let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK
            | if is_last { 0 } else { libc::O_DIRECTORY };
        let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), flags) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        let opened = unsafe { File::from_raw_fd(fd) };
        let metadata = opened.metadata()?;
        if metadata.uid() != 0 || metadata.mode() & 0o022 != 0 {
            return Err(error("quota helper policy path is not administrator controlled"));
        }
        if is_last {
            if !metadata.is_file() || metadata.nlink() != 1 || metadata.mode() & 0o777 != 0o600
                || metadata.len() == 0 || metadata.len() > MAX_POLICY {
                return Err(error("quota helper policy file must be root-owned mode 0600 and bounded"));
            }
            let mut bytes = Vec::new();
            opened.take(MAX_POLICY + 1).read_to_end(&mut bytes)?;
            let policy: QuotaHelperPolicy = serde_json::from_slice(&bytes)
                .map_err(|_| error("invalid quota helper policy JSON"))?;
            policy.validate()?;
            return Ok(policy);
        }
        if !metadata.is_dir() { return Err(error("quota helper policy ancestor is not a directory")); }
        parent = opened;
    }
    Err(error("quota helper policy path has no file"))
}

pub fn authorize_peer(policy: &QuotaHelperPolicy, peer_uid: u32) -> io::Result<()> {
    policy.validate()?;
    if peer_uid != policy.caller_uid { return Err(error("quota helper peer UID is not authorized")); }
    Ok(())
}

#[cfg(target_os = "linux")]
fn peer_uid(stream: &UnixStream) -> io::Result<u32> {
    let mut credentials: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe { libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED,
        &mut credentials as *mut _ as *mut libc::c_void, &mut len) } < 0 {
        return Err(io::Error::last_os_error());
    }
    if len as usize != std::mem::size_of::<libc::ucred>() {
        return Err(error("quota helper peer credentials are incomplete"));
    }
    Ok(credentials.uid)
}

#[cfg(target_os = "linux")]
pub fn serve_systemd_socket(policy_path: &Path) -> io::Result<()> {
    let policy = load_root_policy(policy_path)?;
    if std::env::var("LISTEN_PID").ok().and_then(|value| value.parse::<u32>().ok()) != Some(std::process::id())
        || std::env::var("LISTEN_FDS").as_deref() != Ok("1") {
        return Err(error("quota helper requires one systemd socket activation descriptor"));
    }
    let mut stat: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(3, &mut stat) } < 0 { return Err(io::Error::last_os_error()); }
    if (stat.st_mode & libc::S_IFMT) != libc::S_IFSOCK || stat.st_uid != 0 {
        return Err(error("quota helper activation descriptor is not a root-owned socket"));
    }
    let mut accepting: libc::c_int = 0;
    let mut size = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
    if unsafe { libc::getsockopt(3, libc::SOL_SOCKET, libc::SO_ACCEPTCONN,
        &mut accepting as *mut _ as *mut libc::c_void, &mut size) } < 0 {
        return Err(io::Error::last_os_error());
    }
    if accepting != 1 || size as usize != std::mem::size_of::<libc::c_int>() {
        return Err(error("quota helper activation descriptor is not listening"));
    }
    let mut kind: libc::c_int = 0;
    size = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
    if unsafe { libc::getsockopt(3, libc::SOL_SOCKET, libc::SO_TYPE,
        &mut kind as *mut _ as *mut libc::c_void, &mut size) } < 0 {
        return Err(io::Error::last_os_error());
    }
    if kind != libc::SOCK_STREAM || size as usize != std::mem::size_of::<libc::c_int>() {
        return Err(error("quota helper activation socket has unsafe type or access"));
    }
    let listener = unsafe { UnixListener::from_raw_fd(3) };
    if listener.local_addr()?.as_pathname() != Some(policy.socket_path.as_path()) {
        return Err(error("quota helper activation socket differs from administrator policy"));
    }
    let pinned_path = verify_socket_path(&policy.socket_path)?;
    let queued = challenge_listener_path(&listener, &policy.socket_path)?;
    // The request contains no bytes. A connection selects exactly the one
    // administrator-pinned policy loaded at startup.
    for stream in queued { serve_checked_peer(&policy, &pinned_path, stream)?; }
    for incoming in listener.incoming() {
        serve_checked_peer(&policy, &pinned_path, incoming?)?;
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn serve_checked_peer(policy: &QuotaHelperPolicy, pinned_path: &File, stream: UnixStream) -> io::Result<()> {
    let visible = verify_socket_path(&policy.socket_path)?;
    let old = pinned_path.metadata()?;
    let now = visible.metadata()?;
    if (old.dev(), old.ino()) != (now.dev(), now.ino()) {
        return Err(error("quota helper socket pathname changed after activation"));
    }
    // An authenticated peer closing early cannot terminate the service.
    let _ = respond_to_peer(policy, stream);
    Ok(())
}

#[cfg(target_os = "linux")]
fn verify_socket_path(path: &Path) -> io::Result<File> {
    let parent_path = path.parent().ok_or_else(|| error("quota helper socket has no parent"))?;
    let mut directory = File::open("/")?;
    for component in parent_path.components().skip(1) {
        let Component::Normal(name) = component else { return Err(error("unsafe quota helper socket path")); };
        let name = CString::new(name.as_bytes()).map_err(|_| error("unsafe quota helper socket name"))?;
        let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        directory = unsafe { File::from_raw_fd(fd) };
        let meta = directory.metadata()?;
        if meta.uid() != 0 || meta.mode() & 0o022 != 0 {
            return Err(error("quota helper socket parent is not administrator controlled"));
        }
    }
    let name = CString::new(path.file_name().ok_or_else(|| error("quota helper socket has no name"))?
        .as_bytes()).map_err(|_| error("unsafe quota helper socket name"))?;
    let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(),
        libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
    if fd < 0 { return Err(io::Error::last_os_error()); }
    let socket = unsafe { File::from_raw_fd(fd) };
    let meta = socket.metadata()?;
    if !meta.file_type().is_socket() || meta.uid() != 0 || meta.mode() & 0o007 != 0 {
        return Err(error("quota helper socket path is not root-owned and private"));
    }
    Ok(socket)
}

#[cfg(target_os = "linux")]
fn challenge_listener_path(listener: &UnixListener, path: &Path) -> io::Result<Vec<UnixStream>> {
    // A Unix listener FD lives in sockfs and has a different dev/inode from
    // its pathname entry. A nonce sent to the pathname and read from this
    // exact listening FD establishes their current kernel connection instead.
    let mut nonce = [0u8; 32];
    if unsafe { libc::getrandom(nonce.as_mut_ptr() as *mut libc::c_void, nonce.len(), libc::GRND_NONBLOCK) }
        != nonce.len() as isize {
        return Err(error("quota helper listener challenge has no kernel randomness"));
    }
    let mut client = connect_challenge_path(path)?;
    client.set_write_timeout(Some(Duration::from_secs(1)))?;
    client.write_all(&nonce)?;
    listener.set_nonblocking(true)?;
    let deadline = Instant::now() + Duration::from_secs(1);
    let mut queued = Vec::new();
    loop {
        if Instant::now() >= deadline {
            return Err(error("quota helper listener path challenge timed out"));
        }
        match listener.accept() {
            Ok((mut stream, _)) => {
                if peer_uid(&stream)? != 0 {
                    if queued.len() >= 16 {
                        return Err(error("quota helper activation has too many queued peers"));
                    }
                    queued.push(stream);
                    continue;
                }
                stream.set_read_timeout(Some(Duration::from_millis(100)))?;
                let mut received = [0u8; 32];
                if stream.read_exact(&mut received).is_ok() && received == nonce { break; }
            },
            Err(cause) if cause.kind() == io::ErrorKind::WouldBlock && Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(1));
            },
            Err(cause) if cause.kind() == io::ErrorKind::WouldBlock => {
                return Err(error("quota helper listener does not own the configured pathname"));
            },
            Err(cause) => return Err(cause),
        }
    }
    listener.set_nonblocking(false)?;
    Ok(queued)
}

#[cfg(target_os = "linux")]
fn connect_challenge_path(path: &Path) -> io::Result<UnixStream> {
    let bytes = path.as_os_str().as_bytes();
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    if bytes.is_empty() || bytes.len() >= address.sun_path.len() {
        return Err(error("quota helper socket pathname is too long"));
    }
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (index, byte) in bytes.iter().enumerate() { address.sun_path[index] = *byte as libc::c_char; }
    let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC, 0) };
    if fd < 0 { return Err(io::Error::last_os_error()); }
    let stream = unsafe { UnixStream::from_raw_fd(fd) };
    if unsafe { libc::connect(fd, &address as *const _ as *const libc::sockaddr,
        std::mem::size_of::<libc::sockaddr_un>() as libc::socklen_t) } < 0 {
        // A full backlog or unavailable path refuses the helper immediately.
        return Err(io::Error::last_os_error());
    }
    stream.set_nonblocking(false)?;
    Ok(stream)
}

#[cfg(target_os = "linux")]
fn respond_to_peer(policy: &QuotaHelperPolicy, mut stream: UnixStream) -> io::Result<()> {
    stream.set_write_timeout(Some(Duration::from_secs(3)))?;
    authorize_peer(policy, peer_uid(&stream)?)?;
    let mut response = serde_json::to_vec(&policy.result())?;
    response.push(b'\n');
    stream.write_all(&response)
}

#[cfg(not(target_os = "linux"))]
pub fn serve_systemd_socket(_: &Path) -> io::Result<()> {
    Err(error("quota helper requires Linux"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::quota_verify::QuotaBinding;

    fn policy() -> QuotaHelperPolicy {
        let bind = |inode| QuotaBinding { device: 9, inode, mount_id: 8 };
        QuotaHelperPolicy { version: 1, session_id: "session-1".into(),
            root: PathBuf::from("/owner/sessions/session-1"),
            socket_path: PathBuf::from("/run/doxa/quota/session-1.sock"),
            caller_uid: 2001, owner_uid: 2002, project_id: 41,
            hard_limit_bytes: 64 * 1024 * 1024,
            bindings: QuotaBindings { root: bind(1), checkout: bind(2), home: bind(3),
                cache: bind(4), broker: bind(5) } }
    }

    fn client_manifest(policy: &QuotaHelperPolicy) -> Manifest {
        let mut manifest = policy.manifest();
        manifest.checkout_device = policy.bindings.checkout.device;
        manifest.checkout_inode = policy.bindings.checkout.inode;
        manifest
    }

    fn verified_reply(policy: &QuotaHelperPolicy) -> QuotaHelperReply {
        QuotaHelperReply { version: 2, session_id: policy.session_id.clone(),
            root: policy.root.clone(), owner_uid: policy.owner_uid, bindings: policy.bindings,
            verified: true, admissible_as_hard_quota: false,
            project_id: Some(policy.project_id), hard_limit_bytes: Some(policy.hard_limit_bytes),
            mount_id: Some(policy.bindings.root.mount_id), descendants_checked: Some(4),
            broker_entries_checked: Some(1) }
    }

    #[test]
    fn advisory_client_binds_reply_to_exact_saved_tree_and_refuses_admission_flags() {
        let policy = policy();
        let manifest = client_manifest(&policy);
        let valid = verified_reply(&policy);
        assert_eq!(validate_reply(&manifest, verified_reply(&policy)).unwrap(),
            AdvisoryQuotaEvidence { project_id: 41, hard_limit_bytes: 64 * 1024 * 1024,
                mount_id: 8, descendants_checked: 4, broker_entries_checked: 1 });
        let mut changed = verified_reply(&policy); changed.session_id = "other".into();
        assert!(validate_reply(&manifest, changed).is_err());
        let mut changed = verified_reply(&policy); changed.root = PathBuf::from("/owner/sessions/other");
        assert!(validate_reply(&manifest, changed).is_err());
        let mut changed = verified_reply(&policy); changed.bindings.checkout.inode += 1;
        assert!(validate_reply(&manifest, changed).is_err());
        let mut changed = verified_reply(&policy); changed.bindings.broker = changed.bindings.home;
        assert!(validate_reply(&manifest, changed).is_err());
        let mut changed = verified_reply(&policy); changed.bindings.cache.mount_id += 1;
        assert!(validate_reply(&manifest, changed).is_err());
        let mut changed = verified_reply(&policy); changed.mount_id = Some(9);
        assert!(validate_reply(&manifest, changed).is_err());
        let mut changed = verified_reply(&policy); changed.owner_uid = unsafe { libc::geteuid() };
        assert!(validate_reply(&manifest, changed).is_err());
        let mut changed = verified_reply(&policy); changed.verified = false;
        assert!(validate_reply(&manifest, changed).is_err());
        let mut changed = verified_reply(&policy); changed.admissible_as_hard_quota = true;
        assert!(validate_reply(&manifest, changed).is_err());
        let mut changed = verified_reply(&policy); changed.project_id = None;
        assert!(validate_reply(&manifest, changed).is_err());
        let mut changed = verified_reply(&policy); changed.hard_limit_bytes = Some(0);
        assert!(validate_reply(&manifest, changed).is_err());
        let mut changed = verified_reply(&policy); changed.broker_entries_checked = Some(3);
        assert!(validate_reply(&manifest, changed).is_err());
        let mut native = manifest.clone(); native.profile = Profile::Native;
        assert!(validate_reply(&native, valid).is_err());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn same_uid_socket_server_cannot_impersonate_root_quota_helper() {
        if unsafe { libc::geteuid() } == 0 { return; }
        let (client, _server) = UnixStream::pair().unwrap();
        let refusal = read_root_reply(&client_manifest(&policy()), client).unwrap_err();
        assert!(refusal.to_string().contains("peer is not root"));
    }

    #[test]
    fn only_distinct_pinned_peer_can_request_snapshot() {
        let policy = policy();
        assert!(authorize_peer(&policy, 2001).is_ok());
        for uid in [0, 2002, 2003] { assert!(authorize_peer(&policy, uid).is_err()); }
        let mut same_uid = policy.clone(); same_uid.caller_uid = same_uid.owner_uid;
        assert!(authorize_peer(&same_uid, 2002).is_err());
    }

    #[test]
    fn policy_refuses_aliases_unbounded_limits_and_untrusted_fields() {
        let mut changed = policy(); changed.bindings.cache = changed.bindings.checkout;
        assert!(changed.validate().is_err());
        let mut changed = policy(); changed.bindings.broker.mount_id += 1;
        assert!(changed.validate().is_err());
        let mut changed = policy(); changed.hard_limit_bytes = 0;
        assert!(changed.validate().is_err());
        let mut changed = policy(); changed.root = PathBuf::from("/owner/../session-1");
        assert!(changed.validate().is_err());
        let mut json = serde_json::to_value(policy()).unwrap_or_default();
        json["caller_supplied_project_id"] = serde_json::json!(999);
        assert!(serde_json::from_value::<QuotaHelperPolicy>(json).is_err());
    }

    #[test]
    fn failure_result_never_claims_hardened_admission() {
        let policy = policy();
        let result = policy.result();
        assert!(!result.verified);
        assert!(!result.admissible_as_hard_quota);
    }
}
