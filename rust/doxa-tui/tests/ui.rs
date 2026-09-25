use crossterm::event::{
    Event, KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use doxa_tui::{theme, ui::{App, Focus, Session, Split}};
use ratatui::{
    backend::TestBackend,
    layout::{Constraint, Direction, Layout, Rect},
    Terminal,
};
use serde_json::json;

fn key(code: KeyCode, modifiers: KeyModifiers) -> Event {
    Event::Key(KeyEvent::new(code, modifiers))
}

fn mouse(kind: MouseEventKind, column: u16, row: u16) -> Event {
    Event::Mouse(MouseEvent {
        kind,
        column,
        row,
        modifiers: KeyModifiers::NONE,
    })
}

fn pane_boundary(app: &App) -> (u16, u16) {
    let outer = app.size;
    let body = if app.rail_visible {
        Layout::default()
            .direction(Direction::Horizontal)
            .constraints([Constraint::Length(app.rail_width), Constraint::Min(1)])
            .split(outer)[1]
    } else {
        outer
    };
    let panes = Layout::default()
        .direction(if app.split == Split::Vertical {
            Direction::Horizontal
        } else {
            Direction::Vertical
        })
        .constraints([
            Constraint::Percentage(app.split_percent),
            Constraint::Percentage(100 - app.split_percent),
        ])
        .split(body);
    if app.split == Split::Vertical {
        (panes[1].x, body.y + 5)
    } else {
        (body.x + 5, panes[1].y)
    }
}

#[test]
fn vertical_divider_drag_respects_minimum_width_and_release() {
    let mut app = App::default();
    app.handle(Event::Resize(120, 40));
    let (x, y) = pane_boundary(&app);
    assert!(app.handle(mouse(MouseEventKind::Down(MouseButton::Left), x, y)));
    assert!(app.handle(mouse(MouseEventKind::Drag(MouseButton::Left), 110, y)));
    assert!(app.split_percent > 50);
    let (boundary, _) = pane_boundary(&app);
    assert!(120 - boundary >= 28);
    app.handle(mouse(MouseEventKind::Drag(MouseButton::Left), 0, y));
    let (boundary, _) = pane_boundary(&app);
    assert!(boundary >= app.rail_width + 28);
    app.handle(mouse(MouseEventKind::Up(MouseButton::Left), 0, y));
    let released = app.split_percent;
    assert!(!app.handle(mouse(MouseEventKind::Drag(MouseButton::Left), 95, y)));
    assert_eq!(app.split_percent, released);
}

#[test]
fn horizontal_divider_drag_respects_minimum_height_and_resize_cancels_drag() {
    let mut app = App::default();
    app.rail_visible = false;
    app.split = Split::Horizontal;
    app.handle(Event::Resize(90, 40));
    let (x, y) = pane_boundary(&app);
    assert!(app.handle(mouse(MouseEventKind::Down(MouseButton::Left), x, y)));
    app.handle(mouse(MouseEventKind::Drag(MouseButton::Left), x, 39));
    let (_, boundary) = pane_boundary(&app);
    assert!(40 - boundary >= 8);
    app.handle(mouse(MouseEventKind::Drag(MouseButton::Left), x, 0));
    let (_, boundary) = pane_boundary(&app);
    assert!(boundary >= 8);
    let settled = app.split_percent;
    app.handle(Event::Resize(90, 35));
    assert!(!app.handle(mouse(MouseEventKind::Drag(MouseButton::Left), x, 20)));
    assert_eq!(app.split_percent, settled);
}

#[test]
fn rail_drag_preserves_pane_space_and_modal_blocks_drag() {
    let mut app = App::default();
    app.handle(Event::Resize(100, 40));
    assert!(app.handle(mouse(MouseEventKind::Down(MouseButton::Left), 25, 5)));
    app.handle(mouse(MouseEventKind::Drag(MouseButton::Left), 90, 5));
    assert_eq!(app.rail_width, 44);
    app.handle(mouse(MouseEventKind::Drag(MouseButton::Left), 0, 5));
    assert_eq!(app.rail_width, 12);
    app.handle(mouse(MouseEventKind::Up(MouseButton::Left), 0, 5));
    assert!(app.handle(mouse(MouseEventKind::Down(MouseButton::Left), 12, 5)));
    app.apply_daemon_frame(&json!({"type":"hello", "session_id":"one", "model":"test"}));
    app.apply_daemon_frame(
        &json!({"type":"event", "session_id":"one", "event":{"type":"needs_input", "data":{
        "id":"req", "kind":"permission", "title":"Approve?"}}}),
    );
    assert!(!app.handle(mouse(MouseEventKind::Drag(MouseButton::Left), 35, 5)));
    assert!(!app.handle(mouse(MouseEventKind::Down(MouseButton::Left), 12, 5)));
    assert_eq!(app.rail_width, 12);
}

fn session(id: &str, collection: &str) -> Session {
    Session {
        id: id.into(),
        title: format!("Session {id}"),
        collection: collection.into(),
        transcript: "# Heading\n\nTranscript body".into(),
        status: "Ready".into(),
    }
}

fn screen(app: &App, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal.draw(|frame| app.draw(frame)).unwrap();
    let buffer = terminal.backend().buffer();
    (0..height)
        .map(|y| {
            (0..width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn panes_reclaim_footer_row_and_keep_notice_visible() {
    let mut app = App::default();
    app.notice = "Prompt queue full".into();
    let rendered = screen(&app, 80, 24);
    let status_row = rendered.lines().nth(23).unwrap();
    assert!(status_row.contains("Prompt queue"), "{rendered}");
    assert!(!rendered.contains("Ctrl+P actions"), "{rendered}");
    assert!(rendered.lines().nth(19).unwrap().contains("Vendor ?"));
}

#[test]
fn two_panes_keep_distinct_prompts_and_live_identity_chips_at_80x24() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"one","engine":"codex","model":"sol"}));
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"two","engine":"claude","model":null}));
    app.groups[1].tabs.push("two".into());
    for c in "first draft".chars() { app.handle(key(KeyCode::Char(c), KeyModifiers::NONE)); }
    app.handle(key(KeyCode::Tab, KeyModifiers::SHIFT));
    for c in "second draft".chars() { app.handle(key(KeyCode::Char(c), KeyModifiers::NONE)); }
    let rendered = screen(&app, 80, 24);
    assert!(rendered.contains("> first draft"), "{rendered}");
    assert!(rendered.contains("> second draft"), "{rendered}");
    assert!(rendered.contains(" codex "), "{rendered}");
    assert!(rendered.contains("+"), "{rendered}");
    assert!(rendered.contains(" claude "), "{rendered}");
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
    terminal.draw(|frame| app.draw(frame)).unwrap();
    let styled_cells = (25..52)
        .filter(|&x| terminal.backend().buffer()[(x, 19)].bg == theme::HIGHLIGHT)
        .count();
    assert!(styled_cells >= 5, "engine/model chips have no visible highlight");
    app.handle(key(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(app.take_prompts(), vec![("two".into(), "second draft".into())]);
    app.handle(key(KeyCode::Tab, KeyModifiers::SHIFT));
    assert_eq!(app.input, "first draft");
    app.apply_daemon_frame(&json!({"type":"event","session_id":"one","event":{"type":"model_changed","data":{"model":"astra"}}}));
    let rendered = screen(&app, 160, 24);
    assert!(rendered.contains(" astra "), "{rendered}");
    assert!(!rendered.lines().nth(19).unwrap_or("").contains(" sol "), "{rendered}");
}

#[test]
fn same_session_in_two_panes_keeps_independent_drafts() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"one","engine":"codex","model":"sol"}));
    app.groups[1].tabs.push("one".into());
    app.handle(key(KeyCode::Char('a'), KeyModifiers::NONE));
    app.handle(key(KeyCode::Tab, KeyModifiers::SHIFT));
    app.handle(key(KeyCode::Char('b'), KeyModifiers::NONE));
    let rendered = screen(&app, 80, 24);
    assert!(rendered.contains("> a"), "{rendered}");
    assert!(rendered.contains("> b"), "{rendered}");
    app.handle(key(KeyCode::Tab, KeyModifiers::SHIFT));
    assert_eq!(app.input, "a");
}

