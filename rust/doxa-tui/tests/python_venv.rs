use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;

fn venv(root: &Path) -> PathBuf {
    let path = root.join("venv");
    let status = Command::new("python3")
        .args(["-m", "venv", "--without-pip"])
        .arg(&path)
        .status()
        .unwrap();
    assert!(status.success());
    let python = path.join("bin/python3");
    assert!(fs::symlink_metadata(&python).unwrap().file_type().is_symlink());
    let site = Command::new(&python)
        .args(["-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"])
        .output()
        .unwrap();
    assert!(site.status.success());
    let site = PathBuf::from(String::from_utf8(site.stdout).unwrap().trim());
    fs::write(site.join("doxa_venv_marker.py"), "VALUE = 'venv only'\n").unwrap();
    let imported = Command::new(&python)
        .args(["-c", "import doxa_venv_marker, sys; assert doxa_venv_marker.VALUE == 'venv only'; print(sys.prefix)"])
        .current_dir("/")
        .output()
        .unwrap();
    assert!(imported.status.success());
    assert_eq!(String::from_utf8(imported.stdout).unwrap().trim(), path.to_str().unwrap());
    python
}

fn captured_python(args: &str, flag: &str) -> String {
    let words: Vec<_> = args.lines().collect();
    words.windows(2).find(|pair| pair[0] == flag).unwrap()[1].to_owned()
}

#[test]
fn frontend_passes_venv_symlink_to_daemon_and_doctor() {
    let dir = tempfile::tempdir().unwrap();
    let python = venv(dir.path());
    let daemon = dir.path().join("fake-daemon");
    let capture = dir.path().join("argv.txt");
    fs::write(&daemon, "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$DOXA_CAPTURE_ARGS\"\nexit 1\n").unwrap();
    fs::set_permissions(&daemon, fs::Permissions::from_mode(0o700)).unwrap();
    let path = format!("{}:{}", python.parent().unwrap().display(), std::env::var("PATH").unwrap());

    let doctor = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args(["doctor", "--engine", "deepseek"])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("PATH", &path)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .env("DEEPSEEK_API_KEY", "test-key")
        .current_dir("/")
        .output()
        .unwrap();
    assert!(doctor.status.success(), "{}", String::from_utf8_lossy(&doctor.stderr));
    assert!(String::from_utf8_lossy(&doctor.stdout).contains(&format!("ok lore python: {}", python.display())));

    let vendor = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args(["new", "--engine", "deepseek"])
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("PATH", &path)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .env("DEEPSEEK_API_KEY", "test-key")
        .current_dir("/")
        .output()
        .unwrap();
    assert!(!vendor.status.success()); // fake daemon exits before registering
    assert_eq!(captured_python(&fs::read_to_string(&capture).unwrap(), "--lore-python"), python.to_str().unwrap());

    let sidecar = dir.path().join("sidecar.py");
    fs::write(&sidecar, "# Claude sidecar fixture\n").unwrap();
    let claude = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
        .args(["new", "--engine", "claude"])
        .arg("--claude-script")
        .arg(&sidecar)
        .env("DOXA_DAEMON_BIN", &daemon)
        .env("PATH", &path)
        .env("DOXA_CAPTURE_ARGS", &capture)
        .env("DOXA_RUNTIME_DIR", dir.path().join("runtime"))
        .current_dir("/")
        .output()
        .unwrap();
    assert!(!claude.status.success());
    assert_eq!(captured_python(&fs::read_to_string(&capture).unwrap(), "--claude-python"), python.to_str().unwrap());
}
