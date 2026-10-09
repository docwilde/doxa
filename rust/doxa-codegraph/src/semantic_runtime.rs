//! Bounded LSP session machinery and a disabled Docker observation seam.
//! Inspect and cgroup observations are necessary evidence, but do not yet
//! prove offline networking, disk quota, or the analyzer binary/configuration.

use super::semantic_evidence::{inspect_definition_reply, DefinitionEvidence};
use super::semantic_producer::{encode_lsp_frame, read_lsp_frame, ProducerPlan};
use super::{file_bytes, CallCandidate, CallEdge};
use serde_json::{json, Value};
use std::cell::{Cell, RefCell};
use std::fs;
use std::io::{Cursor, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command};
use std::time::{Duration, Instant};

const MAX_MESSAGES: usize = 256;
const MAX_STDOUT: usize = 256 * 1024;
const MAX_STDERR: usize = 32 * 1024;
const MAX_FRAME: usize = 32 * 1024;
const MAX_HEADER: usize = 1024;
const SESSION_TIMEOUT: Duration = Duration::from_secs(20);
const PROBE_TIMEOUT: Duration = Duration::from_secs(2);
const MAX_PROBE_OUTPUT: usize = 64 * 1024;
const MEMORY_BYTES: u64 = 1024 * 1024 * 1024;
const MAX_PIDS: u64 = 64;

/// Both checks must be backed by observations of the *effective* runtime,
/// not by the requested Docker arguments. No production caller exists.
#[allow(dead_code)]
trait RuntimeAttestation {
    fn before_launch(&self, plan: &ProducerPlan) -> Result<(), String>;
    fn launching(&self) {}
    fn after_launch(&self, plan: &ProducerPlan, child: &Child) -> Result<(), String>;
}

/// An internal observation gate for a future reviewed launcher. The binary
/// path is explicit so fixtures cannot reach the host's `docker` by PATH.
/// The CLI does not construct this gate or call `run_observed_definition`.
#[allow(dead_code)]
struct DockerObservationGate {
    binary: PathBuf,
    cidfile: PathBuf,
    proc_root: PathBuf,
    cgroup_root: PathBuf,
    image_id: RefCell<Option<String>>,
    cid: RefCell<Option<String>>,
    name: String,
    attempted: Cell<bool>,
}

fn random_container_name() -> Result<String, String> {
    let mut nonce = [0u8; 16];
    fs::File::open("/dev/urandom").map_err(|_| "cannot open random source")?
        .read_exact(&mut nonce).map_err(|_| "cannot read random source")?;
    let mut name = String::from("doxa-semantic-");
    for byte in nonce { name.push_str(&format!("{byte:02x}")); }
    Ok(name)
}

fn sha256_id(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|hex| hex.len() == 64
        && hex.bytes().all(|byte| byte.is_ascii_hexdigit()))
}

fn json_probe(binary: &Path, docker_host: &str, args: &[&str]) -> Result<Value, String> {
    let output = text_probe(binary, docker_host, args)?;
    let value: Value = serde_json::from_str(&output).map_err(|_| "invalid Docker probe JSON")?;
    if !value.is_object() { return Err("Docker probe result must be an object".into()); }
    Ok(value)
}

fn text_probe(binary: &Path, docker_host: &str, args: &[&str]) -> Result<String, String> {
    if !binary.is_absolute() { return Err("Docker probe binary must be absolute".into()); }
    let mut command = Command::new(binary);
    command.env_clear().env("DOCKER_HOST", docker_host).args(args)
        .stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let mut session = Session::start(command, PROBE_TIMEOUT)?;
    let output = session.capture_probe_output()?;
    String::from_utf8(output).map_err(|_| "Docker probe output is not UTF-8".into())
}

fn bounded_file(path: &Path, cap: u64) -> Result<String, String> {
    let metadata = fs::symlink_metadata(path).map_err(|_| format!("missing runtime observation: {}", path.display()))?;
    if !metadata.file_type().is_file() || metadata.len() > cap {
        return Err("runtime observation is not a bounded regular file".into());
    }
    let mut bytes = Vec::new();
    fs::File::open(path).map_err(|_| "cannot open runtime observation")?
        .take(cap + 1).read_to_end(&mut bytes).map_err(|_| "cannot read runtime observation")?;
    if bytes.len() as u64 > cap { return Err("runtime observation exceeds bound".into()); }
    String::from_utf8(bytes).map_err(|_| "runtime observation is not UTF-8".into())
}

