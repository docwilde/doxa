//! Preserve JSON's object shape by refusing duplicate keys before Value parsing.
use serde::de::{self, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer};
use serde_json::{Map, Number, Value};
use std::fmt;

struct Strict(Value);
impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Json;
        impl<'de> Visitor<'de> for Json {
            type Value = Strict;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result { write!(f, "JSON without duplicate keys") }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Strict,E> { Ok(Strict(Value::Bool(v))) }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Strict,E> { Ok(Strict(Value::Number(v.into()))) }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Strict,E> { Ok(Strict(Value::Number(v.into()))) }
            fn visit_f64<E: de::Error>(self, v: f64) -> Result<Strict,E> {
                Number::from_f64(v).map(|n|Strict(Value::Number(n))).ok_or_else(||E::custom("nonfinite JSON number"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Strict,E> { Ok(Strict(Value::String(v.into()))) }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Strict,E> { Ok(Strict(Value::String(v))) }
            fn visit_none<E: de::Error>(self) -> Result<Strict,E> { Ok(Strict(Value::Null)) }
            fn visit_unit<E: de::Error>(self) -> Result<Strict,E> { Ok(Strict(Value::Null)) }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Strict,A::Error> {
                let mut values = Vec::new();
                while let Some(v) = seq.next_element::<Strict>()? { values.push(v.0); }
                Ok(Strict(Value::Array(values)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Strict,A::Error> {
                let mut values = Map::new();
                while let Some(key) = map.next_key::<String>()? {
                    if values.contains_key(&key) { return Err(de::Error::custom("duplicate JSON key")); }
                    values.insert(key, map.next_value::<Strict>()?.0);
                }
                Ok(Strict(Value::Object(values)))
            }
        }
        deserializer.deserialize_any(Json)
    }
}

pub fn parse(bytes: &[u8]) -> Result<Value, serde_json::Error> {
    serde_json::from_slice::<Strict>(bytes).map(|value|value.0)
}

#[cfg(test)]
mod tests {
    #[test]
    fn duplicate_keys_and_nonfinite_values_are_refused_recursively() {
        for input in [r#"{"a":1,"a":2}"#, r#"{"x":[{"a":1,"a":2}]}"#, r#"{"a":NaN}"#, r#"{"a":1e999}"#] {
            assert!(super::parse(input.as_bytes()).is_err());
        }
        assert!(super::parse(br#"{"a":null,"b":[true,1,-1,0.3,"x"]}"#).is_ok());
    }
}
