use super::{AuthorityReport, Policy, RootManifest};
use crate::{error, quota_install::enforcement::{self, EnforcedQuotaSnapshot}};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{collections::{BTreeMap,BTreeSet}, ffi::{CString,OsStr}, fs::{self,File},
    io::{self,Read,Write}, mem, os::{fd::{AsRawFd,FromRawFd,RawFd},unix::{ffi::OsStrExt,
    fs::{MetadataExt,FileTypeExt},net::UnixStream}}, path::{Component,Path,PathBuf},
    time::{Duration,Instant}};

const SO_PEERPIDFD: libc::c_int=77;
const MAX_JSON:usize=2*1024*1024;
const MAX_PROC:usize=2*1024*1024;
const MAX_ARTIFACT:u64=512*1024*1024;
#[path="hardened_view.rs"]mod fs_view;

#[derive(Debug,Clone,PartialEq,Eq)]
struct Identity {dev:u64,ino:u64,len:u64,ctime:i64,ctime_nsec:i64,mtime:i64,mtime_nsec:i64,mode:u32,uid:u32,gid:u32}
impl Identity { fn of(f:&File)->io::Result<Self>{let m=f.metadata()?;Ok(Self{dev:m.dev(),ino:m.ino(),len:m.len(),
    ctime:m.ctime(),ctime_nsec:m.ctime_nsec(),mtime:m.mtime(),mtime_nsec:m.mtime_nsec(),mode:m.mode(),uid:m.uid(),gid:m.gid()})} }

struct PinnedFile {path:PathBuf,file:File,identity:Identity,sha256:String}
impl PinnedFile {
    fn revalidate(&self,deadline:Instant)->io::Result<()> {
        let f=root_file(&self.path,MAX_ARTIFACT,false)?;
        if Identity::of(&f)?!=self.identity || Identity::of(&self.file)?!=self.identity
            || hash_file(&f,MAX_ARTIFACT,deadline)?!=self.sha256 {
            return Err(error("administrator file changed after authority was pinned"));
        } Ok(())
    }
}

#[derive(Debug,Clone,PartialEq,Eq)]
struct Scope {namespaces:BTreeMap<String,PathBuf>,cgroup:String}

/// Retained ROOT-controlled policy, Engine/init pidfds and quota capabilities.
/// This does not authenticate a future sender or authorize from a JSON flag.
pub struct Authority {
    pub policy:Policy,
    pub manifest:RootManifest,
    pub policy_sha256:String,
    policy_file:PinnedFile,
    manifest_path:PathBuf,
    engine_file:PinnedFile,
    config_file:PinnedFile,
    authority_file:PinnedFile,
    broker_file:PinnedFile,
    engine_pin:File,
    init_pin:File,
    init_scope:Scope,
    quota:EnforcedQuotaSnapshot,
}

/// Bounded no-follow administrator input for pre-container staging. The SHA
/// binds the exact bytes decoded here; it is not a caller's assertion.
pub fn load_root_policy(path:&Path)->io::Result<(Policy,String)> {
    require_root()?;
    let (pinned,bytes)=pin_json(path,64*1024,Instant::now()+Duration::from_secs(3))?;
    let policy:Policy=serde_json::from_slice(&bytes).map_err(|_|error("administrator policy has invalid or unknown fields"))?;
    policy.validate()?;
    Ok((policy,pinned.sha256))
}

/// Staging confirms only the installed quota tree and ROOT-controlled pins.
/// No container, filter, Engine or gateway admission is asserted by this API.
pub fn stage_check(path:&Path,phase:&str,family:&str)->io::Result<Value> {
    let (policy,policy_sha256)=load_root_policy(path)?;
    if !matches!(phase,"before"|"after")||!matches!(family,"none"|"claude"|"codex")
        ||family!="none"&&!policy.providers.contains_key(family) {
        return Err(error("unsupported fixed staging phase or provider family"));
    }
    let deadline=Instant::now()+Duration::from_secs(policy.limits.operation_seconds);
    let verifier=pin_artifact(&policy.authority.path,&policy.authority.sha256,deadline)?;
    let this=File::open("/proc/self/exe")?.metadata()?;let expected=verifier.file.metadata()?;
    if (this.dev(),this.ino())!=(expected.dev(),expected.ino()) {
        return Err(error("staging verifier is not the administrator-pinned native executable"));
    }
    pin_artifact(&policy.broker.path,&policy.broker.sha256,deadline)?;
    let quota=enforcement::load_root_enforcement_policy(&policy.session_id)?.verify_for_gateway()?;
    check_quota(&policy,&quota)?;
    remaining(deadline)?;
    Ok(json!({"contract":"doxa-root-session-staging-v1","policy_sha256":policy_sha256,
        "phase":phase,"family":family,"quota_ready":true,"boot_id":boot_id()?}))
}

impl Authority {
    pub fn load(policy_path:&Path,manifest_path:&Path,engine_pidfd:RawFd)->io::Result<Self> {
        require_root()?;
        let deadline=Instant::now()+Duration::from_secs(30);
        let (policy_file,bytes)=pin_json(policy_path,64*1024,deadline)?;
        let policy:Policy=serde_json::from_slice(&bytes).map_err(|_|error("administrator policy has invalid or unknown fields"))?;
        policy.validate()?;
        let (_,bytes)=pin_json(manifest_path,16*1024,deadline)?;
        let manifest:RootManifest=serde_json::from_slice(&bytes).map_err(|_|error("ROOT manifest has invalid or unknown fields"))?;
        manifest.validate(&policy,&policy_file.sha256)?;
        if boot_id()?!=manifest.boot_id {return Err(error("ROOT manifest belongs to another kernel boot"));}
        let engine_pin=dup_fd(engine_pidfd)?;
        if pidfd_pid(&engine_pin)?!=manifest.engine_pid as i32 {return Err(error("inherited Engine pidfd differs from ROOT manifest"));}
        live_same(&engine_pin,manifest.engine_pid as i32)?;
        let engine_file=pin_artifact(&policy.engine.exe_path,&policy.engine.exe_sha256,deadline)?;
        let config_file=pin_artifact(&policy.engine.config_path,&policy.engine.config_sha256,deadline)?;
        let authority_file=pin_artifact(&policy.authority.path,&policy.authority.sha256,deadline)?;
        let broker_file=pin_artifact(&policy.broker.path,&policy.broker.sha256,deadline)?;
        let this=File::open("/proc/self/exe")?.metadata()?;let installed=authority_file.file.metadata()?;
        if (this.dev(),this.ino())!=(installed.dev(),installed.ino()) {
            return Err(error("current ROOT verifier is not the administrator-pinned native executable"));
        }
        check_engine_config(&policy,&config_file.file)?;
        let quota=enforcement::load_root_enforcement_policy(&policy.session_id)?.verify_for_gateway()?;
        check_quota(&policy,&quota)?;
        let init_pin=open_pidfd(manifest.host_init_pid as i32)?;
        let init_scope=scope(manifest.host_init_pid as i32)?;
        let authority=Self{policy_sha256:policy_file.sha256.clone(),policy,manifest,policy_file,
            manifest_path:manifest_path.to_owned(),engine_file,config_file,authority_file,broker_file,engine_pin,init_pin,init_scope,quota};
        authority.revalidate()?;
        Ok(authority)
    }

    pub fn report(&self)->Value {
        serde_json::to_value(AuthorityReport{policy_sha256:self.policy_sha256.clone(),cid:self.manifest.cid.clone(),
            engine_pid:self.manifest.engine_pid,host_init_pid:self.manifest.host_init_pid,
            boot_id:self.manifest.boot_id.clone(),quota_ready:true,filter_ready:true}).unwrap_or(Value::Null)
    }

    pub fn revalidate(&self)->io::Result<()> {
        require_root()?;
        let deadline=Instant::now()+Duration::from_secs(self.policy.limits.operation_seconds);
        self.policy_file.revalidate(deadline)?;
        let (_,bytes)=pin_json(&self.manifest_path,16*1024,deadline)?;
        let current:RootManifest=serde_json::from_slice(&bytes).map_err(|_|error("ROOT manifest changed to an invalid shape"))?;
        current.validate(&self.policy,&self.policy_sha256)?;
        let mut core=current;core.gateway_pid=self.manifest.gateway_pid;core.status=self.manifest.status.clone();
        if core!=self.manifest || boot_id()?!=self.manifest.boot_id {
            return Err(error("pinned session or kernel boot changed"));
        }
        self.engine_file.revalidate(deadline)?;
        self.config_file.revalidate(deadline)?;
        self.authority_file.revalidate(deadline)?;
        self.broker_file.revalidate(deadline)?;
        check_engine(&self.policy,&self.manifest,&self.engine_pin,&self.engine_file.file)?;
        self.quota.revalidate()?;
        check_quota(&self.policy,&self.quota)?;
        live_same(&self.init_pin,self.manifest.host_init_pid as i32)?;
        let inspect=self.inspect(deadline)?;
        check_inspect(&self.policy,&self.manifest,&inspect)?;
        let reported=inspect.pointer("/State/Pid").and_then(Value::as_i64).filter(|p|*p>0&&*p<=i32::MAX as i64)
            .ok_or_else(||error("Engine did not report a live container init"))? as i32;
        if map_engine_pid(self.manifest.engine_pid as i32,reported)?!=self.manifest.host_init_pid as i32 {
            return Err(error("Engine-reported init differs from retained host init"));
        }
        let init=self.manifest.host_init_pid as i32;
        if scope(init)?!=self.init_scope {return Err(error("container init moved between namespace or cgroup scopes"));}
        check_kernel(&self.policy,init,&self.init_scope,&self.quota,&self.init_pin,deadline)?;
        live_same(&self.init_pin,init)?;
        live_same(&self.engine_pin,self.manifest.engine_pid as i32)?;
        remaining(deadline)?;
        Ok(())
    }

