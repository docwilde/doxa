//! Official Codex 0.156.1 server requests mapped to DOXA's input protocol.
//! Replies are single use and apply only to the currently displayed request.
use std::sync::{atomic::{AtomicU64, Ordering}, Mutex};
use serde_json::{json, Map, Value};
use tokio::sync::oneshot;
use crate::EngineEvent;

const MAX_REQUEST_BYTES: usize = 32 * 1024;
const MAX_ANSWER_BYTES: usize = 16 * 1024;

#[derive(Default)]
pub struct InputInbox {
    next: AtomicU64,
    pending: Mutex<Option<Pending>>,
}

struct Pending {
    id: String,
    rule: Rule,
    reply: oneshot::Sender<Value>,
}

enum Rule {
    Approval,
    Questions(Vec<Question>),
    Peer { rpc: &'static str, arguments: Value, handler: crate::peer_tools::Handler },
}

struct Question {
    id: String,
    text: String,
    labels: Vec<String>,
    freeform: bool,
}

impl InputInbox {
    pub fn begin_peer(&self, frame: &Value, scrub: impl Fn(&str) -> String, handler: crate::peer_tools::Handler)
        -> Result<(EngineEvent, oneshot::Receiver<Value>), String>
    {
        let params = &frame["params"];
        if frame["method"] != "item/tool/call" || !params["namespace"].is_null()
            || serde_json::to_vec(frame).map_or(true, |bytes| bytes.len() > MAX_REQUEST_BYTES) {
            return Err("Unsupported or oversized Codex peer tool request".into());
        }
        let name = params["tool"].as_str().ok_or("Missing Codex peer tool")?;
        let rpc = crate::peer_tools::rpc(name, &params["arguments"])?;
        let mut pending = self.pending.lock().map_err(|_| "Codex input unavailable")?;
        if pending.as_ref().is_some_and(|item| !item.reply.is_closed()) { return Err("Codex input already pending".into()); }
        let generation = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| "Codex request clock unavailable")?.as_nanos();
        let id = format!("codex-peer-{}-{generation}-{}", std::process::id(), self.next.fetch_add(1, Ordering::Relaxed));
        let input = scrub(&params["arguments"].to_string());
        if input.len() > MAX_REQUEST_BYTES { return Err("Scrubbed peer request exceeds the review limit".into()); }
        let (reply, receiver) = oneshot::channel();
        *pending = Some(Pending {id:id.clone(), rule:Rule::Peer{rpc, arguments:params["arguments"].clone(),handler}, reply});
        Ok((EngineEvent::new("needs_input", json!({"id":id,"kind":"permission","title":"Allow this DOXA peer tool once?","tool_name":name,"input_summary":input,"require_full_review":true})), receiver))
    }
    /// The driver validates thread/turn/item identity before calling this.
    /// Display scrubbing is supplied by the host and is never used to change
    /// the provider's answer IDs or option labels.
    pub fn begin(&self, frame: &Value, scrub: impl Fn(&str) -> String)
        -> Result<(EngineEvent, oneshot::Receiver<Value>), String>
    {
        if serde_json::to_vec(frame).map_or(true, |v| v.len() > MAX_REQUEST_BYTES) {
            return Err("Codex input request exceeds the complete review limit".into());
        }
        let params = &frame["params"];
        let (rule, mut data) = match frame["method"].as_str() {
            Some("item/tool/requestUserInput") => {
                let rows = params["questions"].as_array().filter(|rows| !rows.is_empty() && rows.len() <= 32)
                    .ok_or("Invalid Codex question list")?;
                let mut questions = Vec::new();
                let mut display = Vec::new();
                for row in rows {
                    if row["isSecret"] == true { return Err("Private Codex questions require a masked input interface".into()); }
                    let id = row["id"].as_str().filter(|id| !id.is_empty() && id.len() <= 200 && !id.chars().any(char::is_control))
                        .ok_or("Invalid Codex question ID")?;
                    let text = row["question"].as_str().filter(|text| !text.is_empty()).ok_or("Missing Codex question")?;
                    if questions.iter().any(|q: &Question| q.id == id) { return Err("Duplicate Codex question ID".into()); }
                    let mut labels = Vec::new();
                    let mut options = Vec::new();
                    if let Some(rows) = row["options"].as_array() {
                        if rows.len() > 32 { return Err("Too many Codex question options".into()); }
                        for option in rows {
                            let label = option["label"].as_str().filter(|value| !value.is_empty()).ok_or("Invalid Codex option")?;
                            if labels.iter().any(|existing| existing == label) { return Err("Duplicate Codex option".into()); }
                            labels.push(label.to_owned());
                            options.push(json!({"label":scrub(label),"description":scrub(option["description"].as_str().unwrap_or(""))}));
                        }
                    } else if !row["options"].is_null() { return Err("Invalid Codex question options".into()); }
                    // Option labels can contain secrets. A changed label cannot
                    // be safely mapped back to an unreviewed provider answer.
                    if labels.iter().zip(&options).any(|(label, option)| option["label"] != *label) {
                        return Err("Codex option contains private data; answer withheld".into());
                    }
                    let freeform = labels.is_empty() || row["isOther"] == true;
                    display.push(json!({"id":id,"question":scrub(text),"header":scrub(row["header"].as_str().unwrap_or("")),"options":options,"isOther":freeform}));
                    questions.push(Question {id:id.into(), text:scrub(text), labels, freeform});
                }
                (Rule::Questions(questions), json!({"kind":"ask_user","questions":display}))
            }
            Some("item/commandExecution/requestApproval") => {
                if params["command"].as_str().is_none_or(str::is_empty) {
                    return Err("Codex command approval lacks a complete command".into());
                }
                (Rule::Approval, json!({"kind":"permission","title":"Approve this Codex command once?","tool_name":"command_execution","input_summary":scrub(&params.to_string()),"require_full_review":true}))
            }
            Some("item/fileChange/requestApproval") => {
                let item = &frame["doxa_item"];
                if item["type"] != "fileChange" || !item["changes"].is_array() {
                    return Err("Codex file approval lacks the complete proposed changes".into());
                }
                (Rule::Approval, json!({"kind":"permission","title":"Approve these Codex file changes once?","tool_name":"file_change","input_summary":scrub(&json!({"request":params,"changes":item["changes"]}).to_string()),"require_full_review":true}))
            }
            _ => return Err("Unsupported Codex interactive request".into()),
        };
        if serde_json::to_vec(&data).map_or(true, |bytes| bytes.len() > MAX_REQUEST_BYTES) {
            return Err("Scrubbed Codex input exceeds the review limit".into());
        }
        let mut pending = self.pending.lock().map_err(|_| "Codex input unavailable")?;
        if pending.as_ref().is_some_and(|item| !item.reply.is_closed()) {
            return Err("A Codex input request is already pending".into());
        }
        let generation = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| "Codex request clock unavailable")?.as_nanos();
        let id = format!("codex-input-{}-{generation}-{}", std::process::id(), self.next.fetch_add(1, Ordering::Relaxed));
        data["id"] = json!(id);
        let (reply, receiver) = oneshot::channel();
        *pending = Some(Pending {id, rule, reply});
        Ok((EngineEvent::new("needs_input", data), receiver))
    }

    pub fn answer(&self, id: &str, answer: &Value) -> Result<Value, String> {
        if serde_json::to_vec(answer).map_or(true, |bytes| bytes.len() > MAX_ANSWER_BYTES) {
            return Err("Codex answer exceeds the input limit".into());
        }
        let mut pending = self.pending.lock().map_err(|_| "Codex input unavailable")?;
        let current = pending.as_ref().filter(|item| item.id == id && !item.reply.is_closed())
            .ok_or("Codex input is no longer pending")?;
        let result = match &current.rule {
            Rule::Approval | Rule::Peer { .. } => match answer["decision"].as_str() {
                Some("allow") => json!({"decision":"accept"}),
                Some("deny") => json!({"decision":"decline"}),
                _ => return Err("Invalid Codex approval decision".into()),
            },
            Rule::Questions(questions) => {
                if answer["cancelled"] == true { json!({"answers":{}}) } else {
                    let answers = answer["answers"].as_object().ok_or("Missing Codex answers")?;
                    if answers.len() != questions.len() { return Err("Incomplete Codex answers".into()); }
                    let mut result = Map::new();
                    for question in questions {
                        // Claude clients used question text as the key. Accept
                        // that legacy form only if it identifies one question.
                        let choice = answers.get(&question.id).or_else(|| {
                            (questions.iter().filter(|q| q.text == question.text).count() == 1)
                                .then(|| answers.get(&question.text)).flatten()
                        }).and_then(Value::as_str).filter(|value| !value.trim().is_empty() && value.len() <= 8192)
                            .ok_or("Invalid Codex answer")?;
                        if !question.freeform && !question.labels.iter().any(|label| label == choice) {
                            return Err("Codex answer is not one of the reviewed options".into());
                        }
                        result.insert(question.id.clone(), json!({"answers":[choice]}));
                    }
                    json!({"answers":result})
                }
            }
        };
        let current = pending.take().unwrap();
        drop(pending);
        if let Rule::Peer {rpc, arguments, handler} = current.rule {
            // Peer RPCs include bounded local socket waits. Keep cancellation
            // and daemon status responsive while an approved tool executes.
            std::thread::spawn(move || {
                let result = if result["decision"] == "accept" { handler(rpc, &arguments) }
                    else { Err("The user declined this peer tool".into()) };
                let (success, text) = match result {
                    Ok(value) if value.to_string().len() <= MAX_REQUEST_BYTES => (true, format!("[DOXA PEER DATA -- UNTRUSTED]\n{value}")),
                    Ok(_) => (false, "Peer result exceeded the display limit".into()),
                    Err(_) => (false, "Peer tool was declined or unavailable".into()),
                };
                let _ = current.reply.send(json!({"success":success,"contentItems":[{"type":"inputText","text":text}]}));
            });
        } else { current.reply.send(result).map_err(|_| "Codex input is no longer pending")?; }
        Ok(json!({}))
    }

    pub fn clear(&self) { if let Ok(mut guard) = self.pending.lock() { guard.take(); } }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn question() -> Value { json!({"method":"item/tool/requestUserInput","params":{"questions":[{"id":"q1","question":"Choose?","options":[{"label":"First"},{"label":"Second"}]}]}}) }
    #[test]
    fn answers_are_validated_translated_and_single_use() {
        let inbox = InputInbox::default();
        let (event, mut receiver) = inbox.begin(&question(), str::to_owned).unwrap();
        let id = event.data["id"].as_str().unwrap();
        assert!(inbox.answer("stale", &json!({"answers":{"q1":"First"}})).is_err());
        assert!(inbox.answer(id, &json!({"answers":{"q1":"Invented"}})).is_err());
        inbox.answer(id, &json!({"answers":{"q1":"Second"}})).unwrap();
        assert_eq!(receiver.try_recv().unwrap(), json!({"answers":{"q1":{"answers":["Second"]}}}));
        assert!(inbox.answer(id, &json!({"answers":{"q1":"First"}})).is_err());
    }
    #[test]
    fn secret_and_scrubbed_option_requests_are_refused() {
        let inbox = InputInbox::default();
        let mut frame = question(); frame["params"]["questions"][0]["isSecret"] = json!(true);
        assert!(inbox.begin(&frame, str::to_owned).is_err());
        assert!(inbox.begin(&question(), |_| "[redacted]".into()).is_err());
    }
    #[test]
    fn approval_never_creates_a_session_or_global_policy() {
        let inbox = InputInbox::default();
        let frame = json!({"method":"item/commandExecution/requestApproval","params":{"command":"echo test"}});
        let (event, mut receiver) = inbox.begin(&frame, str::to_owned).unwrap();
        let id = event.data["id"].as_str().unwrap();
        assert_eq!(event.data["require_full_review"], true);
        assert!(inbox.answer(id, &json!({"decision":"acceptForSession"})).is_err());
        inbox.answer(id, &json!({"decision":"allow"})).unwrap();
        assert_eq!(receiver.try_recv().unwrap(), json!({"decision":"accept"}));
    }
    #[test]
    fn dropped_or_cleared_requests_cannot_receive_replayed_answers() {
        let inbox = InputInbox::default();
        let (event, receiver) = inbox.begin(&question(), str::to_owned).unwrap();
        drop(receiver);
        assert!(inbox.answer(event.data["id"].as_str().unwrap(), &json!({})).is_err());
        let (_, mut receiver) = inbox.begin(&question(), str::to_owned).unwrap();
        inbox.clear(); assert!(receiver.try_recv().is_err());
    }
}
