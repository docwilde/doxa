//! Isolated, grantless WebAssembly worker. The TUI has no activation path.
fn main() -> std::process::ExitCode {
    match doxa_tui::native_plugins::plugin_worker_stdio() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(_) => std::process::ExitCode::FAILURE,
    }
}
