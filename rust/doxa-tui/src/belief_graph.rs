//! Private, lazy graph-page serving. LORE produces the HTML; this module only
//! writes and serves it under DOXA's own graphs directory, never LORE's store.
use std::{io::{self,Read,Write},net::{TcpListener,TcpStream},path::{Path,PathBuf},sync::{Arc,atomic::{AtomicBool,Ordering}},thread,time::Duration};
use std::os::unix::fs::{OpenOptionsExt,MetadataExt,PermissionsExt};
pub const MAX_HTML:usize=1024*1024;
#[derive(Debug)]
pub struct GraphServer { addr:std::net::SocketAddr, stop:Arc<AtomicBool>, worker:Option<thread::JoinHandle<()>>, directory:PathBuf, token:String }
impl Drop for GraphServer {fn drop(&mut self){self.stop.store(true,Ordering::Relaxed);let _=TcpStream::connect_timeout(&self.addr,Duration::from_millis(100));if let Some(worker)=self.worker.take(){let _=worker.join();}}}
fn private_directory(path:&Path)->io::Result<()> {
    match std::fs::symlink_metadata(path) {Ok(m) if m.file_type().is_symlink() || !m.is_dir() || m.uid()!=unsafe{libc::geteuid()}=>return Err(io::Error::new(io::ErrorKind::PermissionDenied,"unsafe graph directory")),Ok(_)=>{},Err(e) if e.kind()==io::ErrorKind::NotFound=>std::fs::create_dir(path)?,Err(e)=>return Err(e)}
    std::fs::set_permissions(path,std::fs::Permissions::from_mode(0o700))
}
pub fn write_page(home:&Path,id:u64,html:&str)->io::Result<PathBuf> {
    if id==0 || html.len()>MAX_HTML || html.trim().is_empty(){return Err(io::Error::new(io::ErrorKind::InvalidData,"invalid LORE graph HTML"));}
    private_directory(home)?;let directory=home.join("graphs");private_directory(&directory)?;
    let mut file=tempfile::Builder::new().prefix(".belief-").tempfile_in(&directory)?;
    file.as_file().set_permissions(std::fs::Permissions::from_mode(0o600))?;file.write_all(html.as_bytes())?;file.as_file().sync_all()?;
    let path=directory.join(format!("belief-{id}.html"));
    if std::fs::symlink_metadata(&path).is_ok_and(|m|m.file_type().is_symlink() || !m.is_file() || m.uid()!=unsafe{libc::geteuid()} || m.nlink()!=1){return Err(io::Error::new(io::ErrorKind::PermissionDenied,"unsafe graph page"));}
    file.persist(&path).map_err(|e|e.error)?;Ok(path)
}
fn serve(mut stream:TcpStream,directory:&Path,token:&str) {
    let _=stream.set_read_timeout(Some(Duration::from_secs(2)));let _=stream.set_write_timeout(Some(Duration::from_secs(2)));
    let mut request=[0u8;4096];let Ok(count)=stream.read(&mut request) else{return;};
    let request=String::from_utf8_lossy(&request[..count]);let mut parts=request.lines().next().unwrap_or("").split_whitespace();
    let method=parts.next().unwrap_or("");let target=parts.next().unwrap_or("");
    let name=target.strip_prefix(&format!("/{token}/belief-")).and_then(|v|v.strip_suffix(".html")).filter(|v|!v.is_empty() && v.bytes().all(|b|b.is_ascii_digit()));
    let body=if method=="GET" {name.and_then(|id|{
        let file=std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK).open(directory.join(format!("belief-{id}.html"))).ok()?;
        let m=file.metadata().ok()?;if !m.is_file() || m.uid()!=unsafe{libc::geteuid()} || m.nlink()!=1 || m.len()>MAX_HTML as u64{return None;}
        let mut bytes=Vec::new();file.take((MAX_HTML+1) as u64).read_to_end(&mut bytes).ok()?;if bytes.len()>MAX_HTML{return None;}Some(bytes)
    })}else{None};
    let (status,body)=body.map(|body|("200 OK",body)).unwrap_or_else(||("404 Not Found",b"Not found".to_vec()));
    let header=format!("HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n",body.len());let _=stream.write_all(header.as_bytes());let _=stream.write_all(&body);
}
impl GraphServer {
    pub fn start(directory:PathBuf)->io::Result<Self> {
        let listener=TcpListener::bind((std::net::Ipv4Addr::LOCALHOST,0))?;let addr=listener.local_addr()?;
        let mut entropy=[0u8;16];std::fs::File::open("/dev/urandom")?.read_exact(&mut entropy)?;let token=entropy.iter().map(|b|format!("{b:02x}")).collect::<String>();
        let stop=Arc::new(AtomicBool::new(false));let stopping=stop.clone();let served=directory.clone();let served_token=token.clone();
        let worker=thread::spawn(move||{for stream in listener.incoming(){if stopping.load(Ordering::Relaxed){break;}match stream{Ok(stream)=>serve(stream,&served,&served_token),Err(_)=>break}}});
        Ok(Self{addr,stop,worker:Some(worker),directory,token})
    }
    pub fn matches(&self,directory:&Path)->bool{self.directory==directory}
    pub fn url(&self,id:u64)->String{format!("http://{}/{}/belief-{id}.html",self.addr,self.token)}
}
#[cfg(test)]mod tests{
    use super::*;
    #[test]fn graph_page_serves_only_exact_pages_and_stops_on_drop(){let dir=tempfile::tempdir().unwrap();let page=write_page(dir.path(),17,"<html>LORE graph</html>").unwrap();let server=GraphServer::start(page.parent().unwrap().into()).unwrap();let addr=server.addr;let old_url=server.url(17);let mut stream=TcpStream::connect(addr).unwrap();stream.write_all(format!("GET /{}/belief-17.html HTTP/1.1\r\nHost: localhost\r\n\r\n",server.token).as_bytes()).unwrap();let mut reply=String::new();stream.read_to_string(&mut reply).unwrap();assert!(reply.starts_with("HTTP/1.1 200"));assert!(reply.contains("LORE graph"));let mut stream=TcpStream::connect(addr).unwrap();stream.write_all(b"GET /wrong-token/belief-17.html HTTP/1.1\r\n\r\n").unwrap();let mut denied=String::new();stream.read_to_string(&mut denied).unwrap();assert!(denied.starts_with("HTTP/1.1 404"));let mut stream=TcpStream::connect(addr).unwrap();stream.write_all(b"GET /../config.toml HTTP/1.1\r\n\r\n").unwrap();let mut reply=String::new();stream.read_to_string(&mut reply).unwrap();assert!(reply.starts_with("HTTP/1.1 404"));drop(server);assert!(!old_token_served(&old_url));}
    fn old_token_served(url:&str)->bool {let Some((address,path))=url.strip_prefix("http://").and_then(|tail|tail.split_once('/')) else{return false;};let Ok(mut stream)=TcpStream::connect(address) else{return false;};let _=stream.set_read_timeout(Some(Duration::from_millis(250)));let _=write!(stream,"GET /{path} HTTP/1.0\r\nHost: {address}\r\n\r\n");let mut reply=String::new();let _=stream.read_to_string(&mut reply);reply.starts_with("HTTP/1.1 200")||reply.starts_with("HTTP/1.0 200")}
    #[test]fn graph_writes_refuse_links_and_large_payloads(){let dir=tempfile::tempdir().unwrap();std::os::unix::fs::symlink(dir.path(),dir.path().join("graphs")).unwrap();assert!(write_page(dir.path(),1,"<html/>").is_err());assert!(write_page(dir.path(),1,&"x".repeat(MAX_HTML+1)).is_err());}
}
