//! Provider-reported session usage, context and billing display state.

use super::safe_label;

/// Effective turn identity. Never replaces the session's Auto/target selection.
#[derive(Clone, Debug, PartialEq)]
pub(super) struct RoutingStatus {
    target_id: String,
    engine: String,
    model: String,
    effort: String,
    route_mode: String,
    fallback_reason: Option<String>,
    latency_ms: u64,
    cost_usd: Option<f64>,
}

impl RoutingStatus {
    pub(super) fn from_value(data: &serde_json::Value) -> Option<Self> {
        fn identifier(data: &serde_json::Value, key: &str, limit: usize) -> Option<String> {
            let value = data[key].as_str()?;
            (!value.is_empty() && value.len() <= limit
                && value.bytes().all(|c| c.is_ascii_alphanumeric() || b"-_.:".contains(&c)))
                .then(|| value.to_owned())
        }
        let engine = data["engine"].as_str().filter(|v| matches!(*v, "deepseek" | "glm"))?;
        let effort = data["effort"].as_str().filter(|v| matches!(*v, "none" | "low" | "medium" | "high" | "xhigh" | "max"))?;
        let mode = data["route_mode"].as_str().filter(|v| matches!(*v, "auto" | "pinned"))?;
        let reason = match data.get("fallback_reason") {
            None | Some(serde_json::Value::Null) => None,
            Some(value) => Some(value.as_str().filter(|v| matches!(*v,
                "selected" | "low_confidence" | "unavailable" | "invalid_response" | "budget"
                | "cancelled" | "single_eligible" | "accounting_unknown"))?.to_owned()),
        };
        let cost = match data.get("cost_usd") {
            None | Some(serde_json::Value::Null) => None,
            Some(value) => Some(value.as_f64().filter(|v| v.is_finite() && *v >= 0.0)?),
        };
        Some(Self {
            target_id: identifier(data, "target_id", 48)?,
            engine: engine.into(), model: identifier(data, "model", 128)?, effort: effort.into(),
            route_mode: mode.into(), fallback_reason: reason,
            latency_ms: data["latency_ms"].as_u64()?, cost_usd: cost,
        })
    }

    pub(super) fn label(&self) -> String {
        format!("{} · {}/{} · {}", self.target_id, self.engine, self.model, self.effort)
    }

    pub(super) fn summary(&self) -> String {
        let reason = self.fallback_reason.as_deref().unwrap_or("selected");
        let cost = self.cost_usd.map(|v| format!("${v:.6} est")).unwrap_or_else(|| "unknown cost".into());
        format!("Route {} · {} · {reason} · {} ms · {cost}", self.route_mode, self.label(), self.latency_ms)
    }

    pub(super) fn lines(&self) -> Vec<String> {
        vec![self.summary(), "Cost covers this routing decision; session cost includes worker calls.".into(),
            "Auto/target selection is controlled by /model; effort is configured per target.".into()]
    }
}

#[derive(Clone, Debug, Default)]
pub(super) struct SessionTelemetry {
    pub(super) routing: Option<RoutingStatus>,
    pub(super) isolation: Option<serde_json::Value>,
    pub(super) account: Option<serde_json::Value>,
    pub(super) context: Option<String>,
    pub(super) context_percent: Option<f64>,
    pub(super) context_tokens: Option<u64>,
    pub(super) context_limit: Option<u64>,
    pub(super) turns: Option<u64>,
    pub(super) input_tokens: Option<u64>,
    pub(super) output_tokens: Option<u64>,
    pub(super) cache_read_tokens: Option<u64>,
    pub(super) cache_write_tokens: Option<u64>,
    pub(super) session_cost: Option<String>,
    pub(super) cost: Option<String>,
    pub(super) billing_mode: Option<String>,
    pub(super) subscription_type: Option<String>,
    pub(super) quota: Option<String>,
    pub(super) balance: Option<String>,
    pub(super) lore: Option<String>,
}

