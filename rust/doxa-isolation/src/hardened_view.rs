//! Fixed, read-only container filesystem view for rootless FUSE mounts.
//! ROOT retains authority. The child joins only the pinned init user namespace
//! and returns bounded file facts through an owned pipe; no receipt authorizes.
use super::{error, live_same, open_pidfd, remaining, read_proc, hash_file, Identity, MAX_ARTIFACT};
use serde::{Deserialize,Serialize};
use std::{ffi::CString,fs::File,io::{self,Read},mem,os::{fd::{AsRawFd,FromRawFd},
    unix::{ffi::OsStrExt,fs::MetadataExt}},path::{Path,PathBuf},time::Instant};

const MAX_REPLY:usize=4096;
#[derive(Debug,Clone,Deserialize,Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Fact {
    pub device:u64,pub inode:u64,pub rdev:u64,pub mode:u32,pub size:u64,
    pub mount_id:u64,pub sha256:Option<String>,
}
impl Fact {
    fn from_file(file:&File,sha256:Option<String>)->io::Result<Self> {
        let m=file.metadata()?;
        let raw=read_proc(&format!("/proc/self/fdinfo/{}",file.as_raw_fd()),4096)?;
        let id=super::value(&raw,"mnt_id:")?.parse::<u64>().ok().filter(|id|*id!=0)
            .ok_or_else(||error("view file mount identity is unavailable"))?;
        Ok(Self{device:m.dev(),inode:m.ino(),rdev:m.rdev(),mode:m.mode(),size:m.len(),mount_id:id,sha256})
    }
    pub fn regular(&self)->bool{self.mode&libc::S_IFMT==libc::S_IFREG}
    pub fn character(&self)->bool{self.mode&libc::S_IFMT==libc::S_IFCHR}
    pub fn directory(&self)->bool{self.mode&libc::S_IFMT==libc::S_IFDIR}
}
#[derive(Debug,Clone)]
pub(super) enum Operation {
    Executable,
    Artifact(PathBuf),
    RootMetadata {pid:i32,path:PathBuf,nofollow:bool},
    DescriptorMetadata {pid:i32,fd:i32},
}
#[derive(Deserialize,Serialize)]
#[serde(deny_unknown_fields)]
struct Reply {fact:Option<Fact>,error:Option<String>}

