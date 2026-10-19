//! Fleet execution defaults are independent of the ordinary new-session default.
//! Network-capable and native roles require explicit owner acknowledgement.
use doxa_isolation::Profile;
use serde_json::{json, Value};
use std::io;

#[derive(Clone, Debug)]
pub struct Policy {
    pub workers: Profile,
    pub supervisor: Profile,
    pub confirm_native_workers: bool,
    pub confirm_native_supervisor: bool,
    pub allow_worker_internet: bool,
    pub allow_supervisor_internet: bool,
}
impl Default for Policy {
    fn default() -> Self {
        Self { workers: Profile::DockerOffline, supervisor: Profile::DockerOffline,
            confirm_native_workers: false, confirm_native_supervisor: false,
            allow_worker_internet: false, allow_supervisor_internet: false }
    }
}
pub fn task_internet(profile: Profile) -> bool {
    matches!(profile, Profile::Native | Profile::DockerOpen)
}
impl Policy {
    pub fn slot(&self, coordinator: bool) -> Profile {
        if coordinator { self.supervisor } else { self.workers }
    }
    pub fn required_confirmations(&self, has_supervisor: bool) -> Vec<&'static str> {
        let mut flags=Vec::new();
        if self.workers==Profile::Native { flags.push("--confirm-native-workers"); }
        if task_internet(self.workers) { flags.push("--allow-worker-internet"); }
        if has_supervisor {
            if self.supervisor==Profile::Native { flags.push("--confirm-native-supervisor"); }
            if task_internet(self.supervisor) { flags.push("--allow-supervisor-internet"); }
        }
        flags
    }
    pub fn require_confirmations(&self, has_supervisor: bool) -> io::Result<()> {
        for flag in self.required_confirmations(has_supervisor) {
            let accepted=match flag {
                "--confirm-native-workers"=>self.confirm_native_workers,
                "--confirm-native-supervisor"=>self.confirm_native_supervisor,
                "--allow-worker-internet"=>self.allow_worker_internet,
                "--allow-supervisor-internet"=>self.allow_supervisor_internet,
                _=>false,
            };
            if !accepted { return Err(io::Error::new(io::ErrorKind::PermissionDenied,
                format!("Fleet access requires explicit owner confirmation: {flag}"))); }
        }
        Ok(())
    }
    pub fn review(&self, has_supervisor: bool) -> Value {
        json!({"worker_isolation":self.workers.key(),
            "supervisor_isolation":if has_supervisor {Some(self.supervisor.key())} else {None},
            "worker_task_internet":task_internet(self.workers),
            "supervisor_task_internet":has_supervisor && task_internet(self.supervisor),
            "required_access_confirmations":self.required_confirmations(has_supervisor)})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn both_roles_default_to_docker_without_task_internet() {
        let policy=Policy::default();
        assert_eq!(policy.workers,Profile::DockerOffline);
        assert_eq!(policy.supervisor,Profile::DockerOffline);
        assert!(policy.required_confirmations(true).is_empty());
        assert!(!task_internet(policy.slot(false)));
        assert!(!task_internet(policy.slot(true)));
    }
    #[test]
    fn native_workers_require_both_host_and_network_acknowledgement() {
        let mut policy=Policy {workers:Profile::Native,..Default::default()};
        assert!(policy.require_confirmations(false).is_err());
        policy.confirm_native_workers=true;
        assert!(policy.require_confirmations(false).is_err());
        policy.allow_worker_internet=true;
        policy.require_confirmations(false).unwrap();
    }
    #[test]
    fn supervisor_opt_in_does_not_grant_worker_access() {
        let mut policy=Policy {workers:Profile::DockerOpen,supervisor:Profile::Native,
            confirm_native_supervisor:true,allow_supervisor_internet:true,..Default::default()};
        assert!(policy.require_confirmations(true).is_err());
        policy.allow_worker_internet=true;
        policy.require_confirmations(true).unwrap();
    }
}
