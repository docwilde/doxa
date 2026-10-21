use doxa_state::{legacy_tabset_path, save_tabset, Tab, TabSet};
use crossterm::event::{Event, KeyCode, KeyEvent, KeyModifiers};
use doxa_tui::ui::{App, Split};
use doxa_tui::ui_state::UiStateStore;
use serde_json::{json, Value};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::sync::Mutex;

fn seeded(raw: Value) -> (tempfile::TempDir, UiStateStore) {
    let dir = tempfile::tempdir().unwrap();
    let probe = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    let data = raw.as_object().unwrap().clone();
    let tabs = data["tabs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| Tab {
            session_id: r["session_id"].as_str().unwrap().into(),
            pinned_name: r["pinned_name"].as_str().map(str::to_owned),
            cwd: r["cwd"].as_str().map(str::to_owned),
        })
        .collect();
    let record = TabSet {
        scope_key: "/repo".into(),
        active_session_id: data
            .get("active_session_id")
            .and_then(Value::as_str)
            .map(str::to_owned),
        tabs,
        raw: data,
    };
    save_tabset(probe.path(), &record).unwrap();
    let store = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    (dir, store)
}
fn live(ids: &[&str]) -> Vec<String> {
    ids.iter().map(|s| (*s).into()).collect()
}

#[test]
fn scope_open_mints_machine_identity_and_adopts_legacy_tabset() {
    let dir = tempfile::tempdir().unwrap();
    fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
    fs::create_dir(dir.path().join("tabsets")).unwrap();
    fs::set_permissions(
        dir.path().join("tabsets"),
        fs::Permissions::from_mode(0o700),
    )
    .unwrap();
    let legacy = legacy_tabset_path(dir.path(), "/repo");
    let record = TabSet {
        scope_key: "/repo".into(),
        active_session_id: Some("a".into()),
        tabs: vec![Tab {
            session_id: "a".into(),
            pinned_name: None,
            cwd: None,
        }],
        raw: json!({"scope_key":"/repo","tabs":[{"session_id":"a"}],"active_session_id":"a"})
            .as_object()
            .unwrap()
            .clone(),
    };
    save_tabset(&legacy, &record).unwrap();
    let store = UiStateStore::for_scope(dir.path(), "/repo").unwrap();
    assert!(dir.path().join("machine-id").exists());
    assert!(store.path().exists());
    assert!(!legacy.exists());
    let mut app = App::default();
    assert!(store.restore(&mut app, &live(&["a"])));
}

#[test]
fn incomplete_roster_preserves_existing_tabset_byte_for_byte() {
    let (_dir, mut store) = seeded(
        json!({"tabs":[{"session_id":"live"},{"session_id":"temporarily-missing"}],
        "active_session_id":"temporarily-missing"}),
    );
    let original = fs::read(store.path()).unwrap();
    let mut app = App::default();
    assert!(store.restore(&mut app, &live(&["live"])));
    app.rail_width = 32;
    assert!(!store.save_if_complete(&app, &Mutex::new(false)).unwrap());
    assert_eq!(fs::read(store.path()).unwrap(), original);
}

#[test]
fn flat_legacy_record_keeps_offline_tab_when_live_layout_changes() {
    let (_dir, mut store) = seeded(
        json!({"tabs":[{"session_id":"a"},{"session_id":"stale"},{"session_id":"b"}],"active_session_id":"b"}),
    );
    let mut app = App::default();
    assert!(store.restore(&mut app, &live(&["a", "b"])));
    assert_eq!(app.groups[0].tabs, ["a", "stale", "b"]);
    assert_eq!(app.groups[0].active, 2);
    assert!(app.groups[1].tabs.is_empty());
    app.rail_width = 31;
    assert!(store.save_if_complete(&app, &Mutex::new(true)).unwrap());
    let saved: Value = serde_json::from_slice(&fs::read(store.path()).unwrap()).unwrap();
    assert_eq!(saved["tabs"].as_array().unwrap().len(), 3);
    assert_eq!(saved["tabs"][1]["session_id"], "stale");
    assert_eq!(saved["active_session_id"], "b");
}

