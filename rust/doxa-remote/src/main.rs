//! Private Rust browser adapter for the native DOXA daemon session protocol.
mod daemon;
mod uplink;
use bytes::Bytes;
use doxa_lore::LoreClient;
use doxa_peers::{remote_policy as policy, PeerRecord, Registry};
use futures_util::stream;
use http_body_util::{BodyExt, Full, StreamBody, combinators::UnsyncBoxBody};
use hyper::{body::{Frame, Incoming}, header, Method, Request, Response, StatusCode};
use serde_json::{json, Value};
use std::{convert::Infallible, fs, io, os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt},
    path::{Path, PathBuf}, sync::{atomic::{AtomicBool, Ordering}, Arc, Mutex}, time::Duration};
use tokio::{net::{UnixListener, UnixStream}, sync::{mpsc,Semaphore}};

type Body = UnsyncBoxBody<Bytes, Infallible>;
struct App { runtime: PathBuf, registry: Registry, lore: Mutex<LoreClient> }

fn response(status: StatusCode, content_type: &'static str, bytes: impl Into<Bytes>) -> Response<Body> {
    Response::builder().status(status).header(header::CONTENT_TYPE, content_type)
        .header(header::CACHE_CONTROL, "no-store")
        .header("x-content-type-options", "nosniff")
        .header("content-security-policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        .body(Full::new(bytes.into()).boxed_unsync()).expect("fixed response")
}
fn json_response(status: StatusCode, value: Value) -> Response<Body> {
    response(status, "application/json; charset=utf-8", serde_json::to_vec(&value).unwrap_or_default())
}
fn error(status: StatusCode, message: &str) -> Response<Body> { json_response(status, json!({"error":message})) }
fn denied(message: &str) -> Response<Body> { error(StatusCode::FORBIDDEN, message) }

fn authenticated(request: &Request<Incoming>, attested: bool, kind: &str) -> Result<String, Response<Body>> {
    let headers = request.headers();
    if headers.get_all("tailscale-user-login").iter().count() != 1 { return Err(denied("exactly one Tailscale identity header required")); }
    let login = headers.get("tailscale-user-login").and_then(|value| value.to_str().ok());
    let decision = policy::evaluate(kind, login, attested, None);
    if !decision.allowed { return Err(denied(&decision.reason)); }
    if request.method() != Method::GET && !same_origin(headers) { return Err(denied("cross-origin write refused")); }
    Ok(login.expect("accepted by policy").to_owned())
}
fn same_origin(headers: &hyper::HeaderMap) -> bool {
    let origins = headers.get_all(header::ORIGIN);
    if origins.iter().count() == 0 { return true; }
    if origins.iter().count() != 1 { return false; }
    let Some(origin) = origins.iter().next().and_then(|value| value.to_str().ok()) else { return false; };
    let Some(host) = headers.get(header::HOST).and_then(|value| value.to_str().ok()) else { return false; };
    origin == format!("https://{host}") || origin == format!("http://{host}")
}

impl App {
    fn sessions(&self) -> io::Result<Vec<PeerRecord>> {
        // A failure to scrub any registry display string refuses the whole
        // response. The registry never exposes its private socket fields.
        let failed = AtomicBool::new(false);
        let scrub = |text: &str| match self.lore.lock().ok().and_then(|mut lore| lore.scrub(text).ok()) {
            Some(clean) => clean,
            None => { failed.store(true, Ordering::Release); String::new() }
        };
        let entries = self.registry.read(&scrub, false, true)?;
        if failed.load(Ordering::Acquire) { return Err(io::Error::other("LORE scrub unavailable")); }
        Ok(entries)
    }
    fn session(&self, id: &str) -> io::Result<Option<PeerRecord>> {
        if !valid_id(id) { return Ok(None); }
        Ok(self.sessions()?.into_iter().find(|entry| entry.session_id == id))
    }
}
fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 128 && id.bytes().enumerate().all(|(i, b)| b.is_ascii_alphanumeric() || (i > 0 && b == b'-'))
}
async fn connect(app: &App, entry: &PeerRecord, login: Option<&str>, cursor: Option<u64>) -> io::Result<daemon::Client> {
    let path = entry.daemon_socket.as_deref().ok_or_else(||io::Error::other("session has no daemon socket"))?;
    let path = Path::new(path);
    daemon::validate_socket(path, &app.runtime)?;
    let stream = tokio::time::timeout(Duration::from_secs(2), UnixStream::connect(path)).await
        .map_err(|_|io::Error::new(io::ErrorKind::TimedOut,"daemon connect timed out"))??;
    let stream = stream.into_std()?;
    stream.set_nonblocking(false)?;
    // The registry is owner-checked; the connected process must be owned by
    // the same user before accepting its hello or transcript path.
    let peer = doxa_peers::credentials::peer_credentials(&stream)?;
    if peer.uid != unsafe { libc::geteuid() } || peer.pid != entry.pid {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied,"daemon process identity differs"));
    }
    daemon::Client::from_stream(stream, &entry.session_id, login, cursor)
}
async fn body_json(request: Request<Incoming>) -> Result<Value, Response<Body>> {
    let headers = request.headers();
    if headers.contains_key(header::TRANSFER_ENCODING) { return Err(error(StatusCode::BAD_REQUEST,"chunked writes refused")); }
    let length = headers.get(header::CONTENT_LENGTH).and_then(|v|v.to_str().ok())
        .and_then(|v|v.parse::<usize>().ok()).filter(|n|*n <= 60_000)
        .ok_or_else(||error(StatusCode::BAD_REQUEST,"bounded content length required"))?;
    let raw = tokio::time::timeout(Duration::from_secs(10),http_body_util::Limited::new(request.into_body(),60_000).collect()).await
        .map_err(|_|error(StatusCode::REQUEST_TIMEOUT,"request body timed out"))?
        .map_err(|_|error(StatusCode::BAD_REQUEST,"request body exceeded bound"))?.to_bytes();
    if raw.len() != length { return Err(error(StatusCode::BAD_REQUEST,"content length mismatch")); }
    let value: Value = serde_json::from_slice(&raw).map_err(|_|error(StatusCode::BAD_REQUEST,"invalid JSON"))?;
    if !value.is_object() { return Err(error(StatusCode::BAD_REQUEST,"JSON object required")); }
    Ok(value)
}
fn sse(value: &Value, id: Option<u64>) -> Bytes {
    let mut result = String::new();
    if let Some(id) = id { result.push_str(&format!("id: {id}\n")); }
    result.push_str("data: "); result.push_str(&value.to_string()); result.push_str("\n\n");
    Bytes::from(result)
}
fn event_body(receiver: mpsc::Receiver<Bytes>) -> Body {
    let stream = stream::unfold(receiver, |mut receiver| async move {
        receiver.recv().await.map(|bytes| (Ok::<_, Infallible>(Frame::data(bytes)), receiver))
    });
    StreamBody::new(stream).boxed_unsync()
}
fn remote_dangerous(mode: &str) -> bool { matches!(mode, "bypassPermissions" | "dontAsk" | "full-access") }
fn scrub_data(value: &mut Value, lore: &Mutex<LoreClient>) -> io::Result<()> {
    match value {
        Value::String(text) => *text = lore.lock().map_err(|_|io::Error::other("LORE lock poisoned"))?
            .scrub(text).map_err(|_|io::Error::other("LORE scrub unavailable"))?,
        Value::Array(items) => for item in items { scrub_data(item,lore)?; },
        Value::Object(fields) => for value in fields.values_mut() { scrub_data(value,lore)?; },
        _ => {},
    }
    Ok(())
}
fn bypass_opt_in() -> bool {
    matches!(policy::setting("remote_allow_bypass","DOXA_REMOTE_ALLOW_BYPASS").trim().to_ascii_lowercase().as_str(), "1"|"true"|"yes"|"on")
}
async fn handle(request: Request<Incoming>, app: Arc<App>, attested: bool) -> Response<Body> {
    let method = request.method().clone();
    let path = request.uri().path().to_owned();
    let parts = path.trim_matches('/').split('/').collect::<Vec<_>>();
    let kind = match (method.clone(), parts.as_slice()) {
        (Method::GET, [] | ["remote.js"] | ["remote.css"] | ["api","sessions"]) => "read_status",
        (Method::GET, ["api","sessions",_,"transcript"] | ["api","sessions",_,"events"]) => "read_transcript",
        (Method::POST, ["api","sessions",_,"transcript"]) => "read_transcript",
        (Method::POST, ["api","sessions",_,"prompt"]) => "send_prompt",
        (Method::POST, ["api","sessions",_,"answer"]) => "approve_tool",
        _ => return error(StatusCode::NOT_FOUND,"unknown remote route"),
    };
    let login = match authenticated(&request, attested, kind) { Ok(login)=>login, Err(reply)=>return reply };
    match (method, parts.as_slice()) {
        (Method::GET, []) => response(StatusCode::OK,"text/html; charset=utf-8",include_str!("../assets/index.html")),
        (Method::GET, ["remote.js"]) => response(StatusCode::OK,"text/javascript; charset=utf-8",include_str!("../assets/remote.js")),
        (Method::GET, ["remote.css"]) => response(StatusCode::OK,"text/css; charset=utf-8",include_str!("../assets/remote.css")),
        (Method::GET, ["api","sessions"]) => match app.sessions() {
            Ok(entries) => json_response(StatusCode::OK,json!({"sessions":entries.into_iter().map(|p|json!({
                "id":p.session_id,"title":p.title,"engine":p.engine,"model":p.model,"clients":p.clients,
                "incarnation":p.incarnation.as_deref().unwrap_or(&p.started_at)
            })).collect::<Vec<_>>()})),
            Err(_) => error(StatusCode::SERVICE_UNAVAILABLE,"session registry unavailable"),
        },
        (Method::GET | Method::POST, ["api","sessions",id,"transcript"]) => {
            let before = if request.method()==Method::POST {
                match body_json(request).await {
                    Ok(body) => match body.get("before") {
                        None => None,
                        Some(value) => match value.as_u64() {
                            Some(before) => Some(before),
                            None => return error(StatusCode::BAD_REQUEST,"invalid transcript page cursor"),
                        },
                    },
                    Err(_) => return error(StatusCode::BAD_REQUEST,"invalid transcript request"),
                }
            } else { None };
            let entry = match app.session(id) { Ok(Some(entry))=>entry, Ok(None)=>return error(StatusCode::NOT_FOUND,"session not found"), Err(_)=>return error(StatusCode::SERVICE_UNAVAILABLE,"session registry unavailable") };
            let client = match connect(&app,&entry,None,None).await { Ok(client)=>client, Err(_)=>return error(StatusCode::SERVICE_UNAVAILABLE,"session unavailable") };
            let transcript_app = app.clone();
            let incarnation=entry.incarnation.as_deref().unwrap_or(&entry.started_at).to_owned();
            let result = tokio::task::spawn_blocking(move || {
                let mut history = daemon::transcript_page(&client.hello,before)?;
                history["pending_inputs"]=client.hello["pending_inputs"].clone();
                history["pending_inputs_complete"]=client.hello["pending_inputs_complete"].clone();
                history["incarnation"]=json!(incarnation);
                scrub_data(&mut history, &transcript_app.lore)?;
                Ok::<_,io::Error>(uplink::bounded_history(history))
            }).await;
            match result { Ok(Ok(history))=>json_response(StatusCode::OK,history), _=>error(StatusCode::SERVICE_UNAVAILABLE,"transcript unavailable") }
        },
        (Method::GET, ["api","sessions",id,"events"]) => {
            let entry = match app.session(id) { Ok(Some(entry))=>entry, Ok(None)=>return error(StatusCode::NOT_FOUND,"session not found"), Err(_)=>return error(StatusCode::SERVICE_UNAVAILABLE,"session registry unavailable") };
            let cursor = request.headers().get("last-event-id").and_then(|value|value.to_str().ok()).and_then(|raw|raw.parse::<u64>().ok()).and_then(|last|last.checked_add(1))
                .or_else(||request.uri().query().and_then(|query|query.strip_prefix("cursor=")).and_then(|raw|raw.parse::<u64>().ok()));
            let mut client = match connect(&app,&entry,Some(&login),cursor).await { Ok(client)=>client, Err(_)=>return error(StatusCode::SERVICE_UNAVAILABLE,"session unavailable") };
            let (sender,receiver) = mpsc::channel(64);
            let events_app = app.clone();
            let incarnation=entry.incarnation.as_deref().unwrap_or(&entry.started_at).to_owned();
            tokio::task::spawn_blocking(move || {
                let _ = client.idle_timeout();
                let mut hello = json!({"type":"hello","session_id":client.hello["session_id"],
                    "engine":client.hello["engine"],"model":client.hello["model"],
                    "pending_inputs":client.hello["pending_inputs"],"pending_inputs_complete":client.hello["pending_inputs_complete"],
                    "incarnation":incarnation});
                if scrub_data(&mut hello["pending_inputs"], &events_app.lore).is_err()
                    || sender.blocking_send(sse(&hello,None)).is_err() { return; }
                loop {
                    match client.next() {
                        Ok(mut frame) if frame["type"] == "event" => {
                            if scrub_data(&mut frame["event"]["data"], &events_app.lore).is_err() { break; }
                            let id=frame["seq"].as_u64();
                            if sender.blocking_send(sse(&frame,id)).is_err() { break; }
                        },
                        Ok(_) => {},
                        Err(err) if matches!(err.kind(),io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock) => {
                            let decision=policy::evaluate("read_transcript",Some(&login),true,None);
                            if !decision.allowed || sender.blocking_send(Bytes::from_static(b": ping\n\n")).is_err() { break; }
                        },
                        Err(_) => break,
                    }
                }
            });
            Response::builder().status(StatusCode::OK).header(header::CONTENT_TYPE,"text/event-stream")
                .header(header::CACHE_CONTROL,"no-store").header("x-accel-buffering","no")
                .header("x-content-type-options","nosniff")
                .body(event_body(receiver)).expect("fixed SSE response")
        },
        (Method::POST, ["api","sessions",id,operation @ ("prompt" | "answer")]) => {
            let is_prompt = *operation == "prompt";
            let entry = match app.session(id) { Ok(Some(entry))=>entry, Ok(None)=>return error(StatusCode::NOT_FOUND,"session not found"), Err(_)=>return error(StatusCode::SERVICE_UNAVAILABLE,"session registry unavailable") };
            let input = match body_json(request).await { Ok(value)=>value, Err(reply)=>return reply };
            if input.get("incarnation").is_some()
                && input["incarnation"].as_str()!=Some(entry.incarnation.as_deref().unwrap_or(&entry.started_at)) {
                return denied("session incarnation changed");
            }
            if is_prompt && !input["text"].as_str().is_some_and(|text| !text.trim().is_empty() && text.len() <= 58_000) {
                return error(StatusCode::BAD_REQUEST,"invalid prompt");
            }
            if !is_prompt && (!input["id"].as_str().is_some_and(|id|!id.is_empty()&&id.len()<=128) || !input["answer"].is_object()) {
                return error(StatusCode::BAD_REQUEST,"invalid answer");
            }
            let mut client = match connect(&app,&entry,None,None).await { Ok(client)=>client, Err(_)=>return error(StatusCode::SERVICE_UNAVAILABLE,"session unavailable") };
            let result = tokio::task::spawn_blocking(move || -> io::Result<Value> {
                if is_prompt {
                    let status=client.call("status",json!({}))?;
                    let mode=status["status"]["permission_mode"].as_str().unwrap_or("");
                    if remote_dangerous(mode) && !bypass_opt_in() { return Err(io::Error::new(io::ErrorKind::PermissionDenied,"remote prompt refused in unrestricted permission mode")); }
                    client.remote_prompt(input["text"].as_str().unwrap(),bypass_opt_in())
                } else {
                    let state=client.call("get_state",json!({}))?;
                    if state["pending_inputs_complete"] != true { return Err(io::Error::new(io::ErrorKind::PermissionDenied,"pending input review incomplete")); }
                    let Some(reviewed) = state["pending_inputs"].as_array().and_then(|items|items.iter().find(|item|item["id"]==input["id"])) else {
                        return Err(io::Error::new(io::ErrorKind::PermissionDenied,"input request expired"));
                    };
                    if input.get("reviewed_request").is_some() && input.get("reviewed_request")!=Some(reviewed) {
                        return Err(io::Error::new(io::ErrorKind::PermissionDenied,"reviewed input changed"));
                    }
                    client.call("answer_needs_input",json!({"id":input["id"],"answer":input["answer"],"reviewed_request":reviewed}))
                }
            }).await;
            match result {
                Ok(Ok(reply)) if reply["ok"] == true => json_response(StatusCode::OK,reply),
                Ok(Ok(reply)) => error(StatusCode::CONFLICT,reply["error"].as_str().unwrap_or("session refused operation")),
                Ok(Err(err)) if err.kind()==io::ErrorKind::PermissionDenied => denied(&err.to_string()),
                _ => error(StatusCode::SERVICE_UNAVAILABLE,"session operation failed"),
            }
        },
        _ => error(StatusCode::NOT_FOUND,"unknown remote route"),
    }
}

