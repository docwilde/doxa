//! Owner-scoped Web Push. Notifications contain only a generic event kind;
//! the browser fetches session state after the owner opens the private hub.
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use reqwest::{Client, Url};
use serde_json::Value;
use std::{fs::OpenOptions, io::{self, Read, Write}, os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::Path, sync::Arc, time::Duration};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};
use web_push_native::{Auth, WebPushBuilder, p256::PublicKey,
    jwt_simple::algorithms::{ECDSAP256PublicKeyLike, ES256KeyPair}};

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum Kind { NeedsInput, TurnDone }
impl Kind {
    pub fn from_event(value: &str) -> Option<Self> {
        match value { "needs_input" => Some(Self::NeedsInput),
            "turn_done" | "turn_refused" => Some(Self::TurnDone), _ => None }
    }
    pub fn as_str(self) -> &'static str { match self { Self::NeedsInput => "needs_input", Self::TurnDone => "turn_done" } }
    fn payload(self) -> &'static [u8] {
        match self {
            Self::NeedsInput => br#"{"kind":"needs_input"}"#,
            Self::TurnDone => br#"{"kind":"turn_done"}"#,
        }
    }
}

#[derive(Clone)]
pub struct Subscription { pub endpoint: String, pub public: PublicKey, pub auth: Auth }

fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn push_endpoint(endpoint: &str) -> io::Result<Url> {
    if endpoint.len() > 2048 { return Err(invalid("push endpoint too long")); }
    let url = Url::parse(endpoint).map_err(|_| invalid("invalid push endpoint"))?;
    let host = url.host_str().unwrap_or("");
    // Push subscriptions are URLs supplied by a browser. Restrict outbound
    // requests to known public push services to prevent server-side request
    // forgery into the tailnet or local network.
    if url.scheme() != "https" || !matches!(host,
        "updates.push.services.mozilla.com" | "fcm.googleapis.com" | "web.push.apple.com")
        || url.port().is_some() || !url.username().is_empty() || url.password().is_some()
        || url.fragment().is_some() || url.query().is_some() || url.path() == "/" {
        return Err(invalid("push endpoint is not an approved HTTPS push service"));
    }
    Ok(url)
}

pub fn subscription(value: &Value) -> io::Result<Subscription> {
    let endpoint = value["endpoint"].as_str().ok_or_else(|| invalid("push endpoint missing"))?;
    push_endpoint(endpoint)?;
    let p256dh = value["keys"]["p256dh"].as_str().ok_or_else(|| invalid("push public key missing"))?;
    let auth = value["keys"]["auth"].as_str().ok_or_else(|| invalid("push auth secret missing"))?;
    if p256dh.len()>100 || auth.len()>32 { return Err(invalid("push subscription keys too long")); }
    let public = URL_SAFE_NO_PAD.decode(p256dh).map_err(|_| invalid("invalid push public key"))?;
    let secret = URL_SAFE_NO_PAD.decode(auth).map_err(|_| invalid("invalid push auth secret"))?;
    if public.len() != 65 || public[0] != 4 || secret.len() != 16 {
        return Err(invalid("invalid push subscription key length"));
    }
    let public = PublicKey::from_sec1_bytes(&public).map_err(|_| invalid("invalid push public point"))?;
    Ok(Subscription { endpoint:endpoint.into(), public, auth:Auth::clone_from_slice(&secret) })
}

