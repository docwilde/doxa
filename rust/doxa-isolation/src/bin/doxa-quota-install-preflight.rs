use std::{io, process};

fn run() -> io::Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.len() != 1 { return Err(io::Error::other("usage: doxa-quota-install-preflight SESSION_ID")); }
    let report = doxa_isolation::quota_install::preflight_installed_quota_helper(&args[0])?;
    println!("{}", serde_json::to_string(&report)?);
    Ok(())
}

fn main() {
    if let Err(cause) = run() {
        eprintln!("quota installation preflight: {cause}");
        process::exit(2);
    }
}
