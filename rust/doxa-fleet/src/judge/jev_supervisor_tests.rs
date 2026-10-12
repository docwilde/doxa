use super::*;
use crate::{Alignment, Assignment, Charter, Mode, ReviewConfig, State};
use std::{io::{Read, Write}, net::TcpListener, os::unix::fs::PermissionsExt};

fn fixture(mode: Mode) -> (tempfile::TempDir, Context, Value) {
    let dir = tempfile::tempdir().unwrap();
    std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let charter = Charter { version:1,fleet_id:"jev-fixture".into(),task:"Implement the bounded parser change".into(),
        repo:"/fixture".into(),allowed_paths:vec!["src".into()],required_evidence:vec!["passing host test receipt".into()],
        worker_limit:1,run_budget_usd:Some(10.0),deadline:0,human_actions:vec!["dependency release".into()],test_recipe:None };
    let context = Context { charter_sha256:hash(&charter).unwrap(),charter,
        assignments:vec![Assignment {id:"assignment".into(),session_id:"worker".into(),pid:1,role:"worker".into(),
            task:"Implement the bounded parser change".into(),cwd:"/fixture".into(),base_commit:None,allowed_paths:vec![],depends_on:vec![]}],
        review:ReviewConfig {supervisor:Some(Model::parse("jev:jev-1.13.0").unwrap()),supervisor_mode:mode,
            budget_usd:0.1,max_calls:10,risk_threshold:0.75,..Default::default()},state_path:dir.path().join("state.json") };
    context.validate().unwrap();
    let artifact = json!({"kind":"host_checkpoint","session_id":"worker","assignment_id":"assignment",
        "changed_paths":"src/parser.rs\n","git_observation_available":true,"tests_verified":false,"running":false,"queued":0});
    transaction(&context,|state| { state.artifacts.insert("host-checkpoint".into(),artifact.clone());Ok(()) }).unwrap();
    let snapshot = json!({"charter":context.charter,"assignments":context.assignments,
        "artifacts":[{"id":"host-checkpoint","evidence":artifact}],"budget":{"review_reserved_usd":0.0},
        "guard_observations":[],"recent_handoff_evidence":[],"phase":"monitoring"});
    (dir,context,snapshot)
}
fn supervisor_request(context: &Context, snapshot: &Value) -> Value {
    request_with_threshold(context.review.supervisor.as_ref().unwrap(),SUPERVISOR_INSTRUCTIONS,snapshot,context.review.risk_threshold).unwrap()
}
fn response(body: &Value, alignment: &str, basis: &str, evidence: &str) -> Value {
    let choices = [("alignment",alignment),("basis",basis),("evidence",evidence)];
    let answers = choices.into_iter().map(|(name,choice)| {
        let probabilities = body["questions"][name]["criteria"].as_object().unwrap().keys()
            .map(|id|(id.clone(),json!(if id == choice {1.0} else {0.0}))).collect::<serde_json::Map<_,_>>();
        (name.to_owned(),json!({"type":"choice","choice":choice,"probabilities":probabilities,"confidence":1.0}))
    }).collect::<serde_json::Map<_,_>>();
    json!({"model":"jev-1.13.0","answers":answers,"usage":{"input_tokens":100,"output_tokens":32}})
}
fn server(reply: String, status: u16) -> (String, std::thread::JoinHandle<Value>) {
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let endpoint=format!("http://{}/v1/systemone",listener.local_addr().unwrap());
    let thread=std::thread::spawn(move|| {
        let deadline=Instant::now()+Duration::from_secs(5);
        let mut stream=loop { match listener.accept() {
            Ok((stream,_))=>break stream,
            Err(err) if err.kind()==io::ErrorKind::WouldBlock && Instant::now()<deadline=>std::thread::sleep(Duration::from_millis(5)),
            Err(err)=>panic!("bounded synthetic server accept failed: {err}"),
        }};
        // Darwin can inherit O_NONBLOCK from the listener, unlike Linux.
        // Read/write timeouts do not turn a nonblocking socket into a blocking
        // one: reset the accepted stream before bounded request reads.
        stream.set_nonblocking(false).unwrap();
        stream.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        stream.set_write_timeout(Some(Duration::from_secs(3))).unwrap();
        let mut bytes=Vec::new();
        let request=loop {
            let mut chunk=[0;4096];let count=stream.read(&mut chunk).unwrap();assert!(count>0);
            bytes.extend_from_slice(&chunk[..count]);assert!(bytes.len()<=MAX_INPUT+8_000);
            if let Some(position)=bytes.windows(4).position(|window|window==b"\r\n\r\n") {
                let header=String::from_utf8_lossy(&bytes[..position]).to_ascii_lowercase();
                let length=header.lines().find_map(|line|line.strip_prefix("content-length: ").and_then(|value|value.parse::<usize>().ok())).unwrap();
                if bytes.len()>=position+4+length {
                    assert!(header.contains("authorization: bearer fixture-key"));
                    break serde_json::from_slice::<Value>(&bytes[position+4..position+4+length]).unwrap();
                }
            }
        };
        // A non-success client returns after headers and may close immediately.
        // Buffer the whole reply so fragmented formatting writes do not race
        // that expected completion between the status line and body.
        let wire=format!("HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{reply}",reply.len());
        stream.write_all(wire.as_bytes()).unwrap();
        request
    });
    (endpoint,thread)
}
fn evaluate_mock(context: &Context, body: &Value, reply: String, status: u16) -> (io::Result<Response>, Value) {
    let (endpoint,thread)=server(reply,status);
    let result=evaluate_request_at(context,context.review.supervisor.as_ref().unwrap(),body,&endpoint,"fixture-key");
    (result,thread.join().unwrap())
}
fn state(context: &Context) -> State { transaction(context,|state|Ok(state.clone())).unwrap() }

