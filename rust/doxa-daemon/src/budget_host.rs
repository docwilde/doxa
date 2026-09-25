//! A session-start spend ceiling for hosts that report actual USD costs.
use doxa_runtime::Host;
use crate::peer_host::PEER_TURN_MARKER;
use serde_json::{json, Value};
use std::io;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

pub struct BudgetHost {
    inner: Arc<dyn Host>,
    ceiling: f64,
    pricing: Option<Pricing>,
    priced_model: Option<String>,
    state: Mutex<BudgetState>,
}

/// Rates copied from the sourced Python 1.19 sheet (doxa/prices.py,
/// read 2026-09-21). Native vendor accounting deliberately charges every
/// prompt token at the fresh-input rate: cached tokens are a subset of the
/// prompt, and the published cached rate is lower for every row below.
#[derive(Clone, Copy)]
struct Pricing { input: f64, output: f64, source: &'static str }

fn vendor_price(engine: &str, model: &str) -> Option<Pricing> {
    let (input, output) = match (engine, model) {
        ("deepseek", "deepseek-flash") => (0.3, 1.2),
        ("deepseek", "deepseek-v4-pro") => (1.32, 3.96),
        ("glm", "glm-5.3-flash") => (0.15, 0.5),
        ("glm", "glm-5.3" | "glm-5.2" | "glm-5.1") => (1.4, 4.4),
        ("glm", "glm-5") => (1.0, 3.2),
        ("glm", "glm-4.7" | "glm-4.6" | "glm-4.5") => (0.6, 2.2),
        ("glm", "glm-4.5-air") => (0.2, 1.1),
        _ => return None,
    };
    let source = match engine {
        "deepseek" => "https://api-docs.deepseek.com/quick_start/pricing",
        "glm" => "https://docs.z.ai/guides/overview/pricing",
        _ => return None,
    };
    Some(Pricing { input, output, source })
}

pub(super) fn priced_vendor_model(engine: &str, model: &str) -> bool {
    vendor_price(engine, model).is_some()
}

#[derive(Default)]
struct BudgetState {
    spent: f64,
    unknown: bool,
}

impl BudgetHost {
    pub fn new(inner: Arc<dyn Host>, ceiling: f64) -> Self {
        Self { inner, ceiling, pricing: None, priced_model: None, state: Mutex::new(BudgetState::default()) }
    }

