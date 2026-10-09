//! Disposable-guest read-only quota query. Never install setuid or use for admission.
use doxa_isolation::{quota_verify::{inspect_disposable_fixture_from_fd, QuotaExpectation},
    Manifest, Profile};
use std::{env, error::Error, fs, os::{fd::{AsFd, FromRawFd}, unix::fs::MetadataExt}, path::PathBuf};

fn run() -> Result<(), Box<dyn Error>> {
    let args: Vec<String> = env::args().skip(1).collect();
    if !matches!(args.len(), 5 | 7) || args[0] != "--fixture-only"
        || (args.len() == 7 && args[5] != "--root-fd") {
        return Err("usage: quota_fixture_read --fixture-only ROOT PROJECT_ID HARD_LIMIT_BYTES OWNER_UID [--root-fd FD]".into());
    }
    let root = PathBuf::from(&args[1]);
    if !root.is_absolute() { return Err("fixture root must be absolute".into()); }
    let project_id: u32 = args[2].parse()?;
    let hard_limit_bytes: u64 = args[3].parse()?;
    let owner_uid: u32 = args[4].parse()?;
    let checkout = root.join("checkout");
    let checkout_meta = fs::metadata(&checkout)?;
    let manifest = Manifest { version: 1, session_id: "disposable-quota-fixture".into(),
        profile: Profile::DockerOffline, policy: None, policy_hash: String::new(),
        creation_policy_hash: String::new(), source: root.clone(), checkout,
        context_cwd: None, provider_rollout: None,
        checkout_device: checkout_meta.dev(), checkout_inode: checkout_meta.ino(),
        base_sha: String::new(), branch: String::new(), private_home: root.join("home"),
        cache: root.join("cache"), broker: root.join("broker"), container_id: None,
        nonce: String::new(), state: "ready".into() };
    // A caller may pass a pinned descriptor (for adversarial substitution
    // checks). It must match ROOT; no descriptor alone can select a new tree.
    let root_fd = if args.len() == 7 {
        let supplied: i32 = args[6].parse()?;
        let duplicate = unsafe { libc::fcntl(supplied, libc::F_DUPFD_CLOEXEC, 3) };
        if duplicate < 0 { return Err(std::io::Error::last_os_error().into()); }
        unsafe { fs::File::from_raw_fd(duplicate) }
    } else { fs::File::open(&root)? };
    let snapshot = inspect_disposable_fixture_from_fd(&manifest,
        QuotaExpectation { project_id, hard_limit_bytes }, owner_uid, root_fd.as_fd())?;
    println!("{}", serde_json::json!({"fixture_snapshot_verified":true,
        "admissible_as_hard_quota":false, "project_id":snapshot.project_id,
        "hard_limit_bytes":snapshot.hard_limit_bytes, "mount_id":snapshot.mount_id,
        "filesystem_device":snapshot.filesystem_device,
        "descendants_checked":snapshot.descendants_checked,
        "broker_entries_checked":snapshot.broker_entries_checked}));
    Ok(())
}

fn main() {
    if let Err(cause) = run() {
        eprintln!("quota fixture read refused: {cause}");
        std::process::exit(1);
    }
}
