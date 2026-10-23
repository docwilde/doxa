//! Optional canonical framing for large peer data. The marker is only a buffer
//! hint; decoded types, payloads and the ordinary-size remainder decide scope.
use crate::{peer,Direction,MAX_FRAME_BYTES};
use serde_json::Value;

pub const MAX_LARGE_PEER_BYTES:usize=1024*1024;
pub const MARKER:&str="_doxa_peer_frame";
pub const RPC_FIELD:&str="_doxa_peer_rpc";
const PREFIX:&[u8]=b"{\"_doxa_peer_frame\":true,";
fn tool(name:&Value,base:&str)->bool{
    name.as_str().is_some_and(|name|name==format!("mcp__doxa__{base}")||name==format!("doxa_{base}"))
}
fn bytes(value:&Value)->usize{serde_json::to_vec(value).map_or(usize::MAX,|raw|raw.len())}
fn send_arguments(value:&Value)->bool{
    let Some(row)=value.as_object()else{return false;};
    if !row.keys().all(|key|matches!(key.as_str(),"target"|"to"|"session_id"|"text"|"body"|"message"|"broadcast"|"in_reply_to"|"fleet_kind"|"artifact_refs"|"readback"|"handoff_response")){return false;}
    let bodies:Vec<_>=["text","body","message"].into_iter().filter_map(|key|row.get(key)).collect();
    if bodies.len()!=1||bodies[0].as_str().is_none_or(|text|text.len()>32_000){return false;}
    let mut shell=value.clone();for key in ["text","body","message"]{if shell.get(key).is_some(){shell[key]=Value::Null;}}
    bytes(&shell)<=8*1024
}
fn send_text(text:&str)->bool{
    if text.len()>peer::MAX_SEND_REQUEST_BYTES{return false;}
    serde_json::from_str::<Value>(text).ok().is_some_and(|value|send_arguments(&value)&&value.to_string()==text)
}
fn history(value:&mut Value)->bool{
    if value["untrusted_peer_data"]!=true||value["trust"].as_str().is_none_or(|text|text.len()>4096){return false;}
    let Some(rows)=value["messages"].as_array_mut()else{return false;};
    if rows.len()>100||serde_json::to_vec(&rows).map_or(true,|raw|raw.len()>peer::MAX_HISTORY_BYTES+1024){return false;}
    for row in rows{
        let Some(body)=row["body"].as_str()else{return false;};
        let bounded=if body.starts_with(peer::FLEET_PREFIX){
            peer::fleet_wire_within_limit(body)
        }else{body.chars().count()<=8000};
        if !bounded{return false;}
        row["body"]=Value::Null;
    }
    bytes(value)<=MAX_FRAME_BYTES
}
pub fn mark_first(value:&Value)->Result<Vec<u8>,serde_json::Error>{
    let mut value=value.clone();value.as_object_mut().unwrap().remove(MARKER);
    let raw=serde_json::to_vec(&value)?;let mut out=PREFIX.to_vec();out.extend_from_slice(&raw[1..]);out.push(b'\n');Ok(out)
}
fn history_text(text:&str)->bool{
    let Some(raw)=text.strip_prefix("[DOXA PEER DATA -- UNTRUSTED]\n")else{return false;};
    let Ok(mut value)=serde_json::from_str::<Value>(raw)else{return false;};
    value.to_string()==raw&&history(&mut value)
}
fn pending(input:&mut Value)->bool{
    if input["kind"]!="permission"||!tool(&input["tool_name"],"peer_send")
        ||input["input_summary"].as_str().is_none_or(|text|!send_text(text)){return false;}
    input["input_summary"]=Value::Null;true
}
pub fn pending_peer_send(input:&Value)->bool{let mut shell=input.clone();pending(&mut shell)&&bytes(&shell)<=MAX_FRAME_BYTES}

