//! Explicit, read-only alternating-query benchmark. No automatic indexing.
use doxa_codegraph::{cache::{Origin, ReadOnlyQueryCache}, query, worktree_root, Answer, Query};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::Path;
use std::time::{Duration, Instant};

fn stable_answer_sha256(answer: &Answer) -> Result<String, String> {
    fn remove_read_times(value: &mut Value) {
        match value {
            Value::Object(fields) => {
                fields.retain(|name, _| !name.ends_with("_unix_ms"));
                for child in fields.values_mut() { remove_read_times(child); }
            }
            Value::Array(items) => { for child in items { remove_read_times(child); } }
            _ => {}
        }
    }
    let mut value = serde_json::to_value(answer).map_err(|error| error.to_string())?;
    remove_read_times(&mut value);
    let bytes = serde_json::to_vec(&value).map_err(|error| error.to_string())?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

fn summary(samples: &[Value]) -> Value {
    let mut elapsed = samples.iter().map(|sample| sample["elapsed_ms"].as_f64().unwrap())
        .collect::<Vec<_>>();
    elapsed.sort_by(f64::total_cmp);
    let n = elapsed.len();
    let median = if n % 2 == 0 { (elapsed[n / 2 - 1] + elapsed[n / 2]) / 2.0 }
        else { elapsed[n / 2] };
    json!({"n": n, "p50_ms": median, "p95_ms": elapsed[(n * 95).div_ceil(100) - 1],
        "min_ms": elapsed[0], "max_ms": elapsed[n - 1]})
}

fn run() -> Result<Value, String> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    let (root, file, runs) = match args.as_slice() {
        [root_flag, root, file_flag, file] if root_flag == "--root" && file_flag == "--file" =>
            (root, file, 5),
        [root_flag, root, file_flag, file, runs_flag, runs]
            if root_flag == "--root" && file_flag == "--file" && runs_flag == "--runs" =>
            (root, file, runs.parse::<usize>().map_err(|_| "invalid run count")?),
        _ => return Err("usage: bench_query_cache --root WORKTREE --file RELATIVE_SOURCE [--runs 1..10]".into()),
    };
    if !(1..=10).contains(&runs) { return Err("run count must be between 1 and 10".into()); }
    let root = worktree_root(Path::new(root))?;
    let requests = [Query::File(file.clone()), Query::Calls(file.clone()), Query::Imports(file.clone())];
    let started = Instant::now();
    let mut baseline = Vec::new();
    let mut source_basis = Value::Null;
    let mut cache = ReadOnlyQueryCache::default();
    let mut phases = Vec::new();
    for (phase, loops) in [("fresh", runs), ("cache_empty", 1), ("cache_warmed", runs)] {
        let mut samples = Vec::new();
        for round in 0..loops {
            for (index, request) in requests.iter().enumerate() {
                if started.elapsed() >= Duration::from_secs(120) {
                    return Err("benchmark exceeded two-minute overall budget".into());
                }
                let sample_started = Instant::now();
                let (answer, origin) = if phase == "fresh" {
                    (query(&root, request.clone())?, Origin::Fresh)
                } else { cache.query(&root, request.clone())? };
                let elapsed_ms = sample_started.elapsed().as_secs_f64() * 1000.0;
                if answer.scan_input_sha256.is_none() || answer.python_scan_input_sha256.is_none() {
                    return Err("benchmark requires complete Rust and Python source inventories".into());
                }
                let digest = stable_answer_sha256(&answer)?;
                if phase == "fresh" && round == 0 {
                    let basis = json!({"rust": answer.scan_input_sha256,
                        "python": answer.python_scan_input_sha256,
                        "requested_source": answer.requested_source_sha256,
                        "enumerated_files": answer.coverage.enumerated_files,
                        "parsed_rust_files": answer.coverage.parsed_rust_files,
                        "parsed_python_files": answer.coverage.parsed_python_files});
                    if index == 0 { source_basis = basis; }
                    else if basis != source_basis { return Err("source inventory changed between queries".into()); }
                    baseline.push(digest.clone());
                } else if digest != baseline[index] {
                    return Err(format!("source or answer changed during {phase} {} query", answer.query));
                }
                if (phase == "cache_warmed") != (origin == Origin::Revalidated) {
                    return Err(format!("unexpected cache origin in {phase}"));
                }
                samples.push(json!({"round": round + 1, "query": answer.query,
                    "elapsed_ms": elapsed_ms, "origin": if origin == Origin::Fresh { "fresh" }
                        else { "revalidated" }, "answer_sha256_without_read_times": digest,
                    "observed_unix_ms": answer.observed_unix_ms}));
            }
        }
        phases.push(json!({"phase": phase, "summary": summary(&samples), "samples": samples}));
    }
    Ok(json!({"benchmark": "explicit_alternating_query_cache", "version": env!("CARGO_PKG_VERSION"),
        "root": root, "file": file, "runs": runs, "source_basis": source_basis,
        "cache_cold_definition": "empty answer cache; filesystem cache is uncontrolled",
        "comparison": "same normalized answers and complete source inventory across every sample; non-atomic byte observations",
        "elapsed_ms": started.elapsed().as_secs_f64() * 1000.0, "phases": phases}))
}

fn main() {
    match run() {
        Ok(report) => println!("{}", serde_json::to_string_pretty(&report).expect("serializable report")),
        Err(error) => { eprintln!("code graph benchmark: {error}"); std::process::exit(1); }
    }
}
