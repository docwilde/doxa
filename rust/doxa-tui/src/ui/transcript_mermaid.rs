//! Optional local Mermaid previews. Model-authored source reaches only a
//! reviewed renderer inside a networkless, narrow filesystem sandbox.
use std::{
    collections::HashSet,
    fs::{self, File, OpenOptions},
    io::Write,
    os::unix::{fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt}, process::CommandExt},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{atomic::{AtomicBool, Ordering}, mpsc::{self, Receiver, TryRecvError}, Arc},
    time::{Duration, Instant},
};
use ratatui::{layout::Rect, style::Style, widgets::Paragraph, Frame};
use ratatui_image::{picker::Picker, protocol::Protocol, Image};

use super::{transcript_images, transcript_tools};
use crate::theme;

const MAX_CACHED: usize = 8;
const MAX_ACTIVE: usize = 2;
const TIMEOUT: Duration = Duration::from_secs(5);
const BWRAP: &str = "/usr/bin/bwrap";

struct Preview {
    key: String,
    width: u16,
    cancel: Arc<AtomicBool>,
    state: State,
}
enum State { Loading(Receiver<Option<Protocol>>), Ready(Protocol), Unavailable }

#[derive(Default)]
pub(super) struct Store {
    renderer: String,
    root: String,
    previews: Vec<Preview>,
    revision: u64,
}

impl std::fmt::Debug for Store {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MermaidStore").field("revision", &self.revision)
            .field("previews", &self.previews.len()).finish()
    }
}

impl Store {
    pub fn clear(&mut self) {
        for preview in &self.previews { preview.cancel.store(true, Ordering::Relaxed); }
        self.previews.clear();
        self.revision = self.revision.wrapping_add(1);
    }

    pub fn revision(&self) -> u64 { self.revision }

    pub fn ready_keys(&self, width: u16) -> HashSet<String> {
        self.previews.iter().filter(|entry| entry.width == width
            && matches!(entry.state, State::Ready(_))).map(|entry| entry.key.clone()).collect()
    }

    pub fn poll(&mut self) -> bool {
        let mut changed = false;
        for preview in &mut self.previews {
            let result = match &preview.state {
                State::Loading(receiver) => match receiver.try_recv() {
                    Ok(protocol) => Some(protocol),
                    Err(TryRecvError::Disconnected) => Some(None),
                    Err(TryRecvError::Empty) => None,
                },
                _ => None,
            };
            if let Some(result) = result {
                preview.state = result.map_or(State::Unavailable, State::Ready);
                changed = true;
            }
        }
        if changed { self.revision = self.revision.wrapping_add(1); }
        changed
    }

    pub fn observe(&mut self, transcript: &str, width: u16,
        renderer: &str, root: &str, picker: Option<Picker>) {
        if self.renderer != renderer || self.root != root {
            self.clear();
            self.renderer = renderer.to_owned();
            self.root = root.to_owned();
        }
        let Some(picker) = picker else { return; };
        if renderer.is_empty() || root.is_empty() || !Path::new(BWRAP).is_file() { return; }
        // A transcript can contain many restored diagrams. Admit the latest
        // eight so older entries cannot evict them on every frame.
        for source in transcript_tools::mermaid_sources(transcript).into_iter().rev().take(MAX_CACHED) {
            let key = transcript_tools::mermaid_key(&source);
            if self.previews.iter().any(|entry| entry.key == key && entry.width == width) { continue; }
            if self.previews.iter().filter(|entry| matches!(entry.state, State::Loading(_))).count() >= MAX_ACTIVE { break; }
            if self.previews.len() >= MAX_CACHED {
                if let Some(position) = self.previews.iter().position(|entry| !matches!(entry.state, State::Loading(_))) {
                    self.previews.remove(position);
                } else { break; }
            }
            let (sender, receiver) = mpsc::sync_channel(1);
            let cancel = Arc::new(AtomicBool::new(false));
            let worker_cancel = Arc::clone(&cancel);
            let worker_picker = picker.clone();
            let renderer = renderer.to_owned();
            let root = root.to_owned();
            std::thread::spawn(move || {
                let result = render_source(&source, width, &renderer, &root, &worker_picker, &worker_cancel);
                let _ = sender.send(result);
            });
            self.previews.push(Preview { key, width, cancel, state: State::Loading(receiver) });
        }
    }

