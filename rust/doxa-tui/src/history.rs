//! Render the daemon's persisted JSONL conversation for the early Rust UI.
//! This view is bounded; the JSONL file remains the complete record.

use serde_json::Value;
use crate::transport::TranscriptSnapshot;
use std::ffi::{CStr, OsStr, OsString};
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::ffi::OsStrExt;
use std::os::fd::{AsRawFd, FromRawFd};
use std::path::{Path, PathBuf};
use std::time::SystemTime;

const MAX_PROJECTS: usize = 128;
const MAX_FILES: usize = 2048;
const MAX_OFFLINE: usize = 64;
const MAX_FILE_BYTES: u64 = 512 * 1024;

#[derive(Clone, Debug)]
pub struct OfflineSession {
    pub id: String,
    pub project: String,
    pub markdown: String,
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

/// Scan only LORE transcript filenames and load bounded tails of the newest files.
/// Call on a worker thread; this reads no paths supplied by the history UI.
pub fn discover() -> Vec<OfflineSession> {
    let Some(root) = projects_dir() else { return Vec::new(); };
    discover_in(&root)
}

fn discover_in(root: &Path) -> Vec<OfflineSession> {
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
            let Some(open_file) = open_at(&dir, &name, libc::O_RDONLY | libc::O_NONBLOCK) else { continue; };
            let Ok(meta) = open_file.metadata() else { continue; };
            if !meta.is_file() || meta.uid() != uid { continue; }
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
    candidates.into_iter().filter_map(|(_, id, project, file)| {
        let markdown = read_offline(file, uid)?;
        Some(OfflineSession { id, project, markdown })
    }).collect()
}

const MAX_TURNS: usize = 40;
const MAX_TEXT_CHARS: usize = 20_000;
const MAX_VIEW_BYTES: usize = 480 * 1024;

#[derive(Default)]
struct Turn { prompt: String, answer: String, tools: Vec<String> }

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
                    let name = block["name"].as_str().unwrap_or("tool");
                    turn.tools.push(format!("Tool: {}", name.replace('\n', " ")));
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
        if !turn.answer.is_empty() {
            out.push_str("**Assistant:**\n\n");
            let shortened: String = turn.answer.chars().take(MAX_TEXT_CHARS).collect();
            out.push_str(&shortened);
            if shortened.len() < turn.answer.len() { out.push_str("\n[Assistant text shortened in this view]"); }
            out.push_str("\n\n");
        }
        for tool in turn.tools.iter().take(30) {
            out.push_str(&format!("[{tool}]\n\n"));
        }
        if turn.tools.len() > 30 { out.push_str("[Additional tools omitted from this view]\n\n"); }
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
    use std::os::unix::fs::symlink;

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
        assert!(rendered.contains("[Tool: Search]"));
        assert!(rendered.contains("second?\n\nmore detail"));
        assert!(!rendered.contains("found"));
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
        let found = discover_in(&root);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].id, "offline-1");
        assert!(found[0].markdown.contains("saved question"));
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
