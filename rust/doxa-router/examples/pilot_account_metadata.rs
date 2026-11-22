//! Read account metadata without sending an inference request.

use doxa_vendors::{credentials, decisions::DecisionModel, Vendor};
use reqwest::Client;
use serde_json::{json, Value};
use std::{io, time::Duration};

async fn get(client: &Client, url: &str, key: &str) -> Option<Value> {
    let mut response = client.get(url).bearer_auth(key).send().await.ok()?;
    if response.status() != reqwest::StatusCode::OK {
        return None;
    }
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.ok()? {
        if body.len().checked_add(chunk.len())? > 64 * 1024 {
            return None;
        }
        body.extend_from_slice(&chunk);
    }
    serde_json::from_slice(&body).ok()
}

fn advertised(result: &Value, collection: &str, field: &str, model: &str) -> bool {
    result[collection]
        .as_array()
        .is_some_and(|rows| rows.iter().any(|row| row[field] == model))
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> io::Result<()> {
    let client = Client::builder()
        .timeout(Duration::from_secs(12))
        .redirect(reqwest::redirect::Policy::none())
        .no_proxy()
        .build()
        .map_err(io::Error::other)?;
    let deepseek = credentials::resolve(Vendor::DeepSeek).ok().flatten();
    let jev = DecisionModel::Jev.credential().ok();
    let (models, balance) = if let Some(key) = &deepseek {
        (
            get(&client, "https://api.deepseek.com/models", key).await,
            get(&client, "https://api.deepseek.com/user/balance", key).await,
        )
    } else {
        (None, None)
    };
    let jev_models = if let Some(key) = &jev {
        get(&client, "https://api.typesafe.ai/v1/models", key).await
    } else {
        None
    };
    println!("{}", json!({
        "deepseek_credential_available": deepseek.is_some(),
        "deepseek_catalog_available": models.as_ref().is_some_and(|value| value["data"].is_array()),
        "deepseek_flash_advertised": models.as_ref().is_some_and(|value| advertised(value, "data", "id", "deepseek-flash")),
        "deepseek_pro_advertised": models.as_ref().is_some_and(|value| advertised(value, "data", "id", "deepseek-v4-pro")),
        "deepseek_balance_available": balance.as_ref().and_then(|value| value["is_available"].as_bool()),
        "jev_credential_available": jev.is_some(),
        "jev_catalog_available": jev_models.as_ref().is_some_and(|value| value["models"].is_array()),
        "jev_113_listed": jev_models.as_ref().is_some_and(|value| advertised(value, "models", "name", "jev-1.13.0")),
        "provider_spending_caps_verified": false,
        "inference_requests": 0,
    }));
    Ok(())
}