#[test]
fn visible_tab_labels_switch_sessions_and_restore_drafts_by_mouse() {
    let mut app = App::default();
    app.handle(Event::Resize(100, 24));
    app.apply_update(doxa_tui::ui::DaemonUpdate::Upsert(session("one", "Work")));
    app.apply_update(doxa_tui::ui::DaemonUpdate::Upsert(session("two", "Work")));
    app.groups[0].tabs = vec!["one".into(), "two".into()];
    for c in "first".chars() { app.handle(key(KeyCode::Char(c), KeyModifiers::NONE)); }
    assert!(screen(&app, 100, 24).lines().nth(1).unwrap().contains("Session two"));
    // The second label starts after the first label, its padding and divider.
    assert!(app.handle(mouse(MouseEventKind::Down(MouseButton::Left), 43, 1)));
    assert_eq!(app.groups[0].active, 1);
    app.focus = Focus::Prompt;
    for c in "second".chars() { app.handle(key(KeyCode::Char(c), KeyModifiers::NONE)); }
    assert!(app.handle(mouse(MouseEventKind::Down(MouseButton::Left), 29, 1)));
    assert_eq!(app.groups[0].active, 0);
    assert_eq!(app.input, "first");
    assert!(screen(&app, 100, 24).contains("Session one"));
    assert!(app.handle(mouse(MouseEventKind::Down(MouseButton::Left), 43, 1)));
    assert_eq!(app.input, "second");
}

