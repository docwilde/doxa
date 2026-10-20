//! Clipboard actions only follow explicit keyboard input. No shell, auto-read or auto-submit.
use std::{io::{self,Read,Write}, os::{fd::OwnedFd,unix::{net::UnixStream,process::CommandExt}}, path::Path,
    process::{Command,Stdio},sync::{Arc,atomic::{AtomicBool,Ordering},mpsc},thread::JoinHandle,time::{Duration,Instant}};
pub const LIMIT:usize=64*1024;
pub fn osc52(text:&str)->Vec<u8> {
    let mut end=text.len().min(LIMIT);while !text.is_char_boundary(end){end-=1;}
    let bytes=&text.as_bytes()[..end];let alphabet=b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out=b"\x1b]52;c;".to_vec();
    for group in bytes.chunks(3){let n=(u32::from(group[0])<<16)|(u32::from(*group.get(1).unwrap_or(&0))<<8)|u32::from(*group.get(2).unwrap_or(&0));out.push(alphabet[((n>>18)&63)as usize]);out.push(alphabet[((n>>12)&63)as usize]);out.push(if group.len()>1{alphabet[((n>>6)&63)as usize]}else{b'='});out.push(if group.len()>2{alphabet[(n&63)as usize]}else{b'='});}
    out.push(7);out
}

