use doxa_transcript::{
    valid_session_id, TranscriptStore, MAX_TRANSCRIPT_BYTES, MAX_TRANSCRIPT_LINES,
};
use serde_json::{json, Map, Value};
use std::fs;
use std::os::unix::fs::{symlink, PermissionsExt};
use tempfile::tempdir;

fn store(root: &std::path::Path) -> TranscriptStore {
    TranscriptStore::new(root, "project", "session-1").unwrap()
}

#[test]
fn python_records_round_trip_and_scrubbing_covers_nested_unknown_fields() {
    let temp = tempdir().unwrap();
    let store = store(temp.path());
    fs::write(
        store.transcript_path(),
        include_bytes!("fixtures/python-1.19.jsonl"),
    )
    .unwrap();
    let records = store.read_records().unwrap();
    assert_eq!(records.len(), 5);
    assert_eq!(records[0]["future"]["keep"], true);
    assert_eq!(records[2]["message"]["content"][0]["type"], "tool_result");
    store
        .append(
            json!({"type":"user","message":{"role":"user","content":"secret"},"future":"secret"}),
            "codex",
            |s| s.replace("secret", "[redacted]"),
        )
        .unwrap();
    let records = store.read_records().unwrap();
    assert_eq!(records[5]["message"]["content"], "[redacted]");
    assert_eq!(records[5]["future"], "[redacted]");
    assert_eq!(records[5]["engine"], "codex");
}

#[test]
fn bounded_tail_skips_torn_lines_and_partial_prefix() {
    let temp = tempdir().unwrap();
    let store = store(temp.path());
    fs::write(
        store.transcript_path(),
        b"{bad}\n{\"type\":\"user\"}\n{\"torn\":",
    )
    .unwrap();
    assert_eq!(store.read_records().unwrap().len(), 1);
    let mut raw = vec![b'x'; MAX_TRANSCRIPT_BYTES];
    raw.extend_from_slice(b"\n{\"tail\":true}\n");
    fs::write(store.transcript_path(), raw).unwrap();
    assert_eq!(store.read_records().unwrap(), vec![json!({"tail":true})]);
    assert_eq!(MAX_TRANSCRIPT_LINES, 20_000);
}

#[test]
fn metadata_preserves_future_fields_and_refuses_corruption() {
    let temp = tempdir().unwrap();
    let store = store(temp.path());
    fs::write(
        store.thread_path(),
        include_bytes!("fixtures/python-1.19.codex.json"),
    )
    .unwrap();
    assert_eq!(
        store.recorded_thread_id().unwrap().unwrap(),
        "01999999-aaaa-bbbb-cccc-0123456789ab"
    );
    let mut update = Map::new();
    update.insert("model".into(), Value::String("secret".into()));
    store
        .write_thread(update, |s| s.replace("secret", "[redacted]"))
        .unwrap();
    let metadata = store.read_thread().unwrap().unwrap();
    assert_eq!(metadata["future"]["keep"], true);
    assert_eq!(metadata["model"], "[redacted]");
    fs::write(store.thread_path(), b"{").unwrap();
    assert!(store.recorded_thread_id().unwrap().is_none());
    assert!(store.write_thread(Map::new(), str::to_owned).is_err());
    assert_eq!(fs::read(store.thread_path()).unwrap(), b"{");
}

#[test]
fn writes_new_python_shaped_codex_metadata() {
    let temp = tempdir().unwrap();
    let store = store(temp.path());
    let fields = serde_json::from_value::<Map<String, Value>>(json!({
        "thread_id":"thread-1", "session_id":"session-1", "model":"gpt-5",
        "cwd":"/work/project", "recorded":"2026-09-24T10:00:00Z"
    }))
    .unwrap();
    store.write_thread(fields, str::to_owned).unwrap();
    assert_eq!(
        store.recorded_thread_id().unwrap().as_deref(),
        Some("thread-1")
    );
}

#[test]
fn metadata_write_ignores_orphaned_pid_named_temp_file() {
    let temp = tempdir().unwrap();
    let store = store(temp.path());
    let orphan = temp
        .path()
        .join("project")
        .join(format!(".session-1.codex.{}.tmp", std::process::id()));
    fs::write(&orphan, b"orphaned write").unwrap();
    let mut fields = Map::new();
    fields.insert("thread_id".into(), Value::String("thread-1".into()));
    store.write_thread(fields, str::to_owned).unwrap();
    assert_eq!(
        store.recorded_thread_id().unwrap().as_deref(),
        Some("thread-1")
    );
    assert_eq!(fs::read(orphan).unwrap(), b"orphaned write");
}

