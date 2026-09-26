#![cfg(unix)]
use std::{fs, io::Write, os::{fd::FromRawFd, unix::fs::{MetadataExt, PermissionsExt}}, process::{Command, Stdio}};

struct Fixture { root:tempfile::TempDir, bin:std::path::PathBuf }
impl Fixture {
    fn new() -> Self {
        let cache=std::env::var_os("TMPDIR").map(std::path::PathBuf::from)
            .unwrap_or_else(|| std::path::PathBuf::from(std::env::var_os("HOME").unwrap()).join(".cache/doxa-tests"));
        fs::create_dir_all(&cache).unwrap();
        let root=tempfile::tempdir_in(cache.canonicalize().unwrap()).unwrap(); let bin=root.path().join("bin"); fs::create_dir(&bin).unwrap();
        let script=r#"#!/usr/bin/python3
import json,os,sys
from pathlib import Path
provider=Path(sys.argv[0]).name
root=Path(os.environ['HOME']); state=root/(provider+'-signed-in')
args=sys.argv[1:]
with (root/'calls').open('a') as log: log.write(json.dumps([provider,args])+'\n')
print('FIXTURE_PRIVATE_CREDENTIAL',flush=True)
print('https://auth.openai.com/callback?access_token=FIXTURE_PRIVATE_CREDENTIAL',flush=True)
print('https://auth.openai.com/login?%63lient_secret=FIXTURE_PRIVATE_CREDENTIAL',flush=True)
status=(provider=='claude' and args==['auth','status']) or (provider=='codex' and args==['login','status'])
if status: sys.exit(0 if state.exists() else 1)
login=(provider=='claude' and args==['auth','login']) or (provider=='codex' and args in (['login'],['login','--device-auth']))
logout=(provider=='claude' and args==['auth','logout']) or (provider=='codex' and args==['logout'])
if login:
 if args==['login','--device-auth']:
  print('   \x1b[34mhttps://auth.openai.com/codex/device\x1b[0m',flush=True)
  print('2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m',flush=True)
  print('   \x1b[34mABCD-EFGH\x1b[0m',flush=True)
 else:
  print('https://claude.ai/login?redirect_uri=http%3A%2F%2Flocalhost&scope=user%3Ainference',flush=True)
  print('Device code: ABCD-EFGH',flush=True)
 state.write_text('fixture state only')
elif logout: state.unlink(missing_ok=True)
else: sys.exit(23)
"#;
        for provider in ["claude","codex"] { let executable=bin.join(provider);fs::write(&executable,script).unwrap();fs::set_permissions(executable,fs::Permissions::from_mode(0o700)).unwrap(); }
        Self {root,bin}
    }
    fn command(&self,args:&[&str]) -> Command {
        let mut command=Command::new(env!("CARGO_BIN_EXE_doxa-rs"));
        command.args(args).env_clear().env("HOME",self.root.path()).env("PATH",&self.bin)
            .env("DOXA_HOME",self.root.path().join("doxa")).env("CLAUDE_CONFIG_DIR",self.root.path().join("claude")); command
    }
}

