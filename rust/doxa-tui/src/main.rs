use doxa_tui::{bridge, discovery, fleet_plan, fleet_view, launch, ui_state};
use std::collections::HashSet;
use std::io::{self, Write};
use serde_json::{Map, Value};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::process::Stdio;
mod operations;

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

fn main() -> std::process::ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("doxa: {error}");
            std::process::ExitCode::FAILURE
        }
    }
}

const HELP: &str = r#"DOXA Rust 2.0 alpha

Usage: doxa [COMMAND] [options]

Commands:
  help                 Show this help
  update               Build and install the latest Rust main build
  new                  Start an isolated session
  attach [ID]          Reattach a live session by full ID or unique prefix
  stop [ID]            Stop a live session
  list                 List live sessions
  branch [NAME]        List branches; use NAME --session ID to switch an idle session
  worktrees [list]     Preview orphaned managed worktrees
  worktrees cleanup FULL_ID --confirm
                       Remove one verified clean Rust orphan
  doctor               Check provider and launcher dependencies
  setup                Inspect authentication, LORE store, and stored preferences
  settings             Show native settings and their effective sources
  settings set KEY VALUE | unset KEY
                       Persist linger_secs or worktree_per_session for new sessions
  auth status [NAME]   Check Claude or Codex CLI authentication without showing CLI output
  plugins              List names and enabled flags from Claude Code's plugin registry
  fleet ...            Inspect or start Python-backed fleet runs

Run doxa without a command to restore this project's live sessions or start
a native Codex session. Ctrl+Q detaches without stopping its daemon.

New-session options: --engine codex|claude|deepseek|glm, --model NAME,
  --branch LOCAL_OR_REMOTE, --linger SECONDS, --resume FULL_SESSION_ID.
Codex: --sandbox read-only|workspace-write|danger-full-access, --codex-bin PATH.
Claude: --claude-python PATH, --claude-script ABSOLUTE_PATH.
DeepSeek/GLM: --effort low|high|max (DeepSeek also none).
Use --lore-python PATH for the LORE sidecar; API keys come from provider env vars.

Fleet: doxa fleet preflight --sessions N --run-budget USD [--supervisor ENGINE[:MODEL]] [--approve none|peer|all] [--approval-grace SECONDS] [--root PATH]
       doxa fleet start [Python fleet options]
       doxa fleet runs | status RUN_ID | stop RUN_ID | attach RUN_ID SLOT

Run doxa doctor --engine NAME to check a provider; doxa --version shows the build.
Update requires an installed Rust launcher. From a checkout, use ./task install.
DOXA_RUST_REPO_URL can select a different update source.
"#;

fn installed_bin_dir_for(executable: &Path) -> io::Result<PathBuf> {
    let bin_dir = executable.parent().ok_or_else(|| invalid("cannot locate installed launcher"))?;
    if executable.file_name().is_none_or(|name| name != "doxa-rs")
        || !bin_dir.join("doxa").is_file()
        || !bin_dir.join("doxa-daemon-rs").is_file()
        || !std::fs::symlink_metadata(bin_dir.join(".doxa-sidecar-current"))
            .is_ok_and(|meta| meta.file_type().is_symlink())
    {
        return Err(invalid("update requires an installed Rust doxa launcher; from a source checkout run ./task install"));
    }
    Ok(bin_dir.to_path_buf())
}

fn installed_bin_dir() -> io::Result<PathBuf> {
    installed_bin_dir_for(&std::env::current_exe()?)
}

fn run_update_installer(bin_dir: &Path, shell: &Path) -> io::Result<()> {
    let mut child = Command::new(shell)
        .args(["-s", "--", "main"])
        .env("DOXA_RUST_BIN_DIR", bin_dir)
        .stdin(Stdio::piped())
        .spawn()?;
    let mut stdin = child.stdin.take().ok_or_else(|| io::Error::other("installer input unavailable"))?;
    let write = stdin.write_all(include_bytes!("../../../scripts/install.sh"));
    drop(stdin);
    let status = child.wait()?;
    write?;
    if !status.success() {
        return Err(io::Error::other(format!("update failed with installer status {status}")));
    }
    Ok(())
}

