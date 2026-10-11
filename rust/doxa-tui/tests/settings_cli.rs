use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::process::Command;

#[test]
fn native_settings_cli_persists_safely_and_explains_overrides() {
    let dir = tempfile::tempdir().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let binary = env!("CARGO_BIN_EXE_doxa-rs");
    let run = |args: &[&str], override_worktree: Option<&str>| {
        let mut command = Command::new(binary);
        command.args(args).env("DOXA_HOME", dir.path())
            .env_remove("DOXA_LINGER_SECS").env_remove("DOXA_WORKTREE");
        if let Some(value) = override_worktree { command.env("DOXA_WORKTREE", value); }
        command.output().unwrap()
    };
    let initial = run(&["settings"], None);
    assert!(initial.status.success());
    assert!(String::from_utf8_lossy(&initial.stdout).contains("linger_secs: 120 (default)"));
    assert!(run(&["settings", "set", "linger_secs", "45"], None).status.success());
    assert!(run(&["settings", "set", "worktree_per_session", "off"], None).status.success());
    let path = dir.path().join("config.toml");
    let stored: toml::Value = fs::read_to_string(&path).unwrap().parse().unwrap();
    assert_eq!(stored["linger_secs"].as_float(), Some(45.0));
    assert_eq!(stored["worktree_per_session"].as_str(), Some("0"));
    let blocked = run(&["settings", "set", "worktree_per_session", "on"], Some("0"));
    assert!(!blocked.status.success());
    assert!(String::from_utf8_lossy(&blocked.stderr).contains("DOXA_WORKTREE overrides"));
    assert_eq!(fs::read_to_string(&path).unwrap().parse::<toml::Value>().unwrap(), stored);
    for (key,value) in [("background","transparent"),("clock_hour","12"),("clock_show","off"),("notify","always"),("ctx_absolute","on"),("lore","off"),("graph_view","ascii")] {assert!(run(&["settings","set",key,value],None).status.success(),"{key}");}
    let report=run(&["settings"],None);let report=String::from_utf8_lossy(&report.stdout);assert!(report.contains("clock_show: off (config.toml)"));assert!(report.contains("notify: always (config.toml)"));assert!(report.contains("Memory"));
    assert!(run(&["settings", "unset", "linger_secs"], None).status.success());
    let after: toml::Value = fs::read_to_string(&path).unwrap().parse().unwrap();
    assert!(after.get("linger_secs").is_none());
    assert_eq!(after["worktree_per_session"].as_str(), Some("0"));
}

