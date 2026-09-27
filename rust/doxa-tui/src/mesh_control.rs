//! Owned loopback graph bridge to the packaged Python 1.19 mesh renderer.
//! Only the selected private ledger is readable; the token lives in memory.
use crate::{fleet_view, launch};
use std::{fs, io::{self, BufRead, BufReader, Read, Write}, os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt}, path::{Path, PathBuf}, process::{Child, Command, Stdio}, sync::mpsc, time::{Duration, Instant}};
use serde_json::Value;

const ADAPTER: &str = r#"
import os, sys, json, stat
from pathlib import Path
if sys.argv[2]: sys.path.insert(0, sys.argv[2])
from doxa import meshgraph
path = Path(sys.argv[1])
directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
info = os.fstat(directory)
if info.st_uid != os.getuid() or info.st_mode & 0o077: raise PermissionError('unsafe mesh directory')
def safe_open(name, mode):
    if Path(name) != path or mode != 'rb': raise PermissionError('unexpected mesh file')
    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 134217728:
        os.close(fd); raise PermissionError('unsafe mesh ledger')
    return os.fdopen(fd, 'rb')
meshgraph.open = safe_open
with meshgraph.MeshServer(path=path) as server:
    print(json.dumps({'url':server.url}), flush=True)
    for line in sys.stdin:
        if line.strip() == 'stop': break
        if line.strip() in ('open', 'open-force'):
            from doxa import config
            if line.strip() == 'open-force' or config.mesh_open_browser():
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
    private_directory(root)?;
    let ids = fleet_view::run_ids(root)?;
    let matching: Vec<_> = ids.iter().filter(|candidate| candidate.as_str() == id || candidate.starts_with(id)).collect();
    let selected = ids.iter().find(|candidate| candidate.as_str() == id).or_else(|| if matching.len() == 1 { Some(matching[0]) } else { None })
        .ok_or_else(|| invalid("mesh run ID is missing or ambiguous"))?;
    let run = root.join(selected); private_directory(&run)?;
    let value = fleet_view::manifest_snapshot(root, selected)?;
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
    pub fn start(ledger: &Path) -> io::Result<Self> { Self::start_at(ledger, None) }
    fn start_at(ledger: &Path, working_directory: Option<&Path>) -> io::Result<Self> {
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
        let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let source = if source.join("doxa/meshgraph.py").is_file() { fs::canonicalize(source)? } else { PathBuf::new() };
        // Isolated Python excludes the working directory, PYTHONPATH and user
        // site. Only the compiled checkout bootstrap or packaged sidecar can
        // supply DOXA; the currently opened project cannot shadow imports.
        let mut command = Command::new(python); command.args(["-I", "-u", "-c", ADAPTER]).arg(ledger).arg(source)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
        if let Some(directory) = working_directory { command.current_dir(directory); }
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
    pub fn open_browser(&mut self) -> io::Result<()> {
        self.child.as_mut().and_then(|c| c.stdin.as_mut()).ok_or_else(|| invalid("mesh renderer stopped"))?.write_all(b"open-force\n")
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

#[derive(Clone, Default)]
pub struct WindowSnapshot { pub revision: u64, pub running: bool, pub busy: bool, pub url: String, pub report: String }
enum WindowCommand { Start(Option<String>, Option<PathBuf>), Stop, Open, Close }
#[derive(Clone)]
pub struct WindowHandle { sender: mpsc::Sender<WindowCommand>, snapshot: std::sync::Arc<std::sync::Mutex<WindowSnapshot>> }
impl WindowHandle {
    pub fn snapshot(&self) -> WindowSnapshot { self.snapshot.lock().unwrap_or_else(|p| p.into_inner()).clone() }
    pub fn start(&self, run: Option<String>, root: Option<PathBuf>) { let _ = self.sender.send(WindowCommand::Start(run, root)); }
    pub fn stop(&self) { let _ = self.sender.send(WindowCommand::Stop); }
    pub fn open(&self) { let _ = self.sender.send(WindowCommand::Open); }
}
/// Window ownership outlives the popup. All renderer startup, switching and
/// teardown happens on one worker; shutdown joins it after leaving the TUI.
pub struct WindowMesh { handle: WindowHandle, worker: Option<std::thread::JoinHandle<()>> }
impl WindowMesh {
    pub fn new() -> Self {
        let (sender, receiver) = mpsc::channel();
        let snapshot = std::sync::Arc::new(std::sync::Mutex::new(WindowSnapshot::default()));
        let state = snapshot.clone();
        let worker = std::thread::spawn(move || {
            let mut server: Option<MeshServer> = None;
            while let Ok(command) = receiver.recv() {
                if matches!(command, WindowCommand::Close) { break; }
                { let mut state = state.lock().unwrap_or_else(|p| p.into_inner()); state.busy = true; state.revision += 1; }
                let result = match command {
                    WindowCommand::Start(run, root) => (|| {
                        let ledger = if let Some(run) = run { run_ledger(&root.map(Ok).unwrap_or_else(fleet_view::default_root)?, &run)? } else { default_ledger()? };
                        if server.as_ref().is_some_and(|s| s.ledger == ledger) { return Ok(format!("Mesh already running · {}", ledger.display())); }
                        if let Some(mut old) = server.take() { old.stop()?; }
                        let mut started = MeshServer::start(&ledger)?;
                        started.open_if_configured()?;
                        server = Some(started);
                        Ok(format!("Mesh · {}", ledger.display()))
                    })(),
                    WindowCommand::Stop => if let Some(mut old) = server.take() { old.stop().map(|_| "Mesh stopped; port released".into()) } else { Ok("No mesh server running".into()) },
                    WindowCommand::Open => server.as_mut().ok_or_else(|| invalid("No mesh server running")).and_then(|s| s.open_browser()).map(|_| "Browser launch requested".into()),
                    WindowCommand::Close => unreachable!(),
                };
                let mut state = state.lock().unwrap_or_else(|p| p.into_inner());
                state.busy = false; state.running = server.is_some(); state.url = server.as_ref().map(|s| s.url().to_owned()).unwrap_or_default();
                state.report = crate::markdown::sanitize(&result.unwrap_or_else(|e| format!("Mesh: {e}"))); state.revision += 1;
            }
            drop(server);
        });
        Self { handle: WindowHandle { sender, snapshot }, worker: Some(worker) }
    }
    pub fn handle(&self) -> WindowHandle { self.handle.clone() }
}
impl Default for WindowMesh { fn default() -> Self { Self::new() } }
impl Drop for WindowMesh {
    fn drop(&mut self) { let _ = self.handle.sender.send(WindowCommand::Close); if let Some(worker) = self.worker.take() { let _ = worker.join(); } }
}

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
    #[test]
    fn window_mesh_is_nonblocking_and_releases_renderer_on_window_close() {
        let root = tempfile::tempdir().unwrap();
        fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let run = root.path().join("mesh-fixture");
        for dir in [&run, &run.join("home")] { fs::DirBuilder::new().mode(0o700).create(dir).unwrap(); }
        let manifest = run.join("manifest.json");
        fs::write(&manifest, serde_json::json!({"run_id":"mesh-fixture", "native_version":1,
            "ledger_path":run.join("home/peers/messages.jsonl")}).to_string()).unwrap();
        fs::set_permissions(&manifest, fs::Permissions::from_mode(0o600)).unwrap();
        let window = WindowMesh::new(); let handle = window.handle();
        let started = Instant::now(); handle.start(Some("mesh-fixture".into()), Some(root.path().to_owned()));
        assert!(started.elapsed() < Duration::from_millis(100));
        let deadline = Instant::now() + Duration::from_secs(6);
        let url = loop {
            let state = handle.snapshot();
            if state.running { break state.url; }
            assert!(Instant::now() < deadline, "{}", state.report);
            std::thread::sleep(Duration::from_millis(10));
        };
        assert!(request(&url, "").contains("mesh.js"));
        handle.start(Some("missing".into()), Some(root.path().to_owned()));
        let deadline = Instant::now() + Duration::from_secs(2);
        while !handle.snapshot().report.contains("ambiguous") {
            assert!(Instant::now() < deadline); std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(handle.snapshot().url, url); // refused switch keeps existing owner
        drop(window); // an open popup handle cannot prolong the server
        let address = url.trim_start_matches("http://").split('/').next().unwrap();
        assert!(TcpStream::connect(address).is_err());
    }
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
        fs::remove_file(&ledger).unwrap();
        let filename = std::ffi::CString::new(ledger.as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(filename.as_ptr(), 0o600) }, 0);
        assert!(request(&old, "ledger").contains("200"));
        server.stop().unwrap();
        let address = old.trim_start_matches("http://").split('/').next().unwrap();
        assert!(TcpStream::connect(address).is_err());
        std::os::unix::fs::symlink(&ledger, dir.path().join("link")).unwrap();
        assert!(MeshServer::start(&dir.path().join("link")).is_err());
    }
    #[test]
    fn current_project_cannot_shadow_renderer_imports() {
        let dir = tempfile::tempdir().unwrap(); fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let project = dir.path().join("project"); fs::create_dir(&project).unwrap();
        let marker = dir.path().join("shadow-executed");
        fs::write(project.join("doxa.py"), format!("from pathlib import Path\nPath({:?}).write_text('executed')\nraise RuntimeError('shadowed')\n", marker.to_str().unwrap())).unwrap();
        let ledger = dir.path().join("messages.jsonl");
        let mut server = MeshServer::start_at(&ledger, Some(&project)).unwrap();
        assert!(request(server.url(), "").contains("mesh.js"));
        assert!(!marker.exists()); server.stop().unwrap();
    }
    #[test]
    fn run_manifest_rejects_symlinks_oversize_and_fifo_without_blocking() {
        let root = tempfile::tempdir().unwrap(); fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let run = root.path().join("run"); fs::DirBuilder::new().mode(0o700).create(&run).unwrap();
        fs::DirBuilder::new().mode(0o700).create(run.join("home")).unwrap();
        let manifest = run.join("manifest.json");
        fs::write(&manifest, vec![b' '; 1024 * 1024 + 1]).unwrap(); fs::set_permissions(&manifest, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(fleet_view::manifest_snapshot(root.path(), "run").is_err());
        fs::remove_file(&manifest).unwrap();
        let real = root.path().join("real.json"); fs::write(&real, "{\"run_id\":\"run\"}").unwrap(); fs::set_permissions(&real, fs::Permissions::from_mode(0o600)).unwrap();
        std::os::unix::fs::symlink(&real, &manifest).unwrap();
        assert!(run_ledger(root.path(), "run").is_err()); fs::remove_file(&manifest).unwrap();
        let filename = std::ffi::CString::new(manifest.as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(filename.as_ptr(), 0o600) }, 0);
        let selected_root = root.path().to_owned(); let (sender, receiver) = mpsc::channel();
        std::thread::spawn(move || {
            let failed = fleet_view::manifest_snapshot(&selected_root, "run").is_err() && run_ledger(&selected_root, "run").is_err();
            let _ = sender.send(failed);
        });
        assert!(receiver.recv_timeout(Duration::from_secs(1)).expect("FIFO manifest blocked the reader"));
    }

}
