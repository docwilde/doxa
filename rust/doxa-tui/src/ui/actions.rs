//! One generated command surface for the registry, open tabs and session
//! operations. Query filtering does not mutate the conversation draft.
use super::{App, COMMANDS};
#[derive(Clone, Debug)]
pub enum Action { New, Plugin(String), Fleet(super::fleet_menu::SavedView), Tab(usize, usize), Command(&'static str), Stop, Tools, Close, NextPane }
#[derive(Clone, Debug)]
pub struct Entry { pub label: String, pub help: String, pub action: Action }
pub fn entries(app: &App, query: &str) -> Vec<Entry> {
    let mut rows = vec![Entry { label: "New tab".into(), help: "Select engine and model".into(), action: Action::New }];
    for (pane, group) in app.groups.iter().enumerate() {
        for (tab, id) in group.tabs.iter().enumerate() {
            let title = app.sessions.iter().find(|session| &session.id == id).map_or(id.as_str(), |session| session.title.as_str());
            rows.push(Entry { label: format!("Open tab · {} · pane {}{}", super::safe_label(title), pane + 1,
                if pane == app.active_group && tab == group.active { " · current" } else { "" }),
                help: super::safe_label(id), action: Action::Tab(pane,tab) });
        }
    }
    for view in &app.fleet_views{rows.push(Entry{label:format!("Fleet view · {}",super::safe_label(&view.run_id)),help:super::safe_label(&view.root.display().to_string()),action:Action::Fleet(view.clone())});}
    for command in COMMANDS {
        rows.push(Entry { label: format!("{} · {}", command.name, command.summary), help: command.support.into(), action: Action::Command(command.name) });
    }
    for command in &app.plugin_commands {
        rows.push(Entry { label: format!("Plugin: {} · {}", command.name, command.summary), help: command.usage.clone(), action: Action::Plugin(command.name.clone()) });
    }
    for (command, description) in super::FLEET_ACTIONS {
        rows.push(Entry { label: format!("{command} · {description}"), help: "Current fleet run".into(), action: Action::Command(command) });
    }
    rows.extend([
        Entry { label: "Stop active session".into(), help: "Review before stopping daemon".into(), action: Action::Stop },
        Entry { label: "Inspect tool calls".into(), help: "Current session".into(), action: Action::Tools },
        Entry { label: "Close active tab".into(), help: "Daemon remains running".into(), action: Action::Close },
        Entry { label: "Focus next pane".into(), help: "Cycle pane groups".into(), action: Action::NextPane },
    ]);
    let words: Vec<_> = query.split_whitespace().map(str::to_lowercase).collect();
    rows.into_iter().filter(|entry| {
        let text = format!("{} {}", entry.label, entry.help).to_lowercase();
        words.iter().all(|word| text.contains(word))
    }).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn palette_filters_registry_and_open_tabs_without_modifying_draft() {
        let mut app=App::default();app.groups[0].tabs=vec!["alpha".into()];app.input="draft".into();
        let tabs=entries(&app,"open ALPHA");assert_eq!(tabs.len(),1);
        assert!(matches!(tabs[0].action,Action::Tab(0,0)));
        assert!(entries(&app,"/context").iter().any(|row|matches!(row.action,Action::Command("/context"))));
        assert!(entries(&app,"no-such-action-987").is_empty());assert_eq!(app.input,"draft");
    }
}
