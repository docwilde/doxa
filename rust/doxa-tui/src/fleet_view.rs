//! Read-only access to Python 1.19 fleet manifests.
use serde_json::Value;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read};
use std::os::unix::fs::{FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

const MAX_MANIFEST_BYTES: u64 = 1024 * 1024;
const MAX_RUNS: usize = 1024;

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

pub fn default_root() -> io::Result<PathBuf> {
    let home = std::env::var_os("DOXA_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|value| PathBuf::from(value).join(".doxa")))
        .ok_or_else(|| invalid("DOXA_HOME and HOME are unset"))?;
    if !home.is_absolute() {
        return Err(invalid("DOXA_HOME must be absolute"));
    }
    Ok(home.join("fleet"))
}

fn private_file(path: &Path) -> io::Result<File> {
    let file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW).open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o077 != 0
        || metadata.len() > MAX_MANIFEST_BYTES {
        return Err(io::Error::new(io::ErrorKind::PermissionDenied, "unsafe fleet manifest"));
    }
    Ok(file)
}

fn manifest(run_root: &Path) -> io::Result<Value> {
    let file = private_file(&run_root.join("manifest.json"))?;
    let mut bytes = Vec::new();
    file.take(MAX_MANIFEST_BYTES + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_MANIFEST_BYTES {
        return Err(invalid("fleet manifest exceeds size limit"));
    }
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "fleet manifest is incomplete"))?;
    if !value.is_object() {
        return Err(invalid("fleet manifest must be an object"));
    }
    Ok(value)
}

fn run_dirs(root: &Path) -> io::Result<Vec<PathBuf>> {
    if !root.is_absolute() {
        return Err(invalid("fleet root must be absolute"));
    }
    let entries = match fs::read_dir(root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error),
    };
    let mut dirs = Vec::new();
    for entry in entries.take(MAX_RUNS + 1) {
        let entry = entry?;
        let name = entry.file_name();
        let Some(name) = name.to_str().filter(|name| doxa_state::valid_session_id(name)) else { continue; };
        let path = root.join(name);
        if fs::symlink_metadata(&path).is_ok_and(|metadata| metadata.is_dir() && !metadata.file_type().is_symlink()) {
            dirs.push(path);
        }
    }
    if dirs.len() > MAX_RUNS {
        return Err(invalid("fleet root has too many runs"));
    }
    dirs.sort();
    Ok(dirs)
}

fn short(value: &str) -> String {
    value.chars().filter(|ch| !ch.is_control()).take(80).collect()
}

pub fn runs(root: &Path) -> io::Result<String> {
    let mut rows = Vec::new();
    for dir in run_dirs(root)? {
        let Ok(value) = manifest(&dir) else { continue; };
        let id = value["run_id"].as_str().unwrap_or_else(|| dir.file_name().unwrap().to_str().unwrap_or("?"));
        let state = if value["live"] == true { "LIVE" } else if value["stopped"] == true {
            "stopped"
        } else if value["quiesced"] == true { "quiesced" } else { "timed out" };
        rows.push((value["started_at"].as_str().unwrap_or("").to_owned(),
            format!("  {:<22}  {:<19}  {:>3}  {:<9}  {}", short(id),
                short(value["started_at"].as_str().unwrap_or("?")),
                value["spec"]["n"].as_u64().map_or("?".into(), |n| n.to_string()),
                state, value["ledger"]["messages"].as_u64().map_or("?".into(), |n| n.to_string()))));
    }
    rows.sort_by(|a, b| b.0.cmp(&a.0));
    let mut output = format!("fleet runs under {}\n", root.display());
    if rows.is_empty() {
        output.push_str("no runs recorded under this root");
    } else {
        output.push_str("  run                     started               n  state      ledger\n");
        output.push_str(&rows.into_iter().map(|(_, line)| line).collect::<Vec<_>>().join("\n"));
    }
    Ok(output)
}

fn resolve(root: &Path, prefix: &str) -> io::Result<PathBuf> {
    if !doxa_state::valid_session_id(prefix) {
        return Err(invalid("invalid fleet run ID"));
    }
    let dirs = run_dirs(root)?;
    if let Some(exact) = dirs.iter().find(|path| path.file_name().and_then(|name| name.to_str()) == Some(prefix)) {
        return Ok(exact.clone());
    }
    let hits: Vec<_> = dirs.into_iter()
        .filter(|path| path.file_name().and_then(|name| name.to_str()).is_some_and(|name| name.starts_with(prefix)))
        .collect();
    match hits.as_slice() {
        [run] => Ok(run.clone()),
        [] => Err(io::Error::new(io::ErrorKind::NotFound, "fleet run not found")),
        _ => Err(invalid("fleet run prefix is ambiguous")),
    }
}

