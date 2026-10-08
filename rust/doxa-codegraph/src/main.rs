use doxa_codegraph::query_cli;

fn main() {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    match query_cli(&args) {
        Ok(answer) => println!("{}", serde_json::to_string(&answer).expect("serializable query answer")),
        Err(error) => { eprintln!("code graph: {error}"); std::process::exit(1); }
    }
}