    pub fn draw(&self, frame: &mut Frame, area: Rect, key: &str) {
        if area.width == 0 || area.height == 0 { return; }
        if let Some(Preview { state: State::Ready(protocol), .. }) = self.previews.iter()
            .find(|entry| entry.key == key && entry.width == area.width) {
            frame.render_widget(Image::new(protocol), area);
        } else {
            frame.render_widget(Paragraph::new("Diagram source available above")
                .style(Style::default().fg(theme::MUTED)), area);
        }
    }
}

fn private_temp_root() -> Option<PathBuf> {
    let base = std::env::var_os("TMPDIR").map(PathBuf::from).or_else(||
        std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".cache/doxa/mermaid")))?;
    fs::DirBuilder::new().recursive(true).mode(0o700).create(&base).ok()?;
    Some(base)
}

fn reviewed_paths(renderer: &str, root: &str) -> Option<(PathBuf, PathBuf, PathBuf)> {
    let root = Path::new(root);
    let renderer = Path::new(renderer);
    if !root.is_absolute() || !renderer.is_absolute() { return None; }
    let root = root.canonicalize().ok()?;
    if root == Path::new("/") || !root.is_dir() { return None; }
    let renderer = renderer.canonicalize().ok()?;
    let relative = renderer.strip_prefix(&root).ok()?.to_path_buf();
    if !renderer.is_file() || renderer.metadata().ok()?.permissions().mode() & 0o111 == 0 { return None; }
    Some((renderer, root, relative))
}

fn sandbox_command(root: &Path, relative: &Path, work: &Path) -> Command {
    let mut command = Command::new(BWRAP);
    command.args(["--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/work",
        "--setenv", "TMPDIR", "/work", "--tmpfs", "/"]);
    for directory in ["/usr", "/bin", "/lib", "/lib64"] {
        if Path::new(directory).exists() { command.arg("--ro-bind").arg(directory).arg(directory); }
    }
    if Path::new("/etc/fonts").exists() {
        command.args(["--dir", "/etc", "--ro-bind", "/etc/fonts", "/etc/fonts"]);
    }
    command.args(["--dev", "/dev", "--proc", "/proc"])
        .arg("--ro-bind").arg(root).arg("/renderer")
        .arg("--bind").arg(work).arg("/work")
        .args(["--chdir", "/work", "--"])
        .arg(Path::new("/renderer").join(relative))
        .args(["-i", "/work/input.mmd", "-o", "/work/output.png"])
        .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    // The parent can kill the whole process group on timeout or cancellation.
    unsafe { command.pre_exec(|| {
        if libc::setsid() < 0 { return Err(std::io::Error::last_os_error()); }
        Ok(())
    }); }
    command
}

