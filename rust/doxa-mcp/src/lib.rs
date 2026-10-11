//! Opt-in MCP tools client. Schemas, descriptions and returned content are
//! untrusted data; the embedding host owns authorization and tool provenance.
mod config;
mod http;
mod stdio;
mod wire;
pub use config::{Config, EnvRef, Limits, ServerConfig, TransportConfig};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::{BTreeMap, BTreeSet}, path::Path, sync::{Arc, atomic::{AtomicBool, Ordering}}, time::Duration};

pub const PROTOCOL_VERSIONS: &[&str] = &["2025-11-25", "2025-06-18", "2025-03-26"];
pub type Result<T> = std::result::Result<T, Error>;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ErrorKind {
    Config(&'static str), Credential, Disabled, Io, Protocol(&'static str), UnsupportedVersion,
    MissingToolsCapability, Limit(&'static str), Timeout, Cancelled, Closed,
    Remote { code: i64 }, Http { status: u16 }, SessionExpired, ToolNotAllowed, CatalogChanged,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Error {
    pub kind: ErrorKind,
    /// A conservative send boundary, not evidence that the server did no work.
    pub attempted: bool,
}
impl Error {
    pub fn new(kind: ErrorKind, attempted: bool) -> Self { Self { kind, attempted } }
    pub(crate) fn config(reason: &'static str) -> Self { Self::new(ErrorKind::Config(reason),false) }
    pub(crate) fn protocol(reason: &'static str) -> Self { Self::new(ErrorKind::Protocol(reason),true) }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { write!(f,"MCP {:?}",self.kind) }
}
impl std::error::Error for Error {}

#[derive(Clone, Debug, Serialize)]
pub struct ServerInfo { pub protocol_version: String, pub name: String, pub version: String }

#[derive(Clone, Debug, Serialize)]
pub struct Tool {
    pub name: String,
    pub description: Option<String>,
    pub input_schema: Value,
    pub output_schema: Option<Value>,
}
impl Tool {
    fn from_value(value: &Value) -> Result<Self> {
        let name = value["name"].as_str().filter(|v|config::tool_name(v)).ok_or_else(||Error::protocol("invalid tool name"))?;
        let description = match value.get("description") {
            None => None,
            Some(value) => Some(value.as_str().filter(|v|v.len() <= 16384).ok_or_else(||Error::protocol("invalid tool description"))?.to_owned()),
        };
        let schema = &value["inputSchema"];
        if !schema.is_object() || schema["type"] != "object" { return Err(Error::protocol("object inputSchema required")); }
        let output_schema = match value.get("outputSchema") {
            None => None,
            Some(value) if value.is_object() && value["type"] == "object" => Some(value.clone()),
            _ => return Err(Error::protocol("invalid outputSchema")),
        };
        Ok(Self { name:name.into(), description, input_schema:schema.clone(), output_schema })
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CallResult {
    pub content: Vec<Value>,
    #[serde(rename = "structuredContent", skip_serializing_if = "Option::is_none")]
    pub structured_content: Option<Value>,
    #[serde(rename = "isError")]
    pub is_error: bool,
}
impl CallResult {
    fn from_value(value: Value) -> Result<Self> {
        let content = value["content"].as_array().filter(|v|v.len() <= 128).ok_or_else(||Error::protocol("content array required"))?;
        for block in content {
            let valid = match block["type"].as_str() {
                Some("text") => block["text"].is_string(),
                Some("image"|"audio") => block["data"].is_string() && block["mimeType"].is_string(),
                Some("resource_link") => block["uri"].is_string() && block["name"].is_string(),
                Some("resource") => block["resource"].is_object() && block["resource"]["uri"].is_string()
                    && (block["resource"]["text"].is_string() || block["resource"]["blob"].is_string()),
                _ => false,
            };
            if !valid { return Err(Error::protocol("invalid content block")); }
        }
        let structured_content = match value.get("structuredContent") {
            None => None,
            Some(value) if value.is_object() => Some(value.clone()),
            _ => return Err(Error::protocol("structuredContent object required")),
        };
        let is_error = match value.get("isError") {
            None => false,
            Some(value) => value.as_bool().ok_or_else(||Error::protocol("invalid isError"))?,
        };
        Ok(Self { content:content.clone(), structured_content, is_error })
    }
}

enum Transport { Stdio(stdio::StdioTransport), Http(http::HttpTransport) }

/// Serialized lifecycle with no automatic tool replay, installation or discovery.
pub struct Client {
    transport: Transport,
    config: ServerConfig,
    info: Option<ServerInfo>,
    next_id: u64,
    closed: bool,
    changed: bool,
    catalog_generation: u64,
    catalog: BTreeMap<String,Tool>,
    secrets: Vec<String>,
}
impl Client {
    pub async fn connect(config: &ServerConfig, cwd: &Path, cancel: Arc<AtomicBool>) -> Result<Self> {
        config.validate()?;
        if !config.enabled { return Err(Error::new(ErrorKind::Disabled,false)); }
        if !cwd.is_absolute() { return Err(Error::config("MCP cwd must be absolute")); }
        if cancel.load(Ordering::SeqCst) { return Err(Error::new(ErrorKind::Cancelled,false)); }
        let (transport,secrets) = match &config.transport {
            TransportConfig::Stdio { program,args,env } => {
                let (transport,secrets) = stdio::StdioTransport::open(program,args,env,cwd)?;
                (Transport::Stdio(transport),secrets)
            }
            TransportConfig::StreamableHttp { url,headers,allow_loopback_http } => {
                let (transport,secrets) = http::HttpTransport::open(url,headers,*allow_loopback_http,&config.limits)?;
                (Transport::Http(transport),secrets)
            }
        };
        let mut client = Self { transport, config:config.clone(), info:None, next_id:1, closed:false, changed:false, catalog_generation:0, catalog:BTreeMap::new(), secrets };
        let initialized = client.initialize(cancel).await;
        if let Err(error) = initialized { client.close().await; return Err(error); }
        Ok(client)
    }
    async fn initialize(&mut self, cancel: Arc<AtomicBool>) -> Result<()> {
        let result = self.request("initialize",json!({"protocolVersion":PROTOCOL_VERSIONS[0],"capabilities":{},
            "clientInfo":{"name":"doxa","version":env!("CARGO_PKG_VERSION")}}),cancel.clone()).await?;
        let version = result["protocolVersion"].as_str().filter(|v|PROTOCOL_VERSIONS.contains(v))
            .ok_or_else(||Error::new(ErrorKind::UnsupportedVersion,true))?;
        if !result["capabilities"].is_object() || !result["capabilities"]["tools"].is_object() {
            return Err(Error::new(ErrorKind::MissingToolsCapability,true));
        }
        let name = bounded_identity(&result["serverInfo"]["name"])?;
        let server_version = bounded_identity(&result["serverInfo"]["version"])?;
        self.info = Some(ServerInfo { protocol_version:version.into(), name, version:server_version });
        if let Transport::Http(transport) = &mut self.transport { transport.version = Some(version.into()); }
        let notification = json!({"jsonrpc":"2.0","method":"notifications/initialized"});
        let limits = self.config.limits.clone();
        let transport = &mut self.transport;
        let result = bounded(async {
            match transport {
                Transport::Stdio(transport) => transport.send(&notification,limits.max_frame_bytes,&AtomicBool::new(false)).await,
                Transport::Http(transport) => transport.notify(&notification,&limits).await,
            }
        },limits.timeout_ms,cancel).await;
        result.map_err(|mut error|{error.attempted = true;error})
    }
    pub fn info(&self) -> &ServerInfo { self.info.as_ref().expect("only initialized clients are public") }
    pub fn catalog_changed(&self) -> bool { self.changed }
    pub fn is_closed(&self) -> bool { self.closed }
    /// Re-resolve only the configured credential references and compare with
    /// the private connection snapshot. No credential or digest is exposed.
    /// Authentication stays frozen; a changed source requires a new client.
    pub fn credentials_current(&self) -> Result<bool> {
        let references=match &self.config.transport {
            TransportConfig::Stdio {env,..}=>env.values(),
            TransportConfig::StreamableHttp {headers,..}=>headers.values(),
        };
        let current=references.map(config::resolve).collect::<Result<Vec<_>>>()?;
        Ok(current==self.secrets)
    }
    async fn request(&mut self, method: &str, params: Value, cancel: Arc<AtomicBool>) -> Result<Value> {
        self.request_with_timeout(method,params,cancel,self.config.limits.timeout_ms).await
    }
    async fn request_with_timeout(&mut self, method: &str, params: Value, cancel: Arc<AtomicBool>, timeout_ms: u64) -> Result<Value> {
        if self.closed { return Err(Error::new(ErrorKind::Closed,false)); }
        if cancel.load(Ordering::SeqCst) { return Err(Error::new(ErrorKind::Cancelled,false)); }
        let id = self.next_id;
        self.next_id = self.next_id.checked_add(1).ok_or_else(||Error::new(ErrorKind::Limit("request IDs"),false))?;
        let request = json!({"jsonrpc":"2.0","id":id,"method":method,"params":params});
        let attempted = Arc::new(AtomicBool::new(false));
        let limits = self.config.limits.clone();
        let mut changed=false;
        let result = match &mut self.transport {
            Transport::Stdio(transport) => bounded(transport.exchange(&request,id,&limits,attempted.clone(),&mut changed),timeout_ms,cancel).await,
            Transport::Http(transport) => bounded(transport.exchange(&request,id,&limits,attempted.clone(),&mut changed),timeout_ms,cancel).await,
        };
        if changed {self.changed=true;self.catalog_generation=self.catalog_generation.saturating_add(1);}
        self.finish_request(result,method,id,attempted.load(Ordering::SeqCst)).await
    }
    async fn finish_request(&mut self, result: Result<Value>, method: &str, id: u64, attempted: bool) -> Result<Value> {
        match result {
            Ok(mut value) => {
                if let Err(error) = wire::redact(&mut value,&self.secrets) { self.close().await; return Err(error); }
                Ok(value)
            },
            Err(mut error) => {
                error.attempted |= attempted;
                if matches!(error.kind,ErrorKind::Timeout|ErrorKind::Cancelled) && attempted && method != "initialize" {
                    let notification = json!({"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":id,"reason":"Client stopped waiting"}});
                    let limits = self.config.limits.clone();
                    let _ = tokio::time::timeout(Duration::from_millis(limits.shutdown_ms),async {
                        match &mut self.transport {
                            Transport::Stdio(transport) => transport.send(&notification,limits.max_frame_bytes,&AtomicBool::new(false)).await,
                            Transport::Http(transport) => transport.notify(&notification,&limits).await,
                        }
                    }).await;
                }
                if !matches!(error.kind,ErrorKind::Remote { .. }) { self.close().await; }
                Err(error)
            }
        }
    }
    pub async fn list_tools(&mut self, cancel: Arc<AtomicBool>) -> Result<Vec<Tool>> {
        self.catalog.clear();
        self.changed = true;
        let generation=self.catalog_generation;
        let deadline = std::time::Instant::now() + Duration::from_millis(self.config.limits.timeout_ms);
        let mut catalog = BTreeMap::new();
        let mut seen = BTreeSet::new();
        let mut cursor: Option<String> = None;
        let mut total = 0;
        let mut bytes = 0;
        for _ in 0..self.config.limits.max_list_pages {
            let params = cursor.as_ref().map(|cursor|json!({"cursor":cursor})).unwrap_or_else(||json!({}));
            let remaining = deadline.saturating_duration_since(std::time::Instant::now()).as_millis() as u64;
            if remaining == 0 { self.close().await; return Err(Error::new(ErrorKind::Timeout,true)); }
            let result = self.request_with_timeout("tools/list",params,cancel.clone(),remaining).await?;
            let rows = result["tools"].as_array().ok_or_else(||Error::protocol("tools array required"))?;
            total += rows.len();
            bytes += serde_json::to_vec(&result).map_err(|_|Error::protocol("invalid catalog"))?.len();
            if total > self.config.limits.max_tools || bytes > self.config.limits.max_catalog_bytes { return Err(Error::new(ErrorKind::Limit("tool catalog"),true)); }
            for row in rows {
                let tool = Tool::from_value(row)?;
                if !seen.insert(tool.name.clone()) { return Err(Error::protocol("duplicate tool name")); }
                if self.config.allow_tools.contains(&tool.name) { catalog.insert(tool.name.clone(),tool); }
            }
            match result.get("nextCursor") {
                None => {
                    if self.catalog_generation!=generation {return Err(Error::new(ErrorKind::CatalogChanged,true));}
                    self.catalog = catalog;
                    self.changed = false;
                    return Ok(self.catalog.values().cloned().collect());
                }
                Some(value) => {
                    let next = value.as_str().filter(|v|!v.is_empty() && v.len() <= 4096).ok_or_else(||Error::protocol("invalid pagination cursor"))?;
                    if !seen.insert(format!("\0{next}")) { return Err(Error::protocol("repeated pagination cursor")); }
                    cursor = Some(next.into());
                }
            }
        }
        Err(Error::new(ErrorKind::Limit("catalog pages"),true))
    }
    pub async fn call_tool(&mut self, name: &str, arguments: &Value, cancel: Arc<AtomicBool>) -> Result<CallResult> {
        if self.changed { return Err(Error::new(ErrorKind::CatalogChanged,false)); }
        if !self.config.allow_tools.iter().any(|v|v == name) || !self.catalog.contains_key(name) { return Err(Error::new(ErrorKind::ToolNotAllowed,false)); }
        if !self.credentials_current()? {return Err(Error::new(ErrorKind::Credential,false));}
        if !arguments.is_object() { return Err(Error::config("tool arguments must be an object")); }
        let result = self.request("tools/call",json!({"name":name,"arguments":arguments}),cancel).await?;
        CallResult::from_value(result)
    }
    pub async fn close(&mut self) {
        if self.closed { return; }
        self.closed = true;
        self.catalog.clear();
        match &mut self.transport {
            Transport::Stdio(transport) => transport.close(self.config.limits.shutdown_ms).await,
            Transport::Http(transport) => { let _ = tokio::time::timeout(Duration::from_millis(self.config.limits.shutdown_ms),transport.close()).await; },
        }
    }
}

fn bounded_identity(value: &Value) -> Result<String> {
    value.as_str().filter(|v|!v.is_empty() && v.len() <= 128 && !v.chars().any(char::is_control)).map(str::to_owned)
        .ok_or_else(||Error::protocol("invalid server identity"))
}
async fn cancelled(cancel: Arc<AtomicBool>) {
    loop {
        if cancel.load(Ordering::SeqCst) { return; }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}
async fn bounded<T>(future: impl std::future::Future<Output=Result<T>>, timeout_ms: u64, cancel: Arc<AtomicBool>) -> Result<T> {
    tokio::select! {
        biased;
        _ = cancelled(cancel) => Err(Error::new(ErrorKind::Cancelled,false)),
        result = tokio::time::timeout(Duration::from_millis(timeout_ms),future) => result.unwrap_or_else(|_|Err(Error::new(ErrorKind::Timeout,false))),
    }
}