#[test]
fn waiting_request_blinks_in_inactive_tab_and_pane_then_stops() {
    let mut app = App::default();
    app.apply_update(doxa_tui::ui::DaemonUpdate::Upsert(session("one", "Work")));
    app.apply_update(doxa_tui::ui::DaemonUpdate::Upsert(session("two", "Work")));
    app.groups[0].tabs = vec!["one".into(), "two".into()];
    app.groups[1].tabs = vec!["two".into()];
    app.apply_daemon_frame(&json!({"type":"event","session_id":"two","event":{"type":"needs_input","data":{
        "id":"req", "kind":"permission", "title":"Approve?"}}}));
    let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
    terminal.draw(|frame| app.draw(frame)).unwrap();
    let buffer = terminal.backend().buffer();
    // Inactive tab title and inactive pane border both show the alert phase.
    assert_eq!(buffer[(43, 1)].bg, theme::ERROR);
    assert_eq!(buffer[(63, 0)].fg, theme::ERROR);
    app.apply_daemon_frame(&json!({"type":"event","session_id":"two","event":{"type":"needs_input_resolved","data":{"id":"req"}}}));
    terminal.draw(|frame| app.draw(frame)).unwrap();
    assert_ne!(terminal.backend().buffer()[(43, 1)].bg, theme::ERROR);
    assert_eq!(terminal.backend().buffer()[(63, 0)].fg, theme::BORDER);
}

#[test]
fn identity_chips_follow_status_and_sanitize_daemon_values() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"one","engine":"bad\u{1b}[31m","model":null}));
    let rendered = screen(&app, 80, 24);
    assert!(!rendered.contains('\u{1b}'));
    assert!(!rendered.contains("[session]"));
    app.apply_daemon_frame(&json!({"type":"reply","ok":true,"status":{"session_id":"one","engine":"claude","model":"opus"}}));
    let rendered = screen(&app, 160, 24);
    assert!(rendered.contains(" claude "), "{rendered}");
    assert!(rendered.contains(" opus "), "{rendered}");
    app.apply_daemon_frame(&json!({"type":"event","session_id":"one","event":{"type":"model_changed","data":{"model":null}}}));
    let rendered = screen(&app, 160, 24);
    assert!(rendered.contains(" claude "), "{rendered}");
    assert!(!rendered.contains(" opus "));
}

#[test]
fn telemetry_chips_keep_per_session_provenance_and_unknowns() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"codex","engine":"codex","lore_scrub":"ready"}));
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"vendor","engine":"glm"}));
    app.groups[1].tabs.push("vendor".into());
    let unknown = screen(&app, 300, 24);
    assert!(unknown.contains("Ctx ?   Tokens ?   Beliefs ▾   Cost ?   LORE scrub ready"), "{unknown}");
    assert!(unknown.contains("Ctx ?   Tokens ?   Beliefs ▾   Cost ?   LORE ?"), "{unknown}");
    app.apply_daemon_frame(&json!({"type":"event","session_id":"codex","event":{"type":"turn_done","data":{
        "input_tokens":120,"output_tokens":30,"usage_scope":"session","usage_source":"codex_cli_turn_completed",
        "ctx_percentage":null,"session_cost_usd":null
    }}}));
    app.apply_daemon_frame(&json!({"type":"event","session_id":"vendor","event":{"type":"turn_done","data":{
        "prompt_tokens":7,"completion_tokens":3,"usage_scope":"turn","usage_source":"vendor_response"
    }}}));
    let rendered = screen(&app, 300, 24);
    assert!(rendered.contains("Tokens 120/30 session"), "{rendered}");
    assert!(rendered.contains("Tokens 7/3 turn"), "{rendered}");
    assert!(rendered.contains("Ctx ?"), "{rendered}");
    assert!(rendered.contains("Cost ?"), "{rendered}");
    app.apply_daemon_frame(&json!({"type":"reply","ok":true,"status":{"session_id":"codex","lore_scrub":"unavailable"}}));
    let rendered = screen(&app, 300, 24);
    assert!(rendered.contains("LORE scrub unavailable"), "{rendered}");
    app.apply_daemon_frame(&json!({"type":"telemetry_unavailable","session_id":"codex"}));
    assert!(screen(&app, 300, 24).contains("LORE ?"));
    app.apply_daemon_frame(&json!({"type":"event","session_id":"vendor","event":{"type":"turn_done","data":{
        "ctx_percentage":null,"cost_usd":null,"session_cost_usd":null
    }}}));
    assert!(screen(&app, 300, 24).contains("Ctx ?   Tokens ?   Beliefs ▾   Cost ?   LORE ?"));
}

#[test]
fn telemetry_status_restores_reported_values_without_inventing_zero_usage() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"one","engine":"codex"}));
    app.apply_daemon_frame(&json!({"type":"reply","ok":true,"status":{
        "session_id":"one","ctx_percentage":42.5,"total_cost_usd":0.0123,
        "belief_count":9,"usage":{"num_turns":2,"input_tokens":1000,"output_tokens":50,
            "cost_basis":"published_rates","unpriced_models":[]}
    }}));
    let rendered = screen(&app, 300, 24);
    assert!(rendered.contains("Ctx 42%"), "{rendered}");
    assert!(rendered.contains("Tokens 1000/50 session"), "{rendered}");
    assert!(rendered.contains("Cost $0.0123 est"), "{rendered}");
    assert!(rendered.contains("LORE 9 beliefs"), "{rendered}");
    app.apply_daemon_frame(&json!({"type":"reply","ok":true,"status":{
        "session_id":"one","usage":{"num_turns":0,"input_tokens":0,"output_tokens":0}
    }}));
    assert!(screen(&app, 300, 24).contains("Tokens ?"));
}

