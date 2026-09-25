//! Fold tool activity into one expandable section per conversation turn.
//! The transcript locates each section; live scrubbed tool cards supply its
//! expanded details. Expansion changes only rendering.

use std::collections::HashSet;

use ratatui::{style::{Modifier, Style}, text::Line};
use unicode_width::UnicodeWidthChar;

use crate::{markdown, theme};
use super::transcript_roles::{self, Speaker};
use super::tool_cards::ToolCard;

pub(super) const REASONING_PREFIX: &str = "\u{001e}DOXA_REASONING:";
pub(super) const TOOL_ID_PREFIX: &str = "\u{001f}DOXA_TOOL_ID:";
pub(crate) const RESTORED_TOOL_PREFIX: &str = "\u{001e}DOXA_RESTORED_TOOL:";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct Section {
    pub index: usize,
    pub line: usize,
}

enum Block<'a> {
    Prose(&'a str),
    Heading(&'a str),
    Tools(Vec<&'a str>),
    Reasoning { text: String, tokens: u64, streaming: bool },
}

fn reasoning_block(paragraph: &str) -> Option<Block<'_>> {
    let data: serde_json::Value = serde_json::from_str(paragraph.strip_prefix(REASONING_PREFIX)?).ok()?;
    Some(Block::Reasoning {
        text: data.get("text")?.as_str()?.to_owned(),
        tokens: data.get("tokens")?.as_u64()?,
        streaming: data.get("streaming")?.as_bool()?,
    })
}

fn is_tool_row(paragraph: &str) -> bool {
    let paragraph = paragraph.split_once(RESTORED_TOOL_PREFIX).map_or(paragraph, |(display, _)| display);
    let paragraph = paragraph.split_once(TOOL_ID_PREFIX).map_or(paragraph, |(display, _)| display);
    if paragraph.contains('\n') { return false; }
    let Some(row) = paragraph.strip_prefix("Tool: ") else {
        return paragraph.starts_with("[Tool: ") && paragraph.ends_with(']');
    };
    [" started", " finished", " failed"].iter().any(|status| row.contains(status))
}

fn tool_name(row: &str) -> &str {
    let row = row.split_once(RESTORED_TOOL_PREFIX).map_or(row, |(display, _)| display);
    let row = row.split_once(TOOL_ID_PREFIX).map_or(row, |(display, _)| display);
    let row = row.strip_prefix("Tool: ").or_else(|| row.strip_prefix("[Tool: "))
        .unwrap_or("Tool");
    row.split_once(" started").or_else(|| row.split_once(" finished"))
        .or_else(|| row.split_once(" failed"))
        .map(|(name, _)| name).unwrap_or_else(|| row.trim_end_matches(']'))
}

fn tool_identity(row: &str) -> (&str, Option<String>) {
    let row = row.split_once(RESTORED_TOOL_PREFIX).map_or(row, |(display, _)| display);
    let Some((display, encoded)) = row.split_once(TOOL_ID_PREFIX) else { return (row, None); };
    (display, serde_json::from_str::<String>(encoded).ok())
}

fn restored_detail(row: &str) -> Option<(&str, String)> {
    let (_, encoded) = row.split_once(RESTORED_TOOL_PREFIX)?;
    let detail: serde_json::Value = serde_json::from_str(encoded).ok()?;
    let label = match detail.get("kind")?.as_str()? {
        "input" => "Input",
        "result" => "Result",
        _ => return None,
    };
    Some((label, detail.get("text")?.as_str()?.to_owned()))
}

fn plain_detail(lines: &mut Vec<Line<'static>>, label: &str, value: &str, width: u16) {
    lines.push(Line::styled(format!("  {label}:"), Style::default().fg(theme::ACCENT)));
    let width = usize::from(width.saturating_sub(2).max(1));
    for source in value.lines() {
        let mut row = String::from("  ");
        let mut used = 0;
        for ch in source.chars() {
            let cells = UnicodeWidthChar::width(ch).unwrap_or(0);
            if used + cells > width && used > 0 {
                lines.push(Line::styled(row, Style::default().fg(theme::SECONDARY)));
                row = String::from("  ");
                used = 0;
            }
            row.push(ch);
            used += cells;
        }
        lines.push(Line::styled(row, Style::default().fg(theme::SECONDARY)));
    }
}

fn render_turn(blocks: &mut Vec<Block<'_>>, lines: &mut Vec<Line<'static>>,
               sections: &mut Vec<Section>, width: u16,
               expanded: Option<&HashSet<usize>>, selected: Option<usize>, cards: &[ToolCard]) {
    let mut prose = String::new();
    let mut speaker = None;
    let flush_prose = |prose: &mut String, lines: &mut Vec<Line<'static>>, speaker| {
        if !prose.is_empty() {
            lines.extend(transcript_roles::render(prose, width, speaker));
            prose.clear();
        }
    };
    for block in blocks.drain(..) {
        match block {
            Block::Heading(paragraph) => {
                flush_prose(&mut prose, lines, speaker);
                if !lines.is_empty() && !lines.last().is_some_and(|line| line.spans.is_empty()) {
                    lines.push(Line::default());
                }
                let next_speaker = transcript_roles::heading(paragraph)
                    .expect("heading blocks contain a recognized role");
                speaker = Some(next_speaker);
            }
            Block::Prose(paragraph) => {
                if !prose.is_empty() { prose.push_str("\n\n"); }
                prose.push_str(paragraph);
            }
            Block::Tools(tools) => {
                flush_prose(&mut prose, lines, speaker);
                speaker = Some(Speaker::Assistant);
                let index = sections.len();
                sections.push(Section { index, line: lines.len() });
                let calls = tools.iter().filter(|row| {
                    let display = row.split_once(RESTORED_TOOL_PREFIX).map_or(**row, |(display, _)| display);
                    display.contains(" started") || display.starts_with("[Tool: ")
                }).count();
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
                if open {
                    let mut seen = HashSet::new();
                    for row in tools {
                        let (display, id) = tool_identity(row);
                        if let Some(card) = id.as_deref().and_then(|id| cards.iter().find(|card| card.id == id)) {
                            if !seen.insert(card.id.as_str()) { continue; }
                            lines.push(Line::styled(format!("  {} · {}", card.name, card.status()),
                                Style::default().fg(theme::ACCENT)));
                            if let Some(input) = &card.input { plain_detail(lines, "Input", input, width); }
                            if let Some(result) = &card.result { plain_detail(lines, "Result", result, width); }
                        } else {
                            if let Some((label, detail)) = restored_detail(row) {
                                let status = display.split_once(" · ").map_or(display, |(status, _)| status);
                                lines.push(Line::styled(format!("  {status}"), Style::default().fg(theme::ACCENT)));
                                plain_detail(lines, label, &detail, width);
                            } else {
                                lines.extend(markdown::render(display, width));
                            }
                        }
                    }
                }
            }
            Block::Reasoning { text, tokens, streaming } => {
                flush_prose(&mut prose, lines, speaker);
                speaker = Some(Speaker::Assistant);
                let index = sections.len();
                sections.push(Section { index, line: lines.len() });
                let open = expanded.is_some_and(|set| set.contains(&index));
                let marker = if open { "▾" } else { "▸" };
                let label = format!(" {marker} Reasoning/Thinking · ~{tokens} tokens{}",
                    if streaming { " · receiving" } else { "" });
                let style = if selected == Some(index) {
                    Style::default().fg(theme::ACCENT).add_modifier(Modifier::BOLD)
                } else { Style::default().fg(theme::SECONDARY) };
                lines.push(Line::styled(label, style));
                if open {
                    if text.is_empty() {
                        lines.push(Line::styled(if streaming {
                            "  Waiting for scrubbed content"
                        } else {
                            "  Reasoning content unavailable"
                        }, Style::default().fg(theme::SECONDARY)));
                    } else {
                        lines.extend(markdown::render(&text, width));
                    }
                }
            }
        }
    }
    flush_prose(&mut prose, lines, speaker);
}

