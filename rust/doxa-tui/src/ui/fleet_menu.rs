//! Read-only fleet navigation. Every run and attach target is revalidated by
//! fleet_view, never inferred from a title or a synthesized session ID.
use std::{path::PathBuf, sync::mpsc::{self, Receiver}, time::{Duration, Instant}};
use crossterm::event::{KeyCode, KeyEvent};
use serde::{Serialize,Deserialize};
pub const MAX_SAVED_VIEWS:usize=32;
#[derive(Clone,Debug,PartialEq,Eq,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SavedView {#[serde(rename="kind")] kind:ViewKind,pub root:PathBuf,pub run_id:String}
#[derive(Clone,Debug,PartialEq,Eq,Serialize,Deserialize)]
#[serde(rename_all="snake_case")]
enum ViewKind{Fleet}
impl SavedView{
    pub fn valid(&self)->bool{self.root.is_absolute()&&self.root.to_str().is_some_and(|root|root.len()<=4096&&!root.chars().any(char::is_control))&&doxa_state::valid_session_id(&self.run_id)}
    pub fn parse(value:&serde_json::Value)->Option<Vec<Self>>{
        let rows:Vec<Self>=serde_json::from_value(value.clone()).ok()?;
        if rows.len()>MAX_SAVED_VIEWS||rows.iter().any(|row|!row.valid())||rows.iter().enumerate().any(|(index,row)|rows[..index].contains(row)){None}else{Some(rows)}
    }
}
#[derive(Debug)]
struct Reply {text:String,ids:Vec<String>,run:Option<String>}

