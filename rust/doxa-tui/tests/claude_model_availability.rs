//! Fixture-only Claude catalog observations; no login credentials or inference.
use serde_json::{json, Value};
use std::{fs, os::unix::fs::PermissionsExt, process::Command};

#[test]
fn claude_catalog_lists_aliases_and_resolved_ids_without_claiming_authentication() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path().join("home"); let work = root.path().join("work");
    for path in [&home, &work] { fs::create_dir(path).unwrap(); fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap(); }
    let executable = root.path().join("claude-fixture");
    fs::write(&executable, r#"#!/usr/bin/env python3
import json, os, pathlib, sys
pathlib.Path('catalog.pid').write_text(str(os.getpid()))
for line in sys.stdin:
    request = json.loads(line)
    with open('catalog-calls.jsonl', 'a') as calls:
        calls.write(json.dumps(request) + '\n')
    assert request['type'] == 'control_request'
    assert request['request']['subtype'] == 'initialize'
    response = json.loads(pathlib.Path('catalog.json').read_text())
    print(json.dumps({'type':'control_response','response':{'subtype':'success',
        'request_id':request['request_id'],'response':response}}), flush=True)
"#).unwrap();
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
    let source = home.join(".claude"); fs::create_dir(&source).unwrap();
    let record = work.join("catalog.json");
    let catalog = json!({"account":{"tokenSource":"test-only-auth"},"models":[
        {"value":"opus","resolvedModel":"claude-opus-5-5"},
        {"value":"sonnet[1m]","resolvedModel":"claude-sonnet-5-5[1m]"}]});
    fs::write(&record, catalog.to_string()).unwrap();
    let run = |model: &str, explicit: bool| {
        let mut command = Command::new(env!("CARGO_BIN_EXE_doxa-rs"));
        command.env_clear().env("PATH", "/usr/bin:/bin").env("HOME", &home)
            .env("DOXA_HOME", home.join("doxa")).env("CLAUDE_CONFIG_DIR", &source)
            .env("DOXA_CLAUDE_BIN", &executable).env("PYTHONDONTWRITEBYTECODE", "1")
            .current_dir(&work).args(["model-facts", "claude", model]);
        if explicit { command.arg("--check-availability"); }
        command.output().unwrap()
    };
    let calls = work.join("catalog-calls.jsonl");
    let plain = run("opus", false); assert!(plain.status.success()); assert!(!calls.exists());
    for model in ["opus", "claude-opus-5-5", "sonnet[1m]", "claude-sonnet-5-5[1m]"] {
        let output = run(model, true);
        assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
        let text = String::from_utf8(output.stdout).unwrap();
        assert!(text.contains(&format!("CLI catalog membership · claude/{model}: listed\n")), "{text}");
        assert!(text.contains("Claude CLI initialize"));
        assert!(text.contains("billed tier: unknown"));
        assert!(text.contains("absence does not prove"));
        assert!(text.contains("Account authentication and entitlement: unverified"));
        assert!(!text.contains("test-only-auth"));
        let pid: i32 = fs::read_to_string(work.join("catalog.pid")).unwrap().parse().unwrap();
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1, "discovery child must be reaped");
    }
    let output = run("claude-absent", true);
    assert!(String::from_utf8(output.stdout).unwrap().contains("claude/claude-absent: unlisted\n"));
    fs::write(&record, json!({"models":catalog["models"],"account":{"apiProvider":"firstParty"}}).to_string()).unwrap();
    let text = String::from_utf8(run("opus", true).stdout).unwrap();
    assert!(text.contains("claude/opus: listed\n"));
    assert!(text.contains("Account authentication and entitlement: unverified"));
    for invalid in [json!({"models":[],"account":catalog["account"]}),
        json!({"models":[{"value":"bad\nmodel"}],"account":catalog["account"]})] {
        fs::write(&record, invalid.to_string()).unwrap();
        let output = run("opus", true); assert!(output.status.success());
        let text = String::from_utf8(output.stdout).unwrap();
        assert!(text.contains("claude/opus: unknown")); assert!(!text.contains("test-only-auth"));
        assert!(!text.contains("bad\nmodel"));
    }
    let requests: Vec<Value> = fs::read_to_string(calls).unwrap().lines()
        .map(|line| serde_json::from_str(line).unwrap()).collect();
    assert_eq!(requests.len(), 8);
    assert!(requests.iter().all(|r| r["type"] == "control_request" && r["request"]["subtype"] == "initialize"));
    assert_eq!(fs::read_dir(source).unwrap().count(), 0, "host auth stays untouched");
    let state: Vec<_> = fs::read_dir(home.join("doxa")).unwrap().map(|entry| entry.unwrap().file_name()).collect();
    assert_eq!(state, ["claude-cli"], "only the existing isolated CLI config is prepared");
}
