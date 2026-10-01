use doxa_claude::{Cli, CliOptions, Error};
use serde_json::json;
use std::{fs, os::unix::fs::PermissionsExt, time::Duration};
const SESSION: &str = "5b9aac56-c75e-4b93-ab07-c59f1c5a0b39";
fn fake(body: &str) -> (tempfile::TempDir, Cli) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("claude");
    fs::write(&path,format!("#!/usr/bin/python3\nimport sys,json,time\nassert '--bare' not in sys.argv\nassert '--session-id' in sys.argv\nassert '--include-partial-messages' in sys.argv\nassert sys.argv[sys.argv.index('--permission-prompts')+1] == 'host'\nassert sys.argv[sys.argv.index('--permission-prompt-tool')+1] == 'stdio'\n{body}\n")).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
    let cli = Cli::spawn(CliOptions {
        executable: &path,
        cwd: dir.path(),
        session_id: SESSION,
        resume: false,
        model: Some("opus"),
        effort: Some("high"),
        permission_mode: "manual",
        config_dir: dir.path(),
        plugins: &[],
    })
    .unwrap();
    (dir, cli)
}
#[test]
fn exact_bidirectional_envelopes_and_immediate_partial_messages() {
    let (_dir, mut cli) = fake(
        r#"for line in sys.stdin:
 row=json.loads(line)
 if row['type']=='control_request':
  assert row['request']['subtype']=='get_settings'
  print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':row['request_id'],'response':{'applied':{'model':'claude-opus-5-5','effort':'high'}}}}),flush=True)
 if row['type']=='user':
  assert row['message']['content']=='a `literal` $(echo no)'
  print(json.dumps({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':'first token'}}}),flush=True)
  print(json.dumps({'type':'control_request','request_id':'permission-exact','request':{'subtype':'can_use_tool','tool_name':'AskUserQuestion','input':{'questions':[]}}}),flush=True)
 if row['type']=='control_response':
  assert row['response']['request_id']=='permission-exact'
  assert row['response']['response']['behavior']=='deny'
  print(json.dumps({'type':'result','is_error':False}),flush=True)
"#,
    );
    let id = cli.control(json!({"subtype":"get_settings"})).unwrap();
    let settings = cli.recv(Duration::from_secs(2)).unwrap();
    assert_eq!(settings["response"]["request_id"], id);
    cli.prompt("a `literal` $(echo no)", SESSION).unwrap();
    assert_eq!(
        cli.recv(Duration::from_secs(2)).unwrap()["event"]["delta"]["text"],
        "first token"
    );
    assert_eq!(
        cli.recv(Duration::from_secs(2)).unwrap()["request_id"],
        "permission-exact"
    );
    cli.respond("permission-exact", Ok(json!({"behavior":"deny"})))
        .unwrap();
    assert_eq!(cli.recv(Duration::from_secs(2)).unwrap()["type"], "result");
}
#[test]
fn rejects_oversize_and_bounded_blocking_writes() {
    let (_dir, mut cli) = fake("time.sleep(5)");
    assert!(matches!(
        cli.recv(Duration::from_millis(10)),
        Err(Error::Timeout)
    ));
    assert!(matches!(
        cli.send(json!({"text":"x".repeat(doxa_claude::cli::MAX_CLI_FRAME)})),
        Err(Error::Oversize)
    ));
    let mut blocked = false;
    for _ in 0..512 {
        match cli.send_timeout(
            json!({"text":"x".repeat(60_000)}),
            Duration::from_millis(30),
        ) {
            Ok(_) => {}
            Err(Error::Timeout) => {
                blocked = true;
                break;
            }
            other => panic!("{other:?}"),
        }
    }
    assert!(blocked);
    assert!(matches!(
        cli.control(json!({"subtype":"get_settings"})),
        Err(Error::Closed)
    ));
}
#[test]
fn canonical_uuid_only() {
    assert!(doxa_claude::cli::canonical_session_id(SESSION));
    for invalid in ["5b9aac56c75e4b93ab07c59f1c5a0b39", "test", "../../id", ""] {
        assert!(!doxa_claude::cli::canonical_session_id(invalid));
    }
}