pub(super) struct Context<'a> {pid:i32,pin:&'a File,namespace:File,namespace_identity:(u64,u64)}
impl<'a> Context<'a> {
    pub fn new(pid:i32,pin:&'a File)->io::Result<Self> {
        live_same(pin,pid)?;
        let namespace=File::open(format!("/proc/{pid}/ns/user"))?;let m=namespace.metadata()?;
        Ok(Self{pid,pin,namespace,namespace_identity:(m.dev(),m.ino())})
    }
    fn revalidate(&self)->io::Result<()> {
        live_same(self.pin,self.pid)?;
        let m=File::open(format!("/proc/{}/ns/user",self.pid))?.metadata()?;
        if (m.dev(),m.ino())!=self.namespace_identity{return Err(error("view init changed its pinned user namespace"));}Ok(())
    }
    pub fn read(&self,operation:Operation,deadline:Instant)->io::Result<Fact> {
        self.revalidate()?;remaining(deadline)?;
        let parent_pid=unsafe{libc::getpid()};let parent_pin=open_pidfd(parent_pid)?;
        let mut pipe=[-1,-1];
        if unsafe{libc::pipe2(pipe.as_mut_ptr(),libc::O_CLOEXEC|libc::O_NONBLOCK)}!=0{return Err(io::Error::last_os_error());}
        let input=unsafe{File::from_raw_fd(pipe[0])};let output=unsafe{File::from_raw_fd(pipe[1])};
        // Duplicate above reserved descriptors before fork. dup2 ordering in
        // the child cannot accidentally replace its namespace with the pipe.
        let ns=unsafe{libc::fcntl(self.namespace.as_raw_fd(),libc::F_DUPFD_CLOEXEC,10)};
        if ns<0{return Err(io::Error::last_os_error());}let ns=unsafe{File::from_raw_fd(ns)};
        let reply=unsafe{libc::fcntl(output.as_raw_fd(),libc::F_DUPFD_CLOEXEC,10)};
        if reply<0{return Err(io::Error::last_os_error());}let reply=unsafe{File::from_raw_fd(reply)};
        let child=unsafe{libc::fork()};if child<0{return Err(io::Error::last_os_error());}
        if child==0 {
            // Arm before any namespace/credential setup. If ROOT died between
            // fork and prctl, the inherited exact parent pidfd/getppid catches it.
            if !arm_parent_death(parent_pid,Some(parent_pin.as_raw_fd())){unsafe{libc::_exit(2);}}
            let success=unsafe{libc::dup2(reply.as_raw_fd(),3)}>=0&&unsafe{libc::dup2(ns.as_raw_fd(),4)}>=0
                &&unsafe{libc::close(0)}==0&&unsafe{libc::close(1)}==0&&unsafe{libc::close(2)}==0
                &&unsafe{libc::syscall(libc::SYS_close_range,5u32,u32::MAX,0u32)}==0
                &&unsafe{libc::setgroups(0,std::ptr::null())}==0&&unsafe{libc::chdir(c"/".as_ptr())}==0
                &&unsafe{libc::prctl(libc::PR_SET_DUMPABLE,0,0,0,0)}==0
                &&unsafe{libc::setns(4,libc::CLONE_NEWUSER)}==0;
            unsafe{libc::close(4);}
            if !success{unsafe{libc::_exit(2);}}
            // Credential transitions clear PDEATHSIG. Re-arm and confirm the
            // kernel still names the original parent before reading any file.
            if drop_authority().is_err()||!arm_parent_death(parent_pid,None)
                ||readonly_landlock(self.pid,&operation).is_err()
                ||readonly_filter().is_err(){unsafe{libc::_exit(3);}}
            let reply=match perform(self.pid,operation,deadline) {
                Ok(fact)=>Reply{fact:Some(fact),error:None},
                Err(err)=>Reply{fact:None,error:Some(err.to_string().chars().take(512).collect())},
            };
            let bytes=match serde_json::to_vec(&reply){Ok(bytes) if bytes.len()<=MAX_REPLY=>bytes,_=>unsafe{libc::_exit(4)}};
            let mut cursor=0;
            while cursor<bytes.len(){let n=unsafe{libc::write(3,bytes[cursor..].as_ptr().cast(),bytes.len()-cursor)};
                if n<=0{unsafe{libc::_exit(5);}}cursor+=n as usize;}
            unsafe{libc::_exit(0);}
        }
        drop(output);drop(reply);drop(ns);
        let child_pin=match open_pidfd(child){Ok(pin)=>pin,Err(err)=>{
            unsafe{libc::kill(child,libc::SIGKILL);}reap_bounded(child);return Err(err);}};
        let result=read_reply(&input,&child_pin,child,deadline);
        if result.is_err(){unsafe{libc::syscall(libc::SYS_pidfd_send_signal,child_pin.as_raw_fd(),libc::SIGKILL,std::ptr::null::<libc::siginfo_t>(),0)};}
        let reaped=reap_bounded(child);
        if !reaped{return Err(error("view helper cleanup could not confirm quiescence"));}
        let fact=result?;self.revalidate()?;Ok(fact)
    }
}

fn arm_parent_death(expected:i32,pin:Option<i32>)->bool {
    if unsafe{libc::prctl(libc::PR_SET_PDEATHSIG,libc::SIGKILL,0,0,0)}!=0
        ||unsafe{libc::getppid()}!=expected{return false;}
    if let Some(fd)=pin {
        let mut p=libc::pollfd{fd,events:libc::POLLIN,revents:0};
        if unsafe{libc::poll(&mut p,1,0)}!=0||p.revents!=0{return false;}
    }
    (unsafe{libc::getppid()})==expected
}

