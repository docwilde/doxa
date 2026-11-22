//! Offline request preparation for the matched development pilot. The future
//! endpoint must reserve the receipt's exact wire request before dispatch.

use crate::{
    bounded, invalid, parse_response, prepare, strict, Config, Outcome, Prepared, Provider, Reason,
    RoutingInput, JEV_MODEL,
};
use doxa_vendors::{request_body, Vendor};
use serde::Serialize;
use serde_json::{
    value::{to_raw_value, RawValue},
    Value,
};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    io,
};

const INPUT_LIMIT: usize = 20_000;
const OUTPUT_LIMIT: u64 = 32_768;
const DECISION_RESPONSE_LIMIT: usize = 64 * 1024;
const REPLACEMENTS: [&str; 3] = ["model", "reasoning_effort", "thinking"];
const REQUEST_FIELDS: &[&str] = &[
    "model",
    "messages",
    "tools",
    "tool_choice",
    "max_tokens",
    "stream",
    "stream_options",
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "response_format",
    "logprobs",
    "top_logprobs",
    "thinking",
    "reasoning_effort",
    "n",
];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Target {
    FlashNone,
    ProHigh,
}

impl Target {
    fn id(self) -> &'static str {
        match self {
            Self::FlashNone => "flash-direct",
            Self::ProHigh => "pro-thinking",
        }
    }

    fn model(self) -> &'static str {
        match self {
            Self::FlashNone => "deepseek-flash",
            Self::ProHigh => "deepseek-v4-pro",
        }
    }

    fn effort(self) -> &'static str {
        match self {
            Self::FlashNone => "none",
            Self::ProHigh => "high",
        }
    }
}

pub struct Request {
    fields: BTreeMap<String, Box<RawValue>>,
    input: Value,
    inbound_sha256: String,
    output_limit: u64,
}

#[derive(Debug, Serialize)]
pub struct Receipt {
    pub model: &'static str,
    pub effort: &'static str,
    pub inbound_sha256: String,
    pub request_sha256: String,
    pub input_bound: usize,
    pub output_limit: u64,
    pub replaced_fields: Vec<&'static str>,
}

pub struct Forward {
    body: Vec<u8>,
    receipt: Receipt,
}

impl Forward {
    pub fn body(&self) -> &[u8] {
        &self.body
    }

    pub fn receipt(&self) -> &Receipt {
        &self.receipt
    }
}

pub struct Route<'a> {
    config: &'a Config,
    decision: Prepared,
    flash: Forward,
    pro: Forward,
}

impl Route<'_> {
    pub fn decision(&self) -> &Prepared {
        &self.decision
    }

    /// Consume a decision response already obtained for this prepared request.
    /// Low confidence, ambiguous accounting and invalid answers stop this call.
    pub fn finish(self, response: &[u8], latency_ms: u64) -> io::Result<(Forward, Outcome)> {
        let (forward, outcome) = self.observe(response, latency_ms)?;
        forward.map(|forward| (forward, outcome)).ok_or_else(|| {
            invalid("pilot decision did not establish a selected candidate and bounded usage")
        })
    }

    /// Report verified usage even when the result cannot authorize a repair.
    pub fn observe(self, response: &[u8], latency_ms: u64) -> io::Result<(Option<Forward>, Outcome)> {
        if response.len() > DECISION_RESPONSE_LIMIT {
            return Err(invalid("pilot decision response exceeds the byte limit"));
        }
        let response =
            strict::parse(response).map_err(|_| invalid("invalid pilot decision JSON"))?;
        let result = parse_response(self.config, &self.decision, &response, latency_ms);
        if result.verified_response.is_none() {
            return Err(invalid("pilot decision response could not be verified"));
        }
        let bounded_usage = result
            .usage
            .as_ref()
            .is_some_and(|usage| usage.input_tokens <= self.decision.input_token_reservation);
        if result.reason != Reason::Selected || !bounded_usage {
            return Ok((None, result));
        }
        match result.candidate_id.as_str() {
            "flash-direct" => Ok((Some(self.flash), result)),
            "pro-thinking" => Ok((Some(self.pro), result)),
            _ => Err(invalid("pilot decision selected an unknown candidate")),
        }
    }
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn only_fields(value: &Value, allowed: &[&str]) -> bool {
    value
        .as_object()
        .is_some_and(|fields| fields.keys().all(|key| allowed.contains(&key.as_str())))
}

