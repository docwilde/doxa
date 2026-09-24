//! Markdown presentation module. The first 2.0 milestone will replace this
//! placeholder with a CommonMark presenter shared by live and restored turns.

use ratatui::text::Line;

pub fn render(source: &str, _width: u16) -> Vec<Line<'static>> {
    source.lines().map(|line| Line::from(line.to_owned())).collect()
}
