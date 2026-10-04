//! Child implementation module included by codex_appserver. Uses only the
//! official thread/compact/start API; no prompt is sent to the model.
use super::*;
use crate::codex_compact::ReviewOutcome;

impl AppServerDriver {
    pub async fn compact(&mut self, cancel: &CancellationToken, mut emit: impl FnMut(EngineEvent)) -> Result<(), AppServerError> {
        if !self.compact_gate.as_ref().is_some_and(|gate| gate.verified()) {
            return Err(AppServerError::Protocol("Codex reviewed compaction gate is unavailable"));
        }
        if cancel.is_cancelled() { return Err(AppServerError::CompactionCancelled); }
        if self.turn_id.is_some() { return Err(AppServerError::Protocol("cannot compact during an active turn")); }
        // Obtain the official bound rollout path without hydrating history.
        let thread = self.thread_id().to_owned();
        let deadline = tokio::time::Instant::now() + RPC_TIMEOUT.min(self.options.turn_timeout);
        let result = async {
            let request = self.send_request_bounded("thread/read", json!({"threadId":thread,"includeTurns":false}), Some(cancel), deadline).await?;
            self.wait_response(request, Some(cancel), deadline, false).await
        }.await;
        let result = match result {
            Err(AppServerError::Cancelled) => {
                // read_frame may have consumed part of a frame. Retire this
                // transport; no compact RPC was sent and the thread is resumable.
                self.kill_group();
                return Err(AppServerError::CompactionCancelled);
            }
            other => other?,
        };
        if result["thread"]["id"] != thread { return Err(AppServerError::Protocol("compact source belongs to a different thread")); }
        let path = result["thread"]["path"].as_str().map(std::path::PathBuf::from)
            .ok_or(AppServerError::CompactionBlocked)?;
        let gate = self.compact_gate.as_ref().expect("verified gate");
        let job = gate.manual_review_job(&path).ok().flatten().ok_or(AppServerError::CompactionBlocked)?;
        let carrier = gate.pinned_carrier().map_err(|_| AppServerError::CompactionBlocked)?;
        emit(EngineEvent::new("lore_review_started", json!({"before":"compaction"})));
        let token = cancel.clone();
        let reviewed_job = tokio::task::spawn_blocking(move || {
            let approved = crate::review_worker::review_pinned(&carrier, &job.metadata, "codex", crate::review_worker::REVIEW_TIMEOUT, || token.is_cancelled())
                .unwrap_or(false);
            (approved && job.unchanged().unwrap_or(false)).then_some(job)
        }).await.map_err(|_| AppServerError::CompactionBlocked)?;
        if cancel.is_cancelled() { return Err(AppServerError::CompactionCancelled); }
        let job = reviewed_job.ok_or(AppServerError::CompactionBlocked)?;
        if !job.unchanged().unwrap_or(false) { return Err(AppServerError::CompactionBlocked); }
        emit(EngineEvent::new("lore_review_completed", json!({"before":"compaction","source":"native_pre_request"})));
        self.compact_approved(cancel, emit, job).await
    }