#[test]
fn jev_checkpoint_http_uses_alignment_choices_and_shared_known_usage_in_both_modes() {
    for mode in [Mode::Shadow,Mode::Enforce] {
        for (alignment,basis) in [("aligned","within_charter"),("uncertain","unknown"),("drifted","task_scope"),("blocked","evidence")] {
            let (_dir,context,snapshot)=fixture(mode);let body=supervisor_request(&context,&snapshot);
            let (result,sent)=evaluate_mock(&context,&body,response(&body,alignment,basis,"host-checkpoint").to_string(),200);
            let result=result.unwrap();
            assert_eq!(sent,body);assert_eq!(sent["state"],snapshot);assert_eq!(sent["model"],"jev-1.13.0");
            assert_eq!(sent["questions"].as_object().unwrap().len(),3);
            assert!(sent["questions"].as_object().unwrap().values().all(|question|question["type"]=="choice"));
            assert!(sent.get("tools").is_none() && sent.get("conversation").is_none());
            assert!(sent["questions"].get("within_assignment").is_none(),"supervision is not message Noul classification");
            assert_eq!(sent["questions"]["alignment"]["instructions"]["minimum_confidence"],0.75);
            let verdict:SupervisorVerdict=serde_json::from_value(result.result).unwrap();
            let expected:Alignment=serde_json::from_value(json!(alignment)).unwrap();
            assert_eq!(verdict.verdict,expected);assert_eq!(verdict.evidence_refs,vec!["host-checkpoint"]);
            let state=crate::apply_supervisor(&context,Ok(verdict)).unwrap();
            assert_eq!(state.paused,mode==Mode::Enforce && alignment!="aligned");
            assert!(!state.accounting_unknown);assert_eq!(state.calls,1);
            assert!((state.actual_estimated_usd-0.0000042).abs()<1e-12);
            assert!(state.reserved_usd>state.actual_estimated_usd);
            assert_eq!(state.observations.iter().filter(|row|row["event"]=="review_reserved").count(),1);
            assert_eq!(state.observations.iter().filter(|row|row["event"]=="review_usage").count(),1);
        }
    }
}

