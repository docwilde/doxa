//! Fold tool activity into one expandable section per conversation turn.
//! The transcript remains the source of truth; expansion changes only rendering.

use std::collections::HashSet;

use ratatui::{style::{Modifier, Style}, text::Line};

use crate::{markdown, theme};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct Section {
    pub index: usize,
    pub line: usize,
}

enum Block<'a> {
    Prose(&'a str),
    Tools(Vec<&'a str>),
}

fn is_tool_row(paragraph: &str) -> bool {
    if paragraph.contains('\n') { return false; }
    let Some(row) = paragraph.strip_prefix("Tool: ") else {
        return paragraph.starts_with("[Tool: ") && paragraph.ends_with(']');
    };
    [" started", " finished", " failed"].iter().any(|status| row.contains(status))
}

fn tool_name(row: &str) -> &str {
    let row = row.strip_prefix("Tool: ").or_else(|| row.strip_prefix("[Tool: "))
        .unwrap_or("Tool");
    row.split_once(" started").or_else(|| row.split_once(" finished"))
        .or_else(|| row.split_once(" failed"))
        .map(|(name, _)| name).unwrap_or_else(|| row.trim_end_matches(']'))
}

fn render_turn(blocks: &mut Vec<Block<'_>>, lines: &mut Vec<Line<'static>>,
               sections: &mut Vec<Section>, width: u16,
               expanded: Option<&HashSet<usize>>, selected: Option<usize>) {
    let mut prose = String::new();
    for block in blocks.drain(..) {
        match block {
            Block::Prose(paragraph) => {
                if !prose.is_empty() { prose.push_str("\n\n"); }
                prose.push_str(paragraph);
            }
            Block::Tools(tools) => {
                if !prose.is_empty() {
                    lines.extend(markdown::render(&prose, width));
                    prose.clear();
                }
                let index = sections.len();
                sections.push(Section { index, line: lines.len() });
                let calls = tools.iter().filter(|row| row.contains(" started") || row.starts_with("[Tool: ")).count();
                let count = if calls == 0 { tools.len() } else { calls };
                let open = expanded.is_some_and(|set| set.contains(&index));
                let marker = if open { "▾" } else { "▸" };
                let summary = format!("{marker} {count} tool call{} · {} · Enter",
                    if count == 1 { "" } else { "s" }, tool_name(tools[0]));
                let summary: String = summary.chars().take(usize::from(width.saturating_sub(2))).collect();
                let style = if selected == Some(index) {
                    Style::default().fg(theme::ACCENT).add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(theme::SECONDARY)
                };
                lines.push(Line::styled(format!(" {summary}"), style));
                if open { lines.extend(markdown::render(&tools.join("\n\n"), width)); }
            }
        }
    }
    if !prose.is_empty() { lines.extend(markdown::render(&prose, width)); }
}

pub(super) fn render(
    source: &str,
    width: u16,
    expanded: Option<&HashSet<usize>>,
    selected: Option<usize>,
) -> (Vec<Line<'static>>, Vec<Section>) {
    let mut lines = Vec::new();
    let mut sections = Vec::new();
    let mut blocks = Vec::new();
    let mut fence: Option<&str> = None;
    for paragraph in source.split("\n\n") {
        let paragraph = paragraph.trim_matches('\n');
        if paragraph.is_empty() { continue; }
        if fence.is_none() && paragraph.starts_with("**You:**") {
            render_turn(&mut blocks, &mut lines, &mut sections, width, expanded, selected);
        }
        if fence.is_none() && is_tool_row(paragraph) {
            if let Some(Block::Tools(tools)) = blocks.iter_mut().find(|block| matches!(block, Block::Tools(_))) {
                tools.push(paragraph);
            } else {
                blocks.push(Block::Tools(vec![paragraph]));
            }
        } else {
            blocks.push(Block::Prose(paragraph));
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
    render_turn(&mut blocks, &mut lines, &mut sections, width, expanded, selected);
    (lines, sections)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn shown(lines: &[Line<'_>]) -> String {
        lines.iter().map(ToString::to_string).collect::<Vec<_>>().join("\n")
    }

    #[test]
    fn one_section_per_turn_even_with_interleaved_answer() {
        let transcript = "**You:**\n\nFirst\n\nTool: Read started · first-input\n\nThinking\n\nTool: Read finished · first-result\n\nTool: Write started · second-input\n\n**You:**\n\nSecond\n\nTool: Search started · third-input";
        let (lines, sections) = render(transcript, 80, None, None);
        assert_eq!(sections.len(), 2);
        assert!(shown(&lines).contains("2 tool calls"));
        assert!(!shown(&lines).contains("first-input"));
        assert!(shown(&lines).contains("Thinking"));
        let (lines, _) = render(transcript, 80, Some(&HashSet::from([0])), Some(0));
        let text = shown(&lines);
        assert!(text.contains("first-input") && text.contains("first-result") && text.contains("second-input"));
        assert!(!text.contains("third-input"));
    }

    #[test]
    fn restored_tool_labels_fold_and_expand() {
        let source = "**You:**\n\nQuestion\n\n[Tool: Search]\n\n[Tool: Read]\n\n**Assistant:**\n\nAnswer";
        let (lines, sections) = render(source, 80, None, None);
        assert_eq!(sections.len(), 1);
        assert!(shown(&lines).contains("2 tool calls"));
        assert!(!shown(&lines).contains("[Tool: Search]"));
        let (lines, _) = render(source, 80, Some(&HashSet::from([0])), None);
        assert!(shown(&lines).contains("[Tool: Search]"));
    }

    #[test]
    fn code_fence_tool_text_is_not_folded() {
        let (lines, sections) = render("```\n\nTool: Read started\n\n```", 80, None, None);
        assert!(sections.is_empty());
        assert!(shown(&lines).contains("Tool: Read started"));
    }

    #[test]
    fn plain_tool_label_in_prose_stays_visible() {
        let (lines, sections) = render("Tool: hammer", 80, None, None);
        assert!(sections.is_empty());
        assert!(shown(&lines).contains("Tool: hammer"));
    }
}
