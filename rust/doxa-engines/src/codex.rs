//! Incremental normalizer for `codex exec --json` stdout JSONL.
//!
//! Python reference: `doxa/codex.py::_map_line`, `map_event`, `_tool_*`,
//! `_absorb_usage`, and `_turn_failure`. Process exit, secrets, transcript
//! writes, pricing, and thread-id persistence belong to the future adapter.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use crate::EngineEvent;

pub const MAX_LINE_BYTES: usize = 8 * 1024 * 1024;
const RESULT_SUMMARY_CHARS: usize = 280;
const BAD_SAMPLE_CHARS: usize = 120;

#[derive(Debug, PartialEq, Eq)]
pub enum ParseError {
    LineTooLong,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct TokenUsage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_read_input_tokens: u64,
    pub reasoning_output_tokens: u64,
}

/// Caller must supply the same secret-scrubbing policy used for transcript
/// output. There is deliberately no identity/default scrubber constructor.
pub struct CodexJsonlNormalizer {
    scrub: Box<dyn Fn(&str) -> String + Send + Sync>,
    pending: Vec<u8>,
    thread_id: Option<String>,
    started: HashMap<String, Instant>,
    usage: TokenUsage,
    bad_frames: usize,
    bad_sample: String,
    closed: bool,
    num_turns: u64,
}

impl CodexJsonlNormalizer {
    pub fn new(scrub: impl Fn(&str) -> String + Send + Sync + 'static) -> Self {
        Self {
            scrub: Box::new(scrub), pending: Vec::new(), thread_id: None,
            started: HashMap::new(), usage: TokenUsage::default(),
            bad_frames: 0, bad_sample: String::new(), closed: false, num_turns: 0,
        }
    }

    pub fn begin_turn(&mut self) {
        self.pending.clear();
        self.started.clear();
        self.bad_frames = 0;
        self.bad_sample.clear();
        self.closed = false;
        self.num_turns += 1;
    }

    pub fn thread_id(&self) -> Option<&str> { self.thread_id.as_deref() }
    pub fn usage(&self) -> &TokenUsage { &self.usage }
    pub fn bad_frames(&self) -> usize { self.bad_frames }
    pub fn is_closed(&self) -> bool { self.closed }

    /// Accept arbitrary stdout chunks. An oversized line fails explicitly
    /// before it can grow the buffer further; the caller must stop the turn.
    pub fn push_bytes(&mut self, bytes: &[u8]) -> Result<Vec<EngineEvent>, ParseError> {
        let mut events = Vec::new();
        for segment in bytes.split_inclusive(|byte| *byte == b'\n') {
            if self.closed { break; }
            let has_newline = segment.last() == Some(&b'\n');
            let payload = if has_newline { &segment[..segment.len() - 1] } else { segment };
            if self.pending.len().saturating_add(payload.len()) > MAX_LINE_BYTES {
                self.pending.clear();
                return Err(ParseError::LineTooLong);
            }
            self.pending.extend_from_slice(payload);
            if has_newline {
                let line = std::mem::take(&mut self.pending);
                events.extend(self.map_line(&line));
            }
        }
        Ok(events)
    }

    /// `readline()` in the Python adapter also returns a final record that
    /// lacks a newline. An incomplete JSON object is counted as malformed.
    pub fn flush_eof(&mut self) -> Vec<EngineEvent> {
        if self.closed || self.pending.is_empty() { return Vec::new(); }
        let line = std::mem::take(&mut self.pending);
        self.map_line(&line)
    }

    /// Call after process exit or a read failure. `turn.failed`/`error`
    /// already emitted a terminal event, so no second one is emitted.
    pub fn finish_turn(&mut self, duration_ms: Option<u64>, process_error: Option<&str>) -> Vec<EngineEvent> {
        let mut out = self.flush_eof();
        if self.closed { return out; }
        self.closed = true;
        let failure = process_error.map(|s| (self.scrub)(s)).or_else(|| {
            (self.bad_frames > 0).then(|| {
                let noun = if self.bad_frames == 1 { "line was" } else { "lines were" };
                let sample = if self.bad_sample.is_empty() { String::new() } else { format!(" (first: {})", self.bad_sample) };
                format!("{} unreadable {} dropped from codex stdout{}", self.bad_frames, noun, sample)
            })
        });
        if let Some(reason) = &failure { out.push(EngineEvent::new("text_delta", json!({"text": format!("codex: {reason}")}))); }
        let mut data = turn_done_data(duration_ms, self.num_turns, failure.is_some());
        if let Some(reason) = failure { data["error"] = Value::String(reason); }
        out.push(EngineEvent::new("turn_done", data));
        out
    }

