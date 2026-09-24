use crossterm::event::{Event, KeyCode, KeyEvent, KeyModifiers};
use doxa_tui::ui::App;
use doxa_tui::peer_map::PeerMap;
use ratatui::{backend::TestBackend, Terminal};
use serde_json::json;

fn key(code: KeyCode, modifiers: KeyModifiers) -> Event {
    Event::Key(KeyEvent::new(code, modifiers))
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

fn map_screen(map: &PeerMap, owner: &str) -> String {
    let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
    terminal.draw(|frame| map.render(frame, frame.area(), owner)).unwrap();
    let buffer = terminal.backend().buffer();
    (0..30).map(|y| (0..100).map(|x| buffer[(x, y)].symbol()).collect::<String>())
        .collect::<Vec<_>>().join("\n")
}

#[test]
fn map_clamps_shared_selection_when_switching_to_smaller_scope() {
    let mut map = PeerMap::default();
    let few = (0..2).map(|n| json!({"session_id":format!("b-{n}"),"title":format!("B {n}")})).collect::<Vec<_>>();
    let many = (0..8).map(|n| json!({"session_id":format!("a-{n}"),"title":format!("A {n}")})).collect::<Vec<_>>();
    assert!(map.roster("owner-b", &json!({"ok":true,"peers":few})));
    assert!(map.roster("owner-a", &json!({"ok":true,"peers":many})));
    map.move_selected("owner-a", 7);
    assert!(map_screen(&map, "owner-b").contains("B 1"));
}

#[test]
fn invalid_event_before_roster_keeps_loading_text() {
    let mut map = PeerMap::default();
    assert!(!map.event("owner-a", "peer_left", &json!({"session_id":"unknown"})));
    assert!(map_screen(&map, "owner-a").contains("Peer map loading"));
}

#[test]
fn peer_map_opens_navigates_and_renders_observed_edges() {
    let mut app = App::default();
    app.apply_daemon_frame(
        &json!({"type":"hello","session_id":"self-1","model":"codex","cwd":"/repo"}),
    );
    app.apply_daemon_frame(
        &json!({"type":"peer_roster","session_id":"self-1","ok":true,"peers":[
            {"session_id":"peer-1","title":"Planner"},{"session_id":"peer-2","title":"Builder"}
        ]}),
    );
    app.apply_daemon_frame(
        &json!({"type":"event","session_id":"self-1","event":{"type":"peer_message",
        "data":{"from_id":"peer-1","from_title":"Planner","body":"hello"}}}),
    );
    app.apply_daemon_frame(
        &json!({"type":"event","session_id":"self-1","event":{"type":"peer_sent",
        "data":{"to":["peer-2","peer-2"],"kind":"direct"}}}),
    );
    assert!(app.handle(key(KeyCode::Char('m'), KeyModifiers::CONTROL)));
    let first = screen(&app, 100, 30);
    assert!(first.contains("Peer communications"));
    assert!(first.contains("Planner"));
    assert!(first.contains("1 received"));
    assert!(app.handle(key(KeyCode::Down, KeyModifiers::NONE)));
    let second = screen(&app, 100, 30);
    assert!(second.contains("Builder"));
    assert!(second.contains("1 sent"));
    assert!(app.handle(key(KeyCode::Esc, KeyModifiers::NONE)));
    assert!(!screen(&app, 100, 30).contains("Peer communications"));
}

#[test]
fn native_unavailable_and_empty_roster_are_honest() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"self-1","model":"codex"}));
    app.handle(key(KeyCode::Char('m'), KeyModifiers::CONTROL));
    app.apply_daemon_frame(
        &json!({"type":"peer_roster","session_id":"self-1","ok":false,
        "error":"unavailable"}),
    );
    assert!(screen(&app, 80, 24).contains("Peer discovery unavailable"));
    app.apply_daemon_frame(
        &json!({"type":"peer_roster","session_id":"self-1","ok":true,"peers":[]}),
    );
    assert!(screen(&app, 80, 24).contains("No live peers"));
}

#[test]
fn malformed_ids_and_terminal_escape_titles_never_reach_map() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"self-1","model":"codex"}));
    app.apply_daemon_frame(
        &json!({"type":"peer_roster","session_id":"self-1","ok":true,"peers":[
            {"session_id":"../escape","title":"unsafe"},
            {"session_id":"peer-1","title":"\u{1b}[31mRED\u{1b}[0m\nline"},
            {"session_id":"peer-1","title":"duplicate"}
        ]}),
    );
    app.handle(key(KeyCode::Char('m'), KeyModifiers::CONTROL));
    let shown = screen(&app, 80, 24);
    assert!(!shown.contains("unsafe"));
    assert!(!shown.contains("duplicate"));
    assert!(!shown.contains('\u{1b}'));
    assert!(shown.contains("RED"));
    app.apply_daemon_frame(
        &json!({"type":"peer_roster","session_id":"self-1","ok":true,"peers":"bad"}),
    );
    assert!(screen(&app, 80, 24).contains("malformed data"));
    app.apply_daemon_frame(
        &json!({"type":"peer_roster","session_id":"self-1","ok":true,
        "peers":[{"session_id":"../bad","title":"unsafe"}]}),
    );
    assert!(screen(&app, 80, 24).contains("no valid entries"));
}

#[test]
fn map_handles_tiny_terminals_and_peer_removal() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"self-1","model":"codex"}));
    app.apply_daemon_frame(
        &json!({"type":"peer_roster","session_id":"self-1","ok":true,"peers":[
            {"session_id":"peer-1","title":"One"}
        ]}),
    );
    app.handle(key(KeyCode::Char('m'), KeyModifiers::CONTROL));
    for (width, height) in [(20, 5), (24, 7), (40, 10), (48, 12)] {
        let _ = screen(&app, width, height);
    }
    app.apply_daemon_frame(
        &json!({"type":"event","session_id":"self-1","event":{"type":"peer_left",
        "data":{"session_id":"peer-1"}}}),
    );
    assert!(screen(&app, 80, 24).contains("No live peers"));
}

#[test]
fn graph_remains_bounded_with_many_peers_and_events() {
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello","session_id":"self-1","model":"codex"}));
    let peers = (0..32)
        .map(|n| {
            json!({"session_id":format!("peer-{n}"),
        "title":format!("Peer {n}")})
        })
        .collect::<Vec<_>>();
    app.apply_daemon_frame(
        &json!({"type":"peer_roster","session_id":"self-1","ok":true,"peers":peers}),
    );
    for _ in 0..100 {
        app.apply_daemon_frame(
            &json!({"type":"event","session_id":"self-1","event":{"type":"peer_sent",
            "data":{"to":["peer-0","peer-1","peer-2","peer-3","peer-4","peer-5"]}}}),
        );
    }
    app.handle(key(KeyCode::Char('m'), KeyModifiers::CONTROL));
    let rendered = screen(&app, 100, 30);
    assert!(rendered.contains("Peer communications"));
    for _ in 0..31 {
        app.handle(key(KeyCode::Down, KeyModifiers::NONE));
    }
    let rendered = screen(&app, 100, 30);
    assert!(rendered.contains("Peer 31"));
}
