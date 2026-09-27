//! Same once-only setup offer as Python; cancelling still consumes the marker.
use std::{fs::{self, File, OpenOptions}, io, os::unix::{fs::{DirBuilderExt, MetadataExt, OpenOptionsExt}, io::AsRawFd}, path::Path};
fn home() -> io::Result<std::path::PathBuf> { crate::operations::doxa_home() }
pub fn needed() -> bool {
    home().is_ok_and(|home| needed_in(&home, &std::env::var("DOXA_SKIP_FIRST_RUN").unwrap_or_default()))
}
fn needed_in(home:&Path, skip:&str) -> bool {
    skip.trim().is_empty() && fs::symlink_metadata(home.join(".setup-done"))
        .is_err_and(|error|error.kind()==io::ErrorKind::NotFound)
}
pub fn mark_seen() -> io::Result<bool> { mark_in(&home()?) }
fn mark_in(home:&Path) -> io::Result<bool> {
    if !home.is_absolute() { return Err(io::Error::other("setup home must be absolute")); }
    if fs::symlink_metadata(home).is_err_and(|error|error.kind()==io::ErrorKind::NotFound) {
        fs::DirBuilder::new().recursive(true).mode(0o700).create(home)?;
    }
    let dir=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open(home)?;
    let meta=dir.metadata()?;
    if !meta.is_dir() || meta.uid()!=unsafe{libc::geteuid()} || meta.mode() & 0o022 != 0 {return Err(io::Error::other("unsafe setup home"));}
    let name=c".setup-done";
    let fd=unsafe{libc::openat(dir.as_raw_fd(),name.as_ptr(),libc::O_WRONLY|libc::O_CREAT|libc::O_EXCL|libc::O_NOFOLLOW|libc::O_CLOEXEC,0o600)};
    if fd<0 { let error=io::Error::last_os_error(); return if error.kind()==io::ErrorKind::AlreadyExists {Ok(false)}else{Err(error)}; }
    use std::os::fd::FromRawFd;
    let file=unsafe{File::from_raw_fd(fd)};
    file.sync_all()?;dir.sync_all()?;Ok(true)
}
#[cfg(test)] mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    #[test] fn first_offer_marks_once_and_uses_private_regular_marker() {
        let dir=tempfile::tempdir().unwrap();
        fs::set_permissions(dir.path(),fs::Permissions::from_mode(0o700)).unwrap();
        assert!(needed_in(dir.path(), ""));
        assert!(!needed_in(dir.path(), "0")); // Python's test kill switch accepts any nonblank value.
        assert!(!needed_in(dir.path(), " yes "));
        assert!(mark_in(dir.path()).unwrap());assert!(!mark_in(dir.path()).unwrap());
        assert!(!needed_in(dir.path(), ""));
        let meta=fs::symlink_metadata(dir.path().join(".setup-done")).unwrap();
        assert!(meta.is_file());assert_eq!(meta.mode()&0o777,0o600);
    }
    #[test] fn symlink_home_or_marker_never_changes_target() {
        let dir=tempfile::tempdir().unwrap();let target=dir.path().join("target");fs::create_dir(&target).unwrap();fs::set_permissions(&target,fs::Permissions::from_mode(0o700)).unwrap();
        let alias=dir.path().join("alias");std::os::unix::fs::symlink(&target,&alias).unwrap();
        assert!(mark_in(&alias).is_err());assert!(!target.join(".setup-done").exists());
        fs::write(target.join("keep"),"unchanged").unwrap();
        std::os::unix::fs::symlink(target.join("keep"),target.join(".setup-done")).unwrap();
        assert!(!mark_in(&target).unwrap());assert_eq!(fs::read_to_string(target.join("keep")).unwrap(),"unchanged");
    }
}