#[test]
fn keyboard_and_rail_select_sessions_into_independent_groups() {
    let mut app = App::default();
    app.apply_update(doxa_tui::ui::DaemonUpdate::Upsert(session("one", "Work")));
    app.apply_update(doxa_tui::ui::DaemonUpdate::Upsert(session("two", "Work")));
    assert!(app.handle(key(KeyCode::F(3), KeyModifiers::NONE)));
    assert!(!app.rail_visible);
    app.handle(key(KeyCode::F(3), KeyModifiers::NONE));
    app.handle(key(KeyCode::Tab, KeyModifiers::SHIFT));
    assert_eq!(app.active_group, 1);
    app.focus = Focus::Rail;
    app.handle(key(KeyCode::Down, KeyModifiers::NONE));
    app.handle(key(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(app.groups[1].tabs, vec!["two"]);
    assert_eq!(app.groups[0].tabs, vec!["one"]);
    assert_eq!(app.focus, Focus::Transcript);
    app.handle(key(KeyCode::Char('h'), KeyModifiers::ALT));
    assert_eq!(app.split, Split::Horizontal);
    app.handle(key(KeyCode::Down, KeyModifiers::ALT));
    assert_eq!(app.split_percent, 55);
    app.handle(Event::Resize(100, 40));
    assert_eq!(app.size, Rect::new(0, 0, 100, 40));
}

#[test]
fn daemon_frames_update_visible_session() {
    let mut app = App::default();
    assert!(app.apply_daemon_frame(
        &json!({"type":"hello", "session_id":"abc", "model":"sol", "cwd":"repo"})
    ));
    assert!(app.apply_daemon_frame(
        &json!({"type":"event", "session_id":"abc", "event":{"type":"text_delta", "data":{"text":"hello"}}})
    ));
    assert!(
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"abc", "event":{"type":"turn_done", "data":{}}}))
    );
    assert_eq!(app.sessions[0].transcript, "hello");
    assert_eq!(app.sessions[0].status, "Ready");
    assert!(app.apply_daemon_frame(&json!({"type":"reply", "ok":false, "error":"busy"})));
    assert!(app.notice.contains("busy"));
}

#[test]
fn tool_activity_modal_tracks_call_result_and_blocks_layout_mouse() {
    let mut app = App::default();
    app.handle(Event::Resize(100, 35));
    app.apply_daemon_frame(&json!({"type":"hello", "session_id":"one", "model":"sol"}));
    app.apply_daemon_frame(&json!({"type":"event", "session_id":"one", "event":{
        "type":"tool_call", "data":{"id":"call-1", "name":"Read", "input":{"file_path":"a.rs"}}
    }}));
    app.apply_daemon_frame(&json!({"type":"event", "session_id":"one", "event":{
        "type":"tool_result", "data":{"id":"call-1", "name":"Read", "result_summary":"ok",
            "is_error":false, "duration_ms":15}
    }}));
    assert!(app.handle(key(KeyCode::Char('t'), KeyModifiers::CONTROL)));
    let visible = screen(&app, 100, 35);
    assert!(visible.contains("Tool activity"));
    assert!(visible.contains("a.rs"));
    assert!(visible.contains("finished"));
    let width = app.rail_width;
    assert!(!app.handle(mouse(MouseEventKind::Down(MouseButton::Left), width, 5)));
    assert!(!app.handle(mouse(MouseEventKind::Drag(MouseButton::Left), 45, 5)));
    assert_eq!(app.rail_width, width);
    assert!(app.handle(key(KeyCode::Esc, KeyModifiers::NONE)));
    assert!(!screen(&app, 100, 35).contains("Tool activity · ↑/↓"));
}

