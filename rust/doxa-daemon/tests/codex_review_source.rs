//! Exercise the real hook -> native supervisor -> canonical LORE validator.
//! The only review backend is a local script: no credentials or provider HTTP.
#![cfg(target_os = "linux")]
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{fs, io::Write, os::unix::fs::{MetadataExt, PermissionsExt}, process::{Command, Stdio}};

#[test]
fn isolated_canonical_review_accepts_long_source_and_rejects_foreign_cwd_before_backend() {
    let temporary = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(temporary.path()).unwrap();
    let session = root.join("review-fixture");
    for name in ["home/codex/sessions", "checkout", "cache", "broker"] {
        fs::create_dir_all(session.join(name)).unwrap();
    }
    for path in [&session, &session.join("home"), &session.join("checkout"), &session.join("cache"), &session.join("broker")] {
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    }
    let policy = doxa_isolation::Policy {image:format!("sha256:{}", "a".repeat(64)), docker_host:format!("unix:///run/user/{}/unused-fixture.sock", unsafe {libc::geteuid()}),
        memory_bytes:512*1024*1024, cpus:1.0, pids:64, disk_soft_limit_bytes:None, disk_free_floor_bytes:None};
    let hash = format!("{:x}", Sha256::digest(serde_json::to_vec(&(&policy, doxa_isolation::Profile::DockerOffline)).unwrap()));
    let checkout = session.join("checkout"); let stat = fs::metadata(&checkout).unwrap();
    let isolation = session.join("manifest.json");
    fs::write(&isolation, json!({"version":1,"session_id":"review-fixture","profile":"docker-offline","policy":policy,
        "policy_hash":hash,"creation_policy_hash":hash,"source":checkout,"checkout":checkout,"checkout_device":stat.dev(),"checkout_inode":stat.ino(),
        "base_sha":"","branch":"","private_home":session.join("home"),"cache":session.join("cache"),"broker":session.join("broker"),"nonce":"fixture","state":"ready"}).to_string()).unwrap();
    fs::set_permissions(&isolation, fs::Permissions::from_mode(0o600)).unwrap();
    let codex = session.join("home/codex"); let source = codex.join("sessions/thread.jsonl");
    let header = json!({"type":"session_meta","payload":{"id":"provider-thread","cwd":"/workspace"}}).to_string()+"\n";
    let mut file = fs::File::create(&source).unwrap(); file.write_all(header.as_bytes()).unwrap();
    for text in ["Keep the offline raycaster objective.", "Preserve wall collision and shooting decisions.", "Remember the unresolved enemy occlusion test."] {
        writeln!(file, "{}", json!({"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":text}]}})).unwrap();
    }
    // Metadata records exercise complete-history scanning without inflating the review digest.
    let padding = json!({"type":"world_state","payload":{"fixture_padding":"x".repeat(512*1024)}}).to_string()+"\n";
    for _ in 0..66 { file.write_all(padding.as_bytes()).unwrap(); }
    writeln!(file, "{}", json!({"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_image","image_url":format!("data:image/png;base64,{}", "A".repeat(2*1024*1024))}]}})).unwrap();
    file.sync_all().unwrap(); drop(file);
    let original = fs::read(&source).unwrap(); assert!(original.len() > 32*1024*1024);
    let hook_manifest = root.join("compact.json");
    fs::write(&hook_manifest, json!({"version":doxa_engines::codex_compact::SUPPORTED_VERSION,"provider_thread":"provider-thread","doxa_session":"review-fixture","codex_home":codex,"cwd":checkout,"lore_enabled":true}).to_string()).unwrap();
    let backend = root.join("local-reviewer"); let capture = root.join("review-prompt");
    fs::write(&backend, "#!/bin/sh\ncat > \"$REVIEW_CAPTURE\"\nprintf '%s' '{\"memory\":[],\"filemap\":[],\"skills\":[],\"skill_outcomes\":[],\"conclusions\":[]}'\n").unwrap();
    fs::set_permissions(&backend, fs::Permissions::from_mode(0o700)).unwrap();
    let daemon = env!("CARGO_BIN_EXE_doxa-daemon");
    let digest = format!("{:x}", Sha256::digest(fs::read(daemon).unwrap()));
    let event = json!({"hook_event_name":"PreCompact","trigger":"auto","session_id":"provider-thread","transcript_path":source});
    let carrier = std::env::var_os("DOXA_LORE_RS").unwrap_or_else(|| "lore-rs".into());
    let run = |native: bool| {
        let mut command = Command::new(daemon);
        command.args(["__codex-precompact", hook_manifest.to_str().unwrap(), &digest])
            .env_clear().env("HOME", &root).env("PATH", "/usr/bin:/bin").env("TMPDIR", &root)
            .env("DOXA_HOME", root.join("doxa")).env("DOXA_SESSION_MANIFEST", &isolation)
            // Both inherited roots are deliberately wrong, as in a normal Docker host.
            .env("CODEX_HOME", root.join("owner-codex")).env("LORE_CODEX_SESSIONS_DIR", root.join("owner-codex/sessions"))
            .env("DOXA_LORE_RS", &carrier).env("LORE_ROOT", root.join(if native {"native-lore"} else {"lore"})).env("LORE_PROJECTS_DIR", root.join("projects"))
            .env("LORE_DISABLE_SYNC", "1").env("LORE_DISABLE_REVIEW", "0").env("LORE_CLAUDE_BIN", &backend).env("REVIEW_CAPTURE", &capture).env("RAW_REVIEW_SOURCE", &source)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        if !native { command.env("DOXA_ISOLATION_MANIFEST", &isolation); }
        let mut child = command.spawn().unwrap();
        child.stdin.take().unwrap().write_all((event.to_string()+"\n").as_bytes()).unwrap();
        let output = child.wait_with_output().unwrap();
        assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
        serde_json::from_slice::<Value>(&output.stdout).unwrap()
    };
    assert_eq!(run(false)["continue"], true);
    let prompt = fs::read_to_string(&capture).unwrap(); assert!(prompt.contains("unresolved enemy occlusion"));
    assert_eq!(fs::read(&source).unwrap(), original);
    fs::remove_file(&capture).unwrap();
    // A foreign worker header cannot be repaired into the selected host project.
    let mut file = fs::OpenOptions::new().write(true).open(&source).unwrap();
    let foreign = header.replace("/workspace", "/elsewhere"); assert_eq!(foreign.len(), header.len());
    file.write_all(foreign.as_bytes()).unwrap(); file.sync_all().unwrap();
    assert_eq!(run(false)["continue"], false); assert!(!capture.exists());
    // A changed raw source during canonical review invalidates approval too.
    drop(file); fs::write(&source, &original).unwrap();
    fs::write(&backend, "#!/bin/sh\ncat > \"$REVIEW_CAPTURE\"\nprintf '%s\\n' '{}' >> \"$RAW_REVIEW_SOURCE\"\nprintf '%s' '{\"memory\":[],\"filemap\":[],\"skills\":[],\"skill_outcomes\":[],\"conclusions\":[]}'\n").unwrap();
    assert_eq!(run(false)["continue"], false);
    assert!(capture.exists(), "mutation fixture never reached the canonical reviewer backend");
    assert_eq!(fs::metadata(&source).unwrap().len(), original.len() as u64+3);
    // Native-to-Docker and Docker-to-native imports retain their original header.
    // A native imported session has no active Docker manifest at all.
    let mut scope: Value = serde_json::from_slice(&fs::read(&isolation).unwrap()).unwrap();
    scope["profile"] = json!("native"); scope["context_cwd"] = json!(checkout);
    fs::write(&isolation,scope.to_string()).unwrap();
    let mut header_native: Value = serde_json::from_str(header.trim()).unwrap();
    header_native["payload"]["cwd"] = json!(checkout);
    let mut imported = serde_json::to_vec(&header_native).unwrap();
    imported.extend_from_slice(&original[header.len()-1..]);
    fs::write(&source,&imported).unwrap(); fs::remove_file(&capture).unwrap();
    fs::write(&backend,"#!/bin/sh\ncat > \"$REVIEW_CAPTURE\"\nprintf '%s' '{\"memory\":[],\"filemap\":[],\"skills\":[],\"skill_outcomes\":[],\"conclusions\":[]}'\n").unwrap();
    assert_eq!(run(true)["continue"],true);
    assert!(fs::read_to_string(&capture).unwrap().contains("unresolved enemy occlusion"));
    assert_eq!(fs::read(&source).unwrap(),imported);
}
