//! Shared byte budgets for decoded fleet prose and its serialized transport.
pub const FLEET_PREFIX:&str="[DOXA-FLEET-V1] ";
pub const MAX_FLEET_BODY_BYTES:usize=16*1024;
pub const MAX_FLEET_ENVELOPE_BYTES:usize=128*1024;
pub const MAX_PEER_WIRE_BYTES:usize=256*1024;
pub const MAX_HISTORY_ROW_BYTES:usize=256*1024;
// Leave room for the trust wrapper and another JSON string-escaping layer in
// the bounded one-MiB native tool-result event. Three maximum reports still fit.
pub const MAX_HISTORY_BYTES:usize=448*1024;
pub const MAX_HISTORY_TAIL_BYTES:usize=1024*1024;
pub const MAX_SEND_REQUEST_BYTES:usize=128*1024;
pub fn fleet_wire_within_limit(wire:&str)->bool{
    let Some(raw)=wire.strip_prefix(FLEET_PREFIX)else{return false;};
    if wire.len()>MAX_FLEET_ENVELOPE_BYTES{return false;}
    let Some(mut value)=unique_json(raw)else{return false;};
    // Accept historical struct-key ordering, but no discarded duplicate keys,
    // whitespace inflation, noncanonical escapes or unbounded metadata.
    if value.to_string().len()!=raw.len(){return false;}
    let Some(row)=value.as_object()else{return false;};
    if !row.keys().all(|key|matches!(key.as_str(),"v"|"fleet_id"|"message_id"|"from_session"|"to_session"|"kind"|"assignment_id"|"charter_sha256"|"in_reply_to"|"body"|"artifact_refs"|"requested_action"|"hop"|"readback"|"handoff_response"))
        ||value["v"]!=1||value["hop"].as_u64().is_none_or(|hop|hop>4)
        ||value["body"].as_str().is_none_or(|body|body.len()>MAX_FLEET_BODY_BYTES)
        ||!matches!(value["kind"].as_str(),Some("status"|"question"|"evidence"|"proposal"|"task_request"|"completion"|"handoff"|"ack"|"confirm")){return false;}
    for(key,limit)in [("fleet_id",256),("message_id",128),("from_session",128),("to_session",128),("assignment_id",256),("charter_sha256",128)]{
        if value[key].as_str().is_none_or(|text|text.is_empty()||text.len()>limit){return false;}
    }
    for(key,limit)in [("in_reply_to",128),("requested_action",512)]{if !value[key].is_null()&&value[key].as_str().is_none_or(|text|text.len()>limit){return false;}}
    if value["artifact_refs"].as_array().is_none_or(|rows|rows.len()>8||rows.iter().any(|row|row.as_str().is_none_or(|text|text.len()>128))){return false;}
    value["body"]=serde_json::Value::Null;
    value.to_string().len()<=8*1024
}
fn unique_json(raw:&str)->Option<serde_json::Value>{
    use serde::{Deserialize,Deserializer,de::{self,Visitor,MapAccess,SeqAccess}};
    struct Unique(serde_json::Value);
    impl<'de> Deserialize<'de> for Unique{
        fn deserialize<D:Deserializer<'de>>(deserializer:D)->Result<Self,D::Error>{
            struct V;
            impl<'de> Visitor<'de> for V{
                type Value=Unique;
                fn expecting(&self,f:&mut std::fmt::Formatter)->std::fmt::Result{f.write_str("JSON without duplicate keys")}
                fn visit_map<A:MapAccess<'de>>(self,mut map:A)->Result<Unique,A::Error>{let mut out=serde_json::Map::new();while let Some(key)=map.next_key::<String>()?{if out.contains_key(&key){return Err(de::Error::custom("duplicate JSON key"));}out.insert(key,map.next_value::<Unique>()?.0);}Ok(Unique(serde_json::Value::Object(out)))}
                fn visit_seq<A:SeqAccess<'de>>(self,mut seq:A)->Result<Unique,A::Error>{let mut out=Vec::new();while let Some(value)=seq.next_element::<Unique>()?{out.push(value.0);}Ok(Unique(serde_json::Value::Array(out)))}
                fn visit_str<E:de::Error>(self,value:&str)->Result<Unique,E>{Ok(Unique(value.into()))}
                fn visit_string<E:de::Error>(self,value:String)->Result<Unique,E>{Ok(Unique(value.into()))}
                fn visit_bool<E:de::Error>(self,value:bool)->Result<Unique,E>{Ok(Unique(value.into()))}
                fn visit_u64<E:de::Error>(self,value:u64)->Result<Unique,E>{Ok(Unique(value.into()))}
                fn visit_i64<E:de::Error>(self,value:i64)->Result<Unique,E>{Ok(Unique(value.into()))}
                fn visit_f64<E:de::Error>(self,value:f64)->Result<Unique,E>{serde_json::Number::from_f64(value).map(|value|Unique(value.into())).ok_or_else(||de::Error::custom("invalid number"))}
                fn visit_unit<E:de::Error>(self)->Result<Unique,E>{Ok(Unique(serde_json::Value::Null))}
            }
            deserializer.deserialize_any(V)
        }
    }
    let mut de=serde_json::Deserializer::from_str(raw);let value=Unique::deserialize(&mut de).ok()?.0;de.end().ok()?;Some(value)
}

pub fn body_limit_error(actual:usize)->String{
    format!("Fleet message body is {actual} UTF-8 bytes; maximum is {MAX_FLEET_BODY_BYTES} bytes.")
}
/// Only this closed numeric diagnostic may cross an otherwise private tool error.
pub fn visible_body_limit_error(error:&str)->Option<String>{
    let(actual,limit)=error.strip_prefix("Fleet message body is ")?.split_once(" UTF-8 bytes; maximum is ")?;
    let actual=actual.parse::<usize>().ok()?;
    if limit.strip_suffix(" bytes.")?.parse::<usize>().ok()?!=MAX_FLEET_BODY_BYTES{return None;}
    Some(body_limit_error(actual))
}
