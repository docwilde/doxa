//! Trusted host controller for per-session rootless Docker workers.
//! Only explicit owner configuration selects a policy. No worker receives a
//! Docker socket, host home, main checkout, shared Git store or DOXA state.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    ffi::OsString, fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    os::unix::fs::{DirBuilderExt, FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Component, Path, PathBuf},
    process::{Command, Stdio},
    time::{Duration, Instant},
};
pub mod broker;
pub mod egress;
pub mod hardened;
pub mod workspace;
pub mod test_runner;
pub mod migration;
pub mod cgroup;
mod disk;
mod source_git;
pub use source_git::staged_diff;
pub use disk::{check_disk_budget, DiskSnapshot};

pub const ACTIVE_MANIFEST: &str = "DOXA_ISOLATION_MANIFEST";
pub const SESSION_MANIFEST: &str = "DOXA_SESSION_MANIFEST";
const RESUME_ROLLOUT: &str = "DOXA_CODEX_RESUME_ROLLOUT";
const MAX_MANIFEST: u64 = 64 * 1024;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Profile { #[default] Native, DockerOpen, DockerOffline }
impl Profile {
    pub fn parse(value: &str) -> io::Result<Self> {
        match value { "native" => Ok(Self::Native), "docker-open" => Ok(Self::DockerOpen),
            "docker-offline" => Ok(Self::DockerOffline),
            "docker-hardened" => Err(error("docker-hardened is unavailable: per-session kernel hard-quota, restart and provider-egress proofs are required")),
            _ => Err(error("isolation must be native, docker-open or docker-offline")) }
    }
    pub fn key(self) -> &'static str { match self { Self::Native => "native", Self::DockerOpen => "docker-open", Self::DockerOffline => "docker-offline" } }
    pub fn label(self) -> &'static str { match self { Self::Native => "native", Self::DockerOpen => "docker · open egress", Self::DockerOffline => "docker · no network" } }
    pub fn docker(self) -> bool { self != Self::Native }
    fn network(self) -> &'static str { if self == Self::DockerOffline { "none" } else { "bridge" } }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Policy {
    pub image: String, pub docker_host: String, pub memory_bytes: u64, pub cpus: f64, pub pids: u32,
    // Omitted by beta.10 manifests. Keeping absent fields out of JSON also
    // preserves their saved policy hash; new sessions always record both.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub disk_soft_limit_bytes: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub disk_free_floor_bytes: Option<u64>,
}
impl Policy {
    pub fn configured(home: &Path) -> io::Result<Self> {
        let cfg: toml::Value = match fs::read_to_string(home.join("config.toml")) {
            Ok(bytes) => toml::from_str(&bytes).map_err(|_| error("invalid DOXA owner configuration"))?,
            Err(e) if e.kind() == io::ErrorKind::NotFound => toml::Value::Table(Default::default()),
            Err(e) => return Err(e),
        };
        let string = |key: &str, env: &str| std::env::var(env).ok().or_else(|| cfg.get(key).and_then(toml::Value::as_str).map(str::to_owned));
        let integer = |key: &str, env: &str, default: u64| -> io::Result<u64> {
            match string(key, env) { Some(value) => value.parse().map_err(|_| error(format!("invalid {key}"))),
                None => match cfg.get(key) { Some(value) => value.as_integer().and_then(|n| u64::try_from(n).ok()).ok_or_else(|| error(format!("invalid {key}"))), None => Ok(default) } }
        };
        let cpus = match string("docker_cpus", "DOXA_DOCKER_CPUS") {
            Some(value) => value.parse().map_err(|_| error("invalid docker_cpus"))?,
            None => cfg.get("docker_cpus").and_then(|n| n.as_float().or_else(|| n.as_integer().map(|n| n as f64))).unwrap_or(2.0),
        };
        let policy = Self {
            image: string("docker_image", "DOXA_DOCKER_IMAGE").ok_or_else(|| error("Docker isolation requires docker_image pinned as NAME@sha256:DIGEST; build the reviewed worker image first"))?,
            docker_host: string("docker_host", "DOXA_DOCKER_HOST").or_else(|| std::env::var("DOCKER_HOST").ok())
                .unwrap_or_else(|| format!("unix:///run/user/{}/docker.sock", unsafe { libc::geteuid() })),
            memory_bytes: integer("docker_memory_bytes", "DOXA_DOCKER_MEMORY_BYTES", 4 * 1024 * 1024 * 1024)?,
            cpus, pids: u32::try_from(integer("docker_pids", "DOXA_DOCKER_PIDS", 256)?).map_err(|_| error("invalid docker_pids"))?,
            disk_soft_limit_bytes: Some(integer("docker_disk_soft_limit_bytes", "DOXA_DOCKER_DISK_SOFT_LIMIT_BYTES", 20 * 1024 * 1024 * 1024)?),
            disk_free_floor_bytes: Some(integer("docker_disk_free_floor_bytes", "DOXA_DOCKER_DISK_FREE_FLOOR_BYTES", 2 * 1024 * 1024 * 1024)?),
        };
        policy.validate()?; Ok(policy)
    }
    pub fn validate(&self) -> io::Result<()> {
        let (name, digest) = if let Some(digest)=self.image.strip_prefix("sha256:"){("sha256",digest)}
            else{self.image.split_once("@sha256:").ok_or_else(|| error("Docker image must be pinned by sha256 digest"))?};
        if name.is_empty() || name.starts_with('-') || !name.bytes().all(|b| b.is_ascii_alphanumeric() || b"./:_-".contains(&b))
            || digest.len() != 64 || !digest.bytes().all(|b| b.is_ascii_hexdigit()) { return Err(error("invalid pinned Docker image")); }
        let socket = self.docker_host.strip_prefix("unix://").filter(|s| Path::new(s).is_absolute())
            .ok_or_else(|| error("Docker isolation accepts only a local Unix socket Engine"))?;
        if Path::new(socket) == Path::new("/var/run/docker.sock") || Path::new(socket) == Path::new("/run/docker.sock") { return Err(error("rootful Docker socket is forbidden")); }
        if self.memory_bytes < 128 * 1024 * 1024 || self.memory_bytes > 1024 * 1024 * 1024 * 1024
            || !self.cpus.is_finite() || !(0.25..=256.0).contains(&self.cpus) || !(16..=65536).contains(&self.pids) {
            return Err(error("Docker memory/CPU/PID limits are outside supported finite bounds"));
        }
        if self.disk_soft_limit_bytes.is_some_and(|n| !(128 * 1024 * 1024..=1024 * 1024 * 1024 * 1024).contains(&n))
            || self.disk_free_floor_bytes.is_some_and(|n| !(512 * 1024 * 1024..=1024 * 1024 * 1024 * 1024).contains(&n)) {
            return Err(error("Docker disk soft ceiling/floor are outside supported bounds"));
        }
        Ok(())
    }
    fn hash(&self, profile: Profile) -> String {
        format!("{:x}", Sha256::digest(serde_json::to_vec(&(self, profile)).expect("policy serialization")))
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Manifest {
    pub version: u32, pub session_id: String, pub profile: Profile, pub policy: Option<Policy>,
    pub policy_hash: String, pub source: PathBuf, pub checkout: PathBuf,
    #[serde(default)] pub context_cwd: Option<PathBuf>,
    #[serde(default)] pub provider_rollout: Option<PathBuf>,
    pub creation_policy_hash: String,
    pub checkout_device: u64, pub checkout_inode: u64, pub base_sha: String, pub branch: String,
    pub private_home: PathBuf, pub cache: PathBuf, pub broker: PathBuf,
    pub container_id: Option<String>, pub nonce: String, pub state: String,
}
impl Manifest {
    pub fn status(&self) -> Value {
        let mut result = json!({"profile":self.profile.key(),"label":self.profile.label(),"state":self.state,
            "engine":if self.profile.docker() {"local rootless Docker"} else {"native"},
            "network":if self.profile.docker(){self.profile.network()}else{"host"},
            "image":self.policy.as_ref().map(|p|p.image.as_str()),
            "memory_bytes":self.policy.as_ref().map(|p|p.memory_bytes),
            "cpus":self.policy.as_ref().map(|p|p.cpus),"pids":self.policy.as_ref().map(|p|p.pids),
            "disk_limit":"monitored turn gate; no hard filesystem quota","checkout":self.checkout,
            "disk_soft_limit_bytes":self.policy.as_ref().and_then(|p|p.disk_soft_limit_bytes),
            "disk_free_floor_bytes":self.policy.as_ref().map(|p|p.disk_free_floor_bytes.unwrap_or(2*1024*1024*1024)),
            "mounts":if self.profile.docker(){vec!["independent checkout","private home","private cache","session hook broker"]}else{vec![]},
            "credential_exposure":if self.profile.docker(){"only selected provider auth copied to private session home; visible to worker tools"}else{"native provider environment"},
            "can_set_isolation":true});
        if self.profile.docker() {
            match disk::sample(self) {
                Ok(sample) => {
                    result["disk_usage_bytes"] = json!(sample.usage_bytes);
                    result["disk_free_bytes"] = json!(sample.free_bytes);
                    if let Err(err) = disk::enforce(sample, self.policy.as_ref().unwrap()) { result["disk_budget_error"] = json!(err.to_string()); }
                },
                Err(err) => result["disk_monitor_error"] = json!(err.to_string()),
            }
        }
        result
    }
}
pub fn error(message: impl Into<String>) -> io::Error { io::Error::new(io::ErrorKind::PermissionDenied, message.into()) }
pub fn home() -> io::Result<PathBuf> {
    std::env::var_os("DOXA_HOME").filter(|v| !v.is_empty()).map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|v| PathBuf::from(v).join(".doxa")))
        .filter(|p| p.is_absolute()).ok_or_else(|| error("DOXA home must be absolute"))
}
pub fn configured_profile(home: &Path) -> io::Result<Profile> {
    if let Ok(value) = std::env::var("DOXA_SESSION_ISOLATION") { return Profile::parse(&value); }
    match fs::read_to_string(home.join("config.toml")) {
        Ok(value) => {
            let cfg: toml::Value = toml::from_str(&value).map_err(|_| error("invalid owner configuration"))?;
            match cfg.get("session_isolation") { Some(value) => Profile::parse(value.as_str().ok_or_else(|| error("session_isolation must be a string"))?), None => Ok(Profile::Native) }
        },
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(Profile::Native), Err(e) => Err(e),
    }
}
fn valid_id(value: &str) -> bool {
    !value.is_empty() && value.len() <= 128 && value.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}
pub fn manifest_path(home: &Path, id: &str) -> io::Result<PathBuf> {
    if !valid_id(id) { return Err(error("invalid isolation session identity")); }
    Ok(home.join("isolation").join(id).join("manifest.json"))
}
fn private_directory(path: &Path, create: bool) -> io::Result<()> {
    if !path.is_absolute() || path.components().any(|c| matches!(c, Component::ParentDir)) { return Err(error("unsafe isolation path")); }
    if create { fs::DirBuilder::new().recursive(true).mode(0o700).create(path)?; }
    let mut current = PathBuf::from("/");
    for component in path.components().skip(1) {
        current.push(component);
        let meta = fs::symlink_metadata(&current)?;
        if !meta.is_dir() || meta.file_type().is_symlink() { return Err(error("isolation paths must have no symlink components")); }
    }
    let meta = fs::symlink_metadata(path)?;
    if meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 { return Err(error("isolation directory must be private and owned")); }
    Ok(())
}
pub fn read_manifest(path: &Path) -> io::Result<Manifest> {
    private_directory(path.parent().ok_or_else(|| error("manifest has no parent"))?, false)?;
    let file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file() || meta.nlink() != 1 || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 || meta.len() > MAX_MANIFEST { return Err(error("unsafe isolation manifest")); }
    let mut bytes = Vec::new(); file.take(MAX_MANIFEST + 1).read_to_end(&mut bytes)?;
    let manifest: Manifest = serde_json::from_slice(&bytes)?;
    if manifest.version != 1 || !valid_id(&manifest.session_id)
        || path.parent().and_then(Path::file_name).is_none_or(|name| name != manifest.session_id.as_str()) { return Err(error("isolation manifest identity mismatch")); }
    if manifest.profile.docker() || manifest.context_cwd.is_some() {
        let root = path.parent().unwrap();
        if manifest.checkout != root.join("checkout") || manifest.private_home != root.join("home")
            || manifest.cache != root.join("cache") || manifest.broker != root.join("broker") { return Err(error("manifest mount escapes its private session root")); }
        for directory in [&manifest.checkout, &manifest.private_home, &manifest.cache, &manifest.broker] { private_directory(directory, false)?; }
        let meta = fs::metadata(&manifest.checkout)?;
        if (meta.dev(), meta.ino()) != (manifest.checkout_device, manifest.checkout_inode) { return Err(error("isolated checkout identity changed")); }
        if manifest.profile.docker() {
            let policy = manifest.policy.as_ref().ok_or_else(|| error("Docker manifest has no policy"))?; policy.validate()?;
            if policy.hash(manifest.profile) != manifest.policy_hash { return Err(error("isolation policy hash changed")); }
            if ![policy.hash(Profile::DockerOpen),policy.hash(Profile::DockerOffline)].contains(&manifest.creation_policy_hash) {
                return Err(error("invalid immutable creation policy hash"));
            }
        }
        if manifest.context_cwd.as_ref().is_some_and(|path|!path.is_absolute()||path.components().any(|c|matches!(c,Component::ParentDir))){return Err(error("invalid logical session cwd"));}
        if manifest.provider_rollout.as_ref().is_some_and(|path|!path.starts_with(manifest.private_home.join("codex/sessions"))){return Err(error("provider rollout escapes private session home"));}
    }
    Ok(manifest)
}
fn write_manifest(path: &Path, manifest: &Manifest) -> io::Result<()> {
    private_directory(path.parent().ok_or_else(|| error("manifest has no parent"))?, false)?;
    let mut file = tempfile::NamedTempFile::new_in(path.parent().unwrap())?;
    file.as_file().set_permissions(fs::Permissions::from_mode(0o600))?;
    file.write_all(&serde_json::to_vec_pretty(manifest)?)?; file.as_file().sync_all()?;
    file.persist(path).map_err(|e| e.error)?; File::open(path.parent().unwrap())?.sync_all()
}
fn nonce() -> io::Result<String> {
    let mut bytes = [0; 24]; File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
fn run(mut command: Command) -> io::Result<Vec<u8>> {
    command.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command.spawn()?;
    let stdout = child.stdout.take().unwrap(); let stderr = child.stderr.take().unwrap();
    let out = std::thread::spawn(move || { let mut bytes = Vec::new(); let result = stdout.take(1024 * 1024 + 1).read_to_end(&mut bytes); (result, bytes) });
    let err = std::thread::spawn(move || { let mut bytes = Vec::new(); let result = stderr.take(8193).read_to_end(&mut bytes); (result, bytes) });
    let deadline = Instant::now() + Duration::from_secs(45);
    let status = loop {
        if let Some(status) = child.try_wait()? { break status; }
        if Instant::now() >= deadline { let _ = child.kill(); let _ = child.wait(); return Err(error("isolation operation timed out; native fallback refused")); }
        std::thread::sleep(Duration::from_millis(10));
    };
    let (result, bytes) = out.join().map_err(|_| error("isolation output reader failed"))?; result?;
    let (_, stderr) = err.join().map_err(|_| error("isolation error reader failed"))?;
    if !status.success() { return Err(error(format!("isolation operation failed: {}", String::from_utf8_lossy(&stderr).chars().take(1000).collect::<String>()))); }
    if bytes.len() > 1024 * 1024 { return Err(error("isolation operation output exceeds limit")); }
    Ok(bytes)
}
fn docker(policy: &Policy) -> Command {
    let mut command = Command::new("docker");
    command.arg("--host").arg(&policy.docker_host).env_remove("DOCKER_CONTEXT").env_remove("DOCKER_TLS_VERIFY").env_remove("DOCKER_CERT_PATH");
    command
}
fn docker_run(policy: &Policy, args: &[&str]) -> io::Result<Vec<u8>> {
    let mut command = docker(policy); command.args(args); run(command)
}
fn preflight(policy: &Policy) -> io::Result<()> {
    if !cfg!(target_os = "linux") || unsafe { libc::geteuid() } == 0 { return Err(error("Docker isolation currently requires a non-root Linux host user")); }
    let path = Path::new(policy.docker_host.strip_prefix("unix://").unwrap());
    if fs::canonicalize(path)? != path { return Err(error("Docker socket must be canonical")); }
    let meta = fs::symlink_metadata(path)?;
    if !meta.file_type().is_socket() || meta.uid() != unsafe { libc::geteuid() } { return Err(error("Docker socket must belong to the current unprivileged host user")); }
    let info: Value = serde_json::from_slice(&docker_run(policy, &["info", "--format", "{{json .}}"])?)?;
    if !info["SecurityOptions"].as_array().is_some_and(|rows| rows.iter().any(|v| v.as_str().is_some_and(|s| s.contains("name=rootless"))))
        || info["CgroupVersion"] != "2" || info["CgroupDriver"] == "none"
        || info["MemoryLimit"] != true || info["CpuCfsQuota"] != true || info["PidsLimit"] != true {
        return Err(error("Docker rootless mode and effective cgroup v2 memory/CPU/PID controls are required"));
    }
    let image: Value = serde_json::from_slice(&docker_run(policy, &["image", "inspect", &policy.image])?)?;
    let matches=if policy.image.starts_with("sha256:"){image[0]["Id"]==policy.image}
        else{image[0]["RepoDigests"].as_array().is_some_and(|rows|rows.iter().any(|v|v==&policy.image))};
    if !matches { return Err(error("pinned Docker image digest is unavailable locally; explicitly install the reviewed image")); }
    Ok(())
}
fn git(cwd: &Path, args: &[&str]) -> io::Result<String> {
    let mut command = Command::new("git");
    command.arg("-c").arg("core.hooksPath=/dev/null").args(args).current_dir(cwd)
        .env("GIT_CONFIG_NOSYSTEM", "1").env("GIT_CONFIG_GLOBAL", "/dev/null").env("GIT_NO_REPLACE_OBJECTS", "1").env_remove("GIT_CONFIG_PARAMETERS").env_remove("GIT_CONFIG_COUNT").env_remove("GIT_TEMPLATE_DIR")
        .env_remove("GIT_DIR").env_remove("GIT_WORK_TREE").env_remove("GIT_ALTERNATE_OBJECT_DIRECTORIES").env_remove("GIT_OBJECT_DIRECTORY")
        .env_remove("GIT_COMMON_DIR").env_remove("GIT_INDEX_FILE").env_remove("GIT_SHALLOW_FILE");
    String::from_utf8(run(command)?).map(|s| s.trim().to_owned()).map_err(|_| error("Git output is not UTF-8"))
}
fn clone_checkout(source: &Path, checkout: &Path, id: &str, base: Option<&str>) -> io::Result<(String, String)> {
    let source = fs::canonicalize(source)?;
    let allow_linked = active()?.is_none_or(|manifest| !manifest.profile.docker() || manifest.checkout != source);
    // Host Git never reads mutable source config, hooks, linked objects or
    // replacement refs. Only a descriptor-anchored sanitized snapshot is used.
    let snapshot = source_git::snapshot(&source, allow_linked)?;
    let sha = git(snapshot.path(), &["rev-parse", "--verify", &format!("{}^{{commit}}", base.unwrap_or("HEAD"))])?;
    if sha.len() != 40 || !sha.bytes().all(|b| b.is_ascii_hexdigit()) { return Err(error("invalid checkout base SHA")); }
    let mut command = Command::new("git");
    command.args(["-c", "core.hooksPath=/dev/null", "clone", "--no-local", "--no-hardlinks", "--no-checkout", "--template=", "--"])
        .arg(snapshot.path()).arg(checkout).env("GIT_CONFIG_NOSYSTEM", "1").env("GIT_CONFIG_GLOBAL", "/dev/null").env("GIT_NO_REPLACE_OBJECTS", "1")
        .env_remove("GIT_CONFIG_PARAMETERS").env_remove("GIT_CONFIG_COUNT").env_remove("GIT_TEMPLATE_DIR")
        .env_remove("GIT_DIR").env_remove("GIT_WORK_TREE").env_remove("GIT_ALTERNATE_OBJECT_DIRECTORIES").env_remove("GIT_OBJECT_DIRECTORY")
        .env_remove("GIT_COMMON_DIR").env_remove("GIT_INDEX_FILE").env_remove("GIT_SHALLOW_FILE");
    run(command)?;
    fs::set_permissions(checkout, fs::Permissions::from_mode(0o700))?;
    git(checkout, &["remote", "remove", "origin"])?;
    let branch = format!("doxa/{id}");
    git(checkout, &["checkout", "-b", &branch, &sha])?;
    if !checkout.join(".git").is_dir() || checkout.join(".git/objects/info/alternates").exists() { return Err(error("checkout has shared Git metadata")); }
    Ok((sha, branch))
}

pub fn create_args(manifest: &Manifest) -> io::Result<Vec<OsString>> {
    let policy = manifest.policy.as_ref().ok_or_else(|| error("Docker policy missing"))?;
    let mut args: Vec<OsString> = ["create", "--name"].into_iter().map(Into::into).collect();
    args.push(format!("doxa-{}-{}", manifest.session_id, &manifest.nonce[..12]).into());
    for value in ["--init", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
        "--user=0:0", "--ipc=private", "--cgroupns=private", "--network", manifest.profile.network(),
        "--tmpfs=/tmp:rw,nosuid,nodev,size=134217728,mode=1777"] { args.push(value.into()); }
    for (flag, value) in [("--memory", policy.memory_bytes.to_string()), ("--memory-swap", policy.memory_bytes.to_string()),
        ("--cpus", policy.cpus.to_string()), ("--pids-limit", policy.pids.to_string()),
        ("--label", format!("doxa.session={}", manifest.session_id)), ("--label", format!("doxa.nonce={}", manifest.nonce)),
        ("--label", format!("doxa.policy={}", manifest.policy_hash))] { args.extend([flag.into(), value.into()]); }
    for (source, target) in [(&manifest.checkout, "/workspace"), (&manifest.private_home, "/home/doxa"),
        (&manifest.cache, "/work-cache"), (&manifest.broker, "/run/doxa/session")] {
        private_directory(source, false)?;
        let source = source.to_str().filter(|s| !s.contains(',') && !s.contains('\n')).ok_or_else(|| error("mount path cannot contain comma or newline"))?;
        let access=if target=="/run/doxa/session"{",readonly"}else{""};
        args.extend(["--mount".into(), format!("type=bind,src={source},dst={target}{access}").into()]);
    }
    args.extend(["--env".into(), "HOME=/home/doxa".into(), "--env".into(), "TMPDIR=/work-cache/tmp".into(),
        "--workdir".into(), "/workspace".into(), "--entrypoint".into(), "/usr/local/bin/doxa-isolation-worker".into(),
        policy.image.clone().into(), "hold".into()]);
    Ok(args)
}

pub struct Runtime { path: PathBuf, manifest: Manifest, migration_stop: bool }
impl Runtime {
    pub fn prepare(home: &Path, id: &str, source: &Path, requested: Option<Profile>, resume: bool, base: Option<&str>) -> io::Result<Self> {
        let path = manifest_path(home, id)?;
        if path.exists() {
            if !resume { return Err(error("session isolation manifest already exists; resume the saved session explicitly")); }
            let mut manifest = read_manifest(&path)?;
            if requested.is_some_and(|p| p != manifest.profile) { return Err(error("resume isolation differs from saved policy; attach and change it while idle or explicitly migrate the session")); }
            if source != manifest.checkout && source != manifest.source { return Err(error("resume project does not match isolation manifest")); }
            if manifest.profile.docker() { preflight(manifest.policy.as_ref().unwrap())?; check_disk_budget(&manifest)?; reconcile(&mut manifest, true)?; }
            else {manifest.state="ready".into();}
            write_manifest(&path,&manifest)?;
            return Ok(Self { path, manifest, migration_stop:false });
        }
        let profile = requested.unwrap_or(configured_profile(home)?);
        if resume && profile.docker() { return Err(error("saved native session has no Docker manifest; automatic backend migration is forbidden")); }
        private_directory(&home.join("isolation"), true)?;
        let root = path.parent().unwrap(); private_directory(root, true)?;
        let source = fs::canonicalize(source)?;
        let project_source = active()?.filter(|parent| parent.checkout == source).map(|parent|parent.source).unwrap_or_else(||source.clone());
        let mut manifest = Manifest { version: 1, session_id: id.into(), profile, policy: None,
            policy_hash: String::new(), creation_policy_hash:String::new(), source: project_source, checkout: source.clone(),
            context_cwd:None, provider_rollout:None,
            checkout_device: 0, checkout_inode: 0, base_sha: String::new(), branch: String::new(),
            private_home: root.join("home"), cache: root.join("cache"), broker: root.join("broker"),
            container_id: None, nonce: nonce()?, state: "preparing".into() };
        if profile.docker() {
            let policy = Policy::configured(home)?; preflight(&policy)?;
            manifest.policy_hash = policy.hash(profile); manifest.creation_policy_hash=manifest.policy_hash.clone(); manifest.policy = Some(policy);
            let fs_stats = fs::metadata(home)?;
            if !fs_stats.is_dir() { return Err(error("isolation home missing")); }
            disk::check_host_floor(home, manifest.policy.as_ref().unwrap())?;
            manifest.checkout = root.join("checkout");
            for directory in [&manifest.private_home, &manifest.cache, &manifest.broker, &manifest.cache.join("tmp")] { private_directory(directory, true)?; }
            let (sha, branch) = clone_checkout(&source, &manifest.checkout, id, base)?; manifest.base_sha = sha; manifest.branch = branch;
            let meta = fs::metadata(&manifest.checkout)?; manifest.checkout_device = meta.dev(); manifest.checkout_inode = meta.ino();
            check_disk_budget(&manifest)?;
            write_manifest(&path, &manifest)?;
            let policy = manifest.policy.as_ref().unwrap().clone();
            let mut command = docker(&policy); command.args(create_args(&manifest)?);
            let container = String::from_utf8(run(command)?).map_err(|_| error("invalid container identity"))?.trim().to_owned();
            if container.len() != 64 || !container.bytes().all(|b| b.is_ascii_hexdigit()) { return Err(error("invalid created container ID")); }
            manifest.container_id = Some(container); manifest.state = "created".into(); write_manifest(&path, &manifest)?;
            if let Err(e) = reconcile(&mut manifest, true) {
                // Never remove other containers or silently fall back.
                let _ = docker_run(&policy, &["stop", manifest.container_id.as_deref().unwrap()]);
                manifest.state = "failed".into(); let _ = write_manifest(&path, &manifest); return Err(e);
            }
        } else { manifest.state = "ready".into(); }
        write_manifest(&path, &manifest)?;
        Ok(Self { path, manifest, migration_stop:false })
    }
    pub fn checkout(&self) -> &Path { &self.manifest.checkout }
    pub fn profile(&self) -> Profile { self.manifest.profile }
    pub fn status(&self) -> Value { self.manifest.status() }
    pub fn manifest(&self) -> &Manifest { &self.manifest }
    pub fn mark_migration_stop(&mut self) { self.migration_stop=true; }
    pub fn preserves_native_checkout(&self) -> bool { self.migration_stop }
    pub fn record_native_checkout(&mut self, checkout: &Path) -> io::Result<()> {
        if self.profile().docker() { return Err(error("cannot replace a Docker checkout")); }
        self.manifest.checkout = checkout.to_owned(); write_manifest(&self.path, &self.manifest)
    }
    pub fn prepare_provider(&self, engine: &str) -> io::Result<()> {
        if !self.profile().docker() || engine != "codex" { return Ok(()); }
        let destination = self.manifest.private_home.join("codex");
        private_directory(&destination, true)?;
        let source = std::env::var_os("CODEX_HOME").map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|v| PathBuf::from(v).join(".codex")))
            .ok_or_else(|| error("Codex auth home missing"))?.join("auth.json");
        let target = destination.join("auth.json");
        if target.exists() { return Ok(()); }
        let file = match OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK).open(source) {
            Ok(file) => file, Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()), Err(e) => return Err(e),
        };
        let meta = file.metadata()?;
        if !meta.is_file() || meta.uid() != unsafe {libc::geteuid()} || meta.nlink() != 1 || meta.len() > 1024*1024 { return Err(error("unsafe Codex authentication snapshot")); }
        let mut bytes = Vec::new(); file.take(1024*1024+1).read_to_end(&mut bytes)?;
        let value:Value = serde_json::from_slice(&bytes)?;
        if !value.is_object() { return Err(error("invalid Codex authentication snapshot")); }
        let mut output = OpenOptions::new().write(true).create_new(true).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(target)?;
        output.write_all(&bytes)?; output.sync_all()
    }
    pub fn activate(&self) {
        std::env::remove_var(RESUME_ROLLOUT);
        // Called once by the trusted daemon before any provider threads exist.
        std::env::set_var(SESSION_MANIFEST,&self.path);
        if self.manifest.context_cwd.is_some() && self.manifest.private_home.is_dir() {
            std::env::set_var("DOXA_ISOLATION_HOME",&self.manifest.private_home);
            if self.manifest.private_home.join("codex").is_dir() {
                std::env::set_var("CODEX_HOME",self.manifest.private_home.join("codex"));
            }
        }
        if self.profile().docker() {
            std::env::set_var(ACTIVE_MANIFEST, &self.path);
            std::env::set_var("DOXA_ISOLATION_HOME", &self.manifest.private_home);
        } else {
            std::env::remove_var(ACTIVE_MANIFEST);
            if self.manifest.context_cwd.is_none() { std::env::remove_var("DOXA_ISOLATION_HOME"); }
        }
    }
    pub fn set_profile(&mut self, profile: Profile, confirmed: bool) -> io::Result<Value> {
        if !confirmed { return Err(error("isolation changes require explicit confirmation")); }
        if profile == self.profile() { return Ok(self.status()); }
        if !profile.docker() || !self.profile().docker() { return Err(error("native/Docker migration requires an explicit stopped-session checkout and transcript import; running provider cannot be relabeled")); }
        let policy = self.manifest.policy.as_ref().unwrap();
        check_disk_budget(&self.manifest)?;
        inspect(&self.manifest)?;
        let id = self.manifest.container_id.as_deref().unwrap();
        if profile == Profile::DockerOffline { docker_run(policy, &["network", "disconnect", "-f", "bridge", id])?; }
        else {
            let actual=inspect(&self.manifest)?;
            if actual["NetworkSettings"]["Networks"].get("none").is_some(){
                docker_run(policy,&["network","disconnect","none",id])?;
            }
            docker_run(policy, &["network", "connect", "bridge", id])?;
        }
        let previous = self.manifest.clone();
        self.manifest.profile = profile; self.manifest.policy_hash = policy.hash(profile);
        // Labels bind immutable creation policy; current network policy is
        // separately verified and journaled. No image/mount/resource change.
        if let Err(e) = inspect_network(&self.manifest).and_then(|_| write_manifest(&self.path, &self.manifest)) {
            let rollback = if previous.profile == Profile::DockerOpen { ["network", "connect", "bridge", id] }
                else { ["network", "disconnect", "bridge", id] };
            let _ = docker_run(policy, &rollback); self.manifest = previous; return Err(e);
        }
        Ok(self.status())
    }
    pub fn stop(&mut self) -> io::Result<()> {
        if let Some(policy) = self.manifest.policy.as_ref().filter(|_|self.profile().docker()) {
            inspect(&self.manifest)?;
            self.manifest.state = "stopping".into(); write_manifest(&self.path, &self.manifest)?;
            docker_run(policy, &["stop", "--time", "5", self.manifest.container_id.as_deref().unwrap()])?;
        }
        self.manifest.state = "stopped".into(); write_manifest(&self.path, &self.manifest)
    }
}
pub(crate) fn free_bytes(path: &Path) -> io::Result<u64> {
    use std::os::unix::ffi::OsStrExt;
    let path = std::ffi::CString::new(path.as_os_str().as_bytes()).map_err(|_| error("invalid storage path"))?;
    let mut stat = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(path.as_ptr(), stat.as_mut_ptr()) } != 0 { return Err(io::Error::last_os_error()); }
    let stat = unsafe { stat.assume_init() }; Ok((stat.f_bavail as u64).saturating_mul(stat.f_frsize as u64))
}
fn inspect(manifest: &Manifest) -> io::Result<Value> {
    let policy = manifest.policy.as_ref().unwrap(); let id = manifest.container_id.as_deref().ok_or_else(|| error("container identity unavailable"))?;
    let rows: Value = serde_json::from_slice(&docker_run(policy, &["inspect", id])?)?; let actual = &rows[0];
    validate_inspect(manifest, actual)?; Ok(actual.clone())
}
pub fn validate_inspect(manifest: &Manifest, actual: &Value) -> io::Result<()> {
    let policy = manifest.policy.as_ref().ok_or_else(|| error("missing Docker policy"))?;
    let host = &actual["HostConfig"]; let config = &actual["Config"];
    // Network switches never change creation identity or Docker's labels.
    let label = config["Labels"]["doxa.policy"].as_str();
    if actual["Id"].as_str() != manifest.container_id.as_deref()
        || config["Image"] != policy.image || config["Labels"]["doxa.session"] != manifest.session_id
        || config["Labels"]["doxa.nonce"] != manifest.nonce
        || Some(manifest.creation_policy_hash.as_str())!=label
        || host["Privileged"] != false || host["ReadonlyRootfs"] != true
        || host["Memory"] != policy.memory_bytes || host["MemorySwap"] != policy.memory_bytes
        || host["NanoCpus"].as_u64() != Some((policy.cpus * 1_000_000_000.0) as u64)
        || host["PidsLimit"] != policy.pids || config["User"] != "0:0"
        || host["PidMode"].as_str().is_none_or(|mode| !mode.is_empty()) || host["IpcMode"] != "private"
        || host["CgroupnsMode"] == "host"
        || host["CapDrop"] != json!(["ALL"]) || host["CapAdd"].as_array().is_some_and(|rows| !rows.is_empty())
        || host["SecurityOpt"]!=json!(["no-new-privileges:true"])
        || host["Devices"].as_array().is_some_and(|rows| !rows.is_empty()) {
        return Err(error("container image/identity/hardening/resources differ from manifest; quarantined"));
    }
    let mounts = actual["Mounts"].as_array().ok_or_else(|| error("container has no mount list"))?;
    let expected = [(&manifest.checkout, "/workspace",true), (&manifest.private_home, "/home/doxa",true), (&manifest.cache, "/work-cache",true), (&manifest.broker, "/run/doxa/session",false)];
    if mounts.len() != expected.len() || expected.iter().any(|(source, target,writable)| !mounts.iter().any(|row|
        row["Type"] == "bind" && row["Source"].as_str() == source.to_str() && row["Destination"] == *target && row["RW"] == *writable)) {
        return Err(error("container mounts differ from the four private session mounts"));
    }
    for (source, _,_) in expected { private_directory(source, false)?; }
    let meta = fs::metadata(&manifest.checkout)?;
    if (meta.dev(), meta.ino()) != (manifest.checkout_device, manifest.checkout_inode) { return Err(error("checkout bind identity changed")); }
    Ok(())
}