    pub fn require_connector(&self,stream:&UnixStream)->io::Result<()> {
        self.revalidate()?;
        let (cred,pin)=peer(stream)?;
        if cred.uid!=self.policy.engine.uid {return Err(error("connector is not the dedicated Engine identity"));}
        live_same(&pin,cred.pid)?;
        let observed=scope(cred.pid)?;
        if observed.namespaces!=self.init_scope.namespaces
            || !within(&observed.cgroup,&self.init_scope.cgroup) {
            return Err(error("connector is outside the pinned container namespace/cgroup"));
        }
        check_status(&read_proc(&format!("/proc/{}/status",cred.pid),64*1024)?,self.policy.engine.uid)?;
        let view=fs_view::Context::new(self.manifest.host_init_pid as i32,&self.init_pin)?;
        check_descriptors(cred.pid,true,&self.policy,&self.quota,&view,
            Instant::now()+Duration::from_secs(self.policy.limits.operation_seconds))?;
        live_same(&pin,cred.pid)?;
        live_same(&self.init_pin,self.manifest.host_init_pid as i32)?;
        Ok(())
    }

    fn inspect(&self,deadline:Instant)->io::Result<Value> {
        let socket=engine_socket(&self.policy)?;
        if (socket.dev(),socket.ino())!=(self.manifest.engine_socket_dev,self.manifest.engine_socket_ino) {
            return Err(error("reviewed Engine socket was replaced"));
        }
        let deadline=deadline.min(Instant::now()+Duration::from_secs(3));
        let mut stream=connect_deadline(&self.policy.engine.socket,deadline)?;
        let (cred,pin)=peer(&stream)?;
        if cred.uid!=self.policy.engine.uid || cred.gid!=self.policy.engine.gid
            || cred.pid!=self.manifest.engine_pid as i32 || !same_pidfd(&pin,&self.engine_pin)? {
            return Err(error("Engine HTTP peer differs from retained executable/pidfd identity"));
        }
        live_same(&pin,cred.pid)?;
        let request=format!("GET /v{}/containers/{}/json HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n",
            self.policy.engine.api_version,self.manifest.cid);
        write_deadline(&mut stream,request.as_bytes(),deadline)?;
        let bytes=read_http(&mut stream,deadline)?;
        live_same(&pin,cred.pid)?;
        let now=engine_socket(&self.policy)?;
        if (now.dev(),now.ino())!=(socket.dev(),socket.ino()) {return Err(error("Engine socket changed during request"));}
        serde_json::from_slice(&bytes).map_err(|_|error("pinned Engine inspect is malformed JSON"))
    }
}

fn require_root()->io::Result<()> {
    if unsafe{libc::geteuid()}!=0 {return Err(error("hardened authority requires ROOT"));}
    for name in ["mnt","user","cgroup"] {
        if fs::read_link(format!("/proc/self/ns/{name}"))?!=fs::read_link(format!("/proc/1/ns/{name}"))? {
            return Err(error("ROOT verifier must share initial host mount/user/cgroup namespace"));
        }
    }
    match read_proc("/proc/self/attr/current",4096) {
        Ok(label) if label.trim()!="unconfined"=>return Err(error("privilege-dropped access proof requires an unconfined ROOT verifier")),
        Err(e) if !matches!(e.raw_os_error(),Some(libc::EINVAL)|Some(libc::ENOENT))=>return Err(e),
        _=>{}
    }Ok(())
}
fn remaining(deadline:Instant)->io::Result<Duration> {
    deadline.checked_duration_since(Instant::now()).filter(|d|!d.is_zero())
        .ok_or_else(||io::Error::new(io::ErrorKind::TimedOut,"hardened authority total deadline expired"))
}
fn dup_fd(fd:RawFd)->io::Result<File> {let copied=unsafe{libc::fcntl(fd,libc::F_DUPFD_CLOEXEC,3)};
    if copied<0 {return Err(io::Error::last_os_error());} Ok(unsafe{File::from_raw_fd(copied)})}
fn open_pidfd(pid:i32)->io::Result<File> {let fd=unsafe{libc::syscall(libc::SYS_pidfd_open,pid,0)};
    if fd<0 {return Err(io::Error::last_os_error());}Ok(unsafe{File::from_raw_fd(fd as RawFd)})}
fn same_pidfd(a:&File,b:&File)->io::Result<bool> {let a=a.metadata()?;let b=b.metadata()?;Ok((a.dev(),a.ino())==(b.dev(),b.ino()))}
fn pidfd_pid(fd:&File)->io::Result<i32> {
    let raw=read_proc(&format!("/proc/self/fdinfo/{}",fd.as_raw_fd()),4096)?;
    let mut p=raw.lines().filter_map(|l|l.strip_prefix("Pid:").map(str::trim));
    let pid=p.next().and_then(|v|v.parse::<i32>().ok()).filter(|p|*p>0).ok_or_else(||error("live pidfd PID unavailable"))?;
    if p.next().is_some(){return Err(error("pidfd record has duplicate PIDs"));} Ok(pid)
}
fn live_same(fd:&File,pid:i32)->io::Result<()> {
    let mut poll=libc::pollfd{fd:fd.as_raw_fd(),events:libc::POLLIN,revents:0};
    if unsafe{libc::poll(&mut poll,1,0)}!=0 || pidfd_pid(fd)?!=pid || !same_pidfd(fd,&open_pidfd(pid)?)? {
        return Err(error("retained process exited or changed identity"));
    } Ok(())
}
fn peer(stream:&UnixStream)->io::Result<(libc::ucred,File)> {
    let mut cred:libc::ucred=unsafe{mem::zeroed()};let mut len=mem::size_of_val(&cred) as libc::socklen_t;
    if unsafe{libc::getsockopt(stream.as_raw_fd(),libc::SOL_SOCKET,libc::SO_PEERCRED,(&mut cred as *mut libc::ucred).cast(),&mut len)}!=0 {
        return Err(io::Error::last_os_error());
    }
    if len as usize!=mem::size_of_val(&cred)||cred.pid<=0 {return Err(error("Unix peer credentials incomplete"));}
    let mut fd=-1;len=mem::size_of_val(&fd) as libc::socklen_t;
    if unsafe{libc::getsockopt(stream.as_raw_fd(),libc::SOL_SOCKET,SO_PEERPIDFD,(&mut fd as *mut RawFd).cast(),&mut len)}!=0 {
        return Err(io::Error::last_os_error());
    }
    if fd<0||len as usize!=mem::size_of_val(&fd){if fd>=0{unsafe{libc::close(fd);}}return Err(error("Unix peer pidfd incomplete"));}
    let pin=unsafe{File::from_raw_fd(fd)};live_same(&pin,cred.pid)?;Ok((cred,pin))
}

fn read_bytes(file:&File,max:u64)->io::Result<Vec<u8>> {
    let mut bytes=Vec::new();file.take(max+1).read_to_end(&mut bytes)?;
    if bytes.len() as u64>max {return Err(error("authority file exceeds bounded read"));}Ok(bytes)
}
fn read_proc(path:&str,max:usize)->io::Result<String> {
    String::from_utf8(read_bytes(&File::open(path)?,max as u64)?).map_err(|_|error("kernel record is not UTF-8"))
}
fn boot_id()->io::Result<String>{Ok(read_proc("/proc/sys/kernel/random/boot_id",128)?.trim().to_owned())}

fn open_directory(path:&Path,engine_uid:Option<u32>)->io::Result<File> {
    if !path.is_absolute(){return Err(error("controlled path is not absolute"));}
    let fd=unsafe{libc::open(c"/".as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_CLOEXEC)};
    if fd<0{return Err(io::Error::last_os_error());}let mut dir=unsafe{File::from_raw_fd(fd)};
    for part in path.components().skip(1) {
        let Component::Normal(name)=part else{return Err(error("controlled path contains a noncanonical component"));};
        let name=CString::new(name.as_bytes()).map_err(|_|error("controlled path contains NUL"))?;
        let fd=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
        if fd<0{return Err(io::Error::last_os_error());}dir=unsafe{File::from_raw_fd(fd)};
        let meta=dir.metadata()?;
        if (meta.uid()!=0&&Some(meta.uid())!=engine_uid)||meta.mode()&0o022!=0 {
            return Err(error("controlled path ancestor is writable outside ROOT/Engine authority"));
        }
    } Ok(dir)
}
fn root_file(path:&Path,max:u64,private:bool)->io::Result<File> {
    let dir=open_directory(path.parent().ok_or_else(||error("controlled file has no parent"))?,None)?;
    let name=CString::new(path.file_name().ok_or_else(||error("controlled file has no name"))?.as_bytes()).map_err(|_|error("file contains NUL"))?;
    let fd=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK)};
    if fd<0{return Err(io::Error::last_os_error());}let file=unsafe{File::from_raw_fd(fd)};let m=file.metadata()?;
    if !m.is_file()||m.uid()!=0||m.nlink()!=1||m.mode()&0o022!=0||m.mode()&0o7000!=0
        ||(private&&m.mode()&0o077!=0)||m.len()==0||m.len()>max {
        return Err(error("authority input is not a bounded single-link ROOT-controlled regular file"));
    } Ok(file)
}
fn hash_file(file:&File,max:u64,deadline:Instant)->io::Result<String> {
    let before=Identity::of(file)?;let mut source=file.try_clone()?;
    use std::io::Seek;source.seek(io::SeekFrom::Start(0))?;
    let mut hash=Sha256::new();let mut bytes=[0u8;65536];let mut total=0;
    loop {remaining(deadline)?;let n=source.read(&mut bytes)?;if n==0{break;}total+=n as u64;
        if total>max{return Err(error("hashed authority artifact exceeds bound"));}hash.update(&bytes[..n]);}
    if Identity::of(file)?!=before||total!=before.len{return Err(error("authority artifact changed during hash"));}
    Ok(format!("{:x}",hash.finalize()))
}
fn pin_json(path:&Path,max:u64,deadline:Instant)->io::Result<(PinnedFile,Vec<u8>)> {
    let file=root_file(path,max,true)?;let identity=Identity::of(&file)?;let bytes=read_bytes(&file,max)?;remaining(deadline)?;
    if Identity::of(&file)?!=identity{return Err(error("ROOT input changed while loading"));}
    let sha256=format!("{:x}",Sha256::digest(&bytes));
    Ok((PinnedFile{path:path.to_owned(),file,identity,sha256},bytes))
}
fn pin_artifact(path:&Path,expected:&str,deadline:Instant)->io::Result<PinnedFile> {
    let file=root_file(path,MAX_ARTIFACT,false)?;let identity=Identity::of(&file)?;let sha256=hash_file(&file,MAX_ARTIFACT,deadline)?;
    if sha256!=expected{return Err(error("ROOT artifact SHA differs from administrator content pin"));}
    Ok(PinnedFile{path:path.to_owned(),file,identity,sha256})
}

