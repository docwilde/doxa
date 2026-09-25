//! Bounded LORE picker data. The external sidecar owns search,
//! storage, and secret scrubbing; this module never opens LORE's store.

use doxa_lore::{BeliefAction, BeliefActionResult, BeliefReview, ConsultHit, LoreClient, LoreError, PendingDecision, PendingResolution, PendingReview};
use serde_json::Value;
use std::path::Path;
use std::time::Duration;

pub const PAGE_SIZE: u8 = 20;
pub const EVIDENCE_LIMIT: u8 = 20;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Proposal {
    pub pid: String,
    pub kind: String,
    pub action: String,
    pub scope: String,
    pub summary: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Belief {
    pub id: u64,
    pub subject: String,
    pub claim: String,
    pub truncated: bool,
    pub confidence: f64,
    pub evidence_count: Option<u64>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Evidence {
    pub session_id: String,
    pub project: String,
    pub note: String,
    pub truncated: bool,
    pub created: String,
    pub source_engine: Option<String>,
    pub trail_truncated: bool,
}

#[derive(Debug)]
pub enum ResultPage {
    Beliefs(Vec<Belief>),
    Search(Option<ConsultHit>),
    Evidence(u64, Vec<Evidence>),
    Proposals(Vec<Proposal>),
    Review(PendingReview, bool),
    Resolved(PendingResolution),
    BeliefReview(BeliefReview, bool),
    BeliefActed(BeliefActionResult),
}

#[derive(Clone, Debug)]
pub enum Query {
    Beliefs(u16),
    Search(String),
    Evidence(u64),
    Proposals(String, u16),
    Review(String, String),
    Resolve(String, PendingReview, PendingDecision),
    BeliefReview(String, u64),
    BeliefAction(String, BeliefReview, BeliefAction, String),
}

fn belief_error(error: LoreError) -> &'static str {
    match error {
        LoreError::Remote("belief_changed") => "Belief changed; reopen a fresh exact review",
        LoreError::Remote("belief_unavailable") => "Belief unavailable; refresh the list",
        LoreError::Remote("belief_incomplete") => "Complete belief review unavailable; actions disabled",
        _ => "LORE belief action outcome unknown; inspect LORE before retrying",
    }
}

fn short(value: &Value, key: &str, max: usize) -> Option<String> {
    value.get(key)?.as_str().filter(|text| text.len() <= max).map(str::to_owned)
}

pub fn parse_proposals(rows: Vec<Value>) -> Result<Vec<Proposal>, ()> {
    if rows.len() > PAGE_SIZE as usize { return Err(()); }
    rows.iter().map(|row| {
        let pid = short(row, "pid", 128).ok_or(())?;
        if pid.is_empty() || !pid.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_') { return Err(()); }
        let field = |key| -> Result<String, ()> {
            match row.get(key) {
                None => Ok(String::new()),
                Some(_) => short(row, key, 4096).ok_or(()),
            }
        };
        let summary = ["text", "claim", "path", "name", "description", "reason"]
            .iter().find_map(|key| row.get(*key).and_then(Value::as_str))
            .unwrap_or("");
        if summary.len() > 4096 { return Err(()); }
        Ok(Proposal { pid, kind: field("kind")?, action: field("action")?,
            scope: field("scope")?, summary: summary.to_owned() })
    }).collect()
}

pub fn parse_beliefs(rows: Vec<Value>) -> Result<Vec<Belief>, ()> {
    if rows.len() > PAGE_SIZE as usize { return Err(()); }
    rows.iter().map(|row| {
        Ok(Belief {
            id: row["id"].as_u64().filter(|id| *id > 0).ok_or(())?,
            subject: short(row, "subject", 4096).ok_or(())?,
            claim: short(row, "claim", 4096).ok_or(())?,
            truncated: row["claim_truncated"].as_bool().ok_or(())?,
            confidence: row["confidence"].as_f64().filter(|n| n.is_finite() && (0.0..=1.0).contains(n)).ok_or(())?,
            evidence_count: Some(row["evidence_count"].as_u64().ok_or(())?),
        })
    }).collect()
}

pub fn parse_evidence(rows: Vec<Value>) -> Result<Vec<Evidence>, ()> {
    if rows.len() > EVIDENCE_LIMIT as usize { return Err(()); }
    rows.iter().map(|row| {
        Ok(Evidence {
            session_id: short(row, "session_id", 4096).ok_or(())?,
            project: short(row, "project", 4096).ok_or(())?,
            note: short(row, "note", 4096).ok_or(())?,
            truncated: row["note_truncated"].as_bool().ok_or(())?,
            created: short(row, "created", 4096).ok_or(())?,
            source_engine: if row.get("source_engine").is_some() { Some(short(row, "source_engine", 4096).ok_or(())?) } else { None },
            trail_truncated: match row.get("trail_truncated") { Some(value) => value.as_bool().ok_or(())?, None => false },
        })
    }).collect()
}

/// Each query gets a short-lived sidecar so a closed picker cannot leave a
/// background process holding memory. Call from a worker thread, never redraw.
pub fn fetch(python: &Path, query: Query) -> Result<ResultPage, &'static str> {
    let mut client = LoreClient::spawn(python, Duration::from_secs(2)).map_err(|_| "LORE unavailable")?;
    match query {
        Query::Beliefs(offset) => client.beliefs(offset, PAGE_SIZE)
            .map_err(|_| "Belief list unavailable")
            .and_then(|rows| parse_beliefs(rows).map(ResultPage::Beliefs).map_err(|_| "Invalid LORE belief reply")),
        Query::Search(prompt) => client.consult(&prompt)
            .map(ResultPage::Search).map_err(|_| "LORE search unavailable"),
        Query::Evidence(id) => client.evidence(id, EVIDENCE_LIMIT)
            .map_err(|_| "Evidence unavailable")
            .and_then(|rows| parse_evidence(rows).map(|rows| ResultPage::Evidence(id, rows)).map_err(|_| "Invalid LORE evidence reply")),
        Query::Proposals(cwd, offset) => client.pending(&cwd, offset, PAGE_SIZE)
            .map_err(|_| "Proposal list unavailable")
            .and_then(|rows| parse_proposals(rows).map(ResultPage::Proposals).map_err(|_| "Invalid LORE proposal reply")),
        Query::Review(cwd, pid) => {
            let writable = client.can_resolve_reviewed();
            client.pending_review(&cwd, &pid)
                .map(|review| ResultPage::Review(review, writable))
                .map_err(|_| "Complete proposal review unavailable")
        }
        Query::Resolve(cwd, review, decision) => client.resolve_reviewed(&cwd, &review, decision)
            .map(ResultPage::Resolved).map_err(|_| "Proposal resolution unavailable"),
        Query::BeliefReview(cwd, id) => {
            let writable = client.can_act_on_beliefs();
            client.belief_review(&cwd, id)
                .map(|review| ResultPage::BeliefReview(review, writable))
                .map_err(|error| match error {
                    LoreError::Remote("belief_unavailable") => "Belief unavailable; refresh the list",
                    _ => "Complete belief review unavailable; actions disabled",
                })
        }
        Query::BeliefAction(cwd, review, action, note) => client.belief_action(&cwd, &review, action, &note)
            .map(ResultPage::BeliefActed).map_err(belief_error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[cfg(unix)]
    use std::{fs, os::unix::fs::PermissionsExt};

    #[test]
    fn parses_bounded_scrubbed_rows() {
        let belief = parse_beliefs(vec![json!({"id":1,"subject":"user","claim":"safe","claim_truncated":false,"confidence":0.8,"evidence_count":2})]).unwrap();
        assert_eq!(belief[0].id, 1);
        assert!(parse_beliefs(vec![json!({"id":0,"subject":"x","claim":"x","claim_truncated":false,"confidence":0.8,"evidence_count":0})]).is_err());
        let evidence = parse_evidence(vec![json!({"session_id":"s","project":"p","note":"n","note_truncated":false,"created":"2026","source_engine":"codex","trail_truncated":true})]).unwrap();
        assert!(evidence[0].trail_truncated);
        assert!(parse_evidence(vec![json!({"session_id":"s","project":"p","note":"n","note_truncated":false,"created":"2026","trail_truncated":"yes"})]).is_err());
        let proposals = parse_proposals(vec![json!({"pid":"one-1","kind":"memory","action":"add","scope":"user","text":"[redacted]"})]).unwrap();
        assert_eq!(proposals[0].summary, "[redacted]");
        assert!(parse_proposals(vec![json!({"pid":"../outside","text":"unsafe"})]).is_err());
        assert!(parse_proposals(vec![json!({"pid":"one","text":"x".repeat(4097)})]).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn uses_only_lore_read_capabilities() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sidecar");
        fs::write(&path, r##"#!/usr/bin/env python3
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','beliefs','consult','evidence']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    assert req['op'] in ('beliefs', 'consult', 'evidence')
    if req['op'] == 'beliefs':
        value = [{'id':4,'subject':'user','claim':'[redacted]','claim_truncated':False,'confidence':0.9,'evidence_count':1}]
    elif req['op'] == 'consult':
        value = {'id':4,'claim':'[redacted]','claim_truncated':False,'confidence':0.9,'score':-1.0,'citation_status':'cite_only'}
    else:
        value = [{'session_id':'s','project':'p','note':'[redacted]','note_truncated':False,'created':'2026'}]
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"##).unwrap();
        let mut perms = fs::metadata(&path).unwrap().permissions();
        perms.set_mode(0o700);
        fs::set_permissions(&path, perms).unwrap();
        let ResultPage::Beliefs(rows) = fetch(&path, Query::Beliefs(0)).unwrap() else { panic!("belief list") };
        assert_eq!(rows[0].claim, "[redacted]");
        let ResultPage::Search(Some(hit)) = fetch(&path, Query::Search("hello".into())).unwrap() else { panic!("search") };
        assert_eq!(hit.id, 4);
        let ResultPage::Evidence(4, rows) = fetch(&path, Query::Evidence(4)).unwrap() else { panic!("evidence") };
        assert_eq!(rows[0].note, "[redacted]");

        let old = dir.path().join("old-sidecar");
        fs::write(&old, "#!/usr/bin/env python3\nprint('{\"type\":\"hello\",\"proto\":1,\"capabilities\":[\"scrub\",\"snapshot\"]}', flush=True)\n").unwrap();
        let mut perms = fs::metadata(&old).unwrap().permissions();
        perms.set_mode(0o700);
        fs::set_permissions(&old, perms).unwrap();
        assert!(fetch(&old, Query::Beliefs(0)).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn proposal_picker_reads_complete_snapshot_without_write_request() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sidecar");
        fs::write(&path, r##"#!/usr/bin/env python3
import hashlib, json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','pending','pending_review_v1']}), flush=True)
raw = '{"kind":"sync","op":{"signed":"entire bytes"}}\n'
for line in sys.stdin:
    req = json.loads(line)
    assert req['op'] in ('pending', 'pending_review_v1')
    assert req['cwd'] == '/repo'
    if req['op'] == 'pending':
        value = [{'pid':'one','kind':'sync','scope':'user','text':'[redacted]'}]
    else:
        assert req['pid'] == 'one'
        value = {'pid':'one','raw':raw,'sha256':hashlib.sha256(raw.encode()).hexdigest(),'inode':7,'complete':True}
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"##).unwrap();
        let mut perms = fs::metadata(&path).unwrap().permissions();
        perms.set_mode(0o700);
        fs::set_permissions(&path, perms).unwrap();
        let ResultPage::Proposals(rows) = fetch(&path, Query::Proposals("/repo".into(), 0)).unwrap() else { panic!("proposals") };
        assert_eq!(rows[0].summary, "[redacted]");
        let ResultPage::Review(review, writable) = fetch(&path, Query::Review("/repo".into(), rows[0].pid.clone())).unwrap() else { panic!("review") };
        assert!(review.raw().contains("entire bytes"));
        assert!(!writable);
    }
}
