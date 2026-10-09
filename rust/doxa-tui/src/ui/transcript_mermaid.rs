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
    workspace: Vec<PathBuf>,
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
        renderer: &str, root: &str, workspaces: &[PathBuf], picker: Option<Picker>) {
        let mut workspace_key=workspaces.to_vec();
        workspace_key.sort();
        if self.renderer != renderer || self.root != root || self.workspace != workspace_key {
            self.clear();
            self.renderer = renderer.to_owned();
            self.root = root.to_owned();
            self.workspace = workspace_key;
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
                    self.revision = self.revision.wrapping_add(1);
                } else { break; }
            }
            let (sender, receiver) = mpsc::sync_channel(1);
            let cancel = Arc::new(AtomicBool::new(false));
            let worker_cancel = Arc::clone(&cancel);
            let worker_picker = picker.clone();
            let renderer = renderer.to_owned();
            let root = root.to_owned();
            let workspaces = workspaces.to_vec();
            std::thread::spawn(move || {
                let result = render_source(&source, width, &renderer, &root, &workspaces, &worker_picker, &worker_cancel);
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
            frame.render_widget(Paragraph::new("Diagram unavailable")
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

fn canonical_allow_missing(path:&Path)->Option<PathBuf>{
    if let Ok(found)=path.canonicalize(){return Some(found);}
    let ancestor=path.ancestors().find(|ancestor|ancestor.exists())?;
    let relative=path.strip_prefix(ancestor).ok()?;
    if relative.components().any(|part|!matches!(part,std::path::Component::Normal(_))){return None;}
    Some(ancestor.canonicalize().ok()?.join(relative))
}

fn repository_root(workspace:&Path)->Option<PathBuf>{
    let workspace=workspace.canonicalize().ok()?;
    Some(workspace.ancestors().find(|path|fs::symlink_metadata(path.join(".git")).is_ok())
        .unwrap_or(&workspace).to_path_buf())
}

fn private_overlap(root:&Path,home:&Path,doxa_home:&Path,workspace:&Path,fleet:&Path)->bool{
    home.starts_with(root)
        || [doxa_home,workspace,fleet].iter().any(|private|
            private.starts_with(root)||root.starts_with(private))
}

fn root_exposes_private_paths(root:&Path,workspaces:&[PathBuf])->Option<bool>{
    let home=PathBuf::from(std::env::var_os("HOME")?);
    if !home.is_absolute(){return None;}
    let home=home.canonicalize().ok()?;
    let doxa_home=std::env::var_os("DOXA_HOME").map(PathBuf::from).unwrap_or_else(||home.join(".doxa"));
    if !doxa_home.is_absolute(){return None;}
    let doxa_home=canonical_allow_missing(&doxa_home)?;
    let fleet=canonical_allow_missing(&doxa_home.join("fleet"))?;
    // A dedicated package may live below HOME, but mounting HOME itself or
    // any ancestor would expose every private file. DOXA state, the current
    // repository and fleet state are refused in either direction.
    let current=std::env::current_dir().ok()?;
    let current_repo=repository_root(&current)?;
    if private_overlap(root,&home,&doxa_home,&current_repo,&fleet){return Some(true);}
    for workspace in workspaces {
        let repo=repository_root(workspace)?;
        if private_overlap(root,&home,&doxa_home,&repo,&fleet){return Some(true);}
    }
    Some(false)
}

fn reviewed_paths(renderer: &str, root: &str, workspaces:&[PathBuf]) -> Option<(PathBuf, PathBuf, PathBuf)> {
    checked_paths(renderer, root, workspaces).ok()
}

fn checked_paths(renderer: &str, root: &str, workspaces:&[PathBuf]) -> Result<(PathBuf, PathBuf, PathBuf), &'static str> {
    let root = Path::new(root);
    let renderer = Path::new(renderer);
    if !root.is_absolute() || !renderer.is_absolute() { return Err("renderer and package root must be absolute paths"); }
    let root = root.canonicalize().map_err(|_| "renderer package root is unavailable")?;
    if root == Path::new("/") || !root.is_dir() { return Err("renderer package root is unsafe or not a directory"); }
    match root_exposes_private_paths(&root, workspaces) {
        Some(true) => return Err("renderer package root overlaps private state or a repository"),
        None => return Err("renderer package root cannot be reviewed safely"),
        Some(false) => {}
    }
    let renderer = renderer.canonicalize().map_err(|_| "renderer executable is unavailable")?;
    let relative = renderer.strip_prefix(&root).map_err(|_| "renderer executable is outside its package root")?.to_path_buf();
    if !renderer.is_file() || renderer.metadata().map_err(|_| "renderer executable is unavailable")?.permissions().mode() & 0o111 == 0 {
        return Err("renderer executable is not an executable file");
    }
    Ok((renderer, root, relative))
}

/// A bounded, local diagnostic. Messages deliberately contain no configured paths
/// or renderer output, which can include secrets from the operator's environment.
#[derive(Debug, PartialEq, Eq)]
pub enum DoctorResult {
    Disabled,
    Available,
    Unavailable(&'static str),
}

#[derive(Debug, PartialEq, Eq)]
enum RunResult { Success, Failed, TimedOut, SpawnFailed }

fn run_sandbox(mut command: Command, cancel: &AtomicBool) -> RunResult {
    let Ok(mut child) = command.spawn() else { return RunResult::SpawnFailed; };
    let deadline = Instant::now() + TIMEOUT;
    loop {
        if cancel.load(Ordering::Relaxed) || Instant::now() >= deadline {
            unsafe { libc::killpg(child.id() as i32, libc::SIGKILL); }
            let _ = child.wait();
            return RunResult::TimedOut;
        }
        match child.try_wait() {
            Ok(Some(status)) => return if status.success() { RunResult::Success } else { RunResult::Failed },
            Ok(None) => std::thread::sleep(Duration::from_millis(10)),
            Err(_) => {
                unsafe { libc::killpg(child.id() as i32, libc::SIGKILL); }
                let _ = child.wait();
                return RunResult::Failed;
            }
        }
    }
}

/// Check the same path policy and sandbox used for transcript previews, then
/// smoke-render a fixed diagram. This does not establish Mermaid CLI parity.
pub fn diagnose(renderer: &str, root: &str, workspaces: &[PathBuf]) -> DoctorResult {
    if renderer.is_empty() && root.is_empty() { return DoctorResult::Disabled; }
    if renderer.is_empty() || root.is_empty() {
        return DoctorResult::Unavailable("renderer executable and package root must both be configured");
    }
    let (_, root, relative) = match checked_paths(renderer, root, workspaces) {
        Ok(paths) => paths,
        Err(reason) => return DoctorResult::Unavailable(reason),
    };
    if !Path::new(BWRAP).is_file() || !fs::metadata(BWRAP).is_ok_and(|meta| meta.permissions().mode() & 0o111 != 0) {
        return DoctorResult::Unavailable("bubblewrap is unavailable or not executable");
    }
    let Some(temp_root) = private_temp_root() else { return DoctorResult::Unavailable("private render directory is unavailable"); };
    let Ok(work) = tempfile::Builder::new().prefix("doxa-mermaid-doctor-").tempdir_in(temp_root) else {
        return DoctorResult::Unavailable("private render directory is unavailable");
    };
    match run_sandbox(sandbox_command(Path::new("/usr"), Path::new("bin/true"), work.path()), &AtomicBool::new(false)) {
        RunResult::Success => {}
        RunResult::TimedOut => return DoctorResult::Unavailable("bubblewrap capability probe timed out"),
        RunResult::Failed | RunResult::SpawnFailed => return DoctorResult::Unavailable("bubblewrap or unprivileged user namespaces are unavailable"),
    }
    let input = work.path().join("input.mmd");
    let Ok(mut file) = OpenOptions::new().write(true).create_new(true).mode(0o600).open(input) else {
        return DoctorResult::Unavailable("private render input could not be created");
    };
    if file.write_all(b"graph TD\nA-->B\n").is_err() {
        return DoctorResult::Unavailable("private render input could not be written");
    }
    drop(file);
    match run_sandbox(sandbox_command(&root, &relative, work.path()), &AtomicBool::new(false)) {
        RunResult::TimedOut => return DoctorResult::Unavailable("renderer timed out in sandbox"),
        RunResult::Failed => return DoctorResult::Unavailable("renderer exited unsuccessfully in sandbox"),
        RunResult::SpawnFailed => return DoctorResult::Unavailable("renderer sandbox could not start"),
        RunResult::Success => {}
    }
    let Some(file) = open_png(&work.path().join("output.png")) else {
        return DoctorResult::Unavailable("renderer did not produce a valid bounded PNG");
    };
    let mut picker = Picker::from_fontsize((8, 16));
    picker.set_protocol_type(ratatui_image::picker::ProtocolType::Halfblocks);
    if transcript_images::decode_file(file, 24, &picker).is_none() {
        return DoctorResult::Unavailable("renderer did not produce a valid bounded PNG");
    }
    DoctorResult::Available
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
    workspaces:&[PathBuf], picker: &Picker, cancel: &AtomicBool) -> Option<Protocol> {
    if source.len() > transcript_tools::MAX_MERMAID_SOURCE || cancel.load(Ordering::Relaxed) { return None; }
    let (_, root, relative) = reviewed_paths(renderer, root, workspaces)?;
    let work = tempfile::Builder::new().prefix("doxa-mermaid-")
        .tempdir_in(private_temp_root()?).ok()?;
    let input = work.path().join("input.mmd");
    let mut file = OpenOptions::new().write(true).create_new(true).mode(0o600).open(&input).ok()?;
    file.write_all(source.as_bytes()).ok()?;
    drop(file);
    if run_sandbox(sandbox_command(&root, &relative, work.path()), cancel) != RunResult::Success { return None; }
    let file = open_png(&work.path().join("output.png"))?;
    transcript_images::decode_file(file, width, picker)
}

fn open_png(output: &Path) -> Option<File> {
    let mut file: File = OpenOptions::new().read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK)
        .open(output).ok()?;
    use std::io::{Read, Seek, SeekFrom};
    let mut signature = [0u8; 8];
    file.read_exact(&mut signature).ok()?;
    if signature != [137, 80, 78, 71, 13, 10, 26, 10] { return None; }
    file.seek(SeekFrom::Start(0)).ok()?;
    Some(file)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;
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
    fn doctor_reports_optional_and_invalid_configuration_without_spawning() {
        assert_eq!(diagnose("", "", &[]), DoctorResult::Disabled);
        assert_eq!(diagnose("/missing/renderer", "", &[]), DoctorResult::Unavailable(
            "renderer executable and package root must both be configured"));
        let (package, renderer) = fixture("exit 99");
        assert_eq!(diagnose(&renderer, "/missing/package", &[]), DoctorResult::Unavailable(
            "renderer package root is unavailable"));
        let home = std::env::var("HOME").unwrap();
        assert_eq!(diagnose(&renderer, &home, &[]), DoctorResult::Unavailable(
            "renderer package root overlaps private state or a repository"));
        assert_eq!(diagnose("/missing/renderer", package.path().to_str().unwrap(), &[]),
            DoctorResult::Unavailable("renderer executable is unavailable"));
        assert_eq!(diagnose(&renderer, package.path().to_str().unwrap(), &[package.path().to_path_buf()]),
            DoctorResult::Unavailable("renderer package root overlaps private state or a repository"));
    }

    #[test]
    fn doctor_smoke_render_failure_timeout_and_invalid_png_are_distinct() {
        if !sandbox_available() { return; }
        let (package, renderer) = fixture("cp /renderer/pixel.png \"$4\"");
        image::RgbaImage::from_pixel(8, 8, image::Rgba([100, 140, 220, 255]))
            .save(package.path().join("pixel.png")).unwrap();
        assert_eq!(diagnose(&renderer, package.path().to_str().unwrap(), &[]), DoctorResult::Available);

        let (package, renderer) = fixture("exit 7");
        assert_eq!(diagnose(&renderer, package.path().to_str().unwrap(), &[]),
            DoctorResult::Unavailable("renderer exited unsuccessfully in sandbox"));

        let (package, renderer) = fixture("printf 'not png' > \"$4\"");
        assert_eq!(diagnose(&renderer, package.path().to_str().unwrap(), &[]),
            DoctorResult::Unavailable("renderer did not produce a valid bounded PNG"));

        let (package, renderer) = fixture("sleep 30");
        let started = Instant::now();
        assert_eq!(diagnose(&renderer, package.path().to_str().unwrap(), &[]),
            DoctorResult::Unavailable("renderer timed out in sandbox"));
        assert!(started.elapsed() < TIMEOUT + Duration::from_secs(2));
    }

    #[test]
    fn doctor_never_echoes_configured_paths_or_renderer_output() {
        let secret = "SECRET_RENDERER_PATH_TOKEN_4821";
        let result = diagnose(&format!("/missing/{secret}"), "/missing/package", &[]);
        assert!(!format!("{result:?}").contains(secret));
        if !sandbox_available() { return; }
        let (package, renderer) = fixture(&format!("echo {secret} >&2\nexit 8"));
        let result = diagnose(&renderer, package.path().to_str().unwrap(), &[]);
        assert!(!format!("{result:?}").contains(secret));
        assert!(!format!("{result:?}").contains(&renderer));
    }

    #[test]
    fn broad_private_roots_are_refused_before_renderer_spawn() {
        let home=Path::new("/home/operator");
        let state=home.join(".doxa");
        let repo=home.join("projects/app");
        let fleet=state.join("fleet");
        for root in [Path::new("/"),home,state.as_path(),&state.join("settings"),
            repo.as_path(),&repo.join("tools/mermaid"),fleet.as_path(),&fleet.join("run-1")] {
            assert!(private_overlap(root,home,&state,&repo,&fleet),"{} was not refused",root.display());
        }
        assert!(!private_overlap(&home.join("tools/mermaid"),home,&state,&repo,&fleet));
        let (package,renderer)=fixture("exit 99");
        let home=std::env::var_os("HOME").map(PathBuf::from).unwrap().canonicalize().unwrap();
        assert!(reviewed_paths(&renderer,home.to_str().unwrap(),&[]).is_none());
        assert!(render_source("graph TD; A-->B",24,&renderer,home.to_str().unwrap(),&[],
            &picker(),&AtomicBool::new(false)).is_none());
        assert!(package.path().exists());
    }

    #[test]
    fn repository_and_symlink_alias_roots_are_refused() {
        let base=tempfile::tempdir_in(private_temp_root().unwrap()).unwrap();
        let repo=base.path().join("project");fs::create_dir(&repo).unwrap();
        fs::create_dir(repo.join(".git")).unwrap();
        let package=repo.join("renderer");fs::create_dir(&package).unwrap();
        let executable=package.join("mmdc");fs::write(&executable,"#!/bin/sh\nexit 99\n").unwrap();
        fs::set_permissions(&executable,fs::Permissions::from_mode(0o700)).unwrap();
        assert!(reviewed_paths(executable.to_str().unwrap(),package.to_str().unwrap(),&[repo.clone()]).is_none());
        let alias=base.path().join("package-alias");symlink(&package,&alias).unwrap();
        assert!(reviewed_paths(&alias.join("mmdc").to_string_lossy(),&alias.to_string_lossy(),&[repo.clone()]).is_none());
        assert!(render_source("graph TD; A-->B",24,&alias.join("mmdc").to_string_lossy(),
            &alias.to_string_lossy(),&[repo.clone()],&picker(),&AtomicBool::new(false)).is_none());
        let safe=base.path().join("separate-package");fs::create_dir(&safe).unwrap();
        let safe_executable=safe.join("mmdc");fs::write(&safe_executable,"#!/bin/sh\nexit 99\n").unwrap();
        fs::set_permissions(&safe_executable,fs::Permissions::from_mode(0o700)).unwrap();
        assert!(reviewed_paths(&safe_executable.to_string_lossy(),&safe.to_string_lossy(),&[repo.clone()]).is_some());
    }

    #[test]
    fn successful_stub_renders_inside_sandbox() {
        if !sandbox_available() { return; }
        let (root, renderer) = fixture("cp /renderer/pixel.png \"$4\"");
        image::RgbaImage::from_pixel(16, 16, image::Rgba([180, 80, 40, 255]))
            .save(root.path().join("pixel.png")).unwrap();
        let result = render_source("graph TD; A-->B", 24, &renderer,
            root.path().to_str().unwrap(), &[], &picker(), &AtomicBool::new(false));
        assert!(result.is_some());
        assert!(result.unwrap().area().height > 0);
    }

    #[test]
    fn failed_and_missing_renderer_leave_source_available() {
        let (root, renderer) = fixture("exit 7");
        assert!(render_source("graph TD; A-->B", 24, &renderer,
            root.path().to_str().unwrap(), &[], &picker(), &AtomicBool::new(false)).is_none());
        assert!(reviewed_paths("/missing/renderer", root.path().to_str().unwrap(), &[]).is_none());
    }

    #[test]
    fn hanging_stub_is_killed() {
        if !sandbox_available() { return; }
        let (root, renderer) = fixture("sleep 30");
        let started = Instant::now();
        assert!(render_source("graph TD; A-->B", 24, &renderer,
            root.path().to_str().unwrap(), &[], &picker(), &AtomicBool::new(false)).is_none());
        assert!(started.elapsed() < TIMEOUT + Duration::from_secs(2));
    }

    #[test]
    fn output_symlink_cannot_read_host_file() {
        if !sandbox_available() { return; }
        let (root, renderer) = fixture("ln -s /etc/passwd \"$4\"");
        assert!(render_source("graph TD; A-->B", 24, &renderer,
            root.path().to_str().unwrap(), &[], &picker(), &AtomicBool::new(false)).is_none());
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
            root.path().to_str().unwrap(), &[], &picker(), &AtomicBool::new(false)).is_some());
    }

    #[test]
    fn evicting_a_cached_result_invalidates_transcript_layout() {
        if !Path::new(BWRAP).is_file() { return; }
        let (root, renderer) = fixture("exit 7");
        let package = root.path().to_string_lossy().into_owned();
        let mut store = Store { renderer: renderer.clone(), root: package.clone(),
            workspace: Vec::new(), previews: Vec::new(), revision: 0 };
        for index in 0..MAX_CACHED {
            store.previews.push(Preview { key: format!("old-{index}"), width: 24,
                cancel: Arc::new(AtomicBool::new(false)), state: State::Unavailable });
        }
        store.observe("```mermaid\ngraph TD\nA-->B\n```", 24,
            &renderer, &package, &[], Some(picker()));
        assert_eq!(store.previews.len(), MAX_CACHED);
        assert!(store.revision() > 0);
        store.clear();
    }

    /// Run only through scripts/mermaid-validation/validate.sh after explicitly
    /// provisioning its pinned CLI and browser inside a reviewed package root.
    #[test]
    #[ignore = "requires an owner-provisioned Mermaid CLI and browser"]
    fn real_cli_fixture_suite() {
        let renderer = std::env::var("DOXA_MERMAID_VALIDATION_RENDERER")
            .expect("validation script must set the renderer");
        let root = std::env::var("DOXA_MERMAID_VALIDATION_PACKAGE_ROOT")
            .expect("validation script must set the package root");
        assert_eq!(diagnose(&renderer, &root, &[]), DoctorResult::Available,
            "the fixed-diagram sandbox doctor must pass before fixture rendering");
        let fixtures = [
            ("flowchart", "flowchart TD\n A[Start] --> B{Ready?}\n B -->|yes| C[Done]\n"),
            ("sequence", "sequenceDiagram\n participant A as Alice\n participant B as Bob\n A->>B: Hello\n B-->>A: Ack\n"),
            ("class", "classDiagram\n class Animal {\n  +name: string\n  +speak()\n }\n Animal <|-- Dog\n"),
            ("gantt", "gantt\n title Delivery\n dateFormat YYYY-MM-DD\n section Work\n Design :done, d1, 2026-01-01, 3d\n Build :active, d2, after d1, 4d\n"),
        ];
        for (name, source) in fixtures {
            let image = render_source(source, 60, &renderer, &root, &[],
                &picker(), &AtomicBool::new(false))
                .unwrap_or_else(|| panic!("{name}: sandboxed renderer failed or PNG was invalid"));
            assert!(image.area().height > 0, "{name}: no decoded image rows");
            println!("{name}: bounded sandbox render and halfblock decode passed");
        }
    }
}
