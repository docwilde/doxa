//! Display-only Z.ai account reporting. Never changes inference transport,
//! grants Coding Plan entitlement, or prices a prepaid resource remainder.
use futures_util::StreamExt;
use serde_json::{json, Value};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use std::sync::atomic::{AtomicBool,Ordering};

pub const QUOTA_URL: &str = "https://api.z.ai/api/monitor/usage/quota/limit";
pub const BILLING_CONSOLE: &str = "https://z.ai/manage-apikey/billing";
pub const QUOTA_SOURCE: &str = "https://github.com/zai-org/zai-coding-plugins/blob/0446d0bb0bc537d97d3ab3664c4b8b9c4a0e1254/plugins/glm-plan-usage/skills/usage-query-skill/scripts/query-usage.mjs";
pub const CHECKED_ON: &str = "2026-10-11";
const BODY_MAX: usize = 16 * 1024;
const LIMITS_MAX: usize = 8;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Profile { StandardApi, ApiResourceBundle }
impl Profile {
    pub fn parse(value: &str) -> Option<Self> {
        match value.trim() { "" | "standard_api" => Some(Self::StandardApi),
            "api_resource_bundle" => Some(Self::ApiResourceBundle), _ => None }
    }
    pub fn id(self) -> &'static str { match self {
        Self::StandardApi => "standard_api", Self::ApiResourceBundle => "api_resource_bundle" } }
}

#[derive(Clone, Debug, PartialEq)]
pub struct QuotaSnapshot {
    status: &'static str,
    limits: Vec<QuotaLimit>,
    observed_at: Option<u64>,
}
#[derive(Clone, Debug, PartialEq)]
struct QuotaLimit { kind: &'static str, used_percent: f64 }
impl QuotaSnapshot {
    pub fn not_checked() -> Self { Self::unavailable("not_checked") }
    pub fn refreshing() -> Self { Self::unavailable("refreshing") }
    pub fn unavailable(status: &'static str) -> Self { Self { status, limits:Vec::new(), observed_at:None } }
    pub fn value(&self) -> Value {
        let now = SystemTime::now().duration_since(UNIX_EPOCH).ok().map(|time|time.as_secs());
        let stale = self.status == "verified" && !matches!((now,self.observed_at),
            (Some(now),Some(observed)) if observed <= now && now - observed <= 120);
        json!({"status":if stale {"stale"} else {self.status},"observed_at":self.observed_at,
            "limits":self.limits.iter().filter(|_|!stale).map(|limit|json!({"kind":limit.kind,"used_percent":limit.used_percent})).collect::<Vec<_>>(),
            "source":QUOTA_SOURCE,"source_checked_on":CHECKED_ON})
    }
}

pub fn account_report(profile: Option<Profile>, owner_declared: bool, monitor: bool, quota: &QuotaSnapshot) -> Value {
    json!({"mode":"api","provider":"zai","glm_account":{
        "profile":profile.map(Profile::id),"profile_source":if profile.is_none(){"invalid_config"}else if owner_declared{"owner_declared"}else{"default"},
        "inference_endpoint":super::Vendor::Glm.endpoint(),
        "cash_balance_status":"unknown","resource_bundle_remaining_status":"unknown",
        "provider_charge_status":"unknown","session_price_basis":"list_price_estimate_only",
        "console_url":BILLING_CONSOLE,"source_checked_on":CHECKED_ON,
        "plan_monitor":{"enabled":monitor,"scope":"external_coding_plan_monitor_only",
            "inference_uses_plan":false,"quota":if monitor {quota.value()}else {QuotaSnapshot::unavailable("disabled").value()}}
    }})
}

/// Explicit read-only monitor refresh. Callers must require separate opt-in;
/// normal GLM startup, catalog admission and inference never call this.
pub async fn refresh_plan_quota(cancel:&AtomicBool) -> QuotaSnapshot {
    if cancel.load(Ordering::Acquire) {return QuotaSnapshot::unavailable("cancelled");}
    let key = match super::credentials::resolve(super::Vendor::Glm) {
        Ok(Some(key)) => key,
        _ => return QuotaSnapshot::unavailable("credential_unavailable"),
    };
    fetch_at_cancel(QUOTA_URL, &key, Duration::from_secs(3),cancel).await
}

#[cfg(test)]
async fn fetch_at(endpoint: &str, key: &str, timeout: Duration) -> QuotaSnapshot {
    fetch_at_cancel(endpoint,key,timeout,&AtomicBool::new(false)).await
}

async fn fetch_at_cancel(endpoint: &str, key: &str, timeout: Duration,cancel:&AtomicBool) -> QuotaSnapshot {
    if cancel.load(Ordering::Acquire) {return QuotaSnapshot::unavailable("cancelled");}
    let request = async {
        let client = reqwest::Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none()).build()
            .map_err(|_| "transport_unavailable")?;
        // The official monitor contract uses the key directly, without Bearer.
        let response = client.get(endpoint).header(reqwest::header::AUTHORIZATION, key)
            .header(reqwest::header::ACCEPT_LANGUAGE, "en-US,en").header(reqwest::header::CONTENT_TYPE, "application/json")
            .send().await.map_err(|_| "transport_unavailable")?;
        if matches!(response.status().as_u16(), 401 | 403) { return Err("unauthorized"); }
        if response.status().as_u16() != 200 { return Err("provider_unavailable"); }
        if response.content_length().is_some_and(|length| length > BODY_MAX as u64) { return Err("response_too_large"); }
        let mut bytes = Vec::new(); let mut stream = response.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk.map_err(|_| "transport_unavailable")?;
            if bytes.len().saturating_add(chunk.len()) > BODY_MAX { return Err("response_too_large"); }
            bytes.extend_from_slice(&chunk);
        }
        if !key.is_empty() && bytes.windows(key.len()).any(|part|part==key.as_bytes()) { return Err("invalid_response"); }
        parse(&bytes)
    };
    let cancellation=async {loop {
        if cancel.load(Ordering::Acquire) {break;}
        tokio::time::sleep(Duration::from_millis(25)).await;
    }};
    let result=tokio::select! {
        _=cancellation=>return QuotaSnapshot::unavailable("cancelled"),
        result=tokio::time::timeout(timeout,request)=>result,
    };
    match result {
        Ok(Ok(limits)) => QuotaSnapshot {status:"verified",limits,
            observed_at:SystemTime::now().duration_since(UNIX_EPOCH).ok().map(|time|time.as_secs())},
        Ok(Err(status)) => QuotaSnapshot::unavailable(status),
        Err(_) => QuotaSnapshot::unavailable("timeout"),
    }
}

