//! `doxa model-facts --check-availability` is explicit, observational and
//! leaves no state. Only fixture providers run; no credential is present.
use std::{fs, os::unix::fs::PermissionsExt, path::Path, process::{Command, Output}};

const CODEX_FIXTURE: &str = r#"#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    with open('availability-calls.jsonl', 'a') as calls:
        calls.write(json.dumps(request) + '\n')
    method = request['method']
    if method == 'initialized':
        continue
    if method == 'initialize':
        result = {'userAgent': 'codex_cli_rs/0.167.0'}
    elif method == 'account/read':
        result = {'account': {'type': 'apiKey'}}
    elif method == 'model/list':
        result = {'data': [{'model': 'fixture-listed'}], 'nextCursor': None}
    else:
        raise AssertionError('availability started an unexpected operation: ' + method)
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
"#;

fn tree(root: &Path) -> Vec<(String, Vec<u8>)> {
    let mut entries = Vec::new();
    let mut pending = vec![root.to_owned()];
    while let Some(dir) = pending.pop() {
        for entry in fs::read_dir(&dir).unwrap() {
            let path = entry.unwrap().path();
            let name = path.strip_prefix(root).unwrap().display().to_string();
            if path.is_dir() { pending.push(path.clone()); entries.push((name, Vec::new())); }
            else { entries.push((name, fs::read(&path).unwrap())); }
        }
    }
    entries.sort();
    entries
}

fn stdout(output: &Output) -> String { String::from_utf8_lossy(&output.stdout).into_owned() }

#[test]
fn availability_flag_is_explicit_observational_and_writes_no_state() {
    let root = tempfile::tempdir().unwrap();
    let [home, bin, work] = ["home", "bin", "work"].map(|name| root.path().join(name));
    for dir in [root.path(), home.as_path(), bin.as_path(), work.as_path()] {
        if dir != root.path() { fs::create_dir(dir).unwrap(); }
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700)).unwrap();
    }
    fs::write(bin.join("codex"), CODEX_FIXTURE).unwrap();
    fs::set_permissions(bin.join("codex"), fs::Permissions::from_mode(0o700)).unwrap();
    let run = |args: &[&str]| Command::new(env!("CARGO_BIN_EXE_doxa-rs")).args(args).current_dir(&work)
        .env("DOXA_HOME", &home).env("HOME", &home).env("XDG_DATA_HOME", home.join("data"))
        .env("PATH", format!("{}:/usr/bin:/bin", bin.display()))
        // The Python fixture must not write bytecode caches under the private
        // HOME (macOS /usr/bin/python3 uses ~/Library/Caches/com.apple.python);
        // only DOXA's own writes are under test.
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env_remove("DEEPSEEK_API_KEY").env_remove("ZAI_API_KEY")
        .env_remove("OPENAI_API_KEY").env_remove("ANTHROPIC_API_KEY")
        .output().unwrap();
    let calls = work.join("availability-calls.jsonl");

    let plain = run(&["model-facts", "codex", "fixture-listed"]);
    assert!(plain.status.success(), "{}", String::from_utf8_lossy(&plain.stderr));
    assert!(!stdout(&plain).contains("Account availability"));
    assert!(!calls.exists(), "no provider runs without the explicit flag");
    let before = tree(&home);

    let listed = run(&["model-facts", "codex", "fixture-listed", "--check-availability"]);
    assert!(listed.status.success(), "{}", String::from_utf8_lossy(&listed.stderr));
    let text = stdout(&listed);
    assert!(text.starts_with("Model facts · codex/fixture-listed\n"), "static facts still print first");
    for line in ["\nAccount availability · codex/fixture-listed: listed\n", "\n  observed: 20",
        "\n  source: codex app-server account/read and model/list via ", "\n  listing latency: ",
        "\n  billed tier: unknown\n", "\nObservation only: a listing does not select a model"] {
        assert!(text.contains(line), "{line:?} missing from {text}");
    }
    let unlisted = run(&["model-facts", "codex", "fixture-absent", "--check-availability"]);
    assert!(stdout(&unlisted).contains("\nAccount availability · codex/fixture-absent: unlisted\n"));
    let methods = fs::read_to_string(&calls).unwrap().lines()
        .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap()["method"].as_str().unwrap().to_owned())
        .collect::<Vec<_>>();
    assert_eq!(methods.iter().filter(|method| *method == "model/list").count(), 2);
    assert!(methods.iter().all(|method| matches!(method.as_str(),
        "initialize" | "initialized" | "account/read" | "model/list")), "{methods:?}");

    for (args, expected) in [
        (["model-facts", "deepseek", "deepseek-flash", "--check-availability"],
            "deepseek/deepseek-flash: unknown (no DeepSeek credential)\n"),
        (["model-facts", "glm", "glm-5.3", "--check-availability"],
            "glm/glm-5.3: unknown (Z.AI documents no model-listing endpoint)\n"),
    ] {
        let output = run(&args);
        assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
        let text = stdout(&output);
        assert!(text.contains(&format!("\nAccount availability · {expected}")), "{text}");
        assert!(text.contains("\n  listing latency: not measured (no listing completed)\n"));
    }
    assert!(!run(&["model-facts", "router", "auto", "--check-availability"]).status.success());
    assert!(!run(&["model-facts", "codex", "fixture-listed", "--check-availability", "--check-availability"]).status.success());

    // Reading the absent DeepSeek credential takes the store's existing empty
    // lock; no observation, cache or setting is written.
    let after = tree(&home);
    let added = after.iter().filter(|entry| !before.contains(entry)).collect::<Vec<_>>();
    assert!(added.iter().all(|(name, bytes)| name == ".credentials.lock" && bytes.is_empty()),
        "unexpected writes: {:?}", added.iter().map(|(name, bytes)| (name, bytes.len())).collect::<Vec<_>>());
    assert!(before.iter().all(|entry| after.contains(entry)));
}
