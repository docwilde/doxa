//! Read-only setup review. A catalog check never submits a Jev or worker turn.
use crate::{launch::Engine, new_session_catalog};
use doxa_router::Config;
use serde_json::{json, Value};
use std::{collections::BTreeMap, path::{Path, PathBuf}, time::{Duration, Instant}};

pub const REVIEW_LIFETIME: Duration = Duration::from_secs(60);

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Availability {
    Available,
    Unavailable(&'static str),
    Unknown(&'static str),
}

#[derive(Clone, Debug)]
pub struct Target {
    pub id: String,
    pub provider: String,
    pub model: String,
    pub effort: String,
    pub cli_account: bool,
    pub availability: Availability,
}

impl Target {
    pub fn label(&self) -> String {
        let state = match self.availability {
            Availability::Available => "account/catalog checked",
            Availability::Unavailable(reason) | Availability::Unknown(reason) => reason,
        };
        format!("{} · {}/{} · {} · {state}", self.id, self.provider, self.model, self.effort)
    }
}

#[derive(Clone, Debug)]
pub struct Review {
    pub path: PathBuf,
    pub config_hash: String,
    pub checked: Instant,
    pub targets: Vec<Target>,
    pub fallback_id: String,
    pub jev_key_present: bool,
}

impl Review {
    pub fn ready(&self) -> bool {
        self.jev_key_present && self.targets.iter().any(|target|
            target.id == self.fallback_id && target.availability == Availability::Available)
    }

    pub fn current(&self, path: &Path) -> bool {
        self.path == path && self.checked.elapsed() <= REVIEW_LIFETIME
            && Config::load(path).and_then(|config| config.hash()).is_ok_and(|hash| hash == self.config_hash)
            && self.jev_key_present == jev_key_present()
    }

    pub fn summary(&self) -> String {
        let available = self.targets.iter().filter(|target| target.availability == Availability::Available).count();
        let unknown = self.targets.iter().filter(|target| matches!(target.availability, Availability::Unknown(_))).count();
        let status = if !self.jev_key_present { "Jev key missing" }
            else if self.ready() { "Ready for new sessions" } else { "Fallback unavailable or unverified" };
        format!("{status} · {available}/{} targets available · {unknown} unknown", self.targets.len())
    }

    pub fn cost_scope(&self) -> &'static str {
        if self.targets.iter().any(|target| target.cli_account) {
            "Allowance tracks Jev/API only; CLI account charges unknown and excluded."
        } else { "Allowance tracks Jev/API estimates; turn context/tools/budget rechecked at launch." }
    }
}

fn jev_key_present() -> bool {
    doxa_router::credential().is_ok()
}

pub fn review(path: &Path, cwd: &Path) -> Result<Review, &'static str> {
    let config = Config::load(path).map_err(|_| "Config missing, unsafe, or invalid; use an absolute owner-private JSON file")?;
    review_with(path, &config, jev_key_present(), |provider| provider_catalog(provider, cwd))
}

fn provider_catalog(provider: &str, cwd: &Path) -> Result<Value, Availability> {
    match provider {
        "claude" | "codex" => new_session_catalog::load_authenticated(
            if provider == "claude" { Engine::Claude } else { Engine::Codex }, cwd)
            .map_err(|_| Availability::Unknown("account/catalog unverified · /login or retry")),
        "deepseek" | "glm" => {
            let vendor = if provider == "deepseek" { doxa_vendors::Vendor::DeepSeek } else { doxa_vendors::Vendor::Glm };
            match doxa_vendors::credentials::status(vendor) {
                Ok(doxa_vendors::credentials::CredentialStatus::Missing) => return Err(Availability::Unavailable("credential missing")),
                Err(_) => return Err(Availability::Unknown("credential store unavailable")),
                _ => {},
            }
            let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build()
                .map_err(|_| Availability::Unknown("catalog unavailable"))?;
            let rows = runtime.block_on(async { tokio::time::timeout(Duration::from_secs(2),
                doxa_vendors::catalog_models(vendor)).await.ok().flatten() })
                .ok_or(Availability::Unknown("catalog unavailable or timed out"))?;
            Ok(json!({"capabilities":rows.iter().map(|row|json!({"model":row.id,"efforts":row.efforts})).collect::<Vec<_>>()}))
        }
        _ => Err(Availability::Unavailable("unsupported provider")),
    }
}

pub(crate) fn review_with(path: &Path, config: &Config, jev_key_present: bool,
    mut catalog: impl FnMut(&str) -> Result<Value, Availability>) -> Result<Review, &'static str> {
    let mut catalogs = BTreeMap::new();
    let mut targets = Vec::new();
    for candidate in &config.candidates {
        // The validated wire schema is the compatibility boundary. No candidate
        // descriptions, provider errors or account fields reach settings/UI.
        let value = serde_json::to_value(candidate).map_err(|_| "Config metadata unavailable")?;
        let provider = value["provider"].as_str().ok_or("Config provider unavailable")?;
        let cli_account = value["billing"] == "cli_account";
        let rows = catalogs.entry(provider.to_owned()).or_insert_with(|| catalog(provider));
        let availability = match rows {
            Err(status) => status.clone(),
            Ok(rows) => candidate_availability(&value, rows),
        };
        targets.push(Target { id:candidate.id.clone(), provider:provider.into(), model:candidate.model.clone(),
            effort:candidate.effort.clone(), cli_account, availability });
    }
    Ok(Review { path:path.into(), config_hash:config.hash().map_err(|_|"Config identity unavailable")?,
        checked:Instant::now(),targets,fallback_id:config.fallback_id.clone(),jev_key_present })
}

