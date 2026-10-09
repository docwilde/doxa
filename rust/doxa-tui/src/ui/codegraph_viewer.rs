//! Read-only presentation of the bounded, fresh codegraph syntax queries.
use super::{App, ChipInfo};
use doxa_codegraph::{Answer, Query};
use doxa_lore::CodegraphSnapshot;
use std::sync::mpsc::{self, TryRecvError};
use std::time::Duration;

fn request(args: &str) -> Result<Query, &'static str> {
    let (kind, value) = args.trim().split_once(char::is_whitespace)
        .ok_or("Usage: /codegraph file|symbol|imports|calls|modules VALUE")?;
    let value = value.trim();
    if value.is_empty() || value.len() > 4096 || value.chars().any(char::is_control) {
        return Err("Code graph value must be 1–4096 bytes without control characters");
    }
    Ok(match kind {
        "file" => Query::File(value.into()),
        "symbol" => Query::Symbol(value.into()),
        "imports" => Query::Imports(value.into()),
        "calls" => Query::Calls(value.into()),
        "modules" => Query::Modules(value.into()),
        _ => return Err("Usage: /codegraph file|symbol|imports|calls|modules VALUE"),
    })
}

fn display(value: &str) -> String {
    crate::markdown::sanitize(value).replace('\n', "\\n").replace('\t', "\\t")
}

fn hash(lines: &mut Vec<String>, label: &str, value: &str) {
    lines.push(format!("  {label} SHA-256: {value}"));
}

fn answer_lines(answer: &Answer) -> Vec<String> {
    let mut lines = vec![
        format!("{} {} · {}", answer.query, display(&answer.value), display(&answer.status)),
        format!("Worktree: {}", display(&answer.scope)),
        format!("Observed: {} Unix ms · live source bytes at query time", answer.observed_unix_ms),
        format!("Coverage: {} listed · {} parsed Rust · {} parsed Python · {} skipped · {} unparseable",
            answer.coverage.enumerated_files, answer.coverage.parsed_rust_files,
            answer.coverage.parsed_python_files,
            answer.coverage.skipped.count, answer.coverage.unparseable.count),
        format!("Rust scan inputs: {}", answer.scan_input_sha256.as_deref()
            .unwrap_or("unknown (skipped or unparseable input)")),
        "Syntax only · semantic bindings are unknown; Python module targets are unresolved".into(),
    ];
    for (language, count) in &answer.coverage.unsupported_languages {
        lines.push(format!("Unsupported {language}: {count} file(s)"));
    }
    for issue in answer.coverage.skipped.examples.iter().chain(&answer.coverage.unparseable.examples) {
        lines.push(format!("Issue {}: {}", display(&issue.file), display(&issue.reason)));
    }
    for row in &answer.rows {
        lines.push(String::new());
        lines.push(format!("{} · {}:{} · {}", row.kind, display(&row.file), row.line, display(&row.qualified)));
        if let Some(alias) = &row.alias { lines.push(format!("  alias: {}", display(alias))); }
        if row.glob { lines.push("  glob import declaration".into()); }
        hash(&mut lines, "source", &row.sha256);
        lines.push(format!("  read: {} Unix ms", row.read_unix_ms));
    }
    for edge in &answer.edges {
        lines.push(String::new());
        lines.push(format!("call · {}:{}:{} · {} → {}", display(&edge.file), edge.line, edge.column,
            display(&edge.caller), display(&edge.target)));
        lines.push(format!("  {} · {} · {}", edge.form, edge.binding, edge.reason));
        hash(&mut lines, "source", &edge.sha256);
        lines.push(format!("  read: {} Unix ms", edge.read_unix_ms));
        for candidate in &edge.candidates {
            lines.push(format!("  candidate only: {}:{} · {}", display(&candidate.file), candidate.line,
                display(&candidate.qualified)));
            hash(&mut lines, "candidate", &candidate.sha256);
            lines.push(format!("  candidate read: {} Unix ms", candidate.read_unix_ms));
        }
        if edge.omitted_candidates > 0 { lines.push(format!("  +{} omitted candidates", edge.omitted_candidates)); }
    }
    for edge in &answer.module_edges {
        lines.push(String::new());
        lines.push(format!("module · {}:{}:{} · {}", display(&edge.source), edge.line, edge.column,
            display(&edge.module)));
        lines.push(format!("  {} · {}", edge.resolution, edge.reason));
        lines.push(format!("  target: {}", edge.target.as_deref().map(display).unwrap_or_else(|| "unknown".into())));
        if let Some(candidate) = &edge.conditional_candidate {
            lines.push(format!("  conditional candidate only: {}", display(candidate)));
        }
        hash(&mut lines, "source", &edge.source_sha256);
        lines.push(format!("  source read: {} Unix ms", edge.source_read_unix_ms));
        if let Some(value) = &edge.target_sha256 { hash(&mut lines, "target", value); }
        if let Some(value) = edge.target_read_unix_ms { lines.push(format!("  target read: {value} Unix ms")); }
        if let Some(value) = &edge.conditional_candidate_sha256 { hash(&mut lines, "conditional candidate", value); }
        if let Some(value) = edge.conditional_candidate_read_unix_ms {
            lines.push(format!("  conditional candidate read: {value} Unix ms"));
        }
    }
    for (label, count) in [("rows", answer.omitted_rows), ("calls", answer.omitted_edges),
        ("modules", answer.omitted_module_edges), ("nested modules skipped", answer.skipped_nested_modules)] {
        if count > 0 { lines.push(format!("+{count} {label}")); }
    }
    if let Some(fallback) = answer.fallback { lines.push(format!("Fallback: {fallback}")); }
    lines.push(String::new());
    lines.push(display(answer.note));
    lines
}

