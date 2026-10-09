//! Read-only checks for an administrator-staged, inactive quota helper unit.
//! This reports a current prerequisite snapshot, never hardened admission.
use crate::{error, quota_helper::load_root_policy};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{ffi::{CStr, CString}, fs::{self, File}, io::{self, Read},
    os::{fd::{AsRawFd, FromRawFd}, unix::{ffi::OsStrExt, fs::MetadataExt}},
    path::{Component, Path, PathBuf}};

const SERVICE: &[u8] = include_bytes!("../../../packaging/systemd/doxa-quota-helper@.service");
const SOCKET: &[u8] = include_bytes!("../../../packaging/systemd/doxa-quota-helper@.socket");
const HELPER: &str = "/usr/libexec/doxa/doxa-quota-helper";
const UNITS: &str = "/etc/systemd/system";
const POLICIES: &str = "/etc/doxa/quota";

#[derive(Debug, Serialize)]
pub struct QuotaInstallPreflight {
    pub session_id: String,
    pub project_id: u32,
    pub hard_limit_bytes: u64,
    pub mount_id: u64,
    pub descendants_checked: usize,
    pub broker_entries_checked: usize,
    pub staged_files_verified: bool,
    pub effective_unit_verified: bool,
    pub helper_sha256: String,
    pub socket_inactive: bool,
    pub admissible_as_hard_quota: bool,
}

fn session_id(id: &str) -> io::Result<()> {
    if id.is_empty() || id.len() > 128
        || !id.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-') {
        return Err(error("invalid quota helper installation session identity"));
    }
    Ok(())
}

/// Descriptor-walk an exact root-owned file. Symlink ancestors, writable
/// ancestors, non-regular files and hard links refuse the staged package.
fn root_file(path: &Path, mode: u32, max: u64) -> io::Result<File> {
    if !path.is_absolute() || path.components().skip(1).any(|part| !matches!(part, Component::Normal(_))) {
        return Err(error("unsafe quota helper installation path"));
    }
    let mut directory = File::open("/")?;
    let root = directory.metadata()?;
    if root.uid() != 0 || root.mode() & 0o022 != 0 {
        return Err(error("quota helper installation root is not administrator controlled"));
    }
    let components: Vec<_> = path.components().skip(1).collect();
    if components.is_empty() { return Err(error("quota helper installation path has no file")); }
    for (index, component) in components.iter().enumerate() {
        let Component::Normal(name) = component else { return Err(error("unsafe quota helper installation path")); };
        let name = CString::new(name.as_bytes()).map_err(|_| error("unsafe quota helper installation name"))?;
        let last = index + 1 == components.len();
        let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK
            | if last { 0 } else { libc::O_DIRECTORY };
        let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), flags) };
        if fd < 0 { return Err(io::Error::last_os_error()); }
        let opened = unsafe { File::from_raw_fd(fd) };
        let meta = opened.metadata()?;
        if meta.uid() != 0 || meta.mode() & 0o022 != 0 {
            return Err(error("quota helper installation path is not administrator controlled"));
        }
        if last {
            if !meta.is_file() || meta.nlink() != 1 || meta.mode() & 0o777 != mode
                || meta.len() == 0 || meta.len() > max {
                return Err(error("quota helper installation file has unsafe metadata"));
            }
            return Ok(opened);
        }
        if !meta.is_dir() { return Err(error("quota helper installation ancestor is not a directory")); }
        directory = opened;
    }
    Err(error("quota helper installation path has no file"))
}

fn exact_unit(path: &Path, expected: &[u8]) -> io::Result<()> {
    let file = root_file(path, 0o644, expected.len() as u64)?;
    let mut bytes = Vec::new();
    file.take(expected.len() as u64 + 1).read_to_end(&mut bytes)?;
    if bytes != expected { return Err(error("quota helper installed unit differs from reviewed template")); }
    Ok(())
}

fn absent(path: &Path) -> io::Result<()> {
    match fs::symlink_metadata(path) {
        Err(cause) if cause.kind() == io::ErrorKind::NotFound => Ok(()),
        Ok(_) => Err(error("quota helper installation has an active socket or unit override")),
        Err(cause) => Err(cause),
    }
}

