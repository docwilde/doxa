"""Screen replay and statistics checks for the release TUI code-graph harness."""

import unittest

from scripts import bench_codegraph_tui as bench


class ScreenReplayTests(unittest.TestCase):
    def test_cursor_moves_styles_and_split_utf8_reach_the_right_cells(self):
        screen = bench.Screen(24, 4)
        data = ("\x1b[?1049h\x1b[2J\x1b[1;1H\x1b[38;2;1;2;3mfile a.rs · ok\x1b[0m"
                "\x1b[2;2H\x1b]8;;http://x\x1b\\Work\x1b[1mtree:\x1b[?25l\x1b(B"
                "\x1b[3;1H中x\x1b[3;2K").encode()
        for index in range(len(data)):  # Every escape and UTF-8 sequence split across reads.
            screen.feed(data[index:index + 1])
        rows = screen.text().split("\n")
        self.assertEqual(rows[0].rstrip(), "file a.rs · ok")
        self.assertEqual(rows[1].rstrip(), " Worktree:")
        self.assertEqual(rows[2].rstrip(), "")
        self.assertTrue(bench.answer_visible(screen.text(), "file a.rs"))
        self.assertFalse(bench.answer_visible(screen.text(), "file b.rs"))

    def test_completion_marker_survives_style_changes_but_not_cell_skips(self):
        styled = b"\x1b[2;3H\x1b[38;5;1mWork\x1b[48;2;1;1;1mtree:\x1b[0m"
        self.assertIn(b"Worktree:", bench.ESCAPES.sub(b"", styled))
        # A skipped unchanged cell is a cursor move: the cheap check misses it,
        # which is why visibility is decided only by the replayed screen.
        skipped = b"\x1b[2;3HWork\x1b[2;8Hree:"
        self.assertNotIn(b"Worktree:", bench.ESCAPES.sub(b"", skipped))

    def test_origin_and_coverage_labels(self):
        text = ("Reused parsed declarations · complete source inventory rechecked; call sites reparsed\n"
                "Coverage: 1336 listed · 238 parsed Rust · 95 parsed Python · 0 skipped · 101 unparseable")
        self.assertEqual(bench.origin(text), "indexed")
        self.assertEqual(bench.coverage(text), {"listed": 1336, "parsed_rust": 238, "parsed_python": 95,
                                                "skipped": 0, "unparseable": 101})
        self.assertEqual(bench.origin("Reused syntax answer · Git paths"), "revalidated")
        self.assertEqual(bench.origin("Code graph query failed; no partial answer"), "error")
        self.assertEqual(bench.origin("Coverage: 1 listed"), "fresh")

    def test_nearest_rank_statistics(self):
        values = [float(value) for value in range(1, 21)]
        summary = bench.stats(values)
        self.assertEqual(summary["p50_ms"], 10.5)
        self.assertEqual(summary["p95_ms"], 19.0)
        self.assertEqual(summary["max_ms"], 20.0)
        self.assertEqual(bench.stats([])["n"], 0)


if __name__ == "__main__":
    unittest.main()
