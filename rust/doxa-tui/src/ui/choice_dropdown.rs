//! Shared, staged selection for settings and new-session choice fields.
use std::cell::Cell;
use crossterm::event::{KeyCode, KeyEvent, MouseButton, MouseEvent, MouseEventKind};
use ratatui::{layout::{Position, Rect}, style::{Style}, text::{Line, Span}, widgets::{Block, Borders, Clear, Paragraph}, Frame};
use unicode_width::UnicodeWidthStr;
use super::{App, safe_label, vendor_models};
use crate::{launch, settings, theme};

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum Owner {
    Setting(String),
    NewSession { engine: launch::Engine, field: usize, effort_model: Option<String> },
}

#[derive(Debug)]
pub(super) struct Dropdown {
    owner: Owner,
    title: String,
    choices: Vec<String>,
    selected: usize,
    start: Cell<usize>,
}

impl Dropdown {
    fn visible_start(&self, count: usize) -> usize {
        let mut start = self.start.get().min(self.choices.len().saturating_sub(count));
        if self.selected < start { start = self.selected; }
        if self.selected >= start + count { start = self.selected + 1 - count; }
        self.start.set(start);
        start
    }
}

impl App {
    fn dropdown_choice_label(&self,owner:&Owner,value:&str)->String {
        match owner {
            Owner::Setting(key) if key=="engine"=>safe_label(settings::engine_label(value)),
            Owner::NewSession{engine:launch::Engine::Router,field:0,..}=>safe_label(&self.router_target_label(value)),
            _=>choice_label(value),
        }
    }
    fn choice_values(&self, owner: &Owner) -> Option<(String, Vec<String>, String)> {
        match owner {
            Owner::Setting(key) => {
                let menu = self.settings_menu.as_ref()?;
                let row = menu.rows.get(menu.selected)?;
                if row.setting.key != key || row.setting.category != settings::CATEGORIES[menu.category]
                    || row.shadowed || row.setting.read_only || row.setting.kind != settings::Kind::Choice { return None; }
                Some((row.setting.label.into(), row.setting.choices.iter().map(|s| (*s).into()).collect(), menu.effective_value(key)))
            }
            Owner::NewSession { engine, field, effort_model } => {
                let form = self.new_session.as_ref()?;
                if self.launching || form.engine != *engine || form.field != *field { return None; }
                let vendor = !vendor_models(form.engine).is_empty();
                let isolation_field = if vendor { 2 } else { 1 };
                if *field == isolation_field {
                    let values = if *engine == launch::Engine::Router { vec!["native".into()] }
                        else { vec!["native".into(), "docker-open".into(), "docker-offline".into()] };
                    Some(("Isolation".into(), values, form.isolation.key().into()))
                } else if *field == 0 {
                    Some((if *engine == launch::Engine::Router { "Target" } else { "Model" }.into(), form.models.clone(), form.model.clone()))
                } else if vendor && *field == 1 && effort_model.as_deref() == Some(form.model.as_str()) {
                    Some(("Effort".into(), form.model_efforts.get(&form.model).cloned().unwrap_or_default(), form.effort.clone().unwrap_or_default()))
                } else { None }
            }
        }
    }

    fn open_choice_dropdown(&mut self, owner: Owner) -> bool {
        let Some((title, choices, current)) = self.choice_values(&owner) else { return false; };
        let selected = choices.iter().position(|value| *value == current).unwrap_or(0);
        self.choice_dropdown = Some(Dropdown { owner, title, choices, selected, start: Cell::new(0) });
        true
    }

    pub(super) fn open_setting_dropdown(&mut self) -> bool {
        let Some(key) = self.settings_menu.as_ref().and_then(|menu| menu.rows.get(menu.selected))
            .map(|row| row.setting.key.to_owned()) else { return false; };
        self.open_choice_dropdown(Owner::Setting(key))
    }

