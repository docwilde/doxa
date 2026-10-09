//! Explicit, read-only package review in the TUI. Execution remains in the
//! separately gated Linux CLI until the installed TUI host passes acceptance.
use super::{App, ChipInfo};
use crate::native_plugins::packages::Review;
use std::path::Path;
use std::sync::mpsc::{self, TryRecvError};

const USAGE: &str = "Usage: /native-plugin preflight NAME";

fn request(args: &str) -> Result<&str, &'static str> {
    let parts = args.split_whitespace().collect::<Vec<_>>();
    let ["preflight", name] = parts.as_slice() else { return Err(USAGE); };
    let bytes = name.as_bytes();
    if bytes.is_empty() || bytes.len() > 48 || !bytes[0].is_ascii_lowercase()
        || !bytes.iter().all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-') {
        return Err("Package name must be 1–48 lowercase ASCII letters, digits or hyphens, starting with a letter");
    }
    Ok(name)
}

fn review_lines(review: &Review) -> Vec<String> {
    let mut lines = review.report().lines().map(str::to_owned).collect::<Vec<_>>();
    lines.push(String::new());
    if review.owner_approved && review.requested_grants.is_empty() {
        lines.push("Eligible for explicit grantless Linux CLI run only, on an accepted delegated host:".into());
        lines.push(format!("doxa native-plugin run {} --grantless-prototype", review.name));
        lines.push("The CLI rechecks owner approval, bytes and inodes immediately before cgroup/Bubblewrap admission.".into());
    } else {
        lines.push("Execution unavailable: exact owner approval and zero requested grants are required.".into());
    }
    lines.push("This is a read-only snapshot. TUI execution remains disabled pending installed-host acceptance.".into());
    lines
}

impl App {
    pub(super) fn open_native_package_review(&mut self, args: &str) {
        let name = match request(args) {
            Ok(name) => name,
            Err(error) => { self.notice = error.into(); return; }
        };
        let home = match crate::operations::doxa_home() {
            Ok(home) => home,
            Err(error) => { self.notice = format!("Native package review: {error}"); return; }
        };
        self.open_native_package_review_at(&home, name);
    }

    fn open_native_package_review_at(&mut self, home: &Path, name: &str) {
        if self.native_package_pending.is_some() {
            self.notice = "Wait for the current native package review to finish".into();
            return;
        }
        self.chip_info = Some(ChipInfo {
            kind: "native_package_review",
            label: name.into(),
            lines: vec![format!("Reviewing owner package {name}…")],
            scroll: 0,
            owner: None,
        });
        if self.active_chooser_rect().is_none() {
            self.chip_info = None;
            self.notice = "Enlarge active pane to review native package".into();
            return;
        }
        let (sender, receiver) = mpsc::channel();
        let home = home.to_owned();
        let name = name.to_owned();
        let thread_name = name.clone();
        std::thread::spawn(move || {
            let result = crate::native_plugins::packages::preflight(&home, &thread_name)
                .map_err(|error| error.to_string());
            let _ = sender.send(result);
        });
        self.native_package_pending = Some((name, receiver));
        self.input.clear();
        self.input_cursor = 0;
    }

    pub(super) fn poll_native_package_review(&mut self) -> bool {
        let Some((name, receiver)) = &self.native_package_pending else { return false; };
        let result = match receiver.try_recv() {
            Ok(result) => result,
            Err(TryRecvError::Empty) => return false,
            Err(TryRecvError::Disconnected) => Err("Native package review worker stopped".into()),
        };
        let name = name.clone();
        self.native_package_pending = None;
        let Some(info) = self.chip_info.as_mut().filter(|info| info.kind == "native_package_review" && info.label == name) else {
            return false;
        };
        info.lines = match result {
            Ok(review) => review_lines(&review),
            Err(error) => vec!["Native package review failed; nothing was activated.".into(),
                super::safe_label(&error)],
        };
        info.scroll = 0;
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::time::{Duration, Instant};

    fn write(path: &Path, bytes: &[u8]) {
        fs::write(path, bytes).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
    }

    fn fixture(approved: bool, grants: &[&str]) -> tempfile::TempDir {
        let home = tempfile::tempdir().unwrap();
        let packages = home.path().join("native-plugin-packages");
        let package = packages.join("demo");
        for directory in [home.path(), packages.as_path(), package.as_path()] {
            fs::create_dir_all(directory).unwrap();
            fs::set_permissions(directory, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let module = wat::parse_str("(module (func (export \"doxa_main\") (result i32) i32.const 17))").unwrap();
        let grant_list = grants.iter().map(|grant| format!("'{grant}'")).collect::<Vec<_>>().join(", ");
        let manifest = format!("package_api_version = 1\nname = 'demo'\nversion = '1.0'\nartifact_format = 'wasm-core-v1'\nrequested_grants = [{grant_list}]\n");
        write(&package.join("manifest.toml"), manifest.as_bytes());
        write(&package.join("module.wasm"), &module);
        if approved {
            let config = format!("[[native_plugin_packages]]\nname = 'demo'\nmanifest_sha256 = '{:x}'\nmodule_sha256 = '{:x}'\ngrants = [{grant_list}]\n",
                Sha256::digest(manifest.as_bytes()), Sha256::digest(&module));
            write(&home.path().join("config.toml"), config.as_bytes());
        }
        home
    }

    fn finished(app: &mut App) -> Vec<String> {
        let deadline = Instant::now() + Duration::from_secs(3);
        while !app.poll_native_package_review() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        assert!(app.native_package_pending.is_none(), "preflight worker did not finish");
        app.chip_info.as_ref().unwrap().lines.clone()
    }

    #[test]
    fn exact_zero_grant_review_shows_cli_gate_without_running_worker() {
        let home = fixture(true, &[]);
        let mut app = App::default();
        app.open_native_package_review_at(home.path(), "demo");
        assert_eq!(app.chip_info.as_ref().unwrap().kind, "native_package_review");
        let lines = finished(&mut app);
        assert!(lines.iter().any(|line| line.contains("exact identity and grants match")));
        assert!(lines.iter().any(|line| line == "doxa native-plugin run demo --grantless-prototype"));
        assert!(lines.iter().any(|line| line.contains("TUI execution remains disabled")));
        assert!(app.local_shell_jobs.is_empty() && app.pending_prompts.is_empty());
    }

    #[test]
    fn unapproved_and_grantful_reviews_never_offer_run() {
        for (approved, grants) in [(false, &[][..]), (true, &["render-local-panel-v1"][..])] {
            let home = fixture(approved, grants);
            let mut app = App::default();
            app.open_native_package_review_at(home.path(), "demo");
            let lines = finished(&mut app);
            assert!(!lines.iter().any(|line| line.starts_with("doxa native-plugin run ")));
            assert!(lines.iter().any(|line| line.contains("Execution unavailable")));
        }
    }

    #[test]
    fn invalid_forms_are_consumed_locally_without_a_worker() {
        for draft in ["/native-plugin", "/native-plugin run demo", "/native-plugin preflight ../demo",
            "/native-plugin preflight demo extra", "/native-plugin unknown demo"] {
            let mut app = App::default();
            app.input = draft.into();
            assert!(app.submit_local_command(), "{draft}");
            assert_eq!(app.input, draft);
            assert!(app.native_package_pending.is_none());
            assert!(app.pending_prompts.is_empty() && app.local_shell_jobs.is_empty());
        }
    }
}
