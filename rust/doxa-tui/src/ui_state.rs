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

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LayoutSignature {
    groups: [(Vec<String>, usize); 2],
    active_group: usize,
    split: Split,
    split_percent: u16,
    rail_visible: bool,
    rail_width: u16,
}
impl LayoutSignature {
    pub fn capture(app: &App) -> Self {
        Self {
            groups: app.groups.clone().map(|g| (g.tabs, g.active)),
            active_group: app.active_group,
            split: app.split,
            split_percent: app.split_percent,
            rail_visible: app.rail_visible,
            rail_width: app.rail_width,
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
        if tabs.is_empty() {
            return false;
        }
        let ids: Vec<String> = tabs.iter().map(|t| t.session_id.clone()).collect();
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
                        pinned_name: old.and_then(|t| t.pinned_name.clone()),
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
        // Remove dangling collection membership before changing the flat list.
        if let Some(rows) = record
            .raw
            .get_mut("collections")
            .and_then(Value::as_array_mut)
        {
            for row in rows {
                if let Some(members) = row.get_mut("sessions").and_then(Value::as_array_mut) {
                    members.retain(|id| id.as_str().is_some_and(|id| seen.contains(id)));
                }
            }
        }
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
