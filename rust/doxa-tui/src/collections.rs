//! Python-compatible named session collections. The flat tab list remains
//! authoritative; collections only order and label its members.

use std::collections::HashSet;
use serde_json::{json, Value};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Collection {
    pub name: String,
    pub sessions: Vec<String>,
    pub collapsed: bool,
}

fn clean_name(raw: &str) -> String {
    raw.chars().filter(|ch| !ch.is_control() || ch.is_whitespace())
        .collect::<String>().split_whitespace().collect::<Vec<_>>()
        .join(" ").chars().take(48).collect()
}

fn key(raw: &str) -> String { clean_name(raw).to_lowercase() }

pub fn from_json(value: Option<&Value>, keep: &HashSet<String>) -> Vec<Collection> {
    let mut names = HashSet::new();
    let mut placed = HashSet::new();
    let mut out = Vec::new();
    for row in value.and_then(Value::as_array).into_iter().flatten() {
        let Some(name) = row.get("name").and_then(Value::as_str).map(clean_name).filter(|name| !name.is_empty()) else { continue };
        if names.contains(&key(&name)) { continue; }
        let Some(members) = row.get("sessions").and_then(Value::as_array) else { continue };
        let sessions: Vec<String> = members.iter().filter_map(Value::as_str)
            .map(str::trim).filter(|id| keep.contains(*id) && placed.insert((*id).to_owned()))
            .map(str::to_owned).collect();
        if sessions.is_empty() { continue; }
        names.insert(key(&name));
        out.push(Collection { name, sessions, collapsed: row.get("collapsed").and_then(Value::as_bool).unwrap_or(false) });
    }
    out
}

pub fn to_json(items: &[Collection], keep: &HashSet<String>) -> Vec<Value> {
    let mut placed = HashSet::new();
    items.iter().filter_map(|item| {
        let sessions: Vec<_> = item.sessions.iter()
            .filter(|id| keep.contains(*id) && placed.insert((*id).clone())).cloned().collect();
        if sessions.is_empty() { return None; }
        let mut row = json!({"name": item.name, "sessions": sessions});
        if item.collapsed { row["collapsed"] = Value::Bool(true); }
        Some(row)
    }).collect()
}

pub fn edit(items: &mut Vec<Collection>, verb: &str, rest: &str, active: Option<&str>) -> Result<String, String> {
    let name = clean_name(rest);
    let index = |items: &[Collection], name: &str| items.iter().position(|item| key(&item.name) == key(name));
    match verb {
        "new" => {
            if name.is_empty() { return Err("a collection needs a name".into()); }
            if index(items, &name).is_some() { return Err(format!("there is already a collection called {name:?}")); }
            items.push(Collection { name: name.clone(), sessions: Vec::new(), collapsed: false });
            Ok(format!("collection {name:?} — empty, so far"))
        }
        "rename" => {
            let Some((old, new)) = rest.split_once(char::is_whitespace) else { return Err("Usage: /collection rename <old> <new>".into()) };
            let new = clean_name(new);
            if new.is_empty() { return Err("a collection needs a name".into()); }
            let Some(target) = index(items, old) else { return Err(format!("no collection called {:?}", clean_name(old))) };
            if index(items, &new).is_some_and(|other| other != target) { return Err(format!("there is already a collection called {new:?}")); }
            items[target].name = new.clone();
            Ok(format!("{old:?} is now {new:?}"))
        }
        "delete" => {
            let Some(target) = index(items, &name) else { return Err(format!("no collection called {name:?}")) };
            items.remove(target);
            Ok(format!("collection {name:?} gone — its sessions are ungrouped, not closed"))
        }
        "add" | "move" | "put" => {
            let Some(id) = active else { return Err("select a session before moving it".into()) };
            if name.is_empty() { return Err("a collection needs a name".into()); }
            let target = match index(items, &name) { Some(index) => index, None => {
                items.push(Collection { name: name.clone(), sessions: Vec::new(), collapsed: false });
                items.len() - 1
            }};
            if items[target].sessions.iter().any(|member| member == id) { return Ok(format!("this session is already in {name:?}")); }
            for item in items.iter_mut() { item.sessions.retain(|member| member != id); }
            items[target].sessions.push(id.to_owned());
            Ok(format!("this session is now in {name:?}"))
        }
        "remove" | "rm" | "out" => {
            let Some(id) = active else { return Err("select a session before removing it".into()) };
            let Some(item) = items.iter_mut().find(|item| item.sessions.iter().any(|member| member == id))
                else { return Err("this session is not in a collection".into()) };
            item.sessions.retain(|member| member != id);
            Ok("this session is now ungrouped".into())
        }
        _ => Err("Usage: /collection new|rename|delete|add|remove [name]".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn migration_preserves_order_and_first_membership() {
        let keep = ["one", "two"].into_iter().map(str::to_owned).collect();
        let input = json!([
            {"name":" Alpha ","sessions":["two","one","dead"],"collapsed":true},
            {"name":"alpha","sessions":["one"]},
            {"name":"Beta","sessions":["one","two"]}
        ]);
        let rows = from_json(Some(&input), &keep);
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].sessions, ["two", "one"]);
        assert_eq!(to_json(&rows, &keep), vec![json!({"name":"Alpha","sessions":["two","one"],"collapsed":true})]);
    }
    #[test]
    fn edits_keep_one_ordered_membership_and_delete_keeps_sessions() {
        let mut rows = Vec::new();
        edit(&mut rows, "new", "Alpha", None).unwrap();
        edit(&mut rows, "add", "Alpha", Some("one")).unwrap();
        edit(&mut rows, "add", "Beta", Some("one")).unwrap();
        edit(&mut rows, "add", "Beta", Some("two")).unwrap();
        assert!(rows[0].sessions.is_empty());
        assert_eq!(rows[1].sessions, ["one", "two"]);
        edit(&mut rows, "rename", "Beta Gamma", None).unwrap();
        assert_eq!(rows[1].name, "Gamma");
        edit(&mut rows, "delete", "Gamma", None).unwrap();
        assert_eq!(rows.len(), 1);
    }

    #[test]
    fn empty_and_pruned_collections_are_not_persisted_like_python() {
        let items = vec![
            Collection { name:"Empty".into(), sessions:vec![], collapsed:false },
            Collection { name:"Dead".into(), sessions:vec!["gone".into()], collapsed:false },
        ];
        assert!(to_json(&items, &HashSet::new()).is_empty());
    }
}
