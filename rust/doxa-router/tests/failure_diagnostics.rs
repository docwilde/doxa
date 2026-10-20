#![cfg(feature = "test-transport")]

use doxa_router::*;
use serde_json::{json, Value};
use std::{fs, io::{Read, Write}, net::TcpListener, os::unix::fs::OpenOptionsExt,
    sync::{Arc, atomic::{AtomicBool, AtomicUsize, Ordering}}, thread, time::{Duration, Instant}};

const SECRET: &str = "synthetic-private-response-do-not-retain";
const KEY: &str = "synthetic-router-bearer-do-not-retain";

fn config() -> Config { serde_json::from_str(include_str!("../fixtures/config.example.json")).unwrap() }
fn input() -> RoutingInput {
    RoutingInput { summary:"Format a synthetic mapping as JSON".into(), estimated_input_tokens:300,
        max_output_tokens:500, requires_tools:true, allowed_candidate_ids:vec!["fast".into(),"deliberate".into()] }
}

enum Reply {
    Http { status:u16, body:Vec<u8>, chunked:bool, delay:Duration },
    Close,
}

/// Real loopback requests, no injected reqwest results. Keep accepting briefly
/// after the first reply so a retry would be observed, not hidden by teardown.
fn server(reply:Reply) -> (String, Arc<AtomicUsize>, thread::JoinHandle<()>) {
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let endpoint=format!("http://{}/v1/systemone",listener.local_addr().unwrap());
    let calls=Arc::new(AtomicUsize::new(0));let observed=calls.clone();
    let handle=thread::spawn(move || {
        let mut deadline=Instant::now()+Duration::from_secs(3);
        loop {
            let (mut stream,_)=match listener.accept() {
                Ok(connection)=>connection,
                Err(error) if error.kind()==std::io::ErrorKind::WouldBlock=>{
                    if Instant::now()>=deadline { break; }
                    thread::sleep(Duration::from_millis(2));continue;
                },
                Err(error)=>panic!("local fixture accept failed: {}",error.kind()),
            };
            observed.fetch_add(1,Ordering::AcqRel);
            stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            let mut request=Vec::new();
            loop {
                let mut chunk=[0;4096];let count=stream.read(&mut chunk).unwrap();
                assert!(count>0);request.extend_from_slice(&chunk[..count]);
                assert!(request.len()<32*1024);
                if let Some(offset)=request.windows(4).position(|part|part==b"\r\n\r\n") {
                    let header=String::from_utf8_lossy(&request[..offset]).to_ascii_lowercase();
                    let length=header.lines().find_map(|line|line.strip_prefix("content-length: ")?.parse::<usize>().ok()).unwrap();
                    if request.len()>=offset+4+length { break; }
                }
            }
            if let Reply::Http { status,body,chunked,delay }=&reply {
                thread::sleep(*delay);
                let header=if *chunked {"Transfer-Encoding: chunked".into()}
                    else {format!("Content-Length: {}",body.len())};
                let _=write!(stream,"HTTP/1.1 {status} Fixture\r\n{header}\r\nX-Fixture-Private: {SECRET}\r\nConnection: close\r\n\r\n");
                if *chunked {
                    for chunk in body.chunks(4096) {
                        if write!(stream,"{:x}\r\n",chunk.len()).and_then(|_|stream.write_all(chunk))
                            .and_then(|_|stream.write_all(b"\r\n")).is_err() { break; }
                    }
                    let _=stream.write_all(b"0\r\n\r\n");
                } else { let _=stream.write_all(body); }
            }
            deadline=Instant::now()+Duration::from_millis(150);
        }
        assert!(observed.load(Ordering::Acquire)>0,"fixture received no request");
    });
    (endpoint,calls,handle)
}

