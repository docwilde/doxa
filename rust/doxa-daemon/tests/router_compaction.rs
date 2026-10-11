#![cfg(all(unix, feature = "local-test-server"))]
//! Actual native supervisor, private journals and loopback providers; no paid calls.
use serde_json::{json, Value};
use std::{fs, io::{BufRead, BufReader, Read, Write}, net::TcpListener,
    os::unix::{fs::PermissionsExt, net::UnixStream}, path::{Path, PathBuf}, process::{Child, Command, Stdio},
    sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}}, thread, time::{Duration, Instant}};

fn wait(mut check: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !check() { assert!(Instant::now() < deadline, "native fixture deadline"); thread::sleep(Duration::from_millis(5)); }
}
fn summary(body: &Value) -> bool { body["messages"][0]["content"].as_str().is_some_and(|text| text.starts_with("Summarize the supplied conversation")) }
fn response(body: &Value, text: &str) -> String {
    format!("data: {}\n\ndata: [DONE]\n\n", json!({"model":body["model"],"choices":[{"finish_reason":"stop","delta":{"content":text,"reasoning_content":"PRIVATE SUMMARY TRACE"}}],"usage":{"prompt_tokens":3,"completion_tokens":4}}))
}
struct Server { endpoint: String, requests: Arc<Mutex<Vec<Value>>>, stop: Arc<AtomicBool>, task: Option<thread::JoinHandle<()>> }
impl Server {
    fn new(reply: impl FnMut(&Value) -> (String, Duration) + Send + 'static) -> Self {
        Self::with_catalog(reply, || {})
    }
    fn with_catalog(mut reply: impl FnMut(&Value) -> (String, Duration) + Send + 'static,
        mut on_catalog: impl FnMut() + Send + 'static) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap(); listener.set_nonblocking(true).unwrap();
        let endpoint = format!("http://{}/fixture", listener.local_addr().unwrap());
        let requests = Arc::new(Mutex::new(Vec::new())); let captured = requests.clone();
        let stop = Arc::new(AtomicBool::new(false)); let flag = stop.clone();
        let task = thread::spawn(move || { while !flag.load(Ordering::Acquire) {
            let (mut socket, _) = match listener.accept() { Ok(pair) => pair,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {thread::sleep(Duration::from_millis(2)); continue;}, Err(error) => panic!("{error}") };
            socket.set_nonblocking(false).unwrap(); socket.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
            let mut raw = Vec::new(); let (method, body) = loop {
                let mut bytes = [0;4096]; let count = socket.read(&mut bytes).unwrap();
                if count == 0 { break ("CLOSED".to_owned(), Value::Null); }
                raw.extend_from_slice(&bytes[..count]); assert!(raw.len() < 2*1024*1024);
                if let Some(end) = raw.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
                    let head = String::from_utf8_lossy(&raw[..end]);
                    let length = head.lines().find_map(|line| line.to_ascii_lowercase().strip_prefix("content-length: ").and_then(|value|value.parse::<usize>().ok())).unwrap_or(0);
                    if raw.len() >= end+4+length {
                        break (head.split_whitespace().next().unwrap().to_owned(), if length == 0 {Value::Null} else {serde_json::from_slice(&raw[end+4..end+4+length]).unwrap()});
                    }
                }
            };
            if method == "CLOSED" {continue;}
            captured.lock().unwrap().push(json!({"method":method,"body":body}));
            let (kind, text, delay) = if method == "GET" {on_catalog(); ("application/json",json!({"data":[{"id":"deepseek-flash"},{"id":"glm-5.3-flash"}]}).to_string(),Duration::ZERO)}
                else {assert_ne!(body["model"],"jev-1.13.0","Compaction must never call Jev"); let (text,delay)=reply(&body);("text/event-stream",text,delay)};
            thread::sleep(delay);
            let _ = write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{text}",text.len());
        }});
        Self {endpoint,requests,stop,task:Some(task)}
    }
    fn posts(&self) -> Vec<Value> {self.requests.lock().unwrap().iter().filter(|row|row["method"]=="POST").map(|row|row["body"].clone()).collect()}
}
impl Drop for Server {fn drop(&mut self) {self.stop.store(true,Ordering::Release);self.task.take().unwrap().join().unwrap();}}

