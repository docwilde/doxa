//! Markdown rendering for live and restored assistant turns.
//!
//! Each call reparses the full source. A trailing, incomplete construct may
//! change meaning when the next streamed fragment arrives.

use pulldown_cmark::{Alignment, Event, Options, Parser, Tag, TagEnd};
use ratatui::{style::{Color, Modifier, Style}, text::{Line, Span}};
use unicode_width::UnicodeWidthChar;

#[derive(Default)]
struct Block {
    spans: Vec<Span<'static>>,
    prefix: String,
    continuation: String,
    code: bool,
}

struct List {
    next: Option<u64>,
    marker: String,
    indent: usize,
}

#[derive(Default)]
struct Table {
    alignments: Vec<Alignment>,
    rows: Vec<Vec<Vec<Span<'static>>>>,
    row: Vec<Vec<Span<'static>>>,
    cell: Vec<Span<'static>>,
    in_cell: bool,
    header_rows: usize,
}

struct Renderer {
    lines: Vec<Line<'static>>,
    block: Option<Block>,
    lists: Vec<List>,
    quote_depth: usize,
    style: Style,
    styles: Vec<Style>,
    links: Vec<String>,
    table: Option<Table>,
    width: usize,
}

/// Render CommonMark and GFM tables to styled terminal lines. Link destinations
/// are visible text; this API has no click coordinates or opener. Model supplied
/// control characters are replaced so they cannot become terminal commands.
pub fn render(source: &str, width: u16) -> Vec<Line<'static>> {
    let mut renderer = Renderer {
        lines: Vec::new(), block: None, lists: Vec::new(), quote_depth: 0,
        style: Style::default(), styles: Vec::new(), links: Vec::new(),
        table: None, width: usize::from(width.max(1)),
    };
    let options = Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TASKLISTS;
    for event in Parser::new_ext(source, options) { renderer.event(event); }
    renderer.flush();
    if let Some(table) = renderer.table.take() { renderer.table_lines(table); }
    while renderer.lines.last().is_some_and(|line| line.spans.is_empty()) {
        renderer.lines.pop();
    }
    renderer.lines
}

impl Renderer {
    fn start(&mut self, code: bool) {
        self.flush();
        let mut prefix = "│ ".repeat(self.quote_depth);
        let mut continuation = prefix.clone();
        for list in &self.lists {
            let width = list.indent.max(list.marker.chars().count());
            prefix.push_str(&format!("{:<width$}", list.marker));
            continuation.push_str(&" ".repeat(width));
        }
        self.block = Some(Block { prefix, continuation, code, ..Block::default() });
    }

    fn flush(&mut self) {
        if let Some(block) = self.block.take() {
            if !block.spans.is_empty() { self.wrap(block); }
            if let Some(list) = self.lists.last_mut() { list.marker.clear(); }
        }
    }

    fn push(&mut self, text: &str) {
        let clean = sanitize(text);
        if clean.is_empty() { return; }
        if let Some(table) = self.table.as_mut() {
            if table.in_cell {
                table.cell.push(Span::styled(clean, self.style));
                return;
            }
        }
        if self.block.is_none() { self.start(false); }
        self.block.as_mut().unwrap().spans.push(Span::styled(clean, self.style));
    }

    fn enter(&mut self, style: Style) {
        self.styles.push(self.style);
        self.style = self.style.patch(style);
    }

    fn leave(&mut self) { self.style = self.styles.pop().unwrap_or_default(); }

