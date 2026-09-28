// SPDX-License-Identifier: AGPL-3.0-only
//! Opt-in launcher: resolve credentials with the production guard before
//! isolating HOME. Never serialize keys or accept a provider endpoint override.
use doxa_vendors::{credentials, Vendor};
use serde_json::json;
use std::{env, path::PathBuf, process::{Command, ExitCode}};

fn main() -> ExitCode {
    let args: Vec<_> = env::args().skip(1).collect();
    if args != ["--check"] && args != ["--live"] {
        eprintln!("usage: cargo run -p doxa-vendors --example verify_native_live -- --check|--live");
        return ExitCode::from(2);
    }
    let mut keys = Vec::new();
    let mut status = Vec::new();
    for vendor in [Vendor::DeepSeek, Vendor::Glm] {
        // Saved keys take precedence. An unsafe store fails closed.
        match (credentials::status(vendor), credentials::resolve(vendor)) {
            (Ok(source), Ok(key)) => {
                status.push(json!({"provider":vendor.engine_id(),
                    "credential_source":format!("{source:?}").to_lowercase(),
                    "credential_available":key.is_some()}));
                if let Some(key) = key { keys.push((vendor.env_var(), key)); }
            }
            _ => {
                status.push(json!({"provider":vendor.engine_id(),
                    "credential_source":"guard_rejected","credential_available":false}));
            }
        }
    }
    println!("{}", json!({"credential_check":status,"paid_requests":0}));
    if args == ["--check"] || keys.is_empty() { return ExitCode::SUCCESS; }
    let script = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../scripts/verify_native_vendors_live.py");
    let mut child = Command::new("python3");
    child.arg(script).arg("--live").env_clear();
    // All other inherited settings, integration endpoints and memory paths
    // are intentionally absent. Credentials travel only in child environment.
    for name in ["PATH", "TMPDIR", "DOXA_NATIVE_DAEMON", "DOXA_LORE_RS"] {
        if let Some(value) = env::var_os(name) { child.env(name, value); }
    }
    for (name, key) in keys { child.env(name, key); }
    match child.status() {
        Ok(result) if result.success() => ExitCode::SUCCESS,
        _ => ExitCode::FAILURE,
    }
}