struct Env { root: tempfile::TempDir }
impl Env {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        for name in ["workspace","doxa","run","run-resume"] {fs::create_dir(root.path().join(name)).unwrap();fs::set_permissions(root.path().join(name),fs::Permissions::from_mode(0o700)).unwrap();}
        let candidate = |id,provider,model,effort,input,output| json!({"id":id,"provider":provider,"model":model,"effort":effort,"description":format!("Reviewed {id} fixture"),
            "context_tokens":131072,"max_output_tokens":32,"supports_tools":true,"input_usd_micros_per_million":input,"output_usd_micros_per_million":output});
        let config = json!({"version":1,"jev_model":"jev-1.13.0","criteria_version":"compact-v1","candidates":[
            candidate("ds","deepseek","deepseek-flash","none",300000,1200000),candidate("glm","glm","glm-5.3-flash","high",150000,500000)],
            "fallback_id":"ds","confidence_threshold":0.8,"max_calls":8,"max_spend_usd_micros":10000,"max_input_bytes":8192,"deadline_ms":1000});
        fs::write(root.path().join("router.json"),config.to_string()).unwrap();fs::set_permissions(root.path().join("router.json"),fs::Permissions::from_mode(0o600)).unwrap();
        let worker=root.path().join("review-worker");
        fs::write(&worker,"#!/bin/sh\n[ \"$1\" = review-worker ] || exit 7\ncat > \"$REVIEW_CAPTURE\"\ncase \"$REVIEW_MODE\" in\n deny) exit 1 ;;\n wait) sleep 10 ;;\n mutate) printf ' ' >> \"$SOURCE_PATH\" ;;\n mutate-deny) printf ' ' >> \"$SOURCE_PATH\"; exit 1 ;;\nesac\nexit 0\n").unwrap();
        fs::set_permissions(worker,fs::Permissions::from_mode(0o700)).unwrap();
        Self {root}
    }
    fn path(&self) -> &Path {self.root.path()}
    fn cwd(&self) -> PathBuf {self.path().join("workspace")}
    fn project(&self) -> PathBuf {let slug:String=self.cwd().to_string_lossy().chars().map(|ch|if ch.is_ascii_alphanumeric(){ch}else{'-'}).collect();self.path().join("projects").join(slug)}
    fn journal(&self) -> PathBuf {self.path().join("doxa/router/compact.router.json")}
    fn state(&self) -> Value {serde_json::from_slice(&fs::read(self.journal()).unwrap()).unwrap()}
    fn context(&self) -> PathBuf {self.project().join("compact.context.json")}
    fn originals(&self) -> (Vec<u8>,Vec<u8>) {(fs::read(self.project().join("compact.jsonl")).unwrap(),fs::read(self.project().join("compact.messages.json")).unwrap())}
    fn daemon(&self, server:&Server, resume:bool, model:Option<&str>, review:&str, disabled:bool) -> Daemon {
        self.daemon_key(server,resume,model,review,disabled,false)
    }
    fn daemon_key(&self, server:&Server, resume:bool, model:Option<&str>, review:&str, disabled:bool, router_key:bool) -> Daemon {
        // A killed daemon intentionally leaves its old registry entry. Resume
        // in a fresh runtime namespace while retaining canonical cwd/journal.
        let runtime=self.path().join(if resume{"run-resume"}else{"run"});
        let mut cmd=Command::new(env!("CARGO_BIN_EXE_doxa-daemon"));cmd.env_clear().args(["--runtime-dir",runtime.to_str().unwrap(),"--cwd",self.cwd().to_str().unwrap(),"--session-id","compact",
            "--engine","router","--router-config",self.path().join("router.json").to_str().unwrap(),"--vendor-endpoint",&server.endpoint,"--resume",if resume{"true"}else{"false"},"--linger","20"]);
        if let Some(model)=model {cmd.args(["--model",model]);}
        if router_key {cmd.env("TYPESAFE_API_KEY","local-jev-fixture-key");}
        cmd.env("PATH","/usr/bin:/bin").env("HOME",self.path().join("home")).env("DOXA_HOME",self.path().join("doxa"))
            .env("LORE_ROOT",self.path().join("lore")).env("LORE_PROJECTS_DIR",self.path().join("projects")).env("LORE_SKILLS_DIR",self.path().join("skills"))
            .env("LORE_DISABLE_SYNC","1").env("DOXA_LORE_RS",self.path().join("review-worker")).env("REVIEW_CAPTURE",self.path().join("review.json"))
            .env("REVIEW_MODE",review).env("SOURCE_PATH",self.project().join("compact.jsonl"))
            .env("DEEPSEEK_API_KEY","local-fixture-key").env("ZAI_API_KEY","local-fixture-key").env("DOXA_SESSION_BUDGET_USD","2")
            .env("LORE_DISABLE_REVIEW",if disabled{"1"}else{"0"}).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::piped());
        let mut child=cmd.spawn().unwrap();let registry=runtime.join("registry/compact.json");
        wait(|| {if child.try_wait().unwrap().is_some() {let mut text=String::new();child.stderr.take().unwrap().read_to_string(&mut text).unwrap();panic!("daemon startup: {text}");}
            registry.exists() && serde_json::from_slice::<Value>(&fs::read(&registry).unwrap()).ok().is_some_and(|entry|entry["pid"].as_u64()==Some(child.id() as u64))});
        let entry:Value=serde_json::from_slice(&fs::read(registry).unwrap()).unwrap();let mut socket=UnixStream::connect(entry["daemon_socket"].as_str().unwrap()).unwrap();socket.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        let mut reader=BufReader::new(socket.try_clone().unwrap());receive(&mut reader);send(&mut socket,json!({"type":"attach","cursor":null}));
        send(&mut socket,json!({"type":"call","id":900,"method":"get_settings","params":{}}));
        loop {if receive(&mut reader)["id"]==900 {break;}}
        Daemon {child,socket,reader,id:1000}
    }
}
fn receive(reader:&mut BufReader<UnixStream>) -> Value {let mut line=String::new();assert!(reader.read_line(&mut line).unwrap()>0);serde_json::from_str(&line).unwrap()}
fn send(socket:&mut UnixStream,value:Value) {writeln!(socket,"{value}").unwrap();}
struct Daemon {child:Child,socket:UnixStream,reader:BufReader<UnixStream>,id:u64}
impl Daemon {
    fn start(&mut self,text:&str) {self.id+=1;send(&mut self.socket,json!({"type":"prompt","id":self.id,"text":text}));}
    fn finish(&mut self) -> Value {loop {let frame=receive(&mut self.reader);if matches!(frame["event"]["type"].as_str(),Some("turn_done"|"turn_refused")) {return frame["event"].clone();}}}
    fn prompt(&mut self,text:&str) -> Value {self.start(text);self.finish()}
    fn call(&mut self,method:&str,params:Value) -> Value {self.id+=1;let id=self.id;send(&mut self.socket,json!({"type":"call","id":id,"method":method,"params":params}));loop {let frame=receive(&mut self.reader);if frame["id"]==id{return frame;}}}
    fn interrupt(&mut self) {self.id+=1;send(&mut self.socket,json!({"type":"call","id":self.id,"method":"interrupt","params":{}}));}
    fn stop(mut self) {self.call("stop",json!({}));wait(||self.child.try_wait().unwrap().is_some());}
}
impl Drop for Daemon {fn drop(&mut self) {let _=self.child.kill();let _=self.child.wait();}}

