//! Native, read-only fleet launch preflight. Live orchestration remains in
//! Python until the Rust daemon enforces spend and inbound peer turns.
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

const SESSION_RESIDENT_MB: u64 = 600;
const MEMORY_HEADROOM_MB: u64 = 2048;
const SOCKET_PATH_MAX: usize = 108;
const SOCKET_NAME_BUDGET: usize = 40;
const DEFAULT_RUN_ID_SHAPE: &str = "00000000T000000-0000";

#[derive(Debug, Clone)]
pub struct Preflight {
    pub sessions: u64,
    pub root: PathBuf,
    pub run_id: String,
    pub run_budget_usd: Option<f64>,
    pub allow_unbudgeted: bool,
    pub force: bool,
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

pub fn available_memory_mb() -> Option<u64> {
    fs::read_to_string("/proc/meminfo").ok()?.lines()
        .find_map(|line| line.strip_prefix("MemAvailable:")
            .and_then(|value| value.split_whitespace().next())
            .and_then(|value| value.parse::<u64>().ok()))
        .map(|kilobytes| kilobytes / 1024)
}

pub fn check(spec: &Preflight, available_mb: Option<u64>) -> io::Result<String> {
    if spec.sessions == 0 || spec.sessions > 100_000 {
        return Err(invalid("fleet session count must be between 1 and 100000"));
    }
    if !spec.root.is_absolute() || !doxa_state::valid_session_id(&spec.run_id) {
        return Err(invalid("fleet root must be absolute and run ID must be filename safe"));
    }
    let runtime = spec.root.join(&spec.run_id).join("rt");
    let used = runtime.as_os_str().as_encoded_bytes().len() + 1 + SOCKET_NAME_BUDGET;
    if used > SOCKET_PATH_MAX {
        return Err(invalid(format!("fleet run directory exceeds AF_UNIX socket path budget ({used}/{SOCKET_PATH_MAX} bytes)")));
    }
    let need = spec.sessions.checked_mul(SESSION_RESIDENT_MB)
        .ok_or_else(|| invalid("fleet memory arithmetic overflow"))?;
    let mut lines = vec![format!("N={} x ~{} MB/session = ~{:.1} GB resident", spec.sessions,
        SESSION_RESIDENT_MB, need as f64 / 1024.0)];
    if let Some(have) = available_mb {
        lines[0].push_str(&format!(", against ~{:.1} GB available (reserving {:.1} GB headroom)",
            have as f64 / 1024.0, MEMORY_HEADROOM_MB as f64 / 1024.0));
        if need.saturating_add(MEMORY_HEADROOM_MB) > have && !spec.force {
            return Err(invalid(format!("{} -- refusing to plan launch; lower session count or pass --force", lines[0])));
        }
    } else {
        lines[0].push_str("; available memory could not be measured");
    }
    if spec.force {
        lines.push("capacity arithmetic overridden by --force".into());
    }
    let budget = spec.run_budget_usd.filter(|value| *value > 0.0);
    match budget {
        Some(budget) if budget.is_finite() && budget > 0.0 => {
            lines.push(format!("run budget ${budget:.4} across N={} = ${:.4} per session",
                spec.sessions, budget / spec.sessions as f64));
        }
        Some(_) => return Err(invalid("run budget must be finite")),
        None if spec.allow_unbudgeted => lines.push("no run budget; explicitly accepted by --allow-unbudgeted".into()),
        None => return Err(invalid("inbound peer turns require --run-budget or --allow-unbudgeted")),
    }
    lines.push(format!("socket path budget {used}/{SOCKET_PATH_MAX} bytes · runtime {}", runtime.display()));
    lines.push("preflight only; provider price enforcement and live fleet approval are checked by the Python fleet harness".into());
    Ok(lines.join("\n"))
}

pub fn parse(args: &[String], default_root: &Path) -> io::Result<Preflight> {
    let mut spec = Preflight { sessions: 4, root: default_root.to_path_buf(),
        run_id: DEFAULT_RUN_ID_SHAPE.into(), run_budget_usd: None,
        allow_unbudgeted: false, force: false };
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--sessions" | "--root" | "--run-id" | "--run-budget" => {
                let option = args[index].as_str();
                index += 1;
                let value = args.get(index).ok_or_else(|| invalid(format!("missing value for {option}")))?;
                match option {
                    "--sessions" => spec.sessions = value.parse().map_err(|_| invalid("invalid fleet session count"))?,
                    "--root" => spec.root = PathBuf::from(value),
                    "--run-id" => spec.run_id = value.clone(),
                    "--run-budget" => spec.run_budget_usd = Some(value.parse().map_err(|_| invalid("invalid run budget"))?),
                    _ => unreachable!(),
                }
            }
            "--allow-unbudgeted" => spec.allow_unbudgeted = true,
            "--force" => spec.force = true,
            other => return Err(invalid(format!("unsupported fleet preflight option: {other}"))),
        }
        index += 1;
    }
    Ok(spec)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plan() -> Preflight {
        Preflight { sessions: 4, root: PathBuf::from("/tmp/dx"),
            run_id: "20260925T100000-abcd".into(), run_budget_usd: Some(5.0),
            allow_unbudgeted: false, force: false }
    }

    #[test]
    fn memory_budget_and_socket_checks_match_python_guard_boundaries() {
        let plan = plan();
        let exact = 4 * SESSION_RESIDENT_MB + MEMORY_HEADROOM_MB;
        assert!(check(&plan, Some(exact)).unwrap().contains("$1.2500 per session"));
        assert!(check(&plan, Some(exact - 1)).is_err());
        let mut forced = plan.clone();
        forced.force = true;
        assert!(check(&forced, Some(1)).unwrap().contains("overridden"));
        forced.run_budget_usd = None;
        assert!(check(&forced, Some(1)).is_err(), "force never waives the spend guard");
        forced.allow_unbudgeted = true;
        assert!(check(&forced, Some(1)).unwrap().contains("explicitly accepted"));
        forced.root = PathBuf::from(format!("/tmp/{}", "x".repeat(100)));
        assert!(check(&forced, Some(1)).is_err());
    }

    #[test]
    fn preflight_parses_without_creating_any_run_state() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("fleet");
        let args = vec!["--sessions".into(), "3".into(), "--run-budget".into(), "6".into()];
        let spec = parse(&args, &root).unwrap();
        assert!(check(&spec, Some(10_000)).unwrap().contains("$2.0000 per session"));
        assert!(!root.exists());
        assert!(parse(&["--approve".into(), "all".into()], &root).is_err());
    }
}