#[test]
fn jev_low_confidence_contradictory_uncited_and_empty_snapshot_decisions_are_uncertain() {
    let (_dir,context,snapshot)=fixture(Mode::Enforce);let body=supervisor_request(&context,&snapshot);
    for (alignment,basis,evidence) in [("drifted","task_scope","none"),("blocked","evidence","none"),
        ("aligned","authority","host-checkpoint"),("aligned","within_charter","none")] {
        let raw=response(&body,alignment,basis,evidence);
        let verdict=decision_supervisor::result(context.review.supervisor.as_ref().unwrap(),&raw,&body).unwrap();
        assert_eq!(verdict["verdict"],"uncertain");
        assert!(verdict["reason"].as_str().unwrap().contains("human review"));
    }
    let mut raw=response(&body,"aligned","within_charter","host-checkpoint");
    raw["answers"]["alignment"]["probabilities"]=json!({"aligned":0.25,"uncertain":0.25,"drifted":0.25,"blocked":0.25});
    raw["answers"]["alignment"]["confidence"]=json!(0.0);
    let (result,_)=evaluate_mock(&context,&body,raw.to_string(),200);
    let result=result.unwrap();assert_eq!(result.result["verdict"],"uncertain");
    assert_eq!(crate::apply_supervisor(&context,serde_json::from_value(result.result).map_err(|_|"fixture".into())).unwrap().supervisor_status,"uncertain");
    assert!(!state(&context).accounting_unknown,"uncertainty with verified usage has a known charge subtotal");
    let mut empty=snapshot;empty["artifacts"]=json!([]);let empty_body=supervisor_request(&context,&empty);
    assert_eq!(decision_supervisor::result(context.review.supervisor.as_ref().unwrap(),&response(&empty_body,"aligned","within_charter","none"),&empty_body).unwrap()["verdict"],"uncertain");
    let mut zero_guard=empty_body.clone();zero_guard["questions"]["alignment"]["instructions"]["minimum_confidence"]=json!(0.0);
    let mut tied=response(&zero_guard,"aligned","within_charter","none");
    tied["answers"]["alignment"]["probabilities"]=json!({"aligned":0.5,"uncertain":0.5,"drifted":0.0,"blocked":0.0});
    tied["answers"]["alignment"]["confidence"]=json!(1.0/3.0);
    assert_eq!(decision_supervisor::result(context.review.supervisor.as_ref().unwrap(),&tied,&zero_guard).unwrap()["verdict"],"uncertain");
}

#[test]
fn jev_malformed_outage_and_unsupplied_citations_retain_unknown_reservations() {
    for case in 0..11 {
        let (_dir,context,snapshot)=fixture(Mode::Shadow);let body=supervisor_request(&context,&snapshot);
        let mut raw=response(&body,"drifted","task_scope","host-checkpoint");
        match case {
            0=>raw["model"]=json!("jev-1.13.0-INJECTED"),
            1=>raw["usage"]=json!({"input_tokens":null,"output_tokens":0}),
            2=>raw["answers"]["evidence"]["choice"]=json!("PRIVATE-INJECTED-SERVER"),
            3=>raw["answers"]["alignment"]["probabilities"]["aligned"]=json!(0.5),
            4=>raw["answers"]["alignment"]["confidence"]=json!(0.0),
            5=>raw["answers"]["alignment"]["tool_calls"]=json!([]),
            6=>{raw["answers"].as_object_mut().unwrap().remove("basis");},
            7=>raw["answers"]["alignment"]["choice"]=Value::Null,
            8=>{raw["answers"]["alignment"].as_object_mut().unwrap().remove("choice");raw["answers"]["alignment"]["unknown"]=json!("aligned");},
            _=>{},
        }
        let reply=if case==9 {raw.to_string().replacen("\"model\":", "\"model\":\"jev-1.13.0\",\"model\":",1)} else {raw.to_string()};
        let (result,_)=evaluate_mock(&context,&body,reply,if case==10 {503} else {200});
        let error=result.unwrap_err();assert!(!error.to_string().contains("PRIVATE-INJECTED-SERVER"));
        if case==10 {assert_eq!(error.to_string(),"independent review service refused","fixture must deliver the HTTP outage status");}
        let state=state(&context);assert!(state.accounting_unknown && state.paused,"case {case}");
        assert_eq!(state.calls,1);assert!(state.reserved_usd>0.0);assert_eq!(state.actual_estimated_usd,0.0);
        assert!(crate::resume_review(&context,&context.charter_sha256).is_err());
    }
}

#[test]
fn decision_supervisor_budget_and_call_refusals_never_open_http() {
    for calls_exhausted in [false,true] {
        let (_dir,mut context,snapshot)=fixture(Mode::Enforce);
        if calls_exhausted {context.review.max_calls=1;transaction(&context,|state|{state.calls=1;Ok(())}).unwrap();}
        else {context.review.budget_usd=0.00000001;}
        let body=supervisor_request(&context,&snapshot);
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
        let endpoint=format!("http://{}/v1/systemone",listener.local_addr().unwrap());
        assert!(evaluate_request_at(&context,context.review.supervisor.as_ref().unwrap(),&body,&endpoint,"fixture-key").is_err());
        assert_eq!(listener.accept().unwrap_err().kind(),io::ErrorKind::WouldBlock);
        let state=state(&context);assert_eq!(state.calls,u64::from(calls_exhausted));assert_eq!(state.reserved_usd,0.0);
        assert!(!state.accounting_unknown && state.paused);
    }
}

