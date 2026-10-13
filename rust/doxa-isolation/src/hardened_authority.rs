//! Administrator-controlled, live Linux authority for hardened workers.
//!
//! Serialized inputs describe administrator expectations. They contain no
//! admission flags. Only retained kernel capabilities produce an `Authority`.
use crate::error;
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, io, path::{Component, Path, PathBuf}};

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub version: u32,
    pub session_id: String,
    pub caller_uid: u32,
    pub engine: EnginePolicy,
    pub root: PathBuf,
    pub image: String,
    pub worker: Artifact,
    pub providers: BTreeMap<String, ProviderPolicy>,
    pub authority: Artifact,
    pub broker: Artifact,
    pub limits: Limits,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Artifact { pub path: PathBuf, pub sha256: String }

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EnginePolicy {
    pub uid: u32,
    pub gid: u32,
    pub socket: PathBuf,
    pub exe_path: PathBuf,
    pub exe_sha256: String,
    pub config_path: PathBuf,
    pub config_sha256: String,
    pub api_version: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderPolicy {
    pub path: PathBuf,
    pub sha256: String,
    pub artifacts: BTreeMap<PathBuf, String>,
    pub environment_keys: Vec<String>,
    pub egress: Vec<String>,
    pub proxy_port: u16,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Limits {
    pub memory_bytes: u64,
    pub cpu_quota: u64,
    pub cpu_period: u64,
    pub pids: u32,
    pub disk_bytes: u64,
    pub output_bytes: u64,
    pub stdin_bytes: u64,
    pub deadline_seconds: u64,
    pub operation_seconds: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RootManifest {
    pub version: u32,
    pub session_id: String,
    pub policy_sha256: String,
    pub cid: String,
    pub host_init_pid: u32,
    pub engine_pid: u32,
    pub engine_socket_dev: u64,
    pub engine_socket_ino: u64,
    pub boot_id: String,
    pub gateway_pid: Option<u32>,
    pub status: String,
}

/// Facts obtained from current kernel capabilities. A live guarded gateway
/// must still independently authenticate its per-message sender and status.
#[derive(Debug, Clone, Serialize)]
pub struct AuthorityReport {
    pub policy_sha256: String,
    pub cid: String,
    pub engine_pid: u32,
    pub host_init_pid: u32,
    pub boot_id: String,
    pub quota_ready: bool,
    pub filter_ready: bool,
}

fn digest(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn absolute(path: &Path) -> bool {
    path.is_absolute() && path.components().skip(1).all(|c| matches!(c, Component::Normal(_)))
        && path.components().count() > 1
}

impl Policy {
    pub fn validate(&self) -> io::Result<()> {
        crate::quota_install::enforcement::validate_session_id(&self.session_id)?;
        let l = &self.limits;
        if self.version != 1 || self.caller_uid == 0 || self.engine.uid == 0
            || self.engine.uid == self.caller_uid || self.engine.gid == 0
            || !matches!(self.engine.api_version.as_str(),"1.47"|"1.48"|"1.49"|"1.50"|"1.51"|"1.52")
            || !self.image.strip_prefix("sha256:").is_some_and(digest)
            || self.worker.path != Path::new("/usr/local/bin/doxa-isolation-worker")
            || !absolute(&self.root)
            || self.root.file_name().and_then(|n| n.to_str()) != Some(&self.session_id)
            || l.memory_bytes == 0 || l.memory_bytes > 1024 * 1024 * 1024 * 1024
            || l.cpu_quota == 0 || l.cpu_quota > 100_000_000
            || !(1_000..=1_000_000).contains(&l.cpu_period)
            || l.pids == 0 || l.pids > 4096 || l.disk_bytes == 0
            || l.disk_bytes % 512 != 0 || l.disk_bytes > 1024 * 1024 * 1024 * 1024
            || l.output_bytes == 0 || l.output_bytes > 1024 * 1024 * 1024
            || l.stdin_bytes == 0 || l.stdin_bytes > 1024 * 1024 * 1024
            || l.deadline_seconds == 0 || l.deadline_seconds > 86400
            || l.operation_seconds == 0 || l.operation_seconds > 120
            || l.operation_seconds > l.deadline_seconds {
            return Err(error("invalid administrator hardened policy scope or finite limits"));
        }
        for artifact in [&self.worker, &self.authority, &self.broker] {
            if !absolute(&artifact.path) || !digest(&artifact.sha256) {
                return Err(error("administrator artifact is not an absolute content pin"));
            }
        }
        for path in [&self.engine.socket, &self.engine.exe_path, &self.engine.config_path] {
            if !absolute(path) { return Err(error("administrator Engine path is not canonical")); }
        }
        if !digest(&self.engine.exe_sha256) || !digest(&self.engine.config_sha256)
            || self.providers.is_empty() || self.providers.len() > 2 {
            return Err(error("administrator Engine/provider content pins are missing"));
        }
        for (family, provider) in &self.providers {
            let port = match family.as_str() { "claude" => 3128, "codex" => 3129,
                _ => return Err(error("unsupported hardened provider family")) };
            if provider.path != PathBuf::from(format!("/usr/local/bin/{family}"))
                || !digest(&provider.sha256) || provider.proxy_port != port
                || provider.artifacts.len() > 32 || provider.environment_keys.len() > 16
                || provider.egress.is_empty() || provider.egress.len() > 32 {
                return Err(error("invalid fixed hardened provider contract"));
            }
            for (path, hash) in &provider.artifacts {
                if !absolute(path) || !digest(hash) || mutable_target(path) {
                    return Err(error("provider artifacts must be immutable image paths"));
                }
            }
            let mut hosts = std::collections::BTreeSet::new();
            for host in &provider.egress {
                if host.len() > 253 || !host.contains('.') || host.starts_with('.')
                    || host.ends_with('.') || host.split('.').any(|label| label.is_empty()
                        || label.len() > 63 || label.starts_with('-') || label.ends_with('-')
                        || !label.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-'))
                    || host.parse::<std::net::IpAddr>().is_ok() || !hosts.insert(host) {
                    return Err(error("provider egress must name distinct exact DNS hosts"));
                }
            }
            let mut keys = std::collections::BTreeSet::new();
            for key in &provider.environment_keys {
                if !matches!(key.as_str(), "ANTHROPIC_API_KEY" | "OPENAI_API_KEY" | "TERM" | "LANG" | "LC_ALL")
                    ||family=="claude"&&key=="OPENAI_API_KEY"||family=="codex"&&key=="ANTHROPIC_API_KEY"
                    || !keys.insert(key) {
                    return Err(error("hardened environment key is not an explicit credential name"));
                }
            }
        }
        Ok(())
    }
}

fn mutable_target(path: &Path) -> bool {
    ["/workspace", "/home", "/work-cache", "/run", "/tmp", "/proc", "/sys", "/dev"]
        .iter().any(|root| path.starts_with(root))
}

impl RootManifest {
    pub fn validate(&self, policy: &Policy, policy_sha256: &str) -> io::Result<()> {
        if self.version != 1 || self.session_id != policy.session_id
            || self.policy_sha256 != policy_sha256 || !digest(&self.policy_sha256)
            || !digest(&self.cid) || self.host_init_pid == 0 || self.host_init_pid > i32::MAX as u32
            || self.engine_pid == 0 || self.engine_pid > i32::MAX as u32
            || self.engine_socket_dev == 0 || self.engine_socket_ino == 0
            || !matches!(self.status.as_str(),"preparing"|"ready")
            || self.gateway_pid.is_some_and(|p| p == 0 || p > i32::MAX as u32)
            || self.boot_id.len() != 36 || !self.boot_id.bytes().enumerate().all(|(i,b)|
                if [8,13,18,23].contains(&i) { b == b'-' } else { b.is_ascii_digit() || (b'a'..=b'f').contains(&b) }) {
            return Err(error("root manifest differs from immutable session policy"));
        }
        Ok(())
    }
}

#[cfg(target_os = "linux")]
#[path = "hardened_authority_linux.rs"]
mod linux;
#[cfg(target_os = "linux")]
pub use linux::{Authority,load_root_policy,stage_check};

#[cfg(test)]
mod tests {
    use super::*;
    pub(super) fn policy() -> Policy {
        Policy { version:1, session_id:"authority-test".into(), caller_uid:1000,
            engine: EnginePolicy { uid:1001,gid:1001,socket:"/run/user/1001/docker.sock".into(),
                exe_path:"/usr/bin/dockerd".into(),exe_sha256:"a".repeat(64),
                config_path:"/etc/doxa/engine.json".into(),config_sha256:"b".repeat(64),api_version:"1.51".into() },
            root:"/srv/doxa/authority-test".into(), image:format!("sha256:{}","c".repeat(64)),
            worker: Artifact {path:"/usr/local/bin/doxa-isolation-worker".into(),sha256:"d".repeat(64)},
            authority:Artifact{path:"/usr/local/libexec/doxa-hardened-gateway".into(),sha256:"e".repeat(64)},
            broker:Artifact{path:"/usr/local/libexec/doxa-root-broker.py".into(),sha256:"f".repeat(64)},
            providers:BTreeMap::from([("codex".into(),ProviderPolicy{path:"/usr/local/bin/codex".into(),
                sha256:"0".repeat(64),artifacts:BTreeMap::new(),environment_keys:vec!["OPENAI_API_KEY".into()],
                egress:vec!["api.openai.com".into()],proxy_port:3129})]),
            limits:Limits{memory_bytes:536870912,cpu_quota:100000,cpu_period:100000,pids:128,
                disk_bytes:33554432,output_bytes:1048576,stdin_bytes:1048576,deadline_seconds:600,operation_seconds:2} }
    }
    #[test]
    fn policy_rejects_authority_flags_and_ambiguous_scope() {
        let p=policy(); p.validate().unwrap();
        let mut value=serde_json::to_value(&p).unwrap(); value["ready"]=true.into();
        assert!(serde_json::from_value::<Policy>(value).is_err());
        let mut bad=p.clone(); bad.engine.uid=bad.caller_uid; assert!(bad.validate().is_err());
        let mut bad=p.clone(); bad.image="worker:latest".into(); assert!(bad.validate().is_err());
        let mut bad=p.clone(); bad.root="/srv/doxa/../authority-test".into(); assert!(bad.validate().is_err());
        let mut bad=p.clone(); bad.limits.cpu_quota=0; assert!(bad.validate().is_err());
        let mut bad=p.clone(); bad.providers.get_mut("codex").unwrap().egress=vec!["*.openai.com".into()];
        assert!(bad.validate().is_err());
        let mut bad=p; bad.providers.get_mut("codex").unwrap().environment_keys=vec!["LD_PRELOAD".into()];
        assert!(bad.validate().is_err());
    }
}
