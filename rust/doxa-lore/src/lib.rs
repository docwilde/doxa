//! Bounded client for canonical native LORE. Production uses an in-process
//! Core; explicitly selected sidecars remain available for protocol fixtures.
//! LORE owns memory, authority, locks and scrubbing. DOXA retains its result
//! validation and never silently persists unsanitized text.

mod native_config;
pub mod stream;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::io::{self, Read, Write};
#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub const PROTOCOL_VERSION: u64 = 1;
pub const MAX_FRAME_BYTES: usize = 1024 * 1024;
const MAX_MEMORY_CHARS: u64 = 1024 * 1024;

#[derive(Debug)]
pub struct PendingClusters {
    pub memory_clusters: Vec<Vec<Value>>,
    pub other: Vec<Value>,
}

/// Source-scoped pending IDs from one consistent LORE store snapshot. A
/// missing or incomplete row cannot prove that a session has zero proposals.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingSession {
    pub session_id: String,
    pub pending_pids: Vec<String>,
    pub complete: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingSessions {
    pub source_project_slug: String,
    pub snapshot: String,
    pub complete: bool,
    pub sessions: Vec<PendingSession>,
}

/// LORE's curated project file map. It is read-only, has no source hash, and
/// cannot establish that a current codegraph file has this purpose.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileMapEntry {
    pub path: String,
    pub purpose: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileMap {
    pub key: String,
    pub cap_chars: u64,
    pub entries: Vec<FileMapEntry>,
}

/// LORE's local, owner-reviewed syntax snapshot. The requested source is
/// verified by LORE; DOXA rechecks included references and, when present, the
/// complete enumerated Rust scan-input inventory. Bindings remain unknown.
#[derive(Debug, Clone, PartialEq)]
pub enum CodegraphSnapshot {
    Missing,
    Current(StoredCodegraph),
}

#[derive(Debug, Clone, PartialEq)]
pub struct StoredCodegraph {
    pub project_key: String,
    pub worktree_root: String,
    pub query: String,
    pub path: String,
    pub revision: u64,
    pub source_sha256: String,
    pub graph_sha256: String,
    pub graph: Value,
    pub referenced_sources: ReferenceFreshness,
    pub scan_inputs: ScanInputFreshness,
}

/// Read-time verification of the producer's complete Git-listed Rust input
/// inventory. This does not prove semantic binding or an atomic source view.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScanInputFreshness {
    pub status: &'static str,
    pub checked_files: usize,
    pub reason: &'static str,
}

impl ScanInputFreshness {
    fn check(cwd: &str, graph: &Value) -> Self {
        let Some(expected) = graph.get("scan_input_sha256").and_then(Value::as_str) else {
            return Self { status: "unknown", checked_files: 0,
                reason: "scan_input_digest_absent" };
        };
        if !valid_digest(expected) {
            return Self { status: "unknown", checked_files: 0,
                reason: "invalid_scan_input_digest" };
        }
        let coverage = &graph["coverage"];
        let complete = coverage["skipped"]["count"].as_u64() == Some(0)
            && coverage["unparseable"]["count"].as_u64() == Some(0);
        let Some(expected_files) = coverage["parsed_rust_files"].as_u64() else {
            return Self { status: "unknown", checked_files: 0,
                reason: "scan_coverage_uncheckable" };
        };
        if !complete {
            return Self { status: "unknown", checked_files: 0,
                reason: "scan_incomplete" };
        }
        match doxa_codegraph::current_scan_input_sha256(Path::new(cwd)) {
            Ok((actual, checked_files)) => Self {
                status: if actual == expected && checked_files as u64 == expected_files {
                    "verified" } else { "stale" },
                checked_files,
                reason: if actual == expected && checked_files as u64 == expected_files {
                    "matched" } else { "scan_inputs_changed" },
            },
            Err(_) => Self { status: "unknown", checked_files: 0,
                reason: "scan_inputs_uncheckable" },
        }
    }
}

/// Read-time byte checks for files included in the stored answer. This says
/// nothing about omitted rows, scan coverage, or semantic Rust bindings.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReferenceFreshness {
    pub status: &'static str,
    pub checked_files: usize,
    pub issues: Vec<ReferenceIssue>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReferenceIssue {
    pub path: String,
    pub reason: &'static str,
}

const MAX_SNAPSHOT_REFERENCES: usize = 1024;
const MAX_REFERENCE_ISSUES: usize = 8;

#[derive(Default)]
struct ReferenceCheck {
    expected: BTreeMap<String, String>,
    issues: Vec<ReferenceIssue>,
    unknown: bool,
    stale: bool,
}

impl ReferenceCheck {
    fn issue(&mut self, path: &str, reason: &'static str, stale: bool) {
        if stale { self.stale = true; } else { self.unknown = true; }
        if self.issues.len() < MAX_REFERENCE_ISSUES {
            self.issues.push(ReferenceIssue {
                path: path.chars().take(128).collect(), reason,
            });
        }
    }

    fn record(&mut self, path: Option<&Value>, digest: Option<&Value>) {
        let Some(path) = path.and_then(Value::as_str) else {
            self.issue("<missing>", "missing_reference_path", false);
            return;
        };
        if path.is_empty() || path.len() > 4096 || !path.ends_with(".rs")
            || path.chars().any(char::is_control)
            || !Path::new(path).components().all(|part|
                matches!(part, std::path::Component::Normal(_))) {
            self.issue(path, "unsafe_reference_path", false);
            return;
        }
        let Some(digest) = digest.and_then(Value::as_str).filter(|digest| valid_digest(digest)) else {
            self.issue(path, "missing_reference_hash", false);
            return;
        };
        if let Some(previous) = self.expected.get(path) {
            if previous != digest { self.issue(path, "conflicting_reference_hash", false); }
        } else if self.expected.len() < MAX_SNAPSHOT_REFERENCES {
            self.expected.insert(path.to_owned(), digest.to_owned());
        } else {
            self.issue(path, "reference_limit", false);
        }
    }

    fn finish(mut self, cwd: &str) -> ReferenceFreshness {
        let mut checked_files = 0;
        for (path, expected) in std::mem::take(&mut self.expected) {
            match doxa_codegraph::source_sha256(Path::new(cwd), &path) {
                Ok(actual) => {
                    checked_files += 1;
                    if actual != expected { self.issue(&path, "source_hash_changed", true); }
                }
                Err(_) => self.issue(&path, "source_uncheckable", false),
            }
        }
        ReferenceFreshness { status: if self.stale { "stale" } else if self.unknown {
            "unknown" } else { "verified" }, checked_files, issues: self.issues }
    }
}

impl ReferenceFreshness {
    fn check(cwd: &str, graph: &Value) -> Self {
        let mut check = ReferenceCheck::default();
        match graph["rows"].as_array() {
            Some(rows) => for row in rows {
                check.record(row.get("file"), row.get("sha256"));
            },
            None => check.issue("<rows>", "malformed_reference_section", false),
        }
        match graph["edges"].as_array() {
            Some(edges) => for edge in edges {
                check.record(edge.get("file"), edge.get("sha256"));
                match edge.get("candidates").and_then(Value::as_array) {
                    Some(candidates) => for candidate in candidates {
                        check.record(candidate.get("file"), candidate.get("sha256"));
                    },
                    None => check.issue("<edge>", "missing_candidates", false),
                }
            },
            None => check.issue("<edges>", "malformed_reference_section", false),
        }
        match graph["module_edges"].as_array() {
            Some(edges) => for edge in edges {
                check.record(edge.get("source"), edge.get("source_sha256"));
                for (path_key, hash_key) in [("target", "target_sha256"),
                    ("conditional_candidate", "conditional_candidate_sha256")] {
                    if edge.get(path_key).is_some_and(|value| !value.is_null()) {
                        check.record(edge.get(path_key), edge.get(hash_key));
                    } else if edge.get(hash_key).is_some_and(|value| !value.is_null()) {
                        check.issue("<module>", "hash_without_reference_path", false);
                    }
                }
            },
            None => check.issue("<module_edges>", "malformed_reference_section", false),
        }
        check.finish(cwd)
    }
}

impl CodegraphSnapshot {
    fn parse(value: Value, cwd: &str, query: &str, path: &str) -> Result<Self, LoreError> {
        if serde_json::to_vec(&value).map_or(true, |bytes| bytes.len() > 128 * 1024) {
            return Err(LoreError::InvalidFrame);
        }
        let object = value.as_object().ok_or(LoreError::InvalidFrame)?;
        match object.get("status").and_then(Value::as_str) {
            Some("missing") if object.len() == 1 => Ok(Self::Missing),
            Some("current") if object.len() == 12 => {
                let project_key = object.get("project_key").and_then(Value::as_str)
                    .filter(|key| *key == lore_core::config::project_slug(Path::new(cwd)))
                    .ok_or(LoreError::InvalidFrame)?.to_owned();
                let worktree_root = object.get("worktree_root").and_then(Value::as_str)
                    .filter(|root| *root == cwd).ok_or(LoreError::InvalidFrame)?.to_owned();
                if object.get("schema_version") != Some(&json!(1))
                    || object.get("query") != Some(&json!(query))
                    || object.get("path") != Some(&json!(path))
                    || object.get("binding") != Some(&json!("unknown"))
                    || object.get("freshness") != Some(&json!("requested_source_verified_only")) {
                    return Err(LoreError::InvalidFrame);
                }
                let revision = object.get("revision").and_then(Value::as_u64)
                    .filter(|revision| *revision > 0).ok_or(LoreError::InvalidFrame)?;
                let source_sha256 = object.get("source_sha256").and_then(Value::as_str)
                    .filter(|digest| valid_digest(digest)).ok_or(LoreError::InvalidFrame)?.to_owned();
                let graph_sha256 = object.get("graph_sha256").and_then(Value::as_str)
                    .filter(|digest| valid_digest(digest)).ok_or(LoreError::InvalidFrame)?.to_owned();
                let graph = object.get("graph").filter(|graph| graph.is_object()
                    && graph["scope"] == cwd && graph["query"] == query && graph["value"] == path
                    && graph["status"] == "ok" && graph["requested_source_sha256"] == source_sha256
                    && graph["requested_source_read_unix_ms"].as_u64().is_some()
                    && graph["coverage"].is_object() && graph["rows"].is_array()
                    && graph["edges"].is_array() && graph["module_edges"].is_array())
                    .ok_or(LoreError::InvalidFrame)?.clone();
                let bytes = serde_json::to_vec(&graph).map_err(|_| LoreError::InvalidFrame)?;
                if format!("{:x}", Sha256::digest(&bytes)) != graph_sha256 {
                    return Err(LoreError::InvalidFrame);
                }
                let referenced_sources = ReferenceFreshness::check(cwd, &graph);
                let scan_inputs = ScanInputFreshness::check(cwd, &graph);
                Ok(Self::Current(StoredCodegraph { project_key, worktree_root, query: query.into(),
                    path: path.into(), revision, source_sha256, graph_sha256, graph,
                    referenced_sources, scan_inputs }))
            }
            _ => Err(LoreError::InvalidFrame),
        }
    }

