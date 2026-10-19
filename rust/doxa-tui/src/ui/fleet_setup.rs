//! Menu-only fleet creation; the existing read-through review owns launch.
use super::{App,ChipInfo,fleet_process,safe_label};
use crossterm::event::{KeyCode,KeyEvent};
use std::{io,path::PathBuf};
const LABELS:[&str;14]=["Task","Directory","Worker engine","Worker model (blank = default)","Workers","Coordinator engine","Coordinator model","Worker isolation","Coordinator isolation","Total budget USD","Alignment supervisor","Message review","Message judge","Review budget USD"];
#[derive(Debug)]
pub(super) struct Wizard{values:[String;14],selected:usize,editing:bool}
impl Wizard{
    fn new(cwd:PathBuf)->Self{Self{values:[String::new(),cwd.to_string_lossy().into_owned(),"glm".into(),String::new(),"3".into(),"glm".into(),String::new(),"docker-offline".into(),"docker-offline".into(),"5".into(),"off".into(),"off".into(),"jev:jev-1.13.0".into(),"0.5".into()],selected:0,editing:false}}
    fn choices(&self)->Option<&'static [&'static str]>{match self.selected{2=>Some(&["glm","deepseek","claude","codex"]),5=>Some(&["off","glm","deepseek","claude","codex"]),7|8=>Some(&["docker-offline","docker-open","native"]),10=>Some(&["off","jev:jev-1.13.0","subq:subq-snap-preview"]),11=>Some(&["off","shadow","enforce"]),_=>None}}
    fn cycle(&mut self,back:bool){if let Some(options)=self.choices(){let i=options.iter().position(|row|*row==self.values[self.selected]).unwrap_or(0);self.values[self.selected]=options[(i+if back{options.len()-1}else{1})%options.len()].into();}}
    fn lines(&self)->Vec<String>{
        let mut lines=vec!["New fleet · ↑↓ field · ←→ choice · Enter edit · F10 review · Esc".into(),
            "Defaults: Docker offline. Native and internet require owner confirmation.".into(),
            "Offline GLM/DeepSeek work. Offline Claude/Codex CLI needs a provider gateway.".into()];
        for (index,label) in LABELS.iter().enumerate(){lines.push(format!("{} {label}: {}{}",if self.selected==index{"▸"}else{" "},safe_label(&self.values[index]),if self.selected==index&&self.editing{" ▏"}else{""}));}
        lines.push("An online coordinator can answer offline workers' research questions. Alignment reviewers are separate read-only API calls with no tools.".into());lines
    }
    fn args(&self)->io::Result<Vec<String>>{
        if self.values[0].trim().is_empty(){return Err(io::Error::other("Enter the approved task"));}
        let model=|engine:&str,model:&str|if model.is_empty(){engine.into()}else{format!("{engine}:{model}")};
        let mut args=vec!["--pool".into(),model(&self.values[2],&self.values[3]),"--prompt".into(),self.values[0].clone(),"--cwd".into(),self.values[1].clone(),"-n".into(),self.values[4].clone(),"--run-budget".into(),self.values[9].clone(),"--worker-isolation".into(),self.values[7].clone(),"--supervisor-isolation".into(),self.values[8].clone()];
        if self.values[5]!="off"{args.extend(["--supervisor".into(),model(&self.values[5],&self.values[6])]);}
        args.extend(["--alignment-supervisor".into(),self.values[10].clone(),"--message-review".into(),self.values[11].clone()]);
        if self.values[11]!="off"{args.extend(["--message-judge".into(),self.values[12].clone()]);}
        if self.values[10]!="off"||self.values[11]!="off"{args.extend(["--review-budget".into(),self.values[13].clone()]);}
        Ok(args)
    }
}
impl App{
    pub(super) fn open_fleet_setup(&mut self){
        if self.active_remote(){self.notice="Create a fleet on its local host".into();return;}
        if self.fleet_controller.is_some(){self.notice="Detach or stop this window's fleet controller before creating another".into();return;}
        let cwd=self.groups[self.active_group].active_id().and_then(|id|self.session_cwds.get(id)).cloned().or_else(||std::env::current_dir().ok()).unwrap_or_else(||PathBuf::from("."));
        let wizard=Wizard::new(cwd);let lines=wizard.lines();self.fleet_menu=None;
        self.chip_info=Some(ChipInfo{kind:"fleet_setup",label:String::new(),lines,scroll:0,owner:None});self.fleet_setup=Some(wizard);
    }
    pub(super) fn fleet_setup_key(&mut self,key:KeyEvent)->bool{
        let Some(wizard)=self.fleet_setup.as_mut()else{return false;};
        if key.code==KeyCode::Esc{if wizard.editing{wizard.editing=false;}else{self.fleet_setup=None;self.chip_info=None;return true;}}
        else if wizard.editing{match key.code{KeyCode::Enter=>wizard.editing=false,KeyCode::Backspace=>{wizard.values[wizard.selected].pop();},KeyCode::Char(c) if !c.is_control()&&wizard.values[wizard.selected].len()<16*1024=>wizard.values[wizard.selected].push(c),_=>{}}}
        else{match key.code{
            KeyCode::Up=>wizard.selected=wizard.selected.saturating_sub(1),KeyCode::Down=>wizard.selected=(wizard.selected+1).min(LABELS.len()-1),
            KeyCode::Left=>wizard.cycle(true),KeyCode::Right|KeyCode::Char(' ')=>wizard.cycle(false),
            KeyCode::Enter=>if wizard.choices().is_some(){wizard.cycle(false)}else{wizard.editing=true},
            KeyCode::F(10)=>{
                let prepared=wizard.args().and_then(|args|fleet_process::Prepared::start(args,None));
                match prepared{Ok(prepared)=>{let lines=prepared.lines.clone();self.fleet_review=Some(prepared);self.fleet_setup=None;self.chip_info=Some(ChipInfo{kind:"fleet_review",label:String::new(),lines,scroll:0,owner:None});},Err(error)=>self.notice=safe_label(&error.to_string())}
                return true;
            },_=>{}
        }}
        if let Some(wizard)=&self.fleet_setup{
            let lines=wizard.lines();let selected=wizard.selected;let area=self.active_chooser_rect();
            if let Some(info)=self.chip_info.as_mut(){info.lines=lines;if let Some(area)=area{let row=info.lines.iter().take(3+selected).flat_map(|line|crate::memory_menu::wrap_review(line,usize::from(area.width.saturating_sub(3)).max(1))).count();info.scroll=row.saturating_sub(usize::from(area.height.saturating_sub(2))/2);}}
        }true
    }
}
#[cfg(test)]mod tests{
    use super::*;
    #[test]fn wizard_keeps_role_defaults_offline_and_never_adds_access_acknowledgements(){let mut wizard=Wizard::new(std::env::current_dir().unwrap());wizard.values[0]="Review code".into();let args=wizard.args().unwrap();let review=crate::fleet_control::Spec::parse(&args).unwrap().review().unwrap();assert_eq!(review["access"]["worker_isolation"],"docker-offline");assert_eq!(review["access"]["supervisor_isolation"],"docker-offline");assert!(!args.iter().any(|value|value.starts_with("--allow-")||value.starts_with("--confirm-")));}
}
