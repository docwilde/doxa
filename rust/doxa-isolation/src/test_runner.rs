//! Bounded checkout source capture for owner-run fleet tests. Only an
//! offline Docker checkout can yield host evidence; no project executable is
//! launched on the host.
use crate::{error, workspace, Manifest, Profile};
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, ffi::CString, fs::{self, File}, io::{self, Read, Write}, os::{fd::{AsRawFd, FromRawFd}, unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt}, unix::process::CommandExt}, path::Path, process::{Command, Stdio}, sync::{atomic::{AtomicBool, Ordering}, mpsc}, time::{Duration, Instant}};

const MAX_PATHS: usize = 4096;
const MAX_LIST_BYTES: usize = 512 * 1024;
const MAX_FILE_BYTES: u64 = 8 * 1024 * 1024;
const MAX_TOTAL_BYTES: u64 = 128 * 1024 * 1024;
const MAX_OUTPUT_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone)]
pub struct Capture { pub sha256: String, pub files: usize, pub bytes: u64 }

#[derive(Debug, Clone)]
pub struct RunResult { pub exit_code: i32, pub duration_ms: u64, pub output_sha256: String, pub output_bytes: u64, pub passed: bool }

#[derive(Debug)]
pub struct CleanupUnconfirmed(pub String);
impl std::fmt::Display for CleanupUnconfirmed {
    fn fmt(&self,f:&mut std::fmt::Formatter<'_>)->std::fmt::Result{write!(f,"fleet test Docker cleanup unconfirmed: {}",self.0)}
}
impl std::error::Error for CleanupUnconfirmed {}
pub fn cleanup_unconfirmed(error:&io::Error)->bool {
    error.get_ref().is_some_and(|source|source.is::<CleanupUnconfirmed>())
}

fn command_for(manifest: &Manifest, snapshot: &Path, argv: &[String], cwd_relative: &str, name: &str) -> io::Result<Command> {
    let policy = manifest.policy.as_ref().ok_or_else(|| error("offline Docker test policy unavailable"))?;
    if manifest.profile != Profile::DockerOffline || argv.is_empty() || !argv[0].starts_with('/')
        || cwd_relative.starts_with('/') || cwd_relative.split('/').any(|part| part == ".." || part == ".") {
        return Err(error("invalid offline fleet test command"));
    }
    let source = snapshot.to_str().filter(|value| !value.contains([',', '\n', '\r']))
        .ok_or_else(|| error("invalid fleet test snapshot path"))?;
    let mut command = Command::new("docker");
    command.env_clear().env("PATH", "/usr/bin:/bin").args(["--host", &policy.docker_host, "run", "--rm", "--pull=never",
        "--name", name, "--network", "none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true", "--user=0:0", "--ipc=private", "--cgroupns=private",
        "--memory", "1073741824", "--memory-swap", "1073741824", "--cpus", "1", "--pids-limit", "128",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=67108864,mode=1777",
        "--tmpfs=/scratch:rw,nosuid,nodev,size=536870912,mode=1777",
        "--mount", &format!("type=bind,src={source},dst=/workspace,readonly"),
        "--env", "HOME=/scratch", "--env", "TMPDIR=/scratch", "--env", "CARGO_TARGET_DIR=/scratch/target",
        "--workdir", &format!("/workspace/{cwd_relative}"), "--entrypoint", &argv[0], &policy.image]);
    command.args(&argv[1..]);
    Ok(command)
}

fn remove_test_container(manifest: &Manifest, name: &str) -> io::Result<()> {
    if let Some(policy) = &manifest.policy {
        let mut command = Command::new("docker");
        command.env_clear().env("PATH", "/usr/bin:/bin").args(["--host", &policy.docker_host, "rm", "-f", name])
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        let mut child = command.spawn()?;
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(status) = child.try_wait()? { return if status.success() { Ok(()) } else { Err(error("fleet test container cleanup failed")) }; }
            if Instant::now() >= deadline { let _ = child.kill(); let _ = child.wait(); return Err(error("fleet test container cleanup timed out")); }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
    Err(error("fleet test container has no Docker policy"))
}

fn read_output(mut input: impl Read + Send + 'static, stream: usize, tx: mpsc::SyncSender<(usize, io::Result<Vec<u8>>)>) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        loop {
            let mut buf = [0u8; 4096];
            match input.read(&mut buf) {
                Ok(0) => { let _ = tx.send((stream, Ok(Vec::new()))); break; },
                Ok(count) => { if tx.send((stream, Ok(buf[..count].to_vec()))).is_err() { break; } },
                Err(err) => { let _ = tx.send((stream, Err(err))); break; },
            }
        }
    })
}

