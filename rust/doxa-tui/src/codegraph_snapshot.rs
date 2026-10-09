//! Explicit, read-only export of one fresh syntax answer plus LORE's curated
//! file-map candidates. LORE does not yet own a durable codegraph protocol.
use doxa_codegraph::Answer;
use doxa_lore::FileMap;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const MAX_EXPORT_BYTES: usize = 96 * 1024;

pub fn export(answer: Answer, file_map: FileMap) -> Result<Value, &'static str> {
    if !matches!(answer.query, "file" | "imports" | "calls" | "modules")
        || answer.status != "ok" || answer.requested_source_sha256.is_none() {
        return Err("only a parsed file query can be exported with a curated file-map overlay");
    }
    let answer_bytes = serde_json::to_vec(&answer).map_err(|_| "query serialization failed")?;
    let query_sha256 = format!("{:x}", Sha256::digest(&answer_bytes));
    // Exact path equality is deliberate. LORE's map may contain absolute,
    // host-prefixed, stale, or competing entries; none proves code binding.
    let candidates = file_map.entries.iter().filter(|entry| entry.path == answer.value)
        .map(|entry| json!({"path":entry.path,"purpose":entry.purpose})).collect::<Vec<_>>();
    let resolution = match candidates.len() {
        0 => "unknown", 1 => "curated_unverified", _ => "ambiguous",
    };
    let value = json!({
        "schema_version": 1,
        "storage": "export_only_not_persisted",
        "query_sha256": query_sha256,
        "graph_binding": "unknown",
        "graph": answer,
        "curated_purpose": {
            "project_key": file_map.key,
            "resolution": resolution,
            "candidates": candidates,
            "freshness": "unverified_no_lore_source_hash"
        }
    });
    if serde_json::to_vec(&value).map_or(true, |bytes| bytes.len() > MAX_EXPORT_BYTES) {
        return Err("snapshot export exceeds 96 KiB");
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use doxa_codegraph::{query, Query};
    use doxa_lore::FileMapEntry;
    use std::{fs, process::Command};

    #[test]
    fn export_keeps_hashes_and_ambiguity_without_claiming_binding_or_storage() {
        let root = tempfile::tempdir().unwrap();
        assert!(Command::new("git").args(["init", "-q"]).arg(root.path()).status().unwrap().success());
        fs::write(root.path().join("lib.rs"), "mod child;\n").unwrap();
        fs::write(root.path().join("child.rs"), "pub fn one() {}\n").unwrap();
        fs::create_dir(root.path().join("child")).unwrap();
        fs::write(root.path().join("child/mod.rs"), "pub fn two() {}\n").unwrap();
        let answer = query(root.path(), Query::Modules("lib.rs".into())).unwrap();
        assert_eq!(answer.module_edges[0].reason, "ambiguous_layout");
        let map = FileMap { key: "fixture".into(), cap_chars: 4400, entries: vec![
            FileMapEntry { path: "lib.rs".into(), purpose: "first".into() },
            FileMapEntry { path: "lib.rs".into(), purpose: "second".into() },
            FileMapEntry { path: "child.rs".into(), purpose: "other file".into() },
        ] };
        let exported = export(answer, map.clone()).unwrap();
        assert_eq!(exported["storage"], "export_only_not_persisted");
        assert_eq!(exported["graph_binding"], "unknown");
        assert_eq!(exported["curated_purpose"]["resolution"], "ambiguous");
        assert_eq!(exported["curated_purpose"]["candidates"].as_array().unwrap().len(), 2);
        assert_eq!(exported["graph"]["module_edges"][0]["reason"], "ambiguous_layout");
        assert_eq!(exported["graph"]["module_edges"][0]["target"], Value::Null);
        assert_eq!(exported["graph"]["requested_source_sha256"].as_str().unwrap().len(), 64);
        assert_eq!(exported["query_sha256"].as_str().unwrap().len(), 64);
        fs::write(root.path().join("lib.rs"), "mod child;\n// changed\n").unwrap();
        let changed = export(query(root.path(), Query::Modules("lib.rs".into())).unwrap(), map).unwrap();
        assert_ne!(exported["graph"]["requested_source_sha256"], changed["graph"]["requested_source_sha256"]);
        assert_ne!(exported["query_sha256"], changed["query_sha256"]);
    }
}