fn checked_failure(reply:Reply, category:FailureCategory, reason:Reason, timeout:bool) {
    let mut cfg=config();if timeout {cfg.deadline_ms=100;}
    let prepared=prepare(&cfg,&input()).unwrap();let mut ledger=Ledger::default();
    let hold=reserve(&cfg,&mut ledger,&prepared).unwrap();
    let held_before=ledger.held_usd_micros();
    let (endpoint,calls,server)=server(reply);
    let outcome=call_at(&cfg,&prepared,KEY,&AtomicBool::new(false),&endpoint);
    server.join().unwrap();
    assert_eq!(calls.load(Ordering::Acquire),1,"failed routing request was retried");
    assert_eq!(outcome.reason,reason);assert_eq!(outcome.failure_category,Some(category));
    assert_eq!(outcome.candidate_id,cfg.fallback_id);assert!(outcome.attempted);
    assert!(outcome.verified_response.is_none() && outcome.usage.is_none());
    assert!(outcome.router_cost_usd_micros.is_none());
    settle(&mut ledger,&hold,&outcome).unwrap();
    assert!(ledger.accounting_unknown);assert_eq!(ledger.calls,1);
    assert_eq!(ledger.held_usd_micros(),held_before);
    assert!(reserve(&cfg,&mut ledger,&prepared).is_err());

    // Exercise the same closed serde shapes used by the evaluator journal.
    let directory=tempfile::tempdir().unwrap();let path=directory.path().join("journal.jsonl");
    let mut file=fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(&path).unwrap();
    writeln!(file,"{}",json!({"event":"reserved","ledger":{"calls":1,"reserved_usd_micros":held_before,
        "actual_usd_micros":0,"accounting_unknown":false,"last_settled_call":0},"reservation":hold})).unwrap();
    writeln!(file,"{}",json!({"event":"outcome","ledger":ledger,"reservation":hold,"outcome":outcome})).unwrap();
    file.sync_all().unwrap();
    let journal=read_private(&path,64*1024).unwrap();
    for private in [SECRET,KEY,&endpoint,"X-Fixture-Private","reqwest"] {
        assert!(!journal.contains(private),"untrusted diagnostic escaped into journal");
    }
    let rows=journal.lines().map(|line|serde_json::from_str::<Value>(line).unwrap()).collect::<Vec<_>>();
    let mut restored:Ledger=serde_json::from_value(rows[1]["ledger"].clone()).unwrap();
    let restored_outcome:Outcome=serde_json::from_value(rows[1]["outcome"].clone()).unwrap();
    assert_eq!(restored_outcome.failure_category,Some(category));
    assert!(restored.accounting_unknown);assert_eq!(restored.held_usd_micros(),held_before);
    assert!(reserve(&cfg,&mut restored,&prepared).is_err(),"restart cleared unknown accounting");
    let mut unsettled:Ledger=serde_json::from_value(rows[0]["ledger"].clone()).unwrap();
    assert!(reserve(&cfg,&mut unsettled,&prepared).is_err(),"unsettled journal admitted a replay");
    assert_eq!(calls.load(Ordering::Acquire),1);
}

#[test]
fn http_status_diagnostics_do_not_read_private_error_bodies_or_release_holds() {
    for (status,category) in [(401,FailureCategory::Authentication),(403,FailureCategory::Authentication),
        (402,FailureCategory::PaymentRequired),(429,FailureCategory::RateLimited),
        (503,FailureCategory::Overloaded),(418,FailureCategory::OtherHttp)] {
        checked_failure(Reply::Http {status,body:SECRET.as_bytes().to_vec(),chunked:false,delay:Duration::ZERO},
            category,Reason::Unavailable,false);
    }
}

#[test]
fn invalid_json_schema_and_declared_or_streamed_oversize_remain_unknown_and_private() {
    for (body,chunked,category) in [
        (format!("{{private:{SECRET}}}").into_bytes(),false,FailureCategory::InvalidJson),
        (format!("{{\"private\":\"{SECRET}\",\"private\":0}}").into_bytes(),false,FailureCategory::InvalidJson),
        (json!({"private":SECRET}).to_string().into_bytes(),false,FailureCategory::InvalidSchema),
        (SECRET.repeat(3000).into_bytes(),false,FailureCategory::ResponseTooLarge),
        (SECRET.repeat(3000).into_bytes(),true,FailureCategory::ResponseTooLarge),
    ] {
        checked_failure(Reply::Http {status:200,body,chunked,delay:Duration::ZERO},
            category,Reason::InvalidResponse,false);
    }
}

#[test]
fn timeout_and_dropped_transport_have_closed_diagnostics_without_retries() {
    checked_failure(Reply::Http {status:200,body:b"{}".to_vec(),chunked:false,delay:Duration::from_millis(250)},
        FailureCategory::Timeout,Reason::Unavailable,true);
    checked_failure(Reply::Close,FailureCategory::Transport,Reason::Unavailable,false);
}

#[test]
fn absent_diagnostics_remain_backward_compatible_and_nonattempts_have_none() {
    let cfg=config();let prepared=prepare(&cfg,&input()).unwrap();
    let outcome=call_at(&cfg,&prepared,KEY,&AtomicBool::new(true),"http://127.0.0.1:1/v1/systemone");
    assert_eq!(outcome.reason,Reason::Cancelled);assert!(!outcome.attempted);
    assert!(outcome.failure_category.is_none());
    let old=serde_json::to_value(&outcome).unwrap();assert!(old.get("failure_category").is_none());
    let restored:Outcome=serde_json::from_value(old.clone()).unwrap();assert_eq!(outcome,restored);
    let mut unknown=old;unknown["failure_category"]=json!(SECRET);
    assert!(serde_json::from_value::<Outcome>(unknown).is_err());
}
