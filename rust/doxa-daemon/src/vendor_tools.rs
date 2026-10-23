//! Explicitly enabled, read-only workspace tool for native vendor engines.
//! Path resolution stays below an opened workspace directory and rejects symlinks.
use doxa_vendors::{ToolCall, ToolGate};
use futures_util::future::BoxFuture;
use serde_json::{json, Value};
use std::ffi::CString;
use std::fs::{File, OpenOptions};
use std::io::Read;
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Component, Path};

const MAX_READ_BYTES: u64 = 64 * 1024;

pub struct WorkspaceReadGate<'a> {
    root: &'a Path,
    scrub: &'a dyn Fn(&str) -> Result<String, ()>,
}

#[derive(Debug)]
enum ReadFailure { Refused, Integrity }

impl<'a> WorkspaceReadGate<'a> {
    pub fn new(root: &'a Path, scrub: &'a dyn Fn(&str) -> Result<String, ()>) -> Self {
        Self { root, scrub }
    }

    fn read(&self, call: &ToolCall) -> Result<Value, ReadFailure> {
        if call.name != "workspace_read" || call.arguments.len() != 1 {
            return Err(ReadFailure::Refused);
        }
        let path = call.arguments.get("path").and_then(Value::as_str).ok_or(ReadFailure::Refused)?;
        let parts: Vec<_> = Path::new(path).components().collect();
        if parts.is_empty() || parts.len() > 16 || path.len() > 1024 || parts.iter().any(|part| {
            !matches!(part, Component::Normal(name) if !name.to_string_lossy().starts_with('.'))
        }) {
            return Err(ReadFailure::Refused);
        }
        let mut file = OpenOptions::new().read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(self.root).map_err(|_| ReadFailure::Refused)?;
        for (index, part) in parts.iter().enumerate() {
            let Component::Normal(name) = part else { return Err(ReadFailure::Refused); };
            if index + 1 == parts.len() && doxa_vendors::credentials::is_credential_path(&file, name).map_err(|_| ReadFailure::Integrity)? {
                return Err(ReadFailure::Refused);
            }
            use std::os::unix::ffi::OsStrExt;
            let name = CString::new(name.as_bytes()).map_err(|_| ReadFailure::Refused)?;
            let last = index + 1 == parts.len();
            let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC
                | if last { libc::O_NONBLOCK } else { libc::O_DIRECTORY };
            // SAFETY: openat receives a live directory fd and a NUL-terminated name.
            let fd = unsafe { libc::openat(file.as_raw_fd(), name.as_ptr(), flags) };
            if fd < 0 {
                // A new project often has files the worker has not created yet.
                // Let the model handle absence without aborting a paid tool
                // turn and losing its otherwise verified response usage.
                // Path, credential, symlink and scrub failures remain closed.
                if std::io::Error::last_os_error().kind() == std::io::ErrorKind::NotFound {
                    return Ok(json!({"is_error":true,"error":"file_not_found"}));
                }
                return Err(ReadFailure::Refused);
            }
            // SAFETY: a successful openat returns a newly owned fd.
            file = unsafe { File::from_raw_fd(fd) };
        }
        let metadata = file.metadata().map_err(|_| ReadFailure::Refused)?;
        if !metadata.is_file() || metadata.len() > MAX_READ_BYTES { return Err(ReadFailure::Refused); }
        if doxa_vendors::credentials::is_credential_file(&file).map_err(|_| ReadFailure::Integrity)? { return Err(ReadFailure::Refused); }
        let mut bytes = Vec::new();
        file.take(MAX_READ_BYTES + 1).read_to_end(&mut bytes).map_err(|_| ReadFailure::Refused)?;
        if bytes.len() as u64 > MAX_READ_BYTES { return Err(ReadFailure::Refused); }
        let content = String::from_utf8(bytes).map_err(|_| ReadFailure::Refused)?;
        Ok(json!({"path":path,"content":(self.scrub)(&content).map_err(|_| ReadFailure::Integrity)?}))
    }
    fn execute_read(&self, call: &ToolCall) -> Result<Value, ()> {
        match self.read(call) {
            Ok(result) => Ok(result),
            Err(ReadFailure::Refused) => Ok(json!({"is_error":true,"error":"workspace_read_refused"})),
            Err(ReadFailure::Integrity) => Err(()),
        }
    }

}

