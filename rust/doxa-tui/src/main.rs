fn main() -> std::io::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.as_slice() {
        [flag, path] if flag == "--socket" => doxa_tui::bridge::run_socket(path),
        [flag] if flag == "--list" => {
            let sessions = doxa_tui::discovery::sessions()?;
            if sessions.is_empty() {
                println!("No live DOXA daemon sessions.");
            }
            for session in sessions {
                let clients = session.clients.map_or("?".into(), |n| n.to_string());
                println!("{}  {}  clients:{}", session.id, session.title, clients);
            }
            Ok(())
        }
        [flag, id] if flag == "--session" => attach(Some(id)),
        [] => attach(None),
        [flag] if flag == "--demo" => doxa_tui::ui::run(),
        [flag] if flag == "--version" => {
            println!("doxa-rs {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        [flag] if flag == "--help" => {
            println!("Usage: doxa-rs [--session ID]\n       doxa-rs --list\n       doxa-rs --socket PATH\n       doxa-rs --demo\n\nAttach to a live DOXA daemon session. An ID can be a unique prefix. Ctrl+Q exits without stopping the daemon.");
            Ok(())
        }
        _ => Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "use --list, --session ID, --socket PATH, or --help",
        )),
    }
}

fn attach(prefix: Option<&str>) -> std::io::Result<()> {
    let sessions = doxa_tui::discovery::sessions()?;
    let session = doxa_tui::discovery::select(&sessions, prefix)?;
    doxa_tui::bridge::run_socket(&session.socket)
}
