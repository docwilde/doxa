//! Native Codex hook carrier: bounded input, owned source proof, canonical review.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{fs, io::{self, Read}, os::unix::fs::{MetadataExt, OpenOptionsExt}, path::Path, time::{Duration, Instant}};

pub const MAX_INPUT: usize = 64 * 1024;
pub const MAX_ROLLOUT: usize = doxa_isolation::MAX_PROVIDER_REVIEW_BYTES;
mod rollout;

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
    read_owned(path,limit,false)
}
/// Cargo uses hardlinks between its debug entrypoint and artifact. Executable
/// trust pins the bytes and inode; hardlinks never relax transcript ownership.
pub fn executable_digest(path:&Path)->io::Result<String>{
    let (_,proof)=read_owned(path,256*1024*1024,true)?;Ok(proof.sha256)
}
fn read_owned(path: &Path, limit: usize, executable:bool) -> io::Result<(Vec<u8>, SourceProof)> {
    if !path.is_absolute() || fs::canonicalize(path)? != path { return Err(io::Error::other("noncanonical source")); }
    let mut file = fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
    let before = file.metadata()?;
    if !before.is_file() || before.uid() != unsafe { libc::geteuid() } || (!executable && before.nlink() != 1) || before.len() > limit as u64 {
        return Err(io::Error::other("unsafe review source"));
    }
    let mut bytes = Vec::new();
    file.by_ref().take(limit as u64 + 1).read_to_end(&mut bytes)?;
    let after = file.metadata()?;
    // Creating/removing another private carrier changes link-count ctime on
    // the immutable running image. Transcript identity still includes ctime.
    let stable=|metadata:&fs::Metadata|{let mut fields=identity(metadata);if executable{fields.3=0;fields.4=0;}fields};
    if bytes.len() > limit || stable(&before) != stable(&after) || stable(&after) != stable(&fs::symlink_metadata(path)?) {
        return Err(io::Error::other("review source changed"));
    }
    let sha256 = format!("{:x}",Sha256::digest(&bytes));
    Ok((bytes, SourceProof { sha256, device:before.dev(), inode:before.ino(), size:before.len(),
        ctime:before.ctime(), ctime_nsec:before.ctime_nsec(), mtime:before.mtime(), mtime_nsec:before.mtime_nsec() }))
}

pub(crate) fn review_job(manifest_path: &Path, event: &Value) -> io::Result<Option<ReviewJob>> {
    let (descriptor, _) = safe_read(manifest_path, MAX_INPUT)?;
    let manifest: Value = serde_json::from_slice(&descriptor)?;
    review_job_for_manifest(&manifest, event)
}

fn complete_provider_source(bytes: &[u8], proof: &Value) -> bool {
    !bytes.is_empty() && bytes.len() <= MAX_ROLLOUT
        && proof["size"].as_u64() == Some(bytes.len() as u64)
        && proof["sha256"].as_str() == Some(format!("{:x}", Sha256::digest(bytes)).as_str())
}

pub(crate) fn review_job_for_manifest(manifest: &Value, event: &Value) -> io::Result<Option<ReviewJob>> {
    if !crate::codex_compact::descriptor_contract(manifest) || event["hook_event_name"] != "PreCompact"
        || !matches!(event["trigger"].as_str(),Some("auto" | "manual"))
        || manifest["provider_thread"].as_str().is_none_or(str::is_empty) || event["session_id"] != manifest["provider_thread"]
        || manifest["lore_enabled"] != true || crate::review_worker::review_disabled() { return Ok(None); }
    let isolation = doxa_isolation::codex_review_scope()?;
    review_job_for_scope(manifest, event, isolation.as_ref())
}

