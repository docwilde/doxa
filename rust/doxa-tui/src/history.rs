//! Render the daemon's persisted JSONL conversation for the early Rust UI.
//! This view is bounded; the JSONL file remains the complete record.

use serde_json::Value;
use crate::transport::TranscriptSnapshot;
use std::ffi::{CStr, OsStr, OsString};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::ffi::OsStrExt;
use std::os::fd::{AsRawFd, FromRawFd};
use std::path::{Path, PathBuf};
use std::time::SystemTime;
use crate::launch::{Engine, LaunchOptions};
use std::time::Duration;

const MAX_PROJECTS: usize = 128;
const MAX_FILES: usize = 2048;
const MAX_OFFLINE: usize = 64;
const MAX_FILE_BYTES: u64 = 512 * 1024;
const MAX_CODEX_METADATA_BYTES: u64 = 64 * 1024;

#[derive(Clone, Debug)]
pub struct OfflineSession {
    pub id: String,
    pub project: String,
    pub markdown: String,
    /// Cwd from the first DOXA user record. It is a hint until resume preflight.
    pub cwd: Option<PathBuf>,
}

fn projects_dir() -> Option<PathBuf> {
    projects_dir_from(
        std::env::var_os("LORE_PROJECTS_DIR").filter(|v| !v.is_empty()).map(PathBuf::from),
        std::env::var_os("HOME").map(PathBuf::from),
    )
}

fn projects_dir_from(configured: Option<PathBuf>, home: Option<PathBuf>) -> Option<PathBuf> {
    configured.or_else(|| home.map(|home| home.join(".claude/projects")))
}

fn owned_dir(file: &File, uid: u32) -> bool {
    file.metadata().is_ok_and(|meta| meta.is_dir() && meta.uid() == uid)
}

fn open_at(parent: &File, name: &OsStr, flags: i32) -> Option<File> {
    let name = std::ffi::CString::new(name.as_bytes()).ok()?;
    let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), flags | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
    if fd < 0 { None } else { Some(unsafe { File::from_raw_fd(fd) }) }
}

fn entry_present(parent: &File, name: &OsStr) -> bool {
    let Ok(name) = std::ffi::CString::new(name.as_bytes()) else { return true; };
    let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
    unsafe { libc::fstatat(parent.as_raw_fd(), name.as_ptr(), stat.as_mut_ptr(), libc::AT_SYMLINK_NOFOLLOW) == 0 }
}

fn names_in(open_dir: &File, limit: usize, accept: impl Fn(&OsStr) -> bool) -> Vec<OsString> {
    // fdopendir owns its descriptor, so duplicate the pinned directory FD.
    // Names returned by readdir are copied before the next call overwrites
    // its buffer. No pathname is reopened during enumeration.
    let fd = unsafe { libc::dup(open_dir.as_raw_fd()) };
    if fd < 0 { return Vec::new(); }
    if unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } < 0 {
        unsafe { libc::close(fd); }
        return Vec::new();
    }
    let dir = unsafe { libc::fdopendir(fd) };
    if dir.is_null() {
        unsafe { libc::close(fd); }
        return Vec::new();
    }
    let mut names = Vec::new();
    // Ignore unrelated names without spending the transcript budget, but cap
    // directory work if a project contains a very large number of junk files.
    let mut inspected = 0;
    while names.len() < limit && inspected < limit.saturating_add(MAX_FILES) {
        let entry = unsafe { libc::readdir(dir) };
        if entry.is_null() { break; }
        let raw = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
        if raw == b"." || raw == b".." { continue; }
        inspected += 1;
        let name = OsStr::from_bytes(raw);
        if accept(name) { names.push(name.to_os_string()); }
    }
    unsafe { libc::closedir(dir); }
    names
}

fn valid_transcript_name(name: &OsStr) -> bool {
    let path = Path::new(name);
    path.extension().is_some_and(|ext| ext == "jsonl")
        && path.file_stem().and_then(|stem| stem.to_str()).is_some_and(crate::discovery::valid_id)
}

fn valid_codex_thread_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 128 && !id.starts_with('-')
        && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

type Candidate = (Option<SystemTime>, String, String, File);

fn candidate_order(a: &Candidate, b: &Candidate) -> std::cmp::Ordering {
    b.0.cmp(&a.0).then_with(|| a.2.cmp(&b.2)).then_with(|| a.1.cmp(&b.1))
}

fn read_offline(mut file: File, uid: u32) -> Option<String> {
    let meta = file.metadata().ok()?;
    if !meta.is_file() || meta.uid() != uid { return None; }
    let start = meta.len().saturating_sub(MAX_FILE_BYTES);
    let starts_on_line = if start > 0 {
        file.seek(SeekFrom::Start(start - 1)).ok()?;
        let mut preceding = [0];
        file.read_exact(&mut preceding).ok()?;
        preceding[0] == b'\n'
    } else { true };
    file.seek(SeekFrom::Start(start)).ok()?;
    let mut bytes = Vec::new();
    file.take(meta.len() - start).read_to_end(&mut bytes).ok()?;
    if !starts_on_line {
        if let Some(cut) = bytes.iter().position(|byte| *byte == b'\n') {
            bytes.drain(..=cut);
        } else {
            bytes.clear();
        }
    }
    Some(render(&TranscriptSnapshot { bytes, earlier_bytes_omitted: start > 0 }))
}

