//! Local, read-only prerequisites. Engine/rootless/cgroup/image admission stays
//! in doxa-isolation; painting never runs a Docker command or connects a socket.
use std::path::Path;
use doxa_isolation::Policy;

pub(crate) const GUIDE: &str = "containers/session/README.md";

#[derive(Debug)]
pub(crate) struct Prerequisites {
    issues: Vec<String>,
}
impl Prerequisites {
    pub(crate) fn inspect() -> Self {
        let home = match doxa_isolation::home() {
            Ok(home) => home,
            Err(_) => return Self { issues: vec!["DOXA home unavailable".into()] },
        };
        let config = match doxa_state::load_config_checked(&home.join("config.toml")) {
            Ok(config) => config,
            Err(_) => return Self { issues: vec!["Owner configuration unreadable or invalid".into()] },
        };
        let config = toml::Value::Table(config);
        let policy = Policy::from_config(&config);
        let socket = policy.as_ref().ok().map(|policy| policy.docker_host.clone())
            .or_else(|| std::env::var("DOXA_DOCKER_HOST").ok())
            .or_else(|| config.get("docker_host").and_then(toml::Value::as_str).map(str::to_owned))
            .or_else(|| std::env::var("DOCKER_HOST").ok())
            .unwrap_or_else(|| format!("unix:///run/user/{}/docker.sock", unsafe { libc::geteuid() }));
        Self::check(policy, &socket)
    }

    fn check(policy: std::io::Result<Policy>, endpoint: &str) -> Self {
        let mut issues = Vec::new();
        if !cfg!(target_os = "linux") || unsafe { libc::geteuid() } == 0 {
            issues.push("Requires an unprivileged Linux host".into());
        }
        if let Err(error) = policy {
            issues.push(if error.to_string().contains("requires docker_image") {
                "Image digest missing".into()
            } else { format!("Image/resource policy: {}", crate::operations::safe_report_value(&error.to_string())) });
        }
        if let Err(error) = local_socket(endpoint) { issues.push(error.into()); }
        Self { issues }
    }

    pub(crate) fn ready(&self) -> bool { self.issues.is_empty() }
    pub(crate) fn label(&self) -> String {
        if self.ready() { "Image policy/socket checked · Start verifies Engine/image".into() }
        else { self.issues.join(" · ") }
    }
}

#[cfg(unix)]
fn local_socket(endpoint: &str) -> Result<(), &'static str> {
    use std::os::unix::fs::{FileTypeExt, MetadataExt};
    let path = endpoint.strip_prefix("unix://").map(Path::new)
        .filter(|path| path.is_absolute()).ok_or("Local Unix socket required")?;
    if matches!(path.to_str(), Some("/var/run/docker.sock" | "/run/docker.sock")) {
        return Err("Rootful Docker socket forbidden");
    }
    let metadata = std::fs::symlink_metadata(path).map_err(|_| "Rootless socket missing or inaccessible")?;
    if !metadata.file_type().is_socket() || metadata.uid() != unsafe { libc::geteuid() }
        || std::fs::canonicalize(path).ok().as_deref() != Some(path) {
        return Err("Socket must be canonical and owned by this user");
    }
    Ok(())
}
#[cfg(not(unix))]
fn local_socket(_: &str) -> Result<(), &'static str> { Err("Local Unix socket required") }

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::os::unix::{fs::symlink, net::UnixListener};
    #[test]
    fn docker_setup_checks_owned_canonical_socket_without_connecting() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("docker.sock");
        let endpoint = format!("unix://{}", path.display());
        assert!(local_socket(&endpoint).unwrap_err().contains("missing"));
        let _listener = UnixListener::bind(&path).unwrap();
        assert!(local_socket(&endpoint).is_ok());
        let alias = root.path().join("alias.sock");
        symlink(&path, &alias).unwrap();
        assert!(local_socket(&format!("unix://{}", alias.display())).is_err());
        assert!(local_socket("unix:///var/run/docker.sock").is_err());
        assert!(local_socket("tcp://localhost:2375").is_err());
        assert!(local_socket("ssh://example.com").is_err());
        // No connection was made to the listener (there is no Docker daemon).
        _listener.set_nonblocking(true).unwrap();
        assert_eq!(_listener.accept().unwrap_err().kind(), std::io::ErrorKind::WouldBlock);
    }
}

/// Pure policy validation for either wizard/Settings field; no environment
/// resolution or socket connection can hide an invalid edited value.
pub(crate) fn validate_setting(key: &str, value: &str) -> std::io::Result<()> {
    let mut policy = Policy {
        image: format!("sha256:{}", "0".repeat(64)),
        docker_host: "unix:///run/user/1000/docker.sock".into(),
        memory_bytes: 4 * 1024 * 1024 * 1024, cpus: 2.0, pids: 256,
        disk_soft_limit_bytes: None, disk_free_floor_bytes: None,
    };
    match key {
        "docker_image" => policy.image = value.into(),
        "docker_host" => policy.docker_host = value.into(),
        _ => return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput, "unsupported Docker setup field")),
    }
    policy.validate()
}

pub(crate) fn guide() -> String {
    format!("Docker isolation setup\n\n1. Use a non-root Linux account and rootless Docker with cgroup v2 memory/CPU/PID controls. Install newuidmap/newgidmap and configure at least 65,536 subordinate UIDs/GIDs.\nRootless installation: https://docs.docker.com/engine/security/rootless/\nWith the installed setup tool: dockerd-rootless-setuptool.sh install\nStart the user Engine: systemctl --user start docker.service\n\n2. In a current DOXA checkout, build the worker:\ncargo build --locked -p doxa-isolation\n\n3. Choose a reviewed Debian/Ubuntu base pinned as NAME@sha256:DIGEST, then build the worker image with the helper (replace the placeholders):\npython3 scripts/build-session-image.py --docker-host unix:///run/user/UID/docker.sock --base-image NAME@sha256:DIGEST --worker target/debug/doxa-isolation-worker\nAdd --claude-bin /absolute/claude and/or --codex-provider /absolute/codex-current for the engines you will use. The protected Codex receipt must match this checkout.\n\n4. Save the helper's printed sha256:CONTENT_ID in Docker worker image digest, and the same local rootless socket in Docker socket. A mutable tag is insufficient. Engine/rootless/cgroup/image checks run when starting a session.\n\nFull build/provider/toolchain guide: {GUIDE}")
}

pub(crate) fn report() -> String {
    let prerequisites = Prerequisites::inspect();
    format!("Docker prerequisites\n{}\n\n{}", prerequisites.label(), guide())
}
