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
use doxa_lore::{LoreClient, SessionSearchHit};

const MAX_PROJECTS: usize = 128;
const MAX_FILES: usize = 2048;
const MAX_OFFLINE: usize = 64;
const MAX_FILE_BYTES: u64 = 512 * 1024;
const MAX_CODEX_METADATA_BYTES: u64 = 64 * 1024;
const MAX_RESTORED_PEER_EVENTS: usize = 2048;
const MAX_RESTORED_PEER_RECORD_BYTES: usize = 32 * 1024;

#[derive(Clone, Debug)]
pub struct OfflineSession {
    pub id: String,
    pub project: String,
    pub markdown: String,
    /// At most three scrubbed, bounded FTS excerpts from this search.
    pub search_snippets: Vec<String>,
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
    if !meta.is_file() || meta.uid() != uid || meta.nlink() > 1 { return None; }
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
    Some(render(&TranscriptSnapshot { bytes, earlier_bytes_omitted: start > 0, ..Default::default() }))
}

fn internal_router_worker(file: &mut File) -> bool {
    if file.seek(SeekFrom::Start(0)).is_err() {return true;}
    let mut head=Vec::new();if file.take(64*1024).read_to_end(&mut head).is_err(){return true;}
    head.split(|byte|*byte==b'\n').take(128).filter_map(|line|serde_json::from_slice::<Value>(line).ok())
        .any(|record|record["router_worker"] == true)
}

fn recorded_cwd(file: &mut File) -> Option<PathBuf> {
    file.seek(SeekFrom::Start(0)).ok()?;
    let mut head = Vec::new();
    file.take(64 * 1024).read_to_end(&mut head).ok()?;
    for line in head.split(|byte| *byte == b'\n').take(128) {
        let Ok(record) = serde_json::from_slice::<Value>(line) else { continue };
        // Native CLI worker artifacts are private proof for the parent router,
        // not separately selectable/resumable UI conversations.
        if record["router_worker"] == true { return None; }
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

/// Resolve a saved tab through canonical project identity, then open only its
/// exact owned transcript. A missing managed checkout can use its recorded cwd.
pub fn saved_session(id: &str, cwd: &Path) -> Option<OfflineSession> {
    if !crate::discovery::valid_id(id) || !cwd.is_absolute() { return None; }
    let root = projects_dir()?;
    if let Ok(mut lore) = LoreClient::open(Duration::from_secs(3)) {
        if let Ok((canonical_root, project)) = lore.transcript_identity(&cwd.to_string_lossy()) {
            if canonical_root == root {
                let mut entries = indexed_hits_in(&root, vec![SessionSearchHit {
                    session_id: id.into(), project, snippet: String::new(),
                }]);
                if let Some(entry) = entries.pop() { return Some(entry); }
            }
        }
    }
    // Do not guess another project's identity when a checkout is present.
    if !fs::symlink_metadata(cwd).is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound) { return None; }
    let mut entries = discover_prefix(id).into_iter().filter(|entry|
        entry.id == id && entry.cwd.as_deref() == Some(cwd));
    let entry = entries.next()?;
    entries.next().is_none().then_some(entry)
}

/// Remove the exact owned DOXA JSONL transcript after its daemon has stopped.
/// Provider-native archives are independent records and are not touched here.
pub fn delete_saved_session(id: &str, cwd: &Path) -> Result<(), &'static str> {
    if !crate::discovery::valid_id(id) || !cwd.is_absolute() { return Err("invalid session identity"); }
    let root = projects_dir().ok_or("transcript root unavailable")?;
    let (project, verified) = if let Some(entry) = saved_session(id, cwd) {
        if entry.cwd.as_deref().is_some_and(|recorded| recorded != cwd) {
            return Err("transcript records a different session directory");
        }
        (entry.project, true)
    } else {
        // Empty sessions and already-removed transcripts still have a saved
        // window record. Resolve their exact project before treating absence
        // as success; an unavailable identity service is not proof of absence.
        let mut lore = LoreClient::open(Duration::from_secs(3)).map_err(|_| "transcript identity unavailable")?;
        let (canonical_root, project) = lore.transcript_identity(&cwd.to_string_lossy())
            .map_err(|_| "transcript identity unavailable")?;
        if canonical_root != root { return Err("transcript root identity changed"); }
        (project, false)
    };
    delete_exact_in(&root, &project, id, verified)
}

fn delete_exact_in(root: &Path, project: &str, id: &str, verified: bool) -> Result<(), &'static str> {
    if !crate::discovery::valid_id(id) || project.is_empty() || project.len() > 255
        || project.contains('/') || project.contains('\\') || project.chars().any(char::is_control) {
        return Err("invalid transcript identity");
    }
    let uid = unsafe { libc::geteuid() };
    let root = match OpenOptions::new().read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(root) {
            Ok(root) => root,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(_) => return Err("transcript root unavailable"),
        };
    if !owned_dir(&root, uid) { return Err("unsafe transcript root"); }
    let dir = match open_at(&root, OsStr::new(project), libc::O_RDONLY | libc::O_DIRECTORY) {
        Some(dir) => dir,
        None if std::io::Error::last_os_error().kind() == std::io::ErrorKind::NotFound => return Ok(()),
        None => return Err("project transcript directory unavailable"),
    };
    if !owned_dir(&dir, uid) { return Err("unsafe project transcript directory"); }
    let name = format!("{id}.jsonl");
    let file = match open_at(&dir, OsStr::new(&name), libc::O_RDONLY | libc::O_NONBLOCK) {
        Some(file) => file,
        None if std::io::Error::last_os_error().kind() == std::io::ErrorKind::NotFound => return Ok(()),
        None => return Err("transcript unavailable"),
    };
    let meta = file.metadata().map_err(|_| "cannot inspect transcript")?;
    if !verified { return Err("saved transcript could not be verified"); }
    if !meta.is_file() || meta.uid() != uid || meta.nlink() != 1 {
        return Err("unsafe transcript file");
    }
    let name = std::ffi::CString::new(name).map_err(|_| "invalid transcript name")?;
    let mut current = std::mem::MaybeUninit::<libc::stat>::uninit();
    if unsafe { libc::fstatat(dir.as_raw_fd(), name.as_ptr(), current.as_mut_ptr(), libc::AT_SYMLINK_NOFOLLOW) } != 0 {
        return Err("transcript changed before deletion");
    }
    let current = unsafe { current.assume_init() };
    if u64::try_from(current.st_dev).ok() != Some(meta.dev())
        || u64::try_from(current.st_ino).ok() != Some(meta.ino()) {
        return Err("transcript changed before deletion");
    }
    if unsafe { libc::unlinkat(dir.as_raw_fd(), name.as_ptr(), 0) } != 0 {
        return Err("could not delete transcript");
    }
    Ok(())
}

