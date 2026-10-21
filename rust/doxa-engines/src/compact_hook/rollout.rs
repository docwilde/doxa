//! Complete source hashing with one bounded JSONL record in memory. Review
//! projections change only session metadata; original provider bytes stay put.
use super::{identity, SourceProof, MAX_ROLLOUT};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{fs, io::{self, BufRead, BufReader, Read, Write}, os::unix::fs::{MetadataExt, OpenOptionsExt}, path::Path};

pub(super) const MAX_LINE: usize = doxa_isolation::MAX_PROVIDER_REVIEW_LINE_BYTES;

pub(super) struct ProjectionProof { pub sha256: String, pub size: u64 }

pub(super) fn snapshot(path: &Path) -> io::Result<(Value, SourceProof)> {
    let (header, proof, _) = scan(path, None)?;
    Ok((header, proof))
}

pub(super) fn project(path: &Path, output: &mut fs::File, thread: &str, cwd: &str) -> io::Result<(Value, SourceProof, ProjectionProof)> {
    let (header, original, projected) = scan(path, Some((output, thread, cwd)))?;
    Ok((header, original, projected.ok_or_else(|| io::Error::other("missing projection proof"))?))
}

fn projected_write(output: &mut fs::File, bytes: &[u8], hash: &mut Sha256, size: &mut u64) -> io::Result<()> {
    output.write_all(bytes)?;
    // Hash the intended bytes, never reread a mutable copy to establish trust.
    hash.update(bytes); *size += bytes.len() as u64;
    Ok(())
}

fn scan(path: &Path, mut projection: Option<(&mut fs::File, &str, &str)>) -> io::Result<(Value, SourceProof, Option<ProjectionProof>)> {
    if !path.is_absolute() || fs::canonicalize(path)? != path { return Err(io::Error::other("noncanonical review source")); }
    let file = fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
    let before = file.metadata()?;
    if !before.is_file() || before.uid() != unsafe {libc::geteuid()} || before.nlink() != 1 || before.len() > MAX_ROLLOUT as u64 {
        return Err(io::Error::other("unsafe review source or history exceeds 256 MiB"));
    }
    let projected = projection.is_some();
    let mut projected_hash = Sha256::new();
    let mut projected_size = 0;
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
                projected_write(output, &serde_json::to_vec(&mapped)?, &mut projected_hash, &mut projected_size)?;
                if line.ends_with(b"\n") { projected_write(output, b"\n", &mut projected_hash, &mut projected_size)?; }
            }
            header = Some(value);
        } else if let Some((output, _, _)) = projection.as_mut() {
            projected_write(output, &line, &mut projected_hash, &mut projected_size)?;
        }
    }
    let after = input.get_ref().metadata()?;
    if total as u64 != before.len() || identity(&before) != identity(&after) || identity(&after) != identity(&fs::symlink_metadata(path)?) {
        return Err(io::Error::other("review source changed"));
    }
    let header = header.ok_or_else(|| io::Error::other("empty review source"))?;
    Ok((header, SourceProof {sha256:format!("{:x}", hash.finalize()), device:before.dev(), inode:before.ino(), size:before.len(),
        ctime:before.ctime(), ctime_nsec:before.ctime_nsec(), mtime:before.mtime(), mtime_nsec:before.mtime_nsec()},
        projected.then(||ProjectionProof {sha256:format!("{:x}",projected_hash.finalize()), size:projected_size})))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn projection_seal_comes_from_the_intended_bytes_not_a_later_mutable_copy() {
        let temporary = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(temporary.path()).unwrap();
        let original = root.join("source.jsonl"); let copy = root.join("review.jsonl");
        let raw = format!("{}\n{}\n",json!({"type":"session_meta","payload":{"id":"thread","cwd":"/workspace"}}),
            json!({"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"actual-message"}]}}));
        fs::write(&original,&raw).unwrap();
        let mut file = fs::File::create(&copy).unwrap();
        let (_, original_proof, written) = project(&original,&mut file,"thread","/host/project").unwrap();
        file.sync_all().unwrap();
        assert_eq!(snapshot(&copy).unwrap().1.sha256,written.sha256);
        assert_eq!(fs::metadata(&copy).unwrap().len(),written.size);
        let forged = fs::read_to_string(&copy).unwrap().replace("actual-message","benign-message");
        fs::write(&copy,forged).unwrap();
        assert_ne!(snapshot(&copy).unwrap().1.sha256,written.sha256,"a fresh proof of forged bytes cannot establish projection trust");
        assert_eq!(snapshot(&original).unwrap().1,original_proof);
    }
}