#[test]
fn action_menu_opens_views_and_navigates_sessions_without_leaking_keys_to_prompt() {
    let mut app = App::default();
    app.handle(Event::Resize(80, 24));
    app.apply_update(doxa_tui::ui::DaemonUpdate::Upsert(session("one", "Work")));
    app.apply_update(doxa_tui::ui::DaemonUpdate::Upsert(session("two", "Work")));
    app.input = "draft".into();
    assert!(app.handle(key(KeyCode::Char('p'), KeyModifiers::CONTROL)));
    assert!(screen(&app, 80, 24).contains("Actions"));
    assert!(screen(&app, 80, 24).contains("Peer map"));
    assert!(app.handle(key(KeyCode::Char('x'), KeyModifiers::NONE)));
    assert_eq!(app.input, "draft");
    assert!(app.handle(key(KeyCode::Enter, KeyModifiers::NONE)));
    assert!(screen(&app, 80, 24).contains("Peer"));
    assert!(app.handle(key(KeyCode::Esc, KeyModifiers::NONE)));

    app.handle(key(KeyCode::Char('p'), KeyModifiers::CONTROL));
    app.handle(key(KeyCode::Down, KeyModifiers::NONE));
    app.handle(key(KeyCode::Enter, KeyModifiers::NONE));
    assert!(screen(&app, 80, 24).contains("Tool activity"));
    app.handle(key(KeyCode::Esc, KeyModifiers::NONE));

    app.rail_selected = 1;
    app.handle(key(KeyCode::Char('p'), KeyModifiers::CONTROL));
    app.handle(key(KeyCode::Down, KeyModifiers::NONE));
    app.handle(key(KeyCode::Down, KeyModifiers::NONE));
    app.handle(key(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(app.groups[0].tabs.len(), 2);
    assert_eq!(app.focus, Focus::Transcript);
    app.handle(key(KeyCode::Char('p'), KeyModifiers::CONTROL));
    for _ in 0..3 {
        app.handle(key(KeyCode::Down, KeyModifiers::NONE));
    }
    app.handle(key(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(app.groups[0].active, 0);
    app.handle(key(KeyCode::Char('p'), KeyModifiers::CONTROL));
    for _ in 0..4 {
        app.handle(key(KeyCode::Down, KeyModifiers::NONE));
    }
    app.handle(key(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(app.groups[0].active, 1);
    assert!(app.pending_prompts.is_empty());
}

#[test]
fn action_menu_stays_bounded_on_tiny_terminal_and_blocks_mouse_drag() {
    let mut app = App::default();
    app.handle(Event::Resize(20, 5));
    app.handle(key(KeyCode::Char('p'), KeyModifiers::CONTROL));
    for _ in 0..5 {
        app.handle(key(KeyCode::Down, KeyModifiers::NONE));
    }
    let tiny = screen(&app, 20, 5);
    assert!(tiny.contains("Actions"));
    let rail_width = app.rail_width;
    assert!(!app.handle(mouse(
        MouseEventKind::Down(MouseButton::Left),
        rail_width,
        2
    )));
    assert_eq!(app.rail_width, rail_width);
    app.handle(key(KeyCode::Esc, KeyModifiers::NONE));
    assert!(!screen(&app, 20, 5).contains("Actions · ↑/↓"));
}

#[test]
fn action_menu_cannot_cover_a_pending_permission_request() {
    let mut app = App::default();
    app.handle(Event::Resize(80, 24));
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"one","model":"test"}));
    app.apply_daemon_frame(&json!({"type":"event","session_id":"one","event":{
        "type":"needs_input","data":{"id":"req","kind":"permission","title":"Approve?"}
    }}));
    app.handle(key(KeyCode::Char('p'), KeyModifiers::CONTROL));
    let visible = screen(&app, 80, 24);
    assert!(visible.contains("Approve?"));
    assert!(!visible.contains("Actions · ↑/↓"));
    assert!(app.pending_answers.is_empty());
}

#[test]
fn daemon_labels_and_errors_cannot_emit_terminal_controls() {
    let mut app = App::default();
    app.apply_daemon_frame(
        &json!({"type":"hello", "session_id":"abc", "model":"bad\u{1b}[31m", "cwd":"repo\nnext"}),
    );
    app.apply_daemon_frame(&json!({"type":"reply", "ok":false, "error":"oops\u{1b}[0m\u{202e}"}));
    assert!(!app.sessions[0].title.contains('\u{1b}'));
    assert!(!app.sessions[0].collection.contains('\n'));
    assert!(!app.notice.contains('\u{1b}'));
    assert!(!app.notice.contains('\u{202e}'));
}

#[test]
fn streamed_transcript_is_bounded_without_resetting_user_scroll() {
    let mut app = App::default();
    app.apply_daemon_frame(
        &json!({"type":"hello", "session_id":"abc", "model":"test", "cwd":"repo"}),
    );
    app.groups[0].scroll = 7;
    for _ in 0..20 {
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"abc", "event":{"type":"text_delta", "data":{"text":"x".repeat(32_000)}}}));
    }
    app.apply_daemon_frame(
        &json!({"type":"event", "session_id":"abc", "event":{"type":"text_delta", "data":{"text":"🦀"}}}),
    );
    assert!(app.sessions[0].transcript.len() <= 512 * 1024);
    assert!(app.sessions[0].transcript.ends_with("🦀"));
    assert_eq!(app.groups[0].scroll, 7);
    assert!(app.notice.contains("limited"));
}

#[test]
fn long_transcript_can_render_its_first_and_last_lines() {
    let mut app = App::default();
    app.rail_visible = false;
    app.apply_update(doxa_tui::ui::DaemonUpdate::Upsert(session("long", "Work")));
    let lines: String = (0..70_000).map(|i| format!("{i:05}\n")).collect();
    assert!(lines.len() < 512 * 1024);
    app.sessions[0].transcript = format!("```\n{lines}```");

    assert!(screen(&app, 90, 25).contains("69999"));
    app.groups[0].scroll = usize::MAX;
    assert!(screen(&app, 90, 25).contains("00000"));
}

