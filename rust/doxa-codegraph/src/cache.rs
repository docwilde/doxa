//! One-entry, process-local reuse for repeated explicit syntax queries.
//!
//! A hit is still a bounded whole-source read: only parsing and answer
//! assembly are reused. Nothing is persisted or promoted to semantic binding.

use super::{file_bytes, listed_files, query, scan_digest, source_language, worktree_root,
    Answer, Query, MAX_FINAL_REHASH_TIME, MAX_TOTAL_SOURCE_BYTES};
use std::path::Path;
use std::time::Instant;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Origin { Fresh, Revalidated }

#[derive(Debug, PartialEq, Eq)]
struct Fingerprint { listing: String, rust: String, python: String }

#[derive(Debug)]
struct Entry {
    root: std::path::PathBuf,
    request: Query,
    fingerprint: Fingerprint,
    answer: Answer,
}

/// Holds at most one serialized-answer-sized syntax result and its bounded
/// fingerprint. Use one instance per TUI process; do not share it as durable
/// memory or use it to authorize a LORE snapshot write.
#[derive(Debug, Default)]
pub struct ReadOnlyQueryCache { entry: Option<Entry> }

impl ReadOnlyQueryCache {
    pub fn query(&mut self, root: &Path, request: Query) -> Result<(Answer, Origin), String> {
        let root = worktree_root(root)?;
        // Module resolution also probes ignored and unlisted candidate paths.
        // A listed-source fingerprint cannot invalidate those observations.
        if matches!(request, Query::Modules(_)) {
            self.entry = None;
            return query(&root, request).map(|answer| (answer, Origin::Fresh));
        }
        if let Some(entry) = &self.entry {
            if entry.root == root && entry.request == request
                && fingerprint(&root).ok().as_ref() == Some(&entry.fingerprint) {
                return Ok((entry.answer.clone(), Origin::Revalidated));
            }
        }
        self.entry = None;
        let before = listed_files(&root)?;
        let answer = query(&root, request.clone())?;
        // Incomplete language scans cannot be reused: the fresh answer may
        // legitimately report skipped or unparseable inputs, but a cache hit
        // must have a complete source inventory for both languages.
        if let (Some(rust), Some(python)) = (&answer.scan_input_sha256,
            &answer.python_scan_input_sha256) {
            if let Ok(current) = fingerprint(&root) {
                let before_listing = scan_digest(b"doxa-codegraph-listed-paths-v1\0",
                    before.iter().map(|path| (path.as_str(), "")));
                if current.listing == before_listing
                    && &current.rust == rust && &current.python == python {
                    self.entry = Some(Entry { root, request, fingerprint: current,
                        answer: answer.clone() });
                }
            }
        }
        Ok((answer, Origin::Fresh))
    }
}

fn fingerprint(root: &Path) -> Result<Fingerprint, String> {
    fingerprint_with_pre_final_pass(root, |_| {})
}