impl SessionTelemetry {
    /// The most consumed reported window determines the subscription warning.
    /// Unknown quotas and API billing retain the ordinary chip style.
    pub(super) fn quota_color(&self) -> Option<ratatui::style::Color> {
        if self.billing_mode.as_deref() != Some("subscription") {
            return None;
        }
        let quota = self.quota.as_deref()?;
        let highest = quota
            .match_indices('%')
            .filter_map(|(end, _)| {
                let start = quota[..end]
                    .char_indices()
                    .rev()
                    .find(|(_, c)| !c.is_ascii_digit() && *c != '.')
                    .map(|(i, c)| i + c.len_utf8())
                    .unwrap_or(0);
                if quota[..start].ends_with('-') {
                    return None;
                }
                quota[start..end]
                    .parse::<f64>()
                    .ok()
                    .filter(|v| v.is_finite() && (0.0..=100.0).contains(v))
            })
            .reduce(f64::max)?;
        Some(if highest > 90.0 {
            super::theme::ERROR
        } else if highest > 66.0 {
            super::theme::WARNING
        } else {
            super::theme::SUCCESS
        })
    }

    pub(super) fn update_turn(&mut self, data: &serde_json::Value) {
        if let Some(routing) = data.get("routing") { self.routing = RoutingStatus::from_value(routing); }
        let context = data["ctx_percentage"]
            .as_f64()
            .filter(|value| value.is_finite() && (0.0..=100.0).contains(value))
            .map(|value| format!("{value:.0}%"));
        let absolute = data["ctx_tokens"]
            .as_u64()
            .zip(data["ctx_max_tokens"].as_u64())
            .filter(|(used, limit)| *limit > 0 && used <= limit)
            .map(|(used, limit)| format!("{used}/{limit}"));
        if data.get("ctx_percentage").is_some() || data.get("ctx_tokens").is_some() {
            self.context = context.or(absolute);
            self.context_percent = data["ctx_percentage"]
                .as_f64()
                .filter(|value| value.is_finite() && (0.0..=100.0).contains(value));
            self.context_tokens = data["ctx_tokens"].as_u64();
            self.context_limit = data["ctx_max_tokens"].as_u64().filter(|limit| *limit > 0);
        }
        if data["usage_scope"] == "session" {
            self.turns = data["num_turns"].as_u64().or(self.turns);
            self.input_tokens = data["input_tokens"].as_u64().or(self.input_tokens);
            self.output_tokens = data["output_tokens"].as_u64().or(self.output_tokens);
            self.cache_read_tokens = data["cache_read_input_tokens"]
                .as_u64()
                .or(self.cache_read_tokens);
        }
        if let Some(cost) = data["session_cost_usd"]
            .as_f64()
            .filter(|value| value.is_finite() && *value >= 0.0)
        {
            self.cost = Some(format!("${cost:.4}{}", if data["cost_is_estimate"] == true { " est" } else { "" }));
        } else if let Some(cost) = data["cost_usd"]
            .as_f64()
            .filter(|value| value.is_finite() && *value >= 0.0)
        {
            self.cost = Some(format!("${cost:.4} turn{}", if data["cost_is_estimate"] == true { " est" } else { "" }));
        } else if data.get("session_cost_usd").is_some() || data.get("cost_usd").is_some() {
            self.cost = None;
        }
        if data.get("session_cost_usd").is_some() {
            self.session_cost = data["session_cost_usd"]
                .as_f64()
                .filter(|value| value.is_finite() && *value >= 0.0)
                .map(|cost| format!("${cost:.4}{}", if data["cost_is_estimate"] == true { " est" } else { "" }));
        }
    }