fn recorded_cwd(file: &mut File) -> Option<PathBuf> {
    file.seek(SeekFrom::Start(0)).ok()?;
    let mut head = Vec::new();
    file.take(64 * 1024).read_to_end(&mut head).ok()?;
    for line in head.split(|byte| *byte == b'\n').take(128) {
        let Ok(record) = serde_json::from_slice::<Value>(line) else { continue };
        if record["type"] != "user" { continue; }
        let Some(cwd) = record["cwd"].as_str() else { continue; };
        if cwd.len() <= 4096 && !cwd.contains('\0') && Path::new(cwd).is_absolute() {
            return Some(PathBuf::from(cwd));
        }
    }
    None
}

/// Scan only LORE transcript filenames and load bounded tails of the newest files.
/// Call on a worker thread; this reads no paths supplied by the history UI.
pub fn discover() -> Vec<OfflineSession> {
    let Some(root) = projects_dir() else { return Vec::new(); };
    discover_in(&root, None, None)
}

pub fn discover_prefix(prefix: &str) -> Vec<OfflineSession> {
    if !crate::discovery::valid_id(prefix) { return Vec::new(); }
    let Some(root) = projects_dir() else { return Vec::new(); };
    discover_in(&root, Some(prefix), None)
}

/// Search bounded tails across the complete scanned inventory, including
/// archives older than the 64 recents. LORE's full-text index remains broader.
pub fn discover_query(query: &str) -> Vec<OfflineSession> {
    let query = query.trim();
    if query.is_empty() || query.len() > 200 || query.chars().any(char::is_control) { return Vec::new(); }
    let Some(root) = projects_dir() else { return Vec::new(); };
    discover_in(&root, None, Some(query))
}

fn tail_matches(file: &mut File, query: &str) -> bool {
    let Ok(len) = file.metadata().map(|meta| meta.len()) else { return false; };
    if file.seek(SeekFrom::Start(len.saturating_sub(16 * 1024))).is_err() { return false; }
    let mut tail = Vec::new();
    if file.take(16 * 1024).read_to_end(&mut tail).is_err() { return false; }
    String::from_utf8_lossy(&tail).to_lowercase().contains(&query.to_lowercase())
}

fn discover_in(root: &Path, prefix: Option<&str>, query: Option<&str>) -> Vec<OfflineSession> {
    let uid = unsafe { libc::geteuid() };
    let Ok(root) = OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC).open(root) else { return Vec::new(); };
    if !owned_dir(&root, uid) { return Vec::new(); }
    let projects = names_in(&root, MAX_PROJECTS, |_| true);
    let mut candidates = Vec::new();
    let mut visited = 0;
    for project in projects {
        let Some(dir) = open_at(&root, &project, libc::O_RDONLY | libc::O_DIRECTORY) else { continue; };
        if !owned_dir(&dir, uid) { continue; }
        let files = names_in(&dir, MAX_FILES.saturating_sub(visited), valid_transcript_name);
        for name in files {
            visited += 1;
            let path = Path::new(&name);
            let id = path.file_stem().and_then(|stem| stem.to_str()).unwrap();
            if prefix.is_some_and(|prefix| !id.starts_with(prefix)) { continue; }
            let Some(mut open_file) = open_at(&dir, &name, libc::O_RDONLY | libc::O_NONBLOCK) else { continue; };
            let Ok(meta) = open_file.metadata() else { continue; };
            if !meta.is_file() || meta.uid() != uid { continue; }
            if query.is_some_and(|query| !id.to_lowercase().contains(&query.to_lowercase()) && !tail_matches(&mut open_file, query)) {
                continue;
            }
            let stamp = meta.modified().ok();
            candidates.push((stamp, id.to_owned(), project.to_string_lossy().into_owned(), open_file));
            if candidates.len() > MAX_OFFLINE {
                candidates.sort_by(candidate_order);
                candidates.pop();
            }
        }
        if visited == MAX_FILES { break; }
    }
    candidates.sort_by(candidate_order);
    candidates.into_iter().filter_map(|(_, id, project, mut file)| {
        let cwd = recorded_cwd(&mut file);
        let markdown = read_offline(file, uid)?;
        Some(OfflineSession { id, project, markdown, cwd })
    }).collect()
}

/// Verify the session's recorded cwd maps back to this transcript directory,
/// then require the original engine's replay artefact.
pub fn resume_plan(entry: &OfflineSession, python: &Path) -> Result<LaunchOptions, &'static str> {
    let root = projects_dir().ok_or("transcript root unavailable")?;
    let claude = claude_store_root().ok_or("DOXA home unavailable")?;
    resume_plan_in(entry, python, &root, &claude)
}

fn claude_store_root() -> Option<PathBuf> {
    let home = std::env::var_os("DOXA_HOME").filter(|value| !value.is_empty())
        .map(PathBuf::from).or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".doxa")));
    home.filter(|home| home.is_absolute()).map(|home| home.join("claude-cli/projects"))
}

fn claude_history_present(root: &Path, id: &str, uid: u32) -> bool {
    let Ok(root) = OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW).open(root) else { return false; };
    if !owned_dir(&root, uid) { return false; }
    let name = format!("{id}.jsonl");
    for project in names_in(&root, MAX_PROJECTS, |_| true) {
        let Some(dir) = open_at(&root, &project, libc::O_RDONLY | libc::O_DIRECTORY) else { continue; };
        if !owned_dir(&dir, uid) { continue; }
        let Some(file) = open_at(&dir, OsStr::new(&name), libc::O_RDONLY | libc::O_NONBLOCK) else { continue; };
        if file.metadata().is_ok_and(|meta| meta.is_file() && meta.uid() == uid && meta.len() > 0) { return true; }
    }
    false
}

