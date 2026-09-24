//! Render the daemon's persisted JSONL conversation for the early Rust UI.
//! This view is bounded; the JSONL file remains the complete record.

use serde_json::Value;
use crate::transport::TranscriptSnapshot;

const MAX_TURNS: usize = 40;
const MAX_TEXT_CHARS: usize = 20_000;
const MAX_VIEW_BYTES: usize = 480 * 1024;

#[derive(Default)]
struct Turn { prompt: String, answer: String, tools: Vec<String> }

pub fn render(snapshot: &TranscriptSnapshot) -> String {
    let mut turns: Vec<Turn> = Vec::new();
    for line in snapshot.bytes.split(|byte| *byte == b'\n') {
        let Ok(record) = serde_json::from_slice::<Value>(line) else { continue };
        let kind = record["type"].as_str().unwrap_or("");
        let content = &record["message"]["content"];
        if kind == "user" {
            if let Some(prompt) = content.as_str() {
                turns.push(Turn { prompt: prompt.to_owned(), ..Turn::default() });
                continue;
            }
        }
        if kind != "assistant" { continue; }
        let Some(blocks) = content.as_array() else { continue };
        if turns.is_empty() { turns.push(Turn::default()); }
        let turn = turns.last_mut().unwrap();
        for block in blocks {
            match block["type"].as_str() {
                Some("text") => turn.answer.push_str(block["text"].as_str().unwrap_or("")),
                Some("tool_use") => {
                    let name = block["name"].as_str().unwrap_or("tool");
                    turn.tools.push(format!("Tool: {}", name.replace('\n', " ")));
                }
                _ => {}
            }
        }
    }
    let omitted_turns = turns.len().saturating_sub(MAX_TURNS);
    let mut out = String::new();
    if snapshot.earlier_bytes_omitted || omitted_turns > 0 {
        out.push_str("[Earlier transcript omitted from this view; the session JSONL retains it.]\n\n");
    }
    for turn in turns.into_iter().skip(omitted_turns) {
        if !turn.prompt.is_empty() {
            out.push_str("**You:**\n\n");
            out.push_str(&turn.prompt);
            out.push_str("\n\n");
        }
        if !turn.answer.is_empty() {
            out.push_str("**Assistant:**\n\n");
            let shortened: String = turn.answer.chars().take(MAX_TEXT_CHARS).collect();
            out.push_str(&shortened);
            if shortened.len() < turn.answer.len() { out.push_str("\n[Assistant text shortened in this view]"); }
            out.push_str("\n\n");
        }
        for tool in turn.tools.iter().take(30) {
            out.push_str(&format!("[{tool}]\n\n"));
        }
        if turn.tools.len() > 30 { out.push_str("[Additional tools omitted from this view]\n\n"); }
    }
    if out.len() > MAX_VIEW_BYTES {
        let mut start = out.len() - MAX_VIEW_BYTES;
        while !out.is_char_boundary(start) { start += 1; }
        out = format!("[Earlier transcript omitted from this view; the session JSONL retains it.]\n\n{}", &out[start..]);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn restores_prompts_and_assistant_text_without_tool_result_turns() {
        let lines = concat!(
            "{\"type\":\"user\",\"message\":{\"content\":\"first?\"}}\n",
            "{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"yes\"},{\"type\":\"tool_use\",\"name\":\"Search\"}]}}\n",
            "{\"type\":\"user\",\"message\":{\"content\":[{\"type\":\"tool_result\",\"content\":\"found\"}]}}\n",
            "{\"type\":\"user\",\"message\":{\"content\":\"second?\"}}\n",
        );
        let rendered = render(&TranscriptSnapshot { bytes: lines.as_bytes().to_vec(), earlier_bytes_omitted: false });
        assert!(rendered.contains("first?"));
        assert!(rendered.contains("yes"));
        assert!(rendered.contains("[Tool: Search]"));
        assert!(rendered.contains("second?"));
        assert!(!rendered.contains("found"));
    }
}
