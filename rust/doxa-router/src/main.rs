use doxa_router::{Config,evaluation};
use std::{io,path::Path,sync::atomic::AtomicBool};

fn run(args: &[String]) -> io::Result<()> {
    if args.iter().any(|arg|matches!(arg.as_str(),"-h"|"--help")) {
        println!("doxa-router-eval offline PRIVATE_CONFIG PRIVATE_RECORDED_JSONL\ndoxa-router-eval fixture PRIVATE_CONFIG PRIVATE_SYNTHETIC_CASES_JSONL\ndoxa-router-eval live PRIVATE_CONFIG PRIVATE_SYNTHETIC_CASES_JSONL --live-synthetic\n\nNo worker execution. Live: <=20 synthetic Jev calls and <=$0.01 reservations.");return Ok(());
    }
    let live=matches!(args,[command,_,_,flag] if command=="live" && flag=="--live-synthetic");
    if args.len()!=if live{4}else{3} || !matches!(args.first().map(String::as_str),Some("offline"|"fixture")) && !live {
        return Err(io::Error::new(io::ErrorKind::InvalidInput,"use offline|fixture CONFIG DATA or live CONFIG DATA --live-synthetic"));
    }
    let config=Config::load(Path::new(&args[1]))?;
    let (text,_)=evaluation::load_cases(Path::new(&args[2]))?;
    if args[0]=="fixture" {println!("{}",evaluation::fixture_recordings(&config,&text)?);return Ok(());}
    let key=if live{doxa_router::credential().ok()}else{None};
    let report=evaluation::evaluate(&config,&text,live,key.as_deref(),&AtomicBool::new(false))?;
    let failed=live && report.live_status!="completed";
    println!("{}",serde_json::to_string_pretty(&report)?);
    if failed {return Err(io::Error::other("synthetic live router smoke incomplete; inspect aggregate report"));}
    Ok(())
}
fn main()->std::process::ExitCode {
    let args=std::env::args().skip(1).collect::<Vec<_>>();
    match run(&args) {Ok(())=>std::process::ExitCode::SUCCESS,Err(error)=>{eprintln!("router evaluation: {error}");std::process::ExitCode::FAILURE}}
}
