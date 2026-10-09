//! Optional, owner-scoped Android FCM data push. No session content leaves the hub.
use reqwest::Client;
use serde_json::{json, Value};
use std::{fs::OpenOptions, io::{self, Read}, os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt}, path::Path,
    sync::Arc, time::{Duration, Instant}};
use tokio::sync::{Mutex, OwnedSemaphorePermit, Semaphore};
use web_push_native::jwt_simple::{algorithms::{RS256KeyPair, RSAKeyPairLike}, claims::Claims, prelude::Duration as JwtDuration};
use crate::push::Kind;

const TOKEN_URL: &str = "https://oauth2.googleapis.com/token";
const SCOPE: &str = "https://www.googleapis.com/auth/firebase.messaging";
fn invalid(message: &'static str) -> io::Error { io::Error::new(io::ErrorKind::InvalidInput, message) }
async fn bounded_json(mut response: reqwest::Response) -> Option<Value> {
    let mut raw = Vec::new();
    while let Some(chunk) = response.chunk().await.ok()? {
        if raw.len() + chunk.len() > 8192 { return None; }
        raw.extend_from_slice(&chunk);
    }
    serde_json::from_slice(&raw).ok()
}

pub fn valid_token(token: &str) -> bool {
    (20..=4096).contains(&token.len()) && token.bytes().all(|c|
        c.is_ascii_alphanumeric() || matches!(c, b':' | b'-' | b'_' | b'.'))
}

pub fn valid_tag(tag: &str) -> bool { tag.len() == 32 && tag.bytes().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()) }

pub struct Runtime {
    project: String,
    email: String,
    key: RS256KeyPair,
    http: Client,
    access: Mutex<Option<(String, Instant)>>,
    slots: Arc<Semaphore>,
}
impl Runtime {
    pub fn load(runtime: &Path) -> io::Result<Option<Self>> {
        let path = runtime.join("fcm-service-account.json");
        let mut file = match OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC).open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        let meta = file.metadata()?;
        if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() } || meta.nlink() != 1
            || meta.permissions().mode() & 0o077 != 0 || meta.len() > 16_384 {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "FCM key file must be private and owned"));
        }
        let mut raw = String::new();
        Read::by_ref(&mut file).take(16_385).read_to_string(&mut raw)?;
        if raw.len() > 16_384 { return Err(invalid("FCM key file too large")); }
        let value: Value = serde_json::from_str(&raw).map_err(|_| invalid("invalid FCM key JSON"))?;
        if value["type"] != "service_account" || value["token_uri"] != TOKEN_URL {
            return Err(invalid("FCM service account or token URI invalid"));
        }
        let project = value["project_id"].as_str().filter(|s| !s.is_empty() && s.len() <= 128
            && s.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')).ok_or_else(|| invalid("invalid FCM project"))?;
        let email = value["client_email"].as_str().filter(|s| s.len() <= 320 && s.ends_with(".gserviceaccount.com")
            && !s.contains(char::is_whitespace)).ok_or_else(|| invalid("invalid FCM service account email"))?;
        let pem = value["private_key"].as_str().ok_or_else(|| invalid("FCM private key missing"))?;
        let key = RS256KeyPair::from_pem(pem).map_err(|_| invalid("invalid FCM private key"))?;
        let http = Client::builder().timeout(Duration::from_secs(10)).redirect(reqwest::redirect::Policy::none())
            .build().map_err(io::Error::other)?;
        Ok(Some(Self { project: project.into(), email: email.into(), key, http,
            access: Mutex::new(None), slots: Arc::new(Semaphore::new(8)) }))
    }
    pub fn permit(&self) -> Option<OwnedSemaphorePermit> { self.slots.clone().try_acquire_owned().ok() }
    async fn access_token(&self) -> Option<String> {
        let mut cached = self.access.lock().await;
        if let Some((token, expiry)) = cached.as_ref() {
            if *expiry > Instant::now() { return Some(token.clone()); }
        }
        let claims = Claims::with_custom_claims(json!({"scope":SCOPE}), JwtDuration::from_secs(3600))
            .with_issuer(&self.email).with_audience(TOKEN_URL);
        let assertion = self.key.sign(claims).ok()?;
        let response = self.http.post(TOKEN_URL).form(&[
            ("grant_type", "urn:ietf:params:oauth:grant-type:jwt-bearer"),
            ("assertion", assertion.as_str()),
        ]).send().await.ok()?;
        if !response.status().is_success() { return None; }
        let body = bounded_json(response).await?;
        let token = body["access_token"].as_str().filter(|s| !s.is_empty() && s.len() <= 4096)?.to_owned();
        let seconds = body["expires_in"].as_u64().filter(|s| *s >= 120 && *s <= 3600)?;
        *cached = Some((token.clone(), Instant::now() + Duration::from_secs(seconds - 60)));
        Some(token)
    }
    pub async fn send(&self, token: &str, tag: &str, kind: Kind, _permit: OwnedSemaphorePermit) -> bool {
        let Some(access) = self.access_token().await else { return false; };
        let body = message(token, tag, kind);
        let url = format!("https://fcm.googleapis.com/v1/projects/{}/messages:send", self.project);
        let Ok(response) = self.http.post(url).bearer_auth(access).json(&body).send().await else { return false; };
        if response.status().is_success() { return false; }
        // Only a token-specific UNREGISTERED response retires a registration.
        if response.status().as_u16() != 404 { return false; }
        bounded_json(response).await.is_some_and(|body|
            body["error"]["details"].as_array().is_some_and(|details| details.iter().any(|detail|
                detail["errorCode"] == "UNREGISTERED")))
    }
}
fn message(token: &str, tag: &str, kind: Kind) -> Value {
    json!({"message":{"token":token,"data":{"kind":kind.as_str(),"tag":tag},
        "android":{"priority":if kind == Kind::NeedsInput {"HIGH"} else {"NORMAL"},"ttl":"300s"}}})
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn token_and_payload_are_bounded_and_generic() {
        assert!(!valid_token("short"));
        assert!(!valid_token("a".repeat(4097).as_str()));
        assert!(!valid_token("a:bad/url with space"));
        assert!(valid_token("a:abcdefghijklmnopqrstuvwxyz-_."));
        assert!(valid_tag("abcdef0123456789abcdef0123456789"));
        assert!(!valid_tag("ABCDEF0123456789abcdef0123456789"));
        let value = message("opaque-registration-token", "abcdef0123456789abcdef0123456789", Kind::NeedsInput);
        assert_eq!(value["message"]["data"], json!({"kind":"needs_input","tag":"abcdef0123456789abcdef0123456789"}));
        assert_eq!(value["message"]["android"]["priority"], "HIGH");
        assert!(value.to_string().find("session").is_none());
    }
    #[test]
    fn runtime_requires_private_owned_regular_key_file() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        assert!(Runtime::load(dir.path()).unwrap().is_none());
        let path = dir.path().join("fcm-service-account.json");
        std::fs::write(&path, "{}").unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert_eq!(Runtime::load(dir.path()).err().unwrap().kind(), io::ErrorKind::PermissionDenied);
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert!(Runtime::load(dir.path()).is_err());
        std::fs::remove_file(&path).unwrap();
        std::os::unix::fs::symlink("outside", &path).unwrap();
        assert!(Runtime::load(dir.path()).is_err());
    }
}