fn render_source(source: &str, width: u16, renderer: &str, root: &str,
    picker: &Picker, cancel: &AtomicBool) -> Option<Protocol> {
    if source.len() > transcript_tools::MAX_MERMAID_SOURCE || cancel.load(Ordering::Relaxed) { return None; }
    let (_, root, relative) = reviewed_paths(renderer, root)?;
    let work = tempfile::Builder::new().prefix("doxa-mermaid-")
        .tempdir_in(private_temp_root()?).ok()?;
    let input = work.path().join("input.mmd");
    let mut file = OpenOptions::new().write(true).create_new(true).mode(0o600).open(&input).ok()?;
    file.write_all(source.as_bytes()).ok()?;
    drop(file);
    let mut child = sandbox_command(&root, &relative, work.path()).spawn().ok()?;
    let deadline = Instant::now() + TIMEOUT;
    let success = loop {
        if cancel.load(Ordering::Relaxed) || Instant::now() >= deadline {
            unsafe { libc::killpg(child.id() as i32, libc::SIGKILL); }
            let _ = child.wait();
            break false;
        }
        match child.try_wait() {
            Ok(Some(status)) => break status.success(),
            Ok(None) => std::thread::sleep(Duration::from_millis(10)),
            Err(_) => break false,
        }
    };
    if !success { return None; }
    let output = work.path().join("output.png");
    let mut file: File = OpenOptions::new().read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
        .open(output).ok()?;
    use std::io::{Read, Seek, SeekFrom};
    let mut signature = [0u8; 8];
    file.read_exact(&mut signature).ok()?;
    if signature != [137, 80, 78, 71, 13, 10, 26, 10] { return None; }
    file.seek(SeekFrom::Start(0)).ok()?;
    transcript_images::decode_file(file, width, picker)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui_image::picker::ProtocolType;

    fn fixture(script: &str) -> (tempfile::TempDir, String) {
        let root = tempfile::tempdir_in(private_temp_root().unwrap()).unwrap();
        let renderer = root.path().join("renderer");
        fs::write(&renderer, format!("#!/bin/sh\n{script}\n")).unwrap();
        fs::set_permissions(&renderer, fs::Permissions::from_mode(0o700)).unwrap();
        (root, renderer.to_string_lossy().into_owned())
    }

    fn picker() -> Picker {
        let mut picker = Picker::from_fontsize((8, 16));
        picker.set_protocol_type(ProtocolType::Halfblocks);
        picker
    }

    fn sandbox_available() -> bool {
        if !Path::new(BWRAP).is_file() { return false; }
        let work = tempfile::tempdir_in(private_temp_root().unwrap()).unwrap();
        sandbox_command(Path::new("/usr"), Path::new("bin/true"), work.path())
            .status().is_ok_and(|status| status.success())
    }

    #[test]
    fn successful_stub_renders_inside_sandbox() {
        if !sandbox_available() { return; }
        let (root, renderer) = fixture("cp /renderer/pixel.png \"$4\"");
        image::RgbaImage::from_pixel(16, 16, image::Rgba([180, 80, 40, 255]))
            .save(root.path().join("pixel.png")).unwrap();
        let result = render_source("graph TD; A-->B", 24, &renderer,
            root.path().to_str().unwrap(), &picker(), &AtomicBool::new(false));
        assert!(result.is_some());
        assert!(result.unwrap().area().height > 0);
    }

    #[test]
    fn failed_and_missing_renderer_leave_source_available() {
        let (root, renderer) = fixture("exit 7");
        assert!(render_source("graph TD; A-->B", 24, &renderer,
            root.path().to_str().unwrap(), &picker(), &AtomicBool::new(false)).is_none());
        assert!(reviewed_paths("/missing/renderer", root.path().to_str().unwrap()).is_none());
    }

    #[test]
    fn hanging_stub_is_killed() {
        if !sandbox_available() { return; }
        let (root, renderer) = fixture("sleep 30");
        let started = Instant::now();
        assert!(render_source("graph TD; A-->B", 24, &renderer,
            root.path().to_str().unwrap(), &picker(), &AtomicBool::new(false)).is_none());
        assert!(started.elapsed() < TIMEOUT + Duration::from_secs(2));
    }

    #[test]
    fn output_symlink_cannot_read_host_file() {
        if !sandbox_available() { return; }
        let (root, renderer) = fixture("ln -s /etc/passwd \"$4\"");
        assert!(render_source("graph TD; A-->B", 24, &renderer,
            root.path().to_str().unwrap(), &picker(), &AtomicBool::new(false)).is_none());
    }

    #[test]
    fn renderer_cannot_see_a_host_file_outside_its_package() {
        if !sandbox_available() { return; }
        let secret = tempfile::tempdir_in(private_temp_root().unwrap()).unwrap();
        fs::write(secret.path().join("private.txt"), "not for diagrams").unwrap();
        let guarded = format!("test ! -e '{}' || exit 8\ncp /renderer/pixel.png \"$4\"",
            secret.path().join("private.txt").display());
        let (root, renderer) = fixture(&guarded);
        image::RgbaImage::from_pixel(8, 8, image::Rgba([100, 140, 220, 255]))
            .save(root.path().join("pixel.png")).unwrap();
        assert!(render_source("graph TD; A-->B", 24, &renderer,
            root.path().to_str().unwrap(), &picker(), &AtomicBool::new(false)).is_some());
    }
}
