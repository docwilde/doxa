//! Host Unix sockets below `DOXA_HOME/isolation/SESSION_ID/broker/` must fit
//! `sockaddr_un.sun_path` (108 bytes on Linux, including the trailing NUL).
//! Checking the budget before admission replaces the kernel's opaque
//! "path must be shorter than SUN_LEN" failure after a session has started.
use crate::{error, Profile};
use std::{io, os::unix::ffi::OsStrExt, path::Path};

/// Session identities minted by DOXA are 36-byte UUIDs.
pub const SESSION_ID_BYTES: usize = 36;
/// Docker sessions bind the hook broker and one egress gateway per provider.
const DOCKER_SOCKETS: [&str; 3] = ["hook.sock", "egress-claude.sock", "egress-codex.sock"];

/// `sun_path` capacity in bytes, including the NUL terminator.
pub fn unix_socket_path_bytes() -> usize {
    // SAFETY: sockaddr_un is plain old data; the zeroed value is only measured.
    let address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    address.sun_path.len()
}

/// The longest socket path suffix after `DOXA_HOME` for a session ID length.
fn suffix_bytes(session_id_bytes: usize) -> usize {
    let longest = DOCKER_SOCKETS.iter().map(|name| name.len()).max().unwrap_or(0);
    "/isolation/".len() + session_id_bytes + "/broker/".len() + longest
}

/// Largest `DOXA_HOME` length, in bytes, whose Docker session sockets fit.
pub fn docker_home_budget(session_id_bytes: usize) -> usize {
    unix_socket_path_bytes().saturating_sub(1 + suffix_bytes(session_id_bytes))
}

/// Refuse a Docker home before any session file, container or socket exists.
/// ROOT-hardened sessions bind their sockets below the ROOT policy root.
pub fn check_docker_socket_paths(home: &Path, session_id_bytes: usize, profile: Profile) -> io::Result<()> {
    if !profile.docker() || profile == Profile::DockerHardened { return Ok(()); }
    // Measure the joined path so separators match the actual bind target.
    let longest = DOCKER_SOCKETS.iter().copied().max_by_key(|name| name.len()).unwrap_or("hook.sock");
    let probe = home.join("isolation").join("x".repeat(session_id_bytes)).join("broker").join(longest);
    let actual = probe.as_os_str().as_bytes().len().saturating_sub(suffix_bytes(session_id_bytes));
    let budget = docker_home_budget(session_id_bytes);
    if probe.as_os_str().as_bytes().len() < unix_socket_path_bytes() { return Ok(()); }
    Err(error(format!(
        "DOXA_HOME is {actual} bytes, but Docker sessions allow at most {budget}: session sockets such as \
         DOXA_HOME/isolation/<{session_id_bytes}-byte session ID>/broker/{longest} must fit the {}-byte Unix \
         socket path limit (including the trailing NUL). Set DOXA_HOME to a shorter absolute path",
        unix_socket_path_bytes())))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn home(bytes: usize) -> PathBuf { PathBuf::from(format!("/{}", "h".repeat(bytes - 1))) }

    #[test]
    fn docker_home_budget_names_the_byte_limit_and_actual_length() {
        // 107 usable bytes - "/isolation/" - UUID - "/broker/" - "egress-claude.sock".
        #[cfg(target_os = "linux")]
        assert_eq!(docker_home_budget(SESSION_ID_BYTES), 34);
        let budget = docker_home_budget(SESSION_ID_BYTES);
        assert!(check_docker_socket_paths(&home(budget), SESSION_ID_BYTES, Profile::DockerOffline).is_ok());
        let error = check_docker_socket_paths(&home(budget + 1), SESSION_ID_BYTES, Profile::DockerOpen).unwrap_err();
        let message = error.to_string();
        assert!(message.contains(&format!("DOXA_HOME is {} bytes", budget + 1)), "{message}");
        assert!(message.contains(&format!("at most {budget}")), "{message}");
        assert!(message.contains(&format!("{}-byte Unix socket path limit", unix_socket_path_bytes())), "{message}");
        assert!(!message.contains("SUN_LEN"));
        // A trailing separator does not change the bound socket path.
        let trailing = PathBuf::from(format!("{}/", home(budget).display()));
        assert!(check_docker_socket_paths(&trailing, SESSION_ID_BYTES, Profile::DockerOffline).is_ok());
    }

    #[test]
    fn admission_refuses_long_paths_before_creating_session_state() {
        let directory = tempfile::tempdir().unwrap();
        let home = directory.path().join("h".repeat(unix_socket_path_bytes()));
        let error = match crate::Runtime::prepare(&home, &"a".repeat(SESSION_ID_BYTES),
            directory.path(), Some(Profile::DockerOffline), false, None) {
            Ok(_) => panic!("oversized socket path admitted"),
            Err(error) => error,
        };
        assert!(error.to_string().contains("DOXA_HOME is"), "{error}");
        assert!(!home.exists());
        // UTF-8 paths consume bytes rather than character cells.
        let unicode = std::path::PathBuf::from(format!("/{}", "界".repeat(docker_home_budget(SESSION_ID_BYTES) / 2)));
        assert!(check_docker_socket_paths(&unicode, SESSION_ID_BYTES, Profile::DockerOpen).is_err());
    }

    #[test]
    fn budget_matches_the_longest_actual_bind_and_skips_non_host_sockets() {
        let id = "a".repeat(SESSION_ID_BYTES);
        let budget = docker_home_budget(SESSION_ID_BYTES);
        for (bytes, fits) in [(budget, true), (budget + 1, false)] {
            let socket = home(bytes).join("isolation").join(&id).join("broker").join("egress-claude.sock");
            let bound = std::os::unix::net::UnixListener::bind(&socket);
            let too_long = matches!(&bound, Err(error) if error.kind() == io::ErrorKind::InvalidInput);
            assert_eq!(!too_long, fits, "{} bytes", socket.as_os_str().len());
        }
        let long = home(budget + 20);
        assert!(check_docker_socket_paths(&long, SESSION_ID_BYTES, Profile::Native).is_ok());
        assert!(check_docker_socket_paths(&long, SESSION_ID_BYTES, Profile::DockerHardened).is_ok());
    }
}