fn stored_lines(snapshot: &CodegraphSnapshot) -> Vec<String> {
    match snapshot {
        CodegraphSnapshot::Missing => vec![
            "No reviewed LORE snapshot for this file and query".into(),
            "Run a fresh /codegraph query; storage requires an explicit LORE review".into(),
        ],
        CodegraphSnapshot::Current(row) => {
            let mut lines = vec![
                format!("Reviewed LORE code graph · revision {}", row.revision),
                format!("{} {} · {}", row.query, display(&row.path), display(&row.project_key)),
                format!("Worktree: {}", display(&row.worktree_root)),
                format!("Requested source SHA-256: {}", row.source_sha256),
                format!("Graph SHA-256: {}", row.graph_sha256),
                "Freshness: requested source verified at read time".into(),
                format!("Included reference files: {} · {} checked at read time",
                    row.referenced_sources.status, row.referenced_sources.checked_files),
                format!("Rust scan inputs: {} · {} checked · {}",
                    row.scan_inputs.status, row.scan_inputs.checked_files, row.scan_inputs.reason),
                "Binding: unknown · syntax data only".into(),
                String::new(),
            ];
            for issue in &row.referenced_sources.issues {
                lines.push(format!("Reference {}: {}", display(&issue.path), issue.reason));
            }
            match serde_json::to_string_pretty(&row.graph) {
                Ok(graph) => lines.extend(graph.lines().map(display)),
                Err(_) => lines.push("Stored graph could not be displayed".into()),
            }
            lines
        }
    }
}

pub(super) fn wrapped_lines(lines: &[String], width: usize) -> Vec<String> {
    lines.iter().flat_map(|line| crate::memory_menu::wrap_review(line, width.max(1))).collect()
}

impl App {
    /// Render a deterministic syntax-query example through the production modal.
    /// The gallery caller supplies inert text; no worktree scan is started.
    #[doc(hidden)]
    pub fn show_codegraph_fixture(&mut self, lines: &[&str]) {
        self.chip_info = Some(ChipInfo { kind: "codegraph", label: String::new(),
            lines: lines.iter().map(|line| (*line).to_owned()).collect(),
            scroll: 0, owner: None });
    }

