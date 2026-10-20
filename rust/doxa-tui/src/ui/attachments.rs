//! Attachments stay with one pane/session draft until explicit submission.
use super::{App, Focus, safe_label};
use crate::clipboard::Target;
use crossterm::event::{KeyCode,KeyEvent,KeyModifiers};
use crossterm::event::{MouseEvent,MouseEventKind,MouseButton};
use doxa_input::Attachment;
use ratatui::{Frame,layout::Rect,style::{Style,Modifier},widgets::{Block,Borders,Clear,Paragraph}};
use std::{path::PathBuf,sync::mpsc};

#[derive(Debug)]
pub(super) struct Picker {pub(super) owner:(usize,String),pub path:String,selected:usize,list:bool}
#[derive(Debug)]
pub(super) struct FileJob {target:Target,rx:mpsc::Receiver<std::io::Result<Attachment>>}
impl App {
    fn attachment_rect(area:Rect)->Option<Rect>{
        let width=area.width.saturating_sub(4).min(90);let height=area.height.saturating_sub(2).min(17);
        (width>=20&&height>=5).then(||Rect::new(area.x+(area.width-width)/2,area.y+(area.height-height)/2,width,height))
    }
    pub(super) fn attachment_mouse(&mut self,mouse:MouseEvent)->bool{
        if self.attachment_picker.is_none(){return false;}
        let Some(rect)=Self::attachment_rect(self.size)else{return true;};
        if mouse.kind!=MouseEventKind::Down(MouseButton::Left)||!rect.contains(ratatui::layout::Position::new(mouse.column,mouse.row)){return true;}
        let x=mouse.column.saturating_sub(rect.x+1);let y=mouse.row.saturating_sub(rect.y+1);
        if rect.height>=13&&y==rect.height.saturating_sub(4){
            if x<7{self.attachment_picker.as_mut().unwrap().list=false;return self.attachment_key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE));}
            if x<18{self.attachment_picker.as_mut().unwrap().list=true;return self.attachment_key(KeyEvent::new(KeyCode::Delete,KeyModifiers::NONE));}
            self.attachment_picker=None;return true;
        }
        let picker=self.attachment_picker.as_mut().unwrap();
        if y==3{picker.list=false;}else if y>=5{let index=usize::from(y-5);if index<self.prompt_attachments.get(&picker.owner).map_or(0,Vec::len){picker.list=true;picker.selected=index;}}
        true
    }
    pub(super) fn open_attachments(&mut self){
        let owner=self.prompt_owner();
        if owner.1.is_empty()||self.active_remote()||self.offline_ids.contains(&owner.1)||self.active_request_index().is_some(){self.notice="Attachments require an attached local session prompt".into();return;}
        self.attachment_picker=Some(Picker{owner,path:String::new(),selected:0,list:false});self.focus=Focus::Prompt;
        self.notice="Enter a local image or UTF-8 file path · Tab manages attached files".into();
    }
    pub(super) fn attachment_key(&mut self,key:KeyEvent)->bool{
        let Some(picker)=self.attachment_picker.as_mut()else{return false;};
        match key.code {
            KeyCode::Esc=>{self.attachment_picker=None;},
            KeyCode::Tab=>picker.list=!picker.list,
            KeyCode::Up=>picker.selected=picker.selected.saturating_sub(1),
            KeyCode::Down=>picker.selected=(picker.selected+1).min(self.prompt_attachments.get(&picker.owner).map_or(0,|v|v.len().saturating_sub(1))),
            KeyCode::Delete|KeyCode::Backspace if picker.list=>{if let Some(values)=self.prompt_attachments.get_mut(&picker.owner){if picker.selected<values.len(){values.remove(picker.selected);picker.selected=picker.selected.min(values.len().saturating_sub(1));}}},
            KeyCode::Backspace=>{picker.path.pop();},
            KeyCode::Char('v'|'V') if key.modifiers.contains(KeyModifiers::CONTROL)=>self.notice="Paste a path using terminal Ctrl+Shift+V".into(),
            KeyCode::Char(c) if !key.modifiers.intersects(KeyModifiers::CONTROL|KeyModifiers::ALT)&&!super::unsafe_input_char(c)=>{if picker.path.len()+c.len_utf8()<=4096{picker.path.push(c);}},
            KeyCode::Enter if !picker.list&&!picker.path.trim().is_empty()=>{
                if self.attachment_file_job.is_some(){self.notice="An attachment is still loading".into();return true;}
                let text=picker.path.trim();let text=text.strip_prefix('"').and_then(|s|s.strip_suffix('"')).or_else(||text.strip_prefix('\'').and_then(|s|s.strip_suffix('\''))).unwrap_or(text);
                let mut path=if let Some(rest)=text.strip_prefix("~/"){std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default().join(rest)}else{PathBuf::from(text)};
                if path.is_relative(){path=self.session_cwds.get(&picker.owner.1).map(PathBuf::from).unwrap_or_else(||PathBuf::from(".")).join(path);}
                let target=self.clipboard_target();let(tx,rx)=mpsc::sync_channel(1);
                std::thread::spawn(move||{let _=tx.send(Attachment::load(&path));});
                self.attachment_file_job=Some(FileJob{target,rx});self.notice="Loading attachment…".into();
            }
            _=>{},
        }true
    }
    pub(super) fn attachment_paste(&mut self,text:&str)->bool{
        let Some(picker)=self.attachment_picker.as_mut()else{return false;};
        if !picker.list {for c in text.chars().filter(|c|!super::unsafe_input_char(*c)){if picker.path.len()+c.len_utf8()>4096{break;}picker.path.push(c);}}true
    }
    fn accept_attachment(&mut self,target:Target,value:Attachment)->bool{
        let owner=(target.pane,target.session.clone());
        let exists=self.groups.get(target.pane).is_some_and(|g|g.tabs.contains(&target.session))
            &&!self.input_requests.iter().any(|r|r.session_id==target.session)
            &&!self.offline_ids.contains(&target.session);
        let unchanged=if self.prompt_owner()==owner{self.input==target.draft&&self.input_cursor==target.cursor}else{self.input_drafts.get(&owner).is_some_and(|(draft,cursor)|*draft==target.draft&&*cursor==target.cursor)};
        if !exists||!unchanged{self.notice="Attachment discarded · original prompt changed or closed".into();return true;}
        if value.is_image()&&self.session_identity.get(&target.session).and_then(|p|p.0.as_deref()).is_some_and(|engine|!matches!(engine,"claude"|"codex")){
            self.notice="Image attachments currently require Claude or Codex".into();return true;
        }
        let values=self.prompt_attachments.entry(owner).or_default();
        if values.len()>=doxa_input::MAX_COUNT||values.iter().map(|v|v.bytes.len()).sum::<usize>()+value.bytes.len()>doxa_input::MAX_TOTAL{self.notice="Attachment limit: 4 files and 4 MiB per prompt".into();return true;}
        values.push(value);self.notice="Attachment added to original draft · Alt+A reviews or removes files".into();true
    }
    pub(super) fn poll_attachment_jobs(&mut self)->bool{
        let mut changed=false;
        if let Some(result)=self.attachment_file_job.as_ref().and_then(|job|job.rx.try_recv().ok()){
            let target=self.attachment_file_job.take().unwrap().target;
            match result{Ok(value)=>{self.accept_attachment(target,value);if let Some(picker)=&mut self.attachment_picker{picker.path.clear();}},Err(error)=>self.notice=format!("Attachment unavailable: {}",safe_label(&error.to_string()))};changed=true;
        }
        if let Some(result)=self.clipboard_image_job.as_ref().and_then(crate::clipboard::ImageJob::poll){
            let target=self.clipboard_image_job.take().unwrap().target.clone();
            match result {
                Ok(Some(value))=>{self.accept_attachment(target,value);},
                Ok(None)=>match crate::clipboard::Job::start(target){Ok(job)=>self.clipboard_job=Some(job),Err(_)=>self.notice="Clipboard reader unavailable · use terminal Ctrl+Shift+V".into()},
                Err(error)=>self.notice=format!("Clipboard image unavailable: {}",safe_label(&error.to_string())),
            }changed=true;
        }changed
    }
    pub(super) fn draw_attachments(&self,frame:&mut Frame,area:Rect){
        let Some(picker)=&self.attachment_picker else{return;};
        let Some(rect)=Self::attachment_rect(area)else{return;};
        let mut lines=vec!["Local image (PNG/JPEG/GIF/WebP) or UTF-8 file".to_owned(),"Enter adds · Tab switches to files · Delete removes · Esc closes".into(),String::new(),format!("{} Path: {}",if picker.list{" "}else{">"},safe_label(&picker.path)),String::new()];
        if let Some(values)=self.prompt_attachments.get(&picker.owner){for(index,value)in values.iter().enumerate(){lines.push(format!("{} {} · {} KiB",if picker.list&&picker.selected==index{">"}else{" "},safe_label(&value.name),(value.bytes.len()+1023)/1024));}}
        if lines.len()==5{lines.push("No attachments yet".into());}
        frame.render_widget(Clear,rect);frame.render_widget(Paragraph::new(lines.join("\n")).style(Style::default().fg(crate::theme::TEXT).bg(crate::theme::RAISED)).block(Block::default().title(" Attach files · Alt+A ").borders(Borders::ALL).border_style(Style::default().fg(crate::theme::ACCENT).add_modifier(Modifier::BOLD))),rect);
        if rect.height>=13{
            frame.render_widget(Paragraph::new("[Add]  [Remove]   [Done]").style(Style::default().fg(crate::theme::ACCENT)),Rect::new(rect.x+1,rect.bottom()-3,rect.width-2,1));
            frame.render_widget(Paragraph::new(safe_label(&self.notice)).style(Style::default().fg(crate::theme::SECONDARY)),Rect::new(rect.x+1,rect.bottom()-2,rect.width-2,1));
        }
    }
}

