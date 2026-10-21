//! Read only the rollout owned by a DOXA Codex thread. `codex exec --json`
//! omits context telemetry, while Codex persists its internal token count.
//! This is a bounded, best-effort bridge until the host uses app-server.

use std::collections::HashSet;
use std::ffi::CString;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

const MAX_DIR_ENTRIES: usize = 512;
const MAX_META_BYTES: usize = doxa_isolation::MAX_PROVIDER_REVIEW_LINE_BYTES + 1;
const MAX_NEW_BYTES: u64 = 8 * 1024 * 1024;
const BASELINE_TOKENS: u64 = 12_000; // Codex TUI's effective context reserve.

fn codex_sessions() -> Option<PathBuf> {
    let home = match doxa_isolation::active().ok()? {
        Some(scope) => scope.private_home.join("codex"),
        None => std::env::var_os("CODEX_HOME").map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".codex")))?,
    };
    Some(home.join("sessions"))
}

fn mapped_rollout(path: &Path, sessions: &Path, isolated: bool) -> PathBuf {
    if isolated {
        if let Ok(relative) = path.strip_prefix("/home/doxa/codex/sessions") { return sessions.join(relative); }
    }
    path.to_owned()
}

fn host_paths(path: &Path) -> Option<(PathBuf,PathBuf)> {
    let sessions = codex_sessions()?;
    let isolated = doxa_isolation::active().ok()?.is_some();
    let path = mapped_rollout(path, &sessions, isolated);
    Some((sessions,path))
}

fn safe_dir(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|meta| meta.is_dir() && !meta.file_type().is_symlink())
}

fn open_child(parent: &File, name: &std::ffi::OsStr, flags: libc::c_int) -> Option<File> {
    let name = CString::new(name.as_bytes()).ok()?;
    let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), flags) };
    if fd < 0 { return None; }
    Some(unsafe { File::from_raw_fd(fd) })
}

