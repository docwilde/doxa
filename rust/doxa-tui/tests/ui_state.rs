use doxa_tui::ui::{App, Split};
use doxa_tui::ui_state::UiStateStore;
use doxa_state::{save_tabset, Tab, TabSet};
use serde_json::{json, Value};
use std::fs;

fn seeded(raw: Value) -> (tempfile::TempDir, UiStateStore) {
    let dir = tempfile::tempdir().unwrap();
    let probe = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    let data = raw.as_object().unwrap().clone();
    let tabs = data["tabs"].as_array().unwrap().iter().map(|r| Tab {
        session_id: r["session_id"].as_str().unwrap().into(),
        pinned_name: r["pinned_name"].as_str().map(str::to_owned),
        cwd: r["cwd"].as_str().map(str::to_owned),
    }).collect();
    let record = TabSet { scope_key: "/repo".into(), active_session_id: data.get("active_session_id").and_then(Value::as_str).map(str::to_owned), tabs, raw: data };
    save_tabset(probe.path(), &record).unwrap();
    let store = UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
    (dir, store)
}
fn live(ids: &[&str]) -> Vec<String> { ids.iter().map(|s| (*s).into()).collect() }

#[test]
fn flat_legacy_record_restores_active_tab_and_skips_stale_ids() {
    let (_dir, mut store) = seeded(json!({"tabs":[{"session_id":"a"},{"session_id":"stale"},{"session_id":"b"}],"active_session_id":"b"}));
    let mut app = App::default();
    assert!(store.restore(&mut app, &live(&["a", "b"])));
    assert_eq!(app.groups[0].tabs, ["a", "b"]);
    assert_eq!(app.groups[0].active, 1);
    assert!(app.groups[1].tabs.is_empty());
    store.save(&app).unwrap();
    let saved: Value = serde_json::from_slice(&fs::read(store.path()).unwrap()).unwrap();
    assert_eq!(saved["tabs"].as_array().unwrap().len(), 2);
    assert_eq!(saved["layout"]["kind"], "tabs");
    assert_eq!(saved["layout"]["groups"]["tabs"].as_array().unwrap().len(), 2);
    assert_eq!(saved["active_session_id"], "b");
}

#[test]
fn grouped_split_restores_geometry_and_preserves_leaf_metadata() {
    let (_dir, mut store) = seeded(json!({"tabs":[{"session_id":"a"},{"session_id":"b"},{"session_id":"c"}],"active_session_id":"c",
      "layout":{"kind":"tabs","groups":{"kind":"split","orientation":"column","weights":[0.7,0.3],"children":[
        {"kind":"group","active":1,"tabs":[{"kind":"leaf","session_id":"a","view":"diff"},{"kind":"leaf","session_id":"b","prompt_ratio":0.3}]},
        {"kind":"group","active":0,"tabs":[{"kind":"leaf","session_id":"c"}]}
      ]}},"collections":[{"name":"work","sessions":["a","stale"]}],"future_key":42}));
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
    assert_eq!(saved["layout"]["groups"]["children"][0]["tabs"][0]["view"], "diff");
    assert_eq!(saved["layout"]["groups"]["children"][0]["tabs"][1]["prompt_ratio"], 0.3);
    assert_eq!(saved["collections"][0]["sessions"], json!(["a"]));
    assert_eq!(saved["rust_ui"], json!({"rail_width":33,"rail_visible":false}));
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
    let (_dir, store) = seeded(json!({"tabs":[{"session_id":"a"},{"session_id":"b"},{"session_id":"c"}],"active_session_id":"b",
        "layout":{"kind":"tabs","trees":[{"kind":"split","orientation":"row","weights":[0.4,0.6],"children":[
        {"kind":"leaf","session_id":"a"},{"kind":"leaf","session_id":"b"}]}]}}));
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
    let (_dir, mut store) = seeded(json!({"tabs":[{"session_id":"a"},{"session_id":"b"}],"active_session_id":"a",
        "layout":{"kind":"tabs","groups":{"kind":"split","orientation":"row","weights":[0.2,0.3,0.5],"children":[
            {"kind":"group","tabs":[{"kind":"leaf","session_id":"a"}]},
            {"kind":"group","tabs":[{"kind":"leaf","session_id":"b"}]},
            {"kind":"group","tabs":[{"kind":"leaf","session_id":"z"}]}
        ]}}}));
    let original = fs::read(store.path()).unwrap();
    let mut app = App::default();
    assert!(store.restore(&mut app, &live(&["a", "b"])));
    assert_eq!(app.groups[0].tabs, ["a", "b"]);
    assert!(store.save(&app).is_err());
    assert_eq!(fs::read(store.path()).unwrap(), original);
}

#[test]
fn no_live_saved_tab_leaves_fresh_app_untouched() {
    let (_dir, store) = seeded(json!({"tabs":[{"session_id":"old"}],"active_session_id":"old"}));
    let mut app = App::default();
    assert!(!store.restore(&mut app, &live(&["current"])));
    assert!(app.groups.iter().all(|g| g.tabs.is_empty()));
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
fn stale_first_pane_collapses_to_live_second_pane() {
    let (_dir, store) = seeded(json!({"tabs":[{"session_id":"old"},{"session_id":"live"}],"active_session_id":"live",
        "layout":{"kind":"tabs","groups":{"kind":"split","orientation":"row","weights":[0.5,0.5],"children":[
            {"kind":"group","active":0,"tabs":[{"kind":"leaf","session_id":"old"}]},
            {"kind":"group","active":0,"tabs":[{"kind":"leaf","session_id":"live"}]}
        ]}}}));
    let mut app = App::default();
    assert!(store.restore(&mut app, &live(&["live"])));
    assert_eq!(app.groups[0].tabs, ["live"]);
    assert!(app.groups[1].tabs.is_empty());
    assert_eq!(app.active_group, 0);
}