#[test]
fn managed_router_compaction_preserves_originals_mode_and_last_target_then_resumes_across_provider_switch() {
    let env=Env::new();let server=Server::new(|body|(response(body,if summary(body){"MANAGED SUMMARY"}else{"original public answer"}),Duration::ZERO));
    let mut daemon=env.daemon_key(&server,false,Some("glm"),"approve",false,true);
    assert_eq!(daemon.prompt("original user")["data"]["is_error"],false);
    daemon.call("set_model",json!({"model":"auto"}));let before=env.state();let originals=env.originals();
    let compact=daemon.prompt("/compact");assert_eq!(compact["data"]["is_error"],false,"{compact}");
    assert_eq!(compact["data"]["compaction"]["target_id"],"glm");assert_eq!(compact["data"]["compaction"]["reviewed"],true);
    assert_eq!(compact["data"]["compaction_semantics"],"doxa_managed_summary");assert_eq!(env.originals(),originals);
    let state=env.state();assert_eq!(state["selection"],before["selection"]);assert_eq!(state["router"],before["router"]);assert!(state["pinned"].is_null());assert!(state["pending_compaction"].is_null());
    assert_eq!(state["receipts"][1]["operation"],"compact");assert_eq!(state["worker"]["input_tokens"],6);assert_eq!(state["worker"]["output_tokens"],8);
    let review:Value=serde_json::from_slice(&fs::read(env.path().join("review.json")).unwrap()).unwrap();assert_eq!(review["conversation_engine"],"router");assert_eq!(review["summary_target"]["model"],"glm-5.3-flash");assert!(review["expected_source"]["sha256"].is_string());
    let posts=server.posts();assert_eq!(posts.len(),2);assert_eq!(posts[1]["max_tokens"],32);assert!(posts[1].get("tools").is_none());assert!(!posts[1].to_string().contains("PRIVATE SUMMARY TRACE"));
    daemon.stop();let mut resumed=env.daemon(&server,true,None,"approve",false);
    assert_eq!(resumed.call("get_settings",json!({}))["model"],"auto");
    assert_eq!(resumed.prompt("followup user")["data"]["is_error"],false);
    let posts=server.posts();assert_eq!(posts.len(),3);assert_eq!(posts[2]["model"],"deepseek-flash");assert!(posts[2].to_string().contains("MANAGED SUMMARY"));assert!(!posts[2].to_string().contains("original public answer"));assert!(!posts[2].to_string().contains("reasoning_content"));
    assert_eq!(env.state()["compaction"]["target_id"],"glm");resumed.stop();
}

