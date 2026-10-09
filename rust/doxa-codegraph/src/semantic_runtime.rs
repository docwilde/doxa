//! Bounded LSP session machinery. There is deliberately no production
//! attestation implementation: a planned Docker command is not evidence that
//! its effective cgroup, network, mount, or analyzer settings were enforced.

use super::semantic_evidence::{inspect_definition_reply, DefinitionEvidence};
use super::semantic_producer::{encode_lsp_frame, read_lsp_frame, ProducerPlan};
use super::{file_bytes, CallCandidate, CallEdge};
use serde_json::{json, Value};
use std::io::{Cursor, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command};
use std::time::{Duration, Instant};

const MAX_MESSAGES: usize = 256;
const MAX_STDOUT: usize = 256 * 1024;
const MAX_STDERR: usize = 32 * 1024;
const MAX_FRAME: usize = 32 * 1024;
const MAX_HEADER: usize = 1024;
const SESSION_TIMEOUT: Duration = Duration::from_secs(20);

/// Both checks must be backed by observations of the *effective* runtime,
/// not by the requested Docker arguments. No production implementation exists.
#[allow(dead_code)]
trait RuntimeAttestation {
    fn before_launch(&self, plan: &ProducerPlan) -> Result<(), String>;
    fn after_launch(&self, plan: &ProducerPlan, child: &Child) -> Result<(), String>;
}

fn nonblocking(fd: i32) -> Result<(), String> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err("cannot set nonblocking LSP pipe".into());
    }
    Ok(())
}

struct Session {
    child: Child,
    stdin: ChildStdin,
    stdout: ChildStdout,
    stderr: ChildStderr,
    input: Vec<u8>,
    output_bytes: usize,
    error_bytes: usize,
    messages: usize,
    stderr_open: bool,
    ready: bool,
    deadline: Instant,
    reaped: bool,
}

impl Drop for Session {
    fn drop(&mut self) {
        if !self.reaped {
            // setsid in pre_exec makes the child the process-group leader.
            // Never signal a reaped PID, which might have been reused.
            unsafe { libc::kill(-(self.child.id() as i32), libc::SIGKILL); }
            let _ = self.child.kill();
            let _ = self.child.wait();
            self.reaped = true;
        }
    }
}

