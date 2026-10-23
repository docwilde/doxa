//! Scrub review prose without interpreting JSON syntax or host numeric facts.
use serde_json::Value;

pub(super) fn scrub(snapshot: Value,
    scrubber: &mut impl FnMut(&str) -> Result<String, String>) -> Result<Value, String> {
    scrub_with_limit(snapshot,doxa_fleet::judge::MAX_INPUT,scrubber)
}
pub(super) fn scrub_with_limit(mut snapshot:Value,limit:usize,
    scrubber:&mut impl FnMut(&str)->Result<String,String>)->Result<Value,String>{
    let bounded = |value: &Value| serde_json::to_vec(value)
        .is_ok_and(|bytes| bytes.len() <= limit);
    if !bounded(&snapshot) { return Err("Fleet review snapshot exceeds bounds".into()); }
    fn visit(value: &mut Value, depth: usize,
        scrubber: &mut impl FnMut(&str) -> Result<String, String>) -> Result<(), String> {
        if depth > 64 { return Err("Fleet review snapshot nesting exceeds bounds".into()); }
        match value {
            Value::String(text) => *text = scrubber(text)?,
            Value::Array(rows) => for row in rows { visit(row, depth + 1, scrubber)?; },
            Value::Object(rows) => for (key, row) in rows {
                // Never leak a secret in a key or silently change its identity.
                if scrubber(key)? != *key { return Err("Fleet review snapshot key was redacted".into()); }
                visit(row, depth + 1, scrubber)?;
            },
            _ => {},
        }
        Ok(())
    }
    visit(&mut snapshot, 0, scrubber)?;
    if !bounded(&snapshot) { return Err("Scrubbed fleet review snapshot exceeds bounds".into()); }
    Ok(snapshot)
}

#[cfg(test)]
mod tests {
    use super::scrub;
    use serde_json::json;

    #[test]
    fn strings_are_scrubbed_with_json_escaping_and_numeric_facts_stay_typed() {
        let input=json!({"cost_usd":0.016003199999999998,"input_tokens":12345,
            "items":[{"body":"private\nquoted \"text\""}],"running":false,"missing":null});
        let mut seen=Vec::new();
        let clean=scrub(input.clone(),&mut|text|{seen.push(text.to_owned());Ok(text.replace("private","a \"redacted\" value\n"))}).unwrap();
        assert!(serde_json::from_slice::<serde_json::Value>(&serde_json::to_vec(&clean).unwrap()).is_ok());
        assert!(!clean.to_string().contains("private"));
        for key in ["cost_usd","input_tokens","running","missing"] { assert_eq!(clean[key],input[key]); }
        assert!(!seen.iter().any(|text|text=="12345"||text.starts_with("0.016")));
    }

    #[test]
    fn redacted_keys_or_failed_scrubbing_withhold_the_entire_snapshot() {
        assert!(scrub(json!({"private-key":"value"}),&mut|text|Ok(text.replace("private-key","redacted"))).is_err());
        assert!(scrub(json!({"body":"private"}),&mut|_|Err("scrub unavailable".into())).is_err());
    }

    #[test]
    fn input_output_and_nesting_bounds_are_enforced() {
        assert!(scrub(json!({"body":"x".repeat(doxa_fleet::judge::MAX_INPUT)}),&mut|s|Ok(s.to_owned())).is_err());
        assert!(scrub(json!({"body":"x"}),&mut|s|Ok(if s=="x"{"x".repeat(doxa_fleet::judge::MAX_INPUT)}else{s.to_owned()})).is_err());
        let mut nested=json!(false);for _ in 0..66 {nested=json!([nested]);}
        assert!(scrub(nested,&mut|s|Ok(s.to_owned())).is_err());
    }
}
