//! Deterministic gallery frames rendered through the production Ratatui App.
use crossterm::event::{Event, KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use doxa_tui::ui::{App, Focus};
use doxa_tui::{history, transport::TranscriptSnapshot};
use doxa_worktrees::RepoStatus;
use ratatui::{backend::TestBackend, style::Color, Terminal};
use serde_json::{json, Value};

fn key(app: &mut App, code: KeyCode, modifiers: KeyModifiers) {
    app.handle(Event::Key(KeyEvent::new(code, modifiers)));
}

fn event(app: &mut App, id: &str, kind: &str, data: Value) {
    app.apply_daemon_frame(&json!({"type":"event","session_id":id,"event":{"type":kind,"data":data}}));
}

fn tool_activity(app: &mut App) {
    event(app,"demo-codex-01","tool_call",json!({"id":"tool-1","name":"Read","input":{"path":"src/parser.rs"}}));
    event(app,"demo-codex-01","tool_result",json!({"id":"tool-1","name":"Read","result_summary":"File read successfully","duration_ms":42}));
    event(app,"demo-codex-01","tool_result_detail",json!({"id":"tool-1","text":"pub fn parse(input: &str) -> Result<Node, Error> {\n    check_bounds(input)?;\n    decode(input)\n}"}));
    event(app,"demo-codex-01","tool_call",json!({"id":"tool-2","name":"Edit","input":{"path":"src/parser.rs"}}));
    event(app,"demo-codex-01","tool_result",json!({"id":"tool-2","name":"Edit","result_summary":"Updated 2 hunks","duration_ms":81}));
    event(app,"demo-codex-01","tool_result_detail",json!({"id":"tool-2","text":"Updated src/parser.rs and src/parser_test.rs; bounds and reconnect checks passed."}));
}

// These are the normalized events emitted for a Codex webSearch whose
// action details first become available on item/completed. The gallery drives
// the production card reducer; it never calls an engine or browser.
fn completed_web_activity(app: &mut App) {
    let input = json!({"action":"search","queries":[
        "Rust parser bounds checks", "Rust reconnect regression tests"
    ]});
    let detail = format!("Web request completed.\nRequest: {}\nPage/search result content is not exposed by Codex in this tool event.",
        serde_json::to_string_pretty(&input).unwrap());
    let summary: String = detail.chars().take(280).collect();
    event(app,"demo-codex-01","tool_call",json!({"id":"web-1","name":"web_search",
        "input":{"details":"Request details not yet reported by Codex"}}));
    event(app,"demo-codex-01","tool_result",json!({"id":"web-1","name":"web_search",
        "input":input,"result_summary":summary,"duration_ms":126,"is_error":false}));
    event(app,"demo-codex-01","tool_result_detail",json!({"id":"web-1","text":detail}));
}

fn fixture() -> App {
    let mut app = App::default();
    app.handle(Event::Resize(126, 31));
    app.rail_width = 23;
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"demo-codex-01","engine":"codex","model":"gpt-6-sol","cwd":"/demo/project","lore_scrub":"ready"}));
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"demo-claude-02","engine":"claude","model":"claude-sonnet-4","cwd":"/demo/project","permission_mode":"default","can_set_permission_mode":true,"lore_scrub":"ready"}));
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"demo-deepseek-03","engine":"deepseek","model":"deepseek-chat","cwd":"/demo/project","lore_scrub":"ready"}));
    for id in ["demo-codex-01", "demo-claude-02", "demo-deepseek-03"] {
        app.set_lore_memory_usage(id, 3471, 8800, 2824, 4500);
    }
    app.groups[0].active = 0;
    app.sessions.iter_mut().for_each(|s| s.title = match s.id.as_str() {
        "demo-codex-01" => "Refactor parser".into(),
        "demo-claude-02" => "Review tests".into(),
        _ => "Write release notes".into(),
    });
    event(&mut app,"demo-codex-01","text_delta",json!({"text":"## Parser update\n\nThe boundary is now explicit. Three checks passed:\n\n- Input stays bounded\n- Errors keep their source\n- Reconnect restores the transcript\n\n| Check | Result |\n| --- | --- |\n| Parse | Passed |\n| Restore | Passed |\n\nRead the [parser guide](https://doc.rust-lang.org/book/ch09-02-recoverable-errors-with-result.html) for the error-handling contract."}));
    event(&mut app,"demo-codex-01","turn_done",json!({"input_tokens":4821,"output_tokens":918,"usage_scope":"session","usage_source":"codex_cli_turn_completed","ctx_percentage":18.0,"session_cost_usd":0.0187}));
    event(&mut app,"demo-claude-02","text_delta",json!({"text":"## Test review\n\nThe new cases cover clipped input and reconnects. One edge case remains in the transport fixture."}));
    event(&mut app,"demo-claude-02","turn_done",json!({"ctx_percentage":9.0,"session_cost_usd":0.0062}));
    app.notice = format!("Rust {} · fixture session", env!("CARGO_PKG_VERSION"));
    app
}

