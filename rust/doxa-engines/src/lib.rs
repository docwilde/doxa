//! Provider-neutral event and capability types. This crate currently contains
//! only a Codex stdout parser; it does not launch or authenticate a CLI.

pub mod codex;
pub mod codex_interaction;
pub mod peer_tools;
pub mod session_tools;
#[cfg(unix)]
pub mod codex_appserver;
#[cfg(unix)]
pub mod provider_owner;
#[cfg(unix)]
pub mod codex_compact;
pub mod review_worker;
pub mod compact_hook;
pub mod model_registry;
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

impl EngineCapabilities {
    /// Decode the control assertions made by a connected runtime. Provider
    /// names and current model/effort values never imply a capability.
    pub fn from_session_controls(status: &Value) -> Self {
        let mut capabilities = Self::default();
        capabilities.update_session_controls(status);
        capabilities
    }

    /// Status replies may be partial. Omitted fields retain their last runtime
    /// assertion; present malformed fields revoke the corresponding control.
    pub fn update_session_controls(&mut self, status: &Value) {
        if let Some(value) = status.get("can_set_model") {
            self.live_model_switch = value.as_bool() == Some(true);
        }
        if let Some(value) = status.get("can_set_permission_mode") {
            self.permission_modes = value.as_bool() == Some(true);
        }
    }
}

/// Verified per-model effort metadata from a connected provider's catalog.
/// Empty/unknown metadata authorizes no effort choices. Transport adapters keep
/// their own provider schemas and convert them to this bounded runtime shape.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ModelCatalog {
    models: Vec<ModelCapabilities>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ModelCapabilities {
    pub model: String,
    pub efforts: Vec<String>,
}

impl ModelCatalog {
    pub fn decode(capabilities: &Value) -> Self {
        let mut models: Vec<ModelCapabilities> = Vec::new();
        for row in capabilities.as_array().into_iter().flatten().take(100) {
            let Some(model) = row["model"].as_str().filter(|model|
                !model.is_empty() && model.len() <= 128 && !model.chars().any(char::is_control)) else { continue; };
            let mut efforts = Vec::new();
            for level in row["efforts"].as_array().into_iter().flatten().take(16).filter_map(Value::as_str) {
                if !level.is_empty() && level.len() <= 32 && level.bytes().all(|byte| byte.is_ascii_alphanumeric())
                    && !efforts.iter().any(|seen| seen == level) {
                    efforts.push(level.to_owned());
                }
            }
            // Conflicting duplicate model rows cannot broaden an assertion.
            if let Some(existing) = models.iter_mut().find(|entry| entry.model == model) {
                existing.efforts.retain(|level| efforts.contains(level));
            } else { models.push(ModelCapabilities { model: model.to_owned(), efforts }); }
        }
        Self { models }
    }

    pub fn efforts(&self, model: &str) -> &[String] {
        self.models.iter().find(|entry| entry.model == model).map(|entry| entry.efforts.as_slice()).unwrap_or(&[])
    }
}

#[cfg(test)]
mod capability_tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn runtime_control_assertions_are_partial_explicit_and_fail_closed() {
        let mut capabilities = EngineCapabilities::from_session_controls(&json!({"engine":"claude","model":"sonnet","effort":"high"}));
        assert!(!capabilities.live_model_switch && !capabilities.permission_modes);
        capabilities.update_session_controls(&json!({"can_set_model":true,"can_set_permission_mode":true}));
        capabilities.update_session_controls(&json!({"model":"opus","can_set_model":"true"}));
        assert!(!capabilities.live_model_switch && capabilities.permission_modes);
        capabilities.update_session_controls(&json!({"can_set_permission_mode":null}));
        assert!(!capabilities.permission_modes);
    }

    #[test]
    fn model_catalog_requires_safe_metadata_and_conflicting_rows_do_not_broaden() {
        let catalog = ModelCatalog::decode(&json!([
            {"model":"known","efforts":["low","high","bad\nlevel","high"]},
            {"model":"known","efforts":["high","max"]},
            {"model":"empty","efforts":"high"},
            {"model":"bad\nmodel","efforts":["high"]}]));
        assert_eq!(catalog.efforts("known"), ["high"]);
        assert!(catalog.efforts("empty").is_empty());
        assert!(catalog.efforts("unknown").is_empty());
        assert!(catalog.efforts("bad\nmodel").is_empty());
        assert!(ModelCatalog::decode(&Value::Null).efforts("known").is_empty());
    }

    #[test]
    fn model_catalog_bounds_rows_and_levels_before_allocation() {
        let levels = (0..20).map(|index| format!("level{index}")).collect::<Vec<_>>();
        let rows = (0..110).map(|index| json!({"model":format!("model{index}"),"efforts":levels})).collect::<Vec<_>>();
        let catalog = ModelCatalog::decode(&json!(rows));
        assert_eq!(catalog.models.len(), 100);
        assert_eq!(catalog.efforts("model99").len(), 16);
        assert!(catalog.efforts("model100").is_empty());
    }
}
