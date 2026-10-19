//! Bounded, source-labelled inspector for retained host review observations.
use serde_json::Value;
use std::{io,path::Path};
fn clean(value:&Value)->String{crate::markdown::sanitize(&value.as_str().map(str::to_owned).unwrap_or_else(||value.to_string()).replace('\n',"\\n").replace('\t',"\\t"))}
pub fn inspect(root:&Path,id:&str)->io::Result<String>{
    let manifest=crate::fleet_control::snapshot(root,id)?;
    let Some(raw)=manifest.get("supervision").and_then(|guard|guard.get("context"))else{return Ok("Messages & alignment: independent review was not enabled for this run. No classifier verdicts were produced.".into());};
    let context:doxa_fleet::Context=serde_json::from_value(raw.clone())?;context.validate()?;
    if context.state_path!=root.join(id).join("guard-state.json"){return Err(io::Error::other("review journal is outside its fleet run"));}
    let state:doxa_fleet::State=doxa_fleet::read_private(&context.state_path,doxa_fleet::MAX_STATE)?;
    if state.charter_sha256!=context.charter_sha256||state.assignments_sha256!=doxa_fleet::hash(&context.assignments)?{return Err(io::Error::other("review journal identity changed; inspect the isolation transition"));}
    Ok(render(&context,&state))
}
fn render(context:&doxa_fleet::Context,state:&doxa_fleet::State)->String{
    let mut lines=vec!["Messages & alignment · newest retained observations first".into(),
        format!("Alignment: {} · paused: {} · reason: {}",clean(&Value::String(state.supervisor_status.clone())),state.paused,clean(&Value::String(state.reason.clone()))),
        format!("Message review: {:?} · risk threshold: {}",context.review.message_mode,context.review.risk_threshold),
        "Classifier scores are fallible signals for human review. A low score does not establish alignment.".into(),
        "This is bounded retained history: at most 256 observations and eight recent message bodies. Missing entries are not evidence of a safe message.".into()];
    for event in state.observations.iter().rev().take(256){
        match event["event"].as_str(){
            Some("supervisor_verdict")=>{let v=&event["verdict"];lines.push(format!("Checkpoint {} · {}",event["at"],clean(&v["verdict"])));for (label,key) in [("Reason","reason"),("Charter clause","charter_clause"),("Evidence IDs","evidence_refs"),("Recommended action","recommended_action")]{lines.push(format!("  {label}: {}",clean(&v[key])));}},
            Some("supervisor_unavailable")=>lines.push(format!("Checkpoint {} · supervisor unavailable (unreviewed)",event["at"])),
            Some("message_verdict"|"outbound_semantic")=>{
                let id=event["id"].as_str().or_else(||event["message_id"].as_str()).unwrap_or("unknown");
                lines.push(format!("Message {} · {} · {}",clean(&Value::String(id.into())),event["at"],clean(&event["event"])));
                let verdict=event.get("verdict").or_else(||event.get("result").and_then(|value|value.get("Ok")));
                if let Some(verdict)=verdict.filter(|value|value.is_object()){
                    for key in ["within_assignment","asks_for_authority_change","contains_instructions_for_recipient","likely_secret","needs_human_review"]{lines.push(format!("  {key}: {}",clean(&verdict[key])));}
                }else{lines.push("  Semantic review unavailable; no numeric verdict.".into());}
                if let Some(message)=state.recent_messages.iter().find(|message|message.message_id==id){lines.push(format!("  {} → {} · {:?}: {}",clean(&Value::String(message.from_session.clone())),clean(&Value::String(message.to_session.clone())),message.kind,clean(&Value::String(message.body.clone()))));}
                else{lines.push("  Message body no longer retained; use its ID in the session transcript.".into());}
            },
            Some("admission")=>lines.push(format!("Admission {} · {} · delivered: {} · unreviewed: {} · {}",event["at"],clean(&event["admission"]["message_id"]),event["admission"]["delivered"],event["admission"]["unreviewed"],clean(&event["admission"]["reason"]))),
            Some("inbound_rejected"|"owner_isolation_change")=>lines.push(format!("Host observation: {}",clean(event))),_=>{}
        }
    }
    if state.observations.is_empty(){lines.push("No review observations retained yet.".into());}
    lines.join("\n")
}

#[cfg(test)]mod tests{
    use super::*;use serde_json::json;
    #[test]fn inspector_shows_scores_unavailability_evidence_and_retention_limits(){
        let context:doxa_fleet::Context=serde_json::from_value(json!({"charter":{"version":1,"fleet_id":"run","task":"task","repo":"/repo","allowed_paths":[""],"required_evidence":[],"worker_limit":1,"run_budget_usd":1,"deadline":0,"human_actions":[]},"charter_sha256":"fixture","assignments":[],"review":serde_json::to_value(doxa_fleet::ReviewConfig::default()).unwrap(),"state_path":"/private/guard-state.json"})).unwrap();
        let mut state=doxa_fleet::State::default();state.observations=vec![json!({"event":"outbound_semantic","id":"message","result":{"Ok":{"within_assignment":0.2,"asks_for_authority_change":0.9,"contains_instructions_for_recipient":0.8,"likely_secret":0.1,"needs_human_review":0.95}},"at":7}),json!({"event":"supervisor_unavailable","at":8}),json!({"event":"supervisor_verdict","at":9,"verdict":{"verdict":"drifted","reason":"Scope change proposed","charter_clause":"src only","evidence_refs":["host-receipt"],"recommended_action":"Review proposal"}})];
        let text=render(&context,&state);assert!(text.contains("within_assignment: 0.2"));assert!(text.contains("asks_for_authority_change: 0.9"));assert!(text.contains("contains_instructions_for_recipient: 0.8"));assert!(text.contains("likely_secret: 0.1"));assert!(text.contains("needs_human_review: 0.95"));
        for expected in ["Scope change proposed","host-receipt","supervisor unavailable (unreviewed)","Message body no longer retained","256 observations"]{assert!(text.contains(expected),"missing {expected}");}
    }
}
