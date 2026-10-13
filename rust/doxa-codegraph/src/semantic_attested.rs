//! Explicit experimental definition query through an operator-provisioned
//! broker. The public key is an out-of-band trust anchor, not socket metadata.
//! A signed broker receipt is checked against fresh source bytes and pins.

use crate::{query, current_scan_input_sha256, semantic_evidence::inspect_definition_reply,
    semantic_runtime::{bounded_unix_connect, read_until, write_until}, whole_scan::current_whole_worktree_sha256, Query};
use ring::{rand::{SecureRandom, SystemRandom}, signature::{UnparsedPublicKey, ED25519}};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{io::Read, path::Path, time::{Duration, Instant}};

const PROTOCOL: &str = "doxa-semantic-attested-v1";
const MAX_REPLY: usize = 256 * 1024;
const MAX_REQUEST: usize = 16 * 1024;
pub const POLICY: &str = "immutable-root-snapshot-rootless-no-egress-v1";

fn analyzer_settings() -> Value {
    json!({"cargo":{"buildScripts":{"enable":false},"autoreload":true,"noDeps":true},
        "procMacro":{"enable":false},"checkOnSave":false,"files":{"watcher":"client"},
        "cachePriming":{"numThreads":1},"numThreads":1})
}

fn hex(bytes: &[u8]) -> String { bytes.iter().map(|byte| format!("{byte:02x}")).collect() }
fn unhex(value: &str, max: usize) -> Result<Vec<u8>, String> {
    if value.len() % 2 != 0 || value.len() > max * 2 || !value.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("invalid or oversized hexadecimal evidence".into());
    }
    (0..value.len()).step_by(2).map(|offset|
        u8::from_str_radix(&value[offset..offset + 2], 16).map_err(|e| e.to_string())).collect()
}
fn digest(bytes: &[u8]) -> String { format!("{:x}", Sha256::digest(bytes)) }
fn sha(value: &str) -> bool { value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit()) }

/// Pins must come from a reviewed provision receipt. The DOXA user must not
/// possess the private key or write/ptrace the broker or Engine identity.
#[derive(Debug)]
pub struct BrokerPins {
    pub public_key_hex: String,
    pub broker_sha256: String,
    pub config_sha256: String,
    pub engine_sha256: String,
    pub analyzer_sha256: String,
    pub image_id: String,
}

fn verify_receipt(envelope: &Value, pins: &BrokerPins, nonce: &str,
    challenge_sha: &str, snapshot_sha: &str) -> Result<Value, String> {
    let object = envelope.as_object().ok_or("invalid signed broker envelope")?;
    if object.len() != 2 { return Err("unexpected signed broker envelope fields".into()); }
    let payload = unhex(envelope["payload_hex"].as_str().ok_or("missing broker payload")?, MAX_REPLY / 2)?;
    let signature = unhex(envelope["signature_hex"].as_str().ok_or("missing broker signature")?, 64)?;
    let key = unhex(&pins.public_key_hex, 32)?;
    if key.len() != 32 || signature.len() != 64 {
        return Err("invalid Ed25519 key or signature size".into());
    }
    UnparsedPublicKey::new(&ED25519, key).verify(&payload, &signature)
        .map_err(|_| "broker signature does not match the operator trust anchor")?;
    let receipt: Value = serde_json::from_slice(&payload).map_err(|_| "invalid signed broker payload")?;
    for (field, expected) in [("protocol", PROTOCOL), ("nonce", nonce),
        ("challenge_sha256", challenge_sha), ("source_input_sha256", snapshot_sha),
        ("policy", POLICY), ("broker_sha256", pins.broker_sha256.as_str()),
        ("config_sha256", pins.config_sha256.as_str()), ("engine_sha256", pins.engine_sha256.as_str()),
        ("analyzer_sha256", pins.analyzer_sha256.as_str()), ("image_id", pins.image_id.as_str())] {
        if receipt[field] != expected { return Err(format!("signed broker receipt differs from pin: {field}")); }
    }
    if receipt["status"] != "attested" {
        let reason = receipt["error"].as_str().unwrap_or("broker did not attest this transaction");
        return Err(format!("broker declined attestation: {} (cleanup_removed={})",
            reason.chars().take(512).collect::<String>(), receipt["cleanup_removed"]));
    }
    let cid = receipt["container_id"].as_str().ok_or("missing exact container ID")?;
    if !sha(cid) || receipt["snapshot_content_sha256"].as_str().is_none_or(|value| !sha(value)) {
        return Err("invalid container or immutable snapshot digest".into());
    }
    for field in ["engine_identity_stable", "source_immutable", "effective_policy_verified",
        "quiescent", "attached_same_container", "cleanup_removed", "analyzer_exit_successful", "reader_exec_clean", "analyzer_identity_stable"] {
        if receipt[field] != true { return Err(format!("broker attestation gate failed: {field}")); }
    }
    if receipt["analyzer_settings"] != analyzer_settings() {
        return Err("broker analyzer settings differ from immutable snapshot policy".into());
    }
    if receipt["status"] != "attested" { return Err("broker did not attest this transaction".into()); }
    Ok(receipt)
}

