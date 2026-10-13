//! One bounded process-local syntax inventory. No AST, source bytes, semantic
//! bindings or durable memory are retained. Every use still rereads sources.

use super::{Parsed, PythonParsed, Row, file_bytes_observed, source_language, scan_digest, MAX_FINAL_REHASH_TIME, MAX_TOTAL_SOURCE_BYTES};
use std::{path::Path, time::Instant};
use std::collections::{BTreeMap, BTreeSet};

const MAX_INDEX_BYTES: usize = 16 * 1024 * 1024;
const MAX_INDEX_ROWS: usize = 100_000;

#[derive(Debug)]
struct Entry {
    sha256: String,
    symbols: Vec<Row>,
    imports: Vec<Row>,
    macro_items: usize,
    unsupported_syntax: usize,
    retained_bytes: usize,
    error: Option<String>,
}

#[derive(Debug, Default)]
pub(super) struct SyntaxIndex {
    entries: BTreeMap<String, Entry>,
    observed: BTreeMap<String, (String, u32)>,
    pub listing: Vec<String>,
    retained_bytes: usize,
    rows: usize,
    overflowed: bool,
    pub reused_files: usize,
}

impl SyntaxIndex {
    pub fn clear(&mut self) { *self = Self::default(); }

    pub fn begin(&mut self, paths: &BTreeSet<String>) {
        if !self.listing.is_empty() && self.listing.iter().ne(paths.iter()) { self.clear(); }
        self.reused_files = 0;
        self.observed.clear();
    }

    pub fn stats(&self) -> (usize, usize, usize, usize) {
        (self.entries.len(), self.rows, self.retained_bytes, self.entries.values().filter(|entry| entry.error.is_some()).count())
    }

    fn get(&mut self, path: &str, sha256: &str) -> Option<&Entry> {
        let entry = self.entries.get(path)?;
        if entry.sha256 != sha256 { return None; }
        self.reused_files += 1;
        Some(entry)
    }

    pub fn rust(&mut self, path: &str, sha256: &str) -> Option<Parsed> {
        if self.entries.get(path)?.error.is_some() { return None; }
        let entry = self.get(path, sha256)?;
        Some(Parsed { symbols: entry.symbols.clone(), imports: entry.imports.clone(),
            calls: Vec::new(), modules: Vec::new(), nested_modules: 0,
            macro_items: entry.macro_items, unsupported_syntax: entry.unsupported_syntax })
    }

    pub fn python(&mut self, path: &str, sha256: &str) -> Option<PythonParsed> {
        if self.entries.get(path)?.error.is_some() { return None; }
        let entry = self.get(path, sha256)?;
        Some(PythonParsed { symbols: entry.symbols.clone(), imports: entry.imports.clone(),
            calls: Vec::new(), call_sites: 0, modules: Vec::new(), module_sites: 0 })
    }

    pub fn remember(&mut self, path: &str, sha256: &str, symbols: &[Row], imports: &[Row],
        macro_items: usize, unsupported_syntax: usize) {
        if self.overflowed || self.entries.get(path).is_some_and(|entry| entry.sha256 == sha256) {
            return;
        }
        // Capacity plus struct sizes account for all retained variable-sized
        // strings/arrays. The per-file allowance includes BTree nodes/pointers.
        let row_bytes = |row: &Row| std::mem::size_of::<Row>() + row.file.capacity()
            + row.name.capacity() + row.qualified.capacity() + row.sha256.capacity()
            + row.alias.as_ref().map_or(0, String::capacity);
        let bytes = 256 + path.len() + sha256.len()
            + symbols.iter().chain(imports).map(row_bytes).sum::<usize>();
        let rows = symbols.len() + imports.len();
        if self.retained_bytes.saturating_add(bytes) > MAX_INDEX_BYTES
            || self.rows.saturating_add(rows) > MAX_INDEX_ROWS {
            self.entries.clear(); self.rows = 0; self.retained_bytes = 0;
            self.overflowed = true;
            return;
        }
        if let Some(previous) = self.entries.remove(path) {
            self.retained_bytes -= previous.retained_bytes;
            self.rows -= previous.symbols.len() + previous.imports.len();
        }
        self.retained_bytes += bytes;
        self.rows += rows;
        self.entries.insert(path.into(), Entry { sha256: sha256.into(),
            symbols: symbols.to_vec(), imports: imports.to_vec(), macro_items, unsupported_syntax, retained_bytes: bytes, error: None });
    }