/// Run only from a host-owned copied snapshot. No provider home, broker,
/// credential or worker container is mounted into this ephemeral container.
pub fn run_offline(manifest: &Manifest, snapshot: &Path, argv: &[String], cwd_relative: &str, timeout_s: u64) -> io::Result<RunResult> {
    let cancel=AtomicBool::new(false);
    run_offline_cancel(manifest,snapshot,argv,cwd_relative,timeout_s,&cancel)
}

/// The fleet controller keeps this cancellation flag until the Docker CLI is
/// reaped and the named container cleanup has been attempted and confirmed.
pub fn run_offline_cancel(manifest: &Manifest, snapshot: &Path, argv: &[String], cwd_relative: &str, timeout_s: u64,
    cancel:&AtomicBool) -> io::Result<RunResult> {
    if !(1..=300).contains(&timeout_s) || !snapshot.is_dir() { return Err(error("invalid fleet test snapshot or timeout")); }
    if cancel.load(Ordering::Acquire) { return Err(io::Error::new(io::ErrorKind::Interrupted,"fleet host test cancelled")); }
    crate::preflight(manifest.policy.as_ref().ok_or_else(|| error("offline Docker policy unavailable"))?)?;
    if cancel.load(Ordering::Acquire) { return Err(io::Error::new(io::ErrorKind::Interrupted,"fleet host test cancelled")); }
    let name = format!("doxa-test-{}-{}", &format!("{:x}", Sha256::digest(manifest.session_id.as_bytes()))[..12], std::process::id());
    let command = command_for(manifest, snapshot, argv, cwd_relative, &name)?;
    execute_bounded_cancel(command, timeout_s, || remove_test_container(manifest, &name),cancel)
}

#[cfg(test)]
fn execute_bounded(command: Command, timeout_s: u64, cleanup_container: impl FnOnce() -> io::Result<()>) -> io::Result<RunResult> {
    let cancel=AtomicBool::new(false);
    execute_bounded_cancel(command,timeout_s,cleanup_container,&cancel)
}

fn execute_bounded_cancel(mut command: Command, timeout_s: u64, cleanup_container: impl FnOnce() -> io::Result<()>,cancel:&AtomicBool) -> io::Result<RunResult> {
    if cancel.load(Ordering::Acquire) { return Err(io::Error::new(io::ErrorKind::Interrupted,"fleet host test cancelled")); }
    let start = Instant::now();
    let mut child = command.process_group(0).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn()?;
    let (tx, rx) = mpsc::sync_channel::<(usize, io::Result<Vec<u8>>)>(16);
    let readers = [read_output(child.stdout.take().unwrap(), 0, tx.clone()),
        read_output(child.stderr.take().unwrap(), 1, tx.clone())];
    drop(tx);
    let mut outputs = [Sha256::new(), Sha256::new()];
    let mut total = 0usize;
    let mut exceeded = false;
    let mut io_failed = false;
    let mut ended = 0usize;
    let deadline = start + Duration::from_secs(timeout_s);
    let mut status = None;
    let mut timed_out = false;
    let mut cancelled = false;
    loop {
        if let Ok((stream, result)) = rx.recv_timeout(Duration::from_millis(20)) {
            match result {
                Ok(bytes) if bytes.is_empty() => ended += 1,
                Ok(bytes) => { total = total.saturating_add(bytes.len()); if total > MAX_OUTPUT_BYTES { exceeded = true; } else { outputs[stream].update(&bytes); } },
                Err(_) => io_failed = true,
            }
        }
        if status.is_none() { status = child.try_wait()?; }
        if status.is_some() && ended == 2 { break; }
        if exceeded || io_failed { break; }
        if cancel.load(Ordering::Acquire) { cancelled=true; break; }
        if Instant::now() >= deadline { timed_out = true; break; }
    }
    let cleanup = if status.is_none() || exceeded || io_failed || timed_out || cancelled {
        unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL); }
        let _ = child.kill();
        cleanup_container()
    } else { Ok(()) };
    let status = child.wait()?;
    drop(rx);
    for reader in readers { let _ = reader.join(); }
    cleanup.map_err(|error|io::Error::other(CleanupUnconfirmed(error.to_string())))?;
    if cancelled {return Err(io::Error::new(io::ErrorKind::Interrupted,"fleet host test cancelled after Docker cleanup"));}
    let elapsed = start.elapsed().as_millis().min(u64::MAX as u128) as u64;
    let mut summary = Sha256::new(); for output in outputs { summary.update(output.finalize()); }
    Ok(RunResult { exit_code: status.code().unwrap_or(-1), duration_ms: elapsed,
        output_sha256: format!("{:x}", summary.finalize()), output_bytes: total as u64,
        passed: status.success() && !exceeded && !io_failed && !timed_out && elapsed <= timeout_s * 1000 })
}

