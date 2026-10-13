//! ROOT-attested worker compaction relay. Review runs in the caller's trusted
//! host process; the actual Engine-owned rollout is rechecked before approval.
use crate::{error, hardened_authority::Authority, root_gateway::{self, alive, peer, write_frame}};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{ffi::CString, fs::{self, File}, io::{self, Read}, os::{fd::{AsRawFd, FromRawFd},
    unix::{ffi::OsStrExt, fs::{FileTypeExt, MetadataExt, PermissionsExt}, net::{UnixListener, UnixStream}}},
    path::{Component, Path, PathBuf}, sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}},
    thread::JoinHandle, time::{Duration, Instant}};

const MAX_EVENT:usize=65536;
const MAX_SOURCE:usize=32*1024*1024;
const REVIEW_TIME:Duration=Duration::from_secs(205);

struct Registration { socket:PathBuf, pid:i32, pin:File, identity:(u64,u64), capability:String }
impl Registration {
    fn connect(&self, caller:u32)->io::Result<UnixStream> {
        alive(&self.pin)?;
        let meta=fs::symlink_metadata(&self.socket)?;
        if (meta.dev(),meta.ino())!=self.identity||meta.uid()!=caller||!meta.file_type().is_socket()||meta.mode()&0o777!=0o600 {
            return Err(error("host compaction callback identity changed"));
        }
        let stream=UnixStream::connect(&self.socket)?;
        let(cred,pin)=peer(&stream,caller)?;
        let a=pin.metadata()?;let b=self.pin.metadata()?;
        if cred.pid!=self.pid||(a.dev(),a.ino())!=(b.dev(),b.ino()) {return Err(error("host compaction callback process changed"));}
        alive(&self.pin)?;Ok(stream)
    }
}

fn callback_socket(path:&Path, caller:u32)->io::Result<()> {
    if !path.is_absolute()||path.as_os_str().as_bytes().len()>104
        ||path.components().skip(1).any(|part|!matches!(part,Component::Normal(_))) {
        return Err(error("invalid private host callback path"));
    }
    let parent=path.parent().ok_or_else(||error("host callback has no parent"))?;
    let mut current=PathBuf::from("/");
    for part in parent.components().skip(1) {
        current.push(part);let meta=fs::symlink_metadata(&current)?;
        if !meta.is_dir()||meta.file_type().is_symlink()||meta.mode()&0o002!=0 {
            return Err(error("host callback ancestry is not private and canonical"));
        }
    }
    let meta=fs::symlink_metadata(parent)?;
    if meta.uid()!=caller||meta.mode()&0o777!=0o700 {return Err(error("host callback directory must be caller-owned and private"));}
    let meta=fs::symlink_metadata(path)?;
    if !meta.file_type().is_socket()||meta.uid()!=caller||meta.mode()&0o777!=0o600||meta.nlink()!=1 {
        return Err(error("host callback socket must be caller-owned and private"));
    }
    Ok(())
}

fn owned_open(parent:&File, name:&std::ffi::OsStr, directory:bool)->io::Result<File> {
    let name=CString::new(name.as_bytes()).map_err(|_|error("invalid provider source component"))?;
    let flags=libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK
        |if directory{libc::O_DIRECTORY}else{0};
    let fd=unsafe{libc::openat(parent.as_raw_fd(),name.as_ptr(),flags)};
    if fd<0{return Err(io::Error::last_os_error());}Ok(unsafe{File::from_raw_fd(fd)})
}

fn source_file(authority:&Authority, relative:&Path)->io::Result<File> {
    let parts:Vec<_>=relative.components().collect();
    if parts.len()<2||parts.len()>24||!matches!(parts[0],Component::Normal(name) if name=="sessions"||name=="archived_sessions")
        ||parts.iter().any(|part|!matches!(part,Component::Normal(_))) {
        return Err(error("compaction source escapes fixed provider rollout scope"));
    }
    let home=File::open(authority.policy.root.join("home"))?;
    let mut parent=owned_open(&home,std::ffi::OsStr::new("codex"),true)?;
    for (index,part) in parts.iter().enumerate() {
        let Component::Normal(name)=part else{unreachable!()};
        parent=owned_open(&parent,name,index+1<parts.len())?;
    }
    let meta=parent.metadata()?;
    if !meta.is_file()||meta.uid()!=authority.policy.engine.uid||meta.nlink()!=1||meta.len()==0||meta.len()>MAX_SOURCE as u64 {
        return Err(error("actual provider rollout is not a bounded Engine-owned single-link source"));
    }
    Ok(parent)
}

