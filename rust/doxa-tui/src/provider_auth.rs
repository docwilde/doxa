//! Read-only host credential hints. No login, refresh, keyring/helper execution or network.
use serde_json::Value;
use std::{
    fs::OpenOptions,
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthState {
    Missing,
    Authenticated, // CLI status only; never server verification.
    Unauthenticated,
    TimedOut,
    Unknown,
    Present,
    ApiKey,
    Expired,
    RefreshRequired,
}
impl AuthState {
    pub fn text(self) -> &'static str {
        match self {
            Self::Missing => "CLI not installed",
            Self::Authenticated | Self::Present => {
                "credentials present; server validity unverified"
            }
            Self::Unauthenticated => "not authenticated; sign in via /login",
            Self::TimedOut => "auth status timed out; /setup to retry",
            Self::Unknown => "login unknown or missing; /setup to check",
            Self::ApiKey => "API key configured; validity unverified",
            Self::Expired => "login expired; sign in again via /login",
            Self::RefreshRequired => {
                "access token expired; refresh credential present, recovery unverified"
            }
        }
    }
    pub fn warning(self) -> bool {
        !matches!(self, Self::Present | Self::ApiKey | Self::Authenticated)
    }
}

fn owned(path: &Path) -> Option<Vec<u8>> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .ok()?;
    let meta = file.metadata().ok()?;
    if !meta.is_file() || meta.uid() != unsafe { libc::getuid() } || meta.len() > 64 * 1024 {
        return None;
    }
    let mut bytes = Vec::new();
    file.by_ref()
        .take(64 * 1024 + 1)
        .read_to_end(&mut bytes)
        .ok()?;
    (bytes.len() <= 64 * 1024).then_some(bytes)
}
fn nonempty(value: &Value) -> bool {
    value.as_str().is_some_and(|text| !text.trim().is_empty())
}

// The unverified JWT payload supplies an expiry hint only, never authentication proof.
fn jwt_expiry(token: &str) -> Option<u64> {
    if token.len() > 16384 {
        return None;
    }
    let mut parts = token.split('.');
    parts.next()?;
    let payload = parts.next()?;
    parts.next()?;
    if parts.next().is_some() || payload.len() > 8192 {
        return None;
    }
    let mut decoded = Vec::new();
    let (mut bits, mut count) = (0u32, 0u32);
    for byte in payload.bytes() {
        let digit = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'-' => 62,
            b'_' => 63,
            _ => return None,
        };
        bits = (bits << 6) | u32::from(digit);
        count += 6;
        if count >= 8 {
            count -= 8;
            decoded.push((bits >> count) as u8);
            bits &= (1 << count) - 1;
        }
    }
    serde_json::from_slice::<Value>(&decoded).ok()?["exp"].as_u64()
}
fn token_state(access: bool, refresh: bool, expiry: Option<u64>, now: u64) -> AuthState {
    if !access {
        return AuthState::Unknown;
    }
    match expiry {
        Some(expiry) if expiry <= now => {
            if refresh {
                AuthState::RefreshRequired
            } else {
                AuthState::Expired
            }
        }
        Some(_) if access => AuthState::Present,
        _ => AuthState::Unknown,
    }
}
fn metadata(provider: &str, value: &Value, now: u64) -> AuthState {
    if provider == "claude" {
        let oauth = &value["claudeAiOauth"];
        token_state(
            nonempty(&oauth["accessToken"]),
            nonempty(&oauth["refreshToken"]),
            oauth["expiresAt"].as_u64().map(|ms| ms / 1000),
            now,
        )
    } else {
        let mode = value["auth_mode"].as_str();
        if nonempty(&value["OPENAI_API_KEY"])
            && (mode.is_none() || matches!(mode, Some("apikey" | "api_key")))
        {
            return AuthState::ApiKey;
        }
        if mode.is_some_and(|mode| {
            !matches!(
                mode,
                "chatgpt" | "chatgptAuthTokens" | "chatgpt_auth_tokens"
            )
        }) {
            return AuthState::Unknown;
        }
        let tokens = &value["tokens"];
        token_state(
            nonempty(&tokens["access_token"]),
            nonempty(&tokens["refresh_token"]),
            tokens["access_token"].as_str().and_then(jwt_expiry),
            now,
        )
    }
}

