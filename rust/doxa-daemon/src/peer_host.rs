//! Same-scope native peer RPCs. LORE starts lazily and failed starts retry.
use doxa_lore::LoreClient;
use doxa_peers::delivery::{self, Ledger, PeerFrame, RateLimiter, SendLimits};
use doxa_peers::{now, presence, scope_for_cwd, PeerRecord, Registry};
use doxa_runtime::Host;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{mpsc::SyncSender, Arc, Mutex};
use std::time::Duration;

pub struct PeerHost {
    inner: Arc<dyn Host>,
    lore: Mutex<Option<LoreClient>>,
    lore_python: Option<PathBuf>,
    runtime: PathBuf,
    scope: String,
    session_id: String,
    title: String,
    limiter: Mutex<RateLimiter>,
    inbound_limiters: Mutex<HashMap<String, RateLimiter>>,
    ledger: Ledger,
    events: SyncSender<Value>,
}

impl PeerHost {
    pub fn new(
        inner: Arc<dyn Host>,
        runtime: PathBuf,
        cwd: &Path,
        session_id: String,
        title: String,
        lore_python: Option<&Path>,
        events: SyncSender<Value>,
    ) -> io::Result<Self> {
        let scope = scope_for_cwd(cwd)?;
        let home = std::env::var_os("DOXA_HOME")
            .filter(|s| !s.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".doxa")
            });
        if !home.is_absolute() {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "DOXA home must be absolute"));
        }
        Ok(Self {
            inner,
            lore: Mutex::new(None),
            lore_python: lore_python.map(Path::to_path_buf),
            runtime,
            scope,
            session_id,
            title,
            limiter: Mutex::new(RateLimiter::new(SendLimits::default())),
            inbound_limiters: Mutex::new(HashMap::new()),
            ledger: Ledger::new(home.join("peers/messages.jsonl")),
            events,
        })
    }

    fn with_lore<T>(
        &self,
        work: impl FnOnce(&mut LoreClient) -> Result<T, String>,
    ) -> Result<T, String> {
        let python = self
            .lore_python
            .as_deref()
            .ok_or("LORE scrub unavailable")?;
        let mut guard = self.lore.lock().map_err(|_| "LORE scrub unavailable")?;
        if guard.is_none() {
            *guard = Some(
                LoreClient::spawn(python, Duration::from_secs(5))
                    .map_err(|_| "LORE scrub unavailable")?,
            );
        }
        let result = work(guard.as_mut().ok_or("LORE scrub unavailable")?);
        if result.is_err() && guard.as_ref().is_some_and(|client| !client.is_alive()) {
            *guard = None;
        }
        result
    }

    fn roster(&self, lore: &mut LoreClient) -> Result<Vec<presence::DisplayPeer>, String> {
        presence::list_scoped_readonly(&self.runtime, &self.scope, &self.session_id, |text| {
            lore.scrub(text)
                .map_err(|_| io::Error::other("LORE scrub unavailable"))
        })
        .map_err(|_| "peer discovery unavailable or LORE scrub failed".to_owned())
    }

    fn peers(&self) -> Result<Value, String> {
        self.with_lore(|lore| Ok(json!({"peers":self.roster(lore)?})))
    }

    fn msg(&self, params: &Value) -> Result<Value, String> {
        let target = params["target"].as_str().ok_or("invalid peer target")?;
        let body = params["text"].as_str().ok_or("invalid peer message")?;
        if target.is_empty()
            || target.len() > 128
            || !target
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-')
            || body.trim().is_empty()
            || body.chars().count() > delivery::MAX_BODY_CHARS
        {
            return Err("invalid peer target or message".into());
        }
        let (roster, clean_body, clean_title, clean_scope) = self.with_lore(|lore| {
            let roster = self.roster(lore)?;
            let clean_body = lore
                .scrub(body)
                .map_err(|_| "LORE scrub unavailable".to_owned())?;
            let clean_title = lore
                .scrub(&self.title)
                .map_err(|_| "LORE scrub unavailable".to_owned())?;
            let clean_scope = lore
                .scrub(&self.scope)
                .map_err(|_| "LORE scrub unavailable".to_owned())?;
            Ok((roster, clean_body, clean_title, clean_scope))
        })?;
        if clean_body.trim().is_empty() {
            return Err("message empty after scrubbing".into());
        }
        if clean_body.chars().count() > delivery::MAX_BODY_CHARS {
            return Err("message too long after scrubbing".into());
        }
        let matches: Vec<_> = roster
            .iter()
            .filter(|p| p.session_id.starts_with(target))
            .collect();
        let peer = match matches.as_slice() {
            [peer] => *peer,
            [] => return Err("no live same-scope peer matches target".into()),
            _ => return Err("peer target is ambiguous".into()),
        };
        let registry = Registry::open(&self.runtime).map_err(|_| "peer registry unavailable")?;
        let mut peer_info = registry
            .scoped(
                &self.scope,
                Some(&self.session_id),
                &|s: &str| s.to_owned(),
                true,
            )
            .map_err(|_| "peer registry unavailable")?
            .into_iter()
            .find(|p| p.session_id == peer.session_id)
            .ok_or("peer is no longer live")?;
        self.with_lore(|lore| {
            let clean = |lore: &mut LoreClient, value: &str| {
                lore.scrub(value)
                    .map_err(|_| "LORE scrub unavailable".to_owned())
            };
            peer_info.title = clean(lore, &peer_info.title)?;
            peer_info.cwd = clean(lore, &peer_info.cwd)?;
            peer_info.socket_path = clean(lore, &peer_info.socket_path)?;
            peer_info.started_at = clean(lore, &peer_info.started_at)?;
            peer_info.heartbeat_at = clean(lore, &peer_info.heartbeat_at)?;
            peer_info.repo_root = peer_info
                .repo_root
                .as_deref()
                .map(|s| clean(lore, s))
                .transpose()?;
            peer_info.daemon_socket = peer_info
                .daemon_socket
                .as_deref()
                .map(|s| clean(lore, s))
                .transpose()?;
            peer_info.provider = peer_info
                .provider
                .as_deref()
                .map(|s| clean(lore, s))
                .transpose()?;
            peer_info.model = peer_info
                .model
                .as_deref()
                .map(|s| clean(lore, s))
                .transpose()?;
            peer_info.engine = peer_info
                .engine
                .as_deref()
                .map(|s| clean(lore, s))
                .transpose()?;
            peer_info.parent_session_id = peer_info
                .parent_session_id
                .as_deref()
                .map(|s| clean(lore, s))
                .transpose()?;
            Ok(())
        })?;
        let sender = PeerRecord {
            session_id: self.session_id.clone(),
            pid: std::process::id() as i32,
            socket_path: String::new(),
            cwd: self.scope.clone(),
            repo_root: None,
            title: clean_title,
            started_at: now(),
            heartbeat_at: now(),
            daemon_socket: None,
            clients: None,
            usage_tokens: None,
            provider: None,
            model: None,
            engine: None,
            parent_session_id: None,
        };
        let scope = self.scope.clone();
        let raw_body = body.to_owned();
        let result = delivery::deliver(
            &registry,
            &sender,
            std::slice::from_ref(&peer.session_id),
            body,
            "direct",
            None,
            &self.limiter,
            &self.ledger,
            &move |text: &str| {
                if text == raw_body {
                    clean_body.clone()
                } else if text == scope {
                    clean_scope.clone()
                } else {
                    text.to_owned()
                }
            },
        )
        .map_err(|_| "peer delivery failed".to_owned())?;
        let _ = self.events.try_send(json!({"type":"peer_sent","data":{
            "to":result.delivered,"kind":"direct",
            "message_id":result.record.as_ref().map(|r| r.id.as_str())}}));
        Ok(json!({"peer":peer_info,
            "delivered_to":result.delivered,"failed":result.failed,
            "message_id":result.record.as_ref().map(|r| r.id.as_str()),
            "ledger_error":result.ledger_error}))
    }

    pub fn inbound_event(&self, frame: PeerFrame) -> Result<Value, String> {
        if frame.from_id == self.session_id {
            return Err("self peer frame".into());
        }
        let roster = self.with_lore(|lore| self.roster(lore))?;
        if !roster.iter().any(|p| p.session_id == frame.from_id) {
            return Err("sender is not a live same-scope peer".into());
        }
        {
            let mut limiters = self.inbound_limiters
                .lock()
                .map_err(|_| "peer rate limiter unavailable")?;
            limiters.retain(|id, _| roster.iter().any(|peer| peer.session_id == *id));
            limiters.entry(frame.from_id.clone())
                .or_insert_with(|| RateLimiter::new(SendLimits::default()))
                .charge(None, 1)
                .map_err(|_| "peer receive limit".to_owned())?;
        }
        let (title, body, repo, sent_at, kind) = self.with_lore(|lore| {
            let clean = |lore: &mut LoreClient, text: &str| {
                lore.scrub(text)
                    .map_err(|_| "LORE scrub unavailable".to_owned())
            };
            Ok((
                clean(lore, &frame.from_title)?,
                clean(lore, &frame.body)?,
                frame
                    .from_repo
                    .as_deref()
                    .map(|s| clean(lore, s))
                    .transpose()?,
                clean(lore, &frame.sent_at)?,
                frame.kind.as_deref().map(|s| clean(lore, s)).transpose()?,
            ))
        })?;
        Ok(
            json!({"type":"peer_message","data":{"from_id":frame.from_id,
            "from_title":title,"body":body,"from_repo":repo,"sent_at":sent_at,"kind":kind}}),
        )
    }
}

impl Host for PeerHost {
    fn initial_model(&self) -> Option<String> { self.inner.initial_model() }
    fn initial_permission_mode(&self) -> String { self.inner.initial_permission_mode() }
    fn transcript_snapshot(&self) -> io::Result<Option<(PathBuf, u64)>> {
        self.inner.transcript_snapshot()
    }
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        self.inner.prompt(text, emit);
    }
    fn public_prompt(&self, text: &str) -> Result<String, String> {
        self.inner.public_prompt(text)
    }
    fn call(&self, method: &str, params: &Value) -> Result<Value, String> {
        match method {
            "peers" => self.peers(),
            "msg" => self.msg(params),
            _ => self.inner.call(method, params),
        }
    }
}
