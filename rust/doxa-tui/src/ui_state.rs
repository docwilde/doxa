//! Python-compatible tabset projection for the two-pane Rust frontend.
//!
//! Callers provide the live IDs from daemon discovery. A saved ID alone is
//! never evidence that its daemon is still attachable. Unsupported multi-pane
//! geometry is readable as flat tabs but deliberately not rewritten.
use std::collections::HashSet;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use doxa_state::{
    load_tabset, resolve_tabset_path, save_tabset, tabset_path, valid_session_id, Tab, TabSet,
};
use serde_json::{json, Value};

use crate::ui::{App, PaneGroup, Split};
use crate::ui::panes::{Tree, MAX_DEPTH, MAX_PANES, MAX_TABS};
use crate::collections::{self, Collection};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LayoutSignature {
    groups: Vec<(Vec<String>, usize)>,
    pane_tree: Option<Tree>,
    fleet_views: Vec<crate::ui::fleet_menu::SavedView>,
    custom_names: Vec<(String, String)>,
    default_names: Vec<(String, String)>,
    killed: Vec<String>,
    active_group: usize,
    split: Split,
    split_percent: u16,
    rail_visible: bool,
    rail_width: u16,
    collections: Vec<Collection>,
}
impl LayoutSignature {
    pub fn capture(app: &App) -> Self {
        Self {
            groups: app.groups.iter().map(|g| {
                let selected=g.tabs.get(g.active);
                let tabs=g.tabs.iter().filter(|id|!crate::remote_client::valid_target(id)).cloned().collect::<Vec<_>>();
                let active=selected.and_then(|id|tabs.iter().position(|tab|tab==id)).unwrap_or(0);
                (tabs,active)
            }).collect(),
            pane_tree: app.pane_tree.clone(),
            killed: { let mut ids: Vec<_> = app.killed_this_run.iter().cloned().collect(); ids.sort(); ids },
            fleet_views: app.fleet_views.clone(),
            custom_names: {
                let mut names: Vec<_> = app.custom_names.iter().filter(|(id,_)|!crate::remote_client::valid_target(id))
                    .map(|(id, name)| (id.clone(), name.clone())).collect();
                names.sort();
                names
            },
            default_names: {
                let mut names: Vec<_> = app.default_names.iter().filter(|(id,_)|!crate::remote_client::valid_target(id))
                    .map(|(id, name)| (id.clone(), name.clone())).collect();
                names.sort();
                names
            },
            active_group: if app.groups.get(app.active_group).is_some_and(|g|g.tabs.iter().any(|id|!crate::remote_client::valid_target(id))){
                app.active_group
            }else{app.groups.iter().position(|g|g.tabs.iter().any(|id|!crate::remote_client::valid_target(id))).unwrap_or(0)},
            split: app.split,
            split_percent: app.split_percent,
            rail_visible: app.rail_visible,
            rail_width: app.rail_width,
            collections: app.collections.clone(),
        }
    }
}

pub struct UiStateStore {
    path: PathBuf,
    scope_key: String,
    record: Option<TabSet>,
    writable_layout: bool,
    pub(crate) startup_archives: Vec<crate::startup_restore::Archive>,
    pub(crate) startup_extra_ids: Vec<String>,
    pub(crate) startup_notice: String,
    pub(crate) startup_failed: bool,
    pub(crate) startup_error: Option<String>,
    pub(crate) startup_overflow_id: Option<String>,
}

