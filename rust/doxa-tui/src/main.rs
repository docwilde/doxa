fn main() -> std::io::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.as_slice() {
        [flag, path] if flag == "--socket" => doxa_tui::bridge::run_socket(path),
        [flag] if flag == "--demo" => doxa_tui::ui::run(),
        [flag] if flag == "--version" => {
            println!("doxa-rs {}", env!("CARGO_PKG_VERSION"));
            Ok(())
        }
        [flag] if flag == "--help" => {
            println!("Usage: doxa-rs --socket PATH\n       doxa-rs --demo\n\nAttach to one existing DOXA daemon session. Ctrl+Q exits without stopping the daemon.");
            Ok(())
        }
        _ => Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "use --socket PATH to attach, or --help for usage",
        )),
    }
}