fn identity(file:&File)->io::Result<(u64,u64,u64,i64,i64,i64,i64)> {
    let m=file.metadata()?;Ok((m.dev(),m.ino(),m.len(),m.ctime(),m.ctime_nsec(),m.mtime(),m.mtime_nsec()))
}
fn read_source(file:&mut File, deadline:Instant)->io::Result<(Vec<u8>,Value)> {
    let before=identity(file)?;
    let mut bytes=Vec::new();let mut chunk=[0;65536];
    loop {
        if Instant::now()>=deadline{return Err(error("provider source snapshot total deadline"));}
        let n=file.read(&mut chunk)?;if n==0{break;}
        if bytes.len()+n>MAX_SOURCE{return Err(error("provider rollout byte limit"));}bytes.extend_from_slice(&chunk[..n]);
    }
    if before!=identity(file)?||bytes.len() as u64!=before.2 {return Err(error("actual provider source changed while read"));}
    let proof=json!({"device":before.0,"inode":before.1,"size":before.2,"ctime":before.3,"ctime_nsec":before.4,
        "mtime":before.5,"mtime_nsec":before.6,"sha256":format!("{:x}",Sha256::digest(&bytes))});
    Ok((bytes,proof))
}

fn cancelled_read(stream:&mut UnixStream, bytes:&mut[u8], deadline:Instant, stop:&AtomicBool)->io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_millis(200)))?;
    let mut at=0;while at<bytes.len() {
        if stop.load(Ordering::Acquire)||Instant::now()>=deadline{return Err(error("host compaction callback lost or timed out"));}
        match stream.read(&mut bytes[at..]) {Ok(0)=>return Err(error("host compaction callback closed")),Ok(n)=>at+=n,
            Err(e) if matches!(e.kind(),io::ErrorKind::WouldBlock|io::ErrorKind::TimedOut|io::ErrorKind::Interrupted)=>{},Err(e)=>return Err(e),}
    }Ok(())
}

