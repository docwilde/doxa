//! Stopped-session backend migration. Originals and durable conversation
//! checkpoints remain in place; only the provider workspace/context is copied.
use super::*;
use std::os::unix::fs::symlink;

pub struct Migration {
    home:PathBuf, path:PathBuf, previous:Manifest, target:Profile, plan:Value,
    _lock:File, installed:Option<Manifest>,
}
impl Migration {
    /// Validate target availability before requesting provider shutdown.
    pub fn prepare(home:&Path,plan:Value,target:Profile)->io::Result<Self>{
        let previous:Manifest=serde_json::from_value(plan["manifest"].clone())?;
        let path=manifest_path(home,&previous.session_id)?;
        let current=read_manifest(&path)?;
        if serde_json::to_value(&current)?!=plan["manifest"]||target.docker()==current.profile.docker(){
            return Err(error("isolation migration plan is stale or does not change backend"));
        }
        if target.docker(){preflight(&Policy::configured(home)?)?;}
        let lock=OpenOptions::new().write(true).create(true).truncate(false).mode(0o600)
            .custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK).open(path.parent().unwrap().join("migration.lock"))?;
        if unsafe{libc::flock(std::os::fd::AsRawFd::as_raw_fd(&lock),libc::LOCK_EX|libc::LOCK_NB)}!=0{
            return Err(error("another isolation migration owns this session"));
        }
        Ok(Self{home:home.into(),path,previous,target,plan,_lock:lock,installed:None})
    }
    pub fn previous(&self)->&Manifest{&self.previous}
    pub fn commit(&mut self)->io::Result<Manifest>{
        let stopped=read_manifest(&self.path)?;
        if stopped.state!="stopped"||stopped.profile!=self.previous.profile||stopped.checkout!=self.previous.checkout{
            return Err(error("original backend has not completed verified teardown"));
        }
        if let Some(path)=self.plan["transcript_path"].as_str(){
            if fingerprint(Path::new(path),self.plan["transcript_bytes"].as_u64().ok_or_else(||error("transcript size missing"))?)? != self.plan["transcript_sha256"]{
                return Err(error("conversation checkpoint changed during provider shutdown"));
            }
        }
        let root=self.path.parent().unwrap();
        let mut next=stopped.clone();
        next.context_cwd=Some(self.previous.context_cwd.clone().unwrap_or_else(||self.previous.checkout.clone()));
        next.profile=self.target;next.state="preparing".into();
        for dir in [&next.private_home,&next.cache,&next.broker,&next.cache.join("tmp")]{private_directory(dir,true)?;}
        if self.target.docker()&&next.checkout!=root.join("checkout"){
            let stage=tempfile::Builder::new().prefix(".migration-").tempdir_in(root)?;
            let checkout=stage.path().join("checkout");
            let (sha,branch)=clone_checkout(&self.previous.checkout,&checkout,&next.session_id,None)?;
            copy_checkout(&self.previous.checkout,&checkout)?;
            let destination=root.join("checkout");
            if destination.exists(){
                private_directory(&destination,false)?;
                // Retain every failed prior clone rather than deleting work.
                fs::rename(&destination,root.join(format!("retained-checkout-{}",nonce()?)))?;
            }
            fs::rename(checkout,&destination)?;
            next.checkout=destination;next.base_sha=sha;next.branch=branch;
            let meta=fs::metadata(&next.checkout)?;next.checkout_device=meta.dev();next.checkout_inode=meta.ino();
        }
        import_context(&self.plan,&self.previous,&mut next)?;
        if self.target.docker(){
            let policy=Policy::configured(&self.home)?;preflight(&policy)?;
            next.policy_hash=policy.hash(next.profile);next.creation_policy_hash=next.policy_hash.clone();
            next.policy=Some(policy.clone());next.nonce=nonce()?;next.container_id=None;
            if let (Some(old_policy),Some(old_id))=(&self.previous.policy,&self.previous.container_id){
                let listed=String::from_utf8(docker_run(old_policy,&["ps","-aq","--no-trunc","--filter",&format!("label=doxa.session={}",next.session_id)])?).map_err(|_|error("invalid container list"))?;
                if listed.lines().any(|id|id==old_id){
                    let actual=inspect(&self.previous)?;
                    if actual["State"]["Running"]!=false{return Err(error("retained backend is still running"));}
                    docker_run(old_policy,&["rm",old_id])?;
                }
            }
            let mut command=docker(&policy);command.args(create_args(&next)?);
            let id=String::from_utf8(run(command)?).map_err(|_|error("invalid container identity"))?.trim().to_owned();
            if id.len()!=64||!id.bytes().all(|b|b.is_ascii_hexdigit()){return Err(error("invalid migration container identity"));}
            next.container_id=Some(id);
            self.installed=Some(next.clone());
            reconcile(&mut next,true)?;
        }else{next.state="ready".into();}
        write_manifest(&self.path,&next)?;
        self.installed=Some(next.clone());
        Ok(next)
    }
    /// Restore the exact source identity after a refused backend start. The
    /// private clone/context remain available for inspection and recovery.
    pub fn rollback(&mut self)->io::Result<()> {
        if let Some(manifest)=self.installed.take().filter(|m|m.profile.docker()){
            let mut runtime=Runtime{path:self.path.clone(),manifest,migration_stop:false};
            runtime.stop()?;
            if let (Some(policy),Some(id))=(&runtime.manifest.policy,&runtime.manifest.container_id){docker_run(policy,&["rm",id])?;}
        }
        let mut previous=self.previous.clone();previous.state="stopped".into();
        write_manifest(&self.path,&previous)
    }
}

