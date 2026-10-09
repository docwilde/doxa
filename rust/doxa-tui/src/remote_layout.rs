//! Opt-in, owner-scoped remote-only pane persistence. No transcript, draft,
//! approval, retry ID or event cursor is ever stored here.
use crate::remote_client::valid_target;
use crate::ui::{panes::{Tree, MAX_DEPTH, MAX_PANES}, App, PaneGroup, Split};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

const MAX_FILE: u64 = 64 * 1024;
const MAX_REMOTE_TABS: usize = 64;

fn invalid(message: &'static str) -> io::Error { io::Error::new(io::ErrorKind::InvalidData, message) }
fn private(meta: &fs::Metadata) -> bool {
    meta.uid() == unsafe { libc::geteuid() } && meta.mode() & 0o077 == 0
}
fn split_name(value: Split) -> &'static str {
    match value { Split::Vertical => "row", Split::Horizontal => "column" }
}
fn split_value(value: &str) -> Option<Split> {
    match value { "row" => Some(Split::Vertical), "column" => Some(Split::Horizontal), _ => None }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Identity { pub id: String, pub incarnation: String }

#[derive(Clone, Debug)]
pub(crate) struct Inventory { pub owner: String, pub sessions: Vec<Identity> }
impl Inventory {
    pub fn parse(value: &Value) -> io::Result<Self> {
        let owner = value["owner"].as_str().filter(|owner| !owner.is_empty() && owner.len() <= 255
            && !owner.chars().any(char::is_control)).ok_or_else(|| invalid("authenticated hub owner missing"))?;
        let rows = value["sessions"].as_array().filter(|rows| rows.len() <= MAX_REMOTE_TABS)
            .ok_or_else(|| invalid("invalid remote inventory"))?;
        let mut seen = HashSet::new();
        let sessions = rows.iter().map(|row| {
            let id = row["id"].as_str().filter(|id| valid_target(id))
                .ok_or_else(|| invalid("invalid remote session ID"))?;
            let incarnation = row["incarnation"].as_str().filter(|incarnation| !incarnation.is_empty()
                && incarnation.len() <= 64 && !incarnation.chars().any(char::is_control))
                .ok_or_else(|| invalid("remote session incarnation missing"))?;
            if !seen.insert(id) { return Err(invalid("duplicate remote session ID")); }
            Ok(Identity { id: id.into(), incarnation: incarnation.into() })
        }).collect::<io::Result<Vec<_>>>()?;
        Ok(Self { owner: owner.into(), sessions })
    }
    fn keys(&self) -> HashMap<&str, &str> {
        self.sessions.iter().map(|session| (session.id.as_str(), session.incarnation.as_str())).collect()
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Group { tabs: Vec<String>, active: Option<String> }

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum SavedTree {
    Group { index: usize },
    Split { orientation: String, children: Vec<SavedTree>, weights: Vec<u16> },
}
impl SavedTree {
    fn capture(tree: &Tree) -> Self {
        match tree {
            Tree::Group(index) => Self::Group { index: *index },
            Tree::Split { orientation, children, weights } => Self::Split {
                orientation: split_name(*orientation).into(),
                children: children.iter().map(Self::capture).collect(), weights: weights.clone(),
            },
        }
    }
    fn decode(&self, groups: usize, depth: usize, seen: &mut HashSet<usize>) -> Option<Tree> {
        if depth > MAX_DEPTH { return None; }
        match self {
            Self::Group { index } if *index < groups && seen.insert(*index) => Some(Tree::Group(*index)),
            Self::Split { orientation, children, weights }
                if (2..=MAX_PANES).contains(&children.len()) && children.len() == weights.len()
                    && weights.iter().all(|weight| *weight > 0) => {
                let orientation = split_value(orientation)?;
                let children = children.iter().map(|child| child.decode(groups, depth + 1, seen))
                    .collect::<Option<Vec<_>>>()?;
                Some(Tree::Split { orientation, children, weights: weights.clone() })
            }
            _ => None,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Layout {
    version: u8,
    hub: String,
    owner: String,
    sessions: Vec<Identity>,
    groups: Vec<Group>,
    tree: Option<SavedTree>,
    active_group: usize,
    split: String,
    split_percent: u16,
    rail_visible: bool,
    rail_width: u16,
}
impl Layout {
    fn validate(&self, hub: &str, owner: &str) -> io::Result<()> {
        if self.version != 1 || self.hub != hub || self.owner != owner
            || self.sessions.len() > MAX_REMOTE_TABS || self.groups.is_empty()
            || self.groups.len() > MAX_PANES || self.active_group >= self.groups.len()
            || split_value(&self.split).is_none() || !(20..=80).contains(&self.split_percent)
            || !(12..=44).contains(&self.rail_width) {
            return Err(invalid("remote layout scope or geometry invalid"));
        }
        let identities: HashMap<_, _> = self.sessions.iter().map(|row| (row.id.as_str(), row.incarnation.as_str())).collect();
        if identities.len() != self.sessions.len() || self.sessions.iter().any(|row|
            !valid_target(&row.id) || row.incarnation.is_empty() || row.incarnation.len() > 64
                || row.incarnation.chars().any(char::is_control)) {
            return Err(invalid("remote layout identity invalid"));
        }
        let mut tabs = HashSet::new();
        for group in &self.groups {
            if group.active.as_ref().is_some_and(|active| !group.tabs.contains(active))
                || (!group.tabs.is_empty() && group.active.is_none()) {
                return Err(invalid("remote layout active tab invalid"));
            }
            for id in &group.tabs {
                if !identities.contains_key(id.as_str()) || !tabs.insert(id.as_str()) {
                    return Err(invalid("remote layout tab identity invalid"));
                }
            }
        }
        if tabs.len() > MAX_REMOTE_TABS { return Err(invalid("remote layout exceeds tab bound")); }
        if let Some(tree) = &self.tree {
            let mut seen = HashSet::new();
            tree.decode(self.groups.len(), 0, &mut seen)
                .filter(|_| seen.len() == self.groups.len())
                .ok_or_else(|| invalid("remote layout tree invalid"))?;
        } else if self.groups.len() > 2 {
            return Err(invalid("remote layout tree missing"));
        }
        Ok(())
    }
    fn capture(app: &App, hub: &str, inventory: &Inventory) -> io::Result<Self> {
        let live = inventory.keys();
        let groups = app.groups.iter().map(|group| {
            if group.tabs.iter().any(|id| !live.contains_key(id.as_str())) {
                return Err(invalid("remote layout has an unverified tab"));
            }
            Ok(Group { tabs: group.tabs.clone(), active: group.tabs.get(group.active).cloned() })
        }).collect::<io::Result<Vec<_>>>()?;
        let open = groups.iter().flat_map(|group| group.tabs.iter()).collect::<HashSet<_>>();
        let layout = Self { version: 1, hub: hub.into(), owner: inventory.owner.clone(),
            sessions: inventory.sessions.iter().filter(|row| open.contains(&row.id)).cloned().collect(), groups,
            tree: app.pane_tree.as_ref().map(SavedTree::capture), active_group: app.active_group,
            split: split_name(app.split).into(), split_percent: app.split_percent,
            rail_visible: app.rail_visible, rail_width: app.rail_width };
        layout.validate(hub, &inventory.owner)?;
        Ok(layout)
    }
    fn project(&self, app: &mut App, inventory: &Inventory) -> bool {
        let live = inventory.keys();
        let saved = self.sessions.iter().map(|row| (row.id.as_str(), row.incarnation.as_str()))
            .collect::<HashMap<_, _>>();
        let mut groups = self.groups.iter().map(|group| {
            let tabs = group.tabs.iter().filter(|id| live.get(id.as_str()) == saved.get(id.as_str()))
                .cloned().collect::<Vec<_>>();
            let active = group.active.as_ref().and_then(|id| tabs.iter().position(|tab| tab == id)).unwrap_or(0);
            PaneGroup { tabs, active, scroll: 0 }
        }).collect::<Vec<_>>();
        if groups.iter().all(|group| group.tabs.is_empty()) { return false; }
        let mut tree = self.tree.as_ref().and_then(|tree| {
            let mut seen = HashSet::new(); tree.decode(groups.len(), 0, &mut seen)
        });
        let mut active = self.active_group;
        for index in (0..groups.len()).rev() {
            if !self.groups[index].tabs.is_empty() && groups[index].tabs.is_empty() && groups.len() > 1 {
                groups.remove(index);
                tree = tree.and_then(|tree| tree.without(index));
                if index < active { active -= 1; }
            }
        }
        let active = active.min(groups.len() - 1);
        let (split, percent) = match &tree {
            Some(Tree::Split { orientation, weights, .. }) => (*orientation,
                (u32::from(weights[0]) * 100 / weights.iter().map(|w| u32::from(*w)).sum::<u32>().max(1)) as u16),
            _ => (split_value(&self.split).unwrap_or(Split::Vertical), self.split_percent),
        };
        while groups.len() < 2 { groups.push(PaneGroup { tabs: vec![], active: 0, scroll: 0 }); }
        app.groups = groups;
        app.pane_tree = if app.groups.len() > 2 { tree } else { None };
        app.active_group = active;
        app.split = split;
        app.split_percent = percent.clamp(20, 80);
        app.rail_visible = self.rail_visible;
        app.rail_width = self.rail_width;
        true
    }
}

pub(crate) struct Store {
    hub: String,
    initial: Inventory,
    path: PathBuf,
    loaded_bytes: Option<Vec<u8>>,
    layout: Option<Layout>,
    restored: bool,
}
impl Store {
    pub(crate) fn open(home: &Path, hub: &str, initial: Inventory) -> io::Result<Self> {
        let base = crate::remote_client::hub_url(hub)?;
        let hub = base.as_str().to_owned();
        let digest = Sha256::digest(format!("{hub}\0{}", initial.owner).as_bytes());
        let directory = home.join("remote-layouts");
        fs::DirBuilder::new().recursive(true).mode(0o700).create(&directory)?;
        let path = directory.join(format!("{:x}.json", digest));
        let mut store = Self { hub, initial, path, loaded_bytes: None, layout: None, restored: false };
        Self::with_lock(&store.path, || {
            store.loaded_bytes = Self::read_checked(&store.path)?;
            if let Some(bytes) = &store.loaded_bytes {
                let layout: Layout = serde_json::from_slice(bytes).map_err(|_| invalid("remote layout JSON invalid"))?;
                layout.validate(&store.hub, &store.initial.owner)?;
                store.layout = Some(layout);
            }
            Ok(())
        })?;
        Ok(store)
    }
    pub(crate) fn ready(&self, app: &App) -> bool {
        self.initial.sessions.iter().all(|row| app.sessions.iter().any(|session| session.id == row.id))
    }
    pub(crate) fn restore_if_ready(&mut self, app: &mut App) -> bool {
        if self.restored || !self.ready(app) { return false; }
        self.restored = true;
        self.layout.as_ref().is_some_and(|layout| layout.project(app, &self.initial))
    }
    pub(crate) fn save_checked(&mut self, app: &App, fresh: Inventory) -> io::Result<()> {
        if !self.restored || !self.ready(app) || fresh.owner != self.initial.owner {
            return Err(invalid("remote layout roster incomplete"));
        }
        let current = fresh.keys();
        let initial = self.initial.keys();
        if app.groups.iter().flat_map(|group| group.tabs.iter()).any(|id|
            current.get(id.as_str()).is_none() || initial.get(id.as_str()).is_some_and(|old|
                current.get(id.as_str()) != Some(old))) {
            return Err(invalid("remote layout session identity changed"));
        }
        let next = Layout::capture(app, &self.hub, &fresh)?;
        let bytes = serde_json::to_vec(&next).map_err(io::Error::other)?;
        if bytes.len() as u64 > MAX_FILE { return Err(invalid("remote layout exceeds file bound")); }
        let path = self.path.clone();
        Self::with_lock(&path, || {
            if Self::read_checked(&path)? != self.loaded_bytes {
                return Err(io::Error::new(io::ErrorKind::AlreadyExists, "remote layout changed in another window"));
            }
            let parent = path.parent().ok_or_else(|| invalid("remote layout directory missing"))?;
            let mut temp = tempfile::Builder::new().prefix(".remote-layout-").tempfile_in(parent)?;
            temp.as_file().set_permissions(fs::Permissions::from_mode(0o600))?;
            temp.write_all(&bytes)?;
            temp.as_file().sync_all()?;
            temp.persist(&path).map_err(|error| error.error)?;
            File::open(parent)?.sync_all()?;
            self.loaded_bytes = Some(bytes);
            self.layout = Some(next);
            Ok(())
        })
    }
    fn read_checked(path: &Path) -> io::Result<Option<Vec<u8>>> {
        let mut file = match OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        let meta = file.metadata()?;
        if !meta.is_file() || meta.nlink() != 1 || !private(&meta) || meta.len() > MAX_FILE {
            return Err(invalid("untrusted remote layout file"));
        }
        let mut bytes = Vec::new();
        Read::by_ref(&mut file).take(MAX_FILE + 1).read_to_end(&mut bytes)?;
        if bytes.len() as u64 > MAX_FILE { return Err(invalid("remote layout exceeds file bound")); }
        Ok(Some(bytes))
    }
    fn with_lock<T>(path: &Path, action: impl FnOnce() -> io::Result<T>) -> io::Result<T> {
        let parent = path.parent().ok_or_else(|| invalid("remote layout directory missing"))?;
        let dir = OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW).open(parent)?;
        if !private(&dir.metadata()?) { return Err(invalid("remote layout directory must be private")); }
        let lock_path = path.with_extension("lock");
        let lock = OpenOptions::new().read(true).write(true).create(true).mode(0o600)
            .custom_flags(libc::O_NOFOLLOW).open(lock_path)?;
        let meta = lock.metadata()?;
        if !meta.is_file() || meta.nlink() != 1 || !private(&meta) { return Err(invalid("untrusted remote layout lock")); }
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } != 0 { return Err(io::Error::last_os_error()); }
        action()
    }
    pub(crate) fn hub(&self) -> &str { &self.hub }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn inventory(owner: &str, incarnation: &str) -> Inventory {
        Inventory::parse(&json!({"owner":owner,"sessions":[
            {"id":"host~a","incarnation":incarnation},{"id":"host~b","incarnation":"second"}]})).unwrap()
    }
    fn hello(app: &mut App, id: &str) {
        app.apply_worker_frame(crate::worker_frames::WorkerFrame::Daemon { session_id: id.into(),
            frame: json!({"type":"hello","session_id":id,"title":id,"remote":true}) });
    }
    #[test]
    fn owner_incarnation_and_live_inventory_gate_restore_without_cursor() {
        let dir = tempfile::tempdir().unwrap();
        let hub = "https://hub.tail.ts.net/";
        let initial = inventory("one@example.com", "first");
        let mut store = Store::open(dir.path(), hub, initial.clone()).unwrap();
        let mut app = App::default();
        hello(&mut app, "host~a"); hello(&mut app, "host~b");
        app.groups[0].tabs = vec!["host~b".into(), "host~a".into()]; app.groups[0].active = 1;
        store.restore_if_ready(&mut app);
        store.save_checked(&app, initial.clone()).unwrap();
        let bytes = fs::read(&store.path).unwrap();
        assert!(!String::from_utf8_lossy(&bytes).contains("cursor"));
        let mut reopened = Store::open(dir.path(), hub, initial).unwrap();
        let mut restored = App::default();
        hello(&mut restored, "host~a");
        assert!(!reopened.restore_if_ready(&mut restored));
        hello(&mut restored, "host~b");
        assert!(reopened.restore_if_ready(&mut restored));
        assert_eq!(restored.groups[0].tabs, ["host~b", "host~a"]);
        assert_eq!(restored.groups[0].active, 1);
        let mut changed = Store::open(dir.path(), hub, inventory("one@example.com", "replaced")).unwrap();
        let mut app = App::default();
        hello(&mut app, "host~a"); hello(&mut app, "host~b");
        assert!(changed.restore_if_ready(&mut app));
        assert_eq!(app.groups[0].tabs, ["host~b"]);
        changed.save_checked(&app, inventory("one@example.com", "replaced")).unwrap();
        assert_eq!(changed.layout.as_ref().unwrap().sessions.len(), 1);
        assert!(Store::open(dir.path(), hub, inventory("other@example.com", "first")).unwrap().layout.is_none());
    }
    #[test]
    fn stale_or_cross_owner_roster_and_concurrent_writer_cannot_replace_layout() {
        let dir = tempfile::tempdir().unwrap(); let hub = "https://hub.tail.ts.net/";
        let initial = inventory("one@example.com", "first");
        let mut first = Store::open(dir.path(), hub, initial.clone()).unwrap();
        let mut second = Store::open(dir.path(), hub, initial.clone()).unwrap();
        let mut app = App::default();
        hello(&mut app, "host~a"); hello(&mut app, "host~b");
        first.restore_if_ready(&mut app); second.restore_if_ready(&mut app);
        assert!(second.save_checked(&app, inventory("other@example.com", "first")).is_err());
        assert!(second.save_checked(&app, inventory("one@example.com", "replaced")).is_err());
        first.save_checked(&app, initial.clone()).unwrap();
        assert_eq!(second.save_checked(&app, initial).unwrap_err().kind(), io::ErrorKind::AlreadyExists);
    }
    #[test]
    fn nested_panes_keep_geometry_and_prune_replaced_session() {
        let initial = inventory("one@example.com", "first");
        let hub = "https://hub.tail.ts.net/";
        let mut app = App::default();
        app.groups[0].tabs = vec!["host~a".into()];
        app.groups[1].tabs = vec!["host~b".into()];
        app.groups.push(PaneGroup { tabs: vec![], active: 0, scroll: 0 });
        app.active_group = 1;
        let mut tree = Tree::pair(Split::Vertical, 40);
        assert!(tree.split(1, 2, Split::Horizontal, 0));
        app.pane_tree = Some(tree.clone());
        let layout = Layout::capture(&app, hub, &initial).unwrap();
        let mut restored = App::default();
        assert!(layout.project(&mut restored, &initial));
        assert_eq!(restored.groups.len(), 3);
        assert_eq!(restored.pane_tree, Some(tree));
        let changed = inventory("one@example.com", "replaced");
        assert!(layout.project(&mut restored, &changed));
        assert_eq!(restored.groups[0].tabs, ["host~b"]);
        assert_eq!(restored.active_group, 0);
        assert!(restored.groups[1].tabs.is_empty());
        assert!(restored.pane_tree.is_none());
    }
    #[test]
    fn newly_discovered_tab_uses_fresh_incarnation_at_save() {
        let dir = tempfile::tempdir().unwrap();
        let initial = inventory("one@example.com", "first");
        let mut store = Store::open(dir.path(), "https://hub.tail.ts.net/", initial).unwrap();
        let mut app = App::default();
        hello(&mut app, "host~a"); hello(&mut app, "host~b");
        store.restore_if_ready(&mut app);
        hello(&mut app, "host~c");
        app.groups[1].tabs.push("host~c".into());
        let fresh = Inventory::parse(&json!({"owner":"one@example.com","sessions":[
            {"id":"host~a","incarnation":"first"},
            {"id":"host~b","incarnation":"second"},
            {"id":"host~c","incarnation":"third"}]})).unwrap();
        store.save_checked(&app, fresh).unwrap();
        assert!(store.layout.as_ref().unwrap().sessions.iter().any(|row|
            row.id == "host~c" && row.incarnation == "third"));
    }
    #[test]
    fn malformed_scopes_and_untrusted_file_are_rejected() {
        assert!(Inventory::parse(&json!({"owner":"one","sessions":[{"id":"host~a"}]})).is_err());
        let dir = tempfile::tempdir().unwrap(); let hub = "https://hub.tail.ts.net/";
        let inventory = inventory("one@example.com", "first");
        let store = Store::open(dir.path(), hub, inventory.clone()).unwrap();
        fs::write(&store.path, br#"{"version":1,"cursor":99}"#).unwrap();
        assert!(Store::open(dir.path(), hub, inventory.clone()).is_err());
        fs::remove_file(&store.path).unwrap();
        std::os::unix::fs::symlink("/etc/passwd", &store.path).unwrap();
        assert!(Store::open(dir.path(), hub, inventory).is_err());
    }
}