fn paths(provider: &str) -> Option<(PathBuf, Option<PathBuf>)> {
    let home = std::env::var_os("HOME").map(PathBuf::from);
    if provider == "claude" {
        let base = std::env::var_os("CLAUDE_CONFIG_DIR")
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .or_else(|| home.map(|home| home.join(".claude")))?;
        Some((base.join(".credentials.json"), None))
    } else {
        let base = std::env::var_os("CODEX_HOME")
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .or_else(|| home.map(|home| home.join(".codex")))?;
        Some((base.join("auth.json"), Some(base.join("config.toml"))))
    }
}

#[derive(Default)]
struct Environment {
    claude_key: bool,
    claude_override: bool,
    codex_override: bool,
}
impl Environment {
    fn current() -> Self {
        let present = |key: &str| std::env::var_os(key).is_some_and(|value| !value.is_empty());
        Self {
            claude_key: present("ANTHROPIC_API_KEY"),
            claude_override: [
                "CLAUDE_CODE_OAUTH_TOKEN",
                "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_USE_BEDROCK",
                "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_FOUNDRY",
                "ANTHROPIC_PROFILE",
                "ANTHROPIC_FEDERATION_RULE_ID",
            ]
            .into_iter()
            .any(present),
            // Native 0.163 app-server and `login status` disable CODEX_API_KEY
            // env auth; generic OPENAI_API_KEY is not a ChatGPT login override.
            codex_override: ["CODEX_ACCESS_TOKEN", "OPENAI_WORKLOAD_IDENTITY_CONTEXT"]
                .into_iter()
                .any(present),
        }
    }
}

fn optional_json(path: &Path) -> Result<Value, ()> {
    match owned(path) {
        Some(raw) => serde_json::from_slice::<Value>(&raw)
            .ok()
            .filter(Value::is_object)
            .ok_or(()),
        None if matches!(std::fs::symlink_metadata(path),Err(error) if error.kind()==std::io::ErrorKind::NotFound) => {
            Ok(Value::Null)
        }
        None => Err(()),
    }
}

pub fn local(provider: &str) -> AuthState {
    let Some((path, config)) = paths(provider) else {
        return AuthState::Unknown;
    };
    let Ok(now) = SystemTime::now().duration_since(UNIX_EPOCH) else {
        return AuthState::Unknown;
    };
    selected_files(
        provider,
        &path,
        config.as_deref(),
        now.as_secs(),
        &Environment::current(),
    )
}

fn selected_files(
    provider: &str,
    path: &Path,
    config: Option<&Path>,
    now: u64,
    env: &Environment,
) -> AuthState {
    if provider == "claude" {
        if env.claude_override {
            return AuthState::Unknown;
        }
        let Ok(settings) = optional_json(&path.with_file_name("settings.json")) else {
            return AuthState::Unknown;
        };
        if settings.get("forceLoginMethod").is_some()
            || settings.get("forceLoginOrgUUID").is_some()
            || settings["env"].as_object().is_some_and(|vars| {
                vars.keys()
                    .any(|key| key.starts_with("ANTHROPIC_") || key.starts_with("CLAUDE_CODE_"))
            })
        {
            return AuthState::Unknown;
        }
        // Noninteractive Claude uses a supplied API key ahead of apiKeyHelper
        // and saved OAuth. Merely configured keys remain explicitly unverified.
        if env.claude_key {
            return AuthState::ApiKey;
        }
        if settings.get("apiKeyHelper").is_some() || cfg!(target_os = "macos") {
            return AuthState::Unknown;
        }
    } else if env.codex_override {
        return AuthState::Unknown;
    }
    local_files(provider, path, config, now)
}

