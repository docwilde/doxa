#![cfg(unix)]

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use doxa_engines::codex_driver::{valid_thread_id, CodexCliDriver, DriverError, DriverOptions, SandboxMode};
use doxa_engines::EngineEvent;
use tempfile::TempDir;
use tokio_util::sync::CancellationToken;

fn fake_script(dir: &Path, body: &str) -> PathBuf {
    let path = dir.join("fake-codex");
    fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
    let mut perms = fs::metadata(&path).unwrap().permissions();
    perms.set_mode(0o700);
    fs::set_permissions(&path, perms).unwrap();
    path
}

fn options(dir: &TempDir, executable: PathBuf) -> DriverOptions {
    let mut options = DriverOptions::new(dir.path().to_path_buf());
    options.executable = executable;
    options.turn_timeout = Duration::from_secs(5);
    options
}

async fn events(driver: &mut CodexCliDriver, prompt: &str) -> (Result<doxa_engines::codex_driver::TurnOutcome, DriverError>, Vec<EngineEvent>) {
    let mut out = Vec::new();
    let result = driver.run_turn(prompt, &CancellationToken::new(), |event| out.push(event)).await;
    (result, out)
}

#[tokio::test]
async fn fake_cli_receives_stdin_and_resumes_with_safe_argv() {
    let dir = TempDir::new().unwrap();
    let args_path = dir.path().join("args.txt");
    let prompt_path = dir.path().join("prompt.txt");
    let script = fake_script(dir.path(), &format!(
        "printf '%s\\n' \"$@\" >> '{}'\necho END >> '{}'\ncat > '{}'\nprintf '%s\\n' '{{\"type\":\"thread.started\",\"thread_id\":\"thread_1\"}}' '{{\"type\":\"item.completed\",\"item\":{{\"type\":\"agent_message\",\"text\":\"done\"}}}}' '{{\"type\":\"turn.completed\",\"usage\":{{\"input_tokens\":2}}}}'",
        args_path.display(), args_path.display(), prompt_path.display(),
    ));
    let mut opts = options(&dir, script);
    opts.model = Some("gpt-test".into());
    opts.sandbox = SandboxMode::ReadOnly;
    let mut driver = CodexCliDriver::new(opts, str::to_owned);
    let prompt = "secret prompt; $(touch /tmp/doxa-should-never-execute)";
    let (result, first) = events(&mut driver, prompt).await;
    let result = result.unwrap();
    assert_eq!(result.thread_id.as_deref(), Some("thread_1"));
    assert_eq!(result.usage.input_tokens, 2);
    assert_eq!(fs::read_to_string(&prompt_path).unwrap(), prompt);
    assert!(!Path::new("/tmp/doxa-should-never-execute").exists());
    assert_eq!(first.iter().map(|e| e.kind.as_str()).collect::<Vec<_>>(), ["text_delta", "turn_done"]);
    let (_, second) = events(&mut driver, "second").await;
    assert_eq!(second.last().unwrap().data["num_turns"], 2);
    let args = fs::read_to_string(&args_path).unwrap();
    let groups: Vec<&str> = args.split("END\n").collect();
    assert!(groups[0].starts_with("exec\n--json\n"));
    assert!(groups[1].starts_with("exec\nresume\nthread_1\n--json\n"));
    assert!(groups[0].contains("approval_policy=\"never\"\n"));
    assert!(groups[0].contains("sandbox_mode=\"read-only\"\n"));
    assert!(groups[0].contains("-m\ngpt-test\n-\n"));
    assert!(!args.contains(prompt));
}

#[tokio::test]
async fn failure_drains_large_stderr_and_emits_one_failed_turn() {
    let dir = TempDir::new().unwrap();
    let script = fake_script(dir.path(), "head -c 100000 /dev/zero | tr '\\000' X >&2\necho 'bad auth' >&2\nexit 17");
    let mut driver = CodexCliDriver::new(options(&dir, script), str::to_owned);
    let (result, out) = events(&mut driver, "hi").await;
    assert_eq!(result.unwrap().exit_code, Some(17));
    assert_eq!(out.iter().filter(|event| event.kind == "turn_done").count(), 1);
    assert_eq!(out.last().unwrap().data["is_error"], true);
    assert!(out.last().unwrap().data["error"].as_str().unwrap().contains("bad auth"));
    assert!(out.last().unwrap().data["error"].as_str().unwrap().len() <= 64 * 1024);
}

