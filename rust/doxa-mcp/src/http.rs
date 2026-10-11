use crate::{config::{self, EnvRef}, wire, Error, ErrorKind, Limits, Result};
use reqwest::{header::{HeaderMap, HeaderName, HeaderValue, ACCEPT, CONTENT_TYPE}, Client, Response, Url};
use serde_json::{json, Value};
use std::{collections::BTreeMap, sync::{Arc, atomic::{AtomicBool, Ordering}}, time::Duration};

pub(crate) struct HttpTransport {
    client: Client,
    url: Url,
    headers: HeaderMap,
    pub(crate) session: Option<HeaderValue>,
    pub(crate) version: Option<String>,
}
impl HttpTransport {
    pub(crate) fn open(url: &str, headers: &BTreeMap<String,EnvRef>, allow_local: bool, limits: &Limits) -> Result<(Self,Vec<String>)> {
        config::credential_free(url)?;
        let url = config::endpoint(url, allow_local)?;
        let mut resolved = HeaderMap::new();
        let mut secrets = Vec::new();
        for (name, reference) in headers {
            let secret = config::resolve(reference)?;
            if url.as_str().contains(&secret) { return Err(Error::config("credentials must not occur in endpoint")); }
            let value = if reference.bearer { format!("Bearer {secret}") } else { secret.clone() };
            let mut value = HeaderValue::from_str(&value).map_err(|_| Error::new(ErrorKind::Credential,false))?;
            value.set_sensitive(true);
            resolved.insert(HeaderName::from_bytes(name.as_bytes()).map_err(|_|Error::config("invalid header"))?,value);
            secrets.push(secret);
        }
        let client = Client::builder().redirect(reqwest::redirect::Policy::none()).no_proxy()
            .connect_timeout(Duration::from_millis(limits.timeout_ms)).build().map_err(|_|Error::new(ErrorKind::Io,false))?;
        Ok((Self { client, url, headers: resolved, session: None, version: None },secrets))
    }
    fn request(&self, method: reqwest::Method) -> reqwest::RequestBuilder {
        let mut request = self.client.request(method,self.url.clone()).headers(self.headers.clone())
            .header(ACCEPT,"application/json, text/event-stream");
        if let Some(session) = &self.session { request = request.header("Mcp-Session-Id",session); }
        if let Some(version) = &self.version { request = request.header("MCP-Protocol-Version",version); }
        request
    }
    async fn post(&mut self, value: &Value, limit: usize, attempted: &AtomicBool) -> Result<Response> {
        let initializing = value["method"] == "initialize";
        let bytes = serde_json::to_vec(value).map_err(|_|Error::protocol("cannot encode request"))?;
        if bytes.len() > limit { return Err(Error::new(ErrorKind::Limit("request bytes"),false)); }
        let request = self.request(reqwest::Method::POST).header(CONTENT_TYPE,"application/json").body(bytes)
            .build().map_err(|_|Error::new(ErrorKind::Io,false))?;
        attempted.store(true,Ordering::SeqCst);
        let response = self.client.execute(request).await.map_err(|_|Error::new(ErrorKind::Io,true))?;
        if response.status().as_u16() == 404 && self.session.is_some() { return Err(Error::new(ErrorKind::SessionExpired,true)); }
        if !response.status().is_success() { return Err(Error::new(ErrorKind::Http { status: response.status().as_u16() },true)); }
        let values = response.headers().get_all("Mcp-Session-Id").iter().collect::<Vec<_>>();
        if values.len() > 1 { return Err(Error::protocol("multiple session IDs")); }
        if let Some(value) = values.first() {
            if value.as_bytes().is_empty() || value.as_bytes().len() > 256 || !value.as_bytes().iter().all(|b|(0x21..=0x7e).contains(b)) {
                return Err(Error::protocol("invalid session ID"));
            }
            if let Some(existing) = &self.session {
                if existing != *value { return Err(Error::protocol("session ID changed")); }
            } else {
                if !initializing { return Err(Error::protocol("session ID assigned after initialization")); }
                let mut value = (*value).clone(); value.set_sensitive(true); self.session = Some(value);
            }
        }
        Ok(response)
    }
    pub(crate) async fn notify(&mut self, value: &Value, limits: &Limits) -> Result<()> {
        let mut response = self.post(value,limits.max_frame_bytes,&AtomicBool::new(false)).await?;
        if response.status().as_u16() != 202 { return Err(Error::protocol("notification requires HTTP 202")); }
        while let Some(chunk) = response.chunk().await.map_err(|_|Error::new(ErrorKind::Io,true))? {
            if !chunk.is_empty() { return Err(Error::protocol("notification response must be empty")); }
        }
        Ok(())
    }
    async fn message(&mut self, bytes: &[u8], id: u64, limits: &Limits, changed: &mut bool) -> Result<Option<Value>> {
        match wire::classify(wire::parse(bytes)?,id)? {
            wire::Message::Response(value) => Ok(Some(value)),
            wire::Message::Notification(method) => { if method == "notifications/tools/list_changed" { *changed = true; } Ok(None) },
            wire::Message::Request(id,method) => {
                let reply = if method == "ping" { json!({"jsonrpc":"2.0","id":id,"result":{}}) }
                    else { json!({"jsonrpc":"2.0","id":id,"error":{"code":-32601,"message":"Client capability not supported"}}) };
                self.notify(&reply,limits).await?; Ok(None)
            }
        }
    }
    pub(crate) async fn exchange(&mut self, request: &Value, id: u64, limits: &Limits, attempted: Arc<AtomicBool>, changed: &mut bool) -> Result<Value> {
        let mut response = self.post(request,limits.max_frame_bytes,&attempted).await?;
        let media = response.headers().get(CONTENT_TYPE).and_then(|v|v.to_str().ok()).unwrap_or("").split(';').next().unwrap_or("").trim().to_ascii_lowercase();
        if !matches!(media.as_str(),"application/json"|"text/event-stream") { return Err(Error::protocol("unsupported HTTP response type")); }
        if response.content_length().is_some_and(|n| n > limits.max_frame_bytes as u64) { return Err(Error::new(ErrorKind::Limit("response bytes"),true)); }
        let mut total = 0;
        let mut pending = Vec::new();
        let mut data = String::new();
        let mut messages = 0;
        while let Some(chunk) = response.chunk().await.map_err(|_|Error::new(ErrorKind::Io,true))? {
            total += chunk.len();
            if total > limits.max_frame_bytes { return Err(Error::new(ErrorKind::Limit("response bytes"),true)); }
            pending.extend_from_slice(&chunk);
            if media == "application/json" { continue; }
            let mut processed = 0;
            while let Some(end) = pending[processed..].iter().position(|&b|b == b'\n') {
                let end = processed + end;
                let line = pending[processed..end].strip_suffix(b"\r").unwrap_or(&pending[processed..end]);
                let line = std::str::from_utf8(line).map_err(|_|Error::protocol("invalid SSE UTF-8"))?;
                processed = end + 1;
                if line.is_empty() {
                    if !data.is_empty() {
                        messages += 1;
                        if messages > limits.max_notifications + 1 { return Err(Error::new(ErrorKind::Limit("server messages"),true)); }
                        if let Some(result) = self.message(data.as_bytes(),id,limits,changed).await? { return Ok(result); }
                        data.clear();
                    }
                } else if let Some(value) = line.strip_prefix("data:") {
                    let value = value.strip_prefix(' ').unwrap_or(value);
                    if !data.is_empty() { data.push('\n'); }
                    data.push_str(value);
                }
            }
            pending.drain(..processed);
        }
        if media == "application/json" {
            return match wire::classify(wire::parse(&pending)?,id)? {
                wire::Message::Response(result) => Ok(result),
                _ => Err(Error::protocol("HTTP JSON must contain a response")),
            };
        }
        Err(Error::new(ErrorKind::Io,true)) // no unsafe POST replay/resume
    }
    pub(crate) async fn close(&mut self) {
        if self.session.is_some() {
            let _ = self.request(reqwest::Method::DELETE).send().await;
            self.session = None;
        }
    }
}
