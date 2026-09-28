//! Native launcher for DOXA's private fail-closed app server. Other commands
//! retain the official CLI. The provider itself attests its compiled contract.
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{env, ffi::CString, fs::{File, OpenOptions}, io::{self, Read}, os::unix::{fs::{MetadataExt, OpenOptionsExt}, io::AsRawFd, process::CommandExt}, path::{Path, PathBuf}, process::Command};

const SOURCE: &str = "b412ff32c417f855c2b2d1581b77058eed87c84b";
const CONTRACT: &str = "doxa-precompact-fail-closed-v1";
const PATCH: &str = "96a3c37b55f2bade5dc72f3ab344d30ed25c39c8fb42b42cfe5774fb7b210e6b";
const MAX_BINARY: u64 = 1024 * 1024 * 1024;

#[derive(Deserialize)]
struct Receipt { contract: String, source_commit: String, patch_sha256: String, binary_sha256: String, official_cli: PathBuf }
fn invalid(message: &str) -> io::Error { io::Error::other(message) }
fn safe_file(path: &Path, maximum: u64, executable: bool) -> io::Result<File> {
    let file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.uid() != unsafe { libc::geteuid() } || metadata.mode() & 0o077 != 0
        || metadata.nlink() != 1 || metadata.len() > maximum || (executable && metadata.mode() & 0o100 == 0) {
        return Err(invalid("provider artifact must be a bounded private owned regular file"));
    }
    Ok(file)
}
fn receipt(path: &Path) -> io::Result<Receipt> {
    let mut data = Vec::new();
    safe_file(path, 16 * 1024, false)?.take(16 * 1024 + 1).read_to_end(&mut data)?;
    if data.len() > 16 * 1024 { return Err(invalid("provider receipt is oversized")); }
    let receipt: Receipt = serde_json::from_slice(&data).map_err(|_| invalid("invalid provider receipt"))?;
    if receipt.contract != CONTRACT || receipt.source_commit != SOURCE || receipt.patch_sha256 != PATCH {
        return Err(invalid("provider receipt does not match the reviewed compaction contract"));
    }
    Ok(receipt)
}
fn run() -> io::Result<()> {
    let launcher = env::current_exe()?;
    let root = launcher.parent().ok_or_else(|| invalid("provider launcher has no directory"))?;
    let directory = std::fs::symlink_metadata(root)?;
    if !directory.is_dir() || directory.file_type().is_symlink() || directory.uid() != unsafe { libc::geteuid() } || directory.mode() & 0o077 != 0 {
        return Err(invalid("provider installation must be private and owned"));
    }
    let receipt = receipt(&root.join("receipt.json"))?;
    let mut args = env::args_os().skip(1).collect::<Vec<_>>();
    if args.first().is_none_or(|arg| arg != "app-server") {
        if !receipt.official_cli.is_absolute() || std::fs::canonicalize(&receipt.official_cli)? == launcher {
            return Err(invalid("official Codex CLI is unavailable"));
        }
        return Err(Command::new(receipt.official_cli).args(args).exec());
    }
    let path = root.join("codex-app-server");
    let mut binary = safe_file(&path, MAX_BINARY, true)?;
    let before = binary.metadata()?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 65536];
    let mut total = 0_u64;
    loop {
        let count = binary.read(&mut buffer)?;
        if count == 0 { break; }
        total += count as u64;
        if total > MAX_BINARY { return Err(invalid("provider binary grew beyond its bound")); }
        hasher.update(&buffer[..count]);
    }
    let after = binary.metadata()?;
    if format!("{:x}", hasher.finalize()) != receipt.binary_sha256 ||
        (before.dev(), before.ino(), before.len(), before.mtime(), before.mtime_nsec()) !=
        (after.dev(), after.ino(), after.len(), after.mtime(), after.mtime_nsec()) {
        return Err(invalid("protected provider binary differs from its build receipt"));
    }
    args.remove(0);
    if args.first().is_some_and(|arg| arg == "--stdio") { args.remove(0); }
    let mut command = Command::new(&path);
    command.args(["--listen", "stdio://"]).args(args);
    // fexecve executes the pinned inode, including when its path is replaced.
    let command_args = std::iter::once(command.get_program()).chain(command.get_args())
        .map(|value| { use std::os::unix::ffi::OsStrExt; CString::new(value.as_bytes()).map_err(|_| invalid("invalid provider argument")) })
        .collect::<io::Result<Vec<_>>>()?;
    let environment = env::vars_os().map(|(key,value)| { use std::os::unix::ffi::OsStrExt;
        let mut bytes=key.as_bytes().to_vec(); bytes.push(b'='); bytes.extend_from_slice(value.as_bytes());
        CString::new(bytes).map_err(|_| invalid("invalid provider environment"))
    }).collect::<io::Result<Vec<_>>>()?;
    let args = command_args.iter().map(|value| value.as_ptr()).chain(std::iter::once(std::ptr::null())).collect::<Vec<_>>();
    let environment = environment.iter().map(|value| value.as_ptr()).chain(std::iter::once(std::ptr::null())).collect::<Vec<_>>();
    unsafe { libc::fexecve(binary.as_raw_fd(), args.as_ptr(), environment.as_ptr()); }
    Err(io::Error::last_os_error())
}
fn main() { if let Err(error)=run() { eprintln!("doxa-codex: {error}"); std::process::exit(1); } }
