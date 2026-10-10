use doxa_router::*;
use serde_json::{json,Value};
use std::{fs,os::unix::fs::PermissionsExt,sync::atomic::AtomicBool};

fn config()->Config {serde_json::from_str(include_str!("../fixtures/config.example.json")).unwrap()}
fn input()->RoutingInput {RoutingInput {summary:"Format a synthetic mapping as JSON".into(),estimated_input_tokens:300,max_output_tokens:500,requires_tools:true,allowed_candidate_ids:vec!["fast".into(),"deliberate".into()]}}
fn response()->Value {json!({"model":JEV_MODEL,"answers":{"target":{"type":"choice","choice":"fast","probabilities":{"fast":0.9,"deliberate":0.1},"confidence":0.8}},"usage":{"input_tokens":100,"output_tokens":20}})}

#[test]
fn deterministic_eligibility_cost_and_fallback_never_enlarge_candidate_set() {
    let cfg=config();let mut request=input();let prepared=prepare(&cfg,&request).unwrap();
    assert_eq!(prepared.eligible_ids,["fast","deliberate"]);
    assert_eq!(candidate_cost(&cfg.candidates[0],&request).unwrap(),690);
    assert_eq!(token_cost(1,0,1,0).unwrap(),1);
    request.allowed_candidate_ids=vec!["deliberate".into()];
    let only=prepare(&cfg,&request).unwrap();
    let out=call(&cfg,&only,"fixture-key",&AtomicBool::new(false));
    assert_eq!(out.reason,Reason::SingleEligible);assert!(!out.attempted);
    request.allowed_candidate_ids=vec!["fast".into()];assert!(prepare(&cfg,&request).is_err());
    request.allowed_candidate_ids=vec!["deliberate".into(),"invented".into()];assert!(prepare(&cfg,&request).is_err());
    request=input();request.max_output_tokens=1001;assert!(prepare(&cfg,&request).is_err());
    request=input();request.estimated_input_tokens=u64::MAX;assert!(prepare(&cfg,&request).is_err());
    let mut narrow=cfg.clone();narrow.candidates[0].supports_tools=false;
    assert_eq!(prepare(&narrow,&input()).unwrap().eligible_ids,["deliberate"]);
    assert!(token_cost(u64::MAX,u64::MAX,u64::MAX,u64::MAX).is_err());
}

#[test]
fn strict_choice_and_known_usage_survive_low_confidence_or_invalid_answer() {
    let cfg=config();let prepared=prepare(&cfg,&input()).unwrap();
    let good=parse_response(&cfg,&prepared,&response(),12);
    assert_eq!(good.candidate_id,"fast");assert_eq!(good.reason,Reason::Selected);
    assert_eq!(good.router_cost_usd_micros,Some(5));assert!(good.verified_response.is_some());
    let mut uncertain=response();uncertain["answers"]["target"]["probabilities"]=json!({"fast":0.55,"deliberate":0.45});uncertain["answers"]["target"]["confidence"]=json!(0.1);
    let out=parse_response(&cfg,&prepared,&uncertain,20);assert_eq!(out.reason,Reason::LowConfidence);assert_eq!(out.candidate_id,"deliberate");assert_eq!(out.router_cost_usd_micros,Some(5));
    for (pointer,value) in [
        ("/model",json!("jev-latest")),("/answers/target/choice",json!("unknown")),
        ("/answers/target/confidence",json!(0.1)),("/answers/target/probabilities/fast",json!(0.1)),
        ("/answers/target/probabilities/unknown",json!(0.0)),("/answers/target/type",json!("noul")),
        ("/usage/input_tokens",json!(null)),("/usage/input_tokens",json!(0)),
    ] {
        let mut bad=response();
        if pointer.ends_with("/unknown") {bad["answers"]["target"]["probabilities"]["unknown"]=value;}else{*bad.pointer_mut(pointer).unwrap()=value;}
        let out=parse_response(&cfg,&prepared,&bad,1);
        assert_eq!(out.reason,Reason::InvalidResponse,"{pointer}");assert_eq!(out.candidate_id,"deliberate");assert!(out.verified_response.is_none());
        if pointer=="/model" {assert!(out.model.is_none());}
    }
    let mut bad=response();bad["answers"]["target"]["unexpected"]=json!("untrusted instructions");
    let out=parse_response(&cfg,&prepared,&bad,1);assert_eq!(out.usage.as_ref().unwrap().input_tokens,100);assert_eq!(out.router_cost_usd_micros,Some(5));
    assert!(!serde_json::to_string(&out).unwrap().contains("untrusted instructions"));
    let mut bad=response();bad["model"]=json!("\u{1b}]777;notify;secret\u{7}");
    assert!(parse_response(&cfg,&prepared,&bad,1).model.is_none());
}

