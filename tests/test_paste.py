"""doxa.paste -- pure functions, no running app needed.

The behavioral half (multi-line paste never spuriously submits, the box
grows/caps, Ctrl+V stays unbound) lives in tests/test_prompt_paste.py,
which needs a real Pilot session; this file pins the collapse/normalize
rules those tests build on.
"""

from __future__ import annotations

from doxa import paste


def test_crlf_and_lone_cr_both_become_lf():
    assert paste.normalize_newlines("a\r\nb\rc\nd") == "a\nb\nc\nd"


def test_normalize_is_a_no_op_on_already_clean_text():
    assert paste.normalize_newlines("a\nb\nc") == "a\nb\nc"
    assert paste.normalize_newlines("") == ""


def test_line_count():
    assert paste.line_count("") == 0
    assert paste.line_count("one line") == 1
    assert paste.line_count("a\nb\nc") == 3
    assert paste.line_count("trailing\n") == 2  # an empty final line counts


def test_should_collapse_is_line_count_or_byte_size():
    assert paste.should_collapse("a\nb\nc\nd") is False  # 4 lines: at the line
    assert paste.should_collapse("a\nb\nc\nd\ne") is True  # 5 lines: over it
    assert paste.should_collapse("x" * (paste.COLLAPSE_BYTES + 1)) is True
    assert paste.should_collapse("x" * (paste.COLLAPSE_BYTES - 1)) is False


def test_placeholder_format_and_pluralization():
    assert paste.placeholder_for("one line") == "⧉ pasted 1 line (0.0 KB)"
    text = "\n".join(f"line {i}" for i in range(5))
    placeholder = paste.placeholder_for(text)
    assert placeholder.startswith("⧉ pasted 5 lines (")
    assert placeholder.endswith(" KB)")


def test_placeholder_kb_is_utf8_byte_size_not_char_count():
    # A multi-byte character (e.g. the collapse glyph itself) must count
    # its ENCODED size, not len() -- the number is a network/context-cost
    # estimate, not a character tally.
    text = "é" * 1024  # 2 bytes each in UTF-8 -> 2 KB, not 1
    placeholder = paste.placeholder_for(text)
    assert "2.0 KB" in placeholder


def test_detect_clipboard_image_mime_absent_tools_returns_none(monkeypatch):
    monkeypatch.setattr(paste.shutil, "which", lambda _name: None)
    assert paste.detect_clipboard_image_mime() is None


def test_detect_clipboard_image_mime_reads_wl_paste_list_types(monkeypatch):
    monkeypatch.setattr(
        paste.shutil, "which", lambda name: "/usr/bin/wl-paste" if name == "wl-paste" else None
    )

    def fake_run(cmd, **kwargs):
        class Result:
            stdout = "text/plain\nimage/png\n"
        return Result()

    monkeypatch.setattr(paste.subprocess, "run", fake_run)
    assert paste.detect_clipboard_image_mime() == "image/png"


def test_detect_clipboard_image_mime_text_only_clipboard(monkeypatch):
    monkeypatch.setattr(
        paste.shutil, "which", lambda name: "/usr/bin/xclip" if name == "xclip" else None
    )

    def fake_run(cmd, **kwargs):
        class Result:
            stdout = "text/plain\nUTF8_STRING\n"
        return Result()

    monkeypatch.setattr(paste.subprocess, "run", fake_run)
    assert paste.detect_clipboard_image_mime() is None


def test_detect_clipboard_image_mime_never_raises_on_probe_failure(monkeypatch):
    monkeypatch.setattr(paste.shutil, "which", lambda _name: "/usr/bin/wl-paste")

    def fake_run(cmd, **kwargs):
        raise OSError("no display")

    monkeypatch.setattr(paste.subprocess, "run", fake_run)
    assert paste.detect_clipboard_image_mime() is None
