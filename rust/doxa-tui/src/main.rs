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
                println!("{}  clients:{}", session.id, clients);
            }
            Ok(())
        }
        [flag, id] if flag == "--session" => attach(Some(id)),
        [] => attach_project(),
        [flag] if flag == "--demo" => doxa_tui::ui::run(),
        [flag] if flag == "--version" => {
            println!("doxa-rs {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        [flag] if flag == "--help" => {
            println!("Usage: doxa-rs [--session ID]\n       doxa-rs --list\n       doxa-rs --socket PATH\n       doxa-rs --demo\n\nWithout arguments, attach to all live sessions in the current project scope and restore its tab layout. An ID can be a unique prefix. Ctrl+Q exits without stopping daemons.");
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

fn attach_project() -> std::io::Result<()> {
    let scope = doxa_tui::discovery::current_scope()?;
    let sessions: Vec<_> = doxa_tui::discovery::sessions()?.into_iter()
        .filter(|session| session.scope_key == scope).collect();
    if sessions.is_empty() {
        return Err(std::io::Error::new(std::io::ErrorKind::NotFound,
            "no live daemon sessions in this project; use --list or --session ID"));
    }
    let home = std::env::var_os("DOXA_HOME").filter(|s| !s.is_empty())
        .map(std::path::PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|s| std::path::PathBuf::from(s).join(".doxa")));
    let store = home.and_then(|home| doxa_state::machine_id(&home).ok()
        .and_then(|machine| doxa_tui::ui_state::UiStateStore::new(&home, &scope, &machine).ok()));
    doxa_tui::bridge::run_sessions(&sessions, store)
}
