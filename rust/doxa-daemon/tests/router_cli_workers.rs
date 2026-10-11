#![cfg(all(target_os="linux",feature="local-test-server"))]
//! Credential-free native process/catalog/tool/permission/router fixtures.
use serde_json::{json,Value};
use std::{fs,io::{BufRead,BufReader,Read,Write},net::TcpListener,os::unix::{fs::PermissionsExt,net::UnixStream},
    path::{Path,PathBuf},process::{Child,Command,Stdio},sync::{Arc,Mutex,atomic::{AtomicBool,Ordering}},thread,time::{Duration,Instant}};
fn wait(mut check:impl FnMut()->bool) {let end=Instant::now()+Duration::from_secs(15);while !check(){assert!(Instant::now()<end,"native fixture deadline");thread::sleep(Duration::from_millis(5));}}
struct Server {endpoint:String,posts:Arc<Mutex<Vec<Value>>>,stop:Arc<AtomicBool>,task:Option<thread::JoinHandle<()>>}
impl Server {
    fn new(mut choice:impl FnMut()->String+Send+'static)->Self {
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
        let endpoint=format!("http://{}/fixture",listener.local_addr().unwrap());let posts=Arc::new(Mutex::new(Vec::new()));let capture=posts.clone();
        let stop=Arc::new(AtomicBool::new(false));let flag=stop.clone();
        let task=thread::spawn(move||while !flag.load(Ordering::Acquire) {
            let (mut socket,_)=match listener.accept(){Ok(pair)=>pair,Err(error) if error.kind()==std::io::ErrorKind::WouldBlock=>{thread::sleep(Duration::from_millis(2));continue;},Err(error)=>panic!("{error}")};
            socket.set_read_timeout(Some(Duration::from_secs(3))).unwrap();let mut bytes=Vec::new();let mut buf=[0;4096];
            let (method,body)=loop {let n=socket.read(&mut buf).unwrap();if n==0 {break ("closed".to_owned(),Value::Null)}bytes.extend_from_slice(&buf[..n]);
                if let Some(end)=bytes.windows(4).position(|row|row==b"\r\n\r\n") {let header=String::from_utf8_lossy(&bytes[..end]);
                    let len=header.lines().find_map(|line|line.to_ascii_lowercase().strip_prefix("content-length: ").and_then(|value|value.parse::<usize>().ok())).unwrap_or(0);
                    if bytes.len()>=end+4+len {break (header.split_whitespace().next().unwrap().to_owned(),if len==0{Value::Null}else{serde_json::from_slice(&bytes[end+4..end+4+len]).unwrap()});}}
            };
            if method=="closed" {continue;}
            let (kind,response)=if method=="GET" {("application/json",json!({"data":[{"id":"deepseek-flash"},{"id":"glm-5.3-flash"}]}).to_string())}
            else {capture.lock().unwrap().push(body.clone());if body["model"]==doxa_router::JEV_MODEL {
                let id=choice();let probabilities=body["questions"]["target"]["criteria"].as_object().unwrap().keys().map(|key|(key.clone(),json!(if *key==id{1.0}else{0.0}))).collect::<serde_json::Map<_,_>>();
                ("application/json",json!({"model":doxa_router::JEV_MODEL,"answers":{"target":{"type":"choice","choice":id,"probabilities":probabilities,"confidence":1.0}},"usage":{"input_tokens":100,"output_tokens":0}}).to_string())
            } else {("text/event-stream",format!("data: {}\n\ndata: [DONE]\n\n",json!({"model":body["model"],"choices":[{"finish_reason":"stop","delta":{"content":"API answer","reasoning_content":"PRIVATE API TRACE"}}],"usage":{"prompt_tokens":7,"completion_tokens":5}})))}};
            let _=write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response}",response.len());
        });Self{endpoint,posts,stop,task:Some(task)}
    }
    fn posts(&self)->Vec<Value>{self.posts.lock().unwrap().clone()}
}
impl Drop for Server {fn drop(&mut self){self.stop.store(true,Ordering::Release);self.task.take().unwrap().join().unwrap();}}
struct Env {root:tempfile::TempDir}
impl Env {
    fn new()->Self {let root=tempfile::tempdir().unwrap();let env=Self{root};fs::create_dir(env.cwd()).unwrap();fs::write(env.path().join("mode"),"normal").unwrap();
        for provider in ["claude","codex"] {let path=env.path().join(provider);fs::write(&path,include_str!("fixtures/router_cli.py")).unwrap();fs::set_permissions(path,fs::Permissions::from_mode(0o700)).unwrap();}
        let cli=|id:&str,provider:&str|json!({"id":id,"provider":provider,"billing":"cli_account","model":format!("{provider}-fixture-1"),"effort":"high","description":"Operator local fixture policy only","context_tokens":131072,"max_output_tokens":512,"supports_tools":true});
        let api=|id:&str,provider:&str,model:&str,input,output|json!({"id":id,"provider":provider,"model":model,"effort":"none","description":"Operator local fixture policy only","context_tokens":131072,"max_output_tokens":512,"supports_tools":true,"input_usd_micros_per_million":input,"output_usd_micros_per_million":output});
        let config=json!({"version":1,"jev_model":doxa_router::JEV_MODEL,"criteria_version":"native-fixture-v1","candidates":[cli("claude","claude"),cli("codex","codex"),api("ds","deepseek","deepseek-flash",300000,1200000),api("glm","glm","glm-5.3-flash",150000,500000)],"fallback_id":"ds","confidence_threshold":0.8,"max_calls":10,"max_spend_usd_micros":10000,"max_input_bytes":8192,"deadline_ms":1000});
        fs::write(env.path().join("router.json"),config.to_string()).unwrap();fs::set_permissions(env.path().join("router.json"),fs::Permissions::from_mode(0o600)).unwrap();env
    }
    fn path(&self)->&Path {self.root.path()}
    fn cwd(&self)->PathBuf {self.path().join("workspace")}
    fn project(&self)->PathBuf {let slug:String=self.cwd().to_string_lossy().chars().map(|ch|if ch.is_ascii_alphanumeric(){ch}else{'-'}).collect();self.path().join("projects").join(slug)}
    fn state(&self)->Value {serde_json::from_slice(&fs::read(self.path().join("doxa/router/routed.router.json")).unwrap()).unwrap()}
    fn originals(&self)->Vec<u8>{fs::read(self.project().join("routed.messages.json")).unwrap()}
    fn capture(&self)->Vec<Value>{fs::read_to_string(self.path().join("calls.jsonl")).unwrap_or_default().lines().map(|line|serde_json::from_str(line).unwrap()).collect()}
    fn mode(&self,mode:&str){fs::write(self.path().join("mode"),mode).unwrap();}
    fn daemon(&self,server:&Server,resume:bool,bins:bool)->Daemon {
        let run=self.path().join(if resume{"resume-run"}else{"run"});let mut cmd=Command::new(env!("CARGO_BIN_EXE_doxa-daemon"));
        cmd.env_clear().args(["--engine","router","--router-config",self.path().join("router.json").to_str().unwrap(),"--runtime-dir",run.to_str().unwrap(),"--cwd",self.cwd().to_str().unwrap(),"--session-id","routed","--vendor-endpoint",&server.endpoint,"--resume",if resume{"true"}else{"false"},"--linger","20"]);
        if bins {cmd.arg("--claude-bin").arg(self.path().join("claude")).arg("--codex-bin").arg(self.path().join("codex"));}
        cmd.env("PATH","/usr/bin:/bin").env("HOME",self.path().join("home")).env("DOXA_HOME",self.path().join("doxa")).env("CODEX_HOME",self.path().join("codex-home"))
            .env("LORE_ROOT",self.path().join("lore")).env("LORE_PROJECTS_DIR",self.path().join("projects")).env("LORE_SKILLS_DIR",self.path().join("skills"))
            .env("DOXA_LORE_RS",std::env::var_os("DOXA_LORE_RS").unwrap()).env("LORE_DISABLE_SYNC","1").env("LORE_DISABLE_REVIEW","1").env("DOXA_LORE","0").env("DOXA_WORKTREE","0").env("DOXA_AGENT_PEER_SEND","1")
            .env("DEEPSEEK_API_KEY","local-worker-fixture-key").env("ZAI_API_KEY","local-worker-fixture-key").env("TYPESAFE_API_KEY","local-jev-fixture-key").env("DOXA_SESSION_BUDGET_USD","2")
            .env("FIXTURE_MODE_FILE",self.path().join("mode")).env("FIXTURE_LOG",self.path().join("calls.jsonl"))
            .env("FIXTURE_CANONICAL_MESSAGES",self.project().join("routed.messages.json"))
            .env("TMPDIR",std::env::var_os("TMPDIR").unwrap()).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::piped());
        let mut child=cmd.spawn().unwrap();let registry=run.join("registry/routed.json");wait(||{if child.try_wait().unwrap().is_some(){let mut error=String::new();child.stderr.take().unwrap().read_to_string(&mut error).unwrap();panic!("native startup: {error}");}registry.exists()});
        let entry:Value=serde_json::from_slice(&fs::read(registry).unwrap()).unwrap();let mut socket=UnixStream::connect(entry["daemon_socket"].as_str().unwrap()).unwrap();socket.set_read_timeout(Some(Duration::from_secs(20))).unwrap();
        let mut reader=BufReader::new(socket.try_clone().unwrap());receive(&mut reader);send(&mut socket,json!({"type":"attach","cursor":null}));
        let mut daemon=Daemon{child,socket,reader,id:0,events:Vec::new(),run};daemon.call("get_settings",json!({}));daemon
    }
}
fn receive(reader:&mut BufReader<UnixStream>)->Value{let mut line=String::new();assert!(reader.read_line(&mut line).unwrap()>0);serde_json::from_str(&line).unwrap()}
fn send(socket:&mut UnixStream,value:Value){writeln!(socket,"{value}").unwrap();}
struct Daemon{child:Child,socket:UnixStream,reader:BufReader<UnixStream>,id:u64,events:Vec<Value>,run:PathBuf}
impl Daemon {
    fn call(&mut self,method:&str,params:Value)->Value{self.id+=1;let id=self.id;send(&mut self.socket,json!({"type":"call","id":id,"method":method,"params":params}));loop{let frame=receive(&mut self.reader);if frame["id"]==id{return frame;}if frame["type"]=="event"{self.events.push(frame["event"].clone());}}}
    fn start(&mut self,text:&str){self.id+=1;send(&mut self.socket,json!({"type":"prompt","id":self.id,"text":text}));}
    fn finish(&mut self)->Value{loop{let frame=receive(&mut self.reader);if frame["type"]!="event"{continue;}let event=frame["event"].clone();self.events.push(event.clone());
        if event["type"]=="needs_input" {self.id+=1;send(&mut self.socket,json!({"type":"call","id":self.id,"method":"answer_needs_input","params":{"id":event["data"]["id"],"answer":{"decision":"allow"}}}));}
        if matches!(event["type"].as_str(),Some("turn_done"|"turn_refused")){return event;}}}
    fn prompt(&mut self,text:&str)->Value{self.start(text);self.finish()}
    fn stop(mut self){self.call("stop",json!({}));wait(||self.child.try_wait().unwrap().is_some());}
}
impl Drop for Daemon{fn drop(&mut self){let _=self.child.kill();let _=self.child.wait();}}

