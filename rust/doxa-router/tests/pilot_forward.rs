use doxa_router::{
    pilot_forward::{Forward, Request, Target},
    Config, Reason, JEV_MODEL,
};
use serde_json::{json, value::RawValue, Value};
use std::collections::BTreeMap;

const REQUEST: &[u8] = br#"{
    "model": "openai/development-pilot",
    "messages": [
        {"role":"system", "content":"PRIVATE_SYSTEM_FIXTURE"},
        {"role":"user", "content":[{"type":"text","text":"Please repair the sample."}]},
        {"role":"assistant", "content":null, "reasoning_content":"PRIVATE_REASONING_FIXTURE",
         "tool_calls":[{"type":"function","id":"tool-1","function":{"name":"read_fixture","arguments":"{ \"query\" : 1 }"}}]},
        {"role":"tool", "tool_call_id":"tool-1", "content":"PRIVATE_TOOL_RESULT_FIXTURE"},
        {"role":"user", "content":"Resume with the same tests."}
    ],
    "tools":[{"type":"function","function":{"name":"read_fixture","parameters":{"type":"object"}}}],
    "tool_choice":"auto",
    "max_tokens":32768,
    "temperature":0.21234567890123456789,
    "top_p":1,
    "stream":true,
    "stream_options":{"include_usage":true},
    "thinking":{"type":"enabled"},
    "reasoning_effort":"max"
}"#;

fn config() -> Config {
    let mappings: Value = serde_json::from_slice(include_bytes!(
        "../../../scripts/routing-benchmark/model-mappings.json"
    ))
    .unwrap();
    let candidates: Vec<_> = mappings["candidates"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["id"] != "flash-thinking")
        .map(|row| {
            let mut row = row.clone();
            row["supports_tools"] = json!(true);
            row
        })
        .collect();
    serde_json::from_value(json!({
        "version":1, "jev_model":JEV_MODEL, "criteria_version":"development-pilot-fixture-v1",
        "candidates":candidates, "fallback_id":"flash-direct", "confidence_threshold":0.75,
        "max_calls":30, "max_spend_usd_micros":1000000, "max_input_bytes":24576, "deadline_ms":12000
    }))
    .unwrap()
}

fn decision(target: &str, winner_probability: f64, input_tokens: u64) -> Vec<u8> {
    let opposing = if target == "flash-direct" {
        "pro-thinking"
    } else {
        "flash-direct"
    };
    serde_json::to_vec(&json!({
        "model":JEV_MODEL, "usage":{"input_tokens":input_tokens,"output_tokens":1},
        "answers":{"target":{"type":"choice", "choice":target, "confidence":2.0 * winner_probability - 1.0,
            "probabilities":{(target):winner_probability,(opposing):1.0 - winner_probability}}}
    })).unwrap()
}

fn exact_fields(raw: &[u8]) -> BTreeMap<String, Box<RawValue>> {
    serde_json::from_slice(raw).unwrap()
}

#[derive(Debug, PartialEq, Eq)]
struct FixtureResponse {
    status: u16,
    content_type: &'static str,
    chunks: Vec<&'static [u8]>,
}

fn relay_fixture(
    request: &Forward,
    transport: impl FnOnce(&[u8]) -> FixtureResponse,
) -> FixtureResponse {
    transport(request.body())
}

#[test]
fn fixed_and_jev_arms_send_only_permitted_substitutions_and_return_raw_responses() {
    let input = Request::parse(REQUEST).unwrap();
    let original = exact_fields(REQUEST);
    let config = config();
    for (target, id, model, thinking) in [
        (
            Target::FlashNone,
            "flash-direct",
            "deepseek-flash",
            "disabled",
        ),
        (
            Target::ProHigh,
            "pro-thinking",
            "deepseek-v4-pro",
            "enabled",
        ),
    ] {
        let fixed = input.for_fixed(target).unwrap();
        let routing = input.for_jev(&config).unwrap();
        let summary = routing.decision().body["state"]["task_summary"]
            .as_str()
            .unwrap();
        assert!(summary.contains("Please repair the sample."));
        assert!(summary.contains("Resume with the same tests."));
        assert!(!summary.contains("PRIVATE_"));
        let (selected, observed) = routing.finish(&decision(id, 0.96, 88), 11).unwrap();
        assert_eq!(observed.reason, Reason::Selected);
        assert_eq!(observed.latency_ms, 11);
        assert_eq!(fixed.body(), selected.body());
        assert_eq!(
            fixed.receipt().request_sha256,
            selected.receipt().request_sha256
        );
        assert_eq!(fixed.receipt().input_bound, fixed.body().len());
        assert_eq!(fixed.receipt().output_limit, 32768);
        let replaced = if target == Target::ProHigh {
            &["model", "reasoning_effort"][..]
        } else {
            &["model", "reasoning_effort", "thinking"][..]
        };
        assert_eq!(fixed.receipt().replaced_fields, replaced);
        let receipt = serde_json::to_string(fixed.receipt()).unwrap();
        assert!(!receipt.contains("PRIVATE_"));
        assert_eq!(fixed.receipt().model, model);

        let response = relay_fixture(&selected, |sent| {
            let actual = exact_fields(sent);
            for key in actual.keys().chain(original.keys()) {
                if !matches!(key.as_str(), "model" | "reasoning_effort" | "thinking") {
                    assert_eq!(actual[key].get(), original[key].get(), "modified {key}");
                }
            }
            assert_eq!(actual["temperature"].get(), "0.21234567890123456789");
            let parsed: Value = serde_json::from_slice(sent).unwrap();
            assert_eq!(parsed["model"], model);
            assert_eq!(parsed["thinking"], json!({"type":thinking}));
            if target == Target::ProHigh {
                assert_eq!(parsed["reasoning_effort"], "high");
            } else {
                assert!(parsed.get("reasoning_effort").is_none());
            }
            FixtureResponse {
                status: 200,
                content_type: "text/event-stream",
                chunks: vec![
                    b"data: {\"choices\":[",
                    b"]}\r\n\r\n",
                    b"data: [DONE]\r\n\r\n",
                ],
            }
        });
        assert_eq!(
            response,
            FixtureResponse {
                status: 200,
                content_type: "text/event-stream",
                chunks: vec![
                    b"data: {\"choices\":[",
                    b"]}\r\n\r\n",
                    b"data: [DONE]\r\n\r\n"
                ],
            }
        );
        let refusal = FixtureResponse {
            status: 429,
            content_type: "application/json",
            chunks: vec![b"{\"error\":{\"message\":\"fixture\"}}\n"],
        };
        assert_eq!(
            relay_fixture(&selected, |_| FixtureResponse {
                status: refusal.status,
                content_type: refusal.content_type,
                chunks: refusal.chunks.clone(),
            }),
            refusal
        );
    }
}

