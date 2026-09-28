//! Reviewed, session-scoped PreCompact hook for the exact verified Codex build.
//! Codex's OS-level hook errors are fail-open; consumers must monitor hook
//! failure notifications and abort. Expected review errors return blocking JSON.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{fs, io, path::{Path, PathBuf}};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::io::Write;
#[cfg(test)]
use std::os::unix::fs::PermissionsExt;

pub const SUPPORTED_VERSION: &str = "0.156.1";
pub const HOOK_TIMEOUT: u64 = 240;
pub const HOOK_KEY: &str = "/<session-flags>/config.toml:pre_compact:0:0";
const MATCHER: &str = "^(auto|manual)$";

pub struct CompactGate {
    directory: PathBuf,
    manifest: PathBuf,
    descriptor: Value,
    command: String,
    hash: String,
    verified: bool,
    directory_identity: (u64, u64),
    carrier_identity: (u64,u64),
    carrier_digest: String,
    manifest_identity: (u64,u64),
}
fn shell_quote(value: &str) -> String { format!("'{}'", value.replace('\'', "'\\''")) }
fn string(value: &str) -> String { serde_json::to_string(value).expect("string JSON") }
fn digest(bytes: &[u8]) -> String { format!("{:x}", Sha256::digest(bytes)) }
fn private_directory(path: &Path) -> io::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_dir() || meta.file_type().is_symlink() || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 {
        return Err(io::Error::other("compact gate directory must be private and owned"));
    }
    Ok(())
}
fn write_new(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut file = fs::OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(path)?;
    file.write_all(bytes)?; file.sync_all()
}
impl CompactGate {
    /// `directory` is a fresh, private DOXA-owned session directory. The native
    /// daemon executable is pinned by digest, never from a plugin import path.
    pub fn prepare(directory: &Path, executable: &Path, codex_home: &Path, cwd: &Path, session_id: &str, version: &str) -> io::Result<Self> {
        Self::prepare_with_memory(directory, executable, codex_home, cwd, session_id, version, true)
    }
    #[allow(clippy::too_many_arguments)]
    pub fn prepare_with_memory(directory: &Path, executable: &Path, codex_home: &Path, cwd: &Path, session_id: &str, version: &str, lore_enabled: bool) -> io::Result<Self> {
        if version != SUPPORTED_VERSION { return Err(io::Error::other("Codex build has no verified PreCompact hook contract")); }
        if session_id.is_empty() || session_id.len() > 128 || !session_id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
            return Err(io::Error::other("invalid DOXA session id"));
        }
        private_directory(directory)?;
        let directory_meta = fs::symlink_metadata(directory)?;
        let directory_identity = (directory_meta.dev(), directory_meta.ino());
        let manifest = directory.join("compact-session.json");
        let source = fs::canonicalize(executable)?;
        let running=fs::metadata("/proc/self/exe")?;
        let executable_digest = crate::compact_hook::executable_digest(&source)?;
        let executable=directory.join("native-carrier");
        match fs::hard_link(&source,&executable){
            Ok(())=>{
                let pinned=fs::metadata(&executable)?;
                if (pinned.dev(),pinned.ino())!=(running.dev(),running.ino()){
                    let _=fs::remove_file(&executable);return Err(io::Error::other("native executable changed before pinning"));
                }
            },
            Err(error) if error.raw_os_error()==Some(libc::EXDEV)=>{
                let mut input=fs::File::open(&source)?;let pinned=input.metadata()?;
                if (pinned.dev(),pinned.ino())!=(running.dev(),running.ino()){return Err(io::Error::other("native executable changed before pinning"));}
                let mut output=fs::OpenOptions::new().write(true).create_new(true).mode(0o700).custom_flags(libc::O_NOFOLLOW).open(&executable)?;
                if let Err(error)=io::copy(&mut input,&mut output).and_then(|_|output.sync_all()) {let _=fs::remove_file(&executable);return Err(error);}
            },Err(error)=>return Err(error),
        }
        let carrier=fs::metadata(&executable)?;let carrier_identity=(carrier.dev(),carrier.ino());
        if crate::compact_hook::executable_digest(&executable)?!=executable_digest{
            let _=fs::remove_file(&executable);return Err(io::Error::other("native carrier digest changed"));
        }
        let command = [executable.display().to_string(), "__codex-precompact".into(), manifest.display().to_string(), executable_digest.clone()]
            .iter().map(|s| shell_quote(s)).collect::<Vec<_>>().join(" ");
        let normalized = json!({"event_name":"pre_compact","matcher":MATCHER,"hooks":[{
            "type":"command","command":command,"timeout":HOOK_TIMEOUT,"async":false
        }]});
        let hash = format!("sha256:{}", digest(&serde_json::to_vec(&normalized)?));
        let descriptor = json!({"version":SUPPORTED_VERSION,"provider_thread":null,"doxa_session":session_id,
            "codex_home":codex_home,"cwd":cwd,"lore_enabled":lore_enabled});
        let descriptor_bytes = serde_json::to_vec(&descriptor)?;
        if let Err(error)=write_new(&manifest, &descriptor_bytes){
            if fs::symlink_metadata(&executable).is_ok_and(|meta|meta.is_file()&&(meta.dev(),meta.ino())==carrier_identity){let _=fs::remove_file(&executable);}
            return Err(error);
        }
        let manifest_meta = fs::symlink_metadata(&manifest)?;
        Ok(Self { directory:directory.to_owned(), manifest, descriptor, command, hash, verified:false, directory_identity,carrier_identity,
            carrier_digest: executable_digest, manifest_identity: (manifest_meta.dev(),manifest_meta.ino()) })
    }
    /// Append as process-local `-c` overrides. These trust this single pinned
    /// command; they never set bypass_hook_trust or edit CODEX_HOME/config.toml.
    pub fn cli_overrides(&self) -> Vec<String> {
        let group = format!("{{matcher={},hooks=[{{type=\"command\",command={},timeout={},async=false}}]}}",
            string(MATCHER), string(&self.command), HOOK_TIMEOUT);
        let state = format!("{}={{enabled=true,trusted_hash={}}}", string(HOOK_KEY), string(&self.hash));
        vec!["features.codex_hooks=true".into(), "features.token_budget=false".into(), format!("hooks={{PreCompact=[{group}],state={{{state}}}}}")]
    }
    /// Call `hooks/list` after initialize and BEFORE starting/resuming a thread.
    /// Failure means this provider must not claim protected compaction.
    pub fn verify_hooks(&mut self, result: &Value) -> io::Result<()> {
        let matches = result["data"].as_array().into_iter().flatten().flat_map(|entry| entry["hooks"].as_array().into_iter().flatten())
            .filter(|hook| hook["key"] == HOOK_KEY || (hook["source"] == "sessionFlags" && hook["eventName"] == "preCompact")).collect::<Vec<_>>();
        if matches.len() != 1 { return Err(io::Error::other("DOXA PreCompact hook missing or duplicated")); }
        let hook = matches[0];
        if hook["command"] != self.command || hook["key"] != HOOK_KEY || hook["handlerType"] != "command" || hook["enabled"] != true || hook["trustStatus"] != "trusted" || hook["currentHash"] != self.hash || hook["eventName"] != "preCompact" || hook["source"] != "sessionFlags" || hook["timeoutSec"] != HOOK_TIMEOUT || hook["async"] != false {
            return Err(io::Error::other("DOXA PreCompact hook is not active with its pinned hash"));
        }
        self.verified = true; Ok(())
    }
    /// Bind only the provider's actual thread/start or thread/resume identity,
    /// before its first turn/compact request. Hooks reject all other identities.
    pub fn bind_thread(&mut self, thread: &str) -> io::Result<()> {
        if !self.verified || thread.is_empty() || thread.len() > 128 || !thread.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
            return Err(io::Error::other("cannot bind an unverified compact gate"));
        }
        private_directory(&self.directory)?;
        let mut descriptor = self.descriptor.clone();
        descriptor["provider_thread"] = json!(thread);
        let temporary = self.directory.join("compact-session.next.json");
        write_new(&temporary, &serde_json::to_vec(&descriptor)?)?;
        if let Err(error) = fs::rename(&temporary, &self.manifest) { let _ = fs::remove_file(temporary); return Err(error); }
        let manifest_meta = fs::symlink_metadata(&self.manifest)?;
        self.manifest_identity = (manifest_meta.dev(),manifest_meta.ino());
        self.descriptor = descriptor;
        Ok(())
    }
    pub(crate) fn manual_review_job(&self, transcript: &Path) -> io::Result<Option<crate::compact_hook::ReviewJob>> {
        if !self.verified() { return Ok(None); }
        self.validate_manifest()?;
        // The original in-memory descriptor owns thread/cwd authorization.
        // A disk manifest replacement never contributes new authority.
        crate::compact_hook::review_job_for_manifest(&self.descriptor, &json!({"hook_event_name":"PreCompact","trigger":"manual",
            "session_id":self.descriptor["provider_thread"],"transcript_path":transcript}))
    }
    fn validate_manifest(&self) -> io::Result<()> {
        private_directory(&self.directory)?;
        let directory = fs::symlink_metadata(&self.directory)?;
        if (directory.dev(),directory.ino()) != self.directory_identity { return Err(io::Error::other("compact gate directory replaced")); }
        let (bytes, proof) = crate::compact_hook::safe_read(&self.manifest, crate::compact_hook::MAX_INPUT)?;
        if (proof.device,proof.inode) != self.manifest_identity || bytes != serde_json::to_vec(&self.descriptor)? {
            return Err(io::Error::other("compact gate manifest replaced or changed"));
        }
        Ok(())
    }
    pub(crate) fn pinned_carrier(&self) -> io::Result<fs::File> {
        use std::io::Read;
        self.validate_manifest()?;
        let path = self.directory.join("native-carrier");
        let mut file = fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(&path)?;
        let before = file.metadata()?;
        if !before.is_file() || (before.dev(),before.ino()) != self.carrier_identity || before.len() > 256*1024*1024 {
            return Err(io::Error::other("compact native carrier replaced"));
        }
        let mut hasher = Sha256::new();
        let mut bytes = [0u8;65536];
        loop { let count=file.read(&mut bytes)?; if count==0 {break;} hasher.update(&bytes[..count]); }
        let after = file.metadata()?;
        if (before.dev(),before.ino(),before.len(),before.mtime(),before.mtime_nsec()) != (after.dev(),after.ino(),after.len(),after.mtime(),after.mtime_nsec())
            || format!("{:x}",hasher.finalize()) != self.carrier_digest {
            return Err(io::Error::other("compact native carrier changed"));
        }
        // Keep this exact open inode alive through exec, even if its path moves.
        Ok(file)
    }
    pub fn verified(&self) -> bool { self.verified && self.descriptor["provider_thread"].is_string() }
    pub fn hook_key(&self) -> &str { HOOK_KEY }
    /// Only the DOXA session-flags command contributes review authorization.
    pub fn observe_completion(&mut self, run: &Value) -> ReviewOutcome {
        if run["eventName"] != "preCompact" || run["source"] != "sessionFlags" || run["sourcePath"] != "/<session-flags>/config.toml" { return ReviewOutcome::Unrelated; }
        if run["handlerType"] != "command" || run["executionMode"] != "sync" { self.verified = false; return ReviewOutcome::Failed; }
        match run["status"].as_str() {
            Some("completed") => ReviewOutcome::Reviewed,
            Some("stopped" | "blocked") => ReviewOutcome::Blocked,
            _ => { self.verified = false; ReviewOutcome::Failed }
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum ReviewOutcome { Unrelated, Reviewed, Blocked, Failed }
impl Drop for CompactGate {
    fn drop(&mut self) {
        if fs::symlink_metadata(&self.directory).is_ok_and(|meta| meta.is_dir() && !meta.file_type().is_symlink() && (meta.dev(),meta.ino()) == self.directory_identity) {
            for name in ["compact-session.json", "compact-session.next.json"] { let _ = fs::remove_file(self.directory.join(name)); }
            let carrier=self.directory.join("native-carrier");
            if fs::symlink_metadata(&carrier).is_ok_and(|meta|meta.is_file()&&(meta.dev(),meta.ino())==self.carrier_identity){let _=fs::remove_file(carrier);}
            // Never recursively remove unknown content added to this directory.
            let _ = fs::remove_dir(&self.directory);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn prepare(root: &Path) -> CompactGate {
        fs::set_permissions(root, fs::Permissions::from_mode(0o700)).unwrap();
        CompactGate::prepare(root, &std::env::current_exe().unwrap(), Path::new("/fixture/codex"), Path::new("/fixture/project"), "doxa-session", SUPPORTED_VERSION).unwrap()
    }
    fn metadata(gate: &CompactGate) -> Value {
        json!({"data":[{"hooks":[{"key":HOOK_KEY,"command":gate.command,"async":false,"handlerType":"command","enabled":true,"trustStatus":"trusted","currentHash":gate.hash,"eventName":"preCompact","source":"sessionFlags","sourcePath":"/<session-flags>/config.toml","timeoutSec":HOOK_TIMEOUT}]}]})
    }
    #[test]
    fn gate_requires_exact_active_trust_before_thread_binding() {
        let dir = tempfile::tempdir().unwrap(); let mut gate = prepare(dir.path());
        assert!(gate.bind_thread("provider-thread").is_err());
        let mut result = metadata(&gate); result["data"][0]["hooks"][0]["trustStatus"] = json!("modified");
        assert!(gate.verify_hooks(&result).is_err());
        gate.verify_hooks(&metadata(&gate)).unwrap(); gate.bind_thread("provider-thread").unwrap();
        assert!(gate.verified());
        let descriptor: Value = serde_json::from_slice(&fs::read(&gate.manifest).unwrap()).unwrap();
        assert_eq!(descriptor["provider_thread"], "provider-thread");
        let run = json!({"eventName":"preCompact","source":"sessionFlags","sourcePath":"/<session-flags>/config.toml","handlerType":"command","executionMode":"sync","status":"failed"});
        assert_eq!(gate.observe_completion(&run), ReviewOutcome::Failed); assert!(!gate.verified());
    }
    #[test]
    fn cleanup_does_not_remove_a_replaced_native_carrier(){
        let dir=tempfile::tempdir().unwrap();let gate=prepare(dir.path());let carrier=dir.path().join("native-carrier");
        fs::rename(&carrier,dir.path().join("old-carrier")).unwrap();fs::write(&carrier,"replacement").unwrap();
        drop(gate);assert_eq!(fs::read_to_string(carrier).unwrap(),"replacement");
    }
    #[test]
    fn unknown_version_and_unsafe_preparation_never_enable_gate() {
        let dir = tempfile::tempdir().unwrap();
        assert!(CompactGate::prepare(dir.path(), Path::new("python"), Path::new("codex"), Path::new("project"), "session", "0.unknown").is_err());
        assert!(!dir.path().join("precompact.py").exists());
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o755)).unwrap();
        assert!(CompactGate::prepare(dir.path(), Path::new("python"), Path::new("codex"), Path::new("project"), "session", SUPPORTED_VERSION).is_err());
    }
    #[test]
    fn native_command_is_pinned_and_cleanup_preserves_unknown_files() {
        let dir = tempfile::tempdir().unwrap(); let gate = prepare(dir.path());
        assert!(gate.command.contains("__codex-precompact"));
        assert!(gate.command.contains("native-carrier"));
        assert_eq!(fs::metadata(dir.path().join("native-carrier")).unwrap().ino(),fs::metadata("/proc/self/exe").unwrap().ino());
        assert!(!gate.command.contains("python"));
        assert!(!dir.path().join("precompact.py").exists());
        fs::write(dir.path().join("keep"), "unknown content").unwrap();
        drop(gate); assert!(!dir.path().join("compact-session.json").exists());
        assert!(dir.path().join("keep").exists());
    }
    #[test]
    fn manual_review_refuses_replaced_manifest_and_carrier() {
        for replacement in ["manifest", "carrier"] {
            let dir=tempfile::tempdir().unwrap(); let mut gate=prepare(dir.path());
            gate.verify_hooks(&metadata(&gate)).unwrap(); gate.bind_thread("provider-thread").unwrap();
            let codex_home=Path::new("/fixture/codex");
            if replacement=="manifest" {
                fs::rename(&gate.manifest,dir.path().join("old-manifest")).unwrap();
                fs::write(&gate.manifest,serde_json::to_vec(&gate.descriptor).unwrap()).unwrap();
                assert!(gate.manual_review_job(&codex_home.join("sessions/thread.jsonl")).is_err());
            } else {
                let carrier=dir.path().join("native-carrier");
                fs::rename(&carrier,dir.path().join("old-carrier")).unwrap();
                fs::write(&carrier,b"replacement executable").unwrap();
                assert!(gate.pinned_carrier().is_err());
            }
        }
    }

}
