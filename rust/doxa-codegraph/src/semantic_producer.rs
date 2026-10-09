//! Opt-in, data-only rust-analyzer producer contract.
//!
//! No function here starts Docker or promotes an LSP reply to a binding. The
//! disabled launcher seam in `semantic_runtime` observes selected effective
//! settings, but live network, quota, and analyzer proof is still required.

use super::worktree_root;
use serde_json::{json, Value};
use std::ffi::OsString;
use std::io::BufRead;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

const MAX_LSP_BODY: usize = 32 * 1024;
const MAX_HEADER: usize = 1024;
const MEMORY_BYTES: u64 = 1024 * 1024 * 1024;
const TMPFS_BYTES: u64 = 64 * 1024 * 1024;

/// A reviewable Docker invocation and pinned LSP initialization request.
/// `docker_command` is built with an empty inherited environment; there is no
/// public `spawn` method while production attestation remains incomplete.
#[derive(Debug)]
pub struct ProducerPlan {
    pub root: PathBuf,
    pub image: String,
    pub docker_host: String,
    pub args: Vec<OsString>,
    pub initialize: Value,
    pub attestation: &'static str,
}

fn pinned_image(image: &str) -> bool {
    let (name, digest) = match image.split_once("@sha256:") {
        Some(parts) => parts,
        None => return false,
    };
    !name.is_empty() && !name.starts_with('-')
        && name.bytes().all(|byte| byte.is_ascii_alphanumeric() || b"./:_-".contains(&byte))
        && digest.len() == 64 && digest.bytes().all(|byte| byte.is_ascii_hexdigit())
}

/// Build a restrictive plan only after the caller explicitly enables this
/// experimental producer. This is not a Docker preflight or launch authority.
pub fn plan_rust_analyzer(
    root: &Path,
    image: &str,
    docker_host: &str,
    explicitly_enabled: bool,
) -> Result<ProducerPlan, String> {
    if !explicitly_enabled { return Err("semantic producer requires explicit opt-in".into()); }
    if !pinned_image(image) { return Err("rust-analyzer image must be pinned by SHA-256 digest".into()); }
    let socket = docker_host.strip_prefix("unix://").ok_or("only a local Unix Docker socket is allowed")?;
    if !Path::new(socket).is_absolute() || matches!(socket, "/var/run/docker.sock" | "/run/docker.sock") {
        return Err("rootful or relative Docker socket is forbidden".into());
    }
    let root = worktree_root(root)?;
    let source = root.to_str().ok_or("non-UTF-8 worktree path")?;
    if source.contains([',', '\n', '\r', '\0', '%', '?', '#']) {
        return Err("worktree path cannot be represented safely in Docker mount and LSP URI".into());
    }
    let mut args: Vec<OsString> = [
        "run", "--rm", "--pull=never", "--init", "--read-only",
        "--network=none", "--ipc=private", "--cgroupns=private",
        "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
        "--user=0:0", "--cpus=1", "--pids-limit=64", "--ulimit=nofile=64:64",
        "--env=HOME=/tmp", "--env=TMPDIR=/tmp", "--env=CARGO_HOME=/tmp/cargo",
        "--env=RUSTUP_HOME=/tmp/rustup", "--env=CARGO_NET_OFFLINE=true",
        "--workdir", source, "--entrypoint=/usr/local/bin/rust-analyzer",
    ].into_iter().map(Into::into).collect();
    args.extend([
        format!("--memory={MEMORY_BYTES}").into(),
        format!("--memory-swap={MEMORY_BYTES}").into(),
        format!("--tmpfs=/tmp:rw,noexec,nosuid,nodev,size={TMPFS_BYTES},mode=1777").into(),
    ]);
    args.extend(["--mount".into(), format!("type=bind,src={source},dst={source},readonly").into()]);
    args.push(image.into());
    let uri = format!("file://{source}");
    let initialize = json!({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "processId": null, "rootUri": uri,
            "workspaceFolders": [{"uri": uri, "name": "doxa-semantic"}],
            "capabilities": {"experimental": {"serverStatusNotification": true}},
            "initializationOptions": {
                "cargo": {"buildScripts": {"enable": false}, "autoreload": false, "noDeps": true},
                "procMacro": {"enable": false}, "checkOnSave": false,
                "files": {"watcher": "client"}
            }
        }
    });
    Ok(ProducerPlan { root, image: image.into(), docker_host: docker_host.into(), args, initialize,
        attestation: "unavailable_runtime_not_started" })
}

