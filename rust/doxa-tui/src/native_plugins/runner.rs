//! Grantless WebAssembly worker contract and dedicated child entrypoint.
//! No TUI or plugin command launches it; end-to-end cgroup proof remains open.
use super::packages::{self, RecheckedPackage, MAX_MODULE};
use sha2::{Digest, Sha256};
use std::io::{self, IsTerminal, Read, Write};
use wasmparser::{ExternalKind, Parser, Payload};
use wasmi::{Config, Engine, Instance, Module, Store, StoreLimits, StoreLimitsBuilder, TrapCode};

const REQUEST_MAGIC: &[u8; 8] = b"DOXAW1\0\0";
const REQUEST_HEADER: usize = REQUEST_MAGIC.len() + 4 + 32;
const RESPONSE_MAGIC: &[u8; 8] = b"DOXAR1\0\0";
const RESPONSE_BYTES: usize = RESPONSE_MAGIC.len() + 1 + 4;
pub(crate) const READY_MARKER: &[u8] = b"DOXA-WORKER-READY-v1\n";
const FUEL: u64 = 5_000_000;
const MEMORY_BYTES: usize = 256 * 65_536;
const TABLE_ELEMENTS: usize = 1024;

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

/// Encodes only fresh owner-approved bytes with zero requested grants.
/// A future parent must call recheck_approved immediately before spawning a
/// sandboxed worker; it must never reopen a package path.
pub(crate) fn encode_request(package: &RecheckedPackage) -> io::Result<Vec<u8>> {
    if !package.review().owner_approved || !package.review().requested_grants.is_empty() {
        return Err(invalid("worker contract requires exact approval and zero grants"));
    }
    let bytes = package.module_bytes();
    if bytes.len() as u64 > MAX_MODULE {
        return Err(invalid("worker module exceeds input limit"));
    }
    let digest = Sha256::digest(bytes);
    if format!("{digest:x}") != package.review().module_sha256 {
        return Err(invalid("worker bytes differ from reviewed digest"));
    }
    let mut frame = Vec::with_capacity(REQUEST_HEADER + bytes.len());
    frame.extend_from_slice(REQUEST_MAGIC);
    frame.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
    frame.extend_from_slice(&digest);
    frame.extend_from_slice(bytes);
    Ok(frame)
}

/// Reads exactly one bounded frame. The sender must close its pipe after
/// writing; an unexpected trailing byte fails closed.
pub(crate) fn decode_request(mut input: impl Read) -> io::Result<Vec<u8>> {
    let mut header = [0u8; REQUEST_HEADER];
    input.read_exact(&mut header)?;
    if &header[..REQUEST_MAGIC.len()] != REQUEST_MAGIC {
        return Err(invalid("worker protocol version mismatch"));
    }
    let length = u32::from_le_bytes(header[8..12].try_into().unwrap()) as u64;
    if length > MAX_MODULE || length < 8 {
        return Err(invalid("worker input length outside bounds"));
    }
    let mut bytes = vec![0u8; length as usize];
    input.read_exact(&mut bytes)?;
    if input.read(&mut [0u8; 1])? != 0 {
        return Err(invalid("worker input has trailing bytes"));
    }
    let digest = Sha256::digest(&bytes);
    if digest.as_slice() != &header[12..44] {
        return Err(invalid("worker input digest mismatch"));
    }
    packages::validate_module(&bytes)?;
    validate_entry(&bytes)?;
    Ok(bytes)
}

