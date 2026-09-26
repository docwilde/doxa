#![cfg(unix)]
use doxa_engines::codex_appserver::{AppServerDriver, AppServerOptions};
use doxa_engines::codex_driver::SandboxMode;
use std::os::unix::fs::PermissionsExt;
use std::time::Duration;
use tokio_util::sync::CancellationToken;

fn fake() -> (tempfile::TempDir, AppServerOptions) {
    let cache = std::env::var_os("TMPDIR").map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target/doxa-test-cache"));
    std::fs::create_dir_all(&cache).unwrap();
    let dir = tempfile::tempdir_in(cache).unwrap();
    let executable = dir.path().join("fake-codex");
    std::fs::write(&executable, r#"#!/usr/bin/env python3
import json, sys

def read():
    return json.loads(sys.stdin.readline())
def send(value):
    print(json.dumps(value), flush=True)

init = read()
assert init['method'] == 'initialize' and init['params']['clientInfo']['title'] is None
send({'id':init['id'],'result':{'userAgent':'fake'}})
assert read()['method'] == 'initialized'
thread = read()
assert thread['method'] in ('thread/start','thread/resume')
if thread['method'] == 'thread/resume':
    assert thread['params']['threadId'] == 'thread_1'
else:
    assert thread['params']['approvalPolicy'] == 'never'
send({'id':thread['id'],'result':{'thread':{'id':'thread_1'}}})
turn = read()
assert turn['method'] == 'turn/start'
assert turn['params']['input'][0]['text'] == 'hello'
send({'method':'item/reasoning/textDelta','params':{'threadId':'thread_1','turnId':'turn_1','itemId':'r','delta':'sec'}})
send({'method':'item/reasoning/textDelta','params':{'threadId':'thread_1','turnId':'turn_1','itemId':'r','delta':'ret'}})
send({'id':turn['id'],'result':{'turn':{'id':'turn_1'}}})
send({'method':'item/started','params':{'threadId':'thread_1','turnId':'turn_1','item':{'type':'commandExecution','id':'cmd_1','command':'echo secret'}}})
send({'method':'item/completed','params':{'threadId':'thread_1','turnId':'turn_1','item':{'type':'commandExecution','id':'cmd_1','command':'echo secret','status':'completed','aggregatedOutput':'secret result','exitCode':0}}})
send({'method':'item/agentMessage/delta','params':{'threadId':'thread_1','turnId':'turn_1','itemId':'a','delta':'ans'}})
send({'method':'item/agentMessage/delta','params':{'threadId':'thread_1','turnId':'turn_1','itemId':'a','delta':'wer'}})
send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread_1','turnId':'turn_1','tokenUsage':{'last':{'totalTokens':100,'inputTokens':80,'outputTokens':20,'cachedInputTokens':10,'reasoningOutputTokens':7},'modelContextWindow':200000}}})
send({'method':'turn/completed','params':{'threadId':'thread_1','turn':{'id':'turn_1','status':'completed','error':None}}})
"#).unwrap();
    let mut perms = std::fs::metadata(&executable).unwrap().permissions();
    perms.set_mode(0o700);
    std::fs::set_permissions(&executable, perms).unwrap();
    let options = AppServerOptions {
        executable, cwd: dir.path().to_path_buf(), model: None,
        sandbox: SandboxMode::WorkspaceWrite, resume_thread: None,
        turn_timeout: Duration::from_secs(5),
    };
    (dir, options)
}

#[tokio::test]
async fn fake_appserver_streams_reasoning_progress_and_exact_usage() {
    let (_dir, options) = fake();
    let mut driver = AppServerDriver::spawn(options, |s| s.replace("answer", "clean").replace("secret", "[redacted]")).await.unwrap();
    assert_eq!(driver.thread_id(), "thread_1");
    let mut events = Vec::new();
    driver.run_turn("hello", &CancellationToken::new(), |event| events.push(event)).await.unwrap();
    assert_eq!(events.iter().map(|event| event.kind.as_str()).collect::<Vec<_>>(),
        ["reasoning_progress", "reasoning_progress", "tool_call", "tool_result", "tool_result_detail", "usage", "reasoning_delta", "text_delta", "turn_done"]);
    assert_eq!(events[0].data["count_is_estimate"], true);
    assert_eq!(events[2].data["input"]["command"], "echo [redacted]");
    assert_eq!(events[4].data["text"], "[redacted] result");
    assert_eq!(events[6].data["text"], "[redacted]");
    assert_eq!(events[7].data["text"], "clean");
    assert_eq!(events[5].data["inference_reasoning_output_tokens"], 7);
    assert_eq!(events[5].data["context_window"], 200000);
    assert_eq!(events[8].data["is_error"], false);
    assert!(events[8].data["reasoning_output_tokens"].is_null());
    assert_eq!(events[8].data["reasoning_count_is_estimate"], true);
    assert!(events.iter().all(|event| !event.data.to_string().contains("secret")));
}

#[tokio::test]
async fn fake_appserver_resume_uses_the_recorded_thread() {
    let (_dir, mut options) = fake();
    options.resume_thread = Some("thread_1".into());
    let mut driver = AppServerDriver::spawn(options, |s| s.to_owned()).await.unwrap();
    driver.run_turn("hello", &CancellationToken::new(), |_| {}).await.unwrap();
}

#[tokio::test]
async fn cancellation_interrupts_and_reaps_appserver_process_group() {
    let cache = std::env::var_os("TMPDIR").map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../target/doxa-test-cache"));
    std::fs::create_dir_all(&cache).unwrap();
    let dir = tempfile::tempdir_in(cache).unwrap();
    let executable = dir.path().join("fake-codex");
    let marker = dir.path().join("descendant-survived");
    let script = r#"#!/usr/bin/env python3
import json, subprocess, sys, time

def read(): return json.loads(sys.stdin.readline())
def send(v): print(json.dumps(v),flush=True)
init=read(); send({'id':init['id'],'result':{}})
assert read()['method']=='initialized'
thread=read(); send({'id':thread['id'],'result':{'thread':{'id':'thread_1'}}})
turn=read(); send({'id':turn['id'],'result':{'turn':{'id':'turn_1'}}})
subprocess.Popen(['/bin/sh','-c','sleep 1; touch __MARKER__'])
request=read()
assert request['method']=='turn/interrupt'
time.sleep(3)
"#.replace("__MARKER__", marker.to_str().unwrap());
    std::fs::write(&executable, script).unwrap();
    let mut perms = std::fs::metadata(&executable).unwrap().permissions();
    perms.set_mode(0o700);
    std::fs::set_permissions(&executable, perms).unwrap();
    let options = AppServerOptions { executable, cwd: dir.path().to_path_buf(), model: None,
        sandbox: SandboxMode::WorkspaceWrite, resume_thread: None,
        turn_timeout: Duration::from_secs(5) };
    let mut driver = AppServerDriver::spawn(options, |s| s.to_owned()).await.unwrap();
    let cancel = CancellationToken::new();
    let trigger = cancel.clone();
    tokio::spawn(async move { tokio::time::sleep(Duration::from_millis(100)).await; trigger.cancel(); });
    assert!(matches!(driver.run_turn("hello", &cancel, |_| {}).await,
        Err(doxa_engines::codex_appserver::AppServerError::Cancelled)));
    drop(driver);
    tokio::time::sleep(Duration::from_millis(1200)).await;
    assert!(!marker.exists(), "app-server descendant survived cancellation");
}

#[tokio::test]
async fn approval_is_refused_with_a_clear_error() {
    let (dir, options) = fake();
    let marker = dir.path().join("approval-denied");
    let script = std::fs::read_to_string(&options.executable).unwrap();
    let injection = format!("send({{'id':99,'method':'item/commandExecution/requestApproval','params':{{'threadId':'thread_1','turnId':'turn_1','itemId':'cmd'}}}})\nassert read()['result']['decision'] == 'decline'\nopen({:?}, 'w').write('denied')\n", marker.to_str().unwrap());
    let script = script.replace("send({'id':turn['id'],'result':{'turn':{'id':'turn_1'}}})", &format!("send({{'id':turn['id'],'result':{{'turn':{{'id':'turn_1'}}}}}})\n{injection}"));
    std::fs::write(&options.executable, script).unwrap();
    let mut driver = AppServerDriver::spawn(options, |s| s.to_owned()).await.unwrap();
    let outcome = driver.run_turn("hello", &CancellationToken::new(), |_| {}).await;
    assert!(matches!(outcome, Err(doxa_engines::codex_appserver::AppServerError::Server(ref message)) if message.contains("refused") && message.contains("not supported")));
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(std::fs::read_to_string(marker).unwrap(), "denied");
}

#[tokio::test]
async fn turn_write_honors_timeout_and_cancellation_when_server_stops_reading() {
    for cancel_write in [false, true] {
        let (_dir, mut options) = fake();
        let script = std::fs::read_to_string(&options.executable).unwrap()
            .replace("turn = read()", "import time; time.sleep(10)\nturn = read()");
        std::fs::write(&options.executable, script).unwrap();
        options.turn_timeout = if cancel_write { Duration::from_secs(3) } else { Duration::from_millis(80) };
        let mut driver = AppServerDriver::spawn(options, |s| s.to_owned()).await.unwrap();
        let cancel = CancellationToken::new();
        if cancel_write {
            let trigger = cancel.clone();
            tokio::spawn(async move { tokio::time::sleep(Duration::from_millis(80)).await; trigger.cancel(); });
        }
        let started = std::time::Instant::now();
        let result = driver.run_turn(&"a".repeat(4 * 1024 * 1024), &cancel, |_| {}).await;
        if cancel_write {
            assert!(matches!(result, Err(doxa_engines::codex_appserver::AppServerError::Cancelled)));
        } else {
            assert!(matches!(result, Err(doxa_engines::codex_appserver::AppServerError::TimedOut)));
        }
        assert!(started.elapsed() < Duration::from_secs(1));
        driver.shutdown().await;
    }
}

#[tokio::test]
async fn group_cleanup_kills_descendant_after_server_leader_exits() {
    let (dir, options) = fake();
    let marker = dir.path().join("orphan-survived");
    let script = std::fs::read_to_string(&options.executable).unwrap();
    let injection = format!("import subprocess\nsubprocess.Popen(['/bin/sh','-c','sleep 1; touch {}'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\nsys.exit(0)\n", marker.display());
    let script = script.replace("send({'id':turn['id'],'result':{'turn':{'id':'turn_1'}}})", &format!("send({{'id':turn['id'],'result':{{'turn':{{'id':'turn_1'}}}}}})\n{injection}"));
    std::fs::write(&options.executable, script).unwrap();
    let mut driver = AppServerDriver::spawn(options, |s| s.to_owned()).await.unwrap();
    assert!(driver.run_turn("hello", &CancellationToken::new(), |_| {}).await.is_err());
    driver.shutdown().await;
    drop(driver);
    tokio::time::sleep(Duration::from_millis(1200)).await;
    assert!(!marker.exists(), "orphan tool survived an exited app-server leader");
}

#[tokio::test]
async fn completed_only_snapshots_and_oversized_item_ids_are_bounded() {
    for oversized_id in [false, true] {
        let (_dir, options) = fake();
        let script = std::fs::read_to_string(&options.executable).unwrap();
        let injection = if oversized_id {
            "send({'method':'item/agentMessage/delta','params':{'threadId':'thread_1','turnId':'turn_1','itemId':'x'*1000,'delta':'answer'}})\n".to_owned()
        } else {
            "for i in range(3):\n    send({'method':'item/completed','params':{'threadId':'thread_1','turnId':'turn_1','item':{'type':'agentMessage','id':'msg_'+str(i),'text':'x'*(3*1024*1024)}}})\n".to_owned()
        };
        let script = script.replace("send({'id':turn['id'],'result':{'turn':{'id':'turn_1'}}})", &format!("send({{'id':turn['id'],'result':{{'turn':{{'id':'turn_1'}}}}}})\n{injection}"));
        std::fs::write(&options.executable, script).unwrap();
        let mut driver = AppServerDriver::spawn(options, |s| s.to_owned()).await.unwrap();
        let mut emitted_bytes = 0;
        let result = driver.run_turn("hello", &CancellationToken::new(), |event| {
            if event.kind == "text_delta" { emitted_bytes += event.data["text"].as_str().unwrap().len(); }
        }).await;
        assert!(matches!(result, Err(doxa_engines::codex_appserver::AppServerError::Protocol(_))));
        assert!(emitted_bytes <= 8 * 1024 * 1024);
        driver.shutdown().await;
    }
}

#[tokio::test]
async fn multiple_inferences_do_not_promote_last_reasoning_usage_to_turn_total() {
    let (_dir, options) = fake();
    let script = std::fs::read_to_string(&options.executable).unwrap();
    let prefix = "send({'method':'turn/completed'";
    let second_usage = "send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread_1','turnId':'turn_1','tokenUsage':{'total':{'reasoningOutputTokens':10},'last':{'reasoningOutputTokens':3,'totalTokens':100},'modelContextWindow':200000}}})\n";
    std::fs::write(&options.executable, script.replace(prefix, &format!("{second_usage}{prefix}"))).unwrap();
    let mut driver = AppServerDriver::spawn(options, |s| s.to_owned()).await.unwrap();
    let mut terminal = None;
    driver.run_turn("hello", &CancellationToken::new(), |event| {
        if event.kind == "turn_done" { terminal = Some(event.data); }
    }).await.unwrap();
    let terminal = terminal.unwrap();
    assert!(terminal["reasoning_output_tokens"].is_null());
    assert_eq!(terminal["reasoning_count_is_estimate"], true);
}

#[tokio::test]
async fn already_cancelled_turn_never_submits_a_prompt() {
    let (dir, options) = fake();
    let marker = dir.path().join("prompt-submitted");
    let script = std::fs::read_to_string(&options.executable).unwrap();
    let script = script.replace("turn = read()", &format!("turn = read()\nopen({:?},'w').write('submitted')", marker.to_str().unwrap()));
    std::fs::write(&options.executable, script).unwrap();
    let mut driver = AppServerDriver::spawn(options, |s| s.to_owned()).await.unwrap();
    let cancel = CancellationToken::new();
    cancel.cancel();
    assert!(matches!(driver.run_turn("hello", &cancel, |_| {}).await,
        Err(doxa_engines::codex_appserver::AppServerError::Cancelled)));
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(!marker.exists());
    driver.shutdown().await;
}

#[tokio::test]
async fn completed_messages_are_separated_and_split_deltas_scrub_as_one_message() {
    let (_dir, options) = fake();
    let script = std::fs::read_to_string(&options.executable).unwrap();
    let insertion = r#"
send({'method':'item/completed','params':{'threadId':'thread_1','turnId':'turn_1','item':{'type':'agentMessage','id':'empty','text':''}}})
send({'method':'item/agentMessage/delta','params':{'threadId':'thread_1','turnId':'turn_1','itemId':'first','delta':'sec'}})
send({'method':'item/agentMessage/delta','params':{'threadId':'thread_1','turnId':'turn_1','itemId':'first','delta':'ret first'}})
send({'method':'item/completed','params':{'threadId':'thread_1','turnId':'turn_1','item':{'type':'agentMessage','id':'first'}}})
send({'method':'item/completed','params':{'threadId':'thread_1','turnId':'turn_1','item':{'type':'agentMessage','id':'empty_middle','text':''}}})
send({'method':'item/completed','params':{'threadId':'thread_1','turnId':'turn_1','item':{'type':'agentMessage','id':'second','text':'secret second'}}})
"#;
    let response = "send({'id':turn['id'],'result':{'turn':{'id':'turn_1'}}})";
    std::fs::write(&options.executable, script.replace(response, &format!("{response}\n{insertion}"))).unwrap();
    let mut driver = AppServerDriver::spawn(options, |s| s.replace("secret", "[redacted]")).await.unwrap();
    let mut messages = Vec::new();
    driver.run_turn("hello", &CancellationToken::new(), |event| {
        if event.kind == "text_delta" { messages.push(event.data["text"].as_str().unwrap().to_owned()); }
    }).await.unwrap();
    assert_eq!(messages, ["[redacted] first", "\n\n[redacted] second", "\n\nanswer"]);
    assert_eq!(messages.concat(), "[redacted] first\n\n[redacted] second\n\nanswer");
    assert!(messages.concat().len() <= 8 * 1024 * 1024);
}
