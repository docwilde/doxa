//! Destructive session commands resolve hints again before each stop request.
use crate::{discovery::{self, Session}, transport::DaemonClient};
use std::{collections::HashSet, io, path::Path, sync::mpsc::{self, Receiver}, time::{Duration, Instant}};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Action { Kill(String), KillDetached }
impl Action {
    pub fn parse(args: &[&str]) -> io::Result<Self> {
        match args {
            ["kill", prefix] if !prefix.is_empty() && prefix.len() <= 200 && !prefix.chars().any(char::is_control) => Ok(Self::Kill((*prefix).into())),
            ["kill-detached" | "kill-all-detached"] => Ok(Self::KillDetached),
            _ => Err(io::Error::new(io::ErrorKind::InvalidInput, "usage: /sessions [kill <prefix> | kill-detached]")),
        }
    }
}

pub fn targets(entries: Vec<Session>, action: &Action, attached: &HashSet<String>) -> io::Result<Vec<Session>> {
    if *action == Action::KillDetached {
        return Ok(entries.into_iter().filter(|entry| !attached.contains(&entry.id)).collect());
    }
    let Action::Kill(prefix) = action else { unreachable!() };
    // Exact ID wins. Then ID prefix wins over title prefix. Refuse ambiguity.
    let matches = |predicate: &dyn Fn(&Session) -> bool| entries.iter().filter(|entry| predicate(entry)).cloned().collect::<Vec<_>>();
    let mut selected = matches(&|entry| entry.id == *prefix);
    if selected.is_empty() { selected = matches(&|entry| entry.id.starts_with(prefix)); }
    if selected.is_empty() { selected = matches(&|entry| entry.title.starts_with(prefix)); }
    if selected.len() > 1 { return Err(io::Error::new(io::ErrorKind::InvalidInput, "sessions: ambiguous prefix; use the full session ID")); }
    Ok(selected)
}

#[derive(Debug)]
pub struct Report { pub stopped: Vec<String>, pub requested: Vec<String>, pub failed: Vec<String>, pub error: Option<String> }
impl Report {
    pub fn text(&self) -> String {
        if let Some(error) = &self.error { return format!("sessions: {error}"); }
        let mut lines = Vec::new();
        if !self.stopped.is_empty() { lines.push(format!("stopped: {}", self.stopped.iter().map(|id| &id[..id.len().min(8)]).collect::<Vec<_>>().join(", "))); }
        if !self.requested.is_empty() { lines.push(format!("stop accepted; teardown unconfirmed: {}", self.requested.iter().map(|id| &id[..id.len().min(8)]).collect::<Vec<_>>().join(", "))); }
        if !self.failed.is_empty() { lines.push(format!("could not stop: {}", self.failed.iter().map(|id| &id[..id.len().min(8)]).collect::<Vec<_>>().join(", "))); }
        lines.join(" · ")
    }
}

pub fn start(action: Action, attached: HashSet<String>) -> io::Result<Receiver<Report>> {
    let runtime = discovery::runtime_dir()?;
    let (tx, rx) = mpsc::sync_channel(1);
    std::thread::Builder::new().name("session-stop".into()).spawn(move || { let _ = tx.send(run(&runtime, action, attached)); })?;
    Ok(rx)
}
fn run(runtime: &Path, action: Action, attached: HashSet<String>) -> Report {
    let mut report = Report { stopped: Vec::new(), requested: Vec::new(), failed: Vec::new(), error: None };
    let entries = match discovery::sessions_in(runtime).and_then(|entries| targets(entries, &action, &attached)) {
        Ok(entries) => entries,
        Err(error) => { report.error = Some(error.to_string()); return report; }
    };
    if entries.is_empty() {
        report.error = Some(if action == Action::KillDetached { "nothing detached to kill" } else { "nothing matched" }.into());
        return report;
    }
    // Bound total work even with a hostile listener or a very large registry.
    let deadline = Instant::now() + Duration::from_secs(60);
    for entry in entries {
        match stop_verified(runtime, &entry, deadline) {
            Ok(StopOutcome::Completed) => report.stopped.push(entry.id),
            Ok(StopOutcome::Requested) => report.requested.push(entry.id),
            Err(_) => report.failed.push(entry.id),
        }
    }
    report
}

