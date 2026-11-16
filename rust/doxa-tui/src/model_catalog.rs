//! Explicit, read-only model-listing checks. They never start inference and
//! never claim that a listed model can complete a request.
//!
//! `check` reads OpenAI's API catalog for an API key. `check_availability`
//! asks whether an engine's current account lists one exact model ID. Each
//! result is printed for the operator and dropped: no cache, picker row,
//! effort choice or budget admission reads it, and `unknown` stays unknown.
use doxa_engines::codex_appserver::{
    AppServerDriver, AppServerError, AppServerOptions, CodexPermission, ModelListing, ACCOUNT_UNAVAILABLE,
};
use doxa_engines::codex_driver::SandboxMode;
use doxa_vendors::{credentials, Vendor};
use serde::Deserialize;
use std::{io, path::Path, time::{Duration, Instant}};

const MODELS_URL: &str = "https://api.openai.com/v1/models";
const MAX_RESPONSE: usize = 1024 * 1024;
const MAX_MODELS: usize = 4096;
const DEADLINE: Duration = Duration::from_secs(5);
/// Includes provider start-up; the reported latency covers `model/list` only.
const CODEX_DEADLINE: Duration = Duration::from_secs(20);
/// DeepSeek documents `GET /models` returning `object: "list"` and `data[]`
/// rows with `object: "model"` and `id`, the shape `classify` requires. It
/// documents no pagination, service tier or billing field (checked 2026-11-14).
const DEEPSEEK_LISTING_DOCS: &str = "https://api-docs.deepseek.com/api/list-models/";
/// Z.AI's API reference index lists no model-listing endpoint (checked
/// 2026-11-14). The picker's `/paas/v4/models` catalog path is undocumented,
/// so it is not used as account availability evidence.
const GLM_NO_LISTING: &str = "https://docs.z.ai/llms.txt";
/// Claude Code documents `supportedModels()` only as "available models with
/// display info", not as an account-scoped list of exact model IDs. The
/// Anthropic Models API lists models for an API key, which need not be the
/// Claude CLI login (checked 2026-11-14).
const CLAUDE_NO_LISTING: &str = "https://code.claude.com/docs/en/agent-sdk/typescript";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CatalogStatus { Listed, Unlisted, Unknown }
impl CatalogStatus {
    pub fn as_str(self) -> &'static str {
        match self { Self::Listed => "listed", Self::Unlisted => "unlisted", Self::Unknown => "unknown" }
    }
}

/// One explicit account-listing observation for an exact model ID.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Observation {
    pub status: CatalogStatus,
    /// Fixed reason for `unknown`; never provider response text.
    pub reason: Option<&'static str>,
    /// The listing endpoint or RPC, or the reference showing that none exists.
    pub source: String,
    /// UTC time when this result was determined (RFC 3339, whole seconds).
    pub observed_at: String,
    /// Duration of the listing request alone; None when none completed.
    pub latency: Option<Duration>,
    /// A default service tier the listing states for this exact row. It is a
    /// catalog default, not the billed tier of any request.
    pub catalog_default_tier: Option<String>,
}

impl Observation {
    fn observed(status: CatalogStatus, source: String, latency: Duration, catalog_default_tier: Option<String>) -> Self {
        Self { status, reason: None, source, observed_at: observed_now(), latency: Some(latency), catalog_default_tier }
    }

    fn unknown(source: String, reason: &'static str, latency: Option<Duration>) -> Self {
        Self { status: CatalogStatus::Unknown, reason: Some(reason), source, observed_at: observed_now(),
            latency, catalog_default_tier: None }
    }

    pub fn render(&self, engine: &str, model: &str) -> String {
        let status = match self.reason {
            Some(reason) => format!("{} ({reason})", self.status.as_str()),
            None => self.status.as_str().to_owned(),
        };
        let latency = self.latency.map_or_else(|| "not measured (no listing completed)".to_owned(),
            |latency| format!("{} ms", latency.as_millis()));
        let tier = self.catalog_default_tier.as_deref().map_or_else(String::new,
            |tier| format!("; the listing states catalog default service tier {tier}, not a billed request tier"));
        [
            format!("Account availability · {engine}/{model}: {status}"),
            format!("  observed: {}", self.observed_at),
            format!("  source: {}", self.source),
            format!("  listing latency: {latency}"),
            format!("  billed tier: unknown{tier}"),
            "Observation only: a listing does not select a model, add effort choices, admit a budget, \
             or prove that a request will succeed.".into(),
        ].join("\n")
    }
}

