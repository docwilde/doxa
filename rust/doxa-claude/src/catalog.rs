//! Read the connected CLI's model catalog without submitting a user prompt.
use crate::{Cli, CliOptions, Error};
use serde_json::{json, Value};
use std::{time::{Duration, Instant}, sync::atomic::{AtomicBool, Ordering}};

pub fn decode(value: &Value) -> Result<Value, Error> {
    let rows = value["models"].as_array().filter(|rows| !rows.is_empty() && rows.len() <= 64)
        .ok_or(Error::Protocol)?;
    let mut ids = Vec::new();
    let mut capabilities = Vec::new();
    for row in rows {
        let valid = |name: &&str| !name.is_empty() && name.len() <= 128 && !name.chars().any(char::is_control);
        let name = row["value"].as_str().or_else(|| row["model"].as_str()).filter(valid).ok_or(Error::Protocol)?;
        let resolved=row["resolvedModel"].as_str().filter(valid);
        let mut names = vec![name];
        if let Some(resolved) = row["resolvedModel"].as_str().filter(valid) {
            if resolved != name { names.push(resolved); }
        }
        let efforts = row.get("supportedEfforts").or_else(|| row.get("supportedEffortLevels"))
            .and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_str)
            .filter(|level| matches!(*level, "low" | "medium" | "high" | "xhigh" | "max")).collect::<Vec<_>>();
        for name in names {
            if !ids.contains(&name) {
                ids.push(name);
                capabilities.push(json!({"model":name,"efforts":efforts,"resolved_model":resolved}));
            }
        }
    }
    Ok(json!({"models":ids,"capabilities":capabilities,"note":"Live model catalog from the connected Claude CLI"}))
}

pub fn authenticated(value: &Value) -> bool {
    let present=|key|value["account"][key].as_str().is_some_and(|text|!text.is_empty()
        && text.len()<=256 && !text.chars().any(char::is_control) && !matches!(text,"none"|"profile"));
    // Installed CLI 2.1.296 supplies apiProvider:"firstParty" even when signed
    // out. Backend identity alone, or an email alone, never authenticates.
    present("tokenSource") || present("apiKeySource") || (present("subscriptionType") && present("email"))
}

/// Initialize the connected account without a user prompt. A catalog alone is
/// not authentication evidence; missing account metadata remains unavailable.
pub fn discover_authenticated(options: CliOptions<'_>, timeout: Duration) -> Result<Value, Error> {
    discover_inner(options, timeout, true, None)
}

