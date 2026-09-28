//! Bounded native cross-machine peer wire. Only kernel-attested Unix proxy
//! connections may supply identity; forwarded headers never prove identity.
use crate::{remote_policy as policy, delivery::MAX_FRAME_BYTES};
use serde_json::{json, Value};
use std::{fs, io::{self,Read,Write}, os::{fd::AsRawFd,unix::{fs::{FileTypeExt,MetadataExt,OpenOptionsExt,PermissionsExt},net::{UnixListener,UnixStream},process::CommandExt}}, path::{Path,PathBuf}, process::{Command,Stdio}, time::{Duration,Instant}};

const MAX_HEAD: usize = 4096;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
pub const BRIDGE_IDLE: Duration = Duration::from_secs(75);
fn invalid(reason: &'static str) -> io::Error { io::Error::new(io::ErrorKind::InvalidData,reason) }

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Endpoint { pub label:String, pub host:String, pub port:u16 }
impl Endpoint {
    pub fn parse(entry: &str) -> io::Result<Self> {
        let (label,target) = entry.trim().split_once('=').ok_or_else(||invalid("endpoint needs label=host:port"))?;
        let (host,port) = target.trim().split_once(':').unwrap_or((target.trim(),"47600"));
        let label = label.trim(); let host = host.trim();
        if label.is_empty() || label.len()>64 || label.chars().any(char::is_control) || host.is_empty() || host.len()>253
            || !host.bytes().all(|b|b.is_ascii_alphanumeric() || b".-".contains(&b)) || host.starts_with('.') || host.ends_with('.')
            || host.split('.').any(|part|part.is_empty()||part.starts_with('-')||part.ends_with('-')) { return Err(invalid("invalid remote endpoint")); }
        let port = port.trim().parse().ok().filter(|port|*port>0).ok_or_else(||invalid("invalid endpoint port"))?;
        Ok(Self { label:label.into(),host:host.into(),port })
    }
}
pub fn endpoints() -> Vec<Endpoint> {
    policy::setting("remote_peers","DOXA_REMOTE_PEERS").split(',').take(128).filter_map(|entry|Endpoint::parse(entry).ok()).collect()
}

/// No redirects, proxy credentials, or caller-set identity header. Tailscale
/// Serve supplies its own identity at the receiving trusted Unix connection.
pub fn request(endpoint: &Endpoint, body: &Value) -> io::Result<Value> {
    let payload = serde_json::to_vec(body)?;
    if !body.is_object() || payload.len()>MAX_FRAME_BYTES { return Err(invalid("remote peer request exceeds bound")); }
    // Revalidate even programmatically constructed endpoints before any dial.
    Endpoint::parse(&format!("{}={}:{}",endpoint.label,endpoint.host,endpoint.port))?;
    let client = reqwest::blocking::Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(5)).timeout(Duration::from_secs(5)).build().map_err(|_|invalid("remote client unavailable"))?;
    let mut response = client.post(format!("http://{}:{}/peers",endpoint.host,endpoint.port)).header("Content-Type","application/json").body(payload)
        .send().map_err(|_|invalid("remote peer connection failed"))?;
    let status = response.status(); let mut raw = Vec::new(); response.by_ref().take(MAX_FRAME_BYTES as u64+1).read_to_end(&mut raw)?;
    if raw.len()>MAX_FRAME_BYTES { return Err(invalid("remote peer reply exceeds bound")); }
    let answer: Value = serde_json::from_slice(&raw).map_err(|_|invalid("unreadable remote peer reply"))?;
    if !status.is_success() || answer["ok"] != true { return Err(io::Error::new(io::ErrorKind::PermissionDenied,"remote peer request refused")); }
    Ok(answer)
}
pub fn fetch_roster(endpoint: &Endpoint) -> io::Result<Vec<Value>> {
    let answer = request(endpoint,&json!({"op":"roster"}))?;
    let rows = answer["peers"].as_array().ok_or_else(||invalid("invalid remote peer roster"))?;
    if rows.len()>crate::MAX_REGISTRY_ENTRIES { return Err(invalid("remote peer roster exceeds bound")); }
    Ok(rows.iter().filter(|row|row["session_id"].as_str().is_some_and(crate::safe_id)).map(|row| {
        let mut row=row.clone(); row["origin"]=json!(endpoint.label); row["socket_path"]=json!("");row["pid"]=json!(0);row["daemon_socket"]=Value::Null; row
    }).collect())
}
/// Endpoints are independently bounded and queried together; one unavailable
/// machine cannot multiply the caller's five-second network wait by the list.
pub fn rosters() -> Vec<(Endpoint, io::Result<Vec<Value>>)> {
    std::thread::scope(|scope| {
        let pending=endpoints().into_iter().map(|endpoint| {
            let worker=scope.spawn(move || {let result=fetch_roster(&endpoint);(endpoint,result)});worker
        }).collect::<Vec<_>>();
        pending.into_iter().filter_map(|worker|worker.join().ok()).collect()
    })
}