fn parse(bytes: &[u8]) -> Result<Vec<QuotaLimit>, &'static str> {
    let value: Value = serde_json::from_slice(bytes).map_err(|_| "invalid_response")?;
    if value.get("success").is_some_and(|success|success != true)
        || value.get("code").is_some_and(|code|code.as_u64() != Some(200)) { return Err("invalid_response"); }
    let data = value.get("data").unwrap_or(&value);
    let limits = data["limits"].as_array().filter(|limits| !limits.is_empty() && limits.len() <= LIMITS_MAX).ok_or("invalid_response")?;
    let mut types=std::collections::HashSet::new();
    limits.iter().map(|limit| {
        // Preserve only returned percentages. Unrecognized type codes remain
        // generic provider quotas: no inferred credit/token units or windows.
        let code=limit["type"].as_str().filter(|kind| !kind.is_empty() && kind.len() <= 32
            && kind.bytes().all(|byte|byte.is_ascii_uppercase() || byte == b'_')).ok_or("invalid_response")?;
        // Duplicate codes without verified window identity are ambiguous.
        if !types.insert(code) {return Err("invalid_response");}
        let kind = match code {
            "TOKENS_LIMIT" => "token_quota",
            "TIME_LIMIT" => "mcp_quota",
            _ => "provider_quota",
        };
        let used_percent = limit["percentage"].as_f64().filter(|percent|percent.is_finite() && (0.0..=100.0).contains(percent)).ok_or("invalid_response")?;
        Ok(QuotaLimit {kind,used_percent})
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{io::{Read,Write},net::TcpListener,thread};
    fn fixture(status: u16, body: String, delay: Duration) -> (String,thread::JoinHandle<()>) {
        let response=format!("HTTP/1.1 {status} fixture\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len());
        raw_fixture(response,delay)
    }
    fn raw_fixture(response:String, delay:Duration)->(String,thread::JoinHandle<()>) {
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();let url=format!("http://{}/quota",listener.local_addr().unwrap());
        listener.set_nonblocking(true).unwrap();
        let worker=thread::spawn(move || {
            let deadline=std::time::Instant::now()+Duration::from_secs(3);
            let mut socket=loop {match listener.accept() {
                Ok((socket,_))=>break socket,
                Err(error) if error.kind()==std::io::ErrorKind::WouldBlock=>{
                    assert!(std::time::Instant::now()<deadline,"fixture request absent");thread::sleep(Duration::from_millis(2));
                }
                Err(error)=>panic!("{error}"),
            }};
            socket.set_nonblocking(false).unwrap();socket.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
            let mut bytes=Vec::new();while !bytes.ends_with(b"\r\n\r\n") {let mut byte=[0];socket.read_exact(&mut byte).unwrap();bytes.push(byte[0]);}
            let request=String::from_utf8(bytes).unwrap().to_ascii_lowercase();
            assert!(request.starts_with("get /quota http/1.1\r\n"));assert!(request.contains("authorization: synthetic-quota-fixture\r\n"));
            assert!(!request.contains("authorization: bearer "));thread::sleep(delay);
            let _=socket.write_all(response.as_bytes());
        });(url,worker)
    }
    #[tokio::test]
    async fn provider_percentages_are_bounded_and_do_not_claim_token_windows_or_balances() {
        let body=json!({"success":true,"code":200,"data":{"limits":[{"type":"TOKENS_LIMIT","percentage":0,"unit":3,"number":5},
            {"type":"CREDIT_LIMIT","percentage":82,"usage":2000,"remaining":346},{"type":"TIME_LIMIT","percentage":8}],"level":"private account name"}}).to_string();
        let (url,worker)=fixture(200,body,Duration::ZERO);let quota=fetch_at(&url,"synthetic-quota-fixture",Duration::from_secs(2)).await;worker.join().unwrap();
        assert_eq!(quota.status,"verified");assert_eq!(quota.limits.len(),3);assert_eq!(quota.limits[0].used_percent,0.0);
        let report=account_report(Some(Profile::ApiResourceBundle),true,true,&quota);
        assert_eq!(report["mode"],"api");assert_eq!(report["glm_account"]["profile_source"],"owner_declared");
        assert_eq!(report["glm_account"]["cash_balance_status"],"unknown");
        assert_eq!(report["glm_account"]["resource_bundle_remaining_status"],"unknown");
        assert_eq!(report["glm_account"]["plan_monitor"]["inference_uses_plan"],false);
        assert!(!report.to_string().contains("private account name"));assert!(!report.to_string().contains("remaining\":346"));
    }
    #[tokio::test]
    async fn malformed_unauthorized_timeout_and_large_quota_responses_stay_unknown() {
        for (status,body,delay,timeout,expected) in [
            (200,"{malformed".into(),Duration::ZERO,Duration::from_secs(2),"invalid_response"),
            (200,json!({"success":false,"data":{"limits":[{"type":"TOKENS_LIMIT","percentage":0}]}}).to_string(),Duration::ZERO,Duration::from_secs(2),"invalid_response"),
            (200,json!({"data":{"limits":[{"type":"TOKENS_LIMIT","percentage":5},{"type":"TOKENS_LIMIT","percentage":5}]}}).to_string(),Duration::ZERO,Duration::from_secs(2),"invalid_response"),
            (200,json!({"data":{"limits":[{"type":"TOKENS_LIMIT","percentage":"0"}]}}).to_string(),Duration::ZERO,Duration::from_secs(2),"invalid_response"),
            (200,json!({"data":{"limits":[{"type":"private account text","percentage":0}]}}).to_string(),Duration::ZERO,Duration::from_secs(2),"invalid_response"),
            (200,json!({"data":{"limits":[{"type":"TOKENS_LIMIT","percentage":0}]},"diagnostic":"synthetic-quota-fixture"}).to_string(),Duration::ZERO,Duration::from_secs(2),"invalid_response"),
            (401,"synthetic-quota-fixture provider error".into(),Duration::ZERO,Duration::from_secs(2),"unauthorized"),
            (200,"x".repeat(BODY_MAX+1),Duration::ZERO,Duration::from_secs(2),"response_too_large"),
            (200,"{}".into(),Duration::from_millis(600),Duration::from_millis(300),"timeout") ] {
            let (url,worker)=fixture(status,body,delay);let quota=fetch_at(&url,"synthetic-quota-fixture",timeout).await;worker.join().unwrap();
            assert_eq!(quota.status,expected);assert!(quota.limits.is_empty());assert_eq!(quota.observed_at,None);
            assert!(!quota.value().to_string().contains("synthetic-quota-fixture"));
        }
    }
    #[tokio::test]
    async fn chunked_body_cap_and_redirect_refusal_protect_monitor_credentials() {
        let body="x".repeat(BODY_MAX+1);
        let response=format!("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n{:x}\r\n{body}\r\n0\r\n\r\n",body.len());
        let (url,worker)=raw_fixture(response,Duration::ZERO);
        let quota=fetch_at(&url,"synthetic-quota-fixture",Duration::from_secs(2)).await;worker.join().unwrap();
        assert_eq!(quota.status,"response_too_large");
        let destination=TcpListener::bind("127.0.0.1:0").unwrap();destination.set_nonblocking(true).unwrap();
        let response=format!("HTTP/1.1 302 Found\r\nLocation: http://{}/leak\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",destination.local_addr().unwrap());
        let (url,worker)=raw_fixture(response,Duration::ZERO);
        let quota=fetch_at(&url,"synthetic-quota-fixture",Duration::from_secs(2)).await;worker.join().unwrap();
        assert_eq!(quota.status,"provider_unavailable");
        assert_eq!(destination.accept().unwrap_err().kind(),std::io::ErrorKind::WouldBlock);
    }

    #[test]
    fn stale_provider_quota_retains_timestamp_without_presenting_current_percentages() {
        let quota=QuotaSnapshot {status:"verified",limits:vec![QuotaLimit {kind:"provider_quota",used_percent:82.0}],observed_at:Some(1)};
        assert_eq!(quota.value()["status"],"stale");assert_eq!(quota.value()["observed_at"],1);
        assert!(quota.value()["limits"].as_array().unwrap().is_empty());
    }
    #[test]
    fn malformed_or_missing_fields_never_imply_zero_or_free_prepaid_work() {
        for limit in [json!({"type":"TOKENS_LIMIT"}),json!({"type":"TOKENS_LIMIT","percentage":-1}),
            json!({"type":"TOKENS_LIMIT","percentage":101}),json!({"type":"TOKENS_LIMIT","percentage":"0"})] {
            assert!(parse(json!({"data":{"limits":[limit]}}).to_string().as_bytes()).is_err());
        }
        assert!(parse(b"{\"data\":{\"limits\":[]}}").is_err());
        let report=account_report(Some(Profile::StandardApi),false,false,&QuotaSnapshot::not_checked());
        assert_eq!(report["glm_account"]["plan_monitor"]["quota"]["status"],"disabled");
        assert!(Profile::parse("coding_plan").is_none());
        for kind in [json!(null),json!(22),json!(""),json!("tokens_limit"),json!("TOKEN\nLIMIT"),json!("A".repeat(33))] {
            assert!(parse(json!({"limits":[{"type":kind,"percentage":0}]}).to_string().as_bytes()).is_err());
        }
        assert!(parse(br#"{"limits":[{"type":"TOKENS_LIMIT","percentage":5},{"type":"TOKENS_LIMIT","percentage":5}]}"#).is_err());
    }

    #[tokio::test]
    async fn cancelled_monitor_is_bounded_and_hides_percentages() {
        let cancel=std::sync::Arc::new(AtomicBool::new(true));
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
        let quota=fetch_at_cancel(&format!("http://{}/quota",listener.local_addr().unwrap()),"synthetic-quota-fixture",Duration::from_secs(2),&cancel).await;
        assert_eq!(quota.status,"cancelled");assert!(listener.accept().is_err());
        cancel.store(false,Ordering::Release);
        let (url,worker)=fixture(200,"{}".into(),Duration::from_millis(600));
        let cancelled=cancel.clone();
        let set=tokio::spawn(async move {tokio::time::sleep(Duration::from_millis(100)).await;cancelled.store(true,Ordering::Release);});
        let start=std::time::Instant::now();let quota=fetch_at_cancel(&url,"synthetic-quota-fixture",Duration::from_secs(2),&cancel).await;
        assert_eq!(quota.status,"cancelled");assert!(start.elapsed()<Duration::from_millis(500));
        assert!(quota.limits.is_empty());set.await.unwrap();worker.join().unwrap();
    }
}