    fn map_line(&mut self, raw: &[u8]) -> Vec<EngineEvent> {
        let frame = serde_json::from_slice::<Value>(raw).ok();
        match frame {
            Some(Value::Object(map)) => self.map_frame(&map),
            _ => {
                self.bad_frames += 1;
                if self.bad_sample.is_empty() {
                    self.bad_sample = truncate(&(self.scrub)(&String::from_utf8_lossy(raw)), BAD_SAMPLE_CHARS);
                }
                Vec::new()
            }
        }
    }

    fn map_frame(&mut self, frame: &Map<String, Value>) -> Vec<EngineEvent> {
        let kind = string(frame.get("type"));
        match kind.as_str() {
            "thread.started" => {
                let id = string(frame.get("thread_id"));
                if !id.is_empty() { self.thread_id = Some(id); }
                Vec::new()
            }
            "turn.started" => Vec::new(),
            "turn.completed" => { self.absorb_usage(frame.get("usage")); Vec::new() }
            "turn.failed" | "error" => {
                self.closed = true;
                let message = frame.get("message").or_else(|| frame.get("error"))
                    .filter(|v| !v.is_null()).map(value_string).unwrap_or_else(|| kind.clone());
                let message = truncate(&(self.scrub)(&message), RESULT_SUMMARY_CHARS);
                let mut data = turn_done_data(None, self.num_turns, true);
                data["error"] = Value::String(message.clone());
                vec![EngineEvent::new("text_delta", json!({"text": format!("codex: {message}")})), EngineEvent::new("turn_done", data)]
            }
            "item.started" | "item.updated" | "item.completed" => self.map_item(&kind, frame.get("item")),
            _ => Vec::new(), // Well-formed unknown frames are forward-compatible.
        }
    }

    fn map_item(&mut self, event_kind: &str, item: Option<&Value>) -> Vec<EngineEvent> {
        let Some(item) = item.and_then(Value::as_object) else { return Vec::new(); };
        let item_kind = string(item.get("type"));
        let id = string(item.get("id"));
        if item_kind == "agent_message" || item_kind == "reasoning" || item_kind == "agent_reasoning" {
            if event_kind != "item.completed" { return Vec::new(); }
            let raw = if item_kind == "agent_message" { item.get("text") } else { item.get("text").or_else(|| item.get("summary")) };
            let text = string(raw);
            if text.is_empty() { return Vec::new(); }
            let event = if item_kind == "agent_message" { "text_delta" } else { "reasoning_delta" };
            return vec![EngineEvent::new(event, json!({"text": (self.scrub)(&text)}))];
        }
        if !matches!(item_kind.as_str(), "command_execution" | "file_change" | "mcp_tool_call" | "web_search" | "todo_list" | "patch_apply") { return Vec::new(); }
        let name = if item_kind == "mcp_tool_call" {
            format!("{}/{}", nonempty(item.get("server"), "mcp"), nonempty(item.get("tool"), "tool"))
        } else { item_kind.clone() };
        // Provider-supplied names and IDs reach chips and transcripts too.
        // Keep raw IDs only for internal duration correlation.
        let display_name = (self.scrub)(&name);
        let display_id = (self.scrub)(&id);
        if event_kind == "item.started" {
            self.started.insert(id.clone(), Instant::now());
            return vec![EngineEvent::new("tool_call", json!({"id":display_id,"name":display_name,"input":self.tool_input(&item_kind,item)}))];
        }
        let (summary, is_error) = self.tool_result(&item_kind, item);
        let duration_ms = self.started.get(&id).map(|start| start.elapsed().as_millis() as u64);
        if event_kind == "item.completed" { self.started.remove(&id); }
        vec![EngineEvent::new("tool_result", json!({"id":display_id,"name":display_name,"result_summary":summary,"is_error":is_error,"duration_ms":duration_ms}))]
    }

