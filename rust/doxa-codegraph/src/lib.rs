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
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use syn::spanned::Spanned;
use syn::visit::{self, Visit};

pub mod semantic_evidence;
pub mod semantic_producer;
pub mod semantic_runtime;

const MAX_FILES: usize = 20_000;
const MAX_PATH_BYTES: usize = 4_096;
const MAX_LIST_BYTES: usize = 4 * 1024 * 1024;
const MAX_SOURCE_BYTES: u64 = 1024 * 1024;
const MAX_TOTAL_SOURCE_BYTES: u64 = 64 * 1024 * 1024;
const MAX_PYTHON_NODES: usize = 200_000;
const MAX_PYTHON_SCOPE_DEPTH: usize = 128;
const MAX_PYTHON_PARSE_TIME: Duration = Duration::from_secs(2);
const MAX_PYTHON_SCAN_TIME: Duration = Duration::from_secs(10);
const MAX_ROWS: usize = 100;
const MAX_ISSUE_EXAMPLES: usize = 20;
const MAX_REPLY_BYTES: usize = 64 * 1024;
const MAX_CALL_SITES: usize = 10_000;
const MAX_CANDIDATE_SYMBOLS: usize = 100_000;
const MAX_EDGE_CANDIDATES: usize = 8;

pub enum Query { File(String), Symbol(String), Imports(String), Calls(String), Modules(String) }

impl Query {
    pub fn file_scope(&self) -> Option<(&'static str, &str)> {
        match self {
            Self::File(path) => Some(("file", path)),
            Self::Imports(path) => Some(("imports", path)),
            Self::Calls(path) => Some(("calls", path)),
            Self::Modules(path) => Some(("modules", path)),
            Self::Symbol(_) => None,
        }
    }
}

pub fn parse_cli(args: &[String]) -> Result<(PathBuf, Query), String> {
    let (root, rest) = if args.first().is_some_and(|arg| arg == "--root") {
        let path = args.get(1).ok_or("missing --root path")?;
        (PathBuf::from(path), &args[2..])
    } else { (PathBuf::from("."), args) };
    let request = match rest {
        [kind, value] if kind == "file" => Query::File(value.clone()),
        [kind, value] if kind == "symbol" => Query::Symbol(value.clone()),
        [kind, value] if kind == "imports" => Query::Imports(value.clone()),
        [kind, value] if kind == "calls" => Query::Calls(value.clone()),
        [kind, value] if kind == "modules" => Query::Modules(value.clone()),
        _ => return Err("usage: doxa codegraph [--root WORKTREE] file PATH | symbol NAME | imports PATH | calls PATH | modules PATH".into()),
    };
    Ok((root, request))
}

pub fn query_cli(args: &[String]) -> Result<Answer, String> {
    let (root, request) = parse_cli(args)?;
    query(&root, request)
}

#[derive(Debug, Serialize)]
pub struct Answer {
    pub scope: String,
    pub query: &'static str,
    pub value: String,
    pub observed_unix_ms: u128,
    /// Parsed bytes of the specifically requested source file, including an
    /// empty file with no rows or module declarations.
    pub requested_source_sha256: Option<String>,
    pub requested_source_read_unix_ms: Option<u128>,
    /// Digest of every listed Rust path and parsed source digest. Absent when
    /// any Rust input was skipped or failed to parse. This is a scan-input
    /// inventory, not proof of compiler bindings or an atomic filesystem view.
    pub scan_input_sha256: Option<String>,
    pub status: String,
    pub coverage: Coverage,
    pub rows: Vec<Row>,
    pub omitted_rows: usize,
    pub edges: Vec<CallEdge>,
    pub omitted_edges: usize,
    pub module_edges: Vec<ModuleEdge>,
    pub omitted_module_edges: usize,
    pub skipped_nested_modules: usize,
    pub note: &'static str,
    pub fallback: Option<&'static str>,
}

#[derive(Debug, Default, Serialize)]
pub struct Coverage {
    pub enumerated_files: usize,
    pub parsed_rust_files: usize,
    pub parsed_python_files: usize,
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

#[derive(Clone, Debug, Serialize)]
pub struct CallEdge {
    pub file: String,
    pub line: usize,
    pub column: usize,
    pub caller: String,
    pub target: String,
    pub form: &'static str,
    pub binding: &'static str,
    pub reason: &'static str,
    pub candidates: Vec<CallCandidate>,
    pub omitted_candidates: usize,
    pub sha256: String,
    pub read_unix_ms: u128,
}

#[derive(Clone, Debug, Serialize)]
pub struct CallCandidate {
    pub file: String,
    pub line: usize,
    pub qualified: String,
    pub sha256: String,
    pub read_unix_ms: u128,
}

#[derive(Clone, Debug, Serialize)]
pub struct ModuleEdge {
    pub source: String,
    pub line: usize,
    pub column: usize,
    pub module: String,
    pub target: Option<String>,
    /// A unique file observed for a cfg-gated declaration. It is never a
    /// verified target because this query does not evaluate compilation cfg.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub conditional_candidate: Option<String>,
    pub resolution: &'static str,
    pub reason: &'static str,
    pub source_sha256: String,
    pub source_read_unix_ms: u128,
    pub target_sha256: Option<String>,
    pub target_read_unix_ms: Option<u128>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub conditional_candidate_sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub conditional_candidate_read_unix_ms: Option<u128>,
}

const NOTE: &str = "Rust and Python syntax only; semantic binding is unknown. Rust call candidates match a final name segment, not bindings; even one candidate is unverified. Imports are declarations. Rust module edges resolve file layout only, not compilation reachability; cfg-gated files are candidates, never verified targets. Python calls and modules are unsupported. cfg predicates, cfg_attr, macro expansion, local definitions/imports, and other expression calls are not resolved.";

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

pub fn worktree_root(path: &Path) -> Result<PathBuf, String> {
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
    let content = String::from_utf8(bytes).map_err(|_| "non-UTF-8 source")?;
    Ok((content, sha, read_unix_ms))
}

/// Recheck one recorded Rust source without following symlinks in any path
/// component. This uses the same 1 MiB, descriptor-anchored read as queries.
pub fn source_sha256(root: &Path, relative: &str) -> Result<String, String> {
    file_bytes(root, relative).map(|(_, sha, _)| sha)
}

fn scan_digest<'a>(entries: impl IntoIterator<Item = (&'a str, &'a str)>) -> String {
    let mut hasher = Sha256::new();
    hasher.update(b"doxa-rust-scan-input-v1\0");
    for (path, digest) in entries {
        hasher.update((path.len() as u64).to_be_bytes());
        hasher.update(path.as_bytes());
        hasher.update(digest.as_bytes());
    }
    format!("{:x}", hasher.finalize())
}

/// Re-enumerate and hash all nonignored Git-listed Rust inputs. Any
/// uncheckable file or scan-budget breach fails closed. Callers compare this
/// with an answer's scan-input digest to detect edits, additions, and removals.
pub fn current_scan_input_sha256(root: &Path) -> Result<(String, usize), String> {
    let root = worktree_root(root)?;
    let paths = listed_files(&root)?;
    let mut entries = Vec::new();
    let mut total = 0u64;
    for path in paths.iter().filter(|path| source_language(path) == Some("rust")) {
        let (content, sha, _) = file_bytes(&root, path)?;
        total = total.saturating_add(content.len() as u64);
        if total > MAX_TOTAL_SOURCE_BYTES {
            return Err("Rust source scan exceeded 64 MiB; no partial verification".into());
        }
        entries.push((path.as_str(), sha));
    }
    let after = listed_files(&root)?;
    if paths != after {
        return Err("Git worktree listing changed during scan verification".into());
    }
    let count = entries.len();
    Ok((scan_digest(entries.iter().map(|(path, sha)| (*path, sha.as_str()))), count))
}

#[derive(Clone)]
struct ModuleDecl {
    name: String,
    line: usize,
    column: usize,
    inline: bool,
    attributes: ModuleAttributes,
}

