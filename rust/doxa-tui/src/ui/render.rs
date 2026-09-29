//! Paint the current frontend state without dispatching commands or starting jobs.
use super::{
    belief_review_buttons, chip_hint, chip_text, chooser_list_lines, chooser_row_style,
    chooser_visible_start, clipped_title, input_request_body, launch, links, raw_visual_rows,
    repo_path_label, safe_label, theme, vendor_models, App, ChipHit, Focus, RailRow,
    RenderedTranscript, ENGINE_CHOICES, MAX_RENDERED_TRANSCRIPTS, PERMISSION_CHOICES,
    REVIEW_BODY_RESERVE, SPINNER_FRAMES,
};
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Tabs, Wrap};
use ratatui::Frame;
use unicode_width::UnicodeWidthStr;

impl App {
    pub fn draw(&self, frame: &mut Frame) {
        self.rendered_belief_rows.borrow_mut().clear();
        let area = frame.area();
        self.transcript_selection.borrow_mut().begin_frame();
        if self.link_interaction_blocked() || area.width < 20 || area.height < 5 {
            self.transcript_selection.borrow_mut().clear();
        }
        self.visible_tool_sections.borrow_mut().clear();
        self.visible_links.borrow_mut().clear();
        *self.rendered_chip_hits.borrow_mut() = Some(Vec::new());
        frame.render_widget(
            Block::default().style(Style::default().bg(theme::BASE).fg(theme::TEXT)),
            area,
        );
        if area.width < 20 || area.height < 5 {
            frame.render_widget(Paragraph::new("DOXA · enlarge terminal"), area);
            return;
        }
        let layout = self.layout(area);
        if let Some(rail) = layout.rail {
            self.draw_rail(frame, rail);
        }
        if let Some(panes) = layout.panes {
            if self.diff_pane {
                self.draw_group(frame, panes[self.active_group], self.active_group);
                self.draw_diff_pane(frame, panes[(self.active_group + 1) % panes.len()]);
            } else {
                for (index, pane) in panes.iter().enumerate() {
                    self.draw_group(frame, *pane, index);
                }
            }
        } else {
            self.draw_group(frame, layout.body, self.active_group);
        }
        if self.active_chooser_rect().is_none() && self.active_request_index().is_none()
        {
            let width = area.width.saturating_sub(2).min(74);
            let height = area.height.saturating_sub(2).min(19);
            if width >= 18 && height >= 3 {
                let fallback = Rect::new(
                    area.x + (area.width - width) / 2,
                    area.y + (area.height - height) / 2,
                    width,
                    height,
                );
                if self.settings_menu.is_some()
                    || self.action_menu
                    || self.lore_picker.is_some()
                    || self.repo_picker.is_some()
                    || self.engine_picker
                    || self.new_session.is_some()
                    || self.model_picker.is_some()
                    || self.effort_picker.is_some()
                    || self.permission_picker.is_some()
                {
                    frame.render_widget(Clear, fallback);
                    if self.settings_menu.is_some() {
                        self.draw_settings_menu(frame, fallback);
                    } else if self.action_menu {
                        self.draw_actions(frame, fallback);
                    } else if self.repo_picker.is_some() {
                        self.draw_repo_picker(frame, fallback);
                    } else if self.lore_picker.is_some() {
                        self.draw_lore_picker(frame, fallback);
                    } else {
                        self.draw_chip_picker(frame, fallback);
                    }
                }
            }
        }
        self.draw_tool_cards(frame, area);
        if self.map_modal {
            self.peer_map.render(
                frame,
                area,
                self.groups[self.active_group].active_id().unwrap_or(""),
            );
        }
        self.draw_diff(frame, area);
        self.draw_stop_confirmation(frame, area);
        if self.active_chooser_rect().is_none()
            && self
                .active_request_index()
                .is_some_and(|index| self.input_requests[index].kind == "ask_user")
        {
            self.draw_request(frame, area, false);
        }
        self.draw_chip_tooltip(frame);
        self.draw_link_tooltip(frame);
        if self
            .belief_preview
            .owner()
            .is_some_and(|owner| self.valid_belief_preview_owner(owner))
        {
            self.belief_preview.render(frame, area);
        }
        if self.memory_preview.owner().is_some_and(|owner| self.valid_memory_preview_owner(owner)) {
            self.memory_preview.render_titled(frame,area," Full memory "," Memory preview ");
        }
        self.transcript_selection
            .borrow_mut()
            .finish_paint(frame.buffer_mut());
        if self.preferences.value("background") == "transparent" {
            for cell in &mut frame.buffer_mut().content {
                if matches!(cell.bg, theme::BASE | theme::RAISED | theme::RAIL) {
                    cell.bg = Color::Reset;
                }
            }
        }
    }

    pub(super) fn draw_link_tooltip(&self, frame: &mut Frame) {
        if self.link_interaction_blocked() {
            return;
        }
        let (Some(url), Some((column, row))) = (&self.link_hover, self.link_hover_position) else {
            return;
        };
        // Revalidate against this paint: scrolling/folding may have replaced
        // the row under a stationary mouse since the last motion event.
        if self.link_at(column, row).as_ref() != Some(url) {
            return;
        }
        let screen = frame.area();
        let hint = format!(" {url} · Ctrl-click to open ");
        let width = hint.width().min(usize::from(screen.width)) as u16;
        let x = column
            .min(screen.right().saturating_sub(width))
            .max(screen.x);
        let y = if row > screen.y {
            row - 1
        } else {
            row.saturating_add(1).min(screen.bottom().saturating_sub(1))
        };
        let text = clipped_title(&hint, usize::from(width)).0;
        frame.render_widget(
            Paragraph::new(text).style(Style::default().fg(theme::ACCENT).bg(theme::HIGHLIGHT)),
            Rect::new(x, y, width, 1),
        );
    }

    pub(super) fn draw_chip_tooltip(&self, frame: &mut Frame) {
        if !self.chip_tooltip_visible {
            return;
        }
        let Some(hit) = &self.chip_hover else {
            return;
        };
        if self.chip_info.is_some()
            || self.active_chooser_rect().is_some()
            || self.active_request_index().is_some()
            || self.map_modal
            || self.diff_modal
            || self.tool_modal
            || self.stop_confirmation.is_some()
        {
            return;
        }
        let hint = if hit.kind == "repo" {
            self.repo_detail(hit.group)
                .unwrap_or_else(|| chip_hint(hit.kind).to_owned())
        } else {
            chip_hint(hit.kind).to_owned()
        };
        if hint.is_empty() || hit.rect.y <= hit.pane.y.saturating_add(3) {
            return;
        }
        let width = (hint.width() + 2).min(usize::from(hit.pane.width)) as u16;
        let x = hit.rect.x.min(hit.pane.right().saturating_sub(width));
        let area = Rect::new(x, hit.rect.y - 1, width, 1);
        let text = clipped_title(&format!(" {hint} "), usize::from(width)).0;
        frame.render_widget(
            Paragraph::new(text).style(Style::default().fg(theme::ACCENT).bg(theme::HIGHLIGHT)),
            area,
        );
    }

