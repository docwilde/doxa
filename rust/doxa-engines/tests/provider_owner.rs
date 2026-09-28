#![cfg(target_os = "linux")]
use doxa_engines::provider_owner::{self, CONTROL_ENV, READY};
use std::{ffi::CString, fs, io::{Read, Write}, os::unix::{io::{AsRawFd, FromRawFd}, net::UnixStream, process::CommandExt}, process::{Child, Command, Stdio}, time::{Duration, Instant}};

// Each owner runs in an isolated native subprocess, never in the test runner.
#[test]
#[ignore]
fn owner_process_entry() {
    let Some(fd) = std::env::var_os("DOXA_OWNER_TEST_FD") else { return; };
    let fd: i32 = fd.to_str().unwrap().parse().unwrap();
    let root = std::env::var("DOXA_OWNER_TEST_ROOT").unwrap();
    let script = CString::new(r#"
import os,sys,time,json
root=sys.argv[1]
children=[]
for detached in [False,True]:
 p=os.fork()
 if p==0:
  if detached:os.setsid()
  else:os.setpgid(0,0)
  time.sleep(60);os._exit(0)
 children.append(p)
# An intermediate parent exits before shutdown: its setsid tool is adopted.
p=os.fork()
if p==0:
 c=os.fork()
 if c==0:
  os.setsid();open(root+'/orphan','w').write(str(os.getpid()));time.sleep(60);os._exit(0)
 os._exit(0)
os.waitpid(p,0)
while not os.path.exists(root+'/orphan'):time.sleep(.01)
children.append(int(open(root+'/orphan').read()))
open(root+'/ready','w').write(json.dumps(children))
if os.path.exists(root+'/exit'):sys.exit(23)
time.sleep(60)
"#).unwrap();
    let executable = CString::new("/usr/bin/python3").unwrap();
    let flag = CString::new("-c").unwrap();
    let root = CString::new(root).unwrap();
    let args = [executable.as_ptr(), flag.as_ptr(), script.as_ptr(), root.as_ptr(), std::ptr::null()];
    // libtest runs tests on a worker thread. Fork an isolated single-thread
    // owner first, matching the production launcher (getpid == gettid).
    let owner = unsafe { libc::fork() };
    assert!(owner >= 0);
    if owner > 0 {
        let mut status=0;assert_eq!(unsafe {libc::waitpid(owner,&mut status,0)},owner);
        std::process::exit(if libc::WIFEXITED(status) {libc::WEXITSTATUS(status)} else {1});
    }
    let code = provider_owner::supervise(unsafe { UnixStream::from_raw_fd(fd) }, || {
        unsafe { libc::execv(executable.as_ptr(), args.as_ptr()); }
        std::io::Error::last_os_error()
    }).unwrap();
    std::process::exit(code);
}

fn spawn_owner(root: &std::path::Path) -> (Child, UnixStream) {
    let (parent, peer) = UnixStream::pair().unwrap();
    let fd = peer.as_raw_fd();
    let mut command = Command::new(std::env::current_exe().unwrap());
    command.args(["--exact", "owner_process_entry", "--ignored", "--nocapture"])
        .env("DOXA_OWNER_TEST_FD",fd.to_string()).env("DOXA_OWNER_TEST_ROOT",root)
        .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    unsafe { command.pre_exec(move || {
        if libc::fcntl(fd,libc::F_SETFD,0)<0 { return Err(std::io::Error::last_os_error()); }
        Ok(())
    }); }
    let child=command.spawn().unwrap(); drop(peer);
    parent.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    (child,parent)
}
fn wait_until(mut condition: impl FnMut()->bool) {
    let deadline=Instant::now()+Duration::from_secs(5);
    while !condition() { assert!(Instant::now()<deadline,"owned lifecycle deadline"); std::thread::sleep(Duration::from_millis(10)); }
}
fn ready(control: &mut UnixStream) {
    let mut received=vec![0;READY.len()];control.read_exact(&mut received).unwrap();assert_eq!(received,READY);
}
#[test]
fn control_eof_reaps_escaped_groups_sessions_and_already_orphaned_tools_only() {
    for provider_exits in [false,true] {
        let dir=tempfile::tempdir().unwrap();
        let mut unrelated=Command::new("/usr/bin/sleep").arg("60").spawn().unwrap();
        if provider_exits {fs::write(dir.path().join("exit"),b"").unwrap();}
        let (mut owner,mut control)=spawn_owner(dir.path());ready(&mut control);control.write_all(b"G").unwrap();
        wait_until(||dir.path().join("ready").exists());
        let pids:Vec<u32>=serde_json::from_slice(&fs::read(dir.path().join("ready")).unwrap()).unwrap();
        assert_eq!(pids.len(),3);
        drop(control);
        wait_until(||owner.try_wait().unwrap().is_some());
        for pid in pids {assert!(!std::path::Path::new(&format!("/proc/{pid}")).exists(),"owned child {pid} was not reaped");}
        assert!(unrelated.try_wait().unwrap().is_none(),"unrelated process was signaled");
        unrelated.kill().unwrap();unrelated.wait().unwrap();
    }
}
#[test]
fn readiness_cancellation_never_forks_a_provider() {
    let dir=tempfile::tempdir().unwrap();let (mut owner,mut control)=spawn_owner(dir.path());
    ready(&mut control);drop(control);wait_until(||owner.try_wait().unwrap().is_some());
    assert!(!dir.path().join("ready").exists());assert!(!dir.path().join("orphan").exists());
}
#[tokio::test]
async fn parent_handshake_refuses_invalid_or_closed_owner() {
    for invalid in [false,true] {
        let (mut parent,mut peer)=UnixStream::pair().unwrap();
        if invalid {peer.write_all(&vec![b'X';READY.len()]).unwrap();}
        drop(peer);assert!(provider_owner::acknowledge(&mut parent).await.is_err());
    }
    assert_eq!(CONTROL_ENV,"DOXA_CODEX_OWNER_FD");
}
