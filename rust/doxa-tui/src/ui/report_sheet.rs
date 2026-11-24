//! Shared layout for menu reports and read-through confirmations.
use std::ops::Range;

use crossterm::event::KeyCode;
use ratatui::{
    Frame,
    layout::{Position, Rect},
    style::{Modifier, Style},
    text::{Line, Span},
    widgets::Paragraph,
};
use unicode_width::UnicodeWidthStr;

use super::{
    App, ChipInfo,
    menu_sheet::{self, Sheet},
};
use crate::theme;

enum Entry<'a> {
    Section(&'a str),
    Field(&'a str, &'a str),
    Detail(&'a str),
    Text(&'a str),
}

impl<'a> Entry<'a> {
    fn parse(text: &'a str, help: bool) -> Self {
        if let Some(heading) = text.strip_prefix("## ") {
            return Self::Section(heading);
        }
        if help {
            if let Some(detail) = text.strip_prefix("  ") {
                return Self::Detail(detail.strip_prefix("local · ").unwrap_or(detail));
            }
            return text
                .split_once(" · ")
                .map_or(Self::Text(text), |(key, value)| Self::Field(key, value));
        }
        let field = text.trim_start().split_once(": ").filter(|(label, _)| {
            !label.is_empty() && label.width() <= 56 && !label.starts_with("CONFIRM")
        });
        field.map_or(Self::Text(text), |(key, value)| Self::Field(key, value))
    }
}

struct Row {
    source: usize,
    line: Line<'static>,
}

pub(super) struct Report {
    sheet: Sheet,
    title: &'static str,
    heading: &'static str,
    footer: &'static str,
    rows: Vec<Row>,
    scroll: usize,
    owner_matches: bool,
}

impl Report {
    pub(super) fn kind(kind: &str) -> bool {
        matches!(
            kind,
            "fleet" | "fleet_review" | "fleet_setup" | "isolation" | "isolation_change" | "help"
        )
    }

    fn new(
        info: &ChipInfo,
        area: Rect,
        owner_matches: bool,
        heading: &'static str,
        footer: &'static str,
    ) -> Self {
        let title = match info.kind {
            "fleet_review"
                if info
                    .lines
                    .first()
                    .is_some_and(|line| line == "Resume fleet") =>
            {
                "Fleet resume review"
            }
            "fleet_review" => "Fleet launch review",
            "fleet_setup" => "New fleet",
            "fleet" => "Fleet",
            "isolation" => "Isolation",
            "isolation_change" => "Confirm isolation and internet",
            _ => "Help",
        };
        let sheet = Sheet::with_heading(area, !heading.is_empty());
        let width = usize::from(sheet.body.width.saturating_sub(3)).max(1);
        let unavailable = ["Session changed. Reopen isolation controls.".to_owned()];
        let source = if owner_matches {
            info.lines.as_slice()
        } else {
            &unavailable
        };
        let entries = source
            .iter()
            .enumerate()
            .filter_map(|(index, text)| {
                if index == 0 && matches!(info.kind, "fleet_review" | "fleet_setup") {
                    return None;
                }
                let (selected, text) = match text.strip_prefix("▸ ") {
                    Some(text) => (true, text),
                    None => (false, text.as_str()),
                };
                Some((index, selected, Entry::parse(text, info.kind == "help")))
            })
            .collect::<Vec<_>>();
        let labels = entries
            .iter()
            .filter_map(|(_, _, entry)| match entry {
                Entry::Field(key, _) => Some(key.width()),
                _ => None,
            })
            .max()
            .unwrap_or(0);
        let column = (width >= 48 && labels > 0).then_some(labels.min(width * 43 / 100).min(36));
        let mut rows = Vec::new();
        for (source, selected, entry) in entries {
            let indent = if selected { "› " } else { "  " };
            let key_style = Style::default()
                .fg(theme::TEXT)
                .add_modifier(Modifier::BOLD);
            let value_style = Style::default().fg(theme::SECONDARY);
            let mut emit = |spans: Vec<Span<'static>>| {
                let line = Line::from(spans);
                let line = if selected {
                    line.style(menu_sheet::selected_style(true))
                } else {
                    line
                };
                rows.push(Row { source, line });
            };
            match entry {
                Entry::Section(label) => {
                    for line in wrap(label, width) {
                        emit(vec![
                            Span::raw(indent),
                            Span::styled(line, key_style.fg(theme::ACCENT)),
                        ]);
                    }
                }
                Entry::Field(key, value) => {
                    if let Some(label_width) = column {
                        let labels = wrap(key, label_width);
                        let values = wrap(value, width.saturating_sub(label_width + 2).max(1));
                        for index in 0..labels.len().max(values.len()) {
                            let key = labels.get(index).map(String::as_str).unwrap_or("");
                            let value = values.get(index).map(String::as_str).unwrap_or("");
                            let gap = " ".repeat(label_width.saturating_sub(key.width()) + 2);
                            emit(vec![
                                Span::raw(indent),
                                Span::styled(key.to_owned(), key_style),
                                Span::raw(gap),
                                Span::styled(value.to_owned(), value_style),
                            ]);
                        }
                    } else {
                        for line in wrap(key, width) {
                            emit(vec![Span::raw(indent), Span::styled(line, key_style)]);
                        }
                        for line in wrap(value, width.saturating_sub(2).max(1)) {
                            emit(vec![Span::raw("    "), Span::styled(line, value_style)]);
                        }
                    }
                }
                Entry::Detail(detail) => {
                    if detail == "local" {
                        continue;
                    }
                    let padding = column.map_or(2, |label_width| label_width + 2);
                    for text in wrap(detail, width.saturating_sub(padding).max(1)) {
                        emit(vec![
                            Span::raw(" ".repeat(padding + 2)),
                            Span::styled(text, Style::default().fg(theme::MUTED)),
                        ]);
                    }
                }
                Entry::Text(text) => {
                    let style = if text.starts_with("CONFIRM") {
                        key_style.fg(theme::ACCENT)
                    } else {
                        value_style
                    };
                    for text in wrap(text, width) {
                        emit(vec![Span::raw(indent), Span::styled(text, style)]);
                    }
                }
            }
        }
        Self {
            sheet,
            title,
            heading,
            footer,
            rows,
            scroll: info.scroll,
            owner_matches,
        }
    }

    fn visible(&self) -> usize {
        usize::from(self.sheet.body.height)
    }

    fn max_scroll(&self) -> usize {
        self.rows.len().saturating_sub(self.visible())
    }

    fn page(&self) -> Range<usize> {
        let start = self.scroll.min(self.max_scroll());
        start..(start + self.visible()).min(self.rows.len())
    }

    pub(super) fn scroll_to(&self, key: KeyCode) -> Option<usize> {
        let start = self.page().start;
        let page = self.visible().max(1);
        Some(match key {
            KeyCode::Up => start.saturating_sub(1),
            KeyCode::Down => (start + 1).min(self.max_scroll()),
            KeyCode::PageUp => start.saturating_sub(page),
            KeyCode::PageDown => (start + page).min(self.max_scroll()),
            KeyCode::Home => 0,
            KeyCode::End => self.max_scroll(),
            _ => return None,
        })
    }

    pub(super) fn source_at(&self, point: Position) -> Option<usize> {
        let mut body = self.sheet.body;
        body.width = body.width.saturating_sub(1);
        if !body.contains(point) {
            return None;
        }
        let index = self.page().start + usize::from(point.y - body.y);
        self.rows.get(index).map(|row| row.source)
    }

    pub(super) fn reveal(&self, source: usize, last: bool) -> usize {
        let index = if last {
            self.rows.iter().rposition(|row| row.source == source)
        } else {
            self.rows.iter().position(|row| row.source == source)
        };
        let page = self.page();
        match index {
            Some(index) if index < page.start => index,
            Some(index) if index >= page.end => (index + 1)
                .saturating_sub(self.visible())
                .min(self.max_scroll()),
            _ => page.start,
        }
    }

    fn draw(&self, frame: &mut Frame<'_>) {
        let page = self.page();
        self.sheet.draw(frame, self.title);
        menu_sheet::line(
            frame,
            self.sheet.heading,
            self.heading,
            Style::default().fg(theme::ACCENT),
        );
        menu_sheet::line(
            frame,
            self.sheet.footer,
            self.footer,
            Style::default().fg(theme::MUTED),
        );
        frame.render_widget(
            Paragraph::new(
                self.rows[page.clone()]
                    .iter()
                    .map(|row| row.line.clone())
                    .collect::<Vec<_>>(),
            ),
            self.sheet.body,
        );
        menu_sheet::scrollbar(
            frame,
            self.sheet.body,
            self.rows.len(),
            self.visible(),
            page.start,
        );
    }
}

// Wrap on whitespace when possible. Long identifiers are hard-wrapped so all
// characters, including review digests and exact JSON, remain available.
fn wrap(text: &str, width: usize) -> Vec<String> {
    let text = crate::markdown::sanitize(text).replace('\n', "\\n");
    let mut rows = Vec::new();
    let mut row = String::new();
    let mut cells = 0;
    for word in text.split_inclusive(char::is_whitespace) {
        let length = word.width();
        if cells > 0 && cells + word.trim_end().width() > width {
            rows.push(std::mem::take(&mut row));
            cells = 0;
        }
        if length > width || cells + length > width {
            row.push_str(word);
            let mut parts = crate::memory_menu::wrap_review(&row, width);
            row = parts.pop().unwrap_or_default();
            cells = row.width();
            rows.extend(parts);
        } else {
            row.push_str(word);
            cells += length;
        }
    }
    rows.push(row);
    rows
}

impl App {
    pub(super) fn report_sheet(&self, area: Rect) -> Option<Report> {
        let info = self
            .chip_info
            .as_ref()
            .filter(|info| Report::kind(info.kind))?;
        let owner_matches = info
            .owner
            .as_ref()
            .is_none_or(|(id, _)| self.groups[self.active_group].active_id() == Some(id.as_str()));
        let compact = area.width < 40;
        let scroll = if compact {
            " ↑↓ scroll   Esc close"
        } else {
            " ↑↓ / PgUp/PgDn scroll   Esc close"
        };
        let cancel = if compact {
            " ↑↓ scroll   Esc cancel"
        } else {
            " ↑↓ / PgUp/PgDn scroll   Esc cancel"
        };
        let (heading, footer) = match info.kind {
            "fleet_review" => {
                let state = match &self.fleet_review {
                    Some(review) if review.armed => " Shift+Y launch",
                    Some(review) if review.complete.get() => " Shift+A arm   Shift+Y launch",
                    _ => " Read all rows to arm launch",
                };
                (state, cancel)
            }
            "isolation_change" => {
                let state = if self
                    .isolation_confirmation
                    .as_ref()
                    .is_some_and(|review| review.complete.get())
                {
                    " Shift+Y apply"
                } else {
                    " Read all rows to confirm"
                };
                (state, cancel)
            }
            "fleet_setup" => (" F10 review   ↑↓ field", " Enter edit   ←→ preset   Esc"),
            "isolation" => (" 1 offline   2 internet   3 native", scroll),
            "fleet"
                if self
                    .fleet_menu
                    .as_ref()
                    .is_some_and(|menu| menu.is_run_list()) =>
            {
                (
                    " Enter open   N new fleet",
                    " ↑↓ select   PgUp/PgDn scroll   Esc",
                )
            }
            "fleet" if compact => (
                " M alignment   B runs   N new",
                " ↑↓ scroll   R refresh   Esc",
            ),
            "fleet" => (
                " M messages/alignment   B runs   N new fleet",
                " ↑↓ scroll   R refresh   Esc close",
            ),
            _ => ("", scroll),
        };
        Some(Report::new(
            info,
            area,
            owner_matches,
            if owner_matches { heading } else { "" },
            footer,
        ))
    }

    pub(super) fn draw_report_sheet(&self, frame: &mut Frame<'_>, area: Rect) -> bool {
        let Some(mut report) = self.report_sheet(area) else {
            return false;
        };
        let page = report.page();
        if report.owner_matches && !page.is_empty() && self.active_chooser_rect() == Some(area) {
            match self.chip_info.as_ref().unwrap().kind {
                "fleet_review" => {
                    if let Some(review) = self
                        .fleet_review
                        .as_ref()
                        .filter(|review| page.start <= review.seen.get())
                    {
                        review.seen.set(review.seen.get().max(page.end));
                        review.complete.set(page.end == report.rows.len());
                        if review.complete.get() && !review.armed {
                            report.heading = " Shift+A arm   Shift+Y launch";
                        }
                    }
                }
                "isolation_change" => {
                    if let Some(review) = self
                        .isolation_confirmation
                        .as_ref()
                        .filter(|review| page.start <= review.seen.get())
                    {
                        review.seen.set(review.seen.get().max(page.end));
                        review
                            .complete
                            .set(review.complete.get() || page.end == report.rows.len());
                        if review.complete.get() {
                            report.heading = " Shift+Y apply";
                        }
                    }
                }
                _ => {}
            }
        }
        report.draw(frame);
        true
    }

    pub(super) fn report_scroll(&mut self, key: KeyCode) -> bool {
        let Some(scroll) = self
            .active_chooser_rect()
            .and_then(|area| self.report_sheet(area))
            .and_then(|report| report.scroll_to(key))
        else {
            return false;
        };
        self.chip_info.as_mut().unwrap().scroll = scroll;
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::{Event, KeyEvent, KeyModifiers};
    use ratatui::{Terminal, backend::TestBackend};
    use serde_json::json;

    fn app(width: u16, height: u16) -> App {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(width, height));
        app
    }

    fn draw(app: &App) -> Vec<String> {
        let mut terminal =
            Terminal::new(TestBackend::new(app.size.width, app.size.height)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let buffer = terminal.backend().buffer();
        (0..app.size.height)
            .map(|y| {
                (0..app.size.width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect()
            })
            .collect()
    }

    fn value_column(screen: &[String], label: &str, value: &str) -> usize {
        let row = screen
            .iter()
            .find(|row| row.contains(label) && row.contains(value))
            .unwrap_or_else(|| {
                panic!(
                    "{label} and {value} not shown together:\n{}",
                    screen.join("\n")
                )
            });
        row[..row.find(value).unwrap()].width()
    }

    #[test]
    fn visible_value_columns_align_in_fleet_help_and_isolation() {
        let mut app = app(120, 38);
        app.show_fleet_view_fixture(
            "demo-run",
            &["## Fleet", "Mode: supervised", "Approval policy: Manual"],
        );
        let screen = draw(&app);
        assert_eq!(
            value_column(&screen, "Mode", "supervised"),
            value_column(&screen, "Approval policy", "Manual")
        );
        app.fleet_menu = None;
        app.open_help();
        let screen = draw(&app);
        assert_eq!(
            value_column(&screen, "Tab / Shift+Tab", "Move focus"),
            value_column(&screen, "Enter", "Open a focused chip")
        );
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"iso", "cwd":"/fixture",
            "isolation":{"profile":"docker-open","state":"ready","engine":"rootless Docker","network":"bridge"}}));
        app.open_isolation_info(0);
        let screen = draw(&app);
        assert_eq!(
            value_column(&screen, "Engine", "rootless Docker"),
            value_column(&screen, "Network", "bridge")
        );
    }

    #[test]
    fn narrow_fleet_review_keeps_exact_digest_and_cannot_skip_unread_rows() {
        let mut app = app(38, 24);
        let digest = "0123456789abcdef".repeat(4);
        app.show_fleet_review_fixture(&json!({
            "root": "/fixtures/fleet",
            "run_id": "narrow",
            "mode": "supervised",
            "workers": 2,
            "sessions": 3,
            "prompt_sha256": digest,
            "assignments_sha256": "foo".repeat(40),
            "test_recipe": {"argv": ["cargo", "test", "--workspace"], "cwd_relative": ".", "timeout_s": 60},
        })).unwrap();
        let key = |code| KeyEvent::new(code, KeyModifiers::NONE);
        let area = app.active_chooser_rect().unwrap();
        let report = app.report_sheet(area).unwrap();
        let digest_row = app
            .chip_info
            .as_ref()
            .unwrap()
            .lines
            .iter()
            .position(|line| line.starts_with("Task digest: "))
            .unwrap();
        let digest_lines: Vec<_> = report
            .rows
            .iter()
            .filter(|row| row.source == digest_row)
            .map(|row| {
                row.line
                    .spans
                    .iter()
                    .map(|span| span.content.as_ref())
                    .collect::<String>()
            })
            .collect();
        assert!(digest_lines[0].ends_with("Task digest"));
        assert_eq!(
            digest_lines[1..]
                .iter()
                .map(|row| row.trim())
                .collect::<String>(),
            digest
        );
        assert!(
            report
                .rows
                .iter()
                .all(|row| row.line.width() < usize::from(report.sheet.body.width))
        );

        draw(&app);
        app.fleet_review_key(key(KeyCode::End));
        draw(&app);
        app.fleet_review_key(KeyEvent::new(KeyCode::Char('A'), KeyModifiers::SHIFT));
        assert!(!app.fleet_review.as_ref().unwrap().armed);
        app.fleet_review_key(key(KeyCode::Home));
        for _ in 0..report.rows.len() {
            draw(&app);
            if app.fleet_review.as_ref().unwrap().complete.get() {
                break;
            }
            app.fleet_review_key(key(KeyCode::PageDown));
        }
        assert!(app.fleet_review.as_ref().unwrap().complete.get());
        assert!(draw(&app).iter().any(|row| row.contains("Shift+A arm")));
        app.fleet_review_key(KeyEvent::new(KeyCode::Char('A'), KeyModifiers::SHIFT));
        assert!(app.fleet_review.as_ref().unwrap().armed);
        app.handle(Event::Resize(40, 24));
        assert!(!app.fleet_review.as_ref().unwrap().complete.get());
        assert!(!app.fleet_review.as_ref().unwrap().armed);
    }

    #[test]
    fn wrapped_fleet_headers_do_not_change_mouse_choice_indices() {
        let info = ChipInfo {
            kind: "fleet",
            label: String::new(),
            lines: vec![
                format!("Fleet root: /{}", "workspace/".repeat(15)),
                "## Recorded runs".into(),
                "▸ first-run".into(),
                "second-run".into(),
            ],
            scroll: 0,
            owner: None,
        };
        let area = Rect::new(0, 0, 38, 9);
        let mut report = Report::new(&info, area, true, "Enter open", "Esc close");
        report.scroll = report.reveal(3, false);
        let position =
            report.rows.iter().position(|row| row.source == 3).unwrap() - report.page().start;
        assert_eq!(
            report.source_at(Position::new(
                report.sheet.body.x + 2,
                report.sheet.body.y + position as u16
            )),
            Some(3)
        );
        assert_eq!(
            report.source_at(Position::new(
                report.sheet.footer.x + 2,
                report.sheet.footer.y
            )),
            None
        );
    }
}
