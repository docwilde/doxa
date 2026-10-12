//! Outbound-only host connector for the private DOXA hub.
use super::{connect, remote_dangerous, bypass_opt_in, scrub_data, daemon, App};
use reqwest::{Client, Url};
use doxa_remote_wire as wire;
use serde_json::{json, Value};
use std::{collections::HashMap, io, sync::Arc, time::{Duration,Instant}};

fn invalid(message:&'static str)->io::Error{io::Error::new(io::ErrorKind::InvalidInput,message)}
fn shorten(value:&mut Value,limit:usize){
    if let Some(text)=value.as_str().filter(|text|text.chars().count()>limit){
        *value=json!(format!("{}…",text.chars().take(limit).collect::<String>()));
    }
}
pub(super) fn bounded_history(mut history:Value)->Value{
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
        && serde_json::to_vec(&history).is_ok_and(|bytes|bytes.len()>80_000){
        history["turns"].as_array_mut().unwrap().remove(0);dropped+=1;
    }
    if dropped > 0 {
        if let Some(before) = history["turns"][0]["_offset"].as_u64() {
            history["before"] = json!(before);
        }
    }
    if let Some(turns) = history["turns"].as_array_mut() {
        for turn in turns { turn.as_object_mut().map(|turn|turn.remove("_offset")); }
    }
    if let Some(before) = history["before"].as_u64() {
        history["has_more"] = json!(before > 0);
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
const CLIENT_USAGE:&str="usage: doxa remote list URL | send URL SESSION TEXT | review URL SESSION REQUEST_ID | answer URL SESSION REQUEST_ID allow|deny /ABS_PRIVATE_REVIEW.json";

fn review_receipt(target:&str,id:&str,snapshot:&Value)->io::Result<Value>{
    if snapshot["ok"]!=true || snapshot["pending_inputs_complete"]!=true {
        return Err(invalid("pending input review incomplete"));
    }
    let incarnation=snapshot["incarnation"].as_str().filter(|value|!value.is_empty()&&value.len()<=128)
        .ok_or_else(||invalid("session incarnation missing"))?;
    let request=snapshot["pending_inputs"].as_array().filter(|items|items.len()<=64)
        .and_then(|items|items.iter().find(|item|item["id"]==id&&item.is_object()))
        .ok_or_else(||invalid("input request changed or expired"))?;
    Ok(json!({"version":1,"target":target,"incarnation":incarnation,"reviewed_request":request}))
}
fn receipt_answer(receipt:&Value,target:&str,id:&str,decision:&str)->io::Result<Value>{
    if receipt.as_object().is_none_or(|object|object.len()!=4)
        || receipt["version"]!=1 || receipt["target"]!=target
        || !receipt["incarnation"].as_str().is_some_and(|value|!value.is_empty()&&value.len()<=128)
        || !receipt["reviewed_request"].is_object() || receipt["reviewed_request"]["id"]!=id {
        return Err(invalid("review receipt does not match target/request; run remote review and inspect it"));
    }
    Ok(json!({"id":id,"answer":{"decision":decision},"incarnation":receipt["incarnation"],
        "reviewed_request":receipt["reviewed_request"]}))
}
fn read_review(path:&str)->io::Result<Value>{
    use std::fs::{File,OpenOptions};
    use std::io::Read;
    use std::os::unix::fs::{MetadataExt,OpenOptionsExt};
    use std::os::unix::io::{AsRawFd,FromRawFd};
    use std::path::{Component,Path};
    let path=Path::new(path);
    if !path.is_absolute(){return Err(invalid("review receipt must be an absolute private file"));}
    let parts=path.components().collect::<Vec<_>>();
    if parts.len()<2 || parts.iter().skip(1).any(|part|!matches!(part,Component::Normal(_))) {
        return Err(invalid("invalid review receipt path"));
    }
    let mut directory=OpenOptions::new().read(true).custom_flags(libc::O_DIRECTORY|libc::O_NOFOLLOW|libc::O_CLOEXEC).open("/")?;
    for (index,part) in parts.iter().enumerate().skip(1){
        let Component::Normal(name)=part else{return Err(invalid("invalid review receipt path"));};
        use std::os::unix::ffi::OsStrExt;
        let name=std::ffi::CString::new(name.as_bytes()).map_err(|_|invalid("invalid review receipt path"))?;
        let leaf=index==parts.len()-1;
        let flags=libc::O_RDONLY|libc::O_NOFOLLOW|libc::O_CLOEXEC|if leaf{libc::O_NONBLOCK}else{libc::O_DIRECTORY};
        let fd=unsafe{libc::openat(directory.as_raw_fd(),name.as_ptr(),flags)};
        if fd<0{return Err(io::Error::last_os_error());}
        let mut file=unsafe{File::from_raw_fd(fd)};
        let meta=file.metadata()?;
        if leaf {
            if !meta.is_file() || meta.uid()!=unsafe{libc::geteuid()} || meta.mode()&0o077!=0 || meta.nlink()!=1 || meta.len()>96_000 {
                return Err(invalid("review receipt must be an owner-only regular file <=96KiB"));
            }
            let mut bytes=Vec::new();
            (&mut file).take(96_001).read_to_end(&mut bytes)?;
            let after=file.metadata()?;
            if bytes.len()>96_000 || after.len()!=meta.len() || after.mtime()!=meta.mtime() || after.mtime_nsec()!=meta.mtime_nsec(){
                return Err(invalid("review receipt changed during read"));
            }
            return serde_json::from_slice(&bytes).map_err(|_|invalid("invalid review receipt JSON"));
        }
        if !meta.is_dir() || meta.mode()&0o022!=0{return Err(invalid("unsafe review receipt directory"));}
        directory=file;
    }
    Err(invalid("invalid review receipt path"))
}
pub async fn client_action(args:&[String])->io::Result<()> {
    let [action,url,rest @ ..]=args else{return Err(invalid(CLIENT_USAGE))};
    let base=hub_url(url)?;let http=http_client()?;
    let key=wire::configured_key()?;
    if action=="list"&&rest.is_empty(){
        let value=bounded_reply(http.get(base.join("api/sessions").map_err(|_|invalid("invalid hub route"))?)
            .send().await.map_err(|_|io::Error::other("hub unavailable"))?).await?;
        for session in value["sessions"].as_array().into_iter().flatten(){
            println!("{}  {}",session["id"].as_str().unwrap_or("?"),session["title"].as_str().unwrap_or("session"));
        }
        return Ok(());
    }
    let (session,operation,body)=match (action.as_str(),rest){
        ("review",[session,id]) if valid_target(session)&&super::valid_id(id)=>{
            let snapshot=client_command(&http,&base,session,"transcript",json!({}),key.as_ref()).await?;
            println!("{}",serde_json::to_string_pretty(&review_receipt(session,id,&snapshot)?).map_err(|_|invalid("invalid review receipt"))?);
            return Ok(());
        },
        ("send",[session,text]) if valid_target(session)&&!text.trim().is_empty()&&text.len()<=58_000=>{
            let snapshot=client_command(&http,&base,session,"transcript",json!({}),key.as_ref()).await?;
            let incarnation=snapshot["incarnation"].as_str().filter(|value|!value.is_empty()&&value.len()<=128)
                .ok_or_else(||invalid("session incarnation missing"))?;
            (session.as_str(),"prompt",json!({"text":text,"incarnation":incarnation}))
        },
        ("answer",[session,id,decision,path]) if valid_target(session)&&super::valid_id(id)&&matches!(decision.as_str(),"allow"|"deny")=>
            (session.as_str(),"answer",receipt_answer(&read_review(path)?,session,id,decision)?),
        ("answer",[_,_,_])=>return Err(invalid("approval requires an inspected private review receipt; run remote review first")),
        _=>return Err(invalid(CLIENT_USAGE)),
    };
    let value=client_command(&http,&base,session,operation,body,key.as_ref()).await?;
    println!("{value}");Ok(())
}
const MAX_HUB_COMMAND_BATCH:usize=1_200_000; // Eight bounded 128,000-byte commands plus envelopes.
async fn bounded_reply(response:reqwest::Response)->io::Result<Value>{
    bounded_reply_limit(response,128_000,Duration::from_secs(5)).await
}
async fn bounded_reply_limit(mut response:reqwest::Response,limit:usize,deadline:Duration)->io::Result<Value>{
    tokio::time::timeout(deadline,async {
        if !response.status().is_success(){return Err(io::Error::new(io::ErrorKind::PermissionDenied,"hub refused request"));}
        if response.content_length().is_some_and(|size|size>limit as u64){return Err(invalid("hub reply exceeds bound"));}
        let mut bytes=Vec::new();
        while let Some(chunk)=response.chunk().await.map_err(|_|io::Error::other("unreadable hub reply"))?{
            if bytes.len().saturating_add(chunk.len())>limit{return Err(invalid("hub reply exceeds bound"));}
            bytes.extend_from_slice(&chunk);
        }
        serde_json::from_slice(&bytes).map_err(|_|invalid("invalid hub reply"))
    }).await.map_err(|_|io::Error::new(io::ErrorKind::TimedOut,"hub reply timed out"))?
}
async fn client_command(http:&Client,base:&Url,session:&str,operation:&str,mut body:Value,key:Option<&[u8;32]>)->io::Result<Value>{
    let path=format!("api/sessions/{session}/{operation}");
    let request_id=if key.is_some(){
        let id=uuid::Uuid::new_v4().to_string();
        body["request_id"]=json!(id);
        Some(id)
    }else{None};
    let body=if let Some(key)=key{
        wire::issue_command(&mut body)?;
        json!({"sealed":wire::seal(key,&format!("{session}|command|{operation}"),&body)?,
            "request_id":request_id})
    }else{body};
    let queued=post(http,base,&path,body,None).await?;
    let Some(command)=queued["command_id"].as_str().filter(|id|super::valid_id(id)) else{return Err(io::Error::other("hub did not return a command ID"))};
    for _ in 0..240{
        tokio::time::sleep(Duration::from_millis(250)).await;
        let path=format!("api/commands/{command}");
        let result=bounded_reply(http.get(base.join(&path).map_err(|_|invalid("invalid hub path"))?).send().await
            .map_err(|_|io::Error::other("hub status unavailable"))?).await?;
        match result["status"].as_str(){
            Some("accepted")=>{
                let value=if let Some(key)=key{
                    wire::open(key,&format!("{session}|result|{operation}|{}",request_id.as_deref().unwrap()),&result["result"]["sealed"])?
                }else{result["result"].clone()};
                if value["ok"]==false{return Err(io::Error::other(value["error"].as_str().unwrap_or("remote command refused")));}
                return Ok(value);
            },
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
    bounded_reply_limit(response,MAX_HUB_COMMAND_BATCH,Duration::from_secs(5)).await
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
fn decode_command(target:&str,op:&str,payload:&Value,key:Option<&[u8;32]>)->Result<Value,&'static str>{
    let android=payload["android_fence_v1"]==true;
    if android && (!matches!(op,"prompt"|"answer")
        || !payload["request_id"].as_str().is_some_and(super::valid_id)
        || !payload["hub_boot"].as_str().is_some_and(|boot|boot.len()==32 && boot.bytes().all(|b|b.is_ascii_hexdigit()&&!b.is_ascii_uppercase()))
        || !payload["incarnation"].as_str().is_some_and(|value|!value.is_empty()&&value.len()<=64)
        || !payload["request_id"].as_str().unwrap_or("").starts_with(&format!("{}-",payload["hub_boot"].as_str().unwrap_or("")))) {
        return Err("Android command scope invalid");
    }
    if key.is_some() && !payload["request_id"].as_str().is_some_and(super::valid_id){
        return Err("encrypted request ID required");
    }
    let payload=match key {
        Some(key)=>match wire::open(key,&format!("{target}|command|{op}"),&payload["sealed"]){
            Ok(value) if value["request_id"]==payload["request_id"] && wire::command_is_fresh(&value)
                && (!android || (value["hub_boot"]==payload["hub_boot"] && value["incarnation"]==payload["incarnation"]))=>value,
            Ok(_)=>return Err("encrypted command expired or request ID changed"),
            Err(_)=>return Err("encrypted command authentication failed"),
        },
        None if payload.get("sealed").is_some()=>return Err("encrypted command requires host key"),
        None=>payload.clone(),
    };
    if android && !wire::command_is_fresh(&payload) {
        return Err("Android command expired");
    }
    if android && op=="answer" && !payload["reviewed_request"].is_object() {
        return Err("Android reviewed input required");
    }
    Ok(payload)
}
pub(super) fn reviewed_input_is_current(payload:&Value,current:&Value)->bool {
    payload.get("reviewed_request")==Some(current)
}
fn strict_incarnation(entry:&doxa_peers::PeerRecord)->Option<&str>{
    entry.incarnation.as_deref().filter(|value|doxa_peers::valid_incarnation(value))
}
fn remote_incarnation(entry:&doxa_peers::PeerRecord)->&str{
    strict_incarnation(entry).unwrap_or(&entry.started_at)
}
fn android_incarnation_matches(entry:&doxa_peers::PeerRecord,payload:&Value)->bool{
    strict_incarnation(entry).is_some_and(|value|payload["incarnation"].as_str()==Some(value))
}
async fn execute(app:&Arc<App>,owner:&str,host:&str,session_id:&str,op:&str,payload:&Value,key:Option<&[u8;32]>)->Value{
    let target=format!("{host}~{session_id}");
    let android=payload["android_fence_v1"]==true;
    let payload=match decode_command(&target,op,payload,key){Ok(payload)=>payload,Err(error)=>return json!({"ok":false,"error":error})};
    let kind=match op{"prompt"=>"send_prompt","answer"=>"approve_tool","transcript"=>"read_transcript",_=>return json!({"ok":false,"error":"unsupported remote command"})};
    if !doxa_peers::remote_policy::evaluate(kind,Some(owner),true,None).allowed{
        return json!({"ok":false,"error":"remote policy refused command"});
    }
    let entry=match app.session(session_id){Ok(Some(entry))=>entry,_=>return json!({"ok":false,"error":"session offline"})};
    if android && !android_incarnation_matches(&entry,&payload) {
        return json!({"ok":false,"error":"Android session incarnation changed"});
    }
    if matches!(op,"prompt"|"answer") && payload["incarnation"].as_str()!=Some(remote_incarnation(&entry)) {
        return json!({"ok":false,"error":"session incarnation changed"});
    }
    let client=match connect(app,&entry,None,None).await{Ok(client)=>client,Err(_)=>return json!({"ok":false,"error":"session unavailable"})};
    let transcript_identity=(entry.session_id.clone(),entry.started_at.clone(),entry.incarnation.clone(),entry.pid,entry.daemon_socket.clone());
    let op=op.to_owned();let app=app.clone();
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
                if !reviewed_input_is_current(&payload,reviewed) {
                    return Err(invalid("reviewed input changed"));
                }
                client.call("answer_needs_input",json!({"id":id,"answer":payload["answer"],"reviewed_request":reviewed}))
            },
            "transcript"=>{
                let before=payload.get("before").map(|value|value.as_u64()
                    .ok_or_else(||invalid("invalid transcript page cursor"))).transpose()?;
                let mut history=daemon::transcript_page(&client.hello,before)?;
                history["pending_inputs"]=client.hello["pending_inputs"].clone();
                history["pending_inputs_complete"]=client.hello["pending_inputs_complete"].clone();
                scrub_data(&mut history,&app.lore)?;
                let current=app.session(&transcript_identity.0)?
                    .ok_or_else(||invalid("transcript session went offline"))?;
                if current.started_at!=transcript_identity.1 || current.incarnation!=transcript_identity.2
                    || current.pid!=transcript_identity.3 || current.daemon_socket!=transcript_identity.4 {
                    return Err(invalid("transcript session incarnation changed"));
                }
                history["incarnation"]=json!(remote_incarnation(&current));
                Ok(bounded_history(history))
            },
            _=>Err(invalid("unsupported remote command")),
        }
    }).await{
        Ok(Ok(value))=>value,Ok(Err(error))=>json!({"ok":false,"error":error.to_string()}),
        Err(_)=>json!({"ok":false,"error":"local command worker failed"}),
    }
}
async fn forward_events(app:&Arc<App>,http:&Client,base:&Url,host:&str,lease:&str,cursors:&mut HashMap<String,u64>,key:Option<&[u8;32]>){
    let entries=match app.sessions(){Ok(entries)=>entries,Err(_)=>return};
    let live=entries.iter().map(|entry|format!("{}~{}",entry.session_id,remote_incarnation(entry))).collect::<Vec<_>>();
    cursors.retain(|key,_|live.contains(key));
    for entry in entries{
        let cursor_key=format!("{}~{}",entry.session_id,remote_incarnation(&entry));
        let cursor=cursors.get(&cursor_key).copied();
        let client=match connect(app,&entry,None,cursor).await{Ok(client)=>client,Err(_)=>continue};
        if cursor.is_none(){
            if let Some(next)=client.hello["next_seq"].as_u64(){cursors.insert(cursor_key.clone(),next);}
        }
        let frames=tokio::task::spawn_blocking(move||{
            let mut client=client;let _=client.short_timeout();let mut frames=Vec::new();
            for _ in 0..32{match client.next(){Ok(frame) if frame["type"]=="event"=>frames.push(frame),Ok(_)=>{},Err(_)=>break}}
            frames
        }).await.unwrap_or_default();
        let mut batch=Vec::new();let mut bytes=16usize;let mut last=None;
        for mut frame in frames{
            let Some(seq)=frame["seq"].as_u64() else{continue};
            if scrub_data(&mut frame["event"]["data"],&app.lore).is_err(){return;}
            let incarnation=remote_incarnation(&entry).to_owned();
            frame["incarnation"]=json!(incarnation);
            if let Some(key)=key {
                if serde_json::to_vec(&frame["event"]["data"]).is_ok_and(|bytes|bytes.len()>38_000){
                    frame["event"]["type"]=json!("remote_event_omitted");
                    frame["event"]["data"]=json!({"reason":"event exceeds encrypted transport bound; reload transcript"});
                }
                let event_type=frame["event"]["type"].as_str().unwrap_or("").to_owned();
                let target=format!("{host}~{}",entry.session_id);
                let context=match wire::event_context(&target,&incarnation,seq,&event_type){Ok(context)=>context,Err(_)=>return};
                let data=frame["event"]["data"].clone();
                let sealed=match wire::seal(key,&context,&data){Ok(sealed)=>sealed,Err(_)=>return};
                frame["event"]["data"]=json!({"sealed":sealed});
            }
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
    let key=wire::configured_key()?;
    let mut lease:Option<String>=None;let mut owner:Option<String>=None;let mut cursors=HashMap::new();
    let mut seen_nonces:HashMap<String,Instant>=HashMap::new();
    let mut last_refresh=Instant::now()-Duration::from_secs(60);
    loop{
        if !doxa_peers::remote_policy::remote_enabled(){return Err(io::Error::new(io::ErrorKind::PermissionDenied,"remote access disabled"));}
        if last_refresh.elapsed()>=Duration::from_secs(15){
            let sessions=app.sessions()?.into_iter().map(|entry|json!({"id":entry.session_id,
                "title":if key.is_some(){"Encrypted session"}else{&entry.title},
                "engine":if key.is_some(){""}else{entry.engine.as_deref().unwrap_or("")},
                "model":if key.is_some(){""}else{entry.model.as_deref().unwrap_or("")},
                "incarnation":remote_incarnation(&entry),"encrypted":key.is_some()})).collect::<Vec<_>>();
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
                let replay=if key.is_some(){
                    seen_nonces.retain(|_,at|at.elapsed()<Duration::from_secs(180));
                    match command["payload"]["sealed"]["nonce"].as_str(){
                        Some(nonce) if seen_nonces.len()<8_192 && seen_nonces.insert(nonce.to_owned(),Instant::now()).is_none()=>false,
                        _=>true,
                    }
                }else{false};
                let result=if replay{json!({"ok":false,"error":"encrypted command replay refused"})}
                    else{execute(&app,active_owner,host,session,op,&command["payload"],key.as_ref()).await};
                let result=if let Some(key)=key.as_ref(){
                    let request=command["payload"]["request_id"].as_str().unwrap_or("missing");
                    match wire::seal(key,&format!("{host}~{session}|result|{op}|{request}"),&result){
                        Ok(sealed)=>json!({"ok":true,"sealed":sealed}),
                        Err(_)=>json!({"ok":false,"error":"encrypted result exceeds bound"}),
                    }
                }else{result};
                let path=format!("api/host/{host}/result");
                let _=post(&http,&base,&path,json!({"command_id":id,"result":result}),Some(active_lease)).await;
            }
        }
        forward_events(&app,&http,&base,host,active_lease,&mut cursors,key.as_ref()).await;
        tokio::time::sleep(Duration::from_millis(150)).await;
    }
}

#[cfg(test)]mod tests{
    #[test]
    fn all_clients_require_the_exact_reviewed_snapshot() {
        let current=json!({"id":"ask-1","kind":"permission","input_summary":"echo B"});
        assert!(!reviewed_input_is_current(&json!({"id":"ask-1"}),&current));
        assert!(!reviewed_input_is_current(&json!({"reviewed_request":{"id":"ask-1","kind":"permission","input_summary":"echo A"}}),&current));
        assert!(reviewed_input_is_current(&json!({"reviewed_request":current}),&current));
    }
    #[test]
    fn cli_receipt_binds_target_request_and_complete_review() {
        let state=json!({"ok":true,"incarnation":"first","pending_inputs_complete":true,
            "pending_inputs":[{"id":"ask-1","kind":"permission","input_summary":"echo reviewed"}]});
        let receipt=review_receipt("host~session","ask-1",&state).unwrap();
        let answer=receipt_answer(&receipt,"host~session","ask-1","allow").unwrap();
        assert_eq!(answer["incarnation"],"first");
        assert!(reviewed_input_is_current(&answer,&state["pending_inputs"][0]));
        assert!(receipt_answer(&receipt,"host~other","ask-1","allow").is_err());
        assert!(receipt_answer(&receipt,"host~session","ask-2","allow").is_err());
        let mut incomplete=state;incomplete["pending_inputs_complete"]=json!(false);
        assert!(review_receipt("host~session","ask-1",&incomplete).is_err());
    }
    #[test]
    fn cli_receipt_reader_refuses_unsafe_files_and_bounds_json() {
        use std::os::unix::fs::{PermissionsExt,symlink};
        let dir=tempfile::tempdir().unwrap();let path=dir.path().join("review.json");
        std::fs::set_permissions(dir.path(),std::fs::Permissions::from_mode(0o700)).unwrap();
        std::fs::write(&path,b"{\"version\":1}").unwrap();
        std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(read_review(path.to_str().unwrap()).unwrap()["version"],1);
        let link=dir.path().join("link.json");symlink(&path,&link).unwrap();assert!(read_review(link.to_str().unwrap()).is_err());
        let hard=dir.path().join("hard.json");std::fs::hard_link(&path,&hard).unwrap();assert!(read_review(path.to_str().unwrap()).is_err());std::fs::remove_file(hard).unwrap();
        std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o644)).unwrap();assert!(read_review(path.to_str().unwrap()).is_err());
        std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o600)).unwrap();std::fs::write(&path,vec![b' ';96_001]).unwrap();assert!(read_review(path.to_str().unwrap()).is_err());
    }
    #[tokio::test]
    async fn hub_response_bound_accepts_full_queue_and_rejects_hostile_bodies() {
        use tokio::io::{AsyncReadExt,AsyncWriteExt};
        async fn response(body:String,chunked:bool,slow:bool)->reqwest::Response {
            let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let addr=listener.local_addr().unwrap();
            tokio::spawn(async move {
                let (mut socket,_)=listener.accept().await.unwrap();let mut input=[0;4096];let _=socket.read(&mut input).await;
                let header=if chunked{"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n".into()}
                    else{format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n",body.len())};
                let _=socket.write_all(header.as_bytes()).await;
                if slow{tokio::time::sleep(Duration::from_millis(100)).await;}
                if chunked{let _=socket.write_all(format!("{:x}\r\n{}\r\n0\r\n\r\n",body.len(),body).as_bytes()).await;}
                else{let _=socket.write_all(body.as_bytes()).await;}
            });
            http_client().unwrap().get(format!("http://{addr}/")).send().await.unwrap()
        }
        let batch=json!({"commands":(0..8).map(|_|json!({"payload":{"text":"x".repeat(127_000)}})).collect::<Vec<_>>()});
        let body=batch.to_string();assert!(body.len()>1_000_000);
        assert_eq!(bounded_reply_limit(response(body,false,false).await,MAX_HUB_COMMAND_BATCH,Duration::from_secs(1)).await.unwrap(),batch);
        for chunked in [false,true]{
            let body=format!("{{\"data\":\"{}\"}}","x".repeat(MAX_HUB_COMMAND_BATCH));
            assert_eq!(bounded_reply_limit(response(body,chunked,false).await,MAX_HUB_COMMAND_BATCH,Duration::from_secs(1)).await.unwrap_err().kind(),io::ErrorKind::InvalidInput);
        }
        assert_eq!(bounded_reply_limit(response("{}".into(),true,true).await,MAX_HUB_COMMAND_BATCH,Duration::from_millis(10)).await.unwrap_err().kind(),io::ErrorKind::TimedOut);
    }
    use super::*;
    #[test]fn android_requires_random_registry_incarnation_while_legacy_remote_identity_remains_available(){
        let mut entry:doxa_peers::PeerRecord=serde_json::from_value(json!({
            "session_id":"session","pid":1,"socket_path":"/private/socket",
            "cwd":"/work","repo_root":null,"title":"session",
            "started_at":"2026-10-09T00:00:00.000000Z","heartbeat_at":"2026-10-09T00:00:00.000000Z"
        })).unwrap();
        let old=entry.started_at.clone();
        assert_eq!(remote_incarnation(&entry),old);
        assert!(!android_incarnation_matches(&entry,&json!({"incarnation":old})));
        let nonce=doxa_peers::new_incarnation();
        entry.incarnation=Some(nonce.clone());
        assert_eq!(remote_incarnation(&entry),nonce);
        assert!(android_incarnation_matches(&entry,&json!({"incarnation":nonce})));
        assert!(!android_incarnation_matches(&entry,&json!({"incarnation":old})));
        entry.incarnation=Some("invalid".into());
        assert_eq!(remote_incarnation(&entry),old);
        assert!(!android_incarnation_matches(&entry,&json!({"incarnation":"invalid"})));
    }
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
    #[test]fn transcript_relay_advances_page_cursor_when_dropping_turns(){
        let turns=(0..30).map(|i|json!({"prompt":"p".repeat(2_000),
            "text":"x".repeat(6_000),"tools":[],"_offset":i*100})).collect::<Vec<_>>();
        let history=bounded_history(json!({"turns":turns,"before":0,"has_more":false,"dropped_turns":0}));
        let first=history["turns"][0]["prompt"].as_str().unwrap();
        assert!(!first.is_empty());
        assert!(history["before"].as_u64().unwrap()>0);
        assert_eq!(history["has_more"],true);
        assert!(history["turns"].as_array().unwrap().iter().all(|turn|turn.get("_offset").is_none()));
    }
    #[test]fn android_plain_and_sealed_scope_fail_closed(){
        let boot="a".repeat(32);
        let id=format!("{boot}-11111111-1111-4111-8111-111111111111");
        let mut inner=json!({"text":"once","request_id":id,"hub_boot":boot,"incarnation":"v1"});
        wire::issue_command(&mut inner).unwrap();
        let mut plain=inner.clone();plain["android_fence_v1"]=json!(true);
        assert_eq!(decode_command("host~session","prompt",&plain,None).unwrap()["text"],"once");
        plain["hub_boot"]=json!("b".repeat(32));
        assert!(decode_command("host~session","prompt",&plain,None).is_err());
        plain=inner.clone();plain["android_fence_v1"]=json!(true);plain["issued_at"]=json!(1);
        assert!(decode_command("host~session","prompt",&plain,None).is_err());
        let key=[7u8;32];
        let sealed=wire::seal(&key,"host~session|command|prompt",&inner).unwrap();
        let mut outer=json!({"request_id":id,"hub_boot":boot,"incarnation":"v1","sealed":sealed,"android_fence_v1":true});
        assert_eq!(decode_command("host~session","prompt",&outer,Some(&key)).unwrap()["text"],"once");
        outer["incarnation"]=json!("v2");
        assert!(decode_command("host~session","prompt",&outer,Some(&key)).is_err());
        outer["incarnation"]=json!("v1");outer["request_id"]=json!(format!("{boot}-22222222-2222-4222-8222-222222222222"));
        assert!(decode_command("host~session","prompt",&outer,Some(&key)).is_err());
        outer["request_id"]=json!(id);outer["hub_boot"]=json!("b".repeat(32));
        assert!(decode_command("host~session","prompt",&outer,Some(&key)).is_err());
    }
    #[test]fn android_answer_requires_the_exact_reviewed_input(){
        let boot="a".repeat(32);
        let id=format!("{boot}-11111111-1111-4111-8111-111111111111");
        let current=json!({"id":"input-1","question":"Approve deletion?","options":["yes","no"]});
        let mut inner=json!({"id":"input-1","answer":{"choice":"no"},
            "request_id":id,"hub_boot":boot,"incarnation":"v1"});
        wire::issue_command(&mut inner).unwrap();
        let mut plain=inner.clone();plain["android_fence_v1"]=json!(true);
        assert!(decode_command("host~session","answer",&plain,None).is_err());
        inner["reviewed_request"]=current.clone();
        plain=inner.clone();plain["android_fence_v1"]=json!(true);
        let decoded=decode_command("host~session","answer",&plain,None).unwrap();
        assert!(reviewed_input_is_current(&decoded,&current));
        assert!(!reviewed_input_is_current(&decoded,
            &json!({"id":"input-1","question":"Approve deletion?","options":["yes","always"]})));
        let key=[7u8;32];
        let sealed=wire::seal(&key,"host~session|command|answer",&inner).unwrap();
        let outer=json!({"request_id":id,"hub_boot":boot,"incarnation":"v1",
            "sealed":sealed,"android_fence_v1":true});
        assert!(reviewed_input_is_current(
            &decode_command("host~session","answer",&outer,Some(&key)).unwrap(),&current));
    }
}
