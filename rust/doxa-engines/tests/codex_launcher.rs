#![cfg(unix)]
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{fs, os::unix::fs::PermissionsExt, process::Command};
const PATCH: &str = "d6c8a41c0370c12dcace10d6babe13de7852f0095fed7b46289b38e7a6cd0f4b";
fn fixture() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    fs::copy(
        env!("CARGO_BIN_EXE_doxa-codex-protected"),
        dir.path().join("codex"),
    )
    .unwrap();
    fs::copy("/usr/bin/true", dir.path().join("codex-app-server")).unwrap();
    fs::copy(
        env!("CARGO_BIN_EXE_doxa-codex-protected"),
        dir.path().join("codex-code-mode-host"),
    )
    .unwrap();
    fs::copy(
        "/usr/bin/true",
        dir.path().join("codex-code-mode-host-payload"),
    )
    .unwrap();
    fs::set_permissions(
        dir.path().join("codex-code-mode-host-payload"),
        fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    fs::set_permissions(
        dir.path().join("codex-code-mode-host"),
        fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    fs::set_permissions(
        dir.path().join("codex-app-server"),
        fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    fs::write(dir.path().join("receipt.json"),json!({"contract":"doxa-precompact-fail-closed-v1",
        "source_commit":"b412ff32c417f855c2b2d1581b77058eed87c84b","patch_sha256":PATCH,
        "code_mode_host_source_commit":"b412ff32c417f855c2b2d1581b77058eed87c84b",
        "code_mode_host_sha256":format!("{:x}",Sha256::digest(fs::read("/usr/bin/true").unwrap())),
        "code_mode_host_dispatcher_sha256":format!("{:x}",Sha256::digest(fs::read(env!("CARGO_BIN_EXE_doxa-codex-protected")).unwrap())),
        "binary_sha256":format!("{:x}",Sha256::digest(fs::read("/usr/bin/true").unwrap())),"official_cli":"/usr/bin/printf"}).to_string()).unwrap();
    fs::set_permissions(
        dir.path().join("receipt.json"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    dir
}
#[test]
fn helper_missing_changed_or_unbound_refuses_before_provider_dispatch() {
    for change in [
        "missing", "hash", "source", "receipt", "fifo", "symlink", "hardlink", "mode",
    ] {
        let dir = fixture();
        let host = dir.path().join("codex-code-mode-host-payload");
        let receipt = dir.path().join("receipt.json");
        match change {
            "missing" => fs::remove_file(&host).unwrap(),
            "hash" => fs::write(&host, b"changed helper").unwrap(),
            "mode" => fs::set_permissions(&host, fs::Permissions::from_mode(0o755)).unwrap(),
            "hardlink" => fs::hard_link(&host, dir.path().join("host-alias")).unwrap(),
            "source" | "receipt" => {
                let mut value: serde_json::Value =
                    serde_json::from_slice(&fs::read(&receipt).unwrap()).unwrap();
                if change == "source" {
                    value["code_mode_host_source_commit"] = json!("wrong");
                } else {
                    value
                        .as_object_mut()
                        .unwrap()
                        .remove("code_mode_host_sha256");
                    value
                        .as_object_mut()
                        .unwrap()
                        .remove("code_mode_host_source_commit");
                }
                fs::write(&receipt, value.to_string()).unwrap();
            }
            "symlink" => {
                fs::rename(&host, dir.path().join("original-host")).unwrap();
                std::os::unix::fs::symlink(dir.path().join("original-host"), &host).unwrap();
            }
            "fifo" => {
                fs::remove_file(&host).unwrap();
                use std::os::unix::ffi::OsStrExt;
                let name = std::ffi::CString::new(host.as_os_str().as_bytes()).unwrap();
                assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o700) }, 0);
            }
            _ => unreachable!(),
        }
        let started = std::time::Instant::now();
        assert!(
            !Command::new(dir.path().join("codex"))
                .arg("app-server")
                .output()
                .unwrap()
                .status
                .success(),
            "{change}"
        );
        assert!(
            !Command::new(dir.path().join("codex-code-mode-host"))
                .output()
                .unwrap()
                .status
                .success(),
            "lazy helper {change}"
        );
        assert!(
            started.elapsed() < std::time::Duration::from_secs(1),
            "{change}"
        );
        // Official auth/help dispatch remains available even when the helper is missing.
        assert!(Command::new(dir.path().join("codex"))
            .args(["%s", "official"])
            .output()
            .unwrap()
            .status
            .success());
    }
}
#[test]
fn native_launcher_executes_checked_binary_and_keeps_official_cli_commands() {
    let dir = fixture();
    assert!(Command::new(dir.path().join("codex"))
        .args(["app-server", "--stdio"])
        .status()
        .unwrap()
        .success());
    assert!(Command::new(dir.path().join("codex-code-mode-host"))
        .status()
        .unwrap()
        .success());
    let result = Command::new(dir.path().join("codex"))
        .args(["version=%s", "official"])
        .output()
        .unwrap();
    assert!(result.status.success());
    assert_eq!(result.stdout, b"version=official");
}
#[test]
fn invalid_receipts_and_changed_provider_artifacts_refuse_without_dispatch() {
    for change in [
        "receipt-fifo",
        "binary-fifo",
        "receipt-symlink",
        "oversized",
        "hash",
        "mode",
        "contract",
        "hardlink",
    ] {
        let dir = fixture();
        let receipt = dir.path().join("receipt.json");
        let binary = dir.path().join("codex-app-server");
        match change {
            "receipt-fifo" | "binary-fifo" => {
                let path = if change == "receipt-fifo" {
                    &receipt
                } else {
                    &binary
                };
                fs::remove_file(path).unwrap();
                use std::os::unix::ffi::OsStrExt;
                let path = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
                assert_eq!(unsafe { libc::mkfifo(path.as_ptr(), 0o700) }, 0);
            }
            "receipt-symlink" => {
                fs::rename(&receipt, dir.path().join("original")).unwrap();
                std::os::unix::fs::symlink(dir.path().join("original"), &receipt).unwrap();
            }
            "oversized" => fs::write(&receipt, vec![b' '; 16385]).unwrap(),
            "hash" => fs::write(&binary, b"changed provider").unwrap(),
            "mode" => fs::set_permissions(&binary, fs::Permissions::from_mode(0o755)).unwrap(),
            "contract" => {
                let mut value: serde_json::Value =
                    serde_json::from_slice(&fs::read(&receipt).unwrap()).unwrap();
                value["patch_sha256"] = json!("wrong");
                fs::write(&receipt, value.to_string()).unwrap();
            }
            "hardlink" => fs::hard_link(&binary, dir.path().join("binary-alias")).unwrap(),
            _ => unreachable!(),
        }
        let started = std::time::Instant::now();
        let result = Command::new(dir.path().join("codex"))
            .args(["app-server", "--stdio"])
            .output()
            .unwrap();
        assert!(!result.status.success(), "{change}");
        assert!(
            started.elapsed() < std::time::Duration::from_secs(1),
            "{change}"
        );
    }
}

#[cfg(target_os = "linux")]
#[test]
fn verified_launcher_owns_provider_after_control_handshake() {
    use std::io::{Read, Write};
    use std::os::unix::{io::AsRawFd, net::UnixStream, process::CommandExt};
    let dir = fixture();
    let (mut parent, peer) = UnixStream::pair().unwrap();
    let fd = peer.as_raw_fd();
    let mut command = Command::new(dir.path().join("codex"));
    command
        .args(["app-server", "--stdio"])
        .env(doxa_engines::provider_owner::CONTROL_ENV, fd.to_string());
    unsafe {
        command.pre_exec(move || {
            if libc::fcntl(fd, libc::F_SETFD, 0) < 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut owner = command.spawn().unwrap();
    drop(peer);
    parent
        .set_read_timeout(Some(std::time::Duration::from_secs(5)))
        .unwrap();
    let mut ready = vec![0; doxa_engines::provider_owner::READY.len()];
    parent.read_exact(&mut ready).unwrap();
    assert_eq!(ready, doxa_engines::provider_owner::READY);
    assert!(owner.try_wait().unwrap().is_none());
    parent.write_all(b"G").unwrap();
    assert!(owner.wait().unwrap().success());
}
