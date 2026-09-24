use doxa_engines::codex::{CodexJsonlNormalizer, ParseError, MAX_LINE_BYTES};
use doxa_engines::{EngineCapabilities, EngineEvent};
use serde_json::json;

fn parser() -> CodexJsonlNormalizer {
    CodexJsonlNormalizer::new(|text| text.replace("fixture-secret", "[redacted]"))
}

#[test]
fn fixture_normalizes_chunked_turn_and_usage() {
    let mut parser = parser();
    parser.begin_turn();
    let source = include_bytes!("fixtures/codex_turn.jsonl");
    let mut events = Vec::new();
    for chunk in source.chunks(17) { events.extend(parser.push_bytes(chunk).unwrap()); }
    assert_eq!(parser.thread_id(), Some("thread-fixture-1"));
    assert_eq!(parser.usage().input_tokens, 100);
    assert_eq!(parser.usage().cache_read_input_tokens, 20);
    assert_eq!(parser.usage().output_tokens, 30);
    assert_eq!(parser.usage().reasoning_output_tokens, 5);
    assert!(parser.flush_eof().is_empty());
    assert_eq!(events.iter().map(|e| e.kind.as_str()).collect::<Vec<_>>(), [
        "tool_call", "tool_result", "tool_call", "tool_result", "tool_result", "reasoning_delta", "text_delta"
    ]);
    assert_eq!(events[0], EngineEvent::new("tool_call", json!({"id":"cmd-1","name":"command_execution","input":{"command":"printf hello"}})));
    assert_eq!(events[3].data["result_summary"], "1/2 done");
    assert_eq!(events[4].data["result_summary"], "2/2 done");
    assert_eq!(events[5].data, json!({"text":"I checked the result."}));
    assert_eq!(events[6].data, json!({"text":"Done."}));
    let terminal = parser.finish_turn(Some(123), None);
    assert_eq!(terminal.len(), 1);
    assert_eq!(terminal[0].kind, "turn_done");
    assert_eq!(terminal[0].data["is_error"], false);
    assert_eq!(terminal[0].data["cost_usd"], serde_json::Value::Null);
    assert_eq!(terminal[0].data["ctx_percentage"], serde_json::Value::Null);
    assert!(parser.finish_turn(None, None).is_empty());
}

#[test]
fn tool_kinds_keep_codex_names_and_scrub_output() {
    let mut parser = parser();
    parser.begin_turn();
    let lines = [
        json!({"type":"item.started","item":{"id":"m","type":"mcp_tool_call","server":"doxa","tool":"lookup","arguments":{"q":"x"}}}),
        json!({"type":"item.completed","item":{"id":"m","type":"mcp_tool_call","result":{"content":[{"text":"fixture-secret"}]}}}),
        json!({"type":"item.started","item":{"id":"f","type":"file_change","changes":[{"path":"a.txt"}]}}),
        json!({"type":"item.completed","item":{"id":"f","type":"file_change","changes":[{"path":"a.txt"}]}}),
        json!({"type":"item.started","item":{"id":"w","type":"web_search","query":"fixture-secret"}}),
        json!({"type":"item.completed","item":{"id":"w","type":"web_search","status":"failed","error":{"message":"fixture-secret unavailable"}}}),
    ];
    let mut events = Vec::new();
    for frame in lines { events.extend(parser.push_bytes(format!("{frame}\n").as_bytes()).unwrap()); }
    assert_eq!(events[0].data["name"], "doxa/lookup");
    assert_eq!(events[0].data["input"], json!({"arguments":{"q":"x"}}));
    assert_eq!(events[1].data["result_summary"], "[redacted]");
    assert_eq!(events[2].data["input"], json!({"paths":["a.txt"]}));
    assert_eq!(events[3].data["result_summary"], "1 file(s) changed");
    assert_eq!(events[4].data["input"]["query"], "[redacted]");
    assert_eq!(events[5].data["is_error"], true);
}

#[test]
fn explicit_error_closes_once_and_unknown_frame_does_not() {
    let mut parser = parser();
    parser.begin_turn();
    assert!(parser.push_bytes(b"{\"type\":\"future.event\"}\n").unwrap().is_empty());
    let events = parser.push_bytes(b"{\"type\":\"turn.failed\",\"message\":\"fixture-secret expired\"}\n").unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0].data["text"], "codex: [redacted] expired");
    assert_eq!(events[1].data["error"], "[redacted] expired");
    assert!(parser.push_bytes(b"{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"late\"}}\n").unwrap().is_empty());
    assert!(parser.finish_turn(None, None).is_empty());
}

#[test]
fn malformed_lines_fail_the_turn_and_long_lines_stop_reading() {
    let mut parser = parser();
    parser.begin_turn();
    assert!(parser.push_bytes(b"not JSON\n").unwrap().is_empty());
    assert_eq!(parser.bad_frames(), 1);
    let events = parser.finish_turn(Some(5), None);
    assert_eq!(events[0].kind, "text_delta");
    assert_eq!(events[1].data["is_error"], true);
    assert!(events[1].data["error"].as_str().unwrap().contains("unreadable line was dropped"));

    parser.begin_turn();
    assert_eq!(parser.push_bytes(&vec![b'x'; MAX_LINE_BYTES + 1]), Err(ParseError::LineTooLong));
    parser.begin_turn();
    parser.push_bytes(b"{\"type\":\"turn.").unwrap();
    assert!(parser.flush_eof().is_empty());
    assert_eq!(parser.bad_frames(), 1);
    parser.begin_turn();
    parser.push_bytes(b"{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"last\"}}").unwrap();
    assert_eq!(parser.flush_eof()[0].data["text"], "last");
}

#[test]
fn capabilities_do_not_claim_unimplemented_provider_features() {
    let caps = EngineCapabilities::default();
    assert!(!caps.mcp_tools && !caps.resume && !caps.token_usage && !caps.permission_modes);
}

#[test]
fn process_failure_is_visible_and_usage_rejects_invalid_counts() {
    let mut parser = parser();
    parser.begin_turn();
    parser.push_bytes(b"{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":-1,\"output_tokens\":true,\"cached_input_tokens\":4}}\n").unwrap();
    assert_eq!(parser.usage().input_tokens, 0);
    assert_eq!(parser.usage().output_tokens, 0);
    assert_eq!(parser.usage().cache_read_input_tokens, 4);
    let events = parser.finish_turn(Some(20), Some("exec exited 2: fixture-secret"));
    assert_eq!(events.len(), 2);
    assert_eq!(events[0].data["text"], "codex: exec exited 2: [redacted]");
    assert_eq!(events[1].data["error"], "exec exited 2: [redacted]");
    assert_eq!(events[1].data["num_turns"], 1);
}
