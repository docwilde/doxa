//! ROOT-only gateway lifetime and authenticated status. Saved JSON can never
//! substitute for the live process, exact Engine, current quota or sender.
use crate::{error, egress::{AllowedHosts, EgressGateway}, hardened_authority::Authority};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{fs::{self, File, OpenOptions}, io::{self, Read, Write},
    os::{fd::{AsRawFd, FromRawFd}, unix::{fs::{FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt}, net::{UnixListener, UnixStream}}},
    path::{Component, Path, PathBuf}, sync::Arc, time::{Duration, Instant}};

pub const CONTRACT: &str = "doxa-root-session-authority-v1";
const MAX_STATUS: usize = 16 * 1024;
const STATUS_TIME: Duration = Duration::from_secs(3);
const SO_PEERPIDFD: i32 = 77;

pub(crate) fn controlled(path: &Path) -> io::Result<()> {
    if !path.is_absolute() || path.components().skip(1).any(|part| !matches!(part, Component::Normal(_))) {
        return Err(error("ROOT gateway path is not absolute and canonical"));
    }
    let mut current = PathBuf::from("/");
    for part in path.components().skip(1) {
        current.push(part);
        let meta = fs::symlink_metadata(&current)?;
        if meta.file_type().is_symlink() || meta.uid() != 0 || meta.mode() & 0o022 != 0 {
            return Err(error("ROOT gateway path is not under administrator control"));
        }
    }
    Ok(())
}

pub(crate) fn peer(stream: &UnixStream, uid: u32) -> io::Result<(libc::ucred, File)> {
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut size = std::mem::size_of_val(&cred) as libc::socklen_t;
    if unsafe { libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED,
        (&mut cred as *mut libc::ucred).cast(), &mut size) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if size as usize != std::mem::size_of_val(&cred) || cred.uid != uid || cred.pid <= 0 {
        return Err(error("ROOT gateway peer credentials differ from policy"));
    }
    let mut raw = -1i32; size = std::mem::size_of_val(&raw) as libc::socklen_t;
    if unsafe { libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, SO_PEERPIDFD,
        (&mut raw as *mut i32).cast(), &mut size) } != 0 { return Err(io::Error::last_os_error()); }
    if raw < 0 || size as usize != std::mem::size_of_val(&raw) {
        if raw >= 0 { unsafe { libc::close(raw); } }
        return Err(error("ROOT gateway peer pidfd is unavailable"));
    }
    let pin = unsafe { File::from_raw_fd(raw) };
    alive(&pin)?;
    let raw = fs::read_to_string(format!("/proc/self/fdinfo/{}", pin.as_raw_fd()))?;
    let mut pids = raw.lines().filter_map(|line| line.strip_prefix("Pid:").map(str::trim));
    if pids.next().and_then(|value| value.parse::<i32>().ok()) != Some(cred.pid) || pids.next().is_some() {
        return Err(error("ROOT gateway peer PID and pidfd disagree"));
    }
    Ok((cred, pin))
}

pub(crate) fn alive(pin: &File) -> io::Result<()> {
    let mut poll = libc::pollfd {fd:pin.as_raw_fd(), events:libc::POLLIN, revents:0};
    if unsafe { libc::poll(&mut poll, 1, 0) } == 0 { Ok(()) }
    else { Err(error("pinned gateway or callback process exited")) }
}

pub(crate) fn executable_hash(pid: i32) -> io::Result<String> {
    let mut file = File::open(format!("/proc/{pid}/exe"))?;
    let before = file.metadata()?;
    if !before.is_file() || before.len() > 256 * 1024 * 1024 || before.uid() != 0 || before.mode() & 0o022 != 0 {
        return Err(error("gateway executable is not an immutable ROOT artifact"));
    }
    let deadline = Instant::now() + STATUS_TIME;
    let mut hash = Sha256::new(); let mut chunk = [0; 65536];
    loop {
        if Instant::now() >= deadline { return Err(error("gateway executable hash deadline")); }
        let n = file.read(&mut chunk)?; if n == 0 { break; } hash.update(&chunk[..n]);
    }
    let after = file.metadata()?;
    if (before.dev(),before.ino(),before.len(),before.ctime(),before.ctime_nsec())
        != (after.dev(),after.ino(),after.len(),after.ctime(),after.ctime_nsec()) {
        return Err(error("gateway executable changed during attestation"));
    }
    Ok(format!("{:x}", hash.finalize()))
}

