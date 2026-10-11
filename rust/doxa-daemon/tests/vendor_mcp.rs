#![cfg(all(target_os="linux", feature="local-test-server"))]
//! Actual daemon + native API transport + local MCP processes; no accounts.
use serde_json::{json, Value};
use std::{fs, io::{BufRead, BufReader, Read, Write}, net::TcpListener,
    os::unix::{fs::PermissionsExt, net::UnixStream}, path::{Path, PathBuf}, process::{Child, Command, Stdio},
    sync::{Arc, Mutex, atomic::{AtomicBool, Ordering}}, thread, time::{Duration, Instant}};
fn wait(mut check: impl FnMut()->bool) { let deadline=Instant::now()+Duration::from_secs(15);
    while !check() { assert!(Instant::now()<deadline,"local MCP fixture deadline"); thread::sleep(Duration::from_millis(5)); } }
fn private(path:&Path,value:Value) { fs::write(path,value.to_string()).unwrap(); fs::set_permissions(path,fs::Permissions::from_mode(0o600)).unwrap(); }
fn records(path:&Path)->Vec<Value> { fs::read_to_string(path).unwrap_or_default().lines().map(|line|serde_json::from_str(line).unwrap()).collect() }
fn calls(path:&Path)->usize { records(path).iter().filter(|row|row["method"]=="tools/call").count() }
struct Model { endpoint:String, requests:Arc<Mutex<Vec<Value>>>, stop:Arc<AtomicBool>, task:Option<thread::JoinHandle<()>> }
impl Model {
    fn new(call_count:usize)->Self {
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
        let endpoint=format!("http://{}/chat",listener.local_addr().unwrap());let requests=Arc::new(Mutex::new(Vec::new()));let capture=requests.clone();
        let stop=Arc::new(AtomicBool::new(false));let flag=stop.clone();
        let task=thread::spawn(move||while !flag.load(Ordering::Acquire) {
            let (mut socket,_)=match listener.accept(){Ok(pair)=>pair,Err(error) if error.kind()==std::io::ErrorKind::WouldBlock=>{thread::sleep(Duration::from_millis(2));continue;},Err(error)=>panic!("{error}")};
            socket.set_read_timeout(Some(Duration::from_secs(5))).unwrap();let mut raw=Vec::new();let mut buffer=[0;4096];
            let (headers,end,length)=loop {let count=socket.read(&mut buffer).unwrap();if count==0 {break (String::new(),0,0);}raw.extend_from_slice(&buffer[..count]);
                if let Some(index)=raw.windows(4).position(|chunk|chunk==b"\r\n\r\n") {let headers=String::from_utf8_lossy(&raw[..index]).to_string();
                    let length=headers.lines().find_map(|line|line.to_ascii_lowercase().strip_prefix("content-length:").and_then(|s|s.trim().parse::<usize>().ok())).unwrap_or(0);break(headers,index+4,length);} };
            if end==0 {continue;}while raw.len()<end+length {let count=socket.read(&mut buffer).unwrap();assert!(count>0);raw.extend_from_slice(&buffer[..count]);}
            let (content_type,body)=if headers.starts_with("GET ") {
                ("application/json",json!({"data":[{"id":"deepseek-flash"},{"id":"glm-5.3-flash"}]}).to_string())
            } else {
                let request:Value=serde_json::from_slice(&raw[end..end+length]).unwrap();capture.lock().unwrap().push(request.clone());
                let already_called=request["messages"].as_array().unwrap().iter().any(|row|row["role"]=="tool");
                let names=request["tools"].as_array().map(|tools|tools.iter().filter_map(|tool|tool["function"]["name"].as_str().filter(|name|name.starts_with("mcp_ext_")).map(str::to_owned)).collect::<Vec<_>>()).unwrap_or_default();
                let tool=call_count>0 && !already_called && !names.is_empty();
                let delta=if tool {json!({"tool_calls":(0..call_count).map(|index|json!({"index":index,"id":format!("external-{index}"),"type":"function",
                    "function":{"name":names[index%names.len()],"arguments":json!({"query":format!("synthetic query {index}"),"password":"synthetic argument private phrase"}).to_string()}})).collect::<Vec<_>>()})} else {json!({"content":"Completed local synthetic turn."})};
                ("text/event-stream",format!("data: {}\n\ndata: [DONE]\n\n",json!({"model":request["model"],"choices":[{"finish_reason":if tool{"tool_calls"}else{"stop"},"delta":delta}],"usage":{"prompt_tokens":5,"completion_tokens":3}})))
            };
            let response=format!("HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len());let _=socket.write_all(response.as_bytes());
        });Self{endpoint,requests,stop,task:Some(task)}
    }
}
impl Drop for Model {fn drop(&mut self){self.stop.store(true,Ordering::Release);if let Some(task)=self.task.take(){task.join().unwrap();}}}
struct Fixture { root:tempfile::TempDir, model:Model, server:Option<Child>, config:PathBuf, log:PathBuf, control:PathBuf, credentials:PathBuf, secret:String }
impl Fixture {
    fn new(transport:&str,call_count:usize)->Self {
        let root=tempfile::tempdir().unwrap();let home=root.path().join("home");fs::create_dir(root.path().join("workspace")).unwrap();fs::create_dir(&home).unwrap();fs::set_permissions(&home,fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(root.path(),fs::Permissions::from_mode(0o700)).unwrap();
        let secret=format!("synthetic-search-{}",root.path().file_name().unwrap().to_string_lossy());let credentials=home.join("credentials.json");
        private(&credentials,json!({"deepseek":"synthetic-api-fixture-key","glm":"synthetic-api-fixture-key","tavily":secret}));
        let log=root.path().join("mcp-log.jsonl");let control=root.path().join("control.json");private(&control,json!({"secret":secret}));
        let script=Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/mcp_server.py");let address=root.path().join("address");
        let (server,fields)=if transport=="stdio" {(None,json!({"transport":"stdio","program":"/usr/bin/python3","args":[script,transport,control,log,address],"env":{"TAVILY_API_KEY":{"env":"TAVILY_API_KEY"}}}))}
        else {let child=Command::new("/usr/bin/python3").env_clear().args([script.to_str().unwrap(),transport,control.to_str().unwrap(),log.to_str().unwrap(),address.to_str().unwrap()])
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn().unwrap();wait(||address.exists());
            (Some(child),json!({"transport":"streamable_http","url":fs::read_to_string(&address).unwrap(),"headers":{"Authorization":{"env":"TAVILY_API_KEY","bearer":true}},"allow_loopback_http":true}))};
        let mut entry=json!({"id":"search-local","enabled":true,"allow_tools":["search","workspace_read","msg"],"limits":{"timeout_ms":2000,"shutdown_ms":100}});
        entry["transport"]=fields;
        let config=root.path().join("mcp.json");private(&config,json!({"schema_version":1,"servers":[entry]}));
        Self{root,model:Model::new(call_count),server,config,log,control,credentials,secret}
    }
    fn path(&self)->&Path {self.root.path()}
    fn launch(&self,engine:&str,routed:bool,enabled:bool)->Daemon {self.launch_resume(engine,routed,enabled,false)}
    fn launch_resume(&self,engine:&str,routed:bool,enabled:bool,resume:bool)->Daemon {
        let model=if engine=="glm"{"glm-5.3-flash"}else{"deepseek-flash"};let effort=if engine=="glm"{"high"}else{"none"};
        let runtime=self.path().join(format!("runtime-{}",std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));fs::create_dir_all(&runtime).unwrap();let mut cmd=Command::new(env!("CARGO_BIN_EXE_doxa-daemon"));
        cmd.env_clear().args(["--runtime-dir",runtime.to_str().unwrap(),"--cwd",self.path().join("workspace").to_str().unwrap(),"--session-id","mcp-session","--engine",if routed{"router"}else{engine},"--vendor-endpoint",&self.model.endpoint,"--linger","20"])
            .env("PATH","/usr/bin:/bin").env("HOME",self.path().join("home")).env("DOXA_HOME",self.path().join("home")).env("DOXA_LORE","0")
            .env("DOXA_MCP_ENABLED",if enabled{"1"}else{"0"}).env("DOXA_MCP_CONFIG",&self.config).env("DOXA_MCP_UNRELATED","must-not-enter-server")
            .env("LORE_ROOT",self.path().join("lore")).env("LORE_PROJECTS_DIR",self.path().join("projects")).env("LORE_SKILLS_DIR",self.path().join("skills"))
            .env("LORE_DISABLE_SYNC","1").env("LORE_DISABLE_REVIEW","1").env("TMPDIR",std::env::var_os("TMPDIR").unwrap())
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::piped());
        if let Some(binary)=std::env::var_os("DOXA_LORE_RS"){cmd.env("DOXA_LORE_RS",binary);}
        if routed {
            let candidate=|id:&str,provider:&str,model:&str,effort:&str,billing:&str,input:u64,output:u64|json!({"id":id,"provider":provider,"model":model,"effort":effort,"billing":billing,
                "description":"Synthetic exact account target","context_tokens":131072,"max_output_tokens":512,"supports_tools":true,
                "input_usd_micros_per_million":input,"output_usd_micros_per_million":output});
            let config=self.path().join("router.json");private(&config,json!({"version":1,"jev_model":"jev-1.13.0","criteria_version":"synthetic-mcp","fallback_id":"ds",
                "confidence_threshold":0.8,"max_calls":20,"max_spend_usd_micros":10000,"max_input_bytes":2048,"deadline_ms":1000,
                "candidates":[candidate("ds","deepseek","deepseek-flash","none","api",300000,1200000),candidate("glm","glm","glm-5.3-flash","high","api",150000,500000),
                    candidate("cli-unavailable","claude","claude-exact-fixture","high","cli_account",0,0)]}));
            cmd.args(["--router-config",config.to_str().unwrap(),"--model",if engine=="glm"{"glm"}else{"ds"}]);
        } else {cmd.args(["--model",model,"--effort",effort]);}
        if resume {cmd.args(["--resume","true"]);}
        let mut child=cmd.spawn().unwrap();let registry=runtime.join("registry/mcp-session.json");wait(||{if child.try_wait().unwrap().is_some(){let mut text=String::new();child.stderr.take().unwrap().read_to_string(&mut text).unwrap();panic!("daemon startup: {text}");}registry.exists()});
        let record:Value=serde_json::from_slice(&fs::read(registry).unwrap()).unwrap();let mut socket=UnixStream::connect(record["daemon_socket"].as_str().unwrap()).unwrap();socket.set_read_timeout(Some(Duration::from_secs(12))).unwrap();
        let mut reader=BufReader::new(socket.try_clone().unwrap());receive(&mut reader);send(&mut socket,json!({"type":"attach","cursor":null}));
        Daemon{child,socket,reader,id:10,events:Vec::new()}
    }
    fn change(&self,value:Value){private(&self.control,value);}
    fn child_pid(&self)->u64 {records(&self.log).iter().find_map(|row|row["pid"].as_u64()).unwrap()}
}
impl Drop for Fixture {fn drop(&mut self){if let Some(child)=self.server.as_mut(){let _=child.kill();let _=child.wait();}}}
fn receive(reader:&mut BufReader<UnixStream>)->Value {let mut line=String::new();assert!(reader.read_line(&mut line).unwrap()>0);serde_json::from_str(&line).unwrap()}
fn send(socket:&mut UnixStream,value:Value){writeln!(socket,"{value}").unwrap();}
struct Daemon {child:Child,socket:UnixStream,reader:BufReader<UnixStream>,id:u64,events:Vec<Value>}
impl Daemon {
    fn call(&mut self,method:&str,params:Value)->Value {self.id+=1;let id=self.id;send(&mut self.socket,json!({"type":"call","id":id,"method":method,"params":params}));loop {let frame=receive(&mut self.reader);if frame["id"]==id {return frame;}if frame["type"]=="event"{self.events.push(frame["event"].clone());}}}
    fn start(&mut self){self.id+=1;send(&mut self.socket,json!({"type":"prompt","id":self.id,"text":"Run local synthetic external tools"}));}
    fn next(&mut self)->Value {loop{let frame=receive(&mut self.reader);if frame["type"]=="event"{let event=frame["event"].clone();self.events.push(event.clone());return event;}}}
    fn approval(&mut self)->Value {loop {let event=self.next();assert!(!matches!(event["type"].as_str(),Some("turn_done"|"turn_refused")),"ended before approval: {event}");if event["type"]=="needs_input" {return event["data"].clone();}}}
    fn answer(&mut self,request:&Value,decision:&str){assert_eq!(self.call("answer_needs_input",json!({"id":request["id"],"answer":{"decision":decision}}))["ok"],true);}
    fn finish(&mut self)->Value {loop{let event=self.next();assert_ne!(event["type"],"needs_input","unexpected pending approval");if matches!(event["type"].as_str(),Some("turn_done"|"turn_refused")){return event;}}}
    fn stop(mut self){self.call("stop",json!({}));wait(||self.child.try_wait().unwrap().is_some());}
}
impl Drop for Daemon {fn drop(&mut self){let _=self.child.kill();let _=self.child.wait();}}
fn verify_consecutive(engine:&str,transport:&str,routed:bool){
    let fixture=Fixture::new(transport,2);let mut daemon=fixture.launch(engine,routed,true);
    if routed {assert_eq!(daemon.call("set_permission_mode",json!({"mode":"auto"}))["ok"],true);}
    daemon.start();let first=daemon.approval();assert_eq!(first["require_full_review"],true);assert_eq!(calls(&fixture.log),0);
    assert_eq!(first["mcp_server_id"],"search-local");assert!(first["tool_name"].as_str().unwrap().starts_with("mcp_ext_search_local__"));
    let reviewed:Value=serde_json::from_str(first["input_summary"].as_str().unwrap()).unwrap();
    assert!(!reviewed.to_string().contains("synthetic argument private phrase"),"LORE did not scrub synthetic password argument");
    let offered=fixture.model.requests.lock().unwrap()[0].clone();let names=offered["tools"].as_array().unwrap().iter().filter_map(|tool|tool["function"]["name"].as_str().filter(|name|name.starts_with("mcp_ext_"))).collect::<Vec<_>>();
    assert_eq!(names.len(),3);assert!(names.iter().all(|name|name.len()<=63));assert!(names.iter().any(|name|name.contains("workspace_re")));assert!(names.iter().any(|name|name.contains("__msg_")));assert!(names.iter().all(|name|!name.contains("not_allowed")));
    daemon.answer(&first,"allow");let second=daemon.approval();assert_ne!(first["id"],second["id"]);assert_eq!(calls(&fixture.log),1);
    let transmitted=records(&fixture.log).into_iter().find(|row|row["method"]=="tools/call").unwrap();
    assert_eq!(transmitted["params"]["arguments"],reviewed["arguments"],"transmission differed from exact reviewed scrubbed arguments");
    let kinds=daemon.events.iter().map(|event|event["type"].as_str().unwrap()).collect::<Vec<_>>();let one=kinds.iter().position(|kind|*kind=="needs_input").unwrap();let two=kinds.iter().rposition(|kind|*kind=="needs_input").unwrap();
    assert!(kinds[one+1..two].contains(&"tool_result"));assert!(kinds[one+1..two].contains(&"needs_input_resolved"));assert!(!kinds[one+1..two].contains(&"text_delta"));
    daemon.answer(&second,"allow");let end=daemon.finish();assert_eq!(end["data"]["is_error"],false);assert_eq!(calls(&fixture.log),2);
    assert_eq!(end["data"]["external_mcp"]["calls_attempted"],2);assert!(end["data"]["total_cost_usd"].is_null());
    let observed=fixture.model.requests.lock().unwrap().clone();assert_eq!(observed.len(),2);let continuation=observed[1].to_string();assert!(!continuation.contains(&fixture.secret));assert!(!continuation.contains("synthetic phrase"));assert!(!continuation.contains("OPAQUE_IMAGE_PAYLOAD"));assert!(continuation.contains("Non-text MCP content withheld"));
    assert!(!serde_json::to_string(&daemon.events).unwrap().contains(&fixture.secret));
    let pid=fixture.child_pid();daemon.stop();if transport=="stdio"{wait(||!Path::new(&format!("/proc/{pid}")).exists());assert_eq!(records(&fixture.log)[0]["unrelated_env_present"],false);}else{wait(||records(&fixture.log).iter().any(|row|row["delete"]==true));}
}
#[test] fn deepseek_stdio_two_external_calls_flush_without_model_text_and_reap(){verify_consecutive("deepseek","stdio",false);}
#[test] fn glm_http_scoped_names_one_call_reviews_and_scrubbed_continuation(){verify_consecutive("glm","http",false);}
#[test] fn router_api_workers_inherit_mcp_and_auto_permissions_still_need_two_reviews(){for (engine,transport) in [("deepseek","stdio"),("glm","http")]{verify_consecutive(engine,transport,true);}}
#[test] fn stale_config_credentials_or_schema_during_review_withhold_arguments(){
    for change in ["config","credential","schema"]{let fixture=Fixture::new("stdio",1);let mut daemon=fixture.launch("glm",false,true);daemon.start();let request=daemon.approval();
        match change {"config"=>{let mut config:Value=serde_json::from_slice(&fs::read(&fixture.config).unwrap()).unwrap();config["servers"][0]["allow_tools"]=json!(["search"]);private(&fixture.config,config);},
            "credential"=>private(&fixture.credentials,json!({"deepseek":"synthetic-api-fixture-key","glm":"synthetic-api-fixture-key","tavily":"synthetic-changed-search-key"})),
            _=>fixture.change(json!({"changed_schema":true}))}
        daemon.answer(&request,"allow");daemon.finish();assert_eq!(calls(&fixture.log),0,"stale {change} sent tool arguments");
        assert!(daemon.events.iter().any(|event|event["type"]=="tool_result"&&event["data"]["is_error"]==true));daemon.stop();
    }
}
#[test] fn deny_and_cancel_clear_review_and_cancel_attempt_blocks_replay_and_reaps(){
    for mode in ["deny","review-cancel","call-cancel"] {let fixture=Fixture::new("stdio",1);let mut daemon=fixture.launch("deepseek",false,true);daemon.start();let request=daemon.approval();
        if mode=="deny"{daemon.answer(&request,"deny");daemon.finish();assert_eq!(calls(&fixture.log),0);}
        else if mode=="review-cancel" {daemon.call("interrupt",json!({}));daemon.finish();assert_eq!(calls(&fixture.log),0);assert_ne!(daemon.call("answer_needs_input",json!({"id":request["id"],"answer":{"decision":"allow"}}))["ok"],true);}
        else {fixture.change(json!({"hold":true}));daemon.answer(&request,"allow");wait(||calls(&fixture.log)==1);daemon.call("interrupt",json!({}));daemon.finish();
            let before=fixture.model.requests.lock().unwrap().len();daemon.start();let end=daemon.finish();assert_eq!(end["data"]["is_error"],true);assert_eq!(fixture.model.requests.lock().unwrap().len(),before,"unknown external outcome replayed");}
        let pid=fixture.child_pid();daemon.stop();wait(||!Path::new(&format!("/proc/{pid}")).exists());
    }
}
#[test] fn oversize_external_result_is_withheld_and_disabled_mcp_starts_no_process(){
    let fixture=Fixture::new("stdio",1);fixture.change(json!({"oversize":true}));let mut daemon=fixture.launch("glm",false,true);daemon.start();let request=daemon.approval();daemon.answer(&request,"allow");daemon.finish();
    let requests=fixture.model.requests.lock().unwrap();assert!(requests[1].to_string().contains("exceeded host display/model limit"));assert!(requests[1].to_string().len()<20000);drop(requests);daemon.stop();
    let fixture=Fixture::new("stdio",0);fs::remove_file(&fixture.config).unwrap();let mut daemon=fixture.launch("deepseek",false,false);daemon.start();let end=daemon.finish();assert_eq!(end["data"]["is_error"],false);assert!(!fixture.log.exists());daemon.stop();
}