impl Session {
    fn start(mut command: Command, deadline: Duration) -> Result<Self, String> {
        // A hard wall-clock deadline also bounds a child that never reads stdin.
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() < 0 { Err(std::io::Error::last_os_error()) } else { Ok(()) }
            });
        }
        let mut child = command.spawn().map_err(|e| format!("LSP child launch failed: {e}"))?;
        let setup = (|| {
            let stdin = child.stdin.take().ok_or("missing LSP stdin")?;
            let stdout = child.stdout.take().ok_or("missing LSP stdout")?;
            let stderr = child.stderr.take().ok_or("missing LSP stderr")?;
            nonblocking(stdin.as_raw_fd())?;
            nonblocking(stdout.as_raw_fd())?;
            nonblocking(stderr.as_raw_fd())?;
            Ok::<_, String>((stdin, stdout, stderr))
        })();
        let (stdin, stdout, stderr) = match setup {
            Ok(pipes) => pipes,
            Err(error) => {
                unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL); }
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
        };
        Ok(Self { child, stdin, stdout, stderr, input: Vec::new(), output_bytes: 0,
            error_bytes: 0, messages: 0, stderr_open: true, ready: false,
            deadline: Instant::now() + deadline, reaped: false })
    }

    fn pump(&mut self, writable: bool) -> Result<bool, String> {
        let remaining = self.deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() { return Err("LSP session deadline exceeded".into()); }
        let mut fds = [
            libc::pollfd { fd: self.stdout.as_raw_fd(), events: libc::POLLIN, revents: 0 },
            libc::pollfd { fd: if self.stderr_open { self.stderr.as_raw_fd() } else { -1 }, events: libc::POLLIN, revents: 0 },
            libc::pollfd { fd: self.stdin.as_raw_fd(), events: if writable { libc::POLLOUT } else { 0 }, revents: 0 },
        ];
        let timeout = remaining.as_millis().min(50) as i32;
        let result = unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, timeout) };
        if result < 0 {
            if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted { return Ok(false); }
            return Err("LSP poll failed".into());
        }
        if fds[0].revents & (libc::POLLIN | libc::POLLHUP) != 0 {
            let mut buf = [0u8; 4096];
            match self.stdout.read(&mut buf) {
                Ok(0) => return Err("LSP server closed stdout".into()),
                Ok(n) => {
                    self.output_bytes = self.output_bytes.checked_add(n).ok_or("LSP stdout count overflow")?;
                    if self.output_bytes > MAX_STDOUT { return Err("LSP stdout exceeds 256 KiB".into()); }
                    self.input.extend_from_slice(&buf[..n]);
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {},
                Err(_) => return Err("LSP stdout read failed".into()),
            }
        }
        if fds[1].revents & (libc::POLLIN | libc::POLLHUP) != 0 {
            let mut buf = [0u8; 4096];
            match self.stderr.read(&mut buf) {
                Ok(0) => self.stderr_open = false,
                Ok(n) => {
                    self.error_bytes = self.error_bytes.checked_add(n).ok_or("LSP stderr count overflow")?;
                    if self.error_bytes > MAX_STDERR { return Err("LSP stderr exceeds 32 KiB".into()); }
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {},
                Err(_) => return Err("LSP stderr read failed".into()),
            }
        }
        if writable && fds[2].revents & (libc::POLLHUP | libc::POLLERR | libc::POLLNVAL) != 0 {
            return Err("LSP stdin closed".into());
        }
        Ok(writable && fds[2].revents & libc::POLLOUT != 0)
    }

    fn send(&mut self, message: &Value) -> Result<(), String> {
        let frame = encode_lsp_frame(message)?;
        let mut offset = 0;
        while offset < frame.len() {
            if self.pump(true)? {
                match self.stdin.write(&frame[offset..]) {
                    Ok(0) => return Err("LSP stdin closed".into()),
                    Ok(n) => offset += n,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {},
                    Err(_) => return Err("LSP stdin write failed".into()),
                }
            }
        }
        Ok(())
    }

    fn next(&mut self) -> Result<Value, String> {
        loop {
            if let Some(split) = self.input.windows(4).position(|part| part == b"\r\n\r\n") {
                let header_end = split + 4;
                if header_end > MAX_HEADER { return Err("invalid or oversized LSP header".into()); }
                let header = std::str::from_utf8(&self.input[..split]).map_err(|_| "invalid LSP header")?;
                let lengths = header.split("\r\n").filter_map(|line| line.strip_prefix("Content-Length: ")).collect::<Vec<_>>();
                if lengths.len() != 1 || lengths[0].starts_with('0') ||
                    !lengths[0].bytes().all(|byte| byte.is_ascii_digit()) {
                    return Err("invalid LSP Content-Length".into());
                }
                let size: usize = lengths[0].parse().map_err(|_| "LSP length overflow")?;
                if size == 0 || size > MAX_FRAME { return Err("LSP body exceeds 32 KiB".into()); }
                if self.input.len() >= header_end + size {
                    let frame = self.input.drain(..header_end + size).collect::<Vec<_>>();
                    let message = read_lsp_frame(&mut Cursor::new(frame))?;
                    self.messages += 1;
                    if self.messages > MAX_MESSAGES { return Err("LSP message count exceeded".into()); }
                    return Ok(message);
                }
            } else if self.input.len() > MAX_HEADER {
                return Err("invalid or oversized LSP header".into());
            }
            self.pump(false)?;
        }
    }

    fn exit_successfully(&mut self) -> Result<(), String> {
        loop {
            // A successful server can leave helpers behind. Observe its exit
            // without reaping the group leader, whose PID must stay reserved
            // until the whole process group has been killed. This also avoids
            // signaling an unrelated group after PID reuse.
            #[cfg(target_os = "linux")]
            {
                let mut info = unsafe { std::mem::zeroed::<libc::siginfo_t>() };
                let result = unsafe { libc::waitid(libc::P_PID, self.child.id(), &mut info,
                    libc::WEXITED | libc::WNOHANG | libc::WNOWAIT) };
                if result < 0 {
                    if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
                        continue;
                    }
                    return Err("LSP wait failed".into());
                }
                if unsafe { info.si_pid() } != 0 {
                    unsafe { libc::kill(-(self.child.id() as i32), libc::SIGKILL); }
                    let status = self.child.wait().map_err(|_| "LSP wait failed")?;
                    self.reaped = true;
                    return if status.success() { Ok(()) } else { Err("LSP server exited unsuccessfully".into()) };
                }
            }
            #[cfg(not(target_os = "linux"))]
            if let Some(status) = self.child.try_wait().map_err(|_| "LSP wait failed")? {
                self.reaped = true;
                return if status.success() { Ok(()) } else { Err("LSP server exited unsuccessfully".into()) };
            }
            if Instant::now() >= self.deadline { return Err("LSP session deadline exceeded".into()); }
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    fn observe_status(&mut self, message: &Value) -> Result<(), String> {
        if message.get("method").and_then(Value::as_str) == Some("experimental/serverStatus") {
            let health = message.pointer("/params/health").and_then(Value::as_str);
            let quiet = message.pointer("/params/quiescent").and_then(Value::as_bool);
            if health != Some("ok") || quiet.is_none() { return Err("rust-analyzer is not healthy".into()); }
            self.ready = quiet == Some(true);
        }
        Ok(())
    }
}

fn expect_reply(session: &mut Session, id: u64) -> Result<Value, String> {
    loop {
        let message = session.next()?;
        if message.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
            return Err("invalid LSP JSON-RPC version".into());
        }
        if message.get("id").is_some() {
            if message.get("id").and_then(Value::as_u64) != Some(id) || message.get("error").is_some() {
                return Err("unexpected or failed LSP response".into());
            }
            return Ok(message);
        }
        // Notifications may arrive while a request is outstanding. Any
        // server-to-client request is unsupported and fails closed.
        if message.get("method").and_then(Value::as_str).is_none() {
            return Err("invalid LSP notification".into());
        }
        session.observe_status(&message)?;
    }
}

