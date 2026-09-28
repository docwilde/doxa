#![cfg(all(unix, feature = "local-test-server"))]
//! Controlled native daemon/HTTP fixtures. No real provider or reviewer calls.
use serde_json::{json,Value};
use std::{fs,io::{BufRead,BufReader,Read,Write},net::TcpListener,os::unix::{fs::PermissionsExt,net::UnixStream},path::{Path,PathBuf},process::{Child,Command,Stdio},thread,time::{Duration,Instant}};

fn wait(mut check:impl FnMut()->bool){let deadline=Instant::now()+Duration::from_secs(10);while !check(){assert!(Instant::now()<deadline,"native fixture deadline");thread::sleep(Duration::from_millis(10));}}
struct Daemon(Child);
impl Drop for Daemon{fn drop(&mut self){let _=self.0.kill();let _=self.0.wait();}}
fn receive(reader:&mut BufReader<UnixStream>)->Value{let mut line=String::new();assert!(reader.read_line(&mut line).unwrap()>0);serde_json::from_str(&line).unwrap()}
fn send(socket:&mut UnixStream,value:Value){writeln!(socket,"{value}").unwrap();}
fn done(reader:&mut BufReader<UnixStream>)->Value{loop{let frame=receive(reader);if frame["event"]["type"]=="turn_done"{return frame["event"]["data"].clone();}}}
fn serve(frames:Vec<&'static str>)->(String,thread::JoinHandle<Vec<Value>>){
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
    let endpoint=format!("http://{}/chat/completions",listener.local_addr().unwrap());
    let worker=thread::spawn(move||{
        let mut requests=vec![];
        for content in frames{
            let deadline=Instant::now()+Duration::from_secs(10);
            let mut socket=loop{match listener.accept(){Ok((stream,_))=>break stream,Err(error) if error.kind()==std::io::ErrorKind::WouldBlock=>{assert!(Instant::now()<deadline,"provider fixture request deadline");thread::sleep(Duration::from_millis(10));},Err(error)=>panic!("{error}")}};
            socket.set_read_timeout(Some(Duration::from_secs(5))).unwrap();let mut request=vec![];
            loop{let mut bytes=[0;4096];let count=socket.read(&mut bytes).unwrap();assert!(count>0);request.extend_from_slice(&bytes[..count]);
                if let Some(index)=request.windows(4).position(|bytes|bytes==b"\r\n\r\n"){
                    let head=String::from_utf8_lossy(&request[..index]);let length=head.lines().find_map(|line|line.to_ascii_lowercase().strip_prefix("content-length: ").and_then(|value|value.parse::<usize>().ok())).unwrap();
                    if request.len()>=index+4+length{requests.push(serde_json::from_slice(&request[index+4..index+4+length]).unwrap());break;}
                }
            }
            let event=json!({"model":"deepseek-flash","choices":[{"finish_reason":"stop","delta":{"content":content}}],"usage":{"prompt_tokens":3,"completion_tokens":4}});
            let body=format!("data: {event}\n\ndata: [DONE]\n\n");
            write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len()).unwrap();
        }requests
    });(endpoint,worker)
}
fn transcript_dir(runtime:&Path)->PathBuf{let slug:String=runtime.to_string_lossy().chars().map(|c|if c.is_ascii_alphanumeric(){c}else{'-'}).collect();runtime.join("projects").join(slug)}

