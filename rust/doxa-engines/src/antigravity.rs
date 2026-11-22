//! Antigravity's ACP v1 stdio transport and account-advertised configuration.
use serde_json::{json, Value};
use std::{
    env,
    io::{self, BufRead, BufReader, Write},
    os::{fd::AsRawFd, unix::process::CommandExt},
    path::Path,
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::atomic::{AtomicBool, Ordering},
    thread,
    time::{Duration, Instant},
};

const MAX_FRAME: usize = 1024 * 1024;
const REQUEST_TIMEOUT: Duration = Duration::from_secs(8);

fn error(message: &'static str) -> io::Error {
    io::Error::other(message)
}

pub struct Acp {
    child: Child,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
    line: Vec<u8>,
    next_id: u64,
    resume: bool,
    close: bool,
}

impl Acp {
    pub fn connect(executable: &Path, cwd: &Path, cancel: &AtomicBool) -> io::Result<Self> {
        if !cfg!(target_os = "linux") {
            return Err(error("Antigravity ACP currently requires native Linux"));
        }
        let mut cmd = Command::new(executable);
        cmd.args([
            "acp",
            "--sandbox",
            "--disable-telemetry",
            "--include-default-customizations=false",
        ])
        .current_dir(cwd)
        .env_clear()
        .env("LORE_TOOL_EVIDENCE", "0")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0);
        #[cfg(target_os = "linux")]
        {
            let parent = std::process::id() as libc::pid_t;
            unsafe {
                cmd.pre_exec(move || {
                    if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL, 0, 0, 0) < 0 {
                        return Err(io::Error::last_os_error());
                    }
                    if libc::getppid() != parent {
                        return Err(io::Error::from_raw_os_error(libc::ESRCH));
                    }
                    Ok(())
                });
            }
        }
        for key in [
            "HOME",
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TMPDIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
            "XDG_RUNTIME_DIR",
            "DBUS_SESSION_BUS_ADDRESS",
        ] {
            if let Some(value) = env::var_os(key) {
                cmd.env(key, value);
            }
        }
        let mut child = cmd.spawn()?;
        let input = child.stdin.take().expect("piped ACP stdin");
        let output = child.stdout.take().expect("piped ACP stdout");
        let mut acp = Self {
            child,
            input,
            output: BufReader::new(output),
            line: Vec::new(),
            next_id: 1,
            resume: false,
            close: false,
        };
        for fd in [acp.input.as_raw_fd(), acp.output.get_ref().as_raw_fd()] {
            let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
            if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0
            {
                return Err(io::Error::last_os_error());
            }
        }
        let reply = acp.request(
            "initialize",
            json!({
                "protocolVersion": 1, "clientCapabilities": {},
                "clientInfo": {"name": "DOXA", "version": env!("CARGO_PKG_VERSION")}
            }),
            cancel,
        )?;
        if reply["protocolVersion"] != 1 || reply["agentInfo"]["name"] != "Antigravity ACP Server" {
            return Err(error("agy did not negotiate Antigravity ACP v1"));
        }
        let capabilities = &reply["agentCapabilities"]["sessionCapabilities"];
        acp.resume = capabilities["resume"].is_object();
        acp.close = capabilities["close"].is_object();
        Ok(acp)
    }

    pub fn session(
        &mut self,
        cwd: &Path,
        resume: Option<&str>,
        cancel: &AtomicBool,
    ) -> io::Result<(String, Config)> {
        let mut params = json!({"cwd": cwd, "mcpServers": []});
        let method = if let Some(id) = resume {
            if !self.resume {
                return Err(error("agy does not advertise ACP session resume"));
            }
            params["sessionId"] = json!(id);
            "session/resume"
        } else {
            "session/new"
        };
        let result = self.request(method, params, cancel)?;
        let id = match resume {
            Some(id) if result.get("sessionId").is_none_or(|value| value == id) => id,
            Some(_) => return Err(error("agy returned a different resumed session")),
            None => result["sessionId"]
                .as_str()
                .filter(|id| valid_session_id(id))
                .ok_or_else(|| error("agy returned an invalid session ID"))?,
        };
        Ok((id.to_owned(), Config::parse(&result)?))
    }

    pub fn close_session(&mut self, id: &str, cancel: &AtomicBool) -> io::Result<()> {
        if self.close {
            self.request("session/close", json!({"sessionId": id}), cancel)?;
        }
        Ok(())
    }

    fn request(&mut self, method: &str, params: Value, cancel: &AtomicBool) -> io::Result<Value> {
        if cancel.load(Ordering::Acquire) {
            return Err(error("Antigravity ACP request cancelled"));
        }
        let id = self.send_request(method, params)?;
        let deadline = Instant::now() + REQUEST_TIMEOUT;
        while Instant::now() < deadline && !cancel.load(Ordering::Acquire) {
            let Some(frame) = self.receive(Duration::from_millis(30))? else {
                continue;
            };
            if let Some(request_id) = callback_id(&frame) {
                self.send(json!({"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": "Request unavailable during ACP setup"}}))?;
            } else if frame["id"] == id && frame.get("method").is_none() {
                return response(frame);
            }
        }
        Err(error("Antigravity ACP setup timed out or was cancelled"))
    }

    pub fn send_request(&mut self, method: &str, params: Value) -> io::Result<u64> {
        let id = self.next_id;
        self.next_id += 1;
        self.send(json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}))?;
        Ok(id)
    }

    pub fn send(&mut self, frame: Value) -> io::Result<()> {
        let mut bytes = serde_json::to_vec(&frame)?;
        bytes.push(b'\n');
        if bytes.len() > MAX_FRAME {
            return Err(error("Antigravity ACP outgoing frame exceeds limit"));
        }
        let mut remaining = bytes.as_slice();
        let deadline = Instant::now() + Duration::from_secs(1);
        while !remaining.is_empty() {
            match self.input.write(remaining) {
                Ok(0) => return Err(error("Antigravity ACP stdin closed")),
                Ok(n) => remaining = &remaining[n..],
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(5));
                }
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) => return Err(e),
            }
            if Instant::now() >= deadline && !remaining.is_empty() {
                return Err(error("Antigravity ACP stdin timed out"));
            }
        }
        Ok(())
    }

    pub fn receive(&mut self, wait: Duration) -> io::Result<Option<Value>> {
        let deadline = Instant::now() + wait;
        loop {
            match self.output.fill_buf() {
                Ok([]) => return Err(error("Antigravity ACP stdout closed")),
                Ok(bytes) => {
                    let newline = bytes.iter().position(|byte| *byte == b'\n');
                    let count = newline.map_or(bytes.len(), |index| index + 1);
                    if self.line.len() + count > MAX_FRAME {
                        return Err(error("Antigravity ACP incoming frame exceeds limit"));
                    }
                    self.line.extend_from_slice(&bytes[..count]);
                    self.output.consume(count);
                    if newline.is_some() {
                        let frame = serde_json::from_slice::<Value>(&self.line).ok();
                        self.line.clear();
                        return frame
                            .filter(|value| value["jsonrpc"] == "2.0")
                            .map(Some)
                            .ok_or_else(|| error("Invalid Antigravity ACP frame"));
                    }
                    continue;
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    thread::sleep(
                        Duration::from_millis(5)
                            .min(deadline.saturating_duration_since(Instant::now())),
                    );
                }
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) => return Err(e),
            }
            if Instant::now() >= deadline {
                return Ok(None);
            }
        }
    }
}