fn ready_resume_cwd(cwd: &Path, id: &str, missing: bool, python: &Path,
    expected_root: &Path, expected_slug: &str) -> Result<PathBuf, &'static str> {
    let _recovered = if missing {
        Some(doxa_worktrees::recover_missing(cwd, id)
            .map_err(|reason| {
                if reason.contains("pinned base commit") {
                    "saved checkout lacks pinned Git metadata; recovery refused"
                } else if reason.contains("registered to another checkout")
                    || reason.contains("checkout path already exists")
                    || reason.contains("checkout path was occupied") {
                    "session branch or path belongs to another checkout; recovery refused"
                } else {
                    "managed checkout could not be recovered; deleted uncommitted files cannot be restored from Git"
                }
            })?)
    } else { None };
    let canonical = cwd.canonicalize().map_err(|_| "session directory is gone")?;
    if canonical != cwd || !canonical.is_dir() { return Err("session directory changed during verification"); }
    let mut lore = doxa_lore::LoreClient::spawn(python, Duration::from_secs(3))
        .map_err(|_| "LORE unavailable for resume verification")?;
    let (root, slug) = lore.transcript_identity(&canonical.to_string_lossy())
        .map_err(|_| "cannot verify session project")?;
    if root != expected_root || slug != expected_slug {
        return Err("session directory does not match transcript project");
    }
    Ok(canonical)
}

fn resume_plan_in(entry: &OfflineSession, python: &Path, expected_root: &Path, claude_root: &Path) -> Result<LaunchOptions, &'static str> {
    if !crate::discovery::valid_id(&entry.id) { return Err("invalid session ID"); }
    let cwd = entry.cwd.as_ref().ok_or("session directory was not recorded")?;
    let (cwd, missing) = match cwd.canonicalize() {
        Ok(path) if path.is_dir() => (path, false),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound && cwd.is_absolute()
            && fs::symlink_metadata(cwd).is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound) => (cwd.clone(), true),
        _ => return Err("session directory is gone"),
    };
    // LORE derives a managed checkout's project identity through Git. With
    // the checkout absent, that lookup can only identify the missing path
    // itself. Verify the owned saved files under the expected root first;
    // ready_resume_cwd repeats LORE identity after recovery.
    let root = if missing {
        expected_root.to_path_buf()
    } else {
        let mut lore = doxa_lore::LoreClient::spawn(python, Duration::from_secs(3))
            .map_err(|_| "LORE unavailable for resume verification")?;
        let (root, slug) = lore.transcript_identity(&cwd.to_string_lossy())
            .map_err(|_| "cannot verify session project")?;
        if root != expected_root || slug != entry.project {
            return Err("session directory does not match transcript project");
        }
        root
    };
    let uid = unsafe { libc::geteuid() };
    let root = OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW).open(&root)
        .map_err(|_| "transcript root unavailable")?;
    if !owned_dir(&root, uid) { return Err("unsafe transcript root"); }
    let dir = open_at(&root, OsStr::new(&entry.project), libc::O_RDONLY | libc::O_DIRECTORY)
        .ok_or("project history unavailable")?;
    if !owned_dir(&dir, uid) { return Err("unsafe project history"); }
    let transcript_name = format!("{}.jsonl", entry.id);
    let transcript = open_at(&dir, OsStr::new(&transcript_name), libc::O_RDONLY | libc::O_NONBLOCK)
        .ok_or("session transcript is no longer available")?;
    if !transcript.metadata().is_ok_and(|meta| meta.is_file() && meta.uid() == uid && meta.nlink() == 1 && meta.len() > 0) {
        return Err("session transcript is unsafe or empty");
    }
    let codex_name = format!("{}.codex.json", entry.id);
    if entry_present(&dir, OsStr::new(&codex_name))
        && open_at(&dir, OsStr::new(&codex_name), libc::O_RDONLY | libc::O_NONBLOCK).is_none() {
        return Err("unsafe Codex thread record");
    }
    if let Some(mut record) = open_at(&dir, OsStr::new(&codex_name), libc::O_RDONLY | libc::O_NONBLOCK) {
        let vendor_name = format!("{}.messages.json", entry.id);
        if entry_present(&dir, OsStr::new(&vendor_name))
            || claude_history_present(claude_root, &entry.id, uid) {
            return Err("ambiguous session engine state");
        }
        let meta = record.metadata().map_err(|_| "unreadable Codex thread record")?;
        if !meta.is_file() || meta.uid() != uid || meta.nlink() != 1
            || meta.len() == 0 || meta.len() > MAX_CODEX_METADATA_BYTES {
            return Err("unsafe Codex thread record");
        }
        let mut bytes = Vec::new();
        record.read_to_end(&mut bytes).map_err(|_| "unreadable Codex thread record")?;
        let state: Value = serde_json::from_slice(&bytes).map_err(|_| "invalid Codex thread record")?;
        if state["session_id"].as_str() != Some(&entry.id)
            || state["cwd"].as_str() != cwd.to_str()
            || state.get("turn_incomplete") != Some(&Value::Bool(false))
            || !state["thread_id"].as_str().is_some_and(valid_codex_thread_id) {
            return Err("Codex thread record does not match this session");
        }
        let model = match state.get("model") {
            None | Some(Value::Null) => None,
            Some(Value::String(model)) if !model.is_empty() && model.len() <= 128
                && !model.chars().any(char::is_control) => Some(model.clone()),
            _ => return Err("invalid Codex model in thread record"),
        };
        let cwd = ready_resume_cwd(&cwd, &entry.id, missing, python, expected_root, &entry.project)?;
        return Ok(LaunchOptions { engine: Engine::Codex, cwd: Some(cwd), model,
            resume: Some(entry.id.clone()), ..LaunchOptions::default() });
    }
    let name = format!("{}.messages.json", entry.id);
    let Some(mut saved) = open_at(&dir, OsStr::new(&name), libc::O_RDONLY | libc::O_NONBLOCK) else {
        if claude_history_present(claude_root, &entry.id, uid) {
            let cwd = ready_resume_cwd(&cwd, &entry.id, missing, python, expected_root, &entry.project)?;
            return Ok(LaunchOptions { engine: Engine::Claude, cwd: Some(cwd),
                resume: Some(entry.id.clone()), ..LaunchOptions::default() });
        }
        return Err("no Claude CLI or vendor replay state for this session");
    };
    let meta = saved.metadata().map_err(|_| "unreadable replay state")?;
    if !meta.is_file() || meta.uid() != uid || meta.len() > 8 * 1024 * 1024 {
        return Err("unsafe replay state");
    }
    let mut bytes = Vec::new();
    saved.read_to_end(&mut bytes).map_err(|_| "unreadable replay state")?;
    let state: Value = serde_json::from_slice(&bytes).map_err(|_| "invalid replay state")?;
    let engine = match state["engine"].as_str() {
        Some("deepseek") => Engine::DeepSeek,
        Some("glm") => Engine::Glm,
        _ => return Err("unsupported or unknown saved engine"),
    };
    if state.get("session_id").is_some_and(|id| id.as_str() != Some(&entry.id)) {
        return Err("replay state belongs to another session");
    }
    let model = state["model"].as_str().filter(|m| !m.is_empty() && m.len() <= 128 && !m.chars().any(char::is_control))
        .ok_or("saved vendor model is unknown")?;
    if !state["messages"].is_array() { return Err("invalid replay state"); }
    let cwd = ready_resume_cwd(&cwd, &entry.id, missing, python, expected_root, &entry.project)?;
    Ok(LaunchOptions { engine, cwd: Some(cwd), model: Some(model.to_owned()),
        resume: Some(entry.id.clone()), ..LaunchOptions::default() })
}