fn local_files(provider: &str, path: &Path, config: Option<&Path>, now: u64) -> AuthState {
    let mut forced = None;
    if let Some(config) = config {
        if let Some(raw) = owned(config) {
            let Some(parsed) = std::str::from_utf8(&raw)
                .ok()
                .and_then(|raw| raw.parse::<toml::Table>().ok())
            else {
                return AuthState::Unknown;
            };
            // A file may be stale and ignored by an explicitly selected
            // keyring, helper or future store. Never infer that it is active.
            if parsed
                .get("cli_auth_credentials_store")
                .is_some_and(|store| store.as_str() != Some("file"))
            {
                return AuthState::Unknown;
            }
            if parsed.contains_key("profile")
                || parsed
                    .get("model_provider")
                    .is_some_and(|provider| provider.as_str() != Some("openai"))
                || parsed
                    .get("model_providers")
                    .and_then(toml::Value::as_table)
                    .is_some_and(|providers| providers.contains_key("openai"))
            {
                return AuthState::Unknown;
            }
            if let Some(method) = parsed.get("forced_login_method") {
                forced = match method.as_str() {
                    Some("api") => Some(false),
                    Some("chatgpt") => Some(true),
                    _ => return AuthState::Unknown,
                };
            }
        } else if !matches!(std::fs::symlink_metadata(config),Err(error) if error.kind()==std::io::ErrorKind::NotFound)
        {
            return AuthState::Unknown;
        }
    }
    let Some(raw) = owned(&path) else {
        return AuthState::Unknown;
    };
    let Some(value) = serde_json::from_slice::<Value>(&raw).ok() else {
        return AuthState::Unknown;
    };
    let state = metadata(provider, &value, now);
    if matches!(forced, Some(true)) && state == AuthState::ApiKey
        || matches!(forced, Some(false)) && state != AuthState::ApiKey
    {
        AuthState::Unknown
    } else {
        state
    }
}