fn safe_file(path: &Path, sessions: &Path) -> Option<File> {
    let relative = path.strip_prefix(sessions).ok()?;
    let parts: Vec<_> = relative.components().collect();
    if parts.len() != 4 || parts.iter().any(|part| !matches!(part, Component::Normal(_))) {
        return None;
    }
    let mut dir = OpenOptions::new().read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(sessions).ok()?;
    for part in &parts[..3] {
        dir = open_child(&dir, part.as_os_str(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)?;
    }
    // Open relative to pinned directory handles so a replaced parent cannot
    // redirect the read. O_NONBLOCK keeps a replacement FIFO from hanging us.
    let file = open_child(&dir, parts[3].as_os_str(),
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)?;
    let meta = file.metadata().ok()?;
    if !meta.is_file() || meta.nlink() != 1 || meta.uid() != unsafe { libc::geteuid() } {
        return None;
    }
    Some(file)
}

fn matches_thread(file: &mut File, thread_id: &str) -> bool {
    let mut first = vec![0; MAX_META_BYTES];
    let Ok(n) = file.read(&mut first) else { return false; };
    header_matches_thread(&first[..n], thread_id)
}

fn header_matches_thread(bytes: &[u8], thread_id: &str) -> bool {
    let Some(end) = bytes.iter().position(|byte| *byte == b'\n') else { return false; };
    let Ok(frame) = serde_json::from_slice::<Value>(&bytes[..end]) else { return false; };
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
    true
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
                && safe_file(&path, &sessions).is_some_and(|mut file| matches_thread(&mut file, thread_id)) {
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
    if !doxa_engines::codex_driver::valid_thread_id(thread_id) { return None; }
    match doxa_isolation::snapshot_provider_file(path, 0, MAX_META_BYTES, Some(thread_id)).ok()? {
        Some(snapshot) => {
            let size = snapshot.proof["size"].as_u64()?;
            if snapshot.bytes.len() != size.min(MAX_META_BYTES as u64) as usize
                || !header_matches_thread(&snapshot.bytes, thread_id)
                || !doxa_isolation::verify_provider_snapshot(&snapshot).ok()? { return None; }
            Some(size)
        },
        None => { let (sessions,path) = host_paths(path)?; size_in(&path, thread_id, &sessions) },
    }
}

fn size_in(path: &Path, thread_id: &str, sessions: &Path) -> Option<u64> {
    if !valid_path_in(path, thread_id, sessions) { return None; }
    let mut file = safe_file(path, sessions)?;
    if !matches_thread(&mut file, thread_id) { return None; }
    file.metadata().ok().map(|meta| meta.len())
}

/// Parse only bytes appended during this successful turn. A missing or
/// oversized append is unknown, never a stale prior-turn reading.
pub(crate) fn read_since(path: &Path, thread_id: &str, before: u64) -> Option<Value> {
    if !doxa_engines::codex_driver::valid_thread_id(thread_id) { return None; }
    match doxa_isolation::snapshot_provider_file(path, before, MAX_NEW_BYTES as usize, Some(thread_id)).ok()? {
        Some(snapshot) => {
            let context = parsed_context(&snapshot.bytes, before, snapshot.proof["size"].as_u64()?, Some(thread_id))?;
            doxa_isolation::verify_provider_snapshot(&snapshot).ok()?.then_some(context)
        },
        None => { let (sessions,path) = host_paths(path)?; read_since_in(&path, thread_id, before, &sessions) },
    }
}

fn read_since_in(path: &Path, thread_id: &str, before: u64, sessions: &Path) -> Option<Value> {
    if !valid_path_in(path, thread_id, sessions) { return None; }
    let mut file = safe_file(path, sessions)?;
    if !matches_thread(&mut file, thread_id) { return None; }
    let after = file.metadata().ok()?.len();
    let appended = after.checked_sub(before)?;
    if appended == 0 || appended > MAX_NEW_BYTES { return None; }
    file.seek(SeekFrom::Start(before)).ok()?;
    let mut bytes = Vec::with_capacity(appended as usize);
    file.take(appended).read_to_end(&mut bytes).ok()?;
    parsed_context(&bytes, before, after, Some(thread_id))
}

/// Latest context occupancy is independent of cumulative spend. Resume reads a
/// bounded tail of the exact owned thread, not the byte size of its full history.
pub(crate) fn latest(path: &Path, thread_id: &str) -> Option<Value> {
    if !doxa_engines::codex_driver::valid_thread_id(thread_id) { return None; }
    match doxa_isolation::snapshot_provider_file(path, 0, MAX_META_BYTES, Some(thread_id)).ok()? {
        Some(header) => {
            if !header_matches_thread(&header.bytes, thread_id) { return None; }
            let size = header.proof["size"].as_u64()?;
            let offset = size.saturating_sub(MAX_NEW_BYTES);
            let tail = doxa_isolation::snapshot_provider_file(path, offset, MAX_NEW_BYTES as usize, Some(thread_id)).ok()??;
            if header.proof["source"] != tail.proof["source"] { return None; }
            let context = tail_context(&tail.bytes, offset, size, thread_id)?;
            doxa_isolation::verify_provider_snapshot(&tail).ok()?.then_some(context)
        },
        None => { let (sessions,path) = host_paths(path)?; latest_in(&path,thread_id,&sessions) },
    }
}

fn latest_in(path: &Path, thread_id: &str, sessions: &Path) -> Option<Value> {
    if !valid_path_in(path,thread_id,sessions) || fs::canonicalize(sessions).ok()? != sessions { return None; }
    let mut file = safe_file(path,sessions)?;
    let before = file.metadata().ok()?;
    if !matches_thread(&mut file,thread_id) { return None; }
    let offset = before.len().saturating_sub(MAX_NEW_BYTES);
    file.seek(SeekFrom::Start(offset)).ok()?;
    let mut bytes = Vec::new();
    file.by_ref().take(MAX_NEW_BYTES).read_to_end(&mut bytes).ok()?;
    let facts = |m:&fs::Metadata| (m.dev(),m.ino(),m.len(),m.ctime(),m.ctime_nsec(),m.mtime(),m.mtime_nsec(),m.nlink(),m.uid());
    if facts(&before) != facts(&file.metadata().ok()?) || facts(&before) != facts(&fs::symlink_metadata(path).ok()?) { return None; }
    tail_context(&bytes,offset,before.len(),thread_id)
}

fn tail_context(bytes:&[u8], offset:u64, size:u64, thread_id:&str) -> Option<Value> {
    if bytes.len() as u64 != size.checked_sub(offset)? { return None; }
    // A tail may begin in an image/tool record. Never parse its partial prefix.
    let skip = if offset == 0 { 0 } else { bytes.iter().position(|b| *b == b'\n')? + 1 };
    parsed_context(&bytes[skip..],offset+skip as u64,size,Some(thread_id))
}

#[cfg(test)]
fn appended_context(bytes: &[u8], before: u64, after: u64) -> Option<Value> {
    parsed_context(bytes,before,after,None)
}

fn parsed_context(bytes: &[u8], before: u64, after: u64, thread_id: Option<&str>) -> Option<Value> {
    let appended = after.checked_sub(before)?;
    if appended == 0 || appended > MAX_NEW_BYTES || bytes.len() != appended as usize { return None; }
    let mut latest = None;
    let mut model = None;
    for line in bytes.split(|byte| *byte == b'\n') {
        let Ok(frame) = serde_json::from_slice::<Value>(line) else { continue; };
        if frame["type"] == "turn_context" {
            model = frame["payload"]["model"].as_str().filter(|model| !model.is_empty()
                && model.len() <= 128 && !model.chars().any(char::is_control)).map(str::to_owned);
            continue;
        }
        if frame["type"] == "compacted" {
            latest = None;
            continue;
        }
        if frame["type"] == "token_usage_record" {
            let payload = &frame["payload"];
            // Modern records carry per-response, per-turn and lifetime totals.
            // Only per-response usage measures the current context occupancy.
            if thread_id.is_none() || payload["thread_id"].as_str() != thread_id { return None; }
            let tokens = payload["usage"]["total_tokens"].as_u64().filter(|n| *n > 0)?;
            latest = Some(json!({"raw_context_tokens":tokens,"context_model":model,
                "ctx_source":"codex_rollout_token_usage_record"}));
            continue;
        }
        if frame["type"] != "event_msg" || frame["payload"]["type"] != "token_count" { continue; }
        let info = &frame["payload"]["info"];
        let (Some(tokens), Some(limit)) = (info["last_token_usage"]["total_tokens"].as_u64(),
            info["model_context_window"].as_u64()) else { continue; };
        if limit <= BASELINE_TOKENS { continue; }
        let used = tokens.saturating_sub(BASELINE_TOKENS);
        let effective = limit - BASELINE_TOKENS;
        latest = Some(json!({"ctx_tokens":used,"ctx_max_tokens":effective,
            "raw_context_tokens":tokens,"raw_context_window":limit,
            "context_model":model,
            "ctx_percentage":(used as f64 / effective as f64 * 100.0).clamp(0.0,100.0),
            "ctx_source":"codex_rollout_token_count"}));
    }
    latest
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn modern_context_reads_owned_long_history_tail_without_using_lifetime_spend() {
        use std::io::Write;
        let temporary = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(temporary.path()).unwrap();
        let thread = "01a0da9a-ae05-77f2-8d51-9f3440bc6cfb";
        let sessions = root.join("codex/sessions"); let day = sessions.join("2026/10/21");
        fs::create_dir_all(&day).unwrap();
        let path = day.join(format!("rollout-2026-10-21T00-00-00-{thread}.jsonl"));
        let mut file = File::create(&path).unwrap();
        writeln!(file,"{}",json!({"type":"session_meta","payload":{"id":thread,"padding":"x".repeat(2*1024*1024)}})).unwrap();
        let padding = json!({"type":"world_state","payload":{"padding":"x".repeat(512*1024)}}).to_string();
        for _ in 0..24 { writeln!(file,"{padding}").unwrap(); }
        writeln!(file,"{}",json!({"type":"compacted"})).unwrap();
        writeln!(file,"{}",json!({"type":"turn_context","payload":{"model":"gpt-6-astra"}})).unwrap();
        let count = json!({"type":"token_usage_record","payload":{"thread_id":thread,
            "usage":{"total_tokens":23947},"turn_token_usage":{"total_tokens":157249},"thread_token_usage":{"total_tokens":230957}}});
        writeln!(file,"{count}").unwrap(); file.sync_all().unwrap();
        let actual = latest_in(&path,thread,&sessions).unwrap();
        assert_eq!(actual["raw_context_tokens"],23947);
        assert_eq!(actual["context_model"],"gpt-6-astra");
        assert!(actual.get("raw_context_window").is_none(),"modern usage did not attest an allocation");
        assert!(read_since_in(&path,thread,0,&sessions).is_none(),"a long log must not look like a complete bounded append");
        let worker = Path::new("/home/doxa/codex/sessions/2026/10/21").join(path.file_name().unwrap());
        assert_eq!(mapped_rollout(&worker,&sessions,true),path);
        assert_eq!(mapped_rollout(&worker,&sessions,false),worker);
        let foreign = Path::new("/home/doxa-other/codex/sessions/2026/10/21/log.jsonl");
        assert_eq!(mapped_rollout(foreign,&sessions,true),foreign);
        writeln!(file,"{}",json!({"type":"compacted"})).unwrap(); file.sync_all().unwrap();
        assert!(latest_in(&path,thread,&sessions).is_none(),"compaction invalidates pre-compaction occupancy");
        writeln!(file,"{count}").unwrap(); file.sync_all().unwrap();
        assert_eq!(latest_in(&path,thread,&sessions).unwrap()["raw_context_tokens"],23947);
        writeln!(file,"{}",json!({"type":"token_usage_record","payload":{"thread_id":"foreign","usage":{"total_tokens":1}}})).unwrap();
        file.sync_all().unwrap();
        assert!(latest_in(&path,thread,&sessions).is_none(),"foreign usage must not expose a stale smaller count");
    }

    #[test]
    fn partial_tail_and_modern_usage_without_thread_proof_never_supply_context() {
        let record = json!({"type":"token_usage_record","payload":{"thread_id":"thread-1","usage":{"total_tokens":1234}}}).to_string()+"\n";
        assert!(parsed_context(record.as_bytes(),0,record.len() as u64,None).is_none());
        let partial = format!("partial record\n{record}"); let offset = 99;
        assert_eq!(tail_context(partial.as_bytes(),offset,offset+partial.len() as u64,"thread-1").unwrap()["raw_context_tokens"],1234);
        assert!(tail_context(record.as_bytes(),offset,offset+record.len() as u64,"thread-1").is_none());
    }

    #[test]
    fn bounded_provider_ranges_cannot_reuse_truncated_or_prior_context() {
        let bytes = serde_json::to_vec(&json!({"type":"event_msg","payload":{"type":"token_count",
            "info":{"last_token_usage":{"total_tokens":19000},"model_context_window":258400}}})).unwrap();
        let before = 123;
        let after = before + bytes.len() as u64;
        assert_eq!(appended_context(&bytes, before, after).unwrap()["ctx_tokens"], 7000);
        assert!(appended_context(&bytes[..bytes.len()-1], before, after).is_none());
        assert!(appended_context(&bytes, before, before).is_none());
        assert!(appended_context(&bytes, before, before-1).is_none());
        assert!(appended_context(&bytes, before, before+MAX_NEW_BYTES+1).is_none());
        assert!(appended_context(&bytes, u64::MAX, 0).is_none());
        let mut compacted = bytes.clone(); compacted.extend_from_slice(b"\n{\"type\":\"compacted\"}\n");
        assert!(appended_context(&compacted, before, before+compacted.len() as u64).is_none());
        assert!(!header_matches_thread(&bytes, "thread-1"));
        assert!(!header_matches_thread(b"{\"type\":\"session_meta\",\"payload\":{\"id\":\"thread-2\"}}\n", "thread-1"));
        assert!(header_matches_thread(b"{\"type\":\"session_meta\",\"payload\":{\"id\":\"thread-1\"}}\n", "thread-1"));
    }

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

    #[test]
    fn compaction_invalidates_previous_context_until_new_count() {
        use std::io::Write;
        let root = tempfile::tempdir().unwrap();
        let thread = "01a0da9a-ae05-77f2-8d51-9f3440bc6cfb";
        let sessions = root.path().join("sessions");
        let day = sessions.join("2026/09/26");
        fs::create_dir_all(&day).unwrap();
        let path = day.join(format!("rollout-2026-09-26T00-06-05-{thread}.jsonl"));
        fs::write(&path, format!("{{\"type\":\"session_meta\",\"payload\":{{\"id\":\"{thread}\"}}}}\n")).unwrap();
        let before = size_in(&path, thread, &sessions).unwrap();
        let count = |tokens| json!({"type":"event_msg","payload":{"type":"token_count",
            "info":{"last_token_usage":{"total_tokens":tokens},"model_context_window":258400}}});
        let mut file = OpenOptions::new().append(true).open(&path).unwrap();
        writeln!(file, "{}", count(216667)).unwrap();
        writeln!(file, "{{\"type\":\"compacted\",\"payload\":{{}}}}").unwrap();
        assert!(read_since_in(&path, thread, before, &sessions).is_none());
        writeln!(file, "{}", count(11941)).unwrap();
        assert_eq!(read_since_in(&path, thread, before, &sessions).unwrap()["ctx_tokens"], 0);
    }

    #[test]
    fn symlinked_parent_cannot_redirect_rollout_read() {
        let root = tempfile::tempdir().unwrap();
        let thread = "01a0da9a-ae05-77f2-8d51-9f3440bc6cfb";
        let sessions = root.path().join("sessions");
        let outside = root.path().join("outside");
        fs::create_dir_all(&outside).unwrap();
        fs::create_dir_all(sessions.join("2026/09")).unwrap();
        let filename = format!("rollout-2026-09-26T00-06-05-{thread}.jsonl");
        fs::write(outside.join(&filename), format!("{{\"type\":\"session_meta\",\"payload\":{{\"id\":\"{thread}\"}}}}\n")).unwrap();
        std::os::unix::fs::symlink(&outside, sessions.join("2026/09/26")).unwrap();
        assert!(size_in(&sessions.join("2026/09/26").join(filename), thread, &sessions).is_none());
    }
}
