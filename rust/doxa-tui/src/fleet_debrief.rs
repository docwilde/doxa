//! Read-only post-run audit from the native host manifest and private guard journal.
//! No peer message body or model-generated recommendation enters this report.
use crate::fleet_control;
use doxa_fleet::{Context, Kind, Mode, State};
use serde_json::Value;
use std::{collections::BTreeSet, io, path::Path};

fn invalid(message: &'static str) -> io::Error { io::Error::new(io::ErrorKind::InvalidData, message) }
fn label(value: &str) -> String { value.chars().filter(|ch| !ch.is_control()).take(80).collect() }

/// Exact run IDs only: a debrief never resolves a prefix to a different run.
pub fn read(root: &Path, id: &str) -> io::Result<String> {
    let manifest = fleet_control::snapshot(root, id)?;
    if manifest["native_version"] != 1 || manifest["run_id"] != id {
        return Err(invalid("debrief requires an exact native fleet run ID"));
    }
    if manifest["live"] != false || manifest["phase"] != "finished" {
        return Err(invalid("debrief requires a finished fleet run"));
    }
    let context_value = match manifest.as_object().and_then(|row| row.get("supervision")) {
        None => return Ok(format!("fleet {id} debrief\nHost guard journal: unavailable (unsupervised run)\nAssignment outcomes, handoffs, test receipts, judge events and spend: unknown")),
        Some(Value::Object(guard)) => guard.get("context")
            .ok_or_else(|| invalid("supervised fleet debrief is missing its host context"))?,
        Some(_) => return Err(invalid("invalid supervised fleet debrief journal")),
    };
    let context: Context = serde_json::from_value(context_value.clone())
        .map_err(|_| invalid("invalid debrief fleet context"))?;
    context.validate()?;
    if context.charter.fleet_id != id || context.state_path != root.join(id).join("guard-state.json") {
        return Err(invalid("debrief host journal identity changed"));
    }
    let state: State = doxa_fleet::read_private(&context.state_path, doxa_fleet::MAX_STATE)?;
    if state.charter_sha256 != context.charter_sha256
        || state.assignments_sha256 != doxa_fleet::hash(&context.assignments)? {
        return Err(invalid("debrief charter or assignment journal binding changed"));
    }
    render(id, &context, &state)
}

fn render(id: &str, context: &Context, state: &State) -> io::Result<String> {
    let mut completions = BTreeSet::new();
    for trace in state.traces.values().filter(|trace| trace.seq > 0 && trace.kind == Kind::Completion) {
        doxa_fleet::evidence::recorded_completion_receipts(context, state, &trace.from, &trace.artifact_refs)?;
        completions.insert(trace.from.as_str());
    }
    let mut lines = vec![format!("fleet {} debrief", label(id)),
        "Owner-private host journal summary; no peer text or recommendations".into(),
        "Assignments".into()];
    let mut complete = 0; let mut blocked = 0; let mut unknown = 0;
    for (slot, assignment) in context.assignments.iter().enumerate().filter(|(_, row)| row.role == "worker") {
        let outcome = if completions.contains(assignment.session_id.as_str()) {
            complete += 1; "completion admitted with recorded host receipts; current tree unknown"
        } else if !assignment.depends_on.is_empty()
            && !state.dispatched_assignments.get(&assignment.id).copied().unwrap_or(false) {
            blocked += 1; "blocked at dependency dispatch"
        } else if state.paused {
            blocked += 1; "blocked by current fleet pause"
        } else {
            unknown += 1; "outcome unknown"
        };
        lines.push(format!("  worker slot {slot}: {outcome}"));
    }
    lines.push(format!("Assignment counts: completion admitted {complete} · blocked {blocked} · unknown {unknown}"));

    let handoffs: Vec<_> = state.traces.iter()
        .filter(|(_, trace)| trace.seq > 0 && trace.kind == Kind::Handoff).collect();
    let mut readbacks = 0; let mut confirmations = 0; let mut agreed = 0;
    let mut corrections = 0; let mut open_questions = 0;
    for (handoff_id, handoff) in &handoffs {
        let ack = state.traces.iter().filter(|(_, trace)| trace.kind == Kind::Ack
            && trace.in_reply_to.as_deref() == Some(handoff_id.as_str())
            && trace.seq > handoff.seq && trace.readback.is_some())
            .max_by_key(|(_, trace)| trace.seq);
        let Some((ack_id, ack)) = ack else { continue; };
        readbacks += 1;
        open_questions += ack.readback.as_ref().map_or(0, |row| row.open_questions.len());
        // A confirmation is counted only for the exact ACK, never merely for
        // another message in the same worker/coordinator pair.
        let confirm = state.traces.values().filter(|trace|
            trace.kind == Kind::Confirm && trace.in_reply_to.as_deref() == Some(ack_id.as_str())
            && trace.seq > ack.seq && trace.handoff_response.is_some())
            .max_by_key(|trace| trace.seq);
        if let Some(confirm) = confirm {
            confirmations += 1;
            if let Some(response) = &confirm.handoff_response {
                if response.agrees && ack.readback.as_ref().is_some_and(|row| row.open_questions.is_empty()) {
                    agreed += 1;
                } else if !response.agrees { corrections += 1; }
            }
        }
    }
    lines.push(format!("Handoffs: {} · read-backs {readbacks} · confirmations {confirmations} · agreed {agreed} · corrections {corrections} · open questions {open_questions} · recorded human release entries {} (may be stale)",
        handoffs.len(), state.dependency_releases.len()));

    let mut tests = 0u64; let mut passed = 0u64; let mut failed = 0u64; let mut test_ms = 0u64;
    for (artifact_id, artifact) in &state.artifacts {
        if let Some(test) = doxa_fleet::evidence::recorded_test(context, artifact_id, artifact)? {
            tests += 1;
            if test.passed && test.exit_code == 0 { passed += 1; } else { failed += 1; }
            test_ms = test_ms.saturating_add(test.duration_ms);
        }
    }
    lines.push(format!("Host test receipts: {tests} · passed {passed} · failed {failed} · sum of recorded test durations {}",
        if tests == 0 { "unknown (no receipt)".into() } else { format!("{test_ms} ms") }));

    // The observation ring retains at most 256 entries. These are lower
    // bounds over that window, not inferred whole-run event totals.
    let mut quarantine_ids = BTreeSet::new(); let mut supervisor_holds = 0; let mut resumes = 0;
    for row in &state.observations {
        if row["event"] == "admission" && row["admission"]["delivered"] == false
            && row["admission"]["reason"].as_str().is_some_and(|reason| reason.starts_with("semantic review")) {
            if let Some(id) = row["admission"]["message_id"].as_str() { quarantine_ids.insert(id); }
        }
        if row["event"] == "outbound_semantic" && context.review.message_mode == Mode::Enforce {
            if let Ok(Ok(verdict)) = serde_json::from_value::<Result<doxa_fleet::SemanticVerdict, String>>(row["result"].clone()) {
                if verdict.validate().is_ok() && verdict.risky(context.review.risk_threshold) {
                    if let Some(id) = row["id"].as_str() { quarantine_ids.insert(id); }
                }
            }
        }
        if row["event"] == "supervisor_verdict" && context.review.supervisor_mode == Mode::Enforce
            && row["verdict"]["verdict"] != "aligned" { supervisor_holds += 1; }
        if row["event"] == "human_resume" { resumes += 1; }
    }
    lines.push(format!("Review events (retained {}/256): judge quarantine IDs {} · non-aligned supervisor verdicts {supervisor_holds} · human resumes {resumes}",
        state.observations.len(), quarantine_ids.len()));
    lines.push(format!("Current fleet pause: {}", if state.paused { "yes" } else { "no" }));
    if state.accounting_unknown {
        lines.push("Review token estimate: unknown (accounting flagged uncertain)".into());
    } else if state.calls > 0 {
        lines.push(format!("Recorded review token estimate: ${:.6} across {} reserved calls; actual billed cost unknown", state.actual_estimated_usd, state.calls));
    } else {
        lines.push("Recorded review token estimate: $0.000000 across 0 calls; actual billed cost unknown".into());
    }
    lines.push("Worker spend and run wall time: unknown (not recorded in the host journal)".into());
    Ok(lines.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use doxa_fleet::{Assignment, Charter, DependencyRelease, HandoffReadback, HandoffResponse,
        MessageTrace, ReviewConfig};
    use doxa_fleet::evidence::{Binding, DiffEvidence, TestEvidence, TestRecipe};
    use serde_json::json;
    use std::{fs, path::Path};
    use std::os::unix::fs::PermissionsExt;

    fn context(run: &Path) -> Context {
        let recipe = TestRecipe { argv: vec!["/usr/bin/true".into()], cwd_relative: String::new(), timeout_s: 5 };
        let charter = Charter { version: 1, fleet_id: "run".into(), task: "private task".into(), repo: "/repo".into(),
            allowed_paths: vec![String::new()], required_evidence: vec![], worker_limit: 2,
            run_budget_usd: Some(2.0), deadline: 0, human_actions: vec![], test_recipe: Some(recipe) };
        Context { charter_sha256: doxa_fleet::hash(&charter).unwrap(), charter,
            assignments: vec![
                Assignment { id: "run-0".into(), session_id: "coordinator".into(), pid: 1, role: "coordinator".into(),
                    task: "Coordinate".into(), cwd: "/repo".into(), base_commit: Some("a".repeat(40)), allowed_paths: vec![], depends_on: vec![] },
                Assignment { id: "run-1".into(), session_id: "worker-one".into(), pid: 2, role: "worker".into(),
                    task: "Part one".into(), cwd: "/repo".into(), base_commit: Some("a".repeat(40)), allowed_paths: vec![], depends_on: vec![] },
                Assignment { id: "run-2".into(), session_id: "worker-two".into(), pid: 3, role: "worker".into(),
                    task: "Part two".into(), cwd: "/repo".into(), base_commit: Some("a".repeat(40)), allowed_paths: vec![], depends_on: vec!["run-1".into()] },
            ], review: ReviewConfig::default(), state_path: run.join("guard-state.json") }
    }
    fn trace(from: &str, to: &str, seq: u64, kind: Kind, reply: Option<&str>) -> MessageTrace {
        MessageTrace { from: from.into(), to: to.into(), hop: if reply.is_some() { 1 } else { 0 }, seq, kind,
            artifact_refs: vec![], in_reply_to: reply.map(str::to_owned), readback: None, handoff_response: None }
    }
    #[test]
    fn debrief_uses_signed_host_receipts_and_typed_handoffs_without_peer_text() {
        let temp = tempfile::tempdir().unwrap();
        let run = temp.path().join("run"); fs::create_dir(&run).unwrap();
        fs::set_permissions(&run, fs::Permissions::from_mode(0o700)).unwrap();
        let context = context(&run); context.validate().unwrap();
        doxa_fleet::evidence::create_key(&context).unwrap();
        let binding = Binding { fleet_id: "run".into(), charter_sha256: context.charter_sha256.clone(),
            assignment_id: "run-1".into(), session_id: "worker-one".into(), base_commit: "a".repeat(40), snapshot_sha256: "b".repeat(64) };
        let diff = DiffEvidence { binding: binding.clone(), changed_paths: vec!["src/lib.rs".into()] };
        let (diff_id, diff_value) = doxa_fleet::evidence::issue(&context, "git_diff", json!(diff)).unwrap();
        let receipt = TestEvidence { binding, recipe_sha256: doxa_fleet::hash(context.charter.test_recipe.as_ref().unwrap()).unwrap(),
            runner_image: format!("image@sha256:{}", "c".repeat(64)), exit_code: 0, passed: true,
            duration_ms: 123, output_sha256: "d".repeat(64), output_bytes: 0 };
        let (test_id, test_value) = doxa_fleet::evidence::issue(&context, "test_result", json!(receipt)).unwrap();
        let mut state = State { charter_sha256: context.charter_sha256.clone(),
            assignments_sha256: doxa_fleet::hash(&context.assignments).unwrap(), ..State::default() };
        state.artifacts.insert(diff_id.clone(), diff_value);
        state.artifacts.insert(test_id.clone(), test_value);
        let mut completion = trace("worker-one", "coordinator", 1, Kind::Completion, None);
        completion.artifact_refs = vec![diff_id.clone(), test_id];
        state.traces.insert("completion".into(), completion);
        state.traces.insert("handoff".into(), trace("worker-one", "coordinator", 2, Kind::Handoff, None));
        let mut ack = trace("coordinator", "worker-one", 3, Kind::Ack, Some("handoff"));
        ack.readback = Some(HandoffReadback { next_action: "Review result".into(), assumptions: vec![], open_questions: vec![] });
        state.traces.insert("ack".into(), ack);
        let mut confirm = trace("worker-one", "coordinator", 4, Kind::Confirm, Some("ack"));
        confirm.handoff_response = Some(HandoffResponse { agrees: true, correction: None });
        state.traces.insert("confirm".into(), confirm);
        state.dependency_releases.insert("run-1".into(), DependencyRelease { assignment_id: "run-1".into(), handoff_id: "handoff".into(),
            artifact_refs: vec![], checkpoint_id: String::new(), checkpoint_turn_serial: 0, checkpoint_turn_sha256: String::new(),
            turn_serial: 0, last_turn_sha256: String::new(), at: 1 });
        state.observations.push(json!({"event":"admission","admission":{"delivered":false,"reason":"semantic review requires human review","message_id":"held"},"body":"secret peer text"}));
        state.calls = 2; state.actual_estimated_usd = 0.125;
        let report = render("run", &context, &state).unwrap();
        assert!(report.contains("completion admitted 1 · blocked 1 · unknown 0"));
        assert!(report.contains("worker slot 1: completion admitted"));
        assert!(report.contains("Handoffs: 1 · read-backs 1 · confirmations 1 · agreed 1"));
        assert!(report.contains("Host test receipts: 1 · passed 1 · failed 0 · sum of recorded test durations 123 ms"));
        assert!(report.contains("judge quarantine IDs 1"));
        assert!(report.contains("$0.125000"));
        assert!(!report.contains("secret peer text"));
        state.artifacts.remove(&diff_id);
        assert!(render("run", &context, &state).is_err(), "a partial completion receipt must not be reported as completed");
    }

    #[test]
    fn finished_debrief_is_read_only_and_refuses_changed_journal_identity() {
        let temp = tempfile::tempdir().unwrap();
        fs::set_permissions(temp.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let run = temp.path().join("run"); fs::create_dir(&run).unwrap();
        fs::set_permissions(&run, fs::Permissions::from_mode(0o700)).unwrap();
        let context = context(&run);
        let state = State { charter_sha256: context.charter_sha256.clone(),
            assignments_sha256: doxa_fleet::hash(&context.assignments).unwrap(), ..State::default() };
        doxa_fleet::save_private(&context.state_path, &state).unwrap();
        let manifest_path = run.join("manifest.json");
        fs::write(&manifest_path, serde_json::to_vec(&json!({"native_version":1,"run_id":"run","live":false,
            "phase":"finished","supervision":{"context":context}})).unwrap()).unwrap();
        fs::set_permissions(&manifest_path, fs::Permissions::from_mode(0o600)).unwrap();
        let before = fs::read(&manifest_path).unwrap(); let guard_before = fs::read(&context.state_path).unwrap();
        let report = read(temp.path(), "run").unwrap();
        assert!(report.contains("Assignment counts: completion admitted 0 · blocked 1 · unknown 1"));
        assert_eq!(fs::read(&manifest_path).unwrap(), before);
        assert_eq!(fs::read(&context.state_path).unwrap(), guard_before);
        let mut invalid = state; invalid.assignments_sha256 = "changed".into();
        doxa_fleet::save_private(&context.state_path, &invalid).unwrap();
        assert!(read(temp.path(), "run").is_err());
        fs::write(&context.state_path, b"{partial").unwrap();
        assert!(read(temp.path(), "run").is_err());
        fs::write(&manifest_path, b"{partial").unwrap();
        assert!(read(temp.path(), "run").is_err());
    }

    #[test]
    fn debrief_does_not_print_private_assignment_labels() {
        let temp = tempfile::tempdir().unwrap();
        let mut context = context(temp.path());
        context.assignments[1].id = "confidential user message".into();
        let state = State::default();
        let report = render("run", &context, &state).unwrap();
        assert!(report.contains("worker slot 1"));
        assert!(!report.contains("confidential user message"));
    }

    #[test]
    fn only_absent_supervision_is_reported_as_unsupervised() {
        let temp = tempfile::tempdir().unwrap();
        fs::set_permissions(temp.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let run = temp.path().join("run"); fs::create_dir(&run).unwrap();
        fs::set_permissions(&run, fs::Permissions::from_mode(0o700)).unwrap();
        let path = run.join("manifest.json");
        let base = json!({"native_version":1,"run_id":"run","live":false,"phase":"finished"});
        fs::write(&path, serde_json::to_vec(&base).unwrap()).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(read(temp.path(), "run").unwrap().contains("unsupervised run"));
        for malformed in [json!({}), json!({"context":null}), json!({"context":[]}),
            Value::Null, json!("off")] {
            let mut manifest = base.clone();
            manifest["supervision"] = malformed;
            fs::write(&path, serde_json::to_vec(&manifest).unwrap()).unwrap();
            assert!(read(temp.path(), "run").is_err(), "present malformed supervision must fail closed");
        }
    }
}
