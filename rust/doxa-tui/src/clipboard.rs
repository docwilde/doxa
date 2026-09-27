//! Clipboard actions only follow explicit keyboard input. No shell, auto-read or auto-submit.
use std::{io::{self,Read}, os::{fd::OwnedFd,unix::{net::UnixStream,process::CommandExt}}, path::Path,
    process::{Command,Stdio},sync::{Arc,atomic::{AtomicBool,Ordering},mpsc},thread::JoinHandle,time::{Duration,Instant}};
pub const LIMIT:usize=64*1024;
pub fn osc52(text:&str)->Vec<u8> {
    let mut end=text.len().min(LIMIT);while !text.is_char_boundary(end){end-=1;}
    let bytes=&text.as_bytes()[..end];let alphabet=b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out=b"\x1b]52;c;".to_vec();
    for group in bytes.chunks(3){let n=(u32::from(group[0])<<16)|(u32::from(*group.get(1).unwrap_or(&0))<<8)|u32::from(*group.get(2).unwrap_or(&0));out.push(alphabet[((n>>18)&63)as usize]);out.push(alphabet[((n>>12)&63)as usize]);out.push(if group.len()>1{alphabet[((n>>6)&63)as usize]}else{b'='});out.push(if group.len()>2{alphabet[(n&63)as usize]}else{b'='});}
    out.push(7);out
}
#[derive(Clone,Debug)]
pub struct Target {pub pane:usize,pub session:String,pub draft:String,pub cursor:usize}
#[derive(Debug)]
pub struct Job {pub target:Target,rx:mpsc::Receiver<io::Result<String>>,cancel:Arc<AtomicBool>,thread:Option<JoinHandle<()>>}
impl Job {
    pub fn start(target:Target)->io::Result<Self>{
        if cfg!(test) { return Err(io::Error::other("clipboard reader requires an injected fixture in tests")); }
        let mut command=reader_command()?;let(tx,rx)=mpsc::sync_channel(1);let cancel=Arc::new(AtomicBool::new(false));let cancelled=cancel.clone();
        let thread=std::thread::spawn(move||{let _=tx.send(read_command(&mut command,&cancelled,Duration::from_secs(1)));});Ok(Self{target,rx,cancel,thread:Some(thread)})
    }
    pub fn poll(&self)->Option<io::Result<String>>{self.rx.try_recv().ok()}
    #[cfg(test)]
    pub fn fixture(target:Target,value:io::Result<String>)->Self{let(tx,rx)=mpsc::sync_channel(1);tx.send(value).unwrap();Self{target,rx,cancel:Arc::new(AtomicBool::new(false)),thread:None}}
}
impl Drop for Job {fn drop(&mut self){self.cancel.store(true,Ordering::Release);if let Some(thread)=self.thread.take(){let _=thread.join();}}}
fn reader_command()->io::Result<Command>{
    let candidates:[(&str,&[&str]);3]=[("/usr/bin/wl-paste",&["--no-newline"]),("/usr/bin/xclip",&["-selection","clipboard","-o"]),("/usr/bin/pbpaste",&[])];
    for(path,args)in candidates {let usable=match path {"/usr/bin/wl-paste"=>std::env::var_os("WAYLAND_DISPLAY").is_some(),"/usr/bin/xclip"=>std::env::var_os("DISPLAY").is_some(),_=>cfg!(target_os="macos")};if usable&&Path::new(path).is_file(){let mut command=Command::new(path);command.args(args);return Ok(command);}}
    Err(io::Error::new(io::ErrorKind::NotFound,"clipboard reader unavailable"))
}
fn read_command(command:&mut Command,cancel:&AtomicBool,timeout:Duration)->io::Result<String>{
    let(mut reader,writer)=UnixStream::pair()?;reader.set_nonblocking(true)?;
    let mut child=command.stdin(Stdio::null()).stdout(Stdio::from(OwnedFd::from(writer))).stderr(Stdio::null()).process_group(0).spawn()?;
    command.stdout(Stdio::null()); // release the parent's pipe writer after spawn
    let started=Instant::now();let mut raw=Vec::new();let mut buffer=[0u8;8192];let mut eof=false;let mut exited=None;
    let result='read: loop {
        if cancel.load(Ordering::Acquire)||started.elapsed()>=timeout{break Err(io::Error::new(io::ErrorKind::TimedOut,"clipboard read cancelled or timed out"));}
        for _ in 0..8 {match reader.read(&mut buffer){Ok(0)=>{eof=true;break},Ok(n)=>{if raw.len()+n>LIMIT {break 'read Err(io::Error::new(io::ErrorKind::InvalidData,"clipboard exceeds 64 KiB"));}raw.extend_from_slice(&buffer[..n]);},Err(e)if e.kind()==io::ErrorKind::WouldBlock=>break,Err(e)if e.kind()==io::ErrorKind::Interrupted=>continue,Err(e)=>break 'read Err(e),}}
        // Detect overflow even when a writer continues beyond the bounded buffer.
        if raw.len()==LIMIT {match reader.read(&mut buffer){Ok(n)if n>0=>break Err(io::Error::new(io::ErrorKind::InvalidData,"clipboard exceeds 64 KiB")),Ok(0)=>eof=true,Err(e)if e.kind()==io::ErrorKind::WouldBlock=>{},Err(e)=>break Err(e),_=>{}}}
        match child.try_wait(){Ok(Some(status))=>exited=Some(status),Ok(None)=>{},Err(e)=>break Err(e)}
        if eof&&exited.is_some(){break if exited.unwrap().success(){String::from_utf8(raw).map_err(|_|io::Error::new(io::ErrorKind::InvalidData,"clipboard is not text"))}else{Err(io::Error::other("clipboard reader failed"))};}
        std::thread::sleep(Duration::from_millis(5));
    };
    if result.is_err(){unsafe{libc::kill(-(child.id()as i32),libc::SIGKILL);}}let _=child.wait();result
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]fn osc52_encodes_controls_and_bounds_utf8(){assert_eq!(osc52("a\x1b"),b"\x1b]52;c;YRs=\x07");assert_eq!(osc52("ab"),b"\x1b]52;c;YWI=\x07");assert!(osc52(&"界".repeat(LIMIT)).len()<=7+4*((LIMIT+2)/3)+1);}
    #[test]fn private_reader_accepts_text_and_rejects_overflow_without_system_clipboard(){
        let mut command=Command::new("/usr/bin/python3");command.args(["-c","print('fixture', end='')"]);
        assert_eq!(read_command(&mut command,&AtomicBool::new(false),Duration::from_secs(1)).unwrap(),"fixture");
        let mut command=Command::new("/usr/bin/python3");command.args(["-c","import sys; sys.stdout.write('x'*70000)"]);
        assert_eq!(read_command(&mut command,&AtomicBool::new(false),Duration::from_secs(1)).unwrap_err().kind(),io::ErrorKind::InvalidData);
    }
    #[test]fn private_reader_times_out_and_reaps_without_reading_system_clipboard(){let mut command=Command::new("/usr/bin/python3");command.args(["-c","import time; time.sleep(10)"]);let start=Instant::now();assert_eq!(read_command(&mut command,&AtomicBool::new(false),Duration::from_millis(40)).unwrap_err().kind(),io::ErrorKind::TimedOut);assert!(start.elapsed()<Duration::from_secs(1));}
}
