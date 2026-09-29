//! Native opening view. App chrome is styled terminal text, never a transcript
//! record and never Markdown source supplied to the conversation renderer.
use ratatui::{style::{Modifier,Style},text::Line};
use crate::{markdown,theme};

// Match the established Python ΔΟΞΑ banner with terminal-safe full blocks.
const MARK: [&str;7] = [
    "       █       ", "      ███      ", "    ███████    ",
    "   █████████   ", "  ███████████  ", " █████████████ ",
    "███████████████",
];
const DELTA: [&str;7] = [
    "    █    ", "   █ █   ", "  █   █  ", " █     █ ",
    "█       █", "█       █", "█████████",
];
const OMICRON: [&str;7] = [
    "  ███  ", " █   █ ", "█     █", "█     █",
    "█     █", " █   █ ", "  ███  ",
];
const XI: [&str;7] = [
    "█████████", "         ", "         ", "  █████  ",
    "         ", "         ", "█████████",
];
const ALPHA: [&str;7] = [
    "    █    ", "   █ █   ", "  █   █  ", " ███████ ",
    "█       █", "█       █", "█       █",
];

fn greek_row(row:usize)->String {
    format!("{}  {}  {}  {}",DELTA[row],OMICRON[row],XI[row],ALPHA[row])
}

pub enum State<'a> {
    Connecting,
    Starting,
    Ready {engine:Option<&'a str>,model:Option<&'a str>},
    Empty {reason:Option<&'a str>},
}

pub fn lines(state:State<'_>,mark:bool,width:u16,height:u16)->Vec<Line<'static>> {
    let mut lines=Vec::new();
    let content_rows=match &state {
        State::Connecting|State::Starting=>2,
        State::Ready {engine,model}=>3+usize::from(engine.is_some())+usize::from(model.is_some()),
        State::Empty {reason}=>4+usize::from(reason.is_some()),
    };
    let accent=Style::default().fg(theme::ACCENT);
    if mark && width>=58 && usize::from(height)>=content_rows+10 {
        for row in 0..MARK.len() {
            lines.push(Line::styled(format!("{}   {}",MARK[row],greek_row(row)),accent));
        }
        lines.push(Line::default());
        lines.push(Line::styled("                  belief earns knowledge",Style::default().fg(theme::SECONDARY)));
        lines.push(Line::default());
    } else if mark && width>=40 && usize::from(height)>=content_rows+8 {
        for row in 0..MARK.len() { lines.push(Line::styled(greek_row(row),accent)); }
        lines.push(Line::default());
    } else if mark && width>=22 && usize::from(height)>=content_rows+8 {
        for (row,mark_row) in MARK.iter().enumerate() {
            let label=if row==3 {"DOXA"}else{""};
            lines.push(Line::styled(format!("{mark_row}   {label}"),accent));
        }
        lines.push(Line::default());
    } else if mark && width>=8 && usize::from(height)>=content_rows+2 {
        lines.push(Line::styled("ΔΟΞΑ",accent));
        lines.push(Line::default());
    }
    let heading=Style::default().fg(theme::TEXT).add_modifier(Modifier::BOLD);
    let hint=Style::default().fg(theme::SECONDARY);
    match state {
        State::Connecting=>{
            lines.push(Line::styled("Connecting to session…",heading));
            lines.push(Line::styled("Waiting for the daemon's session identity.",hint));
        }
        State::Starting=>{
            lines.push(Line::styled("Starting session…",heading));
            lines.push(Line::styled("Waiting for the new session's identity.",hint));
        }
        State::Ready {engine,model}=>{
            lines.push(Line::styled("Session ready",heading));
            if let Some(engine)=engine { lines.push(Line::styled(format!("Engine · {}",markdown::sanitize(engine)),hint)); }
            if let Some(model)=model { lines.push(Line::styled(format!("Model · {}",markdown::sanitize(model)),hint)); }
            lines.push(Line::default());
            lines.push(Line::styled("Type a prompt below. /help lists commands.",hint));
        }
        State::Empty {reason}=>{
            lines.push(Line::styled(if reason.is_some(){"Session could not start"}else{"No session connected"},heading));
            if let Some(reason)=reason {lines.push(Line::styled(markdown::sanitize(reason),Style::default().fg(theme::ERROR)));}
            lines.push(Line::styled("/setup checks authentication and dependencies.",hint));
            lines.push(Line::styled("/engine selects an engine and starts a session here.",hint));
            lines.push(Line::styled("Ctrl+P opens actions. The prompt remains available below.",hint));
        }
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn full_banner_uses_greek_blocks_and_fits_its_pane() {
        let lines=lines(State::Ready {engine:Some("claude"),model:Some("opus")},true,70,16);
        assert_eq!(lines.len(),15);
        assert!(lines.iter().take(7).all(|line|line.spans.iter().all(|span|span.content.chars().all(|c|c=='█'||c==' '))));
        assert!(lines.iter().take(7).all(|line|line.width()<=70));
        assert!(lines.iter().any(|line|line.spans.iter().any(|span|span.content.contains("belief earns knowledge"))));
    }
    #[test]
    fn narrow_pane_keeps_block_wordmark_without_clipping() {
        let lines=lines(State::Empty {reason:Some("Startup unavailable")},true,40,13);
        assert_eq!(lines.len(),13);
        assert!(lines.iter().take(7).any(|line|line.spans.iter().any(|span|span.content.contains('█'))));
        assert!(lines.iter().take(7).all(|line|line.width()<=40));
    }
    #[test]
    fn compact_recovery_keeps_actions_and_native_styles() {
        let lines=lines(State::Empty {reason:Some("Startup unavailable")},true,40,8);
        let text=lines.iter().map(|line|line.spans.iter().map(|span|span.content.as_ref()).collect::<String>()).collect::<Vec<_>>().join("\n");
        assert!(!text.contains('█'));
        assert!(text.contains("/setup") && text.contains("/engine"));
        assert!(lines.iter().flat_map(|line|&line.spans).all(|span|span.style.bg.is_none()));
    }
}
