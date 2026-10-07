//! Validated, user-configurable window shortcuts. Menu editing keys are local
//! to their menus; these bindings never depend on a project checkout.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use std::io;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    NewTab, CloseTab, CloseTabAlternate, Quit, PreviousTab, NextTab,
    PreviousPane, NextPane, NextPaneAlternate, Tools, Palette, Search,
    Settings, PeerMap, Sidebar, Diff, DiffAlternate, SplitHorizontal,
    SplitVertical, Model, Effort, Permission, Engine, Lore, Stop, DeleteTranscript,
}

#[derive(Clone, Copy)]
pub struct Definition {
    pub action: Action,
    pub key: &'static str,
    pub default: &'static str,
    pub label: &'static str,
}

pub const DEFINITIONS: &[Definition] = &[
    Definition { action: Action::NewTab, key: "key_new_tab", default: "Ctrl+T", label: "new tab" },
    Definition { action: Action::CloseTab, key: "key_close_tab", default: "Ctrl+W", label: "close tab" },
    Definition { action: Action::CloseTabAlternate, key: "key_close_tab_alt", default: "Delete", label: "close focused tab" },
    Definition { action: Action::Quit, key: "key_quit", default: "Ctrl+Q", label: "quit and detach" },
    Definition { action: Action::PreviousTab, key: "key_previous_tab", default: "Ctrl+Left", label: "previous tab" },
    Definition { action: Action::NextTab, key: "key_next_tab", default: "Ctrl+Right", label: "next tab" },
    Definition { action: Action::PreviousPane, key: "key_previous_pane", default: "Shift+Left", label: "previous pane prompt" },
    Definition { action: Action::NextPane, key: "key_next_pane", default: "Shift+Right", label: "next pane prompt" },
    Definition { action: Action::NextPaneAlternate, key: "key_next_pane_alt", default: "Alt+Tab", label: "next pane alternate" },
    Definition { action: Action::Tools, key: "key_tools", default: "Alt+T", label: "tool calls" },
    Definition { action: Action::Palette, key: "key_palette", default: "Ctrl+P", label: "action palette" },
    Definition { action: Action::Search, key: "key_search", default: "Ctrl+R", label: "session search" },
    Definition { action: Action::Settings, key: "key_settings", default: "Ctrl+,", label: "settings" },
    Definition { action: Action::PeerMap, key: "key_peer_map", default: "Ctrl+M", label: "peer map" },
    Definition { action: Action::Sidebar, key: "key_sidebar", default: "F3", label: "session rail" },
    Definition { action: Action::Diff, key: "key_diff", default: "F2", label: "diff" },
    Definition { action: Action::DiffAlternate, key: "key_diff_alt", default: "Alt+G", label: "diff alternate" },
    Definition { action: Action::SplitHorizontal, key: "key_split_horizontal", default: "Alt+H", label: "stacked split" },
    Definition { action: Action::SplitVertical, key: "key_split_vertical", default: "Alt+V", label: "side-by-side split" },
    Definition { action: Action::Model, key: "key_model", default: "Alt+M", label: "model picker" },
    Definition { action: Action::Effort, key: "key_effort", default: "Alt+F", label: "effort picker" },
    Definition { action: Action::Permission, key: "key_permission", default: "Alt+P", label: "permission picker" },
    Definition { action: Action::Engine, key: "key_engine", default: "Alt+E", label: "engine picker" },
    Definition { action: Action::Lore, key: "key_lore", default: "Alt+L", label: "LORE beliefs" },
    Definition { action: Action::Stop, key: "key_stop", default: "Ctrl+X", label: "stop session" },
    Definition { action: Action::DeleteTranscript, key: "key_delete_transcript", default: "Ctrl+Delete", label: "delete session transcript" },
];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Chord { code: KeyCode, modifiers: KeyModifiers }

