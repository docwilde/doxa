//! A reported context window is independent of billing totals and LORE chars.
use ratatui::{style::{Color, Style}, text::{Line, Span}};
use serde_json::Value;
use super::{App, safe_label, theme};

#[derive(Clone, Debug, Default)]
pub(super) struct ContextUsage {
    pub(super) used: Option<u64>,
    pub(super) capacity: Option<u64>,
    pub(super) percentage: Option<f64>,
    invalid: bool,
    components: Vec<(String, u64)>,
    invalid_components: bool,
    components_unreported: bool,
}

impl ContextUsage {
    pub(super) fn reported(data: &Value, used: &str, capacity: &str, percentage: &str) -> Self {
        let integer = |key: &str| match data.get(key) {
            None | Some(Value::Null) => Ok(None),
            Some(value) => value.as_u64().map(Some).ok_or(()),
        };
        let parsed = (|| {
            let used = integer(used)?;
            let capacity = integer(capacity)?;
            let percentage = match data.get(percentage) {
                None | Some(Value::Null) => None,
                Some(value) => Some(value.as_f64().filter(|n| n.is_finite() && (0.0..=100.0).contains(n)).ok_or(())?),
            };
            if capacity == Some(0) || used.zip(capacity).is_some_and(|(u, c)| u > c) { return Err(()); }
            // Percentages may be rounded by the provider, but must describe the
            // same window as the supplied counts. Never silently clamp it.
            if let (Some(u), Some(c), Some(p)) = (used, capacity, percentage) {
                if (u as f64 / c as f64 * 100.0 - p).abs() > 1.0 { return Err(()); }
            }
            Ok(Self { used, capacity, percentage, ..Self::default() })
        })();
        parsed.unwrap_or_else(|_| Self { invalid: true, ..Self::default() })
    }

    pub(super) fn detail(&self, detail: &Value) -> Self {
        let mut result = if ["total_tokens", "max_tokens", "percentage"].iter().any(|key| detail.get(key).is_some()) {
            Self::reported(detail, "total_tokens", "max_tokens", "percentage")
        } else {
            Self { components: vec![], invalid_components: false, components_unreported: false, ..self.clone() }
        };
        if let Some(categories) = detail.get("categories").filter(|value| !value.is_null()) {
            let components = (|| {
                let (used, capacity) = result.used.zip(result.capacity)?;
                let rows = categories.as_array().filter(|rows| !rows.is_empty() && rows.len() <= 64)?;
                let mut names = std::collections::HashSet::new();
                let mut used_sum = 0_u64;
                let mut free_sum = 0_u64;
                let mut saw_free = false;
                let mut components = Vec::new();
                for row in rows {
                    let name = row["name"].as_str()?.trim();
                    if name.is_empty() || name.len() > 160 || name.chars().any(char::is_control)
                        || !names.insert(name.to_lowercase()) { return None; }
                    let tokens = row["tokens"].as_u64()?;
                    if tokens > capacity { return None; }
                    if matches!(name.to_lowercase().as_str(), "free space" | "free" | "available") {
                        free_sum = free_sum.checked_add(tokens)?;
                        saw_free = true;
                    } else {
                        used_sum = used_sum.checked_add(tokens)?;
                        components.push((safe_label(name), tokens));
                    }
                }
                // Partial or overlapping categories cannot divide a total bar.
                if used_sum != used || saw_free && free_sum != capacity.checked_sub(used)? { return None; }
                Some(components)
            })();
            if let Some(mut components) = components {
                // Keep distinct theme colors legible; the full provider list
                // remains in the detailed token table below this overview.
                if components.len() > 4 {
                    let remaining = components.drain(3..).map(|(_, tokens)| tokens).sum();
                    components.push(("Remaining reported components".into(), remaining));
                }
                result.components = components;
            }
            else { result.invalid_components = true; }
        } else if result.fill_percentage().is_some() {
            result.components_unreported = true;
        }
        result
    }

    pub(super) fn fill_percentage(&self) -> Option<f64> {
        self.used.zip(self.capacity).map(|(u, c)| u as f64 / c as f64 * 100.0).or(self.percentage)
    }

    pub(super) fn chip_label(&self) -> Option<String> {
        self.percentage.map(|p| format!("{p:.0}%"))
            .or_else(|| self.used.zip(self.capacity).map(|(u, c)| format!("{u}/{c}")))
    }

