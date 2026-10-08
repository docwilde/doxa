//! Isolation is enforced below provider hosts; this wrapper exposes its actual
//! policy and permits only explicit idle user changes through host RPC.
use doxa_isolation::{Profile, Runtime};
use doxa_runtime::{Host, PeerToolHandler};
use serde_json::{json, Value};
use std::{io, path::PathBuf, sync::{Arc, Mutex}};

pub struct IsolationHost { inner: Arc<dyn Host>, runtime: Arc<Mutex<Runtime>>, launch:Value }
impl IsolationHost {
    pub fn new(inner: Arc<dyn Host>, runtime: Arc<Mutex<Runtime>>, launch:Value) -> Self { Self { inner, runtime, launch } }
    fn migration_plan(&self)->Result<Value,String>{
        if self.inner.has_active_work(){return Err("isolation migration requires an idle provider".into());}
        let snapshot=self.inner.transcript_snapshot().map_err(|e|e.to_string())?;
        let fingerprint=snapshot.as_ref().map(|(path,bytes)|doxa_isolation::migration::fingerprint(path,*bytes)).transpose().map_err(|e|e.to_string())?;
        let runtime=self.runtime.lock().unwrap();
        Ok(json!({"manifest":runtime.manifest(),"launch":self.launch,
            "transcript_path":snapshot.as_ref().map(|(path,_)|path),
            "transcript_bytes":snapshot.as_ref().map(|(_,bytes)|bytes),
            "transcript_sha256":fingerprint,
            "model":self.inner.initial_model(),"effort":self.inner.initial_effort(),
            "permission_mode":self.inner.initial_permission_mode()}))
    }
}

#[cfg(test)]
mod tests{
    use super::*;
    use std::{fs,sync::atomic::{AtomicUsize,Ordering}};
    struct Checkpoint{path:PathBuf,stops:AtomicUsize}
    impl Host for Checkpoint{
        fn prompt(&self,_:&str,_:&mut dyn FnMut(Value)){}
        fn call(&self,method:&str,_:&Value)->Result<Value,String>{assert_eq!(method,"stop");self.stops.fetch_add(1,Ordering::SeqCst);Ok(json!({}))}
        fn transcript_snapshot(&self)->io::Result<Option<(PathBuf,u64)>>{Ok(Some((self.path.clone(),fs::metadata(&self.path)?.len())))}
    }
    #[test]
    fn migration_stop_binds_confirmation_to_exact_checkpoint_and_preserves_native_checkout(){
        let dir=tempfile::tempdir().unwrap();let root=fs::canonicalize(dir.path()).unwrap();let source=root.join("source");fs::create_dir(&source).unwrap();
        let path=root.join("transcript.jsonl");fs::write(&path,"old\n").unwrap();
        let runtime=Arc::new(Mutex::new(Runtime::prepare(&root.join("home"),"session",&source,Some(Profile::Native),false,None).unwrap()));
        let checkpoint=Arc::new(Checkpoint{path:path.clone(),stops:AtomicUsize::new(0)});let host=IsolationHost::new(checkpoint.clone(),runtime.clone(),json!({"engine":"codex"}));
        let stale=host.call("isolation_migration_plan",&json!({})).unwrap();fs::write(&path,"new\n").unwrap();
        assert!(host.call("isolation_migration_stop",&json!({"confirmed":true,"expected":stale})).is_err());
        let current=host.call("isolation_migration_plan",&json!({})).unwrap();
        assert!(host.call("isolation_migration_stop",&json!({"confirmed":false,"expected":current})).is_err());
        assert_eq!(checkpoint.stops.load(Ordering::SeqCst),0);assert!(!runtime.lock().unwrap().preserves_native_checkout());
        host.call("isolation_migration_stop",&json!({"confirmed":true,"expected":current})).unwrap();
        assert_eq!(checkpoint.stops.load(Ordering::SeqCst),1);assert!(runtime.lock().unwrap().preserves_native_checkout());assert!(source.is_dir());
    }
}
impl Host for IsolationHost {
    fn prompt(&self,text:&str,emit:&mut dyn FnMut(Value)) {
        if self.runtime.lock().unwrap().profile().docker() {
            // Every provider transport launch also checks the actual container.
            if let Err(error) = doxa_isolation::active() {
                emit(json!({"type":"turn_refused","data":{"message":error.to_string()}})); return;
            }
        }
        self.inner.prompt(text,emit)
    }
    fn call(&self,method:&str,params:&Value)->Result<Value,String> {
        if method=="isolation_migration_plan"{return self.migration_plan();}
        if method=="isolation_migration_stop"{
            let plan=self.migration_plan()?;
            if params["confirmed"]!=true||params["expected"]!=plan{return Err("isolation migration snapshot changed; review it again".into());}
            if matches!(self.launch["engine"].as_str(),Some("glm"|"deepseek")){
                self.inner.call("checkpoint_for_migration",&json!({}))?;
            }
            self.inner.call("stop",&json!({}))?;
            self.runtime.lock().unwrap().mark_migration_stop();
            return Ok(json!({"migration_plan":plan}));
        }
        if method == "set_isolation" {
            let profile = Profile::parse(params["profile"].as_str().ok_or("missing isolation profile")?).map_err(|e|e.to_string())?;
            if self.inner.has_active_work() { return Err("isolation changes require an idle provider".into()); }
            let status = self.runtime.lock().unwrap().set_profile(profile,params["confirmed"]==true).map_err(|e|e.to_string())?;
            return Ok(json!({"isolation":status}));
        }
        self.inner.call(method,params)
    }
    fn isolation_status(&self)->Option<Value>{Some(self.runtime.lock().unwrap().status())}
    fn initial_model(&self)->Option<String>{self.inner.initial_model()}
    fn initial_effort(&self)->Option<String>{self.inner.initial_effort()}
    fn initial_permission_mode(&self)->String{self.inner.initial_permission_mode()}
    fn has_active_work(&self)->bool{self.inner.has_active_work()}
    fn can_set_model(&self)->bool{self.inner.can_set_model()}
    fn model_change_requires_idle(&self)->bool{self.inner.model_change_requires_idle()}
    fn can_set_permission_mode(&self)->bool{self.inner.can_set_permission_mode()}
    fn permission_change_requires_idle(&self)->bool{self.inner.permission_change_requires_idle()}
    fn set_peer_tool_handler(&self,handler:PeerToolHandler)->bool{self.inner.set_peer_tool_handler(handler)}
    fn set_session_tool_handler(&self,handler:PeerToolHandler)->bool{self.inner.set_session_tool_handler(handler)}
    fn peer_tools_ready(&self)->bool{self.inner.peer_tools_ready()}
    fn billing_snapshot(&self)->Option<Value>{self.inner.billing_snapshot()}
    fn lore_enabled(&self)->Option<bool>{self.inner.lore_enabled()}
    fn account_snapshot(&self)->Option<Value>{self.inner.account_snapshot()}
    fn lore_status(&self)->Option<Value>{self.inner.lore_status()}
    fn lore_scrub_status(&self)->Option<&'static str>{self.inner.lore_scrub_status()}
    fn public_prompt(&self,text:&str)->Result<String,String>{self.inner.public_prompt(text)}
    fn transcript_snapshot(&self)->io::Result<Option<(PathBuf,u64)>>{self.inner.transcript_snapshot()}
}