#[tokio::test]
async fn cancellation_and_timeout_reap_fake_process() {
    let dir = TempDir::new().unwrap();
    let marker = dir.path().join("surviving-child.txt");
    let script = fake_script(dir.path(), &format!("sh -c 'sleep 1; echo leaked > {}' & wait", marker.display()));
    let mut driver = CodexCliDriver::new(options(&dir, script.clone()), str::to_owned);
    let cancel = CancellationToken::new();
    let cancel_later = cancel.clone();
    tokio::spawn(async move { tokio::time::sleep(Duration::from_millis(100)).await; cancel_later.cancel(); });
    let mut output = Vec::new();
    let result = tokio::time::timeout(Duration::from_secs(3), driver.run_turn("hi", &cancel, |event| output.push(event))).await.unwrap();
    assert!(matches!(result, Err(DriverError::Cancelled)));
    assert!(output.iter().all(|event| event.kind != "turn_done"));

    let mut opts = options(&dir, script);
    opts.turn_timeout = Duration::from_millis(100);
    let mut driver = CodexCliDriver::new(opts, str::to_owned);
    let (_, output) = tokio::time::timeout(Duration::from_secs(3), events(&mut driver, "hi")).await.unwrap();
    assert_eq!(output.last().unwrap().kind, "turn_done");
    assert_eq!(output.last().unwrap().data["is_error"], true);
    tokio::time::sleep(Duration::from_millis(1200)).await;
    assert!(!marker.exists(), "a subprocess survived group cancellation");
}

#[test]
fn resume_rejects_missing_and_unsafe_thread_ids() {
    let mut options = DriverOptions::new(PathBuf::from("/tmp"));
    options.require_resume = true;
    assert!(matches!(options.argv(None), Err(DriverError::MissingResumeThread)));
    for id in ["-C", "../other", "a b", "", "a\n--dangerously-bypass-approvals"] {
        assert!(!valid_thread_id(id));
        assert!(matches!(options.argv(Some(id)), Err(DriverError::InvalidThreadId)));
    }
    assert!(valid_thread_id("01990abc-def0-7abc-8def-0123456789ab"));
}

#[tokio::test]
async fn provider_supplied_unsafe_thread_id_cannot_be_resumed() {
    let dir = TempDir::new().unwrap();
    let script = fake_script(dir.path(), "cat >/dev/null\necho '{\"type\":\"thread.started\",\"thread_id\":\"--unsafe\"}'");
    let mut driver = CodexCliDriver::new(options(&dir, script), str::to_owned);
    let (result, _) = events(&mut driver, "first").await;
    assert!(result.unwrap().thread_id.is_none());
    let (result, _) = events(&mut driver, "second").await;
    assert!(matches!(result, Err(DriverError::MissingResumeThread)));
}

#[tokio::test]
async fn signaled_process_is_a_failed_turn() {
    let dir = TempDir::new().unwrap();
    let script = fake_script(dir.path(), "kill -TERM $$");
    let mut driver = CodexCliDriver::new(options(&dir, script), str::to_owned);
    let (_, output) = events(&mut driver, "hi").await;
    assert_eq!(output.last().unwrap().data["is_error"], true);
    assert!(output.last().unwrap().data["error"].as_str().unwrap().contains("signal"));
}

#[tokio::test]
async fn drains_stdout_while_writing_prompt_larger_than_pipe_capacity() {
    let dir = TempDir::new().unwrap();
    let received = dir.path().join("prompt-bytes.txt");
    let script = fake_script(dir.path(), &format!(
        "i=0\nwhile [ $i -lt 5000 ]; do echo '{{\"type\":\"future.event\"}}'; i=$((i+1)); done\nwc -c > '{}'\necho '{{\"type\":\"item.completed\",\"item\":{{\"type\":\"agent_message\",\"text\":\"received\"}}}}'",
        received.display(),
    ));
    let mut opts = options(&dir, script);
    opts.turn_timeout = Duration::from_secs(3);
    let mut driver = CodexCliDriver::new(opts, str::to_owned);
    let prompt = "P".repeat(256 * 1024);
    let (result, output) = tokio::time::timeout(Duration::from_secs(5), events(&mut driver, &prompt)).await.unwrap();
    assert!(result.is_ok());
    assert_eq!(fs::read_to_string(&received).unwrap().trim(), prompt.len().to_string());
    assert_eq!(output.iter().filter(|event| event.kind == "turn_done").count(), 1);
    assert_eq!(output.last().unwrap().data["is_error"], false);
    assert_eq!(output[0].data["text"], "received");
}

#[tokio::test]
async fn terminal_error_while_stdin_is_blocked_emits_one_turn_done() {
    let dir = TempDir::new().unwrap();
    let script = fake_script(dir.path(), "echo '{\"type\":\"turn.failed\",\"message\":\"fixture-secret denied\"}'\nsleep 30");
    let mut driver = CodexCliDriver::new(options(&dir, script), |text| text.replace("fixture-secret", "[redacted]"));
    let prompt = "P".repeat(256 * 1024);
    let mut output = Vec::new();
    let result = tokio::time::timeout(
        Duration::from_secs(3),
        driver.run_turn(&prompt, &CancellationToken::new(), |event| output.push(event)),
    ).await.unwrap();
    assert!(result.is_ok());
    assert_eq!(output.iter().filter(|event| event.kind == "turn_done").count(), 1);
    assert_eq!(output.last().unwrap().data["is_error"], true);
    assert_eq!(output.last().unwrap().data["error"], "[redacted] denied");
}