pub fn discover(options: CliOptions<'_>, timeout: Duration) -> Result<Value, Error> {
    discover_inner(options, timeout, false, None)
}
/// Prospective session discovery owns the CLI until cancellation has killed
/// and reaped it.
pub fn discover_cancellable(options: CliOptions<'_>, timeout: Duration,
    cancel: &AtomicBool) -> Result<Value, Error> {
    discover_inner(options, timeout, false, Some(cancel))
}
/// Router discovery owns the CLI until cancellation has killed and reaped it.
pub fn discover_authenticated_cancellable(options: CliOptions<'_>, timeout: Duration,
    cancel: &AtomicBool) -> Result<Value, Error> {
    discover_inner(options, timeout, true, Some(cancel))
}
fn discover_inner(options: CliOptions<'_>, timeout: Duration, require_auth: bool, cancel: Option<&AtomicBool>) -> Result<Value, Error> {
    if cancel.is_some_and(|cancel|cancel.load(Ordering::Acquire)) { return Err(Error::Closed); }
    let deadline = Instant::now() + timeout;
    let mut cli = Cli::spawn(options)?;
    if cancel.is_some_and(|cancel|cancel.load(Ordering::Acquire)) { return Err(Error::Closed); }
    let id = cli.control(json!({"subtype":"initialize","sdkMcpServers":[]}))?;
    loop {
        if cancel.is_some_and(|cancel|cancel.load(Ordering::Acquire)) { return Err(Error::Closed); }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() { return Err(Error::Timeout); }
        let frame = match cli.recv(if cancel.is_some() {remaining.min(Duration::from_millis(10))} else {remaining}) {
            Err(Error::Timeout) if cancel.is_some() => continue,
            frame => frame?,
        };
        match frame["type"].as_str() {
            Some("control_response") if frame["response"]["request_id"] == id => {
                if frame["response"]["subtype"] != "success" { return Err(Error::Protocol); }
                let response = &frame["response"]["response"];
                if require_auth && !authenticated(response) { return Err(Error::Protocol); }
                return decode(response);
            }
            Some("control_request") => {
                let request_id = frame["request_id"].as_str().ok_or(Error::Protocol)?;
                cli.respond(request_id, Err("Model discovery cannot run tools"))?;
            }
            Some("system" | "rate_limit_event") => {},
            _ => return Err(Error::Protocol),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::PermissionsExt, sync::Arc, thread};

    #[test]
    fn authenticated_catalog_requires_account_evidence_beyond_provider_identity() {
        for account in [json!({}),json!({"apiProvider":"firstParty"}),json!({"email":"local@example.test"}),
            json!({"apiKeySource":"none","tokenSource":"profile"}),json!({"tokenSource":"bad\nsource"})] {
            assert!(!authenticated(&json!({"account":account})));
        }
        for account in [json!({"apiKeySource":"ANTHROPIC_API_KEY"}),json!({"tokenSource":"claude.ai"}),
            json!({"email":"local@example.test","subscriptionType":"account-plan"})] {
            assert!(authenticated(&json!({"account":account})));
        }
    }

    #[test]
    fn catalog_discovery_initializes_without_prompts_and_cleans_up_the_cli() {
        let root = tempfile::tempdir().unwrap();
        fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let executable = root.path().join("claude-fixture");
        fs::write(&executable, r#"#!/usr/bin/env python3
import json, os, pathlib, sys
for line in sys.stdin:
    request = json.loads(line)
    with open(pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']) / 'calls.jsonl', 'a') as calls:
        calls.write(json.dumps(request) + '\n')
    assert request['type'] == 'control_request'
    assert request['request']['subtype'] == 'initialize'
    print(json.dumps({'type':'control_response','response':{'subtype':'success',
        'request_id':request['request_id'],'response':{'models':[{'value':'fixture-model',
        'resolvedModel':'fixture-resolved','supportedEfforts':['low','high']}]}}}), flush=True)
"#).unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
        let reply = discover(CliOptions {
            executable: &executable, cwd: root.path(), config_dir: root.path(),
            session_id: "12345678-1234-1234-1234-123456789abc", resume: false,
            model: None, effort: None, permission_mode: "manual", plugins: &[],
        }, Duration::from_secs(3)).unwrap();
        assert_eq!(reply["models"], json!(["fixture-model", "fixture-resolved"]));
        assert_eq!(reply["capabilities"][1]["efforts"], json!(["low", "high"]));
        let calls = fs::read_to_string(root.path().join("calls.jsonl")).unwrap();
        assert_eq!(calls.lines().count(), 1);
        assert!(!calls.contains("\"type\": \"user\""));
    }

    #[test]
    fn cancelled_catalog_discovery_kills_and_reaps_its_provider_process() {
        let root = tempfile::tempdir().unwrap();
        fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let executable = root.path().join("claude-fixture");
        fs::write(&executable, r#"#!/usr/bin/env python3
import os, pathlib, sys, time
pathlib.Path('catalog.pid').write_text(str(os.getpid()))
for line in sys.stdin:
    time.sleep(0.1)
"#).unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
        let cancel=Arc::new(AtomicBool::new(false));
        let worker_cancel=cancel.clone();
        let pidfile=root.path().join("catalog.pid");
        let watcher=thread::spawn(move || {
            let deadline=Instant::now()+Duration::from_secs(3);
            while Instant::now()<deadline {
                if pidfile.exists() { worker_cancel.store(true,Ordering::Release); return true; }
                thread::sleep(Duration::from_millis(10));
            }
            false
        });
        let result=discover_cancellable(CliOptions {
            executable:&executable,cwd:root.path(),config_dir:root.path(),
            session_id:"12345678-1234-1234-1234-123456789abc",resume:false,
            model:None,effort:None,permission_mode:"manual",plugins:&[],
        },Duration::from_secs(5),&cancel);
        assert!(watcher.join().unwrap(),"provider fixture did not start");
        assert!(matches!(result,Err(Error::Closed)));
        let pid=fs::read_to_string(root.path().join("catalog.pid")).unwrap().parse::<i32>().unwrap();
        assert_eq!(unsafe { libc::kill(pid,0) },-1,"cancelled catalog child was not reaped");
    }

    #[test]
    fn catalog_decoder_rejects_bad_models_and_bounds_the_provider_list() {
        assert!(decode(&json!({"models":[{"value":"bad\nmodel"}]})).is_err());
        assert!(decode(&json!({"models":[]})).is_err());
        assert!(decode(&json!({"models":vec![json!({"value":"fixture"}); 65]})).is_err());
        let reply = decode(&json!({"models":[{"value":"fixture","supportedEfforts":["high","unknown"]},
            {"value":"fixture"}]})).unwrap();
        assert_eq!(reply["models"], json!(["fixture"]));
        assert_eq!(reply["capabilities"][0]["efforts"], json!(["high"]));
    }
}