#[test]
fn authenticated_native_workers_review_tools_keep_canonical_history_and_resume_across_api_switch() {
    let env=Env::new();let mut turn=0;let server=Server::new(move||{turn+=1;["claude","codex","ds","glm"][turn-1].into()});
    let mut daemon=env.daemon(&server,false,true);
    assert_eq!(daemon.call("get_settings",json!({}))["permission_mode"],"default");
    for (text,engine) in [("first actual user request","claude"),("second actual user request","codex"),("third actual user request","deepseek")] {
        let result=daemon.prompt(text);assert_eq!(result["data"]["is_error"],false,"{result}");assert_eq!(result["data"]["engine"],engine);
        assert_eq!(result["data"]["cost_scope"],"jev_and_direct_api_only");assert!(result["data"]["total_cost_usd"].is_null());assert_eq!(result["data"]["accounting_unknown"],false);
    }
    assert!(daemon.events.iter().filter(|event|event["type"]=="needs_input").count()>=4);
    assert!(daemon.events.iter().filter(|event|event["type"]=="turn_started").all(|event|!event["data"]["prompt"].as_str().unwrap().contains("DOXA ROUTER CONVERSATION CONTEXT")));
    let mut entries=fs::read_dir(daemon.run.join("registry")).unwrap().map(|entry|entry.unwrap().file_name())
        .filter(|name|Path::new(name).extension().is_some_and(|extension|extension=="json")).collect::<Vec<_>>();entries.sort();
    assert_eq!(entries,[std::ffi::OsString::from("routed.json")],"internal worker entered the public registry");
    let capture=env.capture();assert_eq!(capture.iter().filter(|row|row["kind"]=="peer-result").count(),2);
    let claude_pid=&capture.iter().find(|row|row["provider"]=="claude"&&row["kind"]=="prompt").unwrap()["pid"];
    assert_eq!(capture.iter().filter(|row|row["pid"]==*claude_pid&&row["kind"]=="interrupt").count(),1,"worker teardown ran twice");
    let codex=capture.iter().find(|row|row["provider"]=="codex"&&row["kind"]=="turn/start").unwrap();
    assert!(codex["value"]["input"].to_string().contains("Claude answer"));assert!(codex["value"]["input"].to_string().contains("first actual user request"));
    let state=env.state();assert_eq!(state["worker"]["cli_turns"],2);assert!(state["pending_cli"].is_null());
    let ids:Vec<_>=state["receipts"].as_array().unwrap().iter().filter_map(|row|row["worker"]["worker_id"].as_str()).collect();assert_eq!(ids.len(),2);assert_ne!(ids[0],ids[1]);assert!(ids.iter().all(|id|doxa_claude::cli::canonical_session_id(id)));
    let messages:Value=serde_json::from_slice(&env.originals()).unwrap();assert_eq!(messages["engine"],"router");assert_eq!(messages["messages"].as_array().unwrap().len(),6);assert!(!messages.to_string().contains("reasoning_content"));
    daemon.stop();let users=env.capture().iter().filter(|row|row["kind"]=="prompt"||row["kind"]=="turn/start").count();
    let mut resumed=env.daemon(&server,true,true);assert_eq!(resumed.call("get_settings",json!({}))["model"],"auto");assert_eq!(env.capture().iter().filter(|row|row["kind"]=="prompt"||row["kind"]=="turn/start").count(),users);
    let result=resumed.prompt("fourth resumed user request");assert_eq!(result["data"]["is_error"],false,"{result}");assert_eq!(result["data"]["engine"],"glm");assert_eq!(result["data"]["cost_scope"],"jev_and_direct_api_only");assert!(result["data"]["total_cost_usd"].is_null());
    assert!(server.posts().last().unwrap()["messages"].to_string().contains("Codex answer"));resumed.stop();
}

