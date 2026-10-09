//! Explicit, read-only identity review for future executable plugins.
//! Nothing in this module loads, compiles, instantiates, or runs the module.
use super::{identifier, invalid, open_child, open_dir, plain, private, MAX_CONFIG};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs::{File, Metadata};
use std::io;
use std::os::unix::fs::MetadataExt;
use std::path::Path;

const MAX_PACKAGE_MANIFEST: u64 = 16 * 1024;
const MAX_MODULE: u64 = 8 * 1024 * 1024;
const MAX_PACKAGES: usize = 16;
const WASM_CORE_V1: &[u8; 8] = b"\0asm\x01\0\0\0";
const GRANTS: &[&str] = &["render-local-panel-v1"];

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    package_api_version: u32,
    name: String,
    version: String,
    artifact_format: String,
    #[serde(default)]
    requested_grants: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OwnerApproval {
    name: String,
    manifest_sha256: String,
    module_sha256: String,
    grants: Vec<String>,
}

/// A review result, never an activation token. A future runner must re-open and
/// re-hash the files and perform its own sandbox and grant enforcement.
#[derive(Debug)]
pub struct Review {
    pub name: String,
    pub version: String,
    pub manifest_sha256: String,
    pub module_sha256: String,
    pub requested_grants: Vec<String>,
    pub owner_approved: bool,
    pub manifest_inode: (u64, u64),
    pub module_inode: (u64, u64),
}

impl Review {
    pub fn report(&self) -> String {
        let grants = if self.requested_grants.is_empty() { "none".to_owned() }
            else { self.requested_grants.join(", ") };
        format!("Package: {} {}\nFormat: wasm-core-v1\nManifest SHA-256: {}\nModule SHA-256: {}\nRequested grants: {}\nOwner approval: {}\nOpened inodes (device:inode): manifest {}:{}, module {}:{}\nExecution: unavailable (no plugin runner)\n",
            self.name, self.version, self.manifest_sha256, self.module_sha256,
            grants, if self.owner_approved { "exact identity and grants match" } else { "review required" },
            self.manifest_inode.0, self.manifest_inode.1, self.module_inode.0, self.module_inode.1)
    }
}

fn digest(bytes: &[u8]) -> String { format!("{:x}", Sha256::digest(bytes)) }

fn valid_digest(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_grants(grants: &[String]) -> bool {
    grants.len() <= GRANTS.len() && grants.iter().all(|grant| GRANTS.contains(&grant.as_str()))
        && grants.iter().collect::<HashSet<_>>().len() == grants.len()
}

fn stable(file: &File, before: &Metadata) -> io::Result<()> {
    let after = file.metadata()?;
    private(&after, false, true)?;
    if before.dev() != after.dev() || before.ino() != after.ino()
        || before.len() != after.len() || before.mtime() != after.mtime()
        || before.mtime_nsec() != after.mtime_nsec()
        || before.ctime() != after.ctime() || before.ctime_nsec() != after.ctime_nsec() {
        return Err(invalid("plugin package file changed during review"));
    }
    Ok(())
}

fn approvals(home: &File) -> io::Result<Vec<OwnerApproval>> {
    let (file, bytes, before) = match open_child(home, "config.toml", false, true, MAX_CONFIG) {
        Ok(value) => value,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error),
    };
    stable(&file, &before)?;
    let text = std::str::from_utf8(&bytes).map_err(|_| invalid("DOXA config must be UTF-8"))?;
    let config: toml::Table = toml::from_str(text).map_err(|error| invalid(format!("invalid DOXA config: {error}")))?;
    let Some(value) = config.get("native_plugin_packages") else { return Ok(Vec::new()); };
    let rows = value.as_array().ok_or_else(|| invalid("native_plugin_packages must be an array of tables"))?;
    if rows.len() > MAX_PACKAGES { return Err(invalid("too many plugin package approvals")); }
    let mut seen = HashSet::new();
    rows.iter().map(|row| {
        let approval: OwnerApproval = row.clone().try_into()
            .map_err(|error| invalid(format!("invalid plugin package approval: {error}")))?;
        if !identifier(&approval.name) || !seen.insert(approval.name.clone())
            || !valid_digest(&approval.manifest_sha256) || !valid_digest(&approval.module_sha256)
            || !valid_grants(&approval.grants) {
            return Err(invalid("invalid or duplicate plugin package approval"));
        }
        Ok(approval)
    }).collect()
}

