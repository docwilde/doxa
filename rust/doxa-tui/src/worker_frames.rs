//! Owned terminal-worker messages. JSON is retained only for daemon payloads;
//! `into_legacy_value` is the single adapter for the existing frame reducer.

use serde::Serialize;
use serde_json::{json, Value};

#[derive(Debug)]
pub enum WorkerFrame {
    Daemon { session_id: String, frame: Value },
    SnapshotText { session_id: String, markdown: String },
    Launch { group: usize, result: LaunchResult },
    Attach { session_id: String, group: usize, result: Result<(), String> },
    Notice { session_id: String, message: String },
    PromptFailed { session_id: String, text: String, message: String, delivery: PromptDelivery },
    Command { session_id: String, result: CommandResult },
    Telemetry { session_id: String, reply: Value },
    TelemetryUnavailable { session_id: String },
}

#[derive(Debug)]
pub enum LaunchResult {
    Attached { session_id: String },
    Failed { message: String, started_session: Option<String> },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PromptDelivery { Rejected, Uncertain }

#[derive(Debug, PartialEq, Eq)]
pub struct ReplyStatus { pub ok: bool, pub error: Option<String> }

impl ReplyStatus {
    pub fn from_wire(reply: &Value) -> Self {
        Self { ok: reply["ok"] == true, error: wire_string(reply, "error") }
    }
    pub fn failed(message: impl Into<String>) -> Self {
        Self { ok: false, error: Some(message.into()) }
    }
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct QueueRow { pub id: String, pub preview: String }

#[derive(Debug)]
pub enum CommandResult {
    Models { status: ReplyStatus, models: Option<Value>, note: Option<String>, loading: Option<bool>, capabilities: Option<Value> },
    SetModel { status: ReplyStatus, model: Option<String> },
    SetEffort { status: ReplyStatus, effort: Option<String>, verification_pending: Option<bool> },
    SetPermissionMode { status: ReplyStatus, mode: Option<String> },
    Branch { status: ReplyStatus, base: Option<Value>, branches: Option<Value>, message: Option<String> },
    ContextDetail { status: ReplyStatus, detail: Option<Value> },
    QueueList { status: ReplyStatus, rows: Vec<QueueRow> },
    QueueCancel { status: ReplyStatus, queue_id: String },
    Answer { request_id: String, ok: bool, uncertain: Option<bool>, message: String },
    PeerRoster { status: ReplyStatus, peers: Option<Value> },
    PeerMessage { status: ReplyStatus, uncertain: Option<bool>, draft: String, peer: Option<Value>, delivered_to: Option<Value>, failed: Option<Value>, ledger_error: Option<Value> },
    Stop { status: ReplyStatus, for_clear: bool },
}

pub(crate) fn wire_string(reply: &Value, key: &str) -> Option<String> {
    reply.get(key)?.as_str().map(str::to_owned)
}

pub(crate) fn wire_value(reply: &Value, key: &str) -> Option<Value> { reply.get(key).cloned() }

impl WorkerFrame {
    /// Migration boundary only. Workers and routing queues carry this enum,
    /// never an encoded synthetic JSON envelope.
    pub fn into_legacy_value(self) -> Value {
        match self {
            Self::Daemon { session_id, mut frame } => {
                if let Some(object) = frame.as_object_mut() { object.insert("session_id".into(), json!(session_id)); }
                frame
            }
            Self::SnapshotText { session_id, markdown } => json!({"type":"event", "session_id":session_id,
                "event":{"type":"text_delta", "data":{"text":markdown,"snapshot":true}}}),
            Self::Launch { group, result: LaunchResult::Attached { session_id } } =>
                json!({"type":"launch_reply","ok":true,"session_id":session_id,"group":group}),
            Self::Launch { group, result: LaunchResult::Failed { message, started_session } } => {
                let mut frame = json!({"type":"launch_reply","ok":false,"message":message,"group":group});
                if let Some(id) = started_session { frame["started"] = json!(true); frame["session_id"] = json!(id); }
                frame
            }
            Self::Attach { session_id, group, result } => {
                let mut frame = json!({"type":"attach_reply","session_id":session_id,"group":group,"ok":result.is_ok()});
                if let Err(message) = result { frame["message"] = json!(message); }
                frame
            }
            Self::Notice { session_id, message } => json!({"type":"client_notice","session_id":session_id,"message":message}),
            Self::PromptFailed { session_id, text, message, delivery } => json!({"type":match delivery {
                PromptDelivery::Rejected => "prompt_rejected", PromptDelivery::Uncertain => "prompt_uncertain" },
                "session_id":session_id,"text":text,"message":message}),
            Self::Command { session_id, result } => result.into_legacy_value(session_id),
            Self::Telemetry { session_id, mut reply } => {
                reply["type"] = json!("telemetry_status"); reply["session_id"] = json!(session_id.clone());
                if let Some(status) = reply.get_mut("status").and_then(Value::as_object_mut) {
                    status.insert("session_id".into(), json!(session_id));
                }
                reply
            }
            Self::TelemetryUnavailable { session_id } => json!({"type":"telemetry_unavailable","session_id":session_id}),
        }
    }
}

impl CommandResult {
    fn into_legacy_value(self, session_id: String) -> Value {
        let (kind, status, mut fields) = match self {
            Self::Models { status, models, note, loading, capabilities } => ("models_reply",status,json!({"models":models,"note":note,"loading":loading,"capabilities":capabilities})),
            Self::SetModel { status, model } => ("set_model_reply",status,json!({"model":model})),
            Self::SetEffort { status, effort, verification_pending } => ("set_effort_reply",status,json!({"effort":effort,"verification_pending":verification_pending})),
            Self::SetPermissionMode { status, mode } => ("set_permission_mode_reply",status,json!({"mode":mode})),
            Self::Branch { status, base, branches, message } => ("branch_reply",status,json!({"base":base,"branches":branches,"message":message})),
            Self::ContextDetail { status, detail } => ("context_detail",status,json!({"detail":detail})),
            Self::QueueList { status, rows } => ("queue_list_reply",status,json!({"rows":rows})),
            Self::QueueCancel { status, queue_id } => ("queue_cancel_reply",status,json!({"queue_id":queue_id})),
            Self::Answer { request_id, ok, uncertain, message } => {
                let mut frame = json!({"type":"answer_reply","session_id":session_id,"request_id":request_id,"ok":ok,"message":message});
                if let Some(value) = uncertain { frame["uncertain"] = json!(value); }
                return frame;
            }
            Self::PeerRoster { status, peers } => ("peer_roster",status,json!({"peers":peers})),
            Self::PeerMessage { status, uncertain, draft, peer, delivered_to, failed, ledger_error } => {
                let mut fields = json!({"draft":draft,"peer":peer,"delivered_to":delivered_to,"failed":failed,"ledger_error":ledger_error});
                if let Some(value) = uncertain { fields["uncertain"] = json!(value); }
                ("peer_message_reply",status,fields)
            }
            Self::Stop { status, for_clear } => (if for_clear {"clear_finalize_reply"} else {"stop_reply"},status,json!({})),
        };
        fields["type"] = json!(kind); fields["session_id"] = json!(session_id);
        fields["ok"] = json!(status.ok); fields["error"] = json!(status.error);
        fields
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn launch_adapter_distinguishes_no_launch_from_failed_attach_to_a_live_session() {
        let refused = WorkerFrame::Launch { group: 7, result: LaunchResult::Failed {
            message: "launch refused".into(), started_session: None } }.into_legacy_value();
        assert_eq!(refused, json!({"type":"launch_reply","group":7,"ok":false,"message":"launch refused"}));
        let detached = WorkerFrame::Launch { group: 9, result: LaunchResult::Failed {
            message: "attach failed".into(), started_session: Some("live-owned".into()) } }.into_legacy_value();
        assert_eq!(detached, json!({"type":"launch_reply","group":9,"ok":false,"message":"attach failed","started":true,"session_id":"live-owned"}));
    }

    #[test]
    fn adapter_preserves_request_identity_delivery_uncertainty_and_original_drafts() {
        let answer = WorkerFrame::Command { session_id: "background".into(), result: CommandResult::Answer {
            request_id: "request-2".into(), ok: false, uncertain: Some(true), message: "reply lost".into() } }.into_legacy_value();
        assert_eq!(answer, json!({"type":"answer_reply","session_id":"background","request_id":"request-2","ok":false,"uncertain":true,"message":"reply lost"}));
        let prompt = WorkerFrame::PromptFailed { session_id: "original".into(), text: "original draft".into(),
            message: "delivery unknown".into(), delivery: PromptDelivery::Uncertain }.into_legacy_value();
        assert_eq!(prompt, json!({"type":"prompt_uncertain","session_id":"original","text":"original draft","message":"delivery unknown"}));
    }

    #[test]
    fn adapter_rebinds_only_the_owner_of_daemon_payloads_and_cached_telemetry() {
        let frame = WorkerFrame::Daemon { session_id: "socket-owner".into(), frame: json!({"type":"event",
            "session_id":"forged","seq":42,"event":{"type":"text_delta","data":{"text":"fixture"}}}) }.into_legacy_value();
        assert_eq!(frame["session_id"], "socket-owner"); assert_eq!(frame["seq"], 42);
        assert_eq!(frame["event"]["data"]["text"], "fixture");
        let telemetry = WorkerFrame::Telemetry { session_id: "other-owner".into(), reply: json!({"type":"reply",
            "id":7,"ok":true,"status":{"session_id":"forged","running":true,"model":"fixture-model"}}) }.into_legacy_value();
        assert_eq!(telemetry["type"], "telemetry_status"); assert_eq!(telemetry["id"], 7);
        assert_eq!(telemetry["status"]["session_id"], "other-owner");
        assert_eq!(telemetry["status"]["running"], true);
    }
}