    pub fn to_value(&self) -> Value {
        match self {
            Self::Missing => json!({"status":"missing"}),
            Self::Current(row) => json!({"status":"current","schema_version":1,
                "project_key":row.project_key,"worktree_root":row.worktree_root,
                "query":row.query,"path":row.path,"revision":row.revision,
                "source_sha256":row.source_sha256,"graph_sha256":row.graph_sha256,
                "binding":"unknown","freshness":"requested_source_verified_only","graph":row.graph,
                "referenced_sources":{"status":row.referenced_sources.status,
                    "checked_files":row.referenced_sources.checked_files,
                    "issues":row.referenced_sources.issues.iter().map(|issue|
                        json!({"path":issue.path,"reason":issue.reason})).collect::<Vec<_>>()},
                "scan_inputs":{"status":row.scan_inputs.status,
                    "checked_files":row.scan_inputs.checked_files,
                    "reason":row.scan_inputs.reason}}),
        }
    }
}

impl FileMap {
    fn parse(value: Value, cwd: &str) -> Result<Self, LoreError> {
        if serde_json::to_vec(&value).map_or(true, |bytes| bytes.len() > 32 * 1024) {
            return Err(LoreError::InvalidFrame);
        }
        let object = value.as_object().filter(|object| object.len() == 3)
            .ok_or(LoreError::InvalidFrame)?;
        let key = object.get("key").and_then(Value::as_str)
            .filter(|key| *key == lore_core::config::project_slug(Path::new(cwd)))
            .ok_or(LoreError::InvalidFrame)?.to_owned();
        let cap_chars = object.get("cap_chars").and_then(Value::as_u64)
            .filter(|cap| *cap > 0 && *cap <= 1_000_000)
            .ok_or(LoreError::InvalidFrame)?;
        let rows = object.get("entries").and_then(Value::as_array)
            .filter(|rows| rows.len() <= 256)
            .ok_or(LoreError::InvalidFrame)?;
        let mut entries = Vec::with_capacity(rows.len());
        for row in rows {
            let row = row.as_object().filter(|row| row.len() == 2)
                .ok_or(LoreError::InvalidFrame)?;
            let path = row.get("path").and_then(Value::as_str)
                .filter(|path| !path.is_empty() && path.len() <= 4096
                    && !path.chars().any(char::is_control))
                .ok_or(LoreError::InvalidFrame)?.to_owned();
            let purpose = row.get("purpose").and_then(Value::as_str)
                .filter(|purpose| purpose.len() <= 4096 && !purpose.chars().any(char::is_control))
                .ok_or(LoreError::InvalidFrame)?.to_owned();
            entries.push(FileMapEntry { path, purpose });
        }
        Ok(Self { key, cap_chars, entries })
    }
}

impl PendingSessions {
    fn parse(value: Value, cwd: &str, requested: &[String]) -> Result<Self, LoreError> {
        let object = value.as_object().filter(|object| object.len() == 4)
            .ok_or(LoreError::InvalidFrame)?;
        let source_project_slug = object.get("source_project_slug").and_then(Value::as_str)
            .filter(|slug| *slug == lore_core::config::project_slug(Path::new(cwd)))
            .ok_or(LoreError::InvalidFrame)?.to_owned();
        let snapshot = object.get("snapshot").and_then(Value::as_str).filter(|digest| valid_digest(digest))
            .ok_or(LoreError::InvalidFrame)?.to_owned();
        let complete = object.get("complete").and_then(Value::as_bool).ok_or(LoreError::InvalidFrame)?;
        let rows = object.get("sessions").and_then(Value::as_array).filter(|rows| rows.len() == requested.len())
            .ok_or(LoreError::InvalidFrame)?;
        let mut sessions = Vec::with_capacity(rows.len());
        let mut seen_pids = HashSet::new();
        for (row, expected) in rows.iter().zip(requested) {
            let row = row.as_object().filter(|row| row.len() == 3)
                .ok_or(LoreError::InvalidFrame)?;
            let session_id = row.get("session_id").and_then(Value::as_str)
                .filter(|id| *id == expected).ok_or(LoreError::InvalidFrame)?.to_owned();
            let row_complete = row.get("complete").and_then(Value::as_bool).ok_or(LoreError::InvalidFrame)?;
            let pids = row.get("pending_pids").and_then(Value::as_array).filter(|pids| pids.len() <= 4096)
                .ok_or(LoreError::InvalidFrame)?;
            let mut pending_pids = Vec::with_capacity(pids.len());
            for pid in pids {
                let pid = pid.as_str().filter(|pid| !pid.is_empty() && pid.len() <= 128
                    && !pid.chars().any(char::is_control))
                    .ok_or(LoreError::InvalidFrame)?;
                if !seen_pids.insert(pid.to_owned()) { return Err(LoreError::InvalidFrame); }
                if seen_pids.len() > 4096 { return Err(LoreError::InvalidFrame); }
                pending_pids.push(pid.to_owned());
            }
            sessions.push(PendingSession { session_id, pending_pids, complete: row_complete });
        }
        if complete != sessions.iter().all(|row| row.complete) {
            return Err(LoreError::InvalidFrame);
        }
        Ok(Self { source_project_slug, snapshot, complete, sessions })
    }
}
impl PendingClusters {
    fn parse(value: Value) -> Result<Self, LoreError> {
        let groups = value["memory_clusters"].as_array().ok_or(LoreError::InvalidFrame)?;
        let other = value["other"].as_array().ok_or(LoreError::InvalidFrame)?;
        let mut count = other.len();
        let mut clusters = Vec::new();
        for group in groups {
            let rows = group.as_array().ok_or(LoreError::InvalidFrame)?;
            count = count.checked_add(rows.len()).ok_or(LoreError::InvalidFrame)?;
            if rows.is_empty() || count > 4096 { return Err(LoreError::InvalidFrame); }
            clusters.push(rows.clone());
        }
        if count > 4096 || !clusters.iter().flatten().chain(other.iter())
            .all(|row| row.is_object() && row["pid"].as_str().is_some_and(|pid| !pid.is_empty() && pid.len() <= 128)) {
            return Err(LoreError::InvalidFrame);
        }
        Ok(Self { memory_clusters: clusters, other: other.clone() })
    }
}

/// Effective DOXA store for native child carriers. This contains only a path;
/// shared settings and credentials are resolved by LORE inside the child.
pub fn carrier_root() -> Result<Option<PathBuf>, LoreError> {
    native_config::carrier_root()
}

/// Mandatory review policy uses the same effective store and persisted settings
/// as the worker. Resolution errors cannot authorize protected compaction.
pub fn review_disabled() -> Result<bool, LoreError> {
    native_config::review_disabled()
}

fn valid_digest(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn claim_bytes_valid(s: &str) -> bool { s.len() <= 16384 }

/// Exact Unicode character counts of LORE's curated project and user entries.
/// These are not model token counts or the full injected context size.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MemoryUsage {
    pub project_chars: u64,
    pub user_chars: u64,
    pub project_cap_chars: u64,
    pub user_cap_chars: u64,
}

/// A complete, immutable pending-file snapshot for a human review screen.
/// The raw JSON is the exact UTF-8 byte sequence whose digest LORE reports.
/// This value alone grants no authority to approve or mutate the proposal.
#[derive(Clone, PartialEq, Eq)]
pub struct PendingReview {
    pid: String,
    raw: String,
    sha256: String,
    inode: u64,
}

/// One exact active belief selected for review. The sidecar checks the raw
/// claim digest again under LORE's write lock before applying an action.
#[derive(Clone, PartialEq, Eq)]
pub struct BeliefReview {
    id: u64,
    uid: String,
    subject: String,
    claim: String,
    claim_sha256: String,
}

#[derive(Clone, PartialEq, Eq)]
pub struct BeliefGraph { pub id: u64, pub lines: Vec<String>, pub html: Option<String>, pub note: String }
impl std::fmt::Debug for BeliefGraph {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BeliefGraph").field("id", &self.id).field("lines", &self.lines.len())
            .field("html_bytes", &self.html.as_ref().map(String::len)).finish()
    }
}

impl std::fmt::Debug for BeliefReview {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BeliefReview")
            .field("id", &self.id)
            .field("claim_bytes", &self.claim.len())
            .finish()
    }
}

