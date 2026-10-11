//! Persisted policy must stop the supervisor before a worker can return success.
use std::os::unix::fs::PermissionsExt;
use std::{
    fs,
    process::{Command, Stdio},
};

#[test]
fn native_reviewer_accepts_router_source_under_selected_api_authority() {
    use doxa_transcript::TranscriptStore;
    use serde_json::{json, Value};

    let owned = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(owned.path()).unwrap();
    let cwd = root.join("workspace");
    fs::create_dir(&cwd).unwrap();
    let slug: String = cwd.to_string_lossy().chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect();
    let projects = root.join("projects");
    let store = TranscriptStore::new(&projects, &slug, "router-review").unwrap();
    for text in ["retain the objective", "retain completed changes", "retain unresolved work"] {
        store.try_append_router_turn(cwd.to_str().unwrap(), text, "verified fixture answer",
            "2026-10-11T00:00:00Z", &json!({"target_id":"glm","engine":"glm","model":"glm-5.3-flash"}),
            |s| Ok(s.to_owned())).unwrap();
    }
    let source = store.transcript_path();
    let original = fs::read(&source).unwrap();
    let (_, proof) = doxa_engines::compact_hook::safe_read(&source, 1024 * 1024).unwrap();
    let metadata = json!({"cwd":cwd,"session_id":"router-review","transcript":source,
        "older":true,"expected_source":proof.json(),"conversation_engine":"router",
        "summary_target":{"target_id":"glm","engine":"glm","model":"glm-5.3-flash","effort":"high"}});
    let capture = root.join("provider-prompt");
    let provider = root.join("review-provider");
    fs::write(&provider, "#!/bin/sh\ncat > \"$REVIEW_PROVIDER_CAPTURE\"\nprintf '%s' '{\"memory\":[],\"filemap\":[],\"skills\":[],\"skill_outcomes\":[],\"conclusions\":[]}'\n").unwrap();
    fs::set_permissions(&provider, fs::Permissions::from_mode(0o700)).unwrap();
    let carrier = std::env::var_os("DOXA_LORE_RS").unwrap_or_else(|| "lore-rs".into());
    let run = |request: &Value| {
        let mut child = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
            .args(["__review-supervisor", "glm", &request.to_string(), "5000"])
            .env_clear().env("PATH", "/usr/bin:/bin")
            .env("HOME", &root).env("DOXA_HOME", root.join("doxa"))
            .env("DOXA_LORE_RS", &carrier).env("LORE_ROOT", root.join("lore"))
            .env("LORE_PROJECTS_DIR", &projects).env("LORE_SKILLS_DIR", root.join("skills"))
            .env("LORE_DISABLE_SYNC", "1").env("LORE_DISABLE_REVIEW", "0")
            .env("LORE_CLAUDE_BIN", &provider).env("REVIEW_PROVIDER_CAPTURE", &capture)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
            .spawn().unwrap();
        // Keep the supervisor's parent-liveness pipe open until it finishes.
        let control = child.stdin.take().unwrap();
        let output = child.wait_with_output().unwrap();
        drop(control);
        output
    };
    let approved = run(&metadata);
    assert!(approved.status.success(), "native reviewer did not accept the router source");
    assert_eq!(serde_json::from_slice::<Value>(&approved.stdout).unwrap(),
        doxa_engines::review_worker::approval_receipt(&metadata, "glm", std::time::Duration::from_secs(5)).unwrap());
    let prompt = fs::read_to_string(&capture).unwrap();
    assert!(prompt.contains("retain unresolved work"), "native review skipped the provider fixture");
    assert_eq!(fs::read(&source).unwrap(), original);
    for line in original.split(|b| *b == b'\n').filter(|line| !line.is_empty()) {
        assert_eq!(serde_json::from_slice::<Value>(line).unwrap()["engine"], "router");
    }
    fs::remove_file(&capture).unwrap();
    let mut stale = metadata.clone();
    stale["expected_source"]["sha256"] = json!("stale-proof");
    let refused = run(&stale);
    assert!(!refused.status.success());
    assert!(refused.stdout.is_empty());
    assert!(!capture.exists(), "stale source proof reached the reviewer provider");
}

#[test]
fn saved_disabled_review_never_spawns_worker_or_emits_approval() {
    let root = tempfile::tempdir().unwrap();
    let claude = root.path().join(".claude");
    fs::create_dir(&claude).unwrap();
    let settings = claude.join("settings.json");
    let marker = root.path().join("worker-ran");
    let worker = root.path().join("worker");
    fs::write(
        &worker,
        "#!/bin/sh\nprintf ran > \"$POLICY_WORKER_MARKER\"\nexit 0\n",
    )
    .unwrap();
    fs::set_permissions(&worker, fs::Permissions::from_mode(0o700)).unwrap();
    for name in ["LORE_DISABLE_REVIEW", "LORE_SKIP"] {
        let mut values = serde_json::Map::new();
        values.insert(name.into(), serde_json::json!("1"));
        fs::write(&settings, serde_json::json!({"env":values}).to_string()).unwrap();
        fs::set_permissions(&settings, fs::Permissions::from_mode(0o600)).unwrap();
        let output = Command::new(env!("CARGO_BIN_EXE_doxa-daemon"))
            .args(["__review-supervisor", "codex", "{}", "1000"])
            .env_clear()
            .env("HOME", root.path())
            .env("PATH", "/usr/bin:/bin")
            .env("DOXA_HOME", root.path().join("doxa"))
            .env("DOXA_LORE_RS", &worker)
            .env("POLICY_WORKER_MARKER", &marker)
            .stdin(Stdio::null())
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(
            output.stdout.is_empty(),
            "disabled policy emitted an approval receipt"
        );
        assert!(!marker.exists(), "disabled policy spawned a review worker");
    }
}
