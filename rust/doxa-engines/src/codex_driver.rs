//! Bounded `codex exec --json` process driver. The process is isolated in a
//! new POSIX session so cancellation/timeouts reap its process group.
//! Authentication, MCP registration, transcript persistence and prices are
//! intentionally outside this first driver slice.

use std::io;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::{Duration, Instant};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::process::Command;
use tokio::time::{sleep, sleep_until, timeout, Instant as TokioInstant};
use tokio_util::sync::CancellationToken;

use crate::codex::{duration_ms, CodexJsonlNormalizer, ParseError, TokenUsage, MAX_LINE_BYTES};
use crate::EngineEvent;

const STDERR_TAIL_BYTES: usize = 64 * 1024;
const STDERR_WAIT: Duration = Duration::from_secs(5);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SandboxMode {
    ReadOnly,
    WorkspaceWrite,
    DangerFullAccess,
}

impl SandboxMode {
    fn config_value(self) -> &'static str {
        match self {
            Self::ReadOnly => "read-only",
            Self::WorkspaceWrite => "workspace-write",
            Self::DangerFullAccess => "danger-full-access",
        }
    }
}

#[derive(Clone, Debug)]
pub struct DriverOptions {
    pub executable: PathBuf,
    pub cwd: PathBuf,
    pub model: Option<String>,
    pub sandbox: SandboxMode,
    pub turn_timeout: Duration,
    /// A previously recorded Codex thread ID for an explicit resume.
    pub resume_thread: Option<String>,
    /// Refuse to create a new thread if the requested resume ID is absent.
    pub require_resume: bool,
}

impl DriverOptions {
    pub fn new(cwd: PathBuf) -> Self {
        Self {
            executable: PathBuf::from("codex"),
            cwd,
            model: None,
            sandbox: SandboxMode::WorkspaceWrite,
            turn_timeout: Duration::from_secs(3600),
            resume_thread: None,
            require_resume: false,
        }
    }

    /// The same shape is used for first turns and `exec resume`. Every
    /// argument is passed as a distinct OS string; the prompt is stdin.
    pub fn argv(&self, thread_id: Option<&str>) -> Result<Vec<String>, DriverError> {
        if self.require_resume && thread_id.is_none() {
            return Err(DriverError::MissingResumeThread);
        }
        let mut args = vec!["exec".to_owned()];
        if let Some(id) = thread_id {
            if !valid_thread_id(id) {
                return Err(DriverError::InvalidThreadId);
            }
            args.extend(["resume".to_owned(), id.to_owned()]);
        }
        args.extend([
            "--json".to_owned(),
            "--skip-git-repo-check".to_owned(),
            "-c".to_owned(),
            "approval_policy=\"never\"".to_owned(),
            "-c".to_owned(),
            format!("sandbox_mode=\"{}\"", self.sandbox.config_value()),
        ]);
        if let Some(model) = &self.model {
            args.extend(["-m".to_owned(), model.clone()]);
        }
        args.push("-".to_owned());
        Ok(args)
    }
}

/// Prevent a value from a provider frame or state file becoming a CLI option
/// or path-like token. Codex IDs are UUIDs today; this allows their safe
/// ASCII token subset without claiming to validate an account's existence.
pub fn valid_thread_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 128
        && !id.starts_with('-')
        && id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

#[derive(Debug)]
pub enum DriverError {
    MissingResumeThread,
    InvalidThreadId,
    Spawn(io::Error),
    Cancelled,
}

#[derive(Clone, Debug)]
pub struct TurnOutcome {
    pub thread_id: Option<String>,
    pub usage: TokenUsage,
    pub exit_code: Option<i32>,
}

pub struct CodexCliDriver {
    options: DriverOptions,
    normalizer: CodexJsonlNormalizer,
    turns_finished: u64,
}

impl CodexCliDriver {
    pub fn new(
        options: DriverOptions,
        scrub: impl Fn(&str) -> String + Send + Sync + 'static,
    ) -> Self {
        Self {
            options,
            normalizer: CodexJsonlNormalizer::new(scrub),
            turns_finished: 0,
        }
    }

    pub fn thread_id(&self) -> Option<&str> {
        self.normalizer
            .thread_id()
            .or(self.options.resume_thread.as_deref())
            .filter(|id| valid_thread_id(id))
    }

