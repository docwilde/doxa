//! Fold the structured tool rows in a transcript into expandable runs.
//! The stored transcript stays intact, so history and replay retain details.

use std::collections::HashSet;

use ratatui::{style::{Modifier, Style}, text::Line};

use crate::{markdown, theme};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct Section {
    pub index: usize,
    pub line: usize,
}

pub(super) fn render(
    source: &str,
    width: u16,
    expanded: Option<&HashSet<usize>>,
    selected: Option<usize>,
) -> (Vec<Line<'static>>, Vec<Section>) {
    let mut lines = Vec::new();
    let mut sections = Vec::new();
    let mut prose = String::new();
    let mut tools = Vec::new();

    let flush_prose = |prose: &mut String, lines: &mut Vec<Line<'static>>| {
        if !prose.is_empty() {
            lines.extend(markdown::render(prose, width));
            prose.clear();
        }
    };
    let flush_tools = |tools: &mut Vec<&str>, lines: &mut Vec<Line<'static>>,
                       sections: &mut Vec<Section>| {
        if tools.is_empty() { return; }
        let index = sections.len();
        sections.push(Section { index, line: lines.len() });
        let calls = tools.iter().filter(|row| row.contains(" started")).count();
        let count = if calls == 0 { tools.len() } else { calls };
        let name = tools[0].strip_prefix("Tool: ").unwrap_or("Tool")
            .split_once(" started").or_else(|| tools[0].strip_prefix("Tool: ").unwrap_or("Tool").split_once(" finished"))
            .or_else(|| tools[0].strip_prefix("Tool: ").unwrap_or("Tool").split_once(" failed"))
            .map(|(name, _)| name).unwrap_or("Tool");
        let open = expanded.is_some_and(|set| set.contains(&index));
        let marker = if open { "▾" } else { "▸" };
        let summary = format!("{marker} {count} tool call{} · {name} · Enter", if count == 1 { "" } else { "s" });
        let summary: String = summary.chars().take(usize::from(width.saturating_sub(2))).collect();
        let style = if selected == Some(index) {
            Style::default().fg(theme::ACCENT).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(theme::SECONDARY)
        };
        lines.push(Line::styled(format!(" {summary}"), style));
        if open {
            lines.extend(markdown::render(&tools.join("\n\n"), width));
        }
        tools.clear();
    };

    let mut fence: Option<&str> = None;
    for paragraph in source.split("\n\n") {
        let paragraph = paragraph.trim_matches('\n');
        if paragraph.is_empty() { continue; }
        let structured_tool = paragraph.starts_with("Tool: ")
            && [" started", " finished", " failed"]
                .iter().any(|status| paragraph.contains(status));
        if fence.is_none() && structured_tool && !paragraph.contains('\n') {
            flush_prose(&mut prose, &mut lines);
            tools.push(paragraph);
        } else {
            flush_tools(&mut tools, &mut lines, &mut sections);
            if !prose.is_empty() { prose.push_str("\n\n"); }
            prose.push_str(paragraph);
            for line in paragraph.lines() {
                let line = line.trim_start();
                if line.starts_with("```") {
                    if fence == Some("```") { fence = None; }
                    else if fence.is_none() { fence = Some("```"); }
                } else if line.starts_with("~~~") {
                    if fence == Some("~~~") { fence = None; }
                    else if fence.is_none() { fence = Some("~~~"); }
                }
            }
        }
    }
    flush_tools(&mut tools, &mut lines, &mut sections);
    flush_prose(&mut prose, &mut lines);
    (lines, sections)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hides_tool_details_until_expanded_and_keeps_runs_separate() {
        let transcript = "Intro\n\nTool: Read started · secret path\n\nTool: Read finished · result\n\nAnswer\n\nTool: Write started · other path";
        let (lines, sections) = render(transcript, 80, None, None);
        let text = lines.iter().map(|line| line.to_string()).collect::<Vec<_>>().join("\n");
        assert_eq!(sections.len(), 2);
        assert!(text.contains("1 tool call"));
        assert!(!text.contains("secret path"));
        assert!(!text.contains("other path"));
        let (lines, _) = render(transcript, 80, Some(&HashSet::from([0])), Some(0));
        let text = lines.iter().map(|line| line.to_string()).collect::<Vec<_>>().join("\n");
        assert!(text.contains("secret path"));
        assert!(!text.contains("other path"));
    }

    #[test]
    fn code_fence_tool_text_is_not_folded() {
        let (lines, sections) = render("```\n\nTool: example\n\n```", 80, None, None);
        assert!(sections.is_empty());
        assert!(lines.iter().any(|line| line.to_string().contains("Tool: example")));
    }

    #[test]
    fn plain_tool_label_in_prose_stays_visible() {
        let (lines, sections) = render("Tool: hammer", 80, None, None);
        assert!(sections.is_empty());
        assert!(lines.iter().any(|line| line.to_string().contains("Tool: hammer")));
    }
}
