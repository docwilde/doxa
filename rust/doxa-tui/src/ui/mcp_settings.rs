//! Explicit MCP setup and transient search-key input. No key enters a normal
//! Settings draft, provider prompt, debug output or asynchronous result.
use super::{App, credential_editor};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use doxa_vendors::credentials::{self, CredentialStatus, SearchProvider};
use sha2::{Digest, Sha256};
use std::{path::{Path, PathBuf}, sync::{Arc, atomic::{AtomicBool, Ordering}, mpsc::{self, Receiver, TryRecvError}}, time::Duration};

pub(super) struct SecretEdit {
    pub token: u64,
    provider: SearchProvider,
    owner: (usize, String),
    buffer: credential_editor::Buffer,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::Event;
    use ratatui::{backend::TestBackend, layout::Rect, Terminal};
    use serde_json::json;
    use std::os::unix::fs::PermissionsExt;

    // Environment and credential I/O run in an isolated subprocess, so the
    // regular frontend suite never writes or resolves the owner's live keys.
    fn isolated(name:&str)->bool {
        if std::env::var("DOXA_MCP_UI_FIXTURE").as_deref()==Ok(name) {return true;}
        let directory=tempfile::tempdir().unwrap();
        std::fs::set_permissions(directory.path(),std::fs::Permissions::from_mode(0o700)).unwrap();
        let mut command=std::process::Command::new(std::env::current_exe().unwrap());
        command.args(["--exact",name,"--test-threads=1"]).env("DOXA_MCP_UI_FIXTURE",name).env("DOXA_HOME",directory.path());
        for name in ["DEEPSEEK_API_KEY","ZAI_API_KEY","TYPESAFE_API_KEY","TAVILY_API_KEY","FIRECRAWL_API_KEY","EXA_API_KEY","BRAVE_API_KEY","DOXA_ENGINE","DOXA_MCP_ENABLED","DOXA_MCP_CONFIG"] {command.env_remove(name);}
        let output=command.output().unwrap();
        assert!(output.status.success(),"{} {}",String::from_utf8_lossy(&output.stdout),String::from_utf8_lossy(&output.stderr));
        false
    }
    fn key(app:&mut App,code:KeyCode) {app.handle(Event::Key(KeyEvent::new(code,KeyModifiers::NONE)));}
    fn paint(app:&App)->String {
        let mut terminal=Terminal::new(TestBackend::new(140,36)).unwrap();
        terminal.draw(|frame|app.draw(frame)).unwrap();
        terminal.backend().buffer().content.iter().map(|cell|cell.symbol()).collect()
    }
    fn select(app:&mut App,name:&str) {
        let menu=app.settings_menu.as_mut().unwrap();
        menu.selected=menu.rows.iter().position(|row|row.setting.key==name).unwrap();
    }
    fn app()->App {
        let mut app=App {size:Rect::new(0,0,140,36),..Default::default()};
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"one","engine":"glm","model":"fixture","cwd":std::env::var("DOXA_HOME").unwrap()}));
        app.input="ordinary prompt".into();app.input_cursor=app.input.len();
        app.open_settings_menu();key(&mut app,KeyCode::Char('0'));
        assert_eq!(app.settings_menu.as_ref().unwrap().category,9);
        app
    }
    #[test]
    fn mcp_settings_masks_private_keys_and_keeps_config_and_prompts_clean() {
        if !isolated("ui::mcp_settings::tests::mcp_settings_masks_private_keys_and_keeps_config_and_prompts_clean") {return;}
        let mut app=app();select(&mut app,"tavily_api_key");key(&mut app,KeyCode::Enter);
        key(&mut app,KeyCode::Char('X'));
        assert!(paint(&app).contains('•'));assert!(!format!("{app:?}").contains("buffer"));
        key(&mut app,KeyCode::Backspace);
        let secret="fixture-tavily-secret";
        app.handle(Event::Paste(secret.into()));
        assert_eq!(app.input,"ordinary prompt");assert!(app.pending_prompts.is_empty());
        assert!(!paint(&app).contains(secret));assert!(!format!("{app:?}").contains(secret));
        assert!(app.settings_menu.as_ref().unwrap().draft.is_none());
        assert!(!app.settings_menu.as_ref().unwrap().edits.contains_key("tavily_api_key"));
        key(&mut app,KeyCode::Enter);
        assert!(app.mcp_secret.is_none());
        assert_eq!(credentials::search_resolve(SearchProvider::Tavily).unwrap().as_deref(),Some(secret));
        assert!(!crate::operations::settings_report().unwrap().contains(secret));
        let config_path=crate::settings::config_path().unwrap();
        assert!(!std::fs::read_to_string(&config_path).unwrap_or_default().contains(secret));
        assert!(crate::settings::coerce(crate::settings::find("tavily_api_key").unwrap(),Some(secret)).is_err());
        assert!(crate::settings::coerce(crate::settings::find("tavily_api_key").unwrap(),None).is_err());
        std::env::set_var("TAVILY_API_KEY","fixture-environment-tavily");
        app.open_settings_menu();key(&mut app,KeyCode::Char('0'));select(&mut app,"tavily_api_key");
        key(&mut app,KeyCode::Enter);assert!(app.mcp_secret.is_none());
        assert!(app.notice.contains("Environment"));
        assert!(!crate::operations::settings_report().unwrap().contains("fixture-environment-tavily"));
        std::env::remove_var("TAVILY_API_KEY");app.open_settings_menu();key(&mut app,KeyCode::Char('0'));select(&mut app,"tavily_api_key");
        key(&mut app,KeyCode::Delete);assert_eq!(credentials::search_status(SearchProvider::Tavily).unwrap(),CredentialStatus::Missing);
    }
    #[test]
    fn mcp_secret_owner_change_resize_and_late_clipboard_discard_input() {
        if !isolated("ui::mcp_settings::tests::mcp_secret_owner_change_resize_and_late_clipboard_discard_input") {return;}
        let mut app=app();select(&mut app,"exa_api_key");key(&mut app,KeyCode::Enter);
        let token=app.mcp_secret.as_ref().unwrap().token;
        app.clipboard_secret_owner=Some(token);
        app.clipboard_job=Some(crate::clipboard::Job::fixture(app.clipboard_target(),Ok("fixture-late-exa-secret".into())));
        app.handle(Event::Paste("fixture-unsaved-exa".into()));
        let before=app.prompt_owner();app.groups[0].tabs.push("two".into());app.groups[0].active=app.groups[0].tabs.len()-1;
        app.finish_prompt_owner_transition(before);assert!(app.mcp_secret.is_none());
        assert!(app.poll_clipboard());assert!(!app.input.contains("secret"));
        assert!(app.input_drafts.values().all(|(value,_)|!value.contains("fixture-unsaved-exa")));
        assert!(app.pending_prompts.is_empty());
        app.open_settings_menu();key(&mut app,KeyCode::Char('0'));select(&mut app,"exa_api_key");key(&mut app,KeyCode::Enter);
        app.handle(Event::Paste("fixture-resize-exa".into()));app.handle(Event::Resize(20,5));
        assert!(app.mcp_secret.is_none());assert!(app.settings_menu.is_none());
        assert_eq!(credentials::search_status(SearchProvider::Exa).unwrap(),CredentialStatus::Missing);
    }
    #[test]
    fn mcp_probe_results_are_bound_to_owner_and_path() {
        if !isolated("ui::mcp_settings::tests::mcp_probe_results_are_bound_to_owner_and_path") {return;}
        let mut app=app();let path=PathBuf::from("/private/fixture.json");
        app.settings_menu.as_mut().unwrap().edits.insert("mcp_config".into(),Some(path.display().to_string()));
        let (sender,receiver)=mpsc::sync_channel(1);let stop=Arc::new(AtomicBool::new(false));
        app.mcp_probe=Some(Probe {owner:app.prompt_owner(),path:path.clone(),cancel:stop.clone(),state:ProbeState::Running(receiver)});
        app.start_mcp_probe();assert!(app.notice.contains("still running"));
        app.settings_menu.as_mut().unwrap().edits.insert("mcp_config".into(),Some("/private/changed.json".into()));
        app.retire_mcp_if_changed();assert!(stop.load(Ordering::SeqCst));
        sender.send(Ok(Report {digest:vec![0;32],servers:999,tools:333,checks:Vec::new()})).unwrap();
        assert!(app.poll_mcp_probe());assert!(!app.mcp_probe_lines(Path::new("/private/changed.json")).join(" ").contains("999"));
        let before=app.prompt_owner();app.groups[0].tabs.push("two".into());app.groups[0].active=app.groups[0].tabs.len()-1;
        app.finish_prompt_owner_transition(before);
        assert!(!app.mcp_probe_lines(&path).join(" ").contains("999"));
    }
    #[test]
    fn mcp_server_pages_show_safe_ids_allowlists_and_individual_probe_status() {
        if !isolated("ui::mcp_settings::tests::mcp_server_pages_show_safe_ids_allowlists_and_individual_probe_status") {return;}
        let mut app=app();let path=PathBuf::from("/private/fixture.json");
        app.settings_menu.as_mut().unwrap().edits.insert("mcp_config".into(),Some(path.display().to_string()));
        let checks=vec![
            ServerCheck {id:"brave".into(),enabled:false,allow_tools:vec!["brave_web_search".into()],status:"disabled · no connection".into()},
            ServerCheck {id:"exa".into(),enabled:true,allow_tools:vec!["web_search_exa".into()],status:"error · initialization failed".into()},
        ];
        app.mcp_config_view=Some(ConfigView {owner:app.prompt_owner(),path:path.clone(),state:ConfigViewState::Complete(Ok(checks.clone()))});
        let lines=app.mcp_probe_lines(&path).join(" ");
        assert!(lines.contains("brave") && lines.contains("disabled") && lines.contains("brave_web_search"));
        key(&mut app,KeyCode::PageDown);assert_eq!(app.mcp_server_page,1);
        let lines=app.mcp_probe_lines(&path).join(" ");assert!(lines.contains("exa") && lines.contains("web_search_exa") && lines.contains("error"));
        key(&mut app,KeyCode::PageUp);assert_eq!(app.mcp_server_page,0);
        app.mcp_probe=Some(Probe {owner:app.prompt_owner(),path:path.clone(),cancel:Arc::new(AtomicBool::new(false)),
            state:ProbeState::Complete(Ok(Report {digest:vec![0;32],servers:0,tools:0,checks}))});
        assert!(app.mcp_probe_lines(&path).join(" ").contains("Snapshot"));
        let config:doxa_mcp::Config=serde_json::from_str(include_str!("../../../../docs/examples/mcp-search.json")).unwrap();
        assert_eq!(server_checks(&config).unwrap().len(),4);
        std::env::set_var("EXA_API_KEY","brave_web_search");
        assert!(server_checks(&config).is_err());
    }
    #[test]
    fn mcp_probe_lists_local_fixture_and_never_calls_tools_or_enables_settings() {
        if !isolated("ui::mcp_settings::tests::mcp_probe_lists_local_fixture_and_never_calls_tools_or_enables_settings") {return;}
        let directory=tempfile::tempdir().unwrap();let path=directory.path().join("mcp.json");
        let journal=directory.path().join("journal.jsonl");
        let config=json!({"schema_version":1,"servers":[{"id":"fixture","enabled":true,"allow_tools":["search"],
            "transport":{"transport":"stdio","program":"/usr/bin/python3","args":["-u",format!("{}/../doxa-mcp/tests/fake_stdio.py",env!("CARGO_MANIFEST_DIR")),"good",journal]},
            "limits":{"timeout_ms":2000,"shutdown_ms":30}}]});
        std::fs::write(&path,config.to_string()).unwrap();std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o600)).unwrap();
        let runtime=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let report=runtime.block_on(inspect(&path,directory.path(),Arc::new(AtomicBool::new(false)))).unwrap();
        assert_eq!((report.servers,report.tools),(1,1));
        let records=std::fs::read_to_string(&journal).unwrap();assert!(records.contains("tools/list"));assert!(!records.contains("tools/call"));
        assert!(!crate::settings::enabled("mcp_enabled"));
        crate::settings::save(&crate::settings::config_path().unwrap(),&[("mcp_config".into(),Some(path.display().to_string())),("mcp_enabled".into(),Some("on".into()))],"glm").unwrap();
        assert!(crate::settings::enabled("mcp_enabled"));
        let before=std::fs::read_to_string(crate::settings::config_path().unwrap()).unwrap();
        assert!(crate::settings::save(&crate::settings::config_path().unwrap(),&[("mcp_config".into(),None)],"glm").is_err());
        assert_eq!(std::fs::read_to_string(crate::settings::config_path().unwrap()).unwrap(),before);
        let tabs=super::super::choice_dropdown::settings_tabs(Rect::new(0,0,34,12),9);
        assert!(tabs.iter().any(|(index,_,label)|*index==9 && label.contains("0 MCP")));
    }
}
pub(super) struct Probe {
    owner: (usize, String),
    path: PathBuf,
    cancel: Arc<AtomicBool>,
    state: ProbeState,
}
#[derive(Clone,Debug)]
pub(super) struct ServerCheck {id:String,enabled:bool,allow_tools:Vec<String>,status:String}
pub(super) struct ConfigView {owner:(usize,String),path:PathBuf,state:ConfigViewState}
enum ConfigViewState {Running(Receiver<Result<Vec<ServerCheck>,&'static str>>),Complete(Result<Vec<ServerCheck>,&'static str>)}
impl std::fmt::Debug for ConfigView {
    fn fmt(&self,f:&mut std::fmt::Formatter<'_>)->std::fmt::Result {f.write_str("McpConfigView { private_metadata: withheld }")}
}
pub(super) enum ProbeState {
    Running(Receiver<Result<Report, &'static str>>),
    Complete(Result<Report, &'static str>),
}
pub(super) struct Report { digest: Vec<u8>, servers: usize, tools: usize,checks:Vec<ServerCheck> }
impl std::fmt::Debug for SecretEdit {
    fn fmt(&self,f:&mut std::fmt::Formatter<'_>)->std::fmt::Result {f.write_str("SearchCredentialEditor { masked: true }")}
}
impl std::fmt::Debug for Probe {
    fn fmt(&self,f:&mut std::fmt::Formatter<'_>)->std::fmt::Result {f.write_str("McpProbe { private_metadata: withheld }")}
}
impl Drop for Probe { fn drop(&mut self) { self.cancel.store(true, Ordering::SeqCst); } }

fn digest(config: &doxa_mcp::Config) -> Vec<u8> {
    Sha256::digest(serde_json::to_vec(config).expect("validated serializable config")).to_vec()
}
fn server_checks(config:&doxa_mcp::Config)->Result<Vec<ServerCheck>,&'static str> {
    let public=serde_json::Value::Array(config.servers.iter().map(|server|serde_json::json!({
        "id":server.id,"allow_tools":server.allow_tools})).collect());
    let mut scrubbed=public.clone();
    credentials::redact_json(&mut scrubbed).map_err(|_|"Public MCP metadata unavailable")?;
    if scrubbed!=public {return Err("Credential reflected in MCP public metadata; details withheld");}
    Ok(config.servers.iter().map(|server|ServerCheck {id:server.id.clone(),enabled:server.enabled,
        allow_tools:server.allow_tools.clone(),status:if server.enabled {"not probed"} else {"disabled · no connection"}.into()}).collect())
}

// A ten-second total probe deadline includes all enabled servers. Each client
// owns its runtime until explicit close; no tools/call or automatic retry.
async fn inspect(path: &Path, cwd: &Path, cancel: Arc<AtomicBool>) -> Result<Report, &'static str> {
    let config=doxa_mcp::Config::load(path).map_err(|_|"Private MCP config unavailable or invalid")?;
    let original=digest(&config);
    let mut checks=server_checks(&config)?;
    let stop=cancel.clone();
    let timer=tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(10)).await;
        stop.store(true,Ordering::SeqCst);
    });
    let result=async {
        let mut servers=0;
        let mut tools=0;
        for (server,check) in config.servers.iter().zip(checks.iter_mut()) {
            if !server.enabled {continue;}
            if cancel.load(Ordering::SeqCst) {check.status="stopped/deadline · no connection".into();continue;}
            let mut bounded=server.clone();
            bounded.limits.timeout_ms=bounded.limits.timeout_ms.min(5000);
            bounded.limits.shutdown_ms=bounded.limits.shutdown_ms.min(100);
            let mut client=match doxa_mcp::Client::connect(&bounded,cwd,cancel.clone()).await {
                Ok(client)=>client,
                Err(_)=>{check.status="error · connection/initialization failed".into();continue;},
            };
            let listed=client.list_tools(cancel.clone()).await;
            client.close().await;
            let listed=match listed {Ok(listed)=>listed,Err(_)=>{check.status="error · catalog unavailable/invalid".into();continue;}};
            check.status=format!("connected · catalog {} allowlisted",listed.len());
            tools+=listed.len();
            servers+=1;
        }
        let current=doxa_mcp::Config::load(path).map_err(|_|"MCP config changed during probe")?;
        if digest(&current)!=original {return Err("MCP config changed during probe");}
        Ok(Report {digest:original,servers,tools,checks})
    }.await;
    timer.abort();let _=timer.await;
    result
}