fn drop_authority()->io::Result<()> {
    if unsafe{libc::setresgid(0,0,0)}!=0||unsafe{libc::setresuid(0,0,0)}!=0 {return Err(io::Error::last_os_error());}
    let last=read_proc("/proc/sys/kernel/cap_last_cap",128)?.trim().parse::<u32>().map_err(|_|error("kernel capability bound unavailable"))?;
    if last>63{return Err(error("kernel capability bound exceeds reviewed helper ABI"));}
    for cap in 0..=last {if unsafe{libc::prctl(libc::PR_CAPBSET_DROP,cap,0,0,0)}!=0{return Err(io::Error::last_os_error());}}
    #[repr(C)]struct Header{version:u32,pid:i32}
    #[repr(C)]struct Data{effective:u32,permitted:u32,inheritable:u32}
    let header=Header{version:0x20080522,pid:0};let caps=[Data{effective:0,permitted:0,inheritable:0},Data{effective:0,permitted:0,inheritable:0}];
    if unsafe{libc::syscall(libc::SYS_capset,&header as *const Header,caps.as_ptr())}!=0
        ||unsafe{libc::prctl(libc::PR_SET_NO_NEW_PRIVS,1,0,0,0)}!=0
        ||unsafe{libc::prctl(libc::PR_SET_DUMPABLE,0,0,0,0)}!=0
        ||unsafe{libc::prctl(libc::PR_SET_PDEATHSIG,libc::SIGKILL,0,0,0)}!=0 {
        return Err(io::Error::last_os_error());
    }Ok(())
}

fn reap_bounded(pid:i32)->bool {
    for _ in 0..100 {
        let mut status=0;let n=unsafe{libc::waitpid(pid,&mut status,libc::WNOHANG)};
        if n==pid{return true;}if n<0{return io::Error::last_os_error().raw_os_error()==Some(libc::ECHILD);}
        // Bounded cleanup only. No waitpid(0) can hang on a wedged FUSE read.
        let mut p=libc::pollfd{fd:-1,events:0,revents:0};unsafe{libc::poll(&mut p,0,1);}
    }false
}
fn read_reply(file:&File,pin:&File,pid:i32,deadline:Instant)->io::Result<Fact> {
    let mut bytes=Vec::new();let mut source=file;let mut ended=false;
    while !ended {
        let mut descriptors=[libc::pollfd{fd:file.as_raw_fd(),events:libc::POLLIN,revents:0},
            libc::pollfd{fd:pin.as_raw_fd(),events:libc::POLLIN,revents:0}];
        let ms=remaining(deadline)?.as_millis().min(i32::MAX as u128).max(1) as i32;
        let n=unsafe{libc::poll(descriptors.as_mut_ptr(),2,ms)};
        if n<0{let err=io::Error::last_os_error();if err.kind()==io::ErrorKind::Interrupted{continue;}return Err(err);}
        if n==0{return Err(error("view helper total deadline expired"));}
        let mut chunk=[0u8;1024];
        loop {match source.read(&mut chunk){
            Ok(0)=>{ended=true;break;},Ok(n)=>{if bytes.len()+n>MAX_REPLY{return Err(error("view helper reply exceeds bound"));}bytes.extend_from_slice(&chunk[..n]);},
            Err(err) if err.kind()==io::ErrorKind::WouldBlock=>break,
            Err(err) if err.kind()==io::ErrorKind::Interrupted=>continue,Err(err)=>return Err(err),
        }}
    }
    // EOF is not proof of exit: a compromised child can close the reply pipe
    // and remain alive. Observe the retained child pidfd under the same deadline.
    loop {
        let mut status=0;let n=unsafe{libc::waitpid(pid,&mut status,libc::WNOHANG)};
        if n==pid {if status!=0{return Err(error("read-only view helper did not exit successfully"));}break;}
        if n<0{return Err(io::Error::last_os_error());}
        let mut p=libc::pollfd{fd:pin.as_raw_fd(),events:libc::POLLIN,revents:0};
        let ms=remaining(deadline)?.as_millis().min(i32::MAX as u128).max(1) as i32;
        let n=unsafe{libc::poll(&mut p,1,ms)};
        if n<0&&io::Error::last_os_error().kind()!=io::ErrorKind::Interrupted{return Err(io::Error::last_os_error());}
        if n==0{return Err(error("view helper exit deadline expired"));}
    }
    let reply:Reply=serde_json::from_slice(&bytes).map_err(|_|error("view helper reply is malformed or incomplete"))?;
    match (reply.fact,reply.error){(Some(fact),None)=>Ok(fact),(None,Some(err))=>Err(error(format!("view helper: {err}"))),
        _=>Err(error("view helper reply fields are ambiguous"))}
}