    async fn compact_approved(&mut self, cancel: &CancellationToken, mut emit: impl FnMut(EngineEvent), job: crate::compact_hook::ReviewJob) -> Result<(), AppServerError> {
        if cancel.is_cancelled() { return Err(AppServerError::CompactionCancelled); }
        // The private job cannot be created by adapter consumers. Recheck the
        // approved exact source immediately before submitting compaction.
        if !job.unchanged().unwrap_or(false)
            || self.compact_gate.as_ref().is_none_or(|gate| !gate.verified() || gate.pinned_carrier().is_err()) {
            return Err(AppServerError::CompactionBlocked);
        }
        if cancel.is_cancelled() { return Err(AppServerError::CompactionCancelled); }
        self.usage=None;
        let thread = self.thread_id().to_owned();
        let deadline = tokio::time::Instant::now() + self.options.turn_timeout;
        let request = self.send_request_bounded("thread/compact/start", json!({"threadId":thread}), Some(cancel), deadline).await?;
        self.wait_response(request, Some(cancel), deadline, false).await?;
        let mut turn: Option<String> = None;
        let mut reviewed = false;
        let mut completed = false;
        let mut blocked = false;
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
                "thread/tokenUsage/updated"=>{self.usage=Some(params["tokenUsage"].clone());}
                "hook/completed" => {
                    let outcome = self.compact_gate.as_mut().map(|gate| gate.observe_completion(&params["run"])).unwrap_or(ReviewOutcome::Failed);
                    match outcome {
                        ReviewOutcome::Reviewed => { reviewed = true; emit(EngineEvent::new("lore_review_completed", json!({"before":"compaction"}))); }
                        ReviewOutcome::Blocked => { blocked = true; }
                        ReviewOutcome::Failed => { self.kill_group(); return Err(AppServerError::Protocol("Codex PreCompact hook failed; protected session stopped")); }
                        ReviewOutcome::Unrelated => {}
                    }
                }
                "item/completed" if params["item"]["type"] == "contextCompaction" => {
                    if blocked || !reviewed { self.kill_group(); return Err(AppServerError::Protocol("compaction completed without verified LORE review")); }
                    completed = true;
                }
                "turn/completed" => {
                    if blocked && !completed {
                        if self.usage.as_ref().is_some_and(|usage|usage["last"]["inputTokens"].as_u64()!=Some(0)||usage["last"]["outputTokens"].as_u64()!=Some(0)){
                            self.kill_group();return Err(AppServerError::Protocol("blocked compaction unexpectedly reported provider usage"));
                        }
                        self.turn_id = None;
                        return Err(AppServerError::CompactionBlocked);
                    }
                    if params["turn"]["status"] != "completed" || !reviewed || !completed {
                        return Err(AppServerError::Protocol("reviewed compaction did not complete"));
                    }
                    self.turn_id = None;
                    emit(EngineEvent::new("compaction_done", json!({"provider_turn":id,"reviewed":true})));
                    let total=self.usage.as_ref().map(|usage|&usage["total"]);
                    emit(EngineEvent::new("turn_done", json!({"is_error":false,"operation":"compact","reviewed":true,
                        "model":self.effective_model,"model_consistent":self.effective_model.is_some()&&self.effective_model==self.options.model,
                        "usage_scope":"session","usage_source":"codex_app_server_token_usage_updated",
                        "usage_complete":total.is_some_and(|value|value["inputTokens"].as_u64().is_some()&&value["outputTokens"].as_u64().is_some()),
                        "input_tokens":total.and_then(|value|value["inputTokens"].as_u64()),
                        "output_tokens":total.and_then(|value|value["outputTokens"].as_u64()),
                        "cache_read_input_tokens":total.and_then(|value|value["cachedInputTokens"].as_u64()),
                        "cost_usd":null,"session_cost_usd":null})));
                    return Ok(());
                }
                "error" => return Err(AppServerError::Server("Codex compaction failed".into())),
                _ => {}
            }
        }
    }
}

// CompactGate pins the running carrier through /proc/self/exe on Linux.
#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use crate::codex_compact::CompactGate;
    use std::{fs, os::unix::fs::PermissionsExt};
    include!("../tests/fixtures/codex_protected_fixture.rs");

    // The test oracle replaces only canonical review, after the same owned
    // source parsing and proof checks as the runtime. Private submission is
    // deliberately inaccessible to consumers of AppServerDriver.
    async fn approved_compact(driver: &mut AppServerDriver, cancel: &CancellationToken, emit: impl FnMut(EngineEvent)) -> Result<(), AppServerError> {
        let result = driver.request("thread/read", json!({"threadId":driver.thread_id(),"includeTurns":false})).await?;
        let path = std::path::Path::new(result["thread"]["path"].as_str().unwrap());
        let job = driver.compact_gate.as_ref().unwrap().manual_review_job(path).unwrap().unwrap();
        assert!(job.metadata["expected_source"]["sha256"].as_str().is_some());
        driver.compact_approved(cancel, emit, job).await
    }
