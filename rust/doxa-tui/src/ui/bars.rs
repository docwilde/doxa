//! Font-independent colored cell bars shared by context and quota views.
use ratatui::{style::{Color, Style}, text::{Line, Span}};

/// Draws bracketed background-color cells. Segment lengths are capped to the
/// requested width and the remaining cells use `available`.
pub(super) fn colored(cells: usize, segments: &[(usize, Color)], available: Color) -> Line<'static> {
    let mut spans = vec![Span::raw("[")];
    let mut remaining = cells;
    for &(requested, color) in segments {
        let count = requested.min(remaining);
        if count > 0 {
            spans.push(Span::styled(" ".repeat(count), Style::default().bg(color)));
            remaining -= count;
        }
    }
    if remaining > 0 {
        spans.push(Span::styled(" ".repeat(remaining), Style::default().bg(available)));
    }
    spans.push(Span::raw("]"));
    Line::from(spans)
}

/// Unknown telemetry gets a neutral outline, never a fabricated empty/full bar.
pub(super) fn unknown(cells: usize) -> Line<'static> {
    let mut spans = vec![Span::raw("[")];
    if cells > 0 {
        spans.push(Span::styled(
            " ".repeat(cells),
            Style::default().bg(crate::theme::BORDER),
        ));
    }
    spans.push(Span::raw("]"));
    Line::from(spans)
}