    pub(super) fn open_codegraph(&mut self, args: &str) {
        let (stored, args) = args.trim().strip_prefix("stored ")
            .map_or((false, args), |rest| (true, rest));
        let request = match request(args) {
            Ok(value) => value,
            Err(error) => { self.notice = error.into(); return; }
        };
        if stored && request.file_scope().is_none() {
            self.notice = "Stored code graph requires file|imports|calls|modules PATH".into();
            return;
        }
        if self.codegraph_pending.is_some() {
            self.notice = "Wait for the current code graph query to finish".into();
            return;
        }
        let Some(id) = self.groups[self.active_group].active_id().map(str::to_owned) else {
            self.notice = "Select a local session to query its worktree".into();
            return;
        };
        let Some(cwd) = self.session_cwds.get(&id).cloned() else {
            self.notice = "Active session directory unavailable".into();
            return;
        };
        let owner = (id.clone(), cwd.to_string_lossy().into_owned());
        self.chip_info = Some(ChipInfo { kind: "codegraph", label: String::new(),
            lines: vec![if stored { "Reading reviewed LORE snapshot…".into() }
                else { "Scanning current Git worktree…".into() }], scroll: 0, owner: Some(owner) });
        if self.active_chooser_rect().is_none() {
            self.chip_info = None;
            self.notice = "Enlarge active pane to open code graph".into();
            return;
        }
        let (sender, receiver) = mpsc::channel();
        let root = cwd.clone();
        std::thread::spawn(move || {
            let result = if stored {
                (|| {
                    let root = doxa_codegraph::worktree_root(&root)?;
                    let cwd = root.to_str().ok_or("worktree path is not UTF-8")?;
                    let (kind, path) = request.file_scope().expect("validated file scope");
                    let mut lore = doxa_lore::LoreClient::open(Duration::from_secs(3))
                        .map_err(|error| error.to_string())?;
                    lore.codegraph_snapshot(cwd, kind, path)
                        .map(|snapshot| stored_lines(&snapshot))
                        .map_err(|error| error.to_string())
                })()
            } else {
                doxa_codegraph::query(&root, request).map(|answer| answer_lines(&answer))
            };
            let _ = sender.send(result);
        });
        self.codegraph_pending = Some((id, cwd, receiver));
        self.input.clear();
        self.input_cursor = 0;
    }