#[cfg(test)]mod tests {
    use super::*;
    use crossterm::event::Event;
    use crate::{bridge::WorkerCommand,worker_frames::{WorkerFrame,PromptDelivery}};
    fn app()->App{let mut app=App::default();app.groups[0].tabs=vec!["a".into()];app.groups[1].tabs=vec!["b".into()];app.input="look".into();app.input_cursor=4;app}
    fn image()->Attachment{let mut png=std::io::Cursor::new(Vec::new());image::DynamicImage::new_rgb8(2,2).write_to(&mut png,image::ImageFormat::Png).unwrap();Attachment::from_source("clipboard.png",png.into_inner()).unwrap()}
    #[test]fn pasted_image_stays_on_original_prompt_and_stale_results_do_not_attach(){
        let mut app=app();let target=app.clipboard_target();app.clipboard_image_job=Some(crate::clipboard::ImageJob::fixture(target,Ok(Some(image()))));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab,KeyModifiers::ALT)));assert_eq!(app.active_group,1);app.input="other".into();app.input_cursor=5;
        assert!(app.poll_clipboard());assert_eq!(app.input,"other");assert_eq!(app.prompt_attachments[&(0,"a".into())].len(),1);assert!(app.pending_prompts.is_empty());
        let target=app.clipboard_target();app.clipboard_image_job=Some(crate::clipboard::ImageJob::fixture(target,Ok(Some(image()))));app.input.push('!');app.input_cursor+=1;app.poll_clipboard();assert!(!app.prompt_attachments.contains_key(&(1,"b".into())));assert!(app.notice.contains("discarded"));
    }
    #[test]fn attachments_menu_preserves_text_and_removes_without_submitting(){
        let mut app=app();app.size=Rect::new(0,0,110,30);app.prompt_attachments.insert((0,"a".into()),vec![image()]);app.open_attachments();
        app.paste("/file with spaces.png");assert_eq!(app.attachment_picker.as_ref().unwrap().path,"/file with spaces.png");assert_eq!(app.input,"look");
        let mut term=ratatui::Terminal::new(ratatui::backend::TestBackend::new(110,30)).unwrap();term.draw(|f|app.draw(f)).unwrap();let text=term.backend().buffer().content.iter().map(|c|c.symbol()).collect::<String>();assert!(text.contains("clipboard.png"));assert!(text.contains("Attach files"));
        app.attachment_key(KeyEvent::new(KeyCode::Tab,KeyModifiers::NONE));app.attachment_key(KeyEvent::new(KeyCode::Delete,KeyModifiers::NONE));assert!(app.prompt_attachments[&(0,"a".into())].is_empty());assert!(app.pending_prompts.is_empty());
        app.attachment_key(KeyEvent::new(KeyCode::Esc,KeyModifiers::NONE));assert!(app.attachment_picker.is_none());assert_eq!(app.input,"look");
    }
    #[test]fn attachment_submission_backpressure_keeps_order_and_recovers_original_bytes(){
        let mut app=app();app.pending_prompts.push(("a".into(),"first".into()));app.prompt_attachments.insert((0,"a".into()),vec![image()]);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE)));assert_eq!(app.pending_prompts.len(),2);assert_eq!(app.pending_prompt_attachments[&1].len(),1);assert!(app.prompt_attachments.is_empty());
        let(tx,rx)=mpsc::sync_channel(1);super::super::terminal_loop::dispatch_prompts(&mut app,&tx);assert_eq!(app.pending_prompts.len(),1);assert_eq!(app.pending_prompt_attachments[&0].len(),1);
        assert!(matches!(rx.recv().unwrap(),WorkerCommand::Prompt(_,text)if text=="first"));super::super::terminal_loop::dispatch_prompts(&mut app,&tx);
        let WorkerCommand::PromptAttachments(id,text,attachments)=rx.recv().unwrap()else{panic!("image bytes lost")};let expected=attachments[0].bytes.clone();
        app.apply_worker_frame(WorkerFrame::AttachmentFailed{session_id:id,text,attachments,message:"disconnected".into(),delivery:PromptDelivery::Uncertain});assert!(app.notice.contains("unconfirmed"));assert!(app.pending_prompts.is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Up,KeyModifiers::ALT)));assert_eq!(app.input,"look");assert_eq!(app.prompt_attachments[&(0,"a".into())][0].bytes,expected);assert!(app.pending_prompts.is_empty());
    }
}

