//! Native terminal acceptance fixture. Uses production App/renderer, synthetic
//! transcript text, and no provider, session daemon, clipboard or network API.
use crossterm::{event::{Event, KeyCode, KeyModifiers}, execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen}};
use doxa_tui::ui::App;
use ratatui::{backend::CrosstermBackend, Terminal};
use serde_json::json;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = std::env::args().collect::<Vec<_>>();
    if !args.iter().any(|arg| arg == "--terminal") {
        println!("{}", json!({"version":env!("CARGO_PKG_VERSION")}));
        return Ok(());
    }
    let (name, source) = match args.get(1).map(String::as_str).unwrap_or("flowchart") {
        "flowchart" => ("Flowchart", "flowchart LR\n A[Start] --> B{Ready?}\n B -->|yes| C[Done]"),
        "sequence" => ("Sequence", "sequenceDiagram\n participant A as Alice\n participant B as Bob\n A->>B: Hello\n B-->>A: Ack"),
        "class" => ("Class", "classDiagram\n class Animal {\n  +name: string\n  +speak()\n }\n Animal <|-- Dog"),
        "gantt" => ("Gantt", "gantt\n title Delivery\n dateFormat YYYY-MM-DD\n section Work\n Design :done, d1, 2026-01-01, 3d\n Build :active, d2, after d1, 4d"),
        _ => return Err("choose flowchart, sequence, class or gantt".into()),
    };
    let mode = std::env::var("DOXA_IMAGE_MODE").unwrap_or_else(|_| "halfblock".into());
    let mut app = App::default();
    app.apply_daemon_frame(&json!({"type":"hello", "session_id":"mermaid-fixture",
        "engine":"claude", "model":"local Mermaid fixture", "cwd":std::env::current_dir()?}));
    app.groups[0].tabs = vec!["mermaid-fixture".into()];
    app.apply_daemon_frame(&json!({"type":"event", "session_id":"mermaid-fixture",
        "event":{"type":"text_delta", "data":{"text":format!(
            "## {name} preview\n\nRendered locally inside the DOXA transcript.\n\n```mermaid\n{source}\n```\n\nSynthetic fixture · no provider or hosted renderer.")}}}));
    app.notice = format!("Mermaid acceptance · {mode} · Ctrl+C closes fixture");
    crossterm::terminal::enable_raw_mode()?;
    execute!(std::io::stdout(), EnterAlternateScreen)?;
    let mut terminal = Terminal::new(CrosstermBackend::new(std::io::stdout()))?;
    app.configure_terminal_images(&mode);
    loop {
        app.poll_terminal_images();
        terminal.draw(|frame| app.draw(frame))?;
        if crossterm::event::poll(std::time::Duration::from_millis(50))? {
            let event = crossterm::event::read()?;
            if matches!(&event, Event::Key(key) if key.code == KeyCode::Char('c')
                && key.modifiers.contains(KeyModifiers::CONTROL)) { break; }
            app.handle(event);
        }
    }
    execute!(std::io::stdout(), LeaveAlternateScreen)?;
    crossterm::terminal::disable_raw_mode()?;
    Ok(())
}