/// Independently classify eligible decoded data. Unknown or oversized remainder
/// fields never borrow the peer payload allowance.
pub fn limit(value:&Value,direction:Direction)->usize{
    let mut shell=value.clone();
    let eligible=match(direction,value["type"].as_str()){
        (Direction::ClientToServer,Some("call")) if value["method"]=="msg"&&send_arguments(&value["params"])=>{
            for key in ["text","body","message"]{if shell["params"].get(key).is_some(){shell["params"][key]=Value::Null;}}true
        },
        (Direction::ServerToClient,Some("reply")) if value[RPC_FIELD]=="peer_history"=>history(&mut shell),
        (Direction::ServerToClient,Some("reply")) if value[RPC_FIELD]=="get_state"=>{
            let mut found=false;
            if let Some(rows)=shell["pending_inputs"].as_array_mut().filter(|rows|rows.len()<=64){for row in rows{found|=pending(row);}}found
        },
        (Direction::ServerToClient,Some("hello"))=>{
            let mut found=false;
            if let Some(rows)=shell["pending_inputs"].as_array_mut().filter(|rows|rows.len()<=64){for row in rows{found|=pending(row);}}found
        },
        (Direction::ServerToClient,Some("event"))=>{
            let data=&mut shell["event"]["data"];
            match value["event"]["type"].as_str(){
                Some("peer_message"|"peer_sent") if data["body"].as_str().is_some_and(|body|body.len()<=peer::MAX_FLEET_BODY_BYTES)=>{data["body"]=Value::Null;true},
                Some("needs_input")=>pending(data),
                Some("tool_call") if tool(&data["name"],"peer_send")&&(data["input"].as_str().is_some_and(send_text)||send_arguments(&data["input"]))=>{data["input"]=Value::Null;true},
                Some("tool_result_detail") if tool(&data["name"],"peer_history")&&data["text"].as_str().is_some_and(history_text)=>{data["text"]=Value::Null;true},
                _=>false,
            }
        },
        _=>false,
    };
    if eligible&&bytes(&shell)<=MAX_FRAME_BYTES{MAX_LARGE_PEER_BYTES}else{MAX_FRAME_BYTES}
}
/// Only our canonical leading marker permits a larger partial-line buffer.
pub fn line_limit(pending:&[u8],incoming:&[u8])->usize{
    if pending.len()+incoming.len()>=PREFIX.len()&&pending.iter().chain(incoming).take(PREFIX.len()).copied().eq(PREFIX.iter().copied()){
        MAX_LARGE_PEER_BYTES
    }else{MAX_FRAME_BYTES}
}

