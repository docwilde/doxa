//! One bounded local-only background scan, shared by startup, retained tabs and choosers.
use super::App;
use crate::provider_auth::{self, AuthState};
use std::{
    collections::{HashMap, HashSet},
    sync::mpsc::{self, Receiver, TryRecvError},
    time::{Duration, Instant},
};

#[derive(Default)]
pub(super) struct Status {
    states: HashMap<&'static str, AuthState>,
    pending: Option<Receiver<[(&'static str, AuthState); 2]>>,
    pending_started: Option<Instant>,
    checked: Option<Instant>,
    enabled: bool,
    rejected_sessions: HashSet<String>,
}
impl Status {
    fn invalidate(&mut self) {
        self.checked = None;
    }
    fn apply(&mut self, rows: [(&'static str, AuthState); 2], now: Instant) -> bool {
        let changed = rows
            .iter()
            .any(|(name, state)| self.states.get(name) != Some(state));
        self.states = rows.into_iter().collect();
        self.checked = Some(now);
        self.enabled = true;
        changed
    }
    pub(super) fn hint(&self, provider: &str) -> Option<String> {
        if !self.enabled || !matches!(provider, "claude" | "codex") {
            return None;
        }
        let state = self
            .states
            .get(provider)
            .copied()
            .unwrap_or(AuthState::Unknown);
        state
            .warning()
            .then(|| format!("Host {provider} {} · /setup", state.text()))
    }
    fn poll(&mut self, now: Instant) -> bool {
        self.enabled = true;
        let mut changed = false;
        if let Some(job) = self.pending.as_ref() {
            match job.try_recv() {
                Ok(rows) => {
                    self.pending = None;
                    self.pending_started = None;
                    changed |= self.apply(rows, now);
                }
                Err(TryRecvError::Disconnected) => {
                    self.pending = None;
                    self.pending_started = None;
                    changed |= self.apply(
                        [
                            ("claude", AuthState::Unknown),
                            ("codex", AuthState::Unknown),
                        ],
                        now,
                    );
                }
                Err(TryRecvError::Empty) => {
                    return if self.pending_started.is_some_and(|started| {
                        now.saturating_duration_since(started) >= Duration::from_secs(2)
                    }) {
                        // A pathological home filesystem cannot block the UI or
                        // accumulate replacement threads. Keep this sole worker.
                        self.apply(
                            [
                                ("claude", AuthState::TimedOut),
                                ("codex", AuthState::TimedOut),
                            ],
                            now,
                        )
                    } else {
                        false
                    }
                }
            }
        }
        if self
            .checked
            .is_none_or(|at| now.saturating_duration_since(at) >= Duration::from_secs(5))
            && !cfg!(test)
        {
            let (tx, receiver) = mpsc::sync_channel(1);
            if std::thread::Builder::new()
                .name("host-login-status".into())
                .spawn(move || {
                    let _ = tx.send(provider_auth::snapshot());
                })
                .is_ok()
            {
                self.pending = Some(receiver);
                self.pending_started = Some(now);
            }
            self.checked = Some(now);
        }
        changed
    }
}

// Existing native providers emit scrubbed terminal diagnostics, not a common
// typed auth code. Match specific authentication failures only, and display
// static text rather than copying any diagnostic into a login notice.
fn rejected(error: &str) -> bool {
    if error.len() > 16 * 1024 {
        return false;
    }
    let error = error.to_ascii_lowercase();
    [
        "authentication expired",
        "authentication failed",
        "authentication unavailable",
        "failed to authenticate",
        "not authenticated",
        "not logged in",
        "invalid api key",
        "invalid authentication",
        "authentication token is invalid",
        "401 unauthorized",
        "access token has expired",
        "access token expired",
        "oauth session expired",
        "refresh token has expired",
        "refresh token was already used",
    ]
    .iter()
    .any(|reason| error.contains(reason))
}

impl App {
    pub(super) fn poll_host_auth(&mut self) -> bool {
        if self.remote_mode {
            return false;
        }
        self.host_auth.poll(Instant::now())
    }
    pub(super) fn invalidate_host_auth(&mut self) {
        self.host_auth.invalidate();
    }
    pub(super) fn observe_session_auth(&mut self, id: &str, event: &str, data: &serde_json::Value) {
        if !self.sessions.iter().any(|session| session.id == id)
            || !self
                .session_identity
                .get(id)
                .and_then(|identity| identity.0.as_deref())
                .is_some_and(|provider| matches!(provider, "claude" | "codex"))
        {
            return;
        }
        let failure = match event {
            "turn_done" if data["is_error"] == true => data["error"].as_str(),
            "turn_refused" => data["message"].as_str(),
            _ => None,
        };
        if failure.is_some_and(rejected) {
            self.host_auth.rejected_sessions.insert(id.to_owned());
        } else if event == "turn_done" && data["is_error"] == false && data["operation"].is_null() {
            // Admission, reconnect and a fresh global credential file are not
            // proof that this daemon's private credential snapshot recovered.
            self.host_auth.rejected_sessions.remove(id);
        }
    }
    pub(super) fn host_auth_hint(&self, session: Option<&str>) -> Option<String> {
        if self.active_remote() {
            return None;
        }
        if let Some(form) = &self.new_session {
            return self.host_auth.hint(super::engine_name(form.engine));
        }
        if self.engine_picker {
            return self
                .host_auth
                .hint(super::ENGINE_CHOICES[self.engine_selected]);
        }
        if let Some(id) = session {
            if self.host_auth.rejected_sessions.contains(id) {
                return Some("Session login expired or rejected · /setup; retry/reconnect to verify recovery".into());
            }
            return self
                .session_identity
                .get(id)
                .and_then(|identity| identity.0.as_deref())
                .and_then(|provider| self.host_auth.hint(provider));
        }
        self.host_auth
            .hint("claude")
            .or_else(|| self.host_auth.hint("codex"))
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::launch;
    #[test]
    fn expired_login_is_visible_before_first_prompt_in_startup_and_new_session() {
        let mut app = App::default();
        app.host_auth.apply(
            [
                ("claude", AuthState::Expired),
                ("codex", AuthState::Present),
            ],
            Instant::now(),
        );
        assert!(app.host_auth_hint(None).unwrap().contains("expired"));
        app.new_session = Some(super::super::NewSession {
            isolation: doxa_isolation::Profile::Native,
            engine: launch::Engine::Claude,
            router_config: None,
            model: String::new(),
            models: Vec::new(),
            model_efforts: HashMap::new(),
            catalog_note: "Models ready".into(),
            catalog_pending: false,
            launch_error: None,
            retry_allowed: true,
            effort: None,
            prompt: String::new(),
            field: 0,
        });
        assert!(app.host_auth_hint(None).unwrap().contains("expired"));
        assert!(app.pending_prompts.is_empty());
        app.handle(crossterm::event::Event::Resize(140, 40));
        let mut terminal =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(140, 40)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(screen.contains("Host claude login expired"), "{screen}");
    }
    #[test]
    fn reauthentication_clears_host_warning_for_retained_session_without_discarding_it() {
        let mut app = App::default();
        app.apply_daemon_frame(
            &serde_json::json!({"type":"hello","session_id":"retained","engine":"codex"}),
        );
        app.host_auth.apply(
            [
                ("claude", AuthState::Present),
                ("codex", AuthState::RefreshRequired),
            ],
            Instant::now(),
        );
        assert!(app
            .host_auth_hint(Some("retained"))
            .unwrap()
            .contains("recovery unverified"));
        app.host_auth.apply(
            [
                ("claude", AuthState::Present),
                ("codex", AuthState::Present),
            ],
            Instant::now(),
        );
        assert!(app.host_auth_hint(Some("retained")).is_none());
        assert!(app.sessions.iter().any(|session| session.id == "retained"));
        // This says only the host file recovered, not that an already running
        // daemon's private credential snapshot was refreshed or a turn succeeded.
    }
    #[test]
    fn one_pending_scan_does_not_block_ui_or_spawn_another_and_vendor_keys_are_unaffected() {
        let mut status = Status::default();
        let (_tx, rx) = mpsc::sync_channel(1);
        status.pending = Some(rx);
        let start = Instant::now();
        assert!(!status.poll(start));
        assert!(start.elapsed() < Duration::from_secs(1));
        assert!(status.pending.is_some());
        assert!(status.hint("deepseek").is_none());
        status.pending_started = Some(start);
        assert!(status.poll(start + Duration::from_secs(2)));
        assert!(status.hint("codex").unwrap().contains("timed out"));
        assert!(status.pending.is_some());
    }
    #[test]
    fn host_reauthentication_and_reconnect_cannot_hide_rejected_private_session_login() {
        for provider in ["claude", "codex"] {
            let mut app = App::default();
            let hello =
                serde_json::json!({"type":"hello","session_id":"private","engine":provider});
            app.apply_daemon_frame(&hello);
            let event = |kind: &str, data: serde_json::Value| serde_json::json!({"type":"event","session_id":"private","event":{"type":kind,"data":data}});
            let failure = if provider == "claude" {
                "Claude authentication expired or was rejected. PRIVATE_DIAGNOSTIC"
            } else {
                "Your access token has expired. PRIVATE_DIAGNOSTIC"
            };
            app.apply_daemon_frame(&event(
                "turn_done",
                serde_json::json!({"is_error":true,"error":failure}),
            ));
            app.host_auth.apply(
                [
                    ("claude", AuthState::Present),
                    ("codex", AuthState::Present),
                ],
                Instant::now(),
            );
            app.apply_daemon_frame(&hello);
            app.apply_daemon_frame(&event("prompt_queued", serde_json::json!({})));
            let hint = app.host_auth_hint(Some("private")).unwrap();
            assert!(hint.contains("Session login expired or rejected"));
            assert!(!hint.contains("PRIVATE_DIAGNOSTIC"));
            app.apply_daemon_frame(&event(
                "turn_done",
                serde_json::json!({"is_error":true,"error":"network unavailable"}),
            ));
            assert!(app.host_auth_hint(Some("private")).is_some());
            app.apply_daemon_frame(&event(
                "turn_done",
                serde_json::json!({"is_error":false,"operation":"compact"}),
            ));
            assert!(app.host_auth_hint(Some("private")).is_some());
            app.apply_daemon_frame(&event("turn_done", serde_json::json!({"is_error":false})));
            assert!(app.host_auth_hint(Some("private")).is_none());
            assert_eq!(app.sessions.len(), 1);
        }
    }
    #[test]
    fn unrelated_provider_failures_are_not_reported_as_expired_login() {
        for error in [
            "quota exceeded",
            "network unavailable",
            "access denied for file",
            "Codex protected contract unavailable",
        ] {
            assert!(!rejected(error), "{error}");
        }
    }
}
