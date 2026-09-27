//! One bounded startup advisory. No provider connection or Python identity.
use std::{fs, io::{self, Read}, os::unix::{fs::MetadataExt, io::AsRawFd, process::CommandExt}, path::Path, process::{Command, Stdio}, sync::{atomic::{AtomicBool, Ordering}, mpsc::{self, Receiver, TryRecvError}, Arc}, thread, time::{Duration, Instant}};

pub const DEFAULT_REPO: &str = "https://github.com/docwilde/doxa";
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
fn marker(executable: &Path) -> io::Result<Option<String>> {
    let bin = executable.parent().ok_or_else(|| io::Error::other("executable directory unavailable"))?;
    let pointer = bin.join(".doxa-sidecar-current");
    if executable.file_name().is_none_or(|name| name != "doxa-rs") { return Ok(None); }
    match fs::symlink_metadata(&pointer) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
        Ok(metadata) if !metadata.file_type().is_symlink() => return Err(io::Error::other("invalid installation pointer")),
        Ok(_) => {}
    }
    let target = fs::read_link(&pointer)?;
    let target = if target.is_absolute() { target } else { bin.join(target) };
    let path = target.parent().ok_or_else(|| io::Error::other("sidecar directory unavailable"))?.join(".doxa-install-sha");
    let metadata = fs::symlink_metadata(&path)?;
    if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len() > 128 || metadata.nlink() != 1 {
        return Err(io::Error::other("invalid installation marker"));
    }
    let mut value = String::new(); fs::File::open(path)?.take(129).read_to_string(&mut value)?;
    if value.len() > 128 { return Err(io::Error::other("invalid installation marker")); }
    sha(&value).map(Some).ok_or_else(|| io::Error::other("invalid installation commit"))
}
fn measure(executable: &Path, source: &Path, config: &Path, repo: &str, skip: bool, cancel: &AtomicBool, deadline: Instant, git: &Path) -> Snapshot {
    let mut rows = vec![format!("Executable · {}", display(&executable.to_string_lossy())),
        format!("Platform · {} ({})", std::env::consts::OS, std::env::consts::ARCH),
        format!("Config · {}{}", display(&config.to_string_lossy()), if config.exists() { "" } else { " (not written yet)" }),
        format!("Update source · {} main", repo_display(repo))];
    let installed = marker(executable);
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
    let mut child = command.spawn()?; let mut output = child.stdout.take().ok_or_else(|| io::Error::other("advisory output unavailable"))?;
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
    #[test]
    fn hanging_and_oversized_commands_are_stopped_and_invalid_remote_rows_rejected() {
        let dir = tempfile::tempdir().unwrap(); let cancel = AtomicBool::new(false);
        let git = script(dir.path(), "sleep 30"); let started = Instant::now();
        assert!(run(&git, &[], None, &cancel, started+Duration::from_millis(50)).is_err()); assert!(started.elapsed()<Duration::from_secs(1));
        let git = script(dir.path(), "yes x"); assert!(run(&git, &[], None, &cancel, Instant::now()+TIMEOUT).is_err());
        assert!(remote_main(&format!("{A} refs/heads/other")).is_none()); assert!(remote_main(&format!("{A} refs/heads/main\n{B} refs/heads/main")).is_none());
    }
}
