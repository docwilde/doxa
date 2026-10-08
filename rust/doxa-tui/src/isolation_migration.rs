//! Host-side transaction for the explicit idle /isolation control.
use crate::{launch::{self,Engine,LaunchOptions},transport::DaemonClient};
use doxa_isolation::{Manifest,Profile,migration::Migration};
use serde_json::{json,Value};
use std::{fs::{self,File,OpenOptions},io,os::{fd::AsRawFd,unix::fs::{MetadataExt,OpenOptionsExt}},path::{Path,PathBuf},thread,time::{Duration,Instant}};

fn invalid(message:impl std::fmt::Display)->io::Error{io::Error::other(message.to_string())}
fn call(client:&mut DaemonClient,method:&str,params:Value)->io::Result<Value>{
    let reply=client.call_until(method,params.as_object().cloned().ok_or_else(||invalid("invalid migration request"))?,Instant::now()+Duration::from_secs(90)).map_err(invalid)?;
    if reply["ok"]!=true{return Err(invalid(reply["error"].as_str().unwrap_or("migration request refused")));}
    let mut object=reply.as_object().cloned().ok_or_else(||invalid("invalid migration reply"))?;
    for key in ["type","id","ok"]{object.remove(key);}
    Ok(Value::Object(object))
}

/// Claim the same stable lock inode as the daemon, after verified teardown.
/// Holding it prevents any other frontend from opening provider/checkpoint
/// state while the stopped workspace is being copied.
fn stopped_claim(runtime:&Path,id:&str)->io::Result<File>{
    let path=runtime.join("registry").join(format!("{id}.lock"));
    let file=OpenOptions::new().read(true).write(true).custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK).open(path)?;
    let meta=file.metadata()?;
    if !meta.is_file()||meta.uid()!=unsafe{libc::geteuid()}||meta.nlink()!=1||meta.mode()&0o077!=0{return Err(invalid("unsafe migration session claim"));}
    let deadline=Instant::now()+Duration::from_secs(90);
    loop{
        if unsafe{libc::flock(file.as_raw_fd(),libc::LOCK_EX|libc::LOCK_NB)}==0{
            match fs::symlink_metadata(runtime.join("registry").join(format!("{id}.json"))){
                Err(e) if e.kind()==io::ErrorKind::NotFound=>return Ok(file),
                _=>return Err(invalid("original session registry was not removed after teardown")),
            }
        }
        let error=io::Error::last_os_error();
        if error.kind()!=io::ErrorKind::WouldBlock{return Err(error);}
        if Instant::now()>=deadline{return Err(invalid("original provider did not complete teardown; migration was not committed"));}
        thread::sleep(Duration::from_millis(25));
    }
}

fn options(plan:&Value,manifest:&Manifest)->io::Result<LaunchOptions>{
    let record=&plan["launch"];
    let engine=match record["engine"].as_str(){Some("codex")=>Engine::Codex,Some("claude")=>Engine::Claude,
        Some("deepseek")=>Engine::DeepSeek,Some("glm")=>Engine::Glm,_=>return Err(invalid("unsupported migration engine"))};
    Ok(LaunchOptions{isolation:Some(manifest.profile),engine,cwd:Some(manifest.checkout.clone()),resume:Some(manifest.session_id.clone()),
        model:plan["model"].as_str().map(str::to_owned),effort:plan["effort"].as_str().map(str::to_owned),
        linger:record["linger"].as_f64(),sandbox:record["sandbox"].as_str().map(str::to_owned),
        codex_bin:if engine==Engine::Codex{record["codex_bin"].as_str().map(PathBuf::from)}else{None},
        claude_bin:if engine==Engine::Claude{record["claude_bin"].as_str().map(PathBuf::from)}else{None},..LaunchOptions::default()})
}