pub(crate) fn read_exact_until(stream: &mut UnixStream, bytes: &mut [u8], deadline: Instant) -> io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_millis(200)))?;
    let mut at = 0;
    while at < bytes.len() {
        if Instant::now() >= deadline { return Err(error("ROOT gateway frame total deadline")); }
        match stream.read(&mut bytes[at..]) {
            Ok(0) => return Err(error("ROOT gateway frame truncated")),
            Ok(n) => at += n,
            Err(e) if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut | io::ErrorKind::Interrupted) => {},
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

pub(crate) fn read_frame(stream: &mut UnixStream, limit: usize, deadline: Instant) -> io::Result<Vec<u8>> {
    let mut length = [0;4]; read_exact_until(stream, &mut length, deadline)?;
    let length = u32::from_be_bytes(length) as usize;
    if length == 0 || length > limit { return Err(error("ROOT gateway frame exceeds bound")); }
    let mut bytes = vec![0;length]; read_exact_until(stream, &mut bytes, deadline)?; Ok(bytes)
}

pub(crate) fn write_frame(stream: &mut UnixStream, bytes: &[u8]) -> io::Result<()> {
    write_frame_until(stream,bytes,Instant::now()+STATUS_TIME)
}
fn write_frame_until(stream:&mut UnixStream,bytes:&[u8],deadline:Instant)->io::Result<()> {
    let length=u32::try_from(bytes.len()).map_err(|_|error("ROOT frame output exceeds protocol bound"))?.to_be_bytes();
    for part in [length.as_slice(),bytes] {
        let mut at=0;
        while at<part.len() {
            let remaining=deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero(){return Err(error("ROOT gateway frame write total deadline"));}
            stream.set_write_timeout(Some(remaining.min(Duration::from_millis(200))))?;
            match stream.write(&part[at..]) {
                Ok(0)=>return Err(error("ROOT gateway output closed")),Ok(n)=>at+=n,
                Err(e) if matches!(e.kind(),io::ErrorKind::WouldBlock|io::ErrorKind::TimedOut|io::ErrorKind::Interrupted)=>{},Err(e)=>return Err(e),
            }
        }
    }
    if Instant::now()>=deadline{return Err(error("ROOT gateway frame write total deadline"));}
    Ok(())
}
pub(crate) fn process_start(pid:i32)->io::Result<u64> {
    let stat=fs::read_to_string(format!("/proc/{pid}/stat"))?;
    // comm may contain spaces and closing parentheses; fields after its last
    // closing parenthesis begin at state(3), starttime is field22.
    stat.rsplit_once(')').and_then(|(_,tail)|tail.split_whitespace().nth(19))
        .and_then(|value|value.parse().ok()).filter(|value|*value>0)
        .ok_or_else(||error("gateway process start time is unavailable"))
}
pub(crate) fn instance_path(id:&str)->PathBuf {
    Path::new("/run/doxa/hardened").join(id).join("gateway-instance.json")
}
fn publish_instance(authority:&Authority,status:&fs::Metadata)->io::Result<(PathBuf,(u64,u64))> {
    let pid=unsafe{libc::getpid()};
    let value=json!({"contract":"doxa-root-gateway-instance-v1","session_id":authority.policy.session_id,
        "policy_sha256":authority.policy_sha256,"cid":authority.manifest.cid,"boot_id":authority.manifest.boot_id,
        "pid":pid,"start_time":process_start(pid)?,"exe_path":authority.policy.authority.path,
        "exe_sha256":executable_hash(pid)?,"socket_dev":status.dev(),"socket_ino":status.ino()});
    let path=instance_path(&authority.policy.session_id);
    controlled(path.parent().unwrap())?;
    let mut temporary=tempfile::NamedTempFile::new_in(path.parent().unwrap())?;
    temporary.as_file().set_permissions(fs::Permissions::from_mode(0o444))?;
    temporary.write_all(&serde_json::to_vec(&value)?)?;temporary.as_file().sync_all()?;
    temporary.persist(&path).map_err(|e|e.error)?;File::open(path.parent().unwrap())?.sync_all()?;
    let m=fs::symlink_metadata(&path)?;Ok((path,(m.dev(),m.ino())))
}
pub(crate) fn instance_hash(authority:&Authority)->io::Result<String> {
    let path=instance_path(&authority.policy.session_id);
    let mut file=crate::root_client::root_file(&path,true,false,MAX_STATUS as u64)?;
    let before=file.metadata()?;let mut bytes=Vec::new();Read::by_ref(&mut file).take(MAX_STATUS as u64+1).read_to_end(&mut bytes)?;
    let after=file.metadata()?;let current=fs::symlink_metadata(&path)?;
    if bytes.len()>MAX_STATUS||(before.dev(),before.ino(),before.len(),before.ctime(),before.ctime_nsec())
        !=(after.dev(),after.ino(),after.len(),after.ctime(),after.ctime_nsec())
        ||(current.dev(),current.ino())!=(before.dev(),before.ino()) {return Err(error("ROOT gateway instance changed"));}
    Ok(format!("{:x}",Sha256::digest(&bytes)))
}

fn status_path(authority: &Authority) -> PathBuf {
    Path::new("/run/doxa/hardened").join(&authority.policy.session_id).join("gateway-status.sock")
}

fn nonce() -> io::Result<String> {
    let mut bytes = [0;16]; File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// A status packet is useful only on a live pidfd-pinned socket from the
/// exact ROOT-installed native binary, scoped by a fresh challenge.
fn gateway_status(authority: &Authority) -> io::Result<Value> {
    let path = status_path(authority); controlled(path.parent().unwrap())?;
    let meta = fs::symlink_metadata(&path)?;
    if !meta.file_type().is_socket() || meta.uid() != 0 || meta.mode() & 0o777 != 0o600 {
        return Err(error("ROOT gateway status socket authority differs"));
    }
    let mut stream = UnixStream::connect(&path)?;
    let (cred, pin) = peer(&stream, 0)?;
    if executable_hash(cred.pid)? != authority.policy.authority.sha256
        || authority.manifest.gateway_pid.is_some_and(|pid| pid != cred.pid as u32) {
        return Err(error("live gateway executable or pidfd differs from policy"));
    }
    let challenge = nonce()?;
    write_frame(&mut stream, &serde_json::to_vec(&json!({"op":"status","session_id":authority.policy.session_id,
        "policy_sha256":authority.policy_sha256,"nonce":challenge}))?)?;
    let reply: Value = serde_json::from_slice(&read_frame(&mut stream, MAX_STATUS, Instant::now()+STATUS_TIME)?)?;
    alive(&pin)?;
    let current = fs::symlink_metadata(&path)?;
    if (current.dev(),current.ino()) != (meta.dev(),meta.ino()) || reply["nonce"] != challenge
        || reply["session_id"] != authority.policy.session_id || reply["policy_sha256"] != authority.policy_sha256
        || reply["cid"] != authority.manifest.cid || reply["engine_pid"] != authority.manifest.engine_pid
        || reply["boot_id"] != authority.manifest.boot_id || reply["gateway_ready"] != true {
        return Err(error("ROOT gateway live status is stale or mismatched"));
    }
    authority.revalidate()?;
    Ok(reply)
}

pub fn broker_check(policy: &Path, manifest: &Path, engine_pidfd: i32, phase: &str, family: &str) -> io::Result<Value> {
    if !matches!(phase, "prepare" | "resume" | "provider_exec" | "workspace_exec")
        || !matches!(family, "none" | "claude" | "codex") {
        return Err(error("unsupported ROOT broker phase or provider family"));
    }
    let authority = Authority::load(policy, manifest, engine_pidfd)?;
    let status = gateway_status(&authority)?;
    let hook_ready = if family == "codex" { status["hook_ready"] == true } else { true };
    if family == "codex" && !hook_ready { return Err(error("Codex requires a live ROOT-attested host compaction callback")); }
    Ok(json!({"contract":CONTRACT,"ready":true,"policy_sha256":authority.policy_sha256,"cid":authority.manifest.cid,
        "engine_pid":authority.manifest.engine_pid,"boot_id":authority.manifest.boot_id,"phase":phase,"family":family,
        "quota_ready":true,"filter_ready":true,"gateway_ready":true,"hook_ready":hook_ready}))
}

pub(crate) fn origin_exact(authority: &Authority, stream: &UnixStream,
    sender: &crate::broker_sender::ConnectorSenderPin, output: &mut [u8], deadline: Instant) -> io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_millis(200)))?;
    let mut at=0;
    while at<output.len() {
        if Instant::now()>=deadline {return Err(error("ROOT origin frame total deadline"));}
        match crate::broker_sender::read_pinned_sender(stream,&mut output[at..],sender) {
            Ok(0)=>return Err(error("ROOT origin frame truncated")),
            Ok(n)=>{authority.require_connector(stream)?;at+=n;},
            Err(e) if matches!(e.kind(),io::ErrorKind::WouldBlock|io::ErrorKind::TimedOut|io::ErrorKind::Interrupted)=>{},
            Err(e)=>return Err(e),
        }
    }
    Ok(())
}

