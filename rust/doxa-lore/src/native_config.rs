//! Resolve DOXA's sticky root before the canonical shared LORE settings loader.
//! Neither configuration nor carrier setup mutates the process environment.
use crate::LoreError;
use lore_core::config::Config;
use std::path::PathBuf;
use std::time::Duration;

pub(crate) fn carrier_root() -> Result<Option<PathBuf>, LoreError> {
    if let Some(root) =
        std::env::var_os("LORE_ROOT").filter(|value| !value.to_string_lossy().trim().is_empty())
    {
        let root = PathBuf::from(root);
        if !root.is_absolute() {
            return Err(LoreError::InvalidFrame);
        }
        return Ok(Some(root));
    }
    let home = std::env::var_os("DOXA_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|value| PathBuf::from(value).join(".doxa")))
        .ok_or(LoreError::Unavailable)?;
    let table = doxa_state::load_config(&home.join("config.toml"));
    let Some(root) = table
        .get("lore_root")
        .and_then(|value| value.as_str())
        .filter(|value| !value.is_empty())
    else {
        return Ok(None);
    };
    let root = PathBuf::from(root);
    if !root.is_absolute() {
        return Err(LoreError::InvalidFrame);
    }
    Ok(Some(root))
}

pub(crate) fn resolve(timeout: Duration) -> Result<Config, LoreError> {
    Config::from_env_with_root(carrier_root()?, timeout)
        .map_err(|error| LoreError::Remote(error.code()))
}

pub(crate) fn review_disabled() -> Result<bool, LoreError> {
    let config = resolve(Duration::from_secs(1))?;
    let settings = config
        .settings()
        .map_err(|error| LoreError::Remote(error.code()))?;
    let value = |name| match settings.get(name) {
        Ok(value) => Ok(Some(value)),
        Err(std::env::VarError::NotPresent) => Ok(None),
        Err(std::env::VarError::NotUnicode(_)) => Err(LoreError::InvalidFrame),
    };
    Ok(
        value("LORE_DISABLE_REVIEW")?.is_some_and(|value| !matches!(value.as_str(), "" | "0"))
            || value("LORE_SKIP")?.is_some_and(|value| !value.is_empty()),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::{fs, process::Command};

    #[test]
    fn shared_settings_respect_sticky_roots_overrides_and_isolated_homes() {
        let root = tempfile::tempdir().unwrap();
        let home = root.path().join("home");
        let claude = home.join(".claude");
        let doxa = home.join(".doxa");
        fs::create_dir_all(&claude).unwrap();
        fs::create_dir_all(&doxa).unwrap();
        let settings = claude.join("settings.json");
        fs::write(&settings, r#"{"env":{"LORE_MEMORY_CAP":"17600","LORE_FILEMAP_CAP":"8800","LORE_SYNC_HMAC_KEY":"synthetic-test-key","LORE_DISABLE_SYNC":"0"}}"#).unwrap();
        fs::set_permissions(&settings, fs::Permissions::from_mode(0o600)).unwrap();
        let custom = root.path().join("custom/lore");
        let default_root = claude.join("lore");
        for mode in [
            "sticky-default",
            "sticky-custom",
            "matching-custom",
            "explicit-env",
            "explicit-empty-cap",
            "isolated-home",
        ] {
            let sticky = if mode == "sticky-custom" || mode == "matching-custom" {
                &custom
            } else {
                &default_root
            };
            fs::write(
                doxa.join("config.toml"),
                format!(
                    "lore_root = {}\n",
                    serde_json::to_string(&sticky.to_string_lossy()).unwrap()
                ),
            )
            .unwrap();
            let mut child = Command::new(std::env::current_exe().unwrap());
            child
                .args([
                    "--exact",
                    "native_config::tests::configuration_probe",
                    "--nocapture",
                ])
                .env_clear()
                .env("HOME", &home)
                .env("DOXA_HOME", &doxa)
                .env("DOXA_CONFIG_PROBE", mode);
            if let Some(tmp) = std::env::var_os("TMPDIR") {
                child.env("TMPDIR", tmp);
            }
            match mode {
                "matching-custom" => {
                    fs::create_dir_all(custom.parent().unwrap()).unwrap();
                    fs::copy(&settings, custom.parent().unwrap().join("settings.json")).unwrap();
                    child.env("CLAUDE_CONFIG_DIR", custom.parent().unwrap());
                }
                "explicit-env" => {
                    child
                        .env("LORE_ROOT", &custom)
                        .env("LORE_MEMORY_CAP", "12345")
                        .env("LORE_FILEMAP_CAP", "6789")
                        .env("LORE_SYNC_HMAC_KEY", "")
                        .env("LORE_DISABLE_SYNC", "1");
                }
                "explicit-empty-cap" => {
                    child.env("LORE_MEMORY_CAP", "");
                }
                "isolated-home" => {
                    let isolated = root.path().join("isolated");
                    fs::create_dir_all(&isolated).unwrap();
                    child.env("HOME", &isolated).env_remove("DOXA_HOME");
                }
                _ => {}
            }
            assert!(
                child.status().unwrap().success(),
                "configuration probe failed: {mode}"
            );
        }
        fs::write(
            doxa.join("config.toml"),
            format!(
                "lore_root = {}\n",
                serde_json::to_string(&default_root.to_string_lossy()).unwrap()
            ),
        )
        .unwrap();
        for (name, mode) in [
            ("LORE_DISABLE_REVIEW", "review-disabled"),
            ("LORE_SKIP", "review-skipped"),
        ] {
            let mut values = serde_json::Map::new();
            values.insert(name.into(), serde_json::json!("1"));
            fs::write(&settings, serde_json::json!({"env":values}).to_string()).unwrap();
            for override_empty in [false, true] {
                let mut child = Command::new(std::env::current_exe().unwrap());
                child
                    .args([
                        "--exact",
                        "native_config::tests::configuration_probe",
                        "--nocapture",
                    ])
                    .env_clear()
                    .env("HOME", &home)
                    .env("DOXA_HOME", &doxa)
                    .env(
                        "DOXA_CONFIG_PROBE",
                        if override_empty {
                            "review-empty-override"
                        } else {
                            mode
                        },
                    );
                if override_empty {
                    child.env(name, "");
                }
                assert!(
                    child.status().unwrap().success(),
                    "review policy probe failed"
                );
            }
        }
    }

    #[test]
    fn configuration_probe() {
        let Ok(mode) = std::env::var("DOXA_CONFIG_PROBE") else {
            return;
        };
        let before = std::env::vars_os().collect::<Vec<_>>();
        if mode.starts_with("review-") {
            assert_eq!(review_disabled().unwrap(), mode != "review-empty-override");
            assert_eq!(before, std::env::vars_os().collect::<Vec<_>>());
            return;
        }
        if mode == "explicit-empty-cap" {
            assert!(resolve(Duration::from_secs(1)).is_err());
            assert_eq!(before, std::env::vars_os().collect::<Vec<_>>());
            return;
        }
        let config = resolve(Duration::from_secs(1)).unwrap();
        let root = carrier_root().unwrap();
        match mode.as_str() {
            "sticky-default" | "matching-custom" => {
                assert_eq!((config.project_cap, config.filemap_cap), (17600, 8800));
                assert!(config.sync.key.is_some());
                assert!(config.sync.enabled);
                assert_eq!(Some(&config.root), root.as_ref());
            }
            "sticky-custom" | "isolated-home" => {
                assert_eq!((config.project_cap, config.filemap_cap), (8800, 4400));
                assert!(config.sync.key.is_none());
                if mode == "isolated-home" {
                    assert!(config.root.starts_with(std::env::var_os("HOME").unwrap()));
                }
            }
            "explicit-env" => {
                assert_eq!((config.project_cap, config.filemap_cap), (12345, 6789));
                assert!(config.sync.key.is_none());
                assert!(!config.sync.enabled);
                assert_eq!(
                    config.root,
                    PathBuf::from(std::env::var_os("LORE_ROOT").unwrap())
                );
            }
            _ => panic!("unknown synthetic probe"),
        }
        assert_eq!(before, std::env::vars_os().collect::<Vec<_>>());
    }
}