    pub fn new_priced(inner: Arc<dyn Host>, ceiling: f64, engine: &str, model: &str) -> Result<Self, String> {
        let pricing = vendor_price(engine, model)
            .ok_or_else(|| format!("no native budget price for {engine}:{model}"))?;
        Ok(Self { inner, ceiling, pricing: Some(pricing), priced_model: Some(model.to_owned()), state: Mutex::new(BudgetState::default()) })
    }
}

impl Host for BudgetHost {
    fn initial_effort(&self) -> Option<String> { self.inner.initial_effort() }
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        // The runtime serializes prompt execution, but this mutex also keeps
        // observation and the next admission on one state boundary.
        let mut state = self.state.lock().unwrap_or_else(|poison| poison.into_inner());
        if state.unknown || state.spent >= self.ceiling {
            let peer_started = text.starts_with(PEER_TURN_MARKER);
            let peer_origin = if peer_started {
                text.lines().find(|line| line.starts_with("--- peer message "))
            } else { None };
            let message = if state.unknown {
                "Session spend is unknown; further turns are withheld until cost accounting is available"
            } else {
                "Session spend ceiling reached; further turns are withheld"
            };
            emit(json!({"type":"turn_refused","data":{
                "reason":"budget","message":message,"spent_usd":if state.unknown { None } else { Some(state.spent) },
                "ceiling_usd":self.ceiling,"peer_started":peer_started,"peer_origin":peer_origin,"prompt":null
            }}));
            return;
        }
        let mut terminal_seen = false;
        self.inner.prompt(text, &mut |mut event| {
            if event["type"] == "turn_done" {
                terminal_seen = true;
                let cost = match self.pricing {
                    None => event["data"]["cost_usd"].as_f64(),
                    Some(price) => {
                        let data = &event["data"];
                        let model_ok = data["model"].as_str()
                            == self.priced_model.as_deref();
                        if data["usage_complete"] == true && data["model_consistent"] == true && model_ok {
                            data["prompt_tokens"].as_u64().zip(data["completion_tokens"].as_u64())
                                .map(|(input, output)| (input as f64 * price.input + output as f64 * price.output) / 1_000_000.0)
                        } else { None }
                    }
                };
                match cost {
                    Some(cost) if cost.is_finite() && cost >= 0.0 => {
                        state.spent += cost;
                        if !state.spent.is_finite() { state.unknown = true; }
                        if let Some(price) = self.pricing {
                            event["data"]["cost_usd"] = json!(cost);
                            event["data"]["session_cost_usd"] = json!(state.spent);
                            event["data"]["cost_basis"] = json!("priced_conservative");
                            event["data"]["price_source"] = json!(price.source);
                            event["data"]["price_read_on"] = json!("2026-09-21");
                        }
                    }
                    _ => state.unknown = true,
                }
            }
            emit(event);
        });
        if !terminal_seen { state.unknown = true; }
    }
    fn call(&self, method: &str, params: &Value) -> Result<Value, String> {
        if self.pricing.is_some() && method == "set_model" {
            return Err("model changes are unavailable under a priced session budget".into());
        }
        self.inner.call(method, params)
    }
    fn initial_model(&self) -> Option<String> { self.inner.initial_model() }
    fn initial_permission_mode(&self) -> String { self.inner.initial_permission_mode() }
    fn can_set_model(&self) -> bool { self.pricing.is_none() && self.inner.can_set_model() }
    fn can_set_permission_mode(&self) -> bool { self.inner.can_set_permission_mode() }
    fn billing_snapshot(&self) -> Option<Value> { self.inner.billing_snapshot() }
    fn lore_scrub_status(&self) -> Option<&'static str> { self.inner.lore_scrub_status() }
    fn public_prompt(&self, text: &str) -> Result<String, String> { self.inner.public_prompt(text) }
    fn transcript_snapshot(&self) -> io::Result<Option<(PathBuf, u64)>> { self.inner.transcript_snapshot() }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct CostHost(Option<f64>);
    impl Host for CostHost {
        fn prompt(&self, _: &str, emit: &mut dyn FnMut(Value)) {
            emit(json!({"type":"turn_done","data":{"cost_usd":self.0}}));
        }
        fn call(&self, _: &str, _: &Value) -> Result<Value, String> { Ok(json!({})) }
    }
    #[test]
    fn measured_spend_refuses_the_next_turn_without_calling_host() {
        let host = BudgetHost::new(Arc::new(CostHost(Some(0.6))), 1.0);
        let mut events = Vec::new();
        for _ in 0..3 { host.prompt("hello", &mut |event| events.push(event)); }
        assert_eq!(events[2]["type"], "turn_refused");
        assert_eq!(events[2]["data"]["spent_usd"], 1.2);
        assert_eq!(events[2]["data"]["ceiling_usd"], 1.0);
    }
    #[test]
    fn missing_cost_fails_closed_after_one_turn() {
        let host = BudgetHost::new(Arc::new(CostHost(None)), 1.0);
        let mut events = Vec::new();
        for _ in 0..2 { host.prompt("hello", &mut |event| events.push(event)); }
        assert_eq!(events[1]["type"], "turn_refused");
        assert!(events[1]["data"]["spent_usd"].is_null());
    }
    struct VendorCostHost(Value);
    impl Host for VendorCostHost {
        fn prompt(&self, _: &str, emit: &mut dyn FnMut(Value)) {
            emit(json!({"type":"turn_done","data":self.0}));
        }
        fn call(&self, _: &str, _: &Value) -> Result<Value, String> { Ok(json!({})) }
        fn can_set_model(&self) -> bool { true }
    }
    fn priced_event() -> Value {
        json!({"model":"deepseek-flash","model_consistent":true,
            "usage_complete":true,"prompt_tokens":1_000_000,"completion_tokens":1_000_000})
    }
    #[test]
    fn vendor_price_conservatively_counts_all_prompt_tokens() {
        let host = BudgetHost::new_priced(
            Arc::new(VendorCostHost(priced_event())), 1.0, "deepseek", "deepseek-flash"
        ).unwrap();
        let mut events = Vec::new();
        host.prompt("hello", &mut |event| events.push(event));
        host.prompt("hello", &mut |event| events.push(event));
        assert_eq!(events[0]["data"]["cost_usd"], 1.5);
        assert_eq!(events[0]["data"]["cost_basis"], "priced_conservative");
        assert_eq!(events[0]["data"]["price_read_on"], "2026-09-21");
        assert_eq!(events[1]["type"], "turn_refused");
        assert!(host.call("set_model", &json!({"model":"glm-5-turbo"})).is_err());
        assert!(!host.can_set_model());
    }
    #[test]
    fn priced_vendor_requires_complete_usage_and_consistent_model() {
        for field in ["usage_complete", "model_consistent"] {
            let mut data = priced_event();
            data[field] = json!(false);
            let host = BudgetHost::new_priced(
                Arc::new(VendorCostHost(data)), 1.0, "deepseek", "deepseek-flash"
            ).unwrap();
            let mut events = Vec::new();
            host.prompt("hello", &mut |event| events.push(event));
            host.prompt("hello", &mut |event| events.push(event));
            assert_eq!(events[1]["type"], "turn_refused");
            assert!(events[1]["data"]["spent_usd"].is_null());
        }
        assert!(BudgetHost::new_priced(
            Arc::new(VendorCostHost(priced_event())), 1.0, "glm", "glm-5-turbo"
        ).is_err());
    }
}