fn engine_socket(policy:&Policy)->io::Result<fs::Metadata> {
    let path=&policy.engine.socket;
    let dir=open_directory(path.parent().ok_or_else(||error("Engine socket parent missing"))?,Some(policy.engine.uid))?;
    let name=CString::new(path.file_name().unwrap().as_bytes()).map_err(|_|error("Engine socket name contains NUL"))?;
    let mut m:libc::stat=unsafe{mem::zeroed()};
    if unsafe{libc::fstatat(dir.as_raw_fd(),name.as_ptr(),&mut m,libc::AT_SYMLINK_NOFOLLOW)}!=0{return Err(io::Error::last_os_error());}
    let meta=fs::symlink_metadata(path)?;
    if !meta.file_type().is_socket()||meta.uid()!=policy.engine.uid||meta.gid()!=policy.engine.gid
        ||meta.mode()&0o7777!=0o600||meta.nlink()!=1
        ||(meta.dev(),meta.ino())!=(m.st_dev,m.st_ino) {
        return Err(error("Engine API socket is not private to the pinned dedicated identity"));
    }Ok(meta)
}
fn check_engine_config(policy:&Policy,file:&File)->io::Result<()> {
    use std::io::Seek;let mut f=file.try_clone()?;f.seek(io::SeekFrom::Start(0))?;
    let config:Value=serde_json::from_slice(&read_bytes(&f,64*1024)?).map_err(|_|error("reviewed Engine config is not JSON"))?;
    if config.get("hosts")!=Some(&json!([format!("unix://{}",policy.engine.socket.display())]))
        || !config.get("exec-opts").and_then(Value::as_array).is_some_and(|v|v.contains(&json!("native.cgroupdriver=systemd"))) {
        return Err(error("reviewed Engine configuration exposes another API or lacks systemd cgroups"));
    } Ok(())
}
fn check_engine(policy:&Policy,manifest:&RootManifest,pin:&File,exe:&File)->io::Result<()> {
    let pid=manifest.engine_pid as i32;live_same(pin,pid)?;
    let actual=File::open(format!("/proc/{pid}/exe"))?;
    let a=actual.metadata()?;let b=exe.metadata()?;
    if (a.dev(),a.ino())!=(b.dev(),b.ino()) {return Err(error("live Engine executable differs from ROOT-owned artifact"));}
    let status=read_proc(&format!("/proc/{pid}/status"),64*1024)?;
    if four_ids(&status,"Uid:")?!=[policy.engine.uid;4]||four_ids(&status,"Gid:")?!=[policy.engine.gid;4] {
        return Err(error("live Engine identity differs from policy"));
    }
    let cmd=read_bytes(&File::open(format!("/proc/{pid}/cmdline"))?,8192)?;
    let argv:Vec<&[u8]>=cmd.split(|b|*b==0).filter(|v|!v.is_empty()).collect();
    let flag=format!("--config-file={}",policy.engine.config_path.display());
    let pair=argv.len()==3&&argv[1]==b"--config-file"&&argv[2]==policy.engine.config_path.as_os_str().as_bytes();
    if !(argv.len()==2&&argv[1]==flag.as_bytes()||pair) {return Err(error("live Engine command line does not exclusively use reviewed ROOT config"));}
    let user=fs::read_link(format!("/proc/{pid}/ns/user"))?;
    if user==fs::read_link("/proc/self/ns/user")? {return Err(error("Engine is not rootless in a private user namespace"));}
    check_uid_map(&read_proc(&format!("/proc/{pid}/uid_map"),8192)?,policy)?;
    live_same(pin,pid)
}

fn connect_deadline(path:&Path,deadline:Instant)->io::Result<UnixStream> {
    let bytes=path.as_os_str().as_bytes();let mut addr:libc::sockaddr_un=unsafe{mem::zeroed()};
    if bytes.len()>=addr.sun_path.len(){return Err(error("Engine Unix socket path exceeds kernel bound"));}
    addr.sun_family=libc::AF_UNIX as libc::sa_family_t;
    for (to,from) in addr.sun_path.iter_mut().zip(bytes){*to=*from as libc::c_char;}
    let fd=unsafe{libc::socket(libc::AF_UNIX,libc::SOCK_STREAM|libc::SOCK_NONBLOCK|libc::SOCK_CLOEXEC,0)};
    if fd<0{return Err(io::Error::last_os_error());}let stream=unsafe{UnixStream::from_raw_fd(fd)};
    let result=unsafe{libc::connect(fd,(&addr as *const libc::sockaddr_un).cast(),mem::size_of_val(&addr) as libc::socklen_t)};
    if result<0 {
        let e=io::Error::last_os_error();if !matches!(e.raw_os_error(),Some(libc::EINPROGRESS)|Some(libc::EAGAIN)){return Err(e);}
        loop {let ms=remaining(deadline)?.as_millis().min(i32::MAX as u128).max(1) as i32;
            let mut p=libc::pollfd{fd,events:libc::POLLOUT,revents:0};let n=unsafe{libc::poll(&mut p,1,ms)};
            if n<0{let e=io::Error::last_os_error();if e.kind()==io::ErrorKind::Interrupted{continue;}return Err(e);}
            if n==0{return Err(io::Error::new(io::ErrorKind::TimedOut,"Engine connect deadline expired"));}
            let mut status=0;let mut len=mem::size_of_val(&status) as libc::socklen_t;
            if unsafe{libc::getsockopt(fd,libc::SOL_SOCKET,libc::SO_ERROR,(&mut status as *mut i32).cast(),&mut len)}!=0{return Err(io::Error::last_os_error());}
            if status!=0{return Err(io::Error::from_raw_os_error(status));}break;
        }
    }
    stream.set_nonblocking(false)?;remaining(deadline)?;Ok(stream)
}
fn write_deadline(stream:&mut UnixStream,mut bytes:&[u8],deadline:Instant)->io::Result<()> {
    while !bytes.is_empty(){stream.set_write_timeout(Some(remaining(deadline)?))?;let n=stream.write(bytes)?;
        if n==0{return Err(io::Error::new(io::ErrorKind::WriteZero,"Engine closed while request was sent"));}bytes=&bytes[n..];}Ok(())
}
fn read_http(stream:&mut UnixStream,deadline:Instant)->io::Result<Vec<u8>> {
    let mut bytes=Vec::new();let end=loop{
        if bytes.len()>=16384{return Err(error("Engine HTTP header exceeds bound"));}
        stream.set_read_timeout(Some(remaining(deadline)?))?;let mut b=[0u8;1];stream.read_exact(&mut b)?;bytes.push(b[0]);
        if bytes.ends_with(b"\r\n\r\n"){break bytes.len();}
    };
    let header=std::str::from_utf8(&bytes).map_err(|_|error("Engine HTTP header is not ASCII/UTF-8"))?;
    match http_framing(header)? {
        HttpBody::Length(length)=>{
            bytes.resize(end+length,0);read_exact_deadline(stream,&mut bytes[end..],deadline)?;
            remaining(deadline)?;Ok(bytes.split_off(end))
        },
        HttpBody::Chunked=>read_chunks(stream,deadline),
    }
}
#[derive(Debug,PartialEq,Eq)]enum HttpBody{Length(usize),Chunked}
fn http_framing(header:&str)->io::Result<HttpBody> {
    let mut lines=header.split("\r\n");
    if lines.next()!=Some("HTTP/1.1 200 OK"){return Err(error("Engine inspect did not return exact HTTP 200"));}
    let mut length=None;let mut chunked=false;
    for line in lines.filter(|l|!l.is_empty()) {
        let (name,value)=line.split_once(':').ok_or_else(||error("malformed Engine HTTP header"))?;
        if name.is_empty()||!name.bytes().all(|b|b.is_ascii_alphanumeric()||b"!#$%&'*+-.^_`|~".contains(&b)) {
            return Err(error("Engine HTTP header name is malformed"));
        }
        if name.eq_ignore_ascii_case("transfer-encoding"){
            if chunked||!value.trim().eq_ignore_ascii_case("chunked") {
                return Err(error("Engine transfer encoding is duplicated or unsupported"));
            }chunked=true;
        }
        if name.eq_ignore_ascii_case("content-length") {
            let value=value.trim();if length.is_some()||value.is_empty()||!value.bytes().all(|b|b.is_ascii_digit()){return Err(error("Engine body length is ambiguous"));}
            let parsed=value.parse::<usize>().map_err(|_|error("Engine body length overflow"))?;
            if parsed==0||parsed>MAX_JSON{return Err(error("Engine body exceeds finite inspect bound"));}length=Some(parsed);
        }
    }
    match (length,chunked) {
        (Some(_),true)=>Err(error("Engine HTTP body has ambiguous length and transfer encoding")),
        (Some(length),false)=>Ok(HttpBody::Length(length)),(None,true)=>Ok(HttpBody::Chunked),
        (None,false)=>Err(error("Engine HTTP body has no explicit bounded framing")),
    }
}

