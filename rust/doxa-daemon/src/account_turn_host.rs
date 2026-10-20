//! Explicit provider-account attempt allowance. It is not an API dollar budget.
use doxa_runtime::{Host,PeerTurnOrigin};
use serde::{Deserialize,Serialize};
use serde_json::{json,Value};
use std::{fs,io::{self,Read,Write},os::{fd::{AsRawFd,FromRawFd},unix::{ffi::OsStrExt,fs::{DirBuilderExt,MetadataExt,OpenOptionsExt}}},path::{Path,PathBuf,Component},sync::{Arc,Mutex}};

#[derive(Clone,Debug,PartialEq,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {version:u8,identity:Value,limit:u64,used:u64,pending:bool,last_outcome:String}
/// The canonical directory is pinned for descriptor-relative journal I/O. Root
/// owned OS aliases (for example macOS /var) and root-owned sticky temporary
/// ancestors are allowed; owner aliases and shared writable ancestors refuse.
struct PrivateDirectory { original:PathBuf, canonical:PathBuf, file:fs::File, dev:u64, ino:u64 }
fn trusted_chain(path:&Path)->io::Result<()> {
    if !path.is_absolute(){return Err(denied("account journal parent must be absolute"));}
    let mut current=PathBuf::new();
    for component in path.components(){
        match component{Component::RootDir=>current.push(component),Component::Normal(part)=>current.push(part),_=>return Err(denied("account journal parent has noncanonical components"))}
        let meta=match fs::symlink_metadata(&current){Ok(meta)=>meta,Err(error)if error.kind()==io::ErrorKind::NotFound=>break,Err(error)=>return Err(error)};
        if meta.file_type().is_symlink(){
            let parent=fs::metadata(current.parent().ok_or_else(||denied("account journal alias parent unavailable"))?)?;
            if meta.uid()!=0||parent.uid()!=0||parent.mode()&0o022!=0{return Err(denied("account journal owner or shared alias is unsafe"));}
            // Check the resolved OS path too, so an allowed alias cannot hide
            // an owner-controlled or shared-writable directory.
            let resolved=fs::canonicalize(&current)?;trusted_canonical_chain(&resolved)?;
        }else{trusted_directory_meta(&meta)?;}
    }
    Ok(())
}
fn trusted_directory_meta(meta:&fs::Metadata)->io::Result<()> {
    let root_sticky=meta.uid()==0&&meta.mode()&0o1000!=0;
    if !meta.is_dir()||![0,unsafe{libc::geteuid()}].contains(&meta.uid())||meta.mode()&0o022!=0&&!root_sticky{return Err(denied("account journal ancestor is not trusted"));}Ok(())
}
fn trusted_canonical_chain(path:&Path)->io::Result<()> {
    for ancestor in path.ancestors(){let meta=fs::symlink_metadata(ancestor)?;if meta.file_type().is_symlink(){return Err(denied("account journal canonical ancestor changed"));}trusted_directory_meta(&meta)?;}Ok(())
}
impl PrivateDirectory {
    fn open(parent:&Path,create:bool)->io::Result<Self>{
        trusted_chain(parent)?;
        if create{fs::DirBuilder::new().recursive(true).mode(0o700).create(parent)?;}
        trusted_chain(parent)?;let canonical=fs::canonicalize(parent)?;trusted_canonical_chain(&canonical)?;
        let file=fs::OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(&canonical)?;
        let meta=file.metadata()?;
        if meta.uid()!=unsafe{libc::geteuid()}||meta.mode()&0o077!=0{return Err(denied("account journal parent must be private and owned"));}
        let result=Self{original:parent.into(),canonical,file,dev:meta.dev(),ino:meta.ino()};result.verify()?;Ok(result)
    }
    fn verify(&self)->io::Result<()> {
        trusted_chain(&self.original)?;
        if fs::canonicalize(&self.original)?!=self.canonical{return Err(denied("account journal parent changed"));}
        trusted_canonical_chain(&self.canonical)?;
        let meta=fs::symlink_metadata(&self.canonical)?;let held=self.file.metadata()?;
        if meta.dev()!=self.dev||meta.ino()!=self.ino||held.dev()!=self.dev||held.ino()!=self.ino||meta.uid()!=unsafe{libc::geteuid()}||meta.mode()&0o077!=0{return Err(denied("account journal parent identity changed"));}Ok(())
    }
    fn open_file(&self,name:&std::ffi::CStr,flags:i32)->io::Result<fs::File>{
        self.verify()?;
        let fd=unsafe{libc::openat(self.file.as_raw_fd(),name.as_ptr(),flags|libc::O_NOFOLLOW|libc::O_NONBLOCK|libc::O_CLOEXEC,0o600)};
        if fd<0{return Err(io::Error::last_os_error());}let file=unsafe{fs::File::from_raw_fd(fd)};self.verify()?;Ok(file)
    }
}
struct Journal {path:PathBuf,policy:PathBuf,directory:PrivateDirectory,_lock:fs::File}
pub struct AccountTurnHost {inner:Arc<dyn Host>,journal:Journal,state:Mutex<Record>}
fn denied(message:&str)->io::Error{io::Error::other(message)}
fn basename(path:&Path)->io::Result<std::ffi::CString>{std::ffi::CString::new(path.file_name().ok_or_else(||denied("account journal name missing"))?.as_bytes()).map_err(|_|denied("invalid account journal name"))}
impl Journal {
    fn read(&self)->io::Result<Record>{self.read_named(&self.path)}
    fn read_named(&self,path:&Path)->io::Result<Record>{
        let mut file=self.directory.open_file(&basename(path)?,libc::O_RDONLY)?;let record=Self::decode(&mut file)?;self.directory.verify()?;Ok(record)
    }
    fn read_path(path:&Path)->io::Result<Record>{
        let directory=PrivateDirectory::open(path.parent().ok_or_else(||denied("account journal parent unavailable"))?,false)?;
        let mut file=directory.open_file(&basename(path)?,libc::O_RDONLY)?;let record=Self::decode(&mut file)?;directory.verify()?;Ok(record)
    }
    fn decode(file:&mut fs::File)->io::Result<Record>{
        let meta=file.metadata()?;
        if !meta.is_file()||meta.nlink()!=1||meta.uid()!=unsafe{libc::geteuid()}||meta.mode()&0o077!=0||meta.len()>8192{return Err(denied("unsafe account turn journal"));}
        let mut bytes=Vec::new();Read::by_ref(file).take(8193).read_to_end(&mut bytes)?;
        if bytes.len()>8192{return Err(denied("account turn journal exceeds bound"));}
        serde_json::from_slice(&bytes).map_err(|_|denied("invalid account turn journal"))
    }
    fn write(&self,value:&Record,new:bool)->io::Result<()>{self.write_named(&self.path,value,new)}
    fn write_named(&self,path:&Path,value:&Record,new:bool)->io::Result<()>{
        self.directory.verify()?;let name=basename(path)?;
        let bytes=serde_json::to_vec(value)?;if bytes.len()>8192{return Err(denied("account journal exceeds bound"));}
        if new {
            let mut file=self.directory.open_file(&name,libc::O_WRONLY|libc::O_CREAT|libc::O_EXCL)?;
            file.write_all(&bytes)?;file.sync_all()?;
        }else{
            static NEXT:std::sync::atomic::AtomicU64=std::sync::atomic::AtomicU64::new(0);
            let temporary=std::ffi::CString::new(format!(".account-{}-{}",std::process::id(),NEXT.fetch_add(1,std::sync::atomic::Ordering::Relaxed))).unwrap();
            let result=(||->io::Result<()>{
                let mut file=self.directory.open_file(&temporary,libc::O_WRONLY|libc::O_CREAT|libc::O_EXCL)?;file.write_all(&bytes)?;file.sync_all()?;
                self.directory.verify()?;
                if unsafe{libc::renameat(self.directory.file.as_raw_fd(),temporary.as_ptr(),self.directory.file.as_raw_fd(),name.as_ptr())}!=0{return Err(io::Error::last_os_error());}Ok(())
            })();
            if result.is_err(){unsafe{libc::unlinkat(self.directory.file.as_raw_fd(),temporary.as_ptr(),0)};}result?;
        }
        self.directory.file.sync_all()?;self.directory.verify()
    }
}
/// Startup must enforce persisted account policy even when a caller clears
/// its environment. A frontend hint is never authority to remove an allowance.
pub fn preflight(path:&std::path::Path,requested:Option<u64>,resume:bool,session:&str,model:Option<&str>)->io::Result<()>{
    let policy=path.with_extension("policy.json");
    if requested.is_some(){trusted_chain(path.parent().ok_or_else(||denied("account journal parent unavailable"))?)?;}
    let present=|path:&std::path::Path|match fs::symlink_metadata(path){Ok(_)=>Ok(true),Err(error)if error.kind()==io::ErrorKind::NotFound=>Ok(false),Err(error)=>Err(error)};
    let existing=present(path)?||present(&policy)?;
    if !existing&&requested.is_none(){return Ok(());}
    let limit=requested.ok_or_else(||denied("session has durable account turn policy; its original allowance is required"))?;
    if !resume {if existing{return Err(denied("account turn policy already exists; explicit resume required"));}return Ok(());}
    let row=Journal::read_path(path).map_err(|_|denied("saved account turn record is missing or unsafe"))?;let original=Journal::read_path(&policy).map_err(|_|denied("saved account turn policy is missing or unsafe"))?;
    if row.version!=1||row.limit!=limit||row.identity["session_id"]!=session||row.identity["engine"]!="codex"||row.identity["role"]!="coordinator"
        ||model.is_some_and(|model|row.identity["model"]!=model)||row.pending||row.used>limit
        ||original!= (Record{version:1,identity:row.identity.clone(),limit,used:0,pending:false,last_outcome:"new".into()}){
        return Err(denied("saved account turn identity, policy or settlement changed"));
    }
    Ok(())
}
impl AccountTurnHost {
    pub fn new(inner:Arc<dyn Host>,path:PathBuf,identity:Value,limit:u64,resume:bool)->io::Result<Self>{
        if !(1..=32).contains(&limit)||identity["engine"]!="codex"||identity["role"]!="coordinator"
            ||identity["model"].as_str().is_none_or(|model|!model.starts_with("gpt-")||model.len()>128){return Err(denied("account turn allowance requires an exact native Codex coordinator"));}
        let parent=path.parent().ok_or_else(||denied("account journal parent unavailable"))?;
        let directory=PrivateDirectory::open(parent,true)?;
        let lock=directory.open_file(&basename(&path.with_extension("lock"))?,libc::O_RDWR|libc::O_CREAT)?;
        let meta=lock.metadata()?;
        if !meta.is_file()||meta.nlink()!=1||meta.uid()!=unsafe{libc::geteuid()}||meta.mode()&0o077!=0
            ||unsafe{libc::flock(lock.as_raw_fd(),libc::LOCK_EX|libc::LOCK_NB)}!=0{return Err(denied("account journal is unsafe or already active"));}
        let policy=path.with_extension("policy.json");
        let journal=Journal{path,policy,directory,_lock:lock};
        let original=Record{version:1,identity:identity.clone(),limit,used:0,pending:false,last_outcome:"new".into()};
        let state=if resume {
            if journal.read_named(&journal.policy)?!=original{return Err(denied("account turn policy changed or missing"));}
            let row=journal.read()?;
            if row.version!=1||row.identity!=identity||row.limit!=limit||row.used>limit||row.pending
                ||!matches!(row.last_outcome.as_str(),"new"|"completed"|"refused"|"error") {return Err(denied("account turn identity or settlement changed; allowance withheld"));}row
        }else{
            journal.write_named(&journal.policy,&original,true)?;journal.write(&original,true)?;original
        };
        Ok(Self{inner,journal,state:Mutex::new(state)})
    }
    fn reserve(&self)->io::Result<()>{
        let mut state=self.state.lock().map_err(|_|denied("account turn state unavailable"))?;
        let original=Record{version:1,identity:state.identity.clone(),limit:state.limit,used:0,pending:false,last_outcome:"new".into()};
        if self.journal.read_named(&self.journal.policy)?!=original||self.journal.read()?!=*state||state.pending||state.used>=state.limit{return Err(denied("account turn allowance exhausted, unsettled or changed"));}
        let mut next=state.clone();next.used+=1;next.pending=true;next.last_outcome="pending".into();
        if let Err(error)=self.journal.write(&next,false){state.pending=true;return Err(error);}
        *state=next;Ok(())
    }
    fn prompt_images(&self,text:&str,images:&[doxa_input::Attachment],origin:Option<&PeerTurnOrigin>,emit:&mut dyn FnMut(Value)){
        if self.reserve().is_err(){emit(json!({"type":"turn_refused","data":{"reason":"account_turn_allowance","message":"Provider-account turn allowance exhausted, unsettled or changed; no new provider turn was dispatched"}}));return;}
        let mut terminals=0;let mut outcome="pending";let mut terminal=None;
        // Every dispatch consumes one attempt, including peer/queued/image and
        // /compact prompts. Cancellation/refusal/error never refunds an attempt.
        self.inner.prompt_with_images(text,images,origin,&mut|event|{
            if matches!(event["type"].as_str(),Some("turn_done"|"turn_refused")){
                terminals+=1;outcome=if event["type"]=="turn_refused"{"refused"}else if event["data"]["is_error"]==true{"error"}else{"completed"};
                if terminal.is_none(){terminal=Some(event);}return;
            }
            emit(event);
        });
        if terminals==1 {if let Ok(mut state)=self.state.lock(){
            if self.journal.read().is_ok_and(|record|record==*state){
                let mut next=state.clone();next.pending=false;next.last_outcome=outcome.into();
                if self.journal.write(&next,false).is_ok(){*state=next;}
            }
        }}
        let mut event=terminal.unwrap_or_else(||json!({"type":"turn_done","data":{"is_error":true,"error":"Provider-account attempt did not reach a verified terminal boundary"}}));
        let allowance=self.allowance();
        if terminals!=1||allowance["pending"]!=false{event["type"]=json!("turn_done");event["data"]["is_error"]=json!(true);event["data"]["failure_category"]=json!("account_turn_unsettled");}
        event["data"]["provider_charge_usd"]=Value::Null;event["data"]["cost_usd"]=Value::Null;event["data"]["session_cost_usd"]=Value::Null;
        event["data"]["cost_scope"]=json!("provider_account_unknown");event["data"]["account_turn_allowance"]=allowance;emit(event);
    }
    fn allowance(&self)->Value{
        self.state.lock().map(|state|json!({"limit":state.limit,"used":state.used,"remaining":state.limit.saturating_sub(state.used),"pending":state.pending,"durable":true,"provider_charge_usd":null,"outside_api_usd_budget":true})).unwrap_or_else(|_|json!({"unavailable":true,"provider_charge_usd":null,"outside_api_usd_budget":true}))
    }
}
impl Host for AccountTurnHost {
    fn prompt(&self,text:&str,emit:&mut dyn FnMut(Value)){self.prompt_images(text,&[],None,emit)}
    fn prompt_with_peer_origin(&self,text:&str,origin:Option<&PeerTurnOrigin>,emit:&mut dyn FnMut(Value)){self.prompt_images(text,&[],origin,emit)}
    fn prompt_with_images(&self,text:&str,images:&[doxa_input::Attachment],origin:Option<&PeerTurnOrigin>,emit:&mut dyn FnMut(Value)){self.prompt_images(text,images,origin,emit)}
    fn take_native_tool_audit(&self)->Option<Value>{self.inner.take_native_tool_audit()}
    fn supports_images(&self)->bool{self.inner.supports_images()}
    fn call(&self,method:&str,params:&Value)->Result<Value,String>{
        if method=="set_model"{return Err("model changes require a newly reviewed account turn policy".into());}
        if method=="isolation_migration_plan"{let mut plan=self.inner.call(method,params)?;plan["account_turn_allowance"]=self.allowance();return Ok(plan);}
        if method=="isolation_migration_stop" {
            if params["expected"]["account_turn_allowance"]!=self.allowance()||self.allowance()["pending"]!=false{return Err("account turn policy changed before migration".into());}
            let mut inner=params.clone();inner["expected"].as_object_mut().ok_or("invalid migration review")?.remove("account_turn_allowance");
            let mut result=self.inner.call(method,&inner)?;result["migration_plan"]["account_turn_allowance"]=self.allowance();return Ok(result);
        }
        self.inner.call(method,params)
    }
    fn initial_model(&self)->Option<String>{self.inner.initial_model()}
    fn initial_effort(&self)->Option<String>{self.inner.initial_effort()}
    fn initial_permission_mode(&self)->String{self.inner.initial_permission_mode()}
    fn isolation_status(&self)->Option<Value>{self.inner.isolation_status()}
    fn has_active_work(&self)->bool{self.inner.has_active_work()}
    fn peer_tools_ready(&self)->bool{self.inner.peer_tools_ready()}
    fn set_session_tool_handler(&self,_handler:doxa_runtime::PeerToolHandler)->bool{false}
    fn set_peer_tool_handler(&self,handler:doxa_runtime::PeerToolHandler)->bool{self.inner.set_peer_tool_handler(handler)}
    fn can_set_model(&self)->bool{false}
    fn model_change_requires_idle(&self)->bool{true}
    fn can_set_permission_mode(&self)->bool{self.inner.can_set_permission_mode()}
    fn permission_change_requires_idle(&self)->bool{self.inner.permission_change_requires_idle()}
    fn can_set_permission_mode_while_running(&self,mode:&str)->bool{self.inner.can_set_permission_mode_while_running(mode)}
    fn account_snapshot(&self)->Option<Value>{self.inner.account_snapshot()}
    fn billing_snapshot(&self)->Option<Value>{let mut value=self.inner.billing_snapshot().unwrap_or_else(||json!({}));value["provider_charge_usd"]=Value::Null;value["account_turn_allowance"]=self.allowance();value["total_cost_usd"]=Value::Null;value["cost_usd"]=Value::Null;value["session_cost_usd"]=Value::Null;value["cost_scope"]=json!("provider_account_unknown");Some(value)}
    fn lore_enabled(&self)->Option<bool>{self.inner.lore_enabled()}
    fn lore_status(&self)->Option<Value>{self.inner.lore_status()}
    fn lore_scrub_status(&self)->Option<&'static str>{self.inner.lore_scrub_status()}
    fn public_prompt(&self,text:&str)->Result<String,String>{self.inner.public_prompt(text)}
    fn transcript_snapshot(&self)->io::Result<Option<(PathBuf,u64)>>{self.inner.transcript_snapshot()}
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize,Ordering};
    use std::os::unix::fs::PermissionsExt;
    struct Fixture {path:PathBuf,calls:AtomicUsize,images:AtomicUsize}
    impl Host for Fixture {
        fn prompt(&self,text:&str,emit:&mut dyn FnMut(Value)){
            let record:Value=serde_json::from_slice(&fs::read(&self.path).unwrap()).unwrap();
            assert_eq!(record["pending"],true,"durable reservation precedes every actual provider dispatch");
            self.calls.fetch_add(1,Ordering::SeqCst);
            if text=="panic"{panic!("fixture provider panic");}
            if text=="missing"{return;}
            if text=="two-terminals"{emit(json!({"type":"turn_done","data":{}}));}
            emit(json!({"type":if text=="refuse"{"turn_refused"}else{"turn_done"},"data":{"is_error":text=="error","cost_usd":99999}}));
        }
        fn prompt_with_images(&self,text:&str,images:&[doxa_input::Attachment],_:Option<&PeerTurnOrigin>,emit:&mut dyn FnMut(Value)){
            self.images.fetch_add(images.len(),Ordering::SeqCst);self.prompt(text,emit);
        }
        fn call(&self,method:&str,params:&Value)->Result<Value,String>{
            match method {
                "isolation_migration_plan"=>Ok(json!({"launch":{"engine":"codex"},"model":"gpt-fixture"})),
                "isolation_migration_stop"=>{assert!(params["expected"]["account_turn_allowance"].is_null());Ok(json!({"migration_plan":params["expected"]}))},
                _=>Ok(json!({})),
            }
        }
        fn initial_model(&self)->Option<String>{Some("gpt-fixture".into())}
    }
    fn private_temp()->tempfile::TempDir{let dir=tempfile::tempdir().unwrap();fs::set_permissions(dir.path(),fs::Permissions::from_mode(0o700)).unwrap();dir}
    fn identity()->Value{json!({"session_id":"session","engine":"codex","role":"coordinator","model":"gpt-fixture","cwd":"/fixture"})}
    fn fixture(root:&std::path::Path)->Arc<Fixture>{Arc::new(Fixture{path:root.join("account/session.json"),calls:AtomicUsize::new(0),images:AtomicUsize::new(0)})}
    #[test]
    fn account_directory_rejects_shared_alias_and_swapped_parent_before_dispatch(){
        for scenario in ["shared","alias","swapped","replaced_alias","chmod"] {
            let dir=private_temp();let root=dir.path().join("owned");fs::create_dir(&root).unwrap();
            fs::set_permissions(&root,fs::Permissions::from_mode(0o700)).unwrap();
            if scenario=="shared"{fs::set_permissions(&root,fs::Permissions::from_mode(0o770)).unwrap();}
            let exposed=if scenario=="alias"{let alias=dir.path().join("alias");std::os::unix::fs::symlink(&root,&alias).unwrap();alias}else{root.clone()};
            let fixture=fixture(&exposed);
            if matches!(scenario,"shared"|"alias") {
                assert!(AccountTurnHost::new(fixture.clone(),fixture.path.clone(),identity(),2,false).is_err());
                assert!(preflight(&fixture.path,Some(2),false,"session",Some("gpt-fixture")).is_err());
            }else{
                let host=AccountTurnHost::new(fixture.clone(),fixture.path.clone(),identity(),2,false).unwrap();
                let parent=fixture.path.parent().unwrap();let original=fs::read(&fixture.path).unwrap();
                if scenario=="chmod"{fs::set_permissions(&root,fs::Permissions::from_mode(0o770)).unwrap();}
                else{let moved=root.join("moved");fs::rename(parent,&moved).unwrap();
                    if scenario=="replaced_alias"{std::os::unix::fs::symlink(&moved,parent).unwrap();}
                    else{fs::create_dir(parent).unwrap();fs::set_permissions(parent,fs::Permissions::from_mode(0o700)).unwrap();fs::write(&fixture.path,&original).unwrap();fs::write(fixture.path.with_extension("policy.json"),fs::read(moved.join("session.policy.json")).unwrap()).unwrap();}
                }
                let mut events=Vec::new();host.prompt("must-refuse",&mut|event|events.push(event));
                assert_eq!(events.last().unwrap()["type"],"turn_refused","{scenario}");
                assert_eq!(host.allowance()["used"],0,"{scenario}");
            }
            assert_eq!(fixture.calls.load(Ordering::SeqCst),0,"{scenario}");
        }
    }
    #[test]
    fn account_attempts_include_peer_attachment_compaction_refusal_and_error_without_refunds(){
        let dir=private_temp();let fixture=fixture(dir.path());
        let host=AccountTurnHost::new(fixture.clone(),fixture.path.clone(),identity(),4,false).unwrap();
        let image=doxa_input::Attachment::from_source("source.txt",b"fixture bytes".to_vec()).unwrap();
        let mut events=Vec::new();host.prompt_with_images("image",&[image],None,&mut|event|events.push(event));
        let origin=PeerTurnOrigin::new("worker".into(),"Worker".into(),Some("message-1".into()),None,None,None,"peer_message".into()).unwrap();
        host.prompt_with_peer_origin("refuse",Some(&origin),&mut|event|events.push(event));
        host.prompt("/compact",&mut|event|events.push(event));host.prompt("error",&mut|event|events.push(event));
        host.prompt("over-limit",&mut|event|events.push(event));
        assert_eq!(fixture.calls.load(Ordering::SeqCst),4);assert_eq!(fixture.images.load(Ordering::SeqCst),1);
        assert_eq!(events.last().unwrap()["type"],"turn_refused");
        let billing=host.billing_snapshot().unwrap();assert_eq!(billing["account_turn_allowance"]["used"],4);
        assert_eq!(billing["account_turn_allowance"]["pending"],false);assert!(billing["provider_charge_usd"].is_null());
        assert!(events.iter().filter(|event|event["type"]=="turn_done").all(|event|event["data"]["cost_usd"].is_null()));
    }
    #[test]
    fn settled_allowance_survives_respawn_and_migration_without_model_or_limit_changes(){
        let dir=private_temp();let fixture=fixture(dir.path());let path=fixture.path.clone();
        let host=AccountTurnHost::new(fixture.clone(),path.clone(),identity(),2,false).unwrap();host.prompt("work",&mut|_|{});
        let plan=host.call("isolation_migration_plan",&json!({})).unwrap();assert_eq!(plan["account_turn_allowance"]["used"],1);
        let mut changed=plan.clone();changed["account_turn_allowance"]["limit"]=json!(3);
        assert!(host.call("isolation_migration_stop",&json!({"confirmed":true,"expected":changed})).is_err());
        assert!(host.call("isolation_migration_stop",&json!({"confirmed":true,"expected":plan})).is_ok());
        assert!(host.call("set_model",&json!({"model":"other"})).is_err());drop(host);
        assert!(AccountTurnHost::new(fixture.clone(),path.clone(),identity(),3,true).is_err());
        let mut wrong=identity();wrong["model"]=json!("gpt-other");assert!(AccountTurnHost::new(fixture.clone(),path.clone(),wrong,2,true).is_err());
        let resumed=AccountTurnHost::new(fixture.clone(),path,identity(),2,true).unwrap();resumed.prompt("second",&mut|_|{});resumed.prompt("third",&mut|_|{});
        assert_eq!(fixture.calls.load(Ordering::SeqCst),2);assert_eq!(resumed.allowance()["remaining"],0);
    }
    #[test]
    fn incomplete_crashed_missing_corrupt_and_duplicate_records_fail_closed(){
        for scenario in ["missing","panic","two-terminals","remove","corrupt","duplicate"] {
            let dir=private_temp();let fixture=fixture(dir.path());let path=fixture.path.clone();
            let host=AccountTurnHost::new(fixture.clone(),path.clone(),identity(),2,false).unwrap();
            match scenario {
                "missing"=>host.prompt("missing",&mut|_|{}),
                "two-terminals"=>{let mut terminal=None;host.prompt("two-terminals",&mut|event|terminal=Some(event));assert_eq!(terminal.unwrap()["data"]["failure_category"],"account_turn_unsettled");},
                "panic"=>{let _=std::panic::catch_unwind(std::panic::AssertUnwindSafe(||host.prompt("panic",&mut|_|{})));},
                "remove"=>{fs::remove_file(&path).unwrap();host.prompt("withheld",&mut|_|{});assert_eq!(fixture.calls.load(Ordering::SeqCst),0);},
                "corrupt"=>{fs::write(&path,b"{}").unwrap();host.prompt("withheld",&mut|_|{});assert_eq!(fixture.calls.load(Ordering::SeqCst),0);},
                "duplicate"=>{assert!(AccountTurnHost::new(fixture.clone(),path.clone(),identity(),2,true).is_err());continue;},_=>unreachable!(),
            }
            drop(host);assert!(AccountTurnHost::new(fixture.clone(),path,identity(),2,true).is_err(),"reopened {scenario}");
        }
        let dir=private_temp();let fixture=fixture(dir.path());let mut worker=identity();worker["role"]=json!("worker");
        assert!(AccountTurnHost::new(fixture.clone(),fixture.path.clone(),worker,2,false).is_err());
    }
    #[test]
    fn queued_peer_turns_reserve_individually_before_the_fixture_provider(){
        use std::{sync::mpsc,time::{Duration,Instant}};
        struct Blocking {inner:Arc<Fixture>,started:mpsc::Sender<()>,release:Mutex<mpsc::Receiver<()>>}
        impl Host for Blocking {
            fn prompt(&self,text:&str,emit:&mut dyn FnMut(Value)){
                if text=="first"{self.started.send(()).unwrap();self.release.lock().unwrap().recv_timeout(Duration::from_secs(3)).unwrap();}
                self.inner.prompt(text,emit);
            }
            fn call(&self,_:&str,_:&Value)->Result<Value,String>{Ok(json!({}))}
        }
        let dir=private_temp();let fixture=fixture(dir.path());let(started,notice)=mpsc::channel();let(release,wait)=mpsc::channel();
        let inner=Arc::new(Blocking{inner:fixture.clone(),started,release:Mutex::new(wait)});
        let host=Arc::new(AccountTurnHost::new(inner,fixture.path.clone(),identity(),2,false).unwrap());
        let mut daemon=doxa_runtime::Daemon::bind(dir.path().join("rt"),doxa_runtime::Session{session_id:"session".into(),cwd:"/fixture".into(),model:Some("gpt-fixture".into()),engine:"codex".into(),doxa_version:"test".into()},host.clone()).unwrap().start();
        let origin=PeerTurnOrigin::new("worker".into(),"Worker".into(),Some("message-1".into()),None,None,None,"peer_message".into()).unwrap();
        daemon.enqueue_peer_prompt_with_origin("first".into(),origin.clone()).unwrap();notice.recv_timeout(Duration::from_secs(3)).unwrap();
        assert_eq!(daemon.enqueue_peer_prompt_with_origin("second".into(),origin.clone()).unwrap(),doxa_runtime::ExternalPrompt::Queued);
        assert_eq!(daemon.enqueue_peer_prompt_with_origin("third".into(),origin).unwrap(),doxa_runtime::ExternalPrompt::Queued);release.send(()).unwrap();
        let deadline=Instant::now()+Duration::from_secs(3);
        while host.allowance()["used"]!=2||host.allowance()["pending"]!=false{assert!(Instant::now()<deadline);std::thread::sleep(Duration::from_millis(5));}
        std::thread::sleep(Duration::from_millis(20));assert_eq!(fixture.calls.load(Ordering::SeqCst),2);daemon.shutdown();
    }
}
