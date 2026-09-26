//! Read-only fleet navigation. Every run and attach target is revalidated by
//! fleet_view, never inferred from a title or a synthesized session ID.
use std::{path::PathBuf, sync::mpsc::{self, Receiver}, time::{Duration, Instant}};
use crossterm::event::{KeyCode, KeyEvent};
#[derive(Debug)]
pub struct Menu {
    root: PathBuf, run: Option<String>, rows: Vec<String>, selected: usize,
    pub lines: Vec<String>, receiver: Option<Receiver<Result<String,String>>>,
    refreshed: Instant,
}
impl Menu {
    pub fn new(root: PathBuf, run: Option<String>) -> Self {
        let mut menu = Self {root,run,rows:Vec::new(),selected:0,lines:vec!["Loading fleet manifests…".into()],receiver:None,refreshed:Instant::now()};
        menu.refresh(); menu
    }
    fn refresh(&mut self) {
        if self.receiver.is_some() {return;}
        let root=self.root.clone(); let run=self.run.clone(); let(tx,rx)=mpsc::channel();
        self.receiver=Some(rx); self.refreshed=Instant::now();
        std::thread::spawn(move || {let result=match run {Some(run)=>crate::fleet_view::status(&root,&run),None=>crate::fleet_view::runs(&root)};
            let _=tx.send(result.map_err(|e|e.to_string()));});
    }
    pub fn poll(&mut self)->bool {
        if let Some(result)=self.receiver.as_ref().and_then(|rx|rx.try_recv().ok()) {
            self.receiver=None;
            match result {
                Ok(text)=>{self.rows=if self.run.is_none(){text.lines().skip(2).filter_map(|line|line.split_whitespace().next()).filter(|id|doxa_state::valid_session_id(id)).map(str::to_owned).collect()}else{Vec::new()};
                    self.selected=self.selected.min(self.rows.len().saturating_sub(1));
                    self.lines=vec!["Fleet · ↑/↓ select · Enter open · B runs · R refresh · Esc close".into()];
                    self.lines.extend(text.lines().map(str::to_owned));
                    if let Some(run)=&self.run {self.lines.push(format!("Attach a slot: /fleet attach {run} <index>"));}
                },Err(error)=>{self.lines=vec![format!("Fleet: {}",super::safe_label(&error))];}
            }
            return true;
        }
        if self.refreshed.elapsed()>=Duration::from_secs(1){self.refresh();}
        false
    }
    pub fn key(&mut self,key:KeyEvent){match key.code{
        KeyCode::Up=>self.selected=self.selected.saturating_sub(1),
        KeyCode::Down=>self.selected=(self.selected+1).min(self.rows.len().saturating_sub(1)),
        KeyCode::Enter=>{if let Some(run)=self.rows.get(self.selected).cloned(){self.run=Some(run);self.receiver=None;self.refresh();}},
        KeyCode::Char('b')=>{self.run=None;self.receiver=None;self.refresh();},
        KeyCode::Char('r')=>self.refresh(),_=>{}}
    }
    pub fn hover(&mut self,row:usize)->bool{let Some(index)=row.checked_sub(3) else{return false;};if self.run.is_none() && index<self.rows.len(){self.selected=index;true}else{false}}
    pub fn display(&self)->Vec<String>{let mut lines=self.lines.clone();if self.run.is_none(){if let Some(line)=lines.get_mut(self.selected+3){line.insert_str(0,"▸ ");}}lines}
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn hover_rejects_headers_and_rows_outside_real_run_choices() {
        let mut menu=Menu {root:PathBuf::from("/unused"),run:None,rows:vec!["real-run".into()],selected:0,lines:Vec::new(),receiver:None,refreshed:Instant::now()};
        assert!(!menu.hover(0));assert!(!menu.hover(2));assert!(menu.hover(3));assert!(!menu.hover(4));
        menu.run=Some("real-run".into());assert!(!menu.hover(3));
    }
}
