//! WebAssembly worker contracts and the dedicated child entrypoint.
//! The zero-grant contract returns one integer. The `render-local-panel-v1`
//! contract returns bounded, validated, data-only panel text. Production runs
//! either contract only in the sandboxed `doxa-plugin-worker` child behind
//! exact owner approval and, in the TUI, a signed installed-host permit.
use super::packages::{self, RecheckedPackage, MAX_MODULE, RENDER_PANEL_GRANT};
use sha2::{Digest, Sha256};
use std::io::{self, IsTerminal, Read, Write};
use unicode_width::UnicodeWidthChar;
use wasmparser::{ExternalKind, Parser, Payload};
use wasmi::errors::{ErrorKind, InstantiationError, MemoryError};
use wasmi::{Config, Engine, Instance, Module, Store, StoreLimits, StoreLimitsBuilder, TrapCode};

const REQUEST_MAGIC: &[u8; 8] = b"DOXAW1\0\0";
const PANEL_REQUEST_MAGIC: &[u8; 8] = b"DOXAPQ1\0";
const REQUEST_HEADER: usize = REQUEST_MAGIC.len() + 4 + 32;
const RESPONSE_MAGIC: &[u8; 8] = b"DOXAR1\0\0";
const RESPONSE_BYTES: usize = RESPONSE_MAGIC.len() + 1 + 4;
const PANEL_RESPONSE_MAGIC: &[u8; 8] = b"DOXAPR1\0";
const PANEL_RESPONSE_HEADER: usize = PANEL_RESPONSE_MAGIC.len() + 1 + 2;
pub(crate) const READY_MARKER: &[u8] = b"DOXA-WORKER-READY-v1\n";
const FUEL: u64 = 5_000_000;
const MEMORY_BYTES: usize = 256 * 65_536;
const TABLE_ELEMENTS: usize = 1024;

const PANEL_ENTRY: &str = "doxa_render_panel";
const PANEL_MEMORY: &str = "memory";
/// Runtime linear-memory cap for a panel module, enforced by the store
/// limiter during instantiation and every `memory.grow`. It is deliberately a
/// quarter of the 16 MiB static admission bound, so the runtime limiter, not
/// the module's declared maximum, is the binding memory guard.
pub(crate) const PANEL_MEMORY_BYTES: usize = 64 * 65_536;
/// Panel text bounds: UTF-8 bytes, LF-separated lines and terminal cells per
/// line. Text outside them is refused, never truncated or repaired.
pub(crate) const PANEL_MAX_BYTES: usize = 4096;
pub(crate) const PANEL_MAX_LINES: usize = 32;
pub(crate) const PANEL_MAX_CELLS: usize = 80;

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

/// The worker contract selected by the parent from the exact approved grant
/// set. It decides the request/response protocol and the only exports the
/// module may have. Neither contract imports a host function.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Contract {
    /// Zero grants; exports exactly `doxa_main: () -> i32`.
    Grantless,
    /// Exactly `render-local-panel-v1`; exports exactly
    /// `doxa_render_panel: () -> i64` and its linear `memory`.
    RenderPanel,
}

impl Contract {
    fn grants(self) -> &'static [&'static str] {
        match self {
            Self::Grantless => &[],
            Self::RenderPanel => &[RENDER_PANEL_GRANT],
        }
    }

    /// True only for the exact grant set of this contract, in order.
    pub(crate) fn matches(self, grants: &[String]) -> bool {
        grants.iter().map(String::as_str).eq(self.grants().iter().copied())
    }

    fn magic(self) -> &'static [u8; 8] {
        match self {
            Self::Grantless => REQUEST_MAGIC,
            Self::RenderPanel => PANEL_REQUEST_MAGIC,
        }
    }

    fn from_magic(magic: &[u8]) -> Option<Self> {
        [Self::Grantless, Self::RenderPanel].into_iter().find(|contract| contract.magic() == magic)
    }
}

/// Encodes only fresh owner-approved bytes with zero requested grants.
/// The parent calls recheck_approved immediately before spawning a sandboxed
/// worker; it never reopens a package path after encoding this frame.
pub(crate) fn encode_request(package: &RecheckedPackage) -> io::Result<Vec<u8>> {
    encode_contract_request(package, Contract::Grantless)
}

/// Encodes only fresh owner-approved bytes whose exact approved grant set is
/// `render-local-panel-v1`. A zero-grant package never gets a panel frame.
pub(crate) fn encode_panel_request(package: &RecheckedPackage) -> io::Result<Vec<u8>> {
    encode_contract_request(package, Contract::RenderPanel)
}

pub(crate) fn encode_contract_request(package: &RecheckedPackage, contract: Contract) -> io::Result<Vec<u8>> {
    let review = package.review();
    if !review.owner_approved || !contract.matches(&review.requested_grants) {
        return Err(invalid(match contract {
            Contract::Grantless => "worker contract requires exact approval and zero grants",
            Contract::RenderPanel => "panel contract requires exact approval of only render-local-panel-v1",
        }));
    }
    let bytes = package.module_bytes();
    if bytes.len() as u64 > MAX_MODULE {
        return Err(invalid("worker module exceeds input limit"));
    }
    let digest = Sha256::digest(bytes);
    if format!("{digest:x}") != review.module_sha256 {
        return Err(invalid("worker bytes differ from reviewed digest"));
    }
    let mut frame = Vec::with_capacity(REQUEST_HEADER + bytes.len());
    frame.extend_from_slice(contract.magic());
    frame.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
    frame.extend_from_slice(&digest);
    frame.extend_from_slice(bytes);
    Ok(frame)
}

