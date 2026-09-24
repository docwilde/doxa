use doxa_tui::markdown::render;
use ratatui::style::{Color, Modifier};
use unicode_width::UnicodeWidthStr;

fn plain(source: &str, width: u16) -> Vec<String> {
    render(source, width).iter().map(|line| {
        line.spans.iter().map(|span| span.content.as_ref()).collect::<String>()
    }).collect()
}

#[test]
fn transcript_prose_and_inline_styles() {
    let lines = render("# Summary\n\nUse **strong**, *emphasis*, and `code`.", 80);
    assert!(lines.iter().flat_map(|line| &line.spans).any(|span| {
        span.content.contains("Summary") && span.style.add_modifier.contains(Modifier::BOLD)
    }));
    assert!(lines.iter().flat_map(|line| &line.spans).any(|span| {
        span.content.contains('s') && span.style.add_modifier.contains(Modifier::BOLD)
    }));
    assert!(lines.iter().flat_map(|line| &line.spans).any(|span| {
        span.content.contains('e') && span.style.add_modifier.contains(Modifier::ITALIC)
    }));
    assert!(lines.iter().flat_map(|line| &line.spans).any(|span| {
        span.content.contains('c') && span.style.fg == Some(Color::Yellow)
    }));
    assert_eq!(plain("# Summary\n\nUse **strong**, *emphasis*, and `code`.", 80).concat(),
        "SummaryUse strong, emphasis, and code.");
}

#[test]
fn lists_quotes_rules_and_links_survive_streamed_reparse() {
    let first = "- first item\n- second";
    let more = " item\n\n> quoted [source](https://example.com)\n\n---\n";
    let before = plain(first, 80);
    assert_eq!(before, ["•  first item", "•  second"]);
    let after = plain(&format!("{first}{more}"), 80);
    assert_eq!(after[0], before[0]);
    assert_eq!(after[1], "•  second item");
    assert!(after.iter().any(|line| line.contains("│ quoted source (https://example.com)")));
    assert!(after.iter().any(|line| line.starts_with('─')));
}

#[test]
fn ordered_and_nested_lists_keep_indentation() {
    let lines = plain("9. alpha\n10. beta\n    - nested\n11. gamma", 80);
    assert!(lines.iter().any(|line| line.starts_with("9. alpha")));
    assert!(lines.iter().any(|line| line.starts_with("10. beta")));
    assert!(lines.iter().any(|line| line.contains("•  nested")));
    assert!(lines.iter().any(|line| line.starts_with("11. gamma")));
}

#[test]
fn narrow_unicode_wrap_uses_terminal_cells() {
    let lines = plain("- café 界界 wideword", 10);
    assert!(lines.len() > 1);
    for line in &lines { assert!(UnicodeWidthStr::width(line.as_str()) <= 10, "{line:?}"); }
    assert!(lines.join("").contains("界界"));
    for line in plain("- 界", 1) {
        assert!(UnicodeWidthStr::width(line.as_str()) <= 1, "{line:?}");
    }
}

#[test]
fn long_streamed_list_preserves_every_item_after_append() {
    let mut source = String::new();
    for index in 0..120 {
        source.push_str(&format!("- transcript line {index:03}: reproducible text\n"));
    }
    let before = plain(&source, 80);
    assert_eq!(before.len(), 120);
    source.push_str("- final appended item\n");
    let after = plain(&source, 80);
    assert_eq!(&after[..120], before);
    assert_eq!(after.last().unwrap(), "•  final appended item");
}

#[test]
fn code_fences_reparse_after_streaming_completion() {
    let partial = "```rust\nlet n = 1;";
    assert!(plain(partial, 80).join(" ").contains("let n = 1;"));
    let complete = format!("{partial}\n```\nAfterward.");
    assert!(plain(&complete, 80).join(" ").contains("Afterward."));
}

#[test]
fn table_and_untrusted_control_text() {
    let table = "| Key | Value |\n| --- | --- |\n| A | **B** |";
    let lines = plain(table, 40);
    assert!(lines.iter().any(|line| line.contains("Key") && line.contains("Value")));
    assert!(lines.iter().any(|line| line.contains('A') && line.contains('B')));
    assert!(lines.iter().any(|line| line.starts_with('├')));
    let unsafe_text = plain("answer \u{1b}[31m \u{202e}[click](https://example.com/\u{1b}x)", 80).join(" ");
    assert!(!unsafe_text.contains('\u{1b}'));
    assert!(!unsafe_text.contains('\u{202e}'));
    assert!(unsafe_text.contains('�'));
}

#[test]
fn table_columns_align_with_unicode_styles_and_gfm_alignment() {
    let source = "| Name | Count | Center |\n| :--- | ---: | :---: |\n| **界** | 7 | x |\n| café | 123 | yz |";
    let lines = render(source, 40);
    let plain: Vec<String> = lines.iter().map(|line| line.spans.iter().map(|span| span.content.as_ref()).collect()).collect();
    assert_eq!(plain.len(), 4);
    let borders: Vec<Vec<usize>> = plain.iter().filter(|line| line.starts_with('│')).map(|line| {
        line.char_indices().filter_map(|(byte, ch)| (ch == '│').then_some(UnicodeWidthStr::width(&line[..byte]))).collect()
    }).collect();
    assert!(borders.windows(2).all(|pair| pair[0] == pair[1]), "{plain:?}");
    assert!(plain.iter().all(|line| UnicodeWidthStr::width(line.as_str()) <= 40));
    assert!(plain[2].contains("7 "));
    assert!(lines[2].spans.iter().any(|span| span.content.contains('界') && span.style.add_modifier.contains(Modifier::BOLD)));
}

#[test]
fn narrow_tables_wrap_cells_without_exceeding_viewport() {
    let source = "| Key | Value |\n| --- | --- |\n| 界界界 | abcdefghijklmnop |";
    for width in [1, 5, 11, 16] {
        let lines = plain(source, width);
        assert!(!lines.is_empty());
        assert!(lines.iter().all(|line| UnicodeWidthStr::width(line.as_str()) <= width as usize), "{width}: {lines:?}");
    }
    let lines = plain(source, 16);
    assert!(lines.iter().any(|line| line.contains("abc")));
    assert!(lines.len() > 3);
}

#[test]
fn long_table_cell_stays_bounded_by_viewport() {
    let source = format!("| Data |\n| --- |\n| {} |", "x".repeat(10_000));
    let lines = plain(&source, 20);
    assert!(lines.len() > 500);
    assert!(lines.iter().all(|line| UnicodeWidthStr::width(line.as_str()) <= 20));
    assert_eq!(lines.iter().map(|line| line.matches('x').count()).sum::<usize>(), 10_000);
}

#[test]
fn link_destination_is_visible_and_sanitized_in_table_cell() {
    let source = "| Ref |\n| --- |\n| [go](https://example.com/x) |";
    let lines = plain(source, 40).join("\n");
    assert!(lines.contains("go (https://example.com/x)"));
    let lines = plain("| Ref |\n| --- |\n| [go](https://example.com/\u{1b}]8;;evil\u{7}) |", 28).join("\n");
    assert!(!lines.contains('\u{1b}'));
    assert!(!lines.contains('\u{7}'));
}
