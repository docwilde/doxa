//! Optional native peer bridge bootstrap; authorization stays in remote_policy.
use std::{io, path::Path, process::{Child,Command,Stdio}, sync::Mutex, time::{Duration,Instant}};
use serde_json::{json,Value};
use doxa_lore::LoreClient;
use doxa_peers::{Registry,delivery::{self,Ledger,PeerFrame},peernet};

pub struct Bootstrap { child:Child,deadline:Instant,cancelled:bool }
impl Bootstrap {
    pub fn request(runtime:&Path)->io::Result<Option<Self>> {
        if !doxa_peers::remote_policy::remote_enabled(){return Ok(None);}
        let child=Command::new(std::env::current_exe()?).arg("__peernet-ensure").arg(runtime)
            .current_dir(runtime).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn()?;
        Ok(Some(Self{child,deadline:Instant::now()+Duration::from_secs(3),cancelled:false}))
    }
    pub fn poll(&mut self)->Option<bool>{match self.child.try_wait(){Ok(Some(status))=>Some(status.success()&&!self.cancelled),Ok(None) if !self.cancelled&&Instant::now()>=self.deadline=>{let _=self.child.kill();self.cancelled=true;None},Ok(None)=>None,Err(_)=>{let _=self.child.kill();self.cancelled=true;None}}}
}
impl Drop for Bootstrap{fn drop(&mut self){let _=self.child.kill();let _=self.child.wait();}}

fn scrub_value(value:&mut Value,lore:&mut LoreClient)->io::Result<()> {
    match value{Value::String(text)=>*text=lore.scrub(text).map_err(|_|io::Error::other("LORE scrub unavailable"))?,Value::Array(values)=>for value in values{scrub_value(value,lore)?},Value::Object(values)=>for value in values.values_mut(){scrub_value(value,lore)?},_=>{}}Ok(())
}

/// Shared service watches the native registry and retains the existing scoped,
/// scrubbed local peer delivery path. It never exposes the daemon prompt/RPC wire.
pub fn serve(runtime:&Path)->io::Result<()> {
    if !doxa_peers::remote_policy::remote_enabled(){return Ok(());}
    let bridge=peernet::Bridge::bind(runtime)?;
    let registry=Registry::open(runtime)?;
    let lore=Mutex::new(LoreClient::open(Duration::from_secs(5)).map_err(|_|io::Error::other("LORE scrub unavailable"))?);
    let home=std::env::var_os("DOXA_HOME").filter(|v|!v.is_empty()).map(std::path::PathBuf::from)
        .unwrap_or_else(||std::path::PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".doxa"));
    let ledger=Ledger::new(std::env::var_os("DOXA_PEER_LEDGER").filter(|v|!v.is_empty()).map(std::path::PathBuf::from).unwrap_or_else(||home.join("peers/messages.jsonl")));
    let handler=|body:&Value|->io::Result<Value>{
        let mut lore=lore.lock().map_err(|_|io::Error::other("LORE unavailable"))?;
        let live=registry.read(&|text:&str|text.to_owned(),false,true)?;
        let mut value=match body["op"].as_str(){
            Some("roster")=>{let rows=live.into_iter().map(|peer|{let mut value=serde_json::to_value(peer).unwrap_or(Value::Null);value["socket_path"]=json!("");value["pid"]=json!(0);value["daemon_socket"]=Value::Null;value["origin"]=Value::Null;value}).collect::<Vec<_>>();json!({"count":rows.len(),"peers":rows})},
            Some("history")=>{let limit=body["limit"].as_u64().unwrap_or(50).clamp(1,200) as usize;let rows=ledger.recent(limit)?;json!({"count":rows.len(),"messages":rows})},
            Some("deliver")=>{
                let target=body["target"].as_str().filter(|s|!s.is_empty()&&s.len()<=128).ok_or_else(||io::Error::other("remote target required"))?;
                let matches=live.iter().filter(|peer|peer.session_id==target||peer.session_id.starts_with(target)).collect::<Vec<_>>();
                if matches.len()!=1{return Err(io::Error::other("remote target missing or ambiguous"));}let peer=matches[0];
                if Path::new(&peer.socket_path).parent()!=Some(runtime)||peer.daemon_socket.as_deref()==Some(&peer.socket_path){return Err(io::Error::other("target has no native peer inbox"));}
                let text=body["body"].as_str().filter(|s|!s.trim().is_empty()&&s.chars().count()<=delivery::MAX_BODY_CHARS).ok_or_else(||io::Error::other("bounded remote message required"))?;
                let from=body["from_id"].as_str().filter(|s|doxa_transcript::valid_session_id(s)).ok_or_else(||io::Error::other("remote sender identity required"))?;
                let mut frame=serde_json::to_value(PeerFrame{from_id:from.into(),from_title:body["from_title"].as_str().unwrap_or("remote peer").into(),body:text.into(),from_repo:body["from_repo"].as_str().map(str::to_owned),sent_at:doxa_peers::now(),kind:Some(body["kind"].as_str().filter(|s|matches!(*s,"direct"|"broadcast")).unwrap_or("direct").into())})?;
                scrub_value(&mut frame,&mut lore)?;
                if frame["from_id"]!=from{return Err(io::Error::other("LORE changed remote sender identity"));}
                let frame=serde_json::from_value(frame)?;delivery::send(Path::new(&peer.socket_path),&frame)?;
                json!({"delivered_to":peer.session_id,"title":peer.title})
            },_=>return Err(io::Error::other("unsupported remote peer operation")),
        };scrub_value(&mut value,&mut lore)?;Ok(value)
    };
    let mut empty_since=None;let mut next_registry_check=Instant::now();
    loop{
        if !doxa_peers::remote_policy::remote_enabled(){return Ok(());}
        if let Err(error)=bridge.poll(&handler){if error.kind()!=io::ErrorKind::BrokenPipe&&error.kind()!=io::ErrorKind::ConnectionReset{return Err(error);}}
        if Instant::now()<next_registry_check{std::thread::sleep(Duration::from_millis(50));continue;}
        next_registry_check=Instant::now()+Duration::from_secs(5);
        let live=registry.read(&|text:&str|text.to_owned(),false,true)?;
        if !live.is_empty(){empty_since=None;}else if let Some(since)=empty_since{
            if Instant::now().duration_since(since)>=peernet::BRIDGE_IDLE{
                let _lock=peernet::StartLock::acquire(runtime)?;
                if registry.read(&|text:&str|text.to_owned(),false,true)?.is_empty(){drop(bridge);return Ok(());}empty_since=None;
            }
        }else{empty_since=Some(Instant::now());}
        std::thread::sleep(Duration::from_millis(50));
    }
}