fn credentials(stream: &UnixStream) -> io::Result<libc::ucred> {
    let mut cred = libc::ucred { pid:0,uid:0,gid:0 }; let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe { libc::getsockopt(stream.as_raw_fd(),libc::SOL_SOCKET,libc::SO_PEERCRED,(&mut cred as *mut libc::ucred).cast(),&mut len) } != 0
        || len as usize != std::mem::size_of::<libc::ucred>() { return Err(invalid("Unix proxy credentials unavailable")); }
    Ok(cred)
}
pub fn attested_proxy(stream: &UnixStream) -> bool { credentials(stream).is_ok_and(|cred|Some(cred.uid)==policy::proxy_uid()) }

async fn parse_request(request:hyper::Request<hyper::body::Incoming>)->io::Result<(Value,Option<String>)> {
    use http_body_util::BodyExt;
    if request.method()!=hyper::Method::POST || request.uri().path_and_query().map(|v|v.as_str())!=Some("/peers") {
        return Err(invalid("remote bridge serves only POST /peers"));
    }
    let headers=request.headers();
    if headers.iter().map(|(key,value)|key.as_str().len()+value.as_bytes().len()+4).sum::<usize>()>MAX_HEAD {
        return Err(invalid("remote HTTP headers exceed bound"));
    }
    if headers.contains_key(hyper::header::TRANSFER_ENCODING) || headers.get_all("tailscale-user-login").iter().count()>1 {
        return Err(invalid("unsupported body encoding or duplicate identity"));
    }
    let length=headers.get(hyper::header::CONTENT_LENGTH).and_then(|v|v.to_str().ok()).and_then(|v|v.parse::<usize>().ok())
        .filter(|size|*size<=MAX_FRAME_BYTES).ok_or_else(||invalid("bounded HTTP content length required"))?;
    let login=headers.get("tailscale-user-login").map(|v|v.to_str().map(str::to_owned)).transpose().map_err(|_|invalid("invalid identity header"))?;
    let bytes=http_body_util::Limited::new(request.into_body(),MAX_FRAME_BYTES).collect().await.map_err(|_|invalid("remote HTTP body exceeds bound"))?.to_bytes();
    if bytes.len()!=length {return Err(invalid("remote HTTP body length mismatch"));}
    let body:Value=serde_json::from_slice(&bytes).map_err(|_|invalid("malformed remote HTTP JSON"))?;
    if !body.is_object(){return Err(invalid("remote HTTP JSON must be object"));}Ok((body,login))
}
fn http_response(status:u16,value:&Value)->hyper::Response<http_body_util::Full<bytes::Bytes>> {
    let mut body=serde_json::to_vec(value).unwrap_or_default();
    let status=if body.len()>MAX_FRAME_BYTES{body=br#"{"ok":false,"reason":"remote peer result exceeds bound"}"#.to_vec();500}else{status};
    hyper::Response::builder().status(status).header("content-type","application/json").header("connection","close")
        .body(http_body_util::Full::new(bytes::Bytes::from(body))).expect("fixed native HTTP response")
}
fn serve_connection(stream:UnixStream,handler:&impl Fn(&Value)->io::Result<Value>)->io::Result<()> {
    // Credentials are checked by Bridge before this function converts the socket.
    stream.set_nonblocking(true)?;
    let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build()?;
    runtime.block_on(async {
        let stream=tokio::net::UnixStream::from_std(stream)?;
        let service=hyper::service::service_fn(|request|async {
            let (status,value)=match parse_request(request).await{Ok((body,login))=>dispatch(&body,login.as_deref(),true,handler),Err(_)=>(400,json!({"ok":false,"reason":"bad remote peer request"}))};
            Ok::<_,std::convert::Infallible>(http_response(status,&value))
        });
        let mut builder=hyper::server::conn::http1::Builder::new();
        builder.timer(hyper_util::rt::TokioTimer::new()).header_read_timeout(Duration::from_secs(3)).max_buf_size(8192).max_headers(32).keep_alive(false);
        let connection=builder.serve_connection(hyper_util::rt::TokioIo::new(stream),service);
        tokio::time::timeout(REQUEST_TIMEOUT,connection).await.map_err(|_|invalid("remote HTTP deadline exceeded"))?
            .map_err(|_|invalid("invalid remote HTTP request"))
    })
}
fn dispatch(body:&Value, login:Option<&str>, attested:bool, handler:&impl Fn(&Value)->io::Result<Value>) -> (u16,Value) {
    let kind=match body["op"].as_str(){Some("roster")=>"read_status",Some("history")=>"read_transcript",Some("deliver")=>"send_prompt",_=>return(400,json!({"ok":false,"reason":"unrecognized remote peer operation"}))};
    let decision=policy::evaluate(kind,login,attested,None);
    if !decision.allowed{return(403,json!({"ok":false,"reason":decision.reason}));}
    match handler(body){Ok(mut value) if value.is_object()=>{value["ok"]=json!(true);value["reason"]=json!(decision.reason);(200,value)},_ => (500,json!({"ok":false,"reason":"native peer handler failed"}))}
}
fn reply(stream:&mut UnixStream,status:u16,value:&Value)->io::Result<()> {
    let body=serde_json::to_vec(value)?;
    if body.len()>MAX_FRAME_BYTES{return reply(stream,500,&json!({"ok":false,"reason":"remote peer result exceeds bound"}));}
    stream.set_write_timeout(Some(Duration::from_secs(2)))?;
    write!(stream,"HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",body.len())?;stream.write_all(&body)
}

pub struct Bridge { listener:UnixListener,path:PathBuf,identity:(u64,u64) }
impl Bridge {
    pub fn bind(runtime:&Path)->io::Result<Self>{
        if !policy::remote_enabled(){return Err(io::Error::new(io::ErrorKind::PermissionDenied,"remote listening is off"));}
        private_runtime(runtime)?;let directory=fs::symlink_metadata(runtime)?;let path=runtime.join("peernet.sock");
        if fs::symlink_metadata(&path).is_ok(){return Err(io::Error::new(io::ErrorKind::AlreadyExists,"remote bridge entry already exists"));}
        let listener=UnixListener::bind(&path)?;let metadata=fs::symlink_metadata(&path)?;
        let bridge=Self{listener,path,identity:(metadata.dev(),metadata.ino())};
        fs::set_permissions(&bridge.path,fs::Permissions::from_mode(0o600))?;bridge.listener.set_nonblocking(true)?;
        private_runtime(runtime)?;let current=fs::symlink_metadata(runtime)?;
        if (directory.dev(),directory.ino())!=(current.dev(),current.ino()){return Err(invalid("remote runtime changed during bind"));}
        Ok(bridge)
    }
    pub fn poll(&self,handler:&impl Fn(&Value)->io::Result<Value>)->io::Result<bool>{
        let (mut stream,_)=match self.listener.accept(){Ok(pair)=>pair,Err(error) if error.kind()==io::ErrorKind::WouldBlock=>return Ok(false),Err(error)=>return Err(error)};
        if !attested_proxy(&stream){reply(&mut stream,403,&json!({"ok":false,"reason":"Unix peer is not the attested Tailscale proxy"}))?;return Ok(true);}
        // A malformed, slow, or disconnected caller cannot stop the shared listener.
        let _=serve_connection(stream,handler);Ok(true)
    }
}
impl Drop for Bridge {fn drop(&mut self){if fs::symlink_metadata(&self.path).is_ok_and(|meta|meta.file_type().is_socket()&&(meta.dev(),meta.ino())==self.identity){let _=fs::remove_file(&self.path);}}}
fn private_runtime(runtime:&Path)->io::Result<()>{
    let meta=fs::symlink_metadata(runtime)?;
    if !runtime.is_absolute()||fs::canonicalize(runtime)?!=runtime||!meta.is_dir()||meta.uid()!=unsafe{libc::geteuid()}||meta.mode()&0o077!=0{return Err(invalid("remote runtime must be private, canonical and owned"));}Ok(())
}
pub struct StartLock(fs::File);
impl StartLock {pub fn acquire(runtime:&Path)->io::Result<Self>{
    private_runtime(runtime)?;let file=fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).mode(0o600).custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK).open(runtime.join("peernet-start.lock"))?;
    let meta=file.metadata()?;if !meta.is_file()||meta.uid()!=unsafe{libc::geteuid()}||meta.nlink()!=1||meta.mode()&0o077!=0{return Err(invalid("unsafe remote startup lock"));}
    let deadline=Instant::now()+Duration::from_secs(2);loop{if unsafe{libc::flock(file.as_raw_fd(),libc::LOCK_EX|libc::LOCK_NB)}==0{return Ok(Self(file));}if Instant::now()>=deadline{return Err(invalid("remote startup lock timed out"));}std::thread::sleep(Duration::from_millis(20));}
}}
impl Drop for StartLock {fn drop(&mut self){unsafe{libc::flock(self.0.as_raw_fd(),libc::LOCK_UN);}}}
fn connect_probe(path:&Path)->io::Result<UnixStream>{
    let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build()?;
    runtime.block_on(async{tokio::time::timeout(Duration::from_millis(100),tokio::net::UnixStream::connect(path)).await
        .map_err(|_|io::Error::new(io::ErrorKind::TimedOut,"remote socket probe deadline"))??.into_std()})
}
pub fn ensure(executable:&Path,runtime:&Path)->io::Result<()> {
    if !policy::remote_enabled(){return Ok(());}let _lock=StartLock::acquire(runtime)?;let socket=runtime.join("peernet.sock");
    if let Ok(meta)=fs::symlink_metadata(&socket){
        if !meta.file_type().is_socket()||meta.uid()!=unsafe{libc::geteuid()}||meta.mode()&0o077!=0{return Err(invalid("unsafe remote bridge socket"));}
        match connect_probe(&socket){Ok(stream)=>{if credentials(&stream)?.uid!=unsafe{libc::geteuid()}{return Err(invalid("remote bridge has another owner"));}return Ok(());},Err(error) if error.raw_os_error()==Some(libc::ECONNREFUSED)=>{
            let current=fs::symlink_metadata(&socket)?;if (meta.dev(),meta.ino())!=(current.dev(),current.ino()){return Err(invalid("remote socket changed"));}fs::remove_file(&socket)?;
        },Err(error)=>return Err(error)}
    }
    let mut command=Command::new(executable);command.arg("__peernet-serve").arg(runtime).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    unsafe{command.pre_exec(||{if libc::setsid()<0{return Err(io::Error::last_os_error());}Ok(())});}
    let mut child=command.spawn()?;let deadline=Instant::now()+Duration::from_millis(400);
    while Instant::now()<deadline{if let Ok(stream)=connect_probe(&socket){if credentials(&stream)?.uid!=unsafe{libc::geteuid()}{return Err(invalid("remote bridge has another owner"));}return Ok(());}if child.try_wait()?.is_some(){return Err(invalid("native remote bridge failed to start"));}std::thread::sleep(Duration::from_millis(20));}
    let _=child.kill();let _=child.wait();Err(invalid("native remote bridge startup timed out"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]fn endpoint_validation_rejects_header_url_and_port_injection(){
        for invalid in ["x=http://host","x=host:0","x=host:70000","x=host/path","x=host\r\nHost:attack","x=-host"]{assert!(Endpoint::parse(invalid).is_err(),"{invalid}");}
        assert_eq!(Endpoint::parse("machine=host.tailnet.ts.net:47600").unwrap().port,47600);
    }
    #[test]fn duplicate_identity_headers_and_oversize_lengths_are_refused(){
        use std::sync::{Arc,atomic::{AtomicUsize,Ordering}};
        for head in ["POST /peers HTTP/1.1\r\nContent-Length: 2\r\nTailscale-User-Login: a\r\nTailscale-User-Login: b\r\n\r\n{}","POST /peers HTTP/1.1\r\nContent-Length: 999999\r\n\r\n"]{
            let (server,mut client)=UnixStream::pair().unwrap();let called=Arc::new(AtomicUsize::new(0));let count=called.clone();
            let worker=std::thread::spawn(move||serve_connection(server,&|_|{count.fetch_add(1,Ordering::SeqCst);Ok(json!({}))}));
            client.set_read_timeout(Some(Duration::from_secs(4))).unwrap();client.write_all(head.as_bytes()).unwrap();
            let mut reply=String::new();client.read_to_string(&mut reply).unwrap();worker.join().unwrap().unwrap();
            assert!(reply.starts_with("HTTP/1.1 400"));assert_eq!(called.load(Ordering::SeqCst),0);
        }
    }
    #[test]fn hyper_wire_policy_refuses_unattested_or_missing_identity_before_permissive_handler(){
        use std::sync::{Arc,atomic::{AtomicUsize,Ordering}};
        struct Env(Vec<(&'static str,Option<std::ffi::OsString>)>);
        impl Drop for Env{fn drop(&mut self){for (key,value) in &self.0{if let Some(value)=value{std::env::set_var(key,value)}else{std::env::remove_var(key)}}}}
        let keys=[("DOXA_REMOTE_ENABLED","1"),("DOXA_REMOTE_ALLOWED_LOGINS","owner@example.com")];
        let _restore=Env(keys.iter().map(|(key,_)|(*key,std::env::var_os(key))).collect());
        for (key,value) in keys{std::env::set_var(key,value);}
        let called=Arc::new(AtomicUsize::new(0));let handler=|_:&Value|{called.fetch_add(1,Ordering::SeqCst);Ok(json!({"permissive":true}))};
        assert_eq!(dispatch(&json!({"op":"roster"}),Some("owner@example.com"),false,&handler).0,403);
        assert_eq!(called.load(Ordering::SeqCst),0);
        for (identity,status) in [("X-Forwarded-User: owner@example.com",403),("Tailscale-User-Login: owner@example.com",200)]{
            let (server,mut client)=UnixStream::pair().unwrap();let count=called.clone();
            let worker=std::thread::spawn(move||serve_connection(server,&|_|{count.fetch_add(1,Ordering::SeqCst);Ok(json!({"permissive":true}))}));
            let body=r#"{"op":"roster"}"#;
            let wire=format!("POST /peers HTTP/1.1\r\nContent-Length: {}\r\n{identity}\r\n\r\n{body}",body.len());
            client.set_read_timeout(Some(Duration::from_secs(4))).unwrap();client.write_all(wire.as_bytes()).unwrap();
            let mut reply=String::new();client.read_to_string(&mut reply).unwrap();worker.join().unwrap().unwrap();
            assert!(reply.starts_with(&format!("HTTP/1.1 {status}")),"{reply}");
        }
        assert_eq!(called.load(Ordering::SeqCst),1);
        std::env::set_var("DOXA_REMOTE_ENABLED","0");
        assert_eq!(dispatch(&json!({"op":"deliver"}),Some("owner@example.com"),true,&handler).0,403);
        assert_eq!(called.load(Ordering::SeqCst),1);
    }
    #[test]fn socket_drop_preserves_replacement_inode(){
        let dir=tempfile::tempdir().unwrap();let path=dir.path().join("peernet.sock");
        let listener=UnixListener::bind(&path).unwrap();let meta=fs::symlink_metadata(&path).unwrap();
        let bridge=Bridge{listener,path:path.clone(),identity:(meta.dev(),meta.ino())};
        fs::rename(&path,dir.path().join("moved.sock")).unwrap();let _replacement=UnixListener::bind(&path).unwrap();
        drop(bridge);assert!(path.exists());assert!(dir.path().join("moved.sock").exists());
    }
    #[test]fn roster_origin_is_from_dialed_endpoint_and_client_never_follows_redirect(){
        use std::net::TcpListener;
        for redirect in [false,true]{
            let listener=TcpListener::bind("127.0.0.1:0").unwrap();let port=listener.local_addr().unwrap().port();
            let worker=std::thread::spawn(move||{
                let (mut stream,_)=listener.accept().unwrap();stream.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
                let mut head=Vec::new();while !head.windows(4).any(|bytes|bytes==b"\r\n\r\n"){assert!(head.len()<MAX_HEAD);let mut byte=[0];stream.read_exact(&mut byte).unwrap();head.push(byte[0]);}
                let head=String::from_utf8(head).unwrap();assert_eq!(head.lines().next(),Some("POST /peers HTTP/1.1"));assert!(!head.to_ascii_lowercase().contains("tailscale-user-login"));
                let lengths=head.lines().skip(1).filter_map(|line|line.split_once(':'))
                    .filter(|(name,_)|name.eq_ignore_ascii_case("content-length"))
                    .map(|(_,value)|value.trim().parse::<usize>().unwrap()).collect::<Vec<_>>();
                assert_eq!(lengths.len(),1);assert!(lengths[0]<=MAX_FRAME_BYTES);
                // Closing with unread POST bytes can reset TCP and discard the
                // response. Consume and verify the bounded request first.
                let mut request=vec![0;lengths[0]];stream.read_exact(&mut request).unwrap();
                assert_eq!(serde_json::from_slice::<Value>(&request).unwrap(),json!({"op":"roster"}));
                let body=r#"{"ok":true,"peers":[{"session_id":"12345678-1234-1234-1234-123456789012","origin":"forged","pid":1,"socket_path":"/attacker","daemon_socket":"/attacker"}]}"#;
                let status=if redirect{"302 Found"}else{"200 OK"};
                write!(stream,"HTTP/1.1 {status}\r\nContent-Length: {}\r\nLocation: http://127.0.0.1:1/attack\r\nConnection: close\r\n\r\n{body}",body.len()).unwrap();
            });
            let result=fetch_roster(&Endpoint{label:"observed-machine".into(),host:"127.0.0.1".into(),port});
            worker.join().unwrap();if redirect{assert!(result.is_err());}else{let rows=result.unwrap();assert_eq!(rows[0]["origin"],"observed-machine");assert_eq!(rows[0]["socket_path"],"");assert_eq!(rows[0]["pid"],0);assert!(rows[0]["daemon_socket"].is_null());}
        }
    }

}