pub fn status(root: &Path, prefix: &str) -> io::Result<String> {
    let run = resolve(root, prefix)?;
    let value = manifest(&run)?;
    let id = value["run_id"].as_str().unwrap_or(prefix);
    let state = if value["live"] == true { "running" } else { "finished" };
    let mut lines = vec![format!("fleet {} — {}", short(id), state),
        format!("mode {} · started {} · sessions {}", short(value["mode"].as_str().unwrap_or("symmetric")),
            short(value["started_at"].as_str().unwrap_or("?")),
            value["spec"]["sessions"].as_u64().map_or("?".into(), |n| n.to_string()))];
    if value["stopped"] == true {
        lines.push("ended on operator request".into());
    } else if value["quiesced"] == true {
        lines.push("quiesced".into());
    }
    if let Some(messages) = value["ledger"]["messages"].as_u64() {
        lines.push(format!("ledger messages {messages}"));
    }
    if let Some(slots) = value["slots"].as_array() {
        for slot in slots.iter().take(32) {
            let index = slot["index"].as_u64().map_or("?".into(), |n| n.to_string());
            lines.push(format!("  slot {} · {} · {} · {}", index,
                short(slot["role"].as_str().unwrap_or("worker")),
                short(slot["phase"].as_str().unwrap_or("?")),
                short(slot["session_id"].as_str().unwrap_or("?"))));
            let pending = slot["pending_asks"].as_array().map_or(0, Vec::len);
            if pending > 0 {
                lines.push(format!("    {pending} permission ask(s) waiting · fleet attach {} {index}", short(id)));
            }
        }
    }
    lines.push(format!("manifest {}", run.join("manifest.json").display()));
    Ok(lines.join("\n"))
}

pub fn slot_socket(root: &Path, prefix: &str, index: usize) -> io::Result<(PathBuf, String)> {
    let run = resolve(root, prefix)?;
    let value = manifest(&run)?;
    let slot = value["slots"].as_array().and_then(|slots| slots.iter().find(|slot| slot["index"].as_u64() == Some(index as u64)))
        .ok_or_else(|| invalid("fleet slot not found"))?;
    let session_id = slot["session_id"].as_str().filter(|id| doxa_state::valid_session_id(id))
        .ok_or_else(|| invalid("fleet slot has no valid session ID"))?;
    let socket = PathBuf::from(slot["socket_path"].as_str().ok_or_else(|| invalid("fleet slot has no socket"))?);
    let runtime = run.join("rt");
    let runtime_meta = fs::symlink_metadata(&runtime)?;
    if !runtime_meta.is_dir() || runtime_meta.file_type().is_symlink()
        || runtime_meta.uid() != unsafe { libc::geteuid() }
        || runtime_meta.permissions().mode() & 0o077 != 0 {
        return Err(invalid("unsafe fleet runtime directory"));
    }
    if socket.parent().and_then(|parent| parent.canonicalize().ok()) != Some(runtime.canonicalize()?) {
        return Err(invalid("fleet socket is outside run runtime"));
    }
    let metadata = fs::symlink_metadata(&socket)?;
    if !metadata.file_type().is_socket() || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o077 != 0 {
        return Err(invalid("unsafe fleet socket"));
    }
    Ok((socket, session_id.to_owned()))
}

pub struct StopReport {
    pub text: String,
    pub complete: bool,
}

fn stop_one(socket: PathBuf, expected_id: String) -> Result<&'static str, String> {
    let mut client = crate::transport::DaemonClient::connect(&socket, None)
        .map_err(|error| error.to_string())?;
    if client.hello["session_id"] != expected_id {
        return Err("daemon session identity differs from manifest".into());
    }
    let reply = client.call("stop", serde_json::Map::new())
        .map_err(|error| format!("stop acknowledgement unavailable: {error}"))?;
    if reply["ok"] != true {
        return Err("daemon refused stop request".into());
    }
    let deadline = Instant::now() + Duration::from_secs(60);
    loop {
        if Instant::now() >= deadline {
            return Err("stop accepted; daemon close not confirmed within 60 seconds".into());
        }
        match client.poll_frame(Duration::from_millis(250)) {
            Ok(Some(_)) | Ok(None) => {}
            Err(crate::transport::TransportError::Closed) => return Ok("daemon connection closed"),
            Err(error) => return Err(format!("stop accepted; daemon close unconfirmed: {error}")),
        }
    }
}