fn resume(plan:&Value,manifest:&Manifest)->io::Result<DaemonClient>{
    let session=launch::spawn_migrated(&options(plan,manifest)?,&plan["launch"])?;
    let mut client=DaemonClient::connect(&session.socket,None).map_err(invalid)?;
    let result=(||{
        if client.hello["session_id"]!=manifest.session_id||client.hello["isolation"]["profile"]!=manifest.profile.key()
            ||client.hello["isolation"]["state"]!="ready"{return Err(invalid("resumed daemon did not verify its session and isolation identity"));}
        let resumed=call(&mut client,"isolation_migration_plan",json!({}))?;
        for key in ["transcript_path","transcript_bytes","transcript_sha256","model","effort","permission_mode"]{
            if resumed[key]!=plan[key]{return Err(invalid(format!("resumed conversation changed {key}")));}
        }
        if plan["launch"]["engine"]=="codex"{
            let verified=call(&mut client,"verify_resume",json!({}))?;
            if verified["verified"]!=true{return Err(invalid("Codex provider did not verify the saved thread"));}
        }
        Ok(())
    })();
    if let Err(error)=result{
        // A failed validation never exposes a usable target client. Stop it
        // through the authoritative idle gate and wait for checkpoint release.
        call(&mut client,"stop_if_idle",json!({}))?;
        let runtime=Path::new(plan["launch"]["runtime"].as_str().ok_or_else(||invalid("migration runtime missing"))?);
        drop(stopped_claim(runtime,&manifest.session_id)?);
        return Err(error);
    }
    Ok(client)
}

/// Returns a new connection only after the original session/thread, durable
/// checkpoint and target backend all passed verification. Failure restores
/// the original profile and starts its same-session resume when possible.
pub(crate) fn change(client:&mut DaemonClient,target:Profile)->io::Result<(DaemonClient,Value)>{
    let plan=call(client,"isolation_migration_plan",json!({}))?;
    let home=Path::new(plan["launch"]["home"].as_str().ok_or_else(||invalid("migration owner home missing"))?);
    let runtime=Path::new(plan["launch"]["runtime"].as_str().ok_or_else(||invalid("migration runtime missing"))?);
    let mut transaction=Migration::prepare(home,plan.clone(),target)?;
    let original=transaction.previous().clone();
    // Validate the preserved launch before asking the original provider to stop.
    options(&plan,&original)?;
    call(client,"isolation_migration_stop",json!({"confirmed":true,"expected":plan}))?;
    let claim=stopped_claim(runtime,&original.session_id)?;
    let prepared=transaction.commit();
    drop(claim);
    let result=prepared.and_then(|manifest|resume(&plan,&manifest).map(|client|(client,manifest.status())));
    match result{
        Ok(result)=>Ok(result),
        Err(error)=>{
            let claim=stopped_claim(runtime,&original.session_id)?;
            transaction.rollback()?;
            drop(claim);
            match resume(&plan,&original){
                Ok(_)=>Err(invalid(format!("{error}; original isolation and conversation resumed"))),
                Err(restore)=>Err(invalid(format!("{error}; original checkpoint retained, resume refused: {restore}"))),
            }
        }
    }
}

#[cfg(test)]
mod tests{
    use super::*;
    #[test]
    fn snapshot_relaunch_preserves_scope_lineage_and_provider_choice(){
        let root=tempfile::tempdir().unwrap();let home=fs::canonicalize(root.path()).unwrap();let cwd=home.join("source");fs::create_dir(&cwd).unwrap();
        let runtime=doxa_isolation::Runtime::prepare(&home.join("doxa"),"session",&cwd,Some(Profile::Native),false,None).unwrap();
        let plan=json!({"launch":{"engine":"codex","codex_bin":"/selected/provider","sandbox":"read-only","linger":120},"model":"gpt-5.4","effort":"high"});
        let restored=options(&plan,runtime.manifest()).unwrap();
        assert_eq!(restored.resume.as_deref(),Some("session"));assert_eq!(restored.cwd.as_deref(),Some(cwd.as_path()));
        assert_eq!(restored.codex_bin.as_deref(),Some(Path::new("/selected/provider")));assert_eq!(restored.sandbox.as_deref(),Some("read-only"));
        assert_eq!(restored.model.as_deref(),Some("gpt-5.4"));assert_eq!(restored.effort.as_deref(),Some("high"));
    }
}
