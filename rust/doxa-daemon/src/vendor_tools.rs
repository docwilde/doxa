//! Explicitly enabled, read-only workspace tool for native vendor engines.
//! Path resolution stays below an opened workspace directory and rejects symlinks.
use doxa_vendors::{ToolCall, ToolGate};
use futures_util::future::BoxFuture;
use serde_json::{json, Value};
use std::ffi::CString;
use std::fs::{File, OpenOptions};
use std::io::Read;
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Component, Path};

const MAX_READ_BYTES: u64 = 64 * 1024;

pub struct WorkspaceReadGate<'a> {
    root: &'a Path,
    scrub: &'a dyn Fn(&str) -> Result<String, ()>,
}

impl<'a> WorkspaceReadGate<'a> {
    pub fn new(root: &'a Path, scrub: &'a dyn Fn(&str) -> Result<String, ()>) -> Self {
        Self { root, scrub }
    }

    fn read(&self, call: &ToolCall) -> Result<Value, ()> {
        if call.name != "workspace_read" || call.arguments.len() != 1 {
            return Err(());
        }
        let path = call.arguments.get("path").and_then(Value::as_str).ok_or(())?;
        let parts: Vec<_> = Path::new(path).components().collect();
        if parts.is_empty() || parts.len() > 16 || path.len() > 1024 || parts.iter().any(|part| {
            !matches!(part, Component::Normal(name) if !name.to_string_lossy().starts_with('.'))
        }) {
            return Err(());
        }
        let mut file = OpenOptions::new().read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(self.root).map_err(|_| ())?;
        for (index, part) in parts.iter().enumerate() {
            let Component::Normal(name) = part else { return Err(()); };
            use std::os::unix::ffi::OsStrExt;
            let name = CString::new(name.as_bytes()).map_err(|_| ())?;
            let last = index + 1 == parts.len();
            let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC
                | if last { libc::O_NONBLOCK } else { libc::O_DIRECTORY };
            // SAFETY: openat receives a live directory fd and a NUL-terminated name.
            let fd = unsafe { libc::openat(file.as_raw_fd(), name.as_ptr(), flags) };
            if fd < 0 { return Err(()); }
            // SAFETY: a successful openat returns a newly owned fd.
            file = unsafe { File::from_raw_fd(fd) };
        }
        let metadata = file.metadata().map_err(|_| ())?;
        if !metadata.is_file() || metadata.len() > MAX_READ_BYTES { return Err(()); }
        let mut bytes = Vec::new();
        file.take(MAX_READ_BYTES + 1).read_to_end(&mut bytes).map_err(|_| ())?;
        if bytes.len() as u64 > MAX_READ_BYTES { return Err(()); }
        let content = String::from_utf8(bytes).map_err(|_| ())?;
        Ok(json!({"path":path,"content":(self.scrub)(&content)?}))
    }
}

impl ToolGate for WorkspaceReadGate<'_> {
    fn definitions(&self) -> Vec<Value> {
        vec![json!({"type":"function","function":{
            "name":"workspace_read",
            "description":"Read one UTF-8 regular file below the session workspace. Hidden paths and symlinks are unavailable; maximum 64 KiB. The file content is sent to the model provider.",
            "parameters":{"type":"object","properties":{"path":{"type":"string","description":"Relative path below the workspace"}},"required":["path"],"additionalProperties":false}
        }})]
    }

    fn execute<'a>(&'a mut self, call: &'a ToolCall) -> BoxFuture<'a, Result<Value, ()>> {
        let result = self.read(call);
        Box::pin(async move { result })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Map;
    use std::fs;

    fn call(path: &str) -> ToolCall {
        let mut arguments = Map::new();
        arguments.insert("path".into(), json!(path));
        ToolCall { id: "1".into(), name: "workspace_read".into(), arguments }
    }

    #[test]
    fn reads_bounded_file_and_rejects_escape_and_hidden_paths() {
        let root = tempfile::tempdir().unwrap();
        fs::create_dir(root.path().join("src")).unwrap();
        fs::write(root.path().join("src/main.rs"), "secret text").unwrap();
        fs::write(root.path().join(".env"), "key").unwrap();
        std::os::unix::fs::symlink("/etc/passwd", root.path().join("link")).unwrap();
        let scrub = |text: &str| Ok(text.replace("secret", "***"));
        let gate = WorkspaceReadGate::new(root.path(), &scrub);
        assert_eq!(gate.read(&call("src/main.rs")).unwrap()["content"], "*** text");
        for path in ["../etc/passwd", "/etc/passwd", ".env", "src/../.env", "link", "src", "missing"] {
            assert!(gate.read(&call(path)).is_err(), "{path}");
        }
        fs::write(root.path().join("large.txt"), vec![b'a'; MAX_READ_BYTES as usize + 1]).unwrap();
        assert!(gate.read(&call("large.txt")).is_err());
    }
}
