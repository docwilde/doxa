use crate::{Error, ErrorKind, Result};
use serde::{de::{self, MapAccess, SeqAccess, Visitor}, Deserialize};
use serde_json::{Map, Value};
use std::fmt;

// Duplicate object fields are ambiguous policy/protocol evidence, never last-wins.
struct Unique(Value);
impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: de::Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Unique;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result { f.write_str("JSON with unique keys") }
            fn visit_bool<E: de::Error>(self, v: bool) -> std::result::Result<Unique,E> { Ok(Unique(v.into())) }
            fn visit_i64<E: de::Error>(self, v: i64) -> std::result::Result<Unique,E> { Ok(Unique(v.into())) }
            fn visit_u64<E: de::Error>(self, v: u64) -> std::result::Result<Unique,E> { Ok(Unique(v.into())) }
            fn visit_f64<E: de::Error>(self, v: f64) -> std::result::Result<Unique,E> {
                serde_json::Number::from_f64(v).map(|n|Unique(Value::Number(n))).ok_or_else(|| E::custom("nonfinite"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> std::result::Result<Unique,E> { Ok(Unique(v.into())) }
            fn visit_string<E: de::Error>(self, v: String) -> std::result::Result<Unique,E> { Ok(Unique(v.into())) }
            fn visit_unit<E: de::Error>(self) -> std::result::Result<Unique,E> { Ok(Unique(Value::Null)) }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> std::result::Result<Unique,A::Error> {
                let mut values = Vec::new();
                while let Some(Unique(value)) = seq.next_element()? { values.push(value); }
                Ok(Unique(Value::Array(values)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut access: A) -> std::result::Result<Unique,A::Error> {
                let mut map = Map::new();
                while let Some(key) = access.next_key::<String>()? {
                    if map.contains_key(&key) { return Err(de::Error::custom("duplicate JSON field")); }
                    let Unique(value) = access.next_value()?;
                    map.insert(key, value);
                }
                Ok(Unique(Value::Object(map)))
            }
        }
        deserializer.deserialize_any(V)
    }
}
pub(crate) fn parse(bytes: &[u8]) -> Result<Value> {
    serde_json::from_slice::<Unique>(bytes).map(|v|v.0).map_err(|_|Error::protocol("invalid or duplicate JSON"))
}

pub(crate) enum Message { Response(Value), Notification(String), Request(Value, String) }
pub(crate) fn classify(value: Value, expected: u64) -> Result<Message> {
    let map = value.as_object().ok_or_else(|| Error::protocol("JSON-RPC object required"))?;
    if value["jsonrpc"] != "2.0" || map.keys().any(|k| !matches!(k.as_str(),"jsonrpc"|"id"|"result"|"error"|"method"|"params")) {
        return Err(Error::protocol("invalid JSON-RPC envelope"));
    }
    if let Some(method) = value.get("method") {
        let method = method.as_str().filter(|v| !v.is_empty() && v.len() <= 128).ok_or_else(|| Error::protocol("invalid method"))?.to_owned();
        if map.contains_key("result") || map.contains_key("error") || map.get("params").is_some_and(|v| !v.is_object()) { return Err(Error::protocol("invalid server message")); }
        return if let Some(id) = map.get("id") {
            if !id.is_string() && !id.is_i64() && !id.is_u64() { return Err(Error::protocol("invalid server request ID")); }
            Ok(Message::Request(id.clone(), method))
        } else { Ok(Message::Notification(method)) };
    }
    if value["id"].as_u64() != Some(expected) || map.contains_key("params") || map.contains_key("result") == map.contains_key("error") {
        return Err(Error::protocol("response ID or shape mismatch"));
    }
    if let Some(error) = value.get("error") {
        let code = error["code"].as_i64().ok_or_else(|| Error::protocol("invalid remote error"))?;
        if !error["message"].is_string() { return Err(Error::protocol("invalid remote error")); }
        return Err(Error::new(ErrorKind::Remote { code }, true));
    }
    if !value["result"].is_object() { return Err(Error::protocol("result object required")); }
    Ok(Message::Response(value["result"].clone()))
}

pub(crate) fn redact(value: &mut Value, secrets: &[String]) -> Result<()> {
    doxa_vendors::credentials::redact_json(value).map_err(|_|Error::new(ErrorKind::Credential,true))?;
    redact_references(value,secrets)
}
fn redact_references(value: &mut Value, secrets: &[String]) -> Result<()> {
    fn scrub(text: &str, secrets: &[String]) -> String {
        secrets.iter().fold(text.to_owned(), |text, secret| text.replace(secret, "[redacted]"))
    }
    match value {
        Value::String(text) => *text = scrub(text, secrets),
        Value::Array(values) => for value in values { redact_references(value,secrets)?; },
        Value::Object(values) => {
            let old = std::mem::take(values);
            for (key, mut value) in old {
                if secrets.iter().any(|secret|key.contains(secret)) { return Err(Error::protocol("credential reflected in object key")); }
                redact_references(&mut value,secrets)?; values.insert(key, value);
            }
        }
        _ => {},
    }
    Ok(())
}