pub fn discover_prefix(prefix: &str) -> Vec<OfflineSession> {
    if !crate::discovery::valid_id(prefix) { return Vec::new(); }
    let Some(root) = projects_dir() else { return Vec::new(); };
    discover_in(&root, Some(prefix), None)
}

/// Search LORE's existing FTS index first, then load exact owned transcript
/// files through checked descriptors. The bounded tail scan is a fallback
/// for an unavailable or empty index.
pub fn discover_query(query: &str, cwd: &Path) -> Vec<OfflineSession> {
    let query = query.trim();
    if query.is_empty() || query.len() > 200 || query.chars().any(char::is_control) { return Vec::new(); }
    let Some(root) = projects_dir() else { return Vec::new(); };
    if let Some(cwd) = cwd.to_str() {
        if let Ok(mut lore) = LoreClient::open(Duration::from_secs(3)) {
            if let Ok(hits) = lore.session_search(cwd, query) {
                let found = indexed_hits_in(&root, hits);
                if !found.is_empty() { return found; }
            }
        }
    }
    discover_in(&root, None, Some(query))
}

fn indexed_hits_in(root: &Path, hits: Vec<SessionSearchHit>) -> Vec<OfflineSession> {
    let uid = unsafe { libc::geteuid() };
    let Ok(root) = OpenOptions::new().read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC).open(root)
        else { return Vec::new(); };
    if !owned_dir(&root, uid) { return Vec::new(); }
    let mut found = Vec::new();
    let mut positions = std::collections::HashMap::new();
    for hit in hits {
        if !crate::discovery::valid_id(&hit.session_id)
            || hit.project.is_empty() || hit.project.len() > 255
            || hit.project.contains('/') || hit.project.contains('\\')
            || hit.project.chars().any(char::is_control) { continue; }
        let key = (hit.project.clone(), hit.session_id.clone());
        if let Some(&position) = positions.get(&key) {
            let entry: &mut OfflineSession = &mut found[position];
            if entry.search_snippets.len() < 3 && !entry.search_snippets.contains(&hit.snippet) {
                entry.search_snippets.push(hit.snippet);
            }
            continue;
        }
        let Some(dir) = open_at(&root, OsStr::new(&hit.project), libc::O_RDONLY | libc::O_DIRECTORY) else { continue; };
        if !owned_dir(&dir, uid) { continue; }
        let name = format!("{}.jsonl", hit.session_id);
        let Some(mut file) = open_at(&dir, OsStr::new(&name), libc::O_RDONLY | libc::O_NONBLOCK) else { continue; };
        if !file.metadata().is_ok_and(|meta| meta.is_file() && meta.uid() == uid && meta.nlink() == 1) { continue; }
        if internal_router_worker(&mut file) {continue;}
        let cwd = recorded_cwd(&mut file);
        if let Some(markdown) = read_offline(file, uid) {
            let snippet = if hit.snippet.is_empty() { Vec::new() } else { vec![hit.snippet] };
            positions.insert(key, found.len());
            found.push(OfflineSession { id: hit.session_id, project: hit.project, markdown,
                search_snippets: snippet, cwd });
        }
    }
    found
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
            if !meta.is_file() || meta.uid() != uid || internal_router_worker(&mut open_file) { continue; }
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
        Some(OfflineSession { id, project, markdown, search_snippets: Vec::new(), cwd })
    }).collect()
}

/// Verify the session's recorded cwd maps back to this transcript directory,
/// then require the original engine's replay artefact.
pub fn resume_plan(entry: &OfflineSession) -> Result<LaunchOptions, &'static str> {
    let root = projects_dir().ok_or("transcript root unavailable")?;
    let claude = claude_store_root().ok_or("DOXA home unavailable")?;
    resume_plan_in(entry, &root, &claude, &|| LoreClient::open(Duration::from_secs(3)))
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

fn ready_resume_cwd(cwd: &Path, id: &str, missing: bool,
    expected_root: &Path, expected_slug: &str, open_lore: &impl Fn() -> Result<LoreClient, doxa_lore::LoreError>) -> Result<PathBuf, &'static str> {
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
    let mut lore = open_lore().map_err(|_| "LORE unavailable for resume verification")?;
    let (root, slug) = lore.transcript_identity(&canonical.to_string_lossy())
        .map_err(|_| "cannot verify session project")?;
    if root != expected_root || slug != expected_slug {
        return Err("session directory does not match transcript project");
    }
    Ok(canonical)
}

