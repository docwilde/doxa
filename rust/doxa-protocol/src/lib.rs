//! Bounded, versioned DOXA daemon protocol v1 frame validation.
//!
//! Both the native daemon and the terminal client can use this crate while
//! Python 1.x is still on the other end of the socket. Unknown optional
//! fields survive decoding so a newer peer does not break an older one.

use serde_json::Value;
use std::fmt;

pub const PROTOCOL_VERSION: u64 = 1;
pub const MAX_FRAME_BYTES: usize = 64 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction { ClientToServer, ServerToClient }

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum WireError {
    FrameTooLarge,
    IncompleteFrame,
    InvalidJson,
    InvalidField(&'static str),
    UnsupportedVersion(u64),
    UnknownType,
}

impl fmt::Display for WireError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::FrameTooLarge => write!(f, "frame exceeds 64 KiB"),
            Self::IncompleteFrame => write!(f, "frame has no newline terminator"),
            Self::InvalidJson => write!(f, "invalid JSON object"),
            Self::InvalidField(field) => write!(f, "invalid {field}"),
            Self::UnsupportedVersion(version) => write!(f, "unsupported protocol v{version}"),
            Self::UnknownType => write!(f, "unknown frame type"),
        }
    }
}

impl std::error::Error for WireError {}

/// Decode one full newline-delimited frame, retaining extension fields.
pub fn decode_line(line: &[u8], direction: Direction) -> Result<Value, WireError> {
    if line.len() > MAX_FRAME_BYTES { return Err(WireError::FrameTooLarge); }
    if !line.ends_with(b"\n") { return Err(WireError::IncompleteFrame); }
    let value: Value = serde_json::from_slice(line).map_err(|_| WireError::InvalidJson)?;
    validate(&value, direction)?;
    Ok(value)
}

/// Encode a validated frame. This refuses output that a Python 1.x peer
/// would drop because it exceeds `peers.MAX_FRAME_BYTES`.
pub fn encode_line(value: &Value, direction: Direction) -> Result<Vec<u8>, WireError> {
    validate(value, direction)?;
    let mut bytes = serde_json::to_vec(value).map_err(|_| WireError::InvalidJson)?;
    bytes.push(b'\n');
    if bytes.len() > MAX_FRAME_BYTES { return Err(WireError::FrameTooLarge); }
    Ok(bytes)
}

pub fn validate(value: &Value, direction: Direction) -> Result<(), WireError> {
    if !value.is_object() { return Err(WireError::InvalidJson); }
    let kind = value["type"].as_str().ok_or(WireError::InvalidField("type"))?;
    match (direction, kind) {
        (Direction::ClientToServer, "attach") => {
            if !value["cursor"].is_null() { unsigned(value, "cursor")?; }
            if let Some(login) = value.get("remote_login") {
                if !login.is_null() && !login.is_string() { return Err(WireError::InvalidField("remote_login")); }
            }
        }
        (Direction::ClientToServer, "prompt") => {
            unsigned(value, "id")?;
            string(value, "text")?;
        }
        (Direction::ClientToServer, "call") => {
            unsigned(value, "id")?;
            nonempty(value, "method")?;
            if let Some(params) = value.get("params") {
                if !params.is_object() { return Err(WireError::InvalidField("params")); }
            }
        }
        (Direction::ServerToClient, "hello") => {
            let version = unsigned(value, "proto")?;
            if version != PROTOCOL_VERSION { return Err(WireError::UnsupportedVersion(version)); }
            nonempty(value, "session_id")?;
            nonempty(value, "cwd")?;
            unsigned(value, "next_seq")?;
            for key in ["model", "engine"] {
                if !value[key].is_null() && !value[key].is_string() { return Err(WireError::InvalidField(key)); }
            }
        }
        (Direction::ServerToClient, "event") => {
            unsigned(value, "seq")?;
            if !value["turn"].is_null() && !value["turn"].is_string() { return Err(WireError::InvalidField("turn")); }
            nonempty(&value["event"], "type")?;
            if !value["event"]["data"].is_object() { return Err(WireError::InvalidField("event.data")); }
        }
        (Direction::ServerToClient, "reply") => {
            unsigned(value, "id")?;
            if !value["ok"].is_boolean() { return Err(WireError::InvalidField("ok")); }
        }
        _ => return Err(WireError::UnknownType),
    }
    Ok(())
}

fn unsigned(value: &Value, key: &'static str) -> Result<u64, WireError> {
    value[key].as_u64().ok_or(WireError::InvalidField(key))
}

fn string<'a>(value: &'a Value, key: &'static str) -> Result<&'a str, WireError> {
    value[key].as_str().ok_or(WireError::InvalidField(key))
}

fn nonempty<'a>(value: &'a Value, key: &'static str) -> Result<&'a str, WireError> {
    let text = string(value, key)?;
    if text.is_empty() { Err(WireError::InvalidField(key)) } else { Ok(text) }
}

/// Incremental line splitter. A peer can send partial chunks; the cap is
/// enforced before extending the buffer. Each returned line includes `\n`.
#[derive(Default)]
pub struct LineBuffer { pending: Vec<u8> }

impl LineBuffer {
    pub fn push(&mut self, bytes: &[u8]) -> Result<Vec<Vec<u8>>, WireError> {
        let mut lines = Vec::new();
        for segment in bytes.split_inclusive(|byte| *byte == b'\n') {
            if self.pending.len().saturating_add(segment.len()) > MAX_FRAME_BYTES {
                self.pending.clear();
                return Err(WireError::FrameTooLarge);
            }
            self.pending.extend_from_slice(segment);
            if segment.ends_with(b"\n") { lines.push(std::mem::take(&mut self.pending)); }
        }
        Ok(lines)
    }

    pub fn finish(&self) -> Result<(), WireError> {
        if self.pending.is_empty() { Ok(()) } else { Err(WireError::IncompleteFrame) }
    }
}