fn identifier(value: &Value) -> Option<&str> {
    value.as_str().filter(|value| {
        !value.is_empty()
            && value.len() <= 128
            && value
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    })
}

fn text_content(content: &Value) -> bool {
    content.is_string()
        || content.as_array().is_some_and(|parts| {
            !parts.is_empty()
                && parts.iter().all(|part| {
                    only_fields(part, &["type", "text"])
                        && part["type"] == "text"
                        && part["text"].is_string()
                })
        })
}

fn validate_messages(messages: &Value) -> bool {
    let Some(messages) = messages.as_array().filter(|messages| !messages.is_empty()) else {
        return false;
    };
    messages
        .iter()
        .all(|message| match message["role"].as_str() {
            Some("system") => {
                only_fields(message, &["role", "name", "content"]) && message["content"].is_string()
            }
            Some("user") => {
                only_fields(message, &["role", "name", "content"])
                    && text_content(&message["content"])
            }
            Some("tool") => {
                only_fields(message, &["role", "tool_call_id", "content"])
                    && message["tool_call_id"]
                        .as_str()
                        .is_some_and(|id| !id.is_empty())
                    && text_content(&message["content"])
            }
            Some("assistant") => {
                only_fields(
                    message,
                    &["role", "name", "content", "reasoning_content", "tool_calls"],
                ) && (message["reasoning_content"].is_null()
                    || message["reasoning_content"].is_string())
                    && (message["content"].is_string() || message["content"].is_null())
                    && (message["tool_calls"].is_null()
                        || message["tool_calls"].as_array().is_some_and(|calls| {
                            calls.iter().all(|call| {
                                only_fields(call, &["id", "index", "type", "function"])
                                    && call["type"] == "function"
                                    && call["id"].as_str().is_some_and(|id| !id.is_empty())
                                    && only_fields(&call["function"], &["name", "arguments"])
                                    && identifier(&call["function"]["name"]).is_some()
                                    && call["function"]["arguments"].is_string()
                            })
                        }))
            }
            _ => false,
        })
}

fn validate_tools(tools: &Value, choice: &Value) -> bool {
    let mut names = BTreeSet::new();
    (tools.is_null()
        || tools.as_array().is_some_and(|tools| {
            tools.iter().all(|tool| {
                only_fields(tool, &["type", "function"])
                    && tool["type"] == "function"
                    && only_fields(
                        &tool["function"],
                        &["name", "description", "parameters", "strict"],
                    )
                    && identifier(&tool["function"]["name"]).is_some_and(|name| names.insert(name))
            })
        }))
        && (choice.is_null() || matches!(choice.as_str(), Some("none" | "auto")))
}

impl Request {
    pub fn parse(raw: &[u8]) -> io::Result<Self> {
        if raw.len() > INPUT_LIMIT {
            return Err(invalid("pilot request exceeds the byte limit"));
        }
        let input = strict::parse(raw).map_err(|_| invalid("invalid pilot request JSON"))?;
        let output_limit = input["max_tokens"]
            .as_u64()
            .filter(|cap| (1..=OUTPUT_LIMIT).contains(cap))
            .ok_or_else(|| invalid("an explicit pilot max_tokens allowance is required"))?;
        if !only_fields(&input, REQUEST_FIELDS)
            || !input["model"]
                .as_str()
                .is_some_and(|model| bounded(model, 128))
            || input.get("n").is_some_and(|n| n.as_u64() != Some(1))
            || !validate_messages(&input["messages"])
            || !validate_tools(&input["tools"], &input["tool_choice"])
        {
            return Err(invalid("unsupported pilot request shape"));
        }
        // Use the strict parsed form only for validation. The wire fields keep
        // exact nested messages, tool values and numeric spelling.
        Ok(Self {
            fields: serde_json::from_slice(raw)
                .map_err(|_| invalid("invalid pilot request fields"))?,
            input,
            inbound_sha256: digest(raw),
            output_limit,
        })
    }