const MAX_TURNS: usize = 40;
const MAX_TEXT_CHARS: usize = 20_000;
const MAX_VIEW_BYTES: usize = 480 * 1024;

#[derive(Default)]
struct Turn { prompt: String, answer: String, tools: Vec<String>, tool_names: std::collections::HashMap<String, String> }

fn tool_detail(value: &Value) -> String {
    if value.is_null() { return String::new(); }
    let raw = value.as_str().map(str::to_owned).unwrap_or_else(|| value.to_string());
    let clean = crate::markdown::sanitize(&raw).replace(['\n', '\r'], " ");
    let mut chars = clean.chars();
    let detail: String = chars.by_ref().take(400).collect();
    let mut escaped = String::with_capacity(detail.len());
    for ch in detail.chars() {
        if matches!(ch, '\\' | '`' | '*' | '_' | '{' | '}' | '[' | ']' | '(' | ')' | '#' | '+' | '-' | '.' | '!' | '>' | '|' | '~') {
            escaped.push('\\');
        }
        escaped.push(ch);
    }
    if chars.next().is_some() { escaped.push('…'); }
    escaped
}

fn append_text(target: &mut String, text: &str) {
    if text.is_empty() { return; }
    if !target.is_empty() { target.push_str("\n\n"); }
    target.push_str(text);
}

