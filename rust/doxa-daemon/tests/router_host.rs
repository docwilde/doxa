//! Actual loopback catalog/Jev/provider transports through the production host.
#[path = "../src/agent_tools.rs"] mod agent_tools;
#[path = "../src/budget_host.rs"] mod budget_host;
#[path = "../src/vendor_host.rs"] mod vendor_host;
#[path = "../src/vendor_tools.rs"] mod vendor_tools;
#[path = "../src/router_host.rs"] mod router_host;
mod peer_host { pub const PEER_TURN_MARKER: &str = "[PEER-STARTED TURN]"; }
fn iso_now() -> String { "2026-10-10T23:00:00Z".into() }

#[cfg(feature = "local-test-server")]
mod tests {
    use super::router_host::RouterHost;
    use doxa_runtime::Host;
    use doxa_router::{Candidate, Config, Provider};
    use serde_json::{json, Value};
    use std::{fs, io::{Read, Write}, net::TcpListener, path::PathBuf,
        sync::{Arc, Mutex, OnceLock, atomic::{AtomicBool, Ordering}}, time::{Duration, Instant}};

    struct Env { _lock:std::sync::MutexGuard<'static,()>, root:tempfile::TempDir, previous:Vec<(&'static str,Option<std::ffi::OsString>)> }
    impl Env {
        fn new() -> Self {
            static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
            let lock = LOCK.get_or_init(||Mutex::new(())).lock().unwrap_or_else(|p|p.into_inner());
            let root = tempfile::tempdir().unwrap();
            let values = [("DOXA_HOME",root.path().join("home").into_os_string()),
                ("LORE_ROOT",root.path().join("lore").into_os_string()),
                ("LORE_PROJECTS_DIR",root.path().join("projects").into_os_string()),
                ("DEEPSEEK_API_KEY","worker-fixture-key-1234".into()),("ZAI_API_KEY","worker-fixture-key-1234".into()),
                ("TYPESAFE_API_KEY","jev-fixture-key-1234".into()),("DOXA_LORE","0".into()),
                ("DOXA_VENDOR_TOOLS","workspace-read".into())];
            let previous = values.iter().map(|(key,_)|(*key,std::env::var_os(key))).collect();
            for (key,value) in values { std::env::set_var(key,value); }
            fs::create_dir(root.path().join("workspace")).unwrap();
            fs::write(root.path().join("workspace/visible.txt"),"read-only fixture").unwrap();
            Self {_lock:lock,root,previous}
        }
        fn cwd(&self)->PathBuf { self.root.path().join("workspace") }
        fn journal(&self)->PathBuf { self.root.path().join("home/router/routed.router.json") }
        fn state(&self)->Value { serde_json::from_slice(&fs::read(self.journal()).unwrap()).unwrap() }
    }
    impl Drop for Env { fn drop(&mut self) { for (key,value) in self.previous.drain(..) {
        match value {Some(value)=>std::env::set_var(key,value),None=>std::env::remove_var(key)}
    } } }

    fn config() -> Config {
        let candidate = |id:&str,provider:Provider,model:&str,effort:&str,input,output| Candidate {
            id:id.into(),provider,model:model.into(),effort:effort.into(),description:format!("Reviewed {id} test target"),
            context_tokens:131072,max_output_tokens:512,supports_tools:true,
            input_usd_micros_per_million:input,output_usd_micros_per_million:output };
        Config {version:1,jev_model:doxa_router::JEV_MODEL.into(),criteria_version:"fixture-v1".into(),
            candidates:vec![candidate("ds",Provider::Deepseek,"deepseek-flash","none",300000,1200000),
                candidate("glm",Provider::Glm,"glm-5.3-flash","high",150000,500000),
                candidate("thinking",Provider::Deepseek,"deepseek-flash","high",300000,1200000)],
            fallback_id:"ds".into(),confidence_threshold:0.8,max_calls:8,max_spend_usd_micros:10000,
            max_input_bytes:8192,deadline_ms:1000 }
    }

    struct Server { endpoint:String, requests:Arc<Mutex<Vec<Value>>>, stop:Arc<AtomicBool>, thread:Option<std::thread::JoinHandle<()>> }
    impl Server {
        fn new(mut reply:impl FnMut(&str,&Value)->(String,String,Duration)+Send+'static)->Self {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap(); listener.set_nonblocking(true).unwrap();
            let endpoint = format!("http://{}/fixture",listener.local_addr().unwrap());
            let stop = Arc::new(AtomicBool::new(false)); let requests = Arc::new(Mutex::new(Vec::new()));
            let flag = stop.clone(); let captured = requests.clone();
            let thread = std::thread::spawn(move|| {
                while !flag.load(Ordering::Acquire) {
                    let (mut socket,_) = match listener.accept() { Ok(pair)=>pair,
                        Err(error) if error.kind()==std::io::ErrorKind::WouldBlock=>{std::thread::sleep(Duration::from_millis(2));continue;},Err(error)=>panic!("{error}") };
                    socket.set_nonblocking(false).unwrap(); socket.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
                    let mut bytes = Vec::new(); let mut buffer = [0;4096];
                    let (method,body) = loop {
                        let count=socket.read(&mut buffer).unwrap(); if count==0 {break ("CLOSED".into(),Value::Null);}
                        bytes.extend_from_slice(&buffer[..count]); assert!(bytes.len()<1024*1024);
                        if let Some(end)=bytes.windows(4).position(|bytes|bytes==b"\r\n\r\n") {
                            let header=String::from_utf8_lossy(&bytes[..end]);
                            let length=header.lines().find_map(|line|line.to_ascii_lowercase().strip_prefix("content-length: ").and_then(|n|n.parse::<usize>().ok())).unwrap_or(0);
                            if bytes.len()>=end+4+length {
                                let method=header.split_whitespace().next().unwrap().to_owned();
                                let body=if length==0 {Value::Null}else{serde_json::from_slice(&bytes[end+4..end+4+length]).unwrap()};
                                break (method,body);
                            }
                        }
                    };
                    if method=="CLOSED" {continue;}
                    captured.lock().unwrap().push(json!({"method":method,"body":body}));
                    let (kind,body,delay)=reply(&method,&body); std::thread::sleep(delay);
                    let _=write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len());
                }
            });
            Self {endpoint,requests,stop,thread:Some(thread)}
        }
        fn bodies(&self)->Vec<Value> {self.requests.lock().unwrap().iter().filter(|row|row["method"]=="POST").map(|row|row["body"].clone()).collect()}
    }
    impl Drop for Server {fn drop(&mut self){self.stop.store(true,Ordering::Release);self.thread.take().unwrap().join().unwrap();}}
    fn catalog()->(String,String,Duration) {("application/json".into(),json!({"data":[{"id":"deepseek-flash"},{"id":"glm-5.3-flash"}]}).to_string(),Duration::ZERO)}
    fn choice(body:&Value,id:&str)->(String,String,Duration) {
        let probabilities=body["questions"]["target"]["criteria"].as_object().unwrap().keys()
            .map(|key|(key.clone(),json!(if key==id {1.0}else{0.0}))).collect::<serde_json::Map<_,_>>();
        ("application/json".into(),json!({"model":doxa_router::JEV_MODEL,"answers":{"target":{"type":"choice","choice":id,
            "probabilities":probabilities,"confidence":1.0}},"usage":{"input_tokens":100,"output_tokens":0}}).to_string(),Duration::ZERO)
    }
    fn worker(body:&Value,tool:bool)->(String,String,Duration) {
        let delta=if tool {json!({"reasoning_content":"PRIVATE WORKER TRACE","tool_calls":[{"index":0,"id":"read-1",
            "function":{"name":"workspace_read","arguments":"{\"path\":\"visible.txt\"}"}}]})}
            else {json!({"content":format!("{} public answer",body["model"].as_str().unwrap()),"reasoning_content":"PRIVATE WORKER TRACE"})};
        ("text/event-stream".into(),format!("data: {}\n\ndata: [DONE]\n\n",json!({"model":body["model"],"choices":[{"finish_reason":if tool{"tool_calls"}else{"stop"},"delta":delta}],
            "usage":{"prompt_tokens":3,"completion_tokens":4}})),Duration::ZERO)
    }
    fn make_host(env:&Env,cfg:Config,server:&Server,resume:bool,ceiling:Option<f64>)->RouterHost {
        RouterHost::new(cfg,&env.cwd(),"routed",resume,ceiling,Some(server.endpoint.clone())).unwrap()
    }
    fn prompt(host:&RouterHost,text:&str)->Vec<Value> {let mut events=Vec::new();host.prompt(text,&mut|event|events.push(event));events}

    #[test]
    fn actual_jev_switch_keeps_canonical_history_and_tools_sticky_without_reasoning_transfer() {
        let env=Env::new(); let mut routed=0; let mut worker_step=0;
        let server=Server::new(move|method,body| {
            if method=="GET" {return catalog();}
            if body["model"]==doxa_router::JEV_MODEL {routed+=1;return choice(body,if routed==1{"glm"}else{"ds"});}
            worker_step+=1; worker(body,worker_step%2==1)
        });
        let cfg=config(); let host=make_host(&env,cfg.clone(),&server,false,Some(2.0));
        let mut final_events=Vec::new();
        for text in ["first worker-fixture-key-1234 task","second task"] {
            let events=prompt(&host,text); assert_eq!(events.last().unwrap()["type"],"turn_done");
            assert_eq!(events.last().unwrap()["data"]["is_error"],false,"{events:?}");
            final_events.push(events.last().unwrap().clone());
        }
        let bodies=server.bodies();
        assert_eq!(bodies.iter().filter(|row|row["model"]==doxa_router::JEV_MODEL).count(),2);
        let jev=bodies.iter().filter(|row|row["model"]==doxa_router::JEV_MODEL).collect::<Vec<_>>();
        assert!(!jev[0].to_string().contains("worker-fixture-key-1234"));
        assert!(!jev[1].to_string().contains("PRIVATE WORKER TRACE"));
        assert!(jev[1]["questions"]["target"]["criteria"].get("thinking").is_none());
        let workers=bodies.iter().filter(|row|row["model"]!=doxa_router::JEV_MODEL).collect::<Vec<_>>();
        assert_eq!(workers.iter().map(|row|row["model"].as_str().unwrap()).collect::<Vec<_>>(),
            ["glm-5.3-flash","glm-5.3-flash","deepseek-flash","deepseek-flash"]);
        assert_eq!(workers.iter().map(|row|row["max_tokens"].as_u64().unwrap()).collect::<Vec<_>>(),[512,508,512,508]);
        assert!(!workers[2].to_string().contains("PRIVATE WORKER TRACE"));
        assert!(workers[2]["messages"].as_array().unwrap().iter().any(|row|row["content"]=="glm-5.3-flash public answer"));
        let state=env.state(); assert_eq!(state["router"]["calls"],2); assert_eq!(state["worker"]["input_tokens"],12);
        assert_eq!(state["worker"]["output_tokens"],16); assert_eq!(state["incomplete"],false);
        assert_eq!(state["receipts"][0]["selection"]["engine"],"glm"); assert_eq!(state["receipts"][1]["selection"]["engine"],"deepseek");
        let spent=state["router"]["actual_usd_micros"].as_u64().unwrap()+state["worker"]["estimated_actual_usd_micros"].as_u64().unwrap();
        assert!(state["router"]["actual_usd_micros"].as_u64().unwrap()>0);
        assert_eq!(final_events[1]["data"]["session_cost_usd"],json!(spent as f64/1_000_000.0));
        assert_eq!(host.billing_snapshot().unwrap()["session_cost_usd"],final_events[1]["data"]["session_cost_usd"]);
        let last=&state["receipts"][1];
        let last_cost=(6*300000u64+8*1200000).div_ceil(1_000_000)+last["router"]["router_cost_usd_micros"].as_u64().unwrap();
        assert_eq!(final_events[1]["data"]["cost_usd"],json!(last_cost as f64/1_000_000.0));
        assert_eq!(final_events[1]["data"]["cost_is_estimate"],true);
        host.shutdown(); drop(host);
        let resumed=make_host(&env,cfg.clone(),&server,true,Some(2.0));
        resumed.call("set_model",&json!({"model":"thinking"})).unwrap();
        let refused=prompt(&resumed,"thinking with foreign tools history");assert_eq!(refused[0]["type"],"turn_refused");
        assert_eq!(server.bodies().len(),6,"resume must not replay committed tools");
        resumed.call("set_model",&json!({"model":"auto"})).unwrap();
        assert_eq!(resumed.initial_model(),Some("auto".into()));
        let before=env.state(); resumed.shutdown(); drop(resumed);
        let again=make_host(&env,cfg,&server,true,Some(2.0));
        assert_eq!(env.state()["router"],before["router"]); assert_eq!(env.state()["worker"],before["worker"]);
        again.shutdown();
        let files=fs::read_dir(env.root.path().join("projects")).unwrap().filter_map(Result::ok).collect::<Vec<_>>();
        let messages:Value=serde_json::from_slice(&fs::read(files[0].path().join("routed.messages.json")).unwrap()).unwrap();
        assert_eq!(messages["engine"],"router"); assert_eq!(messages["model"],"router-conversation-v1");
        assert!(!messages.to_string().contains("reasoning_content"));
        let transcript=fs::read_to_string(files[0].path().join("routed.jsonl")).unwrap();
        assert!(transcript.lines().all(|line|serde_json::from_str::<Value>(line).unwrap()["engine"]=="router"));
    }

    #[test]
    fn invalid_choice_with_verified_usage_falls_back_and_budgeted_resume_does_not_reset_jev() {
        let env=Env::new(); let mut calls=0;
        let server=Server::new(move|method,body| {
            if method=="GET" {return catalog();}
            if body["model"]==doxa_router::JEV_MODEL {calls+=1;assert_eq!(calls,1);return choice(body,"outside");}
            worker(body,false)
        });
        let mut cfg=config();cfg.max_calls=1;
        let first=make_host(&env,cfg.clone(),&server,false,Some(2.0));
        let events=prompt(&first,"fallback"); assert_eq!(events.last().unwrap()["data"]["is_error"],false,"{events:?}");
        assert_eq!(events[0]["data"]["fallback_reason"],"invalid_response");first.shutdown();drop(first);
        let resumed=make_host(&env,cfg,&server,true,Some(2.0));
        let events=prompt(&resumed,"next turn");assert_eq!(events.last().unwrap()["data"]["is_error"],false);
        assert_eq!(events[0]["data"]["fallback_reason"],"budget");
        assert_eq!(env.state()["router"]["calls"],1);assert_eq!(server.bodies().iter().filter(|row|row["model"]==doxa_router::JEV_MODEL).count(),1);
        resumed.shutdown();
    }

    #[test]
    fn aggregate_reservation_unknown_catalog_and_underpriced_targets_refuse_before_jev() {
        let env=Env::new();
        let server=Server::new(|method,_| {assert_eq!(method,"GET");catalog()});
        let cfg=config();let first=make_host(&env,cfg.clone(),&server,false,Some(0.01));
        assert_eq!(prompt(&first,"too little allowance")[0]["type"],"turn_refused");
        assert!(server.bodies().is_empty());assert_eq!(env.state()["router"]["calls"],0);
        let before=env.state();first.shutdown();drop(first);
        let requests=server.requests.lock().unwrap().len();
        let resumed=make_host(&env,cfg.clone(),&server,true,Some(0.01));
        assert_eq!(env.state(),before);assert_eq!(server.requests.lock().unwrap().len(),requests);
        resumed.shutdown();drop(resumed);
        let mut wrong=cfg;wrong.candidates[0].input_usd_micros_per_million=1;
        assert!(RouterHost::new(wrong.clone(),&env.cwd(),"routed",true,Some(0.01),Some(server.endpoint.clone())).is_err());
        let underpriced=RouterHost::new(wrong,&env.cwd(),"underpriced",false,None,Some(server.endpoint.clone())).unwrap();
        assert_eq!(prompt(&underpriced,"underpriced fallback")[0]["type"],"turn_refused");
        assert!(server.bodies().is_empty());underpriced.shutdown();
        let empty=Server::new(|method,_|{assert_eq!(method,"GET");("application/json".into(),"{\"data\":[]}".into(),Duration::ZERO)});
        let other=RouterHost::new(config(),&env.cwd(),"empty",false,None,Some(empty.endpoint.clone())).unwrap();
        assert_eq!(prompt(&other,"catalog unknown")[0]["type"],"turn_refused");assert!(empty.bodies().is_empty());other.shutdown();
    }

    #[test]
    fn config_numeric_caps_survive_scrubbing_and_known_credentials_are_refused() {
        let env=Env::new();let server=Server::new(|_,_|catalog());
        let cfg=config();let host=make_host(&env,cfg.clone(),&server,false,None);
        assert_eq!(host.initial_model(),Some("auto".into()));
        host.call("set_model",&json!({"model":"glm"})).unwrap();
        assert_eq!(host.initial_model(),Some("glm".into()));host.shutdown();
        let mut secret=cfg;secret.candidates[0].description="jev-fixture-key-1234".into();
        assert!(RouterHost::new(secret,&env.cwd(),"secret",false,None,Some(server.endpoint.clone())).is_err());
        assert!(server.requests.lock().unwrap().is_empty());
    }

    #[test]
    fn empty_router_conversation_resumes_without_allowance_reset_or_http() {
        let env=Env::new();let server=Server::new(|_,_|panic!("Empty resume must not send HTTP"));
        let cfg=config();let first=make_host(&env,cfg.clone(),&server,false,Some(2.0));
        first.call("set_model",&json!({"model":"glm"})).unwrap();
        let before=env.state();first.shutdown();drop(first);
        assert!(RouterHost::new(cfg.clone(),&env.cwd(),"routed",false,Some(2.0),Some(server.endpoint.clone())).is_err());
        let resumed=make_host(&env,cfg.clone(),&server,true,Some(2.0));
        assert_eq!(resumed.initial_model(),Some("glm".into()));assert_eq!(env.state(),before);
        assert_eq!(resumed.billing_snapshot().unwrap()["session_cost_usd"],0.0);
        resumed.shutdown();drop(resumed);
        assert!(RouterHost::new(cfg,&env.cwd(),"routed",true,Some(1.0),Some(server.endpoint.clone())).is_err());
        assert!(server.requests.lock().unwrap().is_empty());
        let project=fs::read_dir(env.root.path().join("projects")).unwrap().next().unwrap().unwrap().path();
        let messages:Value=serde_json::from_slice(&fs::read(project.join("routed.messages.json")).unwrap()).unwrap();
        assert_eq!(messages,json!({"engine":"router","session_id":"routed","model":"router-conversation-v1","messages":[]}));
    }

    #[test]
    fn interrupted_worker_and_crash_after_history_commit_keep_durable_allowance_closed() {
        let env=Env::new();let server=Server::new(|method,body| {
            if method=="GET"{return catalog();}if body["model"]==doxa_router::JEV_MODEL{return choice(body,"ds");}
            worker(body,false)
        });
        let cfg=config();let first=make_host(&env,cfg.clone(),&server,false,Some(2.0));
        let events=prompt(&first,"committed");assert_eq!(events.last().unwrap()["data"]["is_error"],false);
        first.shutdown();drop(first);
        // Simulate loss of the final accounting write after canonical history
        // committed: the durable marker remains pending, not a fresh turn.
        let mut state=env.state();state["incomplete"]=json!(true);state["receipts"]=json!([]);
        fs::write(env.journal(),serde_json::to_vec(&state).unwrap()).unwrap();
        let resumed=make_host(&env,cfg,&server,true,Some(2.0));let prior=server.bodies().len();
        assert_eq!(prompt(&resumed,"retry after crash")[0]["type"],"turn_refused");
        assert_eq!(server.bodies().len(),prior);assert_eq!(env.state()["router"]["calls"],1);resumed.shutdown();
    }

    #[test]
    fn cancellation_during_catalog_is_bounded_and_does_not_brick_a_known_no_call_session() {
        let env=Env::new();let mut gets=0;
        let server=Server::new(move|method,body| {
            if method=="GET"{gets+=1;let(mut kind,body,mut delay)=catalog();if gets==1{delay=Duration::from_millis(200);}kind.push_str("");return(kind,body,delay);}
            if body["model"]==doxa_router::JEV_MODEL{return choice(body,"ds");}worker(body,false)
        });
        let cfg=config();let first=Arc::new(make_host(&env,cfg.clone(),&server,false,Some(2.0)));let running=first.clone();
        let thread=std::thread::spawn(move||prompt(&running,"cancel catalog"));
        let deadline=Instant::now()+Duration::from_secs(2);
        while server.requests.lock().unwrap().is_empty(){assert!(Instant::now()<deadline);std::thread::sleep(Duration::from_millis(2));}
        let started=Instant::now();first.call("interrupt",&json!({})).unwrap();
        assert_eq!(thread.join().unwrap()[0]["type"],"turn_refused");assert!(started.elapsed()<Duration::from_millis(100));
        assert_eq!(env.state()["incomplete"],false);assert_eq!(env.state()["router"]["calls"],0);
        assert!(server.bodies().is_empty());let before=env.state();first.shutdown();drop(first);
        let count=server.requests.lock().unwrap().len();
        let resumed=make_host(&env,cfg,&server,true,Some(2.0));
        assert_eq!(env.state(),before);assert_eq!(server.requests.lock().unwrap().len(),count);
        let events=prompt(&resumed,"valid next turn");assert_eq!(events.last().unwrap()["data"]["is_error"],false,"{events:?}");
        resumed.call("stop",&json!({})).unwrap();assert_eq!(prompt(&resumed,"after stop")[0]["type"],"turn_refused");
    }

    #[test]
    fn attempted_jev_and_worker_cancellation_retain_unknown_usage_and_block_resume_replay() {
        for during_jev in [true,false] {
            let env=Env::new();
            let server=Server::new(move|method,body| {
                if method=="GET" {return catalog();}
                let is_jev=body["model"]==doxa_router::JEV_MODEL;
                let (kind,text,_) = if is_jev {choice(body,"ds")} else {worker(body,false)};
                (kind,text,if is_jev==during_jev {Duration::from_millis(400)} else {Duration::ZERO})
            });
            let cfg=config();let first=Arc::new(make_host(&env,cfg.clone(),&server,false,Some(2.0)));
            let prior=server.bodies().len();
            let running=first.clone();let thread=std::thread::spawn(move||prompt(&running,"cancel active HTTP"));
            let deadline=Instant::now()+Duration::from_secs(3);
            while !server.bodies().iter().skip(prior).any(|body|(body["model"]==doxa_router::JEV_MODEL)==during_jev) {
                assert!(Instant::now()<deadline);std::thread::sleep(Duration::from_millis(2));
            }
            first.call("interrupt",&json!({})).unwrap();
            let events=thread.join().unwrap();
            assert!(events.iter().any(|event|event["type"]=="turn_refused" || event["type"]=="turn_done" && event["data"]["is_error"]==true));
            assert_eq!(first.billing_snapshot().unwrap()["budget"]["accounting_unknown"],true);
            assert!(first.billing_snapshot().unwrap()["session_cost_usd"].is_null());
            for event in &events {if event["type"]=="turn_done" {assert!(event["data"]["cost_usd"].is_null());assert!(event["data"]["session_cost_usd"].is_null());}}
            assert!(env.state()["incomplete"]==true);
            first.shutdown();drop(first);
            let resumed=make_host(&env,cfg,&server,true,Some(2.0));
            let count=server.bodies().len();assert_eq!(prompt(&resumed,"do not replay")[0]["type"],"turn_refused");
            assert_eq!(server.bodies().len(),count);resumed.shutdown();
        }
    }
}
