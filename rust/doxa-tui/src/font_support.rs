//! Conservative, process-local icon selection. Terminals do not expose a
//! portable glyph-coverage query; installed host fonts are not evidence about
//! the rendering client (especially through SSH and multiplexers).
use std::{io::IsTerminal, sync::OnceLock};

pub(crate) fn mode(value: &str) -> Option<&'static str> {
    match value.trim().to_ascii_lowercase().as_str() {
        "" | "auto" => Some("auto"),
        "on" | "1" | "true" | "yes" => Some("on"),
        "off" | "0" | "false" | "no" => Some("off"),
        _ => None,
    }
}

pub(crate) fn enabled(value: &str) -> bool {
    match mode(value) {
        Some("on") => true,
        Some("auto") => automatic(),
        _ => false,
    }
}

fn automatic() -> bool {
    static SUPPORTED: OnceLock<bool> = OnceLock::new();
    *SUPPORTED.get_or_init(|| detect(std::io::stdout().is_terminal(), |key| std::env::var(key).ok()))
}

fn detect(terminal: bool, env: impl Fn(&str) -> Option<String>) -> bool {
    if !terminal { return false; }
    let get = |key| env(key).unwrap_or_default();
    let term = get("TERM");
    if term.is_empty() || matches!(term.as_str(), "dumb" | "linux") { return false; }
    // Inherited server-side identity can describe a different attached client.
    // VTE itself supplies no glyph-coverage evidence; prefer readable labels.
    if ["SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "TMUX", "STY", "VTE_VERSION"]
        .iter().any(|key| !get(key).is_empty())
        || term.starts_with("screen") || term.starts_with("tmux") { return false; }
    // These terminals ship their own Nerd Font fallback, independent of the
    // selected text face. Do not infer support from COLORTERM or an xterm TERM.
    // https://sw.kovidgoyal.net/kitty/faq/#kitty-is-not-able-to-use-my-favorite-font
    // https://wezterm.org/config/fonts.html#fallback
    match get("TERM_PROGRAM").to_ascii_lowercase().as_str() {
        "wezterm" => true,
        "kitty" => term == "xterm-kitty",
        _ => term == "xterm-kitty" && !get("KITTY_WINDOW_ID").is_empty(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn supported(terminal: bool, values: &[(&str, &str)]) -> bool {
        detect(terminal, |key| values.iter().find(|(name, _)| *name == key).map(|(_, value)| (*value).into()))
    }
    #[test]
    fn unknown_and_stock_vte_terminals_use_labels() {
        for values in [vec![], vec![("TERM", "xterm-256color")],
            vec![("TERM", "xterm-256color"), ("VTE_VERSION", "8200"), ("COLORTERM", "truecolor")],
            vec![("TERM", "xterm-256color"), ("TERM_PROGRAM", "vscode")]] {
            assert!(!supported(true, &values));
        }
    }
    #[test]
    fn bundled_symbols_enable_icons_only_for_direct_terminal_clients() {
        for values in [vec![("TERM", "xterm-kitty"), ("KITTY_WINDOW_ID", "3")],
            vec![("TERM", "xterm-kitty"), ("TERM_PROGRAM", "kitty")],
            vec![("TERM", "xterm-256color"), ("TERM_PROGRAM", "WezTerm")]] {
            assert!(supported(true, &values));
            assert!(!supported(false, &values));
            for boundary in ["SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "TMUX", "STY", "VTE_VERSION"] {
                let mut inherited=values.clone(); inherited.push((boundary, "present"));
                assert!(!supported(true, &inherited), "inherited {boundary}");
            }
        }
        for term in ["", "dumb", "linux", "screen-256color", "tmux-256color"] {
            assert!(!supported(true, &[("TERM",term),("TERM_PROGRAM","WezTerm")]));
        }
    }
    #[test]
    fn manual_modes_preserve_legacy_boolean_spellings() {
        for value in ["on", "1", "true", "yes", " ON "] { assert!(enabled(value)); }
        for value in ["off", "0", "false", "no", " OFF ", "invalid"] { assert!(!enabled(value)); }
        assert_eq!(mode("auto"),Some("auto"));
        assert_eq!(mode(""),Some("auto"));
        assert_eq!(mode("invalid"),None);
    }
}
