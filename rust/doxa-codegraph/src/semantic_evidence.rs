//! Fail-closed inspection of a Rust LSP definition reply.
//!
//! This module does not launch or trust a language server. Matching an LSP
//! reply to parsed source is useful evidence, but it is never a binding claim
//! until a separate sandboxed producer can prove its configuration, isolation,
//! and quiescence. No caller may promote this result to `verified`.

use super::{file_bytes, listed_files, parse_rust, worktree_root, CallCandidate, CallEdge};
use serde::Serialize;
use serde_json::Value;
use std::path::Path;

const MAX_MESSAGE_BYTES: usize = 32 * 1024;

#[derive(Debug, Serialize)]
pub struct DefinitionEvidence {
    pub status: &'static str,
    pub binding: &'static str,
    pub reason: &'static str,
    pub source_file: String,
    pub source_sha256: String,
    pub target_file: String,
    pub target_sha256: String,
    pub target_name: String,
}

fn limited(value: &Value) -> Result<(), String> {
    if serde_json::to_vec(value).map_err(|e| e.to_string())?.len() > MAX_MESSAGE_BYTES {
        return Err("LSP message exceeds 32 KiB".into());
    }
    Ok(())
}

fn uri_path(root: &Path, uri: &str) -> Result<String, String> {
    // An unescaped local file URI is the only supported transport in this
    // foundation. Reject percent encoding, query strings, authorities, and
    // lexical traversal rather than guessing their meaning.
    let absolute = uri.strip_prefix("file://").ok_or("non-file LSP URI")?;
    if !absolute.starts_with('/') || absolute.contains(['%', '?', '#', '\0']) {
        return Err("unsupported or encoded LSP file URI".into());
    }
    let path = Path::new(absolute);
    if path.components().any(|part| matches!(part, std::path::Component::ParentDir | std::path::Component::CurDir)) {
        return Err("LSP URI contains traversal".into());
    }
    let relative = path.strip_prefix(root).map_err(|_| "LSP URI is outside the worktree")?;
    let relative = relative.to_str().ok_or("non-UTF-8 LSP path")?;
    if relative.is_empty() { return Err("LSP URI names the worktree root".into()); }
    Ok(relative.to_owned())
}

fn position(value: &Value) -> Result<(usize, usize), String> {
    let line = value.get("line").and_then(Value::as_u64).ok_or("missing LSP line")?;
    let col = value.get("character").and_then(Value::as_u64).ok_or("missing LSP UTF-16 column")?;
    Ok((usize::try_from(line).map_err(|_| "LSP line overflow")?,
        usize::try_from(col).map_err(|_| "LSP column overflow")?))
}

fn single_location(response: &Value) -> Result<&Value, String> {
    let result = response.get("result").ok_or("missing LSP result")?;
    match result {
        Value::Array(locations) if locations.len() == 1 => Ok(&locations[0]),
        Value::Object(_) => Ok(result),
        _ => Err("LSP definition is missing or ambiguous".into()),
    }
}

fn ascii_line(content: &str, line: usize) -> Result<&str, String> {
    let text = content.lines().nth(line).ok_or("LSP line is outside source")?;
    if !text.is_ascii() { return Err("non-ASCII LSP line requires UTF-16 mapping".into()); }
    Ok(text)
}

fn declaration_at(content: &str, name: &str, line: usize, column: usize) -> Result<bool, String> {
    fn ident_matches(ident: &syn::Ident, name: &str, line: usize, column: usize) -> bool {
        let start = ident.span().start();
        ident == name && start.line == line + 1 && start.column == column
    }
    fn items_match(items: &[syn::Item], name: &str, line: usize, column: usize) -> bool {
        items.iter().any(|item| match item {
            syn::Item::Fn(value) => ident_matches(&value.sig.ident, name, line, column),
            syn::Item::Mod(value) => value.content.as_ref().is_some_and(|(_, nested)| items_match(nested, name, line, column)),
            syn::Item::Trait(value) => value.items.iter().any(|item| match item {
                syn::TraitItem::Fn(method) => ident_matches(&method.sig.ident, name, line, column),
                _ => false,
            }),
            syn::Item::Impl(value) => value.items.iter().any(|item| match item {
                syn::ImplItem::Fn(method) => ident_matches(&method.sig.ident, name, line, column),
                _ => false,
            }),
            _ => false,
        })
    }
    let syntax = syn::parse_file(content).map_err(|e| format!("Rust parse error: {e}"))?;
    Ok(items_match(&syntax.items, name, line, column))
}

