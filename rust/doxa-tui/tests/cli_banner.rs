//! Informational CLI dispatch stays local and works when stdout is redirected.
use std::process::Command;

#[test]
fn help_and_version_aliases_share_greek_banner_and_exact_package_identity() {
    let private_home = tempfile::tempdir().unwrap();
    let invoke = |args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_doxa-rs"))
            .args(args)
            .env_clear()
            .env("HOME", private_home.path())
            .env("TMPDIR", private_home.path())
            .env("DOXA_HOME", private_home.path().join("doxa"))
            .output()
            .unwrap()
    };
    let mut help = None;
    let mut version = None;
    for (aliases, expected) in [
        (&["help", "--help", "-help", "-h"][..], &mut help),
        (&["--version", "-V"][..], &mut version),
    ] {
        for alias in aliases {
            let output = invoke(&[alias]);
            assert!(output.status.success(), "{alias}: {}", String::from_utf8_lossy(&output.stderr));
            assert!(output.stderr.is_empty());
            let text = String::from_utf8(output.stdout).unwrap();
            assert!(text.contains("ΔΟΞΑ · DOXA"));
            assert!(text.contains(&format!("doxa {}", env!("CARGO_PKG_VERSION"))));
            assert!(text.contains("Author   docwilde <docwilde@proton.me>"));
            assert!(!text.contains('\u{1b}'), "redirected output must be ANSI-free");
            match expected.as_ref() {
                Some(previous) => assert_eq!(&text, previous, "alias {alias}"),
                None => *expected = Some(text),
            }
        }
    }
    let help = help.unwrap();
    for inventory in ["Usage\n  doxa [COMMAND] [options]", "Commands:", "settings set KEY VALUE", "codegraph", "Rust/Python syntax", "fleet ...", "remote tui URL", "--router-config"] {
        assert!(help.contains(inventory), "missing help entry {inventory}");
    }
    let version = version.unwrap();
    assert!(version.contains("Build    native Rust"));
    assert!(version.contains(&format!("Platform {}/{}", std::env::consts::OS, std::env::consts::ARCH)));
    assert!(version.contains("Commit   "));
    for bad in [&["--help", "extra"][..], &["-V", "extra"][..]] {
        let output = invoke(bad);
        assert!(!output.status.success());
        assert!(output.stdout.is_empty());
    }
    assert!(!private_home.path().join("doxa").exists(), "information must not create session or config state");
}