fn resume_plan_in(entry: &OfflineSession, expected_root: &Path, claude_root: &Path, open_lore: &impl Fn() -> Result<LoreClient, doxa_lore::LoreError>) -> Result<LaunchOptions, &'static str> {
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
        let mut lore = open_lore().map_err(|_| "LORE unavailable for resume verification")?;
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
        let meta = record.metadata().map_err(|_| "unreadable Codex thread record")?;
        if !meta.is_file() || meta.uid() != uid || meta.nlink() != 1
            || meta.len() == 0 || meta.len() > MAX_CODEX_METADATA_BYTES {
            return Err("unsafe Codex thread record");
        }
        let mut bytes = Vec::new();
        record.read_to_end(&mut bytes).map_err(|_| "unreadable Codex thread record")?;
        let state: Value = serde_json::from_slice(&bytes).map_err(|_| "invalid Codex thread record")?;
        // Native Claude and Codex share TranscriptStore's thread sidecar.
        // The filename is a storage convention, not the engine identity.
        let engine = match state.get("engine") {
            None | Some(Value::Null) => Engine::Codex,
            Some(Value::String(engine)) if engine == "codex" => Engine::Codex,
            Some(Value::String(engine)) if engine == "claude" => Engine::Claude,
            _ => return Err("unsupported saved thread engine"),
        };
        let vendor_name = format!("{}.messages.json", entry.id);
        let cli_history = claude_history_present(claude_root, &entry.id, uid);
        if entry_present(&dir, OsStr::new(&vendor_name)) || engine == Engine::Codex && cli_history {
            return Err("ambiguous session engine state");
        }
        if engine == Engine::Claude && !cli_history {
            return Err("saved Claude CLI history is unavailable");
        }
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
        let cwd = ready_resume_cwd(&cwd, &entry.id, missing, expected_root, &entry.project, open_lore)?;
        return Ok(LaunchOptions { engine, cwd: Some(cwd), model,
            resume: Some(entry.id.clone()), ..LaunchOptions::default() });
    }
    let name = format!("{}.messages.json", entry.id);
    let Some(mut saved) = open_at(&dir, OsStr::new(&name), libc::O_RDONLY | libc::O_NONBLOCK) else {
        if claude_history_present(claude_root, &entry.id, uid) {
            let cwd = ready_resume_cwd(&cwd, &entry.id, missing, expected_root, &entry.project, open_lore)?;
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
        Some("router") => return Err("router resume needs doxa new --engine router --router-config PATH --resume ID; automatic config inheritance is unavailable"),
        _ => return Err("unsupported or unknown saved engine"),
    };
    if state.get("session_id").is_some_and(|id| id.as_str() != Some(&entry.id)) {
        return Err("replay state belongs to another session");
    }
    let model = state["model"].as_str().filter(|m| !m.is_empty() && m.len() <= 128 && !m.chars().any(char::is_control))
        .ok_or("saved vendor model is unknown")?;
    if !state["messages"].is_array() { return Err("invalid replay state"); }
    let cwd = ready_resume_cwd(&cwd, &entry.id, missing, expected_root, &entry.project, open_lore)?;
    Ok(LaunchOptions { engine, cwd: Some(cwd), model: Some(model.to_owned()),
        resume: Some(entry.id.clone()), ..LaunchOptions::default() })
}

const MAX_TURNS: usize = 40;
const MAX_TEXT_CHARS: usize = 20_000;
const MAX_VIEW_BYTES: usize = 480 * 1024;
const MAX_RESTORED_DETAIL_BYTES: usize = 256 * 1024;

#[derive(Default)]
struct Turn {
    prompt: String,
    peer_card: Option<String>,
    answer: String,
    tools: Vec<String>,
    tool_names: std::collections::HashMap<String, String>,
    codex_results: std::collections::HashMap<String, usize>,
    codex_details: std::collections::HashMap<String, String>,
}

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

fn restored_tool_id(id: &str) -> String {
    if id.is_empty() || id.len() > 200 || id.chars().any(char::is_control) { return String::new(); }
    format!("{}{}", crate::ui::transcript_tools::TOOL_ID_PREFIX, serde_json::json!(id))
}

fn tool_label(value: &Value) -> String {
    let raw = value.as_str().map(str::to_owned).unwrap_or_else(|| value.to_string());
    crate::markdown::sanitize(&raw).replace(['\n', '\r'], " ").chars().take(400).collect()
}

fn restored_detail_marker(kind: &str, value: &Value) -> String {
    if value.is_null() { return String::new(); }
    let raw = value.as_str().map(str::to_owned).unwrap_or_else(|| value.to_string());
    let clean = crate::markdown::sanitize(&raw);
    let mut end = clean.len().min(MAX_RESTORED_DETAIL_BYTES);
    while !clean.is_char_boundary(end) { end -= 1; }
    let mut visible = clean[..end].to_owned();
    if end < clean.len() { visible.push_str("\n[Tool detail display limit reached]"); }
    format!("{}{}", crate::ui::transcript_tools::RESTORED_TOOL_PREFIX,
        serde_json::json!({"kind":kind,"text":visible}))
}

fn append_text(target: &mut String, text: &str) {
    if text.is_empty() { return; }
    if !target.is_empty() { target.push_str("\n\n"); }
    target.push_str(text);
}

