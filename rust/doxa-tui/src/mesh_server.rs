//! Token-gated, bounded loopback HTTP/SSE reader of the private peer ledger.
//! The directory descriptor pins the original directory across path replacement.
use serde_json::{json, Value};
use std::{collections::HashSet, ffi::CString, fs::{File, OpenOptions}, io::{self, BufRead, BufReader, Read, Seek, SeekFrom}, net::TcpListener, os::{fd::{AsRawFd, FromRawFd}, unix::fs::{MetadataExt, OpenOptionsExt}}, path::Path, sync::{Arc, atomic::{AtomicBool, Ordering}}, thread::{self, JoinHandle}, time::{Duration, Instant}};

const LEDGER_CAP: u64 = 128 << 20;
const LINE_CAP: usize = 1 << 20;
const BATCH_CAP: u64 = 4 << 20;
const WIRE_CAP: usize = 8 << 20;
use std::convert::Infallible;
use hyper::{Request, Response, StatusCode, body::{Bytes,Frame,Incoming}, server::conn::http1, service::service_fn};
use hyper_util::rt::{TokioIo,TokioTimer};
use http_body_util::{BodyExt,Full,StreamBody,combinators::UnsyncBoxBody};
type Body=UnsyncBoxBody<Bytes,io::Error>;
const HEADER_CAP: usize = 8192;
const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

struct Ledger { directory: File, name: CString }
impl Ledger {
    fn new(path: &Path) -> io::Result<Self> {
        let directory = OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC).open(path.parent().ok_or_else(|| io::Error::other("missing mesh directory"))?)?;
        let info = directory.metadata()?;
        if info.uid() != unsafe { libc::geteuid() } || info.mode() & 0o077 != 0 { return Err(io::Error::other("unsafe mesh directory")); }
        let name = CString::new(path.file_name().ok_or_else(|| io::Error::other("missing mesh filename"))?.as_encoded_bytes())?;
        Ok(Self { directory, name })
    }
    fn open(&self) -> io::Result<Option<File>> {
        let fd = unsafe { libc::openat(self.directory.as_raw_fd(), self.name.as_ptr(), libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC) };
        if fd < 0 { return Ok(None); }
        let file = unsafe { File::from_raw_fd(fd) };
        let info = file.metadata()?;
        if !info.is_file() || info.uid() != unsafe { libc::geteuid() } || info.mode() & 0o077 != 0 || info.nlink() != 1 { return Ok(None); }
        if info.len() > LEDGER_CAP { return Err(io::Error::other("mesh ledger exceeds 128 MiB")); }
        Ok(Some(file))
    }
    fn size(&self) -> io::Result<u64> { Ok(self.open()?.map(|f| f.metadata().map(|m|m.len())).transpose()?.unwrap_or(0)) }
    fn batch(&self, offset: u64, until: Option<u64>) -> io::Result<(Vec<(Value,u64)>,u64)> {
        let Some(mut file) = self.open()? else { return Ok((vec![], offset)); };
        let end = file.metadata()?.len().min(until.unwrap_or(LEDGER_CAP));
        if offset >= end { return Ok((vec![],offset)); }
        file.seek(SeekFrom::Start(offset))?;
        let mut reader = BufReader::new(file.take(end-offset));
        let mut position=offset; let mut consumed=offset; let mut rows=vec![]; let mut wire=0;
        let deadline=Instant::now()+Duration::from_secs(1);
        while position < end && rows.len()<256 && position-offset < BATCH_CAP {
            let mut line=Vec::new(); let mut oversized=false; let mut complete=false;
            loop {
                if Instant::now()>=deadline { return Ok((rows,consumed)); }
                let bytes=reader.fill_buf()?;
                if bytes.is_empty() { break; }
                let length=bytes.iter().position(|b|*b==b'\n').map(|n|n+1).unwrap_or(bytes.len());
                let newline=bytes[length-1]==b'\n';
                if !oversized && line.len()+length<=LINE_CAP { line.extend_from_slice(&bytes[..length]); } else { oversized=true; line.clear(); }
                reader.consume(length); position+=length as u64;
                if newline { complete=true; break; }
            }
            if !complete { break; } // leave partial tail at its start
            if !oversized {
                if let Some(row)=serde_json::from_slice(&line).ok().and_then(normalize) {
                    let cost=json_bytes(&row).len()+64;
                    if cost>WIRE_CAP { return Err(io::Error::other("mesh record exceeds wire limit")); }
                    if wire+cost>WIRE_CAP { break; }
                    wire+=cost; rows.push((row,position));
                }
            }
            consumed=position;
        }
        Ok((rows,consumed))
    }
}
fn normalize(value: Value) -> Option<Value> {
    let sender=value.get("from")?.as_object()?;
    let from=sender.get("session")?.as_str()?;
    if !doxa_state::valid_session_id(from) { return None; }
    let recipients=value.get("to")?.as_array()?;
    if recipients.len()>4096 { return None; }
    let mut seen=HashSet::new(); let mut to=vec![];
    for recipient in recipients.iter().filter_map(Value::as_str).filter(|s|!s.is_empty()) {
        if !doxa_state::valid_session_id(recipient) { return None; }
        if seen.insert(recipient) { to.push(recipient); }
    }
    let kind=match value["kind"].as_str() { Some("direct")=>"direct", Some("broadcast")=>"broadcast", _=>"unknown" };
    let text=|key|value.get(key).and_then(Value::as_str).unwrap_or("");
    let sender_text=|key|sender.get(key).and_then(Value::as_str).unwrap_or("");
    let edges:Vec<_>=to.iter().filter(|id|**id!=from).map(|id|json!({"from":from,"to":id,"kind":kind})).collect();
    Some(json!({"id":text("id"),"ts":text("ts"),"from":from,"title":sender_text("title"),"repo":sender_text("repo"),"model":sender_text("model"),"engine":sender_text("engine"),"to":to,"kind":kind,"in_reply_to":value["in_reply_to"].as_str(),"body":text("body"),"sender_latency_ms":value["latency_ms"].as_f64(),"sender_turn_state":value["turn"]["state"].as_str().unwrap_or(""),"edges":edges}))
}
fn json_bytes(value:&Value)->Vec<u8> { value.to_string().replace('<',"\\u003c").replace('>',"\\u003e").replace('&',"\\u0026").into_bytes() }