impl Drop for Acp {
    fn drop(&mut self) {
        // The child stays unreaped until after signaling its process group.
        unsafe {
            libc::kill(-(self.child.id() as i32), libc::SIGKILL);
        }
        let _ = self.child.wait();
    }
}

pub fn callback_id(frame: &Value) -> Option<&Value> {
    frame["method"].as_str()?;
    frame
        .get("id")
        .filter(|id| id.is_i64() || id.as_str().is_some_and(|id| id.len() <= 256))
}

pub fn response(frame: Value) -> io::Result<Value> {
    let Value::Object(mut fields) = frame else {
        return Err(error("Invalid Antigravity ACP response"));
    };
    match (fields.remove("result"), fields.remove("error")) {
        (Some(result), None) if result.is_object() => Ok(result),
        (None, Some(failure))
            if failure["code"] == -32000 && failure["message"] == "Authentication required" =>
        {
            Err(error("Antigravity sign-in required; run agy acp --login"))
        }
        _ => Err(error("Antigravity rejected the ACP request")),
    }
}

pub fn valid_session_id(id: &str) -> bool {
    !id.is_empty()
        && !id.starts_with('-')
        && id.len() <= 128
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

#[derive(Clone, Default)]
pub struct Config {
    model: Option<Select>,
    effort: Option<Select>,
}

#[derive(Clone)]
struct Select {
    id: String,
    current: String,
    choices: Vec<(String, String)>,
}

fn identifier(value: &Value) -> Option<&str> {
    value
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 128 && !s.chars().any(char::is_control))
}

