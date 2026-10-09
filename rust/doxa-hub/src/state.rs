//! Owner-scoped, volatile command broker. Uncertain commands are never replayed.
//! Android fences linearize with enqueue/take under the Hub mutex. A process
//! restart loses delivery evidence; in-process retired epochs retain it.
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{collections::{HashMap, HashSet, VecDeque}, time::{Duration, Instant}};
use subtle::ConstantTimeEq;
use uuid::Uuid;
use crate::push::{Kind as PushKind, Subscription};

const LEASE: Duration = Duration::from_secs(45);
const COMMAND_TTL: Duration = Duration::from_secs(60);
const MAX_SESSIONS: usize = 64;
const MAX_COMMANDS: usize = 8;
const MAX_RETAINED_COMMANDS: usize = 512;
// A request ID embeds the issuing boot nonce. At capacity, an issuance epoch
// can end and only proven-safe records can be dropped: strict Android POSTs
// reject every ID from the old epoch, even after its record is gone.
const MAX_ANDROID_RECORDS: usize = 8192;
// Rotate on inventory reads before the hard cap, leaving room for writes and
// fences minted from that inventory while an older epoch drains.
const ANDROID_ROTATE_AT: usize = MAX_ANDROID_RECORDS * 3 / 4;
// Avoid churning the boot nonce when almost the entire ledger is unresolved.
const MIN_ANDROID_RECLAIM: usize = MAX_ANDROID_RECORDS / 4;
// Once an epoch falls out of this in-process proof window, fences fail closed.
const MAX_RETIRED_ANDROID_BOOTS: usize = 16;
const MAX_EVENTS: usize = 512;
const MAX_EVENT_BYTES: usize = 64 * 1024;
const MAX_TOTAL_EVENT_BYTES: usize = 32 * 1024 * 1024;
const ANDROID_LEASE: Duration = Duration::from_secs(24 * 3600);

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
struct AndroidSubscription { token:String, host:String, session:String, incarnation:String, tag:String, expires:Instant }
struct Command { owner:String, host:String, session:String, op:String, payload:Value,
    state:&'static str, result:Option<Value>, created:Instant, android_request:Option<String> }
struct AndroidRecord {
    host:String, session:String, op:String, incarnation:String,
    digest:Option<[u8;32]>, command_id:Option<String>, state:AndroidState, fenced:bool,
}
struct RetiredAndroidBoot { nonce:String, terminal_ids:HashSet<Uuid> }
#[derive(Clone,Copy,PartialEq,Eq)]
enum AndroidState { Absent, Queued, Cancelled, Delivered, ExpiredUndelivered, Accepted, Refused }
impl AndroidState {
    fn reclaimable(self)->bool {
        matches!(self,Self::Absent|Self::Cancelled|Self::ExpiredUndelivered|Self::Accepted|Self::Refused)
    }
}
pub struct Hub { hosts:HashMap<(String,String),Host>, commands:HashMap<String,Command>,
    requests:HashMap<(String,String),String>,
    boot:String, retired_boots:VecDeque<RetiredAndroidBoot>, android_records:HashMap<(String,String),AndroidRecord>,
    events:HashMap<(String,String,String),VecDeque<Value>>,
    event_order:VecDeque<((String,String,String),u64,usize)>,event_bytes:usize,
    subscriptions:HashMap<String,Vec<Subscription>>,
    pending_push:VecDeque<(String,PushKind)>,
    android:HashMap<String,Vec<AndroidSubscription>>,
    pending_android:VecDeque<(String,String,String,String,PushKind)>,
    last_push:HashMap<(String,String,String,PushKind),Instant> }
impl Hub {
    pub fn new()->Self{Self{hosts:HashMap::new(),commands:HashMap::new(),requests:HashMap::new(),
        boot:Uuid::new_v4().simple().to_string(),retired_boots:VecDeque::new(),android_records:HashMap::new(),events:HashMap::new(),event_order:VecDeque::new(),event_bytes:0,
        subscriptions:HashMap::new(),pending_push:VecDeque::new(),android:HashMap::new(),pending_android:VecDeque::new(),last_push:HashMap::new()}}
    fn rotate_android_issuance_if_needed(&mut self){
        if self.android_records.len()<ANDROID_ROTATE_AT ||
            self.android_records.values().filter(|record|record.state.reclaimable()).count()<MIN_ANDROID_RECLAIM {
            return;
        }
        let prior=self.boot.clone();
        loop {
            self.boot=Uuid::new_v4().simple().to_string();
            if self.boot!=prior && !self.retired_boots.iter().any(|entry|entry.nonce==self.boot) {break;}
        }
        // Preserve unsettled records. A compact terminal-ID index prevents a
        // reclaimed accepted/refused write from masquerading as an absent ID
        // whose original scope is no longer available to compare.
        let mut terminals=Vec::new();
        self.android_records.retain(|(_,request_id),record|{
            if !record.state.reclaimable(){return true;}
            if matches!(record.state,AndroidState::Accepted|AndroidState::Refused) {
                let Some((record_boot,uuid))=request_id.split_once('-').and_then(|(boot,suffix)|
                    Uuid::parse_str(suffix).ok().map(|uuid|(boot.to_owned(),uuid))) else {
                    return true;
                };
                terminals.push((record_boot,uuid));
            }
            false
        });
        let mut terminal_ids=HashSet::new();
        for (record_boot,uuid) in terminals {
            if record_boot==prior {terminal_ids.insert(uuid);}
            else if let Some(entry)=self.retired_boots.iter_mut().find(|entry|entry.nonce==record_boot) {
                entry.terminal_ids.insert(uuid);
            }
        }
        self.retired_boots.push_back(RetiredAndroidBoot{nonce:prior,terminal_ids});
        if self.retired_boots.len()>MAX_RETIRED_ANDROID_BOOTS {self.retired_boots.pop_front();}
    }
    fn reap(&mut self){
        let now=Instant::now();
        let old_hosts=self.hosts.len();
        self.hosts.retain(|_,host|host.expires>now);
        for command in self.commands.values_mut(){
            if now.duration_since(command.created)>=COMMAND_TTL && !matches!(command.state,"accepted"|"refused") {
                if command.state=="delivered" && command.android_request.is_some(){continue;}
                if command.state=="queued" {
                    if let Some(request)=command.android_request.as_ref() {
                        if let Some(record)=self.android_records.get_mut(&(command.owner.clone(),request.clone())) {
                            record.state=AndroidState::ExpiredUndelivered;
                        }
                    }
                }
                command.state="expired";
            }
        }
        self.commands.retain(|_,command|now.duration_since(command.created)<COMMAND_TTL*2
            || (command.state=="delivered" && command.android_request.is_some()));
        self.requests.retain(|_,id|self.commands.contains_key(id));
        for entries in self.android.values_mut() { entries.retain(|entry| entry.expires > now); }
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
    pub fn subscribe_android(&mut self,owner:&str,target:&str,incarnation:&str,token:&str,tag:&str)->Result<Value,&'static str>{
        self.reap();
        if !crate::fcm::valid_token(token) || !crate::fcm::valid_tag(tag) || incarnation.is_empty() || incarnation.len()>64 {
            return Err("invalid Android subscription");
        }
        let (host_id,session_id)=target.split_once('~').filter(|(h,s)|valid_id(h)&&valid_id(s))
            .ok_or("invalid session target")?;
        let host=self.hosts.get(&(owner.to_owned(),host_id.to_owned())).ok_or("session offline")?;
        if !host.sessions.iter().any(|session|session["id"]==session_id && session["incarnation"]==incarnation){
            return Err("session incarnation changed");
        }
        let total=self.android.values().map(Vec::len).sum::<usize>();
        let entries=self.android.entry(owner.to_owned()).or_default();
        let next=AndroidSubscription { token:token.into(),host:host_id.into(),session:session_id.into(),
            incarnation:incarnation.into(),tag:tag.into(),expires:Instant::now()+ANDROID_LEASE };
        if let Some(old)=entries.iter_mut().find(|old|old.token==token){*old=next;}
        else if entries.len()>=16 || total>=256 {return Err("Android subscription limit reached")}
        else {entries.push(next);}
        Ok(json!({"subscribed":true,"expires_in":ANDROID_LEASE.as_secs()}))
    }
    pub fn unsubscribe_android(&mut self,owner:&str,token:&str)->Value{
        if let Some(entries)=self.android.get_mut(owner){entries.retain(|entry|entry.token!=token);}
        json!({"subscribed":false})
    }
    pub fn take_android(&mut self)->Vec<(String,String,String,PushKind)> {
        self.reap();
        let mut deliveries=Vec::new();
        while let Some((owner,host,session,incarnation,kind))=self.pending_android.pop_front(){
            if self.hosts.get(&(owner.clone(),host.clone())).is_none_or(|current|
                !current.sessions.iter().any(|item|item["id"]==session && item["incarnation"]==incarnation)){continue;}
            if let Some(entries)=self.android.get(&owner){
                deliveries.extend(entries.iter().filter(|entry|entry.host==host&&entry.session==session
                    &&entry.incarnation==incarnation).map(|entry|(owner.clone(),entry.token.clone(),entry.tag.clone(),kind)));
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
                (current.is_none_or(|item|item["incarnation"]!=old["incarnation"]
                    || item["encrypted"]!=old["encrypted"])).then(||id.to_owned())
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
                    && matches!(command.state,"queued"|"delivered"){
                    if command.state=="delivered" && command.android_request.is_some(){continue;}
                    if command.state=="queued" {
                        if let Some(request)=command.android_request.as_ref() {
                            if let Some(record)=self.android_records.get_mut(&(owner.to_owned(),request.clone())) {
                                record.state=AndroidState::ExpiredUndelivered;
                            }
                        }
                    }
                    command.state="expired";
                }
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
    pub fn inventory(&mut self,owner:&str)->Value{
        self.reap();
        self.rotate_android_issuance_if_needed();
        self.list(owner)
    }
    pub fn list(&mut self,owner:&str)->Value{
        self.reap();
        json!({"owner":owner,"hub_boot":self.boot,"sessions":self.hosts.iter().filter(|((login,_),_)|login==owner).flat_map(|((_,host_id),host)|{
            host.sessions.iter().map(move |session|json!({"id":format!("{}~{}",host_id,session["id"].as_str().unwrap_or("")),
                "host_id":host_id,"session_id":session["id"],"title":session["title"],
                "engine":session["engine"],"model":session["model"],
                "incarnation":session["incarnation"],"encrypted":session["encrypted"]}))
        }).collect::<Vec<_>>()})
    }
    pub fn enqueue(&mut self,owner:&str,host_id:&str,session_id:&str,op:&str,payload:Value)->Result<Value,&'static str>{
        self.enqueue_inner(owner,host_id,session_id,op,payload,false)
    }
    fn enqueue_inner(&mut self,owner:&str,host_id:&str,session_id:&str,op:&str,payload:Value,android:bool)->Result<Value,&'static str>{
        self.reap();
        if !matches!(op,"prompt"|"answer"|"transcript"){return Err("unsupported command");}
        let request_id=payload["request_id"].as_str().filter(|id|valid_id(id)).map(str::to_owned);
        if payload.get("request_id").is_some()&&request_id.is_none(){return Err("invalid request id");}
        if !android && (payload.get("android_fence_v1").is_some()
            || request_id.as_deref().and_then(Self::android_boot).is_some()) {
            return Err("Android request id requires Android route");
        }
        if request_id.as_ref().is_some_and(|id|self.android_records.get(&(owner.to_owned(),id.clone())).is_some_and(|record|record.fenced)) {
            return Err("request id fenced");
        }
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
            op:op.into(),payload,state:"queued",result:None,created:Instant::now(),android_request:None});
        if let Some(request_id)=request_id{self.requests.insert((owner.to_owned(),request_id),id.clone());}
        Ok(json!({"command_id":id,"status":"queued"}))
    }
    fn android_boot(request_id:&str)->Option<&str>{
        let (boot,suffix)=request_id.split_once('-')?;
        if boot.len()!=32 || !boot.bytes().all(|b|b.is_ascii_hexdigit()&&!b.is_ascii_uppercase()) {return None;}
        let uuid=Uuid::parse_str(suffix).ok()?;
        (uuid.to_string()==suffix).then_some(boot)
    }
    pub fn enqueue_android(&mut self,owner:&str,host_id:&str,session_id:&str,op:&str,payload:Value)->Result<Value,&'static str>{
        self.reap();
        if !matches!(op,"prompt"|"answer"){return Err("unsupported Android command");}
        let request_id=payload["request_id"].as_str().ok_or("Android request id required")?.to_owned();
        let boot=payload["hub_boot"].as_str().ok_or("Android hub boot required")?;
        let incarnation=payload["incarnation"].as_str().filter(|s|!s.is_empty()&&s.len()<=64)
            .ok_or("Android session incarnation required")?.to_owned();
        if Self::android_boot(&request_id)!=Some(boot) || boot!=self.boot {return Err("Android hub boot changed");}
        if !valid_id(&request_id){return Err("invalid Android request id");}
        let host=self.hosts.get(&(owner.to_owned(),host_id.to_owned())).ok_or("host offline")?;
        if !host.sessions.iter().any(|session|session["id"]==session_id && session["incarnation"]==incarnation){
            return Err("session incarnation changed");
        }
        let digest:[u8;32]=Sha256::digest(serde_json::to_vec(&payload).map_err(|_|"invalid Android payload")?).into();
        let key=(owner.to_owned(),request_id.clone());
        if let Some(record)=self.android_records.get(&key) {
            if record.fenced {return Err("request id fenced");}
            if record.host!=host_id||record.session!=session_id||record.op!=op||record.incarnation!=incarnation||record.digest!=Some(digest) {
                return Err("request id was used for different content");
            }
            return Ok(json!({"command_id":record.command_id,"status":match record.state {
                AndroidState::Queued=>"queued",AndroidState::Delivered=>"delivered",
                AndroidState::Accepted=>"accepted",AndroidState::Refused=>"refused",
                AndroidState::ExpiredUndelivered|AndroidState::Cancelled=>"expired",_=>"expired"}}));
        }
        if self.android_records.len()>=MAX_ANDROID_RECORDS{return Err("Android request ledger full");}
        let mut payload=payload;
        payload["android_fence_v1"]=json!(true);
        let result=self.enqueue_inner(owner,host_id,session_id,op,payload,true)?;
        let id=result["command_id"].as_str().ok_or("command id missing")?.to_owned();
        self.commands.get_mut(&id).ok_or("command missing")?.android_request=Some(request_id.clone());
        self.android_records.insert(key,AndroidRecord{host:host_id.into(),session:session_id.into(),op:op.into(),incarnation,
            digest:Some(digest),command_id:Some(id),state:AndroidState::Queued,fenced:false});
        Ok(result)
    }
    pub fn fence_android(&mut self,owner:&str,request_id:&str,target:&str,op:&str,incarnation:&str)->Result<Value,&'static str>{
        self.reap();
        if !matches!(op,"prompt"|"answer") || incarnation.is_empty()||incarnation.len()>64 {return Err("invalid Android fence scope");}
        let (host,session)=target.split_once('~').filter(|(h,s)|valid_id(h)&&valid_id(s)).ok_or("invalid session target")?;
        let boot=Self::android_boot(request_id).ok_or("invalid Android request id")?;
        if !valid_id(request_id){return Err("invalid Android request id");}
        let key=(owner.to_owned(),request_id.to_owned());
        if let Some(record)=self.android_records.get(&key) {
            if record.host!=host||record.session!=session||record.op!=op||record.incarnation!=incarnation {
                return Err("Android fence scope differs");
            }
        } else {
            // A retired epoch from this process has retained every unsettled
            // delivery and remembers reclaimed terminal IDs. A restart has no
            // such proof and remains unsafe.
            if boot!=self.boot {
                let terminal_id=request_id.split_once('-').and_then(|(_,suffix)|Uuid::parse_str(suffix).ok())
                    .ok_or("invalid Android request id")?;
                return Ok(if self.retired_boots.iter().any(|known|known.nonce==boot
                    && !known.terminal_ids.contains(&terminal_id)) {
                    json!({"status":"absent_fenced","safe_to_clear":true})
                } else {
                    json!({"status":"unknown_old_boot","safe_to_clear":false})
                });
            }
            // The full ledger admits no new request IDs until an inventory
            // read changes the nonce. Absence is therefore safe without a
            // stored tombstone: this ID cannot be accepted later this epoch.
            if self.android_records.len()>=MAX_ANDROID_RECORDS {
                return Ok(json!({"status":"absent_fenced","safe_to_clear":true}));
            }
            self.android_records.insert(key.clone(),AndroidRecord{host:host.into(),session:session.into(),op:op.into(),incarnation:incarnation.into(),
                digest:None,command_id:None,state:AndroidState::Absent,fenced:true});
        }
        let record=self.android_records.get_mut(&key).expect("inserted");
        record.fenced=true;
        if record.state==AndroidState::Queued {
            let id=record.command_id.as_ref().ok_or("Android command missing")?;
            let command=self.commands.get_mut(id).ok_or("Android command missing")?;
            if command.state!="queued" {return Err("Android command delivery uncertain");}
            command.state="expired";
            record.state=AndroidState::Cancelled;
        }
        let (status,safe,terminal)=match record.state {
            AndroidState::Absent=>("absent_fenced",true,None),
            AndroidState::Cancelled=>("queued_cancelled",true,None),
            AndroidState::ExpiredUndelivered=>("expired_undelivered",true,None),
            AndroidState::Delivered=>("delivered_unsettled",false,None),
            AndroidState::Accepted=>("terminal",true,Some("accepted")),
            AndroidState::Refused=>("terminal",true,Some("refused")),
            AndroidState::Queued=>unreachable!(),
        };
        let mut response=json!({"status":status,"safe_to_clear":safe});
        if let Some(terminal)=terminal {response["command_status"]=json!(terminal);}
        Ok(response)
    }
    pub fn take(&mut self,owner:&str,host_id:&str,lease:&str)->Result<Value,&'static str>{
        self.reap(); self.host(owner,host_id,lease)?;
        let host=self.hosts.get_mut(&(owner.to_owned(),host_id.to_owned())).expect("checked");
        let mut result=Vec::new();
        while let Some(id)=host.pending.pop_front(){
            if let Some(command)=self.commands.get_mut(&id){
                if command.state!="queued"{continue;}
                command.state="delivered";
                if let Some(request)=command.android_request.as_ref() {
                    if let Some(record)=self.android_records.get_mut(&(owner.to_owned(),request.clone())) {
                        record.state=AndroidState::Delivered;
                    }
                }
                result.push(json!({"command_id":id,"session_id":command.session,"op":command.op,"payload":command.payload}));
                if command.android_request.is_some(){command.payload=Value::Null;}
            }
        }
        Ok(json!({"commands":result}))
    }
    pub fn complete(&mut self,owner:&str,host_id:&str,lease:&str,id:&str,result:Value)->Result<Value,&'static str>{
        self.reap(); self.host(owner,host_id,lease)?;
        let command=self.commands.get_mut(id).ok_or("command expired")?;
        if command.owner!=owner||command.host!=host_id||command.state!="delivered"{return Err("command not pending on this host");}
        command.state=if result["ok"]==true{"accepted"}else{"refused"};command.result=Some(result);
        if let Some(request)=command.android_request.as_ref() {
            if let Some(record)=self.android_records.get_mut(&(owner.to_owned(),request.clone())) {
                record.state=if command.state=="accepted"{AndroidState::Accepted}else{AndroidState::Refused};
            }
        }
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
        let incarnation=host.sessions.iter().find(|item|item["id"]==session_id)
            .and_then(|item|item["incarnation"].as_str()).unwrap_or("").to_owned();
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
            let web=self.subscriptions.get(owner).is_some_and(|entries|!entries.is_empty());
            let android=!incarnation.is_empty() && self.android.get(owner).is_some_and(|entries|
                entries.iter().any(|entry|entry.host==host_id&&entry.session==session_id
                    &&entry.incarnation==incarnation&&entry.expires>Instant::now()));
            if (web||android) && self.last_push.get(&key).is_none_or(|sent|sent.elapsed()>=Duration::from_secs(5)){
                self.last_push.insert(key,Instant::now());
                if web && self.pending_push.len()<64{self.pending_push.push_back((owner.to_owned(),kind));}
                if android && self.pending_android.len()<64{self.pending_android.push_back((owner.to_owned(),host_id.to_owned(),session_id.to_owned(),incarnation,kind));}
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
    #[test] fn inventory_scopes_owner_and_reports_session_incarnation(){
        let mut hub=Hub::new();
        hub.register("one@example.com","host",bounded_sessions(&json!([{
            "id":"session","incarnation":"started-1"}])).unwrap(),None).unwrap();
        let visible=hub.list("one@example.com");
        assert_eq!(visible["owner"],"one@example.com");
        assert_eq!(visible["sessions"][0]["id"],"host~session");
        assert_eq!(visible["sessions"][0]["incarnation"],"started-1");
        assert_eq!(hub.list("other@example.com")["sessions"],json!([]));
    }
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
    #[test]fn android_push_is_bound_to_owner_session_incarnation_and_current_tag(){
        let mut hub=Hub::new();
        let owner="owner@example.com";
        let lease=hub.register(owner,"host",bounded_sessions(&json!([
            {"id":"one","incarnation":"one-v1"},{"id":"two","incarnation":"two-v1"}
        ])).unwrap(),None).unwrap()["lease"].as_str().unwrap().to_owned();
        let token="fcm:abcdefghijklmnopqrstuvwxyz0123456789";
        let tag="abcdef0123456789abcdef0123456789";
        assert!(hub.subscribe_android("other@example.com","host~one","one-v1",token,tag).is_err());
        assert!(hub.subscribe_android(owner,"host~two","one-v1",token,tag).is_err());
        assert!(hub.subscribe_android(owner,"host~one","stale",token,tag).is_err());
        assert!(hub.subscribe_android(owner,"host~one","one-v1",token,"invalid").is_err());
        hub.subscribe_android(owner,"host~one","one-v1",token,tag).unwrap();
        assert!(hub.list(owner)["sessions"].as_array().unwrap().iter().any(|item|
            item["id"]=="host~one" && item["incarnation"]=="one-v1"));
        let event=|seq|json!({"type":"event","seq":seq,"event":{"type":"needs_input","data":{"id":"private"}}});
        hub.event(owner,"host",&lease,"two",event(1)).unwrap();
        assert!(hub.take_android().is_empty());
        hub.event(owner,"host",&lease,"one",event(1)).unwrap();
        let sent=hub.take_android();
        assert_eq!(sent.len(),1);
        assert_eq!(sent[0].0,owner);
        assert_eq!(sent[0].1,token);
        assert_eq!(sent[0].2,tag);
        assert!(hub.take_android().is_empty());
        hub.register(owner,"host",bounded_sessions(&json!([
            {"id":"one","incarnation":"one-v2"},{"id":"two","incarnation":"two-v1"}
        ])).unwrap(),Some(&lease)).unwrap();
        hub.event(owner,"host",&lease,"one",event(2)).unwrap();
        assert!(hub.take_android().is_empty());
        assert!(hub.subscribe_android(owner,"host~one","one-v1",token,tag).is_err());
        hub.subscribe_android(owner,"host~one","one-v2",token,tag).unwrap();
        hub.android.get_mut(owner).unwrap()[0].expires=Instant::now()-Duration::from_secs(1);
        hub.event(owner,"host",&lease,"one",event(3)).unwrap();
        assert!(hub.android.get(owner).is_some_and(Vec::is_empty));
        assert!(hub.take_android().is_empty());
        hub.subscribe_android(owner,"host~one","one-v2",token,tag).unwrap();
        hub.unsubscribe_android("other@example.com",token);
        assert_eq!(hub.android[owner].len(),1);
        hub.unsubscribe_android(owner,token);
        assert!(hub.android[owner].is_empty());
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
    fn android_id(hub:&Hub)->String {format!("{}-{}",hub.boot,Uuid::new_v4())}
    fn android_payload(hub:&Hub,id:&str)->Value {
        json!({"text":"once","request_id":id,"hub_boot":hub.boot,"incarnation":"v1","issued_at":1})
    }
    fn android_host(hub:&mut Hub,encrypted:bool)->String {
        hub.register("user","host",bounded_sessions(&json!([{"id":"session","incarnation":"v1","encrypted":encrypted}])).unwrap(),None)
            .unwrap()["lease"].as_str().unwrap().to_owned()
    }
    #[test]fn android_fence_linearizes_absent_queued_and_take_for_plain_and_sealed(){
        for encrypted in [false,true] {
            let mut hub=Hub::new();let lease=android_host(&mut hub,encrypted);
            let absent=android_id(&hub);
            assert!(hub.enqueue("user","host","session","prompt",json!({"request_id":absent,"text":"legacy before fence"})).is_err());
            let reply=hub.fence_android("user",&absent,"host~session","prompt","v1").unwrap();
            assert_eq!(reply,json!({"status":"absent_fenced","safe_to_clear":true}));
            assert_eq!(hub.fence_android("user",&absent,"host~session","prompt","v1").unwrap(),reply);
            assert!(hub.enqueue_android("user","host","session","prompt",android_payload(&hub,&absent)).is_err());
            assert!(hub.enqueue("user","host","session","prompt",json!({"request_id":absent,"text":"legacy"})).is_err());
            let id=android_id(&hub);
            let mut payload=android_payload(&hub,&id);
            if encrypted {payload.as_object_mut().unwrap().remove("text");payload["sealed"]=json!({"nonce":"opaque"});}
            let first=hub.enqueue_android("user","host","session","prompt",payload.clone()).unwrap();
            assert_eq!(hub.enqueue_android("user","host","session","prompt",payload.clone()).unwrap(),first);
            assert!(hub.enqueue_android("user","host","session","prompt",json!({"request_id":id,"hub_boot":hub.boot,"incarnation":"v1","text":"changed"})).is_err());
            let reply=hub.fence_android("user",&id,"host~session","prompt","v1").unwrap();
            assert_eq!(reply,json!({"status":"queued_cancelled","safe_to_clear":true}));
            assert_eq!(hub.fence_android("user",&id,"host~session","prompt","v1").unwrap(),reply);
            assert!(hub.enqueue_android("user","host","session","prompt",payload).is_err());
            assert!(hub.enqueue("user","host","session","prompt",json!({"request_id":id,"text":"legacy"})).is_err());
            assert!(hub.take("user","host",&lease).unwrap()["commands"].as_array().unwrap().is_empty());
        }
    }
    #[test]fn delivered_android_command_stays_unsettled_through_ttl_incarnation_and_lease_expiry(){
        let mut hub=Hub::new();let lease=android_host(&mut hub,false);let id=android_id(&hub);
        let payload=android_payload(&hub,&id);
        let command=hub.enqueue_android("user","host","session","prompt",payload).unwrap();
        let command_id=command["command_id"].as_str().unwrap().to_owned();
        assert_eq!(hub.take("user","host",&lease).unwrap()["commands"][0]["command_id"],command_id);
        hub.commands.get_mut(&command_id).unwrap().created=Instant::now()-COMMAND_TTL*3;
        hub.reap();
        assert_eq!(hub.commands[&command_id].state,"delivered");
        let next=bounded_sessions(&json!([{"id":"session","incarnation":"v2"}])).unwrap();
        hub.register("user","host",next,Some(&lease)).unwrap();
        assert_eq!(hub.commands[&command_id].state,"delivered");
        let reply=hub.fence_android("user",&id,"host~session","prompt","v1").unwrap();
        assert_eq!(reply,json!({"status":"delivered_unsettled","safe_to_clear":false}));
        hub.hosts.get_mut(&("user".into(),"host".into())).unwrap().expires=Instant::now()-Duration::from_secs(1);
        hub.reap();
        assert_eq!(hub.fence_android("user",&id,"host~session","prompt","v1").unwrap(),reply);
        assert!(hub.commands.contains_key(&command_id));
        let new_lease=hub.register("user","host",bounded_sessions(&json!([{"id":"session","incarnation":"v2"}])).unwrap(),None)
            .unwrap()["lease"].as_str().unwrap().to_owned();
        hub.complete("user","host",&new_lease,&command_id,json!({"ok":true})).unwrap();
        assert_eq!(hub.fence_android("user",&id,"host~session","prompt","v1").unwrap(),
            json!({"status":"terminal","safe_to_clear":true,"command_status":"accepted"}));
    }
    #[test]fn queued_expiry_and_old_boot_never_masquerade_as_absence(){
        let mut hub=Hub::new();android_host(&mut hub,false);let id=android_id(&hub);
        let payload=android_payload(&hub,&id);
        let command=hub.enqueue_android("user","host","session","prompt",payload.clone()).unwrap();
        let command_id=command["command_id"].as_str().unwrap();
        hub.commands.get_mut(command_id).unwrap().created=Instant::now()-COMMAND_TTL-Duration::from_secs(1);
        assert_eq!(hub.fence_android("user",&id,"host~session","prompt","v1").unwrap(),
            json!({"status":"expired_undelivered","safe_to_clear":true}));
        assert!(hub.enqueue_android("user","host","session","prompt",payload.clone()).is_err());
        let mut restarted=Hub::new();android_host(&mut restarted,false);
        assert_ne!(hub.boot,restarted.boot);
        assert_eq!(restarted.fence_android("user",&id,"host~session","prompt","v1").unwrap(),
            json!({"status":"unknown_old_boot","safe_to_clear":false}));
        assert!(restarted.enqueue_android("user","host","session","prompt",payload).is_err());
        assert!(restarted.enqueue("user","host","session","prompt",json!({"request_id":id,"text":"legacy"})).is_err());
    }
    #[test]fn android_write_requires_current_boot_and_exact_incarnation(){
        let mut hub=Hub::new();android_host(&mut hub,true);let id=android_id(&hub);
        let mut payload=android_payload(&hub,&id);payload.as_object_mut().unwrap().remove("text");payload["sealed"]=json!({"opaque":true});
        assert!(hub.enqueue_android("user","host","session","prompt",payload.clone()).is_ok());
        let mut stale=payload.clone();stale["incarnation"]=json!("v2");
        assert!(hub.enqueue_android("user","host","session","prompt",stale).is_err());
        let mut wrong_boot=payload;wrong_boot["hub_boot"]=json!(Uuid::new_v4().simple().to_string());
        assert!(hub.enqueue_android("user","host","session","prompt",wrong_boot).is_err());
        assert!(hub.fence_android("user",&id,"host~session","answer","v1").is_err());
    }
    #[test]fn full_ledger_rotates_issuance_and_rejects_reclaimed_late_posts(){
        let mut hub=Hub::new();let lease=android_host(&mut hub,false);
        let old_boot=hub.boot.clone();
        let delivered=android_id(&hub);
        let delivered_command=hub.enqueue_android("user","host","session","prompt",android_payload(&hub,&delivered)).unwrap();
        let delivered_command=delivered_command["command_id"].as_str().unwrap().to_owned();
        hub.take("user","host",&lease).unwrap();
        let queued=android_id(&hub);
        hub.enqueue_android("user","host","session","prompt",android_payload(&hub,&queued)).unwrap();
        let reclaimed=android_id(&hub);
        assert_eq!(hub.fence_android("user",&reclaimed,"host~session","prompt","v1").unwrap()["safe_to_clear"],true);
        for _ in 0..MAX_ANDROID_RECORDS-3 {
            let id=android_id(&hub);
            hub.fence_android("user",&id,"host~session","prompt","v1").unwrap();
        }
        assert_eq!(hub.android_records.len(),MAX_ANDROID_RECORDS);
        assert_eq!(hub.list("user")["hub_boot"],old_boot);
        assert_eq!(hub.boot,old_boot);
        assert_eq!(hub.inventory("user")["hub_boot"],hub.boot);
        assert_ne!(hub.boot,old_boot);
        assert_eq!(hub.android_records.len(),2);
        assert!(hub.android_records.contains_key(&("user".into(),delivered.clone())));
        assert!(hub.android_records.contains_key(&("user".into(),queued.clone())));
        assert_eq!(hub.fence_android("user",&reclaimed,"host~session","prompt","v1").unwrap(),
            json!({"status":"absent_fenced","safe_to_clear":true}));
        assert_eq!(hub.android_records.len(),2);
        let mut late=json!({"text":"late","request_id":reclaimed,"hub_boot":old_boot,"incarnation":"v1"});
        assert!(hub.enqueue_android("user","host","session","prompt",late.clone()).is_err());
        late["hub_boot"]=json!(hub.boot);
        assert!(hub.enqueue_android("user","host","session","prompt",late).is_err());
        assert!(hub.enqueue("user","host","session","prompt",json!({"request_id":reclaimed,"text":"legacy"})).is_err());
        assert_eq!(hub.fence_android("user",&delivered,"host~session","prompt","v1").unwrap(),
            json!({"status":"delivered_unsettled","safe_to_clear":false}));
        assert_eq!(hub.fence_android("user",&queued,"host~session","prompt","v1").unwrap(),
            json!({"status":"queued_cancelled","safe_to_clear":true}));
        hub.complete("user","host",&lease,&delivered_command,json!({"ok":true})).unwrap();
        assert_eq!(hub.fence_android("user",&delivered,"host~session","prompt","v1").unwrap()["command_status"],"accepted");
        let fresh=android_id(&hub);
        assert_eq!(hub.enqueue_android("user","host","session","prompt",android_payload(&hub,&fresh)).unwrap()["status"],"queued");
    }
    #[test]fn inventory_reclaims_terminal_records_before_the_hard_cap(){
        let mut hub=Hub::new();let lease=android_host(&mut hub,false);
        let boot=hub.boot.clone();
        let accepted=android_id(&hub);
        let accepted_payload=android_payload(&hub,&accepted);
        let command=hub.enqueue_android("user","host","session","prompt",accepted_payload.clone()).unwrap();
        let command_id=command["command_id"].as_str().unwrap().to_owned();
        hub.take("user","host",&lease).unwrap();
        hub.complete("user","host",&lease,&command_id,json!({"ok":true})).unwrap();
        for _ in 1..ANDROID_ROTATE_AT {
            let id=android_id(&hub);
            hub.fence_android("user",&id,"host~session","prompt","v1").unwrap();
        }
        assert_eq!(hub.android_records.len(),ANDROID_ROTATE_AT);
        hub.list("user");
        assert_eq!(hub.boot,boot);
        hub.inventory("user");
        assert_ne!(hub.boot,boot);
        assert!(hub.android_records.is_empty());
        assert_eq!(hub.fence_android("user",&accepted,"host~session","prompt","v1").unwrap(),
            json!({"status":"unknown_old_boot","safe_to_clear":false}));
        assert!(hub.enqueue_android("user","host","session","prompt",accepted_payload).is_err());
    }
    #[test]fn late_terminal_result_is_indexed_under_its_original_retired_boot(){
        let mut hub=Hub::new();let lease=android_host(&mut hub,false);
        let id=android_id(&hub);
        let command=hub.enqueue_android("user","host","session","prompt",android_payload(&hub,&id)).unwrap();
        let command_id=command["command_id"].as_str().unwrap().to_owned();
        hub.take("user","host",&lease).unwrap();
        for _ in 1..ANDROID_ROTATE_AT {
            let absent=android_id(&hub);
            hub.fence_android("user",&absent,"host~session","prompt","v1").unwrap();
        }
        hub.inventory("user");
        assert_eq!(hub.fence_android("user",&id,"host~session","prompt","v1").unwrap()["status"],"delivered_unsettled");
        hub.complete("user","host",&lease,&command_id,json!({"ok":true})).unwrap();
        for _ in 0..ANDROID_ROTATE_AT-1 {
            let absent=android_id(&hub);
            hub.fence_android("user",&absent,"host~session","prompt","v1").unwrap();
        }
        hub.inventory("user");
        assert!(!hub.android_records.contains_key(&("user".into(),id.clone())));
        assert_eq!(hub.fence_android("user",&id,"host~session","prompt","v1").unwrap(),
            json!({"status":"unknown_old_boot","safe_to_clear":false}));
    }
    #[test]fn retired_boot_proves_absence_only_within_one_process_and_bounded_history(){
        let mut hub=Hub::new();android_host(&mut hub,false);
        let first=android_id(&hub);
        let mut newest=first.clone();
        for _ in 0..MAX_RETIRED_ANDROID_BOOTS+1 {
            let old=hub.boot.clone();
            newest=android_id(&hub);
            for _ in 0..ANDROID_ROTATE_AT {
                let id=android_id(&hub);
                hub.android_records.insert(("user".into(),id),AndroidRecord{
                    host:"host".into(),session:"session".into(),op:"prompt".into(),incarnation:"v1".into(),
                    digest:None,command_id:None,state:AndroidState::Absent,fenced:true,
                });
            }
            hub.inventory("user");
            assert_ne!(hub.boot,old);
            assert!(hub.retired_boots.len()<=MAX_RETIRED_ANDROID_BOOTS);
        }
        assert_eq!(hub.retired_boots.len(),MAX_RETIRED_ANDROID_BOOTS);
        assert_eq!(hub.fence_android("user",&newest,"host~session","prompt","v1").unwrap(),
            json!({"status":"absent_fenced","safe_to_clear":true}));
        assert_eq!(hub.fence_android("user",&first,"host~session","prompt","v1").unwrap(),
            json!({"status":"unknown_old_boot","safe_to_clear":false}));
        let mut restarted=Hub::new();android_host(&mut restarted,false);
        assert_eq!(restarted.fence_android("user",&newest,"host~session","prompt","v1").unwrap(),
            json!({"status":"unknown_old_boot","safe_to_clear":false}));
    }
    #[test]fn unresolved_ledger_stays_full_and_old_boot_probes_are_not_stored(){
        let mut hub=Hub::new();android_host(&mut hub,false);
        let boot=hub.boot.clone();
        for _ in 0..MAX_ANDROID_RECORDS {
            let id=android_id(&hub);
            hub.android_records.insert(("user".into(),id),AndroidRecord{
                host:"host".into(),session:"session".into(),op:"prompt".into(),incarnation:"v1".into(),
                digest:None,command_id:None,state:AndroidState::Delivered,fenced:true,
            });
        }
        assert_eq!(hub.android_records.len(),MAX_ANDROID_RECORDS);
        assert_eq!(hub.inventory("user")["hub_boot"],boot);
        assert_eq!(hub.boot,boot);
        let absent=android_id(&hub);
        assert_eq!(hub.fence_android("user",&absent,"host~session","prompt","v1").unwrap(),
            json!({"status":"absent_fenced","safe_to_clear":true}));
        assert!(hub.enqueue_android("user","host","session","prompt",android_payload(&hub,&absent)).is_err());
        assert_eq!(hub.android_records.len(),MAX_ANDROID_RECORDS);
        let unknown=format!("{}-{}",Uuid::new_v4().simple(),Uuid::new_v4());
        for _ in 0..100 {
            assert_eq!(hub.fence_android("user",&unknown,"host~session","prompt","v1").unwrap(),
                json!({"status":"unknown_old_boot","safe_to_clear":false}));
        }
        assert_eq!(hub.android_records.len(),MAX_ANDROID_RECORDS);
    }
}
