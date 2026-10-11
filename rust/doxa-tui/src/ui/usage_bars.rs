//! Read-only provider quota bars for the session usage submenu.
use ratatui::{style::Color, text::Line};
use time::{format_description::well_known::Rfc3339, OffsetDateTime};

use super::{session_telemetry::QuotaWindow, App, theme};

impl App {
    pub(super) fn usage_info_lines(&self, width: usize) -> Vec<Line<'static>> {
        let Some(info) = self.chip_info.as_ref().filter(|info| info.kind == "usage") else { return vec![]; };
        let Some((id, _)) = info.owner.as_ref().filter(|(id, _)| {
            self.groups[self.active_group].active_id() == Some(id.as_str())
        }) else {
            return vec![Line::from("Session changed; reopen usage")];
        };
        let width = width.max(1);
        let mut lines = info.lines.iter()
            .flat_map(|line| crate::memory_menu::wrap_review(line, width))
            .map(Line::from).collect::<Vec<_>>();
        lines.push(Line::from(""));
        let telemetry = self.session_telemetry.get(id);
        match telemetry.and_then(|value| value.billing_mode.as_deref()) {
            Some("subscription") => {
                append_window(&mut lines, width, "Session", telemetry.and_then(|value| value.session_quota.as_ref()));
                append_window(&mut lines, width, "Weekly", telemetry.and_then(|value| value.weekly_quota.as_ref()));
            }
            Some("api") => lines.extend(crate::memory_menu::wrap_review("Subscription quota windows are not reported for API billing", width).into_iter().map(Line::from)),
            _ => lines.extend(crate::memory_menu::wrap_review("Subscription quota windows not reported by provider", width).into_iter().map(Line::from)),
        }
        lines
    }
}

fn append_window(lines: &mut Vec<Line<'static>>, width: usize, label: &str, quota: Option<&QuotaWindow>) {
    let Some(quota) = quota else {
        lines.extend(crate::memory_menu::wrap_review(&format!("{label} quota not reported by provider"), width).into_iter().map(Line::from));
        return;
    };
    let percent = percent_label(quota.percent);
    let stale = if quota.stale { " · stale report" } else { "" };
    lines.extend(crate::memory_menu::wrap_review(&format!("{label} quota · {} · {percent}% used{stale}", quota.duration), width).into_iter().map(Line::from));
    let cells = width.saturating_sub(2);
    let used = (quota.percent * cells as f64 / 100.0).floor() as usize;
    lines.push(super::bars::colored(cells, &[(used, quota_color(quota.percent))], theme::BORDER));
    let reset = quota.resets_at.and_then(|stamp| i64::try_from(stamp).ok())
        .and_then(|stamp| OffsetDateTime::from_unix_timestamp(stamp).ok())
        .and_then(|time| time.format(&Rfc3339).ok());
    let reset = reset.map(|time| format!("Reset at {time}"))
        .unwrap_or_else(|| "Reset time not reported by provider".into());
    lines.extend(crate::memory_menu::wrap_review(&reset, width).into_iter().map(Line::from));
}

fn percent_label(percent: f64) -> String {
    if percent.fract() == 0.0 { format!("{percent:.0}") } else { format!("{percent:.1}") }
}

fn quota_color(percent: f64) -> Color {
    if percent > 90.0 { theme::ERROR }
    else if percent > 66.0 { theme::WARNING }
    else { theme::SUCCESS }
}
