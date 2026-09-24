//! Narrow rendering prototype for comparison with scripts/bench_ui.py.
//! This is not a DOXA client or a port of DOXA's Textual widgets.

use std::env;
use std::fs::File;
use std::hint::black_box;
use std::io::{BufRead, BufReader};
use std::time::{Duration, Instant};

use ratatui::backend::TestBackend;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::{Frame, Terminal};

const HEIGHT: u16 = 48;
const DEFAULT_WIDTH: u16 = 160;
const TABS_PER_GROUP: [usize; 2] = [4, 3];
const WIDTHS: [u16; 20] = [
    22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41,
];

#[derive(Clone)]
struct Pane {
    title: String,
    transcript: Vec<String>,
    scroll: u16,
}

#[derive(Clone)]
struct App {
    groups: [Vec<Pane>; 2],
    active: [usize; 2],
    focused_group: usize,
    sidebar_width: u16,
}

impl App {
    fn fixture() -> Self {
        let mut number = 0;
        let groups = std::array::from_fn(|group| {
            (0..TABS_PER_GROUP[group])
                .map(|_| {
                    number += 1;
                    Pane {
                        title: format!("session-{number}"),
                        transcript: Vec::new(),
                        scroll: 0,
                    }
                })
                .collect()
        });
        Self {
            groups,
            active: [0, 0],
            focused_group: 1,
            sidebar_width: 25,
        }
    }

    fn visible_mut(&mut self) -> &mut Pane {
        &mut self.groups[self.focused_group][self.active[self.focused_group]]
    }

    fn render(&self, frame: &mut Frame) {
        let main = Layout::default()
            .direction(Direction::Horizontal)
            .constraints([Constraint::Length(self.sidebar_width), Constraint::Min(1)])
            .split(frame.area());
        let sidebar = self
            .groups
            .iter()
            .flat_map(|group| group.iter())
            .map(|pane| Line::raw(pane.title.as_str()))
            .collect::<Vec<_>>();
        frame.render_widget(
            Paragraph::new(Text::from(sidebar))
                .block(Block::default().borders(Borders::ALL).title("Sessions")),
            main[0],
        );
        let halves = Layout::default()
            .direction(Direction::Horizontal)
            .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
            .split(main[1]);
        for (group, area) in halves.iter().copied().enumerate() {
            self.render_group(frame, group, area);
        }
    }

    fn render_group(&self, frame: &mut Frame, group: usize, area: Rect) {
        let rows = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(1),
                Constraint::Min(1),
                Constraint::Length(1),
            ])
            .split(area);
        let tabs = self.groups[group]
            .iter()
            .enumerate()
            .map(|(index, pane)| {
                let style = if index == self.active[group] {
                    Style::default()
                        .fg(Color::Yellow)
                        .add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(Color::Gray)
                };
                Span::styled(format!(" {} ", pane.title), style)
            })
            .collect::<Vec<_>>();
        frame.render_widget(Paragraph::new(Line::from(tabs)), rows[0]);

        let pane = &self.groups[group][self.active[group]];
        let text = Text::from(
            pane.transcript
                .iter()
                .map(|s| Line::raw(s.as_str()))
                .collect::<Vec<_>>(),
        );
        let body = Paragraph::new(text)
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(pane.title.as_str()),
            )
            .wrap(Wrap { trim: false })
            .scroll((pane.scroll, 0));
        frame.render_widget(body, rows[1]);
        frame.render_widget(
            Paragraph::new(format!(
                "group {} | {} lines | scroll {}",
                group + 1,
                pane.transcript.len(),
                pane.scroll
            )),
            rows[2],
        );
    }
}

fn sample(
    draws: usize,
    mut run: impl FnMut(usize) -> Result<(), Box<dyn std::error::Error>>,
) -> Result<Vec<Duration>, Box<dyn std::error::Error>> {
    let mut samples = Vec::with_capacity(draws);
    for i in 0..draws {
        let start = Instant::now();
        run(i)?;
        samples.push(start.elapsed());
    }
    Ok(samples)
}

