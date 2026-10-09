//! Explicit rootless fixture for the restricted-egress transport boundary.
//! No provider credentials or production profile are used. This does not
//! establish provider compatibility or a hardened production egress policy.
use doxa_isolation::{egress::{AllowedHosts, EgressGateway}, manifest_path, Profile, Runtime};
use std::{fs, io, os::unix::fs::PermissionsExt, path::Path, process::{Command, Output}};

fn docker(host: &str, args: &[&str]) -> io::Result<Output> {
    Command::new("docker").env_clear().env("PATH", "/usr/bin:/bin")
        .args(["--host", host]).args(args).output()
}

fn git(path: &Path, args: &[&str]) {
    let output = Command::new("git").env_clear().env("PATH", "/usr/bin:/bin")
        .env("GIT_CONFIG_NOSYSTEM", "1").env("GIT_CONFIG_GLOBAL", "/dev/null")
        .current_dir(path).args(args).output().unwrap();
    assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
}

struct FixtureContainer { host: String, id: String }
impl Drop for FixtureContainer {
    fn drop(&mut self) {
        let _ = docker(&self.host, &["stop", "--time", "1", &self.id]);
        let _ = docker(&self.host, &["rm", &self.id]);
    }
}

const PROXY_REQUEST: &str = r#"
import socket, sys, time
for attempt in range(40):
    try:
        client = socket.create_connection(('127.0.0.1', 33128), timeout=1)
        break
    except OSError:
        time.sleep(0.05)
else:
    sys.exit('fixture proxy did not start')
client.sendall(b'CONNECT denied.example:443 HTTP/1.1\r\nHost: denied.example:443\r\n\r\n')
reply = client.recv(256)
sys.stdout.buffer.write(reply)
client.close()
"#;

#[test]
#[ignore = "requires a task-local rootless Engine and pinned credential-free worker/Python fixture image"]
fn network_none_worker_reaches_only_guarded_gateway_and_fails_closed_on_loss() {
    let image = std::env::var("DOXA_ISOLATION_TEST_IMAGE").expect("explicit pinned fixture image");
    let host = std::env::var("DOXA_ISOLATION_TEST_HOST").expect("explicit task-local rootless Engine");
    let uid = unsafe { libc::geteuid() };
    assert!(host.starts_with(&format!("unix:///run/user/{uid}/")));
    assert!(!host.ends_with("/docker.sock"));

    let fixture = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(fixture.path()).unwrap();
    let source = root.join("source");
    fs::create_dir(&source).unwrap();
    git(&source, &["init", "-q"]);
    fs::write(source.join("README"), "egress fixture\n").unwrap();
    git(&source, &["add", "README"]);
    git(&source, &["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "test: source fixture"]);
    let home = root.join("home");
    fs::create_dir(&home).unwrap();
    fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
    let config = format!("docker_image = {image:?}\ndocker_host = {host:?}\ndocker_memory_bytes = 1073741824\ndocker_cpus = 1.0\ndocker_pids = 128\n");
    fs::write(home.join("config.toml"), config).unwrap();
    let id = format!("egress-smoke-{}", std::process::id());
    let mut runtime = Runtime::prepare(&home, &id, &source, Some(Profile::DockerOffline), false, None).unwrap();
    let container = FixtureContainer { host: host.clone(), id: runtime.manifest().container_id.clone().unwrap() };

    let gateway = EgressGateway::start_for_session(
        &manifest_path(&home, &id).unwrap(), AllowedHosts::new(&["allowed.example".into()]).unwrap(),
    ).unwrap();
    assert!(gateway.socket().exists());
    let started = docker(&host, &["exec", "-d", &container.id,
        "/usr/local/bin/doxa-isolation-worker", "egress-proxy", "33128"]).unwrap();
    assert!(started.status.success(), "{}", String::from_utf8_lossy(&started.stderr));
    let denied = docker(&host, &["exec", &container.id, "/usr/bin/env", "python3", "-c", PROXY_REQUEST]).unwrap();
    assert!(denied.status.success(), "{}", String::from_utf8_lossy(&denied.stderr));
    assert!(denied.stdout.starts_with(b"HTTP/1.1 403 Forbidden"), "{:?}", denied.stdout);

    let bypass = docker(&host, &["exec", &container.id, "/usr/bin/env", "python3", "-c",
        "import socket; socket.create_connection(('1.1.1.1', 443), timeout=0.5)"]).unwrap();
    assert!(!bypass.status.success(), "network-none worker reached a public IP directly");

    drop(gateway);
    let unavailable = docker(&host, &["exec", &container.id, "/usr/bin/env", "python3", "-c", PROXY_REQUEST]).unwrap();
    assert!(unavailable.status.success(), "{}", String::from_utf8_lossy(&unavailable.stderr));
    assert!(unavailable.stdout.starts_with(b"HTTP/1.1 502 Bad Gateway"), "{:?}", unavailable.stdout);
    runtime.stop().unwrap();
}
