//! Capture completion metadata at the host boundary, after provider decoding.
use doxa_lore::LoreClient;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap},
    path::Path,
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::Duration,
};

const MAX_PER_TURN: usize = 128;

pub(crate) struct Recorder {
    engine: &'static str,
    client: Mutex<Option<LoreClient>>,
    warned: AtomicBool,
}

impl Recorder {
    pub fn configured(engine: &'static str, lore_enabled: bool) -> Option<Self> {
        (lore_enabled
            && engine != "fixture"
            && std::env::var("LORE_TOOL_EVIDENCE").as_deref() != Ok("0")
            && !std::env::var("LORE_SKIP").is_ok_and(|value| !value.is_empty()))
        .then(|| Self {
            engine,
            client: Mutex::new(None),
            warned: AtomicBool::new(false),
        })
    }

    pub fn turn<'a>(&'a self, cwd: &'a Path, session: &'a str) -> Turn<'a> {
        Turn {
            recorder: self,
            cwd,
            session,
            turn: uuid::Uuid::new_v4().to_string(),
            routed_engine: None,
            tools: HashMap::new(),
            observations: BTreeMap::new(),
            count: 0,
        }
    }

    fn warn(&self, message: &str) {
        if !self.warned.swap(true, Ordering::Relaxed) {
            eprintln!("doxa-daemon: {message}");
        }
    }

    fn record(&self, cwd: &Path, session: &str, observations: BTreeMap<String, Vec<Value>>) {
        if observations.is_empty() {
            return;
        }
        let Some(cwd) = cwd.to_str() else { return };
        let mut client = self
            .client
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        if client.is_none() {
            *client = LoreClient::open_tool_evidence(Duration::from_millis(750)).ok();
        }
        let Some(carrier) = client.as_mut() else {
            self.warn("LORE tool evidence requires a carrier with tool-evidence support");
            return;
        };
        for (engine, rows) in observations {
            if carrier
                .record_tool_evidence(cwd, &engine, session, &rows)
                .is_err()
            {
                self.warn("LORE could not record tool evidence");
                if !carrier.is_alive() {
                    *client = None;
                }
                return;
            }
        }
    }
}

pub(crate) struct Turn<'a> {
    recorder: &'a Recorder,
    cwd: &'a Path,
    session: &'a str,
    turn: String,
    routed_engine: Option<String>,
    tools: HashMap<(String, String), String>,
    observations: BTreeMap<String, Vec<Value>>,
    count: usize,
}

fn token(value: &Value, limit: usize) -> Option<&str> {
    value.as_str().filter(|s| {
        !s.is_empty()
            && s.len() <= limit
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_-.:/".contains(&b))
    })
}

impl Turn<'_> {
    pub fn observe(&mut self, event: &Value) {
        let kind = event["type"].as_str();
        if self.recorder.engine == "router" && kind == Some("routing_selected") {
            self.routed_engine = event["data"]["engine"].as_str().map(str::to_owned);
            self.tools.clear();
            return;
        }
        if !matches!(kind, Some("tool_call" | "tool_result")) {
            return;
        }
        let data = &event["data"];
        let engine = if self.recorder.engine == "router" {
            self.routed_engine.as_deref().unwrap_or_default()
        } else {
            self.recorder.engine
        };
        if !matches!(
            engine,
            "claude" | "codex" | "antigravity" | "deepseek" | "glm"
        ) {
            return;
        }
        let Some(id) = token(&data["id"], 192) else {
            return;
        };
        let name = token(&data["evidence_tool"], 128).or_else(|| token(&data["name"], 128));
        let key = (engine.to_owned(), id.to_owned());
        if kind == Some("tool_call") {
            if let Some(name) = name.filter(|_| self.tools.len() < MAX_PER_TURN) {
                self.tools.entry(key).or_insert_with(|| name.to_owned());
            }
            return;
        }
        let started = self.tools.remove(&key);
        let Some(name) = name.or(started.as_deref()) else {
            return;
        };
        let outcome = if matches!(engine, "deepseek" | "glm") {
            data["is_error"]
                .as_bool()
                .map(|error| if error { "failure" } else { "success" })
        } else {
            data["evidence_outcome"]
                .as_str()
                .filter(|s| matches!(*s, "success" | "failure"))
        };
        let Some(outcome) = outcome else {
            return;
        };
        let call_id = format!("{}:{id}", self.turn);
        let rows = self.observations.entry(engine.to_owned()).or_default();
        if rows.iter().any(|row| row["call_id"] == call_id) {
            return;
        }
        if self.count >= MAX_PER_TURN {
            self.recorder
                .warn("LORE tool evidence limit reached for this turn");
            return;
        }
        rows.push(json!({"call_id":call_id,"tool":name,"outcome":outcome}));
        self.count += 1;
    }

    pub fn finish(self) {
        self.recorder
            .record(self.cwd, self.session, self.observations);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn routed_observations_bind_the_selected_worker_and_skip_unknown_outcomes() {
        let recorder = Recorder {
            engine: "router",
            client: Mutex::new(None),
            warned: AtomicBool::new(false),
        };
        let mut turn = recorder.turn(Path::new("/project"), "fixture");
        let result = json!({"type":"tool_result","data":{"id":"1","name":"command_execution",
            "evidence_outcome":"success","engine":"claude"}});
        turn.observe(&result);
        assert!(turn.observations.is_empty());
        turn.observe(&json!({"type":"routing_selected","data":{"engine":"codex"}}));
        turn.observe(&json!({"type":"tool_result","data":{"id":"2","name":"command_execution","is_error":false}}));
        assert!(turn.observations.is_empty());
        turn.observe(&result);
        turn.observe(&result);
        assert_eq!(turn.observations.len(), 1);
        let rows = &turn.observations["codex"];
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["outcome"], "success");
    }
}