pub(super) struct Server { pub url:String, stop:Arc<AtomicBool>, worker:Option<JoinHandle<()>> }
impl Server {
    pub fn start(path:&Path)->io::Result<Self> {
        let ledger=Arc::new(Ledger::new(path)?); ledger.open()?;
        let listener=TcpListener::bind(("127.0.0.1",0))?; listener.set_nonblocking(true)?;
        let mut random=[0u8;32]; File::open("/dev/urandom")?.read_exact(&mut random)?;
        let token:String=random.iter().map(|b|format!("{b:02x}")).collect();
        let url=format!("http://127.0.0.1:{}/{token}/",listener.local_addr()?.port());
        let stop=Arc::new(AtomicBool::new(false)); let stopping=stop.clone();
        let runtime=tokio::runtime::Builder::new_current_thread().enable_all().max_blocking_threads(4).build()?;
        let worker=thread::spawn(move|| {
            runtime.block_on(async move {
                let Ok(listener)=tokio::net::TcpListener::from_std(listener) else{return;};
                let mut connections=tokio::task::JoinSet::new();
                while !stopping.load(Ordering::Acquire) {
                    while connections.try_join_next().is_some() {}
                    tokio::select! {
                        accepted=listener.accept()=>{
                            let Ok((stream,_))=accepted else{break;};
                            if connections.len()>=16 {drop(stream);continue;}
                            let ledger=ledger.clone();let token=token.clone();let stop=stopping.clone();
                            connections.spawn(async move {
                                let service=service_fn(move|request|handle(request,token.clone(),ledger.clone(),stop.clone()));
                                let mut builder=http1::Builder::new();
                                builder.keep_alive(false).max_headers(64).max_buf_size(HEADER_CAP).timer(TokioTimer::new()).header_read_timeout(Duration::from_secs(2));
                                // Bound connection retention; EventSource resumes from its last ID.
                                let _=tokio::time::timeout(Duration::from_secs(60),builder.serve_connection(TokioIo::new(stream),service)).await;
                            });
                        },
                        _=tokio::time::sleep(Duration::from_millis(20))=>{},
                    }
                }
                connections.abort_all();while connections.join_next().await.is_some() {}
            });
            runtime.shutdown_timeout(Duration::from_secs(2));
        });
        Ok(Self {url,stop,worker:Some(worker)})
    }
    pub fn running(&self)->bool {self.worker.as_ref().is_some_and(|w|!w.is_finished())}
    pub fn stop(&mut self) {self.stop.store(true,Ordering::Release);if let Some(worker)=self.worker.take(){let _=worker.join();}}
}
impl Drop for Server {fn drop(&mut self){self.stop();}}
fn full(body:impl Into<Bytes>)->Body {Full::new(body.into()).map_err(|never|match never{}).boxed_unsync()}
fn response(status:StatusCode,kind:&str,body:impl Into<Bytes>)->Response<Body> {
    decorate(Response::builder().status(status).header("Content-Type",kind).body(full(body)).expect("static response headers"))
}
fn decorate(mut response:Response<Body>)->Response<Body> {
    for(key,value)in [("Connection","close"),("Cache-Control","no-store"),("X-Content-Type-Options","nosniff"),("Referrer-Policy","no-referrer"),("Content-Security-Policy",CSP)] {
        response.headers_mut().insert(hyper::header::HeaderName::from_bytes(key.as_bytes()).unwrap(),hyper::header::HeaderValue::from_static(value));
    }
    response
}
fn query_number(query:&str,key:&str,default:u64)->u64 {query.split('&').find_map(|part|part.split_once('=').filter(|(k,_)|*k==key).and_then(|(_,v)|v.parse().ok())).unwrap_or(default)}
async fn handle(request:Request<Incoming>,token:String,ledger:Arc<Ledger>,stop:Arc<AtomicBool>)->Result<Response<Body>,Infallible> {
    let refused=|status,body:&'static str|response(status,"text/plain",body);
    if request.method()!=hyper::Method::GET {return Ok(refused(StatusCode::METHOD_NOT_ALLOWED,"GET only"));}
    if request.headers().contains_key("transfer-encoding") || request.headers().get("content-length").is_some_and(|v|v.as_bytes()!=b"0") {
        return Ok(refused(StatusCode::BAD_REQUEST,"GET bodies are unsupported"));
    }
    let path=request.uri().path();let query=request.uri().query().unwrap_or("");
    let prefix=format!("/{token}");
    if path==prefix {
        let mut redirect=refused(StatusCode::MOVED_PERMANENTLY,"");
        redirect.headers_mut().insert("location",hyper::header::HeaderValue::from_str(&format!("{prefix}/")).expect("hex token"));return Ok(redirect);
    }
    let Some(route)=path.strip_prefix(&(prefix+"/")) else {return Ok(refused(StatusCode::NOT_FOUND,"not found"));};
    let asset=match route {""|"index.html"=>Some(("text/html; charset=utf-8",include_bytes!("../../../assets/mesh/index.html").as_slice())),"mesh.js"=>Some(("application/javascript; charset=utf-8",include_bytes!("../../../assets/mesh/mesh.js").as_slice())),"mesh.css"=>Some(("text/css; charset=utf-8",include_bytes!("../../../assets/mesh/mesh.css").as_slice())),_=>None};
    if let Some((kind,body))=asset {return Ok(response(StatusCode::OK,kind,Bytes::from_static(body)));}
    if !matches!(route,"ledger"|"events") {return Ok(refused(StatusCode::NOT_FOUND,"not found"));}
    let selected=ledger.clone();let end=match tokio::task::spawn_blocking(move||selected.size()).await {
        Ok(Ok(size))=>size,_=>return Ok(refused(StatusCode::PAYLOAD_TOO_LARGE,"ledger limit exceeded"))
    };
    let mut offset=query_number(query,"from",if route=="events"{end}else{0});
    if route=="ledger" {
        let end=query_number(query,"until",end).min(end);let selected=ledger.clone();
        let(rows,next)=match tokio::task::spawn_blocking(move||selected.batch(offset,Some(end))).await {
            Ok(Ok(batch))=>batch,_=>return Ok(refused(StatusCode::PAYLOAD_TOO_LARGE,"ledger limit exceeded"))
        };
        let mut value=json!({"records":rows.into_iter().map(|(row,_)|row).collect::<Vec<_>>(),"offset":next});
        if offset<next && next<end {value["more"]=json!(true);value["snapshot_end"]=json!(end);}
        return Ok(response(StatusCode::OK,"application/json; charset=utf-8",json_bytes(&value)));
    }
    let mut cursors=request.headers().get_all("last-event-id").iter();
    if let Some(value)=cursors.next() {
        let Some(cursor)=value.to_str().ok().and_then(|v|v.trim().parse::<u64>().ok()) else{return Ok(refused(StatusCode::BAD_REQUEST,"invalid event cursor"));};
        if cursors.next().is_some(){return Ok(refused(StatusCode::BAD_REQUEST,"duplicate event cursor"));}offset=cursor;
    }
    let stream=futures_util::stream::unfold((ledger,stop,offset,true,Instant::now()),| (ledger,stop,mut offset,first,mut heartbeat) |async move {
        if first{return Some((Ok::<_,io::Error>(Frame::data(Bytes::from_static(b": open\n\n"))),(ledger,stop,offset,false,heartbeat)));}
        loop {
            if stop.load(Ordering::Acquire){return None;}
            let selected=ledger.clone();let batch=tokio::task::spawn_blocking(move||selected.batch(offset,None)).await;
            let(rows,next)=match batch {Ok(Ok(batch))=>batch,_=>return Some((Err(io::Error::other("mesh ledger unavailable")),(ledger,stop,offset,false,heartbeat)))};
            offset=next;
            if !rows.is_empty() {
                let mut bytes=Vec::new();for(row,position)in rows {bytes.extend_from_slice(format!("id: {position}\ndata: ").as_bytes());bytes.extend_from_slice(&json_bytes(&row));bytes.extend_from_slice(b"\n\n");}
                heartbeat=Instant::now();return Some((Ok(Frame::data(Bytes::from(bytes))),(ledger,stop,offset,false,heartbeat)));
            }
            if heartbeat.elapsed()>=Duration::from_secs(15){heartbeat=Instant::now();return Some((Ok(Frame::data(Bytes::from_static(b": beat\n\n"))),(ledger,stop,offset,false,heartbeat)));}
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
    });
    Ok(decorate(Response::builder().header("Content-Type","text/event-stream; charset=utf-8").body(StreamBody::new(stream).boxed_unsync()).expect("static event headers")))
}

#[cfg(test)] mod tests {
    use super::*; use std::os::unix::fs::PermissionsExt;
    #[test] fn batching_preserves_partial_tails_and_record_cursors() {
        let dir=tempfile::tempdir().unwrap();std::fs::set_permissions(dir.path(),std::fs::Permissions::from_mode(0o700)).unwrap();let path=dir.path().join("ledger");
        let line=b"{\"from\":{\"session\":\"a\"},\"to\":[\"b\",\"b\",\"a\"],\"body\":\"<script>\"}\n";
        let mut bytes=line.to_vec();bytes.extend_from_slice(b"partial");std::fs::write(&path,&bytes).unwrap();std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o600)).unwrap();
        let ledger=Ledger::new(&path).unwrap();let(rows,cursor)=ledger.batch(0,None).unwrap();assert_eq!(cursor,line.len() as u64);assert_eq!(rows[0].1,cursor);assert_eq!(rows[0].0["edges"].as_array().unwrap().len(),1);assert!(!String::from_utf8(json_bytes(&rows[0].0)).unwrap().contains("<script>"));
        assert!(ledger.batch(cursor,None).unwrap().0.is_empty());
        std::fs::rename(dir.path(),dir.path().with_extension("moved")).unwrap();let(rows,_)=ledger.batch(0,None).unwrap();assert_eq!(rows.len(),1);std::fs::remove_dir_all(dir.path().with_extension("moved")).unwrap();
    }
}