#[cfg(target_os = "linux")]
fn caller_can_open_socket(caller_uid: u32) -> io::Result<()> {
    let group_name = CString::new("doxa-quota-readers").unwrap();
    let mut group: libc::group = unsafe { std::mem::zeroed() };
    let mut group_buf = vec![0u8; 16384];
    let mut group_result = std::ptr::null_mut();
    if unsafe { libc::getgrnam_r(group_name.as_ptr(), &mut group, group_buf.as_mut_ptr().cast(),
        group_buf.len(), &mut group_result) } != 0 || group_result.is_null() || group.gr_gid == 0 {
        return Err(error("dedicated quota socket group is unavailable"));
    }
    let mut user: libc::passwd = unsafe { std::mem::zeroed() };
    let mut user_buf = vec![0u8; 16384];
    let mut user_result = std::ptr::null_mut();
    if unsafe { libc::getpwuid_r(caller_uid, &mut user, user_buf.as_mut_ptr().cast(),
        user_buf.len(), &mut user_result) } != 0 || user_result.is_null() {
        return Err(error("dedicated quota caller account is unavailable"));
    }
    let name = unsafe { CStr::from_ptr(user.pw_name) };
    let mut groups = vec![0 as libc::gid_t; 256];
    let mut count = groups.len() as libc::c_int;
    if unsafe { libc::getgrouplist(name.as_ptr(), user.pw_gid, groups.as_mut_ptr(), &mut count) } < 0
        || count < 1 || count as usize > groups.len()
        || !groups[..count as usize].contains(&group.gr_gid) {
        return Err(error("dedicated quota caller cannot open the socket group"));
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
fn caller_can_open_socket(_: u32) -> io::Result<()> { Err(error("quota helper installation requires Linux")) }

/// Check only installed files and the live read-only kernel quota snapshot.
/// This does not start a service, create a socket, or issue admission proof.
pub fn preflight_installed_quota_helper(id: &str, reviewed_helper_sha256: &str) -> io::Result<QuotaInstallPreflight> {
    if unsafe { libc::geteuid() } != 0 { return Err(error("quota helper installation preflight requires root")); }
    session_id(id)?;
    if reviewed_helper_sha256.len() != 64
        || !reviewed_helper_sha256.bytes().all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()) {
        return Err(error("reviewed quota helper SHA-256 must be 64 lowercase hex digits"));
    }
    let policy_path = Path::new(POLICIES).join(format!("{id}.json"));
    let policy = load_root_policy(&policy_path)?;
    let socket_path = Path::new("/run/doxa/quota").join(format!("{id}.sock"));
    if policy.session_id != id || policy.socket_path != socket_path {
        return Err(error("quota helper installation policy differs from the fixed session endpoint"));
    }
    let unit_dir = Path::new(UNITS);
    exact_unit(&unit_dir.join("doxa-quota-helper@.service"), SERVICE)?;
    exact_unit(&unit_dir.join("doxa-quota-helper@.socket"), SOCKET)?;
    let mut helper = root_file(Path::new(HELPER), 0o755, 32 * 1024 * 1024)?;
    let mut digest = Sha256::new();
    let mut buffer = [0u8; 8192];
    loop {
        let count = helper.read(&mut buffer)?;
        if count == 0 { break; }
        digest.update(&buffer[..count]);
    }
    let helper_sha256 = format!("{:x}", digest.finalize());
    if helper_sha256 != reviewed_helper_sha256 {
        return Err(error("installed quota helper differs from reviewed SHA-256"));
    }
    caller_can_open_socket(policy.caller_uid)?;
    absent(&socket_path)?;
    for name in ["service", "socket"] {
        let template = format!("doxa-quota-helper@.{name}.d");
        let instance = format!("doxa-quota-helper@{id}.{name}");
        for directory in ["/etc/systemd/system", "/run/systemd/system",
            "/usr/lib/systemd/system", "/lib/systemd/system"] {
            let directory = PathBuf::from(directory);
            absent(&directory.join(format!("{name}.d")))?;
            absent(&directory.join(&template))?;
            absent(&directory.join(format!("{instance}.d")))?;
            absent(&directory.join(&instance))?;
        }
    }
    let snapshot = policy.inspect()?;
    Ok(QuotaInstallPreflight { session_id: id.to_owned(), project_id: snapshot.project_id,
        hard_limit_bytes: snapshot.hard_limit_bytes, mount_id: snapshot.mount_id,
        descendants_checked: snapshot.descendants_checked,
        broker_entries_checked: snapshot.broker_entries_checked,
        staged_files_verified: true, effective_unit_verified: false,
        helper_sha256, socket_inactive: true,
        admissible_as_hard_quota: false })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn instance_name_cannot_escape_fixed_policy_or_socket_paths() {
        for id in ["", "../other", "session_1", "session.1", "a/b", "é"] {
            assert!(session_id(id).is_err(), "{id}");
        }
        assert!(session_id("session-1").is_ok());
        assert!(session_id(&"a".repeat(129)).is_err());
    }

    #[test]
    fn bundled_units_keep_inactive_root_owned_contract() {
        let service = std::str::from_utf8(SERVICE).unwrap();
        let socket = std::str::from_utf8(SOCKET).unwrap();
        assert!(service.contains("ExecStart=/usr/libexec/doxa/doxa-quota-helper /etc/doxa/quota/%i.json"));
        assert!(service.contains("CapabilityBoundingSet=CAP_SYS_ADMIN"));
        assert!(socket.contains("ListenStream=/run/doxa/quota/%i.sock"));
        assert!(socket.contains("SocketUser=root"));
        assert!(socket.contains("SocketGroup=doxa-quota-readers"));
        assert!(socket.contains("SocketMode=0660"));
        assert!(socket.contains("Accept=no"));
    }
}
