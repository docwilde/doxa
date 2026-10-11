//! Nonrecursive admission before syn's recursive parse, visitors and AST drop.
//! Byte bounds alone do not bound a Rust syntax tree's stack depth. This is a
//! conservative complexity check, not another Rust parser; syn still validates
//! admitted syntax. Strings/comments are opaque and never add fake delimiters.

const MAX_NESTING: usize = 32;
const MAX_ACTIVE_TOKENS: usize = 128;

#[derive(Default)]
struct Frame {
    delimiter: u8,
    tokens: usize,
    prefix: bool,
    item: bool,
    item_body: bool,
    abi: bool,
}
impl Frame {
    fn region() -> Self { Self { prefix: true, ..Self::default() } }
    fn reset(&mut self) {
        self.tokens = 0; self.prefix = true; self.item = false; self.abi = false;
    }
    fn word(&mut self, word: &[u8]) {
        if !self.prefix { return; }
        match word {
            b"fn" | b"mod" | b"impl" | b"trait" | b"struct" | b"enum" | b"union"
                | b"macro_rules" | b"macro" => { self.item = true; self.prefix = false; }
            b"pub" | b"unsafe" | b"async" | b"default" | b"const" => { self.abi = false; }
            b"extern" => { self.abi = true; }
            _ => { self.prefix = false; self.abi = false; }
        }
    }
}

fn quoted(bytes: &[u8], mut at: usize) -> usize {
    at += 1;
    while at < bytes.len() {
        match bytes[at] {
            b'\\' => at = (at + 2).min(bytes.len()),
            b'"' => return at + 1,
            _ => at += 1,
        }
    }
    bytes.len() // syn reports an unterminated literal.
}

fn raw_string(bytes: &[u8], start: usize) -> Result<Option<usize>, String> {
    if bytes.get(start) != Some(&b'r') { return Ok(None); }
    let mut at = start + 1;
    while bytes.get(at) == Some(&b'#') { at += 1; }
    if bytes.get(at) != Some(&b'"') { return Ok(None); }
    let hashes = at - start - 1;
    if hashes > 255 { return Err("Rust raw literal exceeds 255 hash delimiters".into()); }
    at += 1;
    while at < bytes.len() {
        if bytes[at] == b'"' {
            let mut end = at + 1;
            while end < at + 1 + hashes && bytes.get(end) == Some(&b'#') { end += 1; }
            if end == at + 1 + hashes { return Ok(Some(end)); }
            at = end;
        } else { at += 1; }
    }
    Ok(Some(bytes.len()))
}

// A lifetime starts with an apostrophe too. Only consume a character literal
// when exactly one scalar/escape is followed by its closing apostrophe.
fn character(content: &str, start: usize) -> Option<usize> {
    let bytes = content.as_bytes();
    let mut at = start + 1;
    if bytes.get(at) == Some(&b'\\') {
        at += 1;
        match *bytes.get(at)? {
            b'x' => at = at.checked_add(3)?,
            b'u' if bytes.get(at + 1) == Some(&b'{') => {
                at += 2;
                while bytes.get(at).is_some_and(|byte| byte.is_ascii_hexdigit() || *byte == b'_') { at += 1; }
                if bytes.get(at) != Some(&b'}') { return None; }
                at += 1;
            }
            _ => at += 1,
        }
    } else {
        let scalar = content.get(at..)?.chars().next()?;
        if matches!(scalar, '\n' | '\r' | '\'') { return None; }
        at += scalar.len_utf8();
    }
    (bytes.get(at) == Some(&b'\'')).then_some(at + 1)
}

