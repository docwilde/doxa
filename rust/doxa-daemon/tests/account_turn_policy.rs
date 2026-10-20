//! Startup must not let a cleared environment remove a durable account policy.
use serde_json::json;
use std::{fs,os::unix::fs::PermissionsExt,process::Command};
#[test]
fn direct_daemon_resume_refuses_lost_account_environment_state_policy_and_identity_before_provider_start(){
    for scenario in ["missing_env","cleared_env","missing_state","missing_policy","both_missing","wrong_limit","wrong_session","wrong_model","pending","not_resume"] {
        let root=tempfile::tempdir().unwrap();fs::set_permissions(root.path(),fs::Permissions::from_mode(0o700)).unwrap();let home=root.path().join("home");fs::create_dir(&home).unwrap();fs::set_permissions(&home,fs::Permissions::from_mode(0o700)).unwrap();
        let account=home.join("account-turns");fs::create_dir(&account).unwrap();fs::set_permissions(&account,fs::Permissions::from_mode(0o700)).unwrap();
        let marker=root.path().join("provider-called");let provider=root.path().join("provider");
        fs::write(&provider,format!("#!/usr/bin/python3\nopen({:?},'w').write('unexpected')\nraise SystemExit(1)\n",marker.to_string_lossy())).unwrap();fs::set_permissions(&provider,fs::Permissions::from_mode(0o700)).unwrap();
        let mut original=json!({"version":1,"identity":{"session_id":"account-owned","engine":"codex","role":"coordinator","model":"gpt-fixture","cwd":root.path()},"limit":2,"used":0,"pending":false,"last_outcome":"new"});
        if scenario=="wrong_session"{original["identity"]["session_id"]=json!("other-session");}
        let mut record=original.clone();record["used"]=json!(1);record["last_outcome"]=json!("completed");
        if scenario=="pending"{record["pending"]=json!(true);record["last_outcome"]=json!("pending");}
        for (name,value,omit) in [("account-owned.json",record,scenario=="missing_state"||scenario=="both_missing"),("account-owned.policy.json",original,scenario=="missing_policy"||scenario=="both_missing")] {
            if !omit{let path=account.join(name);fs::write(&path,value.to_string()).unwrap();fs::set_permissions(&path,fs::Permissions::from_mode(0o600)).unwrap();}
        }
        let runtime=root.path().join("rt");let mut command=Command::new(env!("CARGO_BIN_EXE_doxa-daemon"));
        command.env_clear().env("PATH","/usr/bin:/bin").env("HOME",&home).env("DOXA_HOME",&home).env("DOXA_LORE","0").env("DOXA_WORKTREE","0")
            .args(["--engine","codex","--model",if scenario=="wrong_model"{"gpt-other"}else{"gpt-fixture"},"--session-id","account-owned","--isolation","native","--codex-bin"]).arg(&provider)
            .arg("--runtime-dir").arg(&runtime).arg("--cwd").arg(root.path());
        if scenario!="not_resume"{command.args(["--resume","true"]);}
        if scenario!="missing_env"{command.env("DOXA_ACCOUNT_TURN_LIMIT",if scenario=="cleared_env"{""}else if scenario=="wrong_limit"{"3"}else{"2"}).env("DOXA_ACCOUNT_TURN_SCOPE","fleet_supervisor");}
        let output=command.output().unwrap();assert!(!output.status.success(),"opened {scenario}");
        assert!(!marker.exists(),"provider was initialized for rejected {scenario}");
        assert!(String::from_utf8_lossy(&output.stderr).contains("account"),"{scenario}: {}",String::from_utf8_lossy(&output.stderr));
    }
}

#[test]
fn direct_daemon_new_account_refuses_untrusted_custom_home_before_provider_start(){
    for scenario in ["shared_parent","owner_alias"] {
        let root=tempfile::tempdir().unwrap();fs::set_permissions(root.path(),fs::Permissions::from_mode(0o700)).unwrap();let parent=root.path().join("owned");fs::create_dir(&parent).unwrap();fs::set_permissions(&parent,fs::Permissions::from_mode(0o700)).unwrap();
        let home=parent.join("home");fs::create_dir(&home).unwrap();fs::set_permissions(&home,fs::Permissions::from_mode(0o700)).unwrap();
        let exposed=if scenario=="shared_parent"{fs::set_permissions(&parent,fs::Permissions::from_mode(0o770)).unwrap();home.clone()}else{let alias=root.path().join("alias");std::os::unix::fs::symlink(&home,&alias).unwrap();alias};
        let marker=root.path().join("called");let provider=root.path().join("provider");fs::write(&provider,format!("#!/usr/bin/python3\nopen({:?},'w').write('unexpected')\n",marker.to_string_lossy())).unwrap();fs::set_permissions(&provider,fs::Permissions::from_mode(0o700)).unwrap();
        let output=Command::new(env!("CARGO_BIN_EXE_doxa-daemon")).env_clear().env("PATH","/usr/bin:/bin").env("HOME",&home).env("DOXA_HOME",&exposed).env("DOXA_LORE","0").env("DOXA_WORKTREE","0")
            .env("DOXA_ACCOUNT_TURN_LIMIT","2").env("DOXA_ACCOUNT_TURN_SCOPE","fleet_supervisor")
            .args(["--engine","codex","--model","gpt-fixture","--session-id","account-owned","--isolation","native","--codex-bin"]).arg(&provider)
            .arg("--runtime-dir").arg(root.path().join("rt")).arg("--cwd").arg(root.path()).output().unwrap();
        assert!(!output.status.success(),"opened {scenario}");assert!(!marker.exists(),"provider initialized for {scenario}");
        assert!(String::from_utf8_lossy(&output.stderr).contains("account"),"{scenario}: {}",String::from_utf8_lossy(&output.stderr));
        assert!(!home.join("account-turns").exists(),"untrusted path created account state for {scenario}");
    }
}
