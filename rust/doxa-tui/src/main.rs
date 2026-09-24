use doxa_tui::{bridge, discovery, launch, ui_state};
use std::io;
use std::path::PathBuf;

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

fn main() -> io::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = run(&args);
    if let Err(error) = &result {
        eprintln!("doxa-rs: {error}");
    }
    result
}

fn run(args: &[String]) -> io::Result<()> {
    let mut command: Option<&str> = None;
    let mut prefix: Option<&str> = None;
    let mut options = launch::LaunchOptions::default();
    let mut socket: Option<&str> = None;
    let mut index = 0;
    while index < args.len() {
        let arg = args[index].as_str();
        match arg {
            "new" | "attach" | "stop" | "list" | "doctor" | "--list" | "--demo" | "--version"
            | "--help"
                if command.is_none() =>
            {
                command = Some(arg)
            }
            "--session" | "--socket" | "--engine" | "--model" | "--linger" | "--sandbox"
            | "--codex-bin" | "--lore-python" => {
                index += 1;
                let value = args
                    .get(index)
                    .ok_or_else(|| invalid(format!("missing value for {arg}")))?;
                match arg {
                    "--session" => {
                        if prefix.is_some() {
                            return Err(invalid("session ID specified twice"));
                        }
                        prefix = Some(value);
                    }
                    "--socket" => socket = Some(value),
                    "--engine" => match value.as_str() {
                        "codex" => options.fixture = false,
                        "fixture" => options.fixture = true,
                        _ => return Err(invalid("native engine must be codex or fixture")),
                    },
                    "--model" => options.model = Some(value.clone()),
                    "--linger" => {
                        options.linger = Some(value.parse().map_err(|_| invalid("invalid linger"))?)
                    }
                    "--sandbox" => options.sandbox = Some(value.clone()),
                    "--codex-bin" => options.codex_bin = Some(PathBuf::from(value)),
                    "--lore-python" => options.lore_python = Some(PathBuf::from(value)),
                    _ => unreachable!(),
                }
            }
            _ if !arg.starts_with('-')
                && matches!(command, Some("attach" | "stop"))
                && prefix.is_none() =>
            {
                prefix = Some(arg)
            }
            _ => return Err(invalid(format!("unexpected argument: {arg}"))),
        }
        index += 1;
    }
    if socket.is_some() && (command.is_some() || prefix.is_some()) {
        return Err(invalid(
            "--socket cannot be combined with a command or session ID",
        ));
    }
    if let Some(socket) = socket {
        return bridge::run_socket(socket);
    }
    match command {
        Some("--help") => {
            println!("Usage: doxa-rs [new|attach [ID]|stop [ID]|list|doctor] [options]\n       doxa-rs --session ID\n       doxa-rs --socket PATH\n\nPlain doxa-rs restores live sessions in the current project, or starts a native Codex session.\nnew always starts a session. attach and stop accept a full ID or unique prefix.\nOptions for new sessions: --engine codex|fixture, --model NAME, --linger SECONDS,\n--sandbox read-only|workspace-write|danger-full-access, --codex-bin PATH,\n--lore-python PATH. DOXA_DAEMON_BIN selects an absolute native daemon path.\nCtrl+Q detaches without stopping the daemon.");
            Ok(())
        }
        Some("--version") => {
            println!("doxa-rs {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        Some("--demo") => doxa_tui::ui::run(),
        Some("list" | "--list") => {
            for session in discovery::sessions()? {
                println!(
                    "{}  clients:{}  scope:{}",
                    session.id,
                    session.clients.map_or("?".into(), |n| n.to_string()),
                    session.scope_key
                );
            }
            Ok(())
        }
        Some("doctor") => {
            let daemon = launch::daemon_binary();
            let codex = launch::executable(
                options
                    .codex_bin
                    .as_deref()
                    .unwrap_or(std::path::Path::new("codex")),
            );
            let python = launch::executable(
                options
                    .lore_python
                    .as_deref()
                    .unwrap_or(std::path::Path::new("python3")),
            );
            let runtime = discovery::runtime_dir();
            let mut missing = false;
            for (name, result) in [
                ("daemon", daemon),
                ("codex", codex),
                ("python", python),
                ("runtime", runtime),
            ] {
                match result {
                    Ok(path) => println!("ok {name}: {}", path.display()),
                    Err(error) => {
                        println!("missing {name}: {error}");
                        missing = true;
                    }
                }
            }
            let live = discovery::sessions()?;
            println!("live sessions: {}", live.len());
            if missing {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    "one or more native startup dependencies are missing",
                ));
            }
            Ok(())
        }
        Some("stop") => {
            let sessions = discovery::sessions()?;
            let session = discovery::select(&sessions, prefix)?;
            launch::stop(session)?;
            println!("stopped {}", session.id);
            Ok(())
        }
        Some("attach") => {
            let sessions = discovery::sessions()?;
            let session = discovery::select(&sessions, prefix)?;
            bridge::run_socket(&session.socket)
        }
        Some("new") => {
            if prefix.is_some() {
                return Err(invalid("new does not accept a session ID"));
            }
            let session = launch::spawn(&options)?;
            eprintln!("started native session {}", session.id);
            bridge::run_socket(&session.socket)
        }
        None if prefix.is_some() => {
            let sessions = discovery::sessions()?;
            let session = discovery::select(&sessions, prefix)?;
            bridge::run_socket(&session.socket)
        }
        None => {
            let scope = discovery::current_scope()?;
            let sessions: Vec<_> = discovery::sessions()?
                .into_iter()
                .filter(|s| s.scope_key == scope)
                .collect();
            if sessions.is_empty() {
                let session = launch::spawn(&options)?;
                eprintln!("started native session {}", session.id);
                bridge::run_socket(&session.socket)
            } else {
                let home = std::env::var_os("DOXA_HOME")
                    .filter(|s| !s.is_empty())
                    .map(PathBuf::from)
                    .or_else(|| std::env::var_os("HOME").map(|s| PathBuf::from(s).join(".doxa")));
                let store = home.and_then(|home| {
                    doxa_state::machine_id(&home).ok().and_then(|machine| {
                        ui_state::UiStateStore::new(&home, &scope, &machine).ok()
                    })
                });
                bridge::run_sessions(&sessions, store)
            }
        }
        _ => Err(invalid("invalid command")),
    }
}