/// Reads exactly one bounded zero-grant frame. A panel frame is refused here.
pub(crate) fn decode_request(input: impl Read) -> io::Result<Vec<u8>> {
    match decode_contract_request(input) {
        Ok((Contract::Grantless, bytes)) => Ok(bytes),
        Ok((Contract::RenderPanel, _)) => Err(invalid("worker protocol version mismatch")),
        Err((_, error)) => Err(error),
    }
}

/// Reads exactly one bounded frame of either contract. The sender must close
/// its pipe after writing; an unexpected trailing byte fails closed. Once the
/// version magic is known, a later failure names its contract so the worker
/// can answer in the protocol the parent expects.
pub(crate) fn decode_contract_request(mut input: impl Read)
    -> Result<(Contract, Vec<u8>), (Option<Contract>, io::Error)>
{
    let mut magic = [0u8; 8];
    input.read_exact(&mut magic).map_err(|error| (None, error))?;
    let contract = Contract::from_magic(&magic)
        .ok_or_else(|| (None, invalid("worker protocol version mismatch")))?;
    decode_body(input, contract).map(|bytes| (contract, bytes)).map_err(|error| (Some(contract), error))
}

fn decode_body(mut input: impl Read, contract: Contract) -> io::Result<Vec<u8>> {
    let mut header = [0u8; REQUEST_HEADER - 8];
    input.read_exact(&mut header)?;
    let length = u32::from_le_bytes(header[..4].try_into().unwrap()) as u64;
    if length > MAX_MODULE || length < 8 {
        return Err(invalid("worker input length outside bounds"));
    }
    let mut bytes = vec![0u8; length as usize];
    input.read_exact(&mut bytes)?;
    if input.read(&mut [0u8; 1])? != 0 {
        return Err(invalid("worker input has trailing bytes"));
    }
    let digest = Sha256::digest(&bytes);
    if digest.as_slice() != &header[4..36] {
        return Err(invalid("worker input digest mismatch"));
    }
    packages::validate_module(&bytes)?;
    validate_entry(&bytes, contract)?;
    Ok(bytes)
}

