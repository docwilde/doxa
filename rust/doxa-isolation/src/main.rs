//! Minimal image-owned worker. No Docker control or DOXA state APIs.
use std::{io::{self, Read, Write}, os::{fd::AsRawFd, unix::{net::UnixStream, process::CommandExt}},
    process::{Command, Stdio}, sync::{Arc, atomic::{AtomicBool, Ordering}}, time::Duration};
fn run() -> io::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("--version") if args.len()==1=>{println!("doxa-isolation-worker {}",env!("CARGO_PKG_VERSION"));Ok(())},
        Some("--self-test") if args.len()==1=>{
            let value=doxa_isolation::Profile::parse("docker-offline")?;
            if value.key()!="docker-offline"{return Err(io::Error::other("profile self-test failed"));}
            println!("doxa-isolation-worker self-test ok");Ok(())
        },
        Some("hold") if args.len() == 1 => loop { std::thread::park(); },
        Some("egress-proxy") if args.len() == 2 => {
            let port = args[1].parse().map_err(|_| io::Error::other("invalid fixture proxy port"))?;
            doxa_isolation::egress::serve_loopback_adapter(port)
        },
        Some("probe") if args.len() == 4 => {
            let memory = args[1].parse().map_err(|_| io::Error::other("invalid expected memory ceiling"))?;
            let cpus = args[2].parse().map_err(|_| io::Error::other("invalid expected CPU ceiling"))?;
            let pids = args[3].parse().map_err(|_| io::Error::other("invalid expected PID ceiling"))?;
            doxa_isolation::cgroup::verify_limits(memory, cpus, pids)?;
            for directory in ["/workspace", "/home/doxa", "/work-cache"] {
                let path = std::path::Path::new(directory).join(format!(".doxa-write-probe-{}", std::process::id()));
                let mut file = std::fs::OpenOptions::new().write(true).create_new(true).open(&path)?;
                file.write_all(b"rootless write mapping")?; file.sync_all()?; std::fs::remove_file(path)?;
            }
            if !std::fs::metadata("/run/doxa/session")?.is_dir(){return Err(io::Error::other("session broker mount missing"));}
            if std::fs::OpenOptions::new().write(true).create_new(true).open("/run/doxa/session/.worker-write-probe").is_ok(){
                let _=std::fs::remove_file("/run/doxa/session/.worker-write-probe");
                return Err(io::Error::other("session broker directory must be read-only to worker"));
            }
            for path in ["/var/run/docker.sock", "/run/docker.sock", "/home/docwilde", "/root/.ssh", "/root/.doxa"] {
                if std::path::Path::new(path).exists() { return Err(io::Error::other("worker exposes a forbidden host path")); }
            }
            Ok(())
        },
        Some("exec") if args.len() >= 2 => {
            // A helper lives for exactly one provider transport. Losing the
            // host stdio channel kills the provider process group, so resume
            // never adopts an orphan CLI writer after a supervisor crash.
            if !matches!(args[1].as_str(), "/usr/local/bin/claude" | "/usr/local/bin/codex") { return Err(io::Error::other("unsupported worker provider")); }
            let (mut owner, peer) = if args[1] == "/usr/local/bin/codex" {
                let (parent, child) = UnixStream::pair()?; (Some(parent), Some(child))
            } else { (None, None) };
            let fd = peer.as_ref().map(AsRawFd::as_raw_fd);
            let mut command = Command::new(&args[1]);
            command.args(&args[2..]).process_group(0).stdin(Stdio::piped()).stdout(Stdio::inherit()).stderr(Stdio::inherit());
            if let Some(fd) = fd {
                command.env("DOXA_CODEX_OWNER_FD", fd.to_string());
                unsafe { command.pre_exec(move || { if libc::fcntl(fd,libc::F_SETFD,0) < 0 { return Err(io::Error::last_os_error()); } Ok(()) }); }
            }
            let mut child = command.spawn()?; drop(peer);
            if let Some(owner) = owner.as_mut() {
                owner.set_read_timeout(Some(Duration::from_secs(30)))?;
                let mut ready = [0;23]; owner.read_exact(&mut ready)?;
                if &ready != b"DOXA_PROVIDER_OWNER_V1\n" { return Err(io::Error::other("invalid protected provider owner")); }
                owner.write_all(b"G")?;
            }
            let pid = child.id() as i32; let mut input = child.stdin.take().unwrap();
            let ended = Arc::new(AtomicBool::new(false)); let eof = ended.clone();
            std::thread::spawn(move || {
                let _ = io::copy(&mut io::stdin().lock(), &mut input);
                drop(input); eof.store(true, Ordering::Release);
            });
            loop {
                if let Some(status) = child.try_wait()? { if status.success() { return Ok(()); } else { return Err(io::Error::other("worker provider exited unsuccessfully")); } }
                if ended.load(Ordering::Acquire) {
                    drop(owner);
                    unsafe { libc::kill(-pid, libc::SIGKILL); }
                    let _ = child.wait(); return Ok(());
                }
                std::thread::sleep(Duration::from_millis(10));
            }
        },
        Some("hook") if args.len() == 3 => {
            if args[1] != "/run/doxa/session/hook.sock" || args[2].len() != 48 || !args[2].bytes().all(|b| b.is_ascii_hexdigit()) { return Err(io::Error::other("invalid session hook capability")); }
            let mut input = Vec::new(); io::stdin().take(65537).read_to_end(&mut input)?;
            if input.len() > 65536 { return Err(io::Error::other("hook input limit exceeded")); }
            let event: serde_json::Value = serde_json::from_slice(&input)?;
            let mut stream = UnixStream::connect(&args[1])?;
            stream.set_read_timeout(Some(Duration::from_secs(215)))?; stream.set_write_timeout(Some(Duration::from_secs(5)))?;
            let bytes = serde_json::to_vec(&serde_json::json!({"version":1,"capability":args[2],"event":event}))?;
            stream.write_all(&(bytes.len() as u32).to_be_bytes())?; stream.write_all(&bytes)?;
            let mut size = [0; 4]; stream.read_exact(&mut size)?;
            let size = u32::from_be_bytes(size) as usize;
            if size > 65536 { return Err(io::Error::other("hook response limit exceeded")); }
            let mut bytes = vec![0;size]; stream.read_exact(&mut bytes)?;
            let result: serde_json::Value = serde_json::from_slice(&bytes)?; println!("{result}"); Ok(())
        },
        _ => Err(io::Error::other("usage: doxa-isolation-worker hold|probe MEMORY_BYTES CPUS PIDS|exec PROVIDER [ARGS]|hook SOCKET CAPABILITY|egress-proxy PORT")),
    }
}
fn main() {
    if let Err(error) = run() {
        eprintln!("doxa isolation worker: {error}");
        // Hook errors produce an explicit block even if the provider ignores
        // nonzero command status.
        if std::env::args().nth(1).as_deref() == Some("hook") {
            println!("{}", serde_json::json!({"continue":false,"suppressOutput":true,"stopReason":"DOXA host hook broker unavailable; compaction blocked"}));
        } else { std::process::exit(1); }
    }
}