fn observed_now() -> String {
    time::OffsetDateTime::now_utc().replace_nanosecond(0).ok()
        .and_then(|now| now.format(&time::format_description::well_known::Rfc3339).ok())
        .unwrap_or_else(|| "unknown".into())
}

#[derive(Deserialize)]
struct Catalog { object: String, data: Vec<CatalogModel> }
#[derive(Deserialize)]
struct CatalogModel { object: String, id: String }

fn exact_id(value: &str) -> bool {
    !value.is_empty() && value.len() <= 100 && value.bytes().all(|byte|
        byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
}

/// Uses the same OPENAI_API_KEY as an independent `codex:` fleet reviewer.
/// A missing key or failed catalog read yields `unknown`, never an assertion
/// of unavailable inference access. Only the explicit CLI flag calls this.
pub fn check(engine: &str, model: &str) -> io::Result<CatalogStatus> {
    if engine != "codex" || !exact_id(model) {
        return Err(io::Error::new(io::ErrorKind::InvalidInput,
            "catalog check requires an exact codex model ID"));
    }
    let key = match std::env::var("OPENAI_API_KEY").ok().filter(|key|
        (8..=4096).contains(&key.len()) && key.bytes().all(|byte| byte.is_ascii_graphic())) {
        Some(key) => key,
        None => return Ok(CatalogStatus::Unknown),
    };
    Ok(check_at(MODELS_URL, &key, model, DEADLINE))
}

/// Only the explicit `--check-availability` flag calls this. Missing
/// credentials, refused or malformed listings, timeouts and engines without a
/// documented account listing yield `unknown`; no other path runs it.
pub fn check_availability(engine: &str, model: &str) -> io::Result<Observation> {
    if !exact_id(model) {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "availability check requires an exact model ID"));
    }
    Ok(match engine {
        // The same executable and project directory as a new Codex session.
        "codex" => match (crate::launch::codex_executable(&Default::default()), std::env::current_dir()) {
            (Ok(executable), Ok(cwd)) => codex_at(&executable, &cwd, model, CODEX_DEADLINE),
            _ => Observation::unknown("codex app-server account/read and model/list".into(),
                "Codex executable or working directory unavailable", None),
        },
        "deepseek" => deepseek_at(Vendor::DeepSeek.models_endpoint(), credentials::resolve(Vendor::DeepSeek), model, DEADLINE),
        "glm" => Observation::unknown(
            format!("none; the Z.AI API reference lists no model-listing endpoint ({GLM_NO_LISTING}, checked 2026-11-14)"),
            "Z.AI documents no model-listing endpoint", None),
        "claude" => Observation::unknown(
            format!("none; Claude Code documents no account-scoped exact-ID model listing ({CLAUDE_NO_LISTING}, checked 2026-11-14)"),
            "no documented account listing for the Claude CLI", None),
        _ => return Err(io::Error::new(io::ErrorKind::InvalidInput,
            "availability check supports claude, codex, deepseek or glm")),
    })
}

/// Codex can answer `model/list` from its own model cache, so the account
/// state may predate the observation time and the latency may be local.
fn codex_at(executable: &Path, cwd: &Path, model: &str, deadline: Duration) -> Observation {
    let source = format!("codex app-server account/read and model/list via {} \
        (Codex may answer from its own model cache)", executable.display());
    let options = AppServerOptions { executable: executable.into(), cwd: cwd.into(), model: None,
        sandbox: SandboxMode::ReadOnly, permission: CodexPermission::OnRequest, resume_thread: None,
        turn_timeout: deadline };
    let listing = match tokio::runtime::Builder::new_current_thread().enable_all().build() {
        Ok(runtime) => runtime.block_on(AppServerDriver::observe_account_models(options, deadline)),
        Err(_) => return Observation::unknown(source, "local runtime unavailable", None),
    };
    match listing {
        Ok(listing) => classify_codex(&listing, model, source),
        Err(error) => Observation::unknown(source, codex_reason(&error), None),
    }
}

