//! Headless benchmark of the actual Rust 2.0 reducer, presenter, and draw path.
use std::env;
use std::error::Error;
use std::time::Instant;

use crossterm::event::{Event, KeyCode, KeyEvent, KeyModifiers};
use doxa_tui::ui::{App, Focus, Split};
use ratatui::backend::TestBackend;
use ratatui::Terminal;
use serde_json::{json, Value};

type Result<T> = std::result::Result<T, Box<dyn Error>>;
const WIDTH: u16 = 160;
const HEIGHT: u16 = 48;

fn terminal() -> Result<Terminal<TestBackend>> {
    Ok(Terminal::new(TestBackend::new(WIDTH, HEIGHT))?)
}

fn draw(app: &App, term: &mut Terminal<TestBackend>) -> Result<()> {
    term.draw(|frame| app.draw(frame))?;
    Ok(())
}

fn fixture() -> Result<(App, Terminal<TestBackend>)> {
    let mut app = App::default();
    app.handle(Event::Resize(WIDTH, HEIGHT));
    let mut term = terminal()?;
    for i in 0..7 {
        let frame = json!({"type":"hello", "session_id":format!("bench-{i}"),
            "model":format!("Session {i}"), "cwd": if i < 4 { "left" } else { "right" }});
        assert!(app.apply_daemon_frame(&frame));
    }
    app.groups[0].tabs = (0..4).map(|i| format!("bench-{i}")).collect();
    app.groups[1].tabs = (4..7).map(|i| format!("bench-{i}")).collect();
    app.groups[0].active = 3;
    app.groups[1].active = 2;
    app.active_group = 1;
    app.rail_visible = true;
    assert_eq!(app.sessions.len(), 7);
    assert_eq!(app.groups[0].tabs.len() + app.groups[1].tabs.len(), 7);
    draw(&app, &mut term)?;
    Ok((app, term))
}

fn first_frame() -> Result<()> {
    let mut app = App::default();
    let mut term = terminal()?;
    assert!(app.handle(Event::Resize(WIDTH, HEIGHT)));
    assert!(app.apply_daemon_frame(&json!({"type":"hello", "session_id":"bench-0",
        "model":"Session 0", "cwd":"left"})));
    draw(&app, &mut term)
}

fn seed(app: &mut App, term: &mut Terminal<TestBackend>) -> Result<()> {
    let transcript: String = (0..120)
        .map(|i| format!("- transcript line {i:03}: reproducible text\n"))
        .collect();
    for id in ["bench-3", "bench-6"] {
        assert!(app.apply_daemon_frame(&json!({"type":"event", "session_id":id,
            "event":{"type":"text_delta", "data":{"text":transcript}}})));
    }
    draw(app, term)
}

fn timed<F>(app: &mut App, term: &mut Terminal<TestBackend>, samples: &mut Vec<f64>, mut change: F) -> Result<()>
where F: FnMut(&mut App) -> bool {
    let start = Instant::now();
    assert!(change(app), "benchmark operation did not change state");
    draw(app, term)?;
    samples.push(start.elapsed().as_secs_f64() * 1000.0);
    Ok(())
}

fn key(code: KeyCode, modifiers: KeyModifiers) -> Event {
    Event::Key(KeyEvent::new(code, modifiers))
}

fn run_interactions(samples: &mut Samples) -> Result<()> {
    let (mut app, mut term) = fixture()?;
    // Match the Python scenario: 20 sidebar widths, then seed two visible panes.
    for width in 22..42 {
        timed(&mut app, &mut term, &mut samples.resize, |app| {
            app.rail_width = width;
            true
        })?;
    }
    seed(&mut app, &mut term)?;
    // Exercise the split input path separately from the sidebar resize samples.
    for i in 0..20 {
        let code = if i % 2 == 0 { KeyCode::Right } else { KeyCode::Left };
        timed(&mut app, &mut term, &mut samples.split, |app| {
            let before = app.split_percent;
            app.handle(key(code, KeyModifiers::ALT)) && app.split_percent != before
        })?;
    }
    assert_eq!(app.split, Split::Vertical);
    for i in 0..100 {
        let chunk = format!("delta {i:03}: {}\n", "x".repeat(28));
        assert_eq!(chunk.len(), 40);
        let frame: Value = json!({"type":"event", "session_id":"bench-6",
            "event":{"type":"text_delta", "data":{"text":chunk}}});
        timed(&mut app, &mut term, &mut samples.append, |app| app.apply_daemon_frame(&frame))?;
    }
    app.focus = Focus::Transcript;
    app.groups[1].scroll = 100;
    draw(&app, &mut term)?;
    for _ in 0..100 {
        timed(&mut app, &mut term, &mut samples.scroll, |app| {
            let before = app.groups[1].scroll;
            app.handle(key(KeyCode::Down, KeyModifiers::NONE)) && app.groups[1].scroll != before
        })?;
    }
    Ok(())
}

#[derive(Default)]
struct Samples { startup: Vec<f64>, resize: Vec<f64>, split: Vec<f64>, append: Vec<f64>, scroll: Vec<f64> }

fn stats(values: &[f64]) -> Value {
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.total_cmp(b));
    let n = sorted.len();
    if n == 0 { return json!({"n":0}); }
    let median = if n % 2 == 0 { (sorted[n/2 - 1] + sorted[n/2]) / 2.0 } else { sorted[n/2] };
    json!({"n":n, "p50_ms":median, "p95_ms":sorted[(n*95/100).min(n-1)],
        "min_ms":sorted[0], "max_ms":sorted[n-1]})
}

fn main() -> Result<()> {
    let args: Vec<String> = env::args().collect();
    let runs: usize = args.windows(2).find(|w| w[0] == "--runs")
        .map(|w| w[1].parse()).transpose()?.unwrap_or(5);
    let warmups: usize = args.windows(2).find(|w| w[0] == "--warmups")
        .map(|w| w[1].parse()).transpose()?.unwrap_or(2);
    if runs == 0 { return Err("--runs must be positive".into()); }
    for _ in 0..warmups {
        run_interactions(&mut Samples::default())?;
    }
    let mut samples = Samples::default();
    for _ in 0..runs {
        let started = Instant::now();
        first_frame()?;
        samples.startup.push(started.elapsed().as_secs_f64() * 1000.0);
        run_interactions(&mut samples)?;
    }
    println!("{}", serde_json::to_string_pretty(&json!({
        "source":"Rust 2.0 App reducer + Markdown presenter + Ratatui TestBackend draw",
        "size":[WIDTH, HEIGHT], "tabs":7, "groups":2,
        "sidebar_widths":[22,41], "warmups":warmups, "runs":runs,
        "results":{"startup":stats(&samples.startup), "resize":stats(&samples.resize),
            "split":stats(&samples.split), "append":stats(&samples.append),
            "scroll":stats(&samples.scroll)}
    }))?);
    Ok(())
}