    pub(super) fn poll_codegraph(&mut self) -> bool {
        let Some((id, cwd, receiver)) = &self.codegraph_pending else { return false; };
        let result = match receiver.try_recv() {
            Ok(value) => value,
            Err(TryRecvError::Empty) => return false,
            Err(TryRecvError::Disconnected) => Err("Code graph query worker stopped".into()),
        };
        let owner = (id.clone(), cwd.to_string_lossy().into_owned());
        let cwd = cwd.clone();
        self.codegraph_pending = None;
        let modal_matches = self.chip_info.as_ref().is_some_and(|info| info.kind == "codegraph" && info.owner.as_ref() == Some(&owner));
        if !modal_matches { return false; }
        if self.groups[self.active_group].active_id() != Some(owner.0.as_str())
            || self.session_cwds.get(&owner.0) != Some(&cwd) {
            self.chip_info = None;
            self.notice = "Session changed; reopen code graph".into();
            return true;
        }
        let info = self.chip_info.as_mut().expect("matched code graph modal");
        info.lines = match result {
            Ok(lines) => lines,
            Err(error) => vec!["Code graph query failed; no partial answer".into(), display(&error)],
        };
        info.scroll = 0;
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    use ratatui::layout::Rect;
    use std::fs;
    use std::process::Command;

    #[test]
    fn viewer_retains_ambiguity_unknown_and_exact_hashes() {
        let root = tempfile::tempdir().unwrap();
        assert!(Command::new("git").args(["init", "-q"]).arg(root.path()).status().unwrap().success());
        fs::write(root.path().join("lib.rs"), "fn work() { target(); }\nmod missing;\n#[cfg(unix)] mod optional;\n").unwrap();
        fs::write(root.path().join("other.rs"), "fn target() {}\nmod missing;\n").unwrap();
        fs::write(root.path().join("another.rs"), "fn target() {}\n").unwrap();
        fs::write(root.path().join("optional.rs"), "pub fn optional() {}\n").unwrap();
        let calls = doxa_codegraph::query(root.path(), Query::Calls("lib.rs".into())).unwrap();
        let lines = answer_lines(&calls).join("\n");
        assert!(lines.contains("ambiguous · multiple_observed_name_matches"));
        assert!(lines.contains(&calls.edges[0].sha256));
        assert!(lines.contains(&calls.edges[0].candidates[0].sha256));
        let modules = doxa_codegraph::query(root.path(), Query::Modules("lib.rs".into())).unwrap();
        let lines = answer_lines(&modules).join("\n");
        assert!(lines.contains("unknown"));
        assert!(lines.contains(&modules.module_edges[0].source_sha256));
        assert!(lines.contains("conditional candidate only: optional.rs"));
        assert!(modules.module_edges[1].target.is_none());
        assert!(lines.contains(modules.module_edges[1].conditional_candidate_sha256.as_deref().unwrap()));
        assert!(request("module lib.rs").is_err());
        assert!(request("file ../lib.rs\nattack").is_err());
    }

    #[test]
    fn viewer_labels_python_calls_and_import_modules_as_unresolved() {
        let root = tempfile::tempdir().unwrap();
        assert!(Command::new("git").args(["init", "-q"]).arg(root.path()).status().unwrap().success());
        fs::write(root.path().join("service.py"), "from package import helper\nhelper()\n").unwrap();
        let calls = doxa_codegraph::query(root.path(), Query::Calls("service.py".into())).unwrap();
        let lines = answer_lines(&calls).join("\n");
        assert!(lines.contains("python_name · unresolved · python_binding_unknown"));
        assert!(lines.contains(&calls.edges[0].sha256));
        let modules = doxa_codegraph::query(root.path(), Query::Modules("service.py".into())).unwrap();
        let lines = answer_lines(&modules).join("\n");
        assert!(lines.contains("unknown · python_from_import_declaration"));
        assert!(lines.contains("target: unknown"));
        assert!(lines.contains(&modules.module_edges[0].source_sha256));
    }

    #[test]
    fn stored_view_preserves_unknown_binding_and_candidate_details() {
        let row = doxa_lore::StoredCodegraph {
            project_key: "fixture".into(), worktree_root: "/fixture".into(),
            query: "modules".into(), path: "lib.rs".into(), revision: 3,
            source_sha256: "a".repeat(64), graph_sha256: "b".repeat(64),
            graph: serde_json::json!({"module_edges":[{"resolution":"unknown",
                "reason":"conditional_compilation_unverified",
                "conditional_candidate":"child.rs","target":null}]}),
            referenced_sources: doxa_lore::ReferenceFreshness { status: "unknown",
                checked_files: 0, issues: vec![] },
            scan_inputs: doxa_lore::ScanInputFreshness { status: "unknown",
                checked_files: 0, reason: "scan_input_digest_absent" },
        };
        let lines = stored_lines(&CodegraphSnapshot::Current(row)).join("\n");
        assert!(lines.contains("revision 3"));
        assert!(lines.contains("requested source verified at read time"));
        assert!(lines.contains("Included reference files: unknown"));
        assert!(lines.contains("Binding: unknown"));
        assert!(lines.contains("conditional_candidate"));
        assert!(lines.contains("conditional_compilation_unverified"));
        assert!(stored_lines(&CodegraphSnapshot::Missing).join(" ").contains("No reviewed"));
    }

    #[test]
    fn stored_command_rejects_symbol_without_starting_worker() {
        let mut app = App::default();
        app.open_codegraph("stored symbol Child");
        assert!(app.codegraph_pending.is_none());
        assert!(app.notice.contains("requires file|imports|calls|modules"));
    }

    #[test]
    fn command_stays_local_and_navigation_stops_at_answer_bounds() {
        let root = tempfile::tempdir().unwrap();
        assert!(Command::new("git").args(["init", "-q"]).arg(root.path()).status().unwrap().success());
        fs::write(root.path().join("lib.rs"), "fn work() { target(); }\nfn target() {}\n").unwrap();
        let mut app = App::default();
        app.size = Rect::new(0, 0, 96, 32);
        app.rail_visible = false;
        app.groups[0].tabs.push("session".into());
        app.session_cwds.insert("session".into(), root.path().to_path_buf());
        app.input = "/codegraph calls lib.rs".into();
        app.input_cursor = app.input.len();
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.codegraph_pending.is_some(), "notice: {} · input: {}", app.notice, app.input);
        assert!(app.pending_prompts.is_empty() && app.pending_queue_commands.is_empty());
        for _ in 0..100 {
            if app.poll_codegraph() { break; }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        let info = app.chip_info.as_ref().unwrap();
        assert_eq!(info.kind, "codegraph");
        assert!(info.lines.join("\n").contains("candidate_only"));
        let area = app.active_chooser_rect().unwrap();
        let count = wrapped_lines(&info.lines, usize::from(area.width.saturating_sub(3))).len();
        let max_scroll = count.saturating_sub(usize::from(area.height.saturating_sub(2)).max(1));
        for _ in 0..100 { app.key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)); }
        assert_eq!(app.chip_info.as_ref().unwrap().scroll, max_scroll);
        app.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.chip_info.is_none());
        app.open_codegraph("file lib.rs");
        app.groups[0].tabs.push("other".into());
        app.groups[0].active = 1;
        for _ in 0..100 {
            if app.poll_codegraph() { break; }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        assert!(app.chip_info.is_none());
        assert!(app.notice.contains("Session changed"));
    }
}
