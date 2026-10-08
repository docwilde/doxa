//! Bounded, read-only syntax queries over the bytes in one Git worktree.
//! No persistent index exists: each answer names the exact bytes it parsed.
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::Read;
use std::ffi::CString;
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

const MAX_FILES: usize = 20_000;
const MAX_PATH_BYTES: usize = 4_096;
const MAX_LIST_BYTES: usize = 4 * 1024 * 1024;
const MAX_SOURCE_BYTES: u64 = 1024 * 1024;
const MAX_TOTAL_SOURCE_BYTES: u64 = 64 * 1024 * 1024;
const MAX_ROWS: usize = 100;
const MAX_ISSUE_EXAMPLES: usize = 20;
const MAX_REPLY_BYTES: usize = 64 * 1024;

pub enum Query { File(String), Symbol(String), Imports(String) }

pub fn query_cli(args: &[String]) -> Result<Answer, String> {
    let (root, rest) = if args.first().is_some_and(|arg| arg == "--root") {
        let path = args.get(1).ok_or("missing --root path")?;
        (PathBuf::from(path), &args[2..])
    } else { (PathBuf::from("."), args) };
    let request = match rest {
        [kind, value] if kind == "file" => Query::File(value.clone()),
        [kind, value] if kind == "symbol" => Query::Symbol(value.clone()),
        [kind, value] if kind == "imports" => Query::Imports(value.clone()),
        _ => return Err("usage: doxa codegraph [--root WORKTREE] file PATH | symbol NAME | imports PATH".into()),
    };
    query(&root, request)
}

#[derive(Debug, Serialize)]
pub struct Answer {
    pub scope: String,
    pub query: &'static str,
    pub value: String,
    pub observed_unix_ms: u128,
    pub status: String,
    pub coverage: Coverage,
    pub rows: Vec<Row>,
    pub omitted_rows: usize,
    pub note: &'static str,
    pub fallback: Option<&'static str>,
}

#[derive(Debug, Default, Serialize)]
pub struct Coverage {
    pub enumerated_files: usize,
    pub parsed_rust_files: usize,
    pub unsupported_languages: BTreeMap<String, usize>,
    pub other_files: usize,
    pub unparseable: Issues,
    pub skipped: Issues,
    pub macro_items: usize,
    pub unsupported_syntax: usize,
}

