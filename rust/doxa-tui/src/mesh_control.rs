//! Owned loopback graph bridge to the packaged Python 1.19 mesh renderer.
//! Only the selected private ledger is readable; the token lives in memory.
use crate::{fleet_view, launch};
use std::{fs, io::{self, BufRead, BufReader, Read, Write}, os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt}, path::{Path, PathBuf}, process::{Child, Command, Stdio}, sync::mpsc, time::{Duration, Instant}};
use serde_json::Value;

const ADAPTER: &str = r#"
import os, sys, json, stat
from pathlib import Path
from doxa import meshgraph
path = Path(sys.argv[1])
directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
info = os.fstat(directory)
if info.st_uid != os.getuid() or info.st_mode & 0o077: raise PermissionError('unsafe mesh directory')
def safe_open(name, mode):
    if Path(name) != path or mode != 'rb': raise PermissionError('unexpected mesh file')
    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 134217728:
        os.close(fd); raise PermissionError('unsafe mesh ledger')
    return os.fdopen(fd, 'rb')
meshgraph.open = safe_open
with meshgraph.MeshServer(path=path) as server:
    print(json.dumps({'url':server.url}), flush=True)
    for line in sys.stdin:
        if line.strip() == 'stop': break
        if line.strip() == 'open':
            from doxa import config
            if config.mesh_open_browser():
                import webbrowser
                webbrowser.open(server.url)
os.close(directory)
"#;
fn invalid(message: &str) -> io::Error { io::Error::new(io::ErrorKind::InvalidInput, message) }
fn private_directory(path: &Path) -> io::Result<()> {
    let info = fs::symlink_metadata(path)?;
    if !info.is_dir() || info.file_type().is_symlink() || info.uid() != unsafe { libc::geteuid() } || info.permissions().mode() & 0o077 != 0 {
        return Err(invalid("mesh ledger directory must be private and owned"));
    }
    Ok(())
}
pub fn default_ledger() -> io::Result<PathBuf> {
    if let Some(path) = std::env::var_os("DOXA_PEER_LEDGER").filter(|value| !value.is_empty()) {
        let path = PathBuf::from(path);
        if !path.is_absolute() { return Err(invalid("peer ledger must be absolute")); }
        return Ok(path);
    }
    Ok(fleet_view::default_root()?.parent().ok_or_else(|| invalid("missing DOXA home"))?.join("peers/messages.jsonl"))
}
pub fn run_ledger(root: &Path, id: &str) -> io::Result<PathBuf> {
    let ids = fleet_view::run_ids(root)?;
    let matching: Vec<_> = ids.iter().filter(|candidate| candidate.as_str() == id || candidate.starts_with(id)).collect();
    let selected = ids.iter().find(|candidate| candidate.as_str() == id).or_else(|| if matching.len() == 1 { Some(matching[0]) } else { None })
        .ok_or_else(|| invalid("mesh run ID is missing or ambiguous"))?;
    let run = root.join(selected); private_directory(&run)?;
    let value: Value = serde_json::from_str(&fs::read_to_string(run.join("manifest.json"))?).map_err(|_| invalid("invalid mesh run manifest"))?;
    private_directory(&run.join("home"))?;
    let expected = run.join("home/peers/messages.jsonl");
    if value["native_version"].is_number() && value["ledger_path"].as_str() != expected.to_str() {
        return Err(invalid("native fleet has no verified private ledger; cannot graph mixed machine traffic"));
    }
    Ok(expected)
}
/// At most one handle is owned by a window; callers must stop it before
/// switching ledgers. Drop closes the control pipe and reaps the child.
pub struct MeshServer { child: Option<Child>, pub ledger: PathBuf, url: String }
impl MeshServer {
    pub fn start(ledger: &Path) -> io::Result<Self> {
        if !ledger.is_absolute() || ledger.file_name().is_none() { return Err(invalid("mesh ledger must be absolute")); }
        let parent = ledger.parent().ok_or_else(|| invalid("mesh ledger has no directory"))?;
        if fs::symlink_metadata(parent).is_err_and(|error| error.kind() == io::ErrorKind::NotFound) {
            private_directory(parent.parent().ok_or_else(|| invalid("mesh ledger directory has no parent"))?)?;
            fs::DirBuilder::new().mode(0o700).create(parent)?;
        }
        private_directory(parent)?;
        if let Ok(info) = fs::symlink_metadata(ledger) {
            if !info.is_file() || info.file_type().is_symlink() || info.uid() != unsafe { libc::geteuid() } || info.permissions().mode() & 0o077 != 0 || info.nlink() != 1 {
                return Err(invalid("unsafe mesh ledger"));
            }
        }
        let python = launch::python_executable(&std::env::var_os("DOXA_LORE_PYTHON").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("python3")))?;
        let mut command = Command::new(python); command.args(["-u", "-c", ADAPTER]).arg(ledger)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
        let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        // Source builds can be launched from any project directory; installed
        // releases use their packaged interpreter once this checkout is gone.
        if source.join("doxa/meshgraph.py").is_file() {
            let mut paths = vec![source]; paths.extend(std::env::split_paths(&std::env::var_os("PYTHONPATH").unwrap_or_default()));
            command.env("PYTHONPATH", std::env::join_paths(paths).map_err(|_| invalid("invalid Python module path"))?);
        }
        let mut child = command.spawn()?;
        let output = child.stdout.take().unwrap(); let (sender, receiver) = mpsc::channel();
        std::thread::spawn(move || { let mut line = String::new(); let result = BufReader::new(output).take(4096).read_line(&mut line).map(|_| line); let _ = sender.send(result); });
        let mut owned = Self { child:Some(child), ledger:ledger.to_owned(), url:String::new() };
        let line = receiver.recv_timeout(Duration::from_secs(5)).map_err(|_| io::Error::other("mesh renderer startup timed out"))??;
        let value: Value = serde_json::from_str(&line).map_err(|_| io::Error::other("mesh renderer did not start"))?;
        let url = value["url"].as_str().ok_or_else(|| invalid("mesh renderer URL missing"))?;
        let rest = url.strip_prefix("http://127.0.0.1:").ok_or_else(|| invalid("mesh renderer is not loopback"))?;
        let (port, token) = rest.split_once('/').ok_or_else(|| invalid("mesh renderer token missing"))?;
        if port.parse::<u16>().ok().filter(|p| *p != 0).is_none() || token.trim_end_matches('/').len() < 24 || !token.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '-'|'_'|'/')) { return Err(invalid("invalid mesh renderer URL")); }
        owned.url = url.to_owned(); Ok(owned)
    }
    pub fn url(&self) -> &str { &self.url }
    /// Honors Python's existing config/env browser setting, default off.
    pub fn open_if_configured(&mut self) -> io::Result<()> {
        self.child.as_mut().and_then(|c| c.stdin.as_mut()).ok_or_else(|| invalid("mesh renderer stopped"))?.write_all(b"open\n")
    }
    pub fn stop(&mut self) -> io::Result<()> {
        let Some(mut child) = self.child.take() else { return Ok(()); };
        if let Some(mut input) = child.stdin.take() { let _ = input.write_all(b"stop\n"); }
        let deadline = Instant::now() + Duration::from_secs(2);
        while child.try_wait()?.is_none() {
            if Instant::now() >= deadline { let _ = child.kill(); child.wait()?; break; }
            std::thread::sleep(Duration::from_millis(20));
        }
        Ok(())
    }
}
impl Drop for MeshServer { fn drop(&mut self) { let _ = self.stop(); } }

