//! Complete read/parse/answer latency for novel queries after automatic index
//! preparation. Each answer is compared with a fresh query over identical bytes.
use doxa_codegraph::{cache::{ReadOnlyQueryCache, Origin}, query, worktree_root, Query, Answer};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{path::Path, time::{Duration, Instant}};

fn normalized(answer: &Answer) -> String {
    fn clean(value: &mut Value) {
        match value {
            Value::Object(fields) => {
                fields.retain(|name, _| !name.ends_with("_unix_ms"));
                for value in fields.values_mut() { clean(value); }
            }
            Value::Array(values) => { for value in values { clean(value); } }
            _ => {}
        }
    }
    let mut value = serde_json::to_value(answer).unwrap(); clean(&mut value);
    format!("{:x}", Sha256::digest(serde_json::to_vec(&value).unwrap()))
}
fn run() -> Result<Value, String> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if args.len() != 4 || args[0] != "--root" || args[2] != "--file" {
        return Err("usage: bench_index --root WORKTREE --file SOURCE".into());
    }
    let root = worktree_root(Path::new(&args[1]))?;
    let started = Instant::now(); let mut samples = Vec::new();
    for pair in 0..5 {
        let mut cache = ReadOnlyQueryCache::default();
        let prep = Instant::now(); let stats = cache.prepare(&root)?;
        let prepare_ms = prep.elapsed().as_secs_f64() * 1000.0;
        let requests = [Query::File(args[3].clone()), Query::Calls(args[3].clone()),
            Query::Imports(args[3].clone()), Query::Symbol(format!("__missing_pair_{pair}__"))];
        for request in requests {
            if started.elapsed() > Duration::from_secs(120) { return Err("benchmark120s deadline".into()); }
            let mut outputs = Vec::new();
            for indexed in if pair % 2 == 0 { [false,true] } else { [true,false] } {
                let timer = Instant::now();
                let (answer, origin) = if indexed { cache.query(&root, request.clone())? }
                    else { (query(&root, request.clone())?, Origin::Fresh) };
                outputs.push((indexed, timer.elapsed().as_secs_f64() * 1000.0,
                    normalized(&answer), format!("{origin:?}")));
            }
            if outputs[0].2 != outputs[1].2 { return Err("normalized answer/source inventory changed".into()); }
            samples.push(json!({"pair":pair+1,"query":format!("{request:?}"),
                "prepare_ms":prepare_ms,"indexed_files":stats.files,"indexed_rows":stats.rows,
                "retained_bytes":stats.retained_bytes,"unparseable_files":stats.unparseable_files,"samples":outputs}));
        }
    }
    Ok(json!({"benchmark":"prepared_inventory_novel_queries","root":root,
        "source_cache_state":"warm/uncontrolled; use dedicated eviction harness for cold sources",
        "controls":"five alternating-order pairs; identical normalized answers and complete inventories",
        "samples":samples,"elapsed_ms":started.elapsed().as_secs_f64()*1000.0}))
}
fn main() { match run() { Ok(report)=>println!("{}",serde_json::to_string_pretty(&report).unwrap()),
    Err(error)=>{eprintln!("{error}");std::process::exit(1);} } }
