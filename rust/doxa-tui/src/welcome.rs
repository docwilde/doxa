//! Native opening view. App chrome is styled terminal text, never a transcript
//! record and never Markdown source supplied to the conversation renderer.
use ratatui::{style::{Modifier,Style},text::Line};
use crate::{markdown,theme};

pub enum State<'a> {
    Connecting,
    Starting,
    Ready {engine:Option<&'a str>,model:Option<&'a str>},
    Empty {reason:Option<&'a str>},
}

pub fn lines(state:State<'_>,mark:bool,width:u16,height:u16)->Vec<Line<'static>> {
    let mut lines=Vec::new();
    if mark && width>=24 && height>=16 {
        for row in ["       █", "      ███", "    ███████", "   █████████   DOXA",
            "  ███████████", " █████████████", "███████████████"] {
            lines.push(Line::styled(row,Style::default().fg(theme::ACCENT)));
        }
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
    fn compact_recovery_keeps_actions_and_native_styles() {
        let lines=lines(State::Empty {reason:Some("Startup unavailable")},true,40,8);
        let text=lines.iter().map(|line|line.spans.iter().map(|span|span.content.as_ref()).collect::<String>()).collect::<Vec<_>>().join("\n");
        assert!(!text.contains('█'));
        assert!(text.contains("/setup") && text.contains("/engine"));
        assert!(lines.iter().flat_map(|line|&line.spans).all(|span|span.style.bg.is_none()));
    }
}
