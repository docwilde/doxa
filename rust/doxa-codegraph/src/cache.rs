//! Bounded, process-local reuse for repeated explicit syntax queries.
//!
//! A hit is still a bounded whole-source read: only parsing and answer
//! assembly are reused. New query shapes automatically reuse one bounded
//! parsed declaration inventory. Nothing is persisted or promoted to semantic binding.

use super::{listed_files, query, scan_digest, source_language, worktree_root,
    Answer, Query, file_bytes_observed, index::SyntaxIndex, query_with_index_and_hooks, MAX_FINAL_REHASH_TIME, MAX_TOTAL_SOURCE_BYTES};
use std::collections::VecDeque;
use std::path::Path;
use std::time::Instant;

const MAX_ENTRIES: usize = 8;
const MAX_RETAINED_ANSWER_BYTES: usize = 256 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Origin { Fresh, Indexed, Revalidated }

#[derive(Debug, Clone, Copy)]
pub struct IndexStats {
    pub files: usize,
    pub rows: usize,
    pub retained_bytes: usize,
    pub unparseable_files: usize,
}

#[derive(Debug, PartialEq, Eq)]
struct Fingerprint { listing: String, rust: String, python: String, modes: String }

#[derive(Debug)]
struct Entry {
    request: Query,
    answer: Answer,
    serialized_bytes: usize,
}

/// Holds at most eight syntax answers from one worktree/source inventory, with
/// a combined serialized size of at most 256 KiB. The in-memory Rust values
/// have allocation overhead in addition to that budget. Use one instance per
/// TUI process; this is neither durable memory nor LORE write authority.
#[derive(Debug, Default)]
pub struct ReadOnlyQueryCache {
    root: Option<std::path::PathBuf>,
    fingerprint: Option<Fingerprint>,
    entries: VecDeque<Entry>,
    retained_answer_bytes: usize,
    index: SyntaxIndex,
}

impl ReadOnlyQueryCache {
    fn clear(&mut self) {
        self.root = None;
        self.fingerprint = None;
        self.entries.clear();
        self.retained_answer_bytes = 0;
        self.index.clear();
    }

    fn remember(&mut self, request: Query, answer: &Answer) {
        let Ok(serialized) = serde_json::to_vec(answer) else { return; };
        let serialized_bytes = serialized.len();
        if serialized_bytes > MAX_RETAINED_ANSWER_BYTES { return; }
        while self.entries.len() >= MAX_ENTRIES
            || self.retained_answer_bytes + serialized_bytes > MAX_RETAINED_ANSWER_BYTES {
            let Some(evicted) = self.entries.pop_front() else { break; };
            self.retained_answer_bytes -= evicted.serialized_bytes;
        }
        self.retained_answer_bytes += serialized_bytes;
        self.entries.push_back(Entry { request, answer: answer.clone(), serialized_bytes });
    }

    /// Automatically prepare one read-only inventory off the UI input path.
    /// This has the same complete source/hash checks as an explicit query.
    pub fn prepare(&mut self, root: &Path) -> Result<IndexStats, String> {
        self.query(root, Query::Symbol("__doxa_index_prepare__".into()))?;
        let (files, rows, retained_bytes, unparseable_files) = self.index.stats();
        if files == 0 { return Err("complete readable source inventory unavailable or exceeds index bounds".into()); }
        Ok(IndexStats { files, rows, retained_bytes, unparseable_files })
    }