#[derive(Debug)]
pub struct Menu {
    root: PathBuf, run: Option<String>, rows: Vec<String>, selected: usize,
    pub lines: Vec<String>, receiver: Option<Receiver<Result<Reply,String>>>,
    refreshed: Instant,
    verified:bool,
    fixture:bool,
}
impl Menu {
    pub fn new(root: PathBuf, run: Option<String>) -> Self {
        let mut menu = Self {root,run,rows:Vec::new(),selected:0,lines:vec!["Loading fleet manifests…".into()],receiver:None,refreshed:Instant::now(),verified:false,fixture:false};
        menu.refresh(); menu
    }
    /// A render-only status fixture. It cannot poll a filesystem or persist a fake run.
    #[doc(hidden)]
    pub fn from_fixture(root:PathBuf,id:&str,lines:Vec<String>)->Self{
        Self{root,run:Some(id.into()),rows:Vec::new(),selected:0,lines,receiver:None,refreshed:Instant::now(),verified:false,fixture:true}
    }
    fn refresh(&mut self) {
        if self.fixture{return;}
        if self.receiver.is_some() {return;}
        let root=self.root.clone(); let run=self.run.clone(); let(tx,rx)=mpsc::channel();
        self.receiver=Some(rx); self.refreshed=Instant::now();
        std::thread::spawn(move || {
            let result=(||->std::io::Result<Reply>{
                let ids=crate::fleet_view::run_ids(&root)?;
                if let Some(query)=run{
                    let exact:Vec<_>=ids.iter().filter(|id|**id==query).collect();
                    let matched:Vec<_>=if exact.is_empty(){ids.iter().filter(|id|id.starts_with(&query)).collect()}else{exact};
                    if matched.len()!=1{return Err(std::io::Error::new(std::io::ErrorKind::NotFound,"Fleet run missing or ambiguous"));}
                    let id=matched[0].clone();let mut text=crate::fleet_view::status(&root,&id)?;
                    if let Ok(snapshot)=crate::fleet_control::snapshot(&root,&id){
                        if snapshot["native_version"]==1{
                            let phase=snapshot["phase"].as_str().unwrap_or("unknown");
                            let mut actual:Vec<_>=text.lines().map(str::to_owned).collect();
                            if let Some(first)=actual.first_mut(){*first=format!("fleet {} — {}",super::safe_label(&id),super::safe_label(phase));}
                            text=actual.join("\n");
                            text.push_str(&format!("\nNative controller phase: {}",super::safe_label(phase)));
                            if snapshot["supervision"].is_object(){
                                let guard=&snapshot["supervision"];let review=&guard["context"]["review"];
                                for(label,value)in [("Independent supervisor",&review["supervisor"]),("Alignment",&guard["status"]),("Paused",&guard["paused"]),("Pause reason",&guard["reason"]),("Fast judge",&review["message_judge"]),("Message review",&review["message_mode"]),("Review reservation USD (estimate)",&guard["review_reserved_usd"]),("Review usage USD (estimated from tokens)",&guard["review_estimated_usd"]),("Review calls",&guard["calls"]),("Charter hash",&guard["context"]["charter_sha256"])]{if !value.is_null(){text.push_str(&format!("\n{label}: {}",super::safe_label(&value.to_string())));}}
                                if guard["paused"]==true{if let Some(hash)=guard["context"]["charter_sha256"].as_str(){text.push_str(&format!("\nHuman recovery: /fleet continue {id} {hash}"));}}
                            }
                            for(label,value)in [("Total budget USD",&snapshot["spec"]["run_budget_usd"]),("Approval policy",&snapshot["approvals"]["policy"]),("Approvals asked",&snapshot["approvals"]["asked"]),("Auto approved",&snapshot["approvals"]["auto_approved"]),("Refused",&snapshot["approvals"]["refused"]) ]{if !value.is_null(){text.push_str(&format!("\n{label}: {}",super::safe_label(&value.to_string())));}}
                        }
                    }
                    Ok(Reply{text,ids:Vec::new(),run:Some(id)})
                }else{Ok(Reply{text:format!("Runs under {}\nRecorded run IDs:",root.display()),ids,run:None})}
            })();let _=tx.send(result.map_err(|e|e.to_string()));
        });
    }
    pub fn poll(&mut self)->bool {
        if self.fixture{return false;}
        if let Some(result)=self.receiver.as_ref().and_then(|rx|rx.try_recv().ok()) {
            self.receiver=None;
            match result {
                Ok(reply)=>{self.rows=reply.ids;self.run=reply.run;self.verified=self.run.is_some();
                    self.selected=self.selected.min(self.rows.len().saturating_sub(1));
                    self.lines=vec!["Fleet · ↑/↓ select · Enter open · B runs · R refresh · Esc close".into()];
                    self.lines.extend(reply.text.lines().map(str::to_owned));
                    if let Some(run)=&self.run {self.lines.push(format!("Attach a slot: /fleet attach {run} <index>"));}
                    else{self.lines.extend(self.rows.iter().cloned());if self.rows.is_empty(){self.lines.push("No recorded runs".into());}}
                },Err(error)=>{self.verified=false;self.rows.clear();self.lines=vec![format!("Fleet: {}",super::safe_label(&error))];}
            }
            return true;
        }
        if self.refreshed.elapsed()>=Duration::from_secs(1){self.refresh();}
        false
    }
    pub fn key(&mut self,key:KeyEvent){match key.code{
        KeyCode::Up=>self.selected=self.selected.saturating_sub(1),
        KeyCode::Down=>self.selected=(self.selected+1).min(self.rows.len().saturating_sub(1)),
        KeyCode::Enter=>{if let Some(run)=self.rows.get(self.selected).cloned(){self.run=Some(run);self.verified=false;self.receiver=None;self.refresh();}},
        KeyCode::Char('b')=>{self.run=None;self.verified=false;self.receiver=None;self.refresh();},
        KeyCode::Char('r')=>self.refresh(),_=>{}}
    }
    pub fn verified_view(&self)->Option<SavedView>{self.run.as_ref().filter(|_|self.verified).map(|id|SavedView{kind:ViewKind::Fleet,root:self.root.clone(),run_id:id.clone()})}
    pub fn hover(&mut self,row:usize)->bool{let Some(index)=row.checked_sub(3) else{return false;};if self.run.is_none() && index<self.rows.len(){self.selected=index;true}else{false}}
    pub fn display(&self)->Vec<String>{let mut lines=self.lines.clone();if self.run.is_none(){if let Some(line)=lines.get_mut(self.selected+3){line.insert_str(0,"▸ ");}}lines}
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn hover_rejects_headers_and_rows_outside_real_run_choices() {
        let mut menu=Menu {root:PathBuf::from("/unused"),run:None,rows:vec!["real-run".into()],selected:0,lines:Vec::new(),receiver:None,refreshed:Instant::now(),verified:false,fixture:false};
        assert!(!menu.hover(0));assert!(!menu.hover(2));assert!(menu.hover(3));assert!(!menu.hover(4));
        menu.run=Some("real-run".into());assert!(!menu.hover(3));
    }
}
