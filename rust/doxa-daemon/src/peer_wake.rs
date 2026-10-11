//! The owner may grant peer-started turns through environment or Settings.
//! File-backed grants are refreshed at receipt; explicit launch grants stay fixed.
use std::path::PathBuf;

pub struct Policy {
    inherited: Option<String>,
    config: Option<PathBuf>,
}
impl Policy {
    pub fn from_environment() -> Self {
        let inherited = std::env::var("DOXA_PEER_INBOUND_TURNS").ok()
            .filter(|value| !value.trim().is_empty());
        let config = std::env::var_os("DOXA_HOME").filter(|value| !value.is_empty()).map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|value| PathBuf::from(value).join(".doxa")))
            .filter(|path| path.is_absolute()).map(|path| path.join("config.toml"));
        Self { inherited, config }
    }
    pub fn enabled(&self) -> bool {
        let raw = if let Some(value) = &self.inherited { value.clone() }
        else {
            let Some(path) = &self.config else { return false; };
            let Ok(config) = doxa_state::load_config_checked(path) else { return false; };
            doxa_state::raw_setting(None, &config, "peer_inbound_turns")
        };
        !raw.trim().is_empty()
            && !matches!(raw.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no" | "off")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn saved_grant_refreshes_without_overriding_explicit_launch_policy() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        let config = dir.path().join("config.toml");
        let policy = Policy { inherited: None, config: Some(config.clone()) };
        assert!(!policy.enabled());
        doxa_state::save_config(&config, &"peer_inbound_turns = true".parse().unwrap()).unwrap();
        assert!(policy.enabled());
        let denied = Policy { inherited: Some("off".into()), config: Some(config.clone()) };
        assert!(!denied.enabled());
        doxa_state::save_config(&config, &"peer_inbound_turns = false".parse().unwrap()).unwrap();
        assert!(!policy.enabled());
        let granted = Policy { inherited: Some("yes".into()), config: Some(config.clone()) };
        assert!(granted.enabled());
        std::fs::write(&config, "not = [valid").unwrap();
        assert!(!policy.enabled());
        assert!(granted.enabled());
    }
}