fn cgroup_path(proc_root: &Path, pid: u64, cgroup_root: &Path) -> Result<PathBuf, String> {
    if pid == 0 { return Err("invalid inspected container PID".into()); }
    let membership = bounded_file(&proc_root.join(pid.to_string()).join("cgroup"), 4096)?;
    let mut rows = membership.lines();
    let row = rows.next().ok_or("empty cgroup membership")?;
    if rows.next().is_some() || !row.starts_with("0::/") {
        return Err("container is not in a single cgroup v2 hierarchy".into());
    }
    let relative = row[4..].trim_start_matches('/');
    if relative.is_empty() { return Err("container shares the cgroup root".into()); }
    let path = Path::new(relative);
    if !path.components().all(|part| matches!(part, std::path::Component::Normal(_))) {
        return Err("unsafe cgroup membership path".into());
    }
    let mut observed = cgroup_root.to_path_buf();
    for part in path.components() {
        observed.push(part);
        if fs::symlink_metadata(&observed).map_err(|_| "missing container cgroup")?.file_type().is_symlink() {
            return Err("symlinked container cgroup".into());
        }
    }
    Ok(observed)
}

fn finite_u64(path: &Path, max: u64) -> Result<(), String> {
    let observed = bounded_file(path, 64)?;
    let value: u64 = observed.trim().parse().map_err(|_| "unbounded or invalid cgroup limit")?;
    if value > max { return Err("effective cgroup limit exceeds profile".into()); }
    Ok(())
}

fn verify_cgroup(proc_root: &Path, cgroup_root: &Path, pid: u64) -> Result<(), String> {
    let path = cgroup_path(proc_root, pid, cgroup_root)?;
    finite_u64(&path.join("memory.max"), MEMORY_BYTES)?;
    finite_u64(&path.join("memory.swap.max"), 0)?;
    finite_u64(&path.join("pids.max"), MAX_PIDS)?;
    let cpu = bounded_file(&path.join("cpu.max"), 64)?;
    let mut values = cpu.split_whitespace();
    let quota: u64 = values.next().ok_or("missing CPU quota")?.parse()
        .map_err(|_| "unbounded or invalid CPU quota")?;
    let period: u64 = values.next().ok_or("missing CPU period")?.parse()
        .map_err(|_| "invalid CPU period")?;
    if values.next().is_some() || period == 0 || quota > period {
        return Err("effective CPU quota exceeds profile".into());
    }
    Ok(())
}

fn exact_tmpfs(value: &Value) -> bool {
    let Some(options) = value.pointer("/HostConfig/Tmpfs/~1tmp").and_then(Value::as_str) else { return false; };
    let tokens = options.split(',').collect::<std::collections::BTreeSet<_>>();
    options.split(',').count() == 6
        && tokens == ["rw", "noexec", "nosuid", "nodev", "size=67108864", "mode=1777"].into_iter().collect()
        && value.pointer("/HostConfig/Tmpfs").and_then(Value::as_object).is_some_and(|map| map.len() == 1)
}