#[test]
fn durable_reservations_bound_calls_spend_replays_and_unknown_accounting() {
    let mut cfg=config();cfg.max_calls=1;
    let prepared=prepare(&cfg,&input()).unwrap();let mut ledger=Ledger::default();
    let reservation=reserve(&cfg,&mut ledger,&prepared).unwrap();
    assert!(reserve(&cfg,&mut ledger,&prepared).is_err());
    let out=parse_response(&cfg,&prepared,&response(),1);
    settle(&mut ledger,&reservation,&out).unwrap();
    assert_eq!(ledger.actual_usd_micros,5);assert_eq!(ledger.held_usd_micros(),reservation.usd_micros);
    assert!(settle(&mut ledger,&reservation,&out).is_err());assert!(reserve(&cfg,&mut ledger,&prepared).is_err());
    let cfg=config();let prepared=prepare(&cfg,&input()).unwrap();let mut ledger=Ledger::default();let hold=reserve(&cfg,&mut ledger,&prepared).unwrap();
    let mut unknown=fallback(&cfg,&prepared,Reason::Unavailable);unknown.attempted=true;unknown.router_cost_usd_micros=None;
    settle(&mut ledger,&hold,&unknown).unwrap();assert!(ledger.accounting_unknown);assert!(reserve(&cfg,&mut ledger,&prepared).is_err());
    let mut cfg=config();cfg.max_spend_usd_micros=1;let prepared=prepare(&cfg,&input()).unwrap();assert!(reserve(&cfg,&mut Ledger::default(),&prepared).is_err());
}

#[test]
fn malformed_zero_unknown_duplicate_and_private_config_inputs_are_refused() {
    let cfg=config();cfg.validate().unwrap();
    let value=serde_json::to_value(&cfg).unwrap();
    for (pointer,new) in [("/jev_model",json!("jev-latest")),("/candidates/0/input_usd_micros_per_million",json!(0)),
        ("/candidates/0/context_tokens",json!(0)),("/candidates/0/id",json!("deliberate")),
        ("/candidates/0/id",json!("auto")),
        ("/confidence_threshold",json!(null)),("/max_calls",json!(0)),("/deadline_ms",json!(12001)),("/candidates/0/description",json!(""))] {
        let mut bad=value.clone();*bad.pointer_mut(pointer).unwrap()=new;
        assert!(serde_json::from_value::<Config>(bad).and_then(|cfg|cfg.validate().map_err(serde::de::Error::custom)).is_err(),"{pointer}");
    }
    let root=tempfile::tempdir().unwrap();let path=root.path().join("router.json");fs::write(&path,serde_json::to_string(&cfg).unwrap()).unwrap();
    assert!(Config::load(&path).is_err());fs::set_permissions(&path,fs::Permissions::from_mode(0o600)).unwrap();assert!(Config::load(&path).is_ok());
    let link=root.path().join("link");std::os::unix::fs::symlink(&path,&link).unwrap();assert!(Config::load(&link).is_err());
    let mut bad=value.clone();bad["unknown"]=json!(true);fs::write(&path,bad.to_string()).unwrap();assert!(Config::load(&path).is_err());
    fs::write(&path,serde_json::to_string(&cfg).unwrap().replacen("\"version\":1","\"version\":1,\"version\":1",1)).unwrap();assert!(Config::load(&path).is_err());
}