    pub(super) fn open_new_session_dropdown(&mut self) -> bool {
        let Some(form) = self.new_session.as_ref() else { return false; };
        let effort_model = (!vendor_models(form.engine).is_empty() && form.field == 1).then(|| form.model.clone());
        self.open_choice_dropdown(Owner::NewSession { engine: form.engine, field: form.field, effort_model })
    }

    pub(super) fn refresh_choice_dropdown(&mut self) {
        let Some(dropdown) = self.choice_dropdown.as_ref() else { return; };
        let previous = dropdown.choices.get(dropdown.selected).cloned();
        let Some((_, choices, current)) = self.choice_values(&dropdown.owner) else { self.choice_dropdown = None; return; };
        let dropdown = self.choice_dropdown.as_mut().unwrap();
        dropdown.selected = previous.and_then(|value| choices.iter().position(|choice| *choice == value))
            .or_else(|| choices.iter().position(|choice| *choice == current)).unwrap_or(0);
        dropdown.choices = choices;
    }

    fn select_dropdown_choice(&mut self) {
        let Some(dropdown) = self.choice_dropdown.take() else { return; };
        let Some(value) = dropdown.choices.get(dropdown.selected) else { return; };
        // Async catalogs and parent changes cannot make an old option authoritative.
        if !self.choice_values(&dropdown.owner).is_some_and(|(_, choices, _)| choices.contains(value)) {
            self.notice = "Choices changed; reopen the dropdown".into(); return;
        }
        match dropdown.owner {
            Owner::Setting(key) => { self.settings_menu.as_mut().unwrap().edits.insert(key, Some(value.clone())); }
            Owner::NewSession { field, .. } => {
                let form = self.new_session.as_mut().unwrap();
                let vendor = !vendor_models(form.engine).is_empty();
                if field == (if vendor { 2 } else { 1 }) {
                    form.isolation = match value.as_str() {
                        "native" => doxa_isolation::Profile::Native,
                        "docker-open" => doxa_isolation::Profile::DockerOpen,
                        "docker-offline" => doxa_isolation::Profile::DockerOffline,
                        _ => return,
                    };
                } else if field == 0 {
                    form.model = value.clone();
                    if vendor {
                        let levels = form.model_efforts.get(value).cloned().unwrap_or_default();
                        if form.effort.as_ref().is_none_or(|effort| !levels.contains(effort)) {
                            form.effort = levels.iter().find(|level| *level == "high").cloned().or_else(|| levels.first().cloned());
                        }
                    }
                } else { form.effort = Some(value.clone()); }
            }
        }
    }

    pub(super) fn choice_dropdown_key(&mut self, key: KeyEvent) -> bool {
        let Some(dropdown) = self.choice_dropdown.as_ref() else { return false; };
        if self.choice_values(&dropdown.owner).is_none() { self.choice_dropdown = None; return true; }
        if matches!(key.code, KeyCode::Char('r' | 'R')) && matches!(dropdown.owner, Owner::NewSession { field: 0, .. }) {
            self.refresh_new_session_catalog();
            return true;
        }
        if dropdown.choices.is_empty() {
            if key.code == KeyCode::Esc { self.choice_dropdown = None; }
            return true;
        }
        let selected = dropdown.selected;
        let last = dropdown.choices.len() - 1;
        match key.code {
            KeyCode::Esc => self.choice_dropdown = None,
            KeyCode::Enter => self.select_dropdown_choice(),
            KeyCode::Up | KeyCode::BackTab => self.choice_dropdown.as_mut().unwrap().selected = selected.saturating_sub(1),
            KeyCode::Down | KeyCode::Tab => self.choice_dropdown.as_mut().unwrap().selected = (selected + 1).min(last),
            KeyCode::Home => self.choice_dropdown.as_mut().unwrap().selected = 0,
            KeyCode::End => self.choice_dropdown.as_mut().unwrap().selected = last,
            KeyCode::PageUp | KeyCode::PageDown => {
                let count = self.choice_dropdown_rect().map_or(1, |area| usize::from(area.height.saturating_sub(2)).max(1));
                let dropdown = self.choice_dropdown.as_mut().unwrap();
                dropdown.selected = if key.code == KeyCode::PageUp { dropdown.selected.saturating_sub(count) }
                    else { (dropdown.selected + count).min(dropdown.choices.len() - 1) };
            }
            _ => {},
        }
        true
    }

