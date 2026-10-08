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
    use std::{ffi::OsString,os::unix::fs::PermissionsExt,process::Command};
    struct Environment(Vec<(&'static str,Option<OsString>)>);
    impl Environment{
        fn set(&mut self,key:&'static str,value:impl AsRef<std::ffi::OsStr>){
            if !self.0.iter().any(|(existing,_)|*existing==key){self.0.push((key,std::env::var_os(key)));}
            std::env::set_var(key,value);
        }
    }
    impl Drop for Environment{fn drop(&mut self){for (key,value) in self.0.drain(..).rev(){match value{Some(value)=>std::env::set_var(key,value),None=>std::env::remove_var(key)}}}}
    fn git(cwd:&Path,args:&[&str])->String{
        let output=Command::new("git").args(args).current_dir(cwd).env("GIT_CONFIG_GLOBAL","/dev/null").env("GIT_CONFIG_NOSYSTEM","1").output().unwrap();
        assert!(output.status.success(),"{}",String::from_utf8_lossy(&output.stderr));String::from_utf8(output.stdout).unwrap().trim().to_owned()
    }
    struct Cleanup{runtime:PathBuf,host:String,ids:Vec<String>}
    impl Drop for Cleanup{fn drop(&mut self){for id in &self.ids{
        if let Ok(sessions)=crate::discovery::sessions_in(&self.runtime){if let Some(session)=sessions.into_iter().find(|s|s.id==*id){
            if let Ok(mut client)=DaemonClient::connect(&session.socket,None){let _=call(&mut client,"stop_if_idle",json!({}));}
        }}
        let _=stopped_claim(&self.runtime,id);
        if let Ok(output)=Command::new("docker").args(["--host",&self.host,"ps","-aq","--no-trunc","--filter",&format!("label=doxa.session={id}")]).output(){
            for container in String::from_utf8_lossy(&output.stdout).lines(){if container.len()==64&&container.bytes().all(|b|b.is_ascii_hexdigit()){
                let _=Command::new("docker").args(["--host",&self.host,"rm","-f",container]).output();
            }}
        }
    }}}
    #[test]
    #[ignore="requires reviewed DOXA_ISOLATION_TEST_IMAGE/HOST and built DOXA_DAEMON_BIN; no vendor turn is submitted"]
    fn rootless_frontend_roundtrip_keeps_clean_and_dirty_sessions_and_rolls_back_start_failure(){
        let image=std::env::var("DOXA_ISOLATION_TEST_IMAGE").expect("explicit reviewed worker image");
        let host=std::env::var("DOXA_ISOLATION_TEST_HOST").expect("explicit private test Engine");
        assert!(host.starts_with("unix:///run/user/")&&!host.ends_with("/docker.sock"));
        let daemon=fs::canonicalize(std::env::var_os("DOXA_DAEMON_BIN").expect("explicit built daemon")).unwrap();
        let mut environment=Environment(Vec::new());
        let dir=tempfile::tempdir().unwrap();let root=fs::canonicalize(dir.path()).unwrap();let home=root.join("owner");let runtime=root.join("runtime");
        fs::create_dir(&home).unwrap();fs::set_permissions(&home,fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(home.join("config.toml"),format!("docker_image={image:?}\ndocker_host={host:?}\nworktree_per_session=true\nlore=false\n")).unwrap();
        for (key,value) in [("DOXA_HOME",home.clone()),("DOXA_RUNTIME_DIR",runtime.clone()),("LORE_ROOT",root.join("lore")),("LORE_PROJECTS_DIR",root.join("projects")),("LORE_SKILLS_DIR",root.join("skills")),("LORE_CODEX_SESSIONS_DIR",root.join("codex"))]{environment.set(key,value);}
        for (key,value) in [("DOXA_LORE","0"),("DOXA_WORKTREE","1"),("LORE_DISABLE_SYNC","1"),("LORE_DISABLE_REVIEW","1"),("DOXA_SESSION_BUDGET_USD",""),("ZAI_API_KEY","migration-fixture-private-key-00000001")]{environment.set(key,value);}
        environment.set("DOXA_DOCKER_IMAGE",&image);environment.set("DOXA_DOCKER_HOST",&host);
        let source=root.join("source");fs::create_dir(&source).unwrap();git(&source,&["init"]);
        fs::write(source.join("tracked"),"base").unwrap();fs::write(source.join("removed"),"base").unwrap();fs::write(source.join(".gitignore"),"ignored\n").unwrap();
        git(&source,&["add","."]);git(&source,&["-c","user.name=Fixture","-c","user.email=fixture@example.invalid","commit","-m","feat: migration fixture"]);
        let wrapper=root.join("reject-docker-daemon");
        fs::write(&wrapper,format!("#!/bin/sh\nfor arg do\n case \"$arg\" in docker-open|docker-offline) echo 'injected target startup failure' >&2; exit 91;; esac\ndone\nexec {} \"$@\"\n",format!("'{}'",daemon.to_string_lossy().replace('\'',"'\\''")))).unwrap();
        fs::set_permissions(&wrapper,fs::Permissions::from_mode(0o700)).unwrap();
        let mut cleanup=Cleanup{runtime:runtime.clone(),host:host.clone(),ids:Vec::new()};
        for dirty in [false,true]{
            environment.set("DOXA_DAEMON_BIN",&daemon);
            let session=launch::spawn(&LaunchOptions{engine:Engine::Glm,cwd:Some(source.clone()),isolation:Some(Profile::Native),model:Some("glm-5.3-flash".into()),effort:Some("high".into()),linger:Some(120.0),..LaunchOptions::default()}).unwrap();
            cleanup.ids.push(session.id.clone());let mut client=DaemonClient::connect(&session.socket,None).unwrap();
            assert_eq!(client.hello["isolation"]["can_set_isolation"],true);
            let original=PathBuf::from(client.hello["cwd"].as_str().unwrap());assert_ne!(original,source,"fixture must use a managed native worktree");
            if dirty{
                fs::write(original.join("tracked"),"staged").unwrap();git(&original,&["add","tracked"]);fs::write(original.join("tracked"),"working").unwrap();
                fs::remove_file(original.join("removed")).unwrap();fs::write(original.join("untracked"),"private work").unwrap();fs::write(original.join("ignored"),"retained").unwrap();
            }
            let before=call(&mut client,"isolation_migration_plan",json!({})).unwrap();
            let (docker,status)=change(&mut client,Profile::DockerOffline).unwrap();client=docker;
            assert_eq!(crate::discovery::sessions_in(&runtime).unwrap().into_iter().find(|s|s.id==session.id).unwrap().scope_key,session.scope_key);
            assert_eq!(status["profile"],"docker-offline");assert!(original.is_dir(),"admitted migration must preserve even a clean managed native worktree");
            let clone=PathBuf::from(client.hello["cwd"].as_str().unwrap());assert_ne!(clone,original);
            let after=call(&mut client,"isolation_migration_plan",json!({})).unwrap();assert_eq!(after["manifest"]["context_cwd"],json!(original));
            for key in ["transcript_path","transcript_bytes","model","effort","permission_mode"]{assert_eq!(before[key],after[key],"changed {key}");}
            if dirty{assert_eq!(git(&clone,&["show",":tracked"]),"staged");assert_eq!(fs::read_to_string(clone.join("tracked")).unwrap(),"working");assert!(!clone.join("removed").exists());assert!(clone.join("untracked").exists()&&clone.join("ignored").exists());}
            let (native,status)=change(&mut client,Profile::Native).unwrap();client=native;assert_eq!(status["profile"],"native");assert_eq!(client.hello["cwd"],json!(clone));
            assert_eq!(crate::discovery::sessions_in(&runtime).unwrap().into_iter().find(|s|s.id==session.id).unwrap().scope_key,session.scope_key);
            assert_eq!(fs::read_to_string(source.join("tracked")).unwrap(),"base");assert!(!source.join("untracked").exists());
            environment.set("DOXA_DAEMON_BIN",&wrapper);
            let failure=change(&mut client,Profile::DockerOffline).err().expect("target start must fail");assert!(failure.to_string().contains("original isolation and conversation resumed"),"{failure}");
            let restored=crate::discovery::sessions_in(&runtime).unwrap().into_iter().find(|s|s.id==session.id).unwrap();client=DaemonClient::connect(&restored.socket,None).unwrap();
            assert_eq!(client.hello["isolation"]["profile"],"native");assert_eq!(client.hello["cwd"],json!(clone));
            assert_eq!(restored.scope_key,session.scope_key);
            if dirty{assert_eq!(git(&clone,&["show",":tracked"]),"staged");assert_eq!(fs::read_to_string(clone.join("untracked")).unwrap(),"private work");}
            call(&mut client,"stop_if_idle",json!({})).unwrap();drop(stopped_claim(&runtime,&session.id).unwrap());
        }
    }
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
