//! Closed display state for standard GLM API reporting and an external monitor.
use serde_json::Value;

#[derive(Clone, Debug, PartialEq)]
pub(super) struct GlmAccount {
    profile: Option<&'static str>,
    source: &'static str,
    pub(super) monitor: bool,
    status: &'static str,
    observed_at: Option<u64>,
    limits: Vec<(&'static str, f64)>,
}

impl GlmAccount {
    pub(super) fn from_value(value: &Value) -> Option<Self> {
        if value["inference_endpoint"] != doxa_vendors::Vendor::Glm.endpoint()
            || value["cash_balance_status"] != "unknown"
            || value["resource_bundle_remaining_status"] != "unknown"
            || value["provider_charge_status"] != "unknown"
            || value["session_price_basis"] != "list_price_estimate_only" { return None; }
        let profile = match value["profile"].as_str() {
            Some("standard_api") => Some("standard_api"),
            Some("api_resource_bundle") => Some("api_resource_bundle"),
            None if value["profile"].is_null() => None,
            _ => return None,
        };
        let source = match value["profile_source"].as_str()? {
            "default" if profile == Some("standard_api") => "default",
            "owner_declared" if profile.is_some() => "owner-declared",
            "invalid_config" if profile.is_none() => "invalid configuration",
            _ => return None,
        };
        let plan = &value["plan_monitor"];
        if plan["scope"] != "external_coding_plan_monitor_only" || plan["inference_uses_plan"] != false { return None; }
        let monitor = plan["enabled"].as_bool()?;
        let quota = &plan["quota"];
        let status = match quota["status"].as_str()? {
            "disabled" if !monitor => "off",
            "not_checked" if monitor => "not checked",
            "refreshing" if monitor => "refreshing",
            "verified" if monitor => "provider-reported",
            "stale" if monitor => "stale; refresh required",
            "unauthorized" if monitor => "unauthorized",
            "timeout" if monitor => "timed out",
            "cancelled" if monitor => "cancelled",
            "credential_unavailable" if monitor => "credential unavailable",
            "invalid_response" if monitor => "invalid provider response",
            "response_too_large" if monitor => "oversized provider response",
            "provider_unavailable" | "transport_unavailable" | "unavailable" | "local_fixture" if monitor => "unavailable",
            "invalid_config" if !monitor => "configuration invalid",
            _ => return None,
        };
        let mut limits = Vec::new();
        let observed_at = quota["observed_at"].as_u64();
        if status == "provider-reported" {
            if observed_at.is_none_or(|time|time == 0) { return None; }
            let rows = quota["limits"].as_array().filter(|rows|!rows.is_empty() && rows.len()<=8)?;
            for row in rows {
                let kind = match row["kind"].as_str()? {
                    "token_quota" => "Token quota", "mcp_quota" => "MCP quota", "provider_quota" => "Provider quota", _ => return None,
                };
                let percentage = row["used_percent"].as_f64().filter(|v|v.is_finite() && (0.0..=100.0).contains(v))?;
                limits.push((kind,percentage));
            }
        }
        Some(Self {profile,source,monitor,status,observed_at,limits})
    }

    pub(super) fn label(&self) -> String {
        if self.profile == Some("api_resource_bundle") {"GLM bundle · remainder ?".into()}
        else {"GLM API · balance ?".into()}
    }

    pub(super) fn unavailable(&mut self) {
        self.status="unavailable";self.observed_at=None;self.limits.clear();
    }

    pub(super) fn lines(&self) -> Vec<String> {
        let mut lines = vec![String::new(), "GLM account reporting".into(),
            format!("Profile: {} ({})", match self.profile {Some("api_resource_bundle")=>"API resource bundle",Some(_)=>"standard API",None=>"unknown"},self.source),
            format!("Inference: {}",doxa_vendors::Vendor::Glm.endpoint()),
            "Cash balance: unknown · bundle remainder: unknown".into(),
            "Actual provider charge: unknown; session cost is a separate list-price estimate.".into(),
            format!("Billing console: {}",doxa_vendors::glm_billing::BILLING_CONSOLE),
            format!("External Coding Plan quota monitor: {}",self.status),
            "Monitor only; this session does not use Coding Plan inference or claim entitlement.".into()];
        if self.monitor {
            lines.push("R refreshes the read-only monitor; failures do not block model turns.".into());
            for (kind,percentage) in &self.limits { lines.push(format!("{kind}: {percentage:.1}% used (provider-reported; window/units unspecified)")); }
            if self.limits.is_empty() {lines.push("Quota: unknown; no remaining amount inferred.".into());}
            if let Some(time) = self.observed_at {lines.push(format!("Last observed: {time} UTC epoch seconds"));}
            lines.push(format!("Quota source: {}",doxa_vendors::glm_billing::QUOTA_SOURCE));
        } else { lines.push("Enable external Coding Plan monitor in /settings → Session to allow explicit refresh.".into()); }
        lines
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use doxa_vendors::glm_billing::{account_report,Profile,QuotaSnapshot};
    use serde_json::json;

    #[test]
    fn bundle_source_and_unknown_remainder_are_separate_from_api_cost_and_plan() {
        let report=account_report(Some(Profile::ApiResourceBundle),true,false,&QuotaSnapshot::not_checked());
        let account=GlmAccount::from_value(&report["glm_account"]).unwrap();
        assert_eq!(account.label(),"GLM bundle · remainder ?");
        let lines=account.lines().join("\n");
        assert!(lines.contains("owner-declared"));assert!(lines.contains("bundle remainder: unknown"));
        assert!(lines.contains("Actual provider charge: unknown"));assert!(lines.contains("list-price estimate"));
        assert!(!lines.contains("$0"));assert!(!account.monitor);
    }

    #[test]
    fn monitor_percentages_require_verified_timestamp_and_closed_fields() {
        let mut report=account_report(Some(Profile::StandardApi),false,true,&QuotaSnapshot::not_checked());
        let quota=&mut report["glm_account"]["plan_monitor"]["quota"];
        *quota=json!({"status":"verified","observed_at":1,"limits":[{"kind":"provider_quota","used_percent":82}],"arbitrary":"private provider text"});
        let account=GlmAccount::from_value(&report["glm_account"]).unwrap();
        assert!(account.lines().join("\n").contains("82.0% used"));
        assert!(!account.lines().join("\n").contains("private provider text"));
        for bad in [json!(-1),json!(101),json!("0"),Value::Null] {
            report["glm_account"]["plan_monitor"]["quota"]["limits"][0]["used_percent"]=bad;
            assert!(GlmAccount::from_value(&report["glm_account"]).is_none());
        }
        report["glm_account"]["plan_monitor"]["quota"]=json!({"status":"stale","observed_at":1,"limits":[{"kind":"provider_quota","used_percent":82}]});
        assert!(!GlmAccount::from_value(&report["glm_account"]).unwrap().lines().join("\n").contains("82.0%"));
        report["glm_account"]["plan_monitor"]["inference_uses_plan"]=json!(true);
        assert!(GlmAccount::from_value(&report["glm_account"]).is_none());
    }
}
