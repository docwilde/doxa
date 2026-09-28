//! Native stdio adapter for the exec transport. Identity comes only from its
//! launching host. This transport exposes memory staging and read-only peers.
use crate::{agent_tools::AgentTools, peer_host::PeerHost, FixtureHost};
use doxa_runtime::Host;
use serde_json::{json,Value};
use std::{io::{self,BufRead,Write},path::PathBuf,sync::{Arc,mpsc}};
const FRAME_LIMIT:u64=64*1024;
pub fn serve()->io::Result<()> {
    let id=std::env::var("DOXA_MCP_SESSION_ID").map_err(|_|io::Error::other("missing host session"))?;
    if !doxa_state::valid_session_id(&id){return Err(io::Error::other("invalid host session"));}
    let cwd=PathBuf::from(std::env::var_os("DOXA_MCP_CWD").ok_or_else(||io::Error::other("missing host cwd"))?);
    if !cwd.is_absolute(){return Err(io::Error::other("host cwd must be absolute"));}
    let enabled=std::env::var("DOXA_MCP_LORE").as_deref()!=Ok("0");
    let tools=AgentTools::new(&cwd.to_string_lossy(),&id,"codex",enabled);
    if enabled && tools.is_none(){return Err(io::Error::other("canonical memory operators unavailable"));}
    let(events,_receiver)=mpsc::sync_channel(256);
    let peers=PeerHost::new(Arc::new(FixtureHost),doxa_peers::runtime_dir(),&cwd,id,String::new(),events)?;
    let input=io::stdin();let mut input=input.lock();let output=io::stdout();let mut output=output.lock();
    loop {
        let mut frame=Vec::new();let count=std::io::Read::take(&mut input,FRAME_LIMIT+1).read_until(b'\n',&mut frame)?;
        if count==0{break;}
        if count as u64>FRAME_LIMIT{return Err(io::Error::other("MCP request exceeds limit"));}
        let request:Value=match serde_json::from_slice(&frame){Ok(value)=>value,Err(_)=>{writeln!(output,"{}",json!({"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Invalid JSON"}}))?;output.flush()?;continue;}};
        let Some(id)=request.get("id").filter(|id|id.is_string()||id.is_i64()||id.is_u64()) else {continue;};
        let result=match request["method"].as_str().unwrap_or("") {
            "initialize"=>Ok(json!({"protocolVersion":match request["params"]["protocolVersion"].as_str(){Some("2025-03-26")=>"2025-03-26",Some("2025-06-18")=>"2025-06-18",Some("2025-11-25")=>"2025-11-25",_=>"2024-11-05"},"capabilities":{"tools":{}},"serverInfo":{"name":"doxa","version":env!("CARGO_PKG_VERSION")}})),
            "ping"=>Ok(json!({})),
            "tools/list"=>{
                let mut definitions=tools.as_ref().map(|tools|tools.definitions()).unwrap_or_default();
                definitions.extend(doxa_engines::peer_tools::definitions().into_iter().filter(|row|row["name"]!=doxa_engines::peer_tools::SEND));
                Ok(json!({"tools":definitions.into_iter().map(|row|json!({"name":row["name"].as_str().unwrap_or("").strip_prefix("mcp__doxa__").unwrap_or(""),"description":row["description"],"inputSchema":row["inputSchema"]})).collect::<Vec<_>>() }))
            },
            "tools/call"=>{
                let name=format!("mcp__doxa__{}",request["params"]["name"].as_str().unwrap_or(""));
                let args=request["params"].get("arguments").cloned().unwrap_or_else(||json!({}));
                let result=if matches!(name.as_str(),doxa_engines::peer_tools::LIST|doxa_engines::peer_tools::HISTORY) {
                    doxa_engines::peer_tools::rpc(&name,&args).map_err(str::to_owned).and_then(|method|peers.call(method,&args))
                } else {tools.as_ref().ok_or_else(||"Unavailable tool".to_owned()).and_then(|tools|tools.call(&name,&args))};
                let failed=result.is_err();let text=result.map(|value|value.to_string()).unwrap_or_else(|_|"Canonical tool refused or failed".to_owned());
                Ok(json!({"isError":failed,"content":[{"type":"text","text":text}]}))
            },
            _=>Err(json!({"code":-32601,"message":"Method not found"})),
        };
        let response=match result {Ok(result)=>json!({"jsonrpc":"2.0","id":id,"result":result}),Err(error)=>json!({"jsonrpc":"2.0","id":id,"error":error})};
        writeln!(output,"{response}")?;output.flush()?;
    }
    Ok(())
}
