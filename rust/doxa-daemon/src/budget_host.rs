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
    state: Mutex<BudgetState>,
}

#[derive(Default)]
struct BudgetState {
    spent: f64,
    unknown: bool,
}

impl BudgetHost {
    pub fn new(inner: Arc<dyn Host>, ceiling: f64) -> Self {
        Self { inner, ceiling, state: Mutex::new(BudgetState::default()) }
    }
}

impl Host for BudgetHost {
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
        self.inner.prompt(text, &mut |event| {
            if event["type"] == "turn_done" {
                match event["data"]["cost_usd"].as_f64() {
                    Some(cost) if cost.is_finite() && cost >= 0.0 => {
                        state.spent += cost;
                        if !state.spent.is_finite() { state.unknown = true; }
                    }
                    _ => state.unknown = true,
                }
            }
            emit(event);
        });
    }
    fn call(&self, method: &str, params: &Value) -> Result<Value, String> { self.inner.call(method, params) }
    fn initial_model(&self) -> Option<String> { self.inner.initial_model() }
    fn initial_permission_mode(&self) -> String { self.inner.initial_permission_mode() }
    fn can_set_model(&self) -> bool { self.inner.can_set_model() }
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
}