    fn lines(&self, width: usize, ascii: bool) -> Vec<Line<'static>> {
        let width = width.max(3);
        let heading = match self.fill_percentage() {
            Some(p) => format!("Context · {p:.1}% in use"),
            None if self.invalid => "Context usage unavailable · invalid report".into(),
            None => "Context usage not reported".into(),
        };
        let mut lines = crate::memory_menu::wrap_review(&heading, width).into_iter().map(Line::from).collect::<Vec<_>>();
        let counts = match (self.used, self.capacity) {
            (Some(u), Some(c)) => format!("{u} / {c} tokens"),
            (Some(u), None) => format!("{u} tokens · capacity not reported"),
            (None, Some(c)) => format!("Capacity {c} tokens · used not reported"),
            _ => "Token counts not reported".into(),
        };
        lines.extend(crate::memory_menu::wrap_review(&counts, width).into_iter().map(Line::from));
        let cells = width - 2;
        if let Some(percent) = self.fill_percentage() {
            let used_cells = self.used.zip(self.capacity)
                .map(|(u, c)| (u128::from(u) * cells as u128 / u128::from(c)) as usize)
                .unwrap_or_else(|| (percent * cells as f64 / 100.0).floor() as usize);
            if ascii {
                let mut spans = vec![Span::raw("[")];
                if self.components.is_empty() {
                    spans.push(Span::styled("#".repeat(used_cells), Style::default().fg(theme::ACCENT)));
                } else {
                    let capacity = self.capacity.unwrap();
                    let mut cumulative = 0_u64;
                    let mut previous = 0;
                    for (index, (_, tokens)) in self.components.iter().enumerate() {
                        cumulative += tokens; // validated against used <= capacity
                        let end = (u128::from(cumulative) * cells as u128 / u128::from(capacity)) as usize;
                        spans.push(Span::styled(component_marker(index).to_string().repeat(end - previous), Style::default().fg(component_color(index))));
                        previous = end;
                    }
                }
                spans.push(Span::styled(".".repeat(cells - used_cells), Style::default().fg(theme::MUTED)));
                spans.push(Span::raw("]"));
                lines.push(Line::from(spans));
            } else {
                let mut segments = Vec::new();
                if self.components.is_empty() {
                    segments.push((used_cells, theme::ACCENT));
                } else {
                    let capacity = self.capacity.unwrap();
                    let mut cumulative = 0_u64;
                    let mut previous = 0;
                    for (index, (_, tokens)) in self.components.iter().enumerate() {
                        cumulative += tokens; // validated against used <= capacity
                        let end = (u128::from(cumulative) * cells as u128 / u128::from(capacity)) as usize;
                        segments.push((end - previous, component_color(index)));
                        previous = end;
                    }
                }
                lines.push(super::bars::colored(cells, &segments, theme::BORDER));
            }
        } else {
            lines.push(super::bars::unknown(cells));
        }
        if self.fill_percentage().is_some() {
            if self.components.is_empty() {
                lines.extend(crate::memory_menu::wrap_review(if ascii { "# Used · . Available" } else { "Colored cells Used · dark cells Available" }, width).into_iter().map(Line::from));
            } else {
                for (index, (name, tokens)) in self.components.iter().enumerate() {
                    let label = if ascii { format!("{} {name}: {tokens} tokens", component_marker(index)) }
                        else { format!("{name}: {tokens} tokens") };
                    lines.extend(crate::memory_menu::wrap_review(&label, width).into_iter().map(|text| Line::styled(text, Style::default().fg(component_color(index)))));
                }
                let available = if ascii { format!(". Available: {} tokens", self.capacity.unwrap() - self.used.unwrap()) }
                    else { format!("Available: {} tokens", self.capacity.unwrap() - self.used.unwrap()) };
                lines.extend(crate::memory_menu::wrap_review(&available, width)
                    .into_iter().map(|text| Line::styled(text, Style::default().fg(theme::MUTED))));
            }
        } else {
            lines.push(Line::from("Fill not reported · bar is unavailable"));
        }
        if self.invalid_components {
            lines.extend(crate::memory_menu::wrap_review("Component breakdown unavailable · inconsistent or incomplete report", width).into_iter().map(Line::from));
        } else if self.components_unreported && self.fill_percentage().is_some() {
            lines.extend(crate::memory_menu::wrap_review("Component breakdown not reported by provider", width).into_iter().map(Line::from));
        }
        lines
    }
}

