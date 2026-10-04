//! Native TUI client for the private DOXA hub. The hub is the transport; the
//! existing terminal reducer remains the only owner of visible session state.
use crate::bridge::{self, WorkerCommand};
use crate::worker_frames::{CommandResult, PromptDelivery, WorkerFrame};
use futures_util::StreamExt;
use reqwest::{Client, Url};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::io;
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::sync::{mpsc as async_mpsc, watch, Semaphore};

const MAX_SESSIONS: usize = 64;
const MAX_JSON: usize = 128_000;
const MAX_SSE: usize = 128_000;
const MAX_RETRY_IDS: usize = 64;
type RetryIds = HashMap<String, (String, Instant)>;

fn invalid(message: &'static str) -> io::Error { io::Error::new(io::ErrorKind::InvalidInput, message) }
fn unavailable(message: &'static str) -> io::Error { io::Error::other(message) }

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 128 && id.as_bytes()[0].is_ascii_alphanumeric()
        && id.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
}
fn valid_target(id: &str) -> bool {
    id.split_once('~').is_some_and(|(host, session)| valid_id(host) && valid_id(session))
}
fn retry_key(kind: &str, id: &str, content: &str) -> String {
    format!("{kind}:{id}:{:x}", Sha256::digest(content.as_bytes()))
}
fn retry_id(saved: &Mutex<RetryIds>, key: &str) -> Option<String> {
    let mut saved = saved.lock().unwrap_or_else(|poison| poison.into_inner());
    saved.retain(|_, (_, created)| created.elapsed() < Duration::from_secs(120));
    if let Some((id, _)) = saved.get(key) { return Some(id.clone()); }
    if saved.len() >= MAX_RETRY_IDS { return None; }
    let id = uuid::Uuid::new_v4().to_string();
    saved.insert(key.to_owned(), (id.clone(), Instant::now()));
    Some(id)
}
fn clear_retry(saved: &Mutex<RetryIds>, key: &str) {
    saved.lock().unwrap_or_else(|poison| poison.into_inner()).remove(key);
}
fn hub_url(raw: &str) -> io::Result<Url> {
    let url = Url::parse(raw).map_err(|_| invalid("invalid hub URL"))?;
    if url.scheme() != "https" || !url.host_str().is_some_and(|host| host.ends_with(".ts.net"))
        || !url.username().is_empty() || url.password().is_some() || url.query().is_some()
        || url.fragment().is_some() || url.path() != "/" {
        return Err(invalid("hub URL must be a private https://*.ts.net origin"));
    }
    Ok(url)
}
fn http_client() -> io::Result<Client> {
    Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(5)).build()
        .map_err(|_| unavailable("remote HTTP client unavailable"))
}
async fn bounded_json(mut response: reqwest::Response) -> io::Result<Value> {
    let mut bytes = Vec::new();
    loop {
        let chunk = tokio::time::timeout(Duration::from_secs(10), response.chunk()).await
            .map_err(|_| unavailable("hub response timed out"))?
            .map_err(|_| unavailable("hub response failed"))?;
        let Some(chunk) = chunk else { break };
        if bytes.len().saturating_add(chunk.len()) > MAX_JSON {
            return Err(invalid("hub response exceeds bound"));
        }
        bytes.extend_from_slice(&chunk);
    }
    serde_json::from_slice(&bytes).map_err(|_| invalid("invalid hub JSON"))
}
async fn get(http: &Client, base: &Url, path: &str) -> io::Result<Value> {
    let url = base.join(path).map_err(|_| invalid("invalid hub route"))?;
    let response = tokio::time::timeout(Duration::from_secs(10), http.get(url).send()).await
        .map_err(|_| unavailable("hub request timed out"))?
        .map_err(|_| unavailable("hub unavailable"))?;
    if !response.status().is_success() { return Err(io::Error::new(io::ErrorKind::PermissionDenied, "hub refused request")); }
    bounded_json(response).await
}
async fn post(http: &Client, base: &Url, path: &str, body: Value) -> io::Result<Value> {
    let url = base.join(path).map_err(|_| invalid("invalid hub route"))?;
    let response = tokio::time::timeout(Duration::from_secs(10), http.post(url).json(&body).send()).await
        .map_err(|_| unavailable("hub request timed out"))?
        .map_err(|_| unavailable("hub unavailable"))?;
    if !response.status().is_success() { return Err(io::Error::new(io::ErrorKind::PermissionDenied, "hub refused request")); }
    bounded_json(response).await
}

