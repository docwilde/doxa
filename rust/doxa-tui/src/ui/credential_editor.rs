//! Transient credential entry: no Debug, prompt ownership or persisted draft.
use doxa_vendors::Vendor;

pub(super) struct Editor {
    pub vendor: Vendor,
    bytes: Vec<u8>,
}
impl Editor {
    pub fn new(vendor: Vendor) -> Self {
        Self {
            vendor,
            bytes: Vec::with_capacity(4096),
        }
    }
    pub fn append(&mut self, text: &str) -> bool {
        if text.is_empty()
            || self.bytes.len().saturating_add(text.len()) > 4096
            || !text.bytes().all(|c| (0x20..=0x7e).contains(&c))
        {
            return false;
        }
        self.bytes.extend_from_slice(text.as_bytes());
        true
    }
    pub fn backspace(&mut self) {
        if let Some(last) = self.bytes.last_mut() {
            // SAFETY: this byte is uniquely borrowed and valid for the write.
            unsafe {
                std::ptr::write_volatile(last, 0);
            }
            self.bytes.pop();
        }
    }
    pub fn value(&self) -> &str {
        std::str::from_utf8(&self.bytes).unwrap_or("")
    }
    pub fn mask(&self) -> String {
        let mut mask = "•".repeat(self.bytes.len().min(40));
        if self.bytes.len() > 40 {
            mask.push('…');
        }
        mask
    }
}
impl Drop for Editor {
    fn drop(&mut self) {
        for byte in &mut self.bytes {
            // SAFETY: each byte is uniquely borrowed; volatile prevents removal
            // of the erase when the allocation is subsequently freed.
            unsafe {
                std::ptr::write_volatile(byte, 0);
            }
        }
    }
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
        assert_eq!(editor.bytes.capacity(), 4096);
    }
}