fn wait_quiescent(session: &mut Session) -> Result<(), String> {
    loop {
        let message = session.next()?;
        if message.get("jsonrpc").and_then(Value::as_str) != Some("2.0") || message.get("id").is_some() {
            return Err("unexpected response during LSP quiescence".into());
        }
        if message.get("method").and_then(Value::as_str).is_none() {
            return Err("invalid LSP notification".into());
        }
        session.observe_status(&message)?;
        if message.get("method").and_then(Value::as_str) == Some("experimental/serverStatus") && session.ready {
            return Ok(());
        }
    }
}

fn hash_matches(plan: &ProducerPlan, edge: &CallEdge, candidate: &CallCandidate) -> Result<(), String> {
    for (path, expected) in [(&edge.file, &edge.sha256), (&candidate.file, &candidate.sha256)] {
        let (_, observed, _) = file_bytes(&plan.root, path)?;
        if &observed != expected { return Err("semantic source changed since the query".into()); }
    }
    Ok(())
}

/// Drives one definition exchange only behind an attestation implementation.
/// The tests use an in-memory fake gate and a fixture server. Production has
/// no gate; consequently no repo is launched by the CLI on this host.
#[allow(dead_code)]
fn run_definition(
    plan: &ProducerPlan, edge: &CallEdge, candidate: &CallCandidate,
    command: Command, attestation: &impl RuntimeAttestation, timeout: Duration,
) -> Result<DefinitionEvidence, String> {
    attestation.before_launch(plan)?;
    hash_matches(plan, edge, candidate)?;
    if edge.form != "function_path" || !edge.target.is_ascii() { return Err("unsupported call form".into()); }
    let request = json!({"jsonrpc":"2.0","id":7,"method":"textDocument/definition",
        "params":{"textDocument":{"uri":format!("file://{}/{}", plan.root.display(), edge.file)},
            "position":{"line":edge.line.checked_sub(1).ok_or("invalid source line")?,"character":edge.column}}});
    let mut session = Session::start(command, timeout.min(SESSION_TIMEOUT))?;
    attestation.after_launch(plan, &session.child)?;
    session.send(&plan.initialize)?;
    let initialized = expect_reply(&mut session, 1)?;
    if !initialized.pointer("/result/capabilities").is_some_and(Value::is_object) {
        return Err("invalid LSP initialize response".into());
    }
    session.send(&json!({"jsonrpc":"2.0","method":"initialized","params":{}}))?;
    wait_quiescent(&mut session)?;
    hash_matches(plan, edge, candidate)?;
    session.send(&request)?;
    let response = expect_reply(&mut session, 7)?;
    if !session.ready { return Err("rust-analyzer ceased to be quiescent".into()); }
    let evidence = inspect_definition_reply(&plan.root, edge, candidate, &request, &response)?;
    session.send(&json!({"jsonrpc":"2.0","id":99,"method":"shutdown","params":null}))?;
    let shutdown = expect_reply(&mut session, 99)?;
    if !shutdown.get("result").is_some_and(Value::is_null) { return Err("invalid LSP shutdown response".into()); }
    session.send(&json!({"jsonrpc":"2.0","method":"exit","params":null}))?;
    session.exit_successfully()?;
    hash_matches(plan, edge, candidate)?;
    Ok(evidence)
}