impl BeliefReview {
    pub fn id(&self) -> u64 { self.id }
    pub fn subject(&self) -> &str { &self.subject }
    pub fn claim(&self) -> &str { &self.claim }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BeliefAction { Confirmed, Contradicted, Stale, Retract }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BeliefStatus { Active, Dormant, Retracted }

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BeliefActionResult {
    pub status: BeliefStatus,
    pub retired: bool,
    pub confirmed: u64,
    pub contradicted: u64,
    pub stale: u64,
}

impl std::fmt::Debug for PendingReview {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PendingReview")
            .field("pid", &self.pid)
            .field("sha256", &self.sha256)
            .field("inode", &self.inode)
            .field("raw_bytes", &self.raw.len())
            .finish()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PendingDecision { Approve, Reject }

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PendingResolution {
    Approved,
    Rejected,
    Refused { code: String, applied: bool },
    /// The locked writer may have applied the proposal; recovery must decide.
    Indeterminate { code: String },
}

impl PendingReview {
    pub fn pid(&self) -> &str {
        &self.pid
    }
    pub fn raw(&self) -> &str {
        &self.raw
    }
    pub fn sha256(&self) -> &str {
        &self.sha256
    }
    pub fn inode(&self) -> u64 {
        self.inode
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct SyncState {
    pub last_pull_age_s: Option<f64>,
    pub unpushed: u64,
    pub conflicts: u64,
    pub unverified: u64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ConsultHit {
    pub id: u64,
    pub claim: String,
    pub claim_truncated: bool,
    pub confidence: f64,
    pub score: f64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionSearchHit {
    pub session_id: String,
    pub project: String,
    pub snippet: String,
}

#[derive(Debug)]
pub enum LoreError {
    Io(io::Error),
    Timeout,
    Closed,
    InvalidFrame,
    FrameTooLarge,
    Unavailable,
    Remote(&'static str),
}

impl std::fmt::Display for LoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(error) => write!(f, "LORE sidecar I/O: {error}"),
            Self::Timeout => write!(f, "LORE sidecar timed out"),
            Self::Closed => write!(f, "LORE sidecar closed"),
            Self::InvalidFrame => write!(f, "invalid LORE sidecar frame"),
            Self::FrameTooLarge => write!(f, "LORE sidecar frame too large"),
            Self::Unavailable => write!(f, "LORE is unavailable"),
            Self::Remote(code) => write!(f, "LORE operation failed: {code}"),
        }
    }
}

impl std::error::Error for LoreError {}

enum ReadResult {
    Line(Vec<u8>),
    Closed,
    TooLarge,
    Io,
}

enum Backend {
    Native { core: lore_core::Core, agent: bool },
    Sidecar {
        child: Child,
        stdin: ChildStdin,
        rx: Receiver<ReadResult>,
        reader: Option<JoinHandle<()>>,
    },
}

pub struct LoreClient {
    backend: Backend,
    timeout: Duration,
    next_id: u64,
    alive: bool,
    capabilities: HashSet<String>,
}

impl LoreClient {
    /// Lazy native human-review carrier. Construction does not open a store;
    /// memory-off sessions can use the pure scrub operation safely.
    pub fn open(timeout: Duration) -> Result<Self, LoreError> {
        Self::native(native_config::resolve(timeout)?, timeout, false)
    }

    /// The model carrier starts unbound. Only agent_catalog_v1 may freeze the
    /// host identity; subsequent JSON cannot choose authority or change it.
    pub fn open_agent(timeout: Duration) -> Result<Self, LoreError> {
        Self::native(native_config::resolve(timeout)?, timeout, true)
    }

    /// Explicit isolated configuration for embedders and owned fixtures.
    pub fn open_config(config: lore_core::config::Config, timeout: Duration) -> Result<Self, LoreError> {
        Self::native(config, timeout, false)
    }

    fn native(mut config: lore_core::config::Config, timeout: Duration, agent: bool) -> Result<Self, LoreError> {
        if timeout.is_zero() { return Err(LoreError::InvalidFrame); }
        config.timeout = timeout;
        let authority = if agent {
            lore_core::gate::Authority::Model { agent: "doxa".into(), engine: "unbound".into(), session_id: String::new() }
        } else {
            lore_core::gate::Authority::HumanReview { agent: "doxa-ui".into(), engine: "human".into() }
        };
        let capabilities = if agent { vec!["agent_catalog_v1", "agent_tool_v1", "agent_status_v1"] }
            else { lore_core::Core::capabilities().to_vec() };
        Ok(Self { backend: Backend::Native { core: lore_core::Core::new(config, authority), agent },
            timeout, next_id: 1, alive: true,
            capabilities: capabilities.into_iter().map(str::to_owned).collect() })
    }
    pub fn is_alive(&self) -> bool {
        self.alive
    }

    pub fn can_resolve_reviewed(&self) -> bool {
        self.capabilities.contains("resolve_reviewed_v1")
    }

    pub fn can_pending_for_sessions(&self) -> bool {
        self.capabilities.contains("pending_for_sessions_v1")
    }

    pub fn can_read_file_map(&self) -> bool { self.capabilities.contains("filemap") }

    pub fn can_read_codegraph_snapshot(&self) -> bool {
        self.capabilities.contains("codegraph_snapshot_read_v1")
    }

    pub fn can_act_on_beliefs(&self) -> bool {
        self.capabilities.contains("belief_review_v1")
            && self.capabilities.contains("belief_action_v1")
    }

    /// Launch the trusted native LORE carrier when a session requests memory.
    /// Its `-m` dispatch preserves the development compatibility protocol.
    pub fn spawn(carrier: &Path, timeout: Duration) -> Result<Self, LoreError> {
        Self::spawn_module(carrier, timeout, "doxa.lore_bridge", &["scrub", "snapshot"])
    }
    /// Separate canonical agent operator process; it binds one host identity.
    pub fn spawn_agent(carrier: &Path, timeout: Duration) -> Result<Self, LoreError> {
        Self::spawn_module(carrier, timeout, "doxa.native_agent_tools", &["agent_catalog_v1", "agent_tool_v1"])
    }
    fn spawn_module(carrier: &Path, timeout: Duration, module: &str, required: &[&str]) -> Result<Self, LoreError> {
        let mut command = Command::new(carrier);
        if let Some(root) = carrier_root()? { command.env("LORE_ROOT", root); }
        command
            .args(["-m", module])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(unix)]
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() == -1 {
                    Err(io::Error::last_os_error())
                } else {
                    Ok(())
                }
            });
        }
        // A carrier executable being replaced during an update can return
        // ETXTBSY. Retry only that transient error; other spawn failures are
        // reported immediately.
        #[cfg(unix)]
        let mut busy_retries = 0;
        let mut child = loop {
            match command.spawn() {
                Ok(child) => break child,
                Err(error) => {
                    #[cfg(unix)]
                    if error.raw_os_error() == Some(libc::ETXTBSY) && busy_retries < 10 {
                        busy_retries += 1;
                        thread::sleep(Duration::from_millis(10));
                        continue;
                    }
                    return Err(LoreError::Io(error));
                }
            }
        };
        let stdin = child.stdin.take().ok_or(LoreError::InvalidFrame)?;
        #[cfg(unix)]
        {
            let fd = stdin.as_raw_fd();
            let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
            if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0
            {
                let error = io::Error::last_os_error();
                #[cfg(unix)]
                unsafe {
                    libc::kill(-(child.id() as i32), libc::SIGKILL);
                }
                let _ = child.wait();
                return Err(LoreError::Io(error));
            }
        }
        let stdout = child.stdout.take().ok_or(LoreError::InvalidFrame)?;
        let (tx, rx) = mpsc::channel();
        let reader = thread::spawn(move || read_frames(stdout, tx));
        let mut client = Self {
            backend: Backend::Sidecar { child, stdin, rx, reader: Some(reader) },
            timeout,
            next_id: 1,
            alive: true,
            capabilities: HashSet::new(),
        };
        let hello = client.receive(timeout)?;
        if hello["type"] != "hello" || hello["proto"].as_u64() != Some(PROTOCOL_VERSION) {
            client.disable();
            return Err(LoreError::InvalidFrame);
        }
        let caps = hello["capabilities"]
            .as_array()
            .ok_or(LoreError::InvalidFrame)?;
        if !required
            .iter()
            .all(|name| caps.iter().any(|cap| cap.as_str() == Some(name)))
        {
            client.disable();
            return Err(LoreError::Unavailable);
        }
        client.capabilities = caps
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect();
        Ok(client)
    }