/// Provider error text can carry account detail, so only a fixed label leaves.
fn codex_reason(error: &AppServerError) -> &'static str {
    match error {
        AppServerError::TimedOut => "timed out",
        AppServerError::Protocol(message) if *message == ACCOUNT_UNAVAILABLE => "Codex account not authenticated",
        AppServerError::Server(_) => "listing request refused",
        AppServerError::Io(_) => "Codex app-server unavailable",
        _ => "malformed listing",
    }
}

fn classify_codex(listing: &ModelListing, model: &str, source: String) -> Observation {
    let rows = listing.models.iter().filter(|row| row.model == model).collect::<Vec<_>>();
    let Some(first) = rows.first() else {
        return if listing.complete {
            Observation::observed(CatalogStatus::Unlisted, source, listing.latency, None)
        } else {
            Observation::unknown(source, "listing exceeded DOXA's page bound", Some(listing.latency))
        };
    };
    // Preset rows may repeat a model; disagreeing tiers stay unknown.
    let tier = first.default_service_tier.clone()
        .filter(|tier| rows.iter().all(|row| row.default_service_tier.as_ref() == Some(tier)));
    Observation::observed(CatalogStatus::Listed, source, listing.latency, tier)
}

/// DeepSeek's documented listing with the key its sessions resolve. The key is
/// sent only as a bearer token to the fixed endpoint and never printed.
pub(crate) fn deepseek_at(url: &str, key: io::Result<Option<String>>, model: &str, deadline: Duration) -> Observation {
    let source = format!("GET {url} ({DEEPSEEK_LISTING_DOCS})");
    match key {
        Ok(Some(key)) => match listing_at(url, &key, model, deadline) {
            Ok((status, latency)) => Observation::observed(status, source, latency, None),
            Err(reason) => Observation::unknown(source, reason, None),
        },
        Ok(None) => Observation::unknown(source, "no DeepSeek credential", None),
        Err(_) => Observation::unknown(source, "DeepSeek credential store unavailable", None),
    }
}

fn check_at(url: &str, key: &str, model: &str, deadline: Duration) -> CatalogStatus {
    listing_at(url, key, model, deadline).map_or(CatalogStatus::Unknown, |(status, _)| status)
}

/// One GET with no generation payload, redirects or environment proxy. The
/// latency covers that request and its body; failures return a fixed reason.
fn listing_at(url: &str, key: &str, model: &str, deadline: Duration)
    -> Result<(CatalogStatus, Duration), &'static str> {
    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build()
        .map_err(|_| "local runtime unavailable")?;
    let started = Instant::now();
    let body = runtime.block_on(async {
        tokio::time::timeout(deadline, async {
            let http = reqwest::Client::builder()
                .no_proxy().redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_secs(2)).build().map_err(|_| "HTTP client unavailable")?;
            let mut reply = http.get(url).bearer_auth(key).send().await.map_err(|_| "request failed")?;
            if reply.status().is_redirection() { return Err("redirect refused"); }
            if reply.status() != reqwest::StatusCode::OK { return Err("listing request refused"); }
            if reply.content_length().is_some_and(|length| length > MAX_RESPONSE as u64) {
                return Err("response too large");
            }
            let mut bytes = Vec::new();
            while let Some(chunk) = reply.chunk().await.map_err(|_| "response read failed")? {
                if bytes.len().saturating_add(chunk.len()) > MAX_RESPONSE { return Err("response too large"); }
                bytes.extend_from_slice(&chunk);
            }
            Ok(bytes)
        }).await.unwrap_or(Err("timed out"))
    })?;
    let latency = started.elapsed();
    Ok((classify(&body, model)?, latency))
}