fn choices(rows: &Value, id_key: &str) -> io::Result<Vec<(String, String)>> {
    let rows = rows
        .as_array()
        .filter(|rows| rows.len() <= 256)
        .ok_or_else(|| error("Invalid Antigravity ACP catalog"))?;
    let mut choices = Vec::new();
    for row in rows {
        if let Some(group) = row.get("options") {
            // ACP permits one level of grouping.
            let group = group
                .as_array()
                .ok_or_else(|| error("Invalid Antigravity ACP catalog group"))?;
            for option in group {
                add_choice(&mut choices, option, id_key)?;
            }
        } else {
            add_choice(&mut choices, row, id_key)?;
        }
    }
    Ok(choices)
}

fn add_choice(choices: &mut Vec<(String, String)>, row: &Value, id_key: &str) -> io::Result<()> {
    let id = identifier(&row[id_key]).ok_or_else(|| error("Invalid Antigravity ACP option ID"))?;
    let name = identifier(&row["name"]).unwrap_or(id);
    if choices.len() >= 256 || choices.iter().any(|(value, _)| value == id) {
        return Err(error("Ambiguous Antigravity ACP catalog"));
    }
    choices.push((id.to_owned(), name.to_owned()));
    Ok(())
}

impl Config {
    pub fn parse(result: &Value) -> io::Result<Self> {
        let mut config = Self::default();
        let reject_unattended = |value: &Value| -> io::Result<()> {
            if value.as_str().is_some_and(|id| {
                matches!(
                    id.to_ascii_lowercase().as_str(),
                    "auto"
                        | "accept-edits"
                        | "turbo"
                        | "bypasspermissions"
                        | "dangerously-skip-permissions"
                )
            }) {
                return Err(error(
                    "Choose a review-based permission mode in Antigravity before using DOXA",
                ));
            }
            Ok(())
        };
        reject_unattended(&result["modes"]["currentModeId"])?;
        if let Some(options) = result["configOptions"].as_array() {
            for option in options {
                let target = match option["category"].as_str() {
                    Some("model") => &mut config.model,
                    Some("thought_level") => &mut config.effort,
                    Some("mode") => {
                        reject_unattended(&option["currentValue"])?;
                        continue;
                    }
                    _ => continue,
                };
                if target.is_some() || option["type"] != "select" {
                    return Err(error("Ambiguous Antigravity ACP configuration"));
                }
                let id = identifier(&option["id"])
                    .ok_or_else(|| error("Invalid Antigravity ACP config ID"))?;
                let current = identifier(&option["currentValue"])
                    .ok_or_else(|| error("Invalid Antigravity ACP config value"))?;
                *target = Some(Select {
                    id: id.to_owned(),
                    current: current.to_owned(),
                    choices: choices(&option["options"], "value")?,
                });
            }
        }
        if config.model.is_none() && result["models"].is_object() {
            let models = &result["models"];
            let current = identifier(&models["currentModelId"])
                .ok_or_else(|| error("Invalid Antigravity ACP model ID"))?;
            config.model = Some(Select {
                id: String::new(),
                current: current.to_owned(),
                choices: choices(&models["availableModels"], "modelId")?,
            });
        }
        Ok(config)
    }