fn read_exact_deadline(stream:&mut UnixStream,mut bytes:&mut[u8],deadline:Instant)->io::Result<()> {
    while !bytes.is_empty(){stream.set_read_timeout(Some(remaining(deadline)?))?;let n=stream.read(bytes)?;
        if n==0{return Err(error("Engine body ended before declared length"));}
        bytes=&mut bytes[n..];
    }remaining(deadline)?;Ok(())
}
fn read_chunks(stream:&mut UnixStream,deadline:Instant)->io::Result<Vec<u8>> {
    let mut output=Vec::new();
    for _ in 0..256 {
        let mut line=Vec::new();
        while !line.ends_with(b"\r\n") {
            if line.len()>=16{return Err(error("Engine chunk header exceeds bound"));}
            let mut byte=[0u8];read_exact_deadline(stream,&mut byte,deadline)?;line.push(byte[0]);
        }
        let number=&line[..line.len()-2];
        if number.is_empty()||number.len()>8||!number.iter().all(u8::is_ascii_hexdigit) {
            return Err(error("Engine chunk size/extensions are invalid"));
        }
        let size=usize::from_str_radix(std::str::from_utf8(number).map_err(|_|error("Engine chunk size is not ASCII"))?,16)
            .map_err(|_|error("Engine chunk size overflows"))?;
        if size>MAX_JSON-output.len(){return Err(error("Engine chunked inspect body exceeds finite bound"));}
        if size==0 {
            let mut delimiter=[0u8;2];read_exact_deadline(stream,&mut delimiter,deadline)?;
            if delimiter!=*b"\r\n"{return Err(error("Engine chunk trailers are refused"));}
            if output.is_empty(){return Err(error("Engine inspect body is empty"));}return Ok(output);
        }
        let start=output.len();output.resize(start+size,0);read_exact_deadline(stream,&mut output[start..],deadline)?;
        let mut delimiter=[0u8;2];read_exact_deadline(stream,&mut delimiter,deadline)?;
        if delimiter!=*b"\r\n"{return Err(error("Engine chunk delimiter is malformed"));}
    }Err(error("Engine chunk count exceeds finite inspect bound"))
}

fn check_quota(policy:&Policy,q:&EnforcedQuotaSnapshot)->io::Result<()> {
    let p=&q.policy;
    if p.session_id!=policy.session_id||p.root!=policy.root||p.caller_uid!=policy.caller_uid
        ||p.engine_uid!=policy.engine.uid||p.engine_gid!=policy.engine.gid
        ||p.worker_image!=policy.image||p.worker_binary_sha256!=policy.worker.sha256
        ||p.hard_limit_bytes!=policy.limits.disk_bytes||p.security_contract_version!=1 {
        return Err(error("retained kernel quota capability differs from exact worker policy"));
    }Ok(())
}
fn expect(info:&Value,pointer:&str,value:Value)->io::Result<()> {
    if info.pointer(pointer)!=Some(&value){return Err(error(format!("pinned Engine inspect differs at {pointer}")));}Ok(())
}
fn empty(info:&Value,pointer:&str)->bool {
    match info.pointer(pointer) {None|Some(Value::Null)=>true,Some(Value::Array(v))=>v.is_empty(),
        Some(Value::Object(v))=>v.is_empty(),Some(Value::String(v))=>v.is_empty(),_=>false}
}
fn check_inspect(policy:&Policy,m:&RootManifest,info:&Value)->io::Result<()> {
    let l=&policy.limits;
    for (key,value) in [("/Id",json!(m.cid)),("/Image",json!(policy.image)),
        ("/Config/Labels/io.doxa.root-session",json!(policy.session_id)),
        ("/Config/Labels/io.doxa.root-policy",json!(m.policy_sha256)),
        ("/Path",json!(policy.worker.path)),("/Args",json!(["hold-hardened"])),
        ("/Config/Entrypoint",json!([policy.worker.path])),("/Config/Cmd",json!(["hold-hardened"])),
        ("/Config/User",json!("0:0")),("/Config/WorkingDir",json!("/workspace")),
        ("/Config/Tty",json!(false)),("/Config/OpenStdin",json!(false)),
        ("/HostConfig/ReadonlyRootfs",json!(true)),("/HostConfig/NetworkMode",json!("none")),
        ("/HostConfig/Privileged",json!(false)),("/HostConfig/CapDrop",json!(["ALL"])),
        ("/HostConfig/SecurityOpt",json!(["no-new-privileges:true"])),
        ("/HostConfig/Memory",json!(l.memory_bytes)),("/HostConfig/MemorySwap",json!(l.memory_bytes)),
        ("/HostConfig/NanoCpus",json!(l.cpu_quota*1_000_000_000/l.cpu_period)),
        ("/HostConfig/PidsLimit",json!(l.pids)),("/HostConfig/PidMode",json!("")),
        ("/HostConfig/IpcMode",json!("none")),("/HostConfig/CgroupnsMode",json!("private")),
        ("/HostConfig/AutoRemove",json!(false)),("/State/Running",json!(true))] {expect(info,key,value)?;}
    for key in ["/HostConfig/CapAdd","/HostConfig/Devices","/HostConfig/DeviceRequests","/HostConfig/Links",
        "/HostConfig/VolumesFrom","/HostConfig/ExtraHosts","/HostConfig/PortBindings","/HostConfig/Dns",
        "/HostConfig/DnsOptions","/HostConfig/DnsSearch","/HostConfig/GroupAdd","/HostConfig/Sysctls",
        "/HostConfig/UTSMode","/HostConfig/UsernsMode","/Config/Volumes","/Config/Healthcheck"] {
        if !empty(info,key){return Err(error("Engine inspect includes unreviewed process/device/mount authority"));}
    }
    if info.pointer("/HostConfig/PublishAllPorts")==Some(&json!(true)) {
        return Err(error("Engine published a hardened worker port"));
    }
    expect(info,"/HostConfig/Tmpfs",json!({"/tmp":"rw,noexec,nosuid,nodev,size=65536k,mode=1777"}))?;
    expect(info,"/HostConfig/RestartPolicy",json!({"Name":"no","MaximumRetryCount":0}))?;
    let mounts=info.get("Mounts").and_then(Value::as_array).ok_or_else(||error("Engine mount inventory unavailable"))?;
    if mounts.len()!=4 {return Err(error("hardened worker has an extra/missing host mount"));}
    let mut expected=BTreeMap::from([
        ("/workspace",(policy.root.join("checkout"),true)),("/home/doxa",(policy.root.join("home"),true)),
        ("/work-cache",(policy.root.join("cache"),true)),("/run/doxa/session",(policy.root.join("broker"),false))]);
    for mount in mounts {
        let target=mount.get("Destination").and_then(Value::as_str).ok_or_else(||error("Engine mount target missing"))?;
        let (source,rw)=expected.remove(target).ok_or_else(||error("Engine mount target unknown or duplicated"))?;
        if mount.get("Type")!=Some(&json!("bind"))||mount.get("Source")!=Some(&json!(source))
            ||mount.get("RW")!=Some(&json!(rw))||mount.get("Propagation")!=Some(&json!("rprivate")) {
            return Err(error("Engine mounted directory outside retained quota scope"));
        }
    }
    let networks=info.pointer("/NetworkSettings/Networks").and_then(Value::as_object)
        .ok_or_else(||error("Engine network inventory missing"))?;
    if networks.keys().any(|key|key!="none")||networks.values().any(|n|["IPAddress","Gateway","GlobalIPv6Address","IPv6Gateway"]
        .iter().any(|k|!empty(n,&format!("/{k}")))){return Err(error("Engine worker has external network configuration"));}
    Ok(())
}

fn value<'a>(status:&'a str,key:&str)->io::Result<&'a str> {
    let mut values=status.lines().filter_map(|l|l.strip_prefix(key).map(str::trim));
    let first=values.next().ok_or_else(||error(format!("kernel status has no {key}")))?;
    if values.next().is_some(){return Err(error("kernel status field duplicated"));}Ok(first)
}
fn four_ids(status:&str,key:&str)->io::Result<[u32;4]> {
    let ids=value(status,key)?.split_whitespace().map(str::parse::<u32>).collect::<Result<Vec<_>,_>>()
        .map_err(|_|error("kernel credentials are malformed"))?;
    ids.try_into().map_err(|_|error("kernel credentials are incomplete"))
}
fn nspids(pid:i32)->io::Result<Vec<i32>> {
    let status=read_proc(&format!("/proc/{pid}/status"),64*1024)?;
    let ids=value(&status,"NSpid:")?.split_whitespace().map(str::parse::<i32>).collect::<Result<Vec<_>,_>>()
        .map_err(|_|error("kernel namespace PID list malformed"))?;
    if ids.is_empty()||ids.len()>32||ids.iter().any(|p|*p<=0){return Err(error("kernel namespace PID list incomplete"));}Ok(ids)
}
fn map_engine_pid(engine_pid:i32,reported:i32)->io::Result<i32> {
    let depth=nspids(engine_pid)?.len();let mut found=None;let mut count=0;
    for entry in fs::read_dir("/proc")? {
        let entry=entry?;let Some(pid)=entry.file_name().to_str().and_then(|s|s.parse::<i32>().ok()) else{continue;};
        count+=1;if count>65536{return Err(error("host PID inventory exceeds bound"));}
        let ids=match nspids(pid){Ok(ids)=>ids,Err(e) if matches!(e.kind(),io::ErrorKind::NotFound|io::ErrorKind::PermissionDenied)=>continue,Err(e)=>return Err(e)};
        if ids.len()>depth&&ids[depth-1]==reported {
            if found.replace(pid).is_some(){return Err(error("Engine-reported namespace PID is ambiguous"));}
        }
    }found.ok_or_else(||error("Engine-reported init has no host PID mapping"))
}
fn scope(pid:i32)->io::Result<Scope> {
    let mut namespaces=BTreeMap::new();
    for name in ["pid","mnt","net","user","cgroup","ipc","uts"] {
        namespaces.insert(name.into(),fs::read_link(format!("/proc/{pid}/ns/{name}"))?);
    }
    let cgroup=parse_cgroup(&read_proc(&format!("/proc/{pid}/cgroup"),8192)?)?;
    Ok(Scope{namespaces,cgroup})
}
fn parse_cgroup(raw:&str)->io::Result<String> {
    let mut lines=raw.lines();let name=lines.next().and_then(|l|l.strip_prefix("0::"))
        .ok_or_else(||error("authority requires cgroup v2"))?;
    if lines.next().is_some()||name=="/"||!name.starts_with('/')||name.contains("//")
        ||name.split('/').skip(1).any(|p|p.is_empty()||p=="."||p=="..") {
        return Err(error("container cgroup is not an exact private scope"));
    }Ok(name.to_owned())
}
fn within(candidate:&str,root:&str)->bool {candidate==root||candidate.starts_with(&format!("{root}/"))}
fn check_uid_map(raw:&str,policy:&Policy)->io::Result<()> {
    let mut zero=false;let mut count=0;
    for row in raw.lines(){count+=1;if count>16{return Err(error("user namespace map exceeds bound"));}
        let fields=row.split_whitespace().map(str::parse::<u64>).collect::<Result<Vec<_>,_>>().map_err(|_|error("user namespace map malformed"))?;
        if fields.len()!=3||fields[2]==0{return Err(error("user namespace map incomplete"));}
        let end=fields[1].checked_add(fields[2]).ok_or_else(||error("user namespace map overflow"))?;
        if fields[1]==0||(fields[1]<=policy.caller_uid as u64&&end>policy.caller_uid as u64) {
            return Err(error("container user namespace maps ROOT or client identity"));
        }
        if fields[0]==0 {if fields[1]!=policy.engine.uid as u64||fields[2]!=1{return Err(error("container root does not map exclusively to Engine UID"));}zero=true;}
    }
    if !zero{return Err(error("user namespace has no pinned root mapping"));}Ok(())
}
fn check_status(status:&str,engine_uid:u32)->io::Result<()> {
    if four_ids(status,"Uid:")?!=[engine_uid;4]||value(status,"NoNewPrivs:")?!="1"
        ||value(status,"Seccomp:")?!="2"||value(status,"Seccomp_filters:")?.parse::<u64>().unwrap_or(0)<2 {
        return Err(error("worker thread lacks mandatory UID/no-new-privileges/seccomp contract"));
    }
    for key in ["CapInh:","CapPrm:","CapEff:","CapBnd:","CapAmb:"] {
        if u64::from_str_radix(value(status,key)?,16).ok()!=Some(0){return Err(error("worker thread retains namespace/process authority capabilities"));}
    }
    if value(status,"TracerPid:")?!="0" {return Err(error("worker thread is ptrace-controlled"));}Ok(())
}