fn component_marker(index: usize) -> char {
    // Distinct ASCII markers remain readable without color or glyph coverage.
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-"[index] as char
}
fn component_color(index: usize) -> Color {
    [theme::ACCENT, theme::SUCCESS, theme::WARNING, theme::SECONDARY][index % 4]
}

impl App {
    /// Rebuild from the menu owner's telemetry and the current pane width. A
    /// retained menu never displays the next session's context after a switch.
    pub(super) fn context_info_lines(&self, width: usize) -> Vec<Line<'static>> {
        let Some(info) = self.chip_info.as_ref().filter(|info| info.kind == "context") else { return vec![]; };
        let Some((id, _)) = info.owner.as_ref().filter(|(id, _)| self.groups[self.active_group].active_id() == Some(id.as_str())) else {
            return vec![Line::from("Session changed; reopen context")];
        };
        let unknown = ContextUsage::default();
        let usage = self.session_telemetry.get(id).map(|t| &t.context_usage).unwrap_or(&unknown);
        let mut lines = usage.lines(width, self.preferences.value("context_grid") == "ascii");
        lines.push(Line::from(""));
        lines.extend(info.lines.iter().filter(|line| !line.starts_with("context  ") && !line.starts_with("in use   "))
            .flat_map(|line| crate::memory_menu::wrap_review(line, width.max(1))).map(Line::from));
        lines
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use crossterm::event::{Event, KeyCode, KeyEvent, KeyModifiers};
    use ratatui::{backend::TestBackend, layout::Rect, Terminal};

