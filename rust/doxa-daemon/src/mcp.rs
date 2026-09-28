//! Native stdio adapter for the exec transport. Identity comes only from its
//! launching host. This transport exposes memory staging and read-only peers.
use crate::{agent_tools::AgentTools, peer_host::PeerHost, FixtureHost};
use doxa_runtime::Host;
use serde_json::{json,Value};
use std::{io::{self,BufRead,Write},path::PathBuf,sync::{Arc,mpsc}};
const FRAME_LIMIT:u64=64*1024;
fn spawn_in_parent(session:&str,cwd:&std::path::Path,args:&Value)->Result<Value,String> {
    doxa_engines::session_tools::validate(args)?;
    let runtime=std::env::var_os("DOXA_MCP_RUNTIME").map(PathBuf::from).ok_or("Parent runtime unavailable")?;
    let scope=doxa_peers::scope_for_cwd(cwd).map_err(|_|"Parent scope unavailable")?;
    let peers=doxa_peers::Registry::open(runtime).and_then(|registry|registry.scoped(&scope,None,&|text:&str|text.to_owned(),false)).map_err(|_|"Parent registry unavailable")?;
    let peer=peers.iter().find(|peer|peer.session_id==session&&peer.engine.as_deref()==Some("codex")).ok_or("Parent session unavailable")?;
    let socket=peer.daemon_socket.as_deref().ok_or("Parent socket unavailable")?;
    let mut stream=std::os::unix::net::UnixStream::connect(socket).map_err(|_|"Parent socket unavailable")?;
    stream.set_read_timeout(Some(std::time::Duration::from_secs(2))).map_err(|_|"Parent handshake unavailable")?;
    let mut reader=std::io::BufReader::new(stream.try_clone().map_err(|_|"Parent handshake unavailable")?);
    let mut frame=Vec::new();std::io::Read::take(&mut reader,FRAME_LIMIT+1).read_until(b'\n',&mut frame).map_err(|_|"Parent handshake unavailable")?;
    let hello:Value=serde_json::from_slice(&frame).map_err(|_|"Parent handshake unavailable")?;
    if frame.len() as u64>FRAME_LIMIT||hello["type"]!="hello"||hello["session_id"]!=session||hello["engine"]!="codex"{return Err("Parent identity changed".into());}
    stream.set_read_timeout(None).map_err(|_|"Parent answer unavailable")?;reader.get_ref().set_read_timeout(None).map_err(|_|"Parent answer unavailable")?;
    writeln!(stream,"{}",json!({"type":"call","id":1,"method":"spawn_session","params":args})).map_err(|_|"Parent request unavailable")?;
    stream.flush().map_err(|_|"Parent request unavailable")?;
    loop {frame.clear();let count=std::io::Read::take(&mut reader,FRAME_LIMIT+1).read_until(b'\n',&mut frame).map_err(|_|"Parent reply unavailable")?;
        if count==0||count as u64>FRAME_LIMIT{return Err("Parent reply unavailable".into());}let reply:Value=serde_json::from_slice(&frame).map_err(|_|"Parent reply unavailable")?;
        if reply["type"]=="reply"&&reply["id"]==1 {return if reply["ok"]==true{Ok(reply)}else{Err("Parent spawn was refused or failed".into())};}
    }
}
pub fn serve()->io::Result<()> {
    let id=std::env::var("DOXA_MCP_SESSION_ID").map_err(|_|io::Error::other("missing host session"))?;
    if !doxa_state::valid_session_id(&id){return Err(io::Error::other("invalid host session"));}
    let cwd=PathBuf::from(std::env::var_os("DOXA_MCP_CWD").ok_or_else(||io::Error::other("missing host cwd"))?);
    if !cwd.is_absolute(){return Err(io::Error::other("host cwd must be absolute"));}
    let enabled=std::env::var("DOXA_MCP_LORE").as_deref()!=Ok("0");
    let tools=AgentTools::new(&cwd.to_string_lossy(),&id,"codex",enabled);
    if enabled && tools.is_none(){return Err(io::Error::other("canonical memory operators unavailable"));}
    let(events,_receiver)=mpsc::sync_channel(256);
    let peers=PeerHost::new(Arc::new(FixtureHost),doxa_peers::runtime_dir(),&cwd,id.clone(),String::new(),events)?;
    let input=io::stdin();let mut input=input.lock();let output=io::stdout();let mut output=output.lock();
    loop {
        let mut frame=Vec::new();let count=std::io::Read::take(&mut input,FRAME_LIMIT+1).read_until(b'\n',&mut frame)?;
        if count==0{break;}
        if count as u64>FRAME_LIMIT{return Err(io::Error::other("MCP request exceeds limit"));}
        let request:Value=match serde_json::from_slice(&frame){Ok(value)=>value,Err(_)=>{writeln!(output,"{}",json!({"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Invalid JSON"}}))?;output.flush()?;continue;}};
        if request["jsonrpc"]!="2.0" || !request.is_object() {
            writeln!(output,"{}",json!({"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Invalid request"}}))?;output.flush()?;continue;
        }
        let Some(request_id)=request.get("id").filter(|id|id.is_string()||id.is_i64()||id.is_u64()) else {continue;};
        let result=match request["method"].as_str().unwrap_or("") {
            "initialize"=>Ok(json!({"protocolVersion":match request["params"]["protocolVersion"].as_str(){Some("2025-03-26")=>"2025-03-26",Some("2025-06-18")=>"2025-06-18",Some("2025-11-25")=>"2025-11-25",_=>"2024-11-05"},"capabilities":{"tools":{}},"serverInfo":{"name":"doxa","version":env!("CARGO_PKG_VERSION")}})),
            "ping"=>Ok(json!({})),
            "tools/list"=>{
                let mut definitions=tools.as_ref().map(|tools|tools.definitions()).unwrap_or_default();
                if crate::session_spawn::enabled()&&std::env::var_os("DOXA_MCP_RUNTIME").is_some(){definitions.extend(doxa_engines::session_tools::definitions());}
                definitions.extend(doxa_engines::peer_tools::definitions().into_iter().filter(|row|row["name"]!=doxa_engines::peer_tools::SEND));
                Ok(json!({"tools":definitions.into_iter().map(|row|json!({"name":row["name"].as_str().unwrap_or("").strip_prefix("mcp__doxa__").unwrap_or(""),"description":row["description"],"inputSchema":row["inputSchema"]})).collect::<Vec<_>>() }))
            },
            "tools/call"=>{
                let name=format!("mcp__doxa__{}",request["params"]["name"].as_str().unwrap_or(""));
                let args=request["params"].get("arguments").cloned().unwrap_or_else(||json!({}));
                let result=if name==doxa_engines::session_tools::SPAWN {spawn_in_parent(&id,&cwd,&args)} else if matches!(name.as_str(),doxa_engines::peer_tools::LIST|doxa_engines::peer_tools::HISTORY) {
                    doxa_engines::peer_tools::rpc(&name,&args).map_err(str::to_owned).and_then(|method|peers.call(method,&args))
                } else {tools.as_ref().ok_or_else(||"Unavailable tool".to_owned()).and_then(|tools|tools.call(&name,&args))};
                let failed=result.is_err();let text=result.map(|value|value.to_string()).unwrap_or_else(|_|"Canonical tool refused or failed".to_owned());
                Ok(json!({"isError":failed,"content":[{"type":"text","text":text}]}))
            },
            _=>Err(json!({"code":-32601,"message":"Method not found"})),
        };
        let response=match result {Ok(result)=>json!({"jsonrpc":"2.0","id":request_id,"result":result}),Err(error)=>json!({"jsonrpc":"2.0","id":request_id,"error":error})};
        writeln!(output,"{response}")?;output.flush()?;
    }
    Ok(())
}