#[derive(Clone, Debug)]
struct SessionInfo { id: String, title: String, host: String, engine: String, model: String }
fn sessions(value: &Value) -> io::Result<Vec<SessionInfo>> {
    let rows = value["sessions"].as_array().ok_or_else(|| invalid("invalid hub inventory"))?;
    if rows.len() > MAX_SESSIONS { return Err(invalid("hub session inventory exceeds bound")); }
    let mut seen = HashSet::new();
    rows.iter().map(|row| {
        let id = row["id"].as_str().filter(|id| valid_target(id)).ok_or_else(|| invalid("invalid remote session ID"))?;
        if !seen.insert(id) { return Err(invalid("duplicate remote session ID")); }
        let host = id.split_once('~').unwrap().0;
        let clean = |key: &str, limit: usize| -> String {
            row[key].as_str().unwrap_or("").chars().filter(|c| !c.is_control())
                .take(limit).collect()
        };
        let title = clean("title", 160);
        Ok(SessionInfo { id: id.into(), title: if title.is_empty() { id.into() } else { title },
            host: host.into(), engine: clean("engine", 40), model: clean("model", 100) })
    }).collect()
}
fn hello(session: &SessionInfo) -> WorkerFrame {
    WorkerFrame::Daemon { session_id: session.id.clone(), frame: json!({"type":"hello",
        "session_id":session.id,"title":format!("{} · {}",session.title,session.host),
        "engine":session.engine,"model":session.model,"remote":true,"running":false}) }
}

fn snapshot_markdown(snapshot: &Value) -> io::Result<String> {
    let turns = snapshot["turns"].as_array().filter(|turns| turns.len() <= 80)
        .ok_or_else(|| invalid("invalid remote transcript"))?;
    let mut output = String::new();
    if snapshot["dropped_turns"].as_u64().unwrap_or(0) > 0 {
        output.push_str("[Earlier remote turns omitted from this view.]\n\n");
    }
    for turn in turns {
        for (key, role) in [("prompt", "You"), ("text", "Assistant")] {
            if let Some(text) = turn[key].as_str().filter(|text| !text.is_empty()) {
                output.push_str(&format!("**{role}:**\n\n"));
                output.extend(text.chars().filter(|c| !c.is_control() || matches!(c, '\n' | '\t')).take(8_000));
                output.push_str("\n\n");
            }
        }
        for tool in turn["tools"].as_array().into_iter().flatten().take(4) {
            let name = tool["name"].as_str().unwrap_or("Tool");
            let result = tool["result"].as_str().unwrap_or("");
            output.push_str("Tool: ");
            output.extend(name.chars().filter(|c| !c.is_control()).take(120));
            if !result.is_empty() {
                output.push_str(" finished · ");
                output.extend(result.chars().filter(|c| !c.is_control() || matches!(c, '\n' | '\t')).take(2_000));
            }
            output.push_str("\n\n");
        }
    }
    if output.len() > 512 * 1024 {
        let mut start = output.len() - 512 * 1024;
        while !output.is_char_boundary(start) { start += 1; }
        output = format!("[Earlier remote turns omitted from this view.]\n\n{}", &output[start..]);
    }
    Ok(output)
}

async fn command_result(http: &Client, base: &Url, target: &str, operation: &str, body: Value) -> io::Result<Value> {
    let queued = post(http, base, &format!("api/sessions/{target}/{operation}"), body).await?;
    let id = queued["command_id"].as_str().filter(|id| valid_id(id))
        .ok_or_else(|| invalid("hub did not return command ID"))?;
    for _ in 0..240 {
        tokio::time::sleep(Duration::from_millis(250)).await;
        let status = get(http, base, &format!("api/commands/{id}")).await?;
        match status["status"].as_str() {
            Some("accepted") => return Ok(status["result"].clone()),
            Some("refused") => return Err(io::Error::new(io::ErrorKind::PermissionDenied,
                status["result"]["error"].as_str().unwrap_or("host refused command").to_owned())),
            Some("expired") => return Err(unavailable("command outcome uncertain; inspect session before retrying")),
            _ => {}
        }
    }
    Err(unavailable("command acknowledgement timed out; inspect session before retrying"))
}
async fn snapshot(http: &Client, base: &Url, target: &str) -> io::Result<(String, Value, bool, u64)> {
    let result = command_result(http, base, target, "transcript", json!({})).await?;
    if result["ok"] != true { return Err(unavailable("remote transcript refused")); }
    let markdown = snapshot_markdown(&result)?;
    let inputs = result["pending_inputs"].as_array().filter(|items| items.len() <= 64)
        .ok_or_else(|| invalid("invalid remote pending input snapshot"))?;
    let complete = result["pending_inputs_complete"].as_bool()
        .ok_or_else(|| invalid("remote pending input state missing"))?;
    let cursor = result["next_seq"].as_u64().ok_or_else(|| invalid("remote cursor missing"))?;
    Ok((markdown, Value::Array(inputs.clone()), complete, cursor))
}

