use doxa_isolation::{create_args, manifest_path, read_manifest, validate_inspect, Manifest, Policy, Profile, Runtime};
use serde_json::{json, Value};
use std::{fs, os::unix::fs::{MetadataExt,PermissionsExt}, path::Path};

fn policy()->Policy{Policy{image:format!("sha256:{}","a".repeat(64)),docker_host:"unix:///run/user/1000/test.sock".into(),memory_bytes:512*1024*1024,cpus:1.5,pids:128,disk_soft_limit_bytes:Some(20*1024*1024*1024),disk_free_floor_bytes:Some(2*1024*1024*1024)}}
fn manifest(root:&Path)->Manifest{
    let root=fs::canonicalize(root).unwrap();let root=root.as_path();
    let dirs=["checkout","home","cache","broker"];
    for directory in dirs{fs::create_dir(root.join(directory)).unwrap();fs::set_permissions(root.join(directory),fs::Permissions::from_mode(0o700)).unwrap();}
    let meta=fs::metadata(root.join("checkout")).unwrap();
    Manifest{version:1,session_id:"session".into(),profile:Profile::DockerOpen,policy:Some(policy()),policy_hash:"hash".into(),
        creation_policy_hash:"created".into(),context_cwd:None,provider_rollout:None,source:root.into(),checkout:root.join("checkout"),checkout_device:meta.dev(),checkout_inode:meta.ino(),
        base_sha:"a".repeat(40),branch:"doxa/session".into(),private_home:root.join("home"),cache:root.join("cache"),broker:root.join("broker"),
        container_id:Some("b".repeat(64)),nonce:"c".repeat(48),state:"ready".into()}
}
fn inspection(manifest:&Manifest)->Value{
    let policy=manifest.policy.as_ref().unwrap();
    json!({"Id":manifest.container_id,"Config":{"Image":policy.image,"User":"0:0",
        "Labels":{"doxa.session":manifest.session_id,"doxa.nonce":manifest.nonce,"doxa.policy":manifest.creation_policy_hash}},
        "HostConfig":{"Privileged":false,"ReadonlyRootfs":true,"Memory":policy.memory_bytes,"MemorySwap":policy.memory_bytes,
            "NanoCpus":(policy.cpus*1_000_000_000.0) as u64,"PidsLimit":policy.pids,"PidMode":"","IpcMode":"private",
            "CapDrop":["ALL"],"CapAdd":null,"SecurityOpt":["no-new-privileges:true"],"Devices":[]},
        "Mounts":[{"Type":"bind","Source":manifest.checkout,"Destination":"/workspace","RW":true},
            {"Type":"bind","Source":manifest.private_home,"Destination":"/home/doxa","RW":true},
            {"Type":"bind","Source":manifest.cache,"Destination":"/work-cache","RW":true},
            {"Type":"bind","Source":manifest.broker,"Destination":"/run/doxa/session","RW":false}]})
}
#[test]
fn unsupported_profiles_and_unpinned_images_fail_closed(){
    for value in ["docker","hardened","docker-host","native "]{assert!(Profile::parse(value).is_err());}
    let mut value=policy();value.validate().unwrap();
    for image in ["ubuntu:latest","ubuntu@sha256:abcd","-evil@sha256:abc"]{value.image=image.into();assert!(value.validate().is_err());}
    value=policy();value.docker_host="tcp://localhost:2375".into();assert!(value.validate().is_err());
    value.docker_host="unix:///var/run/docker.sock".into();assert!(value.validate().is_err());
    value=policy();value.cpus=f64::NAN;assert!(value.validate().is_err());
    value=policy();value.pids=0;assert!(value.validate().is_err());
    value=policy();value.disk_soft_limit_bytes=Some(0);assert!(value.validate().is_err());
    value=policy();value.disk_free_floor_bytes=Some(0);assert!(value.validate().is_err());
}
#[test]
fn disk_status_shows_sample_and_monitored_limits(){
    let root=tempfile::tempdir().unwrap();fs::set_permissions(root.path(),fs::Permissions::from_mode(0o700)).unwrap();let value=manifest(root.path());
    fs::write(value.cache.join("sample"),vec![b'x';8192]).unwrap();
    let status=value.status();
    assert!(status["disk_usage_bytes"].as_u64().is_some_and(|n|n>=8192),"{status}");
    assert!(status["disk_free_bytes"].as_u64().unwrap()>0);
    assert_eq!(status["disk_soft_limit_bytes"],20*1024*1024*1024_u64);
    assert!(status["disk_limit"].as_str().unwrap().contains("no hard filesystem quota"));
}
#[test]
fn docker_launch_contains_only_four_private_mounts_and_enforced_controls(){
    // CI uses .doxa-tests as its temporary parent; a substring in an owned
    // private path is not a mount of the user's DOXA store.
    let root=tempfile::Builder::new().prefix(".doxa-tests-").tempdir().unwrap();let value=manifest(root.path());
    let args=create_args(&value).unwrap().into_iter().map(|v|v.to_string_lossy().into_owned()).collect::<Vec<_>>();
    assert_eq!(args.iter().filter(|arg|arg.as_str()=="--mount").count(),4);
    assert!(args.iter().any(|arg|arg.ends_with("dst=/run/doxa/session,readonly")));
    assert!(args.contains(&"--cap-drop=ALL".into()));assert!(args.contains(&"--read-only".into()));
    assert!(args.contains(&"--cgroupns=private".into()));
    assert!(args.contains(&"--pids-limit".into()));assert!(args.contains(&"--memory-swap".into()));
    assert!(args.contains(&"--security-opt=no-new-privileges:true".into()));
    let mounts=args.windows(2).filter(|pair|pair[0]=="--mount").map(|pair|pair[1].clone()).collect::<Vec<_>>();
    assert_eq!(mounts,vec![
        format!("type=bind,src={},dst=/workspace",value.checkout.display()),
        format!("type=bind,src={},dst=/home/doxa",value.private_home.display()),
        format!("type=bind,src={},dst=/work-cache",value.cache.display()),
        format!("type=bind,src={},dst=/run/doxa/session,readonly",value.broker.display()),
    ],"only the four session-private paths may enter the worker");
    assert!(!args.contains(&"--privileged".into()));assert!(!args.contains(&"host".into()));
}
#[test]
fn inspection_rejects_extra_mounts_host_namespaces_and_weakened_limits(){
    let root=tempfile::tempdir().unwrap();let value=manifest(root.path());let baseline=inspection(&value);
    validate_inspect(&value,&baseline).unwrap();
    for (field,replacement) in [("Privileged",json!(true)),("ReadonlyRootfs",json!(false)),("Memory",json!(0)),
        ("NanoCpus",json!(0)),("PidsLimit",json!(-1)),("PidMode",json!("host")),("IpcMode",json!("host")),
        ("CapDrop",json!([])),("CapAdd",json!(["SYS_ADMIN"])),("Devices",json!([{"PathOnHost":"/dev/sda"}])),
        ("CgroupnsMode",json!("host"))]{
        let mut actual=baseline.clone();actual["HostConfig"][field]=replacement;assert!(validate_inspect(&value,&actual).is_err(),"{field}");
    }
    let mut actual=baseline.clone();actual["Mounts"].as_array_mut().unwrap().push(json!({"Type":"bind","Source":"/home","Destination":"/host","RW":true}));
    assert!(validate_inspect(&value,&actual).is_err());
    let mut actual=baseline.clone();actual["Config"]["Labels"]["doxa.nonce"]=json!("other");assert!(validate_inspect(&value,&actual).is_err());
}
#[test]
fn immutable_creation_label_survives_current_network_profile_change(){
    let root=tempfile::tempdir().unwrap();let mut value=manifest(root.path());let baseline=inspection(&value);
    value.profile=Profile::DockerOffline;value.policy_hash="changed".into();
    validate_inspect(&value,&baseline).unwrap();
    let mut changed=baseline;changed["Config"]["Labels"]["doxa.policy"]=json!("changed");
    assert!(validate_inspect(&value,&changed).is_err());
}
#[test]
fn mount_replacement_and_symlink_components_are_refused(){
    let root=tempfile::tempdir().unwrap();let value=manifest(root.path());let baseline=inspection(&value);
    fs::rename(&value.checkout,root.path().join("original")).unwrap();
    fs::create_dir(&value.checkout).unwrap();fs::set_permissions(&value.checkout,fs::Permissions::from_mode(0o700)).unwrap();
    assert!(validate_inspect(&value,&baseline).is_err());
    fs::remove_dir(&value.checkout).unwrap();std::os::unix::fs::symlink(root.path().join("original"),&value.checkout).unwrap();
    assert!(create_args(&value).is_err());assert!(validate_inspect(&value,&baseline).is_err());
}
#[test]
fn native_resume_requires_original_manifest_and_never_relabels_a_running_session(){
    let root=tempfile::tempdir().unwrap();let canonical=fs::canonicalize(root.path()).unwrap();let home=canonical.join("home");fs::create_dir(&home).unwrap();
    let workspace=canonical.join("project");fs::create_dir(&workspace).unwrap();
    let mut runtime=Runtime::prepare(&home,"session",&workspace,Some(Profile::Native),false,None).unwrap();
    assert_eq!(runtime.status()["profile"],"native");
    assert!(runtime.set_profile(Profile::DockerOpen,false).is_err());
    assert!(runtime.set_profile(Profile::DockerOpen,true).is_err());
    runtime.stop().unwrap();drop(runtime);
    let runtime=Runtime::prepare(&home,"session",&workspace,None,true,None).unwrap();
    assert_eq!(runtime.status()["state"],"ready");
    assert!(Runtime::prepare(&home,"session",&workspace,Some(Profile::DockerOpen),true,None).is_err());
    assert!(Runtime::prepare(&home,"session",&workspace,None,false,None).is_err());
    let path=manifest_path(&home,"session").unwrap();
    fs::set_permissions(&path,fs::Permissions::from_mode(0o644)).unwrap();
    assert!(read_manifest(&path).is_err());
    assert!(manifest_path(&home,"../other").is_err());
}
