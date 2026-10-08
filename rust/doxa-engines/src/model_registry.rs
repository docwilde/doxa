//! Exact engine/model facts independent of provider catalog availability.
//!
//! A catalog answers which models a connected account can use. This registry
//! answers only what DOXA can substantiate about an exact model ID. Unknown
//! facts never turn into defaults or authorize a model, effort, or budget.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Thinking {
    Unsupported,
    Optional,
    Mandatory,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Provenance {
    Unknown,
    /// A hand-maintained fact checked against the named source on this date.
    Static { source: &'static str, as_of: &'static str },
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Fact<T> {
    pub value: Option<T>,
    pub provenance: Provenance,
}

impl<T> Fact<T> {
    pub const fn unknown() -> Self {
        Self { value: None, provenance: Provenance::Unknown }
    }

    pub const fn static_value(value: T, source: &'static str, as_of: &'static str) -> Self {
        Self { value: Some(value), provenance: Provenance::Static { source, as_of } }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ModelFacts {
    pub context_window: Fact<u64>,
    /// USD per million fresh input tokens. Cached-token discounts are omitted.
    pub input_usd_per_million: Fact<f64>,
    pub output_usd_per_million: Fact<f64>,
    pub thinking: Fact<Thinking>,
}

impl ModelFacts {
    pub const fn unknown() -> Self {
        Self {
            context_window: Fact::unknown(),
            input_usd_per_million: Fact::unknown(),
            output_usd_per_million: Fact::unknown(),
            thinking: Fact::unknown(),
        }
    }

    /// A budget may consume a price only when both exact-model fields have a
    /// source and date. The caller retains its independent admission checks.
    pub fn priced_pair(self) -> Option<(f64, f64, &'static str, &'static str)> {
        let input = self.input_usd_per_million.value?;
        let output = self.output_usd_per_million.value?;
        let (Provenance::Static { source: input_source, as_of: input_date }, Provenance::Static { source: output_source, as_of: output_date }) =
            (self.input_usd_per_million.provenance, self.output_usd_per_million.provenance)
        else { return None; };
        let dated = |date: &str| {
            let bytes = date.as_bytes();
            bytes.len() == 10 && bytes[4] == b'-' && bytes[7] == b'-'
                && bytes.iter().enumerate().all(|(index, byte)| index == 4 || index == 7 || byte.is_ascii_digit())
        };
        (input_source == output_source && input_date == output_date
            && input_source.starts_with("https://") && input_source.len() > "https://".len()
            && dated(input_date) && input.is_finite() && output.is_finite() && input >= 0.0 && output >= 0.0)
            .then_some((input, output, input_source, input_date))
    }
}

/// Existing native budget rates, checked against the linked provider pages
/// on 2026-09-30. No alias, family, or substring matching is allowed.
pub fn lookup(engine: &str, model: &str) -> ModelFacts {
    let (input, output, source) = match (engine, model) {
        ("codex", "gpt-6-astra") => (20.0, 100.0, "https://developers.openai.com/api/docs/pricing"),
        ("codex", "gpt-5.6-sol") => (8.0, 40.0, "https://developers.openai.com/api/docs/pricing"),
        ("codex", "gpt-5.6-terra") => (4.0, 24.0, "https://developers.openai.com/api/docs/pricing"),
        ("codex", "gpt-5.6-luna") => (0.4, 2.4, "https://developers.openai.com/api/docs/pricing"),
        ("codex", "gpt-5.5") => (12.5, 75.0, "https://developers.openai.com/api/docs/pricing"),
        ("codex", "gpt-5.3-codex") => (3.5, 28.0, "https://developers.openai.com/api/docs/pricing"),
        ("deepseek", "deepseek-flash") => (0.3, 1.2, "https://api-docs.deepseek.com/quick_start/pricing"),
        ("deepseek", "deepseek-v4-pro") => (1.32, 3.96, "https://api-docs.deepseek.com/quick_start/pricing"),
        ("glm", "glm-5.3-flash") => (0.15, 0.5, "https://docs.z.ai/guides/overview/pricing"),
        ("glm", "glm-5.3-flashx") => (0.37, 1.25, "https://docs.z.ai/guides/overview/pricing"),
        ("glm", "glm-5.3" | "glm-5.2" | "glm-5.1") => (1.4, 4.4, "https://docs.z.ai/guides/overview/pricing"),
        ("glm", "glm-5") => (1.0, 3.2, "https://docs.z.ai/guides/overview/pricing"),
        ("glm", "glm-4.7" | "glm-4.6" | "glm-4.5") => (0.6, 2.2, "https://docs.z.ai/guides/overview/pricing"),
        ("glm", "glm-4.7-flashx") => (0.07, 0.4, "https://docs.z.ai/guides/overview/pricing"),
        ("glm", "glm-4.5-air") => (0.2, 1.1, "https://docs.z.ai/guides/overview/pricing"),
        _ => return ModelFacts::unknown(),
    };
    ModelFacts {
        input_usd_per_million: Fact::static_value(input, source, "2026-09-30"),
        output_usd_per_million: Fact::static_value(output, source, "2026-09-30"),
        ..ModelFacts::unknown()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lookup_uses_exact_engine_and_model_without_inferred_capabilities() {
        let known = lookup("codex", "gpt-5.6-sol");
        assert_eq!(known.priced_pair(), Some((8.0, 40.0, "https://developers.openai.com/api/docs/pricing", "2026-09-30")));
        assert_eq!(known.context_window, Fact::unknown());
        assert_eq!(known.thinking, Fact::unknown());
        for (engine, model) in [("claude", "gpt-5.6-sol"), ("codex", "gpt-5.6-sol-latest"), ("codex", "GPT-5.6-SOL")] {
            assert_eq!(lookup(engine, model), ModelFacts::unknown());
            assert!(lookup(engine, model).priced_pair().is_none());
        }
    }

    #[test]
    fn incomplete_or_unattributed_price_cannot_authorize_budget() {
        let mut facts = lookup("glm", "glm-5.3-flash");
        facts.output_usd_per_million = Fact::unknown();
        assert!(facts.priced_pair().is_none());
        facts.output_usd_per_million = Fact { value: Some(0.5), provenance: Provenance::Unknown };
        assert!(facts.priced_pair().is_none());
        facts.output_usd_per_million = Fact::static_value(0.5, "https://docs.z.ai/guides/overview/pricing", "2026-09-29");
        assert!(facts.priced_pair().is_none());
        facts.output_usd_per_million = Fact::static_value(0.5, "https://docs.z.ai/guides/overview/pricing", "2026-09-30");
        facts.input_usd_per_million = Fact::static_value(0.15, "", "2026-09-30");
        facts.output_usd_per_million = Fact::static_value(0.5, "", "2026-09-30");
        assert!(facts.priced_pair().is_none());
        facts.input_usd_per_million = Fact::static_value(0.15, "https://docs.z.ai/guides/overview/pricing", "undated");
        facts.output_usd_per_million = Fact::static_value(0.5, "https://docs.z.ai/guides/overview/pricing", "undated");
        assert!(facts.priced_pair().is_none());
    }
}
