use std::cell::Cell;
use unicode_width::UnicodeWidthStr;

use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    style::{Modifier, Style},
    text::Line,
    widgets::{Block, Borders, Clear, Paragraph, Scrollbar, ScrollbarOrientation, ScrollbarState},
};

use crate::theme;

#[derive(Clone, Copy)]
pub(super) struct Sheet {
    pub(super) area: Rect,
    pub(super) heading: Rect,
    pub(super) body: Rect,
    pub(super) footer: Rect,
}

impl Sheet {
    pub(super) fn new(area: Rect) -> Self {
        Self::with_heading(area, true)
    }

    pub(super) fn with_heading(area: Rect, heading: bool) -> Self {
        let content = Rect::new(
            area.x + 1,
            area.y + 1,
            area.width.saturating_sub(2),
            area.height.saturating_sub(2),
        );
        let [heading, body, footer] = Layout::vertical([
            Constraint::Length(u16::from(heading)),
            Constraint::Fill(1),
            Constraint::Length(1),
        ])
        .areas(content);
        Self {
            area,
            heading,
            body,
            footer,
        }
    }

    pub(super) fn draw(self, frame: &mut Frame<'_>, title: &str) {
        frame.render_widget(Clear, self.area);
        frame.render_widget(
            Block::default()
                .title(format!(" {title} "))
                .borders(Borders::ALL)
                .border_style(theme::modal_border_style())
                .style(Style::default().bg(theme::RAISED)),
            self.area,
        );
    }
}

pub(super) fn line(frame: &mut Frame<'_>, area: Rect, text: &str, style: Style) {
    if !area.is_empty() {
        frame.render_widget(
            Paragraph::new(super::clipped_title(text, usize::from(area.width)).0).style(style),
            area,
        );
    }
}

pub(super) fn scroll_text(
    frame: &mut Frame<'_>,
    area: Rect,
    lines: &[String],
    scroll: &Cell<usize>,
) {
    if area.is_empty() {
        return;
    }
    let width = usize::from(area.width.saturating_sub(1)).max(1);
    let mut wrapped = Vec::new();
    for line in lines {
        let mut row = String::new();
        for word in line.split_whitespace() {
            if !row.is_empty() && row.width() + 1 + word.width() <= width {
                row.push(' ');
                row.push_str(word);
                continue;
            }
            if !row.is_empty() {
                wrapped.push(std::mem::take(&mut row));
            }
            let mut parts = crate::memory_menu::wrap_review(word, width);
            row = parts.pop().unwrap_or_default();
            wrapped.extend(parts);
        }
        wrapped.push(row);
    }
    let visible = usize::from(area.height);
    let start = scroll.get().min(wrapped.len().saturating_sub(visible));
    scroll.set(start);
    frame.render_widget(
        Paragraph::new(
            wrapped
                .iter()
                .skip(start)
                .take(visible)
                .map(|line| Line::from(line.as_str()))
                .collect::<Vec<_>>(),
        )
        .style(Style::default().fg(theme::SECONDARY)),
        area,
    );
    scrollbar(frame, area, wrapped.len(), visible, start);
}

pub(super) fn scrollbar(
    frame: &mut Frame<'_>,
    area: Rect,
    total: usize,
    visible: usize,
    start: usize,
) {
    if area.is_empty() || visible == 0 || visible >= total {
        return;
    }
    let mut state = ScrollbarState::new(total)
        .viewport_content_length(visible)
        .position(start);
    let bar = Scrollbar::new(ScrollbarOrientation::VerticalRight)
        .begin_symbol(None)
        .end_symbol(None)
        .thumb_style(Style::default().fg(theme::ACCENT));
    frame.render_stateful_widget(bar, area, &mut state);
}

pub(super) fn selected_style(selected: bool) -> Style {
    if selected {
        Style::default()
            .fg(theme::TEXT)
            .bg(theme::HIGHLIGHT)
            .add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(theme::SECONDARY)
    }
}

#[derive(Default, Debug)]
pub(super) struct SettingsNavigation {
    pub(super) category_cursor: Option<usize>,
    pub(super) details: bool,
    pub(super) detail_scroll: Cell<usize>,
    pub(super) row_start: Cell<usize>,
    category_start: Cell<usize>,
}

pub(super) fn window(start: &Cell<usize>, selected: usize, total: usize, visible: usize) -> usize {
    let start_at = super::chooser_visible_start(start, selected, visible.max(1))
        .min(total.saturating_sub(visible));
    start.set(start_at);
    start_at
}

