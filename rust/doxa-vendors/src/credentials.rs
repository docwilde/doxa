// SPDX-License-Identifier: AGPL-3.0-only
//! Private, bounded native vendor credentials. Public status and errors never
//! include key values. Every operation reopens the store; clients cache no keys.
use crate::Vendor;
use serde_json::{Map, Value};
use std::{ffi::CString, fs::{self, File, OpenOptions}, io::{self, Read, Write},
    os::{fd::{AsRawFd, FromRawFd}, unix::{ffi::OsStrExt, fs::{MetadataExt, OpenOptionsExt}}},
    path::{Component, Path, PathBuf}, sync::atomic::{AtomicU64, Ordering}};

const FILE_NAME: &str = "credentials.json";
const LOCK_NAME: &str = ".credentials.lock";
const MAX_BYTES: u64 = 32 * 1024;
const MAX_KEY: usize = 4096;
static NEXT_FILE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CredentialStatus { Saved, Environment, Missing }

fn error(kind: io::ErrorKind) -> io::Error { io::Error::new(kind, "Native vendor credential operation failed") }
fn private_error() -> io::Error { error(io::ErrorKind::PermissionDenied) }
fn clean<T>(result: io::Result<T>) -> io::Result<T> { result.map_err(|e| error(e.kind())) }

fn home() -> io::Result<PathBuf> {
    std::env::var_os("DOXA_HOME").filter(|p| !p.is_empty()).map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").filter(|p| !p.is_empty()).map(|p| PathBuf::from(p).join(".doxa")))
        .ok_or_else(|| error(io::ErrorKind::NotFound))
}

fn valid_key(value: &str) -> io::Result<&str> {
    // A pasted terminal newline is refused, even if trimming would remove it.
    if value.contains(['\n', '\r']) { return Err(error(io::ErrorKind::InvalidInput)); }
    let key = value.trim();
    if !(8..=MAX_KEY).contains(&key.len()) || !key.bytes().all(|b| b.is_ascii_graphic()) {
        return Err(error(io::ErrorKind::InvalidInput));
    }
    Ok(key)
}

fn name(value: &str) -> CString { CString::new(value).expect("fixed credential filename") }
fn open_at(parent: &File, entry: &str, flags: i32, mode: u32) -> io::Result<File> {
    let entry = name(entry);
    // SAFETY: live directory fd, valid C string; successful fd is newly owned.
    let fd = unsafe { libc::openat(parent.as_raw_fd(), entry.as_ptr(), flags | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK, mode) };
    if fd < 0 { Err(io::Error::last_os_error()) } else { Ok(unsafe { File::from_raw_fd(fd) }) }
}