pub(super) fn preflight(content: &str) -> Result<(), String> {
    let bytes = content.as_bytes();
    let mut frames = vec![Frame::region()];
    let mut at = 0;
    while at < bytes.len() {
        if bytes[at].is_ascii_whitespace() { at += 1; continue; }
        if bytes.get(at..at + 2) == Some(b"//") {
            while at < bytes.len() && bytes[at] != b'\n' { at += 1; }
            continue;
        }
        if bytes.get(at..at + 2) == Some(b"/*") {
            let mut depth = 1usize; at += 2;
            while at < bytes.len() && depth > 0 {
                match bytes.get(at..at + 2) {
                    Some(b"/*") => { depth += 1; at += 2; }
                    Some(b"*/") => { depth -= 1; at += 2; }
                    _ => at += 1,
                }
            }
            continue;
        }
        let raw_start = if matches!(bytes[at], b'b' | b'c') { at + 1 } else { at };
        let literal = raw_string(bytes, raw_start)?
            .or_else(|| (bytes[at] == b'"').then(|| quoted(bytes, at)))
            .or_else(|| (bytes[at] == b'\'').then(|| character(content, at)).flatten());
        if let Some(end) = literal {
            let frame = frames.last_mut().unwrap();
            frame.tokens += 1;
            if !frame.abi { frame.prefix = false; }
            frame.abi = false;
            at = end;
        } else if bytes[at].is_ascii_alphanumeric() || bytes[at] == b'_' || bytes[at] >= 128 {
            let start = at; at += 1;
            while at < bytes.len() && (bytes[at].is_ascii_alphanumeric() || bytes[at] == b'_' || bytes[at] >= 128) { at += 1; }
            let frame = frames.last_mut().unwrap();
            frame.tokens += 1; frame.word(&bytes[start..at]);
        } else {
            let byte = bytes[at]; at += 1;
            let frame = frames.last_mut().unwrap(); frame.tokens += 1;
            match byte {
                b';' => frame.reset(),
                b'(' | b'[' | b'{' => {
                    if frames.len() > MAX_NESTING {
                        return Err("Rust syntax exceeds 32 nested delimiter groups; recursive parsing withheld".into());
                    }
                    let body = byte == b'{' && frames.last().unwrap().item;
                    let mut child = Frame::region(); child.delimiter = byte; child.item_body = body;
                    frames.push(child);
                }
                b')' | b']' | b'}' => {
                    let expected = match byte { b')' => b'(', b']' => b'[', _ => b'{' };
                    if frames.len() == 1 || frames.last().unwrap().delimiter != expected {
                        return Err("Rust syntax contains mismatched delimiter groups".into());
                    }
                    let child = frames.pop().unwrap();
                    if child.item_body { frames.last_mut().unwrap().reset(); }
                }
                b'#' | b'!' if frame.prefix => {}, // item attributes; their groups remain bounded.
                _ => { frame.prefix = false; frame.abi = false; }
            }
        }
        // Count all active regions, including an enclosing expression with
        // many brace operands/else-if branches. Reset only at statement/item
        // boundaries: resetting at every closing brace misses recursive ASTs.
        if frames.iter().map(|frame| frame.tokens).sum::<usize>() > MAX_ACTIVE_TOKENS {
            return Err("Rust syntax exceeds 128 active statement/item tokens; recursive parsing withheld".into());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ignores_comments_and_all_literal_families_but_keeps_lifetimes() {
        let noise = "{([".repeat(1000);
        let source = format!("/* {noise} /* nested */ */\n// {noise}\nfn f<'a>() {{ let s = r###\"{noise}\"###; let b = br#\"{noise}\"#; let c = cr##\"{noise}\"##; let x = \"{noise}\\\"\"; let q = '{{'; let u = '\\u{{1F600}}'; let b = b'['; }}");
        assert!(preflight(&source).is_ok());
        assert!(syn::parse_file(&source).is_ok());
    }
    #[test]
    fn bounds_recursive_groups_and_chains_without_accumulating_sibling_items() {
        assert!(preflight(&format!("fn f() {{ {}0;{} }}", "{".repeat(500), "}".repeat(500))).is_err());
        assert!(preflight(&format!("fn f() {{ {}true; }}", "!".repeat(500))).is_err());
        assert!(preflight(&format!("fn f() {{ {}0; }}", "if true {} else ".repeat(500))).is_err());
        assert!(preflight(&format!("fn f() {{ {}; }}", (0..500).map(|_| "{} + ").collect::<String>() + "0")).is_err());
        let ordinary = format!("mod outer {{ {} }}", "pub(crate) async fn f() { let x = 1 + 2; }\n".repeat(500));
        assert!(preflight(&ordinary).is_ok());
    }

    #[test]
    fn admitted_boundary_parses_visits_and_drops_on_default_worker_stack() {
        const CHILD: &str = "DOXA_CODEGRAPH_DEPTH_FIXTURE";
        if std::env::var_os(CHILD).is_none() {
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "rust_bounds::tests::admitted_boundary_parses_visits_and_drops_on_default_worker_stack", "--nocapture"])
                .env(CHILD, "1").output().unwrap();
            assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
            return;
        }
        std::thread::spawn(|| {
            for source in [
                format!("fn f() {{ {}0;{} }}", "{".repeat(30), "}".repeat(30)),
                format!("fn f() {{ {}true; }}", "!".repeat(100)),
                format!("fn f() {{ {}; }}", "0 + ".repeat(40) + "0"),
                format!("type T = {}u8{};", "Option<".repeat(30), ">".repeat(30)),
            ] {
                assert!(preflight(&source).is_ok());
                let parsed = crate::parse_rust(&source, "fixture.rs", "fixture", 0, true).unwrap();
                drop(parsed);
            }
        }).join().unwrap();
    }
}
