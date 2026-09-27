//! Provider-reported session usage, context and billing display state.

use super::safe_label;

#[derive(Clone, Debug, Default)]
pub(super) struct SessionTelemetry {
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
    pub(super) fn update_turn(&mut self, data: &serde_json::Value) {
        let context = data["ctx_percentage"].as_f64()
            .filter(|value| value.is_finite() && (0.0..=100.0).contains(value))
            .map(|value| format!("{value:.0}%"));
        let absolute = data["ctx_tokens"].as_u64().zip(data["ctx_max_tokens"].as_u64())
            .filter(|(used, limit)| *limit > 0 && used <= limit)
            .map(|(used, limit)| format!("{used}/{limit}"));
        if data.get("ctx_percentage").is_some() || data.get("ctx_tokens").is_some() {
            self.context = context.or(absolute);
            self.context_percent = data["ctx_percentage"].as_f64()
                .filter(|value| value.is_finite() && (0.0..=100.0).contains(value));
            self.context_tokens = data["ctx_tokens"].as_u64();
            self.context_limit = data["ctx_max_tokens"].as_u64().filter(|limit| *limit > 0);
        }
        if data["usage_scope"] == "session" {
            self.turns = data["num_turns"].as_u64().or(self.turns);
            self.input_tokens = data["input_tokens"].as_u64().or(self.input_tokens);
            self.output_tokens = data["output_tokens"].as_u64().or(self.output_tokens);
            self.cache_read_tokens = data["cache_read_input_tokens"].as_u64().or(self.cache_read_tokens);
        }
        if let Some(cost) = data["session_cost_usd"].as_f64()
            .filter(|value| value.is_finite() && *value >= 0.0) {
            self.cost = Some(format!("${cost:.4}"));
        } else if let Some(cost) = data["cost_usd"].as_f64()
            .filter(|value| value.is_finite() && *value >= 0.0) {
            self.cost = Some(format!("${cost:.4} turn"));
        } else if data.get("session_cost_usd").is_some() || data.get("cost_usd").is_some() {
            self.cost = None;
        }
        if data.get("session_cost_usd").is_some() {
            self.session_cost = data["session_cost_usd"].as_f64()
                .filter(|value| value.is_finite() && *value >= 0.0)
                .map(|cost| format!("${cost:.4}"));
        }
    }

    pub(super) fn update_billing(&mut self, billing: &serde_json::Value) {
            self.billing_mode = match billing["mode"].as_str() {
                Some("api") => Some("api".into()),
                Some("subscription") => Some("subscription".into()),
                _ => None,
            };
            self.subscription_type = billing["type"].as_str()
                .filter(|name| !name.is_empty() && name.len() <= 64 && !name.chars().any(char::is_control))
                .map(safe_label);
            self.quota = billing["quota"].as_str()
                .filter(|quota| !quota.is_empty() && quota.len() <= 120 && !quota.chars().any(char::is_control))
                .map(safe_label);
            self.balance = billing["balance"].as_str()
                .filter(|balance| !balance.is_empty() && balance.len() <= 80 && !balance.chars().any(char::is_control))
                .map(safe_label);
    }

    pub(super) fn update_status(&mut self, status: &serde_json::Value) {
        if let Some(account) = status.get("account") {
            let mut fields = serde_json::Map::new();
            for key in ["email", "organization", "subscriptionType", "apiProvider"] {
                if let Some(text) = account[key].as_str().filter(|text| !text.trim().is_empty()
                    && text.len() <= 256 && !text.chars().any(char::is_control)) {
                    fields.insert(key.into(), serde_json::Value::String(safe_label(text.trim())));
                }
            }
            self.account = (!fields.is_empty()).then_some(serde_json::Value::Object(fields));
        }
        if let Some(billing) = status.get("billing") { self.update_billing(billing); }
        let context = status["ctx_percentage"].as_f64()
            .filter(|value| value.is_finite() && (0.0..=100.0).contains(value))
            .map(|value| format!("{value:.0}%"));
        let absolute = status["ctx_tokens"].as_u64().zip(status["ctx_max_tokens"].as_u64())
            .filter(|(used, limit)| *limit > 0 && used <= limit)
            .map(|(used, limit)| format!("{used}/{limit}"));
        if status.get("ctx_percentage").is_some() || status.get("ctx_tokens").is_some() {
            self.context = context.or(absolute);
            self.context_percent = status["ctx_percentage"].as_f64()
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
        if let Some(cost) = status["total_cost_usd"].as_f64()
            .filter(|value| value.is_finite() && *value >= 0.0) {
            let label = if status["usage"]["cost_basis"].as_str().is_some() {
                if status["usage"]["unpriced_models"].as_array().is_some_and(|models| !models.is_empty()) {
                    "est partial"
                } else { "est" }
            } else { "" };
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
            Some("deepseek" | "glm") => Some(self.cost.clone().unwrap_or_else(|| "$?".into())),
            Some("codex" | "claude") => match self.billing_mode.as_deref() {
                Some("api") => Some(self.cost.clone().unwrap_or_else(|| "$?".into())),
                Some("subscription") => {
                    let tier = self.subscription_type.as_deref().filter(|tier| *tier != "subscription");
                    if tier.is_none() && self.quota.is_none() { return None; }
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