#[test]
fn structured_events_render_in_target_pane_and_track_status() {
    let mut app = App::default();
    for id in ["one", "two"] {
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":id, "model":id, "cwd":"repo"}));
    }
    app.groups[1].tabs.push("two".into());
    let events = [
        ("reasoning_delta", json!({"text":"Checking the repository"})),
        (
            "tool_call",
            json!({"id":"t1", "name":"Read", "input":{"path":"src/main.rs"}}),
        ),
        (
            "tool_result",
            json!({"id":"t1", "name":"Read", "result_summary":"2 lines", "duration_ms":12}),
        ),
        (
            "peer_message",
            json!({"from_title":"Worker", "body":"Ready for review"}),
        ),
        (
            "peer_joined",
            json!({"title":"Reviewer", "session_id":"peer1"}),
        ),
        ("tool_disabled", json!({"name":"Write", "reason":"policy"})),
    ];
    for (kind, data) in events {
        assert!(app.apply_daemon_frame(
            &json!({"type":"event", "session_id":"two", "event":{"type":kind, "data":data}})
        ));
    }
    assert!(app.apply_daemon_frame(&json!({"type":"event", "session_id":"two", "event":{"type":"needs_input", "data":{"kind":"permission", "tool_name":"Write"}}})));
    assert_eq!(app.sessions[1].status, "Needs input");
    assert!(app.apply_daemon_frame(&json!({"type":"event", "session_id":"two", "event":{"type":"needs_input_resolved", "data":{"id":"q1"}}})));
    assert_eq!(app.sessions[1].status, "Running");
    assert!(app.apply_daemon_frame(&json!({"type":"event", "session_id":"two", "event":{"type":"turn_done", "data":{"is_error":true, "error":"failed"}}})));
    assert_eq!(app.sessions[1].status, "Error");
    assert!(app.sessions[0].transcript.is_empty());
    let rendered = screen(&app, 110, 30);
    assert!(rendered.contains("Reasoning:"));
    assert!(rendered.contains("1 tool call"));
    assert!(rendered.contains("Peer Worker:"));
    assert!(app.sessions[1].transcript.contains("Turn failed: failed"));
}

#[test]
fn tool_activity_folds_in_transcript_and_expands_by_keyboard_or_mouse() {
    let mut app = App::default();
    app.handle(Event::Resize(110, 30));
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"one","model":"sol"}));
    for (kind, data) in [
        ("tool_call", json!({"id":"t1","name":"Read","input":"hidden-input"})),
        ("tool_result", json!({"id":"t1","name":"Read","result_summary":"hidden-result"})),
    ] {
        app.apply_daemon_frame(&json!({"type":"event","session_id":"one","event":{"type":kind,"data":data}}));
    }
    let collapsed = screen(&app, 110, 30);
    assert!(collapsed.contains("1 tool call"), "{collapsed}");
    assert!(!collapsed.contains("hidden-input"), "{collapsed}");
    assert!(!collapsed.contains("hidden-result"), "{collapsed}");

    app.handle(key(KeyCode::Tab, KeyModifiers::NONE));
    assert!(app.handle(key(KeyCode::Enter, KeyModifiers::NONE)));
    let expanded = screen(&app, 110, 30);
    assert!(expanded.contains("hidden-input"), "{expanded}");
    assert!(expanded.contains("hidden-result"), "{expanded}");
    assert!(app.handle(key(KeyCode::Enter, KeyModifiers::NONE)));
    let collapsed = screen(&app, 110, 30);
    let (row, column) = collapsed.lines().enumerate()
        .find_map(|(row, line)| line.find("1 tool call").map(|column| (row, line[..column].chars().count())))
        .expect("visible tool summary");
    assert!(app.handle(mouse(MouseEventKind::Down(MouseButton::Left), column as u16, row as u16)));
    assert!(screen(&app, 110, 30).contains("hidden-input"));
}

#[test]
fn structured_event_fields_are_escaped_and_bounded() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello", "session_id":"one", "model":"test"}));
    let hostile = format!("**bold**\u{1b}[31m\u{202e}{}", "x".repeat(2000));
    assert!(app.apply_daemon_frame(&json!({"type":"event", "session_id":"one", "event":{"type":"tool_result", "data":{"name":"Read", "result_summary":hostile}}})));
    let transcript = &app.sessions[0].transcript;
    assert!(transcript.contains("\\*\\*bold\\*\\*"));
    assert!(!transcript.contains('\u{1b}'));
    assert!(!transcript.contains('\u{202e}'));
    assert!(transcript.contains('…'));
    assert!(transcript.len() < 700);
    assert!(!app.apply_daemon_frame(
        &json!({"type":"event", "session_id":"one", "event":{"type":"future_event", "data":{"text":"ignored"}}})
    ));
}

