use std::{
    fs,
    io::{BufRead, BufReader, Read},
    net::TcpStream,
    os::unix::fs::PermissionsExt,
    process::{Command, Stdio},
    thread,
    time::Duration,
};

#[test]
fn missing_browser_opener_does_not_stop_mesh_serve() {
    let root = tempfile::tempdir().unwrap();
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
    let ledger = root.path().join("messages.jsonl");
    let mut child = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args(["mesh", "serve", "--ledger", ledger.to_str().unwrap()])
        .env("DOXA_HOME", root.path().join("home"))
        .env("DOXA_MESH_OPEN_BROWSER", "1")
        .env("PATH", "/nonexistent")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let mut line = String::new();
    assert!(stdout.read_line(&mut line).unwrap() > 0, "mesh did not print its ledger");
    line.clear();
    assert!(stdout.read_line(&mut line).unwrap() > 0, "mesh did not print its URL");
    let url = line.trim().to_owned();
    let address = url.trim_start_matches("http://").split('/').next().unwrap();
    thread::sleep(Duration::from_millis(150));
    let still_running = child.try_wait().unwrap().is_none();
    let reachable = TcpStream::connect(address).is_ok();
    unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGINT); }
    let mut stderr = String::new();
    child.stderr.take().unwrap().read_to_string(&mut stderr).unwrap();
    let status = child.wait().unwrap();
    assert!(still_running, "mesh exited after browser launch failed: {stderr}");
    assert!(reachable, "mesh URL was not reachable: {url}");
    assert!(stderr.contains("browser unavailable"), "{stderr}");
    assert!(status.success(), "{stderr}");
}
