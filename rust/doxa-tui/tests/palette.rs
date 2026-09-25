use doxa_tui::{theme, ui::App};
use ratatui::{backend::TestBackend, Terminal};

#[test]
fn old_doxa_surfaces_fit_compact_and_split_terminals() {
    for (width, height) in [(80, 24), (110, 32)] {
        let app = App::default();
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        terminal.draw(|frame| app.draw(frame)).unwrap();
        let buffer = terminal.backend().buffer();
        assert_eq!(buffer[(2, 3)].bg, theme::RAIL, "rail at {width}x{height}");
        assert_eq!(buffer[(27, height - 3)].bg, theme::RAISED, "pane prompt at {width}x{height}");
        assert_eq!(buffer[(2, height - 1)].bg, theme::RAIL, "rail reaches bottom at {width}x{height}");
        assert_eq!(buffer[(27, height - 1)].bg, theme::RAISED, "pane reaches bottom at {width}x{height}");
        assert_eq!(buffer[(27, 1)].bg, theme::RAISED, "tab at {width}x{height}");
        assert_eq!(buffer[(27, 7)].bg, theme::BASE, "transcript at {width}x{height}");
        assert_eq!(buffer[(25, height - 5)].fg, theme::ACCENT, "active pane prompt border at {width}x{height}");
    }
}
