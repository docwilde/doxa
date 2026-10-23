use super::*;
use crate::{Assignment, Charter, Envelope, Kind, Mode, ReviewConfig};
use std::os::unix::fs::PermissionsExt;

fn fixture() -> (tempfile::TempDir, Context, Model) {
    let dir = tempfile::tempdir().unwrap();
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let model = Model::parse("deepseek:fixture-model").unwrap();
    let charter = Charter {version:1,fleet_id:"correlation".into(),task:"Scoped review".into(),repo:"/fixture".into(),allowed_paths:vec!["src".into()],required_evidence:vec![],worker_limit:2,run_budget_usd:Some(1.0),deadline:0,human_actions:vec![],test_recipe:None};
    let assignments = ["a", "b"].map(|id|Assignment{id:id.into(),session_id:id.into(),pid:1,role:"worker".into(),task:"Scoped review".into(),cwd:"/fixture".into(),base_commit:None,allowed_paths:vec![],depends_on:vec![]}).to_vec();
    let context = Context {charter_sha256:hash(&charter).unwrap(),charter,assignments,review:ReviewConfig{message_judge:Some(model.clone()),message_mode:Mode::Shadow,budget_usd:1.0,..Default::default()},state_path:dir.path().join("state.json")};
    context.validate().unwrap();
    (dir, context, model)
}

fn message(context: &Context, body: &str) -> Envelope {
    Envelope::issue(context,"a","b",Kind::Status,body.into(),None).unwrap()
}
fn raw_safe() -> Value {
    // Integer JSON and typed f64 verdicts intentionally serialize differently.
    json!({"within_assignment":1,"asks_for_authority_change":0,"contains_instructions_for_recipient":0,"likely_secret":0,"needs_human_review":0})
}

#[test]
fn equal_outputs_keep_distinct_invocations_across_interleaving_and_ring_eviction() {
    let (_dir, context, model) = fixture();
    let a = message(&context,"First actual message");
    let b = message(&context,"Second actual message");
    let body_a = request(&model,SEMANTIC_INSTRUCTIONS,&json!({"message":a})).unwrap();
    let body_b = request(&model,SEMANTIC_INSTRUCTIONS,&json!({"message":b})).unwrap();
    let mut reserved_a = reserve(&context,&model,&body_a).unwrap();
    let mut reserved_b = reserve(&context,&model,&body_b).unwrap();
    reserved_a.message_sha256 = Some(hash(&a).unwrap());
    reserved_b.message_sha256 = Some(hash(&b).unwrap());
    let response = |latency_ms|Ok(Response{result:raw_safe(),model:model.model.clone(),input_tokens:100,output_tokens:10,latency_ms,review_receipt:None});
    let proof_b = settle(&context,&model,&reserved_b,&response(222)).unwrap().unwrap();
    let proof_a = settle(&context,&model,&reserved_a,&response(111)).unwrap().unwrap();
    assert_ne!(proof_a.invocation_id,proof_b.invocation_id);
    assert_ne!(proof_a.request_sha256,proof_b.request_sha256);
    assert_eq!(proof_a.output_sha256,proof_b.output_sha256);
    assert_eq!((proof_a.call,proof_b.call),(1,2));
    let verdict = ||serde_json::from_value::<SemanticVerdict>(raw_safe()).unwrap();
    assert_ne!(proof_a.output_sha256,hash(&verdict()).unwrap());
    // A receipt cannot be attached to a different envelope or typed verdict.
    assert!(crate::cache_semantic_with_receipt(&context,&a,Ok(verdict()),Some(proof_b.clone())).is_err());
    let mut changed = verdict();changed.needs_human_review = 1.0;
    assert!(crate::cache_semantic_with_receipt(&context,&a,Ok(changed),Some(proof_a.clone())).is_err());
    crate::cache_semantic_with_receipt(&context,&b,Ok(verdict()),Some(proof_b.clone())).unwrap();
    // The first usage can disappear before its later cache transaction.
    transaction(&context,|state|{state.observations.clear();Ok(())}).unwrap();
    crate::cache_semantic_with_receipt(&context,&a,Ok(verdict()),Some(proof_a.clone())).unwrap();
    transaction(&context,|state|{
        for _ in 0..300 {crate::record(state,json!({"event":"unrelated"}));}
        Ok(())
    }).unwrap();
    let state = crate::read_private::<crate::State>(&context.state_path,crate::MAX_STATE).unwrap();
    assert_eq!(state.observations.len(),256);
    assert_eq!(state.semantic_cache[&a.message_id].review_receipt,Some(proof_a));
    assert_eq!(state.semantic_cache[&b.message_id].review_receipt,Some(proof_b));
    assert_eq!(state.semantic_cache[&a.message_id].verdict_sha256,Some(hash(&verdict()).unwrap()));
    let legacy = json!({"envelope_sha256":hash(&a).unwrap(),"result":{"Ok":verdict()}});
    let legacy: crate::CachedSemantic = serde_json::from_value(legacy).unwrap();
    assert!(legacy.review_receipt.is_none() && legacy.verdict_sha256.is_none());
}

