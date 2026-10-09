//! One bounded startup advisory. No provider connection or Python identity.
use std::{fs, io::{self, Read}, os::unix::{fs::{MetadataExt, OpenOptionsExt, PermissionsExt}, io::AsRawFd, process::CommandExt}, path::Path, process::{Command, Stdio}, sync::{atomic::{AtomicBool, Ordering}, mpsc::{self, Receiver, TryRecvError}, Arc}, thread, time::{Duration, Instant}};

pub const DEFAULT_REPO: &str = "https://github.com/docwilde/doxa";

/// Install the desktop entry and compiled icons without a scripting runtime.
pub fn install_launcher(command: &Path) -> io::Result<std::path::PathBuf> {
    if !command.is_absolute() || !command.is_file() {return Err(io::Error::other("launcher must be an absolute file"));}
    #[cfg(target_os = "macos")]
    {
        let home=std::env::var_os("HOME").ok_or_else(||io::Error::other("HOME is unavailable"))?;
        let home=std::path::PathBuf::from(home);
        if !home.is_absolute(){return Err(io::Error::other("HOME must be absolute"));}
        let script=home.join("Applications/DOXA.command");
        let value=command.to_string_lossy();
        if value.chars().any(char::is_control){return Err(io::Error::other("launcher path contains control characters"));}
        let quoted=format!("'{}'",value.replace('\'',"'\\''"));
        LauncherDir::open(script.parent().unwrap())?.write_mode(script.file_name().unwrap(),
            format!("#!/bin/sh\nexec {quoted} \"$@\"\n").as_bytes(),0o700)?;
        return Ok(script);
    }
    #[cfg(not(target_os = "macos"))]
    {
    let word=desktop_word(&command.to_string_lossy())?;
    let home=std::env::var_os("HOME").ok_or_else(||io::Error::other("HOME is unavailable"))?;
    let data=std::env::var_os("XDG_DATA_HOME").filter(|v|!v.is_empty()).map(std::path::PathBuf::from).unwrap_or_else(||std::path::PathBuf::from(home).join(".local/share"));
    if !data.is_absolute(){return Err(io::Error::other("XDG_DATA_HOME must be absolute"));}
    let write=|path:&Path,bytes:&[u8]|->io::Result<()> {
        let parent=path.parent().ok_or_else(||io::Error::other("missing asset directory"))?;
        let name=path.file_name().ok_or_else(||io::Error::other("missing asset name"))?;
        LauncherDir::open(parent)?.write(name,bytes)
    };
    let desktop=data.join("applications/doxa.desktop");
    write(&desktop,format!("[Desktop Entry]\nType=Application\nName=DOXA\nGenericName=Agent terminal\nComment=Agent terminal with reviewed memory\nExec={word}\nIcon=doxa\nTerminal=true\nCategories=Development;Utility;\nKeywords=claude;codex;agent;terminal;lore;memory;\nX-DOXA-Version={}\n",env!("CARGO_PKG_VERSION")).as_bytes())?;
    write(&data.join("icons/hicolor/512x512/apps/doxa.png"),include_bytes!("../../../assets/icon.png"))?;
    write(&data.join("icons/hicolor/scalable/apps/doxa.svg"),include_bytes!("../../../assets/icon.svg"))?;
    for(mut command,path)in [(Command::new("update-desktop-database"),data.join("applications")),(Command::new("gtk-update-icon-cache"),data.join("icons/hicolor"))] {
        let _=command.arg(path).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).status();
    }
    Ok(desktop)
    }
}
/// Pin both temporary creation and publication to the checked directory inode.
/// A renamed/replaced ancestor must not redirect a desktop asset write.
struct LauncherDir(fs::File);
impl LauncherDir {
    fn open(path:&Path)->io::Result<Self> {
        use std::os::unix::fs::DirBuilderExt;
        // Explicit permissions keep fresh XDG paths safe under a group-writable
        // caller umask; existing unsafe directories must still be refused.
        fs::DirBuilder::new().recursive(true).mode(0o755).create(path)?;
        let file=fs::OpenOptions::new().read(true)
            .custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(path)?;
        let meta=file.metadata()?;
        if !meta.is_dir() || meta.uid()!=unsafe {libc::geteuid()} || meta.mode()&0o022!=0 {
            return Err(io::Error::other("unsafe launcher directory"));
        }
        Ok(Self(file))
    }
    fn write(&self,name:&std::ffi::OsStr,bytes:&[u8])->io::Result<()> {
        self.write_mode(name,bytes,0o600)
    }
    fn write_mode(&self,name:&std::ffi::OsStr,bytes:&[u8],mode:u32)->io::Result<()> {
        use std::{ffi::CString,os::fd::FromRawFd,io::Write};
        if name.as_encoded_bytes().contains(&b'/') {return Err(io::Error::other("invalid asset name"));}
        let target=CString::new(name.as_encoded_bytes())?;
        let mut random=[0u8;16];fs::File::open("/dev/urandom")?.read_exact(&mut random)?;
        let temporary=CString::new(format!(".doxa-{}",random.iter().map(|b|format!("{b:02x}")).collect::<String>()))?;
        let directory=self.0.as_raw_fd();
        let fd=unsafe {libc::openat(directory,temporary.as_ptr(),libc::O_WRONLY|libc::O_CREAT|libc::O_EXCL|libc::O_NOFOLLOW|libc::O_CLOEXEC,0o600)};
        if fd<0 {return Err(io::Error::last_os_error());}
        let mut file=unsafe {fs::File::from_raw_fd(fd)};
        let result=(|| {
            file.write_all(bytes)?;file.set_permissions(fs::Permissions::from_mode(mode))?;file.sync_all()?;
            if unsafe {libc::renameat(directory,temporary.as_ptr(),directory,target.as_ptr())}!=0 {return Err(io::Error::last_os_error());}
            self.0.sync_all()
        })();
        if result.is_err(){unsafe {libc::unlinkat(directory,temporary.as_ptr(),0);}}
        result
    }
}
fn desktop_word(value:&str)->io::Result<String> {
    if value.contains('=') || value.chars().any(char::is_control){return Err(io::Error::other("launcher path cannot be represented in desktop entry"));}
    // Desktop entry string escaping is decoded before Exec argument escaping.
    let escaped=value.replace('%',"%%").replace('\\',"\\\\\\\\").replace('"',"\\\\\"").replace('`',"\\\\`").replace('$',"\\\\$");
    Ok(format!("\"{escaped}\""))
}
const LIMIT: usize = 4096;
const TIMEOUT: Duration = Duration::from_secs(8);

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update { Unchecked, Skipped, Current, Available, Unknown }
impl Update {
    pub fn label(&self) -> &'static str { match self {
        Self::Unchecked => "not checked yet", Self::Skipped => "check skipped",
        Self::Current => "matches configured main", Self::Available => "configured main differs · /update",
        Self::Unknown => "check unavailable",
    } }
}
#[derive(Clone, Debug)]
pub struct Snapshot { pub rows: Vec<String>, pub update: Update }
impl Default for Snapshot {
    fn default() -> Self { Self { rows: Vec::new(), update: Update::Unchecked } }
}
pub struct Worker { receiver: Receiver<Snapshot>, cancel: Arc<AtomicBool>, worker: Option<thread::JoinHandle<()>> }
impl Worker {
    pub fn start() -> io::Result<Self> {
        let executable = std::env::current_exe()?;
        let source = Path::new(env!("CARGO_MANIFEST_DIR")).parent().and_then(Path::parent)
            .ok_or_else(|| io::Error::other("source directory unavailable"))?.to_path_buf();
        let config = crate::settings::config_path()?;
        let repo = std::env::var("DOXA_RUST_REPO_URL").unwrap_or_else(|_| DEFAULT_REPO.into());
        let skip = std::env::var("DOXA_SKIP_UPDATE_CHECK").is_ok_and(|value| !value.trim().is_empty());
        let (sender, receiver) = mpsc::sync_channel(1);
        let cancel = Arc::new(AtomicBool::new(false)); let cancelled = cancel.clone();
        let worker = thread::Builder::new().name("installation-advisory".into()).spawn(move || {
            let result = measure(&executable, &source, &config, &repo, skip, &cancelled, Instant::now() + TIMEOUT, Path::new("git"));
            let _ = sender.send(result);
        })?;
        Ok(Self { receiver, cancel, worker: Some(worker) })
    }
    pub fn poll(&self) -> Option<Snapshot> { match self.receiver.try_recv() {
        Ok(result) => Some(result), Err(TryRecvError::Empty) => None,
        Err(TryRecvError::Disconnected) => Some(Snapshot { rows: Vec::new(), update: Update::Unknown }),
    } }
}
impl Drop for Worker {
    fn drop(&mut self) { self.cancel.store(true, Ordering::Release); if let Some(worker) = self.worker.take() { let _ = worker.join(); } }
}
fn sha(value: &str) -> Option<String> {
    let value = value.trim();
    (value.len() == 40 && value.bytes().all(|byte| byte.is_ascii_hexdigit())).then(|| value.to_ascii_lowercase())
}
fn display(value: &str) -> String { value.chars().filter(|char| !char.is_control()).take(1024).collect() }
fn repo_display(repo: &str) -> String {
    let repo = repo.split(['?', '#']).next().unwrap_or(repo);
    if let Some(start) = repo.find("://") {
        let authority = &repo[start + 3..];
        if let Some(at) = authority.split('/').next().and_then(|part| part.rfind('@')) {
            return display(&format!("{}[redacted]@{}", &repo[..start + 3], &authority[at + 1..]));
        }
    }
    display(repo)
}
pub fn installed_commit(executable: &Path) -> io::Result<Option<String>> {
    let bin = executable.parent().ok_or_else(|| io::Error::other("executable directory unavailable"))?;
    if executable.file_name().is_none_or(|name| name != "doxa-rs") { return Ok(None); }
    let native = bin.join(".doxa-install-sha");
    match std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(&native) {
        Ok(file) => {
            let metadata=file.metadata()?;
            if !metadata.is_file() || metadata.uid()!=unsafe {libc::geteuid()} || metadata.nlink()!=1 || metadata.len()>128 || metadata.mode()&0o077!=0 {return Err(io::Error::other("invalid installation marker"));}
            let mut value=String::new();file.take(129).read_to_string(&mut value)?;
            return sha(&value).map(Some).ok_or_else(||io::Error::other("invalid installation commit"));
        },
        Err(error) if error.kind()==io::ErrorKind::NotFound=>{},
        Err(error)=>return Err(error),
    }
    // Read an older installation's marker so `doxa update` can migrate it.
    let pointer = bin.join(".doxa-sidecar-current");
    match fs::symlink_metadata(&pointer) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
        Ok(metadata) if !metadata.file_type().is_symlink() => return Err(io::Error::other("invalid installation pointer")),
        Ok(_) => {}
    }
    let target = fs::read_link(&pointer)?;
    let target = if target.is_absolute() { target } else { bin.join(target) };
    let path = target.parent().ok_or_else(|| io::Error::other("sidecar directory unavailable"))?.join(".doxa-install-sha");
    let file=std::fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK).open(&path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.len()>128 || metadata.nlink()!=1 || metadata.uid()!=unsafe {libc::geteuid()} {
        return Err(io::Error::other("invalid installation marker"));
    }
    let mut value = String::new(); file.take(129).read_to_string(&mut value)?;
    if value.len() > 128 { return Err(io::Error::other("invalid installation marker")); }
    sha(&value).map(Some).ok_or_else(|| io::Error::other("invalid installation commit"))
}
fn measure(executable: &Path, source: &Path, config: &Path, repo: &str, skip: bool, cancel: &AtomicBool, deadline: Instant, git: &Path) -> Snapshot {
    let mut rows = vec![format!("Executable · {}", display(&executable.to_string_lossy())),
        format!("Platform · {} ({})", std::env::consts::OS, std::env::consts::ARCH),
        format!("Config · {}{}", display(&config.to_string_lossy()), if config.exists() { "" } else { " (not written yet)" }),
        format!("Update source · {} main", repo_display(repo))];
    let installed = installed_commit(executable);
    let local = match installed {
        Ok(Some(value)) => { rows.push(format!("Installed commit · {value}")); Some(value) }
        Err(_) => { rows.push("Installed commit · unavailable".into()); None }
        Ok(None) if source.join(".git").exists() => {
            rows.push(format!("Source checkout · {}", display(&source.to_string_lossy())));
            let value = run(git, &["rev-parse", "--verify", "HEAD"], Some(source), cancel, deadline).ok().and_then(|value| sha(&value));
            if let Some(value) = &value { rows.push(format!("Source commit · {value}")); }
            rows.push(format!("Source worktree · {}", match run(git, &["status", "--porcelain"], Some(source), cancel, deadline) {
                Ok(status) if status.trim().is_empty() => "clean", Ok(_) => "modified", Err(_) => "status unavailable",
            }));
            value
        }
        Ok(None) => None,
    };
    let update = if skip { Update::Skipped } else if let Some(local) = local {
        if repo.trim().is_empty() || repo.len() > 4096 || repo.chars().any(char::is_control) { Update::Unknown }
        else {
            match run(git, &["-c", "credential.helper=", "ls-remote", "--exit-code", "--", repo, "refs/heads/main"], None, cancel, deadline)
                .ok().and_then(|value| remote_main(&value)) {
                Some(remote) if remote == local => Update::Current,
                Some(_) => Update::Available, None => Update::Unknown,
            }
        }
    } else { Update::Unknown };
    Snapshot { rows, update }
}
fn remote_main(value: &str) -> Option<String> {
    let mut lines = value.lines(); let mut fields = lines.next()?.split_whitespace();
    let hash = sha(fields.next()?)?;
    (fields.next()? == "refs/heads/main" && fields.next().is_none() && lines.next().is_none()).then_some(hash)
}
fn run(program: &Path, args: &[&str], cwd: Option<&Path>, cancel: &AtomicBool, deadline: Instant) -> io::Result<String> {
    if cancel.load(Ordering::Acquire) || Instant::now() >= deadline { return Err(io::Error::other("advisory cancelled")); }
    let mut command = Command::new(program); command.args(args).process_group(0).stdin(Stdio::null()).stderr(Stdio::null()).stdout(Stdio::piped())
        .env("GIT_TERMINAL_PROMPT", "0").env("GIT_ASKPASS", "/bin/false").env("SSH_ASKPASS", "/bin/false");
    if let Some(cwd) = cwd { command.current_dir(cwd); }
    // An updater can briefly hold Git open for writing. Bound retries to the
    // transient executable-busy case and stay within the advisory deadline.
    let mut busy_retries = 0;
    let mut child = loop {
        if cancel.load(Ordering::Acquire) || Instant::now() >= deadline {
            return Err(io::Error::other("advisory cancelled or timed out"));
        }
        match command.spawn() {
            Ok(child) => break child,
            Err(error) if error.raw_os_error() == Some(libc::ETXTBSY)
                && busy_retries < 3 && Instant::now() + Duration::from_millis(30) < deadline => {
                busy_retries += 1;
                thread::sleep(Duration::from_millis(30));
            }
            Err(error) => return Err(error),
        }
    };
    let mut output = child.stdout.take().ok_or_else(|| io::Error::other("advisory output unavailable"))?;
    let fd = output.as_raw_fd();
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    let nonblocking = flags >= 0 && unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } >= 0;
    let mut reaped = false;
    let result = (|| {
        if !nonblocking { return Err(io::Error::other("advisory output unavailable")); }
        let mut bytes = Vec::new(); let mut status = None;
        loop {
            let mut buffer = [0; 1024];
            loop { match output.read(&mut buffer) {
                Ok(0) => break, Ok(count) => { bytes.extend_from_slice(&buffer[..count]); if bytes.len() > LIMIT { return Err(io::Error::other("advisory output too large")); } }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            } }
            if let Some(status) = status { return if status { String::from_utf8(bytes).map_err(io::Error::other) } else { Err(io::Error::other("advisory command failed")) }; }
            if cancel.load(Ordering::Acquire) || Instant::now() >= deadline { return Err(io::Error::other("advisory timed out")); }
            status = child.try_wait()?.map(|status| status.success());
            reaped = status.is_some();
            if status.is_none() { thread::sleep(Duration::from_millis(10)); }
        }
    })();
    if result.is_err() && !reaped {
        // The unreaped group leader owns this PID; stop only its descendants.
        unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL); }
        let _ = child.wait();
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*; use std::{path::PathBuf, os::unix::fs::{symlink, PermissionsExt}};
    const A: &str = "1111111111111111111111111111111111111111";
    const B: &str = "2222222222222222222222222222222222222222";
    fn installed(root: &Path) -> PathBuf {
        let bin = root.join("bin"); let env = root.join("sidecars").join(A); fs::create_dir_all(&bin).unwrap(); fs::create_dir_all(env.join("bin")).unwrap();
        fs::write(env.join(".doxa-install-sha"), A).unwrap(); symlink(env.join("bin"), bin.join(".doxa-sidecar-current")).unwrap(); bin.join("doxa-rs")
    }
    fn script(root: &Path, text: &str) -> PathBuf {
        let path = root.join("git-fixture"); fs::write(&path, format!("#!/bin/sh\n{text}\n")).unwrap(); fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap(); path
    }
    #[test]
    fn native_marker_and_desktop_writes_refuse_links_and_pin_the_directory() {
        let root=tempfile::tempdir().unwrap();
        let bin=root.path().join("bin");fs::create_dir(&bin).unwrap();
        let marker=bin.join(".doxa-install-sha");fs::write(&marker,A).unwrap();
        fs::set_permissions(&marker,fs::Permissions::from_mode(0o600)).unwrap();
        let exe=bin.join("doxa-rs");assert_eq!(installed_commit(&exe).unwrap(),Some(A.into()));
        fs::hard_link(&marker,bin.join("linked")).unwrap();assert!(installed_commit(&exe).is_err());
        fs::remove_file(&marker).unwrap();symlink(bin.join("linked"),&marker).unwrap();assert!(installed_commit(&exe).is_err());
        let parent=root.path().join("applications");fs::create_dir(&parent).unwrap();
        fs::set_permissions(&parent,fs::Permissions::from_mode(0o700)).unwrap();
        let writer=LauncherDir::open(&parent).unwrap();let moved=root.path().join("original");
        fs::rename(&parent,&moved).unwrap();fs::create_dir(&parent).unwrap();
        writer.write(std::ffi::OsStr::new("doxa.desktop"),b"native").unwrap();
        assert_eq!(fs::read(moved.join("doxa.desktop")).unwrap(),b"native");
        assert!(!parent.join("doxa.desktop").exists());
        assert!(desktop_word("/path/line\nbreak").is_err());
        assert_eq!(desktop_word("/path/100%/doxa").unwrap(),"\"/path/100%%/doxa\"");
    }
    #[test]
    fn installed_identity_skip_and_failures_never_claim_current() {
        let dir = tempfile::tempdir().unwrap(); let exe = installed(dir.path()); let cancel = AtomicBool::new(false);
        let git = script(dir.path(), "exit 7");
        let snapshot = measure(&exe, dir.path(), &dir.path().join("config.toml"), DEFAULT_REPO, true, &cancel, Instant::now()+TIMEOUT, &git);
        assert_eq!(snapshot.update, Update::Skipped); assert!(snapshot.rows.iter().any(|row| row.contains(A)));
        assert_eq!(measure(&exe, dir.path(), &dir.path().join("config.toml"), DEFAULT_REPO, false, &cancel, Instant::now()+TIMEOUT, &git).update, Update::Unknown);
        let git = script(dir.path(), &format!("test \"$1\" = -c && test \"$2\" = credential.helper= && test \"$3\" = ls-remote && test \"$6\" = '{}' && printf '{}\\trefs/heads/main\\n'", DEFAULT_REPO, A));
        assert_eq!(measure(&exe, dir.path(), &dir.path().join("config.toml"), DEFAULT_REPO, false, &cancel, Instant::now()+TIMEOUT, &git).update, Update::Current);
        let git = script(dir.path(), &format!("printf '{}\\trefs/heads/main\\n'", B));
        assert_eq!(measure(&exe, dir.path(), &dir.path().join("config.toml"), DEFAULT_REPO, false, &cancel, Instant::now()+TIMEOUT, &git).update, Update::Available);
        assert_eq!(repo_display("https://secret@example.test/repo"), "https://[redacted]@example.test/repo");
    }
    #[cfg(target_os = "linux")]
    #[test]
    fn briefly_busy_git_advisory_recovers_within_deadline() {
        let dir = tempfile::tempdir().unwrap();
        let git = script(dir.path(), "printf 'ready'");
        let writer = fs::OpenOptions::new().write(true).open(&git).unwrap();
        let release = thread::spawn(move || {
            thread::sleep(Duration::from_millis(50));
            drop(writer);
        });
        let cancel = AtomicBool::new(false);
        let output = run(&git, &[], None, &cancel, Instant::now() + TIMEOUT).unwrap();
        release.join().unwrap();
        assert_eq!(output, "ready");
    }
    #[test]
    fn hanging_and_oversized_commands_are_stopped_and_invalid_remote_rows_rejected() {
        let dir = tempfile::tempdir().unwrap(); let cancel = AtomicBool::new(false);
        let git = script(dir.path(), "sleep 30"); let started = Instant::now();
        assert!(run(&git, &[], None, &cancel, started+Duration::from_millis(50)).is_err()); assert!(started.elapsed()<Duration::from_secs(1));
        let git = script(dir.path(), "yes x"); assert!(run(&git, &[], None, &cancel, Instant::now()+TIMEOUT).is_err());
        assert!(remote_main(&format!("{A} refs/heads/other")).is_none()); assert!(remote_main(&format!("{A} refs/heads/main\n{B} refs/heads/main")).is_none());
    }
}
