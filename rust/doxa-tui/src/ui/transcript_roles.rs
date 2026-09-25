//! Compact speaker styling for transcript headings and message bodies.
//! The transcript remains ordinary Markdown for persistence and replay.

use ratatui::{
    style::{Modifier, Style},
    text::{Line, Span},
};

use crate::{markdown, theme};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Speaker {
    User,
    Assistant,
}

/// Recognize a standalone transcript heading. Callers should avoid treating
/// text inside a fenced code block as a heading.
pub(super) fn heading(paragraph: &str) -> Option<(Speaker, Line<'static>)> {
    match paragraph {
        "**You:**" => Some((
            Speaker::User,
            Line::styled(
                "❯ You",
                Style::default()
                    .fg(theme::ACCENT)
                    .bg(theme::HIGHLIGHT)
                    .add_modifier(Modifier::BOLD),
            ),
        )),
        "**Assistant:**" => Some((
            Speaker::Assistant,
            Line::styled(
                "● Assistant",
                Style::default()
                    .fg(theme::SECONDARY)
                    .add_modifier(Modifier::BOLD),
            ),
        )),
        _ => None,
    }
}

/// Render message Markdown at its actual content width. User messages get a
/// warm highlight and a narrow left rule; assistant text retains the normal
/// transcript surface. There are no spacer rows between heading and body.
pub(super) fn render(source: &str, width: u16, speaker: Option<Speaker>) -> Vec<Line<'static>> {
    let user = speaker == Some(Speaker::User);
    let rule = user && width > 2;
    let body_width = if rule { width - 2 } else { width };
    markdown::render(source, body_width)
        .into_iter()
        .map(|mut line| {
            if user {
                if rule {
                    line.spans.insert(0, Span::styled("│ ", Style::default().fg(theme::ACCENT)));
                }
                line = line.style(Style::default().bg(theme::HIGHLIGHT));
            }
            line
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn standalone_headings_are_distinct_without_changing_transcript_text() {
        let (user, user_line) = heading("**You:**").unwrap();
        let (assistant, assistant_line) = heading("**Assistant:**").unwrap();
        assert_eq!(user, Speaker::User);
        assert_eq!(assistant, Speaker::Assistant);
        assert_eq!(user_line.to_string(), "❯ You");
        assert_eq!(assistant_line.to_string(), "● Assistant");
        assert_eq!(user_line.style.bg, Some(theme::HIGHLIGHT));
        assert_ne!(user_line.style.fg, assistant_line.style.fg);
        assert!(heading("**You:** more text").is_none());
        assert!(heading("**Assistant:** more text").is_none());
    }

    #[test]
    fn user_markdown_wraps_inside_rule_and_assistant_keeps_its_formatting() {
        let source = "Please check **bold** and `code` in this fairly long line.";
        let user = render(source, 24, Some(Speaker::User));
        let assistant = render(source, 24, Some(Speaker::Assistant));
        assert!(user.len() > 1);
        assert!(user.iter().all(|line| line.to_string().starts_with("│ ")
            && line.width() <= 24 && line.style.bg == Some(theme::HIGHLIGHT)));
        assert!(assistant.iter().all(|line| !line.to_string().starts_with("│ ")
            && line.style.bg.is_none()));
        assert!(user.iter().flat_map(|line| &line.spans).any(|span|
            span.content.contains("bold") && span.style.add_modifier.contains(Modifier::BOLD)));
        assert!(user.iter().flat_map(|line| &line.spans).any(|span|
            span.content.contains("code") && span.style.fg == Some(theme::ACCENT)));
    }

    #[test]
    fn very_narrow_panes_keep_content_in_bounds() {
        for width in 1..=2 {
            let lines = render("text", width, Some(Speaker::User));
            assert!(lines.iter().all(|line| line.width() <= usize::from(width)));
        }
    }
}