/// Query one displayed candidate. Revalidated mutable sources establish that
/// the frozen bytes match the UI's answer; the signed broker establishes the
/// immutable mount, actual Engine attachment, runtime policy and cleanup.
pub fn definition(root: &Path, socket: &Path, pins: &BrokerPins,
    file: &str, edge_index: usize, candidate_index: usize) -> Result<Value, String> {
    if !socket.is_absolute() || !sha(&pins.broker_sha256) || !sha(&pins.config_sha256)
        || !sha(&pins.engine_sha256) || !sha(&pins.analyzer_sha256)
        || pins.image_id.strip_prefix("sha256:").is_none_or(|value| !sha(value)) {
        return Err("semantic broker requires absolute socket and exact operator pins".into());
    }
    let answer = query(root, Query::Calls(file.into()))?;
    let root = Path::new(&answer.scope);
    let scan = answer.scan_input_sha256.as_deref().ok_or("complete Rust source inventory required")?;
    let edge = answer.edges.get(edge_index).ok_or("call edge index outside displayed answer")?;
    let candidate = edge.candidates.get(candidate_index).ok_or("candidate index outside displayed edge")?;
    // This first activatable slice supports plain, nongenerated declarations.
    // Cargo cfg/macro/build reachability is outside its proof claim.
    for path in [&edge.file, &candidate.file] {
        let (content, _, _) = crate::file_bytes(root, path)?;
        crate::rust_bounds::preflight(&content)?;
        let syntax = syn::parse_file(&content).map_err(|error| error.to_string())?;
        struct Conditional(bool);
        impl<'ast> syn::visit::Visit<'ast> for Conditional {
            fn visit_attribute(&mut self, attribute: &'ast syn::Attribute) {
                if attribute.path().is_ident("cfg") || attribute.path().is_ident("cfg_attr") {
                    self.0 = true;
                }
                syn::visit::visit_attribute(self, attribute);
            }
            fn visit_item_macro(&mut self, _: &'ast syn::ItemMacro) { self.0 = true; }
        }
        let mut conditional = Conditional(false);
        syn::visit::Visit::visit_file(&mut conditional, &syntax);
        if conditional.0 || path.ends_with("build.rs") || content.contains("@generated") {
            return Err("conditional, macro-generated or generated semantic evidence remains unknown".into());
        }
    }
    let whole = current_whole_worktree_sha256(root)?;
    let request = json!({"jsonrpc":"2.0","id":3,"method":"textDocument/definition",
        "params":{"textDocument":{"uri":format!("file://{}/{}", answer.scope, edge.file)},
        "position":{"line":edge.line.checked_sub(1).ok_or("invalid call line")?,"character":edge.column}}});
    let mut random = [0u8; 32];
    SystemRandom::new().fill(&mut random).map_err(|_| "fresh broker challenge unavailable")?;
    let nonce = hex(&random);
    let challenge = json!({"protocol":PROTOCOL,"nonce":nonce,"root":answer.scope,
        "source_input_sha256":whole.digest,"rust_scan_input_sha256":scan,
        "source_sha256":edge.sha256,"target_sha256":candidate.sha256,"target_file":candidate.file,
        "request":request});
    let bytes = serde_json::to_vec(&challenge).map_err(|e| e.to_string())?;
    if bytes.len() > MAX_REQUEST { return Err("semantic challenge exceeds 16 KiB".into()); }
    let deadline = Instant::now() + Duration::from_secs(60);
    let mut stream = bounded_unix_connect(socket, deadline)?;
    write_until(&mut stream, &(bytes.len() as u32).to_be_bytes(), deadline)?;
    write_until(&mut stream, &bytes, deadline)?;
    let mut length = [0u8; 4];
    read_until(&mut stream, &mut length, deadline)?;
    let length = u32::from_be_bytes(length) as usize;
    if length == 0 || length > MAX_REPLY { return Err("semantic receipt exceeds bound".into()); }
    let mut payload = vec![0; length];
    read_until(&mut stream, &mut payload, deadline)?;
    if Instant::now() >= deadline { return Err("semantic broker transaction exceeded deadline".into()); }
    stream.set_read_timeout(Some(deadline.saturating_duration_since(Instant::now())))
        .map_err(|e| e.to_string())?;
    let mut trailing = [0u8; 1];
    if stream.read(&mut trailing).map_err(|e| format!("broker receipt EOF: {e}"))? != 0 {
        return Err("trailing bytes after signed broker receipt".into());
    }
    let envelope: Value = serde_json::from_slice(&payload).map_err(|_| "invalid semantic broker envelope")?;
    let receipt = verify_receipt(&envelope, pins, &nonce, &digest(&bytes), &whole.digest)?;
    if receipt["request"] != request { return Err("broker changed the exact LSP request".into()); }
    inspect_definition_reply(root, edge, candidate, &request, &receipt["response"])?;
    if current_scan_input_sha256(root)?.0 != scan || current_whole_worktree_sha256(root)? != whole {
        return Err("worktree inputs changed during attested definition transaction".into());
    }
    Ok(json!({"status":"attested_analyzer_definition","binding":"analyzer_resolved",
        "scope":answer.scope,"source":edge.file,"source_sha256":edge.sha256,
        "target":candidate.file,"target_sha256":candidate.sha256,"target_qualified":candidate.qualified,
        "reason":"operator_pinned_broker_attested_immutable_snapshot_and_engine_lsp_stream",
        "limit":"analyzer definition under disabled build scripts/proc macros; not compiler execution proof",
        "receipt":receipt}))
}

/// Shared installed/development CLI entry. Provisioning is always external;
/// this command only submits one explicit definition query to an existing broker.
pub fn definition_cli(args: &[String]) -> Result<Value, String> {
    use std::{io::Read, os::unix::fs::OpenOptionsExt};
    if args.len() != 15 || args[0] != "--semantic-broker" || args[1] != "--root" || args[3] != "--socket"
        || args[5] != "--pins" || args[7] != "--broker-key" || args[9] != "--file"
        || args[11] != "--edge" || args[13] != "--candidate" {
        return Err("usage: codegraph --semantic-broker --root ROOT --socket SOCKET --pins RECEIPT.json --broker-key ED25519_HEX --file PATH --edge N --candidate N".into());
    }
    let mut file = std::fs::OpenOptions::new().read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(&args[6])
        .map_err(|e| format!("broker pins: {e}"))?;
    let metadata = file.metadata().map_err(|e| e.to_string())?;
    if !metadata.is_file() || metadata.len() > 16384 { return Err("broker pins must be a regular file <=16KiB".into()); }
    let mut bytes = Vec::new(); file.by_ref().take(16385).read_to_end(&mut bytes).map_err(|e| e.to_string())?;
    if bytes.len() > 16384 { return Err("broker pins exceed16KiB".into()); }
    let values: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
    let pin = |name: &str| values[name].as_str().map(str::to_owned)
        .ok_or_else(|| format!("missing operator broker pin: {name}"));
    let pins = BrokerPins { public_key_hex:args[8].clone(), broker_sha256:pin("broker_sha256")?,
        config_sha256:pin("config_sha256")?, engine_sha256:pin("engine_sha256")?,
        analyzer_sha256:pin("analyzer_sha256")?, image_id:pin("image_id")? };
    definition(Path::new(&args[2]), Path::new(&args[4]), &pins, &args[10],
        args[12].parse().map_err(|_| "invalid edge index")?, args[14].parse().map_err(|_| "invalid candidate index")?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ring::signature::{Ed25519KeyPair, KeyPair};
    #[test]
    fn slow_receipt_trickle_cannot_reset_absolute_client_deadline() {
        use std::{io::Write, os::unix::net::UnixStream};
        let (mut client, mut server) = UnixStream::pair().unwrap();
        let sender = std::thread::spawn(move || {
            for _ in 0..20 {
                if server.write_all(b"x").is_err() { break; }
                std::thread::sleep(Duration::from_millis(10));
            }
        });
        let started = Instant::now();
        let mut body = [0u8; 20];
        assert!(read_until(&mut client, &mut body, started + Duration::from_millis(35)).is_err());
        assert!(started.elapsed() < Duration::from_millis(120));
        drop(client); sender.join().unwrap();
    }

    #[test]
    fn signatures_and_all_runtime_gates_are_required() {
        let signer = Ed25519KeyPair::from_seed_unchecked(&[19; 32]).unwrap();
        let pins = BrokerPins { public_key_hex:hex(signer.public_key().as_ref()),
            broker_sha256:"b".repeat(64), config_sha256:"c".repeat(64), engine_sha256:"e".repeat(64),
            analyzer_sha256:"a".repeat(64), image_id:format!("sha256:{}", "f".repeat(64)) };
        let receipt = json!({"protocol":PROTOCOL,"nonce":"fresh","challenge_sha256":"challenge",
            "source_input_sha256":"snapshot","policy":POLICY,"broker_sha256":pins.broker_sha256,
            "config_sha256":pins.config_sha256,"engine_sha256":pins.engine_sha256,
            "analyzer_sha256":pins.analyzer_sha256,"image_id":pins.image_id,
            "container_id":"1".repeat(64),"snapshot_content_sha256":"2".repeat(64),
            "engine_identity_stable":true,"source_immutable":true,"effective_policy_verified":true,
            "quiescent":true,"attached_same_container":true,"cleanup_removed":true,"analyzer_exit_successful":true,"reader_exec_clean":true,"analyzer_identity_stable":true,"analyzer_settings":analyzer_settings(),"status":"attested"});
        let sign = |value:&Value| {
            let bytes=serde_json::to_vec(value).unwrap();
            json!({"payload_hex":hex(&bytes),"signature_hex":hex(signer.sign(&bytes).as_ref())})
        };
        assert!(verify_receipt(&sign(&receipt), &pins,"fresh","challenge","snapshot").is_ok());
        assert!(verify_receipt(&sign(&receipt), &pins,"replay","challenge","snapshot").is_err());
        let mut declined=receipt.clone();
        declined["status"]=json!("unknown");declined["error"]=json!("project-controlled configuration override refused");
        declined.as_object_mut().unwrap().remove("container_id");
        assert!(verify_receipt(&sign(&declined), &pins,"fresh","challenge","snapshot")
            .unwrap_err().contains("configuration override refused"));
        for field in ["cleanup_removed","source_immutable","quiescent","attached_same_container"] {
            let mut altered=receipt.clone(); altered[field]=json!(false);
            assert!(verify_receipt(&sign(&altered), &pins,"fresh","challenge","snapshot").is_err());
        }
        for settings in [json!(null), json!({"cargo":{"autoreload":false}})] {
            let mut altered=receipt.clone(); altered["analyzer_settings"]=settings;
            assert!(verify_receipt(&sign(&altered), &pins,"fresh","challenge","snapshot").is_err());
        }
        let mut forged=sign(&receipt); forged["signature_hex"]=json!("00".repeat(64));
        assert!(verify_receipt(&forged, &pins,"fresh","challenge","snapshot").is_err());
    }
}