    pub fn query(&mut self, root: &Path, request: Query) -> Result<(Answer, Origin), String> {
        let root = match worktree_root(root) {
            Ok(root) => root,
            Err(error) => { self.clear(); return Err(error); }
        };
        // Module resolution also probes ignored and unlisted candidate paths.
        // A listed-source fingerprint cannot invalidate those observations.
        if matches!(request, Query::Modules(_)) {
            self.clear();
            return query(&root, request).map(|answer| (answer, Origin::Fresh));
        }
        if self.root.as_ref() != Some(&root) { self.clear(); }
        if let Some(index) = self.entries.iter().position(|entry| entry.request == request) {
            if fingerprint(&root).ok().as_ref() == self.fingerprint.as_ref() {
                    let entry = self.entries.remove(index).expect("located cache entry");
                    let answer = entry.answer.clone();
                    self.entries.push_back(entry);
                    return Ok((answer, Origin::Revalidated));
            } else {
                // Drift and failed source checks invalidate every answer from
                // the old inventory, including answers for different files.
                self.clear();
            }
        }
        // Unlike an answer hit, a new query reads every supported source and
        // performs both final rehash passes. The declaration index only avoids
        // parsing unchanged files; requested call sites are always reparsed.
        let answer = match query_with_index_and_hooks(&root, request.clone(),
            Some(&mut self.index), |_| {}, |_| {}) {
            Ok(answer) => answer,
            Err(error) => { self.clear(); return Err(error); }
        };
        let origin = if self.index.reused_files > 0 { Origin::Indexed } else { Origin::Fresh };
        if !self.index.listing.is_empty() {
            let (rust, python, modes) = self.index.fingerprints();
            let current = Fingerprint {
                listing: scan_digest(b"doxa-codegraph-listed-paths-v1\0",
                    self.index.listing.iter().map(|path| (path.as_str(), ""))), rust, python, modes,
            };
            if self.fingerprint.as_ref() != Some(&current) {
                self.entries.clear(); self.retained_answer_bytes = 0;
            }
            self.root = Some(root);
            self.fingerprint = Some(current);
            // Tombstones preserve incomplete coverage. They never make an
            // incomplete language digest present or enable cached answers.
            if answer.scan_input_sha256.is_some() && answer.python_scan_input_sha256.is_some() {
                self.remember(request, &answer);
            } else { self.entries.clear(); self.retained_answer_bytes = 0; }
            return Ok((answer, origin));
        }
        self.clear();
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
    let mut modes = std::collections::BTreeMap::new();
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
        let (content, sha, _, mode) = file_bytes_observed(root, path)?;
        *total = total.saturating_add(content.len() as u64);
        if *total > MAX_TOTAL_SOURCE_BYTES {
            return Err("cached code graph source check exceeded 64 MiB per language".into());
        }
        entries.push((path.as_str(), sha));
        modes.insert(path.as_str(), format!("{mode:o}"));
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
                let (content, actual, _, mode) = file_bytes_observed(root, path)?;
                total = total.saturating_add(content.len() as u64);
                if total > MAX_TOTAL_SOURCE_BYTES {
                    return Err("cached code graph source check exceeded 64 MiB per language".into());
                }
                if &actual != expected || modes.get(path).is_none_or(|expected| expected != &format!("{mode:o}")) {
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
        modes: scan_digest(b"doxa-codegraph-source-modes-v1\0", modes.iter().map(|(path, mode)| (*path, mode.as_str()))),
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
        assert_eq!(origin, Origin::Indexed);
        let other = worktree();
        let (_, origin) = cache.query(other.path(), Query::Symbol("first".into())).unwrap();
        assert_eq!(origin, Origin::Fresh);
    }

    #[test]
    fn alternating_queries_preserve_answers_and_evict_the_least_recently_used() {
        let root = worktree();
        let mut cache = ReadOnlyQueryCache::default();
        let queries = [Query::File("lib.rs".into()), Query::Calls("lib.rs".into()),
            Query::Imports("service.py".into())];
        let mut originals = Vec::new();
        for request in &queries {
            let (answer, origin) = cache.query(root.path(), request.clone()).unwrap();
            assert_ne!(origin, Origin::Revalidated);
            originals.push(serde_json::to_value(answer).unwrap());
        }
        for (request, original) in queries.iter().zip(&originals) {
            let (answer, origin) = cache.query(root.path(), request.clone()).unwrap();
            assert_eq!(origin, Origin::Revalidated);
            assert_eq!(&serde_json::to_value(answer).unwrap(), original);
        }

        let mut cache = ReadOnlyQueryCache::default();
        for index in 0..MAX_ENTRIES {
            cache.query(root.path(), Query::Symbol(format!("missing_{index}"))).unwrap();
        }
        assert_eq!(cache.entries.len(), MAX_ENTRIES);
        assert_eq!(cache.query(root.path(), Query::Symbol("missing_0".into())).unwrap().1,
            Origin::Revalidated);
        cache.query(root.path(), Query::Symbol("missing_8".into())).unwrap();
        assert_eq!(cache.query(root.path(), Query::Symbol("missing_0".into())).unwrap().1,
            Origin::Revalidated);
        assert_eq!(cache.query(root.path(), Query::Symbol("missing_1".into())).unwrap().1,
            Origin::Indexed);
        assert_eq!(cache.entries.len(), MAX_ENTRIES);
    }

    #[test]
    fn serialized_byte_budget_evicts_before_the_entry_count_limit() {
        let root = worktree();
        for file in 0..6 {
            let source = (0..80).map(|row| format!("fn f_{file}_{row}_{}() {{}}\n", "x".repeat(180)))
                .collect::<String>();
            fs::write(root.path().join(format!("large_{file}.rs")), source).unwrap();
        }
        let mut cache = ReadOnlyQueryCache::default();
        let mut all_answer_bytes = 0;
        for file in 0..6 {
            let (answer, origin) = cache.query(root.path(), Query::File(format!("large_{file}.rs"))).unwrap();
            assert_ne!(origin, Origin::Revalidated);
            all_answer_bytes += serde_json::to_vec(&answer).unwrap().len();
            assert!(cache.retained_answer_bytes <= MAX_RETAINED_ANSWER_BYTES);
            assert_eq!(cache.retained_answer_bytes,
                cache.entries.iter().map(|entry| entry.serialized_bytes).sum::<usize>());
        }
        assert!(all_answer_bytes > MAX_RETAINED_ANSWER_BYTES);
        assert!(cache.entries.len() < 6);
        assert_eq!(cache.query(root.path(), Query::File("large_0.rs".into())).unwrap().1,
            Origin::Indexed);
    }

    #[test]
    fn all_answers_invalidate_on_source_listing_and_worktree_changes() {
        let root = worktree();
        let other = worktree();
        let mut cache = ReadOnlyQueryCache::default();
        let file = Query::File("lib.rs".into());
        let imports = Query::Imports("service.py".into());
        let prime = |cache: &mut ReadOnlyQueryCache| {
            cache.query(root.path(), file.clone()).unwrap();
            cache.query(root.path(), imports.clone()).unwrap();
            assert_eq!(cache.entries.len(), 2);
        };
        prime(&mut cache);
        fs::write(root.path().join("lib.rs"), "fn changed() {}\n").unwrap();
        assert_eq!(cache.query(root.path(), imports.clone()).unwrap().1, Origin::Fresh);
        assert_eq!(cache.query(root.path(), file.clone()).unwrap().1, Origin::Indexed);

        // An ignored source entering the Git inventory changes the generation,
        // even when neither displayed query names that source.
        fs::write(root.path().join(".gitignore"), "hidden.rs\n").unwrap();
        fs::write(root.path().join("hidden.rs"), "fn hidden() {}\n").unwrap();
        prime(&mut cache);
        assert!(Command::new("git").args(["-C"]).arg(root.path())
            .args(["add", "-f", "hidden.rs"]).status().unwrap().success());
        assert_eq!(cache.query(root.path(), imports.clone()).unwrap().1, Origin::Fresh);
        assert_eq!(cache.query(root.path(), file.clone()).unwrap().1, Origin::Indexed);
        fs::remove_file(root.path().join("lib.rs")).unwrap();
        assert_eq!(cache.query(root.path(), imports.clone()).unwrap().1, Origin::Fresh);
        assert!(cache.query(root.path(), file.clone()).is_err());
        assert!(cache.entries.is_empty());

        cache.query(other.path(), file.clone()).unwrap();
        fs::write(root.path().join("lib.rs"), "fn changed() {}\n").unwrap();
        assert_eq!(cache.query(root.path(), file.clone()).unwrap().1, Origin::Fresh);
        assert_eq!(cache.query(other.path(), file).unwrap().1, Origin::Fresh);
    }

    #[test]
    fn incomplete_scan_discards_other_answers_even_if_original_bytes_return() {
        let root = worktree();
        let mut cache = ReadOnlyQueryCache::default();
        cache.query(root.path(), Query::File("lib.rs".into())).unwrap();
        fs::write(root.path().join("service.py"), "def broken(\n").unwrap();
        let (answer, origin) = cache.query(root.path(), Query::Calls("lib.rs".into())).unwrap();
        assert_eq!(origin, Origin::Fresh);
        assert!(answer.python_scan_input_sha256.is_none());
        assert!(cache.entries.is_empty());
        fs::write(root.path().join("service.py"), "def first(): pass\n").unwrap();
        assert_eq!(cache.query(root.path(), Query::File("lib.rs".into())).unwrap().1,
            Origin::Indexed);
    }

    #[test]
    fn incomplete_scan_never_enters_cache_and_late_edit_fails_fingerprint() {
        let root = worktree();
        fs::write(root.path().join("service.py"), "def broken(\n").unwrap();
        let mut cache = ReadOnlyQueryCache::default();
        let (_, first) = cache.query(root.path(), Query::File("lib.rs".into())).unwrap();
        let (_, second) = cache.query(root.path(), Query::File("lib.rs".into())).unwrap();
        assert_eq!((first, second), (Origin::Fresh, Origin::Indexed));

        fs::write(root.path().join("service.py"), "def first(): pass\n").unwrap();
        let error = fingerprint_with_pre_final_pass(root.path(), |root| {
            fs::write(root.join("lib.rs"), "fn late() {}\n").unwrap();
        }).err().unwrap();
        assert!(error.contains("source changed during recheck: lib.rs"), "{error}");
    }

    #[test]
    fn automatic_index_retains_tombstones_without_complete_language_claims() {
        let root = worktree();
        fs::write(root.path().join("service.py"), "def broken(\n").unwrap();
        let mut cache = ReadOnlyQueryCache::default();
        let stats = cache.prepare(root.path()).unwrap();
        assert_eq!((stats.files, stats.unparseable_files), (2, 1));
        let (answer, origin) = cache.query(root.path(), Query::Symbol("first".into())).unwrap();
        assert_eq!(origin, Origin::Indexed);
        assert_eq!(answer.rows.len(), 1);
        assert!(answer.python_scan_input_sha256.is_none());
        assert_eq!(answer.coverage.python_unparseable_files, 1);
        assert!(cache.entries.is_empty());
        fs::write(root.path().join("service.py"), "def repaired(): pass\n").unwrap();
        let (repaired, _) = cache.query(root.path(), Query::Symbol("repaired".into())).unwrap();
        assert!(repaired.python_scan_input_sha256.is_some());
        assert_eq!(repaired.coverage.python_unparseable_files, 0);
        assert_eq!(repaired.rows.len(), 1);
    }

    #[test]
    fn indexed_tombstone_and_mode_are_rechecked_before_return() {
        use std::os::unix::fs::PermissionsExt;
        let root = worktree();
        fs::write(root.path().join("service.py"), "def broken(\n").unwrap();
        let mut index = SyntaxIndex::default();
        let error = query_with_index_and_hooks(root.path(), Query::Symbol("first".into()),
            Some(&mut index), |_| {}, |root| {
                fs::write(root.join("service.py"), "def changed(\n").unwrap();
            }).unwrap_err();
        assert!(error.contains("hash/mode changed"), "{error}");
        let mut cache = ReadOnlyQueryCache::default();
        fs::write(root.path().join("service.py"), "def first(): pass\n").unwrap();
        cache.query(root.path(), Query::File("lib.rs".into())).unwrap();
        fs::set_permissions(root.path().join("lib.rs"), fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(cache.query(root.path(), Query::File("lib.rs".into())).unwrap().1, Origin::Fresh);
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
