use doxa_tui::discovery::repo_root_for;
use std::{fs, os::unix::{fs::PermissionsExt, process::CommandExt}, process::{Command, Stdio},
          time::{Duration, Instant}};

#[test]
fn scope_discovery_bounds_git_and_keeps_linked_worktree_identity() {
    const CHILD: &str = "DOXA_SCOPE_DISCOVERY_TEST_ROOT";
    if let Some(root) = std::env::var_os(CHILD) {
        let started = Instant::now();
        assert!(repo_root_for(std::path::Path::new(&root)).is_none());
        assert!(started.elapsed() < Duration::from_secs(3));
        return;
    }
    let temp = tempfile::tempdir().unwrap();
    let repo = temp.path().join("repo");
    let linked = temp.path().join("linked");
    fs::create_dir(&repo).unwrap();
    let git = |args: &[&str]| {
        assert!(Command::new("/usr/bin/git").current_dir(&repo).args(args)
            .env_remove("GIT_DIR").env_remove("GIT_WORK_TREE")
            .env_remove("GIT_COMMON_DIR").env_remove("GIT_INDEX_FILE")
            .status().unwrap().success());
    };
    git(&["init", "-q", "-b", "main"]);
    git(&["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
          "commit", "-q", "--allow-empty", "-m", "fixture"]);
    git(&["worktree", "add", "-q", "-b", "linked", linked.to_str().unwrap()]);
    assert_eq!(repo_root_for(&repo), Some(repo.canonicalize().unwrap()));
    assert_eq!(repo_root_for(&linked), Some(repo.canonicalize().unwrap()));
    assert!(repo_root_for(temp.path()).is_none());

    let bin = temp.path().join("bin");
    fs::create_dir(&bin).unwrap();
    let program = bin.join("git");
    for body in [
        "import time; time.sleep(30)",
        "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])",
        "import sys; sys.stdout.write('x'*70000)",
    ] {
        fs::write(&program, format!("#!/usr/bin/python3\n{body}\n")).unwrap();
        fs::set_permissions(&program, fs::Permissions::from_mode(0o700)).unwrap();
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "scope_discovery_bounds_git_and_keeps_linked_worktree_identity"])
            .env_clear().env("PATH", &bin).env("HOME", temp.path())
            .env("DOXA_HOME", temp.path().join("state")).env("TMPDIR", temp.path())
            .env(CHILD, &repo).process_group(0)
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
            .spawn().unwrap();
        let deadline = Instant::now() + Duration::from_secs(4);
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                assert!(status.success(), "bounded scope discovery fixture failed");
                break;
            }
            if Instant::now() >= deadline {
                unsafe { libc::kill(-(child.id() as i32), libc::SIGKILL); }
                let _ = child.wait();
                panic!("scope discovery stayed blocked on a local Git fixture");
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }
}