    pub fn agent_catalog(&mut self, identity: &Value) -> Result<Vec<Value>, LoreError> {
        let value = self.request_value("agent_catalog_v1", json!({"identity":identity}))?;
        let rows = value.as_array().filter(|rows| rows.len() <= 6).ok_or(LoreError::InvalidFrame)?;
        let mut seen = HashSet::new();
        for row in rows {
            let name = row["name"].as_str().ok_or(LoreError::InvalidFrame)?;
            if !matches!(name, "lore_belief_search" | "lore_belief_show" | "lore_belief_neighbours" |
                "lore_memory_list" | "lore_session_search" | "lore_remember") || !seen.insert(name)
                || row["description"].as_str().is_none_or(|text| text.len() > 8192 || text.chars().any(char::is_control))
                || row["inputSchema"]["type"] != "object" || !row["inputSchema"].is_object() {
                return Err(LoreError::InvalidFrame);
            }
        }
        if serde_json::to_vec(rows).map_or(true, |bytes| bytes.len() > 32 * 1024) { return Err(LoreError::InvalidFrame); }
        Ok(rows.clone())
    }
    pub fn agent_status(&mut self, identity: &Value) -> Result<Value, LoreError> {
        let value = self.request_value("agent_status_v1", json!({"identity":identity}))?;
        if !value["belief_count"].is_null() && value["belief_count"].as_u64().is_none() { return Err(LoreError::InvalidFrame); }
        let disabled=value["disabled_tools"].as_array().filter(|rows|rows.len()<=6).ok_or(LoreError::InvalidFrame)?;
        let mut seen=HashSet::new();
        for row in disabled {
            let name=row.as_str().ok_or(LoreError::InvalidFrame)?;
            if !matches!(name,"lore_belief_search"|"lore_belief_show"|"lore_belief_neighbours"|"lore_memory_list"|"lore_session_search"|"lore_remember")
                || !seen.insert(name) { return Err(LoreError::InvalidFrame); }
        }
        Ok(json!({"belief_count":value["belief_count"],"disabled_tools":disabled}))
    }
    pub fn agent_call(&mut self, identity: &Value, name: &str, arguments: &Value) -> Result<Value, LoreError> {
        if !arguments.is_object() || serde_json::to_vec(arguments).map_or(true, |bytes| bytes.len() > 32 * 1024) {
            return Err(LoreError::InvalidFrame);
        }
        self.request_value("agent_tool_v1", json!({"identity":identity,"name":name,"arguments":arguments}))
    }

    pub fn scrub(&mut self, text: &str) -> Result<String, LoreError> {
        self.request_text(json!({"op":"scrub","text":text}))
    }

    pub fn snapshot(&mut self, cwd: &str, scope: &str) -> Result<String, LoreError> {
        if cwd.is_empty()
            || cwd.len() > 4096
            || cwd.contains('\0')
            || !matches!(scope, "all" | "user" | "project")
        {
            return Err(LoreError::InvalidFrame);
        }
        self.request_text(json!({"op":"snapshot","cwd":cwd,"scope":scope}))
    }

    /// Query LORE's existing curated file map without a mutation request.
    /// The result's key must agree with LORE's project mapping for `cwd`.
    pub fn file_map(&mut self, cwd: &str) -> Result<FileMap, LoreError> {
        if !Path::new(cwd).is_absolute() || cwd.len() > 4096 || cwd.contains('\0') {
            return Err(LoreError::InvalidFrame);
        }
        FileMap::parse(self.request_value("filemap", json!({"cwd":cwd}))?, cwd)
    }

    /// The only codegraph storage operation exposed by DOXA is read-only.
    /// LORE rehashes the requested source and checks its worktree identity;
    /// this client validates the returned scope and graph digest as well.
    pub fn codegraph_snapshot(&mut self, cwd: &str, query: &str, path: &str)
        -> Result<CodegraphSnapshot, LoreError> {
        if !Path::new(cwd).is_absolute() || cwd.len() > 4096 || cwd.chars().any(char::is_control)
            || !matches!(query, "file" | "imports" | "calls" | "modules")
            || path.is_empty() || path.len() > 4096 || !path.ends_with(".rs")
            || path.chars().any(char::is_control)
            || !Path::new(path).components().all(|part|
                matches!(part, std::path::Component::Normal(_))) {
            return Err(LoreError::InvalidFrame);
        }
        CodegraphSnapshot::parse(self.request_value("codegraph_snapshot_read_v1",
            json!({"cwd":cwd,"query":query,"path":path}))?, cwd, query, path)
    }

