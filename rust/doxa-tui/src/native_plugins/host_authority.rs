//! A separately signed, short-lived operator permit for one running frontend.
//! Review receipts are never read. Owner package approval remains independent.
use super::{invalid, open_child, open_dir, private, MAX_CONFIG};
use super::runner_sandbox::{self, HostObservation};
use ring::signature::{UnparsedPublicKey, ED25519};
use serde::{Deserialize, Serialize};
use std::fs::{self, File, Metadata};
use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const MAX_PERMIT: u64 = 16 * 1024;
const MAX_LIFETIME: u64 = 900;
const PURPOSE: &str = "doxa-installed-plugin-tui-v1";
const CASES: [&str; 9] = ["approved-wasm", "boundary", "cpu", "launch-error", "memory",
    "module-error", "pids", "setsid-cancel", "timeout"];

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Policy { operator_public_key: String, epoch: u64 }

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Envelope { payload_hex: String, signature_hex: String }

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Instance {
    uid: u32, pid: u32, start_ticks: u64, boot_id: String,
    home_device: u64, home_inode: u64,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Permit {
    format_version: u32, purpose: String, epoch: u64,
    issued_at: u64, expires_at: u64, instance: Instance,
    issued_boottime_ms: u64, expires_boottime_ms: u64,
    host: HostObservation, cases: Vec<String>, proof_log_sha256: String,
}

fn hex(value: &str, size: usize) -> io::Result<Vec<u8>> {
    if value.len() != size * 2 || !value.bytes().all(|byte| byte.is_ascii_digit()
        || (b'a'..=b'f').contains(&byte)) { return Err(invalid("invalid plugin authority hex")); }
    value.as_bytes().chunks_exact(2).map(|pair| {
        u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16)
            .map_err(|_| invalid("invalid plugin authority hex"))
    }).collect()
}

fn now() -> io::Result<u64> {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|value| value.as_secs())
        .map_err(|_| invalid("plugin authority clock is before epoch"))
}

