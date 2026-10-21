//! Complete source hashing with one bounded JSONL record in memory. Review
//! projections change only session metadata; original provider bytes stay put.
use super::{identity, SourceProof, MAX_ROLLOUT};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{fs, io::{self, BufRead, BufReader, Read, Write}, os::unix::fs::{MetadataExt, OpenOptionsExt}, path::Path};

pub(super) const MAX_LINE: usize = doxa_isolation::MAX_PROVIDER_REVIEW_LINE_BYTES;

pub(super) fn snapshot(path: &Path) -> io::Result<(Value, SourceProof)> {
    scan(path, None)
}

pub(super) fn project(path: &Path, output: &mut fs::File, thread: &str, cwd: &str) -> io::Result<(Value, SourceProof)> {
    scan(path, Some((output, thread, cwd)))
}

fn scan(path: &Path, mut projection: Option<(&mut fs::File, &str, &str)>) -> io::Result<(Value, SourceProof)> {
    if !path.is_absolute() || fs::canonicalize(path)? != path { return Err(io::Error::other("noncanonical review source")); }
    let file = fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
    let before = file.metadata()?;
    if !before.is_file() || before.uid() != unsafe {libc::geteuid()} || before.nlink() != 1 || before.len() > MAX_ROLLOUT as u64 {
        return Err(io::Error::other("unsafe review source or history exceeds 256 MiB"));
    }
    let mut input = BufReader::new(file);
    let mut hash = Sha256::new();
    let mut total = 0usize;
    let mut line = Vec::new();
    let mut header = None;
    loop {
        line.clear();
        // Take bounds allocation before read_until, including a possible newline.
        let n = input.by_ref().take(MAX_LINE as u64 + 2).read_until(b'\n', &mut line)?;
        if n == 0 { break; }
        total = total.checked_add(n).ok_or_else(|| io::Error::other("review source size overflow"))?;
        if total > MAX_ROLLOUT || line.len() - usize::from(line.ends_with(b"\n")) > MAX_LINE {
            return Err(io::Error::other("Codex review history exceeds 256 MiB or JSONL record exceeds 8 MiB"));
        }
        hash.update(&line);
        if header.is_none() {
            let value: Value = serde_json::from_slice(&line)?;
            if let Some((output, thread, cwd)) = projection.as_mut() {
                if value["type"] != "session_meta" || value["payload"]["id"] != *thread || value["payload"]["cwd"] != "/workspace" {
                    return Err(io::Error::other("isolated review source thread or workspace differs"));
                }
                let mut mapped = value.clone();
                mapped["payload"]["cwd"] = Value::String((*cwd).to_owned());
                serde_json::to_writer(&mut **output, &mapped)?;
                if line.ends_with(b"\n") { output.write_all(b"\n")?; }
            }
            header = Some(value);
        } else if let Some((output, _, _)) = projection.as_mut() {
            output.write_all(&line)?;
        }
    }
    let after = input.get_ref().metadata()?;
    if total as u64 != before.len() || identity(&before) != identity(&after) || identity(&after) != identity(&fs::symlink_metadata(path)?) {
        return Err(io::Error::other("review source changed"));
    }
    let header = header.ok_or_else(|| io::Error::other("empty review source"))?;
    Ok((header, SourceProof {sha256:format!("{:x}", hash.finalize()), device:before.dev(), inode:before.ino(), size:before.len(),
        ctime:before.ctime(), ctime_nsec:before.ctime_nsec(), mtime:before.mtime(), mtime_nsec:before.mtime_nsec()}))
}
