//! Native Claude Code transport and isolated authentication/artifact snapshots.
pub mod cli;
pub mod isolation;
pub use cli::{Cli, CliOptions};
use std::io;
#[derive(Debug)]
pub enum Error {
    Io(io::Error),
    Timeout,
    Closed,
    Oversize,
    Protocol,
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(_) => write!(f, "Claude CLI I/O failed"),
            Self::Timeout => write!(f, "Claude CLI operation timed out"),
            Self::Closed => write!(f, "Claude CLI closed"),
            Self::Oversize => write!(f, "Claude CLI frame exceeds 1 MiB"),
            Self::Protocol => write!(f, "invalid Claude CLI protocol frame"),
        }
    }
}
impl std::error::Error for Error {}
impl From<io::Error> for Error {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}