    pub(super) fn draw_stop_confirmation(&self, frame: &mut Frame, area: Rect) {
        let Some(id) = &self.stop_confirmation else {
            return;
        };
        let width = area.width.saturating_sub(4).min(78);
        let height = area.height.saturating_sub(4).min(12);
        if width < 36 || height < 8 {
            return;
        }
        let modal = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        let lines = vec![
            Line::from(
                clipped_title(
                    &format!(" Session: {}", safe_label(id)),
                    usize::from(width.saturating_sub(2)),
                )
                .0,
            ),
            Line::from(" Daemon shutdown runs; tab and draft stay."),
            Line::from(""),
            Line::from(" Y stop · Esc/N cancel"),
        ];
        frame.render_widget(Clear, modal);
        frame.render_widget(
            Paragraph::new(lines)
                .wrap(Wrap { trim: false })
                .block(
                    Block::default()
                        .title(" Stop active session ")
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::ERROR)),
                )
                .style(Style::default().fg(theme::TEXT).bg(theme::RAISED)),
            modal,
        );
    }

    pub(super) fn draw_chip_picker(&self, frame: &mut Frame, area: Rect) {
        if !self.engine_picker
            && self.new_session.is_none()
            && self.model_picker.is_none()
            && self.effort_picker.is_none()
            && self.permission_picker.is_none()
        {
            return;
        }
        let height = area.height;
        let modal = area;
        let mut lines = Vec::new();
        let title;
        if self.engine_picker {
            title = " New session · choose engine · Enter continue · Esc close ";
            if height >= 10 {
                lines.push(Line::from(" Select an engine for a new session:"));
                lines.push(Line::from(" Model and first prompt follow."));
                lines.push(Line::from(""));
            } else {
                lines.push(Line::from(" Choose engine:"));
            }
            let offset = if height >= 10 { 4 } else { 2 };
            let visible = usize::from(height.saturating_sub(offset + 1)).max(1);
            let start =
                chooser_visible_start(&self.chooser_view_start, self.engine_selected, visible);
            for (index, engine) in ENGINE_CHOICES.iter().enumerate().skip(start).take(visible) {
                lines.push(Line::styled(
                    format!(
                        " {} {}",
                        if index == self.engine_selected {
                            '›'
                        } else {
                            ' '
                        },
                        engine
                    ),
                    chooser_row_style(index == self.engine_selected),
                ));
            }
        } else if let Some(form) = &self.new_session {
            title = " New session · Tab field · Enter continue/start · Esc close ";
            let name = match form.engine {
                launch::Engine::Codex => "codex",
                launch::Engine::Claude => "claude",
                launch::Engine::DeepSeek => "deepseek",
                launch::Engine::Glm => "glm",
                launch::Engine::Fixture => "fixture",
            };
            if height < 8 && form.launch_error.is_some() {
                lines.push(Line::styled(
                    clipped_title(
                        form.launch_error.as_deref().unwrap(),
                        usize::from(area.width.saturating_sub(2)),
                    )
                    .0,
                    Style::default().fg(theme::ERROR),
                ));
            } else {
                lines.push(Line::from(format!(" Engine: {name}")));
            }
            if height >= 8 && form.launch_error.is_some() {
                lines.push(Line::styled(
                    clipped_title(
                        form.launch_error.as_deref().unwrap(),
                        usize::from(area.width.saturating_sub(2)),
                    )
                    .0,
                    Style::default().fg(theme::ERROR),
                ));
                lines.push(Line::from(if form.retry_allowed {
                    " Esc close · /setup checks authentication · Enter retry"
                } else {
                    " Session exists; use doxa-rs attach · Esc close"
                }));
            } else if height >= 8 {
                lines.push(Line::from(if vendor_models(form.engine).is_empty() {
                    " Blank model uses configured engine default."
                } else {
                    " Left/Right choose vendor model and effort."
                }));
                lines.push(Line::from(if vendor_models(form.engine).is_empty() {
                    format!(
                        " Effort preference: {} · new session only",
                        safe_label(form.effort.as_deref().unwrap_or("provider default"))
                    )
                } else {
                    format!(" {}", safe_label(&form.catalog_note))
                }));
            }
            lines.push(Line::styled(
                format!(
                    " {} Model: {}",
                    if form.field == 0 { '›' } else { ' ' },
                    safe_label(&form.model)
                ),
                chooser_row_style(form.field == 0),
            ));
            let prompt_field = if vendor_models(form.engine).is_empty() {
                1
            } else {
                2
            };
            if prompt_field == 2 {
                lines.push(Line::styled(
                    format!(
                        " {} Effort: {}",
                        if form.field == 1 { '›' } else { ' ' },
                        form.effort.as_deref().unwrap_or("unknown")
                    ),
                    chooser_row_style(form.field == 1),
                ));
            }
            lines.push(Line::styled(
                format!(
                    " {} First prompt: {}",
                    if form.field == prompt_field {
                        '›'
                    } else {
                        ' '
                    },
                    safe_label(&form.prompt)
                ),
                chooser_row_style(form.field == prompt_field),
            ));
            lines.push(Line::styled(
                if self.launching {
                    " Starting session… · waiting for launch result"
                } else {
                    " [ Start session ]"
                },
                chooser_row_style(form.field == prompt_field + 1),
            ));
        } else if let Some((id, selected)) = &self.permission_picker {
            title = " Claude permissions · this session · Enter select · Esc close ";
            if height >= 10 {
                lines.push(Line::from(
                    " Changes how Claude handles tool permission requests.",
                ));
                lines.push(Line::from(if self.permission_confirm_dont_ask {
                    " dontAsk silently denies unapproved calls. Enter again to confirm."
                } else {
                    " Current mode marked with ●; dontAsk requires confirmation."
                }));
                lines.push(Line::from(""));
            } else {
                lines.push(Line::from(" Permission mode:"));
            }
            let offset = if height >= 10 { 4 } else { 2 };
            let visible = usize::from(height.saturating_sub(offset + 1)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, *selected, visible);
            for (index, (mode, description)) in PERMISSION_CHOICES
                .iter()
                .enumerate()
                .skip(start)
                .take(visible)
            {
                let current = self
                    .permission_modes
                    .get(id)
                    .is_some_and(|current| current == mode);
                lines.push(Line::styled(
                    format!(
                        " {} {} {} · {}",
                        if index == *selected { '›' } else { ' ' },
                        if current { '●' } else { ' ' },
                        mode,
                        description
                    ),
                    chooser_row_style(index == *selected),
                ));
            }
        } else if let Some(picker) = &self.effort_picker {
            title = " Effort · this session · Enter select · Esc close ";
            let current = self
                .session_efforts
                .get(&picker.session_id)
                .map(String::as_str)
                .unwrap_or("unknown");
            lines.push(Line::from(format!(
                " Current: {current} · {}/{} · idle session required",
                picker.engine, picker.model
            )));
            lines.push(Line::from(""));
            let visible = usize::from(height.saturating_sub(4)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            for (index, level) in picker.levels.iter().enumerate().skip(start).take(visible) {
                lines.push(Line::styled(
                    format!(
                        " {} {}",
                        if index == picker.selected { '›' } else { ' ' },
                        level
                    ),
                    chooser_row_style(index == picker.selected),
                ));
            }
        } else {
            title = " Model · this session · R retry · Enter select · Esc close ";
            let picker = self.model_picker.as_ref().unwrap();
            lines.push(Line::from(format!(" {}", picker.note)));
            lines.push(Line::from(""));
            if picker.catalog_pending {
                lines.push(Line::from(
                    " Catalog probe in progress · refreshes automatically",
                ));
            } else if !picker.loading && picker.models.is_empty() {
                lines.push(Line::from(" No verified models available for this session"));
            }
            let visible = picker.visible_rows(height);
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            for (index, model) in picker.models.iter().enumerate().skip(start).take(visible) {
                lines.push(Line::styled(
                    format!(
                        " {} {}",
                        if index == picker.selected { '›' } else { ' ' },
                        model
                    ),
                    chooser_row_style(index == picker.selected),
                ));
            }
        }
        frame.render_widget(
            Paragraph::new(lines)
                .block(
                    Block::default()
                        .title(title)
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::ACCENT)),
                )
                .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            modal,
        );
    }

    pub(super) fn draw_settings_menu(&self, frame: &mut Frame, area: Rect) {
        let Some(menu) = &self.settings_menu else {
            return;
        };
        let mut lines = vec![
            Line::from(" env > config.toml > default"),
            Line::styled(
                format!(
                    " {} · shift+←/→ categories · {} unsaved",
                    crate::settings::CATEGORIES[menu.category],
                    menu.edits.len() + usize::from(menu.draft.is_some())
                ),
                Style::default().fg(theme::ACCENT),
            ),
        ];
        for index in menu.visible_indices(area.height) {
            let row = &menu.rows[index];
            let draft = menu
                .draft
                .as_ref()
                .filter(|(k, _)| k == row.setting.key)
                .map(|(_, v)| v.as_str());
            let edited = menu.edits.get(row.setting.key);
            let shown = draft
                .or_else(|| edited.and_then(|v| v.as_deref()))
                .unwrap_or(if edited == Some(&None) {
                    "(default after save)"
                } else if row.value.is_empty() {
                    "(unset)"
                } else {
                    &row.value
                });
            lines.push(Line::styled(
                format!(
                    " {} {}: {} ({}){}",
                    if menu.selected == index { '›' } else { ' ' },
                    row.setting.label,
                    safe_label(shown),
                    row.source,
                    if row.shadowed {
                        " · set by env"
                    } else if row.setting.read_only {
                        " · read-only"
                    } else if edited.is_some() {
                        " *"
                    } else {
                        ""
                    }
                ),
                Style::default()
                    .fg(if row.shadowed {
                        theme::MUTED
                    } else if menu.selected == index {
                        theme::TEXT
                    } else {
                        theme::SECONDARY
                    })
                    .bg(if menu.selected == index {
                        theme::HIGHLIGHT
                    } else {
                        theme::RAISED
                    }),
            ));
        }
        if let Some(row) = menu
            .rows
            .get(menu.selected)
            .filter(|r| r.setting.category == crate::settings::CATEGORIES[menu.category])
        {
            lines.push(Line::from(""));
            lines.push(Line::from(format!(" {}", row.setting.help)));
            if !row.setting.choices.is_empty() {
                lines.push(Line::from(format!(
                    " Choices: {}",
                    row.setting
                        .choices
                        .iter()
                        .filter(|s| !s.is_empty())
                        .copied()
                        .collect::<Vec<_>>()
                        .join(" | ")
                )));
            }
            if ["derive_secs", "consult_floor", "graph_context"].contains(&row.setting.key) {
                lines.push(Line::from(" Claude session memory control; Codex/vendor use their engine's snapshot policy."));
            }
            if row.setting.key == "session_budget_usd" && menu.engine != "claude" {
                lines.push(Line::from(
                    " This engine reports no session dollar cost; ceiling cannot be enforced.",
                ));
            }
            if !row.setting.note.is_empty() {
                lines.push(Line::from(format!(" {}", row.setting.note)));
            }
        }
        if crate::settings::CATEGORIES[menu.category] == "Paths" {
            if let Ok(path) = crate::settings::config_path() {
                lines.push(Line::from(format!(
                    " config file: {} (resolved; 0600)",
                    path.display()
                )));
            }
        }
        if crate::settings::CATEGORIES[menu.category] == "About" {
            lines.push(Line::from(format!(
                " DOXA Rust {} · /about build · /update refresh",
                env!("CARGO_PKG_VERSION")
            )));
            if let Some(telemetry) = self.groups[self.active_group]
                .active_id()
                .and_then(|id| self.session_telemetry.get(id))
            {
                if let Some(tier) = &telemetry.subscription_type {
                    lines.push(Line::from(format!(
                        " plan: {} (reported by session)",
                        safe_label(tier)
                    )));
                }
            }
        }
        // Keep the action hint visible even when a setting has a long caveat.
        lines.truncate(usize::from(area.height.saturating_sub(3)));
        lines.push(Line::from(if menu.draft.is_some() {
            " Enter/Ctrl+S save · Esc cancel edit"
        } else {
            " Enter edit/toggle · U unset · Ctrl+S save · Esc discard/close"
        }));
        frame.render_widget(
            Paragraph::new(lines)
                .block(
                    Block::default()
                        .title(" Settings ")
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::ACCENT)),
                )
                .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            area,
        );
    }

    pub(super) fn draw_chip_info(&self, frame: &mut Frame, area: Rect) {
        if let Some(manager) = &self.memory_manager {
            let visible=usize::from(area.height.saturating_sub(4));
            for (offset,(index,entry)) in manager.visible_entries(visible).into_iter().enumerate() {
                self.rendered_belief_rows.borrow_mut().push(crate::belief_preview::Owner {
                    id:index as u64+1,pane:self.active_group,session:Some(manager.owner.0.clone()),cwd:manager.owner.1.clone(),
                    query:manager.scope.into(),offset:0,rect:Rect::new(area.x+1,area.y+2+offset as u16,area.width.saturating_sub(2),1),
                    menu:area,subject:manager.scope.into(),claim:entry,truncated:false,
                });
            }
            manager.draw(frame, area);
            return;
        }
        if let Some(menu) = &self.operations_menu {
            let width = usize::from(area.width.saturating_sub(2));
            let lines = menu
                .lines(width)
                .into_iter()
                .map(|text| {
                    if text.starts_with('›') {
                        let padding = " ".repeat(width.saturating_sub(text.width()));
                        Line::from(format!("{text}{padding}"))
                            .style(Style::default().fg(theme::TEXT).bg(theme::HIGHLIGHT))
                    } else {
                        Line::from(text)
                    }
                })
                .collect::<Vec<_>>();
            frame.render_widget(
                Paragraph::new(lines)
                    .block(
                        Block::default()
                            .title(" DOXA operations ")
                            .borders(Borders::ALL),
                    )
                    .style(Style::default().fg(theme::TEXT).bg(theme::RAISED)),
                area,
            );
            return;
        }
        let Some(info) = &self.chip_info else {
            return;
        };
        if let Some(review) = &self.fleet_review {
            let lines = info
                .lines
                .iter()
                .flat_map(|line| {
                    crate::memory_menu::wrap_review(
                        line,
                        usize::from(area.width.saturating_sub(2)).max(1),
                    )
                })
                .collect::<Vec<_>>();
            let visible = usize::from(area.height.saturating_sub(2));
            let start = info.scroll.min(lines.len().saturating_sub(visible));
            let end = start + visible;
            if start <= review.seen.get() {
                review.seen.set(review.seen.get().max(end));
                review.complete.set(end >= lines.len());
            }
            frame.render_widget(
                Paragraph::new(lines.join("\n"))
                    .scroll((start.min(u16::MAX as usize) as u16, 0))
                    .block(
                        Block::default()
                            .title(" Native fleet launch review ")
                            .borders(Borders::ALL),
                    )
                    .style(Style::default().fg(theme::TEXT).bg(theme::RAISED)),
                area,
            );
            return;
        }
        if info.kind == "fleet" {
            frame.render_widget(
                Paragraph::new(info.lines.join("\n"))
                    .scroll((info.scroll.min(u16::MAX as usize) as u16, 0))
                    .block(
                        Block::default()
                            .title(" Fleet · PgUp/PgDn scroll ")
                            .borders(Borders::ALL),
                    )
                    .style(Style::default().fg(theme::TEXT).bg(theme::RAISED)),
                area,
            );
            return;
        }
        if info.kind == "memory" {
            if let Some(list) = &self.memory_list {
                let current = self.groups[self.active_group].active_id().and_then(|id| {
                    self.session_cwds
                        .get(id)
                        .and_then(|cwd| cwd.to_str())
                        .map(|cwd| (id, cwd))
                });
                let matches = list
                    .owner
                    .as_ref()
                    .is_none_or(|(id, cwd)| current == Some((id.as_str(), cwd.as_str())));
                let width = usize::from(area.width.saturating_sub(2));
                let mut lines = vec![Line::styled(
                    crate::lore_table::memory_header(width),
                    Style::default()
                        .fg(theme::TEXT)
                        .add_modifier(Modifier::BOLD),
                )];
                if matches {
                    let indices = list.indices();
                    let visible = usize::from(area.height.saturating_sub(3));
                    let start = info.scroll.min(indices.len().saturating_sub(visible));
                    for index in indices.iter().skip(start).take(visible) {
                        let fact = &list.facts[*index];
                        lines.push(Line::from(crate::lore_table::memory_row(
                            &fact.scope,
                            &fact.text,
                            fact.source.as_deref().unwrap_or("—"),
                            width,
                        )));
                    }
                    if indices.is_empty() {
                        lines.push(Line::from(crate::lore_table::cell(
                            info.lines
                                .first()
                                .map(String::as_str)
                                .unwrap_or("No matching curated facts"),
                            width,
                        )));
                    }
                } else {
                    lines.push(Line::from("Session changed; reopen memory"));
                }
                frame.render_widget(
                    Paragraph::new(lines)
                        .block(
                            Block::default()
                                .title(" Curated memory · Shift+M manage · Esc close ")
                                .borders(Borders::ALL)
                                .border_style(Style::default().fg(theme::ACCENT)),
                        )
                        .style(Style::default().fg(theme::TEXT).bg(theme::RAISED)),
                    area,
                );
                return;
            }
        }
        if matches!(
            info.kind,
            "memory" | "usage" | "context" | "help" | "sessions" | "about"
        ) {
            let current = self.groups[self.active_group].active_id().and_then(|id| {
                self.session_cwds
                    .get(id)
                    .and_then(|cwd| cwd.to_str())
                    .map(|cwd| (id, cwd))
            });
            let owner_matches = info.owner.as_ref().is_some_and(|(id, cwd)| {
                if info.kind == "memory" {
                    current == Some((id.as_str(), cwd.as_str()))
                } else {
                    self.groups[self.active_group].active_id() == Some(id.as_str())
                }
            });
            let message;
            let source = if info.owner.is_some() && !owner_matches {
                message = vec![format!("Session changed; reopen {}", info.kind)];
                &message
            } else {
                &info.lines
            };
            let visible = usize::from(area.height.saturating_sub(2)).max(1);
            let start = info.scroll.min(source.len().saturating_sub(visible));
            let lines: Vec<String> = source
                .iter()
                .skip(start)
                .take(visible)
                .map(|line| clipped_title(line, usize::from(area.width.saturating_sub(2))).0)
                .collect();
            frame.render_widget(
                Paragraph::new(lines.join("\n"))
                    .block(
                        Block::default()
                            .title(format!(
                                " {} · ↑↓ scroll · Esc close ",
                                if info.kind == "memory" {
                                    "LORE memory"
                                } else {
                                    info.kind
                                }
                            ))
                            .borders(Borders::ALL)
                            .border_style(Style::default().fg(theme::ACCENT)),
                    )
                    .style(Style::default().fg(theme::TEXT).bg(theme::RAISED)),
                area,
            );
            return;
        }
        let title = format!(" {} · Esc close ", safe_label(info.kind));
        let body = format!(" {}\n {}", safe_label(&info.label), chip_hint(info.kind));
        frame.render_widget(
            Paragraph::new(body)
                .wrap(Wrap { trim: false })
                .block(
                    Block::default()
                        .title(title)
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::ACCENT)),
                )
                .style(Style::default().fg(theme::TEXT).bg(theme::RAISED)),
            area,
        );
    }

    pub(super) fn draw_history(&self, frame: &mut Frame, area: Rect) {
        if !self.history_modal {
            return;
        }
        let matches = self.history_matches();
        let mut lines = vec![Line::from(if self.history_resume {
            " Select a session to resume"
        } else {
            " Type search terms in the prompt below"
        })];
        if matches.is_empty() {
            lines.push(Line::from(
                if self.history_pending.is_some() || self.history_query_due.is_some() {
                    " Finding saved transcripts…"
                } else {
                    " No matching sessions"
                },
            ));
        }
        let visible = usize::from(area.height.saturating_sub(3)).max(1);
        for (position, header, label) in self.history_rows(visible) {
            let style = if header && position == self.history_selected {
                Style::default()
                    .fg(theme::ACCENT)
                    .bg(theme::HIGHLIGHT)
                    .add_modifier(Modifier::BOLD)
            } else if header {
                Style::default().fg(theme::SECONDARY)
            } else {
                Style::default().fg(theme::MUTED)
            };
            let label = clipped_title(&label, usize::from(area.width.saturating_sub(2))).0;
            let padded = format!(
                "{label}{}",
                " ".repeat(usize::from(area.width.saturating_sub(2)).saturating_sub(label.width()))
            );
            lines.push(Line::styled(padded, style));
        }
        frame.render_widget(
            Paragraph::new(lines).block(
                Block::default()
                    .title(if self.history_resume {
                        " Resume session · Enter to open · Esc close "
                    } else {
                        " Session history · type to filter "
                    })
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(theme::ACCENT))
                    .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            ),
            area,
        );
    }

    pub(super) fn draw_attach_picker(&self, frame: &mut Frame, area: Rect) {
        let Some(picker) = &self.attach_picker else {
            return;
        };
        let matches = self.attach_matches();
        let mut lines = vec![Line::from(format!(
            " Search: {}",
            safe_label(&picker.query)
        ))];
        if matches.is_empty() {
            lines.push(Line::from(" No matching live sessions"));
        }
        let visible = usize::from(area.height.saturating_sub(3)).max(1);
        let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
        for (position, &index) in matches.iter().enumerate().skip(start).take(visible) {
            let session = &picker.rows[index];
            let title = if session.title.trim().is_empty() {
                "Untitled session"
            } else {
                &session.title
            };
            let label = format!(
                " {} {} · {}",
                if position == picker.selected {
                    '›'
                } else {
                    ' '
                },
                safe_label(title),
                safe_label(&session.id)
            );
            let style = if position == picker.selected {
                Style::default()
                    .fg(theme::ACCENT)
                    .bg(theme::HIGHLIGHT)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(theme::SECONDARY)
            };
            let label = clipped_title(&label, usize::from(area.width.saturating_sub(2))).0;
            let padded = format!(
                "{label}{}",
                " ".repeat(usize::from(area.width.saturating_sub(2)).saturating_sub(label.width()))
            );
            lines.push(Line::styled(padded, style));
        }
        frame.render_widget(
            Paragraph::new(lines).block(
                Block::default()
                    .title(" Attach live session · type to filter ")
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(theme::ACCENT))
                    .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            ),
            area,
        );
    }

    pub(super) fn draw_branch_picker(&self, frame: &mut Frame, area: Rect) {
        let Some(picker) = &self.branch_picker else {
            return;
        };
        let mut lines = vec![Line::from(format!(
            " Current base: {}",
            safe_label(&picker.base)
        ))];
        let visible = usize::from(area.height.saturating_sub(3)).max(1);
        let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
        for (index, branch) in picker.branches.iter().enumerate().skip(start).take(visible) {
            let current = if branch == &picker.base {
                " · current"
            } else {
                ""
            };
            let label = format!(
                " {} {}{}",
                if index == picker.selected { '›' } else { ' ' },
                safe_label(branch),
                current
            );
            let label = clipped_title(&label, usize::from(area.width.saturating_sub(2))).0;
            let padded = format!(
                "{label}{}",
                " ".repeat(usize::from(area.width.saturating_sub(2)).saturating_sub(label.width()))
            );
            let style = if index == picker.selected {
                Style::default()
                    .fg(theme::ACCENT)
                    .bg(theme::HIGHLIGHT)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(theme::SECONDARY)
            };
            lines.push(Line::styled(padded, style));
        }
        frame.render_widget(
            Paragraph::new(lines).block(
                Block::default()
                    .title(" Base branch · Enter select · Esc close ")
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(theme::ACCENT))
                    .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            ),
            area,
        );
    }

    pub(super) fn draw_repo_picker(&self, frame: &mut Frame, area: Rect) {
        let Some(picker) = &self.repo_picker else {
            return;
        };
        let mut lines = vec![Line::from(
            " Select a folder · Enter browse · current opens new tab",
        )];
        let visible = usize::from(area.height.saturating_sub(3)).max(1);
        let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
        let width = usize::from(area.width.saturating_sub(2));
        for (index, path) in picker.paths.iter().enumerate().skip(start).take(visible) {
            let marker = if index == 0 {
                "current"
            } else if picker.current_dir.parent() == Some(path.as_path()) {
                "up"
            } else {
                "folder"
            };
            let label = format!(
                " {} {} · {}",
                if index == picker.selected { '›' } else { ' ' },
                marker,
                safe_label(&repo_path_label(path))
            );
            let label = clipped_title(&label, width).0;
            let padded = format!("{label}{}", " ".repeat(width.saturating_sub(label.width())));
            let style = if index == picker.selected {
                Style::default()
                    .fg(theme::ACCENT)
                    .bg(theme::HIGHLIGHT)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(theme::SECONDARY)
            };
            lines.push(Line::styled(padded, style));
        }
        frame.render_widget(
            Paragraph::new(lines)
                .block(
                    Block::default()
                        .title(" Directory · ↑↓ select · Enter browse/open · Esc close ")
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::ACCENT)),
                )
                .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            area,
        );
    }

    pub(super) fn draw_slash_suggestions(&self, frame: &mut Frame, area: Rect) {
        let matches = self.slash_suggestions();
        if matches.is_empty() {
            return;
        }
        let visible = usize::from(area.height.saturating_sub(2)).max(1);
        let selected = self.slash_selected.min(matches.len() - 1);
        let start = chooser_visible_start(&self.chooser_view_start, selected, visible);
        let rows: Vec<Line> = matches
            .iter()
            .enumerate()
            .skip(start)
            .take(visible)
            .map(|(index, (command, description))| {
                let label = format!(
                    " {} {:<12} {}",
                    if index == selected { '›' } else { ' ' },
                    command,
                    description
                );
                let label = clipped_title(&label, usize::from(area.width.saturating_sub(2))).0;
                let style = if index == selected {
                    Style::default()
                        .fg(theme::ACCENT)
                        .bg(theme::HIGHLIGHT)
                        .add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(theme::SECONDARY)
                };
                Line::styled(label, style)
            })
            .collect();
        frame.render_widget(
            Paragraph::new(rows)
                .block(
                    Block::default()
                        .title(" Commands · ↑/↓ select · Tab complete ")
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::ACCENT)),
                )
                .style(Style::default().bg(theme::RAISED)),
            area,
        );
    }

    pub(super) fn draw_queue_picker(&self, frame: &mut Frame, area: Rect) {
        let Some(picker) = &self.queue_picker else {
            return;
        };
        let mut lines = vec![Line::from(if picker.loading {
            " Refreshing queue…"
        } else if picker.rows.is_empty() {
            " No queued prompts"
        } else if picker.cancelling.is_some() {
            " Cancelling selected prompt…"
        } else {
            " X cancel selected · R refresh · Esc close"
        })];
        let visible = usize::from(area.height.saturating_sub(3)).max(1);
        let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
        for (index, row) in picker.rows.iter().enumerate().skip(start).take(visible) {
            let ambiguous = picker.rows.iter().filter(|item| item.id == row.id).count() > 1;
            let label = format!(
                " {} {} · {}{}",
                if index == picker.selected { '›' } else { ' ' },
                safe_label(&row.id),
                if ambiguous { "[ambiguous ID] " } else { "" },
                safe_label(&row.preview)
            );
            let label = clipped_title(&label, usize::from(area.width.saturating_sub(2))).0;
            let padded = format!(
                "{label}{}",
                " ".repeat(usize::from(area.width.saturating_sub(2)).saturating_sub(label.width()))
            );
            let style = if index == picker.selected {
                Style::default()
                    .fg(theme::ACCENT)
                    .bg(theme::HIGHLIGHT)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(theme::SECONDARY)
            };
            lines.push(Line::styled(padded, style));
        }
        frame.render_widget(
            Paragraph::new(lines).block(
                Block::default()
                    .title(" Prompt queue · stable IDs ")
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(theme::ACCENT))
                    .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            ),
            area,
        );
    }

    pub(super) fn draw_lore_picker(&self, frame: &mut Frame, area: Rect) {
        let Some(picker) = &self.lore_picker else {
            return;
        };
        if picker.proposal_mode {
            let label_width = usize::from(area.width.saturating_sub(3));
            let mut lines = vec![Line::from(format!(
                " {}",
                clipped_title(&picker.status, label_width).0
            ))];
            if let Some(review) = &picker.review {
                lines.push(Line::from(format!(
                    " {}",
                    clipped_title(
                        &format!("{} · inode {}", review.pid(), review.inode()),
                        label_width
                    )
                    .0
                )));
                lines.push(Line::from(format!(" SHA-256 {}", review.sha256())));
                lines.push(Line::from(
                    clipped_title(
                        if picker.can_resolve {
                            " Raw proposal · ↓/PgDn read all · A approve · R reject · Esc back"
                        } else {
                            " Raw proposal · read only with this LORE version · Esc back"
                        },
                        label_width,
                    )
                    .0,
                ));
                let visible = usize::from(area.height.saturating_sub(REVIEW_BODY_RESERVE));
                let width = usize::from(area.width.saturating_sub(3)).max(1);
                // Preserve all raw content across visual rows; terminal controls
                // are shown with visible escapes, and no field is summarized.
                let visual_rows = raw_visual_rows(review.raw(), width);
                for line in visual_rows.iter().skip(picker.review_scroll).take(visible) {
                    lines.push(Line::from(line.clone()));
                }
            } else {
                lines.push(Line::from(format!(
                    " Page offset {} · {} rows",
                    picker.offset,
                    picker.proposals.len()
                )));
                let visible = usize::from(area.height.saturating_sub(5)).max(1);
                let start =
                    chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
                for (index, row) in picker
                    .proposals
                    .iter()
                    .enumerate()
                    .skip(start)
                    .take(visible)
                {
                    let label = format!(
                        " {} {} · {}/{} · {} · {}",
                        if index == picker.selected { '›' } else { ' ' },
                        safe_label(&row.pid),
                        safe_label(&row.kind),
                        safe_label(&row.action),
                        safe_label(&row.scope),
                        safe_label(&row.summary)
                    );
                    lines.push(Line::styled(
                        label,
                        chooser_row_style(index == picker.selected),
                    ));
                }
            }
            if picker.review.is_none() {
                lines = chooser_list_lines(lines, usize::from(area.width.saturating_sub(2)));
            }
            frame.render_widget(Paragraph::new(lines)
                .block(Block::default().title(lore_view_title(picker))
                    .borders(Borders::ALL).border_style(Style::default().fg(theme::ACCENT)))
                .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)).wrap(Wrap { trim: false }), area);
            return;
        }
        if let Some(review) = &picker.belief_review {
            let width = usize::from(area.width.saturating_sub(3)).max(1);
            let mut lines = vec![Line::from(format!(
                " {}",
                clipped_title(&picker.status, width).0
            ))];
            lines.push(Line::from(format!(
                " Exact belief #{} · complete LORE review",
                review.id()
            )));
            lines.push(Line::from(clipped_title(if picker.can_act_on_beliefs {
                " ↓/PgDn read all · Shift+A accept · Shift+R reject · C/X/S/R detailed note · Esc back"
            } else { " Read only with this LORE version · Esc back" }, width).0));
            if let Some(action) = picker.belief_action {
                let label = match action {
                    doxa_lore::BeliefAction::Confirmed => "accept (confirmed)",
                    doxa_lore::BeliefAction::Contradicted => "contradicted",
                    doxa_lore::BeliefAction::Stale => "stale",
                    doxa_lore::BeliefAction::Retract => "reject (retract)",
                };
                lines.push(Line::from(format!(
                    " {label} note: {}",
                    safe_label(&picker.belief_note)
                )));
                lines.push(Line::from(""));
            } else {
                lines.push(Line::from(match picker.belief_intent {
                    Some(doxa_lore::BeliefAction::Confirmed) => " Accept selected: read all, then Shift+A or click Accept to confirm",
                    Some(doxa_lore::BeliefAction::Retract) => " Reject selected: read all, then Shift+R or click Reject to retract",
                    _ => " Accept records confirmation; Reject retracts active belief, keeping history",
                }));
                lines.push(Line::from(""));
            }
            let full = format!("Subject: {}\nClaim: {}", review.subject(), review.claim());
            let visual_rows = raw_visual_rows(&full, width);
            let visible = usize::from(area.height.saturating_sub(REVIEW_BODY_RESERVE));
            for line in visual_rows.iter().skip(picker.review_scroll).take(visible) {
                lines.push(Line::from(line.clone()));
            }
            lines = chooser_list_lines(lines, usize::from(area.width.saturating_sub(2)));
            frame.render_widget(
                Paragraph::new(lines)
                    .block(
                        Block::default()
                            .title(" LORE belief review ")
                            .borders(Borders::ALL)
                            .border_style(Style::default().fg(theme::ACCENT)),
                    )
                    .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED))
                    .wrap(Wrap { trim: false }),
                area,
            );
            for (rect, label, _) in belief_review_buttons(area, picker) {
                frame.render_widget(
                    Paragraph::new(label)
                        .style(chooser_row_style(self.belief_button_hover == Some(rect))),
                    rect,
                );
            }
            return;
        }
        if let Some((id, graph)) = self.belief_graph_lines.as_ref().filter(|(id, _)| {
            picker
                .rows
                .get(picker.selected)
                .is_some_and(|r| r.id == *id)
        }) {
            let lines = std::iter::once(Line::from(format!(" Belief {id} · g/Esc back")))
                .chain(
                    graph
                        .iter()
                        .skip(self.belief_graph_scroll)
                        .take(usize::from(area.height.saturating_sub(3)))
                        .map(|line| Line::from(safe_label(line))),
                )
                .collect::<Vec<_>>();
            frame.render_widget(
                Paragraph::new(lines)
                    .wrap(Wrap { trim: false })
                    .block(
                        Block::default()
                            .title(" LORE graph neighbourhood ")
                            .borders(Borders::ALL)
                            .border_style(Style::default().fg(theme::ACCENT)),
                    )
                    .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
                area,
            );
            return;
        }
        let width = usize::from(area.width.saturating_sub(2));
        let mut lines = Vec::new();
        if let Some((id, evidence)) = &picker.evidence {
            lines.push(Line::from(format!(
                " Belief #{id} · {} evidence rows",
                evidence.len()
            )));
            for row in evidence
                .iter()
                .take(usize::from(area.height.saturating_sub(4) / 2))
            {
                lines.push(Line::from(format!(
                    " {} · {} · {}{}",
                    safe_label(&row.created),
                    safe_label(&row.project),
                    safe_label(&row.session_id),
                    row.source_engine
                        .as_ref()
                        .map(|engine| format!(" · {}", safe_label(engine)))
                        .unwrap_or_default()
                )));
                lines.push(Line::from(format!(
                    " {}{}",
                    safe_label(&row.note),
                    if row.truncated { "…" } else { "" }
                )));
            }
            if evidence.last().is_some_and(|row| row.trail_truncated) {
                lines.push(Line::from(" More evidence exists in LORE"));
            }
        } else {
            let columns = crate::lore_table::BeliefColumns::new(width);
            lines.push(Line::styled(
                columns.header(),
                Style::default()
                    .fg(theme::TEXT)
                    .add_modifier(Modifier::BOLD),
            ));
            let visible = usize::from(area.height.saturating_sub(3)).max(1);
            let start = chooser_visible_start(&self.chooser_view_start, picker.selected, visible);
            for (index, row) in picker.rows.iter().enumerate().skip(start).take(visible) {
                self.rendered_belief_rows
                    .borrow_mut()
                    .push(crate::belief_preview::Owner {
                        id: row.id,
                        pane: self.active_group,
                        session: self.groups[self.active_group]
                            .active_id()
                            .map(str::to_owned),
                        cwd: picker.cwd.clone(),
                        query: picker.query.clone(),
                        offset: picker.offset,
                        rect: Rect::new(
                            area.x + 1,
                            area.y + 2 + (index - start) as u16,
                            area.width.saturating_sub(2),
                            1,
                        ),
                        menu: area,
                        subject: row.subject.clone(),
                        claim: row.claim.clone(),
                        truncated: row.truncated,
                    });
                lines.push(Line::styled(
                    columns.belief(
                        row.id,
                        &row.subject,
                        &row.claim,
                        row.confidence,
                        row.evidence_count,
                        row.recency.as_deref(),
                    ),
                    chooser_row_style(index == picker.selected),
                ));
            }
            if picker.rows.is_empty() {
                lines.push(Line::from(safe_label(&picker.status)));
            }
        }
        frame.render_widget(
            Paragraph::new(lines)
                .block(
                    Block::default()
                        .title(lore_view_title(picker))
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::ACCENT)),
                )
                .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            area,
        );
    }

    pub(super) fn draw_diff(&self, frame: &mut Frame, area: Rect) {
        if !self.diff_modal {
            return;
        }
        let width = area.width.saturating_sub(4).min(120);
        let height = area.height.saturating_sub(4).min(36);
        if width < 24 || height < 8 {
            return;
        }
        let modal = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        frame.render_widget(Clear, modal);
        let queued_rows = self.queued_diff_rows();
        let mut rows: Vec<Line> = self
            .diff_text
            .lines()
            .enumerate()
            .skip(self.diff_scroll)
            .take(usize::from(height.saturating_sub(
                if self.diff_reject_confirm.is_some() {
                    3
                } else {
                    2
                },
            )))
            .map(|(row, line)| {
                if queued_rows.contains(&row) {
                    return Line::styled(
                        format!("⏳ {line}"),
                        Style::default()
                            .fg(theme::ACCENT)
                            .add_modifier(Modifier::BOLD),
                    );
                }
                let color = if line.starts_with('+') && !line.starts_with("+++") {
                    theme::SUCCESS
                } else if line.starts_with('-') && !line.starts_with("---") {
                    theme::ERROR
                } else if line.starts_with("@@") {
                    theme::ACCENT
                } else {
                    theme::SECONDARY
                };
                Line::styled(line.to_owned(), Style::default().fg(color))
            })
            .collect();
        if let Some(draft) = &self.diff_reject_confirm {
            rows.push(Line::styled(
                format!(
                    " Reason (optional): {}_ · Enter confirm · Esc cancel",
                    draft.reason
                ),
                Style::default().fg(theme::ACCENT),
            ));
        }
        let pending = self.rejections_for_target();
        let title = format!(
            " Worktree diff{} · N/P files · J/K hunks · X reject · R refresh · F2/Esc close ",
            if pending == 0 {
                String::new()
            } else {
                format!(" · {pending} queued")
            }
        );
        frame.render_widget(
            Paragraph::new(rows).block(
                Block::default()
                    .title(title)
                    .borders(Borders::ALL)
                    .border_style(Style::default().fg(theme::BORDER))
                    .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            ),
            modal,
        );
    }

    pub(super) fn draw_diff_pane(&self, frame: &mut Frame, area: Rect) {
        let queued_rows = self.queued_diff_rows();
        let mut rows: Vec<Line> = self
            .diff_text
            .lines()
            .enumerate()
            .skip(self.diff_scroll)
            .take(usize::from(area.height.saturating_sub(
                if self.diff_reject_confirm.is_some() {
                    3
                } else {
                    2
                },
            )))
            .map(|(row, line)| {
                if queued_rows.contains(&row) {
                    return Line::styled(
                        format!("⏳ {line}"),
                        Style::default()
                            .fg(theme::ACCENT)
                            .add_modifier(Modifier::BOLD),
                    );
                }
                let color = if line.starts_with('+') && !line.starts_with("+++") {
                    theme::SUCCESS
                } else if line.starts_with('-') && !line.starts_with("---") {
                    theme::ERROR
                } else if line.starts_with("@@") {
                    theme::ACCENT
                } else {
                    theme::SECONDARY
                };
                Line::styled(line.to_owned(), Style::default().fg(color))
            })
            .collect();
        if let Some(draft) = &self.diff_reject_confirm {
            rows.push(Line::styled(
                format!(
                    " Reason (optional): {}_ · Enter confirm · Esc cancel",
                    draft.reason
                ),
                Style::default().fg(theme::ACCENT),
            ));
        }
        let pending = self.rejections_for_target();
        let title = format!(" Worktree diff{} · Alt+N/B files · Alt+J/K hunks · Alt+R reject · F5 refresh · F4 close ",
            if pending == 0 { String::new() } else { format!(" · {pending} queued") });
        frame.render_widget(
            Paragraph::new(rows)
                .block(
                    Block::default()
                        .title(title)
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::BORDER)),
                )
                .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            area,
        );
    }

    pub(super) fn draw_actions(&self, frame: &mut Frame, area: Rect) {
        if !self.action_menu {
            return;
        }
        let rows = self.action_rows();
        let visible = usize::from(area.height.saturating_sub(3)).max(1);
        let start = chooser_visible_start(&self.chooser_view_start, self.action_selected, visible);
        let mut lines = vec![Line::from(format!(
            " Filter: {}",
            safe_label(&self.action_query)
        ))];
        if rows.is_empty() {
            lines.push(Line::from(" No matching actions"));
        }
        for (index, row) in rows.iter().enumerate().skip(start).take(visible) {
            let label = clipped_title(
                &format!(
                    " {} {} · {}",
                    if index == self.action_selected {
                        '›'
                    } else {
                        ' '
                    },
                    row.label,
                    row.help
                ),
                usize::from(area.width.saturating_sub(2)),
            )
            .0;
            lines.push(Line::styled(
                label,
                if index == self.action_selected {
                    Style::default()
                        .fg(theme::ACCENT)
                        .bg(theme::HIGHLIGHT)
                        .add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(theme::SECONDARY)
                },
            ));
        }
        frame.render_widget(
            Paragraph::new(lines)
                .block(
                    Block::default()
                        .title(" Actions · type filter · Enter select · Esc close ")
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::ACCENT)),
                )
                .style(Style::default().bg(theme::RAISED)),
            area,
        );
    }

    pub(super) fn draw_tool_cards(&self, frame: &mut Frame, area: Rect) {
        if !self.tool_modal {
            return;
        }
        let width = area.width.saturating_sub(4).min(100);
        let height = area.height.saturating_sub(4).min(28);
        if width < 24 || height < 7 {
            return;
        }
        let modal = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        let cards = self.active_tool_cards();
        let mut body = String::new();
        if cards.is_empty() {
            body.push_str("No tool activity in this session.");
        } else {
            let selected = self.tool_selected.min(cards.len() - 1);
            let start = selected
                .saturating_sub(7)
                .min(cards.len().saturating_sub(8));
            for (index, card) in cards.iter().enumerate().skip(start).take(8) {
                body.push_str(if index == selected { "▸ " } else { "  " });
                body.push_str(&format!("{} · {}\n", card.name, card.status()));
            }
            let card = &cards[selected];
            body.push_str("\nTool: ");
            body.push_str(&card.name);
            if let Some(parent) = &card.parent_id {
                body.push_str("\nParent: ");
                body.push_str(parent);
            }
            body.push_str("\nStatus: ");
            body.push_str(&card.status());
            body.push_str("\n\nInput:\n");
            body.push_str(card.input.as_deref().unwrap_or("(unavailable)"));
            body.push_str("\n\nResult:\n");
            body.push_str(card.result.as_deref().unwrap_or("(pending)"));
        }
        frame.render_widget(Clear, modal);
        frame.render_widget(
            Paragraph::new(body)
                .wrap(Wrap { trim: false })
                .scroll((self.tool_scroll, 0))
                .block(
                    Block::default()
                        .title(" Tool activity · ↑/↓ select · PgUp/PgDn scroll · Esc close ")
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::BORDER))
                        .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
                ),
            modal,
        );
    }

    pub(super) fn draw_request(&self, frame: &mut Frame, area: Rect, inline: bool) {
        let Some(index) = self.active_request_index() else {
            return;
        };
        let request = &self.input_requests[index];
        let width = if inline {
            area.width
        } else {
            area.width.saturating_sub(4).min(90)
        };
        let height = if inline {
            area.height
        } else {
            area.height.saturating_sub(4).min(22)
        };
        if width < 20 || height < 5 {
            return;
        }
        let modal = if inline {
            area
        } else {
            Rect::new(
                area.x + (area.width - width) / 2,
                area.y + (area.height - height) / 2,
                width,
                height,
            )
        };
        let (body, selected_row, _) =
            input_request_body(request, usize::from(modal.width.saturating_sub(4)));
        let question = request.questions.get(request.step);
        let reviewed_body;
        let mut scroll = request.scroll;
        let body = if request.require_full_review {
            reviewed_body = body
                .lines()
                .flat_map(|line| {
                    crate::memory_menu::wrap_review(
                        line,
                        usize::from(modal.width.saturating_sub(2)).max(1),
                    )
                })
                .collect::<Vec<_>>()
                .join("\n");
            let total = reviewed_body.lines().count();
            let start = usize::from(request.scroll)
                .min(total.saturating_sub(usize::from(modal.height.saturating_sub(2))));
            scroll = start as u16;
            let seen = request.review_seen.get();
            let end = start + usize::from(modal.height.saturating_sub(2));
            if start <= seen {
                request.review_seen.set(seen.max(end));
                request
                    .review_complete
                    .set(request.review_complete.get() || request.review_available && end >= total);
            }
            reviewed_body.as_str()
        } else {
            body.as_str()
        };
        let lines: Vec<Line> = body
            .lines()
            .enumerate()
            .map(|(index, text)| {
                if Some(index) == selected_row {
                    let padding =
                        usize::from(modal.width.saturating_sub(2)).saturating_sub(text.width());
                    Line::styled(
                        format!("{text}{}", " ".repeat(padding)),
                        Style::default()
                            .fg(theme::ACCENT)
                            .bg(theme::HIGHLIGHT)
                            .add_modifier(Modifier::BOLD),
                    )
                } else {
                    Line::from(text.to_owned())
                }
            })
            .collect();
        let title = if request.kind == "ask_user" {
            question
                .map(|question| {
                    format!(
                        " {} ",
                        clipped_title(
                            &question.question,
                            usize::from(modal.width.saturating_sub(4))
                        )
                        .0
                    )
                })
                .unwrap_or_else(|| " Choose an answer ".into())
        } else {
            format!(
                " {}{} ",
                if self.blink_on && !request.sending {
                    "● "
                } else {
                    "  "
                },
                clipped_title(
                    &super::markdown::sanitize(
                        request
                            .heading
                            .lines()
                            .next()
                            .unwrap_or("Approval required")
                            .trim_start_matches("Title: ")
                    ),
                    usize::from(modal.width.saturating_sub(8)),
                )
                .0,
            )
        };
        if !inline {
            frame.render_widget(Clear, modal);
        }
        let widget = Paragraph::new(lines).scroll((scroll, 0)).block(
            Block::default()
                .title(title)
                .borders(Borders::ALL)
                .border_style(Style::default().fg(theme::ACCENT))
                .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
        );
        let widget = if request.require_full_review {
            widget
        } else {
            widget.wrap(Wrap { trim: false })
        };
        frame.render_widget(widget, modal);
    }

    pub(super) fn draw_rail(&self, frame: &mut Frame, area: Rect) {
        let mut lines = Vec::new();
        let mut position = 0;
        for row in self.rail_rows() {
            match row {
                RailRow::Heading(index) => {
                    let item = &self.collections[index];
                    let mark = if item.collapsed { "▸" } else { "▾" };
                    lines.push(Line::styled(
                        format!(" {mark} {}", item.name),
                        Style::default()
                            .fg(theme::ACCENT)
                            .add_modifier(Modifier::BOLD),
                    ));
                }
                RailRow::LooseHeading => lines.push(Line::styled(
                    "  Sessions",
                    Style::default()
                        .fg(theme::ACCENT)
                        .add_modifier(Modifier::BOLD),
                )),
                RailRow::Session(index) => {
                    let session = &self.sessions[index];
                    let mark = if position == self.rail_selected {
                        "▸"
                    } else {
                        " "
                    };
                    let style = if self.waiting_for_input(&session.id) && self.blink_on {
                        Style::default()
                            .fg(theme::TEXT)
                            .bg(theme::ERROR)
                            .add_modifier(Modifier::BOLD)
                    } else {
                        Style::default()
                    };
                    lines.push(Line::styled(format!("{mark} {}", session.title), style));
                    position += 1;
                }
            }
        }
        if lines.is_empty() {
            lines.push(Line::from("  No sessions"));
        }
        frame.render_widget(
            Paragraph::new(lines)
                .style(Style::default().fg(theme::SECONDARY).bg(theme::RAIL))
                .block(
                    Block::default()
                        .title(if self.focus == Focus::Rail {
                            " Sessions ● "
                        } else {
                            " Sessions "
                        })
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(theme::BORDER)),
                ),
            area,
        );
    }

    pub(super) fn draw_group(&self, frame: &mut Frame, area: Rect, index: usize) {
        if area.width < 4 || area.height < 6 {
            return;
        }
        let group = &self.groups[index];
        let session = group
            .active_id()
            .and_then(|id| self.sessions.iter().find(|s| s.id == id));
        let active = self.active_group == index;
        let (draft, cursor) = group
            .active_id()
            .map(|id| {
                if active
                    && self
                        .active_request_index()
                        .is_some_and(|index| self.input_requests[index].freeform())
                {
                    let request = &self.input_requests[self.active_request_index().unwrap()];
                    (request.free_text.as_str(), request.free_cursor)
                } else if active && self.history_modal {
                    (
                        self.history_query.as_str(),
                        self.history_query_cursor.min(self.history_query.len()),
                    )
                } else if active {
                    (self.input.as_str(), self.input_cursor)
                } else {
                    self.input_drafts
                        .get(&(index, id.to_owned()))
                        .map(|(text, cursor)| (text.as_str(), *cursor))
                        .unwrap_or(("", 0))
                }
            })
            .unwrap_or(("", 0));
        let (draft, cursor) = if active {
            self.lore_picker
                .as_ref()
                .filter(|picker| {
                    picker.review.is_none() && !picker.resolving && !picker.belief_acting
                        && picker.belief_review.is_none()
                        && picker.evidence.is_none()
                })
                .map(|picker| (picker.query.as_str(), picker.query.len()))
                .unwrap_or((draft, cursor))
        } else {
            (draft, cursor)
        };
        let (draft, cursor) = if active
            && self.memory_manager.is_none()
            && self
                .chip_info
                .as_ref()
                .is_some_and(|info| info.kind == "memory")
        {
            self.memory_list
                .as_ref()
                .map(|list| (list.query.as_str(), list.query.len()))
                .unwrap_or((draft, cursor))
        } else {
            (draft, cursor)
        };
        let inner = self.pane_regions(index, area);
        let chooser_height = inner[2].height;
        let titles: Vec<Line> = group
            .tabs
            .iter()
            .map(|id| {
                let name = self
                    .sessions
                    .iter()
                    .find(|s| &s.id == id)
                    .map(|s| s.title.as_str())
                    .unwrap_or(id);
                let style = if self.waiting_for_input(id) && self.blink_on {
                    Style::default()
                        .fg(theme::TEXT)
                        .bg(theme::ERROR)
                        .add_modifier(Modifier::BOLD)
                } else {
                    Style::default()
                };
                Line::styled(name.to_owned(), style)
            })
            .collect();
        let clock_width = if index == self.active_group && !self.clock_text.is_empty() {
            (self.clock_text.width() + 2).min(usize::from(inner[0].width.saturating_sub(15))) as u16
        } else {
            0
        };
        let tabs_area = Rect::new(
            inner[0].x,
            inner[0].y,
            inner[0].width.saturating_sub(clock_width),
            inner[0].height,
        );
        let tabs = Tabs::new(if titles.is_empty() {
            vec![Line::from("Empty")]
        } else {
            titles
        })
        .select(group.active.min(group.tabs.len().saturating_sub(1)))
        .highlight_style(
            if group
                .active_id()
                .is_some_and(|id| self.waiting_for_input(id))
                && self.blink_on
            {
                Style::default()
                    .fg(theme::TEXT)
                    .bg(theme::ERROR)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default()
                    .fg(theme::ACCENT)
                    .add_modifier(Modifier::BOLD)
            },
        )
        .block(
            Block::default()
                .title(format!(
                    " Pane {}{} ",
                    index + 1,
                    if self.active_group == index && self.focus == Focus::Tabs {
                        " ● tabs"
                    } else if self.active_group == index {
                        " ●"
                    } else {
                        ""
                    },
                ))
                .borders(Borders::ALL)
                .border_style(
                    Style::default().fg(
                        if group
                            .active_id()
                            .is_some_and(|id| self.waiting_for_input(id))
                            && self.blink_on
                        {
                            theme::ERROR
                        } else if self.active_group == index && self.focus == Focus::Tabs {
                            theme::ACCENT
                        } else {
                            theme::BORDER
                        },
                    ),
                ),
        );
        frame.render_widget(
            tabs.style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            tabs_area,
        );
        if clock_width > 0 {
            frame.render_widget(
                Paragraph::new(self.clock_text.as_str())
                    .style(Style::default().fg(theme::MUTED).bg(theme::RAISED)),
                Rect::new(
                    inner[0].right() - clock_width,
                    inner[0].y + 1,
                    clock_width,
                    1,
                ),
            );
        }
        let content = session.map(|s| s.transcript.as_str()).unwrap_or("");
        let id = group.active_id().unwrap_or("");
        let cards_revision = self.tool_cards_revision.get(id).copied().unwrap_or(0);
        let activity_line = if self.activity_label(id) == Some("Processing") {
            Some(Line::styled(
                format!(" {} Processing…", SPINNER_FRAMES[self.spinner_frame]),
                Style::default().fg(theme::ACCENT),
            ))
        } else if self.activity_label(id) == Some("Queued") {
            Some(Line::styled(
                " Queued",
                Style::default().fg(theme::SECONDARY),
            ))
        } else {
            None
        };
        let show_welcome = content.trim().is_empty() && activity_line.is_none();
        let (lines, sections, link_regions, top) = {
            let mut cache = self.rendered_transcripts.borrow_mut();
            let position = cache
                .iter()
                .position(|entry| entry.pane == index && entry.id == id);
            let position = if let Some(position) = position {
                position
            } else {
                if let Some(old) = cache.iter().position(|entry| entry.pane == index) {
                    cache.remove(old);
                }
                if cache.len() == MAX_RENDERED_TRANSCRIPTS {
                    cache.remove(0);
                }
                cache.push(RenderedTranscript::render(
                    index,
                    id,
                    content,
                    inner[1].width.saturating_sub(2),
                    self.expanded_tool_sections.get(id),
                    self.tool_section_hover.as_ref().filter(|(session, _)| session == id).map(|(_, key)| key.clone()).or_else(|| (active && self.focus == Focus::Transcript).then(|| self.selected_tool_sections.get(id).cloned()).flatten()),
                    cards_revision,
                    self.tool_cards.for_session(id),
                ));
                cache.len() - 1
            };
            cache[position].update(
                content,
                inner[1].width.saturating_sub(2),
                self.expanded_tool_sections.get(id),
                self.tool_section_hover.as_ref().filter(|(session, _)| session == id).map(|(_, key)| key.clone()).or_else(|| (active && self.focus == Focus::Transcript).then(|| self.selected_tool_sections.get(id).cloned()).flatten()),
                cards_revision,
                self.tool_cards.for_session(id),
            );
            let (window, top) = transcript_window(
                &cache[position].lines,
                inner[1].height,
                group.scroll,
                activity_line,
            );
            if show_welcome {
                let identity = self.session_identity.get(id);
                let state = if session.is_some() {
                    crate::welcome::State::Ready {
                        engine: identity.and_then(|value| value.0.as_deref()),
                        model: identity.and_then(|value| value.1.as_deref()),
                    }
                } else if self.launching {
                    crate::welcome::State::Starting
                } else if self.awaiting_initial_attach || group.active_id().is_some() {
                    crate::welcome::State::Connecting
                } else {
                    crate::welcome::State::Empty {
                        reason: self.startup_recovery.as_deref(),
                    }
                };
                (
                    crate::welcome::lines(
                        state,
                        self.persist_preferences && self.preferences.on("boot_banner"),
                        inner[1].width.saturating_sub(2),
                        inner[1].height,
                    ),
                    Vec::new(),
                    Vec::new(),
                    0,
                )
            } else {
                (
                    window,
                    cache[position].sections.clone(),
                    cache[position]
                        .links
                        .iter()
                        .filter(|link| {
                            link.row >= top && link.row < top + usize::from(inner[1].height)
                        })
                        .cloned()
                        .map(|mut link| {
                            link.row -= top;
                            link
                        })
                        .collect::<Vec<_>>(),
                    top,
                )
            }
        };
        for section in sections {
            if section.line >= top && section.line < top + usize::from(inner[1].height) {
                self.visible_tool_sections.borrow_mut().push((
                    Rect::new(
                        inner[1].x.saturating_add(1),
                        inner[1].y.saturating_add((section.line - top) as u16),
                        inner[1].width.saturating_sub(2),
                        1,
                    ),
                    index,
                    id.to_owned(),
                    section.index,
                ));
            }
        }
        let content_width = usize::from(inner[1].width.saturating_sub(2));
        let mut visible_links = self.visible_links.borrow_mut();
        // Every explicit label reserves its cells, including labels whose
        // destination is refused. They must never fall back to a URL-looking
        // label's text and acquire a different destination.
        let label_cells: Vec<_> = link_regions
            .iter()
            .map(|link| (link.row, link.start, link.end))
            .collect();
        let explicit: Vec<links::LinkHit> = link_regions
            .into_iter()
            .filter(|link| links::safe_url(&link.url) && link.start < content_width)
            .map(|link| links::LinkHit {
                row: link.row,
                start: link.start,
                end: link.end.min(content_width),
                url: link.url.to_string(),
            })
            .collect();
        let mut hitboxes = explicit.clone();
        // A label may itself look like a bare URL. Its explicit destination
        // wins; never derive a second destination from those painted cells.
        hitboxes.extend(
            links::hits(&lines, content_width)
                .into_iter()
                .filter(|hit| {
                    !label_cells.iter().any(|&(row, start, end)| {
                        row == hit.row && start < hit.end && hit.start < end
                    })
                }),
        );
        for hit in hitboxes {
            if hit.end > hit.start {
                visible_links.push((
                    Rect::new(
                        inner[1]
                            .x
                            .saturating_add(1)
                            .saturating_add(hit.start as u16),
                        inner[1].y.saturating_add(hit.row as u16),
                        (hit.end - hit.start) as u16,
                        1,
                    ),
                    hit.url,
                ));
            }
        }
        frame.render_widget(
            Paragraph::new(lines)
                .style(Style::default().fg(theme::TEXT).bg(theme::BASE))
                .wrap(Wrap { trim: false })
                .block(
                    Block::default()
                        .borders(Borders::LEFT | Borders::RIGHT)
                        .border_style(
                            Style::default().fg(
                                if group
                                    .active_id()
                                    .is_some_and(|id| self.waiting_for_input(id))
                                    && self.blink_on
                                {
                                    theme::ERROR
                                } else {
                                    theme::BORDER
                                },
                            ),
                        ),
                ),
            inner[1],
        );
        if !self.link_interaction_blocked() {
            self.transcript_selection.borrow_mut().register(
                crate::selection::Owner {
                    pane: index,
                    session: id.to_owned(),
                },
                Rect::new(
                    inner[1].x.saturating_add(1),
                    inner[1].y,
                    inner[1].width.saturating_sub(2),
                    inner[1].height,
                ),
            );
        }
        if active && chooser_height > 0 {
            if self.active_request_index().is_some()
            {
                self.draw_request(frame, inner[2], true);
            } else if self.settings_menu.is_some() {
                self.draw_settings_menu(frame, inner[2]);
            } else if self.engine_picker
                || self.new_session.is_some()
                || self.model_picker.is_some()
                || self.effort_picker.is_some()
                || self.permission_picker.is_some()
            {
                self.draw_chip_picker(frame, inner[2]);
            } else if self.repo_picker.is_some() {
                self.draw_repo_picker(frame, inner[2]);
            } else if self.lore_picker.is_some() {
                self.draw_lore_picker(frame, inner[2]);
            } else if self.action_menu {
                self.draw_actions(frame, inner[2]);
            } else if self.chip_info.is_some() {
                self.draw_chip_info(frame, inner[2]);
            } else if self.history_modal {
                self.draw_history(frame, inner[2]);
            } else if self.queue_picker.is_some() {
                self.draw_queue_picker(frame, inner[2]);
            } else if self.attach_picker.is_some() {
                self.draw_attach_picker(frame, inner[2]);
            } else if self.branch_picker.is_some() {
                self.draw_branch_picker(frame, inner[2]);
            } else if !self.slash_suggestions().is_empty() {
                self.draw_slash_suggestions(frame, inner[2]);
            }
        }
        let mut chip_spans = Vec::new();
        let mut chip_x = inner[3].x;
        for (kind, label) in self.chip_window(index, usize::from(inner[3].width)) {
            if !chip_spans.is_empty() {
                chip_spans.push(Span::raw(" "));
            }
            let text = chip_text(kind, &label);
            let end = chip_x
                .saturating_add(text.width() as u16)
                .min(inner[3].right());
            if end > chip_x {
                if let Some(hits) = self.rendered_chip_hits.borrow_mut().as_mut() {
                    hits.push(ChipHit {
                        group: index,
                        kind,
                        rect: Rect::new(chip_x, inner[3].y, end - chip_x, 1),
                        pane: area,
                    });
                }
            }
            chip_x = end.saturating_add(1);
            let hovered = !self.link_interaction_blocked()
                && self
                    .chip_hover
                    .as_ref()
                    .is_some_and(|hit| hit.group == index && hit.kind == kind);
            let quota_color = (kind == "cost").then(|| self.session_telemetry.get(id)
                .and_then(|telemetry| telemetry.quota_color())).flatten();
            chip_spans.push(Span::styled(
                text,
                Style::default()
                    .fg(quota_color.unwrap_or(if hovered { theme::ACCENT } else { theme::TEXT }))
                    .bg(theme::HIGHLIGHT)
                    .add_modifier(if active && self.focus == Focus::Chip(kind) {
                        Modifier::BOLD | Modifier::UNDERLINED | Modifier::REVERSED
                    } else if hovered {
                        Modifier::BOLD
                    } else {
                        Modifier::empty()
                    }),
            ));
        }
        frame.render_widget(
            Paragraph::new(Line::from(chip_spans)).style(Style::default().bg(theme::RAISED)),
            inner[3],
        );
        let cursor = cursor.min(draft.len());
        let cursor_line = draft[..cursor].bytes().filter(|b| *b == b'\n').count();
        let cursor_column =
            UnicodeWidthStr::width(draft[..cursor].rsplit('\n').next().unwrap_or("")) + 2;
        let mut rows: Vec<String> = draft
            .split('\n')
            .enumerate()
            .map(|(line, text)| format!("{}{}", if line == 0 { "> " } else { "  " }, text))
            .collect();
        if active && self.focus == Focus::Prompt {
            let offset = draft[..cursor].rsplit('\n').next().unwrap_or("").len() + 2;
            rows[cursor_line].insert(offset, '▏');
        }
        let visible = usize::from(inner[4].height.saturating_sub(2)).max(1);
        let scroll_y = cursor_line.saturating_sub(visible.saturating_sub(1));
        let width = usize::from(inner[4].width.saturating_sub(2)).max(1);
        let scroll_x = cursor_column.saturating_sub(width.saturating_sub(1));
        frame.render_widget(
            Paragraph::new(rows.join("\n"))
                .scroll((scroll_y as u16, scroll_x as u16))
                .style(Style::default().fg(theme::TEXT).bg(theme::RAISED))
                .block(
                    Block::default()
                        .title(
                            if active
                                && self
                                    .active_request_index()
                                    .is_some_and(|index| self.input_requests[index].freeform())
                            {
                                " Answer question ● "
                            } else if active && self.history_modal {
                                " Search sessions ● "
                            } else if active
                                && self.memory_manager.is_none()
                                && self
                                    .chip_info
                                    .as_ref()
                                    .is_some_and(|info| info.kind == "memory")
                            {
                                " Filter memory ● "
                            } else if active
                                && self.lore_picker.as_ref().is_some_and(|picker| {
                                    picker.review.is_none() && !picker.resolving && !picker.belief_acting
                                        && picker.belief_review.is_none()
                                        && picker.evidence.is_none()
                                })
                            {
                                if self.lore_picker.as_ref().is_some_and(|picker| picker.proposal_mode) { " Filter pending ● " } else { " Filter beliefs ● " }
                            } else if active && self.focus == Focus::Prompt {
                                " Prompt ● "
                            } else {
                                " Prompt "
                            },
                        )
                        .borders(Borders::ALL)
                        .border_style(Style::default().fg(
                            if active && self.focus == Focus::Prompt {
                                theme::ACCENT
                            } else {
                                theme::BORDER
                            },
                        )),
                ),
            inner[4],
        );
        let status = session.map(|s| s.status.as_str()).unwrap_or("No session");
        let status_line = if active && !self.notice.is_empty() {
            format!(" {} · {status}", self.notice)
        } else {
            format!(" {status}")
        };
        frame.render_widget(
            Paragraph::new(status_line)
                .style(Style::default().fg(theme::SECONDARY).bg(theme::RAISED)),
            Rect {
                height: 1,
                ..inner[5]
            },
        );
    }
}

