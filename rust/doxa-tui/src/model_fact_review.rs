//! Read-only, operator-selected review of the exact static model facts.
//! This module never feeds model selection, effort choices, or budget admission.

use doxa_engines::model_registry::{self, Fact, Provenance, Thinking};
use time::Date;

fn exact_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 100
        && id.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
}

fn review_date(value: &str) -> Result<Date, String> {
    if value.len() != 10 || !value.bytes().enumerate().all(|(index, byte)| {
        if index == 4 || index == 7 { byte == b'-' } else { byte.is_ascii_digit() }
    }) {
        return Err("review cutoff must be a YYYY-MM-DD date".into());
    }
    let year = value[..4].parse::<i32>().map_err(|_| "invalid review year")?;
    let month = value[5..7].parse::<u8>().map_err(|_| "invalid review month")?;
    let day = value[8..].parse::<u8>().map_err(|_| "invalid review day")?;
    let month = time::Month::try_from(month)
        .map_err(|_| "review cutoff must be a valid YYYY-MM-DD date")?;
    Date::from_calendar_date(year, month, day)
        .map_err(|_| "review cutoff must be a valid YYYY-MM-DD date".into())
}

fn row(
    lines: &mut Vec<String>, label: &str, value: Option<String>, provenance: Provenance,
    before: Option<Date>, review_count: &mut usize, unknown_count: &mut usize,
) {
    match (value, provenance) {
        (Some(value), Provenance::Static { source, as_of }) => {
            let candidate = before.is_some_and(|cutoff| review_date(as_of).map_or(true, |checked| checked < cutoff));
            *review_count += usize::from(candidate);
            lines.push(format!("{label}: {value} · checked {as_of}{}", if candidate { " · REVIEW" } else { "" }));
            lines.push(format!("  source: {source}"));
        }
        _ => {
            *unknown_count += 1;
            lines.push(format!("{label}: unknown"));
        }
    }
}

fn value<T: ToString>(fact: Fact<T>) -> Option<String> { fact.value.map(|value| value.to_string()) }

/// Show evidence for one exact registry key. `review_before` is an operator's
/// sorting choice, not a freshness rule or a live-provider verification.
pub fn report(engine: &str, model: &str, review_before: Option<&str>) -> Result<String, String> {
    if !exact_id(engine) || !exact_id(model) {
        return Err("engine and model must be exact IDs (1–100 ASCII letters, digits, '.', '_' or '-')".into());
    }
    let before = review_before.map(review_date).transpose()?;
    let facts = model_registry::lookup(engine, model);
    let mut lines = vec![
        format!("Model facts · {engine}/{model}"),
        "Static evidence only; the live provider catalog controls availability and effort choices.".into(),
        "A checked date does not verify current availability, price, billing tier, or provider charge.".into(),
    ];
    if let Some(date) = review_before {
        lines.push(format!("Review facts checked before {date} (operator-selected cutoff; no automatic expiry)."));
    }
    lines.push(String::new());
    let mut review_count = 0;
    let mut unknown_count = 0;
    row(&mut lines, "Context window", value(facts.context_window).map(|v| format!("{v} tokens")),
        facts.context_window.provenance, before, &mut review_count, &mut unknown_count);
    let thinking = facts.thinking.value.map(|v| match v {
        Thinking::Unsupported => "unsupported", Thinking::Optional => "optional", Thinking::Mandatory => "mandatory",
    }.to_owned());
    row(&mut lines, "Thinking", thinking, facts.thinking.provenance, before, &mut review_count, &mut unknown_count);
    for (label, fact) in [
        ("Standard API input", facts.input_usd_per_million),
        ("Standard API output", facts.output_usd_per_million),
        ("Native budget input bound", facts.budget_input_usd_per_million),
        ("Native budget output bound", facts.budget_output_usd_per_million),
    ] {
        row(&mut lines, label, value(fact).map(|v| format!("${v}/million tokens")),
            fact.provenance, before, &mut review_count, &mut unknown_count);
    }
    lines.push(String::new());
    lines.push(format!("{} review candidates · {} unknown fields", review_count, unknown_count));
    lines.push("To refresh a fact, check its exact model ID against its primary source and update the registry in a reviewed code change.".into());
    Ok(lines.join("\n"))
}

#[cfg(test)]
mod tests {
    use super::report;

    #[test]
    fn review_is_field_level_and_operator_selected() {
        let marked = report("deepseek", "deepseek-flash", Some("2026-10-09")).unwrap();
        assert!(marked.contains("Context window: 1048576 tokens · checked 2026-10-08 · REVIEW"));
        assert!(marked.contains("source: https://api-docs.deepseek.com/api/list-models/"));
        assert!(marked.contains("Standard API input: $0.3/million tokens · checked 2026-09-30 · REVIEW"));
        assert!(marked.contains("6 review candidates · 0 unknown fields"));

        let same_day = report("deepseek", "deepseek-flash", Some("2026-09-30")).unwrap();
        assert!(same_day.contains("0 review candidates · 0 unknown fields"));
        assert!(!same_day.contains(" · REVIEW"));
        let no_cutoff = report("deepseek", "deepseek-flash", None).unwrap();
        assert!(no_cutoff.contains("0 review candidates · 0 unknown fields"));
    }

    #[test]
    fn missing_facts_remain_unknown_and_no_alias_is_inherited() {
        let sol = report("codex", "gpt-5.6-sol", Some("2026-10-10")).unwrap();
        assert!(sol.contains("Native budget input bound: unknown"));
        assert!(sol.contains("Native budget output bound: unknown"));
        assert!(sol.contains("4 review candidates · 2 unknown fields"));
        let alias = report("codex", "gpt-5.6-sol-latest", Some("2026-10-10")).unwrap();
        assert!(alias.contains("0 review candidates · 6 unknown fields"));
        assert!(!alias.contains("developers.openai.com"));
    }

    #[test]
    fn malformed_cutoff_and_control_character_ids_are_refused() {
        for date in ["2026-1-09", "2026-02-30", "2026-13-01", "2026-10-09\n"] {
            assert!(report("codex", "gpt-6-astra", Some(date)).is_err());
        }
        assert!(report("codex", "gpt-6-astra\x1b[0m", None).is_err());
        assert!(report("codex", "", None).is_err());
    }
}