/// Request stop on each live slot that still has a socket. All readable socket
/// targets are validated before the first request; this never signals a PID.
pub fn stop(root: &Path, prefix: &str) -> io::Result<StopReport> {
    let run = resolve(root, prefix)?;
    let value = manifest(&run)?;
    if value["live"] != true {
        return Err(invalid("fleet manifest is not live"));
    }
    let slots = value["slots"].as_array().ok_or_else(|| invalid("fleet manifest has no slots"))?;
    if slots.is_empty() || slots.len() > 64 {
        return Err(invalid("fleet slot count is out of bounds"));
    }
    let mut targets = Vec::new();
    let mut missing = Vec::new();
    let mut not_spawned = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for slot in slots {
        let index = slot["index"].as_u64().and_then(|index| usize::try_from(index).ok())
            .ok_or_else(|| invalid("fleet slot index is invalid"))?;
        if !seen.insert(index) {
            return Err(invalid("fleet slot index is duplicated"));
        }
        if slot["socket_path"].as_str().is_none_or(str::is_empty) {
            if slot["pid"].is_null() && matches!(slot["phase"].as_str(), Some("pending" | "failed")) {
                not_spawned.push(index);
            } else {
                missing.push(index);
            }
            continue;
        }
        match slot_socket(root, prefix, index) {
            Ok((socket, id)) => targets.push((index, socket, id)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => missing.push(index),
            Err(error) => return Err(error),
        }
    }
    if targets.is_empty() {
        return Err(invalid("fleet has no live slot sockets to stop"));
    }
    // Concurrent requests match v1.19 teardown's per-slot fanout and keep a
    // single slow finalize from delaying the other stop requests.
    let handles: Vec<_> = targets.into_iter().map(|(index, socket, id)| {
        std::thread::spawn(move || (index, stop_one(socket, id)))
    }).collect();
    let mut lines = Vec::new();
    let mut complete = missing.is_empty();
    for index in missing {
        lines.push((index, format!("slot {index}: no live socket recorded")));
    }
    for index in not_spawned {
        lines.push((index, format!("slot {index}: was not spawned")));
    }
    for handle in handles {
        match handle.join() {
            Ok((index, Ok(state))) => lines.push((index, format!("slot {index}: {state}"))),
            Ok((index, Err(error))) => {
                complete = false;
                lines.push((index, format!("slot {index}: {}", short(&error))));
            }
            Err(_) => {
                complete = false;
                lines.push((usize::MAX, "slot worker failed".into()));
            }
        }
    }
    lines.sort_by_key(|(index, _)| *index);
    Ok(StopReport { text: format!("fleet {} stop requests\n{}", short(value["run_id"].as_str().unwrap_or(prefix)),
        lines.into_iter().map(|(_, line)| line).collect::<Vec<_>>().join("\n")), complete })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Write};
    use std::os::unix::net::UnixListener;

    #[test]
    fn reads_python_manifest_and_refuses_ambiguous_or_unsafe_attach() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let run = root.join("20260925T100000-abcd");
        fs::create_dir(&run).unwrap();
        let runtime = run.join("rt");
        fs::create_dir(&runtime).unwrap();
        fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
        let socket = runtime.join("daemon-sess-123.sock");
        let _listener = UnixListener::bind(&socket).unwrap();
        fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
        let manifest_path = run.join("manifest.json");
        let mut file = File::create(&manifest_path).unwrap();
        writeln!(file, "{}", serde_json::json!({"run_id":"20260925T100000-abcd", "live":true,
            "mode":"supervisor", "started_at":"2026-09-25T10:00:00Z",
            "spec":{"n":2,"sessions":3}, "ledger":{"messages":4},
            "slots":[{"index":0,"role":"supervisor","phase":"armed",
                "session_id":"sess-123","socket_path":socket,
                "pending_asks":[{"tool":"Bash","summary":"private operation"}]}]})).unwrap();
        fs::set_permissions(&manifest_path, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(runs(root).unwrap().contains("LIVE"));
        assert!(status(root, "20260925T100000").unwrap().contains("slot 0 · supervisor · armed"));
        assert!(status(root, "20260925T100000").unwrap().contains("1 permission ask(s) waiting"));
        assert_eq!(slot_socket(root, "20260925T100000", 0).unwrap().1, "sess-123");
        assert!(slot_socket(root, "../other", 0).is_err());
        fs::set_permissions(&socket, fs::Permissions::from_mode(0o666)).unwrap();
        assert!(slot_socket(root, "20260925T100000", 0).is_err());
        fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
        let outside = root.join("outside.sock");
        let _outside_listener = UnixListener::bind(&outside).unwrap();
        fs::set_permissions(&outside, fs::Permissions::from_mode(0o600)).unwrap();
        fs::write(&manifest_path, serde_json::json!({"slots":[{"index":0,
            "session_id":"sess-123", "socket_path":outside}]}).to_string()).unwrap();
        assert!(slot_socket(root, "20260925T100000-abcd", 0).is_err());
        fs::create_dir(root.join("20260925T100000-efgh")).unwrap();
        assert!(status(root, "20260925T100000").is_err());
        fs::set_permissions(&manifest_path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(status(root, "20260925T100000-abcd").is_err());
    }

    #[test]
    fn stop_waits_for_daemon_close_after_acknowledgement() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("daemon.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            writeln!(socket, "{}", serde_json::json!({"type":"hello", "proto":1,
                "session_id":"sess-123", "cwd":"/tmp", "engine":"fixture", "next_seq":0})).unwrap();
            let mut reader = BufReader::new(socket.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            assert!(line.contains("attach"));
            line.clear();
            reader.read_line(&mut line).unwrap();
            let call: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(call["method"], "stop");
            writeln!(socket, "{}", serde_json::json!({"type":"reply", "id":call["id"], "ok":true})).unwrap();
        });
        assert_eq!(stop_one(path, "sess-123".into()).unwrap(), "daemon connection closed");
        server.join().unwrap();
    }

    #[test]
    fn stop_preflights_all_targets_before_sending_any_request() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let run = root.join("20260925T100000-abcd");
        let runtime = run.join("rt");
        fs::create_dir_all(&runtime).unwrap();
        fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
        let first = runtime.join("daemon-first.sock");
        let first_listener = UnixListener::bind(&first).unwrap();
        first_listener.set_nonblocking(true).unwrap();
        fs::set_permissions(&first, fs::Permissions::from_mode(0o600)).unwrap();
        let second = runtime.join("daemon-second.sock");
        let _second_listener = UnixListener::bind(&second).unwrap();
        fs::set_permissions(&second, fs::Permissions::from_mode(0o666)).unwrap();
        let manifest_path = run.join("manifest.json");
        fs::write(&manifest_path, serde_json::json!({"run_id":"20260925T100000-abcd", "live":true,
            "slots":[{"index":0,"session_id":"first","socket_path":first},
                {"index":1,"session_id":"second","socket_path":second}]}).to_string()).unwrap();
        fs::set_permissions(&manifest_path, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(stop(root, "20260925T100000-abcd").is_err());
        assert_eq!(first_listener.accept().unwrap_err().kind(), io::ErrorKind::WouldBlock);
    }

    #[test]
    fn stop_requests_one_validated_fleet_slot() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let run = root.join("20260925T100000-abcd");
        let runtime = run.join("rt");
        fs::create_dir_all(&runtime).unwrap();
        fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();
        let socket = runtime.join("daemon-first.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        fs::set_permissions(&socket, fs::Permissions::from_mode(0o600)).unwrap();
        let path = run.join("manifest.json");
        fs::write(&path, serde_json::json!({"run_id":"20260925T100000-abcd", "live":true,
            "slots":[{"index":0,"session_id":"sess-123","socket_path":socket}]}).to_string()).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            writeln!(stream, "{}", serde_json::json!({"type":"hello", "proto":1,
                "session_id":"sess-123", "cwd":"/tmp", "engine":"fixture", "next_seq":0})).unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            line.clear();
            reader.read_line(&mut line).unwrap();
            let call: Value = serde_json::from_str(&line).unwrap();
            assert_eq!(call["method"], "stop");
            writeln!(stream, "{}", serde_json::json!({"type":"reply", "id":call["id"], "ok":true})).unwrap();
        });
        let report = stop(root, "20260925T100000").unwrap();
        assert!(report.complete);
        assert!(report.text.contains("slot 0: daemon connection closed"));
        server.join().unwrap();
    }
}