fn review_job_for_scope(manifest: &Value, event: &Value, isolation: Option<&doxa_isolation::Manifest>) -> io::Result<Option<ReviewJob>> {
    let source = Path::new(event["transcript_path"].as_str().ok_or_else(|| io::Error::other("missing transcript"))?);
    let root = std::path::PathBuf::from(manifest["codex_home"].as_str().ok_or_else(|| io::Error::other("missing Codex home"))?);
    let root = fs::canonicalize(root)?;
    let cwd = manifest["cwd"].as_str().filter(|cwd| Path::new(cwd).is_absolute()).ok_or_else(|| io::Error::other("invalid cwd"))?;
    let thread = manifest["provider_thread"].as_str().filter(|id| !id.is_empty()).ok_or_else(|| io::Error::other("missing provider thread"))?;
    let session = manifest["doxa_session"].as_str().filter(|id| !id.is_empty() && id.len() <= 128 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b)))
        .ok_or_else(|| io::Error::other("invalid session"))?;
    // Ask for the attested actual size first. Older installed ROOT brokers
    // retain their 32 MiB ceiling; small sessions must not fail merely because
    // this client can now review larger histories with an upgraded broker.
    let provider = match doxa_isolation::snapshot_provider_file(source, 0, MAX_INPUT, Some(thread))? {
        Some(prefix) => {
            let size = prefix.proof["size"].as_u64().and_then(|size| usize::try_from(size).ok())
                .filter(|size| *size > 0 && *size <= MAX_ROLLOUT)
                .ok_or_else(|| io::Error::other("provider review source exceeds the complete history bound"))?;
            if prefix.bytes.len() == size { Some(prefix) } else {
                let full = doxa_isolation::snapshot_provider_file(source, 0, size, Some(thread))?
                    .ok_or_else(|| io::Error::other("provider source transport changed"))?;
                if prefix.proof["source"] != full.proof["source"] {
                    return Err(io::Error::other("provider source changed between size and complete snapshot"));
                }
                Some(full)
            }
        },
        None => None,
    };
    if provider.as_ref().is_some_and(|snapshot| !complete_provider_source(&snapshot.bytes, &snapshot.proof)) {
        return Err(io::Error::other("provider review source is incomplete or differs from its original proof"));
    }
    let provider_file = provider.as_ref().map(doxa_isolation::provider_review_file).transpose()?;
    let original = provider_file.as_ref().map(|file|file.path.as_path()).unwrap_or(source);
    if provider_file.is_none() && !original.starts_with(root.join("sessions")) && !original.starts_with(root.join("archived_sessions")) { return Ok(None); }
    let mut review_directory = None;
    let mut original_proof = None;
    let mut projected_proof = None;
    let mut source_cwd = None;
    let review_source = if let Some(scope) = isolation {
        if (!scope.profile.docker() && scope.context_cwd.is_none()) || scope.session_id != session
            || scope.context_cwd.as_deref().unwrap_or(&scope.checkout) != Path::new(cwd) || scope.private_home.join("codex") != root {
            return Err(io::Error::other("review scope differs from the trusted isolation manifest"));
        }
        use std::os::unix::fs::PermissionsExt;
        let parent = doxa_isolation::prepare_codex_review_home(scope)?.join("sessions");
        let meta = fs::symlink_metadata(&parent)?;
        if !meta.is_dir() || meta.file_type().is_symlink() || meta.uid() != unsafe {libc::geteuid()} || fs::canonicalize(&parent)? != parent {
            return Err(io::Error::other("unsafe isolated review directory"));
        }
        let directory = tempfile::Builder::new().permissions(fs::Permissions::from_mode(0o700)).prefix(".doxa-review-").tempdir_in(&parent)?;
        let path = directory.path().join("rollout.jsonl");
        let mut output = fs::OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(&path)?;
        // Migration preserves provider bytes, so a native-origin header can
        // retain a host path. Only manifest-authorized labels become aliases;
        // the provider can never select the review's destination project.
        let mut origins = vec!["/workspace"];
        if scope.context_cwd.is_some() {
            origins.extend([Some(cwd),scope.checkout.to_str(),scope.source.to_str()].into_iter().flatten());
        }
        let (header, proof, projected) = rollout::project(original, &mut output, thread, cwd, &origins)?;
        source_cwd = Some(header["payload"]["cwd"].clone());
        output.sync_all()?;
        let written = output.metadata()?;
        if written.len() != projected.size || written.nlink() != 1 {
            return Err(io::Error::other("review projection changed while written"));
        }
        projected_proof = Some(SourceProof {sha256:projected.sha256, size:projected.size, device:written.dev(), inode:written.ino(),
            ctime:written.ctime(), ctime_nsec:written.ctime_nsec(), mtime:written.mtime(), mtime_nsec:written.mtime_nsec()});
        original_proof = Some((original.to_owned(), proof));
        review_directory = Some(directory);
        path
    } else { original.to_owned() };
    let (first, before) = rollout::snapshot(&review_source)?;
    if projected_proof.as_ref().is_some_and(|expected| expected != &before) {
        return Err(io::Error::other("review projection differs from the complete original source mapping"));
    }
    if first["type"] != "session_meta" || first["payload"]["id"] != thread { return Ok(None); }
    let mut metadata = json!({"cwd":cwd,"session_id":session,"provider_thread":thread,"transcript":review_source,
        "older":true,"expected_source":before.json()});
    if let Some(snapshot) = &provider { metadata["origin_source"] = snapshot.proof.clone(); }
    else if let Some((_, proof)) = &original_proof { metadata["origin_source"] = proof.json(); }
    if let Some(cwd_source) = source_cwd { metadata["source_workspace_mapping"] = json!({"provider_cwd":cwd_source,"review_cwd":cwd}); }
    Ok(Some(ReviewJob {source:review_source, proof:before, original:original_proof, provider,
        _provider_file:provider_file, _review_directory:review_directory, metadata}))
}