fn bytes(path:&Path,limit:u64)->io::Result<Vec<u8>>{
    if fs::canonicalize(path)?!=path{return Err(error("migration context path contains symlink components"));}
    let file=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK).open(path)?;
    let meta=file.metadata()?;
    if !meta.is_file()||meta.uid()!=unsafe{libc::geteuid()}||meta.nlink()!=1||meta.len()>limit{
        return Err(error("migration input must be a bounded owned regular file"));
    }
    let mut out=Vec::new();file.take(limit+1).read_to_end(&mut out)?;
    if out.len() as u64!=meta.len(){return Err(error("migration input changed while being copied"));}
    Ok(out)
}
pub fn fingerprint(path:&Path,expected:u64)->io::Result<String>{
    let content=bytes(path,64*1024*1024)?;
    if content.len() as u64!=expected{return Err(error("conversation checkpoint size changed"));}
    Ok(format!("{:x}",Sha256::digest(content)))
}
fn write_copy(destination:&Path,content:&[u8])->io::Result<()> {
    private_directory(destination.parent().ok_or_else(||error("copy parent missing"))?,true)?;
    let mut tmp=tempfile::NamedTempFile::new_in(destination.parent().unwrap())?;
    tmp.as_file().set_permissions(fs::Permissions::from_mode(0o600))?;
    tmp.write_all(content)?;tmp.as_file().sync_all()?;tmp.persist(destination).map_err(|e|e.error)?;
    File::open(destination.parent().unwrap())?.sync_all()
}
fn copy_checkout(source:&Path,destination:&Path)->io::Result<()> {
    // This destination is a new private clone. Its original Git object store
    // is retained; the worktree is replaced by the complete stopped workspace.
    for entry in fs::read_dir(destination)?{
        let entry=entry?;if entry.file_name()==".git"{continue;}
        if entry.file_type()?.is_dir(){fs::remove_dir_all(entry.path())?;}else{fs::remove_file(entry.path())?;}
    }
    fn walk(source:&Path,dest:&Path,count:&mut u64,total:&mut u64)->io::Result<()> {
        for entry in fs::read_dir(source)?{
            let entry=entry?;if entry.file_name()==".git"{continue;}
            *count+=1;if *count>100_000{return Err(error("workspace migration exceeds 100000 files"));}
            let from=entry.path();let to=dest.join(entry.file_name());let meta=fs::symlink_metadata(&from)?;
            if meta.is_dir(){fs::create_dir(&to)?;walk(&from,&to,count,total)?;fs::set_permissions(&to,fs::Permissions::from_mode(meta.mode()&0o777))?;}
            else if meta.file_type().is_symlink(){symlink(fs::read_link(&from)?,&to)?;}
            else if meta.is_file(){
                *total=total.checked_add(meta.len()).ok_or_else(||error("workspace size overflow"))?;
                if *total>8*1024*1024*1024{return Err(error("workspace migration exceeds 8 GiB; original files are retained"));}
                let input=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK).open(&from)?;
                let identity=input.metadata()?;
                if (meta.dev(),meta.ino(),meta.len())!=(identity.dev(),identity.ino(),identity.len()){return Err(error("workspace changed during migration"));}
                let mut output=OpenOptions::new().write(true).create_new(true).mode(meta.mode()&0o777).open(&to)?;
                let copied=io::copy(&mut input.take(meta.len()+1),&mut output)?;output.sync_all()?;
                if copied!=meta.len(){return Err(error("workspace file changed during migration"));}
            }else{return Err(error("workspace migration refuses sockets, devices and special files"));}
        }Ok(())
    }
    walk(source,destination,&mut 0,&mut 0)?;
    // Preserve staged changes separately from the copied working tree.
    let patch=git(source,&["diff","--cached","--binary","--no-ext-diff","--no-textconv"])?;
    if !patch.is_empty(){
        let mut file=tempfile::NamedTempFile::new_in(destination.parent().unwrap())?;
        writeln!(file,"{patch}")?;file.flush()?;
        git(destination,&["apply","--cached","--binary",file.path().to_str().ok_or_else(||error("non-UTF8 patch path"))?])?;
    }
    File::open(destination)?.sync_all()
}
fn import_context(plan:&Value,previous:&Manifest,next:&mut Manifest)->io::Result<()> {
    let engine=plan["launch"]["engine"].as_str().ok_or_else(||error("migration engine missing"))?;
    if engine=="codex"{
        let transcript=PathBuf::from(plan["transcript_path"].as_str().ok_or_else(||error("complete a turn before migrating Codex"))?);
        let record:Value=serde_json::from_slice(&bytes(&transcript.with_extension("codex.json"),64*1024)?)?;
        if record["session_id"]!=previous.session_id||record["turn_incomplete"]!=false
            ||record["transcript_bytes"]!=plan["transcript_bytes"]||record["transport"]!="app-server"{
            return Err(error("migration requires the exact clean protected Codex checkpoint"));
        }
        let source=record["rollout_path"].as_str().map(PathBuf::from).or_else(||previous.provider_rollout.clone())
            .ok_or_else(||error("Codex rollout unavailable; original backend remains resumable"))?;
        let content=bytes(&source,64*1024*1024)?;
        let first=content.split(|b|*b==b'\n').find(|row|!row.is_empty()).ok_or_else(||error("empty Codex rollout"))?;
        let identity:Value=serde_json::from_slice(first)?;
        if identity["type"]!="session_meta"||identity["payload"]["id"]!=record["thread_id"]||content.last()!=Some(&b'\n'){
            return Err(error("provider rollout does not prove the saved Codex thread"));
        }
        let thread=record["thread_id"].as_str().filter(|id|!id.is_empty()&&id.len()<=128&&id.bytes().all(|b|b.is_ascii_alphanumeric()||matches!(b,b'-'|b'_'))).ok_or_else(||error("invalid provider thread identity"))?;
        // Keep the dated rollout shape required by the protected context
        // reader. The file is still selected solely by its verified thread.
        let relative=Path::new("1970/01/01").join(format!("rollout-migration-{thread}.jsonl"));
        let destination=next.private_home.join("codex/sessions").join(relative);
        write_copy(&destination,&content)?;next.provider_rollout=Some(destination);
    }else if engine=="claude"{
        let home=PathBuf::from(plan["launch"]["claude_home"].as_str().ok_or_else(||error("Claude context home missing"))?);
        let old_cwd=if previous.profile.docker(){PathBuf::from("/workspace")}else{previous.checkout.clone()};
        let slug=|path:&Path|path.to_string_lossy().chars().map(|c|if c.is_ascii_alphanumeric(){c}else{'-'}).collect::<String>();
        let name=format!("{}.jsonl",previous.session_id);
        let mut source=home.join("projects").join(slug(&old_cwd)).join(&name);
        if !source.exists(){
            let candidates=fs::read_dir(home.join("projects"))?.take(4097).filter_map(Result::ok)
                .filter(|entry|entry.file_type().is_ok_and(|ty|ty.is_dir())).map(|entry|entry.path().join(&name)).filter(|path|path.exists()).collect::<Vec<_>>();
            if candidates.len()!=1{return Err(error("Claude provider context is missing or ambiguous"));}source=candidates[0].clone();
        }
        let content=bytes(&source,64*1024*1024)?;
        if content.last()!=Some(&b'\n'){return Err(error("incomplete Claude provider context"));}
        for row in content.split(|b|*b==b'\n').filter(|row|!row.is_empty()){
            let value:Value=serde_json::from_slice(row)?;
            if value.get("sessionId").is_some_and(|id|id!=&json!(previous.session_id)){return Err(error("Claude provider context identity changed"));}
        }
        let cwd=if next.profile.docker(){PathBuf::from("/workspace")}else{next.checkout.clone()};
        write_copy(&next.private_home.join("claude/projects").join(slug(&cwd)).join(name),&content)?;
    }else if !matches!(engine,"deepseek"|"glm"){return Err(error("unsupported isolation migration engine"));}
    Ok(())
}

