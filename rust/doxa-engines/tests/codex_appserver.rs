#![cfg(unix)]
use doxa_engines::codex_appserver::{AppServerDriver, AppServerOptions};
use doxa_engines::codex_driver::SandboxMode;
use std::os::unix::fs::PermissionsExt;
use std::time::Duration;
use tokio_util::sync::CancellationToken;

fn fake() -> (tempfile::TempDir, AppServerOptions) {
    let cache = std::env::var("TMPDIR").expect("tests must put temporary files in cache, not /tmp");
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
    assert_eq!(events[5].data["reasoning_output_tokens"], 7);
    assert_eq!(events[5].data["context_window"], 200000);
    assert_eq!(events[8].data["is_error"], false);
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
    let cache = std::env::var("TMPDIR").expect("tests must use cache TMPDIR");
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
