//! Reproducible recorded-response evaluation and explicitly opt-in synthetic smoke.
use crate::{Config, Ledger, Outcome, Prepared, Reason, Reservation, RoutingInput, Usage, JEV_MODEL};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{collections::{BTreeMap, BTreeSet}, io, path::Path, sync::atomic::AtomicBool};

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all="snake_case")]
pub enum Split { Development, Holdout }
#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all="snake_case")]
pub enum Origin { Synthetic, Real }

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Recording {
    pub config_sha256: String,
    pub criteria_sha256: String,
    pub request_sha256: String,
    pub response: Option<Value>,
    pub transport_error: Option<Reason>,
    pub latency_ms: u64,
    pub attempted: bool,
    /// Retain reported usage for a rejected answer without retaining invalid prose.
    pub rejected_usage: Option<Usage>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Case {
    pub version: u32,
    pub id: String,
    pub group_id: String,
    pub split: Split,
    pub origin: Origin,
    pub label_source: String,
    pub consented: bool,
    pub scrubbed: bool,
    pub input: RoutingInput,
    pub expected_candidate_id: String,
    pub recording: Option<Recording>,
}

#[derive(Debug, Serialize)]
pub struct CaseResult {
    pub id: String,
    pub split: Split,
    pub expected_candidate_id: String,
    pub correct: bool,
    pub criteria_sha256: String,
    pub reservation: Option<Reservation>,
    pub outcome: Outcome,
}

#[derive(Debug, Serialize)]
pub struct Metrics {
    pub cases: usize,
    pub correct: usize,
    pub errors: usize,
    pub error_rate: f64,
    pub fallback_count: usize,
    pub transport_or_schema_failures: usize,
    pub p50_ms: u64,
    pub p95_ms: u64,
    pub max_ms: u64,
    pub confusion: BTreeMap<String,BTreeMap<String,usize>>,
}

#[derive(Debug, Serialize)]
pub struct Report {
    pub version: u32,
    pub mode: String,
    pub model: String,
    pub origin: Origin,
    pub label_source: String,
    pub config_sha256: String,
    pub input_sha256: String,
    pub ledger: Ledger,
    pub development: Metrics,
    pub holdout: Metrics,
    pub router_cost_usd_micros: Option<u64>,
    pub worker_execution_cost_usd_micros: Option<u64>,
    pub total_cost_usd_micros: Option<u64>,
    pub real_quality_validated: bool,
    pub live_status: String,
    pub results: Vec<CaseResult>,
    /// Synthetic live records are replayable offline. No raw invalid response bodies.
    pub recordings: Vec<Case>,
    pub note: &'static str,
}

fn invalid(message: &'static str) -> io::Error { io::Error::new(io::ErrorKind::InvalidData,message) }

pub fn load_cases(path: &Path) -> io::Result<(String,Vec<Case>)> {
    let text=crate::read_private(path,2*1024*1024)?;
    let cases=parse_cases(&text)?;
    Ok((text,cases))
}

pub fn parse_cases(text: &str) -> io::Result<Vec<Case>> {
    if text.len()>2*1024*1024 { return Err(invalid("router evaluation exceeds 2 MiB")); }
    let mut cases=Vec::new();let mut ids=BTreeSet::new();let mut groups=BTreeMap::new();
    let mut provenance=None;
    for line in text.lines().filter(|line|!line.trim().is_empty()) {
        if cases.len()>=1000 { return Err(invalid("router evaluation exceeds 1000 rows")); }
        let value=crate::strict::parse(line.as_bytes()).map_err(|_|invalid("invalid router evaluation JSON"))?;
        let row:Case=serde_json::from_value(value).map_err(|_|invalid("invalid router evaluation schema"))?;
        if row.version!=1 || !crate::identifier(&row.id) || !crate::identifier(&row.group_id)
            || !ids.insert(row.id.clone()) || !row.scrubbed || !row.consented
            || !matches!((row.origin,row.label_source.as_str()),(Origin::Synthetic,"machine")|(Origin::Real,"human")) {
            return Err(invalid("invalid evaluation identity, consent or label provenance"));
        }
        if groups.insert(row.group_id.clone(),row.split).is_some_and(|old|old!=row.split) {
            return Err(invalid("one evaluation group cannot cross development and holdout"));
        }
        if provenance.is_some_and(|old|old!=row.origin) { return Err(invalid("real and synthetic evaluation must be separate")); }
        provenance=Some(row.origin);cases.push(row);
    }
    if !cases.iter().any(|row|row.split==Split::Development)||!cases.iter().any(|row|row.split==Split::Holdout) {
        return Err(invalid("router evaluation requires separate development and holdout groups"));
    }
    Ok(cases)
}

fn metrics(results: &[CaseResult], split: Split) -> Metrics {
    let rows:Vec<_>=results.iter().filter(|row|row.split==split).collect();
    let correct=rows.iter().filter(|row|row.correct).count();
    let mut latency:Vec<_>=rows.iter().map(|row|row.outcome.latency_ms).collect();latency.sort_unstable();
    let mut confusion=BTreeMap::<String,BTreeMap<String,usize>>::new();
    for row in &rows { *confusion.entry(row.expected_candidate_id.clone()).or_default()
        .entry(row.outcome.candidate_id.clone()).or_default()+=1; }
    Metrics {cases:rows.len(),correct,errors:rows.len()-correct,error_rate:(rows.len()-correct) as f64/rows.len() as f64,
        fallback_count:rows.iter().filter(|row|row.outcome.reason!=Reason::Selected).count(),
        transport_or_schema_failures:rows.iter().filter(|row|matches!(row.outcome.reason,Reason::Unavailable|Reason::InvalidResponse|Reason::Cancelled)).count(),
        p50_ms:latency[(latency.len()*50).div_ceil(100)-1],p95_ms:latency[(latency.len()*95).div_ceil(100)-1],
        max_ms:*latency.last().unwrap(),confusion }
}

fn recorded(config: &Config, prepared: &Prepared, record: &Recording) -> io::Result<Outcome> {
    if record.config_sha256!=prepared.config_sha256 || record.criteria_sha256!=prepared.criteria_sha256
        || record.request_sha256!=prepared.request_sha256 || record.latency_ms>120_000 {
        return Err(invalid("recorded response does not bind exact model criteria configuration and input"));
    }
    match (&record.response,&record.transport_error) {
        (Some(response),None) if record.attempted && record.rejected_usage.is_none()=>Ok(crate::parse_response(config,prepared,response,record.latency_ms)),
        (None,Some(reason)) if matches!(reason,Reason::Unavailable|Reason::InvalidResponse|Reason::Cancelled|Reason::Budget|Reason::AccountingUnknown|Reason::SingleEligible)=>{
            if !record.attempted && record.rejected_usage.is_some() { return Err(invalid("uncalled router cannot report token usage")); }
            let mut out=crate::fallback(config,prepared,reason.clone());out.attempted=record.attempted;out.latency_ms=record.latency_ms;
            out.usage=record.rejected_usage.clone();
            out.router_cost_usd_micros=if !record.attempted {Some(0)} else {out.usage.as_ref().and_then(|usage|
                crate::token_cost(usage.input_tokens,0,crate::JEV_INPUT_USD_MICROS_PER_MILLION,0).ok())};
            if out.usage.is_some() {out.model=Some(JEV_MODEL.into());}
            Ok(out)
        },
        _=>Err(invalid("recorded response must contain exactly one response or transport failure")),
    }
}

/// Live is restricted to predeclared machine-authored synthetic cases, <=20
/// requests and <=$0.01 cumulative reservations. It never runs a worker provider.
pub fn evaluate(config: &Config, text: &str, live: bool, key: Option<&str>, cancel: &AtomicBool) -> io::Result<Report> {
    evaluate_observed(config,text,live,key,cancel,|_,_,_|Ok(()))
}

/// The live CLI uses this hook to sync every reservation before HTTP and every
/// bounded outcome afterward. An observer failure prevents the next call.
pub fn evaluate_observed(config: &Config, text: &str, live: bool, key: Option<&str>, cancel: &AtomicBool,
    mut observer: impl FnMut(&Ledger,Option<&Reservation>,Option<&Outcome>)->io::Result<()>) -> io::Result<Report> {
    config.validate()?;
    let cases=parse_cases(text)?;
    if live && (cases.len()>20 || cases.iter().any(|row|row.origin!=Origin::Synthetic || row.recording.is_some())) {
        return Err(invalid("live router evaluation accepts at most 20 unrecorded synthetic cases"));
    }
    // Freeze and validate all eligibility and labels before any paid request.
    let prepared=cases.iter().map(|row| {
        let prepared=crate::prepare(config,&row.input)?;
        if !prepared.eligible_ids.contains(&row.expected_candidate_id) {return Err(invalid("evaluation label is not an eligible candidate"));}
        if !live && row.recording.is_none() {return Err(invalid("offline router evaluation requires recorded responses"));}
        Ok(prepared)
    }).collect::<io::Result<Vec<_>>>()?;
    let mut ledger=Ledger::default();let mut results=Vec::new();let mut recordings=Vec::new();
    for (case,prepared) in cases.iter().zip(prepared.iter()) {
        let mut reservation=None;
        let outcome=if live && key.is_none() {
            crate::fallback(config,prepared,Reason::Unavailable)
        } else if live && ledger.held_usd_micros().saturating_add(prepared.reservation_usd_micros)>10_000 {
            crate::fallback(config,prepared,Reason::Budget)
        } else {
            match crate::reserve(config,&mut ledger,prepared) {
                Ok(hold)=>{
                    observer(&ledger,Some(&hold),None)?;
                    let out=if live { crate::call(config,prepared,key.unwrap(),cancel) }
                        else {recorded(config,prepared,case.recording.as_ref().unwrap())?};
                    crate::settle(&mut ledger,&hold,&out)?;
                    reservation=Some(hold);out
                },
                Err(_)=>crate::fallback(config,prepared,if ledger.accounting_unknown{Reason::AccountingUnknown}else{Reason::Budget}),
            }
        };
        observer(&ledger,reservation.as_ref(),Some(&outcome))?;
        if cancel.load(std::sync::atomic::Ordering::Acquire) { return Err(invalid("router evaluation cancelled; inspect durable call journal")); }
        if live {
            let mut row=case.clone();
            row.recording=Some(Recording{config_sha256:prepared.config_sha256.clone(),criteria_sha256:prepared.criteria_sha256.clone(),
                request_sha256:prepared.request_sha256.clone(),response:outcome.verified_response.clone(),
                transport_error:if outcome.verified_response.is_none(){Some(outcome.reason.clone())}else{None},
                latency_ms:outcome.latency_ms,attempted:outcome.attempted,rejected_usage:if outcome.verified_response.is_none(){outcome.usage.clone()}else{None}});
            recordings.push(row);
        }
        results.push(CaseResult{id:case.id.clone(),split:case.split,expected_candidate_id:case.expected_candidate_id.clone(),
            correct:outcome.reason!=Reason::Cancelled && outcome.candidate_id==case.expected_candidate_id,
            criteria_sha256:prepared.criteria_sha256.clone(),reservation,outcome});
    }
    let router_cost_usd_micros=if ledger.accounting_unknown {None}else{Some(ledger.actual_usd_micros)};
    Ok(Report {version:1,mode:if live{"live_synthetic"}else{"offline_recorded"}.into(),model:JEV_MODEL.into(),
        origin:cases[0].origin,label_source:cases[0].label_source.clone(),config_sha256:config.hash()?,input_sha256:format!("{:x}",sha2::Sha256::digest(text.as_bytes())),
        development:metrics(&results,Split::Development),holdout:metrics(&results,Split::Holdout),ledger,
        router_cost_usd_micros,worker_execution_cost_usd_micros:None,total_cost_usd_micros:None,
        real_quality_validated:false,live_status:if !live{"not_requested"}else if key.is_none(){"missing_credential"}
            else if results.iter().all(|row|matches!(row.outcome.reason,Reason::Selected|Reason::LowConfidence|Reason::SingleEligible)){"completed"}else{"partial_failure"}.into(),results,recordings,
        note:"Exploratory router-label agreement only. Machine-authored synthetic labels are not human holdout or real routing quality. Confidence measures Choice distribution concentration, not worker success. Worker execution/quality and total cost are unmeasured; usage-based router cost is not an invoice. Real labels/consent/scrubbing are operator attestations."})
}

use sha2::Digest;

/// Artificial responses make offline tests reproducible, never real measurements.
pub fn fixture_recordings(config: &Config, text: &str) -> io::Result<String> {
    let cases=parse_cases(text)?;
    if cases.iter().any(|row|row.origin!=Origin::Synthetic||row.recording.is_some()) {
        return Err(invalid("fixture generation accepts unrecorded synthetic cases only"));
    }
    let rows=cases.into_iter().map(|mut row| {
        let prepared=crate::prepare(config,&row.input)?;
        if !prepared.eligible_ids.contains(&row.expected_candidate_id){return Err(invalid("synthetic label is ineligible"));}
        let probabilities=prepared.eligible_ids.iter().map(|id|(id.clone(),json!(if id==&row.expected_candidate_id{1.0}else{0.0})))
            .collect::<serde_json::Map<_,_>>();
        row.recording=Some(Recording{config_sha256:prepared.config_sha256,criteria_sha256:prepared.criteria_sha256,
            request_sha256:prepared.request_sha256,response:Some(json!({"model":JEV_MODEL,"answers":{"target":{"type":"choice", "choice":row.expected_candidate_id,"probabilities":probabilities,"confidence":1.0}},"usage":{"input_tokens":100,"output_tokens":20}})),
            transport_error:None,latency_ms:10,attempted:true,rejected_usage:None});
        serde_json::to_string(&row).map_err(io::Error::other)
    }).collect::<io::Result<Vec<_>>>()?;
    Ok(rows.join("\n"))
}