fn directory(path: &Path, create: bool) -> io::Result<Option<File>> {
    // Walk without following a symlink in any component, then use this fd for
    // all child operations, including rename. A path swap cannot redirect them.
    let absolute = if path.is_absolute() { path.to_owned() } else { std::env::current_dir()?.join(path) };
    if absolute.components().any(|part| matches!(part, Component::ParentDir)) { return Err(private_error()); }
    let mut dir = OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC).open("/")?;
    let parts: Vec<_> = absolute.components().filter_map(|part| if let Component::Normal(n) = part { Some(n) } else { None }).collect();
    for (index, part) in parts.iter().enumerate() {
        let entry = CString::new(part.as_bytes()).map_err(|_| private_error())?;
        // SAFETY: fd stays live; entry is a valid nul-terminated component.
        let mut fd = unsafe { libc::openat(dir.as_raw_fd(), entry.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
        if fd < 0 && io::Error::last_os_error().kind() == io::ErrorKind::NotFound {
            if !create { return Ok(None); }
            // Only DOXA_HOME itself is created; a missing parent is an error.
            if index + 1 != parts.len() { return Err(error(io::ErrorKind::NotFound)); }
            if unsafe { libc::mkdirat(dir.as_raw_fd(), entry.as_ptr(), 0o700) } != 0 && io::Error::last_os_error().kind() != io::ErrorKind::AlreadyExists {
                return Err(io::Error::last_os_error());
            }
            fd = unsafe { libc::openat(dir.as_raw_fd(), entry.as_ptr(), libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
        }
        if fd < 0 { return Err(io::Error::last_os_error()); }
        dir = unsafe { File::from_raw_fd(fd) };
    }
    let meta = dir.metadata()?;
    if !meta.is_dir() || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o022 != 0 { return Err(private_error()); }
    Ok(Some(dir))
}

fn check_file(file: &File) -> io::Result<fs::Metadata> {
    let meta = file.metadata()?;
    if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() } || meta.nlink() != 1 || meta.mode() & 0o7777 != 0o600 {
        return Err(private_error());
    }
    Ok(meta)
}
fn identity(dir: &File, entry: &str) -> io::Result<Option<(u64, u64)>> {
    let entry = name(entry);
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    // SAFETY: stat output points at enough writable memory; no symlink follows.
    if unsafe { libc::fstatat(dir.as_raw_fd(), entry.as_ptr(), stat.as_mut_ptr(), libc::AT_SYMLINK_NOFOLLOW) } != 0 {
        let e = io::Error::last_os_error();
        return if e.kind() == io::ErrorKind::NotFound { Ok(None) } else { Err(e) };
    }
    let stat = unsafe { stat.assume_init() };
    if stat.st_mode & libc::S_IFMT != libc::S_IFREG || stat.st_uid != unsafe { libc::geteuid() }
        || stat.st_nlink != 1 || stat.st_mode & 0o7777 != 0o600 { return Err(private_error()); }
    Ok(Some((stat.st_dev, stat.st_ino)))
}
fn lock(dir: &File) -> io::Result<File> {
    let lock = open_at(dir, LOCK_NAME, libc::O_RDWR | libc::O_CREAT, 0o600)?;
    let meta = check_file(&lock)?;
    if identity(dir, LOCK_NAME)? != Some((meta.dev(), meta.ino())) { return Err(private_error()); }
    // SAFETY: live owned regular fd. Closing the guard releases the lock.
    let deadline = std::time::Instant::now() + std::time::Duration::from_millis(500);
    loop {
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 { break; }
        let e = io::Error::last_os_error();
        if e.kind() != io::ErrorKind::WouldBlock { return Err(e); }
        if std::time::Instant::now() >= deadline { return Err(error(io::ErrorKind::WouldBlock)); }
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
    if identity(dir, LOCK_NAME)? != Some((meta.dev(), meta.ino())) { return Err(private_error()); }
    Ok(lock)
}
struct Store { dir: File, _lock: File, values: Map<String, Value>, identity: Option<(u64, u64)> }
impl Store {
    fn open(path: &Path, create: bool) -> io::Result<Option<Self>> {
        let Some(dir) = directory(path, create)? else { return Ok(None); };
        let guard = lock(&dir)?;
        let before = identity(&dir, FILE_NAME)?;
        let mut values = Map::new();
        if let Some(expected) = before {
            let mut file = open_at(&dir, FILE_NAME, libc::O_RDONLY, 0)?;
            let meta = check_file(&file)?;
            if (meta.dev(), meta.ino()) != expected || meta.len() > MAX_BYTES { return Err(private_error()); }
            let mut bytes = Vec::new();
            (&mut file).take(MAX_BYTES + 1).read_to_end(&mut bytes)?;
            if bytes.len() as u64 > MAX_BYTES { return Err(error(io::ErrorKind::InvalidData)); }
            let value: Value = serde_json::from_slice(&bytes).map_err(|_| error(io::ErrorKind::InvalidData))?;
            values = value.as_object().cloned().ok_or_else(|| error(io::ErrorKind::InvalidData))?;
            for (vendor, value) in &values {
                if !matches!(vendor.as_str(), "deepseek" | "glm") || value.as_str().is_none_or(|k| valid_key(k).is_err() || k.trim() != k) {
                    return Err(error(io::ErrorKind::InvalidData));
                }
            }
            if identity(&dir, FILE_NAME)? != before { return Err(private_error()); }
        }
        Ok(Some(Self { dir, _lock: guard, values, identity: before }))
    }
    fn write(&self) -> io::Result<()> {
        let bytes = serde_json::to_vec(&self.values).map_err(|_| error(io::ErrorKind::InvalidData))?;
        if bytes.len() as u64 > MAX_BYTES { return Err(error(io::ErrorKind::InvalidData)); }
        let temp = format!(".credentials-{}-{}", std::process::id(), NEXT_FILE.fetch_add(1, Ordering::Relaxed));
        let mut file = open_at(&self.dir, &temp, libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL, 0o600)?;
        let meta = file.metadata()?;
        let temp_identity = (meta.dev(), meta.ino());
        let result = (|| {
            let meta = check_file(&file)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            if identity(&self.dir, &temp)? != Some((meta.dev(), meta.ino())) || identity(&self.dir, FILE_NAME)? != self.identity { return Err(private_error()); }
            // SAFETY: both paths are fixed child names of the same opened fd.
            if unsafe { libc::renameat(self.dir.as_raw_fd(), name(&temp).as_ptr(), self.dir.as_raw_fd(), name(FILE_NAME).as_ptr()) } != 0 { return Err(io::Error::last_os_error()); }
            self.dir.sync_all()
        })();
        // SAFETY: removes only our temporary child name; absent after rename.
        if identity(&self.dir, &temp).ok() == Some(Some(temp_identity)) {
            unsafe { libc::unlinkat(self.dir.as_raw_fd(), name(&temp).as_ptr(), 0); }
        }
        result
    }
}
fn saved(vendor: Vendor) -> io::Result<Option<String>> {
    Ok(Store::open(&home()?, false)?.and_then(|store| store.values.get(vendor.engine_id()).and_then(Value::as_str).map(str::to_owned)))
}
fn environment(vendor: Vendor) -> Option<String> {
    // Preserve inherited credential compatibility; bound all values before use.
    std::env::var(vendor.env_var()).ok().and_then(|k| valid_key(&k).ok().map(str::to_owned))
}
/// Source only; the saved and inherited values are never exposed to setup UI.
pub fn status(vendor: Vendor) -> io::Result<CredentialStatus> {
    clean(saved(vendor).map(|key| if key.is_some() { CredentialStatus::Saved } else if environment(vendor).is_some() { CredentialStatus::Environment } else { CredentialStatus::Missing }))
}
/// Save a single printable key. Outer spaces are trimmed; newlines are refused.
pub fn save(vendor: Vendor, value: &str) -> io::Result<()> {
    clean((|| {
        let key = valid_key(value)?;
        let mut store = Store::open(&home()?, true)?.ok_or_else(private_error)?;
        store.values.insert(vendor.engine_id().into(), Value::String(key.into()));
        store.write()
    })())
}
/// Remove the explicit override. Subsequent requests use the inherited key.
pub fn remove(vendor: Vendor) -> io::Result<()> {
    clean((|| {
        if let Some(mut store) = Store::open(&home()?, false)? {
            if store.values.remove(vendor.engine_id()).is_some() { store.write()?; }
        }
        Ok(())
    })())
}
/// Resolve fresh for each request. An unsafe store fails closed, including when
/// an inherited key is available. No key is retained by a client struct.
pub fn resolve(vendor: Vendor) -> io::Result<Option<String>> { clean(saved(vendor).map(|key| key.or_else(|| environment(vendor)))) }

/// Redact keys locally before text crosses a LORE/model/transcript boundary.
/// Both overrides and inherited values are covered, including inactive vendors.
pub fn redact(text: &str) -> io::Result<String> {
    clean((|| {
        let store = Store::open(&home()?, false)?;
        let mut keys: Vec<String> = [Vendor::DeepSeek, Vendor::Glm].into_iter().flat_map(|vendor| {
            let saved = store.as_ref().and_then(|s| s.values.get(vendor.engine_id())).and_then(Value::as_str).map(str::to_owned);
            saved.into_iter().chain(environment(vendor))
        }).collect();
        keys.sort_by_key(|key| std::cmp::Reverse(key.len()));
        Ok(keys.iter().fold(text.to_owned(), |text, key| text.replace(key, "[REDACTED]")))
    })())
}

/// Reject the private credential file if a workspace tool encounters it through
/// another relative path. File identity also covers a renamed opened directory.
pub fn is_credential_file(file: &File) -> io::Result<bool> {
    clean((|| {
        let Some(dir) = directory(&home()?, false)? else { return Ok(false); };
        let meta = file.metadata()?;
        Ok(identity(&dir, FILE_NAME)? == Some((meta.dev(), meta.ino())))
    })())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    struct EnvironmentGuard { _lock: std::sync::MutexGuard<'static, ()>, previous: Vec<(&'static str, Option<std::ffi::OsString>)> }
    impl Drop for EnvironmentGuard { fn drop(&mut self) {
        for (name, value) in self.previous.drain(..) {
            match value { Some(value) => std::env::set_var(name, value), None => std::env::remove_var(name) }
        }
    } }
    fn fixture() -> (EnvironmentGuard, tempfile::TempDir) {
        let lock = ENV_LOCK.lock().unwrap();
        // Opaque snapshots are restored only, never resolved/asserted/displayed.
        let previous = ["DOXA_HOME", "DEEPSEEK_API_KEY", "ZAI_API_KEY"].into_iter().map(|name| (name, std::env::var_os(name))).collect();
        let guard = EnvironmentGuard { _lock: lock, previous };
        let dir = tempfile::tempdir().unwrap();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        std::env::set_var("DOXA_HOME", dir.path());
        // Synthetic fixtures replace both inherited variables before resolution.
        std::env::set_var("DEEPSEEK_API_KEY", "inherited-deepseek-fixture");
        std::env::set_var("ZAI_API_KEY", "inherited-zai-fixture");
        (guard, dir)
    }
    #[test]
    fn source_precedence_changes_immediately_and_remove_returns_to_environment() {
        let (_guard, dir) = fixture();
        assert_eq!(status(Vendor::DeepSeek).unwrap(), CredentialStatus::Environment);
        save(Vendor::DeepSeek, "  saved-deepseek-fixture  ").unwrap();
        save(Vendor::Glm, "saved-zai-fixture").unwrap();
        assert_eq!(status(Vendor::DeepSeek).unwrap(), CredentialStatus::Saved);
        assert_eq!(resolve(Vendor::DeepSeek).unwrap().as_deref(), Some("saved-deepseek-fixture"));
        save(Vendor::DeepSeek, "replacement-deepseek-fixture").unwrap();
        assert_eq!(resolve(Vendor::DeepSeek).unwrap().as_deref(), Some("replacement-deepseek-fixture"));
        remove(Vendor::DeepSeek).unwrap();
        assert_eq!(resolve(Vendor::DeepSeek).unwrap().as_deref(), Some("inherited-deepseek-fixture"));
        assert_eq!(resolve(Vendor::Glm).unwrap().as_deref(), Some("saved-zai-fixture"));
        std::env::remove_var("DEEPSEEK_API_KEY");
        assert_eq!(status(Vendor::DeepSeek).unwrap(), CredentialStatus::Missing);
        assert_eq!(fs::metadata(dir.path().join(FILE_NAME)).unwrap().mode() & 0o777, 0o600);
        assert_eq!(fs::read_dir(dir.path()).unwrap().count(), 2); // store + lock; no temp files
    }
    #[test]
    fn invalid_input_and_corrupt_files_never_echo_values() {
        let (_guard, dir) = fixture();
        for invalid in ["too-short", "secret\nfixture", "secret\rfixture", "secret fixture", "secret\tfixture", "秘密fixture"] {
            let invalid = if invalid == "too-short" { "short" } else { invalid };
            let err = save(Vendor::DeepSeek, invalid).unwrap_err();
            assert!(!format!("{err:?} {err}").contains(invalid));
        }
        assert!(save(Vendor::DeepSeek, &"s".repeat(MAX_KEY + 1)).is_err());
        save(Vendor::DeepSeek, "synthetic-secret-fixture").unwrap();
        for body in [br#"{"deepseek":"synthetic-secret-fixture", BAD}"#.as_slice(), br#"{"unknown":"synthetic-secret-fixture"}"#.as_slice(), br#"{"deepseek":7}"#.as_slice()] {
            fs::write(dir.path().join(FILE_NAME), body).unwrap();
            let err = resolve(Vendor::DeepSeek).unwrap_err();
            assert!(!format!("{err:?} {err}").contains("synthetic-secret-fixture"));
            assert!(status(Vendor::DeepSeek).is_err());
        }
    }
    #[test]
    fn refuses_symlink_hardlink_mode_fifo_and_oversize_even_with_environment_key() {
        let (_guard, dir) = fixture();
        let path = dir.path().join(FILE_NAME);
        let outside = dir.path().join("outside");
        fs::write(&outside, "{}").unwrap();
        fs::set_permissions(&outside, fs::Permissions::from_mode(0o600)).unwrap();
        symlink(&outside, &path).unwrap();
        assert!(resolve(Vendor::DeepSeek).is_err());
        assert!(save(Vendor::DeepSeek, "synthetic-secret-fixture").is_err());
        assert!(remove(Vendor::DeepSeek).is_err());
        fs::remove_file(&path).unwrap();
        fs::hard_link(&outside, &path).unwrap();
        assert!(resolve(Vendor::DeepSeek).is_err());
        fs::remove_file(&path).unwrap();
        fs::write(&path, "{}").unwrap();
        for mode in [0o644, 0o660, 0o400, 0o700] {
            fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
            assert!(resolve(Vendor::DeepSeek).is_err());
        }
        fs::remove_file(&path).unwrap();
        let fifo = CString::new(path.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o600) }, 0);
        assert!(resolve(Vendor::DeepSeek).is_err());
        fs::remove_file(&path).unwrap();
        fs::write(&path, vec![b' '; MAX_BYTES as usize + 1]).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(resolve(Vendor::DeepSeek).is_err());
    }
    #[test]
    fn refuses_unsafe_directory_and_lock_without_changing_them() {
        let (_guard, dir) = fixture();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o777)).unwrap();
        assert!(save(Vendor::DeepSeek, "synthetic-secret-fixture").is_err());
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let linked = dir.path().join("linked");
        symlink(dir.path(), &linked).unwrap();
        std::env::set_var("DOXA_HOME", &linked);
        assert!(save(Vendor::DeepSeek, "synthetic-secret-fixture").is_err());
        std::env::set_var("DOXA_HOME", dir.path());
        symlink("missing", dir.path().join(LOCK_NAME)).unwrap();
        assert!(save(Vendor::DeepSeek, "synthetic-secret-fixture").is_err());
        assert!(!dir.path().join(FILE_NAME).exists());
    }
    #[test]
    fn opened_directory_pins_writes_and_replacement_is_refused() {
        let (_guard, dir) = fixture();
        save(Vendor::DeepSeek, "synthetic-secret-fixture").unwrap();
        let mut store = Store::open(dir.path(), false).unwrap().unwrap();
        store.values.insert("deepseek".into(), Value::String("replacement-secret-fixture".into()));
        fs::rename(dir.path().join(FILE_NAME), dir.path().join("old")).unwrap();
        fs::write(dir.path().join(FILE_NAME), "{}").unwrap();
        fs::set_permissions(dir.path().join(FILE_NAME), fs::Permissions::from_mode(0o600)).unwrap();
        assert!(store.write().is_err());
        assert_eq!(fs::read(dir.path().join(FILE_NAME)).unwrap(), b"{}");
        assert!(!fs::read_dir(dir.path()).unwrap().any(|entry| entry.unwrap().file_name().to_string_lossy().starts_with(".credentials-")));
        drop(store);
        let parent = tempfile::tempdir().unwrap();
        let home = parent.path().join("home");
        fs::create_dir(&home).unwrap();
        fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
        let store = Store::open(&home, true).unwrap().unwrap();
        fs::rename(&home, parent.path().join("original")).unwrap();
        symlink(dir.path(), &home).unwrap();
        store.write().unwrap();
        assert!(parent.path().join("original").join(FILE_NAME).exists());
        assert_eq!(fs::read(dir.path().join(FILE_NAME)).unwrap(), b"{}");
    }
    #[test]
    fn exact_known_keys_are_redacted_before_crossing_boundaries_and_inode_is_identified() {
        let (_guard, dir) = fixture();
        save(Vendor::DeepSeek, "saved-deepseek-fixture").unwrap();
        save(Vendor::Glm, "saved-zai-fixture").unwrap();
        let text = "saved-deepseek-fixture inherited-deepseek-fixture saved-zai-fixture inherited-zai-fixture ordinary";
        assert_eq!(redact(text).unwrap(), "[REDACTED] [REDACTED] [REDACTED] [REDACTED] ordinary");
        assert!(is_credential_file(&File::open(dir.path().join(FILE_NAME)).unwrap()).unwrap());
        let other = dir.path().join("other");
        fs::write(&other, "ordinary").unwrap();
        assert!(!is_credential_file(&File::open(other).unwrap()).unwrap());
        assert_eq!(format!("{:?}", status(Vendor::DeepSeek).unwrap()), "Saved");
    }
    #[test]
    fn foreign_owned_directory_and_contended_lock_are_refused() {
        let (_guard, dir) = fixture();
        if unsafe { libc::geteuid() } != 0 {
            assert!(directory(Path::new("/usr"), false).is_err());
        }
        let _store = Store::open(dir.path(), false).unwrap().unwrap();
        let start = std::time::Instant::now();
        assert_eq!(status(Vendor::DeepSeek).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        assert!(start.elapsed() < std::time::Duration::from_secs(2));
    }
    #[test]
    fn private_home_created_but_status_and_remove_do_not_create_a_missing_home() {
        let (_guard, dir) = fixture();
        let home = dir.path().join("new-home");
        std::env::set_var("DOXA_HOME", &home);
        status(Vendor::DeepSeek).unwrap();
        remove(Vendor::DeepSeek).unwrap();
        assert!(!home.exists());
        save(Vendor::DeepSeek, "saved-deepseek-fixture").unwrap();
        assert_eq!(fs::metadata(home).unwrap().mode() & 0o777, 0o700);
    }
}
