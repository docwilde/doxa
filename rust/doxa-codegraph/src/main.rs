use doxa_codegraph::{query_cli, semantic_producer::plan_rust_analyzer, semantic_runtime::unavailable_status};

fn main() {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if args.first().is_some_and(|arg| arg == "--semantic-probe") {
        let result = match args.as_slice() {
            [_, root_flag, root, image_flag, image, host_flag, host]
                if root_flag == "--root" && image_flag == "--image" && host_flag == "--docker-host" => {
                plan_rust_analyzer(std::path::Path::new(root), image, host, true)
                    .map(|plan| unavailable_status(&plan))
            }
            _ => Err("usage: doxa-codegraph --semantic-probe --root WORKTREE --image NAME@sha256:DIGEST --docker-host unix:///run/user/UID/docker.sock".into()),
        };
        match result {
            Ok(status) => println!("{}", status),
            Err(error) => { eprintln!("code graph: {error}"); std::process::exit(1); }
        }
        return;
    }
    match query_cli(&args) {
        Ok(answer) => println!("{}", serde_json::to_string(&answer).expect("serializable query answer")),
        Err(error) => { eprintln!("code graph: {error}"); std::process::exit(1); }
    }
}