#[test]
fn archived_middle_tab_and_collection_survive_split_mutation() {
    let (_dir, mut store) = seeded(json!({
        "tabs":[{"session_id":"live-a"},{"session_id":"archived","pinned_name":"Old work","cwd":"/repo"},{"session_id":"live-b"}],
        "active_session_id":"live-b",
        "layout":{"kind":"tabs","groups":{"kind":"group","active":2,"tabs":[
            {"kind":"leaf","session_id":"live-a"},
            {"kind":"leaf","session_id":"archived","view":"diff"},
            {"kind":"leaf","session_id":"live-b"}]}},
        "collections":[{"name":"Keep","sessions":["live-a","archived","live-b"]}]
    }));
    let mut app = App::default();
    assert!(store.restore(&mut app, &live(&["live-a", "live-b"])));
    assert_eq!(app.groups[0].tabs, ["live-a", "archived", "live-b"]);
    app.groups[0].tabs.pop();
    app.groups[1].tabs.push("live-b".into());
    app.groups[0].active = 0;
    app.groups[1].active = 0;
    app.active_group = 1;
    app.split_percent = 65;
    app.rail_width = 34;
    assert!(store.save_if_complete(&app, &Mutex::new(true)).unwrap());
    let saved: Value = serde_json::from_slice(&fs::read(store.path()).unwrap()).unwrap();
    assert_eq!(
        saved["tabs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row["session_id"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["live-a", "archived", "live-b"]
    );
    assert_eq!(saved["layout"]["groups"]["children"][0]["tabs"][1]["view"], "diff");
    assert_eq!(
        saved["collections"][0]["sessions"],
        json!(["live-a", "archived", "live-b"])
    );
}

#[test]
fn grouped_split_restores_geometry_and_preserves_leaf_metadata() {
    let (_dir, mut store) = seeded(
        json!({"tabs":[{"session_id":"a"},{"session_id":"b"},{"session_id":"c"}],"active_session_id":"c",
      "layout":{"kind":"tabs","groups":{"kind":"split","orientation":"column","weights":[0.7,0.3],"children":[
        {"kind":"group","active":1,"tabs":[{"kind":"leaf","session_id":"a","view":"diff"},{"kind":"leaf","session_id":"b","prompt_ratio":0.3}]},
        {"kind":"group","active":0,"tabs":[{"kind":"leaf","session_id":"c"}]}
      ]}},"collections":[{"name":"work","sessions":["a","stale"]}],"future_key":42}),
    );
    let mut app = App::default();
    assert!(store.restore(&mut app, &live(&["a", "b", "c"])));
    assert_eq!(app.split, Split::Horizontal);
    assert_eq!(app.split_percent, 70);
    assert_eq!(app.groups[0].tabs, ["a", "b"]);
    assert_eq!(app.groups[0].active, 1);
    assert_eq!(app.active_group, 1);
    app.rail_width = 33;
    app.rail_visible = false;
    app.split_percent = 62;
    store.save(&app).unwrap();
    let saved: Value = serde_json::from_slice(&fs::read(store.path()).unwrap()).unwrap();
    assert_eq!(saved["layout"]["groups"]["weights"][0], 0.62);
    assert_eq!(saved["layout"]["groups"]["orientation"], "column");
    assert_eq!(
        saved["layout"]["groups"]["children"][0]["tabs"][0]["view"],
        "diff"
    );
    assert_eq!(
        saved["layout"]["groups"]["children"][0]["tabs"][1]["prompt_ratio"],
        0.3
    );
    assert_eq!(saved["collections"][0]["sessions"], json!(["a"]));
    assert_eq!(
        saved["rust_ui"],
        json!({"rail_width":33,"rail_visible":false})
    );
    assert_eq!(saved["future_key"], 42);
    let reload = UiStateStore::new(_dir.path(), "/repo", "machine").unwrap();
    let mut again = App::default();
    assert!(reload.restore(&mut again, &live(&["a", "b", "c"])));
    assert_eq!(again.rail_width, 33);
    assert!(!again.rail_visible);
    assert_eq!(again.split_percent, 62);
}

#[test]
fn old_tree_layout_restores_two_panes_and_flat_extra_tabs() {
    let (_dir, store) = seeded(
        json!({"tabs":[{"session_id":"a"},{"session_id":"b"},{"session_id":"c"}],"active_session_id":"b",
        "layout":{"kind":"tabs","trees":[{"kind":"split","orientation":"row","weights":[0.4,0.6],"children":[
        {"kind":"leaf","session_id":"a"},{"kind":"leaf","session_id":"b"}]}]}}),
    );
    let mut app = App::default();
    assert!(store.restore(&mut app, &live(&["a", "b", "c"])));
    assert_eq!(app.split, Split::Vertical);
    assert_eq!(app.split_percent, 40);
    assert_eq!(app.groups[0].tabs, ["a", "c"]);
    assert_eq!(app.groups[1].tabs, ["b"]);
    assert_eq!(app.active_group, 1);
}

#[test]
fn complex_future_layout_is_readable_but_never_rewritten() {
    let (_dir, mut store) = seeded(
        json!({"tabs":[{"session_id":"a"},{"session_id":"b"}],"active_session_id":"a",
        "layout":{"kind":"tabs","groups":{"kind":"split","orientation":"row","weights":[0.2,0.3,0.5],"children":[
            {"kind":"group","tabs":[{"kind":"leaf","session_id":"a"}]},
            {"kind":"group","tabs":[{"kind":"leaf","session_id":"b"}]},
            {"kind":"group","tabs":[{"kind":"leaf","session_id":"z"}]}
        ]}}}),
    );
    let original = fs::read(store.path()).unwrap();
    let mut app = App::default();
    assert!(store.restore(&mut app, &live(&["a", "b"])));
    assert_eq!(app.groups[0].tabs, ["a", "b"]);
    assert!(store.save(&app).is_err());
    assert_eq!(fs::read(store.path()).unwrap(), original);
}

#[test]
fn no_live_saved_tab_remains_visible_with_unavailable_reason() {
    let (_dir, store) = seeded(json!({"tabs":[{"session_id":"old"}],"active_session_id":"old"}));
    let mut app = App::default();
    assert!(store.restore(&mut app, &live(&["current"])));
    assert_eq!(app.groups[0].tabs, ["old"]);
    assert!(app.sessions[0].status.contains("unavailable"));
}

#[test]
fn new_live_tab_preserves_metadata_from_saved_empty_tabset() {
    let (_dir, mut store) = seeded(json!({"tabs":[],"active_session_id":null,
        "layout":{"kind":"tabs","tabs":[],"future_layout":"keep"},
        "collections":[{"name":"Archive","sessions":[]}],"future_key":42}));
    let mut app = App::default();
    app.groups[0].tabs.push("new-session".into());
    store.save(&app).unwrap();
    let saved: Value = serde_json::from_slice(&fs::read(store.path()).unwrap()).unwrap();
    assert_eq!(saved["tabs"][0]["session_id"], "new-session");
    assert_eq!(saved["layout"]["future_layout"], "keep");
    assert_eq!(saved["collections"][0]["name"], "Archive");
    assert_eq!(saved["future_key"], 42);
}

#[test]
fn transcript_scroll_does_not_count_as_layout_change() {
    let mut app = App::default();
    let before = doxa_tui::ui_state::LayoutSignature::capture(&app);
    app.groups[0].scroll = 100;
    assert_eq!(before, doxa_tui::ui_state::LayoutSignature::capture(&app));
}

#[test]
fn duplicate_session_in_two_panes_cannot_corrupt_group_record() {
    let (_dir, mut store) = seeded(json!({"tabs":[{"session_id":"a"}]}));
    let original = fs::read(store.path()).unwrap();
    let mut app = App::default();
    app.groups[0].tabs.push("a".into());
    app.groups[1].tabs.push("a".into());
    assert!(store.save(&app).is_err());
    assert_eq!(fs::read(store.path()).unwrap(), original);
}

#[test]
fn unavailable_first_pane_preserves_live_second_pane_and_focus() {
    let (_dir, store) = seeded(
        json!({"tabs":[{"session_id":"old"},{"session_id":"live"}],"active_session_id":"live",
        "layout":{"kind":"tabs","groups":{"kind":"split","orientation":"row","weights":[0.5,0.5],"children":[
            {"kind":"group","active":0,"tabs":[{"kind":"leaf","session_id":"old"}]},
            {"kind":"group","active":0,"tabs":[{"kind":"leaf","session_id":"live"}]}
        ]}}}),
    );
    let mut app = App::default();
    assert!(store.restore(&mut app, &live(&["live"])));
    assert_eq!(app.groups[0].tabs, ["old"]);
    assert_eq!(app.groups[1].tabs, ["live"]);
    assert_eq!(app.active_group, 1);
    assert!(app.sessions[0].status.contains("unavailable"));
}

#[test]
fn moved_tab_layout_saves_and_restores_active_group() {
    let (dir, mut store) = seeded(json!({"tabs":[{"session_id":"a"},{"session_id":"b"},{"session_id":"c"}],
        "active_session_id":"a"}));
    let mut app = App::default();
    assert!(store.restore(&mut app, &live(&["a", "b", "c"])));
    app.input = "/movepane 2".into();
    app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
    assert_eq!(app.groups[0].tabs, ["b", "c"]);
    assert_eq!(app.groups[1].tabs, ["a"]);
    store.save(&app).unwrap();
    let again_store = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    let mut again = App::default();
    assert!(again_store.restore(&mut again, &live(&["a", "b", "c"])));
    assert_eq!(again.groups[0].tabs, ["b", "c"]);
    assert_eq!(again.groups[1].tabs, ["a"]);
    assert_eq!(again.active_group, 1);
    assert_eq!(again.groups[1].active, 0);
}

fn recovery_session(id: &str) -> doxa_tui::discovery::Session {
    doxa_tui::discovery::Session { id: id.into(), title: id.into(), socket: "/unused".into(),
        scope_key: "/repo".into(), clients: Some(0), started_at: "2026".into() }
}

#[test]
fn recovery_registration_reloads_stale_windows_and_opens_both_before_provider_reply() {
    let dir = tempfile::tempdir().unwrap();
    let mut first = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    let mut second = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    first.remember_session(&recovery_session("first")).unwrap();
    second.remember_session(&recovery_session("second")).unwrap();
    let restored = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    assert_eq!(restored.saved_tabs().unwrap().iter().map(|tab|tab.session_id.as_str()).collect::<Vec<_>>(), ["first", "second"]);
    let mut app = App::default();
    assert!(restored.restore(&mut app, &live(&["first", "second"])));
    assert_eq!(app.groups[0].tabs, ["first", "second"]);
    assert!(!dir.path().join("first.codex.json").exists());
}

#[test]
fn additive_recovery_preserves_complex_archive_layout_and_partial_window_never_prunes_it() {
    let (_dir, mut full) = seeded(json!({"tabs":[{"session_id":"archived","pinned_name":"Keep"}],
        "layout":{"kind":"tabs","groups":{"kind":"future-layout","owned":"unchanged"}},
        "collections":[{"name":"Keep collection","sessions":["archived"]}],"future_field":{"keep":true}}));
    let before: Value = serde_json::from_slice(&fs::read(full.path()).unwrap()).unwrap();
    full.remember_session(&recovery_session("running")).unwrap();
    let after = fs::read(full.path()).unwrap();
    let value: Value = serde_json::from_slice(&after).unwrap();
    assert_eq!(value["layout"]["groups"], before["layout"]["groups"]);
    assert_eq!(value["collections"], before["collections"]);
    assert_eq!(value["future_field"], before["future_field"]);
    assert_eq!(value["tabs"][0]["pinned_name"], "Keep");
    assert_eq!(value["tabs"][1]["session_id"], "running");
    let mut partial = App::default();
    partial.groups[0].tabs.push("running".into());
    assert!(!full.save_if_complete(&partial, &Mutex::new(false)).unwrap());
    assert_eq!(fs::read(full.path()).unwrap(), after);
}

#[test]
fn stale_full_window_cannot_replace_sibling_layout_or_collections() {
    let (dir, mut sibling) = seeded(json!({"tabs":[{"session_id":"one"}],"collections":[{"name":"Old","sessions":["one"]}]}));
    let mut stale = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    let mut current = App::default();
    sibling.restore(&mut current, &live(&["one"]));
    current.rail_width = 39;
    current.collections[0].name = "New name".into();
    sibling.save(&current).unwrap();
    let good = fs::read(sibling.path()).unwrap();
    let mut old = App::default();
    stale.restore(&mut old, &live(&["one"]));
    old.rail_width = 17;
    assert!(stale.save(&old).is_err());
    assert_eq!(fs::read(sibling.path()).unwrap(), good);
}

#[test]
fn full_window_adopts_only_additive_creation_and_explicit_stop_removes_its_recovery_marker() {
    let dir = tempfile::tempdir().unwrap();
    let mut window = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    let mut launch = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    launch.remember_session(&recovery_session("running")).unwrap();
    let mut app = App::default();
    app.groups[0].tabs.push("running".into());
    assert!(window.save_if_complete(&app, &Mutex::new(true)).unwrap());
    let saved: Value = serde_json::from_slice(&fs::read(window.path()).unwrap()).unwrap();
    assert!(saved["rust_ui"].get("recovery_open_tabs").is_none());
    launch.remember_session(&recovery_session("other")).unwrap();
    let mut killed = std::collections::HashSet::new(); killed.insert("other".into());
    window.forget_sessions(&killed).unwrap();
    let saved: Value = serde_json::from_slice(&fs::read(window.path()).unwrap()).unwrap();
    assert_eq!(saved["tabs"].as_array().unwrap().len(), 1);
    assert_eq!(saved["tabs"][0]["session_id"], "running");
    assert_eq!(saved["rust_ui"]["recovery_open_tabs"], json!([]));
}

#[test]
fn recovery_registration_rejects_wrong_scope_and_malformed_existing_state_without_replacement() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    let mut other = recovery_session("other"); other.scope_key = "/foreign".into();
    assert!(store.remember_session(&other).is_err());
    assert!(!store.path().exists());
    fs::create_dir_all(store.path().parent().unwrap()).unwrap();
    fs::set_permissions(store.path().parent().unwrap(), fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(store.path(), b"malformed original").unwrap();
    assert!(store.remember_session(&recovery_session("running")).is_err());
    assert_eq!(fs::read(store.path()).unwrap(), b"malformed original");
}

#[test]
fn explicit_attachment_reopens_only_its_detached_identity_and_vetoes_stale_window_save() {
    let (dir, mut attached) = seeded(json!({"tabs":[{"session_id":"open"},{"session_id":"reopened"},{"session_id":"stay-closed"}],
        "layout":{"kind":"tabs","groups":{"kind":"group","active":0,"tabs":[{"kind":"leaf","session_id":"open"}]}}}));
    let mut stale = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    let ids = live(&["open", "reopened", "stay-closed"]);
    let mut old = App::default(); stale.restore(&mut old, &ids);
    assert_eq!(old.groups[0].tabs, ["open"]);
    attached.reopen_session(&recovery_session("reopened")).unwrap();
    let good = fs::read(attached.path()).unwrap();
    assert!(stale.save_if_complete(&old, &Mutex::new(true)).is_err());
    assert_eq!(fs::read(attached.path()).unwrap(), good);
    let restored = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    let mut app = App::default(); restored.restore(&mut app, &ids);
    assert_eq!(app.groups[0].tabs, ["open", "reopened"]);
    attached.remember_session(&recovery_session("stay-closed")).unwrap();
    assert_eq!(fs::read(attached.path()).unwrap(), good);
}