fn finite(raw:&str,name:&str)->io::Result<u64> {
    let raw=raw.trim();if raw.is_empty()||!raw.bytes().all(|b|b.is_ascii_digit()){return Err(error(format!("{name} is not finite")));}
    raw.parse().map_err(|_|error("cgroup limit overflow"))
}
fn check_limits(policy:&Policy,cgroup:&str)->io::Result<()> {
    let root=Path::new("/sys/fs/cgroup").join(cgroup.trim_start_matches('/'));
    let read=|name:&str|read_proc(root.join(name).to_str().ok_or_else(||error("cgroup path UTF-8 unavailable"))?,128);
    for (name,max) in [("memory.max",policy.limits.memory_bytes),("pids.max",policy.limits.pids as u64)] {
        let actual=finite(&read(name)?,name)?;if actual==0||actual>max{return Err(error("effective cgroup ceiling exceeds administrator policy"));}
    }
    if finite(&read("memory.swap.max")?,"memory.swap.max")?!=0{return Err(error("worker cgroup permits unreviewed swap"));}
    let raw=read("cpu.max")?;let mut fields=raw.split_whitespace();
    let quota=finite(fields.next().ok_or_else(||error("cpu.max quota missing"))?,"cpu.max")?;
    let period=finite(fields.next().ok_or_else(||error("cpu.max period missing"))?,"cpu.max")?;
    if fields.next().is_some()||quota==0||period==0
        ||u128::from(quota)*u128::from(policy.limits.cpu_period)>u128::from(policy.limits.cpu_quota)*u128::from(period) {
        return Err(error("effective CPU cgroup ceiling exceeds administrator policy"));
    }Ok(())
}

#[derive(Debug)]
struct Mount {id:u64,target:PathBuf,kind:String,options:BTreeSet<String>,super_options:BTreeSet<String>}
fn decode_mount(raw:&str)->io::Result<PathBuf> {
    let mut out=Vec::new();let mut bytes=raw.as_bytes().iter().copied();
    while let Some(b)=bytes.next(){if b==b'\\'{let a=bytes.next().ok_or_else(||error("mount escape truncated"))?;
        let b=bytes.next().ok_or_else(||error("mount escape truncated"))?;let c=bytes.next().ok_or_else(||error("mount escape truncated"))?;
        if ![a,b,c].iter().all(|v|(b'0'..=b'7').contains(v)){return Err(error("mount escape malformed"));}
        out.push((a-b'0')*64+(b-b'0')*8+(c-b'0'));}else{out.push(b);}}
    if out.contains(&0){return Err(error("mount target has NUL"));}Ok(PathBuf::from(OsStr::from_bytes(&out)))
}
fn mount_inventory(raw:&str)->io::Result<Vec<Mount>> {
    let mut entries=Vec::new();let mut targets=BTreeSet::new();
    for line in raw.lines(){if entries.len()>=256{return Err(error("container mount inventory exceeds bound"));}
        let (left,right)=line.split_once(" - ").ok_or_else(||error("kernel mount record malformed"))?;
        let left=left.split_whitespace().collect::<Vec<_>>();let right=right.split_whitespace().collect::<Vec<_>>();
        if left.len()<6||right.len()!=3||left[6..].iter().any(|v|v.starts_with("shared:")||v.starts_with("master:")) {
            return Err(error("worker mount is malformed or shares propagation with host"));
        }
        let target=decode_mount(left[4])?;
        if !targets.insert(target.clone()){return Err(error("worker has stacked/ambiguous mounts"));}
        let id=left[0].parse::<u64>().ok().filter(|id|*id!=0).ok_or_else(||error("kernel mount ID malformed"))?;
        entries.push(Mount{id,target,kind:right[0].into(),options:left[5].split(',').map(str::to_owned).collect(),
            super_options:right[2].split(',').map(str::to_owned).collect()});
    }Ok(entries)
}
fn check_mounts(pid:i32,q:&EnforcedQuotaSnapshot,view:&fs_view::Context<'_>,deadline:Instant)->io::Result<()> {
    let mounts=mount_inventory(&read_proc(&format!("/proc/{pid}/mountinfo"),MAX_PROC)?)?;
    let root=mounts.iter().find(|m|m.target==Path::new("/")).ok_or_else(||error("worker root mount missing"))?;
    if !root.options.contains("ro"){return Err(error("worker image root is writable"));}
    let cgroup=mounts.iter().find(|m|m.target==Path::new("/sys/fs/cgroup")).ok_or_else(||error("worker cgroup mount missing"))?;
    if cgroup.kind!="cgroup2"||!cgroup.options.contains("ro")||!cgroup.options.contains("nosuid")||!cgroup.options.contains("nodev") {
        return Err(error("worker can mutate cgroup migration/control filesystem"));
    }
    let tmp=mounts.iter().find(|m|m.target==Path::new("/tmp")).ok_or_else(||error("worker bounded temporary mount missing"))?;
    if tmp.kind!="tmpfs"||!["noexec","nosuid","nodev"].iter().all(|v|tmp.options.contains(*v))
        ||!tmp.super_options.contains("size=65536k") {return Err(error("worker temporary filesystem lacks fixed bound/restrictions"));}
    for (target,file,rw) in [("/workspace",&q.tree.checkout,true),("/home/doxa",&q.tree.home,true),
        ("/work-cache",&q.tree.cache,true),("/run/doxa/session",&q.tree.broker,false)] {
        let a=view.read(fs_view::Operation::RootMetadata{pid,path:target.into(),nofollow:true},deadline)?;let b=file.metadata()?;
        if !a.directory()||(a.device,a.inode)!=(b.dev(),b.ino()) {return Err(error("actual worker bind differs from retained kernel quota directory"));}
        let mounted=mounts.iter().find(|m|m.target==Path::new(target)).ok_or_else(||error("actual worker bind mount absent"))?;
        if !mounted.options.contains(if rw{"rw"}else{"ro"}) {return Err(error("actual worker bind write policy differs"));}
    }
    for m in &mounts {
        let known=matches!(m.target.to_str(),Some("/"|"/workspace"|"/home/doxa"|"/work-cache"|"/run/doxa/session"|"/tmp"
            |"/etc/hosts"|"/etc/hostname"|"/etc/resolv.conf"|"/dev"|"/dev/pts"|"/dev/shm"|"/dev/mqueue"));
        let kernel=m.target.starts_with("/proc")||m.target.starts_with("/sys");
        if !known&&!kernel{return Err(error("actual worker has an unreviewed accessible host mount"));}
        if m.target.starts_with("/sys")&&!m.options.contains("ro"){return Err(error("worker sysfs/control mount is writable"));}
        if kernel&&!matches!(m.kind.as_str(),"proc"|"sysfs"|"cgroup2"|"tmpfs") {
            return Err(error("worker kernel namespace contains a foreign filesystem mount"));
        }
    }Ok(())
}
fn check_network(pid:i32)->io::Result<()> {
    let raw=read_proc(&format!("/proc/{pid}/net/dev"),64*1024)?;
    let devices=raw.lines().skip(2).filter_map(|l|l.split_once(':').map(|(n,_)|n.trim())).collect::<Vec<_>>();
    if devices!=["lo"]{return Err(error("worker has a network interface other than private loopback"));}
    let v4=read_proc(&format!("/proc/{pid}/net/route"),64*1024)?;
    if v4.lines().skip(1).any(|l|!l.trim().is_empty()){return Err(error("worker has an IPv4 route"));}
    let v6=read_proc(&format!("/proc/{pid}/net/ipv6_route"),64*1024)?;
    for row in v6.lines(){let fields=row.split_whitespace().collect::<Vec<_>>();
        let loopback=fields.len()==10&&fields[0]=="00000000000000000000000000000001"&&fields[1]=="80";
        let unreachable=fields.len()==10&&fields[0]=="00000000000000000000000000000000"&&fields[1]=="00"
            &&u32::from_str_radix(fields[8],16).ok().is_some_and(|flags|flags&0x200!=0);
        if fields.len()!=10||fields[9]!="lo"||(!loopback&&!unreachable)
            ||fields[2]!="00000000000000000000000000000000"||fields[3]!="00"||fields[4]!="00000000000000000000000000000000" {
            return Err(error("worker has a non-loopback IPv6 route"));
        }
    }Ok(())
}
fn check_descriptors(pid:i32,allow_socket:bool,policy:&Policy,quota:&EnforcedQuotaSnapshot,
    view:&fs_view::Context<'_>,deadline:Instant)->io::Result<()> {
    let mut count=0;
    for fd in fs::read_dir(format!("/proc/{pid}/fd"))? {let fd=fd?;count+=1;
        if count>4096{return Err(error("worker open descriptor inventory exceeds bound"));}
        let link=fs::read_link(fd.path())?;let raw=link.as_os_str().as_bytes();
        let fd_number=fd.file_name().to_str().and_then(|s|s.parse::<i32>().ok()).ok_or_else(||error("worker descriptor number invalid"))?;
        if raw.starts_with(b"pipe:[")||raw.starts_with(b"anon_inode:[event")||raw==b"anon_inode:[signalfd]"
            ||raw==b"anon_inode:[timerfd]" {continue;}
        if raw.starts_with(b"socket:[") {
            if !allow_socket{return Err(error("container init retains an unexpected control socket"));}
            check_socket_descriptor(pid,fd_number,raw,policy,quota,view,deadline)?;continue;
        }
        if raw.starts_with(b"anon_inode:") {return Err(error("worker retains an unreviewed kernel capability descriptor"));}
        if [b"/dev/null".as_slice(),b"/dev/zero",b"/dev/random",b"/dev/urandom"].contains(&raw) {
            check_stateless_device_view(pid,fd_number,&link,view,deadline)?;continue;
        }
        if link.starts_with("/dev/pts") {
            check_private_pty_view(pid,fd_number,&link,view,deadline)?;continue;
        }
        if link.is_absolute() {
            let a=view.read(fs_view::Operation::DescriptorMetadata{pid,fd:fd_number},deadline)?;
            let b=view.read(fs_view::Operation::RootMetadata{pid,path:link.clone(),nofollow:false},deadline)?;
            if (a.device,a.inode)!=(b.device,b.inode) {return Err(error("worker descriptor names a host object outside its private mounts"));}
            if !a.regular()&&!a.directory(){return Err(error("worker retains an unreviewed device or IPC object descriptor"));}
            if link.starts_with("/sys")||link.starts_with("/proc")||link.starts_with("/run/user")||link.starts_with("/var/run") {
                return Err(error("worker retains namespace/cgroup/Engine control descriptor"));
            }
        } else {return Err(error("worker descriptor cannot be tied to private mount inventory"));}
    }Ok(())
}

