use crate::{Error, ErrorKind, Result};
use serde::{Deserialize, Serialize};
use std::{collections::{BTreeMap, BTreeSet}, path::{Path, PathBuf}};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub schema_version: u32,
    pub servers: Vec<ServerConfig>,
}

impl Config {
    /// An explicitly supplied owner-private file. Never discovers project files.
    pub fn load(path: &Path) -> Result<Self> {
        if !path.is_absolute() { return Err(Error::config("config path must be absolute")); }
        let bytes=doxa_vendors::credentials::read_private_config(path,262144)
            .map_err(|_|Error::config("config requires a protected owner directory and regular 0600 file"))?;
        let value = crate::wire::parse(&bytes).map_err(|_| Error::config("invalid or duplicate JSON field"))?;
        let config: Self = serde_json::from_value(value).map_err(|_| Error::config("invalid config schema"))?;
        config.validate()?;
        Ok(config)
    }
    pub fn validate(&self) -> Result<()> {
        if self.schema_version != 1 || self.servers.len() > 16 { return Err(Error::config("unsupported schema or server count")); }
        let mut ids = BTreeSet::new();
        for server in &self.servers {
            server.validate()?;
            if !ids.insert(&server.id) { return Err(Error::config("duplicate server ID")); }
        }
        Ok(())
    }
    pub fn enabled_servers(&self) -> impl Iterator<Item = &ServerConfig> { self.servers.iter().filter(|server| server.enabled) }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ServerConfig {
    pub id: String,
    pub enabled: bool,
    pub allow_tools: Vec<String>,
    pub transport: TransportConfig,
    #[serde(default)]
    pub limits: Limits,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(tag = "transport", rename_all = "snake_case", deny_unknown_fields)]
pub enum TransportConfig {
    Stdio { program: PathBuf, #[serde(default)] args: Vec<String>, #[serde(default)] env: BTreeMap<String, EnvRef> },
    StreamableHttp { url: String, #[serde(default)] headers: BTreeMap<String, EnvRef>, #[serde(default)] allow_loopback_http: bool },
}

// Endpoints, arguments and credential-bearing state are never debug diagnostics.
impl std::fmt::Debug for TransportConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self { Self::Stdio { .. } => "Stdio { .. }", Self::StreamableHttp { .. } => "StreamableHttp { .. }" })
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EnvRef {
    pub env: String,
    #[serde(default)]
    pub bearer: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Limits {
    pub timeout_ms: u64,
    pub max_frame_bytes: usize,
    pub max_catalog_bytes: usize,
    pub max_list_pages: usize,
    pub max_tools: usize,
    pub max_notifications: usize,
    pub shutdown_ms: u64,
}
impl Default for Limits {
    fn default() -> Self { Self { timeout_ms: 30000, max_frame_bytes: 262144, max_catalog_bytes: 1048576, max_list_pages: 16, max_tools: 128, max_notifications: 64, shutdown_ms: 500 } }
}
impl Limits {
    pub fn validate(&self) -> Result<()> {
        if !(1..=120000).contains(&self.timeout_ms) || !(1024..=1048576).contains(&self.max_frame_bytes)
            || !(1024..=4194304).contains(&self.max_catalog_bytes)
            || !(1..=64).contains(&self.max_list_pages) || !(1..=1024).contains(&self.max_tools)
            || !(1..=256).contains(&self.max_notifications) || !(1..=2000).contains(&self.shutdown_ms) {
            return Err(Error::config("limits exceed supported bounds"));
        }
        Ok(())
    }
}

pub(crate) fn tool_name(text: &str) -> bool {
    !text.is_empty() && text.len() <= 128 && text.bytes().all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
}
fn env_name(text: &str) -> bool {
    !text.is_empty() && text.len() <= 128 && text.bytes().enumerate().all(|(i,b)| b == b'_' || b.is_ascii_alphabetic() || (i > 0 && b.is_ascii_digit()))
}
impl ServerConfig {
    pub fn validate(&self) -> Result<()> {
        self.limits.validate()?;
        if self.id.is_empty() || self.id.len() > 32 || !self.id.as_bytes()[0].is_ascii_lowercase()
            || !self.id.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-') {
            return Err(Error::config("invalid server ID"));
        }
        let mut tools = BTreeSet::new();
        if self.allow_tools.len() > self.limits.max_tools { return Err(Error::config("allowlist exceeds tool limit")); }
        for tool in &self.allow_tools {
            if !tool_name(tool) || !tools.insert(tool) { return Err(Error::config("invalid or duplicate allowed tool")); }
        }
        match &self.transport {
            TransportConfig::Stdio { program, args, env } => {
                if !program.is_absolute() || args.len() > 64 || args.iter().any(|v| v.len() > 4096 || v.contains('\0')) || env.len() > 32 {
                    return Err(Error::config("invalid stdio command"));
                }
                // Installation is a separate operator action, never a connection side effect.
                if program.file_name().and_then(|v| v.to_str()).is_some_and(|v| matches!(v, "npx" | "npm" | "pnpm" | "yarn" | "uvx")) {
                    return Err(Error::config("package installers are not MCP executables"));
                }
                for (name, reference) in env {
                    if !env_name(name) || !env_name(&reference.env) || reference.bearer { return Err(Error::config("invalid stdio env reference")); }
                }
            }
            TransportConfig::StreamableHttp { url, headers, allow_loopback_http } => {
                endpoint(url, *allow_loopback_http)?;
                if headers.len() > 16 { return Err(Error::config("too many headers")); }
                let mut names = BTreeSet::new();
                for (name, reference) in headers {
                    let lower = name.to_ascii_lowercase();
                    if reqwest::header::HeaderName::from_bytes(name.as_bytes()).is_err() || !names.insert(lower.clone())
                        || matches!(lower.as_str(), "host" | "accept" | "content-type" | "content-length" | "transfer-encoding"
                            | "connection" | "mcp-protocol-version" | "mcp-session-id" | "proxy-authorization" | "cookie")
                        || !env_name(&reference.env) { return Err(Error::config("invalid or reserved header")); }
                }
            }
        }
        Ok(())
    }
}

pub(crate) fn endpoint(text: &str, allow_local: bool) -> Result<reqwest::Url> {
    if text.len() > 2048 { return Err(Error::config("endpoint exceeds limit")); }
    let url = reqwest::Url::parse(text).map_err(|_| Error::config("invalid endpoint"))?;
    let loopback = matches!(url.host_str(), Some("localhost" | "127.0.0.1" | "[::1]" | "::1"));
    if url.host_str().is_none() || !url.username().is_empty() || url.password().is_some() || url.query().is_some() || url.fragment().is_some()
        || !(url.scheme() == "https" || (url.scheme() == "http" && allow_local && loopback)) {
        return Err(Error::config("endpoint requires HTTPS or explicit loopback HTTP; no URL credentials/query/fragment"));
    }
    Ok(url)
}

pub(crate) fn resolve(reference: &EnvRef) -> Result<String> {
    if let Some(provider)=doxa_vendors::credentials::SearchProvider::from_env_var(&reference.env) {
        return doxa_vendors::credentials::search_resolve(provider)
            .map_err(|_|Error::new(ErrorKind::Credential,false))?
            .ok_or_else(||Error::new(ErrorKind::Credential,false));
    }
    std::env::var(&reference.env).ok().filter(|v| !v.is_empty() && v.len() <= 8192 && !v.contains('\0'))
        .ok_or_else(|| Error::new(ErrorKind::Credential, false))
}

pub(crate) fn credential_free(text: &str) -> Result<()> {
    let scrubbed=doxa_vendors::credentials::redact(text).map_err(|_|Error::new(ErrorKind::Credential,false))?;
    if scrubbed!=text { return Err(Error::config("credentials must not occur in endpoint or arguments")); }
    Ok(())
}