fn open_root(pid:i32)->io::Result<File> {
    let path=CString::new(format!("/proc/{pid}/root")).unwrap();
    let fd=unsafe{libc::open(path.as_ptr(),libc::O_PATH|libc::O_DIRECTORY|libc::O_CLOEXEC)};
    if fd<0{return Err(io::Error::last_os_error());}Ok(unsafe{File::from_raw_fd(fd)})
}
fn in_root(root:&File,path:&Path,flags:i32)->io::Result<File> {
    let relative=path.strip_prefix("/").unwrap_or(path);
    if relative.as_os_str().as_bytes().len()>4096||relative.components().any(|p|!matches!(p,std::path::Component::Normal(_))) {
        return Err(error("fixed view path is not a canonical image path"));
    }
    let path=CString::new(relative.as_os_str().as_bytes()).map_err(|_|error("view path contains NUL"))?;
    #[repr(C)]struct How{flags:u64,mode:u64,resolve:u64}
    // openat2 rejects O_NONBLOCK combined with O_PATH; metadata handles never
    // perform blocking I/O. Content handles remain explicitly nonblocking.
    let flags=flags|libc::O_CLOEXEC|if flags&libc::O_PATH==0{libc::O_NONBLOCK}else{0};
    let how=How{flags:flags as u64,mode:0,resolve:0x10|0x02}; // IN_ROOT | NO_MAGICLINKS
    let fd=unsafe{libc::syscall(libc::SYS_openat2,root.as_raw_fd(),path.as_ptr(),&how as *const How,mem::size_of::<How>())};
    if fd<0{return Err(io::Error::last_os_error());}Ok(unsafe{File::from_raw_fd(fd as i32)})
}
fn perform(init:i32,operation:Operation,deadline:Instant)->io::Result<Fact> {
    remaining(deadline)?;
    match operation {
        Operation::Executable=>{
            let path=format!("/proc/{init}/exe");let file=File::open(&path)?;let first=Identity::of(&file)?;
            let hash=hash_file(&file,MAX_ARTIFACT,deadline)?;let fresh=File::open(path)?;
            if Identity::of(&fresh)?!=first{return Err(error("actual init executable changed during view read"));}
            Fact::from_file(&fresh,Some(hash))
        },
        Operation::Artifact(path)=>{
            let root=open_root(init)?;let file=in_root(&root,&path,libc::O_RDONLY)?;let first=Identity::of(&file)?;
            if !file.metadata()?.is_file(){return Err(error("protected image artifact is not regular"));}
            let hash=hash_file(&file,MAX_ARTIFACT,deadline)?;let fresh=in_root(&root,&path,libc::O_RDONLY)?;
            if Identity::of(&fresh)?!=first{return Err(error("protected image artifact changed during view read"));}
            Fact::from_file(&fresh,Some(hash))
        },
        Operation::RootMetadata{pid,path,nofollow}=>{
            let root=open_root(pid)?;let file=in_root(&root,&path,libc::O_PATH|if nofollow{libc::O_NOFOLLOW}else{0})?;
            Fact::from_file(&file,None)
        },
        Operation::DescriptorMetadata{pid,fd}=>{
            if pid<=0||fd<0{return Err(error("view descriptor identity is invalid"));}
            let path=CString::new(format!("/proc/{pid}/fd/{fd}")).unwrap();
            let copied=unsafe{libc::open(path.as_ptr(),libc::O_PATH|libc::O_CLOEXEC)};
            if copied<0{return Err(io::Error::last_os_error());}Fact::from_file(&unsafe{File::from_raw_fd(copied)},None)
        },
    }
}

