use doxa_router::{Config,evaluation,Reason};
use serde_json::{json,Value};
use std::sync::atomic::AtomicBool;
fn config()->Config {serde_json::from_str(include_str!("../fixtures/config.example.json")).unwrap()}
fn cases()->&'static str {include_str!("../fixtures/synthetic-cases.jsonl")}
#[test]
fn recorded_synthetic_evaluation_reports_confusion_hashes_cost_and_unknown_worker_quality() {
    let cfg=config();let recorded=evaluation::fixture_recordings(&cfg,cases()).unwrap();
    let report=evaluation::evaluate(&cfg,&recorded,false,None,&AtomicBool::new(false)).unwrap();
    assert_eq!(report.development.cases,4);assert_eq!(report.holdout.cases,2);assert_eq!(report.holdout.errors,0);
    assert_eq!(report.model,"jev-1.13.0");assert_eq!(report.ledger.calls,6);assert_eq!(report.router_cost_usd_micros,Some(30));
    assert!(report.total_cost_usd_micros.is_none() && report.worker_execution_cost_usd_micros.is_none());assert!(!report.real_quality_validated);
    let mut rows=evaluation::parse_cases(&recorded).unwrap();rows[4].recording.as_mut().unwrap().response.as_mut().unwrap()["answers"]["target"]["choice"]=json!("deliberate");
    let text=rows.iter().map(|row|serde_json::to_string(row).unwrap()).collect::<Vec<_>>().join("\n");
    let out=evaluation::evaluate(&cfg,&text,false,None,&AtomicBool::new(false)).unwrap();assert_eq!(out.holdout.errors,1);assert_eq!(out.holdout.transport_or_schema_failures,1);
    assert_eq!(out.results[4].outcome.reason,Reason::InvalidResponse);assert!(out.results[4].outcome.usage.is_some());
}
#[test]
fn leakage_wrong_provenance_raw_duplicate_and_unbound_recordings_are_refused() {
    let cfg=config();let recorded=evaluation::fixture_recordings(&cfg,cases()).unwrap();
    for altered in [cases().replace("hold-group","dev-group"),cases().replacen("\"machine\"","\"human\"",1),
        cases().replacen("\"synthetic\"","\"real\"",1),cases().replacen("\"id\":\"extract\"","\"id\":\"extract\",\"id\":\"extract\"",1)] {
        assert!(evaluation::parse_cases(&altered).is_err());
    }
    let mut rows=evaluation::parse_cases(&recorded).unwrap();rows[0].recording.as_mut().unwrap().criteria_sha256="0".repeat(64);
    let text=rows.iter().map(|row|serde_json::to_string(row).unwrap()).collect::<Vec<_>>().join("\n");
    assert!(evaluation::evaluate(&cfg,&text,false,None,&AtomicBool::new(false)).is_err());
    assert!(evaluation::evaluate(&cfg,&recorded,true,Some("fixture-key"),&AtomicBool::new(false)).is_err());
    assert!(evaluation::evaluate(&cfg,cases(),false,None,&AtomicBool::new(false)).is_err());
}
#[test]
fn missing_credential_is_measured_zero_calls_and_a_reported_limitation() {
    let report=evaluation::evaluate(&config(),cases(),true,None,&AtomicBool::new(false)).unwrap();
    assert_eq!(report.live_status,"missing_credential");assert_eq!(report.ledger.calls,0);assert_eq!(report.router_cost_usd_micros,Some(0));
    assert!(report.results.iter().all(|row|!row.outcome.attempted));
    let serialized:Value=serde_json::to_value(&report).unwrap();assert_eq!(serialized["total_cost_usd_micros"],Value::Null);
}
