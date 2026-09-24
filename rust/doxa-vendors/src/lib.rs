// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded DeepSeek/GLM chat-completions transport and SSE normalization.
//! Tool execution deliberately belongs to a future gated engine integration.

use futures_util::{future::BoxFuture, StreamExt};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::time::{Duration, Instant};
use tokio::sync::watch;

pub const STREAM_LINE_MAX: usize = 1024 * 1024;
pub const STREAM_BODY_MAX: usize = 64 * 1024 * 1024;
pub const ERROR_BODY_MAX: usize = 800;
pub const TOOL_ARGUMENT_MAX: usize = 1024 * 1024;
pub const MAX_TOOL_CALLS: usize = 128;
pub const MAX_TOOL_STEPS: usize = 24;
pub const MAX_HISTORY_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_HISTORY_MESSAGES: usize = 512;
pub const MAX_TOOL_RESULT_BYTES: usize = 1024 * 1024;
pub const MAX_TURN_DURATION: Duration = Duration::from_secs(3600);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Vendor {
    DeepSeek,
    Glm,
}
impl Vendor {
    pub fn engine_id(self) -> &'static str {
        match self {
            Self::DeepSeek => "deepseek",
            Self::Glm => "glm",
        }
    }
    pub fn env_var(self) -> &'static str {
        match self {
            Self::DeepSeek => "DEEPSEEK_API_KEY",
            Self::Glm => "ZAI_API_KEY",
        }
    }
    pub fn endpoint(self) -> &'static str {
        match self {
            Self::DeepSeek => "https://api.deepseek.com/chat/completions",
            Self::Glm => "https://api.z.ai/api/paas/v4/chat/completions",
        }
    }
    pub fn default_model(self) -> &'static str {
        match self {
            Self::DeepSeek => "deepseek-flash",
            Self::Glm => "glm-5.3-flash",
        }
    }
    fn valid_effort(self, effort: &str) -> bool {
        matches!(effort, "low" | "high" | "max") || self == Self::DeepSeek && effort == "none"
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    MissingCredential(&'static str),
    InvalidEffort,
    InvalidEndpoint,
    Transport,
    Timeout,
    Cancelled,
    Http { status: u16, code: Option<String> },
    StreamLineTooLarge,
    StreamBodyTooLarge,
    ToolArgumentsTooLarge,
    InvalidToolArguments,
    TooManyToolCalls,
    IncompleteStream,
    InvalidUtf8,
    InvalidToolCall,
    UnexpectedToolCall,
    ToolLimit,
    ToolFailed,
    InvalidToolDefinitions,
    HistoryTooLarge,
    ToolResultTooLarge,
    UsageOverflow,
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for Error {}

pub fn request_body(
    vendor: Vendor,
    model: &str,
    messages: &[Value],
    effort: &str,
) -> Result<Value, Error> {
    if !vendor.valid_effort(effort) {
        return Err(Error::InvalidEffort);
    }
    let mut body = json!({
        "model": model, "messages": messages, "temperature": 0.2,
        "stream": true, "stream_options": {"include_usage": true}
    });
    if effort == "none" {
        body["thinking"] = json!({"type": "disabled"});
    } else if vendor == Vendor::DeepSeek {
        body["thinking"] = json!({"type": "enabled", "reasoning_effort": effort});
    } else {
        body["thinking"] = json!({"type": "enabled"});
        body["reasoning_effort"] = json!(effort);
    }
    Ok(body)
}

#[derive(Clone, Debug, PartialEq)]
pub enum Delta {
    Text(String),
    Reasoning(String),
}
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub arguments: Map<String, Value>,
}
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Completion {
    pub text: String,
    pub reasoning: String,
    pub model: Option<String>,
    pub usage: Option<Value>,
    pub finish_reason: Option<String>,
    pub tool_calls: Vec<ToolCall>,
    pub malformed_chunks: usize,
}
#[derive(Default)]
struct PendingCall {
    id: String,
    name: String,
    arguments: String,
}
#[derive(Default)]
pub struct Accumulator {
    completion: Completion,
    calls: BTreeMap<usize, PendingCall>,
    text_filter: SecretFilter,
    reasoning_filter: SecretFilter,
}
#[derive(Default)]
struct SecretFilter {
    pending: String,
}
impl SecretFilter {
    fn push(&mut self, fragment: &str, key: &str) -> String {
        self.pending.push_str(fragment);
        self.pending = scrub(&self.pending, key);
        let hold = key.chars().count().max(8).saturating_sub(1);
        let count = self.pending.chars().count();
        if count <= hold {
            return String::new();
        }
        let cut = self
            .pending
            .char_indices()
            .nth(count - hold)
            .map(|(i, _)| i)
            .unwrap_or(self.pending.len());
        self.pending.drain(..cut).collect()
    }
    fn flush(&mut self, key: &str) -> String {
        scrub(&std::mem::take(&mut self.pending), key)
    }
}
impl Accumulator {
    pub fn absorb(
        &mut self,
        payload: &str,
        key: &str,
        mut on_delta: impl FnMut(Delta),
    ) -> Result<(), Error> {
        let Ok(chunk) = serde_json::from_str::<Value>(payload) else {
            self.completion.malformed_chunks += 1;
            return Ok(());
        };
        if let Some(model) = chunk.get("model").and_then(Value::as_str) {
            self.completion.model = Some(scrub(model, key));
        }
        if let Some(usage) = chunk.get("usage").filter(|v| v.is_object()) {
            self.completion.usage = Some(scrub_json(usage.clone(), key));
        }
        let Some(choice) = chunk
            .get("choices")
            .and_then(Value::as_array)
            .and_then(|a| a.first())
        else {
            return Ok(());
        };
        if let Some(reason) = choice.get("finish_reason").and_then(Value::as_str) {
            self.completion.finish_reason = Some(scrub(reason, key));
        }
        let Some(delta) = choice.get("delta") else {
            return Ok(());
        };
        for (field, reasoning) in [("reasoning_content", true), ("content", false)] {
            if let Some(s) = delta
                .get(field)
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
            {
                let safe = if reasoning {
                    self.reasoning_filter.push(s, key)
                } else {
                    self.text_filter.push(s, key)
                };
                if !safe.is_empty() {
                    if reasoning {
                        self.completion.reasoning.push_str(&safe);
                        on_delta(Delta::Reasoning(safe));
                    } else {
                        self.completion.text.push_str(&safe);
                        on_delta(Delta::Text(safe));
                    }
                }
            }
        }
        if let Some(calls) = delta.get("tool_calls").and_then(Value::as_array) {
            for call in calls {
                let index = call.get("index").and_then(Value::as_u64).unwrap_or(0) as usize;
                if index >= MAX_TOOL_CALLS {
                    return Err(Error::TooManyToolCalls);
                }
                if !self.calls.contains_key(&index) && self.calls.len() >= MAX_TOOL_CALLS {
                    return Err(Error::TooManyToolCalls);
                }
                let target = self.calls.entry(index).or_default();
                if let Some(id) = call
                    .get("id")
                    .and_then(Value::as_str)
                    .filter(|s| !s.is_empty())
                {
                    target.id = scrub(id, key);
                }
                if let Some(function) = call.get("function") {
                    if let Some(name) = function
                        .get("name")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                    {
                        target.name = scrub(name, key);
                    }
                    if let Some(fragment) = function.get("arguments").and_then(Value::as_str) {
                        if target.arguments.len().saturating_add(fragment.len()) > TOOL_ARGUMENT_MAX
                        {
                            return Err(Error::ToolArgumentsTooLarge);
                        }
                        target.arguments.push_str(fragment);
                    }
                }
            }
        }
        Ok(())
    }
    pub fn flush(&mut self, key: &str, mut on_delta: impl FnMut(Delta)) {
        let text = self.text_filter.flush(key);
        if !text.is_empty() {
            self.completion.text.push_str(&text);
            on_delta(Delta::Text(text));
        }
        let reasoning = self.reasoning_filter.flush(key);
        if !reasoning.is_empty() {
            self.completion.reasoning.push_str(&reasoning);
            on_delta(Delta::Reasoning(reasoning));
        }
    }
    pub fn finish(mut self, key: &str) -> Result<Completion, Error> {
        self.flush(key, |_| {});
        for (_, call) in self.calls {
            let arguments = serde_json::from_str::<Map<String, Value>>(&call.arguments)
                .map_err(|_| Error::InvalidToolArguments)?;
            if call.name.is_empty() || call.id.is_empty() {
                return Err(Error::InvalidToolCall);
            }
            let arguments = arguments
                .into_iter()
                .map(|(k, v)| (scrub(&k, key), scrub_json(v, key)))
                .collect();
            self.completion.tool_calls.push(ToolCall {
                id: call.id,
                name: call.name,
                arguments,
            });
        }
        Ok(self.completion)
    }
}
fn scrub_json(value: Value, key: &str) -> Value {
    match value {
        Value::String(s) => Value::String(scrub(&s, key)),
        Value::Array(a) => Value::Array(a.into_iter().map(|v| scrub_json(v, key)).collect()),
        Value::Object(m) => Value::Object(
            m.into_iter()
                .map(|(k, v)| (scrub(&k, key), scrub_json(v, key)))
                .collect(),
        ),
        other => other,
    }
}
fn scrub(text: &str, key: &str) -> String {
    let mut out = if key.is_empty() {
        text.to_owned()
    } else {
        text.replace(key, "***")
    };
    if let Some(tail) = key.get(key.len().saturating_sub(4)..) {
        if tail.len() == 4 {
            out = out.replace(&format!("****{tail}"), "****");
        }
    }
    out
}

/// Incremental SSE decoder. Data is processed as bytes to cap a line before UTF-8 allocation.
#[derive(Default)]
pub struct SseDecoder {
    pending: Vec<u8>,
    total: usize,
    done: bool,
}
impl SseDecoder {
    pub fn push(&mut self, bytes: &[u8]) -> Result<Vec<String>, Error> {
        self.total = self.total.saturating_add(bytes.len());
        if self.total > STREAM_BODY_MAX {
            return Err(Error::StreamBodyTooLarge);
        }
        let mut payloads = Vec::new();
        for &byte in bytes {
            if byte == b'\n' {
                if self.pending.len() > STREAM_LINE_MAX {
                    return Err(Error::StreamLineTooLarge);
                }
                let line = std::str::from_utf8(&self.pending)
                    .map_err(|_| Error::InvalidUtf8)?
                    .trim();
                if let Some(data) = line.strip_prefix("data:") {
                    let data = data.trim();
                    if data == "[DONE]" {
                        self.done = true;
                    } else if !self.done {
                        payloads.push(data.to_owned());
                    }
                }
                self.pending.clear();
            } else {
                if self.pending.len() >= STREAM_LINE_MAX {
                    return Err(Error::StreamLineTooLarge);
                }
                self.pending.push(byte);
            }
        }
        Ok(payloads)
    }
    pub fn done(&self) -> bool {
        self.done
    }
}

/// One request only. The caller owns history, tool gate, and the 24-step loop.
/// Cancel watch changes abort the in-flight stream via Tokio's select loop.
pub async fn stream_once(
    vendor: Vendor,
    body: Value,
    cancel: watch::Receiver<bool>,
    timeout: Duration,
    on_delta: impl FnMut(Delta),
) -> Result<Completion, Error> {
    stream_at(vendor, vendor.endpoint(), body, cancel, timeout, on_delta).await
}

/// Loopback transport override exclusively for deterministic integration tests.
#[cfg(feature = "local-test-server")]
pub async fn stream_once_local(
    vendor: Vendor,
    endpoint: &str,
    body: Value,
    cancel: watch::Receiver<bool>,
    timeout: Duration,
    on_delta: impl FnMut(Delta),
) -> Result<Completion, Error> {
    validate_local_endpoint(endpoint)?;
    stream_at(vendor, endpoint, body, cancel, timeout, on_delta).await
}

#[cfg(feature = "local-test-server")]
fn validate_local_endpoint(endpoint: &str) -> Result<(), Error> {
    let url = reqwest::Url::parse(endpoint).map_err(|_| Error::InvalidEndpoint)?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Err(Error::InvalidEndpoint);
    }
    Ok(())
}