#[cfg(feature="test-transport")]
mod http {
    use super::*;
    use std::{io::{Read,Write},net::TcpListener,thread,time::{Duration,Instant},sync::{Arc,atomic::Ordering}};
    fn mock_server(body: String,delay:Duration)->(String,thread::JoinHandle<()>) {
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();let endpoint=format!("http://{}/v1/systemone",listener.local_addr().unwrap());
        let thread=thread::spawn(move|| {
            let (mut stream,_)=listener.accept().unwrap();stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            let mut raw=Vec::new();loop {let mut chunk=[0;4096];let n=stream.read(&mut chunk).unwrap();if n==0{return;}raw.extend_from_slice(&chunk[..n]);
                if let Some(position)=raw.windows(4).position(|part|part==b"\r\n\r\n") {
                    let head=String::from_utf8_lossy(&raw[..position]);let len=head.lines().find_map(|line|line.to_lowercase().strip_prefix("content-length: ").and_then(|n|n.parse::<usize>().ok())).unwrap();
                    if raw.len()>=position+4+len {let request:Value=serde_json::from_slice(&raw[position+4..position+4+len]).unwrap();assert_eq!(request["model"],JEV_MODEL);assert_eq!(request["questions"]["target"]["type"],"choice");break;}
                }
            }
            thread::sleep(delay);let _=write!(stream,"HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",body.len(),body);
        });(endpoint,thread)
    }
    #[test]
    fn actual_choice_http_transport_is_strict_and_bounded() {
        let cfg=config();let prepared=prepare(&cfg,&input()).unwrap();
        for body in [response().to_string(),response().to_string().replacen("\"model\":", "\"model\":\"jev-1.13.0\",\"model\":",1),"x".repeat(65537)] {
            let valid=body==response().to_string();let (endpoint,server)=mock_server(body,Duration::ZERO);
            let result=call_at(&cfg,&prepared,"fixture-key",&AtomicBool::new(false),&endpoint);server.join().unwrap();
            assert_eq!(result.reason,if valid{Reason::Selected}else{Reason::InvalidResponse});
            if !valid {assert!(result.verified_response.is_none());}
        }
        let out=call_at(&cfg,&prepared,"fixture-key",&AtomicBool::new(false),"http://example.com/v1/systemone");assert!(!out.attempted);
    }
    #[test]
    fn in_flight_cancel_and_deadline_return_without_worker_authority() {
        let mut cfg=config();cfg.deadline_ms=100;let prepared=prepare(&cfg,&input()).unwrap();
        let (endpoint,server)=mock_server(response().to_string(),Duration::from_millis(300));let started=Instant::now();
        let out=call_at(&cfg,&prepared,"fixture-key",&AtomicBool::new(false),&endpoint);assert_eq!(out.reason,Reason::Unavailable);assert!(started.elapsed()<Duration::from_millis(250));server.join().unwrap();
        cfg.deadline_ms=2000;let prepared=prepare(&cfg,&input()).unwrap();let (endpoint,server)=mock_server(response().to_string(),Duration::from_millis(300));
        let flag=Arc::new(AtomicBool::new(false));let other=flag.clone();let stop=thread::spawn(move||{thread::sleep(Duration::from_millis(50));other.store(true,Ordering::Release);});
        let started=Instant::now();let out=call_at(&cfg,&prepared,"fixture-key",&flag,&endpoint);stop.join().unwrap();assert_eq!(out.reason,Reason::Cancelled);assert!(out.attempted);assert!(out.router_cost_usd_micros.is_none());assert!(started.elapsed()<Duration::from_millis(200));server.join().unwrap();
    }
}