pub fn render(snapshot: &TranscriptSnapshot) -> String {
    let mut turns: Vec<Turn> = Vec::new();
    for line in snapshot.bytes.split(|byte| *byte == b'\n') {
        let Ok(record) = serde_json::from_slice::<Value>(line) else { continue };
        let kind = record["type"].as_str().unwrap_or("");
        let content = &record["message"]["content"];
        if kind == "user" {
            let mut prompt = String::new();
            if let Some(text) = content.as_str() {
                append_text(&mut prompt, text);
            } else if let Some(blocks) = content.as_array() {
                for block in blocks {
                    if block["type"] == "text" {
                        append_text(&mut prompt, block["text"].as_str().unwrap_or(""));
                    }
                }
            }
            if !prompt.is_empty() { turns.push(Turn { prompt, ..Turn::default() }); }
            else if let Some(blocks) = content.as_array() {
                if let Some(turn) = turns.last_mut() {
                    for block in blocks.iter().filter(|block| block["type"] == "tool_result") {
                        let id = block["tool_use_id"].as_str().unwrap_or("");
                        let name = turn.tool_names.get(id).map(String::as_str).unwrap_or("Tool");
                        let outcome = if block["is_error"] == true { "failed" } else { "finished" };
                        let detail = tool_detail(&block["content"]);
                        turn.tools.push(format!("Tool: {name} {outcome}{}", if detail.is_empty() { String::new() } else { format!(" · {detail}") }));
                    }
                }
            }
            continue;
        }
        if kind != "assistant" { continue; }
        let Some(blocks) = content.as_array() else { continue };
        if turns.is_empty() { turns.push(Turn::default()); }
        let turn = turns.last_mut().unwrap();
        for block in blocks {
            match block["type"].as_str() {
                Some("text") => append_text(&mut turn.answer, block["text"].as_str().unwrap_or("")),
                Some("tool_use") => {
                    let name = tool_detail(&block["name"]);
                    let name = if name.is_empty() { "Tool".to_owned() } else { name };
                    if let Some(id) = block["id"].as_str() { turn.tool_names.insert(id.to_owned(), name.clone()); }
                    let detail = tool_detail(&block["input"]);
                    turn.tools.push(format!("Tool: {name} started{}", if detail.is_empty() { String::new() } else { format!(" · {detail}") }));
                }
                _ => {}
            }
        }
    }
    let omitted_turns = turns.len().saturating_sub(MAX_TURNS);
    let mut out = String::new();
    if snapshot.earlier_bytes_omitted || omitted_turns > 0 {
        out.push_str("[Earlier transcript omitted from this view; the session JSONL retains it.]\n\n");
    }
    for turn in turns.into_iter().skip(omitted_turns) {
        if !turn.prompt.is_empty() {
            out.push_str("**You:**\n\n");
            out.push_str(&turn.prompt);
            out.push_str("\n\n");
        }
        if !turn.answer.is_empty() || !turn.tools.is_empty() {
            out.push_str("**Assistant:**\n\n");
        }
        if !turn.answer.is_empty() {
            let shortened: String = turn.answer.chars().take(MAX_TEXT_CHARS).collect();
            out.push_str(&shortened);
            if shortened.len() < turn.answer.len() { out.push_str("\n[Assistant text shortened in this view]"); }
            out.push_str("\n\n");
        }
        for tool in turn.tools.iter().take(60) {
            out.push_str(&format!("{tool}\n\n"));
        }
        if turn.tools.len() > 60 { out.push_str("[Additional tools omitted from this view]\n\n"); }
    }
    if out.len() > MAX_VIEW_BYTES {
        let mut start = out.len() - MAX_VIEW_BYTES;
        while !out.is_char_boundary(start) { start += 1; }
        out = format!("[Earlier transcript omitted from this view; the session JSONL retains it.]\n\n{}", &out[start..]);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::process::Command;

    fn fake_lore(dir: &Path, root: &Path) -> PathBuf {
        let script = dir.join("fake-lore");
        fs::write(&script, format!(r#"#!/usr/bin/env python3
import json, sys
print(json.dumps({{'type':'hello','proto':1,'capabilities':['scrub','snapshot','transcript_identity']}}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    print(json.dumps({{'type':'reply','id':req['id'],'ok':True,'value':{{'projects_dir':{},'slug':'project'}}}}), flush=True)
"#, serde_json::to_string(&root.to_string_lossy()).unwrap())).unwrap();
        let mut perms = fs::metadata(&script).unwrap().permissions();
        perms.set_mode(0o700);
        fs::set_permissions(&script, perms).unwrap();
        script
    }

    #[test]
    fn transcript_root_matches_lore_projects_configuration() {
        assert_eq!(projects_dir_from(Some(PathBuf::from("/configured/projects")), Some(PathBuf::from("/home/me"))),
            Some(PathBuf::from("/configured/projects")));
        assert_eq!(projects_dir_from(None, Some(PathBuf::from("/home/me"))),
            Some(PathBuf::from("/home/me/.claude/projects")));
        assert_eq!(projects_dir_from(None, None), None);
    }
    #[test]
    fn restores_prompts_and_assistant_text_without_tool_result_turns() {
        let lines = concat!(
            "{\"type\":\"user\",\"message\":{\"content\":\"first?\"}}\n",
            "{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"yes\"},{\"type\":\"text\",\"text\":\"indeed\"},{\"type\":\"tool_use\",\"name\":\"Search\"}]}}\n",
            "{\"type\":\"user\",\"message\":{\"content\":[{\"type\":\"tool_result\",\"content\":\"found\"}]}}\n",
            "{\"type\":\"user\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"second?\"},{\"type\":\"text\",\"text\":\"more detail\"}]}}\n",
        );
        let rendered = render(&TranscriptSnapshot { bytes: lines.as_bytes().to_vec(), earlier_bytes_omitted: false });
        assert!(rendered.contains("first?"));
        assert!(rendered.contains("yes\n\nindeed"));
        assert!(rendered.contains("Tool: Search started"));
        assert!(rendered.contains("Tool: Tool finished · found"));
        assert!(rendered.contains("second?\n\nmore detail"));
        assert_eq!(rendered.matches("**You:**").count(), 2);
    }

    #[test]
    fn restored_tool_call_and_result_keep_bounded_matching_details() {
        let input = "x".repeat(1000);
        let lines = format!("{{\"type\":\"user\",\"message\":{{\"content\":\"question\"}}}}\n{{\"type\":\"assistant\",\"message\":{{\"content\":[{{\"type\":\"tool_use\",\"id\":\"call-1\",\"name\":\"Read\",\"input\":{{\"path\":\"{input}\"}}}}]}}}}\n{{\"type\":\"user\",\"message\":{{\"content\":[{{\"type\":\"tool_result\",\"tool_use_id\":\"call-1\",\"content\":\"found\"}}]}}}}\n");
        let rendered = render(&TranscriptSnapshot { bytes: lines.into_bytes(), earlier_bytes_omitted: false });
        assert!(rendered.contains("Tool: Read started ·"));
        assert!(rendered.contains("Tool: Read finished · found"));
        assert!(!rendered.contains(&input));
        assert!(rendered.contains('…'));
    }

    #[test]
    fn bounded_tail_keeps_a_complete_first_line() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("session.jsonl");
        let first = b"discard\n";
        let second = b"{\"type\":\"user\",\"message\":{\"content\":\"retained\"}}\n";
        let mut bytes = first.to_vec();
        bytes.extend_from_slice(second);
        bytes.resize(first.len() + MAX_FILE_BYTES as usize, b'\n');
        fs::write(&path, bytes).unwrap();
        let rendered = read_offline(File::open(path).unwrap(), unsafe { libc::geteuid() }).unwrap();
        assert!(rendered.contains("retained"));
        assert!(rendered.contains("Earlier transcript omitted"));
    }

    #[test]
    fn bounded_tail_without_newline_still_shows_omission() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("session.jsonl");
        fs::write(&path, vec![b'x'; MAX_FILE_BYTES as usize + 1]).unwrap();
        let rendered = read_offline(File::open(path).unwrap(), unsafe { libc::geteuid() }).unwrap();
        assert!(rendered.contains("Earlier transcript omitted"));
    }

    #[test]
    fn filename_budget_counts_only_valid_transcript_names() {
        let temp = tempfile::tempdir().unwrap();
        let dir = OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY).open(temp.path()).unwrap();
        fs::write(temp.path().join("junk.txt"), b"").unwrap();
        fs::write(temp.path().join("invalid_id.jsonl"), b"").unwrap();
        fs::write(temp.path().join("valid-1.jsonl"), b"").unwrap();
        assert_eq!(names_in(&dir, 1, valid_transcript_name), vec![OsString::from("valid-1.jsonl")]);
    }

    #[test]
    fn equal_mtime_candidates_have_stable_eviction_order() {
        let file = tempfile::tempfile().unwrap();
        let stamp = Some(SystemTime::UNIX_EPOCH);
        let mut candidates = vec![
            (stamp, "z".to_owned(), "project".to_owned(), file.try_clone().unwrap()),
            (stamp, "a".to_owned(), "project".to_owned(), file),
        ];
        candidates.sort_by(candidate_order);
        assert_eq!(candidates[0].1, "a");
    }

    #[test]
    fn discovers_owned_transcripts_and_rejects_symlinked_files() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("projects");
        let project = root.join("project");
        fs::create_dir_all(&project).unwrap();
        let valid = project.join("offline-1.jsonl");
        fs::write(&valid, b"{\"type\":\"user\",\"message\":{\"content\":\"saved question\"}}\n").unwrap();
        symlink(&valid, project.join("offline-2.jsonl")).unwrap();
        let found = discover_in(&root, None, None);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].id, "offline-1");
        assert!(found[0].markdown.contains("saved question"));
    }

    #[test]
    fn vendor_resume_requires_matching_project_and_owned_replay_envelope() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("projects");
        fs::create_dir_all(root.join("project")).unwrap();
        let cwd = temp.path().join("checkout");
        fs::create_dir(&cwd).unwrap();
        let script = fake_lore(temp.path(), &root);
        let entry = OfflineSession { id: "saved-1".into(), project: "project".into(),
            markdown: String::new(), cwd: Some(cwd.clone()) };
        fs::write(root.join("project/saved-1.jsonl"), b"saved transcript\n").unwrap();
        let replay = root.join("project/saved-1.messages.json");
        fs::write(&replay, br#"{"engine":"deepseek","session_id":"saved-1","model":"deepseek-chat","messages":[]}"#).unwrap();
        let plan = resume_plan_in(&entry, &script, &root, &temp.path().join("cli-projects")).unwrap();
        assert_eq!(plan.engine, Engine::DeepSeek);
        assert_eq!(plan.cwd, Some(cwd));
        assert_eq!(plan.resume.as_deref(), Some("saved-1"));
        let mut wrong = entry.clone();
        wrong.project = "another-project".into();
        assert!(resume_plan_in(&wrong, &script, &root, &temp.path().join("cli-projects")).is_err());
        fs::remove_file(&replay).unwrap();
        symlink(temp.path().join("outside"), &replay).unwrap();
        assert!(resume_plan_in(&entry, &script, &root, &temp.path().join("cli-projects")).is_err());
    }

    #[test]
    fn claude_resume_requires_isolated_cli_history_and_codex_ambiguity_is_rejected() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("projects");
        fs::create_dir_all(root.join("project")).unwrap();
        let cwd = temp.path().join("checkout");
        fs::create_dir(&cwd).unwrap();
        let cli = temp.path().join("cli-projects");
        fs::create_dir_all(cli.join("encoded-cwd")).unwrap();
        let script = fake_lore(temp.path(), &root);
        let entry = OfflineSession { id: "saved-1".into(), project: "project".into(),
            markdown: String::new(), cwd: Some(cwd) };
        fs::write(root.join("project/saved-1.jsonl"), b"saved transcript\n").unwrap();
        assert!(resume_plan_in(&entry, &script, &root, &cli).is_err());
        fs::write(cli.join("encoded-cwd/saved-1.jsonl"), b"saved CLI history\n").unwrap();
        let plan = resume_plan_in(&entry, &script, &root, &cli).unwrap();
        assert_eq!(plan.engine, Engine::Claude);
        fs::write(root.join("project/saved-1.codex.json"), b"{}").unwrap();
        assert!(resume_plan_in(&entry, &script, &root, &cli).unwrap_err().contains("ambiguous"));
    }

    #[test]
    fn codex_resume_requires_matching_owned_complete_thread_record() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("projects");
        fs::create_dir_all(root.join("project")).unwrap();
        let cwd = temp.path().join("checkout");
        fs::create_dir(&cwd).unwrap();
        let script = fake_lore(temp.path(), &root);
        let entry = OfflineSession { id: "saved-1".into(), project: "project".into(),
            markdown: String::new(), cwd: Some(cwd.clone()) };
        fs::write(root.join("project/saved-1.jsonl"), b"saved transcript\n").unwrap();
        let record = root.join("project/saved-1.codex.json");
        let state = serde_json::json!({"thread_id":"thread-123", "session_id":"saved-1",
            "cwd":cwd, "model":"gpt-test", "turn_incomplete":false});
        fs::write(&record, state.to_string()).unwrap();
        let plan = resume_plan_in(&entry, &script, &root, &temp.path().join("cli-projects")).unwrap();
        assert_eq!(plan.engine, Engine::Codex);
        assert_eq!(plan.resume.as_deref(), Some("saved-1"));
        assert_eq!(plan.model.as_deref(), Some("gpt-test"));
        for bad in [
            serde_json::json!({"thread_id":"-unsafe", "session_id":"saved-1", "cwd":cwd}),
            serde_json::json!({"thread_id":"thread-123", "session_id":"other", "cwd":cwd}),
            serde_json::json!({"thread_id":"thread-123", "session_id":"saved-1", "cwd":cwd}),
            serde_json::json!({"thread_id":"thread-123", "session_id":"saved-1", "cwd":cwd, "turn_incomplete":true}),
        ] {
            fs::write(&record, bad.to_string()).unwrap();
            assert!(resume_plan_in(&entry, &script, &root, &temp.path().join("cli-projects")).is_err());
        }
        fs::remove_file(&record).unwrap();
        symlink(temp.path().join("outside"), &record).unwrap();
        assert!(resume_plan_in(&entry, &script, &root, &temp.path().join("cli-projects")).is_err());
    }

    #[test]
    fn missing_checkout_does_not_trigger_recovery_for_invalid_replay_state() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("projects");
        fs::create_dir_all(root.join("project")).unwrap();
        let cwd = temp.path().join("missing-checkout");
        let script = fake_lore(temp.path(), &root);
        let entry = OfflineSession { id: "saved-1".into(), project: "project".into(),
            markdown: String::new(), cwd: Some(cwd.clone()) };
        fs::write(root.join("project/saved-1.jsonl"), b"saved transcript\n").unwrap();
        fs::write(root.join("project/saved-1.codex.json"),
            serde_json::json!({"thread_id":"thread-123", "session_id":"other",
                "cwd":cwd, "turn_incomplete":false}).to_string()).unwrap();
        assert_eq!(resume_plan_in(&entry, &script, &root, &temp.path().join("cli-projects"))
            .unwrap_err(), "Codex thread record does not match this session");
        assert!(!cwd.exists());
    }

    #[test]
    fn missing_managed_checkout_recovers_after_replay_verification() {
        let output = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "history::tests::missing_managed_checkout_recovery_child", "--nocapture"])
            .env("DOXA_RECOVERY_TEST_CHILD", "1").output().unwrap();
        assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stdout));
    }

    #[test]
    fn missing_managed_checkout_recovery_child() {
        if std::env::var("DOXA_RECOVERY_TEST_CHILD").as_deref() != Ok("1") { return; }
        let temp = tempfile::tempdir().unwrap();
        let main = temp.path().join("repo");
        let home = temp.path().join("home");
        let root = temp.path().join("projects");
        fs::create_dir(&main).unwrap();
        fs::create_dir_all(root.join("project")).unwrap();
        let git = |args: &[&str]| {
            let output = Command::new("git").args(args).current_dir(&main).output().unwrap();
            assert!(output.status.success(), "git {:?}: {}", args, String::from_utf8_lossy(&output.stderr));
            String::from_utf8(output.stdout).unwrap().trim().to_owned()
        };
        git(&["init", "-q", "-b", "main"]);
        fs::write(main.join("README"), "seed\n").unwrap();
        git(&["add", "README"]);
        git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: seed"]);
        let oid = git(&["rev-parse", "HEAD"]);
        git(&["branch", "doxa/saved123", "main"]);
        let worktrees = home.join("worktrees");
        let meta = worktrees.join(".meta");
        fs::create_dir_all(&meta).unwrap();
        fs::set_permissions(&worktrees, fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(&meta, fs::Permissions::from_mode(0o700)).unwrap();
        let cwd = worktrees.join("repo-saved123");
        let sidecar = meta.join("repo-saved123.json");
        fs::write(&sidecar, serde_json::json!({"main_root":main,"branch":"doxa/saved123",
            "base_ref":"main","base_oid":oid,"session_id":"saved123-session"}).to_string()).unwrap();
        fs::set_permissions(&sidecar, fs::Permissions::from_mode(0o600)).unwrap();
        std::env::set_var("DOXA_HOME", &home);
        let script = temp.path().join("fake-lore");
        fs::write(&script, format!(r#"#!/usr/bin/env python3
import json, pathlib, sys
print(json.dumps({{'type':'hello','proto':1,'capabilities':['scrub','snapshot','transcript_identity']}}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    slug = 'project' if pathlib.Path(req['cwd']).is_dir() else 'wrong-missing-slug'
    print(json.dumps({{'type':'reply','id':req['id'],'ok':True,
        'value':{{'projects_dir':{},'slug':slug}}}}), flush=True)
"#, serde_json::to_string(&root.to_string_lossy()).unwrap())).unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
        let transcript = root.join("project/saved123-session.jsonl");
        let record = root.join("project/saved123-session.codex.json");
        fs::write(&transcript, b"saved transcript\n").unwrap();
        let entry = OfflineSession { id: "saved123-session".into(), project: "project".into(),
            markdown: String::new(), cwd: Some(cwd.clone()) };
        fs::write(&record, serde_json::json!({"thread_id":"thread-123",
            "session_id":"other", "cwd":cwd, "turn_incomplete":false}).to_string()).unwrap();
        assert!(resume_plan_in(&entry, &script, &root, &temp.path().join("claude"))
            .unwrap_err().contains("thread record does not match"));
        assert!(!cwd.exists(), "invalid replay state reconstructed the checkout");
        fs::write(&record, serde_json::json!({"thread_id":"thread-123",
            "session_id":"saved123-session", "cwd":cwd, "turn_incomplete":false}).to_string()).unwrap();
        let plan = resume_plan_in(&entry, &script, &root, &temp.path().join("claude")).unwrap();
        assert_eq!(plan.cwd.as_deref(), Some(cwd.as_path()));
        assert_eq!(plan.engine, Engine::Codex);
        assert_eq!(fs::read_to_string(cwd.join("README")).unwrap(), "seed\n");
    }

    #[test]
    fn history_records_original_cwd_for_resume() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("projects");
        fs::create_dir_all(root.join("project")).unwrap();
        fs::write(root.join("project/saved-1.jsonl"),
            b"{\"type\":\"user\",\"cwd\":\"/tmp/original\",\"message\":{\"content\":\"needle\"}}\n").unwrap();
        let found = discover_in(&root, Some("saved"), None);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].cwd.as_deref(), Some(Path::new("/tmp/original")));
        assert!(discover_in(&root, Some("different"), None).is_empty());
    }

    #[test]
    fn search_reaches_archives_outside_recent_window() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("projects");
        fs::create_dir_all(root.join("project")).unwrap();
        fs::write(root.join("project/z-archive.jsonl"),
            b"{\"type\":\"user\",\"message\":{\"content\":\"rare needle\"}}\n").unwrap();
        for index in 0..70 {
            fs::write(root.join(format!("project/recent-{index:02}.jsonl")),
                b"{\"type\":\"user\",\"message\":{\"content\":\"ordinary\"}}\n").unwrap();
        }
        assert!(!discover_in(&root, None, None).iter().any(|entry| entry.id == "z-archive"));
        let hits = discover_in(&root, None, Some("needle"));
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, "z-archive");
    }

    #[test]
    fn opened_transcript_survives_path_replacement_without_reading_target() {
        let temp = tempfile::tempdir().unwrap();
        let dir = OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW).open(temp.path()).unwrap();
        let original = temp.path().join("saved.jsonl");
        let other = temp.path().join("other.jsonl");
        fs::write(&original, b"{\"type\":\"user\",\"message\":{\"content\":\"original\"}}\n").unwrap();
        fs::write(&other, b"{\"type\":\"user\",\"message\":{\"content\":\"replacement\"}}\n").unwrap();
        let file = open_at(&dir, OsStr::new("saved.jsonl"), libc::O_RDONLY).unwrap();
        fs::remove_file(&original).unwrap();
        symlink(&other, &original).unwrap();
        let rendered = read_offline(file, unsafe { libc::geteuid() }).unwrap();
        assert!(rendered.contains("original"));
        assert!(!rendered.contains("replacement"));
        assert!(open_at(&dir, OsStr::new("saved.jsonl"), libc::O_RDONLY).is_none());
    }

    #[test]
    fn directory_enumeration_uses_opened_directory_after_path_replacement() {
        let temp = tempfile::tempdir().unwrap();
        let original = temp.path().join("project");
        let moved = temp.path().join("moved");
        let replacement = temp.path().join("replacement");
        fs::create_dir(&original).unwrap();
        fs::create_dir(&replacement).unwrap();
        fs::write(original.join("original.jsonl"), b"").unwrap();
        fs::write(replacement.join("replacement.jsonl"), b"").unwrap();
        let open_dir = OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW).open(&original).unwrap();
        fs::rename(&original, &moved).unwrap();
        symlink(&replacement, &original).unwrap();
        let names = names_in(&open_dir, 10, |_| true);
        assert!(names.contains(&OsString::from("original.jsonl")));
        assert!(!names.contains(&OsString::from("replacement.jsonl")));
    }
}