pub(super) fn render(
    source: &str,
    width: u16,
    expanded: Option<&HashSet<usize>>,
    selected: Option<usize>,
) -> (Vec<Line<'static>>, Vec<Section>) {
    render_with_cards(source, width, expanded, selected, &[])
}

pub(super) fn render_with_cards(
    source: &str,
    width: u16,
    expanded: Option<&HashSet<usize>>,
    selected: Option<usize>,
    cards: &[ToolCard],
) -> (Vec<Line<'static>>, Vec<Section>) {
    let mut lines = Vec::new();
    let mut sections = Vec::new();
    let mut blocks = Vec::new();
    let mut tool_index: Option<usize> = None;
    let mut fence: Option<&str> = None;
    for paragraph in source.split("\n\n") {
        let paragraph = paragraph.trim_matches('\n');
        if paragraph.is_empty() { continue; }
        if fence.is_none() && matches!(paragraph, "**You:**" | "**Assistant:**") {
            if paragraph == "**You:**" {
                render_turn(&mut blocks, &mut lines, &mut sections, width, expanded, selected, cards);
                tool_index = None;
            }
            blocks.push(Block::Heading(paragraph));
        } else if fence.is_none() && paragraph.starts_with(REASONING_PREFIX) {
            blocks.push(reasoning_block(paragraph).unwrap_or(Block::Prose("Reasoning unavailable")));
        } else if fence.is_none() && is_tool_row(paragraph) {
            if let Some(index) = tool_index {
                let Block::Tools(tools) = &mut blocks[index] else { unreachable!() };
                tools.push(paragraph);
            } else {
                tool_index = Some(blocks.len());
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
    render_turn(&mut blocks, &mut lines, &mut sections, width, expanded, selected, cards);
    (lines, sections)
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::tool_cards::ToolCards;
    use serde_json::json;

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
    fn reasoning_stays_collapsed_with_a_live_token_count() {
        let marker = format!("{REASONING_PREFIX}{}", serde_json::json!({
            "text":"private reasoning\nwith a second line", "tokens":42, "streaming":true
        }));
        let source = format!("**You:**\n\nQuestion\n\n**Assistant:**\n\n{marker}\n\nAnswer");
        let (lines, sections) = render(&source, 80, None, None);
        assert_eq!(sections.len(), 1);
        assert!(shown(&lines).contains("Reasoning/Thinking · ~42 tokens · receiving"));
        assert!(!shown(&lines).contains("private reasoning"));
        assert!(shown(&lines).contains("Answer"));
        let (expanded, _) = render(&source, 80, Some(&HashSet::from([0])), Some(0));
        assert!(shown(&expanded).contains("private reasoning"));
        assert!(shown(&expanded).contains("with a second line"));
    }

    #[test]
    fn expanded_tool_section_uses_scrubbed_detail_instead_of_short_summary() {
        let mut cards = ToolCards::default();
        cards.record("s", "tool_call", &json!({"id":"call-1","name":"Read","input":{"path":"a.rs"}}));
        cards.record("s", "tool_result", &json!({"id":"call-1","name":"Read","result_summary":"short"}));
        let detail = "long result ".repeat(80);
        cards.record("s", "tool_result_detail", &json!({"id":"call-1","text":detail}));
        let source = format!("**Assistant:**\n\nTool: Read started{TOOL_ID_PREFIX}\"call-1\"\n\nTool: Read finished · short{TOOL_ID_PREFIX}\"call-1\"");
        let (collapsed, sections) = render_with_cards(&source, 80, None, None, cards.for_session("s"));
        assert_eq!(sections.len(), 1);
        assert!(!shown(&collapsed).contains("long result"));
        let (expanded, _) = render_with_cards(&source, 80, Some(&HashSet::from([0])), None, cards.for_session("s"));
        let visible = shown(&expanded);
        assert!(visible.contains("a.rs"));
        assert!(visible.contains("long result"));
        assert!(!visible.contains(TOOL_ID_PREFIX));
        assert!(!visible.contains("finished · short"));
    }

    #[test]
    fn restored_tool_detail_is_hidden_until_expanded_and_keeps_multiline_result() {
        let result = "first line\n".to_owned() + &"second line ".repeat(80);
        let records = format!("{}\n{}\n{}\n",
            serde_json::json!({"type":"user","message":{"content":"question"}}),
            serde_json::json!({"type":"assistant","message":{"content":[{"type":"tool_use","id":"t1","name":"Read","input":{"path":"src/main.rs"}}]}}),
            serde_json::json!({"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":result}]}}));
        let source = crate::history::render(&crate::transport::TranscriptSnapshot {
            bytes: records.into_bytes(), earlier_bytes_omitted: false,
        });
        let (collapsed, sections) = render(&source, 80, None, None);
        assert_eq!(sections.len(), 1);
        assert!(shown(&collapsed).contains("1 tool call"));
        assert!(!shown(&collapsed).contains("second line"));
        let (expanded, _) = render(&source, 80, Some(&HashSet::from([0])), None);
        let visible = shown(&expanded);
        assert!(visible.contains("src/main.rs"));
        assert!(visible.contains("first line"));
        assert!(visible.contains("second line"));
        assert!(!visible.contains(RESTORED_TOOL_PREFIX));
    }

    #[test]
    fn speakers_stay_distinct_around_a_collapsed_tool_section() {
        let source = "**You:**\n\nCheck **this**.\n\n**Assistant:**\n\nWorking.\n\nTool: Read started · hidden-path\n\nDone.";
        let (lines, sections) = render(source, 40, None, None);
        assert_eq!(sections.len(), 1);
        assert!(lines[0].to_string().starts_with("│ Check this."));
        assert!(lines.iter().any(|line| line.to_string().starts_with("│ Check this.")
            && line.style.bg == Some(theme::HIGHLIGHT)));
        assert!(!shown(&lines).contains("● Assistant"));
        assert!(!shown(&lines).contains("❯ You"));
        assert!(shown(&lines).contains("1 tool call"));
        assert!(!shown(&lines).contains("hidden-path"));
        assert!(shown(&lines).contains("Done."));
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
    fn code_fence_role_label_is_not_a_message_heading() {
        let (lines, sections) = render("**Assistant:**\n\n```text\n\n**You:**\n\n```\n\nDone.", 80, None, None);
        assert!(sections.is_empty());
        assert!(!shown(&lines).contains("● Assistant"));
        assert!(!shown(&lines).contains("❯ You"));
        assert!(shown(&lines).contains("**You:**"));
        assert!(shown(&lines).contains("Done."));
    }

    #[test]
    fn plain_tool_label_in_prose_stays_visible() {
        let (lines, sections) = render("Tool: hammer", 80, None, None);
        assert!(sections.is_empty());
        assert!(shown(&lines).contains("Tool: hammer"));
    }
}
