use doxa_transcript::{TranscriptStore, MAX_VENDOR_REASONING_BYTES};
use serde_json::{json, Value};
use std::{fs, os::unix::fs::PermissionsExt};

#[test]
fn private_reasoning_is_scrubbed_resumable_and_absent_from_public_transcript() {
    let dir = tempfile::tempdir().unwrap();
    let store = TranscriptStore::new(dir.path(), "project", "session-1").unwrap();
    let messages = vec![json!({"role":"user","content":"question"}),
        json!({"role":"assistant","content":"answer","reasoning_content":"fixture-secret hidden reasoning"})];
    let saved = store.try_write_vendor_messages("deepseek", "model", &messages,
        |text| Ok(text.replace("fixture-secret", "[redacted]"))).unwrap();
    assert_eq!(saved[1]["reasoning_content"], "[redacted] hidden reasoning");
    let path = store.vendor_messages_path();
    assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o7777, 0o600);
    assert!(!fs::read_to_string(&path).unwrap().contains("fixture-secret"));
    assert_eq!(store.read_vendor_messages("deepseek", "model").unwrap().unwrap(), saved);
    store.try_append_vendor_turn("deepseek", "/fixture", "question", "answer", "2026-09-28T00:00:00Z",
        |text| Ok(text.to_owned())).unwrap();
    store.verify_vendor_transcript("deepseek", &saved).unwrap();
    let public = fs::read_to_string(store.transcript_path()).unwrap();
    assert!(!public.contains("reasoning_content"));
    assert!(!public.contains("hidden reasoning"));
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(store.read_vendor_messages("deepseek", "model").is_err());
}

#[test]
fn reasoning_shape_size_and_failed_scrub_leave_private_state_unchanged() {
    let dir = tempfile::tempdir().unwrap();
    let store = TranscriptStore::new(dir.path(), "project", "session-1").unwrap();
    let valid = vec![json!({"role":"user","content":"question"}),
        json!({"role":"assistant","content":"answer","reasoning_content":"private"})];
    store.try_write_vendor_messages("deepseek", "model", &valid, |text| Ok(text.to_owned())).unwrap();
    let original = fs::read(store.vendor_messages_path()).unwrap();
    for invalid in [
        vec![json!({"role":"user","content":"question","reasoning_content":"not an assistant"}), valid[1].clone()],
        vec![valid[0].clone(), json!({"role":"assistant","content":"answer","reasoning_content":7})],
        vec![valid[0].clone(), json!({"role":"assistant","content":"answer","reasoning_content":"private","tool_calls":[]})],
        vec![valid[0].clone(), json!({"role":"assistant","content":"answer","reasoning_content":"x".repeat(MAX_VENDOR_REASONING_BYTES + 1)})],
        vec![valid[0].clone(), json!({"role":"assistant","content":"answer","reasoning_content":"é".repeat(MAX_VENDOR_REASONING_BYTES / 2 + 1)})],
    ] {
        assert!(store.try_write_vendor_messages("deepseek", "model", &invalid, |text| Ok(text.to_owned())).is_err());
        assert_eq!(fs::read(store.vendor_messages_path()).unwrap(), original);
    }
    assert!(store.try_write_vendor_messages("deepseek", "model", &valid, |text|
        if text == "private" { Err(std::io::Error::other("fixture scrub failure")) } else { Ok(text.to_owned()) }).is_err());
    assert_eq!(fs::read(store.vendor_messages_path()).unwrap(), original);
    let maximum = vec![valid[0].clone(), json!({"role":"assistant","content":"answer",
        "reasoning_content":"x".repeat(MAX_VENDOR_REASONING_BYTES)})];
    assert!(store.try_write_vendor_messages("deepseek", "model", &maximum, |text|
        if text.len() == MAX_VENDOR_REASONING_BYTES { Ok(format!("{text}x")) } else { Ok(text.to_owned()) }).is_err());
    assert_eq!(fs::read(store.vendor_messages_path()).unwrap(), original);
    store.try_write_vendor_messages("deepseek", "model", &maximum, |text| Ok(text.to_owned())).unwrap();
    let saved: Value = serde_json::from_slice(&fs::read(store.vendor_messages_path()).unwrap()).unwrap();
    assert_eq!(saved["messages"][1]["reasoning_content"].as_str().unwrap().len(), MAX_VENDOR_REASONING_BYTES);
}
