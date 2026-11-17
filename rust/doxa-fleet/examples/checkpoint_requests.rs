//! Freeze both production Choice request bodies from one supplied scrubbed card.
//! The example has no transport, credential resolution or fleet state access.
use doxa_fleet::{hash, invalid, judge, read_private};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    env, fs,
    io::{self, Write},
    os::unix::fs::MetadataExt,
    path::Path,
};

const MODELS: [&str; 2] = ["jev:jev-1.13.0", "subq:subq-snap-1.2"];
const KIND: &str = "offline_checkpoint_request_fixture";
const MAX_PLAN_BYTES: u64 = 256 * 1024;

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    charter: serde_json::Map<String, Value>,
    assignments: Vec<Value>,
    artifacts: Vec<Value>,
    guard_observations: Vec<Value>,
    recent_handoff_evidence: Vec<Value>,
    budget: serde_json::Map<String, Value>,
    elapsed_deadline: Value,
    phase: Value,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Card {
    version: u8,
    id: String,
    snapshot: Snapshot,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Arm {
    model: String,
    request_sha256: String,
    request: Value,
}

#[derive(Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Plan {
    version: u8,
    kind: String,
    threshold: f64,
    snapshot_sha256: String,
    card: Card,
    arms: [Arm; 2],
}

fn prepare(card: Card, threshold: f64) -> io::Result<Plan> {
    let id = uuid::Uuid::parse_str(&card.id).map_err(|_| invalid("invalid checkpoint card ID"))?;
    if card.version != 1 || id.to_string() != card.id {
        return Err(invalid("use a version 1 card with a canonical opaque UUID"));
    }
    let snapshot = serde_json::to_value(&card.snapshot)?;
    let build = |name| {
        let model = judge::Model::parse(name)?;
        let request = judge::checkpoint_choice_request(&model, &snapshot, threshold)?;
        Ok::<_, io::Error>(Arm {
            model: model.display(),
            request_sha256: hash(&request)?,
            request,
        })
    };
    Ok(Plan {
        version: 1,
        kind: KIND.into(),
        threshold,
        snapshot_sha256: hash(&snapshot)?,
        card,
        arms: [build(MODELS[0])?, build(MODELS[1])?],
    })
}

fn freeze(path: &Path, plan: &Plan) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| invalid("plan needs a parent directory"))?;
    let meta = fs::symlink_metadata(parent)?;
    if !path.is_absolute()
        || !meta.is_dir()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.mode() & 0o077 != 0
        || parent.canonicalize()? != parent
    {
        return Err(invalid(
            "use a canonical owner-private directory for the new plan",
        ));
    }
    let raw = serde_json::to_vec(plan)?;
    if raw.len() as u64 >= MAX_PLAN_BYTES {
        return Err(invalid("checkpoint plan exceeds bounds"));
    }
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    file.write_all(&raw)?;
    file.write_all(b"\n")?;
    file.as_file().sync_all()?;
    file.persist_noclobber(path).map_err(|err| err.error)?;
    fs::File::open(parent)?.sync_all()
}

fn verify(raw_plan: Plan, expected: &str) -> io::Result<Plan> {
    if hash(&raw_plan)? != expected {
        return Err(invalid(
            "checkpoint request plan digest differs from the frozen digest",
        ));
    }
    // Restore the very same snapshot for each arm. Checking only the supplied
    // request digests would allow a modified request and digest to agree.
    let rebuilt = prepare(raw_plan.card, raw_plan.threshold)?;
    if hash(&rebuilt)? != expected {
        return Err(invalid(
            "current production builder disagrees with the frozen request plan",
        ));
    }
    Ok(rebuilt)
}

