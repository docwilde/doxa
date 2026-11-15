//! Exact engine/model facts independent of provider catalog availability.
//!
//! A catalog answers which models a connected account can use. This registry
//! answers only what DOXA can substantiate about an exact model ID. Unknown
//! facts never turn into defaults or authorize a model, effort, budget or
//! image attachment.

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
    /// Standard API USD per million fresh input tokens; not a budget bound.
    pub input_usd_per_million: Fact<f64>,
    pub output_usd_per_million: Fact<f64>,
    /// Upper rates for native token-only budget admission, when all supported
    /// service tiers and cache-write rates have a documented bound.
    pub budget_input_usd_per_million: Fact<f64>,
    pub budget_output_usd_per_million: Fact<f64>,
    pub thinking: Fact<Thinking>,
    /// Whether the exact model accepts inline image content in a chat request.
    /// `Some(false)` is a documented refusal; unknown never means supported.
    pub image_input: Fact<bool>,
    /// Documented upper bound of input tokens one image can consume, billed
    /// with text input tokens. Unknown refuses image attachments.
    pub image_tokens_per_image: Fact<u64>,
}

/// Exact-model evidence admitting image attachments, with its own provenance.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ImageBound {
    pub tokens_per_image: u64,
    pub source: &'static str,
    pub as_of: &'static str,
}

/// Why an image attachment is refused before submission.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ImageRefusal {
    /// No sourced fact says this exact model accepts images.
    Unknown,
    /// A primary source says this exact model does not accept images.
    Unsupported { source: &'static str, as_of: &'static str },
    /// The model accepts images, but no per-image token bound is published.
    UnpricedImages { source: &'static str, as_of: &'static str },
    /// The model has no complete, attributed token price bound.
    UnpricedTokens,
}

impl ImageRefusal {
    pub fn message(self, engine: &str, model: &str) -> String {
        match self {
            Self::Unknown => format!("Image input is not established for {engine} model {model}; attachment refused before submission"),
            Self::Unsupported { source, as_of } =>
                format!("{engine} model {model} does not accept image input ({source}, checked {as_of}); attachment refused before submission"),
            Self::UnpricedImages { source, as_of } =>
                format!("{engine} model {model} accepts images ({source}, checked {as_of}), but its per-image token charge is unpublished; DOXA refuses rather than under-count"),
            Self::UnpricedTokens => format!("{engine} model {model} has no complete price bound; image attachment refused before submission"),
        }
    }
}

impl ModelFacts {
    pub const fn unknown() -> Self {
        Self {
            context_window: Fact::unknown(),
            input_usd_per_million: Fact::unknown(),
            output_usd_per_million: Fact::unknown(),
            budget_input_usd_per_million: Fact::unknown(),
            budget_output_usd_per_million: Fact::unknown(),
            thinking: Fact::unknown(),
            image_input: Fact::unknown(),
            image_tokens_per_image: Fact::unknown(),
        }
    }

    /// Images are admitted only with a sourced acceptance fact, a sourced
    /// positive per-image token bound and a complete budget price pair. Any
    /// unknown field refuses; a family name never supplies a missing fact.
    pub fn image_admission(self) -> Result<ImageBound, ImageRefusal> {
        let (Some(accepts), Provenance::Static { source, as_of }) = (self.image_input.value, self.image_input.provenance)
            else { return Err(ImageRefusal::Unknown); };
        if !accepts { return Err(ImageRefusal::Unsupported { source, as_of }); }
        let bound = match (self.image_tokens_per_image.value, self.image_tokens_per_image.provenance) {
            (Some(tokens), Provenance::Static { source, as_of }) if tokens > 0 && source.starts_with("https://") =>
                ImageBound { tokens_per_image: tokens, source, as_of },
            _ => return Err(ImageRefusal::UnpricedImages { source, as_of }),
        };
        self.budget_bound_pair().ok_or(ImageRefusal::UnpricedTokens)?;
        Ok(bound)
    }