    pub fn for_fixed(&self, target: Target) -> io::Result<Forward> {
        if target == Target::ProHigh
            && self.input["tools"].is_array()
            && self.input["messages"].as_array().is_some_and(|messages| {
                messages.iter().any(|message| {
                    message["role"] == "assistant" && !message["reasoning_content"].is_string()
                })
            })
        {
            return Err(invalid(
                "thinking-mode tools require prior assistant reasoning",
            ));
        }
        let profile = request_body(Vendor::DeepSeek, target.model(), &[], target.effort())
            .map_err(|_| invalid("invalid frozen pilot provider settings"))?;
        let mut fields = self.fields.clone();
        let mut replaced_fields = Vec::new();
        for field in REPLACEMENTS {
            let replacement = profile.get(field).map(to_raw_value).transpose()?;
            if fields.get(field).map(|raw| raw.get()) == replacement.as_ref().map(|raw| raw.get()) {
                continue;
            }
            replaced_fields.push(field);
            if let Some(replacement) = replacement {
                fields.insert(field.to_owned(), replacement);
            } else {
                fields.remove(field);
            }
        }
        let body = serde_json::to_vec(&fields)?;
        if body.len() > INPUT_LIMIT {
            return Err(invalid("forwarded pilot request exceeds the input bound"));
        }
        Ok(Forward {
            receipt: Receipt {
                model: target.model(),
                effort: target.effort(),
                inbound_sha256: self.inbound_sha256.clone(),
                request_sha256: digest(&body),
                input_bound: body.len(),
                output_limit: self.output_limit,
                replaced_fields,
            },
            body,
        })
    }

    pub fn for_jev<'a>(&self, config: &'a Config) -> io::Result<Route<'a>> {
        let targets = [Target::FlashNone, Target::ProHigh];
        if config.jev_model != JEV_MODEL
            || config.candidates.len() != targets.len()
            || !targets.iter().all(|target| {
                config.candidate(target.id()).is_some_and(|row| {
                    row.provider == Provider::Deepseek
                        && row.model == target.model()
                        && row.effort == target.effort()
                })
            })
        {
            return Err(invalid(
                "Jev must route only between the frozen pilot candidates",
            ));
        }
        // Both models must be eligible before asking Jev.
        let flash = self.for_fixed(Target::FlashNone)?;
        let pro = self.for_fixed(Target::ProHigh)?;
        let messages = self.input["messages"]
            .as_array()
            .ok_or_else(|| invalid("missing pilot messages"))?;
        let mut public_text: Vec<String> = messages
            .iter()
            .rev()
            .filter_map(|message| {
                let role = message["role"]
                    .as_str()
                    .filter(|role| matches!(*role, "user" | "assistant"))?;
                let content = &message["content"];
                let text = content.as_str().map(str::to_owned).or_else(|| {
                    content.as_array().map(|parts| {
                        parts
                            .iter()
                            .filter_map(|part| part["text"].as_str())
                            .collect::<Vec<_>>()
                            .join("\n")
                    })
                })?;
                Some(format!("{role}: {text}"))
            })
            .take(5)
            .collect();
        public_text.reverse();
        let summary = public_text.join("\n");
        let mut start = summary.len().saturating_sub(config.max_input_bytes / 2);
        while !summary.is_char_boundary(start) {
            start += 1;
        }
        let decision = prepare(
            config,
            &RoutingInput {
                summary: summary[start..].to_owned(),
                estimated_input_tokens: flash.receipt.input_bound.max(pro.receipt.input_bound)
                    as u64,
                max_output_tokens: self.output_limit,
                requires_tools: self.input["tools"]
                    .as_array()
                    .is_some_and(|rows| !rows.is_empty()),
                allowed_candidate_ids: targets
                    .iter()
                    .map(|target| target.id().to_owned())
                    .collect(),
            },
        )?;
        if decision.eligible_ids.len() != targets.len() {
            return Err(invalid("both pilot candidates must be eligible for Jev"));
        }
        Ok(Route {
            config,
            decision,
            flash,
            pro,
        })
    }
}