#[test]
fn exact_pin_and_missing_previous_selection_use_configured_targets_without_jev() {
    let env=Env::new();let server=Server::new(|body|(response(body,"public fixture"),Duration::ZERO));
    let mut daemon=env.daemon_key(&server,false,Some("glm"),"approve",false,true);assert_eq!(daemon.prompt("original")["data"]["is_error"],false);
    let selection=env.state()["selection"].clone();daemon.call("set_model",json!({"model":"ds"}));
    assert_eq!(daemon.prompt("/compact")["data"]["compaction"]["target_id"],"ds");assert_eq!(env.state()["selection"],selection);assert_eq!(env.state()["pinned"],"ds");
    daemon.call("set_model",json!({"model":"auto"}));daemon.stop();
    let mut state=env.state();state["selection"]=Value::Null;fs::write(env.journal(),state.to_string()).unwrap();
    let mut resumed=env.daemon_key(&server,true,None,"approve",false,true);assert_eq!(resumed.prompt("/compact")["data"]["compaction"]["target_id"],"ds");assert!(env.state()["selection"].is_null());assert_eq!(env.state()["router"]["calls"],0);resumed.stop();
}

#[test]
fn saved_messages_mutation_and_serialized_summary_overhead_refuse_before_provider_posts() {
    for case in ["messages","catalog-messages","body-cap"] {
        let env=Env::new();let mutate=Arc::new(AtomicBool::new(false));let during_catalog=mutate.clone();let source=env.project().join("compact.messages.json");
        let server=Server::with_catalog(move|body| {
            // Complete lexical units keep this large escaped text scrub-safe;
            // JSON wrapping still amplifies it beyond the summary request cap.
            let text=if case=="body-cap" {"\\\" ".repeat(20000)}else{"original public answer".into()};
            (response(body,&text),Duration::ZERO)
        }, move|| {if during_catalog.swap(false,Ordering::AcqRel) {let mut value:Value=serde_json::from_slice(&fs::read(&source).unwrap()).unwrap();value["messages"][1]["content"]=json!("changed during catalog lookup");fs::write(&source,value.to_string()).unwrap();}});
        let mut daemon=env.daemon(&server,false,None,"approve",false);let first=daemon.prompt("original");assert_eq!(first["data"]["is_error"],false,"{case}: {first}");
        if case=="messages" {let path=env.project().join("compact.messages.json");let mut value:Value=serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();value["messages"][1]["content"]=json!("externally changed source");fs::write(path,value.to_string()).unwrap();}
        let before=env.originals();if case=="catalog-messages" {mutate.store(true,Ordering::Release);}
        let count=server.posts().len();let result=daemon.prompt("/compact");
        if case=="catalog-messages" {assert_eq!(result["data"]["is_error"],true,"{result}");assert!(!mutate.load(Ordering::Acquire));}
        else {assert_eq!(result["type"],"turn_refused","{case}: {result}");}
        let changed=env.originals();if case=="catalog-messages" {assert_eq!(changed.0,before.0);assert_ne!(changed.1,before.1);}else{assert_eq!(changed,before);}
        assert_eq!(server.posts().len(),count);assert!(!env.context().exists());
        if case!="body-cap" {assert_eq!(daemon.prompt("do not overwrite external edit")["type"],"turn_refused");assert_eq!(server.posts().len(),count);assert_eq!(env.originals(),changed);}
        daemon.stop();
    }
}