impl Chord {
    pub fn parse(raw: &str) -> io::Result<Option<Self>> {
        if raw.eq_ignore_ascii_case("none") { return Ok(None); }
        let mut modifiers = KeyModifiers::NONE;
        let mut key = None;
        for part in raw.split('+') {
            let part = part.trim();
            let modifier = match part.to_ascii_lowercase().as_str() {
                "ctrl" | "control" => Some(KeyModifiers::CONTROL),
                "alt" => Some(KeyModifiers::ALT),
                "shift" => Some(KeyModifiers::SHIFT),
                _ => None,
            };
            if let Some(flag) = modifier {
                if modifiers.contains(flag) { return Err(invalid("duplicate modifier")); }
                modifiers.insert(flag);
                continue;
            }
            if key.is_some() { return Err(invalid("multiple keys")); }
            key = Some(match part.to_ascii_lowercase().as_str() {
                "left" => KeyCode::Left, "right" => KeyCode::Right,
                "up" => KeyCode::Up, "down" => KeyCode::Down,
                "tab" => KeyCode::Tab, "enter" => KeyCode::Enter,
                "delete" | "del" => KeyCode::Delete,
                "esc" | "escape" => KeyCode::Esc,
                "," | "comma" => KeyCode::Char(','),
                other if other.len() == 1 && other.bytes().all(|b| b.is_ascii_alphabetic()) =>
                    KeyCode::Char(other.chars().next().unwrap()),
                other if other.starts_with('f') && other[1..].parse::<u8>().is_ok_and(|n| (1..=12).contains(&n)) =>
                    KeyCode::F(other[1..].parse().unwrap()),
                _ => return Err(invalid("use a letter, arrow, Delete, Tab, Enter, Esc, comma or F1–F12")),
            });
        }
        let code = key.ok_or_else(|| invalid("missing key"))?;
        if modifiers.is_empty() && !matches!(code, KeyCode::F(_) | KeyCode::Delete) {
            return Err(invalid("printable and editing keys need Ctrl, Alt or Shift"));
        }
        if modifiers.contains(KeyModifiers::CONTROL)
            && matches!(code, KeyCode::Char('c' | 'v' | 'j')) {
            return Err(invalid("Ctrl+C, Ctrl+V and Ctrl+J are reserved for clipboard, cancel and newline"));
        }
        if matches!(code, KeyCode::Esc | KeyCode::Enter) {
            return Err(invalid("Enter and Escape remain local to dialogs"));
        }
        if matches!(code, KeyCode::F(4 | 5)) {
            return Err(invalid("F4 and F5 remain reserved for the diff pane"));
        }
        Ok(Some(Self { code, modifiers }))
    }

    pub fn matches(&self, event: KeyEvent) -> bool {
        let mut modifiers = event.modifiers & (KeyModifiers::CONTROL | KeyModifiers::ALT | KeyModifiers::SHIFT);
        let code = match event.code {
            KeyCode::Char(ch) => KeyCode::Char(ch.to_ascii_lowercase()),
            KeyCode::BackTab => { modifiers.insert(KeyModifiers::SHIFT); KeyCode::Tab },
            other => other,
        };
        code == self.code && modifiers == self.modifiers
    }

    pub fn display(&self) -> String {
        let mut parts = Vec::new();
        if self.modifiers.contains(KeyModifiers::CONTROL) { parts.push("Ctrl".to_owned()); }
        if self.modifiers.contains(KeyModifiers::ALT) { parts.push("Alt".to_owned()); }
        if self.modifiers.contains(KeyModifiers::SHIFT) { parts.push("Shift".to_owned()); }
        parts.push(match self.code {
            KeyCode::Char(',') => ",".into(),
            KeyCode::Char(ch) => ch.to_ascii_uppercase().to_string(),
            KeyCode::Left => "Left".into(), KeyCode::Right => "Right".into(),
            KeyCode::Up => "Up".into(), KeyCode::Down => "Down".into(),
            KeyCode::Tab => "Tab".into(), KeyCode::Delete => "Delete".into(), KeyCode::F(n) => format!("F{n}"),
            _ => unreachable!(),
        });
        parts.join("+")
    }
}