fn boottime_ms() -> io::Result<u64> {
    let mut stamp: libc::timespec = unsafe { std::mem::zeroed() };
    if unsafe { libc::clock_gettime(libc::CLOCK_BOOTTIME, &mut stamp) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if stamp.tv_sec < 0 || !(0..1_000_000_000).contains(&stamp.tv_nsec) {
        return Err(invalid("invalid plugin host boot clock"));
    }
    (stamp.tv_sec as u64).checked_mul(1000)
        .and_then(|millis| millis.checked_add(stamp.tv_nsec as u64 / 1_000_000))
        .ok_or_else(|| invalid("plugin host boot clock overflow"))
}

fn start_ticks(text: &str) -> io::Result<u64> {
    // The comm field may itself contain spaces and ')'. Field 22 follows the
    // last ')', not a whitespace split of the complete proc record.
    text.rsplit_once(')').and_then(|(_, fields)| fields.split_whitespace().nth(19))
        .and_then(|field| field.parse().ok()).ok_or_else(|| invalid("invalid frontend process identity"))
}

fn instance(home: &Metadata) -> io::Result<Instance> {
    let boot_id = fs::read_to_string("/proc/sys/kernel/random/boot_id")?.trim().to_owned();
    if boot_id.len() != 36 { return Err(invalid("invalid boot identity")); }
    Ok(Instance { uid: unsafe { libc::geteuid() }, pid: std::process::id(),
        start_ticks: start_ticks(&fs::read_to_string("/proc/self/stat")?)?, boot_id,
        home_device: home.dev(), home_inode: home.ino() })
}

fn unchanged(before: &Metadata, after: &Metadata, directory: bool) -> bool {
    before.dev() == after.dev() && before.ino() == after.ino() && before.uid() == after.uid()
        && before.mode() == after.mode()
        && (directory || before.nlink() == after.nlink() && before.len() == after.len() && before.mtime() == after.mtime()
            && before.mtime_nsec() == after.mtime_nsec() && before.ctime() == after.ctime()
            && before.ctime_nsec() == after.ctime_nsec())
}

struct Watched { path: PathBuf, file: File, metadata: Metadata, directory: bool }

impl Watched {
    fn current(&self) -> bool {
        // Do not follow a replaced path; a held descriptor alone cannot detect
        // unlink revocation. Directory timestamps change during normal runs.
        self.file.metadata().is_ok_and(|meta| unchanged(&self.metadata, &meta, self.directory))
            && fs::symlink_metadata(&self.path).is_ok_and(|meta|
                !meta.file_type().is_symlink() && unchanged(&self.metadata, &meta, self.directory))
    }
}

/// Constructible only by signature validation against an explicitly enrolled
/// owner key. Lifetime is also bounded by a monotonic deadline, so a wall-clock
/// rollback cannot extend an already admitted run.
pub(crate) struct InstalledHostAuthority {
    pub(super) host: HostObservation,
    home: PathBuf,
    membership: String,
    expires_at: u64,
    expires_boottime_ms: u64,
    deadline: Instant,
    watched: Vec<Watched>,
}

fn verify(bytes: &[u8], policy: &Policy, expected: &Instance, host: &HostObservation,
    clock: u64, boot_clock_ms: u64) -> io::Result<Permit> {
    let envelope: Envelope = serde_json::from_slice(bytes).map_err(|_| invalid("invalid plugin authority envelope"))?;
    if envelope.payload_hex.len() > MAX_PERMIT as usize { return Err(invalid("plugin authority payload too large")); }
    let payload = hex(&envelope.payload_hex, envelope.payload_hex.len() / 2)?;
    let signature = hex(&envelope.signature_hex, 64)?;
    let key = hex(&policy.operator_public_key, 32)?;
    UnparsedPublicKey::new(&ED25519, key).verify(&payload, &signature)
        .map_err(|_| invalid("plugin authority signature is not from the enrolled operator"))?;
    let permit: Permit = serde_json::from_slice(&payload).map_err(|_| invalid("invalid plugin authority payload"))?;
    if permit.format_version != 1 || permit.purpose != PURPOSE || policy.epoch == 0
        || permit.epoch != policy.epoch || permit.instance != *expected || permit.host != *host
        || permit.issued_at > clock || permit.expires_at <= clock
        || permit.expires_at <= permit.issued_at
        || permit.expires_at - permit.issued_at > MAX_LIFETIME
        || permit.issued_boottime_ms > boot_clock_ms || permit.expires_boottime_ms <= boot_clock_ms
        || permit.expires_boottime_ms <= permit.issued_boottime_ms
        || permit.expires_boottime_ms - permit.issued_boottime_ms != (permit.expires_at - permit.issued_at) * 1000
        || permit.cases.iter().map(String::as_str).collect::<Vec<_>>() != CASES
        || hex(&permit.proof_log_sha256, 32).is_err() {
        return Err(invalid("plugin authority is expired, revoked or bound to a different instance or host"));
    }
    Ok(permit)
}

pub(super) fn load(home: &Path) -> io::Result<InstalledHostAuthority> {
    let directory = open_dir(home)?;
    let home_meta = directory.metadata()?;
    private(&home_meta, true, true)?;
    if !home.is_absolute() || home.canonicalize()?.ancestors().any(|path| path.join(".git").exists()) {
        return Err(invalid("plugin authority home must be absolute and outside a repository"));
    }
    let expected = instance(&home_meta)?;
    let (config, config_bytes, config_meta) = open_child(&directory, "config.toml", false, true, MAX_CONFIG)?;
    let table: toml::Table = toml::from_str(std::str::from_utf8(&config_bytes)
        .map_err(|_| invalid("plugin authority config must be UTF-8"))?)
        .map_err(|_| invalid("invalid plugin authority config"))?;
    let policy: Policy = table.get("native_plugin_host").ok_or_else(|| invalid("no enrolled plugin host operator"))?
        .clone().try_into().map_err(|_| invalid("invalid plugin host operator enrollment"))?;
    let (permits, _, permits_meta) = open_child(&directory, "native-plugin-authority", true, true, 0)?;
    let name = format!("{}-{}.json", expected.pid, expected.start_ticks);
    let (file, bytes, meta) = open_child(&permits, &name, false, true, MAX_PERMIT)?;
    let frontend = std::env::current_exe()?;
    let worker = frontend.with_file_name("doxa-plugin-worker");
    let membership = fs::read_to_string("/proc/self/cgroup")?;
    let host = runner_sandbox::host_observation(&frontend, &worker)?;
    let clock = now()?;
    let boot_clock_ms = boottime_ms()?;
    let permit = verify(&bytes, &policy, &expected, &host, clock, boot_clock_ms)?;
    let mut watched = vec![
        Watched { path: home.to_owned(), file: directory, metadata: home_meta, directory: true },
        Watched { path: home.join("config.toml"), file: config, metadata: config_meta, directory: false },
        Watched { path: home.join("native-plugin-authority"), file: permits, metadata: permits_meta, directory: true },
        Watched { path: home.join("native-plugin-authority").join(name), file, metadata: meta, directory: false },
    ];
    for path in [frontend, worker, PathBuf::from("/usr/bin/bwrap"), host.parent.clone(),
        Path::new("/sys/fs/cgroup").join(membership.trim().strip_prefix("0::/")
            .ok_or_else(|| invalid("invalid plugin authority membership"))?)] {
        let file = File::open(&path)?;
        let metadata = file.metadata()?;
        let directory = metadata.is_dir();
        watched.push(Watched { path, file, metadata, directory });
    }
    let authority = InstalledHostAuthority { host, home: home.to_owned(), membership,
        expires_at: permit.expires_at, expires_boottime_ms: permit.expires_boottime_ms,
        deadline: Instant::now() + Duration::from_millis(permit.expires_boottime_ms - boot_clock_ms), watched };
    authority.check(home)?;
    Ok(authority)
}

impl InstalledHostAuthority {
    pub(super) fn check(&self, home: &Path) -> io::Result<()> {
        if home != self.home || Instant::now() >= self.deadline || now()? >= self.expires_at
            || boottime_ms()? >= self.expires_boottime_ms
            || !self.watched.iter().all(Watched::current)
            || fs::read_to_string("/proc/self/cgroup")? != self.membership {
            return Err(invalid("installed plugin authority expired, revoked or changed"));
        }
        Ok(())
    }

    /// Keep revocation effective while Wasm is running, and synchronously join
    /// the watcher before returning. The sandbox owns descendant cleanup.
    pub(super) fn supervise<T>(&self, home: &Path, cancel: &AtomicBool,
        run: impl FnOnce() -> io::Result<T>) -> io::Result<T> {
        self.check(home)?;
        let finished = AtomicBool::new(false);
        std::thread::scope(|scope| {
            let watcher = scope.spawn(|| {
                while !finished.load(Ordering::Acquire) {
                    if self.check(home).is_err() { cancel.store(true, Ordering::Release); break; }
                    std::thread::sleep(Duration::from_millis(20));
                }
            });
            struct Finish<'a>(&'a AtomicBool);
            impl Drop for Finish<'_> {
                fn drop(&mut self) { self.0.store(true, Ordering::Release); }
            }
            let finish = Finish(&finished);
            let result = run();
            drop(finish);
            watcher.join().map_err(|_| invalid("plugin authority watcher stopped"))?;
            self.check(home)?;
            result
        })
    }
}

