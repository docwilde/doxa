//! Explicit task-local rootless Engine smoke. This never uses a system Engine.
use doxa_isolation::{Profile, Runtime};
use std::{fs, path::Path, process::Command, os::unix::fs::PermissionsExt};

fn git(path:&Path,args:&[&str]){
    let output=Command::new("git").args(args).current_dir(path).env("GIT_CONFIG_NOSYSTEM","1")
        .env("GIT_CONFIG_GLOBAL","/dev/null").output().unwrap();
    assert!(output.status.success(),"{}",String::from_utf8_lossy(&output.stderr));
}
struct Container {host:String,id:String}
impl Drop for Container{
    fn drop(&mut self){
        let _=Command::new("docker").args(["--host",&self.host,"stop","--time","1",&self.id]).output();
        let _=Command::new("docker").args(["--host",&self.host,"rm",&self.id]).output();
    }
}
#[test]
#[ignore="requires explicit DOXA_ISOLATION_TEST_IMAGE and DOXA_ISOLATION_TEST_HOST task-local rootless Engine"]
fn rootless_mount_boundary_and_network_transition_resume(){
    let image=std::env::var("DOXA_ISOLATION_TEST_IMAGE").expect("explicit reviewed test image");
    let host=std::env::var("DOXA_ISOLATION_TEST_HOST").expect("explicit task-local test Engine");
    assert!(host.starts_with("unix:///run/user/"));assert!(!host.ends_with("/docker.sock"));
    let root=tempfile::tempdir().unwrap();let path=fs::canonicalize(root.path()).unwrap();
    let source=path.join("source");fs::create_dir(&source).unwrap();
    git(&source,&["init"]);fs::write(source.join("README"),"owned host checkout").unwrap();
    git(&source,&["add","README"]);git(&source,&["-c","user.name=Fixture","-c","user.email=fixture@example.invalid","commit","-m","feat: smoke"]);
    let home=path.join("home");fs::create_dir(&home).unwrap();fs::set_permissions(&home,fs::Permissions::from_mode(0o700)).unwrap();
    let config=format!("docker_image = {image:?}\ndocker_host = {host:?}\ndocker_memory_bytes = 1073741824\ndocker_cpus = 1.0\ndocker_pids = 128\n");
    fs::write(home.join("config.toml"),config).unwrap();
    let mut runtime=Runtime::prepare(&home,&format!("isolation-smoke-{}",std::process::id()),&source,Some(Profile::DockerOffline),false,None).unwrap();
    let container=Container{host:host.clone(),id:runtime.manifest().container_id.clone().unwrap()};
    let id=runtime.manifest().session_id.clone();let checkout=runtime.checkout().to_owned();
    let output=Command::new("docker").args(["--host",&host,"exec",&container.id,"sh","-c",
        "test ! -e /var/run/docker.sock && test ! -e /run/docker.sock && test ! -e /root/.doxa && test ! -e /root/.ssh && test ! -e /workspace/.git/objects/info/alternates && test -z \"$(git -C /workspace remote)\" && printf 'worker-only' > /workspace/worker-change"]).output().unwrap();
    assert!(output.status.success(),"{}",String::from_utf8_lossy(&output.stderr));
    let outside=Command::new("docker").args(["--host",&host,"exec",&container.id,"test","-e",source.to_str().unwrap()]).status().unwrap();
    assert!(!outside.success(),"original host checkout leaked into worker");
    assert!(!source.join("worker-change").exists());assert_eq!(fs::read_to_string(checkout.join("worker-change")).unwrap(),"worker-only");
    assert!(runtime.set_profile(Profile::DockerOpen,false).is_err());
    assert_eq!(runtime.set_profile(Profile::DockerOpen,true).unwrap()["profile"],"docker-open");
    assert_eq!(runtime.set_profile(Profile::DockerOffline,true).unwrap()["profile"],"docker-offline");
    runtime.stop().unwrap();drop(runtime);
    let mut resumed=Runtime::prepare(&home,&id,&checkout,None,true,None).unwrap();
    assert_eq!(resumed.manifest().container_id.as_deref(),Some(container.id.as_str()));
    assert_eq!(resumed.profile(),Profile::DockerOffline);
    assert_eq!(fs::read_to_string(checkout.join("worker-change")).unwrap(),"worker-only");
    resumed.stop().unwrap();
}
