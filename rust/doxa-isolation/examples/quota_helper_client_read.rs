//! Disposable-guest caller for the production read-only quota helper client.
//! The arguments are fixture identities; this never grants admission.
use doxa_isolation::{quota_helper::query_advisory_quota, Manifest, Profile};
use std::{env, io, path::PathBuf};

fn run() -> io::Result<()> {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.len() != 4 { return Err(io::Error::other("usage: quota_helper_client_read SESSION_ID ROOT CHECKOUT_DEVICE CHECKOUT_INODE")); }
    let root = PathBuf::from(&args[1]);
    let manifest = Manifest { version: 1, session_id: args[0].clone(), profile: Profile::DockerOffline,
        policy: None, policy_hash: String::new(), creation_policy_hash: String::new(),
        source: root.clone(), checkout: root.join("checkout"), context_cwd: None,
        provider_rollout: None, checkout_device: args[2].parse().map_err(|_| io::Error::other("invalid device"))?,
        checkout_inode: args[3].parse().map_err(|_| io::Error::other("invalid inode"))?,
        base_sha: String::new(), branch: String::new(), private_home: root.join("home"),
        cache: root.join("cache"), broker: root.join("broker"), container_id: None,
        nonce: String::new(), state: "ready".into() };
    let result = query_advisory_quota(&manifest)?;
    println!("project_id={} hard_limit_bytes={} mount_id={} descendants_checked={} broker_entries_checked={} admission=false",
        result.project_id, result.hard_limit_bytes, result.mount_id,
        result.descendants_checked, result.broker_entries_checked);
    Ok(())
}

fn main() {
    if let Err(cause) = run() { eprintln!("quota client: {cause}"); std::process::exit(1); }
}
