//! Host review callback authenticated by the actual ROOT gateway process.
//! ROOT publishes its own immutable instance; every received segment carries
//! the kernel's same live sender pidfd. Worker paths remain official paths so
//! the review bridge obtains/rechecks the original over the ROOT transport.
use crate::{broker_sender::{self,ConnectorSenderPin}, error, root_gateway::{self,alive,peer,write_frame}, Manifest};
use serde_json::{json,Value};
use sha2::{Digest,Sha256};
use std::{fs::{self,File},io::{self,Read},os::unix::{fs::{MetadataExt,PermissionsExt},net::{UnixListener,UnixStream}},
    path::{Path,PathBuf},sync::{Arc,atomic::{AtomicBool,Ordering}},thread::JoinHandle,time::{Duration,Instant}};
const MAX_HEADER:usize=65536;
const MAX_SOURCE:usize=32*1024*1024;
const BLOCKED:&str="DOXA rejected an invalid ROOT compaction callback";

struct Instance {bytes:Vec<u8>,identity:(u64,u64,i64,i64),pid:i32,start:u64,pin:File}
fn instance(stream:&UnixStream,manifest:&Manifest)->io::Result<Instance>{
    let installed=crate::root_client::installed(&manifest.session_id)?;
    if installed.policy_sha256!=manifest.policy_hash {return Err(error("ROOT callback policy changed"));}
    let(cred,pin)=peer(stream,0)?;
    let path=root_gateway::instance_path(&manifest.session_id);
    let mut file=crate::root_client::root_file(&path,true,false,MAX_HEADER as u64)?;
    let before=file.metadata()?;let mut bytes=Vec::new();file.by_ref().take(MAX_HEADER as u64+1).read_to_end(&mut bytes)?;
    let after=file.metadata()?;let current=fs::symlink_metadata(&path)?;
    let id=|m:&fs::Metadata|(m.dev(),m.ino(),m.ctime(),m.ctime_nsec());
    if bytes.len()>MAX_HEADER||id(&before)!=id(&after)||id(&after)!=id(&current){return Err(error("ROOT instance changed while pinned"));}
    let v:Value=serde_json::from_slice(&bytes)?;
    let start=root_gateway::process_start(cred.pid)?;
    let artifact=crate::root_client::root_file(&installed.policy.authority.path,false,true,256*1024*1024)?;
    let mut hash=Sha256::new();let mut artifact=artifact.take(256*1024*1024+1);let mut chunk=[0;65536];
    while let n @ 1..=65536=artifact.read(&mut chunk)? {hash.update(&chunk[..n]);}
    let socket=fs::symlink_metadata(path.parent().unwrap().join("gateway-status.sock"))?;
    if v.as_object().is_none_or(|m|m.len()!=11)||v["contract"]!="doxa-root-gateway-instance-v1"
        ||v["session_id"]!=manifest.session_id||v["policy_sha256"]!=manifest.policy_hash
        ||v["cid"].as_str()!=manifest.container_id.as_deref()||v["pid"]!=cred.pid||v["start_time"]!=start
        ||v["boot_id"]!=fs::read_to_string("/proc/sys/kernel/random/boot_id")?.trim()
        ||v["exe_path"].as_str()!=installed.policy.authority.path.to_str()
        ||v["exe_sha256"]!=installed.policy.authority.sha256
        ||format!("{:x}",hash.finalize())!=installed.policy.authority.sha256
        ||v["socket_dev"]!=socket.dev()||v["socket_ino"]!=socket.ino()
        ||socket.uid()!=0||socket.mode()&0o777!=0o600 {
        return Err(error("ROOT gateway instance differs from its installed scope or kernel peer"));
    }
    alive(&pin)?;Ok(Instance{bytes,identity:id(&before),pid:cred.pid,start,pin})
}
fn current(instance:&Instance,manifest:&Manifest)->io::Result<()> {
    alive(&instance.pin)?;
    let meta=fs::symlink_metadata(root_gateway::instance_path(&manifest.session_id))?;
    if (meta.dev(),meta.ino(),meta.ctime(),meta.ctime_nsec())!=instance.identity
        ||root_gateway::process_start(instance.pid)?!=instance.start {return Err(error("ROOT callback instance was replaced"));}
    Ok(())
}
fn exact(stream:&UnixStream,sender:&ConnectorSenderPin,bytes:&mut[u8],deadline:Instant,stop:&AtomicBool)->io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_millis(200)))?;
    let mut at=0;
    while at<bytes.len(){
        if stop.load(Ordering::Acquire)||Instant::now()>=deadline{return Err(error("ROOT callback read stopped or timed out"));}
        match broker_sender::read_pinned_sender(stream,&mut bytes[at..],sender){
            Ok(0)=>return Err(error("ROOT callback frame truncated")),Ok(n)=>at+=n,
            Err(e) if matches!(e.kind(),io::ErrorKind::WouldBlock|io::ErrorKind::TimedOut|io::ErrorKind::Interrupted)=>{},Err(e)=>return Err(e),
        }
    }Ok(())
}
fn frame(stream:&UnixStream,sender:&ConnectorSenderPin,limit:usize,deadline:Instant,stop:&AtomicBool)->io::Result<Vec<u8>> {
    let mut length=[0;4];exact(stream,sender,&mut length,deadline,stop)?;
    let length=u32::from_be_bytes(length) as usize;
    if length==0||length>limit{return Err(error("ROOT callback frame exceeds bound"));}
    let mut bytes=vec![0;length];exact(stream,sender,&mut bytes,deadline,stop)?;Ok(bytes)
}
fn checked_header(header:&Value,raw:&[u8],manifest:&Manifest,capability:&str)->io::Result<Value>{
    let event=&header["event"];
    let review=header["review_id"].as_str().filter(|s|s.len()==48&&s.bytes().all(|b|b.is_ascii_hexdigit()));
    let source=&header["source"];
    if header.as_object().is_none_or(|m|m.len()!=8)||header["version"]!=1
        ||header["session_id"]!=manifest.session_id||header["policy_sha256"]!=manifest.policy_hash
        ||header["cid"].as_str()!=manifest.container_id.as_deref()||header["capability"]!=capability||review.is_none()
        ||source.as_object().is_none_or(|m|m.len()!=8)||source["size"].as_u64()!=Some(raw.len() as u64)
        ||source["sha256"]!=format!("{:x}",Sha256::digest(raw))||raw.is_empty()||raw.len()>MAX_SOURCE
        ||raw.split(|b|*b==b'\n').any(|line|line.len()>1024*1024)
        ||event["hook_event_name"]!="PreCompact"||!matches!(event["trigger"].as_str(),Some("auto"|"manual")) {
        return Err(error("ROOT callback source, capability, or event scope differs"));
    }
    if ["device","inode"].iter().any(|key|source[*key].as_u64().is_none())
        ||["ctime","mtime"].iter().any(|key|source[*key].as_i64().is_none())
        ||["ctime_nsec","mtime_nsec"].iter().any(|key|source[*key].as_u64().is_none_or(|n|n>=1_000_000_000)) {
        return Err(error("ROOT callback filesystem identity is malformed"));
    }
    let thread=event["session_id"].as_str().filter(|s|!s.is_empty()&&s.len()<=128&&s.bytes().all(|b|b.is_ascii_alphanumeric()||b==b'-'))
        .ok_or_else(||error("ROOT callback provider thread is invalid"))?;
    let path=Path::new(event["transcript_path"].as_str().ok_or_else(||error("ROOT callback rollout missing"))?);
    crate::provider_relative(manifest,path)?;
    let first:Value=serde_json::from_slice(raw.split(|b|*b==b'\n').next().unwrap_or_default())?;
    if first["type"]!="session_meta"||first["payload"]["id"]!=thread{return Err(error("ROOT callback transcript belongs to another thread"));}
    Ok(event.clone())
}
fn process(mut stream:UnixStream,capability:&str,manifest:&Manifest,stop:&AtomicBool,handler:&impl Fn(Value,&Manifest)->Value)->io::Result<()> {
    let instance=instance(&stream,manifest)?;
    let sender=broker_sender::pin_connector_sender_for_uid(&stream,0)?;
    let nonce=crate::nonce()?[..32].to_owned();
    write_frame(&mut stream,&serde_json::to_vec(&json!({"op":"callback_challenge","nonce":nonce}))?)?;
    let deadline=Instant::now()+Duration::from_secs(5);
    let hello:Value=serde_json::from_slice(&frame(&stream,&sender,MAX_HEADER,deadline,stop)?)?;
    if hello.as_object().is_none_or(|m|m.len()!=8)||hello["version"]!=1||hello["nonce"]!=nonce
        ||hello["session_id"]!=manifest.session_id||hello["policy_sha256"]!=manifest.policy_hash
        ||hello["cid"].as_str()!=manifest.container_id.as_deref()||hello["pid"]!=instance.pid
        ||hello["boot_id"]!=fs::read_to_string("/proc/sys/kernel/random/boot_id")?.trim()
        ||hello["instance_sha256"]!=format!("{:x}",Sha256::digest(&instance.bytes)) {return Err(error("ROOT callback fresh challenge differs"));}
    let header:Value=serde_json::from_slice(&frame(&stream,&sender,MAX_HEADER,deadline,stop)?)?;
    let raw=frame(&stream,&sender,MAX_SOURCE,deadline,stop)?;
    let event=checked_header(&header,&raw,manifest,capability)?;
    current(&instance,manifest)?;
    let mut result=handler(event,manifest);
    current(&instance,manifest)?;
    if !result.is_object()||serde_json::to_vec(&result)?.len()>MAX_HEADER-256||stop.load(Ordering::Acquire){
        result=json!({"continue":false,"suppressOutput":true,"stopReason":BLOCKED});
    }
    write_frame(&mut stream,&serde_json::to_vec(&json!({"review_id":header["review_id"],"result":result}))?)
}

