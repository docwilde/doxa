//! Prospective session catalogs use the same provider CLI and account as launch.
//! Discovery starts no DOXA daemon, thread, user turn or inference request.
use crate::launch::{self, Engine, LaunchOptions};
use doxa_engines::codex_appserver::{AppServerDriver, AppServerOptions, CodexPermission};
use doxa_engines::codex_driver::SandboxMode;
use serde_json::{json, Value};
use std::{path::Path, time::Duration};

const DEADLINE: Duration = Duration::from_secs(20);

pub(crate) fn load(engine: Engine, cwd: &Path) -> Result<Value, &'static str> {
    match engine {
        Engine::Codex => {
            let executable = launch::codex_executable(&LaunchOptions::default())
                .map_err(|_| "Codex executable unavailable")?;
            codex_at(&executable, cwd)
        }
        Engine::Claude => {
            let options = LaunchOptions { claude_bin: std::env::var_os("DOXA_CLAUDE_BIN").map(Into::into), ..Default::default() };
            let executable = launch::claude_executable(&options).map_err(|_| "Claude executable unavailable")?;
            doxa_claude::isolation::sync_credentials(false).map_err(|_| "Claude account unavailable")?;
            let config_dir = doxa_claude::isolation::config_dir();
            let session = uuid::Uuid::new_v4().to_string();
            doxa_claude::catalog::discover(doxa_claude::CliOptions {
                executable: &executable, cwd, session_id: &session, resume: false,
                model: None, effort: None, permission_mode: "manual", config_dir: &config_dir, plugins: &[],
            }, DEADLINE).map_err(|_| "Claude model catalog unavailable")
        }
        _ => Err("This engine uses its own model catalog"),
    }
}

fn codex_at(executable: &Path, cwd: &Path) -> Result<Value, &'static str> {
    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build()
        .map_err(|_| "Codex model catalog unavailable")?;
    let rows = runtime.block_on(async { tokio::time::timeout(DEADLINE,
        AppServerDriver::discover_models(AppServerOptions {
            executable: executable.into(), cwd: cwd.into(), model: None, sandbox: SandboxMode::ReadOnly,
            permission: CodexPermission::OnRequest, resume_thread: None, turn_timeout: DEADLINE,
        })).await }).map_err(|_| "Codex model catalog timed out")?
        .map_err(|_| "Codex model catalog unavailable")?;
    Ok(json!({"models":rows.iter().map(|row| row["model"].clone()).collect::<Vec<_>>(),
        "capabilities":rows,"note":"Live account model catalog from Codex"}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::PermissionsExt};

    #[test]
    fn codex_model_discovery_reads_paginated_catalog_without_starting_a_thread() {
        let root = tempfile::tempdir().unwrap();
        let executable = root.path().join("codex-fixture");
        fs::write(&executable, r#"#!/usr/bin/env python3
import json, pathlib, sys
for line in sys.stdin:
    request = json.loads(line)
    with open('calls.jsonl', 'a') as calls:
        calls.write(json.dumps(request) + '\n')
    method = request['method']
    if method == 'initialized':
        continue
    if method == 'initialize':
        result = {'userAgent':'codex_cli_rs/0.156.1'}
    elif method == 'model/list':
        second = request['params']['cursor'] == 'page-2'
        result = {'data':[{'model':'fixture-b' if second else 'fixture-a',
            'isDefault':second,'supportedReasoningEfforts':[{'reasoningEffort':'high'}],
            'defaultReasoningEffort':'high'}], 'nextCursor':None if second else 'page-2'}
    else:
        raise AssertionError('discovery started an unexpected operation: ' + method)
    print(json.dumps({'id':request['id'],'result':result}), flush=True)
"#).unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
        let reply = codex_at(&executable, root.path()).unwrap();
        assert_eq!(reply["models"], json!(["fixture-a", "fixture-b"]));
        assert_eq!(reply["capabilities"][1]["is_default"], true);
        let calls: Vec<Value> = fs::read_to_string(root.path().join("calls.jsonl")).unwrap()
            .lines().map(|line| serde_json::from_str(line).unwrap()).collect();
        assert_eq!(calls.iter().map(|call| call["method"].as_str().unwrap()).collect::<Vec<_>>(),
            ["initialize", "initialized", "model/list", "model/list"]);
    }
}