    fn attributed_pair(input_fact: Fact<f64>, output_fact: Fact<f64>) -> Option<(f64, f64, &'static str, &'static str)> {
        let input = input_fact.value?;
        let output = output_fact.value?;
        let (Provenance::Static { source: input_source, as_of: input_date }, Provenance::Static { source: output_source, as_of: output_date }) =
            (input_fact.provenance, output_fact.provenance)
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


    pub fn priced_pair(self) -> Option<(f64, f64, &'static str, &'static str)> {
        Self::attributed_pair(self.input_usd_per_million, self.output_usd_per_million)
    }

    /// Unknown upper bound fails native priced-budget admission closed.
    pub fn budget_bound_pair(self) -> Option<(f64, f64, &'static str, &'static str)> {
        Self::attributed_pair(self.budget_input_usd_per_million, self.budget_output_usd_per_million)
    }
}

/// Exact-model API price facts. DeepSeek/GLM rates were rechecked 2026-10-10;
/// OpenAI rows were refreshed 2026-10-09. No aliases are inherited.
pub fn lookup(engine: &str, model: &str) -> ModelFacts {
    let (input, output, source, date) = match (engine, model) {
        ("codex", "gpt-6-astra") => (10.0, 50.0, "https://developers.openai.com/api/docs/pricing", "2026-10-09"),
        ("codex", "gpt-5.6-sol") => (4.0, 20.0, "https://developers.openai.com/api/docs/pricing", "2026-10-09"),
        ("codex", "gpt-5.6-terra") => (2.0, 12.0, "https://developers.openai.com/api/docs/pricing", "2026-10-09"),
        ("codex", "gpt-5.6-luna") => (0.2, 1.2, "https://developers.openai.com/api/docs/pricing", "2026-10-09"),
        ("codex", "gpt-5.5") => (5.0, 30.0, "https://developers.openai.com/api/docs/pricing", "2026-10-09"),
        ("codex", "gpt-5.3-codex") => (1.75, 14.0, "https://developers.openai.com/api/docs/pricing", "2026-10-09"),
        ("deepseek", "deepseek-flash") => (0.3, 1.2, "https://api-docs.deepseek.com/quick_start/pricing", "2026-10-10"),
        ("deepseek", "deepseek-v4-pro") => (1.32, 3.96, "https://api-docs.deepseek.com/quick_start/pricing", "2026-10-10"),
        ("glm", "glm-5.3-flash") => (0.15, 0.5, "https://docs.z.ai/guides/overview/pricing", "2026-10-10"),
        ("glm", "glm-5.3-flashx") => (0.37, 1.25, "https://docs.z.ai/guides/overview/pricing", "2026-10-10"),
        ("glm", "glm-5.3" | "glm-5.2" | "glm-5.1") => (1.4, 4.4, "https://docs.z.ai/guides/overview/pricing", "2026-10-10"),
        ("glm", "glm-5") => (1.0, 3.2, "https://docs.z.ai/guides/overview/pricing", "2026-10-10"),
        ("glm", "glm-4.7" | "glm-4.6" | "glm-4.5") => (0.6, 2.2, "https://docs.z.ai/guides/overview/pricing", "2026-10-10"),
        ("glm", "glm-4.7-flashx") => (0.07, 0.4, "https://docs.z.ai/guides/overview/pricing", "2026-10-10"),
        ("glm", "glm-4.5-air") => (0.2, 1.1, "https://docs.z.ai/guides/overview/pricing", "2026-10-10"),
        _ => return ModelFacts::unknown(),
    };
    let mut facts = ModelFacts {
        input_usd_per_million: Fact::static_value(input, source, date),
        output_usd_per_million: Fact::static_value(output, source, date),
        budget_input_usd_per_million: Fact::static_value(input, source, date),
        budget_output_usd_per_million: Fact::static_value(output, source, date),
        ..ModelFacts::unknown()
    };
    if engine == "codex" {
        // API Standard rates are shown in the picker. Budget bounds must also
        // cover long context, Fast/Ultrafast, cache writes and the published
        // 10% regional uplift because Codex turn usage lacks service-tier data.
        // GPT-5.6 Sol's preview Ultrafast rate and GPT-5.5's Fast long-context
        // rate are unpublished; both remain unpriced for native budgets.
        let bound = match model {
            // Ultrafast long-context cache write/output with regional uplift.
            "gpt-6-astra" => Some((165.0, 495.0)),
            // Fast long-context cache write/output with regional uplift.
            "gpt-5.6-terra" => Some((11.0, 39.6)),
            "gpt-5.6-luna" => Some((1.1, 3.96)),
            // Specialized Codex Fast rate; no cache-write or long-context tier
            // is listed. FedRAMP's 10% uplift has no model release-date cutoff.
            "gpt-5.3-codex" => Some((3.85, 30.8)),
            _ => None,
        };
        facts.budget_input_usd_per_million = bound.map(|(input, _)| Fact::static_value(input, "https://developers.openai.com/api/docs/pricing", "2026-10-09")).unwrap_or(Fact::unknown());
        facts.budget_output_usd_per_million = bound.map(|(_, output)| Fact::static_value(output, "https://developers.openai.com/api/docs/pricing", "2026-10-09")).unwrap_or(Fact::unknown());
    }
    // OpenAI's API model pages specify the window for these exact IDs. This
    // describes the model, not a Codex account's effective session allocation.
    let codex_model_page = match (engine, model) {
        ("codex", "gpt-6-astra") => Some(("https://developers.openai.com/api/docs/models/gpt-6-astra", 1_050_000)),
        ("codex", "gpt-5.6-sol") => Some(("https://developers.openai.com/api/docs/models/gpt-5.6-sol", 1_050_000)),
        ("codex", "gpt-5.6-terra") => Some(("https://developers.openai.com/api/docs/models/gpt-5.6-terra", 1_050_000)),
        ("codex", "gpt-5.6-luna") => Some(("https://developers.openai.com/api/docs/models/gpt-5.6-luna", 1_050_000)),
        ("codex", "gpt-5.5") => Some(("https://developers.openai.com/api/docs/models/gpt-5.5", 1_050_000)),
        ("codex", "gpt-5.3-codex") => Some(("https://developers.openai.com/api/docs/models/gpt-5.3-codex", 400_000)),
        _ => None,
    };
    if let Some((source, window)) = codex_model_page {
        facts.context_window = Fact::static_value(window, source, "2026-10-09");
    }
    if (engine, model) == ("codex", "gpt-6-astra") {
        facts.thinking = Fact::static_value(
            Thinking::Mandatory, "https://developers.openai.com/api/docs/guides/reasoning", "2026-10-09"
        );
    } else if engine == "codex" && matches!(model, "gpt-5.6-sol" | "gpt-5.6-terra" | "gpt-5.6-luna" | "gpt-5.5") {
        if let Some((source, _)) = codex_model_page {
            facts.thinking = Fact::static_value(Thinking::Optional, source, "2026-10-09");
        }
    }
    // DeepSeek's documented /models example gives an exact integer for these
    // IDs. Its "1M" marketing label alone would not distinguish 1,000,000
    // from 1,048,576 tokens.
    if matches!((engine, model), ("deepseek", "deepseek-flash" | "deepseek-v4-pro")) {
        facts.context_window = Fact::static_value(
            1_048_576, "https://api-docs.deepseek.com/api/list-models/", "2026-10-08"
        );
        facts.thinking = Fact::static_value(
            Thinking::Optional, "https://api-docs.deepseek.com/quick_start/pricing/", "2026-10-08"
        );
    }
    // The exact GLM-5.3 IDs below document that thinking cannot be disabled.
    // Other named GLM models in the thinking guide permit disabling it. The
    // GLM context labels are rounded ("1M", "200K", "128K"), so a precise
    // integer window remains unknown until an exact value is published.
    facts.thinking = match (engine, model) {
        ("glm", "glm-5.3") => Fact::static_value(
            Thinking::Mandatory, "https://docs.z.ai/guides/llm/glm-5.3", "2026-10-08"
        ),
        ("glm", "glm-5.3-flash" | "glm-5.3-flashx") => Fact::static_value(
            Thinking::Mandatory, "https://docs.z.ai/guides/vlm/glm-5.3-flash", "2026-10-08"
        ),
        ("glm", "glm-5.2" | "glm-5.1" | "glm-5" | "glm-4.7") => Fact::static_value(
            Thinking::Optional, "https://docs.z.ai/guides/capabilities/thinking-mode", "2026-10-08"
        ),
        _ => facts.thinking,
    };
    // Image input, checked 2026-11-14 against each provider's page for the
    // exact ID. DeepSeek's vision guide names `deepseek-flash`, documents
    // OpenAI-compatible `image_url` data URLs, says image tokens are billed
    // with text tokens and caps each image at 1024 tokens after resizing. Its
    // pricing table marks `deepseek-v4-pro` Vision "Not supported". Z.AI's
    // GLM-5.3-Flash page lists both IDs with image input through `image_url`
    // but publishes no per-image token count, so that bound stays unknown.
    match (engine, model) {
        ("deepseek", "deepseek-flash") => {
            facts.image_input = Fact::static_value(true, "https://api-docs.deepseek.com/guides/vision", "2026-11-14");
            facts.image_tokens_per_image = Fact::static_value(1024, "https://api-docs.deepseek.com/guides/vision", "2026-11-14");
        }
        ("deepseek", "deepseek-v4-pro") => {
            facts.image_input = Fact::static_value(false, "https://api-docs.deepseek.com/quick_start/pricing/", "2026-11-14");
        }
        ("glm", "glm-5.3-flash" | "glm-5.3-flashx") => {
            facts.image_input = Fact::static_value(true, "https://docs.z.ai/guides/vlm/glm-5.3-flash", "2026-11-14");
        }
        _ => {}
    }
    facts
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lookup_uses_exact_engine_and_model_without_inferred_capabilities() {
        let known = lookup("codex", "gpt-5.6-sol");
        assert_eq!(known.priced_pair(), Some((4.0, 20.0, "https://developers.openai.com/api/docs/pricing", "2026-10-09")));
        assert!(known.budget_bound_pair().is_none());
        assert_eq!(known.context_window.value, Some(1_050_000));
        assert_eq!(known.thinking.value, Some(Thinking::Optional));
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

    #[test]
    fn openai_budget_bounds_are_separate_from_standard_api_prices() {
        let source = "https://developers.openai.com/api/docs/pricing";
        for (model, standard, bound) in [
            ("gpt-6-astra", (10.0, 50.0), Some((165.0, 495.0))),
            ("gpt-5.6-sol", (4.0, 20.0), None),
            ("gpt-5.6-terra", (2.0, 12.0), Some((11.0, 39.6))),
            ("gpt-5.6-luna", (0.2, 1.2), Some((1.1, 3.96))),
            ("gpt-5.5", (5.0, 30.0), None),
            ("gpt-5.3-codex", (1.75, 14.0), Some((3.85, 30.8))),
        ] {
            let facts = lookup("codex", model);
            assert_eq!(facts.priced_pair(), Some((standard.0, standard.1, source, "2026-10-09")));
            assert_eq!(facts.budget_bound_pair(), bound.map(|pair| (pair.0, pair.1, source, "2026-10-09")));
        }
        let mut facts = lookup("codex", "gpt-6-astra");
        facts.budget_output_usd_per_million = Fact::unknown();
        assert!(facts.budget_bound_pair().is_none());
        facts.budget_output_usd_per_million = Fact::static_value(495.0, source, "2026-10-08");
        assert!(facts.budget_bound_pair().is_none());
    }

    #[test]
    fn dated_capabilities_are_exact_and_do_not_change_price_admission() {
        let flash = lookup("deepseek", "deepseek-flash");
        assert_eq!(flash.context_window, Fact::static_value(
            1_048_576, "https://api-docs.deepseek.com/api/list-models/", "2026-10-08"
        ));
        assert_eq!(flash.thinking.value, Some(Thinking::Optional));
        assert_eq!(lookup("deepseek", "deepseek-v4-pro").context_window, flash.context_window);
        assert_eq!(lookup("glm", "glm-5.3-flash").thinking.value, Some(Thinking::Mandatory));
        assert_eq!(lookup("glm", "glm-5.3-flashx").thinking.value, Some(Thinking::Mandatory));
        assert_eq!(lookup("glm", "glm-5.3").thinking.value, Some(Thinking::Mandatory));
        assert_eq!(lookup("glm", "glm-5.2").thinking.value, Some(Thinking::Optional));
        assert_eq!(lookup("glm", "glm-5.3").context_window, Fact::unknown());
        assert_eq!(lookup("glm", "glm-5.3-flash").context_window, Fact::unknown());
        for (engine, model) in [
            ("deepseek", "deepseek-flash-latest"),
            ("glm", "glm-5.3-flash-latest"),
            ("glm", "glm-5.3-turbo"),
            ("claude", "glm-5.3"),
        ] {
            assert_eq!(lookup(engine, model), ModelFacts::unknown());
            assert!(lookup(engine, model).priced_pair().is_none());
        }
        // Capability evidence is advisory; it never supplies a missing price.
        let mut unpriced = lookup("glm", "glm-5.3");
        unpriced.input_usd_per_million = Fact::unknown();
        assert_eq!(unpriced.thinking.value, Some(Thinking::Mandatory));
        assert!(unpriced.priced_pair().is_none());
    }

    #[test]
    fn image_admission_needs_exact_sourced_support_token_bound_and_price() {
        let vision = "https://api-docs.deepseek.com/guides/vision";
        assert_eq!(lookup("deepseek", "deepseek-flash").image_admission(),
            Ok(ImageBound { tokens_per_image: 1024, source: vision, as_of: "2026-11-14" }));
        assert_eq!(lookup("deepseek", "deepseek-v4-pro").image_admission(), Err(ImageRefusal::Unsupported {
            source: "https://api-docs.deepseek.com/quick_start/pricing/", as_of: "2026-11-14" }));
        for model in ["glm-5.3-flash", "glm-5.3-flashx"] {
            let facts = lookup("glm", model);
            assert_eq!(facts.image_input.value, Some(true));
            assert_eq!(facts.image_tokens_per_image, Fact::unknown());
            assert_eq!(facts.image_admission(), Err(ImageRefusal::UnpricedImages {
                source: "https://docs.z.ai/guides/vlm/glm-5.3-flash", as_of: "2026-11-14" }));
        }
        // Family names, aliases, other engines and unlisted IDs stay unknown.
        for (engine, model) in [("glm", "glm-5.3"), ("glm", "glm-5.3-flashx-legal"), ("deepseek", "deepseek-v4-flash-vision-exp"),
            ("deepseek", "deepseek-flash-latest"), ("router", "deepseek-flash"), ("codex", "gpt-6-astra"), ("claude", "deepseek-flash")] {
            assert_eq!(lookup(engine, model).image_admission(), Err(ImageRefusal::Unknown), "{engine}:{model}");
        }
        // An image bound never substitutes for a missing token price.
        let mut unpriced = lookup("deepseek", "deepseek-flash");
        unpriced.budget_output_usd_per_million = Fact::unknown();
        assert_eq!(unpriced.image_admission(), Err(ImageRefusal::UnpricedTokens));
        let mut unattributed = lookup("deepseek", "deepseek-flash");
        unattributed.image_tokens_per_image = Fact { value: Some(1024), provenance: Provenance::Unknown };
        assert!(matches!(unattributed.image_admission(), Err(ImageRefusal::UnpricedImages { .. })));
        unattributed.image_tokens_per_image = Fact::static_value(0, vision, "2026-11-14");
        assert!(matches!(unattributed.image_admission(), Err(ImageRefusal::UnpricedImages { .. })));
        let mut unattributed = lookup("deepseek", "deepseek-flash");
        unattributed.image_input = Fact { value: Some(true), provenance: Provenance::Unknown };
        assert_eq!(unattributed.image_admission(), Err(ImageRefusal::Unknown));
        assert!(ImageRefusal::UnpricedImages { source: "https://docs.z.ai/guides/vlm/glm-5.3-flash", as_of: "2026-11-14" }
            .message("glm", "glm-5.3-flash").contains("per-image token charge is unpublished"));
    }

    #[test]
    fn openai_model_pages_supply_only_exact_dated_capabilities() {
        let astra = lookup("codex", "gpt-6-astra");
        assert_eq!(astra.context_window, Fact::static_value(
            1_050_000, "https://developers.openai.com/api/docs/models/gpt-6-astra", "2026-10-09"
        ));
        assert_eq!(astra.thinking, Fact::static_value(
            Thinking::Mandatory, "https://developers.openai.com/api/docs/guides/reasoning", "2026-10-09"
        ));
        for (model, page) in [
            ("gpt-5.6-sol", "https://developers.openai.com/api/docs/models/gpt-5.6-sol"),
            ("gpt-5.6-terra", "https://developers.openai.com/api/docs/models/gpt-5.6-terra"),
            ("gpt-5.6-luna", "https://developers.openai.com/api/docs/models/gpt-5.6-luna"),
            ("gpt-5.5", "https://developers.openai.com/api/docs/models/gpt-5.5"),
        ] {
            let facts = lookup("codex", model);
            assert_eq!(facts.context_window, Fact::static_value(1_050_000, page, "2026-10-09"));
            assert_eq!(facts.thinking, Fact::static_value(Thinking::Optional, page, "2026-10-09"));
            assert!(facts.priced_pair().is_some(), "capability update must not change Standard price facts");
        }
        let codex = lookup("codex", "gpt-5.3-codex");
        assert_eq!(codex.context_window, Fact::static_value(
            400_000, "https://developers.openai.com/api/docs/models/gpt-5.3-codex", "2026-10-09"
        ));
        assert_eq!(codex.thinking, Fact::unknown(), "the model page does not establish a thinking off switch");
        for (engine, model) in [("codex", "gpt-5.6"), ("codex", "gpt-6-astra-latest"), ("claude", "gpt-6-astra")] {
            assert_eq!(lookup(engine, model), ModelFacts::unknown());
        }
    }
}
