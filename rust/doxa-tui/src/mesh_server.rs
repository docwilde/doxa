//! Token-gated, bounded loopback HTTP/SSE reader of the private peer ledger.
//! The directory descriptor pins the original directory across path replacement.
use serde_json::{json, Value};
use std::{collections::HashSet, ffi::CString, fs::{File, OpenOptions}, io::{self, BufRead, BufReader, Read, Seek, SeekFrom, Write}, net::{Shutdown, TcpListener, TcpStream}, os::{fd::{AsRawFd, FromRawFd}, unix::fs::{MetadataExt, OpenOptionsExt}}, path::Path, sync::{Arc, atomic::{AtomicBool, Ordering}}, thread::{self, JoinHandle}, time::{Duration, Instant}};

const LEDGER_CAP: u64 = 128 << 20;
const LINE_CAP: usize = 1 << 20;
const BATCH_CAP: u64 = 4 << 20;
const WIRE_CAP: usize = 8 << 20;
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
        let worker=thread::spawn(move|| {
            let mut connections:Vec<(TcpStream,JoinHandle<()>)>=vec![];
            while !stopping.load(Ordering::Acquire) {
                let mut i=0;
                while i<connections.len() { if connections[i].1.is_finished() { let (_,worker)=connections.swap_remove(i); let _=worker.join(); } else {i+=1;} }
                match listener.accept() {
                    Ok((stream,_))=> {
                        if connections.len()>=16 { let _=stream.shutdown(Shutdown::Both); continue; }
                        let Ok(socket)=stream.try_clone() else {continue;};
                        let ledger=ledger.clone();let token=token.clone();let stop=stopping.clone();
                        let worker=thread::spawn(move||{let _=handle(stream,&token,&ledger,&stop);});
                        connections.push((socket,worker));
                    },
                    Err(error) if error.kind()==io::ErrorKind::WouldBlock=>thread::sleep(Duration::from_millis(20)),
                    Err(_)=>break,
                }
            }
            for (socket,worker) in connections {let _=socket.shutdown(Shutdown::Both);let _=worker.join();}
        });
        Ok(Self {url,stop,worker:Some(worker)})
    }
    pub fn running(&self)->bool {self.worker.as_ref().is_some_and(|w|!w.is_finished())}
    pub fn stop(&mut self) {self.stop.store(true,Ordering::Release);if let Some(worker)=self.worker.take(){let _=worker.join();}}
}
impl Drop for Server {fn drop(&mut self){self.stop();}}
fn response(stream:&mut TcpStream,status:&str,content_type:&str,body:&[u8],extra:&str)->io::Result<()> {
    write!(stream,"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\nContent-Security-Policy: {CSP}\r\n{extra}\r\n",body.len())?;
    stream.write_all(body)
}
fn query_number(query:&str,key:&str,default:u64)->u64 {query.split('&').find_map(|part|part.split_once('=').filter(|(k,_)|*k==key).and_then(|(_,v)|v.parse().ok())).unwrap_or(default)}
fn handle(mut stream:TcpStream,token:&str,ledger:&Ledger,stop:&AtomicBool)->io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_millis(100)))?; stream.set_write_timeout(Some(Duration::from_secs(2)))?;
    let deadline=Instant::now()+Duration::from_secs(2);let mut header=Vec::new();
    while !header.ends_with(b"\r\n\r\n") {
        if stop.load(Ordering::Acquire) {return Ok(());}
        if header.len()>=HEADER_CAP || Instant::now()>=deadline {return response(&mut stream,"431 Request Header Fields Too Large","text/plain",b"bounded request headers","");}
        let mut byte=[0u8;1];match stream.read(&mut byte) {Ok(0)=>return Ok(()),Ok(_)=>header.push(byte[0]),Err(e) if matches!(e.kind(),io::ErrorKind::WouldBlock|io::ErrorKind::TimedOut)=>continue,Err(e)=>return Err(e)}
    }
    let Ok(header)=std::str::from_utf8(&header) else {return response(&mut stream,"400 Bad Request","text/plain",b"invalid request","");};
    let mut lines=header.split("\r\n");let request:Vec<_>=lines.next().unwrap_or("").split_whitespace().collect();
    if request.len()!=3 || request[0]!="GET" {return response(&mut stream,"405 Method Not Allowed","text/plain",b"GET only","");}
    let (path,query)=request[1].split_once('?').unwrap_or((request[1],""));
    let prefix=format!("/{token}");
    if path==prefix {return response(&mut stream,"301 Moved Permanently","text/plain",b"",&format!("Location: {prefix}/\r\n"));}
    let Some(route)=path.strip_prefix(&(prefix+"/")) else {return response(&mut stream,"404 Not Found","text/plain",b"not found","");};
    let asset=match route {""|"index.html"=>Some(("text/html; charset=utf-8",include_bytes!("../../../assets/mesh/index.html").as_slice())),"mesh.js"=>Some(("application/javascript; charset=utf-8",include_bytes!("../../../assets/mesh/mesh.js").as_slice())),"mesh.css"=>Some(("text/css; charset=utf-8",include_bytes!("../../../assets/mesh/mesh.css").as_slice())),_=>None};
    if let Some((kind,body))=asset {return response(&mut stream,"200 OK",kind,body,"");}
    if !matches!(route,"ledger"|"events") {return response(&mut stream,"404 Not Found","text/plain",b"not found","");}
    let end=match ledger.size(){Ok(size)=>size,Err(_)=>return response(&mut stream,"413 Content Too Large","text/plain",b"ledger limit exceeded","")};
    let mut offset=query_number(query,"from",if route=="events"{end}else{0});
    if route=="ledger" {
        let end=query_number(query,"until",end).min(end);
        let (rows,next)=match ledger.batch(offset,Some(end)){Ok(batch)=>batch,Err(_)=>return response(&mut stream,"413 Content Too Large","text/plain",b"ledger limit exceeded","")};
        let mut value=json!({"records":rows.into_iter().map(|(row,_)|row).collect::<Vec<_>>(),"offset":next});
        if offset<next && next<end {value["more"]=json!(true);value["snapshot_end"]=json!(end);}
        return response(&mut stream,"200 OK","application/json; charset=utf-8",&json_bytes(&value),"");
    }
    for line in lines {if let Some((key,value))=line.split_once(':'){if key.eq_ignore_ascii_case("last-event-id") {let Ok(cursor)=value.trim().parse::<u64>() else{return response(&mut stream,"400 Bad Request","text/plain",b"invalid event cursor","");};offset=cursor;}}}
    write!(stream,"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nCache-Control: no-store\r\nConnection: close\r\nX-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\nContent-Security-Policy: {CSP}\r\n\r\n: open\n\n")?;
    let mut heartbeat=Instant::now();
    while !stop.load(Ordering::Acquire) {
        let(rows,next)=ledger.batch(offset,None)?;offset=next;
        for(row,position)in rows {write!(stream,"id: {position}\ndata: ")?;stream.write_all(&json_bytes(&row))?;stream.write_all(b"\n\n")?;heartbeat=Instant::now();}
        if heartbeat.elapsed()>=Duration::from_secs(15){stream.write_all(b": beat\n\n")?;heartbeat=Instant::now();}
        for _ in 0..5 {if stop.load(Ordering::Acquire){break;}thread::sleep(Duration::from_millis(50));}
    }
    Ok(())
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
