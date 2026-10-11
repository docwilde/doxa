use crate::{wire, Error, ErrorKind, Limits, Result, config::{self, EnvRef}};
use serde_json::{json, Value};
use std::{collections::BTreeMap, os::unix::process::CommandExt, path::Path, process::Stdio, sync::{Arc, atomic::{AtomicBool, Ordering}}, time::Duration};
use tokio::{io::{AsyncBufReadExt, AsyncWriteExt, BufReader}, process::{Child, ChildStdin, ChildStdout, Command}};

pub(crate) struct StdioTransport {
    child: Child,
    pid: i32,
    input: Option<ChildStdin>,
    output: BufReader<ChildStdout>,
    closed: bool,
}
impl StdioTransport {
    pub(crate) fn open(program: &Path, args: &[String], env: &BTreeMap<String, EnvRef>, cwd: &Path) -> Result<(Self, Vec<String>)> {
        config::credential_free(&format!("{}\n{}",program.display(),args.join("\n")))?;
        let mut command = Command::new(program);
        command.args(args).current_dir(cwd).env_clear().stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).kill_on_drop(true);
        command.as_std_mut().process_group(0);
        let mut secrets = Vec::new();
        for (name, reference) in env {
            let value = config::resolve(reference)?;
            if args.iter().any(|arg| arg.contains(&value)) { return Err(Error::config("credentials must not occur in command arguments")); }
            command.env(name, &value);
            secrets.push(value);
        }
        let mut child = command.spawn().map_err(|_| Error::new(ErrorKind::Io, false))?;
        let pid = child.id().ok_or_else(|| Error::new(ErrorKind::Io, false))? as i32;
        let input = child.stdin.take();
        let output = BufReader::new(child.stdout.take().ok_or_else(|| Error::new(ErrorKind::Io, false))?);
        Ok((Self { child, pid, input, output, closed: false }, secrets))
    }
    pub(crate) async fn send(&mut self, value: &Value, limit: usize, attempted: &AtomicBool) -> Result<()> {
        let bytes = serde_json::to_vec(value).map_err(|_| Error::protocol("cannot encode request"))?;
        if bytes.len() >= limit { return Err(Error::new(ErrorKind::Limit("request bytes"), false)); }
        let input = self.input.as_mut().ok_or_else(|| Error::new(ErrorKind::Closed, false))?;
        attempted.store(true, Ordering::SeqCst);
        input.write_all(&bytes).await.map_err(|_| Error::new(ErrorKind::Io, true))?;
        input.write_all(b"\n").await.map_err(|_| Error::new(ErrorKind::Io, true))?;
        input.flush().await.map_err(|_| Error::new(ErrorKind::Io, true))?;
        Ok(())
    }
    async fn receive(&mut self, limit: usize) -> Result<Value> {
        let mut bytes = Vec::new();
        loop {
            let available = self.output.fill_buf().await.map_err(|_| Error::new(ErrorKind::Io, true))?;
            if available.is_empty() { return Err(Error::new(ErrorKind::Io, true)); }
            let take = available.iter().position(|&b| b == b'\n').map(|i|i + 1).unwrap_or(available.len());
            if bytes.len() + take > limit { return Err(Error::new(ErrorKind::Limit("response bytes"), true)); }
            let complete = available[take-1] == b'\n';
            bytes.extend_from_slice(&available[..take]);
            self.output.consume(take);
            if complete { return wire::parse(&bytes); }
        }
    }
    pub(crate) async fn exchange(&mut self, request: &Value, id: u64, limits: &Limits, attempted: Arc<AtomicBool>, changed: &mut bool) -> Result<Value> {
        self.send(request, limits.max_frame_bytes, &attempted).await?;
        for _ in 0..=limits.max_notifications {
            match wire::classify(self.receive(limits.max_frame_bytes).await?, id)? {
                wire::Message::Response(result) => return Ok(result),
                wire::Message::Notification(method) => if method == "notifications/tools/list_changed" { *changed = true; },
                wire::Message::Request(id, method) => {
                    let reply = if method == "ping" { json!({"jsonrpc":"2.0","id":id,"result":{}}) }
                        else { json!({"jsonrpc":"2.0","id":id,"error":{"code":-32601,"message":"Client capability not supported"}}) };
                    self.send(&reply, limits.max_frame_bytes, &attempted).await?;
                }
            }
        }
        Err(Error::new(ErrorKind::Limit("server messages"), true))
    }
    pub(crate) async fn close(&mut self, timeout_ms: u64) {
        if self.closed { return; }
        self.input.take();
        let grace = Duration::from_millis(timeout_ms);
        if tokio::time::timeout(grace, self.child.wait()).await.is_err() {
            unsafe { libc::kill(-self.pid, libc::SIGTERM); }
            if tokio::time::timeout(grace, self.child.wait()).await.is_err() {
                unsafe { libc::kill(-self.pid, libc::SIGKILL); }
                let _ = self.child.start_kill();
                let _ = tokio::time::timeout(grace, self.child.wait()).await;
            }
        }
        // Also remove any descendants which retained the process group/pipes.
        unsafe { libc::kill(-self.pid, libc::SIGKILL); }
        self.closed = true;
    }
}
impl Drop for StdioTransport {
    fn drop(&mut self) {
        if !self.closed {
            unsafe { libc::kill(-self.pid, libc::SIGKILL); }
            let _ = self.child.start_kill();
        }
    }
}