    pub(super) fn update_billing(&mut self, billing: &serde_json::Value) {
        if let Some(routing) = billing.get("routing") { self.routing = RoutingStatus::from_value(routing); }
        self.billing_mode = match billing["mode"].as_str() {
            Some("api") => Some("api".into()),
            Some("subscription") => Some("subscription".into()),
            _ => None,
        };
        self.subscription_type = billing["type"]
            .as_str()
            .filter(|name| {
                !name.is_empty() && name.len() <= 64 && !name.chars().any(char::is_control)
            })
            .map(safe_label);
        self.quota = billing["quota"]
            .as_str()
            .filter(|quota| {
                !quota.is_empty() && quota.len() <= 120 && !quota.chars().any(char::is_control)
            })
            .map(safe_label);
        self.balance = billing["balance"]
            .as_str()
            .filter(|balance| {
                !balance.is_empty() && balance.len() <= 80 && !balance.chars().any(char::is_control)
            })
            .map(safe_label);
    }

    pub(super) fn update_status(&mut self, status: &serde_json::Value) {
        if let Some(routing) = status.get("routing") { self.routing = RoutingStatus::from_value(routing); }
        if let Some(value)=status.get("isolation") {
            self.isolation=super::isolation_controls::verified_status(value);
        }
        if let Some(account) = status.get("account") {
            let mut fields = serde_json::Map::new();
            for key in ["email", "organization", "subscriptionType", "apiProvider"] {
                if let Some(text) = account[key].as_str().filter(|text| {
                    !text.trim().is_empty()
                        && text.len() <= 256
                        && !text.chars().any(char::is_control)
                }) {
                    fields.insert(
                        key.into(),
                        serde_json::Value::String(safe_label(text.trim())),
                    );
                }
            }
            self.account = (!fields.is_empty()).then_some(serde_json::Value::Object(fields));
        }
        if let Some(billing) = status.get("billing") {
            self.update_billing(billing);
        }
        let context = status["ctx_percentage"]
            .as_f64()
            .filter(|value| value.is_finite() && (0.0..=100.0).contains(value))
            .map(|value| format!("{value:.0}%"));
        let absolute = status["ctx_tokens"]
            .as_u64()
            .zip(status["ctx_max_tokens"].as_u64())
            .filter(|(used, limit)| *limit > 0 && used <= limit)
            .map(|(used, limit)| format!("{used}/{limit}"));
        if status.get("ctx_percentage").is_some() || status.get("ctx_tokens").is_some() {
            self.context = context.or(absolute);
            self.context_percent = status["ctx_percentage"]
                .as_f64()
                .filter(|value| value.is_finite() && (0.0..=100.0).contains(value));
            self.context_tokens = status["ctx_tokens"].as_u64();
            self.context_limit = status["ctx_max_tokens"].as_u64().filter(|limit| *limit > 0);
        }
        if let Some(usage) = status.get("usage") {
            self.turns = usage["num_turns"].as_u64();
            self.input_tokens = usage["input_tokens"].as_u64();
            self.output_tokens = usage["output_tokens"].as_u64();
            self.cache_read_tokens = usage["cache_read_input_tokens"].as_u64();
            self.cache_write_tokens = usage["cache_creation_input_tokens"].as_u64();
        }
        if let Some(cost) = status["total_cost_usd"]
            .as_f64()
            .filter(|value| value.is_finite() && *value >= 0.0)
        {
            let label = if status["usage"]["cost_basis"].as_str().is_some() {
                if status["usage"]["unpriced_models"]
                    .as_array()
                    .is_some_and(|models| !models.is_empty())
                {
                    "est partial"
                } else {
                    "est"
                }
            } else {
                ""
            };
            self.cost = Some(format!("${cost:.4} {label}").trim_end().to_owned());
            self.session_cost = self.cost.clone();
        } else if status.get("total_cost_usd").is_some() {
            self.cost = None;
            self.session_cost = None;
        }
        if let Some(count) = status["belief_count"].as_u64() {
            self.lore = Some(format!("{count} beliefs"));
        } else if status.get("lore_scrub").is_some() {
            self.lore = match status["lore_scrub"].as_str() {
                Some("ready") => Some("scrub ready".into()),
                Some("unavailable") => Some("scrub unavailable".into()),
                _ => None,
            };
        }
    }

