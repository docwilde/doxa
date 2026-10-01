//! Native fleet command review and controller ownership. No shell, provider
//! output, or task text reaches the review/status display.
use std::{cell::Cell, collections::BTreeMap, fmt, io, path::{Path,PathBuf}, process::{Child,Command,Stdio}, sync::atomic::{AtomicU64,Ordering}, time::{SystemTime,UNIX_EPOCH}};
use std::os::unix::process::CommandExt;
use serde_json::{json,Value};
static NEXT_ID:AtomicU64=AtomicU64::new(0);
fn invalid(message:&str)->io::Error{io::Error::new(io::ErrorKind::InvalidInput,message)}

/// Small shell-word tokenizer; quotes group literal arguments, with no variable,
/// command, glob or shell expansion. Bounds also apply after unquoting.
pub fn words(text:&str)->io::Result<Vec<String>>{
    if text.len()>128*1024||text.chars().any(|c|c.is_control()){return Err(invalid("Fleet command exceeds bounds or contains control characters"));}
    let mut out=Vec::new();let mut word=String::new();let mut quote=None;let mut escaped=false;let mut started=false;
    for c in text.chars(){
        if escaped{word.push(c);escaped=false;started=true;continue;}
        if c=='\\' && quote!=Some('\''){escaped=true;started=true;continue;}
        if let Some(q)=quote{if c==q{quote=None;}else{word.push(c);}continue;}
        if c=='\''||c=='"'{quote=Some(c);started=true;}
        else if c.is_whitespace(){if started{out.push(std::mem::take(&mut word));started=false;}}
        else{word.push(c);started=true;}
    }
    if escaped||quote.is_some(){return Err(invalid("Close fleet argument quotes or escaped character"));}
    if started{out.push(word);}
    if out.len()>512{return Err(invalid("Too many fleet arguments"));}Ok(out)
}

fn has_option(args:&[String],name:&str)->bool{let mut index=0;while index<args.len(){let key=args[index].as_str();if key==name{return true;}index+=if matches!(key,"--force"|"--allow-unbudgeted"|"--dry-run"){1}else{2};}false}