/// The host owns permission decisions and tool implementations. Returning an error
/// aborts the turn without putting the error text in a provider request or log.
pub trait ToolGate {
    /// OpenAI-compatible function definitions this gate is prepared to execute.
    fn definitions(&self) -> Vec<Value>;
    fn execute<'a>(&'a mut self, call: &'a ToolCall) -> BoxFuture<'a, Result<Value, ()>>;
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct TokenUsage {
    pub prompt_tokens: u64,
    pub completion_tokens: u64,
}
impl TokenUsage {
    fn add(&mut self, usage: Option<&Value>) -> Result<(), Error> {
        let Some(usage) = usage else {
            return Ok(());
        };
        for (field, total) in [
            ("prompt_tokens", &mut self.prompt_tokens),
            ("completion_tokens", &mut self.completion_tokens),
        ] {
            if let Some(value) = usage.get(field) {
                let count = value.as_u64().ok_or(Error::UsageOverflow)?;
                *total = total.checked_add(count).ok_or(Error::UsageOverflow)?;
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct TurnOutcome {
    pub text: String,
    pub reasoning: String,
    pub model: Option<String>,
    pub usage: TokenUsage,
    /// Number of provider requests, including the final response request.
    pub requests: usize,
}

/// A successful turn appends Python-compatible chat-completions messages to
/// `history`. On error, history is unchanged. A gate may already have made a
/// side effect before a later request fails, so callers must not blindly retry.
#[allow(clippy::too_many_arguments)]
pub async fn run_turn(
    vendor: Vendor,
    model: &str,
    effort: &str,
    history: &mut Vec<Value>,
    prompt: &str,
    gate: Option<&mut dyn ToolGate>,
    cancel: watch::Receiver<bool>,
    deadline: Duration,
    on_delta: impl FnMut(Delta),
) -> Result<TurnOutcome, Error> {
    run_turn_at(
        vendor,
        vendor.endpoint(),
        model,
        effort,
        history,
        prompt,
        gate,
        cancel,
        deadline,
        on_delta,
    )
    .await
}

/// Loopback-only variant for deterministic integration tests.
#[cfg(feature = "local-test-server")]
#[allow(clippy::too_many_arguments)]
pub async fn run_turn_local(
    vendor: Vendor,
    endpoint: &str,
    model: &str,
    effort: &str,
    history: &mut Vec<Value>,
    prompt: &str,
    gate: Option<&mut dyn ToolGate>,
    cancel: watch::Receiver<bool>,
    deadline: Duration,
    on_delta: impl FnMut(Delta),
) -> Result<TurnOutcome, Error> {
    validate_local_endpoint(endpoint)?;
    run_turn_at(
        vendor, endpoint, model, effort, history, prompt, gate, cancel, deadline, on_delta,
    )
    .await
}

fn validate_tool_definitions(definitions: &[Value]) -> Result<(), Error> {
    if definitions.len() > MAX_TOOL_CALLS {
        return Err(Error::InvalidToolDefinitions);
    }
    let mut names = std::collections::BTreeSet::new();
    let mut bytes = 0usize;
    for definition in definitions {
        bytes = bytes
            .checked_add(
                serde_json::to_vec(definition)
                    .map_err(|_| Error::InvalidToolDefinitions)?
                    .len(),
            )
            .ok_or(Error::InvalidToolDefinitions)?;
        if bytes > TOOL_ARGUMENT_MAX {
            return Err(Error::InvalidToolDefinitions);
        }
        let name = definition
            .pointer("/function/name")
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty());
        if definition.get("type").and_then(Value::as_str) != Some("function")
            || name.is_none_or(|name| !names.insert(name.to_owned()))
        {
            return Err(Error::InvalidToolDefinitions);
        }
    }
    Ok(())
}

fn check_history(messages: &[Value]) -> Result<(), Error> {
    if messages.len() > MAX_HISTORY_MESSAGES {
        return Err(Error::HistoryTooLarge);
    }
    let mut bytes = 0usize;
    for message in messages {
        bytes = bytes
            .checked_add(
                serde_json::to_vec(message)
                    .map_err(|_| Error::HistoryTooLarge)?
                    .len(),
            )
            .ok_or(Error::HistoryTooLarge)?;
        if bytes > MAX_HISTORY_BYTES {
            return Err(Error::HistoryTooLarge);
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn run_turn_at(
    vendor: Vendor,
    endpoint: &str,
    model: &str,
    effort: &str,
    history: &mut Vec<Value>,
    prompt: &str,
    mut gate: Option<&mut dyn ToolGate>,
    mut cancel: watch::Receiver<bool>,
    deadline: Duration,
    mut on_delta: impl FnMut(Delta),
) -> Result<TurnOutcome, Error> {
    let deadline = deadline.min(MAX_TURN_DURATION);
    let started = Instant::now();
    let definitions = gate.as_ref().map(|g| g.definitions()).unwrap_or_default();
    validate_tool_definitions(&definitions)?;
    let allowed: std::collections::BTreeSet<&str> = definitions
        .iter()
        .filter_map(|d| d.pointer("/function/name").and_then(Value::as_str))
        .collect();
    let key =
        std::env::var(vendor.env_var()).map_err(|_| Error::MissingCredential(vendor.env_var()))?;
    if key.is_empty() {
        return Err(Error::MissingCredential(vendor.env_var()));
    }
    let mut messages = history.clone();
    messages.push(json!({"role":"user","content":prompt}));
    check_history(&messages)?;
    let mut outcome = TurnOutcome {
        text: String::new(),
        reasoning: String::new(),
        model: None,
        usage: TokenUsage::default(),
        requests: 0,
    };
    loop {
        if *cancel.borrow() {
            return Err(Error::Cancelled);
        }
        let remaining = deadline
            .checked_sub(started.elapsed())
            .ok_or(Error::Timeout)?;
        if remaining.is_zero() {
            return Err(Error::Timeout);
        }
        let mut body = request_body(vendor, model, &messages, effort)?;
        if !definitions.is_empty() {
            body["tools"] = Value::Array(definitions.clone());
            body["tool_choice"] = json!("auto");
        }
        let completion = stream_at(
            vendor,
            endpoint,
            body,
            cancel.clone(),
            remaining,
            &mut on_delta,
        )
        .await?;
        outcome.requests += 1;
        outcome.usage.add(completion.usage.as_ref())?;
        outcome.model = completion.model.or(outcome.model);
        outcome.text.push_str(&completion.text);
        outcome.reasoning.push_str(&completion.reasoning);
        if completion.tool_calls.is_empty() {
            if completion.finish_reason.as_deref() == Some("tool_calls") {
                return Err(Error::InvalidToolCall);
            }
            if completion.finish_reason.as_deref() != Some("stop") {
                return Err(Error::IncompleteStream);
            }
            if *cancel.borrow() {
                return Err(Error::Cancelled);
            }
            messages.push(json!({"role":"assistant","content":completion.text}));
            check_history(&messages)?;
            *history = messages;
            return Ok(outcome);
        }
        if completion.finish_reason.as_deref() != Some("tool_calls")
            || gate.is_none()
            || outcome.requests > MAX_TOOL_STEPS
        {
            return Err(if outcome.requests > MAX_TOOL_STEPS {
                Error::ToolLimit
            } else {
                Error::UnexpectedToolCall
            });
        }
        let mut call_ids = std::collections::BTreeSet::new();
        for call in &completion.tool_calls {
            if !allowed.contains(call.name.as_str()) || !call_ids.insert(call.id.as_str()) {
                return Err(Error::UnexpectedToolCall);
            }
        }
        let calls: Vec<Value> = completion.tool_calls.iter().map(|call| json!({
            "id":call.id, "type":"function", "function":{
                "name":call.name, "arguments":Value::Object(call.arguments.clone()).to_string()
            }
        })).collect();
        messages.push(json!({"role":"assistant","content":completion.text,"tool_calls":calls}));
        check_history(&messages)?;
        for call in &completion.tool_calls {
            if *cancel.borrow() {
                return Err(Error::Cancelled);
            }
            let remaining = deadline
                .checked_sub(started.elapsed())
                .ok_or(Error::Timeout)?;
            if remaining.is_zero() {
                return Err(Error::Timeout);
            }
            let execute = gate.as_deref_mut().expect("checked gate").execute(call);
            let result = tokio::select! {
                biased;
                _ = wait_cancel(&mut cancel) => return Err(Error::Cancelled),
                result = tokio::time::timeout(remaining, execute) => result.map_err(|_| Error::Timeout)?.map_err(|_| Error::ToolFailed)?,
            };
            let content = scrub_json(result, &key).to_string();
            if content.len() > MAX_TOOL_RESULT_BYTES {
                return Err(Error::ToolResultTooLarge);
            }
            messages.push(json!({"role":"tool","tool_call_id":call.id,"content":content}));
            check_history(&messages)?;
        }
    }
}

async fn stream_at(
    vendor: Vendor,
    endpoint: &str,
    body: Value,
    mut cancel: watch::Receiver<bool>,
    timeout: Duration,
    mut on_delta: impl FnMut(Delta),
) -> Result<Completion, Error> {
    let key =
        std::env::var(vendor.env_var()).map_err(|_| Error::MissingCredential(vendor.env_var()))?;
    if key.is_empty() {
        return Err(Error::MissingCredential(vendor.env_var()));
    }
    let client = reqwest::Client::builder()
        .timeout(timeout)
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| Error::Transport)?;
    let run = async {
        let response = client
            .post(endpoint)
            .bearer_auth(&key)
            .json(&body)
            .send()
            .await
            .map_err(map_transport)?;
        let status = response.status();
        let mut stream = response.bytes_stream();
        if !status.is_success() {
            let mut raw = Vec::new();
            while let Some(next) = stream.next().await {
                let bytes = next.map_err(map_transport)?;
                let remaining = ERROR_BODY_MAX.saturating_sub(raw.len());
                raw.extend_from_slice(&bytes[..bytes.len().min(remaining)]);
                if raw.len() >= ERROR_BODY_MAX {
                    break;
                }
            }
            let code = serde_json::from_slice::<Value>(&raw)
                .ok()
                .and_then(|v| v.pointer("/error/code").cloned())
                .map(|v| {
                    if let Some(s) = v.as_str() {
                        s.to_owned()
                    } else {
                        v.to_string()
                    }
                })
                .map(|s| sanitize_code(&scrub(&s, &key)));
            return Err(Error::Http {
                status: status.as_u16(),
                code,
            });
        }
        let mut decoder = SseDecoder::default();
        let mut acc = Accumulator::default();
        while let Some(next) = stream.next().await {
            let bytes = next.map_err(map_transport)?;
            for payload in decoder.push(&bytes)? {
                acc.absorb(&payload, &key, &mut on_delta)?;
            }
            if decoder.done() {
                acc.flush(&key, &mut on_delta);
                return acc.finish(&key);
            }
        }
        Err(Error::IncompleteStream)
    };
    tokio::select! {
        biased;
        _ = wait_cancel(&mut cancel) => Err(Error::Cancelled),
        result = tokio::time::timeout(timeout, run) => result.unwrap_or(Err(Error::Timeout)),
    }
}
async fn wait_cancel(cancel: &mut watch::Receiver<bool>) {
    if *cancel.borrow() {
        return;
    }
    while cancel.changed().await.is_ok() {
        if *cancel.borrow() {
            return;
        }
    }
    std::future::pending::<()>().await;
}
fn sanitize_code(code: &str) -> String {
    code.chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'))
        .take(64)
        .collect()
}
fn map_transport(error: reqwest::Error) -> Error {
    if error.is_timeout() {
        Error::Timeout
    } else {
        Error::Transport
    }
}
