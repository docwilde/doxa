//! Frontend preferences. Loaded at startup and after a deliberate settings save;
//! only the visible clock schedules a repaint, at its next displayed boundary.
use std::{collections::HashMap, io, path::Path, process::{Command,Stdio}, time::{Duration,SystemTime,UNIX_EPOCH}};
#[derive(Debug, Clone)]
pub struct Preferences { values: HashMap<&'static str,String> }
impl Default for Preferences { fn default() -> Self { Self::load() } }
impl Preferences {
    pub fn load() -> Self {
        let config = crate::settings::config_path().map(|p| doxa_state::load_config(&p)).unwrap_or_default();
        let values = crate::settings::SETTINGS.iter().map(|s| {
            let value = crate::settings::raw_from(&config,s,std::env::var(s.env).ok().as_deref(),"claude");
            (s.key,if value.is_empty() { s.default.into() } else { value })
        }).collect(); Self { values }
    }
    pub fn value(&self,key:&str)->&str { self.values.get(key).map(String::as_str).unwrap_or("") }
    #[cfg(test)]
    pub(crate) fn set_for_test(&mut self,key:&'static str,value:&str) { self.values.insert(key,value.into()); }
    pub fn on(&self,key:&str)->bool { let v=self.value(key); !v.is_empty() && !matches!(v.to_ascii_lowercase().as_str(),"0"|"false"|"off"|"no") }
    pub fn sidebar_width(&self)->u16 { self.value("sidebar_width").parse::<f64>().ok().filter(|v|v.is_finite()).unwrap_or(25.0).clamp(22.0,41.0) as u16 }
    pub fn clock_format(&self)->String {
        if !self.value("clock_format").is_empty() && validate_clock_format(self.value("clock_format")).is_ok() { return self.value("clock_format").into(); }
        let date=if self.on("clock_date") { "%Y-%m-%d " } else { "" };
        let hour=if self.value("clock_hour")=="12" { "%I:%M" } else { "%H:%M" };
        format!("{date}{hour}{}{}",if self.on("clock_seconds") { ":%S" } else { "" },if self.value("clock_hour")=="12" { " %p" } else { "" })
    }
    pub fn clock_period(&self)->u64 {
        let format=self.clock_format();
        if self.on("clock_seconds") || ["%S","%s","%T","%X","%r","%c","%+"].iter().any(|s|format.contains(s)) { 1 } else { 60 }
    }
    pub fn clock(&self,now:SystemTime)->String {
        if !self.on("clock_show") { return String::new(); }
        let seconds=now.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
        let mut cmd=Command::new("/usr/bin/date");
        cmd.arg(format!("--date=@{seconds}")).arg(format!("+{}",self.clock_format()));
        let tz=self.value("clock_tz");
        let valid_tz=tz.is_empty() || (Path::new(tz).components().all(|c|matches!(c,std::path::Component::Normal(_))) && Path::new("/usr/share/zoneinfo").join(tz).is_file());
        if !tz.is_empty() && valid_tz { cmd.env("TZ",tz); }
        let result=cmd.stdin(Stdio::null()).stderr(Stdio::null()).output().ok().filter(|o|o.status.success())
            .map(|o|String::from_utf8_lossy(&o.stdout).trim().chars().filter(|c|!c.is_control()).take(120).collect::<String>()).unwrap_or_default();
        if !valid_tz || (!self.value("clock_format").is_empty() && validate_clock_format(self.value("clock_format")).is_err()) { format!("{result} ⚠") } else { result }
    }
    pub fn clock_delay(&self,now:SystemTime)->Option<Duration> {
        if !self.on("clock_show") { return None; }
        let elapsed=now.duration_since(UNIX_EPOCH).unwrap_or_default();
        let period=self.clock_period();
        Some(Duration::from_secs(period-elapsed.as_secs()%period).saturating_sub(Duration::from_nanos(u64::from(elapsed.subsec_nanos()))))
    }
    pub fn should_notify(&self,trigger:&str,focused:bool)->bool {
        self.on(trigger) && match self.value("notify") { "off"=>false,"always"=>true,_=>!focused }
    }
}
pub fn validate_clock_format(value:&str)->io::Result<()> {
    // libc's strftime, as in the Python baseline: never treat format text as
    // shell code, and reject values which render no visible clock at all.
    if value.len()>4096 || value.chars().any(char::is_control) { return Err(io::Error::new(io::ErrorKind::InvalidInput,"invalid clock format")); }
    let format=std::ffi::CString::new(value).map_err(io::Error::other)?;
    let timestamp:libc::time_t=1_800_000_000;
    let mut tm=unsafe { std::mem::zeroed::<libc::tm>() };
    if unsafe { libc::localtime_r(&timestamp,&mut tm) }.is_null() { return Err(io::Error::other("clock unavailable")); }
    let mut bytes=vec![0_u8;8192];
    let count=unsafe { libc::strftime(bytes.as_mut_ptr().cast(),bytes.len(),format.as_ptr(),&tm) };
    if count==0 || String::from_utf8_lossy(&bytes[..count]).trim().is_empty() { return Err(io::Error::new(io::ErrorKind::InvalidInput,"empty clock format")); } Ok(())
}
pub fn notify(title:&str,body:&str) {
    // Event driven and bounded. No detached completion notification, no daemon
    // output on the terminal, and no synchronous wait on the UI thread.
    let title=title.chars().filter(|c|!c.is_control()).take(160).collect::<String>();
    let body=body.chars().filter(|c|!c.is_control()).take(500).collect::<String>();
    std::thread::spawn(move || {
        if let Ok(mut child)=Command::new("notify-send").args(["--app-name=DOXA","--",&title,&body]).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn() {
            let deadline=std::time::Instant::now()+Duration::from_secs(2);
            loop { match child.try_wait() { Ok(Some(_))|Err(_)=>break,_ if std::time::Instant::now()>=deadline=>{let _=child.kill();let _=child.wait();break;},_=>std::thread::sleep(Duration::from_millis(25)) } }
        }
    });
}

pub fn context_grid(detail:&serde_json::Value,ascii:bool)->Vec<String> {
    let Some(window)=detail["max_tokens"].as_u64().filter(|n|*n>0) else {return vec![];};
    let Some(categories)=detail["categories"].as_array().filter(|r|!r.is_empty()) else {return vec![];};
    let mut cells=Vec::new();let mut cumulative=0_u64;
    for category in categories.iter().take(64) {
        let Some(tokens)=category["tokens"].as_u64() else {continue;};
        cumulative=cumulative.saturating_add(tokens).min(window);
        let end=(u128::from(cumulative)*200/u128::from(window)) as usize;
        let used=!matches!(category["name"].as_str().unwrap_or("").to_ascii_lowercase().as_str(),"free space"|"free"|"available");
        cells.resize(end.min(200),used);
    }
    cells.resize(200,false);
    (0..10).map(|row|(0..20).map(|column| {
        if ascii {if cells[row*20+column] {"[#]"} else {"[ ]"}}
        else if !cells[row*20+column] {"⛶ "} else if (row+column)%2==0 {"⛀ "} else {"⛁ "}
    }).collect::<String>()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    fn preferences(values:&[(&'static str,&str)])->Preferences {
        let mut result=Preferences { values:crate::settings::SETTINGS.iter().map(|s|(s.key,s.default.into())).collect() };
        for (k,v) in values {result.values.insert(k,(*v).into());}result
    }
    #[test]
    fn clock_has_only_the_selected_display_boundary_and_off_has_none() {
        let now=UNIX_EPOCH+Duration::new(123,500_000_000);
        let default=preferences(&[]);assert_eq!(default.clock_delay(now),Some(Duration::from_millis(56500)));
        assert_eq!(default.clock_format(),"%H:%M");
        let seconds=preferences(&[("clock_seconds","1"),("clock_hour","12"),("clock_date","1")]);
        assert_eq!(seconds.clock_delay(now),Some(Duration::from_millis(500)));assert_eq!(seconds.clock_format(),"%Y-%m-%d %I:%M:%S %p");
        let custom=preferences(&[("clock_format","%H:%M:%S")]);assert_eq!(custom.clock_period(),1);
        let off=preferences(&[("clock_show","0")]);assert_eq!(off.clock_delay(now),None);assert!(off.clock(now).is_empty());
        assert!(validate_clock_format("%a %H:%M").is_ok());assert!(validate_clock_format("   ").is_err());
    }
    #[test]
    fn notification_triggers_obey_focus_master_and_opt_in() {
        let p=preferences(&[]);assert!(!p.should_notify("notify_needs_input",false));assert!(!p.should_notify("notify_staged",true));assert!(p.should_notify("notify_staged",false));
        let always=preferences(&[("notify","always"),("notify_needs_input","1")]);assert!(always.should_notify("notify_needs_input",true));
        let off=preferences(&[("notify","off"),("notify_needs_input","1")]);assert!(!off.should_notify("notify_needs_input",false));
    }
    #[test]
    fn measured_grid_floors_cumulative_shares_and_never_invents_a_window() {
        let detail=serde_json::json!({"max_tokens":1000,"categories":[{"name":"system","tokens":4},{"name":"messages","tokens":7},{"name":"free space","tokens":989}]});
        let grid=context_grid(&detail,true);assert_eq!(grid.len(),10);assert_eq!(grid.iter().map(|r|r.matches("[#]").count()).sum::<usize>(),2);
        assert!(context_grid(&serde_json::json!({"categories":[]}),false).is_empty());
        assert_eq!(context_grid(&detail,false).iter().flat_map(|s|s.chars()).filter(|c|matches!(c,'⛀'|'⛁')).count(),2);
    }
}

/// Pass this only to child processes. Leaving it unset preserves the owner's
/// inherited LORE_NOTIFY when the canonical LORE notification is selected.
pub fn lore_notify_override()->Option<&'static str> {
    let prefs=Preferences::load();if prefs.on("notify_staged") || !prefs.on("notify_lore") {Some("0")} else {None}
}
#[cfg(test)]
impl Preferences {pub(crate) fn set_test(&mut self,key:&'static str,value:&str){self.values.insert(key,value.into());}}
