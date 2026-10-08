//! Host-requested Git operations retain a Docker session's process boundary.
//! Repository configuration can run filters, fsmonitor, or hooks. Do not let
//! a worker-owned checkout cause those programs to execute on the host.
use super::{error, inspect, inspect_network, read_manifest, Manifest};
use std::{ffi::OsStr, io, path::Path, process::Command};

/// The checkout's parent manifest is outside all worker mounts. A reserved
/// isolation path with missing/corrupt metadata is refused, never run natively.
pub fn manifest_for(cwd: &Path) -> io::Result<Option<Manifest>> {
    for checkout in cwd.ancestors() {
        if checkout.file_name() != Some(OsStr::new("checkout")) { continue; }
        let Some(root) = checkout.parent() else { continue; };
        if root.parent().and_then(Path::file_name) != Some(OsStr::new("isolation")) { continue; }
        let manifest = read_manifest(&root.join("manifest.json"))?;
        if manifest.checkout != checkout { return Err(error("workspace does not match its saved isolation manifest")); }
        return Ok(Some(manifest));
    }
    Ok(None)
}

fn container_command(command: &Command, manifest: &Manifest, cwd: &Path) -> io::Result<Command> {
    let policy = manifest.policy.as_ref().ok_or_else(|| error("workspace Docker policy unavailable"))?;
    let id = manifest.container_id.as_deref().ok_or_else(|| error("workspace container unavailable"))?;
    let relative = cwd.strip_prefix(&manifest.checkout).map_err(|_| error("workspace command escapes checkout"))?;
    if relative.components().any(|part| !matches!(part, std::path::Component::Normal(_))) {
        return Err(error("invalid workspace command directory"));
    }
    let mut result = super::docker(policy);
    result.args(["exec", "-i", "--workdir"]).arg(Path::new("/workspace").join(relative));
    for setting in ["GIT_OPTIONAL_LOCKS=0", "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null", "GIT_TERMINAL_PROMPT=0"] {
        result.args(["--env", setting]);
    }
    result.arg(id).arg("/usr/bin/git");
    for argument in command.get_args() { result.arg(argument); }
    Ok(result)
}

/// Construct Git commands before configuring pipes/process groups: those are
/// owned by the caller's bounded subprocess runner. Host credentials and
/// configuration environment are never copied into the worker.
pub fn command(command: Command) -> io::Result<Command> {
    if command.get_program() != OsStr::new("git") { return Err(error("workspace transport supports Git only")); }
    let cwd = command.get_current_dir().ok_or_else(|| error("workspace command requires an explicit directory"))?;
    let Some(manifest) = manifest_for(cwd)? else { return Ok(command); };
    if !manifest.profile.docker() { return Ok(command); }
    let actual = inspect(&manifest)?;
    inspect_network(&manifest)?;
    if actual["State"]["Running"] != true {
        return Err(error("session container is stopped; resume it before inspecting its workspace"));
    }
    container_command(&command, &manifest, cwd)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Policy, Profile};
    #[test]
    fn native_commands_keep_the_explicit_workspace() {
        let root = tempfile::tempdir().unwrap();
        let mut original = Command::new("git"); original.arg("status").current_dir(root.path());
        let command = command(original).unwrap();
        assert_eq!(command.get_program(), "git");
        assert_eq!(command.get_current_dir(), Some(root.path()));
    }
    #[test]
    fn missing_reserved_manifest_never_falls_back_to_host_git() {
        let root = tempfile::tempdir().unwrap();
        let checkout = root.path().join("isolation/session/checkout");
        std::fs::create_dir_all(&checkout).unwrap();
        let mut original = Command::new("git"); original.arg("status").current_dir(checkout);
        assert!(command(original).is_err());
    }
    #[test]
    fn container_git_never_inherits_host_keys_or_configuration() {
        let manifest = Manifest { version:1, session_id:"session".into(), profile:Profile::DockerOpen,
            policy:Some(Policy { image:format!("sha256:{}", "a".repeat(64)), docker_host:"unix:///run/user/1000/task.sock".into(), memory_bytes:1024*1024*1024, cpus:1.0,pids:128 }),
            policy_hash:String::new(),creation_policy_hash:String::new(),source:"/original".into(),checkout:"/private/checkout".into(),
            checkout_device:0,checkout_inode:0,base_sha:String::new(),branch:String::new(),private_home:"/private/home".into(),cache:"/private/cache".into(),broker:"/private/broker".into(),container_id:Some("b".repeat(64)),nonce:"c".repeat(48),state:"ready".into() };
        let mut original = Command::new("git"); original.args(["diff", "--no-ext-diff"]).env("PRIVATE_KEY","never forward").env("GIT_CONFIG_COUNT","1");
        let mapped = container_command(&original,&manifest,Path::new("/private/checkout/src")).unwrap();
        let args:Vec<_> = mapped.get_args().map(|arg|arg.to_string_lossy().into_owned()).collect();
        assert!(args.windows(2).any(|pair|pair==["--workdir","/workspace/src"]));
        assert!(args.iter().any(|arg|arg=="/usr/bin/git"));
        assert!(!args.iter().any(|arg|arg.contains("PRIVATE_KEY")||arg.contains("GIT_CONFIG_COUNT")||arg.contains("never forward")));
    }
}
