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
use crate::collections::{self, Collection};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LayoutSignature {
    groups: [(Vec<String>, usize); 2],
    custom_names: Vec<(String, String)>,
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
            groups: app.groups.clone().map(|g| (g.tabs, g.active)),
            custom_names: {
                let mut names: Vec<_> = app.custom_names.iter().map(|(id, name)| (id.clone(), name.clone())).collect();
                names.sort();
                names
            },
            active_group: app.active_group,
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
}

impl UiStateStore {
    /// A clear may finalize its old daemon only when replacing the tab can
    /// be persisted from a complete live view of a representable layout.
    pub fn clear_preflight(&self, app: &App, complete: &Mutex<bool>) -> Result<(), &'static str> {
        if !*complete.lock().map_err(|_| "live roster guard unavailable")? {
            return Err("live roster incomplete");
        }
        if !self.writable_layout { return Err("saved layout has more than two panes"); }
        if app.has_offline_open_tabs() { return Err("archived tabs are read-only"); }
        if self.record.as_ref().is_some_and(|record| record.tabs.iter().any(|tab|
            !app.groups.iter().any(|group| group.tabs.contains(&tab.session_id)))) {
            return Err("saved layout includes offline tabs");
        }
        Ok(())
    }
    /// Create the machine identity when needed and adopt a safe pre-1.10
    /// tabset before the native UI restores this project's layout.
    pub fn for_scope(home: &Path, scope_key: &str) -> io::Result<Self> {
        let path = resolve_tabset_path(home, scope_key)?;
        let record = load_tabset(&path, scope_key);
        let writable_layout = record.as_ref().is_none_or(|r| supported_layout(&r.raw));
        Ok(Self {
            path,
            scope_key: scope_key.into(),
            record,
            writable_layout,
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
        let writable_layout = record.as_ref().is_none_or(|r| supported_layout(&r.raw));
        Ok(Self {
            path,
            scope_key: scope_key.into(),
            record,
            writable_layout,
        })
    }

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

    /// Project a saved tabset onto currently live daemon IDs. Returns false
    /// when no saved session remains live; the caller keeps its fresh layout.
    pub fn restore(&self, app: &mut App, live_ids: &[String]) -> bool {
        let Some(record) = &self.record else {
            return false;
        };
        let live: HashSet<&str> = live_ids.iter().map(String::as_str).collect();
        let tabs: Vec<_> = record
            .tabs
            .iter()
            .filter(|t| live.contains(t.session_id.as_str()))
            .collect();
        let ids: Vec<String> = tabs.iter().map(|t| t.session_id.clone()).collect();
        app.collections = collections::from_json(record.raw.get("collections"), &ids.iter().cloned().collect());
        if tabs.is_empty() {
            return false;
        }
        let active = record
            .active_session_id
            .as_deref()
            .filter(|id| ids.iter().any(|s| s == id));
        let layout = record.raw.get("layout");
        let mut groups = [empty_group(), empty_group()];
        let mut split = Split::Vertical;
        let mut percent = 50;
        if let Some(raw) = layout.and_then(|l| l.get("groups")) {
            if let Some((projected, orientation, weight)) = parse_groups(raw, &ids) {
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
                groups = projected;
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
        for id in ids {
            if seen.insert(id.clone()) {
                groups[0].tabs.push(id);
            }
        }
        if groups[0].tabs.is_empty() && !groups[1].tabs.is_empty() {
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
        app.groups = groups;
        for tab in &tabs {
            if let Some(name) = tab.pinned_name.as_ref().filter(|name| !name.is_empty()) {
                app.custom_names.insert(tab.session_id.clone(), name.clone());
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

    /// Persist the two visible pane groups. The flat list remains authoritative
    /// for old DOXA readers, and the tree and legacy trees describe the same
    /// geometry. A record with more complex groups is never overwritten.
    pub fn save(&mut self, app: &App) -> io::Result<()> {
        if !self.writable_layout {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "saved layout has more than two panes",
            ));
        }
        // This UI mounts live daemons only. A saved tab whose daemon is
        // offline may still have a transcript and restore as an archived tab
        // in Python. Do not rewrite the shared record from a partial view.
        if self.record.as_ref().is_some_and(|record| {
            record.tabs.iter().any(|tab| {
                !app.groups
                    .iter()
                    .any(|group| group.tabs.contains(&tab.session_id))
                    && !app.clear_stop_after_save.contains(&tab.session_id)
            })
        }) {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "saved layout includes offline tabs",
            ));
        }
        let mut seen = HashSet::new();
        let mut tabs = Vec::new();
        for group in &app.groups {
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
                        cwd: old.and_then(|t| t.cwd.clone()),
                    });
                }
            }
        }
        if tabs.is_empty() {
            return Ok(());
        }
        if tabs.len() != app.groups.iter().map(|g| g.tabs.len()).sum::<usize>() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "same session shown in multiple pane groups",
            ));
        }
        let active = app
            .groups
            .get(app.active_group)
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
        let groups: Vec<Value> = app.groups.iter().filter(|g| !g.tabs.is_empty()).map(|g| {
            let leaves: Vec<Value> = g.tabs.iter().map(|id| leaf(&record, id)).collect();
            json!({"kind":"group","active":g.active.min(leaves.len().saturating_sub(1)),"tabs":leaves})
        }).collect();
        let trees: Vec<Value> = app
            .groups
            .iter()
            .filter_map(|g| g.tabs.get(g.active).map(|id| leaf(&record, id)))
            .collect();
        let group_tree = if groups.len() == 2 {
            json!({"kind":"split","orientation":orientation(app.split),"weights":[app.split_percent.clamp(20,80) as f64 / 100.0, 1.0 - app.split_percent.clamp(20,80) as f64 / 100.0],"children":groups})
        } else {
            groups[0].clone()
        };
        let layout = record.raw.entry("layout").or_insert_with(|| json!({}));
        let object = layout
            .as_object_mut()
            .ok_or_else(|| io::Error::new(io::ErrorKind::Unsupported, "unknown layout format"))?;
        object.insert("groups".into(), group_tree);
        object.insert("trees".into(), Value::Array(trees));
        record.raw.insert(
            "rust_ui".into(),
            json!({"rail_visible":app.rail_visible,"rail_width":app.rail_width.clamp(12,44)}),
        );
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
        return supported_group_tree(groups);
    }
    if let Some(trees) = layout.get("trees").and_then(Value::as_array) {
        // The old tree format can have one split tree for the active tab.
        // Nested or multi-way geometry cannot be represented by this UI.
        return trees.iter().all(supported_legacy_tree);
    }
    true
}
fn supported_group_tree(node: &Value) -> bool {
    match node.get("kind").and_then(Value::as_str) {
        Some("group") => node
            .get("tabs")
            .and_then(Value::as_array)
            .is_some_and(|tabs| {
                tabs.iter()
                    .all(|leaf| leaf.get("kind").and_then(Value::as_str) == Some("leaf"))
            }),
        Some("split") => {
            parse_orientation(&node["orientation"]).is_some()
                && node
                    .get("children")
                    .and_then(Value::as_array)
                    .is_some_and(|children| {
                        children.len() == 2
                            && children.iter().all(|child| {
                                child.get("kind").and_then(Value::as_str) == Some("group")
                                    && supported_group_tree(child)
                            })
                    })
        }
        _ => false,
    }
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
fn parse_groups(raw: &Value, live: &[String]) -> Option<([PaneGroup; 2], Split, u16)> {
    if raw.get("kind")?.as_str()? == "group" {
        return Some(([group_tabs(raw, live)?, empty_group()], Split::Vertical, 50));
    }
    if raw.get("kind")?.as_str()? != "split" {
        return None;
    }
    let children = raw.get("children")?.as_array()?;
    if children.len() != 2 {
        return None;
    }
    Some((
        [
            group_tabs(&children[0], live)?,
            group_tabs(&children[1], live)?,
        ],
        parse_orientation(&raw["orientation"])?,
        weight(raw),
    ))
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
}