pub struct Prepared {
    pub root:PathBuf,pub id:String,pub lines:Vec<String>,args:Vec<String>,resume_snapshot:Option<Value>,
    pub seen:Cell<usize>,pub complete:Cell<bool>,pub armed:bool,prompt_digest:Option<String>,lore_default:Option<bool>,
}
impl fmt::Debug for Prepared{fn fmt(&self,f:&mut fmt::Formatter<'_>)->fmt::Result{f.debug_struct("PreparedFleet").field("root",&self.root).field("id",&self.id).finish_non_exhaustive()}}
impl Prepared {
    /// Deterministic renderer fixture; the empty controller arguments prohibit launch.
    #[doc(hidden)]
    pub fn from_fixture_review(review:&Value)->io::Result<Self>{
        let root=PathBuf::from(review["root"].as_str().ok_or_else(||invalid("Fixture root missing"))?);
        let id=review["run_id"].as_str().filter(|id|doxa_state::valid_session_id(id)).ok_or_else(||invalid("Fixture run ID missing"))?.to_owned();
        Ok(Self{root,id,lines:review_lines("Start native fleet",review),args:Vec::new(),resume_snapshot:None,prompt_digest:None,lore_default:review["lore_enabled"].as_bool(),seen:Cell::new(0),complete:Cell::new(false),armed:false})
    }
    pub fn start(mut args:Vec<String>,cwd:Option<&Path>)->io::Result<Self>{
        if !has_option(&args,"--run-id"){
            let id=format!("ui-{}-{}-{}",SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs(),std::process::id(),NEXT_ID.fetch_add(1,Ordering::Relaxed));
            args.extend(["--run-id".into(),id]);
        }
        if !has_option(&args,"--cwd"){if let Some(cwd)=cwd{args.extend(["--cwd".into(),cwd.to_string_lossy().into_owned()]);}}
        let spec=crate::fleet_control::Spec::parse(&args).map_err(public_plan_error)?;let review=spec.review().map_err(public_plan_error)?;
        if review["review_version"]!=1||review["prompt_sha256"].as_str().is_none_or(|digest|digest.len()!=64||!digest.bytes().all(|byte|byte.is_ascii_hexdigit())){return Err(invalid("Fleet review contract unavailable"));}
        let root=PathBuf::from(review["root"].as_str().ok_or_else(||invalid("Fleet review root unavailable"))?);
        let id=review["run_id"].as_str().filter(|id|doxa_state::valid_session_id(id)).ok_or_else(||invalid("Fleet review run ID unavailable"))?.to_owned();
        let lines=review_lines("Start native fleet",&review);
        let mut command=vec!["start".into()];command.extend(args);
        Ok(Self{root,id,lines,args:command,resume_snapshot:None,prompt_digest:review["prompt_sha256"].as_str().map(str::to_owned),lore_default:review["lore_enabled"].as_bool(),seen:Cell::new(0),complete:Cell::new(false),armed:false})
    }
    pub fn resume(root:PathBuf,id:&str)->io::Result<Self>{
        let snapshot=crate::fleet_control::snapshot(&root,id)?;
        if snapshot["phase"]!="monitoring"||snapshot["live"]!=true{return Err(invalid("Only a native live monitoring run can resume"));}
        let review=json!({"run_id":id,"root":root,"cwd":snapshot["spec"]["cwd"],"mode":snapshot["mode"],"sessions":snapshot["spec"]["sessions"],"run_budget_usd":snapshot["spec"]["run_budget_usd"],"allow_unbudgeted":snapshot["spec"]["allow_unbudgeted"],"approval_policy":snapshot["approvals"]["policy"],"memory_off":snapshot["spec"]["memory_off"],"lore_enabled":snapshot["spec"]["lore_enabled"],"approval_grace_s":snapshot["approvals"]["grace_s"],"slots":snapshot["slots"]});
        Ok(Self{lines:review_lines("Resume native fleet",&review),root:root.clone(),id:id.into(),args:vec!["resume".into(),id.into(),"--root".into(),root.to_string_lossy().into_owned()],resume_snapshot:Some(snapshot),prompt_digest:None,lore_default:None,seen:Cell::new(0),complete:Cell::new(false),armed:false})
    }
    pub fn launch(self,exe:&Path)->io::Result<Controller>{
        if self.args.is_empty(){return Err(invalid("Gallery fixture cannot launch a controller"));}
        if !self.armed||!self.complete.get(){return Err(invalid("Read and explicitly confirm the complete fleet review"));}
        if let Some(snapshot)=&self.resume_snapshot{if &crate::fleet_control::snapshot(&self.root,&self.id)?!=snapshot{return Err(invalid("Fleet changed since review; review it again"));}}
        let mut command=Command::new(exe);command.arg("fleet").args(&self.args).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).process_group(0);
        if let Some(enabled)=self.lore_default{command.env("DOXA_LORE",if enabled{"1"}else{"0"});}
        if let Some(digest)=&self.prompt_digest{if digest.len()!=64||!digest.bytes().all(|byte|byte.is_ascii_hexdigit()){return Err(invalid("Fleet review task digest invalid"));}command.env("DOXA_FLEET_REVIEW_PROMPT_SHA256",digest);}
        let child=command.spawn()?;
        Ok(Controller{child:Some(child),root:self.root,id:self.id,cancelling:false})
    }
}
fn public_plan_error(error:io::Error)->io::Error{
    let text=error.to_string();let message=if text.contains("socket path budget"){"Fleet socket path too long; use a short absolute --root"}
        else if text.contains("budget"){"Fleet budget refused; specify a valid --run-budget or explicitly review --allow-unbudgeted"}
        else if text.contains("pool"){"Fleet pool refused; use --pool ENGINE:MODEL"}
        else if text.contains("prompt"){"Fleet task refused; check --prompt or bounded --prompt-file"}
        else if text.contains("memory"){"Fleet memory plan refused; reduce workers or review --force"}
        else if text.contains("socket"){"Fleet socket path too long; use a short absolute --root"}
        else{"Native fleet arguments or preflight refused; check options, worker count, budget and paths"};
    io::Error::new(error.kind(),message)
}
fn review_lines(title:&str,value:&Value)->Vec<String>{
    let mut lines=vec![format!("{title} · read all rows, Shift+A arm, Shift+Y launch · Esc cancel")];
    for (label,key)in [("Run","run_id"),("Root","root"),("Directory","cwd"),("Mode","mode"),("Workers","workers"),("Sessions","sessions"),("Seed","seed"),("Workers with memory off","memory_off"),("Memory default enabled","lore_enabled"),("Total budget USD","run_budget_usd"),("Allow unbudgeted","allow_unbudgeted"),("Approval policy","approval_policy"),("Approval grace seconds","approval_grace_s"),("Dry run","dry_run"),("Preflight","preflight"),("Task digest","prompt_sha256"),("Quiescence deadline seconds","quiescence_timeout_s"),("Quiescence grace seconds","quiescence_grace_s")]{
        if !value[key].is_null(){lines.push(format!("{label}: {}",value[key].as_str().map(str::to_owned).unwrap_or_else(||value[key].to_string())));}
    }
    let mut counts:BTreeMap<(String,String,String,String),usize>=BTreeMap::new();
    for slot in value["slots"].as_array().into_iter().flatten(){let key=(slot["role"].as_str().unwrap_or("worker").into(),slot["engine"].as_str().unwrap_or("unknown").into(),slot["model"].as_str().unwrap_or("provider default").into(),match slot["lore"].as_bool(){Some(true)=>"on",Some(false)=>"off",None=>"unknown"}.into());*counts.entry(key).or_default()+=1;}
    for ((role,engine,model,lore),count)in counts{lines.push(format!("Planned {count} × {role}: {engine}:{model} · memory {lore}"));}
    lines.push("Task text stays private; controller output is suppressed. Ctrl+C cancels the controller and waits for teardown.".into());
    lines.into_iter().map(|line|crate::markdown::sanitize(&line.replace('\n',"\\n").replace('\t',"\\t"))).collect()
}
#[derive(Debug)]
pub struct Controller {child:Option<Child>,pub root:PathBuf,pub id:String,pub cancelling:bool}
impl Controller{
    /// Detach only this window's owned controller. Its existing budgets and
    /// approval deadlines keep running; closing the TUI no longer cancels it.
    pub fn detach(mut self)->(PathBuf,String){
        if let Some(mut child)=self.child.take(){std::thread::spawn(move||{let _=child.wait();});}
        (self.root.clone(),self.id.clone())
    }
    pub fn cancel(&mut self){if !self.cancelling{if let Some(child)=&self.child{unsafe{libc::kill(child.id() as i32,libc::SIGINT);}}self.cancelling=true;}}
    pub fn poll(&mut self)->io::Result<Option<bool>>{let Some(child)=&mut self.child else{return Ok(Some(true));};match child.try_wait()?{Some(status)=>{self.child=None;Ok(Some(status.success()))},None=>Ok(None)}}
}
impl Drop for Controller{fn drop(&mut self){self.cancel();if let Some(mut child)=self.child.take(){let _=child.wait();}}}

#[cfg(test)]
mod tests{
    use super::*;
    #[test]
    fn review_paths_cannot_inject_rows_or_hide_long_suffixes(){
        let path=format!("/{}\nApproval policy: all\tTAIL", "x".repeat(300));
        let lines=review_lines("Review",&json!({"root":path}));
        assert_eq!(lines.len(),3);
        assert!(lines[1].ends_with("\\nApproval policy: all\\tTAIL"));
        assert!(!lines[1].contains('\n'));assert!(!lines[1].contains('\t'));
        assert!(lines[1].len()>300);
    }
    #[test]
    fn literal_tokenizer_never_expands_shell_code(){assert_eq!(words("start --prompt '$(touch nope) `$HOME`' --cwd \"a b\"").unwrap(),vec!["start","--prompt","$(touch nope) `$HOME`","--cwd","a b"]);assert!(words("'unfinished").is_err());assert!(words("x\n--force").is_err());}
    #[test]
    fn public_review_omits_prompt_and_private_slot_requests(){let lines=review_lines("Review",&json!({"prompt":"secret","slots":[{"role":"worker","engine":"fixture","model":"fixture-v1","pending_asks":[{"input_summary":"PRIVATE-SLOT-SENTINEL"}]}]})).join("\n");assert!(!lines.contains("secret"));assert!(!lines.contains("PRIVATE-SLOT-SENTINEL"));assert!(lines.contains("1 × worker: fixture:fixture-v1"));}
    fn fixture_child()->Child{
        Command::new("python3").args(["-c","import signal,time,sys; signal.signal(signal.SIGINT,lambda *_:sys.exit(0)); time.sleep(10)"])
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).process_group(0).spawn().unwrap()
    }
    #[test]
    fn controller_cancel_and_drop_reap_fixture_process(){
        let child=fixture_child();let pid=child.id();let mut controller=Controller{child:Some(child),root:PathBuf::from("/unused"),id:"fixture-run".into(),cancelling:false};
        std::thread::sleep(std::time::Duration::from_millis(100));controller.cancel();
        let deadline=std::time::Instant::now()+std::time::Duration::from_secs(3);
        loop{if controller.poll().unwrap().is_some(){break;}assert!(std::time::Instant::now()<deadline);std::thread::sleep(std::time::Duration::from_millis(10));}
        assert!(controller.child.is_none());assert_eq!(unsafe{libc::kill(pid as i32,0)},-1);
        let child=fixture_child();let pid=child.id();std::thread::sleep(std::time::Duration::from_millis(100));
        drop(Controller{child:Some(child),root:PathBuf::from("/unused"),id:"fixture-drop".into(),cancelling:false});
        assert_eq!(unsafe{libc::kill(pid as i32,0)},-1);
    }
    #[test]
    fn detached_controller_stays_alive_and_is_reaped_when_it_finishes() {
        let dir = tempfile::tempdir().unwrap(); let done = dir.path().join("done");
        let child = Command::new("python3").args(["-c", "import time,pathlib,sys; time.sleep(.15); pathlib.Path(sys.argv[1]).write_text('completed')", done.to_str().unwrap()])
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).process_group(0).spawn().unwrap();
        let pid = child.id();
        let controller = Controller { child: Some(child), root: dir.path().to_owned(), id: "fixture-detach".into(), cancelling: false };
        let (root, id) = controller.detach(); assert_eq!(root, dir.path()); assert_eq!(id, "fixture-detach");
        assert_eq!(unsafe { libc::kill(pid as i32, 0) }, 0);
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        while unsafe { libc::kill(pid as i32, 0) } == 0 { assert!(std::time::Instant::now() < deadline); std::thread::sleep(std::time::Duration::from_millis(10)); }
        assert_eq!(std::fs::read_to_string(done).unwrap(), "completed");
    }

    #[test]
    fn exact_native_spec_review_requires_explicit_arm_and_hides_task(){
        let temp=tempfile::Builder::new().prefix("u").tempdir().unwrap();
        let args=vec!["--pool".into(),"fixture:fixture-v1".into(),"--prompt".into(),"PRIVATE-TASK-SENTINEL".into(),"-n".into(),"1".into(),"--run-budget".into(),"1".into(),"--root".into(),temp.path().to_string_lossy().into_owned()];
        let prepared=Prepared::start(args,None).unwrap();
        assert!(!prepared.lines.join("\n").contains("PRIVATE-TASK-SENTINEL"));assert!(prepared.prompt_digest.is_some());
        assert!(prepared.launch(Path::new("/nonexistent-fixture-executable")).unwrap_err().to_string().contains("explicitly confirm"));
        assert!(!temp.path().join("manifest.json").exists());
        assert!(!public_plan_error(invalid("unsupported native fleet option PRIVATE-SECRET")).to_string().contains("PRIVATE-SECRET"));
    }

}