/// A disposable-host diagnostic for actual per-send origin. It returns kernel
/// observations only; the production broker cannot consume it as admission.
pub fn kernel_listener(policy:&Path,manifest:&Path,engine_pidfd:i32,frames:u32,send_fd:Option<i32>)->io::Result<Value> {
    if !(1..=2).contains(&frames) {return Err(error("origin diagnostic frame bound"));}
    if send_fd.is_some() && frames!=2{return Err(error("descriptor diagnostic requires two frames"));}
    let authority=Authority::load(policy,manifest,engine_pidfd)?;
    if executable_hash(unsafe{libc::getpid()})?!=authority.policy.authority.sha256 {
        return Err(error("origin diagnostic executable differs from root policy"));
    }
    let path=authority.policy.root.join("broker/egress-claude.sock");
    if path.exists(){return Err(error("origin diagnostic requires a fresh fixed socket"));}
    let listener=UnixListener::bind(&path)?;
    let raw=std::ffi::CString::new(std::os::unix::ffi::OsStrExt::as_bytes(path.as_os_str()))
        .map_err(|_|error("invalid origin diagnostic socket"))?;
    if unsafe{libc::chown(raw.as_ptr(),0,authority.policy.engine.gid)}!=0{return Err(io::Error::last_os_error());}
    fs::set_permissions(&path,fs::Permissions::from_mode(0o660))?;
    crate::broker_sender::prepare_listener(&listener)?;
    listener.set_nonblocking(true)?;
    struct DiagnosticSocket(PathBuf,(u64,u64));
    impl Drop for DiagnosticSocket {fn drop(&mut self){if fs::symlink_metadata(&self.0).is_ok_and(|m|(m.dev(),m.ino())==self.1){let _=fs::remove_file(&self.0);}}}
    let meta=fs::symlink_metadata(&path)?;let _owned=DiagnosticSocket(path,(meta.dev(),meta.ino()));
    let deadline=Instant::now()+Duration::from_secs(authority.policy.limits.operation_seconds);
    let(mut stream,_)=loop {
        if Instant::now()>=deadline{return Err(error("origin diagnostic accept deadline"));}
        match listener.accept(){Ok(value)=>break value,
            Err(e) if e.kind()==io::ErrorKind::WouldBlock=>std::thread::sleep(Duration::from_millis(20)),
            Err(e)=>return Err(e),}
    };
    authority.require_connector(&stream)?;
    let sender=crate::broker_sender::pin_connector_sender_for_uid(&stream,authority.policy.engine.uid)?;
    for index in 0..frames {
        let mut length=[0;4];origin_exact(&authority,&stream,&sender,&mut length,deadline)?;
        let length=u32::from_be_bytes(length) as usize;
        if length==0||length>65536{return Err(error("origin diagnostic frame bound"));}
        let mut bytes=vec![0;length];origin_exact(&authority,&stream,&sender,&mut bytes,deadline)?;
        authority.require_connector(&stream)?;
        write_frame(&mut stream,&bytes)?;
        if index==0 {
            if let Some(fd)=send_fd {
                // ROOT-only disposable-host negative probe. Production serve
                // never sends descriptor capabilities into a worker.
                let mut byte=[b'F'];let mut iov=libc::iovec{iov_base:byte.as_mut_ptr().cast(),iov_len:1};
                let mut control=[0u64;3];let mut msg:libc::msghdr=unsafe{std::mem::zeroed()};
                msg.msg_iov=&mut iov;msg.msg_iovlen=1;msg.msg_control=control.as_mut_ptr().cast();
                msg.msg_controllen=unsafe{libc::CMSG_SPACE(std::mem::size_of::<i32>() as u32)} as usize;
                unsafe{
                    let cmsg=libc::CMSG_FIRSTHDR(&msg);(*cmsg).cmsg_level=libc::SOL_SOCKET;
                    (*cmsg).cmsg_type=libc::SCM_RIGHTS;(*cmsg).cmsg_len=libc::CMSG_LEN(std::mem::size_of::<i32>() as u32) as usize;
                    std::ptr::write_unaligned(libc::CMSG_DATA(cmsg).cast::<i32>(),fd);
                    if libc::sendmsg(stream.as_raw_fd(),&msg,libc::MSG_NOSIGNAL)!=1{return Err(io::Error::last_os_error());}
                }
            }
        }
    }
    authority.revalidate()?;
    let mut report=authority.report();report["contract"]=json!("doxa-root-origin-diagnostic-v1");
    report["frames_verified"]=json!(frames);Ok(report)
}

