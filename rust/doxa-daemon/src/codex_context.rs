//! Read only the rollout owned by a DOXA Codex thread. `codex exec --json`
//! omits context telemetry, while Codex persists its internal token count.
//! This is a bounded, best-effort bridge until the host uses app-server.

use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

const MAX_DIR_ENTRIES: usize = 512;
const MAX_META_BYTES: usize = 64 * 1024;
const MAX_NEW_BYTES: u64 = 8 * 1024 * 1024;
const BASELINE_TOKENS: u64 = 12_000; // Codex TUI's effective context reserve.

fn codex_sessions() -> Option<PathBuf> {
    let home = std::env::var_os("CODEX_HOME").map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".codex")))?;
    Some(home.join("sessions"))
}

fn safe_dir(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|meta| meta.is_dir() && !meta.file_type().is_symlink())
}

fn safe_file(path: &Path) -> Option<File> {
    // A pathname can be replaced between directory scanning and open. In
    // particular, opening a FIFO for read would wait indefinitely without
    // O_NONBLOCK, before we have a chance to reject its file type.
    let file = OpenOptions::new().read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
        .open(path).ok()?;
    let meta = file.metadata().ok()?;
    if !meta.is_file() || meta.nlink() != 1 || meta.uid() != unsafe { libc::geteuid() } {
        return None;
    }
    Some(file)
}

fn matches_thread(file: &mut File, thread_id: &str) -> bool {
    let mut first = vec![0; MAX_META_BYTES];
    let Ok(n) = file.read(&mut first) else { return false; };
    let Some(end) = first[..n].iter().position(|byte| *byte == b'\n') else { return false; };
    let Ok(frame) = serde_json::from_slice::<Value>(&first[..end]) else { return false; };
    frame["type"] == "session_meta" && frame["payload"]["id"] == thread_id
}

fn valid_path_in(path: &Path, thread_id: &str, sessions: &Path) -> bool {
    let Ok(relative) = path.strip_prefix(&sessions) else { return false; };
    let components: Vec<_> = relative.components().collect();
    if components.len() != 4 || components.iter().any(|part| !matches!(part, Component::Normal(_))) {
        return false;
    }
    if components[0].as_os_str().to_string_lossy().len() != 4
        || components[1..3].iter().any(|part| part.as_os_str().to_string_lossy().len() != 2) {
        return false;
    }
    let filename = components[3].as_os_str().to_string_lossy();
    if !filename.starts_with("rollout-") || !filename.ends_with(&format!("-{thread_id}.jsonl")) {
        return false;
    }
    safe_dir(&sessions) && safe_dir(&sessions.join(components[0].as_os_str()))
        && safe_dir(&sessions.join(components[0].as_os_str()).join(components[1].as_os_str()))
        && safe_dir(path.parent().unwrap_or(&sessions))
}

pub(crate) fn find(thread_id: &str, started: SystemTime) -> Option<PathBuf> {
    if !doxa_engines::codex_driver::valid_thread_id(thread_id) { return None; }
    let sessions = codex_sessions()?;
    if !safe_dir(&sessions) { return None; }
    let mut found = None;
    let mut visited = HashSet::new();
    // Rollout dates use local time. UTC and local midnight can disagree; only
    // inspect these three adjacent days, never the whole session tree.
    for shift in [-1_i64, 0, 1] {
        let seconds = started.duration_since(UNIX_EPOCH).ok()?.as_secs() as i64 + shift * 86_400;
        let raw = seconds as libc::time_t;
        let mut time = unsafe { std::mem::zeroed::<libc::tm>() };
        if unsafe { libc::localtime_r(&raw, &mut time) }.is_null() { continue; }
        let day = sessions.join(format!("{:04}", time.tm_year + 1900))
            .join(format!("{:02}", time.tm_mon + 1)).join(format!("{:02}", time.tm_mday));
        if !visited.insert(day.clone()) || !safe_dir(&day) { continue; }
        let mut entries = fs::read_dir(&day).ok()?;
        for _ in 0..MAX_DIR_ENTRIES {
            let Some(entry) = entries.next() else { break; };
            let entry = entry.ok()?;
            let path = entry.path();
            if valid_path_in(&path, thread_id, &sessions)
                && safe_file(&path).is_some_and(|mut file| matches_thread(&mut file, thread_id)) {
                if found.is_some() { return None; }
                found = Some(path);
            }
        }
        // An incomplete filename scan cannot establish uniqueness.
        if entries.next().is_some() { return None; }
    }
    found
}

pub(crate) fn size(path: &Path, thread_id: &str) -> Option<u64> {
    size_in(path, thread_id, &codex_sessions()?)
}

fn size_in(path: &Path, thread_id: &str, sessions: &Path) -> Option<u64> {
    if !valid_path_in(path, thread_id, sessions) { return None; }
    let mut file = safe_file(path)?;
    if !matches_thread(&mut file, thread_id) { return None; }
    file.metadata().ok().map(|meta| meta.len())
}

