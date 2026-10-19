//! Owner-controlled, fail-closed daemon identity transition. The journal stays
//! locked to one session and one replacement; peers cannot authorize it.
use crate::{Context,State,invalid,hash,transaction,read_private,MAX_STATE};
use serde::{Deserialize,Serialize};
use serde_json::json;
use std::io;
#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Change {
    pub session_id:String,
    pub previous_assignments_sha256:String,
    pub replacement_assignments_sha256:Option<String>,
}
pub fn begin(context:&Context,session:&str)->io::Result<()> {
    context.assignment(session)?;
    transaction(context,|state|{
        if state.isolation_change.is_some(){return Err(invalid("an isolation change is already pending; inspect or stop the run"));}
        state.isolation_change=Some(Change{session_id:session.into(),previous_assignments_sha256:hash(&context.assignments)?,replacement_assignments_sha256:None});
        state.paused=true;state.reason="Owner isolation change in progress; all peer admission is held".into();
        Ok(())
    })
}
pub fn authorized(context:&Context,session:&str)->io::Result<bool>{
    transaction(context,|state|Ok(state.isolation_change.as_ref().is_some_and(|change|change.session_id==session)))
}
pub fn held(context:&Context)->io::Result<bool>{transaction(context,|state|Ok(state.isolation_change.is_some()))}
fn validate_replacement(old:&Context,new:&Context,change:&Change)->io::Result<()> {
    old.validate()?;new.validate()?;
    let mut expected=old.clone();
    let index=expected.assignments.iter().position(|row|row.session_id==change.session_id).ok_or_else(||invalid("isolation target disappeared"))?;
    let actual=new.assignment(&change.session_id)?;
    expected.assignments[index].pid=actual.pid;
    expected.assignments[index].cwd=actual.cwd.clone();
    if &expected!=new || change.previous_assignments_sha256!=hash(&old.assignments)?{
        return Err(invalid("isolation change cannot alter charter, task, role, scope, budget or another identity"));
    }
    Ok(())
}
pub fn rebind(old:&Context,session:&str,pid:i32,cwd:&str)->io::Result<Context>{
    let mut new=old.clone();
    let row=new.assignments.iter_mut().find(|row|row.session_id==session).ok_or_else(||invalid("isolation target disappeared"))?;
    row.pid=pid;row.cwd=cwd.into();
    transaction(old,|state|{
        let change=state.isolation_change.as_mut().filter(|change|change.session_id==session&&change.replacement_assignments_sha256.is_none()).ok_or_else(||invalid("isolation change was not approved"))?;
        validate_replacement(old,&new,change)?;
        let digest=hash(&new.assignments)?;
        change.replacement_assignments_sha256=Some(digest.clone());state.assignments_sha256=digest;
        Ok(())
    })?;
    Ok(new)
}
/// Old hosts intentionally cannot use their stale context for normal policy
/// transactions once the replacement is bound. Only this exact replacement is
/// accepted, and admission remains held until every host acknowledges it.
pub fn verify_reconfiguration(old:&Context,new:&Context)->io::Result<()> {
    let state:State=read_private(&old.state_path,MAX_STATE)?;
    let change=state.isolation_change.as_ref().ok_or_else(||invalid("no owner isolation change"))?;
    validate_replacement(old,new,change)?;
    let digest=hash(&new.assignments)?;
    if state.charter_sha256!=old.charter_sha256 || !state.paused || change.replacement_assignments_sha256.as_deref()!=Some(&digest) || state.assignments_sha256!=digest {
        return Err(invalid("isolation replacement has not been bound by the owner controller"));
    }
    Ok(())
}
pub fn finish(context:&Context)->io::Result<()> {
    transaction(context,|state|{
        let change=state.isolation_change.take().ok_or_else(||invalid("no isolation change to finish"))?;
        state.paused=true;state.reason="Isolation change verified; owner must review and continue the fleet".into();
        state.observations.push(json!({"event":"owner_isolation_change","session_id":change.session_id,"assignments_sha256":state.assignments_sha256,"at":crate::unix_now()}));
        if state.observations.len()>256{state.observations.remove(0);}
        Ok(())
    })
}