/// The module's complete export list must equal the contract's list. Panel
/// modules may export only their entry and the memory the worker reads.
fn validate_entry(bytes: &[u8], contract: Contract) -> io::Result<()> {
    let expected: &[(&str, ExternalKind)] = match contract {
        Contract::Grantless => &[("doxa_main", ExternalKind::Func)],
        Contract::RenderPanel => &[(PANEL_ENTRY, ExternalKind::Func), (PANEL_MEMORY, ExternalKind::Memory)],
    };
    let refused = || invalid(match contract {
        Contract::Grantless => "worker requires one doxa_main function export",
        Contract::RenderPanel => "panel worker requires exactly doxa_render_panel and memory exports",
    });
    let mut exports = Vec::new();
    for payload in Parser::new(0).parse_all(bytes) {
        if let Payload::ExportSection(section) =
            payload.map_err(|_| invalid("invalid worker module"))?
        {
            for export in section {
                let export = export.map_err(|_| invalid("invalid worker export"))?;
                if exports.len() == expected.len() { return Err(refused()); }
                exports.push((export.name, export.kind));
            }
        }
    }
    if exports.len() != expected.len()
        || !expected.iter().all(|wanted| exports.contains(wanted)) {
        return Err(refused());
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum WorkerFailure {
    InvalidModule,
    InvalidSignature,
    FuelExhausted,
    Trap,
    /// The store limiter denied a linear-memory allocation while the module
    /// was instantiated or running. Reported only by the panel contract.
    MemoryLimit,
    /// A panel module returned a range or text outside the panel contract.
    InvalidOutput,
}

impl WorkerFailure {
    fn code(self) -> u8 {
        match self {
            Self::InvalidModule => 1,
            Self::InvalidSignature => 2,
            Self::FuelExhausted => 3,
            Self::Trap => 4,
            Self::MemoryLimit => 5,
            Self::InvalidOutput => 6,
        }
    }

    fn from_code(code: u8) -> Option<Self> {
        [Self::InvalidModule, Self::InvalidSignature, Self::FuelExhausted, Self::Trap,
            Self::MemoryLimit, Self::InvalidOutput].into_iter().find(|failure| failure.code() == code)
    }
}

/// A fixed-size response has no arbitrary plugin text or error string.
/// Crashes, timeouts and sandbox failures are parent-side process outcomes.
pub(crate) fn encode_response(result: Result<i32, WorkerFailure>) -> [u8; RESPONSE_BYTES] {
    let mut frame = [0u8; RESPONSE_BYTES];
    frame[..8].copy_from_slice(RESPONSE_MAGIC);
    match result {
        Ok(value) => frame[9..13].copy_from_slice(&value.to_le_bytes()),
        Err(failure) => frame[8] = failure.code(),
    }
    frame
}

pub(crate) fn decode_response(mut input: impl Read) -> io::Result<Result<i32, WorkerFailure>> {
    let mut frame = [0u8; RESPONSE_BYTES];
    input.read_exact(&mut frame)?;
    if &frame[..8] != RESPONSE_MAGIC {
        return Err(invalid("worker response protocol version mismatch"));
    }
    if input.read(&mut [0u8; 1])? != 0 {
        return Err(invalid("worker response has trailing bytes"));
    }
    let value = i32::from_le_bytes(frame[9..13].try_into().unwrap());
    match (frame[8], WorkerFailure::from_code(frame[8])) {
        (0, _) => Ok(Ok(value)),
        (_, Some(failure)) if value == 0 => Ok(Err(failure)),
        _ => Err(invalid("invalid worker response status or payload")),
    }
}

/// Fuel, store memory and table limits are installed on every instance.
/// They are interpreter-enforced while Wasm runs, not admission checks.
fn instantiate(bytes: &[u8], memory_bytes: usize) -> Result<(Store<StoreLimits>, Instance), wasmi::Error> {
    let mut config = Config::default();
    config.consume_fuel(true);
    let engine = Engine::new(&config);
    let module = Module::new(&engine, bytes)?;
    let limits: StoreLimits = StoreLimitsBuilder::new()
        .memory_size(memory_bytes)
        .table_elements(TABLE_ELEMENTS)
        .memories(1)
        .tables(1)
        .instances(1)
        .trap_on_grow_failure(true)
        .build();
    let mut store = Store::new(&engine, limits);
    store.limiter(|limits| limits);
    store.set_fuel(FUEL)?;
    let instance = Instance::new(&mut store, &module, &[])?;
    Ok((store, instance))
}

/// Interpreter core used only by the sandbox child in production. Unit tests
/// also call it directly; fuel and store limits cannot protect the DOXA
/// process if this function is called there.
pub(crate) fn execute_worker(bytes: &[u8]) -> Result<i32, WorkerFailure> {
    packages::validate_module(bytes).map_err(|_| WorkerFailure::InvalidModule)?;
    validate_entry(bytes, Contract::Grantless).map_err(|_| WorkerFailure::InvalidModule)?;
    let (mut store, instance) = instantiate(bytes, MEMORY_BYTES)
        .map_err(|_| WorkerFailure::InvalidModule)?;
    let entry = instance
        .get_typed_func::<(), i32>(&store, "doxa_main")
        .map_err(|_| WorkerFailure::InvalidSignature)?;
    entry.call(&mut store, ()).map_err(|error| {
        if error.as_trap_code() == Some(TrapCode::OutOfFuel) {
            WorkerFailure::FuelExhausted
        } else {
            WorkerFailure::Trap
        }
    })
}

fn panel_failure(error: &wasmi::Error, otherwise: WorkerFailure) -> WorkerFailure {
    match (error.as_trap_code(), error.kind()) {
        (Some(TrapCode::OutOfFuel), _) => WorkerFailure::FuelExhausted,
        (Some(TrapCode::GrowthOperationLimited), _)
        | (_, ErrorKind::Memory(MemoryError::ResourceLimiterDeniedAllocation))
        | (_, ErrorKind::Instantiation(InstantiationError::FailedToInstantiateMemory(
            MemoryError::ResourceLimiterDeniedAllocation))) => WorkerFailure::MemoryLimit,
        _ => otherwise,
    }
}

/// `render-local-panel-v1` interpreter core, used only by the sandbox child in
/// production. The module returns `(length << 32) | offset` into its own
/// exported memory. The worker copies at most 4 KiB from that range and
/// releases it only after full panel validation. Fuel and the 4 MiB store
/// memory cap are enforced while the module runs.
pub(crate) fn execute_panel(bytes: &[u8]) -> Result<PanelText, WorkerFailure> {
    packages::validate_module(bytes).map_err(|_| WorkerFailure::InvalidModule)?;
    validate_entry(bytes, Contract::RenderPanel).map_err(|_| WorkerFailure::InvalidModule)?;
    let (mut store, instance) = instantiate(bytes, PANEL_MEMORY_BYTES)
        .map_err(|error| panel_failure(&error, WorkerFailure::InvalidModule))?;
    let memory = instance.get_memory(&store, PANEL_MEMORY).ok_or(WorkerFailure::InvalidSignature)?;
    let entry = instance
        .get_typed_func::<(), i64>(&store, PANEL_ENTRY)
        .map_err(|_| WorkerFailure::InvalidSignature)?;
    let packed = entry.call(&mut store, ())
        .map_err(|error| panel_failure(&error, WorkerFailure::Trap))? as u64;
    let offset = (packed & 0xffff_ffff) as usize;
    let length = (packed >> 32) as usize;
    if length > PANEL_MAX_BYTES { return Err(WorkerFailure::InvalidOutput); }
    let data = memory.data(&store);
    let end = offset.checked_add(length).filter(|end| *end <= data.len())
        .ok_or(WorkerFailure::InvalidOutput)?;
    validate_panel(&data[offset..end]).map_err(|_| WorkerFailure::InvalidOutput)
}

/// Text that passed every panel rule. Its only constructor is
/// `validate_panel`, so holding one is proof of validation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct PanelText { text: String }

impl PanelText {
    pub(crate) fn lines(&self) -> impl Iterator<Item = &str> { self.text.split('\n') }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum PanelRefusal {
    Empty,
    TooLarge,
    NotUtf8,
    ControlCharacter,
    InvisibleCharacter,
    ZeroWidthCharacter,
    TooManyLines,
    LineTooWide,
}

/// Format, bidirectional, separator, filler, variation-selector, tag and
/// noncharacter code points. They can hide, reorder or spoof visible text.
fn invisible(c: char) -> bool {
    matches!(c, '\u{00ad}' | '\u{034f}' | '\u{061c}' | '\u{115f}' | '\u{1160}'
        | '\u{17b4}' | '\u{17b5}' | '\u{180b}'..='\u{180f}' | '\u{200b}'..='\u{200f}'
        | '\u{2028}'..='\u{202e}' | '\u{2060}'..='\u{206f}' | '\u{3164}'
        | '\u{fdd0}'..='\u{fdef}' | '\u{fe00}'..='\u{fe0f}' | '\u{feff}' | '\u{ffa0}'
        | '\u{fff0}'..='\u{fffb}' | '\u{1bca0}'..='\u{1bca3}' | '\u{1d173}'..='\u{1d17a}'
        | '\u{e0000}'..='\u{e0fff}')
        || (c as u32) & 0xfffe == 0xfffe
}

/// Data-only panel text: 1–4096 bytes of UTF-8, at most 32 LF-separated
/// lines (one trailing LF is ignored) of at most 80 terminal cells. Every
/// other control character, including ESC, CR, TAB and C1 controls, and
/// every invisible or zero-width character is refused, so no terminal escape
/// sequence or hidden text can reach the TUI. Both the worker and the parent
/// apply this check; the parent never trusts the worker's verdict.
pub(crate) fn validate_panel(bytes: &[u8]) -> Result<PanelText, PanelRefusal> {
    if bytes.len() > PANEL_MAX_BYTES { return Err(PanelRefusal::TooLarge); }
    let text = std::str::from_utf8(bytes).map_err(|_| PanelRefusal::NotUtf8)?;
    let text = text.strip_suffix('\n').unwrap_or(text);
    if text.is_empty() { return Err(PanelRefusal::Empty); }
    for (index, line) in text.split('\n').enumerate() {
        if index == PANEL_MAX_LINES { return Err(PanelRefusal::TooManyLines); }
        let mut cells = 0;
        for c in line.chars() {
            if c.is_control() { return Err(PanelRefusal::ControlCharacter); }
            if invisible(c) { return Err(PanelRefusal::InvisibleCharacter); }
            match c.width() {
                Some(width) if width > 0 => cells += width,
                _ => return Err(PanelRefusal::ZeroWidthCharacter),
            }
            if cells > PANEL_MAX_CELLS { return Err(PanelRefusal::LineTooWide); }
        }
    }
    Ok(PanelText { text: text.to_owned() })
}

/// A bounded panel response: version, status, u16 length and validated text.
/// A failure carries no text.
pub(crate) fn encode_panel_response(result: Result<&PanelText, WorkerFailure>) -> Vec<u8> {
    let mut frame = Vec::with_capacity(PANEL_RESPONSE_HEADER + PANEL_MAX_BYTES);
    frame.extend_from_slice(PANEL_RESPONSE_MAGIC);
    match result {
        Ok(panel) => {
            frame.push(0);
            frame.extend_from_slice(&(panel.text.len() as u16).to_le_bytes());
            frame.extend_from_slice(panel.text.as_bytes());
        }
        Err(failure) => {
            frame.push(failure.code());
            frame.extend_from_slice(&0u16.to_le_bytes());
        }
    }
    frame
}

/// Parent-side decoder. The worker is untrusted for panel content, so the
/// payload is validated again here; unsafe text is a protocol failure.
pub(crate) fn decode_panel_response(mut input: impl Read) -> io::Result<Result<PanelText, WorkerFailure>> {
    let mut header = [0u8; PANEL_RESPONSE_HEADER];
    input.read_exact(&mut header)?;
    if &header[..8] != PANEL_RESPONSE_MAGIC {
        return Err(invalid("panel response protocol version mismatch"));
    }
    let length = u16::from_le_bytes([header[9], header[10]]) as usize;
    if length > PANEL_MAX_BYTES {
        return Err(invalid("panel response exceeds its limit"));
    }
    let mut payload = vec![0u8; length];
    input.read_exact(&mut payload)?;
    if input.read(&mut [0u8; 1])? != 0 {
        return Err(invalid("panel response has trailing bytes"));
    }
    match (header[8], WorkerFailure::from_code(header[8])) {
        (0, _) => validate_panel(&payload).map(Ok)
            .map_err(|_| invalid("worker returned text outside the panel contract")),
        (_, Some(failure)) if length == 0 => Ok(Err(failure)),
        _ => Err(invalid("invalid panel response status or payload")),
    }
}

/// Dedicated child entrypoint. Its only input is one bounded, independently
/// decoded frame on stdin; its only output is the contract's fixed or bounded
/// response. The parent must launch this binary through the cgroup-backed
/// sandbox seam.
pub(crate) fn serve_stdio() -> io::Result<()> {
    if io::stdin().is_terminal() || io::stdout().is_terminal() {
        return Err(invalid("plugin worker requires private pipes"));
    }
    // The parent can now distinguish a child that reached the worker entry
    // from a Bubblewrap setup or loader failure. No module bytes are read yet.
    let mut diagnostic = io::stderr().lock();
    diagnostic.write_all(READY_MARKER)?;
    diagnostic.flush()?;
    drop(diagnostic);
    let response = match decode_contract_request(io::stdin().lock()) {
        Ok((Contract::Grantless, bytes)) => encode_response(execute_worker(&bytes)).to_vec(),
        Ok((Contract::RenderPanel, bytes)) =>
            encode_panel_response(execute_panel(&bytes).as_ref().map_err(|failure| *failure)),
        Err((Some(Contract::RenderPanel), _)) => encode_panel_response(Err(WorkerFailure::InvalidModule)),
        Err(_) => encode_response(Err(WorkerFailure::InvalidModule)).to_vec(),
    };
    let mut output = io::stdout().lock();
    output.write_all(&response)?;
    output.flush()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::path::Path;

    fn wasm(wat: &str) -> Vec<u8> { wat::parse_str(wat).unwrap() }

    fn write_private(path: &Path, bytes: &[u8]) {
        std::fs::write(path, bytes).unwrap();
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
    }

    fn approved_package(bytes: &[u8]) -> (tempfile::TempDir, RecheckedPackage) {
        approved_package_with(bytes, &[])
    }

    fn approved_package_with(bytes: &[u8], grants: &[&str]) -> (tempfile::TempDir, RecheckedPackage) {
        let dir = tempfile::tempdir().unwrap();
        let package_dir = dir.path().join("native-plugin-packages/demo");
        std::fs::create_dir_all(&package_dir).unwrap();
        for path in [dir.path().to_path_buf(), dir.path().join("native-plugin-packages"), package_dir.clone()] {
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let list = grants.iter().map(|grant| format!("'{grant}'")).collect::<Vec<_>>().join(", ");
        let manifest = format!("package_api_version = 1\nname = 'demo'\nversion = '1.0'\nartifact_format = 'wasm-core-v1'\nrequested_grants = [{list}]\n");
        write_private(&package_dir.join("manifest.toml"), manifest.as_bytes());
        write_private(&package_dir.join("module.wasm"), bytes);
        write_private(&dir.path().join("config.toml"), format!(
            "[[native_plugin_packages]]\nname = 'demo'\nmanifest_sha256 = '{:x}'\nmodule_sha256 = '{:x}'\ngrants = [{list}]\n",
            Sha256::digest(manifest.as_bytes()), Sha256::digest(bytes)).as_bytes());
        let review = packages::preflight(dir.path(), "demo").unwrap();
        let checked = packages::recheck_approved(dir.path(), &review).unwrap();
        (dir, checked)
    }

    /// A panel module whose data segment holds `text` at offset 16.
    fn panel_module(text: &str) -> Vec<u8> {
        let escaped = text.bytes().map(|byte| format!("\\{byte:02x}")).collect::<String>();
        let packed = ((text.len() as u64) << 32) | 16;
        wasm(&format!("(module (memory (export \"memory\") 1 1) (data (i32.const 16) \"{escaped}\")\
            (func (export \"doxa_render_panel\") (result i64) i64.const {packed}))"))
    }

    #[test]
    fn approved_grantless_request_roundtrips() {
        let bytes = wasm("(module (func (export \"doxa_main\") (result i32) i32.const 17))");
        let (_dir, checked) = approved_package(&bytes);
        let frame = encode_request(&checked).unwrap();
        assert_eq!(decode_request(frame.as_slice()).unwrap(), bytes);
        assert_eq!(execute_worker(&bytes), Ok(17));
    }

    #[test]
    fn request_rejects_changed_identity_and_grants() {
        let bytes = wasm("(module (func (export \"doxa_main\") (result i32) i32.const 17))");
        let (dir, checked) = approved_package(&bytes);
        let mut frame = encode_request(&checked).unwrap();
        frame[12] ^= 1;
        assert!(decode_request(frame.as_slice()).unwrap_err().to_string().contains("digest"));
        write_private(&dir.path().join("config.toml"), b"");
        assert!(packages::recheck_approved(dir.path(), checked.review()).is_err());
        let manifest = dir.path().join("native-plugin-packages/demo/manifest.toml");
        let granted = b"package_api_version = 1\nname = 'demo'\nversion = '1.0'\nartifact_format = 'wasm-core-v1'\nrequested_grants = ['render-local-panel-v1']\n";
        write_private(&manifest, granted);
        write_private(&dir.path().join("config.toml"), format!(
            "[[native_plugin_packages]]\nname = 'demo'\nmanifest_sha256 = '{:x}'\nmodule_sha256 = '{:x}'\ngrants = ['render-local-panel-v1']\n",
            Sha256::digest(granted), Sha256::digest(&bytes)).as_bytes());
        let granted_review = packages::preflight(dir.path(), "demo").unwrap();
        let granted_package = packages::recheck_approved(dir.path(), &granted_review).unwrap();
        assert!(encode_request(&granted_package).unwrap_err().to_string().contains("zero grants"));
    }

    #[test]
    fn malformed_frames_and_wrong_exports_fail_closed() {
        let bytes = wasm("(module (func (export \"doxa_main\") (result i32) i32.const 17))");
        let (_dir, checked) = approved_package(&bytes);
        let frame = encode_request(&checked).unwrap();
        assert!(decode_request(&frame[..frame.len()-1]).is_err());
        let mut oversized = frame.clone();
        oversized[8..12].copy_from_slice(&((MAX_MODULE + 1) as u32).to_le_bytes());
        assert!(decode_request(oversized.as_slice()).is_err());
        let mut trailing = frame.clone();
        trailing.push(0);
        assert!(decode_request(trailing.as_slice()).is_err());
        let mut version = frame.clone();
        version[0] ^= 1;
        assert!(decode_request(version.as_slice()).is_err());
        for module in [
            wasm("(module (func (export \"other\") (result i32) i32.const 0))"),
            wasm("(module (func (export \"doxa_main\") (result i32) i32.const 0) (memory (export \"mem\") 1 1))"),
        ] {
            assert!(validate_entry(&module, Contract::Grantless).is_err());
        }
    }

    #[test]
    fn fuel_trap_signature_and_memory_limits_are_enforced() {
        assert_eq!(execute_worker(&wasm("(module (func (export \"doxa_main\") (result i32) (loop br 0) i32.const 0))")),
            Err(WorkerFailure::FuelExhausted));
        assert_eq!(execute_worker(&wasm("(module (func (export \"doxa_main\") (result i32) unreachable))")),
            Err(WorkerFailure::Trap));
        assert_eq!(execute_worker(&wasm("(module (func (export \"doxa_main\")))")),
            Err(WorkerFailure::InvalidSignature));
        // A declared maximum makes memory.grow return -1 under WASM1;
        // the store cap independently prevents growth beyond 16 MiB.
        assert_eq!(execute_worker(&wasm("(module (memory 1 256) (func (export \"doxa_main\") (result i32) i32.const 256 memory.grow))")),
            Ok(-1));
        assert_eq!(execute_worker(&wasm("(module (memory 1 256) (func (export \"doxa_main\") (result i32) i32.const 255 memory.grow))")),
            Ok(1));
    }

    #[test]
    fn response_frame_is_fixed_and_rejects_ambiguity() {
        for outcome in [Ok(17), Ok(-1), Err(WorkerFailure::Trap), Err(WorkerFailure::FuelExhausted)] {
            let frame = encode_response(outcome);
            assert_eq!(frame.len(), RESPONSE_BYTES);
            assert_eq!(decode_response(frame.as_slice()).unwrap(), outcome);
            assert!(decode_response(&frame[..frame.len()-1]).is_err());
            let mut trailing = frame.to_vec();
            trailing.push(0);
            assert!(decode_response(trailing.as_slice()).is_err());
        }
        let mut invalid_status = encode_response(Ok(17));
        invalid_status[8] = 2;
        assert!(decode_response(invalid_status.as_slice()).is_err());
        let mut invalid_version = encode_response(Ok(17));
        invalid_version[0] ^= 1;
        assert!(decode_response(invalid_version.as_slice()).is_err());
        let mut unknown_status = encode_response(Err(WorkerFailure::Trap));
        unknown_status[8] = 7;
        assert!(decode_response(unknown_status.as_slice()).is_err());
    }

    #[test]
    fn panel_request_requires_exact_approved_render_grant() {
        let module = panel_module("hello");
        let (_dir, granted) = approved_package_with(&module, &[RENDER_PANEL_GRANT]);
        let frame = encode_panel_request(&granted).unwrap();
        assert_eq!(&frame[..8], PANEL_REQUEST_MAGIC);
        let (contract, bytes) = decode_contract_request(frame.as_slice()).unwrap();
        assert_eq!((contract, bytes), (Contract::RenderPanel, module.clone()));
        // A panel frame is never a zero-grant frame, and the zero-grant
        // encoder refuses the granted package.
        assert!(decode_request(frame.as_slice()).is_err());
        assert!(encode_request(&granted).is_err());
        // A zero-grant package gets no panel frame.
        let (_dir, grantless) = approved_package_with(&module, &[]);
        assert!(encode_panel_request(&grantless).unwrap_err().to_string().contains("render-local-panel-v1"));
        assert!(Contract::RenderPanel.matches(&[RENDER_PANEL_GRANT.to_owned()]));
        assert!(!Contract::RenderPanel.matches(&[]));
        assert!(!Contract::RenderPanel.matches(&[RENDER_PANEL_GRANT.to_owned(), RENDER_PANEL_GRANT.to_owned()]));
        assert!(!Contract::Grantless.matches(&[RENDER_PANEL_GRANT.to_owned()]));
    }

    #[test]
    fn panel_identity_change_after_approval_is_refused_before_encoding() {
        let module = panel_module("hello");
        let (dir, granted) = approved_package_with(&module, &[RENDER_PANEL_GRANT]);
        let review = granted.review();
        let path = dir.path().join("native-plugin-packages/demo/module.wasm");
        // Same bytes, new inode: the approved identity is the opened inode.
        let retired = std::fs::File::open(&path).unwrap();
        std::fs::remove_file(&path).unwrap();
        write_private(&path, &module);
        assert!(packages::recheck_approved(dir.path(), review).unwrap_err()
            .to_string().contains("identity changed"));
        drop(retired);
        // Changed bytes no longer match the owner's digest approval.
        write_private(&path, &panel_module("other"));
        assert!(packages::recheck_approved(dir.path(), review).is_err());
    }

    #[test]
    fn panel_exports_are_exactly_entry_and_memory() {
        let good = panel_module("ok");
        assert!(validate_entry(&good, Contract::RenderPanel).is_ok());
        assert!(validate_entry(&good, Contract::Grantless).is_err());
        for module in [
            wasm("(module (func (export \"doxa_main\") (result i32) i32.const 0))"),
            wasm("(module (memory (export \"memory\") 1 1))"),
            wasm("(module (memory 1 1) (func (export \"doxa_render_panel\") (result i64) i64.const 0))"),
            wasm("(module (memory (export \"memory\") 1 1) (global (export \"g\") i32 (i32.const 0))\
                (func (export \"doxa_render_panel\") (result i64) i64.const 0))"),
            wasm("(module (memory (export \"memory\") 1 1) (func (export \"doxa_render_panel\") (result i64) i64.const 0)\
                (func (export \"doxa_main\") (result i32) i32.const 0))"),
        ] {
            assert!(validate_entry(&module, Contract::RenderPanel).is_err());
            assert_eq!(execute_panel(&module), Err(WorkerFailure::InvalidModule));
        }
        assert_eq!(execute_panel(&wasm("(module (memory (export \"memory\") 1 1)\
            (func (export \"doxa_render_panel\") (result i32) i32.const 0))")), Err(WorkerFailure::InvalidSignature));
    }

    #[test]
    fn panel_returns_validated_text_from_module_memory() {
        let panel = execute_panel(&panel_module("Queue: 3 jobs\nNext: deploy ✓\n")).unwrap();
        assert_eq!(panel.lines().collect::<Vec<_>>(), ["Queue: 3 jobs", "Next: deploy ✓"]);
        let frame = encode_panel_response(Ok(&panel));
        assert_eq!(decode_panel_response(frame.as_slice()).unwrap(), Ok(panel));
    }

    #[test]
    fn panel_refuses_escape_sequences_and_control_characters() {
        for text in ["\u{1b}[2J\u{1b}[H", "title\u{1b}]52;c;cGF5bG9hZA==\u{7}", "a\u{7}", "a\rb",
            "a\tb", "a\u{0}b", "a\u{8}b", "a\u{7f}", "\u{9b}31m", "\u{9d}0;title\u{9c}", "a\u{85}b"] {
            assert_eq!(validate_panel(text.as_bytes()), Err(PanelRefusal::ControlCharacter), "{text:?}");
            assert_eq!(execute_panel(&panel_module(text)), Err(WorkerFailure::InvalidOutput), "{text:?}");
        }
    }

    #[test]
    fn panel_refuses_invisible_bidi_and_zero_width_text() {
        for text in ["admin\u{202e}txt", "a\u{2066}b", "a\u{200b}b", "a\u{feff}", "a\u{2028}b",
            "\u{3164}", "a\u{e0041}", "a\u{fe0f}", "a\u{ffff}"] {
            assert_eq!(validate_panel(text.as_bytes()), Err(PanelRefusal::InvisibleCharacter), "{text:?}");
        }
        assert_eq!(validate_panel("e\u{301}".as_bytes()), Err(PanelRefusal::ZeroWidthCharacter));
        assert_eq!(validate_panel(&[b'a', 0xff, b'b']), Err(PanelRefusal::NotUtf8));
        assert_eq!(validate_panel(b""), Err(PanelRefusal::Empty));
        assert_eq!(validate_panel(b"\n"), Err(PanelRefusal::Empty));
    }

    #[test]
    fn panel_bounds_bytes_lines_and_cells() {
        let wide = "界".repeat(PANEL_MAX_CELLS / 2);
        assert!(validate_panel(wide.as_bytes()).is_ok());
        assert_eq!(validate_panel(format!("{wide}x").as_bytes()), Err(PanelRefusal::LineTooWide));
        assert!(validate_panel("x".repeat(PANEL_MAX_CELLS).as_bytes()).is_ok());
        assert_eq!(validate_panel("x".repeat(PANEL_MAX_CELLS + 1).as_bytes()), Err(PanelRefusal::LineTooWide));
        let rows = vec!["row"; PANEL_MAX_LINES].join("\n");
        assert_eq!(validate_panel(rows.as_bytes()).unwrap().lines().count(), PANEL_MAX_LINES);
        assert_eq!(validate_panel(format!("{rows}\nrow").as_bytes()), Err(PanelRefusal::TooManyLines));
        let full = vec!["x".repeat(PANEL_MAX_CELLS); PANEL_MAX_LINES].join("\n");
        assert!(full.len() < PANEL_MAX_BYTES);
        // Four-byte, two-cell characters: every line fits, the bytes do not.
        let large = vec!["😀".repeat(PANEL_MAX_CELLS / 2); PANEL_MAX_LINES].join("\n");
        assert!(large.len() > PANEL_MAX_BYTES);
        assert_eq!(validate_panel(large.as_bytes()), Err(PanelRefusal::TooLarge));
        assert_eq!(execute_panel(&panel_module(&large)), Err(WorkerFailure::InvalidOutput));
    }

    #[test]
    fn panel_range_outside_module_memory_is_refused() {
        for packed in [
            (5u64 << 32) | 65_534,          // runs past the 64 KiB memory
            (5u64 << 32) | 0xffff_ffff,     // offset at the u32 limit
            ((PANEL_MAX_BYTES as u64 + 1) << 32), // longer than the panel limit
            0,                               // empty
        ] {
            let module = wasm(&format!("(module (memory (export \"memory\") 1 1)\
                (func (export \"doxa_render_panel\") (result i64) i64.const {packed}))"));
            assert_eq!(execute_panel(&module), Err(WorkerFailure::InvalidOutput), "{packed:#x}");
        }
    }

    #[test]
    fn panel_runtime_fuel_stops_an_infinite_loop() {
        let looping = wasm("(module (memory (export \"memory\") 1 1)\
            (func (export \"doxa_render_panel\") (result i64) (loop br 0) i64.const 0))");
        let (sender, receiver) = std::sync::mpsc::channel();
        std::thread::spawn(move || { let _ = sender.send(execute_panel(&looping)); });
        let outcome = receiver.recv_timeout(std::time::Duration::from_secs(20))
            .expect("infinite loop was not stopped by the runtime fuel limit");
        assert_eq!(outcome, Err(WorkerFailure::FuelExhausted));
    }

    #[test]
    fn panel_runtime_memory_cap_stops_allocation_bombs() {
        // Grows one page at a time until refused, then would return valid
        // text. Without the runtime cap it reaches the 16 MiB declared max.
        let grow_loop = wasm("(module (memory (export \"memory\") 1 256) (data (i32.const 16) \"ok\")\
            (func (export \"doxa_render_panel\") (result i64)\
              (block (loop (br_if 1 (i32.eq (memory.grow (i32.const 1)) (i32.const -1))) (br 0)))\
              i64.const 0x0000000200000010))");
        assert_eq!(execute_panel(&grow_loop), Err(WorkerFailure::MemoryLimit));
        let one_step = wasm("(module (memory (export \"memory\") 1 256) (data (i32.const 16) \"ok\")\
            (func (export \"doxa_render_panel\") (result i64)\
              (drop (memory.grow (i32.const 255))) i64.const 0x0000000200000010))");
        assert_eq!(execute_panel(&one_step), Err(WorkerFailure::MemoryLimit));
        // An initial size above the runtime cap is refused at instantiation.
        let eager = wasm("(module (memory (export \"memory\") 255 256) (data (i32.const 16) \"ok\")\
            (func (export \"doxa_render_panel\") (result i64) i64.const 0x0000000200000010))");
        assert!(packages::validate_module(&eager).is_ok(), "static admission accepts 255 pages");
        assert_eq!(execute_panel(&eager), Err(WorkerFailure::MemoryLimit));
        // Growth inside the cap is ordinary.
        let within = wasm(&format!("(module (memory (export \"memory\") 1 256) (data (i32.const 16) \"ok\")\
            (func (export \"doxa_render_panel\") (result i64)\
              (drop (memory.grow (i32.const {}))) i64.const 0x0000000200000010))", PANEL_MEMORY_BYTES / 65_536 - 1));
        assert_eq!(execute_panel(&within).unwrap().lines().collect::<Vec<_>>(), ["ok"]);
    }

    #[test]
    fn panel_trap_and_recursion_are_module_failures() {
        assert_eq!(execute_panel(&wasm("(module (memory (export \"memory\") 1 1)\
            (func (export \"doxa_render_panel\") (result i64) unreachable))")), Err(WorkerFailure::Trap));
        let recursion = wasm("(module (memory (export \"memory\") 1 1)\
            (func $deep (result i64) call $deep)\
            (func (export \"doxa_render_panel\") (result i64) call $deep))");
        assert!(matches!(execute_panel(&recursion), Err(WorkerFailure::Trap | WorkerFailure::FuelExhausted)));
    }

    #[test]
    fn panel_response_is_bounded_and_revalidated_by_the_parent() {
        let panel = validate_panel(b"ready").unwrap();
        let frame = encode_panel_response(Ok(&panel));
        assert!(decode_panel_response(&frame[..frame.len() - 1]).is_err());
        let mut trailing = frame.clone();
        trailing.push(b'x');
        assert!(decode_panel_response(trailing.as_slice()).is_err());
        let mut version = frame.clone();
        version[0] ^= 1;
        assert!(decode_panel_response(version.as_slice()).is_err());
        // A worker that skips its own check cannot pass an escape sequence.
        let mut forged = PANEL_RESPONSE_MAGIC.to_vec();
        forged.push(0);
        forged.extend_from_slice(&5u16.to_le_bytes());
        forged.extend_from_slice(b"\x1b[2J!");
        assert!(decode_panel_response(forged.as_slice()).unwrap_err().to_string().contains("panel contract"));
        let mut oversized = PANEL_RESPONSE_MAGIC.to_vec();
        oversized.push(0);
        oversized.extend_from_slice(&((PANEL_MAX_BYTES + 1) as u16).to_le_bytes());
        oversized.extend(std::iter::repeat_n(b'x', PANEL_MAX_BYTES + 1));
        assert!(decode_panel_response(oversized.as_slice()).is_err());
        let mut failure_with_text = encode_panel_response(Err(WorkerFailure::Trap));
        failure_with_text[9] = 1;
        failure_with_text.push(b'x');
        assert!(decode_panel_response(failure_with_text.as_slice()).is_err());
        for failure in [WorkerFailure::MemoryLimit, WorkerFailure::InvalidOutput, WorkerFailure::FuelExhausted] {
            let frame = encode_panel_response(Err(failure));
            assert_eq!(decode_panel_response(frame.as_slice()).unwrap(), Err(failure));
        }
    }
}