#[derive(Debug, Default, Serialize)]
pub struct Issues { pub count: usize, pub examples: Vec<Issue> }
#[derive(Debug, Serialize)]
pub struct Issue { pub file: String, pub reason: String }
impl Issues {
    fn add(&mut self, file: &str, reason: impl Into<String>) {
        self.count += 1;
        if self.examples.len() < MAX_ISSUE_EXAMPLES {
            self.examples.push(Issue { file: file.to_owned(), reason: reason.into() });
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Row {
    pub kind: &'static str,
    pub file: String,
    pub line: usize,
    pub name: String,
    pub qualified: String,
    pub alias: Option<String>,
    pub glob: bool,
    pub sha256: String,
    pub read_unix_ms: u128,
}

const NOTE: &str = "Rust syntax only; imports are declarations, not resolved dependencies. cfg and macro expansion are not evaluated. Function-local definitions and imports, references, calls, and non-Rust languages are not indexed.";

fn source_language(path: &str) -> Option<&'static str> {
    match Path::new(path).extension().and_then(|s| s.to_str()) {
        Some("rs") => Some("rust"), Some("py") => Some("python"),
        Some("js" | "jsx" | "mjs" | "cjs") => Some("javascript"),
        Some("ts" | "tsx") => Some("typescript"),
        Some("go") => Some("go"), Some("c" | "h") => Some("c"),
        Some("cc" | "cpp" | "cxx" | "hpp") => Some("cpp"),
        Some("java") => Some("java"), Some("kt" | "kts") => Some("kotlin"),
        Some("swift") => Some("swift"), Some("sh" | "bash") => Some("shell"),
        _ => None,
    }
}

fn worktree_root(path: &Path) -> Result<PathBuf, String> {
    let requested = fs::canonicalize(path).map_err(|e| format!("worktree path: {e}"))?;
    let output = Command::new("git").arg("-C").arg(&requested)
        .args(["rev-parse", "--show-toplevel"]).output()
        .map_err(|e| format!("git unavailable: {e}"))?;
    if !output.status.success() { return Err("path is not inside a Git worktree".into()); }
    let root = String::from_utf8(output.stdout).map_err(|_| "non-UTF-8 Git root")?;
    let root = root.strip_suffix('\n').ok_or("invalid Git root reply")?;
    fs::canonicalize(root).map_err(|e| format!("Git root: {e}"))
}

fn listed_files(root: &Path) -> Result<BTreeSet<String>, String> {
    let output = Command::new("git").arg("-C").arg(root)
        .args(["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
        .output().map_err(|e| format!("Git enumeration: {e}"))?;
    if !output.status.success() || output.stdout.len() > MAX_LIST_BYTES {
        return Err("Git worktree enumeration failed or exceeded its byte limit; no partial answer".into());
    }
    let mut paths = BTreeSet::new();
    for bytes in output.stdout.split(|byte| *byte == 0).filter(|bytes| !bytes.is_empty()) {
        if bytes.len() > MAX_PATH_BYTES { return Err("file path exceeds limit; no partial answer".into()); }
        let path = String::from_utf8(bytes.to_vec()).map_err(|_| "non-UTF-8 file path; no partial answer")?;
        if Path::new(&path).is_absolute() || Path::new(&path).components().any(|part| matches!(part, std::path::Component::ParentDir)) {
            return Err("unsafe Git file path; no partial answer".into());
        }
        paths.insert(path);
        if paths.len() > MAX_FILES { return Err("file count exceeds limit; no partial answer".into()); }
    }
    Ok(paths)
}

fn file_bytes(root: &Path, relative: &str) -> Result<(String, String, u128), String> {
    // Anchor each component to an opened worktree descriptor. A repository
    // writer may replace a symlink between path validation and File::open;
    // canonicalize + symlink_metadata followed by a pathname open leaks the
    // target's bytes in that race.
    let expected_root = fs::metadata(root).map_err(|e| format!("worktree metadata: {e}"))?;
    let mut directory = fs::OpenOptions::new().read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(root).map_err(|e| format!("worktree open: {e}"))?;
    let opened_root = directory.metadata().map_err(|e| format!("worktree descriptor: {e}"))?;
    if (expected_root.dev(), expected_root.ino()) != (opened_root.dev(), opened_root.ino()) {
        return Err("worktree changed during open".into());
    }
    let parts = Path::new(relative).components().map(|part| match part {
        std::path::Component::Normal(name) => CString::new(name.as_bytes())
            .map_err(|_| "NUL in Git file path".to_owned()),
        _ => Err("unsafe Git file path".to_owned()),
    }).collect::<Result<Vec<_>, _>>()?;
    if parts.is_empty() { return Err("empty Git file path".into()); }
    let mut file = None;
    for (index, part) in parts.iter().enumerate() {
        let last = index + 1 == parts.len();
        let flags = libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW
            | if last { libc::O_NONBLOCK } else { libc::O_DIRECTORY };
        let fd = unsafe { libc::openat(directory.as_raw_fd(), part.as_ptr(), flags) };
        if fd < 0 { return Err(format!("file open: {}", std::io::Error::last_os_error())); }
        let opened = unsafe { File::from_raw_fd(fd) };
        if last { file = Some(opened); } else { directory = opened; }
    }
    let mut file = file.expect("nonempty Git path");
    let metadata = file.metadata().map_err(|e| format!("file metadata: {e}"))?;
    if !metadata.is_file() { return Err("not a regular file (symlinks are skipped)".into()); }
    if metadata.len() > MAX_SOURCE_BYTES { return Err("source file exceeds 1 MiB limit".into()); }
    let mut bytes = Vec::new();
    file.by_ref().take(MAX_SOURCE_BYTES + 1).read_to_end(&mut bytes).map_err(|e| format!("file read: {e}"))?;
    let after = file.metadata().map_err(|e| format!("file metadata after read: {e}"))?;
    if bytes.len() as u64 > MAX_SOURCE_BYTES || metadata.len() != after.len()
        || metadata.modified().ok() != after.modified().ok() {
        return Err("source changed during read or exceeded 1 MiB limit".into());
    }
    let sha = format!("{:x}", Sha256::digest(&bytes));
    let read_unix_ms = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis();
    let content = String::from_utf8(bytes).map_err(|_| "non-UTF-8 Rust source")?;
    Ok((content, sha, read_unix_ms))
}

struct Parsed { symbols: Vec<Row>, imports: Vec<Row>, macro_items: usize, unsupported_syntax: usize }
fn row(kind: &'static str, file: &str, line: usize, name: String, qualified: String,
    alias: Option<String>, glob: bool, sha: &str, read_unix_ms: u128) -> Row {
    Row { kind, file: file.into(), line, name, qualified, alias, glob, sha256: sha.into(), read_unix_ms }
}
fn qualified(scope: &str, name: &str) -> String {
    if scope.is_empty() { name.into() }
    else if scope == "::" { format!("::{name}") }
    else { format!("{scope}::{name}") }
}
fn rust_type(ty: &syn::Type) -> Option<String> {
    let syn::Type::Path(path) = ty else { return None; };
    if path.qself.is_some() || path.path.segments.iter().any(|segment| !matches!(segment.arguments, syn::PathArguments::None)) { return None; }
    Some(path.path.segments.iter().map(|segment| segment.ident.to_string()).collect::<Vec<_>>().join("::"))
}
fn flatten_use(tree: &syn::UseTree, prefix: &str, file: &str, scope: &str, line: usize,
    sha: &str, read_unix_ms: u128, rows: &mut Vec<Row>) {
    match tree {
        syn::UseTree::Path(path) => flatten_use(&path.tree, &qualified(prefix, &path.ident.to_string()), file, scope, line, sha, read_unix_ms, rows),
        syn::UseTree::Name(name) => {
            let path = qualified(prefix, &name.ident.to_string());
            rows.push(row("import", file, line, path.clone(), qualified(scope, &path), None, false, sha, read_unix_ms));
        }
        syn::UseTree::Rename(rename) => {
            let path = qualified(prefix, &rename.ident.to_string());
            rows.push(row("import", file, line, path.clone(), qualified(scope, &path), Some(rename.rename.to_string()), false, sha, read_unix_ms));
        }
        syn::UseTree::Glob(_) => {
            let path = qualified(prefix, "*");
            rows.push(row("import", file, line, path.clone(), qualified(scope, &path), None, true, sha, read_unix_ms));
        }
        syn::UseTree::Group(group) => for item in &group.items { flatten_use(item, prefix, file, scope, line, sha, read_unix_ms, rows); },
    }
}
fn add_symbol(parsed: &mut Parsed, kind: &'static str, ident: &syn::Ident, scope: &str,
    file: &str, sha: &str, read_unix_ms: u128) {
    let name = ident.to_string();
    parsed.symbols.push(row(kind, file, ident.span().start().line, name.clone(), qualified(scope, &name), None, false, sha, read_unix_ms));
}
fn walk_items(items: &[syn::Item], scope: &str, file: &str, sha: &str, read_unix_ms: u128, parsed: &mut Parsed) {
    for item in items {
        match item {
            syn::Item::Fn(value) => add_symbol(parsed, "function", &value.sig.ident, scope, file, sha, read_unix_ms),
            syn::Item::Struct(value) => add_symbol(parsed, "struct", &value.ident, scope, file, sha, read_unix_ms),
            syn::Item::Enum(value) => add_symbol(parsed, "enum", &value.ident, scope, file, sha, read_unix_ms),
            syn::Item::Union(value) => add_symbol(parsed, "union", &value.ident, scope, file, sha, read_unix_ms),
            syn::Item::Type(value) => add_symbol(parsed, "type", &value.ident, scope, file, sha, read_unix_ms),
            syn::Item::Const(value) => add_symbol(parsed, "const", &value.ident, scope, file, sha, read_unix_ms),
            syn::Item::Static(value) => add_symbol(parsed, "static", &value.ident, scope, file, sha, read_unix_ms),
            syn::Item::Trait(value) => {
                add_symbol(parsed, "trait", &value.ident, scope, file, sha, read_unix_ms);
                let owner = qualified(scope, &value.ident.to_string());
                for child in &value.items {
                    if let syn::TraitItem::Fn(method) = child { add_symbol(parsed, "trait_method", &method.sig.ident, &owner, file, sha, read_unix_ms); }
                }
            }
            syn::Item::Mod(value) => {
                add_symbol(parsed, if value.content.is_some() { "inline_module" } else { "module_decl" }, &value.ident, scope, file, sha, read_unix_ms);
                if let Some((_, nested)) = &value.content {
                    let next = qualified(scope, &value.ident.to_string());
                    walk_items(nested, &next, file, sha, read_unix_ms, parsed);
                }
            }
            syn::Item::Impl(value) => {
                if let Some(target) = rust_type(&value.self_ty) {
                    let owner = if let Some((_, path, _)) = &value.trait_ {
                        format!("impl[{} for {}]", path.segments.iter().map(|part| part.ident.to_string()).collect::<Vec<_>>().join("::"), target)
                    } else { format!("impl[{target}]") };
                    let owner = qualified(scope, &owner);
                    for child in &value.items {
                        if let syn::ImplItem::Fn(method) = child { add_symbol(parsed, "method", &method.sig.ident, &owner, file, sha, read_unix_ms); }
                    }
                } else { parsed.unsupported_syntax += 1; }
            }
            syn::Item::Use(value) => {
                let prefix = if value.leading_colon.is_some() { "::" } else { "" };
                flatten_use(&value.tree, prefix, file, scope, value.use_token.span.start().line, sha, read_unix_ms, &mut parsed.imports);
            }
            syn::Item::ExternCrate(value) => {
                let path = value.ident.to_string();
                parsed.imports.push(row("extern_crate", file, value.ident.span().start().line, path.clone(), qualified(scope, &path), value.rename.as_ref().map(|(_, alias)| alias.to_string()), false, sha, read_unix_ms));
            }
            syn::Item::Macro(_) => parsed.macro_items += 1,
            _ => parsed.unsupported_syntax += 1,
        }
    }
}
fn parse_rust(content: &str, file: &str, sha: &str, read_unix_ms: u128) -> Result<Parsed, String> {
    let syntax = syn::parse_file(content).map_err(|e| format!("Rust parse error: {e}"))?;
    let mut parsed = Parsed { symbols: Vec::new(), imports: Vec::new(), macro_items: 0, unsupported_syntax: 0 };
    walk_items(&syntax.items, "", file, sha, read_unix_ms, &mut parsed);
    Ok(parsed)
}

pub fn query(root: &Path, request: Query) -> Result<Answer, String> {
    let root = worktree_root(root)?;
    let paths = listed_files(&root)?;
    let (kind, value) = match request {
        Query::File(value) => ("file", value), Query::Symbol(value) => ("symbol", value),
        Query::Imports(value) => ("imports", value),
    };
    if value.is_empty() || value.len() > MAX_PATH_BYTES || value.chars().any(char::is_control) {
        return Err("invalid query value".into());
    }
    if kind != "symbol" && !paths.contains(&value) { return Err("file is not present in the worktree listing".into()); }
    let mut answer = Answer { scope: root.to_string_lossy().into_owned(), query: kind, value: value.clone(),
        observed_unix_ms: SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis(),
        status: "ok".into(), coverage: Coverage::default(), rows: Vec::new(), omitted_rows: 0,
        note: NOTE, fallback: None };
    answer.coverage.enumerated_files = paths.len();
    let mut total = 0u64;
    for path in paths {
        match source_language(&path) {
            Some("rust") => {
                let (content, sha, read_unix_ms) = match file_bytes(&root, &path) {
                    Ok(result) => result,
                    Err(reason) => { answer.coverage.skipped.add(&path, reason); if path == value { answer.status = "skipped".into(); } continue; }
                };
                total = total.saturating_add(content.len() as u64);
                if total > MAX_TOTAL_SOURCE_BYTES { return Err("Rust source scan exceeded 64 MiB; no partial answer".into()); }
                let parsed = match parse_rust(&content, &path, &sha, read_unix_ms) {
                    Ok(result) => result,
                    Err(reason) => { answer.coverage.unparseable.add(&path, reason); if path == value { answer.status = "unparseable".into(); } continue; }
                };
                answer.coverage.parsed_rust_files += 1;
                answer.coverage.macro_items += parsed.macro_items;
                answer.coverage.unsupported_syntax += parsed.unsupported_syntax;
                let relevant = if kind == "symbol" {
                    parsed.symbols.into_iter().filter(|row| row.name == value || row.qualified == value).collect::<Vec<_>>()
                } else if path == value && kind == "imports" { parsed.imports }
                else if path == value { parsed.symbols.into_iter().chain(parsed.imports).collect::<Vec<_>>() }
                else { Vec::new() };
                for row in relevant {
                    if answer.rows.len() < MAX_ROWS { answer.rows.push(row); } else { answer.omitted_rows += 1; }
                }
            }
            Some(language) => {
                *answer.coverage.unsupported_languages.entry(language.into()).or_default() += 1;
                if path == value { answer.status = format!("unsupported:{language}"); }
            }
            None => { answer.coverage.other_files += 1; if path == value { answer.status = "unsupported:unknown".into(); } }
        }
    }
    if answer.rows.is_empty() && answer.status == "ok" {
        answer.fallback = Some("Search the live worktree with rg; query syntax excludes generated code and unsupported languages.");
    }
    if serde_json::to_vec(&answer).map_err(|e| e.to_string())?.len() > MAX_REPLY_BYTES {
        return Err("query reply exceeds 64 KiB; narrow the query".into());
    }
    Ok(answer)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;
    use tempfile::TempDir;

    fn worktree() -> TempDir {
        let root = tempfile::tempdir().unwrap();
        let status = Command::new("git").arg("init").arg("-q").arg(root.path()).status().unwrap();
        assert!(status.success());
        root
    }

    #[test]
    fn syntax_queries_keep_same_name_candidates_imports_and_source_basis() {
        let root = worktree();
        fs::write(root.path().join("a.rs"), "use crate::foo::{Bar as B, baz, *};\nmod inner { pub fn duplicate() {} }\nfn duplicate() {}\n").unwrap();
        fs::write(root.path().join("b.rs"), "fn duplicate() {}\n").unwrap();
        fs::write(root.path().join("other.py"), "def duplicate(): pass\n").unwrap();
        let symbols = query(root.path(), Query::Symbol("duplicate".into())).unwrap();
        assert_eq!(symbols.rows.len(), 3);
        assert_eq!(symbols.coverage.unsupported_languages["python"], 1);
        assert!(symbols.rows.iter().any(|row| row.qualified == "inner::duplicate" && row.line == 2));
        assert!(symbols.rows.iter().all(|row| row.sha256.len() == 64 && row.read_unix_ms > 0));
        let imports = query(root.path(), Query::Imports("a.rs".into())).unwrap();
        let paths = imports.rows.iter().map(|row| row.name.as_str()).collect::<Vec<_>>();
        assert_eq!(paths, ["crate::foo::Bar", "crate::foo::baz", "crate::foo::*"]);
        assert_eq!(imports.rows[0].alias.as_deref(), Some("B"));
        assert!(imports.rows[2].glob);
        let python = query(root.path(), Query::File("other.py".into())).unwrap();
        assert_eq!(python.status, "unsupported:python");
        assert!(python.rows.is_empty());
    }

    #[test]
    fn each_query_reads_current_bytes_and_reports_parse_failures() {
        let root = worktree();
        let source = root.path().join("changing.rs");
        fs::write(&source, "fn alpha() {}\n").unwrap();
        fs::write(root.path().join("broken.rs"), "fn broken( {\n").unwrap();
        let first = query(root.path(), Query::Symbol("alpha".into())).unwrap();
        assert_eq!(first.rows.len(), 1);
        assert_eq!(first.coverage.unparseable.count, 1);
        assert_eq!(first.coverage.unparseable.examples[0].file, "broken.rs");
        fs::write(&source, "fn bravo() {}\n").unwrap();
        let stale = query(root.path(), Query::Symbol("alpha".into())).unwrap();
        assert!(stale.rows.is_empty());
        assert!(stale.fallback.is_some());
        let fresh = query(root.path(), Query::Symbol("bravo".into())).unwrap();
        assert_eq!(fresh.rows.len(), 1);
        assert_ne!(first.rows[0].sha256, fresh.rows[0].sha256);
        let broken = query(root.path(), Query::File("broken.rs".into())).unwrap();
        assert_eq!(broken.status, "unparseable");
    }

    #[test]
    fn symlinks_and_oversized_sources_are_named_as_skipped() {
        let root = worktree();
        let outside = tempfile::tempdir().unwrap();
        fs::write(outside.path().join("secret.rs"), "fn should_not_appear() {}\n").unwrap();
        symlink(outside.path().join("secret.rs"), root.path().join("linked.rs")).unwrap();
        fs::write(root.path().join("huge.rs"), vec![b' '; MAX_SOURCE_BYTES as usize + 1]).unwrap();
        let answer = query(root.path(), Query::Symbol("should_not_appear".into())).unwrap();
        assert!(answer.rows.is_empty());
        assert_eq!(answer.coverage.skipped.count, 2);
        assert!(answer.coverage.skipped.examples.iter().any(|item| item.file == "linked.rs"));
        assert!(answer.coverage.skipped.examples.iter().any(|item| item.file == "huge.rs"));
    }

    #[test]
    fn descriptor_walk_refuses_symlinked_final_and_parent_components() {
        let root = worktree();
        let outside = tempfile::tempdir().unwrap();
        fs::write(outside.path().join("secret.rs"), "fn secret() {}\n").unwrap();
        symlink(outside.path().join("secret.rs"), root.path().join("linked.rs")).unwrap();
        symlink(outside.path(), root.path().join("parent")).unwrap();
        assert!(file_bytes(root.path(), "linked.rs").is_err());
        assert!(file_bytes(root.path(), "parent/secret.rs").is_err());
        assert!(file_bytes(root.path(), "../secret.rs").is_err());
        fs::create_dir(root.path().join("safe")).unwrap();
        fs::write(root.path().join("safe/real.rs"), "fn visible() {}\n").unwrap();
        assert!(file_bytes(root.path(), "safe/real.rs").unwrap().0.contains("visible"));
    }

    #[test]
    fn bounded_results_count_omissions_without_claiming_a_complete_page() {
        let root = worktree();
        fs::write(root.path().join("many.rs"), "fn repeated() {}\n".repeat(MAX_ROWS + 7)).unwrap();
        let answer = query(root.path(), Query::Symbol("repeated".into())).unwrap();
        assert_eq!(answer.rows.len(), MAX_ROWS);
        assert_eq!(answer.omitted_rows, 7);
        assert_eq!(answer.coverage.parsed_rust_files, 1);
    }
}
