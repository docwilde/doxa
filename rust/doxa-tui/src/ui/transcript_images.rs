//! Bounded local transcript image previews. Decoding and protocol encoding
//! happen on workers; only completed Ratatui widgets touch the frame buffer.
use std::{
    ffi::CString,
    fs::{File, OpenOptions},
    io::{BufReader, Seek, SeekFrom},
    os::{fd::{AsRawFd, FromRawFd}, unix::{ffi::OsStrExt, fs::OpenOptionsExt}},
    path::{Component, Path, PathBuf},
    sync::mpsc::{self, Receiver, TryRecvError},
};
use ratatui::{layout::Rect, style::Style, widgets::Paragraph, Frame};
use ratatui_image::{picker::{Picker, ProtocolType}, protocol::Protocol, Image, Resize};

use crate::theme;

const MAX_PREVIEWS: usize = 8;
const MAX_ACTIVE: usize = 2;
const MAX_FILE_BYTES: u64 = 8 * 1024 * 1024;
const MAX_PIXELS: u64 = 8_000_000;

struct Preview {
    source: String,
    root: PathBuf,
    width: u16,
    state: State,
}
enum State {
    Loading(Receiver<Option<Protocol>>),
    Ready(Protocol),
    Unavailable,
}

#[derive(Default)]
pub(super) struct Store {
    picker: Option<Picker>,
    previews: Vec<Preview>,
}

impl std::fmt::Debug for Store {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ImageStore")
            .field("enabled", &self.picker.is_some())
            .field("preview_count", &self.previews.len())
            .finish()
    }
}

impl Store {
    pub fn clear(&mut self) { self.previews.clear(); }

    pub fn configure(&mut self, mode: &str) {
        self.previews.clear();
        if matches!(mode, "text" | "off") {
            self.picker = None;
            return;
        }
        // Probe only on explicit request, after the first alternate-screen
        // frame and before the terminal event reader begins.
        let mut picker = if mode == "probe" {
            Picker::from_query_stdio().unwrap_or_else(|_| Picker::from_fontsize((8, 16)))
        } else {
            Picker::from_fontsize((8, 16))
        };
        let forced = match mode {
            "kgp" => Some(ProtocolType::Kitty),
            "sixel" => Some(ProtocolType::Sixel),
            "iterm2" => Some(ProtocolType::Iterm2),
            "halfblock" | "" => Some(ProtocolType::Halfblocks),
            _ => Some(ProtocolType::Halfblocks),
        };
        if let Some(protocol) = forced { picker.set_protocol_type(protocol); }
        picker.set_background_color(image::Rgba([0x17, 0x15, 0x12, 0xff]));
        self.picker = Some(picker);
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
        changed
    }

    pub fn draw(&mut self, frame: &mut Frame, area: Rect, source: &str, alt: &str,
        root: Option<&Path>) {
        if area.width == 0 || area.height == 0 { return; }
        let Some(picker) = self.picker.as_ref() else { return; };
        let Some(root) = root else {
            paint_fallback(frame, area, &format!("Image unavailable: {alt}"));
            return;
        };
        let position = self.previews.iter().position(|preview|
            preview.source == source && preview.root == root && preview.width == area.width);
        let position = if let Some(position) = position { position } else {
            if self.previews.iter().filter(|preview| matches!(preview.state, State::Loading(_))).count() >= MAX_ACTIVE {
                paint_fallback(frame, area, "Image queued");
                return;
            }
            if self.previews.len() >= MAX_PREVIEWS {
                if let Some(index) = self.previews.iter().position(|preview| !matches!(preview.state, State::Loading(_))) {
                    self.previews.remove(index);
                } else {
                    paint_fallback(frame, area, "Image queued");
                    return;
                }
            }
            let (sender, receiver) = mpsc::sync_channel(1);
            let source_owned = source.to_owned();
            let worker_source = source_owned.clone();
            let worker_root = root.to_path_buf();
            let worker_picker = picker.clone();
            let width = area.width;
            std::thread::spawn(move || {
                let _ = sender.send(load(&worker_source, &worker_root, width, &worker_picker));
            });
            self.previews.push(Preview { source: source_owned, root: root.to_path_buf(),
                width, state: State::Loading(receiver) });
            self.previews.len() - 1
        };
        match &self.previews[position].state {
            State::Ready(protocol) => frame.render_widget(Image::new(protocol), area),
            State::Loading(_) => paint_fallback(frame, area, "Loading image…"),
            State::Unavailable => paint_fallback(frame, area, &format!("Image unavailable: {alt}")),
        }
    }
}

fn paint_fallback(frame: &mut Frame, area: Rect, label: &str) {
    let label = label.chars().take(usize::from(area.width)).collect::<String>();
    frame.render_widget(Paragraph::new(label).style(Style::default().fg(theme::MUTED)), area);
}

