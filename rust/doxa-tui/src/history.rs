//! Render the daemon's persisted JSONL conversation for the early Rust UI.
//! This view is bounded; the JSONL file remains the complete record.

use serde_json::Value;
use crate::transport::TranscriptSnapshot;
use std::fs::{self, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

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
    if let Some(path) = std::env::var_os("LORE_PROJECTS_DIR").filter(|v| !v.is_empty()) {
        return Some(PathBuf::from(path));
    }
    if let Some(root) = std::env::var_os("LORE_ROOT").filter(|v| !v.is_empty()) {
        return Some(PathBuf::from(root).join("projects"));
    }
    std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".claude/lore/projects"))
}

fn owned_dir(path: &Path, uid: u32) -> bool {
    fs::symlink_metadata(path).is_ok_and(|meta| meta.is_dir() && meta.uid() == uid && !meta.file_type().is_symlink())
}

fn read_offline(path: &Path, uid: u32) -> Option<String> {
    let before = fs::symlink_metadata(path).ok()?;
    if !before.is_file() || before.uid() != uid || before.file_type().is_symlink() { return None; }
    let mut file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path).ok()?;
    let meta = file.metadata().ok()?;
    if !meta.is_file() || meta.uid() != uid || meta.ino() != before.ino() || meta.dev() != before.dev() { return None; }
    let start = meta.len().saturating_sub(MAX_FILE_BYTES);
    file.seek(SeekFrom::Start(start)).ok()?;
    let mut bytes = Vec::new();
    file.take(MAX_FILE_BYTES + 1).read_to_end(&mut bytes).ok()?;
    if bytes.len() as u64 > MAX_FILE_BYTES { bytes.truncate(MAX_FILE_BYTES as usize); }
    if start > 0 {
        let cut = bytes.iter().position(|byte| *byte == b'\n')? + 1;
        bytes.drain(..cut);
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
    if !owned_dir(root, uid) { return Vec::new(); }
    let Ok(projects) = fs::read_dir(root) else { return Vec::new(); };
    let mut candidates = Vec::new();
    let mut visited = 0;
    for project in projects.flatten().take(MAX_PROJECTS) {
        let dir = project.path();
        if !owned_dir(&dir, uid) { continue; }
        let Ok(files) = fs::read_dir(&dir) else { continue; };
        for file in files.flatten() {
            if visited == MAX_FILES { break; }
            visited += 1;
            let path = file.path();
            if path.extension().is_none_or(|ext| ext != "jsonl") { continue; }
            let Some(id) = path.file_stem().and_then(|stem| stem.to_str()) else { continue; };
            if !crate::discovery::valid_id(id) { continue; }
            let Ok(meta) = fs::symlink_metadata(&path) else { continue; };
            if !meta.is_file() || meta.uid() != uid { continue; }
            let stamp = meta.modified().ok();
            candidates.push((stamp, id.to_owned(), project.file_name().to_string_lossy().into_owned(), path));
        }
        if visited == MAX_FILES { break; }
    }
    candidates.sort_by(|a, b| b.0.cmp(&a.0));
    candidates.into_iter().take(MAX_OFFLINE).filter_map(|(_, id, project, path)| {
        let markdown = read_offline(&path, uid)?;
        Some(OfflineSession { id, project, markdown })
    }).collect()
}

const MAX_TURNS: usize = 40;
const MAX_TEXT_CHARS: usize = 20_000;
const MAX_VIEW_BYTES: usize = 480 * 1024;

#[derive(Default)]
struct Turn { prompt: String, answer: String, tools: Vec<String> }

pub fn render(snapshot: &TranscriptSnapshot) -> String {
    let mut turns: Vec<Turn> = Vec::new();
    for line in snapshot.bytes.split(|byte| *byte == b'\n') {
        let Ok(record) = serde_json::from_slice::<Value>(line) else { continue };
        let kind = record["type"].as_str().unwrap_or("");
        let content = &record["message"]["content"];
        if kind == "user" {
            if let Some(prompt) = content.as_str() {
                turns.push(Turn { prompt: prompt.to_owned(), ..Turn::default() });
                continue;
            }
        }
        if kind != "assistant" { continue; }
        let Some(blocks) = content.as_array() else { continue };
        if turns.is_empty() { turns.push(Turn::default()); }
        let turn = turns.last_mut().unwrap();
        for block in blocks {
            match block["type"].as_str() {
                Some("text") => turn.answer.push_str(block["text"].as_str().unwrap_or("")),
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
    use std::os::unix::fs::symlink;
    #[test]
    fn restores_prompts_and_assistant_text_without_tool_result_turns() {
        let lines = concat!(
            "{\"type\":\"user\",\"message\":{\"content\":\"first?\"}}\n",
            "{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"yes\"},{\"type\":\"tool_use\",\"name\":\"Search\"}]}}\n",
            "{\"type\":\"user\",\"message\":{\"content\":[{\"type\":\"tool_result\",\"content\":\"found\"}]}}\n",
            "{\"type\":\"user\",\"message\":{\"content\":\"second?\"}}\n",
        );
        let rendered = render(&TranscriptSnapshot { bytes: lines.as_bytes().to_vec(), earlier_bytes_omitted: false });
        assert!(rendered.contains("first?"));
        assert!(rendered.contains("yes"));
        assert!(rendered.contains("[Tool: Search]"));
        assert!(rendered.contains("second?"));
        assert!(!rendered.contains("found"));
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
}
