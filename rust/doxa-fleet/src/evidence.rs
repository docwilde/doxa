//! Host-issued, keyed evidence. The key lives next to the private fleet journal,
//! outside Docker worker mounts. A JSON claim or a content hash is not a receipt.
use crate::{evidence_id, hash, invalid, Assignment, Context, Envelope, State};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{fs, io::{self, Read, Write}, os::unix::fs::{MetadataExt, OpenOptionsExt}, path::{Path, PathBuf}};

const KEY_BYTES: usize = 32;
const MAX_OUTPUT: u64 = 64 * 1024;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    pub fleet_id: String,
    pub charter_sha256: String,
    pub assignment_id: String,
    pub session_id: String,
    pub base_commit: String,
    pub snapshot_sha256: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DiffEvidence {
    pub binding: Binding,
    pub changed_paths: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TestEvidence {
    pub binding: Binding,
    pub recipe_sha256: String,
    pub runner_image: String,
    pub exit_code: i32,
    pub passed: bool,
    pub duration_ms: u64,
    pub output_sha256: String,
    pub output_bytes: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct TestRecipe {
    pub argv: Vec<String>,
    pub cwd_relative: String,
    pub timeout_s: u64,
}
impl TestRecipe {
    pub fn validate(&self) -> io::Result<()> {
        let Some(program) = self.argv.first() else { return Err(invalid("fleet test recipe needs a program")); };
        if self.argv.len() > 32 || self.argv.iter().map(String::len).sum::<usize>() > 4096
            || self.argv.iter().any(|part| part.is_empty() || part.chars().any(char::is_control))
            || !program.starts_with('/') || program.split('/').any(|part| part == "..")
            || !["/usr/bin/", "/usr/local/bin/", "/opt/"].iter().any(|prefix| program.starts_with(prefix))
            || self.cwd_relative.starts_with('/') || self.cwd_relative.len() > 512
            || (!self.cwd_relative.is_empty() && self.cwd_relative.split('/').any(|part| part.is_empty() || part == ".." || part == "."))
            || self.cwd_relative.chars().any(char::is_control) || !(1..=300).contains(&self.timeout_s) {
            return Err(invalid("fleet test recipe exceeds approved argv, cwd or timeout bounds"));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    kind: String,
    host_verified: bool,
    payload: Value,
    mac: String,
}

fn key_path(context: &Context) -> io::Result<PathBuf> {
    Ok(context.state_path.parent().ok_or_else(|| invalid("fleet journal has no directory"))?.join("evidence.key"))
}

/// Call once before worker dispatch. No existing key is ever replaced.
pub fn create_key(context: &Context) -> io::Result<()> {
    let path = key_path(context)?;
    let mut key = [0u8; KEY_BYTES];
    // The OS random source is required; a failed draw cannot create evidence.
    let mut random = fs::File::open("/dev/urandom")?;
    random.read_exact(&mut key)?;
    let mut file = fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(path)?;
    file.write_all(&key)?;
    file.sync_all()?;
    fs::File::open(context.state_path.parent().unwrap())?.sync_all()
}

fn key(context: &Context) -> io::Result<[u8; KEY_BYTES]> {
    let path = key_path(context)?;
    let mut file = fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file() || meta.nlink() != 1 || meta.uid() != unsafe { libc::geteuid() }
        || meta.mode() & 0o077 != 0 || meta.len() != KEY_BYTES as u64 {
        return Err(invalid("unsafe fleet evidence key"));
    }
    let mut bytes = [0u8; KEY_BYTES];
    file.read_exact(&mut bytes)?;
    Ok(bytes)
}

fn hmac_sha256(key: &[u8; KEY_BYTES], data: &[u8]) -> String {
    let mut inner = [0x36u8; 64];
    let mut outer = [0x5cu8; 64];
    for index in 0..KEY_BYTES { inner[index] ^= key[index]; outer[index] ^= key[index]; }
    let inner_digest = Sha256::new().chain_update(inner).chain_update(data).finalize();
    format!("{:x}", Sha256::new().chain_update(outer).chain_update(inner_digest).finalize())
}

pub fn issue(context: &Context, kind: &str, payload: Value) -> io::Result<(String, Value)> {
    if !matches!(kind, "git_diff" | "test_result") { return Err(invalid("unsupported fleet evidence kind")); }
    let material = json!({"kind":kind,"host_verified":true,"payload":payload});
    let receipt = Receipt { kind: kind.into(), host_verified: true, payload,
        mac: hmac_sha256(&key(context)?, &serde_json::to_vec(&material)?) };
    let value = serde_json::to_value(receipt)?;
    Ok((evidence_id(&value)?, value))
}

fn valid_digest(value: &str) -> bool { value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit()) }
fn binding_matches(binding: &Binding, context: &Context, assignment: &Assignment) -> bool {
    binding.fleet_id == context.charter.fleet_id && binding.charter_sha256 == context.charter_sha256
        && binding.assignment_id == assignment.id && binding.session_id == assignment.session_id
        && assignment.base_commit.as_deref() == Some(binding.base_commit.as_str())
        && valid_digest(&binding.snapshot_sha256)
}

fn verify(context: &Context, id: &str, value: &Value) -> io::Result<Receipt> {
    if evidence_id(value)? != id { return Err(invalid("fleet evidence content ID changed")); }
    let receipt: Receipt = serde_json::from_value(value.clone()).map_err(|_| invalid("invalid fleet evidence receipt"))?;
    if !receipt.host_verified || !matches!(receipt.kind.as_str(), "git_diff" | "test_result") || !valid_digest(&receipt.mac) {
        return Err(invalid("fleet evidence is not a signed host receipt"));
    }
    let material = json!({"kind":receipt.kind,"host_verified":receipt.host_verified,"payload":receipt.payload});
    let expected = hmac_sha256(&key(context)?, &serde_json::to_vec(&material)?);
    // Fixed-length byte comparison avoids exposing a partial MAC match.
    if receipt.mac.bytes().zip(expected.bytes()).fold(0u8, |acc, (a, b)| acc | (a ^ b)) != 0 {
        return Err(invalid("fleet evidence host MAC changed"));
    }
    Ok(receipt)
}

/// Read a recorded host test receipt for audit/reporting. This verifies the
/// host MAC and immutable assignment binding, but does not claim that the
/// source tree still matches the tested snapshot.
pub fn recorded_test(context: &Context, id: &str, value: &Value) -> io::Result<Option<TestEvidence>> {
    if value["kind"] != "test_result" { return Ok(None); }
    let receipt = verify(context, id, value)?;
    let test: TestEvidence = serde_json::from_value(receipt.payload)
        .map_err(|_| invalid("invalid recorded host test receipt"))?;
    let assignment = context.assignments.iter().find(|row| row.id == test.binding.assignment_id)
        .ok_or_else(|| invalid("recorded test assignment is outside the fleet"))?;
    if !binding_matches(&test.binding, context, assignment)
        || context.charter.test_recipe.as_ref().is_none_or(|recipe| hash(recipe).ok().as_deref() != Some(test.recipe_sha256.as_str()))
        || test.duration_ms > 300_000 || test.output_bytes > MAX_OUTPUT
        || !valid_digest(&test.output_sha256) || !test.runner_image.contains("@sha256:") {
        return Err(invalid("recorded host test receipt has wrong scope or bounds"));
    }
    Ok(Some(test))
}

/// Exact sender, charter, baseline, and tree must agree across both receipts.
/// The current tree is checked by the daemon before calling this function.
fn validate_receipts(context: &Context, state: &State, envelope: &Envelope) -> io::Result<(String, String)> {
    receipt_pair(context, state, &envelope.from_session, &envelope.artifact_refs)
}

/// Recheck the persisted receipt pair without touching the current checkout.
/// This proves only what admission recorded at that time, not current source.
pub fn recorded_completion_receipts(context: &Context, state: &State, from_session: &str, artifact_refs: &[String]) -> io::Result<()> {
    receipt_pair(context, state, from_session, artifact_refs).map(|_| ())
}

fn receipt_pair(context: &Context, state: &State, from_session: &str, artifact_refs: &[String]) -> io::Result<(String, String)> {
    let assignment = context.assignment(from_session)?;
    let mut diff: Option<DiffEvidence> = None;
    let mut test: Option<TestEvidence> = None;
    for id in artifact_refs {
        let Some(value) = state.artifacts.get(id) else { continue; };
        let receipt = verify(context, id, value)?;
        match receipt.kind.as_str() {
            "git_diff" => {
                let item: DiffEvidence = serde_json::from_value(receipt.payload).map_err(|_| invalid("invalid host diff receipt"))?;
                if !binding_matches(&item.binding, context, assignment) || item.changed_paths.is_empty()
                    || item.changed_paths.len() > 4096 || item.changed_paths.iter().any(|path| !assignment.permits(&context.charter, path)) {
                    return Err(invalid("host diff receipt has wrong scope"));
                }
                diff = Some(item);
            }
            "test_result" => {
                let item: TestEvidence = serde_json::from_value(receipt.payload).map_err(|_| invalid("invalid host test receipt"))?;
                let recipe=context.charter.test_recipe.as_ref().ok_or_else(||invalid("fleet charter has no approved test recipe"))?;
                if !binding_matches(&item.binding, context, assignment) || !item.passed || item.exit_code != 0
                    || item.recipe_sha256 != hash(recipe)? || !valid_digest(&item.output_sha256)
                    || item.output_bytes > MAX_OUTPUT || item.duration_ms > 300_000
                    || !item.runner_image.contains("@sha256:") {
                    return Err(invalid("host test receipt is not a passing bounded run"));
                }
                test = Some(item);
            }
            _ => return Err(invalid("unsupported host receipt")),
        }
    }
    let (Some(diff), Some(test)) = (diff, test) else { return Err(invalid("completion requires signed host diff and passing test receipts")); };
    if diff.binding.snapshot_sha256 != test.binding.snapshot_sha256 { return Err(invalid("fleet diff and test refer to different snapshots")); }
    Ok((diff.binding.snapshot_sha256, test.runner_image))
}

fn match_current(context: &Context, state: &State, envelope: &Envelope, current_sha256: &str, current_image: &str) -> io::Result<String> {
    let (expected, runner_image) = validate_receipts(context, state, envelope)?;
    if expected != current_sha256 || runner_image != current_image {
        return Err(invalid("fleet source or runner image changed after host test"));
    }
    Ok(expected)
}

pub fn completion_snapshot(context: &Context, state: &State, envelope: &Envelope) -> io::Result<String> {
    validate_receipts(context,state,envelope)?;
    let assignment = context.assignment(&envelope.from_session)?;
    let manifest=doxa_isolation::workspace::manifest_for(Path::new(&assignment.cwd))?
        .ok_or_else(||invalid("fleet worker has no Docker manifest"))?;
    if manifest.profile!=doxa_isolation::Profile::DockerOffline || manifest.session_id!=assignment.session_id {
        return Err(invalid("fleet test worker is not the approved offline Docker session"));
    }
    let actual = doxa_isolation::test_runner::capture(Path::new(&assignment.cwd), None)?;
    match_current(context,state,envelope,&actual.sha256,
        &manifest.policy.as_ref().ok_or_else(||invalid("fleet test image unavailable"))?.image)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Charter, Kind, ReviewConfig};
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn hmac_matches_rfc4231_case_one() {
        // Our production keys have 32 random bytes; the HMAC primitive also
        // matches the published first SHA-256 vector for 0x0b repeated.
        let mut key = [0u8; 32]; key[..20].fill(0x0b);
        assert_eq!(hmac_sha256(&key, b"Hi There"), "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7");
    }
    #[test]
    fn only_matching_signed_receipts_and_current_snapshot_can_prove_completion() {
        let root=tempfile::tempdir().unwrap();
        fs::set_permissions(root.path(),fs::Permissions::from_mode(0o700)).unwrap();
        let recipe=TestRecipe{argv:vec!["/usr/bin/true".into()],cwd_relative:String::new(),timeout_s:5};
        let charter=Charter{version:1,fleet_id:"run".into(),task:"Task".into(),repo:"/repo".into(),allowed_paths:vec!["src".into()],required_evidence:vec![],worker_limit:2,run_budget_usd:Some(1.0),deadline:0,human_actions:vec![],test_recipe:Some(recipe.clone())};
        let context=Context{charter_sha256:hash(&charter).unwrap(),charter,assignments:vec![
            Assignment{id:"a-id".into(),session_id:"a".into(),pid:1,role:"worker".into(),task:"task".into(),cwd:"/repo-a".into(),base_commit:Some("a".repeat(40)),allowed_paths:vec![],depends_on:vec![]},
            Assignment{id:"b-id".into(),session_id:"b".into(),pid:2,role:"worker".into(),task:"task".into(),cwd:"/repo-b".into(),base_commit:Some("a".repeat(40)),allowed_paths:vec![],depends_on:vec![]}
        ],review:ReviewConfig::default(),state_path:root.path().join("guard-state.json")};
        create_key(&context).unwrap();
        let binding=Binding{fleet_id:"run".into(),charter_sha256:context.charter_sha256.clone(),assignment_id:"a-id".into(),session_id:"a".into(),base_commit:"a".repeat(40),snapshot_sha256:"b".repeat(64)};
        let diff=DiffEvidence{binding:binding.clone(),changed_paths:vec!["src/lib.rs".into()]};
        let test=TestEvidence{binding:binding.clone(),recipe_sha256:hash(&recipe).unwrap(),runner_image:format!("image@sha256:{}","c".repeat(64)),exit_code:0,passed:true,duration_ms:100,output_sha256:"d".repeat(64),output_bytes:0};
        let (diff_id,diff_value)=issue(&context,"git_diff",serde_json::to_value(diff).unwrap()).unwrap();
        let (test_id,test_value)=issue(&context,"test_result",serde_json::to_value(test.clone()).unwrap()).unwrap();
        let mut state=State::default();state.artifacts.insert(diff_id.clone(),diff_value.clone());state.artifacts.insert(test_id.clone(),test_value.clone());
        let mut message=Envelope::issue(&context,"a","b",Kind::Completion,"Done".into(),None).unwrap();
        message.artifact_refs=vec![diff_id.clone(),test_id.clone()];
        assert_eq!(match_current(&context,&state,&message,&binding.snapshot_sha256,&test.runner_image).unwrap(),binding.snapshot_sha256);
        assert!(match_current(&context,&state,&message,&"e".repeat(64),&test.runner_image).is_err(),"changed current tree invalidates test");
        assert!(match_current(&context,&state,&message,&binding.snapshot_sha256,"other@sha256:bad").is_err(),"different image invalidates test");
        let mut tampered=state.clone();tampered.artifacts.get_mut(&test_id).unwrap()["payload"]["passed"]=json!(false);
        assert!(validate_receipts(&context,&tampered,&message).is_err(),"altered receipt fails content ID and MAC");
        let mut foreign=test;foreign.binding.assignment_id="b-id".into();
        let (foreign_id,foreign_value)=issue(&context,"test_result",serde_json::to_value(foreign).unwrap()).unwrap();
        state.artifacts.insert(foreign_id.clone(),foreign_value);
        message.artifact_refs=vec![diff_id,foreign_id];
        assert!(validate_receipts(&context,&state,&message).is_err(),"worker cannot reuse another assignment's result");
    }
}