fn update() -> io::Result<()> {
    let bin_dir = installed_bin_dir()?;
    let repo = std::env::var("DOXA_RUST_REPO_URL")
        .unwrap_or_else(|_| "https://github.com/docwilde/doxa".to_owned());
    eprintln!("Updating DOXA Rust from {repo} main into {}", bin_dir.display());
    run_update_installer(&bin_dir, Path::new("/bin/sh"))
}

fn run(args: &[String]) -> io::Result<()> {
    if let Some(command) = args.first().map(String::as_str) {
        match command {
            "help" | "--help" | "-help" | "-h" => {
                if args.len() != 1 { return Err(invalid("help takes no arguments")); }
                print!("{HELP}");
                return Ok(());
            }
            "update" => {
                if args.len() != 1 { return Err(invalid("update takes no arguments")); }
                return update();
            }
            "setup" => {
                if args.len() != 1 { return Err(invalid("setup takes no arguments")); }
                println!("{}", operations::setup_report()?);
                return Ok(());
            }
            "settings" => {
                match args {
                    [_] => println!("{}", operations::settings_report()?),
                    [_, action, key, value] if action == "set" =>
                        println!("{}", operations::settings_change(key, Some(value))?),
                    [_, action, key] if action == "unset" =>
                        println!("{}", operations::settings_change(key, None)?),
                    _ => return Err(invalid("usage: doxa settings [set KEY VALUE | unset KEY]")),
                }
                return Ok(());
            }
            "auth" => {
                let name = match args {
                    [_, status] if status == "status" => None,
                    [_, status, name] if status == "status" => Some(name.as_str()),
                    _ => return Err(invalid("usage: doxa auth status [claude|codex]")),
                };
                println!("{}", operations::auth_status(name)?);
                return Ok(());
            }
            "plugins" => {
                if args.len() != 1 { return Err(invalid("plugins takes no arguments")); }
                println!("{}", operations::plugins_report()?);
                return Ok(());
            }
            _ => {}
        }
    }
    if args.first().is_some_and(|arg| arg == "fleet") {
        return fleet(&args[1..]);
    }
    if args.first().is_some_and(|arg| arg == "worktrees") {
        return worktrees(&args[1..]);
    }
    let mut command: Option<&str> = None;
    let mut prefix: Option<&str> = None;
    let mut branch_target: Option<&str> = None;
    let mut options = launch::LaunchOptions::default();
    let mut socket: Option<&str> = None;
    let mut index = 0;
    while index < args.len() {
        let arg = args[index].as_str();
        match arg {
            "new" | "attach" | "stop" | "list" | "doctor" | "branch" | "--list" | "--demo" | "--version"
                if command.is_none() =>
            {
                command = Some(arg)
            }
            "--session" | "--socket" | "--engine" | "--model" | "--effort" | "--linger"
            | "--sandbox" | "--codex-bin" | "--lore-python" | "--claude-python"
            | "--claude-script" | "--resume" | "--branch" => {
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
                    "--engine" => {
                        match value.as_str() {
                            "codex" => options.engine = launch::Engine::Codex,
                            "claude" => options.engine = launch::Engine::Claude,
                            "fixture" => options.engine = launch::Engine::Fixture,
                            "deepseek" => options.engine = launch::Engine::DeepSeek,
                            "glm" => options.engine = launch::Engine::Glm,
                            _ => return Err(invalid(
                                "native engine must be codex, claude, deepseek, glm, or fixture",
                            )),
                        }
                    }
                    "--model" => options.model = Some(value.clone()),
                    "--effort" => options.effort = Some(value.clone()),
                    "--linger" => {
                        options.linger = Some(value.parse().map_err(|_| invalid("invalid linger"))?)
                    }
                    "--sandbox" => options.sandbox = Some(value.clone()),
                    "--codex-bin" => options.codex_bin = Some(PathBuf::from(value)),
                    "--lore-python" => options.lore_python = Some(PathBuf::from(value)),
                    "--claude-python" => options.claude_python = Some(PathBuf::from(value)),
                    "--claude-script" => options.claude_script = Some(PathBuf::from(value)),
                    "--resume" => options.resume = Some(value.clone()),
                    "--branch" => options.branch = Some(value.clone()),
                    _ => unreachable!(),
                }
            }
            _ if !arg.starts_with('-')
                && matches!(command, Some("attach" | "stop"))
                && prefix.is_none() =>
            {
                prefix = Some(arg)
            }
            _ if !arg.starts_with('-') && command == Some("branch") && branch_target.is_none() => {
                branch_target = Some(arg)
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
    if options.resume.is_some() && command != Some("new") {
        return Err(invalid(
            "--resume requires new --engine codex|claude|deepseek|glm",
        ));
    }
    if options.branch.is_some() && command != Some("new") {
        return Err(invalid("--branch requires new"));
    }
    match command {
        Some("--version") => {
            println!("doxa {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        Some("--demo") => doxa_tui::ui::run(),
        Some("branch") => {
            if let Some(target) = branch_target {
                let id = prefix.ok_or_else(|| invalid("branch NAME requires --session ID so the daemon can verify that the session is idle"))?;
                let sessions = discovery::sessions()?;
                let session = discovery::select(&sessions, Some(id))?;
                let mut client = doxa_tui::transport::DaemonClient::connect(&session.socket, None)
                    .map_err(io::Error::other)?;
                if client.hello["session_id"] != session.id {
                    return Err(invalid("session identity changed during branch switch"));
                }
                let mut params = Map::new();
                params.insert("name".into(), Value::String(target.to_owned()));
                let reply = client.call("switch_branch", params).map_err(io::Error::other)?;
                if reply["ok"] != true {
                    return Err(invalid(reply["error"].as_str().unwrap_or("branch switch refused")));
                }
                println!("branch: {}", reply["message"].as_str().unwrap_or("switched"));
                return Ok(());
            }
            if let Some(id) = prefix {
                let sessions = discovery::sessions()?;
                let session = discovery::select(&sessions, Some(id))?;
                let mut client = doxa_tui::transport::DaemonClient::connect(&session.socket, None)
                    .map_err(io::Error::other)?;
                if client.hello["session_id"] != session.id {
                    return Err(invalid("session identity changed during branch listing"));
                }
                let reply = client.call("branch", Map::new()).map_err(io::Error::other)?;
                if reply["ok"] != true { return Err(invalid(reply["error"].as_str().unwrap_or("branch listing refused"))); }
                println!("branch: {}", reply["base"].as_str().unwrap_or("(none)"));
                println!();
                for name in reply["branches"].as_array().into_iter().flatten().filter_map(|row| row.as_str()) {
                    let mark = if Some(name) == reply["base"].as_str() { "▸" } else { " " };
                    println!(" {mark} {name}");
                }
                println!("\nusage: doxa branch NAME --session ID");
                return Ok(());
            }
            let cwd = std::env::current_dir()?;
            let status = doxa_worktrees::branch_status(&cwd)
                .ok_or_else(|| invalid("branch: no supported Git checkout here"))?;
            println!("branch: {}", status.base.as_deref().unwrap_or("(none)"));
            println!();
            for name in status.branches {
                let mark = if Some(name.as_str()) == status.base.as_deref() { "▸" } else { " " };
                println!(" {mark} {name}");
            }
            println!("\nstart an isolated session: doxa new --branch NAME");
            Ok(())
        }
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
            let runtime = discovery::runtime_dir();
            let mut missing = false;
            let mut checks = vec![("daemon", daemon), ("runtime", runtime)];
            match options.engine {
                launch::Engine::Codex => {
                    checks.push((
                        "codex",
                        launch::executable(
                            options
                                .codex_bin
                                .as_deref()
                                .unwrap_or(std::path::Path::new("codex")),
                        ),
                    ));
                    checks.push((
                        "lore python",
                        launch::python_executable(
                            options
                                .lore_python
                                .as_deref()
                                .unwrap_or(std::path::Path::new("python3")),
                        ),
                    ));
                }
                launch::Engine::Claude => {
                    checks.push((
                        "claude python",
                        launch::python_executable(
                            options
                                .claude_python
                                .as_deref()
                                .unwrap_or(std::path::Path::new("python3")),
                        ),
                    ));
                    checks.push((
                        "claude sidecar",
                        launch::resolve_claude_script(&options),
                    ));
                }
                launch::Engine::Fixture => {}
                launch::Engine::DeepSeek | launch::Engine::Glm => {
                    checks.push((
                        "lore python",
                        launch::python_executable(
                            options
                                .lore_python
                                .as_deref()
                                .unwrap_or(std::path::Path::new("python3")),
                        ),
                    ));
                    if let Err(error) = launch::vendor_effort(&options) {
                        println!("missing vendor effort: {error}");
                        missing = true;
                    }
                    let key = options.engine.vendor_key().expect("vendor engine");
                    if std::env::var(key).is_ok_and(|value| !value.is_empty()) {
                        println!("ok {key}: set");
                    } else {
                        println!("missing {key}: unset");
                        missing = true;
                    }
                }
            }
            for (name, result) in checks {
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
            let ids: HashSet<String> = live.iter().map(|session| session.id.clone()).collect();
            let orphans = doxa_worktrees::list_orphans(&ids);
            println!("managed worktrees without a live session: {}", orphans.len());
            for tree in orphans {
                println!("  {:?}  branch:{:?}  session:{}", tree.path, tree.branch, tree.session_id);
            }
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
                let store =
                    home.and_then(|home| ui_state::UiStateStore::for_scope(&home, &scope).ok());
                bridge::run_sessions(&sessions, store)
            }
        }
        _ => Err(invalid("invalid command")),
    }
}

fn worktrees(args: &[String]) -> io::Result<()> {
    let cleanup_id = match args {
        [] => None,
        [action] if action == "list" => None,
        [action, id, confirm] if action == "cleanup" && confirm == "--confirm" && !id.is_empty() => Some(id.as_str()),
        _ => return Err(invalid("usage: doxa worktrees [list] | cleanup FULL_SESSION_ID --confirm")),
    };
    let live: HashSet<String> = discovery::sessions()?.into_iter().map(|session| session.id).collect();
    let previews = doxa_worktrees::preview_orphans(&live);
    if let Some(id) = cleanup_id {
        let mut matches = previews.into_iter().filter(|preview| preview.record.session_id == id);
        let preview = matches.next().ok_or_else(|| invalid("no verified orphan has that full session ID"))?;
        if matches.next().is_some() {
            return Err(invalid("more than one orphan has that session ID; cleanup refused"));
        }
        match doxa_worktrees::cleanup_orphan(&preview, || {
            discovery::sessions().ok().map(|sessions| sessions.into_iter().map(|session| session.id).collect())
        }) {
            doxa_worktrees::CleanupResult::Removed => {
                println!("removed clean orphan {} ({})", id, preview.record.branch);
                return Ok(());
            }
            doxa_worktrees::CleanupResult::Kept(reason) => {
                return Err(io::Error::other(format!("kept {id}: {reason}")));
            }
        }
    }
    println!("managed worktrees without an attachable session: {}", previews.len());
    for preview in previews {
        let state = match preview.state {
            doxa_worktrees::OrphanState::Ready { .. } => "clean; eligible for explicit cleanup",
            doxa_worktrees::OrphanState::Dirty => "dirty; kept",
            doxa_worktrees::OrphanState::UniqueCommits => "unique commits; kept",
            doxa_worktrees::OrphanState::Uncertain => "uncertain; kept",
        };
        println!("  {}  {state}  branch:{:?}  path:{:?}",
            preview.record.session_id, preview.record.branch, preview.record.path);
    }
    Ok(())
}

fn fleet(args: &[String]) -> io::Result<()> {
    if args.first().is_some_and(|arg| arg == "start") {
        return fleet_start_compat(&args[1..]);
    }
    if args.first().is_some_and(|arg| arg == "preflight") {
        let root = fleet_view::default_root().unwrap_or_default();
        let plan = fleet_plan::parse(&args[1..], &root)?;
        println!("{}", fleet_plan::check(&plan, fleet_plan::available_memory_mb())?);
        return Ok(());
    }
    let mut root = None;
    let mut words = Vec::new();
    let mut index = 0;
    while index < args.len() {
        if args[index] == "--root" {
            index += 1;
            root = Some(PathBuf::from(args.get(index).ok_or_else(|| invalid("missing fleet root"))?));
        } else {
            words.push(args[index].as_str());
        }
        index += 1;
    }
    let root = match root { Some(root) => root, None => fleet_view::default_root()? };
    match words.as_slice() {
        ["runs"] => println!("{}", fleet_view::runs(&root)?),
        ["status", run] => println!("{}", fleet_view::status(&root, run)?),
        ["stop", run] => {
            let report = fleet_view::stop(&root, run)?;
            println!("{}", report.text);
            if !report.complete {
                return Err(io::Error::other("one or more fleet daemon connections did not close"));
            }
        }
        ["attach", run, slot] => {
            let slot: usize = slot.parse().map_err(|_| invalid("fleet slot must be a number"))?;
            let (socket, session_id) = fleet_view::slot_socket(&root, run, slot)?;
            return bridge::run_socket_expected(socket, Some(&session_id));
        }
        _ => return Err(invalid("usage: doxa fleet start PYTHON_FLEET_OPTIONS|preflight --sessions N --run-budget USD [--supervisor ENGINE[:MODEL]] [--approve none|peer|all] [--approval-grace SECONDS] [--root ABSOLUTE_PATH]|runs|status RUN_ID|stop RUN_ID|attach RUN_ID SLOT [--root ABSOLUTE_PATH]")),
    }
    Ok(())
}

fn fleet_start_compat(args: &[String]) -> io::Result<()> {
    let selected = std::env::var_os("DOXA_LORE_PYTHON")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("python3"));
    let python = launch::python_executable(&selected)?;
    let packaged = Command::new(&python).args(["-c", "import doxa.fleet"])
        .stdout(Stdio::null()).stderr(Stdio::null()).status().is_ok_and(|status| status.success());
    let mut command = Command::new(python);
    command.args(["-m", "doxa.fleet"]).args(args);
    // Source builds run from arbitrary project directories. Installed builds
    // use the packaged sidecar environment after the source tree is gone.
    let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    if !packaged && source.join("doxa/fleet.py").is_file() {
        let mut paths = vec![source];
        paths.extend(std::env::split_paths(&std::env::var_os("PYTHONPATH").unwrap_or_default()));
        command.env("PYTHONPATH", std::env::join_paths(paths).map_err(|_| invalid("invalid PYTHONPATH"))?);
    }
    eprintln!("fleet start: running the Python fleet harness for capacity, budget, barrier and teardown controls");
    let status = command.status()?;
    if !status.success() {
        std::process::exit(status.code().unwrap_or(1));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::{symlink, PermissionsExt};

    #[test]
    fn update_runs_embedded_installer_for_verified_bin_directory() {
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join("bin");
        fs::create_dir(&bin).unwrap();
        for name in ["doxa-rs", "doxa", "doxa-daemon-rs"] {
            fs::write(bin.join(name), "fixture").unwrap();
        }
        let executable = bin.join("doxa-rs");
        assert!(installed_bin_dir_for(&executable).is_err());
        symlink("unused-sidecar", bin.join(".doxa-sidecar-current")).unwrap();
        assert_eq!(installed_bin_dir_for(&executable).unwrap(), bin);

        let shell = dir.path().join("fake-sh");
        fs::write(&shell, "#!/bin/sh\n[ \"$1\" = -s ] && [ \"$2\" = -- ] && [ \"$3\" = main ] || exit 21\nprintf '%s' \"$DOXA_RUST_BIN_DIR\" > \"$(dirname \"$0\")/bin-dir\"\ncat > \"$(dirname \"$0\")/installer\"\n").unwrap();
        fs::set_permissions(&shell, fs::Permissions::from_mode(0o700)).unwrap();
        run_update_installer(&bin, &shell).unwrap();
        assert_eq!(fs::read_to_string(dir.path().join("bin-dir")).unwrap(), bin.to_string_lossy());
        let script = fs::read_to_string(dir.path().join("installer")).unwrap();
        assert!(script.starts_with("#!/bin/sh\n"));
        assert!(script.contains("main \"$@\""));
    }
}
