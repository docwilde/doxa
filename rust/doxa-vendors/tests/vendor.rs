// SPDX-License-Identifier: AGPL-3.0-only
use doxa_vendors::{request_body, stream_once_local, Accumulator, Delta, Error, SseDecoder, Vendor, STREAM_LINE_MAX};
use serde_json::json;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::time::Duration;
use tokio::sync::watch;

fn server(response: Vec<u8>, delay: Duration) -> (String, std::thread::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}/chat/completions", listener.local_addr().unwrap());
    let task = std::thread::spawn(move || {
        let (mut socket, _) = listener.accept().unwrap();
        socket.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let mut request = Vec::new(); let mut buf = [0; 4096];
        loop {
            let n = socket.read(&mut buf).unwrap();
            if n == 0 { break; }
            request.extend_from_slice(&buf[..n]);
            if let Some(pos) = request.windows(4).position(|x| x == b"\r\n\r\n") {
                let header = String::from_utf8_lossy(&request[..pos]);
                let len = header.lines().find_map(|line| line.to_ascii_lowercase().strip_prefix("content-length: ").and_then(|n| n.parse::<usize>().ok())).unwrap_or(0);
                if request.len() >= pos + 4 + len { break; }
            }
        }
        std::thread::sleep(delay);
        let _ = socket.write_all(&response);
        String::from_utf8_lossy(&request).to_string()
    });
    (url, task)
}
fn http(status: &str, body: &str, content_type: &str) -> Vec<u8> {
    format!("HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).into_bytes()
}
#[test]
fn provider_bodies_match_measured_contract() {
    let d = request_body(Vendor::DeepSeek, "deepseek-flash", &[json!({"role":"user","content":"Hi"})], "high").unwrap();
    let g = request_body(Vendor::Glm, "glm-5.3-flash", &[], "high").unwrap();
    assert_eq!(d.pointer("/thinking/reasoning_effort"), Some(&json!("high")));
    assert_eq!(g.get("reasoning_effort"), Some(&json!("high")));
    assert!(g.pointer("/thinking/reasoning_effort").is_none());
    assert!(d.get("max_tokens").is_none() && g.get("max_tokens").is_none());
    assert!(d.get("tools").is_none()); // gate not integrated
    assert_eq!(request_body(Vendor::DeepSeek, "x", &[], "none").unwrap()["thinking"]["type"], "disabled");
    assert_eq!(request_body(Vendor::Glm, "x", &[], "none"), Err(Error::InvalidEffort));
}
#[test]
fn fragmented_sse_and_tool_arguments() {
    let sse = concat!(
        ": keepalive\n\n",
        "data: {\"model\":\"resolved-model\",\"choices\":[{\"delta\":{\"reasoning_content\":\"think\",\"content\":\"hello\",\"tool_calls\":[{\"index\":0,\"id\":\"call-1\",\"function\":{\"name\":\"lookup\",\"arguments\":\"{\\\"x\\\":\"}}]}}]}\n\n",
        "data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\"1}\"}}]}}]}\n\n",
        "data: {\"usage\":{\"prompt_tokens\":4,\"completion_tokens\":7},\"choices\":[{\"finish_reason\":\"tool_calls\",\"delta\":{}}]}\n\n",
        "data: [DONE]\n\n"
    );
    let mut decoder = SseDecoder::default(); let mut acc = Accumulator::default(); let mut deltas = Vec::new();
    for byte in sse.as_bytes().chunks(7) { for p in decoder.push(byte).unwrap() { acc.absorb(&p, "secret", |d| deltas.push(d)).unwrap(); } }
    assert!(decoder.done()); acc.flush("secret", |d| deltas.push(d)); let out = acc.finish("secret").unwrap();
    assert_eq!(deltas, vec![Delta::Text("hello".into()), Delta::Reasoning("think".into())]);
    assert_eq!(out.model.as_deref(), Some("resolved-model"));
    assert_eq!(out.usage.unwrap()["completion_tokens"], 7);
    assert_eq!(out.tool_calls[0].arguments["x"], 1);
}
#[test]
fn provider_metadata_cannot_echo_the_active_key() {
    let key = "test-secret-1234";
    let mut acc = Accumulator::default();
    let frame = json!({
        "usage": {"prompt_tokens": 4, "metadata": {"echo": key}, "labels": [key]},
        "choices": [{"finish_reason": key, "delta": {}}]
    });
    acc.absorb(&frame.to_string(), key, |_| {}).unwrap();
    let completion = acc.finish(key).unwrap();
    assert_eq!(completion.usage.unwrap(), json!({
        "prompt_tokens": 4, "metadata": {"echo": "***"}, "labels": ["***"]
    }));
    assert_eq!(completion.finish_reason.as_deref(), Some("***"));
}
#[test]
fn bounds_reject_oversized_line_and_arguments() {
    let mut decoder = SseDecoder::default();
    assert_eq!(decoder.push(&vec![b'a'; STREAM_LINE_MAX+1]), Err(Error::StreamLineTooLarge));
    let mut acc = Accumulator::default();
    let frame = json!({"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"x","arguments":"a".repeat(1024*1024+1)}}]}}]}).to_string();
    assert_eq!(acc.absorb(&frame,"", |_|{}), Err(Error::ToolArgumentsTooLarge));
}

#[test]
fn malformed_tool_arguments_cannot_become_empty_arguments() {
    for arguments in ["", "{\"path\":", "[1,2]", "null"] {
        let mut acc = Accumulator::default();
        let frame = json!({"choices":[{"delta":{"tool_calls":[{"index":0,
            "function":{"name":"delete_file","arguments":arguments}}]}}]});
        acc.absorb(&frame.to_string(), "", |_| {}).unwrap();
        assert_eq!(acc.finish(""), Err(Error::InvalidToolArguments));
    }
}
#[tokio::test]
async fn fake_server_stream_and_scrub() {
    std::env::set_var("DEEPSEEK_API_KEY", "test-secret-1234");
    let body = "data: {\"model\":\"deepseek-flash\",\"choices\":[{\"delta\":{\"content\":\"hello test-se\"}}]}\n\ndata: {\"choices\":[{\"delta\":{\"content\":\"cret-1234\"}}]}\n\ndata: [DONE]\n\n";
    let (url, task) = server(http("200 OK", body, "text/event-stream"), Duration::ZERO);
    let (_, cancel) = watch::channel(false); let mut deltas = Vec::new();
    let result = stream_once_local(Vendor::DeepSeek, &url, json!({"model":"deepseek-flash"}), cancel, Duration::from_secs(3), |d| deltas.push(d)).await.unwrap();
    let request = task.join().unwrap();
    assert!(request.contains("Authorization: Bearer test-secret-1234") || request.contains("authorization: Bearer test-secret-1234"));
    assert_eq!(result.text, "hello ***");
    assert_eq!(deltas.iter().filter_map(|d| if let Delta::Text(s)=d {Some(s.as_str())} else {None}).collect::<String>(), "hello ***");
}
#[tokio::test]
async fn fake_server_error_code_never_exposes_key() {
    std::env::set_var("ZAI_API_KEY", "test-secret-1234");
    let body = r#"{"error":{"code":"1302","message":"bad test-secret-1234"}}"#;
    let (url, task) = server(http("429 Too Many Requests", body, "application/json"), Duration::ZERO);
    let (_, cancel) = watch::channel(false);
    let error = stream_once_local(Vendor::Glm, &url, json!({}), cancel, Duration::from_secs(3), |_|{}).await.unwrap_err();
    task.join().unwrap();
    assert_eq!(error, Error::Http { status: 429, code: Some("1302".into()) });
    assert!(!format!("{error}").contains("test-secret"));
}
#[tokio::test]
async fn cancellation_and_timeout_abort_request() {
    std::env::set_var("DEEPSEEK_API_KEY", "test-secret-1234");
    let (url, task) = server(http("200 OK", "data: [DONE]\n\n", "text/event-stream"), Duration::from_millis(250));
    let (sender, cancel) = watch::channel(false);
    let future = stream_once_local(Vendor::DeepSeek, &url, json!({}), cancel, Duration::from_secs(2), |_|{});
    tokio::spawn(async move { tokio::time::sleep(Duration::from_millis(40)).await; sender.send(true).unwrap(); });
    assert_eq!(future.await, Err(Error::Cancelled)); task.join().unwrap();
    let (url, task) = server(http("200 OK", "data: [DONE]\n\n", "text/event-stream"), Duration::from_millis(250));
    let (_, cancel) = watch::channel(false);
    assert_eq!(stream_once_local(Vendor::DeepSeek, &url, json!({}), cancel, Duration::from_millis(50), |_|{}).await, Err(Error::Timeout));
    task.join().unwrap();
}

#[tokio::test]
async fn local_override_rejects_userinfo_and_remote_hosts() {
    for endpoint in [
        "http://127.0.0.1:80@evil.example/chat/completions",
        "http://user@127.0.0.1:8080/chat/completions",
        "http://user:pass@127.0.0.1:8080/chat/completions",
        "http://127.0.0.1/chat/completions",
        "https://127.0.0.1:8080/chat/completions",
        "http://localhost:8080/chat/completions",
    ] {
        let (_, cancel) = watch::channel(false);
        let result = stream_once_local(
            Vendor::DeepSeek,
            endpoint,
            json!({}),
            cancel,
            Duration::from_millis(50),
            |_| {},
        ).await;
        assert_eq!(result, Err(Error::InvalidEndpoint), "{endpoint}");
    }
}