    fn tool_input(&self, kind: &str, item: &Map<String, Value>) -> Value {
        match kind {
            "command_execution" => json!({"command": (self.scrub)(&string(item.get("command")))}),
            "file_change" => {
                let paths: Vec<String> = item.get("changes").and_then(Value::as_array).into_iter().flatten()
                    .filter_map(Value::as_object).map(|row| (self.scrub)(&string(row.get("path")))).collect();
                json!({"paths":paths})
            }
            "mcp_tool_call" => json!({"arguments": self.scrub_value(&Value::Object(item.get("arguments").and_then(Value::as_object).cloned().unwrap_or_default()))}),
            "todo_list" => json!({"steps": item.get("items").and_then(Value::as_array).map_or(0, Vec::len)}),
            "web_search" => json!({"query": (self.scrub)(&string(item.get("query")))}),
            _ => json!({}),
        }
    }

    fn scrub_value(&self, value: &Value) -> Value {
        match value {
            Value::String(text) => Value::String((self.scrub)(text)),
            Value::Array(items) => Value::Array(items.iter().map(|item| self.scrub_value(item)).collect()),
            Value::Object(fields) => Value::Object(fields.iter().map(|(key, value)| (key.clone(), self.scrub_value(value))).collect()),
            _ => value.clone(),
        }
    }

    fn tool_result(&self, kind: &str, item: &Map<String, Value>) -> (String, bool) {
        if let Some(message) = item.get("error").and_then(Value::as_object).and_then(|v| v.get("message")) {
            if !message.is_null() { return (truncate(&(self.scrub)(&value_string(message)), RESULT_SUMMARY_CHARS), true); }
        }
        let mut failed = matches!(string(item.get("status")).as_str(), "failed" | "error");
        let summary = match kind {
            "command_execution" => {
                let code = item.get("exit_code").and_then(Value::as_i64);
                failed |= code.is_some_and(|n| n != 0);
                let out = truncate(&(self.scrub)(&string(item.get("aggregated_output"))), RESULT_SUMMARY_CHARS);
                if out.is_empty() { format!("exit {}", code.map_or("None".to_owned(), |n| n.to_string())) } else { out }
            }
            "file_change" => format!("{} file(s) changed", item.get("changes").and_then(Value::as_array).map_or(0, Vec::len)),
            "mcp_tool_call" => {
                let texts: Vec<String> = item.get("result").and_then(|v| v.get("content")).and_then(Value::as_array)
                    .into_iter().flatten().filter_map(Value::as_object).map(|row| string(row.get("text"))).collect();
                truncate(&(self.scrub)(&texts.join("\n")), RESULT_SUMMARY_CHARS)
            }
            "todo_list" => {
                let rows = item.get("items").and_then(Value::as_array);
                let done = rows.into_iter().flatten().filter(|row| row.get("completed").and_then(Value::as_bool) == Some(true)).count();
                format!("{done}/{} done", rows.map_or(0, Vec::len))
            }
            _ => truncate(&(self.scrub)(&Value::Object(item.clone()).to_string()), RESULT_SUMMARY_CHARS),
        };
        (summary, failed)
    }

    fn absorb_usage(&mut self, usage: Option<&Value>) {
        let Some(usage) = usage.and_then(Value::as_object) else { return; };
        for (key, total) in [
            ("input_tokens", &mut self.usage.input_tokens),
            ("output_tokens", &mut self.usage.output_tokens),
            ("cached_input_tokens", &mut self.usage.cache_read_input_tokens),
            ("reasoning_output_tokens", &mut self.usage.reasoning_output_tokens),
        ] {
            if let Some(n) = usage.get(key).and_then(Value::as_u64) { *total = total.saturating_add(n); }
        }
    }
}

fn string(value: Option<&Value>) -> String { value.filter(|v| !v.is_null()).map(value_string).unwrap_or_default() }
fn value_string(value: &Value) -> String { value.as_str().map(str::to_owned).unwrap_or_else(|| value.to_string()) }
fn nonempty(value: Option<&Value>, fallback: &str) -> String { let s = string(value); if s.is_empty() { fallback.to_owned() } else { s } }
fn truncate(value: &str, chars: usize) -> String { value.chars().take(chars).collect() }
fn turn_done_data(duration_ms: Option<u64>, num_turns: u64, is_error: bool) -> Value {
    json!({"duration_ms":duration_ms,"cost_usd":null,"session_cost_usd":null,"num_turns":num_turns,"is_error":is_error,"ctx_percentage":null,"ctx_tokens":null,"ctx_max_tokens":null})
}

// Used by the future process adapter to convert a wall-clock Duration.
pub fn duration_ms(duration: Duration) -> u64 { duration.as_millis().min(u128::from(u64::MAX)) as u64 }