pub fn snapshot() -> [(&'static str, AuthState); 2] {
    ["claude", "codex"].map(|provider| {
        let state = if crate::launch::executable(Path::new(provider)).is_err() {
            AuthState::Missing
        } else {
            local(provider)
        };
        (provider, state)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn expired_access_is_not_authenticated_even_with_cli_success_and_refresh_is_not_rejection() {
        let mut value = json!({"claudeAiOauth":{"accessToken":"PRIVATE","expiresAt":100000,"refreshToken":"RECOVERABLE"}});
        assert_eq!(metadata("claude", &value, 101), AuthState::RefreshRequired);
        value["claudeAiOauth"]["refreshToken"] = Value::Null;
        assert_eq!(metadata("claude", &value, 101), AuthState::Expired);
        value["claudeAiOauth"]["expiresAt"] = json!(200000);
        assert_eq!(metadata("claude", &value, 101), AuthState::Present);
        assert!(!AuthState::RefreshRequired.text().contains("PRIVATE"));
    }
    #[test]
    fn codex_access_expiry_and_api_keys_are_separate() {
        // eyJleHAiOjEwMH0 is {"exp":100}; no signature or server validation implied.
        let value = json!({"auth_mode":"chatgpt","tokens":{"access_token":"a.eyJleHAiOjEwMH0.c","refresh_token":"r"}});
        assert_eq!(metadata("codex", &value, 101), AuthState::RefreshRequired);
        assert_eq!(metadata("codex", &value, 99), AuthState::Present);
        assert_eq!(
            metadata(
                "codex",
                &json!({"auth_mode":"apikey","OPENAI_API_KEY":"private"}),
                101
            ),
            AuthState::ApiKey
        );
        assert_eq!(
            metadata("codex", &json!({"tokens":{"access_token":"opaque"}}), 101),
            AuthState::Unknown
        );
    }
    #[test]
    fn metadata_read_never_blocks_on_fifo_or_follows_secret_symlink() {
        use std::os::unix::ffi::OsStrExt;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("credentials");
        let name = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        assert!(owned(&path).is_none());
        std::fs::remove_file(&path).unwrap();
        std::os::unix::fs::symlink("/not-a-credential", &path).unwrap();
        assert!(owned(&path).is_none());
    }
    #[test]
    fn missing_malformed_or_non_file_store_cannot_look_authenticated_from_stale_metadata() {
        let dir = tempfile::tempdir().unwrap();
        let auth = dir.path().join("auth.json");
        let config = dir.path().join("config.toml");
        assert_eq!(
            local_files("codex", &auth, Some(&config), 99),
            AuthState::Unknown
        );
        std::fs::write(
            &auth,
            br#"{"tokens":{"access_token":"a.eyJleHAiOjEwMH0.c"}}"#,
        )
        .unwrap();
        assert_eq!(
            local_files("codex", &auth, Some(&config), 99),
            AuthState::Present
        );
        for setting in [
            "cli_auth_credentials_store = 'keyring'",
            "cli_auth_credentials_store = 'auto'",
            "cli_auth_credentials_store = 7",
            "invalid = [",
        ] {
            std::fs::write(&config, setting).unwrap();
            assert_eq!(
                local_files("codex", &auth, Some(&config), 99),
                AuthState::Unknown,
                "{setting}"
            );
        }
        std::fs::write(&config, "cli_auth_credentials_store = 'file'").unwrap();
        assert_eq!(
            local_files("codex", &auth, Some(&config), 99),
            AuthState::Present
        );
    }
    #[test]
    fn inactive_oauth_files_do_not_override_helpers_env_tokens_or_forced_api_mode() {
        let dir = tempfile::tempdir().unwrap();
        let auth = dir.path().join("auth.json");
        let config = dir.path().join("config.toml");
        std::fs::write(&auth,br#"{"auth_mode":"chatgpt","tokens":{"access_token":"a.eyJleHAiOjEwMH0.c","refresh_token":"r"}}"#).unwrap();
        std::fs::write(&config, "forced_login_method = 'chatgpt'").unwrap();
        // OPENAI_API_KEY is intentionally not an input to native app-server
        // auth selection; an unrelated exported key cannot hide this expiry.
        assert_eq!(
            selected_files("codex", &auth, Some(&config), 101, &Environment::default()),
            AuthState::RefreshRequired
        );
        std::fs::write(&config, "forced_login_method = 'api'").unwrap();
        assert_eq!(
            selected_files("codex", &auth, Some(&config), 101, &Environment::default()),
            AuthState::Unknown
        );
        std::fs::write(&config, "").unwrap();
        assert_eq!(
            selected_files(
                "codex",
                &auth,
                Some(&config),
                101,
                &Environment {
                    codex_override: true,
                    ..Environment::default()
                }
            ),
            AuthState::Unknown
        );
        let claude = dir.path().join(".credentials.json");
        std::fs::write(
            &claude,
            br#"{"claudeAiOauth":{"accessToken":"private","expiresAt":1000}}"#,
        )
        .unwrap();
        assert_eq!(
            selected_files(
                "claude",
                &claude,
                None,
                101,
                &Environment {
                    claude_override: true,
                    ..Environment::default()
                }
            ),
            AuthState::Unknown
        );
        std::fs::write(
            dir.path().join("settings.json"),
            br#"{"apiKeyHelper":"never execute this helper"}"#,
        )
        .unwrap();
        assert_eq!(
            selected_files("claude", &claude, None, 101, &Environment::default()),
            AuthState::Unknown
        );
        assert_eq!(
            selected_files(
                "claude",
                &claude,
                None,
                101,
                &Environment {
                    claude_key: true,
                    ..Environment::default()
                }
            ),
            AuthState::ApiKey
        );
    }
}
