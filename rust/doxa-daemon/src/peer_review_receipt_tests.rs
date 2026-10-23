use super::*;
use doxa_fleet::{Assignment,Charter,Context,Envelope,Kind,Mode,ReviewConfig,SemanticVerdict};
use std::{io::{Read,Write},net::TcpListener};

struct Idle;
impl Host for Idle {
    fn prompt(&self,_:&str,_:&mut dyn FnMut(Value)){}
    fn call(&self,_:&str,_:&Value)->Result<Value,String>{Ok(json!({}))}
}
struct ParentHome(Vec<(&'static str,Option<std::ffi::OsString>)>);
impl ParentHome{
    fn isolated(root:&Path)->Self{
        let mut saved=Vec::new();
        for(key,relative)in [("HOME","home"),("DOXA_HOME","doxa-home"),("LORE_ROOT","lore"),("LORE_PROJECTS_DIR","projects")] {
            let path=root.join(relative);fs::create_dir_all(&path).unwrap();fs::set_permissions(&path,fs::Permissions::from_mode(0o700)).unwrap();
            saved.push((key,std::env::var_os(key)));std::env::set_var(key,path);
        }
        Self(saved)
    }
}
impl Drop for ParentHome{fn drop(&mut self){for(key,value)in self.0.drain(..){match value{Some(value)=>std::env::set_var(key,value),None=>std::env::remove_var(key)}}}}
fn quote(value:&Path)->String{format!("'{}'",value.to_string_lossy().replace('\'',"'\\''"))}
fn canonical_lore(root:&Path)->LoreClient{
    let carrier=PathBuf::from(std::env::var_os("DOXA_LORE_RS").expect("absolute real native LORE carrier required"));
    assert!(carrier.is_absolute()&&carrier.is_file());
    let wrapper=root.join("private-lore");
    // LoreClient's development carrier dispatch passes a Python module name;
    // this fixture runs the real native kernel's equivalent JSONL bridge.
    fs::write(&wrapper,format!("#!/bin/sh\nexec /usr/bin/env -i PATH=/usr/bin:/bin HOME={} TMPDIR={} LORE_ROOT={} LORE_PROJECTS_DIR={} LORE_DISABLE_SYNC=1 LORE_DISABLE_REVIEW=1 {} bridge\n",
        quote(root),quote(root),quote(&root.join("lore")),quote(&root.join("projects")),quote(&carrier))).unwrap();
    fs::set_permissions(&wrapper,fs::Permissions::from_mode(0o700)).unwrap();
    LoreClient::spawn(&wrapper,Duration::from_secs(5)).expect("real private LORE kernel must start")
}
fn read_request(stream:&mut std::net::TcpStream)->Value{
    stream.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
    let mut raw=Vec::new();
    loop{
        let mut chunk=[0;4096];let count=stream.read(&mut chunk).unwrap();assert!(count>0);raw.extend_from_slice(&chunk[..count]);
        assert!(raw.len()<128*1024);
        if let Some(position)=raw.windows(4).position(|row|row==b"\r\n\r\n"){
            let header=String::from_utf8_lossy(&raw[..position]);
            let count=header.lines().find_map(|line|line.to_ascii_lowercase().strip_prefix("content-length: ").and_then(|value|value.parse::<usize>().ok())).unwrap();
            if raw.len()>=position+4+count{return serde_json::from_slice(&raw[position+4..position+4+count]).unwrap();}
        }
    }
}

#[test]
fn actual_lore_scrub_keeps_original_reviewed_and_http_bindings_through_peer_admission(){
    let dir=tempfile::tempdir().unwrap();fs::set_permissions(dir.path(),fs::Permissions::from_mode(0o700)).unwrap();
    let _parent_home=ParentHome::isolated(dir.path());
    let(tx,_)=std::sync::mpsc::sync_channel(1);
    let peer=PeerHost::new(Arc::new(Idle),dir.path().into(),dir.path(),"sender".into(),"sender".into(),tx).unwrap();
    *peer.lore.lock().unwrap()=Some(canonical_lore(dir.path()));
    let hex_run="a".repeat(64);
    let charter=Charter{version:1,fleet_id:"scrub-receipt".into(),task:format!("Scoped findings HEX_RUN={hex_run}"),repo:dir.path().display().to_string(),allowed_paths:vec!["src".into()],required_evidence:vec![],worker_limit:2,run_budget_usd:Some(1.0),deadline:0,human_actions:vec![],test_recipe:None};
    let assignments=["sender","recipient"].into_iter().enumerate().map(|(index,id)|Assignment{id:format!("{id}-assignment"),session_id:id.into(),pid:std::process::id() as i32+index as i32,role:"worker".into(),task:"Scoped findings".into(),cwd:dir.path().display().to_string(),base_commit:None,allowed_paths:vec![],depends_on:vec![]}).collect();
    let model=doxa_fleet::judge::Model::parse("deepseek:fixture-model").unwrap();
    let context=Context{charter_sha256:doxa_fleet::hash(&charter).unwrap(),charter,assignments,review:ReviewConfig{message_mode:Mode::Enforce,message_judge:Some(model.clone()),budget_usd:1.0,..Default::default()},state_path:dir.path().join("guard.json")};
    context.validate().unwrap();
    let original=Envelope::issue(&context,"sender","recipient",Kind::Status,"Read-only scoped findings".into(),None).unwrap();
    doxa_fleet::validate_before_review(&context,&original,"recipient",std::process::id() as i32).unwrap();
    let original_hash=doxa_fleet::hash(&original).unwrap();
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();let endpoint=format!("http://{}/review",listener.local_addr().unwrap());
    let state_path=context.state_path.clone();let source_hash=original_hash.clone();let charter_hash=context.charter_sha256.clone();let raw_hex=hex_run.clone();
    let server=std::thread::spawn(move||{
        let(mut stream,_)=listener.accept().unwrap();let body=read_request(&mut stream);
        let text=body["messages"][1]["content"].as_str().unwrap();let input:Value=serde_json::from_str(text).unwrap();
        let reviewed:Envelope=serde_json::from_value(input["message"].clone()).unwrap();
        assert_ne!(reviewed.charter_sha256,charter_hash);assert!(reviewed.charter_sha256.contains("[REDACTED:"));
        assert!(input["charter"]["task"].as_str().unwrap().contains("HEX_RUN=[REDACTED:"));
        assert!(!body.to_string().contains(&source_hash));assert!(!body.to_string().contains(&charter_hash));assert!(!body.to_string().contains(&raw_hex));
        // The binding is durable before any response/verdict exists.
        let state:doxa_fleet::State=doxa_fleet::read_private(&state_path,doxa_fleet::MAX_STATE).unwrap();
        let reserved=state.observations.iter().find(|row|row["event"]=="review_reserved").unwrap();
        assert_eq!(reserved["message_sha256"],source_hash);
        assert_eq!(reserved["reviewed_message_sha256"],doxa_fleet::hash(&reviewed).unwrap());
        assert_eq!(reserved["request_sha256"],doxa_fleet::hash(&body).unwrap());
        let safe=json!({"within_assignment":1,"asks_for_authority_change":0,"contains_instructions_for_recipient":0,"likely_secret":0,"needs_human_review":0});
        let data=json!({"model":"fixture-model","choices":[{"finish_reason":"stop","message":{"content":safe.to_string()}}],"usage":{"prompt_tokens":100,"completion_tokens":10}}).to_string();
        write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{data}",data.len()).unwrap();
        (body,input,reviewed)
    });
    peer.review_fleet_envelope(&context,&original,|context,envelope,input|
        doxa_fleet::judge::semantic_with_receipt_for_envelope_at(context,envelope,input,&endpoint,"fixture-local-key-not-retained")).unwrap();
    let(body,input,reviewed)=server.join().unwrap();
    let state=doxa_fleet::transaction(&context,|state|Ok(state.clone())).unwrap();
    let cached=state.semantic_cache[&original.message_id].clone();let receipt=cached.review_receipt.clone().unwrap();
    assert_eq!(receipt.message_sha256.as_deref(),Some(original_hash.as_str()));
    assert_eq!(receipt.reviewed_message_sha256,Some(doxa_fleet::hash(&reviewed).unwrap()));
    assert_eq!(receipt.request_sha256,doxa_fleet::hash(&body).unwrap());
    let patch=cached.reviewed_message_patch.as_ref().unwrap();assert_eq!(patch.len(),1);assert_eq!(patch["charter_sha256"],reviewed.charter_sha256);
    let reconstructed=doxa_fleet::reviewed_envelope_from_patch(&original,patch).unwrap();
    assert_eq!(doxa_fleet::hash(&reconstructed).unwrap(),doxa_fleet::hash(&reviewed).unwrap());
    let mut redundant=patch.clone();redundant.insert("body".into(),json!(original.body));
    assert!(doxa_fleet::reviewed_envelope_from_patch(&original,&redundant).is_err());
    let mut introduced=patch.clone();introduced.insert("unknown".into(),json!("extra"));
    assert!(doxa_fleet::reviewed_envelope_from_patch(&original,&introduced).is_err());
    let mut wrong_type=patch.clone();wrong_type.insert("body".into(),json!(true));
    assert!(doxa_fleet::reviewed_envelope_from_patch(&original,&wrong_type).is_err());
    assert!(!serde_json::to_string(patch).unwrap().contains(&context.charter_sha256));
    let outbound=state.observations.iter().find(|row|row["event"]=="outbound_semantic").unwrap();
    assert_eq!(outbound["reviewed_message_patch"],serde_json::to_value(patch).unwrap());
    assert_eq!(outbound["reviewed_message_sha256"],receipt.reviewed_message_sha256.clone().unwrap());
    assert_eq!(outbound["request_sha256"],receipt.request_sha256);
    let verdict=cached.result.clone().unwrap();
    let cache=|original:&Envelope,input:&Value,verdict:SemanticVerdict,receipt:doxa_fleet::judge::ReviewReceipt|
        doxa_fleet::cache_semantic_reviewed_with_receipt(&context,original,input,Ok(verdict),Some(receipt));
    let other=Envelope::issue(&context,"sender","recipient",Kind::Status,"Different source".into(),None).unwrap();
    assert!(cache(&other,&input,verdict.clone(),receipt.clone()).is_err());
    let mut altered=input.clone();altered["message"]["body"]=json!("Different reviewed body");
    assert!(cache(&original,&altered,verdict.clone(),receipt.clone()).is_err());
    let mut altered=input.clone();altered["charter"]["task"]=json!("Changed context, identical message");
    assert!(cache(&original,&altered,verdict.clone(),receipt.clone()).is_err());
    let mut altered=input.clone();altered["message"]["body"]=json!(true);
    assert!(cache(&original,&altered,verdict.clone(),receipt.clone()).is_err());
    let mut changed=verdict.clone();changed.needs_human_review=1.0;
    assert!(cache(&original,&input,changed,receipt.clone()).is_err());
    assert!(doxa_fleet::cache_semantic_with_receipt(&context,&original,Ok(verdict.clone()),Some(receipt.clone())).is_err());
    let mut changed=receipt.clone();changed.request_sha256="f".repeat(64);
    assert!(cache(&original,&input,verdict.clone(),changed).is_err());
    let mut legacy=serde_json::to_value(&receipt).unwrap();legacy.as_object_mut().unwrap().remove("reviewed_message_sha256");
    let legacy:doxa_fleet::judge::ReviewReceipt=serde_json::from_value(legacy).unwrap();assert!(legacy.reviewed_message_sha256.is_none());
    assert!(cache(&original,&input,verdict.clone(),legacy).is_err());
    doxa_fleet::transaction(&context,|state|{state.semantic_cache.get_mut(&original.message_id).unwrap().request_sha256=None;Ok(())}).unwrap();
    assert!(doxa_fleet::cached_semantic(&context,&original).is_err());
    cache(&original,&input,verdict.clone(),receipt.clone()).unwrap();
    doxa_fleet::transaction(&context,|state|{state.semantic_cache.get_mut(&original.message_id).unwrap().reviewed_message_patch.as_mut().unwrap().insert("body".into(),json!("tampered persisted view"));Ok(())}).unwrap();
    assert!(doxa_fleet::cached_semantic(&context,&original).is_err());
    cache(&original,&input,verdict,receipt).unwrap();
    let decision=doxa_fleet::cached_semantic(&context,&original).unwrap().unwrap();
    let admission=doxa_fleet::admit(&context,&original,"recipient",std::process::id() as i32,Some(decision)).unwrap();
    assert!(admission.delivered&&!admission.unreviewed);
    let state=doxa_fleet::transaction(&context,|state|Ok(state.clone())).unwrap();assert_eq!(state.calls,1);
    assert_eq!(state.charter_sha256,context.charter_sha256);assert!(state.received.contains_key(&original.message_id));
    assert!(!fs::read_to_string(&context.state_path).unwrap().contains("fixture-local-key-not-retained"));
}