fn verify_container(value: &Value, plan: &ProducerPlan, cid: &str, image_id: &str, name: &str) -> Result<u64, String> {
    let required_env = ["HOME=/tmp", "TMPDIR=/tmp", "CARGO_HOME=/tmp/cargo",
        "RUSTUP_HOME=/tmp/rustup", "CARGO_NET_OFFLINE=true"];
    let matches = value.pointer("/Id").and_then(Value::as_str) == Some(cid)
        && value.pointer("/Name").and_then(Value::as_str) == Some(format!("/{name}").as_str())
        && value.pointer("/Image").and_then(Value::as_str) == Some(image_id)
        && value.pointer("/Config/Image").and_then(Value::as_str) == Some(plan.image.as_str())
        && value.pointer("/Config/User").and_then(Value::as_str) == Some("0:0")
        && value.pointer("/Config/WorkingDir").and_then(Value::as_str) == plan.root.to_str()
        && value.pointer("/Config/Entrypoint").and_then(Value::as_array).is_some_and(|items|
            items.len() == 1 && items[0].as_str() == Some("/usr/local/bin/rust-analyzer"))
        && value.pointer("/Config/Cmd").is_some_and(|cmd| cmd.is_null()
            || cmd.as_array().is_some_and(|items| items.is_empty()))
        && value.pointer("/Config/Env").and_then(Value::as_array).is_some_and(|items|
            required_env.iter().all(|required| items.iter().any(|item| item.as_str() == Some(*required))))
        && value.pointer("/State/Running").and_then(Value::as_bool) == Some(true)
        && value.pointer("/HostConfig/NetworkMode").and_then(Value::as_str) == Some("none")
        && value.pointer("/NetworkSettings/Networks").and_then(Value::as_object).is_some_and(|map| map.is_empty())
        && value.pointer("/HostConfig/ReadonlyRootfs").and_then(Value::as_bool) == Some(true)
        && value.pointer("/HostConfig/Privileged").and_then(Value::as_bool) == Some(false)
        && value.pointer("/HostConfig/Init").and_then(Value::as_bool) == Some(true)
        && value.pointer("/HostConfig/AutoRemove").and_then(Value::as_bool) == Some(true)
        && value.pointer("/HostConfig/Memory").and_then(Value::as_u64) == Some(MEMORY_BYTES)
        && value.pointer("/HostConfig/MemorySwap").and_then(Value::as_u64) == Some(MEMORY_BYTES)
        && value.pointer("/HostConfig/NanoCpus").and_then(Value::as_u64) == Some(1_000_000_000)
        && value.pointer("/HostConfig/PidsLimit").and_then(Value::as_u64) == Some(MAX_PIDS)
        && value.pointer("/HostConfig/IpcMode").and_then(Value::as_str) == Some("private")
        && value.pointer("/HostConfig/CgroupnsMode").and_then(Value::as_str) == Some("private")
        && value.pointer("/HostConfig/CapDrop").and_then(Value::as_array).is_some_and(|items|
            items.len() == 1 && items[0].as_str().is_some_and(|s| s.eq_ignore_ascii_case("all")))
        && value.pointer("/HostConfig/CapAdd").is_some_and(|items|
            items.is_null() || items.as_array().is_some_and(|items| items.is_empty()))
        && value.pointer("/HostConfig/Devices").is_some_and(|items|
            items.is_null() || items.as_array().is_some_and(|items| items.is_empty()))
        && value.pointer("/HostConfig/DeviceRequests").is_some_and(|items|
            items.is_null() || items.as_array().is_some_and(|items| items.is_empty()))
        && value.pointer("/HostConfig/PidMode").and_then(Value::as_str)
            .is_some_and(|mode| mode.is_empty() || mode == "private")
        && value.pointer("/HostConfig/SecurityOpt").and_then(Value::as_array).is_some_and(|items|
            items.len() == 1 && items[0].as_str() == Some("no-new-privileges:true"))
        && value.pointer("/HostConfig/Ulimits").and_then(Value::as_array).is_some_and(|items|
            items.len() == 1 && items[0].get("Name").and_then(Value::as_str) == Some("nofile")
                && items[0].get("Soft").and_then(Value::as_u64) == Some(64)
                && items[0].get("Hard").and_then(Value::as_u64) == Some(64))
        && exact_tmpfs(value);
    if !matches { return Err("inspected Docker policy differs from the producer plan".into()); }
    let mounts = value.pointer("/Mounts").and_then(Value::as_array).ok_or("missing Docker mounts")?;
    if mounts.len() != 1 || mounts[0].get("Type").and_then(Value::as_str) != Some("bind")
        || mounts[0].get("Source").and_then(Value::as_str) != plan.root.to_str()
        || mounts[0].get("Destination").and_then(Value::as_str) != plan.root.to_str()
        || mounts[0].get("RW").and_then(Value::as_bool) != Some(false) {
        return Err("inspected Docker mounts differ from the producer plan".into());
    }
    value.pointer("/State/Pid").and_then(Value::as_u64).filter(|pid| *pid > 0)
        .ok_or("missing inspected container PID".into())
}

impl RuntimeAttestation for DockerObservationGate {
    fn before_launch(&self, plan: &ProducerPlan) -> Result<(), String> {
        let info = json_probe(&self.binary, &plan.docker_host, &["info", "--format", "{{json .}}"]) ?;
        if !info.get("SecurityOptions").and_then(Value::as_array).is_some_and(|options|
            options.iter().any(|item| item.as_str() == Some("name=rootless"))) {
            return Err("Docker Engine did not report rootless mode".into());
        }
        let image = json_probe(&self.binary, &plan.docker_host,
            &["image", "inspect", &plan.image, "--format", "{{json .}}"]) ?;
        let image_id = image.get("Id").and_then(Value::as_str).filter(|id| sha256_id(id))
            .ok_or("missing inspected image ID")?;
        if !image.get("RepoDigests").and_then(Value::as_array).is_some_and(|digests|
            digests.iter().any(|digest| digest.as_str() == Some(plan.image.as_str()))) {
            return Err("pinned image digest was not found in local image inspect".into());
        }
        self.image_id.replace(Some(image_id.into()));
        Ok(())
    }

    fn launching(&self) { self.attempted.set(true); }

