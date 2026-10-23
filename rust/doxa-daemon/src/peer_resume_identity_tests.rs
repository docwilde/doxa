use super::*;
use doxa_fleet::{Assignment,Charter,Context};

struct Idle;
impl Host for Idle{
    fn prompt(&self,_:&str,_:&mut dyn FnMut(Value)){}
    fn call(&self,_:&str,_:&Value)->Result<Value,String>{Ok(json!({}))}
}
struct Home(Vec<(&'static str,Option<std::ffi::OsString>)>);
impl Home{
    fn isolated(root:&Path)->Self{
        let previous=["DOXA_HOME","DOXA_PEER_LEDGER"].into_iter().map(|key|(key,std::env::var_os(key))).collect();
        std::env::set_var("DOXA_HOME",root);std::env::remove_var("DOXA_PEER_LEDGER");Self(previous)
    }
}
impl Drop for Home{fn drop(&mut self){for(key,value)in self.0.drain(..){match value{Some(value)=>std::env::set_var(key,value),None=>std::env::remove_var(key)}}}}
fn fixture()->(tempfile::TempDir,Home,Arc<PeerHost>,Context){
    let dir=tempfile::tempdir().unwrap();fs::set_permissions(dir.path(),fs::Permissions::from_mode(0o700)).unwrap();
    let home=Home::isolated(dir.path());let runtime=dir.path().join("rt");fs::create_dir(&runtime).unwrap();fs::set_permissions(&runtime,fs::Permissions::from_mode(0o700)).unwrap();
    let(tx,_)=std::sync::mpsc::sync_channel(1);
    let peer=Arc::new(PeerHost::new(Arc::new(Idle),runtime,dir.path(),"worker".into(),"worker".into(),tx).unwrap());
    let charter=Charter{version:1,fleet_id:"identity".into(),task:"Scoped inspection".into(),repo:dir.path().display().to_string(),allowed_paths:vec![String::new()],required_evidence:vec![],worker_limit:1,run_budget_usd:Some(1.0),deadline:0,human_actions:vec![],test_recipe:None};
    let context=Context{charter_sha256:doxa_fleet::hash(&charter).unwrap(),charter,assignments:vec![Assignment{id:"worker-id".into(),session_id:"worker".into(),pid:std::process::id() as i32,role:"worker".into(),task:"Inspect source".into(),cwd:dir.path().display().to_string(),base_commit:None,allowed_paths:vec![],depends_on:vec![]}],review:doxa_fleet::ReviewConfig{budget_usd:1.0,..Default::default()},state_path:dir.path().join("guard-state.json")};
    assert!(peer.call("fleet_identity",&json!({})).unwrap()["guard"].is_null());
    assert!(peer.call("fleet_identity",&json!({"validate_guard":true})).is_err());
    peer.call("fleet_configure",&serde_json::to_value(&context).unwrap()).unwrap();
    (dir,home,peer,context)
}

#[test]
fn large_persisted_guard_has_a_compact_validated_resume_identity_and_owner_acknowledgement(){
    use doxa_tui::transport::DaemonClient;
    let(_dir,_home,peer,context)=fixture();
    let observation=json!({"event":"retained_receipt_fixture","body":"Retained scrubbed review evidence. ".repeat(5000)});
    doxa_fleet::transaction(&context,|state|{state.observations.push(observation.clone());state.paused=true;state.reason="Owner review required".into();Ok(())}).unwrap();
    let before=fs::read(&context.state_path).unwrap();assert!(before.len()>doxa_runtime::MAX_FRAME_BYTES);
    let mut daemon=doxa_runtime::Daemon::bind(peer.fleet_path.parent().unwrap(),doxa_runtime::Session{session_id:"worker".into(),cwd:context.charter.repo.clone(),model:None,engine:"fixture".into(),doxa_version:env!("CARGO_PKG_VERSION").into()},peer.clone()).unwrap().start();
    let mut client=DaemonClient::connect(daemon.socket_path(),None).unwrap();
    let full=client.call("fleet_state",serde_json::Map::new()).unwrap();assert_eq!(full["ok"],false);assert_eq!(full["error"],"reply exceeded the frame cap","the global frame cap must remain enforced");
    let identity=client.call("fleet_identity",json!({"validate_guard":true}).as_object().unwrap().clone()).unwrap();
    assert_eq!(identity["ok"],true);assert!(identity.to_string().len()<2048);assert_eq!(identity["session_id"],"worker");assert_eq!(identity["pid"],std::process::id());
    assert_eq!(identity["guard"]["version"],1);assert_eq!(identity["guard"]["context_sha256"],doxa_fleet::hash(&context).unwrap());
    assert_eq!(identity["guard"]["charter_sha256"],context.charter_sha256);assert_eq!(identity["guard"]["assignments_sha256"],doxa_fleet::hash(&context.assignments).unwrap());assert_eq!(identity["guard"]["assignment_sha256"],doxa_fleet::hash(&context.assignments[0]).unwrap());
    assert_eq!(fs::read(&context.state_path).unwrap(),before,"identity inspection must retain the complete guard and receipts");
    let refused=client.call("fleet_resume",json!({"compact":true,"charter_sha256":"different"}).as_object().unwrap().clone()).unwrap();assert_eq!(refused["ok"],false);
    let resumed=client.call("fleet_resume",json!({"compact":true,"charter_sha256":context.charter_sha256}).as_object().unwrap().clone()).unwrap();assert_eq!(resumed["ok"],true);assert_eq!(resumed["resumed"],true);assert_eq!(resumed["charter_sha256"],context.charter_sha256);assert!(resumed.to_string().len()<1024);
    let after:doxa_fleet::State=doxa_fleet::read_private(&context.state_path,doxa_fleet::MAX_STATE).unwrap();assert!(!after.paused);assert_eq!(after.observations[0],observation);assert!(fs::metadata(&context.state_path).unwrap().len()>doxa_runtime::MAX_FRAME_BYTES as u64);
    daemon.shutdown();
}

#[test]
fn validated_resume_identity_refuses_stale_context_guard_and_host_bindings(){
    let(_dir,_home,peer,context)=fixture();let request=json!({"validate_guard":true});
    assert!(peer.call("fleet_identity",&request).is_ok());
    for request in [json!({"validate_guard":false}),json!({"validate_guard":"true"}),json!({"validate_guard":true,"other":true})]{assert!(peer.call("fleet_identity",&request).is_err());}
    let mut changed=context.clone();changed.review.budget_usd=2.0;doxa_fleet::save_private(&peer.fleet_path,&changed).unwrap();assert!(peer.call("fleet_identity",&request).unwrap_err().contains("Persisted fleet context changed"));doxa_fleet::save_private(&peer.fleet_path,&context).unwrap();
    let state:doxa_fleet::State=doxa_fleet::read_private(&context.state_path,doxa_fleet::MAX_STATE).unwrap();
    for field in ["charter_sha256","assignments_sha256"]{
        let mut changed=serde_json::to_value(&state).unwrap();changed[field]=json!("mismatched");doxa_fleet::save_private(&context.state_path,&changed).unwrap();assert!(peer.call("fleet_identity",&request).is_err(),"{field}");assert_eq!(doxa_fleet::read_private::<Value>(&context.state_path,doxa_fleet::MAX_STATE).unwrap(),changed,"a refused identity must not replace a mismatched guard");doxa_fleet::save_private(&context.state_path,&state).unwrap();
    }
    for field in ["pid","cwd","session_id"]{
        let mut changed=context.clone();match field{"pid"=>changed.assignments[0].pid+=1,"cwd"=>changed.assignments[0].cwd.push_str("/other"),_=>changed.assignments[0].session_id="other".into()};
        *peer.fleet.lock().unwrap()=Some(changed.clone());doxa_fleet::save_private(&peer.fleet_path,&changed).unwrap();assert!(peer.call("fleet_identity",&request).is_err(),"{field}");
    }
    *peer.fleet.lock().unwrap()=Some(context.clone());doxa_fleet::save_private(&peer.fleet_path,&context).unwrap();
    fs::set_permissions(&context.state_path,fs::Permissions::from_mode(0o644)).unwrap();assert!(peer.call("fleet_identity",&request).is_err());fs::set_permissions(&context.state_path,fs::Permissions::from_mode(0o600)).unwrap();
    fs::write(&context.state_path,b"{invalid guard}").unwrap();assert!(peer.call("fleet_identity",&request).is_err());
    assert!(peer.call("fleet_identity",&json!({})).unwrap()["guard"].is_null(),"pre-configuration metadata contract remains independent of guard validation");
}