fn relay(mut stream:UnixStream, authority:&Authority, registration:&Mutex<Option<Registration>>, stop:&AtomicBool)->io::Result<()> {
    authority.require_connector(&stream)?;
    let sender=crate::broker_sender::pin_connector_sender_for_uid(&stream,authority.policy.engine.uid)?;
    let input_deadline=Instant::now()+Duration::from_secs(5);
    let mut length=[0;4];root_gateway::origin_exact(authority,&stream,&sender,&mut length,input_deadline)?;
    let length=u32::from_be_bytes(length) as usize;
    if length==0||length>MAX_EVENT{return Err(error("compaction event frame limit"));}
    let mut bytes=vec![0;length];root_gateway::origin_exact(authority,&stream,&sender,&mut bytes,input_deadline)?;
    let frame:Value=serde_json::from_slice(&bytes)?;
    let(reg_socket,capability)={
        let registered=registration.lock().map_err(|_|error("compaction registry unavailable"))?;
        let registered=registered.as_ref().ok_or_else(||error("host compaction callback is unavailable"))?;
        (registered.connect(authority.policy.caller_uid)?,registered.capability.clone())
    };
    if frame.as_object().is_none_or(|rows|rows.len()!=3)||frame["version"]!=1||frame["capability"]!=capability {
        return Err(error("compaction capability or event schema changed"));
    }
    let event=&frame["event"];
    let thread=event["session_id"].as_str().filter(|id|!id.is_empty()&&id.len()<=128&&id.bytes().all(|b|b.is_ascii_alphanumeric()||b==b'-'))
        .ok_or_else(||error("compaction provider thread identity is invalid"))?;
    if event["hook_event_name"]!="PreCompact"||!matches!(event["trigger"].as_str(),Some("auto"|"manual")) {
        return Err(error("unsupported compaction hook event"));
    }
    let relative=Path::new(event["transcript_path"].as_str().ok_or_else(||error("provider rollout path missing"))?)
        .strip_prefix("/home/doxa/codex").map_err(|_|error("compaction source is outside Codex home"))?;
    authority.require_connector(&stream)?;
    let mut original=source_file(authority,relative)?;
    let(raw,proof)=read_source(&mut original,Instant::now()+Duration::from_secs(5))?;
    if raw.split(|b|*b==b'\n').any(|line|line.len()>1024*1024){return Err(error("provider JSONL line exceeds limit"));}
    let first:Value=serde_json::from_slice(raw.split(|b|*b==b'\n').next().unwrap_or_default())?;
    if first["type"]!="session_meta"||first["payload"]["id"]!=thread{return Err(error("actual rollout belongs to another provider thread"));}
    let mut callback=reg_socket;
    // The caller chooses a fresh challenge. Its ROOT-controlled public
    // instance pin binds this kernel peer without cross-UID /proc/exe access.
    let challenge:Value=serde_json::from_slice(&root_gateway::read_frame(&mut callback,MAX_EVENT,Instant::now()+Duration::from_secs(5))?)?;
    let challenge_nonce=challenge["nonce"].as_str().filter(|s|s.len()==32&&s.bytes().all(|b|b.is_ascii_hexdigit()))
        .ok_or_else(||error("host callback challenge is invalid"))?;
    if challenge.as_object().is_none_or(|m|m.len()!=2)||challenge["op"]!="callback_challenge" {
        return Err(error("host callback challenge schema differs"));
    }
    write_frame(&mut callback,&serde_json::to_vec(&json!({"version":1,"nonce":challenge_nonce,
        "session_id":authority.policy.session_id,"policy_sha256":authority.policy_sha256,"cid":authority.manifest.cid,
        "pid":unsafe{libc::getpid()},"boot_id":authority.manifest.boot_id,"instance_sha256":root_gateway::instance_hash(authority)?}))?)?;
    let review_id=crate::nonce()?;
    let header=json!({"version":1,"session_id":authority.policy.session_id,"policy_sha256":authority.policy_sha256,
        "cid":authority.manifest.cid,"review_id":review_id,"capability":capability,"event":event,"source":proof});
    write_frame(&mut callback,&serde_json::to_vec(&header)?)?;
    write_frame(&mut callback,&raw)?;
    let deadline=Instant::now()+REVIEW_TIME;
    let mut length=[0;4];cancelled_read(&mut callback,&mut length,deadline,stop)?;
    let length=u32::from_be_bytes(length) as usize;
    if length==0||length>MAX_EVENT{return Err(error("host compaction response bound"));}
    let mut bytes=vec![0;length];cancelled_read(&mut callback,&mut bytes,deadline,stop)?;
    let result:Value=serde_json::from_slice(&bytes)?;
    if result["review_id"]!=review_id||!result["result"].is_object(){return Err(error("host compaction response is not bound to this review"));}
    let mut current=source_file(authority,relative)?;
    let(_,after)=read_source(&mut current,Instant::now()+Duration::from_secs(5))?;
    if after!=proof||identity(&original)?!=identity(&current)? {return Err(error("actual provider source changed after human review"));}
    authority.require_connector(&stream)?;
    {let registered=registration.lock().map_err(|_|error("compaction registry unavailable"))?;
        alive(&registered.as_ref().ok_or_else(||error("host compaction callback removed"))?.pin)?;}
    write_frame(&mut stream,&serde_json::to_vec(&result["result"])?)
}

pub(crate) struct RootHooks { path:PathBuf,identity:(u64,u64),stop:Arc<AtomicBool>,
    worker:Option<JoinHandle<()>>, registration:Arc<Mutex<Option<Registration>>>, authority:Arc<Authority> }
