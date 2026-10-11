//! Provider-reported session usage, context and billing display state.

use super::safe_label;

#[derive(Clone, Debug, PartialEq)]
pub(super) struct QuotaWindow {
    pub(super) percent: f64,
    pub(super) duration: String,
    pub(super) resets_at: Option<u64>,
    pub(super) stale: bool,
}

impl QuotaWindow {
    fn from_value(value: &serde_json::Value, default_duration: &str, stale: bool) -> Option<Self> {
        let percent = value["percent"].as_u64().map(|n| n as f64)
            .or_else(|| value["percent"].as_f64())
            .filter(|n| n.is_finite() && (0.0..=100.0).contains(n))?;
        let duration = value["window_minutes"].as_u64()
            .filter(|n| *n > 0 && *n <= 10_080)
            .map(duration_label)
            .unwrap_or_else(|| default_duration.to_owned());
        let resets_at = value["resets_at"].as_u64().filter(|n| *n <= 253_402_300_799);
        Some(Self { percent, duration, resets_at, stale })
    }

    fn from_text(quota: &str, label: &str, duration: &str, stale: bool) -> Option<Self> {
        let percent = legacy_quota_percent(quota, label)?;
        Some(Self { percent, duration: duration.to_owned(), resets_at: None, stale })
    }
}

fn duration_label(minutes: u64) -> String {
    if minutes == 10_080 { "7d".into() }
    else if minutes % 60 == 0 { format!("{}h", minutes / 60) }
    else { format!("{minutes}m") }
}

fn legacy_quota_percent(quota: &str, wanted: &str) -> Option<f64> {
    let mut waiting_for_value = false;
    let mut found = None;
    for part in quota.split(|c: char| c.is_whitespace() || matches!(c, '·' | ',' | ';')) {
        if part.is_empty() { continue; }
        let value = if let Some((label, value)) = part.split_once(':') {
            if label == wanted { Some(value) } else { None }
        } else if part == wanted {
            waiting_for_value = true;
            continue;
        } else if waiting_for_value {
            waiting_for_value = false;
            Some(part)
        } else {
            None
        };
        let Some(value) = value else { continue; };
        let value = value.strip_suffix('~').unwrap_or(value).strip_suffix('%')?.parse::<f64>().ok()
            .filter(|n| n.is_finite() && (0.0..=100.0).contains(n))?;
        if found.replace(value).is_some() { return None; }
    }
    found
}

fn identifier(data: &serde_json::Value, key: &str, limit: usize) -> Option<String> {
    let value = data[key].as_str()?;
    (!value.is_empty() && value.len() <= limit
        && value.bytes().all(|c| c.is_ascii_alphanumeric() || b"-_.:".contains(&c)))
        .then(|| value.to_owned())
}

/// Summary worker metadata is separate from ordinary turn routing and controls.
#[derive(Clone, Debug, PartialEq)]
pub(super) struct CompactionStatus {
    target_id: String,
    engine: String,
    model: String,
    effort: String,
    reviewed: bool,
    original_messages: u64,
}

impl CompactionStatus {
    pub(super) fn from_value(data: &serde_json::Value) -> Option<Self> {
        Some(Self {
            target_id: identifier(data, "target_id", 48)?,
            engine: data["engine"].as_str().filter(|v| matches!(*v,"deepseek"|"glm"))?.into(),
            model: identifier(data, "model", 128)?,
            effort: data["effort"].as_str().filter(|v| matches!(*v,"none"|"low"|"medium"|"high"|"xhigh"|"max"))?.into(),
            reviewed: data["reviewed"].as_bool()?,
            original_messages: data["original_messages"].as_u64()?,
        })
    }

    pub(super) fn reviewed(&self) -> bool { self.reviewed }
    pub(super) fn label(&self) -> String {
        format!("Summary {} · {}/{} · {}", self.target_id, self.engine, self.model, self.effort)
    }
    pub(super) fn summary(&self) -> String {
        format!("{} · {} · {} original messages retained", self.label(),
            if self.reviewed {"reviewed summary"} else {"summary unverified"}, self.original_messages)
    }
    pub(super) fn lines(&self) -> Vec<String> {
        vec![self.summary(), "A verified managed summary is separate context; canonical originals are retained.".into(),
            "Auto/pin and the last ordinary routed worker remain unchanged; no Jev selection call.".into(),
            "Summary call cost is included in the tracked Jev/direct API estimate for Router sessions.".into()]
    }
}

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
    tracked_cost_only: bool,
}

