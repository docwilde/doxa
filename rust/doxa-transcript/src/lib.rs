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
    pub fn thread_path(&self) -> PathBuf {
        self.dir
            .join(format!("{}{}", self.session_id, THREAD_SUFFIX))
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

    /// Merge metadata keys with the existing object, retaining future fields and writing atomically.
    pub fn write_thread(
        &self,
        updates: Map<String, Value>,
        scrub: impl Fn(&str) -> String,
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
        scrub_value(&mut value, &scrub);
        let bytes = serde_json::to_vec(&value)?;
        if bytes.len() as u64 > MAX_METADATA_BYTES {
            return Err(bad_data());
        }
        // Use a fresh name for each write. A crash may leave an old temp
        // file behind, and concurrent writers in this process need distinct
        // files even though they share a PID.
        let mut temp = tempfile::Builder::new()
            .prefix(&format!(".{}.codex.", self.session_id))
            .suffix(".tmp")
            .tempfile_in(&self.dir)?;
        checked_file(temp.as_file())?;
        temp.write_all(&bytes)?;
        temp.as_file().sync_all()?;
        temp.persist(self.thread_path())
            .map_err(|error| error.error)?;
        Ok(())
    }

    /// Fallible variant for the LORE sidecar. Scrub before creating the temp file.
    pub fn try_write_thread(
        &self,
        updates: Map<String, Value>,
        mut scrub: impl FnMut(&str) -> io::Result<String>,
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
        try_scrub_value(&mut value, &mut scrub)?;
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
        temp.as_file().sync_all()?;
        temp.persist(self.thread_path())
            .map_err(|error| error.error)?;
        Ok(())
    }
}