#[derive(Clone)]
enum ModuleAttributes {
    Plain,
    Conditional,
    LiteralPath(PathBuf),
    ConditionalLiteralPath(PathBuf),
    Unresolved,
}

fn literal_module_path(value: &str) -> Option<PathBuf> {
    let path = Path::new(value);
    if value.is_empty() || value.len() > MAX_PATH_BYTES || value.chars().any(char::is_control)
        || path.extension().and_then(|extension| extension.to_str()) != Some("rs") {
        return None;
    }
    let components = path.components().collect::<Vec<_>>();
    if components.is_empty() || !components.iter().all(|part| matches!(part, std::path::Component::Normal(_))) {
        return None;
    }
    Some(components.iter().map(|part| part.as_os_str()).collect())
}

fn module_attributes(attrs: &[syn::Attribute]) -> ModuleAttributes {
    let mut conditional = false;
    let mut path = None;
    for attr in attrs {
        if attr.path().is_ident("cfg") && matches!(&attr.meta, syn::Meta::List(_)) {
            conditional = true;
        } else if attr.path().is_ident("path") {
            let syn::Meta::NameValue(meta) = &attr.meta else { return ModuleAttributes::Unresolved; };
            let syn::Expr::Lit(expr) = &meta.value else { return ModuleAttributes::Unresolved; };
            let syn::Lit::Str(value) = &expr.lit else { return ModuleAttributes::Unresolved; };
            let Some(value) = literal_module_path(&value.value()) else { return ModuleAttributes::Unresolved; };
            if path.replace(value).is_some() { return ModuleAttributes::Unresolved; }
        } else {
            // cfg_attr can insert or replace a path; arbitrary attributes may
            // be macros. Do not infer a target from either.
            return ModuleAttributes::Unresolved;
        }
    }
    match (conditional, path) {
        (false, None) => ModuleAttributes::Plain,
        (true, None) => ModuleAttributes::Conditional,
        (false, Some(path)) => ModuleAttributes::LiteralPath(path),
        (true, Some(path)) => ModuleAttributes::ConditionalLiteralPath(path),
    }
}

struct Parsed {
    symbols: Vec<Row>, imports: Vec<Row>, calls: Vec<CallEdge>,
    modules: Vec<ModuleDecl>, nested_modules: usize,
    macro_items: usize, unsupported_syntax: usize,
}

fn nested_module_count(items: &[syn::Item]) -> usize {
    items.iter().map(|item| match item {
        syn::Item::Mod(module) => 1 + module.content.as_ref()
            .map(|(_, children)| nested_module_count(children)).unwrap_or(0),
        _ => 0,
    }).sum()
}

struct CallCollector<'a> {
    file: &'a str,
    caller: &'a str,
    sha: &'a str,
    read_unix_ms: u128,
    calls: &'a mut Vec<CallEdge>,
}
impl CallCollector<'_> {
    fn push(&mut self, line: usize, column: usize, target: String, form: &'static str,
        reason: &'static str) {
        self.calls.push(CallEdge {
            file: self.file.into(), line, column, caller: self.caller.into(), target,
            form, binding: "unresolved", reason, candidates: Vec::new(),
            omitted_candidates: 0, sha256: self.sha.into(), read_unix_ms: self.read_unix_ms,
        });
    }
}
impl<'ast> Visit<'ast> for CallCollector<'_> {
    fn visit_expr_call(&mut self, node: &'ast syn::ExprCall) {
        if let syn::Expr::Path(path) = node.func.as_ref() {
            let target = path.path.segments.iter().map(|part| part.ident.to_string())
                .collect::<Vec<_>>().join("::");
            let target = if path.path.leading_colon.is_some() { format!("::{target}") } else { target };
            let at = node.func.span().start();
            self.push(at.line, at.column, target, "function_path",
                if path.qself.is_some() { "qualified_self_type_unknown" } else { "pending_name_match" });
        }
        visit::visit_expr_call(self, node);
    }

    fn visit_expr_method_call(&mut self, node: &'ast syn::ExprMethodCall) {
        let at = node.method.span().start();
        self.push(at.line, at.column, node.method.to_string(), "method_receiver",
            "receiver_type_unknown");
        visit::visit_expr_method_call(self, node);
    }

    // A nested item has its own lexical caller. It is outside this slice's
    // definition inventory, so do not attribute its calls to the outer item.
    fn visit_item(&mut self, _: &'ast syn::Item) {}
}