fn readonly_landlock(init:i32,operation:&Operation)->io::Result<()> {
    // openat2 keeps root-relative symlinks confined, but seccomp cannot inspect
    // its pointer flags. Landlock denies every filesystem write independently.
    // The only file-content grant is the fixed immutable file being measured;
    // fresh reopening after the hash is therefore still possible and checked.
    let abi=unsafe{libc::syscall(libc::SYS_landlock_create_ruleset,std::ptr::null::<u8>(),0usize,1u32)};
    if abi<5{return Err(error("read-only view requires Landlock ABI 5 or newer"));}
    #[repr(C)]struct Ruleset{handled_access_fs:u64}
    #[repr(C,packed)]struct Rule{allowed_access:u64,parent_fd:i32}
    let attr=Ruleset{handled_access_fs:(1u64<<16)-1};
    let fd=unsafe{libc::syscall(libc::SYS_landlock_create_ruleset,&attr as *const Ruleset,mem::size_of::<Ruleset>(),0u32)};
    if fd<0{return Err(io::Error::last_os_error());}let rules=unsafe{File::from_raw_fd(fd as i32)};
    let add=|file:&File,access:u64|->io::Result<()> {
        let rule=Rule{allowed_access:access,parent_fd:file.as_raw_fd()};
        if unsafe{libc::syscall(libc::SYS_landlock_add_rule,rules.as_raw_fd(),1u32,&rule as *const Rule,0u32)}!=0 {
            return Err(io::Error::last_os_error());
        }Ok(())
    };
    let content=match operation {
        Operation::Executable=>Some(File::open(format!("/proc/{init}/exe"))?),
        Operation::Artifact(path)=>Some(in_root(&open_root(init)?,path,libc::O_PATH)?),
        Operation::RootMetadata{..}|Operation::DescriptorMetadata{..}=>None,
    };
    if let Some(file)=content {if !file.metadata()?.is_file(){return Err(error("view content grant is not regular"));}add(&file,1<<2)?;}
    let raw=unsafe{libc::open(c"/proc/self/fdinfo".as_ptr(),libc::O_PATH|libc::O_DIRECTORY|libc::O_CLOEXEC)};
    if raw<0{return Err(io::Error::last_os_error());}let fdinfo=unsafe{File::from_raw_fd(raw)};
    add(&fdinfo,(1<<2)|(1<<3))?;
    if unsafe{libc::syscall(libc::SYS_landlock_restrict_self,rules.as_raw_fd(),0u32)}!=0{return Err(io::Error::last_os_error());}
    Ok(())
}

