//! Regenerate browser-extension/tests/rust-envelope.json after wire changes.
use base64::{engine::general_purpose::STANDARD_NO_PAD, Engine};
use serde_json::json;

fn main() {
    let key = [7u8; 32];
    let context = "host~session|result|transcript|request-1";
    let value = json!({"ok":true,"turns":[{"text":"Rust compressed reply. ".repeat(200)}]});
    let envelope = doxa_remote_wire::seal(&key, context, &value).expect("test fixture seal");
    println!("{}", json!({"key":STANDARD_NO_PAD.encode(key),"context":context,
        "value":value,"envelope":envelope}));
}