impl UiStateStore {
    /// Keep startup controls usable when a durable tabset could not be opened.
    /// The transient store never writes; it only carries a startup notice.
    pub fn transient(scope_key:&str)->Self {
        Self {path:PathBuf::new(),scope_key:scope_key.into(),record:None,writable_layout:false,
            startup_archives:Vec::new(),startup_extra_ids:Vec::new(),startup_notice:String::new(),startup_failed:false,startup_error:None,startup_overflow_id:None}
    }
    /// A clear may finalize its old daemon only when replacing the tab can
    /// be persisted from a complete live view of a representable layout.
    pub fn clear_preflight(&self, app: &App, complete: &Mutex<bool>) -> Result<(), &'static str> {
        if !*complete.lock().map_err(|_| "live roster guard unavailable")? {
            return Err("live roster incomplete");
        }
        if self.path.as_os_str().is_empty() {return Err("persistent tabset unavailable");}
        if !self.writable_layout { return Err("saved layout exceeds supported pane bounds"); }
        if app.has_offline_open_tabs() { return Err("archived tabs are read-only"); }
        if self.record.as_ref().is_some_and(|record| record.tabs.iter().any(|tab|
            !app.groups.iter().any(|group| group.tabs.contains(&tab.session_id))
                && !app.detached_this_run.contains(&tab.session_id))) {
            return Err("saved layout includes offline tabs");
        }
        Ok(())
    }
    /// Create the machine identity when needed and adopt a safe pre-1.10
    /// tabset before the native UI restores this project's layout.
    pub fn for_scope(home: &Path, scope_key: &str) -> io::Result<Self> {
        let path = resolve_tabset_path(home, scope_key)?;
        let record = load_tabset(&path, scope_key);
        let startup_overflow_id=record.as_ref().and_then(|record|record.raw.get("rust_ui")
            .and_then(|ui|ui.get("startup_fresh_slot")).and_then(Value::as_str)
            .filter(|id|valid_session_id(id)&&record.tabs.iter().any(|tab|tab.session_id==*id)).map(str::to_owned));
        let writable_layout = record.as_ref().is_none_or(|r| supported_layout(&r.raw)
            && (r.tabs.len()<=MAX_TABS || (r.tabs.len()==crate::startup_restore::MAX_STARTUP_TABS && startup_overflow_id.is_some())));
        Ok(Self {
            path,
            scope_key: scope_key.into(),
            record,
            writable_layout,
            startup_archives: Vec::new(), startup_extra_ids: Vec::new(), startup_notice: String::new(), startup_failed:false,startup_error:None, startup_overflow_id,
        })
    }

    /// `machine_id` is the raw contents of DOXA_HOME/machine-id, not its tag.
    /// A read-only caller must use `doxa_state::machine_id`; it never mints one.
    pub fn new(home: &Path, scope_key: &str, machine_id: &str) -> io::Result<Self> {
        if scope_key.is_empty() || machine_id.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "missing scope or machine id",
            ));
        }
        let path = tabset_path(home, scope_key, machine_id);
        let record = load_tabset(&path, scope_key);
        let startup_overflow_id=record.as_ref().and_then(|record|record.raw.get("rust_ui")
            .and_then(|ui|ui.get("startup_fresh_slot")).and_then(Value::as_str)
            .filter(|id|valid_session_id(id)&&record.tabs.iter().any(|tab|tab.session_id==*id)).map(str::to_owned));
        let writable_layout = record.as_ref().is_none_or(|r| supported_layout(&r.raw)
            && (r.tabs.len()<=MAX_TABS || (r.tabs.len()==crate::startup_restore::MAX_STARTUP_TABS && startup_overflow_id.is_some())));
        Ok(Self {
            path,
            scope_key: scope_key.into(),
            record,
            writable_layout,
            startup_archives: Vec::new(), startup_extra_ids: Vec::new(), startup_notice: String::new(), startup_failed:false,startup_error:None, startup_overflow_id,
        })
    }

    pub fn saved_tabs(&self) -> Option<&[Tab]> { self.record.as_ref().map(|record| record.tabs.as_slice()) }
    pub(crate) fn has_open_live_session(&self, sessions: &[crate::discovery::Session]) -> bool {
        let Some(groups) = self.record.as_ref().and_then(|record|record.raw.get("layout"))
            .and_then(|layout|layout.get("groups")) else { return !sessions.is_empty(); };
        if !supported_group_tree(groups) { return !sessions.is_empty(); }
        let open = tree_ids(groups);
        sessions.iter().any(|session|open.contains(session.id.as_str()))
    }
    pub fn discard_loaded_layout(&mut self) { self.record = None; self.writable_layout = true; self.startup_overflow_id=None; }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// A partial daemon roster must never prune saved tabs or collections.
    /// The bridge permanently revokes this capability on any attach failure.
    pub fn save_if_complete(&mut self, app: &App, complete: &Mutex<bool>) -> io::Result<bool> {
        // Hold the roster gate through the write. A disconnect cannot revoke
        // completeness between the check and the tabset replacement.
        let guard = complete
            .lock()
            .map_err(|_| io::Error::other("roster guard unavailable"))?;
        if !*guard {
            return Ok(false);
        }
        self.save(app)?;
        Ok(true)
    }

    /// Project saved layout onto verified live and readonly transcript identities.
    /// Missing identities remain protected from accidental persistence.
    pub fn restore(&self, app: &mut App, live_ids: &[String]) -> bool {
        if !self.startup_notice.is_empty() { app.notice = self.startup_notice.clone(); }
        let Some(record) = &self.record else {return false;};
        if let Some(views)=record.raw.get("rust_ui").and_then(|ui|ui.get("fleet_views")).and_then(crate::ui::fleet_menu::SavedView::parse){app.fleet_views=views;}
        for archive in &self.startup_archives { app.restore_archive(&archive.entry, &archive.note); }
        if !self.startup_notice.is_empty() { app.notice = self.startup_notice.clone(); }
        let live: HashSet<&str> = live_ids.iter().map(String::as_str)
            .chain(self.startup_archives.iter().map(|archive| archive.entry.id.as_str())).collect();
        // Preserve archived content for saved identities discovery cannot
        // verify. Grouped tabs keep their panes; flat detached records do not
        // become tabs again on restoration.
        for tab in &record.tabs {
            if !live.contains(tab.session_id.as_str()) {
                let archive = crate::startup_restore::unavailable(tab);
                app.restore_archive(&archive.entry, &archive.note);
            }
        }
        let tabs: Vec<_> = record.tabs.iter().collect();
        let ids: Vec<String> = tabs.iter().map(|t| t.session_id.clone()).collect();
        app.collections = collections::from_json(record.raw.get("collections"), &ids.iter().cloned().collect());
        if tabs.len() > MAX_TABS + usize::from(self.startup_overflow_id.is_some()) { app.notice = "Saved tab count exceeds supported bounds".into(); return false; }
        if tabs.is_empty() {
            return false;
        }
        let active = record
            .active_session_id
            .as_deref()
            .filter(|id| ids.iter().any(|s| s == id));
        let layout = record.raw.get("layout");
        let mut groups = vec![empty_group(), empty_group()];
        let mut pane_tree = None;
        let mut split = Split::Vertical;
        let mut percent = 50;
        let mut grouped_layout = false;
        if let Some(raw) = layout.and_then(|l| l.get("groups")) {
            let owned:HashSet<_>=record.tabs.iter().map(|tab|tab.session_id.as_str()).collect();
            if let Some((projected, orientation, weight, tree)) = tree_ids(raw).iter().all(|id|owned.contains(id)).then(||parse_groups(raw,&ids)).flatten() {
                grouped_layout = true;
                pane_tree = tree;
                groups = projected;
                split = orientation;
                percent = weight;
            } else {
                groups[0].tabs = ids.clone();
            }
        } else if let Some(trees) = layout
            .and_then(|l| l.get("trees"))
            .and_then(Value::as_array)
        {
            let chosen = trees
                .iter()
                .find(|tree| active.is_some_and(|id| tree_ids(tree).contains(id)))
                .or_else(|| trees.first());
            if let Some((projected, orientation, weight)) =
                chosen.and_then(|tree| parse_legacy_tree(tree, &ids))
            {
                groups = projected.to_vec();
                split = orientation;
                percent = weight;
            } else {
                groups[0].tabs = ids.clone();
            }
        } else {
            groups[0].tabs = ids.clone();
        }
        let mut seen = HashSet::new();
        for group in &mut groups {
            group.tabs.retain(|id| seen.insert(id.clone()));
            group.active = group.active.min(group.tabs.len().saturating_sub(1));
        }
        let mut detached = Vec::new();
        for id in ids {
            if seen.insert(id.clone()) {
                if grouped_layout { detached.push(id); }
                else { groups[0].tabs.push(id); }
            }
        }
        if pane_tree.is_none() && groups[0].tabs.is_empty() && !groups[1].tabs.is_empty() {
            groups.swap(0, 1);
        }
        if let Some(id) = active {
            for (index, group) in groups.iter_mut().enumerate() {
                if let Some(tab) = group.tabs.iter().position(|tab| tab == id) {
                    group.active = tab;
                    app.active_group = index;
                }
            }
        }
        // Fresh usable tab beside an all-readonly restore; saved focus stays put.
        for id in &self.startup_extra_ids {
            if live.contains(id.as_str()) && seen.insert(id.clone()) { groups[0].tabs.push(id.clone()); }
        }
        app.groups = groups;
        app.pane_tree = pane_tree;
        app.detached_this_run = detached;
        for tab in &tabs {
            let automatic = record.raw.get("rust_ui").and_then(|ui|ui.get("default_titles"))
                .and_then(|titles|titles.get(&tab.session_id)).and_then(Value::as_str)
                .map(crate::ui::safe_label).filter(|title|!title.is_empty());
            if let Some(title) = &automatic {
                app.default_names.insert(tab.session_id.clone(), title.clone());
            }
            if let Some(name) = tab.pinned_name.as_ref().filter(|name| !name.is_empty()) {
                let name = crate::ui::safe_label(name);
                if !name.is_empty() { app.custom_names.insert(tab.session_id.clone(), name); }
            }
            if let Some(session) = app.sessions.iter_mut().find(|session|session.id==tab.session_id) {
                if let Some(title) = app.custom_names.get(&tab.session_id).or(automatic.as_ref()) {
                    session.title = title.clone();
                }
            }
        }
        app.split = split;
        app.split_percent = percent;
        if let Some(rust_ui) = record.raw.get("rust_ui") {
            if let Some(visible) = rust_ui.get("rail_visible").and_then(Value::as_bool) {
                app.rail_visible = visible;
            }
            if let Some(width) = rust_ui.get("rail_width").and_then(Value::as_u64) {
                app.rail_width = width.clamp(12, 44) as u16;
            }
        }
        true
    }

    /// A successful explicit kill vetoes restore without pruning unrelated
    /// offline tabs or changing this window's active prompt/layout.
    pub fn forget_sessions(&mut self, killed: &HashSet<String>) -> io::Result<()> {
        let Some(old) = self.record.as_ref() else { return Ok(()); };
        if !old.tabs.iter().any(|tab| killed.contains(&tab.session_id)) { return Ok(()); }
        let mut record = old.clone();
        record.tabs.retain(|tab| !killed.contains(&tab.session_id));
        if record.active_session_id.as_ref().is_some_and(|id| killed.contains(id)) {
            record.active_session_id = record.tabs.first().map(|tab| tab.session_id.clone());
        }
        fn prune(value: &mut Value, killed: &HashSet<String>) {
            match value {
                Value::Array(rows) => {
                    rows.retain(|row| !row.get("session_id").and_then(Value::as_str).is_some_and(|id| killed.contains(id))
                        && !row.as_str().is_some_and(|id| killed.contains(id)));
                    for row in rows { prune(row, killed); }
                }
                Value::Object(object) => {
                    for (key, value) in object.iter_mut() {
                        if matches!(key.as_str(), "tabs" | "trees" | "children" | "groups" | "sessions") { prune(value, killed); }
                    }
                    if let Some(tabs) = object.get("tabs").and_then(Value::as_array) {
                        let active = object.get("active").and_then(Value::as_u64).unwrap_or(0) as usize;
                        object.insert("active".into(), json!(active.min(tabs.len().saturating_sub(1))));
                    }
                }
                _ => {}
            }
        }
        if let Some(layout) = record.raw.get_mut("layout") { prune(layout, killed); }
        if let Some(collections) = record.raw.get_mut("collections") { prune(collections, killed); }
        if let Some(titles) = record.raw.get_mut("rust_ui").and_then(|ui|ui.get_mut("default_titles"))
            .and_then(Value::as_object_mut) { titles.retain(|id,_|!killed.contains(id)); }
        record.raw.insert("tabs".into(), json!(record.tabs.iter().map(|tab| json!({"session_id":tab.session_id,"pinned_name":tab.pinned_name,"cwd":tab.cwd})).collect::<Vec<_>>()));
        save_tabset(&self.path, &record)?;
        self.record = Some(record);
        Ok(())
    }

    /// Persist the two visible pane groups. The flat list remains authoritative
    /// for old DOXA readers, and the tree and legacy trees describe the same
    /// geometry. A record with more complex groups is never overwritten.
    pub fn save(&mut self, app: &App) -> io::Result<()> {
        if self.path.as_os_str().is_empty() {return Err(io::Error::new(io::ErrorKind::PermissionDenied,"persistent tabset unavailable"));}
        if app.has_unverified_archived_tabs() {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "unverified archived tabs cannot be persisted"));
        }
        let persisted_groups: Vec<PaneGroup> = app.groups.iter().map(|group| {
            let active_id = group.tabs.get(group.active);
            let tabs: Vec<String> = group.tabs.iter().filter(|id| !app.killed_this_run.contains(*id)
                && !crate::remote_client::valid_target(id)).cloned().collect();
            let active = active_id.and_then(|id| tabs.iter().position(|candidate| candidate == id)).unwrap_or(0);
            PaneGroup { tabs, active, scroll: group.scroll }
        }).collect();
        if !self.writable_layout {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "saved layout exceeds supported pane bounds",
            ));
        }
        // Missing transcripts remain protected even when other saved tabs restore.
        // A partial view must never prune the shared record.
        if self.record.as_ref().is_some_and(|record| {
            record.tabs.iter().any(|tab| {
                !persisted_groups
                    .iter()
                    .any(|group| group.tabs.contains(&tab.session_id))
                    && !app.clear_stop_after_save.contains(&tab.session_id)
                    && !app.killed_this_run.contains(&tab.session_id)
                    && !app.detached_this_run.contains(&tab.session_id)
            })
        }) {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "saved layout includes offline tabs",
            ));
        }
        if persisted_groups.len() > MAX_PANES || persisted_groups.iter().map(|g| g.tabs.len()).sum::<usize>() > MAX_TABS + usize::from(self.startup_overflow_id.as_ref().is_some_and(|id|persisted_groups.iter().any(|group|group.tabs.contains(id)))) {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "pane/tab bounds exceeded"));
        }
        let mut seen = HashSet::new();
        let mut tabs = Vec::new();
        for group in &persisted_groups {
            for id in &group.tabs {
                if !valid_session_id(id) {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "invalid session id",
                    ));
                }
                if seen.insert(id.as_str()) {
                    let old = self
                        .record
                        .as_ref()
                        .and_then(|r| r.tabs.iter().find(|t| t.session_id == *id));
                    tabs.push(Tab {
                        session_id: id.clone(),
                        pinned_name: app.custom_names.get(id).cloned(),
                        cwd: app.recorded_session_cwd(id).map(|cwd| cwd.to_string_lossy().into_owned()).or_else(|| old.and_then(|t| t.cwd.clone())),
                    });
                }
            }
        }
        if app.fleet_views.len()>crate::ui::fleet_menu::MAX_SAVED_VIEWS || app.fleet_views.iter().any(|view|!view.valid()){
            return Err(io::Error::new(io::ErrorKind::InvalidInput,"invalid saved fleet view"));
        }
        if tabs.len() != persisted_groups.iter().map(|g| g.tabs.len()).sum::<usize>() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "same session shown in multiple pane persisted_groups",
            ));
        }
        // Deliberate Ctrl+W detaches remain flat restore records, as in the
        // Python window store. They have no invented current pane geometry.
        for id in &app.detached_this_run {
            if app.killed_this_run.contains(id) || app.clear_stop_after_save.contains(id) || !seen.insert(id.as_str()) { continue; }
            if !valid_session_id(id) { return Err(io::Error::new(io::ErrorKind::InvalidInput,"invalid detached session id")); }
            let old = self.record.as_ref().and_then(|record|record.tabs.iter().find(|tab|tab.session_id==*id));
            tabs.push(Tab {session_id:id.clone(),
                pinned_name:app.custom_names.get(id).cloned().or_else(||old.and_then(|tab|tab.pinned_name.clone())),
                cwd:app.recorded_session_cwd(id).map(|cwd|cwd.to_string_lossy().into_owned()).or_else(||old.and_then(|tab|tab.cwd.clone()))});
        }
        if tabs.len()>MAX_TABS + usize::from(self.startup_overflow_id.as_ref().is_some_and(|id|tabs.iter().any(|tab|tab.session_id==*id))) {
            return Err(io::Error::new(io::ErrorKind::InvalidInput,"saved tab bounds exceeded including detached records"));
        }
        if tabs.is_empty() && app.fleet_views.is_empty() && app.killed_this_run.is_empty() { return Ok(()); }
        let persisted_active=if persisted_groups.get(app.active_group).is_some_and(|g|!g.tabs.is_empty()){
            app.active_group
        }else{persisted_groups.iter().position(|g|!g.tabs.is_empty()).unwrap_or(0)};
        let active = persisted_groups
            .get(persisted_active)
            .and_then(|g| g.tabs.get(g.active))
            .cloned();
        let mut record = self.record.clone().unwrap_or_else(|| TabSet {
            scope_key: self.scope_key.clone(),
            active_session_id: None,
            tabs: Vec::new(),
            raw: Default::default(),
        });
        record.scope_key = self.scope_key.clone();
        record.active_session_id = active;
        record.tabs = tabs;
        let groups: Vec<Value> = persisted_groups.iter().filter(|g| app.pane_tree.is_some() || !g.tabs.is_empty()).map(|g| {
            let leaves: Vec<Value> = g.tabs.iter().map(|id| leaf(&record, id)).collect();
            json!({"kind":"group","active":g.active.min(leaves.len().saturating_sub(1)),"tabs":leaves})
        }).collect();
        let trees: Vec<Value> = persisted_groups
            .iter()
            .filter_map(|g| g.tabs.get(g.active).map(|id| leaf(&record, id)))
            .collect();
        let group_tree = if let Some(tree) = &app.pane_tree {
            tree.serialize(&groups)
        } else if groups.len() == 2 {
            json!({"kind":"split","orientation":orientation(app.split),"weights":[app.split_percent.clamp(20,80) as f64 / 100.0, 1.0 - app.split_percent.clamp(20,80) as f64 / 100.0],"children":groups})
        } else {
            groups.first().cloned().unwrap_or_else(||json!({"kind":"group","active":0,"tabs":[]}))
        };
        let layout = record.raw.entry("layout").or_insert_with(|| json!({}));
        let object = layout
            .as_object_mut()
            .ok_or_else(|| io::Error::new(io::ErrorKind::Unsupported, "unknown layout format"))?;
        object.insert("groups".into(), group_tree);
        object.insert("trees".into(), Value::Array(trees));
        let rust_ui=record.raw.entry("rust_ui").or_insert_with(||json!({})).as_object_mut()
            .ok_or_else(||io::Error::new(io::ErrorKind::Unsupported,"unknown native UI metadata"))?;
        if let Some(id)=self.startup_overflow_id.as_ref().filter(|id|record.tabs.iter().any(|tab|tab.session_id==**id)) {
            rust_ui.insert("startup_fresh_slot".into(),json!(id));
        }else{rust_ui.remove("startup_fresh_slot");}
        rust_ui.insert("rail_visible".into(),json!(app.rail_visible));
        rust_ui.insert("rail_width".into(),json!(app.rail_width.clamp(12,44)));
        let previous_titles=rust_ui.get("default_titles").and_then(Value::as_object).cloned().unwrap_or_default();
        let mut titles=serde_json::Map::new();
        for tab in &record.tabs {
            let title=app.default_names.get(&tab.session_id).cloned()
                .or_else(||(!app.custom_names.contains_key(&tab.session_id)).then(||
                    app.sessions.iter().find(|session|session.id==tab.session_id).map(|session|session.title.clone())).flatten())
                .or_else(||previous_titles.get(&tab.session_id).and_then(Value::as_str).map(str::to_owned));
            if let Some(title)=title.map(|title|crate::ui::safe_label(&title)).filter(|title|!title.is_empty()) {
                titles.insert(tab.session_id.clone(),json!(title));
            }
        }
        if titles.is_empty(){rust_ui.remove("default_titles");}
        else{rust_ui.insert("default_titles".into(),Value::Object(titles));}
        if app.fleet_views.is_empty(){rust_ui.remove("fleet_views");}else{rust_ui.insert("fleet_views".into(),serde_json::to_value(&app.fleet_views).map_err(io::Error::other)?);}
        let keep: HashSet<String> = record.tabs.iter().map(|tab| tab.session_id.clone()).collect();
        let collections = collections::to_json(&app.collections, &keep);
        // A legacy empty tabset can carry an empty collection row as inert
        // metadata. If no restore or edit loaded collections, leave that
        // untouched instead of erasing it during an unrelated new-tab save.
        let untouched_empty_record = self.record.as_ref().is_some_and(|old| old.tabs.is_empty())
            && app.collections.is_empty()
            && record.raw.get("collections").and_then(Value::as_array)
                .is_some_and(|rows| rows.iter().all(|row| row.get("sessions").and_then(Value::as_array)
                    .is_some_and(Vec::is_empty)));
        if untouched_empty_record { /* retain the original row */ }
        else if collections.is_empty() { record.raw.remove("collections"); }
        else { record.raw.insert("collections".into(), Value::Array(collections)); }
        // save_tabset checks old IDs against structure. We have rebuilt both
        // structures above, so set its reference list to the new safe list.
        record.raw.insert("tabs".into(), Value::Array(record.tabs.iter().map(|t| json!({"session_id":t.session_id,"pinned_name":t.pinned_name,"cwd":t.cwd})).collect()));
        save_tabset(&self.path, &record)?;
        self.record = Some(record);
        Ok(())
    }
}