    fn text(lines: &[Line<'_>]) -> String {
        lines.iter().map(|line| line.spans.iter().map(|span| span.content.as_ref()).collect::<String>()).collect::<Vec<_>>().join("\n")
    }
    fn bar(usage: &ContextUsage, width: usize) -> String {
        text(&usage.lines(width, true)).lines().find(|line| line.starts_with('[')).unwrap().to_owned()
    }
    fn measured() -> ContextUsage {
        ContextUsage::default().detail(&json!({"total_tokens":300,"max_tokens":1000,"percentage":30,
            "categories":[{"name":"System","tokens":100},{"name":"Tools","tokens":200},{"name":"Free space","tokens":700}]}))
    }

    #[test]
    fn reported_components_divide_capacity_with_matching_legend_and_resize() {
        let usage = measured();
        assert_eq!(bar(&usage, 22), "[AABBBB..............]");
        assert_eq!(bar(&usage, 12), "[ABB.......]");
        let rendered = text(&usage.lines(40, true));
        for expected in ["30.0% in use", "300 / 1000 tokens", "A System: 100 tokens", "B Tools: 200 tokens", "Available: 700 tokens"] {
            assert!(rendered.contains(expected), "{expected}: {rendered}");
        }
        let colored = usage.lines(22, false);
        let line = colored.iter().find(|line| line.spans.first().is_some_and(|span| span.content == "[")).unwrap();
        assert_eq!(line.spans[1].style.bg, Some(theme::ACCENT));
        assert_eq!(line.spans[2].style.bg, Some(theme::SUCCESS));
        assert_eq!(line.spans[3].style.bg, Some(theme::BORDER));
        assert!(!text(&colored).contains('█') && !text(&colored).contains('░'));
    }

    #[test]
    fn unknown_invalid_and_reported_zero_are_distinct() {
        for report in [json!({}), json!({"lore_snapshot_chars":999,"project_cap_chars":1000}),
            json!({"total_tokens":400}), json!({"max_tokens":1000}),
            json!({"total_tokens":1001,"max_tokens":1000}), json!({"total_tokens":0,"max_tokens":0}),
            json!({"total_tokens":-1,"max_tokens":1000}), json!({"percentage":101}),
            json!({"percentage":"30"}), json!({"total_tokens":300,"max_tokens":1000,"percentage":60})] {
            let usage = ContextUsage::default().detail(&report);
            assert_eq!(bar(&usage, 12), "[          ]", "{report}");
            assert!(!text(&usage.lines(40, true)).contains("0.0% in use"), "{report}");
            assert!(text(&usage.lines(40, true)).contains("Fill not reported"), "{report}");
        }
        let zero = ContextUsage::default().detail(&json!({"total_tokens":0,"max_tokens":1000}));
        assert_eq!(bar(&zero, 12), "[..........]");
        assert!(text(&zero.lines(40, true)).contains("0.0% in use"));
        let percentage = ContextUsage::default().detail(&json!({"percentage":75}));
        assert_eq!(bar(&percentage, 22), "[###############.....]");
        assert!(text(&percentage.lines(40, true)).contains("Token counts not reported"));
    }

    #[test]
    fn inconsistent_components_do_not_replace_valid_total_or_invent_empty_space() {
        for categories in [json!([{"name":"System","tokens":299}]),
            json!([{"name":"System","tokens":300},{"name":"Available","tokens":701}]),
            json!([{"name":"System","tokens":300},{"name":"system","tokens":0}]),
            json!([{"name":"System","tokens":-1}]), json!([{"name":"System"}]),
            json!([{"name":"System\u{1b}","tokens":300}]), json!([]), json!(false)] {
            let usage = ContextUsage::default().detail(&json!({"total_tokens":300,"max_tokens":1000,"categories":categories}));
            assert_eq!(bar(&usage, 22), "[######..............]");
            assert!(text(&usage.lines(100, true)).contains("Component breakdown unavailable"));
        }
        let maximum = ContextUsage::default().detail(&json!({"total_tokens":u64::MAX/2,"max_tokens":u64::MAX}));
        assert_eq!(bar(&maximum, 12), "[####......]");
        let grouped = ContextUsage::default().detail(&json!({"total_tokens":5,"max_tokens":10,
            "categories":(0..5).map(|n| json!({"name":format!("Part {n}"),"tokens":1})).collect::<Vec<_>>()}));
        assert_eq!(grouped.components.len(), 4);
        assert_eq!(grouped.components[3], ("Remaining reported components".into(), 2));
    }

    #[test]
    fn telemetry_updates_clear_old_breakdown_and_invalid_counts() {
        let mut telemetry = super::super::session_telemetry::SessionTelemetry::default();
        telemetry.update_status(&json!({"ctx_tokens":300,"ctx_max_tokens":1000}));
        telemetry.update_context_detail(&json!({"categories":[{"name":"System","tokens":300}]}));
        assert_eq!(telemetry.context_usage.components.len(), 1);
        telemetry.update_turn(&json!({"ctx_percentage":50,"ctx_tokens":500,"ctx_max_tokens":1000}));
        assert!(telemetry.context_usage.components.is_empty());
        assert_eq!(bar(&telemetry.context_usage, 12), "[#####.....]");
        telemetry.update_status(&json!({"ctx_tokens":1001,"ctx_max_tokens":1000}));
        assert!(telemetry.context_tokens.is_none());
        assert!(telemetry.context.is_none());
        assert_eq!(bar(&telemetry.context_usage, 12), "[          ]");
    }

    #[test]
    fn context_menu_reply_and_bar_keep_owner_and_active_pane_on_resize() {
        let mut app = App::default();
        app.rail_visible = false;
        app.preferences.set_test("context_grid", "ascii");
        app.handle(Event::Resize(140, 38));
        for (id, used) in [("s", 300), ("other", 750)] {
            app.apply_daemon_frame(&json!({"type":"hello","session_id":id,"engine":"claude","model":"fixture",
                "ctx_tokens":used,"ctx_max_tokens":1000,"cwd":"/fixture"}));
        }
        app.groups[0].tabs = vec!["s".into(), "other".into()];
        app.groups[0].active = 0;
        app.active_group = 0;
        app.groups[1].tabs = vec!["other".into()];
        app.open_diagnostic("context");
        assert!(!app.apply_daemon_frame(&json!({"type":"context_detail","session_id":"other","ok":true,
            "detail":{"total_tokens":999,"max_tokens":1000}})));
        assert!(app.apply_daemon_frame(&json!({"type":"context_detail","session_id":"s","ok":true,
            "detail":{"total_tokens":300,"max_tokens":1000,"categories":[{"name":"System","tokens":100},
                {"name":"Tools","tokens":200},{"name":"Free","tokens":700}],"lore_snapshot_chars":321}})));
        let mut widths = Vec::new();
        for width in [140, 84] {
            app.handle(Event::Resize(width, 38));
            let area = app.active_chooser_rect().unwrap();
            let pane = app.layout(app.size).panes.unwrap()[app.active_group];
            assert!(area.x >= pane.x && area.right() <= pane.right());
            let expected = bar(&app.session_telemetry["s"].context_usage, usize::from(area.width - 3));
            widths.push(expected.len());
            let mut terminal = Terminal::new(TestBackend::new(width, 38)).unwrap();
            terminal.draw(|frame| app.draw(frame)).unwrap();
            let buffer = terminal.backend().buffer();
            let row = (area.x + 1..area.right() - 2).map(|x| buffer[(x, area.y + 3)].symbol()).collect::<String>();
            assert_eq!(row, expected);
            let lines = text(&app.context_info_lines(usize::from(area.width - 3)));
            assert!(lines.contains("30.0% in use"));
            assert!(lines.contains("321 chars"));
            assert!(!lines.contains("321 tokens"));
        }
        assert!(widths[1] < widths[0]);
        app.groups[0].active = 1;
        let hidden = text(&app.context_info_lines(40));
        assert_eq!(hidden, "Session changed; reopen context");
        assert!(!app.apply_daemon_frame(&json!({"type":"context_detail","session_id":"s","ok":true,
            "detail":{"total_tokens":1000,"max_tokens":1000}})));
        assert_eq!(app.session_telemetry["s"].context_tokens, Some(300));
        app.chip_info = None;
        assert!(!app.apply_daemon_frame(&json!({"type":"context_detail","session_id":"other","ok":true,
            "detail":{"total_tokens":999,"max_tokens":1000}})));
        assert_eq!(app.session_telemetry["other"].context_tokens, Some(750));
        app.groups[0].active = 0;
        app.open_diagnostic("context");
        app.chip_info.as_mut().unwrap().lines.extend((0..30).map(|n| format!("Detail row {n}")));
        app.handle(Event::Key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)));
        assert!(app.chip_info.as_ref().unwrap().scroll > 0);
        app.handle(Event::Resize(16, 8));
        assert!(app.chip_info.is_none());
        assert!(app.active_chooser_rect().is_none());
        assert_eq!(app.size, Rect::new(0, 0, 16, 8));
    }

    #[test]
    fn context_menu_draws_colored_cells_and_keeps_provider_composition() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(140, 38));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"claude","model":"fixture",
            "ctx_tokens":300,"ctx_max_tokens":1000,"cwd":"/fixture"}));
        app.open_diagnostic("context");
        assert!(app.apply_daemon_frame(&json!({"type":"context_detail","session_id":"s","ok":true,
            "detail":{"total_tokens":300,"max_tokens":1000,"categories":[{"name":"System","tokens":100},
                {"name":"Tools","tokens":200},{"name":"Free space","tokens":700}]}})));

        let area = app.active_chooser_rect().unwrap();
        let mut terminal = Terminal::new(TestBackend::new(140, 38)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let buffer = terminal.backend().buffer();
        let y = area.y + 3;
        assert_eq!(buffer[(area.x + 1, y)].symbol(), "[");
        assert_eq!(buffer[(area.right() - 3, y)].symbol(), "]");
        let cells = usize::from(area.width.saturating_sub(5));
        let system = cells * 100 / 1000;
        let tools = cells * 300 / 1000 - system;
        for index in 0..cells {
            let cell = &buffer[(area.x + 2 + index as u16, y)];
            assert_eq!(cell.symbol(), " ", "cell {index}");
            let expected = if index < system { theme::ACCENT }
                else if index < system + tools { theme::SUCCESS }
                else { theme::BORDER };
            assert_eq!(cell.bg, expected, "cell {index}");
        }
        assert!(text(&app.context_info_lines(usize::from(area.width - 3))).contains("Tools: 200 tokens"));
    }

    #[test]
    fn context_totals_without_provider_categories_do_not_claim_a_composition() {
        let usage = ContextUsage::default().detail(&json!({"total_tokens":300,"max_tokens":1000}));
        let rendered = text(&usage.lines(80, false));
        assert!(rendered.contains("30.0% in use"));
        assert!(rendered.contains("Component breakdown not reported by provider"));
        assert!(!rendered.contains("System:") && !rendered.contains("Tools:"));
    }
}
