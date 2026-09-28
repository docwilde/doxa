//! Native Codex hook carrier: bounded input, owned source proof, canonical review.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{fs, io::{self, Read}, os::unix::fs::{MetadataExt, OpenOptionsExt}, path::Path, time::{Duration, Instant}};

pub const MAX_INPUT: usize = 64 * 1024;
pub const MAX_ROLLOUT: usize = 32 * 1024 * 1024;
const MAX_LINE: usize = 1024 * 1024;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SourceProof {
    pub sha256: String, pub device: u64, pub inode: u64, pub size: u64,
    ctime: i64, ctime_nsec: i64, mtime: i64, mtime_nsec: i64,
}
impl SourceProof {
    pub fn json(&self) -> Value { json!({"sha256":self.sha256,"device":self.device,"inode":self.inode,
        "size":self.size,"ctime":self.ctime,"ctime_nsec":self.ctime_nsec}) }
}
fn identity(metadata: &fs::Metadata) -> (u64,u64,u64,i64,i64,i64,i64) {
    (metadata.dev(),metadata.ino(),metadata.len(),metadata.ctime(),metadata.ctime_nsec(),metadata.mtime(),metadata.mtime_nsec())
}

/// All ancestors and the final entry are canonical; no same-user JSONL path
/// selected by a provider can substitute for the owned source after review.
pub fn safe_read(path: &Path, limit: usize) -> io::Result<(Vec<u8>, SourceProof)> {
    if !path.is_absolute() || fs::canonicalize(path)? != path { return Err(io::Error::other("noncanonical source")); }
    let mut file = fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
    let before = file.metadata()?;
    if !before.is_file() || before.uid() != unsafe { libc::geteuid() } || before.nlink() != 1 || before.len() > limit as u64 {
        return Err(io::Error::other("unsafe review source"));
    }
    let mut bytes = Vec::new();
    file.by_ref().take(limit as u64 + 1).read_to_end(&mut bytes)?;
    let after = file.metadata()?;
    if bytes.len() > limit || identity(&before) != identity(&after) || identity(&after) != identity(&fs::symlink_metadata(path)?) {
        return Err(io::Error::other("review source changed"));
    }
    Ok((bytes.clone(), SourceProof { sha256:format!("{:x}",Sha256::digest(&bytes)), device:before.dev(), inode:before.ino(), size:before.len(),
        ctime:before.ctime(), ctime_nsec:before.ctime_nsec(), mtime:before.mtime(), mtime_nsec:before.mtime_nsec() }))
}

pub fn review(manifest_path: &Path, event: &Value, mut worker: impl FnMut(&Value) -> io::Result<bool>) -> io::Result<bool> {
    let (descriptor, _) = safe_read(manifest_path, MAX_INPUT)?;
    let manifest: Value = serde_json::from_slice(&descriptor)?;
    if manifest["version"] != crate::codex_compact::SUPPORTED_VERSION || event["hook_event_name"] != "PreCompact"
        || !matches!(event["trigger"].as_str(),Some("auto" | "manual"))
        || manifest["provider_thread"].as_str().is_none_or(str::is_empty) || event["session_id"] != manifest["provider_thread"]
        || manifest["lore_enabled"] != true || crate::review_worker::review_disabled() { return Ok(false); }
    let source = Path::new(event["transcript_path"].as_str().ok_or_else(|| io::Error::other("missing transcript"))?);
    let root = fs::canonicalize(manifest["codex_home"].as_str().ok_or_else(|| io::Error::other("missing Codex home"))?)?;
    if !source.starts_with(root.join("sessions")) && !source.starts_with(root.join("archived_sessions")) { return Ok(false); }
    let (bytes, before) = safe_read(source, MAX_ROLLOUT)?;
    if bytes.is_empty() || bytes.split(|byte| *byte == b'\n').any(|line| line.len() > MAX_LINE) { return Ok(false); }
    let first: Value = serde_json::from_slice(bytes.split(|byte| *byte == b'\n').next().unwrap_or_default())?;
    if first["type"] != "session_meta" || first["payload"]["id"] != manifest["provider_thread"] { return Ok(false); }
    let cwd = manifest["cwd"].as_str().filter(|cwd| Path::new(cwd).is_absolute()).ok_or_else(|| io::Error::other("invalid cwd"))?;
    let session = manifest["doxa_session"].as_str().filter(|id| !id.is_empty() && id.len() <= 128 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b)))
        .ok_or_else(|| io::Error::other("invalid session"))?;
    let approved = worker(&json!({"cwd":cwd,"session_id":session,"provider_thread":manifest["provider_thread"],"transcript":source,
        "older":true,"expected_source":before.json()}))?;
    Ok(approved && safe_read(source, MAX_ROLLOUT)?.1 == before)
}

