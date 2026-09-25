//! Deterministic gallery frames rendered through the production Ratatui App.
use crossterm::event::{Event, KeyCode, KeyEvent, KeyModifiers};
use doxa_tui::ui::App;
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
    event(app,"demo-codex-01","tool_call",json!({"id":"tool-2","name":"Edit","input":{"path":"src/parser.rs"}}));
    event(app,"demo-codex-01","tool_result",json!({"id":"tool-2","name":"Edit","result_summary":"Updated 2 hunks","duration_ms":81}));
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
    event(&mut app,"demo-codex-01","text_delta",json!({"text":"## Parser update\n\nThe boundary is now explicit. Three checks passed:\n\n- Input stays bounded\n- Errors keep their source\n- Reconnect restores the transcript\n\n| Check | Result |\n| --- | --- |\n| Parse | Passed |\n| Restore | Passed |"}));
    event(&mut app,"demo-codex-01","turn_done",json!({"input_tokens":4821,"output_tokens":918,"usage_scope":"session","usage_source":"codex_cli_turn_completed","ctx_percentage":18.0,"session_cost_usd":0.0187}));
    event(&mut app,"demo-claude-02","text_delta",json!({"text":"## Test review\n\nThe new cases cover clipped input and reconnects. One edge case remains in the transport fixture."}));
    event(&mut app,"demo-claude-02","turn_done",json!({"ctx_percentage":9.0,"session_cost_usd":0.0062}));
    app.notice = "Rust 2.0.0-alpha.16 · fixture session".into();
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
        "tool-activity" => {
            tool_activity(&mut app);
        }
        "tool-expanded" => {
            tool_activity(&mut app);
            key(&mut app, KeyCode::Tab, KeyModifiers::NONE);
            key(&mut app, KeyCode::Enter, KeyModifiers::NONE);
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
            app.notice = "Rust 2.0.0-alpha.16 · effort fixture".into();
        }
        "history" => {
            key(&mut app, KeyCode::Char('r'), KeyModifiers::CONTROL);
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
        "memory" => {
            app.groups[0].tabs = vec!["demo-codex-01".into()];
            app.show_memory_menu_fixture(0,
                &["- Prefer concise explanations [source: codex]", "- Keep test evidence in reports"],
                &["- Run parser checks before release", "- Keep reconnect behavior stable"],
                &["- testing: Reconnect must preserve the transcript", "- release: Cite checks before publishing"]);
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
        "hero" | "tool-activity" | "tool-expanded" | "processing" | "needs-input" | "permissions" | "effort" | "history" | "queue" | "memory" => (126,31),
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