fn scene(name: &str) -> App {
    let mut app = fixture();
    match name {
        "hero" => {
            app.rail_visible = true;
            app.groups[0].tabs = vec!["demo-codex-01".into(), "demo-claude-02".into(), "demo-deepseek-03".into()];
            app.set_repo_status("demo-codex-01", RepoStatus::Repository {
                repo: "project".into(), base: Some("main".into()),
                checked_out: Some("feat".into()), sha: Some("a1b2c3d".into()),
                worktree: Some("feat".into()),
            });
        }
        "repo-picker" => {
            app.groups[0].tabs = vec!["demo-codex-01".into()];
            let cwd = std::env::current_dir().expect("gallery repository directory");
            app.apply_daemon_frame(&json!({"type":"hello","session_id":"demo-codex-01",
                "engine":"codex","model":"gpt-6-sol","cwd":cwd}));
            app.set_repo_status("demo-codex-01", RepoStatus::Directory { name: "doxa".into() });
            let mut terminal = Terminal::new(TestBackend::new(126, 31)).unwrap();
            terminal.draw(|frame| app.draw(frame)).unwrap();
            let label = ["d", "i", "r", " ", "d", "o", "x", "a"];
            let point = (0..31).find_map(|y| (0..=126-label.len() as u16).find_map(|x| {
                label.iter().enumerate().all(|(offset, symbol)|
                    terminal.backend().buffer()[(x + offset as u16, y)].symbol() == *symbol)
                    .then_some((x, y))
            })).expect("visible directory chip");
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
                column: point.0, row: point.1, modifiers: KeyModifiers::NONE }));
        }
        "claude-session" => {
            app.groups[0].tabs = vec!["demo-codex-01".into()];
            app.input = "/engine".into();
            key(&mut app, KeyCode::Enter, KeyModifiers::NONE);
            key(&mut app, KeyCode::Down, KeyModifiers::NONE);
            key(&mut app, KeyCode::Enter, KeyModifiers::NONE);
        }
        "tool-activity" => {
            tool_activity(&mut app);
        }
        "tool-expanded" => {
            app.groups[0].tabs = vec!["demo-codex-01".into()];
            if let Some(session) = app.sessions.iter_mut().find(|session| session.id == "demo-codex-01") {
                session.transcript.clear();
            }
            event(&mut app,"demo-codex-01","text_delta",json!({"text":
                "## Documentation check\n\nCodex reported the completed search action and its two queries."}));
            completed_web_activity(&mut app);
            app.focus = Focus::Transcript;
            key(&mut app, KeyCode::Enter, KeyModifiers::NONE);
            app.notice = "Fixture · completed Codex web request · no browser or provider".into();
        }
        "restored-tool" => {
            app.groups[0].tabs = vec!["demo-claude-02".into()];
            if let Some(session) = app.sessions.iter_mut().find(|session| session.id == "demo-claude-02") {
                session.transcript.clear();
            }
            let records = [
                json!({"type":"user","message":{"content":"Review the parser after reconnect."}}),
                json!({"type":"assistant","message":{"content":[{"type":"tool_use","id":"restore-1","name":"Read","input":{"path":"src/parser.rs"}}]}}),
                json!({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"restore-1","content":"pub fn parse(input: &str) -> Result<Node, Error> {\n    check_bounds(input)?;\n    decode(input)\n}\n\nThe remaining boundary case is covered by parser_test.rs."}]}}),
            ];
            let bytes = records.iter().map(Value::to_string).collect::<Vec<_>>().join("\n").into_bytes();
            let markdown = history::render(&TranscriptSnapshot { bytes, earlier_bytes_omitted: false });
            event(&mut app, "demo-claude-02", "text_delta", json!({"text":markdown,"snapshot":true}));
            app.focus = Focus::Transcript;
            key(&mut app, KeyCode::Enter, KeyModifiers::NONE);
            app.notice = "Restored session · expanded tool detail".into();
        }
        "processing" => {
            app.groups[0].tabs = vec!["demo-codex-01".into()];
            if let Some(session) = app.sessions.iter_mut().find(|session| session.id == "demo-codex-01") {
                session.transcript.clear();
            }
            event(&mut app,"demo-codex-01","turn_started",json!({"prompt":"Summarize the parser change."}));
            event(&mut app,"demo-codex-01","text_delta",json!({"text":"The parser now checks bounds before decoding."}));
            event(&mut app,"demo-codex-01","turn_done",json!({"is_error":false}));
            event(&mut app,"demo-codex-01","turn_started",json!({"prompt":"What should I test next?"}));
            tool_activity(&mut app);
            event(&mut app,"demo-codex-01","text_delta",json!({"text":"The bounds and reconnect checks pass. I am checking the remaining error paths."}));
        }
        "reasoning" => {
            app.groups[0].tabs = vec!["demo-deepseek-03".into()];
            if let Some(session) = app.sessions.iter_mut().find(|session| session.id == "demo-deepseek-03") {
                session.transcript.clear();
            }
            event(&mut app,"demo-deepseek-03","turn_started",json!({"prompt":"Compare the two parser designs."}));
            event(&mut app,"demo-deepseek-03","reasoning_progress",json!({"approx_tokens":128}));
        }
        "commands" => {
            app.groups[0].tabs = vec!["demo-codex-01".into()];
            for ch in "/mo".chars() { key(&mut app, KeyCode::Char(ch), KeyModifiers::NONE); }
        }
        "help" => {
            app.groups[0].tabs = vec!["demo-codex-01".into()];
            app.input = "/help".into();
            key(&mut app, KeyCode::Enter, KeyModifiers::NONE);
        }
        "needs-input" => {
            event(&mut app,"demo-codex-01","needs_input",json!({"id":"req-1","kind":"ask_user","title":"Choose migration target","questions":[{"question":"Where should the migration run?","header":"Environment","options":[{"label":"Staging","description":"Validate before release"},{"label":"Production","description":"Apply to live data"}]}]}));
        }
        "permissions" => {
            app.groups[0].tabs = vec!["demo-claude-02".into()];
            key(&mut app, KeyCode::Char('p'), KeyModifiers::ALT);
        }
        "effort" => {
            app.groups[0].tabs = vec!["demo-deepseek-03".into()];
            app.apply_daemon_frame(&json!({"type":"hello","session_id":"demo-deepseek-03",
                "engine":"deepseek","model":"deepseek-flash","effort":"high","cwd":"/demo/project"}));
            if let Some(session) = app.sessions.iter_mut().find(|session| session.id == "demo-deepseek-03") {
                session.title = "Compare models".into();
            }
            event(&mut app,"demo-deepseek-03","text_delta",json!({"text":
                "## Model options\n\nThe selected model supports reasoning effort controls.\n\n- This session reports high effort\n- Its next turn may use a different level\n- Model choices follow the selected engine"}));
            key(&mut app, KeyCode::Char('f'), KeyModifiers::ALT);
            app.notice = format!("Rust {} · effort fixture", env!("CARGO_PKG_VERSION"));
        }
        "history" => {
            app.show_history_fixture("layout", vec![
                history::OfflineSession { id: "saved-layout-01".into(), project: "doxa".into(),
                    markdown: "**You:** Improve the split layout".into(),
                    search_snippets: vec!["split layout responds to terminal resize".into(),
                        "pane layout keeps each prompt independent".into()], cwd: None },
                history::OfflineSession { id: "saved-layout-02".into(), project: "doxa".into(),
                    markdown: "**You:** Check the rail layout".into(),
                    search_snippets: vec!["rail layout preserves grouped sessions".into()], cwd: None },
            ]);
        }
        "queue" => {
            app.groups[0].tabs = vec!["demo-codex-01".into()];
            app.input = "/queue".into();
            key(&mut app, KeyCode::Enter, KeyModifiers::NONE);
            app.apply_daemon_frame(&json!({"type":"queue_list_reply","session_id":"demo-codex-01",
                "ok":true,"rows":[
                    {"id":"q4","preview":"Review the parser error path after this turn"},
                    {"id":"q7","preview":"Summarize the test failures and proposed fix"}
                ]}));
        }
        "beliefs" | "belief-hover" => {
            app.groups[0].tabs = vec!["demo-codex-01".into()];
            app.sessions.iter_mut().for_each(|session| session.transcript.clear());
            // The fixture uses canonical newest-first order, independent of
            // IDs. An unknown timestamp stays unknown, without an invented date.
            app.show_belief_browser_timed_fixture(0, &[
                (17, "project:parser", "Reconnect preserves the current conversation.", Some("2026-09-27T12:15:00Z")),
                (3, "user", "Prefer concise explanations.", Some("2026-09-26T08:40:00Z")),
                (41, "project:parser", "Run parser checks before release.", None),
            ]);
            app.notice = "Fixture · per-belief decisions · writes disabled".into();
            if name=="belief-hover" {
                let mut terminal=Terminal::new(TestBackend::new(126,31)).unwrap();
                terminal.draw(|frame|app.draw(frame)).unwrap();
                app.show_belief_hover_fixture(17);
            }
        }
        "memory" => {
            app.groups[0].tabs = vec!["demo-codex-01".into()];
            app.show_memory_menu_fixture(0,
                &["Prefer concise explanations.", "Keep test evidence in reports."],
                &["Run parser checks before release.", "Keep reconnect behavior stable."],
                &[]);
        }
        "memory-management" | "memory-change" => {
            app.groups[0].tabs=vec!["demo-codex-01".into()];
            app.sessions.iter_mut().for_each(|session|session.transcript.clear());
            let entries=["Run parser checks before release.", "Keep each pane's draft with its session."];
            let chars=entries.join("\n").chars().count();
            app.set_lore_memory_usage("demo-codex-01",chars as u64,8800,2824,4500);
            app.show_memory_manager_fixture(0,"project",json!({"scope":"project","key":"/demo/project","sha256":"f".repeat(64),"entries":entries,"chars":chars,"cap_chars":8800})).unwrap();
            if name=="memory-change"{
                key(&mut app,KeyCode::Char('e'),KeyModifiers::NONE);
                for _ in entries[0].chars(){key(&mut app,KeyCode::Backspace,KeyModifiers::NONE);}
                for ch in "Run parser checks before every release.".chars(){key(&mut app,KeyCode::Char(ch),KeyModifiers::NONE);}
                key(&mut app,KeyCode::Enter,KeyModifiers::NONE);
            }
            app.notice="Fixture · curated memory · writes disabled".into();
        }
        "fleet-review" => {
            app.groups[0].tabs=vec!["demo-codex-01".into()];
            app.sessions.iter_mut().for_each(|session|session.transcript.clear());
            app.show_fleet_review_fixture(&json!({"review_version":1,"run_id":"parser-checks","root":"/demo/fleet","cwd":"/demo/project","mode":"symmetric","workers":2,"sessions":2,"seed":7,"run_budget_usd":6.0,"allow_unbudgeted":false,"approval_policy":"none","approval_grace_s":0,"dry_run":false,"prompt_sha256":"a".repeat(64),"quiescence_timeout_s":1800,"quiescence_grace_s":5,"preflight":"Planned 2 workers; budget bounded","slots":[{"index":0,"engine":"codex","model":"gpt-6-sol","role":"worker"},{"index":1,"engine":"claude","model":"claude-sonnet-4","role":"worker"}]})).unwrap();
            // Drive the same reducer/visibility watermark used by the live menu.
            let mut terminal=Terminal::new(TestBackend::new(126,31)).unwrap();
            terminal.draw(|frame|app.draw(frame)).unwrap();
            key(&mut app,KeyCode::PageDown,KeyModifiers::NONE);
            app.notice="Fixture · fleet launch review · controller disabled".into();
        }
        "fleet-view" => {
            app.groups[0].tabs=vec!["demo-codex-01".into()];
            app.sessions.iter_mut().for_each(|session|session.transcript.clear());
            app.show_fleet_view_fixture("parser-checks",&[
                "Fleet · ↑/↓ select · Enter open · B runs · R refresh · Esc close",
                "fleet parser-checks — finished",
                "mode symmetric · sessions 2",
                "  slot 0 · worker · stopped · demo-codex-01",
                "  slot 1 · worker · stopped · demo-claude-02",
                "Native controller phase: finished",
                "Total budget USD: 6.0",
                "Approval policy: none",
                "Approvals asked: 0",
                "Auto approved: 0",
                "Attach a slot: /fleet attach parser-checks <index>",
            ]);
            app.notice="Fixture · recorded fleet view · no live controller".into();
        }
        _ => panic!("unknown scene: {name}"),
    }
    app
}

