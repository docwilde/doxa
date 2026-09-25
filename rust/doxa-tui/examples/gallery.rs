//! Deterministic gallery frames rendered through the production Ratatui App.
use crossterm::event::{Event, KeyCode, KeyEvent, KeyModifiers};
use doxa_tui::ui::App;
use ratatui::{backend::TestBackend, style::Color, Terminal};
use serde_json::{json, Value};

fn key(app: &mut App, code: KeyCode, modifiers: KeyModifiers) {
    app.handle(Event::Key(KeyEvent::new(code, modifiers)));
}

fn event(app: &mut App, id: &str, kind: &str, data: Value) {
    app.apply_daemon_frame(&json!({"type":"event","session_id":id,"event":{"type":kind,"data":data}}));
}

fn fixture() -> App {
    let mut app = App::default();
    app.handle(Event::Resize(148, 31));
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"demo-codex-01","engine":"codex","model":"gpt-6-sol","cwd":"/demo/project","lore_scrub":"ready"}));
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"demo-claude-02","engine":"claude","model":"claude-sonnet-4","cwd":"/demo/project","permission_mode":"default","can_set_permission_mode":true,"lore_scrub":"ready"}));
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"demo-deepseek-03","engine":"deepseek","model":"deepseek-chat","cwd":"/demo/project","lore_scrub":"ready"}));
    app.groups[0].active = 0;
    app.groups[1].tabs.push("demo-claude-02".into());
    app.sessions.iter_mut().for_each(|s| s.title = match s.id.as_str() {
        "demo-codex-01" => "Refactor parser".into(),
        "demo-claude-02" => "Review tests".into(),
        _ => "Write release notes".into(),
    });
    event(&mut app,"demo-codex-01","text_delta",json!({"text":"## Parser update\n\nThe boundary is now explicit. Three checks passed:\n\n- Input stays bounded\n- Errors keep their source\n- Reconnect restores the transcript\n\n| Check | Result |\n| --- | --- |\n| Parse | Passed |\n| Restore | Passed |"}));
    event(&mut app,"demo-codex-01","turn_done",json!({"input_tokens":4821,"output_tokens":918,"usage_scope":"session","usage_source":"codex_cli_turn_completed","ctx_percentage":18.0,"session_cost_usd":0.0187}));
    event(&mut app,"demo-claude-02","text_delta",json!({"text":"## Test review\n\nThe new cases cover clipped input and reconnects. One edge case remains in the transport fixture."}));
    event(&mut app,"demo-claude-02","turn_done",json!({"ctx_percentage":9.0,"session_cost_usd":0.0062}));
    app.notice = "Rust 2.0.0-alpha.9 · fixture session".into();
    app
}

fn scene(name: &str) -> App {
    let mut app = fixture();
    match name {
        "hero" => {
            app.rail_visible = true;
            app.groups[0].tabs = vec!["demo-codex-01".into(), "demo-claude-02".into(), "demo-deepseek-03".into()];
        }
        "split-panes" => { app.rail_visible = false; }
        "tool-activity" => {
            app.groups[1].tabs.clear();
            event(&mut app,"demo-codex-01","tool_call",json!({"id":"tool-1","name":"Read","input":{"path":"src/parser.rs"}}));
            event(&mut app,"demo-codex-01","tool_result",json!({"id":"tool-1","name":"Read","result_summary":"File read successfully","duration_ms":42}));
            event(&mut app,"demo-codex-01","tool_call",json!({"id":"tool-2","name":"Edit","input":{"path":"src/parser.rs"}}));
            event(&mut app,"demo-codex-01","tool_result",json!({"id":"tool-2","name":"Edit","result_summary":"Updated 2 hunks","duration_ms":81}));
            key(&mut app, KeyCode::Char('t'), KeyModifiers::CONTROL);
        }
        "needs-input" => {
            app.groups[1].tabs.clear();
            event(&mut app,"demo-codex-01","needs_input",json!({"id":"req-1","kind":"ask_user","title":"Choose migration target","questions":[{"question":"Where should the migration run?","header":"Environment","options":[{"label":"Staging","description":"Validate before release"},{"label":"Production","description":"Apply to live data"}]}]}));
        }
        "permissions" => {
            app.groups[0].tabs = vec!["demo-claude-02".into()];
            app.groups[1].tabs.clear();
            key(&mut app, KeyCode::Char('p'), KeyModifiers::ALT);
        }
        "history" => {
            app.groups[1].tabs.clear();
            key(&mut app, KeyCode::Char('r'), KeyModifiers::CONTROL);
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
        "hero" => (148,31), "split-panes" => (160,31),
        "tool-activity" => (148,31), "needs-input" => (148,31),
        "permissions" => (148,31), "history" => (148,31),
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