fn empty_group() -> PaneGroup {
    PaneGroup {
        tabs: Vec::new(),
        active: 0,
        scroll: 0,
    }
}
fn orientation(split: Split) -> &'static str {
    match split {
        Split::Horizontal => "column",
        Split::Vertical => "row",
    }
}
fn parse_orientation(raw: &Value) -> Option<Split> {
    match raw.as_str()? {
        "column" => Some(Split::Horizontal),
        "row" => Some(Split::Vertical),
        _ => None,
    }
}
fn leaf(record: &TabSet, id: &str) -> Value {
    let old = record.tabs.iter().find(|t| t.session_id == id);
    let mut row = record
        .raw
        .get("layout")
        .and_then(|layout| layout.get("groups"))
        .and_then(|root| find_leaf(root, id))
        .cloned()
        .or_else(|| {
            record
                .raw
                .get("layout")
                .and_then(|layout| layout.get("trees"))
                .and_then(Value::as_array)
                .and_then(|trees| trees.iter().find_map(|root| find_leaf(root, id)))
                .cloned()
        })
        .unwrap_or_else(|| json!({"kind":"leaf","session_id":id}));
    if let Some(name) = old.and_then(|t| t.pinned_name.as_ref()) {
        row["pinned_name"] = json!(name);
    } else if let Some(object) = row.as_object_mut() {
        object.remove("pinned_name");
    }
    if let Some(cwd) = old.and_then(|t| t.cwd.as_ref()) {
        row["cwd"] = json!(cwd);
    }
    row
}
fn find_leaf<'a>(node: &'a Value, id: &str) -> Option<&'a Value> {
    if node.get("kind").and_then(Value::as_str) == Some("leaf")
        && node.get("session_id").and_then(Value::as_str) == Some(id)
    {
        return Some(node);
    }
    for key in ["tabs", "children"] {
        if let Some(rows) = node.get(key).and_then(Value::as_array) {
            for row in rows {
                if let Some(found) = find_leaf(row, id) {
                    return Some(found);
                }
            }
        }
    }
    None
}
fn tree_ids(raw: &Value) -> HashSet<&str> {
    let mut ids = HashSet::new();
    fn walk<'a>(node: &'a Value, ids: &mut HashSet<&'a str>) {
        if let Some(id) = node.get("session_id").and_then(Value::as_str) {
            ids.insert(id);
        }
        for key in ["tabs", "children"] {
            if let Some(rows) = node.get(key).and_then(Value::as_array) {
                for row in rows {
                    walk(row, ids);
                }
            }
        }
    }
    walk(raw, &mut ids);
    ids
}
fn weight(raw: &Value) -> u16 {
    raw.get("weights")
        .and_then(Value::as_array)
        .and_then(|w| w.first())
        .and_then(Value::as_f64)
        .filter(|w| w.is_finite() && *w > 0.0 && *w < 1.0)
        .map(|w| (w * 100.0).round() as u16)
        .unwrap_or(50)
        .clamp(20, 80)
}
fn supported_layout(raw: &serde_json::Map<String, Value>) -> bool {
    if raw.get("rust_ui").and_then(|ui|ui.get("fleet_views")).is_some_and(|views|crate::ui::fleet_menu::SavedView::parse(views).is_none()){return false;}
    let Some(layout) = raw.get("layout") else {
        return true;
    };
    let Some(layout) = layout.as_object() else {
        return false;
    };
    if layout.get("kind").and_then(Value::as_str) != Some("tabs") {
        return false;
    }
    if let Some(groups) = layout.get("groups") {
        let owned:HashSet<_>=raw.get("tabs").or_else(||layout.get("tabs")).and_then(Value::as_array).into_iter().flatten()
            .filter_map(|row|row["session_id"].as_str()).collect();
        return supported_group_tree(groups)&&tree_ids(groups).iter().all(|id|owned.contains(id));
    }
    if let Some(trees) = layout.get("trees").and_then(Value::as_array) {
        // The old tree format can have one split tree for the active tab.
        // Nested or multi-way geometry cannot be represented by this UI.
        return trees.iter().all(supported_legacy_tree);
    }
    true
}
fn supported_group_tree(node: &Value) -> bool {
    fn walk(node: &Value, depth: usize, groups: &mut usize, tabs: &mut usize) -> bool {
        if depth > MAX_DEPTH { return false; }
        match node.get("kind").and_then(Value::as_str) {
            Some("group") => {
                *groups += 1;
                let Some(rows) = node.get("tabs").and_then(Value::as_array) else { return false; };
                *tabs += rows.len();
                *groups <= MAX_PANES && *tabs <= crate::startup_restore::MAX_STARTUP_TABS && rows.iter().all(|leaf|
                    leaf["kind"] == "leaf" && leaf["session_id"].as_str().is_some_and(valid_session_id))
            }
            Some("split") => {
                let Some(children) = node["children"].as_array() else { return false; };
                parse_orientation(&node["orientation"]).is_some() && (2..=MAX_PANES).contains(&children.len())
                    && children.iter().all(|child| walk(child, depth + 1, groups, tabs))
            }
            _ => false,
        }
    }
    walk(node, 0, &mut 0, &mut 0)
}
fn supported_legacy_tree(node: &Value) -> bool {
    match node.get("kind").and_then(Value::as_str) {
        Some("leaf") => true,
        Some("split") => {
            parse_orientation(&node["orientation"]).is_some()
                && node
                    .get("children")
                    .and_then(Value::as_array)
                    .is_some_and(|children| {
                        children.len() == 2
                            && children.iter().all(|child| {
                                child.get("kind").and_then(Value::as_str) == Some("leaf")
                            })
                    })
        }
        _ => false,
    }
}
fn group_tabs(raw: &Value, live: &[String]) -> Option<PaneGroup> {
    if raw.get("kind")?.as_str()? != "group" {
        return None;
    }
    let mut group = empty_group();
    for row in raw.get("tabs")?.as_array()? {
        let Some(id) = row.get("session_id").and_then(Value::as_str) else {
            continue;
        };
        if live.iter().any(|s| s == id) && !group.tabs.iter().any(|s| s == id) {
            group.tabs.push(id.into());
        }
    }
    let saved_active = raw.get("active").and_then(Value::as_u64).unwrap_or(0) as usize;
    if let Some(id) = raw
        .get("tabs")
        .and_then(Value::as_array)
        .and_then(|rows| rows.get(saved_active))
        .and_then(|row| row.get("session_id"))
        .and_then(Value::as_str)
    {
        group.active = group.tabs.iter().position(|s| s == id).unwrap_or(0);
    }
    Some(group)
}
fn parse_groups(raw: &Value, live: &[String]) -> Option<(Vec<PaneGroup>, Split, u16, Option<Tree>)> {
    if !supported_group_tree(raw) { return None; }
    fn walk(raw: &Value, live: &[String], groups: &mut Vec<PaneGroup>) -> Option<Tree> {
        if raw["kind"] == "group" {
            let index = groups.len();
            groups.push(group_tabs(raw, live)?);
            return Some(Tree::Group(index));
        }
        let children: Option<Vec<_>> = raw["children"].as_array()?.iter().map(|child| walk(child, live, groups)).collect();
        let children = children?;
        let count = children.len();
        let mut weights: Vec<u16> = raw["weights"].as_array().into_iter().flatten().filter_map(|value| value.as_f64())
            .filter(|value| value.is_finite() && *value > 0.0 && *value <= 1.0)
            .map(|value| (value * 10000.0).round().clamp(1.0, 10000.0) as u16).collect();
        if weights.len() != count { weights = vec![10000 / count as u16; count]; }
        Some(Tree::Split { orientation: parse_orientation(&raw["orientation"])?, children, weights })
    }
    let mut groups = Vec::new();
    let mut tree = walk(raw, live, &mut groups)?;
    for index in (0..groups.len()).rev() {
        if groups[index].tabs.is_empty() && groups.len() > 1 {
            groups.remove(index);
            tree = tree.without(index)?;
        }
    }
    let (orientation, percent) = match &tree {
        Tree::Split { orientation, weights, .. } => (*orientation,
            (u32::from(weights[0]) * 100 / weights.iter().map(|w|u32::from(*w)).sum::<u32>().max(1)) as u16),
        _ => (Split::Vertical, 50),
    };
    if groups.len() <= 2 {
        while groups.len() < 2 { groups.push(empty_group()); }
        Some((groups, orientation, percent, None))
    } else { Some((groups, orientation, percent, Some(tree))) }
}
fn parse_legacy_tree(raw: &Value, live: &[String]) -> Option<([PaneGroup; 2], Split, u16)> {
    if raw.get("kind")?.as_str()? != "split" {
        return None;
    }
    let children = raw.get("children")?.as_array()?;
    if children.len() != 2 {
        return None;
    }
    let mut groups = [empty_group(), empty_group()];
    for (index, child) in children.iter().enumerate() {
        if child.get("kind")?.as_str()? != "leaf" {
            return None;
        }
        let id = child.get("session_id")?.as_str()?;
        if live.iter().any(|s| s == id) {
            groups[index].tabs.push(id.into());
        }
    }
    Some((groups, parse_orientation(&raw["orientation"])?, weight(raw)))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn mixed_window_persists_only_local_sessions(){
        let dir=tempfile::tempdir_in(std::env::current_dir().unwrap()).unwrap();
        let mut store=UiStateStore::new(dir.path(),"/project","machine").unwrap();
        let mut app=App::default();
        app.groups[0].tabs=vec!["local-session".into(),"host~remote-session".into()];
        app.groups[0].active=1;
        app.default_names.insert("host~remote-session".into(),"Remote".into());
        store.save(&app).unwrap();
        let saved=load_tabset(store.path(),"/project").unwrap();
        assert_eq!(saved.tabs.iter().map(|tab|tab.session_id.as_str()).collect::<Vec<_>>(),["local-session"]);
        assert_eq!(saved.active_session_id.as_deref(),Some("local-session"));
        assert!(!std::fs::read_to_string(store.path()).unwrap().contains("host~remote-session"));
    }
    #[test]
    fn explicit_detach_keeps_flat_restore_record_and_allows_later_layout_saves() {
        use crossterm::event::{Event,KeyCode,KeyEvent,KeyModifiers};
        let dir = tempfile::tempdir().unwrap();
        let mut store = UiStateStore::new(dir.path(),"/project","machine").unwrap();
        let mut app = App::default();
        app.groups[0].tabs = vec!["one".into(),"two".into()];
        app.custom_names.insert("one".into(),"Pinned".into());
        store.save(&app).unwrap();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('w'),KeyModifiers::CONTROL)));
        assert_eq!(app.groups[0].tabs,["two"]);
        app.groups[0].tabs.push("fresh".into()); app.groups[0].active = 1;
        app.rail_width = 31;
        assert!(store.save_if_complete(&app,&Mutex::new(true)).unwrap());
        let saved = load_tabset(store.path(),"/project").unwrap();
        assert_eq!(saved.tabs.iter().map(|tab|tab.session_id.as_str()).collect::<Vec<_>>(),["two","fresh","one"]);
        assert_eq!(saved.tabs[2].pinned_name.as_deref(),Some("Pinned"));
        assert_eq!(saved.raw["layout"]["groups"]["tabs"].as_array().unwrap().len(),2);
        assert_eq!(saved.raw["rust_ui"]["rail_width"],31);
        assert_eq!(store.clear_preflight(&app,&Mutex::new(true)),Ok(()));
        let mut reloaded = UiStateStore::new(dir.path(),"/project","machine").unwrap();
        reloaded.startup_archives.push(crate::startup_restore::Archive {
            entry:crate::history::OfflineSession {id:"one".into(),project:"project".into(),
                markdown:"saved conversation".into(),search_snippets:vec![],cwd:Some("/project".into())},
            note:String::new(),
        });
        let mut restored = App::default();
        assert!(reloaded.restore(&mut restored,&["two".into(),"fresh".into()]));
        assert_eq!(restored.groups[0].tabs,["two","fresh"]);
        assert_eq!(restored.detached_this_run,["one"]);
        assert_eq!(restored.sessions[0].title,"Pinned");
        assert_eq!(restored.groups[0].tabs.get(restored.groups[0].active).map(String::as_str),Some("fresh"));
        assert!(restored.pending_prompts.is_empty());
        assert!(reloaded.save_if_complete(&restored,&Mutex::new(true)).unwrap());
        assert!(reloaded.saved_tabs().unwrap().iter().any(|tab|tab.session_id=="one"));
    }

    #[test]
    fn generated_session_title_survives_layout_restore_and_next_hello() {
        let dir=tempfile::tempdir().unwrap();
        let mut store=UiStateStore::new(dir.path(),"/project","machine").unwrap();
        let mut app=App::default();
        app.groups[0].tabs.push("session-1".into());
        app.default_names.insert("session-1".into(),"gpt-6-sol@main/doxa".into());
        store.save(&app).unwrap();
        assert_eq!(load_tabset(store.path(),"/project").unwrap().raw["rust_ui"]["default_titles"]["session-1"],"gpt-6-sol@main/doxa");
        let mut restored=App::default();
        assert!(store.restore(&mut restored,&["session-1".into()]));
        restored.apply_daemon_frame(&json!({"type":"hello","session_id":"session-1","model":"gpt-6-sol"}));
        assert_eq!(restored.sessions[0].title,"gpt-6-sol@main/doxa");
    }
    use serde_json::json;

    #[test]
    fn pinned_tab_name_round_trips_and_clear_removes_leaf_name() {
        let temp = tempfile::tempdir().unwrap();
        let mut store = UiStateStore::new(temp.path(), "/project", "machine").unwrap();
        let mut app = App::default();
        app.groups[0].tabs.push("session-1".into());
        app.custom_names.insert("session-1".into(), "My work".into());
        store.save(&app).unwrap();
        let saved = std::fs::read_to_string(store.path()).unwrap();
        assert!(saved.contains("My work"));
        let mut restored = App::default();
        assert!(store.restore(&mut restored, &["session-1".into()]));
        assert_eq!(restored.custom_names.get("session-1").map(String::as_str), Some("My work"));
        restored.apply_daemon_frame(&json!({"type":"hello", "session_id":"session-1", "model":"auto"}));
        assert_eq!(restored.sessions[0].title, "My work");
        restored.custom_names.remove("session-1");
        store.save(&restored).unwrap();
        let saved: Value = serde_json::from_slice(&std::fs::read(store.path()).unwrap()).unwrap();
        assert!(saved["tabs"][0]["pinned_name"].is_null());
        assert!(saved["layout"]["groups"]["tabs"][0].get("pinned_name").is_none());
    }

    #[test]
    fn python_collections_restore_order_and_prune_dead_members_on_save() {
        let temp = tempfile::tempdir().unwrap();
        let path = tabset_path(temp.path(), "/project", "machine");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::set_permissions(path.parent().unwrap(), std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        std::fs::write(&path, serde_json::to_vec(&json!({
            "scope_key":"/project", "tabs":[{"session_id":"one"},{"session_id":"two"}],
            "layout":{"kind":"tabs","tabs":[{"session_id":"one"},{"session_id":"two"}]},
            "collections":[
                {"name":"First","sessions":["two","dead","one"],"collapsed":true},
                {"name":"Second","sessions":["one"]}
            ]
        })).unwrap()).unwrap();
        let mut store = UiStateStore::new(temp.path(), "/project", "machine").unwrap();
        let mut app = App::default();
        assert!(store.restore(&mut app, &["one".into(), "two".into()]));
        assert_eq!(app.collections.len(), 1);
        assert_eq!(app.collections[0].sessions, ["two", "one"]);
        store.save(&app).unwrap();
        let saved: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(saved["collections"], json!([{"name":"First","sessions":["two","one"],"collapsed":true}]));
    }

    #[test]
    fn collection_edit_refuses_to_overwrite_unsupported_layout() {
        let temp = tempfile::tempdir().unwrap();
        let path = tabset_path(temp.path(), "/project", "machine");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::set_permissions(path.parent().unwrap(), std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        let original = json!({"tabs":[{"session_id":"one"}],
            "layout":{"kind":"tabs","groups":{"kind":"split","children":[]}},
            "collections":[{"name":"Keep","sessions":["one"]}]});
        std::fs::write(&path, serde_json::to_vec(&original).unwrap()).unwrap();
        let mut store = UiStateStore::new(temp.path(), "/project", "machine").unwrap();
        let mut app = App::default();
        app.groups[0].tabs.push("one".into());
        app.collections.push(Collection { name:"Changed".into(), sessions:vec!["one".into()], collapsed:false });
        assert_eq!(store.save(&app).unwrap_err().kind(), io::ErrorKind::Unsupported);
        let saved: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(saved, original);
    }

    #[test]
    fn empty_named_collection_is_only_in_memory_like_python() {
        let temp = tempfile::tempdir().unwrap();
        let mut store = UiStateStore::new(temp.path(), "/project", "machine").unwrap();
        let mut app = App::default();
        app.collections.push(Collection { name:"Later".into(), sessions:vec![], collapsed:true });
        store.save(&app).unwrap();
        assert!(!store.path().exists());
        assert_eq!(app.collections[0].name, "Later");
    }

    #[test]
    fn clear_replacement_is_the_only_authorized_missing_saved_tab() {
        let temp = tempfile::tempdir().unwrap();
        let mut store = UiStateStore::new(temp.path(), "/project", "machine").unwrap();
        let mut app = App::default();
        app.groups[0].tabs.push("old".into());
        store.save(&app).unwrap();
        let before = std::fs::read(store.path()).unwrap();
        app.groups[0].tabs[0] = "fresh".into();
        assert_eq!(store.save(&app).unwrap_err().kind(), io::ErrorKind::Unsupported);
        assert_eq!(std::fs::read(store.path()).unwrap(), before);
        app.clear_stop_after_save.push("old".into());
        store.save(&app).unwrap();
        let saved: Value = serde_json::from_slice(&std::fs::read(store.path()).unwrap()).unwrap();
        assert_eq!(saved["tabs"][0]["session_id"], "fresh");
        assert_eq!(saved["layout"]["groups"]["tabs"][0]["session_id"], "fresh");
    }

    #[test]
    fn clear_preflight_requires_complete_roster_and_every_saved_tab() {
        let temp = tempfile::tempdir().unwrap();
        let mut store = UiStateStore::new(temp.path(), "/project", "machine").unwrap();
        let mut app = App::default();
        app.groups[0].tabs.push("old".into());
        store.save(&app).unwrap();
        let complete = Mutex::new(true);
        assert!(store.clear_preflight(&app, &complete).is_ok());
        *complete.lock().unwrap() = false;
        assert_eq!(store.clear_preflight(&app, &complete), Err("live roster incomplete"));
        *complete.lock().unwrap() = true;
        app.groups[0].tabs.clear();
        assert_eq!(store.clear_preflight(&app, &complete), Err("saved layout includes offline tabs"));
    }
    #[test]
    fn nested_three_pane_tree_round_trips_and_bounds_are_strict() {
        let leaf=|id:&str| json!({"kind":"group","tabs":[{"kind":"leaf","session_id":id}]});
        let raw=json!({"kind":"split","orientation":"row","weights":[0.4,0.6],"children":[leaf("a"),
            {"kind":"split","orientation":"column","weights":[0.5,0.5],"children":[leaf("b"),leaf("c")]}]});
        let (groups,_,_,tree)=parse_groups(&raw,&["a".into(),"b".into(),"c".into()]).unwrap();
        assert_eq!(groups.len(),3);
        let tree=tree.unwrap();
        let saved=tree.serialize(&[leaf("a"),leaf("b"),leaf("c")]);
        assert!(supported_group_tree(&saved));
        assert_eq!(parse_groups(&saved,&["a".into(),"b".into(),"c".into()]).unwrap().3,Some(tree));
        let oversized=json!({"kind":"split","orientation":"row","children":(0..=MAX_PANES).map(|_|leaf("a")).collect::<Vec<_>>()});
        assert!(!supported_group_tree(&oversized));
    }

    #[test]
    fn typed_fleet_views_round_trip_without_synthetic_session_tabs(){
        let temp=tempfile::tempdir().unwrap();let mut store=UiStateStore::new(temp.path(),"/project","machine").unwrap();
        let mut app=App::default();app.fleet_views=crate::ui::fleet_menu::SavedView::parse(&json!([{"kind":"fleet","root":"/real/fleet","run_id":"actual-run"}])).unwrap();
        store.save(&app).unwrap();let raw:Value=serde_json::from_slice(&std::fs::read(store.path()).unwrap()).unwrap();
        assert_eq!(raw["tabs"],json!([]));assert_eq!(raw["rust_ui"]["fleet_views"][0]["run_id"],"actual-run");
        let saved=UiStateStore::new(temp.path(),"/project","machine").unwrap();let mut restored=App::default();
        assert!(!saved.restore(&mut restored,&[]));assert_eq!(restored.fleet_views,app.fleet_views);
        assert!(restored.groups.iter().all(|group|group.tabs.is_empty()));
        assert!(crate::ui::fleet_menu::SavedView::parse(&json!([{"kind":"session","root":"/real/fleet","run_id":"actual-run"}])).is_none());
    }

    #[test]
    fn explicit_kill_veto_preserves_other_offline_tabs_and_active_draft() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
        let mut app = App::default();
        app.groups[0].tabs = vec!["kill-me".into(), "keep-me".into()];
        app.groups[0].active = 1;
        app.input = "active draft".into();
        store.save(&app).unwrap();
        app.groups[0].tabs = vec!["kill-me".into()];
        store.forget_sessions(&HashSet::from(["kill-me".into()])).unwrap();
        let saved = load_tabset(store.path(), "/repo").unwrap();
        assert_eq!(saved.tabs.iter().map(|tab| tab.session_id.as_str()).collect::<Vec<_>>(), ["keep-me"]);
        assert_eq!(saved.active_session_id.as_deref(), Some("keep-me"));
        assert!(!serde_json::to_string(&saved.raw).unwrap().contains("kill-me"));
        assert_eq!(app.groups[0].tabs, ["kill-me"]);
        assert_eq!(app.input, "active draft");
    }

    #[test]
    fn startup_archives_preserve_saved_order_focus_geometry_and_record() {
        let dir=tempfile::tempdir().unwrap();
        let mut store=UiStateStore::new(dir.path(),"/project","machine").unwrap();
        let mut original=App::default();
        original.groups[0].tabs=vec!["archive-a".into(),"live-b".into()];
        original.groups[1].tabs=vec!["archive-c".into()];
        original.active_group=1;
        original.split=Split::Horizontal;original.split_percent=37;
        original.rail_visible=true;original.rail_width=31;
        original.custom_names.insert("archive-c".into(),"Saved name".into());
        store.save(&original).unwrap();
        for id in ["archive-a","archive-c"] {
            store.startup_archives.push(crate::startup_restore::Archive {
                entry:crate::history::OfflineSession {id:id.into(),project:"project".into(),markdown:"saved content".into(),search_snippets:vec![],cwd:Some("/project".into())},
                note:"not resumed — provider history unavailable".into(),
            });
        }
        let mut restored=App::default();
        assert!(store.restore(&mut restored,&["live-b".into()]));
        assert_eq!(restored.groups[0].tabs,original.groups[0].tabs);
        assert_eq!(restored.groups[1].tabs,original.groups[1].tabs);
        assert_eq!((restored.active_group,restored.split,restored.split_percent),(1,Split::Horizontal,37));
        assert_eq!((restored.rail_visible,restored.rail_width),(true,31));
        assert_eq!(restored.custom_names.get("archive-c"),Some(&"Saved name".into()));
        assert!(restored.has_offline_open_tabs());
        assert!(restored.sessions.iter().all(|session|session.status.contains("provider history unavailable")));
        assert!(store.save_if_complete(&restored,&Mutex::new(true)).unwrap());
        let saved=load_tabset(store.path(),"/project").unwrap();
        assert_eq!(saved.tabs.len(),3);
        assert_eq!(saved.tabs[0].cwd.as_deref(),Some("/project"));
        assert_eq!(saved.active_session_id.as_deref(),Some("archive-c"));
        // An unavailable saved transcript is never silently removed.
        restored.groups[0].tabs.remove(0);
        let before=std::fs::read(store.path()).unwrap();
        assert!(store.save(&restored).is_err());
        assert_eq!(std::fs::read(store.path()).unwrap(),before);
    }
    #[test]
    fn saved_only_restore_keeps_archived_focus_and_adds_one_usable_tab() {
        let dir=tempfile::tempdir().unwrap();
        let mut store=UiStateStore::new(dir.path(),"/project","machine").unwrap();
        let mut app=App::default();app.groups[0].tabs=vec!["archive".into()];store.save(&app).unwrap();
        store.startup_archives.push(crate::startup_restore::Archive {
            entry:crate::history::OfflineSession {id:"archive".into(),project:"project".into(),markdown:"saved".into(),search_snippets:vec![],cwd:None},note:String::new()});
        store.startup_extra_ids.push("fresh".into());
        let mut restored=App::default();assert!(store.restore(&mut restored,&["fresh".into()]));
        assert_eq!(restored.groups[0].tabs,vec!["archive","fresh"]);
        assert_eq!(restored.groups[0].active,0);
        assert_eq!(store.saved_tabs().unwrap().len(),1);
    }

}