    pub(super) fn choice_dropdown_rect(&self) -> Option<Rect> {
        let dropdown = self.choice_dropdown.as_ref()?;
        self.choice_values(&dropdown.owner)?;
        if self.active_request_index().is_some() || self.delete_confirmation.is_some()
            || self.stop_confirmation.is_some() || self.fleet_review.is_some()
            || self.fleet_dependency_review.is_some() || self.diff_modal || self.map_modal { return None; }
        let parent = self.active_chooser_rect()?;
        let row = match &dropdown.owner {
            Owner::Setting(_) => {
                let menu = self.settings_menu.as_ref()?;
                parent.y + 3 + menu.visible_indices(parent.height).iter().position(|index| *index == menu.selected)? as u16
            }
            Owner::NewSession { field, .. } => parent.y + if parent.height >= 8 { 4 } else { 2 } + *field as u16,
        };
        let height = (dropdown.choices.len() as u16 + 2).min(parent.height.saturating_sub(2)).max(3);
        let width = dropdown.choices.iter().map(|value| self.dropdown_choice_label(&dropdown.owner,value).width() as u16 + 4).max().unwrap_or(10)
            .max(safe_label(&dropdown.title).width() as u16 + 4).max(26).min(parent.width.saturating_sub(2));
        let max_y = parent.bottom().saturating_sub(height + 1);
        let y = if row + 1 + height < parent.bottom() { row + 1 }
            else if row >= parent.y + height + 1 { row - height }
            else { parent.y + 1 }.min(max_y);
        Some(Rect::new(parent.x + 1, y, width, height))
    }

    pub(super) fn choice_dropdown_mouse(&mut self, mouse: MouseEvent) -> bool {
        let Some(area) = self.choice_dropdown_rect() else { self.choice_dropdown = None; return true; };
        let point = Position::new(mouse.column, mouse.row);
        if matches!(mouse.kind, MouseEventKind::ScrollUp | MouseEventKind::ScrollDown) {
            if area.contains(point) {
                let code = if mouse.kind == MouseEventKind::ScrollUp { KeyCode::Up } else { KeyCode::Down };
                return self.choice_dropdown_key(KeyEvent::new(code, crossterm::event::KeyModifiers::NONE));
            }
        } else if matches!(mouse.kind, MouseEventKind::Moved | MouseEventKind::Down(MouseButton::Left)) {
            let count = usize::from(area.height - 2);
            let dropdown = self.choice_dropdown.as_ref().unwrap();
            let start = dropdown.visible_start(count);
            let index = start + usize::from(mouse.row.saturating_sub(area.y + 1));
            if mouse.column > area.x && mouse.column < area.right() - 1 && mouse.row > area.y
                && mouse.row < area.bottom() - 1 && index < dropdown.choices.len() {
                self.choice_dropdown.as_mut().unwrap().selected = index;
                if mouse.kind == MouseEventKind::Down(MouseButton::Left) { self.select_dropdown_choice(); }
            } else if mouse.kind == MouseEventKind::Down(MouseButton::Left) && !area.contains(point) {
                self.choice_dropdown = None;
            }
        }
        true
    }

