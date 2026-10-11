//! Internal native CLI workers. Provider artifacts and protected context proof
//! belong to the worker UUID; public conversation/registry identity stays router.
use crate::{claude_host::ClaudeHost,codex_host::CodexHost};
use doxa_engines::{codex_appserver::{AppServerDriver,AppServerOptions,CodexPermission},codex_driver::{DriverOptions,SandboxMode}};
use doxa_router::{Candidate,Provider};
use doxa_runtime::Host;
use serde_json::Value;
use std::{io::Read,path::{Path,PathBuf},sync::{Arc,atomic::{AtomicBool,Ordering}},time::Duration};

#[derive(Clone,Default)]
pub struct CliOptions { pub claude: Option<PathBuf>, pub codex: Option<PathBuf> }

pub(super) fn advertised(candidate:&Candidate,row:&Value)->bool {
    row["model"] == candidate.model
        && row["resolved_model"].as_str().is_none_or(|resolved|resolved == candidate.model)
        && row["efforts"].as_array().is_some_and(|efforts|efforts.iter().any(|effort|effort == &candidate.effort))
}

enum WorkerKind { Claude(ClaudeHost), Codex(CodexHost) }
pub(super) struct Worker { inner: WorkerKind, closed: AtomicBool }
impl Worker {
    fn new(inner:WorkerKind)->Self {Self{inner,closed:AtomicBool::new(false)}}
    pub(super) fn host(&self) -> &dyn Host { match &self.inner {WorkerKind::Claude(host)=>host,WorkerKind::Codex(host)=>host} }
    pub(super) fn shutdown(&self) {
        if self.closed.swap(true,Ordering::AcqRel) {return;}
        match &self.inner {WorkerKind::Claude(host)=>{host.shutdown();},WorkerKind::Codex(host)=>{host.shutdown();}}
    }
}
impl Drop for Worker { fn drop(&mut self) { self.shutdown(); } }

impl CliOptions {
    pub(super) fn catalog(&self, provider: Provider, cwd: &Path, cancel: &AtomicBool) -> Result<Vec<Value>,String> {
        if cancel.load(Ordering::Acquire) { return Err("Router discovery cancelled".into()); }
        match provider {
            Provider::Claude => {
                let path = self.claude.as_deref().ok_or("Router Claude executable unavailable")?;
                let home = doxa_claude::isolation::prepare().map_err(|_|"Router Claude private account unavailable")?;
                let plugins = doxa_claude::isolation::adopted_plugins().map_err(|_|"Router Claude plugin snapshot unavailable")?;
                let id = worker_id().map_err(|_|"Router discovery identity unavailable")?;
                let catalog = doxa_claude::catalog::discover_authenticated_cancellable(doxa_claude::CliOptions {
                    executable:path,cwd,config_dir:&home,session_id:&id,resume:false,model:None,effort:None,
                    permission_mode:"manual",plugins:&plugins },Duration::from_secs(15),cancel)
                    .map_err(|_|"Router Claude current authentication or model catalog unavailable")?;
                Ok(catalog["capabilities"].as_array().ok_or("Router Claude capabilities unavailable")?.clone())
            }
            Provider::Codex => {
                let path = self.codex.clone().ok_or("Router Codex executable unavailable")?;
                let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().map_err(|_|"Router catalog runtime unavailable")?;
                runtime.block_on(async { tokio::time::timeout(Duration::from_secs(20),AppServerDriver::discover_authenticated_models_cancellable(AppServerOptions {
                    executable:path,cwd:cwd.to_owned(),model:None,sandbox:SandboxMode::WorkspaceWrite,
                    permission:CodexPermission::OnRequest,resume_thread:None,turn_timeout:Duration::from_secs(3600) },cancel)).await })
                    .map_err(|_|"Router Codex catalog timed out")?.map_err(|_|"Router Codex current authentication or model catalog unavailable".into())
            }
            _ => Err("Router CLI discovery requires a CLI provider".into()),
        }
    }

    pub(super) fn start(&self,candidate:&Candidate,cwd:&Path,id:&str,permission:&str,
        agent:Option<Arc<crate::agent_tools::AgentTools>>, cancel:&AtomicBool) -> Result<Arc<Worker>,String> {
        // Fresh current account/catalog check immediately before native worker
        // admission. Jev's earlier shortlist cannot bypass a changed account.
        let rows = self.catalog(candidate.provider,cwd,cancel)?;
        if !rows.iter().any(|row|advertised(candidate,row)) {
            return Err("Router CLI selected model or effort is no longer authenticated and advertised".into());
        }
        if cancel.load(Ordering::Acquire) { return Err("Router worker startup cancelled".into()); }
        let worker = match candidate.provider {
            Provider::Claude => WorkerKind::Claude(ClaudeHost::new_routed_cancellable(self.claude.as_deref().ok_or("Claude executable unavailable")?,
                cwd,id,&candidate.model,&candidate.effort,agent,cancel)?),
            Provider::Codex => {
                let mut options=DriverOptions::new(cwd.to_owned());
                options.executable=self.codex.clone().ok_or("Codex executable unavailable")?;
                options.model=Some(candidate.model.clone()); options.effort=Some(candidate.effort.clone());
                WorkerKind::Codex(CodexHost::new_routed(options,id,agent)?)
            }
            _=>return Err("Not a CLI worker".into()),
        };
        let worker=Worker::new(worker);
        if permission == "auto" { worker.host().call("set_permission_mode",&serde_json::json!({"mode":"auto"}))?; }
        Ok(Arc::new(worker))
    }
}

pub(super) fn worker_id() -> std::io::Result<String> {
    let mut bytes=[0u8;16];std::fs::File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    bytes[6]=(bytes[6]&0x0f)|0x40;bytes[8]=(bytes[8]&0x3f)|0x80;
    let hex:String=bytes.iter().map(|byte|format!("{byte:02x}")).collect();
    Ok(format!("{}-{}-{}-{}-{}",&hex[..8],&hex[8..12],&hex[12..16],&hex[16..20],&hex[20..]))
}