#[test]
fn missing_cli_executable_keeps_api_fallback_usable_and_pin_refuses_before_jev() {
    let env=Env::new();let server=Server::new(||"ds".into());let mut daemon=env.daemon(&server,false,false);
    let result=daemon.prompt("API fallback");assert_eq!(result["data"]["is_error"],false,"{result}");let calls=server.posts().len();
    daemon.call("set_model",json!({"model":"claude"}));assert_eq!(daemon.prompt("missing pinned CLI")["type"],"turn_refused");assert_eq!(server.posts().len(),calls);assert!(env.capture().is_empty());daemon.stop();
}

#[test]
fn native_cli_missing_usage_or_wrong_model_blocks_future_work_and_resume_replay() {
    for (provider,mode) in [("claude","missing-usage"),("codex","missing-usage"),("claude","wrong-model"),("codex","wrong-model"),("claude","excess-usage"),("codex","excess-usage")] {
        let env=Env::new();env.mode(mode);let server=Server::new(||"ds".into());let mut daemon=env.daemon(&server,false,true);
        daemon.call("set_model",json!({"model":provider}));let originals=env.originals();let result=daemon.prompt("unverified worker task");assert_eq!(result["data"]["is_error"],true,"{provider}/{mode}: {result}");
        assert_eq!(env.originals(),originals);assert_eq!(env.state()["worker"]["accounting_unknown"],true);assert!(env.state()["pending_cli"].is_object());assert_eq!(daemon.prompt("blocked retry")["type"],"turn_refused");
        daemon.stop();let calls=env.capture().len();let mut resumed=env.daemon(&server,true,true);assert_eq!(resumed.prompt("do not replay")["type"],"turn_refused");assert_eq!(env.capture().len(),calls);resumed.stop();
    }
}