#[derive(Default)]
struct SseDecoder { buffer: Vec<u8> }
impl SseDecoder {
    fn push(&mut self, chunk: &[u8]) -> io::Result<Vec<Value>> {
        if self.buffer.len().saturating_add(chunk.len()) > MAX_SSE { return Err(invalid("remote event exceeds bound")); }
        self.buffer.extend_from_slice(chunk);
        let mut frames = Vec::new();
        while let Some(end) = self.buffer.windows(2).position(|bytes| bytes == b"\n\n") {
            let block = self.buffer.drain(..end + 2).collect::<Vec<_>>();
            for line in block.split(|byte| *byte == b'\n') {
                if let Some(data) = line.strip_prefix(b"data: ") {
                    let frame: Value = serde_json::from_slice(data).map_err(|_| invalid("invalid remote event"))?;
                    frames.push(frame);
                }
            }
        }
        Ok(frames)
    }
}

async fn session_stream(session: SessionInfo, http: Client, base: Url,
    frames: async_mpsc::Sender<WorkerFrame>, mut cancel: watch::Receiver<bool>) {
    let mut cursor = None;
    loop {
        if *cancel.borrow() { return; }
        if cursor.is_none() {
            match snapshot(&http, &base, &session.id).await {
                Ok((markdown, pending_inputs, pending_inputs_complete, next)) => {
                    if frames.send(WorkerFrame::RemoteSnapshot { session_id: session.id.clone(), markdown, pending_inputs, pending_inputs_complete }).await.is_err() { return; }
                    cursor = Some(next);
                }
                Err(_) => {
                    let _ = frames.send(WorkerFrame::RemoteConnectivity { session_id: session.id.clone(), status: "Remote snapshot unavailable · retrying".into() }).await;
                    tokio::select! { _=cancel.changed()=>return, _=tokio::time::sleep(Duration::from_secs(3))=>{} }
                    continue;
                }
            }
        }
        let path = format!("api/sessions/{}/events?cursor={}", session.id, cursor.unwrap());
        let url = match base.join(&path) { Ok(url) => url, Err(_) => return };
        let response = tokio::select! {
            _=cancel.changed()=>return,
            response=http.get(url).send()=>response,
        };
        if let Ok(response) = response {
            if response.status().is_success() {
                let _ = frames.send(WorkerFrame::RemoteConnectivity { session_id: session.id.clone(), status: "Remote connected".into() }).await;
                let mut stream = response.bytes_stream();
                let mut decoder = SseDecoder::default();
                loop {
                    let next = tokio::select! {
                        _=cancel.changed()=>return,
                        next=tokio::time::timeout(Duration::from_secs(45), stream.next())=>next,
                    };
                    let Some(Ok(chunk)) = next.ok().flatten() else { break };
                    let decoded = match decoder.push(&chunk) { Ok(frames) => frames, Err(_) => { cursor=None; break } };
                    for frame in decoded {
                        if frame["event"]["type"] == "replay_gap" { cursor=None; break; }
                        if frame["type"] != "event" { continue; }
                        let Some(seq) = frame["seq"].as_u64() else { cursor=None; break };
                        if seq < cursor.unwrap_or(0) { continue; }
                        if seq > cursor.unwrap_or(0) { cursor=None; break; }
                        let mut frame = frame;
                        frame["session_id"] = json!(session.id);
                        if frames.send(WorkerFrame::Daemon { session_id: session.id.clone(), frame }).await.is_err() { return; }
                        cursor = seq.checked_add(1);
                    }
                    if cursor.is_none() { break; }
                }
            }
        }
        let _ = frames.send(WorkerFrame::RemoteConnectivity { session_id: session.id.clone(), status: "Remote reconnecting".into() }).await;
        tokio::select! { _=cancel.changed()=>return, _=tokio::time::sleep(Duration::from_secs(2))=>{} }
    }
}