#[cfg(test)]
pub(super) fn test_authority() -> InstalledHostAuthority {
    InstalledHostAuthority { host: serde_json::from_value(serde_json::json!({
        "parent":"/unused", "parent_device":0,"parent_inode":0,"supervisor_device":0,"supervisor_inode":0,
        "frontend":{"device":0,"inode":0,"bytes":0,"sha256":""},
        "worker":{"device":0,"inode":0,"bytes":0,"sha256":""},
        "bwrap":{"device":0,"inode":0,"bytes":0,"sha256":""}
    })).unwrap(), home: PathBuf::new(), membership:String::new(), expires_at:0, expires_boottime_ms:0,
        deadline:Instant::now(), watched:Vec::new() }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ring::signature::{Ed25519KeyPair, KeyPair};
    fn envelope(permit: &Permit, key: &Ed25519KeyPair) -> Vec<u8> {
        let payload = serde_json::to_vec(permit).unwrap();
        fn encode(bytes: &[u8]) -> String { bytes.iter().map(|byte| format!("{byte:02x}")).collect() }
        serde_json::to_vec(&serde_json::json!({"payload_hex":encode(&payload),
            "signature_hex":encode(key.sign(&payload).as_ref())})).unwrap()
    }
    #[test]
    fn signed_permit_binds_instance_host_epoch_and_short_lifetime() {
        let key = Ed25519KeyPair::from_seed_unchecked(&[7;32]).unwrap();
        let public = key.public_key().as_ref().iter().map(|byte| format!("{byte:02x}")).collect();
        let mut policy = Policy { operator_public_key: public, epoch:1 };
        let instance = Instance { uid:1000,pid:42,start_ticks:900,boot_id:"boot-one".into(),home_device:1,home_inode:3 };
        let host = test_authority().host;
        let mut permit = Permit { format_version:1,purpose:PURPOSE.into(),epoch:1,issued_at:100,expires_at:200,
            issued_boottime_ms:1000,expires_boottime_ms:101000,
            instance:instance.clone(),host:host.clone(),cases:CASES.iter().map(|value| (*value).into()).collect(),
            proof_log_sha256:"a".repeat(64) };
        let signed = envelope(&permit,&key);
        assert!(verify(&signed,&policy,&instance,&host,150,1500).is_ok());
        // Fresh validation of the same signed bytes has no retained Instant
        // state. Rolling wall time backwards cannot revive boot-expired bytes.
        assert!(verify(&signed,&policy,&instance,&host,190,100999).is_ok());
        assert!(verify(&signed,&policy,&instance,&host,150,101000).is_err());
        assert!(verify(&signed,&policy,&instance,&host,120,101001).is_err());
        let mut changed_host = host.clone(); changed_host.parent = PathBuf::from("/different");
        assert!(verify(&signed,&policy,&instance,&changed_host,150,1500).is_err());
        let mut changed = instance.clone(); changed.start_ticks += 1;
        assert!(verify(&signed,&policy,&changed,&host,150,1500).is_err());
        changed = instance.clone(); changed.boot_id = "boot-two".into();
        assert!(verify(&signed,&policy,&changed,&host,150,1500).is_err());
        assert!(verify(&signed,&policy,&instance,&host,200,1500).is_err());
        assert!(verify(&signed,&policy,&instance,&host,99,1500).is_err());
        policy.epoch = 2;
        assert!(verify(&signed,&policy,&instance,&host,150,1500).is_err());
        policy.epoch = 1;
        policy.operator_public_key = "00".repeat(32);
        assert!(verify(&signed,&policy,&instance,&host,150,1500).is_err());
        policy.operator_public_key = key.public_key().as_ref().iter().map(|byte| format!("{byte:02x}")).collect();
        permit.expires_at = 1001;
        assert!(verify(&envelope(&permit,&key),&policy,&instance,&host,150,1500).is_err());
        permit.expires_at = 200; permit.cases.pop();
        assert!(verify(&envelope(&permit,&key),&policy,&instance,&host,150,1500).is_err());
        assert!(verify(br#"{"tui_execution_authorized":true}"#,&policy,&instance,&host,150,1500).is_err());
    }
    #[test]
    fn proc_identity_handles_parentheses_and_spaces_in_comm() {
        let suffix = (1..=20).map(|value| value.to_string()).collect::<Vec<_>>().join(" ");
        assert_eq!(start_ticks(&format!("42 (odd ) process) {suffix}" )).unwrap(),20);
        assert!(start_ticks("42 (truncated) S").is_err());
    }
    #[test]
    fn revoke_by_unlink_or_replace_is_detected_even_with_open_descriptor() {
        let temp = tempfile::tempdir().unwrap(); let path = temp.path().join("permit");
        fs::write(&path,b"old").unwrap(); let file = File::open(&path).unwrap();
        let watched = Watched { path:path.clone(), metadata:file.metadata().unwrap(),file,directory:false };
        assert!(watched.current()); fs::remove_file(&path).unwrap(); assert!(!watched.current());
        fs::write(&path,b"old").unwrap(); assert!(!watched.current());
    }

    #[test]
    fn revocation_cancels_and_reaps_an_already_running_process_group() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("permit");
        fs::write(&path,b"permit").unwrap();
        let file = File::open(&path).unwrap();
        let mut authority = test_authority();
        authority.home = temp.path().to_owned();
        authority.membership = fs::read_to_string("/proc/self/cgroup").unwrap();
        authority.expires_at = now().unwrap() + 10;
        authority.expires_boottime_ms = boottime_ms().unwrap() + 10_000;
        authority.deadline = Instant::now() + Duration::from_secs(10);
        authority.watched.push(Watched { path:path.clone(), metadata:file.metadata().unwrap(), file, directory:false });
        let cancel = AtomicBool::new(false);
        let revoker = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(100)); fs::remove_file(path).unwrap();
        });
        let mut capture = None;
        let result = authority.supervise(temp.path(), &cancel, || {
            let mut command = std::process::Command::new("/bin/sleep");
            command.arg("30");
            capture = Some(super::super::runner_process::supervise(&mut command, &cancel,
                Instant::now() + Duration::from_secs(2))?);
            Ok(())
        });
        revoker.join().unwrap();
        assert!(result.is_err());
        assert!(cancel.load(Ordering::Acquire));
        let capture = capture.unwrap();
        assert_eq!(capture.outcome, super::super::runner_process::Outcome::Cancelled);
        assert_eq!(unsafe { libc::kill(capture.group_id as i32, 0) }, -1);
    }

    #[test]
    fn expired_or_changed_policy_never_enters_worker() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("config");
        fs::write(&path,b"epoch=1").unwrap();
        let file = File::open(&path).unwrap();
        let mut authority = test_authority();
        authority.home = temp.path().to_owned();
        authority.membership = fs::read_to_string("/proc/self/cgroup").unwrap();
        authority.expires_at = now().unwrap() + 10;
        authority.expires_boottime_ms = boottime_ms().unwrap() + 10_000;
        authority.deadline = Instant::now() + Duration::from_secs(10);
        authority.watched.push(Watched { path:path.clone(),metadata:file.metadata().unwrap(),file,directory:false });
        assert!(authority.check(temp.path()).is_ok());
        fs::write(path,b"epoch=2").unwrap();
        assert!(authority.supervise(temp.path(), &AtomicBool::new(false),
            || -> io::Result<()> { panic!("revoked authority dispatched") }).is_err());
        authority.watched.clear();
        authority.deadline = Instant::now();
        assert!(authority.supervise(temp.path(), &AtomicBool::new(false),
            || -> io::Result<()> { panic!("expired authority dispatched") }).is_err());
    }
}