fn report(name: &str, mut samples: Vec<Duration>) {
    samples.sort_unstable();
    let n = samples.len();
    let p50 = if n % 2 == 0 {
        (samples[n / 2 - 1].as_secs_f64() + samples[n / 2].as_secs_f64()) * 500.0
    } else {
        samples[n / 2].as_secs_f64() * 1000.0
    };
    let p95 = samples[(n * 95 / 100).min(n - 1)].as_secs_f64() * 1000.0;
    println!("{name}: iterations={n} p50_ms={p50:.3} p95_ms={p95:.3}");
}

fn draw(term: &mut Terminal<TestBackend>, app: &App) -> Result<(), Box<dyn std::error::Error>> {
    term.draw(|frame| app.render(frame))?;
    black_box(term.backend().buffer());
    Ok(())
}

fn ingest_events(app: &mut App, path: &str) -> Result<usize, Box<dyn std::error::Error>> {
    let mut saw_hello = false;
    let mut deltas = 0;
    for line in BufReader::new(File::open(path)?).lines() {
        let frame: serde_json::Value = serde_json::from_str(&line?)?;
        match frame.get("type").and_then(|v| v.as_str()) {
            Some("hello") => {
                if frame.get("proto").and_then(|v| v.as_u64()) != Some(1) {
                    return Err("unsupported DOXA protocol version".into());
                }
                saw_hello = true;
            }
            Some("event") if saw_hello => {
                if frame.pointer("/event/type").and_then(|v| v.as_str()) == Some("text_delta") {
                    if let Some(text) = frame.pointer("/event/data/text").and_then(|v| v.as_str()) {
                        app.visible_mut().transcript.push(text.to_owned());
                        deltas += 1;
                    }
                }
            }
            _ => {}
        }
    }
    if !saw_hello {
        return Err("event stream has no protocol hello".into());
    }
    Ok(deltas)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = env::args().skip(1).collect();
    let events = args
        .iter()
        .position(|s| s == "--events")
        .and_then(|i| args.get(i + 1));
    let mut term = Terminal::new(TestBackend::new(DEFAULT_WIDTH, HEIGHT))?;
    let mut app = App::fixture();
    if let Some(path) = events {
        let count = ingest_events(&mut app, path)?;
        eprintln!("ingested_text_deltas={count}");
    }
    if args.iter().any(|s| s == "--startup-only") {
        draw(&mut term, &app)?;
        return Ok(());
    }
    for _ in 0..10 {
        draw(&mut term, &app)?;
    }

    // The terminal stays 160x48. The widths are the sidebar's width.
    report(
        "resize_draw",
        sample(WIDTHS.len(), |i| {
            app.sidebar_width = WIDTHS[i];
            draw(&mut term, &app)
        })?,
    );
    for group in 0..2 {
        let active = app.active[group];
        app.groups[group][active].transcript = (0..120)
            .map(|i| format!("- transcript line {i:03}: reproducible text"))
            .collect();
    }
    draw(&mut term, &app)?;
    let chunks = (0..100)
        .map(|i| {
            let chunk = format!("delta {i:03}: {}\n", "x".repeat(28));
            assert_eq!(chunk.len(), 40);
            chunk
        })
        .collect::<Vec<_>>();

    report(
        "append_draw",
        sample(100, |i| {
            let pane = app.visible_mut();
            pane.transcript
                .push(chunks[i].trim_end_matches('\n').to_owned());
            pane.scroll = pane.transcript.len().saturating_sub(44) as u16;
            draw(&mut term, &app)
        })?,
    );

    app.visible_mut().scroll = 0;
    draw(&mut term, &app)?;
    report(
        "scroll_draw",
        sample(100, |_| {
            app.visible_mut().scroll += 1;
            draw(&mut term, &app)
        })?,
    );

    Ok(())
}
