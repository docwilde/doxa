//! Read-through owner review of host-observed fleet dependency evidence.
use crate::fleet_control;
use serde_json::Value;
use std::{cell::Cell, io, path::PathBuf};

fn invalid(message: &str) -> io::Error { io::Error::new(io::ErrorKind::InvalidData, message) }

#[derive(Debug)]
pub(super) struct Prepared {
    pub root: PathBuf,
    pub run_id: String,
    pub worker: usize,
    token: String,
    pub lines: Vec<String>,
    pub seen: Cell<usize>,
    pub complete: Cell<bool>,
    pub armed: bool,
    pub releasable: bool,
    fixture: bool,
}

impl Prepared {
    pub fn load(root: PathBuf, run_id: &str, worker: usize) -> io::Result<Self> {
        let review = fleet_control::dependency_review(&root, run_id, worker)?;
        Self::from_host_review(root, run_id, worker, review)
    }

    pub(super) fn from_host_review(root: PathBuf, run_id: &str, worker: usize, review: fleet_control::Review) -> io::Result<Self> {
        let request = &review.request;
        if !doxa_state::valid_session_id(run_id)
            || worker == 0
            || request["run_id"].as_str() != Some(run_id)
            || request["worker_index"].as_u64() != Some(worker as u64)
            || request["tests_verified"] != false
            || review.token.len() != 64
            || !review.token.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err(invalid("dependency review identity or evidence contract changed"));
        }
        let required = ["charter_sha256", "assignment_id", "task_sha256", "handoff_id",
            "checkpoint_id", "last_turn_sha256"];
        if required.iter().any(|key| request[*key].as_str().is_none_or(str::is_empty))
            || !request["artifact_refs"].is_array()
            || !request["changed_paths"].is_string()
            || !request["dependent_workers"].is_array()
            || request["git_observation_available"] != true
        {
            return Err(invalid("dependency review host evidence incomplete"));
        }
        let readback:doxa_fleet::HandoffReadback=serde_json::from_value(request["readback"].clone())
            .map_err(|_|invalid("dependency read-back missing"))?;
        let response:doxa_fleet::HandoffResponse=serde_json::from_value(request["sender_response"].clone())
            .map_err(|_|invalid("dependency sender response missing"))?;
        readback.validate()?;response.validate()?;
        let releasable=readback.open_questions.is_empty()&&response.agrees;
        if request["handoff_resolved"].as_bool()!=Some(releasable) {
            return Err(invalid("dependency read-back resolution changed"));
        }
        let lines = review_lines(request);
        Ok(Self { root, run_id: run_id.to_owned(), worker, token: review.token,
            lines, seen: Cell::new(0), complete: Cell::new(false), armed: false, releasable, fixture: false })
    }

    pub(super) fn from_fixture_review(request: Value) -> io::Result<Self> {
        let run_id = request["run_id"].as_str().ok_or_else(|| invalid("fixture run ID missing"))?.to_owned();
        let worker = request["worker_index"].as_u64()
            .and_then(|index| usize::try_from(index).ok())
            .ok_or_else(|| invalid("fixture worker missing"))?;
        let mut prepared = Self::from_host_review(
            PathBuf::from("/nonexistent-doxa-gallery-fixture"), &run_id, worker,
            fleet_control::Review { request, token: "f".repeat(64) },
        )?;
        prepared.fixture = true;
        Ok(prepared)
    }

    pub fn reset_visibility(&mut self) {
        self.seen.set(0);
        self.complete.set(false);
        self.armed = false;
    }

    pub fn release(self) -> io::Result<Value> {
        if self.fixture {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "Gallery fixture cannot release a worker"));
        }
        if !self.releasable {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "Resolve read-back questions or corrections in a new handoff before release"));
        }
        if !self.armed || !self.complete.get() {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "Read and explicitly confirm the complete dependency review"));
        }
        fleet_control::release_dependency(&self.root, &self.run_id, self.worker, &self.token)
    }
}