/// Desktop copy uses a fixed helper when available, then the caller can fall back to OSC52.
#[derive(Debug)]
pub struct CopyJob {
    pub text:String,
    rx:mpsc::Receiver<io::Result<()>>,
    cancel:Arc<AtomicBool>,
    thread:Option<JoinHandle<()>>,
}
impl CopyJob {
    pub fn start(text:String)->io::Result<Self> {
        if cfg!(test) { return Err(io::Error::other("clipboard writer requires an injected fixture in tests")); }
        Ok(Self::with_command(writer_command()?,text,Duration::from_secs(1)))
    }
    fn with_command(mut command:Command,text:String,timeout:Duration)->Self {
        let text=bounded_text(&text).to_owned();
        let input=text.clone();
        let(tx,rx)=mpsc::sync_channel(1);
        let cancel=Arc::new(AtomicBool::new(false));let cancelled=cancel.clone();
        let thread=std::thread::spawn(move||{let _=tx.send(write_command(&mut command,&input,&cancelled,timeout));});
        Self{text,rx,cancel,thread:Some(thread)}
    }
    pub fn poll(&self)->Option<io::Result<()>> {self.rx.try_recv().ok()}
}
impl Drop for CopyJob {
    fn drop(&mut self) {self.cancel.store(true,Ordering::Release);if let Some(thread)=self.thread.take(){let _=thread.join();}}
}
fn bounded_text(text:&str)->&str {
    let mut end=text.len().min(LIMIT);while !text.is_char_boundary(end){end-=1;} &text[..end]
}
fn writer_command()->io::Result<Command> {
    let candidates:[(&str,&[&str]);3]=[("/usr/bin/wl-copy",&["--type","text/plain;charset=utf-8"]),("/usr/bin/xclip",&["-selection","clipboard","-in"]),("/usr/bin/pbcopy",&[])];
    for(path,args)in candidates {
        let usable=match path {"/usr/bin/wl-copy"=>std::env::var_os("WAYLAND_DISPLAY").is_some(),"/usr/bin/xclip"=>std::env::var_os("DISPLAY").is_some(),_=>cfg!(target_os="macos")};
        if usable&&Path::new(path).is_file(){let mut command=Command::new(path);command.args(args);return Ok(command);}
    }
    Err(io::Error::new(io::ErrorKind::NotFound,"desktop clipboard helper unavailable"))
}
fn write_command(command:&mut Command,text:&str,cancel:&AtomicBool,timeout:Duration)->io::Result<()> {
    let(mut writer,reader)=UnixStream::pair()?;writer.set_nonblocking(true)?;
    let mut child=command.stdin(Stdio::from(OwnedFd::from(reader))).stdout(Stdio::null()).stderr(Stdio::null()).process_group(0).spawn()?;
    command.stdin(Stdio::null()); // release the parent's read endpoint after spawn
    let started=Instant::now();let input=bounded_text(text).as_bytes();let mut written=0;let mut closed=false;
    let result=loop {
        if cancel.load(Ordering::Acquire)||started.elapsed()>=timeout {break Err(io::Error::new(io::ErrorKind::TimedOut,"clipboard write cancelled or timed out"));}
        if written<input.len() {
            match writer.write(&input[written..]) {
                Ok(0)=>break Err(io::Error::new(io::ErrorKind::WriteZero,"clipboard helper closed input")),
                Ok(n)=>written+=n,
                Err(e)if matches!(e.kind(),io::ErrorKind::WouldBlock|io::ErrorKind::Interrupted)=>{},
                Err(e)=>break Err(e),
            }
        }
        if written==input.len()&&!closed {
            if let Err(e)=writer.shutdown(std::net::Shutdown::Write){break Err(e);}
            closed=true;
        }
        match child.try_wait() {
            Ok(Some(status))=>break if status.success()&&closed {Ok(())}else{Err(io::Error::other("clipboard helper failed"))},
            Ok(None)=>{},Err(e)=>break Err(e),
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    if result.is_err(){unsafe{libc::kill(-(child.id()as i32),libc::SIGKILL);}}
    let _=child.wait();result
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
    read_command_bytes(command,cancel,timeout,LIMIT).and_then(|bytes|String::from_utf8(bytes).map_err(|_|io::Error::new(io::ErrorKind::InvalidData,"clipboard is not text")))
}
fn read_command_bytes(command:&mut Command,cancel:&AtomicBool,timeout:Duration,limit:usize)->io::Result<Vec<u8>>{
    let(mut reader,writer)=UnixStream::pair()?;reader.set_nonblocking(true)?;
    let mut child=command.stdin(Stdio::null()).stdout(Stdio::from(OwnedFd::from(writer))).stderr(Stdio::null()).process_group(0).spawn()?;
    command.stdout(Stdio::null()); // release the parent's pipe writer after spawn
    let started=Instant::now();let mut raw=Vec::new();let mut buffer=[0u8;8192];let mut eof=false;let mut exited=None;
    let result='read: loop {
        if cancel.load(Ordering::Acquire)||started.elapsed()>=timeout{break Err(io::Error::new(io::ErrorKind::TimedOut,"clipboard read cancelled or timed out"));}
        for _ in 0..8 {match reader.read(&mut buffer){Ok(0)=>{eof=true;break},Ok(n)=>{if raw.len()+n>limit {break 'read Err(io::Error::new(io::ErrorKind::InvalidData,"clipboard exceeds limit"));}raw.extend_from_slice(&buffer[..n]);},Err(e)if e.kind()==io::ErrorKind::WouldBlock=>break,Err(e)if e.kind()==io::ErrorKind::Interrupted=>continue,Err(e)=>break 'read Err(e),}}
        // Detect overflow even when a writer continues beyond the bounded buffer.
        if raw.len()==limit {match reader.read(&mut buffer){Ok(n)if n>0=>break Err(io::Error::new(io::ErrorKind::InvalidData,"clipboard exceeds limit")),Ok(0)=>eof=true,Err(e)if e.kind()==io::ErrorKind::WouldBlock=>{},Err(e)=>break Err(e),_=>{}}}
        match child.try_wait(){Ok(Some(status))=>exited=Some(status),Ok(None)=>{},Err(e)=>break Err(e)}
        if eof&&exited.is_some(){break if exited.unwrap().success(){Ok(raw)}else{Err(io::Error::other("clipboard reader failed"))};}
        std::thread::sleep(Duration::from_millis(5));
    };
    if result.is_err(){unsafe{libc::kill(-(child.id()as i32),libc::SIGKILL);}}let _=child.wait();result
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]fn osc52_encodes_controls_and_bounds_utf8(){assert_eq!(osc52("a\x1b"),b"\x1b]52;c;YRs=\x07");assert_eq!(osc52("ab"),b"\x1b]52;c;YWI=\x07");assert!(osc52(&"界".repeat(LIMIT)).len()<=7+4*((LIMIT+2)/3)+1);}
    #[test]fn private_writer_preserves_text_and_bounds_utf8_without_system_clipboard(){
        let fixture=tempfile::tempdir().unwrap();let output=fixture.path().join("clipboard.txt");
        let value="unicode 界\ncontrol \u{1b}\u{7}";
        let mut command=Command::new("python3");command.args(["-c","import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"]).arg(&output);
        write_command(&mut command,value,&AtomicBool::new(false),Duration::from_secs(1)).unwrap();
        assert_eq!(std::fs::read_to_string(&output).unwrap(),value);
        let value="界".repeat(LIMIT);
        let mut command=Command::new("python3");command.args(["-c","import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"]).arg(&output);
        write_command(&mut command,&value,&AtomicBool::new(false),Duration::from_secs(1)).unwrap();
        assert_eq!(std::fs::read_to_string(output).unwrap(),bounded_text(&value));
    }
    #[test]fn private_writer_bounds_wait_and_reports_failure_without_system_clipboard(){
        let mut command=Command::new("python3");command.args(["-c","import time; time.sleep(10)"]);
        let start=Instant::now();assert_eq!(write_command(&mut command,&"x".repeat(LIMIT),&AtomicBool::new(false),Duration::from_millis(40)).unwrap_err().kind(),io::ErrorKind::TimedOut);assert!(start.elapsed()<Duration::from_secs(1));
        let mut command=Command::new("python3");command.args(["-c","import sys; sys.stdin.buffer.read(); sys.exit(1)"]);
        assert!(write_command(&mut command,"fixture",&AtomicBool::new(false),Duration::from_secs(1)).is_err());
        assert!(CopyJob::start("fixture".into()).is_err());
    }
    #[test]fn dropping_copy_job_cancels_and_reaps_its_private_helper(){
        let fixture=tempfile::tempdir().unwrap();let pidfile=fixture.path().join("helper.pid");
        let mut command=Command::new("python3");command.args(["-c","import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(10)"]).arg(&pidfile);
        let job=CopyJob::with_command(command,"fixture".into(),Duration::from_secs(10));
        let deadline=Instant::now()+Duration::from_secs(2);
        while !pidfile.exists()&&Instant::now()<deadline {std::thread::sleep(Duration::from_millis(5));}
        let pid:libc::pid_t=std::fs::read_to_string(pidfile).unwrap().parse().unwrap();
        let started=Instant::now();drop(job);assert!(started.elapsed()<Duration::from_secs(1));
        assert_ne!(unsafe{libc::kill(pid,0)},0,"helper must be reaped before cancellation returns");
    }
    #[test]fn private_reader_accepts_text_and_rejects_overflow_without_system_clipboard(){
        let mut command=Command::new("python3");command.args(["-c","print('fixture', end='')"]);
        assert_eq!(read_command(&mut command,&AtomicBool::new(false),Duration::from_secs(1)).unwrap(),"fixture");
        let mut command=Command::new("python3");command.args(["-c","import sys; sys.stdout.write('x'*70000)"]);
        assert_eq!(read_command(&mut command,&AtomicBool::new(false),Duration::from_secs(1)).unwrap_err().kind(),io::ErrorKind::InvalidData);
    }
    #[test]fn private_reader_times_out_and_reaps_without_reading_system_clipboard(){let mut command=Command::new("python3");command.args(["-c","import time; time.sleep(10)"]);let start=Instant::now();assert_eq!(read_command(&mut command,&AtomicBool::new(false),Duration::from_millis(40)).unwrap_err().kind(),io::ErrorKind::TimedOut);assert!(start.elapsed()<Duration::from_secs(1));}
}