#[test]
fn rejects_missing_oversized_or_hidden_authority_before_forwarding() {
    let good =
        json!({"model":"pilot","max_tokens":32768,"messages":[{"role":"user","content":"test"}]});
    for (field, value) in [
        ("max_tokens", json!(32769)),
        ("max_tokens", json!(0)),
        ("max_tokens", Value::Null),
        ("max_completion_tokens", json!(1)),
        ("n", json!(4)),
        ("tools", json!([{"type":"deepseek_web_search"}])),
        ("tools", json!([{"type":"deepseek_read_url"}])),
        ("tool_choice", json!("required")),
        (
            "messages",
            json!([{"role":"user","content":[{"type":"file","file_id":"placeholder"}]}]),
        ),
        (
            "messages",
            json!([{"role":"user","content":[{"type":"image_url","image_url":{"url":"placeholder"}}]}]),
        ),
        (
            "messages",
            json!([{"role":"assistant","content":null,"server_tool_calls":[]} ]),
        ),
        ("task_id", json!("fixture-task")),
    ] {
        let mut bad = good.clone();
        bad[field] = value;
        assert!(
            Request::parse(&serde_json::to_vec(&bad).unwrap()).is_err(),
            "{bad}"
        );
    }
    assert!(Request::parse(br#"{"model":"pilot","max_tokens":32768,"messages":[{"role":"user","content":"first","content":"second"}]}"#).is_err());
    assert!(Request::parse(&vec![b' '; 20001]).is_err());

    let pilot: Value = serde_json::from_slice(include_bytes!(
        "../../../scripts/routing-benchmark/pilot.json"
    ))
    .unwrap();
    let raw_cap = pilot["max_input_tokens"].as_u64().unwrap() as usize;
    let mut near_limit = good;
    near_limit["messages"][0]["content"] = json!("x".repeat(raw_cap - 100));
    let initial_size = serde_json::to_vec(&near_limit).unwrap().len();
    near_limit["messages"][0]["content"] = json!("x".repeat(
        near_limit["messages"][0]["content"].as_str().unwrap().len() + raw_cap - initial_size
    ));
    let bounded = serde_json::to_vec(&near_limit).unwrap();
    assert_eq!(bounded.len(), raw_cap);
    let request = Request::parse(&bounded).unwrap();
    assert!(
        request.for_fixed(Target::ProHigh).is_err(),
        "provider substitutions add to the input bound"
    );
    assert!(Request::parse(&[bounded, vec![b' ']].concat()).is_err());
}

#[test]
fn invalid_low_confidence_and_incompatible_jev_requests_never_forward() {
    let config = config();
    let request = Request::parse(REQUEST).unwrap();
    for response in [
        decision("bad-candidate", 0.96, 88),
        decision("flash-direct", 0.51, 88),
        decision("flash-direct", 0.96, 99_999),
        decision("flash-direct", 0.96, 0),
        vec![b' '; 65537],
    ] {
        assert!(request
            .for_jev(&config)
            .unwrap()
            .finish(&response, 11)
            .is_err());
    }
    let mut incomplete: Value = serde_json::from_slice(REQUEST).unwrap();
    incomplete["messages"][2]
        .as_object_mut()
        .unwrap()
        .remove("reasoning_content");
    let without_reasoning = Request::parse(&serde_json::to_vec(&incomplete).unwrap()).unwrap();
    assert!(without_reasoning.for_fixed(Target::FlashNone).is_ok());
    assert!(without_reasoning.for_fixed(Target::ProHigh).is_err());
    assert!(without_reasoning.for_jev(&config).is_err());
    let mut invalid_config = config;
    invalid_config.candidates[0].effort = "high".into();
    assert!(request.for_jev(&invalid_config).is_err());
}
