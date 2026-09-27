//! Frozen host identity and the canonical Python operator catalog. This process
//! cannot offer daemon controls, spawning, or direct curated-memory mutations.
use doxa_lore::LoreClient;
use serde_json::{json,Value};
use std::{path::Path, sync::{Arc,Mutex}, time::Duration};

pub struct AgentTools { client: Mutex<Option<LoreClient>>, identity: Value, definitions: Vec<Value> }
impl AgentTools {
    pub fn new(python: &Path, cwd: &str, session_id: &str, engine: &str, enabled: bool) -> Option<Arc<Self>> {
        if !enabled { return None; }
        let identity = json!({"cwd":cwd,"session_id":session_id,"source_engine":engine,"spawn_depth":0,"lore":true});
        let mut client = LoreClient::spawn_agent(python, Duration::from_secs(5)).ok()?;
        let rows = client.agent_catalog(&identity).ok()?;
        if rows.is_empty() { return None; }
        let definitions = rows.into_iter().map(|row| json!({"type":"function",
            "name":format!("mcp__doxa__{}",row["name"].as_str().unwrap()),
            "description":row["description"],"inputSchema":row["inputSchema"]})).collect();
        Some(Arc::new(Self { client:Mutex::new(Some(client)), identity, definitions }))
    }
    pub fn callback_definitions(&self) -> Vec<Value> { self.definitions.clone() }
    pub fn definitions(&self) -> Vec<Value> {
        self.client.lock().ok().and_then(|mut client| client.as_mut()?.agent_catalog(&self.identity).ok())
            .map(|rows| rows.into_iter().map(|row| json!({"type":"function",
                "name":format!("mcp__doxa__{}",row["name"].as_str().unwrap()),
                "description":row["description"],"inputSchema":row["inputSchema"]})).collect()).unwrap_or_default()
    }
    pub fn vendor_definitions(&self) -> Vec<Value> {
        self.definitions().iter().map(|row| json!({"type":"function","function":{
            "name":row["name"].as_str().unwrap().strip_prefix("mcp__doxa__").unwrap(),
            "description":row["description"],"parameters":row["inputSchema"]}})).collect()
    }
    pub fn vendor_handler(self: &Arc<Self>) -> doxa_runtime::PeerToolHandler {
        let handler = self.handler(); Arc::new(move |name, args| handler(&format!("mcp__doxa__{name}"), args))
    }
    pub fn contains(&self, name: &str) -> bool { self.definitions.iter().any(|row| row["name"] == name) }
    pub fn call(&self, name: &str, arguments: &Value) -> Result<Value,String> {
        if !self.contains(name) { return Err("Unavailable LORE agent tool".into()); }
        let mut client = self.client.lock().map_err(|_|"LORE agent tools unavailable")?;
        client.as_mut().ok_or("LORE session is closed")?.agent_call(&self.identity, name.strip_prefix("mcp__doxa__").ok_or("Invalid LORE agent tool")?,arguments)
            .map_err(|_|"LORE agent tool failed".into())
    }
    pub fn close(&self) { if let Ok(mut client) = self.client.lock() { client.take(); } }
    pub fn handler(self: &Arc<Self>) -> doxa_runtime::PeerToolHandler {
        let weak = Arc::downgrade(self);
        Arc::new(move |name, arguments| weak.upgrade().ok_or("LORE session is closed")?.call(name, arguments))
    }
}

/// Canonical stdio MCP registration for the legacy exec transport. Values are
/// non-secret host identity or the same path/switch allowlist as Python Codex.
/// No native engine control socket exists for this transport, so peer_send is
/// absent and cannot be activated by inherited model configuration.
pub fn mcp_overrides(python: &Path, cwd: &str, session_id: &str, enabled: bool) -> Vec<String> {
    let quote = |value: &str| serde_json::to_string(value).unwrap();
    let mut values = vec![
        format!("mcp_servers.doxa.command={}", quote(&python.to_string_lossy())),
        "mcp_servers.doxa.args=[\"-m\",\"doxa.mcpserver\"]".into(),
        "mcp_servers.doxa.default_tools_approval_mode=\"approve\"".into(),
        "mcp_servers.doxa.enabled=true".into(),
    ];
    for (name, value) in [("DOXA_MCP_SESSION_ID", session_id), ("DOXA_MCP_CWD",cwd),
        ("DOXA_MCP_ENGINE","codex"), ("DOXA_MCP_SPAWN_DEPTH","0"),
        ("DOXA_MCP_LORE",if enabled {"1"} else {"0"}), ("DOXA_MCP_PEER_SEND","0")] {
        values.push(format!("mcp_servers.doxa.env.{name}={}",quote(value)));
    }
    for name in ["HOME","PATH","PYTHONPATH","LORE_ROOT","LORE_PROJECTS_DIR","DOXA_HOME",
        "DOXA_RUNTIME_DIR","DOXA_LORE_CORE_PATH","DOXA_LORE_SOURCE"] {
        if let Ok(value) = std::env::var(name) { if !value.is_empty() {
            values.push(format!("mcp_servers.doxa.env.{name}={}",quote(&value)));
        } }
    }
    values
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exec_mcp_overrides_freeze_identity_and_memory_off_without_secrets() {
        let options = mcp_overrides(Path::new("/python with spaces"), "/workspace", "fixed-host", false);
        assert!(options.contains(&"mcp_servers.doxa.command=\"/python with spaces\"".into()));
        assert!(options.contains(&"mcp_servers.doxa.env.DOXA_MCP_LORE=\"0\"".into()));
        assert!(options.contains(&"mcp_servers.doxa.env.DOXA_MCP_PEER_SEND=\"0\"".into()));
        assert!(options.contains(&"mcp_servers.doxa.env.DOXA_MCP_SESSION_ID=\"fixed-host\"".into()));
        assert!(!options.iter().any(|value| value.contains("API_KEY") || value.contains("TOKEN") || value.contains("ENGINE_SOCKET")));
    }
}