fn invalid(message: &str) -> io::Error { io::Error::new(io::ErrorKind::InvalidInput, message) }

#[derive(Clone, Debug)]
pub struct Bindings { chords: Vec<Option<Chord>> }
impl Default for Bindings {
    fn default() -> Self { Self::from_config(&toml::Table::new()).expect("valid default bindings") }
}
impl Bindings {
    pub fn from_config(config: &toml::Table) -> io::Result<Self> {
        let mut chords: Vec<Option<Chord>> = Vec::with_capacity(DEFINITIONS.len());
        for definition in DEFINITIONS {
            let raw = match config.get(definition.key) {
                Some(value) => value.as_str().ok_or_else(|| invalid(&format!("{} must be a string", definition.key)))?,
                None => definition.default,
            };
            let chord = Chord::parse(raw).map_err(|error| invalid(&format!("{}: {error}", definition.key)))?;
            if let Some(ref chord) = chord {
                if let Some(previous) = chords.iter().position(|candidate| candidate.as_ref() == Some(chord)) {
                    return Err(invalid(&format!("{} conflicts with {}", definition.key, DEFINITIONS[previous].key)));
                }
            }
            chords.push(chord);
        }
        Ok(Self { chords })
    }
    pub fn load() -> io::Result<Self> {
        let path = crate::settings::config_path()?;
        Self::from_config(&doxa_state::load_config_checked(&path)?)
    }
    pub fn matches(&self, action: Action, event: KeyEvent) -> bool {
        let index = DEFINITIONS.iter().position(|definition| definition.action == action)
            .expect("registered action");
        self.chords[index].as_ref().is_some_and(|chord| chord.matches(event))
    }
    pub fn display(&self, action: Action) -> String {
        let index = DEFINITIONS.iter().position(|definition| definition.action == action)
            .expect("registered action");
        self.chords[index].as_ref().map(Chord::display).unwrap_or_else(|| "unbound".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn defaults_are_unique_and_new_tab_owns_ctrl_t() {
        let bindings = Bindings::default();
        assert!(bindings.matches(Action::NewTab, KeyEvent::new(KeyCode::Char('t'), KeyModifiers::CONTROL)));
        assert!(!bindings.matches(Action::Tools, KeyEvent::new(KeyCode::Char('t'), KeyModifiers::CONTROL)));
        assert!(bindings.matches(Action::CloseTab, KeyEvent::new(KeyCode::Char('w'), KeyModifiers::CONTROL)));
        assert!(bindings.matches(Action::CloseTabAlternate, KeyEvent::new(KeyCode::Delete, KeyModifiers::NONE)));
        assert!(bindings.matches(Action::Stop, KeyEvent::new(KeyCode::Char('x'), KeyModifiers::CONTROL)));
        assert!(bindings.matches(Action::DeleteTranscript, KeyEvent::new(KeyCode::Delete, KeyModifiers::CONTROL)));
    }
    #[test]
    fn validates_collisions_and_canonicalizes_chords() {
        assert_eq!(Chord::parse("shift+ctrl+t").unwrap().unwrap().display(), "Ctrl+Shift+T");
        assert!(Chord::parse("t").is_err());
        assert!(Chord::parse("Ctrl+C").is_err());
        let mut config = toml::Table::new();
        config.insert("key_new_tab".into(), toml::Value::String("Alt+N".into()));
        config.insert("key_tools".into(), toml::Value::String("Alt+N".into()));
        assert!(Bindings::from_config(&config).is_err());
        config.insert("key_tools".into(), toml::Value::String("none".into()));
        assert!(Bindings::from_config(&config).unwrap().matches(Action::NewTab,
            KeyEvent::new(KeyCode::Char('n'), KeyModifiers::ALT)));
        let shift_tab = Chord::parse("Shift+Tab").unwrap().unwrap();
        assert!(shift_tab.matches(KeyEvent::new(KeyCode::BackTab, KeyModifiers::NONE)));
    }
}
