//! Rail signals and explicit owner-side project label edits.
use super::{clipped_title, safe_label, App};
use ratatui::style::Color;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

const PALETTE: [(&str, Color); 6] = [
    ("blue", Color::Rgb(0x8A, 0xBF, 0xF2)),
    ("teal", Color::Rgb(0x78, 0xCF, 0xC3)),
    ("amber", Color::Rgb(0xE6, 0xC0, 0x75)),
    ("violet", Color::Rgb(0xBD, 0xA4, 0xE8)),
    ("coral", Color::Rgb(0xE8, 0x9A, 0x82)),
    ("green", Color::Rgb(0x9B, 0xCE, 0x8F)),
];

pub(super) fn named(name: &str) -> Option<Color> {
    PALETTE.iter().find(|(candidate, _)| *candidate == name).map(|(_, colour)| *colour)
}

/// The owner's config may override an exact canonical project root with a
/// palette name. An invalid explicit name suppresses colour instead of
/// silently pretending it was an assigned hue.
pub(super) fn configured_colours() -> Option<HashMap<PathBuf, String>> {
    let path = crate::settings::config_path().ok()?;
    let config = doxa_state::load_config_checked(&path).ok()?;
    colours_from_config(&config)
}

/// Project aliases are owner config keyed by the same canonical root used for
/// project hues. Bad entries suppress aliases instead of becoming rail text.
pub(super) fn configured_labels() -> Option<HashMap<PathBuf, String>> {
    let path = crate::settings::config_path().ok()?;
    let config = doxa_state::load_config_checked(&path).ok()?;
    labels_from_config(&config)
}

fn labels_from_config(config: &toml::Table) -> Option<HashMap<PathBuf, String>> {
    let Some(value) = config.get("project_labels") else { return Some(HashMap::new()); };
    let table = value.as_table()?;
    if table.len() > 512 { return None; }
    let labels: HashMap<PathBuf, String> = table.iter().map(|(path, value)| {
        if !Path::new(path).is_absolute() { return None; }
        let label = clean_project_label(value.as_str()?)?;
        Some((PathBuf::from(path), label))
    }).collect::<Option<_>>()?;
    let mut names = std::collections::HashSet::new();
    if !labels.values().all(|label| names.insert(label.to_lowercase())) { return None; }
    Some(labels)
}

fn clean_project_label(raw: &str) -> Option<String> {
    if raw.len() > 96 || raw.chars().any(super::unsafe_input_char) { return None; }
    let label = raw.split_whitespace().collect::<Vec<_>>().join(" ");
    (!label.is_empty() && label.chars().count() <= 48).then_some(label)
}

pub(super) fn save_project_label(path: &Path, root: &Path, label: Option<&str>) -> io::Result<()> {
    if !root.is_absolute() || root.canonicalize().ok().as_deref() != Some(root) || !root.is_dir() {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "project root is no longer verified"));
    }
    let key = root.to_str().ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "project root is not UTF-8"))?;
    let label = label.map(|raw| clean_project_label(raw)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "label must be 1–48 visible characters and at most 96 bytes")))
        .transpose()?;
    doxa_state::update_config(path, |config| {
        if labels_from_config(config).is_none() {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "invalid project_labels config"));
        }
        if let Some(label) = label {
            let table = config.entry("project_labels".to_owned())
                .or_insert_with(|| toml::Value::Table(toml::Table::new())).as_table_mut()
                .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "invalid project_labels config"))?;
            table.insert(key.to_owned(), toml::Value::String(label));
        } else if let Some(table) = config.get_mut("project_labels").and_then(toml::Value::as_table_mut) {
            table.remove(key);
            if table.is_empty() { config.remove("project_labels"); }
        }
        if root.canonicalize().ok().as_deref() != Some(root) || labels_from_config(config).is_none() {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "project root or labels changed during edit"));
        }
        Ok(())
    })
}

fn colours_from_config(config: &toml::Table) -> Option<HashMap<PathBuf, String>> {
    let Some(value) = config.get("project_colours") else { return Some(HashMap::new()); };
    let table = value.as_table()?;
    table.iter().map(|(path, value)| {
        if !Path::new(path).is_absolute() { return None; }
        Some((PathBuf::from(path), value.as_str()?.to_owned()))
    }).collect()
}

pub(super) fn project_colour(root: &Path, overrides: &HashMap<PathBuf, String>) -> Option<Color> {
    if let Some(name) = overrides.get(root) { return named(name); }
    let digest = Sha256::digest(root.as_os_str().as_bytes());
    Some(PALETTE[usize::from(digest[0]) % PALETTE.len()].1)
}