#[cfg(test)]
mod tests{
    use super::*;
    fn repository(source:&Path){
        fs::create_dir(source).unwrap();git(source,&["init"]).unwrap();
        fs::write(source.join("tracked"),"base").unwrap();git(source,&["add","."]).unwrap();
        git(source,&["-c","user.name=Fixture","-c","user.email=fixture@example.invalid","commit","-m","feat: fixture"]).unwrap();
    }
    fn stopped_docker(home:&Path,source:&Path)->Manifest{
        let runtime=Runtime::prepare(home,"migration-fixture",source,Some(Profile::Native),false,None).unwrap();
        let mut manifest=runtime.manifest().clone();let root=manifest.private_home.parent().unwrap();
        for dir in [&manifest.private_home,&manifest.cache,&manifest.broker]{private_directory(dir,true).unwrap();}
        manifest.checkout=root.join("checkout");clone_checkout(source,&manifest.checkout,&manifest.session_id,None).unwrap();
        let meta=fs::metadata(&manifest.checkout).unwrap();manifest.checkout_device=meta.dev();manifest.checkout_inode=meta.ino();
        let policy=Policy{image:format!("sha256:{}","a".repeat(64)),docker_host:"unix:///run/user/1000/fixture.sock".into(),memory_bytes:512*1024*1024,cpus:1.0,pids:128};
        manifest.profile=Profile::DockerOpen;manifest.policy_hash=policy.hash(manifest.profile);manifest.creation_policy_hash=manifest.policy_hash.clone();
        manifest.policy=Some(policy);manifest.context_cwd=Some(source.to_owned());manifest.state="stopped".into();
        write_manifest(&manifest_path(home,&manifest.session_id).unwrap(),&manifest).unwrap();manifest
    }
    #[test]
    fn clean_checkout_copy_keeps_source_and_independent_history(){
        let dir=tempfile::tempdir().unwrap();let source=dir.path().join("source");repository(&source);
        let destination=dir.path().join("clone");clone_checkout(&source,&destination,"clean",None).unwrap();copy_checkout(&source,&destination).unwrap();
        assert_eq!(git(&destination,&["status","--porcelain"]).unwrap(),"");assert_eq!(git(&source,&["status","--porcelain"]).unwrap(),"");
        assert_eq!(fs::read(source.join("tracked")).unwrap(),fs::read(destination.join("tracked")).unwrap());
        assert_ne!(fs::metadata(source.join(".git/objects")).unwrap().ino(),fs::metadata(destination.join(".git/objects")).unwrap().ino());
    }
    #[test]
    fn docker_to_native_keeps_private_clone_logical_identity_and_can_roll_back(){
        let dir=tempfile::tempdir().unwrap();let root=fs::canonicalize(dir.path()).unwrap();let source=root.join("source");repository(&source);
        let home=root.join("doxa");let original=stopped_docker(&home,&source);fs::write(original.checkout.join("untracked"),"worker change").unwrap();
        let plan=json!({"manifest":original,"launch":{"engine":"glm"},"transcript_path":null});
        let mut migration=Migration::prepare(&home,plan,Profile::Native).unwrap();let native=migration.commit().unwrap();
        assert_eq!(native.checkout,original.checkout);assert_eq!(native.context_cwd.as_deref(),Some(source.as_path()));
        let mut resumed=Runtime::prepare(&home,&native.session_id,&native.checkout,Some(Profile::Native),true,None).unwrap();
        assert_eq!(resumed.status()["can_set_isolation"],true);resumed.stop().unwrap();
        assert_eq!(fs::read_to_string(native.checkout.join("untracked")).unwrap(),"worker change");assert!(!source.join("untracked").exists());
        migration.rollback().unwrap();let saved=read_manifest(&manifest_path(&home,&native.session_id).unwrap()).unwrap();
        assert_eq!(saved.profile,Profile::DockerOpen);assert_eq!(saved.checkout,original.checkout);assert_eq!(saved.context_cwd,original.context_cwd);
        assert_eq!(fs::read_to_string(saved.checkout.join("untracked")).unwrap(),"worker change");
    }
    #[test]
    fn changed_checkpoint_is_refused_before_manifest_or_workspace_mutation(){
        let dir=tempfile::tempdir().unwrap();let root=fs::canonicalize(dir.path()).unwrap();let source=root.join("source");repository(&source);
        let home=root.join("doxa");let original=stopped_docker(&home,&source);let transcript=root.join("conversation.jsonl");fs::write(&transcript,"old\n").unwrap();
        let plan=json!({"manifest":original,"launch":{"engine":"glm"},"transcript_path":transcript,"transcript_bytes":4,"transcript_sha256":fingerprint(&transcript,4).unwrap()});
        let mut migration=Migration::prepare(&home,plan,Profile::Native).unwrap();fs::write(&transcript,"new\n").unwrap();
        assert!(migration.commit().unwrap_err().to_string().contains("checkpoint changed"));
        assert_eq!(read_manifest(&manifest_path(&home,&original.session_id).unwrap()).unwrap().profile,Profile::DockerOpen);
        assert_eq!(fs::read_to_string(source.join("tracked")).unwrap(),"base");
    }
    #[test]
    fn selected_codex_rollout_imports_only_verified_thread_and_refuses_another(){
        let dir=tempfile::tempdir().unwrap();let root=fs::canonicalize(dir.path()).unwrap();let source=root.join("source");repository(&source);
        let home=root.join("doxa");let original=stopped_docker(&home,&source);let transcript=root.join("conversation.jsonl");fs::write(&transcript,"prior\n").unwrap();
        let provider=root.join("provider-session.jsonl");fs::write(&provider,b"{\"type\":\"session_meta\",\"payload\":{\"id\":\"saved-thread\"}}\n").unwrap();
        fs::write(transcript.with_extension("codex.json"),serde_json::to_vec(&json!({"session_id":original.session_id,"thread_id":"saved-thread","turn_incomplete":false,"transport":"app-server","transcript_bytes":6,"rollout_path":provider})).unwrap()).unwrap();
        fs::write(root.join("unrelated-session.jsonl"),"private unrelated context").unwrap();
        let plan=json!({"launch":{"engine":"codex"},"transcript_path":transcript,"transcript_bytes":6});let mut next=original.clone();next.profile=Profile::Native;
        import_context(&plan,&original,&mut next).unwrap();let imported=next.provider_rollout.clone().unwrap();
        assert_eq!(fs::read(&imported).unwrap(),fs::read(&provider).unwrap());assert_eq!(fs::metadata(&imported).unwrap().mode()&0o777,0o600);
        assert_eq!(fs::read_dir(imported.parent().unwrap()).unwrap().count(),1);
        let before=fs::read(&imported).unwrap();fs::write(&provider,b"{\"type\":\"session_meta\",\"payload\":{\"id\":\"another-thread\"}}\n").unwrap();
        assert!(import_context(&plan,&original,&mut next).is_err());assert_eq!(fs::read(&imported).unwrap(),before);
    }
    #[test]
    fn stopped_workspace_copy_preserves_staging_deletions_ignored_files_and_links(){
        let dir=tempfile::tempdir().unwrap();let source=dir.path().join("source");fs::create_dir(&source).unwrap();
        git(&source,&["init"] ).unwrap();git(&source,&["config","user.name","Fixture"]).unwrap();git(&source,&["config","user.email","fixture@example.invalid"]).unwrap();
        fs::write(source.join("tracked"),"original").unwrap();fs::write(source.join("removed"),"gone").unwrap();fs::write(source.join(".gitignore"),"ignored\n").unwrap();
        git(&source,&["add","."]).unwrap();git(&source,&["commit","-m","fixture"]).unwrap();
        fs::write(source.join("tracked"),"staged").unwrap();git(&source,&["add","tracked"]).unwrap();fs::write(source.join("tracked"),"working").unwrap();
        fs::remove_file(source.join("removed")).unwrap();fs::write(source.join("untracked"),"new").unwrap();fs::write(source.join("ignored"),"keep").unwrap();symlink("tracked",source.join("link")).unwrap();
        let destination=dir.path().join("clone");clone_checkout(&source,&destination,"fixture",None).unwrap();copy_checkout(&source,&destination).unwrap();
        assert_eq!(fs::read_to_string(destination.join("tracked")).unwrap(),"working");
        assert_eq!(git(&destination,&["show",":tracked"]).unwrap(),"staged");
        assert!(!destination.join("removed").exists());assert_eq!(fs::read_to_string(destination.join("ignored")).unwrap(),"keep");
        assert_eq!(fs::read_to_string(destination.join("untracked")).unwrap(),"new");assert_eq!(fs::read_link(destination.join("link")).unwrap(),PathBuf::from("tracked"));
        assert!(!destination.join(".git/objects/info/alternates").exists());
        assert_eq!(fs::read_to_string(source.join("tracked")).unwrap(),"working");
    }
}
