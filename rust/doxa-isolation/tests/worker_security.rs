#![cfg(all(target_os="linux",any(target_arch="x86_64",target_arch="aarch64")))]
use std::process::{Command,Stdio};

#[test]
fn kernel_filter_denies_project_mutation_and_high_bits_without_claiming_compat_execution() {
    let output = Command::new(env!("CARGO_BIN_EXE_doxa-isolation-worker"))
        .arg("filter-probe").stdin(Stdio::null()).output().unwrap();
    assert!(output.status.success(),"{}",String::from_utf8_lossy(&output.stderr));
    let proof: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(proof["contract"],doxa_isolation::worker_security::CONTRACT);
    assert_eq!(proof["quota_mutation_denied"],true);
    assert_eq!(proof["native_high_bits_denied"],true);
    assert_eq!(proof["compat_execution_verified"],false);
}
