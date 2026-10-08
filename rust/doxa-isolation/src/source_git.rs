//! Config-free source snapshots for host-side cloning and native migration.
//! Every mutable entry is opened relative to an already-open directory with
//! O_NOFOLLOW. No source config, hooks, alternates or replace refs enter Git.
use crate::{error, run};
use std::{ffi::{CString, OsStr}, fs::{self, File, OpenOptions}, io::{self, Read, Write},
    os::{fd::{AsRawFd, FromRawFd}, unix::{ffi::OsStrExt, fs::{MetadataExt, OpenOptionsExt, PermissionsExt}}},
    path::{Path, PathBuf}, process::Command};

fn open_entry(parent: &File, name: &OsStr) -> io::Result<File> {
    let name = CString::new(name.as_bytes()).map_err(|_| error("invalid Git entry"))?;
    let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC) };
    if fd < 0 { return Err(io::Error::last_os_error()); }
    let file = unsafe { File::from_raw_fd(fd) };
    let meta = file.metadata()?;
    if meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o002 != 0
        || (!meta.is_dir() && (!meta.is_file() || meta.nlink() != 1)) {
        return Err(error("Git snapshot refuses unowned, linked or special metadata"));
    }
    Ok(file)
}
fn directory(path: &Path) -> io::Result<File> {
    let file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY | libc::O_CLOEXEC).open(path)?;
    if file.metadata()?.uid() != unsafe { libc::geteuid() } { return Err(error("Git directory is not owned")); }
    Ok(file)
}
fn text(mut file: File) -> io::Result<String> {
    if !file.metadata()?.is_file() || file.metadata()?.len() > 16 * 1024 { return Err(error("Git pointer is unsafe or oversized")); }
    let mut bytes = Vec::new(); Read::by_ref(&mut file).take(16 * 1024 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > 16 * 1024 { return Err(error("Git pointer grew beyond limit")); }
    String::from_utf8(bytes).map_err(|_| error("Git pointer is not UTF-8"))
}
fn pointer(base: &Path, value: &str) -> io::Result<PathBuf> {
    let value = value.trim_end_matches('\n');
    if value.is_empty() || value.chars().any(char::is_control) { return Err(error("Git pointer is invalid")); }
    fs::canonicalize(base.join(value))
}
fn metadata(source: &Path, allow_linked: bool) -> io::Result<(File, File)> {
    let root = directory(source)?;
    let entry = open_entry(&root, OsStr::new(".git"))?;
    if entry.metadata()?.is_dir() {
        if open_entry(&entry, OsStr::new("commondir")).is_ok() { return Err(error("Git directory unexpectedly redirects to shared metadata")); }
        return Ok((entry.try_clone()?, entry));
    }
    if !allow_linked { return Err(error("mutable Docker checkout cannot redirect its Git metadata")); }
    let value = text(entry)?;
    let path = pointer(source, value.strip_prefix("gitdir: ").ok_or_else(|| error("native Git pointer is invalid"))?)?;
    let gitdir = directory(&path)?;
    let common = pointer(&path, &text(open_entry(&gitdir, OsStr::new("commondir"))?)?)?;
    // A native linked worktree must have the normal owner-controlled layout
    // and a matching backlink. Arbitrary gitdir files are not authority.
    let relative = path.strip_prefix(common.join("worktrees")).map_err(|_| error("native linked Git metadata has an invalid common directory"))?;
    if relative.components().count() != 1 { return Err(error("native linked Git metadata has an invalid worktree identity")); }
    let backlink = pointer(&path, &text(open_entry(&gitdir, OsStr::new("gitdir"))?)?)?;
    if backlink != source.join(".git") { return Err(error("native linked Git backlink differs from its checkout")); }
    Ok((gitdir, directory(&common)?))
}
fn copy_file(mut file: File, target: &Path) -> io::Result<()> {
    let before = file.metadata()?;
    if !before.is_file() || before.nlink() != 1 { return Err(error("Git snapshot contains shared or special files")); }
    let mut output = OpenOptions::new().write(true).create_new(true).open(target)?;
    output.set_permissions(fs::Permissions::from_mode(0o600))?;
    io::copy(&mut file, &mut output)?;
    let after = file.metadata()?;
    if (before.len(), before.mtime(), before.mtime_nsec(), before.ctime(), before.ctime_nsec())
        != (after.len(), after.mtime(), after.mtime_nsec(), after.ctime(), after.ctime_nsec()) {
        return Err(error("Git metadata changed while snapshotting; retry after writers stop"));
    }
    Ok(())
}
fn copy_tree(source: File, target: &Path, depth: usize) -> io::Result<()> {
    if !source.metadata()?.is_dir() || depth > 32 { return Err(error("Git tree is invalid or too deep")); }
    fs::create_dir(target)?; fs::set_permissions(target, fs::Permissions::from_mode(0o700))?;
    for entry in fs::read_dir(format!("/proc/self/fd/{}", source.as_raw_fd()))? {
        let name = entry?.file_name();
        if matches!(name.to_str(), Some("alternates" | "http-alternates" | "replace")) {
            return Err(error("Git snapshot refuses alternates or replacement refs"));
        }
        let file = open_entry(&source, &name)?;
        if file.metadata()?.is_dir() { copy_tree(file, &target.join(&name), depth + 1)?; }
        else { copy_file(file, &target.join(&name))?; }
    }
    Ok(())
}
pub(crate) fn snapshot(source: &Path, allow_linked: bool) -> io::Result<tempfile::TempDir> {
    let (gitdir, common) = metadata(source, allow_linked)?;
    let snapshot = tempfile::Builder::new().prefix("doxa-git-snapshot-").tempdir()?;
    fs::set_permissions(snapshot.path(), fs::Permissions::from_mode(0o700))?;
    copy_tree(open_entry(&common, OsStr::new("objects"))?, &snapshot.path().join("objects"), 0)?;
    copy_tree(open_entry(&common, OsStr::new("refs"))?, &snapshot.path().join("refs"), 0)?;
    copy_file(open_entry(&gitdir, OsStr::new("HEAD"))?, &snapshot.path().join("HEAD"))?;
    for (parent, name) in [(&common, "packed-refs"), (&gitdir, "index")] {
        match open_entry(parent, OsStr::new(name)) {
            Ok(file) => copy_file(file, &snapshot.path().join(name))?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => {},
            Err(error) => return Err(error),
        }
    }
    let mut config = OpenOptions::new().write(true).create_new(true).open(snapshot.path().join("config"))?;
    config.set_permissions(fs::Permissions::from_mode(0o600))?;
    config.write_all(b"[core]\nrepositoryformatversion = 0\nbare = true\nfsmonitor = false\nhooksPath = /dev/null\n")?;
    Ok(snapshot)
}
/// Preserve a trusted native worktree's staged changes without letting its
/// repository config execute host commands. Docker sources are refused.
pub fn staged_diff(source: &Path) -> io::Result<Vec<u8>> {
    let source = fs::canonicalize(source)?;
    if crate::active()?.is_some_and(|manifest| manifest.profile.docker() && manifest.checkout == source) {
        return Err(error("host staging snapshot cannot read an active Docker checkout"));
    }
    let snapshot = snapshot(&source, true)?;
    let mut command = Command::new("git");
    command.arg("--git-dir").arg(snapshot.path()).arg("--work-tree").arg(&source)
        .args(["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv"])
        .env("GIT_CONFIG_NOSYSTEM", "1").env("GIT_CONFIG_GLOBAL", "/dev/null").env("GIT_NO_REPLACE_OBJECTS", "1")
        .env_remove("GIT_CONFIG_PARAMETERS").env_remove("GIT_CONFIG_COUNT").env_remove("GIT_TEMPLATE_DIR")
        .env_remove("GIT_DIR").env_remove("GIT_WORK_TREE").env_remove("GIT_ALTERNATE_OBJECT_DIRECTORIES").env_remove("GIT_OBJECT_DIRECTORY")
        .env_remove("GIT_COMMON_DIR").env_remove("GIT_INDEX_FILE").env_remove("GIT_SHALLOW_FILE");
    run(command)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture(root: &Path) -> PathBuf {
        let source = root.join("source"); fs::create_dir(&source).unwrap();
        crate::git(&source, &["init"]).unwrap();
        fs::write(source.join("README.txt"), "base\n").unwrap();
        fs::write(source.join(".gitattributes"), "*.txt filter=untrusted diff=untrusted\n").unwrap();
        crate::git(&source, &["add", "."]).unwrap();
        crate::git(&source, &["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "feat: fixture"]).unwrap();
        source
    }
    #[test]
    fn snapshot_clone_and_staging_ignore_source_execution_config() {
        let root = tempfile::tempdir().unwrap(); let source = fixture(root.path());
        fs::write(source.join("README.txt"), "staged\n").unwrap();
        crate::git(&source, &["add", "README.txt"]).unwrap();
        let marker = root.path().join("host-command-ran");
        let config = format!("\n[core]\nfsmonitor = touch {}\n[filter \"untrusted\"]\nsmudge = touch {}; cat\nclean = touch {}; cat\n[diff \"untrusted\"]\ncommand = touch {}\n[include]\npath = /no-such-host-config\n", marker.display(), marker.display(), marker.display(), marker.display());
        OpenOptions::new().append(true).open(source.join(".git/config")).unwrap().write_all(config.as_bytes()).unwrap();
        let checkout = root.path().join("checkout");
        crate::clone_checkout(&source, &checkout, "safe", None).unwrap();
        assert_eq!(fs::read_to_string(checkout.join("README.txt")).unwrap(), "base\n");
        let patch = staged_diff(&source).unwrap();
        assert!(String::from_utf8(patch).unwrap().contains("+staged"));
        assert!(!marker.exists(), "source fsmonitor/filter/diff executed on the host");
    }
    #[test]
    fn snapshot_rejects_redirects_symlinks_and_shared_object_files() {
        let root = tempfile::tempdir().unwrap(); let source = fixture(root.path());
        let info = source.join(".git/objects/info/alternates");
        fs::write(&info, "/outside/object-store\n").unwrap();
        assert!(snapshot(&source, false).is_err()); fs::remove_file(info).unwrap();
        let external = root.path().join("external"); fs::write(&external, "outside").unwrap();
        let injected = source.join(".git/objects/injected");
        std::os::unix::fs::symlink(&external, &injected).unwrap();
        assert!(snapshot(&source, false).is_err()); fs::remove_file(&injected).unwrap();
        fs::hard_link(&external, &injected).unwrap();
        assert!(snapshot(&source, false).is_err()); fs::remove_file(injected).unwrap();
        fs::rename(source.join(".git"), root.path().join("outside-git")).unwrap();
        std::os::unix::fs::symlink(root.path().join("outside-git"), source.join(".git")).unwrap();
        assert!(snapshot(&source, true).is_err());
    }
    #[test]
    fn native_linked_worktree_snapshot_is_verified_but_worker_redirect_is_refused() {
        let root = tempfile::tempdir().unwrap(); let root = fs::canonicalize(root.path()).unwrap();
        let source = fixture(&root); let linked = root.join("linked");
        crate::git(&source, &["worktree", "add", "--detach", linked.to_str().unwrap()]).unwrap();
        let snapshot = snapshot(&linked, true).unwrap();
        assert_eq!(crate::git(snapshot.path(), &["rev-parse", "HEAD"]).unwrap(), crate::git(&source, &["rev-parse", "HEAD"]).unwrap());
        assert!(super::snapshot(&linked, false).is_err());
        let gitdir = source.join(".git/worktrees/linked");
        fs::write(gitdir.join("gitdir"), source.join(".git").to_str().unwrap()).unwrap();
        assert!(super::snapshot(&linked, true).is_err());
    }
}