impl ToolGate for WorkspaceReadGate<'_> {
    fn definitions(&self) -> Vec<Value> {
        vec![json!({"type":"function","function":{
            "name":"workspace_read",
            "description":"Read one UTF-8 regular file below the session workspace. Hidden paths and symlinks are unavailable; maximum 64 KiB. The file content is sent to the model provider.",
            "parameters":{"type":"object","properties":{"path":{"type":"string","description":"Relative path below the workspace"}},"required":["path"],"additionalProperties":false}
        }})]
    }

    fn execute<'a>(&'a mut self, call: &'a ToolCall) -> BoxFuture<'a, Result<Value, ()>> {
        let result = self.execute_read(call);
        Box::pin(async move { result })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Map;
    use std::fs;

    fn call(path: &str) -> ToolCall {
        let mut arguments = Map::new();
        arguments.insert("path".into(), json!(path));
        ToolCall { id: "1".into(), name: "workspace_read".into(), arguments }
    }

    #[test]
    fn credential_file_never_reaches_workspace_scrubber_or_result() {
        let root = tempfile::tempdir().unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(root.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        let previous = std::env::var_os("DOXA_HOME");
        struct Restore(Option<std::ffi::OsString>);
        impl Drop for Restore { fn drop(&mut self) {
            if let Some(value) = self.0.take() { std::env::set_var("DOXA_HOME", value); }
            else { std::env::remove_var("DOXA_HOME"); }
        } }
        let _restore = Restore(previous);
        std::env::set_var("DOXA_HOME", root.path());
        doxa_vendors::credentials::save(doxa_vendors::Vendor::DeepSeek, "synthetic-private-key").unwrap();
        let calls = std::cell::Cell::new(0);
        let scrub = |text: &str| { calls.set(calls.get() + 1); Ok(text.to_owned()) };
        let gate = WorkspaceReadGate::new(root.path(), &scrub);
        assert!(gate.read(&call("credentials.json")).is_err());
        assert_eq!(calls.get(), 0);
    }
    #[test]
    fn reads_bounded_file_and_rejects_escape_and_hidden_paths() {
        let root = tempfile::tempdir().unwrap();
        fs::create_dir(root.path().join("src")).unwrap();
        fs::write(root.path().join("src/main.rs"), "secret text").unwrap();
        fs::write(root.path().join(".env"), "key").unwrap();
        std::os::unix::fs::symlink("/etc/passwd", root.path().join("link")).unwrap();
        let scrub = |text: &str| Ok(text.replace("secret", "***"));
        let gate = WorkspaceReadGate::new(root.path(), &scrub);
        assert_eq!(gate.read(&call("src/main.rs")).unwrap()["content"], "*** text");
        for path in ["../etc/passwd", "/etc/passwd", ".env", "src/../.env", "link", "src"] {
            assert!(gate.read(&call(path)).is_err(), "{path}");
        }
        for path in ["missing", "new-directory/missing"] {
            assert_eq!(gate.read(&call(path)).unwrap(), json!({"is_error":true,"error":"file_not_found"}));
        }
        let failed_scrub = |_: &str| Err(());
        assert!(WorkspaceReadGate::new(root.path(), &failed_scrub).read(&call("src/main.rs")).is_err());
        fs::write(root.path().join("large.txt"), vec![b'a'; MAX_READ_BYTES as usize + 1]).unwrap();
        assert!(gate.read(&call("large.txt")).is_err());
    }
}

/// One-shot permission desk for provider-initiated peer tools. The native
/// runtime publishes the complete request and applies the reviewed answer.
#[derive(Default)]
pub struct PeerDesk {
    pending: std::sync::Mutex<Option<PeerPending>>,
    next: std::sync::atomic::AtomicU64,
}
struct PeerPending { id: String, sender: tokio::sync::oneshot::Sender<bool> }
impl PeerDesk {
    fn begin(&self, tool: &str, arguments: &Value) -> Result<(Value, tokio::sync::oneshot::Receiver<bool>), ()> {
        let summary = arguments.to_string();
        if summary.len() > 32 * 1024 { return Err(()); }
        let mut pending = self.pending.lock().map_err(|_| ())?;
        if pending.as_ref().is_some_and(|ask| !ask.sender.is_closed()) { return Err(()); }
        let generation = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_err(|_| ())?.as_nanos();
        let id = format!("vendor-peer-{}-{generation}-{}", std::process::id(), self.next.fetch_add(1, std::sync::atomic::Ordering::Relaxed));
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let request = json!({"id":id,"kind":"permission","title":if tool.starts_with("lore_") { "Approve this LORE tool once?" } else if tool.starts_with("mcp_ext_") { "Approve this external MCP tool once?" } else { "Approve this peer tool once?" },
            "tool_name":tool,"input_summary":summary,"require_full_review":true});
        *pending = Some(PeerPending { id, sender });
        Ok((request, receiver))
    }
    pub fn answer(&self, id: &str, answer: &Value) -> Result<Value, String> {
        let allowed = match answer["decision"].as_str() { Some("allow") => true, Some("deny") => false, _ => return Err("Peer permission needs allow or deny".into()) };
        let mut pending = self.pending.lock().map_err(|_| "Peer permission desk unavailable")?;
        if !pending.as_ref().is_some_and(|ask| ask.id == id && !ask.sender.is_closed()) { return Err("Peer permission is no longer pending".into()); }
        pending.take().unwrap().sender.send(allowed).map_err(|_| "Peer permission is no longer pending")?;
        Ok(json!({"applied":true}))
    }
    pub fn clear(&self) { if let Ok(mut pending) = self.pending.lock() { pending.take(); } }
}

struct ResolvePeer {
    desk: std::sync::Arc<PeerDesk>, id: Value,
    events: std::sync::Arc<std::sync::Mutex<Vec<Value>>>,
}
impl Drop for ResolvePeer {
    fn drop(&mut self) {
        self.desk.clear();
        if let Ok(mut events) = self.events.lock() {
            if events.len() < 128 { events.push(json!({"type":"needs_input_resolved","data":{"id":self.id}})); }
        }
    }
}

/// Workspace reading retains its explicit enable switch. Peer tools become
/// available only after the daemon supplies its same-scope scrubbed callback;
/// every provider call still requires a per-call permission answer.
pub struct NativeVendorGate<'a> {
    audit: std::sync::Arc<std::sync::Mutex<Value>>,
    workspace: Option<WorkspaceReadGate<'a>>,
    peer: Option<doxa_runtime::PeerToolHandler>,
    agent: Option<(Vec<Value>, doxa_runtime::PeerToolHandler)>,
    mcp: Option<(Vec<crate::vendor_mcp::Binding>, Vec<Value>, std::sync::Arc<crate::vendor_mcp::Service>, std::sync::Arc<std::sync::atomic::AtomicBool>)>,
    desk: std::sync::Arc<PeerDesk>,
    scrub: &'a (dyn Fn(&str) -> Result<String, ()> + Sync),
    emit: &'a dyn Fn(Value),
    events: std::sync::Arc<std::sync::Mutex<Vec<Value>>>,
}
impl<'a> NativeVendorGate<'a> {
    pub fn new(root: &'a Path, workspace: bool, peer: Option<doxa_runtime::PeerToolHandler>,
        desk: std::sync::Arc<PeerDesk>, scrub: &'a (dyn Fn(&str) -> Result<String, ()> + Sync),
        emit: &'a dyn Fn(Value), events: std::sync::Arc<std::sync::Mutex<Vec<Value>>>) -> Self {
        Self { audit:std::sync::Arc::new(std::sync::Mutex::new(json!({"version":1,"complete":true,"actions":[]}))), workspace:workspace.then(|| WorkspaceReadGate::new(root, scrub)), peer, agent:None, mcp:None, desk, scrub, emit, events }
    }
    pub fn with_agent(mut self, definitions: Vec<Value>, handler: doxa_runtime::PeerToolHandler) -> Self {
        self.agent = Some((definitions, handler)); self
    }
    pub(super) fn with_mcp(mut self, bindings: Vec<crate::vendor_mcp::Binding>, definitions: Vec<Value>,
        service: std::sync::Arc<crate::vendor_mcp::Service>, cancel: std::sync::Arc<std::sync::atomic::AtomicBool>) -> Self {
        self.mcp = Some((bindings, definitions, service, cancel)); self
    }
    fn execute_mcp<'b>(&'b mut self, call: &'b ToolCall, binding: crate::vendor_mcp::Binding,
        service: std::sync::Arc<crate::vendor_mcp::Service>, cancel: std::sync::Arc<std::sync::atomic::AtomicBool>) -> BoxFuture<'b, Result<Value, ()>> {
        let started = (|| {
            let call_id = (self.scrub)(&call.id)?;
            if call_id.len() > 256 { return Err(()); }
            let mut arguments = Value::Object(call.arguments.clone());
            crate::vendor_mcp::scrub_value(&mut arguments, self.scrub)?;
            let review = json!({"server_id":binding.server_id,"tool_name":binding.tool_name,"arguments":arguments,
                "boundary":"External MCP server on trusted host; tool arguments may leave this host; external charges are outside model allowance"});
            let (mut request, reply) = self.desk.begin(&call.name, &review)?;
            request["mcp_server_id"] = json!(binding.server_id);
            request["mcp_tool_name"] = json!(binding.tool_name);
            let guard = McpReviewGuard { resolve:ResolvePeer { desk:self.desk.clone(), id:request["id"].clone(), events:self.events.clone() },
                call_id:call_id.clone(), tool_name:call.name.clone(), server:binding.server_id.clone(), original_tool:binding.tool_name.clone(),
                submitted:false, completed:false, service:service.clone(), cancel:cancel.clone() };
            (self.emit)(json!({"type":"tool_call","data":{"id":call_id,"name":call.name,"input":arguments,
                "mcp_server_id":binding.server_id,"mcp_tool_name":binding.tool_name}}));
            (self.emit)(json!({"type":"needs_input","data":request}));
            Ok((arguments, reply, guard, call_id))
        })();
        let scrub = self.scrub; let events = self.events.clone(); let tool_name = call.name.clone();
        Box::pin(async move {
            let (arguments, reply, mut guard, call_id) = started?;
            let allowed = reply.await.map_err(|_| ())?;
            let completion_service=service.clone();
            let result = if allowed {
                guard.submitted = true;
                match tokio::task::spawn_blocking(move || service.call(binding, arguments, cancel)).await.map_err(|_| ())? {
                    Ok(value) => value,
                    Err(error) => error.result(),
                }
            } else { json!({"is_error":true,"error":"MCP permission denied; arguments withheld","attempted":false}) };
            let mut result = result;
            crate::vendor_mcp::scrub_value(&mut result, scrub)?;
            let text = result.to_string();
            if text.len() > crate::vendor_mcp::MAX_RESULT_BYTES { return Err(()); }
            if allowed {tokio::task::spawn_blocking(move||completion_service.complete()).await.map_err(|_|())?.map_err(|_|())?;}
            let failed = !allowed || result["isError"] == true || result["is_error"] == true || result["error"].is_string();
            if let Ok(mut events) = events.lock() {
                events.push(json!({"type":"tool_result","data":{"id":call_id,"name":tool_name,
                    "mcp_server_id":guard.server,"mcp_tool_name":guard.original_tool,"external_charge_usd":null,
                    "result_summary":text.chars().take(1000).collect::<String>(),"is_error":failed}}));
                let mut start = 0;
                while start < text.len() {
                    let mut end = (start + 8192).min(text.len()); while !text.is_char_boundary(end) { end -= 1; }
                    if end == start { break; }
                    events.push(json!({"type":"tool_result_detail","data":{"id":call_id,"text":&text[start..end]}})); start = end;
                }
            }
            guard.completed = true;
            Ok(result)
        })
    }
    pub(super) fn audit_handle(&self)->std::sync::Arc<std::sync::Mutex<Value>>{self.audit.clone()}
    fn scrub_arguments(&self, call: &ToolCall) -> Result<Value, ()> {
        let mut arguments=Value::Object(call.arguments.clone());
        crate::vendor_mcp::scrub_value(&mut arguments,self.scrub)?;
        Ok(arguments)
    }
    fn execute_inner<'b>(&'b mut self, call: &'b ToolCall) -> BoxFuture<'b, Result<Value, ()>> {
        if let Some((bindings, _, service, cancel)) = &self.mcp {
            if let Some(binding) = bindings.iter().find(|binding| binding.provider_name == call.name) {
                let (binding, service, cancel) = (binding.clone(), service.clone(), cancel.clone());
                return self.execute_mcp(call, binding, service, cancel);
            }
        }
        if call.name == "workspace_read" {
            let checked=(|| {
                let id=(self.scrub)(&call.id)?;
                let mut input=Value::Object(call.arguments.clone());
                crate::vendor_mcp::scrub_value(&mut input,self.scrub)?;
                Ok::<_,()>((id,input))
            })();
            let (id,input)=match checked {Ok(value)=>value,Err(())=>return Box::pin(async{Err(())})};
            (self.emit)(json!({"type":"tool_call","data":{"id":id,"name":call.name,"input":input}}));
            let result = self.workspace.as_ref().ok_or(()).and_then(|gate| gate.execute_read(call));
            let is_error=result.as_ref().map_or(true,|value|value["is_error"]==true);
            let summary=match &result {
                Ok(value) if !is_error=>format!("Workspace file read ({} UTF-8 bytes)",value["content"].as_str().map(str::len).unwrap_or(0)),
                Ok(value)=>value["error"].as_str().unwrap_or("workspace_read_refused").to_owned(),
                Err(())=>"Workspace read integrity check failed".into(),
            };
            (self.emit)(json!({"type":"tool_result","data":{"id":id,"name":call.name,"is_error":is_error,"result_summary":summary}}));
            return Box::pin(async move { result });
        }
        // Malformed model arguments have no uncertain backend outcome. Show
        // the scrubbed refusal and let the bounded provider turn correct them;
        // no permission or peer handler is reached on this path.
        if matches!(call.name.as_str(),doxa_engines::peer_tools::LIST|doxa_engines::peer_tools::SEND|doxa_engines::peer_tools::HISTORY) {
            let checked=(|| {
                let id=(self.scrub)(&call.id)?;
                let arguments=self.scrub_arguments(call)?;
                Ok::<_,()>((id,arguments))
            })();
            let (id,arguments)=match checked{Ok(value)=>value,Err(())=>return Box::pin(async{Err(())})};
            if doxa_engines::peer_tools::validated_call(&call.name,&arguments).is_err(){
                let result=json!({"error":"Invalid peer tool arguments; no action was taken. Correct the tool arguments using the advertised schema."});
                (self.emit)(json!({"type":"tool_call","data":{"id":id,"name":call.name,"input":arguments}}));
                (self.emit)(json!({"type":"tool_result","data":{"id":id,"name":call.name,"result_summary":result.to_string(),"is_error":true,"failure_category":"peer_arguments_invalid"}}));
                return Box::pin(async move{Ok(result)});
            }
        }
        let start = (|| {
            let call_id = (self.scrub)(&call.id)?;
            let arguments=self.scrub_arguments(call)?;
            let (peer, method, dispatch_arguments) = if let Some((_, handler)) = self.agent.as_ref().filter(|(rows, _)|
                rows.iter().any(|row| row["function"]["name"] == call.name)) {
                (handler.clone(), call.name.clone(), arguments.clone())
            } else {
                let (method, normalized)=doxa_engines::peer_tools::validated_call(&call.name, &arguments).map_err(|_| ())?;
                (self.peer.clone().ok_or(())?, method.to_owned(), normalized)
            };
            let (request, reply) = self.desk.begin(&call.name, &arguments)?;
            let guard = ResolvePeer { desk:self.desk.clone(), id:request["id"].clone(), events:self.events.clone() };
            (self.emit)(json!({"type":"tool_call","data":{"id":call_id,"name":call.name,"input":arguments}}));
            (self.emit)(json!({"type":"needs_input","data":request}));
            Ok((peer, method, dispatch_arguments, reply, guard, call_id))
        })();
        let scrub = self.scrub;
        let events = self.events.clone();
        let tool_name = call.name.clone();
        Box::pin(async move {
            let (peer, method, arguments, reply, _guard, call_id) = start?;
            let allowed = reply.await.map_err(|_| ())?;
            // A peer refusal is an ordinary operator result (unknown target,
            // rate limit, unavailable ledger), so the model can recover. It
            // is not a canonical backend strike and must not end the turn.
            // Canonical LORE bridge failures retain their uncertain-outcome
            // boundary rather than inviting an automatic write retry.
            let refusal_method=method.clone();
            let result = if allowed { match tokio::task::spawn_blocking(move||peer(&method,&arguments)).await.map_err(|_|())? {
                Ok(result) => result,
                Err(reason) if matches!(refusal_method.as_str(), "peers" | "msg" | "peer_history"|"spawn_session") =>
                    json!({"error":format!("{}: {reason}",tool_name.strip_prefix("mcp__doxa__").unwrap_or(&tool_name))}),
                Err(_) => return Err(()),
            } }
                else { json!({"error":"Tool permission was denied; no action was taken"}) };
            let text = scrub(&result.to_string())?;
            if let Ok(mut events) = events.lock() {
                events.push(json!({"type":"tool_result","data":{"id":call_id,"name":tool_name,
                    "result_summary":text.chars().take(1000).collect::<String>(),"is_error":!allowed || result["error"].is_string()}}));
                let mut start = 0; let limit = text.len().min(64 * 1024);
                while start < limit {
                    let mut end = (start + 8192).min(limit); while !text.is_char_boundary(end) { end -= 1; }
                    if end == start { break; }
                    events.push(json!({"type":"tool_result_detail","data":{"id":call_id,"text":&text[start..end]}})); start = end;
                }
                if limit < text.len() { events.push(json!({"type":"tool_result_detail","data":{"id":call_id,"text":"\n[Tool detail display limit reached]"}})); }
            }
            serde_json::from_str(&text).map_err(|_| ())
        })
    }

}
struct McpReviewGuard {
    resolve: ResolvePeer, call_id: String, tool_name: String, server: String, original_tool: String,
    submitted: bool, completed: bool,
    service: std::sync::Arc<crate::vendor_mcp::Service>, cancel: std::sync::Arc<std::sync::atomic::AtomicBool>,
}
impl Drop for McpReviewGuard {
    fn drop(&mut self) {
        if self.completed { return; }
        if self.submitted {
            self.cancel.store(true, std::sync::atomic::Ordering::Release);
            self.service.abandon();
        }
        if let Ok(mut events) = self.resolve.events.lock() {
            events.push(json!({"type":"tool_result","data":{"id":self.call_id,"name":self.tool_name,
                "mcp_server_id":self.server,"mcp_tool_name":self.original_tool,"is_error":true,
                "result_summary":if self.submitted {"MCP call interrupted or unsafe; outcome unknown; automatic replay withheld"}
                    else {"MCP review cancelled; arguments withheld"},"external_charge_usd":null}}));
        }
        // ResolvePeer drops after this receipt, clearing the one-shot desk and
        // publishing resolution before another approval/provider continuation.
    }
}
impl ToolGate for NativeVendorGate<'_> {
    fn flush_events(&mut self) {
        // The async execution future queues events because the host emitter
        // is turn-local. Release the queue lock before invoking that emitter.
        let events = self.events.lock().map(|mut events| std::mem::take(&mut *events)).unwrap_or_default();
        for event in events { (self.emit)(event); }
    }
    fn definitions(&self) -> Vec<Value> {
        let mut definitions = self.workspace.as_ref().map(ToolGate::definitions).unwrap_or_default();
        if self.peer.is_some() {
            definitions.extend(doxa_engines::peer_tools::definitions().into_iter().map(|definition| {
                json!({"type":"function","function":{"name":definition["name"],"description":definition["description"],"parameters":definition["inputSchema"]}})
            }));
        }
        if let Some((rows, _)) = &self.agent { definitions.extend(rows.clone()); }
        if let Some((_, rows, _, _)) = &self.mcp { definitions.extend(rows.clone()); }
        definitions
    }
    fn execute<'a>(&'a mut self, call: &'a ToolCall) -> BoxFuture<'a, Result<Value, ()>> {
        // Record the actual native execution boundary, including workspace and
        // external tools that do not otherwise emit a peer-card event. No model
        // prose or provider-supplied receipt is used as action evidence.
        let audit=self.audit.clone();
        let operation=match call.name.as_str(){
            doxa_engines::peer_tools::SEND=>"send",
            doxa_engines::peer_tools::LIST=>"list",
            doxa_engines::peer_tools::HISTORY=>"history",_=>"other"};
        let selected=if operation=="send" {
            self.scrub_arguments(call).ok()
                .filter(|args|doxa_engines::peer_tools::validated_call(&call.name,args).is_ok())
                .map(|args|json!({"target":args["target"],"fleet_kind":args["fleet_kind"],"artifact_refs":args["artifact_refs"],"in_reply_to":args["in_reply_to"]}))
        }else{None};
        let index=if let Ok(mut audit)=audit.lock(){
            let index=audit["actions"].as_array().map(Vec::len).unwrap_or(25);
            if index>=25 {audit["complete"]=json!(false);None}else{
                audit["actions"].as_array_mut().unwrap().push(json!({"operation":operation,"request":selected,"completed":false}));
                if serde_json::to_vec(&*audit).map_or(true,|bytes|bytes.len()>8192){
                    audit["actions"].as_array_mut().unwrap().pop();audit["complete"]=json!(false);None
                }else{Some(index)}
            }
        }else{None};
        let execution=self.execute_inner(call);
        Box::pin(async move {
            let result=execution.await;
            if let Some(index)=index {if let Ok(mut audit)=audit.lock(){let row=&mut audit["actions"][index];
                row["completed"]=json!(true);
                row["success"]=json!(result.as_ref().is_ok_and(|result|!result["error"].is_string()&&result["is_error"]!=true&&result["isError"]!=true));
                if operation=="send" {if let Ok(result)=&result {
                    let receipt=json!({"message_id":result["message_id"],"delivered_to":result["delivered_to"],"failed":result["failed"]});
                    if serde_json::to_vec(&receipt).map_or(true,|bytes|bytes.len()>2048){
                        row["success"]=json!(false);audit["complete"]=json!(false);
                    }else{for key in ["message_id","delivered_to","failed"]{row[key]=receipt[key].clone();}}
                }}
                if serde_json::to_vec(&*audit).map_or(true,|bytes|bytes.len()>8192){
                    audit["actions"][index]=json!({"operation":operation,"completed":true,"success":false});audit["complete"]=json!(false);
                }
            }}
            result
        })
    }
}