fn nofollow_object(path:&Path)->io::Result<File> {
    let name=CString::new(path.as_os_str().as_bytes()).map_err(|_|error("device path contains NUL"))?;
    let fd=unsafe{libc::open(name.as_ptr(),libc::O_PATH|libc::O_NOFOLLOW|libc::O_CLOEXEC)};
    if fd<0{return Err(io::Error::last_os_error());}Ok(unsafe{File::from_raw_fd(fd)})
}
fn check_stateless_device(pid:i32,fd_path:&Path,path:&Path)->io::Result<()> {
    let minor=match path.to_str(){Some("/dev/null")=>3,Some("/dev/zero")=>5,Some("/dev/random")=>8,
        Some("/dev/urandom")=>9,_=>return Err(error("device name is not stateless allowlist"))};
    let actual=path_object(fd_path)?.metadata()?;
    let visible=nofollow_object(Path::new(&format!("/proc/{pid}/root{}",path.display())))?.metadata()?;
    if !actual.file_type().is_char_device()||!visible.file_type().is_char_device()
        ||libc::major(actual.rdev())!=1||libc::minor(actual.rdev())!=minor||visible.rdev()!=actual.rdev() {
        return Err(error("worker stateless descriptor is not its expected private character device"));
    }
    let mounts=mount_inventory(&read_proc(&format!("/proc/{pid}/mountinfo"),MAX_PROC)?)?;
    if !mounts.iter().any(|m|m.target==Path::new("/dev")&&m.kind=="tmpfs") {
        return Err(error("worker stateless device does not belong to private device mount"));
    }Ok(())
}
fn check_private_pty(pid:i32,fd_path:&Path,path:&Path)->io::Result<()> {
    let _number=path.to_str().and_then(|p|p.strip_prefix("/dev/pts/"))
        .filter(|name|!name.is_empty()&&name.len()<=10&&name.bytes().all(|b|b.is_ascii_digit()))
        .and_then(|name|name.parse::<u32>().ok()).ok_or_else(||error("worker PTY path is not a numeric private slave node"))?;
    let actual=path_object(fd_path)?.metadata()?;
    let visible=nofollow_object(Path::new(&format!("/proc/{pid}/root{}",path.display())))?.metadata()?;
    let root=nofollow_object(Path::new(&format!("/proc/{pid}/root/dev/pts")))?.metadata()?;
    let host=nofollow_object(Path::new("/dev/pts"))?.metadata()?;
    if !actual.file_type().is_char_device()||!visible.file_type().is_char_device()
        ||actual.rdev()==0
        ||(actual.dev(),actual.ino(),actual.rdev())!=(visible.dev(),visible.ino(),visible.rdev())
        ||actual.dev()!=root.dev()||actual.dev()==host.dev() {
        return Err(error("worker PTY descriptor is not the exact node in its private devpts instance"));
    }
    let mounts=mount_inventory(&read_proc(&format!("/proc/{pid}/mountinfo"),MAX_PROC)?)?;
    if !mounts.iter().any(|m|m.target==Path::new("/dev/pts")&&m.kind=="devpts") {
        return Err(error("worker PTY has no private devpts mount"));
    }Ok(())
}

fn check_stateless_device_view(pid:i32,fd:i32,path:&Path,view:&fs_view::Context<'_>,deadline:Instant)->io::Result<()> {
    let minor=match path.to_str(){Some("/dev/null")=>3,Some("/dev/zero")=>5,Some("/dev/random")=>8,
        Some("/dev/urandom")=>9,_=>return Err(error("device name is not stateless allowlist"))};
    let a=view.read(fs_view::Operation::DescriptorMetadata{pid,fd},deadline)?;
    let b=view.read(fs_view::Operation::RootMetadata{pid,path:path.to_owned(),nofollow:true},deadline)?;
    if !a.character()||!b.character()||libc::major(a.rdev)!=1||libc::minor(a.rdev)!=minor||a.rdev!=b.rdev {
        return Err(error("worker stateless descriptor is not its expected private character device"));
    }
    let mounts=mount_inventory(&read_proc(&format!("/proc/{pid}/mountinfo"),MAX_PROC)?)?;
    if !mounts.iter().any(|m|m.target==Path::new("/dev")&&m.kind=="tmpfs") {
        return Err(error("worker stateless device has no private device mount"));
    }Ok(())
}
fn check_private_pty_view(pid:i32,fd:i32,path:&Path,view:&fs_view::Context<'_>,deadline:Instant)->io::Result<()> {
    path.to_str().and_then(|p|p.strip_prefix("/dev/pts/"))
        .filter(|name|!name.is_empty()&&name.len()<=10&&name.bytes().all(|b|b.is_ascii_digit()))
        .and_then(|name|name.parse::<u32>().ok()).ok_or_else(||error("worker PTY path is not a numeric private slave node"))?;
    let actual=view.read(fs_view::Operation::DescriptorMetadata{pid,fd},deadline)?;
    let root=view.read(fs_view::Operation::RootMetadata{pid,path:"/dev/pts".into(),nofollow:true},deadline)?;
    let host=nofollow_object(Path::new("/dev/pts"))?.metadata()?;
    if !actual.character()||actual.rdev==0||actual.device!=root.device||actual.device==host.dev() {
        return Err(error("worker PTY descriptor is outside its private devpts instance"));
    }
    let visible=view.read(fs_view::Operation::RootMetadata{pid,path:path.to_owned(),nofollow:true},deadline)?;
    if !visible.character()||(actual.device,actual.inode,actual.rdev)!=(visible.device,visible.inode,visible.rdev) {
        return Err(error("worker PTY descriptor is not the exact private devpts node"));
    }
    let mounts=mount_inventory(&read_proc(&format!("/proc/{pid}/mountinfo"),MAX_PROC)?)?;
    if !mounts.iter().any(|m|m.target==Path::new("/dev/pts")&&m.kind=="devpts") {
        return Err(error("worker PTY has no private devpts mount"));
    }Ok(())
}

fn path_object(path:&Path)->io::Result<File> {
    let path=CString::new(path.as_os_str().as_bytes()).map_err(|_|error("descriptor path contains NUL"))?;
    let fd=unsafe{libc::open(path.as_ptr(),libc::O_PATH|libc::O_CLOEXEC)};
    if fd<0{return Err(io::Error::last_os_error());}Ok(unsafe{File::from_raw_fd(fd)})
}

fn artifact_mount_readonly(pid:i32,file:&fs_view::Fact)->io::Result<()> {
    let id=file.mount_id;
    let mounts=mount_inventory(&read_proc(&format!("/proc/{pid}/mountinfo"),MAX_PROC)?)?;
    let mount=mounts.iter().find(|mount|mount.id==id).ok_or_else(||error("provider artifact FD is outside container mount inventory"))?;
    if !mount.options.contains("ro")||!file.regular() {
        return Err(error("provider artifact reaches a mutable bind or nonregular object"));
    }Ok(())
}