    pub fn model(&self) -> Option<&str> {
        self.model.as_ref().map(|option| option.current.as_str())
    }

    pub fn effort(&self) -> Option<&str> {
        self.effort.as_ref().map(|option| option.current.as_str())
    }

    pub fn can_set_model(&self) -> bool {
        self.model.is_some()
    }

    pub fn catalog(&self) -> Value {
        let capabilities: Vec<_> = self
            .model
            .iter()
            .flat_map(|option| &option.choices)
            .map(|(id, label)| {
                let current = Some(id.as_str()) == self.model();
                let levels = self
                    .effort
                    .iter()
                    .filter(|_| current)
                    .flat_map(|option| &option.choices)
                    .map(|(id, _)| id)
                    .collect::<Vec<_>>();
                json!({"model": id, "name": label, "is_default": current, "efforts": levels})
            })
            .collect();
        json!({"models": capabilities.iter().map(|row| &row["model"]).collect::<Vec<_>>(),
            "capabilities": capabilities, "note": "Live model catalog from Antigravity ACP"})
    }

    pub fn selection(&self, effort: bool, requested: &str) -> io::Result<String> {
        let option = if effort {
            self.effort.as_ref()
        } else {
            self.model.as_ref()
        };
        let option = option
            .ok_or_else(|| error("Antigravity did not advertise this model or effort selector"))?;
        option.choices.iter().find(|(id, _)| id == requested).map(|(id, _)| id).or_else(|| {
            let mut labels = option.choices.iter().filter(|(_, name)| name == requested);
            let (first, _) = labels.next()?;
            labels.next().is_none().then_some(first)
        }).cloned().ok_or_else(|| error("Requested model or effort is unavailable in this Antigravity account's ACP catalog"))
    }

    pub fn select(
        &mut self,
        acp: &mut Acp,
        session: &str,
        effort: bool,
        requested: &str,
        cancel: &AtomicBool,
    ) -> io::Result<String> {
        let value = self.selection(effort, requested)?;
        let option = if effort {
            self.effort.as_ref()
        } else {
            self.model.as_ref()
        }
        .expect("validated selector");
        if option.current == value {
            return Ok(value);
        }
        if option.id.is_empty() {
            // Earlier ACP servers acknowledge the exact model via this RPC.
            let reply = acp.request(
                "session/set_model",
                json!({"sessionId": session, "modelId": value}),
                cancel,
            )?;
            if reply
                .get("modelId")
                .is_some_and(|id| id.as_str() != Some(value.as_str()))
            {
                return Err(error("Antigravity did not confirm the requested model"));
            }
            self.model.as_mut().expect("legacy model selector").current = value.clone();
        } else {
            let reply = acp.request(
                "session/set_config_option",
                json!({
                    "sessionId": session, "configId": option.id, "value": value
                }),
                cancel,
            )?;
            let config = Self::parse(&reply)?;
            let actual = if effort {
                config.effort()
            } else {
                config.model()
            };
            if actual != Some(value.as_str()) || effort && config.model() != self.model() {
                return Err(error(
                    "Antigravity did not confirm the requested model or effort",
                ));
            }
            *self = config;
        }
        Ok(value)
    }
}

pub fn discover(executable: &Path, cwd: &Path, cancel: &AtomicBool) -> io::Result<Config> {
    let mut acp = Acp::connect(executable, cwd, cancel)?;
    let (session, config) = acp.session(cwd, None, cancel)?;
    acp.close_session(&session, cancel)?;
    Ok(config)
}