pub(super) fn urgency_label(rank: u8) -> &'static str {
    match rank {
        4 => "!",  // stopped for a human
        3 => "ctx", // reported context use at least 50%
        2 => "lore", // verified recent source-session proposal signal
        1 => "new", // completed but unseen
        _ => "",
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct PaneSignal {
    pub count: usize,
    pub rank: u8,
    pub hidden_source: Option<(usize, String)>,
}
impl PaneSignal {
    pub fn badge(&self) -> String {
        let state = urgency_label(self.rank);
        if let Some((tab, title)) = &self.hidden_source {
            if self.count > 99 { format!(" [{} {state}#{tab}]", self.count) }
            else { format!(" [{} {state}#{tab}:{}]", self.count, clipped_title(title, 4).0) }
        } else if state.is_empty() {
            format!(" [{} tabs]", self.count)
        } else {
            format!(" [{} {state}]", self.count)
        }
    }
}

impl App {
    fn derived_project_label(&self, index: usize) -> &str {
        let session = &self.sessions[index];
        let fallback = session.collection.trim();
        let fallback = if fallback.is_empty() { "Other sessions" } else { fallback };
        match self.repo_cache.get(&session.id).and_then(|(status, _)| status.as_ref()) {
            Some(doxa_worktrees::RepoStatus::Repository { repo, .. }) => repo,
            Some(doxa_worktrees::RepoStatus::Directory { name }) => name,
            None => fallback,
        }
    }

    /// Editing a project heading needs every tab in the selected pane to
    /// resolve to the same root, both in the background snapshot and now.
    pub(super) fn active_verified_project_root(&self) -> Result<PathBuf, String> {
        let pane = self.groups.get(self.active_group).ok_or("select a local project pane")?;
        if pane.tabs.is_empty() { return Err("select a local project pane".into()); }
        let mut root: Option<PathBuf> = None;
        for id in &pane.tabs {
            if self.offline_ids.contains(id) || !self.sessions.iter().any(|session| session.id == *id) {
                return Err("project label unchanged: pane contains an offline or missing tab".into());
            }
            let cached = self.project_roots.get(id)
                .ok_or("project label unchanged: project root is unresolved")?;
            let cwd = self.session_cwds.get(id)
                .ok_or("project label unchanged: session directory is unresolved")?;
            if doxa_worktrees::project_root(cwd).as_ref() != Some(cached) {
                return Err("project label unchanged: project root changed; wait for a fresh probe".into());
            }
            if root.as_ref().is_some_and(|known| known != cached) {
                return Err("project label unchanged: pane mixes project roots".into());
            }
            root = Some(cached.clone());
        }
        root.ok_or_else(|| "select a local project pane".into())
    }

    pub(super) fn project_label_collides(&self, root: &Path, label: &str) -> bool {
        self.project_labels.as_ref().is_some_and(|labels| labels.iter().any(|(other, current)|
            other != root && current.eq_ignore_ascii_case(label)))
            || self.sessions.iter().enumerate().any(|(index, session)|
                !self.offline_ids.contains(&session.id)
                    && self.project_roots.get(&session.id).is_none_or(|other| other != root)
                    && self.rail_project_label(index).eq_ignore_ascii_case(label))
            || self.collections.iter().any(|item| item.name.eq_ignore_ascii_case(label))
    }

    pub(super) fn edit_project_label(&mut self, path: &Path, label: Option<&str>) -> Result<String, String> {
        let root = self.active_verified_project_root()?;
        if label.is_some_and(|name| self.project_label_collides(&root, name)) {
            return Err("project label unchanged: another group already uses that label".into());
        }
        save_project_label(path, &root, label)
            .map_err(|error| format!("project label unchanged: {error}"))?;
        self.project_labels = doxa_state::load_config_checked(path).ok()
            .and_then(|config| labels_from_config(&config));
        self.rail_sort_signature.clear();
        self.rail_sort_order.clear();
        Ok(match label {
            Some(label) => format!("Project label for {}: {label}", root.display()),
            None => format!("Project label cleared for {}", root.display()),
        })
    }

    /// Aggregate the existing urgency ranks over every tab in a pane. The
    /// active row carries the count and names a hidden source when it wins.
    pub(super) fn pane_signal(&self, active_id: &str) -> Option<PaneSignal> {
        let group = self.groups.iter().find(|group| group.active_id() == Some(active_id))?;
        if group.tabs.len() < 2 { return None; }
        let mut winner: Option<(u8, usize, &str)> = None;
        for (tab, id) in group.tabs.iter().enumerate() {
            let Some(index) = self.sessions.iter().position(|session| &session.id == id) else { continue };
            if self.offline_ids.contains(id) { continue; }
            let rank = self.rail_urgency(index);
            if winner.is_none_or(|(best, _, source)| rank > best || rank == best && id == active_id && source != active_id) {
                winner = Some((rank, tab, id));
            }
        }
        let (rank, tab, source) = winner.unwrap_or((0, group.active, active_id));
        let hidden_source = (rank > 0 && source != active_id)
            .then(|| self.sessions.iter().find(|session| session.id == source)
                .map(|session| (tab + 1, safe_label(&session.title)))
                .unwrap_or_else(|| (tab + 1, safe_label(source))));
        Some(PaneSignal { count: group.tabs.len(), rank, hidden_source })
    }

    pub(super) fn rail_project_label(&self, index: usize) -> &str {
        let session = &self.sessions[index];
        if let Some(root) = self.project_roots.get(&session.id) {
            let mixed_or_unknown_pane = self.groups.iter().enumerate()
                .find(|(_, pane)| pane.tabs.iter().any(|id| id == &session.id))
                .is_some_and(|(index, _)| !self.pane_project_marker(index).is_empty());
            if !mixed_or_unknown_pane {
                if let Some(label) = self.project_labels.as_ref().and_then(|labels| labels.get(root)) {
                    // A later session can introduce a heading collision after
                    // the edit was saved. Prefer the derived name in that case.
                    let collision = self.sessions.iter().enumerate().any(|(other, peer)|
                        index != other && !self.offline_ids.contains(&peer.id)
                            && self.project_roots.get(&peer.id) != Some(root)
                            && self.derived_project_label(other).eq_ignore_ascii_case(label));
                    if !collision { return label; }
                }
            }
        }
        self.derived_project_label(index)
    }

    /// A pane can mix projects, or include a tab whose root is not yet
    /// verified. Keep that provenance visible and withhold a project hue.
    pub(super) fn pane_project_marker(&self, group: usize) -> &'static str {
        let Some(pane) = self.groups.get(group) else { return " [root?]" };
        let mut root: Option<&Path> = None;
        for id in &pane.tabs {
            let Some(candidate) = self.project_roots.get(id).map(PathBuf::as_path) else { return " [root?]" };
            if root.is_some_and(|known| known != candidate) { return " [mixed]"; }
            root = Some(candidate);
        }
        if root.is_none() { " [root?]" } else { "" }
    }

    /// A display label can collide across unrelated roots. In that case (or
    /// while any root is unknown) the label remains, but its hue is withheld.
    pub(super) fn rail_project_colour(&self, label: &str) -> Option<Color> {
        let mut root: Option<&Path> = None;
        let mut found = false;
        for (index, session) in self.sessions.iter().enumerate() {
            if self.offline_ids.contains(&session.id)
                || self.collections.iter().any(|item| item.sessions.contains(&session.id))
                || self.rail_project_label(index) != label { continue; }
            found = true;
            let candidate = self.project_roots.get(&session.id)?.as_path();
            if self.preferences.value("rail_entries") == "panes" {
                if let Some(group) = self.groups.iter().position(|group| group.active_id() == Some(session.id.as_str())) {
                    if !self.pane_project_marker(group).is_empty() { return None; }
                }
            }
            if root.is_some_and(|known| known != candidate) { return None; }
            root = Some(candidate);
        }
        found.then(|| project_colour(root?, self.project_colours.as_ref()?)).flatten()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn assigned_colours_are_stable_and_invalid_overrides_are_unknown() {
        let root = Path::new("/work/customer/repo");
        let mut overrides = HashMap::new();
        assert_eq!(project_colour(root, &overrides), project_colour(root, &overrides));
        overrides.insert(root.to_path_buf(), "blue".into());
        assert_eq!(project_colour(root, &overrides), named("blue"));
        overrides.insert(root.to_path_buf(), "#333333".into());
        assert_eq!(project_colour(root, &overrides), None);
        let config = "[project_colours]\n'/work/customer/repo' = 'teal'\n"
            .parse::<toml::Table>().unwrap();
        let parsed = colours_from_config(&config).unwrap();
        assert_eq!(parsed.get(root).map(String::as_str), Some("teal"));
        let invalid = "[project_colours]\nrelative = 'blue'\n".parse::<toml::Table>().unwrap();
        assert!(colours_from_config(&invalid).is_none());
    }

    #[test]
    fn project_labels_validate_and_preserve_other_owner_config() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().join("project");
        std::fs::create_dir(&root).unwrap();
        let root = root.canonicalize().unwrap();
        let path = directory.path().join("owner").join("config.toml");
        doxa_state::update_config(&path, |config| {
            config.insert("future_setting".into(), toml::Value::String("kept".into()));
            Ok(())
        }).unwrap();
        save_project_label(&path, &root, Some("  Client   Work  ")).unwrap();
        let stored = doxa_state::load_config_checked(&path).unwrap();
        assert_eq!(stored["future_setting"].as_str(), Some("kept"));
        assert_eq!(labels_from_config(&stored).unwrap().get(&root).map(String::as_str), Some("Client Work"));
        assert!(save_project_label(&path, &root, Some("\u{202e}spoof")).is_err());
        assert_eq!(doxa_state::load_config_checked(&path).unwrap(), stored);
        save_project_label(&path, &root, None).unwrap();
        let cleared = doxa_state::load_config_checked(&path).unwrap();
        assert!(cleared.get("project_labels").is_none());
        assert_eq!(cleared["future_setting"].as_str(), Some("kept"));
        let invalid = "[project_labels]\nrelative = 'Wrong'\n".parse::<toml::Table>().unwrap();
        assert!(labels_from_config(&invalid).is_none());
        let duplicate = "[project_labels]\n'/one' = 'Same'\n'/two' = 'same'\n"
            .parse::<toml::Table>().unwrap();
        assert!(labels_from_config(&duplicate).is_none());
        let malformed = b"project_labels = 7\nfuture_setting = 'kept'\n";
        std::fs::write(&path, malformed).unwrap();
        assert!(save_project_label(&path, &root, Some("Valid")).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), malformed);
    }
}
