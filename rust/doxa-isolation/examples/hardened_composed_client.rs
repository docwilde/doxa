//! Credential-free installed ROOT Runtime proof; requires a disposable guest.
//! Every containment/source decision uses the actual installed broker APIs.
use doxa_isolation::{broker::HookBroker, root_client, Manifest, Profile, Runtime};
use serde_json::{json, Value};
use std::{
    fs::{self, OpenOptions},
    io::{self, BufRead, BufReader, Read, Write},
    os::{
        fd::AsRawFd,
        unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
    },
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{mpsc, Arc, Mutex},
    time::{Duration, Instant},
};

const THREAD: &str = "composed-thread-20261013";
const ROLLOUT: &str = "/home/doxa/codex/sessions/rollout-composed.jsonl";
const LIMIT: usize = 32 * 1024 * 1024;
const SETTINGS: &[u8] = b"{\"fixture_marker\":\"composed-config-20261013\"}\n";

fn checkpoint(file: &mut fs::File, case: &str) -> io::Result<()> {
    writeln!(file, "{case}")?;
    file.sync_all()
}
fn remaining(deadline: Instant, maximum: u64) -> io::Result<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .map(|duration| duration.min(Duration::from_secs(maximum)))
        .ok_or_else(|| io::Error::other("shared composed proof deadline exceeded"))
}
fn bounded<T>(deadline: Instant, operation: impl FnOnce() -> io::Result<T>) -> io::Result<T> {
    remaining(deadline, 1)?;
    let result = operation()?;
    remaining(deadline, 1)?;
    Ok(result)
}
fn write_bounded(input: &mut ChildStdin, mut bytes: &[u8], deadline: Instant) -> io::Result<()> {
    require(bytes.len() <= 65536, "fixture input exceeded bound")?;
    let flags = unsafe { libc::fcntl(input.as_raw_fd(), libc::F_GETFL) };
    if flags < 0
        || unsafe { libc::fcntl(input.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0
    {
        return Err(io::Error::last_os_error());
    }
    while !bytes.is_empty() {
        remaining(deadline, 1)?;
        match input.write(bytes) {
            Ok(0) => return Err(io::Error::other("fixture input closed")),
            Ok(count) => bytes = &bytes[count..],
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
                ) =>
            {
                let mut poll = libc::pollfd {
                    fd: input.as_raw_fd(),
                    events: libc::POLLOUT,
                    revents: 0,
                };
                let milliseconds = remaining(deadline, 1)?.as_millis().max(1).min(1000) as i32;
                if unsafe { libc::poll(&mut poll, 1, milliseconds) } < 0
                    && io::Error::last_os_error().kind() != io::ErrorKind::Interrupted
                {
                    return Err(io::Error::last_os_error());
                }
            }
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

fn require(ok: bool, reason: &str) -> io::Result<()> {
    if ok {
        Ok(())
    } else {
        Err(io::Error::other(reason))
    }
}
struct Provider {
    child: Child,
    input: Option<ChildStdin>,
    output: mpsc::Receiver<io::Result<Value>>,
    deadline: Instant,
    errors: Arc<Mutex<Vec<u8>>>,
}
impl Provider {
    fn start(manifest: &Manifest, family: &str, deadline: Instant) -> io::Result<Self> {
        remaining(deadline, 1)?;
        let mut original = Command::new(format!("/usr/local/bin/{family}"));
        original.arg("stream").current_dir(&manifest.checkout);
        let mut command = doxa_isolation::isolate_command(original, family)?;
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = command.spawn()?;
        let input = child.stdin.take();
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| io::Error::other("provider stdout missing"))?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| io::Error::other("provider stderr missing"))?;
        let (tx, output) = mpsc::channel();
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut total = 0;
            loop {
                let mut line = Vec::new();
                match reader.by_ref().take(65537).read_until(b'\n', &mut line) {
                    Ok(0) => break,
                    Ok(_) if line.len() <= 65536 => {
                        total += line.len();
                        if total > 256 * 1024 {
                            let _ = tx.send(Err(io::Error::other("fixture output exceeded bound")));
                            break;
                        }
                        let result = serde_json::from_slice(&line).map_err(io::Error::other);
                        if tx.send(result).is_err() {
                            break;
                        }
                    }
                    _ => {
                        let _ = tx.send(Err(io::Error::other(
                            "fixture output malformed or overlong",
                        )));
                        break;
                    }
                }
            }
        });
        let errors = Arc::new(Mutex::new(Vec::new()));
        let store = errors.clone();
        std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let _ = stderr.take(65537).read_to_end(&mut bytes);
            *store.lock().unwrap() = bytes;
        });
        Ok(Self {
            child,
            input,
            output,
            deadline,
            errors,
        })
    }
    fn receive(&self, expected: &str) -> io::Result<Value> {
        let value = self
            .output
            .recv_timeout(remaining(self.deadline, 45)?)
            .map_err(|_| {
                let bytes = self.errors.lock().unwrap();
                io::Error::other(format!(
                    "actual fixture provider response timed out or closed: {}",
                    String::from_utf8_lossy(&bytes[..bytes.len().min(4096)])
                ))
            })??;
        require(
            value["op"] == expected,
            "actual provider returned wrong operation",
        )?;
        Ok(value)
    }
    fn request(&mut self, value: Value) -> io::Result<Value> {
        let op = value["op"]
            .as_str()
            .ok_or_else(|| io::Error::other("fixture operation missing"))?
            .to_owned();
        let input = self
            .input
            .as_mut()
            .ok_or_else(|| io::Error::other("provider input closed"))?;
        let mut bytes = serde_json::to_vec(&value)?;
        bytes.push(b'\n');
        write_bounded(input, &bytes, self.deadline)?;
        self.receive(&op)
    }
    fn finish(&mut self) -> io::Result<()> {
        self.request(json!({"op":"exit"}))?;
        self.input.take();
        let deadline = self.deadline.min(Instant::now() + Duration::from_secs(10));
        loop {
            if let Some(status) = self.child.try_wait()? {
                return require(status.success(), "actual provider exited unsuccessfully");
            }
            require(Instant::now() < deadline, "provider shutdown timed out")?;
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}
impl Drop for Provider {
    fn drop(&mut self) {
        self.input.take();
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

fn git(
    manifest: &Manifest,
    arguments: &[&str],
    input: Option<&[u8]>,
    deadline: Instant,
) -> io::Result<Value> {
    remaining(deadline, 1)?;
    let mut original = Command::new("git");
    original.current_dir(&manifest.checkout).args(arguments);
    let mut command = doxa_isolation::workspace::command(original)?;
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn()?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| io::Error::other("Git stdout absent"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| io::Error::other("Git stderr absent"))?;
    let out = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stdout
            .take(256 * 1024 + 1)
            .read_to_end(&mut bytes)
            .map(|_| bytes)
    });
    let err = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        stderr.take(65537).read_to_end(&mut bytes).map(|_| bytes)
    });
    let deadline = deadline.min(Instant::now() + Duration::from_secs(45));
    if let Some(bytes) = input {
        let written = write_bounded(
            &mut child
                .stdin
                .take()
                .ok_or_else(|| io::Error::other("Git stdin absent"))?,
            bytes,
            deadline,
        );
        if let Err(error) = written {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
    } else {
        child.stdin.take();
    }
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(io::Error::other("bounded ROOT Git wait expired"));
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    let stdout = out
        .join()
        .map_err(|_| io::Error::other("Git output reader failed"))??;
    let stderr = err
        .join()
        .map_err(|_| io::Error::other("Git error reader failed"))??;
    require(
        stdout.len() <= 256 * 1024 && stderr.len() <= 65536,
        "Git fixture output exceeded bound",
    )?;
    require(
        status.success(),
        &format!(
            "actual ROOT workspace Git refused: {}",
            String::from_utf8_lossy(&stderr[..stderr.len().min(4096)])
        ),
    )?;
    Ok(
        json!({"argv":arguments,"stdout":String::from_utf8_lossy(&stdout),"stderr_bytes":stderr.len(),"success":true}),
    )
}

