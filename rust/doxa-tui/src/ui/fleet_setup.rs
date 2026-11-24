//! Menu-only fleet creation; the existing read-through review owns launch.
use super::{App,ChipInfo,fleet_process,safe_label};
use crossterm::event::{KeyCode,KeyEvent};
use std::{io,path::PathBuf};
const LABELS:[&str;15]=["Task","Directory","Worker engine","Worker model","Workers","Coordinator engine","Coordinator model","Worker isolation","Coordinator isolation","Total budget USD","Alignment supervisor","Message review","Message judge","Review budget USD","Router config"];
#[derive(Debug)]
pub(super) struct Wizard{values:[String;15],selected:usize,editing:bool}
impl Wizard{
    fn new(cwd:PathBuf)->Self{Self{values:[String::new(),cwd.to_string_lossy().into_owned(),"glm".into(),String::new(),"3".into(),"glm".into(),String::new(),"docker-offline".into(),"docker-offline".into(),"5".into(),"off".into(),"off".into(),"jev:jev-1.13.0".into(),"0.5".into(),String::new()],selected:0,editing:false}}
    fn choices(&self)->Option<&'static [&'static str]>{match self.selected{2=>Some(&["glm","deepseek","claude","codex","router"]),5=>Some(&["off","glm","deepseek","claude","codex","router"]),7|8=>Some(&["docker-offline","docker-open","native"]),10=>Some(&["off","jev:jev-1.13.0","subq:subq-snap-1.2"]),11=>Some(&["off","shadow","enforce"]),_=>None}}
    fn cycle(&mut self,back:bool){if let Some(options)=self.choices(){let i=options.iter().position(|row|*row==self.values[self.selected]).unwrap_or(0);self.values[self.selected]=options[(i+if back{options.len()-1}else{1})%options.len()].into();}}
    fn lines(&self)->Vec<String>{
        let mut lines=vec!["New fleet · ↑↓ field · ←→ preset · Enter edit · PgUp/PgDn scroll · F10 review · Esc".into(),
            "Defaults: Docker offline. Native and internet require owner confirmation.".into(),
            "Router uses exact configured target IDs in the model fields. Review its private config below.".into()];
        for (index,label) in LABELS.iter().enumerate(){
            let value = if index == 0 { crate::markdown::sanitize(&self.values[index]).replace('\n', " ") }
                else if self.values[index].is_empty() && !(self.selected == index && self.editing) {
                    match index {
                        3 | 6 => "Provider default".into(),
                        14 => "From Settings".into(),
                        _ => String::new(),
                    }
                } else { safe_label(&self.values[index]) };
            lines.push(format!("{} {label}: {}{}",if self.selected==index{"▸"}else{" "},value,if self.selected==index&&self.editing{" ▏"}else{""}));
        }
        lines.push("An online coordinator can answer offline workers' research questions. Alignment reviewers are separate read-only API calls with no tools.".into());
        lines.push("Enter edits the exact PROVIDER:MODEL for the alignment supervisor. A Router config path must be absolute.".into());
        lines
    }
    fn args(&self)->io::Result<Vec<String>>{
        if self.values[0].trim().is_empty(){return Err(io::Error::other("Enter the approved task"));}
        let model=|engine:&str,model:&str|if model.is_empty(){engine.into()}else{format!("{engine}:{model}")};
        let mut args=vec!["--pool".into(),model(&self.values[2],&self.values[3]),"--prompt".into(),self.values[0].clone(),"--cwd".into(),self.values[1].clone(),"-n".into(),self.values[4].clone(),"--run-budget".into(),self.values[9].clone(),"--worker-isolation".into(),self.values[7].clone(),"--supervisor-isolation".into(),self.values[8].clone()];
        if self.values[5]!="off"{args.extend(["--supervisor".into(),model(&self.values[5],&self.values[6])]);}
        if (self.values[2]=="router"||self.values[5]=="router")&&!self.values[14].is_empty(){args.extend(["--router-config".into(),self.values[14].clone()]);}
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
        if self.fleet_setup.is_some() && matches!(key.code, KeyCode::PageUp | KeyCode::PageDown) {
            return self.report_scroll(key.code);
        }
        let Some(wizard)=self.fleet_setup.as_mut()else{return false;};
        if key.code==KeyCode::Esc{if wizard.editing{wizard.editing=false;}else{self.fleet_setup=None;self.chip_info=None;return true;}}
        else if wizard.editing{match key.code{KeyCode::Enter=>wizard.editing=false,KeyCode::Backspace=>{wizard.values[wizard.selected].pop();},KeyCode::Char(c) if !c.is_control()&&wizard.values[wizard.selected].len()<16*1024=>wizard.values[wizard.selected].push(c),_=>{}}}
        else{match key.code{
            KeyCode::Up=>wizard.selected=wizard.selected.saturating_sub(1),KeyCode::Down=>wizard.selected=(wizard.selected+1).min(LABELS.len()-1),
            KeyCode::Left=>wizard.cycle(true),KeyCode::Right|KeyCode::Char(' ')=>wizard.cycle(false),
            KeyCode::Enter=>if wizard.selected==10{wizard.editing=true}else if wizard.choices().is_some(){wizard.cycle(false)}else{wizard.editing=true},
            KeyCode::F(10)=>{
                let prepared=wizard.args().and_then(|args|fleet_process::Prepared::start(args,None));
                match prepared{Ok(prepared)=>{let lines=prepared.lines.clone();self.fleet_review=Some(prepared);self.fleet_setup=None;self.chip_info=Some(ChipInfo{kind:"fleet_review",label:String::new(),lines,scroll:0,owner:None});},Err(error)=>self.notice=safe_label(&error.to_string())}
                return true;
            },_=>{}
        }}
        if let Some(wizard) = &self.fleet_setup {
            let selected = wizard.selected;
            let editing = wizard.editing;
            self.chip_info.as_mut().unwrap().lines = wizard.lines();
            if let Some(scroll) = self.active_chooser_rect().and_then(|area| self.report_sheet(area))
                .map(|report| report.reveal(3 + selected, editing))
            {
                self.chip_info.as_mut().unwrap().scroll = scroll;
            }
        }
        true
    }
}
#[cfg(test)]mod tests{
    use super::*;
    #[test]fn full_task_is_visible_and_scrollable_without_changing_submission(){
        use crossterm::event::KeyModifiers;
        let mut app=App::default();
        app.handle(crossterm::event::Event::Resize(100, 28));
        app.open_fleet_setup();
        let task=format!("{} visible ending 界", "Detail ".repeat(200));
        app.fleet_setup.as_mut().unwrap().values[0]=task.clone();
        app.fleet_setup_key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE));
        assert!(app.chip_info.as_ref().unwrap().lines[3].contains("visible ending 界"));
        let before=app.chip_info.as_ref().unwrap().scroll;
        app.fleet_setup_key(KeyEvent::new(KeyCode::PageDown,KeyModifiers::NONE));
        assert!(app.chip_info.as_ref().unwrap().scroll>before);
        app.fleet_setup_key(KeyEvent::new(KeyCode::PageUp,KeyModifiers::NONE));
        assert!(app.chip_info.as_ref().unwrap().scroll <= before);
        let args=app.fleet_setup.as_ref().unwrap().args().unwrap();
        assert!(args.windows(2).any(|pair|pair[0]=="--prompt"&&pair[1]==task));
    }
    #[test]fn wizard_keeps_role_defaults_offline_and_never_adds_access_acknowledgements(){let mut wizard=Wizard::new(std::env::current_dir().unwrap());wizard.values[0]="Review code".into();let args=wizard.args().unwrap();let review=crate::fleet_control::Spec::parse(&args).unwrap().review().unwrap();assert_eq!(review["access"]["worker_isolation"],"docker-offline");assert_eq!(review["access"]["supervisor_isolation"],"docker-offline");assert!(!args.iter().any(|value|value.starts_with("--allow-")||value.starts_with("--confirm-")));}
    #[test]fn wizard_router_roles_forward_exact_pins_and_explicit_config_to_existing_review(){
        let mut wizard=Wizard::new(std::env::current_dir().unwrap());
        wizard.values[0]="Review bounded handoff".into();wizard.values[2]="router".into();wizard.values[3]="glm-small".into();
        wizard.values[5]="router".into();wizard.values[6]="glm-small".into();wizard.values[14]="/owner/private/router.json".into();
        let args=wizard.args().unwrap();
        for (flag,value) in [("--pool","router:glm-small"),("--supervisor","router:glm-small"),("--router-config","/owner/private/router.json")]{
            assert!(args.windows(2).any(|pair|pair[0]==flag&&pair[1]==value));
        }
        assert!(!args.iter().any(|value|value.starts_with("--allow-")||value.starts_with("--confirm-")));
        wizard.values[14].clear();let inherited=wizard.args().unwrap();
        assert!(!inherited.iter().any(|value|value=="--router-config"));
    }
    #[test]fn wizard_engine_choices_include_router_for_workers_and_coordinator(){
        let mut wizard=Wizard::new(std::env::current_dir().unwrap());
        for field in [2,5]{wizard.selected=field;assert!(wizard.choices().unwrap().contains(&"router"));}
    }

    #[test]fn alignment_selector_enter_edits_an_exact_api_model_without_launching(){
        use crossterm::event::KeyModifiers;
        let mut app=App::default();app.open_fleet_setup();
        let wizard=app.fleet_setup.as_mut().unwrap();wizard.values[0]="Bounded review".into();wizard.selected=10;
        let press=|code|KeyEvent::new(code,KeyModifiers::NONE);
        app.fleet_setup_key(press(KeyCode::Enter));
        for _ in 0..3{app.fleet_setup_key(press(KeyCode::Backspace));}
        for c in "deepseek:deepseek-flash".chars(){app.fleet_setup_key(press(KeyCode::Char(c)));}
        app.fleet_setup_key(press(KeyCode::Enter));
        let wizard=app.fleet_setup.as_ref().unwrap();assert!(!wizard.editing);
        let review=crate::fleet_control::Spec::parse(&wizard.args().unwrap()).unwrap().review().unwrap();
        assert_eq!(review["independent_review"]["supervisor"]["provider"],"deepseek");
        assert_eq!(review["independent_review"]["supervisor"]["model"],"deepseek-flash");
        app.fleet_setup_key(press(KeyCode::Right));
        assert_eq!(app.fleet_setup.as_ref().unwrap().values[10],"jev:jev-1.13.0","arrows retain preset selection");
        assert!(app.fleet_controller.is_none());
    }

}
