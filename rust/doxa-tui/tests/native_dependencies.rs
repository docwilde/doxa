use std::{fs,os::unix::fs::PermissionsExt,process::Command};
#[test]
fn native_launch_and_doctor_work_from_another_directory_without_python_on_path() {
    let dir=tempfile::tempdir().unwrap();let bin=dir.path().join("bin with spaces");fs::create_dir(&bin).unwrap();
    let claude=bin.join("claude");let daemon=bin.join("daemon");let capture=dir.path().join("argv");
    for(path,body)in [(&claude,"#!/bin/sh\nexit 0\n"),(&daemon,"#!/bin/sh\nprintf '%s\n' \"$@\" > \"$DOXA_CAPTURE_ARGS\"\nexit 1\n")] {
        fs::write(path,body).unwrap();fs::set_permissions(path,fs::Permissions::from_mode(0o700)).unwrap();
    }
    let command=||{let mut command=Command::new(env!("CARGO_BIN_EXE_doxa-rs"));command.env("PATH",&bin).env("DOXA_DAEMON_BIN",&daemon).env("DOXA_CAPTURE_ARGS",&capture).env("DOXA_HOME",dir.path().join("home")).env("DOXA_RUNTIME_DIR",dir.path().join("runtime")).env_remove("DOXA_EFFORT").current_dir("/");command};
    let doctor=command().args(["doctor","--engine","claude"]).output().unwrap();assert!(doctor.status.success(),"{}",String::from_utf8_lossy(&doctor.stderr));assert!(String::from_utf8_lossy(&doctor.stdout).contains("ok claude:"));
    let launched=command().args(["new","--engine","claude"]).output().unwrap();assert!(!launched.status.success());
    let args=fs::read_to_string(&capture).unwrap();assert!(args.contains(&claude.to_string_lossy().to_string()));assert!(!args.contains("python"));assert!(!args.contains("--claude-script"));
    fs::remove_file(&capture).unwrap();
    let vendor=command().args(["new","--engine","deepseek"]).env("DEEPSEEK_API_KEY","fixture-vendor-key").output().unwrap();assert!(!vendor.status.success());
    assert!(!fs::read_to_string(capture).unwrap().contains("python"));
}