fn validate_entry(bytes: &[u8]) -> io::Result<()> {
    let mut exports = 0;
    for payload in Parser::new(0).parse_all(bytes) {
        if let Payload::ExportSection(section) =
            payload.map_err(|_| invalid("invalid worker module"))?
        {
            for export in section {
                let export = export.map_err(|_| invalid("invalid worker export"))?;
                exports += 1;
                if export.name != "doxa_main" || export.kind != ExternalKind::Func {
                    return Err(invalid("worker requires one doxa_main function export"));
                }
            }
        }
    }
    if exports != 1 {
        return Err(invalid("worker requires one doxa_main function export"));
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum WorkerFailure {
    InvalidModule,
    InvalidSignature,
    FuelExhausted,
    Trap,
}

/// A fixed-size response has no arbitrary plugin text or error string.
/// Crashes, timeouts and sandbox failures are parent-side process outcomes.
pub(crate) fn encode_response(result: Result<i32, WorkerFailure>) -> [u8; RESPONSE_BYTES] {
    let mut frame = [0u8; RESPONSE_BYTES];
    frame[..8].copy_from_slice(RESPONSE_MAGIC);
    match result {
        Ok(value) => frame[9..13].copy_from_slice(&value.to_le_bytes()),
        Err(failure) => frame[8] = match failure {
            WorkerFailure::InvalidModule => 1,
            WorkerFailure::InvalidSignature => 2,
            WorkerFailure::FuelExhausted => 3,
            WorkerFailure::Trap => 4,
        },
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
    match frame[8] {
        0 => Ok(Ok(value)),
        1..=4 if value == 0 => Ok(Err(match frame[8] {
            1 => WorkerFailure::InvalidModule,
            2 => WorkerFailure::InvalidSignature,
            3 => WorkerFailure::FuelExhausted,
            4 => WorkerFailure::Trap,
            _ => unreachable!(),
        })),
        _ => Err(invalid("invalid worker response status or payload")),
    }
}

/// Interpreter core intended only for a future sandbox child. This function
/// is deliberately unwired: fuel and store limits do not bound compilation
/// memory or protect host resources if called in the DOXA process.
pub(crate) fn execute_worker(bytes: &[u8]) -> Result<i32, WorkerFailure> {
    packages::validate_module(bytes).map_err(|_| WorkerFailure::InvalidModule)?;
    validate_entry(bytes).map_err(|_| WorkerFailure::InvalidModule)?;
    let mut config = Config::default();
    config.consume_fuel(true);
    let engine = Engine::new(&config);
    let module = Module::new(&engine, bytes).map_err(|_| WorkerFailure::InvalidModule)?;
    let limits: StoreLimits = StoreLimitsBuilder::new()
        .memory_size(MEMORY_BYTES)
        .table_elements(TABLE_ELEMENTS)
        .memories(1)
        .tables(1)
        .instances(1)
        .trap_on_grow_failure(true)
        .build();
    let mut store = Store::new(&engine, limits);
    store.limiter(|limits| limits);
    store.set_fuel(FUEL).map_err(|_| WorkerFailure::InvalidModule)?;
    let instance =
        Instance::new(&mut store, &module, &[]).map_err(|_| WorkerFailure::InvalidModule)?;
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

/// Dedicated child entrypoint. Its only input is one bounded, independently
/// decoded frame on stdin; its only output is the fixed 13-byte response.
/// The parent must launch this binary through the cgroup-backed sandbox seam.
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
    let result = match decode_request(io::stdin().lock()) {
        Ok(bytes) => execute_worker(&bytes),
        Err(_) => Err(WorkerFailure::InvalidModule),
    };
    let mut output = io::stdout().lock();
    output.write_all(&encode_response(result))?;
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
        let dir = tempfile::tempdir().unwrap();
        let package_dir = dir.path().join("native-plugin-packages/demo");
        std::fs::create_dir_all(&package_dir).unwrap();
        for path in [dir.path().to_path_buf(), dir.path().join("native-plugin-packages"), package_dir.clone()] {
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let manifest = b"package_api_version = 1\nname = 'demo'\nversion = '1.0'\nartifact_format = 'wasm-core-v1'\nrequested_grants = []\n";
        write_private(&package_dir.join("manifest.toml"), manifest);
        write_private(&package_dir.join("module.wasm"), bytes);
        write_private(&dir.path().join("config.toml"), format!(
            "[[native_plugin_packages]]\nname = 'demo'\nmanifest_sha256 = '{:x}'\nmodule_sha256 = '{:x}'\ngrants = []\n",
            Sha256::digest(manifest), Sha256::digest(bytes)).as_bytes());
        let review = packages::preflight(dir.path(), "demo").unwrap();
        let checked = packages::recheck_approved(dir.path(), &review).unwrap();
        (dir, checked)
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
            assert!(validate_entry(&module).is_err());
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
    }
}