#[test]
fn native_worker_source_change_keeps_external_edit_and_blocks_later_paid_work() {
    for provider in ["claude","codex"] {
        let env=Env::new();env.mode("source-change");let server=Server::new(||"ds".into());let mut daemon=env.daemon(&server,false,true);
        daemon.call("set_model",json!({"model":provider}));let terminal=daemon.prompt("canonical source must match");
        assert_eq!(terminal["data"]["is_error"],true);assert_eq!(terminal["data"]["accounting_unknown"],true);
        let edited=env.originals();let messages:Value=serde_json::from_slice(&edited).unwrap();
        assert_eq!(messages["messages"].as_array().unwrap().len(),1);assert!(messages.to_string().contains("external canonical edit"));
        let posts=server.posts().len();let calls=env.capture().len();assert_eq!(daemon.prompt("refuse source drift")["type"],"turn_refused");
        assert_eq!(server.posts().len(),posts);assert_eq!(env.capture().len(),calls);assert_eq!(env.originals(),edited);daemon.stop();
    }
}

#[test]
fn changed_auth_or_effort_is_ineligible_and_auth_change_after_jev_submits_no_cli_prompt() {
    for mode in ["no-auth","missing-effort"] {let env=Env::new();env.mode(mode);let server=Server::new(||"ds".into());let mut daemon=env.daemon(&server,false,true);
        daemon.call("set_model",json!({"model":"claude"}));assert_eq!(daemon.prompt("unauthorized CLI")["type"],"turn_refused");assert!(server.posts().is_empty());
        daemon.call("set_model",json!({"model":"codex"}));assert_eq!(daemon.prompt("unavailable Codex")["type"],"turn_refused");assert!(server.posts().is_empty());daemon.stop();}
    let env=Env::new();let mode=env.path().join("mode");let server=Server::new(move||{fs::write(&mode,"no-auth").unwrap();"claude".into()});let mut daemon=env.daemon(&server,false,true);
    assert_eq!(daemon.prompt("account changed after shortlist")["type"],"turn_refused");assert!(!env.capture().iter().any(|row|row["kind"]=="prompt"||row["kind"]=="turn/start"));assert!(env.state()["pending_cli"].is_null());daemon.stop();
    let env=Env::new();env.mode("alias");let server=Server::new(||"ds".into());let mut daemon=env.daemon(&server,false,true);
    daemon.call("set_model",json!({"model":"claude"}));assert_eq!(daemon.prompt("require resolved model")["type"],"turn_refused");
    assert!(server.posts().is_empty());assert!(!env.capture().iter().any(|row|row["kind"]=="prompt"));daemon.stop();
}

