//! Isolation presentation accepts only host-asserted profiles. User requests
//! never change a chip until the daemon verifies and reports the new policy.
use super::{App, ChipInfo, PaneGroup, safe_label};
use serde_json::{json, Value};
use crossterm::event::{KeyCode,KeyEvent,KeyModifiers};
#[derive(Debug)]
pub(super) struct Confirmation{pub id:String,pub profile:String,pub seen:std::cell::Cell<usize>,pub complete:std::cell::Cell<bool>}
pub(super) fn verified_status(value:&Value)->Option<Value>{
    let profile=doxa_isolation::Profile::parse(value["profile"].as_str()?).ok()?;
    let state=value["state"].as_str()?;
    if !matches!(state,"ready"|"stopped"|"failed"|"unavailable") {return None;}
    let mut status=value.clone();
    status["label"]=json!(if matches!(state,"failed"|"unavailable"){"unavailable"}else{profile.label()});
    Some(status)
}
impl App {
    pub(super) fn isolation_hint(&self,group:usize)->String{
        let value=self.groups.get(group).and_then(PaneGroup::active_id)
            .and_then(|id|self.session_telemetry.get(id)).and_then(|t|t.isolation.as_ref());
        let Some(value)=value else{return "Isolation status unavailable".into();};
        let engine=value["engine"].as_str().unwrap_or("unknown");
        let network=value["network"].as_str().unwrap_or("unknown");
        format!("{} · engine: {engine} · network: {network} · click for isolation menu and limits",
            value["label"].as_str().unwrap_or("unavailable")) +
            if value["disk_budget_error"].as_str().is_some()||value["disk_monitor_error"].as_str().is_some(){" · disk hold"}else{""}
    }
    pub(super) fn open_isolation_info(&mut self,group:usize){
        let Some(id)=self.groups.get(group).and_then(PaneGroup::active_id).map(str::to_owned)else{return;};
        let Some(value)=self.session_telemetry.get(&id).and_then(|t|t.isolation.as_ref())else{return;};
        let mut lines=vec![format!("Isolation: {}",value["label"].as_str().unwrap_or("unavailable")),
            format!("Engine: {}",value["engine"].as_str().unwrap_or("unknown")),
            format!("Network: {}",value["network"].as_str().unwrap_or("unknown"))];
        if value["profile"]!="native"{
            lines.push(format!("Memory: {} MiB · CPU: {} · PIDs: {}",value["memory_bytes"].as_u64().unwrap_or(0)/1024/1024,value["cpus"],value["pids"]));
            let mib=1024*1024;
            let usage=value["disk_usage_bytes"].as_u64().map(|n|format!("{} MiB",n/mib)).unwrap_or_else(||"unavailable".into());
            let ceiling=value["disk_soft_limit_bytes"].as_u64().map(|n|format!("{} MiB",n/mib)).unwrap_or_else(||"legacy session: unset".into());
            let free=value["disk_free_bytes"].as_u64().map(|n|format!("{} MiB",n/mib)).unwrap_or_else(||"unavailable".into());
            let floor=value["disk_free_floor_bytes"].as_u64().map(|n|format!("{} MiB",n/mib)).unwrap_or_else(||"unavailable".into());
            lines.push(format!("Disk used: {usage} / monitored soft ceiling {ceiling}"));
            lines.push(format!("Host free: {free} / monitored floor {floor}"));
            if let Some(error)=value["disk_monitor_error"].as_str(){lines.push(format!("Disk monitor: {}",safe_label(error)));}
            if let Some(error)=value["disk_budget_error"].as_str(){lines.push(format!("New turns held: {}",safe_label(error)));}
            lines.push(format!("Image: {}",value["image"].as_str().unwrap_or("unavailable")));
            lines.push("Mounts: independent Git checkout, private home/cache and session hook broker".into());
            lines.push("Root read-only; capabilities dropped; no-new-privileges; private PID/IPC; rootless Engine".into());
        }
        lines.push(format!("Disk: {}",value["disk_limit"].as_str().unwrap_or("unknown")));
        lines.push(format!("Credentials: {}",value["credential_exposure"].as_str().unwrap_or("unknown")));
        lines.extend([String::new(),"Change while idle: 1 Docker offline · 2 Docker with internet · 3 Native (confirmation required)".into(),"Offline Claude/Codex CLI cannot reach its model provider; API-backed GLM/DeepSeek call models on the host.".into(),"While idle, explicitly change isolation with:".into(),
            "/isolation docker-offline --confirm".into(),"/isolation docker-open --confirm".into(),
            "/isolation native --confirm".into(),
            "Provider, approvals and conversation identity remain attached.".into(),
            "Backend changes checkpoint the stopped provider, copy the workspace and verify same-session resume.".into(),
            "Native resume retains the private clone; failed migration restores the original profile.".into(),"Fleet changes hold admission across every slot; supervised fleets remain paused for owner review.".into()]);
        self.chip_info=Some(ChipInfo{kind:"isolation",label:String::new(),lines,scroll:0,owner:Some((id,String::new()))});
    }
    fn review_isolation_change(&mut self,id:String,profile:doxa_isolation::Profile){
        let mut lines=vec![format!("Change session {} to {}",safe_label(&id),profile.label()),
            "Shift+Y confirms · Esc cancels. Applies only while the fleet is idle.".into(),
            "The host checkpoints and verifies the conversation and workspace. Fleet admission is held during the change.".into()];
        if profile==doxa_isolation::Profile::Native{lines.push("CONFIRM NATIVE EXECUTION: this agent receives owner host, filesystem and credential access outside Docker.".into());}
        if crate::fleet_policy::task_internet(profile){lines.push("CONFIRM INTERNET OPT-IN: this agent may directly access the internet, including web searches.".into());}
        else{lines.push("Direct internet and provider web-search tools are disabled. Ask an approved online supervisor for research evidence.".into());}
        if profile==doxa_isolation::Profile::DockerOffline{lines.push("Ordinary Claude/Codex CLI cannot reach its model provider in offline Docker. API-backed engines keep model calls on the host.".into());}
        lines.push("Supervised fleets stay paused after verification; review fleet status and explicitly continue.".into());
        self.isolation_confirmation=Some(Confirmation{id:id.clone(),profile:profile.key().into(),seen:std::cell::Cell::new(0),complete:std::cell::Cell::new(false)});
        self.chip_info=Some(ChipInfo{kind:"isolation_change",label:profile.key().into(),lines,scroll:0,owner:Some((id,profile.key().into()))});
    }
    pub(super) fn isolation_control_key(&mut self,key:KeyEvent)->bool{
        let Some(info)=self.chip_info.as_ref().filter(|info|matches!(info.kind,"isolation"|"isolation_change"))else{return false;};
        let Some((id,_))=info.owner.clone()else{return false;};
        if key.code==KeyCode::Esc{self.isolation_confirmation=None;self.chip_info=None;return true;}
        if self.groups[self.active_group].active_id()!=Some(id.as_str()){self.notice="Session changed; reopen isolation controls".into();return true;}
        if info.kind=="isolation" {
            let target=match key.code{KeyCode::Char('1')=>Some(doxa_isolation::Profile::DockerOffline),KeyCode::Char('2')=>Some(doxa_isolation::Profile::DockerOpen),KeyCode::Char('3')=>Some(doxa_isolation::Profile::Native),_=>None};
            if let Some(target)=target{self.review_isolation_change(id,target);return true;}return false;
        }
        if matches!(key.code,KeyCode::Up|KeyCode::Down|KeyCode::PageUp|KeyCode::PageDown){
            let info=self.chip_info.as_mut().unwrap();info.scroll=match key.code{KeyCode::Up=>info.scroll.saturating_sub(1),KeyCode::PageUp=>info.scroll.saturating_sub(8),KeyCode::Down=>info.scroll.saturating_add(1),_=>info.scroll.saturating_add(8)};return true;
        }
        if matches!(key.code,KeyCode::Char('Y')|KeyCode::Char('y'))&&key.modifiers.contains(KeyModifiers::SHIFT){
            let Some(review)=self.isolation_confirmation.as_ref().filter(|review|review.id==id&&review.complete.get())else{self.notice="Read the complete isolation and internet confirmation before applying".into();return true;};
            let profile=review.profile.clone();
            if self.active_remote(){self.notice="Change isolation on the session host".into();return true;}
            if !self.session_activity.get(&id).is_some_and(|(busy,queued)|!*busy&&*queued==0){self.notice="Isolation changes require an idle session with no queued prompts".into();return true;}
            self.pending_queue_commands.push(crate::bridge::WorkerCommand::SetIsolation(id,profile));self.isolation_confirmation=None;self.chip_info=None;
            self.notice="Applying reviewed isolation change; waiting for host verification…".into();
        }
        true
    }
    pub(super) fn dispatch_isolation_command(&mut self)->bool{
        let input=self.input.trim();
        let Some((name,args))=input.split_once(char::is_whitespace).or_else(||(input=="/isolation").then_some((input,"")))else{return false;};
        if name!="/isolation"{return false;}
        if self.active_remote(){self.notice="Isolation changes must be requested on the session host".into();return true;}
        let args:Vec<_>=args.split_whitespace().collect();
        if args.is_empty(){self.open_isolation_info(self.active_group);self.input.clear();return true;}
        if args.len()!=2||args[1]!="--confirm"{
            self.notice="Use /isolation native|docker-open|docker-offline --confirm; changes apply only while idle".into();return true;
        }
        let profile=match doxa_isolation::Profile::parse(args[0]){Ok(profile)=>profile,Err(error)=>{self.notice=error.to_string();return true;}};
        let Some(id)=self.groups[self.active_group].active_id().map(str::to_owned)else{return true;};
        if !self.session_activity.get(&id).is_some_and(|(busy,queued)|!*busy&&*queued==0){
            self.notice="Isolation changes require an idle session with no queued prompts".into();return true;
        }
        self.review_isolation_change(id,profile);
        self.input.clear();true
    }
    pub(super) fn apply_isolation_reply(&mut self,frame:&Value)->Option<bool>{
        if frame["type"]!="set_isolation_reply"{return None;}
        let id=frame["session_id"].as_str().filter(|id|crate::discovery::valid_id(id))?;
        if frame["ok"]==true{
            let Some(value)=verified_status(&frame["isolation"])else{return Some(false);};
            self.session_telemetry.entry(id.into()).or_default().isolation=Some(value);
            self.notice="Isolation policy verified".into();
        }else{self.notice=format!("Isolation change refused · {}",safe_label(frame["error"].as_str().unwrap_or("unknown error")));}
        Some(true)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn labels_come_from_verified_profiles_not_request_text(){
        let value=verified_status(&json!({"profile":"docker-open","label":"hardened","state":"ready"})).unwrap();
        assert_eq!(value["label"],"docker · open egress");
        assert!(verified_status(&json!({"profile":"hardened","state":"ready"})).is_none());
        assert!(verified_status(&json!({"profile":"native","state":"preparing"})).is_none());
    }
}