    pub fn observe(&mut self, path: &str, sha: &str, mode: u32) {
        self.observed.insert(path.into(), (sha.into(), mode));
    }

    pub fn error(&mut self, path: &str, sha: &str) -> Option<String> {
        let entry = self.entries.get(path)?;
        if entry.sha256 != sha { return None; }
        let error = entry.error.clone()?;
        self.reused_files += 1;
        Some(error)
    }

    pub fn remember_error(&mut self, path: &str, sha: &str, error: &str) {
        self.remember(path, sha, &[], &[], 0, 0);
        if let Some(entry) = self.entries.get_mut(path) {
            if entry.error.is_none() {
                self.retained_bytes += error.len();
                entry.retained_bytes += error.len();
                entry.error = Some(error.into());
            }
        }
        if self.retained_bytes > MAX_INDEX_BYTES { self.entries.clear(); self.rows = 0; self.retained_bytes = 0; self.overflowed = true; }
    }

    pub fn fingerprints(&self) -> (String, String, String) {
        let language = |wanted| scan_digest(if wanted == "rust" { b"doxa-rust-scan-input-v1\0" } else { b"doxa-python-scan-input-v1\0" },
            self.observed.iter().filter(|(path, _)| source_language(path) == Some(wanted))
                .map(|(path, (sha, _))| (path.as_str(), sha.as_str())));
        let modes = self.observed.iter().map(|(path, (_, mode))| (path.as_str(), format!("{mode:o}"))).collect::<Vec<_>>();
        (language("rust"), language("python"), scan_digest(b"doxa-codegraph-source-modes-v1\0",
            modes.iter().map(|(path, mode)| (*path, mode.as_str()))))
    }

    pub fn verify(&self, root: &Path, started: Instant) -> Result<(), String> {
        let mut rust = 0u64; let mut python = 0u64;
        for (path, (expected, expected_mode)) in &self.observed {
            if started.elapsed() >= MAX_FINAL_REHASH_TIME { return Err("indexed source recheck exceeded ten seconds".into()); }
            let (content, actual, _, mode) = file_bytes_observed(root, path)?;
            let total = if source_language(path) == Some("rust") { &mut rust } else { &mut python };
            *total += content.len() as u64;
            if *total > MAX_TOTAL_SOURCE_BYTES { return Err("indexed source recheck exceeded64MiB per language".into()); }
            if &actual != expected || &mode != expected_mode {
                return Err(format!("indexed source hash/mode changed during recheck: {path}"));
            }
        }
        Ok(())
    }

    pub fn finish(&mut self, paths: &BTreeSet<String>, complete: bool) {
        if !complete || self.overflowed { self.clear(); }
        else { self.listing = paths.iter().cloned().collect(); }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn source_hash_and_capacity_are_admission_requirements() {
        let mut index = SyntaxIndex::default();
        index.remember("a.rs", "first", &[], &[], 0, 0);
        assert!(index.rust("a.rs", "different").is_none());
        assert!(index.rust("a.rs", "first").is_some());
        for file in 0..MAX_INDEX_BYTES / 256 + 1 {
            index.remember(&format!("file_{file}.rs"), "sha", &[], &[], 0, 0);
        }
        assert!(index.overflowed);
        assert_eq!(index.stats(), (0, 0, 0, 0));
        index.finish(&BTreeSet::new(), true);
        assert_eq!(index.stats(), (0, 0, 0, 0));
    }
}