    fn event(&mut self, event: Event<'_>) {
        match event {
            Event::Start(tag) => match tag {
                Tag::Paragraph => { if self.block.is_none() { self.start(false); } }
                Tag::Heading { .. } => {
                    self.start(false);
                    self.enter(Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD));
                }
                Tag::CodeBlock(_) => {
                    self.start(true);
                    self.enter(Style::default().fg(Color::Yellow));
                }
                Tag::BlockQuote(_) => { self.flush(); self.quote_depth += 1; }
                Tag::List(next) => {
                    self.flush();
                    self.lists.push(List { next, marker: String::new(), indent: 3 });
                }
                Tag::Item => {
                    self.flush();
                    if let Some(list) = self.lists.last_mut() {
                        list.marker = match list.next.as_mut() {
                            Some(number) => {
                                let marker = format!("{number}. ");
                                *number = number.saturating_add(1);
                                marker
                            }
                            None => "•  ".to_owned(),
                        };
                        list.indent = list.indent.max(list.marker.chars().count());
                    }
                }
                Tag::Emphasis => self.enter(Style::default().add_modifier(Modifier::ITALIC)),
                Tag::Strong => self.enter(Style::default().add_modifier(Modifier::BOLD)),
                Tag::Strikethrough => self.enter(Style::default().add_modifier(Modifier::CROSSED_OUT)),
                Tag::Link { dest_url, .. } | Tag::Image { dest_url, .. } => {
                    self.links.push(sanitize(&dest_url).replace('\n', " "));
                    self.enter(Style::default().fg(Color::Blue).add_modifier(Modifier::UNDERLINED));
                }
                Tag::Table(alignments) => {
                    self.flush();
                    self.table = Some(Table { alignments, ..Table::default() });
                }
                Tag::TableRow => { if let Some(table) = self.table.as_mut() { table.row.clear(); } }
                Tag::TableCell => { if let Some(table) = self.table.as_mut() {
                    table.cell.clear(); table.in_cell = true;
                } }
                _ => {}
            },
            Event::End(tag) => match tag {
                TagEnd::Paragraph | TagEnd::Item => self.flush(),
                TagEnd::Heading(_) | TagEnd::CodeBlock => { self.flush(); self.leave(); }
                TagEnd::BlockQuote(_) => {
                    self.flush(); self.quote_depth = self.quote_depth.saturating_sub(1);
                }
                TagEnd::List(_) => { self.flush(); self.lists.pop(); }
                TagEnd::Emphasis | TagEnd::Strong | TagEnd::Strikethrough => self.leave(),
                TagEnd::Link | TagEnd::Image => {
                    self.leave();
                    if let Some(url) = self.links.pop() {
                        if !url.is_empty() { self.push(&format!(" ({url})")); }
                    }
                }
                TagEnd::TableCell => { if let Some(table) = self.table.as_mut() {
                    table.in_cell = false;
                    table.row.push(std::mem::take(&mut table.cell));
                } }
                TagEnd::TableRow => { if let Some(table) = self.table.as_mut() {
                    table.rows.push(std::mem::take(&mut table.row));
                } }
                TagEnd::TableHead => { if let Some(table) = self.table.as_mut() {
                    if !table.row.is_empty() {
                        table.rows.push(std::mem::take(&mut table.row));
                    }
                    table.header_rows = table.rows.len();
                } }
                TagEnd::Table => { if let Some(table) = self.table.take() { self.table_lines(table); } }
                _ => {}
            },
            Event::Text(text) | Event::Html(text) | Event::InlineHtml(text) => self.push(&text),
            Event::Code(text) => {
                self.enter(Style::default().fg(Color::Yellow));
                self.push(&text);
                self.leave();
            }
            Event::SoftBreak => self.push(" "),
            Event::HardBreak => self.push("\n"),
            Event::Rule => {
                self.flush();
                self.lines.push(Line::from("─".repeat(self.width.min(80))));
            }
            Event::TaskListMarker(done) => self.push(if done { "[x] " } else { "[ ] " }),
            Event::FootnoteReference(text) => self.push(&format!("[{text}]")),
            _ => {}
        }
    }

    fn wrap(&mut self, block: Block) {
        let mut output: Vec<Span<'static>> = Vec::new();
        let mut prefix = visible_prefix(&block.prefix, self.width);
        let mut col = 0;
        add_prefix(&mut output, &mut col, prefix);
        for part in block.spans {
            for (index, segment) in part.content.split('\n').enumerate() {
                if index > 0 {
                    self.lines.push(Line::from(std::mem::take(&mut output)));
                    prefix = visible_prefix(&block.continuation, self.width);
                    col = 0;
                    add_prefix(&mut output, &mut col, prefix);
                }
                for word in words(segment) {
                    if !block.code && col > cell_width(prefix) && col + cell_width(word) > self.width {
                        self.lines.push(Line::from(std::mem::take(&mut output)));
                        prefix = visible_prefix(&block.continuation, self.width);
                        col = 0;
                        add_prefix(&mut output, &mut col, prefix);
                    }
                    if col == cell_width(prefix) && word.chars().all(char::is_whitespace) { continue; }
                    for ch in word.chars() {
                        let ch = if UnicodeWidthChar::width(ch).unwrap_or(0) > self.width {
                            '�'
                        } else { ch };
                        let size = UnicodeWidthChar::width(ch).unwrap_or(0);
                        if col > cell_width(prefix) && col + size > self.width {
                            self.lines.push(Line::from(std::mem::take(&mut output)));
                            prefix = visible_prefix(&block.continuation, self.width);
                            col = 0;
                            add_prefix(&mut output, &mut col, prefix);
                        }
                        if let Some(last) = output.last_mut() {
                            if last.style == part.style {
                                last.content.to_mut().push(ch);
                            } else {
                                output.push(Span::styled(ch.to_string(), part.style));
                            }
                        } else {
                            output.push(Span::styled(ch.to_string(), part.style));
                        }
                        col += size;
                    }
                }
            }
        }
        if !output.is_empty() { self.lines.push(Line::from(output)); }
    }

    fn table_lines(&mut self, table: Table) {
        let columns = table.rows.iter().map(Vec::len).max().unwrap_or(0);
        if columns == 0 { return; }
        // A grid needs one cell and four framing cells per column. When the
        // viewport is narrower, use the ordinary bounded line wrapper.
        if columns.saturating_mul(4).saturating_add(1) > self.width {
            for row in table.rows {
                let mut spans = vec![Span::raw("│ ")];
                for (column, cell) in row.into_iter().enumerate() {
                    if column > 0 { spans.push(Span::raw(" │ ")); }
                    spans.extend(cell);
                }
                spans.push(Span::raw(" │"));
                self.wrap(Block { spans, ..Block::default() });
            }
            return;
        }

        let available = self.width - (3 * columns + 1);
        let mut widths = vec![1; columns];
        for row in &table.rows {
            for (column, cell) in row.iter().enumerate() {
                widths[column] = widths[column].max(cell_natural_width(cell, available));
            }
        }
        let total: usize = widths.iter().sum();
        if total > available {
            // Water-fill using a binary searched cap, so a very wide cell
            // cannot cause a loop proportional to its source length.
            let (mut low, mut high) = (1, *widths.iter().max().unwrap());
            while low < high {
                let mid = low + (high - low) / 2;
                if widths.iter().map(|width| (*width).min(mid)).sum::<usize>() >= available {
                    high = mid;
                } else {
                    low = mid + 1;
                }
            }
            widths.iter_mut().for_each(|width| *width = (*width).min(low));
            let mut excess = widths.iter().sum::<usize>() - available;
            for width in widths.iter_mut().rev() {
                let take = excess.min(width.saturating_sub(1));
                *width -= take;
                excess -= take;
                if excess == 0 { break; }
            }
        }

        for (index, row) in table.rows.into_iter().enumerate() {
            let cells: Vec<Vec<Vec<Span<'static>>>> = (0..columns).map(|column| {
                wrap_cell(row.get(column).map(Vec::as_slice).unwrap_or(&[]), widths[column])
            }).collect();
            let height = cells.iter().map(Vec::len).max().unwrap_or(1);
            for line_index in 0..height {
                let mut spans = vec![Span::raw("│ ")];
                for column in 0..columns {
                    if column > 0 { spans.push(Span::raw(" │ ")); }
                    let cell_line = cells[column].get(line_index).map(Vec::as_slice).unwrap_or(&[]);
                    let used = cell_line.iter().map(|span| cell_width(&span.content)).sum::<usize>();
                    let padding = widths[column] - used;
                    let alignment = table.alignments.get(column).copied().unwrap_or(Alignment::Left);
                    let left = match alignment {
                        Alignment::Right => padding,
                        Alignment::Center => padding / 2,
                        _ => 0,
                    };
                    if left > 0 { spans.push(Span::raw(" ".repeat(left))); }
                    spans.extend(cell_line.iter().cloned());
                    if padding > left { spans.push(Span::raw(" ".repeat(padding - left))); }
                }
                spans.push(Span::raw(" │"));
                self.lines.push(Line::from(spans));
            }
            if index + 1 == table.header_rows {
                let mut rule = String::from("├");
                for (column, width) in widths.iter().enumerate() {
                    if column > 0 { rule.push('┼'); }
                    rule.push_str(&"─".repeat(width + 2));
                }
                rule.push('┤');
                self.lines.push(Line::from(rule));
            }
        }
    }
}

