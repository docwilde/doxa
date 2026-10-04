//! Outbound-only host connector for the private DOXA hub.
use super::{connect, remote_dangerous, bypass_opt_in, scrub_data, daemon, App};
use reqwest::{Client, Url};
use serde_json::{json, Value};
use std::{collections::HashMap, io, sync::Arc, time::{Duration,Instant}};

fn invalid(message:&'static str)->io::Error{io::Error::new(io::ErrorKind::InvalidInput,message)}
fn shorten(value:&mut Value,limit:usize){
    if let Some(text)=value.as_str().filter(|text|text.chars().count()>limit){
        *value=json!(format!("{}…",text.chars().take(limit).collect::<String>()));
    }
}
fn bounded_history(mut history:Value)->Value{
    if let Some(turns)=history["turns"].as_array_mut(){
        for turn in turns.iter_mut(){
            shorten(&mut turn["prompt"],2_000);
            shorten(&mut turn["text"],6_000);
            if let Some(tools)=turn["tools"].as_array_mut(){
                tools.truncate(4);
                for tool in tools.iter_mut(){
                    shorten(&mut tool["name"],120);
                    if !tool["result"].is_null(){
                        let detail=tool["result"].as_str().map(str::to_owned)
                            .unwrap_or_else(||tool["result"].to_string());
                        tool["result"]=json!(detail);
                        shorten(&mut tool["result"],2_000);
                    }
                }
            }
        }
    }
    let mut dropped=0usize;
    while history["turns"].as_array().is_some_and(|turns|turns.len()>1)
        && serde_json::to_vec(&history).is_ok_and(|bytes|bytes.len()>100_000){
        history["turns"].as_array_mut().unwrap().remove(0);dropped+=1;
    }
    history["dropped_turns"]=json!(history["dropped_turns"].as_u64().unwrap_or(0)+dropped as u64);
    history["ok"]=json!(true);
    history
}
fn valid_target(target:&str)->bool{target.split_once('~').is_some_and(|(host,session)|super::valid_id(host)&&super::valid_id(session))}
fn hub_url(raw:&str)->io::Result<Url>{
    let url=Url::parse(raw).map_err(|_|invalid("invalid hub URL"))?;
    if url.scheme()!="https"||!url.host_str().is_some_and(|host|host.ends_with(".ts.net"))
        || url.username()!=""||url.password().is_some()||url.query().is_some()||url.fragment().is_some()||url.path()!="/"{
        return Err(invalid("hub URL must be a private https://*.ts.net origin"));
    }
    Ok(url)
}
fn http_client()->io::Result<Client>{
    Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(5)).build().map_err(|_|io::Error::other("HTTP client unavailable"))
}
pub async fn client_action(args:&[String])->io::Result<()> {
    let [action,url,rest @ ..]=args else{return Err(invalid("usage: doxa remote list URL | send URL SESSION TEXT | answer URL SESSION REQUEST_ID allow|deny"))};
    let base=hub_url(url)?;let http=http_client()?;
    if action=="list"&&rest.is_empty(){
        let value:Value=http.get(base.join("api/sessions").map_err(|_|invalid("invalid hub route"))?)
            .send().await.map_err(|_|io::Error::other("hub unavailable"))?.error_for_status()
            .map_err(|_|io::Error::new(io::ErrorKind::PermissionDenied,"hub refused session list"))?
            .json().await.map_err(|_|io::Error::other("unreadable hub reply"))?;
        for session in value["sessions"].as_array().into_iter().flatten(){
            println!("{}  {}",session["id"].as_str().unwrap_or("?"),session["title"].as_str().unwrap_or("session"));
        }
        return Ok(());
    }
    let (session,operation,body)=match (action.as_str(),rest){
        ("send",[session,text]) if valid_target(session)&&!text.trim().is_empty()&&text.len()<=58_000=>
            (session.as_str(),"prompt",json!({"text":text})),
        ("answer",[session,id,decision]) if valid_target(session)&&super::valid_id(id)&&matches!(decision.as_str(),"allow"|"deny")=>
            (session.as_str(),"answer",json!({"id":id,"answer":{"decision":decision}})),
        _=>return Err(invalid("usage: doxa remote list URL | send URL SESSION TEXT | answer URL SESSION REQUEST_ID allow|deny")),
    };
    let path=format!("api/sessions/{session}/{operation}");
    let queued=post(&http,&base,&path,body,None).await?;
    let Some(command)=queued["command_id"].as_str().filter(|id|super::valid_id(id)) else{return Err(io::Error::other("hub did not return a command ID"))};
    for _ in 0..240{
        tokio::time::sleep(Duration::from_millis(250)).await;
        let path=format!("api/commands/{command}");
        let result:Value=http.get(base.join(&path).map_err(|_|invalid("invalid hub path"))?).send().await
            .map_err(|_|io::Error::other("hub status unavailable"))?.error_for_status()
            .map_err(|_|io::Error::other("hub command status refused"))?.json().await
            .map_err(|_|io::Error::other("unreadable command status"))?;
        match result["status"].as_str(){
            Some("accepted")=>{println!("{}",result["result"]);return Ok(());},
            Some("refused")=>return Err(io::Error::other(result["result"]["error"].as_str().unwrap_or("host refused command"))),
            Some("expired")=>return Err(io::Error::other("command outcome uncertain; inspect session before retrying")),
            _=>{},
        }
    }
    Err(io::Error::other("command acknowledgement timed out; inspect session before retrying"))
}
async fn post(client:&Client,base:&Url,path:&str,body:Value,lease:Option<&str>)->io::Result<Value>{
    let url=base.join(path).map_err(|_|invalid("invalid hub path"))?;
    let mut request=client.post(url).json(&body);
    if let Some(lease)=lease{request=request.header("x-doxa-host-lease",lease);}
    let response=request.send().await.map_err(|_|io::Error::other("hub request failed"))?;
    if !response.status().is_success(){return Err(io::Error::new(io::ErrorKind::PermissionDenied,"hub refused request"));}
    response.json().await.map_err(|_|io::Error::other("unreadable hub reply"))
}
async fn publish_batch(http:&Client,base:&Url,host:&str,lease:&str,session:&str,
    batch:&mut Vec<Value>,last:Option<u64>,cursors:&mut HashMap<String,u64>)->bool{
    if batch.is_empty(){return true;}
    let path=format!("api/host/{host}/events");
    let body=json!({"items":std::mem::take(batch)});
    if post(http,base,&path,body,Some(lease)).await.is_err(){return false;}
    if let Some(next)=last{cursors.insert(session.to_owned(),next);}
    true
}
async fn execute(app:&Arc<App>,owner:&str,session_id:&str,op:&str,payload:&Value)->Value{
    let kind=match op{"prompt"=>"send_prompt","answer"=>"approve_tool","transcript"=>"read_transcript",_=>return json!({"ok":false,"error":"unsupported remote command"})};
    if !doxa_peers::remote_policy::evaluate(kind,Some(owner),true,None).allowed{
        return json!({"ok":false,"error":"remote policy refused command"});
    }
    let entry=match app.session(session_id){Ok(Some(entry))=>entry,_=>return json!({"ok":false,"error":"session offline"})};
    let client=match connect(app,&entry,None,None).await{Ok(client)=>client,Err(_)=>return json!({"ok":false,"error":"session unavailable"})};
    let op=op.to_owned();let payload=payload.clone();let app=app.clone();
    match tokio::task::spawn_blocking(move||->io::Result<Value>{
        let mut client=client;
        match op.as_str(){
            "prompt"=>{
                let text=payload["text"].as_str().filter(|text|!text.trim().is_empty()&&text.len()<=58_000)
                    .ok_or_else(||invalid("invalid prompt"))?;
                let status=client.call("status",json!({}))?;
                let mode=status["status"]["permission_mode"].as_str().unwrap_or("");
                if remote_dangerous(mode)&&!bypass_opt_in(){return Err(io::Error::new(io::ErrorKind::PermissionDenied,"unrestricted remote prompt refused"));}
                client.remote_prompt(text,bypass_opt_in())
            },
            "answer"=>{
                let id=payload["id"].as_str().filter(|id|!id.is_empty()&&id.len()<=128).ok_or_else(||invalid("invalid answer ID"))?;
                if !payload["answer"].is_object(){return Err(invalid("invalid answer"));}
                let state=client.call("get_state",json!({}))?;
                if state["pending_inputs_complete"]!=true{return Err(invalid("pending review incomplete"));}
                let reviewed=state["pending_inputs"].as_array().and_then(|items|items.iter().find(|item|item["id"]==id))
                    .ok_or_else(||invalid("input request changed or expired"))?;
                client.call("answer_needs_input",json!({"id":id,"answer":payload["answer"],"reviewed_request":reviewed}))
            },
            "transcript"=>{
                let mut history=daemon::transcript(&client.hello)?;
                history["pending_inputs"]=client.hello["pending_inputs"].clone();
                scrub_data(&mut history,&app.lore)?;
                Ok(bounded_history(history))
            },
            _=>Err(invalid("unsupported remote command")),
        }
    }).await{
        Ok(Ok(value))=>value,Ok(Err(error))=>json!({"ok":false,"error":error.to_string()}),
        Err(_)=>json!({"ok":false,"error":"local command worker failed"}),
    }
}
async fn forward_events(app:&Arc<App>,http:&Client,base:&Url,host:&str,lease:&str,cursors:&mut HashMap<String,u64>){
    let entries=match app.sessions(){Ok(entries)=>entries,Err(_)=>return};
    let live=entries.iter().map(|entry|format!("{}~{}",entry.session_id,entry.started_at)).collect::<Vec<_>>();
    cursors.retain(|key,_|live.contains(key));
    for entry in entries{
        let cursor_key=format!("{}~{}",entry.session_id,entry.started_at);
        let cursor=cursors.get(&cursor_key).copied();
        let client=match connect(app,&entry,None,cursor).await{Ok(client)=>client,Err(_)=>continue};
        let frames=tokio::task::spawn_blocking(move||{
            let mut client=client;let _=client.short_timeout();let mut frames=Vec::new();
            for _ in 0..32{match client.next(){Ok(frame) if frame["type"]=="event"=>frames.push(frame),Ok(_)=>{},Err(_)=>break}}
            frames
        }).await.unwrap_or_default();
        let mut batch=Vec::new();let mut bytes=16usize;let mut last=None;
        for mut frame in frames{
            let Some(seq)=frame["seq"].as_u64() else{continue};
            if scrub_data(&mut frame["event"]["data"],&app.lore).is_err(){return;}
            let item=json!({"session_id":entry.session_id,"frame":frame});
            let length=serde_json::to_vec(&item).map(|bytes|bytes.len()).unwrap_or(128_001);
            if length>120_000{return;}
            if bytes+length>120_000 && !publish_batch(http,base,host,lease,&cursor_key,&mut batch,last,cursors).await{return;}
            if bytes+length>120_000 {bytes=16;}
            batch.push(item);bytes+=length+1;
            last=seq.checked_add(1);
        }
        if !publish_batch(http,base,host,lease,&cursor_key,&mut batch,last,cursors).await{return;}
    }
}
pub async fn run(app:Arc<App>,url:&str,host:&str)->io::Result<()> {
    let base=hub_url(url)?;
    if !super::valid_id(host){return Err(invalid("invalid host id"));}
    let http=http_client()?;
    let mut lease:Option<String>=None;let mut owner:Option<String>=None;let mut cursors=HashMap::new();
    let mut last_refresh=Instant::now()-Duration::from_secs(60);
    loop{
        if !doxa_peers::remote_policy::remote_enabled(){return Err(io::Error::new(io::ErrorKind::PermissionDenied,"remote access disabled"));}
        if last_refresh.elapsed()>=Duration::from_secs(15){
            let sessions=app.sessions()?.into_iter().map(|entry|json!({"id":entry.session_id,
                "title":entry.title,"engine":entry.engine,"model":entry.model,"incarnation":entry.started_at})).collect::<Vec<_>>();
            let registration=post(&http,&base,"api/host/register",json!({"host_id":host,"sessions":sessions}),lease.as_deref()).await;
            match registration{
                Ok(value)=>{
                    let claimed_owner=value["owner"].as_str().unwrap_or("");
                    if !doxa_peers::remote_policy::evaluate("send_prompt",Some(claimed_owner),true,None).allowed{
                        return Err(io::Error::new(io::ErrorKind::PermissionDenied,"hub owner is not on the local allow-list"));
                    }
                    lease=value["lease"].as_str().map(str::to_owned);
                    owner=Some(claimed_owner.to_owned());last_refresh=Instant::now();
                },
                Err(_)=>{tokio::time::sleep(Duration::from_secs(5)).await;continue;}
            }
        }
        let (Some(active_lease),Some(active_owner))=(lease.as_deref(),owner.as_deref()) else{tokio::time::sleep(Duration::from_secs(1)).await;continue};
        let path=format!("api/host/{host}/commands");
        let result=post(&http,&base,&path,json!({}),Some(active_lease)).await;
        if let Ok(batch)=result{
            for command in batch["commands"].as_array().into_iter().flatten(){
                let Some(id)=command["command_id"].as_str() else{continue};
                let session=command["session_id"].as_str().unwrap_or("");
                let op=command["op"].as_str().unwrap_or("");
                let result=execute(&app,active_owner,session,op,&command["payload"]).await;
                let path=format!("api/host/{host}/result");
                let _=post(&http,&base,&path,json!({"command_id":id,"result":result}),Some(active_lease)).await;
            }
        }
        forward_events(&app,&http,&base,host,active_lease,&mut cursors).await;
        tokio::time::sleep(Duration::from_millis(150)).await;
    }
}

#[cfg(test)]mod tests{
    use super::*;
    #[test]fn only_private_tailscale_https_origins(){
        assert!(hub_url("https://host.tail.ts.net/").is_ok());
        for url in ["http://host.tail.ts.net/","https://evil.example.com/","https://user@host.tail.ts.net/","https://host.tail.ts.net/other"]{assert!(hub_url(url).is_err(),"{url}");}
    }
    #[test]fn transcript_relay_keeps_latest_turns_within_command_bound(){
        let turns=(0..40).map(|i|json!({"prompt":format!("{i}{}","p".repeat(8_000)),
            "text":"x".repeat(20_000),"tools":[{"name":"read","result":"r".repeat(9_000)}]})).collect::<Vec<_>>();
        let history=bounded_history(json!({"turns":turns,"dropped_turns":2,"next_seq":90}));
        assert_eq!(history["ok"],true);
        assert_eq!(history["next_seq"],90);
        assert!(history["dropped_turns"].as_u64().unwrap()>2);
        assert!(serde_json::to_vec(&history).unwrap().len()<100_000);
        assert!(history["turns"].as_array().unwrap().last().unwrap()["prompt"].as_str().unwrap().starts_with("39"));
    }
}