    pub async fn run_turn(
        &mut self,
        prompt: &str,
        cancel: &CancellationToken,
        emit: impl FnMut(EngineEvent),
    ) -> Result<TurnOutcome, DriverError> {
        self.run_turn_with_thread(prompt, cancel, emit, |_| {})
            .await
    }

    /// Persist the thread identity as soon as Codex announces it. A daemon
    /// can be stopped mid-turn, before any assistant text or terminal event.
    pub async fn run_turn_with_thread(
        &mut self,
        prompt: &str,
        cancel: &CancellationToken,
        mut emit: impl FnMut(EngineEvent),
        mut on_thread: impl FnMut(&str),
    ) -> Result<TurnOutcome, DriverError> {
        // Once a first turn ran, silently starting a fresh thread would
        // make the DOXA session look resumed when it is not.
        let thread = self.thread_id().map(str::to_owned);
        if self.turns_finished > 0 && thread.is_none() {
            return Err(DriverError::MissingResumeThread);
        }
        let argv = self.options.argv(thread.as_deref())?;
        let mut announced = thread;
        let started = Instant::now();
        let deadline = TokioInstant::now() + self.options.turn_timeout;
        let mut command = Command::new(&self.options.executable);
        command
            .args(&argv)
            .current_dir(&self.options.cwd)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        #[cfg(unix)]
        unsafe {
            command.as_std_mut().pre_exec(|| {
                if libc::setsid() == -1 {
                    Err(io::Error::last_os_error())
                } else {
                    Ok(())
                }
            });
        }
        // A CLI upgrade can briefly hold its executable open for writing.
        // Linux reports ETXTBSY during that window. Retry a few times within
        // the turn deadline; every other spawn failure remains immediate.
        let mut busy_retries = 0;
        let mut child = loop {
            match command.spawn() {
                Ok(child) => break child,
                Err(error) if error.raw_os_error() == Some(libc::ETXTBSY) && busy_retries < 3 => {
                    busy_retries += 1;
                    tokio::select! {
                        _ = sleep(Duration::from_millis(30)) => {},
                        _ = cancel.cancelled() => return Err(DriverError::Cancelled),
                        _ = sleep_until(deadline) => return Err(DriverError::Spawn(error)),
                    }
                }
                Err(error) => return Err(DriverError::Spawn(error)),
            }
        };
        let mut group = ProcessGroupGuard::new(child.id());
        self.normalizer.begin_turn();
        let stderr = child.stderr.take().expect("piped stderr");
        let mut stderr_task = tokio::spawn(drain_stderr_tail(stderr));
        // The CLI may fill stdout before reading stdin. Write in a separate
        // task and drain stdout at the same time; awaiting write_all first
        // deadlocks when both pipes fill (notably with a large LORE preamble).
        let mut stdin = child.stdin.take().expect("piped stdin");
        let prompt_bytes = prompt.as_bytes().to_vec();
        let mut stdin_task = tokio::spawn(async move {
            stdin.write_all(&prompt_bytes).await?;
            stdin.shutdown().await
        });
        let mut stdin_complete = false;
        let mut failure: Option<String> = None;
        let mut cancelled = false;
        let mut exit_code = None;
        let mut signaled = false;
        let mut stdout = child.stdout.take().expect("piped stdout");
        let mut chunk = [0u8; 8192];
        loop {
            tokio::select! {
                read = stdout.read(&mut chunk) => {
                    match read {
                        Ok(0) => break,
                        Ok(n) => match self.normalizer.push_bytes(&chunk[..n]) {
                            Ok(events) => {
                                if let Some(id) = self.normalizer.thread_id().filter(|id| valid_thread_id(id)) {
                                    if announced.as_deref() != Some(id) {
                                        on_thread(id);
                                        announced = Some(id.to_owned());
                                    }
                                }
                                for event in events { emit(event); }
                                if self.normalizer.is_closed() { break; }
                            }
                            Err(ParseError::LineTooLong) => {
                                failure = Some(format!("one stdout event exceeded the {MAX_LINE_BYTES}-byte read limit; the rest of the turn could not be read"));
                                break;
                            }
                        },
                        Err(err) => { failure = Some(format!("stdout read failed: {err}")); break; }
                    }
                }
                result = &mut stdin_task, if !stdin_complete => {
                    stdin_complete = true;
                    match result {
                        Ok(Ok(())) => {},
                        Ok(Err(err)) => { failure = Some(format!("stdin write failed: {err}")); break; }
                        Err(err) => { failure = Some(format!("stdin task failed: {err}")); break; }
                    }
                }
                _ = cancel.cancelled() => { cancelled = true; break; }
                _ = sleep_until(deadline) => { failure = Some("the turn ran past its time limit and the process was killed".into()); break; }
            }
        }
        // EOF can arrive while a large prompt is still being consumed.
        // A successful turn requires both streams to have finished.
        if failure.is_none() && !cancelled && !self.normalizer.is_closed() && !stdin_complete {
            tokio::select! {
                result = &mut stdin_task => {
                    stdin_complete = true;
                    match result {
                        Ok(Ok(())) => {},
                        Ok(Err(err)) => failure = Some(format!("stdin write failed: {err}")),
                        Err(err) => failure = Some(format!("stdin task failed: {err}")),
                    }
                }
                _ = cancel.cancelled() => { cancelled = true; }
                _ = sleep_until(deadline) => { failure = Some("the turn ran past its time limit and the process was killed".into()); }
            }
        }
        if !stdin_complete {
            stdin_task.abort();
        }
        if failure.is_none() && !cancelled && !self.normalizer.is_closed() {
            tokio::select! {
                status = child.wait() => {
                    match status {
                        Ok(status) => { exit_code = status.code(); signaled = !status.success() && status.code().is_none(); group.disarm(); }
                        Err(err) => failure = Some(format!("process wait failed: {err}")),
                    }
                }
                _ = cancel.cancelled() => { cancelled = true; }
                _ = sleep_until(deadline) => { failure = Some("the turn ran past its time limit and the process was killed".into()); }
            }
        }
        if cancelled || failure.is_some() || self.normalizer.is_closed() {
            group.kill();
            let _ = child.wait().await;
        }
        if cancelled {
            stderr_task.abort();
            return Err(DriverError::Cancelled);
        }
        if signaled {
            failure = Some("exec terminated by signal".into());
        }
        if failure.is_none()
            && !self.normalizer.is_closed()
            && exit_code.is_some_and(|code| code != 0)
        {
            failure = Some(format!("exec exited {}", exit_code.unwrap_or_default()));
        }
        if failure.is_some() || exit_code.is_some_and(|code| code != 0) {
            if let Ok(Ok(Ok(tail))) = timeout(STDERR_WAIT, &mut stderr_task).await {
                if !tail.is_empty() && !signaled {
                    let text = String::from_utf8_lossy(&tail);
                    failure = Some(
                        text.chars()
                            .rev()
                            .take(280)
                            .collect::<String>()
                            .chars()
                            .rev()
                            .collect(),
                    );
                }
            }
            if !stderr_task.is_finished() {
                stderr_task.abort();
            }
        } else {
            stderr_task.abort();
        }
        for event in self
            .normalizer
            .finish_turn(Some(duration_ms(started.elapsed())), failure.as_deref())
        {
            emit(event);
        }
        self.turns_finished += 1;
        Ok(TurnOutcome {
            thread_id: self.thread_id().map(str::to_owned),
            usage: self.normalizer.usage().clone(),
            exit_code,
        })
    }
}

async fn drain_stderr_tail(mut stderr: tokio::process::ChildStderr) -> io::Result<Vec<u8>> {
    let mut tail = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        let n = stderr.read(&mut chunk).await?;
        if n == 0 {
            break;
        }
        tail.extend_from_slice(&chunk[..n]);
        if tail.len() > STDERR_TAIL_BYTES {
            tail.drain(..tail.len() - STDERR_TAIL_BYTES);
        }
    }
    Ok(tail)
}

struct ProcessGroupGuard {
    pid: Option<u32>,
}

impl ProcessGroupGuard {
    fn new(pid: Option<u32>) -> Self {
        Self { pid }
    }
    fn disarm(&mut self) {
        self.pid = None;
    }
    fn kill(&mut self) {
        if let Some(pid) = self.pid.take() {
            #[cfg(unix)]
            unsafe {
                libc::kill(-(pid as i32), libc::SIGKILL);
            }
        }
    }
}

impl Drop for ProcessGroupGuard {
    fn drop(&mut self) {
        self.kill();
    }
}
