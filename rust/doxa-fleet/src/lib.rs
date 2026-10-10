//! Host-owned fleet authority, communication admission, and independent review.
//! Reviewers receive bounded data and have no tools or mutable worker history.
pub mod judge;
pub mod calibration;
pub mod message_eval;
pub mod message_gate;
pub mod evidence;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, fs, io::{self, Read, Write}, os::{fd::AsRawFd, unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt}}, path::{Path, PathBuf}, time::{SystemTime, UNIX_EPOCH}};

pub const PREFIX: &str = "[DOXA-FLEET-V1] ";
pub const MAX_BODY: usize = 6_000;
pub const MAX_STATE: u64 = 4 * 1024 * 1024;
pub fn invalid(message: &str) -> io::Error { io::Error::new(io::ErrorKind::InvalidData, message) }
pub fn hash(value: &impl Serialize) -> io::Result<String> { Ok(format!("{:x}", Sha256::digest(serde_json::to_vec(value)?))) }
/// Stable public evidence identity. Short digest groups survive canonical text
/// scrubbing; the underlying host-owned artifact still binds the full SHA256.
pub fn evidence_id(value:&impl Serialize)->io::Result<String>{let digest=hash(value)?;Ok(format!("host-{}-{}-{}-{}",&digest[..16],&digest[16..32],&digest[32..48],&digest[48..]))}
pub fn unix_now() -> u64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs() }

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Mode { #[default] Off, Shadow, Enforce }
impl Mode { pub fn parse(value: &str) -> io::Result<Self> { match value { "off" => Ok(Self::Off), "shadow" => Ok(Self::Shadow), "enforce" => Ok(Self::Enforce), _ => Err(invalid("review mode must be off, shadow or enforce")) } } }

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ReviewConfig {
    pub supervisor: Option<judge::Model>, pub supervisor_mode: Mode,
    pub message_judge: Option<judge::Model>, pub message_mode: Mode,
    pub budget_usd: f64, pub max_calls: u64, pub interval_s: u64,
    /// Owner-approved conservative rates; unknown prices never imply free work.
    pub input_usd_per_million: f64, pub output_usd_per_million: f64,
    pub risk_threshold: f64, pub strict_unavailable: bool,
}
impl Default for ReviewConfig { fn default() -> Self { Self { supervisor:None, supervisor_mode:Mode::Enforce, message_judge:None, message_mode:Mode::Off, budget_usd:0.0, max_calls:200, interval_s:60, input_usd_per_million:100.0, output_usd_per_million:100.0, risk_threshold:0.5, strict_unavailable:false } } }
impl ReviewConfig {
    pub fn enabled(&self) -> bool { self.supervisor.is_some() || self.message_mode != Mode::Off }
    pub fn validate(&self) -> io::Result<()> {
        if self.enabled() && (!self.budget_usd.is_finite() || self.budget_usd <= 0.0 || self.budget_usd > 1_000_000.0) { return Err(invalid("independent fleet review requires a finite positive review budget")); }
        if self.message_mode != Mode::Off && self.message_judge.is_none() { return Err(invalid("message review requires a selected judge")); }
        if !(1..=10_000).contains(&self.max_calls) || !(5..=3600).contains(&self.interval_s) || ![self.input_usd_per_million,self.output_usd_per_million,self.risk_threshold].iter().all(|v| v.is_finite()) || self.input_usd_per_million <= 0.0 || self.output_usd_per_million <= 0.0 || !(0.0..=1.0).contains(&self.risk_threshold) { return Err(invalid("invalid fleet review limits or prices")); }
        if self.supervisor.as_ref().is_some_and(|model|model.provider=="jev") { return Err(invalid("Jev is a message judge; select an LLM alignment supervisor")); }
        if self.supervisor.is_some() && self.supervisor_mode == Mode::Off { return Err(invalid("selected alignment supervisor requires shadow or enforce mode")); }
        Ok(())
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Charter {
    pub version:u32, pub fleet_id:String, pub task:String, pub repo:String,
    pub allowed_paths:Vec<String>, pub required_evidence:Vec<String>,
    pub worker_limit:u64, pub run_budget_usd:Option<f64>, pub deadline:u64,
    pub human_actions:Vec<String>,
    #[serde(default,skip_serializing_if="Option::is_none")]
    pub test_recipe:Option<evidence::TestRecipe>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Assignment { pub id:String, pub session_id:String, pub pid:i32, pub role:String, pub task:String, pub cwd:String, #[serde(default)] pub base_commit:Option<String>, #[serde(default)] pub allowed_paths:Vec<String>, #[serde(default,skip_serializing_if="Vec::is_empty")] pub depends_on:Vec<String> }
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Context { pub charter:Charter, pub charter_sha256:String, pub assignments:Vec<Assignment>, pub review:ReviewConfig, pub state_path:PathBuf }
impl Context {
    pub fn validate(&self) -> io::Result<()> {
        self.review.validate()?;
        if let Some(recipe)=&self.charter.test_recipe { recipe.validate()?; }
        if self.charter.version!=1 || self.charter.task.trim().is_empty() || self.charter.task.len()>64*1024 || self.charter_sha256!=hash(&self.charter)? || !self.state_path.is_absolute() || self.assignments.is_empty() || self.assignments.len()>1025
            || self.charter.allowed_paths.is_empty() || self.charter.allowed_paths.iter().any(|path|path.starts_with('/')||path.len()>512||path.split('/').any(|part|part=="..")||path.chars().any(char::is_control)) {return Err(invalid("invalid immutable fleet charter"));}
        for (index,row) in self.assignments.iter().enumerate() {
            if row.pid<=0 || row.id.is_empty() || row.session_id.is_empty() || !matches!(row.role.as_str(),"worker"|"coordinator") || row.task.trim().is_empty() || row.task.len()>64*1024 || row.base_commit.as_ref().is_some_and(|id|!(40..=64).contains(&id.len())||!id.bytes().all(|byte|byte.is_ascii_hexdigit())) || self.assignments[..index].iter().any(|prior|prior.id==row.id||prior.session_id==row.session_id||prior.pid==row.pid) {return Err(invalid("invalid host-issued fleet assignment"));}
            if row.allowed_paths.iter().any(|path| path.is_empty() || path.starts_with('/') || path.len()>512 || path.split('/').any(|part|part=="..") || path.chars().any(char::is_control) || !self.charter.allowed_paths.iter().any(|prefix| path_within(path,prefix))) {return Err(invalid("assignment path exceeds the owner-approved charter"));}
            if row.depends_on.len()>64 || (!row.depends_on.is_empty() && row.role!="worker") || row.depends_on.iter().any(|id|self.assignments[..index].iter().all(|prior|prior.id!=*id||prior.role!="worker")) || row.depends_on.iter().enumerate().any(|(at,id)|row.depends_on[..at].contains(id)) {return Err(invalid("assignment dependencies must reference unique earlier workers"));}
        }
        if self.assignments.iter().any(|row|!row.depends_on.is_empty())
            && self.assignments.iter().filter(|row|row.role=="coordinator").count()!=1 {
            return Err(invalid("dependent assignments require one host-issued coordinator"));
        }
        Ok(())
    }
    pub fn assignment(&self,id:&str)->io::Result<&Assignment>{self.assignments.iter().find(|row|row.session_id==id).ok_or_else(||invalid("session is outside the approved fleet"))}
}
pub fn path_within(path:&str,prefix:&str)->bool { prefix.is_empty() || path==prefix || path.starts_with(&format!("{}/",prefix.trim_end_matches('/'))) }
impl Assignment {
    pub fn permits(&self,charter:&Charter,path:&str)->bool {
        charter.allowed_paths.iter().any(|prefix|path_within(path,prefix)) &&
            (self.allowed_paths.is_empty() || self.allowed_paths.iter().any(|prefix|path_within(path,prefix)))
    }
    pub fn effective_paths<'a>(&'a self,charter:&'a Charter)->&'a [String] {
        if self.allowed_paths.is_empty(){&charter.allowed_paths}else{&self.allowed_paths}
    }
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all="snake_case")]
pub enum Kind { #[default] Status, Question, Evidence, Proposal, TaskRequest, Completion, Handoff, Ack, Confirm }
impl Kind { pub fn parse(value:&str)->io::Result<Self>{serde_json::from_value(json!(value)).map_err(|_|invalid("unknown fleet message kind"))} pub fn ordinary(self)->bool{matches!(self,Self::Status|Self::Evidence)} }
/// Receiver's bounded interpretation of one exact handoff. This is untrusted
/// peer evidence, never a task amendment or proof that the work is correct.
#[derive(Clone,Debug,PartialEq,Eq,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HandoffReadback { pub next_action:String, pub assumptions:Vec<String>, pub open_questions:Vec<String> }
impl HandoffReadback {
    pub fn validate(&self)->io::Result<()> {
        fn bounded(text:&str,limit:usize)->bool { !text.trim().is_empty()&&text.len()<=limit&&!text.chars().any(char::is_control) }
        if !bounded(&self.next_action,320)||self.assumptions.len()>3||self.open_questions.len()>3
            ||self.assumptions.iter().chain(&self.open_questions).any(|text|!bounded(text,160)) {
            return Err(invalid("handoff read-back exceeds bounded fields"));
        }
        Ok(())
    }
}
/// A correction requires a fresh receiver read-back in a new handoff chain;
/// prose cannot silently settle a disagreement.
#[derive(Clone,Debug,PartialEq,Eq,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HandoffResponse { pub agrees:bool, pub correction:Option<String> }
impl HandoffResponse {
    pub fn validate(&self)->io::Result<()> {
        let valid=self.correction.as_ref().is_none_or(|text|!text.trim().is_empty()&&text.len()<=600&&!text.chars().any(char::is_control));
        if !valid||self.agrees==self.correction.is_some(){return Err(invalid("handoff confirmation must agree or give one bounded correction"));}
        Ok(())
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Envelope {
    pub v:u32,pub fleet_id:String,pub message_id:String,pub from_session:String,pub to_session:String,
    pub kind:Kind,pub assignment_id:String,pub charter_sha256:String,pub in_reply_to:Option<String>,pub body:String,
    pub artifact_refs:Vec<String>,pub requested_action:Option<String>,pub hop:u8,
    #[serde(default,skip_serializing_if="Option::is_none")] pub readback:Option<HandoffReadback>,
    #[serde(default,skip_serializing_if="Option::is_none")] pub handoff_response:Option<HandoffResponse>,
}
impl Envelope {
    pub fn issue(context:&Context,from:&str,to:&str,kind:Kind,body:String,reply:Option<String>)->io::Result<Self>{
        let row=context.assignment(from)?;context.assignment(to)?;
        let hop=if let Some(parent)=&reply{transaction(context,|state|state.traces.get(parent).map(|trace|trace.hop.saturating_add(1)).ok_or_else(||invalid("unknown fleet reply ancestry")))?}else{0};
        Ok(Self{v:1,fleet_id:context.charter.fleet_id.clone(),message_id:uuid::Uuid::new_v4().to_string(),from_session:from.into(),to_session:to.into(),kind,assignment_id:row.id.clone(),charter_sha256:context.charter_sha256.clone(),in_reply_to:reply,body,artifact_refs:Vec::new(),requested_action:None,hop,readback:None,handoff_response:None})
    }
    pub fn wire(&self)->io::Result<String>{Ok(format!("{PREFIX}{}",serde_json::to_string(self)?))}
    pub fn parse(wire:&str)->io::Result<Self>{if wire.len()>24*1024{return Err(invalid("fleet envelope exceeds bounds"));}serde_json::from_str(wire.strip_prefix(PREFIX).ok_or_else(||invalid("free-form message cannot enter a supervised fleet"))?).map_err(|_|invalid("invalid fleet envelope schema"))}
    fn text_bytes(&self)->usize {
        self.body.len()
            +self.readback.as_ref().map_or(0,|row|row.next_action.len()+row.assumptions.iter().map(String::len).sum::<usize>()+row.open_questions.iter().map(String::len).sum::<usize>())
            +self.handoff_response.as_ref().and_then(|row|row.correction.as_ref()).map_or(0,String::len)
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct State {
    pub charter_sha256:String,#[serde(default)] pub assignments_sha256:String,pub paused:bool,pub reason:String,pub supervisor_status:String,
    pub reserved_usd:f64,pub actual_estimated_usd:f64,pub calls:u64,pub accounting_unknown:bool,
    pub received:BTreeMap<String,u64>,pub minute:u64,pub message_count:u64,pub total_bytes:u64,
    pub observations:Vec<Value>,pub artifacts:BTreeMap<String,Value>,pub last_supervisor_at:u64,
    #[serde(default)] pub semantic_cache:BTreeMap<String,CachedSemantic>,
    #[serde(default)] pub traces:BTreeMap<String,MessageTrace>,
    #[serde(default)] pub dependency_releases:BTreeMap<String,DependencyRelease>,
    #[serde(default)] pub dispatched_assignments:BTreeMap<String,bool>,
    #[serde(default)] pub recent_messages:Vec<Envelope>,
}
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MessageTrace {pub from:String,pub to:String,pub hop:u8,#[serde(default)] pub seq:u64,#[serde(default)] pub kind:Kind,#[serde(default)] pub artifact_refs:Vec<String>,#[serde(default,skip_serializing_if="Option::is_none")] pub in_reply_to:Option<String>,#[serde(default,skip_serializing_if="Option::is_none")] pub readback:Option<HandoffReadback>,#[serde(default,skip_serializing_if="Option::is_none")] pub handoff_response:Option<HandoffResponse>}
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DependencyRelease {pub assignment_id:String,pub handoff_id:String,pub artifact_refs:Vec<String>,#[serde(default)] pub checkpoint_id:String,#[serde(default)] pub checkpoint_turn_serial:u64,#[serde(default)] pub checkpoint_turn_sha256:String,#[serde(default)] pub turn_serial:u64,pub last_turn_sha256:String,pub at:u64}
#[derive(Clone,Debug,PartialEq,Eq,Serialize,Deserialize)]
pub struct AcceptedHandoff {pub handoff_id:String,pub artifact_refs:Vec<String>,pub checkpoint_id:String,pub checkpoint_turn_serial:u64,pub checkpoint_turn_sha256:String,pub readback:HandoffReadback,pub response:HandoffResponse}

impl AcceptedHandoff { pub fn resolved(&self)->bool { self.response.agrees&&self.readback.open_questions.is_empty() } }

/// A coordinator's ACK and the sender's confirmation must echo the same
/// host-owned checkpoint. This is provenance, not a model judgment of quality.
pub fn accepted_handoff(context:&Context,state:&State,predecessor:&str)->Option<AcceptedHandoff>{
    let worker=context.assignments.iter().find(|row|row.id==predecessor&&row.role=="worker")?;
    let coordinator=context.assignments.iter().find(|row|row.role=="coordinator")?;
    // A newer handoff supersedes older agreement, even while its ACK or
    // confirmation is pending. Lexical UUID order is never message order.
    let (handoff_id,handoff)=state.traces.iter().filter(|(_,row)|row.kind==Kind::Handoff
        &&row.from==worker.session_id&&row.to==coordinator.session_id).max_by_key(|(_,row)|row.seq)?;
    if handoff.seq==0||handoff.artifact_refs.is_empty(){return None;}
    let (ack_id,ack)=state.traces.iter().filter(|(_,row)|row.kind==Kind::Ack
        &&row.from==coordinator.session_id&&row.to==worker.session_id
        &&row.in_reply_to.as_deref()==Some(handoff_id.as_str())).max_by_key(|(_,row)|row.seq)?;
    let (_,confirm)=state.traces.iter().filter(|(_,row)|row.kind==Kind::Confirm
        &&row.from==worker.session_id&&row.to==coordinator.session_id
        &&row.in_reply_to.as_deref()==Some(ack_id.as_str())).max_by_key(|(_,row)|row.seq)?;
    if ack.seq<=handoff.seq||confirm.seq<=ack.seq
        ||ack.artifact_refs!=handoff.artifact_refs||confirm.artifact_refs!=handoff.artifact_refs{return None;}
    let (Some(readback),Some(response))=(ack.readback.as_ref(),confirm.handoff_response.as_ref()) else {return None;};
    if readback.validate().is_err()||response.validate().is_err(){return None;}
    let (checkpoint_id,checkpoint_turn_serial,checkpoint_turn_sha256)=handoff.artifact_refs.iter().find_map(|id|{
        let row=state.artifacts.get(id)?;
        if row["kind"]!="host_checkpoint"||row["assignment_id"]!=worker.id
            ||row["session_id"]!=worker.session_id||row["git_observation_available"]!=true
            ||!row["changed_paths"].is_string()||row["last_turn_kind"]!="turn_done"
            ||row["last_turn"].is_null()||row["running"]!=false||row["queued"].as_u64()!=Some(0){return None;}
        let serial=row["turn_serial"].as_u64().filter(|serial|*serial>0)?;
        let digest=row["last_turn_sha256"].as_str()?;
        if digest!=hash(&row["last_turn"]).ok()? {return None;}
        Some((id.clone(),serial,digest.to_owned()))
    })?;
    Some(AcceptedHandoff{handoff_id:handoff_id.clone(),artifact_refs:handoff.artifact_refs.clone(),checkpoint_id,checkpoint_turn_serial,checkpoint_turn_sha256,readback:readback.clone(),response:response.clone()})
}

pub fn predecessor_released(context:&Context,state:&State,predecessor:&str,turn_serial:u64,last_turn_sha256:&str)->bool{
    let Some(release)=state.dependency_releases.get(predecessor) else{return false;};
    turn_serial>0&&release.assignment_id==predecessor&&release.turn_serial==turn_serial&&release.last_turn_sha256==last_turn_sha256
        && accepted_handoff(context,state,predecessor).is_some_and(|handoff|
            handoff.resolved()&&handoff.handoff_id==release.handoff_id&&handoff.artifact_refs==release.artifact_refs
            &&handoff.checkpoint_id==release.checkpoint_id
            &&handoff.checkpoint_turn_serial==release.checkpoint_turn_serial
            &&handoff.checkpoint_turn_sha256==release.checkpoint_turn_sha256
            &&handoff.checkpoint_turn_serial.checked_add(1)==Some(turn_serial))
}
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CachedSemantic {pub envelope_sha256:String,pub result:Result<SemanticVerdict,String>}
pub fn read_private<T: for<'a> Deserialize<'a>>(path:&Path,max:u64)->io::Result<T>{
    let mut file=fs::OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK).open(path)?;
    let meta=file.metadata()?;
    if !meta.is_file()||meta.nlink()!=1||meta.uid()!=unsafe{libc::geteuid()}||meta.mode()&0o077!=0||meta.len()>max{return Err(invalid("untrusted fleet state file"));}
    let mut raw=Vec::new();Read::by_ref(&mut file).take(max+1).read_to_end(&mut raw)?;
    if raw.len() as u64>max{return Err(invalid("fleet state exceeds bound"));}serde_json::from_slice(&raw).map_err(|_|invalid("invalid fleet state"))
}
pub fn save_private(path:&Path,value:&impl Serialize)->io::Result<()> {
    let parent=path.parent().ok_or_else(||invalid("fleet file has no parent"))?;
    let meta=fs::symlink_metadata(parent)?;
    if !meta.is_dir()||meta.uid()!=unsafe{libc::geteuid()}||meta.mode()&0o077!=0{return Err(invalid("untrusted fleet directory"));}
    let raw=serde_json::to_vec(value)?;if raw.len() as u64>MAX_STATE{return Err(invalid("fleet state exceeds bound"));}
    let mut file=tempfile::NamedTempFile::new_in(parent)?;file.as_file().set_permissions(fs::Permissions::from_mode(0o600))?;
    file.write_all(&raw)?;file.as_file().sync_all()?;file.persist(path).map_err(|err|err.error)?;fs::File::open(parent)?.sync_all()
}
/// All host processes serialize policy and reservations on the same durable file.
pub fn transaction<T>(context:&Context,work:impl FnOnce(&mut State)->io::Result<T>)->io::Result<T>{
    let lock=fs::OpenOptions::new().read(true).write(true).create(true).mode(0o600).custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK).open(context.state_path.with_extension("lock"))?;
    let meta=lock.metadata()?;
    if !meta.is_file()||meta.nlink()!=1||meta.uid()!=unsafe{libc::geteuid()}||meta.mode()&0o077!=0{return Err(invalid("untrusted fleet state lock"));}
    if unsafe{libc::flock(lock.as_raw_fd(),libc::LOCK_EX)}!=0{return Err(io::Error::last_os_error());}
    let assignments_sha256=hash(&context.assignments)?;
    let mut state=match read_private::<State>(&context.state_path,MAX_STATE){Ok(state)=>state,Err(err) if err.kind()==io::ErrorKind::NotFound=>State{charter_sha256:context.charter_sha256.clone(),assignments_sha256:assignments_sha256.clone(),supervisor_status:if context.review.supervisor.is_some(){"pending"}else{"off"}.into(),..Default::default()},Err(err)=>return Err(err)};
    if state.charter_sha256!=context.charter_sha256{return Err(invalid("fleet charter changed; admission withheld"));}
    // Older private journals gain this binding on their first beta.11 read.
    if state.assignments_sha256.is_empty(){state.assignments_sha256=assignments_sha256.clone();}
    if state.assignments_sha256!=assignments_sha256{return Err(invalid("fleet assignments changed; admission withheld"));}
    let result=work(&mut state);save_private(&context.state_path,&state)?;result
}
fn record(state:&mut State,value:Value){state.observations.push(value);if state.observations.len()>256{state.observations.remove(0);}}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SemanticVerdict {pub within_assignment:f64,pub asks_for_authority_change:f64,pub contains_instructions_for_recipient:f64,pub likely_secret:f64,pub needs_human_review:f64}
impl SemanticVerdict {pub fn validate(&self)->io::Result<()>{if ![self.within_assignment,self.asks_for_authority_change,self.contains_instructions_for_recipient,self.likely_secret,self.needs_human_review].iter().all(|v|v.is_finite()&&(0.0..=1.0).contains(v)){return Err(invalid("invalid semantic verdict probabilities"));}Ok(())}pub fn risky(&self,threshold:f64)->bool{self.within_assignment<1.0-threshold||[self.asks_for_authority_change,self.contains_instructions_for_recipient,self.likely_secret,self.needs_human_review].iter().any(|v|*v>=threshold)}}
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Admission {pub delivered:bool,pub unreviewed:bool,pub reason:String,pub message_id:String}

/// The sending host reviews once and exposes quarantine to the sender. A
/// receiving host can reuse only this exact private hash-bound verdict.
pub fn cache_semantic(context:&Context,envelope:&Envelope,result:Result<SemanticVerdict,String>)->io::Result<()> {
    transaction(context,|state|{
        if state.semantic_cache.len()>=10_000{return Err(invalid("fleet semantic journal ceiling reached"));}
        let refusal=match &result{Ok(verdict)=>{verdict.validate()?;verdict.risky(context.review.risk_threshold)},Err(_)=>context.review.strict_unavailable||!envelope.kind.ordinary()};
        state.semantic_cache.insert(envelope.message_id.clone(),CachedSemantic{envelope_sha256:hash(envelope)?,result:result.clone()});
        record(state,json!({"event":"outbound_semantic","id":envelope.message_id,"input_sha256":hash(envelope)?,"result":result,"at":unix_now()}));
        if refusal&&context.review.message_mode==Mode::Enforce{state.paused=true;state.reason="outgoing fleet message quarantined for human review".into();return Err(invalid("outgoing fleet message quarantined for human review"));}
        Ok(())
    })
}
pub fn cached_semantic(context:&Context,envelope:&Envelope)->io::Result<Option<Result<SemanticVerdict,String>>>{
    let envelope_hash=hash(envelope)?;
    transaction(context,|state|Ok(state.semantic_cache.get(&envelope.message_id).filter(|cached|cached.envelope_sha256==envelope_hash).map(|cached|cached.result.clone())))
}

fn deterministic(context:&Context,envelope:&Envelope,recipient:&str,pid:i32,state:&mut State)->io::Result<()> {
    let from=context.assignment(&envelope.from_session)?;let target=context.assignment(recipient)?;
    if !target.depends_on.is_empty()&&!state.dispatched_assignments.get(&target.id).copied().unwrap_or(false){return Err(invalid("dependent worker awaits host dispatch"));}
    if envelope.v!=1||envelope.fleet_id!=context.charter.fleet_id||envelope.charter_sha256!=context.charter_sha256||envelope.to_session!=recipient||from.pid!=pid||envelope.assignment_id!=from.id||envelope.from_session==recipient||uuid::Uuid::parse_str(&envelope.message_id).is_err(){return Err(invalid("fleet sender, assignment or scope is not verified"));}
    if envelope.body.trim().is_empty()||envelope.body.len()>MAX_BODY||envelope.hop>4||!envelope.artifact_refs.iter().all(|id|state.artifacts.contains_key(id))||envelope.artifact_refs.len()>8{return Err(invalid("fleet message bounds or artifact provenance refused"));}
    match envelope.kind {
        Kind::Ack if envelope.readback.as_ref().is_some_and(|row|row.validate().is_ok())&&envelope.handoff_response.is_none()=>{},
        Kind::Confirm if envelope.readback.is_none()&&envelope.handoff_response.as_ref().is_some_and(|row|row.validate().is_ok())=>{},
        Kind::Ack|Kind::Confirm=>return Err(invalid("handoff ACK requires read-back; confirmation requires a bounded response")),
        _ if envelope.readback.is_none()&&envelope.handoff_response.is_none()=>{},
        _=>return Err(invalid("read-back fields are only valid on handoff ACK and confirmation")),
    }
    if let Some(parent)=&envelope.in_reply_to{
        let trace=state.traces.get(parent).ok_or_else(||invalid("unknown fleet reply ancestry"))?;
        let same_pair=(trace.from==envelope.from_session&&trace.to==recipient)||(trace.to==envelope.from_session&&trace.from==recipient);
        if !same_pair||envelope.hop!=trace.hop.saturating_add(1){return Err(invalid("fleet reply provenance or hop count changed"));}
        match envelope.kind {
            Kind::Ack if trace.kind==Kind::Handoff && trace.to==envelope.from_session && trace.from==recipient && envelope.artifact_refs==trace.artifact_refs => {},
            Kind::Confirm if trace.kind==Kind::Ack && trace.to==envelope.from_session && trace.from==recipient && envelope.artifact_refs==trace.artifact_refs => {},
            Kind::Ack|Kind::Confirm => return Err(invalid("handoff acknowledgment must echo the parent's host artifacts and reverse direction")),
            _ => {},
        }
    }else if envelope.hop!=0{return Err(invalid("root fleet message cannot claim reply hops"));}
    if matches!(envelope.kind,Kind::Handoff) && (envelope.in_reply_to.is_some() || envelope.artifact_refs.is_empty()) || matches!(envelope.kind,Kind::Ack|Kind::Confirm) && envelope.in_reply_to.is_none() {
        return Err(invalid("handoff requires host artifacts and a bounded acknowledgment chain"));
    }
    if envelope.requested_action.is_some()||matches!(envelope.kind,Kind::TaskRequest){return Err(invalid("task changes require host-issued assignment and human review"));}
    if matches!(envelope.kind,Kind::Completion){
        evidence::completion_snapshot(context,state,envelope)?;
    }
    if context.charter.deadline>0&&unix_now()>=context.charter.deadline{return Err(invalid("fleet charter deadline reached"));}
    if state.received.contains_key(&envelope.message_id){return Err(invalid("duplicate fleet message; turn not started"));}
    let text_bytes=envelope.text_bytes();
    if state.received.len()>=10_000||state.total_bytes.saturating_add(text_bytes as u64)>8*1024*1024{return Err(invalid("fleet message journal or byte ceiling reached"));}
    let minute=unix_now()/60;if state.minute!=minute{state.minute=minute;state.message_count=0;}
    if state.message_count>=60{return Err(invalid("fleet message rate ceiling reached"));}
    if (state.paused||state.accounting_unknown)&&!envelope.kind.ordinary(){return Err(invalid("fleet is paused; actionable messages withheld"));}
    Ok(())
}

/// Called after LORE scrubbing and kernel provenance checks, before enqueue.
pub fn admit(context:&Context,envelope:&Envelope,recipient:&str,pid:i32,semantic:Option<Result<SemanticVerdict,String>>)->io::Result<Admission>{
    context.validate()?;
    transaction(context,|state|{
        let checked=deterministic(context,envelope,recipient,pid,state);
        let mut admission=Admission{delivered:checked.is_ok(),unreviewed:false,reason:checked.err().map(|err|err.to_string()).unwrap_or_else(||"deterministic checks passed".into()),message_id:envelope.message_id.clone()};
        if admission.delivered && context.review.message_mode!=Mode::Off {
            match semantic {
                Some(Ok(verdict))=>{verdict.validate()?;let risky=verdict.risky(context.review.risk_threshold);record(state,json!({"event":"message_verdict","id":envelope.message_id,"verdict":verdict,"at":unix_now()}));if risky&&context.review.message_mode==Mode::Enforce{admission.delivered=false;admission.reason="semantic review requires human review".into();state.paused=true;state.reason=admission.reason.clone();}},
                _=>{admission.unreviewed=true;admission.reason="semantic review unavailable".into();if context.review.message_mode==Mode::Enforce&&(context.review.strict_unavailable||!envelope.kind.ordinary()){admission.delivered=false;state.paused=true;state.reason=admission.reason.clone();}},
            }
        }
        if admission.delivered{
            let seq=state.received.len() as u64+1;
            state.received.insert(envelope.message_id.clone(),unix_now());state.traces.insert(envelope.message_id.clone(),MessageTrace{from:envelope.from_session.clone(),to:recipient.into(),hop:envelope.hop,seq,kind:envelope.kind,artifact_refs:envelope.artifact_refs.clone(),in_reply_to:envelope.in_reply_to.clone(),readback:envelope.readback.clone(),handoff_response:envelope.handoff_response.clone()});state.message_count+=1;state.total_bytes+=envelope.text_bytes() as u64;
            state.recent_messages.push(envelope.clone());while state.recent_messages.len()>8||state.recent_messages.iter().map(|row|row.body.len()).sum::<usize>()>12*1024{state.recent_messages.remove(0);}
        }
        record(state,json!({"event":"admission","admission":admission,"from":envelope.from_session,"to":recipient,"kind":envelope.kind,"input_sha256":hash(envelope)?,"at":unix_now()}));
        Ok(admission)
    })
}
/// Deterministic checks must run before sending a message to a judgment service.
pub fn validate_before_review(context:&Context,envelope:&Envelope,recipient:&str,pid:i32)->io::Result<()> {
    context.validate()?;
    transaction(context,|state|deterministic(context,envelope,recipient,pid,&mut state.clone()))
}

#[derive(Clone,Copy,Debug,PartialEq,Eq,Serialize,Deserialize)]
#[serde(rename_all="snake_case")]
pub enum Alignment { Aligned, Uncertain, Drifted, Blocked }
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SupervisorVerdict {pub verdict:Alignment,pub evidence_refs:Vec<String>,pub charter_clause:String,pub reason:String,pub recommended_action:String}
pub fn apply_supervisor(context:&Context,result:Result<SupervisorVerdict,String>)->io::Result<State>{
    transaction(context,|state|{
        let verdict=match result {
            Ok(verdict) if verdict.reason.len()<=2000&&verdict.charter_clause.len()<=512&&verdict.recommended_action.len()<=512&&verdict.evidence_refs.len()<=16&&verdict.evidence_refs.iter().all(|id|state.artifacts.contains_key(id))&&(!matches!(verdict.verdict,Alignment::Drifted|Alignment::Blocked)||!verdict.evidence_refs.is_empty())=>{
                state.supervisor_status=serde_json::to_value(verdict.verdict)?.as_str().unwrap_or("uncertain").into();
                if context.review.supervisor_mode==Mode::Enforce&&verdict.verdict!=Alignment::Aligned{state.paused=true;state.reason=verdict.reason.clone();}
                json!({"event":"supervisor_verdict","verdict":verdict,"at":unix_now()})
            },
            _=>{state.supervisor_status="unavailable".into();if context.review.supervisor_mode==Mode::Enforce{state.paused=true;state.reason="independent supervisor failed or returned unverifiable evidence".into();}json!({"event":"supervisor_unavailable","at":unix_now()})},
        };
        state.last_supervisor_at=unix_now();record(state,verdict);Ok(state.clone())
    })
}
pub fn resume_review(context:&Context,expected_hash:&str)->io::Result<State>{
    context.validate()?;if expected_hash!=context.charter_sha256{return Err(invalid("human resume requires the reviewed charter hash"));}
    transaction(context,|state|{if state.accounting_unknown{return Err(invalid("review spend is unknown; reconcile before resume"));}if state.reserved_usd>=context.review.budget_usd||state.calls>=context.review.max_calls{return Err(invalid("review budget exhausted"));}state.paused=false;state.reason.clear();record(state,json!({"event":"human_resume","charter_sha256":expected_hash,"at":unix_now()}));Ok(state.clone())})
}
