//! Session control replies are reduced independently of focus and presentation.
//! Only the owning App applies the transition and decides whether to show it.
use serde_json::Value;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct SessionOwner<'a>(&'a str);

impl<'a> SessionOwner<'a> {
    pub(super) fn decode(frame: &'a Value) -> Option<Self> {
        frame["session_id"].as_str().filter(|id| crate::discovery::valid_id(id)).map(Self)
    }

    pub(super) fn id(self) -> &'a str { self.0 }
    pub(super) fn is_active(self, active: Option<&str>) -> bool { active == Some(self.0) }
}

#[derive(Clone, Copy, Debug)]
enum Control { Model, Permission, Effort }

#[derive(Clone, Copy, Debug)]
pub(super) struct ControlReply<'a> {
    pub(super) owner: SessionOwner<'a>,
    control: Control,
    ok: bool,
    value: Option<&'a str>,
    error: Option<&'a str>,
    verified: bool,
}

#[derive(Debug, PartialEq, Eq)]
pub(super) enum EffortTransition { Unchanged, Failed, Verified(String) }

#[derive(Debug, PartialEq, Eq)]
pub(super) struct ControlTransition {
    pub(super) notice: String,
    pub(super) effort: EffortTransition,
}

impl<'a> ControlReply<'a> {
    pub(super) fn decode(frame: &'a Value) -> Option<Self> {
        let (control, field) = match frame["type"].as_str()? {
            "set_model_reply" => (Control::Model, "model"),
            "set_permission_mode_reply" => (Control::Permission, "mode"),
            "set_effort_reply" => (Control::Effort, "effort"),
            _ => return None,
        };
        Some(Self { owner: SessionOwner::decode(frame)?, control,
            ok: frame["ok"].as_bool()?, value: frame[field].as_str(),
            error: frame["error"].as_str(), verified: frame["verification_pending"] == false })
    }

    /// Effort replies may act only on the outstanding exact request. Success
    /// without provider verification keeps its prior verified value unchanged.
    pub(super) fn reduce(self, pending_effort: Option<&str>) -> Option<ControlTransition> {
        let mut effort = EffortTransition::Unchanged;
        let notice = match self.control {
            Control::Model | Control::Permission => {
                let (selected, failed) = match self.control {
                    Control::Model => ("Model selected", "Model change failed"),
                    _ => ("Permission mode selected", "Permission change failed"),
                };
                if self.ok { format!("{selected} · {}", super::safe_label(self.value.unwrap_or("awaiting event"))) }
                else { format!("{failed} · {}", super::safe_label(self.error.unwrap_or("unknown error"))) }
            }
            Control::Effort => {
                let requested = pending_effort?;
                if !self.ok {
                    effort = EffortTransition::Failed;
                    format!("Effort change failed · {}", super::safe_label(self.error.unwrap_or("unknown error")))
                } else {
                    if self.value != Some(requested) { return None; }
                    if self.verified {
                        effort = EffortTransition::Verified(requested.to_owned());
                        format!("Effort verified · {requested}")
                    } else { format!("Requested effort {requested} · awaiting provider verification") }
                }
            }
        };
        Some(ControlTransition { notice, effort })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn control_reply_requires_explicit_owner_and_boolean_outcome() {
        for frame in [json!({"type":"set_model_reply","ok":true}),
            json!({"type":"set_model_reply","session_id":"../other","ok":true}),
            json!({"type":"set_model_reply","session_id":"s","ok":"true"})] {
            assert!(ControlReply::decode(&frame).is_none());
        }
    }

    #[test]
    fn effort_reducer_requires_exact_pending_request_and_explicit_verification() {
        let mut frame = json!({"type":"set_effort_reply","session_id":"s","ok":true,"effort":"low"});
        assert!(ControlReply::decode(&frame).unwrap().reduce(None).is_none());
        assert!(ControlReply::decode(&frame).unwrap().reduce(Some("high")).is_none());
        assert_eq!(ControlReply::decode(&frame).unwrap().reduce(Some("low")).unwrap().effort, EffortTransition::Unchanged);
        frame["verification_pending"] = json!(false);
        assert_eq!(ControlReply::decode(&frame).unwrap().reduce(Some("low")).unwrap().effort, EffortTransition::Verified("low".into()));
        frame["ok"] = json!(false);
        assert_eq!(ControlReply::decode(&frame).unwrap().reduce(Some("low")).unwrap().effort, EffortTransition::Failed);
    }
}
