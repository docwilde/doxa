//! A queued ACK is a distinct host-admitted turn. Only a native, fully audited
//! messaging continuation may extend the original work→handoff successor rule.
use serde_json::Value;

fn successful(turn:&Value)->bool {
    turn["is_error"]==false&&turn["usage_complete"]==true&&turn["model_consistent"]==true
}
fn sent(turn:&Value,kind:&str,id:&str,target:&str,reply:Option<&str>,refs:&[String])->bool {
    let audit=&turn["native_tool_audit"];
    if audit["version"]!=1||audit["complete"]!=true{return false;}
    let Some(actions)=audit["actions"].as_array().filter(|rows|!rows.is_empty()&&rows.len()<=8) else{return false;};
    let mut sends=0;let mut history=0;
    for action in actions {
        if action["completed"]!=true||action["success"]!=true{return false;}
        match action["operation"].as_str(){
            Some("list")=>{},
            Some("history")=>{history+=1;if history>2{return false;}},
            Some("send")=>{
                sends+=1;
                let request=&action["request"];
                if request["fleet_kind"]!=kind||request["target"]!=target
                    ||request["in_reply_to"].as_str()!=reply
                    ||request["artifact_refs"]!=serde_json::json!(refs)
                    ||action["message_id"]!=id
                    ||action["delivered_to"]!=serde_json::json!([target])
                    ||action["failed"]!=serde_json::json!([]){return false;}
            },
            _=>return false,
        }
    }
    sends==1
}

pub(super) fn continuation(row:&Value,handoff:&doxa_fleet::AcceptedHandoff,checkpoint:&Value,
    coordinator:&str,current_snapshot:&str)->Option<doxa_fleet::HandshakeContinuation> {
    let previous=&row["previous_turn"];
    let prior=&previous["last_turn"];let current=&row["last_turn"];
    if handoff.checkpoint_turn_serial.checked_add(2)!=row["turn_serial"].as_u64()
        ||handoff.checkpoint_turn_serial.checked_add(1)!=previous["turn_serial"].as_u64()
        ||previous["last_turn_kind"]!="turn_done"||row["last_turn_kind"]!="turn_done"
        ||!successful(&checkpoint["last_turn"])||!successful(prior)||!successful(current)
        ||prior["peer_started"]!=false||!prior["peer_origin_data"].is_null()
        ||current["peer_started"]!=true
        ||current["peer_origin_data"]["source"]!="peer_message"
        ||current["peer_origin_data"]["sender_session_id"]!=coordinator
        ||current["peer_origin_data"]["message_id"]!=handoff.ack_id
        ||current_snapshot.len()!=64||checkpoint["snapshot_sha256"]!=current_snapshot
        ||!sent(prior,"handoff",&handoff.handoff_id,coordinator,None,&handoff.artifact_refs)
        ||!sent(current,"confirm",&handoff.confirm_id,coordinator,Some(&handoff.ack_id),&handoff.artifact_refs){return None;}
    Some(doxa_fleet::HandshakeContinuation{ack_id:handoff.ack_id.clone(),confirm_id:handoff.confirm_id.clone(),
        handoff_turn_sha256:doxa_fleet::hash(prior).ok()?,snapshot_sha256:current_snapshot.into()})
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn fixture()->(Value,doxa_fleet::AcceptedHandoff,Value,String){
        let digest="a".repeat(64);
        let send=|kind:&str,id:&str,reply:Value|json!({"operation":"send","completed":true,"success":true,
            "request":{"fleet_kind":kind,"target":"boss","in_reply_to":reply,"artifact_refs":["checkpoint"]},
            "message_id":id,"delivered_to":["boss"],"failed":[]});
        let turn=|action:Value|json!({"is_error":false,"usage_complete":true,"model_consistent":true,
            "peer_started":false,"native_tool_audit":{"version":1,"complete":true,"actions":[action]}});
        let prior=turn(send("handoff","handoff",Value::Null));
        let mut current=turn(send("confirm","confirm",json!("ack")));
        current["peer_started"]=json!(true);
        current["peer_origin_data"]=json!({"source":"peer_message","sender_session_id":"boss","message_id":"ack"});
        let row=json!({"turn_serial":3,"last_turn_kind":"turn_done","last_turn":current,
            "previous_turn":{"turn_serial":2,"last_turn_kind":"turn_done","last_turn":prior}});
        let handoff=doxa_fleet::AcceptedHandoff{handoff_id:"handoff".into(),ack_id:"ack".into(),confirm_id:"confirm".into(),artifact_refs:vec!["checkpoint".into()],checkpoint_id:"checkpoint".into(),checkpoint_turn_serial:1,checkpoint_turn_sha256:"b".repeat(64),
            readback:doxa_fleet::HandoffReadback{next_action:"Read result".into(),assumptions:vec![],open_questions:vec![]},response:doxa_fleet::HandoffResponse{agrees:true,correction:None}};
        (row,handoff,json!({"snapshot_sha256":digest,"last_turn":{"is_error":false,"usage_complete":true,"model_consistent":true}}),digest)
    }
    #[test]
    fn queued_ack_extends_only_the_exact_successful_native_handshake(){
        let(row,handoff,checkpoint,digest)=fixture();
        assert!(continuation(&row,&handoff,&checkpoint,"boss",&digest).is_some());
        for (path,replacement) in [
            ("/turn_serial",json!(4)),("/previous_turn/turn_serial",json!(1)),
            ("/last_turn/peer_started",json!(false)),
            ("/last_turn/peer_origin_data/message_id",json!("other-ack")),
            ("/last_turn/peer_origin_data/sender_session_id",json!("other-peer")),
            ("/last_turn/peer_origin_data/source",json!("child_task")),
            ("/previous_turn/last_turn/peer_started",json!(true)),
            ("/last_turn/is_error",json!(true)),
            ("/last_turn/native_tool_audit/complete",json!(false)),
            ("/last_turn/native_tool_audit/actions/0/completed",json!(false)),
            ("/last_turn/native_tool_audit/actions/0/success",json!(false)),
            ("/last_turn/native_tool_audit/actions/0/message_id",json!("fake-sent-uuid")),
            ("/last_turn/native_tool_audit/actions/0/request/in_reply_to",json!("other-ack")),
            ("/previous_turn/last_turn/native_tool_audit/actions/0/message_id",json!("fake-handoff")),
            ("/previous_turn/last_turn/native_tool_audit/actions",json!([])),
            ("/last_turn/native_tool_audit/actions/0/delivered_to",json!([])),
        ]{let mut changed=row.clone();*changed.pointer_mut(path).unwrap()=replacement;
            assert!(continuation(&changed,&handoff,&checkpoint,"boss",&digest).is_none(),"accepted {path}");}
        for operation in ["workspace_read","shell","mcp","other"] {
            let mut changed=row.clone();changed["last_turn"]["native_tool_audit"]["actions"].as_array_mut().unwrap()
                .push(json!({"operation":operation,"completed":true,"success":true}));
            assert!(continuation(&changed,&handoff,&checkpoint,"boss",&digest).is_none());
        }
        assert!(continuation(&row,&handoff,&checkpoint,"boss",&"c".repeat(64)).is_none());
        let mut failed=checkpoint.clone();failed["last_turn"]["is_error"]=json!(true);
        assert!(continuation(&row,&handoff,&failed,"boss",&digest).is_none());
        let mut absent=row.clone();absent["last_turn"].as_object_mut().unwrap().remove("native_tool_audit");
        assert!(continuation(&absent,&handoff,&checkpoint,"boss",&digest).is_none());
    }
}