    pub(super) fn draw_choice_dropdown(&self, frame: &mut Frame) {
        let Some(area) = self.choice_dropdown_rect() else { return; };
        let dropdown = self.choice_dropdown.as_ref().unwrap();
        let count = usize::from(area.height - 2);
        let start = dropdown.visible_start(count);
        let mut lines = dropdown.choices.iter().enumerate().skip(start).take(count).map(|(index, value)|
            Line::styled(format!(" {} {}", if index == dropdown.selected { '›' } else { ' ' }, self.dropdown_choice_label(&dropdown.owner,value)),
                super::chooser_row_style(index == dropdown.selected))).collect::<Vec<_>>();
        let model = matches!(dropdown.owner, Owner::NewSession { field: 0, .. });
        if lines.is_empty() {
            let loading = self.new_session.as_ref().is_some_and(|form| form.catalog_pending);
            lines.push(Line::styled(if loading { " Loading models…" } else { " No models · R retries" }, Style::default().fg(theme::MUTED)));
        }
        frame.render_widget(Clear, area);
        frame.render_widget(Paragraph::new(lines).block(Block::default().borders(Borders::ALL)
            .title(format!(" {} ", safe_label(&dropdown.title)))
            .title_bottom(if model { " Enter · Esc · R refresh " } else { " Enter select · Esc cancel " })
            .border_style(Style::default().fg(theme::ACCENT)))
            .style(Style::default().fg(theme::TEXT).bg(theme::RAISED)), area);
        super::render::draw_menu_scrollbar(frame, area, dropdown.choices.len(), count, start);
    }

    pub(super) fn select_settings_category(&mut self, index: usize) {
        let Some(menu) = self.settings_menu.as_mut() else { return; };
        if index >= settings::CATEGORIES.len() { return; }
        menu.finish_draft();
        menu.category = index;
        menu.selected = menu.indices().first().copied().unwrap_or(0);
        self.choice_dropdown = None;
    }
}

fn choice_label(value: &str) -> String {
    if value.is_empty() { "(default)".into() } else { safe_label(value) }
}

/// Render and hit testing consume the same title-bar tabs, including overflow.
pub(super) fn settings_tabs(area: Rect, selected: usize) -> Vec<(usize, Rect, String)> {
    let labels = settings::CATEGORIES.iter().enumerate().map(|(index, label)| format!(" {} {label} ", index + 1)).collect::<Vec<_>>();
    let capacity = usize::from(area.width.saturating_sub(4));
    let mut start = 0;
    while start < selected && labels[start..=selected].iter().map(|s| s.width()).sum::<usize>() > capacity { start += 1; }
    let mut x = area.x + 1;
    let mut result = Vec::new();
    if start > 0 { result.push((start - 1, Rect::new(x, area.y, 1, 1), "‹".into())); }
    x += 1;
    let right = area.right().saturating_sub(2);
    for (index, label) in labels.iter().enumerate().skip(start) {
        let width = label.width() as u16;
        if x + width > right {
            if index == start {
                let shown = label.chars().take(usize::from(right.saturating_sub(x))).collect::<String>();
                result.push((index, Rect::new(x, area.y, shown.width() as u16, 1), shown));
            }
            if index > selected || index == start { result.push(((if index == start { index + 1 } else { index }).min(labels.len()-1), Rect::new(right, area.y, 1, 1), "›".into())); }
            break;
        }
        result.push((index, Rect::new(x, area.y, width, 1), label.clone()));
        x += width;
    }
    result
}

