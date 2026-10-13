use doxa_isolation::quota_install::enforcement::{
    load_root_enforcement_policy, provision_session, refresh_policy_after_remount, ProvisionRequest,
};
use std::{io, path::PathBuf};

fn run() -> io::Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.len() == 2 && matches!(args[0].as_str(), "verify" | "refresh-after-remount") {
        let policy = if args[0] == "refresh-after-remount" {
            refresh_policy_after_remount(&args[1])?
        } else {
            load_root_enforcement_policy(&args[1])?
        };
        let snapshot = policy.verify_for_gateway()?;
        println!(
            "{}",
            serde_json::json!({"session_id": snapshot.policy.session_id,
            "policy_identity": snapshot.policy_identity, "project_id": snapshot.quota.project_id,
            "hard_limit_bytes": snapshot.quota.hard_limit_bytes, "mount_id": snapshot.quota.mount_id,
            "kernel_quota_verified": true, "admissible_as_hard_quota": false})
        );
        return Ok(());
    }
    if args.len() != 10 || args[0] != "create" {
        return Err(io::Error::other("usage: doxa-quota-provision create SESSION_ID ROOT CALLER_UID ENGINE_UID ENGINE_GID PROJECT_ID HARD_BYTES IMAGE WORKER_SHA256 | verify SESSION_ID"));
    }
    let number = |index: usize| {
        args[index]
            .parse::<u32>()
            .map_err(|_| io::Error::other("invalid quota integer"))
    };
    let installed = provision_session(ProvisionRequest {
        session_id: args[1].clone(),
        root: PathBuf::from(&args[2]),
        caller_uid: number(3)?,
        engine_uid: number(4)?,
        engine_gid: number(5)?,
        project_id: number(6)?,
        hard_limit_bytes: args[7]
            .parse()
            .map_err(|_| io::Error::other("invalid quota hard bytes"))?,
        worker_image: args[8].clone(),
        worker_binary_sha256: args[9].clone(),
    })?;
    println!(
        "{}",
        serde_json::json!({"policy": installed.policy, "policy_identity": installed.identity,
        "admissible_as_hard_quota": false})
    );
    Ok(())
}

fn main() {
    if let Err(cause) = run() {
        eprintln!("doxa quota provision: {cause}");
        std::process::exit(2);
    }
}
