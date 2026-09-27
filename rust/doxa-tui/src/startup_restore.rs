//! Plain-launch restoration follows the saved strip, never discovery order.
use std::io;
use std::path::PathBuf;
use doxa_state::Tab;
use crate::{discovery::Session, history::{self, OfflineSession}, launch::{self, LaunchOptions}, ui_state::UiStateStore};

/// One reserved usable tab beside a full readonly restore. Manual additions
/// keep their normal256 limit; persisted startup layouts may have257 identities.
pub const MAX_STARTUP_TABS:usize=crate::ui::panes::MAX_TABS+1;

#[derive(Clone, Debug)]
pub struct Archive { pub entry: OfflineSession, pub note: String }

#[derive(Default)]
struct Planned { sessions: Vec<Session>, archives: Vec<Archive>, resumed: usize, skipped: usize }

fn plan(
    saved: &[Tab], live: &[Session], resume: bool,
    mut read: impl FnMut(&Tab) -> Option<OfflineSession>,
    mut start: impl FnMut(&OfflineSession) -> Result<Session, String>,
) -> Planned {
    let mut result = Planned::default();
    for tab in saved {
        if let Some(session) = live.iter().find(|session| session.id == tab.session_id) {
            result.sessions.push(session.clone());
            continue;
        }
        let Some(entry) = read(tab) else { result.skipped += 1; continue; };
        let note = if resume {
            match start(&entry) {
                Ok(session) => { result.sessions.push(session); result.resumed += 1; continue; }
                Err(why) => format!("not resumed — {why}"),
            }
        } else { String::new() };
        result.archives.push(Archive { entry, note });
    }
    result
}

/// Eager resume loads verified provider history without submitting a prompt.
/// The provider and saved model come from resume_plan, never the window defaults.
pub fn prepare(
    mut store: Option<UiStateStore>, mut live: Vec<Session>, options: &LaunchOptions,
    restore: bool, resume: bool,
) -> io::Result<(Vec<Session>, Option<UiStateStore>)> {
    live.sort_by(|a,b| b.started_at.cmp(&a.started_at).then_with(|| a.id.cmp(&b.id)));
    let saved = restore.then(|| store.as_ref().and_then(UiStateStore::saved_tabs).map(<[Tab]>::to_vec)).flatten();
    let Some(saved) = saved else {
        if !restore { if let Some(store) = &mut store { store.discard_loaded_layout(); } }
        live.truncate(1);
        if live.is_empty() { if let Some(session)=try_start_fresh(store.as_mut(),||launch::spawn(options))? {live.push(session);} }
        return Ok((live, store));
    };
    if saved.len() > MAX_STARTUP_TABS || (saved.len() > crate::ui::panes::MAX_TABS && store.as_ref().is_none_or(|store|store.startup_overflow_id.is_none())) {
        return Err(io::Error::new(io::ErrorKind::Unsupported, "saved tabset exceeds the native 256-tab restore bound plus one reserved startup tab; record retained"));
    }
    let python = options.lore_python.clone().or_else(|| std::env::var_os("DOXA_LORE_PYTHON").map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from("python3"));
    let launch_cwd = std::env::current_dir()?;
    let mut result = plan(&saved, &live, resume,
        |tab| history::saved_session(&tab.session_id,
            tab.cwd.as_deref().map(std::path::Path::new).unwrap_or(&launch_cwd), &python),
        |entry| {
            // Discovery is a hint. Recheck immediately before starting a daemon.
            if let Ok(rows) = crate::discovery::sessions() {
                if let Some(session) = rows.into_iter().find(|session| session.id == entry.id) { return Ok(session); }
            }
            let mut verified = history::resume_plan(entry, &python).map_err(str::to_owned)?;
            verified.lore_python = options.lore_python.clone();
            verified.codex_bin = options.codex_bin.clone();
            verified.claude_python = options.claude_python.clone();
            verified.claude_script = options.claude_script.clone();
            launch::spawn(&verified).map_err(|error| error.to_string())
        });
    if let Some(store) = &mut store {
        ensure_usable(&mut result, store, ||launch::spawn(options))?;
        let failure=std::mem::take(&mut store.startup_notice);
        store.startup_notice = format!("Restored {} live · {} resumed · {} read-only · {} unavailable",
            result.sessions.len().saturating_sub(result.resumed + store.startup_extra_ids.len()),
            result.resumed, result.archives.len(), result.skipped);
        if !failure.is_empty() {store.startup_notice.push_str(" · ");store.startup_notice.push_str(&failure);}
        else if result.sessions.is_empty() { store.startup_notice.push_str(" · reserved fresh slot occupied; detach a tab to make room for a new session"); }
        store.startup_archives = result.archives;
    }
    Ok((result.sessions, store))
}

