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

fn live_groups(sources: &[(PathBuf,String)]) -> (Vec<Group>,Vec<discovery::Session>) {
    let mut groups=Vec::new(); let mut live=HashMap::new();
    for (root,id) in sources {
        let Ok(value)=fleet_control::snapshot(root,id) else {continue;};
        // Fleet daemons live in their own reviewed run runtime, not the
        // operator's ordinary session registry. Never guess across runtimes.
        let Ok(sessions)=discovery::sessions_in(&root.join(id).join("rt")) else {continue;};
        let ids=sessions.iter().map(|session|session.id.clone()).collect();
        if let Some(group)=group(root.clone(),id.clone(),&value,&ids) {
            for session in sessions.into_iter().filter(|session|group.members.contains(&session.id)) {
                live.entry(session.id.clone()).or_insert(session);
            }
            groups.push(group);
        }
    }
    (groups,live.into_values().collect())
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
                    if !self.killed_this_run.contains(id) { self.offline_ids.remove(id); }
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
            let _=tx.send(live_groups(&sources));
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
    pub(super) fn verified_fleet_member(&self, id: &str) -> bool {
        self.fleet_rail.groups.iter().any(|group|group.members.iter().any(|member|member==id))
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
    fn ordinary_registry_cannot_archive_members_verified_in_a_fleet_runtime() {
        let mut app=App::default();
        for id in ["boss","ordinary"] {
            app.sessions.push(Session {id:id.into(),title:id.into(),collection:String::new(),
                transcript:String::new(),status:"Disconnected".into()});
        }
        app.show_fleet_sidebar_fixture("run","boss",&[]);
        assert!(app.mark_dead_detached(&HashSet::new()));
        assert!(app.offline_ids.contains("ordinary")); assert!(!app.offline_ids.contains("boss"));
        app.fleet_rail.groups.clear();
        assert!(app.mark_dead_detached(&HashSet::new())); assert!(app.offline_ids.contains("boss"));
        // A fresh, validated private-runtime snapshot repairs an earlier
        // ordinary-roster result, but never revives an explicitly stopped ID.
        app.fleet_rail.fixture=false;
        let snapshot=|| Group {root:PathBuf::from("/owned/fleet"),id:"run".into(),
            supervisor:Some("boss".into()),members:vec!["boss".into()],collapsed:false};
        let (tx,rx)=mpsc::sync_channel(1);tx.send((vec![snapshot()],Vec::new())).unwrap();
        app.fleet_rail.pending=Some(rx);assert!(app.poll_fleet_rail(Instant::now()));
        assert!(!app.offline_ids.contains("boss"));
        app.offline_ids.insert("boss".into());app.killed_this_run.insert("boss".into());
        let (tx,rx)=mpsc::sync_channel(1);tx.send((vec![snapshot()],Vec::new())).unwrap();
        app.fleet_rail.pending=Some(rx);app.poll_fleet_rail(Instant::now());
        assert!(app.offline_ids.contains("boss"));
    }
    #[test]
    fn selecting_a_fleet_row_queues_its_private_runtime_without_a_blank_tab() {
        let mut app=App::default();
        app.sessions.push(Session {id:"boss".into(),title:"Supervisor".into(),collection:String::new(),
            transcript:String::new(),status:"Disconnected".into()});
        app.show_fleet_sidebar_fixture("run","boss",&[]);
        app.rail_selected=0;app.open_selected();
        assert!(app.groups.iter().all(|group|group.tabs.is_empty()));
        assert_eq!(app.pending_attaches,[("boss".into(),0,Some(PathBuf::from("/demo/fleet/run/rt")))]);
        let (tx,rx)=std::sync::mpsc::sync_channel(1);
        super::super::terminal_loop::dispatch_attaches(&mut app,&tx);
        assert!(matches!(rx.recv().unwrap(),crate::bridge::WorkerCommand::AttachRuntime(id,0,path)
            if id=="boss" && path==PathBuf::from("/demo/fleet/run/rt")));
    }
    #[test]
    fn live_membership_uses_each_verified_fleets_private_runtime() {
        use std::{fs,os::unix::{fs::PermissionsExt,net::UnixListener}};
        let dir=tempfile::tempdir().unwrap(); let root=dir.path();
        fs::set_permissions(root,fs::Permissions::from_mode(0o700)).unwrap();
        let mut listeners=Vec::new();
        for (run,id) in [("one","boss"),("two","outside")] {
            let runtime=root.join(run).join("rt");
            fs::create_dir_all(runtime.join("registry")).unwrap();
            for path in [root.join(run),runtime.clone(),runtime.join("registry")] {
                fs::set_permissions(path,fs::Permissions::from_mode(0o700)).unwrap();
            }
            let manifest=json!({"native_version":1,"run_id":run,"slots":[{"role":"supervisor","session_id":"boss"}]});
            let path=root.join(run).join("manifest.json");fs::write(&path,manifest.to_string()).unwrap();
            fs::set_permissions(path,fs::Permissions::from_mode(0o600)).unwrap();
            let socket=runtime.join(format!("daemon-{id}-{}.sock",std::process::id()));
            listeners.push(UnixListener::bind(&socket).unwrap());
            fs::set_permissions(&socket,fs::Permissions::from_mode(0o600)).unwrap();
            let path=runtime.join("registry").join(format!("{id}.json"));
            fs::write(&path,json!({"session_id":id,"pid":std::process::id(),"cwd":root,
                "heartbeat_at":time::OffsetDateTime::now_utc().format(&time::format_description::well_known::Rfc3339).unwrap(),
                "started_at":"2026-10-19T00:00:00Z","title":id,"daemon_socket":socket}).to_string()).unwrap();
            fs::set_permissions(path,fs::Permissions::from_mode(0o600)).unwrap();
        }
        let (groups,live)=live_groups(&[(root.to_path_buf(),"one".into()),(root.to_path_buf(),"two".into())]);
        assert_eq!(groups.len(),1); assert_eq!(groups[0].id,"one");
        assert_eq!(groups[0].members,["boss"]); assert_eq!(live.len(),1); assert_eq!(live[0].id,"boss");
        // The same live ID in another run must not make a dead slot visible.
        fs::remove_file(root.join("one/rt/registry/boss.json")).unwrap();
        assert!(live_groups(&[(root.to_path_buf(),"two".into())]).0.is_empty());
    }
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