/// Public CLI status while an effective rootless Docker attester is absent.
pub fn unavailable_status(plan: &ProducerPlan) -> Value {
    json!({"status":"unknown","binding":"unknown",
        "reason":"effective_rootless_no_egress_quota_attestation_unavailable",
        "attestation":plan.attestation,"docker_launch":"not_attempted"})
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::{query, Query};
    use super::super::semantic_producer::plan_rust_analyzer;
    use std::fs;
    use std::path::Path;
    use std::process::Stdio;
    use tempfile::TempDir;

    const IMAGE: &str = "reviewed/rust-analyzer@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const SOCKET: &str = "unix:///run/user/1000/docker.sock";
    const FAKE: &str = r#"
import json, os, subprocess, sys, time
mode, target_uri, target_path, pid_path = sys.argv[1:]
with open(pid_path, 'w') as f: f.write(str(os.getpid()))
def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line: raise SystemExit(2)
        if line == b'\r\n': break
        key, value = line.decode().strip().split(': ', 1)
        headers[key] = value
    return json.loads(sys.stdin.buffer.read(int(headers['Content-Length'])))
def send(value):
    body = json.dumps(value, separators=(',', ':')).encode()
    sys.stdout.buffer.write(b'Content-Length: %d\r\n\r\n' % len(body) + body)
    sys.stdout.buffer.flush()
if mode == 'oversized':
    for _ in range(12):
        send({'jsonrpc':'2.0','method':'window/logMessage','params':{'message':'X'*30000}})
    time.sleep(30)
if mode == 'stderr':
    sys.stderr.buffer.write(b'X' * 40000)
    sys.stderr.buffer.flush()
    time.sleep(30)
if mode == 'hang': time.sleep(30)
init = read()
assert init['method'] == 'initialize' and init['id'] == 1
assert init['params']['capabilities']['experimental']['serverStatusNotification'] is True
send({'jsonrpc':'2.0','id':1,'result':{'capabilities':{}}})
assert read()['method'] == 'initialized'
send({'jsonrpc':'2.0','method':'experimental/serverStatus',
      'params':{'health':'ok','quiescent':False}})
if mode == 'unhealthy':
    send({'jsonrpc':'2.0','method':'experimental/serverStatus',
          'params':{'health':'warning','quiescent':True}})
    time.sleep(30)
send({'jsonrpc':'2.0','method':'experimental/serverStatus',
      'params':{'health':'ok','quiescent':True}})
request = read()
assert request['method'] == 'textDocument/definition' and request['id'] == 7
if mode == 'busy_definition':
    send({'jsonrpc':'2.0','method':'experimental/serverStatus',
          'params':{'health':'ok','quiescent':False}})
if mode == 'mutate':
    with open(target_path, 'a') as f: f.write('// changed during LSP run\n')
send({'jsonrpc':'2.0','id':7,'result':{'uri':target_uri,
      'range':{'start':{'line':0,'character':3},'end':{'line':0,'character':9}}}})
shutdown = read()
assert shutdown['method'] == 'shutdown' and shutdown['id'] == 99
if mode == 'stuck_shutdown': time.sleep(30)
if mode == 'mutate_after_reply':
    with open(target_path, 'a') as f: f.write('// changed after definition reply\n')
send({'jsonrpc':'2.0','id':99,'result':None})
assert read()['method'] == 'exit'
if mode == 'spawn_descendant':
    descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    with open(pid_path + '.descendant', 'w') as f: f.write(str(descendant.pid))
"#;

    struct FakeGate;
    impl RuntimeAttestation for FakeGate {
        fn before_launch(&self, _: &ProducerPlan) -> Result<(), String> { Ok(()) }
        fn after_launch(&self, _: &ProducerPlan, _: &Child) -> Result<(), String> { Ok(()) }
    }
    struct DenyGate;
    impl RuntimeAttestation for DenyGate {
        fn before_launch(&self, _: &ProducerPlan) -> Result<(), String> { Err("no effective attestation".into()) }
        fn after_launch(&self, _: &ProducerPlan, _: &Child) -> Result<(), String> { unreachable!() }
    }
    struct DenyAfterLaunch;
    impl RuntimeAttestation for DenyAfterLaunch {
        fn before_launch(&self, _: &ProducerPlan) -> Result<(), String> { Ok(()) }
        fn after_launch(&self, _: &ProducerPlan, _: &Child) -> Result<(), String> {
            Err("effective runtime differs from requested profile".into())
        }
    }

    fn fixture(mode: &str) -> (TempDir, ProducerPlan, CallEdge, CallCandidate, Command) {
        let root = tempfile::tempdir().unwrap();
        assert!(Command::new("git").arg("init").arg("-q").arg(root.path()).status().unwrap().success());
        fs::write(root.path().join("a.rs"), "fn caller() { target(); }\n").unwrap();
        fs::write(root.path().join("b.rs"), "fn target() {}\n").unwrap();
        let answer = query(root.path(), Query::Calls("a.rs".into())).unwrap();
        let edge = answer.edges.into_iter().next().unwrap();
        let candidate = edge.candidates[0].clone();
        let script = root.path().join("fake_lsp.py");
        fs::write(&script, FAKE).unwrap();
        let target_uri = format!("file://{}/b.rs", root.path().display());
        let mut command = Command::new("python3");
        command.arg("-u").arg(script).arg(mode).arg(target_uri)
            .arg(root.path().join("b.rs")).arg(root.path().join("child.pid"))
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        let plan = plan_rust_analyzer(root.path(), IMAGE, SOCKET, true).unwrap();
        (root, plan, edge, candidate, command)
    }

    fn assert_reaped(pid_path: &Path) {
        let pid: i32 = fs::read_to_string(pid_path).unwrap().parse().unwrap();
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1, "child {pid} was not reaped");
    }

    #[cfg(target_os = "linux")]
    fn assert_not_running(pid_path: &Path) {
        let pid: i32 = fs::read_to_string(pid_path).unwrap().parse().unwrap();
        for _ in 0..100 {
            let stat = fs::read_to_string(format!("/proc/{pid}/stat"));
            if stat.is_err() || stat.as_ref().is_ok_and(|value| value.split(") ").nth(1)
                .is_some_and(|tail| tail.starts_with('Z'))) {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        panic!("LSP descendant {pid} survived successful server exit");
    }

    #[test]
    fn fake_server_requires_initialize_quiescence_and_definition_then_returns_unknown() {
        let (root, plan, edge, candidate, command) = fixture("ok");
        let evidence = run_definition(&plan, &edge, &candidate, command, &FakeGate, Duration::from_secs(3)).unwrap();
        assert_eq!(evidence.status, "protocol_match_untrusted");
        assert_eq!(evidence.binding, "unknown");
        assert_reaped(&root.path().join("child.pid"));
        let status = unavailable_status(&plan);
        assert_eq!(status["docker_launch"], "not_attempted");
        assert_eq!(status["binding"], "unknown");
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn successful_server_exit_kills_descendants_before_reaping_group_leader() {
        let (root, plan, edge, candidate, command) = fixture("spawn_descendant");
        let evidence = run_definition(&plan, &edge, &candidate, command, &FakeGate,
            Duration::from_secs(3)).unwrap();
        assert_eq!(evidence.binding, "unknown");
        assert_reaped(&root.path().join("child.pid"));
        assert_not_running(&root.path().join("child.pid.descendant"));
    }

    #[test]
    fn missing_attestation_prevents_launch_and_mutated_target_fails_closed() {
        let (root, plan, edge, candidate, command) = fixture("ok");
        assert_eq!(run_definition(&plan, &edge, &candidate, command, &DenyGate, Duration::from_secs(1))
            .unwrap_err(), "no effective attestation");
        assert!(!root.path().join("child.pid").exists());
        let (root, plan, edge, candidate, command) = fixture("ok");
        fs::write(root.path().join("a.rs"), "fn caller() { target(); }\n// changed\n").unwrap();
        assert!(run_definition(&plan, &edge, &candidate, command, &FakeGate, Duration::from_secs(1))
            .unwrap_err().contains("changed"));
        assert!(!root.path().join("child.pid").exists());
        let (root, plan, edge, candidate, command) = fixture("mutate");
        assert!(run_definition(&plan, &edge, &candidate, command, &FakeGate, Duration::from_secs(3))
            .unwrap_err().contains("changed"));
        assert_reaped(&root.path().join("child.pid"));
        let (root, plan, edge, candidate, command) = fixture("mutate_after_reply");
        assert!(run_definition(&plan, &edge, &candidate, command, &FakeGate, Duration::from_secs(3))
            .unwrap_err().contains("changed"));
        assert_reaped(&root.path().join("child.pid"));
    }

    #[test]
    fn timeout_output_caps_and_unhealthy_status_kill_and_reap() {
        for (mode, reason) in [("hang", "deadline"), ("oversized", "stdout exceeds"),
            ("stderr", "stderr exceeds"), ("unhealthy", "not healthy"),
            ("stuck_shutdown", "deadline"), ("busy_definition", "ceased to be quiescent")] {
            let (root, plan, edge, candidate, command) = fixture(mode);
            let error = run_definition(&plan, &edge, &candidate, command, &FakeGate,
                Duration::from_millis(if mode == "hang" { 250 } else { 2000 })).unwrap_err();
            assert!(error.contains(reason), "{mode}: {error}");
            assert_reaped(&root.path().join("child.pid"));
        }
        let (root, plan, edge, candidate, command) = fixture("ok");
        assert_eq!(run_definition(&plan, &edge, &candidate, command, &DenyAfterLaunch,
            Duration::from_secs(1)).unwrap_err(), "effective runtime differs from requested profile");
        // The child can be killed before its first Python instruction, so the
        // PID file is optional. The process itself is always waited on in Drop.
        if root.path().join("child.pid").exists() { assert_reaped(&root.path().join("child.pid")); }
    }
}