/// Strict OpenAI-compatible list shape. A malformed or duplicate row makes the
/// whole listing unusable instead of silently dropping evidence.
fn classify(body: &[u8], model: &str) -> Result<CatalogStatus, &'static str> {
    let catalog: Catalog = serde_json::from_slice(body).map_err(|_| "malformed listing")?;
    if catalog.object != "list" || catalog.data.len() > MAX_MODELS { return Err("malformed listing"); }
    let mut matches = 0;
    for row in catalog.data {
        if row.object != "model" || row.id.is_empty() || row.id.len() > 256 ||
            !row.id.bytes().all(|byte| byte.is_ascii_graphic()) { return Err("malformed listing"); }
        matches += usize::from(row.id == model);
    }
    match matches {
        0 => Ok(CatalogStatus::Unlisted),
        1 => Ok(CatalogStatus::Listed),
        _ => Err("duplicate listing row"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{io::{Read, Write}, net::{Ipv4Addr, TcpListener}, thread::{self, JoinHandle}, time::Instant};

    fn fake_server(reply: Vec<u8>, delay: Duration) -> (String, JoinHandle<String>) {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        listener.set_nonblocking(true).unwrap();
        let url = format!("http://{}/v1/models", listener.local_addr().unwrap());
        let worker = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(2);
            let mut stream = loop {
                match listener.accept() {
                    Ok((stream, _)) => {
                        // Accepted sockets inherit nonblocking mode on macOS.
                        stream.set_nonblocking(false).unwrap();
                        break stream;
                    }
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock && Instant::now() < deadline =>
                        thread::sleep(Duration::from_millis(5)),
                    Err(error) => panic!("catalog fixture accept failed: {error}"),
                }
            };
            stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            stream.set_write_timeout(Some(Duration::from_secs(2))).unwrap();
            let mut request = Vec::new();
            while !request.ends_with(b"\r\n\r\n") && request.len() < 8192 {
                let mut byte = [0]; stream.read_exact(&mut byte).unwrap(); request.push(byte[0]);
            }
            thread::sleep(delay);
            let _ = stream.write_all(&reply);
            String::from_utf8(request).unwrap()
        });
        (url, worker)
    }
    fn response(status: &str, body: &[u8]) -> Vec<u8> {
        let mut reply = format!("HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).into_bytes();
        reply.extend_from_slice(body); reply
    }

    #[test]
    fn same_key_exact_listing_and_unlisting_use_get_only() {
        for (body, expected) in [
            (br#"{"object":"list","data":[{"object":"model","id":"gpt-6-astra"}]}"#.as_slice(), CatalogStatus::Listed),
            (br#"{"object":"list","data":[{"object":"model","id":"gpt-6-sol"}]}"#, CatalogStatus::Unlisted),
        ] {
            let (url, worker) = fake_server(response("200 OK", body), Duration::ZERO);
            assert_eq!(check_at(&url, "fixture-key", "gpt-6-astra", Duration::from_secs(2)), expected);
            let request = worker.join().unwrap();
            assert!(request.starts_with("GET /v1/models HTTP/1.1\r\n"));
            assert!(request.to_ascii_lowercase().contains("authorization: bearer fixture-key\r\n"));
            assert!(!request.contains("POST "));
        }
    }

    #[test]
    fn malformed_oversized_non_success_and_slow_replies_are_unknown() {
        let malformed = br#"{"object":"other","data":[{"object":"model","id":"gpt-6-astra"}]}"#;
        for reply in [
            response("200 OK", malformed),
            response("401 Unauthorized", b"secret diagnostic body"),
            response("200 OK", &vec![b'x'; MAX_RESPONSE + 1]),
        ] {
            let (url, worker) = fake_server(reply, Duration::ZERO);
            assert_eq!(check_at(&url, "fixture-key", "gpt-6-astra", Duration::from_secs(2)), CatalogStatus::Unknown);
            worker.join().unwrap();
        }
        let (url, worker) = fake_server(response("200 OK", br#"{"object":"list","data":[]}"#), Duration::from_millis(150));
        assert_eq!(check_at(&url, "fixture-key", "gpt-6-astra", Duration::from_millis(30)), CatalogStatus::Unknown);
        worker.join().unwrap();
    }

    #[test]
    fn redirects_are_not_followed_and_invalid_engine_is_rejected() {
        let target = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        target.set_nonblocking(true).unwrap();
        let reply = format!("HTTP/1.1 302 Found\r\nLocation: http://{}/v1/models\r\nContent-Length: 0\r\n\r\n", target.local_addr().unwrap()).into_bytes();
        let (url, worker) = fake_server(reply, Duration::ZERO);
        assert_eq!(check_at(&url, "fixture-key", "gpt-6-astra", Duration::from_secs(2)), CatalogStatus::Unknown);
        worker.join().unwrap();
        assert!(target.accept().is_err(), "redirected request leaked the API key");
        assert!(check("claude", "gpt-6-astra").is_err());
        assert!(check("codex", "gpt-6-astra\n").is_err());
    }

    const DEEPSEEK_EXAMPLE: &[u8] = br#"{"object":"list","data":[{"id":"deepseek-flash","object":"model","owned_by":"deepseek"},{"id":"deepseek-v4-pro","object":"model","owned_by":"deepseek","context_window":1048576}]}"#;

    fn assert_observed_now(observation: &Observation) {
        let observed = ::time::OffsetDateTime::parse(&observation.observed_at,
            &::time::format_description::well_known::Rfc3339).unwrap();
        assert_eq!(observed.offset(), ::time::UtcOffset::UTC);
        assert!((::time::OffsetDateTime::now_utc() - observed).whole_seconds().abs() < 60);
    }

    #[test]
    fn classification_is_exact_and_strict() {
        assert_eq!(classify(DEEPSEEK_EXAMPLE, "deepseek-v4-pro"), Ok(CatalogStatus::Listed));
        assert_eq!(classify(DEEPSEEK_EXAMPLE, "deepseek-v4"), Ok(CatalogStatus::Unlisted), "no prefix or family match");
        assert_eq!(classify(DEEPSEEK_EXAMPLE, "DeepSeek-Flash"), Ok(CatalogStatus::Unlisted));
        assert_eq!(classify(br#"{"object":"list","data":[]}"#, "deepseek-flash"), Ok(CatalogStatus::Unlisted));
        let duplicate = br#"{"object":"list","data":[{"id":"deepseek-flash","object":"model"},{"id":"deepseek-flash","object":"model"}]}"#;
        assert_eq!(classify(duplicate, "deepseek-flash"), Err("duplicate listing row"));
        let too_many = format!(r#"{{"object":"list","data":[{}]}}"#,
            (0..=MAX_MODELS).map(|index| format!(r#"{{"id":"m-{index}","object":"model"}}"#)).collect::<Vec<_>>().join(","));
        for body in [
            b"not json".as_slice(),
            br#"{"object":"page","data":[]}"#,
            br#"{"object":"list"}"#,
            br#"{"object":"list","data":[{"id":"deepseek-flash","object":"file"}]}"#,
            br#"{"object":"list","data":[{"id":"has space","object":"model"}]}"#,
            br#"{"object":"list","data":[{"id":"","object":"model"}]}"#,
            br#"{"object":"list","data":[{"object":"model"}]}"#,
            too_many.as_bytes(),
        ] {
            assert_eq!(classify(body, "deepseek-flash"), Err("malformed listing"),
                "{}", String::from_utf8_lossy(&body[..body.len().min(80)]));
        }
    }

    #[test]
    fn deepseek_listing_reports_source_time_and_latency_without_the_key() {
        for (model, expected) in [("deepseek-v4-pro", CatalogStatus::Listed), ("deepseek-chat", CatalogStatus::Unlisted)] {
            let (url, worker) = fake_server(response("200 OK", DEEPSEEK_EXAMPLE), Duration::from_millis(20));
            let observation = deepseek_at(&url, Ok(Some("fixture-secret-key".into())), model, Duration::from_secs(2));
            let request = worker.join().unwrap();
            assert!(request.starts_with("GET /v1/models HTTP/1.1\r\n"));
            assert!(request.to_ascii_lowercase().contains("authorization: bearer fixture-secret-key\r\n"));
            assert_eq!((observation.status, observation.reason), (expected, None));
            assert!(observation.latency.is_some_and(|latency| latency >= Duration::from_millis(20)));
            assert_eq!(observation.source, format!("GET {url} (https://api-docs.deepseek.com/api/list-models/)"));
            assert_eq!(observation.catalog_default_tier, None, "DeepSeek's listing states no tier");
            assert_observed_now(&observation);
            let rendered = observation.render("deepseek", model);
            assert!(rendered.starts_with(&format!("Account availability · deepseek/{model}: {}\n", expected.as_str())));
            assert!(rendered.contains("\n  listing latency: ") && rendered.contains(" ms\n"));
            assert!(rendered.contains("\n  billed tier: unknown\n"));
            assert!(rendered.contains("does not select a model, add effort choices, admit a budget"));
            assert!(!rendered.contains("fixture-secret-key"));
        }
    }

    #[test]
    fn deepseek_unknown_paths_keep_fixed_reasons_and_no_latency() {
        let missing = deepseek_at("http://127.0.0.1:9/models", Ok(None), "deepseek-flash", Duration::from_secs(1));
        assert_eq!((missing.status, missing.reason, missing.latency),
            (CatalogStatus::Unknown, Some("no DeepSeek credential"), None));
        assert!(missing.render("deepseek", "deepseek-flash").contains("listing latency: not measured (no listing completed)"));
        let unsafe_store = deepseek_at("http://127.0.0.1:9/models",
            Err(io::Error::from(io::ErrorKind::PermissionDenied)), "deepseek-flash", Duration::from_secs(1));
        assert_eq!(unsafe_store.reason, Some("DeepSeek credential store unavailable"));
        let target = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        target.set_nonblocking(true).unwrap();
        let redirect = format!("HTTP/1.1 307 Temporary Redirect\r\nLocation: http://{}/models\r\nContent-Length: 0\r\n\r\n",
            target.local_addr().unwrap()).into_bytes();
        for (reply, delay, deadline, reason) in [
            (response("401 Unauthorized", b"fixture-secret-key rejected"), Duration::ZERO, Duration::from_secs(2), "listing request refused"),
            (response("200 OK", br#"{"object":"list","data":[{"id":"deepseek-flash"}]}"#), Duration::ZERO, Duration::from_secs(2), "malformed listing"),
            (response("200 OK", &vec![b'x'; MAX_RESPONSE + 1]), Duration::ZERO, Duration::from_secs(2), "response too large"),
            (response("200 OK", DEEPSEEK_EXAMPLE), Duration::from_millis(400), Duration::from_millis(50), "timed out"),
            (redirect, Duration::ZERO, Duration::from_secs(2), "redirect refused"),
        ] {
            let (url, worker) = fake_server(reply, delay);
            let observation = deepseek_at(&url, Ok(Some("fixture-secret-key".into())), "deepseek-flash", deadline);
            worker.join().unwrap();
            assert_eq!((observation.status, observation.reason, observation.latency),
                (CatalogStatus::Unknown, Some(reason), None));
            assert!(!observation.render("deepseek", "deepseek-flash").contains("fixture-secret-key"));
        }
        assert!(target.accept().is_err(), "a redirected listing request leaked the key");
    }

    #[test]
    fn codex_listing_classification_never_turns_a_bound_into_unlisted() {
        use doxa_engines::codex_appserver::ListedModel;
        let row = |model: &str, tier: Option<&str>| ListedModel { model: model.into(), default_service_tier: tier.map(Into::into) };
        let listing = |models, complete| ModelListing { models, complete, latency: Duration::from_millis(7) };
        let complete = listing(vec![row("fixture-a", Some("flex")), row("fixture-b", None)], true);
        let listed = classify_codex(&complete, "fixture-a", "fixture source".into());
        assert_eq!((listed.status, listed.reason, listed.latency),
            (CatalogStatus::Listed, None, Some(Duration::from_millis(7))));
        assert_eq!(listed.catalog_default_tier.as_deref(), Some("flex"));
        assert!(listed.render("codex", "fixture-a").contains(
            "billed tier: unknown; the listing states catalog default service tier flex, not a billed request tier"));
        assert_eq!(classify_codex(&complete, "fixture-b", String::new()).catalog_default_tier, None);
        assert_eq!(classify_codex(&complete, "fixture", String::new()).status, CatalogStatus::Unlisted, "no prefix match");
        let disagreeing = listing(vec![row("fixture-a", Some("flex")), row("fixture-a", Some("priority"))], true);
        let repeated = classify_codex(&disagreeing, "fixture-a", String::new());
        assert_eq!((repeated.status, repeated.catalog_default_tier), (CatalogStatus::Listed, None));
        let bounded = classify_codex(&listing(vec![row("fixture-a", None)], false), "fixture-z", String::new());
        assert_eq!((bounded.status, bounded.reason), (CatalogStatus::Unknown, Some("listing exceeded DOXA's page bound")));
        assert_eq!(classify_codex(&listing(vec![row("fixture-z", None)], false), "fixture-z", String::new()).status,
            CatalogStatus::Listed, "a row seen before the bound is still listed");
        for (error, reason) in [
            (AppServerError::TimedOut, "timed out"),
            (AppServerError::Protocol(ACCOUNT_UNAVAILABLE), "Codex account not authenticated"),
            (AppServerError::Server("private provider detail".into()), "listing request refused"),
            (AppServerError::Io(io::Error::from(io::ErrorKind::NotFound)), "Codex app-server unavailable"),
            (AppServerError::Protocol("invalid model catalog row"), "malformed listing"),
        ] { assert_eq!(codex_reason(&error), reason); }
    }

    #[test]
    fn codex_fixture_app_server_reports_account_listing_end_to_end() {
        if std::env::var_os(doxa_isolation::ACTIVE_MANIFEST).is_some() { return; }
        use std::os::unix::fs::PermissionsExt;
        let fixture = r#"#!/usr/bin/env python3
import json, pathlib, sys, time
mode = pathlib.Path('mode').read_text()
for line in sys.stdin:
    request = json.loads(line)
    with open('calls.jsonl', 'a') as calls:
        calls.write(json.dumps(request) + '\n')
    method = request['method']
    if method == 'initialized':
        continue
    if method == 'initialize':
        result = {'userAgent': 'codex_cli_rs/0.167.0'}
    elif method == 'account/read':
        result = {'account': None if mode == 'signed-out' else {'type': 'apiKey'}}
    elif method == 'model/list':
        if mode == 'slow':
            time.sleep(5)
        result = {'data': [{'model': 'fixture-listed', 'defaultServiceTier': 'flex'}], 'nextCursor': None}
    else:
        raise AssertionError('availability started an unexpected operation: ' + method)
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
"#;
        for (mode, model, deadline, status, reason) in [
            ("listed", "fixture-listed", Duration::from_secs(10), CatalogStatus::Listed, None),
            ("listed", "fixture-other", Duration::from_secs(10), CatalogStatus::Unlisted, None),
            ("signed-out", "fixture-listed", Duration::from_secs(10), CatalogStatus::Unknown, Some("Codex account not authenticated")),
            ("slow", "fixture-listed", Duration::from_millis(1500), CatalogStatus::Unknown, Some("timed out")),
        ] {
            let root = tempfile::tempdir().unwrap();
            let executable = root.path().join("codex");
            std::fs::write(&executable, fixture).unwrap();
            std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o700)).unwrap();
            std::fs::write(root.path().join("mode"), mode).unwrap();
            let observation = codex_at(&executable, root.path(), model, deadline);
            assert_eq!((observation.status, observation.reason), (status, reason), "{mode} {model}");
            assert_eq!(observation.latency.is_some(), reason.is_none(), "latency is reported only for a completed listing");
            assert_eq!(observation.catalog_default_tier.as_deref(), (status == CatalogStatus::Listed).then_some("flex"));
            assert!(observation.source.starts_with("codex app-server account/read and model/list via "));
            assert!(observation.source.ends_with(" (Codex may answer from its own model cache)"));
            assert_observed_now(&observation);
            let methods = std::fs::read_to_string(root.path().join("calls.jsonl")).unwrap().lines()
                .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap()["method"].as_str().unwrap().to_owned())
                .collect::<Vec<_>>();
            assert!(methods.iter().all(|method| matches!(method.as_str(),
                "initialize" | "initialized" | "account/read" | "model/list")), "{methods:?}");
        }
    }

    #[test]
    fn engines_without_a_documented_account_listing_stay_unknown_offline() {
        for (engine, model, reason, reference) in [
            ("glm", "glm-5.3", "Z.AI documents no model-listing endpoint", "https://docs.z.ai/llms.txt"),
            ("claude", "claude-opus-5", "no documented account listing for the Claude CLI",
                "https://code.claude.com/docs/en/agent-sdk/typescript"),
        ] {
            let observation = check_availability(engine, model).unwrap();
            assert_eq!((observation.status, observation.reason, observation.latency),
                (CatalogStatus::Unknown, Some(reason), None));
            assert!(observation.source.starts_with("none; ") && observation.source.contains(reference));
            assert_observed_now(&observation);
        }
        assert!(check_availability("router", "auto").is_err());
        assert!(check_availability("glm", "glm-5.3\n").is_err());
        assert!(check_availability("deepseek", "").is_err());
    }
}
