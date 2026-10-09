//! Explicit rootless fixture for the restricted-egress transport boundary.
//! No provider credentials or production profile are used. This does not
//! establish provider compatibility or a hardened production egress policy.
use doxa_isolation::{egress::{AllowedHosts, EgressGateway}, manifest_path, Profile, Runtime};
use std::{fs, io, os::unix::fs::PermissionsExt, path::Path, process::{Command, Output},
    sync::atomic::{AtomicUsize, Ordering}};

static NEXT_FIXTURE: AtomicUsize = AtomicUsize::new(0);

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

struct RootlessFixture {
    runtime: Runtime,
    container: FixtureContainer,
    home: std::path::PathBuf,
    id: String,
    _owned: tempfile::TempDir,
}

fn fixture() -> RootlessFixture {
    let image = std::env::var("DOXA_ISOLATION_TEST_IMAGE").expect("explicit pinned fixture image");
    let host = std::env::var("DOXA_ISOLATION_TEST_HOST").expect("explicit task-local rootless Engine");
    let uid = unsafe { libc::geteuid() };
    assert!(host.starts_with(&format!("unix:///run/user/{uid}/")));
    assert!(!host.ends_with("/docker.sock"));
    let owned = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(owned.path()).unwrap();
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
    let id = format!("egress-smoke-{}-{}", std::process::id(),
        NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed));
    let runtime = Runtime::prepare(&home, &id, &source, Some(Profile::DockerOffline), false, None).unwrap();
    let container = FixtureContainer { host, id: runtime.manifest().container_id.clone().unwrap() };
    RootlessFixture { runtime, container, home, id, _owned: owned }
}

