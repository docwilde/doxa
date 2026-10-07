//! Owner-scoped, volatile command broker. Uncertain commands are never replayed.
use serde_json::{json, Value};
use std::{collections::{HashMap, VecDeque}, time::{Duration, Instant}};
use subtle::ConstantTimeEq;
use uuid::Uuid;
use crate::push::{Kind as PushKind, Subscription};

const LEASE: Duration = Duration::from_secs(45);
const COMMAND_TTL: Duration = Duration::from_secs(60);
const MAX_SESSIONS: usize = 64;
const MAX_COMMANDS: usize = 8;
const MAX_RETAINED_COMMANDS: usize = 512;
const MAX_EVENTS: usize = 512;
const MAX_EVENT_BYTES: usize = 64 * 1024;
const MAX_TOTAL_EVENT_BYTES: usize = 32 * 1024 * 1024;

pub fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 128 && id.bytes().enumerate().all(|(i,b)|b.is_ascii_alphanumeric() || (i>0&&b==b'-'))
}
pub fn bounded_sessions(value: &Value) -> Option<Vec<Value>> {
    let sessions=value.as_array()?;
    if sessions.len()>MAX_SESSIONS { return None; }
    sessions.iter().map(|session| {
        let id=session["id"].as_str()?;
        if !valid_id(id) { return None; }
        let bounded=|key:&str,max:usize|session[key].as_str().filter(|text|text.len()<=max&&!text.chars().any(char::is_control));
        Some(json!({"id":id,"title":bounded("title",160).unwrap_or(id),
            "engine":bounded("engine",32),"model":bounded("model",128),
            "incarnation":bounded("incarnation",64).unwrap_or(""),
            "encrypted":session["encrypted"]==true}))
    }).collect()
}
fn same_secret(actual:&str, supplied:&str)->bool {
    actual.len()==supplied.len() && bool::from(actual.as_bytes().ct_eq(supplied.as_bytes()))
}
struct Host { lease:String, expires:Instant, sessions:Vec<Value>, pending:VecDeque<String> }
struct Command { owner:String, host:String, session:String, op:String, payload:Value,
    state:&'static str, result:Option<Value>, created:Instant }
pub struct Hub { hosts:HashMap<(String,String),Host>, commands:HashMap<String,Command>,
    requests:HashMap<(String,String),String>,
    events:HashMap<(String,String,String),VecDeque<Value>>,
    event_order:VecDeque<((String,String,String),u64,usize)>,event_bytes:usize,
    subscriptions:HashMap<String,Vec<Subscription>>,
    pending_push:VecDeque<(String,PushKind)>,
    last_push:HashMap<(String,String,String,PushKind),Instant> }
impl Hub {
    pub fn new()->Self{Self{hosts:HashMap::new(),commands:HashMap::new(),requests:HashMap::new(),events:HashMap::new(),event_order:VecDeque::new(),event_bytes:0,
        subscriptions:HashMap::new(),pending_push:VecDeque::new(),last_push:HashMap::new()}}
    fn reap(&mut self){
        let now=Instant::now();
        let old_hosts=self.hosts.len();
        self.hosts.retain(|_,host|host.expires>now);
        for command in self.commands.values_mut(){
            if now.duration_since(command.created)>=COMMAND_TTL && !matches!(command.state,"accepted"|"refused") {
                command.state="expired";
            }
        }
        self.commands.retain(|_,command|now.duration_since(command.created)<COMMAND_TTL*2);
        self.requests.retain(|_,id|self.commands.contains_key(id));
        self.last_push.retain(|_,sent|now.duration_since(*sent)<Duration::from_secs(3600));
        for host in self.hosts.values_mut(){
            host.pending.retain(|id|self.commands.get(id).is_some_and(|command|command.state=="queued"));
        }
        if self.hosts.len()!=old_hosts {
            self.events.retain(|(owner,host,session),_|self.hosts.get(&(owner.clone(),host.clone()))
                .is_some_and(|entry|entry.sessions.iter().any(|item|item["id"]==session.as_str())));
            self.event_order.retain(|(key,_,_)|self.events.contains_key(key));
            self.event_bytes=self.event_order.iter().map(|(_,_,bytes)|bytes).sum();
        }
    }
    pub fn subscribe(&mut self,owner:&str,subscription:Subscription)->Result<Value,&'static str>{
        let total=self.subscriptions.values().map(Vec::len).sum::<usize>();
        let entries=self.subscriptions.entry(owner.to_owned()).or_default();
        if let Some(old)=entries.iter_mut().find(|old|old.endpoint==subscription.endpoint){*old=subscription;}
        else if entries.len()>=16||total>=256{return Err("push subscription limit reached")}
        else{entries.push(subscription);}
        Ok(json!({"subscribed":true}))
    }
    pub fn unsubscribe(&mut self,owner:&str,endpoint:&str)->Value{
        if let Some(entries)=self.subscriptions.get_mut(owner){entries.retain(|item|item.endpoint!=endpoint);}
        json!({"subscribed":false})
    }
    pub fn take_push(&mut self)->Vec<(String,Subscription,PushKind)>{
        let mut deliveries=Vec::new();
        while let Some((owner,kind))=self.pending_push.pop_front(){
            if let Some(entries)=self.subscriptions.get(&owner){
                deliveries.extend(entries.iter().cloned().map(|item|(owner.clone(),item,kind)));
            }
        }
        deliveries
    }
    pub fn register(&mut self,owner:&str,id:&str,sessions:Vec<Value>,prior:Option<&str>)->Result<Value,&'static str>{
        self.reap(); if !valid_id(id){return Err("invalid host id");}
        let key=(owner.to_owned(),id.to_owned());
        let old_count=self.hosts.get(&key).map_or(0,|host|host.sessions.len());
        let total=self.hosts.values().map(|host|host.sessions.len()).sum::<usize>()-old_count+sessions.len();
        if total>64{return Err("hub session limit reached");}
        if let Some(host)=self.hosts.get_mut(&key){
            if !prior.is_some_and(|prior|same_secret(&host.lease,prior)){return Err("host lease required");}
            let changed=host.sessions.iter().filter_map(|old|{
                let id=old["id"].as_str()?;
                let current=sessions.iter().find(|item|item["id"]==id);
                (current.is_none_or(|item|item["incarnation"]!=old["incarnation"])).then(||id.to_owned())
            }).collect::<Vec<_>>();
            host.expires=Instant::now()+LEASE;host.sessions=sessions;
            host.pending.retain(|id|self.commands.get(id).is_some_and(|command|!changed.contains(&command.session)));
            let secret=host.lease.clone();
            let active=host.sessions.iter().filter_map(|item|item["id"].as_str().map(str::to_owned)).collect::<Vec<_>>();
            self.events.retain(|(login,machine,session),_|login!=owner||machine!=id||
                (active.iter().any(|id|id==session)&&!changed.contains(session)));
            self.event_order.retain(|(key,_,_)|self.events.contains_key(key));
            self.event_bytes=self.event_order.iter().map(|(_,_,bytes)|bytes).sum();
            for command in self.commands.values_mut(){
                if command.owner==owner&&command.host==id&&changed.contains(&command.session)
                    && matches!(command.state,"queued"|"delivered"){command.state="expired";}
            }
            return Ok(json!({"host_id":id,"lease":secret,"expires_in":LEASE.as_secs()}));
        }
        if self.hosts.keys().filter(|(login,_)|login==owner).count()>=16{return Err("host limit reached");}
        let secret=format!("{}{}",Uuid::new_v4().simple(),Uuid::new_v4().simple());
        self.hosts.insert(key,Host{lease:secret.clone(),expires:Instant::now()+LEASE,sessions,pending:VecDeque::new()});
        Ok(json!({"host_id":id,"lease":secret,"expires_in":LEASE.as_secs()}))
    }
    fn host(&self,owner:&str,id:&str,lease:&str)->Result<&Host,&'static str>{
        let host=self.hosts.get(&(owner.to_owned(),id.to_owned())).ok_or("host lease expired")?;
        if !same_secret(&host.lease,lease){return Err("host lease refused");}
        Ok(host)
    }
    pub fn list(&mut self,owner:&str)->Value{
        self.reap();
        json!({"sessions":self.hosts.iter().filter(|((login,_),_)|login==owner).flat_map(|((_,host_id),host)|{
            host.sessions.iter().map(move |session|json!({"id":format!("{}~{}",host_id,session["id"].as_str().unwrap_or("")),
                "host_id":host_id,"session_id":session["id"],"title":session["title"],
                "engine":session["engine"],"model":session["model"],
                "encrypted":session["encrypted"]}))
        }).collect::<Vec<_>>()})
    }
    pub fn enqueue(&mut self,owner:&str,host_id:&str,session_id:&str,op:&str,payload:Value)->Result<Value,&'static str>{
        self.reap();
        if !matches!(op,"prompt"|"answer"|"transcript"){return Err("unsupported command");}
        let request_id=payload["request_id"].as_str().filter(|id|valid_id(id)).map(str::to_owned);
        if payload.get("request_id").is_some()&&request_id.is_none(){return Err("invalid request id");}
        if let Some(id)=request_id.as_ref().and_then(|id|self.requests.get(&(owner.to_owned(),id.clone()))){
            let command=self.commands.get(id).ok_or("request result expired")?;
            if command.host!=host_id||command.session!=session_id||command.op!=op||command.payload!=payload{
                return Err("request id was used for different content");
            }
            return Ok(json!({"command_id":id,"status":command.state}));
        }
        if self.commands.len()>=MAX_RETAINED_COMMANDS{return Err("hub command retention limit reached");}
        let host=self.hosts.get_mut(&(owner.to_owned(),host_id.to_owned())).ok_or("host offline")?;
        if !host.sessions.iter().any(|session|session["id"]==session_id){return Err("session offline");}
        let encrypted=host.sessions.iter().any(|session|session["id"]==session_id&&session["encrypted"]==true);
        if encrypted && !payload["sealed"].is_object() { return Err("encrypted session requires a sealed command"); }
        if !encrypted && payload.get("sealed").is_some() { return Err("session does not accept a sealed command"); }
        if host.pending.len()>=MAX_COMMANDS{return Err("host command queue full");}
        let id=Uuid::new_v4().to_string();
        host.pending.push_back(id.clone());
        self.commands.insert(id.clone(),Command{owner:owner.into(),host:host_id.into(),session:session_id.into(),
            op:op.into(),payload,state:"queued",result:None,created:Instant::now()});
        if let Some(request_id)=request_id{self.requests.insert((owner.to_owned(),request_id),id.clone());}
        Ok(json!({"command_id":id,"status":"queued"}))
    }
    pub fn take(&mut self,owner:&str,host_id:&str,lease:&str)->Result<Value,&'static str>{
        self.reap(); self.host(owner,host_id,lease)?;
        let host=self.hosts.get_mut(&(owner.to_owned(),host_id.to_owned())).expect("checked");
        let mut result=Vec::new();
        while let Some(id)=host.pending.pop_front(){
            if let Some(command)=self.commands.get_mut(&id){
                if command.state!="queued"{continue;}
                command.state="delivered";
                result.push(json!({"command_id":id,"session_id":command.session,"op":command.op,"payload":command.payload}));
            }
        }
        Ok(json!({"commands":result}))
    }
    pub fn complete(&mut self,owner:&str,host_id:&str,lease:&str,id:&str,result:Value)->Result<Value,&'static str>{
        self.reap(); self.host(owner,host_id,lease)?;
        let command=self.commands.get_mut(id).ok_or("command expired")?;
        if command.owner!=owner||command.host!=host_id||command.state!="delivered"{return Err("command not pending on this host");}
        command.state=if result["ok"]==true{"accepted"}else{"refused"};command.result=Some(result);
        Ok(json!({"status":command.state}))
    }
    pub fn result(&mut self,owner:&str,id:&str)->Result<Value,&'static str>{
        self.reap(); let command=self.commands.get(id).ok_or("command expired")?;
        if command.owner!=owner{return Err("command belongs to another owner");}
        Ok(json!({"command_id":id,"status":command.state,"result":command.result}))
    }
    pub fn event(&mut self,owner:&str,host_id:&str,lease:&str,session_id:&str,frame:Value)->Result<Value,&'static str>{
        self.reap();let host=self.host(owner,host_id,lease)?;
        if !host.sessions.iter().any(|session|session["id"]==session_id){return Err("session offline");}
        let encrypted=host.sessions.iter().any(|session|session["id"]==session_id&&session["encrypted"]==true);
        if encrypted && !frame["event"]["data"]["sealed"].is_object() { return Err("encrypted event required"); }
        if !encrypted && frame["event"]["data"].get("sealed").is_some() { return Err("unexpected encrypted event"); }
        let seq=frame["seq"].as_u64().ok_or("event sequence required")?;
        if frame["type"]!="event" || !frame["event"].is_object(){return Err("invalid event frame");}
        let bytes=serde_json::to_vec(&frame).map_err(|_|"unreadable event")?.len();
        if bytes>MAX_EVENT_BYTES{return Err("event exceeds daemon frame bound");}
        let key=(owner.into(),host_id.into(),session_id.into());
        let ring=self.events.entry(key).or_default();
        if ring.back().is_some_and(|last|last["seq"].as_u64().unwrap_or(0)>=seq){return Ok(json!({"duplicate":true}));}
        ring.push_back(frame);
        if let Some(kind)=ring.back().and_then(|frame|frame["event"]["type"].as_str()).and_then(PushKind::from_event){
            let key=(owner.to_owned(),host_id.to_owned(),session_id.to_owned(),kind);
            if self.subscriptions.get(owner).is_some_and(|entries|!entries.is_empty())
                && self.last_push.get(&key).is_none_or(|sent|sent.elapsed()>=Duration::from_secs(5)){
                self.last_push.insert(key,Instant::now());
                if self.pending_push.len()<64{self.pending_push.push_back((owner.to_owned(),kind));}
            }
        }
        let key=(owner.into(),host_id.into(),session_id.into());
        self.event_order.push_back((key.clone(),seq,bytes));self.event_bytes+=bytes;
        if ring.len()>MAX_EVENTS{
            if let Some(old)=ring.pop_front(){
                let old_seq=old["seq"].as_u64().unwrap_or(0);
                if let Some(index)=self.event_order.iter().position(|(item,id,_)|item==&key&&*id==old_seq){
                    if let Some((_,_,length))=self.event_order.remove(index){self.event_bytes-=length;}
                }
            }
        }
        while self.event_bytes>MAX_TOTAL_EVENT_BYTES{
            let Some((key,id,length))=self.event_order.pop_front() else{break};
            if let Some(ring)=self.events.get_mut(&key){
                if ring.front().is_some_and(|frame|frame["seq"]==id){ring.pop_front();}
            }
            self.event_bytes-=length;
        }
        Ok(json!({"accepted":true}))
    }
    pub fn history(&mut self,owner:&str,host:&str,session:&str,cursor:u64)->Result<Value,&'static str>{
        self.reap();let active=self.hosts.get(&(owner.to_owned(),host.to_owned())).ok_or("host offline")?;
        if !active.sessions.iter().any(|item|item["id"]==session){return Err("session offline");}
        let ring=self.events.get(&(owner.into(),host.into(),session.into()));
        let oldest=ring.and_then(|ring|ring.front()).and_then(|frame|frame["seq"].as_u64());
        let gap=oldest.is_some_and(|oldest|cursor<oldest);
        let mut events=Vec::new();let mut bytes=0usize;
        for frame in ring.into_iter().flat_map(|ring|ring.iter()).filter(|frame|frame["seq"].as_u64().is_some_and(|seq|seq>=cursor)){
            let length=serde_json::to_vec(frame).map(|raw|raw.len()).unwrap_or(0);
            if events.len()>=128 || (bytes+length>512*1024&&!events.is_empty()){break;}
            bytes+=length;events.push(frame.clone());
        }
        let next=events.last().and_then(|frame|frame["seq"].as_u64()).and_then(|seq|seq.checked_add(1)).unwrap_or(cursor);
        Ok(json!({"events":events,"replay_gap":gap,"next_seq":next}))
    }
}

