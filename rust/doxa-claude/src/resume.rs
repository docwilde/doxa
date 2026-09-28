//! Conservative import proof for the alpha36 SDK's two durable Claude logs.
//! No transcript or CLI state is copied, rewritten, or searched outside DOXA's
//! private configuration. A missing native checkpoint is never a new session.
use serde_json::Value;
use std::{
    fs::{self, OpenOptions},
    io::{self, BufRead, BufReader},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Path,
};
const MAX_LOG: u64 = 64 * 1024 * 1024;
const MAX_LINE: u64 = 1024 * 1024;
fn owned_dir(path: &Path) -> io::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_dir() || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o022 != 0 {
        return Err(io::Error::other("unowned legacy state directory"));
    }
    Ok(())
}
fn log(path: &Path, mut row: impl FnMut(Value) -> io::Result<()>) -> io::Result<()> {
    owned_dir(
        path.parent()
            .ok_or_else(|| io::Error::other("missing parent"))?,
    )?;
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.nlink() != 1
        || (meta.mode() & 0o022 != 0
            && fs::symlink_metadata(path.parent().unwrap())?.mode() & 0o077 != 0)
        || meta.len() == 0
        || meta.len() > MAX_LOG
    {
        return Err(io::Error::other("unowned or oversized legacy state"));
    }
    let mut reader = BufReader::new(file);
    let mut total = 0;
    loop {
        let mut bytes = Vec::new();
        let count = std::io::Read::take(&mut reader, MAX_LINE + 1).read_until(b'\n', &mut bytes)?;
        if count == 0 {
            break;
        }
        total += count as u64;
        if count as u64 > MAX_LINE || bytes.last() != Some(&b'\n') || total > MAX_LOG {
            return Err(io::Error::other("incomplete legacy state"));
        }
        row(serde_json::from_slice(&bytes).map_err(io::Error::other)?)?;
    }
    if total != meta.len() {
        return Err(io::Error::other("legacy state changed during proof"));
    }
    Ok(())
}
pub fn verify_legacy(
    config: &Path,
    transcript: &Path,
    session: &str,
    cwd: &Path,
) -> io::Result<()> {
    if !crate::cli::canonical_session_id(session) {
        return Err(io::Error::other("invalid Claude identity"));
    }
    owned_dir(config)?;
    owned_dir(&config.join("projects"))?;
    let cwd = cwd
        .to_str()
        .ok_or_else(|| io::Error::other("invalid cwd"))?;
    let mut source_user = false;
    let mut source_assistant = false;
    let mut last_assistant = false;
    log(transcript, |row| {
        if row["engine"] != "claude" || row["sessionId"] != session {
            return Err(io::Error::other(
                "legacy transcript provider identity changed",
            ));
        }
        if let Some(record_cwd) = row["cwd"].as_str() {
            if record_cwd != cwd {
                return Err(io::Error::other("legacy transcript workspace changed"));
            }
        }
        if row["type"] == "user" && row["message"]["content"].is_string() {
            source_user |= row["cwd"] == cwd;
        }
        source_assistant |= row["type"] == "assistant";
        last_assistant = row["type"] == "assistant";
        Ok(())
    })?;
    if !source_user || !source_assistant || !last_assistant {
        return Err(io::Error::other("legacy turn completion is unproven"));
    }
    let mut matches = 0;
    for entry in fs::read_dir(config.join("projects"))?.take(4097) {
        let entry = entry?;
        let path = entry.path();
        if !entry.file_type()?.is_dir() {
            continue;
        }
        let candidate = path.join(format!("{session}.jsonl"));
        if !candidate.try_exists()? {
            continue;
        }
        let mut provider_user = false;
        let mut provider_assistant = false;
        let mut workspace = false;
        log(&candidate, |row| {
            if row.get("sessionId").is_some_and(|id| id != session) {
                return Err(io::Error::other("legacy CLI provider identity changed"));
            }
            if let Some(record_cwd) = row["cwd"].as_str() {
                if record_cwd != cwd {
                    return Err(io::Error::other("legacy CLI workspace changed"));
                }
                workspace = true;
            }
            provider_user |= row["type"] == "user" && row["sessionId"] == session;
            provider_assistant |= row["type"] == "assistant" && row["sessionId"] == session;
            Ok(())
        })?;
        if !provider_user || !provider_assistant || !workspace {
            return Err(io::Error::other("legacy CLI source is unproven"));
        }
        matches += 1;
    }
    if matches != 1 {
        return Err(io::Error::other("legacy CLI source missing or ambiguous"));
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn import_requires_both_owned_complete_logs_and_matching_identity() {
        let dir = tempfile::tempdir().unwrap();
        let config = dir.path().join("claude-cli");
        let project = config.join("projects/project");
        fs::create_dir_all(&project).unwrap();
        for path in [&config, &config.join("projects"), &project] {
            fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let id = "0b256c09-8d74-4865-9be0-4e6d24384551";
        let transcript = dir.path().join("transcript.jsonl");
        let provider = project.join(format!("{id}.jsonl"));
        let body = format!(
            "{}\n{}\n",
            json!({"type":"user","engine":"claude","sessionId":id,"cwd":dir.path(),"message":{"content":"saved task"}}),
            json!({"type":"assistant","engine":"claude","sessionId":id,"cwd":dir.path(),"message":{"content":[]}})
        );
        fs::write(&transcript, &body).unwrap();
        fs::write(&provider, &body).unwrap();
        for path in [&transcript, &provider] {
            fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
        }
        assert!(verify_legacy(&config, &transcript, id, dir.path()).is_ok());
        fs::write(&provider, body.trim_end()).unwrap();
        assert!(verify_legacy(&config, &transcript, id, dir.path()).is_err());
        fs::write(&provider, &body).unwrap();
        fs::remove_file(&provider).unwrap();
        std::os::unix::fs::symlink(&transcript, &provider).unwrap();
        assert!(verify_legacy(&config, &transcript, id, dir.path()).is_err());
    }
}
