//! Click geometry for visible HTTP(S) links in rendered transcript lines.
//! Coordinates come from the final Ratatui lines, not raw Markdown offsets.

use ratatui::text::Line;
use unicode_width::UnicodeWidthStr;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct LinkHit {
    pub row: usize,
    pub start: usize,
    pub end: usize,
    pub url: String,
}

pub(super) fn safe_url(url: &str) -> bool {
    let rest = url.strip_prefix("https://").or_else(|| url.strip_prefix("http://"));
    let Some(rest) = rest else { return false; };
    let authority = rest.split(&['/', '?', '#'][..]).next().unwrap_or("");
    !authority.is_empty() && url.len() <= 2048
        && !url.chars().any(|ch| ch.is_control() || ch.is_whitespace())
}

fn urls(text: &str) -> Vec<(usize, usize, String)> {
    let mut found = Vec::new();
    let mut from = 0;
    while from < text.len() {
        let http = text[from..].find("http://").map(|offset| from + offset);
        let https = text[from..].find("https://").map(|offset| from + offset);
        let Some(start) = http.into_iter().chain(https).min() else { break; };
        let mut end = text.len();
        for (offset, ch) in text[start..].char_indices() {
            if ch.is_whitespace() || matches!(ch, '<' | '>' | '"' | '\'') {
                end = start + offset;
                break;
            }
        }
        while end > start && text[..end].chars().next_back().is_some_and(|ch| matches!(ch, '.' | ',' | ';' | ':' | '!' | '?' | ')' | ']' | '}')) {
            end -= text[..end].chars().next_back().unwrap().len_utf8();
        }
        if end > start {
            let url = &text[start..end];
            if safe_url(url) { found.push((start, end, url.to_owned())); }
        }
        from = end.max(start + 1);
    }
    found
}

/// Bare URLs remain visible text. Explicit Markdown link labels use renderer
/// metadata; styles never imply a destination.
pub(super) fn hits(lines: &[Line<'_>], max_width: usize) -> Vec<LinkHit> {
    let mut result = Vec::new();
    for (row, line) in lines.iter().enumerate() {
        let mut text = String::new();
        for span in &line.spans {
            text.push_str(&span.content);
        }
        for (start, end, url) in urls(&text) {
            let start_col = UnicodeWidthStr::width(&text[..start]);
            let end_col = UnicodeWidthStr::width(&text[..end]);
            if start_col < max_width {
                result.push(LinkHit { row, start: start_col, end: end_col.min(max_width), url: url.clone() });
            }
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::markdown;

    #[test]
    fn markdown_label_metadata_keeps_destination_out_of_painted_text() {
        let rendered = markdown::render_with_links("Read [the guide](https://example.com/path) now.", 80);
        assert_eq!(rendered.lines[0].to_string(), "Read the guide now.");
        assert_eq!(rendered.links[0], markdown::LinkRegion {
            row: 0, start: 5, end: 14, url: "https://example.com/path".into(),
        });
        assert!(hits(&rendered.lines, 80).is_empty());
        assert!(!safe_url("file:///home/user/secret"));
        assert!(!safe_url("javascript:alert(1)"));
    }

    #[test]
    fn bare_url_trims_sentence_punctuation() {
        let lines = markdown::render("Open https://example.org/docs).", 80);
        let hits = hits(&lines, 80);
        assert!(hits.iter().any(|hit| hit.url == "https://example.org/docs"));
    }
}