static STOP: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
extern "C" fn stop_signal(_: libc::c_int) { STOP.store(true, std::sync::atomic::Ordering::Relaxed); }
/// Foreground CLI owner: Ctrl-C stops and reaps the renderer, releasing port.
pub fn serve(ledger: &Path) -> io::Result<()> {
    STOP.store(false, std::sync::atomic::Ordering::Relaxed);
    let interrupt = unsafe { libc::signal(libc::SIGINT, stop_signal as *const () as libc::sighandler_t) };
    if interrupt == libc::SIG_ERR { return Err(io::Error::last_os_error()); }
    let terminate = unsafe { libc::signal(libc::SIGTERM, stop_signal as *const () as libc::sighandler_t) };
    if terminate == libc::SIG_ERR { unsafe { libc::signal(libc::SIGINT, interrupt); } return Err(io::Error::last_os_error()); }
    let result = (|| {
        let mut server = MeshServer::start(ledger)?;
        println!("mesh: {}\n{}\nCtrl-C stops this loopback renderer.", ledger.display(), server.url());
        if !STOP.load(std::sync::atomic::Ordering::Relaxed) { server.open_if_configured()?; }
        while !STOP.load(std::sync::atomic::Ordering::Relaxed) {
            if server.child.as_mut().unwrap().try_wait()?.is_some() { return Err(io::Error::other("mesh renderer exited")); }
            std::thread::sleep(Duration::from_millis(100));
        }
        server.stop()
    })();
    unsafe { libc::signal(libc::SIGINT, interrupt); libc::signal(libc::SIGTERM, terminate); }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpStream;
    fn request(url: &str, route: &str) -> String {
        let tail = url.strip_prefix("http://").unwrap(); let (host, token) = tail.split_once('/').unwrap();
        let mut stream = TcpStream::connect(host).unwrap(); stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        write!(stream, "GET /{token}{route} HTTP/1.0\r\nHost: {host}\r\n\r\n").unwrap();
        let mut body = String::new(); stream.read_to_string(&mut body).unwrap(); body
    }
    #[test]
    fn renderer_is_token_gated_private_and_reaped() {
        let dir = tempfile::tempdir().unwrap(); fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let ledger = dir.path().join("messages.jsonl");
        fs::write(&ledger, "{\"from\":{\"session\":\"sender\"},\"to\":[\"worker\"],\"body\":\"<script>unsafe</script>\",\"kind\":\"direct\"}\n").unwrap();
        fs::set_permissions(&ledger, fs::Permissions::from_mode(0o600)).unwrap();
        let mut server = MeshServer::start(&ledger).unwrap(); let old = server.url().to_owned();
        assert!(request(&old, "").contains("mesh.js"));
        assert!(request(&old, "ledger").contains("sender"));
        let denied = old.rsplit_once('/').unwrap().0.replace(old.split('/').nth(3).unwrap(), "wrong-token");
        assert!(request(&(denied + "/"), "ledger").contains("404"));
        server.stop().unwrap();
        let address = old.trim_start_matches("http://").split('/').next().unwrap();
        assert!(TcpStream::connect(address).is_err());
        std::os::unix::fs::symlink(&ledger, dir.path().join("link")).unwrap();
        assert!(MeshServer::start(&dir.path().join("link")).is_err());
    }
}
