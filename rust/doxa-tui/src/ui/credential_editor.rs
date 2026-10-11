//! Transient credential entry: no Debug, prompt ownership or persisted draft.
use doxa_vendors::Vendor;
use std::sync::atomic::{AtomicU64, Ordering};
static NEXT_EDITOR: AtomicU64 = AtomicU64::new(1);

pub(super) fn next_token() -> u64 { NEXT_EDITOR.fetch_add(1,Ordering::Relaxed) }

/// Shared transient buffer. No Debug or persisted draft; erased on retirement.
pub(super) struct Buffer { bytes: Vec<u8> }
impl Buffer {
    pub fn new() -> Self {Self {bytes:Vec::with_capacity(4096)}}
    pub fn append(&mut self,text:&str) -> bool {
        if text.is_empty() || self.bytes.len().saturating_add(text.len())>4096
            || !text.bytes().all(|c|(0x20..=0x7e).contains(&c)) {return false;}
        self.bytes.extend_from_slice(text.as_bytes());true
    }
    pub fn backspace(&mut self) {
        if let Some(last)=self.bytes.last_mut() {
            // SAFETY: uniquely borrowed byte; clear before shortening the buffer.
            unsafe {std::ptr::write_volatile(last,0);}
            self.bytes.pop();
        }
    }
    pub fn value(&self)->&str {std::str::from_utf8(&self.bytes).unwrap_or("")}
    pub fn mask(&self)->String {
        let mut mask="•".repeat(self.bytes.len().min(40));
        if self.bytes.len()>40 {mask.push('…');}mask
    }
}
impl Drop for Buffer {
    fn drop(&mut self) {
        for byte in &mut self.bytes {
            // SAFETY: each byte is uniquely borrowed; volatile keeps the erase.
            unsafe {std::ptr::write_volatile(byte,0);}
        }
    }
}
pub(super) struct Editor {pub vendor:Vendor,pub token:u64,buffer:Buffer}
impl Editor {
    pub fn new(vendor:Vendor)->Self {Self {vendor,token:next_token(),buffer:Buffer::new()}}
    pub fn append(&mut self,text:&str)->bool {self.buffer.append(text)}
    pub fn backspace(&mut self) {self.buffer.backspace();}
    pub fn value(&self)->&str {self.buffer.value()}
    pub fn mask(&self)->String {self.buffer.mask()}
}
pub(super) fn label(vendor: Vendor) -> &'static str {
    match vendor {
        Vendor::DeepSeek => "DeepSeek",
        Vendor::Glm => "z.ai",
    }
}
pub(super) fn status(vendor: Vendor) -> &'static str {
    use doxa_vendors::credentials::{status, CredentialStatus};
    match status(vendor) {
        Ok(CredentialStatus::Saved) => "configured (saved)",
        Ok(CredentialStatus::Environment) => "configured (environment)",
        Ok(CredentialStatus::Missing) => "not configured",
        Err(_) => "status unavailable",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn bounded_ascii_input_is_masked_and_backspace_clears_removed_byte() {
        let mut editor = Editor::new(Vendor::DeepSeek);
        assert!(editor.append("fixture-key-value"));
        assert!(!editor.mask().contains("fixture"));
        assert!(!editor.append("\ninvalid"));
        assert!(!editor.append(&"x".repeat(4096)));
        editor.backspace();
        assert_eq!(editor.value(), "fixture-key-valu");
        assert_eq!(editor.buffer.bytes.capacity(), 4096);
    }
}
