use doxa_tui::{bridge, discovery, fleet_control, fleet_plan, fleet_view, launch, mesh_control, operations, remote_client, settings, startup_restore, ui_state};
#[cfg(test)]
use doxa_tui::maintenance;
use std::collections::HashSet;
use std::io::{self, IsTerminal, Write};
use serde_json::{Map, Value};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::process::Stdio;

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

fn startup_message(message: &str) {
    if io::stderr().is_terminal() {
        let mut out = io::stderr().lock();
        let _ = writeln!(out, "{message}…");
        let _ = out.flush();
    }
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

const HELP: &str = r#"DOXA Rust 2.0 beta

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
  setup                Interactive authentication, LORE store, and defaults wizard
  settings             Show native settings and their effective sources
  settings set KEY VALUE | unset KEY
                       Persist supported settings or restore their defaults
  auth status [NAME]   Check Claude or Codex CLI authentication without showing CLI output
  auth login NAME [--device-auth (Codex only)] | auth logout NAME
                       Run the explicitly selected provider authentication
  plugins [refresh | adopt on|off]
                       Discover plugins or change sanitized adoption for new sessions
  fleet ...            Inspect or start native fleet runs
  mesh serve           Serve the private peer graph until Ctrl-C
  remote serve         Serve live sessions to an allowed Tailscale browser
  remote connect URL HOST_ID
                       Register this machine's sessions with a private hub
  remote list URL        List sessions registered with a private hub
  remote tui URL         Open live hub sessions as native DOXA tabs
  remote keygen ABS_PATH Create an owner-only shared key for encrypted remote tabs
  remote send URL SESSION TEXT
                       Send a prompt through the private hub
  remote answer URL SESSION REQUEST_ID allow|deny
                       Resolve one pending approval through the private hub

Run doxa without a command to restore this project's saved tabs or start
a session with the configured engine (Claude by default). Pass --engine or --model to start a new session.
Ctrl+T opens a new tab; Alt+T opens tool calls. Ctrl+X closes the active tab
and hides it from the rail without stopping its daemon; /resume reopens it. Ctrl+Q exits DOXA
and leaves running sessions detached. Ctrl+Left/Right switches tabs in the
current pane; Shift+Left/Right switches between pane prompts. Change window
shortcuts in /settings → Keys or with `doxa settings set key_new_tab Alt+N`.
Inside the TUI, /remote-connect URL HOST_ID shares local sessions while this
window is open; /remote-disconnect stops sharing. /remote-control URL adds
remote tabs beside local tabs; /local selects an open local tab. Ctrl+R opens
remote history, and PageUp at its top fetches older turns.

New-session options: --engine codex|claude|deepseek|glm, --model NAME,
  --isolation native|docker-open|docker-offline,
  --branch LOCAL_OR_REMOTE, --linger SECONDS, --resume FULL_SESSION_ID.
Codex: --sandbox read-only|workspace-write|danger-full-access, --codex-bin PATH.
Claude: --claude-bin PATH, --effort low|medium|high|xhigh|max.
Codex: --effort uses account model capabilities.
DeepSeek/GLM: --effort low|high|max (DeepSeek also none).
LORE memory, provider review and peer services run in Rust.
API keys come from provider environment variables.

Fleet: doxa fleet preflight --sessions N --run-budget USD [--supervisor ENGINE[:MODEL]] [--approve none|peer|all] [--approval-grace SECONDS] [--root PATH]
       doxa fleet start --pool ENGINE:MODEL --prompt TEXT -n N --run-budget USD
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
        || doxa_tui::installation::installed_commit(executable)?.is_none()
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
    if args.first().is_some_and(|arg|arg=="install-launcher") {
        if args.len()!=2 {return Err(invalid("usage: doxa install-launcher ABSOLUTE_LAUNCHER_PATH"));}
        let path=doxa_tui::installation::install_launcher(Path::new(&args[1]))?;
        println!("Desktop shortcut · {}",path.display()); return Ok(());
    }
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
                operations::setup_interactive()?;
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
                match args {
                    [_, action] if action == "status" => println!("{}", operations::auth_status(None)?),
                    [_, action, name] if action == "status" => println!("{}", operations::auth_status(Some(name))?),
                    [_, action, name] if action == "login" || action == "logout" => println!("{}", operations::auth_action(name, action, |message| println!("{message}"))?),
                    [_, action, name, option] if action == "login" && option == "--device-auth" => {
                        let request = operations::parse_auth_request(action, &format!("{name} {option}"))?.ok_or_else(|| invalid("choose a provider explicitly"))?;
                        println!("{}", operations::auth_action_request(request, |message| println!("{message}"), &std::sync::atomic::AtomicBool::new(false))?);
                    },
                    _ => return Err(invalid("usage: doxa auth status [claude|codex] | auth login claude|codex [--device-auth (Codex only)] | auth logout claude|codex")),
                }
                return Ok(());
            }
            "plugins" => {
                match args {
                    [_] => println!("{}", operations::plugins_report()?),
                    [_, action] if action == "refresh" => println!("{}", operations::plugins_reload()?),
                    [_, action, value] if action == "adopt" && (value == "on" || value == "off") => println!("{}", operations::plugins_change(value == "on")?),
                    _ => return Err(invalid("usage: doxa plugins [refresh | adopt on|off]")),
                }
                return Ok(());
            }
            "remote" => {
                if let [first, second, path] = args {
                    if first == "remote" && second == "keygen" {
                        doxa_remote_wire::create_key(std::path::Path::new(path))?;
                        println!("Remote key created at {path}. Copy it securely to the session host and set DOXA_REMOTE_E2EE_KEY_FILE on both machines.");
                        return Ok(());
                    }
                }
                if let [first, second, url] = args {
                    if first == "remote" && second == "tui" {
                        startup_message("Connecting to DOXA hub");
                        return remote_client::run(url);
                    }
                }
                if !matches!(args, [first, second] if first == "remote" && second == "serve")
                    && !matches!(args, [first, second, _, _] if first == "remote" && second == "connect")
                    && !matches!(args, [first, second, _, _, _] if first == "remote" && second == "send")
                    && !matches!(args, [first, second, _] if first == "remote" && second == "list")
                    && !matches!(args, [first, second, _, _, _, _] if first == "remote" && second == "answer") {
                    return Err(invalid("usage: doxa remote serve | connect URL HOST_ID | tui URL | keygen ABS_PATH | list URL | send URL SESSION TEXT | answer URL SESSION REQUEST_ID allow|deny"));
                }
                let executable = std::env::var_os("DOXA_REMOTE_BIN").map(PathBuf::from)
                    .unwrap_or_else(|| std::env::current_exe().unwrap_or_default().with_file_name("doxa-remote"));
                let status = Command::new(executable).args(&args[1..]).status()?;
                if !status.success() { return Err(io::Error::other(format!("remote adapter exited with {status}"))); }
                return Ok(());
            }
            _ => {}
        }
    }
    if args.first().is_some_and(|arg| arg == "mesh") {
        let ledger = match &args[1..] {
            [] => mesh_control::default_ledger()?,
            [mode] if mode == "serve" => mesh_control::default_ledger()?,
            [mode, flag, path] if mode == "serve" && flag == "--ledger" => PathBuf::from(path),
            _ => return Err(invalid("usage: doxa mesh serve [--ledger ABSOLUTE_PATH] | doxa fleet mesh RUN_ID [--root ROOT]")),
        };
        return mesh_control::serve(&ledger);
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
    options.engine = launch::configured_engine();
    let mut explicit_launch = false;
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
            | "--sandbox" | "--codex-bin" | "--claude-bin"
             | "--resume" | "--branch" | "--isolation" => {
                index += 1;
                let value = args
                    .get(index)
                    .ok_or_else(|| invalid(format!("missing value for {arg}")))?;
                match arg {
                    "--isolation" => {
                        explicit_launch = true;
                        options.isolation = Some(doxa_isolation::Profile::parse(value)?);
                    }
                    "--session" => {
                        if prefix.is_some() {
                            return Err(invalid("session ID specified twice"));
                        }
                        prefix = Some(value);
                    }
                    "--socket" => socket = Some(value),
                    "--engine" => {
                        explicit_launch = true;
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
                    "--model" => {
                        explicit_launch = true;
                        options.model = Some(value.clone());
                    }
                    "--effort" => options.effort = Some(value.clone()),
                    "--linger" => {
                        options.linger = Some(value.parse().map_err(|_| invalid("invalid linger"))?)
                    }
                    "--sandbox" => options.sandbox = Some(value.clone()),
                    "--codex-bin" => options.codex_bin = Some(PathBuf::from(value)),
                    "--claude-bin" => options.claude_bin = Some(PathBuf::from(value)),
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
        startup_message("Loading session");
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
                }
                launch::Engine::Claude => {checks.push(("claude",launch::claude_executable(&options)));}
                launch::Engine::Fixture => {}
                launch::Engine::DeepSeek | launch::Engine::Glm => {
                    if let Err(error) = launch::vendor_effort(&options) {
                        println!("missing vendor effort: {error}");
                        missing = true;
                    }
                    let key = options.engine.vendor_key().expect("vendor engine");
                    use doxa_vendors::credentials::CredentialStatus;
                    match options.engine.vendor_credential_status() {
                        Ok(CredentialStatus::Saved) => println!("ok {key}: set (saved)"),
                        Ok(CredentialStatus::Environment) => println!("ok {key}: set (environment)"),
                        Ok(CredentialStatus::Missing) => { println!("missing {key}: unset"); missing = true; }
                        Err(_) => { println!("missing {key}: credential store unavailable"); missing = true; }
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
            startup_message("Restoring session");
            let sessions = discovery::sessions()?;
            let session = discovery::select(&sessions, prefix)?;
            bridge::run_socket(&session.socket)
        }
        Some("new") => {
            if prefix.is_some() {
                return Err(invalid("new does not accept a session ID"));
            }
            startup_message("Loading session");
            let session = launch::spawn(&options)?;
            eprintln!("started native session {}", session.id);
            bridge::run_socket(&session.socket)
        }
        None if prefix.is_some() => {
            startup_message("Restoring session");
            let sessions = discovery::sessions()?;
            let session = discovery::select(&sessions, prefix)?;
            bridge::run_socket(&session.socket)
        }
        None if explicit_launch => {
            startup_message("Loading session");
            let session = launch::spawn(&options)?;
            eprintln!("started native session {}", session.id);
            bridge::run_socket(&session.socket)
        }
        None => {
            startup_message("Restoring sessions");
            let scope = discovery::current_scope()?;
            let sessions: Vec<_> = discovery::sessions()?
                .into_iter()
                .filter(|s| s.scope_key == scope)
                .collect();
            let home = std::env::var_os("DOXA_HOME").filter(|s| !s.is_empty()).map(PathBuf::from)
                .or_else(|| std::env::var_os("HOME").map(|s| PathBuf::from(s).join(".doxa")));
            let store = Some(home.and_then(|home| ui_state::UiStateStore::for_scope(&home, &scope).ok())
                .unwrap_or_else(||ui_state::UiStateStore::transient(&scope)));
            let (sessions, store) = startup_restore::prepare(store, sessions, &options,
                settings::enabled("restore_tabs"), settings::enabled("resume_restored"))?;
            bridge::run_sessions(&sessions, store)
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
    if args.iter().any(|arg|matches!(arg.as_str(),"--help"|"-h")) {print!("{}",fleet_control::HELP);return Ok(());}
    if args.first().is_some_and(|arg| arg == "start") {
        return fleet_control::start(&args[1..]);
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
        ["mesh", run] => return mesh_control::serve(&mesh_control::run_ledger(&root, run)?),
        ["runs"] => println!("{}", fleet_view::runs(&root)?),
        ["status", run] => println!("{}", fleet_view::status(&root, run)?),
        ["resume", run] => return fleet_control::resume(&root, run),
        ["continue", run, charter_hash] => println!("{}", fleet_control::continue_run(&root,run,charter_hash)?),
        ["review", run, slot, request] => {
            let slot = slot.parse().map_err(|_| invalid("fleet slot must be a number"))?;
            let reviewed = fleet_control::review(&root, run, slot, request)?;
            println!("{}\nReview token: {}", serde_json::to_string_pretty(&reviewed.request)?, reviewed.token);
        },
        ["answer", run, slot, request, token, answer] => {
            let slot = slot.parse().map_err(|_| invalid("fleet slot must be a number"))?;
            let answer: serde_json::Value = serde_json::from_str(answer).map_err(|_| invalid("fleet answer must be a JSON object"))?;
            println!("{}", fleet_control::answer(&root, run, slot, request, token, answer)?);
        },
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
        _ => return Err(invalid("usage: doxa fleet start --pool ENGINE:MODEL --prompt TEXT -n N --run-budget USD|preflight --sessions N --run-budget USD [--supervisor ENGINE[:MODEL]] [--approve none|peer|all] [--approval-grace SECONDS] [--root ABSOLUTE_PATH]|runs|status RUN_ID|stop RUN_ID|attach RUN_ID SLOT [--root ABSOLUTE_PATH]")),
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;

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
        fs::write(bin.join(".doxa-install-sha"),"0123456789abcdef0123456789abcdef01234567").unwrap();
        fs::set_permissions(bin.join(".doxa-install-sha"),fs::Permissions::from_mode(0o600)).unwrap();
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

#[cfg(test)]
#[path = "ui/credential_editor.rs"]
mod credential_editor;

#[cfg(test)]
#[path = "ui/operations_menu.rs"]
mod operations_menu_test;
