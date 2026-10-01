//! Kernel identities and bounded Unix socket creation on supported hosts.
use std::{io, os::{fd::{AsRawFd, FromRawFd, OwnedFd}, unix::net::UnixStream}};

pub struct PeerCredentials { pub uid: libc::uid_t, pub pid: libc::pid_t }

pub fn peer_credentials(stream: &UnixStream) -> io::Result<PeerCredentials> {
    #[cfg(target_os = "linux")]
    {
        let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
        let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
        if unsafe { libc::getsockopt(stream.as_raw_fd(), libc::SOL_SOCKET, libc::SO_PEERCRED,
            (&mut cred as *mut libc::ucred).cast(), &mut len) } != 0 { return Err(io::Error::last_os_error()); }
        if len as usize != std::mem::size_of::<libc::ucred>() || cred.pid <= 0 {
            return Err(io::Error::other("Unix peer credentials unavailable"));
        }
        Ok(PeerCredentials { uid:cred.uid, pid:cred.pid })
    }
    #[cfg(target_os = "macos")]
    {
        let mut uid = 0; let mut gid = 0;
        if unsafe { libc::getpeereid(stream.as_raw_fd(), &mut uid, &mut gid) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let mut pid: libc::pid_t = 0;
        let mut len = std::mem::size_of::<libc::pid_t>() as libc::socklen_t;
        if unsafe { libc::getsockopt(stream.as_raw_fd(), libc::SOL_LOCAL, libc::LOCAL_PEERPID,
            (&mut pid as *mut libc::pid_t).cast(), &mut len) } != 0 { return Err(io::Error::last_os_error()); }
        if len as usize != std::mem::size_of::<libc::pid_t>() || pid <= 0 {
            return Err(io::Error::other("Unix peer PID unavailable"));
        }
        Ok(PeerCredentials { uid, pid })
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    { let _ = stream; Err(io::Error::other("Unix peer credentials unavailable on this host")) }
}

/// Create a nonblocking CLOEXEC Unix socket before attempting a bounded dial.
pub fn nonblocking_unix_socket() -> io::Result<OwnedFd> {
    #[cfg(target_os = "linux")]
    let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC, 0) };
    #[cfg(not(target_os = "linux"))]
    let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_STREAM, 0) };
    if fd < 0 { return Err(io::Error::last_os_error()); }
    let fd = unsafe { OwnedFd::from_raw_fd(fd) };
    #[cfg(not(target_os = "linux"))]
    {
        if unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_SETFD, libc::FD_CLOEXEC) } < 0
            || unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_SETFL, libc::O_NONBLOCK) } < 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(fd)
}