#[cfg(test)]mod tests{
    use super::*;
    #[test] fn encrypted_sessions_reject_plaintext_commands_and_events(){
        let mut hub=Hub::new();
        let owner="owner@example.com";
        let registration=hub.register(owner,"workstation",bounded_sessions(&json!([{
            "id":"s1","title":"Encrypted session","encrypted":true
        }])).unwrap(),None).unwrap();
        let lease=registration["lease"].as_str().unwrap();
        assert_eq!(hub.list(owner)["sessions"][0]["encrypted"],true);
        assert!(hub.enqueue(owner,"workstation","s1","prompt",json!({"text":"secret"})).is_err());
        assert!(hub.enqueue(owner,"workstation","s1","prompt",json!({"sealed":{"v":1}})).is_ok());
        assert!(hub.event(owner,"workstation",lease,"s1",json!({"type":"event","seq":1,
            "event":{"type":"text_delta","data":{"text":"secret"}}})).is_err());
        assert!(hub.event(owner,"workstation",lease,"s1",json!({"type":"event","seq":1,
            "event":{"type":"text_delta","data":{"sealed":{"v":1}}}})).is_ok());
    }
    #[test]fn push_is_owner_scoped_and_duplicate_events_do_not_notify_twice(){
        let mut hub=Hub::new();
        use web_push_native::jwt_simple::algorithms::{ECDSAP256PublicKeyLike,ES256KeyPair};
        let key=ES256KeyPair::generate();
        let public=web_push_native::p256::PublicKey::from_sec1_bytes(
            &key.public_key().public_key().to_bytes_uncompressed()).unwrap();
        let auth=web_push_native::Auth::clone_from_slice(&[7u8;16]);
        hub.subscribe("owner@example.com",Subscription{
            endpoint:"https://fcm.googleapis.com/fcm/send/owner".into(),
            public:public.clone(),auth:auth.clone(),
        }).unwrap();
        hub.subscribe("other@example.com",Subscription{
            endpoint:"https://fcm.googleapis.com/fcm/send/other".into(),
            public,auth,
        }).unwrap();
        let lease=hub.register("owner@example.com","host",bounded_sessions(&json!([{"id":"session"}])).unwrap(),None).unwrap()["lease"].as_str().unwrap().to_owned();
        let event=json!({"type":"event","seq":1,"event":{"type":"needs_input","data":{"id":"review"}}});
        hub.event("owner@example.com","host",&lease,"session",event.clone()).unwrap();
        hub.event("owner@example.com","host",&lease,"session",event).unwrap();
        let deliveries=hub.take_push();
        assert_eq!(deliveries.len(),1);
        assert_eq!(deliveries[0].0,"owner@example.com");
        assert_eq!(deliveries[0].2,PushKind::NeedsInput);
        assert!(hub.take_push().is_empty());
        hub.unsubscribe("owner@example.com",&deliveries[0].1.endpoint);
        assert!(hub.subscriptions["owner@example.com"].is_empty());
    }
    #[test]fn lease_owner_queue_and_no_redelivery(){
        let mut hub=Hub::new();let sessions=bounded_sessions(&json!([{"id":"session-1","title":"Session"}])).unwrap();
        let lease=hub.register("owner@example.com","host-1",sessions,None).unwrap()["lease"].as_str().unwrap().to_owned();
        assert!(hub.register("owner@example.com","host-1",Vec::new(),None).is_err());
        assert!(hub.take("other@example.com","host-1",&lease).is_err());
        let command=hub.enqueue("owner@example.com","host-1","session-1","prompt",json!({"text":"hello"})).unwrap();
        let id=command["command_id"].as_str().unwrap();
        let taken=hub.take("owner@example.com","host-1",&lease).unwrap();
        assert_eq!(taken["commands"][0]["command_id"],id);
        assert_eq!(hub.take("owner@example.com","host-1",&lease).unwrap()["commands"].as_array().unwrap().len(),0);
        assert!(hub.complete("other@example.com","host-1",&lease,id,json!({"ok":true})).is_err());
        hub.complete("owner@example.com","host-1",&lease,id,json!({"ok":true})).unwrap();
        assert_eq!(hub.result("owner@example.com",id).unwrap()["status"],"accepted");
        assert!(hub.result("other@example.com",id).is_err());
    }
    #[test]fn event_cursor_and_duplicate_are_bounded(){
        let mut hub=Hub::new();let lease=hub.register("user","host",bounded_sessions(&json!([{"id":"session"}])).unwrap(),None).unwrap()["lease"].as_str().unwrap().to_owned();
        hub.event("user","host",&lease,"session",json!({"type":"event","seq":5,"event":{"type":"turn_done","data":{}}})).unwrap();
        assert_eq!(hub.event("user","host",&lease,"session",json!({"type":"event","seq":5,"event":{}})).unwrap()["duplicate"],true);
        let history=hub.history("user","host","session",0).unwrap();
        assert_eq!(history["replay_gap"],true);assert_eq!(history["next_seq"],6);
    }
    #[test]fn restarted_session_expires_old_commands_and_resets_event_sequence(){
        let mut hub=Hub::new();
        let first=bounded_sessions(&json!([{"id":"session","incarnation":"first"}])).unwrap();
        let lease=hub.register("user","host",first,None).unwrap()["lease"].as_str().unwrap().to_owned();
        let command=hub.enqueue("user","host","session","prompt",json!({"text":"once"})).unwrap();
        let command_id=command["command_id"].as_str().unwrap();
        hub.event("user","host",&lease,"session",json!({"type":"event","seq":800,"event":{"type":"turn_done","data":{}}})).unwrap();
        let next=bounded_sessions(&json!([{"id":"session","incarnation":"second"}])).unwrap();
        hub.register("user","host",next,Some(&lease)).unwrap();
        assert_eq!(hub.result("user",command_id).unwrap()["status"],"expired");
        assert!(hub.take("user","host",&lease).unwrap()["commands"].as_array().unwrap().is_empty());
        hub.event("user","host",&lease,"session",json!({"type":"event","seq":0,"event":{"type":"turn_started","data":{}}})).unwrap();
        assert_eq!(hub.history("user","host","session",0).unwrap()["events"][0]["seq"],0);
    }
    #[test]fn expired_commands_release_queue_capacity(){
        let mut hub=Hub::new();
        hub.register("user","host",bounded_sessions(&json!([{"id":"session"}])).unwrap(),None).unwrap();
        for _ in 0..MAX_COMMANDS{
            let id=hub.enqueue("user","host","session","prompt",json!({"text":"x"})).unwrap()["command_id"].as_str().unwrap().to_owned();
            hub.commands.get_mut(&id).unwrap().created=Instant::now()-COMMAND_TTL-Duration::from_secs(1);
        }
        assert!(hub.enqueue("user","host","session","prompt",json!({"text":"fresh"})).is_ok());
    }
    #[test]fn request_id_retries_do_not_deliver_twice(){
        let mut hub=Hub::new();
        let lease=hub.register("user","host",bounded_sessions(&json!([{"id":"session"}])).unwrap(),None).unwrap()["lease"].as_str().unwrap().to_owned();
        let payload=json!({"text":"once","request_id":"client-1"});
        let first=hub.enqueue("user","host","session","prompt",payload.clone()).unwrap();
        let again=hub.enqueue("user","host","session","prompt",payload.clone()).unwrap();
        assert_eq!(first["command_id"],again["command_id"]);
        assert!(hub.enqueue("user","host","session","prompt",json!({"text":"different","request_id":"client-1"})).is_err());
        assert_eq!(hub.take("user","host",&lease).unwrap()["commands"].as_array().unwrap().len(),1);
    }
}
