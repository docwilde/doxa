//! Optional Python peer bridge bootstrap; authorization stays in remote_policy.
use std::{io, path::Path, process::{Child, Command, Stdio}, time::{Duration, Instant}};

pub struct Bootstrap { child: Child, deadline: Instant, cancelled: bool }
impl Bootstrap {
    pub fn request(python: Option<&Path>, runtime: &Path) -> io::Result<Option<Self>> {
        let home = std::env::var_os("DOXA_HOME").filter(|value| !value.is_empty()).map(std::path::PathBuf::from)
            .unwrap_or_else(|| std::path::PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".doxa"));
        let stored = doxa_state::load_config(&home.join("config.toml"));
        let value = doxa_state::raw_setting(std::env::var("DOXA_REMOTE_ENABLED").ok().as_deref(), &stored, "remote_enabled");
        let value = value.trim().to_ascii_lowercase();
        if value.is_empty() || matches!(value.as_str(), "0" | "false" | "no" | "off") { return Ok(None); }
        let python = python.ok_or_else(|| io::Error::other("remote peer bridge needs the session's private Python interpreter"))?;
        let child = Command::new(python).args(["-m", "doxa.peernet", "--ensure-runtime-bridge"])
            .env("DOXA_RUNTIME_DIR", runtime).current_dir(runtime)
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()?;
        Ok(Some(Self { child, deadline:Instant::now() + Duration::from_secs(3), cancelled:false }))
    }
    /// The session owns only the bootstrap child. The bridge process follows
    /// the shared registry and survives the first session's exit.
    pub fn poll(&mut self) -> Option<bool> {
        match self.child.try_wait() {
            Ok(Some(status)) => Some(status.success() && !self.cancelled),
            Ok(None) if !self.cancelled && Instant::now() >= self.deadline => {
                let _ = self.child.kill(); self.cancelled = true; None
            }
            Ok(None) => None,
            Err(_) => { let _ = self.child.kill(); self.cancelled = true; None }
        }
    }
}
impl Drop for Bootstrap {
    fn drop(&mut self) { let _ = self.child.kill(); let _ = self.child.wait(); }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::PermissionsExt};
    #[test]
    fn optional_bootstrap_is_off_by_default_and_passes_only_canonical_operation() {
        let dir = tempfile::tempdir().unwrap();
        let old_home = std::env::var_os("DOXA_HOME");
        let old_enabled = std::env::var_os("DOXA_REMOTE_ENABLED");
        std::env::set_var("DOXA_HOME", dir.path());
        std::env::remove_var("DOXA_REMOTE_ENABLED");
        assert!(Bootstrap::request(None, dir.path()).unwrap().is_none());
        fs::write(dir.path().join("config.toml"), "remote_enabled = true\n").unwrap();
        assert!(Bootstrap::request(None, dir.path()).is_err());
        std::env::set_var("DOXA_REMOTE_ENABLED", "0");
        assert!(Bootstrap::request(None, dir.path()).unwrap().is_none());
        std::env::set_var("DOXA_REMOTE_ENABLED", "1");
        let script = dir.path().join("python-fixture");
        fs::write(&script, "#!/bin/sh\ntest \"$1\" = -m && test \"$2\" = doxa.peernet && test \"$3\" = --ensure-runtime-bridge && test \"$DOXA_RUNTIME_DIR\" = \"$PWD\"\n").unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
        let mut bootstrap = Bootstrap::request(Some(&script), dir.path()).unwrap().unwrap();
        let result = loop {
            if let Some(result) = bootstrap.poll() { break result; }
            std::thread::sleep(Duration::from_millis(5));
        };
        assert!(result);
        if let Some(value) = old_home { std::env::set_var("DOXA_HOME", value); } else { std::env::remove_var("DOXA_HOME"); }
        if let Some(value) = old_enabled { std::env::set_var("DOXA_REMOTE_ENABLED", value); } else { std::env::remove_var("DOXA_REMOTE_ENABLED"); }
    }
}