fn open_workspace_file(source: &str, root: &Path) -> Option<File> {
    let root = root.canonicalize().ok()?;
    let source = Path::new(source).canonicalize().ok()?;
    let relative = source.strip_prefix(&root).ok()?;
    let parts = relative.components().map(|part| match part {
        Component::Normal(name) => CString::new(name.as_bytes()).ok(),
        _ => None,
    }).collect::<Option<Vec<_>>>()?;
    if parts.is_empty() { return None; }
    // Pin the root inode, then walk every component without following links.
    // A swap after canonicalization cannot redirect the opened handle outside
    // the workspace.
    let mut directory = OpenOptions::new().read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(&root).ok()?;
    for (index, component) in parts.iter().enumerate() {
        let last = index + 1 == parts.len();
        let flags = libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW
            | if last { libc::O_NONBLOCK } else { libc::O_DIRECTORY };
        let fd = unsafe { libc::openat(directory.as_raw_fd(), component.as_ptr(), flags, 0) };
        if fd < 0 { return None; }
        let opened = unsafe { File::from_raw_fd(fd) };
        if last { return Some(opened); }
        directory = opened;
    }
    None
}

fn load(source: &str, root: &Path, width: u16, picker: &Picker) -> Option<Protocol> {
    let mut file = open_workspace_file(source, root)?;
    let metadata = file.metadata().ok()?;
    if !metadata.is_file() || metadata.len() > MAX_FILE_BYTES { return None; }
    let reader = image::ImageReader::new(BufReader::new(&mut file))
        .with_guessed_format().ok()?;
    let (w, h) = reader.into_dimensions().ok()?;
    if w == 0 || h == 0 || u64::from(w) * u64::from(h) > MAX_PIXELS { return None; }
    file.seek(SeekFrom::Start(0)).ok()?;
    let mut reader = image::ImageReader::new(BufReader::new(file))
        .with_guessed_format().ok()?;
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(8_192);
    limits.max_image_height = Some(8_192);
    limits.max_alloc = Some(64 * 1024 * 1024);
    reader.limits(limits);
    let decoded = reader.decode().ok()?;
    picker.new_protocol(decoded, Rect::new(0, 0, width, super::transcript_tools::IMAGE_ROWS),
        Resize::Fit(None)).ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_missing_and_oversized_sources() {
        let mut picker = Picker::from_fontsize((8, 16));
        picker.set_protocol_type(ProtocolType::Halfblocks);
        assert!(load("/missing/doxa-image.png", Path::new("/missing"), 30, &picker).is_none());
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("large.png");
        std::fs::write(&path, vec![0; MAX_FILE_BYTES as usize + 1]).unwrap();
        assert!(load(path.to_str().unwrap(), dir.path(), 30, &picker).is_none());
    }

    #[test]
    fn local_png_encodes_for_ratatui_halfblocks() {
        let mut picker = Picker::from_fontsize((8, 16));
        picker.set_protocol_type(ProtocolType::Halfblocks);
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("small.png");
        let pixels = image::RgbaImage::from_pixel(32, 32, image::Rgba([220, 120, 80, 255]));
        pixels.save(&path).unwrap();
        let protocol = load(path.to_str().unwrap(), dir.path(), 20, &picker).unwrap();
        assert!(protocol.area().width > 0);
        assert!(protocol.area().height > 0);
        let backend = ratatui::backend::TestBackend::new(20, 4);
        let mut terminal = ratatui::Terminal::new(backend).unwrap();
        terminal.draw(|frame| frame.render_widget(Image::new(&protocol), frame.area())).unwrap();
        assert!(terminal.backend().buffer().content().iter().any(|cell|
            cell.symbol() == "▀" || cell.symbol() == "▄"));
    }

    #[test]
    fn outside_workspace_and_symlink_escape_are_refused_before_decode() {
        let root = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let image = outside.path().join("private.png");
        image::RgbaImage::from_pixel(4, 4, image::Rgba([255, 0, 0, 255]))
            .save(&image).unwrap();
        let escaped = root.path().join("escaped.png");
        std::os::unix::fs::symlink(&image, &escaped).unwrap();
        assert!(open_workspace_file(image.to_str().unwrap(), root.path()).is_none());
        assert!(open_workspace_file(escaped.to_str().unwrap(), root.path()).is_none());
        assert!(open_workspace_file(image.to_str().unwrap(), Path::new("/missing")).is_none());
        let mut picker = Picker::from_fontsize((8, 16));
        picker.set_protocol_type(ProtocolType::Halfblocks);
        assert!(load(escaped.to_str().unwrap(), root.path(), 20, &picker).is_none());
    }
}
