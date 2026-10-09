use std::{io, process};

fn run() -> io::Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.len() != 3 || args[1] != "--reviewed-helper-sha256" {
        return Err(io::Error::other("usage: doxa-quota-install-preflight SESSION_ID --reviewed-helper-sha256 DIGEST"));
    }
    let report = doxa_isolation::quota_install::preflight_installed_quota_helper(&args[0], &args[2])?;
    println!("{}", serde_json::to_string(&report)?);
    Ok(())
}

fn main() {
    if let Err(cause) = run() {
        eprintln!("quota installation preflight: {cause}");
        process::exit(2);
    }
}