    /// Read curated memory sizes using LORE's own project mapping and entry
    /// renderer. Older sidecars without this capability return Unavailable.
    pub fn memory_usage(&mut self, cwd: &str) -> Result<MemoryUsage, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0') {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("memory_usage_v1", json!({"cwd":cwd}))?;
        let project_chars = value["project_chars"].as_u64().ok_or(LoreError::InvalidFrame)?;
        let user_chars = value["user_chars"].as_u64().ok_or(LoreError::InvalidFrame)?;
        let project_cap_chars = value["project_cap_chars"].as_u64().ok_or(LoreError::InvalidFrame)?;
        let user_cap_chars = value["user_cap_chars"].as_u64().ok_or(LoreError::InvalidFrame)?;
        if project_chars > MAX_MEMORY_CHARS || user_chars > MAX_MEMORY_CHARS
            || project_cap_chars > MAX_MEMORY_CHARS
            || user_cap_chars > MAX_MEMORY_CHARS {
            return Err(LoreError::InvalidFrame);
        }
        Ok(MemoryUsage { project_chars, user_chars, project_cap_chars, user_cap_chars })
    }

    /// Read complete canonical facts with informational provenance. Scrubbed
    /// rows are marked and cannot substitute for an exact mutation review.
    pub fn memory_entries(&mut self, cwd: &str, scope: &str) -> Result<Vec<Value>, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0') || !matches!(scope, "user" | "project") {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("memory_entries_v1", json!({"cwd":cwd,"scope":scope}))?;
        let rows = value.as_array().filter(|rows| rows.len() <= 400).ok_or(LoreError::InvalidFrame)?;
        let mut bytes = 0usize;
        for row in rows {
            let text = row["text"].as_str().ok_or(LoreError::InvalidFrame)?;
            if text.len() > 16384 || text.chars().any(char::is_control) || !row["redacted"].is_boolean() {
                return Err(LoreError::InvalidFrame);
            }
            if !row["source"].is_null() && row["source"].as_str()
                .is_none_or(|source| source.len() > 64 || source.chars().any(char::is_control)) {
                return Err(LoreError::InvalidFrame);
            }
            bytes += text.len() + 3;
        }
        if bytes > 65536 { return Err(LoreError::InvalidFrame); }
        Ok(rows.clone())
    }

    /// Scoped, complete curated entries and optimistic review identity.
    pub fn memory_review(&mut self, cwd: &str, scope: &str) -> Result<Value, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0') || !matches!(scope, "user" | "project") {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("memory_review_v1", json!({"cwd":cwd,"scope":scope}))?;
        let entries = value["entries"].as_array().ok_or(LoreError::InvalidFrame)?;
        let mut body = String::new();
        if entries.len() > 400 || value["scope"] != scope
            || value["key"].as_str().is_none_or(|key| key.is_empty() || key.len() > 4096 || key.chars().any(char::is_control))
            || value["cap_chars"].as_u64().is_none_or(|cap| cap > MAX_MEMORY_CHARS) {
            return Err(LoreError::InvalidFrame);
        }
        for entry in entries {
            let text = entry.as_str().ok_or(LoreError::InvalidFrame)?;
            if text.len() > 16384 || text.chars().any(char::is_control) { return Err(LoreError::InvalidFrame); }
            body.push_str("- "); body.push_str(text); body.push('\n');
        }
        let digest = format!("{:x}", Sha256::digest(body.as_bytes()));
        if body.len() > 65536 || value["chars"].as_u64() != Some(body.chars().count() as u64)
            || value["sha256"].as_str() != Some(digest.as_str()) { return Err(LoreError::InvalidFrame); }
        Ok(value)
    }

    /// Apply the explicitly reviewed draft through LORE's lock and write gate.
    pub fn memory_action(&mut self, cwd: &str, request: Value) -> Result<Value, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0') || !request.is_object() {
            return Err(LoreError::InvalidFrame);
        }
        if !matches!(request["scope"].as_str(), Some("user" | "project"))
            || !matches!(request["action"].as_str(), Some("add" | "replace" | "remove"))
            || request["text"].as_str().is_none_or(|text| text.len() > 16384 || text.chars().any(char::is_control))
            || request["entry"].as_str().is_none_or(|text| text.len() > 16384 || text.chars().any(char::is_control))
            || request["expected"]["sha256"].as_str().is_none_or(|digest| !valid_digest(digest))
            || request["expected"]["key"].as_str().is_none_or(|key| key.is_empty() || key.len() > 4096) {
            return Err(LoreError::InvalidFrame);
        }
        let mut fields = request;
        fields["cwd"] = json!(cwd);
        self.request_value("memory_action_v1", fields)
    }

    /// Ask LORE for its actual Python 1.x transcript location. Reimplementing
    /// `project_slug` here would risk writing a second history for one project.
    pub fn transcript_identity(&mut self, cwd: &str) -> Result<(PathBuf, String), LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0') {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("transcript_identity", json!({"cwd": cwd}))?;
        let root = value["projects_dir"]
            .as_str()
            .ok_or(LoreError::InvalidFrame)?;
        let slug = value["slug"].as_str().ok_or(LoreError::InvalidFrame)?;
        if !Path::new(root).is_absolute() || slug.is_empty() {
            return Err(LoreError::InvalidFrame);
        }
        Ok((PathBuf::from(root), slug.to_owned()))
    }

    /// Ask external LORE to incrementally index this DOXA-owned transcript.
    /// The sidecar derives the path from LORE's project mapping; no arbitrary
    /// path or transcript contents cross this request boundary. This does not
    /// derive beliefs or approve pending proposals.
    pub fn index_transcript(&mut self, cwd: &str, session_id: &str) -> Result<u64, LoreError> {
        if cwd.is_empty()
            || cwd.len() > 4096
            || cwd.contains('\0')
            || session_id.is_empty()
            || session_id.len() > 128
            || !session_id.bytes().enumerate().all(|(i, b)| {
                b.is_ascii_alphanumeric() || (i > 0 && b == b'-')
            })
        {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value(
            "index_transcript_v1",
            json!({"cwd":cwd,"session_id":session_id}),
        )?;
        let indexed = value["indexed"].as_u64().ok_or(LoreError::InvalidFrame)?;
        let consumed = value["consumed"].as_u64().ok_or(LoreError::InvalidFrame)?;
        if indexed > consumed {
            return Err(LoreError::InvalidFrame);
        }
        Ok(indexed)
    }

    /// Query LORE's existing session FTS index. This does not grow the index
    /// or open transcript files; the caller checks any returned identity
    /// before reading its own bounded transcript view.
    pub fn session_search(&mut self, cwd: &str, query: &str) -> Result<Vec<SessionSearchHit>, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0')
            || query.trim().is_empty() || query.len() > 200
            || query.chars().any(char::is_control) {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("session_search_v1", json!({"cwd":cwd,"query":query}))?;
        let rows = value.as_array().filter(|rows| rows.len() <= 20).ok_or(LoreError::InvalidFrame)?;
        rows.iter().map(|row| {
            let id = row["session_id"].as_str().filter(|id| !id.is_empty() && id.len() <= 128
                && id.bytes().enumerate().all(|(i, b)| b.is_ascii_alphanumeric() || (i > 0 && b == b'-')))
                .ok_or(LoreError::InvalidFrame)?;
            let project = row["project"].as_str().filter(|project| !project.is_empty()
                && project.len() <= 255 && !project.contains('/') && !project.contains('\\')
                && !project.chars().any(char::is_control)).ok_or(LoreError::InvalidFrame)?;
            let snippet = row["snippet"].as_str().filter(|snippet| snippet.len() <= 1120
                && !snippet.chars().any(char::is_control)).ok_or(LoreError::InvalidFrame)?;
            Ok(SessionSearchHit { session_id: id.to_owned(), project: project.to_owned(), snippet: snippet.to_owned() })
        }).collect()
    }

    /// Canonical read-only clusters. Each proposal still needs its own exact review.
    pub fn pending_clustered(&mut self, cwd: &str) -> Result<PendingClusters, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0') { return Err(LoreError::InvalidFrame); }
        if !self.capabilities.contains("pending_cluster_v1") { return Err(LoreError::Remote("pending_cluster_unsupported")); }
        PendingClusters::parse(self.request_value("pending_cluster_v1", json!({"cwd":cwd}))?)
    }

    /// Read at most 64 source sessions in one snapshot. Unsupported older
    /// LORE versions return an error; callers must retain unknown state.
    pub fn pending_for_sessions(&mut self, cwd: &str, session_ids: &[String]) -> Result<PendingSessions, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0')
            || session_ids.is_empty() || session_ids.len() > 64 { return Err(LoreError::InvalidFrame); }
        let mut seen = HashSet::new();
        if !session_ids.iter().all(|id| !id.is_empty() && id.len() <= 128
            && !id.chars().any(char::is_control) && seen.insert(id.as_str())) {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("pending_for_sessions_v1", json!({"cwd":cwd,"session_ids":session_ids}))?;
        PendingSessions::parse(value, cwd, session_ids)
    }

    pub fn pending(&mut self, cwd: &str, offset: u16, limit: u8) -> Result<Vec<Value>, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0') || offset > 10000 || limit > 50
        {
            return Err(LoreError::InvalidFrame);
        }
        let value =
            self.request_value("pending", json!({"cwd":cwd,"offset":offset,"limit":limit}))?;
        let rows = value.as_array().ok_or(LoreError::InvalidFrame)?;
        if rows.len() > limit as usize
            || !rows
                .iter()
                .all(|row| row.is_object() && row["pid"].is_string())
        {
            return Err(LoreError::InvalidFrame);
        }
        Ok(rows.clone())
    }

    /// Fetch one complete proposal from a sidecar that explicitly supports
    /// same-descriptor pending snapshots. The legacy `pending` rows are
    /// scrubbed previews and must never be used as approval evidence.
    ///
    /// A future approval operation must independently check this digest and
    /// inode against the claimed file, after the UI has actually rendered the
    /// entire `raw` value. This client intentionally exposes no mutation.
    pub fn pending_review(&mut self, cwd: &str, pid: &str) -> Result<PendingReview, LoreError> {
        self.pending_review_request(cwd, pid, None)
    }

    /// Re-read an already displayed proposal and refuse a changed digest or
    /// inode. The sidecar compares against a fresh same-descriptor snapshot.
    /// This remains read-only and does not authorize approval.
    pub fn pending_review_if_unchanged(
        &mut self,
        cwd: &str,
        previous: &PendingReview,
    ) -> Result<PendingReview, LoreError> {
        self.pending_review_request(cwd, previous.pid(), Some(previous))
    }

    fn pending_review_request(
        &mut self,
        cwd: &str,
        pid: &str,
        previous: Option<&PendingReview>,
    ) -> Result<PendingReview, LoreError> {
        if cwd.is_empty()
            || cwd.len() > 4096
            || cwd.contains('\0')
            || pid.is_empty()
            || pid.len() > 128
            || !pid
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_'))
        {
            return Err(LoreError::InvalidFrame);
        }
        let mut frame = json!({"cwd":cwd,"pid":pid});
        if let Some(previous) = previous {
            frame["expected"] = json!({"sha256":previous.sha256(), "inode":previous.inode()});
        }
        let value = self.request_value("pending_review_v1", frame)?;
        let returned_pid = value["pid"].as_str().ok_or(LoreError::InvalidFrame)?;
        let raw = value["raw"].as_str().ok_or(LoreError::InvalidFrame)?;
        let sha256 = value["sha256"].as_str().ok_or(LoreError::InvalidFrame)?;
        let inode = value["inode"]
            .as_u64()
            .filter(|v| *v > 0)
            .ok_or(LoreError::InvalidFrame)?;
        if returned_pid != pid
            || value["complete"] != true
            || raw.is_empty()
            || raw.len() > MAX_FRAME_BYTES
            || !sha256
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
            || sha256.len() != 64
            || format!("{:x}", Sha256::digest(raw.as_bytes())) != sha256
            || !serde_json::from_str::<Value>(raw).is_ok_and(|item| item.is_object())
        {
            return Err(LoreError::InvalidFrame);
        }
        if previous.is_some_and(|old| old.sha256() != sha256 || old.inode() != inode) {
            return Err(LoreError::Remote("pending_changed"));
        }
        Ok(PendingReview {
            pid: pid.to_owned(),
            raw: raw.to_owned(),
            sha256: sha256.to_owned(),
            inode,
        })
    }

    /// Resolve one proposal the caller has fully rendered and a person has
    /// explicitly confirmed. The sidecar rechecks project scope and snapshot;
    /// LORE then claims the inode before applying or archiving it.
    pub fn resolve_reviewed(&mut self, cwd: &str, review: &PendingReview,
                            decision: PendingDecision) -> Result<PendingResolution, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0') {
            return Err(LoreError::InvalidFrame);
        }
        let decision = match decision { PendingDecision::Approve => "approve", PendingDecision::Reject => "reject" };
        let value = self.request_value("resolve_reviewed_v1", json!({
            "cwd":cwd, "pid":review.pid(), "decision":decision,
            "expected":{"sha256":review.sha256(), "inode":review.inode()}
        }))?;
        match value["status"].as_str() {
            Some("approved") if value.as_object().is_some_and(|o| o.len() == 1) => Ok(PendingResolution::Approved),
            Some("rejected") if value.as_object().is_some_and(|o| o.len() == 1) => Ok(PendingResolution::Rejected),
            Some("refused") => {
                let code = value["error"].as_str().filter(|s| !s.is_empty() && s.len() <= 64
                    && s.bytes().all(|b| b.is_ascii_lowercase() || b == b'_'))
                    .ok_or(LoreError::InvalidFrame)?;
                if value.get("applied") == Some(&Value::Null) && value["may_have_applied"] == true {
                    return Ok(PendingResolution::Indeterminate { code: code.to_owned() });
                }
                if !value["may_have_applied"].is_null() && value["may_have_applied"] != false {
                    return Err(LoreError::InvalidFrame);
                }
                let applied = value["applied"].as_bool().ok_or(LoreError::InvalidFrame)?;
                Ok(PendingResolution::Refused { code: code.to_owned(), applied })
            }
            _ => Err(LoreError::InvalidFrame),
        }
    }

    /// Fetch a complete, scrubbed belief for a human to review. The private
    /// identity fields are supplied back to LORE for its locked recheck.
    pub fn belief_graph(&mut self, cwd: &str, belief_id: u64, browser: bool) -> Result<BeliefGraph, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0') || belief_id == 0 || belief_id > i64::MAX as u64 {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("belief_graph_v1", json!({"cwd":cwd,"belief_id":belief_id,"browser":browser}))?;
        if value["id"].as_u64() != Some(belief_id) { return Err(LoreError::InvalidFrame); }
        let lines = value["lines"].as_array().filter(|lines| lines.len() <= 200).ok_or(LoreError::InvalidFrame)?
            .iter().map(|line| line.as_str().filter(|line| line.len() <= 4096).map(str::to_owned).ok_or(LoreError::InvalidFrame))
            .collect::<Result<Vec<_>, _>>()?;
        let html = if value["html"].is_null() { None } else {
            Some(value["html"].as_str().filter(|html| browser && html.len() <= MAX_FRAME_BYTES).ok_or(LoreError::InvalidFrame)?.to_owned())
        };
        let note = value["note"].as_str().filter(|note| note.len() <= 512).ok_or(LoreError::InvalidFrame)?.to_owned();
        Ok(BeliefGraph { id: belief_id, lines, html, note })
    }

    pub fn belief_review(&mut self, cwd: &str, belief_id: u64) -> Result<BeliefReview, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0')
            || belief_id == 0 || belief_id > i64::MAX as u64 {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("belief_review_v1", json!({"cwd":cwd,"belief_id":belief_id}))?;
        let obj = value.as_object().filter(|o| o.len() == 5).ok_or(LoreError::InvalidFrame)?;
        if value["id"].as_u64() != Some(belief_id) { return Err(LoreError::InvalidFrame); }
        let uid = obj["uid"].as_str().filter(|s| !s.is_empty() && s.len() <= 128)
            .ok_or(LoreError::InvalidFrame)?;
        let subject = obj["subject"].as_str().filter(|s| !s.is_empty() && s.len() <= 4096)
            .ok_or(LoreError::InvalidFrame)?;
        let claim = obj["claim"].as_str().filter(|s| claim_bytes_valid(s))
            .ok_or(LoreError::InvalidFrame)?;
        let digest = obj["claim_sha256"].as_str().filter(|s| valid_digest(s))
            .ok_or(LoreError::InvalidFrame)?;
        Ok(BeliefReview { id: belief_id, uid: uid.to_owned(), subject: subject.to_owned(),
            claim: claim.to_owned(), claim_sha256: digest.to_owned() })
    }

    /// Apply one explicitly reviewed correction through LORE's ledger. The
    /// note is required for provenance, including a retraction reason.
    pub fn belief_action(&mut self, cwd: &str, review: &BeliefReview,
                         action: BeliefAction, note: &str) -> Result<BeliefActionResult, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0')
            || note.trim().is_empty() || note.len() > 300 || note.contains('\0') {
            return Err(LoreError::InvalidFrame);
        }
        let action = match action {
            BeliefAction::Confirmed => "confirmed", BeliefAction::Contradicted => "contradicted",
            BeliefAction::Stale => "stale", BeliefAction::Retract => "retract",
        };
        let value = self.request_value("belief_action_v1", json!({
            "cwd":cwd,"belief_id":review.id,"action":action,"note":note,
            "expected":{"uid":review.uid,"subject":review.subject,
                        "claim_sha256":review.claim_sha256}
        }))?;
        let obj = value.as_object().filter(|o| o.len() == 5).ok_or(LoreError::InvalidFrame)?;
        let status = match obj["status"].as_str() {
            Some("active") => BeliefStatus::Active,
            Some("dormant") => BeliefStatus::Dormant,
            Some("retracted") => BeliefStatus::Retracted,
            _ => return Err(LoreError::InvalidFrame),
        };
        if (action == "retract") != (status == BeliefStatus::Retracted) {
            return Err(LoreError::InvalidFrame);
        }
        let retired = obj["retired"].as_bool().ok_or(LoreError::InvalidFrame)?;
        if retired != (status != BeliefStatus::Active) { return Err(LoreError::InvalidFrame); }
        Ok(BeliefActionResult {
            status,
            retired,
            confirmed: obj["confirmed"].as_u64().ok_or(LoreError::InvalidFrame)?,
            contradicted: obj["contradicted"].as_u64().ok_or(LoreError::InvalidFrame)?,
            stale: obj["stale"].as_u64().ok_or(LoreError::InvalidFrame)?,
        })
    }

    /// One active FTS belief. It is derived data for citation, never an instruction.
    pub fn consult(&mut self, prompt: &str) -> Result<Option<ConsultHit>, LoreError> {
        if prompt.is_empty() || prompt.len() > 8192 || prompt.contains('\0') {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("consult", json!({"prompt":prompt}))?;
        if value.is_null() {
            return Ok(None);
        }
        if value["citation_status"] != "cite_only" {
            return Err(LoreError::InvalidFrame);
        }
        let id = value["id"]
            .as_u64()
            .filter(|id| *id > 0)
            .ok_or(LoreError::InvalidFrame)?;
        let claim = value["claim"]
            .as_str()
            .filter(|s| s.len() <= 960)
            .ok_or(LoreError::InvalidFrame)?;
        let claim_truncated = value["claim_truncated"]
            .as_bool()
            .ok_or(LoreError::InvalidFrame)?;
        let confidence = value["confidence"]
            .as_f64()
            .filter(|n| n.is_finite() && (0.0..=1.0).contains(n))
            .ok_or(LoreError::InvalidFrame)?;
        let score = value["score"]
            .as_f64()
            .filter(|n| n.is_finite())
            .ok_or(LoreError::InvalidFrame)?;
        Ok(Some(ConsultHit {
            id,
            claim: claim.to_owned(),
            claim_truncated,
            confidence,
            score,
        }))
    }

    pub fn beliefs(&mut self, offset: u16, limit: u8) -> Result<Vec<Value>, LoreError> {
        self.beliefs_request("beliefs", offset, limit, "")
    }

    /// Literal Unicode casefold filter over visible subject/claim text, before
    /// canonical recency ordering and pagination. No semantic consult fallback.
    pub fn beliefs_filtered(&mut self, offset: u16, limit: u8, query: &str) -> Result<Vec<Value>, LoreError> {
        self.beliefs_request(if query.is_empty() { "beliefs" } else { "beliefs_filtered_v1" }, offset, limit, query)
    }

    /// Bounded full display of an active list row. This grants no mutation
    /// review identity; redacted/omitted source text is explicitly incomplete.
    pub fn belief_display(&mut self, cwd: &str, belief_id: u64) -> Result<Value, LoreError> {
        if cwd.is_empty() || cwd.len() > 4096 || cwd.contains('\0')
            || belief_id == 0 || belief_id > i64::MAX as u64 {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("belief_display_v1", json!({"cwd":cwd,"belief_id":belief_id}))?;
        let subject = value["subject"].as_str().ok_or(LoreError::InvalidFrame)?;
        let claim = value["claim"].as_str().ok_or(LoreError::InvalidFrame)?;
        let complete = value["complete"].as_bool().ok_or(LoreError::InvalidFrame)?;
        let redacted = value["redacted"].as_bool().ok_or(LoreError::InvalidFrame)?;
        if value["id"].as_u64() != Some(belief_id) || subject.len() > 4096
            || subject.chars().any(char::is_control) || subject.len() + claim.len() > 65536
            || claim.chars().any(|c| c.is_control() && c != '\n') || (complete && redacted)
            || value.get("claim_sha256").is_some() || value.get("uid").is_some() {
            return Err(LoreError::InvalidFrame);
        }
        Ok(value)
    }

    fn beliefs_request(&mut self, op: &str, offset: u16, limit: u8, query: &str) -> Result<Vec<Value>, LoreError> {
        if offset > 10000 || limit > 50 || query.chars().count() > 200 || query.len() > 1024
            || query.chars().any(char::is_control) {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value(op, json!({"offset":offset,"limit":limit,"query":query}))?;
        let rows = value
            .as_array()
            .filter(|rows| rows.len() <= limit as usize)
            .ok_or(LoreError::InvalidFrame)?;
        if !rows.iter().all(|row| {
            row["id"].as_u64().is_some_and(|id| id > 0)
                && row["subject"].is_string()
                && row["claim"].is_string()
                && row["claim_truncated"].is_boolean()
                && row["confidence"]
                    .as_f64()
                    .is_some_and(|n| n.is_finite() && (0.0..=1.0).contains(&n))
                && row["evidence_count"].as_u64().is_some()
                && ["updated", "created", "recency"].iter().all(|field| {
                    row[*field].is_null() || row[*field].as_str()
                        .is_some_and(|text| text.len() <= 64 && !text.chars().any(char::is_control))
                })
        }) {
            return Err(LoreError::InvalidFrame);
        }
        Ok(rows.clone())
    }

    pub fn evidence(&mut self, belief_id: u64, limit: u8) -> Result<Vec<Value>, LoreError> {
        if belief_id == 0 || belief_id > i64::MAX as u64 || limit > 50 {
            return Err(LoreError::InvalidFrame);
        }
        let value = self.request_value("evidence", json!({"belief_id":belief_id,"limit":limit}))?;
        let rows = value
            .as_array()
            .filter(|rows| rows.len() <= limit as usize)
            .ok_or(LoreError::InvalidFrame)?;
        if !rows.iter().all(|row| {
            row["session_id"].is_string()
                && row["project"].is_string()
                && row["note"].is_string()
                && row["note_truncated"].is_boolean()
                && row["created"].is_string()
                && (row.get("source_engine").is_none() || row["source_engine"].is_string())
                && (row.get("trail_truncated").is_none() || row["trail_truncated"].is_boolean())
        }) {
            return Err(LoreError::InvalidFrame);
        }
        Ok(rows.clone())
    }

    pub fn sync_state(&mut self) -> Result<Option<SyncState>, LoreError> {
        let value = self.request_value("sync_state", json!({}))?;
        if value.is_null() {
            return Ok(None);
        }
        let age = match &value["last_pull_age_s"] {
            Value::Null => None,
            v => Some(
                v.as_f64()
                    .filter(|n| n.is_finite() && *n >= 0.0)
                    .ok_or(LoreError::InvalidFrame)?,
            ),
        };
        Ok(Some(SyncState {
            last_pull_age_s: age,
            unpushed: value["unpushed"].as_u64().ok_or(LoreError::InvalidFrame)?,
            conflicts: value["conflicts"].as_u64().ok_or(LoreError::InvalidFrame)?,
            unverified: value["unverified"]
                .as_u64()
                .ok_or(LoreError::InvalidFrame)?,
        }))
    }

    pub fn refresh_interval(&mut self) -> Result<Option<u64>, LoreError> {
        let value = self.request_value("refresh_interval", json!({}))?;
        if value.is_null() {
            Ok(None)
        } else {
            value.as_u64().map(Some).ok_or(LoreError::InvalidFrame)
        }
    }

    fn request_text(&mut self, frame: Value) -> Result<String, LoreError> {
        let value = self.request(frame)?;
        value["text"].as_str().map(str::to_owned).ok_or_else(|| {
            self.disable();
            LoreError::InvalidFrame
        })
    }

    fn request_value(&mut self, op: &str, mut frame: Value) -> Result<Value, LoreError> {
        if !self.capabilities.contains(op) {
            return Err(LoreError::Unavailable);
        }
        frame["op"] = json!(op);
        let reply = self.request(frame)?;
        reply.get("value").cloned().ok_or_else(|| {
            self.disable();
            LoreError::InvalidFrame
        })
    }

    fn request(&mut self, mut frame: Value) -> Result<Value, LoreError> {
        if !self.alive {
            return Err(LoreError::Closed);
        }
        let id = self.next_id;
        self.next_id = id.checked_add(1).ok_or(LoreError::InvalidFrame)?;
        frame["id"] = json!(id);
        let bytes = encode(&frame)?;
        if let Backend::Native { core, agent } = &mut self.backend {
            if *agent && bytes.len() > 64 * 1024 { return Err(LoreError::FrameTooLarge); }
            let result = if *agent { core.agent_execute(&frame) } else { core.execute(&frame) }
                .map_err(|error| native_error(frame["op"].as_str().unwrap_or(""), error))?;
            let reply = if matches!(frame["op"].as_str(), Some("scrub" | "snapshot")) {
                json!({"type":"reply", "id":id, "ok":true, "text":result})
            } else { json!({"type":"reply", "id":id, "ok":true, "value":result}) };
            // The native path crosses the same finite frame boundary before
            // typed result validators consume it, with no trusted fast path.
            let response = encode(&reply)?;
            if *agent && response.len() > 64 * 1024 { return Err(LoreError::FrameTooLarge); }
            return Ok(reply);
        }
        let started = Instant::now();
        let mut offset = 0;
        while offset < bytes.len() {
            if started.elapsed() >= self.timeout {
                self.disable();
                return Err(LoreError::Timeout);
            }
            let write = match &mut self.backend {
                Backend::Sidecar { stdin, .. } => stdin.write(&bytes[offset..]),
                Backend::Native { .. } => unreachable!(),
            };
            match write {
                Ok(0) => {
                    self.disable();
                    return Err(LoreError::Closed);
                }
                Ok(n) => offset += n,
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(2))
                }
                Err(error) => {
                    self.disable();
                    return Err(LoreError::Io(error));
                }
            }
        }
        let remaining = self.timeout.saturating_sub(started.elapsed());
        if remaining.is_zero() {
            self.disable();
            return Err(LoreError::Timeout);
        }
        let reply = self.receive(remaining)?;
        if reply["type"] != "reply" || reply["id"].as_u64() != Some(id) || !reply["ok"].is_boolean()
        {
            self.disable();
            return Err(LoreError::InvalidFrame);
        }
        if reply["ok"] == true {
            Ok(reply)
        } else {
            let code = match reply["error"].as_str() {
                Some("lore_unavailable") => return Err(LoreError::Unavailable),
                Some("invalid_request") => "invalid_request",
                Some("operation_failed") => "operation_failed",
                Some("output_too_large") => "output_too_large",
                Some("pending_changed") => "pending_changed",
                Some("pending_incomplete") => "pending_incomplete",
                Some("pending_unavailable") => "pending_unavailable",
                Some("belief_changed") => "belief_changed",
                Some("belief_unavailable") => "belief_unavailable",
                Some("belief_incomplete") => "belief_incomplete",
                Some("memory_incomplete") => "memory_incomplete",
                Some("memory_changed") => "memory_changed",
                Some("memory_ambiguous") => "memory_ambiguous",
                Some("memory_over_cap") => "memory_over_cap",
                Some("memory_refused") => "memory_refused",

                _ => "remote_error",
            };
            Err(LoreError::Remote(code))
        }
    }

    fn receive(&mut self, timeout: Duration) -> Result<Value, LoreError> {
        let received = match &self.backend {
            Backend::Sidecar { rx, .. } => rx.recv_timeout(timeout),
            Backend::Native { .. } => return Err(LoreError::InvalidFrame),
        };
        match received {
            Ok(ReadResult::Line(bytes)) => serde_json::from_slice::<Value>(&bytes)
                .ok()
                .filter(Value::is_object)
                .ok_or_else(|| {
                    self.disable();
                    LoreError::InvalidFrame
                }),
            Ok(ReadResult::Closed) => {
                self.disable();
                Err(LoreError::Closed)
            }
            Ok(ReadResult::TooLarge) => {
                self.disable();
                Err(LoreError::FrameTooLarge)
            }
            Ok(ReadResult::Io) => {
                self.disable();
                Err(LoreError::Closed)
            }
            Err(RecvTimeoutError::Timeout) => {
                self.disable();
                Err(LoreError::Timeout)
            }
            Err(RecvTimeoutError::Disconnected) => {
                self.disable();
                Err(LoreError::Closed)
            }
        }
    }

    fn disable(&mut self) {
        if self.alive {
            self.alive = false;
            if let Backend::Sidecar { child, .. } = &mut self.backend {
                #[cfg(unix)]
                unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL); }
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

impl Drop for LoreClient {
    fn drop(&mut self) {
        self.disable();
        // A malicious descendant could keep stdout open after its parent is
        // killed; never make drop wait indefinitely for that pipe.
        let reader = match &mut self.backend {
            Backend::Sidecar { reader, .. } => reader.take(),
            Backend::Native { .. } => None,
        };
        if let Some(reader) = reader {
            for _ in 0..20 {
                if reader.is_finished() {
                    let _ = reader.join();
                    return;
                }
                thread::sleep(Duration::from_millis(5));
            }
        }
    }
}

fn native_error(op: &str, error: lore_core::Error) -> LoreError {
    let code = match (op, error) {
        ("pending_review_v1" | "resolve_reviewed_v1", lore_core::Error::Changed) => "pending_changed",
        ("belief_review_v1" | "belief_action_v1", lore_core::Error::Changed) => "belief_changed",
        ("memory_review_v1" | "memory_action_v1", lore_core::Error::Changed) => "memory_changed",
        ("pending_review_v1", lore_core::Error::TooLarge) => "pending_incomplete",
        ("belief_review_v1", lore_core::Error::TooLarge) => "belief_incomplete",
        ("memory_review_v1", lore_core::Error::TooLarge) => "memory_incomplete",
        ("memory_action_v1", lore_core::Error::OverCap) => "memory_over_cap",
        ("memory_action_v1", lore_core::Error::Untrusted) => "memory_refused",
        (_, error) => error.code(),
    };
    LoreError::Remote(code)
}

fn encode(value: &Value) -> Result<Vec<u8>, LoreError> {
    let mut bytes = serde_json::to_vec(value).map_err(|_| LoreError::InvalidFrame)?;
    bytes.push(b'\n');
    if bytes.len() > MAX_FRAME_BYTES {
        Err(LoreError::FrameTooLarge)
    } else {
        Ok(bytes)
    }
}

fn read_frames(mut reader: impl Read, tx: mpsc::Sender<ReadResult>) {
    let mut pending = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        match reader.read(&mut chunk) {
            Ok(0) => {
                let _ = tx.send(ReadResult::Closed);
                return;
            }
            Ok(n) => {
                for byte in &chunk[..n] {
                    if pending.len() == MAX_FRAME_BYTES {
                        let _ = tx.send(ReadResult::TooLarge);
                        return;
                    }
                    pending.push(*byte);
                    if *byte == b'\n'
                        && tx
                            .send(ReadResult::Line(std::mem::take(&mut pending)))
                            .is_err()
                    {
                        return;
                    }
                }
            }
            Err(_) => {
                let _ = tx.send(ReadResult::Io);
                return;
            }
        }
    }
}

#[cfg(test)]
mod pending_cluster_tests {
    use super::*;
    #[test]
    fn canonical_cluster_shape_and_total_bound() {
        let row = json!({"pid":"proposal-1","kind":"memory"});
        let parsed = PendingClusters::parse(json!({"memory_clusters":[[row.clone()]],"other":[row.clone()]})).unwrap();
        assert_eq!(parsed.memory_clusters.len(),1); assert_eq!(parsed.other.len(),1);
        assert!(PendingClusters::parse(json!({"memory_clusters":[[]],"other":[]})).is_err());
        assert!(PendingClusters::parse(json!({"memory_clusters":[[{"pid":4}]],"other":[]})).is_err());
        assert!(PendingClusters::parse(json!({"memory_clusters":[],"other":vec![row;4097]})).is_err());
    }
}

#[cfg(test)]
mod pending_sessions_tests {
    use super::*;

    #[test]
    fn source_session_summary_requires_exact_scoped_complete_rows() {
        let owned = tempfile::tempdir().unwrap();
        let cwd = owned.path().to_str().unwrap();
        let slug = lore_core::config::project_slug(owned.path());
        let ids = vec!["one".to_owned(), "two".to_owned()];
        let digest = "a".repeat(64);
        let valid = json!({"source_project_slug":slug,"snapshot":digest,"complete":false,
            "sessions":[
                {"session_id":"one","pending_pids":["proposal-1"],"complete":true},
                {"session_id":"two","pending_pids":[],"complete":false}
            ]});
        let parsed = PendingSessions::parse(valid.clone(), cwd, &ids).unwrap();
        assert_eq!(parsed.sessions[0].pending_pids, ["proposal-1"]);
        assert!(!parsed.sessions[1].complete);
        let mut wrong = valid.clone();
        wrong["source_project_slug"] = json!("another-project");
        assert!(PendingSessions::parse(wrong, cwd, &ids).is_err());
        let mut wrong = valid.clone();
        wrong["sessions"][1]["session_id"] = json!("one");
        assert!(PendingSessions::parse(wrong, cwd, &ids).is_err());
        let mut wrong = valid.clone();
        wrong["complete"] = json!(true);
        assert!(PendingSessions::parse(wrong, cwd, &ids).is_err());
        let mut wrong = valid.clone();
        wrong.as_object_mut().unwrap().remove("snapshot");
        wrong["unrelated"] = json!("a".repeat(64));
        assert!(PendingSessions::parse(wrong, cwd, &ids).is_err());
        let mut wrong = valid.clone();
        wrong["sessions"][0].as_object_mut().unwrap().remove("complete");
        wrong["sessions"][0]["unrelated"] = json!(true);
        assert!(PendingSessions::parse(wrong, cwd, &ids).is_err());
        let mut wrong = valid;
        wrong["sessions"][1]["pending_pids"] = json!(["proposal-1"]);
        assert!(PendingSessions::parse(wrong, cwd, &ids).is_err());
    }
}

#[cfg(test)]
mod file_map_tests {
    use super::*;

    #[test]
    fn matching_project_and_same_path_alternatives_are_preserved() {
        let owned = tempfile::tempdir().unwrap();
        let cwd = owned.path().to_str().unwrap();
        let key = lore_core::config::project_slug(owned.path());
        let valid = json!({"key":key,"cap_chars":4400,"entries":[
            {"path":"src/lib.rs","purpose":"public entry"},
            {"path":"src/lib.rs","purpose":"reviewed alternate"}
        ]});
        let parsed = FileMap::parse(valid.clone(), cwd).unwrap();
        assert_eq!(parsed.entries.len(), 2);
        assert_eq!(parsed.entries[0].path, parsed.entries[1].path);
        assert_ne!(parsed.entries[0].purpose, parsed.entries[1].purpose);
        let mut wrong = valid.clone();
        wrong["key"] = json!("other-project");
        assert!(FileMap::parse(wrong, cwd).is_err());
        let mut wrong = valid.clone();
        wrong["entries"][0]["purpose"] = json!("unsafe\nline");
        assert!(FileMap::parse(wrong, cwd).is_err());
        let mut wrong = valid;
        wrong["entries"][1]["sha256"] = json!("invented");
        assert!(FileMap::parse(wrong, cwd).is_err());
    }
}

#[cfg(test)]
mod codegraph_snapshot_tests {
    use super::*;

    fn response(cwd: &str) -> Value {
        let graph = json!({"scope":cwd,"query":"modules","value":"lib.rs","status":"ok",
            "requested_source_sha256":"a".repeat(64),"requested_source_read_unix_ms":1,
            "coverage":{},"rows":[],"edges":[],"module_edges":[
                {"source":"lib.rs","module":"child","resolution":"unknown",
                 "reason":"conditional","target":null,"conditional_candidate":"child.rs"}]});
        let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&graph).unwrap()));
        json!({"status":"current","schema_version":1,
            "project_key":lore_core::config::project_slug(Path::new(cwd)),
            "worktree_root":cwd,"query":"modules","path":"lib.rs","revision":2,
            "source_sha256":"a".repeat(64),"graph_sha256":digest,
            "freshness":"requested_source_verified_only","binding":"unknown","graph":graph})
    }

    #[test]
    fn stored_snapshot_requires_exact_scope_source_digest_and_unknown_binding() {
        let cwd = "/owned/codegraph-fixture";
        let valid = response(cwd);
        let parsed = CodegraphSnapshot::parse(valid.clone(), cwd, "modules", "lib.rs").unwrap();
        assert!(matches!(parsed, CodegraphSnapshot::Current(_)));
        let rendered = parsed.to_value();
        assert_eq!(rendered["referenced_sources"]["status"], "unknown");
        assert_eq!(rendered["scan_inputs"]["status"], "unknown");
        assert_eq!(rendered["scan_inputs"]["reason"], "scan_input_digest_absent");
        assert_eq!(rendered["referenced_sources"]["issues"][0]["reason"],
            "missing_reference_hash");
        let mut original = rendered.clone();
        original.as_object_mut().unwrap().remove("referenced_sources");
        original.as_object_mut().unwrap().remove("scan_inputs");
        assert_eq!(original, valid);
        assert_eq!(CodegraphSnapshot::parse(json!({"status":"missing"}), cwd,
            "modules", "lib.rs").unwrap(), CodegraphSnapshot::Missing);
        for pointer in ["/project_key", "/worktree_root", "/query", "/path",
            "/source_sha256", "/graph_sha256", "/freshness", "/binding",
            "/graph/requested_source_sha256", "/graph/module_edges/0/reason"] {
            let mut wrong = valid.clone();
            *wrong.pointer_mut(pointer).unwrap() = json!("wrong");
            assert!(CodegraphSnapshot::parse(wrong, cwd, "modules", "lib.rs").is_err(),
                "accepted changed {pointer}");
        }
        assert!(CodegraphSnapshot::parse(json!({"status":"missing","graph":{}}), cwd,
            "modules", "lib.rs").is_err());
    }

    #[test]
    fn reviewed_scan_inputs_are_verified_then_stale_or_unknown() {
        let owned = tempfile::tempdir().unwrap();
        let cwd = owned.path().to_str().unwrap();
        assert!(std::process::Command::new("git").args(["init", "-q"])
            .arg(owned.path()).status().unwrap().success());
        std::fs::write(owned.path().join("lib.rs"), "fn first() {}\n").unwrap();
        let answer = doxa_codegraph::query(owned.path(),
            doxa_codegraph::Query::File("lib.rs".into())).unwrap();
        let mut response = response(cwd);
        response["source_sha256"] = json!(answer.requested_source_sha256);
        response["graph"] = serde_json::to_value(&answer).unwrap();
        response["graph_sha256"] = json!(format!("{:x}", Sha256::digest(
            serde_json::to_vec(&response["graph"]).unwrap())));
        // The stored graph must be scoped to the original query.
        response["query"] = json!("file");
        let read = |value: Value| CodegraphSnapshot::parse(value, cwd, "file", "lib.rs").unwrap();
        let CodegraphSnapshot::Current(current) = read(response.clone()) else { panic!("missing") };
        assert_eq!(current.scan_inputs.status, "verified");
        assert_eq!(current.scan_inputs.checked_files, 1);
        let mut incomplete = response["graph"].clone();
        incomplete["coverage"]["skipped"]["count"] = json!(1);
        assert_eq!(ScanInputFreshness::check(cwd, &incomplete).status, "unknown");
        incomplete["coverage"]["skipped"]["count"] = json!(0);
        incomplete["coverage"]["parsed_rust_files"] = json!(2);
        assert_eq!(ScanInputFreshness::check(cwd, &incomplete).status, "stale");
        std::fs::write(owned.path().join("extra.rs"), "fn extra() {}\n").unwrap();
        let CodegraphSnapshot::Current(added) = read(response.clone()) else { panic!("missing") };
        assert_eq!(added.scan_inputs.status, "stale");
        std::fs::remove_file(owned.path().join("extra.rs")).unwrap();
        std::fs::write(owned.path().join("lib.rs"), "fn changed() {}\n").unwrap();
        let CodegraphSnapshot::Current(edited) = read(response.clone()) else { panic!("missing") };
        assert_eq!(edited.scan_inputs.status, "stale");
        std::fs::remove_file(owned.path().join("lib.rs")).unwrap();
        let CodegraphSnapshot::Current(deleted) = read(response.clone()) else { panic!("missing") };
        assert_eq!(deleted.scan_inputs.status, "stale");
        std::os::unix::fs::symlink(owned.path().join("outside.rs"),
            owned.path().join("lib.rs")).unwrap();
        let CodegraphSnapshot::Current(uncheckable) = read(response) else { panic!("missing") };
        assert_eq!(uncheckable.scan_inputs.status, "unknown");
        assert_eq!(uncheckable.scan_inputs.reason, "scan_inputs_uncheckable");
    }


    #[test]
    fn referenced_call_candidates_require_safe_paths_and_matching_hashes() {
        let owned = tempfile::tempdir().unwrap();
        let cwd = owned.path().to_str().unwrap();
        let candidate = owned.path().join("candidate.rs");
        std::fs::write(&candidate, "pub fn f() {}\n").unwrap();
        let digest = format!("{:x}", Sha256::digest(std::fs::read(&candidate).unwrap()));
        let graph = json!({"rows":[],"module_edges":[],"edges":[{
            "file":"candidate.rs","sha256":digest,"candidates":[
                {"file":"candidate.rs","sha256":digest}]}]});
        let fresh = ReferenceFreshness::check(cwd, &graph);
        assert_eq!((fresh.status, fresh.checked_files), ("verified", 1));
        std::fs::write(&candidate, "pub fn changed() {}\n").unwrap();
        let stale = ReferenceFreshness::check(cwd, &graph);
        assert_eq!(stale.status, "stale");
        std::fs::write(&candidate, "pub fn f() {}\n").unwrap();
        let mut unsafe_graph = graph.clone();
        unsafe_graph["edges"][0]["candidates"][0]["file"] = json!("../escape.rs");
        let unknown = ReferenceFreshness::check(cwd, &unsafe_graph);
        assert_eq!(unknown.status, "unknown");
        assert!(unknown.issues.iter().any(|issue| issue.reason == "unsafe_reference_path"));
        let mut missing_hash = graph;
        missing_hash["edges"][0]["candidates"][0].as_object_mut().unwrap().remove("sha256");
        assert_eq!(ReferenceFreshness::check(cwd, &missing_hash).status, "unknown");
        for missing in ["rows", "edges", "module_edges"] {
            let mut malformed = json!({"rows":[],"edges":[],"module_edges":[]});
            malformed.as_object_mut().unwrap().remove(missing);
            let checked = ReferenceFreshness::check(cwd, &malformed);
            assert_eq!(checked.status, "unknown", "accepted missing {missing}");
            assert!(checked.issues.iter().any(|issue|
                issue.reason == "malformed_reference_section"));
        }
    }
}
