//! Provider-neutral definition for the host-owned human-reviewed session operator.
use serde_json::{json, Value};
pub const SPAWN: &str = "mcp__doxa__spawn_session";
pub fn definitions() -> Vec<Value> {
    vec![
        json!({"type":"function","name":SPAWN,"description":"Start a second DOXA session in this same repository with an exact human-reviewed task. The child has its own provider process, worktree, spend and transcript. Every spawn asks the human and enforces depth, live-session and rate caps. The returned session exists; its work is not complete and no automatic result delivery is promised.","inputSchema":{"type":"object","properties":{"task":{"type":"string","maxLength":2000},"model":{"type":"string"},"base_branch":{"type":"string"}},"required":["task"],"additionalProperties":false}}),
    ]
}
pub fn validate(arguments: &Value) -> Result<(), String> {
    let args = arguments
        .as_object()
        .ok_or("Session tool arguments must be an object")?;
    if args
        .keys()
        .any(|key| !matches!(key.as_str(), "task" | "model" | "base_branch"))
        || args.get("task").and_then(Value::as_str).is_none()
    {
        return Err("Unsupported session tool arguments".into());
    }
    for key in ["model", "base_branch"] {
        if args.get(key).is_some_and(|v| {
            v.as_str().is_none_or(|s| {
                s.is_empty()
                    || s.len() > 200
                    || s.starts_with('-')
                    || s.chars().any(char::is_control)
            })
        }) {
            return Err(format!("Invalid child {key}"));
        }
    }
    Ok(())
}
