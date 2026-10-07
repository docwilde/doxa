//! Bounded, authenticated remote envelopes. The broker never receives the key.
use aes_gcm::{aead::{Aead, AeadCore, KeyInit, Payload, OsRng}, Aes256Gcm, Nonce};
use base64::{engine::general_purpose::STANDARD_NO_PAD, Engine};
use flate2::{read::DeflateDecoder, write::DeflateEncoder, Compression};
use serde_json::{json, Value};
use std::{fs::{self, OpenOptions}, io::{self, Read, Write}, os::unix::fs::{MetadataExt, PermissionsExt, OpenOptionsExt}, path::Path};

const MAX_PLAIN: usize = 128_000;
const MAX_CIPHER: usize = 128_000;
const MIN_COMPRESS: usize = 1_024;
const MAX_PADDING: usize = 4_096;

fn invalid(message: &'static str) -> io::Error { io::Error::new(io::ErrorKind::InvalidData, message) }

/// A copied, owner-only key file is deliberately outside the hub protocol.
pub fn key_from_file(path: &Path) -> io::Result<[u8; 32]> {
    if !path.is_absolute() { return Err(invalid("remote key path must be absolute")); }
    let file = OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC).open(path)?;
    let metadata = file.metadata()?;
    if !metadata.file_type().is_file() || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o077 != 0 || metadata.nlink() != 1 {
        return Err(invalid("remote key must be a private, owner-only regular file"));
    }
    let mut data = Vec::new();
    file.take(128).read_to_end(&mut data)?;
    let text = std::str::from_utf8(&data).map_err(|_| invalid("invalid remote key"))?.trim();
    let bytes = STANDARD_NO_PAD.decode(text).map_err(|_| invalid("invalid remote key"))?;
    let key: [u8; 32] = bytes.try_into().map_err(|_| invalid("remote key must have 32 bytes"))?;
    Ok(key)
}

pub fn configured_key() -> io::Result<Option<[u8; 32]>> {
    match std::env::var_os("DOXA_REMOTE_E2EE_KEY_FILE") {
        None => Ok(None),
        Some(path) => key_from_file(Path::new(&path)).map(Some),
    }
}

/// Write a new key without replacing an existing file or exposing it via argv.
pub fn create_key(path: &Path) -> io::Result<()> {
    if !path.is_absolute() { return Err(invalid("remote key path must be absolute")); }
    let parent=path.parent().ok_or_else(||invalid("remote key needs a parent directory"))?;
    let parent_meta=fs::metadata(parent)?;
    if !parent_meta.is_dir() || parent_meta.uid()!=unsafe{libc::geteuid()}
        || parent_meta.permissions().mode() & 0o022 != 0 {
        return Err(invalid("remote key directory must be owned and not writable by others"));
    }
    let first = Aes256Gcm::generate_nonce(&mut OsRng);
    let second = Aes256Gcm::generate_nonce(&mut OsRng);
    // Three independent CSPRNG draws provide 32 bytes for a fresh key.
    let third = Aes256Gcm::generate_nonce(&mut OsRng);
    let mut key = [0u8; 32];
    key[..12].copy_from_slice(&first);
    key[12..24].copy_from_slice(&second);
    key[24..].copy_from_slice(&third[..8]);
    let mut file = OpenOptions::new().write(true).create_new(true).mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC).open(path)?;
    file.write_all(STANDARD_NO_PAD.encode(key).as_bytes())?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    Ok(())
}

fn compress(plain: &[u8]) -> io::Result<(Vec<u8>, bool)> {
    if plain.len() < MIN_COMPRESS { return Ok((plain.to_vec(), false)); }
    let mut encoder = DeflateEncoder::new(Vec::new(), Compression::fast());
    encoder.write_all(plain)?;
    let zipped = encoder.finish()?;
    if zipped.len() + 32 < plain.len() { Ok((zipped, true)) } else { Ok((plain.to_vec(), false)) }
}

fn decompress(data: &[u8], compressed: bool) -> io::Result<Vec<u8>> {
    if !compressed { return Ok(data.to_vec()); }
    let decoder = DeflateDecoder::new(data);
    let mut plain = Vec::new();
    decoder.take((MAX_PLAIN + 1) as u64).read_to_end(&mut plain)?;
    if plain.len() > MAX_PLAIN { return Err(invalid("remote plaintext exceeds bound")); }
    Ok(plain)
}