#[test]
fn decision_supervisor_rejects_aliases_nonhost_artifacts_and_context_overflow_before_reservation() {
    let (_dir,context,mut snapshot)=fixture(Mode::Enforce);
    for model in ["jev:jev-latest","jev:jev-preview","jev:jev-1.13.0-new"] {
        let mut review=context.review.clone();review.supervisor=Some(Model::parse(model).unwrap());assert!(review.validate().is_err());
    }
    snapshot["artifacts"][0]["id"]=json!("invented-host");
    assert!(supervise(&context,&snapshot).unwrap_err().contains("non-host evidence"));
    assert_eq!(state(&context).calls,0);
    snapshot["artifacts"]=json!([]);snapshot["worker_prose"]=json!("x".repeat(32_000));
    assert!(request(context.review.supervisor.as_ref().unwrap(),SUPERVISOR_INSTRUCTIONS,&snapshot).is_err());
    assert_eq!(state(&context).reserved_usd,0.0);
    assert!(ReviewConfig::default().supervisor.is_none());
}

#[test]
fn snap_supervision_uses_frozen_choice_evidence_and_budget_with_local_http() {
    let (_dir,mut context,snapshot)=fixture(Mode::Enforce);
    context.review.supervisor=Some(Model::parse("subq:subq-snap-preview").unwrap());
    context.validate().unwrap();
    let body=supervisor_request(&context,&snapshot);
    assert_eq!(body["model"],"subq-snap-preview");
    assert!(body["questions"]["alignment"]["instructions"].is_string());
    let mut wire=response(&body,"drifted","task_scope","host-checkpoint");
    wire["model"]=json!("subq-snap-preview");wire["usage"]["output_tokens"]=json!(0);
    for answer in wire["answers"].as_object_mut().unwrap().values_mut() { answer["confidence"]=json!(0.95); }
    let (endpoint,thread)=server(wire.to_string(),200);
    let result=evaluate_request_at(&context,context.review.supervisor.as_ref().unwrap(),&body,&endpoint,"fixture-key").unwrap();
    assert_eq!(thread.join().unwrap(),body);
    assert_eq!(result.result["verdict"],"drifted");assert_eq!(result.result["evidence_refs"],json!(["host-checkpoint"]));
    let state=crate::read_private::<State>(&context.state_path,crate::MAX_STATE).unwrap();
    assert_eq!(state.calls,1);assert!(!state.accounting_unknown);
    assert!((state.actual_estimated_usd-0.000005).abs()<1e-10);
    let model=context.review.supervisor.as_ref().unwrap();
    let mut unsupported=response(&body,"aligned","within_charter","no_support");
    unsupported["model"]=json!("subq-snap-preview");unsupported["usage"]["output_tokens"]=json!(0);
    assert_eq!(parse_response_for_request(model,&unsupported,1,Some(&body)).unwrap().result["verdict"],"uncertain");
    let mut empty=snapshot;empty["artifacts"]=json!([]);
    let body=supervisor_request(&context,&empty);
    assert_eq!(body["questions"]["evidence"]["criteria"].as_object().unwrap().len(),2);
    assert!(Model::parse("subq:subq-preview").is_err());
}

#[test]
fn snap_outages_keep_reserved_spend_and_enforced_review_pause() {
    for status in [401,402,429,503] {
        let (_dir,mut context,snapshot)=fixture(Mode::Enforce);
        context.review.supervisor=Some(Model::parse("subq:subq-snap-preview").unwrap());
        let body=supervisor_request(&context,&snapshot);
        let (endpoint,thread)=server(json!({"detail":{"error_type":"fixture_error","message":"untrusted provider text"}}).to_string(),status);
        assert!(evaluate_request_at(&context,context.review.supervisor.as_ref().unwrap(),&body,&endpoint,"fixture-key").is_err());
        assert_eq!(thread.join().unwrap(),body);
        let state=crate::read_private::<State>(&context.state_path,crate::MAX_STATE).unwrap();
        assert!(state.reserved_usd>0.0);assert_eq!(state.calls,1);assert!(state.accounting_unknown && state.paused);
        assert!(!serde_json::to_string(&state).unwrap().contains("untrusted provider text"));
    }
}
