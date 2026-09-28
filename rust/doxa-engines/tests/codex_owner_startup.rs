#![cfg(target_os = "linux")]
use doxa_engines::{codex_appserver::{AppServerDriver,AppServerOptions}, codex_compact::CompactGate,codex_driver::SandboxMode};
use std::{fs,os::unix::fs::PermissionsExt,time::{Duration,Instant}};
include!("fixtures/codex_protected_fixture.rs");

#[tokio::test]
async fn unsupported_or_silent_owner_is_killed_on_refusal_or_startup_cancellation() {
    for mode in ["wrong","silent"] {
        let (dir,options,gate)=fixture("model");
        fs::write(&options.executable,format!(r#"#!/usr/bin/python3
import os,socket,time
open('unsupported-owner.pid','w').write(str(os.getpid()))
if {wrong}:
 control=socket.socket(fileno=int(os.environ['DOXA_CODEX_OWNER_FD']))
 control.sendall(b'XXXXXXXXXXXXXXXXXXXXXXX')
time.sleep(60)
"#,wrong=if mode=="wrong" {"True"} else {"False"})).unwrap();
        fs::set_permissions(&options.executable,fs::Permissions::from_mode(0o700)).unwrap();
        let result=tokio::time::timeout(Duration::from_millis(300),AppServerDriver::spawn_protected(options,str::to_owned,false,gate)).await;
        assert!(result.is_err() || result.unwrap().is_err());
        let pid=fs::read_to_string(dir.path().join("unsupported-owner.pid")).unwrap();
        let deadline=Instant::now()+Duration::from_secs(3);
        while std::path::Path::new(&format!("/proc/{pid}")).exists() {
            assert!(Instant::now()<deadline,"unsupported owner survived cancellation");
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }
}
