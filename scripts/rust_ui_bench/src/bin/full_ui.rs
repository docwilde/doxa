// SPDX-License-Identifier: AGPL-3.0-only
//! Visual DOXA shell model. This is a rendering benchmark, not a client.
use std::env;
use std::hint::black_box;
use std::time::{Duration, Instant};

use ratatui::backend::TestBackend;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::{Frame, Terminal};

const WIDTH: u16 = 160;
const HEIGHT: u16 = 48;
const WIDTHS: [u16; 20] = [22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41];
const BASE: Color = Color::Rgb(23, 21, 18);
const RAIL: Color = Color::Rgb(29, 27, 23);
const RAISED: Color = Color::Rgb(34, 31, 26);
const BORDER: Color = Color::Rgb(58, 52, 41);
const ORANGE: Color = Color::Rgb(217, 119, 87);
const TEXT: Color = Color::Rgb(242, 233, 221);
const SECONDARY: Color = Color::Rgb(216, 205, 187);
const DIM: Color = Color::Rgb(138, 128, 115);

struct Pane { name: String, transcript: String, scroll: u16 }
struct App { groups: [Vec<Pane>; 2], active: [usize; 2], sidebar_width: u16, focused: usize }

impl App {
    fn new() -> Self {
        let mut n = 0;
        let groups = std::array::from_fn(|g| (0..[4, 3][g]).map(|_| {
            n += 1;
            Pane { name: format!("session-{n}"), transcript: String::new(), scroll: 0 }
        }).collect());
        Self { groups, active: [3, 2], sidebar_width: 25, focused: 1 }
    }
    fn active_mut(&mut self, group: usize) -> &mut Pane { &mut self.groups[group][self.active[group]] }
    fn render(&self, frame: &mut Frame) {
        let root = Layout::default().direction(Direction::Horizontal)
            .constraints([Constraint::Length(self.sidebar_width), Constraint::Min(1)]).split(frame.area());
        self.sidebar(frame, root[0]);
        let split = Layout::default().direction(Direction::Horizontal)
            .constraints([Constraint::Percentage(50), Constraint::Percentage(50)]).split(root[1]);
        for (g, area) in split.iter().enumerate() { self.group(frame, g, *area); }
    }
    fn sidebar(&self, frame: &mut Frame, area: Rect) {
        frame.render_widget(Block::default().style(Style::default().bg(RAIL)), area);
        let mut rows = vec![Line::raw(""), Line::styled(" DOXA  /  sessions", Style::default().fg(TEXT).add_modifier(Modifier::BOLD)), Line::raw("")];
        rows.push(Line::styled("  ▾ doxa", Style::default().fg(Color::Rgb(95,179,179)).add_modifier(Modifier::BOLD)));
        for (g, group) in self.groups.iter().enumerate() {
            rows.push(Line::styled(format!("    ▾ pane {}  [{}]", g + 1, group.len()), Style::default().fg(SECONDARY)));
            for (i, pane) in group.iter().enumerate() {
                let active = i == self.active[g];
                let glyph = if active && g == self.focused { "●" } else { "·" };
                rows.push(Line::styled(format!("      {glyph} {}", pane.name), Style::default().fg(if active { ORANGE } else { DIM })));
            }
        }
        rows.push(Line::raw(""));
        rows.push(Line::styled("  F3 rail  ^P commands", Style::default().fg(DIM)));
        let inner = Rect { x: area.x, y: area.y, width: area.width.saturating_sub(1), height: area.height };
        frame.render_widget(Paragraph::new(Text::from(rows)), inner);
        frame.render_widget(Block::default().borders(Borders::RIGHT).border_style(Style::default().fg(BORDER)), area);
    }
    fn group(&self, frame: &mut Frame, g: usize, area: Rect) {
        let area = if g == 0 { Rect { width: area.width.saturating_sub(1), ..area } } else { area };
        let chunks = Layout::default().direction(Direction::Vertical)
            .constraints([Constraint::Length(2), Constraint::Min(5), Constraint::Length(1), Constraint::Length(4)]).split(area);
        let tabs: Vec<Span> = self.groups[g].iter().enumerate().map(|(i, p)| Span::styled(
            format!(" {} {} ", i + 1, p.name), Style::default().fg(if i == self.active[g] { ORANGE } else { DIM })
                .bg(if i == self.active[g] { RAISED } else { BASE })
                .add_modifier(if i == self.active[g] { Modifier::BOLD } else { Modifier::empty() })
        )).collect();
        frame.render_widget(Paragraph::new(Line::from(tabs)).style(Style::default().bg(BASE)), chunks[0]);
        self.transcript(frame, g, chunks[1]);
        let status = Line::from(vec![
            Span::styled(" Claude Sonnet 4.5 ", Style::default().fg(SECONDARY).bg(RAISED)),
            Span::styled("  ⎇ doxa  ", Style::default().fg(SECONDARY).bg(RAISED)),
            Span::styled("  ctx 0%  ", Style::default().fg(DIM).bg(RAISED)),
            Span::styled("  ready ", Style::default().fg(ORANGE).bg(RAISED)),
        ]);
        frame.render_widget(Paragraph::new(status).style(Style::default().bg(RAISED)), chunks[2]);
        let prompt = Paragraph::new("")
            .style(Style::default().fg(DIM).bg(RAISED))
            .block(Block::default().borders(Borders::ALL).border_style(Style::default().fg(if g == self.focused { ORANGE } else { BORDER })));
        frame.render_widget(prompt, Rect { x: chunks[3].x + 1, width: chunks[3].width.saturating_sub(2), height: chunks[3].height.saturating_sub(1), ..chunks[3] });
        if g == 0 { frame.render_widget(Block::default().borders(Borders::RIGHT).border_style(Style::default().fg(BORDER)), area); }
    }
    fn transcript(&self, frame: &mut Frame, g: usize, area: Rect) {
        frame.render_widget(Block::default().style(Style::default().bg(BASE)), area);
        let pane = &self.groups[g][self.active[g]];
        let mut lines = vec![Line::raw("")];
        // The opening cover remains in the scrollable transcript after boot.
        const MARK: [&str; 7] = ["       █       ", "      ███      ", "    ███████    ", "   █████████   ", "  ███████████  ", " █████████████ ", "███████████████"];
        const DELTA: [&str; 7] = ["    █    ", "   █ █   ", "  █   █  ", " █     █ ", "█       █", "█       █", "█████████"];
        const OMICRON: [&str; 7] = ["  ███  ", " █   █ ", "█     █", "█     █", "█     █", " █   █ ", "  ███  "];
        const XI: [&str; 7] = ["█████████", "         ", "         ", "  █████  ", "         ", "         ", "█████████"];
        const ALPHA: [&str; 7] = ["    █    ", "   █ █   ", "  █   █  ", " ███████ ", "█       █", "█       █", "█       █"];
        for i in 0..7 {
            let art = if inner_width(area) >= 58 { format!("{}   {}  {}  {}  {}", MARK[i], DELTA[i], OMICRON[i], XI[i], ALPHA[i]) }
                else { format!("{}   DOXA", MARK[i]) };
            lines.push(Line::styled(art, Style::default().fg(ORANGE)));
        }
        lines.push(Line::styled("                    belief earns knowledge", Style::default().fg(DIM)));
        lines.push(Line::raw(""));
        lines.push(Line::styled("  ▎ doxa", Style::default().fg(SECONDARY).bg(RAISED)));
        lines.push(Line::styled("    DOXA 1.18  ·  model Claude Sonnet 4.5", Style::default().fg(DIM).bg(RAISED)));
        lines.push(Line::raw(""));
        if !pane.transcript.is_empty() {
            lines.push(Line::styled("  ▾ Benchmark transcript", Style::default().fg(ORANGE).bg(RAISED).add_modifier(Modifier::BOLD)));
            for line in pane.transcript.lines() { lines.push(markdown_line(line)); }
        }
        let inner = Rect { x: area.x + 2, y: area.y, width: area.width.saturating_sub(4), height: area.height };
        frame.render_widget(Paragraph::new(Text::from(lines)).wrap(Wrap { trim: false }).scroll((pane.scroll, 0)), inner);
        // DOXA hides its transcript scrollbar; the scroll offset is still modeled.
    }
}

