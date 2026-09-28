//! Python 1.19-compatible DOXA session JSONL and Codex thread metadata.
//! Callers supply their engine's secret scrubber for every write.

use serde_json::{Map, Value};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};

pub const MAX_TRANSCRIPT_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_TRANSCRIPT_LINES: usize = 20_000;
pub const MAX_METADATA_BYTES: u64 = 64 * 1024;
pub const THREAD_SUFFIX: &str = ".codex.json";
pub const MAX_VENDOR_MESSAGES_BYTES: u64 = 16 * 1024 * 1024;
pub const MAX_VENDOR_MESSAGES: usize = 20_000;
pub const MAX_VENDOR_TRANSCRIPT_BYTES: u64 = 16 * 1024 * 1024;

fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidInput,
        "unsafe transcript path or identifier",
    )
}

fn bad_data() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid JSON record")
}

/// Matches doxa.identity's `[0-9A-Za-z][0-9A-Za-z-]{0,127}`.
pub fn valid_session_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 128
        && id
            .bytes()
            .enumerate()
            .all(|(i, b)| b.is_ascii_alphanumeric() || (i > 0 && b == b'-'))
}

fn valid_slug(slug: &str) -> bool {
    !slug.is_empty()
        && slug.len() <= 255
        && slug != "."
        && slug != ".."
        && !slug.contains('/')
        && !slug.contains('\\')
        && !slug.chars().any(char::is_control)
}

fn owned_dir(path: &Path) -> io::Result<()> {
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_dir() || meta.file_type().is_symlink() || meta.uid() != unsafe { libc::geteuid() } {
        return Err(invalid());
    }
    Ok(())
}

fn safe_root(path: &Path) -> io::Result<()> {
    if !path.is_absolute() {
        return Err(invalid());
    }
    let mut prefix = PathBuf::new();
    for component in path.components() {
        if matches!(component, Component::ParentDir | Component::CurDir) {
            return Err(invalid());
        }
        prefix.push(component);
        let meta = match fs::symlink_metadata(&prefix) {
            Ok(meta) => meta,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                fs::create_dir(&prefix)?;
                fs::symlink_metadata(&prefix)?
            }
            Err(error) => return Err(error),
        };
        if !meta.is_dir() || meta.file_type().is_symlink() {
            return Err(invalid());
        }
    }
    owned_dir(path)
}

fn checked_file(file: &File) -> io::Result<()> {
    let meta = file.metadata()?;
    if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() } || meta.nlink() != 1 {
        return Err(invalid());
    }
    Ok(())
}

fn open_read(path: &Path) -> io::Result<File> {
    let mut opts = OpenOptions::new();
    let file = opts
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    checked_file(&file)?;
    Ok(file)
}

fn open_directory(path: &Path) -> io::Result<File> {
    let file = OpenOptions::new().read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
    let meta = file.metadata()?;
    if !meta.is_dir() || meta.uid() != unsafe { libc::geteuid() } {
        return Err(invalid());
    }
    Ok(file)
}