#[test]
fn clean_checkpoint_refuses_missing_torn_or_changed_transcript_and_validates_resume_boundary() {
    let temp = tempdir().unwrap();
    let store = store(temp.path());
    let fields = serde_json::from_value::<Map<String, Value>>(
        json!({"thread_id":"thread-1","turn_incomplete":false})).unwrap();
    assert!(store.write_thread(fields.clone(), str::to_owned).is_err());
    assert!(!store.thread_path().exists());
    fs::write(store.transcript_path(), b"{\"type\":\"user\"}").unwrap();
    assert!(store.write_thread(fields.clone(), str::to_owned).is_err());
    assert!(!store.thread_path().exists());
    store.append(json!({"type":"assistant","message":{"content":"answer"}}),
                 "codex", str::to_owned).unwrap();
    assert!(store.write_thread(fields.clone(), str::to_owned).is_err(),
            "a completed append cannot repair a torn prior JSONL boundary");
    fs::write(store.transcript_path(), b"{\"type\":\"user\"}\n").unwrap();
    store.append(json!({"type":"assistant","message":{"content":"answer"}}),
                 "codex", str::to_owned).unwrap();
    store.write_thread(fields, str::to_owned).unwrap();
    let metadata = store.read_thread().unwrap().unwrap();
    let raw = fs::read(store.transcript_path()).unwrap();
    assert_eq!(metadata["transcript_bytes"].as_u64(), Some(raw.len() as u64));
    store.verify_thread_checkpoint(&metadata).unwrap();
    for changed in [raw[..raw.len() - 1].to_vec(), [raw.as_slice(), b"{}\n"].concat(), {
        let mut changed = raw.clone();
        *changed.last_mut().unwrap() = b' ';
        changed
    }] {
        fs::write(store.transcript_path(), changed).unwrap();
        assert!(store.verify_thread_checkpoint(&metadata).is_err());
    }
    fs::write(store.transcript_path(), raw).unwrap();
    for bytes in [json!(0), json!(-1), json!(true), json!("12")] {
        let mut invalid = metadata.clone();
        invalid["transcript_bytes"] = bytes;
        assert!(store.verify_thread_checkpoint(&invalid).is_err());
    }
    let mut legacy = metadata;
    legacy.as_object_mut().unwrap().remove("transcript_bytes");
    store.verify_thread_checkpoint(&legacy).unwrap();
}

#[test]
fn append_tightens_permissions_on_legacy_transcript() {
    let temp = tempdir().unwrap();
    let store = store(temp.path());
    fs::write(store.transcript_path(), b"{\"type\":\"user\"}\n").unwrap();
    fs::set_permissions(store.transcript_path(), fs::Permissions::from_mode(0o644)).unwrap();
    store
        .append(json!({"type":"assistant"}), "claude", str::to_owned)
        .unwrap();
    assert_eq!(
        fs::metadata(store.transcript_path())
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
}

#[test]
fn sidecar_scrub_failure_writes_no_partial_record_or_thread() {
    let temp = tempdir().unwrap();
    let store = store(temp.path());
    let record =
        json!({"type":"assistant","message":{"content":[{"text":"safe"},{"text":"secret"}]}});
    let result = store.try_append(record, "codex", |text| {
        if text == "secret" {
            Err(std::io::Error::other("scrub failed"))
        } else {
            Ok(text.to_owned())
        }
    });
    assert!(result.is_err());
    assert!(!store.transcript_path().exists());
    let fields = serde_json::from_value::<Map<String, Value>>(
        json!({"thread_id":"thread-1","model":"secret"}),
    )
    .unwrap();
    assert!(store
        .try_write_thread(fields, |text| {
            if text == "secret" {
                Err(std::io::Error::other("scrub failed"))
            } else {
                Ok(text.to_owned())
            }
        })
        .is_err());
    assert!(!store.thread_path().exists());
}

#[test]
fn rejects_unsafe_identifiers_symlink_and_hardlink_targets() {
    let temp = tempdir().unwrap();
    for id in ["", "../oops", "a_b", "-bad", "a*"] {
        assert!(!valid_session_id(id));
    }
    assert!(!valid_session_id(&"a".repeat(129)));
    assert!(TranscriptStore::new(temp.path(), "../escape", "ok").is_err());
    let store = store(temp.path());
    let target = temp.path().join("outside");
    fs::write(&target, "safe").unwrap();
    symlink(&target, store.transcript_path()).unwrap();
    assert!(store
        .append(json!({"type":"user"}), "claude", str::to_owned)
        .is_err());
    assert!(store.read_records().is_err());
    assert_eq!(fs::read_to_string(&target).unwrap(), "safe");
    fs::remove_file(store.transcript_path()).unwrap();
    fs::hard_link(&target, store.transcript_path()).unwrap();
    assert!(store.read_records().is_err());
    fs::remove_file(store.transcript_path()).unwrap();
    fs::remove_dir(temp.path().join("project")).unwrap();
    symlink(&target, temp.path().join("project")).unwrap();
    assert!(TranscriptStore::new(temp.path(), "project", "session-1").is_err());
}