fn inner_width(area: Rect) -> u16 { area.width.saturating_sub(4) }

fn markdown_line(s: &str) -> Line<'static> {
    let style = if s.starts_with("# ") { Style::default().fg(ORANGE).add_modifier(Modifier::BOLD) }
        else if s.starts_with("- ") { Style::default().fg(TEXT) }
        else if s.starts_with("```") { Style::default().fg(DIM).bg(RAISED) }
        else { Style::default().fg(TEXT) };
    Line::styled(format!("    {s}"), style)
}
fn draw(term: &mut Terminal<TestBackend>, app: &App) -> Result<(), Box<dyn std::error::Error>> {
    term.draw(|f| app.render(f))?;
    black_box(term.backend().buffer());
    Ok(())
}
fn summary(name: &str, mut xs: Vec<Duration>) {
    xs.sort_unstable();
    let n = xs.len();
    let p50 = if n % 2 == 0 { (xs[n/2-1].as_secs_f64() + xs[n/2].as_secs_f64()) * 500.0 } else { xs[n/2].as_secs_f64() * 1000.0 };
    let p95 = xs[(n * 95 / 100).min(n-1)].as_secs_f64() * 1000.0;
    println!("{name}: iterations={n} p50_ms={p50:.4} p95_ms={p95:.4}");
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = env::args().skip(1).collect();
    let runs = args.windows(2).find(|w| w[0] == "--runs").and_then(|w| w[1].parse::<usize>().ok()).unwrap_or(3);
    let mut term = Terminal::new(TestBackend::new(WIDTH, HEIGHT))?;
    let mut app = App::new();
    if args.iter().any(|s| s == "--startup-only") { draw(&mut term, &app)?; return Ok(()); }
    if args.iter().any(|s| s == "--snapshot") {
        app.active_mut(0).transcript = "# Overview\n- One visible turn\n- **Markdown-like** content\n```rust\nlet doxa = true;\n```".into();
        app.active_mut(1).transcript = "# Benchmark\n- A second visible pane\n- Seven mounted session tabs".into();
        draw(&mut term, &app)?;
        let buffer = term.backend().buffer();
        for y in 0..HEIGHT {
            for x in 0..WIDTH {
                print!("{}", buffer.cell((x, y)).map(|c| c.symbol()).unwrap_or(" "));
            }
            println!();
        }
        return Ok(());
    }
    let mut resize = Vec::new(); let mut append = Vec::new(); let mut scroll = Vec::new();
    for _ in 0..runs {
        app = App::new();
        for _ in 0..10 { draw(&mut term, &app)?; }
        for width in WIDTHS {
            let start = Instant::now(); app.sidebar_width = width; draw(&mut term, &app)?; resize.push(start.elapsed());
        }
        let seed = (0..120).map(|i| format!("- transcript line {i:03}: reproducible text\n")).collect::<String>();
        for g in 0..2 { app.active_mut(g).transcript = seed.clone(); }
        draw(&mut term, &app)?;
        for i in 0..100 {
            let chunk = format!("delta {i:03}: {}\n", "x".repeat(28));
            let start = Instant::now();
            let pane = app.active_mut(1);
            pane.transcript.push_str(&chunk);
            pane.scroll = pane.transcript.lines().count().saturating_sub(39) as u16;
            draw(&mut term, &app)?; append.push(start.elapsed());
        }
        app.active_mut(1).scroll = 0; draw(&mut term, &app)?;
        for _ in 0..100 {
            let start = Instant::now(); app.active_mut(1).scroll += 1; draw(&mut term, &app)?; scroll.push(start.elapsed());
        }
    }
    summary("resize_draw", resize); summary("append_draw", append); summary("scroll_draw", scroll);
    Ok(())
}
