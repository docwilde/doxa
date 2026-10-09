//! Frontend behavior contracts retained across controller extraction.
use crossterm::event::{Event, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use std::sync::mpsc;
use super::*;
    use ratatui::backend::TestBackend;
    use serde_json::json;

    #[test]
    fn dependency_release_modal_requires_contiguous_read_arm_and_confirm() {
        let mut app = App::default();
        app.handle(Event::Resize(84, 28));
        let host = crate::fleet_control::Review {
            request: json!({
                "run_id":"review-run", "worker_index":1,
                "charter_sha256":"c".repeat(64), "assignment_id":"assignment-1",
                "task_sha256":"t".repeat(64), "checkpoint_id":"checkpoint-1",
                "handoff_id":"handoff-1", "artifact_refs":["artifact-1"],
                "changed_paths":(0..40).map(|i| format!("src/very-long-component-{i}/implementation.rs\n")).collect::<Vec<_>>().join(""),
                "last_turn_sha256":"l".repeat(64), "dependent_workers":[2,3],
                "git_observation_available":true, "tests_verified":false
            }), token: "a".repeat(64),
        };
        let prepared = fleet_dependency_review::Prepared::from_host_review(
            PathBuf::from("/unused"), "review-run", 1, host,
        ).unwrap();
        let lines = prepared.lines.clone();
        app.fleet_dependency_review = Some(prepared);
        app.chip_info = Some(ChipInfo { kind: "fleet_dependency_review", label: String::new(), lines,
            scroll: 0, owner: None });
        let mut terminal = Terminal::new(TestBackend::new(84, 28)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert!(!app.fleet_dependency_review.as_ref().unwrap().complete.get());
        app.key(KeyEvent::new(KeyCode::Char('A'), KeyModifiers::SHIFT));
        assert!(!app.fleet_dependency_review.as_ref().unwrap().armed);
        app.key(KeyEvent::new(KeyCode::Char('Y'), KeyModifiers::SHIFT));
        assert!(app.fleet_dependency_review.is_some());
        // Jumping to the bottom cannot skip unseen rows.
        app.chip_info.as_mut().unwrap().scroll = 10_000;
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert!(!app.fleet_dependency_review.as_ref().unwrap().complete.get());
        app.chip_info.as_mut().unwrap().scroll = 0;
        terminal.draw(|frame| app.draw(frame)).unwrap();
        for _ in 0..40 {
            if app.fleet_dependency_review.as_ref().unwrap().complete.get() { break; }
            app.key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
            terminal.draw(|frame| app.draw(frame)).unwrap();
        }
        assert!(app.fleet_dependency_review.as_ref().unwrap().complete.get());
        app.key(KeyEvent::new(KeyCode::Char('A'), KeyModifiers::SHIFT));
        assert!(app.fleet_dependency_review.as_ref().unwrap().armed);
        app.handle(Event::Resize(84, 27));
        let review = app.fleet_dependency_review.as_ref().unwrap();
        assert!(!review.armed && !review.complete.get());
        app.key(KeyEvent::new(KeyCode::Char('Y'), KeyModifiers::SHIFT));
        assert!(app.fleet_dependency_review.is_some());
        app.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.fleet_dependency_review.is_none() && app.chip_info.is_none());
    }

    #[test]
    fn credential_ui_transient_routes() {
        use doxa_vendors::{credentials, Vendor};
        use std::os::unix::fs::PermissionsExt;
        if std::env::var_os("DOXA_CREDENTIAL_UI_FIXTURE").is_none() {
            let dir = tempfile::tempdir().unwrap();
            std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "ui::tests::credential_ui_transient_routes", "--test-threads=1"])
                .env("DOXA_CREDENTIAL_UI_FIXTURE", "1").env("DOXA_HOME", dir.path())
                .env_remove("DEEPSEEK_API_KEY").env_remove("ZAI_API_KEY").output().unwrap();
            assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stdout));
            assert!(output.stdout.windows(b"1 passed".len()).any(|s| s == b"1 passed"));
            return;
        }
        let mut app = App { size: Rect::new(0,0,140,36), ..Default::default() };
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"ds","engine":"deepseek","model":"deepseek-flash","cwd":"/repo"}));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"za","engine":"glm","model":"glm-5.3-flash","cwd":"/repo"}));
        app.pending_model_queries.clear();
        app.open_operations(operations_menu::Menu::new("setup"));
        let key = |code| Event::Key(KeyEvent::new(code,KeyModifiers::NONE));
        let open = |app: &mut App, label: &str| {
            let lines = app.operations_menu.as_ref().unwrap().lines(140);
            let row = lines.iter().position(|line| line.contains(label) && line.contains("· edit")).unwrap();
            let area = app.active_chooser_rect().unwrap();
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left), column:area.x+2, row:area.y+1+row as u16, modifiers:KeyModifiers::NONE }));
            assert!(app.operations_menu.as_ref().unwrap().editing_credential());
        };
        let secret = "fixture-deepseek-api-key";
        open(&mut app, "DeepSeek API key");
        app.input = "ordinary draft".into(); app.input_cursor = app.input.len();
        app.handle(key(KeyCode::Char('X')));
        app.handle(key(KeyCode::Backspace));
        app.handle(Event::Paste(format!("{secret}X")));
        app.handle(key(KeyCode::Backspace));
        assert_eq!(app.input, "ordinary draft");
        assert!(app.pending_prompts.is_empty());
        assert!(!format!("{app:?}").contains(secret));
        let mut terminal = Terminal::new(TestBackend::new(140,36)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let rendered = terminal.backend().buffer().content.iter().map(|cell| cell.symbol()).collect::<String>();
        assert!(!rendered.contains(secret)); assert!(rendered.contains('•'));
        assert!(!app.operations_menu.as_ref().unwrap().lines(140).join("\n").contains(secret));
        app.handle(key(KeyCode::Enter));
        assert!(!app.operations_menu.as_ref().unwrap().editing_credential());
        assert_eq!(credentials::resolve(Vendor::DeepSeek).unwrap().as_deref(),Some(secret));
        assert_eq!(credentials::status(Vendor::Glm).unwrap(),credentials::CredentialStatus::Missing);
        assert_eq!(app.pending_model_queries, ["ds"]);
        assert!(app.pending_prompts.is_empty());
        assert!(app.input_drafts.values().all(|(s,_)| !s.contains(secret)));
        assert!(!app.notice.contains(secret));

        open(&mut app, "z.ai API key");
        app.clipboard_secret_owner = app.operations_menu.as_ref().unwrap().credential_token();
        app.clipboard_job = Some(crate::clipboard::Job::fixture(app.clipboard_target(),Ok("fixture-zai-api-key".into())));
        assert!(app.poll_clipboard());
        assert_eq!(app.input,"ordinary draft");
        assert!(app.pending_prompts.is_empty());
        app.handle(key(KeyCode::Esc));
        assert!(!app.operations_menu.as_ref().unwrap().editing_credential());
        assert_eq!(credentials::status(Vendor::Glm).unwrap(),credentials::CredentialStatus::Missing);
        open(&mut app, "z.ai API key");
        app.handle(Event::Paste("fixture-zai-api-key".into()));
        let area = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),column:area.x+2,row:area.y+3,modifiers:KeyModifiers::NONE }));
        assert_eq!(credentials::resolve(Vendor::Glm).unwrap().as_deref(),Some("fixture-zai-api-key"));
        assert_eq!(app.pending_model_queries, ["ds","za"]);

        std::env::set_var("DEEPSEEK_API_KEY", "fixture-environment-key");
        let lines = app.operations_menu.as_ref().unwrap().lines(140);
        let row = lines.iter().position(|line| line.contains("Remove saved DeepSeek API key")).unwrap();
        let area = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind:MouseEventKind::Down(MouseButton::Left),column:area.x+2,row:area.y+1+row as u16,modifiers:KeyModifiers::NONE }));
        assert_eq!(credentials::status(Vendor::DeepSeek).unwrap(),credentials::CredentialStatus::Environment);
        assert!(app.operations_menu.as_ref().unwrap().lines(140).join("\n").contains("configured (environment)"));
        open(&mut app, "DeepSeek API key");
        app.handle(Event::Paste("fixture-unsaved-secret".into()));
        let before = app.prompt_owner();
        app.groups[0].tabs.push("other-session".into()); app.groups[0].active = app.groups[0].tabs.len()-1;
        app.finish_prompt_owner_transition(before);
        assert!(app.operations_menu.is_none());
        assert!(app.pending_prompts.is_empty());
        assert!(app.input_drafts.values().all(|(s,_)| !s.contains("fixture-unsaved-secret")));

        app.open_operations(operations_menu::Menu::new("setup"));
        open(&mut app, "DeepSeek API key");
        let old_token = app.operations_menu.as_ref().unwrap().credential_token();
        let target = app.clipboard_target();
        app.handle(key(KeyCode::Esc));
        open(&mut app, "DeepSeek API key");
        app.clipboard_secret_owner = old_token;
        app.clipboard_job = Some(crate::clipboard::Job::fixture(target,Ok("fixture-stale-secret".into())));
        assert!(app.poll_clipboard());
        assert!(!app.operations_menu.as_ref().unwrap().lines(140).join("\n").contains('•'));
        assert!(!app.input.contains("fixture-stale-secret"));
        app.handle(key(KeyCode::Esc));
        app.handle(key(KeyCode::Esc));
        assert!(app.operations_menu.is_none());

        app.open_operations(operations_menu::Menu::new("setup"));
        open(&mut app, "DeepSeek API key");
        let session = app.groups[0].active_id().unwrap().to_owned();
        app.apply_daemon_frame(&json!({"type":"event", "session_id":session,
            "event":{"type":"needs_input", "data":{
                "id":"question", "kind":"ask_user", "questions":[{"question":"Choose?", "options":[]}]
            }}}));
        assert!(app.active_request_index().is_none());
        for ch in "fixture-question-secret".chars() { app.handle(key(KeyCode::Char(ch))); }
        assert!(app.operations_menu.as_ref().unwrap().editing_credential());
        assert!(app.input_requests[0].free_text.is_empty());
        assert!(app.pending_answers.is_empty());
        assert!(!app.input.contains("fixture-question-secret"));
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let rendered = terminal.backend().buffer().content.iter().map(|cell| cell.symbol()).collect::<String>();
        assert!(rendered.contains('•')); assert!(!rendered.contains("fixture-question-secret"));
        assert!(rendered.contains("API key:")); assert!(!rendered.contains("Choose?"));
        app.handle(key(KeyCode::Esc));
        assert!(app.active_request_index().is_some());
        assert!(app.pending_answers.is_empty());
    }

    #[test]
    fn reserved_startup_slot_does_not_raise_manual_tab_capacity() {
        let mut app=App::default();app.groups[0].tabs=(0..panes::MAX_TABS).map(|index|format!("saved-{index}")).collect();
        app.open_engine_picker();assert!(!app.engine_picker);
        app.attach_selected("new");assert!(app.pending_attaches.is_empty());
        app.engine_selected=1;app.select_new_engine();assert!(app.new_session.is_none());
        assert!(app.notice.contains("256"));assert!(app.pending_launches.is_empty());
    }

    #[test]
    fn empty_startup_window_keeps_setup_and_engine_controls_available() {
        let mut app=App {size:Rect::new(0,0,80,24),..Default::default()};
        assert!(app.sessions.is_empty());
        app.input="/engine".into();app.input_cursor=app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE)));assert!(app.engine_picker);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc,KeyModifiers::NONE)));
        app.input="/setup".into();app.input_cursor=app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE)));
        assert!(app.operations_menu.is_some());assert!(app.pending_prompts.is_empty());
        assert!(app.pending_launches.is_empty());assert!(!app.launching);
    }

    #[test]
    fn settings_category_switch_keeps_unsaved_edits_and_escape_discards_them() {
        let mut app=App::default();app.input="keep prompt".into();
        let rows=crate::settings::SETTINGS.iter().map(|setting|crate::settings::Row {setting,value:setting.default.into(),stored:setting.default.into(),source:"default".into(),shadowed:false}).collect::<Vec<_>>();
        let selected=rows.iter().position(|r|r.setting.key=="linger_secs").unwrap();
        app.settings_menu=Some(SettingsMenu{rows,selected,category:0,draft:None,edits:HashMap::new(),engine:"claude".into()});
        app.settings_menu_key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE));
        app.settings_menu_key(KeyEvent::new(KeyCode::Char('9'),KeyModifiers::NONE));
        app.settings_menu_key(KeyEvent::new(KeyCode::Right,KeyModifiers::SHIFT));
        let menu=app.settings_menu.as_ref().unwrap();assert_eq!(menu.category,1);assert_eq!(menu.edits["linger_secs"].as_deref(),Some("1209"));assert!(menu.draft.is_none());
        app.settings_menu_key(KeyEvent::new(KeyCode::Left,KeyModifiers::SHIFT));assert_eq!(app.settings_menu.as_ref().unwrap().edits.len(),1);
        app.settings_menu_key(KeyEvent::new(KeyCode::Esc,KeyModifiers::NONE));assert!(app.settings_menu.is_none());assert_eq!(app.input,"keep prompt");
    }
    #[test]
    fn frontend_preferences_change_actual_context_chips_and_background() {
        let mut app=App::default();app.size=Rect::new(0,0,140,30);app.rail_visible=false;
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","cwd":"/repo","engine":"claude","model":"model","ctx_percentage":25,"ctx_tokens":250,"ctx_max_tokens":1000}));
        assert_eq!(app.chips(0).iter().find(|(k,_)|*k=="context").unwrap().1,"Ctx 25%");
        app.preferences.set_test("ctx_absolute","1");assert_eq!(app.chips(0).iter().find(|(k,_)|*k=="context").unwrap().1,"Ctx 25% 250/1000");
        app.size.width=90;assert_eq!(app.chips(0).iter().find(|(k,_)|*k=="context").unwrap().1,"Ctx 25%");
        app.preferences.set_test("background","transparent");let mut terminal=Terminal::new(ratatui::backend::TestBackend::new(90,30)).unwrap();terminal.draw(|f|app.draw(f)).unwrap();assert_eq!(terminal.backend().buffer().cell((1,1)).unwrap().bg,Color::Reset);
    }
    #[test]
    fn isolation_popup_renders_verified_policy_and_idle_change_commands() {
        let mut app = App::default();
        app.handle(Event::Resize(140, 40));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"isolated", "cwd":"/fixture",
            "isolation":{"profile":"docker-open", "state":"ready", "label":"docker · open egress",
                "engine":"rootless Docker", "network":"bridge", "memory_bytes":4294967296_u64,
                "cpus":2, "pids":256, "image":"sha256:fixture", "disk_limit":"no quota"}}));
        app.open_isolation_info(0);
        assert_eq!(app.chip_info.as_ref().map(|info| info.kind), Some("isolation"));
        let rendered = painted_at(&app, 140, 40);
        for expected in ["Memory: 4096 MiB", "Mounts: independent Git checkout", "Image: sha256:fixture",
            "/isolation docker-offline --confirm", "/isolation docker-open --confirm"] {
            assert!(rendered.contains(expected), "missing {expected}: {rendered}");
        }
    }
    #[test]
    fn graph_reply_checks_selected_identity_and_ascii_expansion_keeps_prompt() {
        let mut app=App::default();app.size=Rect::new(0,0,140,32);app.show_belief_browser_fixture(0,&[(1,"user","first"),(2,"user","second")]);app.input="private draft".into();
        let(tx,rx)=mpsc::sync_channel(1);app.belief_graph_pending=Some((1,String::new(),false,rx));app.lore_picker.as_mut().unwrap().selected=1;
        tx.send(Ok(doxa_lore::BeliefGraph{id:1,lines:vec!["stale relation".into()],html:None,note:"old".into()})).unwrap();assert!(!app.poll_belief_graph());assert!(app.belief_graph_lines.is_none());assert!(app.pending_open_urls.is_empty());
        let(tx,rx)=mpsc::sync_channel(1);app.belief_graph_pending=Some((2,String::new(),false,rx));
        tx.send(Ok(doxa_lore::BeliefGraph{id:2,lines:vec!["--depends_on--> [1] first".into()],html:None,note:"scoped".into()})).unwrap();assert!(app.poll_belief_graph());assert!(painted_at(&app,140,32).contains("depends_on"));
        app.lore_picker_key(KeyEvent::new(KeyCode::Esc,KeyModifiers::NONE));assert!(app.belief_graph_lines.is_none());assert_eq!(app.input,"private draft");
    }
    #[test]
    fn auto_diff_has_no_idle_probe_and_opens_once_without_moving_focus() {
        let mut app=App::default();app.size=Rect::new(0,0,160,40);app.rail_visible=false;app.preferences.set_test("auto_diff","1");
        app.apply_update(DaemonUpdate::Upsert(Session{id:"s".into(),title:"session".into(),collection:String::new(),transcript:String::new(),status:"Ready".into()}));
        let cwd=PathBuf::from("/owned/repo");app.session_cwds.insert("s".into(),cwd.clone());assert!(!app.poll_auto_diff());assert!(app.auto_diff_pending.is_none());
        app.auto_diff_baseline.insert("s".into(),"before".into());let(tx,rx)=mpsc::sync_channel(1);app.auto_diff_pending=Some(("s".into(),cwd,rx));tx.send(("after".into(),true)).unwrap();
        let group=app.active_group;let focus=app.focus;assert!(app.poll_auto_diff());assert!(app.diff_pane);assert_eq!(app.active_group,group);assert_eq!(app.focus,focus);assert!(app.auto_diff_seen.contains("s"));
        app.diff_pane=false;app.request_auto_diff("s");assert!(app.auto_diff_requests.is_empty());
    }
    fn settings_test_rows(linger:&str,shadowed:bool)->Vec<crate::settings::Row> {
        ["linger_secs","worktree_per_session"].into_iter().map(|key|crate::settings::Row {
            setting:crate::settings::find(key).unwrap(), value:if key=="linger_secs" {linger.into()} else {"on".into()},
            stored:if key=="linger_secs" {linger.into()} else {"1".into()},source:if shadowed && key=="linger_secs" {"environment".into()} else {"default".into()},shadowed:shadowed && key=="linger_secs"
        }).collect()
    }
    #[test]
    fn settings_menu_protects_env_shadowed_rows_and_keeps_prompt() {
        let mut app = App::default();
        app.input = "draft prompt".into();
        app.settings_menu = Some(SettingsMenu {
            rows: settings_test_rows("90",true),
            selected: 0,
            draft: None, category: 0, edits: HashMap::new(), engine: "claude".into(),
        });
        app.settings_menu_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.settings_menu.as_ref().unwrap().draft.is_none());
        assert!(app.notice.contains("Environment override"));
        app.settings_menu_key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::NONE));
        assert!(app.settings_menu.is_some());
        assert_eq!(app.input, "draft prompt");
        app.settings_menu_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.settings_menu.is_none());
    }

    #[test]
    fn settings_linger_editor_cancels_without_writing() {
        let mut app = App::default();
        app.settings_menu = Some(SettingsMenu {
            rows: settings_test_rows("120",false),
            selected: 0,
            draft: None, category: 0, edits: HashMap::new(), engine: "claude".into(),
        });
        app.settings_menu_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.settings_menu.as_ref().unwrap().draft.as_ref().map(|(_,v)|v.as_str()), Some("120"));
        app.settings_menu_key(KeyEvent::new(KeyCode::Char('9'), KeyModifiers::NONE));
        app.settings_menu_key(KeyEvent::new(KeyCode::Char('p'), KeyModifiers::CONTROL));
        assert!(app.mermaid_preflight.is_none());
        assert!(app.settings_menu.as_ref().unwrap().draft.is_some());
        app.settings_menu_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.settings_menu.as_ref().unwrap().draft.is_none());
        assert_eq!(app.settings_menu.as_ref().unwrap().rows[0].value, "120");
    }

    #[test]
    fn mermaid_settings_preflight_checks_draft_without_saving_or_exposing_source() {
        let mut app = App::default();
        let rows = ["mermaid_renderer", "mermaid_renderer_root"].into_iter().map(|key| {
            let setting = crate::settings::find(key).unwrap();
            crate::settings::Row { setting, value: String::new(), stored: String::new(),
                source: "default".into(), shadowed: false }
        }).collect();
        app.settings_menu = Some(SettingsMenu { rows, selected: 0, category: 3,
            draft: Some(("mermaid_renderer".into(), "/missing/SECRET_RENDERER_PATH".into())),
            edits: HashMap::from([("mermaid_renderer_root".into(), Some("/missing/package".into()))]),
            engine: "claude".into() });
        app.input = "private transcript diagram source".into();
        assert!(app.settings_menu_key(KeyEvent::new(KeyCode::Char('p'), KeyModifiers::CONTROL)));
        assert!(app.settings_menu.as_ref().unwrap().draft.is_none());
        assert!(app.settings_menu.as_ref().unwrap().edits.contains_key("mermaid_renderer"));
        assert!(app.notice.contains("fixed sample"));
        assert!(!app.notice.contains("private transcript"));
        let deadline = Instant::now() + Duration::from_secs(2);
        while !app.poll_mermaid_preflight() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        let preflight = app.mermaid_preflight.as_ref().unwrap();
        assert!(matches!(preflight.state, MermaidPreflightState::Complete(
            transcript_mermaid::DoctorResult::Unavailable("renderer package root is unavailable"))));
        assert!(!app.notice.contains("SECRET_RENDERER_PATH"));
        assert!(!app.notice.contains("private transcript"));
        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(100, 25)).unwrap();
        terminal.draw(|frame| app.draw_settings_menu(frame, Rect::new(0, 0, 100, 25))).unwrap();
        let rendered: String = terminal.backend().buffer().content().iter()
            .map(|cell| cell.symbol()).collect();
        assert!(rendered.contains("Preflight: Unavailable"));
        assert!(rendered.contains("renderer package root is unavailable"));
        assert!(!rendered.contains("private transcript diagram source"));
        let menu = app.settings_menu.as_mut().unwrap();
        menu.edits.insert("mermaid_renderer_root".into(), Some("/another/package".into()));
        assert!(preflight.message(&menu.mermaid_paths()).contains("Configuration changed"));
    }

    #[cfg(unix)]
    fn belief_review_fixture() -> doxa_lore::BeliefReview {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let sidecar = dir.path().join("sidecar");
        std::fs::write(&sidecar, r##"#!/usr/bin/env python3
import hashlib, json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','belief_review_v1','belief_action_v1']}), flush=True)
for line in sys.stdin:
    req=json.loads(line)
    assert req['op']=='belief_review_v1' and req['belief_id']==7
    claim='A complete claim ' + 'x'*1200
    value={'id':7,'uid':'fixture-7','subject':'Fixture subject','claim':claim,'claim_sha256':hashlib.sha256(claim.encode()).hexdigest()}
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"##).unwrap();
        let mut permissions = std::fs::metadata(&sidecar).unwrap().permissions();
        permissions.set_mode(0o700);
        std::fs::set_permissions(&sidecar, permissions).unwrap();
        let lore_picker::ResultPage::BeliefReview(review, true) = lore_picker::fetch_fixture(&sidecar,
            lore_picker::Query::BeliefReview("/repo".into(), 7)).unwrap() else { panic!("review") };
        review
    }

    #[cfg(unix)]
    fn app_with_review() -> App {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 40);
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","cwd":"/repo"}));
        app.open_lore_picker();
        let picker = app.lore_picker.as_mut().unwrap();
        picker.pending = None;
        picker.rows = vec![lore_picker::Belief { id: 7, subject: "Fixture subject".into(),
            claim: "clipped list text".into(), truncated: true, confidence: 0.8, evidence_count: Some(1), recency:None }];
        let (tx, rx) = mpsc::sync_channel(1);
        picker.pending = Some(rx);
        tx.send(Ok(lore_picker::ResultPage::BeliefReview(belief_review_fixture(), true))).unwrap();
        assert!(app.poll_lore());
        app
    }

    #[test]
    fn ctrl_c_does_not_detach_the_terminal() {
        let mut app = App::default();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)));
        assert!(!app.should_quit);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::CONTROL)));
        assert!(app.should_quit);
    }

    #[test]
    fn peer_commands_use_active_session_without_becoming_model_prompts() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"session-1"}));
        app.input = "/msg peer-12 hello from this pane".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_peer_messages, vec![("session-1".into(), "peer-12".into(), "hello from this pane".into())]);
        assert!(app.pending_prompts.is_empty());
        assert!(app.input.is_empty());

        app.input = "/peers".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.map_modal);
        assert_eq!(app.pending_peer_refresh.as_deref(), Some("session-1"));
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn peer_message_usage_and_uncertain_delivery_are_explicit() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"session-1"}));
        app.input = "/msg peer-12".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.notice.starts_with("Usage:"));
        assert_eq!(app.input, "/msg peer-12");
        assert!(app.pending_peer_messages.is_empty());
        assert!(app.apply_daemon_frame(&json!({"type":"peer_message_reply", "session_id":"session-1",
            "ok":false, "uncertain":true, "draft":"/msg peer-12 important text"})));
        assert!(app.notice.contains("unconfirmed"));
        assert!(app.notice.contains("before Alt+Up retry"));
        assert_eq!(app.rejected_drafts["session-1"], ["/msg peer-12 important text"]);
        assert!(app.apply_daemon_frame(&json!({"type":"peer_message_reply", "session_id":"session-1",
            "ok":true, "peer":{"title":"Builder"}, "delivered_to":["peer-12"],
            "ledger_error":"disk full"})));
        assert!(app.notice.contains("ledger write failed"));
        assert!(app.apply_daemon_frame(&json!({"type":"peer_message_reply", "session_id":"session-1",
            "ok":true, "peer":{"title":"Python peer"}, "delivered_to":null})));
        assert!(app.notice.contains("sent to Python peer"));
    }

    #[test]
    fn background_peer_failure_keeps_recoverable_draft_in_its_session() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"first"}));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"second"}));
        app.groups[0].tabs = vec!["first".into(), "second".into()];
        app.groups[0].active = 0;
        assert!(app.apply_daemon_frame(&json!({"type":"peer_message_reply", "session_id":"second",
            "ok":false, "error":"peer left", "draft":"/msg peer-2 hello"})));
        assert_eq!(app.rejected_drafts["second"], ["/msg peer-2 hello"]);
        assert!(app.notice.contains("second"));
        assert!(app.notice.contains("peer left"));
    }

    #[test]
    fn stop_requires_confirmation_and_preserves_the_target_and_draft() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 28);
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"first", "model":"one"}));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"second", "model":"two"}));
        app.groups[0].tabs = vec!["first".into(), "second".into()];
        app.groups[0].active = 0;
        app.input = "unsent draft".into();
        app.open_stop_confirmation();
        assert_eq!(app.stop_confirmation.as_deref(), Some("first"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(app.pending_stops.is_empty());
        app.open_stop_confirmation();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE)));
        assert_eq!(app.pending_stops, vec!["first"]);
        assert_eq!(app.input, "unsent draft");
        app.apply_daemon_frame(&json!({"type":"stop_reply", "session_id":"first", "ok":true}));
        assert!(app.offline_ids.contains("first"));
        assert_eq!(app.groups[0].active_id(), Some("first"));
        assert_eq!(app.input, "unsent draft");
        app.open_stop_confirmation();
        assert!(app.stop_confirmation.is_none());
    }

    #[test]
    fn refused_stop_keeps_session_live_and_draft_intact() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"first"}));
        app.input = "keep this".into();
        app.apply_daemon_frame(&json!({"type":"stop_reply", "session_id":"first", "ok":false,
            "error":"busy"}));
        assert!(!app.offline_ids.contains("first"));
        assert_eq!(app.input, "keep this");
        assert!(app.notice.contains("busy"));
    }

    #[test]
    fn shrinking_terminal_cancels_hidden_stop_confirmation() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"first"}));
        app.open_stop_confirmation();
        assert_eq!(app.stop_confirmation.as_deref(), Some("first"));
        app.handle(Event::Resize(39, 11));
        assert!(app.stop_confirmation.is_none());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::NONE)));
        assert!(app.pending_stops.is_empty());
    }

    #[test]
    fn delete_edits_prompt_but_closes_focused_tab_and_ctrl_delete_requires_confirmation() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 28);
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"saved", "cwd":"/repo"}));
        app.session_cwds.insert("saved".into(), PathBuf::from("/repo"));
        app.input = "abc".into(); app.input_cursor = 1;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Delete, KeyModifiers::NONE)));
        assert_eq!(app.input, "ac");
        assert_eq!(app.groups[0].active_id(), Some("saved"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Delete, KeyModifiers::CONTROL)));
        assert!(app.delete_confirmation.is_some());
        assert_eq!(app.delete_confirmation.as_ref().unwrap().2, 1);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.delete_confirmation.is_none());
        assert!(app.session_delete_pending.is_none());
        assert_eq!(app.groups[0].active_id(), Some("saved"));
        app.focus = Focus::Tabs;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Delete, KeyModifiers::NONE)));
        assert!(app.groups[0].tabs.is_empty());
        assert!(app.session_stop_pending.is_none());
    }

    #[test]
    fn unconfirmed_daemon_retirement_never_starts_transcript_deletion() {
        let mut app = App::default();
        let (sender, receiver) = mpsc::sync_channel(1);
        app.session_stop_pending = Some(receiver);
        app.delete_after_stop = Some(("saved".into(), PathBuf::from("/missing")));
        sender.send(crate::sessions::Report {
            stopped: Vec::new(), requested: vec!["saved".into()],
            failed: Vec::new(), error: None,
        }).unwrap();
        assert!(app.poll_sessions_stop());
        assert!(app.delete_after_stop.is_none());
        assert!(app.session_delete_pending.is_none());
        assert!(app.notice.contains("Transcript preserved"));
    }

    #[test]
    fn model_picker_is_capability_gated_and_uses_only_daemon_catalog() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"codex-1",
            "engine":"codex", "model":"current", "cwd":"/tmp", "can_set_model":false}));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('m'), KeyModifiers::ALT)));
        assert!(app.model_picker.is_none());
        assert!(app.pending_model_queries.is_empty());

        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"claude-1",
            "engine":"claude", "model":"old", "cwd":"/tmp", "can_set_model":true}));
        app.groups[0].tabs = vec!["claude-1".into()];
        app.groups[0].active = 0;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('m'), KeyModifiers::ALT)));
        assert_eq!(app.pending_model_queries, vec!["claude-1"]);
        assert!(app.model_picker.as_ref().unwrap().loading);
        app.apply_daemon_frame(&json!({"type":"models_reply", "session_id":"codex-1",
            "ok":true, "models":["wrong-engine"]}));
        assert!(app.model_picker.as_ref().unwrap().models.is_empty());
        app.apply_daemon_frame(&json!({"type":"models_reply", "session_id":"claude-1",
            "ok":true, "models":["sonnet", "opus", "bad\nmodel", ""]}));
        assert_eq!(app.model_picker.as_ref().unwrap().models, vec!["sonnet", "opus"]);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_model_changes, vec![("claude-1".into(), "opus".into())]);
    }

    #[test]
    fn model_and_permission_replies_do_not_replace_other_pane_notice_or_draft() {
        let mut app = App::default();
        for id in ["a", "b"] {
            app.apply_daemon_frame(&json!({"type":"hello","session_id":id,
                "engine":"claude","model":"sonnet","permission_mode":"plan"}));
        }
        app.groups[0].tabs = vec!["a".into()];
        app.groups[1] = PaneGroup { tabs: vec!["b".into()], active: 0, scroll: 0 };
        app.active_group = 1;
        app.input = "private B draft".into(); app.input_cursor = app.input.len();
        app.notice = "B notice".into();
        for kind in ["set_model_reply", "set_permission_mode_reply"] {
            for ok in [true, false] {
                assert!(!app.apply_daemon_frame(&json!({"type":kind,"session_id":"a",
                    "ok":ok,"model":"opus","mode":"acceptEdits","error":"A failure"})));
                assert_eq!(app.notice, "B notice");
                assert_eq!(app.input, "private B draft");
            }
            for owner in [json!(null), json!("unknown")] {
                assert!(!app.apply_daemon_frame(&json!({"type":kind,"session_id":owner,"ok":true})));
                assert_eq!(app.notice, "B notice");
            }
        }
        assert_eq!(app.session_identity["a"].1.as_deref(), Some("sonnet"));
        assert_eq!(app.permission_modes["a"], "plan");
        assert!(app.apply_daemon_frame(&json!({"type":"set_model_reply","session_id":"b","ok":true,"model":"opus"})));
        assert!(app.notice.contains("Model selected"));
        // Replies report acceptance; provider events remain authoritative state.
        assert_eq!(app.session_identity["b"].1.as_deref(), Some("sonnet"));
        assert!(app.apply_daemon_frame(&json!({"type":"set_permission_mode_reply","session_id":"b","ok":false,"error":"denied"})));
        assert!(app.notice.contains("Permission change failed"));
        assert_eq!(app.permission_modes["b"], "plan");
    }

    #[test]
    fn permission_picker_requires_capability_and_tracks_daemon_state() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 80, 24);
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"codex-1",
            "engine":"codex", "permission_mode":"on-request", "can_set_permission_mode":false}));
        app.groups[0].tabs = vec!["codex-1".into()];
        app.open_permission_picker();
        assert!(app.permission_picker.is_none());
        assert_eq!(app.chips(0).iter().find(|(kind, _)| *kind == "permission").unwrap().1, "on-request");
        assert!(app.notice.contains("cannot change permission modes"));

        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"codex-2",
            "engine":"codex", "permission_mode":"on-request", "can_set_permission_mode":true}));
        app.groups[0].tabs = vec!["codex-2".into()];
        app.open_permission_picker();
        assert_eq!(app.permission_picker.as_ref().unwrap().1, 0);
        let picker = painted_at(&app, 80, 24);
        assert!(picker.contains("full-access"));
        assert!(!picker.contains("dontAsk"));
        app.permission_picker_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        app.select_permission_mode();
        assert_eq!(app.pending_permission_changes.pop(), Some(("codex-2".into(), "auto".into())));
        assert!(app.apply_daemon_frame(&json!({"type":"event", "session_id":"codex-2",
            "event":{"type":"permission_mode_changed","data":{"mode":"full-access"}}})));
        assert_eq!(app.permission_modes["codex-2"], "full-access");
        assert!(app.chip_hint_for("permission", 0).contains("no provider approval prompts or sandbox"));

        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"deepseek-1",
            "engine":"deepseek", "permission_mode":"default", "can_set_permission_mode":false}));
        app.groups[0].tabs = vec!["deepseek-1".into()];
        assert!(!app.chips(0).iter().any(|(kind, _)| *kind == "permission"));
        app.open_permission_picker();
        assert!(app.permission_picker.is_none());
        assert!(app.notice.contains("no provider permission mode"));

        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"claude-1",
            "engine":"claude", "permission_mode":"plan", "running":false, "queued":0,
            "can_set_permission_mode":true}));
        app.groups[0].tabs = vec!["claude-1".into()];
        app.open_permission_picker();
        assert_eq!(app.permission_picker.as_ref().unwrap().1, 2);
        app.permission_picker.as_mut().unwrap().1 = 1;
        app.permission_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.pending_permission_changes, vec![("claude-1".into(), "acceptEdits".into())]);
        assert_eq!(app.permission_modes["claude-1"], "plan");
        app.apply_daemon_frame(&json!({"type":"set_permission_mode_reply", "session_id":"claude-1",
            "ok":false, "error":"sidecar unavailable"}));
        assert!(app.notice.contains("failed"));
        assert_eq!(app.permission_modes["claude-1"], "plan");
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"claude-1",
            "event":{"type":"permission_mode_changed", "data":{"mode":"acceptEdits"}}}));
        assert_eq!(app.permission_modes["claude-1"], "acceptEdits");
    }

    #[test]
    fn chip_strip_keeps_permission_classifier_vendor_model_and_context_distinct() {
        let mut app = App::default();
        app.handle(Event::Resize(220, 30));
        app.rail_visible = false;
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"claude-1",
            "engine":"claude", "model":"sonnet", "permission_mode":"auto",
            "can_set_permission_mode":true, "can_set_model":true}));
        app.groups[0].tabs = vec!["claude-1".into()];

        let chips = app.chips(0);
        assert_eq!(chips[0], ("permission", "auto".into()));
        assert_eq!(chips[1], ("engine", "claude".into()));
        assert_eq!(chips[2], ("model", "sonnet".into()));
        assert_eq!(chips[3], ("effort", "?".into()));
        assert_eq!(chips[4].0, "context");
        assert!(chips[4].1.starts_with("Ctx "));

        let pane = app.layout(app.size).body;
        let chip_y = pane.bottom().saturating_sub(prompt_height("", pane.height) + 2);
        let click = |app: &mut App, x: u16| {
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
                column: x, row: chip_y, modifiers: KeyModifiers::NONE }));
        };
        click(&mut app, pane.x + 2);
        assert_eq!(app.permission_picker.as_ref().unwrap().1, permission_index("auto", Some("claude")).unwrap());
        app.permission_picker = None;

        let vendor_x = pane.x + chip_text(chips[0].0, &chips[0].1).width() as u16 + 3;
        click(&mut app, vendor_x);
        assert!(app.engine_picker);
        assert_eq!(app.engine_selected, 1);
        assert!(app.new_session.is_none());
        assert_eq!(app.permission_modes["claude-1"], "auto");
        app.engine_picker = false;

        let model_x = vendor_x + chip_text(chips[1].0, &chips[1].1).width() as u16 + 1;
        click(&mut app, model_x);
        assert_eq!(app.model_picker.as_ref().unwrap().session_id, "claude-1");
        assert_eq!(app.pending_model_queries, vec!["claude-1"]);
    }

    #[test]
    fn memory_chip_uses_lore_counts_and_ignores_old_project_reply() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "cwd":"/repo/old"}));
        app.set_lore_memory_usage("s", 401, 1000, 80, 200);
        assert_eq!(app.chips(0).iter().find(|(kind, _)| *kind == "memory").unwrap().1,
            "p 40%/u 40%");
        assert_eq!(memory_fill_percent(0, 1000), 0);
        assert_eq!(memory_fill_percent(4, 1000), 0);
        assert_eq!(memory_fill_percent(5, 1000), 1);
        let (tx, rx) = mpsc::sync_channel(1);
        app.memory_pending = Some(("s".into(), "/repo/old".into(), rx));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "cwd":"/repo/new"}));
        app.offline_ids.insert("s".into());
        tx.send(Some((doxa_lore::MemoryUsage {
            project_chars: 400, project_cap_chars: 1000,
            user_chars: 200, user_cap_chars: 500,
        }, true))).unwrap();
        app.poll_memory();
        assert!(!app.memory_cache.contains_key("s"));
        assert_eq!(app.chips(0).iter().find(|(kind, _)| *kind == "memory").unwrap().1,
            "u ? · scope ?");
    }

    #[test]
    fn zero_capacity_memory_chip_shows_explicit_usage_and_never_divides_by_zero() {
        assert_eq!(memory_fill_percent(0,0),0);
        assert_eq!(memory_fill_percent(16,0),0);
        assert_eq!(memory_fill_percent(u64::MAX,0),0);
        let mut app=App::default();
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","cwd":"/repo"}));
        for (project_chars,project_cap,user_chars,user_cap,label) in [
            (16,0,12,0,"p 16/0/u 12/0"),
            (0,0,0,0,"p 0/0/u 0/0"),
            (0,0,80,200,"p 0/0/u 40%"),
            (401,1000,12,0,"p 40%/u 12/0"),
            (401,1000,80,200,"p 40%/u 40%"),
        ] {
            app.set_lore_memory_usage("s",project_chars,project_cap,user_chars,user_cap);
            assert_eq!(app.chips(0).iter().find(|(kind,_)|*kind=="memory").unwrap().1,label);
        }
    }

    #[test]
    fn memory_chip_opens_inline_entries_for_active_pane_and_rejects_stale_scope() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(160, 32));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"left","cwd":"/tmp/left"}));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"right","cwd":"/tmp/right"}));
        app.groups[1].tabs.push("right".into());
        app.active_group = 1;
        app.open_memory_menu(1);
        assert_eq!(app.chip_info.as_ref().unwrap().kind, "memory");
        let menu = app.active_chooser_rect().unwrap();
        let pane = app.layout(app.size).panes.unwrap()[1];
        assert_eq!(menu.x, pane.x);
        assert!(menu.bottom() < pane.bottom());
        let (tx, rx) = mpsc::sync_channel(1);
        app.memory_menu_pending = Some(("right".into(), "/tmp/right".into(), rx));
        tx.send(Ok(vec![crate::memory_menu::Fact {scope:"user".into(),text:"verified user fact".into(),source:None,redacted:false},
            crate::memory_menu::Fact {scope:"folder".into(),text:"verified folder fact".into(),source:None,redacted:false}])).unwrap();
        assert!(app.poll_memory_menu());
        let rendered = painted_at(&app, 160, 32);
        assert!(rendered.contains("verified user fact"), "{rendered}");
        assert!(rendered.contains("verified folder fact"), "{rendered}");
        let (tx, rx) = mpsc::sync_channel(1);
        app.memory_menu_pending = Some(("right".into(), "/tmp/right".into(), rx));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"right","cwd":"/tmp/moved"}));
        tx.send(Ok(vec![crate::memory_menu::Fact {scope:"user".into(),text:"stale secret".into(),source:None,redacted:false}])).unwrap();
        assert!(!app.poll_memory_menu());
        assert!(!painted_at(&app, 160, 32).contains("stale secret"));
        let rendered = painted_at(&app, 160, 32);
        assert!(rendered.contains("Session changed; reopen memory"));
        assert!(!rendered.contains("verified folder fact"));
    }

    #[test]
    fn memory_gallery_fixture_renders_only_singular_curated_facts_without_lore_worker() {
        let mut app = App::default();
        app.handle(Event::Resize(120, 32));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"gallery","cwd":"/demo/project"}));
        app.show_memory_menu_fixture(0, &["- User entry"], &["- Project entry"], &["- Global belief"]);
        assert!(app.memory_menu_pending.is_none());
        let rendered = painted_at(&app, 120, 32);
        assert!(rendered.contains("User entry"), "{rendered}");
        assert!(rendered.contains("Project entry"), "{rendered}");
        assert!(rendered.contains("Scope") && rendered.contains("Fact") && rendered.contains("Source"), "{rendered}");
        assert!(!rendered.contains("Global belief") && !rendered.contains("## User memory"));
    }

    #[test]
    fn curated_list_selects_visible_rows_and_shows_delayed_full_fact() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(120, 28));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"memory-session","cwd":"/fixture"}));
        app.show_memory_menu_fixture(0, &["first"], &[], &[]);
        let long = "Complete singular memory fact with a visible tail".repeat(3);
        app.memory_list.as_mut().unwrap().facts = (0..30).map(|index| crate::memory_menu::Fact {
            scope: "user".into(), text: if index == 1 { long.clone() } else { format!("Fact {index}") },
            source: Some("codex".into()), redacted: false,
        }).collect();
        let menu = app.active_chooser_rect().unwrap();
        painted_at(&app, 120, 28);
        let mut terminal = Terminal::new(TestBackend::new(120, 28)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert_eq!(terminal.backend().buffer()[(menu.right() - 2, menu.y + 1)].fg, theme::ACCENT);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
        assert_eq!(app.memory_list.as_ref().unwrap().selected, 1);
        let row = app.rendered_belief_rows.borrow()[1].clone();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: row.rect.x + 4, row: row.rect.y, modifiers: KeyModifiers::NONE }));
        assert!(!painted_at(&app, 120, 28).contains("Full memory"));
        assert!(app.tick_belief_preview(Instant::now() + Duration::from_millis(600)));
        assert!(painted_at(&app, 120, 28).contains("Full memory"));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: row.rect.x + 4, row: row.rect.y + 1, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.memory_list.as_ref().unwrap().selected, 2);
        assert!(!painted_at(&app, 120, 28).contains("Full memory"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)));
        assert!(app.chip_info.as_ref().unwrap().scroll > 0);
    }

    #[test]
    fn curated_fact_tooltip_stays_open_while_wheeling_to_its_last_line() {
        let mut app=App::default();app.rail_visible=false;app.handle(Event::Resize(120,40));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"memory-session","cwd":"/fixture"}));
        app.show_memory_menu_fixture(0,&["first"],&[],&[]);
        app.memory_list.as_mut().unwrap().facts[0].text=(0..40).map(|n|format!("Complete fact line {n}\n")).collect();
        painted_at(&app,120,40);let row=app.rendered_belief_rows.borrow()[0].clone();
        app.handle(Event::Mouse(MouseEvent {kind:MouseEventKind::Moved,column:row.rect.x+2,row:row.rect.y,modifiers:KeyModifiers::NONE}));
        app.tick_belief_preview(Instant::now()+Duration::from_millis(600));
        let point=(0..40).flat_map(|y|(0..120).map(move |x|(x,y)))
            .find(|(x,y)|app.memory_preview.memory_tooltip_contains(app.size,ratatui::layout::Position::new(*x,*y))
                && !row.rect.contains(ratatui::layout::Position::new(*x,*y))).unwrap();
        app.handle(Event::Mouse(MouseEvent {kind:MouseEventKind::Moved,column:point.0,row:point.1,modifiers:KeyModifiers::NONE}));
        assert!(app.memory_preview.owner().is_some());
        for _ in 0..20 {app.handle(Event::Mouse(MouseEvent {kind:MouseEventKind::ScrollDown,column:point.0,row:point.1,modifiers:KeyModifiers::NONE}));}
        assert!(painted_at(&app,120,40).contains("Complete fact line 39"));
    }

    #[test]
    fn delegated_session_approval_uses_reviewed_task_and_body() {
        for (code, modifiers) in [
            (KeyCode::Char('a'), KeyModifiers::NONE),
            (KeyCode::Char('A'), KeyModifiers::SHIFT),
            (KeyCode::Enter, KeyModifiers::NONE),
        ] {
            let mut app = App::default();
            app.rail_visible = false;
            app.handle(Event::Resize(100, 30));
            app.groups[0].tabs.push("parent".into());
            app.input_requests.push(InputRequest::from_event("parent", &json!({
                "id":"spawn-one", "kind":"spawn", "title":"Start delegated session?",
                "task":"Review the parser", "body":"Engine: codex\nThis starts a billed session.",
                "require_full_review":true
            })).unwrap());
            app.handle(Event::Key(KeyEvent::new(code, modifiers)));
            assert!(app.pending_answers.is_empty());
            painted_at(&app, 100, 30);
            assert!(app.input_requests[0].review_complete.get());
            if code == KeyCode::Enter {
                app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
                app.handle(Event::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE)));
                assert_eq!(app.input_requests[0].selected, 1);
            }
            app.handle(Event::Key(KeyEvent::new(code, modifiers)));
            assert_eq!(app.pending_answers, vec![("parent".into(), "spawn-one".into(),
                json!({"decision":"allow"}))]);
        }
        let mut app = App::default();
        app.handle(Event::Resize(100, 30));
        app.groups[0].tabs.push("parent".into());
        app.input_requests.push(InputRequest::from_event("parent", &json!({
            "id":"incomplete", "kind":"spawn", "task":"Review", "require_full_review":true
        })).unwrap());
        painted_at(&app, 100, 30);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE)));
        assert!(app.pending_answers.is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert_eq!(app.pending_answers[0].2, json!({"decision":"deny"}));
    }

    #[test]
    fn approval_grant_is_per_tool_and_session_and_only_after_acknowledgement() {
        let mut app = App::default(); app.rail_visible = false;
        app.handle(Event::Resize(100, 30));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"first","cwd":"/fixture"}));
        let request = |session: &str, id: &str, tool: &str, input: &str| json!({"type":"event","session_id":session,
            "event":{"type":"needs_input","data":{"id":id,"kind":"permission","title":"Approve this canonical DOXA tool once?",
                "tool_name":tool,"input_summary":input,"require_full_review":false}}});
        app.apply_daemon_frame(&request("first", "one", "mcp__doxa__peer_list", "{}"));
        let menu = app.active_chooser_rect().unwrap();
        let painted = painted_at(&app, 100, 30);
        assert!(painted.lines().nth(usize::from(menu.y)).unwrap().contains("mcp__doxa__peer_list"));
        let (body, _, _) = input_request_body(&app.input_requests[0], 90);
        assert!(!body.contains("Title:") && !body.contains("Tool:") && !body.contains("Input:"));
        assert!(body.contains("Always approve this tool"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('l'), KeyModifiers::NONE)));
        assert_eq!(app.pending_answers[0].2["scope"], "session");
        assert!(app.permission_grants.is_empty());
        app.apply_daemon_frame(&json!({"type":"answer_reply","session_id":"first","request_id":"one","ok":true}));
        assert!(app.permission_grants.contains(&("first".into(), "mcp__doxa__peer_list".into())));
        app.apply_daemon_frame(&request("first", "two", "mcp__doxa__peer_list", "{\"limit\":100}"));
        assert!(app.pending_answers.iter().any(|(_, id, answer)| id == "two" && answer["decision"] == "allow"));
        app.apply_daemon_frame(&request("first", "three", "mcp__doxa__peer_send", "{}"));
        assert!(!app.pending_answers.iter().any(|(_, id, _)| id == "three"));
        app.apply_daemon_frame(&json!({"type":"event","session_id":"first","event":{"type":"session_done","data":{}}}));
        assert!(app.permission_grants.is_empty());
    }

    #[test]
    fn codex_provider_approvals_never_offer_or_reuse_a_session_grant() {
        for tool in ["command_execution", "file_change", "request_permissions"] {
            let mut app = App::default();
            app.rail_visible = false;
            app.handle(Event::Resize(100, 30));
            app.apply_daemon_frame(&json!({"type":"hello","session_id":"codex-1","engine":"codex"}));
            let request = |id: &str| json!({"type":"event","session_id":"codex-1",
                "event":{"type":"needs_input","data":{"id":id,"kind":"permission",
                    "tool_name":tool,"input_summary":"full request","require_full_review":false}}});
            app.apply_daemon_frame(&request("first"));
            assert!(!app.input_requests[0].can_grant_for_session());
            assert!(!input_request_body(&app.input_requests[0], 90).0.contains("Always approve"));
            app.handle(Event::Key(KeyEvent::new(KeyCode::Char('l'), KeyModifiers::NONE)));
            assert!(app.pending_answers.is_empty(), "{tool} must remain one-shot");
            app.permission_grants.insert(("codex-1".into(), tool.into()));
            app.apply_daemon_frame(&request("second"));
            assert!(app.pending_answers.is_empty(), "stale grant must not auto-approve {tool}");
        }
    }

    #[test]
    fn peer_tool_fold_header_keeps_literal_underscores() {
        let row=super::transcript_events::structured_event("tool_call",&json!({
            "name":"mcp__doxa__peer_list","input":{}
        })).unwrap();
        assert!(row.contains("Tool: mcp__doxa__peer_list started"));
        assert!(!row.contains("\\_"));
    }

    #[test]
    fn fleet_admission_notices_and_unreviewed_status_are_visible_in_the_transcript() {
        let mut app=App::default();
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","cwd":"/demo"}));
        app.apply_daemon_frame(&json!({"type":"event","session_id":"s","event":{"type":"fleet_guard","data":{"delivered":false,"reason":"outside approved assignment"}}}));
        app.apply_daemon_frame(&json!({"type":"event","session_id":"s","event":{"type":"peer_message","data":{"from_title":"Worker","body":"status only","fleet_admission":{"delivered":true,"unreviewed":true}}}}));
        let transcript=&app.sessions[0].transcript;
        assert!(transcript.contains("Fleet message quarantined: outside approved assignment"));
        assert!(transcript.contains("Worker [unreviewed fleet message]: status only"));
        let row=super::transcript_events::structured_event("fleet_guard",&json!({"delivered":false,"reason":"hold\nfor human\u{1b}"})).unwrap();
        assert!(!row.contains('\u{1b}'));
    }

    #[test]
    fn clicking_filter_prompt_keeps_both_lore_menus_and_private_draft() {
        for memory in [false,true] {
            let mut app=App::default();app.rail_visible=false;app.handle(Event::Resize(120,32));
            app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","cwd":"/demo"}));
            app.input="Private draft".into();app.input_cursor=app.input.len();
            if memory {app.show_memory_menu_fixture(0,&["Fact"],&[],&[]);}
            else {app.show_belief_browser_fixture(0,&[(7,"user","Fact")]);}
            app.focus=Focus::Chip("memory");
            let layout=app.layout(app.size);let pane=layout.panes.map_or(layout.body,|panes|panes[app.active_group]);
            let prompt=app.pane_regions(app.active_group,pane)[4];
            app.handle(Event::Mouse(MouseEvent {kind:MouseEventKind::Down(MouseButton::Left),column:prompt.x+2,
                row:prompt.y+1,modifiers:KeyModifiers::NONE}));
            assert_eq!(app.focus,Focus::Prompt);
            assert_eq!(app.input,"Private draft");
            if memory {assert!(app.chip_info.is_some());}else{assert!(app.lore_picker.is_some());}
            app.handle(Event::Key(KeyEvent::new(KeyCode::Char('f'),KeyModifiers::NONE)));
            assert_eq!(app.input,"Private draft");
            assert!(app.pending_prompts.is_empty());
        }
    }

    #[test]
    fn curated_memory_prompt_filters_singular_facts_without_touching_draft() {
        let mut app=App::default();app.handle(Event::Resize(120,32));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","cwd":"/demo"}));
        app.input="Private draft".into();app.input_cursor=app.input.len();
        app.show_memory_menu_fixture(0,&["Prefer concise replies"],&["Run cargo test"],&[]);
        for ch in "cargo".chars(){app.handle(Event::Key(KeyEvent::new(KeyCode::Char(ch),KeyModifiers::NONE)));}
        let filtered=painted_at(&app,120,32);
        assert!(filtered.contains("Filter memory") && filtered.contains("Run cargo test"));
        assert!(!filtered.contains("Prefer concise replies") && !filtered.contains("##"));
        assert_eq!(app.input,"Private draft");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('u'),KeyModifiers::CONTROL)));
        app.handle(Event::Paste("concise".into()));
        assert!(painted_at(&app,120,32).contains("Prefer concise replies"));
        assert_eq!(app.input,"Private draft");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc,KeyModifiers::NONE)));
        assert_eq!(app.input,"Private draft");assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn single_pane_keeps_four_primary_chips_visible_without_prefixes() {
        let mut app = App::default();
        app.handle(Event::Resize(148, 31));
        app.split = Split::Horizontal;
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"claude-1",
            "engine":"claude", "model":"claude-sonnet-4", "permission_mode":"default",
            "can_set_permission_mode":true, "can_set_model":true}));
        app.groups[0].tabs = vec!["claude-1".into()];
        let layout = app.layout(app.size);
        let pane = layout.panes.map_or(layout.body, |panes| panes[0]);
        let visible = app.chip_window(0, usize::from(pane.width));
        assert_eq!(visible.iter().take(4).map(|(kind, _)| *kind).collect::<Vec<_>>(),
            vec!["permission", "engine", "model", "effort"]);
        assert_eq!(visible[0].1, "default");
        assert_eq!(visible[1].1, "claude");
        assert_eq!(visible[2].1, "claude-sonnet-4");
        assert_eq!(visible[3].1, "?");
        let occupied = visible.iter().map(|(kind, label)| chip_text(kind, label).width()).sum::<usize>()
            + visible.len().saturating_sub(1);
        assert!(occupied <= usize::from(pane.width));
    }

    #[test]
    fn dont_ask_requires_idle_and_empty_queue() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 80, 24);
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "engine":"claude",
            "permission_mode":"default", "running":true, "queued":0,
            "can_set_permission_mode":true}));
        app.groups[0].tabs = vec!["s".into()];
        app.open_permission_picker();
        app.permission_picker.as_mut().unwrap().1 = 4;
        app.select_permission_mode();
        assert!(app.pending_permission_changes.is_empty());
        app.apply_daemon_frame(&json!({"type":"reply", "session_id":"s", "ok":true,
            "status":{"session_id":"s", "running":false, "queued":1}}));
        app.select_permission_mode();
        assert!(app.pending_permission_changes.is_empty());
        app.apply_daemon_frame(&json!({"type":"reply", "session_id":"s", "ok":true,
            "status":{"session_id":"s", "running":false, "queued":0}}));
        app.select_permission_mode();
        assert!(app.pending_permission_changes.is_empty());
        assert!(app.permission_confirm_dont_ask);
        app.select_permission_mode();
        assert_eq!(app.pending_permission_changes, vec![("s".into(), "dontAsk".into())]);
        assert!(app.permission_picker.is_none());
    }

    #[test]
    fn dont_ask_confirmation_clears_on_selection_change_and_mouse_cannot_submit() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 80, 24);
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "engine":"claude",
            "permission_mode":"default", "running":false, "queued":0,
            "can_set_permission_mode":true}));
        app.groups[0].tabs = vec!["s".into()];
        app.open_permission_picker();
        app.permission_picker.as_mut().unwrap().1 = 4;
        app.permission_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.permission_confirm_dont_ask);
        app.permission_picker_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
        assert!(!app.permission_confirm_dont_ask);
        app.permission_picker_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: 8, row: 10, modifiers: KeyModifiers::NONE }));
        assert!(app.pending_permission_changes.is_empty());
        assert!(!app.permission_confirm_dont_ask);
    }

    #[test]
    fn permission_picker_mouse_selects_mode_and_outside_click_closes() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 80, 24);
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "engine":"claude",
            "permission_mode":"default", "running":false, "queued":0,
            "can_set_permission_mode":true}));
        app.groups[0].tabs = vec!["s".into()];
        app.open_permission_picker();
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 6, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.pending_permission_changes, vec![("s".into(), "plan".into())]);
        app.open_permission_picker();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: 0, row: 0, modifiers: KeyModifiers::NONE }));
        assert!(app.permission_picker.is_none());
    }

    #[test]
    fn model_catalog_failure_never_offers_a_guessed_model() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s",
            "engine":"claude", "model":"existing", "cwd":"/tmp", "can_set_model":true}));
        app.open_model_picker();
        app.apply_daemon_frame(&json!({"type":"models_reply", "session_id":"s",
            "ok":false, "error":"offline"}));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.pending_model_changes.is_empty());
        assert!(app.model_picker.is_some());
    }

    #[test]
    fn model_picker_retries_in_progress_catalog_with_r() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s",
            "engine":"claude", "model":"existing", "cwd":"/tmp", "can_set_model":true}));
        app.open_model_picker();
        app.pending_model_queries.clear();
        app.apply_daemon_frame(&json!({"type":"models_reply", "session_id":"s",
            "ok":true, "loading":true, "models":[],
            "note":"Claude model catalog is loading; press R to retry"}));
        assert!(app.model_picker.as_ref().unwrap().catalog_pending);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE)));
        assert_eq!(app.pending_model_queries, vec!["s"]);
        app.apply_daemon_frame(&json!({"type":"models_reply", "session_id":"s",
            "ok":true, "models":["verified"], "note":"Claude CLI cache"}));
        assert!(!app.model_picker.as_ref().unwrap().catalog_pending);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_model_changes, vec![("s".into(), "verified".into())]);
    }

    #[test]
    fn live_claude_billing_event_updates_only_its_chip_and_keeps_context() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"claude",
            "ctx_percentage":42,"billing":{"mode":"subscription","type":"Max"}}));
        assert!(app.apply_daemon_frame(&json!({"type":"event","session_id":"s",
            "event":{"type":"billing","data":{"mode":"subscription","type":"Max",
                "quota":"5h 35% · week 21%"}}})));
        assert!(app.chips(0).iter().any(|(kind,label)|*kind=="cost" && label=="Max · 5h 35% · week 21%"));
        assert_eq!(app.session_telemetry["s"].context.as_deref(),Some("42%"));
        assert!(!app.apply_daemon_frame(&json!({"type":"event","session_id":"unknown",
            "event":{"type":"billing","data":{"mode":"subscription","quota":"5h 99%"}}})));
        assert!(!app.session_telemetry.contains_key("unknown"));
    }

    #[test]
    fn subscription_chip_only_shows_reported_plan_and_quota() {
        let mut telemetry = SessionTelemetry::default();
        telemetry.update_status(&json!({"billing":{"mode":"subscription","type":"Max"}}));
        assert_eq!(telemetry.billing_label(Some("claude")).as_deref(), Some("Max"));
        telemetry.update_status(&json!({"billing":{"mode":"subscription","quota":"5h 42% · week 25%"}}));
        assert_eq!(telemetry.billing_label(Some("claude")).as_deref(), Some("5h 42% · week 25%"));
        telemetry.update_status(&json!({"billing":{"mode":"subscription"}}));
        assert!(telemetry.billing_label(Some("claude")).is_none());
    }

    #[test]
    fn model_picker_refreshes_pending_catalog_and_preserves_selection() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s",
            "engine":"claude", "model":"second", "can_set_model":true}));
        app.open_model_picker();
        app.pending_model_queries.clear();
        app.apply_daemon_frame(&json!({"type":"models_reply", "session_id":"s",
            "ok":true, "loading":true, "models":[]}));
        let due = app.model_refresh_after.unwrap();
        assert!(!app.poll_model_catalog(due - Duration::from_millis(1)));
        assert!(app.poll_model_catalog(due));
        assert!(!app.poll_model_catalog(due + Duration::from_secs(1)));
        assert_eq!(app.pending_model_queries, vec!["s"]);
        app.pending_model_queries.clear();
        app.apply_daemon_frame(&json!({"type":"models_reply", "session_id":"s",
            "ok":true, "models":["first", "second"]}));
        assert_eq!(app.model_picker.as_ref().unwrap().selected, 1);
        let due = app.model_refresh_after.unwrap();
        assert!(app.poll_model_catalog(due));
        app.apply_daemon_frame(&json!({"type":"models_reply", "session_id":"s",
            "ok":true, "models":["second", "first"]}));
        assert_eq!(app.model_picker.as_ref().unwrap().selected, 0);
        app.model_picker = None;
        assert!(!app.poll_model_catalog(due + Duration::from_secs(60)));
        assert!(app.model_refresh_after.is_none());
    }

    #[test]
    fn engine_picker_labels_new_session_scope() {
        assert!(!ENGINE_CHOICES.contains(&"fixture"));
        let mut app = App::default();
        app.open_engine_picker();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(!app.engine_picker);
        assert_eq!(app.new_session.as_ref().unwrap().engine, launch::Engine::Claude);
    }

    #[test]
    fn claude_engine_form_accepts_mouse_fields_and_start_without_an_initial_prompt() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.open_engine_picker();
        let engines = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: engines.x + 2, row: engines.y + 3, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.new_session.as_ref().unwrap().engine, launch::Engine::Claude);
        let form = app.active_chooser_rect().unwrap();
        let first = form.y + if form.height >= 8 { 4 } else { 2 };
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: form.x + 2, row: first + 1, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.new_session.as_ref().unwrap().field, 1);
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: form.x + 2, row: first, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.new_session.as_ref().unwrap().field, 0);
        // Replace the displayed configured default deliberately.
        while !app.new_session.as_ref().unwrap().model.is_empty() {
            app.handle(Event::Key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE)));
        }
        for c in "sonnet".chars() {
            app.handle(Event::Key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)));
        }
        let rendered = painted(&app);
        assert!(rendered.contains("Start session"));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: form.x + 2, row: first + 3, modifiers: KeyModifiers::NONE }));
        assert!(app.new_session.is_some());
        let (options, prompt, _) = app.pending_launches.pop().unwrap();
        assert_eq!(options.engine, launch::Engine::Claude);
        assert_eq!(options.model.as_deref(), Some("sonnet"));
        assert!(options.codex_bin.is_none() && options.sandbox.is_none() && options.effort.is_none());
        assert!(prompt.is_none());
    }

    #[test]
    fn new_session_form_queues_engine_model_and_first_prompt() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.open_engine_picker();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.new_session.as_ref().unwrap().model, "deepseek-flash");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.new_session.as_ref().unwrap().effort.as_deref(), Some("high"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        for c in "Explain this".chars() {
            app.handle(Event::Key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)));
        }
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        let (options, prompt, group) = app.pending_launches.pop().unwrap();
        assert_eq!(options.engine, launch::Engine::DeepSeek);
        assert_eq!(options.model.as_deref(), Some("deepseek-flash"));
        assert_eq!(options.effort.as_deref(), Some("high"));
        assert_eq!(prompt.as_deref(), Some("Explain this"));
        assert_eq!(group, 0);
        assert!(app.launching);
        assert!(app.new_session.is_some());
    }

    #[test]
    fn failed_new_session_keeps_form_and_error_after_old_session_updates() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(110, 36));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"old","engine":"codex","model":"gpt-6-sol"}));
        app.open_engine_picker();
        app.engine_selected = 1; // Claude
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.new_session.as_ref().unwrap().engine, launch::Engine::Claude);
        app.new_session.as_mut().unwrap().field = 2;
        app.new_session.as_mut().unwrap().prompt = "Private first prompt".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.launching && app.new_session.is_some());
        assert!(painted_at(&app, 110, 36).contains("waiting for launch result"));
        app.apply_daemon_frame(&json!({"type":"launch_reply","ok":false,"message":"Claude dependency unavailable"}));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"old","engine":"codex","model":"gpt-6-sol"}));
        let form = app.new_session.as_ref().unwrap();
        assert_eq!(form.prompt, "Private first prompt");
        assert!(form.launch_error.as_deref().unwrap().contains("Claude dependency unavailable"));
        assert_eq!(app.groups[0].tabs, vec!["old"]);
        assert!(painted_at(&app, 110, 36).contains("Claude dependency unavailable"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.launching && app.new_session.as_ref().unwrap().launch_error.is_none());
        app.apply_daemon_frame(&json!({"type":"launch_reply","ok":true,"session_id":"fresh","group":0}));
        assert!(app.new_session.is_none());
        assert_eq!(app.groups[0].active_id(), Some("fresh"));
    }

    #[test]
    fn cd_opens_new_tab_at_verified_directory_without_moving_existing_session() {
        let root = tempfile::tempdir().unwrap();
        let child = root.path().join("child");
        std::fs::create_dir(&child).unwrap();
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"existing",
            "engine":"claude", "model":"sonnet", "cwd":root.path()}));
        app.input = "/cd child".into();
        assert!(app.submit_local_command());
        let (options, prompt, group) = app.pending_launches.pop().unwrap();
        assert_eq!(options.engine, launch::Engine::Claude);
        assert_eq!(options.cwd.as_deref(), Some(child.as_path()));
        assert!(prompt.is_none());
        assert_eq!(group, 0);
        assert_eq!(app.groups[0].active_id(), Some("existing"));
        assert_eq!(app.session_cwds["existing"], root.path());
        assert!(app.input.is_empty());
    }

    #[test]
    fn cd_refuses_invalid_directory_and_keeps_draft() {
        let root = tempfile::tempdir().unwrap();
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"existing",
            "engine":"codex", "cwd":root.path()}));
        app.input = "/cd missing".into();
        assert!(app.submit_local_command());
        assert!(app.pending_launches.is_empty());
        assert_eq!(app.input, "/cd missing");
        assert!(app.notice.contains("does not exist"));
    }

    #[test]
    fn effort_chip_picker_requests_current_session_change_and_waits_for_event() {
        let mut app = App::default();
        app.handle(Event::Resize(220, 32));
        app.rail_visible = false;
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"deep-1",
            "engine":"deepseek","model":"deepseek-flash","effort":"high"}));
        app.groups[0].tabs = vec!["deep-1".into()];
        let effort_index = app.chips(0).iter().position(|(kind, _)| *kind == "effort").unwrap();
        assert_eq!(app.chips(0)[effort_index], ("effort", "high".into()));
        assert_eq!(chip_text("effort", "high"), " high ▾ ");
        assert_eq!(chip_text("effort", "?"), " ? ");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('f'), KeyModifiers::ALT)));
        assert_eq!(app.effort_picker.as_ref().unwrap().levels, ["none", "low", "high", "max"]);
        assert_eq!(app.effort_picker.as_ref().unwrap().selected, 2);
        assert!(painted(&app).contains("this session"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_effort_changes, vec![("deep-1".into(), "max".into())]);
        assert_eq!(app.session_efforts["deep-1"], "high");
        app.apply_daemon_frame(&json!({"type":"set_effort_reply","session_id":"deep-1","ok":true,"effort":"max"}));
        assert_eq!(app.session_efforts["deep-1"], "high");
        app.apply_daemon_frame(&json!({"type":"event","session_id":"deep-1",
            "event":{"type":"effort_changed","data":{"effort":"max"}}}));
        assert_eq!(app.session_efforts["deep-1"], "max");

        // The closed picker moves the chip strip back up before the next
        // pointer event; use the freshly painted hit area.
        let _ = painted_at(&app, 220, 32);
        let effort_hit = app.rendered_chip_hits.borrow().as_ref().unwrap().iter()
            .find(|hit| hit.kind == "effort").unwrap().clone();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: effort_hit.rect.x + 1, row: effort_hit.rect.y, modifiers: KeyModifiers::NONE }));
        let hovered_at = Instant::now();
        app.tick_chip_hover(hovered_at);
        app.tick_chip_hover(hovered_at + Duration::from_millis(500));
        assert!(painted_at(&app, 220, 32).contains("Effort · current session"));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: effort_hit.rect.x + 1, row: effort_hit.rect.y, modifiers: KeyModifiers::NONE }));
        assert!(app.effort_picker.is_some());
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 3, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.pending_effort_changes.last(), Some(&("deep-1".into(), "none".into())));
        assert!(app.effort_picker.is_none());
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"glm-2",
            "engine":"glm","model":"glm-5.3-flash","effort":"low"}));
        app.groups[0].tabs.push("glm-2".into());
        app.groups[0].active = 1;
        assert_eq!(app.chips(0).iter().find(|(kind, _)| *kind == "effort").unwrap().1, "low");
        app.open_effort_picker();
        assert_eq!(app.effort_picker.as_ref().unwrap().levels, ["low", "high", "max"]);
        assert!(!app.effort_picker.as_ref().unwrap().levels.contains(&"none".to_owned()));
        assert_eq!(app.pending_effort_changes.last(), Some(&("deep-1".into(), "none".into())));
        app.apply_daemon_frame(&json!({"type":"event","session_id":"glm-2",
            "event":{"type":"model_changed","data":{"model":"unknown-new-model"}}}));
        assert!(app.effort_picker.is_none());
        app.open_effort_picker();
        assert!(app.effort_picker.is_none());
    }

    #[test]
    fn codex_catalog_drives_same_session_effort_and_clears_absent_effort() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"codex-1",
            "engine":"codex","model":"account-model","effort":"high","can_set_model":true}));
        app.groups[0].tabs = vec!["codex-1".into()];
        app.open_model_picker();
        app.apply_daemon_frame(&json!({"type":"models_reply","session_id":"codex-1","ok":true,
            "models":["account-model"],"capabilities":[{"model":"account-model","efforts":["minimal","high"]}]}));
        app.model_picker = None;
        app.open_effort_picker();
        assert_eq!(app.effort_picker.as_ref().unwrap().levels, ["minimal", "high"]);
        app.effort_picker.as_mut().unwrap().selected = 0;
        app.select_effort();
        assert_eq!(app.pending_effort_changes.last(), Some(&("codex-1".into(), "minimal".into())));
        app.apply_daemon_frame(&json!({"type":"event","session_id":"codex-1",
            "event":{"type":"model_changed","data":{"model":"no-reasoning","effort":null}}}));
        assert!(!app.session_efforts.contains_key("codex-1"));
    }

    #[test]
    fn effort_capability_and_vendor_model_choices_fail_closed() {
        assert_eq!(effort_choices("glm", "glm-5.3-flash"), ["low", "high", "max"]);
        assert!(effort_choices("glm", "glm-unverified").is_empty());
        assert!(effort_choices("deepseek", "glm-5.3-flash").is_empty());
        assert!(effort_choices("codex", "gpt-6").is_empty());
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"unknown",
            "engine":"codex","model":"gpt-6","effort":"high"}));
        app.groups[0].tabs = vec!["unknown".into()];
        app.open_effort_picker();
        assert!(app.effort_picker.is_none());
        assert!(app.notice.contains("Loading current model effort capabilities"));

        app.engine_selected = 2;
        app.select_new_engine();
        assert_eq!(app.new_session.as_ref().unwrap().model, "deepseek-flash");
        assert_eq!(app.new_session.as_ref().unwrap().effort.as_deref(), Some("high"));
        app.new_session.as_mut().unwrap().field = 0;
        app.new_session_key(KeyEvent::new(KeyCode::Right, KeyModifiers::NONE));
        assert_eq!(app.new_session.as_ref().unwrap().model, "deepseek-v4-pro");
        app.engine_selected = 3;
        app.select_new_engine();
        assert_eq!(app.new_session.as_ref().unwrap().model, "glm-5.3-flash");
        assert_eq!(app.new_session.as_ref().unwrap().effort.as_deref(), Some("high"));
        let form = app.new_session.as_ref().unwrap();
        assert!(!vendor_models(form.engine).contains(&"deepseek-v4-pro"));
        assert!(!effort_choices(engine_name(form.engine), &form.model).contains(&"none"));
    }

    #[test]
    fn effort_catalogs_belong_to_each_session_and_empty_refresh_revokes_choices() {
        let mut app = App::default();
        for id in ["a", "b"] {
            app.apply_daemon_frame(&json!({"type":"hello","session_id":id,"engine":"claude","model":"same"}));
        }
        app.groups[0].tabs = vec!["a".into(), "b".into()];
        app.apply_daemon_frame(&json!({"type":"models_reply","session_id":"a","ok":true,
            "capabilities":[{"model":"same","efforts":["low"]}]}));
        app.groups[0].active = 1;
        app.open_effort_picker();
        assert!(app.effort_picker.is_none());
        assert_eq!(app.pending_model_queries.last().map(String::as_str), Some("b"));
        app.apply_daemon_frame(&json!({"type":"models_reply","session_id":"b","ok":true,
            "capabilities":[{"model":"same","efforts":["high"]}]}));
        assert_eq!(app.effort_picker.as_ref().unwrap().levels, ["high"]);
        app.apply_daemon_frame(&json!({"type":"models_reply","session_id":"b","ok":true,"capabilities":[]}));
        app.select_effort();
        assert!(app.pending_effort_changes.is_empty(), "an open picker cannot retain revoked effort metadata");
        assert_eq!(app.session_effort_levels("a", "claude", "same"), ["low"]);
        app.open_effort_picker();
        assert!(app.effort_picker.is_none());
        assert!(app.notice.contains("unavailable"));
        app.apply_daemon_frame(&json!({"type":"models_reply","session_id":"a","ok":false}));
        assert!(!app.session_catalogs["a"].reported_for("claude"));
        assert!(app.session_effort_levels("a", "claude", "same").is_empty());
        app.apply_daemon_frame(&json!({"type":"models_reply","session_id":"missing","ok":true,
            "capabilities":[{"model":"same","efforts":["max"]}]}));
        assert!(!app.session_catalogs.contains_key("missing"));
    }

    #[test]
    fn dynamic_vendor_empty_metadata_denies_measured_fallback() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"deepseek","model":"deepseek-flash"}));
        assert_eq!(app.session_effort_levels("s", "deepseek", "deepseek-flash"), ["none", "low", "high", "max"]);
        app.apply_daemon_frame(&json!({"type":"models_reply","session_id":"s","ok":true,
            "capabilities":[{"model":"deepseek-flash","efforts":[]}]}));
        assert!(app.session_effort_levels("s", "deepseek", "deepseek-flash").is_empty());
        assert!(app.session_effort_levels("s", "deepseek", "unknown").is_empty());
        app.apply_daemon_frame(&json!({"type":"models_reply","session_id":"s","ok":false}));
        assert!(app.session_effort_levels("s", "deepseek", "deepseek-flash").is_empty(), "failure cannot resurrect fallback");
    }

    #[test]
    fn revoked_runtime_capabilities_block_open_picker_keyboard_and_mouse_commands() {
        let mut app = App::default(); app.size = Rect::new(0, 0, 100, 28);
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"claude","model":"old",
            "can_set_model":true,"can_set_permission_mode":true}));
        app.open_model_picker();
        app.apply_daemon_frame(&json!({"type":"models_reply","session_id":"s","ok":true,"models":["new"]}));
        app.apply_daemon_frame(&json!({"type":"reply","status":{"session_id":"s","can_set_model":false}}));
        app.model_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.pending_model_changes.is_empty());
        let menu = app.active_chooser_rect().unwrap();
        click_picker_row(&mut app, menu, 3);
        assert!(app.pending_model_changes.is_empty());
        app.model_picker = None;
        app.open_permission_picker();
        app.apply_daemon_frame(&json!({"type":"reply","status":{"session_id":"s","can_set_permission_mode":"true"}}));
        app.permission_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.pending_permission_changes.is_empty());
        let menu = app.active_chooser_rect().unwrap();
        click_picker_row(&mut app, menu, 4);
        assert!(app.pending_permission_changes.is_empty());
    }

    #[test]
    fn live_vendor_catalog_refresh_filters_models_and_switch_discards_stale_rows() {
        fn row(id: &str, efforts: &[&str], default: Option<&str>) -> doxa_vendors::ModelCapability {
            doxa_vendors::ModelCapability { id: id.into(), efforts: efforts.iter().map(|x| (*x).into()).collect(),
                default_effort: default.map(str::to_owned), effort_metadata_present: !efforts.is_empty() }
        }
        let mut app = App::default();
        app.engine_selected = 2;
        app.select_new_engine();
        let (tx, rx) = mpsc::sync_channel(1);
        app.vendor_catalog_pending = Some((launch::Engine::DeepSeek, rx));
        app.new_session.as_mut().unwrap().catalog_pending = true;
        tx.send(Some(vec![row("deepseek-v4-pro", &["low", "high", "max"], Some("high")),
            row("deepseek-next", &["low", "max"], Some("max"))])).unwrap();
        assert!(app.poll_vendor_catalog());
        let form = app.new_session.as_ref().unwrap();
        assert_eq!(form.models, ["deepseek-next", "deepseek-v4-pro"]);
        assert_eq!(form.model_efforts["deepseek-next"], ["low", "max"]);
        assert_eq!(form.model, "deepseek-next");
        assert_eq!(form.effort.as_deref(), Some("max"));
        assert!(form.catalog_note.contains("Live vendor"));

        // A present but unusable capability list is different from absent
        // metadata: never resurrect the static levels for that live model.
        let (tx, rx) = mpsc::sync_channel(1);
        app.vendor_catalog_pending = Some((launch::Engine::DeepSeek, rx));
        tx.send(Some(vec![doxa_vendors::ModelCapability { id: "deepseek-flash".into(),
            efforts: Vec::new(), default_effort: None, effort_metadata_present: true }])).unwrap();
        assert!(app.poll_vendor_catalog());
        assert!(app.new_session.as_ref().unwrap().models.is_empty());

        let (stale_tx, stale_rx) = mpsc::sync_channel(1);
        app.vendor_catalog_pending = Some((launch::Engine::DeepSeek, stale_rx));
        app.engine_selected = 3;
        app.select_new_engine();
        assert!(stale_tx.send(Some(vec![row("deepseek-flash", &[], None)])).is_err());
        assert_eq!(app.new_session.as_ref().unwrap().model, "glm-5.3-flash");
        let (tx, rx) = mpsc::sync_channel(1);
        app.vendor_catalog_pending = Some((launch::Engine::Glm, rx));
        app.new_session.as_mut().unwrap().catalog_pending = true;
        tx.send(Some(vec![row("unknown-glm", &[], None)])).unwrap();
        assert!(app.poll_vendor_catalog());
        assert!(app.new_session.as_ref().unwrap().models.is_empty());
        assert!(app.new_session.as_ref().unwrap().model.is_empty());
        assert!(app.new_session.as_ref().unwrap().catalog_note.contains("choose another engine"));
        app.new_session.as_mut().unwrap().field = 3;
        app.new_session_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.pending_launches.is_empty());
        assert!(app.notice.contains("No verified models"));

        app.engine_selected = 3;
        app.select_new_engine();
        let (tx, rx) = mpsc::sync_channel(1);
        app.vendor_catalog_pending = Some((launch::Engine::Glm, rx));
        tx.send(Some(vec![row("glm-5.3-flash", &[], None)])).unwrap();
        assert!(app.poll_vendor_catalog());
        assert!(app.new_session.as_ref().unwrap().catalog_note.contains("measured effort fallback"));

        app.engine_selected = 2;
        app.select_new_engine();
        let (tx, rx) = mpsc::sync_channel(1);
        app.vendor_catalog_pending = Some((launch::Engine::DeepSeek, rx));
        app.new_session.as_mut().unwrap().catalog_pending = true;
        tx.send(None).unwrap();
        assert!(app.poll_vendor_catalog());
        assert_eq!(app.new_session.as_ref().unwrap().models, ["deepseek-flash", "deepseek-v4-pro"]);
        assert!(app.new_session.as_ref().unwrap().catalog_note.contains("Static fallback"));
        app.new_session.as_mut().unwrap().model_efforts.insert("deepseek-flash".into(), vec!["low".into()]);
        app.engine_selected = 2;
        app.select_new_engine();
        assert_eq!(app.new_session.as_ref().unwrap().model_efforts["deepseek-flash"], ["none", "low", "high", "max"]);
    }

    #[test]
    fn launched_session_opens_in_active_pane_and_failure_is_visible() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"old", "model":"old"}));
        app.launching = true;
        app.apply_daemon_frame(&json!({"type":"launch_reply", "ok":true, "session_id":"new", "group":0}));
        assert_eq!(app.groups[0].active_id(), Some("new"));
        assert!(!app.launching);
        app.launching = true;
        app.apply_daemon_frame(&json!({"type":"launch_reply", "ok":false,
            "message":"DEEPSEEK_API_KEY is required"}));
        assert!(app.notice.contains("DEEPSEEK_API_KEY"));
        assert_eq!(app.groups[0].active_id(), Some("new"));
        app.launching = true;
        app.apply_daemon_frame(&json!({"type":"launch_reply", "ok":false, "started":true,
            "session_id":"surviving", "group":0, "message":"socket refused"}));
        assert!(app.notice.contains("doxa-rs attach surviving"));
    }

    #[test]
    fn clear_replaces_only_active_tab_after_launch_and_defers_finalization() {
        let mut app = App::default();
        app.clear_preflight_error = None;
        app.groups[0].tabs = vec!["other".into(), "old".into()];
        app.groups[0].active = 1;
        app.session_identity.insert("old".into(), (Some("codex".into()), Some("old-model".into())));
        app.session_cwds.insert("old".into(), PathBuf::from("/repo"));
        app.session_activity.insert("old".into(), (false, 0));
        app.collections.push(crate::collections::Collection { name:"Work".into(), sessions:vec!["old".into()], collapsed:false, colour:None });
        app.input = "/clear".into();
        assert!(app.submit_local_command());
        assert_eq!(app.pending_launches.len(), 1);
        assert_eq!(app.pending_launches[0].0.engine, launch::Engine::Codex);
        assert_eq!(app.pending_launches[0].0.cwd.as_deref(), Some(Path::new("/repo")));
        assert!(app.pending_stops.is_empty());
        app.apply_daemon_frame(&json!({"type":"launch_reply","ok":true,"session_id":"fresh","group":0}));
        assert_eq!(app.groups[0].tabs, ["other", "fresh"]);
        assert_eq!(app.groups[0].active_id(), Some("fresh"));
        assert_eq!(app.collections[0].sessions, ["fresh"]);
        assert_eq!(app.clear_stop_after_save, ["old"]);
        assert!(app.pending_stops.is_empty());
        assert!(app.finish_clear_swap(true));
        assert_eq!(app.pending_clear_finalizes, ["old"]);
        assert!(app.clear_stop_after_save.is_empty());
    }

    #[test]
    fn clear_failure_and_unsupported_forms_preserve_the_old_session() {
        let mut app = App::default();
        app.clear_preflight_error = None;
        app.groups[0].tabs = vec!["old".into()];
        app.session_identity.insert("old".into(), (Some("codex".into()), None));
        app.session_cwds.insert("old".into(), PathBuf::from("/repo"));
        app.input = "/clear now".into();
        assert!(app.submit_local_command());
        assert_eq!(app.notice, "Usage: /clear");
        assert!(app.pending_launches.is_empty());
        app.input = "/clear".into();
        assert!(app.submit_local_command());
        app.pending_launches.clear(); // the bridge accepted the launch
        app.apply_daemon_frame(&json!({"type":"launch_reply","ok":false,"message":"spawn failed"}));
        assert_eq!(app.groups[0].tabs, ["old"]);
        assert!(app.clear_stop_after_save.is_empty());
        assert!(app.pending_stops.is_empty());
        assert!(app.pending_prompts.is_empty());
        app.session_activity.insert("old".into(), (true, 1));
        app.input = "/clear".into();
        assert!(app.submit_local_command());
        assert!(app.notice.contains("wait for the current turn"));
        assert!(app.pending_launches.is_empty());
    }

    #[test]
    fn clear_requires_persistent_state_and_rolls_back_unsaved_swap() {
        let mut app = App::default();
        app.groups[0].tabs = vec!["old".into()];
        app.session_identity.insert("old".into(), (Some("codex".into()), None));
        app.session_cwds.insert("old".into(), PathBuf::from("/repo"));
        app.input = "/clear".into();
        assert!(app.submit_local_command());
        assert!(app.notice.contains("persistent tabset unavailable"));
        assert!(app.pending_launches.is_empty());

        app.clear_preflight_error = None;
        app.input = "/clear".into();
        assert!(app.submit_local_command());
        app.apply_daemon_frame(&json!({"type":"launch_reply","ok":true,"session_id":"fresh","group":0}));
        assert!(app.finish_clear_swap(false));
        assert_eq!(app.groups[0].tabs, ["old"]);
        assert!(app.clear_stop_after_save.is_empty());
        assert_eq!(app.pending_clear_finalizes, ["fresh"]);
    }

    #[test]
    fn clear_restarts_from_shared_checkout_when_old_session_is_in_a_worktree() {
        let dir = tempfile::tempdir().unwrap();
        let main = dir.path().join("main");
        let old_tree = dir.path().join("old-tree");
        std::fs::create_dir(&main).unwrap();
        let git = |args: &[&str]| assert!(std::process::Command::new("git").args(args)
            .current_dir(&main).status().unwrap().success());
        git(&["init", "-q"]);
        std::fs::write(main.join("tracked.txt"), "base\n").unwrap();
        git(&["add", "tracked.txt"]);
        git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: base"]);
        git(&["worktree", "add", "--detach", "-q", old_tree.to_str().unwrap()]);
        let mut app = App::default();
        app.clear_preflight_error = None;
        app.groups[0].tabs.push("old".into());
        app.session_identity.insert("old".into(), (Some("codex".into()), None));
        app.session_cwds.insert("old".into(), old_tree);
        app.input = "/clear".into();
        assert!(app.submit_local_command());
        assert_eq!(app.pending_launches[0].0.cwd.as_deref(), Some(main.as_path()));
    }

    #[test]
    fn router_attach_failure_releases_pending_target() {
        const CHILD: &str = "DOXA_TEST_ATTACH_FAILURE";
        if std::env::var_os(CHILD).is_none() {
            let dir = tempfile::tempdir().unwrap();
            let status = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "ui::tests::router_attach_failure_releases_pending_target"])
                .env(CHILD, "1").env("DOXA_RUNTIME_DIR", dir.path())
                .env("DOXA_HOME", dir.path().join("home")).env("HOME", dir.path())
                .status().unwrap();
            assert!(status.success());
            return;
        }
        let bridge = crate::bridge::connect_sessions_inner(&[], true).unwrap();
        let mut app = App::default();
        app.groups[0].tabs.push("owned".into());
        app.attach_selected("vanished");
        assert!(app.attaching_ids.contains("vanished"));
        assert!(!dispatch_attaches(&mut app, &bridge.commands));
        let frame = bridge.frames.recv_timeout(Duration::from_secs(2)).unwrap().into_legacy_value();
        assert_eq!(frame["session_id"], "vanished");
        assert_eq!(frame["group"], 0);
        assert_eq!(frame["ok"], false);
        assert!(app.apply_daemon_frame(&frame));
        assert!(app.attaching_ids.is_empty());
        assert!(app.notice.starts_with("Attach failed"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('w'), KeyModifiers::CONTROL)));
        assert!(app.groups[0].tabs.is_empty(), "failed attach must not veto closing the owned tab");
        assert!(!app.should_quit);
        bridge.shutdown();
    }

    #[test]
    fn asynchronous_launch_and_attach_activation_keep_drafts_with_their_owner() {
        for kind in ["launch_reply", "attach_reply"] {
            let mut app = App::default();
            app.groups[0].tabs.push("a".into());
            app.input = "private draft for a".into(); app.input_cursor = app.input.len();
            if kind == "launch_reply" { app.launching = true; }
            else { app.attaching_ids.insert("b".into()); }
            assert!(app.apply_daemon_frame(&json!({"type":kind,"ok":true,"session_id":"b","group":0})));
            assert_eq!(app.groups[0].active_id(), Some("b"));
            assert!(app.input.is_empty());
            assert_eq!(app.input_drafts[&(0,"a".into())].0, "private draft for a");
            app.focus = Focus::Prompt;
            app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
            assert!(app.pending_prompts.is_empty(), "old draft must never submit to the new session");
            app.focus = Focus::Tabs;
            app.handle(Event::Key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE)));
            assert_eq!(app.input, "private draft for a");
        }
    }

    #[test]
    fn background_attach_activation_restores_destination_draft_and_preserves_clipboard_target() {
        let mut app = App::default();
        app.groups[0].tabs.push("a".into());
        app.input = "original".into(); app.input_cursor = app.input.len();
        let target = app.clipboard_target();
        app.clipboard_job = Some(crate::clipboard::Job::fixture(target, Ok(" pasted".into())));
        app.input_drafts.insert((1,"b".into()), ("destination".into(),11));
        app.attaching_ids.insert("b".into());
        app.apply_daemon_frame(&json!({"type":"attach_reply","ok":true,"session_id":"b","group":1}));
        assert_eq!(app.active_group, 1);
        assert_eq!(app.input, "destination"); assert_eq!(app.input_cursor,11);
        assert!(app.poll_clipboard());
        assert_eq!(app.input, "destination");
        assert_eq!(app.input_drafts[&(0,"a".into())].0, "original pasted");
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn daemon_activation_keeps_same_session_pane_drafts_independent() {
        let mut app = App::default();
        app.groups[0].tabs.push("shared".into());
        app.groups[1].tabs.push("shared".into());
        app.input = "left".into(); app.input_cursor = 4;
        app.input_drafts.insert((1,"shared".into()), ("right".into(),5));
        app.attaching_ids.insert("shared".into());
        app.apply_daemon_frame(&json!({"type":"attach_reply","ok":true,"session_id":"shared","group":1}));
        assert_eq!(app.input, "right");
        // The attach reply moves a provisional group-zero copy. Its old draft
        // is discarded because that tab no longer exists, never applied right.
        assert_eq!(app.input_cursor,5);
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn rail_open_uses_manual_slot_admission_but_can_focus_existing_tabs() {
        let mut app = App::default();
        app.sidebar_auto = false; app.rail_visible = true;
        app.sessions = (0..=panes::MAX_TABS).map(|index| Session {
            id:format!("slot-{index}"), title:String::new(), collection:String::new(),
            transcript:String::new(), status:"Ready".into(),
        }).collect();
        app.groups[0].tabs = app.sessions[..panes::MAX_TABS].iter().map(|session|session.id.clone()).collect();
        app.handle(Event::Resize(120,32)); app.focus = Focus::Rail;
        app.rail_selected = panes::MAX_TABS;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE)));
        assert_eq!(app.groups[0].tabs.len(),panes::MAX_TABS);
        assert!(app.notice.contains("256 tab slots occupied"));
        app.focus = Focus::Rail; app.rail_selected = 2;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(),Some("slot-2"));
        app.groups[0].tabs.pop(); app.focus = Focus::Rail; app.rail_selected = panes::MAX_TABS;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(),Some("slot-256"));
        assert_eq!(app.groups[0].tabs.len(),panes::MAX_TABS);
        assert!(app.pending_attaches.is_empty()); assert!(app.pending_launches.is_empty());
    }

    #[test]
    fn saved_resume_completion_keeps_initiating_pane_and_rejects_removed_owner() {
        const CHILD: &str = "DOXA_TEST_RESUME_OWNER";
        if std::env::var_os(CHILD).is_none() {
            let dir = tempfile::tempdir().unwrap();
            let status = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "ui::tests::saved_resume_completion_keeps_initiating_pane_and_rejects_removed_owner"])
                .env(CHILD,"1").env("DOXA_RUNTIME_DIR",dir.path())
                .env("DOXA_HOME",dir.path().join("home")).env("HOME",dir.path())
                .status().unwrap(); assert!(status.success()); return;
        }
        let mut app = App::default();
        app.groups[0].tabs.push("a".into()); app.groups[1].tabs.push("b".into());
        let (tx,rx) = mpsc::sync_channel(1);
        app.resume_pending = Some((0,Some("a".into()),rx));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab,KeyModifiers::ALT)));
        app.input = "other pane draft".into(); app.input_cursor = app.input.len();
        tx.send(("saved".into(),Ok(launch::LaunchOptions::default()))).unwrap();
        assert!(app.poll_resume());
        assert_eq!(app.pending_launches[0].2,0); assert_eq!(app.active_group,1);
        assert_eq!(app.input,"other pane draft"); assert!(app.pending_prompts.is_empty());
        app.pending_launches.clear(); app.launching = false;
        let (tx,rx) = mpsc::sync_channel(1);
        app.resume_pending = Some((0,Some("a".into()),rx));
        app.groups[0].tabs = vec!["replacement".into()];
        tx.send(("saved".into(),Ok(launch::LaunchOptions::default()))).unwrap();
        assert!(app.poll_resume()); assert!(app.pending_launches.is_empty());
        assert!(app.notice.contains("original pane")); assert_eq!(app.input,"other pane draft");
    }

    #[test]
    fn closing_tabs_bounds_retained_records_and_reattach_reuses_existing_record() {
        let mut app = App::default();
        app.groups[0].tabs = (0..panes::MAX_TABS).map(|index|format!("slot-{index}")).collect();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('w'),KeyModifiers::CONTROL)));
        assert_eq!(app.groups[0].tabs.len(),panes::MAX_TABS-1);
        assert_eq!(app.detached_this_run,["slot-0"]);
        app.open_engine_picker();
        assert!(!app.engine_picker); assert!(app.notice.contains("256 retained session records"));
        app.attach_selected("slot-0");
        assert_eq!(app.pending_attaches,[("slot-0".into(),0)]);
        app.apply_daemon_frame(&json!({"type":"attach_reply","ok":true,"session_id":"slot-0","group":0}));
        assert_eq!(app.groups[0].tabs.len(),panes::MAX_TABS);
        // A confirmed killed ID releases its retained-record reservation.
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('w'),KeyModifiers::CONTROL)));
        app.killed_this_run.insert("slot-0".into());
        assert!(app.manual_tab_available());
        // A form opened earlier must also recheck admission at submission.
        app.killed_this_run.clear();
        app.new_session = Some(NewSession { isolation:doxa_isolation::Profile::Native,engine:launch::Engine::Codex,model:"model".into(),models:Vec::new(),
            model_efforts:HashMap::new(),catalog_note:String::new(),catalog_pending:false,launch_error:None,retry_allowed:true,effort:None,prompt:String::new(),field:1 });
        app.new_session_key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE));
        assert!(app.pending_launches.is_empty()); assert!(app.new_session.is_some());
    }

    #[test]
    fn launch_reply_keeps_the_group_chosen_when_launch_started() {
        let mut app = App::default();
        app.launching = true;
        app.active_group = 1;
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"new", "engine":"codex"}));
        assert_eq!(app.groups[0].active_id(), Some("new"));
        app.active_group = 0;
        app.apply_daemon_frame(&json!({"type":"launch_reply", "ok":true,
            "session_id":"new", "group":1}));
        assert_eq!(app.groups[0].active_id(), None);
        assert_eq!(app.groups[1].active_id(), Some("new"));
        assert_eq!(app.active_group, 0);
        assert!(!app.apply_daemon_frame(&json!({"type":"launch_reply", "ok":true,
            "session_id":"unrequested", "group":1})));
    }

    #[test]
    fn internal_telemetry_refresh_cannot_restore_running_after_turn_done() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "engine":"codex",
            "running":true, "lore_scrub":"ready"}));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"s",
            "event":{"type":"turn_done", "data":{"is_error":false}}}));
        assert_eq!(app.session_activity.get("s").unwrap().0, false);
        let notice = app.notice.clone();
        app.apply_daemon_frame(&json!({"type":"telemetry_status", "session_id":"s",
            "status":{"session_id":"s", "running":true, "lore_scrub":"unavailable"}}));
        assert_eq!(app.session_activity.get("s").unwrap().0, false);
        assert_eq!(app.notice, notice);
        assert_eq!(app.session_telemetry.get("s").unwrap().lore.as_deref(), Some("scrub unavailable"));
    }

    #[test]
    fn deepseek_balance_chip_requires_valid_available_balance() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"deep", "engine":"deepseek"}));
        app.groups[0].tabs = vec!["deep".into()];
        assert!(!app.chips(0).iter().any(|(kind, _)| *kind == "balance"));
        app.apply_daemon_frame(&json!({"type":"telemetry_status", "session_id":"deep",
            "status":{"session_id":"deep", "billing":{"mode":"api","balance":"$3.25 · ¥25.50"}}}));
        assert!(app.chips(0).iter().any(|(kind, label)| *kind == "balance" && label == "Balance $3.25 · ¥25.50"));
        app.apply_daemon_frame(&json!({"type":"telemetry_status", "session_id":"deep",
            "status":{"session_id":"deep", "billing":null}}));
        assert!(!app.chips(0).iter().any(|(kind, _)| *kind == "balance"));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"glm", "engine":"glm",
            "billing":{"mode":"api","balance":"$100.00"}}));
        app.groups[0].tabs = vec!["glm".into()];
        assert!(!app.chips(0).iter().any(|(kind, _)| *kind == "balance"));
    }

    fn painted(app: &App) -> String {
        let mut terminal = Terminal::new(TestBackend::new(100, 28)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let buffer = terminal.backend().buffer();
        (0..28).map(|y| (0..100).map(|x| buffer[(x, y)].symbol()).collect::<String>())
            .collect::<Vec<_>>().join("\n")
    }

    #[test]
    fn repository_chip_tracks_each_panes_actual_session_and_invalidates_stale_work() {
        use doxa_worktrees::RepoStatus;
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(220, 32));
        app.groups[0].tabs = vec!["a".into(), "b".into()];
        app.groups[1].tabs = vec!["c".into()];
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"a","cwd":"/tmp/a"}));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"b","cwd":"/tmp/b"}));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"c","cwd":"/tmp/c"}));
        app.set_repo_status("a", RepoStatus::Repository {
            repo: "project".into(), base: Some("main".into()),
            checked_out: Some("doxa/a".into()), sha: Some("1234567".into()),
            worktree: Some("doxa/a".into()),
        });
        app.set_repo_status("b", RepoStatus::Directory { name: "scratch".into() });
        app.set_repo_status("c", RepoStatus::Repository {
            repo: "other".into(), base: Some("feature".into()),
            checked_out: Some("feature".into()), sha: Some("abcdef0".into()),
            worktree: None,
        });
        assert!(app.chips(0).contains(&("repo", "project ⎇ main @1234567".into())));
        assert_eq!(app.repo_detail(0).as_deref(), Some("base main · HEAD doxa/a · managed worktree doxa/a"));
        let rendered = painted_at(&app, 220, 32);
        assert!(rendered.contains("project ⎇ main @1234567"));
        assert!(rendered.contains("u ? · scope ?"));
        assert!(app.chips(1).contains(&("repo", "other ⎇ feature @abcdef0".into())));
        app.groups[0].active = 1;
        assert!(app.chips(0).contains(&("directory", "dir scratch".into())));
        assert!(!app.chips(0).iter().any(|(kind, _)| *kind == "repo"));
        app.handle(Event::Resize(100, 28));
        assert!(app.chips(0).contains(&("directory", "dir scratch".into())));
        app.apply_daemon_frame(&json!({"type":"event","session_id":"c",
            "event":{"type":"branch_changed","data":{"base":"main"}}}));
        assert!(!app.chips(1).iter().any(|(kind, _)| *kind == "repo"));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"b","cwd":"/tmp/new"}));
        assert!(!app.chips(0).iter().any(|(kind, _)| *kind == "directory"));
        let (tx, rx) = mpsc::sync_channel(1);
        let old_epoch = app.repo_epoch.get("b").copied().unwrap_or_default();
        app.repo_pending = Some(("b".into(), PathBuf::from("/tmp/new"), old_epoch, rx));
        app.invalidate_repo("b");
        tx.send((Some(RepoStatus::Directory { name: "stale".into() }), Some(PathBuf::from("/tmp/new")))).unwrap();
        app.poll_repo();
        assert!(!app.chips(0).iter().any(|(kind, _)| *kind == "directory"));
    }

    fn painted_at(app: &App, width: u16, height: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let buffer = terminal.backend().buffer();
        (0..height).map(|y| (0..width).map(|x| buffer[(x, y)].symbol()).collect::<String>())
            .collect::<Vec<_>>().join("\n")
    }

    fn scrolled_picker_app() -> App {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("session".into());
        app.session_capabilities.insert("session".into(), EngineCapabilities::from_session_controls(&json!({"can_set_model":true})));
        app
    }

    #[test]
    fn model_submenu_scrollbar_appears_only_for_overflow_and_tracks_selection() {
        let mut app = scrolled_picker_app();
        app.model_picker = Some(ModelPicker { session_id: "session".into(),
            models: (0..30).map(|index| format!("model-{index:02}")).collect(),
            selected: 0, note: "Verified models".into(), loading: false,
            catalog_pending: false });
        let column = |app: &App| {
            let menu = app.active_chooser_rect().unwrap();
            let mut terminal = Terminal::new(TestBackend::new(100, 28)).unwrap();
            terminal.draw(|frame| app.draw(frame)).unwrap();
            let buffer = terminal.backend().buffer();
            (menu.y + 1..menu.bottom() - 1)
                .map(|row| buffer[(menu.right() - 2, row)].symbol().to_owned())
                .collect::<Vec<_>>()
        };
        let top = column(&app);
        assert!(top.iter().any(|symbol| symbol != " "), "overflow needs a scrollbar");
        app.model_picker.as_mut().unwrap().selected = 29;
        let bottom = column(&app);
        assert_ne!(top, bottom, "scrollbar must follow the visible models");
        assert_eq!(bottom.last().map(String::as_str), Some("█"),
            "the thumb must reach the final track cell at the last page");
        app.model_picker.as_mut().unwrap().models.truncate(2);
        app.model_picker.as_mut().unwrap().selected = 0;
        assert!(column(&app).iter().all(|symbol| symbol == " "),
            "a fitting submenu should not show a scrollbar");
    }

    fn hover_first_picker_row(app: &mut App, offset: u16) -> (Rect, usize) {
        painted_at(app, 100, 28);
        let start = app.chooser_view_start.get();
        assert!(start > 0, "test must start with a scrolled viewport");
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: menu.y + offset, modifiers: KeyModifiers::NONE }));
        painted_at(app, 100, 28);
        assert_eq!(app.chooser_view_start.get(), start, "hover must not shift visible entries");
        (menu, start)
    }

    fn click_picker_row(app: &mut App, menu: Rect, offset: u16) {
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + offset, modifiers: KeyModifiers::NONE }));
    }

    #[test]
    fn native_welcome_keeps_loading_failure_and_transcript_distinct() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 32));
        app.persist_preferences = true;
        app.preferences.set_test("boot_banner", "1");
        app.preferences.set_test("background", "opaque");
        app.awaiting_initial_attach = true;
        let connecting = painted_at(&app, 100, 32);
        assert!(connecting.contains("Connecting to session"));
        assert!(!connecting.contains("could not start") && !connecting.contains("Select one in the rail"));
        app.awaiting_initial_attach = false;
        app.startup_recovery = Some("Fresh session could not start".into());
        let recovery = painted_at(&app, 100, 32);
        assert!(recovery.contains("Session could not start") && recovery.contains("/setup") && recovery.contains("/engine"));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"codex","model":"gpt-6-sol"}));
        assert!(app.sessions[0].transcript.is_empty());
        let mut terminal = Terminal::new(TestBackend::new(100, 32)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let buffer = terminal.backend().buffer();
        let logo = buffer.content.iter().filter(|cell|cell.symbol()=="█" && cell.fg==theme::ACCENT).collect::<Vec<_>>();
        assert!(!logo.is_empty());
        assert!(logo.iter().all(|cell|cell.fg==theme::ACCENT && cell.bg==theme::BASE));
        let ready = painted_at(&app, 100, 32);
        assert!(ready.contains("Session ready") && ready.contains("gpt-6-sol"));
        app.sessions[0].transcript = "Actual conversation".into();
        let conversation = painted_at(&app, 100, 32);
        assert!(conversation.contains("Actual conversation") && !conversation.contains("███████████████"));
        app.sessions[0].transcript.clear();
        app.preferences.set_test("boot_banner", "0");
        assert!(!painted_at(&app, 100, 32).contains("/________\\"));
    }

    #[test]
    fn minimum_stop_confirmation_shows_confirmation_and_cancel_keys() {
        let mut app = App::default();
        app.stop_confirmation = Some("a-long-session-identifier-that-can-wrap".into());
        let mut terminal = Terminal::new(TestBackend::new(40, 12)).unwrap();
        terminal.draw(|frame| app.draw_stop_confirmation(frame, Rect::new(0, 0, 40, 12))).unwrap();
        let buffer = terminal.backend().buffer();
        let text = (0..12).map(|y| (0..40).map(|x| buffer[(x, y)].symbol()).collect::<String>())
            .collect::<Vec<_>>().join("\n");
        assert!(text.contains("Y stop · Esc/N cancel"));
        assert!(text.contains("draft stay."));
    }

    #[test]
    fn initial_tool_section_navigation_selects_visible_edge_without_skipping() {
        for forward in [false, true] {
            let mut app = scrolled_picker_app();
            app.sessions.push(Session { id: "session".into(), title: "Session".into(),
                collection: String::new(), transcript: "Tool: Read started".into(), status: "Ready".into() });
            *app.visible_tool_sections.borrow_mut() = (0..3).map(|index|
                (Rect::new(0, index as u16, 20, 1), 0, "session".into(), transcript_tools::FoldKey::Section(index))).collect();
            assert!(app.select_tool_section(forward));
            assert_eq!(app.selected_tool_sections["session"], transcript_tools::FoldKey::Section(if forward { 0 } else { 2 }));
            assert!(app.select_tool_section(forward));
            assert_eq!(app.selected_tool_sections["session"], transcript_tools::FoldKey::Section(1));
        }
    }

    #[test]
    fn chooser_bottom_border_never_activates_hidden_choice() {
        for kind in ["engine", "model", "effort", "permission", "slash"] {
            let mut app = scrolled_picker_app();
            match kind {
                "engine" => app.engine_picker = true,
                "model" => app.model_picker = Some(ModelPicker { session_id: "session".into(),
                    models: vec!["one".into(), "two".into(), "three".into()], selected: 0,
                    note: String::new(), loading: false, catalog_pending: false }),
                "effort" => app.effort_picker = Some(EffortPicker { session_id: "session".into(),
                    engine: "deepseek".into(), model: "deepseek-flash".into(),
                    levels: vec!["none".into(), "low".into(), "high".into()], selected: 0 }),
                "permission" => app.permission_picker = Some(("session".into(), 0)),
                _ => { app.focus = Focus::Prompt; app.input = "/".into(); }
            }
            app.active_chooser_rect();
            app.chooser_height_override.set(Some(5));
            let menu = app.active_chooser_rect().unwrap();
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
                column: menu.x + 2, row: menu.bottom() - 1, modifiers: KeyModifiers::NONE }));
            assert!(app.pending_model_changes.is_empty(), "{kind}");
            assert!(app.pending_effort_changes.is_empty(), "{kind}");
            assert!(app.pending_permission_changes.is_empty(), "{kind}");
            assert!(app.new_session.is_none(), "{kind}");
            if kind == "engine" { assert!(app.engine_picker); }
            if kind == "model" { assert!(app.model_picker.is_some()); }
            if kind == "effort" { assert_eq!(app.effort_picker.as_ref().unwrap().selected, 0); }
            if kind == "permission" { assert_eq!(app.permission_picker.as_ref().unwrap().1, 0); }
            if kind == "slash" { assert_eq!(app.input, "/"); }
        }
    }

    #[test]
    fn scrolled_repo_hover_redraw_click_opens_same_visible_directory() {
        let root = tempfile::tempdir().unwrap();
        let paths: Vec<_> = (0..30).map(|i| root.path().join(format!("child-{i:02}"))).collect();
        for path in &paths { std::fs::create_dir(path).unwrap(); }
        let mut app = scrolled_picker_app();
        app.repo_picker = Some(RepoPicker { current_dir: root.path().to_path_buf(),
            paths: paths.clone(), selected: 24 });
        app.active_chooser_rect();
        app.chooser_height_override.set(Some(8));
        let (menu, start) = hover_first_picker_row(&mut app, 2);
        assert_eq!(app.repo_picker.as_ref().unwrap().selected, start);
        click_picker_row(&mut app, menu, 2);
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, paths[start]);
        assert_eq!(app.chooser_height_override.get(), Some(8), "folder browsing keeps the chosen height");
    }

    #[test]
    fn scrolled_branch_hover_redraw_click_requests_same_visible_branch() {
        let mut app = scrolled_picker_app();
        let branches: Vec<_> = (0..30).map(|i| format!("branch-{i:02}")).collect();
        app.branch_picker = Some(BranchPicker { session_id: "session".into(), base: "main".into(),
            branches: branches.clone(), selected: 24 });
        let (menu, start) = hover_first_picker_row(&mut app, 2);
        click_picker_row(&mut app, menu, 2);
        assert!(matches!(&app.pending_queue_commands[0], crate::bridge::WorkerCommand::Branch(id, Some(branch))
            if id == "session" && branch == &branches[start]));
    }

    #[test]
    fn scrolled_model_hover_redraw_click_requests_same_visible_model() {
        let mut app = scrolled_picker_app();
        let models: Vec<_> = (0..30).map(|i| format!("model-{i:02}")).collect();
        app.model_picker = Some(ModelPicker { session_id: "session".into(), models: models.clone(),
            selected: 24, note: "Verified models".into(), loading: false, catalog_pending: false });
        let (menu, start) = hover_first_picker_row(&mut app, 5);
        click_picker_row(&mut app, menu, 5);
        assert_eq!(app.pending_model_changes, vec![("session".into(), models[start].clone())]);
    }

    #[test]
    fn pending_model_catalog_hover_and_click_match_visible_rows() {
        let mut app = scrolled_picker_app();
        let models: Vec<_> = (0..30).map(|i| format!("model-{i:02}")).collect();
        app.model_picker = Some(ModelPicker { session_id: "session".into(), models: models.clone(),
            selected: 24, note: "Verified models".into(), loading: false, catalog_pending: true });
        let menu = app.active_chooser_rect().unwrap();
        click_picker_row(&mut app, menu, 5);
        assert!(app.pending_model_changes.is_empty(), "probe text is not a model");
        assert!(app.model_picker.is_some());
        let picker = app.model_picker.as_ref().unwrap();
        let start = chooser_visible_start(&app.chooser_view_start, picker.selected, picker.visible_rows(menu.height));
        let text = painted_at(&app, 100, 28);
        assert!(text.lines().nth(usize::from(menu.y + 6)).unwrap().contains(&models[start]));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: menu.y + 6, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.model_picker.as_ref().unwrap().selected, start);
        let hovered = painted_at(&app, 100, 28);
        assert!(hovered.lines().nth(usize::from(menu.y + 6)).unwrap().contains(&models[start]));
        click_picker_row(&mut app, menu, 6);
        assert_eq!(app.pending_model_changes, vec![("session".into(), models[start].clone())]);
    }

    #[test]
    fn long_lore_claim_and_proposal_rows_keep_hover_click_target() {
        let cwd = tempfile::tempdir().unwrap();
        for proposal_mode in [false, true] {
            let mut app = scrolled_picker_app();
            app.lore_picker = Some(LorePicker {
            session_id: None,
                rows: (1..=2).map(|id| lore_picker::Belief { id, subject: format!("belief-{id}"),
                    claim: "long claim ".repeat(80), truncated: false, confidence: 0.9,
                    evidence_count: None, recency:None }).collect(),
                proposals: (1..=2).map(|id| lore_picker::Proposal { pid: format!("proposal-{id}"),
                    kind: "belief".into(), action: "add".into(), scope: "project".into(),
                    summary: "long summary ".repeat(80), source_session_id: None, source_project: None }).collect(),
                selected: 0, query: String::new(), filter_focused: false, offset: 0, status: "long status ".repeat(40),
                evidence: None, pending: None, proposal_mode, cluster_mode: false, all_proposals: Vec::new(), review: None, review_scroll: 0,
                review_seen: 0, review_width: 0, armed_resolution: None, can_resolve: false,
                resolving: false, cwd: cwd.path().display().to_string(), belief_review: None, belief_intent: None,
                can_act_on_beliefs: false, belief_action: None, belief_note: String::new(),
                retract_armed: false, belief_acting: false, result_status: None,
            });
            let menu = app.active_chooser_rect().unwrap();
            let offset = if proposal_mode { 4 } else { 3 };
            let rendered = painted_at(&app, 100, 28);
            let row = usize::from(menu.y + offset);
            assert!(rendered.lines().nth(row).unwrap().contains(if proposal_mode { "proposal-2" } else { "#2" }));
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
                column: menu.x + 2, row: menu.y + offset, modifiers: KeyModifiers::NONE }));
            assert_eq!(app.lore_picker.as_ref().unwrap().selected, 1);
            let hovered = painted_at(&app, 100, 28);
            assert!(hovered.lines().nth(row).unwrap().contains(if proposal_mode { "proposal-2" } else { "#2" }));
            click_picker_row(&mut app, menu, offset);
            let picker = app.lore_picker.as_ref().unwrap();
            assert_eq!(picker.selected, 1);
            assert!(picker.pending.is_some(), "click requests review of the highlighted entry");
        }
    }

    #[test]
    fn chooser_resize_resets_after_close_menu_change_and_pane_switch() {
        let mut app = scrolled_picker_app();
        app.engine_picker = true;
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y, modifiers: KeyModifiers::NONE }));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Drag(MouseButton::Left),
            column: menu.x + 2, row: menu.bottom() - 5, modifiers: KeyModifiers::NONE }));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Up(MouseButton::Left),
            column: menu.x + 2, row: menu.bottom() - 5, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.active_chooser_rect().unwrap().height, 5);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert_eq!(app.chooser_height_override.get(), None);
        app.engine_picker = true;
        assert_eq!(app.active_chooser_rect().unwrap().height, 7);
        app.chooser_height_override.set(Some(5));
        app.engine_picker = false;
        app.model_picker = Some(ModelPicker { session_id: "session".into(), models: vec!["one".into(), "two".into()],
            selected: 0, note: String::new(), loading: false, catalog_pending: false });
        assert_eq!(app.active_chooser_rect().unwrap().height, 8);
        app.chooser_height_override.set(Some(5));
        app.active_group = 1;
        app.groups[1].tabs.push("other".into());
        assert_eq!(app.active_chooser_rect().unwrap().height, 8);
    }

    #[test]
    fn repo_chip_picker_hovers_browses_and_opens_verified_directory_in_new_tab() {
        let root = tempfile::tempdir().unwrap();
        let child = root.path().join("child");
        std::fs::create_dir(&child).unwrap();
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("session".into());
        app.session_identity.insert("session".into(), (Some("codex".into()), None));
        app.session_cwds.insert("session".into(), child.clone());
        app.repo_cache.insert("session".into(),
            (Some(doxa_worktrees::RepoStatus::Directory { name: "child".into() }), Instant::now()));
        let mut initial = Terminal::new(TestBackend::new(100, 28)).unwrap();
        initial.draw(|frame| app.draw(frame)).unwrap();
        let hit = app.rendered_chip_hits.borrow().as_ref().unwrap().iter()
            .find(|hit| hit.kind == "directory").unwrap().clone();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: hit.rect.x, row: hit.rect.y, modifiers: KeyModifiers::NONE }));
        let menu = app.active_chooser_rect().unwrap();
        assert_eq!(app.repo_picker.as_ref().unwrap().paths, vec![child, root.path().to_path_buf()]);
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: menu.y + 3, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.repo_picker.as_ref().unwrap().selected, 1);
        let mut terminal = Terminal::new(TestBackend::new(100, 28)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert_eq!(terminal.backend().buffer()[(menu.x + 2, menu.y + 3)].bg, theme::HIGHLIGHT);
        app.repo_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, root.path());
        assert!(app.pending_launches.is_empty());
        app.repo_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.repo_picker.is_none());
        assert_eq!(app.pending_launches[0].0.cwd.as_deref(), Some(root.path()));
        assert_eq!(app.groups[0].active_id(), Some("session"));
    }

    #[test]
    fn repo_picker_mouse_scroll_click_and_unsafe_path_filter() {
        let root = tempfile::tempdir().unwrap();
        let child = root.path().join("child");
        let unsafe_child = root.path().join("unsafe\nname");
        std::fs::create_dir(&child).unwrap();
        std::fs::create_dir(&unsafe_child).unwrap();
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("session".into());
        app.session_identity.insert("session".into(), (Some("codex".into()), None));
        app.session_cwds.insert("session".into(), child);
        app.open_repo_picker(0);
        assert_eq!(app.repo_picker.as_ref().unwrap().paths.len(), 2);
        let menu = app.active_chooser_rect().unwrap();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::ScrollDown,
            column: menu.x + 2, row: menu.y + 2, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.repo_picker.as_ref().unwrap().selected, 1);
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 3, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, root.path());
        assert!(app.pending_launches.is_empty());
        app.session_cwds.insert("session".into(), unsafe_child);
        app.open_repo_picker(0);
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, root.path());
        assert!(!app.repo_picker.as_ref().unwrap().paths.iter().any(|path|
            path.to_string_lossy().contains("unsafe\nname")));
    }

    #[test]
    fn repo_picker_browses_child_directories_and_returns_to_parent() {
        let root = tempfile::tempdir().unwrap();
        let child = root.path().join("child");
        let grandchild = child.join("grandchild");
        std::fs::create_dir_all(&grandchild).unwrap();
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("session".into());
        app.session_identity.insert("session".into(), (Some("codex".into()), None));
        app.session_cwds.insert("session".into(), root.path().to_path_buf());
        app.open_repo_picker(0);
        let child_index = app.repo_picker.as_ref().unwrap().paths.iter()
            .position(|path| path == &child).unwrap();
        app.repo_picker.as_mut().unwrap().selected = child_index;
        app.repo_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, child);
        let grandchild_index = app.repo_picker.as_ref().unwrap().paths.iter()
            .position(|path| path == &grandchild).unwrap();
        app.repo_picker.as_mut().unwrap().selected = grandchild_index;
        app.repo_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, grandchild);
        app.repo_picker.as_mut().unwrap().selected = 1;
        app.repo_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.repo_picker.as_ref().unwrap().current_dir, child);
        assert!(app.pending_launches.is_empty());
    }

    #[test]
    fn repo_picker_does_not_substitute_another_sessions_directory() {
        let other = tempfile::tempdir().unwrap();
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("missing".into());
        app.session_cwds.insert("other".into(), other.path().to_path_buf());
        app.open_repo_picker(0);
        assert!(app.repo_picker.is_none());
        assert_eq!(app.notice, "Current session directory is unavailable");
    }

    #[test]
    fn repo_picker_shows_home_relative_paths_without_changing_targets() {
        let Some(home) = std::env::var_os("HOME").map(PathBuf::from) else { return; };
        let target = home.join("example").join("project");
        assert_eq!(repo_path_label(&target), "~/example/project");
    }

    #[test]
    fn branch_and_settings_rows_select_on_hover() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("session".into());
        app.branch_picker = Some(BranchPicker { session_id: "session".into(),
            base: "main".into(), branches: vec!["main".into(), "feature".into()], selected: 0 });
        let menu = app.active_chooser_rect().unwrap();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: menu.y + 3, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.branch_picker.as_ref().unwrap().selected, 1);
        app.branch_picker = None;
        app.settings_menu = Some(SettingsMenu { rows: settings_test_rows("120",false),
            selected: 0, draft: None, category: 0, edits: HashMap::new(), engine: "claude".into() });
        let menu = app.active_chooser_rect().unwrap();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: menu.y + 4, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.settings_menu.as_ref().unwrap().selected, 1);
    }

    #[test]
    fn ask_user_hover_click_and_border_drag_use_inline_menu() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"session", "engine":"codex"}));
        app.groups[0].tabs.push("session".into());
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"session",
            "event":{"type":"needs_input", "data":{"id":"question", "kind":"ask_user",
                "questions":[{"question":"Choose?", "options":[
                    {"label":"First", "description":"One"}, {"label":"Second", "description":"Two"}]}]}}}));
        let before = app.active_chooser_rect().unwrap();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: before.x + 2, row: before.y, modifiers: KeyModifiers::NONE }));
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Drag(MouseButton::Left),
            column: before.x + 2, row: before.y.saturating_sub(2), modifiers: KeyModifiers::NONE }));
        let menu = app.active_chooser_rect().unwrap();
        assert!(menu.height > before.height);
        app.mouse(MouseEvent { kind: MouseEventKind::Up(MouseButton::Left),
            column: menu.x + 2, row: menu.y, modifiers: KeyModifiers::NONE });
        let request = &app.input_requests[0];
        let second = (menu.y + 1..menu.bottom() - 1)
            .find(|&row| input_request_option_at(request, menu, row) == Some(2)).unwrap();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: second, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.input_requests[0].selected, 2);
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: second, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.pending_answers.len(), 1);
        assert_eq!(app.pending_answers[0].2["answers"]["Choose?"], "Second");
    }

    #[test]
    fn ask_user_mouse_mapping_matches_wrapped_visible_option() {
        let mut app = App::default();
        app.input_requests.push(InputRequest::from_event("session", &json!({
            "id":"question", "kind":"ask_user", "questions":[{"question":"Choose?",
                "options":[
                    {"label":"First option with several long words that wrap into more than one row",
                     "description":"A description that also wraps across the menu"},
                    {"label":"Second", "description":"Short"}]}]
        })).unwrap());
        app.groups[0].tabs.push("session".into());
        let menu = Rect::new(0, 0, 34, 16);
        let mut terminal = Terminal::new(TestBackend::new(menu.width, menu.height)).unwrap();
        terminal.draw(|frame| app.draw_request(frame, menu, true)).unwrap();
        let buffer = terminal.backend().buffer();
        let second_row = (1..menu.height - 1).find(|&row|
            (1..menu.width - 1).map(|x| buffer[(x, row)].symbol()).collect::<String>().contains("Second"))
            .unwrap();
        assert_eq!(input_request_option_at(&app.input_requests[0], menu, second_row), Some(2));
    }

    #[test]
    fn inline_chooser_upper_border_drag_resizes_and_hover_tracks_selected_row() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 30));
        app.groups[0].tabs.push("session".into());
        app.engine_picker = true;
        let before = app.active_chooser_rect().unwrap();
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: before.x + 3, row: before.y, modifiers: KeyModifiers::NONE }));
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Drag(MouseButton::Left),
            column: before.x + 3, row: before.y.saturating_sub(3), modifiers: KeyModifiers::NONE }));
        let after = app.active_chooser_rect().unwrap();
        assert!(after.height > before.height);
        assert_eq!(after.bottom(), before.bottom());
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Up(MouseButton::Left),
            column: before.x + 3, row: after.y, modifiers: KeyModifiers::NONE }));
        let row = after.y + if after.height >= 10 { 5 } else { 3 };
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: after.x + 2, row, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.engine_selected, 1);
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert_eq!(terminal.backend().buffer()[(after.x + 2, row)].bg, theme::HIGHLIGHT);
    }

    #[test]
    fn model_picker_hover_highlights_and_enter_activates_hovered_model() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("session".into());
        app.session_capabilities.insert("session".into(), EngineCapabilities::from_session_controls(&json!({"can_set_model":true})));
        app.model_picker = Some(ModelPicker { session_id: "session".into(),
            models: vec!["first".into(), "second".into()], selected: 0,
            note: "Verified models".into(), loading: false, catalog_pending: false });
        let menu = app.active_chooser_rect().unwrap();
        let row = menu.y + app.model_picker.as_ref().unwrap().row_offset() + 1;
        assert!(app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.model_picker.as_ref().unwrap().selected, 1);
        let mut terminal = Terminal::new(TestBackend::new(100, 28)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert_eq!(terminal.backend().buffer()[(menu.x + 2, row)].bg, theme::HIGHLIGHT);
        app.model_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.pending_model_changes, vec![("session".into(), "second".into())]);
    }

    #[test]
    fn chip_hover_delays_tooltips_and_resets_owner_without_blocking() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(160, 32));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"codex","model":"gpt-6-sol"}));
        let mut terminal = Terminal::new(TestBackend::new(160, 32)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let engine = app.rendered_chip_hits.borrow().as_ref().unwrap().iter().find(|hit|hit.kind=="engine").unwrap().clone();
        assert_eq!(terminal.backend().buffer()[(engine.rect.x,engine.rect.y)].fg, theme::TEXT);
        app.handle(Event::Mouse(MouseEvent {kind:MouseEventKind::Moved,column:engine.rect.x,row:engine.rect.y,modifiers:KeyModifiers::NONE}));
        let since = app.chip_hover_started.as_ref().unwrap().1;
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert_eq!(terminal.backend().buffer()[(engine.rect.x,engine.rect.y)].fg, theme::ACCENT);
        assert!(!app.tick_chip_hover(since + Duration::from_millis(499)));
        assert!(app.tick_chip_hover(since + Duration::from_millis(500)));
        assert!(painted_at(&app,160,32).contains(chip_hint("engine")));
        assert!(!app.tick_chip_hover(since + Duration::from_secs(1)), "only one threshold redraw");
        let model = app.rendered_chip_hits.borrow().as_ref().unwrap().iter().find(|hit|hit.kind=="model").unwrap().clone();
        app.handle(Event::Mouse(MouseEvent {kind:MouseEventKind::Moved,column:model.rect.x,row:model.rect.y,modifiers:KeyModifiers::NONE}));
        assert!(!app.chip_tooltip_visible);
        assert_eq!(app.chip_hover_started.as_ref().unwrap().0.kind,"model");
        app.engine_picker = true;
        assert!(app.tick_chip_hover(since + Duration::from_secs(2)));
        assert!(app.chip_hover.is_none() && app.chip_hover_started.is_none());
        app.engine_picker = false;
        app.handle(Event::Mouse(MouseEvent {kind:MouseEventKind::Moved,column:engine.rect.x,row:engine.rect.y,modifiers:KeyModifiers::NONE}));
        app.handle(Event::Resize(140,30));
        assert!(app.chip_hover_started.is_none());
    }

    #[test]
    fn every_chip_hover_and_click_uses_rendered_strip_geometry() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(220, 32));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"claude",
            "model":"sonnet","cwd":"/repo","permission_mode":"auto",
            "can_set_permission_mode":true,"can_set_model":true,"lore_scrub":"ready"}));
        app.groups[0].tabs = vec!["s".into()];
        app.set_lore_memory_usage("s", 200, 1000, 80, 200);
        let pane = app.layout(app.size).body;
        let strip = app.pane_regions(0, pane)[3];
        let mut x = strip.x;
        let visible = app.chip_window(0, usize::from(strip.width));
        assert_eq!(visible.len(), app.chips(0).len());
        for (kind, label) in visible {
            let inside = x + 1;
            assert!(app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
                column: inside, row: strip.y, modifiers: KeyModifiers::NONE })));
            assert_eq!(app.chip_hover.as_ref().map(|hit| hit.kind), Some(kind));
            assert!(!painted_at(&app, 220, 32).contains(chip_hint(kind)), "tooltip waits for dwell");
            let since = app.chip_hover_started.as_ref().unwrap().1;
            assert!(app.tick_chip_hover(since + Duration::from_millis(500)));
            let rendered = painted_at(&app, 220, 32);
            assert!(rendered.contains(chip_hint(kind)), "hover hint for {kind}");
            assert!(app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
                column: inside, row: strip.y, modifiers: KeyModifiers::NONE })));
            match kind {
                "permission" => assert!(app.permission_picker.is_some()),
                "engine" => assert!(app.engine_picker),
                "model" => assert!(app.model_picker.is_some()),
                "effort" => assert!(app.model_picker.is_some() && app.effort_catalog_pending.is_some()),
                "beliefs" => assert!(app.lore_picker.is_some()),
                _ => {
                    assert_eq!(app.chip_info.as_ref().map(|info| info.kind), Some(kind));
                    if kind == "memory" { assert!(painted_at(&app, 220, 32).contains("Loading curated facts")); }
                }
            }
            app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
            x += chip_text(kind, &label).width() as u16 + 1;
        }
    }

    #[test]
    fn narrow_and_split_panes_hit_visible_chips_without_disturbing_drafts() {
        for split in [Split::Vertical, Split::Horizontal] {
        for width in [76, 100, 140] {
            let mut app = App::default();
            app.rail_visible = false;
            app.handle(Event::Resize(width, 32));
            app.apply_daemon_frame(&json!({"type":"hello","session_id":"left","engine":"codex","model":"gpt-6-sol"}));
            app.apply_daemon_frame(&json!({"type":"hello","session_id":"right","engine":"claude","model":"sonnet"}));
            app.groups[0].tabs = vec!["left".into()];
            app.groups[1].tabs = vec!["right".into()];
            app.split = split;
            app.split_requested = true;
            app.input = "left draft".into();
            app.input_cursor = app.input.len();
            app.input_drafts.insert((1, "right".into()), ("right draft\ncontinued".into(), 21));
            let panes = app.layout(app.size).panes.unwrap();
            let strip = app.pane_regions(1, panes[1])[3];
            let visible = app.chip_window(1, usize::from(strip.width));
            assert!(!visible.is_empty());
            let (kind, _) = &visible[0];
            let x = strip.x + 1;
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
                column: x, row: strip.y, modifiers: KeyModifiers::NONE }));
            assert_eq!(app.chip_hover.as_ref().map(|hit| hit.kind), Some(*kind));
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
                column: x, row: strip.y, modifiers: KeyModifiers::NONE }));
            assert_eq!(app.active_group, 1);
            assert_eq!(app.input, "right draft\ncontinued");
            assert_eq!(app.input_drafts.get(&(0, "left".into())).unwrap().0, "left draft");
        }
        }
        assert!(chip_hint("memory").contains("click to view entries"));
    }

    #[test]
    fn overflow_chip_has_hover_hint_and_cycles_clickable_items() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(60, 28));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"claude",
            "model":"sonnet","permission_mode":"auto","can_set_permission_mode":true}));
        app.groups[0].tabs = vec!["s".into()];
        let pane = app.layout(app.size).body;
        let strip = app.pane_regions(0, pane)[3];
        let visible = app.chip_window(0, usize::from(strip.width));
        assert_eq!(visible.last().map(|row| row.0), Some("more"));
        let preceding = visible[..visible.len() - 1].iter()
            .map(|(kind, label)| chip_text(kind, label).width() + 1).sum::<usize>();
        let x = strip.x + preceding as u16 + 1;
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: x, row: strip.y, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.chip_hover.as_ref().map(|hit| hit.kind), Some("more"));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: x, row: strip.y, modifiers: KeyModifiers::NONE }));
        assert_ne!(app.chip_offsets[0], 0);
        assert!(app.chip_window(0, usize::from(strip.width)).iter().any(|(kind, _)| *kind == "memory"));
    }

    #[test]
    fn chip_hits_match_painted_cells_and_exclude_prompt_separator() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(140, 32));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "engine":"claude",
            "model":"sonnet", "permission_mode":"auto", "can_set_permission_mode":true}));
        app.groups[0].tabs = vec!["s".into()];

        // A redraw can observe new dimensions before its Resize event reaches
        // the input loop. Mouse hits must follow the frame the user sees.
        let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let hits = app.rendered_chip_hits.borrow().clone().unwrap();
        let permission = hits.iter().find(|hit| hit.kind == "permission").unwrap();
        let stale_pane = app.layout(app.size).body;
        assert_ne!(permission.rect.y, app.pane_regions(0, stale_pane)[3].y);
        for hit in &hits {
            for x in hit.rect.x..hit.rect.right() {
                assert_eq!(terminal.backend().buffer()[(x, hit.rect.y)].bg, theme::HIGHLIGHT);
                assert_eq!(app.chip_hit_at(x, hit.rect.y).as_ref().map(|hit| hit.kind), Some(hit.kind));
            }
            assert!(app.chip_hit_at(hit.rect.x, hit.rect.y - 1).is_none());
            assert!(app.chip_hit_at(hit.rect.x, hit.rect.y + 1).is_none());
        }
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: permission.rect.x + 1, row: permission.rect.y + 1,
            modifiers: KeyModifiers::NONE }));
        assert!(app.chip_hover.is_none());
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: permission.rect.x + 1, row: permission.rect.y,
            modifiers: KeyModifiers::NONE }));
        assert!(app.permission_picker.is_some());
    }

    #[test]
    fn refused_explicit_link_destinations_never_fall_back_to_url_looking_labels() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 30));
        let oversized = format!("https://example.org/{}", "x".repeat(2048));
        app.apply_update(DaemonUpdate::Upsert(Session {
            id: "links".into(), title: "links".into(), collection: "repo".into(),
            transcript: format!("**Assistant:**\n\n[https://file-label.example](file:///fixture)\n\n[https://script-label.example](javascript:alert(1))\n\n[https://large-label.example]({oversized})\n\nBare https://bare.example and [https://safe-label.example](https://destination.example)."),
            status: "Idle".into(),
        }));
        app.groups[0].tabs = vec!["links".into()];
        let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let visible: Vec<String> = app.visible_links.borrow().iter().map(|(_, url)| url.clone()).collect();
        assert!(visible.contains(&"https://bare.example".to_owned()));
        assert!(visible.contains(&"https://destination.example".to_owned()));
        assert!(!visible.contains(&"https://safe-label.example".to_owned()));
        for label in ["https://file-label.example", "https://script-label.example", "https://large-label.example"] {
            let buffer = terminal.backend().buffer();
            let (row, column) = (0..30).find_map(|y| {
                let text = (0..100).map(|x| buffer[(x, y)].symbol()).collect::<String>();
                text.find(label).map(|x| (y, x as u16))
            }).expect("refused label remains painted");
            assert!(app.link_at(column, row).is_none());
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
                column, row, modifiers: KeyModifiers::NONE }));
            assert!(app.link_hover.is_none());
            app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
                column, row, modifiers: KeyModifiers::CONTROL }));
            assert!(app.pending_open_urls.is_empty());
        }
    }

    #[test]
    fn duplicate_wrapped_link_labels_hover_tooltip_and_click_exact_destination_after_folding() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(44, 32));
        let reasoning = format!("{}{}", transcript_tools::REASONING_PREFIX,
            json!({"text":"[same **bold** label](https://hidden.example)", "tokens":12, "streaming":false}));
        app.apply_update(DaemonUpdate::Upsert(Session {
            id: "links".into(), title: "links".into(), collection: "repo".into(),
            transcript: format!("**You:**\n\n[User label](https://user.example)\n\n**Assistant:**\n\n{reasoning}\n\n[same **bold** label](https://one.example/path) and [same **bold** label](https://two.example/path)"),
            status: "Idle".into(),
        }));
        app.groups[0].tabs = vec!["links".into()];
        let mut terminal = Terminal::new(TestBackend::new(44, 32)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let text = painted_at(&app, 44, 32);
        assert!(!text.contains("https://"), "destinations must not be appended to labels");
        assert!(!app.visible_links.borrow().iter().any(|(_, url)| url == "https://hidden.example"));
        for url in ["https://one.example/path", "https://two.example/path", "https://user.example"] {
            let rects: Vec<Rect> = app.visible_links.borrow().iter()
                .filter(|(_, target)| target == url).map(|(rect, _)| *rect).collect();
            assert!(!rects.is_empty());
            for rect in rects {
                app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
                    column: rect.x, row: rect.y, modifiers: KeyModifiers::NONE }));
                assert_eq!(app.link_hover.as_deref(), Some(url));
                terminal.draw(|frame| app.draw(frame)).unwrap();
                let tooltip = terminal.backend().buffer();
                let tooltip_text = (0..32).map(|y| (0..44).map(|x|
                    tooltip[(x, y)].symbol()).collect::<String>()).collect::<Vec<_>>().join("\n");
                assert!(tooltip_text.contains(url), "URL must appear in the hover tooltip");
                app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
                    column: rect.x, row: rect.y, modifiers: KeyModifiers::NONE }));
                assert!(app.pending_open_urls.is_empty());
                app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
                    column: rect.x, row: rect.y, modifiers: KeyModifiers::CONTROL }));
                assert_eq!(app.pending_open_urls.pop().as_deref(), Some(url));
            }
        }
        app.expanded_tool_sections.insert("links".into(), HashSet::from([transcript_tools::FoldKey::Section(0)]));
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert!(app.visible_links.borrow().iter().any(|(_, url)| url == "https://hidden.example"));
        let cached = app.rendered_transcripts.borrow().iter().find(|entry| entry.id == "links").unwrap().links.clone();
        assert!(cached.iter().any(|link| link.url.as_ref() == "https://hidden.example"));
    }

    #[test]
    fn streamed_link_metadata_preserves_prefix_and_replaces_only_new_tail_cells() {
        let source = "**You:**\n\n[earlier](https://earlier.example)\n\n**Assistant:**\n\nRead [same](https://one.example/a";
        let mut cached = RenderedTranscript::render(0, "s", source, 20, None, None, 0, &[]);
        assert!(cached.turn_start.is_some());
        let prefix = cached.links.clone();
        assert_eq!(prefix.len(), 1);
        let extended = format!("{source}/b) and [same](https://two.example)");
        cached.update(&extended, 20, None, None, 0, &[]);
        assert_eq!(cached.links[0], prefix[0]);
        assert!(cached.links.iter().any(|link| link.url.as_ref() == "https://one.example/a/b"));
        assert!(cached.links.iter().any(|link| link.url.as_ref() == "https://two.example"));
        let full = RenderedTranscript::render(0, "s", &extended, 20, None, None, 0, &[]);
        assert_eq!(cached.links, full.links);
        assert_eq!(cached.lines, full.lines);
    }

    #[test]
    fn image_rows_survive_streaming_and_width_changes() {
        let source = "**You:**\n\n![chart](/home/user/chart.png)\n\n**Assistant:**\n\nInitial";
        let mut cached = RenderedTranscript::render_media(
            0, "s", source, 30, None, None, 0, &[], transcript_tools::IMAGE_ROWS, 0, None);
        assert_eq!(cached.images.len(), 1);
        let initial_row = cached.images[0].row;
        let extended = format!("{source} response with [reference](https://example.com)");
        cached.update_media(&extended, 30, None, None, 0, &[], transcript_tools::IMAGE_ROWS, 0, None);
        assert_eq!(cached.images[0].row, initial_row);
        let full = RenderedTranscript::render_media(
            0, "s", &extended, 30, None, None, 0, &[], transcript_tools::IMAGE_ROWS, 0, None);
        assert_eq!(cached.lines, full.lines);
        assert_eq!(cached.links, full.links);
        assert_eq!(cached.images, full.images);
        cached.update_media(&extended, 18, None, None, 0, &[], transcript_tools::IMAGE_ROWS, 0, None);
        let resized = RenderedTranscript::render_media(
            0, "s", &extended, 18, None, None, 0, &[], transcript_tools::IMAGE_ROWS, 0, None);
        assert_eq!(cached.lines, resized.lines);
        assert_eq!(cached.links, resized.links);
        assert_eq!(cached.images, resized.images);
    }

    #[test]
    fn completed_mermaid_rebuilds_cached_lines_and_preserves_link_targets() {
        let body = "graph TD\nA-->B";
        let source = format!("**Assistant:**\n\n```mermaid\n{body}\n```\n\n[Next](https://example.com)");
        let mut cached = RenderedTranscript::render_media(
            0, "s", &source, 30, None, None, 0, &[], transcript_tools::IMAGE_ROWS, 0, None);
        assert!(cached.mermaids.is_empty());
        assert!(cached.lines.iter().any(|line| line.to_string().contains("graph TD")));
        let ready = HashSet::from([transcript_tools::mermaid_key(body)]);
        cached.update_media(&source, 30, None, None, 0, &[], transcript_tools::IMAGE_ROWS, 1, Some(&ready));
        assert_eq!(cached.mermaids.len(), 1);
        assert!(!cached.lines.iter().any(|line| line.to_string().contains("graph TD")));
        assert_eq!(cached.links[0].url.as_ref(), "https://example.com");
        assert_eq!(cached.lines[cached.links[0].row].to_string().trim(), "Next");
    }

    #[test]
    fn link_pointer_revalidates_stationary_mouse_after_geometry_changes() {
        let mut app = App::default();
        app.link_hover = Some("https://old.example".into());
        app.link_hover_position = Some((5, 6));
        app.visible_links.borrow_mut().push((Rect::new(5, 6, 4, 1), "https://old.example".into()));
        assert!(app.pointer_on_link());
        app.visible_links.borrow_mut().clear();
        assert!(!app.pointer_on_link());
    }

    #[test]
    fn transcript_links_hover_and_open_only_on_ctrl_left_click() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.apply_update(DaemonUpdate::Upsert(Session {
            id: "links".into(), title: "links".into(), collection: "repo".into(),
            transcript: "**Assistant:**\n\nRead [the guide](https://example.com/docs).".into(),
            status: "Idle".into(),
        }));
        app.groups[0].tabs = vec!["links".into()];
        let mut terminal = Terminal::new(TestBackend::new(100, 28)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let (rect, url) = app.visible_links.borrow().iter()
            .find(|(_, url)| url == "https://example.com/docs").cloned().unwrap();
        assert_eq!(url, "https://example.com/docs");
        assert!(app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: rect.x, row: rect.y, modifiers: KeyModifiers::NONE })));
        assert_eq!(app.link_hover.as_deref(), Some(url.as_str()));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: rect.x, row: rect.y, modifiers: KeyModifiers::NONE }));
        assert!(app.pending_open_urls.is_empty());
        assert!(app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: rect.x, row: rect.y, modifiers: KeyModifiers::CONTROL })));
        assert_eq!(app.pending_open_urls, vec![url]);
        assert!(app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: rect.x, row: rect.y + 1, modifiers: KeyModifiers::NONE })));
        assert!(app.link_hover.is_none());
        assert_eq!(pointer_shape(true), b"\x1b]22;pointer\x1b\\");
        assert_eq!(pointer_shape(false), b"\x1b]22;\x1b\\");
    }

    #[test]
    fn clicking_second_prompt_accepts_text_while_first_pane_waits_for_input() {
        for split in [Split::Vertical, Split::Horizontal] {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 30));
        for id in ["first", "second"] {
            app.apply_daemon_frame(&json!({"type":"hello", "session_id":id,
                "engine":"codex"}));
        }
        app.groups[0].tabs = vec!["first".into()];
        app.groups[1].tabs = vec!["second".into()];
        app.split_requested = true;
        app.split = split;
        app.input = "first draft".into();
        app.input_cursor = app.input.len();
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"first",
            "event":{"type":"needs_input", "data":{"id":"req", "kind":"ask_user",
                "questions":[{"question":"Choose", "options":[{"label":"Yes"}]}]}}}));
        assert!(app.active_request_index().is_some());
        let pane = app.layout(app.size).panes.unwrap()[1];
        let prompt = app.pane_regions(1, pane)[4];
        let point = MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: prompt.x + 2, row: prompt.y + 1, modifiers: KeyModifiers::NONE };
        assert!(app.handle(Event::Mouse(point)));
        assert_eq!(app.active_group, 1);
        assert_eq!(app.focus, Focus::Prompt);
        assert!(app.handle(Event::Key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE))));
        assert_eq!(app.input, "x");
        assert_eq!(app.input_drafts.get(&(0, "first".into())).unwrap().0, "first draft");
        assert!(app.input_requests.iter().any(|request| request.session_id == "first"));
        }
    }

    #[test]
    fn multiline_prompt_edits_at_cursor_and_enter_submits() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Resize(100, 28));
        for ch in "firstlast".chars() {
            app.handle(Event::Key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE)));
        }
        for _ in 0..4 { app.handle(Event::Key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE))); }
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('j'), KeyModifiers::CONTROL)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE)));
        assert_eq!(app.input, "first\n\nlast");
        assert!(painted(&app).contains("first"));
        assert!(painted(&app).contains("last"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_prompts, [("s".into(), "first\n\nlast".into())]);
        assert!(app.input.is_empty());
        assert_eq!(app.input_cursor, 0);
    }

    #[test]
    fn slash_autocomplete_appears_above_prompt_and_completes_without_sending() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Resize(100, 28));
        for ch in "/he".chars() {
            app.handle(Event::Key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE)));
        }
        assert_eq!(app.slash_suggestions(), vec![("/help", "Command registry")]);
        assert!(painted(&app).contains("Commands"));
        assert!(app.active_chooser_rect().is_some());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE)));
        assert_eq!(app.input, "/help");
        assert!(app.slash_suggestions().is_empty());
        assert!(app.pending_prompts.is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.chip_info.as_ref().map(|info| info.kind), Some("help"));
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn slash_autocomplete_mouse_choice_and_escape_preserve_draft() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Resize(100, 28));
        for ch in "/mo".chars() {
            app.handle(Event::Key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE)));
        }
        assert_eq!(app.slash_suggestions().len(), 3);
        painted(&app);
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 2, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.input, "/model");
        assert!(app.slash_suggestions().is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE)));
        assert_eq!(app.input, "/mode");
        assert!(!app.slash_suggestions().is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert_eq!(app.input, "/mode");
        assert!(app.slash_suggestions().is_empty());
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn bare_doxa_commands_stay_local_and_unknown_provider_commands_pass_through() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Resize(100, 28));

        app.input = "/help".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.chip_info.as_ref().map(|info| info.kind), Some("help"));
        assert!(app.input.is_empty());
        assert!(app.pending_prompts.is_empty());
        app.chip_info = None;

        app.input = "/model opus".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.input, "/model opus");
        assert!(app.requested_argument.is_some() || app.notice.contains("model"));
        assert!(app.pending_prompts.is_empty());

        app.input = "/compact".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.notice.contains("only for Claude"));
        assert!(app.pending_prompts.is_empty());

        app.session_identity.insert("s".into(), (Some("claude".into()), None));
        app.input = "/compact".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_prompts, [("s".into(), "/compact".into())]);
        app.pending_prompts.clear();

        app.input = "/compact extra".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.notice.contains("Usage: /compact"));
        assert!(app.pending_prompts.is_empty());

        app.input = "/provider-command".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_prompts, [("s".into(), "/provider-command".into())]);
    }

    #[test]
    fn successful_update_never_stops_busy_or_unsaved_sessions_to_restart() {
        let mut app = App::default(); app.apply_daemon_frame(&json!({"type":"hello", "session_id":"restart-session"}));
        app.session_activity.insert("restart-session".into(), (true, 0));
        app.restart_waiting = true; let mut state = None;
        assert!(app.restart_after_install(&mut state)); assert!(app.restart_job.is_none()); assert!(!app.should_quit);
        assert!(app.notice.contains("busy"));
        app.session_activity.insert("restart-session".into(), (false, 0)); app.restart_waiting = true;
        assert!(app.restart_after_install(&mut state)); assert!(app.notice.contains("durable"));
        assert!(app.restart_job.is_none()); assert!(!app.restart_after_update);
    }

    #[test]
    fn adopted_plugin_commands_share_completion_help_and_palette_without_execution() {
        let mut app = App::default(); app.handle(Event::Resize(100, 30));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"plugin-session", "engine":"claude"}));
        app.plugin_commands = vec![crate::operations::PluginCommand { name: "/example:check".into(), summary: "Inspect changes".into(), usage: "/example:check [path]".into(), plugin: "example".into() }];
        app.input = "/example".into(); assert_eq!(app.slash_suggestions(), vec![("/example:check", "Inspect changes")]);
        app.complete_slash(); assert_eq!(app.input, "/example:check"); assert!(app.pending_prompts.is_empty());
        app.open_help(); assert!(app.chip_info.as_ref().unwrap().lines.iter().any(|line| line.contains("/example:check [path]")));
        app.chip_info = None; app.input = "draft".into();
        let rows = actions::entries(&app, "example:check"); assert_eq!(rows.len(), 1); assert!(matches!(&rows[0].action, actions::Action::Plugin(name) if name == "/example:check"));
        assert_eq!(app.input, "draft"); assert!(app.pending_prompts.is_empty());
        app.input = "/example:check folder".into(); app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_prompts, [("plugin-session".into(), "/example:check folder".into())]);
    }

    #[test]
    fn owner_approved_native_text_command_uses_help_palette_completion_and_local_dispatch() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        let plugin_dir = dir.path().join("native-plugins");
        std::fs::create_dir(&plugin_dir).unwrap();
        std::fs::set_permissions(&plugin_dir, std::fs::Permissions::from_mode(0o700)).unwrap();
        for (path, body) in [
            (dir.path().join("config.toml"), "native_plugins = ['demo']\n"),
            (plugin_dir.join("demo.toml"), "api_version = 1\nname = 'demo'\nversion = '1.0'\n[[commands]]\nname = '/demo:status'\nsummary = 'Show owner status'\nbody = 'Deployment ready'\n"),
        ] {
            std::fs::write(&path, body).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        }
        let inventory = crate::native_plugins::load(dir.path(), &[]).unwrap();
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("s".into());
        app.native_plugin_commands = inventory.commands;
        app.input = "/demo".into();
        assert_eq!(app.slash_suggestions(), vec![("/demo:status", "Show owner status")]);
        assert!(app.complete_slash());
        assert_eq!(app.input, "/demo:status");
        app.open_help();
        assert!(app.chip_info.as_ref().unwrap().lines.iter().any(|line| line.contains("/demo:status · Show owner status")));
        app.chip_info = None;
        assert!(matches!(actions::entries(&app, "demo:status")[0].action, actions::Action::NativePlugin(_)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.chip_info.as_ref().map(|info| info.kind), Some("native_plugin"));
        assert!(painted(&app).contains("Deployment ready"));
        assert!(app.pending_prompts.is_empty());
        assert!(app.input.is_empty());
        app.chip_info = None;
        app.input = "/demo:status unexpected".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.notice.contains("no arguments"));
        assert_eq!(app.input, "/demo:status unexpected");
        assert!(app.pending_prompts.is_empty());
        app.input = "/demo:status\nforward this".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.notice.contains("single line"));
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn owner_status_chip_opens_local_refresh_ledger_without_provider_dispatch() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        std::fs::set_permissions(dir.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
        let plugin_dir = dir.path().join("native-plugins");
        std::fs::create_dir(&plugin_dir).unwrap();
        std::fs::set_permissions(&plugin_dir, std::fs::Permissions::from_mode(0o700)).unwrap();
        for (path, body) in [
            (dir.path().join("config.toml"), "native_plugins = ['demo']\n"),
            (plugin_dir.join("demo.toml"), "api_version = 1\nname = 'demo'\nversion = '1.0'\n[status]\nproducer = 'owner-file-v1'\nlabel = 'Queue'\nrefresh_seconds = 5\n"),
        ] {
            std::fs::write(&path, body).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        }
        let inventory = crate::native_plugins::load(dir.path(), &[]).unwrap();
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("s".into());
        app.native_status = crate::native_plugins::StatusRuntime::new(inventory.statuses);
        assert!(app.chips(0).iter().any(|(kind, label)| *kind == "native_status" && label == "Queue: ?"));
        app.open_chip_info("native_status", 0);
        assert_eq!(app.chip_info.as_ref().map(|info| info.kind), Some("native_status"));
        assert!(app.chip_info.as_ref().unwrap().lines.iter().any(|line| line.contains("failure ledger") || line.contains("ledger:")));
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn shell_is_keyboard_only_and_never_provider_or_command_dispatch() {
        let root = tempfile::tempdir().unwrap(); let proof = root.path().join("proof");
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"shell-session", "cwd":root.path()}));
        app.groups[0].tabs = vec!["shell-session".into()]; app.focus = Focus::Prompt;
        let command = format!("!printf keyboard > '{}'", proof.display());
        app.input = command.clone(); assert!(!app.submit_local_command()); assert!(!proof.exists());
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"shell-session", "event":{"type":"text_delta", "text":command}}));
        assert!(!proof.exists()); assert!(app.local_shell_jobs.is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.pending_prompts.is_empty()); assert_eq!(app.local_shell_jobs.len(), 1);
        let deadline = Instant::now() + Duration::from_secs(2);
        while !app.local_shell_jobs.is_empty() && Instant::now() < deadline { app.poll_shell(); std::thread::sleep(Duration::from_millis(5)); }
        assert!(app.local_shell_jobs.is_empty()); assert_eq!(std::fs::read_to_string(proof).unwrap(), "keyboard");
        assert!(app.sessions[0].transcript.contains("DOXA_LOCAL_SHELL:"));
        assert!(!COMMANDS.iter().any(|row| row.name == "/shell" || row.name == "!"));
    }

    #[test]
    fn about_renders_cached_installation_and_advisory_without_starting_work() {
        let mut app = App::default(); app.handle(Event::Resize(100, 30));
        app.apply_installation(crate::installation::Snapshot { rows:vec!["Installed commit · measured".into()], update:crate::installation::Update::Unknown });
        app.open_about();
        let info = app.chip_info.as_ref().unwrap();
        assert!(info.lines.iter().any(|line| line == "Installed commit · measured"));
        assert!(info.lines.iter().any(|line| line == "Update · check unavailable"));
        assert!(!app.update_notified); assert!(app.pending_launches.is_empty()); assert!(app.pending_prompts.is_empty());
        app.installation.update = crate::installation::Update::Skipped; app.open_about();
        assert!(app.chip_info.as_ref().unwrap().lines.iter().any(|line| line == "Update · check skipped"));
    }

    #[test]
    fn about_uses_only_selected_session_account_and_clears_missing_identity() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 30));
        for (id, email) in [("first", "first@example.test"), ("second", "second@example.test")] {
            app.apply_daemon_frame(&json!({"type":"hello","session_id":id,"engine":"claude",
                "model":"sonnet","cwd":"/project","account":{"email":email,
                    "organization":"SDK org","accessToken":"secret"}}));
        }
        app.groups[0].active = app.groups[0].tabs.iter().position(|id| id == "first").unwrap();
        app.input = "draft unchanged".into();
        app.open_about();
        let info = app.chip_info.as_ref().unwrap();
        assert!(info.lines.iter().any(|line| line.contains("first@example.test")));
        assert!(info.lines.iter().any(|line| line.contains("SDK org")));
        assert!(!info.lines.iter().any(|line| line.contains("second@example.test") || line.contains("secret")));
        assert_eq!(app.input, "draft unchanged");
        assert!(app.pending_prompts.is_empty());
        app.apply_daemon_frame(&json!({"type":"telemetry_status","session_id":"first",
            "status":{"account":null}}));
        app.open_about();
        assert!(app.chip_info.as_ref().unwrap().lines.iter().any(|line| line.contains("unavailable")));
        assert!(!app.chip_info.as_ref().unwrap().lines.iter().any(|line| line.contains("first@example.test")));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::NONE)));
        assert!(app.chip_info.is_none());
        assert_eq!(app.input, "draft unchanged");
    }

    #[test]
    fn help_lists_full_python_registry_with_rust_capabilities() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Resize(100, 30));
        assert_eq!(COMMANDS.len(), 49);
        let mut names = std::collections::HashSet::new();
        for row in COMMANDS { assert!(names.insert(row.name)); }
        app.open_help();
        let info = app.chip_info.as_ref().unwrap();
        assert_eq!(info.kind, "help");
        for form in ["/collection [action] [name]", "/codegraph [stored] file|symbol|imports|calls|modules VALUE", "/usage", "/context", "/compact",
            "/fleet [runs|status [RUN]|stop|detach|attach [RUN] INDEX|dependency-review [RUN] SLOT|mesh [RUN]|start OPTIONS|resume RUN]", "/help"] {
            assert!(info.lines.iter().any(|line| line.starts_with(form)), "missing {form}");
        }
        assert!(info.lines.iter().any(|line| line.contains("unavailable in Rust")));
        assert!(info.lines.iter().any(|line| line.contains("Claude only")));
        let menu = app.active_chooser_rect().unwrap();
        assert!(menu.bottom() < app.layout(app.size).body.bottom());
        app.handle(Event::Key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)));
        assert!(app.chip_info.as_ref().unwrap().scroll > 0);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(app.chip_info.is_none());
    }

    #[test]
    fn remote_slash_commands_are_local_and_keep_invalid_drafts() {
        let mut app = App::default();
        app.groups[0].tabs.push("local-session".into());

        app.input = "/remote-connect http://public.example.org workstation".into();
        assert!(app.submit_local_command());
        assert!(app.remote_connect_request.is_none());
        assert!(app.input.starts_with("/remote-connect"));

        app.input = "/remote-connect https://owner.tail.ts.net workstation".into();
        assert!(app.submit_local_command());
        assert_eq!(app.remote_connect_request.as_ref().unwrap().1, "workstation");
        assert!(app.input.is_empty());
        assert!(app.pending_prompts.is_empty());

        app.input = "/remote-disconnect".into();
        assert!(app.submit_local_command());
        assert!(app.remote_disconnect_requested);
        assert!(app.input.is_empty());

        app.input = "/remote-control https://owner.tail.ts.net".into();
        assert!(app.submit_local_command());
        assert!(matches!(app.remote_handoff, Some(RemoteHandoff::Hub(_))));
        assert!(app.should_quit);
        assert!(app.pending_prompts.is_empty());

        let mut remote = App { remote_mode: true, ..Default::default() };
        remote.input = "/remote-connect https://owner.tail.ts.net workstation".into();
        assert!(remote.submit_local_command());
        assert!(remote.remote_connect_request.is_none());
        remote.input = "/local".into();
        assert!(remote.submit_local_command());
        assert!(matches!(remote.remote_handoff, Some(RemoteHandoff::Local)));
        assert!(remote.should_quit);
    }

    #[test]
    fn known_doxa_commands_never_escape_as_provider_prompts() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Resize(100, 30));
        for command in ["/doctor", "/clear", "/update", "/plugins",
            "/help\nignore", "/msg\t"] {
            app.input = command.into();
            app.input_cursor = app.input.len();
            app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
            assert!(app.pending_prompts.is_empty(), "forwarded {command}");
            app.chip_info = None;
            app.operations_menu = None;
        }
        app.input = "/provider-command".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_prompts, [("s".into(), "/provider-command".into())]);
    }

    #[test]
    fn usage_and_context_open_inline_with_only_measured_session_values() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 32));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"first-session",
            "engine":"claude","model":"sonnet","ctx_percentage":25.0,
            "ctx_tokens":250,"ctx_max_tokens":1000,
            "total_cost_usd":0.25,
            "usage":{"num_turns":2,"input_tokens":100,"output_tokens":20,
                "cache_read_input_tokens":7,"cache_creation_input_tokens":3}}));
        app.groups[0].tabs = vec!["first-session".into(), "second-session".into()];
        app.groups[0].active = 0;
        app.input = "/usage".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        let info = app.chip_info.as_ref().unwrap();
        assert_eq!(info.kind, "usage");
        assert!(info.lines.iter().any(|line| line.contains("tokens in") && line.contains("100")));
        assert!(info.lines.iter().any(|line| line.contains("cost") && line.contains("$0.2500")));
        let menu = app.active_chooser_rect().unwrap();
        assert!(menu.bottom() < app.layout(app.size).body.bottom());
        assert!(app.input.is_empty());
        assert!(app.pending_prompts.is_empty());

        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        app.groups[0].active = 1;
        app.input = "/context".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        let info = app.chip_info.as_ref().unwrap();
        assert_eq!(info.kind, "context");
        assert!(info.lines.iter().any(|line| line.contains("not reported by this engine")));
        assert!(!info.lines.iter().any(|line| line.contains("250") || line.contains("25.0%")));

        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        app.groups[0].active = 0;
        app.open_chip_info("context", 0);
        let info = app.chip_info.as_ref().unwrap();
        assert!(info.lines.iter().any(|line| line.contains("250 / 1000 tokens")));
        assert!(info.lines.iter().any(|line| line.contains("25.0%")));
    }

    #[test]
    fn usage_does_not_promote_turn_cost_or_partial_tokens_to_session_totals() {
        let mut telemetry = SessionTelemetry::default();
        telemetry.update_turn(&json!({"usage_scope":"turn", "num_turns":1,
            "input_tokens":5,"output_tokens":2,"cost_usd":0.01,
            "ctx_percentage":null,"ctx_tokens":null,"ctx_max_tokens":null}));
        assert_eq!(telemetry.turns, None);
        assert_eq!(telemetry.input_tokens, None);
        assert_eq!(telemetry.session_cost, None);
        assert_eq!(telemetry.context, None);
        telemetry.update_status(&json!({"usage":{"num_turns":0,"input_tokens":0},
            "total_cost_usd":null}));
        assert_eq!(telemetry.turns, Some(0));
        assert_eq!(telemetry.input_tokens, Some(0));
        assert_eq!(telemetry.output_tokens, None);
        assert_eq!(telemetry.session_cost, None);
    }

    #[test]
    fn bare_layout_commands_change_the_real_pane_state() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("s".into());
        assert!(app.layout(app.size).panes.is_none());

        app.input = "/vsplit".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.split, Split::Vertical);
        assert!(app.layout(app.size).panes.is_some());
        assert!(app.pending_prompts.is_empty());

        app.input = "/pane".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.active_group, 0);
        assert!(app.notice.contains("2 pane groups"));
        assert!(app.layout(app.size).panes.is_some());

        app.input = "/pane 2".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.active_group, 1);
        app.input = "/pane 1".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.active_group, 0);

        app.input = "/detach".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.groups[0].tabs.is_empty());
        assert!(!app.should_quit);
    }

    #[test]
    fn detach_hides_closed_tab_from_rail_but_keeps_resume_target() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        for id in ["first", "second"] {
            app.apply_update(DaemonUpdate::Upsert(Session {
                id: id.into(), title: id.into(), collection: String::new(),
                transcript: String::new(), status: "Ready".into(),
            }));
        }
        app.groups[0].tabs.push("second".into());
        app.groups[0].active = 1;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('w'), KeyModifiers::CONTROL)));
        assert_eq!(app.groups[0].tabs, ["first"]);
        assert!(!app.should_quit);
        assert!(app.sessions.iter().any(|session| session.id == "second"));
        assert!(!app.rail_order().iter().any(|index| app.sessions[*index].id == "second"));
        app.apply_update(DaemonUpdate::Upsert(Session {
            id: "second".into(), title: "second".into(), collection: String::new(),
            transcript: "still running".into(), status: "Ready".into(),
        }));
        assert!(!app.rail_order().iter().any(|index| app.sessions[*index].id == "second"));
        app.history_resume = true;
        app.history_query = "second".into();
        assert_eq!(app.history_matches().iter().map(|index| app.sessions[*index].id.as_str()).collect::<Vec<_>>(), ["second"]);
        app.attach_selected("second");
        assert_eq!(app.pending_attaches, [("second".into(), 0)]);
    }

    #[test]
    fn navigation_keys_keep_pane_drafts_and_quit_detaches_daemons() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        for id in ["first", "second", "other"] {
            app.apply_update(DaemonUpdate::Upsert(Session {
                id: id.into(), title: id.into(), collection: String::new(),
                transcript: String::new(), status: "Ready".into(),
            }));
        }
        app.groups[0].tabs = vec!["first".into(), "second".into()];
        app.groups[0].active = 0;
        app.groups[1].tabs = vec!["other".into()];
        app.split_requested = true;
        app.focus = Focus::Prompt;
        app.input = "first draft".into();
        app.input_cursor = app.input.len();

        app.handle(Event::Key(KeyEvent::new(KeyCode::Right, KeyModifiers::CONTROL)));
        assert_eq!(app.groups[0].active_id(), Some("second"));
        assert!(app.input.is_empty());
        app.input = "second draft".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Right, KeyModifiers::SHIFT)));
        assert_eq!(app.active_group, 1);
        assert_eq!(app.focus, Focus::Prompt);
        app.input = "other draft".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Left, KeyModifiers::SHIFT)));
        assert_eq!(app.active_group, 0);
        assert_eq!(app.input, "second draft");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Left, KeyModifiers::CONTROL)));
        assert_eq!(app.groups[0].active_id(), Some("first"));
        assert_eq!(app.input, "first draft");

        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('w'), KeyModifiers::CONTROL)));
        assert_eq!(app.groups[0].tabs, ["second"]);
        assert!(!app.should_quit);
        assert!(app.detached_this_run.contains(&"first".to_owned()));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::CONTROL)));
        assert!(app.should_quit);
        assert!(app.killed_this_run.is_empty());
        assert_eq!(app.groups[0].tabs, ["second"]);
        assert_eq!(app.groups[1].tabs, ["other"]);
    }

    #[test]
    fn ctrl_w_last_tab_leaves_an_empty_window_for_new_sessions() {
        let mut app = App::default();
        app.apply_update(DaemonUpdate::Upsert(Session {
            id: "only".into(), title: "only".into(), collection: String::new(),
            transcript: String::new(), status: "Ready".into(),
        }));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('w'), KeyModifiers::CONTROL)));
        assert!(app.groups.iter().all(|group| group.tabs.is_empty()));
        assert_eq!(app.active_group, 0);
        assert!(!app.should_quit);
        assert_eq!(app.detached_this_run, ["only"]);
        assert!(app.rail_order().is_empty());
    }

    #[test]
    fn new_tab_and_tool_shortcuts_can_be_reassigned_without_rebuild() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        let key = |ch, modifiers| Event::Key(KeyEvent::new(KeyCode::Char(ch), modifiers));
        app.handle(key('t', KeyModifiers::CONTROL));
        assert!(app.engine_picker);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(!app.engine_picker);
        app.handle(key('t', KeyModifiers::ALT));
        assert!(app.tool_modal);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(!app.tool_modal);

        let mut config = toml::Table::new();
        config.insert("key_new_tab".into(), toml::Value::String("Alt+N".into()));
        app.keybindings = crate::keybindings::Bindings::from_config(&config).unwrap();
        app.handle(key('t', KeyModifiers::CONTROL));
        assert!(!app.engine_picker);
        app.handle(key('n', KeyModifiers::ALT));
        assert!(app.engine_picker);
    }

    #[test]
    fn only_verified_dead_detached_sessions_leave_the_rail() {
        let mut app = App::default();
        for id in ["dead", "live", "open"] {
            app.apply_update(DaemonUpdate::Upsert(Session {
                id: id.into(), title: id.into(), collection: String::new(),
                transcript: "saved transcript".into(), status: "Disconnected".into(),
            }));
        }
        app.groups[0].tabs = vec!["open".into()];
        let live = std::collections::HashSet::from(["live".to_owned()]);
        assert!(app.mark_dead_detached(&live));
        assert!(app.offline_ids.contains("dead"));
        assert!(!app.offline_ids.contains("live"));
        assert!(!app.offline_ids.contains("open"));
        let visible: Vec<_> = app.rail_order().iter().map(|index| app.sessions[*index].id.as_str()).collect();
        assert_eq!(visible, ["live", "open"]);
        assert!(app.sessions.iter().any(|session| session.id == "dead" && session.transcript == "saved transcript"));
    }

    #[test]
    fn detached_connected_session_stays_hidden_when_daemon_disappears() {
        let mut app=App::default();
        app.apply_update(DaemonUpdate::Upsert(Session {id:"old".into(),title:"Old label".into(),
            collection:"repo".into(),transcript:"saved transcript".into(),status:"Connected".into()}));
        app.groups[0].tabs.clear();
        app.detached_this_run.push("old".into());
        assert!(app.mark_dead_detached(&std::collections::HashSet::new()));
        assert!(app.offline_ids.contains("old"));
        assert!(app.rail_order().is_empty());
        assert_eq!(app.sessions[0].title,"Old label");
        assert_eq!(app.sessions[0].transcript,"saved transcript");
    }

    #[test]
    fn ctrl_w_dismisses_a_past_session_from_the_rail() {
        let mut app = App::default();
        app.apply_update(DaemonUpdate::Upsert(Session {
            id: "old".into(), title: "Old work".into(), collection: "repo".into(),
            transcript: "saved transcript".into(), status: "Disconnected".into(),
        }));
        app.offline_ids.insert("old".into());
        app.detached_this_run.push("old".into());
        app.groups[0].tabs = vec!["old".into()];
        assert_eq!(app.rail_order(), [0]);

        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('w'), KeyModifiers::CONTROL)));

        assert!(app.groups[0].tabs.is_empty());
        assert!(app.rail_order().is_empty());
        assert!(!app.detached_this_run.contains(&"old".to_owned()));
        assert_eq!(app.sessions[0].transcript, "saved transcript");
        assert!(app.notice.contains("Past session closed"));
    }

    #[test]
    fn hovering_a_rail_session_highlights_only_that_row() {
        let mut app = App::default();
        app.handle(Event::Resize(80, 20));
        for id in ["one", "two"] {
            app.apply_update(DaemonUpdate::Upsert(Session {
                id: id.into(), title: id.into(), collection: "repo".into(),
                transcript: String::new(), status: "Ready".into(),
            }));
        }
        let rail = app.layout(app.size).rail.unwrap();
        let rows = app.rail_rows();
        let target = rows.iter().position(|row| matches!(row, RailRow::Session(1))).unwrap();
        let y = rail.y + 1 + (target - app.rail_view_start(rail, &rows)) as u16;
        let focus = app.focus;
        let active = app.groups[app.active_group].active_id().map(str::to_owned);

        assert!(app.handle(Event::Mouse(MouseEvent {
            kind: MouseEventKind::Moved, column: rail.x + 3, row: y,
            modifiers: KeyModifiers::NONE,
        })));
        assert_eq!(app.rail_hover.as_deref(), Some("two"));
        assert_eq!(app.focus, focus);
        assert_eq!(app.groups[app.active_group].active_id(), active.as_deref());
        let mut terminal = Terminal::new(TestBackend::new(80, 20)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert_eq!(terminal.backend().buffer()[(rail.x + 3, y)].bg, theme::RAISED);
        assert_eq!(terminal.backend().buffer()[(rail.x + 3, y - 1)].bg, theme::RAIL);

        assert!(app.handle(Event::Mouse(MouseEvent {
            kind: MouseEventKind::Moved, column: rail.right(), row: y,
            modifiers: KeyModifiers::NONE,
        })));
        assert!(app.rail_hover.is_none());
    }

    #[test]
    fn detaching_last_tab_in_first_pane_preserves_other_pane_draft() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("first".into());
        app.groups[1].tabs.push("second".into());
        app.input_drafts.insert((1, "second".into()), ("other draft".into(), 11));
        app.input = "/detach".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(), Some("second"));
        assert!(app.groups[1].tabs.is_empty());
        assert_eq!(app.input, "other draft");
        assert!(!app.should_quit);
        assert!(!app.split_requested);
    }

    #[test]
    fn pane_sidebar_and_directory_command_forms_stay_local() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.groups[0].tabs.push("first".into());
        app.session_cwds.insert("first".into(), PathBuf::from("/repo/project"));

        app.input = "/pane".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.active_group, 0);
        assert!(app.layout(app.size).panes.is_none());
        assert!(app.notice.contains("One pane group"));
        app.input = "/pane 2".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.input, "/pane 2");
        assert_eq!(app.active_group, 0);
        assert!(app.layout(app.size).panes.is_none());
        assert!(app.notice.contains("Choose pane"));
        app.input = "/vsplit".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));

        for (command, target) in [("/pane 2", 1), ("/pane 1", 0)] {
            app.input = command.into();
            app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
            assert_eq!(app.active_group, target);
        }
        app.input = "/sidebar width 32".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.rail_width, 32);
        assert!(app.rail_visible);
        app.input = "/sidebar off".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(!app.rail_visible);
        app.input = "/dir".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.notice.contains("/repo/project"));
        assert!(app.pending_prompts.is_empty());

        app.input = "/pane 3".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.input, "/pane 3");
        assert!(app.notice.contains("Choose pane"));
    }

    #[test]
    fn bracketed_paste_preserves_lines_sanitizes_controls_and_caps_bytes() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.handle(Event::Paste("one\r\ntwo\rthree\u{1b}[31m\u{7}é".into()));
        assert_eq!(app.input, "one\ntwo\nthree[31mé");
        assert!(app.pending_prompts.is_empty());
        app.handle(Event::Paste("x".repeat(MAX_INPUT_BYTES).into()));
        assert_eq!(app.input.len(), MAX_INPUT_BYTES);
        assert!(app.notice.contains("truncated"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.pending_prompts[0].1.len(), MAX_INPUT_BYTES);
    }

    #[test]
    fn pane_drafts_keep_multiline_cursor_and_modal_ignores_paste() {
        let mut app = App::default();
        app.groups[0].tabs.push("a".into());
        app.groups[1].tabs.push("b".into());
        app.handle(Event::Paste("a\nb".into()));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::ALT)));
        app.handle(Event::Paste("other".into()));
        app.action_menu = true;
        assert!(!app.handle(Event::Paste("ignored".into())));
        app.action_menu = false;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::ALT)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('!'), KeyModifiers::NONE)));
        assert_eq!(app.input, "a\n!b");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::ALT)));
        assert_eq!(app.input, "other");
    }

    #[test]
    fn empty_second_group_uses_one_pane_until_split_is_requested() {
        let mut app = App::default();
        app.handle(Event::Resize(118, 31));
        app.groups[0].tabs.push("first".into());
        assert!(app.layout(app.size).panes.is_none());

        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('v'), KeyModifiers::ALT)));
        assert!(app.layout(app.size).panes.is_some());

        let mut app = App::default();
        app.handle(Event::Resize(118, 31));
        app.groups[0].tabs.push("first".into());
        app.groups[1].tabs.push("second".into());
        assert!(app.layout(app.size).panes.is_some());

        app.groups[1].tabs.clear();
        assert!(app.layout(app.size).panes.is_none());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::ALT)));
        assert_eq!(app.active_group, 1);
        assert!(app.layout(app.size).panes.is_some());

        let mut app = App::default();
        app.handle(Event::Resize(118, 31));
        assert!(app.layout(app.size).panes.is_none());
        app.handle(Event::Key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE)));
        assert!(app.diff_pane);
        assert!(app.layout(app.size).panes.is_some());
        app.handle(Event::Key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE)));
        assert!(!app.diff_pane);
        assert!(app.layout(app.size).panes.is_none());
    }

    #[test]
    fn history_picker_filters_attached_transcripts_and_opens_selected_tab() {
        let mut app = App::default();
        app.apply_update(DaemonUpdate::Upsert(Session { id: "alpha".into(), title: "First".into(),
            collection: "repo".into(), transcript: "red apple".into(), status: "Ready".into() }));
        app.apply_update(DaemonUpdate::Upsert(Session { id: "beta".into(), title: "Second".into(),
            collection: "repo".into(), transcript: "green pear".into(), status: "Ready".into() }));
        app.handle(Event::Resize(100, 28));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)));
        for c in "pear".chars() { app.handle(Event::Key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE))); }
        let view = painted(&app);
        assert!(view.contains("Session history"));
        assert!(view.contains("Second"));
        assert!(!view.contains("First · alpha"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(), Some("beta"));
        assert!(!app.history_modal);
    }

    #[test]
    fn history_search_stays_above_active_prompt_and_mouse_opens_result() {
        let mut app = App::default();
        app.rail_visible = false;
        app.apply_update(DaemonUpdate::Upsert(Session { id: "alpha".into(), title: "First".into(),
            collection: "repo".into(), transcript: "red apple".into(), status: "Ready".into() }));
        app.apply_update(DaemonUpdate::Upsert(Session { id: "beta".into(), title: "Second".into(),
            collection: "repo".into(), transcript: "green pear".into(), status: "Ready".into() }));
        app.groups[0].tabs.push("alpha".into());
        app.groups[1].tabs.push("beta".into());
        app.handle(Event::Resize(100, 28));
        let panes = app.layout(app.size).panes.unwrap();
        let mut before = Terminal::new(TestBackend::new(100, 28)).unwrap();
        before.draw(|frame| app.draw(frame)).unwrap();

        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)));
        let menu = app.active_chooser_rect().unwrap();
        assert_eq!(menu.x, panes[0].x);
        assert_eq!(menu.width, panes[0].width);
        assert!(menu.bottom() < panes[0].bottom());
        let mut after = Terminal::new(TestBackend::new(100, 28)).unwrap();
        after.draw(|frame| app.draw(frame)).unwrap();
        for y in panes[1].y..panes[1].bottom() {
            for x in panes[1].x..panes[1].right() {
                assert_eq!(before.backend().buffer()[(x, y)], after.backend().buffer()[(x, y)]);
            }
        }
        assert_eq!(after.backend().buffer()[(menu.x + 2, menu.y + 2)].bg, theme::HIGHLIGHT);

        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 3, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.groups[0].active_id(), Some("beta"));
        assert!(!app.history_modal);
    }

    #[test]
    fn history_search_closes_outside_panel_and_on_narrow_pane_resize() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 28));
        app.open_history();
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.right(), row: menu.y + 2, modifiers: KeyModifiers::NONE }));
        assert!(!app.history_modal);
        app.open_history();
        app.handle(Event::Resize(100, 18));
        assert!(app.history_modal);
        app.handle(Event::Resize(100, 10));
        assert!(!app.history_modal);
    }

    #[test]
    fn restored_and_appended_transcripts_keep_only_bounded_utf8_tail() {
        let mut app = App::default();
        let long = format!("{}éEND", "a".repeat(MAX_TRANSCRIPT_BYTES));
        app.apply_update(DaemonUpdate::Upsert(Session { id: "s".into(), title: "S".into(),
            collection: "repo".into(), transcript: long.clone(), status: "Ready".into() }));
        assert!(app.sessions[0].transcript.len() <= MAX_TRANSCRIPT_BYTES);
        assert!(app.sessions[0].transcript.ends_with("éEND"));
        app.apply_update(DaemonUpdate::Transcript { id: "s".into(), markdown: long.clone() });
        assert!(app.sessions[0].transcript.len() <= MAX_TRANSCRIPT_BYTES);
        let session = &mut app.sessions[0];
        assert!(append_transcript(session, &long));
        assert!(session.transcript.len() <= MAX_TRANSCRIPT_BYTES);
        assert!(session.transcript.ends_with("éEND"));
    }

    fn saturated_stream_session() -> Session {
        let turn = "**You:**\n\nEarlier question\n\n**Assistant:**\n\nEarlier **answer**.\n\n";
        let current = "**You:**\n\nCurrent question\n\n**Assistant:**\n\nTool: Read started · file.rs\n\nCurrent answer";
        let mut transcript = turn.repeat(6000);
        transcript.push_str(&"é".repeat((MAX_TRANSCRIPT_BYTES - transcript.len() - current.len() - 2) / 2));
        transcript.push_str("\n\n");
        transcript.push_str(current);
        // Fill the single odd byte, if needed, before the current heading.
        if transcript.len() < MAX_TRANSCRIPT_BYTES { transcript.insert(transcript.len() - current.len() - 2, 'a'); }
        Session { id: "cap".into(), title: "Cap".into(), collection: "repo".into(), transcript, status: "Working".into() }
    }

    #[test]
    fn saturated_tail_evicts_old_turns_once_and_preserves_current_turn() {
        let mut session = saturated_stream_session();
        assert_eq!(session.transcript.len(), MAX_TRANSCRIPT_BYTES);
        let current = session.transcript[streamed_turn_start(&session.transcript).unwrap()..].to_owned();
        assert!(append_transcript(&mut session, " delta"));
        assert!(session.transcript.starts_with("**You:**"));
        assert!(session.transcript.ends_with(&format!("{current} delta")));
        assert!(session.transcript.len() < MAX_TRANSCRIPT_BYTES - 32 * 1024);
        let mut cached = RenderedTranscript::render(0, "cap", &session.transcript, 80, None, None, 0, &[]);
        for _ in 0..5 {
            let previous = session.transcript.clone();
            assert!(!append_transcript(&mut session, " é"));
            assert!(session.transcript.starts_with(&previous));
            cached.update(&session.transcript, 80, None, None, 0, &[]);
            let (lines, sections, links) = transcript_tools::render_with_links(&session.transcript, 80, None, None, &[]);
            assert_eq!(cached.lines, lines);
            assert_eq!(cached.sections, sections);
            assert_eq!(cached.links, links);
        }
        // A single giant current turn still obeys the cap and keeps its tail.
        session.transcript = format!("**You:**\n\n{}", "é".repeat(MAX_TRANSCRIPT_BYTES / 2 - 5));
        append_transcript(&mut session, "ENDING");
        assert!(session.transcript.len() <= MAX_TRANSCRIPT_BYTES);
        assert!(session.transcript.ends_with("ENDING"));
    }

    #[test]
    #[ignore = "manual deterministic saturated display throughput measurement"]
    fn saturated_stream_render_throughput() {
        let mut session = saturated_stream_session();
        let mut legacy = session.transcript.clone();
        let mut cached = RenderedTranscript::render(0, "cap", &session.transcript, 80, None, None, 0, &[]);
        let start = Instant::now();
        let mut evictions = 0;
        for _ in 0..200 {
            evictions += usize::from(append_transcript(&mut session, " streamed"));
            cached.update(&session.transcript, 80, None, None, 0, &[]);
        }
        let incremental = start.elapsed();
        let start = Instant::now();
        for _ in 0..200 {
            legacy.drain(.." streamed".len());
            legacy.push_str(" streamed");
            let _ = RenderedTranscript::render(0, "cap", &legacy, 80, None, None, 0, &[]);
        }
        eprintln!("200 saturated assistant delta paints: batched={incremental:?}, byte-trim={:?}, evictions={evictions}, retained={} bytes", start.elapsed(), session.transcript.len());
        assert_eq!(evictions, 1);
    }

    #[test]
    fn input_requests_have_a_visible_capacity_limit() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "engine":"claude"}));
        for index in 0..=MAX_INPUT_REQUESTS {
            app.apply_daemon_frame(&json!({"type":"event", "session_id":"s",
                "event":{"type":"needs_input", "data":{"id":format!("request-{index}"),
                    "kind":"permission", "title":"Approve?"}}}));
        }
        assert_eq!(app.input_requests.len(), MAX_INPUT_REQUESTS);
        assert!(app.notice.contains("Too many input requests"));
    }

    #[test]
    fn engine_picker_header_click_does_not_select_an_engine() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 80, 24);
        app.open_engine_picker();
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 1, modifiers: KeyModifiers::NONE }));
        assert!(app.engine_picker);
        assert!(!app.notice.contains("doxa-rs new"));
    }

    #[test]
    fn choosers_reserve_space_above_active_prompt_without_covering_other_pane() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.rail_visible = false;
        app.groups[0].tabs.push("a".into());
        app.groups[1].tabs.push("b".into());
        let before = painted(&app);
        app.open_engine_picker();
        let menu = app.active_chooser_rect().unwrap();
        let pane = app.layout(app.size).panes.unwrap()[0];
        assert_eq!(menu.x, pane.x);
        assert_eq!(menu.width, pane.width);
        assert!(menu.y > pane.y + 2);
        let after = painted(&app);
        let lines: Vec<_> = after.lines().collect();
        assert!(lines[usize::from(menu.y)].contains("New session"));
        assert!(lines[usize::from(menu.bottom())].contains("Engine"));
        assert!(lines[usize::from(menu.bottom() + 1)].contains("Prompt"));
        let old_lines: Vec<_> = before.lines().collect();
        for y in pane.y..pane.bottom() {
            assert_eq!(lines[usize::from(y)].chars().skip(50).collect::<String>(),
                old_lines[usize::from(y)].chars().skip(50).collect::<String>());
        }
        app.engine_picker = false;
        app.action_menu = true;
        assert_eq!(app.active_chooser_rect().unwrap().bottom(), menu.bottom());
        app.action_menu = false;
        app.lore_picker = Some(LorePicker { session_id: None, rows: vec![], selected: 0, query: String::new(), filter_focused: false,
            offset: 0, status: "Ready".into(), evidence: None, pending: None,
            proposals: Vec::new(), proposal_mode: false, cluster_mode: false, all_proposals: Vec::new(), review: None, review_scroll: 0,
            review_seen: 0, review_width: 0, armed_resolution: None,
            can_resolve: false, resolving: false, cwd: String::new(),
            belief_review: None, belief_intent: None, can_act_on_beliefs: false, belief_action: None,
            belief_note: String::new(), retract_armed: false, belief_acting: false,
            result_status: None });
        let lore = app.active_chooser_rect().unwrap();
        assert_eq!(lore.bottom(), menu.bottom());
        assert!(lore.height < menu.height, "empty LORE list should stay compact");
    }

    #[test]
    fn ask_user_choices_expand_above_prompt_and_beliefs_chip_uses_clicked_pane() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.rail_visible = false;
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"a", "engine":"codex"}));
        app.groups[0].tabs = vec!["a".into()];
        app.groups[1].tabs = vec!["b".into()];
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"a",
            "event":{"type":"needs_input", "data":{"id":"req-1", "kind":"ask_user",
                "title":"Choose target", "questions":[{"header":"Environment", "question":"Where?", "options":[
                    {"label":"Staging", "description":"Validate first"},
                    {"label":"Production", "description":"Release now"}]}]}}}));
        assert!(app.active_request_index().is_some());
        let menu = app.active_chooser_rect().unwrap();
        let screen = painted(&app);
        let rows: Vec<_> = screen.lines().collect();
        assert!(rows[usize::from(menu.y)].contains("Where?"));
        assert!(rows[usize::from(menu.y + 1)].contains("Environment"));
        assert!(!screen.contains("Header:"));
        assert!(!screen.contains("Question:"));
        assert!(!screen.contains("PgUp/PgDn scroll"));
        assert!(rows[usize::from(menu.y + 2)].contains("Staging"));
        assert!(rows[usize::from(menu.bottom() + 1)].contains("Prompt"));
        assert!(menu.height <= 10, "short question should use only its content rows");
        let mut terminal = Terminal::new(TestBackend::new(100, 28)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        assert_eq!(terminal.backend().buffer()[(menu.x + 2, menu.y + 2)].bg, theme::HIGHLIGHT);
        assert_eq!(terminal.backend().buffer()[(menu.right() - 3, menu.y + 2)].bg, theme::HIGHLIGHT);
        app.input_requests.clear();

        let pane = app.layout(app.size).panes.unwrap()[1];
        app.chip_offsets[1] = app.chips(1).iter().position(|(kind, _)| *kind == "beliefs").unwrap();
        assert!(app.chip_window(1, usize::from(pane.width)).iter().any(|(kind, _)| *kind == "beliefs"));
        let mut belief_x = pane.x;
        for (kind, label) in app.chip_window(1, usize::from(pane.width)) {
            if kind == "beliefs" { break; }
            belief_x += chip_text(kind, &label).width() as u16 + 1;
        }
        let chip_y = pane.bottom().saturating_sub(prompt_height("", pane.height) + 2);
        painted(&app);
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: belief_x + 2, row: chip_y, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.active_group, 1);
        assert!(app.lore_picker.is_some());
        assert_eq!(app.active_chooser_rect().unwrap().x, pane.x);
    }

    #[test]
    fn input_blink_ticks_at_bounded_interval_and_resets_when_resolved() {
        let mut app = App::default();
        let start = Instant::now();
        app.blink_at = start;
        assert!(!app.tick_blink(start));
        app.input_requests.push(InputRequest::from_event("a", &json!({
            "id":"req", "kind":"permission", "title":"Approve?"
        })).unwrap());
        assert!(!app.tick_blink(start + Duration::from_millis(649)));
        assert!(app.tick_blink(start + Duration::from_millis(650)));
        assert!(!app.blink_on);
        assert!(!app.tick_blink(start + Duration::from_millis(700)));
        app.input_requests[0].sending = true;
        assert!(app.tick_blink(start + Duration::from_millis(701)));
        assert!(app.blink_on);
        app.input_requests.clear();
        assert!(!app.tick_blink(start + Duration::from_millis(702)));
    }

    #[test]
    fn grouped_rail_blinks_only_waiting_session_and_clears_on_resolution() {
        let mut app = App::default();
        for (id, title, collection) in [
            ("first", "First", "A"),
            ("second", "Second", "A"),
            ("third", "Third session with long title", "B"),
        ] {
            app.apply_update(DaemonUpdate::Upsert(Session {
                id: id.into(), title: title.into(), collection: collection.into(),
                transcript: String::new(), status: "Ready".into(),
            }));
        }
        app.collections = vec![
            crate::collections::Collection { name:"A".into(), sessions:vec!["first".into(), "second".into()], collapsed:false, colour:None },
            crate::collections::Collection { name:"B".into(), sessions:vec!["third".into()], collapsed:false, colour:None },
        ];
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"third",
            "event":{"type":"needs_input", "data":{"id":"req", "kind":"ask_user",
                "title":"Choose", "questions":[{"question":"Choose freely","options":[]}]}}}));
        // Two collection headings precede the third session's row. Keep the
        // rail narrow enough to clip titles while retaining the attention cue.
        let mut terminal = Terminal::new(TestBackend::new(12, 9)).unwrap();
        let mut draw = |app: &App| {
            terminal.draw(|frame| app.draw_rail(frame, Rect::new(0, 0, 12, 9))).unwrap();
            let buffer = terminal.backend().buffer();
            (buffer[(3, 3)].bg, buffer[(3, 5)].bg)
        };
        assert_eq!(draw(&app), (theme::RAIL, theme::ERROR));
        assert!(app.tick_blink(app.blink_at + INPUT_BLINK_INTERVAL));
        assert_eq!(draw(&app), (theme::RAIL, theme::RAIL));
        assert!(app.tick_blink(app.blink_at + INPUT_BLINK_INTERVAL));
        assert_eq!(draw(&app), (theme::RAIL, theme::ERROR));
        app.input_requests[0].sending = true;
        assert_eq!(draw(&app), (theme::RAIL, theme::RAIL));
        app.input_requests[0].sending = false;
        assert_eq!(draw(&app), (theme::RAIL, theme::ERROR));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"third",
            "event":{"type":"needs_input_resolved", "data":{"id":"req"}}}));
        assert_eq!(draw(&app), (theme::RAIL, theme::RAIL));
    }

    #[test]
    fn urgency_sort_waits_for_quiet_and_never_moves_a_group_under_interaction() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 24));
        app.sidebar_auto = false;
        app.rail_visible = true;
        app.preferences.set_for_test("collection_sort", "urgency");
        for id in ["a", "b"] {
            app.apply_update(DaemonUpdate::Upsert(Session { id:id.into(), title:id.into(),
                collection:id.into(), transcript:String::new(), status:"Ready".into() }));
        }
        app.collections = vec![
            crate::collections::Collection { name:"A".into(), sessions:vec!["a".into()], collapsed:false, colour:None },
            crate::collections::Collection { name:"B".into(), sessions:vec!["b".into()], collapsed:false, colour:None },
        ];
        app.rail_selected = 1;
        app.session_telemetry.entry("b".into()).or_default().context_percent = Some(60.0);
        let start = Instant::now();
        assert!(!app.tick_rail_sort(start));
        assert!(matches!(app.rail_rows()[0], RailRow::Heading(0)));
        app.blink_on = false; // phase changes cannot re-rank a waiting group
        assert!(app.tick_rail_sort(start + Duration::from_secs(2)));
        assert!(matches!(app.rail_rows()[0], RailRow::Heading(1)));
        assert_eq!(app.sessions[app.rail_order()[app.rail_selected]].id, "b");
        let mut terminal = Terminal::new(TestBackend::new(25, 8)).unwrap();
        terminal.draw(|frame| app.draw_rail(frame, Rect::new(0, 0, 25, 8))).unwrap();
        let rendered = terminal.backend().buffer().content.iter().map(|cell| cell.symbol()).collect::<String>();
        assert!(rendered.contains("ctx B"));

        app.session_telemetry.get_mut("b").unwrap().context_percent = None;
        assert!(!app.tick_rail_sort(start + Duration::from_millis(2100)));
        let rail = app.layout(app.size).rail.unwrap();
        app.mouse(MouseEvent { kind:MouseEventKind::Moved, column:rail.x+2, row:rail.y+1,
            modifiers:KeyModifiers::NONE });
        assert!(app.rail_pointer_inside);
        assert!(!app.tick_rail_sort(start + Duration::from_secs(5)));
        assert!(matches!(app.rail_rows()[0], RailRow::Heading(1)));
        app.mouse(MouseEvent { kind:MouseEventKind::Moved, column:rail.right(), row:rail.y+1,
            modifiers:KeyModifiers::NONE });
        app.focus = Focus::Rail;
        assert!(!app.tick_rail_sort(start + Duration::from_secs(6)));
        app.focus = Focus::Prompt;
        assert!(app.tick_rail_sort(start + Duration::from_secs(8)));
        assert!(matches!(app.rail_rows()[0], RailRow::Heading(0))); // original tie order
        assert_eq!(app.sessions[app.rail_order()[app.rail_selected]].id, "b");
    }

    #[test]
    fn project_urgency_sort_keeps_child_order_and_unknown_context_unranked() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 24));
        app.sidebar_auto = false; app.rail_visible = true;
        app.preferences.set_for_test("collection_sort", "urgency");
        for (id, project) in [("a1", "alpha"), ("z", "zeta"), ("a2", "alpha")] {
            app.apply_update(DaemonUpdate::Upsert(Session { id:id.into(), title:id.into(),
                collection:project.into(), transcript:String::new(), status:"Ready".into() }));
        }
        app.session_telemetry.entry("a1".into()).or_default().context_percent = None;
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"z",
            "event":{"type":"needs_input", "data":{"id":"req", "kind":"ask_user",
                "title":"Choose", "questions":[{"question":"Choose","options":[]}]}}}));
        let start = Instant::now();
        assert!(!app.tick_rail_sort(start));
        assert!(app.tick_rail_sort(start + Duration::from_secs(2)));
        let rows = app.rail_rows();
        assert!(matches!(&rows[0], RailRow::ProjectHeading(name) if name == "zeta"));
        assert!(matches!(&rows[1], RailRow::Session(1)));
        assert!(matches!(&rows[2], RailRow::ProjectHeading(name) if name == "alpha"));
        assert!(matches!(&rows[3], RailRow::Session(0)));
        assert!(matches!(&rows[4], RailRow::Session(2)));
    }

    #[test]
    fn lore_pending_urgency_includes_collapsed_members_but_excludes_stale_and_remote() {
        let mut app = App::default();
        app.apply_update(DaemonUpdate::Upsert(Session { id:"local".into(), title:"Local".into(),
            collection:"Work".into(), transcript:String::new(), status:"Ready".into() }));
        app.apply_update(DaemonUpdate::Upsert(Session { id:"host~remote".into(), title:"Remote".into(),
            collection:"Elsewhere".into(), transcript:String::new(), status:"Ready".into() }));
        app.session_cwds.insert("local".into(), PathBuf::from("/fixture/own"));
        app.session_cwds.insert("host~remote".into(), PathBuf::from("/fixture/elsewhere"));
        app.collections = vec![
            crate::collections::Collection { name:"Work".into(), sessions:vec!["local".into()], collapsed:true, colour:None },
            crate::collections::Collection { name:"Elsewhere".into(), sessions:vec!["host~remote".into()], collapsed:true, colour:None },
        ];
        let now = Instant::now();
        app.lore_pending_cache.insert("local".into(), LorePendingSignal {
            cwd:PathBuf::from("/fixture/own"), pending:Some(true), checked:now });
        app.lore_pending_cache.insert("host~remote".into(), LorePendingSignal {
            cwd:PathBuf::from("/fixture/elsewhere"), pending:Some(true), checked:now });
        let groups = app.rail_groups();
        assert_eq!(groups[0].2, 2);
        assert_eq!(groups[1].2, 0);
        assert_eq!(groups[0].1.len(), 1); // Collapsed child is still counted.
        app.lore_pending_cache.get_mut("local").unwrap().checked = now - Duration::from_secs(91);
        assert_eq!(app.rail_groups()[0].2, 0);
        app.lore_pending_cache.get_mut("local").unwrap().checked = now;
        app.session_cwds.insert("local".into(), PathBuf::from("/fixture/changed"));
        assert_eq!(app.rail_groups()[0].2, 0);
        app.remote_mode = true;
        assert!(app.poll_lore_pending());
        assert!(app.lore_pending_cache.is_empty());
    }

    #[test]
    fn lore_incomplete_empty_response_stays_unknown_and_positive_row_is_visible() {
        let mut app = App::default();
        let cwd = PathBuf::from("/fixture/source");
        for id in ["empty", "positive"] {
            app.apply_update(DaemonUpdate::Upsert(Session { id:id.into(), title:id.into(),
                collection:id.into(), transcript:String::new(), status:"Ready".into() }));
            app.session_cwds.insert(id.into(), cwd.clone());
        }
        let (tx, receiver) = mpsc::sync_channel(1);
        app.lore_pending_request = Some(LorePendingRequest {
            sessions: vec![("empty".into(), cwd.clone()), ("positive".into(), cwd.clone())], receiver,
        });
        tx.send(LorePendingBatch { sessions: vec![("empty".into(), cwd.clone()), ("positive".into(), cwd.clone())],
            response: Ok(doxa_lore::PendingSessions {
            source_project_slug: "fixture".into(), snapshot: "a".repeat(64), complete: false,
            sessions: vec![
                doxa_lore::PendingSession { session_id:"empty".into(), pending_pids:vec![], complete:false },
                doxa_lore::PendingSession { session_id:"positive".into(), pending_pids:vec!["p".into()], complete:false },
            ],
        }) }).unwrap();
        assert!(app.poll_lore_pending());
        assert_eq!(app.lore_pending_cache["empty"].pending, None);
        assert_eq!(app.lore_pending_cache["positive"].pending, Some(true));
        assert_eq!(app.rail_urgency(0), 0);
        assert_eq!(app.rail_urgency(1), 2);
    }

    #[test]
    fn lore_pending_worker_only_updates_its_selected_scope() {
        let mut app = App::default();
        let own = PathBuf::from("/fixture/own");
        let other = PathBuf::from("/fixture/other");
        for (id, cwd) in [("own", &own), ("other", &other)] {
            app.apply_update(DaemonUpdate::Upsert(Session { id:id.into(), title:id.into(),
                collection:id.into(), transcript:String::new(), status:"Ready".into() }));
            app.session_cwds.insert(id.into(), cwd.clone());
        }
        let (tx, receiver) = mpsc::sync_channel(1);
        app.lore_pending_request = Some(LorePendingRequest {
            sessions: vec![("own".into(), own.clone()), ("other".into(), other.clone())], receiver,
        });
        tx.send(LorePendingBatch {
            sessions: vec![("own".into(), own.clone())],
            response: Ok(doxa_lore::PendingSessions {
                source_project_slug:"fixture".into(), snapshot:"a".repeat(64), complete:true,
                sessions:vec![doxa_lore::PendingSession {
                    session_id:"own".into(), pending_pids:vec!["p".into()], complete:true,
                }],
            }),
        }).unwrap();
        assert!(app.poll_lore_pending());
        assert_eq!(app.lore_pending_cache["own"].pending, Some(true));
        assert!(!app.lore_pending_cache.contains_key("other"));
    }

    #[test]
    fn lore_scope_lookup_does_not_block_terminal_poll() {
        use std::os::unix::fs::PermissionsExt;
        const CHILD: &str = "DOXA_TEST_SLOW_LORE_GIT";
        if std::env::var_os(CHILD).is_none() {
            let dir = tempfile::tempdir().unwrap();
            let git = dir.path().join("git");
            std::fs::write(&git, b"#!/bin/sh\n/bin/sleep 2\nexit 1\n").unwrap();
            std::fs::set_permissions(&git, std::fs::Permissions::from_mode(0o700)).unwrap();
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "ui::tests::lore_scope_lookup_does_not_block_terminal_poll"])
                .env(CHILD, "1").env("PATH", dir.path()).env("DOXA_TEST_LORE_CWD", dir.path())
                .output().unwrap();
            assert!(output.status.success(), "{}{}", String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr));
            return;
        }
        let mut app = App::default();
        let cwd = PathBuf::from(std::env::var_os("DOXA_TEST_LORE_CWD").unwrap());
        app.apply_update(DaemonUpdate::Upsert(Session { id:"local".into(), title:"Local".into(),
            collection:"Local".into(), transcript:String::new(), status:"Ready".into() }));
        app.session_cwds.insert("local".into(), cwd);
        let started = Instant::now();
        app.poll_lore_pending();
        assert!(started.elapsed() < Duration::from_millis(750),
            "Git scope discovery blocked the terminal event loop");
    }

    #[test]
    fn unnamed_collection_uses_known_project_and_task_without_changing_explicit_names() {
        let mut app = App::default();
        app.apply_update(DaemonUpdate::Upsert(Session { id:"s".into(), title:"Fix picker".into(),
            collection:"/fixture/collection-triage-repo".into(), transcript:String::new(), status:"Ready".into() }));
        app.groups[0].tabs.push("s".into());
        app.session_cwds.insert("s".into(), PathBuf::from("/fixture/collection-triage-repo"));
        app.set_repo_status("s", doxa_worktrees::RepoStatus::Repository {
            repo:"doxa".into(), base:None, checked_out:None, sha:None, worktree:None });
        app.local_collection("new");
        assert!(app.collections[0].name.ends_with("doxa · Fix picker"));
        app.local_collection("new");
        assert_ne!(app.collections[0].name, app.collections[1].name);
        app.local_collection("new Chosen name");
        assert_eq!(app.collections[2].name, "Chosen name");
    }

    #[test]
    fn pane_badge_aggregates_hidden_tabs_without_inventing_context() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 24));
        app.rail_visible = true; app.rail_width = 30;
        for (id, title) in [("a", "Active"), ("b", "Beta")] {
            app.apply_update(DaemonUpdate::Upsert(Session { id:id.into(), title:title.into(),
                collection:"project".into(), transcript:String::new(), status:"Ready".into() }));
        }
        app.groups[0].tabs = vec!["a".into(), "b".into()];
        app.groups[0].active = 0;
        assert_eq!(app.pane_signal("a").unwrap().badge(), " [2 tabs]");
        app.session_telemetry.entry("b".into()).or_default().context_percent = Some(51.0);
        assert_eq!(app.pane_signal("a").unwrap().badge(), " [2 ctx#2:Beta]");
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"b",
            "event":{"type":"needs_input", "data":{"id":"question", "kind":"ask_user",
                "title":"Choose", "questions":[{"question":"Choose", "options":[]}]}}}));
        let signal = app.pane_signal("a").unwrap();
        assert_eq!(signal.rank, 4);
        assert_eq!(signal.hidden_source.as_ref().map(|(tab, title)| (*tab, title.as_str())), Some((2, "Beta")));
        assert!(painted_at(&app, 100, 24).contains("[2 !#2:Beta]"));
    }

    #[test]
    fn pane_rail_navigates_existing_groups_and_aggregates_hidden_urgency() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 30));
        app.sidebar_auto = false; app.rail_visible = true;
        app.preferences.set_for_test("rail_entries", "panes");
        for (id, title) in [("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")] {
            app.apply_update(DaemonUpdate::Upsert(Session { id:id.into(), title:title.into(),
                collection:"Work".into(), transcript:String::new(), status:"Ready".into() }));
        }
        app.groups[0].tabs = vec!["a".into(), "b".into()]; app.groups[0].active = 0;
        app.groups[1].tabs = vec!["c".into()]; app.groups[1].active = 0;
        assert_eq!(app.rail_order(), [0, 2]);
        assert!(app.rail_rows().iter().any(|row| matches!(row, RailRow::Pane { group:0, active:0 })));
        assert!(!app.rail_rows().iter().any(|row| matches!(row, RailRow::Session(1))));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"b",
            "event":{"type":"needs_input", "data":{"id":"req", "kind":"ask_user",
                "title":"Choose", "questions":[{"question":"Choose","options":[]}]}}}));
        assert_eq!(app.rail_groups()[0].2, 4);
        assert!(app.pane_signal("a").unwrap().badge().contains("!#2:Beta"));
        app.focus = Focus::Rail; app.rail_selected = 1;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.active_group, 1);
        assert_eq!(app.groups[0].tabs, ["a", "b"]);
        let rail = app.layout(app.size).rail.unwrap();
        let row = app.rail_rows().iter().position(|row| matches!(row, RailRow::Pane { group:0, .. })).unwrap();
        app.handle(Event::Mouse(MouseEvent { kind:MouseEventKind::Down(MouseButton::Left),
            column:rail.x + 2, row:rail.y + 1 + row as u16, modifiers:KeyModifiers::NONE }));
        assert_eq!(app.active_group, 0);
        assert_eq!(app.groups[1].tabs, ["c"]);
        app.focus = Focus::Tabs;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Right, KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(), Some("b"));
        assert_eq!(app.rail_order(), [1, 2]);
        assert_eq!(app.rail_selected, 0); // Selection follows the pane, not the old tab.
    }

    #[test]
    fn pane_rail_group_sort_waits_for_settling_and_keeps_member_order() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 30));
        app.sidebar_auto = false; app.rail_visible = true;
        app.preferences.set_for_test("rail_entries", "panes");
        app.preferences.set_for_test("collection_sort", "urgency");
        for id in ["a", "b", "c"] {
            app.apply_update(DaemonUpdate::Upsert(Session { id:id.into(), title:id.into(),
                collection:String::new(), transcript:String::new(), status:"Ready".into() }));
        }
        app.groups[0].tabs = vec!["a".into(), "b".into()]; app.groups[0].active = 0;
        app.groups[1].tabs = vec!["c".into()]; app.groups[1].active = 0;
        app.collections = vec![
            crate::collections::Collection { name:"A".into(), sessions:vec!["a".into()], collapsed:false, colour:None },
            crate::collections::Collection { name:"C".into(), sessions:vec!["c".into()], collapsed:false, colour:None },
        ];
        app.session_telemetry.entry("c".into()).or_default().context_percent = Some(60.0);
        let start = Instant::now();
        assert!(!app.tick_rail_sort(start));
        assert!(matches!(app.rail_rows()[0], RailRow::Heading(0)));
        assert!(app.tick_rail_sort(start + Duration::from_secs(2)));
        assert!(matches!(app.rail_rows()[0], RailRow::Heading(1)));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"b",
            "event":{"type":"needs_input", "data":{"id":"req", "kind":"ask_user",
                "title":"Choose", "questions":[{"question":"Choose","options":[]}]}}}));
        assert!(!app.tick_rail_sort(start + Duration::from_secs(3)));
        assert!(matches!(app.rail_rows()[0], RailRow::Heading(1)));
        assert!(app.tick_rail_sort(start + Duration::from_secs(5)));
        assert!(matches!(app.rail_rows()[0], RailRow::Heading(0)));
        assert_eq!(app.rail_order(), [0, 2]);
        assert_eq!(app.groups[0].tabs, ["a", "b"]);
    }

    #[test]
    fn manual_collection_hue_paints_heading_without_replacing_its_label() {
        let mut app = App::default();
        app.apply_update(DaemonUpdate::Upsert(Session { id:"a".into(), title:"Alpha".into(),
            collection:String::new(), transcript:String::new(), status:"Ready".into() }));
        app.collections.push(crate::collections::Collection {
            name:"Release Work".into(), sessions:vec!["a".into()], collapsed:false, colour:Some("teal".into()),
        });
        let mut terminal = Terminal::new(TestBackend::new(40, 8)).unwrap();
        terminal.draw(|frame| app.draw_rail(frame, Rect::new(0, 0, 40, 8))).unwrap();
        let line = (1..39).map(|x| terminal.backend().buffer()[(x, 1)].symbol()).collect::<String>();
        assert!(line.contains("Release Work"));
        assert!((1..39).any(|x| terminal.backend().buffer()[(x, 1)].fg == Color::Rgb(0x78, 0xCF, 0xC3)));
    }

    #[test]
    fn pane_rail_withholds_project_hue_for_mixed_or_unknown_roots() {
        let mut app = App::default();
        app.preferences.set_for_test("rail_entries", "panes");
        app.project_colours = Some(HashMap::new());
        for id in ["a", "b"] {
            app.apply_update(DaemonUpdate::Upsert(Session { id:id.into(), title:id.into(),
                collection:"repo".into(), transcript:String::new(), status:"Ready".into() }));
        }
        app.groups[0].tabs = vec!["a".into(), "b".into()]; app.groups[0].active = 0;
        let root = PathBuf::from("/verified/repo");
        app.project_roots.insert("a".into(), root.clone());
        app.project_colours.as_mut().unwrap().insert(root.clone(), "blue".into());
        assert_eq!(app.pane_project_marker(0), " [root?]");
        assert_eq!(app.rail_project_colour("repo"), None);
        app.project_roots.insert("b".into(), PathBuf::from("/verified/other"));
        assert_eq!(app.pane_project_marker(0), " [mixed]");
        assert_eq!(app.rail_project_colour("repo"), None);
        app.project_roots.insert("b".into(), root);
        assert_eq!(app.pane_project_marker(0), "");
        assert_eq!(app.rail_project_colour("repo"), Some(Color::Rgb(0x8A, 0xBF, 0xF2)));
    }

    #[test]
    fn project_hue_requires_one_known_root_even_when_labels_match() {
        let mut app = App::default();
        app.project_colours = Some(HashMap::new());
        for id in ["a", "b"] {
            app.apply_update(DaemonUpdate::Upsert(Session { id:id.into(), title:id.into(),
                collection:"same".into(), transcript:String::new(), status:"Ready".into() }));
        }
        assert_eq!(app.rail_project_colour("same"), None);
        let root = PathBuf::from("/verified/project");
        app.project_roots.insert("a".into(), root.clone());
        assert_eq!(app.rail_project_colour("same"), None);
        app.project_roots.insert("b".into(), PathBuf::from("/verified/other"));
        assert_eq!(app.rail_project_colour("same"), None);
        app.project_roots.insert("b".into(), root.clone());
        app.project_colours.as_mut().unwrap().insert(root, "blue".into());
        assert_eq!(app.rail_project_colour("same"), Some(Color::Rgb(0x8A, 0xBF, 0xF2)));
        let colours = app.project_colours.take();
        assert_eq!(app.rail_project_colour("same"), None);
        app.project_colours = colours;
        let mut terminal = Terminal::new(TestBackend::new(40, 8)).unwrap();
        terminal.draw(|frame| app.draw_rail(frame, Rect::new(0, 0, 40, 8))).unwrap();
        assert_eq!(terminal.backend().buffer()[(2, 1)].fg, Color::Rgb(0x8A, 0xBF, 0xF2));
    }

    #[test]
    fn hidden_tab_gets_one_background_project_probe() {
        let mut app = App::default();
        for id in ["active", "hidden"] {
            app.apply_update(DaemonUpdate::Upsert(Session { id:id.into(), title:id.into(),
                collection:"project".into(), transcript:String::new(), status:"Ready".into() }));
        }
        app.groups[0].tabs = vec!["active".into(), "hidden".into()];
        app.repo_cache.insert("active".into(), (None, Instant::now()));
        app.session_cwds.insert("hidden".into(), PathBuf::from("/nonexistent/doxa-triage-hidden"));
        app.poll_repo();
        assert_eq!(app.repo_pending.as_ref().map(|(id, _, _, _)| id.as_str()), Some("hidden"));
    }

    #[test]
    fn uncollected_sessions_group_by_recorded_project() {
        let mut app = App::default();
        for (id, project) in [("one", "/work/zeta"), ("two", "/work/alpha"), ("three", "/work/zeta")] {
            app.apply_update(DaemonUpdate::Upsert(Session {
                id: id.into(), title: id.into(), collection: project.into(),
                transcript: String::new(), status: "Ready".into(),
            }));
        }
        app.set_repo_status("one", doxa_worktrees::RepoStatus::Repository {
            repo: "zeta".into(), base: None, checked_out: None, sha: None, worktree: None,
        });
        app.set_repo_status("three", doxa_worktrees::RepoStatus::Repository {
            repo: "zeta".into(), base: None, checked_out: None, sha: None, worktree: Some("feature".into()),
        });
        let rows = app.rail_rows();
        assert!(matches!(&rows[0], RailRow::ProjectHeading(label) if label == "/work/alpha"));
        assert!(matches!(&rows[1], RailRow::Session(1)));
        assert!(matches!(&rows[2], RailRow::ProjectHeading(label) if label == "zeta"));
        assert!(matches!(&rows[3], RailRow::Session(0)));
        assert!(matches!(&rows[4], RailRow::Session(2)));
        assert_eq!(app.rail_order(), [1, 0, 2]);
    }

    #[test]
    fn closed_archives_hide_until_opened_and_use_muted_rail_rows() {
        let mut app = App::default();
        app.handle(Event::Resize(80, 20));
        for (id, title) in [("live", "Current work"), ("dead", "Saved work"), ("empty", "Unavailable")] {
            app.apply_update(DaemonUpdate::Upsert(Session { id: id.into(), title: title.into(),
                collection: "repo".into(), transcript: if id == "empty" { "" } else { "saved" }.into(),
                status: "Ready".into() }));
        }
        app.offline_ids.extend(["dead".into(), "empty".into()]);
        app.detached_this_run.extend(["dead".into(), "empty".into()]);
        assert_eq!(app.rail_order(), [0]);
        app.groups[0].tabs.push("dead".into());
        let rows = app.rail_rows();
        assert!(matches!(&rows[0], RailRow::ProjectHeading(project) if project == "repo"));
        assert!(matches!(&rows[1], RailRow::Session(0)));
        assert!(matches!(&rows[2], RailRow::PastHeading));
        assert!(matches!(&rows[3], RailRow::Session(1)));
        assert_eq!(app.rail_order(), [0, 1]);
        let mut terminal = Terminal::new(TestBackend::new(80, 20)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let rail = app.layout(app.size).rail.unwrap();
        let style = terminal.backend().buffer()[(rail.x + 3, rail.y + 4)].style();
        assert_eq!(style.fg, Some(theme::SECONDARY));
        assert!(style.add_modifier.contains(Modifier::ITALIC));
    }

    #[test]
    fn rail_scrollbar_reaches_bottom_at_last_session() {
        let mut app=App::default();
        app.handle(Event::Resize(80,20));
        app.rail_visible=true;
        for index in 0..30 {
            let id=format!("session-{index}");
            app.apply_update(DaemonUpdate::Upsert(Session {id:id.clone(),title:id,
                collection:"repo".into(),transcript:String::new(),status:"Ready".into()}));
        }
        app.rail_selected=app.rail_order().len()-1;
        let mut terminal=Terminal::new(TestBackend::new(80,20)).unwrap();
        terminal.draw(|frame|app.draw(frame)).unwrap();
        let rail=app.layout(app.size).rail.unwrap();
        assert_eq!(terminal.backend().buffer()[(rail.right()-2,rail.bottom()-2)].symbol(),"█");
    }

    #[test]
    fn collection_commands_move_active_session_and_order_rail() {
        let mut app = App::default();
        for id in ["one", "two"] {
            app.apply_update(DaemonUpdate::Upsert(Session {
                id:id.into(), title:id.into(), collection:"repo".into(),
                transcript:String::new(), status:"Ready".into(),
            }));
        }
        app.groups[0].tabs = vec!["one".into(), "two".into()];
        app.groups[0].active = 1;
        let before = crate::ui_state::LayoutSignature::capture(&app);
        app.input = "/collection add Work".into();
        assert!(app.submit_local_command());
        assert_ne!(before, crate::ui_state::LayoutSignature::capture(&app));
        assert!(app.input.is_empty());
        assert_eq!(app.collections[0].sessions, ["two"]);
        assert_eq!(app.rail_order(), [1, 0]);
        app.input = "/collection remove".into();
        assert!(app.submit_local_command());
        assert!(app.collections[0].sessions.is_empty());
        assert!(app.take_prompts().is_empty());
    }

    #[test]
    fn folded_collection_hides_members_and_rail_clicks_follow_visible_rows() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        for id in ["held", "loose"] {
            app.apply_update(DaemonUpdate::Upsert(Session { id:id.into(), title:id.into(),
                collection:String::new(), transcript:String::new(), status:"Ready".into() }));
        }
        app.collections.push(crate::collections::Collection {
            name:"Work".into(), sessions:vec!["held".into()], collapsed:false, colour:None,
        });
        assert_eq!(app.rail_order(), [0, 1]);
        let click = |row| MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column:3, row, modifiers:KeyModifiers::NONE };
        assert!(app.mouse(click(1))); // Work heading
        assert!(app.collections[0].collapsed);
        assert_eq!(app.rail_order(), [1]);
        let before = app.groups[0].tabs.clone();
        assert!(app.mouse(click(2))); // Sessions heading, no session selected
        assert_eq!(app.groups[0].tabs, before);
        assert!(app.mouse(click(3))); // loose session
        assert_eq!(app.groups[0].active_id(), Some("loose"));
        assert!(app.mouse(click(1)));
        assert!(!app.collections[0].collapsed);
        assert_eq!(app.rail_order(), [0, 1]);
    }

    #[test]
    fn long_question_title_is_clipped_and_body_keeps_scrollable_text() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.rail_visible = false;
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"a", "engine":"codex"}));
        app.groups[0].tabs = vec!["a".into()];
        let question = "Which deployment target should receive the migration? ".repeat(30);
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"a",
            "event":{"type":"needs_input", "data":{"id":"req-long", "kind":"ask_user",
                "questions":[{"question":question.clone(), "options":[{"label":"Staging"}]}]}}}));
        let menu = app.active_chooser_rect().unwrap();
        assert_eq!(menu.height, 18);
        let rows: Vec<_> = painted(&app).lines().map(str::to_owned).collect();
        assert!(rows[usize::from(menu.y)].contains('…'));
        assert!(rows[usize::from(menu.y + 1)].contains("Which deployment"));
        let (body, _, _) = input_request_body(&app.input_requests[0], usize::from(menu.width.saturating_sub(4)));
        assert!(body.contains(&question));
        assert!(!body.contains("Question:"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)));
        assert!(app.input_requests[0].scroll > 0);
        assert!(painted(&app).contains("Prompt"));
    }

    #[test]
    fn offline_history_opens_read_only_and_never_queues_prompt() {
        let mut app = App::default();
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        tx.send(vec![history::OfflineSession { id: "saved-1".into(),
            project: "project\u{1b}[31m".into(), markdown: "**You:** saved".into(), search_snippets: Vec::new(), cwd: None }]).unwrap();
        assert!(app.poll_history());
        assert!(app.offline_ids.contains("saved-1"));
        assert!(!app.sessions[0].collection.contains('\u{1b}'));
        app.handle(Event::Resize(100, 28));
        app.open_history();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(), Some("saved-1"));
        app.focus = Focus::Prompt;
        app.input = "do not send".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.pending_prompts.is_empty());
        assert_eq!(app.notice, "Archived transcript is read-only");
    }

    #[test]
    fn repeated_history_results_keep_a_bounded_unopened_cache() {
        let mut app = App::default();
        for number in 0..80 {
            let (tx, rx) = mpsc::sync_channel(1);
            app.history_pending = Some(rx);
            tx.send(vec![history::OfflineSession { id: format!("archive-{number}"),
                project: "project".into(), markdown: "x".repeat(4096),
                search_snippets: Vec::new(), cwd: None }]).unwrap();
            assert!(app.poll_history());
        }
        assert_eq!(app.offline_ids.len(), 64);
        assert_eq!(app.history_entries.len(), 64);
        assert!(!app.offline_ids.contains("archive-0"));
        assert!(app.offline_ids.contains("archive-79"));
        app.groups[0].tabs.push("archive-79".into());
        for number in 80..160 {
            let (tx, rx) = mpsc::sync_channel(1);
            app.history_pending = Some(rx);
            tx.send(vec![history::OfflineSession { id: format!("archive-{number}"),
                project: "project".into(), markdown: "saved".into(),
                search_snippets: Vec::new(), cwd: None }]).unwrap();
            assert!(app.poll_history());
        }
        assert!(app.offline_ids.contains("archive-79"));
        assert!(app.offline_ids.len() <= 65);
    }

    #[test]
    fn pruning_archived_inventory_preserves_highlighted_session_identity() {
        for selected in [0, 20] {
            let mut app = App::default();
            for index in 0..80 {
                let id = format!("archive-{index:02}");
                app.offline_ids.insert(id.clone());
                app.sessions.push(Session { id: id.clone(), title: id, collection: "project".into(),
                    transcript: String::new(), status: "Archived".into() });
            }
            app.history_modal = true;
            app.history_selected = selected;
            let expected = app.sessions[selected].id.clone();
            app.prune_unopened_history();
            assert_eq!(app.sessions.len(), 64);
            assert_eq!(app.sessions[app.history_matches()[app.history_selected]].id, expected);
        }
    }

    #[test]
    fn empty_search_preserves_pending_recent_history_inventory() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        app.local_search("");
        assert!(app.history_modal);
        tx.send(vec![history::OfflineSession { id: "saved-empty-search".into(),
            project: "project".into(), markdown: "saved turn".into(),
            search_snippets: Vec::new(), cwd: None }]).unwrap();
        assert!(app.poll_history());
        assert!(app.sessions.iter().any(|session| session.id == "saved-empty-search"));
        assert!(app.history_query_due.is_none());
    }

    #[test]
    fn resume_prefix_uses_nonmodal_picker_and_never_reaches_model_prompt() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.input = "/resume saved".into();
        assert!(app.submit_local_command());
        assert!(app.input.is_empty());
        assert!(app.history_modal && app.history_resume);
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        tx.send(vec!["saved-1", "saved-2"].into_iter().map(|id| history::OfflineSession {
            id: id.into(), project: "project".into(), markdown: "**You:** old turn".into(), search_snippets: Vec::new(), cwd: None,
        }).collect()).unwrap();
        assert!(app.poll_history());
        assert_eq!(app.history_matches().len(), 2);
        assert!(painted(&app).contains("Resume session"));
        assert!(app.pending_prompts.is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(!app.history_modal);
        app.input = "/resume ../unsafe".into();
        assert!(app.submit_local_command());
        assert!(app.notice.contains("valid session ID"));
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn local_search_filters_saved_transcript_content() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        tx.send(vec![history::OfflineSession { id: "archive-1".into(), project: "project".into(),
            markdown: "**You:** hidden needle".into(), search_snippets: Vec::new(), cwd: None }]).unwrap();
        app.poll_history();
        app.input = "/search needle".into();
        assert!(app.submit_local_command());
        assert!(app.history_modal && !app.history_resume);
        assert_eq!(app.history_matches().len(), 1);
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn archive_raw_search_hit_survives_truncated_render_only_for_its_query() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.local_search("needle");
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        app.history_scan_query = Some("needle".into());
        tx.send(vec![history::OfflineSession { id: "old-archive".into(), project: "project".into(),
            markdown: "**You:** recent visible turn".into(), search_snippets: Vec::new(), cwd: None }]).unwrap();
        assert!(app.poll_history());
        assert_eq!(app.history_matches().len(), 1, "raw JSONL scan found an older hidden turn");
        app.history_query = "different".into();
        assert!(app.history_matches().is_empty(), "scan hit must not satisfy a different query");
    }

    #[test]
    fn live_search_debounces_and_discards_older_query_result() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.local_search("first");
        let due = app.history_query_due.unwrap();
        assert!(!app.start_due_history_query(due - Duration::from_millis(1)));
        app.history_key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
        assert_eq!(app.history_query, "firstx");
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        app.history_scan_query = Some("first".into());
        tx.send(vec![history::OfflineSession { id: "stale-1".into(), project: "project".into(),
            markdown: "old".into(), search_snippets: vec!["first".into()], cwd: None }]).unwrap();
        assert!(app.poll_history());
        assert!(!app.history_entries.contains_key("stale-1"));
        assert!(app.history_query_due.is_some());
    }

    #[test]
    fn indexed_excerpts_group_under_session_and_strip_terminal_controls() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.local_search("needle");
        let (tx, rx) = mpsc::sync_channel(1);
        app.history_pending = Some(rx);
        app.history_scan_query = Some("needle".into());
        tx.send(vec![history::OfflineSession { id: "saved-1".into(), project: "project".into(),
            markdown: "recent visible turn".into(),
            search_snippets: vec!["first [needle] \u{1b}[31m".into(), "second [needle]".into()], cwd: None }]).unwrap();
        assert!(app.poll_history());
        let rows = app.history_rows(8);
        assert_eq!(rows.len(), 3);
        assert!(rows[0].1);
        assert!(!rows[1].1 && !rows[2].1);
        assert!(rows[1].2.contains("first [needle]"));
        assert!(!rows[1].2.contains('\u{1b}'));
        assert!(painted(&app).contains("second [needle]"));
    }

    #[test]
    fn queue_picker_cancels_exact_selected_id_and_refuses_duplicate_ids() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.apply_update(DaemonUpdate::Upsert(Session { id: "session-1".into(), title: "Live".into(),
            collection: "repo".into(), transcript: String::new(), status: "Ready".into() }));
        app.groups[0].tabs.push("session-1".into());
        app.input = "/queue".into();
        assert!(app.submit_local_command());
        assert!(app.queue_picker.is_some());
        assert!(app.active_chooser_rect().is_some());
        assert!(matches!(app.pending_queue_commands.pop(), Some(crate::bridge::WorkerCommand::QueueList(id)) if id == "session-1"));
        app.apply_daemon_frame(&json!({"type":"queue_list_reply","session_id":"session-1","ok":true,
            "rows":[{"id":"q3","preview":"first scrubbed"},{"id":"q9","preview":"second scrubbed"}]}));
        assert!(painted(&app).contains("Prompt queue"));
        app.queue_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        app.queue_key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
        assert!(matches!(app.pending_queue_commands.pop(), Some(crate::bridge::WorkerCommand::QueueCancel(session, id))
            if session == "session-1" && id == "q9"));
        assert!(!app.apply_daemon_frame(&json!({"type":"queue_cancel_reply","session_id":"session-1","queue_id":"q3","ok":true})));
        assert_eq!(app.queue_picker.as_ref().unwrap().cancelling.as_deref(), Some("q9"));
        app.apply_daemon_frame(&json!({"type":"queue_cancel_reply","session_id":"session-1","queue_id":"q9","ok":true}));
        assert!(matches!(app.pending_queue_commands.pop(), Some(crate::bridge::WorkerCommand::QueueList(id)) if id == "session-1"));
        app.apply_daemon_frame(&json!({"type":"queue_list_reply","session_id":"session-1","ok":true,
            "rows":[{"id":"q3","preview":"a"},{"id":"q3","preview":"b"}]}));
        app.queue_key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
        assert!(app.pending_queue_commands.is_empty());
        assert!(app.notice.contains("Duplicate queue ID"));
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn history_and_mouse_switches_restore_each_pane_draft() {
        let mut app = App::default();
        for id in ["alpha", "beta"] {
            app.apply_update(DaemonUpdate::Upsert(Session { id: id.into(), title: id.into(),
                collection: "repo".into(), transcript: String::new(), status: "Ready".into() }));
        }
        app.handle(Event::Resize(100, 28));
        app.input = "alpha draft".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(), Some("beta"));
        assert!(app.input.is_empty());
        app.input = "beta draft".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::ALT)));
        assert!(app.input.is_empty());
        app.input = "second pane draft".into();
        let first = app.layout(app.size).panes.unwrap()[0];
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: first.x + 2, row: first.y + 2, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.input, "beta draft");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(), Some("alpha"));
        assert_eq!(app.input, "alpha draft");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::ALT)));
        assert_eq!(app.input, "second pane draft");
    }

    #[test]
    fn model_change_keeps_default_title_and_hello_sanitizes_id_notice() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"raw\u{1b}[31m", "model":"old"}));
        assert_eq!(app.sessions[0].title, "old");
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"raw\u{1b}[31m",
            "event":{"type":"model_changed", "data":{"model":"new"}}}));
        assert_eq!(app.sessions[0].title, "old");
        app.sessions[0].title = "Custom title".into();
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"raw\u{1b}[31m",
            "event":{"type":"model_changed", "data":{"model":"newer"}}}));
        assert_eq!(app.sessions[0].title, "Custom title");
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"raw\u{1b}[31m"}));
        assert!(!app.notice.contains('\u{1b}'));
    }

    #[test]
    fn registry_title_survives_attach_model_change_and_custom_rename() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"one",
            "model":"gpt-6-sol", "title":"gpt-6-sol@feature/menu/doxa-2"}));
        assert_eq!(app.sessions[0].title, "gpt-6-sol@feature/menu/doxa-2");
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"one",
            "event":{"type":"model_changed", "data":{"model":"gpt-6-astra"}}}));
        assert_eq!(app.sessions[0].title, "gpt-6-sol@feature/menu/doxa-2");
        app.groups[0].tabs = vec!["one".into()];
        app.local_rename("Manual title");
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"one",
            "event":{"type":"model_changed", "data":{"model":"gpt-6-luna"}}}));
        assert_eq!(app.sessions[0].title, "Manual title");
        app.local_rename("");
        assert_eq!(app.sessions[0].title, "gpt-6-sol@feature/menu/doxa-2");
    }

    #[test]
    fn resumed_session_adopts_repo_branch_title_without_overwriting_pinned_name() {
        let mut app = App::default();
        app.default_names.insert("one".into(), "gpt-6-sol@doxa/old-worktree".into());
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"one",
            "model":"gpt-6-sol", "title":"gpt-6-sol@doxa:main"}));
        assert_eq!(app.sessions[0].title, "gpt-6-sol@doxa:main");
        app.custom_names.insert("one".into(), "Pinned name".into());
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"one",
            "model":"gpt-6-sol", "title":"gpt-6-sol@doxa:feature"}));
        assert_eq!(app.sessions[0].title, "Pinned name");
    }

    #[test]
    fn restored_offline_title_is_stable_and_new_session_avoids_collision() {
        let mut app = App::default();
        app.sessions.push(Session { id:"offline".into(), title:"gpt-6-sol@main/doxa".into(),
            collection:String::new(), transcript:String::new(), status:"Offline".into() });
        app.offline_ids.insert("offline".into());
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"new",
            "model":"gpt-6-sol", "title":"gpt-6-sol@main/doxa"}));
        assert_eq!(app.sessions.iter().find(|session| session.id == "new").unwrap().title,
            "gpt-6-sol@main/doxa-2");
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"new",
            "model":"gpt-6-sol", "title":"gpt-6-sol@main/doxa"}));
        assert_eq!(app.sessions.iter().find(|session| session.id == "new").unwrap().title,
            "gpt-6-sol@main/doxa-2");
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"offline",
            "model":"gpt-6-sol", "title":"gpt-6-sol@main/doxa"}));
        assert_eq!(app.sessions.iter().find(|session| session.id == "offline").unwrap().title,
            "gpt-6-sol@main/doxa");
    }

    #[test]
    fn history_stays_closed_when_terminal_cannot_draw_it() {
        let mut app = App::default();
        app.handle(Event::Resize(27, 11));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)));
        assert!(!app.history_modal);
        assert!(app.notice.contains("Enlarge active pane"));
        app.handle(Event::Resize(100, 28));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)));
        assert!(app.history_modal);
        app.handle(Event::Resize(27, 11));
        assert!(!app.history_modal);
    }

    #[test]
    fn diff_view_modal_renders_patch_colors() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.diff_modal = true;
        app.diff_text = "Base: HEAD\n@@ -1 +1 @@\n-old\n+new".into();
        let view = painted(&app);
        assert!(view.contains("Worktree diff"));
        assert!(view.contains("+new"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(!app.diff_modal);
    }

    #[test]
    fn diff_reject_waits_for_idle_turn_and_sends_bounded_reason_after_reverting() {
        let dir = tempfile::tempdir().unwrap();
        let git = |args: &[&str]| assert!(std::process::Command::new("git").args(args)
            .current_dir(dir.path()).status().unwrap().success());
        git(&["init", "-q"]);
        std::fs::write(dir.path().join("tracked.txt"), "old\n").unwrap();
        git(&["add", "tracked.txt"]);
        git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: baseline"]);
        std::fs::write(dir.path().join("tracked.txt"), "new\n").unwrap();
        let snapshot = diff_view::read(dir.path());
        let mut app = App::default();
        app.groups[0].tabs.push("session".into());
        app.session_cwds.insert("session".into(), dir.path().to_path_buf());
        app.diff_target = Some("session".into());
        app.diff_scroll = snapshot.rejectable[0].row;
        app.diff_snapshot = Some(snapshot);
        app.session_activity.insert("session".into(), (true, 0));
        app.begin_diff_reject();
        assert_eq!(app.diff_reject_confirm.as_ref().map(|draft| draft.index), Some(0));
        app.diff_modal = true;
        app.handle(Event::Paste("Please keep the old behavior\n".into()));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.diff_reject_confirm.is_none());
        assert_eq!(app.diff_reject_queue.len(), 1);
        assert!(app.diff_reject_pending.is_none());
        assert_eq!(std::fs::read_to_string(dir.path().join("tracked.txt")).unwrap(), "new\n");
        assert!(painted(&app).contains("queued"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::F(2), KeyModifiers::NONE)));
        assert!(app.diff_modal);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('q'), KeyModifiers::CONTROL)));
        assert!(!app.should_quit);
        app.begin_diff_reject();
        app.confirm_diff_reject();
        assert_eq!(app.diff_reject_queue.len(), 1);
        assert!(app.notice.contains("already queued"));
        app.session_activity.insert("session".into(), (false, 0));
        assert!(app.poll_diff());
        assert!(app.diff_reject_pending.is_some());
        app.pending_prompts = vec![("other".into(), "waiting".into()); MAX_PENDING_PROMPTS];
        for _ in 0..100 {
            app.poll_diff();
            if app.diff_reject_pending.is_none() && app.diff_reject_feedback.is_some() { break; }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(app.diff_reject_pending.is_none());
        assert!(app.diff_reject_feedback.is_some());
        assert_eq!(app.pending_prompts.len(), MAX_PENDING_PROMPTS);
        app.pending_prompts.clear();
        assert!(app.poll_diff());
        assert!(app.diff_reject_feedback.is_none());
        assert_eq!(std::fs::read_to_string(dir.path().join("tracked.txt")).unwrap(), "old\n");
        assert_eq!(app.pending_prompts.len(), 1);
        assert_eq!(app.pending_prompts[0].0, "session");
        assert!(app.pending_prompts[0].1.contains("Do not re-apply"));
        assert!(app.pending_prompts[0].1.contains("Why: Please keep the old behavior"));
    }

    #[test]
    fn queued_diff_rejection_refuses_stale_patch_without_sending_feedback() {
        let dir = tempfile::tempdir().unwrap();
        let git = |args: &[&str]| assert!(std::process::Command::new("git").args(args)
            .current_dir(dir.path()).status().unwrap().success());
        git(&["init", "-q"]);
        std::fs::write(dir.path().join("tracked.txt"), "old\n").unwrap();
        git(&["add", "tracked.txt"]);
        git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: baseline"]);
        std::fs::write(dir.path().join("tracked.txt"), "new\n").unwrap();
        let snapshot = diff_view::read(dir.path());
        let mut app = App::default();
        app.groups[0].tabs.push("session".into());
        app.session_cwds.insert("session".into(), dir.path().to_path_buf());
        app.diff_target = Some("session".into());
        app.diff_scroll = snapshot.rejectable[0].row;
        app.diff_snapshot = Some(snapshot);
        app.session_activity.insert("session".into(), (true, 0));
        app.begin_diff_reject();
        app.handle(Event::Paste(format!("{}\n\u{1b}", "x".repeat(2000))));
        assert_eq!(app.diff_reject_confirm.as_ref().unwrap().reason.len(), MAX_REJECT_REASON_BYTES);
        app.confirm_diff_reject();
        assert_eq!(app.diff_reject_queue.len(), 1);
        std::fs::write(dir.path().join("tracked.txt"), "agent changed again\n").unwrap();
        app.session_activity.insert("session".into(), (false, 0));
        for _ in 0..100 {
            app.poll_diff();
            if app.diff_reject_pending.is_none() && app.diff_reject_queue.is_empty()
                && app.notice.contains("changed") { break; }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(std::fs::read_to_string(dir.path().join("tracked.txt")).unwrap(), "agent changed again\n");
        assert!(app.pending_prompts.is_empty());
        assert!(app.notice.contains("changed"));
    }

    #[test]
    fn diff_pane_keeps_the_active_prompt_and_restores_the_other_session() {
        let mut app = App::default();
        app.apply_update(DaemonUpdate::Upsert(Session { id: "first".into(), title: "First".into(),
            collection: "repo".into(), transcript: "active transcript".into(), status: "Ready".into() }));
        app.apply_update(DaemonUpdate::Upsert(Session { id: "second".into(), title: "Second".into(),
            collection: "repo".into(), transcript: "hidden transcript".into(), status: "Ready".into() }));
        app.groups[1].tabs.push("second".into());
        app.handle(Event::Resize(100, 28));
        app.handle(Event::Key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE)));
        assert!(app.diff_pane);
        app.diff_text = "Base: HEAD\n@@ -1 +1 @@\n-old\n+new".into();
        let view = painted(&app);
        assert!(view.contains("active transcript"));
        assert!(view.contains("Worktree diff"));
        assert!(view.contains("+new"));
        assert!(!view.contains("hidden transcript"));
        let diff_area = app.layout(app.size).panes.unwrap()[1];
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::ScrollDown,
            column: diff_area.x + 2, row: diff_area.y + 2, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.active_group, 0);
        assert_eq!(app.diff_scroll, 3);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE)));
        assert_eq!(app.input, "x");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::ALT)));
        assert!(app.stop_confirmation.is_none());
        assert!(app.notice.contains("activity is unknown") || app.notice.contains("Load the diff"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::F(4), KeyModifiers::NONE)));
        assert!(!app.diff_pane);
        assert!(painted(&app).contains("hidden transcript"));
    }

    #[test]
    fn diff_navigation_jumps_files_and_hunks_without_editing_prompt() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.diff_pane = true;
        app.diff_text = "Base: HEAD\ndiff --git a/one b/one\n@@ first\n line\n@@ second\ndiff --git a/two b/two\n@@ third".into();
        app.diff_files = vec![1, 5];
        app.diff_hunks = vec![2, 4, 6];
        app.input = "draft".into();
        let alt = KeyModifiers::ALT;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('j'), alt)));
        assert_eq!(app.diff_scroll, 2);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('j'), alt)));
        assert_eq!(app.diff_scroll, 4);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('n'), alt)));
        assert_eq!(app.diff_scroll, 5);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('k'), alt)));
        assert_eq!(app.diff_scroll, 4);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('b'), alt)));
        assert_eq!(app.diff_scroll, 1);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('b'), alt)));
        assert_eq!(app.diff_scroll, 1);
        assert!(app.notice.contains("No previous file"));
        assert_eq!(app.input, "draft");

        app.diff_modal = true;
        app.diff_scroll = 0;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::NONE)));
        assert_eq!(app.diff_scroll, 1);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('j'), KeyModifiers::NONE)));
        assert_eq!(app.diff_scroll, 2);
        app.diff_hunks.push(70_000);
        app.diff_scroll = 6;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('j'), KeyModifiers::NONE)));
        assert_eq!(app.diff_scroll, 70_000);
        app.load_diff();
        assert!(app.diff_files.is_empty());
        assert!(app.diff_hunks.is_empty());
    }

    #[test]
    fn live_diff_refresh_preserves_scroll_and_waits_for_rejection_decision() {
        let mut app = App::default();
        app.apply_update(DaemonUpdate::Upsert(Session { id: "session".into(), title: "Session".into(),
            collection: "repo".into(), transcript: String::new(), status: "Ready".into() }));
        let cwd = PathBuf::from("/owned/repo");
        app.session_cwds.insert("session".into(), cwd.clone());
        app.diff_pane = true;
        app.diff_target = Some("session".into());
        app.diff_snapshot = Some(diff_view::DiffSnapshot::message("old\nline\nend".into()));
        app.diff_text = "old\nline\nend".into();
        app.diff_scroll = 2;
        app.diff_reject_confirm = Some(RejectDraft { index: 0, reason: "keep reading".into() });
        let (tx, rx) = mpsc::sync_channel(1);
        app.diff_pending = Some(("session".into(), cwd, rx));
        tx.send(diff_view::DiffSnapshot::message("new\nline\nend\nextra".into())).unwrap();
        assert!(!app.poll_diff());
        assert_eq!(app.diff_text, "old\nline\nend");
        assert_eq!(app.diff_reject_confirm.as_ref().unwrap().reason, "keep reading");
        app.diff_reject_confirm = None;
        assert!(app.poll_diff());
        assert_eq!(app.diff_text, "new\nline\nend\nextra");
        assert_eq!(app.diff_scroll, 2);
        assert!(app.diff_refresh_due.is_some());

        app.diff_refresh_due = Some(Instant::now() - Duration::from_millis(1));
        assert!(!app.poll_diff());
        assert!(app.diff_pending.is_some());

        let (tx, rx) = mpsc::sync_channel(1);
        app.diff_pending = Some(("session".into(), PathBuf::from("/owned/repo"), rx));
        tx.send(diff_view::DiffSnapshot::message("new\nline\nend\nextra".into())).unwrap();
        assert!(!app.poll_diff());
        assert_eq!(app.diff_scroll, 2);
    }

    #[test]
    fn open_diff_refreshes_after_a_tracked_file_changes() {
        let dir = tempfile::tempdir().unwrap();
        let git = |args: &[&str]| assert!(std::process::Command::new("git").args(args)
            .current_dir(dir.path()).status().unwrap().success());
        git(&["init", "-q"]);
        std::fs::write(dir.path().join("tracked.txt"), "old\n").unwrap();
        git(&["add", "tracked.txt"]);
        git(&["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "test: baseline"]);
        let mut app = App::default();
        app.groups[0].tabs.push("session".into());
        app.session_cwds.insert("session".into(), dir.path().to_path_buf());
        app.diff_modal = true;
        app.load_diff();
        for _ in 0..100 {
            app.poll_diff();
            if app.diff_pending.is_none() { break; }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(app.diff_pending.is_none());
        assert!(app.diff_text.contains("No tracked changes"));

        std::fs::write(dir.path().join("tracked.txt"), "new\n").unwrap();
        app.diff_refresh_due = Some(Instant::now() - Duration::from_millis(1));
        app.poll_diff();
        for _ in 0..100 {
            app.poll_diff();
            if app.diff_text.contains("+new") { break; }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(app.diff_text.contains("+new"));
        assert!(app.diff_snapshot.as_ref().is_some_and(|snapshot| !snapshot.rejectable.is_empty()));
    }

    #[test]
    fn diff_block_mouse_actions_comment_and_reject_exact_hunks() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.apply_update(DaemonUpdate::Upsert(Session { id: "session".into(), title: "Session".into(),
            collection: "repo".into(), transcript: String::new(), status: "Ready".into() }));
        app.session_cwds.insert("session".into(), dir.path().to_path_buf());
        app.session_activity.insert("session".into(), (false, 0));
        app.diff_pane = true;
        app.diff_target = Some("session".into());
        let patch = "Base: HEAD\ndiff --git a/one b/one\n--- a/one\n+++ b/one\n@@ -1 +1 @@\n-old\n+new\ndiff --git a/two b/two\n--- a/two\n+++ b/two\n@@ -1 +1 @@\n-before\n+after\nUntracked files (names only; contents are not read):\nloose.txt";
        app.diff_snapshot = Some(diff_view::DiffSnapshot::test_patch(patch, dir.path().to_path_buf(),
            vec![1, 7], vec![4, 10]));
        app.diff_text = patch.into();
        let pane = app.layout(app.size).panes.unwrap()[(app.active_group + 1) % 2];
        let pointer = |kind, row| Event::Mouse(MouseEvent { kind, column: pane.x + 5,
            row: pane.y + 1 + row, modifiers: KeyModifiers::NONE });
        app.handle(pointer(MouseEventKind::Moved, 6));
        assert_eq!(app.diff_hover, Some(0));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: pane.x.saturating_sub(1), row: pane.y + 7, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.diff_hover, None);
        app.handle(pointer(MouseEventKind::Moved, 6));
        app.handle(pointer(MouseEventKind::Down(MouseButton::Left), 6));
        assert_eq!(app.diff_selected, Some(0));
        let view = painted(&app);
        assert!(view.contains("[Reject]"));
        assert!(view.contains("[Comment]"));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: pane.x + 12, row: pane.bottom() - 2, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.diff_comment_draft.as_ref().map(|draft| draft.index), Some(0));
        app.handle(Event::Paste("Please simplify this".into()));
        let rail = app.layout(app.size).rail.unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: rail.x + 2, row: rail.y + 1, modifiers: KeyModifiers::NONE }));
        assert!(app.diff_comment_draft.is_some());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.diff_comment_draft.is_none());
        assert_eq!(app.pending_prompts.len(), 1);
        assert!(app.pending_prompts[0].1.contains("Please simplify this"));
        assert!(app.pending_prompts[0].1.contains("-old\n+new"));

        app.handle(pointer(MouseEventKind::Down(MouseButton::Left), 7));
        assert_eq!(app.diff_selected, Some(1));
        app.handle(pointer(MouseEventKind::Down(MouseButton::Left), 12));
        assert_eq!(app.diff_selected, Some(1));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: pane.x + 2, row: pane.bottom() - 2, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.diff_reject_confirm.as_ref().map(|draft| draft.index), Some(1));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(app.diff_reject_confirm.is_none());
        app.handle(pointer(MouseEventKind::Down(MouseButton::Left), 14));
        assert_eq!(app.diff_selected, None);
    }

    #[test]
    fn modal_diff_click_actions_and_refresh_wait_for_comment() {
        let dir = tempfile::tempdir().unwrap();
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.apply_update(DaemonUpdate::Upsert(Session { id: "session".into(), title: "Session".into(),
            collection: "repo".into(), transcript: String::new(), status: "Ready".into() }));
        let cwd = dir.path().to_path_buf();
        app.session_cwds.insert("session".into(), cwd.clone());
        app.diff_modal = true;
        app.diff_target = Some("session".into());
        let patch = "Base: HEAD\ndiff --git a/one b/one\n--- a/one\n+++ b/one\n@@ -1 +1 @@\n-old\n+new";
        app.diff_snapshot = Some(diff_view::DiffSnapshot::test_patch(patch, cwd.clone(), vec![1], vec![4]));
        app.diff_text = patch.into();
        let modal = App::diff_modal_area(app.size).unwrap();
        let mouse = |kind, column, row| Event::Mouse(MouseEvent { kind, column, row,
            modifiers: KeyModifiers::NONE });
        app.handle(mouse(MouseEventKind::Moved, modal.x + 5, modal.y + 6));
        assert_eq!(app.diff_hover, Some(0));
        app.handle(mouse(MouseEventKind::Down(MouseButton::Left), modal.x + 5, modal.y + 6));
        assert_eq!(app.diff_selected, Some(0));
        assert!(painted(&app).contains("[Comment]"));
        app.handle(mouse(MouseEventKind::Down(MouseButton::Left), modal.x + 12, modal.bottom() - 2));
        assert!(app.diff_comment_draft.is_some());
        let (tx, rx) = mpsc::sync_channel(1);
        app.diff_pending = Some(("session".into(), cwd, rx));
        tx.send(diff_view::DiffSnapshot::message("changed".into())).unwrap();
        assert!(!app.poll_diff());
        assert_eq!(app.diff_text, patch);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(app.poll_diff());
        assert_eq!(app.diff_text, "changed");
        assert_eq!(app.diff_selected, None);
    }

    #[test]
    fn selecting_bottom_diff_row_keeps_it_visible_above_actions() {
        let mut app = App::default();
        app.handle(Event::Resize(40, 12));
        app.diff_modal = true;
        let patch = "Base: HEAD\ndiff --git a/one b/one\n--- a/one\n+++ b/one\n@@ -1 +1 @@\n-old\n+new";
        app.diff_snapshot = Some(diff_view::DiffSnapshot::test_patch(patch, PathBuf::from("/repo"),
            vec![1], vec![4]));
        app.diff_text = patch.into();
        let modal = App::diff_modal_area(app.size).unwrap();
        assert_eq!(app.diff_window(modal), (6, 0));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: modal.x + 5, row: modal.bottom() - 2, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.diff_selected, Some(0));
        assert_eq!(app.diff_window(modal), (5, 1));
    }

    #[test]
    fn long_transcript_window_reaches_both_ends() {
        let lines: Vec<Line<'static>> = (0..70_000).map(|i| Line::from(i.to_string())).collect();
        let (window, top) = transcript_window(&lines, 8, 0, None);
        assert_eq!(window.len(), 8);
        assert_eq!(top, 69_992);
        assert_eq!(window[7].to_string(), "69999");
        let (window, top) = transcript_window(&lines, 8, 69_992, None);
        assert_eq!(top, 0);
        assert_eq!(window[0].to_string(), "0");
        assert_eq!(window[7].to_string(), "7");
    }

    #[test]
    fn transcript_scroll_crosses_u16_boundary_without_losing_lines() {
        let lines: Vec<Line<'static>> = (0..70_000).map(|i| Line::from(i.to_string())).collect();
        for scroll in [0, 1, 4_457, 65_535, 65_536, 69_991, 69_992] {
            let (window, _) = transcript_window(&lines, 8, scroll, None);
            assert_eq!(window.len(), 8);
            assert_eq!(window[0].to_string(), (69_992 - scroll).to_string());
        }
        let mut app = App {
            focus: Focus::Transcript,
            ..Default::default()
        };
        app.groups[0].scroll = usize::from(u16::MAX);
        assert!(app.handle(Event::Key(KeyEvent::new(
            KeyCode::PageUp,
            KeyModifiers::NONE
        ))));
        assert_eq!(app.groups[0].scroll, usize::from(u16::MAX) + 5);
    }

    #[test]
    fn incomplete_roster_retries_unchanged_layout_after_gate_opens() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = crate::ui_state::UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
        let mut app = App::default();
        app.groups[0].tabs.push("one".into());
        let mut saved = crate::ui_state::LayoutSignature::capture(&app);
        app.rail_width = 32;
        let complete = Mutex::new(false);
        assert!(save_layout_if_changed(
            &mut app, &mut store, &complete, &mut saved
        ));
        assert_ne!(saved, crate::ui_state::LayoutSignature::capture(&app));
        assert!(!store.path().exists());
        *complete.lock().unwrap() = true;
        assert!(
            save_layout_if_changed(&mut app, &mut store, &complete, &mut saved),
            "{}",
            app.notice
        );
        assert_eq!(saved, crate::ui_state::LayoutSignature::capture(&app));
        assert!(app.notice.is_empty());
        let written: serde_json::Value =
            serde_json::from_slice(&std::fs::read(store.path()).unwrap()).unwrap();
        assert_eq!(written["rust_ui"]["rail_width"], 32);
    }

    #[test]
    fn failed_layout_write_keeps_signature_pending() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = crate::ui_state::UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
        let mut app = App::default();
        app.groups[0].tabs.push("one".into());
        let mut saved = crate::ui_state::LayoutSignature::capture(&app);
        app.rail_width = 33;
        std::fs::create_dir_all(store.path()).unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(
            store.path().parent().unwrap(),
            std::fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        let complete = Mutex::new(true);
        assert!(save_layout_if_changed(
            &mut app, &mut store, &complete, &mut saved
        ));
        assert_ne!(saved, crate::ui_state::LayoutSignature::capture(&app));
        std::fs::remove_dir(store.path()).unwrap();
        assert!(
            save_layout_if_changed(&mut app, &mut store, &complete, &mut saved),
            "{}",
            app.notice
        );
        assert_eq!(saved, crate::ui_state::LayoutSignature::capture(&app));
        assert!(app.notice.is_empty());
        let written: serde_json::Value =
            serde_json::from_slice(&std::fs::read(store.path()).unwrap()).unwrap();
        assert_eq!(written["rust_ui"]["rail_width"], 33);
    }

    #[test]
    fn disconnected_control_queue_rejects_only_its_owned_requests() {
        let (sender, receiver) = mpsc::sync_channel(1); drop(receiver);
        let mut app = App::default();
        for id in ["a", "b"] {
            app.apply_daemon_frame(&json!({"type":"hello","session_id":id,"engine":"claude","effort":"high"}));
        }
        app.groups[0].tabs = vec!["b".into()];
        app.notice = "B notice".into(); app.input = "B draft".into(); app.input_cursor = app.input.len();
        app.pending_effort_verifications.insert("b".into(), "low".into()); // already admitted to its provider
        app.pending_effort_verifications.insert("a".into(), "max".into());
        app.pending_model_changes.push(("a".into(), "opus".into()));
        app.pending_permission_changes.push(("a".into(), "plan".into()));
        app.pending_effort_changes.push(("a".into(), "max".into()));
        assert!(dispatch_model_controls(&mut app, &sender));
        assert_eq!(app.notice, "B notice"); assert_eq!(app.input, "B draft");
        assert_eq!(app.pending_effort_verifications["b"], "low");
        assert!(!app.pending_effort_verifications.contains_key("a"));
        assert_eq!(app.session_efforts["a"], "high");
        assert!(app.pending_model_changes.is_empty() && app.pending_permission_changes.is_empty() && app.pending_effort_changes.is_empty());
        app.pending_permission_changes.push(("b".into(), "plan".into()));
        assert!(dispatch_model_controls(&mut app, &sender));
        assert!(app.notice.contains("Permission change failed"));
    }

    #[test]
    fn disconnected_attach_clears_marker_and_model_catalog_can_retry() {
        let (sender, receiver) = mpsc::sync_channel(1);
        drop(receiver);
        let mut app = App::default();
        app.attach_selected("session");
        assert!(dispatch_attaches(&mut app, &sender));
        assert!(app.attaching_ids.is_empty());
        app.attach_selected("session");
        assert_eq!(app.pending_attaches.len(), 1);
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"session","engine":"claude","can_set_model":true}));
        app.model_picker = Some(ModelPicker { session_id: "session".into(), models: Vec::new(),
            selected: 0, note: "Loading".into(), loading: true, catalog_pending: false });
        app.pending_model_queries.push("session".into());
        assert!(dispatch_model_controls(&mut app, &sender));
        assert!(!app.model_picker.as_ref().unwrap().loading);
        assert!(app.model_picker_key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE)));
        assert_eq!(app.pending_model_queries, ["session"]);
    }

    #[test]
    fn restoring_saved_layout_clears_obsolete_archived_tab_notice() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = crate::ui_state::UiStateStore::new(dir.path(), "/repo", "machine").unwrap();
        let mut app = App::default();
        app.groups[0].tabs.push("live".into());
        let mut saved = crate::ui_state::LayoutSignature::capture(&app);
        let complete = Mutex::new(true);
        app.offline_ids.insert("archive".into());
        app.groups[0].tabs.push("archive".into());
        assert!(save_layout_if_changed(&mut app, &mut store, &complete, &mut saved));
        assert!(app.notice.contains("archived tabs"));
        app.groups[0].tabs.pop();
        assert!(save_layout_if_changed(&mut app, &mut store, &complete, &mut saved));
        assert!(app.notice.is_empty());
    }

    #[test]
    fn full_worker_queue_keeps_prompts_in_order() {
        let (sender, receiver) = mpsc::sync_channel(1);
        sender
            .send(crate::bridge::WorkerCommand::Prompt(
                "s".into(),
                "first".into(),
            ))
            .unwrap();
        let mut app = App {
            pending_prompts: vec![("s".into(), "second".into()), ("s".into(), "third".into())],
            ..Default::default()
        };
        assert!(!dispatch_prompts(&mut app, &sender));
        assert_eq!(
            app.pending_prompts
                .iter()
                .map(|p| p.1.as_str())
                .collect::<Vec<_>>(),
            vec!["second", "third"]
        );
        assert!(
            matches!(receiver.recv().unwrap(), crate::bridge::WorkerCommand::Prompt(_, text) if text == "first")
        );
        assert!(!dispatch_prompts(&mut app, &sender));
        assert!(
            matches!(receiver.recv().unwrap(), crate::bridge::WorkerCommand::Prompt(_, text) if text == "second")
        );
        assert_eq!(app.pending_prompts[0].1, "third");
    }

    #[test]
    fn stream_heading_detection_respects_longer_outer_fences() {
        let prefix = "**You:**\n\nhello\n\n**Assistant:**\n\n";
        let source = format!("{prefix}````markdown\n\n```\n\n**You:**\n\n```\n\n````\n\nend");
        assert_eq!(streamed_turn_start(&source), Some(0));
        let next = format!("{source}\n\n**You:**\n\nNext turn");
        assert_eq!(streamed_turn_start(&next), next.rfind("**You:**"));
    }

    #[test]
    fn sending_answer_rows_do_not_select_options() {
        let request = InputRequest::from_event("a", &json!({"id":"r", "kind":"ask_user", "questions":[{"question":"Pick", "options":[{"label":"One"}]}]})).unwrap();
        let mut sending = request.clone();
        sending.sending = true;
        let menu = Rect::new(0, 0, 80, 15);
        for row in 1..14 { assert_eq!(input_request_option_at(&sending, menu, row), None); }
        assert!((1..14).any(|row| input_request_option_at(&request, menu, row) == Some(1)));
    }

    #[test]
    fn ended_session_clears_pending_input_and_answers() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"a"}));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"a",
            "event":{"type":"needs_input", "data":{"id":"request", "kind":"ask_user",
                "questions":[{"question":"Choose", "options":[{"label":"One"},{"label":"Two"}]}]}}}));
        app.pending_answers.push(("a".into(), "request".into(), json!({})));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"a", "event":{"type":"session_done", "data":{}}}));
        assert!(app.input_requests.is_empty());
        assert!(app.pending_answers.is_empty());
    }

    #[test]
    fn rejected_prompts_obey_input_bounds_and_unknown_streams_are_ignored() {
        let mut app = App::default();
        for kind in ["prompt_rejected", "prompt_uncertain"] {
            for text in ["x".repeat(MAX_INPUT_BYTES + 1), "bad\u{001b}draft".into()] {
                app.apply_daemon_frame(&json!({"type":kind,"session_id":"","text":text}));
            }
        }
        assert!(app.input.is_empty());
        assert!(app.rejected_drafts.is_empty());
        assert!(!app.append_reasoning("missing", &json!({"text":"secret"}), false));
        assert!(!app.append_event("missing", "tool_call", &json!({"name":"read"})));
        assert!(app.reasoning_streams.is_empty());
        assert!(app.streaming_text.is_empty());
    }

    #[test]
    fn finalized_session_preserves_selected_rail_identity() {
        let mut app = App::default();
        for id in ["a", "b", "c"] { app.apply_daemon_frame(&json!({"type":"hello", "session_id":id})); }
        app.rail_selected = app.rail_order().iter().position(|index| app.sessions[*index].id == "b").unwrap();
        app.apply_daemon_frame(&json!({"type":"clear_finalize_reply", "session_id":"a", "ok":true}));
        assert_eq!(app.sessions[app.rail_order()[app.rail_selected]].id, "b");
    }

    #[test]
    fn rejected_prompt_is_editable_and_does_not_replace_current_draft() {
        let mut app = App {
            input: "new draft".into(),
            ..Default::default()
        };
        app.apply_daemon_frame(
            &json!({"type":"prompt_rejected", "session_id":"", "text":"old prompt", "message":"queue full"}),
        );
        assert_eq!(app.input, "new draft");
        assert_eq!(app.rejected_drafts[""], ["old prompt"]);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)));
        assert_eq!(app.input, "old prompt");
        assert_eq!(app.rejected_drafts[""], ["new draft"]);
    }

    #[test]
    fn unconfirmed_prompt_requires_deliberate_recovery_before_retry() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"prompt_uncertain", "session_id":"", "text":"possibly sent",
            "message":"Prompt delivery unconfirmed"}));
        assert!(app.input.is_empty());
        assert_eq!(app.rejected_drafts[""], ["possibly sent"]);
        assert!(app.notice.contains("check session"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)));
        assert_eq!(app.input, "possibly sent");
    }

    #[test]
    fn background_session_rejection_does_not_replace_active_draft() {
        let mut app = App::default();
        app.groups[0].tabs.push("a".into());
        app.groups[1].tabs.push("b".into());
        app.input = "draft for a".into();
        app.apply_daemon_frame(&json!({"type":"prompt_rejected", "session_id":"b",
            "text":"draft for b", "message":"queue full"}));
        assert_eq!(app.input, "draft for a");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)));
        assert_eq!(app.input, "draft for a");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::ALT)));
        assert!(app.input.is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::ALT)));
        assert_eq!(app.input, "draft for b");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::ALT)));
        assert_eq!(app.input, "draft for a");
    }

    #[test]
    fn untagged_event_or_rejection_cannot_change_the_active_session() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"a", "model":"model"}));
        let original = app.sessions[0].transcript.clone();
        assert!(!app.apply_daemon_frame(&json!({"type":"event",
            "event":{"type":"text_delta", "data":{"text":"wrong session"}}})));
        assert!(!app.apply_daemon_frame(&json!({"type":"prompt_rejected", "text":"wrong draft"})));
        assert_eq!(app.sessions[0].transcript, original);
        assert!(app.rejected_drafts.is_empty());
    }

    #[test]
    fn queued_reply_and_broadcast_count_one_prompt() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"a", "queued":0}));
        app.apply_daemon_frame(&json!({"type":"reply", "session_id":"a", "ok":true, "queued":true}));
        let prompt = format!("{} visible ending", "a".repeat(500));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"a",
            "event":{"type":"prompt_queued", "data":{"id":"q", "text":prompt}}}));
        assert_eq!(app.session_activity["a"].1, 1);
        assert!(app.sessions[0].transcript.contains("visible ending"));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"a",
            "event":{"type":"prompt_dequeued", "data":{"id":"q"}}}));
        assert_eq!(app.session_activity["a"].1, 0);
    }

    #[test]
    fn streamed_chunks_stay_together_and_turns_have_separate_headings() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s"}));
        let event = |kind: &str, data: serde_json::Value| json!({"type":"event", "session_id":"s",
            "event":{"type":kind, "data":data}});
        app.apply_daemon_frame(&event("turn_started", json!({"prompt":"first\nquestion"})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":"answer"})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":" one"})));
        app.apply_daemon_frame(&event("turn_done", json!({"is_error":false})));
        app.apply_daemon_frame(&event("turn_started", json!({"prompt":"second"})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":"answer two"})));
        assert_eq!(app.sessions[0].transcript,
            "**You:**\n\nfirst\nquestion\n\n**Assistant:**\n\nanswer one\n\n**You:**\n\nsecond\n\n**Assistant:**\n\nanswer two");
    }

    #[test]
    fn tool_first_turn_gets_one_assistant_heading() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s"}));
        let event = |kind: &str, data: serde_json::Value| json!({"type":"event", "session_id":"s",
            "event":{"type":kind, "data":data}});
        app.apply_daemon_frame(&event("turn_started", json!({"prompt":"inspect"})));
        app.apply_daemon_frame(&event("tool_call", json!({"name":"Read", "input":{"path":"file.rs"}})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":"Found it."})));
        let transcript = &app.sessions[0].transcript;
        assert_eq!(transcript.matches("**Assistant:**").count(), 1);
        assert!(transcript.contains("Tool: Read started"));
        assert!(transcript.ends_with("Found it."));
    }

    #[test]
    fn restored_running_turn_gets_an_answer_boundary_without_replayed_start() {
        let mut app = App::default();
        app.apply_update(DaemonUpdate::Upsert(Session { id: "s".into(), title: "s".into(),
            collection: "repo".into(), transcript: "**You:**\n\nquestion\n\n".into(), status: "Running".into() }));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"s",
            "event":{"type":"text_delta", "data":{"text":"answer"}}}));
        assert!(app.sessions[0].transcript.ends_with("\n\n**Assistant:**\n\nanswer"));
    }

    #[test]
    fn restored_snapshot_keeps_its_existing_turn_headings() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s"}));
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"s",
            "event":{"type":"text_delta", "data":{"text":"**You:**\n\nprior\n\n**Assistant:**\n\nreply\n\n",
                "snapshot":true}}}));
        assert_eq!(app.sessions[0].transcript, "**You:**\n\nprior\n\n**Assistant:**\n\nreply\n\n");
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"s",
            "event":{"type":"text_delta", "data":{"text":"continued"}}}));
        assert!(app.sessions[0].transcript.ends_with("**Assistant:**\n\ncontinued"));
    }

    #[test]
    fn nested_tool_calls_support_keyboard_mouse_hover_and_streamed_details() {
        use transcript_tools::FoldKey;
        let mut app = App::default();
        app.handle(Event::Resize(100, 32));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s"}));
        let event = |kind: &str, data: serde_json::Value| json!({"type":"event", "session_id":"s", "event":{"type":kind,"data":data}});
        app.apply_daemon_frame(&event("turn_started", json!({"prompt":"inspect"})));
        for (id, name, input) in [("one", "Read", "first secret"), ("two", "Write", "second secret")] {
            app.apply_daemon_frame(&event("tool_call", json!({"id":id,"name":name,"input":input})));
        }
        app.focus = Focus::Transcript;
        painted(&app);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        let frame = painted(&app);
        assert!(frame.contains("Read · running") && frame.contains("Write · running"));
        assert!(!frame.contains("first secret") && !frame.contains("second secret"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char(']'), KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char(' '), KeyModifiers::NONE)));
        assert!(painted(&app).contains("first secret"));
        assert!(!painted(&app).contains("second secret"));
        app.apply_daemon_frame(&event("tool_result_detail", json!({"id":"one","text":"streamed first result"})));
        assert!(painted(&app).contains("streamed first result"));
        let hit = app.visible_tool_sections.borrow().iter()
            .find(|(_, _, _, key)| *key == FoldKey::Tool("two".into()))
            .map(|(rect, _, _, _)| *rect).unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: hit.x + 1, row: hit.y, modifiers: KeyModifiers::NONE }));
        assert_eq!(app.tool_section_hover, Some(("s".into(), FoldKey::Tool("two".into()))));
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: hit.x + 1, row: hit.y, modifiers: KeyModifiers::NONE }));
        let frame = painted(&app);
        assert!(frame.contains("first secret") && frame.contains("second secret"));
        assert!(app.expanded_tool_sections["s"].contains(&FoldKey::Tool("one".into())));
        assert!(app.expanded_tool_sections["s"].contains(&FoldKey::Tool("two".into())));
    }

    #[test]
    fn live_tool_activity_stays_between_latest_response_and_spinner() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s"}));
        let event = |kind: &str, data: serde_json::Value| json!({"type":"event", "session_id":"s", "event":{"type":kind,"data":data}});
        app.apply_daemon_frame(&event("turn_started", json!({"prompt":"inspect"})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":"First response"})));
        app.apply_daemon_frame(&event("tool_call", json!({"name":"Read","input":{}})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":"Latest response"})));
        for expanded in [false, true] {
            if expanded { app.expanded_tool_sections.insert("s".into(), HashSet::from([transcript_tools::FoldKey::Section(0)])); }
            let frame = painted(&app);
            assert!(frame.find("Latest response").unwrap() < frame.find("1 tool call").unwrap());
            assert!(frame.find("1 tool call").unwrap() < frame.find("Processing").unwrap());
        }
        let source = "**You:**\n\nInspect\n\nTool: Read started\n\n**Assistant:**\n\nAnswer";
        let mut cached = RenderedTranscript::render(0,"s", source, 80,None,None,0,&[]);
        let appended = format!("{source} continued");
        cached.update(&appended,80,None,None,0,&[]);
        let (fresh, sections) = transcript_tools::render(&appended,80,None,None);
        assert_eq!(cached.lines,fresh);
        assert_eq!(cached.sections,sections);
    }

    #[test]
    fn processing_spinner_advances_only_for_visible_busy_sessions() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 28));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "running":true}));
        let start = app.spinner_at;
        assert_eq!(app.activity_label("s"), Some("Processing"));
        assert!(painted(&app).contains("◐ Processing"));
        assert!(!app.tick_spinner(start + Duration::from_millis(119)));
        assert!(app.tick_spinner(start + SPINNER_INTERVAL));
        assert!(painted(&app).contains("◓ Processing"));
        app.session_activity.insert("s".into(), (false, 1));
        assert_eq!(app.activity_label("s"), Some("Queued"));
        assert!(painted(&app).contains("Queued"));
        app.session_activity.insert("s".into(), (false, 0));
        assert!(!app.tick_spinner(start + SPINNER_INTERVAL * 2));
        assert!(!painted(&app).contains("Processing"));
    }

    #[test]
    fn streamed_turn_cache_matches_full_render_after_each_append() {
        let mut source = "**You:**\n\nFirst\n\n**Assistant:**\n\nEarlier answer\n\n**You:**\n\nNext\n\n**Assistant:**\n\n".to_owned();
        let mut cached = RenderedTranscript::render(0, "s", &source, 38, None, None, 0, &[]);
        assert!(cached.turn_start.is_some());
        for chunk in ["A line", " with more text", "\n\nA second paragraph", "\n\n```rust\nfn main() {}\n```", "\n\nDone"] {
            source.push_str(chunk);
            cached.update(&source, 38, None, None, 0, &[]);
            let (expected, sections) = transcript_tools::render(&source, 38, None, None);
            assert_eq!(cached.lines, expected);
            assert_eq!(cached.sections, sections);
        }
    }

    #[test]
    fn streamed_turn_cache_preserves_prior_tool_sections() {
        let mut source = "**You:**\n\nInspect\n\n**Assistant:**\n\nTool: Read started · file.rs\n\nTool: Read finished · ok\n\n**You:**\n\nSummarize\n\n**Assistant:**\n\n".to_owned();
        let expanded = HashSet::from([transcript_tools::FoldKey::Section(0)]);
        let mut cached = RenderedTranscript::render(0, "s", &source, 50, Some(&expanded), Some(transcript_tools::FoldKey::Section(0)), 0, &[]);
        for chunk in ["Summary", " with more detail", "\n\nFinal paragraph"] {
            source.push_str(chunk);
            cached.update(&source, 50, Some(&expanded), Some(transcript_tools::FoldKey::Section(0)), 0, &[]);
            let (expected, sections) = transcript_tools::render(&source, 50, Some(&expanded), Some(transcript_tools::FoldKey::Section(0)));
            assert_eq!(cached.lines, expected);
            assert_eq!(cached.sections, sections);
        }
    }

    #[test]
    fn streamed_tool_reasoning_cache_matches_full_render() {
        use transcript_tools::FoldKey;
        let mut source = "**You:**\n\nEarlier\n\n**Assistant:**\n\nTool: Read started · old\n\n**You:**\n\nNext\n\n**Assistant:**\n\n".to_owned();
        let expanded = HashSet::from([FoldKey::Section(0), FoldKey::Section(1), FoldKey::Section(2), FoldKey::Tool("legacy:Section(2):0".into())]);
        let selected = Some(FoldKey::Tool("legacy:Section(2):0".into()));
        let mut cached = RenderedTranscript::render(0, "s", &source, 38, Some(&expanded), selected.clone(), 0, &[]);
        for chunk in [
            "\u{001e}DOXA_REASONING:{\"text\":\"Thinking\",\"tokens\":3,\"streaming\":false}\n\n",
            "Tool: Read started · new\n\n", "Response [link](https://example.com)", " with more text", "\n\n```rust\nfn main() {}\n```", "\n\nDone",
        ] {
            source.push_str(chunk);
            cached.update(&source, 38, Some(&expanded), selected.clone(), 0, &[]);
            let (lines, sections, links) = transcript_tools::render_with_links(&source, 38, Some(&expanded), selected.clone(), &[]);
            assert_eq!(cached.lines, lines);
            assert_eq!(cached.sections, sections);
            assert_eq!(cached.links, links);
        }
    }

    #[test]
    #[ignore = "manual deterministic render throughput measurement"]
    fn streamed_tool_render_throughput() {
        let history = (0..3000).map(|_| "**You:**\n\nEarlier question\n\n**Assistant:**\n\nEarlier answer with **markdown** and a [link](https://example.com).\n\n").collect::<String>();
        let mut source = format!("{history}**You:**\n\nNext\n\n**Assistant:**\n\nTool: Read started · file.rs\n\n");
        let mut cached = RenderedTranscript::render(0, "s", &source, 80, None, None, 0, &[]);
        let start = Instant::now();
        for _ in 0..200 { source.push_str(" streamed"); cached.update(&source, 80, None, None, 0, &[]); }
        let incremental = start.elapsed();
        source.truncate(source.len() - 200 * " streamed".len());
        let start = Instant::now();
        for _ in 0..200 { source.push_str(" streamed"); let _ = transcript_tools::render_with_links(&source, 80, None, None, &[]); }
        eprintln!("200 tool-turn delta paints: cached={incremental:?}, full={:?}, history={} bytes", start.elapsed(), history.len());
    }

    #[test]
    fn rendered_transcript_invalidates_on_width_and_expansion() {
        let source = "**Assistant:**\n\nTool: Read started · file.rs\n\nTool: Read finished · ok";
        let mut cached = RenderedTranscript::render(0, "s", source, 60, None, None, 0, &[]);
        let expanded = HashSet::from([transcript_tools::FoldKey::Section(0)]);
        cached.update(source, 24, Some(&expanded), Some(transcript_tools::FoldKey::Section(0)), 0, &[]);
        let (expected, sections) = transcript_tools::render(source, 24, Some(&expanded), Some(transcript_tools::FoldKey::Section(0)));
        assert_eq!(cached.lines, expected);
        assert_eq!(cached.sections, sections);
        assert_eq!(cached.width, 24);
        assert_eq!(cached.expanded.as_ref(), Some(&expanded));
        let mut cards = ToolCards::default();
        cards.record("s", "tool_call", &json!({"id":"one","name":"Read","input":"file.rs"}));
        cards.record("s", "tool_result", &json!({"id":"one","name":"Read","result_summary":"updated"}));
        let identified = "**Assistant:**\n\nTool: Read started\u{001f}DOXA_TOOL_ID:\"one\"\n\nTool: Read finished\u{001f}DOXA_TOOL_ID:\"one\"";
        cached.update(identified, 24, Some(&expanded), Some(transcript_tools::FoldKey::Section(0)), 1, cards.for_session("s"));
        let (expected, sections) = transcript_tools::render_with_cards(
            identified, 24, Some(&expanded), Some(transcript_tools::FoldKey::Section(0)), cards.for_session("s"));
        assert_eq!(cached.lines, expected);
        assert_eq!(cached.sections, sections);
    }

    #[test]
    fn background_pane_update_keeps_other_panes_render_cache() {
        let mut app = App::default();
        app.handle(Event::Resize(140, 32));
        for id in ["left", "right"] {
            app.apply_daemon_frame(&json!({"type":"hello","session_id":id}));
        }
        app.groups[0].tabs = vec!["left".into()];
        app.groups[1].tabs = vec!["right".into()];
        app.active_group = 1;
        painted_at(&app, 140, 32);
        let (right_source, right_lines) = {
            let cache = app.rendered_transcripts.borrow();
            assert_eq!(cache.len(), 2);
            let right = cache.iter().find(|entry| entry.pane == 1).unwrap();
            (right.source.clone(), right.lines.as_ptr())
        };
        assert!(app.apply_daemon_frame(&json!({"type":"event","session_id":"left",
            "event":{"type":"text_delta","data":{"text":"Background update"}}})));
        painted_at(&app, 140, 32);
        let cache = app.rendered_transcripts.borrow();
        assert!(cache.iter().find(|entry| entry.pane == 0).unwrap().source.contains("Background update"));
        let right = cache.iter().find(|entry| entry.pane == 1).unwrap();
        assert_eq!(right.source, right_source);
        assert_eq!(right.lines.as_ptr(), right_lines);
        drop(cache);
        assert!(app.apply_daemon_frame(&json!({"type":"event","session_id":"left",
            "event":{"type":"tool_call","data":{"id":"call-1","name":"Read","input":"file.rs"}}})));
        painted_at(&app, 140, 32);
        let cache = app.rendered_transcripts.borrow();
        let right = cache.iter().find(|entry| entry.pane == 1).unwrap();
        assert_eq!(right.lines.as_ptr(), right_lines);
    }

    #[test]
    fn count_only_reasoning_is_folded_and_resets_for_next_turn() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s"}));
        let event = |kind:&str,data:serde_json::Value| json!({"type":"event","session_id":"s","event":{"type":kind,"data":data}});
        app.apply_daemon_frame(&event("turn_started",json!({"prompt":"first"})));
        app.apply_daemon_frame(&event("reasoning_progress",json!({"approx_tokens":48,"count_is_estimate":true})));
        let (live,sections)=transcript_tools::render(&app.sessions[0].transcript,80,None,None);
        assert_eq!(sections.len(),1);
        assert!(live.iter().any(|line|line.to_string().contains("~48 tokens · receiving")));
        assert!(app.reasoning_streams["s"].text.is_empty());
        app.apply_daemon_frame(&event("turn_done",json!({"is_error":false})));
        app.apply_daemon_frame(&event("turn_started",json!({"prompt":"second"})));
        app.apply_daemon_frame(&event("reasoning_progress",json!({"approx_tokens":12,"count_is_estimate":true})));
        assert_eq!(app.reasoning_streams["s"].tokens,12);
        assert!(app.reasoning_streams["s"].text.is_empty());
        let (live,sections)=transcript_tools::render(&app.sessions[0].transcript,80,None,None);
        assert_eq!(sections.len(),2);
        assert!(live.iter().any(|line|line.to_string().contains("~12 tokens · receiving")));
    }
    #[test]
    fn streamed_reasoning_counts_live_then_reveals_only_on_expand() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s"}));
        let event = |kind: &str, data: serde_json::Value| json!({"type":"event", "session_id":"s",
            "event":{"type":kind,"data":data}});
        app.apply_daemon_frame(&event("turn_started", json!({"prompt":"check"})));
        app.apply_daemon_frame(&event("reasoning_progress", json!({"approx_tokens":37})));
        let transcript = &app.sessions[0].transcript;
        let (live, sections) = transcript_tools::render(transcript, 80, None, None);
        assert_eq!(sections.len(), 1);
        assert!(live.iter().any(|line| line.to_string().contains("~37 tokens · receiving")));
        app.apply_daemon_frame(&event("reasoning_delta", json!({"text":"scrubbed thought","approx_tokens":42,"final":true})));
        app.apply_daemon_frame(&event("text_delta", json!({"text":"Answer"})));
        app.apply_daemon_frame(&event("turn_done", json!({"is_error":false,"reasoning_output_tokens":7})));
        let transcript = &app.sessions[0].transcript;
        let (collapsed, _) = transcript_tools::render(transcript, 80, None, None);
        assert!(!collapsed.iter().any(|line| line.to_string().contains("scrubbed thought")));
        assert!(collapsed.iter().any(|line| line.to_string().contains("Thinking · 7 tokens")));
        assert!(!collapsed.iter().any(|line| line.to_string().contains("~7 tokens")));
        let (expanded, _) = transcript_tools::render(transcript, 80, Some(&HashSet::from([transcript_tools::FoldKey::Section(0)])), None);
        assert!(expanded.iter().any(|line| line.to_string().contains("scrubbed thought")));
        assert!(expanded.iter().any(|line| line.to_string().contains("Answer")));
        app.handle(Event::Resize(100, 28));
        painted(&app);
        let hit = app.visible_tool_sections.borrow().iter()
            .find(|(_, _, session, section)| session == "s" && *section == transcript_tools::FoldKey::Section(0))
            .map(|(rect, _, _, _)| *rect).unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: hit.x + 1, row: hit.y, modifiers: KeyModifiers::NONE }));
        assert!(app.expanded_tool_sections["s"].contains(&transcript_tools::FoldKey::Section(0)));
    }

    #[test]
    fn lore_picker_keeps_prompt_and_displays_only_read_results() {
        assert_eq!(visible_raw_line("x\ty\u{0000}z"), "x\\ty\\0z");
        assert_eq!(raw_visual_rows("界界", 3), vec!["界", "界"]);
        let mut app = App { input: "unsent draft".into(), ..Default::default() };
        app.lore_picker = Some(LorePicker {
            session_id: None,
            query: String::new(), filter_focused: false, rows: Vec::new(), selected: 0, offset: 0,
            evidence: None, status: String::new(), pending: None,
            proposals: Vec::new(), proposal_mode: false, cluster_mode: false, all_proposals: Vec::new(), review: None, review_scroll: 0,
            review_seen: 0, review_width: 0, armed_resolution: None,
            can_resolve: false, resolving: false, cwd: String::new(),
            belief_review: None, belief_intent: None, can_act_on_beliefs: false, belief_action: None,
            belief_note: String::new(), retract_armed: false, belief_acting: false,
            result_status: None,
        });
        let (tx, rx) = mpsc::sync_channel(1);
        app.lore_picker.as_mut().unwrap().pending = Some(rx);
        tx.send(Ok(lore_picker::ResultPage::Beliefs(vec![lore_picker::Belief {
            id: 7, subject: "user".into(), claim: "safe".into(), truncated: false,
            confidence: 0.8, evidence_count: Some(1), recency:None,
        }]))).unwrap();
        assert!(app.poll_lore());
        assert_eq!(app.lore_picker.as_ref().unwrap().rows[0].id, 7);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('/'), KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE)));
        assert_eq!(app.lore_picker.as_ref().unwrap().query, "r");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(app.lore_picker.is_none());
        assert_eq!(app.input, "unsent draft");
        assert!(app.pending_prompts.is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn belief_actions_require_full_exact_review_and_note_then_refresh_success() {
        let mut app = app_with_review();
        assert!(app.lore_picker.as_ref().unwrap().belief_review.as_ref().unwrap().claim().len() > 1200);
        assert!(painted(&app).contains("Exact belief #7"));
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        for _ in 0..100 {
            app.lore_picker_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
            let picker = app.lore_picker.as_ref().unwrap();
            let full = format!("Subject: {}\nClaim: {}", picker.belief_review.as_ref().unwrap().subject(),
                picker.belief_review.as_ref().unwrap().claim());
            if picker.review_seen == raw_visual_rows(&full, picker.review_width).len() { break; }
        }
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL));
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::ALT));
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::NONE));
        assert_eq!(app.lore_picker.as_ref().unwrap().belief_action, Some(doxa_lore::BeliefAction::Confirmed));
        app.lore_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().pending.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('o'), KeyModifiers::NONE));
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('k'), KeyModifiers::NONE));
        app.lore_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().resolving);
        assert!(app.lore_picker.as_ref().unwrap().pending.is_some());
        let (tx, rx) = mpsc::sync_channel(1);
        app.lore_picker.as_mut().unwrap().pending = Some(rx);
        tx.send(Ok(lore_picker::ResultPage::BeliefActed(doxa_lore::BeliefActionResult {
            status: doxa_lore::BeliefStatus::Active, retired: false,
            confirmed: 1, contradicted: 0, stale: 0,
        }))).unwrap();
        assert!(app.poll_lore());
        assert!(app.notice.contains("Belief action applied"));
        assert!(app.pending_queue_commands.iter().any(|command|
            matches!(command, crate::bridge::WorkerCommand::Status(id) if id == "s")));
        assert!(app.lore_picker.as_ref().unwrap().belief_review.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn belief_review_zero_height_cannot_scroll_or_lose_escape() {
        let mut app = app_with_review();
        app.sync_chooser_state();
        app.chooser_height_override.set(Some(5));
        app.lore_picker_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        let menu = app.active_chooser_rect().unwrap();
        app.mouse(MouseEvent { kind: MouseEventKind::ScrollDown,
            column: menu.x + 2, row: menu.y + 2, modifiers: KeyModifiers::NONE });
        assert_eq!(app.lore_picker.as_ref().unwrap().review_scroll, 0);
        assert_eq!(app.lore_picker.as_ref().unwrap().review_seen, 0);
        app.chooser_height_override.set(Some(0));
        app.lore_picker_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().belief_review.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn belief_action_completion_refreshes_its_original_session() {
        let mut app = app_with_review();
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"other","cwd":"/other"}));
        app.groups[0].tabs = vec!["s".into(), "other".into()];
        app.groups[0].active = 1;
        app.set_lore_memory_usage("s", 10, 100, 10, 100);
        app.set_lore_memory_usage("other", 20, 100, 20, 100);
        let (tx, rx) = mpsc::sync_channel(1);
        let picker = app.lore_picker.as_mut().unwrap();
        picker.pending = Some(rx);
        picker.resolving = true;
        picker.belief_acting = true;
        tx.send(Ok(lore_picker::ResultPage::BeliefActed(doxa_lore::BeliefActionResult {
            status: doxa_lore::BeliefStatus::Active, retired: false,
            confirmed: 1, contradicted: 0, stale: 0,
        }))).unwrap();
        assert!(app.poll_lore());
        assert!(!app.memory_cache.contains_key("s"));
        assert!(app.memory_cache.contains_key("other"));
        assert!(app.pending_queue_commands.iter().any(|command|
            matches!(command, crate::bridge::WorkerCommand::Status(id) if id == "s")));
        assert!(!app.pending_queue_commands.iter().any(|command|
            matches!(command, crate::bridge::WorkerCommand::Status(id) if id == "other")));
    }

    #[test]
    fn idle_memory_poll_does_not_force_redraw() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.session_cwds.insert("s".into(), PathBuf::from("/repo"));
        app.set_lore_memory_usage("s", 10, 100, 10, 100);
        assert!(!app.poll_memory());
        assert!(app.memory_pending.is_none());
    }

    #[test]
    fn memory_scope_change_redraws_even_when_usage_counts_are_equal() {
        let mut app = App::default();
        app.groups[0].tabs.push("s".into());
        app.session_cwds.insert("s".into(), PathBuf::from("/repo"));
        app.set_lore_memory_usage("s", 10, 100, 10, 100);
        let usage = app.memory_cache["s"].0.clone().unwrap();
        let (tx, rx) = mpsc::sync_channel(1);
        app.memory_pending = Some(("s".into(), "/repo".into(), rx));
        tx.send(Some((usage, false))).unwrap();
        assert!(app.poll_memory());
        assert_eq!(app.memory_repo.get("s"), Some(&false));
    }

    #[cfg(unix)]
    #[test]
    fn lore_footer_hover_never_selects_an_offscreen_row() {
        let mut app = app_with_review();
        let picker = app.lore_picker.as_mut().unwrap();
        picker.belief_review = None;
        picker.rows = (1..=30).map(|id| lore_picker::Belief { id, subject: "subject".into(),
            claim: "claim".into(), truncated: false, confidence: 0.8, evidence_count: Some(1), recency:None }).collect();
        let menu = app.active_chooser_rect().unwrap();
        let footer = menu.bottom().saturating_sub(1);
        app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: footer, modifiers: KeyModifiers::NONE });
        assert_eq!(app.lore_picker.as_ref().unwrap().selected, 0);
        let picker = app.lore_picker.as_mut().unwrap();
        picker.proposal_mode = true;
        picker.proposals = (1..=30).map(|index| lore_picker::Proposal { pid: index.to_string(),
            kind: "memory".into(), action: "add".into(), scope: "user".into(), summary: "summary".into(), source_session_id: None, source_project: None }).collect();
        let menu = app.active_chooser_rect().unwrap();
        let footer = menu.y + 4 + menu.height.saturating_sub(6);
        app.mouse(MouseEvent { kind: MouseEventKind::Moved,
            column: menu.x + 2, row: footer, modifiers: KeyModifiers::NONE });
        assert_eq!(app.lore_picker.as_ref().unwrap().selected, 0);
    }

    #[cfg(unix)]
    #[test]
    fn compact_evidence_menu_keeps_the_truncation_notice_visible() {
        let mut app = app_with_review();
        let picker = app.lore_picker.as_mut().unwrap();
        picker.belief_review = None;
        picker.evidence = Some((7, (0..2).map(|_| lore_picker::Evidence {
            session_id: "session".into(), project: "repo".into(), note: "note".into(),
            created: "today".into(), source_engine: None, truncated: false, trail_truncated: true,
        }).collect()));
        let mut terminal = Terminal::new(TestBackend::new(100, 8)).unwrap();
        terminal.draw(|frame| app.draw_lore_picker(frame, Rect::new(0, 0, 100, 8))).unwrap();
        let buffer = terminal.backend().buffer();
        let screen = (0..8).map(|row| (0..100).map(|x| buffer[(x, row)].symbol())
            .collect::<String>()).collect::<Vec<_>>().join("\n");
        assert!(screen.contains("More evidence exists in LORE"));
    }

    #[test]
    fn disconnected_memory_menu_does_not_replace_another_sessions_content() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 28);
        app.groups[0].tabs.push("other".into());
        app.session_cwds.insert("s".into(), PathBuf::from("/repo"));
        app.session_cwds.insert("other".into(), PathBuf::from("/other"));
        app.show_memory_menu_fixture(0, &["Keep this"], &[], &[]);
        let expected = app.chip_info.as_ref().unwrap().lines.clone();
        let (tx, rx) = mpsc::sync_channel(1);
        app.memory_menu_pending = Some(("s".into(), "/repo".into(), rx));
        drop(tx);
        assert!(!app.poll_memory_menu());
        assert_eq!(app.chip_info.as_ref().unwrap().lines, expected);
    }

    #[cfg(unix)]
    #[test]
    fn retract_needs_separate_confirmation_and_changed_selection_disables_actions() {
        let mut app = app_with_review();
        for _ in 0..100 { app.lore_picker_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)); }
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE));
        assert_eq!(app.lore_picker.as_ref().unwrap().belief_action, Some(doxa_lore::BeliefAction::Retract));
        for c in "obsolete".chars() { app.lore_picker_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)); }
        app.lore_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().retract_armed);
        assert!(app.lore_picker.as_ref().unwrap().pending.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('!'), KeyModifiers::NONE));
        assert!(!app.lore_picker.as_ref().unwrap().retract_armed);
        app.lore_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().retract_armed);
        app.lore_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().pending.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE));
        for c in "obsolete".chars() { app.lore_picker_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)); }
        app.lore_picker_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('Y'), KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().pending.is_some());
        app.lore_picker.as_mut().unwrap().pending = None;
        app.lore_picker.as_mut().unwrap().resolving = false;
        app.lore_picker.as_mut().unwrap().belief_acting = false;
        app.lore_picker.as_mut().unwrap().rows.push(lore_picker::Belief { id: 8, subject: "other".into(),
            claim: "other".into(), truncated: false, confidence: 0.7, evidence_count: Some(0), recency:None });
        app.lore_picker.as_mut().unwrap().selected = 1;
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::NONE));
        assert!(!app.lore_picker.as_ref().unwrap().can_act_on_beliefs);
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        assert!(app.lore_picker.as_ref().unwrap().status.contains("Selection changed"));
        app.lore_picker_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().belief_review.is_none());
    }

    #[test]
    fn belief_buttons_have_only_complete_rendered_hit_targets() {
        let buttons = [("[Accept]", KeyCode::Char('A')), ("[Reject]", KeyCode::Char('R'))];
        for width in 2..24 {
            let area = Rect::new(3, 4, width, 10);
            let hits = belief_buttons(area, 7, &buttons);
            for (rect, label, _) in &hits {
                assert_eq!(rect.width as usize, label.len());
                assert!(rect.x > area.x && rect.right() < area.right());
            }
            assert_eq!(hits.len(), if width >= 19 { 2 } else if width >= 10 { 1 } else { 0 });
        }
        assert!(belief_buttons(Rect::new(0, 0, 40, 5), 5, &buttons).is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn belief_reject_button_requires_complete_review_then_explicit_confirmation() {
        let mut app = app_with_review();
        app.belief_browser_fixture = true;
        let menu = app.active_chooser_rect().unwrap();
        let reject = belief_review_buttons(menu, app.lore_picker.as_ref().unwrap()).into_iter()
            .find(|(_, _, key)| *key == KeyCode::Char('R')).unwrap().0;
        let mouse = |kind| MouseEvent { kind, column: reject.x, row: reject.y, modifiers: KeyModifiers::NONE };
        app.mouse(mouse(MouseEventKind::Moved));
        assert_eq!(app.belief_button_hover, Some(reject));
        app.mouse(mouse(MouseEventKind::Down(MouseButton::Left)));
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        for _ in 0..100 { app.lore_picker_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)); }
        app.mouse(mouse(MouseEventKind::Down(MouseButton::Left)));
        let picker = app.lore_picker.as_ref().unwrap();
        assert_eq!(picker.belief_action, Some(doxa_lore::BeliefAction::Retract));
        assert!(picker.retract_armed);
        assert_eq!(picker.belief_note, "Rejected by user in DOXA belief browser");
        assert!(picker.pending.is_none());
        assert!(painted(&app).contains("[Confirm reject]"));
        app.lore_picker_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        app.lore_picker.as_mut().unwrap().selected = 1;
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().pending.is_none());
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
    }

    #[test]
    fn belief_table_third_row_actions_use_the_same_painted_geometry() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 40);
        app.show_belief_browser_fixture(0, &[(7, "first", "claim"), (8, "second", "claim"), (9, "third", "claim")]);
        app.sync_chooser_state();
        app.chooser_height_override.set(Some(10));
        let menu = app.active_chooser_rect().unwrap();
        assert_eq!(menu.height, 10);
        let third_y = menu.y + 4;
        let rendered = painted_at(&app, 100, 40);
        let row = rendered.lines().nth(usize::from(third_y)).unwrap();
        assert!(row.contains("#9") && row.contains("[Accept] [Reject]"), "{row}");
        app.mouse(MouseEvent { kind: MouseEventKind::Moved, column: menu.x + 2,
            row: third_y, modifiers: KeyModifiers::NONE });
        assert_eq!(app.lore_picker.as_ref().unwrap().rows[app.lore_picker.as_ref().unwrap().selected].id, 9);
        let reject = belief_buttons(menu, third_y,
            &[("[Accept]", KeyCode::Char('A')), ("[Reject]", KeyCode::Char('R'))])[1].0;
        app.mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left), column: reject.x,
            row: reject.y, modifiers: KeyModifiers::NONE });
        let picker = app.lore_picker.as_ref().unwrap();
        assert_eq!(picker.rows[picker.selected].id, 9);
        assert_eq!(picker.belief_intent, Some(doxa_lore::BeliefAction::Retract));
        assert!(picker.pending.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('/'), KeyModifiers::NONE));
        for c in "agent".chars() { app.lore_picker_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)); }
        assert_eq!(app.lore_picker.as_ref().unwrap().query, "agent");
    }

    #[test]
    fn belief_hover_preview_waits_for_dwell_and_revalidates_painted_rows() {
        let mut app=App::default();app.rail_visible=false;app.handle(Event::Resize(120,40));
        app.input="Private draft".into();app.input_cursor=app.input.len();
        app.show_belief_browser_fixture(0,&[(7,"user","First line\nSecond full line"),(8,"project","Other claim")]);
        painted_at(&app,120,40);
        let row=app.rendered_belief_rows.borrow()[0].clone();
        app.handle(Event::Mouse(MouseEvent {kind:MouseEventKind::Moved,column:row.rect.x+20,row:row.rect.y,modifiers:KeyModifiers::NONE}));
        let now=Instant::now();
        assert!(!painted_at(&app,120,40).contains("Full belief"));
        assert!(app.tick_belief_preview(now+Duration::from_millis(500)));
        let preview=painted_at(&app,120,40);
        assert!(preview.contains("Full belief") && preview.contains("Second full line"));
        assert_eq!(app.input,"Private draft");assert!(app.pending_prompts.is_empty());
        let picker=app.lore_picker.as_ref().unwrap();
        assert!(picker.belief_review.is_none() && picker.belief_action.is_none() && picker.review_seen==0);
        // Leaving the row must itself request a redraw, even with no chip/link.
        assert!(app.handle(Event::Mouse(MouseEvent {kind:MouseEventKind::Moved,column:row.rect.x+20,row:row.menu.y+1,modifiers:KeyModifiers::NONE})));
        assert!(app.belief_preview.owner().is_none());
        app.handle(Event::Mouse(MouseEvent {kind:MouseEventKind::Moved,column:row.rect.x+20,row:row.rect.y,modifiers:KeyModifiers::NONE}));
        assert!(app.tick_belief_preview(Instant::now()+Duration::from_millis(500)));
        app.lore_picker.as_mut().unwrap().rows[0].claim="Changed source row".into();
        assert!(!painted_at(&app,120,40).contains("Full belief"));
        assert!(app.tick_belief_preview(Instant::now()));
        assert_eq!(app.belief_preview.owner().unwrap().claim,"Changed source row");
        assert!(!painted_at(&app,120,40).contains("Full belief"),"changed row starts a fresh dwell");
        app.handle(Event::Resize(100,32));assert!(app.belief_pointer.is_none());
    }

    #[test]
    fn belief_hover_filter_and_menu_changes_cancel_preview_without_reading_review() {
        let mut app=App::default();app.rail_visible=false;app.handle(Event::Resize(120,40));
        app.show_belief_browser_fixture(0,&[(7,"user","Safe complete belief")]);painted_at(&app,120,40);
        let row=app.rendered_belief_rows.borrow()[0].clone();
        app.handle(Event::Mouse(MouseEvent {kind:MouseEventKind::Moved,column:row.rect.x+20,row:row.rect.y,modifiers:KeyModifiers::NONE}));
        app.tick_belief_preview(Instant::now()+Duration::from_millis(500));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('/'),KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('s'),KeyModifiers::NONE)));
        assert!(app.belief_preview.owner().is_none());
        assert!(app.lore_picker.as_ref().unwrap().belief_review.is_none());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc,KeyModifiers::NONE)));
        assert!(app.belief_preview.owner().is_none());
    }

    #[test]
    fn staged_and_clustered_proposals_show_delayed_full_hover_text() {
        for clustered in [false, true] {
            let mut app = App::default();
            app.rail_visible = false;
            app.handle(Event::Resize(120, 40));
            app.show_belief_browser_fixture(0, &[]);
            let picker = app.lore_picker.as_mut().unwrap();
            picker.proposal_mode = true;
            picker.cluster_mode = clustered;
            picker.proposals = vec![lore_picker::Proposal {
                pid: "proposal-1".into(), kind: "memory".into(), action: "add".into(),
                scope: "user".into(), summary: "Full staged fact with hidden tail".into(),
                source_session_id: None, source_project: None,
            }];
            painted_at(&app, 120, 40);
            let row = app.rendered_belief_rows.borrow()[0].clone();
            assert!(app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Moved,
                column: row.rect.x + 10, row: row.rect.y, modifiers: KeyModifiers::NONE })));
            assert!(!painted_at(&app, 120, 40).contains("Full proposal"));
            assert!(app.tick_belief_preview(Instant::now() + Duration::from_millis(600)));
            let preview = painted_at(&app, 120, 40);
            assert!(preview.contains("Full proposal"), "{preview}");
            assert!(preview.contains("Full staged fact with hidden tail"), "{preview}");
        }
    }

    #[test]
    fn belief_prompt_filter_keeps_session_draft_and_exact_review_selection() {
        let mut app=App::default();app.size=Rect::new(0,0,120,40);
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"codex","model":"gpt-6-sol"}));
        app.input="Private unsent draft".into();app.input_cursor=app.input.len();
        app.show_belief_browser_fixture(0,&[(100,"project","old unrelated"),(7,"user","recent project preference")]);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('/'),KeyModifiers::NONE)));
        for ch in "project preference".chars() {app.handle(Event::Key(KeyEvent::new(KeyCode::Char(ch),KeyModifiers::NONE)));}
        let due=app.belief_filter_due.unwrap();
        assert!(!app.poll_belief_filter(due+Duration::from_millis(199)));
        assert!(app.poll_belief_filter(due+Duration::from_millis(200)));
        assert_eq!(app.lore_picker.as_ref().unwrap().rows.iter().map(|row|row.id).collect::<Vec<_>>(),vec![7]);
        let painted=painted_at(&app,120,40);
        assert!(painted.contains("Filter beliefs") && painted.contains("project preference"));
        assert!(!painted.contains("Search:") && !painted.contains("Shift+A accept"));
        assert_eq!(app.input,"Private unsent draft");
        app.lore_picker_key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE));
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('r'),KeyModifiers::NONE));
        let picker=app.lore_picker.as_ref().unwrap();
        assert_eq!(picker.rows[picker.selected].id,7);
        assert_eq!(picker.belief_intent,Some(doxa_lore::BeliefAction::Retract));
        assert!(app.pending_prompts.is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc,KeyModifiers::NONE)));
        assert_eq!(app.input,"Private unsent draft");
    }

    #[test]
    fn stale_belief_filter_result_cannot_replace_current_query_rows() {
        let mut app=App::default();app.size=Rect::new(0,0,120,40);
        app.show_belief_browser_fixture(0,&[(7,"user","current")]);
        app.belief_filter_request=Some(("old".into(),0));
        let (tx,rx)=mpsc::sync_channel(1);
        app.lore_picker.as_mut().unwrap().pending=Some(rx);
        tx.send(Ok(lore_picker::ResultPage::Beliefs(Vec::new()))).unwrap();
        app.lore_picker.as_mut().unwrap().query="current".into();
        assert!(app.poll_lore());
        assert_eq!(app.lore_picker.as_ref().unwrap().rows[0].id,7);
        assert_eq!(app.lore_picker.as_ref().unwrap().query,"current");
    }

    #[test]
    fn belief_list_lowercase_action_initials_remain_search_text() {
        for query in ["agent", "derive"] {
            let mut app = App::default();
            app.size = Rect::new(0, 0, 100, 40);
            app.show_belief_browser_fixture(0, &[(7, "fixture", "claim")]);
            app.lore_picker_key(KeyEvent::new(KeyCode::Char('/'), KeyModifiers::NONE));
            for c in query.chars() {
                assert!(app.lore_picker_key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE)));
            }
            let picker = app.lore_picker.as_ref().unwrap();
            assert_eq!(picker.query, query);
            assert!(picker.belief_intent.is_none());
            assert!(picker.belief_review.is_none());
            assert!(picker.belief_action.is_none());
            assert!(picker.pending.is_none());
        }
    }

    #[test]
    fn belief_list_buttons_preserve_exact_row_and_intent_in_safe_fixture() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 40);
        app.show_belief_browser_fixture(0, &[(7, "first", "claim"), (8, "second", "claim")]);
        let menu = app.active_chooser_rect().unwrap();
        let first = menu.y + 2;
        let button = belief_buttons(menu, first + 1,
            &[("[Accept]", KeyCode::Char('A')), ("[Reject]", KeyCode::Char('R'))])[1].0;
        app.mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left), column: button.x,
            row: button.y, modifiers: KeyModifiers::NONE });
        let picker = app.lore_picker.as_ref().unwrap();
        assert_eq!(picker.rows[picker.selected].id, 8);
        assert_eq!(picker.belief_intent, Some(doxa_lore::BeliefAction::Retract));
        assert!(picker.pending.is_none());
        assert!(picker.belief_action.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn belief_row_a_and_r_apply_after_exact_review_without_opening_another_menu() {
        for (key, action) in [('a', doxa_lore::BeliefAction::Confirmed),
                              ('r', doxa_lore::BeliefAction::Retract)] {
            let mut app = App::default();
            app.size = Rect::new(0, 0, 100, 40);
            app.show_belief_browser_fixture(0, &[(7, "Fixture subject", "clipped list text")]);
            app.lore_picker_key(KeyEvent::new(KeyCode::Char(key), KeyModifiers::NONE));
            assert_eq!(app.lore_picker.as_ref().unwrap().belief_intent, Some(action));
            let (tx, rx) = mpsc::sync_channel(1);
            app.lore_picker.as_mut().unwrap().pending = Some(rx);
            tx.send(Ok(lore_picker::ResultPage::BeliefReview(belief_review_fixture(), true))).unwrap();
            assert!(app.poll_lore());
            let picker = app.lore_picker.as_ref().unwrap();
            assert!(picker.belief_intent.is_none());
            assert!(picker.belief_review.is_none());
            assert!(picker.pending.is_none(), "gallery fixture must never send an action to LORE");
        }
    }

    #[test]
    fn belief_mouse_selection_only_requests_review_of_exact_clicked_row() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 40);
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","cwd":"/repo"}));
        app.open_lore_picker();
        let picker = app.lore_picker.as_mut().unwrap();
        picker.pending = None;
        picker.rows = [7, 8].into_iter().map(|id| lore_picker::Belief { id,
            subject: "fixture".into(), claim: "list text".into(), truncated: false,
            confidence: 0.8, evidence_count: Some(0), recency:None }).collect();
        let menu = app.active_chooser_rect().unwrap();
        let first = menu.y + 2;
        let click = |row| MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 22, row, modifiers: KeyModifiers::NONE };
        assert!(app.mouse(click(first + 1)));
        assert_eq!(app.lore_picker.as_ref().unwrap().selected, 1);
        assert!(app.lore_picker.as_ref().unwrap().pending.is_none());
        assert!(app.mouse(click(first + 1)));
        assert!(app.lore_picker.as_ref().unwrap().pending.is_some());
        assert!(app.lore_picker.as_ref().unwrap().belief_review.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn belief_error_or_missing_capability_remains_read_only() {
        let mut app = app_with_review();
        app.lore_picker.as_mut().unwrap().can_act_on_beliefs = false;
        for _ in 0..100 { app.lore_picker_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE)); }
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().belief_action.is_none());
        let (tx, rx) = mpsc::sync_channel(1);
        let picker = app.lore_picker.as_mut().unwrap();
        picker.pending = Some(rx);
        picker.resolving = true;
        picker.belief_acting = true;
        tx.send(Err("Belief changed; reopen a fresh exact review")).unwrap();
        assert!(app.poll_lore());
        let picker = app.lore_picker.as_ref().unwrap();
        assert!(!picker.can_act_on_beliefs);
        assert!(picker.belief_review.is_some());
        assert!(picker.status.contains("Belief changed"));
        assert!(app.pending_queue_commands.is_empty());
        assert!(!app.notice.contains("applied"));
    }

    #[cfg(unix)]
    #[test]
    fn wheel_scroll_can_complete_exact_belief_review() {
        let mut app = app_with_review();
        let menu = app.active_chooser_rect().unwrap();
        for _ in 0..100 {
            app.mouse(MouseEvent { kind: MouseEventKind::ScrollDown,
                column: menu.x + 3, row: menu.y + 6, modifiers: KeyModifiers::NONE });
        }
        let picker = app.lore_picker.as_ref().unwrap();
        let review = picker.belief_review.as_ref().unwrap();
        let full = format!("Subject: {}\nClaim: {}", review.subject(), review.claim());
        assert_eq!(picker.review_seen, raw_visual_rows(&full, picker.review_width).len());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::NONE));
        assert_eq!(app.lore_picker.as_ref().unwrap().belief_action, Some(doxa_lore::BeliefAction::Stale));
    }

    #[cfg(unix)]
    #[test]
    fn proposal_action_requires_reading_to_end_then_explicit_arm() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let sidecar = dir.path().join("sidecar");
        std::fs::write(&sidecar, r#"#!/usr/bin/env python3
import hashlib, json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot','pending_review_v1','resolve_reviewed_v1']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    raw = json.dumps({'kind':'memory','text':'x'*4000})
    value = {'pid':req['pid'],'raw':raw,'sha256':hashlib.sha256(raw.encode()).hexdigest(),'inode':11,'complete':True}
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'value':value}), flush=True)
"#).unwrap();
        let mut permissions = std::fs::metadata(&sidecar).unwrap().permissions();
        permissions.set_mode(0o700);
        std::fs::set_permissions(&sidecar, permissions).unwrap();
        let lore_picker::ResultPage::Review(review, can_resolve) = lore_picker::fetch_fixture(&sidecar,
            lore_picker::Query::Review("/repo".into(), "one".into())).unwrap() else { panic!("review") };
        assert!(can_resolve);
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 40);
        app.lore_picker = Some(LorePicker {
            session_id: None,
            query: String::new(), filter_focused: false, rows: Vec::new(), selected: 0, offset: 0,
            proposals: vec![lore_picker::Proposal { pid: "one".into(), kind: "memory".into(),
                action: "add".into(), scope: "user".into(), summary: String::new(), source_session_id: None, source_project: None }],
            proposal_mode: true, cluster_mode: false, all_proposals: Vec::new(), review: Some(review), review_scroll: 0,
            review_seen: 0, review_width: 0, armed_resolution: None,
            can_resolve, resolving: false, cwd: "/repo".into(),
            belief_review: None, belief_intent: None, can_act_on_beliefs: false, belief_action: None,
            belief_note: String::new(), retract_armed: false, belief_acting: false,
            result_status: None,
            evidence: None, status: String::new(), pending: None,
        });
        app.sync_chooser_state();
        app.chooser_height_override.set(Some(5));
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().armed_resolution.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        assert_eq!(app.lore_picker.as_ref().unwrap().review_scroll, 0);
        assert_eq!(app.lore_picker.as_ref().unwrap().review_seen, 0);
        app.chooser_height_override.set(None);
        for _ in 0..200 {
            if app.lore_picker.as_ref().unwrap().review_seen ==
                raw_visual_rows(app.lore_picker.as_ref().unwrap().review.as_ref().unwrap().raw(),
                    app.lore_picker.as_ref().unwrap().review_width).len() { break; }
            app.lore_picker_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        }
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::CONTROL));
        assert!(app.lore_picker.as_ref().unwrap().armed_resolution.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::ALT));
        assert!(app.lore_picker.as_ref().unwrap().armed_resolution.is_none());
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE));
        assert_eq!(app.lore_picker.as_ref().unwrap().armed_resolution, Some(doxa_lore::PendingDecision::Approve));
        assert!(app.lore_picker.as_ref().unwrap().pending.is_none());
        app.lore_picker.as_mut().unwrap().armed_resolution = None;
        app.lore_picker.as_mut().unwrap().can_resolve = false;
        app.lore_picker_key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().armed_resolution.is_none());
        app.chooser_height_override.set(Some(0));
        app.lore_picker_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.lore_picker.as_ref().unwrap().review.is_none());
    }

    #[test]
    fn pending_local_command_opens_proposals_without_sending_prompt() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 40);
        app.groups[0].tabs.push("session-1".into());
        app.input = " /pending ".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.lore_picker.as_ref().unwrap().proposal_mode);
        assert!(app.input.is_empty());
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn branch_command_targets_active_daemon_and_never_becomes_a_prompt() {
        let mut app = App::default();
        app.groups[0].tabs.push("session-1".into());
        app.input = "/branch feature".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(matches!(app.pending_queue_commands.pop(),
            Some(crate::bridge::WorkerCommand::Branch(id, Some(name)))
                if id == "session-1" && name == "feature"));
        assert!(app.pending_prompts.is_empty());
        assert!(app.input.is_empty());
        app.apply_daemon_frame(&json!({"type":"branch_reply", "session_id":"session-1",
            "ok":false,"error":"session is busy"}));
        assert!(app.notice.contains("session is busy"));
    }

    #[test]
    fn branch_listing_opens_bounded_picker_and_enter_targets_active_session() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 35);
        app.groups[0].tabs.push("session-1".into());
        app.input = "/branch".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(matches!(app.pending_queue_commands.pop(),
            Some(crate::bridge::WorkerCommand::Branch(id, None)) if id == "session-1"));
        assert!(app.apply_daemon_frame(&json!({"type":"branch_reply", "session_id":"session-1",
            "ok":true,"base":"main","branches":["feature","main","bad\nname"]})));
        assert_eq!(app.branch_picker.as_ref().unwrap().branches, ["feature", "main"]);
        assert_eq!(app.branch_picker.as_ref().unwrap().selected, 1);
        assert!(app.active_chooser_rect().is_some());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.branch_picker.is_none());
        assert!(matches!(app.pending_queue_commands.pop(),
            Some(crate::bridge::WorkerCommand::Branch(id, Some(name)))
                if id == "session-1" && name == "feature"));
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn branch_picker_cancel_and_stale_reply_do_not_switch_checkout() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 35);
        app.groups[0].tabs.push("session-1".into());
        assert!(!app.apply_daemon_frame(&json!({"type":"branch_reply", "session_id":"other",
            "ok":true,"base":"main","branches":["feature"]})));
        assert!(app.branch_picker.is_none());
        app.apply_daemon_frame(&json!({"type":"branch_reply", "session_id":"session-1",
            "ok":true,"base":"main","branches":["feature","main"]}));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(app.branch_picker.is_none());
        assert!(app.pending_queue_commands.is_empty());
    }

    #[test]
    fn branch_picker_mouse_selects_row_and_click_outside_cancels() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 35);
        app.groups[0].tabs.push("session-1".into());
        let list = json!({"type":"branch_reply", "session_id":"session-1",
            "ok":true,"base":"main","branches":["feature","main"]});
        app.apply_daemon_frame(&list);
        let menu = app.active_chooser_rect().unwrap();
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x + 2, row: menu.y + 2, modifiers: KeyModifiers::NONE }));
        assert!(matches!(app.pending_queue_commands.pop(),
            Some(crate::bridge::WorkerCommand::Branch(id, Some(name)))
                if id == "session-1" && name == "feature"));
        app.apply_daemon_frame(&list);
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: menu.x, row: menu.y.saturating_sub(1), modifiers: KeyModifiers::NONE }));
        assert!(app.branch_picker.is_none());
        assert!(app.pending_queue_commands.is_empty());
    }

    #[test]
    fn attach_selection_queues_new_tab_and_focuses_existing_tab() {
        let mut app = App::default();
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"current", "model":"model"}));
        app.input = "/attach detached".into();
        app.attach_selected("detached");
        assert_eq!(app.pending_attaches, [("detached".into(), 0)]);
        assert_eq!(app.groups[0].active_id(), Some("current"));
        assert!(app.pending_prompts.is_empty());
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"detached", "model":"other"}));
        app.apply_daemon_frame(&json!({"type":"attach_reply", "ok":true,
            "session_id":"detached", "group":0}));
        assert_eq!(app.groups[0].active_id(), Some("detached"));
        assert_eq!(app.groups[0].tabs, ["current", "detached"]);
        app.groups[0].active = 0;
        app.attach_selected("detached");
        assert_eq!(app.groups[0].active_id(), Some("detached"));
        assert_eq!(app.groups[0].tabs.len(), 2);
    }

    #[test]
    fn attach_picker_filters_titles_and_ids_without_sending_a_prompt() {
        let row = |id: &str, title: &str| crate::discovery::Session {
            id: id.into(), title: title.into(), socket: PathBuf::new(),
            scope_key: String::new(), clients: None, started_at: String::new(),
        };
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 30);
        app.attach_picker = Some(AttachPicker { rows: vec![row("abc123", "Alpha work"),
            row("def456", "Beta work")], query: String::new(), selected: 0 });
        assert!(app.active_chooser_rect().is_some());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('b'), KeyModifiers::NONE)));
        assert_eq!(app.attach_matches(), [1]);
        assert!(app.pending_prompts.is_empty());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE)));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE)));
        assert_eq!(app.attach_picker.as_ref().unwrap().selected, 1);
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert!(app.attach_picker.is_none());
        assert!(app.pending_attaches.is_empty());
        assert!(attach_matches(&row("id123", "Release planning"), "plan"));
        assert!(attach_matches(&row("id123", "Release planning"), "ID1"));
        app.attach_picker = Some(AttachPicker { rows: vec![row("definitely-not-live-attach-test", "Gone")],
            query: String::new(), selected: 0 });
        app.open_selected_attach();
        assert!(app.attach_picker.is_none());
        assert!(app.pending_attaches.is_empty());
        assert!(app.notice.starts_with("attach:"));
    }

    #[test]
    fn rail_double_click_edits_name_and_preserves_prompt_draft() {
        let mut app = App::default();
        app.handle(Event::Resize(100, 30));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "title":"sol@doxa:main", "cwd":"/repo"}));
        let rail = app.layout(app.size).rail.unwrap();
        let row = app.rail_rows().iter().position(|row| matches!(row, RailRow::Session(_))).unwrap();
        let click = || Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: rail.x + 2, row: rail.y + 1 + row as u16, modifiers: KeyModifiers::NONE });
        app.input = "unfinished prompt".into();
        app.input_cursor = app.input.len();
        app.handle(click());
        app.handle(click());
        assert_eq!(app.input, "/rename sol@doxa:main");
        assert_eq!(app.focus, Focus::Prompt);
        app.input = "/rename Review session".into();
        app.input_cursor = app.input.len();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.sessions[0].title, "Review session");
        assert_eq!(app.input, "unfinished prompt");
        assert!(app.pending_prompts.is_empty());
        app.handle(click());
        app.handle(click());
        assert_eq!(app.input, "/rename Review session");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)));
        assert_eq!(app.input, "unfinished prompt");
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"other", "title":"Other", "cwd":"/other"}));
        app.groups[0].tabs = vec!["s".into(), "other".into()];
        app.groups[0].active = 0;
        let s_row = app.rail_rows().iter().position(|row| matches!(row,
            RailRow::Session(index) if app.sessions[*index].id == "s")).unwrap();
        let other_row = app.rail_rows().iter().position(|row| matches!(row,
            RailRow::Session(index) if app.sessions[*index].id == "other")).unwrap();
        let s_click = || Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: rail.x + 2, row: rail.y + 1 + s_row as u16, modifiers: KeyModifiers::NONE });
        let other_click = Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column: rail.x + 2, row: rail.y + 1 + other_row as u16, modifiers: KeyModifiers::NONE });
        app.handle(s_click());
        app.handle(s_click());
        app.handle(other_click);
        app.handle(s_click());
        assert_eq!(app.input, "unfinished prompt");
    }

    #[test]
    fn rename_pins_label_until_cleared_and_slash_commands_never_queue_prompts() {
        let mut app = App::default(); app.handle(Event::Resize(100, 30));
        app.apply_daemon_frame(&json!({"type":"hello", "session_id":"s", "model":"old"}));
        app.input = "/rename A useful name".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.custom_names.get("s").map(String::as_str), Some("A useful name"));
        assert_eq!(app.sessions[0].title, "A useful name");
        app.apply_daemon_frame(&json!({"type":"event", "session_id":"s",
            "event":{"type":"model_changed", "data":{"model":"new"}}}));
        assert_eq!(app.sessions[0].title, "A useful name");
        app.input = "/rename".into();
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.custom_names.is_empty());
        assert_eq!(app.sessions[0].title, "old");
        for command in ["/attach bad/id", "/doctor", "/pending unsupported"] {
            app.input = command.into();
            app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
            if command == "/doctor" { assert!(app.operations_menu.is_some()); app.retire_operations(); app.chip_info = None; }
            else { assert!(app.notice.contains("attach:") || app.notice.contains("unavailable")); }
            assert!(app.pending_prompts.is_empty());
        }
    }

    #[test]
    fn indeterminate_lore_resolution_disarms_review_and_requires_recovery() {
        let mut app = App::default();
        app.open_pending_picker();
        let (tx, rx) = mpsc::sync_channel(1);
        let picker = app.lore_picker.as_mut().unwrap();
        picker.pending = Some(rx);
        picker.resolving = true;
        tx.send(Ok(lore_picker::ResultPage::Resolved(
            doxa_lore::PendingResolution::Indeterminate { code: "archive_failed".into() }))).unwrap();
        assert!(app.poll_lore());
        assert!(app.notice.contains("may have applied"));
        assert!(app.notice.contains("recovery required"));
        let picker = app.lore_picker.as_ref().unwrap();
        assert!(picker.pending.is_none() && picker.review.is_none() && picker.armed_resolution.is_none());
        assert!(picker.proposals.is_empty() && !picker.resolving);
    }

    #[test]
    fn partial_lore_archive_failure_stays_visible_and_never_retries() {
        let mut app = App::default();
        app.open_pending_picker();
        let (tx, rx) = mpsc::sync_channel(1);
        let picker = app.lore_picker.as_mut().unwrap();
        picker.pending = Some(rx);
        picker.resolving = true;
        tx.send(Ok(lore_picker::ResultPage::Resolved(
            doxa_lore::PendingResolution::Refused { code: "archive_failed".into(), applied: true }))).unwrap();
        assert!(app.poll_lore());
        assert!(app.notice.contains("do not retry automatically"));
        assert!(app.lore_picker.as_ref().unwrap().pending.is_none());
        assert!(!app.lore_picker.as_ref().unwrap().resolving);
    }

    fn wheel_event(kind: MouseEventKind, area: Rect) -> Event {
        Event::Mouse(MouseEvent {kind,column:area.x+2,row:area.y+2,modifiers:KeyModifiers::NONE})
    }
    #[test]
    fn wheel_scrolls_hovered_unfocused_transcript_and_preserves_prompt_owner() {
        let mut app=App::default(); app.rail_visible=false;app.handle(Event::Resize(120,40));
        for id in ["first","second"] {
            app.apply_update(DaemonUpdate::Upsert(Session{id:id.into(),title:id.into(),collection:"repo".into(),
                transcript:(0..100).map(|n|format!("{id} line {n}\n\n")).collect(),status:"Offline".into()}));
        }
        for session in &mut app.sessions {session.transcript=(0..100).map(|n|format!("{} line {n}\n\n",session.id)).collect();}
        app.groups[0].tabs=vec!["first".into()];app.groups[0].active=0;
        app.groups[1].tabs=vec!["second".into()];app.groups[1].active=0;
        app.split=Split::Vertical;app.active_group=0;app.focus=Focus::Prompt;app.input="unsent draft".into();
        let panes=app.layout(app.size).panes.unwrap();let body=app.pane_regions(1,panes[1])[1];
        let before=painted_at(&app,120,40);
        assert!(app.handle(wheel_event(MouseEventKind::ScrollUp,body)));
        assert_eq!(app.groups[1].scroll,3);assert_eq!(app.groups[0].scroll,0);
        assert_eq!(app.active_group,0);assert_eq!(app.focus,Focus::Prompt);assert_eq!(app.input,"unsent draft");
        assert_ne!(painted_at(&app,120,40),before);
        assert!(app.handle(wheel_event(MouseEventKind::ScrollDown,body)));assert_eq!(app.groups[1].scroll,0);
        let prompt=app.pane_regions(1,panes[1])[4];
        assert!(!app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::ScrollUp,column:prompt.x+1,row:prompt.y,modifiers:KeyModifiers::NONE})));
        assert_eq!(app.groups[0].scroll,0);assert_eq!(app.groups[1].scroll,0);
    }
    #[test]
    fn wheel_navigates_scrolled_inline_model_list_without_activating_or_losing_focus() {
        let mut app=scrolled_picker_app();app.focus=Focus::Prompt;app.input="keep draft".into();
        app.model_picker=Some(ModelPicker{session_id:"session".into(),models:(0..30).map(|n|format!("model-{n:02}")).collect(),selected:24,note:String::new(),loading:false,catalog_pending:false});
        let menu=app.active_chooser_rect().unwrap();painted_at(&app,100,28);
        assert!(app.handle(wheel_event(MouseEventKind::ScrollDown,menu)));assert_eq!(app.model_picker.as_ref().unwrap().selected,25);
        assert!(app.handle(wheel_event(MouseEventKind::ScrollUp,menu)));assert_eq!(app.model_picker.as_ref().unwrap().selected,24);
        assert!(app.pending_model_changes.is_empty());assert_eq!(app.focus,Focus::Prompt);assert_eq!(app.input,"keep draft");
        assert_eq!(app.groups[0].scroll,0);
        let outside=MouseEvent{kind:MouseEventKind::ScrollDown,column:menu.right()+1,row:menu.y+2,modifiers:KeyModifiers::NONE};
        app.handle(Event::Mouse(outside));assert_eq!(app.model_picker.as_ref().unwrap().selected,24);
    }
    #[test]
    fn wheel_over_empty_pane_or_rail_never_scrolls_focused_transcript() {
        let mut app=App::default();app.sidebar_auto=false;app.rail_visible=true;app.handle(Event::Resize(120,40));app.groups[0].tabs.push("session".into());
        app.apply_update(DaemonUpdate::Upsert(Session{id:"session".into(),title:"session".into(),collection:"repo".into(),transcript:String::new(),status:"Offline".into()}));app.split=Split::Vertical;app.split_requested=true;let layout=app.layout(app.size);
        assert!(app.handle(wheel_event(MouseEventKind::ScrollUp,layout.rail.unwrap())));
        assert_eq!(app.focus, Focus::Rail);
        assert!(!app.handle(wheel_event(MouseEventKind::ScrollUp,app.pane_regions(1,layout.panes.unwrap()[1])[1])));
        assert_eq!(app.groups[0].scroll,0);assert_eq!(app.active_group,0);
    }

    #[test]
    fn memory_hover_uses_full_row_background_delayed_preview_and_border_hitboxes() {
        let mut app=App::default();app.rail_visible=false;app.handle(Event::Resize(120,40));
        app.apply_update(DaemonUpdate::Upsert(Session{id:"memory-session".into(),title:"Memory session".into(),collection:"repo".into(),transcript:String::new(),status:"Offline".into()}));
        app.session_cwds.insert("memory-session".into(),PathBuf::from("/fixture"));
        let full="A complete curated fact with a hidden tail ".repeat(7)+"FULL ENTRY TAIL";
        app.show_memory_manager_fixture(0,"user",json!({"scope":"user","key":"user","sha256":"a".repeat(64),"entries":["First fact",full],"chars":400,"cap_chars":9000})).unwrap();
        painted_at(&app,120,40);let row=app.rendered_belief_rows.borrow()[1].clone();
        assert!(app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Moved,column:row.rect.x+20,row:row.rect.y,modifiers:KeyModifiers::NONE})));
        assert_eq!(app.memory_manager.as_ref().unwrap().selected,1);
        let now=Instant::now();assert!(!app.tick_belief_preview(now+Duration::from_millis(400)));
        assert!(!painted_at(&app,120,40).contains("Full memory"));
        let mut terminal=Terminal::new(TestBackend::new(120,40)).unwrap();terminal.draw(|frame|app.draw(frame)).unwrap();
        assert_eq!(terminal.backend().buffer()[(row.rect.right()-1,row.rect.y)].bg,theme::HIGHLIGHT);
        assert!(app.tick_belief_preview(now+Duration::from_millis(600)));
        let displayed=painted_at(&app,120,40);assert!(displayed.contains("Full memory"));assert!(displayed.contains("FULL ENTRY TAIL"));
        assert!(app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Moved,column:row.menu.x,row:row.rect.y,modifiers:KeyModifiers::NONE})));
        assert!(app.memory_preview.owner().is_none());assert!(!painted_at(&app,120,40).contains("Full memory"));
        assert!(app.pending_prompts.is_empty());assert!(!app.memory_manager.as_ref().unwrap().editing());
    }
    #[test]
    fn scrolled_memory_hover_keeps_viewport_and_preview_matches_full_visible_entry() {
        let mut app=scrolled_picker_app();app.session_cwds.insert("session".into(),PathBuf::from("/fixture"));
        let entries=(0..30).map(|index|format!("Curated full entry {index:02} with complete display text")).collect::<Vec<_>>();
        app.show_memory_manager_fixture(0,"user",json!({"scope":"user","key":"user","sha256":"a".repeat(64),"entries":entries,"chars":1800,"cap_chars":9000})).unwrap();
        app.memory_manager.as_mut().unwrap().selected=24;painted_at(&app,100,28);
        let first=app.rendered_belief_rows.borrow()[0].clone();assert!(first.id>1);
        app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Moved,column:first.rect.x+4,row:first.rect.y,modifiers:KeyModifiers::NONE}));
        let expected=first.claim.clone();assert_eq!(app.memory_manager.as_ref().unwrap().selected as u64+1,first.id);
        painted_at(&app,100,28);assert_eq!(app.rendered_belief_rows.borrow()[0].claim,expected);
        assert!(app.tick_belief_preview(Instant::now()+Duration::from_millis(600)));
        assert_eq!(app.memory_preview.owner().unwrap().claim,expected);assert!(painted_at(&app,100,28).contains("Full memory"));
        let menu=app.active_chooser_rect().unwrap();app.handle(wheel_event(MouseEventKind::ScrollDown,menu));
        assert!(app.memory_preview.owner().is_none());
    }

    #[test]
    fn pending_title_click_keyboard_cluster_count_and_individual_navigation() {
        let mut app=scrolled_picker_app();
        app.show_belief_browser_fixture(0, &[(1,"subject","claim")]);
        let menu=app.active_chooser_rect().unwrap();
        let click=|offset| Event::Mouse(MouseEvent {kind:MouseEventKind::Down(MouseButton::Left),column:menu.x+1+offset,row:menu.y,modifiers:KeyModifiers::NONE});
        assert!(app.handle(click(12))); assert!(app.lore_picker.as_ref().unwrap().proposal_mode);
        assert!(!app.lore_picker.as_ref().unwrap().cluster_mode); assert_eq!(app.drag,None);
        app.lore_picker.as_mut().unwrap().pending=None;
        assert!(app.handle(Event::Key(KeyEvent::new(KeyCode::Char('3'),KeyModifiers::CONTROL))));
        assert!(app.lore_picker.as_ref().unwrap().cluster_mode);
        let (tx,rx)=std::sync::mpsc::channel();
        let rows=(0..25).map(|n|lore_picker::Proposal{pid:format!("pid-{n}"),kind:"memory".into(),action:"add".into(),scope:"user".into(),summary:format!("Cluster 1 · fact {n}"),source_session_id:None,source_project:None}).collect();
        tx.send(Ok(lore_picker::ResultPage::ClusteredProposals(rows,1))).unwrap();
        app.lore_picker.as_mut().unwrap().pending=Some(rx); assert!(app.poll_lore());
        let painted=painted_at(&app,100,28); assert!(painted.contains("1 memory clusters")); assert!(painted.contains("3 Clustered"));
        assert!(app.handle(Event::Key(KeyEvent::new(KeyCode::PageDown,KeyModifiers::NONE))));
        assert_eq!(app.lore_picker.as_ref().unwrap().selected,20); assert_eq!(app.lore_picker.as_ref().unwrap().offset,0);
        assert!(app.handle(Event::Key(KeyEvent::new(KeyCode::Char('4'),KeyModifiers::NONE))));
        assert_eq!(app.lore_picker.as_ref().unwrap().proposals.len(),3);
        assert_eq!(app.lore_picker.as_ref().unwrap().proposals[0].pid,"pid-4");
        assert!(app.lore_picker.as_ref().unwrap().review.is_none()); assert!(!app.lore_picker.as_ref().unwrap().can_resolve);
        let menu=app.active_chooser_rect().unwrap();
        assert!(app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Down(MouseButton::Left),column:menu.x+2,row:menu.y,modifiers:KeyModifiers::NONE}))); assert!(!app.lore_picker.as_ref().unwrap().proposal_mode); assert!(!app.lore_picker.as_ref().unwrap().cluster_mode);
    }
#[test]
fn unavailable_saved_tabs_do_not_block_second_split_session_persistence() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = crate::ui_state::UiStateStore::new(dir.path(), "/project", "machine").unwrap();
    let mut original = App::default();
    original.groups[0].tabs = vec!["offline-a".into(), "live-a".into()];
    original.groups[0].active = 1;
    original.groups[1].tabs = vec!["offline-b".into()];
    original.active_group = 1;
    original.split = Split::Vertical;
    original.split_percent = 43;
    store.save(&original).unwrap();
    let original_bytes = std::fs::read(store.path()).unwrap();
    let mut app = App::default();
    assert!(store.restore(&mut app, &["live-a".into()]));
    assert_eq!(app.groups[0].tabs, original.groups[0].tabs);
    assert_eq!(app.groups[1].tabs, original.groups[1].tabs);
    assert_eq!(app.active_group, 1);
    assert_eq!(std::fs::read(store.path()).unwrap(), original_bytes);
    assert!(app.sessions.iter().all(|session| session.status.contains("unavailable")));
    assert!(app.history_entries.values().all(|entry| entry.markdown.is_empty()));
    assert!(crate::history::resume_plan(&app.history_entries["offline-b"]).is_err());
    let mut signature = crate::ui_state::LayoutSignature::capture(&app);

    // Startup hello and delayed launch reply arrive after the user changed pane.
    app.apply_daemon_frame(&json!({"type":"hello", "session_id":"live-a", "cwd":"/project"}));
    app.active_group = 0;
    app.input = "left draft".into(); app.input_cursor = 10;
    app.input_drafts.insert((1, "offline-b".into()), ("right draft".into(), 11));
    app.launching = true;
    app.apply_daemon_frame(&json!({"type":"hello", "session_id":"new-second", "cwd":"/project"}));
    app.apply_daemon_frame(&json!({"type":"launch_reply", "ok":true, "session_id":"new-second", "group":1}));
    assert_eq!(app.active_group, 0);
    assert_eq!(app.input, "left draft");
    assert_eq!(app.input_drafts[&(1, "offline-b".into())].0, "right draft");
    assert_eq!(app.groups[1].tabs, ["offline-b", "new-second"]);
    assert!(app.pending_prompts.is_empty());
    save_layout_if_changed(&mut app, &mut store, &Mutex::new(true), &mut signature);
    assert_eq!(signature, crate::ui_state::LayoutSignature::capture(&app));
    assert!(!app.notice.starts_with("Layout save skipped"));
    drop(store);
    drop(app);

    let mut reloaded = crate::ui_state::UiStateStore::new(dir.path(), "/project", "machine").unwrap();
    let mut restored = App::default();
    assert!(reloaded.restore(&mut restored, &[]));
    assert_eq!(restored.groups[0].tabs, ["offline-a", "live-a"]);
    assert_eq!(restored.groups[1].tabs, ["offline-b", "new-second"]);
    assert_eq!(restored.groups[0].active, 1);
    assert_eq!(restored.groups[1].active, 1);
    assert_eq!((restored.active_group, restored.split, restored.split_percent), (0, Split::Vertical, 43));
    assert!(restored.pending_prompts.is_empty());
    assert!(restored.sessions.iter().all(|session| session.status.contains("unavailable")));
    assert!(reloaded.save_if_complete(&restored, &Mutex::new(true)).unwrap());
}

    #[test]
    fn pending_filters_use_prompt_line_and_lowercase_b_preserves_draft() {
        for clustered in [false,true] {
            let mut app=App::default();app.size=Rect::new(0,0,120,40);
            app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"codex","model":"gpt-6-sol"}));
            app.input="Private unsent draft".into();app.input_cursor=app.input.len();
            app.show_belief_browser_fixture(0,&[]);
            let rows=vec![lore_picker::Proposal {pid:"pid-b".into(),kind:"memory".into(),action:"add".into(),scope:"user".into(),summary:"blue fact".into(),source_session_id:None,source_project:None},lore_picker::Proposal {pid:"pid-a".into(),kind:"memory".into(),action:"add".into(),scope:"user".into(),summary:"red fact".into(),source_session_id:None,source_project:None}];
            let picker=app.lore_picker.as_mut().unwrap();picker.proposal_mode=true;picker.cluster_mode=clustered;picker.all_proposals=rows.clone();picker.proposals=rows;
            for ch in "blue".chars() {assert!(app.handle(Event::Key(KeyEvent::new(KeyCode::Char(ch),KeyModifiers::NONE))));}
            let picker=app.lore_picker.as_ref().unwrap();assert!(picker.proposal_mode);assert_eq!(picker.cluster_mode,clustered);assert_eq!(picker.query,"blue");assert_eq!(picker.proposals.len(),1);
            let painted=painted_at(&app,120,40);assert!(painted.contains("Filter pending"));assert!(!painted.contains("Ctrl+1/2/3") && !painted.contains("Filter:"));
            let prompt=app.pane_regions(0,app.layout(app.size).body)[4];let mut terminal=Terminal::new(TestBackend::new(120,40)).unwrap();terminal.draw(|frame|app.draw(frame)).unwrap();
            let buffer=terminal.backend().buffer();let prompt_text=(prompt.y..prompt.bottom()).flat_map(|y|(prompt.x..prompt.right()).map(move |x|(x,y))).map(|position|buffer[position].symbol()).collect::<String>();assert!(prompt_text.contains("blue"));assert!(!prompt_text.contains("Private unsent draft"));
            assert_eq!(app.input,"Private unsent draft");assert!(app.handle(Event::Key(KeyEvent::new(KeyCode::Backspace,KeyModifiers::NONE))));assert_eq!(app.lore_picker.as_ref().unwrap().query,"blu");
            app.lore_picker.as_mut().unwrap().resolving=true;assert!(!painted_at(&app,120,40).contains("Filter pending"));app.lore_picker.as_mut().unwrap().resolving=false;
            app.handle(Event::Key(KeyEvent::new(KeyCode::Esc,KeyModifiers::NONE)));assert_eq!(app.input,"Private unsent draft");assert!(app.pending_prompts.is_empty());
        }
    }

    #[test]
    fn remote_sessions_share_native_tabs_and_pending_review_without_local_controls() {
        let mut app = App::default();
        app.remote_mode = true;
        app.size = Rect::new(0, 0, 120, 40);
        for id in ["host~one", "host~two", "host~three"] {
            app.apply_worker_frame(crate::worker_frames::WorkerFrame::Daemon {
                session_id: id.into(),
                frame: json!({"type":"hello","session_id":id,"title":id,
                    "engine":"codex","model":"gpt-6-sol","remote":true}),
            });
        }
        assert_eq!(app.groups[0].tabs, ["host~one"]);
        assert_eq!(app.chips(0)[0], ("remote", "Remote · host".into()));
        app.apply_worker_frame(crate::worker_frames::WorkerFrame::RemoteSnapshot {
            session_id: "host~one".into(),
            markdown: "**You:**\n\nQuestion\n\n**Assistant:**\n\nAnswer\n\n".into(),
            pending_inputs: json!([{"id":"review-1","kind":"permission","title":"Approve?"}]),
            pending_inputs_complete: true,
            before:Some(100),has_more:true,
        });
        assert!(app.sessions[0].transcript.contains("Answer"));
        assert!(app.input_requests.iter().any(|request| request.session_id == "host~one"));
        app.local_attach("");
        assert!(app.attach_picker.is_some());
        app.open_selected_attach();
        assert_eq!(app.pending_attaches.len(), 1);
        assert_ne!(app.pending_attaches[0].0, "host~one");
        app.input = "/model".into();
        assert!(app.dispatch_prompt_command());
        assert_eq!(app.input, "/model");
        assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn mixed_remote_tab_uses_remote_controls_and_does_not_change_saved_local_layout() {
        let mut app=App::default();app.size=Rect::new(0,0,120,40);
        app.apply_worker_frame(crate::worker_frames::WorkerFrame::Daemon {session_id:"local".into(),
            frame:json!({"type":"hello","session_id":"local","title":"Local","engine":"codex","model":"gpt"})});
        let saved=crate::ui_state::LayoutSignature::capture(&app);
        app.apply_worker_frame(crate::worker_frames::WorkerFrame::Daemon {session_id:"host~remote".into(),
            frame:json!({"type":"hello","session_id":"host~remote","title":"Remote",
                "engine":"codex","model":"gpt","remote":true,"cwd":"Remote · host"})});
        app.groups[0].tabs.push("host~remote".into());app.groups[0].active=1;
        assert!(app.active_remote());
        assert!(app.tab_title("host~remote").starts_with("◎ "));
        assert_eq!(app.chips(0)[0],("remote","Remote · host".into()));
        assert_eq!(crate::ui_state::LayoutSignature::capture(&app),saved);
        app.input="/model".into();assert!(app.dispatch_prompt_command());
        assert!(app.notice.contains("session host"));
        app.input="/local".into();assert!(app.submit_local_command());
        assert_eq!(app.groups[0].active_id(),Some("local"));
    }

    #[test]
    fn remote_history_pages_prepend_in_a_scrollable_submenu() {
        let mut app=App::default();app.remote_mode=true;app.size=Rect::new(0,0,120,40);
        let id="host~session";
        app.apply_worker_frame(crate::worker_frames::WorkerFrame::Daemon {session_id:id.into(),
            frame:json!({"type":"hello","session_id":id,"title":"Remote","remote":true})});
        app.apply_worker_frame(crate::worker_frames::WorkerFrame::RemoteSnapshot {session_id:id.into(),
            markdown:"recent turn".into(),pending_inputs:json!([]),pending_inputs_complete:true,
            before:Some(100),has_more:true});
        app.open_history();
        assert_eq!(app.chip_info.as_ref().unwrap().kind,"remote_history");
        app.chip_info.as_mut().unwrap().scroll=0;
        app.handle(Event::Key(KeyEvent::new(KeyCode::PageUp,KeyModifiers::NONE)));
        assert_eq!(app.pending_remote_history,vec![(id.to_owned(),100)]);
        app.apply_worker_frame(crate::worker_frames::WorkerFrame::RemoteHistoryPage {session_id:id.into(),
            markdown:"older turn".into(),before:None,has_more:false,error:None});
        assert_eq!(app.chip_info.as_ref().unwrap().lines[0],"older turn");
        assert!(!app.remote_history_before.contains_key(id));
    }