fn complete_transcript_boundary(file: &mut File) -> io::Result<u64> {
    let bytes = file.metadata()?.len();
    if bytes == 0 { return Err(bad_data()); }
    file.seek(SeekFrom::End(-1))?;
    let mut end = [0];
    file.read_exact(&mut end)?;
    if end != [b'\n'] { return Err(bad_data()); }
    let mut cursor = bytes - 1;
    let mut chunks = Vec::new();
    let mut length = 0usize;
    while cursor > 0 {
        let count = cursor.min(8192) as usize;
        cursor -= count as u64;
        file.seek(SeekFrom::Start(cursor))?;
        let mut chunk = vec![0; count];
        file.read_exact(&mut chunk)?;
        let boundary = chunk.iter().rposition(|byte| *byte == b'\n');
        if let Some(index) = boundary { chunk.drain(..=index); }
        length += chunk.len();
        if length > MAX_TRANSCRIPT_BYTES { return Err(bad_data()); }
        chunks.push(chunk);
        if boundary.is_some() { break; }
    }
    let mut record = Vec::with_capacity(length);
    for chunk in chunks.into_iter().rev() { record.extend_from_slice(&chunk); }
    if !serde_json::from_slice::<Value>(&record).map_err(|_| bad_data())?.is_object()
        || file.metadata()?.len() != bytes {
        return Err(bad_data());
    }
    Ok(bytes)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CheckpointBarrier {
    TranscriptData,
    TranscriptEntry,
    ThreadData,
    ThreadEntry,
}

fn scrub_value(value: &mut Value, scrub: &impl Fn(&str) -> String) {
    match value {
        Value::String(s) => *s = scrub(s),
        Value::Array(items) => {
            for item in items {
                scrub_value(item, scrub);
            }
        }
        Value::Object(fields) => {
            for item in fields.values_mut() {
                scrub_value(item, scrub);
            }
        }
        _ => {}
    }
}

fn try_scrub_value(
    value: &mut Value,
    scrub: &mut impl FnMut(&str) -> io::Result<String>,
) -> io::Result<()> {
    match value {
        Value::String(s) => *s = scrub(s)?,
        Value::Array(items) => {
            for item in items {
                try_scrub_value(item, scrub)?;
            }
        }
        Value::Object(fields) => {
            for item in fields.values_mut() {
                try_scrub_value(item, scrub)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn validate_vendor_messages(messages: &[Value]) -> io::Result<()> {
    if messages.len() > MAX_VENDOR_MESSAGES || !messages.len().is_multiple_of(2) {
        return Err(bad_data());
    }
    let mut bytes = 0usize;
    for (index, message) in messages.iter().enumerate() {
        let fields = message.as_object().ok_or_else(bad_data)?;
        if fields.len() != 2
            || fields.get("role").and_then(Value::as_str)
                != Some(if index % 2 == 0 { "user" } else { "assistant" })
            || fields.get("content").and_then(Value::as_str).is_none()
        {
            return Err(bad_data());
        }
        bytes = bytes
            .checked_add(serde_json::to_vec(message)?.len())
            .ok_or_else(bad_data)?;
        if bytes as u64 > MAX_VENDOR_MESSAGES_BYTES {
            return Err(bad_data());
        }
    }
    Ok(())
}

pub struct TranscriptStore {
    dir: PathBuf,
    session_id: String,
}

impl TranscriptStore {
    /// `projects_dir` is LORE_PROJECTS_DIR; `slug` is Python's project_slug output.
    pub fn new(projects_dir: &Path, slug: &str, session_id: &str) -> io::Result<Self> {
        if !valid_slug(slug) || !valid_session_id(session_id) {
            return Err(invalid());
        }
        safe_root(projects_dir)?;
        let dir = projects_dir.join(slug);
        if !dir.exists() {
            fs::create_dir(&dir)?;
        }
        owned_dir(&dir)?;
        Ok(Self {
            dir,
            session_id: session_id.to_owned(),
        })
    }

    pub fn transcript_path(&self) -> PathBuf {
        self.dir.join(format!("{}.jsonl", self.session_id))
    }

    /// Return an owner-checked file boundary for a client's bounded restore.
    pub fn transcript_snapshot(&self) -> io::Result<Option<(PathBuf, u64)>> {
        owned_dir(&self.dir)?;
        let file = match open_read(&self.transcript_path()) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        Ok(Some((self.transcript_path(), file.metadata()?.len())))
    }

    /// Append one accepted vendor turn as two Python-shaped JSONL records.
    /// Scrub both records before opening the file; a write failure remains
    /// detectable on next resume by comparison with `.messages.json`.
    pub fn try_append_vendor_turn(
        &self,
        engine: &str,
        cwd: &str,
        prompt: &str,
        answer: &str,
        timestamp: &str,
        mut scrub: impl FnMut(&str) -> io::Result<String>,
    ) -> io::Result<()> {
        owned_dir(&self.dir)?;
        let user = serde_json::json!({"type":"user", "message":{"role":"user","content":prompt},
            "cwd":cwd,"sessionId":self.session_id,"timestamp":timestamp});
        let assistant = serde_json::json!({"type":"assistant",
            "message":{"role":"assistant","content":[{"type":"text","text":answer}]},
            "sessionId":self.session_id,"timestamp":timestamp});
        let mut bytes = Vec::new();
        for mut record in [user, assistant] {
            record["engine"] = Value::String(engine.to_owned());
            try_scrub_value(&mut record, &mut scrub)?;
            if record["engine"] != engine || record["sessionId"] != self.session_id {
                return Err(bad_data());
            }
            let line = serde_json::to_vec(&record)?;
            if line.len() > MAX_TRANSCRIPT_BYTES {
                return Err(bad_data());
            }
            bytes.extend_from_slice(&line);
            bytes.push(b'\n');
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(self.transcript_path())?;
        checked_file(&file)?;
        if file.metadata()?.len().saturating_add(bytes.len() as u64) > MAX_VENDOR_TRANSCRIPT_BYTES {
            return Err(bad_data());
        }
        if file.metadata()?.permissions().mode() & 0o077 != 0 {
            file.set_permissions(fs::Permissions::from_mode(0o600))?;
        }
        file.write_all(&bytes)?;
        file.sync_all()?;
        Ok(())
    }

    /// Reject a crash that left the replay file and JSONL at different turns.
    pub fn verify_vendor_transcript(&self, engine: &str, messages: &[Value]) -> io::Result<()> {
        owned_dir(&self.dir)?;
        let mut file = match open_read(&self.transcript_path()) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound && messages.is_empty() => {
                return Ok(())
            }
            Err(error) => return Err(error),
        };
        if file.metadata()?.len() > MAX_VENDOR_TRANSCRIPT_BYTES {
            return Err(bad_data());
        }
        let mut raw = Vec::new();
        file.read_to_end(&mut raw)?;
        if raw.last() != Some(&b'\n') {
            return Err(bad_data());
        }
        let mut found = Vec::new();
        for line in raw.split(|b| *b == b'\n').filter(|line| !line.is_empty()) {
            let record: Value = serde_json::from_slice(line).map_err(|_| bad_data())?;
            if record["engine"] != engine || record["sessionId"] != self.session_id {
                return Err(bad_data());
            }
            match record["type"].as_str() {
                Some("user") if record["message"]["role"] == "user" => {
                    found.push(
                        serde_json::json!({"role":"user","content":record["message"]["content"]}),
                    );
                }
                Some("assistant") if record["message"]["role"] == "assistant" => {
                    let blocks = record["message"]["content"]
                        .as_array()
                        .ok_or_else(bad_data)?;
                    if blocks.len() != 1 || blocks[0]["type"] != "text" {
                        return Err(bad_data());
                    }
                    found.push(serde_json::json!({"role":"assistant","content":blocks[0]["text"]}));
                }
                _ => return Err(bad_data()),
            }
        }
        if found != messages {
            return Err(bad_data());
        }
        Ok(())
    }
    pub fn thread_path(&self) -> PathBuf {
        self.dir
            .join(format!("{}{}", self.session_id, THREAD_SUFFIX))
    }

    pub fn vendor_messages_path(&self) -> PathBuf {
        self.dir.join(format!("{}.messages.json", self.session_id))
    }

    pub fn vendor_context_path(&self) -> PathBuf {
        self.dir.join(format!("{}.context.json", self.session_id))
    }

    /// A context summary is a DOXA optimization, anchored to complete durable
    /// original turns. It never substitutes for the transcript or replay file.
    pub fn read_vendor_context(&self, engine: &str, messages: &[Value]) -> io::Result<Option<(usize, String)>> {
        use sha2::{Digest, Sha256};
        owned_dir(&self.dir)?;
        let mut file = match open_read(&self.vendor_context_path()) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        if file.metadata()?.len() > 128 * 1024 { return Err(bad_data()); }
        let mut raw = Vec::new(); Read::by_ref(&mut file).take(128 * 1024 + 1).read_to_end(&mut raw)?;
        let value: Value = serde_json::from_slice(&raw)?;
        let count = value["compacted_messages"].as_u64().and_then(|n| usize::try_from(n).ok()).filter(|n| *n > 0 && n.is_multiple_of(2) && *n <= messages.len()).ok_or_else(bad_data)?;
        let summary = value["summary"].as_str().filter(|s| !s.trim().is_empty() && s.len() <= 64 * 1024).ok_or_else(bad_data)?;
        let hash = format!("{:x}", Sha256::digest(serde_json::to_vec(&messages[..count])?));
        if raw.len() > 128 * 1024 || value["version"] != 1 || value["semantics"] != "doxa_managed_summary"
            || value["engine"] != engine || value["session_id"] != self.session_id || value["prefix_sha256"] != hash { return Err(bad_data()); }
        Ok(Some((count,summary.to_owned())))
    }

    /// Persist only after a reviewed source still matches. `unchanged` must
    /// compare the exact transcript proof and original saved messages identity.
    /// A failed summary commit leaves the original replay files untouched.
    pub fn try_write_vendor_context(&self, engine: &str, messages: &[Value], summary: &str, reviewed_source: &Value,
        mut unchanged: impl FnMut() -> io::Result<bool>) -> io::Result<()> {
        use sha2::{Digest, Sha256};
        owned_dir(&self.dir)?; validate_vendor_messages(messages)?;
        if messages.is_empty() || summary.trim().is_empty() || summary.len() > 64 * 1024 || !reviewed_source.is_object() { return Err(bad_data()); }
        let directory = open_directory(&self.dir)?;
        let identity = directory.metadata()?;
        let value = serde_json::json!({"version":1,"semantics":"doxa_managed_summary","engine":engine,
            "session_id":self.session_id,"compacted_messages":messages.len(),
            "prefix_sha256":format!("{:x}",Sha256::digest(serde_json::to_vec(messages)?)),
            "reviewed_source":reviewed_source,"summary":summary});
        // The fd keeps all writes anchored to this exact owned directory even
        // if someone swaps its path while the provider is summarizing.
        let anchored = PathBuf::from(format!("/proc/self/fd/{}", std::os::fd::AsRawFd::as_raw_fd(&directory)));
        let mut temp = tempfile::Builder::new().prefix(".context-").tempfile_in(&anchored)?;
        temp.as_file().set_permissions(fs::Permissions::from_mode(0o600))?;
        temp.write_all(&serde_json::to_vec(&value)?)?; temp.as_file().sync_all()?;
        let current = open_directory(&self.dir)?.metadata()?;
        if (identity.dev(),identity.ino()) != (current.dev(),current.ino()) || !unchanged()? { return Err(io::Error::other("reviewed context source changed")); }
        let current = open_directory(&self.dir)?.metadata()?;
        if (identity.dev(),identity.ino()) != (current.dev(),current.ino()) { return Err(io::Error::other("context directory changed")); }
        temp.persist(anchored.join(format!("{}.context.json",self.session_id))).map_err(|error| error.error)?;
        directory.sync_all()
    }

    /// Read Python's vendor envelope. Older envelopes omit session_id/model;
    /// when present those identity fields must match exactly. Bare arrays do
    /// not identify a vendor and are unsafe to resume.
    pub fn read_vendor_messages(
        &self,
        engine: &str,
        model: &str,
    ) -> io::Result<Option<Vec<Value>>> {
        owned_dir(&self.dir)?;
        let mut file = match open_read(&self.vendor_messages_path()) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        if file.metadata()?.len() > MAX_VENDOR_MESSAGES_BYTES {
            return Err(bad_data());
        }
        let mut raw = Vec::new();
        file.read_to_end(&mut raw)?;
        let value: Value = serde_json::from_slice(&raw).map_err(|_| bad_data())?;
        let fields = value.as_object().ok_or_else(bad_data)?;
        if fields.get("engine").and_then(Value::as_str) != Some(engine)
            || fields
                .get("session_id")
                .is_some_and(|v| v.as_str() != Some(&self.session_id))
            || fields
                .get("model")
                .is_some_and(|v| v.as_str() != Some(model))
        {
            return Err(bad_data());
        }
        let messages = fields
            .get("messages")
            .and_then(Value::as_array)
            .ok_or_else(bad_data)?;
        validate_vendor_messages(messages)?;
        Ok(Some(messages.clone()))
    }

    /// Scrub the complete history before creating a private replacement file.
    pub fn try_write_vendor_messages(
        &self,
        engine: &str,
        model: &str,
        messages: &[Value],
        mut scrub: impl FnMut(&str) -> io::Result<String>,
    ) -> io::Result<Vec<Value>> {
        owned_dir(&self.dir)?;
        validate_vendor_messages(messages)?;
        let clean = messages.to_vec();
        let mut value = serde_json::json!({"engine":engine,"session_id":self.session_id,
            "model":model,"messages":clean});
        try_scrub_value(&mut value, &mut scrub)?;
        if value["engine"] != engine
            || value["session_id"] != self.session_id
            || value["model"] != model
        {
            return Err(bad_data());
        }
        let clean = value["messages"].as_array().ok_or_else(bad_data)?.clone();
        validate_vendor_messages(&clean)?;
        let bytes = serde_json::to_vec(&value)?;
        if bytes.len() as u64 > MAX_VENDOR_MESSAGES_BYTES {
            return Err(bad_data());
        }
        let mut temp = tempfile::Builder::new()
            .prefix(&format!(".{}.messages.", self.session_id))
            .suffix(".tmp")
            .tempfile_in(&self.dir)?;
        checked_file(temp.as_file())?;
        temp.write_all(&bytes)?;
        temp.as_file().sync_all()?;
        temp.persist(self.vendor_messages_path())
            .map_err(|error| error.error)?;
        Ok(clean)
    }

    /// Append one original record with the Python engine override, after scrubbing every string value.
    pub fn append(
        &self,
        mut record: Value,
        engine: &str,
        scrub: impl Fn(&str) -> String,
    ) -> io::Result<()> {
        owned_dir(&self.dir)?;
        let fields = record.as_object_mut().ok_or_else(bad_data)?;
        fields.insert("engine".into(), Value::String(engine.to_owned()));
        scrub_value(&mut record, &scrub);
        let mut bytes = serde_json::to_vec(&record)?;
        if bytes.len() > MAX_TRANSCRIPT_BYTES {
            return Err(bad_data());
        }
        bytes.push(b'\n');
        let mut opts = OpenOptions::new();
        let mut file = opts
            .create(true)
            .append(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(self.transcript_path())?;
        checked_file(&file)?;
        // Python 1.x may have created this file under a permissive umask.
        // Tighten that existing inode before appending a new record.
        if file.metadata()?.permissions().mode() & 0o077 != 0 {
            file.set_permissions(fs::Permissions::from_mode(0o600))?;
        }
        file.write_all(&bytes)
    }

    /// Fallible scrub boundary for a sidecar. No bytes are written if any
    /// string cannot be scrubbed, including nested provider output.
    pub fn try_append(
        &self,
        mut record: Value,
        engine: &str,
        mut scrub: impl FnMut(&str) -> io::Result<String>,
    ) -> io::Result<()> {
        owned_dir(&self.dir)?;
        let fields = record.as_object_mut().ok_or_else(bad_data)?;
        fields.insert("engine".into(), Value::String(engine.to_owned()));
        try_scrub_value(&mut record, &mut scrub)?;
        let mut bytes = serde_json::to_vec(&record)?;
        if bytes.len() > MAX_TRANSCRIPT_BYTES {
            return Err(bad_data());
        }
        bytes.push(b'\n');
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(self.transcript_path())?;
        checked_file(&file)?;
        if file.metadata()?.permissions().mode() & 0o077 != 0 {
            file.set_permissions(fs::Permissions::from_mode(0o600))?;
        }
        file.write_all(&bytes)
    }

    /// Read the same bounded tail as doxa.transcript._records. Malformed lines are skipped.
    pub fn read_records(&self) -> io::Result<Vec<Value>> {
        owned_dir(&self.dir)?;
        let mut file = match open_read(&self.transcript_path()) {
            Ok(file) => file,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(e) => return Err(e),
        };
        let size = file.metadata()?.len();
        let start = size.saturating_sub(MAX_TRANSCRIPT_BYTES as u64);
        file.seek(SeekFrom::Start(start))?;
        let mut raw = Vec::new();
        file.take(MAX_TRANSCRIPT_BYTES as u64)
            .read_to_end(&mut raw)?;
        if start > 0 {
            match raw.iter().position(|b| *b == b'\n') {
                Some(pos) => {
                    raw.drain(..=pos);
                }
                None => return Ok(Vec::new()),
            }
        }
        Ok(raw
            .split(|b| *b == b'\n')
            .rev()
            .take(MAX_TRANSCRIPT_LINES)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .filter_map(|line| serde_json::from_slice::<Value>(line).ok())
            .filter(Value::is_object)
            .collect())
    }

    /// Read a recorded Codex thread. Unknown keys survive through `write_thread`.
    pub fn read_thread(&self) -> io::Result<Option<Value>> {
        owned_dir(&self.dir)?;
        let mut file = match open_read(&self.thread_path()) {
            Ok(file) => file,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e),
        };
        if file.metadata()?.len() > MAX_METADATA_BYTES {
            return Err(bad_data());
        }
        let mut raw = Vec::new();
        file.read_to_end(&mut raw)?;
        let value: Value = serde_json::from_slice(&raw).map_err(|_| bad_data())?;
        if !value.is_object() {
            return Err(bad_data());
        }
        Ok(Some(value))
    }

    pub fn recorded_thread_id(&self) -> io::Result<Option<String>> {
        // Python's _recorded_thread treats truncated or future non-object data
        // as an unknown thread, never as a new thread to invent.
        match self.read_thread() {
            Ok(value) => Ok(value.and_then(|v| {
                v.get("thread_id")?
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .map(str::to_owned)
            })),
            Err(e) if e.kind() == io::ErrorKind::InvalidData => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// Verify checkpoints written by native Codex before resuming. Legacy
    /// Python/native metadata without this field keeps its previous contract.
    /// A checkpoint covers the exact complete JSONL boundary, not a tail that
    /// the permissive display reader might silently skip after a crash.
    pub fn verify_thread_checkpoint(&self, metadata: &Value) -> io::Result<()> {
        let Some(bytes) = metadata.get("transcript_bytes") else { return Ok(()); };
        let bytes = bytes.as_u64().filter(|bytes| *bytes > 0).ok_or_else(bad_data)?;
        owned_dir(&self.dir)?;
        let mut file = open_read(&self.transcript_path())?;
        if complete_transcript_boundary(&mut file)? != bytes { return Err(bad_data()); }
        Ok(())
    }

    /// Merge metadata keys with the existing object, retaining future fields and writing atomically.
    pub fn write_thread(
        &self,
        updates: Map<String, Value>,
        scrub: impl Fn(&str) -> String,
    ) -> io::Result<()> {
        self.try_write_thread(updates, |text| Ok(scrub(text)))
    }

    /// Fallible variant for the LORE sidecar. Scrub before creating the temp file.
    pub fn try_write_thread(
        &self,
        updates: Map<String, Value>,
        mut scrub: impl FnMut(&str) -> io::Result<String>,
    ) -> io::Result<()> {
        self.try_write_thread_with_sync(updates, &mut scrub, &mut |file, _| file.sync_all())
    }

    fn try_write_thread_with_sync(
        &self,
        updates: Map<String, Value>,
        scrub: &mut impl FnMut(&str) -> io::Result<String>,
        sync: &mut impl FnMut(&File, CheckpointBarrier) -> io::Result<()>,
    ) -> io::Result<()> {
        owned_dir(&self.dir)?;
        let mut base = self
            .read_thread()?
            .and_then(|v| v.as_object().cloned())
            .unwrap_or_default();
        base.extend(updates);
        if base
            .get("thread_id")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
        {
            return Err(bad_data());
        }
        let mut value = Value::Object(base);
        try_scrub_value(&mut value, scrub)?;
        let directory = open_directory(&self.dir)?;
        if value["turn_incomplete"] == false {
            let mut transcript = open_read(&self.transcript_path())?;
            let bytes = complete_transcript_boundary(&mut transcript)?;
            // The clean marker must never reach storage ahead of either the
            // JSONL content or its newly-created directory entry.
            sync(&transcript, CheckpointBarrier::TranscriptData)?;
            sync(&directory, CheckpointBarrier::TranscriptEntry)?;
            value["transcript_bytes"] = Value::from(bytes);
        } else if value["turn_incomplete"] == true {
            value.as_object_mut().expect("thread metadata object").remove("transcript_bytes");
        }
        let bytes = serde_json::to_vec(&value)?;
        if bytes.len() as u64 > MAX_METADATA_BYTES {
            return Err(bad_data());
        }
        let mut temp = tempfile::Builder::new()
            .prefix(&format!(".{}.codex.", self.session_id))
            .suffix(".tmp")
            .tempfile_in(&self.dir)?;
        checked_file(temp.as_file())?;
        temp.write_all(&bytes)?;
        sync(temp.as_file(), CheckpointBarrier::ThreadData)?;
        temp.persist(self.thread_path())
            .map_err(|error| error.error)?;
        // This barrier also makes the dirty guard durable before the caller
        // admits a provider turn. Rename alone is not a durable transaction.
        sync(&directory, CheckpointBarrier::ThreadEntry)
    }
}

#[cfg(test)]
mod checkpoint_tests {
    use super::*;
    use serde_json::json;

    fn fixture() -> (tempfile::TempDir, TranscriptStore) {
        let root = tempfile::tempdir().unwrap();
        let store = TranscriptStore::new(root.path(), "project", "session-1").unwrap();
        store.append(json!({"type":"user","message":{"content":"owned prompt"}}),
                     "codex", str::to_owned).unwrap();
        store.append(json!({"type":"assistant","message":{"content":"owned ü answer"}}),
                     "codex", str::to_owned).unwrap();
        store.write_thread(fields(true), str::to_owned).unwrap();
        (root, store)
    }

    fn fields(incomplete: bool) -> Map<String, Value> {
        serde_json::from_value(json!({"thread_id":"thread-1","turn_incomplete":incomplete,
                                      "future":{"keep":true}})).unwrap()
    }

    #[test]
    fn clean_checkpoint_syncs_owned_transcript_and_directory_before_metadata_commit() {
        let (_root, store) = fixture();
        let mut stages = Vec::new();
        store.try_write_thread_with_sync(fields(false), &mut |text| Ok(text.to_owned()),
            &mut |file, stage| {
                stages.push(stage);
                if stage == CheckpointBarrier::ThreadEntry {
                    let checkpoint = store.read_thread()?.unwrap();
                    assert_eq!(checkpoint["turn_incomplete"], false);
                    assert_eq!(checkpoint["transcript_bytes"].as_u64(),
                               Some(fs::metadata(store.transcript_path())?.len()));
                    store.verify_thread_checkpoint(&checkpoint)?;
                } else {
                    assert_eq!(store.read_thread()?.unwrap()["turn_incomplete"], true,
                               "clean metadata appeared before {stage:?}");
                }
                if stage == CheckpointBarrier::TranscriptData {
                    assert!(file.metadata()?.is_file());
                    assert_eq!(file.metadata()?.ino(), fs::metadata(store.transcript_path())?.ino());
                }
                if matches!(stage, CheckpointBarrier::TranscriptEntry | CheckpointBarrier::ThreadEntry) {
                    assert!(file.metadata()?.is_dir());
                }
                file.sync_all()
            }).unwrap();
        assert_eq!(stages, [CheckpointBarrier::TranscriptData, CheckpointBarrier::TranscriptEntry,
                           CheckpointBarrier::ThreadData, CheckpointBarrier::ThreadEntry]);
        assert_eq!(store.read_thread().unwrap().unwrap()["future"]["keep"], true);
    }

    #[test]
    fn checkpoint_barrier_failures_keep_prior_dirty_metadata_and_allow_safe_retry() {
        for failed in [CheckpointBarrier::TranscriptData, CheckpointBarrier::TranscriptEntry,
                       CheckpointBarrier::ThreadData] {
            let (_root, store) = fixture();
            let previous = fs::read(store.thread_path()).unwrap();
            let mut stages = Vec::new();
            let result = store.try_write_thread_with_sync(fields(false), &mut |text| Ok(text.to_owned()),
                &mut |file, stage| {
                    stages.push(stage);
                    if stage == failed { return Err(io::Error::other("owned sync fault")); }
                    file.sync_all()
                });
            assert!(result.is_err(), "{failed:?}");
            assert_eq!(stages.last(), Some(&failed));
            assert_eq!(fs::read(store.thread_path()).unwrap(), previous);
            assert_eq!(store.read_thread().unwrap().unwrap()["turn_incomplete"], true);
            assert!(fs::read_dir(&store.dir).unwrap().all(|entry|
                !entry.unwrap().file_name().to_string_lossy().ends_with(".tmp")));
            store.write_thread(fields(false), str::to_owned).unwrap();
            store.verify_thread_checkpoint(&store.read_thread().unwrap().unwrap()).unwrap();
        }
    }

    #[test]
    fn final_directory_sync_failure_is_reported_after_durable_transcript_checkpoint() {
        let (_root, store) = fixture();
        let mut stages = Vec::new();
        let result = store.try_write_thread_with_sync(fields(false), &mut |text| Ok(text.to_owned()),
            &mut |file, stage| {
                stages.push(stage);
                if stage == CheckpointBarrier::ThreadEntry {
                    return Err(io::Error::other("owned rename durability fault"));
                }
                file.sync_all()
            });
        assert!(result.is_err());
        // A rename can be visible despite this error, but it cannot advertise
        // transcript data that was not already synced. No success is returned.
        assert_eq!(stages, [CheckpointBarrier::TranscriptData, CheckpointBarrier::TranscriptEntry,
                           CheckpointBarrier::ThreadData, CheckpointBarrier::ThreadEntry]);
        store.verify_thread_checkpoint(&store.read_thread().unwrap().unwrap()).unwrap();
    }

    #[test]
    fn dirty_guard_syncs_metadata_and_rename_before_returning_to_provider_admission() {
        let (_root, store) = fixture();
        store.write_thread(fields(false), str::to_owned).unwrap();
        let mut stages = Vec::new();
        store.try_write_thread_with_sync(fields(true), &mut |text| Ok(text.to_owned()),
            &mut |file, stage| {
                stages.push(stage);
                if stage == CheckpointBarrier::ThreadData {
                    assert_eq!(store.read_thread()?.unwrap()["turn_incomplete"], false);
                } else {
                    let metadata = store.read_thread()?.unwrap();
                    assert_eq!(metadata["turn_incomplete"], true);
                    assert!(metadata.get("transcript_bytes").is_none());
                }
                file.sync_all()
            }).unwrap();
        assert_eq!(stages, [CheckpointBarrier::ThreadData, CheckpointBarrier::ThreadEntry]);
    }
}