fn try_start_fresh(store:Option<&mut UiStateStore>,mut spawn:impl FnMut()->io::Result<Session>)->io::Result<Option<Session>> {
    match spawn() {
        Ok(session)=>Ok(Some(session)),
        Err(error)=>match store {
            Some(store)=>{store.startup_failed=true;store.startup_notice="Fresh session could not start · /setup checks authentication and dependencies; use /engine to retry after setup".into();Ok(None)}
            None=>Err(error),
        }
    }
}

fn ensure_usable(result:&mut Planned,store:&mut UiStateStore,mut spawn:impl FnMut()->io::Result<Session>)->io::Result<()> {
    if result.sessions.is_empty() && result.archives.len()<MAX_STARTUP_TABS {
        if let Some(fresh)=try_start_fresh(Some(&mut *store),&mut spawn)? {
            if result.archives.len()==crate::ui::panes::MAX_TABS {store.startup_overflow_id=Some(fresh.id.clone());}
            store.startup_extra_ids.push(fresh.id.clone());result.sessions.push(fresh);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn tab(id:&str)->Tab { Tab { session_id:id.into(), pinned_name:None, cwd:Some("/project".into()) } }
    fn live(id:&str)->Session { Session { id:id.into(),title:id.into(),socket:PathBuf::from("/unused"),scope_key:"/project".into(),clients:None,started_at:"2026".into() } }
    fn archive(id:&str)->OfflineSession { OfflineSession { id:id.into(),project:"project".into(),markdown:"saved conversation".into(),search_snippets:vec![],cwd:Some("/project".into()) } }
    #[test]
    fn mixed_saved_order_resumes_only_verified_archives() {
        let saved=vec![tab("old-a"),tab("live-b"),tab("old-c"),tab("missing")];
        let mut calls=vec![];
        let p=plan(&saved,&[live("extra"),live("live-b")],true,
            |t| (t.session_id!="missing").then(||archive(&t.session_id)),
            |e| { calls.push(e.id.clone()); if e.id=="old-a" {Ok(live(&e.id))}else{Err("provider history unavailable".into())} });
        assert_eq!(p.sessions.iter().map(|s|s.id.as_str()).collect::<Vec<_>>(),vec!["old-a","live-b"]);
        assert_eq!(calls,vec!["old-a","old-c"]);
        assert_eq!(p.archives[0].entry.id,"old-c");
        assert!(p.archives[0].note.contains("provider history unavailable"));
        assert_eq!((p.resumed,p.skipped),(1,1));
    }
    #[test]
    fn resume_off_keeps_transcripts_and_never_starts_provider() {
        let p=plan(&[tab("ended"),tab("live")],&[live("live")],false,
            |t|Some(archive(&t.session_id)), |_|panic!("resume off started a process"));
        assert_eq!(p.archives[0].entry.id,"ended");
        assert!(p.archives[0].note.is_empty());
        assert_eq!(p.sessions[0].id,"live");
    }
    #[test]
    fn restore_off_uses_newest_live_only_and_does_not_resume_saved_tabs() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = UiStateStore::new(dir.path(), "/project", "machine").unwrap();
        let mut app = crate::ui::App::default();
        app.groups[0].tabs.push("ended".into());
        store.save(&app).unwrap();
        let before = std::fs::read(store.path()).unwrap();
        let mut old=live("old");old.started_at="2024".into();
        let (sessions,store)=prepare(Some(store),vec![old,live("new")],&LaunchOptions::default(),false,true).unwrap();
        assert_eq!(sessions.iter().map(|s|s.id.as_str()).collect::<Vec<_>>(),vec!["new"]);
        let store=store.unwrap();
        assert!(store.saved_tabs().is_none());
        assert_eq!(std::fs::read(store.path()).unwrap(),before);
    }

    #[test]
    fn full_readonly_restore_reserves_one_fresh_slot_and_reloads_257() {
        let dir=tempfile::tempdir().unwrap();
        let mut store=UiStateStore::new(dir.path(),"/project","machine").unwrap();
        let saved:Vec<_>=(0..crate::ui::panes::MAX_TABS).map(|index|tab(&format!("saved-{index}"))).collect();
        let mut original=crate::ui::App::default();
        original.groups[0].tabs=saved.iter().map(|tab|tab.session_id.clone()).collect();original.groups[0].active=128;
        original.rail_width=31;original.rail_visible=true;
        original.custom_names.insert("saved-128".into(),"saved focus".into());
        store.save(&original).unwrap();
        let mut result=plan(&saved,&[],false,|tab|Some(archive(&tab.session_id)),|_|panic!("provider resume not permitted"));
        let mut spawns=0;
        ensure_usable(&mut result,&mut store,||{spawns+=1;Ok(live("fresh"))}).unwrap();
        assert_eq!(spawns,1);assert_eq!(result.archives.len(),256);assert_eq!(result.sessions.len(),1);
        assert_eq!(store.startup_overflow_id.as_deref(),Some("fresh"));
        store.startup_archives=result.archives.clone();
        let mut restored=crate::ui::App::default();
        assert!(store.restore(&mut restored,&["fresh".into()]));
        assert_eq!(restored.groups[0].tabs.len(),MAX_STARTUP_TABS);assert_eq!(restored.groups[0].active,128);
        assert!(store.save_if_complete(&restored,&std::sync::Mutex::new(true)).unwrap());
        let mut reloaded=UiStateStore::new(dir.path(),"/project","machine").unwrap();
        assert_eq!(reloaded.saved_tabs().unwrap().len(),MAX_STARTUP_TABS);
        assert_eq!(reloaded.startup_overflow_id.as_deref(),Some("fresh"));
        reloaded.startup_archives=result.archives;
        let mut again=crate::ui::App::default();assert!(reloaded.restore(&mut again,&["fresh".into()]));
        assert_eq!(again.groups[0].tabs,restored.groups[0].tabs);assert_eq!(again.groups[0].active,128);
        assert_eq!(again.custom_names.get("saved-128"),Some(&"saved focus".into()));
        assert_eq!((again.rail_visible,again.rail_width),(true,31));
        assert!(reloaded.save_if_complete(&again,&std::sync::Mutex::new(true)).unwrap());
        assert_eq!(crate::ui::panes::MAX_TABS,256); // Manual capacity unchanged.
    }

    #[test]
    fn failed_fresh_start_keeps_empty_setup_window_without_phantom_identity() {
        let mut store=UiStateStore::transient("/project");
        let session=try_start_fresh(Some(&mut store),||Err(io::Error::other("provider unavailable secret-output"))).unwrap();
        assert!(session.is_none());assert!(store.startup_failed);assert!(store.startup_notice.contains("/setup"));assert!(store.startup_notice.contains("/engine"));
        assert!(!store.startup_notice.contains("secret-output"));
        let mut app=crate::ui::App::default();assert!(!store.restore(&mut app,&[]));
        assert!(app.sessions.is_empty());assert!(app.groups.iter().all(|group|group.tabs.is_empty()));
        assert!(app.notice.contains("/setup"));assert!(store.save(&app).is_err());
        assert!(try_start_fresh(None,||Err(io::Error::other("precise explicit command failure"))).unwrap_err().to_string().contains("precise"));
    }

}
