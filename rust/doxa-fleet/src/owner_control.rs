//! Private owner requests consumed by the foreground fleet controller.
use crate::{invalid,read_private};
use doxa_isolation::Profile;
use serde::{Deserialize,Serialize};
use std::{io,path::Path};
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IsolationRequest {
    pub id:String,pub session_id:String,pub expected_profile:Profile,pub target:Profile,
    pub confirm_native:bool,pub allow_internet:bool,
}
impl IsolationRequest {
    pub fn validate(&self)->io::Result<()> {
        if uuid::Uuid::parse_str(&self.id).is_err() || !doxa_state::valid_session_id(&self.session_id){return Err(invalid("invalid isolation request identity"));}
        if self.target==Profile::Native&&!self.confirm_native{return Err(invalid("native execution requires explicit owner confirmation"));}
        if matches!(self.target,Profile::Native|Profile::DockerOpen)&&!self.allow_internet{return Err(invalid("direct task internet requires explicit owner opt-in"));}
        Ok(())
    }
}
pub fn active(run:&Path)->io::Result<Option<IsolationRequest>> {
    match read_private::<IsolationRequest>(&run.join("isolation-active.json"),8192){
        Ok(request)=>{request.validate()?;Ok(Some(request))},
        Err(error) if error.kind()==io::ErrorKind::NotFound=>Ok(None),Err(error)=>Err(error),
    }
}
