use std::{io, path::Path};

fn run() -> io::Result<()> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() != 1 { return Err(io::Error::other("usage: doxa-quota-helper /absolute/root-owned/policy.json")); }
    doxa_isolation::quota_helper::serve_systemd_socket(Path::new(&args[0]))
}

fn main() {
    if let Err(cause) = run() {
        eprintln!("doxa quota helper: {cause}");
        std::process::exit(2);
    }
}