fn read_input(deadline: Instant) -> io::Result<Vec<u8>> {
    let mut bytes = Vec::new();
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() { return Err(io::Error::other("hook input timed out")); }
        let mut fd = libc::pollfd { fd:0, events:libc::POLLIN, revents:0 };
        let ready = unsafe { libc::poll(&mut fd, 1, remaining.as_millis().min(1000) as i32) };
        if ready < 0 { return Err(io::Error::last_os_error()); }
        if ready == 0 { continue; }
        let mut chunk = [0u8;8192];
        let count = unsafe { libc::read(0,chunk.as_mut_ptr().cast(),chunk.len()) };
        if count < 0 { return Err(io::Error::last_os_error()); }
        if count == 0 { return Ok(bytes); }
        bytes.extend_from_slice(&chunk[..count as usize]);
        if bytes.len() > MAX_INPUT { return Err(io::Error::other("hook input too large")); }
    }
}

/// Native daemon dispatch handler. Expected errors always produce blocking JSON
/// with exit status zero before Codex's 240 second command timeout.
pub fn hook_main(manifest: &Path, expected_digest: &str) -> Value {
    let result = std::panic::catch_unwind(|| -> io::Result<bool> {
        let deadline = Instant::now() + Duration::from_secs(210);
        let executable = fs::canonicalize(std::env::current_exe()?)?;
        if safe_read(&executable, 256 * 1024 * 1024)?.1.sha256 != expected_digest { return Ok(false); }
        let event: Value = serde_json::from_slice(&read_input(deadline)?)?;
        review(manifest, &event, |metadata| {
            let remaining = deadline.saturating_duration_since(Instant::now()).min(crate::review_worker::REVIEW_TIMEOUT);
            crate::review_worker::review(&executable,metadata,"codex",remaining,|| Instant::now() >= deadline)
        })
    }).ok().and_then(Result::ok).unwrap_or(false);
    if result { json!({"continue":true,"suppressOutput":true}) }
    else { json!({"continue":false,"suppressOutput":true,"stopReason":"DOXA LORE review did not complete; compaction blocked"}) }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn changed_rollout_and_forged_owned_source_cannot_authorize_compaction() {
        let dir = tempfile::tempdir().unwrap(); let sessions = dir.path().join("sessions"); fs::create_dir(&sessions).unwrap();
        let transcript = sessions.join("thread.jsonl"); let raw = br#"{"type":"session_meta","payload":{"id":"provider-thread"}}"#;
        fs::write(&transcript,raw).unwrap();
        let manifest = dir.path().join("manifest.json");
        fs::write(&manifest,json!({"version":"0.156.1","provider_thread":"provider-thread","doxa_session":"doxa-session","codex_home":dir.path(),"cwd":dir.path(),"lore_enabled":true}).to_string()).unwrap();
        let event = json!({"hook_event_name":"PreCompact","trigger":"manual","session_id":"provider-thread","transcript_path":transcript});
        assert!(review(&manifest,&event,|metadata| { assert_eq!(metadata["expected_source"]["size"],raw.len()); Ok(true) }).unwrap());
        assert!(!review(&manifest,&event,|_| { fs::write(&transcript,format!("{}\n{{}}",String::from_utf8_lossy(raw))).unwrap(); Ok(true) }).unwrap());
        let mut forged = event; forged["session_id"] = json!("foreign");
        assert!(!review(&manifest,&forged,|_| panic!("must not review foreign session")).unwrap());
    }
}
