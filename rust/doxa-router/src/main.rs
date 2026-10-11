use doxa_router::{Config,evaluation};
use std::{fs,io::{self,Write},os::unix::fs::{MetadataExt,OpenOptionsExt,PermissionsExt},path::Path,sync::atomic::{AtomicBool,Ordering}};
static CANCEL: AtomicBool=AtomicBool::new(false);
extern "C" fn cancelled(_:libc::c_int) {CANCEL.store(true,Ordering::Release);}
fn write_event(file: &mut fs::File,event: serde_json::Value)->io::Result<()> {
    serde_json::to_writer(&mut *file,&event)?;file.write_all(b"\n")?;file.sync_all()
}
fn journal(path: &Path)->io::Result<fs::File> {
    if !path.is_absolute() {return Err(io::Error::other("live journal path must be absolute"));}
    let parent=path.parent().ok_or_else(||io::Error::other("live journal requires an owner directory"))?;
    let meta=fs::metadata(parent)?;
    if !meta.is_dir() || meta.uid()!=unsafe{libc::geteuid()} || meta.permissions().mode() & 0o022 !=0 {
        return Err(io::Error::other("live journal requires an owner directory without shared writes"));
    }
    fs::OpenOptions::new().write(true).create_new(true).mode(0o600)
        .custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(path)
}
fn run(args: &[String]) -> io::Result<()> {
    if args.iter().any(|arg|matches!(arg.as_str(),"-h"|"--help")) {
        println!("doxa-router-eval offline PRIVATE_CONFIG PRIVATE_RECORDED_JSONL\ndoxa-router-eval fixture PRIVATE_CONFIG PRIVATE_SYNTHETIC_CASES_JSONL\ndoxa-router-eval live PRIVATE_CONFIG PRIVATE_SYNTHETIC_CASES_JSONL --live-synthetic --journal NEW_PRIVATE_JSONL\n\nNo worker execution. Live: <=20 synthetic Jev calls and <=$0.01 cumulative reservations.");return Ok(());
    }
    let live=matches!(args,[command,_,_,flag,journal_flag,_] if command=="live" && flag=="--live-synthetic" && journal_flag=="--journal");
    if args.len()!=(if live{6}else{3}) || (!matches!(args.first().map(String::as_str),Some("offline"|"fixture")) && !live) {
        return Err(io::Error::new(io::ErrorKind::InvalidInput,"use offline|fixture CONFIG DATA or live CONFIG DATA --live-synthetic --journal NEW_PRIVATE_JSONL"));
    }
    let config=Config::load(Path::new(&args[1]))?;
    let (text,_)=evaluation::load_cases(Path::new(&args[2]))?;
    if args[0]=="fixture" {println!("{}",evaluation::fixture_recordings(&config,&text)?);return Ok(());}
    let key=if live{doxa_router::credential().ok()}else{None};
    let mut log=if live {
        let mut file=journal(Path::new(&args[5]))?;
        write_event(&mut file,serde_json::json!({"event":"evaluation_start","model":doxa_router::JEV_MODEL,
            "config_sha256":config.hash()?,"synthetic_only":true,"call_ceiling":20,"spend_ceiling_usd_micros":10000}))?;
        unsafe {libc::signal(libc::SIGINT,cancelled as *const () as libc::sighandler_t);libc::signal(libc::SIGTERM,cancelled as *const () as libc::sighandler_t);}
        Some(file)
    }else{None};
    let report=evaluation::evaluate_observed(&config,&text,live,key.as_deref(),&CANCEL,|ledger,reservation,outcome| {
        if let Some(file)=log.as_mut() {write_event(file,serde_json::json!({"event":if outcome.is_some(){"outcome"}else{"reserved"},"ledger":ledger,"reservation":reservation,"outcome":outcome}))?;}
        Ok(())
    })?;
    let failed=live && report.live_status!="completed";
    if let Some(file)=log.as_mut() {write_event(file,serde_json::json!({"event":"evaluation_complete","status":report.live_status,"ledger":report.ledger}))?;}
    println!("{}",serde_json::to_string_pretty(&report)?);
    if failed {return Err(io::Error::other("synthetic live router smoke incomplete; inspect aggregate report"));}
    Ok(())
}
fn main()->std::process::ExitCode {
    let args=std::env::args().skip(1).collect::<Vec<_>>();
    match run(&args) {Ok(())=>std::process::ExitCode::SUCCESS,Err(error)=>{eprintln!("router evaluation: {error}");std::process::ExitCode::FAILURE}}
}