impl RoutingStatus {
    pub(super) fn from_value(data: &serde_json::Value) -> Option<Self> {
        let engine = data["engine"].as_str().filter(|v| matches!(*v, "deepseek" | "glm" | "claude" | "codex"))?;
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
            tracked_cost_only: data["cost_scope"] == "jev_and_direct_api_only" || matches!(engine,"claude"|"codex"),
        })
    }

    pub(super) fn label(&self) -> String {
        format!("{} · {}/{} · {}", self.target_id, self.engine, self.model, self.effort)
    }

    pub(super) fn summary(&self) -> String {
        let reason = self.fallback_reason.as_deref().unwrap_or("selected");
        let cost = self.cost_usd.map(|v| format!("${v:.6} est")).unwrap_or_else(|| "unknown cost".into());
        format!("Route {} · {} · {reason} · {} ms · {}{cost}{}", self.route_mode, self.label(), self.latency_ms,
            if self.tracked_cost_only {"Jev "} else {""},if self.tracked_cost_only {" · CLI charges unknown"} else {""})
    }

    pub(super) fn lines(&self) -> Vec<String> {
        vec![self.summary(), if self.tracked_cost_only {
            "Decision cost is Jev only. Tracked session cost covers Jev/direct API; CLI account charges and total spend are unknown."
        } else {"Cost covers this routing decision; session cost includes worker calls."}.into(),
            "Auto/target selection is controlled by /model; effort is configured per target.".into()]
    }
}

#[derive(Clone, Debug, Default)]
pub(super) struct SessionTelemetry {
    pub(super) compaction: Option<CompactionStatus>,
    pub(super) routing: Option<RoutingStatus>,
    pub(super) isolation: Option<serde_json::Value>,
    pub(super) account: Option<serde_json::Value>,
    pub(super) context: Option<String>,
    pub(super) context_usage: super::context_usage::ContextUsage,
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
    pub(super) tracked_cost_only: bool,
    pub(super) external_mcp_cost_unknown: bool,
    pub(super) billing_mode: Option<String>,
    pub(super) subscription_type: Option<String>,
    pub(super) quota: Option<String>,
    pub(super) session_quota: Option<QuotaWindow>,
    pub(super) weekly_quota: Option<QuotaWindow>,
    pub(super) balance: Option<String>,
    pub(super) glm_account: Option<super::glm_account::GlmAccount>,
    pub(super) lore: Option<String>,
}

impl SessionTelemetry {
    pub(super) fn update_cost_scope(&mut self, data: &serde_json::Value) {
        let external = &data["external_mcp"];
        self.external_mcp_cost_unknown |= external["enabled"] == true
            && external["cost_scope"] == "outside_model_allowance"
            && external.get("charge_usd").is_some_and(serde_json::Value::is_null)
            && external["calls_attempted"].as_u64().is_some()
            && external["outcome_unknown"].is_boolean();
        // Once a session can use CLI account workers, later API-only turns or
        // incomplete status replies cannot turn the partial amount into a total.
        self.tracked_cost_only |= data["cost_scope"] == "jev_and_direct_api_only"
            || self.routing.as_ref().is_some_and(|routing|routing.tracked_cost_only);
        if self.tracked_cost_only {
            if let Some(routing)=self.routing.as_mut() {routing.tracked_cost_only=true;}
        }
    }
    fn store_context(&mut self, context: super::context_usage::ContextUsage) {
        self.context = context.chip_label();
        self.context_percent = context.percentage;
        self.context_tokens = context.used;
        self.context_limit = context.capacity;
        self.context_usage = context;
    }

    fn update_context(&mut self, report: &serde_json::Value) {
        if ["ctx_percentage", "ctx_tokens", "ctx_max_tokens"].iter().any(|key| report.get(key).is_some()) {
            self.store_context(super::context_usage::ContextUsage::reported(report, "ctx_tokens", "ctx_max_tokens", "ctx_percentage"));
        }
    }

