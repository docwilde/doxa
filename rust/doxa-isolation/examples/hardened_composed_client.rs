//! Credential-free installed ROOT Runtime proof; requires a disposable guest.
//! Every containment/source decision uses the actual installed broker APIs.
use doxa_isolation::{broker::HookBroker, root_client, Manifest, Profile, Runtime};
use serde_json::{json, Value};
use std::{
    fs::{self, OpenOptions},
    io::{self, BufRead, BufReader, Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{mpsc, Arc, Mutex},
    time::{Duration, Instant},
};

const THREAD: &str = "composed-thread-20261013";
const ROLLOUT: &str = "/home/doxa/codex/sessions/rollout-composed.jsonl";
const LIMIT: usize = 32 * 1024 * 1024;

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
}
impl Provider {
    fn start(manifest: &Manifest, family: &str) -> io::Result<Self> {
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
        std::thread::spawn(move || {
            let _ = io::copy(&mut stderr.take(65537), &mut io::sink());
        });
        Ok(Self {
            child,
            input,
            output,
        })
    }
    fn receive(&self, expected: &str) -> io::Result<Value> {
        let value = self
            .output
            .recv_timeout(Duration::from_secs(45))
            .map_err(|_| io::Error::other("actual provider response timed out or closed"))??;
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
        serde_json::to_writer(&mut *input, &value)?;
        input.write_all(b"\n")?;
        input.flush()?;
        self.receive(&op)
    }
    fn finish(&mut self) -> io::Result<()> {
        self.request(json!({"op":"exit"}))?;
        self.input.take();
        let deadline = Instant::now() + Duration::from_secs(10);
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

fn git(manifest: &Manifest, arguments: &[&str], input: Option<&[u8]>) -> io::Result<Value> {
    let mut original = Command::new("git");
    original.current_dir(&manifest.checkout).args(arguments);
    let mut command = doxa_isolation::workspace::command(original)?;
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn()?;
    if let Some(bytes) = input {
        child
            .stdin
            .take()
            .ok_or_else(|| io::Error::other("Git stdin absent"))?
            .write_all(bytes)?;
    } else {
        child.stdin.take();
    }
    let output = child.wait_with_output()?;
    require(
        output.stdout.len() <= 256 * 1024 && output.stderr.len() <= 65536,
        "Git fixture output exceeded bound",
    )?;
    require(output.status.success(), "actual ROOT workspace Git refused")?;
    Ok(
        json!({"argv":arguments,"stdout":String::from_utf8_lossy(&output.stdout),"stderr_bytes":output.stderr.len(),"success":true}),
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
    let mut runtime = Runtime::prepare(
        home,
        sid,
        source,
        Some(Profile::DockerHardened),
        false,
        None,
    )?;
    let manifest = runtime.manifest().clone();
    let cid = manifest
        .container_id
        .clone()
        .ok_or_else(|| io::Error::other("ROOT Runtime CID absent"))?;
    runtime.prepare_providers(&["claude".into(), "codex".into()])?;
    runtime.activate();
    let reviewed = Arc::new(Mutex::new(Vec::<Value>::new()));
    let review = reviewed.clone();
    let hook=HookBroker::start(move|event,manifest| {
        let checked=(||->io::Result<Value>{
            require(event["session_id"]==THREAD&&event["transcript_path"]==ROLLOUT,"callback actual event scope differs")?;
            let snapshot=doxa_isolation::snapshot_provider_file(Path::new(ROLLOUT),0,LIMIT,Some(THREAD))?
                .ok_or_else(||io::Error::other("actual ROOT callback snapshot unavailable"))?;
            let copy=doxa_isolation::provider_review_file(&snapshot)?;
            let meta=fs::symlink_metadata(&copy.path)?;
            require(meta.uid()==1000&&meta.mode()&0o7777==0o600&&meta.nlink()==1,"callback review copy is not private")?;
            require(fs::read(&copy.path)?==snapshot.bytes,"review copy differs from actual ROOT source")?;
            require(doxa_isolation::verify_provider_snapshot(&snapshot)?,"callback original source changed")?;
            Ok(json!({"session_id":manifest.session_id,"source_proof":snapshot.proof,"review_private":true,"fixture_approval_only":true}))
        })();
        match checked {
            Ok(value)=>{review.lock().unwrap().push(value);json!({"continue":true,"fixtureApproved":true})},
            Err(_)=>json!({"continue":false,"stopReason":"credential-free fixture source review refused"}),
        }
    })?.ok_or_else(||io::Error::other("actual ROOT HookBroker unavailable"))?;
    let result = (|| -> io::Result<Value> {
        let mut codex = Provider::start(&manifest, "codex")?;
        let ready = codex.receive("ready")?;
        require(
            ready["family"] == "codex" && ready["owner_handshake"] == true,
            "actual protected Codex owner handshake absent",
        )?;
        doxa_isolation::check_turn_budget(&manifest)?;
        require(
            codex.child.try_wait()?.is_none(),
            "guard ended persistent provider",
        )?;
        let snapshot = doxa_isolation::snapshot_provider_file(Path::new(ROLLOUT), 0, LIMIT, None)?
            .ok_or_else(|| io::Error::other("ROOT snapshot missing"))?;
        require(
            doxa_isolation::verify_provider_snapshot(&snapshot)?,
            "initial actual source recheck refused",
        )?;
        let copy = doxa_isolation::provider_review_file(&snapshot)?;
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
        codex.request(json!({"op":"mutate"}))?;
        let mutation_refused = matches!(
            doxa_isolation::verify_provider_snapshot(&snapshot),
            Ok(false) | Err(_)
        );
        require(
            mutation_refused,
            "mutated actual source retained stale proof",
        )?;
        codex.request(json!({"op":"poison-git"}))?;
        // The broker retires each immutable view after its Git operation.
        // Observe the live readonly view from the concurrent provider while
        // ROOT checks it before starting the actual Git process.
        codex.request(json!({"op":"watch-git-view"}))?;
        let status = git(
            &manifest,
            &["-c", "core.quotepath=false", "status", "--porcelain"],
            None,
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
        let patch=b"diff --git a/plain.txt b/plain.txt\n--- a/plain.txt\n+++ b/plain.txt\n@@ -1 +1 @@\n-before\n+after\n";
        let apply = git(
            &manifest,
            &["apply", "--whitespace=nowarn", "-"],
            Some(patch),
        )?;
        let after = codex.request(json!({"op":"markers"}))?;
        require(
            after["plain"] == "after\n"
                && after["filter_marker"] == false
                && after["diff_marker"] == false,
            "ordinary ROOT apply or filter purity failed",
        )?;
        doxa_isolation::check_turn_budget(&manifest)?;
        codex.finish()?;
        drop(codex);
        drop(copy);
        let mut claude = Provider::start(&manifest, "claude")?;
        let claude_ready = claude.receive("ready")?;
        require(
            claude_ready["family"] == "claude",
            "actual Claude fixture failed",
        )?;
        doxa_isolation::check_turn_budget(&manifest)?;
        claude.finish()?;
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
    let mut report = result?;
    stopped?;
    require(
        root_client::control(sid, "describe")?["status"] == "stopped",
        "ROOT stop was not current",
    )?;
    let mut resumed =
        Runtime::prepare(home, sid, source, Some(Profile::DockerHardened), true, None)?;
    let resumed_cid = resumed.manifest().container_id.clone();
    require(
        resumed_cid.is_some() && resumed_cid.as_deref() != Some(&cid),
        "ROOT resume reused old container lease",
    )?;
    resumed.activate();
    doxa_isolation::check_turn_budget(resumed.manifest())?;
    resumed.stop()?;
    report["stopped"] = true.into();
    report["resumed_cid"] = json!(resumed_cid);
    report["resumed_and_stopped"] = true.into();
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