#[test]
fn test_backend_renders_groups_transcript_prompt_and_small_terminal() {
    let mut app = App::default();
    app.apply_update(doxa_tui::ui::DaemonUpdate::Upsert(session("one", "Work")));
    app.groups[1].tabs.push("one".into());
    app.input = "draft".into();
    let rendered = screen(&app, 100, 25);
    assert!(rendered.contains("Work"));
    assert!(rendered.contains("Heading"));
    assert!(rendered.contains("Transcript body"));
    assert!(rendered.contains("draft"));
    assert!(rendered.contains("Pane 1"));
    assert!(rendered.contains("Pane 2"));
    assert!(screen(&app, 10, 3).contains("DOXA"));
}

#[test]
fn prompt_keeps_draft_until_transport_submits_it() {
    let mut app = App::default();
    app.handle(key(KeyCode::Char('x'), KeyModifiers::NONE));
    app.handle(key(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(app.input, "x");
    assert!(app.notice.contains("Select a session"));
    app.handle(key(KeyCode::Char('q'), KeyModifiers::CONTROL));
    assert!(app.should_quit);
}

#[test]
fn prompt_for_open_session_is_queued_once() {
    let mut app = App::default();
    app.apply_update(doxa_tui::ui::DaemonUpdate::Upsert(session("one", "Work")));
    app.handle(key(KeyCode::Char('x'), KeyModifiers::NONE));
    app.handle(key(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(app.take_prompts(), vec![("one".into(), "x".into())]);
    assert!(app.take_prompts().is_empty());
    assert!(app.input.is_empty());
}

#[test]
fn full_prompt_queue_preserves_draft() {
    let mut app = App::default();
    app.apply_update(doxa_tui::ui::DaemonUpdate::Upsert(session("one", "Work")));
    app.pending_prompts = vec![("one".into(), "queued".into()); 32];
    app.input = "next".into();
    app.handle(key(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(app.pending_prompts.len(), 32);
    assert_eq!(app.input, "next");
    assert!(app.notice.contains("queue full"));
}

#[test]
fn permission_requires_explicit_allow_and_preserves_prompt_draft() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello", "session_id":"one", "model":"test"}));
    app.input = "unfinished prompt".into();
    app.apply_daemon_frame(&json!({"type":"event", "session_id":"one", "event":{"type":"needs_input", "data":{
        "id":"req-1", "kind":"permission", "title":"Run shell command?", "tool_name":"Bash",
        "display_name":"Execute command", "description":"Deletes a file\u{1b}[31m", "input_summary":"rm file"}}}));
    let rendered = screen(&app, 90, 25);
    assert!(rendered.contains("Run shell command?"));
    assert!(rendered.contains("Execute command"));
    assert!(rendered.contains("Deletes a file"));
    assert!(!rendered.contains('\u{1b}'));
    app.handle(key(KeyCode::Enter, KeyModifiers::NONE));
    app.handle(key(KeyCode::Char('a'), KeyModifiers::NONE));
    assert!(app.take_answers().is_empty());
    assert_eq!(app.input, "unfinished prompt");
    app.handle(key(KeyCode::Char('A'), KeyModifiers::SHIFT));
    assert!(app.input_requests[0].allow_armed);
    assert!(app.take_answers().is_empty());
    app.handle(key(KeyCode::Enter, KeyModifiers::NONE));
    assert!(!app.input_requests[0].allow_armed);
    assert!(app.take_answers().is_empty());
    // Some terminals report uppercase letters without a SHIFT modifier.
    app.handle(key(KeyCode::Char('A'), KeyModifiers::NONE));
    assert!(app.take_answers().is_empty());
    app.handle(key(KeyCode::Char('Y'), KeyModifiers::NONE));
    assert_eq!(
        app.take_answers(),
        vec![("one".into(), "req-1".into(), json!({"decision":"allow"}))]
    );
    app.handle(key(KeyCode::Char('A'), KeyModifiers::SHIFT));
    assert!(app.take_answers().is_empty());
    app.apply_daemon_frame(
        &json!({"type":"answer_reply", "session_id":"one", "request_id":"req-1", "ok":false,
        "uncertain":true, "message":"timeout"}),
    );
    assert!(app.input_requests[0].sending);
    assert!(app.notice.contains("unconfirmed"));
    app.apply_daemon_frame(&json!({"type":"event", "session_id":"one", "event":{"type":"needs_input_resolved", "data":{"id":"req-1"}}}));
    assert!(app.input_requests.is_empty());
    assert_eq!(app.input, "unfinished prompt");
}

#[test]
fn shifted_confirmation_allows_only_after_second_key() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello", "session_id":"one", "model":"test"}));
    app.apply_daemon_frame(
        &json!({"type":"event", "session_id":"one", "event":{"type":"needs_input", "data":{
        "id":"req-shift", "kind":"permission", "tool_name":"Write"}}}),
    );
    app.handle(key(KeyCode::Char('A'), KeyModifiers::SHIFT));
    assert!(app.take_answers().is_empty());
    assert!(screen(&app, 90, 25).contains("Approval armed"));
    app.handle(key(KeyCode::Char('Y'), KeyModifiers::SHIFT));
    assert_eq!(app.take_answers()[0].2, json!({"decision":"allow"}));
}

#[test]
fn question_steps_and_failed_delivery_allow_retry() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello", "session_id":"one", "model":"test"}));
    app.apply_daemon_frame(
        &json!({"type":"event", "session_id":"one", "event":{"type":"needs_input", "data":{
        "id":"req-2", "kind":"ask_user", "questions":[
            {"question":"Color?", "options":[{"label":"Red"},{"label":"Blue"}]},
            {"question":"Size?", "options":[{"label":"Small"},{"label":"Large"}]}
        ]}}}),
    );
    app.handle(key(KeyCode::Char('2'), KeyModifiers::NONE));
    assert!(app.take_answers().is_empty());
    assert!(screen(&app, 90, 25).contains("Size?"));
    app.handle(key(KeyCode::Char('1'), KeyModifiers::NONE));
    assert_eq!(
        app.take_answers(),
        vec![(
            "one".into(),
            "req-2".into(),
            json!({"answers":{"Color?":"Blue","Size?":"Small"}})
        )]
    );
    assert_eq!(app.input_requests[0].step, 1);
    assert_eq!(app.input_requests[0].selected, 1);
    assert!(screen(&app, 90, 25).contains("Size?"));
    app.apply_daemon_frame(&json!({"type":"answer_reply", "session_id":"one", "request_id":"req-2", "ok":false, "message":"stale"}));
    assert!(!app.input_requests[0].sending);
    app.handle(key(KeyCode::Enter, KeyModifiers::NONE));
    assert_eq!(
        app.take_answers()[0].2,
        json!({"answers":{"Color?":"Blue","Size?":"Small"}})
    );
    assert_eq!(app.input_requests[0].step, 1);
    app.apply_daemon_frame(
        &json!({"type":"answer_reply", "session_id":"one", "request_id":"req-2", "ok":false,
        "uncertain":true, "message":"timeout"}),
    );
    assert!(!app.input_requests[0].sending);
    assert!(screen(&app, 90, 25).contains("Size?"));
    app.handle(key(KeyCode::Char('2'), KeyModifiers::NONE));
    assert_eq!(
        app.take_answers()[0].2,
        json!({"answers":{"Color?":"Blue","Size?":"Large"}})
    );
    app.handle(key(KeyCode::Esc, KeyModifiers::NONE));
    assert!(app.take_answers().is_empty());
    app.apply_daemon_frame(&json!({"type":"event", "session_id":"one", "event":{"type":"needs_input_resolved", "data":{"id":"req-2"}}}));
    assert!(app.input_requests.is_empty());
    app.handle(key(KeyCode::Char('q'), KeyModifiers::CONTROL));
    assert!(app.should_quit);
}

#[test]
fn request_text_is_sanitized_and_spawn_requires_explicit_allow() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello", "session_id":"one", "model":"test"}));
    app.apply_daemon_frame(&json!({"type":"event", "session_id":"one", "event":{"type":"needs_input", "data":{
        "id":"spawn-1", "kind":"spawn", "title":"Start child?\u{1b}[31m", "task":"Do work\u{202e} safely"}}}));
    let rendered = screen(&app, 90, 25);
    assert!(rendered.contains("Do work"));
    assert!(!rendered.contains('\u{1b}'));
    assert!(!rendered.contains('\u{202e}'));
    app.handle(key(KeyCode::Enter, KeyModifiers::NONE));
    assert!(app.take_answers().is_empty());
    app.handle(key(KeyCode::Esc, KeyModifiers::NONE));
    assert_eq!(app.take_answers()[0].2, json!({"decision":"deny"}));
}

#[test]
fn question_dialog_shows_full_question_header_and_descriptions_with_scroll() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello", "session_id":"one", "model":"test"}));
    let long_description = format!("{}Last line of option detail", "detail line\n".repeat(35));
    app.apply_daemon_frame(
        &json!({"type":"event", "session_id":"one", "event":{"type":"needs_input", "data":{
        "id":"question-1", "kind":"ask_user", "questions":[{
            "header":"Pick a color", "question":"Which color should the warning icon use?",
            "options":[
                {"label":"Red", "description":"Signals danger\u{1b}[31m"},
                {"label":"Blue", "description":long_description}
            ]
        }]}}}),
    );
    let first = screen(&app, 90, 25);
    assert!(first.contains("Pick a color"));
    assert!(first.contains("Which color should the warning icon use?"));
    assert!(first.contains("Signals danger"));
    assert!(!first.contains('\u{1b}'));
    for _ in 0..3 {
        app.handle(key(KeyCode::PageDown, KeyModifiers::NONE));
    }
    assert!(screen(&app, 90, 25).contains("Last line of option detail"));
    app.handle(key(KeyCode::Char('2'), KeyModifiers::NONE));
    assert_eq!(
        app.take_answers()[0].2,
        json!({"answers":{"Which color should the warning icon use?":"Blue"}})
    );
}