fn check_socket_descriptor(pid:i32,fd:i32,raw:&[u8],policy:&Policy,quota:&EnforcedQuotaSnapshot,
    view:&fs_view::Context<'_>,deadline:Instant)->io::Result<()> {
    let text=std::str::from_utf8(raw).map_err(|_|error("socket inode is not ASCII"))?;
    let inode=text.strip_prefix("socket:[").and_then(|v|v.strip_suffix(']'))
        .filter(|v|!v.is_empty()&&v.bytes().all(|b|b.is_ascii_digit())).ok_or_else(||error("socket inode malformed"))?;
    let pin=open_pidfd(pid)?;
    let copied=unsafe{libc::syscall(libc::SYS_pidfd_getfd,pin.as_raw_fd(),fd,0)};
    if copied<0{return Err(error("ROOT cannot independently inspect worker socket capability"));}
    let socket=unsafe{File::from_raw_fd(copied as RawFd)};
    let mut domain=0i32;let mut length=mem::size_of_val(&domain) as libc::socklen_t;
    if unsafe{libc::getsockopt(socket.as_raw_fd(),libc::SOL_SOCKET,libc::SO_DOMAIN,(&mut domain as *mut i32).cast(),&mut length)}!=0 {
        return Err(io::Error::last_os_error());
    }
    let tables:&[&str]=match domain {libc::AF_UNIX=>&["unix"],libc::AF_INET=>&["tcp","udp"],libc::AF_INET6=>&["tcp6","udp6"],
        _=>return Err(error("worker owns an unreviewed socket family"))};
    let mut present=false;
    for table in tables {
        let content=read_proc(&format!("/proc/{pid}/net/{table}"),MAX_PROC)?;
        for row in content.lines().skip(1){let fields=row.split_whitespace().collect::<Vec<_>>();
            let index=if *table=="unix"{6}else{9};
            if fields.get(index)==Some(&inode){
                present=true;
            }
        }
    }
    if !present{return Err(error("worker inherited a socket from outside its private network namespace"));}
    let mut peer_address:libc::sockaddr_un=unsafe{mem::zeroed()};let mut size=mem::size_of_val(&peer_address) as libc::socklen_t;
    if domain==libc::AF_UNIX&&unsafe{libc::getpeername(socket.as_raw_fd(),(&mut peer_address as *mut libc::sockaddr_un).cast(),&mut size)}==0 {
        let count=(size as usize).saturating_sub(mem::size_of::<libc::sa_family_t>()).min(peer_address.sun_path.len());
        let bytes=peer_address.sun_path[..count].iter().map(|b|*b as u8).take_while(|b|*b!=0).collect::<Vec<_>>();
        let stream=dup_fd(socket.as_raw_fd())?;
        let stream=unsafe{UnixStream::from_raw_fd(std::os::fd::IntoRawFd::into_raw_fd(stream))};
        let (credentials,peer_pin)=peer(&stream)?;
        if credentials.uid==0 {
            validate_root_endpoint(pid,&bytes,policy,quota,&credentials,&peer_pin,view,deadline)?;
        } else if credentials.uid==policy.engine.uid {
            let own=scope(pid)?;let other=scope(credentials.pid)?;
            if own.namespaces!=other.namespaces||own.cgroup!=other.cgroup {
                return Err(error("worker Unix descriptor reaches another private scope"));
            }
            check_status(&read_proc(&format!("/proc/{}/status",credentials.pid),64*1024)?,policy.engine.uid)?;
        } else {return Err(error("worker Unix descriptor reaches an unreviewed host identity"));}
        live_same(&peer_pin,credentials.pid)?;
    }
    live_same(&pin,pid)
}

fn validate_root_endpoint(pid:i32,address:&[u8],policy:&Policy,quota:&EnforcedQuotaSnapshot,
    credentials:&libc::ucred,peer_pin:&File,view:&fs_view::Context<'_>,deadline:Instant)->io::Result<()> {
    let address=Path::new(OsStr::from_bytes(address));
    let mut names=vec!["hook.sock".to_owned()];
    names.extend(policy.providers.keys().map(|family|format!("egress-{family}.sock")));
    let name=names.iter().find(|name|address==policy.root.join("broker").join(name)
        ||address==Path::new("/run/doxa/session").join(name))
        .ok_or_else(||error("worker ROOT peer is outside exact scoped gateway endpoints"))?;
    let physical=policy.root.join("broker").join(name);
    let parent=physical.parent().unwrap();let pinned=quota.tree.broker.metadata()?;
    let observed=path_object(parent)?.metadata()?;
    if (pinned.dev(),pinned.ino())!=(observed.dev(),observed.ino()) {
        return Err(error("ROOT listener parent differs from retained quota broker directory"));
    }
    let host=fs::symlink_metadata(&physical)?;
    let alias=view.read(fs_view::Operation::RootMetadata{pid,path:PathBuf::from(format!("/run/doxa/session/{name}")),nofollow:true},deadline)?;
    if !host.file_type().is_socket()||host.uid()!=0||host.gid()!=policy.engine.gid
        ||host.mode()&0o7777!=0o660||host.nlink()!=1||(host.dev(),host.ino())!=(alias.device,alias.inode) {
        return Err(error("ROOT gateway socket is not the exact read-only quota bind inode"));
    }
    let executable=File::open(format!("/proc/{}/exe",credentials.pid))?.metadata()?;
    let expected=root_file(&policy.authority.path,MAX_ARTIFACT,false)?.metadata()?;
    if (executable.dev(),executable.ino())!=(expected.dev(),expected.ino()) {
        return Err(error("ROOT endpoint is not the pinned native gateway executable"));
    }
    live_same(peer_pin,credentials.pid)
}

fn check_kernel(policy:&Policy,pid:i32,init:&Scope,q:&EnforcedQuotaSnapshot,pin:&File,deadline:Instant)->io::Result<()> {
    let host=scope_host()?;
    for (name,ns) in &init.namespaces {if host.get(name)==Some(ns){return Err(error("worker shares a host namespace"));}}
    check_uid_map(&read_proc(&format!("/proc/{pid}/uid_map"),8192)?,policy)?;
    let view=fs_view::Context::new(pid,pin)?;
    let exe=view.read(fs_view::Operation::Executable,deadline)?;
    if exe.sha256.as_deref()!=Some(policy.worker.sha256.as_str()) {return Err(error("live container init is not the reviewed worker executable"));}
    artifact_mount_readonly(pid,&exe)?;
    let cmd=read_bytes(&File::open(format!("/proc/{pid}/cmdline"))?,8192)?;
    let argv=cmd.split(|b|*b==0).filter(|b|!b.is_empty()).collect::<Vec<_>>();
    if argv!=[policy.worker.path.as_os_str().as_bytes(),b"hold-hardened"] {return Err(error("container init does not run the mandatory worker filter mode"));}
    check_mounts(pid,q,&view,deadline)?;check_network(pid)?;check_descriptors(pid,false,policy,q,&view,deadline)?;check_limits(policy,&init.cgroup)?;
    check_client_cgroup_access(policy,&init.cgroup,deadline)?;
    for provider in policy.providers.values() {
        for (path,hash) in provider.artifacts.iter().chain(std::iter::once((&provider.path,&provider.sha256))) {
            let f=view.read(fs_view::Operation::Artifact(path.to_owned()),deadline)?;
            artifact_mount_readonly(pid,&f)?;
            if f.sha256.as_deref()!=Some(hash) {return Err(error("actual provider image bytes differ from protected package pins"));}
        }
    }
    let mut checked=BTreeSet::new();scan_cgroup_processes(policy,init,&Path::new("/sys/fs/cgroup").join(init.cgroup.trim_start_matches('/')),
        q,&view,0,&mut checked,deadline)?;
    if !checked.contains(&pid){return Err(error("retained init is absent from actual cgroup membership"));}
    Ok(())
}
fn scope_host()->io::Result<BTreeMap<String,PathBuf>> {
    let mut ns=BTreeMap::new();for name in ["pid","mnt","net","user","cgroup","ipc","uts"] {
        ns.insert(name.into(),fs::read_link(format!("/proc/self/ns/{name}"))?);
    }Ok(ns)
}
fn scan_cgroup_processes(policy:&Policy,init:&Scope,path:&Path,quota:&EnforcedQuotaSnapshot,view:&fs_view::Context<'_>,depth:usize,checked:&mut BTreeSet<i32>,deadline:Instant)->io::Result<()> {
    remaining(deadline)?;if depth>16||checked.len()>4096{return Err(error("container cgroup hierarchy exceeds bound"));}
    let raw=read_proc(path.join("cgroup.procs").to_str().ok_or_else(||error("cgroup path malformed"))?,64*1024)?;
    for row in raw.lines() {let pid=row.parse::<i32>().ok().filter(|p|*p>0).ok_or_else(||error("cgroup process list malformed"))?;
        if !checked.insert(pid){return Err(error("container cgroup process moved during observation"));}
        let pin=open_pidfd(pid)?;let s=scope(pid)?;
        if s.namespaces!=init.namespaces||!within(&s.cgroup,&init.cgroup){return Err(error("container process has escaped its immutable private scope"));}
        let mut threads=0;
        for task in fs::read_dir(format!("/proc/{pid}/task"))? {let task=task?;threads+=1;
            if threads>4096{return Err(error("worker thread inventory exceeds bound"));}
            check_status(&read_proc(task.path().join("status").to_str().ok_or_else(||error("thread path invalid"))?,64*1024)?,policy.engine.uid)?;
        }
        check_descriptors(pid,true,policy,quota,view,deadline)?;live_same(&pin,pid)?;
    }
    for entry in fs::read_dir(path)? {let entry=entry?;
        if entry.file_type()?.is_dir(){scan_cgroup_processes(policy,init,&entry.path(),quota,view,depth+1,checked,deadline)?;}
    }Ok(())
}