#[test]
fn disabled_review_refused_review_and_exhausted_budget_do_not_post_and_leave_complete_allowance() {
    for mode in ["disabled","deny","budget"] {
        let env=Env::new();let server=Server::new(|body|(response(body,"public fixture"),Duration::ZERO));
        let mut daemon=env.daemon(&server,false,None,if mode=="deny"{"deny"}else{"approve"},mode=="disabled");
        assert_eq!(daemon.prompt("original")["data"]["is_error"],false);let originals=env.originals();
        if mode=="budget" {daemon.stop();let mut state=env.state();state["worker"]["estimated_actual_usd_micros"]=json!(1_999_999);fs::write(env.journal(),state.to_string()).unwrap();daemon=env.daemon(&server,true,None,"approve",false);}
        let before=env.state();let posts=server.posts().len();let result=daemon.prompt("/compact");assert!(result["type"]=="turn_refused"||result["data"]["is_error"]==true,"{result}");
        assert_eq!(server.posts().len(),posts);assert_eq!(env.originals(),originals);assert_eq!(env.state()["worker"],before["worker"]);assert_eq!(env.state()["router"],before["router"]);assert_eq!(env.state()["incomplete"],false);assert!(env.state()["pending_compaction"].is_null());
        if mode!="budget" {assert_eq!(daemon.prompt("still usable")["data"]["is_error"],false);}daemon.stop();
    }
}

