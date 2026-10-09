//! Read-only rail signals. Project identity and urgency use separate channels.
use super::{clipped_title, safe_label, App};
use ratatui::style::Color;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
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
        let fallback = session.collection.trim();
        let fallback = if fallback.is_empty() { "Other sessions" } else { fallback };
        match self.repo_cache.get(&session.id).and_then(|(status, _)| status.as_ref()) {
            Some(doxa_worktrees::RepoStatus::Repository { repo, .. }) => repo,
            Some(doxa_worktrees::RepoStatus::Directory { name }) => name,
            None => fallback,
        }
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
}