fn proof(home: &Path, source: &Path, sid: &str) -> io::Result<Value> {
    require(
        unsafe { libc::geteuid() } == 1000,
        "composed Runtime proof requires actual callerUID1000",
    )?;
    for key in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "DOXA_ISOLATION_MANIFEST",
        "DOXA_SESSION_MANIFEST",
        "TMPDIR",
    ] {
        std::env::remove_var(key);
    }
    std::env::set_var("DOXA_HOME", home);
    let started = Instant::now();
    let deadline = started + Duration::from_secs(300);
    let mut progress = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(home.join("composed-progress.txt"))?;
    let settings = home.join("isolation").join(sid).join("home/claude");
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&settings)?;
    let mut config = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(settings.join("settings.json"))?;
    config.write_all(SETTINGS)?;
    config.sync_all()?;
    drop(config);
    checkpoint(
        &mut progress,
        "actual Runtime prepare with harmless settings",
    )?;
    remaining(deadline, 1)?;
    let mut runtime = Runtime::prepare(
        home,
        sid,
        source,
        Some(Profile::DockerHardened),
        false,
        None,
    )?;
    if let Err(clock) = remaining(deadline, 1) {
        let cleanup = runtime.stop();
        return Err(io::Error::other(format!(
            "{clock}; initial Runtime cleanup: {cleanup:?}"
        )));
    }
    let manifest = runtime.manifest().clone();
    let cid = manifest
        .container_id
        .clone()
        .ok_or_else(|| io::Error::other("ROOT Runtime CID absent"))?;
    bounded(deadline, || {
        runtime.prepare_providers(&["claude".into(), "codex".into()])
    })?;
    runtime.activate();
    checkpoint(&mut progress, "actual Runtime ready and active")?;
    let reviewed = Arc::new(Mutex::new(Vec::<Value>::new()));
    let review = reviewed.clone();
    let hook=bounded(deadline,||HookBroker::start(move|event,manifest| {
        let checked=(||->io::Result<Value>{
            require(event["session_id"]==THREAD&&event["transcript_path"]==ROLLOUT,"callback actual event scope differs")?;
            let snapshot=bounded(deadline,||doxa_isolation::snapshot_provider_file(Path::new(ROLLOUT),0,LIMIT,Some(THREAD)))?
                .ok_or_else(||io::Error::other("actual ROOT callback snapshot unavailable"))?;
            let copy=bounded(deadline,||doxa_isolation::provider_review_file(&snapshot))?;
            let meta=fs::symlink_metadata(&copy.path)?;
            require(meta.uid()==1000&&meta.mode()&0o7777==0o600&&meta.nlink()==1,"callback review copy is not private")?;
            require(fs::read(&copy.path)?==snapshot.bytes,"review copy differs from actual ROOT source")?;
            require(bounded(deadline,||doxa_isolation::verify_provider_snapshot(&snapshot))?,"callback original source changed")?;
            Ok(json!({"session_id":manifest.session_id,"source_proof":snapshot.proof,"review_private":true,"fixture_approval_only":true}))
        })();
        match checked {
            Ok(value)=>{review.lock().unwrap().push(value);json!({"continue":true,"fixtureApproved":true})},
            Err(_)=>json!({"continue":false,"stopReason":"credential-free fixture source review refused"}),
        }
    }))?.ok_or_else(||io::Error::other("actual ROOT HookBroker unavailable"))?;
    checkpoint(&mut progress, "actual ROOT callback registered")?;
    let result = (|| -> io::Result<Value> {
        let mut codex = Provider::start(&manifest, "codex", deadline)?;
        let ready = codex.receive("ready")?;
        require(
            ready["family"] == "codex" && ready["owner_handshake"] == true,
            "actual protected Codex owner handshake absent",
        )?;
        bounded(deadline, || doxa_isolation::check_turn_budget(&manifest))?;
        checkpoint(
            &mut progress,
            "Codex protected owner handshake and concurrent guard",
        )?;
        require(
            codex.child.try_wait()?.is_none(),
            "guard ended persistent provider",
        )?;
        let snapshot = bounded(deadline, || {
            doxa_isolation::snapshot_provider_file(Path::new(ROLLOUT), 0, LIMIT, None)
        })?
        .ok_or_else(|| io::Error::other("ROOT snapshot missing"))?;
        require(
            bounded(deadline, || {
                doxa_isolation::verify_provider_snapshot(&snapshot)
            })?,
            "initial actual source recheck refused",
        )?;
        let copy = bounded(deadline, || doxa_isolation::provider_review_file(&snapshot))?;
        let copied = fs::symlink_metadata(&copy.path)?;
        let directory = fs::symlink_metadata(copy.path.parent().unwrap())?;
        require(
            copied.uid() == 1000
                && copied.mode() & 0o7777 == 0o600
                && directory.mode() & 0o7777 == 0o700,
            "review copy permissions differ",
        )?;
        require(
            fs::read(&copy.path)? == snapshot.bytes,
            "private review copy bytes differ",
        )?;
        checkpoint(
            &mut progress,
            "actual source snapshot recheck and private review copy",
        )?;
        let hook_result = codex.request(json!({"op":"hook","command":hook.command()}))?;
        require(
            hook_result["result"]["continue"] == true
                && hook_result["result"]["fixtureApproved"] == true,
            "actual ROOT callback did not approve fixture",
        )?;
        require(
            reviewed.lock().unwrap().len() == 1,
            "actual ROOT callback handler count differs",
        )?;
        checkpoint(&mut progress, "actual callback fixture-only source review")?;
        codex.request(json!({"op":"mutate"}))?;
        remaining(deadline, 1)?;
        let mutation_refused = matches!(
            doxa_isolation::verify_provider_snapshot(&snapshot),
            Ok(false) | Err(_)
        );
        remaining(deadline, 1)?;
        require(
            mutation_refused,
            "mutated actual source retained stale proof",
        )?;
        checkpoint(&mut progress, "actual source mutation refused stale proof")?;
        codex.request(json!({"op":"poison-git"}))?;
        // The broker retires each immutable view after its Git operation.
        // Observe the live readonly view from the concurrent provider while
        // ROOT checks it before starting the actual Git process.
        codex.request(json!({"op":"watch-git-view"}))?;
        let status = git(
            &manifest,
            &["-c", "core.quotepath=false", "status", "--porcelain"],
            None,
            deadline,
        )?;
        let diff = git(
            &manifest,
            &[
                "--no-pager",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--find-renames",
                "--unified=3",
                "HEAD",
                "--",
            ],
            None,
            deadline,
        )?;
        let before = codex.request(json!({"op":"markers"}))?;
        require(
            before["filter_marker"] == false && before["diff_marker"] == false,
            "ROOT Git executed untrusted filter/diff command",
        )?;
        let readonly = codex.request(json!({"op":"write-git-view"}))?;
        require(
            readonly["errno"]
                .as_i64()
                .is_some_and(|e| e == libc::EROFS as i64 || e == libc::EACCES as i64),
            "worker could write ROOT Git view",
        )?;
        checkpoint(
            &mut progress,
            "readonly Root Git and malicious filter purity",
        )?;
        let patch=b"diff --git a/plain.txt b/plain.txt\n--- a/plain.txt\n+++ b/plain.txt\n@@ -1 +1 @@\n-before\n+after\n";
        let apply = git(
            &manifest,
            &["apply", "--whitespace=nowarn", "-"],
            Some(patch),
            deadline,
        )?;
        let after = codex.request(json!({"op":"markers"}))?;
        require(
            after["plain"] == "after\n"
                && after["filter_marker"] == false
                && after["diff_marker"] == false,
            "ordinary ROOT apply or filter purity failed",
        )?;
        bounded(deadline, || doxa_isolation::check_turn_budget(&manifest))?;
        checkpoint(&mut progress, "ordinary Root Git apply and guard")?;
        codex.finish()?;
        drop(codex);
        drop(copy);
        let mut claude = Provider::start(&manifest, "claude", deadline)?;
        let claude_ready = claude.receive("ready")?;
        require(
            claude_ready["family"] == "claude",
            "actual Claude fixture failed",
        )?;
        use sha2::{Digest, Sha256};
        require(
            claude_ready["settings"]["sha256"] == format!("{:x}", Sha256::digest(SETTINGS))
                && claude_ready["settings"]["bytes"] == SETTINGS.len()
                && claude_ready["settings"]["uid"] == 1001
                && claude_ready["settings"]["mode"] == 0o600
                && claude_ready["settings"]["marker"] == "composed-config-20261013",
            "actual worker did not receive exact harmless private settings",
        )?;
        bounded(deadline, || doxa_isolation::check_turn_budget(&manifest))?;
        claude.finish()?;
        checkpoint(
            &mut progress,
            "Claude actual worker settings digest and guard",
        )?;
        Ok(
            json!({"scope":"actual-root-runtime-client-credential-free","session_id":sid,"cid":cid,
            "policy_sha256":manifest.policy_hash,"runtime_prepared":true,"protected_codex_ready":ready,
            "guard_during_persistent_stdio":true,"snapshot":snapshot.proof,"private_review_copy":true,
            "actual_hook":hook_result,"callback_reviews":reviewed.lock().unwrap().clone(),"source_mutation_refused":mutation_refused,
            "git_status":status,"git_diff":diff,"git_readonly":readonly,"git_apply":apply,"git_markers":after,
            "claude_ready":claude_ready,"live_provider_verified":false,"paid_calls":0}),
        )
    })();
    drop(hook);
    let stopped = runtime.stop();
    let mut report = match (result, stopped) {
        (Ok(report), Ok(())) => report,
        (Err(operation), Err(cleanup)) => {
            return Err(io::Error::other(format!(
                "operation: {operation}; Runtime cleanup: {cleanup}"
            )))
        }
        (Err(operation), Ok(())) => return Err(operation),
        (Ok(_), Err(cleanup)) => {
            return Err(io::Error::other(format!("Runtime cleanup: {cleanup}")))
        }
    };
    remaining(deadline, 1)?;
    checkpoint(&mut progress, "Runtime stopped first CID")?;
    require(
        bounded(deadline, || root_client::control(sid, "describe"))?["status"] == "stopped",
        "ROOT stop was not current",
    )?;
    remaining(deadline, 1)?;
    let mut resumed =
        Runtime::prepare(home, sid, source, Some(Profile::DockerHardened), true, None)?;
    let resumed_cid = resumed.manifest().container_id.clone();
    let resumed_body = (|| -> io::Result<()> {
        remaining(deadline, 1)?;
        require(
            resumed_cid.is_some() && resumed_cid.as_deref() != Some(&cid),
            "ROOT resume reused old container lease",
        )?;
        resumed.activate();
        bounded(deadline, || {
            doxa_isolation::check_turn_budget(resumed.manifest())
        })?;
        Ok(())
    })();
    let resumed_stop = resumed.stop();
    match (resumed_body, resumed_stop) {
        (Ok(()), Ok(())) => {}
        (Err(operation), Err(cleanup)) => {
            return Err(io::Error::other(format!(
                "coldresume: {operation}; cleanup: {cleanup}"
            )))
        }
        (Err(error), Ok(())) | (Ok(()), Err(error)) => return Err(error),
    }
    remaining(deadline, 1)?;
    checkpoint(&mut progress, "cold Runtime resume fresh CID guard stop")?;
    report["stopped"] = true.into();
    report["resumed_cid"] = json!(resumed_cid);
    report["resumed_and_stopped"] = true.into();
    report["resume_scope"] = "cold-runtime-new-lease-guard-stop".into();
    let elapsed = started.elapsed();
    require(
        elapsed <= Duration::from_secs(300),
        "composed body passed its shared deadline",
    )?;
    report["body_elapsed_ms"] = json!(elapsed.as_millis());
    // Output-reader EOF is supervised by the outer retained-pidfd360s host
    // deadline; the local45s Git bound covers command exit and bounded stdin.
    report["local_pipe_deadline_scope"] =
        "host-pidfd-supervisor360s; Git-command-exit45s; body-pass-oracle300s".into();
    Ok(report)
}

fn main() -> io::Result<()> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    require(
        args.len() == 4,
        "usage: hardened_composed_client HOME SOURCE SESSION_ID OUTPUT",
    )?;
    let home = PathBuf::from(&args[0]);
    let source = PathBuf::from(&args[1]);
    let sid = args[2]
        .to_str()
        .ok_or_else(|| io::Error::other("session ID not Unicode"))?;
    let output = PathBuf::from(&args[3]);
    let report = proof(&home, &source, sid)?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(output)?;
    serde_json::to_writer_pretty(&mut file, &report)?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    println!(
        "{}",
        json!({"scope":report["scope"],"session_id":sid,"status":"passed","paid_calls":0})
    );
    Ok(())
}