pub fn render(snapshot: &TranscriptSnapshot) -> String {
    let mut turns: Vec<Turn> = Vec::new();
    let mut peer_cards = Vec::new();
    let mut peer_ids = std::collections::HashSet::new();
    let mut peer_history_omitted = false;
    if let (Some(session_id), Some(scope)) = (snapshot.session_id.as_deref(), snapshot.peer_scope.as_deref()) {
        if doxa_transcript::valid_session_id(session_id) && !scope.is_empty() && scope.len() <= 512
            && !scope.chars().any(char::is_control) {
            let lines = snapshot.peer_bytes.split(|byte| *byte == b'\n').filter(|line| !line.is_empty());
            peer_history_omitted = lines.clone().take(MAX_RESTORED_PEER_EVENTS + 1).count() > MAX_RESTORED_PEER_EVENTS;
            for line in lines.take(MAX_RESTORED_PEER_EVENTS) {
                if line.is_empty() || line.len() > MAX_RESTORED_PEER_RECORD_BYTES { continue; }
                let Ok(row) = serde_json::from_slice::<Value>(line) else { continue };
                let Some(fields) = row.as_object() else { continue };
                if fields.len() != 5 || fields.keys().any(|key| !matches!(key.as_str(),
                    "version" | "session_id" | "scope" | "event_type" | "data"))
                    || row["version"].as_u64() != Some(1)
                    || row["session_id"].as_str() != Some(session_id)
                    || row["scope"].as_str() != Some(scope) { continue; }
                let event_type = row["event_type"].as_str().unwrap_or("");
                let data = &row["data"];
                let allowed = match event_type {
                    "peer_message" => &["from_id", "from_title", "body", "from_repo", "sent_at", "kind",
                        "origin", "message_id", "in_reply_to", "unreviewed"][..],
                    "peer_sent" => &["to", "failed", "body", "sent_at", "kind", "in_reply_to", "message_id"][..],
                    _ => continue,
                };
                let Some(data_fields) = data.as_object() else { continue };
                if data_fields.keys().any(|key| !allowed.contains(&key.as_str())) { continue; }
                let Some(message_id) = data["message_id"].as_str() else { continue };
                let sent = event_type == "peer_sent";
                if peer_ids.contains(&(message_id.to_owned(), sent)) { continue; }
                if let Some(marker) = crate::ui::restored_peer_message(event_type, data, session_id) {
                    peer_ids.insert((message_id.to_owned(), sent));
                    peer_cards.push(marker);
                }
            }
        }
    }
    for line in snapshot.bytes.split(|byte| *byte == b'\n') {
        let Ok(record) = serde_json::from_slice::<Value>(line) else { continue };
        let kind = record["type"].as_str().unwrap_or("");
        let content = &record["message"]["content"];
        if matches!(kind, "tool_call" | "tool_result" | "tool_result_detail")
            && record["engine"] == "codex" {
            let data = &record["data"];
            let Some(id) = data["id"].as_str().filter(|id| !id.is_empty() && id.len() <= 200
                && !id.chars().any(char::is_control)) else { continue };
            if turns.is_empty() { turns.push(Turn::default()); }
            let turn = turns.last_mut().unwrap();
            match kind {
                "tool_call" => {
                    let name = tool_label(&data["name"]);
                    let name = if name.is_empty() { "Tool".to_owned() } else { name };
                    turn.tool_names.insert(id.to_owned(), name.clone());
                    let detail = tool_detail(&data["input"]);
                    turn.tools.push(format!("Tool: {name} started{}{}{}",
                        if detail.is_empty() { String::new() } else { format!(" · {detail}") },
                        restored_tool_id(id), restored_detail_marker("input", &data["input"])));
                }
                "tool_result" => {
                    let name = turn.tool_names.get(id).map(String::as_str)
                        .or_else(|| data["name"].as_str()).unwrap_or("Tool");
                    let outcome = if data["is_error"] == true { "failed" } else { "finished" };
                    let summary = tool_detail(&data["result_summary"]);
                    let row = format!("Tool: {name} {outcome}{}{}",
                        if summary.is_empty() { String::new() } else { format!(" · {summary}") }, restored_tool_id(id));
                    if let Some(index) = turn.codex_results.get(id).copied() {
                        turn.tools[index] = row;
                        turn.codex_details.remove(id);
                    } else {
                        turn.codex_results.insert(id.to_owned(), turn.tools.len());
                        turn.tools.push(row);
                    }
                }
                "tool_result_detail" => {
                    if turn.codex_results.contains_key(id) {
                        if let Some(chunk) = data["text"].as_str() {
                            let detail = turn.codex_details.entry(id.to_owned()).or_default();
                            let remaining = MAX_RESTORED_DETAIL_BYTES.saturating_sub(detail.len());
                            let mut end = chunk.len().min(remaining);
                            while !chunk.is_char_boundary(end) { end -= 1; }
                            detail.push_str(&chunk[..end]);
                            if end < chunk.len() && !detail.ends_with("[Tool detail display limit reached]") {
                                detail.push_str("\n[Tool detail display limit reached]");
                            }
                        }
                    }
                }
                _ => unreachable!(),
            }
            continue;
        }
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
            let origin = record.get("doxa_peer_origin");
            // Bind persisted origin to both the attached snapshot and the
            // exact transcript row before using it to style or suppress the
            // provider-facing prompt. A copied/cross-session field stays prose.
            let row_matches_snapshot = snapshot.session_id.as_deref().is_some_and(|session_id|
                doxa_transcript::valid_session_id(session_id)
                    && record["sessionId"].as_str() == Some(session_id));
            if row_matches_snapshot {
                if origin.and_then(crate::ui::peer_origin_message_id)
                    .is_some_and(|message_id| peer_ids.contains(&(message_id, false))) {
                    // The private receipt journal supplies the same admitted
                    // message as a typed card. Keep a turn slot for its assistant
                    // response, but do not render the provider wrapper twice.
                    turns.push(Turn::default());
                    continue;
                }
                if let Some(marker) = origin.and_then(|origin|
                    crate::ui::restored_peer_turn(origin, &prompt)) {
                    turns.push(Turn { peer_card: Some(marker), ..Turn::default() });
                    continue;
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
                        turn.tools.push(format!("Tool: {name} {outcome}{}{}{}",
                            if detail.is_empty() { String::new() } else { format!(" · {detail}") },
                            restored_tool_id(id), restored_detail_marker("result", &block["content"])));
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
                    let name = tool_label(&block["name"]);
                    let name = if name.is_empty() { "Tool".to_owned() } else { name };
                    if let Some(id) = block["id"].as_str() { turn.tool_names.insert(id.to_owned(), name.clone()); }
                    let detail = tool_detail(&block["input"]);
                    turn.tools.push(format!("Tool: {name} started{}{}{}",
                        if detail.is_empty() { String::new() } else { format!(" · {detail}") },
                        restored_tool_id(block["id"].as_str().unwrap_or("")), restored_detail_marker("input", &block["input"])));
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
    for mut turn in turns.into_iter().skip(omitted_turns) {
        for (id, index) in &turn.codex_results {
            if let Some(detail) = turn.codex_details.get(id) {
                turn.tools[*index].push_str(&restored_detail_marker("result", &Value::String(detail.clone())));
            }
        }
        if let Some(peer_card) = turn.peer_card {
            out.push_str(&peer_card);
            out.push_str("\n\n");
        } else if !turn.prompt.is_empty() {
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
    if !peer_cards.is_empty() {
        append_text(&mut out, "**Recovered peer history (original transcript placement unavailable):**");
        out.push_str("\n\n");
    }
    for marker in peer_cards {
        out.push_str(&marker);
        out.push_str("\n\n");
    }
    if peer_history_omitted {
        append_text(&mut out, "[Additional peer history omitted from this restore view at the event limit.]");
    }
    if out.len() > MAX_VIEW_BYTES {
        let mut start = out.len() - MAX_VIEW_BYTES;
        while !out.is_char_boundary(start) { start += 1; }
        // Keep a complete paragraph: internal restored-detail JSON must
        // never be exposed as a partial prose fragment after tail clipping.
        start = out[start..].find("\n\n").map_or(out.len(), |offset| start + offset + 2);
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

    fn resume_fixture(entry: &OfflineSession, python: &Path, root: &Path, claude: &Path) -> Result<LaunchOptions, &'static str> {
        resume_plan_in(entry, root, claude, &|| LoreClient::spawn(python, Duration::from_secs(3)))
    }

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
        let rendered = render(&TranscriptSnapshot { bytes: lines.as_bytes().to_vec(), earlier_bytes_omitted: false, ..Default::default() });
        assert!(rendered.contains("first?"));
        assert!(rendered.contains("yes\n\nindeed"));
        assert!(rendered.contains("Tool: Search started"));
        assert!(rendered.contains("Tool: Tool finished · found"));
        assert!(rendered.contains("second?\n\nmore detail"));
        assert_eq!(rendered.matches("**You:**").count(), 2);
    }

    #[test]
    fn restores_only_session_and_scope_bound_typed_peer_journal_rows() {
        let owner = "owner-session";
        let scope = "/private/scope";
        let sender = "sender-session";
        let receipt = serde_json::json!({"version":1,"session_id":owner,"scope":scope,
            "event_type":"peer_message","data":{"from_id":sender,"from_title":"Reviewer",
                "body":"received body","from_repo":null,"sent_at":"2026-10-11T13:25:00Z",
                "kind":"direct","origin":null,"message_id":"msg-received","in_reply_to":null,"unreviewed":false}});
        let sent = serde_json::json!({"version":1,"session_id":owner,"scope":scope,
            "event_type":"peer_sent","data":{"to":[sender],"failed":[],"body":"sent body",
                "sent_at":"2026-10-11T13:26:00Z","kind":"direct","in_reply_to":null,"message_id":"msg-sent"}});
        let wrong_session = serde_json::json!({"version":1,"session_id":"other-session","scope":scope,
            "event_type":"peer_sent","data":{"to":[sender],"failed":[],"body":"wrong session",
                "sent_at":"2026-10-11T13:27:00Z","kind":"direct","in_reply_to":null,"message_id":"msg-cross-session"}});
        let wrong_scope = serde_json::json!({"version":1,"session_id":owner,"scope":"other-scope",
            "event_type":"peer_message","data":{"from_id":sender,"from_title":"Reviewer",
                "body":"wrong scope","from_repo":null,"sent_at":null,"kind":"direct",
                "origin":null,"message_id":"msg-cross-scope","in_reply_to":null,"unreviewed":false}});
        let peer_bytes = [receipt, sent, wrong_session, wrong_scope].iter()
            .map(Value::to_string).collect::<Vec<_>>().join("\n").into_bytes();
        let rendered = render(&TranscriptSnapshot { session_id:Some(owner.into()), peer_scope:Some(scope.into()),
            peer_bytes:peer_bytes.clone(), ..Default::default() });
        assert!(rendered.contains("received body") && rendered.contains("sent body"));
        assert!(rendered.contains("Recovered peer history (original transcript placement unavailable)"));
        assert!(!rendered.contains("wrong session") && !rendered.contains("wrong scope"));
        assert_eq!(rendered.matches("\u{001e}DOXA_MESSAGE:").count(), 2);
        assert_eq!(rendered.split("\n\n")
            .filter(|paragraph| paragraph.trim_matches('\n').starts_with("\u{001e}DOXA_MESSAGE:"))
            .count(), 2, "each recovered card must occupy its own renderable paragraph");

        let stale = render(&TranscriptSnapshot { session_id:Some("another-session".into()),
            peer_scope:Some(scope.into()), peer_bytes, ..Default::default() });
        assert!(!stale.contains("\u{001e}DOXA_MESSAGE:"));
    }

    #[test]
    fn restores_typed_peer_turn_in_place_without_journal_and_rejects_cross_session_origin() {
        let owner = "owner-session";
        let origin = serde_json::json!({"sender_session_id":"sender-session","sender_title":"Reviewer",
            "message_id":"msg-restored","sent_at":"2026-10-11T13:25:00Z","from_repo":null,
            "remote_origin":null,"source":"peer_message"});
        let records = [
            serde_json::json!({"type":"user","sessionId":owner,"doxa_peer_origin":origin,
                "message":{"role":"user","content":"typed inbound peer prompt"}}),
            serde_json::json!({"type":"assistant","sessionId":owner,
                "message":{"role":"assistant","content":[{"type":"text","text":"peer answer"}]}}),
            serde_json::json!({"type":"user","sessionId":"other-session","doxa_peer_origin":origin,
                "message":{"role":"user","content":"cross session ordinary prompt"}}),
            serde_json::json!({"type":"assistant","sessionId":"other-session",
                "message":{"role":"assistant","content":[{"type":"text","text":"ordinary answer"}]}}),
        ];
        let bytes = records.iter().map(Value::to_string).collect::<Vec<_>>().join("\n").into_bytes();
        let rendered = render(&TranscriptSnapshot { session_id:Some(owner.into()), bytes, ..Default::default() });

        assert_eq!(rendered.matches("\u{001e}DOXA_MESSAGE:").count(), 1);
        assert_eq!(rendered.matches("**You:**").count(), 1);
        assert!(rendered.contains("cross session ordinary prompt"));
        let marker = "\u{001e}DOXA_MESSAGE:";
        let card_start = rendered.find(marker).unwrap();
        let answer_start = rendered.find("peer answer").unwrap();
        assert!(card_start < answer_start, "the restored peer card stays at its transcript turn position");
        let paragraph = rendered[card_start..].split("\n\n").next().unwrap();
        let card: Value = serde_json::from_str(paragraph.strip_prefix(marker).unwrap()).unwrap();
        assert_eq!(card["starts_turn"], true);
        assert_eq!(card["message_id"], "msg-restored");
        assert!(card["text"].as_str().unwrap().contains("typed inbound peer prompt"));
    }

    #[test]
    fn restored_peer_journal_enforces_exact_event_limit_and_marks_omission() {
        let owner = "owner-session";
        let scope = "/private/scope";
        let bytes = (0..=MAX_RESTORED_PEER_EVENTS).map(|index| serde_json::json!({
            "version":1,"session_id":owner,"scope":scope,"event_type":"peer_message",
            "data":{"from_id":"sender-session","from_title":"Worker","body":"x",
                "from_repo":null,"sent_at":null,"kind":"direct","origin":null,
                "message_id":format!("msg-{index}"),"in_reply_to":null,"unreviewed":false}
        }).to_string()).collect::<Vec<_>>().join("\n").into_bytes();
        let rendered = render(&TranscriptSnapshot { session_id:Some(owner.into()), peer_scope:Some(scope.into()),
            peer_bytes:bytes, ..Default::default() });
        assert!(rendered.contains("msg-2047"));
        assert!(!rendered.contains("msg-2048"));
        assert!(rendered.contains("Additional peer history omitted from this restore view at the event limit."));
    }

    #[test]
    fn restored_tool_call_and_result_keep_bounded_matching_details() {
        let input = "x".repeat(1000);
        let lines = format!("{{\"type\":\"user\",\"message\":{{\"content\":\"question\"}}}}\n{{\"type\":\"assistant\",\"message\":{{\"content\":[{{\"type\":\"tool_use\",\"id\":\"call-1\",\"name\":\"Read\",\"input\":{{\"path\":\"{input}\"}}}}]}}}}\n{{\"type\":\"user\",\"message\":{{\"content\":[{{\"type\":\"tool_result\",\"tool_use_id\":\"call-1\",\"content\":\"found\"}}]}}}}\n");
        let rendered = render(&TranscriptSnapshot { bytes: lines.into_bytes(), earlier_bytes_omitted: false, ..Default::default() });
        assert!(rendered.contains("Tool: Read started ·"));
        assert!(rendered.contains("Tool: Read finished · found"));
        assert!(rendered.contains(crate::ui::transcript_tools::RESTORED_TOOL_PREFIX));
        assert!(rendered.contains(&input));
        assert!(rendered.contains('…')); // collapsed row remains concise
    }

    #[test]
    fn restores_codex_tool_events_and_keeps_legacy_blocks_compatible() {
        let records = [
            serde_json::json!({"type":"user","message":{"content":"inspect"}}),
            serde_json::json!({"type":"tool_call","engine":"codex","data":{"id":"c1","name":"command_execution","input":{"command":"rg needle"}}}),
            serde_json::json!({"type":"tool_result","engine":"codex","data":{"id":"c1","name":"command_execution","result_summary":"short","is_error":false}}),
            serde_json::json!({"type":"tool_result_detail","engine":"codex","data":{"id":"c1","text":"full result\n"}}),
            serde_json::json!({"type":"tool_result_detail","engine":"codex","data":{"id":"c1","text":"second line"}}),
            serde_json::json!({"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}),
        ];
        let bytes = records.iter().map(Value::to_string).collect::<Vec<_>>().join("\n").into_bytes();
        let rendered = render(&TranscriptSnapshot { bytes, earlier_bytes_omitted: false, ..Default::default() });
        assert!(rendered.contains("Tool: command_execution started"));
        assert!(rendered.contains("Tool: command_execution finished · short"));
        assert!(rendered.contains("full result\\nsecond line"));
        assert!(rendered.contains("done"));
        assert_eq!(rendered.matches("**You:**").count(), 1);
        assert_eq!(rendered.matches("**Assistant:**").count(), 1);
    }

    #[test]
    fn restored_detail_limit_is_explicit_and_utf8_safe() {
        let marker = restored_detail_marker("result", &Value::String("é".repeat(150_000)));
        let encoded = marker.strip_prefix(crate::ui::transcript_tools::RESTORED_TOOL_PREFIX).unwrap();
        let detail: Value = serde_json::from_str(encoded).unwrap();
        let text = detail["text"].as_str().unwrap();
        assert!(text.ends_with("[Tool detail display limit reached]"));
        assert!(text.starts_with("éé"));
        assert!(text.len() <= MAX_RESTORED_DETAIL_BYTES + 40);
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
            markdown: String::new(), search_snippets: Vec::new(), cwd: Some(cwd.clone()) };
        fs::write(root.join("project/saved-1.jsonl"), b"saved transcript\n").unwrap();
        let replay = root.join("project/saved-1.messages.json");
        fs::write(&replay, br#"{"engine":"deepseek","session_id":"saved-1","model":"deepseek-chat","messages":[]}"#).unwrap();
        let plan = resume_fixture(&entry, &script, &root, &temp.path().join("cli-projects")).unwrap();
        assert_eq!(plan.engine, Engine::DeepSeek);
        assert_eq!(plan.cwd, Some(cwd));
        assert_eq!(plan.resume.as_deref(), Some("saved-1"));
        fs::write(&replay, br#"{"engine":"router","session_id":"saved-1","model":"router-conversation-v1","messages":[]}"#).unwrap();
        let error=resume_fixture(&entry,&script,&root,&temp.path().join("cli-projects")).unwrap_err();
        assert!(error.contains("--router-config") && error.contains("inheritance is unavailable"));
        let mut wrong = entry.clone();
        wrong.project = "another-project".into();
        assert!(resume_fixture(&wrong, &script, &root, &temp.path().join("cli-projects")).is_err());
        fs::remove_file(&replay).unwrap();
        symlink(temp.path().join("outside"), &replay).unwrap();
        assert!(resume_fixture(&entry, &script, &root, &temp.path().join("cli-projects")).is_err());
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
            markdown: String::new(), search_snippets: Vec::new(), cwd: Some(cwd) };
        fs::write(root.join("project/saved-1.jsonl"), b"saved transcript\n").unwrap();
        assert!(resume_fixture(&entry, &script, &root, &cli).is_err());
        fs::write(cli.join("encoded-cwd/saved-1.jsonl"), b"saved CLI history\n").unwrap();
        let plan = resume_fixture(&entry, &script, &root, &cli).unwrap();
        assert_eq!(plan.engine, Engine::Claude);
        fs::write(root.join("project/saved-1.codex.json"), b"{}").unwrap();
        assert!(resume_fixture(&entry, &script, &root, &cli).unwrap_err().contains("ambiguous"));
    }

    #[test]
    fn native_claude_thread_checkpoint_resumes_claude_with_its_own_cli_history() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("projects");
        fs::create_dir_all(root.join("project")).unwrap();
        let cwd = temp.path().join("checkout");
        fs::create_dir(&cwd).unwrap();
        let cli = temp.path().join("cli-projects");
        fs::create_dir_all(cli.join("encoded-cwd")).unwrap();
        let script = fake_lore(temp.path(), &root);
        let entry = OfflineSession { id: "saved-1".into(), project: "project".into(),
            markdown: String::new(), search_snippets: Vec::new(), cwd: Some(cwd.clone()) };
        fs::write(root.join("project/saved-1.jsonl"), b"saved transcript\n").unwrap();
        let state = serde_json::json!({"thread_id":"saved-1", "session_id":"saved-1",
            "engine":"claude", "transport":"stream-json", "cwd":cwd,
            "model":"claude-test", "turn_incomplete":false});
        fs::write(root.join("project/saved-1.codex.json"), state.to_string()).unwrap();
        assert_eq!(resume_fixture(&entry, &script, &root, &cli).unwrap_err(), "saved Claude CLI history is unavailable");
        fs::write(cli.join("encoded-cwd/saved-1.jsonl"), b"saved CLI history\n").unwrap();
        let plan = resume_fixture(&entry, &script, &root, &cli).unwrap();
        assert_eq!(plan.engine, Engine::Claude);
        assert_eq!(plan.resume.as_deref(), Some("saved-1"));
        assert_eq!(plan.model.as_deref(), Some("claude-test"));
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
            markdown: String::new(), search_snippets: Vec::new(), cwd: Some(cwd.clone()) };
        fs::write(root.join("project/saved-1.jsonl"), b"saved transcript\n").unwrap();
        let record = root.join("project/saved-1.codex.json");
        let state = serde_json::json!({"thread_id":"thread-123", "session_id":"saved-1",
            "cwd":cwd, "model":"gpt-test", "turn_incomplete":false});
        fs::write(&record, state.to_string()).unwrap();
        let plan = resume_fixture(&entry, &script, &root, &temp.path().join("cli-projects")).unwrap();
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
            assert!(resume_fixture(&entry, &script, &root, &temp.path().join("cli-projects")).is_err());
        }
        fs::remove_file(&record).unwrap();
        symlink(temp.path().join("outside"), &record).unwrap();
        assert!(resume_fixture(&entry, &script, &root, &temp.path().join("cli-projects")).is_err());
    }

    #[test]
    fn missing_checkout_does_not_trigger_recovery_for_invalid_replay_state() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("projects");
        fs::create_dir_all(root.join("project")).unwrap();
        let cwd = temp.path().join("missing-checkout");
        let script = fake_lore(temp.path(), &root);
        let entry = OfflineSession { id: "saved-1".into(), project: "project".into(),
            markdown: String::new(), search_snippets: Vec::new(), cwd: Some(cwd.clone()) };
        fs::write(root.join("project/saved-1.jsonl"), b"saved transcript\n").unwrap();
        fs::write(root.join("project/saved-1.codex.json"),
            serde_json::json!({"thread_id":"thread-123", "session_id":"other",
                "cwd":cwd, "turn_incomplete":false}).to_string()).unwrap();
        assert_eq!(resume_fixture(&entry, &script, &root, &temp.path().join("cli-projects"))
            .unwrap_err(), "Codex thread record does not match this session");
        assert!(!cwd.exists());
    }

    #[test]
    fn missing_managed_checkout_recovers_after_replay_verification() {
        let output = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "history::tests::missing_managed_checkout_recovery_child", "--nocapture"])
            .env("DOXA_RECOVERY_TEST_CHILD", "1").output().unwrap();
        let bounded = |bytes: &[u8]| String::from_utf8_lossy(&bytes[..bytes.len().min(4096)]).into_owned();
        assert!(output.status.success(), "recovery child {}\nstdout:\n{}\nstderr:\n{}",
            output.status, bounded(&output.stdout), bounded(&output.stderr));
    }

    #[test]
    fn missing_managed_checkout_recovery_child() {
        if std::env::var("DOXA_RECOVERY_TEST_CHILD").as_deref() != Ok("1") { return; }
        let temp = tempfile::tempdir().unwrap();
        fs::set_permissions(temp.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let main = temp.path().join("repo");
        let home = temp.path().join("home");
        let root = temp.path().join("projects");
        fs::create_dir(&home).unwrap();
        fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
        fs::create_dir(&main).unwrap();
        fs::set_permissions(&main, fs::Permissions::from_mode(0o700)).unwrap();
        fs::create_dir_all(root.join("project")).unwrap();
        for path in [&root, &root.join("project")] {
            fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let git = |args: &[&str]| {
            let output = Command::new("git").env_clear()
                .env("PATH", std::env::var_os("PATH").unwrap_or_default())
                .env("TMPDIR", std::env::var_os("TMPDIR").unwrap_or_default())
                .env("HOME", &home).env("GIT_CONFIG_NOSYSTEM", "1")
                .env("GIT_CONFIG_GLOBAL", "/dev/null")
                .args(args).current_dir(&main).output().unwrap();
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
            markdown: String::new(), search_snippets: Vec::new(), cwd: Some(cwd.clone()) };
        fs::write(&record, serde_json::json!({"thread_id":"thread-123",
            "session_id":"other", "cwd":cwd, "turn_incomplete":false}).to_string()).unwrap();
        assert!(resume_fixture(&entry, &script, &root, &temp.path().join("claude"))
            .unwrap_err().contains("thread record does not match"));
        assert!(!cwd.exists(), "invalid replay state reconstructed the checkout");
        fs::write(&record, serde_json::json!({"thread_id":"thread-123",
            "session_id":"saved123-session", "cwd":cwd, "turn_incomplete":false}).to_string()).unwrap();
        let plan = resume_fixture(&entry, &script, &root, &temp.path().join("claude")).unwrap();
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
    fn private_router_worker_proofs_are_excluded_from_history_and_indexed_restore() {
        let temp=tempfile::tempdir().unwrap();let root=temp.path().join("projects");
        fs::create_dir_all(root.join("project")).unwrap();
        for (id,worker) in [("worker-uuid",true),("outer-router",false)] {
            let header=if worker {serde_json::json!({"type":"router_worker","router_worker":true}).to_string()+"\n"}else{String::new()};
            let content=if worker {"x".repeat(128*1024)}else{"same searchable request {\"router_worker\":true}".into()};
            fs::write(root.join(format!("project/{id}.jsonl")),header+&serde_json::json!({"type":"user",
                "router_worker":worker,"cwd":temp.path(),"message":{"content":content}}).to_string()).unwrap();
        }
        assert_eq!(discover_in(&root,None,None).iter().map(|entry|entry.id.as_str()).collect::<Vec<_>>(),["outer-router"]);
        assert!(discover_in(&root,Some("worker-uuid"),None).is_empty());
        assert!(discover_in(&root,None,Some("searchable")).iter().all(|entry|entry.id=="outer-router"));
        let hits=["worker-uuid","outer-router"].map(|id|SessionSearchHit{session_id:id.into(),project:"project".into(),snippet:"same searchable request".into()});
        assert_eq!(indexed_hits_in(&root,hits.into()).iter().map(|entry|entry.id.as_str()).collect::<Vec<_>>(),["outer-router"]);
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
    fn indexed_identity_opens_exact_owned_transcript_without_inventory_walk() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("projects");
        fs::create_dir_all(root.join("project")).unwrap();
        let mut transcript = b"{\"type\":\"user\",\"message\":{\"content\":\"indexed history\"}}\n".to_vec();
        transcript.extend_from_slice(b"{\"type\":\"user\",\"message\":{\"content\":\"");
        transcript.extend(std::iter::repeat_n(b'x', 20 * 1024));
        transcript.extend_from_slice(b"\"}}\n");
        fs::write(root.join("project/old-archive.jsonl"), transcript).unwrap();
        let mut file = File::open(root.join("project/old-archive.jsonl")).unwrap();
        assert!(!tail_matches(&mut file, "indexed history"));
        for index in 0..(MAX_FILES + 1) {
            fs::write(root.join(format!("project/junk-{index:04}.txt")), b"x").unwrap();
        }
        let hit = SessionSearchHit { session_id: "old-archive".into(), project: "project".into(),
            snippet: "indexed history".into() };
        let mut second = hit.clone();
        second.snippet = "another indexed excerpt".into();
        let found = indexed_hits_in(&root, vec![hit.clone(), second]);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].id, "old-archive");
        assert_eq!(found[0].search_snippets, ["indexed history", "another indexed excerpt"]);
        assert!(found[0].markdown.contains("indexed history"));
        fs::remove_file(root.join("project/old-archive.jsonl")).unwrap();
        symlink(temp.path().join("outside.jsonl"), root.join("project/old-archive.jsonl")).unwrap();
        fs::write(temp.path().join("outside.jsonl"), b"secret").unwrap();
        assert!(indexed_hits_in(&root, vec![hit]).is_empty());
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

    #[test]
    fn exact_transcript_delete_rejects_links_and_leaves_other_history() {
        use std::os::unix::fs::symlink;
        let temp = tempfile::tempdir().unwrap();
        let project = temp.path().join("project");
        fs::create_dir(&project).unwrap();
        let saved = project.join("saved.jsonl");
        let other = project.join("other.jsonl");
        fs::write(&saved, b"saved\n").unwrap();
        fs::write(&other, b"other\n").unwrap();
        assert!(delete_exact_in(temp.path(), "project", "saved", true).is_ok());
        assert!(!saved.exists());
        assert!(delete_exact_in(temp.path(), "project", "saved", false).is_ok());
        assert!(delete_exact_in(temp.path(), "absent-project", "saved", false).is_ok());
        assert!(delete_exact_in(&temp.path().join("absent-root"), "project", "saved", false).is_ok());
        assert_eq!(fs::read(&other).unwrap(), b"other\n");
        assert!(delete_exact_in(temp.path(), "project", "other", false).is_err());
        symlink(&other, &saved).unwrap();
        assert!(delete_exact_in(temp.path(), "project", "saved", true).is_err());
        assert_eq!(fs::read(&other).unwrap(), b"other\n");
        fs::remove_file(&saved).unwrap();
        fs::hard_link(&other, &saved).unwrap();
        assert!(delete_exact_in(temp.path(), "project", "saved", true).is_err());
        assert!(delete_exact_in(temp.path(), "../project", "other", true).is_err());
    }
}
