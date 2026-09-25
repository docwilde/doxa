//! Click geometry for visible HTTP(S) links in rendered transcript lines.
//! Coordinates come from the final Ratatui lines, not raw Markdown offsets.

use ratatui::{style::Modifier, text::Line};
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

/// Extract visible destination cells and their nearby underlined Markdown
/// labels. Long URLs that wrap across terminal rows remain plain text there.
pub(super) fn hits(lines: &[Line<'_>], max_width: usize) -> Vec<LinkHit> {
    let mut result = Vec::new();
    for (row, line) in lines.iter().enumerate() {
        let mut text = String::new();
        let mut underlined = Vec::new();
        let mut col = 0;
        for span in &line.spans {
            let width = UnicodeWidthStr::width(span.content.as_ref());
            if width > 0 && span.style.add_modifier.contains(Modifier::UNDERLINED) {
                underlined.push((col, col + width));
            }
            col += width;
            text.push_str(&span.content);
        }
        for (start, end, url) in urls(&text) {
            let start_col = UnicodeWidthStr::width(&text[..start]);
            let end_col = UnicodeWidthStr::width(&text[..end]);
            if start_col < max_width {
                result.push(LinkHit { row, start: start_col, end: end_col.min(max_width), url: url.clone() });
            }
            // Markdown renders [label](url) as an underlined label followed
            // by ` (url)`. The last contiguous underlined spans share the
            // destination even when emphasis splits the label into spans.
            if let Some(last) = underlined.iter().rposition(|(_, stop)| *stop <= start_col && start_col - *stop <= 3) {
                let mut first = last;
                while first > 0 && underlined[first - 1].1 == underlined[first].0 { first -= 1; }
                for &(label_start, label_end) in &underlined[first..=last] {
                    if label_start < max_width {
                        result.push(LinkHit { row, start: label_start, end: label_end.min(max_width), url: url.clone() });
                    }
                }
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
    fn markdown_label_and_destination_share_a_safe_click_target() {
        let lines = markdown::render("Read [the guide](https://example.com/path) now.", 80);
        let hits = hits(&lines, 80);
        assert!(hits.iter().any(|hit| hit.start <= 5 && hit.end > 5 && hit.url == "https://example.com/path"));
        assert!(hits.iter().any(|hit| hit.start > 10 && hit.url == "https://example.com/path"));
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
