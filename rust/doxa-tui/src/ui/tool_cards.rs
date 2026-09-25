//! Bounded, session-local tool activity for the terminal detail view.
//!
//! The daemon sends already scrubbed display events. This layer also strips
//! terminal controls and bounds retained text; it never writes tool input or
//! results to disk. A replayed call/result updates its existing card by ID.

use std::collections::HashMap;

use serde_json::Value;

use crate::markdown;

const MAX_CARDS_PER_SESSION: usize = 64;
const MAX_SESSIONS: usize = 64;
const MAX_DETAIL_CHARS: usize = 4096;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct ToolCard {
    id: String,
    pub name: String,
    pub input: Option<String>,
    pub result: Option<String>,
    pub failed: bool,
    pub duration_ms: Option<u64>,
    pub parent_id: Option<String>,
}

impl ToolCard {
    pub fn status(&self) -> String {
        match (&self.result, self.failed, self.duration_ms) {
            (None, _, _) => "running".into(),
            (Some(_), true, Some(ms)) => format!("failed · {ms} ms"),
            (Some(_), true, None) => "failed".into(),
            (Some(_), false, Some(ms)) => format!("finished · {ms} ms"),
            (Some(_), false, None) => "finished".into(),
        }
    }
}

#[derive(Clone, Debug, Default)]
pub(super) struct ToolCards {
    by_session: HashMap<String, Vec<ToolCard>>,
}

impl ToolCards {
    pub fn for_session(&self, session_id: &str) -> &[ToolCard] {
        self.by_session
            .get(session_id)
            .map(Vec::as_slice)
            .unwrap_or(&[])
    }

    /// Records Python DOXA's common Claude, Codex, and vendor tool event shape.
    /// Malformed IDs cannot create unbounded or unmatchable cards.
    pub fn record(&mut self, session_id: &str, kind: &str, data: &Value) -> bool {
        if !matches!(kind, "tool_call" | "tool_result") || session_id.is_empty() {
            return false;
        }
        let Some(id) = data
            .get("id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty() && id.len() <= 200 && !id.chars().any(char::is_control))
        else {
            return false;
        };
        if !self.by_session.contains_key(session_id) && self.by_session.len() >= MAX_SESSIONS {
            return false;
        }
        let cards = self.by_session.entry(session_id.to_owned()).or_default();
        let index = cards.iter().position(|card| card.id == id);
        if index.is_none() {
            if cards.len() == MAX_CARDS_PER_SESSION {
                cards.remove(0);
            }
            cards.push(ToolCard {
                id: id.to_owned(),
                name: "Tool".into(),
                input: None,
                result: None,
                failed: false,
                duration_ms: None,
                parent_id: None,
            });
        }
        let selected = index.unwrap_or(cards.len() - 1);
        let card = &mut cards[selected];
        if let Some(name) = data
            .get("name")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            card.name = clean_label(name, 120);
        }
        if let Some(parent) = data
            .get("parent_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            card.parent_id = Some(clean_label(parent, 120));
        }
        match kind {
            "tool_call" => {
                if let Some(input) = data.get("input").filter(|value| !value.is_null()) {
                    let display = if let Some(text) = input.as_str() {
                        text.to_owned()
                    } else {
                        serde_json::to_string_pretty(input).unwrap_or_default()
                    };
                    card.input = Some(clean(&display, MAX_DETAIL_CHARS));
                }
            }
            "tool_result" => {
                card.result = Some(clean(
                    data.get("result_summary")
                        .and_then(Value::as_str)
                        .unwrap_or("(no summary)"),
                    MAX_DETAIL_CHARS,
                ));
                card.failed = data.get("is_error").and_then(Value::as_bool) == Some(true);
                card.duration_ms = data.get("duration_ms").and_then(Value::as_u64);
            }
            _ => unreachable!(),
        }
        true
    }
}

fn clean(value: &str, limit: usize) -> String {
    let sanitized = markdown::sanitize(value);
    let mut chars = sanitized.chars();
    let mut result: String = chars.by_ref().take(limit).collect();
    if chars.next().is_some() {
        result.push('…');
    }
    result
}

fn clean_label(value: &str, limit: usize) -> String {
    clean(value, limit).replace(['\n', '\r'], " ")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn matches_replayed_calls_and_out_of_order_results_by_id() {
        let mut cards = ToolCards::default();
        assert!(cards.record(
            "s",
            "tool_result",
            &json!({"id":"one", "name":"Read",
            "result_summary":"done", "duration_ms":12})
        ));
        assert!(cards.record(
            "s",
            "tool_call",
            &json!({"id":"one", "name":"Read",
            "input":{"file_path":"a.rs"}})
        ));
        assert!(cards.record(
            "s",
            "tool_result",
            &json!({"id":"one", "name":"Read",
            "result_summary":"done", "duration_ms":12})
        ));
        assert_eq!(cards.for_session("s").len(), 1);
        let card = &cards.for_session("s")[0];
        assert!(card.input.as_ref().unwrap().contains("a.rs"));
        assert_eq!(card.result.as_deref(), Some("done"));
        assert_eq!(card.status(), "finished · 12 ms");
    }

    #[test]
    fn session_isolation_and_bounded_retention() {
        let mut cards = ToolCards::default();
        for i in 0..70 {
            assert!(cards.record(
                "a",
                "tool_call",
                &json!({"id":i.to_string(), "name":"Tool"})
            ));
        }
        cards.record("b", "tool_call", &json!({"id":"other", "name":"Other"}));
        assert_eq!(cards.for_session("a").len(), MAX_CARDS_PER_SESSION);
        assert_eq!(cards.for_session("a")[0].id, "6");
        assert_eq!(cards.for_session("b").len(), 1);
    }

    #[test]
    fn strips_terminal_controls_and_limits_detail() {
        let mut cards = ToolCards::default();
        cards.record(
            "s",
            "tool_call",
            &json!({"id":"one", "name":"\u{1b}[31mRead",
            "input":"secret\u{1b}[2J".repeat(1000)}),
        );
        let card = &cards.for_session("s")[0];
        assert!(!card.name.contains('\u{1b}'));
        assert!(!card.input.as_ref().unwrap().contains('\u{1b}'));
        assert!(card.input.as_ref().unwrap().chars().count() <= MAX_DETAIL_CHARS + 1);
        assert!(!cards.record("s", "tool_call", &json!({"id":"\n", "name":"bad"})));
    }
}
