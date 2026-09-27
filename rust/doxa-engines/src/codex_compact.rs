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
const SCRIPT: &str = include_str!("../codex_compact_hook.py");
const REVIEW_SUPERVISOR: &str = include_str!("../../../doxa/review_worker.py");
// This command itself is part of the trusted hash. It verifies the embedded
// source digest before compile/exec, including syntax/import failure handling.
const BOOTSTRAP: &str = "import sys,json,hashlib,os,stat,contextlib; result={'continue':False,'suppressOutput':True,'stopReason':'DOXA LORE review unavailable; compaction blocked'}\ntry:\n fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK); st=os.fstat(fd); assert stat.S_ISREG(st.st_mode) and st.st_uid==os.getuid() and st.st_nlink==1 and st.st_size<65536; data=os.read(fd,65536); os.close(fd); assert hashlib.sha256(data).hexdigest()==sys.argv[2]\n with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):\n  ns={'__name__':'doxa_compact_hook'}; exec(compile(data,sys.argv[1],'exec'),ns); sys.argv=[sys.argv[1],sys.argv[3]]; result=ns['main']()\nexcept BaseException: pass\nprint(json.dumps(result,separators=(',',':')))";

pub struct CompactGate {
    directory: PathBuf,
    manifest: PathBuf,
    descriptor: Value,
    command: String,
    hash: String,
    verified: bool,
    directory_identity: (u64, u64),
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
    /// `directory` is a fresh, private DOXA-owned session directory. Source is
    /// compiled into this binary, never read from a user's plugin directory.
    pub fn prepare(directory: &Path, python: &Path, codex_home: &Path, cwd: &Path, session_id: &str, version: &str) -> io::Result<Self> {
        Self::prepare_with_memory(directory, python, codex_home, cwd, session_id, version, true)
    }
    #[allow(clippy::too_many_arguments)]
    pub fn prepare_with_memory(directory: &Path, python: &Path, codex_home: &Path, cwd: &Path, session_id: &str, version: &str, lore_enabled: bool) -> io::Result<Self> {
        if version != SUPPORTED_VERSION { return Err(io::Error::other("Codex build has no verified PreCompact hook contract")); }
        if session_id.is_empty() || session_id.len() > 128 || !session_id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
            return Err(io::Error::other("invalid DOXA session id"));
        }
        private_directory(directory)?;
        let directory_meta = fs::symlink_metadata(directory)?;
        let directory_identity = (directory_meta.dev(), directory_meta.ino());
        let source = directory.join("precompact.py");
        let manifest = directory.join("compact-session.json");
        // Pin the canonical supervisor in the same source digest as the hook.
        // Neither process resolves DOXA code through the workspace/import path.
        let script = format!("REVIEW_SUPERVISOR_SOURCE = {}\n{}", string(REVIEW_SUPERVISOR), SCRIPT);
        let command = [python.display().to_string(), "-I".into(), "-c".into(), BOOTSTRAP.into(), source.display().to_string(), digest(script.as_bytes()), manifest.display().to_string()]
            .iter().map(|s| shell_quote(s)).collect::<Vec<_>>().join(" ");
        let normalized = json!({"event_name":"pre_compact","matcher":MATCHER,"hooks":[{
            "type":"command","command":command,"timeout":HOOK_TIMEOUT,"async":false
        }]});
        let hash = format!("sha256:{}", digest(&serde_json::to_vec(&normalized)?));
        let descriptor = json!({"version":SUPPORTED_VERSION,"provider_thread":null,"doxa_session":session_id,
            "codex_home":codex_home,"cwd":cwd,"lore_enabled":lore_enabled});
        let descriptor_bytes = serde_json::to_vec(&descriptor)?;
        write_new(&source, script.as_bytes())?;
        if let Err(error) = write_new(&manifest, &descriptor_bytes) {
            let _ = fs::remove_file(source); return Err(error);
        }
        Ok(Self { directory:directory.to_owned(), manifest, descriptor, command, hash, verified:false, directory_identity })
    }
    /// Append as process-local `-c` overrides. These trust this single pinned
    /// command; they never set bypass_hook_trust or edit CODEX_HOME/config.toml.
    pub fn cli_overrides(&self) -> Vec<String> {
        let group = format!("{{matcher={},hooks=[{{type=\"command\",command={},timeout={},async=false}}]}}",
            string(MATCHER), string(&self.command), HOOK_TIMEOUT);
        let state = format!("{}={{enabled=true,trusted_hash={}}}", string(HOOK_KEY), string(&self.hash));
        vec!["features.codex_hooks=true".into(), format!("hooks={{PreCompact=[{group}],state={{{state}}}}}")]
    }
    /// Call `hooks/list` after initialize and BEFORE starting/resuming a thread.
    /// Failure means this provider must not claim protected compaction.
    pub fn verify_hooks(&mut self, result: &Value) -> io::Result<()> {
        let matches = result["data"].as_array().into_iter().flatten().flat_map(|entry| entry["hooks"].as_array().into_iter().flatten())
            .filter(|hook| hook["key"] == HOOK_KEY && hook["command"] == self.command).collect::<Vec<_>>();
        if matches.len() != 1 { return Err(io::Error::other("DOXA PreCompact hook missing or duplicated")); }
        let hook = matches[0];
        if hook["handlerType"] != "command" || hook["enabled"] != true || hook["trustStatus"] != "trusted" || hook["currentHash"] != self.hash || hook["eventName"] != "preCompact" || hook["source"] != "sessionFlags" || hook["timeoutSec"] != HOOK_TIMEOUT || hook["async"] != false {
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
        self.descriptor = descriptor;
        Ok(())
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
            for name in ["precompact.py", "compact-session.json", "compact-session.next.json"] { let _ = fs::remove_file(self.directory.join(name)); }
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
        CompactGate::prepare(root, Path::new("/usr/bin/python3"), Path::new("/fixture/codex"), Path::new("/fixture/project"), "doxa-session", SUPPORTED_VERSION).unwrap()
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
    fn unknown_version_and_unsafe_preparation_never_enable_gate() {
        let dir = tempfile::tempdir().unwrap();
        assert!(CompactGate::prepare(dir.path(), Path::new("python"), Path::new("codex"), Path::new("project"), "session", "0.unknown").is_err());
        assert!(!dir.path().join("precompact.py").exists());
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o755)).unwrap();
        assert!(CompactGate::prepare(dir.path(), Path::new("python"), Path::new("codex"), Path::new("project"), "session", SUPPORTED_VERSION).is_err());
    }
    #[test]
    fn pinned_bootstrap_blocks_changed_source_and_cleanup_preserves_unknown_files() {
        let dir = tempfile::tempdir().unwrap(); let gate = prepare(dir.path());
        let source = dir.path().join("precompact.py"); fs::write(&source, "invalid syntax !!!").unwrap();
        let output = std::process::Command::new("/bin/sh").args(["-c", &gate.command]).output().unwrap();
        assert!(output.status.success()); let result: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(result["continue"], false);
        fs::write(dir.path().join("keep"), "unknown content").unwrap();
        drop(gate); assert!(!source.exists()); assert!(dir.path().join("keep").exists());
    }
    #[test]
    fn pinned_bootstrap_covers_canonical_review_supervisor_bytes() {
        let dir = tempfile::tempdir().unwrap(); let gate = prepare(dir.path());
        let source = dir.path().join("precompact.py");
        let bytes = fs::read_to_string(&source).unwrap();
        assert_eq!(bytes, format!("REVIEW_SUPERVISOR_SOURCE = {}\n{}", string(REVIEW_SUPERVISOR), SCRIPT));
        // A syntactically valid replacement cannot weaken the worker's owner
        // contract while retaining the Codex-approved hook command/hash.
        fs::write(&source, format!("REVIEW_SUPERVISOR_SOURCE = 'changed supervisor'\n{}", SCRIPT)).unwrap();
        let output = std::process::Command::new("/bin/sh").args(["-c", &gate.command]).output().unwrap();
        assert!(output.status.success());
        let result: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(result["continue"], false);
    }
}
