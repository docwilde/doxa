//! Read-only fleet sidebar membership from private, identity-checked manifests.
use super::{App, Session, RailGroupKey, RailRow, fleet_menu, panes};
use crate::{discovery, fleet_control};
use serde_json::Value;
use std::{collections::{HashMap,HashSet}, path::PathBuf, sync::mpsc::{self, Receiver, TryRecvError}, time::{Duration, Instant}};

#[derive(Clone, Debug)]
pub(super) struct Group {
    pub root: PathBuf, pub id: String, pub supervisor: Option<String>,
    pub members: Vec<String>, pub collapsed: bool,
}
#[derive(Debug)]
pub(super) struct Cache {
    fixture: bool,
    pub groups: Vec<Group>, sources: Vec<(PathBuf,String)>, next: Instant,
    pending: Option<Receiver<(Vec<Group>,Vec<discovery::Session>)>>,
}
impl Group {
    fn heading(&self) -> String {format!("Fleet · {} · {}",self.id,super::safe_label(&self.root.to_string_lossy()))}
}
impl Default for Cache {
    fn default() -> Self { Self { fixture:false,groups:Vec::new(),sources:Vec::new(),next:Instant::now(),pending:None } }
}
fn group(root: PathBuf, id: String, value: &Value, live: &HashSet<String>) -> Option<Group> {
    if value["native_version"] != 1 || value["run_id"] != id { return None; }
    let slots=value["slots"].as_array().filter(|rows|rows.len()<=crate::fleet_view::MAX_NATIVE_SLOTS)?;
    let mut members=Vec::new();let mut supervisor=None;let mut seen=HashSet::new();
    for slot in slots {
        let session=slot["session_id"].as_str().filter(|id|discovery::valid_id(id))?;
        if !seen.insert(session) { return None; }
        match slot["role"].as_str()? {
            "supervisor" => { if supervisor.is_some() { return None; } supervisor=Some(session.to_owned()); },
            "worker" => {}, _ => return None,
        }
        if live.contains(session) { members.push(session.to_owned()); }
    }
    if members.is_empty() { return None; }
    supervisor=supervisor.filter(|id|members.contains(id));
    Some(Group {root,id,supervisor,members,collapsed:false})
}
impl App {
    /// Render-only illustrative membership; never reads or persists a run.
    #[doc(hidden)]
    pub fn show_fleet_sidebar_fixture(&mut self, id: &str, supervisor: &str, workers: &[&str]) {
        let members=std::iter::once(supervisor).chain(workers.iter().copied()).map(str::to_owned).collect();
        self.fleet_rail.fixture=true;
        self.fleet_rail.groups=vec![Group {root:PathBuf::from("/demo/fleet"),id:id.into(),supervisor:Some(supervisor.into()),members,collapsed:false}];
    }
    pub(super) fn poll_fleet_rail(&mut self, now: Instant) -> bool {
        if self.remote_mode || self.fleet_rail.fixture { return false; }
        if let Some(receiver)=self.fleet_rail.pending.as_ref() {
            let (mut groups,live)=match receiver.try_recv() {
                Ok(rows)=>rows, Err(TryRecvError::Empty)=>return false,
                Err(TryRecvError::Disconnected)=>{self.fleet_rail.pending=None;return false;},
            };
            self.fleet_rail.pending=None;
            let selected=self.rail_order().get(self.rail_selected).map(|index|self.sessions[*index].id.clone());
            for group in &mut groups {
                group.collapsed=self.fleet_rail.groups.iter().find(|old|old.root==group.root && old.id==group.id).is_some_and(|old|old.collapsed);
                for id in &group.members {
                    if self.sessions.iter().any(|session| &session.id==id) {continue;}
                    if self.sessions.len()>=panes::MAX_TABS || self.killed_this_run.contains(id) {continue;}
                    if let Some(row)=live.iter().find(|row|&row.id==id) {
                        self.sessions.push(Session {id:id.clone(),title:row.title.clone(),collection:row.scope_key.clone(),transcript:String::new(),status:"Disconnected".into()});
                    }
                }
            }
            self.fleet_rail.groups=groups;
            if let Some(id)=selected {self.rail_selected=self.rail_order().iter().position(|index|self.sessions[*index].id==id).unwrap_or(0);}
            return true;
        }
        let mut sources=self.fleet_views.iter().map(|view|(view.root.clone(),view.run_id.clone())).collect::<Vec<_>>();
        if let Some(controller)=&self.fleet_controller {sources.push((controller.root.clone(),controller.id.clone()));}
        sources.sort();sources.dedup();sources.truncate(fleet_menu::MAX_SAVED_VIEWS);
        if sources.is_empty() {if self.fleet_rail.groups.is_empty(){return false;}self.fleet_rail.groups.clear();return true;}
        if now<self.fleet_rail.next && sources==self.fleet_rail.sources {return false;}
        self.fleet_rail.sources=sources.clone();self.fleet_rail.next=now+Duration::from_secs(3);
        if cfg!(test) {return false;}
        let (tx,rx)=mpsc::sync_channel(1);
        if std::thread::Builder::new().name("fleet-sidebar".into()).spawn(move|| {
            let Ok(live)=discovery::sessions() else {let _=tx.send((Vec::new(),Vec::new()));return;};
            let ids=live.iter().map(|row|row.id.clone()).collect();
            let groups=sources.into_iter().filter_map(|(root,id)|fleet_control::snapshot(&root,&id).ok().and_then(|value|group(root,id,&value,&ids))).collect();
            let _=tx.send((groups,live));
        }).is_ok() {self.fleet_rail.pending=Some(rx);}
        false
    }
    pub(super) fn fleet_rail_groups(&self, seen: &mut HashSet<usize>) -> Vec<(RailGroupKey,Vec<RailRow>,u8)> {
        let mut result=Vec::new();
        let indices=self.sessions.iter().enumerate().map(|(index,session)|(session.id.as_str(),index)).collect::<HashMap<_,_>>();
        for (fleet_index, group) in self.fleet_rail.groups.iter().enumerate() {
            let members=group.members.iter().filter_map(|id|indices.get(id.as_str()).copied())
                .filter(|index|!self.offline_ids.contains(&self.sessions[*index].id) && self.rail_session_visible(*index) && !seen.contains(index)).collect::<Vec<_>>();
            if members.is_empty() {continue;}
            seen.extend(&members);
            let parent=group.supervisor.as_ref().and_then(|id|members.iter().find(|index|&self.sessions[**index].id==id)).copied();
            let mut rows=vec![RailRow::FleetHeading(fleet_index)];
            // Retain the supervisor as the keyboard anchor while workers are
            // folded, so Left/Right can collapse and expand the same group.
            if let Some(parent)=parent { rows.push(RailRow::Session(parent)); }
            if !group.collapsed {
                rows.extend(members.iter().copied().filter(|index|Some(*index)!=parent).map(RailRow::Session));
            }
            let rank=members.iter().map(|index|self.rail_urgency(*index)).max().unwrap_or(0);
            result.push((RailGroupKey::Fleet(group.root.clone(),group.id.clone()),rows,rank));
        }
        result
    }
    pub(super) fn fleet_rail_prefix(&self, id: &str) -> String {
        match self.fleet_rail.groups.iter().find(|group|group.members.iter().any(|member|member==id)) {
            Some(group) if group.supervisor.as_deref()==Some(id)=>
                if self.preferences.on("nerd_font") {"  \u{f508} ".into()} else {"  [S] ".into()},
            Some(_)=>"    ↳ ".into(), None=>String::new(),
        }
    }
    pub(super) fn collapse_fleet_group(&mut self, index: usize) -> bool {
        let Some(group)=self.fleet_rail.groups.get_mut(index) else {return false;};
        group.collapsed=!group.collapsed;
        self.rail_selected=self.rail_selected.min(self.rail_order().len().saturating_sub(1));
        true
    }
    pub(super) fn collapse_fleet_parent(&mut self, id: &str, collapsed: Option<bool>) -> bool {
        let Some(group)=self.fleet_rail.groups.iter_mut().find(|group|group.supervisor.as_deref()==Some(id)) else{return false;};
        group.collapsed=collapsed.unwrap_or(!group.collapsed);
        self.rail_selected=self.rail_order().iter().position(|index|self.sessions[*index].id==id).unwrap_or(0);true
    }
    pub(super) fn open_fleet_heading(&mut self, heading: &str) -> bool {
        let Some(group)=self.fleet_rail.groups.iter().find(|group|heading==group.heading()).cloned() else{return false;};
        self.open_fleet(group.root,Some(group.id));true
    }
}

