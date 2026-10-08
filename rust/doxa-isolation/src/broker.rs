//! One narrow session-bound Codex compaction endpoint, outside the worker.
use super::{active, error, nonce, Manifest};
use serde_json::{json, Value};
use std::{fs, io::{self, Read, Write}, os::unix::{fs::{MetadataExt, PermissionsExt}, net::UnixListener},
    path::PathBuf, sync::{Arc, atomic::{AtomicBool, Ordering}}, time::Duration};

pub struct HookBroker { socket: PathBuf, identity: (u64,u64), stopping: Arc<AtomicBool>, worker: Option<std::thread::JoinHandle<()>>, command: String }
impl HookBroker {
    pub fn start(handler: impl Fn(Value, &Manifest) -> Value + Send + 'static) -> io::Result<Option<Self>> {
        let Some(manifest) = active()? else { return Ok(None); };
        let socket = manifest.broker.join("hook.sock");
        // A previous host may have crashed. Only a socket in our already
        // owner-checked private directory may be replaced, never a symlink.
        if let Ok(meta) = fs::symlink_metadata(&socket) {
            use std::os::unix::fs::FileTypeExt;
            if !meta.file_type().is_socket() || meta.uid() != unsafe { libc::geteuid() } { return Err(error("unsafe stale isolation broker")); }
            if std::os::unix::net::UnixStream::connect(&socket).is_ok() { return Err(error("session hook broker is already live")); }
            fs::remove_file(&socket)?;
        }
        let listener = UnixListener::bind(&socket)?; fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
        listener.set_nonblocking(true)?;
        let meta = fs::metadata(&socket)?; let identity = (meta.dev(),meta.ino());
        let capability = nonce()?;
        let command = format!("'/usr/local/bin/doxa-isolation-worker' 'hook' '/run/doxa/session/hook.sock' '{capability}'");
        let stopping = Arc::new(AtomicBool::new(false)); let stop = stopping.clone();
        let worker = std::thread::spawn(move || {
            while !stop.load(Ordering::Acquire) {
                let (mut stream, _) = match listener.accept() { Ok(connection) => connection, Err(e) if e.kind() == io::ErrorKind::WouldBlock => { std::thread::sleep(Duration::from_millis(20)); continue; }, Err(_) => break };
                let _ = stream.set_read_timeout(Some(Duration::from_secs(5))); let _ = stream.set_write_timeout(Some(Duration::from_secs(5)));
                let result = (|| -> io::Result<Value> {
                    let mut size = [0;4]; stream.read_exact(&mut size)?; let size = u32::from_be_bytes(size) as usize;
                    if size > 65536 { return Err(error("broker frame exceeds bound")); }
                    let mut bytes = vec![0;size]; stream.read_exact(&mut bytes)?;
                    let frame: Value = serde_json::from_slice(&bytes)?;
                    if frame["version"] != 1 || frame["capability"] != capability || !frame["event"].is_object() { return Err(error("invalid session broker capability/schema")); }
                    let mut event = frame["event"].clone();
                    if let Some(path) = event["transcript_path"].as_str() {
                        let relative = std::path::Path::new(path).strip_prefix("/home/doxa/codex")
                            .map_err(|_| error("worker transcript path outside private Codex home"))?;
                        if relative.components().any(|p| !matches!(p,std::path::Component::Normal(_))) { return Err(error("invalid broker transcript path")); }
                        event["transcript_path"] = json!(manifest.private_home.join("codex").join(relative));
                    }
                    Ok(handler(event, &manifest))
                })().unwrap_or_else(|_| json!({"continue":false,"suppressOutput":true,"stopReason":"DOXA rejected invalid or unreviewed session hook"}));
                if let Ok(bytes) = serde_json::to_vec(&result) { let _ = stream.write_all(&(bytes.len() as u32).to_be_bytes()).and_then(|_| stream.write_all(&bytes)); }
            }
        });
        Ok(Some(Self { socket, identity, stopping, worker:Some(worker), command }))
    }
    pub fn command(&self) -> &str { &self.command }
}
impl Drop for HookBroker {
    fn drop(&mut self) {
        self.stopping.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() { let _ = worker.join(); }
        if fs::symlink_metadata(&self.socket).is_ok_and(|m| (m.dev(),m.ino()) == self.identity) { let _ = fs::remove_file(&self.socket); }
    }
}