#[test]
fn actual_http_review_propagates_one_message_bound_receipt_without_secret_data() {
    use std::{io::{Read,Write},net::TcpListener};
    let (_dir, context, model) = fixture();
    let envelope = message(&context,"Read-only scoped findings");
    let input=json!({"message":envelope});
    let message_sha256=semantic_message_sha256(SEMANTIC_INSTRUCTIONS,&input).unwrap();
    assert_eq!(message_sha256,Some(hash(&envelope).unwrap()));
    assert_ne!(message_sha256,Some(hash(&input["message"]).unwrap()));
    assert!(semantic_message_sha256(SUPERVISOR_INSTRUCTIONS,&input).unwrap().is_none());
    let body = request(&model,SEMANTIC_INSTRUCTIONS,&input).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let endpoint = format!("http://{}/review",listener.local_addr().unwrap());
    let worker = std::thread::spawn(move || {
        let (mut stream,_) = listener.accept().unwrap();
        stream.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let mut raw=Vec::new();
        loop {
            let mut chunk=[0;4096];let count=stream.read(&mut chunk).unwrap();assert!(count>0);raw.extend_from_slice(&chunk[..count]);
            if let Some(position)=raw.windows(4).position(|bytes|bytes==b"\r\n\r\n") {
                let header=String::from_utf8_lossy(&raw[..position]);
                let length=header.lines().find_map(|line|line.to_lowercase().strip_prefix("content-length: ").and_then(|value|value.parse::<usize>().ok())).unwrap();
                if raw.len()>=position+4+length {break;}
            }
        }
        let data=json!({"model":"fixture-model","choices":[{"finish_reason":"stop","message":{"content":raw_safe().to_string()}}],"usage":{"prompt_tokens":100,"completion_tokens":10}}).to_string();
        write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{data}",data.len()).unwrap();
    });
    let response=evaluate_request_at_bound(&context,&model,&body,&endpoint,"fixture-secret-never-log",message_sha256).unwrap();
    worker.join().unwrap();
    let proof=response.review_receipt.unwrap();
    let verdict:SemanticVerdict=serde_json::from_value(response.result).unwrap();
    crate::cache_semantic_with_receipt(&context,&envelope,Ok(verdict),Some(proof.clone())).unwrap();
    let state=crate::read_private::<crate::State>(&context.state_path,crate::MAX_STATE).unwrap();
    let reserved=state.observations.iter().find(|row|row["event"]=="review_reserved").unwrap();
    let usage=state.observations.iter().find(|row|row["event"]=="review_usage").unwrap();
    let cached=state.observations.iter().find(|row|row["event"]=="outbound_semantic").unwrap();
    assert_eq!(reserved["invocation_id"],proof.invocation_id);
    assert_eq!(reserved["request_sha256"],hash(&body).unwrap());
    assert_eq!(reserved["invocation_id"],usage["invocation_id"]);
    assert_eq!(usage["review_receipt"],cached["review_receipt"]);
    assert_eq!(cached["input_sha256"],proof.message_sha256.unwrap());
    let journal=std::fs::read_to_string(&context.state_path).unwrap();
    assert!(!journal.contains("fixture-secret-never-log"));
    assert!(!journal.contains("127.0.0.1"));
}