fn candidate_availability(candidate: &Value, catalog: &Value) -> Availability {
    let Some(rows) = catalog["capabilities"].as_array() else { return Availability::Unknown("catalog capabilities unverified"); };
    let Some(row) = rows.iter().find(|row| row["model"] == candidate["model"]) else {
        return Availability::Unavailable("exact model not in live catalog");
    };
    if !row["efforts"].as_array().is_some_and(|efforts| efforts.contains(&candidate["effort"])) {
        return Availability::Unavailable("exact effort unverified");
    }
    let provider = candidate["provider"].as_str().unwrap_or("");
    if matches!(provider, "claude" | "codex") {
        return if candidate["billing"] == "cli_account" { Availability::Available }
            else { Availability::Unavailable("CLI account billing must be explicit") };
    }
    let facts = doxa_engines::model_registry::lookup(provider,candidate["model"].as_str().unwrap_or(""));
    let Some((input,output,_,_)) = facts.budget_bound_pair() else { return Availability::Unavailable("source-dated price unknown"); };
    if candidate["input_usd_micros_per_million"].as_u64().is_none_or(|rate| (rate as f64) < (input*1_000_000.0).ceil())
        || candidate["output_usd_micros_per_million"].as_u64().is_none_or(|rate| (rate as f64) < (output*1_000_000.0).ceil()) {
        return Availability::Unavailable("configured rates below source-dated bound");
    }
    if facts.context_window.value.is_some_and(|window| candidate["context_tokens"].as_u64().is_none_or(|cap| cap > window)) {
        return Availability::Unavailable("context cap exceeds verified window");
    }
    Availability::Available
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    pub(crate) fn config() -> Config {
        serde_json::from_value(json!({"version":1,"jev_model":"jev-1.13.0","criteria_version":"settings-v1",
            "fallback_id":"ds","confidence_threshold":0.5,"max_calls":2,"max_spend_usd_micros":1000,
            "max_input_bytes":2048,"deadline_ms":1000,"candidates":[
                {"id":"ds","provider":"deepseek","model":"deepseek-flash","effort":"none","description":"Reviewed API target","context_tokens":131072,"max_output_tokens":1024,"supports_tools":true,"input_usd_micros_per_million":1000000,"output_usd_micros_per_million":2000000},
                {"id":"glm","provider":"glm","model":"glm-5.3-flash","effort":"high","description":"Reviewed API target","context_tokens":131072,"max_output_tokens":1024,"supports_tools":true,"input_usd_micros_per_million":1000000,"output_usd_micros_per_million":2000000}]})).unwrap()
    }

    #[test]
    fn setup_review_requires_exact_live_effort_and_current_fallback_without_inference() {
        let config = config(); let mut calls=Vec::new();
        let review=review_with(Path::new("/private/router.json"),&config,true,|provider| {
            calls.push(provider.to_owned());
            Ok(json!({"capabilities":[{"model":"deepseek-flash","efforts":["none"]}]}))
        }).unwrap();
        assert_eq!(calls,["deepseek","glm"]);
        assert!(review.ready());
        assert_eq!(review.targets[1].availability,Availability::Unavailable("exact model not in live catalog"));
        let review=review_with(Path::new("/private/router.json"),&config,true,|_|Err(Availability::Unknown("catalog unavailable"))).unwrap();
        assert!(!review.ready()); assert!(review.summary().contains("2 unknown"));
        let wrong_effort=json!({"capabilities":[{"model":"deepseek-flash","efforts":["high"]}]});
        assert_eq!(candidate_availability(&serde_json::to_value(&config.candidates[0]).unwrap(),&wrong_effort),
            Availability::Unavailable("exact effort unverified"));
    }

    #[test]
    fn cli_account_catalog_does_not_imply_known_usd_or_subscription_and_never_uses_default_model() {
        let candidate=json!({"provider":"codex","billing":"cli_account","model":"account-model","effort":"high"});
        let catalog=json!({"capabilities":[{"model":"account-model","efforts":["high"]}],"account":{"private":"ignored"}});
        assert_eq!(candidate_availability(&candidate,&catalog),Availability::Available);
        let mut guessed=candidate.clone();guessed["model"]=json!("guessed-model");
        assert_eq!(candidate_availability(&guessed,&catalog),
            Availability::Unavailable("exact model not in live catalog"));
        let mut wrong=candidate.clone();wrong["billing"]=json!("api");
        assert_eq!(candidate_availability(&wrong,&catalog),Availability::Unavailable("CLI account billing must be explicit"));
    }
}
