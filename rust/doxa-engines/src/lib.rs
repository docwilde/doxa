//! Provider-neutral event and capability types. This crate currently contains
//! only a Codex stdout parser; it does not launch or authenticate a CLI.

pub mod codex;
#[cfg(unix)]
pub mod codex_appserver;
#[cfg(unix)]
pub mod codex_driver;

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct EngineEvent {
    #[serde(rename = "type")]
    pub kind: String,
    pub data: Value,
}

impl EngineEvent {
    pub fn new(kind: &str, data: Value) -> Self {
        Self { kind: kind.to_owned(), data }
    }
}

/// A capability is enabled only after its provider implementation is tested.
/// Defaults are intentionally narrower than the Python Codex engine's map.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct EngineCapabilities {
    pub mcp_tools: bool,
    pub hooks: bool,
    pub tool_gate: bool,
    pub permission_modes: bool,
    pub plugins: bool,
    pub resolved_model: bool,
    pub context_window: bool,
    pub token_usage: bool,
    pub cost: bool,
    pub reasoning: bool,
    pub streaming_text: bool,
    pub live_model_switch: bool,
    pub resume: bool,
    pub detachable: bool,
    pub peer_messaging: bool,
    pub peer_send_tool: bool,
    pub spawn_sessions: bool,
    pub lore_pickers: bool,
}
