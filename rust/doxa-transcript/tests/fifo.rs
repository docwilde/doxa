use doxa_transcript::TranscriptStore;
use serde_json::json;
use std::ffi::CString;
use std::fs::{self, OpenOptions};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{FileTypeExt, OpenOptionsExt};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

#[test]
fn append_paths_refuse_fifo_without_waiting_for_a_reader() {
    const CHILD_ROOT: &str = "DOXA_TRANSCRIPT_FIFO_TEST_ROOT";
    const CHILD_METHOD: &str = "DOXA_TRANSCRIPT_FIFO_TEST_METHOD";
    if let Some(root) = std::env::var_os(CHILD_ROOT) {
        let store = TranscriptStore::new(std::path::Path::new(&root), "project", "session-1").unwrap();
        let result = match std::env::var(CHILD_METHOD).unwrap().as_str() {
            "append" => store.append(json!({"type":"user"}), "claude", str::to_owned),
            "try_append" => store.try_append(json!({"type":"user"}), "codex",
                                             |text| Ok(text.to_owned())),
            "vendor" => store.try_append_vendor_turn("glm", "/fixture", "prompt", "answer",
                                                    "2026-10-11", |text| Ok(text.to_owned())),
            "router" => store.try_append_router_turn("/fixture", "prompt", "answer",
                                                    "2026-10-11", &json!({"id":"fixture"}),
                                                    |text| Ok(text.to_owned())),
            other => panic!("unexpected fixture method {other}"),
        };
        assert!(result.is_err(), "a FIFO must never be accepted as a transcript");
        return;
    }

    for method in ["append", "try_append", "vendor", "router"] {
        // With no reader, an ordinary write-open hangs before type validation.
        // With a reader, open succeeds but the regular-file check must refuse it.
        for reader_present in [false, true] {
            let temp = tempfile::tempdir().unwrap();
            let store = TranscriptStore::new(temp.path(), "project", "session-1").unwrap();
            let path = store.transcript_path();
            let c_path = CString::new(path.as_os_str().as_bytes()).unwrap();
            assert_eq!(unsafe { libc::mkfifo(c_path.as_ptr(), 0o600) }, 0);
            let _reader = reader_present.then(|| OpenOptions::new().read(true)
                .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(&path).unwrap());
            let mut child = Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "append_paths_refuse_fifo_without_waiting_for_a_reader"])
                .env(CHILD_ROOT, temp.path()).env(CHILD_METHOD, method)
                .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
                .spawn().unwrap();
            let deadline = Instant::now() + Duration::from_secs(2);
            let status = loop {
                if let Some(status) = child.try_wait().unwrap() { break status; }
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    panic!("{method} blocked opening a FIFO (reader_present={reader_present})");
                }
                std::thread::sleep(Duration::from_millis(5));
            };
            assert!(status.success(), "{method} failed its FIFO refusal assertion");
            assert!(fs::symlink_metadata(path).unwrap().file_type().is_fifo());
        }
    }
}