async fn command_worker(command: WorkerCommand, http: Client, base: Url,
    frames: async_mpsc::Sender<WorkerFrame>, uncertain: Arc<Mutex<RetryIds>>,
    available: Arc<Mutex<HashSet<String>>>) {
    match command {
        WorkerCommand::Prompt(id, text) if valid_target(&id) && available.lock().unwrap_or_else(|poison| poison.into_inner()).contains(&id) => {
            let key = retry_key("prompt", &id, &text);
            let Some(request_id) = retry_id(&uncertain, &key) else {
                let _ = frames.send(WorkerFrame::PromptFailed { session_id:id, text,
                    message:"Too many uncertain remote commands; inspect the sessions before retrying".into(),
                    delivery:PromptDelivery::Rejected }).await;
                return;
            };
            match command_result(&http, &base, &id, "prompt", json!({"text":text,"request_id":request_id})).await {
                Ok(_) => { clear_retry(&uncertain, &key); }
                Err(error) => {
                    let rejected = error.kind() == io::ErrorKind::PermissionDenied || error.kind() == io::ErrorKind::InvalidInput;
                    if rejected { clear_retry(&uncertain, &key); }
                    let _ = frames.send(WorkerFrame::PromptFailed { session_id:id, text,
                        message:error.to_string(), delivery:if rejected { PromptDelivery::Rejected } else { PromptDelivery::Uncertain } }).await;
                }
            }
        }
        WorkerCommand::Answer(id, request, answer) if valid_target(&id) && available.lock().unwrap_or_else(|poison| poison.into_inner()).contains(&id) => {
            let key = retry_key("answer", &id, &format!("{request}:{answer}"));
            let Some(request_id) = retry_id(&uncertain, &key) else {
                let _ = frames.send(WorkerFrame::Command { session_id:id,
                    result:CommandResult::Answer { request_id:request, ok:false,
                        uncertain:Some(true), message:"Too many uncertain remote commands".into() } }).await;
                return;
            };
            let result = command_result(&http, &base, &id, "answer",
                json!({"id":request,"answer":answer,"request_id":request_id})).await;
            let uncertain_reply = result.as_ref().err().is_some_and(|error| error.kind() != io::ErrorKind::PermissionDenied);
            if !uncertain_reply { clear_retry(&uncertain, &key); }
            let _ = frames.send(WorkerFrame::Command { session_id:id,
                result:CommandResult::Answer { request_id:request, ok:result.is_ok(),
                    uncertain:Some(uncertain_reply), message:result.err().map(|error|error.to_string()).unwrap_or_default() } }).await;
        }
        WorkerCommand::Attach(id, group) => {
            let known = available.lock().unwrap_or_else(|poison| poison.into_inner()).contains(&id);
            let _ = frames.send(WorkerFrame::Attach { session_id:id, group,
                result:if known { Ok(()) } else { Err("Remote session is no longer live".into()) } }).await;
        }
        other => {
            let _ = frames.send(bridge::rejection_frame(other, "Remote hub supports prompts, pending answers and transcript only")).await;
        }
    }
}