fn run() -> io::Result<()> {
    let args: Vec<_> = env::args().collect();
    let plan = match args.as_slice() {
        [_, command, card, threshold, destination] if command == "freeze" => {
            let threshold = threshold.parse().map_err(|_| invalid("invalid checkpoint threshold"))?;
            let plan = prepare(read_private(Path::new(card), judge::MAX_INPUT as u64)?, threshold)?;
            freeze(Path::new(destination), &plan)?;
            plan
        }
        [_, command, plan, digest] if command == "verify" => {
            verify(read_private(Path::new(plan), MAX_PLAN_BYTES)?, digest)?
        }
        _ => return Err(invalid(
            "Usage: checkpoint_requests freeze PRIVATE_CARD_JSON THRESHOLD ABSOLUTE_NEW_PLAN | verify PRIVATE_PLAN_JSON EXACT_PLAN_SHA256"
        )),
    };
    println!(
        "{}",
        json!({
            "plan_sha256": hash(&plan)?,
            "snapshot_sha256": plan.snapshot_sha256,
            "models": MODELS,
            "model_calls": 0
        })
    );
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn card() -> Card {
        serde_json::from_str(include_str!(
            "../../../scripts/fleet-evaluation/checkpoint-request-fixture.jsonl"
        ))
        .unwrap()
    }

    #[test]
    fn same_host_snapshot_drives_both_production_requests() {
        let plan = prepare(card(), 0.75).unwrap();
        let snapshot = serde_json::to_value(&plan.card.snapshot).unwrap();
        let [jev, snap] = &plan.arms;
        assert_eq!(plan.snapshot_sha256, hash(&snapshot).unwrap());
        assert_eq!(jev.request["state"], snapshot);
        assert_eq!(
            jev.request["questions"]["alignment"]["instructions"]["minimum_confidence"],
            0.75
        );
        let mut snap_state = snap.request["state"].clone();
        assert_eq!(
            snap_state
                .as_object_mut()
                .unwrap()
                .remove("doxa_confidence_guard"),
            Some(json!(0.75))
        );
        assert_eq!(snap_state, snapshot);
        for arm in [&jev, &snap] {
            assert_eq!(arm.request_sha256, hash(&arm.request).unwrap());
            assert!(arm.request["questions"]["evidence"]["criteria"]["host-example"].is_string());
        }
    }

    #[test]
    fn plan_detects_changed_snapshot_guard_or_rehashed_request() {
        let mut plan = prepare(card(), 0.75).unwrap();
        let digest = hash(&plan).unwrap();
        assert_eq!(
            verify(prepare(card(), 0.75).unwrap(), &digest).unwrap(),
            plan
        );
        assert!(verify(prepare(card(), 0.5).unwrap(), &digest).is_err());
        let mut changed = card();
        changed.snapshot.phase = json!("paused");
        assert!(verify(prepare(changed, 0.75).unwrap(), &digest).is_err());
        plan.arms[0].request["state"]["phase"] = json!("paused");
        plan.arms[0].request_sha256 = hash(&plan.arms[0].request).unwrap();
        let edited_digest = hash(&plan).unwrap();
        assert!(verify(plan, &edited_digest).is_err());
    }

    #[test]
    fn rejects_invalid_thresholds_and_unsuitable_artifacts_before_freezing() {
        assert!(prepare(card(), f64::NAN).is_err());
        assert!(prepare(card(), 1.1).is_err());
        let mut invalid_card = card();
        invalid_card
            .snapshot
            .artifacts
            .push(json!({"id":"host-example","evidence":{}}));
        assert!(prepare(invalid_card, 0.75).is_err());
        let snapshot = serde_json::to_value(card().snapshot).unwrap();
        let other = judge::Model::parse("deepseek:deepseek-flash").unwrap();
        assert!(judge::checkpoint_choice_request(&other, &snapshot, 0.75).is_err());
    }

    #[test]
    fn output_requires_private_directory_and_cannot_replace_a_frozen_plan() {
        let root = tempfile::tempdir().unwrap();
        let output = root.path().join("plan.json");
        let mut card_with_host_float = card();
        card_with_host_float
            .snapshot
            .budget
            .insert("review_reserved_usd".into(), json!(51.248178375505404));
        let plan = prepare(card_with_host_float, 0.75).unwrap();
        fs::set_permissions(root.path(), fs::Permissions::from_mode(0o755)).unwrap();
        assert!(freeze(&output, &plan).is_err());
        fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
        freeze(&output, &plan).unwrap();
        assert_eq!(fs::metadata(&output).unwrap().mode() & 0o777, 0o600);
        assert_eq!(read_private::<Plan>(&output, MAX_PLAN_BYTES).unwrap(), plan);
        assert!(freeze(&output, &prepare(card(), 0.5).unwrap()).is_err());
        assert_eq!(read_private::<Plan>(&output, MAX_PLAN_BYTES).unwrap(), plan);
    }
}