fn git_visible(cwd: &Path) -> io::Result<BTreeSet<String>> {
    let mut git = Command::new("git");
    // Include ignored files too. A worker controls .gitignore and
    // .git/info/exclude, so exclusions cannot define signed source.
    git.current_dir(cwd).args(["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "-c", "core.attributesFile=/dev/null", "ls-files", "--cached", "--others", "-z"]);
    let mut command = workspace::command(git)?;
    let mut child = command.process_group(0).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn()?;
    let mut output = child.stdout.take().ok_or_else(|| error("fleet source listing unavailable"))?;
    if unsafe { libc::fcntl(output.as_raw_fd(), libc::F_SETFL, libc::O_NONBLOCK) } < 0 {
        let _ = child.kill(); let _ = child.wait(); return Err(io::Error::last_os_error());
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut bytes = Vec::new();
    let result = (|| -> io::Result<BTreeSet<String>> {
        loop {
            let mut buf = [0u8; 4096];
            match output.read(&mut buf) {
                Ok(0) => if let Some(status) = child.try_wait()? {
                    if !status.success() { return Err(error("fleet source Git listing failed")); }
                    break;
                },
                Ok(count) => { bytes.extend_from_slice(&buf[..count]); if bytes.len() > MAX_LIST_BYTES { return Err(error("fleet source listing exceeds bound")); } }
                Err(err) if err.kind() == io::ErrorKind::WouldBlock => {},
                Err(err) => return Err(err),
            }
            if Instant::now() >= deadline { return Err(error("fleet source listing timed out")); }
            std::thread::sleep(Duration::from_millis(5));
        }
        let mut paths = BTreeSet::new();
        for raw in bytes.split(|byte| *byte == 0).filter(|path| !path.is_empty()) {
            let path = std::str::from_utf8(raw).map_err(|_| error("fleet source paths must be UTF-8"))?;
            if path.starts_with('/') || path.len() > 512 || path.chars().any(char::is_control)
                || path.split('/').any(|part| part.is_empty() || part == "." || part == ".." || part == ".git") {
                return Err(error("fleet source path escapes checkout"));
            }
            paths.insert(path.to_owned());
            if paths.len() > MAX_PATHS { return Err(error("fleet source file count exceeds bound")); }
        }
        Ok(paths)
    })();
    if result.is_err() { unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL); } let _ = child.kill(); }
    let _ = child.wait();
    result
}

fn open_relative(root: &File, path: &str) -> io::Result<Option<File>> {
    let mut current = root.try_clone()?;
    let mut parts = path.split('/').peekable();
    while let Some(part) = parts.next() {
        let name = CString::new(part).map_err(|_| error("invalid fleet source name"))?;
        let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC
            | if parts.peek().is_some() { libc::O_DIRECTORY } else { 0 };
        let fd = unsafe { libc::openat(current.as_raw_fd(), name.as_ptr(), flags) };
        if fd < 0 {
            let err = io::Error::last_os_error();
            if err.kind() == io::ErrorKind::NotFound { return Ok(None); }
            return Err(err);
        }
        current = unsafe { File::from_raw_fd(fd) };
    }
    Ok(Some(current))
}

fn capture_paths(cwd: &Path, destination: Option<&Path>, paths: BTreeSet<String>) -> io::Result<Capture> {
    let root = fs::OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW).open(cwd)?;
    let mut digest = Sha256::new();
    let mut total = 0u64;
    let mut count = 0usize;
    for path in paths {
        digest.update((path.len() as u32).to_be_bytes()); digest.update(path.as_bytes());
        let Some(mut file) = open_relative(&root, &path)? else { digest.update(b"deleted"); continue; };
        let before = file.metadata()?;
        if !before.is_file() || before.nlink() != 1 || before.len() > MAX_FILE_BYTES {
            return Err(error("fleet source contains linked, special or oversized file"));
        }
        total = total.checked_add(before.len()).ok_or_else(|| error("fleet source byte overflow"))?;
        if total > MAX_TOTAL_BYTES { return Err(error("fleet source bytes exceed bound")); }
        let mut bytes = Vec::with_capacity(before.len() as usize);
        Read::by_ref(&mut file).take(MAX_FILE_BYTES + 1).read_to_end(&mut bytes)?;
        let after = file.metadata()?;
        if bytes.len() as u64 != before.len() || before.dev() != after.dev() || before.ino() != after.ino()
            || before.mtime() != after.mtime() || before.mtime_nsec() != after.mtime_nsec() || before.len() != after.len() {
            return Err(error("fleet source changed during capture"));
        }
        digest.update(b"file"); digest.update(before.len().to_be_bytes()); digest.update((before.mode() & 0o111).to_be_bytes()); digest.update(Sha256::digest(&bytes));
        if let Some(dest) = destination {
            let target = dest.join(&path);
            if let Some(parent) = target.parent() { fs::create_dir_all(parent)?; }
            let mut output = fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(target)?;
            output.write_all(&bytes)?;
            output.set_permissions(fs::Permissions::from_mode(if before.mode() & 0o111 != 0 { 0o500 } else { 0o400 }))?;
        }
        count += 1;
    }
    Ok(Capture { sha256: format!("{:x}", digest.finalize()), files: count, bytes: total })
}

