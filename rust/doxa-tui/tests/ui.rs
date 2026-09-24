use crossterm::event::{Event, KeyCode, KeyEvent, KeyModifiers};
use doxa_tui::ui::{App, Focus, Session, Split};
use ratatui::{backend::TestBackend, layout::Rect, Terminal};
use serde_json::json;

fn key(code: KeyCode, modifiers: KeyModifiers) -> Event {
    Event::Key(KeyEvent::new(code, modifiers))
}

fn session(id: &str, collection: &str) -> Session {
    Session {
        id: id.into(), title: format!("Session {id}"), collection: collection.into(),
        transcript: "# Heading\n\nTranscript body".into(), status: "Ready".into(),
    }
}

fn screen(app: &App, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal.draw(|frame| app.draw(frame)).unwrap();
    let buffer = terminal.backend().buffer();
    (0..height).map(|y| {
        (0..width).map(|x| buffer[(x, y)].symbol()).collect::<String>()
    }).collect::<Vec<_>>().join("\n")
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
    assert!(app.apply_daemon_frame(&json!({"type":"hello", "session_id":"abc", "model":"sol", "cwd":"repo"})));
    assert!(app.apply_daemon_frame(&json!({"type":"event", "event":{"type":"text_delta", "data":{"text":"hello"}}})));
    assert!(app.apply_daemon_frame(&json!({"type":"event", "event":{"type":"turn_done", "data":{}}})));
    assert_eq!(app.sessions[0].transcript, "hello");
    assert_eq!(app.sessions[0].status, "Ready");
    assert!(app.apply_daemon_frame(&json!({"type":"reply", "ok":false, "error":"busy"})));
    assert!(app.notice.contains("busy"));
}

#[test]
fn daemon_labels_and_errors_cannot_emit_terminal_controls() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello", "session_id":"abc", "model":"bad\u{1b}[31m", "cwd":"repo\nnext"}));
    app.apply_daemon_frame(&json!({"type":"reply", "ok":false, "error":"oops\u{1b}[0m\u{202e}"}));
    assert!(!app.sessions[0].title.contains('\u{1b}'));
    assert!(!app.sessions[0].collection.contains('\n'));
    assert!(!app.notice.contains('\u{1b}'));
    assert!(!app.notice.contains('\u{202e}'));
}

#[test]
fn streamed_transcript_is_bounded_without_resetting_user_scroll() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello", "session_id":"abc", "model":"test", "cwd":"repo"}));
    app.groups[0].scroll = 7;
    for _ in 0..20 {
        app.apply_daemon_frame(&json!({"type":"event", "event":{"type":"text_delta", "data":{"text":"x".repeat(32_000)}}}));
    }
    app.apply_daemon_frame(&json!({"type":"event", "event":{"type":"text_delta", "data":{"text":"🦀"}}}));
    assert!(app.sessions[0].transcript.len() <= 512 * 1024);
    assert!(app.sessions[0].transcript.ends_with("🦀"));
    assert_eq!(app.groups[0].scroll, 7);
    assert!(app.notice.contains("limited"));
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
