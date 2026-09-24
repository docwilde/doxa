use doxa_transcript::{TranscriptStore, MAX_VENDOR_MESSAGES_BYTES};
use serde_json::{json, Value};
use std::fs;
use std::os::unix::fs::{symlink, PermissionsExt};

#[test]
fn python_envelope_loads_and_replacement_is_scrubbed_and_private() {
    let dir = tempfile::tempdir().unwrap();
    let store = TranscriptStore::new(dir.path(), "project", "session-1").unwrap();
    let path = store.vendor_messages_path();
    fs::write(&path, br#"{"engine":"deepseek","messages":[{"role":"user","content":"hello"},{"role":"assistant","content":"world"}]}"#).unwrap();
    assert_eq!(
        store
            .read_vendor_messages("deepseek", "model-a")
            .unwrap()
            .unwrap()
            .len(),
        2
    );
    let clean = store
        .try_write_vendor_messages(
            "deepseek",
            "model-a",
            &[
                json!({"role":"user","content":"fixture-secret"}),
                json!({"role":"assistant","content":"fixture-secret answer"}),
            ],
            |s| Ok(s.replace("fixture-secret", "[redacted]")),
        )
        .unwrap();
    assert_eq!(clean[1]["content"], "[redacted] answer");
    let saved: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    assert_eq!(saved["session_id"], "session-1");
    assert_eq!(saved["model"], "model-a");
    assert!(!saved.to_string().contains("fixture-secret"));
    assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o077, 0);
    assert!(store.read_vendor_messages("glm", "model-a").is_err());
    assert!(store.read_vendor_messages("deepseek", "model-b").is_err());
}

#[test]
fn unsafe_and_failed_saves_leave_existing_state_unchanged() {
    let dir = tempfile::tempdir().unwrap();
    let store = TranscriptStore::new(dir.path(), "project", "session-1").unwrap();
    let path = store.vendor_messages_path();
    let original = br#"{"engine":"glm","messages":[]}"#;
    fs::write(&path, original).unwrap();
    let messages = [
        json!({"role":"user","content":"secret"}),
        json!({"role":"assistant","content":"answer"}),
    ];
    assert!(store
        .try_write_vendor_messages("glm", "model", &messages, |_| Err(std::io::Error::other(
            "scrub failure"
        )))
        .is_err());
    assert_eq!(fs::read(&path).unwrap(), original);
    for bad in [
        br#"{"engine":"deepseek","messages":[]}"#.as_slice(),
        br#"{"engine":"glm","messages":[{"role":"tool","content":"x"}]}"#.as_slice(),
        b"{".as_slice(),
    ] {
        fs::write(&path, bad).unwrap();
        assert!(store.read_vendor_messages("glm", "model").is_err());
    }
    fs::remove_file(&path).unwrap();
    let target = dir.path().join("target");
    fs::write(&target, original).unwrap();
    symlink(&target, &path).unwrap();
    assert!(store.read_vendor_messages("glm", "model").is_err());
    fs::remove_file(&path).unwrap();
    fs::write(&path, vec![b'x'; MAX_VENDOR_MESSAGES_BYTES as usize + 1]).unwrap();
    assert!(store.read_vendor_messages("glm", "model").is_err());
}

#[test]
fn vendor_jsonl_uses_python_shape_and_detects_half_commits() {
    let dir = tempfile::tempdir().unwrap();
    let store = TranscriptStore::new(dir.path(), "project", "session-1").unwrap();
    let messages = [
        json!({"role":"user","content":"[redacted] prompt"}),
        json!({"role":"assistant","content":"[redacted] answer"}),
    ];
    store
        .try_append_vendor_turn(
            "glm",
            "/work/project",
            "fixture-secret prompt",
            "fixture-secret answer",
            "2026-09-24T10:00:00Z",
            |s| Ok(s.replace("fixture-secret", "[redacted]")),
        )
        .unwrap();
    store.verify_vendor_transcript("glm", &messages).unwrap();
    let records: Vec<Value> = fs::read_to_string(store.transcript_path())
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(records.len(), 2);
    assert_eq!(records[0]["type"], "user");
    assert_eq!(records[0]["message"]["content"], "[redacted] prompt");
    assert_eq!(records[1]["type"], "assistant");
    assert_eq!(
        records[1]["message"]["content"][0]["text"],
        "[redacted] answer"
    );
    assert_eq!(records[0]["sessionId"], "session-1");
    assert_eq!(records[1]["engine"], "glm");
    assert!(!fs::read_to_string(store.transcript_path())
        .unwrap()
        .contains("fixture-secret"));
    assert!(store.verify_vendor_transcript("glm", &[]).is_err());
    assert!(store
        .verify_vendor_transcript("deepseek", &messages)
        .is_err());
    let snapshot = store.transcript_snapshot().unwrap().unwrap();
    assert_eq!(snapshot.1, fs::metadata(snapshot.0).unwrap().len());
    fs::write(store.transcript_path(), b"{malformed\n").unwrap();
    assert!(store.verify_vendor_transcript("glm", &messages).is_err());
}