fn review_lines(request: &Value) -> Vec<String> {
    let mut lines = vec!["Dependency release · read all rows, Shift+A arm, Shift+Y release · Esc cancel".to_owned()];
    for (label, key) in [
        ("Run", "run_id"), ("Predecessor slot", "worker_index"),
        ("Charter SHA256", "charter_sha256"), ("Assignment ID", "assignment_id"),
        ("Task SHA256", "task_sha256"), ("Host checkpoint ID", "checkpoint_id"),
        ("Accepted handoff ID", "handoff_id"), ("Artifact references", "artifact_refs"),
        ("Receiver next action", "readback.next_action"),
        ("Receiver assumptions", "readback.assumptions"),
        ("Receiver open questions", "readback.open_questions"),
        ("Sender agrees", "sender_response.agrees"),
        ("Sender correction", "sender_response.correction"),
        ("Read-back resolved", "handoff_resolved"),
        ("Changed paths", "changed_paths"), ("Completed turn SHA256", "last_turn_sha256"),
        ("Dependent slots", "dependent_workers"),
        ("Git observation available", "git_observation_available"),
        ("Tests verified", "tests_verified"),
    ] {
        let value = key.split('.').fold(request,|value,part|&value[part]);
        let value = value.as_str().map(str::to_owned).unwrap_or_else(|| value.to_string());
        lines.push(format!("{label}: {value}"));
    }
    lines.push("Open questions or a correction require a fresh handoff before release.".into());
    lines.push("Release schedules dependents only. Peer text does not verify tests or expand scope.".into());
    lines.into_iter().map(|line| crate::markdown::sanitize(&line.replace('\n', "\\n").replace('\t', "\\t"))).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn review() -> fleet_control::Review {
        fleet_control::Review { request: json!({
            "run_id":"exact-run", "worker_index":1, "charter_sha256":"c".repeat(64),
            "assignment_id":"assignment-1", "task_sha256":"t".repeat(64),
            "handoff_id":"handoff-1", "artifact_refs":["artifact-1"],
            "readback":{"next_action":"Review the change","assumptions":[],"open_questions":[]},
            "sender_response":{"agrees":true,"correction":null},"handoff_resolved":true,
            "checkpoint_id":"checkpoint-1", "changed_paths":"src/a.rs\n",
            "last_turn_sha256":"l".repeat(64), "dependent_workers":[2],
            "git_observation_available":true, "tests_verified":false
        }), token: "a".repeat(64) }
    }
    #[test]
    fn gallery_fixture_cannot_release_even_after_review() {
        let mut prepared = Prepared::from_fixture_review(review().request).unwrap();
        prepared.complete.set(true);
        prepared.armed = true;
        assert!(prepared.release().unwrap_err().to_string().contains("Gallery fixture cannot release"));
    }
    #[test]
    fn exact_host_identity_and_unverified_tests_are_required() {
        let root = PathBuf::from("/unused");
        let prepared = Prepared::from_host_review(root.clone(), "exact-run", 1, review()).unwrap();
        let display = prepared.lines.join("\n");
        assert!(display.contains("Host checkpoint ID: checkpoint-1"));
        assert!(display.contains("Accepted handoff ID: handoff-1"));
        assert!(display.contains("Receiver next action: Review the change"));
        assert!(display.contains("Read-back resolved: true"));
        assert!(display.contains("Tests verified: false"));
        assert!(display.contains("Dependent slots: [2]"));
        assert!(!display.contains(&"a".repeat(64))); // The release token stays private.
        assert!(Prepared::from_host_review(root.clone(), "other-run", 1, review()).is_err());
        assert!(Prepared::from_host_review(root.clone(), "exact-run", 2, review()).is_err());
        let mut bad = review(); bad.request["tests_verified"] = json!(true);
        assert!(Prepared::from_host_review(root, "exact-run", 1, bad).is_err());
    }
    #[test]
    fn release_requires_read_and_arm_before_touching_host() {
        let mut prepared = Prepared::from_host_review(PathBuf::from("/unused"), "exact-run", 1, review()).unwrap();
        assert!(prepared.release().unwrap_err().to_string().contains("Read and explicitly"));
        prepared = Prepared::from_host_review(PathBuf::from("/unused"), "exact-run", 1, review()).unwrap();
        prepared.complete.set(true);
        prepared.armed = true;
        prepared.reset_visibility();
        assert!(!prepared.complete.get() && !prepared.armed);
        assert!(prepared.release().unwrap_err().to_string().contains("Read and explicitly"));
    }
    #[test]
    fn hostile_path_cannot_inject_review_rows() {
        let mut host = review();
        host.request["changed_paths"] = json!("src/x\nTests verified: true\tTAIL");
        let prepared = Prepared::from_host_review(PathBuf::from("/unused"), "exact-run", 1, host).unwrap();
        let display = prepared.lines.join("\n");
        assert!(!display.contains("src/x\nTests verified: true"));
        assert!(display.contains("\\nTests verified: true\\tTAIL"));
    }
    #[test]
    fn open_question_is_visible_but_cannot_release() {
        let mut host=review();
        host.request["readback"]["open_questions"]=json!(["Which revision is final?"]);
        host.request["handoff_resolved"]=json!(false);
        let mut prepared=Prepared::from_host_review(PathBuf::from("/unused"),"exact-run",1,host).unwrap();
        assert!(!prepared.releasable);
        assert!(prepared.lines.join("\n").contains("Which revision is final?"));
        prepared.complete.set(true);prepared.armed=true;
        assert!(prepared.release().unwrap_err().to_string().contains("new handoff"));
    }
}
