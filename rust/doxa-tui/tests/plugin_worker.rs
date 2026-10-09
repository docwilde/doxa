use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const WORKER: &str = env!("CARGO_BIN_EXE_doxa-plugin-worker");

fn frame(module: &[u8]) -> Vec<u8> {
    let mut bytes = Vec::with_capacity(44 + module.len());
    bytes.extend_from_slice(b"DOXAW1\0\0");
    bytes.extend_from_slice(&(module.len() as u32).to_le_bytes());
    bytes.extend_from_slice(&Sha256::digest(module));
    bytes.extend_from_slice(module);
    bytes
}

fn run_command(mut command: Command, input: &[u8]) -> (i32, Vec<u8>, Vec<u8>) {
    let mut child = command
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn().unwrap();
    child.stdin.take().unwrap().write_all(input).unwrap_or(());
    let deadline = Instant::now() + Duration::from_secs(3);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() { break status; }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("plugin worker exceeded 3-second fixture deadline");
        }
        thread::sleep(Duration::from_millis(5));
    };
    let mut output = Vec::new();
    child.stdout.take().unwrap().read_to_end(&mut output).unwrap();
    let mut diagnostic = Vec::new();
    child.stderr.take().unwrap().read_to_end(&mut diagnostic).unwrap();
    (status.code().unwrap_or(-1), output, diagnostic)
}

fn run(input: &[u8]) -> (i32, Vec<u8>, Vec<u8>) {
    run_command(Command::new(WORKER), input)
}

fn response(input: &[u8]) -> (u8, i32) {
    let (exit, bytes, diagnostic) = run(input);
    assert_eq!(exit, 0);
    assert_eq!(diagnostic, b"DOXA-WORKER-READY-v1\n");
    assert_eq!(bytes.len(), 13);
    assert_eq!(&bytes[..8], b"DOXAR1\0\0");
    (bytes[8], i32::from_le_bytes(bytes[9..13].try_into().unwrap()))
}

#[test]
fn child_independently_decodes_and_returns_value() {
    let module = wat::parse_str("(module (func (export \"doxa_main\") (result i32) i32.const 42))").unwrap();
    assert_eq!(response(&frame(&module)), (0, 42));
}

#[test]
fn child_bounds_and_authenticates_its_own_request() {
    let module = wat::parse_str("(module (func (export \"doxa_main\") (result i32) i32.const 1))").unwrap();
    let valid = frame(&module);
    for bad in [
        Vec::new(),
        valid[..43].to_vec(),
        valid[..valid.len() - 1].to_vec(),
        { let mut bytes = valid.clone(); bytes[0] = b'X'; bytes },
        { let mut bytes = valid.clone(); bytes[8..12].copy_from_slice(&(8 * 1024 * 1024u32 + 1).to_le_bytes()); bytes },
        { let mut bytes = valid.clone(); bytes[12] ^= 1; bytes },
        { let mut bytes = valid.clone(); bytes.push(0); bytes },
    ] {
        assert_eq!(response(&bad), (1, 0));
    }
}

#[test]
fn child_reports_fuel_exhaustion_and_trap_without_text_output() {
    let loop_module = wat::parse_str("(module (func (export \"doxa_main\") (result i32) (loop br 0) i32.const 0))").unwrap();
    assert_eq!(response(&frame(&loop_module)), (3, 0));
    let trap_module = wat::parse_str("(module (func (export \"doxa_main\") (result i32) unreachable))").unwrap();
    assert_eq!(response(&frame(&trap_module)), (4, 0));
}

#[cfg(target_os = "linux")]
#[test]
fn descriptor_mounted_elf_worker_runs_in_private_namespaces() {
    use std::fs::File;
    use std::os::fd::AsRawFd;
    use std::os::unix::process::CommandExt;
    let supports_fd_mount = Command::new("/usr/bin/bwrap").arg("--help").output()
        .is_ok_and(|output| String::from_utf8_lossy(&output.stdout).contains("--ro-bind-fd"));
    if !supports_fd_mount { return; }
    let probe = Command::new("/usr/bin/bwrap")
        .args(["--unshare-all", "--unshare-user", "--clearenv", "--ro-bind", "/usr", "/usr",
            "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64", "--", "/bin/true"])
        .stdout(Stdio::null()).stderr(Stdio::null()).status();
    if !probe.is_ok_and(|status| status.success()) { return; }
    let worker = File::open(WORKER).unwrap();
    let fd = worker.as_raw_fd();
    let mut command = Command::new("/usr/bin/bwrap");
    command.args(["--unshare-all", "--unshare-user", "--die-with-parent", "--disable-userns",
        "--cap-drop", "ALL", "--clearenv", "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--ro-bind-fd", &fd.to_string(), "/worker",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--", "/worker"]);
    // SAFETY: only the verified worker descriptor is inherited by Bubblewrap.
    unsafe { command.pre_exec(move || {
        if libc::fcntl(fd, libc::F_SETFD, 0) < 0 { return Err(std::io::Error::last_os_error()); }
        Ok(())
    }); }
    let module = wat::parse_str("(module (func (export \"doxa_main\") (result i32) i32.const 7))").unwrap();
    let (exit, bytes, diagnostic) = run_command(command, &frame(&module));
    assert_eq!(exit, 0);
    assert_eq!(diagnostic, b"DOXA-WORKER-READY-v1\n");
    assert_eq!(&bytes[..8], b"DOXAR1\0\0");
    assert_eq!(i32::from_le_bytes(bytes[9..13].try_into().unwrap()), 7);
}