fn wait_bounded(mut child:std::process::Child) -> std::process::Output {
    let deadline=std::time::Instant::now()+std::time::Duration::from_secs(5);
    loop {
        if child.try_wait().unwrap().is_some() { return child.wait_with_output().unwrap(); }
        if std::time::Instant::now()>=deadline {
            child.kill().unwrap();child.wait().unwrap();panic!("fake setup wizard did not finish within five seconds");
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
}

#[test]
fn explicit_provider_login_logout_uses_cli_authority_and_filters_private_output() {
    let fixture=Fixture::new();
    for provider in ["claude","codex"] {
        let status=fixture.command(&["auth","status",provider]).output().unwrap();
        assert!(status.status.success());assert!(String::from_utf8_lossy(&status.stdout).contains("not authenticated"));
        let login=fixture.command(&["auth","login",provider]).output().unwrap();
        assert!(login.status.success(),"{}",String::from_utf8_lossy(&login.stderr));
        let output=String::from_utf8_lossy(&login.stdout);
        assert!(output.contains("completed; authenticated"));assert!(output.contains("redirect_uri=http%3A%2F%2Flocalhost"));assert!(output.contains("ABCD-EFGH"));
        assert!(!output.contains("FIXTURE_PRIVATE_CREDENTIAL"));assert!(!String::from_utf8_lossy(&login.stderr).contains("FIXTURE_PRIVATE_CREDENTIAL"));
        let logout=fixture.command(&["auth","logout",provider]).output().unwrap();
        assert!(logout.status.success());assert!(String::from_utf8_lossy(&logout.stdout).contains("not authenticated"));
    }
    let calls=fs::read_to_string(fixture.root.path().join("calls")).unwrap();
    for expected in [r#"["claude", ["auth", "login"]]"#,r#"["claude", ["auth", "logout"]]"#,r#"["codex", ["login"]]"#,r#"["codex", ["logout"]]"#] { assert!(calls.contains(expected)); }
    assert!(!fixture.root.path().join("doxa").exists(),"auth operations must not create DOXA credential or transcript files");
}

#[test]
fn terminal_setup_wizard_persists_private_store_and_new_session_defaults() {
    let fixture=Fixture::new();
    let (mut master,mut slave)=(-1,-1);
    assert_eq!(unsafe{libc::openpty(&mut master,&mut slave,std::ptr::null_mut(),std::ptr::null(),std::ptr::null())},0);
    for fd in [master,slave] { assert_eq!(unsafe{libc::fcntl(fd,libc::F_SETFD,libc::FD_CLOEXEC)},0); }
    let mut input=unsafe{fs::File::from_raw_fd(master)};
    let terminal=unsafe{fs::File::from_raw_fd(slave)};
    let child=fixture.command(&["setup"]).stdin(Stdio::from(terminal)).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
    input.write_all(b"d\nfixture-model\nhigh\n").unwrap();
    let output=wait_bounded(child);
    assert!(output.status.success(),"{}",String::from_utf8_lossy(&output.stderr));
    let stdout=String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("setup complete"));assert!(!stdout.contains("FIXTURE_PRIVATE_CREDENTIAL"));
    let home=fixture.root.path().join("doxa"); let store=home.join("lore");
    let config:toml::Value=fs::read_to_string(home.join("config.toml")).unwrap().parse().unwrap();
    assert_eq!(config["lore_root"].as_str(),store.to_str());assert_eq!(config["model"].as_str(),Some("fixture-model"));assert_eq!(config["effort"].as_str(),Some("high"));
    for path in [&home,&store] { assert_eq!(fs::metadata(path).unwrap().mode()&0o077,0); }
    let calls=fs::read_to_string(fixture.root.path().join("calls")).unwrap();
    assert!(!calls.contains(r#"["auth", "login"]"#));assert!(!fixture.root.path().join("claude").exists());
}

#[test]
fn terminal_setup_refuses_symlinked_store_ancestor_before_mutating_target() {
    let fixture=Fixture::new();let outside=fixture.root.path().join("outside");fs::create_dir(&outside).unwrap();
    std::os::unix::fs::symlink(&outside,fixture.root.path().join("doxa")).unwrap();
    let (mut master,mut slave)=(-1,-1);
    assert_eq!(unsafe{libc::openpty(&mut master,&mut slave,std::ptr::null_mut(),std::ptr::null(),std::ptr::null())},0);
    for fd in [master,slave] { assert_eq!(unsafe{libc::fcntl(fd,libc::F_SETFD,libc::FD_CLOEXEC)},0); }
    let mut input=unsafe{fs::File::from_raw_fd(master)};let terminal=unsafe{fs::File::from_raw_fd(slave)};
    let child=fixture.command(&["setup"]).stdin(Stdio::from(terminal)).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
    input.write_all(b"d\n\n\n").unwrap();let output=wait_bounded(child);
    assert!(!output.status.success());assert_eq!(fs::read_dir(&outside).unwrap().count(),0);
}

#[test]
fn explicit_device_login_forwards_only_verified_codex_flag() {
    let fixture=Fixture::new();
    let login=fixture.command(&["auth","login","codex","--device-auth"]).output().unwrap();
    assert!(login.status.success(),"{}",String::from_utf8_lossy(&login.stderr));
    let stdout=String::from_utf8_lossy(&login.stdout);
    assert!(stdout.contains("Device code: ABCD-EFGH"));assert!(stdout.contains("https://auth.openai.com/codex/device"));
    assert!(!stdout.contains('\u{1b}'));assert!(!stdout.contains("FIXTURE_PRIVATE_CREDENTIAL"));
    let calls=fs::read_to_string(fixture.root.path().join("calls")).unwrap();
    assert!(calls.contains(r#"["codex", ["login", "--device-auth"]]"#));
    for args in [vec!["auth","login","claude","--device-auth"],vec!["auth","logout","codex","--device-auth"],vec!["auth","login","codex","--with-api-key"]] {
        assert!(!fixture.command(&args).output().unwrap().status.success());
    }
    assert_eq!(fs::read_to_string(fixture.root.path().join("calls")).unwrap(),calls,"unsupported options must never invoke a provider CLI");
}