impl RootHooks {
    pub(crate) fn start(authority:Arc<Authority>)->io::Result<Self> {
        let path=authority.policy.root.join("broker/hook.sock");
        if path.exists(){return Err(error("ROOT hook socket requires verified prior cleanup"));}
        let listener=UnixListener::bind(&path)?;
        let raw=CString::new(path.as_os_str().as_bytes()).map_err(|_|error("invalid ROOT hook socket"))?;
        if unsafe{libc::chown(raw.as_ptr(),0,authority.policy.engine.gid)}!=0{return Err(io::Error::last_os_error());}
        fs::set_permissions(&path,fs::Permissions::from_mode(0o660))?;
        crate::broker_sender::prepare_listener(&listener)?;listener.set_nonblocking(true)?;
        let meta=fs::symlink_metadata(&path)?;
        let stop=Arc::new(AtomicBool::new(false));let registration=Arc::new(Mutex::new(None));
        let(stopping,registered,pin)=(stop.clone(),registration.clone(),authority.clone());
        let worker=std::thread::spawn(move||while !stopping.load(Ordering::Acquire){
            let(stream,_)=match listener.accept(){Ok(value)=>value,
                Err(e) if e.kind()==io::ErrorKind::WouldBlock=>{std::thread::sleep(Duration::from_millis(20));continue;},Err(_)=>break,};
            // One review at a time; the fixed provider wrapper has its own
            // deadline. Invalid origins receive no host or gateway bytes.
            let _=relay(stream,&pin,&registered,&stopping);
        });
        Ok(Self{path,identity:(meta.dev(),meta.ino()),stop,worker:Some(worker),registration,authority})
    }
    pub(crate) fn ready(&self)->bool {
        self.registration.lock().ok().is_some_and(|registered|registered.as_ref().is_some_and(|r|
            alive(&r.pin).is_ok()&&callback_socket(&r.socket,self.authority.policy.caller_uid).is_ok()
            &&fs::symlink_metadata(&r.socket).is_ok_and(|m|(m.dev(),m.ino())==r.identity)))
    }
    pub(crate) fn control(&self,request:&Value)->io::Result<bool> {
        let op=request["op"].as_str().ok_or_else(||error("hook registration operation missing"))?;
        let capability=request["capability"].as_str().filter(|c|c.len()==48&&c.bytes().all(|b|b.is_ascii_hexdigit()))
            .ok_or_else(||error("hook capability is malformed"))?;
        let pid=request["client_pid"].as_i64().and_then(|p|i32::try_from(p).ok()).filter(|p|*p>0)
            .ok_or_else(||error("callback process identity is invalid"))?;
        let mut registered=self.registration.lock().map_err(|_|error("hook registry unavailable"))?;
        match op {
            "register_hook"=>{
                let path=PathBuf::from(request["socket"].as_str().ok_or_else(||error("hook callback socket missing"))?);
                callback_socket(&path,self.authority.policy.caller_uid)?;
                let stream=UnixStream::connect(&path)?;let(cred,pin)=peer(&stream,self.authority.policy.caller_uid)?;
                if cred.pid!=pid{return Err(error("callback PID differs from actual socket peer"));}
                let meta=fs::symlink_metadata(&path)?;
                *registered=Some(Registration{socket:path,pid,pin,identity:(meta.dev(),meta.ino()),capability:capability.into()});Ok(true)
            },
            "unregister_hook"=>{
                if registered.as_ref().is_none_or(|r|r.pid!=pid||r.capability!=capability){return Err(error("hook unregistration scope differs"));}
                *registered=None;Ok(false)
            },
            _=>Err(error("unsupported hook registration operation")),
        }
    }
}
impl Drop for RootHooks {
    fn drop(&mut self){self.stop.store(true,Ordering::Release);if let Some(worker)=self.worker.take(){let _=worker.join();}
        if fs::symlink_metadata(&self.path).is_ok_and(|m|(m.dev(),m.ino())==self.identity){let _=fs::remove_file(&self.path);}}
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn copied_bytes_are_bound_to_actual_source_identity_and_detect_changes() {
        let dir=tempfile::tempdir().unwrap();let path=dir.path().join("rollout");fs::write(&path,b"first").unwrap();
        let mut source=File::open(&path).unwrap();let(bytes,proof)=read_source(&mut source,Instant::now()+Duration::from_secs(1)).unwrap();
        assert_eq!(bytes,b"first");assert_eq!(proof["size"],5);
        fs::write(&path,b"second").unwrap();let mut source=File::open(&path).unwrap();
        let(_,changed)=read_source(&mut source,Instant::now()+Duration::from_secs(1)).unwrap();assert_ne!(proof,changed);
    }
}
