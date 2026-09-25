//! Native, read-only fleet launch preflight. The Python fleet harness still
//! owns the live barrier, approval desk, budget enforcement and teardown.
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
    /// Worker count; a supervisor, when selected, adds one more session.
    pub sessions: u64,
    pub supervisor: Option<String>,
    pub root: PathBuf,
    pub run_id: String,
    pub run_budget_usd: Option<f64>,
    pub allow_unbudgeted: bool,
    pub force: bool,
    pub approve: String,
    pub approval_grace_s: f64,
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.into())
}

fn valid_supervisor(value: &str) -> bool {
    if value.contains(',') || value.chars().any(char::is_control) { return false; }
    let (body, weight) = value.split_once('@').unwrap_or((value, "1"));
    let engine = body.split_once(':').map_or(body, |(engine, _)| engine);
    let weight = if weight.trim().is_empty() { "1" } else { weight.trim() };
    !engine.trim().is_empty() && weight.parse::<f64>()
        .is_ok_and(|weight| weight.is_finite() && weight > 0.0)
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
    if !matches!(spec.approve.as_str(), "none" | "peer" | "all") {
        return Err(invalid("fleet approval policy must be none, peer, or all"));
    }
    if !spec.approval_grace_s.is_finite() || spec.approval_grace_s < 0.0 {
        return Err(invalid("fleet approval grace must be finite and nonnegative"));
    }
    if spec.supervisor.as_deref().is_some_and(|name| !valid_supervisor(name)) {
        return Err(invalid("fleet supervisor must name one engine[:model]"));
    }
    let count = spec.sessions.checked_add(u64::from(spec.supervisor.is_some()))
        .ok_or_else(|| invalid("fleet session count overflow"))?;
    let runtime = spec.root.join(&spec.run_id).join("rt");
    let used = runtime.as_os_str().as_encoded_bytes().len() + 1 + SOCKET_NAME_BUDGET;
    if used > SOCKET_PATH_MAX {
        return Err(invalid(format!("fleet run directory exceeds AF_UNIX socket path budget ({used}/{SOCKET_PATH_MAX} bytes)")));
    }
    let need = count.checked_mul(SESSION_RESIDENT_MB)
        .ok_or_else(|| invalid("fleet memory arithmetic overflow"))?;
    let mut lines = vec![format!("N={count} x ~{} MB/session = ~{:.1} GB resident",
        SESSION_RESIDENT_MB, need as f64 / 1024.0)];
    if let Some(supervisor) = &spec.supervisor {
        lines.push(format!("supervisor {supervisor} at slot 0, plus {} workers", spec.sessions));
    }
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
            lines.push(format!("run budget ${budget:.4} across N={count} = ${:.4} per session",
                budget / count as f64));
        }
        Some(_) => return Err(invalid("run budget must be finite")),
        None if spec.allow_unbudgeted => lines.push("no run budget; explicitly accepted by --allow-unbudgeted".into()),
        None => return Err(invalid("inbound peer turns require --run-budget or --allow-unbudgeted")),
    }
    lines.push(format!("socket path budget {used}/{SOCKET_PATH_MAX} bytes · runtime {}", runtime.display()));
    let policy = match spec.approve.as_str() {
        "all" => "every CLI tool permission ask; questions and spawns still require a human",
        "peer" => "only this run's peer tools",
        _ => "nothing",
    };
    lines.push(format!("approval posture: --approve {} auto-approves {policy}; unanswered asks are refused after {:.0}s",
        spec.approve, spec.approval_grace_s));
    lines.push("preflight only; provider price enforcement, live approvals, dispatch barrier and teardown remain in the Python fleet harness".into());
    Ok(lines.join("\n"))
}

pub fn parse(args: &[String], default_root: &Path) -> io::Result<Preflight> {
    let mut spec = Preflight { sessions: 4, supervisor: None, root: default_root.to_path_buf(),
        run_id: DEFAULT_RUN_ID_SHAPE.into(), run_budget_usd: None,
        allow_unbudgeted: false, force: false, approve: "none".into(), approval_grace_s: 300.0 };
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--sessions" | "--root" | "--run-id" | "--run-budget" | "--supervisor" | "--approve" | "--approval-grace" => {
                let option = args[index].as_str();
                index += 1;
                let value = args.get(index).ok_or_else(|| invalid(format!("missing value for {option}")))?;
                match option {
                    "--sessions" => spec.sessions = value.parse().map_err(|_| invalid("invalid fleet session count"))?,
                    "--root" => spec.root = PathBuf::from(value),
                    "--run-id" => spec.run_id = value.clone(),
                    "--run-budget" => spec.run_budget_usd = Some(value.parse().map_err(|_| invalid("invalid run budget"))?),
                    "--supervisor" => spec.supervisor = Some(value.clone()),
                    "--approve" => spec.approve = value.clone(),
                    "--approval-grace" => spec.approval_grace_s = value.parse().map_err(|_| invalid("invalid approval grace"))?,
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
        Preflight { sessions: 4, supervisor: None, root: PathBuf::from("/tmp/dx"),
            run_id: "20260925T100000-abcd".into(), run_budget_usd: Some(5.0),
            allow_unbudgeted: false, force: false, approve: "none".into(), approval_grace_s: 300.0 }
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
        let args = vec!["--sessions".into(), "3".into(), "--run-budget".into(), "6".into(),
            "--supervisor".into(), "claude:opus".into(), "--approve".into(), "peer".into(),
            "--approval-grace".into(), "15".into()];
        let spec = parse(&args, &root).unwrap();
        let note = check(&spec, Some(10_000)).unwrap();
        assert!(note.contains("$1.5000 per session"));
        assert!(note.contains("--approve peer"));
        assert!(!root.exists());
        assert!(parse(&["--approve".into(), "all".into()], &root).is_ok());
        assert!(parse(&["--unknown".into()], &root).is_err());
    }

    #[test]
    fn supervisor_counts_for_capacity_and_budget_without_waiving_safety() {
        let mut spec = plan();
        spec.supervisor = Some("claude:opus".into());
        let exact = 5 * SESSION_RESIDENT_MB + MEMORY_HEADROOM_MB;
        let note = check(&spec, Some(exact)).unwrap();
        assert!(note.contains("N=5"));
        assert!(note.contains("$1.0000 per session"));
        assert!(note.contains("supervisor claude:opus at slot 0, plus 4 workers"));
        assert!(check(&spec, Some(exact - 1)).is_err());
        spec.run_budget_usd = None;
        assert!(check(&spec, Some(exact)).is_err());
    }

    #[test]
    fn approval_posture_is_validated_and_reports_its_scope() {
        let mut spec = plan();
        spec.approve = "peer".into();
        spec.approval_grace_s = 0.0;
        let note = check(&spec, Some(10_000)).unwrap();
        assert!(note.contains("--approve peer auto-approves only this run's peer tools"));
        assert!(note.contains("refused after 0s"));
        spec.approve = "peeer".into();
        assert!(check(&spec, Some(10_000)).is_err());
        spec.approve = "all".into();
        spec.approval_grace_s = f64::INFINITY;
        assert!(check(&spec, Some(10_000)).is_err());
        spec.approval_grace_s = -1.0;
        assert!(check(&spec, Some(10_000)).is_err());
        spec.approval_grace_s = 0.0;
        spec.supervisor = Some("claude:opus,glm:glm-5".into());
        assert!(check(&spec, Some(10_000)).is_err());
        spec.supervisor = Some("claude:opus@0".into());
        assert!(check(&spec, Some(10_000)).is_err());
    }
}