#[cfg(test)]
mod tests {
    use super::*; use serde_json::json;
    #[test]
    fn membership_requires_manifest_identity_roles_and_current_live_ids() {
        let live=HashSet::from(["boss".into(),"worker".into()]);
        let manifest=json!({"native_version":1,"run_id":"run","slots":[{"role":"worker","session_id":"dead"},{"role":"worker","session_id":"worker"},{"role":"supervisor","session_id":"boss"}]});
        let fleet=group(PathBuf::from("/private"),"run".into(),&manifest,&live).unwrap();
        assert_eq!(fleet.supervisor.as_deref(),Some("boss"));assert_eq!(fleet.members,["worker","boss"]);
        let mut wrong=manifest.clone();wrong["run_id"]=json!("different");assert!(group(PathBuf::from("/private"),"run".into(),&wrong,&live).is_none());
        let mut wrong=manifest;wrong["slots"][0]["session_id"]=json!("worker");assert!(group(PathBuf::from("/private"),"run".into(),&wrong,&live).is_none());
        let mut app=App::default();app.sessions=vec![Session {id:"worker".into(),title:"worker".into(),collection:"repo".into(),transcript:String::new(),status:String::new()},Session {id:"boss".into(),title:"boss".into(),collection:"repo".into(),transcript:String::new(),status:String::new()}];
        app.fleet_rail.groups=vec![fleet];
        assert_eq!(app.rail_order(),[1,0]);assert_eq!(app.fleet_rail_prefix("worker"),"    ↳ ");
        assert!(app.collapse_fleet_parent("boss",Some(true)));assert_eq!(app.rail_order(),[1]);
        assert!(app.collapse_fleet_parent("boss",Some(false)));assert_eq!(app.rail_order(),[1,0]);
    }
}
