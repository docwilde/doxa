//! Feed fixture JSON frames into the production Crossterm event loop.
//! External PTY benchmark controls keyboard and terminal size.
use std::io::{self, BufRead, BufReader};
use std::os::unix::net::UnixListener;
use std::sync::mpsc;
use std::thread;

fn main() -> io::Result<()> {
    let path = std::env::args().nth(1)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "socket path required"))?;
    let listener = UnixListener::bind(&path)?;
    let (send, receive) = mpsc::channel();
    thread::spawn(move || {
        if let Ok((stream, _)) = listener.accept() {
            for line in BufReader::new(stream).lines() {
                let Ok(line) = line else { break; };
                let Ok(frame) = serde_json::from_str(&line) else { continue; };
                if send.send(frame).is_err() { break; }
            }
        }
    });
    let result = doxa_tui::ui::run_with_frames(receive);
    let _ = std::fs::remove_file(path);
    result
}