fn fingerprint_with_pre_final_pass(root: &Path,
    before_final_pass: impl FnOnce(&Path)) -> Result<Fingerprint, String> {
    let started = Instant::now();
    let paths = listed_files(root)?;
    let mut rust = Vec::new();
    let mut python = Vec::new();
    let mut rust_total = 0u64;
    let mut python_total = 0u64;
    for path in &paths {
        let (entries, total) = match source_language(path) {
            Some("rust") => (&mut rust, &mut rust_total),
            Some("python") => (&mut python, &mut python_total),
            _ => continue,
        };
        if started.elapsed() >= MAX_FINAL_REHASH_TIME {
            return Err("cached code graph source check exceeded ten seconds".into());
        }
        let (content, sha, _) = file_bytes(root, path)?;
        *total = total.saturating_add(content.len() as u64);
        if *total > MAX_TOTAL_SOURCE_BYTES {
            return Err("cached code graph source check exceeded 64 MiB per language".into());
        }
        entries.push((path.as_str(), sha));
    }
    // Repeat *both* languages after the first read. An early Rust edit during
    // the Python pass otherwise leaves the old Rust digest apparently valid.
    let mut before_final_pass = Some(before_final_pass);
    for pass in 0..2 {
        if pass == 1 { before_final_pass.take().expect("one final pass hook")(root); }
        for entries in [&rust, &python] {
            let mut total = 0u64;
            for (path, expected) in entries {
                if started.elapsed() >= MAX_FINAL_REHASH_TIME {
                    return Err("cached code graph source check exceeded ten seconds".into());
                }
                let (content, actual, _) = file_bytes(root, path)?;
                total = total.saturating_add(content.len() as u64);
                if total > MAX_TOTAL_SOURCE_BYTES {
                    return Err("cached code graph source check exceeded 64 MiB per language".into());
                }
                if &actual != expected {
                    return Err(format!("cached code graph source changed during recheck: {path}"));
                }
            }
        }
    }
    if started.elapsed() >= MAX_FINAL_REHASH_TIME {
        return Err("cached code graph source check exceeded ten seconds".into());
    }
    if listed_files(root)? != paths {
        return Err("Git worktree listing changed during cached code graph source check".into());
    }
    Ok(Fingerprint {
        listing: scan_digest(b"doxa-codegraph-listed-paths-v1\0",
            paths.iter().map(|path| (path.as_str(), ""))),
        rust: scan_digest(b"doxa-rust-scan-input-v1\0",
            rust.iter().map(|(path, sha)| (*path, sha.as_str()))),
        python: scan_digest(b"doxa-python-scan-input-v1\0",
            python.iter().map(|(path, sha)| (*path, sha.as_str()))),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::process::Command;
    use tempfile::TempDir;

    fn worktree() -> TempDir {
        let root = tempfile::tempdir().unwrap();
        assert!(Command::new("git").arg("init").arg("-q")
            .arg(root.path()).status().unwrap().success());
        fs::write(root.path().join("lib.rs"), "fn first() {}\n").unwrap();
        fs::write(root.path().join("service.py"), "def first(): pass\n").unwrap();
        root
    }

    #[test]
    fn identical_complete_query_reuses_only_after_whole_source_recheck() {
        let root = worktree();
        let mut cache = ReadOnlyQueryCache::default();
        let request = Query::File("lib.rs".into());
        let (first, origin) = cache.query(root.path(), request.clone()).unwrap();
        assert_eq!(origin, Origin::Fresh);
        let (again, origin) = cache.query(root.path(), request.clone()).unwrap();
        assert_eq!(origin, Origin::Revalidated);
        assert_eq!(again.observed_unix_ms, first.observed_unix_ms);
        assert_eq!(again.scan_input_sha256, first.scan_input_sha256);
        assert_eq!(again.python_scan_input_sha256, first.python_scan_input_sha256);

        fs::write(root.path().join("lib.rs"), "fn changed() {}\n").unwrap();
        let (changed, origin) = cache.query(root.path(), request.clone()).unwrap();
        assert_eq!(origin, Origin::Fresh);
        assert_ne!(changed.scan_input_sha256, first.scan_input_sha256);
        fs::write(root.path().join("service.py"), "def changed(): pass\n").unwrap();
        let (changed_python, origin) = cache.query(root.path(), request).unwrap();
        assert_eq!(origin, Origin::Fresh);
        assert_ne!(changed_python.python_scan_input_sha256, first.python_scan_input_sha256);
    }

    #[test]
    fn path_listing_and_query_identity_invalidate_reuse() {
        let root = worktree();
        let mut cache = ReadOnlyQueryCache::default();
        cache.query(root.path(), Query::File("lib.rs".into())).unwrap();
        fs::write(root.path().join("README.md"), "added\n").unwrap();
        let (answer, origin) = cache.query(root.path(), Query::File("lib.rs".into())).unwrap();
        assert_eq!(origin, Origin::Fresh);
        assert_eq!(answer.coverage.enumerated_files, 3);
        let (_, origin) = cache.query(root.path(), Query::Symbol("first".into())).unwrap();
        assert_eq!(origin, Origin::Fresh);
        let other = worktree();
        let (_, origin) = cache.query(other.path(), Query::Symbol("first".into())).unwrap();
        assert_eq!(origin, Origin::Fresh);
    }

    #[test]
    fn incomplete_scan_never_enters_cache_and_late_edit_fails_fingerprint() {
        let root = worktree();
        fs::write(root.path().join("service.py"), "def broken(\n").unwrap();
        let mut cache = ReadOnlyQueryCache::default();
        let (_, first) = cache.query(root.path(), Query::File("lib.rs".into())).unwrap();
        let (_, second) = cache.query(root.path(), Query::File("lib.rs".into())).unwrap();
        assert_eq!((first, second), (Origin::Fresh, Origin::Fresh));

        fs::write(root.path().join("service.py"), "def first(): pass\n").unwrap();
        let error = fingerprint_with_pre_final_pass(root.path(), |root| {
            fs::write(root.join("lib.rs"), "fn late() {}\n").unwrap();
        }).err().unwrap();
        assert!(error.contains("source changed during recheck: lib.rs"), "{error}");
    }

    #[test]
    fn modules_requery_when_an_ignored_candidate_appears() {
        let root = worktree();
        fs::write(root.path().join("service.py"), "import plain\n").unwrap();
        fs::write(root.path().join(".gitignore"), "plain/\n").unwrap();
        let mut cache = ReadOnlyQueryCache::default();
        let (before, origin) = cache.query(root.path(), Query::Modules("service.py".into())).unwrap();
        assert_eq!(origin, Origin::Fresh);
        fs::create_dir(root.path().join("plain")).unwrap();
        fs::write(root.path().join("plain/__init__.py"), "# ignored candidate\n").unwrap();
        assert_eq!(listed_files(root.path()).unwrap().len(), 3);
        let (after, origin) = cache.query(root.path(), Query::Modules("service.py".into())).unwrap();
        assert_eq!(origin, Origin::Fresh);
        assert_ne!(before.module_edges[0].reason, after.module_edges[0].reason);
        assert_eq!(after.module_edges[0].reason, "python_unlisted_or_uncheckable_candidate");
    }
}