#[derive(Debug, PartialEq, Eq)]
pub enum StopOutcome { Completed, Requested }
pub fn stop_verified(runtime: &Path, selected: &Session, deadline: Instant) -> io::Result<StopOutcome> {
    stop_verified_method(runtime, selected, deadline, "stop", true)
}
pub fn stop_idle_verified(runtime: &Path, selected: &Session, deadline: Instant) -> io::Result<()> {
    // Update restart owns its separate all-target teardown check.
    stop_verified_method(runtime, selected, deadline, "stop_if_idle", false).map(|_| ())
}
fn stop_verified_method(runtime: &Path, selected: &Session, deadline: Instant, method: &str, retirement: bool) -> io::Result<StopOutcome> {
    use std::os::unix::fs::MetadataExt;
    let fresh = discovery::sessions_in(runtime)?.into_iter().find(|entry|
        entry.id == selected.id && entry.socket == selected.socket)
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "session identity changed before stop"))?;
    let pid = fresh.socket.file_stem().and_then(|name| name.to_str()).and_then(|name| name.rsplit('-').next())
        .and_then(|pid| pid.parse::<i32>().ok()).filter(|pid| *pid > 0)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid daemon process identity"))?;
    let metadata = std::fs::symlink_metadata(&fresh.socket)?;
    let identity = Some((metadata.dev(), metadata.ino()));
    let mut client = DaemonClient::connect_until(&fresh.socket, Some(u64::MAX - 1), deadline).map_err(io::Error::other)?;
    client.verify_peer(pid).map_err(io::Error::other)?;
    if client.hello["session_id"] != fresh.id { return Err(io::Error::new(io::ErrorKind::PermissionDenied, "session identity changed during stop")); }
    if Instant::now() >= deadline { return Err(io::Error::new(io::ErrorKind::TimedOut, "session stop deadline reached")); }
    let reply = client.call_until(method, serde_json::Map::new(), deadline).map_err(io::Error::other)?;
    if reply["ok"] != true { return Err(io::Error::other("daemon refused stop request")); }
    if !retirement { return Ok(StopOutcome::Requested); }
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() { return Ok(StopOutcome::Requested); }
        match client.poll_frame(remaining.min(Duration::from_millis(100))) {
            Ok(Some(_)) | Ok(None) => {}
            Err(crate::transport::TransportError::Closed) => {
                return Ok(if crate::fleet_view::wait_retirement(&fresh.socket, &fresh.id, identity, deadline).is_ok() {
                    StopOutcome::Completed
                } else { StopOutcome::Requested });
            }
            Err(_) => return Ok(StopOutcome::Requested),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn session(id: &str, title: &str, clients: u64) -> Session { Session {
        id: id.into(), title: title.into(), socket: Default::default(), scope_key: String::new(), clients: Some(clients), started_at: String::new() } }
    #[test]
    fn exact_id_and_id_prefix_win_and_ambiguity_never_stops_multiple() {
        let rows = vec![session("abc123", "named", 0), session("abc456", "abc123", 0)];
        assert_eq!(targets(rows.clone(), &Action::Kill("abc123".into()), &HashSet::new()).unwrap()[0].id, "abc123");
        assert!(targets(rows.clone(), &Action::Kill("abc".into()), &HashSet::new()).is_err());
        assert!(targets(rows.clone(), &Action::Kill("wrong".into()), &HashSet::new()).unwrap().is_empty());
        assert_eq!(targets(rows, &Action::Kill("named".into()), &HashSet::new()).unwrap()[0].id, "abc123");
    }
    #[test]
    fn detached_is_absent_from_this_windows_tabs_regardless_of_client_count() {
        let rows = vec![session("attached", "attached", 0), session("other-window", "other", 3), session("detached", "detached", 0)];
        let targets = targets(rows, &Action::KillDetached, &HashSet::from(["attached".into()])).unwrap();
        assert_eq!(targets.iter().map(|row| row.id.as_str()).collect::<Vec<_>>(), ["other-window", "detached"]);
    }
    #[test]
    fn malformed_forms_and_extra_targets_are_refused() {
        for args in [vec!["kill"], vec!["kill", "one", "two"], vec!["kill-detached", "extra"], vec!["unknown"]] { assert!(Action::parse(&args).is_err()); }
        assert_eq!(Action::parse(&["kill-all-detached"]).unwrap(), Action::KillDetached);
    }
    #[cfg(target_os = "linux")]
    #[test]
    fn verified_stop_rejects_peer_pid_hello_changes_and_refusal_before_success() {
        use std::io::{BufRead, BufReader, Write};
        use std::os::unix::{fs::PermissionsExt, net::UnixListener};
        use time::{OffsetDateTime, format_description::well_known::Rfc3339};
        for mode in ["wrong-pid", "wrong-hello", "refused", "pending", "ok", "idle-refused", "idle-ok"] {
            let dir = tempfile::tempdir().unwrap();
            std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
            let registry = dir.path().join("registry");
            std::fs::create_dir(&registry).unwrap();
            std::fs::set_permissions(&registry, std::fs::Permissions::from_mode(0o700)).unwrap();
            let mut other = std::process::Command::new("sleep").arg("30").spawn().unwrap();
            let pid = if mode == "wrong-pid" { other.id() } else { std::process::id() };
            let socket = dir.path().join(format!("daemon-target-{pid}.sock"));
            let listener = UnixListener::bind(&socket).unwrap();
            std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o600)).unwrap();
            let path = registry.join("target.json");
            std::fs::write(&path, serde_json::to_vec(&serde_json::json!({"session_id":"target","pid":pid,"cwd":"/repo","heartbeat_at":OffsetDateTime::now_utc().format(&Rfc3339).unwrap(),"started_at":"2026-01-01T00:00:00Z","title":"target","daemon_socket":socket})).unwrap()).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
            let retired_socket = socket.clone();
            let retired_registry = path.clone();
            let server = std::thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
                writeln!(stream, "{}", serde_json::json!({"type":"hello","proto":1,"session_id":if mode == "wrong-hello" { "other" } else { "target" },"cwd":"/repo","engine":"fixture","model":null,"next_seq":0})).unwrap();
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut line = String::new(); reader.read_line(&mut line).unwrap();
                assert_eq!(serde_json::from_str::<serde_json::Value>(&line).unwrap()["type"], "attach");
                line.clear();
                if matches!(mode, "wrong-pid" | "wrong-hello") {
                    assert_eq!(reader.read_line(&mut line).unwrap(), 0, "identity failure must not send stop");
                } else {
                    reader.read_line(&mut line).unwrap();
                    let request: serde_json::Value = serde_json::from_str(&line).unwrap();
                    assert_eq!(request["method"], if mode.starts_with("idle-") { "stop_if_idle" } else { "stop" });
                    writeln!(stream, "{}", serde_json::json!({"type":"reply","id":request["id"],"ok":matches!(mode, "ok" | "pending" | "idle-ok")})).unwrap();
                    if mode == "ok" {
                        drop(reader); drop(stream); drop(listener);
                        std::fs::remove_file(retired_socket).unwrap();
                        std::fs::remove_file(retired_registry).unwrap();
                    } else if mode == "pending" { std::thread::sleep(Duration::from_millis(150)); }
                }
            });
            let selected = discovery::sessions_in(dir.path()).unwrap().pop().unwrap();
            let deadline = Instant::now() + if mode == "pending" { Duration::from_millis(100) } else { Duration::from_secs(2) };
            let outcome = if mode.starts_with("idle-") { stop_idle_verified(dir.path(), &selected, deadline).map(|_| StopOutcome::Requested) }
                else { stop_verified(dir.path(), &selected, deadline) };
            assert_eq!(outcome.is_ok(), matches!(mode, "ok" | "pending" | "idle-ok"));
            if let Ok(outcome) = outcome {
                assert_eq!(outcome, if mode == "ok" { StopOutcome::Completed } else { StopOutcome::Requested });
            }
            server.join().unwrap(); let _ = other.kill(); let _ = other.wait();
        }
    }

}
