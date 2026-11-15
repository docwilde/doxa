//! Uploads are bounded and owned by one attached socket. Prompt admission
//! consumes the batch; disconnect drops unfinished bytes. Tokens are never
//! filesystem paths, peer tool arguments, or reusable remote capabilities.
use doxa_input::{Attachment, MAX_COUNT, MAX_IMAGE, MAX_TOTAL};
use serde_json::{json, Value};
use std::{collections::HashMap, time::{Duration, Instant}};

struct Upload { name:String, mime:String, total:usize, bytes:Vec<u8>, ready:Option<Attachment> }
pub(crate) struct Uploads { values:HashMap<String,Upload>, next:u64, updated:Instant }
impl Default for Uploads {fn default()->Self{Self{values:HashMap::new(),next:0,updated:Instant::now()}}}
impl Uploads {
    pub fn clear(&mut self){self.values.clear();}
    /// `images` is the host's current image admission, evaluated only when
    /// an operation needs it; a refusal carries the host's exact reason.
    pub fn call(&mut self,method:&str,params:&Value,images:impl FnOnce()->Result<(),String>)->Result<Value,String>{
        if self.updated.elapsed()>Duration::from_secs(60){self.clear();}self.updated=Instant::now();
        match method {
            "attachment_capabilities"=>{let admission=images();Ok(json!({"images":admission.is_ok(),"image_refusal":admission.err(),"text":true,"max_count":MAX_COUNT,"max_total":MAX_TOTAL}))},
            "attachment_clear"=>{self.clear();Ok(json!({}))},
            "attachment_begin"=>{
                let name=params["name"].as_str().filter(|s|doxa_input::valid_name(s)).ok_or("Invalid attachment filename")?;
                let mime=params["mime"].as_str().filter(|s|matches!(*s,"text/plain"|"image/png"|"image/jpeg")).ok_or("Unsupported attachment media type")?;
                if mime.starts_with("image/"){images()?;}
                let total=params["bytes"].as_u64().and_then(|n|usize::try_from(n).ok()).filter(|n|*n>0&&*n<=MAX_IMAGE).ok_or("Attachment exceeds 1 MiB")?;
                if self.values.len()>=MAX_COUNT||self.values.values().map(|u|u.total).sum::<usize>()+total>MAX_TOTAL{return Err("Attachment batch is full".into());}
                self.next=self.next.checked_add(1).ok_or("Attachment IDs exhausted")?;let id=format!("a{}",self.next);
                self.values.insert(id.clone(),Upload{name:name.into(),mime:mime.into(),total,bytes:Vec::new(),ready:None});
                Ok(json!({"attachment_id":id}))
            }
            "attachment_chunk"=>{
                let id=params["attachment_id"].as_str().ok_or("Missing attachment ID")?;
                let upload=self.values.get_mut(id).ok_or("Attachment expired")?;
                if upload.ready.is_some()||params["offset"].as_u64()!=Some(upload.bytes.len()as u64){return Err("Attachment offset changed".into());}
                let bytes=doxa_input::decode_chunk(params["data"].as_str().ok_or("Missing attachment bytes")?).map_err(|e|e.to_string())?;
                if bytes.is_empty()||upload.bytes.len()+bytes.len()>upload.total{return Err("Attachment size changed".into());}
                upload.bytes.extend_from_slice(&bytes);Ok(json!({"offset":upload.bytes.len()}))
            }
            "attachment_finish"=>{
                let id=params["attachment_id"].as_str().ok_or("Missing attachment ID")?;
                let upload=self.values.get_mut(id).ok_or("Attachment expired")?;
                if upload.ready.is_some()||upload.bytes.len()!=upload.total{return Err("Attachment incomplete".into());}
                upload.ready=Some(Attachment::from_wire(&upload.name,&upload.mime,std::mem::take(&mut upload.bytes)).map_err(|e|e.to_string())?);
                Ok(json!({"attachment_id":id}))
            }
            _=>Err("Unknown attachment operation".into()),
        }
    }
    pub fn resolve(&self,ids:&Value)->Result<Vec<Attachment>,String>{
        if ids.is_null(){return Ok(Vec::new());}
        let ids=ids.as_array().filter(|a|!a.is_empty()&&a.len()<=MAX_COUNT).ok_or("Invalid attachment batch")?;
        if self.updated.elapsed()>Duration::from_secs(60){return Err("Attachments expired; submit again to upload them".into());}
        let mut seen=std::collections::HashSet::new();
        ids.iter().map(|id|{
            let id=id.as_str().filter(|id|seen.insert(*id)).ok_or("Invalid or duplicate attachment ID")?;
            self.values.get(id).and_then(|u|u.ready.clone()).ok_or_else(||"Attachment expired or incomplete".into())
        }).collect()
    }
}
#[cfg(test)]mod tests {
    use super::*;
    #[test]fn uploads_bind_to_connection_and_reject_partial_reordered_or_duplicate_ids(){
        let mut a=Uploads::default();let id=a.call("attachment_begin",&json!({"name":"x.txt","mime":"text/plain","bytes":3}),||Err("no".into())).unwrap()["attachment_id"].clone();
        assert!(a.resolve(&json!([id])).is_err());assert!(a.call("attachment_chunk",&json!({"attachment_id":id,"offset":1,"data":"YWJj"}),||Ok(())).is_err());
        a.call("attachment_chunk",&json!({"attachment_id":id,"offset":0,"data":"YWJj"}),||Ok(())).unwrap();
        a.call("attachment_finish",&json!({"attachment_id":id}),||Ok(())).unwrap();assert_eq!(&**a.resolve(&json!([id])).unwrap()[0].bytes,b"abc");
        assert!(a.resolve(&json!([id,id])).is_err());assert!(Uploads::default().resolve(&json!([id])).is_err());a.clear();assert!(a.resolve(&json!([id])).is_err());
    }
    #[test]fn image_input_is_explicit_and_never_a_host_path(){let mut a=Uploads::default();assert_eq!(a.call("attachment_begin",&json!({"name":"x.png","mime":"image/png","bytes":10}),||Err("exact model refusal".into())).unwrap_err(),"exact model refusal");assert!(a.call("attachment_begin",&json!({"name":"/etc/passwd","mime":"text/plain","bytes":10}),||Ok(())).is_err());
        assert!(a.call("attachment_begin",&json!({"name":"x.png","mime":"image/png","bytes":10}),||Ok(())).is_ok());
        let caps=a.call("attachment_capabilities",&json!({}),||Err("unpriced".into())).unwrap();assert_eq!(caps["images"],false);assert_eq!(caps["image_refusal"],"unpriced");
        // Text uploads never consult image admission.
        assert!(a.call("attachment_begin",&json!({"name":"x.txt","mime":"text/plain","bytes":3}),||panic!("text upload consulted image admission")).is_ok());}
}