struct StatusSocket {path:PathBuf, identity:(u64,u64), instance:Option<(PathBuf,(u64,u64))>, _lock:File}
impl Drop for StatusSocket {
    fn drop(&mut self) {
        if fs::symlink_metadata(&self.path).is_ok_and(|m| (m.dev(),m.ino()) == self.identity) { let _ = fs::remove_file(&self.path); }
        if let Some((path,identity))=&self.instance {
            if fs::symlink_metadata(path).is_ok_and(|m|(m.dev(),m.ino())==*identity){let _=fs::remove_file(path);}
        }
    }
}

pub fn serve(policy: &Path, manifest: &Path, engine_pidfd: i32) -> io::Result<()> {
    let authority = Arc::new(Authority::load(policy, manifest, engine_pidfd)?);
    if executable_hash(unsafe {libc::getpid()})? != authority.policy.authority.sha256 {
        return Err(error("running native gateway differs from administrator content pin"));
    }
    let path = status_path(&authority); controlled(path.parent().unwrap())?;
    let lock = OpenOptions::new().read(true).write(true).create(true).mode(0o600)
        .custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK|libc::O_CLOEXEC).open(path.parent().unwrap().join("gateway.lock"))?;
    let meta = lock.metadata()?;
    if !meta.is_file() || meta.uid()!=0 || meta.nlink()!=1 || meta.mode()&0o077!=0
        || unsafe { libc::flock(lock.as_raw_fd(),libc::LOCK_EX|libc::LOCK_NB) } != 0 {
        return Err(error("ROOT gateway lock is unsafe or already held"));
    }
    // An existing path requires explicit ROOT cleanup. Never probe and remove
    // a potentially live service behind a full Unix listener backlog.
    if path.exists() { return Err(error("ROOT gateway status path requires verified cleanup")); }
    let listener = UnixListener::bind(&path)?;
    fs::set_permissions(&path,fs::Permissions::from_mode(0o600))?;
    listener.set_nonblocking(true)?;
    let meta=fs::symlink_metadata(&path)?;
    let mut _status=StatusSocket{path,identity:(meta.dev(),meta.ino()),instance:None,_lock:lock};
    let mut gateways = Vec::new();
    for (family, provider) in &authority.policy.providers {
        let pin=authority.clone();
        gateways.push(EgressGateway::start_root_controlled(&authority.policy.root.join("broker"),family,
            authority.policy.engine.uid,authority.policy.engine.gid,AllowedHosts::new(&provider.egress)?,
            Arc::new(move |stream| pin.require_connector(stream)))?);
    }
    let hooks=crate::root_hooks::RootHooks::start(authority.clone())?;
    authority.revalidate()?;
    _status.instance=Some(publish_instance(&authority,&meta)?);
    loop {
        authority.revalidate()?;
        let (mut stream,_) = match listener.accept() {
            Ok(value)=>value,
            Err(e) if e.kind()==io::ErrorKind::WouldBlock=>{std::thread::sleep(Duration::from_millis(200));continue;},
            Err(e)=>return Err(e),
        };
        let result=(||->io::Result<Value>{
            let (_,peer)=peer(&stream,0)?;
            let request:Value=serde_json::from_slice(&read_frame(&mut stream,MAX_STATUS,Instant::now()+STATUS_TIME)?)?;
            let challenge=request["nonce"].as_str().filter(|value|value.len()==32 && value.bytes().all(|b|b.is_ascii_hexdigit()))
                .ok_or_else(||error("ROOT gateway requires a fresh bounded challenge"))?;
            let op=request["op"].as_str().ok_or_else(||error("ROOT gateway operation missing"))?;
            let keys=match op {"status"=>4,"register_hook"=>7,"unregister_hook"=>6,_=>return Err(error("unsupported ROOT gateway control operation"))};
            if request["session_id"]!=authority.policy.session_id
                || request["policy_sha256"]!=authority.policy_sha256 || request.as_object().is_none_or(|rows|rows.len()!=keys) {
                return Err(error("ROOT gateway status schema or scope differs"));
            }
            authority.revalidate()?; alive(&peer)?;
            let hook_ready=if op=="status"{hooks.ready()}else{hooks.control(&request)?};
            Ok(json!({"nonce":challenge,"session_id":authority.policy.session_id,"policy_sha256":authority.policy_sha256,
                "cid":authority.manifest.cid,"engine_pid":authority.manifest.engine_pid,"boot_id":authority.manifest.boot_id,
                "gateway_ready":true,"hook_ready":hook_ready}))
        })();
        if let Ok(reply)=result { let _=write_frame(&mut stream,&serde_json::to_vec(&reply)?); }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn status_reader_has_one_total_deadline_and_packet_bound() {
        let (mut receiver,mut sender)=UnixStream::pair().unwrap();
        let task=std::thread::spawn(move||{for byte in 32u32.to_be_bytes().into_iter().chain([b'x';32]) {
            if sender.write_all(&[byte]).is_err(){break;} std::thread::sleep(Duration::from_millis(40));
        }});
        let start=Instant::now();
        assert!(read_frame(&mut receiver,64,start+Duration::from_millis(150)).is_err());
        assert!(start.elapsed()<Duration::from_millis(500)); drop(receiver); task.join().unwrap();
        let (mut receiver,mut sender)=UnixStream::pair().unwrap(); sender.write_all(&65u32.to_be_bytes()).unwrap();
        assert!(read_frame(&mut receiver,64,Instant::now()+STATUS_TIME).is_err());
    }
    #[test]
    fn slow_callback_drain_cannot_extend_the_total_write_deadline() {
        let(mut sender,mut receiver)=UnixStream::pair().unwrap();
        let size=1024i32;
        assert_eq!(unsafe{libc::setsockopt(sender.as_raw_fd(),libc::SOL_SOCKET,libc::SO_SNDBUF,
            (&size as *const i32).cast(),std::mem::size_of_val(&size) as libc::socklen_t)},0);
        let drain=std::thread::spawn(move||{let mut byte=[0];for _ in 0..20 {
            if receiver.read(&mut byte).unwrap_or(0)==0{break;}std::thread::sleep(Duration::from_millis(10));
        }});
        let began=Instant::now();
        assert!(write_frame_until(&mut sender,&vec![b'x';1024*1024],began+Duration::from_millis(120)).is_err());
        assert!(began.elapsed()<Duration::from_millis(500));drop(sender);drain.join().unwrap();
    }
}