fn rgb(color: Color) -> [u8; 3] {
    match color {
        Color::Rgb(r,g,b) => [r,g,b],
        Color::Reset => [242,233,221],
        Color::Black => [0,0,0],
        Color::White => [255,255,255],
        _ => [242,233,221],
    }
}

fn main() {
    let name = std::env::args().nth(1).expect("scene name");
    let (width,height) = match name.as_str() {
        "hero" | "repo-picker" | "claude-session" | "tool-activity" | "tool-expanded" | "restored-tool" | "processing" | "reasoning" | "commands" | "help" | "needs-input" | "permissions" | "effort" | "history" | "queue" | "beliefs" | "belief-hover" | "memory" | "memory-management" | "memory-change" | "fleet-review" | "fleet-view" => (126,31),
        _ => panic!("unknown scene"),
    };
    let app = scene(&name);
    let mut terminal = Terminal::new(TestBackend::new(width,height)).unwrap();
    terminal.draw(|frame| app.draw(frame)).unwrap();
    let cells: Vec<Value> = (0..height).flat_map(|y| (0..width).map(move |x| (x,y)))
        .map(|(x,y)| {
            let cell = &terminal.backend().buffer()[(x,y)];
            json!([cell.symbol(),rgb(cell.fg),rgb(cell.bg),cell.modifier.bits()])
        }).collect();
    println!("{}",json!({"width":width,"height":height,"cells":cells}));
}