pub fn run(raw_url: &str) -> io::Result<()> {
    let base = hub_url(raw_url)?;
    let http = http_client()?;
    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build()?;
    let initial = runtime.block_on(async { sessions(&get(&http, &base, "api/sessions").await?) })?;
    if initial.is_empty() { return Err(io::Error::new(io::ErrorKind::NotFound, "no live remote sessions")); }
    drop(runtime);
    let (frame_tx, frames): (SyncSender<WorkerFrame>, Receiver<WorkerFrame>) = mpsc::sync_channel(128);
    let (commands, command_rx) = mpsc::sync_channel(32);
    let (async_commands, mut async_command_rx) = async_mpsc::channel(32);
    let (async_frames, mut async_frame_rx) = async_mpsc::channel(128);
    let (cancel_tx, cancel_rx) = watch::channel(false);
    let forward = std::thread::spawn(move || {
        while let Some(frame) = async_frame_rx.blocking_recv() {
            if frame_tx.send(frame).is_err() { break; }
        }
    });
    let router = std::thread::spawn(move || {
        while let Ok(command) = command_rx.recv() {
            if async_commands.blocking_send(command).is_err() { break; }
        }
    });
    let engine = std::thread::spawn(move || {
        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build();
        let Ok(runtime) = runtime else { return };
        runtime.block_on(async move {
            let uncertain = Arc::new(Mutex::new(HashMap::new()));
            let available = Arc::new(Mutex::new(HashSet::new()));
            let command_slots = Arc::new(Semaphore::new(8));
            let mut tasks: HashMap<String, tokio::task::JoinHandle<()>> = HashMap::new();
            let mut current = initial;
            let mut cancel = cancel_rx;
            loop {
                let present: HashSet<_> = current.iter().map(|session| session.id.as_str()).collect();
                *available.lock().unwrap_or_else(|poison| poison.into_inner()) = present.iter().map(|id| (*id).to_owned()).collect();
                for (id, task) in tasks.iter() {
                    if !present.contains(id.as_str()) {
                        task.abort();
                        let _ = async_frames.send(WorkerFrame::RemoteConnectivity {
                            session_id:id.clone(), status:"Remote session offline".into() }).await;
                    }
                }
                tasks.retain(|id, _| present.contains(id.as_str()));
                for session in &current {
                    if tasks.contains_key(&session.id) { continue; }
                    if async_frames.send(hello(session)).await.is_err() { return; }
                    tasks.insert(session.id.clone(), tokio::spawn(session_stream(session.clone(), http.clone(), base.clone(),
                        async_frames.clone(), cancel.clone())));
                }
                tokio::select! {
                    _ = cancel.changed() => break,
                    Some(command) = async_command_rx.recv() => {
                        if let Ok(permit) = command_slots.clone().try_acquire_owned() {
                            let worker = command_worker(command, http.clone(), base.clone(), async_frames.clone(), uncertain.clone(), available.clone());
                            tokio::spawn(async move { let _permit = permit; worker.await; });
                        } else {
                            let _ = async_frames.send(bridge::rejection_frame(command, "Remote command limit reached")).await;
                        }
                    }
                    _ = tokio::time::sleep(Duration::from_secs(15)) => {
                        if let Ok(value) = get(&http, &base, "api/sessions").await {
                            if let Ok(next) = sessions(&value) { current = next; }
                        }
                    }
                }
            }
        });
    });
    let result = crate::ui::run_remote_with_worker_channels(frames, commands.clone());
    drop(commands);
    let _ = cancel_tx.send(true);
    let _ = router.join();
    let _ = engine.join();
    let _ = forward.join();
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use hyper::body::Bytes;
    use http_body_util::Full;
    use hyper::{Request, Response};
    use hyper::service::service_fn;
    use hyper_util::rt::TokioIo;
    use std::convert::Infallible;
    use std::sync::atomic::{AtomicUsize, Ordering};
    #[test]
    fn private_origin_and_inventory_are_bounded() {
        assert!(hub_url("https://owner.tail.ts.net/").is_ok());
        for url in ["http://owner.tail.ts.net/", "https://evil.example.com/", "https://user@owner.tail.ts.net/", "https://owner.tail.ts.net/path"] {
            assert!(hub_url(url).is_err(), "{url}");
        }
        assert!(sessions(&json!({"sessions":[{"id":"host~session","title":"Work","engine":"codex","model":"gpt"}]})).is_ok());
        assert!(sessions(&json!({"sessions":[{"id":"host~session"},{"id":"host~session"}]})).is_err());
        let saved = Mutex::new(HashMap::new());
        let key = retry_key("prompt", "host~session", "private prompt");
        assert!(!key.contains("private prompt"));
        assert_eq!(retry_id(&saved, &key), retry_id(&saved, &key));
        clear_retry(&saved, &key);
        assert!(saved.lock().unwrap().is_empty());
    }
    #[test]
    fn transcript_and_sse_preserve_roles_and_bound_frames() {
        let markdown = snapshot_markdown(&json!({"turns":[{"prompt":"Hello","text":"Hi","tools":[{"name":"read","result":"done"}]}]})).unwrap();
        assert!(markdown.contains("**You:**\n\nHello"));
        assert!(markdown.contains("**Assistant:**\n\nHi"));
        let mut decoder = SseDecoder::default();
        assert!(decoder.push(b"id: 1\ndata: {\"type\":\"event\",\"seq\":1").unwrap().is_empty());
        assert_eq!(decoder.push(b"}\n\n").unwrap()[0]["seq"], 1);
        assert!(decoder.push(&vec![b'x'; MAX_SSE + 1]).is_err());
    }

    #[tokio::test]
    async fn hub_snapshot_and_sse_feed_the_native_frame_path() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let check = SseDecoder::default().push(concat!(
            "data: {\"type\":\"hello\"}\n\n",
            "id: 7\ndata: {\"type\":\"event\",\"seq\":7,\"event\":{\"type\":\"text_delta\",\"data\":{\"text\":\"live\"}}}\n\n"
        ).as_bytes()).unwrap();
        assert_eq!(check.len(), 2);
        assert_eq!(check[1]["seq"], 7);
        let base = Url::parse(&format!("http://{}/", listener.local_addr().unwrap())).unwrap();
        let sends = Arc::new(AtomicUsize::new(0));
        let server_sends = sends.clone();
        let server = tokio::spawn(async move {
            loop {
                let Ok((socket, _)) = listener.accept().await else { break };
                let sends = server_sends.clone();
                tokio::spawn(async move {
                    let service = service_fn(move |request: Request<hyper::body::Incoming>| {
                        let sends = sends.clone();
                        async move {
                        let path = request.uri().path();
                        let body = match path {
                            "/api/sessions/host~session/transcript" => json!({"command_id":"command-1"}).to_string(),
                            "/api/sessions/host~session/prompt" => {
                                sends.fetch_add(1, Ordering::SeqCst);
                                json!({"command_id":"command-2"}).to_string()
                            },
                            "/api/sessions/host~session/answer" => json!({"command_id":"command-3"}).to_string(),
                            "/api/commands/command-1" => json!({"status":"accepted","result":{
                                "ok":true,"turns":[{"prompt":"hello","text":"answer","tools":[]}],
                                "pending_inputs":[{"id":"ask-1","kind":"permission"}],
                                "pending_inputs_complete":true,"next_seq":7}}).to_string(),
                            "/api/commands/command-2" | "/api/commands/command-3" =>
                                json!({"status":"accepted","result":{"ok":true}}).to_string(),
                            "/api/sessions/host~session/events" => concat!(
                                "data: {\"type\":\"hello\"}\n\n",
                                "id: 7\ndata: {\"type\":\"event\",\"seq\":7,\"event\":{\"type\":\"text_delta\",\"data\":{\"text\":\"live\"}}}\n\n"
                            ).to_owned(),
                            _ => "{}".into(),
                        };
                        Ok::<_, Infallible>(Response::new(Full::new(Bytes::from(body))))
                    }});
                    let _ = hyper::server::conn::http1::Builder::new()
                        .serve_connection(TokioIo::new(socket), service).await;
                });
            }
        });
        let (frames, mut received) = async_mpsc::channel(8);
        let (cancel, stop) = watch::channel(false);
        let client = http_client().unwrap();
        let task = tokio::spawn(session_stream(SessionInfo {
            id:"host~session".into(),title:"Work".into(),host:"host".into(),
            engine:"codex".into(),model:"gpt".into(),
        }, client.clone(), base.clone(), frames, stop));
        let first = tokio::time::timeout(Duration::from_secs(5), received.recv()).await.unwrap().unwrap();
        assert!(matches!(first, WorkerFrame::RemoteSnapshot { markdown, pending_inputs, .. }
            if markdown.contains("answer") && pending_inputs[0]["id"] == "ask-1"));
        let second = tokio::time::timeout(Duration::from_secs(5), received.recv()).await.unwrap().unwrap();
        assert!(matches!(second, WorkerFrame::RemoteConnectivity { status, .. } if status == "Remote connected"));
        let third = tokio::time::timeout(Duration::from_secs(5), received.recv()).await.unwrap().unwrap();
        assert!(matches!(third, WorkerFrame::Daemon { ref frame, .. }
            if frame["seq"] == 7 && frame["session_id"] == "host~session"), "{third:?}");
        cancel.send(true).unwrap();
        tokio::time::timeout(Duration::from_secs(5), task).await.unwrap().unwrap();
        let available = Arc::new(Mutex::new(HashSet::from(["host~session".to_owned()])));
        let retries = Arc::new(Mutex::new(HashMap::new()));
        let (replies, mut reply_rx) = async_mpsc::channel(4);
        command_worker(WorkerCommand::Prompt("host~session".into(), "next turn".into()),
            client.clone(), base.clone(), replies.clone(), retries.clone(), available.clone()).await;
        assert_eq!(sends.load(Ordering::SeqCst), 1);
        command_worker(WorkerCommand::Answer("host~session".into(), "ask-1".into(), json!({"decision":"allow"})),
            client, base, replies, retries, available).await;
        assert!(matches!(reply_rx.recv().await, Some(WorkerFrame::Command {
            result:CommandResult::Answer { ok:true, .. }, ..
        })));
        server.abort();
    }
}
