//! Compatibility contracts for the audited DOXA 1.19 interaction paths.
use crossterm::event::{Event, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use std::sync::mpsc;
use super::*;
    use serde_json::json;
    fn paint(app: &App) -> String {
        let mut terminal = ratatui::Terminal::new(ratatui::backend::TestBackend::new(app.size.width, app.size.height)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        terminal.backend().buffer().content.iter().map(|cell| cell.symbol()).collect()
    }
    fn click(app: &mut App, column: u16, row: u16) {
        app.handle(Event::Mouse(MouseEvent { kind: MouseEventKind::Down(MouseButton::Left),
            column, row, modifiers: KeyModifiers::NONE }));
    }
    #[test]
    fn startup_defaults_to_selected_legacy_and_preserves_explicit_protocol_overrides() {
        assert_eq!(selected_keyboard_protocol(None),KeyboardProtocol::Legacy);
        assert_eq!(selected_keyboard_protocol(Some("legacy")),KeyboardProtocol::Legacy);
        assert_eq!(selected_keyboard_protocol(Some("kitty")),KeyboardProtocol::Kitty);
        assert_eq!(selected_keyboard_protocol(Some("unknown")),KeyboardProtocol::Unknown);
        assert_eq!(selected_keyboard_protocol(Some("")),KeyboardProtocol::Legacy);
        assert_eq!(selected_keyboard_protocol(Some("unrecognized")),KeyboardProtocol::Legacy);
    }

    #[test]
    fn native_selection_keyboard_copy_only_exports_visible_selected_cells_and_escape_clears() {
        let mut app=App::default();app.size=Rect::new(0,0,100,28);app.rail_visible=false;
        app.sessions.push(Session{id:"s".into(),title:"fixture".into(),transcript:"plain alpha 界 beta\nnext line".into(),collection:String::new(),status:String::new()});app.groups[0].tabs=vec!["s".into()];
        let _=paint(&app);let pane=app.layout(app.size).body;let rect=app.pane_regions(0,pane)[1];
        let start=ratatui::layout::Position::new(rect.x+1,rect.y);let end=ratatui::layout::Position::new(rect.x+5,rect.y);
        app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Down(MouseButton::Left),column:start.x,row:start.y,modifiers:KeyModifiers::NONE}));
        app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Drag(MouseButton::Left),column:end.x,row:end.y,modifiers:KeyModifiers::NONE}));
        let owner=crate::selection::Owner{pane:0,session:"s".into()};let text=app.transcript_selection.borrow().text(&owner).unwrap();assert_eq!(text,"plain");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('c'),KeyModifiers::CONTROL)));assert_eq!(app.pending_clipboard_copy.take(),Some(crate::clipboard::osc52("plain")));assert!(!app.should_quit);assert!(app.pending_prompts.is_empty());
        let _=paint(&app);app.handle(Event::Key(KeyEvent::new(KeyCode::Esc,KeyModifiers::NONE)));assert!(app.transcript_selection.borrow().text(&owner).is_none());
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('C'),KeyModifiers::CONTROL|KeyModifiers::SHIFT)));assert!(app.pending_clipboard_copy.is_none());
    }
    #[test]
    fn selection_in_second_pane_keeps_exact_owner_and_prompt_click_still_types() {
        let mut app=App::default();app.size=Rect::new(0,0,180,40);app.rail_visible=false;
        for id in ["a","b"]{app.sessions.push(Session{id:id.into(),title:id.into(),collection:String::new(),transcript:format!("{id} visible line"),status:String::new()});}
        app.groups[0].tabs=vec!["a".into()];app.groups[1].tabs=vec!["b".into()];let _=paint(&app);
        let pane=app.layout(app.size).panes.unwrap()[1];let regions=app.pane_regions(1,pane);let rect=regions[1];
        for(kind,column)in[(MouseEventKind::Down(MouseButton::Left),rect.x+1),(MouseEventKind::Drag(MouseButton::Left),rect.x+3)]{app.handle(Event::Mouse(MouseEvent{kind,column,row:rect.y,modifiers:KeyModifiers::NONE}));}
        assert_eq!(app.active_group,1);assert_eq!(app.transcript_selection.borrow().text(&crate::selection::Owner{pane:1,session:"b".into()}).as_deref(),Some("b v"));
        app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Down(MouseButton::Left),column:regions[4].x+2,row:regions[4].y+1,modifiers:KeyModifiers::NONE}));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('x'),KeyModifiers::NONE)));assert_eq!(app.input,"x");assert_eq!(app.focus,Focus::Prompt);
    }

    #[test]
    fn clipboard_paste_targets_original_draft_without_submission_or_control_sequences() {
        let mut app=App::default();app.groups[0].tabs=vec!["a".into()];app.groups[1].tabs=vec!["b".into()];app.input="left".into();app.input_cursor=2;
        let target=app.clipboard_target();app.clipboard_job=Some(crate::clipboard::Job::fixture(target,Ok("X\r\nY\u{1b}\u{7}".into())));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab,KeyModifiers::ALT)));assert_eq!(app.active_group,1);
        app.input="right".into();app.input_cursor=5;assert!(app.poll_clipboard());assert_eq!(app.input,"right");assert_eq!(app.input_drafts[&(0,"a".into())].0,"leX\nYft");assert!(app.pending_prompts.is_empty());
        let target=app.clipboard_target();app.clipboard_job=Some(crate::clipboard::Job::fixture(target,Ok("stale".into())));app.input.push('!');app.input_cursor+=1;
        app.poll_clipboard();assert_eq!(app.input,"right!");assert!(app.notice.contains("discarded"));
        let target=app.clipboard_target();app.clipboard_job=Some(crate::clipboard::Job::fixture(target,Ok("closed".into())));app.groups[1].tabs.clear();app.poll_clipboard();assert!(!app.input.contains("closed"));assert!(app.pending_prompts.is_empty());
    }

    #[test]
    fn engine_form_defaults_use_effective_engine_config_without_reusing_live_identity() {
        let config = "model='claude-own'\neffort='max'\n[models]\ncodex='codex-own'\ndeepseek='deepseek-flash'\n".parse::<toml::Table>().unwrap();
        assert_eq!(new_session_preferences(launch::Engine::Claude, &config, None, None), ("claude-own".into(), Some("max".into())));
        assert_eq!(new_session_preferences(launch::Engine::Codex, &config, None, None), ("codex-own".into(), Some("max".into())));
        assert_eq!(new_session_preferences(launch::Engine::Claude, &config, Some("env-model"), Some("low")), ("env-model".into(), Some("low".into())));
        assert_eq!(new_session_preferences(launch::Engine::DeepSeek, &config, None, Some("xhigh")), ("deepseek-flash".into(), Some("high".into())));
        assert_eq!(new_session_preferences(launch::Engine::Codex, &toml::Table::new(), None, None), ("".into(), None));
    }

    #[test]
    fn tab_focus_visits_visible_chips_headers_and_reverses_without_switching_pane() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 220, 32);
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"claude","model":"sonnet","effort":"high"}));
        app.groups[0].tabs = vec!["s".into(), "second".into()];
        app.input = "unsent draft".into(); app.input_cursor = app.input.len();
        let ring = app.focus_ring();
        assert!(ring.contains(&Focus::Tabs)); assert!(ring.contains(&Focus::Chip("engine")));
        for expected in ring.iter().cycle().skip(1).take(ring.len()) {
            app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE)));
            assert_eq!(&app.focus, expected);
            assert_eq!(app.active_group, 0); assert_eq!(app.input, "unsent draft");
        }
        for expected in ring.iter().rev() {
            app.handle(Event::Key(KeyEvent::new(KeyCode::BackTab, KeyModifiers::SHIFT)));
            assert_eq!(&app.focus, expected);
        }
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::SHIFT)));
        assert_eq!(app.focus, *ring.last().unwrap());
        app.focus = Focus::Tabs;
        assert!(paint(&app).contains("● tabs"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Right, KeyModifiers::NONE)));
        assert_eq!(app.groups[0].active_id(), Some("second"));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.focus, Focus::Prompt);
        app.focus = Focus::Chip("engine");
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert!(app.engine_picker); assert!(app.pending_prompts.is_empty());
        let previous = app.focus;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE)));
        assert_eq!(app.focus, previous); // modal owns Tab
    }

    #[test]
    fn focus_ring_skips_hidden_sidebar_and_clipped_chips() {
        let mut app = App::default(); app.size = Rect::new(0, 0, 70, 24); app.rail_visible = false;
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"claude","model":"sonnet","effort":"high"}));
        app.groups[0].tabs = vec!["s".into()];
        let _ = paint(&app);
        let visible = app.rendered_chip_hits.borrow().as_ref().unwrap().iter().map(|hit| Focus::Chip(hit.kind)).collect::<Vec<_>>();
        let ring = app.focus_ring();
        assert!(!ring.contains(&Focus::Rail));
        assert_eq!(ring.iter().copied().filter(|focus| matches!(focus, Focus::Chip(_))).collect::<Vec<_>>(), visible);
        let focused = visible[0]; app.focus = focused;
        let mut terminal = ratatui::Terminal::new(ratatui::backend::TestBackend::new(70, 24)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let hit = app.rendered_chip_hits.borrow().as_ref().unwrap()[0].clone();
        assert!(terminal.backend().buffer()[(hit.rect.x, hit.rect.y)].modifier.contains(Modifier::UNDERLINED));
    }

    #[test]
    fn attached_pending_effort_prevents_a_second_transaction_until_authoritative_clear() {
        let mut app = App::default(); app.size = Rect::new(0, 0, 150, 32);
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"claude","model":"sonnet","effort":"high","pending_effort":"low"}));
        app.groups[0].tabs = vec!["s".into()];
        app.open_effort_picker(); assert!(app.effort_picker.is_none()); assert!(app.notice.contains("awaiting"));
        app.apply_daemon_frame(&json!({"type":"reply","status":{"session_id":"s","effort":"low","pending_effort":null}}));
        assert!(!app.pending_effort_verifications.contains_key("s")); assert_eq!(app.session_efforts["s"], "low");
    }

    #[test]
    fn effort_replies_and_failures_belong_to_requesting_session() {
        let mut app = App::default(); app.size = Rect::new(0, 0, 150, 32);
        for id in ["a", "b"] { app.apply_daemon_frame(&json!({"type":"hello","session_id":id,"engine":"claude","model":"sonnet","effort":"high"})); }
        app.groups[0].tabs = vec!["a".into()];
        app.pending_effort_verifications.insert("b".into(), "low".into());
        app.notice = "active pane notice".into();
        app.apply_daemon_frame(&json!({"type":"set_effort_reply","session_id":"b","ok":true,"effort":"low","verification_pending":true}));
        assert_eq!(app.notice, "active pane notice"); assert_eq!(app.session_efforts["b"], "high");
        assert!(app.apply_daemon_frame(&json!({"type":"set_effort_reply","session_id":"b","ok":true,"effort":"low","verification_pending":false})));
        assert_eq!(app.notice, "active pane notice"); assert_eq!(app.session_efforts["b"], "low");
        assert!(!app.pending_effort_verifications.contains_key("b"));
        app.pending_effort_verifications.insert("a".into(), "low".into());
        app.open_effort_picker(); assert!(app.effort_picker.is_none()); assert!(app.pending_effort_changes.is_empty());
        app.apply_daemon_frame(&json!({"type":"set_effort_reply","session_id":"a","ok":true,"effort":"max","verification_pending":false}));
        assert_eq!(app.session_efforts["a"], "high");
        app.apply_daemon_frame(&json!({"type":"set_effort_reply","session_id":"a","ok":true,"effort":"low","verification_pending":true}));
        assert!(app.notice.contains("awaiting provider"));
        app.apply_daemon_frame(&json!({"type":"event","session_id":"a","event":{"type":"effort_verification_failed","data":{"effort":"high","requested_effort":"low"}}}));
        assert_eq!(app.session_efforts["a"], "high"); assert!(!app.pending_effort_verifications.contains_key("a"));
        assert!(!app.notice.contains("awaiting"));
        // A delayed reply cannot resurrect the failed request.
        app.apply_daemon_frame(&json!({"type":"set_effort_reply","session_id":"a","ok":true,"effort":"low","verification_pending":true}));
        assert!(!app.notice.contains("awaiting"));
        app.pending_effort_verifications.insert("a".into(), "max".into());
        app.apply_daemon_frame(&json!({"type":"set_effort_reply","session_id":"a","ok":true,"effort":"max","verification_pending":false}));
        assert_eq!(app.session_efforts["a"], "max"); assert!(app.notice.contains("verified"));
        app.apply_daemon_frame(&json!({"type":"set_effort_reply","session_id":"a","ok":true,"effort":"max","verification_pending":true}));
        assert!(!app.notice.contains("awaiting"));
        app.pending_effort_verifications.insert("a".into(), "low".into());
        app.apply_daemon_frame(&json!({"type":"event","session_id":"a","event":{"type":"turn_done","data":{"is_error":true}}}));
        assert!(!app.pending_effort_verifications.contains_key("a")); assert_eq!(app.session_efforts["a"], "max");
    }

    #[test]
    fn search_edits_actual_prompt_query_and_restores_saved_draft() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 28);
        app.groups[0].tabs = vec!["s".into()];
        app.input = "unsent draft".into();
        app.input_cursor = app.input.len();
        app.show_history_fixture("find", Vec::new());
        app.history_pending = None;
        let screen = paint(&app);
        assert!(screen.contains("Search sessions ●"));
        assert!(screen.contains("> find▏"));
        assert!(!screen.contains("> unsent draft"));
        app.history_key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
        assert_eq!(app.history_query, "findx");
        app.history_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert_eq!(app.input, "unsent draft");
        assert!(paint(&app).contains("> unsent draft▏"));
    }
    #[test]
    fn context_details_keep_provider_tokens_separate_from_local_chars_and_ignore_other_owner() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 28);
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"claude","model":"sonnet","cwd":"/fixture"}));
        app.groups[0].tabs = vec!["s".into()];
        app.open_diagnostic("context");
        let original = app.chip_info.as_ref().unwrap().lines.clone();
        assert!(!app.apply_daemon_frame(&json!({"type":"context_detail","session_id":"other","ok":true,
            "detail":{"categories":[{"name":"fake","tokens":3}]}})));
        assert_eq!(app.chip_info.as_ref().unwrap().lines, original);
        assert!(app.apply_daemon_frame(&json!({"type":"context_detail","session_id":"s","ok":true,
            "detail":{"source":"Claude official context_usage","categories":[{"name":"system prompt","tokens":123},{"name":"missing"}],
                "memory_files":[{"path":"MEMORY.md","tokens":17}], "agents":[{"agent_type":"reviewer","tokens":12}],
                "lore_snapshot_chars":321,"max_tokens":1000}})));
        let text = app.chip_info.as_ref().unwrap().lines.join("\n");
        assert!(text.contains("system prompt: 123"));
        assert!(text.contains("reviewer: 12 tokens"));
        assert!(text.contains("lore snapshot chars: 321 chars"));
        assert!(!text.contains("missing:"));
        assert!(!text.contains("321 tokens"));
    }
    #[test]
    fn claude_effort_chip_loads_current_capability_without_model_change_and_waits_for_verification() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 220, 32);
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"claude","model":"account-model",
            "effort":"high","can_set_model":true,"cwd":"/fixture"}));
        app.groups[0].tabs = vec!["s".into()];
        paint(&app);
        let hit = app.rendered_chip_hits.borrow().as_ref().unwrap().iter().find(|hit| hit.kind == "effort").unwrap().clone();
        click(&mut app, hit.rect.x + 1, hit.rect.y);
        assert_eq!(app.pending_model_queries, vec!["s"]);
        assert!(app.pending_model_changes.is_empty());
        app.apply_daemon_frame(&json!({"type":"models_reply","session_id":"s","ok":true,"models":["account-model"],
            "capabilities":[{"model":"account-model","efforts":["low","high","max"]}]}));
        assert!(app.model_picker.is_none());
        assert_eq!(app.effort_picker.as_ref().unwrap().levels, ["low", "high", "max"]);
        let menu = app.active_chooser_rect().unwrap();
        click(&mut app, menu.x + 2, menu.y + 3);
        assert_eq!(app.pending_effort_changes, vec![("s".into(), "low".into())]);
        assert_eq!(app.session_efforts.get("s").map(String::as_str), Some("high"));
        app.apply_daemon_frame(&json!({"type":"event","session_id":"s","event":{"type":"effort_requested","data":{"effort":"low"}}}));
        assert_eq!(app.session_efforts.get("s").map(String::as_str), Some("high"));
        app.apply_daemon_frame(&json!({"type":"event","session_id":"s","event":{"type":"effort_verified","data":{"effort":"low"}}}));
        assert_eq!(app.session_efforts.get("s").map(String::as_str), Some("low"));
    }
    #[test]
    fn prompt_search_cursor_edits_utf8_without_touching_draft() {
        let mut app = App::default();
        app.history_modal = true;
        app.history_query = "aü界".into();
        app.history_query_cursor = app.history_query.len();
        app.input = "draft".into();
        app.history_key(KeyEvent::new(KeyCode::Left, KeyModifiers::NONE));
        app.history_key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        assert_eq!(app.history_query, "a界");
        app.history_key(KeyEvent::new(KeyCode::Char('ß'), KeyModifiers::NONE));
        assert_eq!(app.history_query, "aß界");
        app.history_key(KeyEvent::new(KeyCode::Delete, KeyModifiers::NONE));
        assert_eq!(app.history_query, "aß");
        assert_eq!(app.input, "draft");
    }
    #[test]
    fn cancelled_effort_catalog_does_not_reopen_on_async_reply() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 28);
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"s","engine":"codex","model":"m","effort":"high"}));
        app.groups[0].tabs = vec!["s".into()];
        app.open_effort_picker();
        app.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        app.apply_daemon_frame(&json!({"type":"models_reply","session_id":"s","ok":true,"models":["m"],
            "capabilities":[{"model":"m","efforts":["high"]}]}));
        assert!(app.effort_picker.is_none());
        assert!(app.model_picker.is_none());
    }
    #[test]
    fn operations_menu_opens_above_prompt_and_border_click_does_not_start_login() {
        let mut app = App::default();
        app.size = Rect::new(0, 0, 100, 28);
        app.groups[0].tabs = vec!["s".into()];
        app.input = "/login".into();
        app.submit_local_command();
        let area = app.active_chooser_rect().unwrap();
        assert!(paint(&app).contains("Claude (Anthropic)"));
        click(&mut app, area.x, area.y + 2);
        click(&mut app, area.x + 2, area.y + 1);
        assert!(!app.operations_menu.as_ref().unwrap().busy());
        app.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert!(app.operations_menu.is_none());
    }
    #[test]
    fn free_text_uses_stable_question_id_and_preserves_session_draft() {
        let mut app = App::default();
        app.groups[0].tabs = vec!["s".into()];
        app.input = "saved draft".into();
        let data = json!({"id":"r","kind":"ask_user","questions":[{"id":"stable","question":"What?","options":[]}]});
        app.input_requests.push(InputRequest::from_event("s", &data).unwrap());
        app.handle(Event::Paste("héllo".into()));
        app.handle(Event::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)));
        assert_eq!(app.input, "saved draft");
        assert_eq!(app.pending_answers[0].2["answers"]["stable"], "héllo");
    }

    #[test]
    fn hello_snapshot_preserves_only_exact_pending_request() {
        let mut app = App::default();
        let data = json!({"id":"r","kind":"ask_user","questions":[{"id":"q","question":"What?","options":[]}]});
        let mut request = InputRequest::from_event("s", &data).unwrap();
        request.free_text = "draft".into(); request.sending = true;
        app.input_requests.push(request);
        app.restore_pending_inputs("s", &json!({"pending_inputs_complete":true,"pending_inputs":[data.clone()]}));
        assert_eq!(app.input_requests[0].free_text, "draft");
        assert!(app.input_requests[0].sending);
        let mut changed = data; changed["questions"][0]["question"] = json!("Changed?");
        app.restore_pending_inputs("s", &json!({"pending_inputs_complete":true,"pending_inputs":[changed]}));
        assert!(app.input_requests[0].free_text.is_empty());
        assert!(!app.input_requests[0].sending);
        app.restore_pending_inputs("s", &json!({"pending_inputs_complete":false,"pending_inputs":[]}));
        assert!(app.input_requests.is_empty());
    }

    #[test]
    fn full_permission_review_gates_every_approval_path() {
        for code in [KeyCode::Char('a'), KeyCode::Char('A'), KeyCode::Enter] {
            let mut app = App::default(); app.groups[0].tabs = vec!["s".into()];
            app.handle(Event::Resize(100, 30));
            let data = json!({"id":"r","kind":"permission","title":"Review","input_summary":"long review","require_full_review":true});
            app.input_requests.push(InputRequest::from_event("s", &data).unwrap());
            app.request_key(KeyEvent::new(code, KeyModifiers::NONE));
            assert!(app.pending_answers.is_empty());
            app.input_requests[0].review_complete.set(true);
            app.input_requests[0].review_available = false;
            app.request_key(KeyEvent::new(code, KeyModifiers::NONE));
            assert!(app.pending_answers.is_empty());
            app.input_requests[0].review_available = true;
            app.request_key(KeyEvent::new(code, KeyModifiers::NONE));
            assert_eq!(app.pending_answers[0].2, json!({"decision":"allow"}));
        }
    }

    #[test]
    fn permission_menu_is_inline_mouse_selectable_and_owned_by_active_session() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(100, 30));
        app.groups[0].tabs = vec!["s".into(), "other".into()];
        app.input_requests.push(InputRequest::from_event("s", &json!({"id":"r","kind":"permission","title":"Run command?","input_summary":"echo hello"})).unwrap());
        let menu = app.active_chooser_rect().unwrap();
        assert!(menu.y > 0);
        assert!(menu.bottom() < app.size.bottom());
        let row = (menu.y + 1..menu.bottom() - 1).find(|&row| input_request_option_at(&app.input_requests[0], menu, row) == Some(2)).unwrap();
        assert!(app.hover_chooser(menu.x + 2, row));
        assert_eq!(app.input_requests[0].selected, 2);
        app.groups[0].active = 1;
        app.handle(Event::Key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE)));
        assert!(app.pending_answers.is_empty());
        app.groups[0].active = 0;
        click(&mut app, menu.x + 2, row);
        assert_eq!(app.pending_answers, vec![("s".into(), "r".into(), json!({"decision":"deny"}))]);
        click(&mut app, menu.x + 2, menu.y + 1);
        assert_eq!(app.pending_answers.len(), 1);
    }

    #[test]
    fn short_permission_review_pages_contiguously_and_tiny_pane_blocks_allow() {
        let mut app = App::default();
        app.rail_visible = false;
        app.handle(Event::Resize(80, 18));
        app.groups[0].tabs = vec!["s".into()];
        app.input_requests.push(InputRequest::from_event("s", &json!({"id":"r","kind":"permission","input_summary":"review line\n".repeat(50),"require_full_review":true})).unwrap());
        for _ in 0..100 {
            let _ = paint(&app);
            if app.input_requests[0].review_complete.get() { break; }
            app.request_key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        }
        assert!(app.input_requests[0].review_complete.get());
        app.handle(Event::Resize(20, 8));
        app.input_requests[0].require_full_review = false;
        app.request_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE));
        assert!(app.pending_answers.is_empty());
        app.request_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        assert_eq!(app.pending_answers[0].2, json!({"decision":"deny"}));
    }

    #[test]
    fn recursive_panes_focus_and_resize_preserve_tree_and_tabs() {
        let mut app = App::default(); app.rail_visible = false;
        app.handle(Event::Resize(180, 70));
        app.groups[0].tabs = vec!["a".into(),"c".into()]; app.groups[1].tabs = vec!["b".into()];
        app.split_active_pane(Split::Horizontal);
        assert_eq!(app.groups.len(), 3);
        let regions = app.layout(app.size).panes.unwrap();
        assert_eq!(regions.len(), 3);
        let rect = regions[2];
        app.handle(Event::Mouse(MouseEvent {kind:MouseEventKind::Down(MouseButton::Left),column:rect.x+2,row:rect.y+2,modifiers:KeyModifiers::NONE}));
        assert_eq!(app.active_group, 2);
        let tree = app.pane_tree.clone();
        app.handle(Event::Resize(30, 10));
        assert_eq!(app.pane_tree, tree);
        assert_eq!(app.groups[0].tabs, vec!["a","c"]);
        app.handle(Event::Resize(180,70));
        assert_eq!(app.layout(app.size).panes.unwrap().len(),3);
    }

    #[test]
    fn nested_mouse_dividers_resize_both_axes_and_keep_drafts(){
        let mut app=App::default();app.rail_visible=false;app.handle(Event::Resize(180,80));
        app.groups[0].tabs=vec!["a".into()];app.groups[1].tabs=vec!["b".into()];app.active_group=1;
        app.split_active_pane(Split::Horizontal);app.input="keep draft".into();
        let body=app.layout(app.size).body;let regions=app.layout(app.size).panes.unwrap();let x=regions[1].x+10;let boundary=regions[1].bottom();
        app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Down(MouseButton::Left),column:x,row:boundary,modifiers:KeyModifiers::NONE}));
        assert!(matches!(app.drag,Some(DragTarget::NestedPane(_))));
        app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Drag(MouseButton::Left),column:x,row:boundary+10,modifiers:KeyModifiers::NONE}));
        app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Up(MouseButton::Left),column:x,row:boundary+10,modifiers:KeyModifiers::NONE}));
        assert!(app.layout(app.size).panes.unwrap()[1].height>regions[1].height);assert_eq!(app.input,"keep draft");
        let boundary=regions[0].right();let y=body.y+10;
        app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Down(MouseButton::Left),column:boundary,row:y,modifiers:KeyModifiers::NONE}));
        app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Drag(MouseButton::Left),column:boundary-15,row:y,modifiers:KeyModifiers::NONE}));
        app.handle(Event::Mouse(MouseEvent{kind:MouseEventKind::Up(MouseButton::Left),column:boundary-15,row:y,modifiers:KeyModifiers::NONE}));
        assert!(app.layout(app.size).panes.unwrap()[0].width<regions[0].width);assert_eq!(app.input,"keep draft");
    }

    #[test]
    fn gallery_fixture_menus_never_write_spawn_or_save_fake_runs(){
        let mut app=App::default();app.handle(Event::Resize(126,31));
        app.apply_daemon_frame(&json!({"type":"hello","session_id":"gallery","engine":"codex","cwd":"/gallery"}));
        app.show_memory_manager_fixture(0,"project",json!({"scope":"project","key":"/gallery","sha256":"f".repeat(64),"entries":["fixture fact"],"chars":12,"cap_chars":8800})).unwrap();
        app.key(KeyEvent::new(KeyCode::Char('e'),KeyModifiers::NONE));app.key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE));
        let mut terminal=Terminal::new(ratatui::backend::TestBackend::new(126,31)).unwrap();terminal.draw(|frame|app.draw(frame)).unwrap();
        app.key(KeyEvent::new(KeyCode::Char('Y'),KeyModifiers::SHIFT));
        assert!(app.memory_manager.as_ref().unwrap().fixture);assert!(!app.memory_manager.as_ref().unwrap().busy());
        app.memory_manager=None;app.chip_info=None;
        app.show_fleet_review_fixture(&json!({"run_id":"gallery-run","root":"/gallery/fleet","mode":"symmetric","sessions":1})).unwrap();
        app.fleet_review.as_mut().unwrap().complete.set(true);app.fleet_review.as_mut().unwrap().armed=true;
        app.key(KeyEvent::new(KeyCode::Char('Y'),KeyModifiers::SHIFT));assert!(app.fleet_controller.is_none());assert!(app.notice.contains("fixture cannot launch"));
        app.show_fleet_view_fixture("gallery-run",&["Fixture status"]);assert!(!app.poll_fleet());assert!(app.fleet_views.is_empty());
    }

    #[test]
    fn session_kill_completion_preserves_active_prompt_and_layout() {
        let mut app = App::default();
        app.apply_update(DaemonUpdate::Upsert(Session { id:"current".into(), title:"Current".into(), collection:String::new(), transcript:String::new(), status:"Idle".into() }));
        app.groups[0].tabs = vec!["current".into()];
        app.input = "/sessions kill current".into(); app.input_cursor = 7;
        let before = app.groups.clone();
        let (tx, rx) = mpsc::sync_channel(1);
        app.session_stop_pending = Some(rx);
        tx.send(crate::sessions::Report { stopped:vec!["current".into()],requested:Vec::new(),failed:Vec::new(),error:None }).unwrap();
        assert!(app.poll_sessions_stop());
        assert_eq!(app.groups[0].tabs, before[0].tabs);
        assert_eq!(app.groups[0].active, before[0].active);
        assert_eq!(app.input, "/sessions kill current"); assert_eq!(app.input_cursor, 7);
        assert!(app.killed_this_run.contains("current"));
        assert!(app.offline_ids.contains("current"));
        assert!(app.notice.contains("stopped: current"));
        let (tx, rx) = mpsc::sync_channel(1); app.session_stop_pending = Some(rx);
        tx.send(crate::sessions::Report { stopped:Vec::new(),requested:vec!["current".into()],failed:Vec::new(),error:None }).unwrap();
        assert!(app.poll_sessions_stop());
        assert!(app.killed_this_run.contains("current"));
        assert_eq!(app.input, "/sessions kill current"); assert_eq!(app.input_cursor, 7);
        assert!(app.notice.contains("teardown unconfirmed"));
        assert!(!app.notice.contains("stopped:"));
    }

    #[test]
    fn malformed_session_kill_form_never_reaches_agent_or_clears_prompt() {
        let mut app = App::default();
        app.input = "/sessions kill one two".into(); app.input_cursor = app.input.len();
        assert!(app.dispatch_prompt_command());
        assert!(app.notice.contains("usage:"));
        assert_eq!(app.input, "/sessions kill one two");
        assert!(app.session_stop_pending.is_none());
    }