/// An owned review copy and, for hardened isolation, its opaque ROOT source.
/// The worker's answer is accepted only while both identities remain current.
pub(crate) struct ReviewJob {
    source: std::path::PathBuf,
    proof: SourceProof,
    provider: Option<doxa_isolation::ProviderSnapshot>,
    original: Option<(std::path::PathBuf, SourceProof)>,
    _provider_file: Option<doxa_isolation::ProviderReviewFile>,
    _review_directory: Option<tempfile::TempDir>,
    pub(crate) metadata: Value,
}
impl ReviewJob {
    pub(crate) fn unchanged(&self) -> io::Result<bool> {
        if rollout::snapshot(&self.source)?.1 != self.proof { return Ok(false); }
        if let Some((path, proof)) = &self.original {
            if &rollout::snapshot(path)?.1 != proof { return Ok(false); }
        }
        match &self.provider {
            Some(snapshot)=>doxa_isolation::verify_provider_snapshot(snapshot),
            None=>Ok(true),
        }
    }
}

pub fn review(manifest_path: &Path, event: &Value, mut worker: impl FnMut(&Value) -> io::Result<bool>) -> io::Result<bool> {
    let Some(job) = review_job(manifest_path, event)? else { return Ok(false) };
    Ok(worker(&job.metadata)? && job.unchanged()?)
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
        if executable_digest(&executable)? != expected_digest { return Ok(false); }
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
    use std::io::Write;

    fn isolated_fixture(root: &Path) -> (Value, Value, doxa_isolation::Manifest, std::path::PathBuf) {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(root, fs::Permissions::from_mode(0o700)).unwrap();
        let home = root.join("home");
        let sessions = home.join("codex/sessions");
        fs::create_dir_all(&sessions).unwrap();
        fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
        let source = sessions.join("thread.jsonl");
        let checkout = root.join("checkout");
        fs::create_dir(&checkout).unwrap();
        let scope: doxa_isolation::Manifest = serde_json::from_value(json!({"version":1,"session_id":"doxa-session","profile":"docker-offline",
            "policy_hash":"","creation_policy_hash":"","source":checkout,"checkout":checkout,"checkout_device":0,"checkout_inode":0,
            "base_sha":"","branch":"","private_home":home,"cache":root.join("cache"),"broker":root.join("broker"),"nonce":"fixture","state":"ready"})).unwrap();
        let manifest = json!({"version":crate::codex_compact::SUPPORTED_VERSION,"provider_source":crate::codex_compact::SUPPORTED_SOURCE,"provider_patch":crate::codex_compact::SUPPORTED_PATCH,"provider_thread":"provider-thread","doxa_session":"doxa-session",
            "codex_home":home.join("codex"),"cwd":checkout,"lore_enabled":true});
        let event = json!({"hook_event_name":"PreCompact","trigger":"auto","session_id":"provider-thread","transcript_path":source});
        fs::write(&source, format!("{}\n{}\n",json!({"type":"session_meta","payload":{"id":"provider-thread","cwd":"/workspace","extra":{"cwd":"/workspace/unchanged"}}}),
            json!({"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Keep literal /workspace paths in conversation."}]}}))).unwrap();
        (manifest, event, scope, source)
    }

    #[test]
    fn isolated_review_projects_only_session_cwd_and_pins_both_sources() {
        let dir = tempfile::tempdir().unwrap();
        let (manifest, event, scope, source) = isolated_fixture(dir.path());
        let raw = fs::read(&source).unwrap();
        let job = review_job_for_scope(&manifest, &event, Some(&scope)).unwrap().unwrap();
        assert_eq!(fs::read(&source).unwrap(), raw);
        let review = fs::read(&job.source).unwrap();
        assert!(!job.source.starts_with(&scope.private_home), "review copy entered the worker home mount");
        assert_eq!(raw.splitn(2, |b| *b == b'\n').nth(1), review.splitn(2, |b| *b == b'\n').nth(1));
        let first: Value = serde_json::from_slice(review.split(|b| *b == b'\n').next().unwrap()).unwrap();
        assert_eq!(first["payload"]["cwd"], manifest["cwd"]);
        assert_eq!(first["payload"]["extra"]["cwd"], "/workspace/unchanged");
        assert_eq!(job.metadata["origin_source"]["sha256"], format!("{:x}", Sha256::digest(&raw)));
        assert_ne!(job.metadata["origin_source"]["sha256"], job.metadata["expected_source"]["sha256"]);
        assert!(job.unchanged().unwrap());
        let replacement = source.with_extension("replacement");
        fs::write(&replacement, &raw).unwrap(); fs::rename(&replacement, &source).unwrap();
        assert!(!job.unchanged().unwrap(), "same-byte original replacement lost its identity");
    }

    #[test]
    fn isolated_review_refuses_foreign_scope_header_and_projection_replacement() {
        let dir = tempfile::tempdir().unwrap();
        let (manifest, event, mut scope, source) = isolated_fixture(dir.path());
        scope.session_id = "foreign".into();
        assert!(review_job_for_scope(&manifest, &event, Some(&scope)).is_err());
        scope.session_id = "doxa-session".into();
        let raw = fs::read_to_string(&source).unwrap();
        fs::write(&source, raw.replacen("/workspace", "/foreign", 1)).unwrap();
        assert!(review_job_for_scope(&manifest, &event, Some(&scope)).is_err());
        fs::write(&source, raw.replacen("provider-thread", "foreign-thread", 1)).unwrap();
        assert!(review_job_for_scope(&manifest, &event, Some(&scope)).is_err());
        fs::write(&source, &raw).unwrap();
        let job = review_job_for_scope(&manifest, &event, Some(&scope)).unwrap().unwrap();
        let replacement = job.source.with_extension("replacement");
        fs::write(&replacement, fs::read(&job.source).unwrap()).unwrap(); fs::rename(replacement, &job.source).unwrap();
        assert!(!job.unchanged().unwrap(), "same-byte review replacement lost its identity");
    }

    #[test]
    fn migrated_isolation_retains_its_host_authorized_logical_project() {
        let dir = tempfile::tempdir().unwrap();
        let (mut manifest, event, mut scope, source) = isolated_fixture(dir.path());
        let logical = dir.path().join("original-project"); fs::create_dir(&logical).unwrap();
        scope.context_cwd = Some(logical.clone()); manifest["cwd"] = json!(logical);
        let job = review_job_for_scope(&manifest, &event, Some(&scope)).unwrap().unwrap();
        assert_eq!(job.metadata["cwd"], json!(logical));
        assert!(job.unchanged().unwrap());
        // A native-origin header survives import unchanged in either runtime.
        let raw = fs::read(&source).unwrap();
        let end = raw.iter().position(|b| *b == b'\n').unwrap();
        let mut header: Value = serde_json::from_slice(&raw[..end]).unwrap();
        header["payload"]["cwd"] = json!(logical);
        let mut native = serde_json::to_vec(&header).unwrap(); native.extend_from_slice(&raw[end..]);
        fs::write(&source,&native).unwrap();
        for profile in [doxa_isolation::Profile::DockerOffline,doxa_isolation::Profile::Native] {
            scope.profile = profile;
            let imported = review_job_for_scope(&manifest,&event,Some(&scope)).unwrap().unwrap();
            assert_eq!(imported.metadata["source_workspace_mapping"]["provider_cwd"],json!(logical));
            assert!(imported.unchanged().unwrap());
        }
        manifest["cwd"] = json!(scope.checkout);
        assert!(review_job_for_scope(&manifest, &event, Some(&scope)).is_err());
    }

    #[test]
    fn complete_long_history_and_attachment_sized_records_remain_reviewable() {
        let dir = tempfile::tempdir().unwrap();
        let (manifest, event, scope, source) = isolated_fixture(dir.path());
        let mut file = fs::OpenOptions::new().append(true).open(&source).unwrap();
        // Four normalized 1 MiB images fit in a roughly 5.4 MiB provider record.
        let large = json!({"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_image","image_url":format!("data:image/png;base64,{}", "A".repeat(6*1024*1024))}]}}).to_string();
        writeln!(file, "{large}").unwrap();
        let record = json!({"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"x".repeat(512*1024)}]}}).to_string();
        for _ in 0..64 { writeln!(file, "{record}").unwrap(); }
        file.sync_all().unwrap(); drop(file);
        assert!(fs::metadata(&source).unwrap().len() > 32*1024*1024);
        let job = review_job_for_scope(&manifest, &event, Some(&scope)).unwrap().unwrap();
        assert!(job.unchanged().unwrap());
        assert_eq!(job.metadata["origin_source"]["size"], fs::metadata(&source).unwrap().len());
        fs::OpenOptions::new().append(true).open(&source).unwrap().write_all(b"{}\n").unwrap();
        assert!(!job.unchanged().unwrap(), "the proof must include the whole long history");
    }

    #[test]
    fn streaming_review_retains_canonical_record_and_history_bounds() {
        let dir = tempfile::tempdir().unwrap();
        let (manifest, event, scope, source) = isolated_fixture(dir.path());
        fs::OpenOptions::new().append(true).open(&source).unwrap().write_all(&vec![b'x'; rollout::MAX_LINE+1]).unwrap();
        assert!(review_job_for_scope(&manifest, &event, Some(&scope)).is_err());
        fs::File::create(&source).unwrap().set_len(MAX_ROLLOUT as u64+1).unwrap();
        assert!(review_job_for_scope(&manifest, &event, Some(&scope)).is_err());
    }
    #[test]
    fn provider_review_requires_full_original_bytes_and_hash() {
        let bytes = b"complete original rollout\n";
        let proof = json!({"size":bytes.len(),"sha256":format!("{:x}",Sha256::digest(bytes))});
        assert!(complete_provider_source(bytes, &proof));
        assert!(!complete_provider_source(&bytes[..bytes.len()-1], &proof));
        let mut changed = bytes.to_vec(); changed[0] = b'X';
        assert!(!complete_provider_source(&changed, &proof));
        assert!(!complete_provider_source(bytes, &json!({"size":bytes.len()})));
        assert!(!complete_provider_source(bytes, &json!({"size":bytes.len()+1,"sha256":proof["sha256"]})));
        assert!(!complete_provider_source(&[], &json!({"size":0,"sha256":format!("{:x}",Sha256::digest([]))})));
    }

    #[test]
    fn identical_rollout_replacement_after_review_changes_source_identity() {
        let dir = tempfile::tempdir().unwrap();
        let sessions = dir.path().join("sessions"); fs::create_dir(&sessions).unwrap();
        let source = sessions.join("thread.jsonl");
        let bytes = br#"{"type":"session_meta","payload":{"id":"provider-thread"}}"#;
        fs::write(&source, bytes).unwrap();
        let manifest = json!({"version":crate::codex_compact::SUPPORTED_VERSION,"provider_source":crate::codex_compact::SUPPORTED_SOURCE,"provider_patch":crate::codex_compact::SUPPORTED_PATCH,"provider_thread":"provider-thread","doxa_session":"doxa-session",
            "codex_home":dir.path(),"cwd":dir.path(),"lore_enabled":true});
        let event = json!({"hook_event_name":"PreCompact","trigger":"manual","session_id":"provider-thread","transcript_path":source});
        let job = review_job_for_manifest(&manifest, &event).unwrap().unwrap();
        assert!(job.unchanged().unwrap());
        let replacement = sessions.join("replacement.jsonl"); fs::write(&replacement, bytes).unwrap();
        fs::rename(&replacement, &source).unwrap();
        assert_eq!(fs::read(&source).unwrap(), bytes);
        assert!(!job.unchanged().unwrap());
    }
    #[test]
    fn retained_contract_review_requires_its_actual_version_source_and_patch() {
        let dir=tempfile::tempdir().unwrap();let sessions=dir.path().join("sessions");fs::create_dir(&sessions).unwrap();
        let source=sessions.join("thread.jsonl");
        fs::write(&source,br#"{"type":"session_meta","payload":{"id":"provider-thread"}}"#).unwrap();
        let mut manifest=json!({"version":"0.163.0","provider_source":"83086188ae0614bd882818ff1aa23f3194962012",
            "provider_patch":"3a7b8c09b1f95bc523611202005aa71e6ba14d23f26a3833249487385e013e9c",
            "provider_thread":"provider-thread","doxa_session":"retained-session","codex_home":dir.path(),"cwd":dir.path(),"lore_enabled":true});
        let event=json!({"hook_event_name":"PreCompact","trigger":"manual","session_id":"provider-thread","transcript_path":source});
        let job=review_job_for_manifest(&manifest,&event).unwrap().unwrap();
        assert!(job.unchanged().unwrap());
        manifest["version"]=json!(crate::codex_compact::SUPPORTED_VERSION);
        assert!(review_job_for_manifest(&manifest,&event).unwrap().is_none(),"old source must not be relabeled current");
        manifest["version"]=json!("0.163.0");manifest["provider_patch"]=json!(crate::codex_compact::SUPPORTED_PATCH);
        assert!(review_job_for_manifest(&manifest,&event).unwrap().is_none(),"mixed reviewed tuples are not a reviewed payload");
        manifest["version"]=json!("0.162.0");
        assert!(review_job_for_manifest(&manifest,&event).unwrap().is_none());
    }
    #[test]
    fn executable_hardlinks_are_pinned_without_relaxing_transcript_sources(){
        use std::os::unix::fs::PermissionsExt;
        let dir=tempfile::tempdir().unwrap();let executable=dir.path().join("daemon");
        fs::write(&executable,b"native executable fixture").unwrap();fs::set_permissions(&executable,fs::Permissions::from_mode(0o700)).unwrap();
        fs::hard_link(&executable,dir.path().join("artifact")).unwrap();
        assert!(executable_digest(&executable).is_ok());assert!(safe_read(&executable,1024).is_err());
        let link=dir.path().join("symlink");std::os::unix::fs::symlink(&executable,&link).unwrap();assert!(executable_digest(&link).is_err());
    }
    #[test]
    fn changed_rollout_and_forged_owned_source_cannot_authorize_compaction() {
        let dir = tempfile::tempdir().unwrap(); let sessions = dir.path().join("sessions"); fs::create_dir(&sessions).unwrap();
        let transcript = sessions.join("thread.jsonl"); let raw = br#"{"type":"session_meta","payload":{"id":"provider-thread"}}"#;
        fs::write(&transcript,raw).unwrap();
        let manifest = dir.path().join("manifest.json");
        fs::write(&manifest,json!({"version":crate::codex_compact::SUPPORTED_VERSION,"provider_source":crate::codex_compact::SUPPORTED_SOURCE,"provider_patch":crate::codex_compact::SUPPORTED_PATCH,"provider_thread":"provider-thread","doxa_session":"doxa-session","codex_home":dir.path(),"cwd":dir.path(),"lore_enabled":true}).to_string()).unwrap();
        let event = json!({"hook_event_name":"PreCompact","trigger":"manual","session_id":"provider-thread","transcript_path":transcript});
        assert!(review(&manifest,&event,|metadata| { assert_eq!(metadata["expected_source"]["size"],raw.len()); Ok(true) }).unwrap());
        assert!(!review(&manifest,&event,|_| { fs::write(&transcript,format!("{}\n{{}}",String::from_utf8_lossy(raw))).unwrap(); Ok(true) }).unwrap());
        let mut forged = event; forged["session_id"] = json!("foreign");
        assert!(!review(&manifest,&forged,|_| panic!("must not review foreign session")).unwrap());
    }
    #[test]
    fn native_manual_review_failures_never_authorize_an_owned_snapshot() {
        let dir = tempfile::tempdir().unwrap();
        let sessions = dir.path().join("sessions"); fs::create_dir(&sessions).unwrap();
        let source = sessions.join("thread.jsonl");
        let bytes = br#"{"type":"session_meta","payload":{"id":"provider-thread"}}"#;
        fs::write(&source, bytes).unwrap();
        let manifest = dir.path().join("manifest.json");
        fs::write(&manifest, json!({"version":crate::codex_compact::SUPPORTED_VERSION,"provider_source":crate::codex_compact::SUPPORTED_SOURCE,"provider_patch":crate::codex_compact::SUPPORTED_PATCH,"provider_thread":"provider-thread","doxa_session":"doxa-session","codex_home":dir.path(),"cwd":dir.path(),"lore_enabled":true}).to_string()).unwrap();
        let event = json!({"hook_event_name":"PreCompact","trigger":"manual","session_id":"provider-thread","transcript_path":source});
        assert!(!review(&manifest, &event, |_| Ok(false)).unwrap());
        for failure in ["worker spawn failed", "worker timed out", "invalid approval output"] {
            assert!(review(&manifest, &event, |_| Err(io::Error::other(failure))).is_err());
            assert_eq!(fs::read(&source).unwrap(), bytes);
        }
    }

}