pub(crate) struct Callback {pub(crate)command:String,socket:PathBuf,identity:(u64,u64),_directory:tempfile::TempDir,
    stop:Arc<AtomicBool>,worker:Option<JoinHandle<()>>,manifest:Manifest,capability:String}
impl Callback {
    pub(crate) fn start(manifest:Manifest,handler:impl Fn(Value,&Manifest)->Value+Send+'static)->io::Result<Self>{
        let parent=PathBuf::from(format!("/run/user/{}/doxa-hooks",unsafe{libc::geteuid()}));
        crate::private_directory(&parent,true)?;
        let directory=tempfile::Builder::new().permissions(fs::Permissions::from_mode(0o700)).prefix("review-").tempdir_in(&parent)?;
        let socket=directory.path().join("callback.sock");
        let listener=UnixListener::bind(&socket)?;fs::set_permissions(&socket,fs::Permissions::from_mode(0o600))?;
        broker_sender::prepare_listener(&listener)?;listener.set_nonblocking(true)?;
        let m=fs::symlink_metadata(&socket)?;let identity=(m.dev(),m.ino());
        let capability=crate::nonce()?;
        crate::root_client::register_hook(&manifest,&socket,&capability,std::process::id(),true)?;
        let stop=Arc::new(AtomicBool::new(false));let stopping=stop.clone();
        let(manifest_worker,capability_worker)=(manifest.clone(),capability.clone());
        let worker=std::thread::spawn(move||while !stopping.load(Ordering::Acquire){
            let(stream,_)=match listener.accept(){Ok(v)=>v,Err(e) if e.kind()==io::ErrorKind::WouldBlock=>{std::thread::sleep(Duration::from_millis(20));continue;},Err(_)=>break,};
            let _=process(stream,&capability_worker,&manifest_worker,&stopping,&handler);
        });
        let command=format!("'/usr/local/bin/doxa-isolation-worker' 'hook' '/run/doxa/session/hook.sock' '{capability}'");
        Ok(Self{command,socket,identity,_directory:directory,stop,worker:Some(worker),manifest,capability})
    }
}
impl Drop for Callback {
    fn drop(&mut self){
        self.stop.store(true,Ordering::Release);
        let _=crate::root_client::register_hook(&self.manifest,&self.socket,&self.capability,std::process::id(),false);
        if let Some(worker)=self.worker.take(){let _=worker.join();}
        if fs::symlink_metadata(&self.socket).is_ok_and(|m|(m.dev(),m.ino())==self.identity){let _=fs::remove_file(&self.socket);}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn root_callback_rejects_wrong_thread_source_and_capability(){
        let manifest=Manifest{version:1,session_id:"scope".into(),profile:crate::Profile::DockerHardened,policy:None,
            policy_hash:"a".repeat(64),creation_policy_hash:"a".repeat(64),source:"/source".into(),checkout:"/private/checkout".into(),
            context_cwd:None,provider_rollout:None,checkout_device:0,checkout_inode:0,base_sha:String::new(),branch:String::new(),
            private_home:"/private/home".into(),cache:"/private/cache".into(),broker:"/private/broker".into(),container_id:Some("b".repeat(64)),nonce:String::new(),state:"ready".into()};
        let raw=br#"{"type":"session_meta","payload":{"id":"thread"}}"#;
        let cap="c".repeat(48);
        let h=json!({"version":1,"session_id":"scope","policy_sha256":manifest.policy_hash,"cid":manifest.container_id,
            "review_id":"d".repeat(48),"capability":cap,"event":{"hook_event_name":"PreCompact","trigger":"manual","session_id":"thread","transcript_path":"/home/doxa/codex/sessions/rollout.jsonl"},
            "source":{"device":1,"inode":2,"size":raw.len(),"ctime":3,"ctime_nsec":4,"mtime":5,"mtime_nsec":6,"sha256":format!("{:x}",Sha256::digest(raw))}});
        assert!(checked_header(&h,raw,&manifest,&cap).is_ok());
        for pointer in ["/capability","/session_id","/cid","/event/session_id","/event/transcript_path","/source/sha256"]{
            let mut bad=h.clone();*bad.pointer_mut(pointer).unwrap()=json!("forged");assert!(checked_header(&bad,raw,&manifest,&cap).is_err(),"{pointer}");
        }
        assert!(checked_header(&h,b"{}",&manifest,&cap).is_err());
    }
    #[test]
    fn callback_refuses_owner_peer_before_any_handler_or_root_file(){
        let(a,_b)=UnixStream::pair().unwrap();
        if unsafe{libc::geteuid()}!=0 {assert!(peer(&a,0).is_err());}
    }
}