#[test]
fn reviewed_managed_compaction_preserves_originals_and_review_refusal_keeps_session_usable(){
    for approved in [true,false]{
        let dir=tempfile::tempdir().unwrap();let root=dir.path();
        let worker=root.join("review-worker");
        fs::write(&worker,format!("#!/bin/sh\n[ \"$1\" = review-worker ] || exit 7\ncat > \"$REVIEW_CAPTURE\"\nexit {}\n",if approved{0}else{1})).unwrap();
        fs::set_permissions(&worker,fs::Permissions::from_mode(0o700)).unwrap();
        let (endpoint,server)=serve(if approved{vec!["original assistant","MANAGED-SUMMARY-FIXTURE","followup assistant"]}else{vec!["original assistant","followup assistant"]});
        let mut daemon=Daemon(Command::new(env!("CARGO_BIN_EXE_doxa-daemon")).env_clear()
            .args(["--runtime-dir",root.to_str().unwrap(),"--cwd",root.to_str().unwrap(),"--session-id","vendor-compaction","--engine","deepseek","--model","deepseek-flash","--vendor-endpoint",&endpoint,"--linger","20"])
            .env("PATH","/usr/bin:/bin").env("HOME",root.join("home")).env("DOXA_HOME",root.join("doxa"))
            .env("LORE_ROOT",root.join("lore")).env("LORE_PROJECTS_DIR",root.join("projects")).env("LORE_SKILLS_DIR",root.join("skills"))
            .env("LORE_DISABLE_SYNC","1").env("DOXA_LORE_RS",&worker).env("REVIEW_CAPTURE",root.join("review.json"))
            .env("DEEPSEEK_API_KEY","local-fixture-key").env("DOXA_SESSION_BUDGET_USD","1")
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::piped()).spawn().unwrap());
        let registry=root.join("registry/vendor-compaction.json");
        wait(||{if registry.exists(){true}else{if daemon.0.try_wait().unwrap().is_some(){let mut detail=String::new();daemon.0.stderr.take().unwrap().read_to_string(&mut detail).unwrap();panic!("native daemon: {detail}");}false}});
        let entry:Value=serde_json::from_slice(&fs::read(&registry).unwrap()).unwrap();
        let mut socket=UnixStream::connect(entry["daemon_socket"].as_str().unwrap()).unwrap();socket.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        let mut reader=BufReader::new(socket.try_clone().unwrap());receive(&mut reader);send(&mut socket,json!({"type":"attach","cursor":null}));
        send(&mut socket,json!({"type":"prompt","id":1,"text":"original user"}));assert_eq!(done(&mut reader)["is_error"],false);
        let transcripts=transcript_dir(root);let original=fs::read(transcripts.join("vendor-compaction.jsonl")).unwrap();
        let messages=fs::read(transcripts.join("vendor-compaction.messages.json")).unwrap();
        send(&mut socket,json!({"type":"prompt","id":2,"text":"/compact"}));let compact=done(&mut reader);
        assert_eq!(compact["is_error"],!approved);assert_eq!(compact["compaction_semantics"],"doxa_managed_summary");
        assert_eq!(compact["usage_complete"],true);assert!(compact["cost_usd"].as_f64().is_some());
        assert_eq!(fs::read(transcripts.join("vendor-compaction.jsonl")).unwrap(),original);
        assert_eq!(fs::read(transcripts.join("vendor-compaction.messages.json")).unwrap(),messages);
        let metadata:Value=serde_json::from_slice(&fs::read(root.join("review.json")).unwrap()).unwrap();
        assert_eq!(metadata["session_id"],"vendor-compaction");assert_eq!(metadata["older"],true);assert!(metadata["expected_source"]["sha256"].as_str().is_some());
        assert_eq!(transcripts.join("vendor-compaction.context.json").exists(),approved);
        send(&mut socket,json!({"type":"prompt","id":3,"text":"followup user"}));assert_eq!(done(&mut reader)["is_error"],false);
        send(&mut socket,json!({"type":"call","id":4,"method":"stop","params":{}}));
        wait(||daemon.0.try_wait().unwrap().is_some());
        let requests=server.join().unwrap();
        if approved{
            assert!(requests[1].get("tools").is_none());assert_eq!(requests[1]["max_tokens"],4096);
            assert!(requests[1].to_string().contains("original user"));assert!(requests[2].to_string().contains("MANAGED-SUMMARY-FIXTURE"));
            assert!(!requests[2].to_string().contains("original assistant"));
        }else{assert!(requests[1].to_string().contains("original assistant"));assert!(!requests[1].to_string().contains("MANAGED-SUMMARY-FIXTURE"));}
        let durable=fs::read_to_string(transcripts.join("vendor-compaction.messages.json")).unwrap();
        assert!(durable.contains("original assistant"));assert!(durable.contains("followup assistant"));assert!(!durable.contains("MANAGED-SUMMARY-FIXTURE"));
    }
}
