//! Private owner-scoped server broker. No direct daemon RPC or public TCP bind.
mod state;
mod push;
mod fcm;
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
fn extension_origin_with_allowlist(headers:&hyper::HeaderMap,allowlist:&str)->Option<String>{
    let mut values=headers.get_all(header::ORIGIN).iter();
    let origin=values.next()?.to_str().ok()?;
    if values.next().is_some(){return None;}
    let id=origin.strip_prefix("chrome-extension://")?;
    if id.len()!=32||!id.bytes().all(|c|(b'a'..=b'p').contains(&c)){return None;}
    allowlist.split(',').map(str::trim).any(|allowed|allowed==origin).then(||origin.to_owned())
}
fn extension_origin(headers:&hyper::HeaderMap)->Option<String>{
    extension_origin_with_allowlist(headers,
        &policy::setting("remote_extension_origins","DOXA_REMOTE_EXTENSION_ORIGINS"))
}
fn extension_headers(reply:&mut Response<Body>,origin:Option<&str>){
    if let Some(origin)=origin{
        if let Ok(value)=hyper::header::HeaderValue::from_str(origin){
            reply.headers_mut().insert(header::ACCESS_CONTROL_ALLOW_ORIGIN,value);
            reply.headers_mut().insert(header::ACCESS_CONTROL_ALLOW_CREDENTIALS,hyper::header::HeaderValue::from_static("true"));
            reply.headers_mut().insert(header::VARY,hyper::header::HeaderValue::from_static("Origin"));
        }
    }
}
fn same_origin(headers:&hyper::HeaderMap)->bool{
    let mut values=headers.get_all(header::ORIGIN).iter();
    let Some(origin)=values.next() else{return true};
    if values.next().is_some(){return false;}
    let Some(origin)=origin.to_str().ok() else{return false};
    let Some(host)=headers.get(header::HOST).and_then(|h|h.to_str().ok()) else{return false};
    origin==format!("https://{host}")||origin==format!("http://{host}")
        ||extension_origin(headers).is_some()
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
async fn forward_stream(state:Arc<Mutex<Hub>>,sender:mpsc::Sender<Bytes>,owner:String,
    host:String,session:String,mut cursor:u64,identity:(Value,Value)){
    let hello=json!({"type":"hello","engine":"remote","model":null,"incarnation":identity.0});
    if sender.send(sse(&hello,None)).await.is_err(){return;}
    let mut quiet=0u8;
    loop{
        if !policy::evaluate("read_transcript",Some(&owner),true,None).allowed{break;}
        let batch=state.lock().ok().and_then(|mut hub|hub.history(&owner,&host,&session,cursor).ok());
        let Some(batch)=batch else{break};
        if batch["replay_gap"]==true || batch["incarnation"]!=identity.0 || batch["encrypted"]!=identity.1 {
            let gap=json!({"type":"event","seq":cursor,"turn":null,"event":{"type":"replay_gap","data":{}}});
            let _=sender.send(sse(&gap,None)).await;
            // Clients must obtain a new host snapshot before consuming events
            // or answering a request from a different state.
            break;
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
        } else {quiet=0;}
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}
async fn handle(request:Request<Incoming>,state:Arc<Mutex<Hub>>,attested:bool,push:Option<Arc<push::Runtime>>,fcm:Option<Arc<fcm::Runtime>>)->Response<Body>{
    let origin=extension_origin(request.headers());
    if request.method()==Method::OPTIONS {
        let allowed=origin.is_some()
            && matches!(request.headers().get(header::ACCESS_CONTROL_REQUEST_METHOD).and_then(|v|v.to_str().ok()),Some("GET"|"POST"))
            && request.headers().get(header::ACCESS_CONTROL_REQUEST_HEADERS).and_then(|v|v.to_str().ok())
                .is_none_or(|v|v.eq_ignore_ascii_case("content-type"));
        if !allowed{return refused("extension origin is not configured");}
        let mut reply=response(StatusCode::NO_CONTENT,"text/plain",Bytes::new());
        extension_headers(&mut reply,origin.as_deref());
        reply.headers_mut().insert(header::ACCESS_CONTROL_ALLOW_METHODS,hyper::header::HeaderValue::from_static("GET, POST"));
        reply.headers_mut().insert(header::ACCESS_CONTROL_ALLOW_HEADERS,hyper::header::HeaderValue::from_static("content-type"));
        return reply;
    }
    let mut reply=handle_inner(request,state,attested,push,fcm).await;
    extension_headers(&mut reply,origin.as_deref());
    reply
}
async fn handle_inner(request:Request<Incoming>,state:Arc<Mutex<Hub>>,attested:bool,push:Option<Arc<push::Runtime>>,fcm:Option<Arc<fcm::Runtime>>)->Response<Body>{
    let method=request.method().clone();
    let path=request.uri().path().to_owned();
    let parts=path.trim_matches('/').split('/').collect::<Vec<_>>();
    let kind=match (method.clone(),parts.as_slice()){
        (Method::GET,["api","sessions"]|["api","push","config"]|[]|["remote.js"]|["remote.css"]|["remote-sw.js"])=>"read_status",
        (Method::POST|Method::DELETE,["api","push","subscriptions"]|["api","push","android"])=>"read_status",
        (Method::GET,["api","commands",_]|["api","sessions",_,"events"])=>"read_transcript",
        (Method::POST,["api","sessions",_,"transcript"])=>"read_transcript",
        (Method::POST,["api","android","requests",_,"fence"])=>"read_transcript",
        (Method::POST,["api","android","sessions",_,"prompt"])=>"send_prompt",
        (Method::POST,["api","android","sessions",_,"answer"])=>"approve_tool",
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
        let Some(available)=available else{return unavailable("session offline");};
        let identity=(available["incarnation"].clone(),available["encrypted"].clone());
        let (sender,receiver)=mpsc::channel(64);
        tokio::spawn(forward_stream(state,sender,owner,host,session,cursor,identity));
        return Response::builder().status(StatusCode::OK).header(header::CONTENT_TYPE,"text/event-stream")
            .header(header::CACHE_CONTROL,"no-store").header("x-accel-buffering","no")
            .body(stream_body(receiver)).expect("fixed SSE response");
    }
    }
    let body=if method==Method::POST||method==Method::DELETE{match input(request).await{Ok(body)=>body,Err(reply)=>return reply}}else{Value::Null};
    let hub_state=state.clone();
    let mut state=match state.lock(){Ok(state)=>state,Err(_)=>return reply(StatusCode::SERVICE_UNAVAILABLE,json!({"error":"hub unavailable"}))};
    let result=match (method,parts.as_slice()){
        (Method::GET,[])=>return response(StatusCode::OK,"text/html; charset=utf-8",include_str!("../../doxa-remote/assets/index.html")),
        (Method::GET,["remote.js"])=>return response(StatusCode::OK,"text/javascript; charset=utf-8",include_str!("../../doxa-remote/assets/remote.js")),
        (Method::GET,["remote.css"])=>return response(StatusCode::OK,"text/css; charset=utf-8",include_str!("../../doxa-remote/assets/remote.css")),
        (Method::GET,["remote-sw.js"])=>return response(StatusCode::OK,"text/javascript; charset=utf-8",include_str!("../../doxa-remote/assets/remote-sw.js")),
        (Method::GET,["api","sessions"])=>Ok(state.inventory(&owner)),
        (Method::GET,["api","push","config"])=>Ok(match push.as_ref(){
            Some(push)=>json!({"enabled":true,"public_key":push.public_key()}),
            None=>json!({"enabled":false}),
        }),
        (Method::POST,["api","push","subscriptions"])=>{
            if push.is_none(){return unavailable("background push is disabled")}
            match push::subscription(&body){Ok(subscription)=>state.subscribe(&owner,subscription),Err(_)=>Err("invalid push subscription")}
        },
        (Method::DELETE,["api","push","subscriptions"])=>{
            if push.is_none(){return unavailable("background push is disabled")}
            match body["endpoint"].as_str().filter(|endpoint|endpoint.len()<=2048){
                Some(endpoint)=>Ok(state.unsubscribe(&owner,endpoint)),None=>Err("push endpoint required")}
        },
        (Method::POST,["api","push","android"])=>{
            if fcm.is_none(){return unavailable("Android push is disabled")}
            match (body["target"].as_str(),body["incarnation"].as_str(),body["token"].as_str(),body["tag"].as_str()) {
                (Some(target),Some(incarnation),Some(token),Some(tag))=>state.subscribe_android(&owner,target,incarnation,token,tag),
                _=>Err("Android subscription fields required")
            }
        },
        (Method::DELETE,["api","push","android"])=>{
            if fcm.is_none(){return unavailable("Android push is disabled")}
            match body["token"].as_str().filter(|token|fcm::valid_token(token)) {
                Some(token)=>Ok(state.unsubscribe_android(&owner,token)),None=>Err("Android token required")
            }
        },
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
            let encrypted=state.list(&owner)["sessions"].as_array().is_some_and(|rows|rows.iter().any(|row|row["id"]==*id&&row["encrypted"]==true));
            if encrypted && !body["sealed"].is_object() { return bad("encrypted command required"); }
            if !encrypted && *op=="prompt"&&!body["text"].as_str().is_some_and(|text|!text.trim().is_empty()&&text.len()<=58_000){return bad("invalid prompt");}
            if !encrypted && *op=="answer"&&(!body["id"].as_str().is_some_and(valid_id)||!body["answer"].is_object()){return bad("invalid answer");}
            state.enqueue(&owner,host,session,op,body)
        },
        (Method::POST,["api","android","requests",request_id,"fence"])=>{
            match (body["target"].as_str(),body["operation"].as_str(),body["incarnation"].as_str()) {
                (Some(target),Some(op),Some(incarnation))=>state.fence_android(&owner,request_id,target,op,incarnation),
                _=>Err("Android fence scope required"),
            }
        },
        (Method::POST,["api","android","sessions",id,op @ ("prompt"|"answer")])=>{
            let Some((host,session))=id.split_once('~').filter(|(h,s)|valid_id(h)&&valid_id(s)) else{return bad("invalid session target")};
            let encrypted=state.list(&owner)["sessions"].as_array().is_some_and(|rows|rows.iter().any(|row|row["id"]==*id&&row["encrypted"]==true));
            if encrypted && !body["sealed"].is_object() { return bad("encrypted command required"); }
            if !encrypted && body.get("sealed").is_some(){return bad("unexpected sealed command");}
            if !encrypted && *op=="prompt"&&!body["text"].as_str().is_some_and(|text|!text.trim().is_empty()&&text.len()<=58_000){return bad("invalid prompt");}
            if !encrypted && *op=="answer"&&(!body["id"].as_str().is_some_and(valid_id)||!body["answer"].is_object()){return bad("invalid answer");}
            state.enqueue_android(&owner,host,session,op,body)
        },
        _=>Err("unknown hub route"),
    };
    let deliveries=state.take_push();
    let android_deliveries=state.take_android();
    drop(state);
    if let Some(push)=push{
        for (owner,subscription,kind) in deliveries{
            let Some(permit)=push.permit() else{continue};
            let push=push.clone();let state=hub_state.clone();
            tokio::spawn(async move{
                if push.send(&subscription,kind,permit).await{
                    if let Ok(mut hub)=state.lock(){hub.unsubscribe(&owner,&subscription.endpoint);}
                }
            });
        }
    }
    if let Some(fcm)=fcm {
        for (owner,token,tag,kind) in android_deliveries {
            let Some(permit)=fcm.permit() else {continue};
            let fcm=fcm.clone();let state=hub_state.clone();
            tokio::spawn(async move {
                if fcm.send(&token,&tag,kind,permit).await {
                    if let Ok(mut hub)=state.lock(){hub.unsubscribe_android(&owner,&token);}
                }
            });
        }
    }
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
    let runtime=std::env::var_os("DOXA_HUB_RUNTIME_DIR").map(PathBuf::from)
        .ok_or_else(||io::Error::new(io::ErrorKind::InvalidInput,"DOXA_HUB_RUNTIME_DIR is required"))?;
    private_runtime(&runtime)?;
    let args=std::env::args().skip(1).collect::<Vec<_>>();
    if args==["push-keygen"]{
        println!("VAPID public key: {}",push::generate(&runtime)?);
        return Ok(());
    }
    if !args.is_empty(){return Err(io::Error::new(io::ErrorKind::InvalidInput,"usage: doxa-hub [push-keygen]"));}
    if !policy::remote_enabled(){return Err(io::Error::new(io::ErrorKind::PermissionDenied,"remote hub is off"));}
    if policy::setting("remote_allowed_logins","DOXA_REMOTE_ALLOWED_LOGINS").trim().is_empty(){return Err(io::Error::new(io::ErrorKind::PermissionDenied,"remote allow-list is empty"));}
    if policy::proxy_uid().is_none(){return Err(io::Error::new(io::ErrorKind::PermissionDenied,"unsafe proxy UID"));}
    let push=push::Runtime::load(&runtime)?.map(Arc::new);
    let fcm=fcm::Runtime::load(&runtime)?.map(Arc::new);
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
        let push=push.clone();
        let fcm=fcm.clone();
        tokio::spawn(async move{
            let _permit=permit;
            let std_stream=match stream.into_std(){Ok(stream)=>stream,Err(_)=>return};
            let attested=doxa_peers::credentials::peer_uid(&std_stream).is_ok_and(|uid|Some(uid)==policy::proxy_uid());
            let _=std_stream.set_nonblocking(true);
            let stream=match UnixStream::from_std(std_stream){Ok(stream)=>stream,Err(_)=>return};
            let push=push.clone();
            let service=hyper::service::service_fn(move |request|{let state=state.clone();let push=push.clone();let fcm=fcm.clone();async move{Ok::<_,Infallible>(handle(request,state,attested,push,fcm).await)}});
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
    #[test] fn packaged_extension_origin_requires_exact_configured_id(){
        let id="a".repeat(32);
        let approved=format!("chrome-extension://{id}");
        let mut headers=hyper::HeaderMap::new();
        headers.insert(header::ORIGIN,approved.parse().unwrap());
        assert_eq!(extension_origin_with_allowlist(&headers,&approved),Some(approved.clone()));
        assert_eq!(extension_origin_with_allowlist(&headers,""),None);
        assert_eq!(extension_origin_with_allowlist(&headers,"chrome-extension://bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),None);
        headers.insert(header::ORIGIN,"chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaq".parse().unwrap());
        assert_eq!(extension_origin_with_allowlist(&headers,&approved),None);
        headers.insert(header::ORIGIN,"https://hub.ts.net".parse().unwrap());
        assert_eq!(extension_origin_with_allowlist(&headers,"https://hub.ts.net"),None);
    }
    async fn wire(state:Arc<Mutex<Hub>>,attested:bool,raw:String)->String{
        let (server,mut client)=UnixStream::pair().unwrap();
        let task=tokio::spawn(async move{
            let service=hyper::service::service_fn(move |request|{
                let state=state.clone();async move{Ok::<_,Infallible>(handle(request,state,attested,None,None).await)}
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
    #[tokio::test]async fn active_stream_fences_replacement_before_sending_lower_sequence(){
        let _guard=ENV.lock().unwrap();
        let vars=["DOXA_REMOTE_ENABLED","DOXA_REMOTE_ALLOWED_LOGINS"];
        let old=vars.map(std::env::var_os);
        std::env::set_var(vars[0],"1");std::env::set_var(vars[1],"owner@example.com");
        let mut hub=Hub::new();
        let sessions=state::bounded_sessions(&json!([{"id":"session","incarnation":"first"}])).unwrap();
        let lease=hub.register("owner@example.com","host",sessions,None).unwrap()["lease"].as_str().unwrap().to_owned();
        hub.event("owner@example.com","host",&lease,"session",json!({"type":"event","seq":30,"event":{"type":"turn_done","data":{}}})).unwrap();
        let state=Arc::new(Mutex::new(hub));
        let (sender,mut receiver)=mpsc::channel(8);
        let task=tokio::spawn(forward_stream(state.clone(),sender,"owner@example.com".into(),"host".into(),"session".into(),31,(json!("first"),json!(false))));
        let hello=receiver.recv().await.unwrap();
        assert!(String::from_utf8(hello.to_vec()).unwrap().contains("\"incarnation\":\"first\""));
        {
            let mut hub=state.lock().unwrap();
            let replacement=state::bounded_sessions(&json!([{"id":"session","incarnation":"second"}])).unwrap();
            hub.register("owner@example.com","host",replacement,Some(&lease)).unwrap();
            hub.event("owner@example.com","host",&lease,"session",json!({"type":"event","seq":0,"event":{"type":"needs_input","data":{"id":"new-question"}}})).unwrap();
        }
        let gap=tokio::time::timeout(Duration::from_secs(3),receiver.recv()).await.unwrap().unwrap();
        let gap=String::from_utf8(gap.to_vec()).unwrap();
        assert!(gap.contains("replay_gap"));assert!(!gap.contains("new-question"));
        assert!(!gap.starts_with("id:"));
        assert!(receiver.recv().await.is_none());task.await.unwrap();
        for (name,original) in vars.into_iter().zip(old){
            if let Some(value)=original{std::env::set_var(name,value)}else{std::env::remove_var(name)}
        }
    }
    #[tokio::test]async fn extension_preflight_is_scoped_and_post_still_requires_proxy_identity(){
        let _guard=ENV.lock().unwrap();
        let vars=["DOXA_REMOTE_ENABLED","DOXA_REMOTE_ALLOWED_LOGINS","DOXA_REMOTE_EXTENSION_ORIGINS"];
        let old=vars.map(std::env::var_os);
        let origin=format!("chrome-extension://{}","a".repeat(32));
        std::env::set_var(vars[0],"1");
        std::env::set_var(vars[1],"owner@example.com");
        std::env::set_var(vars[2],&origin);
        let state=Arc::new(Mutex::new(Hub::new()));
        let preflight=format!("OPTIONS /api/sessions/host~session/prompt HTTP/1.1\r\nHost: hub.test\r\nOrigin: {origin}\r\nAccess-Control-Request-Method: POST\r\nAccess-Control-Request-Headers: content-type\r\n\r\n");
        let allowed=wire(state.clone(),true,preflight).await;
        assert!(allowed.starts_with("HTTP/1.1 204"),"{allowed}");
        assert!(allowed.to_lowercase().contains(&format!("access-control-allow-origin: {origin}")));
        let denied=wire(state.clone(),true,format!("OPTIONS /api/sessions/host~session/prompt HTTP/1.1\r\nHost: hub.test\r\nOrigin: chrome-extension://{}\r\nAccess-Control-Request-Method: POST\r\n\r\n","b".repeat(32))).await;
        assert!(denied.starts_with("HTTP/1.1 403"));
        let command=format!("POST /api/sessions/host~session/prompt HTTP/1.1\r\nHost: hub.test\r\nOrigin: {origin}\r\nTailscale-User-Login: owner@example.com\r\nContent-Length: 2\r\n\r\n{{}}");
        assert!(wire(state.clone(),false,command.clone()).await.starts_with("HTTP/1.1 403"));
        assert!(wire(state,true,command).await.starts_with("HTTP/1.1 400"));
        for (name,original) in vars.into_iter().zip(old){
            if let Some(value)=original{std::env::set_var(name,value)}else{std::env::remove_var(name)}
        }
    }
    #[tokio::test]async fn http_rejects_forged_identity_then_brokers_exact_owner(){
        let _guard=ENV.lock().unwrap();
        let old_enabled=std::env::var_os("DOXA_REMOTE_ENABLED");
        let old_logins=std::env::var_os("DOXA_REMOTE_ALLOWED_LOGINS");
        std::env::set_var("DOXA_REMOTE_ENABLED","1");
        std::env::set_var("DOXA_REMOTE_ALLOWED_LOGINS","owner@example.com");
        let state=Arc::new(Mutex::new(Hub::new()));
        let config=wire(state.clone(),true,
            "GET /api/push/config HTTP/1.1\r\nHost: hub.test\r\nTailscale-User-Login: owner@example.com\r\n\r\n".into()).await;
        assert_eq!(json_body(&config)["enabled"],false);
        assert!(wire(state.clone(),true,request("/api/push/subscriptions","{}","owner@example.com","")).await.starts_with("HTTP/1.1 409"));
        assert!(wire(state.clone(),true,request("/api/push/android","{}","owner@example.com","")).await.starts_with("HTTP/1.1 409"));
        assert!(wire(state.clone(),false,request("/api/push/android","{}","owner@example.com","")).await.starts_with("HTTP/1.1 403"));
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
    #[tokio::test]async fn android_routes_require_scoped_boot_and_fence_before_host_take(){
        let _guard=ENV.lock().unwrap();
        let old_enabled=std::env::var_os("DOXA_REMOTE_ENABLED");
        let old_logins=std::env::var_os("DOXA_REMOTE_ALLOWED_LOGINS");
        std::env::set_var("DOXA_REMOTE_ENABLED","1");
        std::env::set_var("DOXA_REMOTE_ALLOWED_LOGINS","owner@example.com");
        let state=Arc::new(Mutex::new(Hub::new()));
        let register=request("/api/host/register",r#"{"host_id":"host","sessions":[{"id":"session","incarnation":"v1"}]}"#,"owner@example.com","");
        let registered=wire(state.clone(),true,register).await;
        let lease=json_body(&registered)["lease"].as_str().unwrap().to_owned();
        let inventory=wire(state.clone(),true,"GET /api/sessions HTTP/1.1\r\nHost: hub.test\r\nTailscale-User-Login: owner@example.com\r\n\r\n".into()).await;
        let boot=json_body(&inventory)["hub_boot"].as_str().unwrap().to_owned();
        assert_eq!(boot.len(),32);
        let id=format!("{}-{}",boot,uuid::Uuid::new_v4());
        let fence=json!({"target":"host~session","operation":"prompt","incarnation":"v1"}).to_string();
        let path=format!("/api/android/requests/{id}/fence");
        let fenced=wire(state.clone(),true,request(&path,&fence,"owner@example.com","")).await;
        assert_eq!(json_body(&fenced),json!({"status":"absent_fenced","safe_to_clear":true}));
        let body=json!({"text":"late","request_id":id,"hub_boot":boot,"incarnation":"v1","issued_at":1}).to_string();
        assert!(wire(state.clone(),true,request("/api/android/sessions/host~session/prompt",&body,"owner@example.com","")).await.starts_with("HTTP/1.1 409"));
        assert!(wire(state.clone(),true,request("/api/sessions/host~session/prompt",&body,"owner@example.com","")).await.starts_with("HTTP/1.1 409"));
        let taken=wire(state.clone(),true,request("/api/host/host/commands","{}","owner@example.com",&format!("X-DOXA-Host-Lease: {lease}\r\n"))).await;
        assert!(json_body(&taken)["commands"].as_array().unwrap().is_empty());
        let invalid=json!({"text":"invalid","request_id":format!("{}-{}",boot,uuid::Uuid::new_v4()),"incarnation":"v1"}).to_string();
        assert!(wire(state,true,request("/api/android/sessions/host~session/prompt",&invalid,"owner@example.com","")).await.starts_with("HTTP/1.1 409"));
        if let Some(value)=old_enabled{std::env::set_var("DOXA_REMOTE_ENABLED",value)}else{std::env::remove_var("DOXA_REMOTE_ENABLED")};
        if let Some(value)=old_logins{std::env::set_var("DOXA_REMOTE_ALLOWED_LOGINS",value)}else{std::env::remove_var("DOXA_REMOTE_ALLOWED_LOGINS")};
    }
}