    fn after_launch(&self, plan: &ProducerPlan, _: &Child) -> Result<(), String> {
        // This observes daemon records, not the Child's attached stdio origin.
        // A reviewed client/socket and stream-to-CID proof are still required.
        let deadline = Instant::now() + Duration::from_millis(500);
        while !self.cidfile.exists() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        let cid = bounded_file(&self.cidfile, 65)?;
        let cid = cid.trim_end_matches('\n');
        if cid.len() != 64 || !cid.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err("invalid Docker CID file".into());
        }
        if self.cid.borrow().as_deref().is_some_and(|original| original != cid) {
            return Err("Docker CID changed during definition exchange".into());
        }
        let container = json_probe(&self.binary, &plan.docker_host,
            &["container", "inspect", cid, "--format", "{{json .}}"]) ?;
        let image_id = self.image_id.borrow();
        let image_id = image_id.as_deref().ok_or("missing prelaunch image identity")?;
        let pid = verify_container(&container, plan, cid, image_id, &self.name)?;
        verify_cgroup(&self.proc_root, &self.cgroup_root, pid)?;
        self.cid.replace(Some(cid.into()));
        Ok(())
    }
}

impl DockerObservationGate {
    /// Force removal by the observed CID when available, then by the private
    /// random name. A successful bounded full daemon listing must prove absence;
    /// a timed-out or failed `rm` alone never counts as cleanup.
    fn cleanup(&self, plan: &ProducerPlan) -> Result<(), String> {
        if !self.attempted.get() { return Ok(()); }
        let cid = self.cid.borrow().clone();
        if let Some(cid) = cid.as_deref() {
            let _ = text_probe(&self.binary, &plan.docker_host, &["rm", "-f", cid]);
        }
        let _ = text_probe(&self.binary, &plan.docker_host, &["rm", "-f", &self.name]);
        let listed = text_probe(&self.binary, &plan.docker_host,
            &["ps", "-a", "--no-trunc", "--format", "{{json .}}"])?;
        for line in listed.lines() {
            let row: Value = serde_json::from_str(line).map_err(|_| "invalid Docker cleanup listing")?;
            let id = row.get("ID").and_then(Value::as_str).ok_or("missing Docker listing ID")?;
            let names = row.get("Names").and_then(Value::as_str).ok_or("missing Docker listing names")?;
            if id.len() != 64 || !id.bytes().all(|byte| byte.is_ascii_hexdigit()) {
                return Err("invalid Docker listing ID".into());
            }
            if cid.as_deref() == Some(id) || names.split(',').any(|name| name.trim().trim_start_matches('/') == self.name) {
                return Err("Docker container survived forced removal".into());
            }
        }
        Ok(())
    }
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