#[test]
fn interrupted_native_workers_release_processes_and_withhold_replay() {
    for provider in ["claude","codex"] {
        let env=Env::new();env.mode("hold");let server=Server::new(||"ds".into());let mut daemon=env.daemon(&server,false,true);
        daemon.call("set_model",json!({"model":provider}));let originals=env.originals();daemon.start("interrupt an admitted worker");
        loop {
            let frame=receive(&mut daemon.reader);if frame["type"]!="event"{continue;}
            let event=&frame["event"];daemon.events.push(event.clone());
            if event["type"]=="needs_input" {daemon.id+=1;send(&mut daemon.socket,json!({"type":"call","id":daemon.id,"method":"answer_needs_input",
                "params":{"id":event["data"]["id"],"answer":{"decision":"allow"}}}));}
            if event["type"]=="tool_result" {break;}
            assert!(!matches!(event["type"].as_str(),Some("turn_done"|"turn_refused")),"worker did not reach admitted tool: {event}");
        }
        wait(||env.capture().iter().any(|row|row["kind"]=="peer-result"));
        let capture=env.capture();let pid=capture.iter().find(|row|row["kind"]=="prompt"||row["kind"]=="turn/start").unwrap()["pid"].as_u64().unwrap();
        daemon.id+=1;send(&mut daemon.socket,json!({"type":"call","id":daemon.id,"method":"interrupt","params":{}}));
        let terminal=daemon.finish();assert_eq!(terminal["data"]["is_error"],true);assert_eq!(terminal["data"]["accounting_unknown"],true);
        wait(||!Path::new(&format!("/proc/{pid}")).exists());assert_eq!(env.originals(),originals);assert!(env.state()["pending_cli"].is_object());
        daemon.stop();let calls=env.capture().len();let mut resumed=env.daemon(&server,true,true);
        assert_eq!(resumed.prompt("never replay interrupted CLI")["type"],"turn_refused");assert_eq!(env.capture().len(),calls);resumed.stop();
    }
}
