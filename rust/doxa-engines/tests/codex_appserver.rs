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
send({'method':'item/reasoning/textDelta','params':{'threadId':'thread_1','turnId':'turn_1','itemId':'r','delta':'thinking'}})
send({'id':turn['id'],'result':{'turn':{'id':'turn_1'}}})
send({'id':99,'method':'item/commandExecution/requestApproval','params':{'threadId':'thread_1','turnId':'turn_1','itemId':'cmd'}})
assert read()['result']['decision'] == 'decline'
send({'method':'item/agentMessage/delta','params':{'threadId':'thread_1','turnId':'turn_1','itemId':'a','delta':'answer'}})
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
async fn fake_appserver_streams_reasoning_and_exact_usage_and_denies_approval() {
    let (_dir, options) = fake();
    let mut driver = AppServerDriver::spawn(options, |s| s.replace("answer", "clean")).await.unwrap();
    assert_eq!(driver.thread_id(), "thread_1");
    let mut events = Vec::new();
    driver.run_turn("hello", &CancellationToken::new(), |event| events.push(event)).await.unwrap();
    assert_eq!(events.iter().map(|event| event.kind.as_str()).collect::<Vec<_>>(),
        ["reasoning_delta", "text_delta", "usage", "turn_done"]);
    assert_eq!(events[0].data["count_is_estimate"], true);
    assert_eq!(events[1].data["text"], "clean");
    assert_eq!(events[2].data["reasoning_output_tokens"], 7);
    assert_eq!(events[2].data["context_window"], 200000);
    assert_eq!(events[3].data["is_error"], false);
}

#[tokio::test]
async fn fake_appserver_resume_uses_the_recorded_thread() {
    let (_dir, mut options) = fake();
    options.resume_thread = Some("thread_1".into());
    let mut driver = AppServerDriver::spawn(options, |s| s.to_owned()).await.unwrap();
    driver.run_turn("hello", &CancellationToken::new(), |_| {}).await.unwrap();
}