fn cell_natural_width(cell: &[Span<'_>], cap: usize) -> usize {
    let (mut line, mut widest) = (0usize, 0usize);
    for span in cell {
        for ch in span.content.chars() {
            if ch == '\n' {
                widest = widest.max(line);
                line = 0;
            } else {
                line = line.saturating_add(UnicodeWidthChar::width(ch).unwrap_or(0)).min(cap);
            }
        }
    }
    widest.max(line).max(1)
}

fn wrap_cell(cell: &[Span<'static>], width: usize) -> Vec<Vec<Span<'static>>> {
    let mut lines: Vec<Vec<Span<'static>>> = vec![Vec::new()];
    let mut used = 0;
    for span in cell {
        for ch in span.content.chars() {
            if ch == '\n' {
                lines.push(Vec::new());
                used = 0;
                continue;
            }
            let ch = if UnicodeWidthChar::width(ch).unwrap_or(0) > width { '�' } else { ch };
            let size = UnicodeWidthChar::width(ch).unwrap_or(0);
            if used > 0 && used + size > width {
                lines.push(Vec::new());
                used = 0;
            }
            let line = lines.last_mut().unwrap();
            if let Some(last) = line.last_mut() {
                if last.style == span.style {
                    last.content.to_mut().push(ch);
                } else {
                    line.push(Span::styled(ch.to_string(), span.style));
                }
            } else {
                line.push(Span::styled(ch.to_string(), span.style));
            }
            used += size;
        }
    }
    lines
}

fn add_prefix(spans: &mut Vec<Span<'static>>, col: &mut usize, prefix: &str) {
    if !prefix.is_empty() {
        spans.push(Span::raw(prefix.to_owned()));
        *col = cell_width(prefix);
    }
}

fn visible_prefix(prefix: &str, width: usize) -> &str {
    if cell_width(prefix) >= width { "" } else { prefix }
}

pub(crate) fn sanitize(text: &str) -> String {
    text.chars().map(|ch| {
        if ch == '\n' { ch }
        else if ch == '\t' { ' ' }
        else if ch.is_control()
            || matches!(ch, '\u{200e}' | '\u{200f}' | '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
        { '�' }
        else { ch }
    }).collect()
}

fn cell_width(text: &str) -> usize {
    text.chars().map(|ch| UnicodeWidthChar::width(ch).unwrap_or(0)).sum()
}

fn words(text: &str) -> Vec<&str> {
    let mut result = Vec::new();
    let mut start = 0;
    let mut previous = None;
    for (index, ch) in text.char_indices() {
        let space = ch.is_whitespace();
        if previous.is_some_and(|value| value != space) {
            result.push(&text[start..index]);
            start = index;
        }
        previous = Some(space);
    }
    if start < text.len() { result.push(&text[start..]); }
    result
}