    pub(super) fn update_context_detail(&mut self, detail: &serde_json::Value) {
        self.store_context(self.context_usage.detail(detail));
    }

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
        if let Some(compaction) = data.get("compaction") { self.compaction = CompactionStatus::from_value(compaction); }
        if let Some(routing) = data.get("routing") { self.routing = RoutingStatus::from_value(routing); }
        self.update_cost_scope(data);
        self.update_context(data);
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
        if let Some(account)=billing.get("glm_account") {self.glm_account=super::glm_account::GlmAccount::from_value(account);}
        if let Some(compaction) = billing.get("compaction") { self.compaction = CompactionStatus::from_value(compaction); }
        if let Some(routing) = billing.get("routing") { self.routing = RoutingStatus::from_value(routing); }
        self.update_cost_scope(billing);
        if billing.get("session_cost_usd").is_some() || billing.get("cost_usd").is_some() {
            self.update_turn(billing);
        }
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
        let stale = billing["quota_stale"] == true
            || billing["quota"].as_str().is_some_and(|quota| quota.trim_end().ends_with('~'));
        let structured = billing.get("quota_windows").filter(|value| value.is_object())
            .or_else(|| billing.get("quota_limits").filter(|value| value.is_object()));
        if let Some(windows) = structured {
            self.session_quota = windows.get("session").or_else(|| windows.get("five_hour"))
                .and_then(|value| QuotaWindow::from_value(value, "5h", stale));
            self.weekly_quota = windows.get("weekly").or_else(|| windows.get("seven_day"))
                .and_then(|value| QuotaWindow::from_value(value, "week", stale));
        } else if let Some(quota) = billing["quota"].as_str() {
            self.session_quota = QuotaWindow::from_text(quota, "5h", "5h", stale);
            self.weekly_quota = QuotaWindow::from_text(quota, "week", "week", stale);
        } else {
            self.session_quota = None;
            self.weekly_quota = None;
        }
        self.balance = billing["balance"]
            .as_str()
            .filter(|balance| {
                !balance.is_empty() && balance.len() <= 80 && !balance.chars().any(char::is_control)
            })
            .map(safe_label);
    }

    pub(super) fn update_status(&mut self, status: &serde_json::Value) {
        if let Some(compaction) = status.get("compaction") { self.compaction = CompactionStatus::from_value(compaction); }
        if let Some(routing) = status.get("routing") { self.routing = RoutingStatus::from_value(routing); }
        self.update_cost_scope(status);
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
        self.update_context(status);
        if let Some(usage) = status.get("usage") {
            self.turns = usage["num_turns"].as_u64();
            self.input_tokens = usage["input_tokens"].as_u64();
            self.output_tokens = usage["output_tokens"].as_u64();
            self.cache_read_tokens = usage["cache_read_input_tokens"].as_u64();
            self.cache_write_tokens = usage["cache_creation_input_tokens"].as_u64();
        }
        if !self.tracked_cost_only && !self.external_mcp_cost_unknown { if let Some(cost) = status["total_cost_usd"]
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
        } }
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
        let label = match engine {
            Some("router") if self.tracked_cost_only=>Some(format!("{} Jev/API · CLI ?",self.cost.clone().unwrap_or_else(||"$?".into()))),
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
        };
        label.map(|label| if self.external_mcp_cost_unknown { format!("{label} · MCP ?") } else { label })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn external_mcp_charge_scope_is_sticky_and_restores_without_erasing_model_cost() {
        let external = json!({"enabled":true,"cost_scope":"outside_model_allowance","charge_usd":null,
            "calls_attempted":1,"outcome_unknown":false});
        let mut value=SessionTelemetry::default();
        value.update_turn(&json!({"session_cost_usd":0.01,"cost_usd":0.01,"cost_is_estimate":true,"external_mcp":external}));
        assert_eq!(value.billing_label(Some("glm")).as_deref(),Some("$0.0100 est · MCP ?"));
        value.update_turn(&json!({"session_cost_usd":0.02,"cost_usd":0.01,"cost_is_estimate":true}));
        assert_eq!(value.billing_label(Some("glm")).as_deref(),Some("$0.0200 est · MCP ?"));
        let mut restored=SessionTelemetry::default();
        restored.update_status(&json!({"billing":{"mode":"api","session_cost_usd":0.02,"cost_usd":0.02,
            "cost_is_estimate":true,"external_mcp":external},"total_cost_usd":null}));
        assert_eq!(restored.billing_label(Some("glm")),value.billing_label(Some("glm")));
        let mut malformed=SessionTelemetry::default();
        malformed.update_turn(&json!({"external_mcp":{"enabled":true,"charge_usd":0}}));
        assert!(!malformed.external_mcp_cost_unknown);
    }

    #[test]
    fn mixed_router_cost_scope_survives_api_turns_and_reattach_without_becoming_total() {
        let routing=json!({"target_id":"cli-target","engine":"codex","model":"gpt-test","effort":"high",
            "route_mode":"auto","fallback_reason":null,"latency_ms":10,"cost_usd":0.000001,
            "cost_scope":"jev_and_direct_api_only","total_cost_usd":null,"cli_provider_charge_usd":null});
        let mut telemetry=SessionTelemetry::default();
        telemetry.update_turn(&json!({"routing":routing,"cost_usd":0.01,"session_cost_usd":0.01,"cost_is_estimate":true,
            "cost_scope":"jev_and_direct_api_only","total_cost_usd":null}));
        assert_eq!(telemetry.billing_label(Some("router")).as_deref(),Some("$0.0100 est Jev/API · CLI ?"));
        assert!(telemetry.routing.as_ref().unwrap().lines().join("\n").contains("total spend are unknown"));
        // Sticky scope also protects an incomplete older API-only frame.
        telemetry.update_turn(&json!({"routing":{"target_id":"api-target","engine":"glm","model":"glm-5.3-flash",
            "effort":"high","route_mode":"pinned","latency_ms":0,"cost_usd":0},
            "cost_usd":0.02,"session_cost_usd":0.03,"cost_is_estimate":true}));
        assert_eq!(telemetry.billing_label(Some("router")).as_deref(),Some("$0.0300 est Jev/API · CLI ?"));
        assert!(telemetry.routing.as_ref().unwrap().lines().join("\n").contains("total spend are unknown"));
        let mut restored=SessionTelemetry::default();
        restored.update_status(&json!({"routing":routing,"total_cost_usd":null,"billing":{
            "mode":"api","cost_usd":0.03,"session_cost_usd":0.03,"cost_is_estimate":true,
            "cost_scope":"jev_and_direct_api_only","total_cost_usd":null,"cli_provider_charge_usd":null}}));
        assert_eq!(restored.billing_label(Some("router")),telemetry.billing_label(Some("router")));
        restored.update_turn(&json!({"cost_usd":null,"session_cost_usd":null,"cost_is_estimate":true}));
        assert_eq!(restored.billing_label(Some("router")).as_deref(),Some("$? Jev/API · CLI ?"));
        assert!(restored.session_cost.is_none());
    }

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
        for (key,bad) in [("fallback_reason",json!("provider secret error")),("engine",json!("unsupported")),("target_id",json!("bad\u{1b}target")),("cost_usd",json!(-1.0)),("route_mode",json!("autoPermissions"))] {
            let mut malformed=value.clone();malformed[key]=bad;
            assert!(RoutingStatus::from_value(&malformed).is_none(),"{key}");
        }
        telemetry.update_status(&json!({"routing":null}));
        assert!(telemetry.routing.is_none());
    }
    #[test]
    fn router_billing_restore_preserves_aggregate_estimate_and_clears_unknown_cost() {
        let mut telemetry=SessionTelemetry::default();
        telemetry.update_status(&json!({"ctx_tokens":55,"ctx_max_tokens":1000,"usage":{"input_tokens":55,"num_turns":2}}));
        let routing=json!({"target_id":"glm-fixture","engine":"glm","model":"glm-5.3-flash","effort":"high",
            "route_mode":"auto","fallback_reason":null,"latency_ms":10,"cost_usd":0.000004});
        telemetry.update_status(&json!({"billing":{"mode":"api","session_cost_usd":0.125,"cost_usd":0.125,
            "cost_is_estimate":true,"cost_basis":"aggregate_upper_rates","routing":routing}}));
        assert_eq!(telemetry.billing_label(Some("router")).as_deref(),Some("$0.1250 est"));
        assert_eq!(telemetry.session_cost.as_deref(),Some("$0.1250 est"));
        assert_eq!(telemetry.input_tokens,Some(55));assert_eq!(telemetry.turns,Some(2));
        assert_eq!(telemetry.context.as_deref(),Some("55/1000"));
        assert!(telemetry.routing.as_ref().unwrap().label().contains("glm-5.3-flash"));
        telemetry.update_billing(&json!({"mode":"api","session_cost_usd":null,"cost_usd":null,"cost_is_estimate":true}));
        assert_eq!(telemetry.billing_label(Some("router")).as_deref(),Some("$?"));
        assert!(telemetry.session_cost.is_none());
        assert_eq!(telemetry.input_tokens,Some(55));assert_eq!(telemetry.context.as_deref(),Some("55/1000"));
    }

    #[test]
    fn router_compaction_billing_restores_bounded_target_and_clears_unknown_metadata() {
        let mut telemetry = SessionTelemetry::default();
        let routing = serde_json::json!({"target_id":"ds-fixture","engine":"deepseek","model":"deepseek-flash","effort":"high",
            "route_mode":"auto","fallback_reason":null,"latency_ms":24,"cost_usd":0.000004});
        let compaction = serde_json::json!({"target_id":"glm-fixture","engine":"glm","model":"glm-5.3-flash","effort":"none",
            "reviewed":true,"original_messages":12,"source":"private source","summary":"private summary"});
        telemetry.update_status(&json!({"billing":{"mode":"api","routing":routing,"compaction":compaction,
            "cost_usd":0.125,"session_cost_usd":0.25,"cost_is_estimate":true}}));
        let previous_route = telemetry.routing.clone();
        assert!(telemetry.compaction.as_ref().unwrap().summary().contains("glm-fixture"));
        assert!(!telemetry.compaction.as_ref().unwrap().lines().join("\n").contains("private"));
        assert_eq!(telemetry.billing_label(Some("router")).as_deref(), Some("$0.2500 est"));
        telemetry.update_turn(&json!({"operation":"compact","is_error":true,"cost_usd":null,
            "session_cost_usd":null,"cost_is_estimate":true}));
        assert!(telemetry.compaction.is_some());
        assert_eq!(telemetry.routing, previous_route);
        assert_eq!(telemetry.billing_label(Some("router")).as_deref(), Some("$?"));
        for malformed in [json!(null), json!({"target_id":"missing fields"}), json!({"target_id":"g".repeat(49),
            "engine":"glm","model":"glm-5.3-flash","effort":"high","reviewed":true,"original_messages":12})] {
            telemetry.update_billing(&json!({"compaction":compaction}));
            assert!(telemetry.compaction.is_some());
            telemetry.update_status(&json!({"billing":{"compaction":malformed}}));
            assert!(telemetry.compaction.is_none());
            assert_eq!(telemetry.routing, previous_route);
        }
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

    #[test]
    fn quota_window_bars_use_only_recognized_provider_windows() {
        let mut telemetry = SessionTelemetry::default();
        telemetry.update_billing(&json!({"mode":"subscription","quota":"5h:42% week:75%",
            "quota_limits":{"five_hour":{"percent":42,"resets_at":1_800_000_000},
                "seven_day":{"percent":75}}}));
        let session = telemetry.session_quota.as_ref().unwrap();
        assert_eq!(session.percent, 42.0);
        assert_eq!(session.duration, "5h");
        assert_eq!(session.resets_at, Some(1_800_000_000));
        assert_eq!(telemetry.weekly_quota.as_ref().unwrap().percent, 75.0);
        assert_eq!(telemetry.weekly_quota.as_ref().unwrap().duration, "week");

        telemetry.update_billing(&json!({"mode":"subscription","quota":"1h:33% · week:100%",
            "quota_windows":{"session":{"percent":33.5,"window_minutes":60},
                "weekly":{"percent":100,"window_minutes":10080}}}));
        assert_eq!(telemetry.session_quota.as_ref().unwrap().duration, "1h");
        assert_eq!(telemetry.session_quota.as_ref().unwrap().percent, 33.5);
        assert_eq!(telemetry.weekly_quota.as_ref().unwrap().duration, "7d");
        assert_eq!(telemetry.weekly_quota.as_ref().unwrap().percent, 100.0);

        telemetry.update_billing(&json!({"mode":"subscription","quota":"5h 42% · week 25%~"}));
        assert_eq!(telemetry.session_quota.as_ref().unwrap().percent, 42.0);
        assert!(telemetry.weekly_quota.as_ref().unwrap().stale);
        telemetry.update_billing(&json!({"mode":"subscription","quota":"opus:55%"}));
        assert!(telemetry.session_quota.is_none());
        assert!(telemetry.weekly_quota.is_none());
        telemetry.update_billing(&json!({"mode":"subscription"}));
        assert!(telemetry.session_quota.is_none());
        assert!(telemetry.weekly_quota.is_none());
    }
}
