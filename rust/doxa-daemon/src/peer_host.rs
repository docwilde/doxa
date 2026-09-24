//! Read-only peers RPC wrapper. Engine hosts keep ownership of prompts and
//! other calls; this layer exposes only a scrubbed, same-scope roster.

use doxa_lore::LoreClient;
use doxa_peers::{presence, scope_for_cwd};
use doxa_runtime::Host;
use serde_json::{json, Value};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

pub struct PeerHost {
    inner: Arc<dyn Host>,
    lore: Mutex<Option<LoreClient>>,
    runtime: PathBuf,
    scope: String,
    session_id: String,
}

impl PeerHost {
    pub fn new(
        inner: Arc<dyn Host>,
        runtime: PathBuf,
        cwd: &Path,
        session_id: String,
        lore_python: Option<&Path>,
    ) -> io::Result<Self> {
        let scope = scope_for_cwd(cwd)?;
        let lore =
            lore_python.and_then(|python| LoreClient::spawn(python, Duration::from_secs(5)).ok());
        Ok(Self {
            inner,
            lore: Mutex::new(lore),
            runtime,
            scope,
            session_id,
        })
    }

    fn peers(&self) -> Result<Value, String> {
        let mut guard = self
            .lore
            .lock()
            .map_err(|_| "LORE scrub unavailable".to_owned())?;
        let lore = guard
            .as_mut()
            .ok_or_else(|| "LORE scrub unavailable".to_owned())?;
        lore.scrub("DOXA peer scrub preflight")
            .map_err(|_| "LORE scrub unavailable".to_owned())?;
        let rows =
            presence::list_scoped_readonly(&self.runtime, &self.scope, &self.session_id, |text| {
                lore.scrub(text)
                    .map_err(|_| io::Error::other("LORE scrub unavailable"))
            })
            .map_err(|_| "peer discovery unavailable or LORE scrub failed".to_owned())?;
        Ok(json!({"peers":rows}))
    }
}

impl Host for PeerHost {
    fn prompt(&self, text: &str, emit: &mut dyn FnMut(Value)) {
        self.inner.prompt(text, emit);
    }
    fn public_prompt(&self, text: &str) -> Result<String, String> {
        self.inner.public_prompt(text)
    }
    fn call(&self, method: &str, params: &Value) -> Result<Value, String> {
        if method == "peers" {
            self.peers()
        } else {
            self.inner.call(method, params)
        }
    }
}
