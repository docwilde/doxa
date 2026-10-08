//! Isolation presentation accepts only host-asserted profiles. User requests
//! never change a chip until the daemon verifies and reports the new policy.
use super::{App, ChipInfo, PaneGroup, safe_label};
use serde_json::{json, Value};
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
        format!("{} · engine: {engine} · network: {network} · click for limits, mounts and idle change commands",
            value["label"].as_str().unwrap_or("unavailable"))
    }
    pub(super) fn open_isolation_info(&mut self,group:usize){
        let Some(id)=self.groups.get(group).and_then(PaneGroup::active_id).map(str::to_owned)else{return;};
        let Some(value)=self.session_telemetry.get(&id).and_then(|t|t.isolation.as_ref())else{return;};
        let mut lines=vec![format!("Isolation: {}",value["label"].as_str().unwrap_or("unavailable")),
            format!("Engine: {}",value["engine"].as_str().unwrap_or("unknown")),
            format!("Network: {}",value["network"].as_str().unwrap_or("unknown"))];
        if value["profile"]!="native"{
            lines.push(format!("Memory: {} MiB · CPU: {} · PIDs: {}",value["memory_bytes"].as_u64().unwrap_or(0)/1024/1024,value["cpus"],value["pids"]));
            lines.push(format!("Image: {}",value["image"].as_str().unwrap_or("unavailable")));
            lines.push("Mounts: independent Git checkout, private home/cache and session hook broker".into());
            lines.push("Root read-only; capabilities dropped; no-new-privileges; private PID/IPC; rootless Engine".into());
        }
        lines.push(format!("Disk: {}",value["disk_limit"].as_str().unwrap_or("unknown")));
        lines.push(format!("Credentials: {}",value["credential_exposure"].as_str().unwrap_or("unknown")));
        lines.extend([String::new(),"While idle, explicitly change isolation with:".into(),
            "/isolation docker-offline --confirm".into(),"/isolation docker-open --confirm".into(),
            "/isolation native --confirm".into(),
            "Provider, approvals and conversation identity remain attached.".into(),
            "Backend changes checkpoint the stopped provider, copy the workspace and verify same-session resume.".into(),
            "Native resume retains the private clone; failed migration restores the original profile.".into()]);
        self.chip_info=Some(ChipInfo{kind:"isolation",label:String::new(),lines,scroll:0,owner:None});
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
        self.pending_queue_commands.push(crate::bridge::WorkerCommand::SetIsolation(id,profile.key().into()));
        self.input.clear();self.chip_info=None;
        self.notice=format!("Verifying isolation change to {}…",profile.label());true
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
