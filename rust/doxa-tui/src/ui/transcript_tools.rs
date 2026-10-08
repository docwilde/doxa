//! Fold tool activity into one expandable section per conversation turn.
//! The transcript locates each section; live scrubbed tool cards supply its
//! expanded details. Expansion changes only rendering.

use std::collections::HashSet;

use ratatui::{style::{Modifier, Style}, text::Line};
use pulldown_cmark::{Event, Parser, Tag, TagEnd};
use unicode_width::UnicodeWidthChar;
use sha2::{Digest, Sha256};

use crate::{markdown, theme};
use super::transcript_roles::{self, Speaker};
use super::tool_cards::ToolCard;

pub(super) const SHELL_PREFIX: &str = "\u{001e}DOXA_LOCAL_SHELL:";
pub(super) const REASONING_PREFIX: &str = "\u{001e}DOXA_REASONING:";
pub(crate) const TOOL_ID_PREFIX: &str = "\u{001f}DOXA_TOOL_ID:";
pub(crate) const RESTORED_TOOL_PREFIX: &str = "\u{001e}DOXA_RESTORED_TOOL:";

#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub(super) enum FoldKey { Section(usize), Tool(String) }

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct Section {
    pub index: FoldKey,
    pub line: usize,
}

pub(super) const IMAGE_ROWS: u16 = 4;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct ImagePlacement {
    pub row: usize,
    pub source: String,
    pub alt: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct MermaidPlacement { pub row: usize, pub key: String }

pub(super) const MAX_MERMAID_SOURCE: usize = 16 * 1024;

pub(super) fn mermaid_key(source: &str) -> String {
    format!("{:x}", Sha256::digest(source.as_bytes()))
}

fn fence_marker(line: &str) -> Option<(u8, usize, &str)> {
    let indent = line.bytes().take_while(|byte| *byte == b' ').count();
    if indent > 3 { return None; }
    let line = &line[indent..];
    let marker = *line.as_bytes().first()?;
    if marker != b'`' && marker != b'~' { return None; }
    let count = line.bytes().take_while(|byte| *byte == marker).count();
    (count >= 3).then_some((marker, count, &line[count..]))
}

fn standalone_mermaid(paragraphs: &[&str], start: usize) -> Option<(usize, String, String)> {
    let first = paragraphs.get(start)?.trim_matches('\n');
    let (marker, count, language) = fence_marker(first.lines().next()?)?;
    if !language.trim().eq_ignore_ascii_case("mermaid") { return None; }
    let mut raw = String::new();
    for (end, paragraph) in paragraphs.iter().enumerate().skip(start) {
        if !raw.is_empty() { raw.push_str("\n\n"); }
        raw.push_str(paragraph.trim_matches('\n'));
        if raw.len() > MAX_MERMAID_SOURCE + 128 { return None; }
        let mut lines = raw.lines();
        lines.next();
        let rest: Vec<&str> = lines.collect();
        let Some(last) = rest.last() else { continue; };
        let closing = |line: &str| fence_marker(line).is_some_and(|(kind, n, tail)|
            kind == marker && n >= count && tail.trim().is_empty());
        if rest[..rest.len() - 1].iter().any(|line| closing(line)) { return None; }
        if !closing(last) { continue; }
        let body = rest[..rest.len() - 1].join("\n");
        if body.trim().is_empty() || body.len() > MAX_MERMAID_SOURCE { return None; }
        return Some((end, raw, body));
    }
    None
}

/// Complete, standalone fences only. The same parser drives scheduling and
/// layout, so an outer code fence never starts an unseen renderer job.
pub(super) fn mermaid_sources(source: &str) -> Vec<String> {
    let paragraphs: Vec<_> = source.split("\n\n").collect();
    let mut found = Vec::new();
    let mut index = 0;
    let mut outer: Option<(u8, usize)> = None;
    while index < paragraphs.len() {
        let paragraph = paragraphs[index].trim_matches('\n');
        if outer.is_none() {
            if let Some((end, _, body)) = standalone_mermaid(&paragraphs, index) {
                found.push(body);
                index = end + 1;
                continue;
            }
        }
        for line in paragraph.lines() {
            if let Some((kind, count, tail)) = fence_marker(line) {
                match outer {
                    Some((open, minimum)) if open == kind && count >= minimum && tail.trim().is_empty() => outer = None,
                    None => outer = Some((kind, count)),
                    _ => {}
                }
            }
        }
        index += 1;
    }
    found
}

enum Block<'a> {
    Prose(&'a str),
    Image { source: String, alt: String },
    Mermaid { raw: String, key: String },
    Shell(crate::shell::Result),
    Heading(&'a str),
    Tools(Vec<&'a str>),
    Reasoning { text: String, tokens: u64, streaming: bool, exact: bool },
}

/// A local file attachment must be the whole Markdown paragraph. Ordinary
/// prose, remote URLs and fenced examples keep the existing Markdown path.
fn standalone_local_image(paragraph: &str) -> Option<(String, String)> {
    let mut events = Parser::new(paragraph);
    if !matches!(events.next()?, Event::Start(Tag::Paragraph)) { return None; }
    let Event::Start(Tag::Image { dest_url, .. }) = events.next()? else { return None; };
    let source = dest_url.to_string();
    if source.len() > 4096 || source.chars().any(char::is_control)
        || !std::path::Path::new(&source).is_absolute() { return None; }
    let mut alt = String::new();
    loop {
        match events.next()? {
            Event::Text(text) | Event::Code(text) => alt.push_str(&text),
            Event::End(TagEnd::Image) => break,
            _ => return None,
        }
        if alt.len() > 512 { return None; }
    }
    if !matches!(events.next()?, Event::End(TagEnd::Paragraph)) || events.next().is_some() {
        return None;
    }
    let alt = markdown::sanitize(&alt);
    Some((source, if alt.trim().is_empty() { "image".into() } else { alt }))
}

fn reasoning_block(paragraph: &str) -> Option<Block<'_>> {
    let data: serde_json::Value = serde_json::from_str(paragraph.strip_prefix(REASONING_PREFIX)?).ok()?;
    Some(Block::Reasoning {
        text: data.get("text")?.as_str()?.to_owned(),
        tokens: data.get("tokens")?.as_u64()?,
        streaming: data.get("streaming")?.as_bool()?,
        exact: data.get("exact").and_then(|value| value.as_bool()).unwrap_or(false),
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

fn display_tool_name(name: &str) -> String {
    name.replace("\\_", "_")
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

fn append_markdown(lines: &mut Vec<Line<'static>>, links: &mut Vec<markdown::LinkRegion>,
                   mut rendered: markdown::RenderedMarkdown) {
    for link in &mut rendered.links { link.row += lines.len(); }
    links.extend(rendered.links);
    lines.extend(rendered.lines);
}

fn render_turn(blocks: &mut Vec<Block<'_>>, lines: &mut Vec<Line<'static>>,
               sections: &mut Vec<Section>, links: &mut Vec<markdown::LinkRegion>,
               images: &mut Vec<ImagePlacement>, mermaids: &mut Vec<MermaidPlacement>,
               ready_mermaids: Option<&HashSet<String>>, image_rows: u16, width: u16,
               expanded: Option<&HashSet<FoldKey>>, selected: Option<FoldKey>, cards: &[ToolCard], section_offset: usize) {
    let mut prose = String::new();
    let mut speaker = None;
    let flush_prose = |prose: &mut String, lines: &mut Vec<Line<'static>>, links: &mut Vec<markdown::LinkRegion>, speaker| {
        if !prose.is_empty() {
            append_markdown(lines, links, transcript_roles::render_with_links(prose, width, speaker));
            prose.clear();
        }
    };
    // Keep fold identities in transcript order while laying each turn's tool
    // activity out after its final response. New prose must not move the tool
    // section back above the response or change which section is expanded.
    let mut next_index = section_offset + sections.iter().filter(|s| matches!(s.index, FoldKey::Section(_))).count();
    let mut ordered = Vec::with_capacity(blocks.len());
    let mut tools = Vec::new();
    for block in blocks.drain(..) {
        let index = if matches!(block, Block::Tools(_) | Block::Reasoning { .. } | Block::Shell(_)) {
            let index = next_index;
            next_index += 1;
            Some(index)
        } else { None };
        if matches!(block, Block::Tools(_)) {
            ordered.push((Block::Heading("**Assistant:**"), None));
            tools.push((block, index));
        } else {
            ordered.push((block, index));
        }
    }
    ordered.extend(tools);
    for (block, section_index) in ordered {
        match block {
            Block::Heading(paragraph) => {
                flush_prose(&mut prose, lines, links, speaker);
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
            Block::Image { source, alt } => {
                flush_prose(&mut prose, lines, links, speaker);
                let label = format!(" Image: {alt}");
                let label = label.chars().take(usize::from(width)).collect::<String>();
                lines.push(Line::styled(label, Style::default().fg(theme::SECONDARY)));
                if image_rows > 0 {
                    images.push(ImagePlacement { row: lines.len(), source, alt });
                    lines.extend((0..image_rows).map(|_| Line::default()));
                }
            }
            Block::Mermaid { raw, key } => {
                if image_rows > 0 && ready_mermaids.is_some_and(|ready| ready.contains(&key)) {
                    flush_prose(&mut prose, lines, links, speaker);
                    lines.push(Line::styled(" Mermaid diagram", Style::default().fg(theme::SECONDARY)));
                    mermaids.push(MermaidPlacement { row: lines.len(), key });
                    lines.extend((0..image_rows).map(|_| Line::default()));
                } else {
                    if !prose.is_empty() { prose.push_str("\n\n"); }
                    prose.push_str(&raw);
                }
            }
            Block::Tools(tools) => {
                flush_prose(&mut prose, lines, links, speaker);
                speaker = Some(Speaker::Assistant);
                let index = section_index.expect("foldable blocks have an identity");
                let index = FoldKey::Section(index);
                sections.push(Section { index: index.clone(), line: lines.len() });
                let calls = tools.iter().filter(|row| {
                    let display = row.split_once(RESTORED_TOOL_PREFIX).map_or(**row, |(display, _)| display);
                    display.contains(" started") || display.starts_with("[Tool: ")
                }).count();
                let count = if calls == 0 { tools.len() } else { calls };
                let open = expanded.is_some_and(|set| set.contains(&index));
                let marker = if open { "▾" } else { "▸" };
                let summary = format!("{marker} {count} tool call{} · {} · Enter",
                    if count == 1 { "" } else { "s" }, display_tool_name(tool_name(tools[0])));
                let summary: String = summary.chars().take(usize::from(width.saturating_sub(2))).collect();
                let style = if selected.as_ref() == Some(&index) {
                    Style::default().fg(theme::ACCENT).add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(theme::SECONDARY)
                };
                lines.push(Line::styled(format!(" {summary}"), style));
                if open {
                    // Group start, summary and streamed detail rows by their persisted
                    // call ID. Opening one call never opens its neighbours.
                    let mut calls: Vec<(String, Vec<&str>)> = Vec::new();
                    for row in tools {
                        let (display, id) = tool_identity(row);
                        let id = id.unwrap_or_else(|| {
                            let name = tool_name(row);
                            if !display.contains(" started") && !display.starts_with("[Tool: ") {
                                if let Some((key, _)) = calls.iter().rev().find(|(_, rows)| tool_name(rows[0]) == name) {
                                    return key.clone();
                                }
                            }
                            format!("legacy:{index:?}:{}", calls.len())
                        });
                        if let Some((_, rows)) = calls.iter_mut().find(|(key, _)| *key == id) {
                            rows.push(row);
                        } else { calls.push((id, vec![row])); }
                    }
                    for (id, rows) in calls {
                        let key = FoldKey::Tool(id.clone());
                        let call_open = expanded.is_some_and(|set| set.contains(&key));
                        sections.push(Section { index: key.clone(), line: lines.len() });
                        let card = cards.iter().find(|card| card.id == id);
                        let name = card.map_or_else(|| tool_name(rows[0]), |card| card.name.as_str());
                        let status = card.map_or_else(|| {
                            let (display, _) = tool_identity(rows.last().copied().unwrap());
                            (if display.contains(" failed") { "failed" }
                            else if display.contains(" finished") { "finished" }
                            else { "running" }).to_owned()
                        }, |card| card.status());
                        let style = if selected.as_ref() == Some(&key) {
                            Style::default().fg(theme::ACCENT).add_modifier(Modifier::BOLD)
                        } else { Style::default().fg(theme::SECONDARY) };
                        lines.push(Line::styled(format!("  {} {} · {status} · Enter",
                            if call_open { "▾" } else { "▸" }, display_tool_name(name)), style));
                        if !call_open { continue; }
                        if let Some(card) = card {
                            if let Some(input) = &card.input { plain_detail(lines, "Input", input, width); }
                            if let Some(result) = &card.result { plain_detail(lines, "Result", result, width); }
                        } else {
                            for row in rows {
                                if let Some((label, detail)) = restored_detail(row) {
                                    plain_detail(lines, label, &detail, width);
                                } else {
                                    let (display, _) = tool_identity(row);
                                    append_markdown(lines, links, markdown::render_with_links(display, width));
                                }
                            }
                        }
                    }
                }
            }
            Block::Shell(result) => {
                flush_prose(&mut prose, lines, links, speaker);
                let index = section_index.expect("shell blocks have an identity");
                let index = FoldKey::Section(index);
                sections.push(Section { index: index.clone(), line: lines.len() });
                let open = expanded.is_some_and(|set| set.contains(&index));
                let marker = if open { "▾" } else { "▸" };
                let style = if selected.as_ref() == Some(&index) { Style::default().fg(theme::ACCENT).add_modifier(Modifier::BOLD) } else { Style::default().fg(theme::SECONDARY) };
                let command = markdown::sanitize(&result.command).replace('\n', " ");
                let label = format!(" {marker} !{command} · {}", result.status);
                let label: String = label.chars().take(usize::from(width.saturating_sub(1))).collect();
                lines.push(Line::styled(label, style));
                if open { plain_detail(lines, "Local output", &result.output, width); }
                if result.dropped_bytes > 0 { lines.push(Line::styled(format!("  {} output bytes omitted", result.dropped_bytes), Style::default().fg(theme::SECONDARY))); }
            }
            Block::Reasoning { text, tokens, streaming, exact } => {
                flush_prose(&mut prose, lines, links, speaker);
                speaker = Some(Speaker::Assistant);
                let index = section_index.expect("foldable blocks have an identity");
                let index = FoldKey::Section(index);
                sections.push(Section { index: index.clone(), line: lines.len() });
                let open = expanded.is_some_and(|set| set.contains(&index));
                let marker = if open { "▾" } else { "▸" };
                let estimate = if exact { "" } else { "~" };
                let label = format!(" {marker} Reasoning/Thinking · {estimate}{tokens} tokens{}",
                    if streaming { " · receiving" } else { "" });
                let style = if selected.as_ref() == Some(&index) {
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
                        append_markdown(lines, links, markdown::render_with_links(&text, width));
                    }
                }
            }
        }
    }
    flush_prose(&mut prose, lines, links, speaker);
}

pub(super) fn render(
    source: &str,
    width: u16,
    expanded: Option<&HashSet<FoldKey>>,
    selected: Option<FoldKey>,
) -> (Vec<Line<'static>>, Vec<Section>) {
    render_with_cards(source, width, expanded, selected, &[])
}

pub(super) fn render_with_cards(
    source: &str,
    width: u16,
    expanded: Option<&HashSet<FoldKey>>,
    selected: Option<FoldKey>,
    cards: &[ToolCard],
) -> (Vec<Line<'static>>, Vec<Section>) {
    let (lines, sections, _) = render_with_links(source, width, expanded, selected.clone(), cards);
    (lines, sections)
}

pub(super) fn render_with_links(
    source: &str, width: u16, expanded: Option<&HashSet<FoldKey>>,
    selected: Option<FoldKey>, cards: &[ToolCard],
) -> (Vec<Line<'static>>, Vec<Section>, Vec<markdown::LinkRegion>) {
    render_with_links_from(source, width, expanded, selected, cards, 0)
}

pub(super) fn render_with_images(
    source: &str, width: u16, expanded: Option<&HashSet<FoldKey>>,
    selected: Option<FoldKey>, cards: &[ToolCard], image_rows: u16, section_offset: usize,
) -> (Vec<Line<'static>>, Vec<Section>, Vec<markdown::LinkRegion>, Vec<ImagePlacement>) {
    let (lines, sections, links, images, _) = render_with_media(
        source, width, expanded, selected, cards, image_rows, section_offset, None);
    (lines, sections, links, images)
}

pub(super) fn render_with_media(
    source: &str, width: u16, expanded: Option<&HashSet<FoldKey>>,
    selected: Option<FoldKey>, cards: &[ToolCard], image_rows: u16, section_offset: usize,
    ready_mermaids: Option<&HashSet<String>>,
) -> (Vec<Line<'static>>, Vec<Section>, Vec<markdown::LinkRegion>, Vec<ImagePlacement>, Vec<MermaidPlacement>) {
    let mut lines = Vec::new();
    let mut sections = Vec::new();
    let mut links = Vec::new();
    let mut images = Vec::new();
    let mut mermaids = Vec::new();
    let mut blocks = Vec::new();
    let mut tool_index: Option<usize> = None;
    let mut fence: Option<(u8, usize)> = None;
    let paragraphs: Vec<_> = source.split("\n\n").collect();
    let mut index = 0;
    while index < paragraphs.len() {
        let paragraph = paragraphs[index].trim_matches('\n');
        if fence.is_none() {
            if let Some((end, raw, body)) = standalone_mermaid(&paragraphs, index) {
                blocks.push(Block::Mermaid { raw, key: mermaid_key(&body) });
                index = end + 1;
                continue;
            }
        }
        index += 1;
        if paragraph.is_empty() { continue; }
        if fence.is_none() && matches!(paragraph, "**You:**" | "**Assistant:**") {
            if paragraph == "**You:**" {
                render_turn(&mut blocks, &mut lines, &mut sections, &mut links, &mut images, &mut mermaids,
                    ready_mermaids, image_rows, width, expanded, selected.clone(), cards, section_offset);
                tool_index = None;
            }
            blocks.push(Block::Heading(paragraph));
        } else if fence.is_none() && paragraph.starts_with(SHELL_PREFIX) {
            render_turn(&mut blocks, &mut lines, &mut sections, &mut links, &mut images, &mut mermaids,
                ready_mermaids, image_rows, width, expanded, selected.clone(), cards, section_offset);
            tool_index = None;
            if let Ok(result) = serde_json::from_str(paragraph.strip_prefix(SHELL_PREFIX).unwrap()) { blocks.push(Block::Shell(result)); }
            else { blocks.push(Block::Prose("Local shell output unavailable")); }
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
            if fence.is_none() {
                if let Some((source, alt)) = standalone_local_image(paragraph) {
                    blocks.push(Block::Image { source, alt });
                    continue;
                }
            }
            blocks.push(Block::Prose(paragraph));
            for line in paragraph.lines() {
                if let Some((kind, count, tail)) = fence_marker(line) {
                    match fence {
                        Some((open, minimum)) if open == kind && count >= minimum && tail.trim().is_empty() => fence = None,
                        None => fence = Some((kind, count)),
                        _ => {}
                    }
                }
            }
        }
    }
    render_turn(&mut blocks, &mut lines, &mut sections, &mut links, &mut images, &mut mermaids,
        ready_mermaids, image_rows, width, expanded, selected.clone(), cards, section_offset);
    (lines, sections, links, images, mermaids)
}

pub(super) fn render_with_links_from(
    source: &str, width: u16, expanded: Option<&HashSet<FoldKey>>,
    selected: Option<FoldKey>, cards: &[ToolCard], section_offset: usize,
) -> (Vec<Line<'static>>, Vec<Section>, Vec<markdown::LinkRegion>) {
    let (lines, sections, links, _) =
        render_with_images(source, width, expanded, selected, cards, 0, section_offset);
    (lines, sections, links)
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::tool_cards::ToolCards;
    use serde_json::json;

    #[test]
    fn mermaid_fence_stays_markdown_without_a_ready_local_render() {
        let source = "**Assistant:**\n\nBefore [link](https://example.com).\n\n```mermaid\ngraph TD\nA-->B\n```\n\nAfter.";
        let (baseline, sections, links, images) = render_with_images(
            source, 40, None, None, &[], IMAGE_ROWS, 0);
        let (fallback, fallback_sections, fallback_links, fallback_images, diagrams) =
            render_with_media(source, 40, None, None, &[], IMAGE_ROWS, 0, Some(&HashSet::new()));
        assert_eq!(fallback, baseline);
        assert_eq!(fallback_sections, sections);
        assert_eq!(fallback_links, links);
        assert_eq!(fallback_images, images);
        assert!(diagrams.is_empty());
        assert!(baseline.iter().any(|line| line.to_string().contains("graph TD")));
    }

    #[test]
    fn ready_mermaid_uses_bounded_rows_and_moves_link_geometry() {
        let source = "**Assistant:**\n\n```mermaid\ngraph TD\n\nA-->B\n```\n\nSee [details](https://example.com).";
        let diagrams = mermaid_sources(source);
        assert_eq!(diagrams, vec!["graph TD\n\nA-->B"]);
        let key = mermaid_key(&diagrams[0]);
        let ready = HashSet::from([key.clone()]);
        let (lines, _, links, _, placements) =
            render_with_media(source, 40, None, None, &[], IMAGE_ROWS, 0, Some(&ready));
        assert_eq!(placements, vec![MermaidPlacement { row: 1, key }]);
        assert!(lines[1..=4].iter().all(|line| line.spans.is_empty()));
        assert_eq!(lines[links[0].row].to_string().trim(), "See details.");
        assert_eq!(links[0].url.as_ref(), "https://example.com");
        let (text, _, _, _, placements) =
            render_with_media(source, 40, None, None, &[], 0, 0, Some(&ready));
        assert!(placements.is_empty());
        assert!(text.iter().any(|line| line.to_string().contains("graph TD")));
    }

    #[test]
    fn mermaid_inside_outer_fence_and_incomplete_fence_are_not_scheduled() {
        let nested = "````md\n```mermaid\ngraph TD\nA-->B\n```\n````";
        assert!(mermaid_sources(nested).is_empty());
        assert!(mermaid_sources("```mermaid\ngraph TD\nA-->B").is_empty());
        assert!(mermaid_sources("Prefix\n```mermaid\ngraph TD\nA-->B\n```").is_empty());
    }

    #[test]
    fn local_attachment_reserves_rows_without_moving_link_targets() {
        let source = "**You:**\n\n![chart](/home/user/chart.png)\n\n**Assistant:**\n\nSee [details](https://example.com).";
        let (lines, _, links, images) =
            render_with_images(source, 34, None, None, &[], IMAGE_ROWS, 0);
        assert_eq!(images, vec![ImagePlacement {
            row: 1, source: "/home/user/chart.png".into(), alt: "chart".into(),
        }]);
        assert!(lines[0].to_string().contains("Image: chart"));
        assert!(lines[1..=4].iter().all(|line| line.spans.is_empty()));
        assert_eq!(links.len(), 1);
        assert_eq!(lines[links[0].row].to_string().trim(), "See details.");
        assert_eq!(links[0].url.as_ref(), "https://example.com");
        let (text_lines, _, _, text_images) =
            render_with_images(source, 34, None, None, &[], 0, 0);
        assert!(text_images.is_empty());
        assert_eq!(text_lines.len() + usize::from(IMAGE_ROWS) - 1, lines.len());
    }

    #[test]
    fn fenced_and_remote_images_keep_the_plain_markdown_path() {
        let source = "```md\n![example](/home/user/example.png)\n```\n\n![remote](https://example.com/a.png)";
        let (lines, _, _, images) =
            render_with_images(source, 40, None, None, &[], IMAGE_ROWS, 0);
        assert!(images.is_empty());
        let displayed = shown(&lines);
        assert!(displayed.contains("![example]"));
        assert!(displayed.contains("remote"));
    }

    fn shown(lines: &[Line<'_>]) -> String {
        lines.iter().map(ToString::to_string).collect::<Vec<_>>().join("\n")
    }

    fn opened(source: &str) -> HashSet<FoldKey> {
        let mut keys = HashSet::from([FoldKey::Section(0)]);
        let (_, sections) = render(source, 80, Some(&keys), None);
        keys.extend(sections.into_iter().map(|section| section.index).filter(|key| matches!(key, FoldKey::Tool(_))));
        keys
    }

    #[test]
    fn repeated_legacy_names_remain_separate_calls() {
        let source = "Tool: Read started · first\n\nTool: Read finished · one\n\nTool: Read started · second\n\nTool: Read finished · two";
        let keys = HashSet::from([FoldKey::Section(0), FoldKey::Tool("legacy:Section(0):0".into())]);
        let (lines, sections) = render(source, 80, Some(&keys), None);
        assert_eq!(sections.len(), 3);
        let text = shown(&lines);
        assert!(text.contains("first") && text.contains("one"));
        assert!(!text.contains("second") && !text.contains(" · two"));
    }

    #[test]
    fn calls_expand_independently_and_follow_ids_across_streams_and_reordering() {
        let mut cards = ToolCards::default();
        for (id, secret) in [("one", "first input"), ("two", "second input")] {
            cards.record("s", "tool_call", &json!({"id":id,"name":"Read","input":secret}));
        }
        let row = |id| format!("Tool: Read started{TOOL_ID_PREFIX}{}", json!(id));
        let source = format!("{}\n\n{}", row("one"), row("two"));
        let keys = HashSet::from([FoldKey::Section(0), FoldKey::Tool("one".into())]);
        let (lines, sections) = render_with_cards(&source, 80, Some(&keys), None, cards.for_session("s"));
        assert_eq!(sections.len(), 3);
        assert!(shown(&lines).contains("first input"));
        assert!(!shown(&lines).contains("second input"));
        cards.record("s", "tool_result_detail", &json!({"id":"one","text":"streamed result"}));
        let reordered = format!("{}\n\n{}", row("two"), row("one"));
        let (lines, _) = render_with_cards(&reordered, 80, Some(&keys), None, cards.for_session("s"));
        assert!(shown(&lines).contains("first input"));
        assert!(shown(&lines).contains("streamed result"));
        assert!(!shown(&lines).contains("second input"));
    }

    #[test]
    fn local_shell_output_has_its_own_fold_and_never_parses_output_as_tools() {
        let result = crate::shell::Result { id: 1, command: "echo output".into(), output: "Tool: Write started\n**Assistant:**\nplain output".into(), status: "exit 0".into(), running: false, dropped_bytes: 7 };
        let source = format!("{SHELL_PREFIX}{}", serde_json::to_string(&result).unwrap());
        let (lines, sections) = render(&source, 80, None, None); assert_eq!(sections.len(), 1);
        assert!(!shown(&lines).contains("plain output")); assert!(shown(&lines).contains("7 output bytes omitted"));
        let (lines, sections) = render(&source, 80, Some(&opened(&source)), None);
        assert_eq!(sections.len(), 1); assert!(shown(&lines).contains("Tool: Write started")); assert!(shown(&lines).contains("plain output"));
    }

    #[test]
    fn one_section_per_turn_even_with_interleaved_answer() {
        let transcript = "**You:**\n\nFirst\n\nTool: Read started · first-input\n\nThinking\n\nTool: Read finished · first-result\n\nTool: Write started · second-input\n\n**You:**\n\nSecond\n\nTool: Search started · third-input";
        let (lines, sections) = render(transcript, 80, None, None);
        assert_eq!(sections.len(), 2);
        assert!(shown(&lines).contains("2 tool calls"));
        assert!(!shown(&lines).contains("first-input"));
        assert!(shown(&lines).contains("Thinking"));
        let (lines, _) = render(transcript, 80, Some(&opened(transcript)), Some(FoldKey::Section(0)));
        let text = shown(&lines);
        assert!(text.contains("first-input") && text.contains("first-result") && text.contains("second-input"));
        assert!(!text.contains("third-input"));
    }

    #[test]
    fn tools_follow_final_response_collapsed_and_expanded() {
        let source = "**You:**\n\nQuestion\n\n**Assistant:**\n\nFirst response\n\nTool: Read started · input\n\nSecond response\n\nTool: Read finished · result\n\nLast response";
        for expanded in [None, Some(HashSet::from([FoldKey::Section(0)]))] {
            let (lines, sections) = render(source, 80, expanded.as_ref(), None);
            let text = shown(&lines);
            assert!(text.find("Last response").unwrap() < text.find("1 tool call").unwrap());
            assert_eq!(sections.len(), if expanded.is_some() { 2 } else { 1 });
            if expanded.is_some() { assert!(text.find("Last response").unwrap() < text.find("Read ·").unwrap()); }
        }
    }

    #[test]
    fn tool_identity_survives_later_reasoning_and_stays_in_its_turn() {
        let reasoning = format!("{REASONING_PREFIX}{}", json!({"text":"hidden thinking", "tokens":10, "streaming":false}));
        let source = format!("**You:**\n\nFirst question\n\nTool: Read started · visible input\n\n{reasoning}\n\n**Assistant:**\n\nFinal answer\n\n**You:**\n\nNext question");
        let (lines, sections) = render(&source, 80, Some(&opened(&source)), None);
        let text = shown(&lines);
        assert!(text.find("Final answer").unwrap() < text.find("1 tool call").unwrap());
        assert!(text.find("1 tool call").unwrap() < text.find("Next question").unwrap());
        assert!(text.contains("visible input"));
        assert!(!text.contains("hidden thinking"));
        assert_eq!(sections.iter().map(|section| section.index.clone()).collect::<Vec<_>>(), vec![FoldKey::Section(1), FoldKey::Section(0), FoldKey::Tool("legacy:Section(0):0".into())]);
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
        let (expanded, _) = render(&source, 80, Some(&opened(&source)), Some(FoldKey::Section(0)));
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
        let (expanded, _) = render_with_cards(&source, 80, Some(&opened(&source)), None, cards.for_session("s"));
        let visible = shown(&expanded);
        assert!(visible.contains("a.rs"));
        assert!(visible.contains("long result"));
        assert!(!visible.contains(TOOL_ID_PREFIX));
        assert!(!visible.contains("finished · short"));
    }

    #[test]
    fn restored_same_name_tools_keep_independent_persisted_ids() {
        let records = [
            json!({"type":"user","message":{"content":"inspect"}}),
            json!({"type":"assistant","message":{"content":[
                {"type":"tool_use","id":"a","name":"Read","input":"first restored"},
                {"type":"tool_use","id":"b","name":"Read","input":"second restored"}]}}),
            json!({"type":"user","message":{"content":[
                {"type":"tool_result","tool_use_id":"a","content":"first result"},
                {"type":"tool_result","tool_use_id":"b","content":"second result"}]}}),
        ];
        let source = crate::history::render(&crate::transport::TranscriptSnapshot {
            bytes: records.iter().map(serde_json::Value::to_string).collect::<Vec<_>>().join("\n").into_bytes(),
            earlier_bytes_omitted: false,
        });
        let keys = HashSet::from([FoldKey::Section(0), FoldKey::Tool("a".into())]);
        let (lines, sections) = render(&source, 80, Some(&keys), None);
        assert_eq!(sections.len(), 3);
        let text = shown(&lines);
        assert!(text.contains("first restored") && text.contains("first result"));
        assert!(!text.contains("second restored") && !text.contains("second result"));
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
        let (expanded, _) = render(&source, 80, Some(&opened(&source)), None);
        let visible = shown(&expanded);
        assert!(visible.contains("src/main.rs"));
        assert!(visible.contains("first line"));
        assert!(visible.contains("second line"));
        assert!(!visible.contains(RESTORED_TOOL_PREFIX));
    }

    #[test]
    fn restored_codex_events_expand_input_and_full_result() {
        let records = [
            json!({"type":"user","message":{"content":"inspect"}}),
            json!({"type":"tool_call","engine":"codex","data":{"id":"c1","name":"Read","input":{"path":"src/main.rs"}}}),
            json!({"type":"tool_result","engine":"codex","data":{"id":"c1","name":"Read","result_summary":"short"}}),
            json!({"type":"tool_result_detail","engine":"codex","data":{"id":"c1","text":"first line\n"}}),
            json!({"type":"tool_result_detail","engine":"codex","data":{"id":"c1","text":"second line"}}),
        ];
        let source = crate::history::render(&crate::transport::TranscriptSnapshot {
            bytes: records.iter().map(serde_json::Value::to_string).collect::<Vec<_>>().join("\n").into_bytes(),
            earlier_bytes_omitted: false,
        });
        let (collapsed, sections) = render(&source, 80, None, None);
        assert_eq!(sections.len(), 1);
        assert!(!shown(&collapsed).contains("second line"));
        let (expanded, _) = render(&source, 80, Some(&opened(&source)), None);
        let visible = shown(&expanded);
        assert!(visible.contains("src/main.rs"));
        assert!(visible.contains("first line"));
        assert!(visible.contains("second line"));
        assert!(!visible.contains("finished · short"));
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
        let (lines, _) = render(source, 80, Some(&opened(&source)), None);
        assert!(shown(&lines).contains("Search · running"));
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