#[test]
fn unknown_or_conflicting_summary_usage_retains_pending_identity_and_blocks_resume_without_replay() {
    for bad in ["missing","wrong-model","model-conflict","zero-input","zero-output"] {
        let env=Env::new();let root=env.path().to_owned();let mut summaries=0;
        let server=Server::new(move|body| {
            if !summary(body) {return(response(body,"original public answer"),Duration::ZERO);}
            summaries+=1;if summaries==1 {return(response(body,"OLD SUMMARY"),Duration::ZERO);}
            let state:Value=serde_json::from_slice(&fs::read(root.join("doxa/router/compact.router.json")).unwrap()).unwrap();
            assert_eq!(state["incomplete"],true);let pending=&state["pending_compaction"];assert_eq!(pending["summary_target"]["target_id"],"ds");assert_eq!(pending["input_cap_bytes"],131040);assert_eq!(pending["output_cap_tokens"],32);assert_eq!(pending["request_sha256"].as_str().unwrap().len(),64);assert!(pending["source"]["sha256"].is_string());assert!(state["worker"]["retained_reservation_usd_micros"].as_u64().unwrap()>0);
            let mut event=json!({"model":body["model"],"choices":[{"finish_reason":"stop","delta":{"content":"NEW SUMMARY"}}],"usage":{"prompt_tokens":3,"completion_tokens":4}});
            match bad {"missing"=>{event.as_object_mut().unwrap().remove("usage");},"wrong-model"=>event["model"]=json!("unexpected-server-prose"),"zero-input"=>event["usage"]["prompt_tokens"]=json!(0),"zero-output"=>event["usage"]["completion_tokens"]=json!(0),_=>{}}
            let prefix=if bad=="model-conflict" {format!("data: {}\n\n",json!({"model":"unexpected-server-prose","choices":[{"delta":{"content":"wrong "}}]}))} else {String::new()};
            (format!("{prefix}data: {event}\n\ndata: [DONE]\n\n"),Duration::ZERO)
        });
        let mut daemon=env.daemon(&server,false,None,"approve",false);assert_eq!(daemon.prompt("original")["data"]["is_error"],false);assert_eq!(daemon.prompt("/compact")["data"]["is_error"],false);
        let context=fs::read(env.context()).unwrap();let originals=env.originals();let selection=env.state()["selection"].clone();let result=daemon.prompt("/compact");assert_eq!(result["data"]["is_error"],true,"{bad}: {result}");assert_eq!(result["data"]["accounting_unknown"],true);assert!(!result.to_string().contains("unexpected-server-prose"));assert!(result["data"]["cost_usd"].is_null());assert_eq!(fs::read(env.context()).unwrap(),context);assert_eq!(env.originals(),originals);assert_eq!(env.state()["selection"],selection);assert!(env.state()["pending_compaction"].is_object());let count=server.posts().len();assert_eq!(daemon.prompt("no fresh allowance")["type"],"turn_refused");assert_eq!(server.posts().len(),count);
        daemon.stop();let mut resumed=env.daemon(&server,true,None,"approve",false);assert_eq!(resumed.prompt("no replay")["type"],"turn_refused");assert_eq!(server.posts().len(),count);resumed.stop();
    }
}

#[test]
fn valid_usage_settles_even_when_summary_is_rejected_and_old_context_remains() {
    for bad in ["length","overshoot","malformed"] {
        let env=Env::new();let mut summaries=0;let server=Server::new(move|body| {
            if !summary(body) {return(response(body,"original public answer"),Duration::ZERO);}
            summaries+=1;if summaries==1{return(response(body,"OLD SUMMARY"),Duration::ZERO);}
            let event=json!({"model":body["model"],"choices":[{"finish_reason":if bad=="length"{"length"}else{"stop"},"delta":{"content":"NEW SUMMARY"}}],"usage":{"prompt_tokens":3,"completion_tokens":if bad=="overshoot"{33}else{4}}});
            (format!("{}data: {event}\n\ndata: [DONE]\n\n",if bad=="malformed"{"data: invalid json\n\n"}else{""}),Duration::ZERO)
        });
        let mut daemon=env.daemon(&server,false,None,"approve",false);assert_eq!(daemon.prompt("original")["data"]["is_error"],false);assert_eq!(daemon.prompt("/compact")["data"]["is_error"],false);
        let context=fs::read(env.context()).unwrap();let before=env.state();let result=daemon.prompt("/compact");assert_eq!(result["data"]["is_error"],true);assert_eq!(result["data"]["accounting_unknown"],false);assert!(result["data"]["cost_usd"].as_f64().unwrap()>0.0);assert_eq!(fs::read(env.context()).unwrap(),context);assert!(env.state()["pending_compaction"].is_null());assert_eq!(env.state()["worker"]["input_tokens"].as_u64().unwrap(),before["worker"]["input_tokens"].as_u64().unwrap()+3);assert_eq!(env.state()["receipts"][2]["complete"],true);assert_eq!(daemon.prompt("valid next turn")["data"]["is_error"],false);daemon.stop();
    }
}