    pub(super) fn billing_label(&self, engine: Option<&str>) -> Option<String> {
        match engine {
            Some("deepseek" | "glm" | "router") => Some(self.cost.clone().unwrap_or_else(|| "$?".into())),
            Some("codex" | "claude") => match self.billing_mode.as_deref() {
                Some("api") => Some(self.cost.clone().unwrap_or_else(|| "$?".into())),
                Some("subscription") => {
                    let tier = self
                        .subscription_type
                        .as_deref()
                        .filter(|tier| *tier != "subscription");
                    if tier.is_none() && self.quota.is_none() {
                        return None;
                    }
                    Some(match (tier, self.quota.as_deref()) {
                        (Some(tier), Some(quota)) => format!("{tier} · {quota}"),
                        (Some(tier), None) => tier.to_owned(),
                        (None, Some(quota)) => quota.to_owned(),
                        (None, None) => return None,
                    })
                }
                _ => None,
            },
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn router_restore_uses_bounded_identity_and_unknown_cost_stays_unknown() {
        let mut telemetry=SessionTelemetry::default();
        let value=json!({"target_id":"ds-fixture","engine":"deepseek","model":"deepseek-flash","effort":"high",
            "route_mode":"pinned","fallback_reason":null,"latency_ms":0,"cost_usd":null,"private":"hidden"});
        telemetry.update_status(&json!({"routing":value}));
        let status=telemetry.routing.as_ref().unwrap();
        assert!(status.summary().contains("unknown cost"));
        assert!(status.summary().contains("pinned"));
        assert_eq!(telemetry.billing_label(Some("router")).as_deref(),Some("$?"));
        for (key,bad) in [("fallback_reason",json!("provider secret error")),("engine",json!("claude")),("target_id",json!("bad\u{1b}target")),("cost_usd",json!(-1.0)),("route_mode",json!("autoPermissions"))] {
            let mut malformed=value.clone();malformed[key]=bad;
            assert!(RoutingStatus::from_value(&malformed).is_none(),"{key}");
        }
        telemetry.update_status(&json!({"routing":null}));
        assert!(telemetry.routing.is_none());
    }

    #[test]
    fn vendor_estimate_displays_session_sum_and_partial_turn() {
        let mut telemetry = SessionTelemetry::default();
        telemetry.update_turn(&json!({"cost_usd":0.5,"session_cost_usd":1.5,"cost_is_estimate":true}));
        assert_eq!(telemetry.billing_label(Some("deepseek")).as_deref(), Some("$1.5000 est"));
        telemetry.update_turn(&json!({"cost_usd":0.25,"session_cost_usd":null,"cost_is_estimate":true}));
        assert_eq!(telemetry.billing_label(Some("deepseek")).as_deref(), Some("$0.2500 turn est"));
    }
    #[test]
    fn subscription_quota_uses_highest_reported_window_and_exact_thresholds() {
        let mut telemetry = SessionTelemetry::default();
        for (quota, expected) in [
            ("5h:0% week:65%", Some(super::super::theme::SUCCESS)),
            ("5h:66% week:12%", Some(super::super::theme::SUCCESS)),
            ("5h 66.1% · week 12%", Some(super::super::theme::WARNING)),
            ("5h:12% week:90%", Some(super::super::theme::WARNING)),
            ("5h:12% week:90.1%", Some(super::super::theme::ERROR)),
            ("5h:100% week:12%", Some(super::super::theme::ERROR)),
            ("unknown", None),
            ("5h:NaN% week:101%", None),
            ("5h:-5%", None),
        ] {
            telemetry.update_billing(&json!({"mode":"subscription","quota":quota}));
            assert_eq!(telemetry.quota_color(), expected, "{quota}");
        }
        telemetry.update_billing(&json!({"mode":"api","quota":"5h:99%"}));
        assert_eq!(telemetry.quota_color(), None);
        telemetry.update_billing(&json!({"mode":"subscription","type":"Max"}));
        assert_eq!(telemetry.quota_color(), None);
    }
}