pub(super) fn settings_tab_line(area: Rect, selected: usize) -> Line<'static> {
    let tabs = settings_tabs(area, selected);
    let mut spans = Vec::new();
    let mut x = area.x + 1;
    for (index, rect, label) in tabs {
        if rect.x > x { spans.push(Span::raw(" ".repeat(usize::from(rect.x - x)))); }
        spans.push(Span::styled(label, super::chooser_row_style(index == selected)));
        x = rect.right();
    }
    Line::from(spans)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use crossterm::event::{Event, KeyModifiers};
    use ratatui::{backend::TestBackend, Terminal};
    use super::super::{NewSession, SettingsMenu};

    fn key(app: &mut App, code: KeyCode) {
        app.handle(Event::Key(KeyEvent::new(code, KeyModifiers::NONE)));
    }
    fn mouse(app: &mut App, kind: MouseEventKind, x: u16, y: u16) {
        app.handle(Event::Mouse(MouseEvent { kind, column: x, row: y, modifiers: KeyModifiers::NONE }));
    }
    fn paint(app: &App) -> String {
        let mut terminal = Terminal::new(TestBackend::new(app.size.width, app.size.height)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        terminal.backend().buffer().content.iter().map(|cell| cell.symbol()).collect()
    }
    fn settings_app() -> App {
        let mut app = App { size: Rect::new(0, 0, 160, 36), rail_visible: false, ..Default::default() };
        let rows = settings::SETTINGS.iter().map(|setting| settings::Row {
            setting, value: setting.default.into(), stored: setting.default.into(), source: "default".into(), shadowed: false,
        }).collect();
        app.settings_menu = Some(SettingsMenu { rows, selected: 0, category: 0, draft: None, edits: HashMap::new(), engine: "claude".into() });
        app
    }
    fn vendor_app() -> App {
        let mut app = App { size: Rect::new(0, 0, 100, 30), rail_visible: false, ..Default::default() };
        app.new_session = Some(NewSession {
            engine: launch::Engine::Glm, isolation: doxa_isolation::Profile::Native, router_config: None,
            model: "fixture-a".into(), models: vec!["fixture-a".into(), "fixture-b".into()],
            model_efforts: HashMap::from([("fixture-a".into(), vec!["high".into(), "xhigh".into()]),
                ("fixture-b".into(), vec!["low".into(), "high".into()])]),
            catalog_note: "Controlled catalog".into(), catalog_pending: false, launch_error: None,
            retry_allowed: true, effort: Some("xhigh".into()), prompt: "preserve prompt".into(), field: 0,
        });
        app
    }

    #[test]
    fn every_settings_choice_is_staged_and_environment_rows_stay_read_only() {
        for (index, setting) in settings::SETTINGS.iter().enumerate().filter(|(_, setting)| setting.kind == settings::Kind::Choice) {
            let mut app = settings_app();
            let menu = app.settings_menu.as_mut().unwrap();
            menu.selected = index;
            menu.category = settings::CATEGORIES.iter().position(|category| *category == setting.category).unwrap();
            key(&mut app, KeyCode::Enter);
            assert!(app.choice_dropdown.is_some(), "{}", setting.key);
            key(&mut app, KeyCode::Down);
            assert!(app.settings_menu.as_ref().unwrap().edits.is_empty());
            key(&mut app, KeyCode::Esc);
            assert!(app.settings_menu.is_some());
            assert!(app.settings_menu.as_ref().unwrap().edits.is_empty());
            key(&mut app, KeyCode::Enter);
            key(&mut app, KeyCode::End);
            key(&mut app, KeyCode::Enter);
            assert_eq!(app.settings_menu.as_ref().unwrap().edits[setting.key].as_deref(), setting.choices.last().copied());
            app.settings_menu.as_mut().unwrap().rows[index].shadowed = true;
            key(&mut app, KeyCode::Enter);
            assert!(app.choice_dropdown.is_none());
            assert!(app.notice.contains("Environment override"));
            assert!(app.pending_prompts.is_empty());
        }
    }

    #[test]
    fn settings_numbered_tabs_are_clickable_and_preserve_text_edits() {
        let mut app = settings_app();
        assert!(paint(&app).contains("1 Session"));
        for category in 0..settings::CATEGORIES.len() {
            key(&mut app, KeyCode::Char(char::from(b'1' + category as u8)));
            assert_eq!(app.settings_menu.as_ref().unwrap().category, category);
            let area = app.active_chooser_rect().unwrap();
            let tab = settings_tabs(area, category).into_iter().find(|(index, _, label)| *index == category && label.contains(settings::CATEGORIES[category])).unwrap();
            let mut terminal = Terminal::new(TestBackend::new(app.size.width, app.size.height)).unwrap();
            terminal.draw(|frame| app.draw(frame)).unwrap();
            let row = |y| (area.x..area.right()).map(|x|
                terminal.backend().buffer()[(x, y)].symbol()).collect::<String>();
            assert_eq!(tab.1.y, area.y);
            assert!(row(area.y).contains(settings::CATEGORIES[category]));
            assert!(!row(area.y + 2).contains(settings::CATEGORIES[category]));
            mouse(&mut app, MouseEventKind::Down(MouseButton::Left), tab.1.x + 1, tab.1.y);
            assert_eq!(app.settings_menu.as_ref().unwrap().category, category);
            assert_eq!(app.drag, None, "title tab click must not resize the menu");
        }
        key(&mut app, KeyCode::Char('1'));
        let menu = app.settings_menu.as_mut().unwrap();
        menu.selected = menu.rows.iter().position(|row| row.setting.key == "linger_secs").unwrap();
        key(&mut app, KeyCode::Enter);
        key(&mut app, KeyCode::Char('9'));
        assert_eq!(app.settings_menu.as_ref().unwrap().category, 0);
        let area = app.active_chooser_rect().unwrap();
        let fleet = settings_tabs(area, 0).into_iter().find(|(index, _, _)| *index == 1).unwrap();
        mouse(&mut app, MouseEventKind::Down(MouseButton::Left), fleet.1.x + 1, fleet.1.y);
        assert_eq!(app.settings_menu.as_ref().unwrap().category, 1);
        assert_eq!(app.settings_menu.as_ref().unwrap().edits["linger_secs"].as_deref(), Some("1209"));
        key(&mut app, KeyCode::Esc);
        assert!(app.settings_menu.is_none());
    }

    #[test]
    fn settings_single_click_opens_choice_and_click_selects_without_saving() {
        let mut app = settings_app();
        let output = paint(&app);
        assert!(output.contains("▾"));
        let menu = app.active_chooser_rect().unwrap();
        mouse(&mut app, MouseEventKind::Down(MouseButton::Left), menu.x + 4, menu.y + 3);
        assert!(app.choice_dropdown.is_some());
        let dropdown = app.choice_dropdown_rect().unwrap();
        assert!(menu.contains(Position::new(dropdown.x, dropdown.y)));
        mouse(&mut app, MouseEventKind::Down(MouseButton::Left), dropdown.x + 2, dropdown.y + 3);
        assert!(app.choice_dropdown.is_none());
        assert_eq!(app.settings_menu.as_ref().unwrap().edits["engine"].as_deref(), Some("codex"));
        assert!(app.settings_menu.as_ref().unwrap().draft.is_none());
        assert!(app.pending_launches.is_empty());
    }

    #[test]
    fn new_session_dropdowns_share_click_cancel_and_explicit_selection() {
        let mut app = vendor_app();
        assert!(paint(&app).contains("[fixture-a ▾]"));
        let area = app.active_chooser_rect().unwrap();
        mouse(&mut app, MouseEventKind::Down(MouseButton::Left), area.x + 3, area.y + 4);
        assert!(app.choice_dropdown.is_some());
        key(&mut app, KeyCode::Down);
        assert_eq!(app.new_session.as_ref().unwrap().model, "fixture-a");
        key(&mut app, KeyCode::Esc);
        key(&mut app, KeyCode::Enter);
        key(&mut app, KeyCode::Down);
        key(&mut app, KeyCode::Enter);
        assert_eq!(app.new_session.as_ref().unwrap().model, "fixture-b");
        assert_eq!(app.new_session.as_ref().unwrap().effort.as_deref(), Some("high"));
        key(&mut app, KeyCode::Tab);
        key(&mut app, KeyCode::Enter);
        key(&mut app, KeyCode::Home);
        key(&mut app, KeyCode::Enter);
        assert_eq!(app.new_session.as_ref().unwrap().effort.as_deref(), Some("low"));
        key(&mut app, KeyCode::Tab);
        key(&mut app, KeyCode::Enter);
        key(&mut app, KeyCode::End);
        key(&mut app, KeyCode::Enter);
        assert_eq!(app.new_session.as_ref().unwrap().isolation, doxa_isolation::Profile::DockerOffline);
        assert_eq!(app.new_session.as_ref().unwrap().prompt, "preserve prompt");
        assert!(app.pending_launches.is_empty());
    }

    #[test]
    fn dropdown_refuses_revoked_catalog_choices_and_effort_for_another_model() {
        let mut app = vendor_app();
        key(&mut app, KeyCode::Enter);
        key(&mut app, KeyCode::Down);
        app.new_session.as_mut().unwrap().models.pop();
        key(&mut app, KeyCode::Enter);
        assert_eq!(app.new_session.as_ref().unwrap().model, "fixture-a");
        assert!(app.notice.contains("Choices changed"));
        app.new_session.as_mut().unwrap().field = 1;
        key(&mut app, KeyCode::Enter);
        app.new_session.as_mut().unwrap().model = "fixture-b".into();
        key(&mut app, KeyCode::Home);
        assert!(app.choice_dropdown.is_none());
        assert_eq!(app.new_session.as_ref().unwrap().effort.as_deref(), Some("xhigh"));
        assert!(app.pending_launches.is_empty());
    }

    #[test]
    fn dropdown_scrolls_in_narrow_panes_and_outside_click_does_not_start_session() {
        let mut app = vendor_app();
        app.size = Rect::new(0, 0, 40, 18);
        let form = app.new_session.as_mut().unwrap();
        form.models = (0..30).map(|index| format!("model-{index:02}")).collect();
        form.model = "model-00".into();
        paint(&app);
        key(&mut app, KeyCode::Enter);
        key(&mut app, KeyCode::End);
        assert!(paint(&app).contains("model-29"));
        let area = app.choice_dropdown_rect().unwrap();
        mouse(&mut app, MouseEventKind::ScrollUp, area.x + 2, area.y + 1);
        assert_eq!(app.new_session.as_ref().unwrap().model, "model-00");
        paint(&app);
        let area = app.choice_dropdown_rect().unwrap();
        mouse(&mut app, MouseEventKind::Moved, area.x + 2, area.bottom() - 2);
        let stable = app.choice_dropdown.as_ref().unwrap().start.get();
        paint(&app);
        assert_eq!(app.choice_dropdown.as_ref().unwrap().start.get(), stable);
        mouse(&mut app, MouseEventKind::Down(MouseButton::Left), 0, 0);
        assert!(app.choice_dropdown.is_none());
        assert!(app.new_session.is_some());
        assert_eq!(app.new_session.as_ref().unwrap().model, "model-00");
        assert!(app.pending_launches.is_empty());
    }

    #[test]
    fn settings_text_and_dropdown_values_share_explicit_save_in_private_home() {
        use std::os::unix::fs::PermissionsExt;
        // Re-exec with an isolated config so a regression cannot write user preferences.
        if std::env::var_os("DOXA_SETTINGS_STAGE_FIXTURE").is_none() {
            let home = tempfile::tempdir().unwrap();
            std::fs::set_permissions(home.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "ui::choice_dropdown::tests::settings_text_and_dropdown_values_share_explicit_save_in_private_home", "--nocapture"])
                .env("DOXA_SETTINGS_STAGE_FIXTURE", "1").env("DOXA_HOME", home.path())
                .env_remove("DOXA_LINGER_SECS").env_remove("DOXA_BACKGROUND")
                .output().unwrap();
            assert!(output.status.success(), "{}\n{}", String::from_utf8_lossy(&output.stdout), String::from_utf8_lossy(&output.stderr));
            return;
        }
        let path = settings::config_path().unwrap();
        let original = "linger_secs = 120\nunrelated = \"keep\"\n";
        std::fs::write(&path, original).unwrap();
        let mut app = settings_app();
        let menu = app.settings_menu.as_mut().unwrap();
        menu.selected = menu.rows.iter().position(|row| row.setting.key == "linger_secs").unwrap();
        key(&mut app, KeyCode::Enter);
        for _ in 0..3 { key(&mut app, KeyCode::Backspace); }
        for ch in "45".chars() { key(&mut app, KeyCode::Char(ch)); }
        key(&mut app, KeyCode::Enter);
        assert!(app.settings_menu.as_ref().unwrap().draft.is_none());
        assert_eq!(app.settings_menu.as_ref().unwrap().edits["linger_secs"].as_deref(), Some("45"));
        assert_eq!(std::fs::read_to_string(&path).unwrap(), original);
        key(&mut app, KeyCode::Char('4'));
        let menu = app.settings_menu.as_mut().unwrap();
        menu.selected = menu.rows.iter().position(|row| row.setting.key == "background").unwrap();
        key(&mut app, KeyCode::Enter);
        key(&mut app, KeyCode::End);
        key(&mut app, KeyCode::Enter);
        assert_eq!(std::fs::read_to_string(&path).unwrap(), original);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::CONTROL)));
        assert!(app.notice.starts_with("Settings saved"), "{}", app.notice);
        let saved: toml::Value = std::fs::read_to_string(&path).unwrap().parse().unwrap();
        assert_eq!(saved["linger_secs"].as_float(), Some(45.0), "{saved}");
        assert_eq!(saved["background"].as_str(), Some("transparent"));
        assert_eq!(saved["unrelated"].as_str(), Some("keep"));
        assert!(app.settings_menu.as_ref().unwrap().edits.is_empty());
    }

    #[test]
    fn new_session_cli_model_dropdown_populates_live_while_open_and_uses_reported_default() {
        for (index, engine) in [(0, launch::Engine::Codex), (1, launch::Engine::Claude)] {
            let mut app = settings_app();
            app.settings_menu = None;
            app.engine_selected = index;
            app.select_new_engine();
            let form = app.new_session.as_mut().unwrap();
            form.model.clear();
            form.catalog_pending = true;
            key(&mut app, KeyCode::Enter);
            assert!(paint(&app).contains("Loading models"));
            key(&mut app, KeyCode::Enter);
            assert!(app.choice_dropdown.is_some());
            assert!(app.pending_launches.is_empty());
            let (tx, rx) = std::sync::mpsc::sync_channel(1);
            app.new_session_catalog_pending = Some((engine, rx));
            tx.send(Ok(serde_json::json!({"models":["fixture-a","fixture-b","fixture-a","bad\nmodel",""],
                "capabilities":[{"model":"fixture-b","is_default":true}]}))).unwrap();
            assert!(app.poll_new_session_catalog());
            assert_eq!(app.new_session.as_ref().unwrap().models, ["fixture-a", "fixture-b"]);
            assert_eq!(app.new_session.as_ref().unwrap().model, "fixture-b");
            assert!(paint(&app).contains("fixture-a"));
            assert_eq!(app.choice_dropdown.as_ref().unwrap().choices, ["fixture-a", "fixture-b"]);
            key(&mut app, KeyCode::Home);
            key(&mut app, KeyCode::Enter);
            assert_eq!(app.new_session.as_ref().unwrap().model, "fixture-a");
            key(&mut app, KeyCode::Char('x'));
            assert_eq!(app.new_session.as_ref().unwrap().model, "fixture-a");
            assert!(app.pending_launches.is_empty());
        }
    }

    #[test]
    fn new_session_catalog_failure_and_engine_switch_never_reuse_old_model_choices() {
        let mut app = vendor_app();
        app.engine_selected = 0;
        app.select_new_engine();
        key(&mut app, KeyCode::Enter);
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        app.new_session_catalog_pending = Some((launch::Engine::Codex, rx));
        tx.send(Err("Catalog unavailable")).unwrap();
        assert!(app.poll_new_session_catalog());
        assert!(app.choice_dropdown.as_ref().unwrap().choices.is_empty());
        assert!(paint(&app).contains("No models"));
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        app.new_session_catalog_pending = Some((launch::Engine::Codex, rx));
        app.engine_selected = 1;
        app.select_new_engine();
        assert!(tx.send(Ok(serde_json::json!({"models":["stale-codex-model"]}))).is_err());
        assert!(app.choice_dropdown.is_none());
        assert!(app.new_session.as_ref().unwrap().models.is_empty());
        assert!(!app.poll_new_session_catalog());
    }
}
