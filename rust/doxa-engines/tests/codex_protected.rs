#![cfg(unix)]
use doxa_engines::{codex_appserver::{AppServerDriver, AppServerOptions}, codex_compact::CompactGate, codex_driver::SandboxMode, codex_interaction::InputInbox};
use serde_json::{json, Value};
use std::{fs, os::unix::fs::PermissionsExt, sync::{Arc, atomic::{AtomicUsize, Ordering}}, time::Duration};
use tokio_util::sync::CancellationToken;

include!("fixtures/codex_protected_fixture.rs");

#[tokio::test]
async fn authoritative_version_and_hook_hash_refuse_before_thread_creation() {
    for mode in ["version", "hash", "token-budget"] {
        let (dir, options, gate) = fixture(mode);
        assert!(AppServerDriver::spawn_protected(options, str::to_owned, false, gate).await.is_err());
        assert!(!dir.path().join(format!("unsafe-after-{mode}")).exists());
    }
}

#[tokio::test]
async fn protected_initial_model_is_verified_before_any_turn() {
    for requested in ["gpt-5.5", "gpt-6-astra"] {
        let (dir, mut options, gate)=fixture("model");
        options.model=Some(requested.into());
        let result=AppServerDriver::spawn_protected(options,str::to_owned,false,gate).await;
        assert_eq!(result.is_ok(),requested=="gpt-5.5");
        if let Ok(mut driver)=result {driver.shutdown().await;}
        assert!(!dir.path().join("unsafe-model-turn").exists());
    }
}

#[tokio::test]
async fn peer_dynamic_tool_requires_matching_single_use_permission_and_cancels_pending() {
    for mode in ["peer-allow", "peer-deny", "peer-cancel"] {
        let (dir, options, gate)=fixture(mode);
        let mut driver=AppServerDriver::spawn_protected(options,str::to_owned,true,gate).await.unwrap();
        let inbox=Arc::new(InputInbox::default());
        let count=Arc::new(AtomicUsize::new(0)); let called=count.clone();
        let handler:doxa_engines::peer_tools::Handler=Arc::new(move |rpc:&str,args:&Value| {
            assert_eq!(rpc,"msg"); assert_eq!(args,&json!({"target":"peer-exact","text":"hello"}));
            called.fetch_add(1,Ordering::SeqCst); Ok(json!({"delivered":true}))
        });
        let cancel=CancellationToken::new(); let trigger=cancel.clone(); let input=inbox.clone();
        let result=driver.run_turn_interactive("hello",&cancel,|event| {
            if event.kind=="needs_input" {
                if mode=="peer-cancel" { trigger.cancel(); }
                else { input.answer(event.data["id"].as_str().unwrap(),&json!({"decision":if mode=="peer-allow" {"allow"} else {"deny"}})).unwrap(); }
            }
        },|frame| inbox.begin_peer(frame,str::to_owned,handler.clone()).map(Some)).await;
        assert_eq!(result.is_ok(),mode!="peer-cancel");
        assert_eq!(count.load(Ordering::SeqCst),usize::from(mode=="peer-allow"));
        if mode!="peer-cancel" { assert!(dir.path().join("peer-reply").exists()); }
        inbox.clear(); driver.shutdown().await;
    }
}

#[tokio::test]
async fn codex_dynamic_aliases_route_all_canonical_handlers_and_refuse_unadvertised_namespaces() {
    let names = ["peer_list", "peer_send", "peer_history", "lore_belief_search", "lore_belief_show",
        "lore_belief_neighbours", "lore_memory_list", "lore_session_search", "lore_remember"];
    for mode in ["alias-all", "alias-forged", "alias-namespace"] {
        let (dir, options, gate) = fixture(mode);
        let definitions = names[3..].iter().map(|name| json!({"type":"function",
            "name":format!("mcp__doxa__{name}"), "description":"fixture operator",
            "inputSchema":{"type":"object","properties":{},"additionalProperties":false}})).collect();
        let mut driver = AppServerDriver::spawn_protected_with_agent_tools(options, str::to_owned, true, gate, definitions).await.unwrap();
        let mut called = Vec::new();
        let mut events = Vec::new();
        let result = driver.run_turn_interactive("fixture", &CancellationToken::new(), |event| events.push(event), |frame| {
            let canonical = frame["params"]["tool"].as_str().unwrap();
            assert_eq!(canonical, format!("mcp__doxa__{}", names[called.len()]));
            called.push(canonical.to_owned());
            let (sender, receiver) = tokio::sync::oneshot::channel();
            sender.send(json!({"success":true,"contentItems":[]})).unwrap();
            Ok(Some((doxa_engines::EngineEvent::new("needs_input", json!({"id":"fixture-gate"})), receiver)))
        }).await;
        assert_eq!(result.is_ok(), mode == "alias-all");
        assert_eq!(called.len(), if mode == "alias-all" { 9 } else { 0 });
        if mode == "alias-all" {
            let displayed: Vec<_> = events.iter().filter(|event| event.kind == "tool_call")
                .map(|event| event.data["name"].as_str().unwrap()).collect();
            assert_eq!(displayed, called.iter().map(String::as_str).collect::<Vec<_>>());
        }
        tokio::time::timeout(Duration::from_secs(1), async {
            while !dir.path().join("alias-replies").exists() { tokio::time::sleep(Duration::from_millis(5)).await; }
        }).await.expect("fixture received the matching dynamic-tool reply");
        driver.shutdown().await;
    }
    // Shared Claude MCP/vendor handler contracts retain their canonical names.
    assert_eq!(doxa_engines::peer_tools::definitions()[0]["name"], "mcp__doxa__peer_list");
}


#[tokio::test]
async fn refused_native_review_sends_no_compaction_rpc_and_retains_bound_source() {
    for mode in ["missing-source", "foreign-source", "foreign-thread", "native-review-failure"] {
        let (dir, options, gate) = fixture(mode);
        let mut driver = AppServerDriver::spawn_protected(options, str::to_owned, false, gate).await.unwrap();
        assert!(driver.compact(&CancellationToken::new(), |_| {}).await.is_err());
        let requests = fs::read_to_string(dir.path().join("requests.jsonl")).unwrap();
        assert!(!requests.contains("thread/compact/start"), "{mode}: {requests}");
        assert_eq!(driver.thread_id(), "thread-actual");
        if mode == "native-review-failure" {
            assert_eq!(fs::read(dir.path().join("codex-home/sessions/thread.jsonl")).unwrap(), fs::read(dir.path().join("source-before")).unwrap());
        }
        driver.shutdown().await;
    }
}

#[tokio::test]
async fn provider_slash_compaction_never_bypasses_native_review() {
    let (dir, options, gate) = fixture("native-review-failure");
    let mut driver = AppServerDriver::spawn_protected(options, str::to_owned, false, gate).await.unwrap();
    for text in ["/compact", "  /compact  ", "/compact instructions"] {
        assert!(driver.run_turn(text, &CancellationToken::new(), |_| {}).await.is_err());
    }
    let requests = fs::read_to_string(dir.path().join("requests.jsonl")).unwrap();
    assert!(!requests.contains("turn/start") && !requests.contains("thread/compact/start"));
    driver.shutdown().await;
}