#[tokio::test]
async fn manual_compaction_binds_actual_thread_and_requires_review_before_completion() {
    for mode in ["compact", "order", "failed", "foreign", "stale-turn", "blocked"] {
        let (dir, options, gate) = fixture(mode);
        let mut driver = AppServerDriver::spawn_protected(options, str::to_owned, false, gate).await.unwrap();
        let manifest:Value=serde_json::from_slice(&fs::read(dir.path().join("gate/compact-session.json")).unwrap()).unwrap();
        assert_eq!(manifest["provider_thread"], "thread-actual");
        let mut events=Vec::new();
        let result=approved_compact(&mut driver, &CancellationToken::new(), |e| events.push(e)).await;
        if mode == "blocked" {
            assert!(matches!(result,Err(crate::codex_appserver::AppServerError::CompactionBlocked)));
            assert_eq!(driver.thread_id(),"thread-actual");
            assert!(driver.run_turn("fixture followup",&CancellationToken::new(),|_|{}).await.is_ok());
            assert!(dir.path().join("usable-after-blocked").exists());
        }
        assert_eq!(result.is_ok(), mode=="compact");
        assert_eq!(events.iter().any(|e| e.kind=="compaction_done"),mode=="compact");
        if mode=="compact"{let done=events.iter().find(|event|event.kind=="turn_done").unwrap();assert_eq!(done.data["usage_complete"],true);assert_eq!(done.data["input_tokens"],10);assert_eq!(done.data["output_tokens"],20);}
        if mode=="failed" {
            tokio::time::sleep(Duration::from_millis(400)).await;
            assert!(!dir.path().join("survived-failed-hook").exists());
            assert!(driver.compact(&CancellationToken::new(), |_|{}).await.is_err());
        }
        driver.shutdown().await;
    }
}

#[tokio::test]
async fn manual_compaction_never_invents_missing_or_inconsistent_accounting(){
    for mode in ["compact-no-usage","blocked-usage"]{
        let (_dir,options,gate)=fixture(mode);
        let mut driver=AppServerDriver::spawn_protected(options,str::to_owned,false,gate).await.unwrap();
        let mut events=vec![];let result=approved_compact(&mut driver, &CancellationToken::new(),|event|events.push(event)).await;
        if mode=="compact-no-usage"{assert!(result.is_ok());let done=events.iter().find(|event|event.kind=="turn_done").unwrap();assert_eq!(done.data["usage_complete"],false);assert!(done.data["input_tokens"].is_null());}
        else{assert!(result.is_err());assert!(!matches!(result,Err(crate::codex_appserver::AppServerError::CompactionBlocked)));assert!(!events.iter().any(|event|event.kind=="compaction_done"));}
        driver.shutdown().await;
    }
}

#[tokio::test]
async fn changed_native_snapshot_refuses_before_compact_rpc() {
    let (dir, options, gate) = fixture("native-review-failure");
    let mut driver = AppServerDriver::spawn_protected(options, str::to_owned, false, gate).await.unwrap();
    let result = driver.request("thread/read", json!({"threadId":driver.thread_id(),"includeTurns":false})).await.unwrap();
    let source = std::path::Path::new(result["thread"]["path"].as_str().unwrap());
    let job = driver.compact_gate.as_ref().unwrap().manual_review_job(source).unwrap().unwrap();
    fs::write(source, b"changed after review\n").unwrap();
    assert!(matches!(driver.compact_approved(&CancellationToken::new(), |_| {}, job).await, Err(AppServerError::CompactionBlocked)));
    let requests = fs::read_to_string(dir.path().join("requests.jsonl")).unwrap();
    assert!(!requests.contains("thread/compact/start"));
    assert_eq!(driver.thread_id(), "thread-actual");
    driver.shutdown().await;
}

}