/// Check one definition exchange against a *fresh* codegraph call edge and
/// one of its displayed candidates. The output is always `binding: unknown`.
/// It intentionally does not accept a server's claim of sandbox provenance.
pub fn inspect_definition_reply(
    root: &Path,
    edge: &CallEdge,
    candidate: &CallCandidate,
    request: &Value,
    response: &Value,
) -> Result<DefinitionEvidence, String> {
    inspect_definition_reply_with_pre_final_rehash(root, edge, candidate, request, response, |_| {})
}

fn inspect_definition_reply_with_pre_final_rehash(
    root: &Path,
    edge: &CallEdge,
    candidate: &CallCandidate,
    request: &Value,
    response: &Value,
    before_final_rehash: impl FnOnce(&Path),
) -> Result<DefinitionEvidence, String> {
    limited(request)?;
    limited(response)?;
    let root = worktree_root(root)?;
    let listed = listed_files(&root)?;
    if edge.form != "function_path" || !edge.target.is_ascii()
        || edge.target.is_empty() || !edge.target.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'_') {
        return Err("only simple ASCII function calls are supported".into());
    }
    if !listed.contains(&edge.file) || !listed.contains(&candidate.file) {
        return Err("source or target is outside the listed worktree".into());
    }
    if !edge.candidates.iter().any(|observed| observed.file == candidate.file
        && observed.line == candidate.line && observed.qualified == candidate.qualified
        && observed.sha256 == candidate.sha256) {
        return Err("definition was not a displayed call candidate".into());
    }
    if request.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
        || response.get("jsonrpc").and_then(Value::as_str) != Some("2.0")
        || request.get("method").and_then(Value::as_str) != Some("textDocument/definition")
        || request.get("id").is_none() || request.get("id") != response.get("id")
        || response.get("error").is_some() {
        return Err("LSP definition exchange is invalid".into());
    }
    let source_uri = request.pointer("/params/textDocument/uri").and_then(Value::as_str)
        .ok_or("missing source URI")?;
    if uri_path(&root, source_uri)? != edge.file { return Err("LSP source URI differs from call edge".into()); }
    let (source_line, source_column) = position(request.pointer("/params/position").ok_or("missing definition position")?)?;
    if source_line.checked_add(1) != Some(edge.line) || source_column != edge.column {
        return Err("LSP definition position differs from call edge".into());
    }
    let (source, source_hash, source_time) = file_bytes(&root, &edge.file)?;
    if source_hash != edge.sha256 { return Err("call source changed since the query".into()); }
    let source_text = ascii_line(&source, source_line)?;
    let remainder = source_text.get(source_column..).ok_or("call column outside source")?;
    if !remainder.starts_with(&edge.target)
        || remainder.as_bytes().get(edge.target.len()).is_some_and(|byte| byte.is_ascii_alphanumeric() || *byte == b'_') {
        return Err("call spelling differs from source".into());
    }
    let calls = parse_rust(&source, &edge.file, &source_hash, source_time, true)?.calls;
    if !calls.iter().any(|observed| observed.line == edge.line && observed.column == edge.column
        && observed.form == edge.form && observed.target == edge.target) {
        return Err("call edge is absent from current Rust parse".into());
    }

    let location = single_location(response)?;
    let (uri, range) = if let Some(uri) = location.get("uri").and_then(Value::as_str) {
        (uri, location.get("range").ok_or("missing definition range")?)
    } else {
        let uri = location.get("targetUri").and_then(Value::as_str).ok_or("missing target URI")?;
        let range = location.get("targetSelectionRange").ok_or("missing target selection range")?;
        if let Some(origin) = location.get("originSelectionRange") {
            let (line, start) = position(origin.get("start").ok_or("missing origin start")?)?;
            let (end_line, end) = position(origin.get("end").ok_or("missing origin end")?)?;
            if line != source_line || end_line != line || start != source_column
                || end != source_column + edge.target.len() {
                return Err("LSP origin selection differs from call token".into());
            }
        }
        (uri, range)
    };
    let target_file = uri_path(&root, uri)?;
    if target_file != candidate.file { return Err("LSP target differs from candidate".into()); }
    let (start_line, start_col) = position(range.get("start").ok_or("missing target start")?)?;
    let (end_line, end_col) = position(range.get("end").ok_or("missing target end")?)?;
    if start_line != end_line || start_col >= end_col { return Err("invalid or multiline target range".into()); }
    let (target, target_hash, target_time) = file_bytes(&root, &target_file)?;
    if target_hash != candidate.sha256 { return Err("definition source changed since the query".into()); }
    let target_line = ascii_line(&target, start_line)?;
    let selected = target_line.get(start_col..end_col).ok_or("definition range outside source")?;
    if selected != edge.target || candidate.line != start_line + 1 {
        return Err("LSP target selection differs from parsed declaration".into());
    }
    let parsed = parse_rust(&target, &target_file, &target_hash, target_time, false)?;
    if !parsed.symbols.iter().any(|row| row.line == candidate.line && row.name == edge.target
        && row.qualified == candidate.qualified && matches!(row.kind, "function" | "method" | "trait_method")) {
        return Err("LSP target is not a parsed function declaration".into());
    }
    if !declaration_at(&target, &edge.target, start_line, start_col)? {
        return Err("LSP range does not select the declaration identifier".into());
    }
    // The source was read before parsing the target. Check both names and
    // hashes once more, after all protocol and syntax work, to avoid returning
    // an apparently current match if either file changed during inspection.
    // This still cannot make a mutable worktree an atomic snapshot.
    before_final_rehash(&root);
    if super::source_sha256(&root, &edge.file)? != source_hash {
        return Err("call source changed during definition inspection".into());
    }
    if super::source_sha256(&root, &target_file)? != target_hash {
        return Err("definition source changed during inspection".into());
    }
    if listed_files(&root)? != listed {
        return Err("Git worktree listing changed during definition inspection".into());
    }
    Ok(DefinitionEvidence {
        status: "protocol_match_untrusted",
        binding: "unknown",
        reason: "sandboxed_quiescent_language_server_not_attested",
        source_file: edge.file.clone(), source_sha256: source_hash,
        target_file, target_sha256: target_hash, target_name: edge.target.clone(),
    })
}


