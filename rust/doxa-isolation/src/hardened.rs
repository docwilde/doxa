//! Fail-closed admission seam for a future hardened Docker profile.
//!
//! A bounded EDQUOT receipt from an administrator's fixture is useful evidence,
//! but cannot authorize a different session tree or survive a restart without
//! an on-session kernel verifier. This seam keeps that distinction executable.
use crate::{error, quota_verify::{inspect_session_hard_quota, QuotaExpectation, QuotaSnapshot}, Manifest, Profile};
use serde::Deserialize;
use std::{collections::BTreeMap, io, path::PathBuf};

const MAX_RECEIPT: usize = 8192;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FixtureReceipt {
    version: u32,
    fixture: PathBuf,
    project_id: u32,
    #[serde(default)]
    expected_hard_limit_bytes: Option<u64>,
    max_write_mib: u32,
    bytes_before_edquot: BTreeMap<String, u64>,
    hard_enforcement_verified_for_fixture: bool,
    admissible_as_hard_quota: bool,
}

/// The only quota gate a hardened gateway may use. The current probe always
/// sets `admissible_as_hard_quota=false`; even a forged `true` cannot skip the
/// missing per-session kernel/restart verifier. No production caller can use a
/// fixture receipt to start or label a hardened session.
pub(crate) fn require_session_hard_quota(manifest: &Manifest, receipt: &[u8]) -> io::Result<()> {
    if manifest.profile != Profile::DockerOffline || manifest.state != "ready" {
        return Err(error("hardened admission requires a ready network-none Docker session"));
    }
    if receipt.is_empty() || receipt.len() > MAX_RECEIPT {
        return Err(error("hard-quota fixture receipt is missing or exceeds its bound"));
    }
    let proof: FixtureReceipt = serde_json::from_slice(receipt)
        .map_err(|_| error("hard-quota fixture receipt is malformed"))?;
    if proof.version != 1 || proof.project_id == 0 || !(1..=128).contains(&proof.max_write_mib)
        || !proof.hard_enforcement_verified_for_fixture
        || proof.bytes_before_edquot.len() != 3
        || ["checkout", "home", "cache"].iter().any(|source| {
            proof.bytes_before_edquot.get(*source).is_none_or(|bytes| {
                *bytes == 0 || *bytes >= u64::from(proof.max_write_mib) * 1024 * 1024
            })
        }) {
        return Err(error("hard-quota fixture receipt lacks three bounded EDQUOT proofs"));
    }
    let session_root = manifest.checkout.parent().ok_or_else(|| error("session root missing"))?;
    if proof.fixture != session_root {
        return Err(error("quota fixture proof belongs to a different session tree"));
    }
    if !proof.admissible_as_hard_quota {
        return Err(error("quota fixture proof does not authorize hard-quota admission"));
    }
    // The fixture's write cap is not its hard quota. A future owner policy must
    // supply an exact limit; no inference from the receipt is admissible.
    let hard_limit_bytes = proof.expected_hard_limit_bytes
        .ok_or_else(|| error("hardened admission lacks an exact hard block limit"))?;
    let current_snapshot = inspect_session_hard_quota(manifest, QuotaExpectation {
        project_id: proof.project_id, hard_limit_bytes,
    })?;
    require_runtime_enforcement_proof(current_snapshot)
}

fn require_runtime_enforcement_proof(_snapshot: QuotaSnapshot) -> io::Result<()> {
    // A read-only data-bind snapshot and forgeable JSON cannot attest the
    // live broker path, EDQUOT through binds, or restart/remount behavior.
    Err(error("per-session EDQUOT, broker-path and restart verification is unavailable"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Manifest;
    use std::path::PathBuf;

    fn manifest() -> Manifest {
        let root = PathBuf::from("/owner-private/isolation/session");
        Manifest { version: 1, session_id: "session".into(), profile: Profile::DockerOffline,
            policy: None, policy_hash: String::new(), creation_policy_hash: String::new(),
            source: root.clone(), checkout: root.join("checkout"), context_cwd: None,
            provider_rollout: None, checkout_device: 0, checkout_inode: 0,
            base_sha: String::new(), branch: String::new(), private_home: root.join("home"),
            cache: root.join("cache"), broker: root.join("broker"), container_id: None,
            nonce: String::new(), state: "ready".into() }
    }
    fn receipt(root: &str, admissible: bool) -> Vec<u8> {
        serde_json::to_vec(&serde_json::json!({"version":1,"fixture":root,"project_id":42,
            "max_write_mib":128,"bytes_before_edquot":{"checkout":4096,"home":4096,"cache":4096},
            "hard_enforcement_verified_for_fixture":true,"admissible_as_hard_quota":admissible})).unwrap()
    }
    #[test]
    fn fixture_receipts_never_admit_a_live_session() {
        let manifest = manifest();
        assert!(Profile::parse("docker-hardened").unwrap_err().to_string()
            .contains("per-session kernel hard-quota"));
        assert!(require_session_hard_quota(&manifest, &receipt("/different/fixture", false))
            .unwrap_err().to_string().contains("different session tree"));
        assert!(require_session_hard_quota(&manifest,
            &receipt("/owner-private/isolation/session", false))
            .unwrap_err().to_string().contains("does not authorize"));
        assert!(require_session_hard_quota(&manifest,
            &receipt("/owner-private/isolation/session", true))
            .unwrap_err().to_string().contains("exact hard block limit"));
    }
    #[test]
    fn malformed_missing_or_incomplete_proof_fails_closed() {
        let session = manifest();
        for invalid in [Vec::new(), vec![b'x'; MAX_RECEIPT + 1], b"{}".to_vec(),
            b"{\"version\":1}".to_vec()] {
            assert!(require_session_hard_quota(&session, &invalid).is_err());
        }
        let mut incomplete: serde_json::Value = serde_json::from_slice(
            &receipt("/owner-private/isolation/session", false)).unwrap();
        incomplete["bytes_before_edquot"].as_object_mut().unwrap().remove("cache");
        assert!(require_session_hard_quota(&session, &serde_json::to_vec(&incomplete).unwrap())
            .unwrap_err().to_string().contains("three bounded"));
        let mut native = manifest(); native.profile = Profile::Native;
        assert!(require_session_hard_quota(&native,
            &receipt("/owner-private/isolation/session", true))
            .unwrap_err().to_string().contains("network-none"));
    }
    #[test]
    fn positive_kernel_snapshot_still_cannot_admit_without_runtime_proof() {
        let snapshot = QuotaSnapshot { project_id: 42, hard_limit_bytes: 64 * 1024 * 1024,
            mount_id: 123, filesystem_device: 456, descendants_checked: 3,
            broker_entries_checked: 1 };
        assert!(require_runtime_enforcement_proof(snapshot).unwrap_err().to_string()
            .contains("EDQUOT, broker-path and restart"));
    }
}