impl App {
    pub(super) fn poll_mcp_config_view(&mut self)->bool {
        let selected=self.settings_menu.as_ref().filter(|menu|crate::settings::CATEGORIES[menu.category]=="MCP" && menu.draft.is_none())
            .map(|menu|(self.prompt_owner(),PathBuf::from(menu.effective_value("mcp_config"))));
        if let Some(view)=self.mcp_config_view.as_mut() {
            if let ConfigViewState::Running(receiver)=&view.state {
                match receiver.try_recv() {
                    Ok(result)=>{view.state=ConfigViewState::Complete(result);return true;},
                    Err(TryRecvError::Empty)=>return false,
                    Err(TryRecvError::Disconnected)=>{view.state=ConfigViewState::Complete(Err("Config reader stopped"));return true;},
                }
            }
        }
        let Some((owner,path))=selected.filter(|(_,path)|path.is_absolute()) else {return false;};
        if self.mcp_config_view.as_ref().is_some_and(|view|view.owner==owner && view.path==path) {return false;}
        let (sender,receiver)=mpsc::sync_channel(1);let worker_path=path.clone();
        std::thread::spawn(move || {
            let result=doxa_mcp::Config::load(&worker_path).map_err(|_|"Private MCP config unavailable or invalid")
                .and_then(|config|server_checks(&config));
            let _=sender.send(result);
        });
        self.mcp_server_page=0;
        self.mcp_config_view=Some(ConfigView {owner,path,state:ConfigViewState::Running(receiver)});true
    }
    pub(super) fn mcp_page_key(&mut self,key:KeyEvent)->bool {
        if !self.settings_menu.as_ref().is_some_and(|menu|crate::settings::CATEGORIES[menu.category]=="MCP" && menu.draft.is_none()) {return false;}
        match key.code {
            KeyCode::PageUp=>self.mcp_server_page=self.mcp_server_page.saturating_sub(1),
            KeyCode::PageDown=>self.mcp_server_page=(self.mcp_server_page+1).min(15),
            KeyCode::Char('r') if key.modifiers.contains(KeyModifiers::CONTROL)=>{
                if !self.mcp_config_view.as_ref().is_some_and(|view|matches!(view.state,ConfigViewState::Running(_))) {
                    self.mcp_config_view=None;self.poll_mcp_config_view();
                }
            },
            _=>return false,
        }
        true
    }
    fn mcp_secret_current(&self) -> bool {
        self.mcp_secret.as_ref().is_some_and(|secret|secret.owner==self.prompt_owner()
            && !std::env::var_os(secret.provider.env_var()).is_some_and(|value|!value.is_empty())
            && self.settings_menu.as_ref().is_some_and(|menu|menu.rows.get(menu.selected)
                .is_some_and(|row|SearchProvider::from_setting_key(row.setting.key)==Some(secret.provider)
                    && crate::settings::CATEGORIES[menu.category]=="MCP")))
    }
    pub(super) fn retire_mcp_if_changed(&mut self) {
        if self.mcp_secret.is_some() && !self.mcp_secret_current() {self.mcp_secret=None;}
        let path=self.settings_menu.as_ref().map(|menu|PathBuf::from(menu.effective_value("mcp_config")));
        if self.mcp_probe.as_ref().is_some_and(|probe|probe.owner!=self.prompt_owner() || path.as_ref()!=Some(&probe.path)) {
            self.mcp_probe.as_ref().unwrap().cancel.store(true,Ordering::SeqCst);
        }
    }
    pub(super) fn start_mcp_secret(&mut self, provider: SearchProvider) {
        match credentials::search_status(provider) {
            Ok(CredentialStatus::Environment)=>{self.notice=format!("{} is environment-owned; unset {} to edit",provider.label(),provider.env_var());return;},
            Err(_)=>{self.notice="Private search credential store unavailable".into();return;},
            _=>{},
        }
        self.mcp_secret=Some(SecretEdit {token:credential_editor::next_token(),provider,
            owner:self.prompt_owner(),buffer:credential_editor::Buffer::new()});
        self.choice_dropdown=None;
        self.notice="Masked key editor · Enter saves privately · Esc discards".into();
    }
    pub(super) fn remove_mcp_secret(&mut self, provider: SearchProvider) {
        match credentials::search_remove(provider) {
            Ok(())=>{self.refresh_search_rows();self.notice="Saved search key removed".into();},
            Err(_)=>self.notice="Search key unchanged; environment ownership or private store prevents removal".into(),
        }
    }
    fn refresh_search_rows(&mut self) {
        let Some(menu)=self.settings_menu.as_mut() else {return;};
        if let Ok(rows)=crate::settings::rows(&menu.engine,None) {
            for row in &mut menu.rows {
                if row.setting.kind==crate::settings::Kind::Credential {
                    if let Some(fresh)=rows.iter().find(|fresh|fresh.setting.key==row.setting.key) {*row=fresh.clone();}
                }
            }
        }
    }
    pub(super) fn mcp_secret_mask(&self,key:&str)->Option<String> {
        self.mcp_secret.as_ref().filter(|secret|self.mcp_secret_current()
            && SearchProvider::from_setting_key(key)==Some(secret.provider)).map(|secret| {
                let mask=secret.buffer.mask();if mask.is_empty() {"(enter key · masked)".into()} else {mask}
            })
    }
    pub(super) fn mcp_secret_paste(&mut self,text:&str)->bool {
        if !self.mcp_secret_current() {self.mcp_secret=None;self.notice="Credential paste discarded; editor changed".into();return true;}
        if !self.mcp_secret.as_mut().unwrap().buffer.append(text) {
            self.notice="Key input rejected; use at most 4096 printable ASCII bytes without newlines".into();
        }
        true
    }
    pub(super) fn mcp_secret_key(&mut self,key:KeyEvent)->bool {
        if self.mcp_secret.is_none() {return false;}
        if !self.mcp_secret_current() {self.mcp_secret=None;return true;}
        match key.code {
            KeyCode::Esc=>{self.mcp_secret=None;self.notice="Key edit discarded".into();},
            KeyCode::Enter|KeyCode::Char('s') if key.code==KeyCode::Enter || key.modifiers.contains(KeyModifiers::CONTROL)=>{
                let secret=self.mcp_secret.as_ref().unwrap();
                match credentials::search_save(secret.provider,secret.buffer.value()) {
                    Ok(())=>{self.mcp_secret=None;self.refresh_search_rows();self.notice="Search key saved privately".into();},
                    Err(_)=>self.notice="Key unchanged; check input, environment ownership and private credential store".into(),
                }
            },
            KeyCode::Backspace=>self.mcp_secret.as_mut().unwrap().buffer.backspace(),
            KeyCode::Char(ch) if !key.modifiers.intersects(KeyModifiers::CONTROL|KeyModifiers::ALT)=>{self.mcp_secret_paste(&ch.to_string());},
            _=>{},
        }
        true
    }
    pub(super) fn start_mcp_probe(&mut self) {
        if self.mcp_probe.as_ref().is_some_and(|probe|matches!(probe.state,ProbeState::Running(_))) {
            self.notice="MCP probe still running or closing; wait for bounded cleanup".into();return;
        }
        let Some(menu)=self.settings_menu.as_mut() else {return;};
        menu.finish_draft();
        let path=PathBuf::from(menu.effective_value("mcp_config"));
        if !path.is_absolute() {self.notice="Choose an absolute private MCP config path first".into();return;}
        let owner=self.prompt_owner();
        let cwd=self.session_cwds.get(&owner.1).cloned().or_else(||std::env::current_dir().ok());
        let Some(cwd)=cwd else {self.notice="MCP probe workspace unavailable".into();return;};
        self.mcp_probe=None;
        let cancel=Arc::new(AtomicBool::new(false));
        let stop=cancel.clone();let worker_path=path.clone();
        let (sender,receiver)=mpsc::sync_channel(1);
        std::thread::spawn(move || {
            let result=tokio::runtime::Builder::new_current_thread().enable_all().build()
                .map_err(|_|"MCP probe runtime unavailable")
                .and_then(|runtime|runtime.block_on(inspect(&worker_path,&cwd,stop)));
            let _=sender.send(result);
        });
        self.mcp_probe=Some(Probe {owner,path,cancel,state:ProbeState::Running(receiver)});
        self.notice="MCP connection/list probe · no tool calls · 10-second deadline".into();
    }
    pub(super) fn poll_mcp_probe(&mut self)->bool {
        let existed=self.mcp_probe.is_some();self.retire_mcp_if_changed();
        let Some(probe)=self.mcp_probe.as_mut() else {return existed;};
        let ProbeState::Running(receiver)=&probe.state else {return false;};
        let result=match receiver.try_recv() {
            Ok(result)=>result,Err(TryRecvError::Empty)=>return false,
            Err(TryRecvError::Disconnected)=>Err("MCP probe worker stopped"),
        };
        probe.state=ProbeState::Complete(result);true
    }
    pub(super) fn mcp_probe_lines(&self,path:&Path)->Vec<String> {
        let probe=self.mcp_probe.as_ref().filter(|probe|probe.path==path && probe.owner==self.prompt_owner());
        let view=self.mcp_config_view.as_ref().filter(|view|view.path==path && view.owner==self.prompt_owner());
        let (status,checks)=match probe.map(|probe|&probe.state) {
            Some(ProbeState::Complete(Ok(report)))=>{
                let hash=report.digest.iter().take(6).map(|byte|format!("{byte:02x}")).collect::<String>();
                (format!("Snapshot {} connected · {} tools · config {hash}",report.servers,report.tools),Some(&report.checks))
            },
            Some(ProbeState::Complete(Err(reason)))=>((*reason).into(),None),
            state=>{
                let status=if matches!(state,Some(ProbeState::Running(_))) {"Connecting/listing · no tool calls"} else {"Config snapshot · not probed · Ctrl+P connects/lists"};
                match view.map(|view|&view.state) {
                    Some(ConfigViewState::Complete(Ok(checks)))=>(status.into(),Some(checks)),
                    Some(ConfigViewState::Complete(Err(reason)))=>((*reason).into(),None),
                    Some(ConfigViewState::Running(_))=>("Reading private config · no connections".into(),None),
                    None=>(status.into(),None),
                }
            },
        };
        let mut lines=vec![status];
        if let Some(checks)=checks {
            if let Some(check)=checks.get(self.mcp_server_page.min(checks.len().saturating_sub(1))) {
                let page=self.mcp_server_page.min(checks.len()-1)+1;
                lines.push(format!("Server {page}/{} · PgUp/PgDn · {} · {} · {}",checks.len(),super::safe_label(&check.id),if check.enabled {"enabled"} else {"disabled"},super::safe_label(&check.status)));
                let names=check.allow_tools.iter().take(3).map(|name|super::safe_label(name)).collect::<Vec<_>>().join(", ");
                lines.push(format!("Allowlist ({}): {}{}",check.allow_tools.len(),if names.is_empty() {"none"} else {&names},if check.allow_tools.len()>3 {" … (remaining names in private config)"} else {""}));
            } else {lines.push("No configured servers".into());}
        }
        lines
    }
}