fn readonly_filter()->io::Result<()> {
    #[cfg(target_arch="x86_64")]const ARCH:u32=0xc000003e;
    #[cfg(target_arch="aarch64")]const ARCH:u32=0xc00000b7;
    #[cfg(not(any(target_arch="x86_64",target_arch="aarch64")))]return Err(error("read-only view helper ABI is unsupported"));
    #[cfg(any(target_arch="x86_64",target_arch="aarch64"))]
    {
        let instruction=|code,jt,jf,k|libc::sock_filter{code,jt,jf,k};
        let mut program=vec![instruction(0x20,0,0,4),instruction(0x15,1,0,ARCH),instruction(0x06,0,0,0x80000000),instruction(0x20,0,0,0)];
        #[cfg(target_arch="x86_64")]{program.push(instruction(0x45,0,1,0x40000000));program.push(instruction(0x06,0,0,0x80000000));}
        const ALLOW:u32=0x7fff0000;
        const DENY:u32=0x00050000|libc::EPERM as u32;
        // Default-deny: no service, namespace, process creation, filesystem
        // mutation, ioctl, signal, descriptor transfer or execution syscall.
        for call in [libc::SYS_read,libc::SYS_close,libc::SYS_lseek,libc::SYS_fstat,
            libc::SYS_newfstatat,libc::SYS_statx,libc::SYS_brk,libc::SYS_munmap,
            libc::SYS_mremap,libc::SYS_madvise,libc::SYS_futex,libc::SYS_clock_gettime,
            libc::SYS_getpid,libc::SYS_gettid,libc::SYS_rt_sigaction,libc::SYS_rt_sigprocmask,
            libc::SYS_rt_sigreturn,libc::SYS_exit,libc::SYS_exit_group,libc::SYS_openat2] {
            program.extend([instruction(0x15,0,1,call as u32),instruction(0x06,0,0,ALLOW)]);
        }
        for call in [libc::SYS_write,libc::SYS_writev] {
            program.extend([instruction(0x15,0,4,call as u32),instruction(0x20,0,0,16),instruction(0x15,1,0,3),
                instruction(0x06,0,0,DENY),instruction(0x06,0,0,ALLOW),instruction(0x20,0,0,0)]);
        }
        // All descriptor opens after Landlock are additionally read-only.
        let write_flags=(libc::O_WRONLY|libc::O_RDWR|libc::O_CREAT|libc::O_TRUNC|libc::O_APPEND|0x400000) as u32;
        program.extend([instruction(0x15,0,4,libc::SYS_openat as u32),instruction(0x20,0,0,32),
            instruction(0x45,0,1,write_flags),instruction(0x06,0,0,DENY),instruction(0x06,0,0,ALLOW),instruction(0x20,0,0,0)]);
        #[cfg(target_arch="x86_64")]{
            program.extend([instruction(0x15,0,4,libc::SYS_open as u32),instruction(0x20,0,0,24),
                instruction(0x45,0,1,write_flags),instruction(0x06,0,0,DENY),instruction(0x06,0,0,ALLOW),instruction(0x20,0,0,0)]);
        }
        // Rust File::try_clone needs only this duplication command. No new
        // command can reconfigure the reply pipe or grant asynchronous control.
        program.extend([instruction(0x15,0,4,libc::SYS_fcntl as u32),instruction(0x20,0,0,24),
            instruction(0x15,1,0,libc::F_DUPFD_CLOEXEC as u32),instruction(0x06,0,0,DENY),
            instruction(0x06,0,0,ALLOW),instruction(0x20,0,0,0)]);
        // Heap mappings must be private and non-executable. Shared mappings
        // cannot become an alternative write path to a file or device.
        program.extend([instruction(0x15,0,7,libc::SYS_mmap as u32),instruction(0x20,0,0,32),
            instruction(0x45,0,1,libc::PROT_EXEC as u32),instruction(0x06,0,0,DENY),
            instruction(0x20,0,0,40),instruction(0x45,0,1,libc::MAP_SHARED as u32),
            instruction(0x06,0,0,DENY),instruction(0x06,0,0,ALLOW),instruction(0x20,0,0,0)]);
        program.extend([instruction(0x15,0,4,libc::SYS_mprotect as u32),instruction(0x20,0,0,32),
            instruction(0x45,0,1,libc::PROT_EXEC as u32),instruction(0x06,0,0,DENY),
            instruction(0x06,0,0,ALLOW),instruction(0x20,0,0,0)]);
        program.push(instruction(0x06,0,0,DENY));
        let prog=libc::sock_fprog{len:program.len() as u16,filter:program.as_mut_ptr()};
        if unsafe{libc::syscall(libc::SYS_seccomp,1,0,&prog as *const libc::sock_fprog)}!=0{return Err(io::Error::last_os_error());}
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn readonly_helper_filter_denies_control_and_write_operations() {
        let mut pipe=[-1,-1];assert_eq!(unsafe{libc::pipe2(pipe.as_mut_ptr(),libc::O_CLOEXEC)},0);
        let input=unsafe{File::from_raw_fd(pipe[0])};let output=unsafe{File::from_raw_fd(pipe[1])};
        let child=unsafe{libc::fork()};assert!(child>=0);
        if child==0 {
            if unsafe{libc::dup2(output.as_raw_fd(),3)}<0||unsafe{libc::prctl(libc::PR_SET_NO_NEW_PRIVS,1,0,0,0)}!=0
                ||readonly_filter().is_err(){unsafe{libc::_exit(2);}}
            let mut ok=true;
            let fd=unsafe{libc::openat(libc::AT_FDCWD,c"/dev/null".as_ptr(),libc::O_WRONLY)};
            ok&=fd<0&&io::Error::last_os_error().raw_os_error()==Some(libc::EPERM);
            let fd=unsafe{libc::socket(libc::AF_UNIX,libc::SOCK_STREAM,0)};
            ok&=fd<0&&io::Error::last_os_error().raw_os_error()==Some(libc::EPERM);
            let rc=unsafe{libc::syscall(libc::SYS_setns,-1,0)};
            ok&=rc<0&&io::Error::last_os_error().raw_os_error()==Some(libc::EPERM);
            let rc=unsafe{libc::syscall(libc::SYS_ioctl,-1,0x401c5820u32,0)};
            ok&=rc<0&&io::Error::last_os_error().raw_os_error()==Some(libc::EPERM);
            for call in [libc::SYS_sendfile,libc::SYS_splice,libc::SYS_copy_file_range,
                libc::SYS_fchmod,libc::SYS_unlinkat,libc::SYS_renameat2] {
                let rc=unsafe{libc::syscall(call,-1,-1,-1,-1,-1,-1)};
                ok&=rc<0&&io::Error::last_os_error().raw_os_error()==Some(libc::EPERM);
            }
            let rc=unsafe{libc::mmap(std::ptr::null_mut(),4096,libc::PROT_READ|libc::PROT_WRITE,
                libc::MAP_SHARED|libc::MAP_ANONYMOUS,-1,0)};
            ok&=rc==libc::MAP_FAILED&&io::Error::last_os_error().raw_os_error()==Some(libc::EPERM);
            let byte=[if ok{b'Y'}else{b'N'}];let written=unsafe{libc::write(3,byte.as_ptr().cast(),1)};
            unsafe{libc::_exit(if written==1{0}else{3});}
        }
        drop(output);let mut byte=[0];(&input).read_exact(&mut byte).unwrap();assert_eq!(byte,b"Y"[0..1]);
        let mut status=0;assert_eq!(unsafe{libc::waitpid(child,&mut status,0)},child);assert_eq!(status,0);
    }
    #[test]
    fn view_replies_cannot_contain_admission_flags() {
        assert!(serde_json::from_str::<Reply>(r#"{"ready":true,"fact":null,"error":null}"#).is_err());
        assert!(serde_json::from_str::<Reply>(r#"{"fact":{"device":1},"error":null}"#).is_err());
    }
    #[test]
    fn parent_death_arm_rejects_an_already_reparented_helper() {
        let child=unsafe{libc::fork()};assert!(child>=0);
        if child==0 {
            let actual=unsafe{libc::getppid()};
            let ok=!arm_parent_death(actual.wrapping_add(1),None);
            unsafe{libc::_exit(if ok{0}else{2});}
        }
        let mut status=0;assert_eq!(unsafe{libc::waitpid(child,&mut status,0)},child);assert_eq!(status,0);
    }
    #[test]
    fn closing_reply_without_exiting_has_a_total_deadline() {
        let mut pipe=[-1,-1];assert_eq!(unsafe{libc::pipe2(pipe.as_mut_ptr(),libc::O_CLOEXEC|libc::O_NONBLOCK)},0);
        let input=unsafe{File::from_raw_fd(pipe[0])};let output=unsafe{File::from_raw_fd(pipe[1])};
        let child=unsafe{libc::fork()};assert!(child>=0);
        if child==0 {unsafe{libc::close(pipe[0]);libc::close(pipe[1]);loop{libc::pause();}}}
        drop(output);let pin=open_pidfd(child).unwrap();let start=Instant::now();
        assert!(read_reply(&input,&pin,child,start+std::time::Duration::from_millis(30)).is_err());
        assert!(start.elapsed()<std::time::Duration::from_secs(1));
        unsafe{libc::syscall(libc::SYS_pidfd_send_signal,pin.as_raw_fd(),libc::SIGKILL,std::ptr::null::<libc::siginfo_t>(),0);}
        assert!(reap_bounded(child));
    }
    #[test]
    fn landlock_denies_openat2_write_and_unrelated_file_reads() {
        let fixed=tempfile::NamedTempFile::new().unwrap();std::fs::write(fixed.path(),b"immutable").unwrap();
        let unrelated=tempfile::NamedTempFile::new().unwrap();std::fs::write(unrelated.path(),b"private").unwrap();
        let fixed_path=fixed.path().to_owned();let unrelated_path=unrelated.path().to_owned();
        let child=unsafe{libc::fork()};assert!(child>=0);
        if child==0 {
            if unsafe{libc::prctl(libc::PR_SET_NO_NEW_PRIVS,1,0,0,0)}!=0
                ||readonly_landlock(unsafe{libc::getpid()},&Operation::Artifact(fixed_path.clone())).is_err()
                ||readonly_filter().is_err(){unsafe{libc::_exit(2);}}
            let root=open_root(unsafe{libc::getpid()}).unwrap();
            let mut ok=in_root(&root,&fixed_path,libc::O_RDONLY).is_ok();
            ok&=in_root(&root,&fixed_path,libc::O_WRONLY).is_err();
            ok&=in_root(&root,&unrelated_path,libc::O_RDONLY).is_err();
            unsafe{libc::_exit(if ok{0}else{3});}
        }
        let mut status=0;assert_eq!(unsafe{libc::waitpid(child,&mut status,0)},child);assert_eq!(status,0);
    }
}