/// Official provider component tokens stay distinct from DOXA snapshot chars.
pub(super) fn context_detail_lines(detail: &serde_json::Value) -> Vec<String> {
    let mut lines = Vec::new();
    if let Some(source) = detail["source"].as_str() {
        lines.push(format!("Source: {}", safe_label(source)));
    }
    if let Some(categories) = detail["categories"].as_array() {
        lines.push("## Reported context components (tokens)".into());
        for row in categories.iter().take(64) {
            if let (Some(name), Some(tokens)) = (row["name"].as_str(), row["tokens"].as_u64()) {
                lines.push(format!(
                    "{}: {tokens}",
                    clipped_title(&safe_label(name), 160).0
                ));
            }
        }
    }
    if let Some(files) = detail["memory_files"].as_array() {
        lines.push("## Reported memory files (tokens)".into());
        for row in files.iter().take(64) {
            if let (Some(path), Some(tokens)) = (row["path"].as_str(), row["tokens"].as_u64()) {
                lines.push(format!(
                    "{} · {}: {tokens}",
                    clipped_title(&safe_label(path), 200).0,
                    safe_label(row["type"].as_str().unwrap_or("file"))
                ));
            }
        }
    }
    for field in ["mcp_tools", "agents"] {
        if let Some(rows) = detail[field].as_array() {
            lines.push(format!("## Reported {}", field.replace('_', " ")));
            for row in rows.iter().take(64) {
                if let Some(name) = row
                    .as_str()
                    .or_else(|| row["name"].as_str())
                    .or_else(|| row["agent_type"].as_str())
                {
                    let count = row["tokens"]
                        .as_u64()
                        .map(|tokens| format!(": {tokens} tokens"))
                        .unwrap_or_default();
                    lines.push(format!(
                        "{}{count}",
                        clipped_title(&safe_label(name), 160).0
                    ));
                }
            }
        }
    }
    for (key, label) in [
        ("total_tokens", "Reported total"),
        ("max_tokens", "Reported context window"),
        ("raw_max_tokens", "Raw context window"),
        ("autocompact_threshold", "Auto compact threshold"),
    ] {
        if let Some(tokens) = detail[key].as_u64() {
            lines.push(format!("{label}: {tokens} tokens"));
        }
    }
    if let Some(enabled) = detail["autocompact_enabled"].as_bool() {
        lines.push(format!("Auto compact enabled: {enabled}"));
    }
    for key in ["adopted_skills", "adopted_skill_plugins"] {
        if let Some(count) = detail[key].as_u64() {
            lines.push(format!("{}: {count} (DOXA count)", key.replace('_', " ")));
        }
    }
    if let Some(object) = detail.as_object() {
        let mut counters: Vec<_> = object
            .iter()
            .filter(|(key, value)| key.ends_with("_chars") && value.as_u64().is_some())
            .collect();
        counters.sort_by_key(|(key, _)| *key);
        if !counters.is_empty() {
            lines.push("## DOXA local snapshot metadata (characters; not token counts)".into());
        }
        for (key, value) in counters.into_iter().take(32) {
            lines.push(format!(
                "{}: {} chars",
                safe_label(key).replace('_', " "),
                value
            ));
        }
    }
    if lines.is_empty() {
        lines.push("No detailed context metadata reported".into());
    }
    lines
}

pub(super) fn transcript_window(
    lines: &[Line<'static>],
    viewport: u16,
    scroll: usize,
    extra: Option<Line<'static>>,
) -> (Vec<Line<'static>>, usize) {
    // Markdown has already wrapped lines to the pane's content width. Give
    // Paragraph only visible rows: handing it the whole transcript makes
    // every spinner frame rewrap thousands of off-screen lines.
    let viewport = usize::from(viewport);
    let total = lines.len() + usize::from(extra.is_some());
    let max_scroll = total.saturating_sub(viewport);
    let top = max_scroll.saturating_sub(scroll.min(max_scroll));
    let end = (top + viewport).min(total);
    let mut window = lines[top.min(lines.len())..end.min(lines.len())].to_vec();
    if end > lines.len() {
        if let Some(extra) = extra {
            window.push(extra);
        }
    }
    (window, top)
}

fn lore_view_title(picker: &super::LorePicker) -> Line<'static> {
    Line::from([("1 Active", !picker.proposal_mode), ("2 Pending", picker.proposal_mode && !picker.cluster_mode), ("3 Clustered", picker.cluster_mode)]
        .into_iter().map(|(label, selected)| Span::styled(format!(" {label} "), chooser_row_style(selected))).collect::<Vec<_>>())
}