/// Review only the package named by the operator. No directory enumeration,
/// repository lookup, provider inference, or executable invocation occurs.
pub fn preflight(home: &Path, name: &str) -> io::Result<Review> {
    if !identifier(name) { return Err(invalid("invalid plugin package name")); }
    let home_dir = open_dir(home)?;
    private(&home_dir.metadata()?, true, true)?;
    if home.canonicalize()?.ancestors().any(|ancestor| ancestor.join(".git").exists()) {
        return Err(invalid("plugin package home must be outside a working repository"));
    }
    let approved = approvals(&home_dir)?;
    let (packages, _, _) = open_child(&home_dir, "native-plugin-packages", true, true, 0)?;
    let (package, _, _) = open_child(&packages, name, true, true, 0)?;
    let (manifest_file, manifest_bytes, manifest_meta) =
        open_child(&package, "manifest.toml", false, true, MAX_PACKAGE_MANIFEST)?;
    stable(&manifest_file, &manifest_meta)?;
    let text = std::str::from_utf8(&manifest_bytes).map_err(|_| invalid("package manifest must be UTF-8"))?;
    let manifest: Manifest = toml::from_str(text)
        .map_err(|error| invalid(format!("invalid package manifest: {error}")))?;
    if manifest.package_api_version != 1 || manifest.name != name || !identifier(&manifest.name)
        || !plain(&manifest.version, 64, false) || manifest.artifact_format != "wasm-core-v1"
        || !valid_grants(&manifest.requested_grants) {
        return Err(invalid("unsupported or invalid plugin package identity or grants"));
    }
    let (module_file, module_bytes, module_meta) =
        open_child(&package, "module.wasm", false, true, MAX_MODULE)?;
    stable(&module_file, &module_meta)?;
    if !module_bytes.starts_with(WASM_CORE_V1) {
        return Err(invalid("plugin package artifact is not a WebAssembly core module"));
    }
    let manifest_sha256 = digest(&manifest_bytes);
    let module_sha256 = digest(&module_bytes);
    let owner_approved = if let Some(row) = approved.iter().find(|row| row.name == name) {
        if row.manifest_sha256 != manifest_sha256 || row.module_sha256 != module_sha256
            || row.grants != manifest.requested_grants {
            return Err(invalid("owner approval does not match exact package identity and grants"));
        }
        true
    } else { false };
    Ok(Review { name: manifest.name, version: manifest.version, manifest_sha256,
        module_sha256, requested_grants: manifest.requested_grants, owner_approved,
        manifest_inode: (manifest_meta.dev(), manifest_meta.ino()),
        module_inode: (module_meta.dev(), module_meta.ino()) })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{symlink, PermissionsExt};

    const MANIFEST: &str = "package_api_version = 1\nname = 'demo'\nversion = '1.0'\nartifact_format = 'wasm-core-v1'\nrequested_grants = ['render-local-panel-v1']\n";
    const MODULE: &[u8] = b"\0asm\x01\0\0\0";

    fn write(path: &Path, bytes: impl AsRef<[u8]>) {
        std::fs::write(path, bytes).unwrap();
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
    }

    fn fixture() -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        for path in [dir.path().to_path_buf(), dir.path().join("native-plugin-packages"),
            dir.path().join("native-plugin-packages/demo")] {
            std::fs::create_dir_all(&path).unwrap();
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        write(&dir.path().join("native-plugin-packages/demo/manifest.toml"), MANIFEST);
        write(&dir.path().join("native-plugin-packages/demo/module.wasm"), MODULE);
        dir
    }

    fn approval() -> String {
        format!("[[native_plugin_packages]]\nname = 'demo'\nmanifest_sha256 = '{}'\nmodule_sha256 = '{}'\ngrants = ['render-local-panel-v1']\n",
            digest(MANIFEST.as_bytes()), digest(MODULE))
    }

    #[test]
    fn explicit_review_never_activates_a_package() {
        let dir = fixture();
        let review = preflight(dir.path(), "demo").unwrap();
        assert!(!review.owner_approved);
        assert!(review.report().contains("Execution: unavailable"));
        assert!(super::super::load(dir.path(), &[]).unwrap().commands.is_empty());
        write(&dir.path().join("config.toml"), approval());
        let review = preflight(dir.path(), "demo").unwrap();
        assert!(review.owner_approved);
        assert!(super::super::load(dir.path(), &[]).unwrap().commands.is_empty());
        assert!(preflight(dir.path(), "hidden").is_err());
    }

    #[test]
    fn changed_module_or_grant_invalidates_exact_owner_approval() {
        let dir = fixture();
        write(&dir.path().join("config.toml"), approval());
        let module = dir.path().join("native-plugin-packages/demo/module.wasm");
        write(&module, [MODULE, b"different"].concat());
        assert!(preflight(dir.path(), "demo").unwrap_err().to_string().contains("approval"));
        write(&module, MODULE);
        let manifest = dir.path().join("native-plugin-packages/demo/manifest.toml");
        write(&manifest, MANIFEST.replace("render-local-panel-v1", "unknown-grant"));
        assert!(preflight(dir.path(), "demo").is_err());
        write(&manifest, MANIFEST.replace("1.0", "1.1"));
        assert!(preflight(dir.path(), "demo").unwrap_err().to_string().contains("approval"));
        write(&manifest, MANIFEST);
        write(&dir.path().join("config.toml"), approval().replace("grants = ['render-local-panel-v1']", "grants = []"));
        assert!(preflight(dir.path(), "demo").unwrap_err().to_string().contains("approval"));
    }

    #[test]
    fn unsafe_paths_and_config_fail_closed() {
        let dir = fixture();
        let module = dir.path().join("native-plugin-packages/demo/module.wasm");
        std::fs::remove_file(&module).unwrap();
        symlink(dir.path().join("outside.wasm"), &module).unwrap();
        assert!(preflight(dir.path(), "demo").is_err());
        std::fs::remove_file(&module).unwrap();
        write(&module, MODULE);
        std::fs::set_permissions(&module, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(preflight(dir.path(), "demo").is_err());
        std::fs::set_permissions(&module, std::fs::Permissions::from_mode(0o600)).unwrap();
        std::fs::hard_link(&module, dir.path().join("native-plugin-packages/demo/other.wasm")).unwrap();
        assert!(preflight(dir.path(), "demo").is_err());
        std::fs::remove_file(dir.path().join("native-plugin-packages/demo/other.wasm")).unwrap();
        write(&dir.path().join("config.toml"), approval());
        std::fs::set_permissions(dir.path().join("config.toml"), std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(preflight(dir.path(), "demo").is_err());
        std::fs::create_dir(dir.path().join(".git")).unwrap();
        assert!(preflight(dir.path(), "demo").is_err());
        assert!(preflight(dir.path(), "../demo").is_err());
    }

    #[test]
    fn duplicate_or_unknown_approvals_are_not_interpreted() {
        let dir = fixture();
        write(&dir.path().join("config.toml"), format!("{}{}", approval(), approval()));
        assert!(preflight(dir.path(), "demo").is_err());
        write(&dir.path().join("config.toml"), approval().replace("name = 'demo'", "name = 'demo'\nexec = '/bin/sh'"));
        assert!(preflight(dir.path(), "demo").is_err());
    }

    #[test]
    fn unknown_fields_and_unsupported_artifacts_are_rejected() {
        let dir = fixture();
        let manifest = dir.path().join("native-plugin-packages/demo/manifest.toml");
        write(&manifest, format!("{MANIFEST}exec = '/bin/sh'\n"));
        assert!(preflight(dir.path(), "demo").is_err());
        write(&manifest, MANIFEST);
        let module = dir.path().join("native-plugin-packages/demo/module.wasm");
        write(&module, b"#!/bin/sh\n");
        assert!(preflight(dir.path(), "demo").is_err());
        write(&module, vec![0; MAX_MODULE as usize + 1]);
        assert!(preflight(dir.path(), "demo").is_err());
    }
}
