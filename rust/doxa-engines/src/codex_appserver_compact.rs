//! Child implementation module included by codex_appserver. Uses only the
//! official thread/compact/start API; no prompt is sent to the model.
use super::*;
use crate::codex_compact::ReviewOutcome;

impl AppServerDriver {
    pub async fn compact(&mut self, cancel: &CancellationToken, mut emit: impl FnMut(EngineEvent)) -> Result<(), AppServerError> {
        if !self.compact_gate.as_ref().is_some_and(|gate| gate.verified()) {
            return Err(AppServerError::Protocol("Codex reviewed compaction gate is unavailable"));
        }
        if cancel.is_cancelled() { return Err(AppServerError::Cancelled); }
        let thread = self.thread_id().to_owned();
        let deadline = tokio::time::Instant::now() + self.options.turn_timeout;
        let request = self.send_request_bounded("thread/compact/start", json!({"threadId":thread}), Some(cancel), deadline).await?;
        self.wait_response(request, Some(cancel), deadline, false).await?;
        let mut turn: Option<String> = None;
        let mut reviewed = false;
        let mut completed = false;
        loop {
            let frame = if let Some((frame, bytes)) = self.pending_notifications.pop_front() {
                self.pending_bytes = self.pending_bytes.saturating_sub(bytes); frame
            } else {
                tokio::select! {
                    frame = self.read_frame() => frame?,
                    _ = cancel.cancelled() => {
                        if let Some(id) = &turn { let _ = self.send_request_bounded("turn/interrupt", json!({"threadId":thread,"turnId":id}), None, tokio::time::Instant::now()+Duration::from_millis(200)).await; }
                        return Err(AppServerError::Cancelled);
                    }
                    _ = tokio::time::sleep_until(deadline) => { self.kill_group(); return Err(AppServerError::TimedOut); }
                }
            };
            let Some(method) = frame["method"].as_str() else { continue };
            if frame.get("id").is_some() { self.deny_server_request(&frame, Some(cancel), deadline).await?; continue; }
            let params = &frame["params"];
            if params["threadId"].as_str() != Some(&thread) { continue; }
            if method == "turn/started" {
                let id = params["turn"]["id"].as_str().filter(|id| valid_thread_id(id)).ok_or(AppServerError::Protocol("compact turn lacks a valid ID"))?;
                if turn.as_deref().is_some_and(|known| known != id) { self.kill_group(); return Err(AppServerError::Protocol("overlapping compact turns")); }
                turn = Some(id.to_owned()); self.turn_id = turn.clone();
                emit(EngineEvent::new("compaction_started", json!({"provider_turn":id}))); continue;
            }
            let Some(id) = turn.as_deref() else { continue };
            let matching = if method == "turn/completed" { params["turn"]["id"].as_str() == Some(id) } else { params["turnId"].as_str() == Some(id) };
            if !matching { continue; }
            match method {
                "hook/completed" => {
                    let outcome = self.compact_gate.as_mut().map(|gate| gate.observe_completion(&params["run"])).unwrap_or(ReviewOutcome::Failed);
                    match outcome {
                        ReviewOutcome::Reviewed => { reviewed = true; emit(EngineEvent::new("lore_review_completed", json!({"before":"compaction"}))); }
                        ReviewOutcome::Blocked => { return Err(AppServerError::Server("LORE review blocked Codex compaction".into())); }
                        ReviewOutcome::Failed => { self.kill_group(); return Err(AppServerError::Protocol("Codex PreCompact hook failed; protected session stopped")); }
                        ReviewOutcome::Unrelated => {}
                    }
                }
                "item/completed" if params["item"]["type"] == "contextCompaction" => {
                    if !reviewed { self.kill_group(); return Err(AppServerError::Protocol("compaction completed without verified LORE review")); }
                    completed = true;
                }
                "turn/completed" => {
                    if params["turn"]["status"] != "completed" || !reviewed || !completed {
                        return Err(AppServerError::Protocol("reviewed compaction did not complete"));
                    }
                    self.turn_id = None;
                    emit(EngineEvent::new("compaction_done", json!({"provider_turn":id,"reviewed":true})));
                    emit(EngineEvent::new("turn_done", json!({"is_error":false,"operation":"compact","reviewed":true})));
                    return Ok(());
                }
                "error" => return Err(AppServerError::Server("Codex compaction failed".into())),
                _ => {}
            }
        }
    }
}