#[test]
fn changed_reviewed_source_blocks_future_posts_and_preserves_old_context_and_changed_files() {
    for phase in ["review","provider","refused-review"] {
        let env=Env::new();let source=env.project().join("compact.jsonl");let mut summaries=0;
        let server=Server::new(move|body| {if summary(body){summaries+=1;if summaries==2&&phase=="provider" {use std::fs::OpenOptions;let mut file=OpenOptions::new().append(true).open(&source).unwrap();file.write_all(b" ").unwrap();}}
            (response(body,if summary(body){"OLD SUMMARY"}else{"original public answer"}),Duration::ZERO)});
        let mut daemon=env.daemon(&server,false,None,"approve",false);assert_eq!(daemon.prompt("original")["data"]["is_error"],false);assert_eq!(daemon.prompt("/compact")["data"]["is_error"],false);let context=fs::read(env.context()).unwrap();
        if phase!="provider" {daemon.stop();daemon=env.daemon(&server,true,None,if phase=="review"{"mutate"}else{"mutate-deny"},false);}
        let before=server.posts().len();let result=daemon.prompt("/compact");assert_eq!(result["data"]["is_error"],true,"{phase}: {result}");assert_eq!(result["data"]["accounting_unknown"],false);assert_eq!(server.posts().len(),before+usize::from(phase=="provider"));assert_eq!(fs::read(env.context()).unwrap(),context);
        let changed=env.originals();let count=server.posts().len();assert_eq!(daemon.prompt("do not overwrite changed source")["type"],"turn_refused");assert_eq!(server.posts().len(),count);assert_eq!(env.originals(),changed);daemon.stop();
    }
}

#[test]
fn review_cancellation_is_known_no_call_but_sent_cancellation_and_crash_keep_pending_allowance_closed() {
    for phase in ["review","sent","crash"] {
        let env=Env::new();let server=Server::new(move|body|(response(body,"public summary"),if summary(body)&&phase!="review"{Duration::from_millis(500)}else{Duration::ZERO}));
        let mut daemon=env.daemon(&server,false,None,if phase=="review"{"wait"}else{"approve"},false);assert_eq!(daemon.prompt("original")["data"]["is_error"],false);let originals=env.originals();
        daemon.start("/compact");if phase=="review" {wait(||env.path().join("review.json").exists());}else{wait(||server.posts().iter().any(summary));assert!(env.state()["pending_compaction"].is_object());}
        if phase=="crash" {daemon.child.kill().unwrap();daemon.child.wait().unwrap();drop(daemon);}
        else {daemon.interrupt();let result=daemon.finish();assert_eq!(result["data"]["is_error"],true);assert_eq!(result["data"]["summary_attempted"],phase=="sent");assert_eq!(result["data"]["accounting_unknown"],phase=="sent");daemon.stop();}
        assert_eq!(env.originals(),originals);let count=server.posts().len();let mut resumed=env.daemon(&server,true,None,"approve",false);
        if phase=="review" {assert!(env.state()["pending_compaction"].is_null());assert_eq!(resumed.prompt("usable next turn")["data"]["is_error"],false);assert_eq!(server.posts().len(),count+1);}
        else {assert!(env.state()["pending_compaction"].is_object());assert_eq!(resumed.prompt("no replay or reset")["type"],"turn_refused");assert_eq!(server.posts().len(),count);}
        resumed.stop();
    }
}