pub struct Runtime {
    key: ES256KeyPair,
    public_key: String,
    subject: String,
    http: Client,
    slots: Arc<Semaphore>,
}
impl Runtime {
    pub fn load(runtime: &Path) -> io::Result<Option<Self>> {
        let path = runtime.join("vapid.key");
        let mut file = match OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC).open(&path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        let meta = file.metadata()?;
        if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() }
            || meta.nlink() != 1 || meta.permissions().mode() & 0o077 != 0 || meta.len() > 4096 {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "VAPID key file must be private and owned"));
        }
        let mut encoded = String::new();
        Read::by_ref(&mut file).take(4097).read_to_string(&mut encoded)?;
        if encoded.len()>4096{return Err(invalid("VAPID key file too large"));}
        let raw = URL_SAFE_NO_PAD.decode(encoded.trim_end())
            .map_err(|_| invalid("invalid VAPID private key"))?;
        if raw.len()!=32{return Err(invalid("invalid VAPID private key length"));}
        let key = ES256KeyPair::from_bytes(&raw).map_err(|_| invalid("invalid VAPID private key"))?;
        let subject = std::env::var("DOXA_HUB_VAPID_SUBJECT")
            .map_err(|_| invalid("DOXA_HUB_VAPID_SUBJECT is required when push is enabled"))?;
        if !subject.starts_with("mailto:") || subject.len() > 200
            || !subject[7..].contains('@') || subject.chars().any(|c| c.is_control() || c.is_whitespace()) {
            return Err(invalid("VAPID subject must be a mailto: address"));
        }
        let http = Client::builder().no_proxy().https_only(true)
            .redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(8))
            .build().map_err(|_| invalid("push HTTP client unavailable"))?;
        Ok(Some(Self { public_key:public_key(&key), key,
            subject, http, slots:Arc::new(Semaphore::new(8)) }))
    }
    pub fn public_key(&self) -> &str { &self.public_key }
    pub fn permit(&self) -> Option<OwnedSemaphorePermit> {
        self.slots.clone().try_acquire_owned().ok()
    }

    /// A false result means the push service confirmed the subscription is
    /// expired. Other failures are transient and leave the subscription intact.
    pub async fn send(&self, subscription: &Subscription, kind: Kind, _permit: OwnedSemaphorePermit) -> bool {
        let Ok(endpoint) = subscription.endpoint.parse() else { return false; };
        let builder = WebPushBuilder::new(endpoint, subscription.public.clone(), subscription.auth.clone())
            .with_valid_duration(Duration::from_secs(300))
            .with_vapid(&self.key, &self.subject);
        let Ok(request) = builder.build(kind.payload().to_vec()) else { return false; };
        let (parts, body) = request.into_parts();
        let mut request = self.http.post(parts.uri.to_string()).body(body);
        for (name, value) in &parts.headers {
            let Ok(value) = reqwest::header::HeaderValue::from_bytes(value.as_bytes()) else { return false; };
            request = request.header(name.as_str(), value);
        }
        let result = request.send().await;
        matches!(result, Ok(response) if response.status().as_u16() == 404 || response.status().as_u16() == 410)
    }
}

fn public_key(key: &ES256KeyPair) -> String {
    URL_SAFE_NO_PAD.encode(key.public_key().public_key().to_bytes_uncompressed())
}

/// Generate a key only on an explicit operator command. The hub otherwise
/// starts without push support when the private key file is absent.
pub fn generate(runtime: &Path) -> io::Result<String> {
    let key = ES256KeyPair::generate();
    let mut file = OpenOptions::new().write(true).create_new(true).mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(runtime.join("vapid.key"))?;
    file.write_all(URL_SAFE_NO_PAD.encode(key.to_bytes()).as_bytes())?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    Ok(public_key(&key))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn push_endpoint_rejects_local_and_unlisted_networks() {
        for endpoint in ["http://fcm.googleapis.com/fcm/x", "https://127.0.0.1/push",
            "https://owner.tail.ts.net/push", "https://fcm.googleapis.com:8443/push",
            "https://fcm.googleapis.com.evil.test/push", "https://fcm.googleapis.com/"] {
            assert!(push_endpoint(endpoint).is_err(), "{endpoint}");
        }
        assert!(push_endpoint("https://updates.push.services.mozilla.com/wpush/v2/token").is_ok());
        assert!(subscription(&json!({"endpoint":"https://fcm.googleapis.com/fcm/send/x",
            "keys":{"p256dh":"invalid","auth":"invalid"}})).is_err());
    }
    #[test]
    fn key_generation_is_private_and_never_overwrites() {
        let dir=tempfile::tempdir().unwrap();
        let public=generate(dir.path()).unwrap();
        assert_eq!(URL_SAFE_NO_PAD.decode(&public).unwrap().len(),65);
        let path=dir.path().join("vapid.key");
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,0o600);
        assert!(generate(dir.path()).is_err());
        assert!(!std::fs::read_to_string(path).unwrap().contains(&public));
    }
    #[test]
    fn generated_push_request_encrypts_generic_event_and_signs_vapid() {
        let vapid=ES256KeyPair::generate();
        let receiver=ES256KeyPair::generate();
        let public=PublicKey::from_sec1_bytes(
            &receiver.public_key().public_key().to_bytes_uncompressed()).unwrap();
        let builder=WebPushBuilder::new(
            "https://fcm.googleapis.com/fcm/send/test".parse().unwrap(),
            public,Auth::clone_from_slice(&[4u8;16]))
            .with_valid_duration(Duration::from_secs(300))
            .with_vapid(&vapid,"mailto:test@example.com");
        let request=builder.build(Kind::NeedsInput.payload().to_vec()).unwrap();
        assert_eq!(request.headers()["content-encoding"],"aes128gcm");
        assert!(request.headers()["authorization"].to_str().unwrap().starts_with("vapid "));
        assert!(!request.body().windows(11).any(|window|window==b"needs_input"));
    }
}