fn collect_calls(block: &syn::Block, caller: &str, file: &str, sha: &str,
    read_unix_ms: u128, parsed: &mut Parsed) {
    CallCollector { file, caller, sha, read_unix_ms, calls: &mut parsed.calls }.visit_block(block);
}
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
fn walk_items(items: &[syn::Item], scope: &str, file: &str, sha: &str, read_unix_ms: u128,
    collect: bool, parsed: &mut Parsed) {
    for item in items {
        match item {
            syn::Item::Fn(value) => {
                add_symbol(parsed, "function", &value.sig.ident, scope, file, sha, read_unix_ms);
                if collect { collect_calls(&value.block, &qualified(scope, &value.sig.ident.to_string()), file, sha, read_unix_ms, parsed); }
            }
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
                    if let syn::TraitItem::Fn(method) = child {
                        add_symbol(parsed, "trait_method", &method.sig.ident, &owner, file, sha, read_unix_ms);
                        if collect {
                            if let Some(body) = &method.default {
                                collect_calls(body, &qualified(&owner, &method.sig.ident.to_string()), file, sha, read_unix_ms, parsed);
                            }
                        }
                    }
                }
            }
            syn::Item::Mod(value) => {
                add_symbol(parsed, if value.content.is_some() { "inline_module" } else { "module_decl" }, &value.ident, scope, file, sha, read_unix_ms);
                if let Some((_, nested)) = &value.content {
                    let next = qualified(scope, &value.ident.to_string());
                    walk_items(nested, &next, file, sha, read_unix_ms, collect, parsed);
                }
            }
            syn::Item::Impl(value) => {
                if let Some(target) = rust_type(&value.self_ty) {
                    let owner = if let Some((_, path, _)) = &value.trait_ {
                        format!("impl[{} for {}]", path.segments.iter().map(|part| part.ident.to_string()).collect::<Vec<_>>().join("::"), target)
                    } else { format!("impl[{target}]") };
                    let owner = qualified(scope, &owner);
                    for child in &value.items {
                        if let syn::ImplItem::Fn(method) = child {
                            add_symbol(parsed, "method", &method.sig.ident, &owner, file, sha, read_unix_ms);
                            if collect { collect_calls(&method.block, &qualified(&owner, &method.sig.ident.to_string()), file, sha, read_unix_ms, parsed); }
                        }
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
fn parse_rust(content: &str, file: &str, sha: &str, read_unix_ms: u128,
    collect: bool) -> Result<Parsed, String> {
    let syntax = syn::parse_file(content).map_err(|e| format!("Rust parse error: {e}"))?;
    let mut parsed = Parsed { symbols: Vec::new(), imports: Vec::new(), calls: Vec::new(),
        modules: Vec::new(), nested_modules: 0, macro_items: 0, unsupported_syntax: 0 };
    for item in &syntax.items {
        if let syn::Item::Mod(module) = item {
            let at = module.ident.span().start();
            parsed.modules.push(ModuleDecl {
                name: module.ident.to_string(), line: at.line, column: at.column,
                inline: module.content.is_some(), attributes: module_attributes(&module.attrs),
            });
            if let Some((_, children)) = &module.content {
                parsed.nested_modules += nested_module_count(children);
            }
        }
    }
    walk_items(&syntax.items, "", file, sha, read_unix_ms, collect, &mut parsed);
    parsed.calls.sort_by_key(|edge| (edge.line, edge.column));
    Ok(parsed)
}

fn python_text(node: tree_sitter::Node<'_>, source: &str) -> Result<String, String> {
    node.utf8_text(source.as_bytes())
        .map(|text| text.chars().filter(|ch| !ch.is_whitespace()).collect())
        .map_err(|_| "Python syntax node is outside source bytes".into())
}

fn python_import_name(node: tree_sitter::Node<'_>, source: &str)
    -> Result<(String, Option<String>), String> {
    if node.kind() == "aliased_import" {
        let name = node.child_by_field_name("name").ok_or("Python import lacks a name")?;
        let alias = node.child_by_field_name("alias").ok_or("Python import lacks an alias")?;
        Ok((python_text(name, source)?, Some(python_text(alias, source)?)))
    } else {
        Ok((python_text(node, source)?, None))
    }
}

fn python_imports(node: tree_sitter::Node<'_>, source: &str, file: &str, sha: &str,
    read_unix_ms: u128, rows: &mut Vec<Row>) -> Result<(), String> {
    let module = if node.kind() == "import_from_statement" {
        Some(node.child_by_field_name("module_name")
            .ok_or("Python from-import lacks a module")?)
    } else { None };
    let prefix = if node.kind() == "future_import_statement" {
        "__future__".to_owned()
    } else if let Some(module) = module {
        python_text(module, source)?
    } else { String::new() };
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if module.is_some_and(|module| module.id() == child.id()) { continue; }
        if !matches!(child.kind(), "dotted_name" | "aliased_import" | "wildcard_import") {
            return Err(format!("unsupported Python import syntax: {}", child.kind()));
        }
        let glob = child.kind() == "wildcard_import";
        let (name, alias) = python_import_name(child, source)?;
        let full = if prefix.is_empty() { name }
            else if prefix.ends_with('.') { format!("{prefix}{name}") }
            else { format!("{prefix}.{name}") };
        rows.push(row(if glob { "python_import_glob" } else { "python_import" }, file,
            child.start_position().row + 1, full.clone(), full, alias, glob, sha, read_unix_ms));
    }
    Ok(())
}

fn parse_python(content: &str, file: &str, sha: &str, read_unix_ms: u128, budget: Duration)
    -> Result<(Vec<Row>, Vec<Row>), String> {
    if budget.is_zero() { return Err("Python parse deadline exceeded".into()); }
    let mut parser = tree_sitter::Parser::new();
    parser.set_language(&tree_sitter_python::LANGUAGE.into())
        .map_err(|e| format!("Python parser setup: {e}"))?;
    let started = Instant::now();
    let mut read = |offset, _: tree_sitter::Point| content.as_bytes().get(offset..).unwrap_or_default();
    let mut out_of_time = |_: &tree_sitter::ParseState| started.elapsed() >= budget.min(MAX_PYTHON_PARSE_TIME);
    let tree = parser.parse_with_options(&mut read, None,
        Some(tree_sitter::ParseOptions::new().progress_callback(&mut out_of_time)))
        .ok_or("Python parse deadline exceeded")?;
    if tree.root_node().has_error() {
        return Err("Python syntax error".into());
    }
    let mut symbols = Vec::new();
    let mut imports = Vec::new();
    let mut stack = vec![(tree.root_node(), String::new(), false, 0usize)];
    let mut visited = 0usize;
    while let Some((node, scope, in_class, depth)) = stack.pop() {
        visited += 1;
        if visited % 256 == 0 && started.elapsed() >= budget {
            return Err("Python parse deadline exceeded".into());
        }
        if visited > MAX_PYTHON_NODES {
            return Err("Python syntax tree exceeds 200,000 nodes".into());
        }
        let mut child_scope = scope.clone();
        let mut child_in_class = in_class;
        let mut child_depth = depth;
        match node.kind() {
            "class_definition" | "function_definition" => {
                if depth >= MAX_PYTHON_SCOPE_DEPTH {
                    return Err("Python definition nesting exceeds 128 levels".into());
                }
                let name_node = node.child_by_field_name("name")
                    .ok_or("Python definition lacks a name")?;
                let name = python_text(name_node, content)?;
                let qualified = if scope.is_empty() { name.clone() }
                    else { format!("{scope}.{name}") };
                let is_class = node.kind() == "class_definition";
                symbols.push(row(if is_class { "class" } else if in_class { "method" } else { "function" },
                    file, name_node.start_position().row + 1, name, qualified.clone(),
                    None, false, sha, read_unix_ms));
                child_scope = qualified;
                child_in_class = is_class;
                child_depth += 1;
            }
            "import_statement" | "import_from_statement" | "future_import_statement" => {
                python_imports(node, content, file, sha, read_unix_ms, &mut imports)?;
            }
            _ => {}
        }
        let mut cursor = node.walk();
        let children = node.named_children(&mut cursor).collect::<Vec<_>>();
        for child in children.into_iter().rev() {
            stack.push((child, child_scope.clone(), child_in_class, child_depth));
        }
    }
    Ok((symbols, imports))
}

enum SourceFact {
    Parsed { sha256: String, read_unix_ms: u128 },
    Skipped,
    Unparseable,
}

fn module_base(source: &str) -> PathBuf {
    let path = Path::new(source);
    let parent = path.parent().unwrap_or_else(|| Path::new(""));
    let name = path.file_name().and_then(|name| name.to_str()).unwrap_or_default();
    let cargo_root = parent.ends_with("src/bin")
        || ["tests", "examples", "benches"].iter().any(|segment| parent.ends_with(segment));
    if matches!(name, "lib.rs" | "main.rs" | "mod.rs" | "build.rs") || cargo_root {
        parent.to_path_buf()
    } else {
        parent.join(path.file_stem().unwrap_or_default())
    }
}

fn possible_unlisted_candidate(root: &Path, relative: &str) -> bool {
    // Only test for presence. Never read ignored files or follow a candidate's
    // symlinked parent: an unsafe/indeterminate path blocks a positive edge.
    let Ok(mut directory) = fs::OpenOptions::new().read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(root) else { return true; };
    let parts = Path::new(relative).components().map(|part| match part {
        std::path::Component::Normal(name) => CString::new(name.as_bytes()).ok(),
        _ => None,
    }).collect::<Option<Vec<_>>>();
    let Some(parts) = parts else { return true; };
    let Some((last, parents)) = parts.split_last() else { return true; };
    for part in parents {
        let fd = unsafe { libc::openat(directory.as_raw_fd(), part.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) };
        if fd < 0 {
            return std::io::Error::last_os_error().raw_os_error() != Some(libc::ENOENT);
        }
        directory = unsafe { File::from_raw_fd(fd) };
    }
    let mut metadata = std::mem::MaybeUninit::<libc::stat>::uninit();
    let status = unsafe { libc::fstatat(directory.as_raw_fd(), last.as_ptr(),
        metadata.as_mut_ptr(), libc::AT_SYMLINK_NOFOLLOW) };
    status == 0 || std::io::Error::last_os_error().raw_os_error() != Some(libc::ENOENT)
}

fn module_edges(root: &Path, source: &str, declarations: &[ModuleDecl],
    source_sha: &str, source_read_unix_ms: u128, listed: &BTreeSet<String>,
    facts: &BTreeMap<String, SourceFact>) -> (Vec<ModuleEdge>, usize) {
    let base = module_base(source);
    let source_parent = Path::new(source).parent().unwrap_or_else(|| Path::new(""));
    let mut counts = BTreeMap::<&str, usize>::new();
    for declaration in declarations { *counts.entry(&declaration.name).or_default() += 1; }
    let mut edges = Vec::new();
    for declaration in declarations {
        let mut edge = ModuleEdge {
            source: source.into(), line: declaration.line, column: declaration.column,
            module: declaration.name.clone(), target: None, conditional_candidate: None,
            resolution: "unknown", reason: "no_listed_candidate",
            source_sha256: source_sha.into(), source_read_unix_ms,
            target_sha256: None, target_read_unix_ms: None,
            conditional_candidate_sha256: None, conditional_candidate_read_unix_ms: None,
        };
        if counts[declaration.name.as_str()] > 1 {
            edge.reason = "duplicate_declaration";
        } else if declaration.inline {
            edge.reason = "inline_module_skipped";
        } else if matches!(&declaration.attributes, ModuleAttributes::Unresolved) {
            edge.reason = "module_attribute_unresolved";
        } else {
            let (candidates, conditional, explicit_path) = match &declaration.attributes {
                ModuleAttributes::Plain | ModuleAttributes::Conditional => {
                    let direct = base.join(format!("{}.rs", declaration.name));
                    let directory = base.join(&declaration.name).join("mod.rs");
                    (vec![direct, directory], matches!(&declaration.attributes, ModuleAttributes::Conditional), false)
                }
                ModuleAttributes::LiteralPath(path) | ModuleAttributes::ConditionalLiteralPath(path) => {
                    (vec![source_parent.join(path)],
                        matches!(&declaration.attributes, ModuleAttributes::ConditionalLiteralPath(_)), true)
                }
                ModuleAttributes::Unresolved => unreachable!(),
            };
            let candidates = candidates.into_iter().map(|path| path.to_string_lossy().into_owned())
                .collect::<Vec<_>>();
            let present = candidates.iter().filter(|path| listed.contains(path.as_str()))
                .collect::<Vec<_>>();
            // An ignored or generated-on-disk sibling may change Rust's layout
            // choice, so a single listed candidate is not enough in that case.
            let unlisted_exists = candidates.iter().filter(|path| !listed.contains(path.as_str()))
                .any(|path| possible_unlisted_candidate(root, path));
            if present.len() > 1 {
                edge.reason = "ambiguous_layout";
            } else if unlisted_exists {
                edge.reason = "unlisted_candidate_exists";
            } else if let Some(target) = present.first() {
                match facts.get(target.as_str()) {
                    Some(SourceFact::Parsed { sha256, read_unix_ms }) => {
                        if conditional {
                            edge.conditional_candidate = Some((*target).clone());
                            edge.reason = if explicit_path { "cfg_literal_path_candidate" }
                                else { "cfg_layout_candidate" };
                            edge.conditional_candidate_sha256 = Some(sha256.clone());
                            edge.conditional_candidate_read_unix_ms = Some(*read_unix_ms);
                        } else {
                            edge.target = Some((*target).clone());
                            edge.resolution = "structural_only";
                            edge.reason = if explicit_path { "literal_path_file" }
                                else { "unique_plain_file_layout" };
                            edge.target_sha256 = Some(sha256.clone());
                            edge.target_read_unix_ms = Some(*read_unix_ms);
                        }
                    }
                    Some(SourceFact::Skipped) => edge.reason = "candidate_skipped",
                    Some(SourceFact::Unparseable) => edge.reason = "candidate_unparseable",
                    None => edge.reason = "candidate_not_scanned",
                }
            }
        }
        edges.push(edge);
    }
    let omitted = edges.len().saturating_sub(MAX_ROWS);
    edges.truncate(MAX_ROWS);
    (edges, omitted)
}

pub fn query(root: &Path, request: Query) -> Result<Answer, String> {
    let root = worktree_root(root)?;
    let paths = listed_files(&root)?;
    let (kind, value) = match request {
        Query::File(value) => ("file", value), Query::Symbol(value) => ("symbol", value),
        Query::Imports(value) => ("imports", value), Query::Calls(value) => ("calls", value),
        Query::Modules(value) => ("modules", value),
    };
    if value.is_empty() || value.len() > MAX_PATH_BYTES || value.chars().any(char::is_control) {
        return Err("invalid query value".into());
    }
    if kind != "symbol" && !paths.contains(&value) { return Err("file is not present in the worktree listing".into()); }
    let mut answer = Answer { scope: root.to_string_lossy().into_owned(), query: kind, value: value.clone(),
        observed_unix_ms: SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_millis(),
        requested_source_sha256: None, requested_source_read_unix_ms: None,
        scan_input_sha256: None,
        status: "ok".into(), coverage: Coverage::default(), rows: Vec::new(), omitted_rows: 0,
        edges: Vec::new(), omitted_edges: 0,
        module_edges: Vec::new(), omitted_module_edges: 0, skipped_nested_modules: 0,
        note: NOTE, fallback: None };
    answer.coverage.enumerated_files = paths.len();
    let mut total = 0u64;
    let mut python_total = 0u64;
    let mut python_scan_started = None;
    let mut rust_scan_complete = true;
    let mut candidates: BTreeMap<String, Vec<Row>> = BTreeMap::new();
    let mut candidate_count = 0usize;
    let mut source_facts = BTreeMap::<String, SourceFact>::new();
    let mut requested_modules = None;
    for path in &paths {
        match source_language(&path) {
            Some("rust") => {
                let (content, sha, read_unix_ms) = match file_bytes(&root, &path) {
                    Ok(result) => result,
                    Err(reason) => { answer.coverage.skipped.add(&path, reason); source_facts.insert(path.clone(), SourceFact::Skipped); rust_scan_complete = false; if path == &value { answer.status = "skipped".into(); } continue; }
                };
                total = total.saturating_add(content.len() as u64);
                if total > MAX_TOTAL_SOURCE_BYTES { return Err("Rust source scan exceeded 64 MiB; no partial answer".into()); }
                let parsed = match parse_rust(&content, &path, &sha, read_unix_ms,
                    kind == "calls" && path == &value) {
                    Ok(result) => result,
                    Err(reason) => { answer.coverage.unparseable.add(&path, reason); source_facts.insert(path.clone(), SourceFact::Unparseable); rust_scan_complete = false; if path == &value { answer.status = "unparseable".into(); } continue; }
                };
                source_facts.insert(path.clone(), SourceFact::Parsed { sha256: sha.clone(), read_unix_ms });
                if path == &value && kind != "symbol" {
                    answer.requested_source_sha256 = Some(sha.clone());
                    answer.requested_source_read_unix_ms = Some(read_unix_ms);
                }
                answer.coverage.parsed_rust_files += 1;
                answer.coverage.macro_items += parsed.macro_items;
                answer.coverage.unsupported_syntax += parsed.unsupported_syntax;
                if kind == "modules" {
                    if path == &value {
                        requested_modules = Some((parsed.modules, parsed.nested_modules, sha, read_unix_ms));
                    }
                    continue;
                }
                if kind == "calls" {
                    for symbol in parsed.symbols.iter().filter(|row| matches!(row.kind,
                        "function" | "method" | "trait_method")) {
                        candidate_count += 1;
                        if candidate_count > MAX_CANDIDATE_SYMBOLS {
                            return Err("call candidate inventory exceeds 100,000; no partial answer".into());
                        }
                        candidates.entry(symbol.name.clone()).or_default().push(symbol.clone());
                    }
                    if path == &value {
                        if parsed.calls.len() > MAX_CALL_SITES {
                            return Err("file contains more than 10,000 supported call sites; no partial answer".into());
                        }
                        answer.omitted_edges = parsed.calls.len().saturating_sub(MAX_ROWS);
                        answer.edges = parsed.calls.into_iter().take(MAX_ROWS).collect();
                    }
                    continue;
                }
                let relevant = if kind == "symbol" {
                    parsed.symbols.into_iter().filter(|row| row.name == value || row.qualified == value).collect::<Vec<_>>()
                } else if path == &value && kind == "imports" { parsed.imports }
                else if path == &value { parsed.symbols.into_iter().chain(parsed.imports).collect::<Vec<_>>() }
                else { Vec::new() };
                for row in relevant {
                    if answer.rows.len() < MAX_ROWS { answer.rows.push(row); } else { answer.omitted_rows += 1; }
                }
            }
            Some("python") => {
                let started = *python_scan_started.get_or_insert_with(Instant::now);
                let remaining = MAX_PYTHON_SCAN_TIME.saturating_sub(started.elapsed());
                if remaining.is_zero() { return Err("Python source scan exceeded ten-second limit; no partial answer".into()); }
                let (content, sha, read_unix_ms) = match file_bytes(&root, path) {
                    Ok(result) => result,
                    Err(reason) => {
                        answer.coverage.skipped.add(path, reason);
                        if path == &value { answer.status = "skipped".into(); }
                        continue;
                    }
                };
                python_total = python_total.saturating_add(content.len() as u64);
                if python_total > MAX_TOTAL_SOURCE_BYTES {
                    return Err("Python source scan exceeded 64 MiB; no partial answer".into());
                }
                let (symbols, imports) = match parse_python(&content, path, &sha, read_unix_ms, remaining) {
                    Ok(result) => result,
                    Err(reason) => {
                        if started.elapsed() >= MAX_PYTHON_SCAN_TIME {
                            return Err("Python source scan exceeded ten-second limit; no partial answer".into());
                        }
                        answer.coverage.unparseable.add(path, reason);
                        if path == &value { answer.status = "unparseable".into(); }
                        continue;
                    }
                };
                answer.coverage.parsed_python_files += 1;
                if path == &value && kind != "symbol" {
                    answer.requested_source_sha256 = Some(sha);
                    answer.requested_source_read_unix_ms = Some(read_unix_ms);
                }
                if path == &value && matches!(kind, "calls" | "modules") {
                    answer.status = format!("unsupported:python_{kind}");
                    continue;
                }
                let relevant = if kind == "symbol" {
                    symbols.into_iter().filter(|row| row.name == value || row.qualified == value)
                        .collect::<Vec<_>>()
                } else if path == &value && kind == "imports" { imports }
                else if path == &value && kind == "file" {
                    symbols.into_iter().chain(imports).collect::<Vec<_>>()
                } else { Vec::new() };
                for row in relevant {
                    if answer.rows.len() < MAX_ROWS { answer.rows.push(row); } else { answer.omitted_rows += 1; }
                }
            }
            Some(language) => {
                *answer.coverage.unsupported_languages.entry(language.into()).or_default() += 1;
                if path == &value { answer.status = format!("unsupported:{language}"); }
            }
            None => { answer.coverage.other_files += 1; if path == &value { answer.status = "unsupported:unknown".into(); } }
        }
    }
    if rust_scan_complete {
        answer.scan_input_sha256 = Some(scan_digest(source_facts.iter().map(|(path, fact)| {
            let SourceFact::Parsed { sha256, .. } = fact else {
                unreachable!("complete scan has only parsed Rust sources")
            };
            (path.as_str(), sha256.as_str())
        })));
    }
    if let Some((declarations, nested, sha, read_unix_ms)) = requested_modules {
        answer.skipped_nested_modules = nested;
        (answer.module_edges, answer.omitted_module_edges) = module_edges(
            &root, &value, &declarations, &sha, read_unix_ms, &paths, &source_facts);
    }
    if kind == "calls" {
        for edge in &mut answer.edges {
            if edge.reason != "pending_name_match" { continue; }
            let name = edge.target.rsplit("::").next().unwrap_or_default();
            let matches = candidates.get(name).map(Vec::as_slice).unwrap_or_default();
            edge.binding = match matches.len() {
                0 => "unresolved", 1 => "candidate_only", _ => "ambiguous",
            };
            edge.reason = match matches.len() {
                0 => "no_observed_name_match", 1 => "one_observed_name_match", _ => "multiple_observed_name_matches",
            };
            edge.omitted_candidates = matches.len().saturating_sub(MAX_EDGE_CANDIDATES);
            edge.candidates = matches.iter().take(MAX_EDGE_CANDIDATES).map(|row| CallCandidate {
                file: row.file.clone(), line: row.line, qualified: row.qualified.clone(),
                sha256: row.sha256.clone(), read_unix_ms: row.read_unix_ms,
            }).collect();
        }
    }
    if answer.rows.is_empty() && answer.edges.is_empty() && answer.module_edges.is_empty() && answer.status == "ok" {
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
    fn complete_scan_digest_detects_edits_additions_and_removals() {
        let root = worktree();
        fs::write(root.path().join("lib.rs"), "fn first() {}\n").unwrap();
        let answer = query(root.path(), Query::File("lib.rs".into())).unwrap();
        let expected = answer.scan_input_sha256.unwrap();
        assert_eq!(current_scan_input_sha256(root.path()).unwrap(), (expected.clone(), 1));

        fs::write(root.path().join("lib.rs"), "fn second() {}\n").unwrap();
        assert_ne!(current_scan_input_sha256(root.path()).unwrap().0, expected);
        fs::write(root.path().join("lib.rs"), "fn first() {}\n").unwrap();
        fs::write(root.path().join("added.rs"), "fn added() {}\n").unwrap();
        assert_ne!(current_scan_input_sha256(root.path()).unwrap().0, expected);
        fs::remove_file(root.path().join("added.rs")).unwrap();
        assert_eq!(current_scan_input_sha256(root.path()).unwrap().0, expected);
        fs::remove_file(root.path().join("lib.rs")).unwrap();
        assert_ne!(current_scan_input_sha256(root.path()).unwrap().0, expected);
    }

    #[test]
    fn incomplete_or_uncheckable_scan_never_gets_a_digest() {
        let root = worktree();
        fs::write(root.path().join("lib.rs"), "fn first() {}\n").unwrap();
        fs::write(root.path().join("broken.rs"), "fn broken( {\n").unwrap();
        assert!(query(root.path(), Query::File("lib.rs".into())).unwrap()
            .scan_input_sha256.is_none());
        fs::remove_file(root.path().join("broken.rs")).unwrap();
        symlink(root.path().join("lib.rs"), root.path().join("link.rs")).unwrap();
        assert!(query(root.path(), Query::File("lib.rs".into())).unwrap()
            .scan_input_sha256.is_none());
        assert!(current_scan_input_sha256(root.path()).is_err());
    }

    #[test]
    fn syntax_queries_keep_same_name_candidates_imports_and_source_basis() {
        let root = worktree();
        fs::write(root.path().join("a.rs"), "use crate::foo::{Bar as B, baz, *};\nmod inner { pub fn duplicate() {} }\nfn duplicate() {}\n").unwrap();
        fs::write(root.path().join("b.rs"), "fn duplicate() {}\n").unwrap();
        fs::write(root.path().join("other.py"), "def duplicate(): pass\n").unwrap();
        let symbols = query(root.path(), Query::Symbol("duplicate".into())).unwrap();
        assert_eq!(symbols.rows.len(), 4);
        assert_eq!(symbols.coverage.parsed_python_files, 1);
        assert!(symbols.rows.iter().any(|row| row.qualified == "inner::duplicate" && row.line == 2));
        assert!(symbols.rows.iter().all(|row| row.sha256.len() == 64 && row.read_unix_ms > 0));
        let imports = query(root.path(), Query::Imports("a.rs".into())).unwrap();
        let paths = imports.rows.iter().map(|row| row.name.as_str()).collect::<Vec<_>>();
        assert_eq!(paths, ["crate::foo::Bar", "crate::foo::baz", "crate::foo::*"]);
        assert_eq!(imports.rows[0].alias.as_deref(), Some("B"));
        assert!(imports.rows[2].glob);
        let python = query(root.path(), Query::File("other.py".into())).unwrap();
        assert_eq!(python.status, "ok");
        assert_eq!(python.rows[0].qualified, "duplicate");
        assert!(python.requested_source_sha256.is_some());
    }

    #[test]
    fn python_definitions_and_imports_are_syntactic_and_source_hashed() {
        let root = worktree();
        fs::write(root.path().join("service.py"), concat!(
            "import os.path as osp, json\n",
            "from .helpers import one as first, two\n",
            "from pkg.api import *\n",
            "class Worker:\n",
            "    def run(self):\n",
            "        from .tasks import task\n",
            "        def nested(): pass\n",
            "def outer(): pass\n",
        )).unwrap();
        let file = query(root.path(), Query::File("service.py".into())).unwrap();
        assert_eq!(file.status, "ok");
        assert_eq!(file.coverage.parsed_python_files, 1);
        assert_eq!(file.rows.iter().filter(|row| row.kind == "class").count(), 1);
        let method = file.rows.iter().find(|row| row.qualified == "Worker.run").unwrap();
        assert_eq!(method.kind, "method");
        assert_eq!(method.line, 5);
        assert!(file.rows.iter().any(|row| row.qualified == "Worker.run.nested"));
        assert!(file.rows.iter().all(|row| row.sha256 == file.requested_source_sha256.clone().unwrap()
            && row.read_unix_ms > 0));
        let names = query(root.path(), Query::Symbol("Worker.run".into())).unwrap();
        assert_eq!(names.rows.len(), 1);
        assert_eq!(names.rows[0].name, "run");
        let imports = query(root.path(), Query::Imports("service.py".into())).unwrap();
        let names = imports.rows.iter().map(|row| row.name.as_str()).collect::<Vec<_>>();
        assert_eq!(names, ["os.path", "json", ".helpers.one", ".helpers.two", "pkg.api.*", ".tasks.task"]);
        assert_eq!(imports.rows[0].alias.as_deref(), Some("osp"));
        assert_eq!(imports.rows[2].alias.as_deref(), Some("first"));
        assert!(imports.rows[4].glob);
        assert_eq!(imports.rows[5].line, 6);
        for kind in [Query::Calls("service.py".into()), Query::Modules("service.py".into())] {
            let unsupported = query(root.path(), kind).unwrap();
            assert!(unsupported.status.starts_with("unsupported:python_"));
            assert!(unsupported.rows.is_empty() && unsupported.edges.is_empty()
                && unsupported.module_edges.is_empty());
        }
        fs::write(root.path().join("service.py"), "def replacement(): pass\n").unwrap();
        let changed = query(root.path(), Query::File("service.py".into())).unwrap();
        assert_ne!(file.requested_source_sha256, changed.requested_source_sha256);
        assert_eq!(changed.rows.len(), 1);
        assert_eq!(changed.rows[0].name, "replacement");
    }

    #[test]
    fn python_errors_and_symlinks_do_not_change_rust_scan_digest() {
        let root = worktree();
        fs::write(root.path().join("lib.rs"), "fn stable() {}\n").unwrap();
        fs::write(root.path().join("broken.py"), "def broken(:\n").unwrap();
        let baseline = query(root.path(), Query::File("lib.rs".into())).unwrap();
        assert_eq!(baseline.coverage.unparseable.count, 1);
        let digest = baseline.scan_input_sha256.unwrap();
        assert_eq!(current_scan_input_sha256(root.path()).unwrap().0, digest);
        let broken = query(root.path(), Query::File("broken.py".into())).unwrap();
        assert_eq!(broken.status, "unparseable");
        assert!(broken.rows.is_empty());
        fs::write(root.path().join("broken.py"), "def fixed(): pass\n").unwrap();
        symlink(root.path().join("broken.py"), root.path().join("linked.py")).unwrap();
        let linked = query(root.path(), Query::File("linked.py".into())).unwrap();
        assert_eq!(linked.status, "skipped");
        assert!(linked.rows.is_empty());
        assert_eq!(linked.scan_input_sha256.as_deref(), Some(digest.as_str()));
        fs::write(root.path().join("oversized.py"), vec![b' '; MAX_SOURCE_BYTES as usize + 1]).unwrap();
        let oversized = query(root.path(), Query::File("oversized.py".into())).unwrap();
        assert_eq!(oversized.status, "skipped");
        assert!(oversized.coverage.skipped.examples.iter().any(|item| item.file == "oversized.py"));
        assert_eq!(oversized.scan_input_sha256.as_deref(), Some(digest.as_str()));
        fs::write(root.path().join("added.py"), "class Added: pass\n").unwrap();
        assert_eq!(query(root.path(), Query::File("lib.rs".into())).unwrap()
            .scan_input_sha256.as_deref(), Some(digest.as_str()));
    }

    #[test]
    fn python_rows_are_bounded_and_empty_file_retains_source_hash() {
        let root = worktree();
        fs::write(root.path().join("many.py"), "def repeated(): pass\n".repeat(MAX_ROWS + 7)).unwrap();
        let many = query(root.path(), Query::Symbol("repeated".into())).unwrap();
        assert_eq!(many.rows.len(), MAX_ROWS);
        assert_eq!(many.omitted_rows, 7);
        fs::write(root.path().join("empty.py"), "").unwrap();
        let empty = query(root.path(), Query::File("empty.py".into())).unwrap();
        assert_eq!(empty.status, "ok");
        assert!(empty.rows.is_empty());
        assert_eq!(empty.requested_source_sha256.as_deref(),
            Some(format!("{:x}", Sha256::digest(b"")).as_str()));
    }

    #[test]
    fn python_multiline_imports_and_decorated_definitions_exclude_text_lookalikes() {
        let root = worktree();
        fs::write(root.path().join("syntax.py"), concat!(
            "from __future__ import annotations\n",
            "from . import local\n",
            "from ..pkg import (\n",
            "    first as renamed,\n",
            "    second,\n",
            ")\n",
            "# def fake(): pass\n",
            "text = 'import ghost'\n",
            "@decorator\n",
            "def real(): pass\n",
        )).unwrap();
        let answer = query(root.path(), Query::File("syntax.py".into())).unwrap();
        assert_eq!(answer.status, "ok");
        let imports = answer.rows.iter().filter(|row| row.kind == "python_import")
            .map(|row| row.name.as_str()).collect::<Vec<_>>();
        assert_eq!(imports, ["__future__.annotations", ".local", "..pkg.first", "..pkg.second"]);
        assert!(answer.rows.iter().any(|row| row.kind == "function" && row.name == "real" && row.line == 10));
        assert!(!answer.rows.iter().any(|row| row.name == "fake" || row.name == "ghost"));
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
    fn empty_requested_file_keeps_its_source_hash_for_snapshot_export() {
        let root = worktree();
        let source = root.path().join("empty.rs");
        fs::write(&source, "").unwrap();
        let empty = query(root.path(), Query::File("empty.rs".into())).unwrap();
        assert!(empty.rows.is_empty());
        assert_eq!(empty.requested_source_sha256.as_deref(), Some(format!("{:x}", Sha256::digest(b"")).as_str()));
        assert!(empty.requested_source_read_unix_ms.is_some());
        fs::write(&source, "fn later() {}\n").unwrap();
        let changed = query(root.path(), Query::File("empty.rs".into())).unwrap();
        assert_ne!(empty.requested_source_sha256, changed.requested_source_sha256);
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

    #[test]
    fn call_edges_preserve_ambiguous_candidates_and_unresolved_receivers() {
        let root = worktree();
        fs::write(root.path().join("caller.rs"), "fn run(x: &str) { same(); only(); absent(); x.len(); }\nfn only() {}\n").unwrap();
        fs::write(root.path().join("left.rs"), "mod left { fn same() {} }\n").unwrap();
        fs::write(root.path().join("right.rs"), "mod right { fn same() {} }\n").unwrap();
        let answer = query(root.path(), Query::Calls("caller.rs".into())).unwrap();
        assert_eq!(answer.edges.len(), 4);
        let same = &answer.edges[0];
        assert_eq!(same.caller, "run");
        assert_eq!(same.target, "same");
        assert_eq!(same.binding, "ambiguous");
        assert_eq!(same.reason, "multiple_observed_name_matches");
        assert_eq!(same.candidates.len(), 2);
        assert_eq!(same.candidates[0].qualified, "left::same");
        assert_eq!(same.candidates[1].qualified, "right::same");
        assert_eq!(same.candidates[0].file, "left.rs");
        assert!(same.candidates.iter().all(|item| item.sha256.len() == 64 && item.read_unix_ms > 0));
        assert_eq!(answer.edges[1].binding, "candidate_only");
        assert_eq!(answer.edges[1].candidates[0].file, "caller.rs");
        assert_eq!(answer.edges[2].binding, "unresolved");
        assert_eq!(answer.edges[2].reason, "no_observed_name_match");
        assert_eq!(answer.edges[3].form, "method_receiver");
        assert_eq!(answer.edges[3].reason, "receiver_type_unknown");
        assert!(answer.edges[3].candidates.is_empty());
        assert!(answer.edges.iter().all(|edge| edge.file == "caller.rs" && edge.line == 1
            && edge.sha256.len() == 64 && edge.read_unix_ms > 0));
    }

    #[test]
    fn call_candidates_follow_renames_and_never_read_symlink_targets() {
        let root = worktree();
        let outside = tempfile::tempdir().unwrap();
        fs::write(root.path().join("caller.rs"), "fn run() { moved(); outside(); }\n").unwrap();
        fs::write(root.path().join("target.rs"), "fn moved() {}\n").unwrap();
        let first = query(root.path(), Query::Calls("caller.rs".into())).unwrap();
        assert_eq!(first.edges[0].candidates[0].file, "target.rs");
        fs::rename(root.path().join("target.rs"), root.path().join("renamed.rs")).unwrap();
        let renamed = query(root.path(), Query::Calls("caller.rs".into())).unwrap();
        assert_eq!(renamed.edges[0].candidates[0].file, "renamed.rs");
        assert_eq!(renamed.edges[0].candidates[0].sha256, first.edges[0].candidates[0].sha256);
        fs::write(outside.path().join("secret.rs"), "fn outside() {}\n").unwrap();
        symlink(outside.path().join("secret.rs"), root.path().join("linked.rs")).unwrap();
        let symlinked = query(root.path(), Query::Calls("caller.rs".into())).unwrap();
        assert_eq!(symlinked.edges[1].binding, "unresolved");
        assert!(symlinked.edges[1].candidates.is_empty());
        assert!(symlinked.coverage.skipped.examples.iter().any(|issue| issue.file == "linked.rs"));
        fs::remove_file(root.path().join("renamed.rs")).unwrap();
        symlink(outside.path().join("secret.rs"), root.path().join("renamed.rs")).unwrap();
        let replaced = query(root.path(), Query::Calls("caller.rs".into())).unwrap();
        assert_eq!(replaced.edges[0].binding, "unresolved");
        assert!(replaced.coverage.skipped.examples.iter().any(|issue| issue.file == "renamed.rs"));
    }

    #[test]
    fn nested_local_items_are_not_attributed_to_the_outer_caller() {
        let root = worktree();
        fs::write(root.path().join("local.rs"), "fn outer() { fn inner() { hidden(); } shown(); }\n").unwrap();
        let answer = query(root.path(), Query::Calls("local.rs".into())).unwrap();
        assert_eq!(answer.edges.len(), 1);
        assert_eq!(answer.edges[0].target, "shown");
        assert_eq!(answer.edges[0].caller, "outer");
    }

    #[test]
    fn call_pages_and_candidate_fanout_report_omissions() {
        let root = worktree();
        let mut caller = "fn run() { many();\n".to_owned();
        caller.push_str(&"missing();\n".repeat(MAX_ROWS + 2));
        caller.push_str("}\n");
        fs::write(root.path().join("caller.rs"), caller).unwrap();
        for number in 0..MAX_EDGE_CANDIDATES + 1 {
            fs::write(root.path().join(format!("candidate_{number}.rs")), "fn many() {}\n").unwrap();
        }
        let answer = query(root.path(), Query::Calls("caller.rs".into())).unwrap();
        assert_eq!(answer.edges.len(), MAX_ROWS);
        assert_eq!(answer.omitted_edges, 3);
        assert_eq!(answer.edges[0].binding, "ambiguous");
        assert_eq!(answer.edges[0].candidates.len(), MAX_EDGE_CANDIDATES);
        assert_eq!(answer.edges[0].omitted_candidates, 1);
    }

    #[test]
    fn modules_resolve_plain_root_mod_rs_and_child_layout_with_hashes() {
        let root = worktree();
        fs::create_dir_all(root.path().join("src/folder")).unwrap();
        fs::create_dir_all(root.path().join("src/direct")).unwrap();
        fs::write(root.path().join("src/lib.rs"), "mod direct;\nmod folder;\n").unwrap();
        fs::write(root.path().join("src/direct.rs"), "mod leaf;\n").unwrap();
        fs::write(root.path().join("src/direct/leaf.rs"), "pub fn leaf() {}\n").unwrap();
        fs::write(root.path().join("src/folder/mod.rs"), "mod nested;\n").unwrap();
        fs::write(root.path().join("src/folder/nested.rs"), "pub fn nested() {}\n").unwrap();
        let root_answer = query(root.path(), Query::Modules("src/lib.rs".into())).unwrap();
        assert_eq!(root_answer.module_edges.len(), 2);
        assert_eq!(root_answer.module_edges[0].target.as_deref(), Some("src/direct.rs"));
        assert_eq!(root_answer.module_edges[1].target.as_deref(), Some("src/folder/mod.rs"));
        assert!(root_answer.module_edges.iter().all(|edge| edge.resolution == "structural_only"
            && edge.source_sha256.len() == 64 && edge.target_sha256.as_ref().is_some_and(|sha| sha.len() == 64)
            && edge.source_read_unix_ms > 0 && edge.target_read_unix_ms.is_some_and(|time| time > 0)));
        let direct = query(root.path(), Query::Modules("src/direct.rs".into())).unwrap();
        assert_eq!(direct.module_edges[0].target.as_deref(), Some("src/direct/leaf.rs"));
        let mod_rs = query(root.path(), Query::Modules("src/folder/mod.rs".into())).unwrap();
        assert_eq!(mod_rs.module_edges[0].target.as_deref(), Some("src/folder/nested.rs"));
    }

    #[test]
    fn modules_keep_cfg_candidates_distinct_from_literal_path_targets_and_unknown_declarations() {
        let root = worktree();
        fs::create_dir_all(root.path().join("src/both")).unwrap();
        fs::write(root.path().join("src/lib.rs"), "mod both;\nmod repeat;\nmod repeat;\n#[cfg(unix)] mod conditional;\n#[path = \"elsewhere.rs\"] mod routed;\nmod inline { mod hidden; }\n").unwrap();
        fs::write(root.path().join("src/both.rs"), "").unwrap();
        fs::write(root.path().join("src/both/mod.rs"), "").unwrap();
        fs::write(root.path().join("src/repeat.rs"), "").unwrap();
        fs::write(root.path().join("src/conditional.rs"), "").unwrap();
        fs::write(root.path().join("src/elsewhere.rs"), "").unwrap();
        let answer = query(root.path(), Query::Modules("src/lib.rs".into())).unwrap();
        assert_eq!(answer.module_edges.len(), 6);
        assert_eq!(answer.module_edges[0].reason, "ambiguous_layout");
        assert_eq!(answer.module_edges[1].reason, "duplicate_declaration");
        assert_eq!(answer.module_edges[2].reason, "duplicate_declaration");
        assert_eq!(answer.module_edges[3].reason, "cfg_layout_candidate");
        assert_eq!(answer.module_edges[3].resolution, "unknown");
        assert!(answer.module_edges[3].target.is_none());
        assert_eq!(answer.module_edges[3].conditional_candidate.as_deref(), Some("src/conditional.rs"));
        assert!(answer.module_edges[3].conditional_candidate_sha256.is_some());
        assert_eq!(answer.module_edges[4].reason, "literal_path_file");
        assert_eq!(answer.module_edges[4].resolution, "structural_only");
        assert_eq!(answer.module_edges[4].target.as_deref(), Some("src/elsewhere.rs"));
        assert_eq!(answer.module_edges[5].reason, "inline_module_skipped");
        assert_eq!(answer.skipped_nested_modules, 1);
        assert!([0, 1, 2, 5].iter().all(|index| answer.module_edges[*index].resolution == "unknown"
            && answer.module_edges[*index].target.is_none()
            && answer.module_edges[*index].conditional_candidate.is_none()));
    }

    #[test]
    fn literal_path_is_relative_to_containing_file_and_cfg_path_remains_a_candidate() {
        let root = worktree();
        fs::create_dir_all(root.path().join("src/alternate")).unwrap();
        fs::write(root.path().join("src/parent.rs"), "#[path = \"alternate/leaf.rs\"] mod leaf;\n#[cfg(unix)] #[path = \"alternate/conditional.rs\"] mod conditional;\n").unwrap();
        fs::write(root.path().join("src/alternate/leaf.rs"), "pub fn leaf() {}\n").unwrap();
        fs::write(root.path().join("src/alternate/conditional.rs"), "pub fn conditional() {}\n").unwrap();
        let answer = query(root.path(), Query::Modules("src/parent.rs".into())).unwrap();
        assert_eq!(answer.module_edges[0].target.as_deref(), Some("src/alternate/leaf.rs"));
        assert_eq!(answer.module_edges[0].reason, "literal_path_file");
        assert_eq!(answer.module_edges[1].resolution, "unknown");
        assert!(answer.module_edges[1].target.is_none());
        assert_eq!(answer.module_edges[1].conditional_candidate.as_deref(),
            Some("src/alternate/conditional.rs"));
        assert_eq!(answer.module_edges[1].reason, "cfg_literal_path_candidate");
    }

    #[test]
    fn path_overrides_that_are_unsafe_or_not_exactly_scanned_remain_unknown() {
        let root = worktree();
        fs::create_dir_all(root.path().join("src/alternate")).unwrap();
        fs::write(root.path().join(".gitignore"), "src/alternate/ignored.rs\n").unwrap();
        fs::write(root.path().join("src/lib.rs"), "#[cfg_attr(unix, path = \"alternate/other.rs\")] mod cfg_attr;\n#[path = \"../outside.rs\"] mod traversal;\n#[path = \"/absolute.rs\"] mod absolute;\n#[path = concat!(\"alternate/\", \"other.rs\")] mod macro_path;\n#[path = \"alternate/ignored.rs\"] mod ignored;\n#[path = \"alternate/broken.rs\"] mod broken;\n").unwrap();
        fs::write(root.path().join("src/alternate/other.rs"), "pub fn other() {}\n").unwrap();
        fs::write(root.path().join("src/alternate/ignored.rs"), "pub fn ignored() {}\n").unwrap();
        fs::write(root.path().join("src/alternate/broken.rs"), "fn {\n").unwrap();
        let answer = query(root.path(), Query::Modules("src/lib.rs".into())).unwrap();
        assert_eq!(answer.module_edges.len(), 6);
        assert!(answer.module_edges[..4].iter().all(|edge| edge.reason == "module_attribute_unresolved"));
        assert_eq!(answer.module_edges[4].reason, "unlisted_candidate_exists");
        assert_eq!(answer.module_edges[5].reason, "candidate_unparseable");
        assert!(answer.module_edges.iter().all(|edge| edge.resolution == "unknown"
            && edge.target.is_none() && edge.conditional_candidate.is_none()));
    }

    #[test]
    fn modules_skip_symlinks_ignored_files_and_unlisted_siblings() {
        let root = worktree();
        let outside = tempfile::tempdir().unwrap();
        fs::create_dir_all(root.path().join("src/visible")).unwrap();
        fs::write(root.path().join(".gitignore"), "src/ignored.rs\nsrc/visible/mod.rs\n").unwrap();
        fs::write(root.path().join("src/lib.rs"), "mod linked;\nmod ignored;\nmod visible;\nmod parentlink;\n").unwrap();
        fs::write(outside.path().join("secret.rs"), "pub fn secret() {}\n").unwrap();
        symlink(outside.path().join("secret.rs"), root.path().join("src/linked.rs")).unwrap();
        fs::write(root.path().join("src/ignored.rs"), "").unwrap();
        fs::write(root.path().join("src/visible.rs"), "").unwrap();
        fs::write(root.path().join("src/visible/mod.rs"), "").unwrap();
        fs::write(root.path().join("src/parentlink.rs"), "").unwrap();
        fs::create_dir(outside.path().join("module_dir")).unwrap();
        fs::write(outside.path().join("module_dir/mod.rs"), "").unwrap();
        symlink(outside.path().join("module_dir"), root.path().join("src/parentlink")).unwrap();
        let answer = query(root.path(), Query::Modules("src/lib.rs".into())).unwrap();
        assert_eq!(answer.module_edges[0].reason, "candidate_skipped");
        assert_eq!(answer.module_edges[1].reason, "unlisted_candidate_exists");
        assert_eq!(answer.module_edges[2].reason, "unlisted_candidate_exists");
        assert_eq!(answer.module_edges[3].reason, "unlisted_candidate_exists");
        assert!(answer.module_edges.iter().all(|edge| edge.target_sha256.is_none()));
        assert!(answer.coverage.skipped.examples.iter().any(|issue| issue.file == "src/linked.rs"));
    }

    #[test]
    fn modules_follow_target_edits_and_layout_renames() {
        let root = worktree();
        fs::create_dir_all(root.path().join("src/child")).unwrap();
        fs::write(root.path().join("src/main.rs"), "mod child;\n").unwrap();
        fs::write(root.path().join("src/child.rs"), "pub fn first() {}\n").unwrap();
        let first = query(root.path(), Query::Modules("src/main.rs".into())).unwrap();
        let first_sha = first.module_edges[0].target_sha256.clone().unwrap();
        fs::write(root.path().join("src/child.rs"), "pub fn changed() {}\n").unwrap();
        let changed = query(root.path(), Query::Modules("src/main.rs".into())).unwrap();
        assert_ne!(changed.module_edges[0].target_sha256.as_deref(), Some(first_sha.as_str()));
        fs::rename(root.path().join("src/child.rs"), root.path().join("src/child/mod.rs")).unwrap();
        let renamed = query(root.path(), Query::Modules("src/main.rs".into())).unwrap();
        assert_eq!(renamed.module_edges[0].target.as_deref(), Some("src/child/mod.rs"));
        fs::remove_file(root.path().join("src/child/mod.rs")).unwrap();
        let missing = query(root.path(), Query::Modules("src/main.rs".into())).unwrap();
        assert_eq!(missing.module_edges[0].reason, "no_listed_candidate");
    }

    #[test]
    fn module_page_counts_omissions_and_obeys_reply_cap() {
        let root = worktree();
        let content = (0..MAX_ROWS + 7).map(|n| format!("mod child{n};\n")).collect::<String>();
        fs::write(root.path().join("lib.rs"), content).unwrap();
        let answer = query(root.path(), Query::Modules("lib.rs".into())).unwrap();
        assert_eq!(answer.module_edges.len(), MAX_ROWS);
        assert_eq!(answer.omitted_module_edges, 7);
        assert!(serde_json::to_vec(&answer).unwrap().len() <= MAX_REPLY_BYTES);
    }

    #[test]
    fn module_reply_rejects_over_64_kib_instead_of_truncating_silently() {
        let root = worktree();
        let deep = ["a".repeat(180), "b".repeat(180), "c".repeat(180), "d".repeat(180)]
            .join("/");
        fs::create_dir_all(root.path().join(&deep)).unwrap();
        let source = format!("{deep}/lib.rs");
        let content = (0..MAX_ROWS).map(|n| format!("mod child{n};\n")).collect::<String>();
        fs::write(root.path().join(&source), content).unwrap();
        assert_eq!(query(root.path(), Query::Modules(source)).unwrap_err(),
            "query reply exceeds 64 KiB; narrow the query");
    }
}
