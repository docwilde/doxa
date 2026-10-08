//! Bounded LORE picker data. The integrated canonical core owns search,
//! storage, and secret scrubbing; this module never opens LORE's store.

use doxa_lore::{BeliefAction, BeliefActionResult, BeliefReview, ConsultHit, LoreClient, LoreError, PendingDecision, PendingResolution, PendingReview};
use serde_json::Value;
#[cfg(test)]
use std::path::Path;
use std::time::Duration;

pub const PAGE_SIZE: u8 = 20;
pub const EVIDENCE_LIMIT: u8 = 20;

pub const FILTER_MAX_CHARS:usize=200;
pub const FILTER_MAX_BYTES:usize=1024;

pub fn append_filter_char(query:&mut String,ch:char)->bool {
    if query.chars().count()>=FILTER_MAX_CHARS || query.len()+ch.len_utf8()>FILTER_MAX_BYTES {return false;}
    query.push(ch);true
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Proposal {
    pub pid: String,
    pub kind: String,
    pub action: String,
    pub scope: String,
    pub summary: String,
    /// Source provenance from LORE's scrubbed pending row. This is not a
    /// current-pending signal: the list is paginated without a revision.
    pub source_session_id: Option<String>,
    pub source_project: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Belief {
    pub id: u64,
    pub subject: String,
    pub claim: String,
    pub truncated: bool,
    pub confidence: f64,
    pub evidence_count: Option<u64>,
    pub recency: Option<String>,
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
    ClusteredProposals(Vec<Proposal>, usize),
    Review(PendingReview, bool),
    Resolved(PendingResolution),
    BeliefReview(BeliefReview, bool),
    BeliefActed(BeliefActionResult),
}

#[derive(Clone, Debug)]
pub enum Query {
    Beliefs(u16),
    FilteredBeliefs(u16,String),
    Search(String),
    Evidence(u64),
    Proposals(String, u16),
    ClusteredProposals(String),
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

fn parse_cluster_proposals(rows: Vec<Value>) -> Result<Vec<Proposal>, ()> {
    if rows.len() > 4096 { return Err(()); }
    rows.chunks(PAGE_SIZE as usize).map(|chunk| parse_proposals(chunk.to_vec())).collect::<Result<Vec<_>, _>>().map(|groups| groups.into_iter().flatten().collect())
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
        let origin = |key| -> Result<Option<String>, ()> {
            match row.get(key) {
                None | Some(Value::Null) => Ok(None),
                Some(_) => short(row, key, 255)
                    .filter(|text| !text.is_empty() && !text.chars().any(char::is_control))
                    .map(Some).ok_or(()),
            }
        };
        let summary = ["text", "claim", "path", "name", "description", "reason"]
            .iter().find_map(|key| row.get(*key).and_then(Value::as_str))
            .unwrap_or("");
        if summary.len() > 4096 { return Err(()); }
        Ok(Proposal { pid, kind: field("kind")?, action: field("action")?,
            scope: field("scope")?, summary: summary.to_owned(),
            source_session_id: origin("session_id")?, source_project: origin("project")? })
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
            recency:match row.get("recency") {None|Some(Value::Null)=>None,Some(_)=>Some(short(row,"recency",64).ok_or(())?)},
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

/// Each query gets a native client. Call from a worker thread, never redraw.
pub fn fetch(query: Query) -> Result<ResultPage, &'static str> {
    fetch_with_client(LoreClient::open(Duration::from_secs(2)).map_err(|_| "LORE unavailable")?, query)
}

fn fetch_with_client(mut client: LoreClient, query: Query) -> Result<ResultPage, &'static str> {
    match query {
        Query::Beliefs(offset) => client.beliefs(offset, PAGE_SIZE)
            .map_err(|_| "Belief list unavailable")
            .and_then(|rows| parse_beliefs(rows).map(ResultPage::Beliefs).map_err(|_| "Invalid LORE belief reply")),
        Query::FilteredBeliefs(offset,query)=>client.beliefs_filtered(offset,PAGE_SIZE,&query)
            .map_err(|_|"Filtered belief list unavailable")
            .and_then(|rows|parse_beliefs(rows).map(ResultPage::Beliefs).map_err(|_|"Invalid LORE belief reply")),
        Query::Search(prompt) => client.consult(&prompt)
            .map(ResultPage::Search).map_err(|_| "LORE search unavailable"),
        Query::Evidence(id) => client.evidence(id, EVIDENCE_LIMIT)
            .map_err(|_| "Evidence unavailable")
            .and_then(|rows| parse_evidence(rows).map(|rows| ResultPage::Evidence(id, rows)).map_err(|_| "Invalid LORE evidence reply")),
        Query::Proposals(cwd, offset) => client.pending(&cwd, offset, PAGE_SIZE)
            .map_err(|_| "Proposal list unavailable")
            .and_then(|rows| parse_proposals(rows).map(ResultPage::Proposals).map_err(|_| "Invalid LORE proposal reply")),
        Query::ClusteredProposals(cwd) => {
            let clusters = client.pending_clustered(&cwd).map_err(|error| match error {
                LoreError::Remote("pending_cluster_unsupported") => "Clustered pending requires LORE 0.62.4 or newer",
                _ => "Clustered pending unavailable",
            })?;
            let count = clusters.memory_clusters.len();
            let mut rows = Vec::new();
            for (index, group) in clusters.memory_clusters.into_iter().enumerate() {
                for mut row in parse_cluster_proposals(group).map_err(|_| "Invalid clustered pending reply")? {
                    row.summary = format!("Cluster {} · {}", index + 1, row.summary);
                    rows.push(row);
                }
            }
            for mut row in parse_cluster_proposals(clusters.other).map_err(|_| "Invalid clustered pending reply")? {
                row.summary = format!("Other · {}", row.summary);
                rows.push(row);
            }
            Ok(ResultPage::ClusteredProposals(rows, count))
        }
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
pub(crate) fn fetch_fixture(python: &Path, query: Query) -> Result<ResultPage, &'static str> {
    fetch_with_client(LoreClient::spawn(python, Duration::from_secs(2)).map_err(|_| "Fixture unavailable")?, query)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[cfg(unix)]
    use std::{fs, os::unix::fs::PermissionsExt};


    #[test]
    fn filter_input_matches_canonical_character_and_byte_limits() {
        for ch in ['x','界','🦀'] {
            let mut query=String::new();
            for _ in 0..FILTER_MAX_CHARS {assert!(append_filter_char(&mut query,ch));}
            assert!(!append_filter_char(&mut query,ch));
            assert_eq!(query.chars().count(),FILTER_MAX_CHARS);
            assert!(query.len()<=FILTER_MAX_BYTES);
        }
    }

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
        assert_eq!(proposals[0].source_session_id, None);
        let sourced = parse_proposals(vec![json!({"pid":"one-2","kind":"memory","scope":"project","session_id":"session-7","project":"project-7"})]).unwrap();
        assert_eq!(sourced[0].source_session_id.as_deref(), Some("session-7"));
        assert_eq!(sourced[0].source_project.as_deref(), Some("project-7"));
        assert!(parse_proposals(vec![json!({"pid":"one-3","session_id":7})]).is_err());
        assert!(parse_proposals(vec![json!({"pid":"one-3","project":"wrong\nproject"})]).is_err());
        assert!(parse_proposals(vec![json!({"pid":"../outside","text":"unsafe"})]).is_err());
        assert!(parse_proposals(vec![json!({"pid":"one","text":"x".repeat(4097)})]).is_err());
    }

    #[test]
    fn retains_canonical_recency_and_order_without_id_guessing() {
        let rows=parse_beliefs(vec![json!({"id":7,"subject":"user","claim":"recent","claim_truncated":false,"confidence":0.8,"evidence_count":2,"recency":"2026-09-27T12:00:00Z"}),
            json!({"id":99,"subject":"user","claim":"older","claim_truncated":false,"confidence":0.8,"evidence_count":2,"recency":null})]).unwrap();
        assert_eq!(rows.iter().map(|row|row.id).collect::<Vec<_>>(),vec![7,99]);
        assert_eq!(rows[0].recency.as_deref(),Some("2026-09-27T12:00:00Z"));
        assert!(rows[1].recency.is_none());
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
        let ResultPage::Beliefs(rows) = fetch_fixture(&path, Query::Beliefs(0)).unwrap() else { panic!("belief list") };
        assert_eq!(rows[0].claim, "[redacted]");
        let ResultPage::Search(Some(hit)) = fetch_fixture(&path, Query::Search("hello".into())).unwrap() else { panic!("search") };
        assert_eq!(hit.id, 4);
        let ResultPage::Evidence(4, rows) = fetch_fixture(&path, Query::Evidence(4)).unwrap() else { panic!("evidence") };
        assert_eq!(rows[0].note, "[redacted]");

        let old = dir.path().join("old-sidecar");
        fs::write(&old, "#!/usr/bin/env python3\nprint('{\"type\":\"hello\",\"proto\":1,\"capabilities\":[\"scrub\",\"snapshot\"]}', flush=True)\n").unwrap();
        let mut perms = fs::metadata(&old).unwrap().permissions();
        perms.set_mode(0o700);
        fs::set_permissions(&old, perms).unwrap();
        assert!(fetch_fixture(&old, Query::Beliefs(0)).is_err());
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
        let ResultPage::Proposals(rows) = fetch_fixture(&path, Query::Proposals("/repo".into(), 0)).unwrap() else { panic!("proposals") };
        assert_eq!(rows[0].summary, "[redacted]");
        let ResultPage::Review(review, writable) = fetch_fixture(&path, Query::Review("/repo".into(), rows[0].pid.clone())).unwrap() else { panic!("review") };
        assert!(review.raw().contains("entire bytes"));
        assert!(!writable);
    }
}