#[cfg(test)]
mod checkout_tests {
    use super::*;
    #[test]
    fn independent_clone_copies_objects_without_origin_alternates_or_host_hooks() {
        let root=tempfile::tempdir().unwrap();let source=root.path().join("source");fs::create_dir(&source).unwrap();
        git(&source,&["init"]).unwrap();fs::write(source.join("README"),"base").unwrap();
        git(&source,&["add","README"]).unwrap();
        git(&source,&["-c","user.name=Fixture","-c","user.email=fixture@example.invalid","commit","-m","feat: fixture"]).unwrap();
        fs::write(source.join(".git/hooks/post-checkout"),"untrusted hook").unwrap();
        let target=root.path().join("checkout");
        let (base,branch)=clone_checkout(&source,&target,"session",None).unwrap();
        assert_eq!(branch,"doxa/session");assert_eq!(git(&target,&["rev-parse","HEAD"]).unwrap(),base);
        assert!(target.join(".git").is_dir());assert!(!target.join(".git/objects/info/alternates").exists());
        assert_eq!(git(&target,&["remote"]).unwrap(),"");assert!(!target.join(".git/hooks/post-checkout").exists());
        fs::write(target.join("README"),"session-only").unwrap();
        git(&target,&["add","README"]).unwrap();
        git(&target,&["-c","user.name=Fixture","-c","user.email=fixture@example.invalid","commit","-m","feat: isolated change"]).unwrap();
        assert_eq!(git(&source,&["rev-parse","HEAD"]).unwrap(),base);
        assert_eq!(fs::read_to_string(source.join("README")).unwrap(),"base");
    }
}
#[cfg(test)]
mod remote_engine_policy_tests {
    use super::*;
    #[test]
    fn remote_and_desktop_engine_endpoints_remain_refused_before_connection() {
        let mut policy = Policy {
            image: format!("sha256:{}", "a".repeat(64)),
            docker_host: String::new(), memory_bytes: 512 * 1024 * 1024,
            cpus: 1.0, pids: 128, disk_soft_limit_bytes: None,
            disk_free_floor_bytes: None,
        };
        for endpoint in ["ssh://fixture.invalid", "tcp://fixture.invalid:2376",
            "npipe:////./pipe/docker_engine", "unix:///var/run/docker.sock"] {
            policy.docker_host = endpoint.into();
            assert!(policy.validate().is_err(), "{endpoint} must not reach Docker");
        }
    }
}
#[cfg(test)]
mod disk_policy_compatibility_tests {
    use super::*;
    #[test]
    fn beta10_policy_without_disk_fields_keeps_its_saved_hash() {
        let old = format!(r#"{{"image":"sha256:{}","docker_host":"unix:///run/user/1000/test.sock","memory_bytes":536870912,"cpus":1.0,"pids":128}}"#, "a".repeat(64));
        let policy: Policy = serde_json::from_str(&old).unwrap();
        assert_eq!(policy.disk_soft_limit_bytes, None);
        assert_eq!(serde_json::to_string(&policy).unwrap(), old);
        let expected = format!("{:x}", Sha256::digest(format!(r#"[{old},"docker-open"]"#)));
        assert_eq!(policy.hash(Profile::DockerOpen), expected);
        policy.validate().unwrap();
    }
}
fn inspect_network(manifest: &Manifest) -> io::Result<()> {
    let actual = inspect(manifest)?;
    let networks = actual["NetworkSettings"]["Networks"].as_object().ok_or_else(|| error("Docker network state unavailable"))?;
    let valid = match manifest.profile { Profile::DockerOffline => networks.is_empty() || networks.len() == 1 && networks.contains_key("none"),
        Profile::DockerOpen => networks.len() == 1 && networks.contains_key("bridge"), Profile::Native => false };
    if !valid { return Err(error("container network differs from requested isolation policy")); }
    Ok(())
}
fn probe_worker(manifest: &Manifest) -> io::Result<()> {
    let policy = manifest.policy.as_ref().ok_or_else(|| error("Docker policy missing"))?;
    docker_run(policy, &["exec", manifest.container_id.as_deref().ok_or_else(|| error("container identity unavailable"))?,
        "/usr/local/bin/doxa-isolation-worker", "probe", &policy.memory_bytes.to_string(),
        &policy.cpus.to_string(), &policy.pids.to_string()])?;
    Ok(())
}
fn reconcile(manifest: &mut Manifest, start: bool) -> io::Result<()> {
    let policy = manifest.policy.as_ref().unwrap();
    let listed = docker_run(policy, &["ps", "-aq", "--filter", &format!("label=doxa.session={}", manifest.session_id)])?;
    let ids = String::from_utf8(listed).map_err(|_| error("invalid container list"))?;
    if ids.lines().count() != 1 || !manifest.container_id.as_ref().is_some_and(|id| ids.trim() == id || id.starts_with(ids.trim()) && !ids.trim().is_empty()) {
        return Err(error("missing or duplicate container for saved session; quarantined, native fallback refused"));
    }
    let actual = inspect(manifest)?;
    inspect_network(manifest)?;
    if actual["State"]["Running"] != true && start { docker_run(policy, &["start", manifest.container_id.as_deref().unwrap()])?; }
    // Real rootless UID mapping and all writable bind sources are probed.
    probe_worker(manifest)?;
    manifest.state = "ready".into(); Ok(())
}

/// This is present only in the trusted daemon process, never worker env.
pub fn active() -> io::Result<Option<Manifest>> {
    std::env::var_os(ACTIVE_MANIFEST).map(|path| read_manifest(Path::new(&path))).transpose()
}
/// A trusted host-only identity, separate from the provider's physical cwd.
pub fn session_manifest() -> io::Result<Option<Manifest>> {
    std::env::var_os(SESSION_MANIFEST).map(|path|read_manifest(Path::new(&path))).transpose()
}
pub fn context_cwd(workspace:&Path) -> io::Result<PathBuf> {
    match session_manifest()? {
        Some(manifest) if manifest.checkout==workspace => Ok(manifest.context_cwd.unwrap_or(manifest.checkout)),
        Some(_) => Err(error("session workspace differs from its verified isolation identity")),
        None => Ok(workspace.to_owned()),
    }
}
pub fn resume_rollout() -> io::Result<Option<PathBuf>> {
    let Some(manifest)=session_manifest()? else{return Ok(None);};
    let Some(path)=std::env::var_os(RESUME_ROLLOUT).map(PathBuf::from).or_else(||manifest.provider_rollout.clone()) else{return Ok(None);};
    validate_resume_rollout(&manifest,&path)?;Ok(Some(path))
}
/// Prefer the latest saved rollout when a provider returned a new owned path
/// after an earlier import. The selection stays in the trusted host process.
pub fn select_resume_rollout(path:&Path)->io::Result<bool>{
    let Some(manifest)=session_manifest()? else{return Ok(false);};
    if manifest.provider_rollout.is_none()||!path.starts_with(manifest.private_home.join("codex/sessions")){return Ok(false);}
    validate_resume_rollout(&manifest,path)?;
    std::env::set_var(RESUME_ROLLOUT,path);Ok(true)
}
fn validate_resume_rollout(manifest:&Manifest,path:&Path)->io::Result<()>{
    if !path.starts_with(manifest.private_home.join("codex/sessions")) {return Err(error("imported rollout escaped private provider home"));}
    if fs::canonicalize(path)?!=path{return Err(error("imported rollout contains symlink components"));}
    let file=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK).open(&path)?;
    let meta=file.metadata()?;
    if !meta.is_file()||meta.uid()!=unsafe{libc::geteuid()}||meta.nlink()!=1||meta.mode()&0o077!=0 {
        return Err(error("imported rollout is not a private owned regular file"));
    }
    Ok(())
}
pub fn worker_path(path: &Path) -> io::Result<PathBuf> {
    let Some(manifest) = active()? else { return Ok(path.to_owned()); };
    for (host, worker) in [(&manifest.checkout, "/workspace"), (&manifest.private_home, "/home/doxa"), (&manifest.cache, "/work-cache"), (&manifest.broker, "/run/doxa/session")] {
        if let Ok(relative) = path.strip_prefix(host) { return Ok(Path::new(worker).join(relative)); }
    }
    Err(error("provider requested a path outside the private worker mounts"))
}
pub fn map_frame(mut value: Value) -> io::Result<Value> {
    fn visit(value: &mut Value, manifest: &Manifest) {
        match value {
            Value::String(text) => for (host, worker) in [(&manifest.checkout, "/workspace"), (&manifest.private_home, "/home/doxa")] {
                if let Some(path) = host.to_str() { if text == path || text.starts_with(&format!("{path}/")) { *text = text.replacen(path, worker, 1); break; } }
            },
            Value::Array(rows) => for value in rows { visit(value, manifest); },
            Value::Object(rows) => for value in rows.values_mut() { visit(value, manifest); }, _ => {},
        }
    }
    if let Some(manifest) = active()? { visit(&mut value, &manifest); } Ok(value)
}
/// Convert a fully constructed provider command to a container exec. Only a
/// tiny explicit provider environment is forwarded; host inherited env stays
/// in the Docker client and cannot become worker credentials.
pub fn isolate_command(command: Command, provider: &str) -> io::Result<Command> {
    let Some(manifest) = active()? else { return Ok(command); };
    if !matches!(provider, "claude" | "codex") { return Err(error("unsupported Docker provider")); }
    check_disk_budget(&manifest)?;
    inspect(&manifest)?; inspect_network(&manifest)?;
    probe_worker(&manifest)?;
    let policy = manifest.policy.as_ref().unwrap();
    let mut isolated = docker(policy); isolated.args(["exec", "-i", "--workdir", "/workspace"]);
    for (key, value) in command.get_envs() {
        let Some(value) = value else { continue; };
        if matches!(key.to_str(), Some("CLAUDE_CONFIG_DIR" | "LORE_SKIP" | "CLAUDE_CODE_DISABLE_AUTO_MEMORY" | "DOXA_PEER_INBOUND_TURNS" | "CODEX_HOME")) {
            let value = if matches!(key.to_str(), Some("CLAUDE_CONFIG_DIR" | "CODEX_HOME")) { worker_path(Path::new(value))?.into_os_string() } else { value.to_owned() };
            isolated.arg("--env").arg(format!("{}={}", key.to_string_lossy(), value.to_string_lossy()));
        }
    }
    if provider == "codex" { isolated.args(["--env", "CODEX_HOME=/home/doxa/codex"]); }
    // Pass only this CLI's explicitly selected API credential. Using the bare
    // environment name keeps the secret out of Docker client argv/diagnostics.
    let credential=if provider=="claude"{"ANTHROPIC_API_KEY"}else{"OPENAI_API_KEY"};
    if std::env::var_os(credential).is_some_and(|value|!value.is_empty()){isolated.arg("--env").arg(credential);}
    isolated.arg(manifest.container_id.as_deref().unwrap()).arg("/usr/local/bin/doxa-isolation-worker").arg("exec")
        .arg(format!("/usr/local/bin/{provider}"));
    for arg in command.get_args() {
        let value = arg.to_string_lossy();
        let mut mapped = value.into_owned();
        for (host, target) in [(&manifest.checkout, "/workspace"), (&manifest.private_home, "/home/doxa"), (&manifest.broker, "/run/doxa/session")] {
            if let Some(host) = host.to_str() { mapped = mapped.replace(host, target); }
        }
        isolated.arg(mapped);
    }
    Ok(isolated)
}