fn proxy(fixture: &RootlessFixture) {
    let started = docker(&fixture.container.host, &["exec", "-d", &fixture.container.id,
        "/usr/local/bin/doxa-isolation-worker", "egress-proxy", "33128"]).unwrap();
    assert!(started.status.success(), "{}", String::from_utf8_lossy(&started.stderr));
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

// The fixture supplies only an exact DNS name. No credential, URL query,
// request body, redirect following, or provider endpoint is involved.
const ALLOWED_TLS_REQUEST: &str = r#"
import json, socket, ssl, sys, time
host = sys.argv[1]
for attempt in range(40):
    try:
        raw = socket.create_connection(('127.0.0.1', 33128), timeout=1)
        break
    except OSError:
        time.sleep(0.05)
else:
    sys.exit('fixture proxy did not start')
raw.settimeout(12)
raw.sendall(('CONNECT ' + host + ':443 HTTP/1.1\r\nHost: ' + host + ':443\r\n\r\n').encode('ascii'))
def header(sock):
    data = bytearray()
    while not data.endswith(b'\r\n\r\n') and len(data) < 8192:
        part = sock.recv(1)
        if not part:
            raise RuntimeError('incomplete response header')
        data.extend(part)
    if len(data) >= 8192:
        raise RuntimeError('response header too large')
    return bytes(data)
if not header(raw).startswith(b'HTTP/1.1 200 Connection Established\r\n'):
    raise RuntimeError('allowlisted CONNECT failed')
with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
    tls.settimeout(12)
    tls.sendall(('GET / HTTP/1.1\r\nHost: ' + host + '\r\nConnection: close\r\nUser-Agent: doxa-egress-fixture\r\n\r\n').encode('ascii'))
    first = header(tls).split(b'\r\n', 1)[0].split(b' ')
    if len(first) < 2 or not first[0].startswith(b'HTTP/1.'):
        raise RuntimeError('unexpected HTTPS response')
    status = int(first[1])
    if not 200 <= status < 400:
        raise RuntimeError('HTTPS upstream did not return success or redirect')
    print(json.dumps({'tls_verified': True, 'http_status': status}))
"#;

// A credential-free standard-library client exercises the common provider
// pattern: HTTPS_PROXY, streaming body reads, a second denied destination and
// an attempted direct fallback. This is not a Claude/Codex login proof.
const PROVIDER_STYLE_REQUEST: &str = r#"
import json, sys, urllib.error, urllib.request
host = sys.argv[1]
proxy = urllib.request.getproxies().get('https')
if proxy != 'http://127.0.0.1:33128':
    raise RuntimeError('HTTPS_PROXY was not selected')
with urllib.request.urlopen('https://' + host + '/', timeout=12) as response:
    status = response.status
    chunks = 0
    while chunks < 4:
        part = response.read(128)
        if not part:
            break
        chunks += 1
    if not 200 <= status < 300 or chunks == 0:
        raise RuntimeError('provider-style HTTPS stream unavailable')
try:
    urllib.request.urlopen('https://denied.example/', timeout=5)
except urllib.error.URLError as exc:
    if '403' not in str(exc):
        raise RuntimeError('denied destination did not reach the gateway') from exc
else:
    raise RuntimeError('denied destination unexpectedly reached')
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    direct.open('https://' + host + '/', timeout=2)
except urllib.error.URLError:
    pass
else:
    raise RuntimeError('direct fallback bypassed network-none')
print(json.dumps({'proxy_selected': True, 'stream_chunks': chunks,
                  'denied_connect': True, 'direct_fallback_blocked': True}, sort_keys=True))
"#;

fn verified_probe_reply(bytes: &[u8]) -> io::Result<u16> {
    if bytes.len() > 128 { return Err(io::Error::other("upstream probe reply exceeded bound")); }
    let value: serde_json::Value = serde_json::from_slice(bytes)?;
    let object = value.as_object().ok_or_else(|| io::Error::other("upstream probe reply malformed"))?;
    if object.len() != 2 || object.get("tls_verified") != Some(&serde_json::json!(true)) {
        return Err(io::Error::other("upstream TLS was not verified"));
    }
    let status = object.get("http_status").and_then(serde_json::Value::as_u64)
        .filter(|status| (200..400).contains(status))
        .ok_or_else(|| io::Error::other("upstream HTTP status unverified"))?;
    Ok(status as u16)
}

#[test]
#[ignore = "requires a task-local rootless Engine and pinned credential-free worker/Python fixture image"]
fn network_none_worker_reaches_only_guarded_gateway_and_fails_closed_on_loss() {
    let mut fixture = fixture();

    let gateway = EgressGateway::start_for_session(
        &manifest_path(&fixture.home, &fixture.id).unwrap(), AllowedHosts::new(&["allowed.example".into()]).unwrap(),
    ).unwrap();
    assert!(gateway.socket().exists());
    proxy(&fixture);
    let denied = docker(&fixture.container.host, &["exec", &fixture.container.id, "/usr/bin/env", "python3", "-c", PROXY_REQUEST]).unwrap();
    assert!(denied.status.success(), "{}", String::from_utf8_lossy(&denied.stderr));
    assert!(denied.stdout.starts_with(b"HTTP/1.1 403 Forbidden"), "{:?}", denied.stdout);

    let bypass = docker(&fixture.container.host, &["exec", &fixture.container.id, "/usr/bin/env", "python3", "-c",
        "import socket; socket.create_connection(('1.1.1.1', 443), timeout=0.5)"]).unwrap();
    assert!(!bypass.status.success(), "network-none worker reached a public IP directly");

    drop(gateway);
    let unavailable = docker(&fixture.container.host, &["exec", &fixture.container.id, "/usr/bin/env", "python3", "-c", PROXY_REQUEST]).unwrap();
    assert!(unavailable.status.success(), "{}", String::from_utf8_lossy(&unavailable.stderr));
    assert!(unavailable.stdout.starts_with(b"HTTP/1.1 502 Bad Gateway"), "{:?}", unavailable.stdout);
    fixture.runtime.stop().unwrap();
}

#[test]
#[ignore = "requires explicit public DNS upstream, pinned credential-free image and task-local rootless Engine"]
fn network_none_worker_reaches_one_allowlisted_public_https_upstream_only() {
    let upstream = std::env::var("DOXA_ISOLATION_TEST_EGRESS_UPSTREAM")
        .expect("explicit credential-free public HTTPS hostname");
    let hosts = AllowedHosts::new(&[upstream.clone()]).unwrap();
    let mut fixture = fixture();
    let gateway = EgressGateway::start_for_session(
        &manifest_path(&fixture.home, &fixture.id).unwrap(), hosts).unwrap();
    proxy(&fixture);
    let permitted = docker(&fixture.container.host, &["exec", &fixture.container.id,
        "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "python3", "-c",
        ALLOWED_TLS_REQUEST, &upstream]).unwrap();
    assert!(permitted.status.success(), "credential-free HTTPS probe failed");
    let status = verified_probe_reply(&permitted.stdout).unwrap();
    eprintln!("allowlisted HTTPS transport verified; HTTP status {status}");

    let denied = docker(&fixture.container.host, &["exec", &fixture.container.id,
        "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "python3", "-c",
        PROXY_REQUEST]).unwrap();
    assert!(denied.status.success());
    assert!(denied.stdout.starts_with(b"HTTP/1.1 403 Forbidden"));
    let direct = docker(&fixture.container.host, &["exec", &fixture.container.id,
        "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "python3", "-c",
        "import socket,sys; socket.create_connection((sys.argv[1],443),timeout=2)", &upstream]).unwrap();
    assert!(!direct.status.success(), "network-none worker reached hostname directly");
    let direct_ip = docker(&fixture.container.host, &["exec", &fixture.container.id,
        "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "python3", "-c",
        "import socket; socket.create_connection(('1.1.1.1',443),timeout=2)"]).unwrap();
    assert!(!direct_ip.status.success(), "network-none worker reached a public IP directly");
    drop(gateway);
    fixture.runtime.stop().unwrap();
}

#[test]
#[ignore = "requires explicit public 2xx upstream with a body, pinned Python fixture image and task-local rootless Engine"]
fn network_none_provider_style_proxy_stream_denies_second_host_and_direct_fallback() {
    let upstream = std::env::var("DOXA_ISOLATION_TEST_EGRESS_UPSTREAM")
        .expect("explicit credential-free public HTTPS hostname");
    let mut fixture = fixture();
    let gateway = EgressGateway::start_for_session(
        &manifest_path(&fixture.home, &fixture.id).unwrap(),
        AllowedHosts::new(&[upstream.clone()]).unwrap()).unwrap();
    proxy(&fixture);
    let output = docker(&fixture.container.host, &["exec", &fixture.container.id,
        "/usr/bin/env", "-i", "PATH=/usr/bin:/bin",
        "HTTPS_PROXY=http://127.0.0.1:33128", "HTTP_PROXY=http://127.0.0.1:33128",
        "NO_PROXY=", "python3", "-c", PROVIDER_STYLE_REQUEST, &upstream]).unwrap();
    assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    assert!(output.stdout.len() <= 256);
    let proof: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(proof, serde_json::json!({"proxy_selected":true,
        "stream_chunks":proof["stream_chunks"],"denied_connect":true,
        "direct_fallback_blocked":true}));
    assert!(proof["stream_chunks"].as_u64().is_some_and(|n| (1..=4).contains(&n)));
    drop(gateway);
    fixture.runtime.stop().unwrap();
}

#[test]
fn upstream_probe_receipt_requires_verified_tls_and_bounded_http_status() {
    assert_eq!(verified_probe_reply(br#"{"tls_verified":true,"http_status":200}"#).unwrap(), 200);
    for invalid in [
        br#"{"tls_verified":false,"http_status":200}"#.as_slice(),
        br#"{"tls_verified":true,"http_status":403}"#,
        br#"{"tls_verified":true,"http_status":200,"body":"unreviewed"}"#,
        br#"{"tls_verified":true,"http_status":"200"}"#,
        br#"{"tls_verified":true,"http_status":200"#,
    ] { assert!(verified_probe_reply(invalid).is_err()); }
    assert!(verified_probe_reply(&vec![b'x'; 129]).is_err());
}