// The child performs only libc syscalls after fork. It never writes a control
// byte. Open tests apply real supplementary groups and ACL permissions.
fn check_client_cgroup_access(policy:&Policy,cgroup:&str,deadline:Instant)->io::Result<()> {
    let (gid,groups)=caller_groups(policy.caller_uid)?;
    if groups.contains(&policy.engine.gid){return Err(error("client belongs to dedicated Engine group"));}
    let mut paths=Vec::new();let mut dir=Path::new("/sys/fs/cgroup").join(cgroup.trim_start_matches('/'));
    loop {
        for file in ["cgroup.procs","cgroup.threads","cgroup.subtree_control"] {
            let path=dir.join(file);if path.exists(){paths.push(CString::new(path.as_os_str().as_bytes()).map_err(|_|error("cgroup control path contains NUL"))?);}
        }
        if dir==Path::new("/sys/fs/cgroup"){break;}
        if !dir.pop()||!dir.starts_with("/sys/fs/cgroup"){return Err(error("cgroup hierarchy escaped fixed host mount"));}
    }
    let budget=remaining(deadline)?;
    let child=unsafe{libc::fork()};if child<0{return Err(io::Error::last_os_error());}
    if child==0 {
        let mut status=0;
        // The access probe never carries ROOT Engine, gateway, quota or pidfd
        // capabilities across the UID drop. Outside client processes cannot
        // inspect it or inherit its descriptors.
        if unsafe{libc::syscall(libc::SYS_close_range,0u32,u32::MAX,0u32)}!=0
            ||unsafe{libc::prctl(libc::PR_SET_DUMPABLE,0,0,0,0)}!=0
            ||unsafe{libc::prctl(libc::PR_SET_NO_NEW_PRIVS,1,0,0,0)}!=0
            ||unsafe{libc::prctl(libc::PR_SET_KEEPCAPS,0,0,0,0)}!=0
            ||unsafe{libc::setgroups(groups.len(),groups.as_ptr())}!=0||unsafe{libc::setresgid(gid,gid,gid)}!=0
            ||unsafe{libc::setresuid(policy.caller_uid,policy.caller_uid,policy.caller_uid)}!=0 {status=2;}
        #[repr(C)]struct CapHeader{version:u32,pid:i32}
        #[repr(C)]struct CapData{effective:u32,permitted:u32,inheritable:u32}
        let header=CapHeader{version:0x20080522,pid:0};let caps=[CapData{effective:0,permitted:0,inheritable:0},CapData{effective:0,permitted:0,inheritable:0}];
        if status==0&&unsafe{libc::syscall(libc::SYS_capset,&header as *const CapHeader,caps.as_ptr())}!=0{status=2;}
        if status==0&&(unsafe{libc::prctl(libc::PR_SET_DUMPABLE,0,0,0,0)}!=0
            ||unsafe{libc::prctl(libc::PR_SET_PDEATHSIG,libc::SIGKILL,0,0,0)}!=0){status=2;}
        if status==0 {for path in &paths {let fd=unsafe{libc::open(path.as_ptr(),libc::O_WRONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|libc::O_NONBLOCK)};
            if fd>=0 {unsafe{libc::close(fd);}status=3;break;}
            let errno=unsafe{*libc::__errno_location()};if errno!=libc::EACCES&&errno!=libc::EPERM&&errno!=libc::EROFS {status=4;break;}
        }}unsafe{libc::_exit(status);}
    }
    let pin=match open_pidfd(child){Ok(pin)=>pin,Err(e)=>{unsafe{libc::kill(child,libc::SIGKILL);libc::waitpid(child,std::ptr::null_mut(),0);}return Err(e);}};
    let ms=budget.min(deadline.saturating_duration_since(Instant::now())).as_millis().min(i32::MAX as u128).max(1) as i32;
    let mut p=libc::pollfd{fd:pin.as_raw_fd(),events:libc::POLLIN,revents:0};
    let result=unsafe{libc::poll(&mut p,1,ms)};
    if result<=0 {unsafe{libc::kill(child,libc::SIGKILL);libc::waitpid(child,std::ptr::null_mut(),0);}
        return Err(error("privilege-dropped cgroup access check failed or exceeded deadline"));}
    let mut status=0;if unsafe{libc::waitpid(child,&mut status,0)}!=child{return Err(io::Error::last_os_error());}
    if status!=0{return Err(error("client can mutate Engine cgroup authority or its permissions could not be proved"));}Ok(())
}
fn caller_groups(uid:u32)->io::Result<(u32,Vec<u32>)> {
    let mut record:libc::passwd=unsafe{mem::zeroed()};let mut storage=vec![0u8;65536];let mut found=std::ptr::null_mut();
    if unsafe{libc::getpwuid_r(uid,&mut record,storage.as_mut_ptr().cast(),storage.len(),&mut found)}!=0||found.is_null(){
        return Err(error("ROOT cannot resolve administrator client identity"));
    }
    let mut groups=vec![0u32;256];let mut count=groups.len() as i32;
    if unsafe{libc::getgrouplist(record.pw_name,record.pw_gid,groups.as_mut_ptr(),&mut count)}<0||count<=0||count>256 {
        return Err(error("client supplementary group inventory is unavailable or unbounded"));
    }
    groups.truncate(count as usize);Ok((record.pw_gid,groups))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stateless_device_requires_actual_character_rdev() {
        let file=File::open("/dev/zero").unwrap();
        let fd=PathBuf::from(format!("/proc/self/fd/{}",file.as_raw_fd()));
        let error=check_stateless_device(unsafe{libc::getpid()},&fd,Path::new("/dev/null")).unwrap_err();
        assert!(error.to_string().contains("expected private character device"));
    }
    #[test]
    fn host_pty_cannot_be_claimed_as_private_container_slave() {
        let mut master=-1;let mut slave=-1;
        assert_eq!(unsafe{libc::openpty(&mut master,&mut slave,std::ptr::null_mut(),std::ptr::null(),std::ptr::null())},0);
        let _master=unsafe{File::from_raw_fd(master)};let slave=unsafe{File::from_raw_fd(slave)};
        let mut name=[0 as libc::c_char;512];
        assert_eq!(unsafe{libc::ttyname_r(slave.as_raw_fd(),name.as_mut_ptr(),name.len())},0);
        let path=PathBuf::from(OsStr::from_bytes(unsafe{std::ffi::CStr::from_ptr(name.as_ptr())}.to_bytes()));
        let fd=PathBuf::from(format!("/proc/self/fd/{}",slave.as_raw_fd()));
        let error=check_private_pty(unsafe{libc::getpid()},&fd,&path).unwrap_err();
        assert!(error.to_string().contains("private devpts instance"));
    }
    #[test]
    fn unprivileged_caller_cannot_load_root_policy_or_stage_quota() {
        if unsafe{libc::geteuid()}==0{return;}
        let path=Path::new("/etc/doxa/untrusted.json");
        assert!(load_root_policy(path).unwrap_err().to_string().contains("requires ROOT"));
        assert!(stage_check(path,"before","none").unwrap_err().to_string().contains("requires ROOT"));
    }
    #[test]
    fn strict_http_lengths_refuse_ambiguous_and_unbounded_bodies() {
        assert_eq!(http_framing("HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\n").unwrap(),HttpBody::Length(12));
        for raw in ["HTTP/1.1 200 OK\r\n\r\n","HTTP/1.1 200 OK\r\nContent-Length: 12\r\nContent-Length: 12\r\n\r\n",
            "HTTP/1.1 200 OK\r\nContent-Length: 99999999\r\n\r\n","HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Length: 1\r\n\r\n",
            "HTTP/1.1 404 Not Found\r\nContent-Length: 1\r\n\r\n"] {assert!(http_framing(raw).is_err());}
    }
    #[test]
    fn bounded_chunked_inspect_accepts_real_framing_and_refuses_extensions_trailers() {
        assert_eq!(http_framing("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n").unwrap(),HttpBody::Chunked);
        for header in ["HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n\r\n",
            "HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\n\r\n"]{assert!(http_framing(header).is_err());}
        for (body,valid) in [(b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n".as_slice(),true),
            (b"1;ext=x\r\nx\r\n0\r\n\r\n".as_slice(),false),(b"1\r\nx\r\n0\r\nX-Trailer: no\r\n\r\n".as_slice(),false),
            (b"300000\r\n".as_slice(),false)] {
            let (mut reader,mut writer)=UnixStream::pair().unwrap();writer.write_all(body).unwrap();drop(writer);
            let result=read_chunks(&mut reader,Instant::now()+Duration::from_millis(100));assert_eq!(result.is_ok(),valid);
            if valid{assert_eq!(result.unwrap(),b"abcde");}
        }
    }
    #[test]
    fn cgroup_scope_rejects_prefix_collision_root_and_traversal() {
        assert_eq!(parse_cgroup("0::/engine/container\n").unwrap(),"/engine/container");
        assert!(within("/engine/container/child","/engine/container"));
        assert!(!within("/engine/container-other","/engine/container"));
        for raw in ["0::/\n","0::/engine/../container\n","0::/engine//container\n","0::/engine\n1::/other\n"]{
            assert!(parse_cgroup(raw).is_err());}
    }
    #[test]
    fn mount_parser_refuses_shared_or_stacked_host_paths() {
        let good="1 0 0:1 / / ro - overlay overlay rw\n2 1 0:2 / /sys/fs/cgroup ro,nosuid,nodev - cgroup2 cgroup rw\n";
        assert_eq!(mount_inventory(good).unwrap().len(),2);
        assert!(mount_inventory("1 0 0:1 / / rw shared:3 - overlay overlay rw\n").is_err());
        assert!(mount_inventory(&(good.to_owned()+"3 1 0:1 / / ro - overlay overlay rw\n")).is_err());
    }
    #[test]
    fn rootless_uid_map_excludes_client_and_host_root() {
        let p=super::super::tests::policy();check_uid_map("0 1001 1\n1 165536 65536\n",&p).unwrap();
        for raw in ["0 0 1\n","0 1000 1\n","0 1001 1\n1 900 200\n","0 1001 2\n"]{assert!(check_uid_map(raw,&p).is_err());}
    }
    #[test]
    fn inherited_regular_file_cannot_substitute_for_pidfd() {
        let file=File::open("/dev/null").unwrap();assert!(pidfd_pid(&file).is_err());
        let pin=open_pidfd(unsafe{libc::getpid()}).unwrap();live_same(&pin,unsafe{libc::getpid()}).unwrap();
    }
    #[test]
    fn slow_trickle_headers_consume_one_total_deadline() {
        let (mut reader,mut writer)=UnixStream::pair().unwrap();
        let send=std::thread::spawn(move||{for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nX" {
            if writer.write_all(&[*byte]).is_err(){break;}std::thread::sleep(Duration::from_millis(20));}});
        let start=Instant::now();assert!(read_http(&mut reader,start+Duration::from_millis(70)).is_err());
        assert!(start.elapsed()<Duration::from_millis(300));drop(reader);send.join().unwrap();
    }
}