#[cfg(test)]
mod peer_tests {
    use super::*;
    use std::{cell::RefCell, sync::{Arc, Mutex, atomic::{AtomicUsize, Ordering}}};
    fn peer_call(name: &str, arguments: Value) -> ToolCall {
        ToolCall { id:"provider-call".into(), name:name.into(), arguments:arguments.as_object().unwrap().clone() }
    }
    #[test]
    fn workspace_refusals_are_visible_but_failed_scrubbing_still_aborts(){
        let dir=tempfile::tempdir().unwrap();std::fs::write(dir.path().join("visible"),"unpublishable-content").unwrap();
        let displayed=RefCell::new(Vec::new());let emit=|event|displayed.borrow_mut().push(event);
        let scrub=|text:&str|if text.contains("unpublishable-content"){Err(())}else{Ok(text.to_owned())};
        let mut gate=NativeVendorGate::new(dir.path(),true,None,Arc::new(PeerDesk::default()),&scrub,&emit,Arc::new(Mutex::new(Vec::new())));
        let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let refused=peer_call("workspace_read",json!({"path":"."}));
        assert_eq!(runtime.block_on(gate.execute(&refused)).unwrap(),json!({"is_error":true,"error":"workspace_read_refused"}));
        assert_eq!(displayed.borrow()[0]["type"],"tool_call");assert_eq!(displayed.borrow()[0]["data"]["input"]["path"],".");
        assert_eq!(displayed.borrow()[1]["type"],"tool_result");assert_eq!(displayed.borrow()[1]["data"]["is_error"],true);
        let unsafe_result=peer_call("workspace_read",json!({"path":"visible"}));
        assert!(runtime.block_on(gate.execute(&unsafe_result)).is_err());
        assert!(!serde_json::to_string(&*displayed.borrow()).unwrap().contains("unpublishable-content"));
        let audit=gate.audit_handle();assert!(audit.lock().unwrap()["actions"].as_array().unwrap().iter().all(|row|row["completed"]==true&&row["success"]==false));
    }
    #[test]
    fn native_action_audit_overflow_and_poison_refuse_evidence_without_panicking(){
        let dir=tempfile::tempdir().unwrap();let desk=Arc::new(PeerDesk::default());let scrub=|text:&str|Ok(text.to_owned());let emit=|_:Value|{};
        let mut gate=NativeVendorGate::new(dir.path(),true,None,desk,&scrub,&emit,Arc::new(Mutex::new(Vec::new())));
        let audit=gate.audit_handle();let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let read=peer_call("workspace_read",json!({"path":"absent"}));
        for _ in 0..26 {let _=runtime.block_on(gate.execute(&read));}
        assert_eq!(audit.lock().unwrap()["complete"],false);assert!(serde_json::to_vec(&*audit.lock().unwrap()).unwrap().len()<=8192);
        let poisoned=audit.clone();let _=std::thread::spawn(move||{let _lock=poisoned.lock().unwrap();panic!("fixture audit poison");}).join();
        let _=runtime.block_on(gate.execute(&read));assert!(audit.lock().is_err());
    }
    #[test]
    fn native_action_audit_binds_actual_send_results_and_includes_nonpeer_tools(){
        let dir=tempfile::tempdir().unwrap();std::fs::write(dir.path().join("README.md"),"fixture").unwrap();
        let handler:doxa_runtime::PeerToolHandler=Arc::new(|method,_|{assert_eq!(method,"msg");Ok(json!({"message_id":"actual-message","delivered_to":["boss"],"failed":[]}))});
        let desk=Arc::new(PeerDesk::default());let displayed=RefCell::new(Vec::new());
        let emit=|event|displayed.borrow_mut().push(event);let scrub=|text:&str|Ok(text.to_owned());
        let mut gate=NativeVendorGate::new(dir.path(),true,Some(handler),desk.clone(),&scrub,&emit,Arc::new(Mutex::new(Vec::new())));
        let audit=gate.audit_handle();let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let read=peer_call("workspace_read",json!({"path":"README.md"}));runtime.block_on(gate.execute(&read)).unwrap();
        let send=peer_call(doxa_engines::peer_tools::SEND,json!({"target":"boss","text":"fixture","fleet_kind":"handoff","artifact_refs":["checkpoint"]}));
        let execution=gate.execute(&send);let ask=displayed.borrow().iter().find(|row|row["type"]=="needs_input").unwrap()["data"].clone();
        desk.answer(ask["id"].as_str().unwrap(),&json!({"decision":"allow"})).unwrap();runtime.block_on(execution).unwrap();
        let receipt=audit.lock().unwrap();assert_eq!(receipt["actions"][0]["operation"],"other");
        assert_eq!(receipt["actions"][1]["message_id"],"actual-message");assert_eq!(receipt["actions"][1]["success"],true);
        assert_eq!(receipt["actions"][1]["request"]["fleet_kind"],"handoff");assert!(!receipt.to_string().contains("fixture"));
    }
    #[test]
    fn malformed_peer_handoff_is_a_scrubbed_model_refusal_without_backend_or_approval(){
        let dir=tempfile::tempdir().unwrap();let calls=Arc::new(AtomicUsize::new(0));let called=calls.clone();
        let handler:doxa_runtime::PeerToolHandler=Arc::new(move|_,_|{called.fetch_add(1,Ordering::SeqCst);Ok(json!({}))});
        let desk=Arc::new(PeerDesk::default());let displayed=RefCell::new(Vec::new());
        let emit=|event|displayed.borrow_mut().push(event);let scrub=|text:&str|Ok(text.replace("fixture-secret","[redacted] \"quoted\"\n"));
        let mut gate=NativeVendorGate::new(dir.path(),false,Some(handler),desk,&scrub,&emit,Arc::new(Mutex::new(Vec::new())));
        let call=peer_call(doxa_engines::peer_tools::SEND,json!({"target":"current-exact-session","text":"fixture-secret","fleet_kind":"handoff","artifact_refs":[],"in_reply_to":"host-checkpoint-is-not-a-message-id"}));
        let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let result=runtime.block_on(gate.execute(&call)).unwrap();assert!(result["error"].as_str().unwrap().contains("Invalid peer tool arguments"));
        assert_eq!(calls.load(Ordering::SeqCst),0);
        let events=displayed.borrow();assert!(!events.iter().any(|event|event["type"]=="needs_input"));
        assert!(events.iter().any(|event|event["data"]["failure_category"]=="peer_arguments_invalid"));
        assert!(!serde_json::to_string(&*events).unwrap().contains("fixture-secret"));
    }
    #[test]
    fn canonical_lore_calls_are_available_without_peer_opt_in_and_never_without_approval() {
        let dir=tempfile::tempdir().unwrap(); let calls=Arc::new(AtomicUsize::new(0)); let called=calls.clone();
        let handler: doxa_runtime::PeerToolHandler=Arc::new(move |name,args| {
            assert_eq!(name,"lore_remember"); assert_eq!(args["text"],"proposal");
            called.fetch_add(1,Ordering::SeqCst); Ok(json!({"staged":true}))
        });
        let desk=Arc::new(PeerDesk::default()); let displayed=RefCell::new(Vec::new());
        let emit=|event|displayed.borrow_mut().push(event); let scrub=|text:&str|Ok(text.to_owned());
        let mut gate=NativeVendorGate::new(dir.path(),false,None,desk.clone(),&scrub,&emit,Arc::new(Mutex::new(Vec::new())))
            .with_agent(vec![json!({"type":"function","function":{"name":"lore_remember","parameters":{"type":"object"}}})],handler);
        assert_eq!(gate.definitions().len(),1); assert_eq!(gate.definitions()[0]["function"]["name"],"lore_remember");
        let call=peer_call("lore_remember",json!({"text":"proposal"}));
        let execution=gate.execute(&call); assert_eq!(calls.load(Ordering::SeqCst),0);
        let ask=displayed.borrow().iter().find(|event|event["type"]=="needs_input").unwrap()["data"].clone();
        assert!(ask["title"].as_str().unwrap().contains("LORE"));
        desk.answer(ask["id"].as_str().unwrap(),&json!({"decision":"allow"})).unwrap();
        let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        assert_eq!(runtime.block_on(execution).unwrap()["staged"],true); assert_eq!(calls.load(Ordering::SeqCst),1);
        assert!(runtime.block_on(gate.execute(&peer_call("stop",json!({})))).is_err());
        let execution=gate.execute(&call); desk.clear(); assert!(runtime.block_on(execution).is_err());
        assert_eq!(calls.load(Ordering::SeqCst),1);
    }
    #[test]
    fn peer_action_waits_for_one_shot_permission_and_scrubs_return_value() {
        let dir = tempfile::tempdir().unwrap();
        let calls = Arc::new(AtomicUsize::new(0)); let called = calls.clone();
        let handler: doxa_runtime::PeerToolHandler = Arc::new(move |method, arguments| {
            assert_eq!(method, "msg"); assert_eq!(arguments["text"], "[redacted] message");
            called.fetch_add(1, Ordering::SeqCst); Ok(json!({"result":"secret reply"}))
        });
        let desk = Arc::new(PeerDesk::default()); let displayed = RefCell::new(Vec::new());
        let emit = |event| displayed.borrow_mut().push(event);
        let scrub = |text: &str| Ok(text.replace("saved-inactive-fixture-key", "***").replace("secret", "[redacted]"));
        let events = Arc::new(Mutex::new(Vec::new()));
        let mut gate = NativeVendorGate::new(dir.path(), false, Some(handler), desk.clone(), &scrub, &emit, events.clone());
        assert_eq!(gate.definitions().len(), 3);
        let mut call = peer_call(doxa_engines::peer_tools::SEND, json!({"target":"same-project","text":"secret message"}));
        call.id = "saved-inactive-fixture-key".into();
        let execution = gate.execute(&call);
        assert_eq!(calls.load(Ordering::SeqCst), 0);
        let ask = displayed.borrow().iter().find(|event| event["type"] == "needs_input").unwrap()["data"].clone();
        assert_eq!(ask["tool_name"], doxa_engines::peer_tools::SEND);
        assert!(!ask.to_string().contains("secret"));
        let id = ask["id"].as_str().unwrap();
        assert!(desk.answer(id, &json!({"decision":"allowForSession"})).is_err());
        desk.answer(id, &json!({"decision":"allow"})).unwrap();
        assert!(desk.answer(id, &json!({"decision":"allow"})).is_err());
        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        assert_eq!(runtime.block_on(execution).unwrap()["result"], "[redacted] reply");
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert!(events.lock().unwrap().iter().any(|event| event["type"] == "needs_input_resolved"));
        assert!(!displayed.borrow().iter().any(|event| event.to_string().contains("saved-inactive-fixture-key")));
        assert!(!events.lock().unwrap().iter().any(|event| event.to_string().contains("saved-inactive-fixture-key")));
    }
    #[test]
    fn retryable_peer_refusals_are_scrubbed_results_and_do_not_disable_the_tool() {
        let dir=tempfile::tempdir().unwrap(); let calls=Arc::new(AtomicUsize::new(0)); let called=calls.clone();
        let handler:doxa_runtime::PeerToolHandler=Arc::new(move |method,_| {
            assert_eq!(method,"msg");
            match called.fetch_add(1,Ordering::SeqCst) {
                0=>Err("no live same-scope peer matches secret target".into()),
                1=>Err("peer target is ambiguous".into()),
                _=>Ok(json!({"delivered_to":["fixture"]})),
            }
        });
        let desk=Arc::new(PeerDesk::default()); let displayed=RefCell::new(Vec::new());
        let emit=|event|displayed.borrow_mut().push(event); let scrub=|text:&str|Ok(text.replace("secret","[redacted]"));
        let events=Arc::new(Mutex::new(Vec::new()));
        let mut gate=NativeVendorGate::new(dir.path(),false,Some(handler),desk.clone(),&scrub,&emit,events.clone());
        let call=peer_call(doxa_engines::peer_tools::SEND,json!({"target":"fixture","text":"bounded message"}));
        let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        for index in 0..3 {
            displayed.borrow_mut().clear();
            let execution=gate.execute(&call);
            let ask=displayed.borrow().iter().find(|row|row["type"]=="needs_input").unwrap()["data"].clone();
            desk.answer(ask["id"].as_str().unwrap(),&json!({"decision":"allow"})).unwrap();
            let result=runtime.block_on(execution).unwrap();
            if index<2 { assert!(result["error"].as_str().unwrap().starts_with("peer_send: ")); }
            else { assert_eq!(result["delivered_to"],json!(["fixture"])); }
            assert!(!result.to_string().contains("secret"));
            assert!(gate.definitions().iter().any(|row|row["function"]["name"]==doxa_engines::peer_tools::SEND));
        }
        assert_eq!(calls.load(Ordering::SeqCst),3);
        let rows=events.lock().unwrap();
        assert_eq!(rows.iter().filter(|row|row["type"]=="tool_result"&&row["data"]["is_error"]==true).count(),2);
        assert!(!rows.iter().any(|row|row["type"]=="tool_disabled"));
    }
    #[test]
    fn denied_cancelled_and_extra_argument_peer_calls_take_no_action() {
        let dir = tempfile::tempdir().unwrap(); let calls = Arc::new(AtomicUsize::new(0)); let called = calls.clone();
        let handler: doxa_runtime::PeerToolHandler = Arc::new(move |_, _| { called.fetch_add(1, Ordering::SeqCst); Ok(json!({})) });
        let desk = Arc::new(PeerDesk::default()); let displayed = RefCell::new(Vec::new());
        let emit = |event| displayed.borrow_mut().push(event); let scrub = |text: &str| Ok(text.to_owned());
        let mut gate = NativeVendorGate::new(dir.path(), false, Some(handler), desk.clone(), &scrub, &emit, Arc::new(Mutex::new(Vec::new())));
        let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let bad = peer_call(doxa_engines::peer_tools::LIST, json!({"method":"stop"}));
        let refused=runtime.block_on(gate.execute(&bad)).unwrap();
        assert!(refused["error"].as_str().unwrap().contains("Invalid peer tool arguments"));
        assert_eq!(calls.load(Ordering::SeqCst),0);
        assert!(!displayed.borrow().iter().any(|event|event["type"]=="needs_input"));
        assert!(displayed.borrow().iter().any(|event|event["data"]["failure_category"]=="peer_arguments_invalid"));
        let call = peer_call(doxa_engines::peer_tools::LIST, json!({}));
        let execution = gate.execute(&call); let id = displayed.borrow().iter().find(|event| event["type"] == "needs_input").unwrap()["data"]["id"].as_str().unwrap().to_owned();
        desk.answer(&id, &json!({"decision":"deny"})).unwrap();
        assert!(runtime.block_on(execution).unwrap()["error"].is_string());
        let execution = gate.execute(&call); desk.clear();
        assert!(runtime.block_on(execution).is_err());
        assert_eq!(calls.load(Ordering::SeqCst), 0);
    }
}
