//! Canonical native remote authorization. Transport attestation is independent
//! of the claimed HTTP identity; loopback addresses never attest a proxy.
use std::{collections::BTreeSet, path::PathBuf};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Decision { pub allowed: bool, pub reason: String }
impl Decision {
    fn allow(reason: impl Into<String>) -> Self { Self { allowed:true,reason:reason.into() } }
    fn deny(reason: impl Into<String>) -> Self { Self { allowed:false,reason:reason.into() } }
}
pub fn setting(key: &str, env: &str) -> String {
    let home = std::env::var_os("DOXA_HOME").filter(|v| !v.is_empty()).map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".doxa"));
    let config = doxa_state::load_config(&home.join("config.toml"));
    doxa_state::raw_setting(std::env::var(env).ok().as_deref(),&config,key)
}
fn enabled(value: &str) -> bool { !value.trim().is_empty() && !matches!(value.trim().to_ascii_lowercase().as_str(),"0"|"false"|"no"|"off") }
pub fn remote_enabled() -> bool { enabled(&setting("remote_enabled","DOXA_REMOTE_ENABLED")) }
pub fn listening(enabled: bool) -> Decision {
    if enabled { Decision::allow("remote listening is enabled") } else { Decision::deny("remote listening is OFF by default (DOXA_REMOTE_ENABLED)") }
}
pub fn identity(login: Option<&str>, attested_proxy: bool, allow_list: &BTreeSet<String>) -> Decision {
    if !attested_proxy { return Decision::deny("identity header ignored: the Unix peer is not the attested Tailscale proxy"); }
    let Some(login) = login.map(str::trim).filter(|s| !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control)) else { return Decision::deny("no valid identity presented"); };
    if allow_list.is_empty() { return Decision::deny("the remote allow-list is empty; everyone is refused"); }
    if !allow_list.contains(&login.to_lowercase()) { return Decision::deny("identity is not on the remote allow-list (DOXA_REMOTE_ALLOWED_LOGINS)"); }
    Decision::allow("identity is on the remote allow-list")
}
pub fn request_kind(kind: &str, shell_opt_in: bool, bypass_opt_in: bool, target_mode: Option<&str>) -> Decision {
    match kind {
        "read_transcript"|"read_status"|"send_prompt"|"approve_tool"|"deny_tool" => Decision::allow(format!("{kind} is on the reduced remote surface")),
        "shell_bang" if shell_opt_in => Decision::allow("remote shell opt-in is enabled"),
        "shell_bang" => Decision::deny("remote shell is refused by default"),
        "set_permission_mode" => match target_mode {
            None|Some("") => Decision::deny("permission mode request has no target mode"),
            Some("bypassPermissions") if !bypass_opt_in => Decision::deny("remote bypass mode is refused by default"),
            _ => Decision::allow("requested remote permission mode is permitted; session arming still applies"),
        },
        _ => Decision::deny("unrecognized remote request kind"),
    }
}
pub fn evaluate(kind: &str, login: Option<&str>, attested_proxy: bool, target_mode: Option<&str>) -> Decision {
    let decision = listening(remote_enabled()); if !decision.allowed { return decision; }
    let allowed = setting("remote_allowed_logins","DOXA_REMOTE_ALLOWED_LOGINS").split(',').map(|s|s.trim().to_lowercase()).filter(|s|!s.is_empty()).collect();
    let decision = identity(login,attested_proxy,&allowed); if !decision.allowed { return decision; }
    request_kind(kind,enabled(&setting("remote_allow_shell","DOXA_REMOTE_ALLOW_SHELL")),enabled(&setting("remote_allow_bypass","DOXA_REMOTE_ALLOW_BYPASS")),target_mode)
}
pub fn proxy_uid() -> Option<u32> {
    let raw = setting("remote_proxy_uid","DOXA_REMOTE_PROXY_UID");
    let uid = if raw.trim().is_empty() { 0 } else { raw.trim().parse().ok()? };
    let own = unsafe { libc::geteuid() };
    (own == 0 || uid != own).then_some(uid)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn attestation_empty_allowlist_and_reduced_surface_are_independent_gates() {
        let allow = BTreeSet::from(["owner@example.com".into()]);
        assert!(!identity(Some("owner@example.com"),false,&allow).allowed);
        assert!(!identity(Some("owner@example.com"),true,&BTreeSet::new()).allowed);
        assert!(identity(Some("OWNER@example.com"),true,&allow).allowed);
        assert!(!listening(false).allowed); assert!(listening(true).allowed);
        assert!(!request_kind("shell_bang",false,false,None).allowed);
        assert!(request_kind("shell_bang",true,false,None).allowed);
        assert!(!request_kind("set_permission_mode",true,false,Some("bypassPermissions")).allowed);
        assert!(request_kind("set_permission_mode",false,false,Some("plan")).allowed);
        assert!(!request_kind("set_permission_mode",true,true,None).allowed);
        assert!(!request_kind("unknown",true,true,None).allowed);
    }
}
