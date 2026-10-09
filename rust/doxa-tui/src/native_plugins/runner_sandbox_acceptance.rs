//! Explicit, ignored acceptance run for a Linux host with delegated cgroup v2
//! memory, pids, and cpu controllers. The ordinary suite never runs this.
use super::*;
use super::super::runner_process::{supervise_with_input, Capture, Outcome};
use std::io;
use std::net::TcpListener;
use std::os::unix::fs::PermissionsExt;
use std::sync::atomic::Ordering;
use sha2::{Digest, Sha256};

const POLL: Duration = Duration::from_millis(10);
const PROBE_WAIT: Duration = Duration::from_secs(3);
const EVIDENCE_LIMIT: u64 = 4_096;

fn bounded_text(path: &Path) -> io::Result<String> {
    let mut bytes = Vec::new();
    File::open(path)?.take(EVIDENCE_LIMIT + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > EVIDENCE_LIMIT {
        return Err(unavailable("acceptance evidence exceeded 4 KiB"));
    }
    String::from_utf8(bytes).map_err(|_| unavailable("non-UTF-8 acceptance evidence"))
}

fn counter_in(text: &str, name: &str) -> io::Result<u64> {
    let mut result = None;
    for row in text.lines() {
        let mut fields = row.split_whitespace();
        if fields.next() != Some(name) { continue; }
        let value = fields.next().ok_or_else(|| unavailable("missing cgroup counter"))?;
        if fields.next().is_some() || result.is_some() {
            return Err(unavailable("ambiguous cgroup counter"));
        }
        result = Some(value.parse().map_err(|_| unavailable("invalid cgroup counter"))?);
    }
    result.ok_or_else(|| unavailable(format!("missing cgroup counter {name}")))
}

fn counter(budget: &CgroupBudget, file: &str, name: &str) -> io::Result<u64> {
    counter_in(&bounded_text(&budget.path.join(file))?, name)
}

#[derive(Debug)]
struct Counters {
    memory_peak: u64,
    memory_oom_kill: u64,
    pids_peak: u64,
    pids_max_events: u64,
    cpu_usage_usec: u64,
    cpu_throttled: u64,
}

impl Counters {
    fn read(budget: &CgroupBudget) -> io::Result<Self> {
        Ok(Self {
            memory_peak: bounded_text(&budget.path.join("memory.peak"))?.trim().parse()
                .map_err(|_| unavailable("invalid memory.peak"))?,
            memory_oom_kill: counter(budget, "memory.events", "oom_kill")?,
            pids_peak: bounded_text(&budget.path.join("pids.peak"))?.trim().parse()
                .map_err(|_| unavailable("invalid pids.peak"))?,
            pids_max_events: counter(budget, "pids.events", "max")?,
            cpu_usage_usec: counter(budget, "cpu.stat", "usage_usec")?,
            cpu_throttled: counter(budget, "cpu.stat", "nr_throttled")?,
        })
    }
}

fn assert_installed_limits(budget: &CgroupBudget) {
    for (name, expected) in [
        ("memory.max", MEMORY_MAX), ("memory.swap.max", "0"),
        ("pids.max", PIDS_MAX), ("cpu.max", CPU_MAX),
    ] {
        assert_eq!(bounded_text(&budget.path.join(name)).unwrap().trim(), expected, "{name}");
    }
}

fn members(budget: &CgroupBudget) -> io::Result<Vec<u32>> {
    bounded_text(&budget.path.join("cgroup.procs"))?.lines()
        .map(|line| line.parse().map_err(|_| unavailable("invalid cgroup PID"))).collect()
}

fn wait_for<T>(what: &str, mut probe: impl FnMut() -> Option<T>) -> T {
    let deadline = Instant::now() + PROBE_WAIT;
    loop {
        if let Some(found) = probe() { return found; }
        assert!(Instant::now() < deadline, "acceptance probe timed out: {what}");
        thread::sleep(POLL);
    }
}

fn is_member(budget: &CgroupBudget, pid: u32) -> bool {
    let expected = budget.path.strip_prefix(CGROUP_ROOT).unwrap();
    let expected = format!("0::/{}", expected.display());
    bounded_text(&PathBuf::from(format!("/proc/{pid}/cgroup")))
        .is_ok_and(|actual| actual.lines().any(|line| line == expected))
}

fn fixture_dir() -> tempfile::TempDir {
    let tmpdir = std::env::var_os("TMPDIR").expect("set TMPDIR to a real-disk private directory");
    let tmpdir = PathBuf::from(tmpdir);
    assert!(tmpdir.is_absolute() && !tmpdir.starts_with("/tmp"),
        "TMPDIR must name a real-disk absolute directory outside /tmp");
    tempfile::Builder::new().prefix("doxa-plugin-acceptance-")
        .tempdir_in(tmpdir).unwrap()
}

fn worker_script(dir: &Path, body: &str) -> PathBuf {
    let worker = dir.join("worker");
    fs::write(&worker, format!("#!/bin/sh\n{body}\n")).unwrap();
    fs::set_permissions(&worker, fs::Permissions::from_mode(0o700)).unwrap();
    worker
}

fn write_private(path: &Path, bytes: impl AsRef<[u8]>) {
    fs::write(path, bytes).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
}

fn plugin_cgroups(parent: &Path) -> io::Result<usize> {
    let mut count = 0;
    for (visited, entry) in fs::read_dir(parent)?.enumerate() {
        if visited >= 4096 { return Err(unavailable("delegated parent has too many children to audit")); }
        if entry?.file_name().to_string_lossy().starts_with("doxa-plugin-") {
            count += 1;
        }
    }
    Ok(count)
}

fn approved_wasm_case(parent: &Path) {
    let worker = PathBuf::from(std::env::var_os("DOXA_PLUGIN_ACCEPTANCE_WORKER")
        .expect("proof harness must build and supply DOXA_PLUGIN_ACCEPTANCE_WORKER"));
    assert!(worker.is_absolute(), "acceptance worker path must be absolute");
    let home = fixture_dir();
    let package_dir = home.path().join("native-plugin-packages/demo");
    fs::create_dir_all(&package_dir).unwrap();
    for path in [home.path().to_path_buf(), home.path().join("native-plugin-packages"), package_dir.clone()] {
        fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
    }
    let module = wat::parse_str("(module (func (export \"doxa_main\") (result i32) i32.const 17))").unwrap();
    let manifest = b"package_api_version = 1\nname = 'demo'\nversion = '1.0'\nartifact_format = 'wasm-core-v1'\nrequested_grants = []\n";
    write_private(&package_dir.join("manifest.toml"), manifest);
    write_private(&package_dir.join("module.wasm"), &module);
    let config = format!(
        "[[native_plugin_packages]]\nname = 'demo'\nmanifest_sha256 = '{:x}'\nmodule_sha256 = '{:x}'\ngrants = []\n",
        Sha256::digest(manifest), Sha256::digest(&module));
    write_private(&home.path().join("config.toml"), config.as_bytes());
    let review = super::super::packages::preflight(home.path(), "demo").unwrap();
    assert!(review.owner_approved && review.requested_grants.is_empty());
    assert_eq!(plugin_cgroups(parent).unwrap(), 0, "disposable host already has plugin worker cgroups");
    let start = Instant::now();
    let result = supervise_reviewed(home.path(), &review, &worker, &AtomicBool::new(false),
        start + Duration::from_secs(5)).expect("approved worker failed its sandbox");
    assert_eq!(result, IsolatedOutcome::Return(17));
    assert_eq!(plugin_cgroups(parent).unwrap(), 0, "approved worker cgroup survived return");
    // Changed approval must be refused before the runner creates a cgroup.
    write_private(&home.path().join("config.toml"), b"");
    assert!(supervise_reviewed(home.path(), &review, &worker, &AtomicBool::new(false),
        Instant::now() + Duration::from_secs(5)).is_err());
    assert_eq!(plugin_cgroups(parent).unwrap(), 0, "stale approval created a worker cgroup");
    eprintln!("plugin-acceptance case=approved-wasm outcome=Return(17) elapsed_ms={} stale_approval=refused cleanup=removed",
        start.elapsed().as_millis());
}

struct Case {
    capture: Capture,
    counters: Counters,
    elapsed: Duration,
}

fn run_case(
    name: &str,
    body: &str,
    deadline_after: Duration,
    drive: impl FnOnce(&CgroupBudget, &AtomicBool),
) -> Case {
    let dir = fixture_dir();
    let worker = worker_script(dir.path(), body);
    let mut budget = CgroupBudget::create().expect("delegated memory, pids and cpu controllers required");
    assert_installed_limits(&budget);
    let path = budget.path.clone();
    let WorkerCommand { mut command, status: _status } =
        command_impl(&worker, &[], Some(&budget)).expect("trusted Bubblewrap launch required");
    command.env("DOXA_PLUGIN_TEST_SECRET", "host-only-value");
    let cancel = AtomicBool::new(false);
    let start = Instant::now();
    let deadline = start + deadline_after;
    let capture = thread::scope(|scope| {
        let child = scope.spawn(|| supervise_with_input(&mut command, None, &cancel, deadline));
        drive(&budget, &cancel);
        child.join().unwrap().expect("supervised plugin launch failed")
    });
    let elapsed = start.elapsed();
    let counters = Counters::read(&budget).expect("missing bounded cgroup evidence");
    budget.stop().expect("cgroup.kill must remove all descendants");
    assert!(!path.exists(), "private plugin cgroup survived cleanup");
    eprintln!("plugin-acceptance case={name} outcome={:?} elapsed_ms={} memory_peak={} oom_kill={} pids_peak={} pids_max={} cpu_usec={} cpu_throttled={} stdout_bytes={} stderr_bytes={} cleanup=removed",
        capture.outcome, elapsed.as_millis(), counters.memory_peak, counters.memory_oom_kill,
        counters.pids_peak, counters.pids_max_events, counters.cpu_usage_usec,
        counters.cpu_throttled, capture.stdout.len(), capture.stderr.len());
    Case { capture, counters, elapsed }
}

#[test]
fn acceptance_counter_parser_rejects_missing_duplicate_and_invalid_rows() {
    assert_eq!(counter_in("usage_usec 42\nnr_throttled 3\n", "usage_usec").unwrap(), 42);
    for text in ["", "usage_usec x", "usage_usec 1 2", "usage_usec 1\nusage_usec 2"] {
        assert!(counter_in(text, "usage_usec").is_err(), "accepted {text:?}");
    }
}

/// Run only on a disposable delegated Linux host, explicitly opted in. This
/// tests the actual cgroup-backed Bubblewrap launcher and its cleanup path.
#[test]
#[ignore = "requires explicit delegated-cgroup acceptance host"]
fn delegated_cgroup_containment_acceptance() {
    assert_eq!(std::env::var("DOXA_PLUGIN_CGROUP_ACCEPTANCE").ok().as_deref(), Some("1"),
        "set DOXA_PLUGIN_CGROUP_ACCEPTANCE=1 after provisioning a delegated fixture host");
    assert!(std::thread::available_parallelism().unwrap().get() >= 2,
        "CPU throttling proof needs at least two available processors");
    let parent = delegated_cgroup_parent().expect("empty delegated parent and supervisor leaf required");
    assert!(parent.starts_with(CGROUP_ROOT));
    approved_wasm_case(&parent);

    let host_marker = fixture_dir();
    let secret = host_marker.path().join("host-secret");
    fs::write(&secret, b"host-only").unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let port = listener.local_addr().unwrap().port();
    let host_net_namespace = fs::read_link("/proc/self/ns/net").unwrap();
    let host_mount_namespace = fs::read_link("/proc/self/ns/mnt").unwrap();
    let host_user_namespace = fs::read_link("/proc/self/ns/user").unwrap();
    let host_pid_namespace = fs::read_link("/proc/self/ns/pid").unwrap();
    let boundary = run_case("boundary", &format!(
        "sleep 1\ntest ! -e /home || exit 31\ntest ! -e '{}' || exit 32\ntest -z \"${{DOXA_PLUGIN_TEST_SECRET-}}\" || exit 33\nif /bin/bash -c 'exec 3<>/dev/tcp/127.0.0.1/{port}' 2>/dev/null; then exit 34; fi\n/usr/bin/awk 'NR > 2 {{ split($0, a, \":\"); gsub(/[[:space:]]/, \"\", a[1]); if (a[1] != \"lo\") exit 1 }}' /proc/net/dev || exit 35\n/usr/bin/awk 'NR > 1 {{ exit 1 }}' /proc/net/route || exit 36\nprintf 'net=%s\\nmnt=%s\\nuser=%s\\npid=%s\\n' \"$(/usr/bin/readlink /proc/self/ns/net)\" \"$(/usr/bin/readlink /proc/self/ns/mnt)\" \"$(/usr/bin/readlink /proc/self/ns/user)\" \"$(/usr/bin/readlink /proc/self/ns/pid)\"",
        secret.display()), Duration::from_secs(4), |budget, _| {
        wait_for("worker cgroup membership", || {
            members(budget).ok()?.into_iter().find(|pid| {
                is_member(budget, *pid)
                    && bounded_text(&PathBuf::from(format!("/proc/{pid}/cmdline")))
                        .is_ok_and(|cmdline| cmdline.starts_with("/bin/sh\0/worker\0"))
            })
        });
    });
    assert_eq!(boundary.capture.outcome, Outcome::Exit(0));
    let boundary_receipt = std::str::from_utf8(&boundary.capture.stdout).unwrap();
    let mut receipt_lines = boundary_receipt.lines();
    let net = receipt_lines.next().unwrap().strip_prefix("net=").unwrap();
    let mount = receipt_lines.next().unwrap().strip_prefix("mnt=").unwrap();
    let user = receipt_lines.next().unwrap().strip_prefix("user=").unwrap();
    let pid = receipt_lines.next().unwrap().strip_prefix("pid=").unwrap();
    assert!(receipt_lines.next().is_none(), "unexpected sandbox boundary receipt");
    assert!(net.starts_with("net:[") && net.ends_with(']')
        && mount.starts_with("mnt:[") && mount.ends_with(']')
        && user.starts_with("user:[") && user.ends_with(']')
        && pid.starts_with("pid:[") && pid.ends_with(']'));
    assert_ne!(net, host_net_namespace.to_str().unwrap(), "worker reused host network namespace");
    assert_ne!(mount, host_mount_namespace.to_str().unwrap(), "worker reused host mount namespace");
    assert_ne!(user, host_user_namespace.to_str().unwrap(), "worker reused host user namespace");
    assert_ne!(pid, host_pid_namespace.to_str().unwrap(), "worker reused host PID namespace");
    assert_eq!(listener.accept().unwrap_err().kind(), io::ErrorKind::WouldBlock);

    let pids = run_case("pids", "for i in $(seq 1 48); do /bin/sleep 5 2>/dev/null & done\nwait",
        Duration::from_secs(4), |budget, cancel| {
            wait_for("pids.max event", || (counter(budget, "pids.events", "max").ok()? > 0).then_some(()));
            cancel.store(true, Ordering::Release);
        });
    assert!(matches!(pids.capture.outcome, Outcome::Cancelled | Outcome::Exit(_)),
        "PID denial produced an unexpected supervisor outcome");
    assert!(pids.counters.pids_max_events > 0 && pids.counters.pids_peak <= 16);

    let memory = run_case("memory", "for i in 1 2 3; do /usr/bin/python3 -c 'import time; x=bytearray(110*1024*1024); time.sleep(5)' & done\nwait",
        Duration::from_secs(4), |budget, cancel| {
            wait_for("aggregate memory OOM", || (counter(budget, "memory.events", "oom_kill").ok()? > 0).then_some(()));
            cancel.store(true, Ordering::Release);
        });
    // The kernel may kill the wrapper before the parent observes cancellation.
    assert!(matches!(memory.capture.outcome, Outcome::Cancelled | Outcome::Exit(_) | Outcome::Crash(_)),
        "memory OOM produced an unexpected supervisor outcome");
    assert!(memory.counters.memory_oom_kill > 0 && memory.counters.memory_peak <= 300 * 1024 * 1024);

    let cpu = run_case("cpu", "for i in 1 2 3; do /bin/sh -c 'while :; do :; done' & done\nwait",
        Duration::from_millis(1_500), |budget, _| {
            wait_for("aggregate CPU throttle", || (counter(budget, "cpu.stat", "nr_throttled").ok()? > 0).then_some(()));
        });
    assert_eq!(cpu.capture.outcome, Outcome::Timeout);
    assert!(cpu.counters.cpu_throttled > 0);
    assert!(cpu.counters.cpu_usage_usec <= cpu.elapsed.as_micros() as u64 + 500_000,
        "aggregate CPU use exceeded the one-CPU quota plus scheduling slack");

    let escape_marker = format!("doxa_plugin_escape_{}", uuid::Uuid::new_v4().simple());
    let escaped = run_case("setsid-cancel", &format!(
        "/usr/bin/setsid /bin/bash -c 'exec -a {escape_marker} /bin/sleep 30' >/dev/null 2>&1 &\nwhile :; do /bin/sleep 1; done"),
        Duration::from_secs(4), |budget, cancel| {
            let pid = wait_for("setsid descendant", || {
                members(budget).ok()?.into_iter().find(|pid| {
                    bounded_text(&PathBuf::from(format!("/proc/{pid}/cmdline")))
                        .is_ok_and(|text| text.contains(&escape_marker))
                })
            });
            assert!(is_member(budget, pid), "setsid descendant missed private cgroup");
            let stat = bounded_text(&PathBuf::from(format!("/proc/{pid}/stat"))).unwrap();
            let after_comm = stat.rsplit_once(')').unwrap().1;
            let pgrp: u32 = after_comm.split_whitespace().nth(2).unwrap().parse().unwrap();
            assert_eq!(pgrp, pid, "setsid descendant did not escape the process group");
            cancel.store(true, Ordering::Release);
        });
    assert_eq!(escaped.capture.outcome, Outcome::Cancelled);

    let timeout = run_case("timeout", "while :; do /bin/sleep 1; done",
        Duration::from_millis(350), |budget, _| {
            wait_for("timeout worker membership", || members(budget).ok().filter(|pids| !pids.is_empty()));
        });
    assert_eq!(timeout.capture.outcome, Outcome::Timeout);
}
