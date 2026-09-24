use doxa_claude::{Bridge, Error, MAX_FRAME};
use serde_json::json;
use std::fs;
use std::time::{Duration, Instant};

fn fake(script: &str) -> (tempfile::TempDir, Bridge) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("fake.py");
    fs::write(&path, format!(r#"import sys,json,time
print(json.dumps({{"type":"hello","protocol":"doxa-claude-sidecar","version":1}}),flush=True)
{script}
"#)).unwrap();
    let bridge = Bridge::spawn("python3", path).unwrap();
    (dir, bridge)
}

#[test]
fn sends_versioned_request_without_shell_and_reads_reply() {
    let (_dir, mut bridge) = fake("line=sys.stdin.readline()\nf=json.loads(line)\nprint(json.dumps({'type':'reply','id':f['id'],'ok':True,'result':{'text':f['params']['text']}}),flush=True)");
    let id = bridge.request("prompt", json!({"text":"a `literal` $(echo no)"})).unwrap();
    let reply = bridge.recv(Duration::from_secs(2)).unwrap();
    assert_eq!(reply["id"], id);
    assert_eq!(reply["result"]["text"], "a `literal` $(echo no)");
}

#[test]
fn rejects_oversized_output_and_input() {
    let (_dir, mut bridge) = fake("print('x'*70000,flush=True)");
    assert!(matches!(bridge.recv(Duration::from_secs(2)), Err(Error::Oversize)));
    assert!(matches!(bridge.request("prompt", json!({"text":"x".repeat(MAX_FRAME)})), Err(Error::Oversize)));
}

#[test]
fn times_out_when_child_stalls() {
    let (_dir, mut bridge) = fake("time.sleep(5)");
    assert!(matches!(bridge.recv(Duration::from_millis(20)), Err(Error::Timeout)));
    assert!(bridge.request("ping", json!({})).is_ok(), "receive timeout must leave idle bridge usable");
}

#[test]
fn write_timeout_when_child_stops_reading() {
    let (_dir, mut bridge) = fake("time.sleep(5)");
    let mut timed_out = false;
    // Pipe capacity varies by runner. Fill it with bounded frames until a
    // write actually blocks; a completed write is a valid result.
    for _ in 0..512 {
        match bridge.request_with_timeout(
            "prompt", json!({"text":"x".repeat(MAX_FRAME - 200)}),
            Duration::from_millis(50),
        ) {
            Ok(_) => continue,
            Err(Error::Timeout) => { timed_out = true; break; }
            Err(other) => panic!("unexpected write result: {other}"),
        }
    }
    assert!(timed_out, "pipe did not fill after 512 near-max frames");
    assert!(matches!(bridge.request("ping", json!({})), Err(Error::Closed)));
}

#[test]
fn dropping_bridge_kills_sidecar_descendants() {
    let dir = tempfile::tempdir().unwrap();
    let ready = dir.path().join("ready");
    let marker = dir.path().join("descendant-survived");
    let child_code = format!(
        "import time; time.sleep(0.7); open({}, 'w').write('survived')",
        serde_json::to_string(&marker.to_string_lossy()).unwrap()
    );
    let script = format!(
        "import subprocess\nsubprocess.Popen([sys.executable, '-c', {:?}])\nopen({}, 'w').write('ready')\ntime.sleep(5)",
        child_code,
        serde_json::to_string(&ready.to_string_lossy()).unwrap()
    );
    let (_fake_dir, bridge) = fake(&script);
    let deadline = Instant::now() + Duration::from_secs(2);
    while !ready.exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(5));
    }
    assert!(ready.exists(), "fake sidecar did not spawn its descendant");
    drop(bridge);
    std::thread::sleep(Duration::from_millis(900));
    assert!(!marker.exists(), "descendant outlived the bridge");
}