#[test]
fn native_router_settings_requires_current_private_config_and_authenticated_catalog_without_turns() {
    let root=tempfile::tempdir().unwrap();
    fs::set_permissions(root.path(),fs::Permissions::from_mode(0o700)).unwrap();
    let home=root.path().join("home");fs::create_dir(&home).unwrap();
    fs::set_permissions(&home,fs::Permissions::from_mode(0o700)).unwrap();
    let executable=root.path().join("codex");
    fs::write(&executable,r#"#!/usr/bin/env python3
import json, pathlib, sys
for line in sys.stdin:
    request=json.loads(line)
    with open('catalog-calls.jsonl','a') as log:
        log.write(json.dumps(request)+'\n')
    method=request['method']
    if method=='initialized':
        continue
    if method=='initialize':
        result={'userAgent':'codex_cli_rs/0.156.1'}
    elif method=='account/read':
        result={'account':None if pathlib.Path('refuse-auth').exists() else {'type':'apiKey'}}
    elif method=='model/list':
        result={'data':[{'model':model,'supportedReasoningEfforts':[{'reasoningEffort':'high'}],
            'defaultReasoningEffort':'high'} for model in ['fixture-fast','fixture-slow']], 'nextCursor':None}
    else:
        raise AssertionError('setup must not start a thread/turn/tool: '+method)
    print(json.dumps({'id':request['id'],'result':result}),flush=True)
"#).unwrap();
    fs::set_permissions(&executable,fs::Permissions::from_mode(0o700)).unwrap();
    let path=root.path().join("router.json");
    let candidates=["fast","slow"].iter().map(|id|
        serde_json::json!({"id":id,"provider":"codex","billing":"cli_account",
            "model":format!("fixture-{id}"),"effort":"high","description":"Reviewed fixture target",
            "context_tokens":131072,"max_output_tokens":1024,"supports_tools":true})).collect::<Vec<_>>();
    let router=serde_json::json!({"version":1,"jev_model":"jev-1.13.0","criteria_version":"settings-cli-v1",
        "fallback_id":"fast","confidence_threshold":0.5,"max_calls":2,"max_spend_usd_micros":1000,
        "max_input_bytes":2048,"deadline_ms":1000,"candidates":candidates});
    let bytes=serde_json::to_vec(&router).unwrap();fs::write(&path,&bytes).unwrap();
    fs::set_permissions(&path,fs::Permissions::from_mode(0o600)).unwrap();
    let run=|args:&[&str],engine_override:Option<&str>| {
        let mut command=Command::new(env!("CARGO_BIN_EXE_doxa-rs"));
        command.args(args).current_dir(root.path()).env("DOXA_HOME",&home).env("HOME",&home)
            .env("XDG_DATA_HOME",home.join("data")).env("PATH",format!("{}:/usr/bin:/bin",root.path().display()))
            .env("TYPESAFE_API_KEY","settings-cli-review-fixture-key")
            .env_remove("DOXA_ENGINE").env_remove("DOXA_ROUTER_CONFIG")
            .env_remove("DOXA_MODEL").env_remove("DOXA_EFFORT")
            .env_remove("OPENAI_API_KEY").env_remove("ANTHROPIC_API_KEY");
        if let Some(value)=engine_override {command.env("DOXA_ENGINE",value);}
        command.output().unwrap()
    };
    assert!(!run(&["settings","set","engine","router"],None).status.success());
    assert!(!home.join("config.toml").exists());
    assert!(run(&["settings","set","router_config",path.to_str().unwrap()],None).status.success());
    assert!(!root.path().join("catalog-calls.jsonl").exists(),"storing a path must not run providers");
    fs::write(root.path().join("refuse-auth"),b"").unwrap();
    let before=fs::read(home.join("config.toml")).unwrap();
    let refused=run(&["settings","set","engine","router"],None);
    assert!(!refused.status.success());assert_eq!(fs::read(home.join("config.toml")).unwrap(),before);
    fs::remove_file(root.path().join("refuse-auth")).unwrap();
    let enabled=run(&["settings","set","engine","router"],None);
    assert!(enabled.status.success(),"{}",String::from_utf8_lossy(&enabled.stderr));
    let stored:toml::Value=fs::read_to_string(home.join("config.toml")).unwrap().parse().unwrap();
    assert_eq!(stored["engine"].as_str(),Some("router"));assert_eq!(stored["router_config"].as_str(),path.to_str());
    assert_eq!(fs::read(&path).unwrap(),bytes,"review must not rewrite candidate policy");
    let calls:Vec<serde_json::Value>=fs::read_to_string(root.path().join("catalog-calls.jsonl")).unwrap().lines()
        .map(|line|serde_json::from_str(line).unwrap()).collect();
    assert!(calls.iter().any(|call|call["method"]=="account/read"));
    assert!(calls.iter().any(|call|call["method"]=="model/list"));
    assert!(calls.iter().all(|call|matches!(call["method"].as_str(),Some("initialize"|"initialized"|"account/read"|"model/list"))));
    let blocked=run(&["settings","set","engine","claude"],Some("router"));
    assert!(!blocked.status.success());assert!(String::from_utf8_lossy(&blocked.stderr).contains("DOXA_ENGINE overrides"));
    let call_bytes=fs::read(root.path().join("catalog-calls.jsonl")).unwrap();
    fs::write(&path,b"malformed private input").unwrap();
    assert!(run(&["settings","set","notify","always"],None).status.success());
    assert_eq!(fs::read(root.path().join("catalog-calls.jsonl")).unwrap(),call_bytes);
    assert!(run(&["settings","set","engine","claude"],None).status.success());
    assert_eq!(fs::read(root.path().join("catalog-calls.jsonl")).unwrap(),call_bytes);
    let before=fs::read(home.join("config.toml")).unwrap();
    assert!(!run(&["settings","set","router_config",path.to_str().unwrap()],None).status.success());
    assert_eq!(fs::read(home.join("config.toml")).unwrap(),before);
}