impl ProducerPlan {
    /// Produce a command for a separate, attested launcher. Never call
    /// `spawn` from this module; profile construction alone grants nothing.
    pub fn docker_command(&self) -> Command {
        let mut command = Command::new("docker");
        command.env_clear().env("DOCKER_HOST", &self.docker_host)
            .args(&self.args).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        command
    }

    /// Internal launcher seam. The caller owns a fresh, private CID path and
    /// must attest that the observed container ID and policy match this run.
    /// No CLI path calls this method while production attestation is disabled.
    pub(crate) fn observed_docker_command(&self, docker_binary: &Path, cidfile: &Path) -> Result<Command, String> {
        if !docker_binary.is_absolute() || !cidfile.is_absolute() || cidfile.symlink_metadata().is_ok() {
            return Err("Docker binary and fresh CID file must be absolute paths".into());
        }
        // ProducerPlan's fields are public for inspection, so rederive the
        // complete profile before granting a launch. A caller must not be
        // able to add --privileged or change LSP configuration after planning.
        let expected = plan_rust_analyzer(&self.root, &self.image, &self.docker_host, true)?;
        if self.args != expected.args || self.initialize != expected.initialize
            || self.attestation != expected.attestation || self.root != expected.root {
            return Err("Docker producer plan changed after validation".into());
        }
        let parent = cidfile.parent().ok_or("missing private CID directory")?;
        let metadata = parent.symlink_metadata().map_err(|_| "missing private CID directory")?;
        let canonical = parent.canonicalize().map_err(|_| "invalid private CID directory")?;
        if !metadata.file_type().is_dir() || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.mode() & 0o077 != 0 || canonical.starts_with(&self.root) {
            return Err("CID directory must be private and outside the worktree".into());
        }
        let mut command = Command::new(docker_binary);
        command.env_clear().env("DOCKER_HOST", &self.docker_host)
            .args(&self.args[..self.args.len() - 1]).arg("--cidfile").arg(cidfile)
            .arg(&self.image).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        Ok(command)
    }
}

/// Encode one bounded JSON-RPC message in the LSP Content-Length framing.
pub fn encode_lsp_frame(message: &Value) -> Result<Vec<u8>, String> {
    let body = serde_json::to_vec(message).map_err(|e| e.to_string())?;
    if body.is_empty() || body.len() > MAX_LSP_BODY { return Err("LSP body exceeds 32 KiB".into()); }
    let mut frame = format!("Content-Length: {}\r\n\r\n", body.len()).into_bytes();
    frame.extend(body);
    Ok(frame)
}