#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;
    use std::os::unix::fs::symlink;
    use std::process::Command;
    use tempfile::TempDir;

    fn fixture() -> (TempDir, CallEdge, CallCandidate, Value, Value) {
        let root = tempfile::tempdir().unwrap();
        assert!(Command::new("git").arg("init").arg("-q").arg(root.path()).status().unwrap().success());
        fs::write(root.path().join("a.rs"), "fn caller() { target(); }\n").unwrap();
        fs::write(root.path().join("b.rs"), "fn target() {}\n").unwrap();
        let answer = super::super::query(root.path(), super::super::Query::Calls("a.rs".into())).unwrap();
        let edge = answer.edges.into_iter().next().unwrap();
        let candidate = edge.candidates[0].clone();
        let source_uri = format!("file://{}/a.rs", root.path().display());
        let target_uri = format!("file://{}/b.rs", root.path().display());
        let request = json!({"jsonrpc":"2.0","id":7,"method":"textDocument/definition",
            "params":{"textDocument":{"uri":source_uri},
                "position":{"line":0,"character":edge.column}}});
        let response = json!({"jsonrpc":"2.0","id":7,"result":{"uri":target_uri,
            "range":{"start":{"line":0,"character":3},"end":{"line":0,"character":9}}}});
        (root, edge, candidate, request, response)
    }

    #[test]
    fn matching_protocol_evidence_never_becomes_verified_binding() {
        let (root, edge, candidate, request, response) = fixture();
        let evidence = inspect_definition_reply(root.path(), &edge, &candidate, &request, &response).unwrap();
        assert_eq!(evidence.status, "protocol_match_untrusted");
        assert_eq!(evidence.binding, "unknown");
        assert_eq!(evidence.target_name, "target");
    }

    #[test]
    fn stale_source_or_target_fails_closed() {
        let (root, edge, candidate, request, response) = fixture();
        fs::write(root.path().join("a.rs"), "fn caller() { target(); }\n// edit\n").unwrap();
        assert!(inspect_definition_reply(root.path(), &edge, &candidate, &request, &response).is_err());
        fs::write(root.path().join("a.rs"), "fn caller() { target(); }\n").unwrap();
        fs::write(root.path().join("b.rs"), "fn target() {}\n// edit\n").unwrap();
        assert!(inspect_definition_reply(root.path(), &edge, &candidate, &request, &response).is_err());
    }

    #[test]
    fn source_target_and_listing_changes_during_inspection_fail_closed() {
        let (root, edge, candidate, request, response) = fixture();
        let error = inspect_definition_reply_with_pre_final_rehash(root.path(), &edge, &candidate,
            &request, &response, |root| {
                fs::write(root.join("a.rs"), "fn caller() { target(); }\n// late\n").unwrap();
            }).unwrap_err();
        assert_eq!(error, "call source changed during definition inspection");

        fs::write(root.path().join("a.rs"), "fn caller() { target(); }\n").unwrap();
        let error = inspect_definition_reply_with_pre_final_rehash(root.path(), &edge, &candidate,
            &request, &response, |root| {
                fs::write(root.join("b.rs"), "fn target() {}\n// late\n").unwrap();
            }).unwrap_err();
        assert_eq!(error, "definition source changed during inspection");

        fs::write(root.path().join("b.rs"), "fn target() {}\n").unwrap();
        let error = inspect_definition_reply_with_pre_final_rehash(root.path(), &edge, &candidate,
            &request, &response, |root| {
                fs::write(root.join("new.rs"), "fn new() {}\n").unwrap();
            }).unwrap_err();
        assert_eq!(error, "Git worktree listing changed during definition inspection");
    }

    #[test]
    fn ambiguous_wrong_id_and_external_reply_fail_closed() {
        let (root, edge, candidate, request, mut response) = fixture();
        let original = response["result"].clone();
        response["result"] = json!([original.clone(), original.clone()]);
        assert!(inspect_definition_reply(root.path(), &edge, &candidate, &request, &response).is_err());
        response["result"] = original;
        response["id"] = json!(8);
        assert!(inspect_definition_reply(root.path(), &edge, &candidate, &request, &response).is_err());
        response["id"] = json!(7);
        response["result"]["uri"] = json!("file:///etc/passwd");
        assert!(inspect_definition_reply(root.path(), &edge, &candidate, &request, &response).is_err());
    }

    #[test]
    fn range_must_select_the_parsed_identifier_not_same_line_comment() {
        let (root, edge, _candidate, request, mut response) = fixture();
        fs::write(root.path().join("b.rs"), "fn target() {} // target\n").unwrap();
        let mut edge = edge;
        let candidate = super::super::query(root.path(), super::super::Query::Calls("a.rs".into()))
            .unwrap().edges.remove(0).candidates.remove(0);
        edge.candidates = vec![candidate.clone()];
        response["result"]["range"]["start"]["character"] = json!(18);
        response["result"]["range"]["end"]["character"] = json!(24);
        assert_eq!(inspect_definition_reply(root.path(), &edge, &candidate, &request, &response)
            .unwrap_err(), "LSP range does not select the declaration identifier");
    }

    #[test]
    fn location_link_origin_must_match_the_call_token() {
        let (root, edge, candidate, request, mut response) = fixture();
        let target_uri = response["result"]["uri"].clone();
        let range = response["result"]["range"].clone();
        response["result"] = json!({"targetUri":target_uri,"targetSelectionRange":range,
            "originSelectionRange":{"start":{"line":0,"character":0},
                "end":{"line":0,"character":6}}});
        assert!(inspect_definition_reply(root.path(), &edge, &candidate, &request, &response).is_err());
        response["result"]["originSelectionRange"]["start"]["character"] = json!(edge.column);
        response["result"]["originSelectionRange"]["end"]["character"] = json!(edge.column + edge.target.len());
        assert!(inspect_definition_reply(root.path(), &edge, &candidate, &request, &response).is_ok());
    }

    #[test]
    fn encoded_uri_symlink_and_oversized_reply_fail_closed() {
        let (root, edge, candidate, request, mut response) = fixture();
        response["result"]["uri"] = json!(format!("file://{}/%62.rs", root.path().display()));
        assert!(inspect_definition_reply(root.path(), &edge, &candidate, &request, &response).is_err());
        response["result"]["uri"] = json!(format!("file://{}/b.rs", root.path().display()));
        response["padding"] = json!("x".repeat(MAX_MESSAGE_BYTES));
        assert!(inspect_definition_reply(root.path(), &edge, &candidate, &request, &response).is_err());
        response.as_object_mut().unwrap().remove("padding");
        fs::remove_file(root.path().join("b.rs")).unwrap();
        symlink("/etc/passwd", root.path().join("b.rs")).unwrap();
        assert!(inspect_definition_reply(root.path(), &edge, &candidate, &request, &response).is_err());
    }
}
