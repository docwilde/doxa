//! Private owner-scoped server broker. No direct daemon RPC or public TCP bind.
mod state;
use bytes::Bytes;
use doxa_peers::remote_policy as policy;
use futures_util::stream;
use http_body_util::{BodyExt, Full, StreamBody, combinators::UnsyncBoxBody};
use hyper::{body::{Frame,Incoming}, header, Method, Request, Response, StatusCode};
use serde_json::{json, Value};
use state::{bounded_sessions, valid_id, Hub};
use std::{convert::Infallible, fs, io, os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt}, time::Duration,
    path::{Path,PathBuf}, sync::{Arc,Mutex}};
use tokio::net::{UnixListener,UnixStream};
use tokio::sync::{mpsc,Semaphore};

type Body=UnsyncBoxBody<Bytes,Infallible>;
fn response(status:StatusCode,content_type:&'static str,body:impl Into<Bytes>)->Response<Body>{
    Response::builder().status(status).header(header::CONTENT_TYPE,content_type)
        .header(header::CACHE_CONTROL,"no-store").header("x-content-type-options","nosniff")
        .header("content-security-policy","default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        .body(Full::new(body.into()).boxed_unsync()).expect("fixed response")
}
fn reply(status:StatusCode,value:Value)->Response<Body>{
    let bytes=serde_json::to_vec(&value).unwrap_or_default();
    response(status,"application/json",bytes)
}
fn bad(message:&str)->Response<Body>{reply(StatusCode::BAD_REQUEST,json!({"error":message}))}
fn refused(message:&str)->Response<Body>{reply(StatusCode::FORBIDDEN,json!({"error":message}))}
fn unavailable(message:&str)->Response<Body>{reply(StatusCode::CONFLICT,json!({"error":message}))}
fn same_origin(headers:&hyper::HeaderMap)->bool{
    let mut values=headers.get_all(header::ORIGIN).iter();
    let Some(origin)=values.next() else{return true};
    if values.next().is_some(){return false;}
    let Some(origin)=origin.to_str().ok() else{return false};
    let Some(host)=headers.get(header::HOST).and_then(|h|h.to_str().ok()) else{return false};
    origin==format!("https://{host}")||origin==format!("http://{host}")
}
fn auth(request:&Request<Incoming>,attested:bool,kind:&str)->Result<String,Response<Body>>{
    if request.headers().get_all("tailscale-user-login").iter().count()!=1{return Err(refused("exactly one Tailscale identity required"));}
    let login=request.headers().get("tailscale-user-login").and_then(|h|h.to_str().ok());
    let decision=policy::evaluate(kind,login,attested,None);
    if !decision.allowed{return Err(refused(&decision.reason));}
    if request.method()!=Method::GET&&!same_origin(request.headers()){return Err(refused("cross-origin write refused"));}
    Ok(login.expect("accepted by policy").trim().to_lowercase())
}
fn lease(request:&Request<Incoming>)->Option<String>{
    request.headers().get("x-doxa-host-lease").and_then(|h|h.to_str().ok()).filter(|s|s.len()==64&&s.bytes().all(|b|b.is_ascii_hexdigit())).map(str::to_owned)
}
async fn input(request:Request<Incoming>)->Result<Value,Response<Body>>{
    let headers=request.headers();
    if headers.contains_key(header::TRANSFER_ENCODING){return Err(bad("chunked requests refused"));}
    let size=headers.get(header::CONTENT_LENGTH).and_then(|h|h.to_str().ok()).and_then(|s|s.parse::<usize>().ok())
        .filter(|n|*n<=128_000).ok_or_else(||bad("bounded content length required"))?;
    let bytes=tokio::time::timeout(Duration::from_secs(10),http_body_util::Limited::new(request.into_body(),128_000).collect()).await
        .map_err(|_|reply(StatusCode::REQUEST_TIMEOUT,json!({"error":"request body timed out"})))?
        .map_err(|_|bad("body exceeds bound"))?.to_bytes();
    if bytes.len()!=size{return Err(bad("content length mismatch"));}
    let value:Value=serde_json::from_slice(&bytes).map_err(|_|bad("invalid JSON"))?;
    if !value.is_object(){return Err(bad("JSON object required"));}
    Ok(value)
}
fn sse(frame:&Value,id:Option<u64>)->Bytes{
    let mut result=String::new();if let Some(id)=id{result.push_str(&format!("id: {id}\n"));}
    result.push_str("data: ");result.push_str(&frame.to_string());result.push_str("\n\n");Bytes::from(result)
}
fn stream_body(receiver:mpsc::Receiver<Bytes>)->Body{
    let stream=stream::unfold(receiver,|mut receiver|async move{
        receiver.recv().await.map(|bytes|(Ok::<_,Infallible>(Frame::data(bytes)),receiver))
    });StreamBody::new(stream).boxed_unsync()
}
async fn handle(request:Request<Incoming>,state:Arc<Mutex<Hub>>,attested:bool)->Response<Body>{
    let method=request.method().clone();
    let path=request.uri().path().to_owned();
    let parts=path.trim_matches('/').split('/').collect::<Vec<_>>();
    let kind=match (method.clone(),parts.as_slice()){
        (Method::GET,["api","sessions"]|[]|["remote.js"]|["remote.css"])=>"read_status",
        (Method::GET,["api","commands",_]|["api","sessions",_,"events"])=>"read_transcript",
        (Method::POST,["api","sessions",_,"transcript"])=>"read_transcript",
        (Method::POST,["api","host","register"]|["api","host",_,"result"]|["api","host",_,"event"]|["api","host",_,"events"]|["api","host",_,"commands"]|["api","sessions",_,"prompt"])=>"send_prompt",
        (Method::POST,["api","sessions",_,"answer"])=>"approve_tool",
        _=>return reply(StatusCode::NOT_FOUND,json!({"error":"unknown hub route"})),
    };
    let owner=match auth(&request,attested,kind){Ok(owner)=>owner,Err(reply)=>return reply};
    let supplied_lease=lease(&request);
    let cursor=request.headers().get("last-event-id").and_then(|value|value.to_str().ok()).and_then(|raw|raw.parse::<u64>().ok()).and_then(|last|last.checked_add(1))
        .or_else(||request.uri().query().and_then(|query|query.strip_prefix("cursor=")).and_then(|raw|raw.parse::<u64>().ok())).unwrap_or(0);
    if method==Method::GET {
    if let ["api","sessions",id,"events"] = parts.as_slice() {
        let Some((host,session))=id.split_once('~').filter(|(host,session)|valid_id(host)&&valid_id(session)) else{return bad("invalid session target")};
        let (host,session)=(host.to_owned(),session.to_owned());
        let available=state.lock().ok().and_then(|mut hub|hub.history(&owner,&host,&session,cursor).ok());
        if available.is_none(){return unavailable("session offline");}
        let (sender,receiver)=mpsc::channel(64);
        tokio::spawn(async move{
            let hello=json!({"type":"hello","engine":"remote","model":null});
            if sender.send(sse(&hello,None)).await.is_err(){return;}
            let mut cursor=cursor;
            let mut quiet=0u8;
            loop{
                if !policy::evaluate("read_transcript",Some(&owner),true,None).allowed{break;}
                let batch=state.lock().ok().and_then(|mut hub|hub.history(&owner,&host,&session,cursor).ok());
                let Some(batch)=batch else{break};
                if batch["replay_gap"]==true {
                    let gap=json!({"type":"event","seq":cursor,"turn":null,"event":{"type":"replay_gap","data":{}}});
                    if sender.send(sse(&gap,None)).await.is_err(){break;}
                }
                for frame in batch["events"].as_array().into_iter().flatten(){
                    let seq=frame["seq"].as_u64();
                    if sender.send(sse(frame,seq)).await.is_err(){return;}
                    if let Some(next)=seq.and_then(|seq|seq.checked_add(1)){cursor=next;}
                }
                if batch["events"].as_array().is_some_and(|events|events.is_empty()) {
                    quiet=quiet.saturating_add(1);
                    if quiet>=15 {
                        if sender.send(Bytes::from_static(b": ping\n\n")).await.is_err(){break;}
                        quiet=0;
                    }
                } else { quiet=0; }
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        });
        return Response::builder().status(StatusCode::OK).header(header::CONTENT_TYPE,"text/event-stream")
            .header(header::CACHE_CONTROL,"no-store").header("x-accel-buffering","no")
            .body(stream_body(receiver)).expect("fixed SSE response");
    }
    }
    let body=if method==Method::POST{match input(request).await{Ok(body)=>body,Err(reply)=>return reply}}else{Value::Null};
    let mut state=match state.lock(){Ok(state)=>state,Err(_)=>return reply(StatusCode::SERVICE_UNAVAILABLE,json!({"error":"hub unavailable"}))};
    let result=match (method,parts.as_slice()){
        (Method::GET,[])=>return response(StatusCode::OK,"text/html; charset=utf-8",include_str!("../../doxa-remote/assets/index.html")),
        (Method::GET,["remote.js"])=>return response(StatusCode::OK,"text/javascript; charset=utf-8",include_str!("../../doxa-remote/assets/remote.js")),
        (Method::GET,["remote.css"])=>return response(StatusCode::OK,"text/css; charset=utf-8",include_str!("../../doxa-remote/assets/remote.css")),
        (Method::GET,["api","sessions"])=>Ok(state.list(&owner)),
        (Method::GET,["api","commands",id]) if valid_id(id)=>state.result(&owner,id),
        (Method::GET,["api","sessions",id,"events"])=>{
            match id.split_once('~'){Some((host,session)) if valid_id(host)&&valid_id(session)=>state.history(&owner,host,session,cursor),_=>Err("invalid session target")}
        },
        (Method::POST,["api","host","register"])=>{
            let Some(id)=body["host_id"].as_str() else{return bad("host id required")};
            let Some(sessions)=bounded_sessions(&body["sessions"]) else{return bad("invalid session inventory")};
            state.register(&owner,id,sessions,supplied_lease.as_deref()).map(|mut result|{result["owner"]=json!(owner);result})
        },
        (Method::POST,["api","host",id,"commands"]) if valid_id(id)=>{
            let Some(lease)=supplied_lease.as_deref() else{return refused("host lease required")};
            state.take(&owner,id,lease)
        },
        (Method::POST,["api","host",id,"result"]) if valid_id(id)=>{
            let Some(lease)=supplied_lease.as_deref() else{return refused("host lease required")};
            let Some(command)=body["command_id"].as_str().filter(|s|valid_id(s)) else{return bad("command id required")};
            state.complete(&owner,id,lease,command,body["result"].clone())
        },
        (Method::POST,["api","host",id,"event"]) if valid_id(id)=>{
            let Some(lease)=supplied_lease.as_deref() else{return refused("host lease required")};
            let Some(session)=body["session_id"].as_str().filter(|s|valid_id(s)) else{return bad("session id required")};
            state.event(&owner,id,lease,session,body["frame"].clone())
        },
        (Method::POST,["api","host",id,"events"]) if valid_id(id)=>{
            let Some(lease)=supplied_lease.as_deref() else{return refused("host lease required")};
            let Some(items)=body["items"].as_array().filter(|items|!items.is_empty()&&items.len()<=32) else{return bad("bounded event batch required")};
            if items.iter().any(|item|!item["session_id"].as_str().is_some_and(valid_id)
                || item["frame"]["type"]!="event" || item["frame"]["seq"].as_u64().is_none()){
                return bad("invalid event batch");
            }
            let mut result=Ok(json!({"accepted":items.len()}));
            for item in items{
                result=state.event(&owner,id,lease,item["session_id"].as_str().unwrap(),item["frame"].clone());
                if result.is_err(){break;}
            }
            result
        },
        (Method::POST,["api","sessions",id,op @ ("prompt"|"answer"|"transcript")])=>{
            let Some((host,session))=id.split_once('~').filter(|(h,s)|valid_id(h)&&valid_id(s)) else{return bad("invalid session target")};
            if *op=="prompt"&&!body["text"].as_str().is_some_and(|text|!text.trim().is_empty()&&text.len()<=58_000){return bad("invalid prompt");}
            if *op=="answer"&&(!body["id"].as_str().is_some_and(valid_id)||!body["answer"].is_object()){return bad("invalid answer");}
            state.enqueue(&owner,host,session,op,body)
        },
        _=>Err("unknown hub route"),
    };
    match result{Ok(value)=>reply(StatusCode::OK,value),Err(message)=>unavailable(message)}
}
fn private_runtime(path:&Path)->io::Result<()> {
    let meta=fs::symlink_metadata(path)?;
    if !path.is_absolute()||fs::canonicalize(path)?!=path||!meta.is_dir()||meta.uid()!=unsafe{libc::geteuid()}||meta.permissions().mode()&0o077!=0{
        return Err(io::Error::new(io::ErrorKind::PermissionDenied,"hub runtime must be private, canonical and owned"));
    }Ok(())
}
struct SocketGuard{path:PathBuf,inode:u64}
impl Drop for SocketGuard{fn drop(&mut self){if fs::symlink_metadata(&self.path).is_ok_and(|meta|meta.file_type().is_socket()&&meta.ino()==self.inode){let _=fs::remove_file(&self.path);}}}
#[tokio::main]
async fn main()->io::Result<()> {
    if !policy::remote_enabled(){return Err(io::Error::new(io::ErrorKind::PermissionDenied,"remote hub is off"));}
    if policy::setting("remote_allowed_logins","DOXA_REMOTE_ALLOWED_LOGINS").trim().is_empty(){return Err(io::Error::new(io::ErrorKind::PermissionDenied,"remote allow-list is empty"));}
    if policy::proxy_uid().is_none(){return Err(io::Error::new(io::ErrorKind::PermissionDenied,"unsafe proxy UID"));}
    let runtime=std::env::var_os("DOXA_HUB_RUNTIME_DIR").map(PathBuf::from)
        .ok_or_else(||io::Error::new(io::ErrorKind::InvalidInput,"DOXA_HUB_RUNTIME_DIR is required"))?;
    private_runtime(&runtime)?;
    let path=runtime.join("hub.sock");
    if fs::symlink_metadata(&path).is_ok(){return Err(io::Error::new(io::ErrorKind::AlreadyExists,"hub socket already exists"));}
    let listener=UnixListener::bind(&path)?;
    fs::set_permissions(&path,fs::Permissions::from_mode(0o600))?;
    let _socket=SocketGuard{path:path.clone(),inode:fs::symlink_metadata(&path)?.ino()};
    let state=Arc::new(Mutex::new(Hub::new()));
    eprintln!("doxa-hub: private socket {}",path.display());
    let mut term=tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let slots=Arc::new(Semaphore::new(64));
    loop{
        let (stream,_)=tokio::select!{r=listener.accept()=>r?,_=tokio::signal::ctrl_c()=>break,_=term.recv()=>break};
        let Ok(permit)=slots.clone().try_acquire_owned() else{continue};
        let state=state.clone();
        tokio::spawn(async move{
            let _permit=permit;
            let std_stream=match stream.into_std(){Ok(stream)=>stream,Err(_)=>return};
            let attested=doxa_peers::credentials::peer_uid(&std_stream).is_ok_and(|uid|Some(uid)==policy::proxy_uid());
            let _=std_stream.set_nonblocking(true);
            let stream=match UnixStream::from_std(std_stream){Ok(stream)=>stream,Err(_)=>return};
            let service=hyper::service::service_fn(move |request|{let state=state.clone();async move{Ok::<_,Infallible>(handle(request,state,attested).await)}});
            let mut server=hyper::server::conn::http1::Builder::new();
            server.timer(hyper_util::rt::TokioTimer::new()).header_read_timeout(Duration::from_secs(3))
                .keep_alive(false).max_headers(32).max_buf_size(8192);
            let _=server.serve_connection(hyper_util::rt::TokioIo::new(stream),service).await;
        });
    }
    Ok(())
}

#[cfg(test)]mod tests{
    use super::*;
    use tokio::io::{AsyncReadExt,AsyncWriteExt};
    static ENV:std::sync::Mutex<()> = std::sync::Mutex::new(());
    async fn wire(state:Arc<Mutex<Hub>>,attested:bool,raw:String)->String{
        let (server,mut client)=UnixStream::pair().unwrap();
        let task=tokio::spawn(async move{
            let service=hyper::service::service_fn(move |request|{
                let state=state.clone();async move{Ok::<_,Infallible>(handle(request,state,attested).await)}
            });
            let mut builder=hyper::server::conn::http1::Builder::new();builder.keep_alive(false);
            builder.serve_connection(hyper_util::rt::TokioIo::new(server),service).await.unwrap();
        });
        client.write_all(raw.as_bytes()).await.unwrap();
        let mut response=Vec::new();client.read_to_end(&mut response).await.unwrap();task.await.unwrap();
        String::from_utf8(response).unwrap()
    }
    fn request(path:&str,body:&str,identity:&str,extra:&str)->String{
        format!("POST {path} HTTP/1.1\r\nHost: hub.test\r\nOrigin: https://hub.test\r\nTailscale-User-Login: {identity}\r\nContent-Length: {}\r\n{extra}\r\n{body}",body.len())
    }
    fn json_body(reply:&str)->Value{serde_json::from_str(reply.split("\r\n\r\n").nth(1).unwrap()).unwrap()}
    #[tokio::test]async fn http_rejects_forged_identity_then_brokers_exact_owner(){
        let _guard=ENV.lock().unwrap();
        let old_enabled=std::env::var_os("DOXA_REMOTE_ENABLED");
        let old_logins=std::env::var_os("DOXA_REMOTE_ALLOWED_LOGINS");
        std::env::set_var("DOXA_REMOTE_ENABLED","1");
        std::env::set_var("DOXA_REMOTE_ALLOWED_LOGINS","owner@example.com");
        let state=Arc::new(Mutex::new(Hub::new()));
        let body=r#"{"host_id":"workstation","sessions":[{"id":"s1","title":"Session"}]}"#;
        let raw=request("/api/host/register",body,"owner@example.com","");
        assert!(wire(state.clone(),false,raw.clone()).await.starts_with("HTTP/1.1 403"));
        assert!(wire(state.clone(),true,request("/api/host/register",body,"owner@example.com","Tailscale-User-Login: attacker@example.com\r\n")).await.starts_with("HTTP/1.1 403"));
        let result=wire(state.clone(),true,raw).await;
        assert!(result.starts_with("HTTP/1.1 200"),"{result}");
        let lease=json_body(&result)["lease"].as_str().unwrap().to_owned();
        let prompt=r#"{"text":"hello"}"#;
        let result=wire(state.clone(),true,request("/api/sessions/workstation~s1/prompt",prompt,"owner@example.com","")).await;
        let id=json_body(&result)["command_id"].as_str().unwrap().to_owned();
        assert!(result.starts_with("HTTP/1.1 200"));
        let snapshot=wire(state.clone(),true,request("/api/sessions/workstation~s1/transcript","{}","owner@example.com","")).await;
        let snapshot_id=json_body(&snapshot)["command_id"].as_str().unwrap().to_owned();
        assert!(snapshot.starts_with("HTTP/1.1 200"));
        let result=wire(state,true,request("/api/host/workstation/commands","{}","owner@example.com",&format!("X-DOXA-Host-Lease: {lease}\r\n"))).await;
        assert_eq!(json_body(&result)["commands"][0]["command_id"],id);
        assert_eq!(json_body(&result)["commands"][1]["command_id"],snapshot_id);
        assert_eq!(json_body(&result)["commands"][1]["op"],"transcript");
        if let Some(value)=old_enabled{std::env::set_var("DOXA_REMOTE_ENABLED",value)}else{std::env::remove_var("DOXA_REMOTE_ENABLED")};
        if let Some(value)=old_logins{std::env::set_var("DOXA_REMOTE_ALLOWED_LOGINS",value)}else{std::env::remove_var("DOXA_REMOTE_ALLOWED_LOGINS")};
    }
}