/// Parse one bounded server frame. Unknown/duplicate framing is rejected,
/// preventing a transcript from being spliced into a later request.
pub fn read_lsp_frame(reader: &mut impl BufRead) -> Result<Value, String> {
    let mut total_header = 0usize;
    let mut content_length = None;
    let mut content_type_seen = false;
    loop {
        let mut line = Vec::new();
        loop {
            let mut byte = [0];
            reader.read_exact(&mut byte).map_err(|e| format!("LSP header read: {e}"))?;
            total_header += 1;
            if total_header > MAX_HEADER { return Err("invalid or oversized LSP header".into()); }
            line.push(byte[0]);
            if byte[0] == b'\n' { break; }
        }
        if !line.ends_with(b"\r\n") {
            return Err("invalid or oversized LSP header".into());
        }
        if line == b"\r\n" { break; }
        let header = std::str::from_utf8(&line[..line.len() - 2]).map_err(|_| "non-UTF-8 LSP header")?;
        if let Some(content_type) = header.strip_prefix("Content-Type: ") {
            if content_type_seen || !matches!(content_type,
                "application/vscode-jsonrpc; charset=utf-8" | "application/json") {
                return Err("unsupported or duplicate LSP Content-Type".into());
            }
            content_type_seen = true;
            continue;
        }
        let length = header.strip_prefix("Content-Length: ").ok_or("unsupported LSP header")?;
        if content_length.is_some() || length.starts_with('0') || !length.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err("duplicate or invalid LSP Content-Length".into());
        }
        let length: usize = length.parse().map_err(|_| "LSP length overflow")?;
        if length == 0 || length > MAX_LSP_BODY { return Err("LSP body exceeds 32 KiB".into()); }
        content_length = Some(length);
    }
    let mut body = vec![0; content_length.ok_or("missing LSP Content-Length")?];
    reader.read_exact(&mut body).map_err(|e| format!("truncated LSP body: {e}"))?;
    let value: Value = serde_json::from_slice(&body).map_err(|e| format!("invalid LSP JSON: {e}"))?;
    if !value.is_object() { return Err("LSP JSON-RPC message must be an object".into()); }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use std::process::Command;
    use tempfile::tempdir;

    const IMAGE: &str = "reviewed/rust-analyzer@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const SOCKET: &str = "unix:///run/user/1000/docker.sock";

    fn root() -> tempfile::TempDir {
        let root = tempdir().unwrap();
        assert!(Command::new("git").arg("init").arg("-q").arg(root.path()).status().unwrap().success());
        root
    }

    #[test]
    fn plan_has_no_execution_and_requires_explicit_pinned_offline_profile() {
        let root = root();
        assert!(plan_rust_analyzer(root.path(), IMAGE, SOCKET, false).is_err());
        assert!(plan_rust_analyzer(root.path(), "reviewed/rust-analyzer:latest", SOCKET, true).is_err());
        assert!(plan_rust_analyzer(root.path(), IMAGE, "unix:///var/run/docker.sock", true).is_err());
        let plan = plan_rust_analyzer(root.path(), IMAGE, SOCKET, true).unwrap();
        let args = plan.args.iter().map(|part| part.to_string_lossy().into_owned()).collect::<Vec<_>>();
        for required in ["--pull=never", "--read-only", "--network=none", "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true", "--memory=1073741824",
            "--memory-swap=1073741824", "--pids-limit=64", "--ulimit=nofile=64:64"] {
            assert!(args.contains(&required.into()), "missing {required}");
        }
        assert!(args.iter().any(|part| part.ends_with(",readonly") && part.starts_with("type=bind,")));
        assert_eq!(plan.initialize["params"]["initializationOptions"]["cargo"]["buildScripts"]["enable"], false);
        assert_eq!(plan.initialize["params"]["initializationOptions"]["procMacro"]["enable"], false);
        assert_eq!(plan.initialize["params"]["initializationOptions"]["checkOnSave"], false);
        assert_eq!(plan.attestation, "unavailable_runtime_not_started");
    }

    #[test]
    fn fake_server_frames_are_bounded_and_reject_conflicting_headers() {
        let reply = json!({"jsonrpc":"2.0","id":7,"result":{"uri":"file:///work/a.rs"}});
        let bytes = encode_lsp_frame(&reply).unwrap();
        assert_eq!(read_lsp_frame(&mut Cursor::new(bytes)).unwrap(), reply);
        let with_type = b"Content-Length: 2\r\nContent-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n{}";
        assert_eq!(read_lsp_frame(&mut Cursor::new(with_type)).unwrap(), json!({}));
        let duplicate = b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}";
        assert!(read_lsp_frame(&mut Cursor::new(duplicate)).is_err());
        let truncated = b"Content-Length: 5\r\n\r\n{}";
        assert!(read_lsp_frame(&mut Cursor::new(truncated)).is_err());
        let oversized = b"Content-Length: 32769\r\n\r\n";
        assert!(read_lsp_frame(&mut Cursor::new(oversized)).is_err());
    }
}
