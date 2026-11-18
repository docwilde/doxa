//! Offline projection of recorded Choice responses bound to a frozen development batch.
use super::{hash, invalid, judge, prepare, read_private, write_new, Card, Plan};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{
    collections::{HashMap, HashSet},
    io,
    path::Path,
};

const MAX_CARDS: usize = 128;
const INPUT_BYTES: u64 = 2 * 1024 * 1024;
const BATCH_BYTES: u64 = 8 * 1024 * 1024;
const KIND: &str = "offline_checkpoint_request_batch";

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
enum Origin {
    Synthetic,
    Real,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Input {
    version: u8,
    origin: Origin,
    split: String,
    export_receipt_sha256: String,
    cards: Vec<Card>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Batch {
    version: u8,
    kind: String,
    origin: Origin,
    split: String,
    export_receipt_sha256: String,
    threshold: f64,
    cases: Vec<Plan>,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
enum ResponseProvenance {
    SyntheticFixture,
    RetainedPrivate,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RecordedBatch {
    version: u8,
    provenance: ResponseProvenance,
    records: Vec<Recorded>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Recorded {
    id: String,
    model: String,
    request_sha256: String,
    latency_ms: Option<u64>,
    response_json: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
enum Outcome {
    Decision {
        #[serde(flatten)]
        choice: judge::CheckpointChoice,
    },
    Unavailable,
}

#[derive(Debug, Serialize)]
struct Projected {
    id: String,
    model: String,
    request_sha256: String,
    latency_ms: Option<u64>,
    outcome: Outcome,
    usage: Option<judge::CheckpointChoiceUsage>,
}

fn prepare_batch(input: Input, threshold: f64) -> io::Result<Batch> {
    if input.version != 1 || input.split != "development" {
        return Err(invalid(
            "offline checkpoint request batches accept version 1 development input only",
        ));
    }
    if !(1..=MAX_CARDS).contains(&input.cards.len()) {
        return Err(invalid("offline checkpoint batch needs 1–128 cards"));
    }
    let digest = &input.export_receipt_sha256;
    if digest.len() != 64
        || !digest
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(invalid(
            "offline checkpoint batch needs the export receipt digest",
        ));
    }
    let mut ids = HashSet::new();
    let mut snapshots = HashSet::new();
    let mut cases = Vec::new();
    for card in input.cards {
        let plan = prepare(card, threshold)?;
        if !ids.insert(plan.card.id.clone()) || !snapshots.insert(plan.snapshot_sha256.clone()) {
            return Err(invalid(
                "offline checkpoint batch repeats a card ID or snapshot",
            ));
        }
        cases.push(plan);
    }
    Ok(Batch {
        version: 1,
        kind: KIND.into(),
        split: input.split,
        origin: input.origin,
        export_receipt_sha256: input.export_receipt_sha256,
        threshold,
        cases,
    })
}

fn verify_batch(batch: Batch, digest: &str) -> io::Result<Batch> {
    if hash(&batch)? != digest {
        return Err(invalid(
            "checkpoint batch digest differs from the frozen digest",
        ));
    }
    let input = Input {
        version: batch.version,
        split: batch.split,
        origin: batch.origin,
        export_receipt_sha256: batch.export_receipt_sha256,
        cards: batch.cases.into_iter().map(|case| case.card).collect(),
    };
    let rebuilt = prepare_batch(input, batch.threshold)?;
    if hash(&rebuilt)? != digest {
        return Err(invalid(
            "current production builder disagrees with the frozen checkpoint batch",
        ));
    }
    Ok(rebuilt)
}

fn project_batch(batch: Batch, recorded: RecordedBatch) -> io::Result<Vec<Projected>> {
    let provenance = match batch.origin {
        Origin::Synthetic => ResponseProvenance::SyntheticFixture,
        Origin::Real => ResponseProvenance::RetainedPrivate,
    };
    if recorded.version != 1
        || recorded.provenance != provenance
        || recorded.records.len() != 2 * batch.cases.len()
    {
        return Err(invalid(
            "recorded response provenance or case coverage differs from the checkpoint batch",
        ));
    }
    let mut index = HashMap::new();
    for record in recorded.records {
        if record.latency_ms.is_some_and(|ms| ms > 120000) {
            return Err(invalid("recorded checkpoint latency exceeds bounds"));
        }
        if index
            .insert((record.id.clone(), record.model.clone()), record)
            .is_some()
        {
            return Err(invalid(
                "recorded response repeats a card and candidate model",
            ));
        }
    }
    let mut rows = Vec::with_capacity(index.len());
    for case in batch.cases {
        for arm in case.arms {
            let record = index
                .remove(&(case.card.id.clone(), arm.model.clone()))
                .ok_or_else(|| {
                    invalid("missing response entry for the frozen card and candidate model")
                })?;
            if record.request_sha256 != arm.request_sha256 {
                return Err(invalid(
                    "recorded response does not match the frozen production request hash",
                ));
            }
            let model = judge::Model::parse(&arm.model)?;
            let observation = record.response_json.as_deref().and_then(|raw| {
                judge::checkpoint_choice_observation(&model, &arm.request, raw.as_bytes()).ok()
            });
            let (outcome, usage) = match observation {
                Some(obs) => (
                    Outcome::Decision {
                        choice: obs.projection,
                    },
                    Some(obs.usage),
                ),
                None => (Outcome::Unavailable, None),
            };
            rows.push(Projected {
                id: record.id,
                model: arm.model,
                request_sha256: arm.request_sha256,
                latency_ms: record.latency_ms,
                outcome,
                usage,
            });
        }
    }
    Ok(rows)
}

pub(super) fn freeze(source: &Path, threshold: f64, destination: &Path) -> io::Result<()> {
    let batch = prepare_batch(read_private(source, INPUT_BYTES)?, threshold)?;
    write_new(destination, &batch, BATCH_BYTES)?;
    println!(
        "{}",
        json!({"plan_sha256":hash(&batch)?, "cards":batch.cases.len(), "model_calls":0})
    );
    Ok(())
}

pub(super) fn project(
    plan: &Path,
    digest: &str,
    responses: &Path,
    destination: &Path,
) -> io::Result<()> {
    let batch = verify_batch(read_private(plan, BATCH_BYTES)?, digest)?;
    let recorded: RecordedBatch = read_private(responses, BATCH_BYTES)?;
    let origin = batch.origin;
    let threshold = batch.threshold;
    let export_receipt_sha256 = batch.export_receipt_sha256.clone();
    let provenance = recorded.provenance;
    let responses_sha256 = hash(&recorded)?;
    let rows = project_batch(batch, recorded)?;
    write_new(
        destination,
        &json!({
            "version":1, "split":"development", "origin":origin, "provenance":provenance,
            "threshold":threshold, "plan_sha256":digest, "export_receipt_sha256":export_receipt_sha256,
            "responses_sha256":responses_sha256, "rows":rows,
        }),
        INPUT_BYTES,
    )?;
    println!("{}", json!({"rows":rows.len(), "model_calls":0}));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Map, Value};

    fn input() -> Input {
        let card =
            include_str!("../../../../scripts/fleet-evaluation/checkpoint-request-fixture.jsonl");
        let a: Card = serde_json::from_str(card).unwrap();
        let mut b: Card = serde_json::from_str(card).unwrap();
        b.id = uuid::Uuid::from_u128(2).to_string();
        b.snapshot.phase = json!("paused");
        Input {
            version: 1,
            split: "development".into(),
            origin: Origin::Synthetic,
            export_receipt_sha256: "3".repeat(64),
            cards: vec![a, b],
        }
    }

    fn make_response(request: &Value, tie: bool) -> Value {
        let mut answers = Map::new();
        for (name, selected) in [
            ("alignment", "aligned"),
            ("basis", "within_charter"),
            ("evidence", "host-example"),
        ] {
            let criteria = request["questions"][name]["criteria"].as_object().unwrap();
            let n = criteria.len() as f64;
            let tied = tie && name == "alignment";
            let maximum = if tied { 0.5 } else { 0.99 };
            let other = criteria
                .keys()
                .find(|key| key.as_str() != selected)
                .unwrap();
            let probabilities: Map<String, Value> = criteria
                .keys()
                .map(|key| {
                    let probability = if key == selected || (tied && key == other) {
                        maximum
                    } else if tied {
                        0.0
                    } else {
                        (1.0 - maximum) / (n - 1.0)
                    };
                    (key.clone(), json!(probability))
                })
                .collect();
            answers.insert(
                name.to_owned(),
                json!({"type":"choice", "choice":selected,
                "probabilities":probabilities, "confidence":(maximum - 1.0/n)/(1.0 - 1.0/n)}),
            );
        }
        json!({"model":request["model"], "answers":answers, "usage":{"input_tokens":615, "output_tokens":0}})
    }

    fn records(batch: &Batch) -> RecordedBatch {
        let records = batch
            .cases
            .iter()
            .flat_map(|case| {
                case.arms.iter().map(move |arm| Recorded {
                    id: case.card.id.clone(),
                    model: arm.model.clone(),
                    request_sha256: arm.request_sha256.clone(),
                    latency_ms: Some(120),
                    response_json: Some(make_response(&arm.request, true).to_string()),
                })
            })
            .collect();
        RecordedBatch {
            version: 1,
            provenance: ResponseProvenance::SyntheticFixture,
            records,
        }
    }

    #[test]
    fn matches_both_models_in_frozen_order_and_only_projects_validated_observations() {
        let batch = prepare_batch(input(), 0.75).unwrap();
        let mut recorded = records(&batch);
        let expected_first_id = recorded.records[0].id.clone();
        recorded.records[1].latency_ms = None;
        recorded.records[2].response_json = Some(r#"{"usage":1,"usage":2}"#.to_owned());
        recorded.records[3].response_json = None;
        recorded.records.reverse();
        let projection = serde_json::to_value(project_batch(batch, recorded).unwrap()).unwrap();
        assert_eq!(projection[0]["id"], expected_first_id);
        assert_eq!(projection[0]["model"], "jev:jev-1.13.0");
        assert_eq!(projection[0]["outcome"]["guard_confidence"][0], 0.0);
        assert!(
            projection[0]["outcome"]["alignment_confidence"]
                .as_f64()
                .unwrap()
                > 0.0
        );
        assert_eq!(projection[0]["outcome"]["supported"], true);
        assert_eq!(projection[1]["model"], "subq:subq-snap-1.2");
        assert_eq!(projection[1]["latency_ms"], Value::Null);
        assert_eq!(
            projection[1]["usage"],
            json!({"input_tokens":615, "output_tokens":0})
        );
        assert_eq!(projection[2]["outcome"], json!({"status":"unavailable"}));
        assert_eq!(projection[2]["usage"], Value::Null);
        assert_eq!(projection[2]["latency_ms"], 120);
        assert_eq!(projection[3]["outcome"], json!({"status":"unavailable"}));
        assert!(!projection.to_string().contains("Synthetic parser fixture"));
        assert!(!projection.to_string().contains("answers"));
        assert!(!projection.to_string().contains("label_source"));
    }

    #[test]
    fn refuses_holdout_repeated_snapshots_and_rehashed_edited_plan() {
        let mut holdout = input();
        holdout.split = "holdout".into();
        assert!(prepare_batch(holdout, 0.75).is_err());
        let mut repeated = input();
        repeated.cards[1].snapshot.phase = repeated.cards[0].snapshot.phase.clone();
        assert!(prepare_batch(repeated, 0.75).is_err());
        let mut batch = prepare_batch(input(), 0.75).unwrap();
        let digest = hash(&batch).unwrap();
        assert!(verify_batch(prepare_batch(input(), 0.75).unwrap(), &digest).is_ok());
        batch.cases[0].arms[0].request["state"]["phase"] = json!("changed");
        batch.cases[0].arms[0].request_sha256 = hash(&batch.cases[0].arms[0].request).unwrap();
        let rehashed = hash(&batch).unwrap();
        assert!(verify_batch(batch, &rehashed).is_err());
    }

    #[test]
    fn refuses_mismatched_provenance_missing_rows_repeated_models_and_wrong_request_hashes() {
        for invalid in 0..5 {
            let batch = prepare_batch(input(), 0.75).unwrap();
            let mut recorded = records(&batch);
            match invalid {
                0 => recorded.provenance = ResponseProvenance::RetainedPrivate,
                1 => {
                    recorded.records.pop();
                }
                2 => recorded.records[1].model = recorded.records[0].model.clone(),
                3 => {
                    recorded.records[1].request_sha256 = recorded.records[0].request_sha256.clone()
                }
                _ => recorded.records[1].latency_ms = Some(120001),
            }
            assert!(project_batch(batch, recorded).is_err());
        }
    }
}