fn private_runtime(runtime: &Path) -> io::Result<()> {
    let meta=fs::symlink_metadata(runtime)?;
    if !runtime.is_absolute() || fs::canonicalize(runtime)? != runtime || !meta.is_dir() || meta.file_type().is_symlink()
        || meta.uid()!=unsafe{libc::geteuid()} || meta.permissions().mode() & 0o077 != 0 {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied,"remote runtime must be private, canonical and owned"));
    }
    Ok(())
}
struct BoundSocket { path:PathBuf, inode:u64 }
impl Drop for BoundSocket { fn drop(&mut self) { if fs::symlink_metadata(&self.path).is_ok_and(|meta|meta.file_type().is_socket()&&meta.ino()==self.inode) { let _=fs::remove_file(&self.path); } } }

#[tokio::main]
async fn main() -> io::Result<()> {
    let args=std::env::args().skip(1).collect::<Vec<_>>();
    if args.first().is_some_and(|action|matches!(action.as_str(),"list"|"send"|"answer")){
        return uplink::client_action(&args).await;
    }
    if !policy::remote_enabled() { return Err(io::Error::new(io::ErrorKind::PermissionDenied,"remote listening is off")); }
    if policy::setting("remote_allowed_logins","DOXA_REMOTE_ALLOWED_LOGINS").trim().is_empty() {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied,"remote allow-list is empty"));
    }
    if policy::proxy_uid().is_none() { return Err(io::Error::new(io::ErrorKind::PermissionDenied,"unsafe proxy UID")); }
    let runtime=doxa_peers::runtime_dir(); private_runtime(&runtime)?;
    let registry=Registry::open(&runtime)?;
    let lore=LoreClient::open(Duration::from_secs(5)).map_err(|_|io::Error::other("LORE scrub unavailable"))?;
    let app=Arc::new(App{runtime:runtime.clone(),registry,lore:Mutex::new(lore)});
    match args.as_slice(){
        [mode,url,host] if mode=="connect"=>return uplink::run(app,url,host).await,
        []=>{},
        [mode] if mode=="serve"=>{
            if doxa_remote_wire::configured_key()?.is_some(){
                return Err(io::Error::new(io::ErrorKind::PermissionDenied,
                    "browser adapter cannot serve encrypted sessions; use the native remote TUI"));
            }
        },
        _=>return Err(io::Error::new(io::ErrorKind::InvalidInput,"usage: doxa-remote serve | connect URL HOST_ID | list URL | send URL SESSION TEXT | answer URL SESSION REQUEST_ID allow|deny")),
    }
    let path=runtime.join("remote-browser.sock");
    if fs::symlink_metadata(&path).is_ok() { return Err(io::Error::new(io::ErrorKind::AlreadyExists,"remote browser socket already exists")); }
    let listener=UnixListener::bind(&path)?;
    fs::set_permissions(&path,fs::Permissions::from_mode(0o600))?;
    let inode=fs::symlink_metadata(&path)?.ino();
    let _owned=BoundSocket{path:path.clone(),inode};
    eprintln!("doxa-remote: private browser socket {}",path.display());
    let mut term=tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let slots=Arc::new(Semaphore::new(64));
    loop {
        let (stream,_) = tokio::select! {
            result=listener.accept()=>result?,
            _=tokio::signal::ctrl_c()=>break,
            _=term.recv()=>break,
        };
        let Ok(permit)=slots.clone().try_acquire_owned() else{continue};
        let app=app.clone();
        tokio::spawn(async move {
            let _permit=permit;
            let std_stream=match stream.into_std() { Ok(stream)=>stream,Err(_)=>return };
            let attested=doxa_peers::credentials::peer_uid(&std_stream).is_ok_and(|uid|Some(uid)==policy::proxy_uid());
            let _=std_stream.set_nonblocking(true);
            let stream=match UnixStream::from_std(std_stream) { Ok(stream)=>stream,Err(_)=>return };
            let service=hyper::service::service_fn(move |request| {
                let app=app.clone(); async move { Ok::<_,Infallible>(handle(request,app,attested).await) }
            });
            let mut server=hyper::server::conn::http1::Builder::new();
            server.timer(hyper_util::rt::TokioTimer::new()).header_read_timeout(Duration::from_secs(3))
                .keep_alive(false).max_headers(32).max_buf_size(8192);
            let _=server.serve_connection(hyper_util::rt::TokioIo::new(stream),service).await;
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn remote_policy_is_fail_closed_for_dangerous_modes_and_ids() {
        assert!(remote_dangerous("bypassPermissions")); assert!(remote_dangerous("full-access"));
        assert!(remote_dangerous("dontAsk")); assert!(!remote_dangerous("plan"));
        assert!(valid_id("abc-123")); assert!(!valid_id("../escape"));
    }
    #[test] fn event_id_is_daemon_sequence() {
        assert_eq!(sse(&json!({"type":"event"}),Some(41)),Bytes::from_static(b"id: 41\ndata: {\"type\":\"event\"}\n\n"));
    }
}