/// Parse only bytes appended during this successful turn. A missing or
/// oversized append is unknown, never a stale prior-turn reading.
pub(crate) fn read_since(path: &Path, thread_id: &str, before: u64) -> Option<Value> {
    read_since_in(path, thread_id, before, &codex_sessions()?)
}

fn read_since_in(path: &Path, thread_id: &str, before: u64, sessions: &Path) -> Option<Value> {
    if !valid_path_in(path, thread_id, sessions) { return None; }
    let mut file = safe_file(path)?;
    if !matches_thread(&mut file, thread_id) { return None; }
    let after = file.metadata().ok()?.len();
    let appended = after.checked_sub(before)?;
    if appended == 0 || appended > MAX_NEW_BYTES { return None; }
    file.seek(SeekFrom::Start(before)).ok()?;
    let mut bytes = Vec::with_capacity(appended as usize);
    file.take(appended).read_to_end(&mut bytes).ok()?;
    if bytes.len() != appended as usize { return None; }
    let mut latest = None;
    for line in bytes.split(|byte| *byte == b'\n') {
        let Ok(frame) = serde_json::from_slice::<Value>(line) else { continue; };
        if frame["type"] != "event_msg" || frame["payload"]["type"] != "token_count" { continue; }
        let info = &frame["payload"]["info"];
        let (Some(tokens), Some(limit)) = (info["last_token_usage"]["total_tokens"].as_u64(),
            info["model_context_window"].as_u64()) else { continue; };
        if limit <= BASELINE_TOKENS { continue; }
        let used = tokens.saturating_sub(BASELINE_TOKENS);
        let effective = limit - BASELINE_TOKENS;
        latest = Some(json!({"ctx_tokens":used,"ctx_max_tokens":effective,
            "ctx_percentage":(used as f64 / effective as f64 * 100.0).clamp(0.0,100.0),
            "ctx_source":"codex_rollout_token_count"}));
    }
    latest
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codex_0156_rollout_context_uses_last_not_cumulative_and_is_turn_bounded() {
        let root = tempfile::tempdir().unwrap();
        let thread = "01a0da9a-ae05-77f2-8d51-9f3440bc6cfb";
        let day = root.path().join("sessions/2026/09/26");
        fs::create_dir_all(&day).unwrap();
        let path = day.join(format!("rollout-2026-09-26T00-06-05-{thread}.jsonl"));
        fs::write(&path, format!("{{\"type\":\"session_meta\",\"payload\":{{\"id\":\"{thread}\"}}}}\n")).unwrap();
        let sessions = root.path().join("sessions");
        let before = size_in(&path, thread, &sessions).unwrap();
        assert!(read_since_in(&path, thread, before, &sessions).is_none());
        // Usage block projected from a real codex-cli 0.156.1 rollout. The
        // unrelated account rate-limit payload is intentionally omitted.
        let frame = json!({"type":"event_msg","payload":{"type":"token_count","info":{
            "total_token_usage":{"input_tokens":18528,"cached_input_tokens":12544,
                "cache_write_input_tokens":0,"output_tokens":6,"reasoning_output_tokens":0,
                "total_tokens":18534},
            "last_token_usage":{"input_tokens":18528,"cached_input_tokens":12544,
                "cache_write_input_tokens":0,"output_tokens":6,"reasoning_output_tokens":0,
                "total_tokens":18534},"model_context_window":258400}}});
        use std::io::Write;
        writeln!(OpenOptions::new().append(true).open(&path).unwrap(), "{frame}").unwrap();
        let context = read_since_in(&path, thread, before, &sessions).unwrap();
        assert_eq!(context["ctx_tokens"], 6534);
        assert_eq!(context["ctx_max_tokens"], 246400);
        assert!(context["ctx_percentage"].as_f64().unwrap() < 3.0);
        let next = size_in(&path, thread, &sessions).unwrap();
        let newer = json!({"type":"event_msg","payload":{"type":"token_count","info":{
            "total_token_usage":{"total_tokens":200000},
            "last_token_usage":{"total_tokens":19000},"model_context_window":258400}}});
        writeln!(OpenOptions::new().append(true).open(&path).unwrap(), "{newer}").unwrap();
        assert_eq!(read_since_in(&path, thread, next, &sessions).unwrap()["ctx_tokens"], 7000);
        assert!(read_since_in(&path, "other", before, &sessions).is_none());
        assert!(read_since_in(&path, thread, before + 10_000, &sessions).is_none());
        let link = day.join(format!("rollout-link-{thread}.jsonl"));
        std::os::unix::fs::symlink(&path, &link).unwrap();
        assert!(read_since_in(&link, thread, 0, &sessions).is_none());
        let fifo = day.join(format!("rollout-fifo-{thread}.jsonl"));
        let name = std::ffi::CString::new(fifo.as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        assert!(read_since_in(&fifo, thread, 0, &sessions).is_none());
    }
}
