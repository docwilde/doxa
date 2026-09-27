//! Plain-launch restoration follows the saved strip, never discovery order.
use std::io;
use std::path::PathBuf;
use doxa_state::Tab;
use crate::{discovery::Session, history::{self, OfflineSession}, launch::{self, LaunchOptions}, ui_state::UiStateStore};

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
        if live.is_empty() { live.push(launch::spawn(options)?); }
        return Ok((live, store));
    };
    if saved.len() > 64 {
        return Err(io::Error::new(io::ErrorKind::Unsupported, "saved tabset exceeds the native 64-session restore bound; record retained"));
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
        if result.sessions.is_empty() {
            let fresh = launch::spawn(options)?;
            store.startup_extra_ids.push(fresh.id.clone());
            result.sessions.push(fresh);
        }
        store.startup_notice = format!("Restored {} live · {} resumed · {} read-only · {} unavailable",
            result.sessions.len().saturating_sub(result.resumed + store.startup_extra_ids.len()),
            result.resumed, result.archives.len(), result.skipped);
        store.startup_archives = result.archives;
    }
    Ok((result.sessions, store))
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

}
