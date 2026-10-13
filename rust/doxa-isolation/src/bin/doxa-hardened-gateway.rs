//! Native ROOT gateway. The worker never gets this binary's host capabilities.
use std::{io, path::PathBuf};
fn run()->io::Result<()> {
    let args:Vec<String>=std::env::args().skip(1).collect();
    if args.len()==2 && args[0]=="resolve" {
        use std::net::ToSocketAddrs;
        doxa_isolation::egress::AllowedHosts::new(&[args[1].clone()])?;
        let mut addresses:Vec<_>=format!("{}.:443",args[1]).to_socket_addrs()?.collect();
        addresses.sort();addresses.dedup();
        if addresses.is_empty()||addresses.len()>16{return Err(io::Error::other("bounded DNS address set required"));}
        println!("{}",serde_json::to_string(&addresses)?);return Ok(());
    }
    #[cfg(target_os="linux")]
    {
        if unsafe{libc::geteuid()}!=0{return Err(io::Error::other("native gateway requires ROOT"));}
        let mode=args.first().ok_or_else(||io::Error::other("missing native gateway operation"))?;
        let mut values=std::collections::BTreeMap::new();
        for pair in args[1..].chunks(2){
            if pair.len()!=2||!pair[0].starts_with("--")||values.insert(pair[0].as_str(),pair[1].as_str()).is_some(){return Err(io::Error::other("invalid native gateway arguments"));}
        }
        let policy=PathBuf::from(values.remove("--policy").ok_or_else(||io::Error::other("ROOT policy required"))?);
        if mode=="stage-check" {
            let phase=values.remove("--phase").ok_or_else(||io::Error::other("staging phase required"))?;
            let family=values.remove("--family").ok_or_else(||io::Error::other("staging family required"))?;
            if !values.is_empty(){return Err(io::Error::other("unknown staging argument"));}
            println!("{}",doxa_isolation::hardened_authority::stage_check(&policy,phase,family)?);return Ok(());
        }
        let manifest=PathBuf::from(values.remove("--manifest").ok_or_else(||io::Error::other("ROOT manifest required"))?);
        let engine_fd:i32=values.remove("--engine-pidfd").ok_or_else(||io::Error::other("inherited Engine pidfd required"))?.parse().map_err(|_|io::Error::other("invalid Engine pidfd"))?;
        if mode=="kernel-check" && values.is_empty(){
            let authority=doxa_isolation::hardened_authority::Authority::load(&policy,&manifest,engine_fd)?;
            authority.revalidate()?;println!("{}",authority.report());return Ok(());
        }
        if mode=="kernel-listener" {
            let frames:u32=values.remove("--frames").ok_or_else(||io::Error::other("diagnostic frame count required"))?.parse().map_err(|_|io::Error::other("invalid diagnostic frame count"))?;
            let send_fd:Option<i32>=values.remove("--send-fd").map(str::parse).transpose().map_err(|_|io::Error::other("invalid diagnostic descriptor"))?;
            if !values.is_empty(){return Err(io::Error::other("unknown diagnostic argument"));}
            println!("{}",doxa_isolation::root_gateway::kernel_listener(&policy,&manifest,engine_fd,frames,send_fd)?);return Ok(());
        }
        if mode=="serve" && values.is_empty(){return doxa_isolation::root_gateway::serve(&policy,&manifest,engine_fd);}
        if mode=="broker-check" {
            let phase=values.remove("--phase").ok_or_else(||io::Error::other("broker phase required"))?;
            let family=values.remove("--family").ok_or_else(||io::Error::other("provider family required"))?;
            if !values.is_empty(){return Err(io::Error::other("unknown native gateway argument"));}
            println!("{}",doxa_isolation::root_gateway::broker_check(&policy,&manifest,engine_fd,phase,family)?);return Ok(());
        }
    }
    Err(io::Error::other("unsupported native gateway operation or platform"))
}
fn main(){if let Err(error)=run(){eprintln!("doxa hardened gateway: {error}");std::process::exit(1);}}
