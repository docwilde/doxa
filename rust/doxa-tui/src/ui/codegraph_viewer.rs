//! Read-only presentation of the bounded, fresh codegraph syntax queries.
use super::{App, ChipInfo};
use doxa_codegraph::{Answer, Query};
use std::sync::mpsc::{self, TryRecvError};

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
        format!("Coverage: {} listed · {} parsed Rust · {} skipped · {} unparseable",
            answer.coverage.enumerated_files, answer.coverage.parsed_rust_files,
            answer.coverage.skipped.count, answer.coverage.unparseable.count),
        "Syntax only · declarations and call candidates are not resolved Rust bindings".into(),
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
        hash(&mut lines, "source", &edge.source_sha256);
        lines.push(format!("  source read: {} Unix ms", edge.source_read_unix_ms));
        if let Some(value) = &edge.target_sha256 { hash(&mut lines, "target", value); }
        if let Some(value) = edge.target_read_unix_ms { lines.push(format!("  target read: {value} Unix ms")); }
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
        let request = match request(args) {
            Ok(value) => value,
            Err(error) => { self.notice = error.into(); return; }
        };
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
            lines: vec!["Scanning current Git worktree…".into()], scroll: 0, owner: Some(owner) });
        if self.active_chooser_rect().is_none() {
            self.chip_info = None;
            self.notice = "Enlarge active pane to open code graph".into();
            return;
        }
        let (sender, receiver) = mpsc::channel();
        let root = cwd.clone();
        std::thread::spawn(move || { let _ = sender.send(doxa_codegraph::query(&root, request)); });
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
            Ok(answer) => answer_lines(&answer),
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
        fs::write(root.path().join("lib.rs"), "fn work() { target(); }\nmod missing;\n").unwrap();
        fs::write(root.path().join("other.rs"), "fn target() {}\nmod missing;\n").unwrap();
        fs::write(root.path().join("another.rs"), "fn target() {}\n").unwrap();
        let calls = doxa_codegraph::query(root.path(), Query::Calls("lib.rs".into())).unwrap();
        let lines = answer_lines(&calls).join("\n");
        assert!(lines.contains("ambiguous · multiple_observed_name_matches"));
        assert!(lines.contains(&calls.edges[0].sha256));
        assert!(lines.contains(&calls.edges[0].candidates[0].sha256));
        let modules = doxa_codegraph::query(root.path(), Query::Modules("lib.rs".into())).unwrap();
        let lines = answer_lines(&modules).join("\n");
        assert!(lines.contains("unknown"));
        assert!(lines.contains(&modules.module_edges[0].source_sha256));
        assert!(request("module lib.rs").is_err());
        assert!(request("file ../lib.rs\nattack").is_err());
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
