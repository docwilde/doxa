//! Delayed read-only belief previews. Display replies never contain or grant
//! the exact review token required by mutation APIs.
use std::{path::PathBuf,sync::mpsc::{self,Receiver,TryRecvError},time::{Duration,Instant}};
use ratatui::{Frame,layout::Rect,style::Style,text::Line,widgets::{Block,Borders,Clear,Paragraph}};
use serde_json::Value;
use crate::theme;

#[derive(Clone,Debug,PartialEq,Eq)]
pub struct Owner {
    pub id:u64,pub pane:usize,pub session:Option<String>,pub cwd:String,pub query:String,pub offset:u16,
    pub rect:Rect,pub menu:Rect,pub subject:String,pub claim:String,pub truncated:bool,
}
#[derive(Clone,Debug,PartialEq,Eq)]
pub struct Display {pub subject:String,pub claim:String,pub complete:bool}
#[derive(Debug)]
struct Hover {owner:Owner,since:Instant,visible:bool,requested:bool,display:Option<Display>,failed:bool,
    pending:Option<Receiver<Result<Value,()>>>}
#[derive(Default,Debug)]
pub struct Preview {hover:Option<Hover>}

impl Preview {
    pub fn owner(&self)->Option<&Owner> {self.hover.as_ref().map(|hover|&hover.owner)}
    pub fn clear(&mut self)->bool {self.hover.take().is_some()}
    pub fn set_owner(&mut self,owner:Option<Owner>,now:Instant)->bool {
        if self.owner()==owner.as_ref() {return false;}
        self.hover=owner.map(|owner| {
            let needs_read=owner.truncated || owner.subject.len()>=4096 || owner.subject.contains("[redacted]") || owner.claim.contains("[redacted]");
            let display=(!needs_read).then(||Display {subject:owner.subject.clone(),claim:owner.claim.clone(),complete:true});
            Hover {owner,since:now,visible:false,requested:false,display,failed:false,pending:None}
        });true
    }
    pub fn tick(&mut self,now:Instant)->bool {
        let Some(hover)=&mut self.hover else {return false;};
        if !hover.visible && now.saturating_duration_since(hover.since)>=Duration::from_millis(500) {
            hover.visible=true;return true;
        }false
    }
    pub fn needs_read(&self)->bool {self.hover.as_ref().is_some_and(|hover|hover.visible&&!hover.requested&&hover.display.is_none())}
    pub fn read(&mut self,_python:PathBuf) {
        let Some(hover)=self.hover.as_mut().filter(|hover|hover.visible&&!hover.requested&&hover.display.is_none()) else{return;};
        hover.requested=true;
        let owner=hover.owner.clone();let(tx,rx)=mpsc::sync_channel(1);hover.pending=Some(rx);
        // Dropping a hover drops its receiver. Canonical reads enforce bounded
        // resource and lock deadlines inside the native client.
        std::thread::spawn(move||{
            let result=doxa_lore::LoreClient::open(Duration::from_secs(3))
                .and_then(|mut client|client.belief_display(&owner.cwd,owner.id)).map_err(|_|());
            let _=tx.send(result);
        });
    }
    pub fn poll(&mut self)->bool {
        let Some(hover)=&mut self.hover else{return false;};
        let Some(rx)=&hover.pending else{return false;};
        let reply=match rx.try_recv(){Ok(reply)=>reply,Err(TryRecvError::Empty)=>return false,Err(TryRecvError::Disconnected)=>Err(())};
        hover.pending=None;
        let owner=hover.owner.clone();self.accept(&owner,reply);true
    }
    fn accept(&mut self,owner:&Owner,reply:Result<Value,()>)->bool {
        let Some(hover)=self.hover.as_mut().filter(|hover|&hover.owner==owner) else{return false;};
        hover.display=reply.ok().and_then(|value|parse_display(owner.id,&value));
        hover.failed=hover.display.is_none();true
    }
    pub fn render(&self,frame:&mut Frame,area:Rect) {
        let Some(hover)=self.hover.as_ref().filter(|hover|hover.visible) else{return;};
        let display=hover.display.clone().unwrap_or_else(||Display {subject:if hover.failed {"Full preview unavailable"}else{"Loading full belief…"}.into(),claim:String::new(),complete:false});
        let Some(plan)=plan(&hover.owner,&display,area) else{return;};
        frame.render_widget(Clear,plan.area);
        frame.render_widget(Paragraph::new(plan.lines).block(Block::default().title(if plan.full {" Full belief "}else{" Belief preview "})
            .borders(Borders::ALL).border_style(Style::default().fg(theme::ACCENT)))
            .style(Style::default().fg(theme::TEXT).bg(theme::RAISED)),plan.area);
    }
}
fn parse_display(id:u64,value:&Value)->Option<Display> {
    if value["id"].as_u64()!=Some(id) {return None;}
    let subject=value["subject"].as_str()?.to_owned();let claim=value["claim"].as_str()?.to_owned();
    if subject.len()>4096 || subject.len()+claim.len()>64*1024 {return None;}
    Some(Display {subject,claim,complete:value["complete"].as_bool()?&&!value["redacted"].as_bool()?})
}
struct Plan {area:Rect,lines:Vec<Line<'static>>,full:bool}
fn plan(owner:&Owner,display:&Display,area:Rect)->Option<Plan> {
    let width=area.width.saturating_sub(4).min(110);
    let above=owner.rect.y.saturating_sub(area.y+1);
    let below=area.bottom().saturating_sub(owner.rect.bottom()+1);
    let available=above.max(below);
    if width<20 || available<5 {return None;}
    let body_width=usize::from(width.saturating_sub(2));
    let text=format!("Subject: {}\n\n{}",display.subject,display.claim);
    let mut rows=Vec::new();
    for line in text.split('\n') {
        let mut safe=String::new();
        for ch in line.chars() {
            if ch.is_control() || matches!(ch,'\u{200e}'|'\u{200f}'|'\u{202a}'..='\u{202e}'|'\u{2066}'..='\u{2069}') {safe.extend(ch.escape_debug());}
            else{safe.push(ch);}
        }
        rows.extend(crate::memory_menu::wrap_review(&safe,body_width));
    }
    let full=display.complete && rows.len()+2<=usize::from(available);
    let reserve=if full {2}else{3};
    rows.truncate(usize::from(available.saturating_sub(reserve)));
    if !full {rows.push(if body_width<39 {"Enter: full review"}else if display.complete {"Preview continues · Enter: full review"}else{"Incomplete display · Enter: full review"}.into());}
    let height=(rows.len()+2).min(usize::from(available)) as u16;
    let y=if above>=below {owner.rect.y.saturating_sub(height+1)}else{owner.rect.bottom()+1};
    let x=owner.rect.x.min(area.right().saturating_sub(width+1)).max(area.x+1);
    Some(Plan {area:Rect::new(x,y,width,height),lines:rows.into_iter().map(Line::from).collect(),full})
}

#[cfg(test)]
mod tests {
    use super::*;
    fn owner()->Owner {Owner {id:7,pane:0,session:Some("s".into()),cwd:"/fixture".into(),query:String::new(),offset:0,
        rect:Rect::new(2,25,96,1),menu:Rect::new(1,23,98,8),subject:"user".into(),claim:"First line\nSecond line".into(),truncated:false}}
    #[test]
    fn dwell_resets_on_scope_geometry_and_content_and_never_uses_stale_reply() {
        let now=Instant::now();let mut preview=Preview::default();let one=owner();
        assert!(preview.set_owner(Some(one.clone()),now));
        assert!(!preview.tick(now+Duration::from_millis(499)));
        assert!(preview.tick(now+Duration::from_millis(500)));
        assert!(!preview.tick(now+Duration::from_secs(1)));
        for changed in [Owner {id:8,..one.clone()},Owner {cwd:"/other".into(),..one.clone()},Owner {query:"filter".into(),..one.clone()},
            Owner {rect:Rect::new(2,26,96,1),..one.clone()},Owner {claim:"new claim".into(),..one.clone()}] {
            preview.set_owner(Some(changed),now);assert!(!preview.hover.as_ref().unwrap().visible);
            assert!(!preview.accept(&one,Ok(serde_json::json!({"id":7,"subject":"user","claim":"stale","complete":true,"redacted":false}))));
        }
        preview.clear();assert!(preview.owner().is_none());
    }
    #[test]
    fn multiline_full_display_and_long_claim_have_truthful_fit_affordance() {
        let one=owner();let display=Display {subject:one.subject.clone(),claim:one.claim.clone(),complete:true};
        let full=plan(&one,&display,Rect::new(0,0,100,40)).unwrap();
        assert!(full.full);assert!(full.lines.iter().any(|line|line.spans.iter().any(|span|span.content=="Second line")));
        let long=Display {claim:"界 safe long text ".repeat(3000),..display.clone()};
        let limited=plan(&one,&long,Rect::new(0,0,100,40)).unwrap();
        assert!(!limited.full);
        assert!(limited.lines.last().unwrap().spans[0].content.contains("Enter: full review"));
        let incomplete=Display {complete:false,..display};
        assert!(!plan(&one,&incomplete,Rect::new(0,0,100,40)).unwrap().full);
    }
    #[test]
    fn full_display_reply_is_bounded_and_separate_from_review_permissions() {
        assert!(parse_display(7,&serde_json::json!({"id":8,"subject":"user","claim":"wrong","complete":true,"redacted":false})).is_none());
        assert!(parse_display(7,&serde_json::json!({"id":7,"subject":"user","claim":"x".repeat(65536),"complete":true,"redacted":false})).is_none());
        let redacted=parse_display(7,&serde_json::json!({"id":7,"subject":"user","claim":"[redacted]","complete":true,"redacted":true})).unwrap();
        assert!(!redacted.complete);
        let mut preview=Preview::default();let mut one=owner();one.truncated=true;
        let now=Instant::now();preview.set_owner(Some(one.clone()),now);preview.tick(now+Duration::from_millis(500));
        assert!(preview.needs_read());
        assert!(preview.accept(&one,Ok(serde_json::json!({"id":7,"subject":"user","claim":"Full\nclaim","complete":true,"redacted":false}))));
        assert_eq!(preview.hover.as_ref().unwrap().display.as_ref().unwrap().claim,"Full\nclaim");
    }
}