/// `context` binds ciphertext to its route, target, direction, and sequence.
pub fn seal(key: &[u8; 32], context: &str, value: &Value) -> io::Result<Value> {
    let plain = serde_json::to_vec(value)?;
    if plain.len() > MAX_PLAIN { return Err(invalid("remote plaintext exceeds bound")); }
    let (data, compressed) = compress(&plain)?;
    // Length buckets prevent the broker from seeing a fine-grained compression oracle.
    let target = (data.len()+5).next_multiple_of(MAX_PADDING);
    if target > MAX_CIPHER { return Err(invalid("remote envelope exceeds bound")); }
    let mut padded=Vec::with_capacity(target);
    padded.push(u8::from(compressed));
    padded.extend_from_slice(&(data.len() as u32).to_be_bytes());
    padded.extend_from_slice(&data);
    padded.resize(target, 0);
    let cipher = Aes256Gcm::new_from_slice(key).map_err(|_| invalid("cipher unavailable"))?;
    let nonce = Aes256Gcm::generate_nonce(&mut OsRng);
    let aad = format!("doxa-remote-v1|{context}");
    let ciphertext = cipher.encrypt(&nonce, Payload { msg: &padded, aad: aad.as_bytes() })
        .map_err(|_| invalid("remote encryption failed"))?;
    Ok(json!({"v":1,"alg":"A256GCM",
        "nonce":STANDARD_NO_PAD.encode(nonce),"data":STANDARD_NO_PAD.encode(ciphertext)}))
}

pub fn open(key: &[u8; 32], context: &str, envelope: &Value) -> io::Result<Value> {
    if envelope["v"] != 1 || envelope["alg"] != "A256GCM" { return Err(invalid("unknown remote envelope")); }
    let decode = |field: &str, max: usize| -> io::Result<Vec<u8>> {
        let encoded = envelope[field].as_str().filter(|text| text.len() <= max * 2)
            .ok_or_else(|| invalid("invalid remote envelope field"))?;
        STANDARD_NO_PAD.decode(encoded).map_err(|_| invalid("invalid remote base64"))
    };
    let nonce = decode("nonce", 12)?;
    if nonce.len() != 12 { return Err(invalid("invalid remote nonce")); }
    let ciphertext = decode("data", MAX_CIPHER + 16)?;
    if ciphertext.len() < 16 || ciphertext.len() > MAX_CIPHER + 16 { return Err(invalid("remote ciphertext exceeds bound")); }
    let cipher = Aes256Gcm::new_from_slice(key).map_err(|_| invalid("cipher unavailable"))?;
    let aad = format!("doxa-remote-v1|{context}");
    let padded = cipher.decrypt(Nonce::from_slice(&nonce), Payload { msg: &ciphertext, aad: aad.as_bytes() })
        .map_err(|_| invalid("remote authentication failed"))?;
    if padded.len()<5 || padded.len() % MAX_PADDING != 0 { return Err(invalid("invalid remote padding")); }
    let compressed=match padded[0]{0=>false,1=>true,_=>return Err(invalid("invalid remote compression flag"))};
    let length=u32::from_be_bytes(padded[1..5].try_into().unwrap()) as usize;
    if length>padded.len()-5 || padded[5+length..].iter().any(|byte|*byte!=0){return Err(invalid("invalid remote payload length"));}
    let plain = decompress(&padded[5..5+length], compressed)?;
    if plain.len() > MAX_PLAIN { return Err(invalid("remote plaintext exceeds bound")); }
    serde_json::from_slice(&plain).map_err(|_| invalid("invalid remote plaintext"))
}

#[cfg(test)] mod tests {
    use super::*;
    #[test] fn compressed_roundtrip_and_context_binding() {
        let key = [7u8; 32];
        let value = json!({"text":"repeated secret ".repeat(4_000)});
        let sealed = seal(&key, "host~session|result|transcript", &value).unwrap();
        assert!(sealed.get("zip").is_none());
        assert!(sealed["data"].as_str().unwrap().len() < value.to_string().len());
        assert_eq!(open(&key, "host~session|result|transcript", &sealed).unwrap(), value);
        assert!(open(&key, "other~session|result|transcript", &sealed).is_err());
        let mut altered = sealed.clone(); altered["data"] = json!("AAAA");
        assert!(open(&key, "host~session|result|transcript", &altered).is_err());
    }
    #[test] fn key_file_is_private_and_never_replaced() {
        let root=tempfile::tempdir_in(std::env::current_dir().unwrap()).unwrap();
        fs::set_permissions(root.path(),fs::Permissions::from_mode(0o700)).unwrap();
        let path=root.path().join("remote.key");
        create_key(&path).unwrap();
        let key=key_from_file(&path).unwrap();
        assert_ne!(key,[0u8;32]);
        assert!(create_key(&path).is_err());
        fs::set_permissions(&path,fs::Permissions::from_mode(0o644)).unwrap();
        assert!(key_from_file(&path).is_err());
    }
    #[test] fn forged_or_oversized_envelope_fails_closed() {
        let key=[4u8;32];
        let ciphertext=seal(&key,"target|command|prompt",&json!({"text":"private"})).unwrap();
        assert!(open(&key,"target|command|answer",&ciphertext).is_err());
        let mut forged=ciphertext;
        forged["nonce"]=json!("AAAA");
        assert!(open(&key,"target|command|prompt",&forged).is_err());
        assert!(seal(&key,"target|result|transcript",&json!({"text":"x".repeat(MAX_PLAIN+1)})).is_err());
    }
}