#[test] fn unresolved_attempt_receipt_survives_native_restart_and_blocks_resume_replay(){
    let fixture=Fixture::new("stdio",1);let mut daemon=fixture.launch("glm",false,true);
    daemon.start();let review=daemon.approval();daemon.answer(&review,"allow");daemon.finish();
    fixture.change(json!({"hold":true}));daemon.start();let review=daemon.approval();daemon.answer(&review,"allow");wait(||calls(&fixture.log)==2);
    daemon.call("interrupt",json!({}));daemon.finish();daemon.stop();
    let receipt=fixture.path().join("home/mcp-receipts/mcp-session.json");let saved:Value=serde_json::from_slice(&fs::read(&receipt).unwrap()).unwrap();
    assert!(saved["pending"].is_object());assert!(!saved.to_string().contains("synthetic query"));assert!(!saved.to_string().contains(&fixture.secret));
    let before=fixture.model.requests.lock().unwrap().len();let mut resumed=fixture.launch_resume("glm",false,true,true);resumed.start();
    let end=resumed.finish();assert_eq!(end["data"]["is_error"],true);assert_eq!(end["data"]["external_mcp"]["outcome_unknown"],true);
    assert_eq!(fixture.model.requests.lock().unwrap().len(),before);assert_eq!(calls(&fixture.log),2);resumed.stop();
}
