//! Explicit, read-only OpenAI API catalog check. This never starts inference
//! and does not claim that a listed model can complete a Responses request.
use serde::Deserialize;
use std::{io, time::Duration};

const MODELS_URL: &str = "https://api.openai.com/v1/models";
const MAX_RESPONSE: usize = 1024 * 1024;
const MAX_MODELS: usize = 4096;
const DEADLINE: Duration = Duration::from_secs(5);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CatalogStatus { Listed, Unlisted, Unknown }
impl CatalogStatus {
    pub fn as_str(self) -> &'static str {
        match self { Self::Listed => "listed", Self::Unlisted => "unlisted", Self::Unknown => "unknown" }
    }
}

#[derive(Deserialize)]
struct Catalog { object: String, data: Vec<CatalogModel> }
#[derive(Deserialize)]
struct CatalogModel { object: String, id: String }

fn exact_id(value: &str) -> bool {
    !value.is_empty() && value.len() <= 100 && value.bytes().all(|byte|
        byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
}

/// Uses the same OPENAI_API_KEY as an independent `codex:` fleet reviewer.
/// A missing key or failed catalog read yields `unknown`, never an assertion
/// of unavailable inference access. Only the explicit CLI flag calls this.
pub fn check(engine: &str, model: &str) -> io::Result<CatalogStatus> {
    if engine != "codex" || !exact_id(model) {
        return Err(io::Error::new(io::ErrorKind::InvalidInput,
            "catalog check requires an exact codex model ID"));
    }
    let key = match std::env::var("OPENAI_API_KEY").ok().filter(|key|
        (8..=4096).contains(&key.len()) && key.bytes().all(|byte| byte.is_ascii_graphic())) {
        Some(key) => key,
        None => return Ok(CatalogStatus::Unknown),
    };
    Ok(check_at(MODELS_URL, &key, model, DEADLINE))
}

fn check_at(url: &str, key: &str, model: &str, deadline: Duration) -> CatalogStatus {
    let runtime = match tokio::runtime::Builder::new_current_thread().enable_all().build() {
        Ok(runtime) => runtime,
        Err(_) => return CatalogStatus::Unknown,
    };
    runtime.block_on(async {
        tokio::time::timeout(deadline, async {
            let http = reqwest::Client::builder()
                .no_proxy().redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_secs(2)).build().ok()?;
            let mut reply = http.get(url).bearer_auth(key).send().await.ok()?;
            if reply.status() != reqwest::StatusCode::OK ||
                reply.content_length().is_some_and(|length| length > MAX_RESPONSE as u64) {
                return None;
            }
            let mut bytes = Vec::new();
            while let Some(chunk) = reply.chunk().await.ok()? {
                if bytes.len().saturating_add(chunk.len()) > MAX_RESPONSE { return None; }
                bytes.extend_from_slice(&chunk);
            }
            let catalog: Catalog = serde_json::from_slice(&bytes).ok()?;
            if catalog.object != "list" || catalog.data.len() > MAX_MODELS { return None; }
            let mut matches = 0;
            for row in catalog.data {
                if row.object != "model" || row.id.is_empty() || row.id.len() > 256 ||
                    !row.id.bytes().all(|byte| byte.is_ascii_graphic()) { return None; }
                matches += usize::from(row.id == model);
                if matches > 1 { return None; }
            }
            Some(if matches == 1 { CatalogStatus::Listed } else { CatalogStatus::Unlisted })
        }).await.ok().flatten().unwrap_or(CatalogStatus::Unknown)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{io::{Read, Write}, net::{Ipv4Addr, TcpListener}, thread::{self, JoinHandle}, time::Instant};

    fn fake_server(reply: Vec<u8>, delay: Duration) -> (String, JoinHandle<String>) {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        listener.set_nonblocking(true).unwrap();
        let url = format!("http://{}/v1/models", listener.local_addr().unwrap());
        let worker = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(2);
            let mut stream = loop {
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock && Instant::now() < deadline =>
                        thread::sleep(Duration::from_millis(5)),
                    Err(error) => panic!("catalog fixture accept failed: {error}"),
                }
            };
            stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            stream.set_write_timeout(Some(Duration::from_secs(2))).unwrap();
            let mut request = Vec::new();
            while !request.ends_with(b"\r\n\r\n") && request.len() < 8192 {
                let mut byte = [0]; stream.read_exact(&mut byte).unwrap(); request.push(byte[0]);
            }
            thread::sleep(delay);
            let _ = stream.write_all(&reply);
            String::from_utf8(request).unwrap()
        });
        (url, worker)
    }
    fn response(status: &str, body: &[u8]) -> Vec<u8> {
        let mut reply = format!("HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).into_bytes();
        reply.extend_from_slice(body); reply
    }

    #[test]
    fn same_key_exact_listing_and_unlisting_use_get_only() {
        for (body, expected) in [
            (br#"{"object":"list","data":[{"object":"model","id":"gpt-6-astra"}]}"#.as_slice(), CatalogStatus::Listed),
            (br#"{"object":"list","data":[{"object":"model","id":"gpt-6-sol"}]}"#, CatalogStatus::Unlisted),
        ] {
            let (url, worker) = fake_server(response("200 OK", body), Duration::ZERO);
            assert_eq!(check_at(&url, "fixture-key", "gpt-6-astra", Duration::from_secs(2)), expected);
            let request = worker.join().unwrap();
            assert!(request.starts_with("GET /v1/models HTTP/1.1\r\n"));
            assert!(request.to_ascii_lowercase().contains("authorization: bearer fixture-key\r\n"));
            assert!(!request.contains("POST "));
        }
    }

    #[test]
    fn malformed_oversized_non_success_and_slow_replies_are_unknown() {
        let malformed = br#"{"object":"other","data":[{"object":"model","id":"gpt-6-astra"}]}"#;
        for reply in [
            response("200 OK", malformed),
            response("401 Unauthorized", b"secret diagnostic body"),
            response("200 OK", &vec![b'x'; MAX_RESPONSE + 1]),
        ] {
            let (url, worker) = fake_server(reply, Duration::ZERO);
            assert_eq!(check_at(&url, "fixture-key", "gpt-6-astra", Duration::from_secs(2)), CatalogStatus::Unknown);
            worker.join().unwrap();
        }
        let (url, worker) = fake_server(response("200 OK", br#"{"object":"list","data":[]}"#), Duration::from_millis(150));
        assert_eq!(check_at(&url, "fixture-key", "gpt-6-astra", Duration::from_millis(30)), CatalogStatus::Unknown);
        worker.join().unwrap();
    }

    #[test]
    fn redirects_are_not_followed_and_invalid_engine_is_rejected() {
        let target = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        target.set_nonblocking(true).unwrap();
        let reply = format!("HTTP/1.1 302 Found\r\nLocation: http://{}/v1/models\r\nContent-Length: 0\r\n\r\n", target.local_addr().unwrap()).into_bytes();
        let (url, worker) = fake_server(reply, Duration::ZERO);
        assert_eq!(check_at(&url, "fixture-key", "gpt-6-astra", Duration::from_secs(2)), CatalogStatus::Unknown);
        worker.join().unwrap();
        assert!(target.accept().is_err(), "redirected request leaked the API key");
        assert!(check("claude", "gpt-6-astra").is_err());
        assert!(check("codex", "gpt-6-astra\n").is_err());
    }
}
