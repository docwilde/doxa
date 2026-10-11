//! Local, read-only prerequisites. Engine/rootless/cgroup/image admission stays
//! in doxa-isolation; painting never runs a Docker command or connects a socket.
use std::path::Path;
use doxa_isolation::Policy;
use super::App;

pub(super) const GUIDE: &str = "containers/session/README.md";

#[derive(Debug)]
pub(super) struct Prerequisites {
    issues: Vec<String>,
}
impl Prerequisites {
    fn inspect() -> Self {
        let home = match doxa_isolation::home() {
            Ok(home) => home,
            Err(_) => return Self { issues: vec!["DOXA home unavailable".into()] },
        };
        let config = match doxa_state::load_config_checked(&home.join("config.toml")) {
            Ok(config) => config,
            Err(_) => return Self { issues: vec!["Owner configuration unreadable or invalid".into()] },
        };
        let socket = std::env::var("DOXA_DOCKER_HOST").ok()
            .or_else(|| config.get("docker_host").and_then(toml::Value::as_str).map(str::to_owned))
            .or_else(|| std::env::var("DOCKER_HOST").ok())
            .unwrap_or_else(|| format!("unix:///run/user/{}/docker.sock", unsafe { libc::geteuid() }));
        Self::check(Policy::configured(&home), &socket)
    }

    fn check(policy: std::io::Result<Policy>, endpoint: &str) -> Self {
        let mut issues = Vec::new();
        if !cfg!(target_os = "linux") || unsafe { libc::geteuid() } == 0 {
            issues.push("Requires an unprivileged Linux host".into());
        }
        if let Err(error) = policy {
            issues.push(if error.to_string().contains("requires docker_image") {
                "Image digest missing".into()
            } else { format!("Image/resource policy: {}", super::safe_label(&error.to_string())) });
        }
        if let Err(error) = local_socket(endpoint) { issues.push(error.into()); }
        Self { issues }
    }

    pub(super) fn ready(&self) -> bool { self.issues.is_empty() }
    pub(super) fn label(&self) -> String {
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

impl App {
    pub(super) fn refresh_docker_setup(&mut self) {
        self.docker_setup = self.new_session.as_ref().filter(|form| form.isolation.docker())
            .map(|_| Prerequisites::inspect());
        if self.docker_setup.as_ref().is_some_and(Prerequisites::ready) {
            if let Some(form) = self.new_session.as_mut() {
                if form.launch_error.as_deref().is_some_and(|error| error.starts_with("Docker setup:")) {
                    form.launch_error = None;
                }
            }
        }
    }

    pub(super) fn open_docker_settings(&mut self) -> bool {
        if self.launching || !self.new_session.as_ref().is_some_and(|form| form.isolation.docker()) { return false; }
        // Keep the draft in place: Settings owns modal input/rendering, while
        // pending model catalog replies can still update the underlying form.
        self.open_settings_menu();
        let Some(menu) = self.settings_menu.as_mut() else { return true; };
        menu.category = crate::settings::CATEGORIES.iter().position(|category| *category == "Session").unwrap_or(0);
        menu.selected = menu.rows.iter().position(|row| row.setting.key == "docker_image").unwrap_or(0);
        self.docker_settings_open = true;
        self.notice = format!("Docker worker image digest · Ctrl+S saves · Esc returns to your draft · guide: {GUIDE}");
        true
    }

    pub(super) fn close_settings_menu(&mut self) {
        self.settings_menu = None;
        self.choice_dropdown = None;
        if self.docker_settings_open {
            self.docker_settings_open = false;
            self.refresh_docker_setup();
            self.notice = "Returned to New Session · Docker choice, model and prompt retained".into();
        }
    }
}

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