/// Only a normal prompt's explicit Ctrl+V probes image MIME types. Credential
/// editors continue to use the text-only reader and never request image data.
#[derive(Debug)]
pub struct ImageJob { pub target:Target, rx:mpsc::Receiver<io::Result<Option<doxa_input::Attachment>>>, cancel:Arc<AtomicBool>, thread:Option<JoinHandle<()>> }
impl ImageJob {
    pub fn start(target:Target)->io::Result<Self>{
        if cfg!(test){return Err(io::Error::other("clipboard image reader requires a fixture"));}
        let(tx,rx)=mpsc::sync_channel(1);let cancel=Arc::new(AtomicBool::new(false));let cancelled=cancel.clone();
        let thread=std::thread::spawn(move||{let _=tx.send(read_image(&cancelled));});
        Ok(Self{target,rx,cancel,thread:Some(thread)})
    }
    pub fn poll(&self)->Option<io::Result<Option<doxa_input::Attachment>>>{self.rx.try_recv().ok()}
    #[cfg(test)]pub fn fixture(target:Target,value:io::Result<Option<doxa_input::Attachment>>)->Self{let(tx,rx)=mpsc::sync_channel(1);tx.send(value).unwrap();Self{target,rx,cancel:Arc::new(AtomicBool::new(false)),thread:None}}
}
impl Drop for ImageJob {fn drop(&mut self){self.cancel.store(true,Ordering::Release);if let Some(thread)=self.thread.take(){let _=thread.join();}}}
fn read_image(cancel:&AtomicBool)->io::Result<Option<doxa_input::Attachment>>{
    let (path,list,args):(&str,&[&str],&[&str])=if std::env::var_os("WAYLAND_DISPLAY").is_some()&&Path::new("/usr/bin/wl-paste").is_file(){
        ("/usr/bin/wl-paste",&["--list-types"],&["--type","image/png"])
    }else if std::env::var_os("DISPLAY").is_some()&&Path::new("/usr/bin/xclip").is_file(){
        ("/usr/bin/xclip",&["-selection","clipboard","-target","TARGETS","-out"],&["-selection","clipboard","-target","image/png","-out"])
    }else{return Ok(None);};
    let mut types=Command::new(path);types.args(list);
    let available=match read_command(&mut types,cancel,Duration::from_secs(1)){Ok(value)=>value,Err(_)=>return Ok(None)};
    if !available.lines().any(|line|line.trim()=="image/png"){return Ok(None);}
    let mut command=Command::new(path);command.args(args);
    let raw=read_command_bytes(&mut command,cancel,Duration::from_secs(2),doxa_input::MAX_SOURCE)?;
    doxa_input::Attachment::from_source("clipboard.png",raw).map(Some)
}