pub(super) fn edit_line(frame: &mut Frame<'_>, area: Rect, value: &str) {
    let value = crate::markdown::sanitize(value).replace('\n', " ");
    let text = format!(" {value}▏");
    let offset = text.width().saturating_sub(usize::from(area.width));
    frame.render_widget(
        Paragraph::new(text)
            .scroll((0, offset.min(u16::MAX as usize) as u16))
            .style(selected_style(true)),
        area,
    );
}

pub(super) struct SettingsLayout {
    pub(super) sheet: Sheet,
    pub(super) category_button: Rect,
    pub(super) categories: Rect,
    pub(super) fields: Rect,
    pub(super) wide: bool,
    pub(super) field_header: u16,
}

impl SettingsLayout {
    pub(super) fn new(area: Rect, category_picker: bool) -> Self {
        let sheet = Sheet::new(area);
        let wide = area.width >= 76;
        let category_button = Rect::new(
            sheet.heading.x,
            sheet.heading.y,
            if wide { 18 } else { sheet.heading.width },
            sheet.heading.height,
        );
        let (categories, fields) = if wide {
            let [rail, _, fields] = Layout::horizontal([
                Constraint::Length(17),
                Constraint::Length(1),
                Constraint::Fill(1),
            ])
            .areas(sheet.body);
            (rail, fields)
        } else if category_picker {
            (sheet.body, Rect::default())
        } else {
            (Rect::default(), sheet.body)
        };
        Self {
            sheet,
            category_button,
            categories,
            fields,
            wide,
            field_header: u16::from(fields.height > 1),
        }
    }
}

impl super::App {
    pub(super) fn settings_layout(&self, area: Rect) -> SettingsLayout {
        SettingsLayout::new(area, self.settings_navigation.category_cursor.is_some())
    }

    pub(super) fn settings_categories(&self, area: Rect) -> Vec<(usize, Rect, String)> {
        let Some(menu) = &self.settings_menu else {
            return Vec::new();
        };
        let rail = self.settings_layout(area).categories;
        let labels = crate::settings::CATEGORIES;
        let visible = usize::from(rail.height);
        let selected = self
            .settings_navigation
            .category_cursor
            .unwrap_or(menu.category);
        let start = window(
            &self.settings_navigation.category_start,
            selected,
            labels.len(),
            visible,
        );
        labels
            .iter()
            .enumerate()
            .skip(start)
            .take(visible)
            .enumerate()
            .map(|(offset, (index, label))| {
                let number = (index + 1) % 10;
                (
                    index,
                    Rect::new(rail.x, rail.y + offset as u16, rail.width, 1),
                    format!(" {number} {label}"),
                )
            })
            .collect()
    }

    pub(super) fn settings_fields(&self, area: Rect) -> Vec<(usize, Rect)> {
        let Some(menu) = &self.settings_menu else {
            return Vec::new();
        };
        let layout = self.settings_layout(area);
        let body = layout.fields;
        if body.is_empty() || self.settings_navigation.details {
            return Vec::new();
        }
        let (indices, _, count) = menu.visible_window(area.height);
        let count = count.min(usize::from(body.height.saturating_sub(layout.field_header)));
        let selected = indices
            .iter()
            .position(|index| *index == menu.selected)
            .unwrap_or(0);
        let start = window(
            &self.settings_navigation.row_start,
            selected,
            indices.len(),
            count,
        );
        indices
            .into_iter()
            .skip(start)
            .take(count)
            .enumerate()
            .map(|(offset, index)| {
                (
                    index,
                    Rect::new(
                        body.x,
                        body.y + layout.field_header + offset as u16,
                        body.width.saturating_sub(1),
                        1,
                    ),
                )
            })
            .collect()
    }

    pub(super) fn settings_field_at(
        &self,
        area: Rect,
        point: ratatui::layout::Position,
    ) -> Option<usize> {
        if self.settings_navigation.category_cursor.is_some() {
            return None;
        }
        self.settings_fields(area)
            .into_iter()
            .find_map(|(index, rect)| rect.contains(point).then_some(index))
    }

    pub(super) fn toggle_settings_categories(&mut self) {
        if let Some(menu) = &mut self.settings_menu {
            menu.finish_draft();
            self.mcp_secret = None;
            self.settings_navigation.details = false;
            self.title_tab_hover = None;
            let navigation = &mut self.settings_navigation;
            navigation.category_cursor = if navigation.category_cursor.is_some() {
                None
            } else {
                Some(menu.category)
            };
        }
    }
}