#[cfg(test)]mod offline_tests {
    use super::*;
    use crossterm::event::Event;
    #[test]fn archive_resume_does_not_drop_pending_attachment_content(){
        let mut app=App::default();app.groups[0].tabs=vec!["saved".into()];app.offline_ids.insert("saved".into());app.input="look".into();app.input_cursor=4;
        app.prompt_attachments.insert((0,"saved".into()),vec![Attachment::from_source("source.txt",b"text".to_vec()).unwrap()]);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE)));
        assert_eq!(app.input,"look");assert_eq!(app.prompt_attachments[&(0,"saved".into())].len(),1);assert!(app.pending_launches.is_empty());assert!(app.pending_prompts.is_empty());
    }
}

#[cfg(test)]mod pane_tests {
    use super::*;
    use crossterm::event::Event;
    #[test]fn attach_chip_in_an_inactive_pane_opens_that_panes_draft_on_first_click(){
        let mut app=App::default();app.size=Rect::new(0,0,520,40);app.rail_visible=false;
        for id in ["a","b"]{app.apply_daemon_frame(&serde_json::json!({"type":"hello","session_id":id,"engine":"codex","model":"fixture"}));}
        app.groups[0].tabs=vec!["a".into()];app.groups[1].tabs=vec!["b".into()];app.active_group=0;app.split=super::super::Split::Vertical;app.split_requested=true;
        app.input="left draft".into();app.input_cursor=10;app.input_drafts.insert((1,"b".into()),("right draft".into(),11));
        let mut terminal=ratatui::Terminal::new(ratatui::backend::TestBackend::new(520,40)).unwrap();terminal.draw(|frame|app.draw(frame)).unwrap();
        let panes=app.layout(app.size).panes.unwrap();let strip=app.pane_regions(1,panes[1])[3];let chips=app.chip_window(1,usize::from(strip.width));
        assert!(chips.iter().any(|(kind,_)|*kind=="attachments"));
        let offset=chips.iter().take_while(|(kind,_)|*kind!="attachments").map(|(kind,label)|unicode_width::UnicodeWidthStr::width(super::super::chip_text(kind,label).as_str())as u16+1).sum::<u16>();
        app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Down(MouseButton::Left),column:strip.x+offset+1,row:strip.y,modifiers:KeyModifiers::NONE}));
        assert_eq!(app.active_group,1);assert_eq!(app.attachment_picker.as_ref().unwrap().owner,(1,"b".into()));assert_eq!(app.input,"right draft");assert_eq!(app.input_drafts[&(0,"a".into())].0,"left draft");assert!(app.pending_prompts.is_empty());
    }
}