pub fn capture(cwd: &Path, destination: Option<&Path>) -> io::Result<Capture> {
    let manifest = workspace::manifest_for(cwd)?.ok_or_else(|| error("host test evidence requires a Docker checkout"))?;
    if manifest.profile != Profile::DockerOffline || cwd != manifest.checkout {
        return Err(error("host test evidence requires the offline Docker checkout root"));
    }
    let paths = git_visible(cwd)?;
    capture_paths(cwd, destination, paths)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Policy;
    #[test]
    fn ignored_regular_files_are_included_in_signed_source() {
        let root = tempfile::tempdir().unwrap();
        let source = root.path().join("worker"); fs::create_dir(&source).unwrap();
        let git = |args: &[&str]| {
            let status = Command::new("git").env_clear().env("PATH", std::env::var_os("PATH").unwrap_or_default())
                .current_dir(&source).args(args).status().unwrap();
            assert!(status.success(), "{args:?}");
        };
        git(&["init", "-q"]);
        fs::write(source.join(".gitignore"), "outside/\n").unwrap();
        fs::create_dir(source.join("outside")).unwrap();
        fs::write(source.join("outside/hidden.txt"), "ignored but present\n").unwrap();
        fs::write(source.join(".git/info/exclude"), "excluded.log\n").unwrap();
        fs::write(source.join("excluded.log"), "also ignored\n").unwrap();
        let paths = git_visible(&source).unwrap();
        assert!(paths.contains("outside/hidden.txt"));
        assert!(paths.contains("excluded.log"));
        let copied = root.path().join("copy"); fs::create_dir(&copied).unwrap();
        capture_paths(&source, Some(&copied), paths).unwrap();
        assert_eq!(fs::read(copied.join("outside/hidden.txt")).unwrap(), b"ignored but present\n");
        assert_eq!(fs::read(copied.join("excluded.log")).unwrap(), b"also ignored\n");
    }
    #[test]
    fn copied_source_digest_changes_with_bytes_and_refuses_symlink_escape() {
        let root = tempfile::tempdir().unwrap();
        let source = root.path().join("checkout"); fs::create_dir(&source).unwrap();
        fs::write(source.join("file.rs"), b"first").unwrap();
        let paths = BTreeSet::from(["file.rs".to_owned()]);
        let copy = root.path().join("copy"); fs::create_dir(&copy).unwrap();
        let first = capture_paths(&source, Some(&copy), paths.clone()).unwrap();
        assert_eq!(fs::read(copy.join("file.rs")).unwrap(), b"first");
        fs::write(source.join("file.rs"), b"later").unwrap();
        let later = capture_paths(&source, None, paths.clone()).unwrap();
        assert_ne!(first.sha256, later.sha256, "stale test receipt must not match a changed tree");
        fs::remove_file(source.join("file.rs")).unwrap();
        std::os::unix::fs::symlink(root.path().join("outside-secret"), source.join("file.rs")).unwrap();
        assert!(capture_paths(&source, None, paths).is_err(), "source symlink cannot reach host files");
    }

    #[test]
    fn runner_arguments_mount_only_copied_source_with_offline_limits() {
        let image = format!("fixture@sha256:{}", "a".repeat(64));
        let manifest = Manifest { version:1, session_id:"worker".into(), profile:Profile::DockerOffline,
            policy:Some(Policy{image:image.clone(),docker_host:"unix:///run/user/1000/docker.sock".into(),memory_bytes:1024*1024*1024,cpus:1.0,pids:128,disk_soft_limit_bytes:None,disk_free_floor_bytes:None}),
            policy_hash:String::new(),source:"/source".into(),checkout:"/worker/checkout".into(),context_cwd:None,provider_rollout:None,
            creation_policy_hash:String::new(),checkout_device:0,checkout_inode:0,base_sha:String::new(),branch:String::new(),
            private_home:"/worker/home".into(),cache:"/worker/cache".into(),broker:"/worker/broker".into(),container_id:Some("b".repeat(64)),nonce:String::new(),state:"ready".into()};
        let argv = vec!["/usr/bin/true".into()];
        let command = command_for(&manifest,Path::new("/owner/fleet/source"),&argv,"","test-name").unwrap();
        let args = command.get_args().map(|arg| arg.to_string_lossy().into_owned()).collect::<Vec<_>>();
        assert!(args.windows(2).any(|row| row == ["--network", "none"]));
        assert!(args.contains(&"--read-only".into()));
        assert!(args.contains(&"type=bind,src=/owner/fleet/source,dst=/workspace,readonly".into()));
        assert!(args.contains(&image));
        assert!(!args.iter().any(|arg|arg.contains("/worker/home")||arg.contains("/worker/cache")||arg.contains("/worker/broker")||arg.contains("docker.sock,src")));
        assert!(command.get_envs().all(|(key,_)|key=="PATH"));
    }

    #[test]
    fn excessive_output_and_timeout_never_pass_and_call_cleanup() {
        use std::cell::Cell;
        let cleaned=Cell::new(false);
        let output=execute_bounded(Command::new("/usr/bin/yes"),2,||{cleaned.set(true);Ok(())}).unwrap();
        assert!(cleaned.get() && !output.passed && output.output_bytes>MAX_OUTPUT_BYTES as u64);
        cleaned.set(false);
        let timed=execute_bounded({let mut c=Command::new("sleep");c.arg("2");c},1,||{cleaned.set(true);Ok(())}).unwrap();
        assert!(cleaned.get() && !timed.passed && timed.duration_ms>=1000);
        let unconfirmed=execute_bounded(Command::new("/usr/bin/yes"),2,||Err(error("Docker rm failed"))).unwrap_err();
        assert!(cleanup_unconfirmed(&unconfirmed),"failed container removal must block teardown confirmation");
    }
    #[test]
    fn cancellation_kills_and_reaps_the_command_before_confirming_cleanup() {
        use std::sync::{Arc,atomic::AtomicBool};
        let dir=tempfile::tempdir().unwrap();let pid_file=dir.path().join("pid");
        let cancel=Arc::new(AtomicBool::new(false));let thread_cancel=Arc::clone(&cancel);
        let cleaned=Arc::new(AtomicBool::new(false));let thread_cleaned=Arc::clone(&cleaned);
        let mut command=Command::new("/bin/sh");
        command.arg("-c").arg("printf '%s' $$ > \"$PID_FILE\"; exec sleep 30").env("PID_FILE",&pid_file);
        let runner=std::thread::spawn(move ||execute_bounded_cancel(command,30,||{
            thread_cleaned.store(true,Ordering::Release);Ok(())
        },&thread_cancel));
        let deadline=Instant::now()+Duration::from_secs(3);
        while !pid_file.exists(){assert!(Instant::now()<deadline,"fixture child did not start");std::thread::sleep(Duration::from_millis(5));}
        let pid:i32=fs::read_to_string(&pid_file).unwrap().parse().unwrap();
        cancel.store(true,Ordering::Release);
        let result=runner.join().unwrap();
        assert_eq!(result.unwrap_err().kind(),io::ErrorKind::Interrupted);
        assert!(cleaned.load(Ordering::Acquire));
        assert_eq!(unsafe{libc::kill(pid,0)},-1,"fixture process was not reaped");
    }
}
