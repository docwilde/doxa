//! Read only the LORE entries that its context snapshot actually renders.
use std::path::{Path, PathBuf};
use std::time::Duration;

fn section(snapshot: &str, prefix: &str) -> Option<Vec<String>> {
    let mut lines = snapshot.lines();
    let heading = lines.by_ref().find(|line| line.starts_with(prefix))?;
    let mut rows = vec![heading.to_owned()];
    for line in lines {
        if line.is_empty() || line.starts_with("## ") { break; }
        rows.push(line.to_owned());
    }
    Some(rows)
}

pub fn scope_path(cwd: &Path) -> (PathBuf, bool) {
    match crate::discovery::repo_root_for(cwd) {
        Some(root) => (root, true),
        None => (cwd.to_path_buf(), false),
    }
}

pub fn fetch(python: &Path, cwd: &Path) -> Result<Vec<String>, &'static str> {
    let (project, is_repo) = scope_path(cwd);
    let mut lore = doxa_lore::LoreClient::spawn(python, Duration::from_secs(3))
        .map_err(|_| "LORE unavailable")?;
    let user = lore.snapshot(cwd.to_str().ok_or("invalid session directory")?, "user")
        .map_err(|_| "User memory unavailable")?;
    let project_snapshot = lore.snapshot(project.to_str().ok_or("invalid scope directory")?, "project")
        .map_err(|_| "Scoped memory unavailable")?;
    if user.len() > 64 * 1024 || project_snapshot.len() > 64 * 1024 {
        return Err("LORE memory snapshot too large");
    }
    let mut rows = section(&user, "## User memory").ok_or("User memory section unavailable")?;
    rows.push(String::new());
    let mut scoped = section(&project_snapshot, "## Project memory")
        .ok_or("Scoped memory section unavailable")?;
    if !is_repo { scoped[0] = scoped[0].replacen("Project memory", "Folder memory", 1); }
    rows.extend(scoped);
    if let Some(hint) = project_snapshot.lines().find(|line| line.starts_with("Belief store:")) {
        rows.push(String::new());
        rows.push(hint.to_owned());
    }
    if rows.len() > 400 || rows.iter().map(String::len).sum::<usize>() > 32 * 1024 {
        return Err("LORE memory entries exceed menu limit");
    }
    Ok(rows)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::process::Command;

    fn fake_lore(dir: &Path) -> PathBuf {
        let script = dir.join("fake-lore");
        fs::write(&script, r#"#!/usr/bin/env python3
import json, sys
print(json.dumps({'type':'hello','proto':1,'capabilities':['scrub','snapshot']}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if req['scope'] == 'user':
        text = '## User memory (10/100 chars)\n- prefers concise text [source: codex]\n\nRules:\n'
    else:
        text = '## Project memory (10/100 chars) — ' + req['cwd'] + '\n- run checks\n\nBelief store: 3 active beliefs (derived, uncurated).\nRules:\n'
    print(json.dumps({'type':'reply','id':req['id'],'ok':True,'text':text}), flush=True)
"#).unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
        script
    }

    #[test]
    fn extracts_only_rendered_entries_and_belief_hint() {
        let user = "LORE MEMORY\n## User memory (12/100 chars)\n- likes short replies\n\nRules:\n- unrelated\n";
        let project = "LORE MEMORY\n## Project memory (20/100 chars) — repo\n- run task test\n\nFile map: 2 entries\n\nBelief store: 3 active beliefs (derived, uncurated).\nRules:\n";
        assert_eq!(section(user, "## User memory").unwrap(),
            vec!["## User memory (12/100 chars)", "- likes short replies"]);
        assert_eq!(section(project, "## Project memory").unwrap(),
            vec!["## Project memory (20/100 chars) — repo", "- run task test"]);
        assert!(section(user, "## Project memory").is_none());
        assert_eq!(project.lines().find(|line| line.starts_with("Belief store:")),
            Some("Belief store: 3 active beliefs (derived, uncurated)."));
    }

    #[test]
    fn plain_directory_keeps_lore_folder_scope_without_claiming_repo() {
        let dir = tempfile::tempdir().unwrap();
        assert_eq!(scope_path(dir.path()), (dir.path().to_path_buf(), false));
        let rows = fetch(&fake_lore(dir.path()), dir.path()).unwrap();
        assert!(rows.iter().any(|row| row.starts_with("## Folder memory")));
        assert!(rows.iter().any(|row| row.contains("- run checks")));
        assert!(!rows.iter().any(|row| row.starts_with("## Project memory")));
    }

    #[test]
    fn managed_worktree_reads_main_repository_memory_and_real_belief_hint() {
        let dir = tempfile::tempdir().unwrap();
        let main = dir.path().join("repo");
        let tree = dir.path().join("tree");
        fs::create_dir(&main).unwrap();
        let git = |cwd: &Path, args: &[&str]| {
            let result = Command::new("git").args(args).current_dir(cwd).output().unwrap();
            assert!(result.status.success(), "{}", String::from_utf8_lossy(&result.stderr));
        };
        git(&main, &["init", "-q", "-b", "main"]);
        fs::write(main.join("README"), "base").unwrap();
        git(&main, &["add", "README"]);
        git(&main, &["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base"]);
        git(&main, &["worktree", "add", "-q", "-b", "feature", tree.to_str().unwrap()]);
        assert_eq!(scope_path(&tree), (main.canonicalize().unwrap(), true));
        let rows = fetch(&fake_lore(dir.path()), &tree).unwrap();
        assert!(rows.iter().any(|row| row.starts_with("## User memory")));
        assert!(rows.iter().any(|row| row.contains("[source: codex]")));
        assert!(rows.iter().any(|row| row.contains(main.to_str().unwrap())));
        assert!(rows.iter().any(|row| row.starts_with("Belief store: 3 active beliefs")));
        assert!(!rows.iter().any(|row| row.contains(tree.to_str().unwrap())));
    }
}
