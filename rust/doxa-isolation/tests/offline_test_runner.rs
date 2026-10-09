//! Explicit fixture for the owner-frozen fleet test runner on a real rootless Engine.
//! This exercises the container boundary, not the fleet daemon or signed receipt journal.
use doxa_isolation::{test_runner, Manifest, Policy, Profile};
use std::fs;

#[test]
#[ignore = "requires a task-local rootless Engine and an explicitly installed pinned test image"]
fn owner_frozen_test_runs_on_a_copied_read_only_offline_snapshot() {
    let image = std::env::var("DOXA_ISOLATION_TEST_IMAGE").expect("explicit pinned test image");
    let host = std::env::var("DOXA_ISOLATION_TEST_HOST").expect("explicit task-local rootless Engine");
    let uid = unsafe { libc::geteuid() };
    assert!(host.starts_with(&format!("unix:///run/user/{uid}/")));
    assert!(!host.ends_with("/docker.sock"));

    let fixture = tempfile::tempdir().unwrap();
    let original = fixture.path().join("original");
    fs::create_dir(&original).unwrap();
    let original_file = original.join("fixture.txt");
    fs::write(&original_file, "owner-frozen fixture\n").unwrap();
    let source = fixture.path().join("source");
    fs::create_dir(&source).unwrap();
    let file = source.join("fixture.txt");
    fs::copy(&original_file, &file).unwrap();
    let before = fs::read(&file).unwrap();
    let manifest = Manifest {
        version: 1,
        session_id: format!("offline-test-fixture-{}", std::process::id()),
        profile: Profile::DockerOffline,
        policy: Some(Policy {
            image,
            docker_host: host,
            memory_bytes: 1024 * 1024 * 1024,
            cpus: 1.0,
            pids: 128,
            disk_soft_limit_bytes: None,
            disk_free_floor_bytes: None,
        }),
        policy_hash: String::new(),
        source: original,
        checkout: fixture.path().join("checkout"),
        context_cwd: None,
        provider_rollout: None,
        creation_policy_hash: String::new(),
        checkout_device: 0,
        checkout_inode: 0,
        base_sha: String::new(),
        branch: String::new(),
        private_home: fixture.path().join("home"),
        cache: fixture.path().join("cache"),
        broker: fixture.path().join("broker"),
        container_id: None,
        nonce: String::new(),
        state: "ready".into(),
    };

    let read = test_runner::run_offline(
        &manifest,
        &source,
        &["/usr/bin/cat".into(), "/workspace/fixture.txt".into()],
        "",
        10,
    )
    .unwrap();
    assert!(read.passed);
    assert_eq!(read.exit_code, 0);
    assert_eq!(read.output_bytes, before.len() as u64);

    let write = test_runner::run_offline(
        &manifest,
        &source,
        &["/usr/bin/touch".into(), "/workspace/fixture.txt".into()],
        "",
        10,
    )
    .unwrap();
    assert!(!write.passed);
    assert_ne!(write.exit_code, 0);
    assert_eq!(fs::read(&file).unwrap(), before);
    assert_eq!(fs::read(&original_file).unwrap(), before);

    let network = test_runner::run_offline(
        &manifest,
        &source,
        &["/usr/bin/test".into(), "-e".into(), "/sys/class/net/eth0".into()],
        "",
        10,
    )
    .unwrap();
    assert!(!network.passed, "offline test unexpectedly has a network interface");

    let broker = test_runner::run_offline(
        &manifest,
        &source,
        &["/usr/bin/test".into(), "-e".into(), "/run/doxa/session".into()],
        "",
        10,
    )
    .unwrap();
    assert!(!broker.passed, "offline test unexpectedly has a worker broker mount");
}