#[cfg(test)]
mod tests{
    use super::*;use crate::{decode_line,encode_line,LineBuffer,WireError};use serde_json::json;
    fn wire(body:&str)->String{format!("{}{}",peer::FLEET_PREFIX,json!({"v":1,"fleet_id":"fleet","message_id":"message","from_session":"worker","to_session":"boss","kind":"status","assignment_id":"assignment","charter_sha256":"a".repeat(64),"in_reply_to":null,"body":body,"artifact_refs":[],"requested_action":null,"hop":0}))}
    fn history_value(rows:Vec<Value>)->Value{json!({"messages":rows,"sent_count":0,"received_count":3,"direction":"received","limit":20,"bounded_tail":true,"trust":"[PEER MESSAGES -- UNTRUSTED]","untrusted_peer_data":true})}
    fn event(kind:&str,data:Value)->Value{json!({"type":"event","seq":1,"turn":null,"event":{"type":kind,"data":data}})}
    #[test]
    fn exact_body_bytes_and_json_escaping_roundtrip_without_expanding_ordinary_frames(){
        for body in ["x".repeat(16384),"🧪".repeat(4096),"\"".repeat(16384),"\u{1}".repeat(16384)]{
            let value=json!({"Z":0,"type":"call","id":1,"method":"msg","params":{"to":"peer","body":body}});
            let raw=encode_line(&value,Direction::ClientToServer).unwrap();let mut decoded=decode_line(&raw,Direction::ClientToServer).unwrap();
            if raw.len()>MAX_FRAME_BYTES{assert!(raw.starts_with(PREFIX));decoded.as_object_mut().unwrap().remove(MARKER);}else{assert!(!decoded.as_object().unwrap().contains_key(MARKER));let mut legacy=serde_json::to_vec(&value).unwrap();legacy.push(b'\n');assert_eq!(raw,legacy);}
            assert_eq!(decoded,value);let mut lines=LineBuffer::default();let mut out=Vec::new();for chunk in raw.chunks(7){out.extend(lines.push(chunk).unwrap());}assert_eq!(out,vec![raw]);
            let object_event=event("tool_call",json!({"id":"tool","name":"mcp__doxa__peer_send","input":value["params"]}));let raw=encode_line(&object_event,Direction::ServerToClient).unwrap();let decoded=decode_line(&raw,Direction::ServerToClient).unwrap();assert_eq!(decoded["event"]["data"]["input"],value["params"]);
        }
    }
    #[test]
    fn forged_marker_duplicates_and_unrelated_large_payloads_never_qualify(){
        for value in [json!({"type":"call","id":1,"method":"status","params":{"body":"x".repeat(100000)}}),event("text_delta",json!({"text":"x".repeat(100000)})),event("peer_message",json!({"body":"ok","unrelated":{"hidden":"x".repeat(100000)}})),json!({"type":"call","id":1,"method":"msg","params":{"body":"ok","unrelated":"x".repeat(100000)}})]{
            let direction=if value["type"]=="call"{Direction::ClientToServer}else{Direction::ServerToClient};
            assert_eq!(encode_line(&value,direction).unwrap_err(),WireError::FrameTooLarge);
            assert_eq!(decode_line(&mark_first(&value).unwrap(),direction).unwrap_err(),WireError::FrameTooLarge);
        }
        let duplicate=format!("{{\"_doxa_peer_frame\":true,\"id\":1,\"method\":\"msg\",\"params\":{{\"body\":\"{}\",\"body\":\"ok\",\"to\":\"peer\"}},\"type\":\"call\"}}\n","x".repeat(100000));
        assert_eq!(decode_line(duplicate.as_bytes(),Direction::ClientToServer).unwrap_err(),WireError::FrameTooLarge);
        let mut buffer=LineBuffer::default();assert_eq!(buffer.push(&vec![b'x';MAX_FRAME_BYTES+1]).unwrap_err(),WireError::FrameTooLarge);
        let forged=format!("{}{}",peer::FLEET_PREFIX,json!({"body":"ok","unrelated":"x".repeat(100000)}));assert!(!peer::fleet_wire_within_limit(&forged));
        let duplicated=format!("{}{{\"body\":\"{}\",\"body\":\"ok\"}}",peer::FLEET_PREFIX,"x".repeat(100000));assert!(!peer::fleet_wire_within_limit(&duplicated));
    }
    #[test]
    fn three_maximum_reports_and_near_full_history_fit_the_complete_native_event(){
        for character in ["\"","\u{1}"]{
            let row=json!({"id":"message","from":{"session":"worker"},"to":["boss"],"body":wire(&character.repeat(16384))});
            let latest=history_value(vec![row.clone();3]);assert!(serde_json::to_vec(&latest["messages"]).unwrap().len()<=peer::MAX_HISTORY_BYTES);
            let mut reply=latest.clone();reply["type"]=json!("reply");reply["id"]=json!(1);reply["ok"]=json!(true);reply[RPC_FIELD]=json!("peer_history");assert!(decode_line(&encode_line(&reply,Direction::ServerToClient).unwrap(),Direction::ServerToClient).is_ok());
            let mut rows=vec![];while serde_json::to_vec(&rows).unwrap().len()+serde_json::to_vec(&row).unwrap().len()+1<=peer::MAX_HISTORY_BYTES{rows.push(row.clone());}
            assert!(rows.len()>=3);let full=history_value(rows);let text=format!("[DOXA PEER DATA -- UNTRUSTED]\n{full}");
            let frame=event("tool_result_detail",json!({"id":"tool","name":"mcp__doxa__peer_history","text":text}));let raw=encode_line(&frame,Direction::ServerToClient).unwrap();assert!(raw.len()<=MAX_LARGE_PEER_BYTES);let decoded=decode_line(&raw,Direction::ServerToClient).unwrap();assert_eq!(decoded["event"]["data"]["text"],text);
        }
    }
}