    fn capture_probe_output(&mut self) -> Result<Vec<u8>, String> {
        let mut stdout_open = true;
        let mut stderr_open = true;
        let mut output = Vec::new();
        while stdout_open || stderr_open {
            let remaining = self.deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() { return Err("Docker probe deadline exceeded".into()); }
            let mut fds = [
                libc::pollfd { fd: if stdout_open { self.stdout.as_raw_fd() } else { -1 }, events: libc::POLLIN, revents: 0 },
                libc::pollfd { fd: if stderr_open { self.stderr.as_raw_fd() } else { -1 }, events: libc::POLLIN, revents: 0 },
            ];
            let ready = unsafe { libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t,
                remaining.as_millis().min(50) as i32) };
            if ready < 0 {
                if std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted { continue; }
                return Err("Docker probe poll failed".into());
            }
            for (index, fd) in fds.iter().enumerate() {
                if fd.revents & (libc::POLLIN | libc::POLLHUP) == 0 { continue; }
                let mut buf = [0u8; 4096];
                let read = if index == 0 { self.stdout.read(&mut buf) } else { self.stderr.read(&mut buf) };
                match read {
                    Ok(0) if index == 0 => stdout_open = false,
                    Ok(0) => stderr_open = false,
                    Ok(n) if index == 0 => {
                        if output.len().saturating_add(n) > MAX_PROBE_OUTPUT {
                            return Err("Docker probe output exceeds 64 KiB".into());
                        }
                        output.extend_from_slice(&buf[..n]);
                    }
                    Ok(n) => {
                        self.error_bytes = self.error_bytes.saturating_add(n);
                        if self.error_bytes > MAX_STDERR { return Err("Docker probe stderr exceeds 32 KiB".into()); }
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {},
                    Err(_) => return Err("Docker probe pipe read failed".into()),
                }
            }
        }
        self.exit_successfully()?;
        Ok(output)
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
    attestation.launching();
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
    // Observe the same CID and effective controls again before accepting the
    // reply. This narrows drift during the LSP exchange; live proof is still
    // required before any binding can be promoted.
    attestation.after_launch(plan, &session.child)?;
    session.send(&json!({"jsonrpc":"2.0","id":99,"method":"shutdown","params":null}))?;
    let shutdown = expect_reply(&mut session, 99)?;
    if !shutdown.get("result").is_some_and(Value::is_null) { return Err("invalid LSP shutdown response".into()); }
    session.send(&json!({"jsonrpc":"2.0","method":"exit","params":null}))?;
    session.exit_successfully()?;
    hash_matches(plan, edge, candidate)?;
    Ok(evidence)
}

/// Private seam exercised only by fake Docker fixtures. The reviewed CLI
/// deliberately uses `unavailable_status` until pinned image and live rootless
/// evidence cover the remaining network, quota, and analyzer controls.
#[allow(dead_code)]
fn run_observed_definition(
    plan: &ProducerPlan, edge: &CallEdge, candidate: &CallCandidate,
    docker_binary: &Path, cidfile: &Path, proc_root: &Path, cgroup_root: &Path,
    timeout: Duration,
) -> Result<DefinitionEvidence, String> {
    let name = random_container_name()?;
    let gate = DockerObservationGate {
        binary: docker_binary.to_path_buf(), cidfile: cidfile.to_path_buf(),
        proc_root: proc_root.to_path_buf(), cgroup_root: cgroup_root.to_path_buf(),
        image_id: RefCell::new(None), cid: RefCell::new(None),
        name, attempted: Cell::new(false),
    };
    let command = plan.observed_docker_command(docker_binary, cidfile, &gate.name)?;
    let result = run_definition(plan, edge, candidate, command, &gate, timeout);
    gate.cleanup(plan).map_err(|error| format!("Docker cleanup unconfirmed: {error}"))?;
    result
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
    use std::os::unix::fs::PermissionsExt;
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
if mode == 'change_cid':
    with open(pid_path + '.cidpath') as f: cid_path = f.read()
    with open(cid_path, 'w') as f: f.write('d' * 64 + '\n')
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
    const FAKE_DOCKER: &str = r#"#!/usr/bin/python3
import json, os, pathlib, sys, time
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
with open(root / 'docker-invocations', 'a') as f: f.write(json.dumps(args) + '\n')
if args[:1] == ['info']:
    mode = (root / 'probe-mode').read_text()
    if mode == 'hang': time.sleep(30)
    if mode == 'flood': sys.stdout.write('X' * 70000); sys.stdout.flush(); time.sleep(30)
    print((root / 'info.json').read_text())
elif args[:2] == ['image', 'inspect']:
    print((root / 'image.json').read_text())
elif args[:2] == ['container', 'inspect']:
    if not (root / 'container-active').exists(): raise SystemExit(1)
    count_file = root / 'inspect-count'
    count = int(count_file.read_text()) + 1 if count_file.exists() else 1
    count_file.write_text(str(count))
    result = json.loads((root / 'container.json').read_text())
    result['Name'] = '/' + (root / 'run-name').read_text()
    if (root / 'probe-mode').read_text() == 'wrong_name': result['Name'] = '/unrelated'
    if (root / 'probe-mode').read_text() == 'drift_after_definition' and count > 1:
        result['HostConfig']['NetworkMode'] = 'bridge'
    print(json.dumps(result))
elif args[:1] == ['run']:
    cidfile = pathlib.Path(args[args.index('--cidfile') + 1])
    mode = (root / 'probe-mode').read_text()
    if mode != 'no_cid': cidfile.write_text((root / 'run-cid').read_text())
    (root / 'run-name').write_text(args[args.index('--name') + 1])
    (root / 'container-active').write_text((root / 'run-cid').read_text())
    (root / 'child.pid.cidpath').write_text(str(cidfile))
    target_uri = 'file://' + str(root / 'b.rs')
    lsp_mode = mode if mode in ['change_cid', 'lsp_hang'] else 'ok'
    if lsp_mode == 'lsp_hang': lsp_mode = 'hang'
    os.execv('/usr/bin/python3', ['/usr/bin/python3', '-u', str(root / 'fake_lsp.py'),
        lsp_mode, target_uri, str(root / 'b.rs'), str(root / 'child.pid')])
elif args[:1] == ['rm']:
    if (root / 'probe-mode').read_text() == 'cleanup_fail': raise SystemExit(3)
    active = root / 'container-active'
    if not active.exists(): raise SystemExit(1)
    if args[-1] not in [active.read_text().strip(), (root / 'run-name').read_text()]: raise SystemExit(2)
    active.unlink()
    print(args[-1])
elif args[:1] == ['ps']:
    active = root / 'container-active'
    if active.exists():
        cid = active.read_text().strip()
        name = (root / 'run-name').read_text()
        print(json.dumps({'ID':cid,'Names':name}))
else:
    raise SystemExit(2)
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

    struct ObservedFixture {
        root: TempDir,
        _cid_dir: TempDir,
        plan: ProducerPlan,
        edge: CallEdge,
        candidate: CallCandidate,
        binary: PathBuf,
        cidfile: PathBuf,
        proc_root: PathBuf,
        cgroup_root: PathBuf,
    }

    fn write_json(path: &Path, value: &Value) {
        fs::write(path, serde_json::to_vec(value).unwrap()).unwrap();
    }

    fn observed_fixture() -> ObservedFixture {
        let (root, plan, edge, candidate, _) = fixture("ok");
        let binary = root.path().join("fake-docker");
        fs::write(&binary, FAKE_DOCKER).unwrap();
        fs::set_permissions(&binary, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(root.path().join("probe-mode"), "ok").unwrap();
        let cid = "c".repeat(64);
        let image_id = format!("sha256:{}", "b".repeat(64));
        fs::write(root.path().join("run-cid"), format!("{cid}\n")).unwrap();
        write_json(&root.path().join("info.json"), &json!({"SecurityOptions":["name=rootless"]}));
        write_json(&root.path().join("image.json"), &json!({"Id":image_id,"RepoDigests":[IMAGE]}));
        write_json(&root.path().join("container.json"), &json!({
            "Id":cid,"Image":image_id,"Config":{"Image":IMAGE,"User":"0:0",
                "WorkingDir":plan.root,"Entrypoint":["/usr/local/bin/rust-analyzer"],
                "Cmd":null,"Env":["HOME=/tmp","TMPDIR=/tmp","CARGO_HOME=/tmp/cargo",
                    "RUSTUP_HOME=/tmp/rustup","CARGO_NET_OFFLINE=true"]},
            "State":{"Running":true,"Pid":4242},
            "NetworkSettings":{"Networks":{}},
            "HostConfig":{
                "NetworkMode":"none","ReadonlyRootfs":true,"Privileged":false,
                "Init":true,"AutoRemove":true,
                "Memory":MEMORY_BYTES,"MemorySwap":MEMORY_BYTES,"NanoCpus":1000000000_u64,
                "PidsLimit":MAX_PIDS,"IpcMode":"private","CgroupnsMode":"private",
                "CapDrop":["ALL"],"CapAdd":null,"Devices":[],"DeviceRequests":[],
                "PidMode":"","SecurityOpt":["no-new-privileges:true"],
                "Ulimits":[{"Name":"nofile","Soft":64,"Hard":64}],
                "Tmpfs":{"/tmp":"rw,noexec,nosuid,nodev,size=67108864,mode=1777"}
            },
            "Mounts":[{"Type":"bind","Source":plan.root,"Destination":plan.root,"RW":false}]
        }));
        let proc_root = root.path().join("fake-proc");
        let cgroup_root = root.path().join("fake-cgroup");
        fs::create_dir_all(proc_root.join("4242")).unwrap();
        fs::write(proc_root.join("4242/cgroup"), "0::/test/container\n").unwrap();
        let group = cgroup_root.join("test/container");
        fs::create_dir_all(&group).unwrap();
        for (name, value) in [("memory.max", "1073741824\n"),
            ("memory.swap.max", "0\n"), ("pids.max", "64\n"), ("cpu.max", "100000 100000\n")] {
            fs::write(group.join(name), value).unwrap();
        }
        let cid_dir = tempfile::tempdir().unwrap();
        fs::set_permissions(cid_dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let cidfile = cid_dir.path().join("container.cid");
        ObservedFixture { root, _cid_dir: cid_dir, plan, edge, candidate, binary, cidfile, proc_root, cgroup_root }
    }

    fn run_observed(fixture: &ObservedFixture) -> Result<DefinitionEvidence, String> {
        run_observed_timeout(fixture, Duration::from_secs(4))
    }

    fn run_observed_timeout(fixture: &ObservedFixture, timeout: Duration) -> Result<DefinitionEvidence, String> {
        run_observed_definition(&fixture.plan, &fixture.edge, &fixture.candidate,
            &fixture.binary, &fixture.cidfile, &fixture.proc_root, &fixture.cgroup_root,
            timeout)
    }

    #[test]
    fn fake_docker_observations_allow_only_untrusted_protocol_evidence() {
        let fixture = observed_fixture();
        let evidence = run_observed(&fixture).unwrap();
        assert_eq!(evidence.status, "protocol_match_untrusted");
        assert_eq!(evidence.binding, "unknown");
        let invocations = fs::read_to_string(fixture.root.path().join("docker-invocations")).unwrap();
        assert!(invocations.contains("image"));
        assert!(invocations.contains("container"));
        assert!(invocations.contains("--cidfile"));
        assert!(invocations.contains("--name"));
        assert!(invocations.contains("\"rm\""));
        assert!(invocations.contains("\"ps\""));
        assert!(!fixture.root.path().join("container-active").exists());
        assert_reaped(&fixture.root.path().join("child.pid"));
    }

    #[test]
    fn fake_docker_preflight_denies_nonrootless_and_mismatched_image_before_run() {
        for invalid in [
            ("info.json", json!({"SecurityOptions":["name=seccomp"]})),
            ("image.json", json!({"Id":format!("sha256:{}", "b".repeat(64)),"RepoDigests":["other@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]})),
            ("image.json", json!({"Id":"sha256:short","RepoDigests":[IMAGE]})),
        ] {
            let fixture = observed_fixture();
            write_json(&fixture.root.path().join(invalid.0), &invalid.1);
            assert!(run_observed(&fixture).is_err());
            assert!(!fixture.cidfile.exists(), "Docker run followed a failed preflight");
        }
    }

    #[test]
    fn fake_docker_inspect_denies_identity_network_mount_and_profile_drift() {
        for path in ["/Id", "/Image", "/Config/Image", "/Config/User",
            "/Config/WorkingDir", "/Config/Entrypoint", "/Config/Env",
            "/HostConfig/NetworkMode",
            "/State/Running", "/HostConfig/ReadonlyRootfs", "/HostConfig/Privileged",
            "/HostConfig/Init", "/HostConfig/AutoRemove", "/HostConfig/Ulimits",
            "/HostConfig/Memory", "/HostConfig/MemorySwap",
            "/HostConfig/NanoCpus", "/HostConfig/PidsLimit", "/HostConfig/IpcMode",
            "/HostConfig/CgroupnsMode", "/HostConfig/CapDrop", "/HostConfig/SecurityOpt",
            "/HostConfig/Tmpfs/~1tmp", "/Mounts/0/RW", "/Mounts/0/Source",
            "/Mounts/0/Destination"] {
            let fixture = observed_fixture();
            let file = fixture.root.path().join("container.json");
            let mut value: Value = serde_json::from_slice(&fs::read(&file).unwrap()).unwrap();
            *value.pointer_mut(path).unwrap() = Value::Null;
            write_json(&file, &value);
            assert!(run_observed(&fixture).is_err(), "accepted inspect drift at {path}");
        }
        for path in ["/NetworkSettings/Networks", "/Mounts"] {
            let fixture = observed_fixture();
            let file = fixture.root.path().join("container.json");
            let mut value: Value = serde_json::from_slice(&fs::read(&file).unwrap()).unwrap();
            *value.pointer_mut(path).unwrap() = json!([{"unexpected":"attachment"}]);
            write_json(&file, &value);
            assert!(run_observed(&fixture).is_err(), "accepted extra resource at {path}");
        }
        let fixture = observed_fixture();
        let file = fixture.root.path().join("container.json");
        let mut value: Value = serde_json::from_slice(&fs::read(&file).unwrap()).unwrap();
        value["Config"]["Cmd"] = json!(["--help"]);
        write_json(&file, &value);
        assert!(run_observed(&fixture).is_err(), "accepted unexpected analyzer arguments");
        for (path, replacement) in [
            ("/HostConfig/CapAdd", json!(["SYS_ADMIN"])),
            ("/HostConfig/Devices", json!([{"PathOnHost":"/dev/kvm"}])),
            ("/HostConfig/DeviceRequests", json!([{"Driver":"nvidia","Count":1}])),
            ("/HostConfig/PidMode", json!("host")),
        ] {
            let fixture = observed_fixture();
            let file = fixture.root.path().join("container.json");
            let mut value: Value = serde_json::from_slice(&fs::read(&file).unwrap()).unwrap();
            *value.pointer_mut(path).unwrap() = replacement;
            write_json(&file, &value);
            assert!(run_observed(&fixture).is_err(), "accepted privilege drift at {path}");
        }
    }

    #[test]
    fn fake_docker_rechecks_policy_after_definition_reply() {
        let fixture = observed_fixture();
        fs::write(fixture.root.path().join("probe-mode"), "drift_after_definition").unwrap();
        assert!(run_observed(&fixture).unwrap_err().contains("policy differs"));
        assert_eq!(fs::read_to_string(fixture.root.path().join("inspect-count")).unwrap(), "2");
        assert_reaped(&fixture.root.path().join("child.pid"));
        assert!(!fixture.root.path().join("container-active").exists());
        let fixture = observed_fixture();
        fs::write(fixture.root.path().join("probe-mode"), "change_cid").unwrap();
        assert!(run_observed(&fixture).unwrap_err().contains("CID changed"));
        assert_reaped(&fixture.root.path().join("child.pid"));
        assert!(!fixture.root.path().join("container-active").exists());
    }

    #[test]
    fn fake_daemon_container_is_removed_after_timeout_or_missing_cid() {
        let fixture = observed_fixture();
        fs::write(fixture.root.path().join("probe-mode"), "lsp_hang").unwrap();
        assert!(run_observed_timeout(&fixture, Duration::from_millis(250))
            .unwrap_err().contains("deadline"));
        assert!(!fixture.root.path().join("container-active").exists());
        assert_reaped(&fixture.root.path().join("child.pid"));
        let fixture = observed_fixture();
        fs::write(fixture.root.path().join("probe-mode"), "no_cid").unwrap();
        assert!(run_observed(&fixture).is_err());
        assert!(!fixture.cidfile.exists());
        assert!(!fixture.root.path().join("container-active").exists());
        assert_reaped(&fixture.root.path().join("child.pid"));
    }

    #[test]
    fn fake_daemon_cleanup_failure_overrides_protocol_match() {
        let fixture = observed_fixture();
        fs::write(fixture.root.path().join("probe-mode"), "cleanup_fail").unwrap();
        assert!(run_observed(&fixture).unwrap_err().contains("Docker cleanup unconfirmed"));
        assert!(fixture.root.path().join("container-active").exists());
        assert_reaped(&fixture.root.path().join("child.pid"));
    }

    #[test]
    fn fake_cgroup_denies_missing_unbounded_and_excess_limits() {
        for (name, value) in [("memory.max", "max"), ("memory.max", "1073741825"),
            ("memory.swap.max", "1"), ("pids.max", "65"),
            ("cpu.max", "max 100000"), ("cpu.max", "100001 100000")] {
            let fixture = observed_fixture();
            fs::write(fixture.cgroup_root.join("test/container").join(name), value).unwrap();
            assert!(run_observed(&fixture).is_err(), "accepted {name}={value}");
        }
        let fixture = observed_fixture();
        fs::write(fixture.proc_root.join("4242/cgroup"), "0::/\n").unwrap();
        assert!(run_observed(&fixture).is_err());
        let fixture = observed_fixture();
        fs::remove_file(fixture.cgroup_root.join("test/container/memory.max")).unwrap();
        assert!(run_observed(&fixture).is_err());
        let fixture = observed_fixture();
        fs::write(fixture.proc_root.join("4242/cgroup"), "0::/../outside\n").unwrap();
        assert!(run_observed(&fixture).is_err());
    }

    #[test]
    fn fake_docker_rejects_unrelated_or_reused_container_ids() {
        let fixture = observed_fixture();
        fs::write(fixture.root.path().join("run-cid"), format!("{}\n", "d".repeat(64))).unwrap();
        assert!(run_observed(&fixture).is_err());
        let fixture = observed_fixture();
        fs::write(&fixture.cidfile, "stale").unwrap();
        assert!(run_observed(&fixture).is_err());
        assert!(!fixture.root.path().join("docker-invocations").exists());
        let fixture = observed_fixture();
        fs::write(fixture.root.path().join("probe-mode"), "wrong_name").unwrap();
        assert!(run_observed(&fixture).is_err());
        assert!(!fixture.root.path().join("container-active").exists());
    }

    #[test]
    fn observed_launcher_requires_private_cid_directory_outside_source() {
        let fixture = observed_fixture();
        assert!(fixture.plan.observed_docker_command(&fixture.binary,
            &fixture.root.path().join("inside.cid"), "doxa-semantic-11111111111111111111111111111111").is_err());
        fs::set_permissions(fixture.cidfile.parent().unwrap(), fs::Permissions::from_mode(0o755)).unwrap();
        assert!(fixture.plan.observed_docker_command(&fixture.binary, &fixture.cidfile,
            "doxa-semantic-11111111111111111111111111111111").is_err());
    }

    #[test]
    fn observed_launcher_rederives_plan_before_any_docker_command() {
        let mut fixture = observed_fixture();
        fixture.plan.args.insert(1, "--privileged".into());
        assert!(run_observed(&fixture).unwrap_err().contains("changed after validation"));
        assert!(!fixture.root.path().join("docker-invocations").exists());
        let mut fixture = observed_fixture();
        fixture.plan.initialize["params"]["initializationOptions"]["procMacro"]["enable"] = json!(true);
        assert!(run_observed(&fixture).unwrap_err().contains("changed after validation"));
        assert!(!fixture.root.path().join("docker-invocations").exists());
        let mut fixture = observed_fixture();
        fixture.plan.docker_host = "unix:///var/run/docker.sock".into();
        assert!(run_observed(&fixture).is_err());
        assert!(!fixture.root.path().join("docker-invocations").exists());
    }

    #[test]
    fn docker_probe_has_wall_clock_and_output_bounds() {
        for (mode, reason) in [("hang", "deadline"), ("flood", "exceeds")] {
            let fixture = observed_fixture();
            fs::write(fixture.root.path().join("probe-mode"), mode).unwrap();
            let error = run_observed(&fixture).unwrap_err();
            assert!(error.contains(reason), "{mode}: {error}");
            assert!(!fixture.cidfile.exists());
        }
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
