//! Separate processes are required: a stack overflow aborts the whole process.
use std::{fs, process::{Command, Stdio}, thread, time::{Duration, Instant}};

#[test]
fn shipped_codegraph_cli_withholds_pathological_rust_without_aborting() {
    let owned = tempfile::tempdir().unwrap();
    let worktree = owned.path().join("worktree");
    fs::create_dir(&worktree).unwrap();
    assert!(Command::new("git").args(["init", "-q"]).arg(&worktree)
        .env_clear().env("PATH", "/usr/bin:/bin").status().unwrap().success());
    fs::write(worktree.join("normal.rs"), "fn ordinary() { target(); }\nfn target() {}\n").unwrap();
    for pathological in [
        format!("fn f() {{ {}0;{} }}", "{".repeat(500), "}".repeat(500)),
        format!("fn f() {{ {}true; }}", "!".repeat(500)),
        format!("fn f() {{ {}0; }}", "if true {} else ".repeat(500)),
        format!("{}fn f() {{}}{}", "mod m {".repeat(500), "}".repeat(500)),
        format!("type T = {}u8{};", "Option<".repeat(500), ">".repeat(500)),
        format!("fn f() {{ {}; }}", (0..500).map(|_| "{} + ").collect::<String>() + "0"),
    ] {
        fs::write(worktree.join("deep.rs"), pathological).unwrap();
        // Even an unrelated supported-language inventory input must be safe.
        let mut child = Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
            .env_clear().env("PATH", "/usr/bin:/bin").env("HOME", owned.path())
            .env("DOXA_HOME", owned.path().join("doxa")).env("TMPDIR", owned.path())
            .args(["codegraph", "--root"]).arg(&worktree).args(["calls", "normal.rs"])
            .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped())
            .spawn().unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if child.try_wait().unwrap().is_some() { break; }
            if Instant::now() >= deadline {
                child.kill().unwrap(); let _ = child.wait();
                panic!("codegraph CLI exceeded the private regression deadline");
            }
            thread::sleep(Duration::from_millis(5));
        }
        let output = child.wait_with_output().unwrap();
        assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
        let answer: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(answer["coverage"]["rust_unparseable_files"], 1);
        assert!(answer["scan_input_sha256"].is_null());
        assert!(answer["coverage"]["unparseable"]["examples"][0]["reason"]
            .as_str().unwrap().contains("recursive parsing withheld"));
        assert_eq!(answer["edges"][0]["target"], "target");
    }
}
