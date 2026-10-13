use std::{io, path::Path};

fn run() -> io::Result<()> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() == 2 && args[0] == "--enforced" {
        let id = args[1]
            .to_str()
            .ok_or_else(|| io::Error::other("invalid quota session identity"))?;
        return doxa_isolation::quota_helper::serve_enforced_systemd_socket(id);
    }
    if args.len() != 1 {
        return Err(io::Error::other(
            "usage: doxa-quota-helper /absolute/root-owned/policy.json",
        ));
    }
    doxa_isolation::quota_helper::serve_systemd_socket(Path::new(&args[0]))
}

fn main() {
    if let Err(cause) = run() {
        eprintln!("doxa quota helper: {cause}");
        std::process::exit(2);
    }
}
